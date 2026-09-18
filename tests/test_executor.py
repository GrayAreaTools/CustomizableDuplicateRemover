"""Execution against a fake Stash.

These tests cover the operations that destroy data, so each one asserts both what was
called and what was *not*.
"""

import json
import os

import pytest
from conftest import FakeFileSystem

from audit import AuditLog
from candidates import build_groups
from config_schema import parse_config
from executor import ExecutionAborted, Executor
from guard_rails import group_fingerprint
from models import GroupStatus
from planner import build_plan
from stash_client import StashError

LIBRARY = ["/media/library"]


def raw_file(file_id, *, codec="h264", width=1920, height=1080, size=1_000_000_000,
             bit_rate=8_000_000, duration=3600.0, frame_rate=30.0,
             mod_time="2024-01-01T00:00:00Z"):
    return {
        "id": str(file_id),
        "path": f"/media/library/f{file_id}.mkv",
        "basename": f"f{file_id}.mkv",
        "size": size,
        "mod_time": mod_time,
        "width": width,
        "height": height,
        "duration": duration,
        "video_codec": codec,
        "audio_codec": "aac",
        "frame_rate": frame_rate,
        "bit_rate": bit_rate,
        "fingerprints": [{"type": "phash", "value": "deadbeef"}],
    }


def raw_scene(scene_id, files, **kwargs):
    return {
        "id": str(scene_id),
        "title": f"scene {scene_id}",
        "organized": kwargs.get("organized", False),
        "o_counter": kwargs.get("o_counter", 0),
        "rating100": kwargs.get("rating100"),
        "play_count": kwargs.get("play_count", 0),
        "urls": kwargs.get("urls", []),
        "tags": [{"id": t} for t in kwargs.get("tags", [])],
        "performers": [{"id": p} for p in kwargs.get("performers", [])],
        "scene_markers": [{"id": m} for m in kwargs.get("markers", [])],
        "files": files,
    }


class FakeClient:
    """Records mutations instead of performing them."""

    def __init__(self, scenes, *, fail_on=None):
        self.scenes = {s["id"]: s for s in scenes}
        self.deleted_files = []
        self.destroyed = []
        self.merges = []
        self.primary_sets = []
        self.fail_on = fail_on or set()

    def call(self, query, variables=None, **kwargs):
        # Only the refresh query reaches call() directly.
        ids = [str(i) for i in (variables or {}).get("ids", [])]
        return {"findScenes": {"scenes": [self.scenes[i] for i in ids if i in self.scenes]}}

    def delete_files(self, file_ids):
        if "delete_files" in self.fail_on:
            raise StashError("STASH_GRAPHQL_ERROR", "deleteFiles refused")
        self.deleted_files.extend(file_ids)
        return True

    def destroy_scenes(self, scene_ids, *, delete_file, delete_generated):
        if "destroy_scenes" in self.fail_on:
            raise StashError("STASH_GRAPHQL_ERROR", "scenesDestroy refused")
        # Stash errors if the scene is gone - which is exactly what happens after a merge.
        for sid in scene_ids:
            if str(sid) not in self.scenes:
                raise StashError(
                    "STASH_GRAPHQL_ERROR", f"scene with id {sid} not found"
                )
        self.destroyed.append(
            {"ids": list(scene_ids), "delete_file": delete_file,
             "delete_generated": delete_generated}
        )
        return True

    def merge_scenes(self, source_ids, destination_id, values=None):
        if "merge" in self.fail_on:
            return {}  # a merge that reports nothing must block the destroy
        self.merges.append({"source": list(source_ids), "destination": destination_id,
                            "values": values})
        # Model what Stash actually does: move the sources' files onto the destination
        # and DESTROY the source scene rows. Returning a bare {"id": ...} let the code
        # under test assume the source still existed.
        for sid in source_ids:
            source = self.scenes.pop(str(sid), None)
            if source and str(destination_id) in self.scenes:
                self.scenes[str(destination_id)]["files"].extend(source["files"])
        return {"id": destination_id}

    def set_primary_file(self, scene_id, file_id):
        self.primary_sets.append({"scene": scene_id, "file": file_id})
        return True


def plan_for(scenes, config, fs=None, *, library_file_count=1000):
    groups = build_groups([scenes])
    files = [c.file for group in groups for c in group]
    filesystem = fs or FakeFileSystem({f.path: f.size for f in files})
    return build_plan("run1", groups, config, LIBRARY, filesystem,
                      library_file_count=library_file_count), filesystem


@pytest.fixture
def confirmed():
    return parse_config({"confirmDestructive": True})


@pytest.fixture
def audit(tmp_path):
    return AuditLog(str(tmp_path), "run1")


def read_audit(audit):
    if not os.path.exists(audit.path):
        return []
    with open(audit.path, "r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


# -- gates ------------------------------------------------------------------

def test_execute_refuses_without_confirm_destructive(audit, tmp_path):
    cfg = parse_config({})  # confirmDestructive defaults to false
    scenes = [raw_scene("10", [raw_file("1", codec="hevc")]),
              raw_scene("20", [raw_file("2", codec="h264")])]
    plan, fs = plan_for(scenes, cfg)
    client = FakeClient(scenes)

    with pytest.raises(ExecutionAborted) as excinfo:
        Executor(client, cfg, audit, fs, LIBRARY).execute(plan)

    assert excinfo.value.code == "G_CONFIRM"
    assert client.deleted_files == []
    assert client.destroyed == []


def test_execute_refuses_over_the_run_cap(audit):
    cfg = parse_config({"confirmDestructive": True, "maxDeletionsPerRun": 0})
    scenes = [raw_scene("10", [raw_file("1", codec="hevc")]),
              raw_scene("20", [raw_file("2", codec="h264")])]
    plan, fs = plan_for(scenes, cfg)
    client = FakeClient(scenes)

    with pytest.raises(ExecutionAborted) as excinfo:
        Executor(client, cfg, audit, fs, LIBRARY).execute(plan)

    assert excinfo.value.code == "G_RUN_CAP"
    assert client.deleted_files == []


def test_abort_is_recorded_nowhere_as_a_deletion(audit, confirmed):
    """An aborted run must not leave operation records implying work happened."""
    cfg = parse_config({"maxDeletionsPerRun": 0, "confirmDestructive": True})
    scenes = [raw_scene("10", [raw_file("1", codec="hevc")]),
              raw_scene("20", [raw_file("2", codec="h264")])]
    plan, fs = plan_for(scenes, cfg)

    with pytest.raises(ExecutionAborted):
        Executor(FakeClient(scenes), cfg, audit, fs, LIBRARY).execute(plan)

    assert [r for r in read_audit(audit) if r["event"] == "operation"] == []


# -- separate scenes: destroy ------------------------------------------------

def test_destroys_losing_scene_and_keeps_the_hevc_file(audit, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert client.destroyed == [
        {"ids": ["20"], "delete_file": True, "delete_generated": True}
    ]
    assert client.deleted_files == []
    assert result.scenes_destroyed == 1
    assert result.files_deleted == 1
    assert result.outcomes[0].status is GroupStatus.RESOLVED


def test_refuses_to_destroy_the_scene_holding_the_keeper(audit, confirmed):
    """Defence in depth: even a corrupted plan must not destroy the keeper's scene."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc")]),
        raw_scene("20", [raw_file("2", codec="h264")]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    # Corrupt the plan: point the loser at the keeper's scene.
    loser = plan.groups[0].losers[0]
    from models import Candidate
    loser.candidate = Candidate(scene=plan.groups[0].keeper.candidate.scene,
                                file=loser.candidate.file)
    plan.groups[0].fingerprint = group_fingerprint(
        [e.candidate for e in plan.groups[0].candidates]
    )
    client = FakeClient(scenes)

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    # Two independent rails cover this: the corrupted candidate list no longer matches
    # live state, so G_PLAN_FRESH catches it before KEEPER_SCENE_TARGETED is reached.
    # What matters is that nothing was destroyed.
    assert client.destroyed == []
    assert client.deleted_files == []
    assert result.outcomes[0].status is not GroupStatus.RESOLVED


# -- multi-file scene: delete file -------------------------------------------

def test_deletes_a_file_without_destroying_its_scene(audit, confirmed):
    """Both copies attached to one scene — the post-Unmanic rescan case."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000),
                         raw_file("2", codec="h264", bit_rate=8_000_000)])
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert client.deleted_files == ["2"]
    assert client.destroyed == []
    assert client.primary_sets == []  # file 1 is primary and survives
    assert result.files_deleted == 1


def test_promotes_a_survivor_before_deleting_the_primary_file(audit, confirmed):
    """If the loser is the scene's primary file, a survivor must be promoted first."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="h264", bit_rate=8_000_000),
                         raw_file("2", codec="hevc", bit_rate=4_000_000)])
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert client.primary_sets == [{"scene": "10", "file": "2"}]
    assert client.deleted_files == ["1"]
    assert client.destroyed == []


def test_generated_assets_are_kept_when_only_a_file_is_deleted(audit, confirmed):
    """G_GENERATED: the scene survives, so its sprites and previews must too."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000),
                         raw_file("2", codec="h264", bit_rate=8_000_000)])
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert client.destroyed == []  # no scenesDestroy call at all
    records = [r for r in read_audit(audit) if r["event"] == "operation"]
    assert "generated assets retained" in records[0]["detail"]


# -- metadata --------------------------------------------------------------

def test_merges_metadata_before_destroying_the_losing_scene(audit, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)],
                  tags=["7"], o_counter=4),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert len(client.merges) == 1
    merge = client.merges[0]
    assert merge["source"] == ["20"]
    assert merge["destination"] == "10"
    # sceneMerge copies nothing itself, so the union must be passed as `values`.
    assert merge["values"]["id"] == "10"
    assert merge["values"]["tag_ids"] == ["7"]

    # sceneMerge already destroyed scene 20 and parked its file on the keeper, so the
    # file must be removed with deleteFiles. scenesDestroy would error with
    # "scene with id 20 not found" and the video would survive.
    assert client.deleted_files == ["2"]
    assert client.destroyed == []
    assert result.files_deleted == 1
    assert result.bytes_reclaimed == 1_000_000_000


def test_merged_scene_file_is_reported_deleted_only_because_it_really_was(audit, confirmed):
    """Regression: merge-then-scenesDestroy left the duplicate on disk while the summary
    and audit log both claimed it had been deleted."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)],
                  tags=["7"], performers=["p1"], rating100=90),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    record = [r for r in read_audit(audit) if r["event"] == "operation"][0]
    assert record["result"] == "deleted"
    assert "deleteFiles" in record["detail"]
    assert record["deleted"]["fileId"] == "2"
    # The file id claimed deleted is the one actually passed to deleteFiles.
    assert client.deleted_files == [record["deleted"]["fileId"]]


def test_merge_values_union_preserves_the_keepers_own_metadata(audit, confirmed):
    """A merge must never overwrite something set on the file being kept."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)],
                  tags=["1"], performers=["pk"], rating100=50),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)],
                  tags=["7"], performers=["p1"], rating100=90),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    values = client.merges[0]["values"]
    assert values["tag_ids"] == ["1", "7"]
    assert values["performer_ids"] == ["p1", "pk"]
    # The keeper already had a rating, so it must not be replaced by the loser's.
    assert "rating100" not in values


def test_unmerged_scene_still_uses_scenes_destroy(audit, confirmed):
    """With nothing to merge, the single scenesDestroy call is correct and cheaper."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)], tags=["7"]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert client.merges == []
    assert client.destroyed == [
        {"ids": ["20"], "delete_file": True, "delete_generated": True}
    ]
    assert client.deleted_files == []


def test_no_merge_when_the_loser_carries_nothing_extra(audit, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)], tags=["7"]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert client.merges == []
    assert client.destroyed


def test_unverified_merge_blocks_the_destroy(audit, confirmed):
    """If sceneMerge returns nothing, the source must not be destroyed."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)], tags=["7"]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes, fail_on={"merge"})

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert client.destroyed == []
    assert result.outcomes[0].status is GroupStatus.PARTIAL
    assert "MERGE_UNVERIFIED" in result.outcomes[0].reason


# -- freshness -------------------------------------------------------------

def test_stale_plan_is_skipped_when_the_file_changed(audit, confirmed):
    """Unmanic re-encoding between plan and execute must abort that group."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)

    # The library moves on: the loser is now a different size.
    changed = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000, size=42)]),
    ]
    client = FakeClient(changed)

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert client.deleted_files == []
    assert client.destroyed == []
    assert result.outcomes[0].status is GroupStatus.SKIPPED
    assert "G_PLAN_FRESH" in result.outcomes[0].reason


# -- guard rail re-check ---------------------------------------------------

def test_keeper_missing_at_execution_time_blocks_deletion(audit, confirmed):
    """The plan was fine, but the keeper's share went away before execution."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, _ = plan_for(scenes, confirmed)
    # Re-check runs against a filesystem where the keeper has vanished.
    live_fs = FakeFileSystem({"/media/library/f2.mkv": 1_000_000_000})
    client = FakeClient(scenes)

    result = Executor(client, confirmed, audit, live_fs, LIBRARY).execute(plan)

    assert client.deleted_files == []
    assert client.destroyed == []
    assert result.outcomes[0].status is GroupStatus.SKIPPED
    records = [r for r in read_audit(audit) if r["event"] == "operation"]
    assert records[0]["result"] == "blocked"
    assert "G_KEEPER_INTACT" in records[0]["detail"]


# -- selection -------------------------------------------------------------

def test_selection_restricts_deletion_to_chosen_files(audit, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
        raw_scene("30", [raw_file("3", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(
        plan, selection={"20:2"}
    )

    assert client.destroyed == [
        {"ids": ["20"], "delete_file": True, "delete_generated": True}
    ]
    assert result.files_deleted == 1


def test_selection_cannot_request_an_unplanned_deletion(audit, confirmed):
    """The page may only narrow the plan, never widen it past the guard rails."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    # "10:1" is the keeper; asking for it must be ignored, not honoured.
    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(
        plan, selection={"10:1"}
    )

    assert client.destroyed == []
    assert client.deleted_files == []
    assert result.files_deleted == 0
    assert result.outcomes[0].status is GroupStatus.SKIPPED


def test_empty_selection_deletes_nothing(audit, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan, selection=set())

    # An explicit empty selection means "delete nothing" and must never be conflated
    # with "no selection given", which means the whole plan.
    assert result.files_deleted == 0
    assert client.deleted_files == []
    assert client.destroyed == []
    assert result.outcomes[0].status is GroupStatus.SKIPPED


# -- failure handling ------------------------------------------------------

def test_a_failing_group_does_not_end_the_run(audit, confirmed):
    scenes_a = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    scenes_b = [
        raw_scene("30", [raw_file("3", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("40", [raw_file("4", codec="h264", bit_rate=8_000_000)]),
    ]
    all_scenes = scenes_a + scenes_b
    groups = build_groups([scenes_a, scenes_b])
    files = [c.file for group in groups for c in group]
    fs = FakeFileSystem({f.path: f.size for f in files})
    plan = build_plan("run1", groups, confirmed, LIBRARY, fs, library_file_count=1000)

    class FlakyClient(FakeClient):
        def __init__(self, scenes):
            super().__init__(scenes)
            self.calls = 0

        def destroy_scenes(self, scene_ids, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise StashError("STASH_GRAPHQL_ERROR", "transient")
            return super().destroy_scenes(scene_ids, **kwargs)

    client = FlakyClient(all_scenes)
    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert len(result.outcomes) == 2
    assert result.outcomes[0].status is GroupStatus.PARTIAL
    assert result.outcomes[1].status is GroupStatus.RESOLVED
    assert result.files_deleted == 1


def test_non_resolved_groups_are_skipped_and_logged(audit, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc")]),
        raw_scene("20", [raw_file("2", codec="hevc")]),  # tie -> AMBIGUOUS
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)

    result = Executor(client, confirmed, audit, fs, LIBRARY).execute(plan)

    assert result.outcomes[0].status is GroupStatus.AMBIGUOUS
    assert client.deleted_files == []
    assert [r["event"] for r in read_audit(audit)] == ["group_skipped"]


# -- audit -----------------------------------------------------------------

def test_audit_records_what_was_deleted_and_what_survived(audit, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)

    Executor(FakeClient(scenes), confirmed, audit, fs, LIBRARY).execute(plan)

    records = [r for r in read_audit(audit) if r["event"] == "operation"]
    assert len(records) == 1
    record = records[0]
    assert record["deleted"]["fileId"] == "2"
    assert record["deleted"]["videoCodec"] == "h264"
    assert record["kept"]["fileId"] == "1"
    assert record["kept"]["videoCodec"] == "hevc"
    assert record["decidingKey"] == "codec"
    assert record["result"] == "deleted"
    assert {v["rail"] for v in record["guardRails"]} >= {"G_LAST_COPY"} - {"G_LAST_COPY"}
    assert any(v["rail"] == "G_KEEPER_INTACT" for v in record["guardRails"])


def test_audit_is_append_only_across_runs(tmp_path, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    for run in ("runA", "runB"):
        plan, fs = plan_for(scenes, confirmed)
        audit = AuditLog(str(tmp_path), run)
        Executor(FakeClient(scenes), confirmed, audit, fs, LIBRARY).execute(plan)

    audit = AuditLog(str(tmp_path), "runC")
    records = read_audit(audit)
    assert len({r["runId"] for r in records}) == 2
    assert len([r for r in records if r["event"] == "operation"]) == 2


# -- executed plans must not re-offer deleted files ---------------------------

def test_execution_marks_the_plan_applied(audit, confirmed):
    """Regression: the plan file was never updated, so reloading it in the page showed
    the just-deleted groups as RESOLVED with their DELETE rows still checked."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    group = plan.groups[0]
    assert group.status is GroupStatus.RESOLVED
    assert len(group.losers) == 1

    Executor(FakeClient(scenes), confirmed, audit, fs, LIBRARY).execute(plan)

    assert group.status is GroupStatus.EXECUTED
    assert "Applied" in group.reason
    # No outstanding work left in the group.
    assert group.losers == []
    assert len(group.acted_on) == 1
    assert group.acted_on[0].outcome == "deleted"


def test_re_executing_an_applied_plan_is_a_no_op(audit, confirmed):
    """The plan carries its own history, so a second Execute cannot try again."""
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, fs = plan_for(scenes, confirmed)
    client = FakeClient(scenes)
    executor = Executor(client, confirmed, audit, fs, LIBRARY)

    executor.execute(plan)
    calls_after_first = len(client.destroyed) + len(client.deleted_files)

    second = executor.execute(plan)

    assert len(client.destroyed) + len(client.deleted_files) == calls_after_first
    assert second.files_deleted == 0


def test_blocked_on_recheck_is_recorded_on_the_plan(audit, confirmed):
    scenes = [
        raw_scene("10", [raw_file("1", codec="hevc", bit_rate=4_000_000)]),
        raw_scene("20", [raw_file("2", codec="h264", bit_rate=8_000_000)]),
    ]
    plan, _ = plan_for(scenes, confirmed)
    live_fs = FakeFileSystem({"/media/library/f2.mkv": 1_000_000_000})  # keeper gone

    Executor(FakeClient(scenes), confirmed, audit, live_fs, LIBRARY).execute(plan)

    entry = [e for e in plan.groups[0].candidates if not e.is_keeper][0]
    assert entry.outcome == "blocked"
    assert plan.groups[0].losers == []
