"""Plan serialisation. A misread plan means deleting the wrong files."""

import json
import os

import pytest
from conftest import FakeFileSystem, make_file, make_scene

from config_schema import parse_config
from models import Candidate, GroupStatus, ParseError
from plan_io import (
    PLAN_FORMAT_VERSION,
    latest_plan_path,
    list_plan_runs,
    prune_plans,
    plan_from_dict,
    plan_to_dict,
    read_plan,
    write_plan,
)
from planner import build_plan

LIBRARY = ["/media/library"]


def sample_plan(config, run_id="run1"):
    keeper = Candidate(
        scene=make_scene("10", file_ids=["1"], tag_ids=["5"], rating100=80),
        file=make_file("1", path="/media/library/a.mkv", codec="hevc", bit_rate=4_000_000),
    )
    loser = Candidate(
        scene=make_scene("20", file_ids=["2"], o_counter=3),
        file=make_file("2", path="/media/library/b.mkv", codec="h264", bit_rate=8_000_000),
    )
    fs = FakeFileSystem({keeper.file.path: keeper.file.size, loser.file.path: loser.file.size})
    return build_plan(
        run_id, [[keeper, loser]], config, LIBRARY, fs,
        library_file_count=500, stash_version="0.28.0",
    )


def test_round_trip_preserves_the_decision(config):
    plan = sample_plan(config)

    restored = plan_from_dict(plan_to_dict(plan))

    assert restored.run_id == plan.run_id
    assert restored.stash_version == "0.28.0"
    assert len(restored.groups) == 1
    original_group, restored_group = plan.groups[0], restored.groups[0]
    assert restored_group.status is original_group.status
    assert restored_group.deciding_key == original_group.deciding_key
    assert restored_group.fingerprint == original_group.fingerprint
    assert restored_group.keeper.candidate.file.id == original_group.keeper.candidate.file.id
    assert [e.candidate.file.id for e in restored_group.losers] == \
           [e.candidate.file.id for e in original_group.losers]
    assert restored_group.bytes_reclaimed == original_group.bytes_reclaimed


def test_round_trip_preserves_the_configuration(config):
    cfg = parse_config({
        "rankOrder": "codec,size_asc",
        "tieBreaker": "largest",
        "protectedPaths": "/media/keep/,/mnt/x/",
        "metadataPolicy": "ignore",
        "maxDeletionsPerRun": 7,
    })
    restored = plan_from_dict(plan_to_dict(sample_plan(cfg)))

    assert restored.config.rank_order == ("codec", "size_asc")
    assert restored.config.tie_breaker == "largest"
    assert restored.config.protected_paths == ("/media/keep/", "/mnt/x/")
    assert restored.config.metadata_policy == "ignore"
    assert restored.config.max_deletions_per_run == 7


def test_round_trip_preserves_verdicts(config):
    plan = sample_plan(config)
    restored = plan_from_dict(plan_to_dict(plan))

    verdicts = restored.groups[0].candidates[1].verdicts
    assert {v.rail for v in verdicts} == {
        "G_KEEPER_INTACT", "G_LIBRARY_SCOPE", "G_PROTECTED_PATHS",
        "G_DURATION", "G_QUALITY_FLOOR", "G_METADATA",
    }
    assert all(v.allowed for v in verdicts)


def test_serialised_plan_is_json_clean(config):
    """The UI page consumes this directly, so it must survive a JSON round trip."""
    payload = json.loads(json.dumps(plan_to_dict(sample_plan(config))))

    assert payload["formatVersion"] == PLAN_FORMAT_VERSION
    assert payload["groups"][0]["candidates"][0]["isKeeper"] is True
    assert payload["groups"][0]["candidates"][1]["action"] == "DESTROY_SCENE"
    assert payload["summary"]["filesToDelete"] == 1


def test_unknown_format_version_is_rejected(config):
    payload = plan_to_dict(sample_plan(config))
    payload["formatVersion"] = PLAN_FORMAT_VERSION + 99

    with pytest.raises(ParseError) as excinfo:
        plan_from_dict(payload)

    assert excinfo.value.code == "PLAN_FORMAT_UNSUPPORTED"


def test_missing_format_version_is_rejected(config):
    payload = plan_to_dict(sample_plan(config))
    del payload["formatVersion"]

    with pytest.raises(ParseError):
        plan_from_dict(payload)


def test_write_plan_is_atomic_and_leaves_no_temp_files(tmp_path, config):
    path = str(tmp_path / "plan-run1.json")

    write_plan(sample_plan(config), path)

    assert os.path.exists(path)
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".plan-")]
    assert leftovers == []
    assert read_plan(path).run_id == "run1"


def test_write_plan_creates_the_directory(tmp_path, config):
    path = str(tmp_path / "nested" / "deep" / "plan-run1.json")

    write_plan(sample_plan(config), path)

    assert os.path.exists(path)


def test_read_plan_rejects_malformed_json(tmp_path):
    path = tmp_path / "plan-bad.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ParseError) as excinfo:
        read_plan(str(path))

    assert excinfo.value.code == "PLAN_MALFORMED"


def test_read_plan_reports_a_missing_file(tmp_path):
    with pytest.raises(ParseError) as excinfo:
        read_plan(str(tmp_path / "nope.json"))

    assert excinfo.value.code == "PLAN_NOT_FOUND"


def test_latest_plan_path_picks_the_newest(tmp_path, config):
    first = str(tmp_path / "plan-run1.json")
    second = str(tmp_path / "plan-run2.json")
    write_plan(sample_plan(config, "run1"), first)
    write_plan(sample_plan(config, "run2"), second)
    os.utime(first, (1, 1))
    os.utime(second, (2, 2))

    assert latest_plan_path(str(tmp_path)) == second


def test_latest_plan_path_ignores_unrelated_files(tmp_path, config):
    write_plan(sample_plan(config), str(tmp_path / "plan-run1.json"))
    (tmp_path / "audit.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "plan-run1.html").write_text("<h1>x</h1>", encoding="utf-8")

    assert latest_plan_path(str(tmp_path)).endswith("plan-run1.json")


def test_latest_plan_path_errors_with_no_plans(tmp_path):
    with pytest.raises(ParseError) as excinfo:
        latest_plan_path(str(tmp_path))

    assert excinfo.value.code == "NO_PLAN_AVAILABLE"


def test_latest_plan_path_errors_on_missing_directory(tmp_path):
    with pytest.raises(ParseError) as excinfo:
        latest_plan_path(str(tmp_path / "absent"))

    assert excinfo.value.code == "REPORT_DIR_MISSING"


def test_ambiguous_group_round_trips_with_no_keeper(config):
    a = Candidate(scene=make_scene("10", file_ids=["1"]),
                  file=make_file("1", path="/media/library/a.mkv", codec="hevc"))
    b = Candidate(scene=make_scene("20", file_ids=["2"]),
                  file=make_file("2", path="/media/library/b.mkv", codec="hevc"))
    fs = FakeFileSystem({a.file.path: a.file.size, b.file.path: b.file.size})
    plan = build_plan("run1", [[a, b]], config, LIBRARY, fs)

    restored = plan_from_dict(plan_to_dict(plan))

    assert restored.groups[0].status is GroupStatus.AMBIGUOUS
    assert restored.groups[0].keeper is None
    assert restored.groups[0].losers == []


# -- retention ---------------------------------------------------------------

def _write_run(tmp_path, config, run_id):
    plan = sample_plan(config, run_id)
    write_plan(plan, str(tmp_path / f"plan-{run_id}.json"))
    (tmp_path / f"plan-{run_id}.html").write_text("<h1>x</h1>", encoding="utf-8")
    (tmp_path / f"plan-{run_id}.csv").write_text("a,b\n", encoding="utf-8")


def test_list_plan_runs_is_ordered_by_run_id(tmp_path, config):
    for run_id in ("20260731T120000Z", "20260731T100000Z", "20260731T110000Z"):
        _write_run(tmp_path, config, run_id)

    assert list_plan_runs(str(tmp_path)) == [
        "20260731T100000Z", "20260731T110000Z", "20260731T120000Z",
    ]


def test_list_plan_runs_ignores_mtime(tmp_path, config):
    """An executed plan is rewritten in place, so mtime no longer says when it was built."""
    _write_run(tmp_path, config, "20260731T100000Z")
    _write_run(tmp_path, config, "20260731T110000Z")
    os.utime(tmp_path / "plan-20260731T100000Z.json", (9_000_000, 9_000_000))

    assert list_plan_runs(str(tmp_path))[-1] == "20260731T110000Z"


def test_prune_keeps_the_most_recent(tmp_path, config):
    for run_id in ("20260731T100000Z", "20260731T110000Z", "20260731T120000Z"):
        _write_run(tmp_path, config, run_id)

    removed = prune_plans(str(tmp_path), keep=1)

    assert removed == ["20260731T100000Z", "20260731T110000Z"]
    assert list_plan_runs(str(tmp_path)) == ["20260731T120000Z"]


def test_prune_removes_every_artifact_of_a_run(tmp_path, config):
    _write_run(tmp_path, config, "20260731T100000Z")
    _write_run(tmp_path, config, "20260731T110000Z")

    prune_plans(str(tmp_path), keep=1)

    leftovers = sorted(n for n in os.listdir(tmp_path) if "100000" in n)
    assert leftovers == []


def test_prune_with_keep_zero_removes_all(tmp_path, config):
    _write_run(tmp_path, config, "20260731T100000Z")
    _write_run(tmp_path, config, "20260731T110000Z")

    prune_plans(str(tmp_path), keep=0)

    assert list_plan_runs(str(tmp_path)) == []


def test_prune_never_touches_the_audit_log(tmp_path, config):
    """The audit log is the record of what was deleted and must outlive the plans."""
    _write_run(tmp_path, config, "20260731T100000Z")
    audit = tmp_path / "audit.jsonl"
    audit.write_text('{"event":"operation"}\n', encoding="utf-8")

    prune_plans(str(tmp_path), keep=0)

    assert audit.exists()
    assert audit.read_text(encoding="utf-8") == '{"event":"operation"}\n'


def test_prune_is_a_no_op_when_under_the_limit(tmp_path, config):
    _write_run(tmp_path, config, "20260731T100000Z")

    assert prune_plans(str(tmp_path), keep=5) == []
    assert list_plan_runs(str(tmp_path)) == ["20260731T100000Z"]


def test_prune_tolerates_a_missing_directory(tmp_path):
    assert prune_plans(str(tmp_path / "absent"), keep=1) == []
    assert list_plan_runs(str(tmp_path / "absent")) == []


def test_outcome_round_trips(tmp_path, config):
    """An applied plan must still read as applied after a reload."""
    plan = sample_plan(config)
    entry = [e for e in plan.groups[0].candidates if not e.is_keeper][0]
    entry.outcome = "deleted"
    plan.groups[0].status = GroupStatus.EXECUTED

    restored = plan_from_dict(plan_to_dict(plan))

    assert restored.groups[0].status is GroupStatus.EXECUTED
    assert restored.groups[0].losers == []
    assert restored.groups[0].acted_on[0].outcome == "deleted"
