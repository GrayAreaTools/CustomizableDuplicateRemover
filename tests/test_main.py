"""Dispatch and the plugin protocol envelope."""

import io
import json
import sys

import pytest

import main
from models import ParseError


class StubClient:
    """Stands in for StashClient across the dispatch surface."""

    def __init__(self, *args, **kwargs):
        self.settings = {}
        self.written = None

    def call(self, query, variables=None, **kwargs):
        return {}

    def plugin_settings(self, plugin_id):
        return dict(self.settings)

    def configure_plugin(self, plugin_id, settings):
        # configurePlugin replaces the whole map, so the stub does too - that is the
        # behaviour the read-modify-write in mode_ui_save_settings has to cope with.
        self.written = dict(settings)
        self.settings = dict(settings)
        return dict(settings)

    def version(self):
        return "0.28.0"

    def library_paths(self):
        return ["/media/library"]

    def scene_count(self):
        return 100

    def duplicate_groups(self, distance, duration_diff):
        return []

    def scenes_missing_phash(self):
        return 0


@pytest.fixture
def stub(monkeypatch):
    client = StubClient()
    monkeypatch.setattr(main, "StashClient", lambda *a, **k: client)
    return client


def payload(mode, **args):
    return {
        "server_connection": {"Scheme": "http", "Host": "127.0.0.1", "Port": 9999},
        "args": {"mode": mode, **args},
    }


def run_main(monkeypatch, data):
    """Invoke main() with stdin/stdout captured, as Stash does."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = main.main()
    return code, json.loads(out.getvalue())


def test_plan_mode_returns_a_summary_envelope(monkeypatch, stub, tmp_path):
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False}

    code, envelope = run_main(monkeypatch, payload("plan"))

    assert code == 0
    assert envelope["error"] is None
    assert envelope["output"]["groups"] == 0
    assert envelope["output"]["filesToDelete"] == 0


def test_unknown_mode_is_reported_not_guessed(monkeypatch, stub):
    code, envelope = run_main(monkeypatch, payload("delete_everything"))

    assert code == 1
    assert "Unknown mode" in envelope["error"]
    assert envelope["output"]["code"] == "UNKNOWN_MODE"


def test_invalid_setting_aborts_before_any_work(monkeypatch, stub):
    stub.settings = {"rankOrder": "codec,nonsense"}

    code, envelope = run_main(monkeypatch, payload("plan"))

    assert code == 1
    assert envelope["output"]["code"] == "SETTING_UNKNOWN_RANK_KEY"
    assert "nonsense" in envelope["error"]


def test_malformed_stdin_produces_an_error_envelope(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    code = main.main()

    assert code == 1
    assert "not valid JSON" in json.loads(out.getvalue())["error"]


def test_stdout_carries_exactly_one_json_object(monkeypatch, stub, tmp_path):
    """Stash parses stdout, so nothing else may be written there."""
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload("plan"))))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    main.main()

    # json.loads on the whole buffer succeeds only if there is one object and no extras.
    assert isinstance(json.loads(out.getvalue()), dict)


def test_an_unexpected_exception_still_yields_a_valid_envelope(monkeypatch, stub):
    def explode(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(main, "mode_plan", explode)

    code, envelope = run_main(monkeypatch, payload("plan"))

    assert code == 1
    assert "RuntimeError" in envelope["error"]


def test_execute_refuses_when_confirm_destructive_is_off(monkeypatch, stub, tmp_path):
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False}
    # Write a plan so the failure is the confirmation gate, not a missing file.
    run_main(monkeypatch, payload("plan"))

    code, envelope = run_main(monkeypatch, payload("execute"))

    assert code == 1
    assert envelope["output"]["code"] == "G_CONFIRM"


def test_overrides_cannot_enable_destruction(monkeypatch, stub, tmp_path):
    """The destructive gate lives in settings only; a page request must not set it."""
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False}
    run_main(monkeypatch, payload("plan"))

    code, envelope = run_main(
        monkeypatch, payload("ui_execute", overrides={"confirmDestructive": True},
                             selection=[])
    )

    assert code == 1
    assert envelope["output"]["code"] == "OVERRIDE_NOT_PERMITTED"


@pytest.mark.parametrize(
    "setting,value",
    [
        ("confirmDestructive", True),
        ("protectedPaths", ""),
        ("qualityFloorRatio", 0),
        ("qualityFloorRatioCrossCodec", 0),
        ("durationTolerance", 3600),
        ("metadataPolicy", "ignore"),
        ("maxDeletionsPerRun", 100000),
        ("maxFractionOfLibrary", 1.0),
        ("reportDir", "/etc/cron.d"),
    ],
)
def test_overrides_cannot_relax_any_safety_control(monkeypatch, stub, tmp_path,
                                                   setting, value):
    """Regression: a denylist excluding only confirmDestructive left every other rail
    parameter overridable, so one request could empty protectedPaths, zero the quality
    floor, widen the duration tolerance and lift both caps."""
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False}

    code, envelope = run_main(
        monkeypatch, payload("ui_plan", overrides={setting: value})
    )

    assert code == 1
    assert envelope["output"]["code"] == "OVERRIDE_NOT_PERMITTED"
    assert envelope["output"]["context"]["setting"] == setting


def test_overrides_the_page_actually_sends_are_permitted(monkeypatch, stub, tmp_path):
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False}

    code, envelope = run_main(
        monkeypatch,
        payload("ui_plan", overrides={"rankOrder": "resolution,codec",
                                      "tieBreaker": "largest",
                                      "phashDistance": 4, "durationDiff": 2}),
    )

    assert code == 0
    assert envelope["output"]["summary"]["config"]["rankOrder"] == ["resolution", "codec"]


def test_overridable_settings_excludes_every_safety_control():
    forbidden = {"confirmDestructive", "maxDeletionsPerRun", "maxFractionOfLibrary",
                 "protectedPaths", "reportDir", "durationTolerance", "metadataPolicy",
                 "qualityFloorRatio", "qualityFloorRatioCrossCodec"}

    assert not forbidden & set(main.OVERRIDABLE_SETTINGS)


def test_apply_overrides_merges_permitted_keys():
    merged = main._apply_overrides(
        {"rankOrder": "codec", "confirmDestructive": False},
        {"rankOrder": "resolution", "tieBreaker": "largest"},
    )

    assert merged["rankOrder"] == "resolution"
    assert merged["tieBreaker"] == "largest"
    assert merged["confirmDestructive"] is False


def test_apply_overrides_rejects_rather_than_ignores():
    """Silently dropping an override would run under different rails than the caller
    asked for, which is worse than refusing."""
    with pytest.raises(ParseError) as excinfo:
        main._apply_overrides({}, {"maxDeletionsPerRun": 99999})

    assert excinfo.value.code == "OVERRIDE_NOT_PERMITTED"
    assert excinfo.value.context["setting"] == "maxDeletionsPerRun"


def test_apply_overrides_tolerates_none():
    assert main._apply_overrides({"rankOrder": "codec"}, None) == {"rankOrder": "codec"}


def test_read_only_modes_are_declared():
    """Guards against a destructive mode being added to the read-only list by mistake."""
    assert "execute" not in main.READ_ONLY_MODES
    assert "plan_execute" not in main.READ_ONLY_MODES
    assert "ui_execute" not in main.READ_ONLY_MODES
    assert "plan" in main.READ_ONLY_MODES


def test_resolve_report_dir_prefers_the_plugin_dir():
    assert main.resolve_report_dir("/config", "/plugins/cdr") == "/plugins/cdr/reports"
    assert main.resolve_report_dir("/config", "") == "/config/reports"


def test_ui_config_returns_live_settings_not_a_plan_snapshot(monkeypatch, stub, tmp_path):
    """Regression: the page read confirmDestructive from the plan's frozen config, so
    enabling the setting after planning left the delete button disabled forever."""
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False}

    # Plan while the gate is closed, so the plan file records confirmDestructive: false.
    _, planned = run_main(monkeypatch, payload("plan"))
    assert planned["output"]["config"]["confirmDestructive"] is False

    # Operator now opens the gate in the plugin settings.
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False,
                     "confirmDestructive": True}

    code, envelope = run_main(monkeypatch, payload("ui_config"))

    assert code == 0
    assert envelope["output"]["config"]["confirmDestructive"] is True
    assert envelope["output"]["planAvailable"] is True

    # The stale snapshot inside the plan file must still say false, which is exactly why
    # the page must not read it.
    code, loaded = run_main(monkeypatch, payload("ui_load"))
    assert loaded["output"]["summary"]["config"]["confirmDestructive"] is False


def test_ui_config_reports_when_no_plan_exists(monkeypatch, stub, tmp_path):
    stub.settings = {"reportDir": str(tmp_path / "empty"), "applyTags": False}

    code, envelope = run_main(monkeypatch, payload("ui_config"))

    assert code == 0
    assert envelope["output"]["planAvailable"] is False


def test_ui_config_is_read_only(monkeypatch, stub, tmp_path):
    stub.settings = {"reportDir": str(tmp_path), "applyTags": False}
    assert "ui_config" in main.READ_ONLY_MODES

    code, envelope = run_main(monkeypatch, payload("ui_config"))

    assert code == 0
    assert envelope["error"] is None


# -- ui_save_settings -------------------------------------------------------

def test_save_settings_preserves_settings_it_did_not_change(monkeypatch, stub, tmp_path):
    """configurePlugin overwrites the whole map, so a partial write would silently drop
    protectedPaths and the run caps back to their defaults."""
    stub.settings = {
        "reportDir": str(tmp_path),
        "protectedPaths": "/media/originals/",
        "maxDeletionsPerRun": 5,
        "confirmDestructive": True,
        "rankOrder": "codec,size",
    }

    code, envelope = run_main(
        monkeypatch,
        payload("ui_save_settings", settings={"rankOrder": "resolution,codec"}),
    )

    assert code == 0
    written = stub.written
    assert written["rankOrder"] == "resolution,codec"
    assert written["protectedPaths"] == "/media/originals/"
    assert written["maxDeletionsPerRun"] == 5
    assert written["confirmDestructive"] is True
    assert written["reportDir"] == str(tmp_path)


def test_save_settings_returns_the_validated_config(monkeypatch, stub, tmp_path):
    stub.settings = {"reportDir": str(tmp_path)}

    code, envelope = run_main(
        monkeypatch,
        payload("ui_save_settings",
                settings={"rankOrder": "resolution,codec", "tieBreaker": "largest"}),
    )

    assert code == 0
    assert envelope["output"]["config"]["rankOrder"] == ["resolution", "codec"]
    assert envelope["output"]["config"]["tieBreaker"] == "largest"
    assert envelope["output"]["saved"] == ["rankOrder", "tieBreaker"]


@pytest.mark.parametrize(
    "setting", ["confirmDestructive", "maxDeletionsPerRun", "protectedPaths", "reportDir",
                "maxFractionOfLibrary"]
)
def test_save_settings_refuses_to_write_safety_settings(monkeypatch, stub, tmp_path, setting):
    """The page must never be able to widen what a later run may delete."""
    stub.settings = {"reportDir": str(tmp_path)}

    code, envelope = run_main(
        monkeypatch, payload("ui_save_settings", settings={setting: "whatever"})
    )

    assert code == 1
    assert envelope["output"]["code"] == "SETTING_NOT_WRITABLE"
    assert setting in envelope["output"]["context"]["rejected"]
    assert stub.written is None  # nothing was written at all


def test_save_settings_rejects_invalid_values_without_writing(monkeypatch, stub, tmp_path):
    """An invalid rank key must not reach stored settings, or every later run aborts."""
    stub.settings = {"reportDir": str(tmp_path), "rankOrder": "codec"}

    code, envelope = run_main(
        monkeypatch, payload("ui_save_settings", settings={"rankOrder": "codec,bogus"})
    )

    assert code == 1
    assert envelope["output"]["code"] == "SETTING_UNKNOWN_RANK_KEY"
    assert stub.written is None
    assert stub.settings["rankOrder"] == "codec"  # untouched


def test_save_settings_rejects_a_non_object_payload(monkeypatch, stub, tmp_path):
    stub.settings = {"reportDir": str(tmp_path)}

    code, envelope = run_main(
        monkeypatch, payload("ui_save_settings", settings=["rankOrder"])
    )

    assert code == 1
    assert envelope["output"]["code"] == "SETTINGS_NOT_AN_OBJECT"
    assert stub.written is None


def test_save_settings_with_no_settings_key_is_rejected(monkeypatch, stub, tmp_path):
    stub.settings = {"reportDir": str(tmp_path)}

    code, envelope = run_main(monkeypatch, payload("ui_save_settings"))

    assert code == 1
    assert envelope["output"]["code"] == "SETTINGS_NOT_AN_OBJECT"


def test_save_settings_is_not_a_read_only_mode():
    assert "ui_save_settings" not in main.READ_ONLY_MODES


def test_saveable_settings_excludes_every_safety_control():
    forbidden = {"confirmDestructive", "maxDeletionsPerRun", "maxFractionOfLibrary",
                 "protectedPaths", "reportDir", "durationTolerance",
                 "qualityFloorRatio", "qualityFloorRatioCrossCodec"}

    assert not forbidden & set(main.SAVEABLE_SETTINGS)


def test_ui_config_exposes_the_option_schema(monkeypatch, stub, tmp_path):
    """The page renders its pickers from this, so it must never be empty."""
    stub.settings = {"reportDir": str(tmp_path)}

    code, envelope = run_main(monkeypatch, payload("ui_config"))

    schema = envelope["output"]["schema"]
    assert "codec" in schema["validRankKeys"]
    assert "file_id" not in schema["validRankKeys"]  # implicit, not operator-selectable
    assert schema["validMetadataPolicies"] == ["merge", "skip", "ignore"]
    assert schema["validTieBreakers"][0] == "skip"
    assert schema["knownCodecs"][:3] == ["av1", "hevc", "h264"]
    assert "rankOrder" in schema["orderedListSettings"]
    assert schema["enumSettings"]["metadataPolicy"] == ["merge", "skip", "ignore"]
    assert schema["defaults"]["rankOrder"]


def test_ui_config_schema_matches_what_the_saver_accepts(monkeypatch, stub, tmp_path):
    """Every ordered-list and enum setting the page edits must be writable."""
    stub.settings = {"reportDir": str(tmp_path)}
    _, envelope = run_main(monkeypatch, payload("ui_config"))
    schema = envelope["output"]["schema"]

    editable = set(schema["orderedListSettings"]) | set(schema["enumSettings"])

    assert editable == set(main.SAVEABLE_SETTINGS)
