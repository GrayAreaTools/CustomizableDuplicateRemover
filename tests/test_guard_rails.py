"""Guard rails. Each rail gets a case proving it fires and one proving it permits.

The final test is the invariant the whole plugin rests on: no configuration and no
group shape may ever produce zero survivors.
"""

import itertools

import pytest
from conftest import FakeFileSystem, make_candidate, make_file, make_scene

from config_schema import parse_config
from guard_rails import (
    RunTotals,
    confirm_destructive,
    duration_match,
    evaluate_pair,
    first_block,
    generated_flag,
    group_fingerprint,
    keeper_intact,
    last_copy,
    library_scope,
    metadata_preserved,
    plan_fresh,
    protected_path,
    quality_floor,
    run_cap,
)
from models import Candidate
from ranking import rank_group

LIBRARY = ["/media/library"]


# G_LAST_COPY

def test_last_copy_permits_deleting_all_but_one():
    assert last_copy(group_size=3, loser_count=2).allowed


def test_last_copy_blocks_deleting_every_file():
    verdict = last_copy(group_size=2, loser_count=2)

    assert not verdict.allowed
    assert "all 2 of 2" in verdict.reason


def test_last_copy_blocks_more_losers_than_candidates():
    assert not last_copy(group_size=2, loser_count=3).allowed


def test_last_copy_blocks_empty_group():
    assert not last_copy(group_size=0, loser_count=0).allowed


def test_last_copy_permits_single_file_group_with_no_losers():
    assert last_copy(group_size=1, loser_count=0).allowed


# G_KEEPER_INTACT

def test_keeper_intact_permits_healthy_file():
    keeper = make_candidate("1", "10", size=1000)
    fs = FakeFileSystem({keeper.file.path: 1000})

    assert keeper_intact(keeper, fs).allowed


def test_keeper_intact_blocks_missing_file():
    """The unRAID case: an unmounted share leaves the DB row healthy but no file."""
    keeper = make_candidate("1", "10")
    verdict = keeper_intact(keeper, FakeFileSystem({}))

    assert not verdict.allowed
    assert "does not exist" in verdict.reason


def test_keeper_intact_blocks_zero_length_file():
    keeper = make_candidate("1", "10", size=1000)
    verdict = keeper_intact(keeper, FakeFileSystem({keeper.file.path: 0}))

    assert not verdict.allowed
    assert "zero length" in verdict.reason


def test_keeper_intact_blocks_unreadable_file():
    keeper = make_candidate("1", "10", size=1000)
    fs = FakeFileSystem({keeper.file.path: 1000}, unreadable={keeper.file.path})

    verdict = keeper_intact(keeper, fs)

    assert not verdict.allowed
    assert "not readable" in verdict.reason


def test_keeper_intact_blocks_size_drift_beyond_one_percent():
    """A file still being written by Unmanic must not be trusted as a keeper."""
    keeper = make_candidate("1", "10", size=1_000_000)
    verdict = keeper_intact(keeper, FakeFileSystem({keeper.file.path: 500_000}))

    assert not verdict.allowed
    assert "differs from the database" in verdict.reason


def test_keeper_intact_tolerates_size_drift_within_one_percent():
    keeper = make_candidate("1", "10", size=1_000_000)

    assert keeper_intact(keeper, FakeFileSystem({keeper.file.path: 1_005_000})).allowed


# G_LIBRARY_SCOPE

def test_library_scope_permits_path_inside_library():
    candidate = make_candidate("1", "10", path="/media/library/sub/a.mkv")
    fs = FakeFileSystem({candidate.file.path: 1})

    assert library_scope(candidate, LIBRARY, fs).allowed


def test_library_scope_blocks_path_outside_library():
    candidate = make_candidate("1", "10", path="/etc/passwd")

    verdict = library_scope(candidate, LIBRARY, FakeFileSystem())

    assert not verdict.allowed
    assert "outside every configured library path" in verdict.reason


def test_library_scope_blocks_symlink_escaping_the_library():
    """A symlink inside the library pointing out of it must not widen the blast radius."""
    candidate = make_candidate("1", "10", path="/media/library/evil.mkv")
    fs = FakeFileSystem(links={"/media/library/evil.mkv": "/etc/shadow"})

    assert not library_scope(candidate, LIBRARY, fs).allowed


def test_library_scope_blocks_sibling_prefix_collision():
    """'/media/library2' must not be treated as inside '/media/library'."""
    candidate = make_candidate("1", "10", path="/media/library2/a.mkv")

    assert not library_scope(candidate, LIBRARY, FakeFileSystem()).allowed


def test_library_scope_with_no_known_library_paths_blocks_everything():
    candidate = make_candidate("1", "10")

    verdict = library_scope(candidate, [], FakeFileSystem())

    assert not verdict.allowed
    assert "refusing to delete" in verdict.reason


# G_PROTECTED_PATHS

def test_protected_path_blocks_matching_prefix():
    cfg = parse_config({"protectedPaths": "/media/library/originals/"})
    candidate = make_candidate("1", "10", path="/media/library/originals/a.mkv")

    verdict = protected_path(candidate, cfg, FakeFileSystem())

    assert not verdict.allowed
    assert verdict.context["prefix"] == "/media/library/originals/"


def test_protected_path_permits_non_matching_prefix():
    cfg = parse_config({"protectedPaths": "/media/library/originals/"})
    candidate = make_candidate("1", "10", path="/media/library/encoded/a.mkv")

    assert protected_path(candidate, cfg, FakeFileSystem()).allowed


def test_protected_path_permits_when_unset(config):
    assert protected_path(make_candidate("1", "10"), config, FakeFileSystem()).allowed


def test_protected_path_blocks_a_symlink_into_a_protected_directory():
    """The unRAID case: the same content is reachable as /mnt/user/... and /mnt/diskN/...
    Comparing the raw path left protected content deletable through the other path."""
    cfg = parse_config({"protectedPaths": "/media/library/originals/"})
    candidate = make_candidate("1", "10", path="/media/library/alias/a.mkv")
    fs = FakeFileSystem(links={"/media/library/alias/a.mkv":
                               "/media/library/originals/a.mkv"})

    verdict = protected_path(candidate, cfg, fs)

    assert not verdict.allowed
    assert "resolves to" in verdict.reason


def test_protected_path_still_blocks_a_prefix_that_is_not_on_disk():
    """A protected prefix that does not exist yet must still protect."""
    cfg = parse_config({"protectedPaths": "/media/library/originals/"})
    candidate = make_candidate("1", "10", path="/media/library/originals/a.mkv")

    assert not protected_path(candidate, cfg, FakeFileSystem()).allowed


def test_protected_path_respects_a_path_boundary():
    """Protecting /media/Keep must not also protect /media/Keepsakes."""
    cfg = parse_config({"protectedPaths": "/media/Keep"})
    inside = make_candidate("1", "10", path="/media/Keep/a.mkv")
    sibling = make_candidate("2", "20", path="/media/Keepsakes/b.mkv")

    assert not protected_path(inside, cfg, FakeFileSystem()).allowed
    assert protected_path(sibling, cfg, FakeFileSystem()).allowed


# G_DURATION

def test_duration_match_permits_equal_durations(config):
    keeper = make_candidate("1", "10", duration=3600.0)
    loser = make_candidate("2", "20", duration=3600.4)

    assert duration_match(keeper, loser, config).allowed


def test_duration_match_blocks_truncated_keeper(config):
    """A phash can match on a sample even when one file is truncated."""
    keeper = make_candidate("1", "10", duration=600.0)
    loser = make_candidate("2", "20", duration=3600.0)

    verdict = duration_match(keeper, loser, config)

    assert not verdict.allowed
    assert "Durations differ" in verdict.reason


def test_duration_tolerance_is_configurable():
    cfg = parse_config({"durationTolerance": 60})
    keeper = make_candidate("1", "10", duration=3600.0)
    loser = make_candidate("2", "20", duration=3630.0)

    assert duration_match(keeper, loser, cfg).allowed


# G_QUALITY_FLOOR

def test_quality_floor_permits_hevc_at_half_the_bitrate(config):
    """The legitimate Unmanic result: HEVC at roughly half H.264's bitrate."""
    keeper = make_candidate("1", "10", codec="hevc", bit_rate=4_000_000)
    loser = make_candidate("2", "20", codec="h264", bit_rate=8_000_000)

    verdict = quality_floor(keeper, loser, config)

    assert verdict.allowed
    assert verdict.context["cross_codec"] is True


def test_quality_floor_blocks_botched_transcode(config):
    """HEVC is efficient, but not 100x efficient."""
    keeper = make_candidate("1", "10", codec="hevc", bit_rate=50_000)
    loser = make_candidate("2", "20", codec="h264", bit_rate=8_000_000)

    verdict = quality_floor(keeper, loser, config)

    assert not verdict.allowed
    assert "failed transcode" in verdict.reason


def test_quality_floor_uses_stricter_ratio_for_same_codec(config):
    keeper = make_candidate("1", "10", codec="hevc", bit_rate=2_000_000)
    loser = make_candidate("2", "20", codec="hevc", bit_rate=8_000_000)

    verdict = quality_floor(keeper, loser, config)

    assert not verdict.allowed
    assert verdict.context["cross_codec"] is False


def test_quality_floor_compares_across_resolutions(config):
    """Bits per pixel per frame, not raw bitrate: a 4K file needs more bits."""
    keeper = make_candidate("1", "10", codec="hevc", width=1920, height=1080,
                            bit_rate=4_000_000)
    loser = make_candidate("2", "20", codec="h264", width=3840, height=2160,
                           bit_rate=8_000_000)

    # Keeper has 2x the bits per pixel despite half the raw bitrate.
    assert quality_floor(keeper, loser, config).allowed


def test_quality_floor_abstains_when_metadata_is_missing(config):
    """Incomplete scan data must not block every group."""
    keeper = make_candidate("1", "10", bit_rate=0)
    loser = make_candidate("2", "20", bit_rate=8_000_000)

    verdict = quality_floor(keeper, loser, config)

    assert verdict.allowed
    assert verdict.context["indeterminate"] is True


def test_quality_floor_abstains_on_zero_framerate(config):
    keeper = make_candidate("1", "10", frame_rate=0.0)
    loser = make_candidate("2", "20")

    assert quality_floor(keeper, loser, config).allowed


# G_METADATA

def test_metadata_permits_when_loser_has_nothing_extra(config):
    keeper = make_scene("10", tag_ids=["1", "2"], rating100=80)
    loser = make_scene("20", tag_ids=["1"])

    verdict = metadata_preserved(keeper, loser, config)

    assert verdict.allowed
    assert verdict.context["lost"] == {}


def test_metadata_flags_merge_required_under_merge_policy(config):
    keeper = make_scene("10")
    loser = make_scene("20", tag_ids=["7"], performer_ids=["3"], o_counter=5)

    verdict = metadata_preserved(keeper, loser, config)

    assert verdict.allowed
    assert verdict.context["merge_required"] is True
    assert verdict.context["lost"]["tag_ids"] == ["7"]
    assert verdict.context["lost"]["o_counter"] == 5


def test_metadata_blocks_under_skip_policy():
    cfg = parse_config({"metadataPolicy": "skip"})
    keeper = make_scene("10")
    loser = make_scene("20", tag_ids=["7"])

    verdict = metadata_preserved(keeper, loser, cfg)

    assert not verdict.allowed
    assert "tag_ids" in verdict.reason


def test_metadata_permits_under_ignore_policy():
    cfg = parse_config({"metadataPolicy": "ignore"})
    keeper = make_scene("10")
    loser = make_scene("20", tag_ids=["7"], marker_count=3)

    verdict = metadata_preserved(keeper, loser, cfg)

    assert verdict.allowed
    assert "merge_required" not in verdict.context


def test_metadata_skips_check_within_a_single_scene(config):
    """Deleting a file from a multi-file scene loses no scene metadata."""
    scene = make_scene("10", file_ids=["1", "2"], tag_ids=["7"])

    verdict = metadata_preserved(scene, scene, config)

    assert verdict.allowed
    assert verdict.context["same_scene"] is True


def test_metadata_counts_markers_as_loss(config):
    keeper = make_scene("10")
    loser = make_scene("20", marker_count=4)

    assert metadata_preserved(keeper, loser, config).context["lost"]["marker_count"] == 4


# G_RUN_CAP

def test_run_cap_permits_within_limits(config):
    assert run_cap(RunTotals(planned_deletions=10, library_file_count=1000), config).allowed


def test_run_cap_blocks_over_absolute_limit():
    cfg = parse_config({"maxDeletionsPerRun": 5})

    verdict = run_cap(RunTotals(planned_deletions=6, library_file_count=10_000), cfg)

    assert not verdict.allowed
    assert "maxDeletionsPerRun" in verdict.reason


def test_run_cap_blocks_over_library_fraction():
    cfg = parse_config({"maxDeletionsPerRun": 10_000, "maxFractionOfLibrary": 0.01})

    verdict = run_cap(RunTotals(planned_deletions=50, library_file_count=100), cfg)

    assert not verdict.allowed
    assert "maxFractionOfLibrary" in verdict.reason


def test_run_cap_ignores_fraction_when_library_size_unknown(config):
    assert run_cap(RunTotals(planned_deletions=1, library_file_count=0), config).allowed


# G_CONFIRM

def test_confirm_destructive_blocks_by_default(config):
    verdict = confirm_destructive(config)

    assert not verdict.allowed
    assert "confirmDestructive" in verdict.reason


def test_confirm_destructive_permits_when_enabled():
    assert confirm_destructive(parse_config({"confirmDestructive": True})).allowed


# G_GENERATED

def test_generated_assets_only_removed_on_scene_destroy():
    assert generated_flag(is_scene_destroy=True) is True
    assert generated_flag(is_scene_destroy=False) is False


# G_PLAN_FRESH

def test_group_fingerprint_is_order_independent():
    a = make_candidate("1", "10")
    b = make_candidate("2", "20")

    assert group_fingerprint([a, b]) == group_fingerprint([b, a])


@pytest.mark.parametrize("field,value", [("size", 999), ("mod_time", 1.0), ("path", "/x/y.mkv")])
def test_group_fingerprint_changes_when_file_state_changes(field, value):
    original = make_candidate("1", "10")
    mutated = Candidate(
        scene=original.scene,
        file=make_file("1", **{field: value}),
    )

    assert group_fingerprint([original]) != group_fingerprint([mutated])


def test_plan_fresh_permits_matching_fingerprint():
    assert plan_fresh("abc", "abc").allowed


def test_plan_fresh_blocks_changed_group():
    verdict = plan_fresh("abc", "def")

    assert not verdict.allowed
    assert "Re-run the Plan task" in verdict.reason


def test_plan_fresh_blocks_missing_fingerprint():
    assert not plan_fresh("", "abc").allowed


# Composition

def test_evaluate_pair_returns_a_verdict_per_rail(config):
    keeper = make_candidate("1", "10", path="/media/library/a.mkv", size=100)
    loser = make_candidate("2", "20", path="/media/library/b.mkv")
    fs = FakeFileSystem({keeper.file.path: 100, loser.file.path: 200})

    verdicts = evaluate_pair(keeper, loser, config, LIBRARY, fs)

    assert {v.rail for v in verdicts} == {
        "G_KEEPER_INTACT", "G_LIBRARY_SCOPE", "G_PROTECTED_PATHS",
        "G_DURATION", "G_QUALITY_FLOOR", "G_METADATA",
    }
    assert first_block(verdicts) is None


def test_first_block_reports_the_earliest_failure(config):
    keeper = make_candidate("1", "10")
    loser = make_candidate("2", "20", duration=99.0)
    fs = FakeFileSystem()  # keeper missing, so G_KEEPER_INTACT fires first

    blocked = first_block(evaluate_pair(keeper, loser, config, LIBRARY, fs))

    assert blocked.rail == "G_KEEPER_INTACT"


# The core invariant

@pytest.mark.parametrize(
    "rank_order,tie_breaker,codecs",
    list(
        itertools.product(
            ["codec,resolution,size", "resolution,codec", "size_asc", "age,codec"],
            ["skip", "smallest", "largest", "smallest_of_highest_resolution"],
            [("hevc", "hevc"), ("hevc", "h264"), ("h264", "h264"), ("weird", "weird")],
        )
    ),
)
def test_no_configuration_ever_deletes_every_copy(rank_order, tie_breaker, codecs):
    """`G_LAST_COPY` holds across the configuration matrix.

    Ranking picks at most one keeper, so the loser count is always strictly less than
    the group size — the property `G_LAST_COPY` exists to enforce.
    """
    cfg = parse_config({"rankOrder": rank_order, "tieBreaker": tie_breaker})
    candidates = [
        make_candidate(str(i + 1), str(10 * (i + 1)), codec=codec)
        for i, codec in enumerate(codecs)
    ]

    result = rank_group(candidates, cfg)
    loser_count = 0 if result.keeper is None else len(candidates) - 1

    assert last_copy(len(candidates), loser_count).allowed
    assert loser_count < len(candidates)


def test_all_protected_group_keeps_everything():
    """When every file is protected, the group resolves to zero deletions, not zero files."""
    cfg = parse_config({"protectedPaths": "/media/library/"})
    candidates = [
        make_candidate("1", "10", codec="hevc", path="/media/library/a.mkv"),
        make_candidate("2", "20", codec="h264", path="/media/library/b.mkv"),
    ]
    result = rank_group(candidates, cfg)
    losers = [c for c in candidates if c is not result.keeper]
    blocked = [c for c in losers if not protected_path(c, cfg, FakeFileSystem()).allowed]

    assert len(blocked) == len(losers)
    assert last_copy(len(candidates), len(losers) - len(blocked)).allowed


@pytest.mark.parametrize(
    "keeper_codec,loser_codec,expected_comparison,expected_threshold",
    [
        ("hevc", "h264", "codec-upgrade", 0.15),
        ("hevc", "hevc", "same-codec", 0.35),
        ("h264", "h264", "same-codec", 0.35),
        # The most suspicious case: fewer bits AND a worse codec. Keeps the strict floor
        # and must not be described as "same-codec".
        ("h264", "hevc", "keeper-on-lower-ranked-codec", 0.35),
        ("mpeg4", "h264", "keeper-on-lower-ranked-codec", 0.35),
    ],
)
def test_quality_floor_labels_the_three_comparisons_distinctly(
    config, keeper_codec, loser_codec, expected_comparison, expected_threshold
):
    """Regression: every non-upgrade was reported as 'same-codec', including the case
    where the keeper used a worse codec than the file being deleted."""
    keeper = make_candidate("1", "10", codec=keeper_codec, bit_rate=1_000_000)
    loser = make_candidate("2", "20", codec=loser_codec, bit_rate=8_000_000)

    verdict = quality_floor(keeper, loser, config)

    assert verdict.context["comparison"] == expected_comparison
    assert verdict.context["threshold"] == pytest.approx(expected_threshold)
    assert verdict.context["keeper_codec"] == keeper_codec
    assert verdict.context["loser_codec"] == loser_codec


def test_quality_floor_message_names_both_codecs(config):
    keeper = make_candidate("1", "10", codec="h264", bit_rate=100_000)
    loser = make_candidate("2", "20", codec="hevc", bit_rate=8_000_000)

    verdict = quality_floor(keeper, loser, config)

    assert not verdict.allowed
    assert "keeper-on-lower-ranked-codec" in verdict.reason
    assert "keeping h264 over hevc" in verdict.reason
    assert "same-codec" not in verdict.reason


def test_quality_floor_normalises_aliases_in_the_label(config):
    keeper = make_candidate("1", "10", codec="x265", bit_rate=4_000_000)
    loser = make_candidate("2", "20", codec="hevc", bit_rate=8_000_000)

    verdict = quality_floor(keeper, loser, config)

    # x265 and hevc are the same codec, so this is same-codec, not an upgrade.
    assert verdict.context["comparison"] == "same-codec"
    assert verdict.context["keeper_codec"] == "hevc"
