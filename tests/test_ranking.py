"""Ranking behaviour, including the case that motivated the whole plugin."""

import pytest
from conftest import make_candidate

from config_schema import parse_config
from ranking import (
    deciding_key,
    effective_rank_order,
    rank_group,
    ranks_strictly_better_codec,
)


def test_rank_group_codec_first_keeps_1080p_hevc_over_4k_h264(config):
    """The motivating case: Unmanic's HEVC 1080p output must beat the 4K H.264 source."""
    hevc_1080 = make_candidate("1", "10", codec="hevc", width=1920, height=1080,
                               size=2_000_000_000, bit_rate=4_000_000)
    h264_4k = make_candidate("2", "20", codec="h264", width=3840, height=2160,
                             size=8_000_000_000, bit_rate=20_000_000)

    result = rank_group([h264_4k, hevc_1080], config)

    assert result.keeper is hevc_1080
    assert result.deciding == "codec"


def test_rank_group_resolution_first_keeps_4k_h264():
    """The inverse policy must produce the inverse result."""
    cfg = parse_config({"rankOrder": "resolution,codec,bitrate,size"})
    hevc_1080 = make_candidate("1", "10", codec="hevc", width=1920, height=1080)
    h264_4k = make_candidate("2", "20", codec="h264", width=3840, height=2160)

    result = rank_group([hevc_1080, h264_4k], cfg)

    assert result.keeper is h264_4k
    assert result.deciding == "resolution"


@pytest.mark.parametrize("alias", ["h265", "x265", "H.265", "HEVC", "hvc1"])
def test_rank_group_normalises_codec_aliases(config, alias):
    aliased = make_candidate("1", "10", codec=alias)
    h264 = make_candidate("2", "20", codec="h264")

    assert rank_group([h264, aliased], config).keeper is aliased


def test_rank_group_unknown_codec_sorts_last(config):
    known = make_candidate("1", "10", codec="h264")
    unknown = make_candidate("2", "20", codec="realvideo4")

    result = rank_group([unknown, known], config)

    assert result.keeper is known


def test_rank_group_is_total_under_shuffling(config, rng):
    """Ranking must not depend on the order Stash returned the group in."""
    candidates = [
        make_candidate(str(i), str(100 + i), codec="hevc", width=1920, height=1080,
                       size=1_000_000_000, bit_rate=4_000_000)
        for i in range(1, 8)
    ]
    cfg = parse_config({"tieBreaker": "smallest"})

    keepers = set()
    for _ in range(20):
        shuffled = candidates[:]
        rng.shuffle(shuffled)
        keepers.add(rank_group(shuffled, cfg).keeper.file.id)

    assert len(keepers) == 1


def test_effective_rank_order_always_ends_with_file_id(config):
    assert effective_rank_order(config)[-1] == "file_id"

    cfg = parse_config({"rankOrder": "file_id,codec"})
    order = effective_rank_order(cfg)
    assert order[-1] == "file_id"
    assert order.count("file_id") == 1


@pytest.mark.parametrize(
    "key,better,worse",
    [
        ("resolution", {"width": 3840, "height": 2160}, {"width": 1920, "height": 1080}),
        ("resolution_asc", {"width": 1920, "height": 1080}, {"width": 3840, "height": 2160}),
        ("bitrate", {"bit_rate": 9_000_000}, {"bit_rate": 3_000_000}),
        ("bitrate_asc", {"bit_rate": 3_000_000}, {"bit_rate": 9_000_000}),
        ("size", {"size": 9_000_000}, {"size": 3_000_000}),
        ("size_asc", {"size": 3_000_000}, {"size": 9_000_000}),
        ("framerate", {"frame_rate": 60.0}, {"frame_rate": 24.0}),
        ("duration", {"duration": 4000.0}, {"duration": 3000.0}),
        ("age", {"mod_time": 1_000.0}, {"mod_time": 2_000.0}),
        ("age_desc", {"mod_time": 2_000.0}, {"mod_time": 1_000.0}),
    ],
)
def test_each_rank_key_sorts_in_documented_direction(key, better, worse):
    cfg = parse_config({"rankOrder": key})
    winner = make_candidate("1", "10", **better)
    loser = make_candidate("2", "20", **worse)

    result = rank_group([loser, winner], cfg)

    assert result.keeper is winner
    assert result.deciding == key


def test_path_priority_prefers_earlier_prefix():
    cfg = parse_config({
        "rankOrder": "path_priority",
        "preferredPaths": "/media/keep/,/media/other/",
    })
    preferred = make_candidate("1", "10", path="/media/keep/a.mkv")
    other = make_candidate("2", "20", path="/media/other/b.mkv")

    assert rank_group([other, preferred], cfg).keeper is preferred


def test_organized_key_prefers_organized_scene():
    cfg = parse_config({"rankOrder": "organized"})
    plain = make_candidate("1", "10")
    organized = make_candidate("2", "20")
    organized = type(organized)(
        scene=type(organized.scene)(**{**organized.scene.__dict__, "organized": True}),
        file=organized.file,
    )

    assert rank_group([plain, organized], cfg).keeper is organized


def test_tie_with_skip_breaker_is_ambiguous(config):
    a = make_candidate("1", "10", codec="hevc")
    b = make_candidate("2", "20", codec="hevc")

    result = rank_group([a, b], config)

    assert result.is_ambiguous
    assert result.keeper is None
    assert len(result.tied) == 2
    assert result.deciding == "tie"


@pytest.mark.parametrize(
    "breaker,expected_id",
    [
        ("smallest", "2"),
        ("largest", "1"),
        ("oldest", "1"),
        ("newest", "2"),
    ],
)
def test_tie_breakers_pick_documented_candidate(breaker, expected_id):
    cfg = parse_config({"rankOrder": "codec", "tieBreaker": breaker})
    big_old = make_candidate("1", "10", codec="hevc", size=9_000_000, mod_time=1_000.0)
    small_new = make_candidate("2", "20", codec="hevc", size=3_000_000, mod_time=2_000.0)

    result = rank_group([big_old, small_new], cfg)

    assert result.keeper.file.id == expected_id
    assert result.deciding == f"tieBreaker:{breaker}"


def test_smallest_of_highest_resolution_ignores_lower_resolution_smaller_file():
    """The operator's requested breaker: pick the most efficient copy at the best size."""
    cfg = parse_config({"rankOrder": "codec", "tieBreaker": "smallest_of_highest_resolution"})
    tiny_720 = make_candidate("1", "10", codec="hevc", width=1280, height=720, size=500_000)
    small_1080 = make_candidate("2", "20", codec="hevc", width=1920, height=1080, size=2_000_000)
    big_1080 = make_candidate("3", "30", codec="hevc", width=1920, height=1080, size=5_000_000)

    result = rank_group([tiny_720, big_1080, small_1080], cfg)

    assert result.keeper is small_1080


def test_largest_of_highest_resolution():
    cfg = parse_config({"rankOrder": "codec", "tieBreaker": "largest_of_highest_resolution"})
    small_1080 = make_candidate("1", "10", codec="hevc", width=1920, height=1080, size=2_000_000)
    big_1080 = make_candidate("2", "20", codec="hevc", width=1920, height=1080, size=5_000_000)
    huge_720 = make_candidate("3", "30", codec="hevc", width=1280, height=720, size=9_000_000)

    assert rank_group([huge_720, small_1080, big_1080], cfg).keeper is big_1080


def test_single_candidate_group_has_no_deciding_key(config):
    only = make_candidate("1", "10")
    result = rank_group([only], config)

    assert result.keeper is only
    assert result.deciding == "none"


def test_empty_group_is_ambiguous_not_a_crash(config):
    result = rank_group([], config)

    assert result.keeper is None
    assert result.ordered == []


def test_deciding_key_reports_first_distinguishing_key(config):
    """Same codec and resolution, different bitrate: bitrate is the third key."""
    a = make_candidate("1", "10", codec="hevc", bit_rate=9_000_000)
    b = make_candidate("2", "20", codec="hevc", bit_rate=3_000_000)

    assert deciding_key(a, b, config) == "bitrate"


def test_ranks_strictly_better_codec(config):
    hevc = make_candidate("1", "10", codec="hevc")
    h264 = make_candidate("2", "20", codec="h264")

    assert ranks_strictly_better_codec(hevc, h264, config)
    assert not ranks_strictly_better_codec(h264, hevc, config)
    assert not ranks_strictly_better_codec(hevc, hevc, config)
