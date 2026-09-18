"""Planning: keeper choice, action selection, group classification, run caps."""

import pytest
from conftest import FakeFileSystem, make_file, make_scene

from config_schema import parse_config
from models import Action, Candidate, GroupStatus
from planner import build_plan, plan_group

LIBRARY = ["/media/library"]


def candidate(file_id, scene_id, *, scene_file_ids=None, **file_kwargs):
    """A candidate whose scene may declare more files than this one."""
    file = make_file(file_id, path=f"/media/library/f{file_id}.mkv", **file_kwargs)
    scene = make_scene(scene_id, file_ids=scene_file_ids or [file.id])
    return Candidate(scene=scene, file=file)


def fs_for(*candidates):
    return FakeFileSystem({c.file.path: c.file.size for c in candidates})


def test_resolves_hevc_over_h264_and_marks_scene_destroy(config):
    keeper = candidate("1", "10", codec="hevc", width=1920, height=1080,
                       bit_rate=4_000_000, size=2_000_000_000)
    loser = candidate("2", "20", codec="h264", width=3840, height=2160,
                      bit_rate=20_000_000, size=8_000_000_000)

    plan = plan_group(0, [keeper, loser], config, LIBRARY, fs_for(keeper, loser))

    assert plan.status is GroupStatus.RESOLVED
    assert plan.keeper.candidate is keeper
    assert plan.deciding_key == "codec"
    assert len(plan.losers) == 1
    # The loser is its scene's only file, and the keeper is a different scene.
    assert plan.losers[0].action is Action.DESTROY_SCENE
    assert plan.bytes_reclaimed == 8_000_000_000


def test_multi_file_scene_uses_file_deletion_not_scene_destruction(config):
    """Both files on one scene: deleting one must not destroy the scene."""
    keeper = candidate("1", "10", scene_file_ids=["1", "2"], codec="hevc")
    loser = candidate("2", "10", scene_file_ids=["1", "2"], codec="h264")
    # Same scene id, so rebuild both against one shared scene.
    scene = make_scene("10", file_ids=["1", "2"])
    keeper = Candidate(scene=scene, file=keeper.file)
    loser = Candidate(scene=scene, file=loser.file)

    plan = plan_group(0, [keeper, loser], config, LIBRARY, fs_for(keeper, loser))

    assert plan.status is GroupStatus.RESOLVED
    assert plan.losers[0].action is Action.DELETE_FILE


def test_loser_with_surviving_sibling_uses_file_deletion(config):
    """A loser whose scene holds another file Stash keeps must not destroy the scene."""
    keeper = candidate("1", "10", codec="hevc")
    loser = candidate("2", "20", scene_file_ids=["2", "3"], codec="h264")

    plan = plan_group(0, [keeper, loser], config, LIBRARY, fs_for(keeper, loser))

    assert plan.losers[0].action is Action.DELETE_FILE


def test_ambiguous_group_records_no_losers(config):
    a = candidate("1", "10", codec="hevc")
    b = candidate("2", "20", codec="hevc")

    plan = plan_group(0, [a, b], config, LIBRARY, fs_for(a, b))

    assert plan.status is GroupStatus.AMBIGUOUS
    assert plan.losers == []
    assert plan.keeper is None
    assert "tie" in plan.reason.lower()


def test_group_with_one_candidate_is_skipped(config):
    only = candidate("1", "10")

    plan = plan_group(0, [only], config, LIBRARY, fs_for(only))

    assert plan.status is GroupStatus.SKIPPED
    assert plan.losers == []


def test_protected_loser_marks_the_group_protected():
    cfg = parse_config({"protectedPaths": "/media/library/f2.mkv"})
    keeper = candidate("1", "10", codec="hevc")
    loser = candidate("2", "20", codec="h264")

    plan = plan_group(0, [keeper, loser], cfg, LIBRARY, fs_for(keeper, loser))

    assert plan.status is GroupStatus.PROTECTED
    assert plan.losers == []
    assert plan.bytes_reclaimed == 0


def test_missing_keeper_on_disk_blocks_all_losers(config):
    keeper = candidate("1", "10", codec="hevc")
    loser = candidate("2", "20", codec="h264")
    fs = FakeFileSystem({loser.file.path: loser.file.size})  # keeper absent

    plan = plan_group(0, [keeper, loser], config, LIBRARY, fs)

    # SKIPPED, not PROTECTED: nothing here is under a protected path, the keeper is
    # simply unusable. The reason must name the rail so the report explains itself.
    assert plan.status is GroupStatus.SKIPPED
    assert plan.losers == []
    assert "G_KEEPER_INTACT" in plan.reason
    assert "G_KEEPER_INTACT" in [v.rail for v in plan.candidates[1].blocked_by]


def test_truncated_loser_is_blocked_not_deleted(config):
    keeper = candidate("1", "10", codec="hevc", duration=3600.0)
    loser = candidate("2", "20", codec="h264", duration=120.0)

    plan = plan_group(0, [keeper, loser], config, LIBRARY, fs_for(keeper, loser))

    assert plan.losers == []
    assert "G_DURATION" in [v.rail for v in plan.candidates[1].blocked_by]


def test_a_group_never_plans_to_delete_every_candidate(config):
    """The invariant, checked through the planner rather than the rail directly."""
    candidates = [
        candidate(str(i), str(10 * i), codec="hevc" if i == 1 else "h264")
        for i in range(1, 5)
    ]

    plan = plan_group(0, candidates, config, LIBRARY, fs_for(*candidates))

    assert len(plan.losers) == len(candidates) - 1
    assert plan.keeper is not None


def test_plan_group_records_a_verdict_for_every_loser(config):
    keeper = candidate("1", "10", codec="hevc")
    loser = candidate("2", "20", codec="h264")

    plan = plan_group(0, [keeper, loser], config, LIBRARY, fs_for(keeper, loser))

    assert plan.candidates[0].verdicts == []  # keeper is not evaluated against itself
    assert len(plan.candidates[1].verdicts) == 6


def test_build_plan_aggregates_and_evaluates_the_cap(config):
    groups = []
    for i in range(3):
        keeper = candidate(f"{i}a", f"{i}0", codec="hevc")
        loser = candidate(f"{i}b", f"{i}1", codec="h264")
        groups.append([keeper, loser])
    all_candidates = [c for group in groups for c in group]

    plan = build_plan(
        "run1", groups, config, LIBRARY, fs_for(*all_candidates),
        library_file_count=100, stash_version="0.28.0",
    )

    assert len(plan.groups) == 3
    assert plan.total_deletions == 3
    assert plan.is_executable
    assert plan.counts_by_status()["RESOLVED"] == 3
    assert plan.summary()["stashVersion"] == "0.28.0"


def test_build_plan_blocks_when_over_the_deletion_cap():
    cfg = parse_config({"maxDeletionsPerRun": 1})
    groups = []
    for i in range(3):
        keeper = candidate(f"{i}a", f"{i}0", codec="hevc")
        loser = candidate(f"{i}b", f"{i}1", codec="h264")
        groups.append([keeper, loser])
    all_candidates = [c for group in groups for c in group]

    plan = build_plan("run1", groups, cfg, LIBRARY, fs_for(*all_candidates),
                      library_file_count=100)

    assert not plan.is_executable
    assert "maxDeletionsPerRun" in plan.cap_verdict.reason


def test_build_plan_blocks_when_over_the_library_fraction():
    cfg = parse_config({"maxFractionOfLibrary": 0.001})
    keeper = candidate("1", "10", codec="hevc")
    loser = candidate("2", "20", codec="h264")

    plan = build_plan("run1", [[keeper, loser]], cfg, LIBRARY, fs_for(keeper, loser),
                      library_file_count=10)

    assert not plan.is_executable
    assert "maxFractionOfLibrary" in plan.cap_verdict.reason


def test_build_plan_reports_progress(config):
    seen = []
    groups = [[candidate("1", "10", codec="hevc"), candidate("2", "20", codec="h264")]]

    build_plan("run1", groups, config, LIBRARY, FakeFileSystem(), on_progress=seen.append)

    assert seen == [1.0]


def test_build_plan_survives_a_failing_group(config, monkeypatch):
    """One bad group must be reported, not abort the run."""
    import planner

    good = [candidate("1", "10", codec="hevc"), candidate("2", "20", codec="h264")]
    original = planner.rank_group
    calls = {"n": 0}

    def flaky(candidates, cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return original(candidates, cfg)

    monkeypatch.setattr(planner, "rank_group", flaky)

    plan = build_plan("run1", [good, good], config, LIBRARY, fs_for(*good))

    assert plan.groups[0].status is GroupStatus.FAILED
    assert "boom" in plan.groups[0].reason
    assert plan.groups[1].status is GroupStatus.RESOLVED


def test_every_group_carries_a_fingerprint(config):
    keeper = candidate("1", "10", codec="hevc")
    loser = candidate("2", "20", codec="h264")

    plan = build_plan("run1", [[keeper, loser]], config, LIBRARY, fs_for(keeper, loser))

    assert plan.groups[0].fingerprint
    assert len(plan.groups[0].fingerprint) == 64  # sha256 hex


@pytest.mark.parametrize("policy", ["merge", "ignore"])
def test_metadata_policies_that_allow_deletion(policy):
    cfg = parse_config({"metadataPolicy": policy})
    keeper_file = make_file("1", path="/media/library/f1.mkv", codec="hevc")
    loser_file = make_file("2", path="/media/library/f2.mkv", codec="h264")
    keeper = Candidate(scene=make_scene("10", file_ids=["1"]), file=keeper_file)
    loser = Candidate(
        scene=make_scene("20", file_ids=["2"], tag_ids=["99"], o_counter=3),
        file=loser_file,
    )

    plan = plan_group(0, [keeper, loser], cfg, LIBRARY, fs_for(keeper, loser))

    assert len(plan.losers) == 1


def test_metadata_skip_policy_blocks_the_loser():
    cfg = parse_config({"metadataPolicy": "skip"})
    keeper_file = make_file("1", path="/media/library/f1.mkv", codec="hevc")
    loser_file = make_file("2", path="/media/library/f2.mkv", codec="h264")
    keeper = Candidate(scene=make_scene("10", file_ids=["1"]), file=keeper_file)
    loser = Candidate(
        scene=make_scene("20", file_ids=["2"], tag_ids=["99"]), file=loser_file
    )

    plan = plan_group(0, [keeper, loser], cfg, LIBRARY, fs_for(keeper, loser))

    assert plan.losers == []
    assert plan.status is GroupStatus.SKIPPED
    assert "G_METADATA" in plan.reason


def test_multi_file_scene_losing_every_file_destroys_the_scene(config):
    """Regression: deciding each loser's action in isolation marked both files of a
    two-file scene DELETE_FILE, the executor then found no survivor to promote to primary
    and aborted, and because nothing changed on disk the fingerprint still matched — so
    every later run failed identically and the group was deadlocked forever."""
    scene_a = make_scene("1", file_ids=["11", "12"])
    losers = [
        Candidate(scene=scene_a, file=make_file("11", path="/media/library/f11.mkv",
                                                codec="h264")),
        Candidate(scene=scene_a, file=make_file("12", path="/media/library/f12.mkv",
                                                codec="h264")),
    ]
    keeper = candidate("21", "2", codec="hevc", bit_rate=4_000_000)
    everything = losers + [keeper]

    plan = plan_group(0, everything, config, LIBRARY, fs_for(*everything))

    assert plan.status is GroupStatus.RESOLVED
    assert plan.keeper.candidate is keeper
    actions = {e.candidate.file.id: e.action for e in plan.losers}
    assert actions == {"11": Action.DESTROY_SCENE, "12": Action.DESTROY_SCENE}


def test_a_blocked_sibling_keeps_the_scene_alive(config):
    """If one of a scene's files is blocked it survives, so the other is a file delete."""
    cfg = parse_config({"protectedPaths": "/media/library/f12.mkv"})
    scene_a = make_scene("1", file_ids=["11", "12"])
    losers = [
        Candidate(scene=scene_a, file=make_file("11", path="/media/library/f11.mkv",
                                                codec="h264")),
        Candidate(scene=scene_a, file=make_file("12", path="/media/library/f12.mkv",
                                                codec="h264")),
    ]
    keeper = candidate("21", "2", codec="hevc", bit_rate=4_000_000)
    everything = losers + [keeper]

    plan = plan_group(0, everything, cfg, LIBRARY, fs_for(*everything))

    actions = {e.candidate.file.id: e.action for e in plan.losers}
    assert actions == {"11": Action.DELETE_FILE}
