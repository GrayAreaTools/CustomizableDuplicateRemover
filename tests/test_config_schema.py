"""Settings validation. Every setting has a case covering its exception path."""

import pytest

from config_schema import MAX_SETTING_LENGTH, VALID_RANK_KEYS, normalise_codec, parse_config
from models import ParseError


def test_defaults_are_codec_first():
    cfg = parse_config({})

    assert cfg.rank_order == ("codec", "resolution", "bitrate", "size", "age")
    assert cfg.codec_preference[:3] == ("av1", "hevc", "h264")
    assert cfg.tie_breaker == "skip"
    assert cfg.confirm_destructive is False
    assert cfg.phash_distance == 0


def test_none_settings_yield_defaults():
    assert parse_config(None).rank_order == parse_config({}).rank_order


def test_unknown_rank_key_names_the_offending_token():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"rankOrder": "codec,resulution,size"})

    error = excinfo.value
    assert error.code == "SETTING_UNKNOWN_RANK_KEY"
    assert error.context["token"] == "resulution"
    assert "resulution" in error.message
    assert "codec" in error.message  # lists the valid keys


@pytest.mark.parametrize("key", VALID_RANK_KEYS)
def test_every_documented_rank_key_is_accepted(key):
    settings = {"rankOrder": key}
    if key == "path_priority":
        settings["preferredPaths"] = "/media/"

    assert parse_config(settings).rank_order == (key,)


def test_rank_order_tolerates_whitespace_and_case():
    cfg = parse_config({"rankOrder": "  CODEC , resolution ,  SIZE  "})

    assert cfg.rank_order == ("codec", "resolution", "size")


def test_empty_rank_order_is_rejected():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"rankOrder": " , , "})

    assert excinfo.value.code == "SETTING_EMPTY"


def test_duplicate_rank_key_is_rejected():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"rankOrder": "codec,size,codec"})

    assert excinfo.value.code == "SETTING_DUPLICATE_ENTRY"
    assert excinfo.value.context["token"] == "codec"


def test_codec_preference_rejects_aliases_of_the_same_codec():
    """'hevc,h265' would collapse to one codec and make ranking order-dependent."""
    with pytest.raises(ParseError) as excinfo:
        parse_config({"codecPreference": "hevc,h265,h264"})

    assert excinfo.value.code == "SETTING_DUPLICATE_ENTRY"


def test_path_priority_requires_preferred_paths():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"rankOrder": "path_priority,codec"})

    assert excinfo.value.code == "SETTING_DEPENDENCY"
    assert excinfo.value.context["setting"] == "preferredPaths"


def test_invalid_tie_breaker_lists_accepted_values():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"tieBreaker": "keep_the_good_one"})

    assert excinfo.value.code == "SETTING_INVALID_ENUM"
    assert "smallest_of_highest_resolution" in excinfo.value.message


def test_invalid_metadata_policy_is_rejected():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"metadataPolicy": "discard"})

    assert excinfo.value.code == "SETTING_INVALID_ENUM"


@pytest.mark.parametrize(
    "setting", ["phashDistance", "durationDiff", "qualityFloorRatio", "maxDeletionsPerRun"]
)
def test_numeric_settings_reject_non_numbers(setting):
    with pytest.raises(ParseError) as excinfo:
        parse_config({setting: "lots"})

    assert excinfo.value.code == "SETTING_NOT_A_NUMBER"
    assert excinfo.value.context["setting"] == setting


@pytest.mark.parametrize(
    "setting,value",
    [
        ("phashDistance", -1),
        ("phashDistance", 65),
        ("durationDiff", -0.5),
        ("qualityFloorRatio", -1),
        ("maxFractionOfLibrary", 1.5),
        ("maxDeletionsPerRun", -5),
    ],
)
def test_numeric_settings_reject_out_of_range(setting, value):
    with pytest.raises(ParseError) as excinfo:
        parse_config({setting: value})

    assert excinfo.value.code == "SETTING_OUT_OF_RANGE"


def test_phash_distance_rejects_fractional():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"phashDistance": 1.5})

    assert excinfo.value.code == "SETTING_NOT_INTEGRAL"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_numeric_settings_reject_non_finite(value):
    with pytest.raises(ParseError) as excinfo:
        parse_config({"durationDiff": value})

    assert excinfo.value.code in ("SETTING_NOT_FINITE", "SETTING_OUT_OF_RANGE")


def test_numeric_settings_accept_string_numbers_from_the_ui():
    """Stash NUMBER settings can arrive as strings depending on the client."""
    cfg = parse_config({"phashDistance": "4", "durationDiff": "2.5"})

    assert cfg.phash_distance == 4
    assert cfg.duration_diff == 2.5


@pytest.mark.parametrize(
    "raw,expected",
    [(True, True), (False, False), ("true", True), ("False", False), ("1", True), ("0", False)],
)
def test_boolean_settings_accept_documented_forms(raw, expected):
    assert parse_config({"confirmDestructive": raw}).confirm_destructive is expected


def test_boolean_setting_rejects_nonsense():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"confirmDestructive": "maybe"})

    assert excinfo.value.code == "SETTING_NOT_A_BOOLEAN"


def test_empty_string_falls_back_to_default():
    """Stash sends empty strings for unset STRING settings."""
    cfg = parse_config({"tieBreaker": "", "phashDistance": "", "confirmDestructive": ""})

    assert cfg.tie_breaker == "skip"
    assert cfg.phash_distance == 0
    assert cfg.confirm_destructive is False


def test_oversized_setting_is_rejected():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"protectedPaths": "/x/" * MAX_SETTING_LENGTH})

    assert excinfo.value.code == "SETTING_TOO_LONG"


def test_too_many_list_items_is_rejected():
    with pytest.raises(ParseError) as excinfo:
        parse_config({"protectedPaths": ",".join(f"/media/p{i}/" for i in range(200))})

    assert excinfo.value.code == "SETTING_LIST_TOO_LONG"


def test_protected_paths_preserve_case():
    """Filesystems may be case-sensitive, so path prefixes must not be lowercased."""
    cfg = parse_config({"protectedPaths": "/media/Originals/,/mnt/Keep/"})

    assert cfg.protected_paths == ("/media/Originals/", "/mnt/Keep/")


def test_report_dir_falls_back_to_supplied_default():
    cfg = parse_config({}, default_report_dir="/plugins/cdr/reports")

    assert cfg.report_dir == "/plugins/cdr/reports"


def test_as_dict_round_trips_through_json():
    import json

    cfg = parse_config({"rankOrder": "codec,size", "tieBreaker": "largest"})
    restored = json.loads(json.dumps(cfg.as_dict()))

    assert restored["rankOrder"] == ["codec", "size"]
    assert restored["tieBreaker"] == "largest"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("h265", "hevc"),
        ("X265", "hevc"),
        ("H.265", "hevc"),
        ("hvc1", "hevc"),
        ("avc1", "h264"),
        ("x264", "h264"),
        ("xvid", "mpeg4"),
        ("hevc", "hevc"),
        ("vp9", "vp9"),
        ("", ""),
        (None, ""),
        ("  HEVC  ", "hevc"),
    ],
)
def test_normalise_codec(raw, expected):
    assert normalise_codec(raw) == expected


def test_apply_tags_is_off_by_default():
    """Tagging updates scenes, firing every other plugin's Scene.Update.Post hooks, so
    it must be opt-in."""
    assert parse_config({}).apply_tags is False


def test_default_codec_preference_ranks_vc1_above_wmv3():
    """VC-1 Advanced Profile outranks WMV9; the original default had them inverted."""
    order = parse_config({}).codec_preference

    assert order.index("vc1") < order.index("wmv3")
    assert order.index("wmv3") < order.index("msmpeg4v3")
    assert order.index("h264") < order.index("vc1")
    assert order[:4] == ("av1", "hevc", "h264", "vp9")
