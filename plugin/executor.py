"""Plan execution — the only module that deletes anything.

Every guard rail is re-evaluated here against live state before each operation, because
the library may have changed since the plan was written. Within a group, operations run
in the order metadata-merge → file-delete → scene-destroy, so a failure part way
through never leaves a scene stripped of its metadata but still present, or a file
deleted whose metadata had not yet been folded into the keeper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from audit import AuditLog
from candidates import SCENE_FRAGMENT, build_group
from config_schema import Config
from guard_rails import (
    FileSystem,
    RunTotals,
    confirm_destructive,
    evaluate_pair,
    first_block,
    generated_flag,
    group_fingerprint,
    last_copy,
    plan_fresh,
    run_cap,
)
from models import (
    Action,
    Candidate,
    CandidatePlan,
    GroupPlan,
    GroupStatus,
    Verdict,
    merge_values,
)
from planner import Plan
from stash_client import StashClient, StashError


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


class ExecutionAborted(RuntimeError):
    """A whole-run condition that must stop execution before anything is deleted."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class GroupOutcome:
    index: int
    status: GroupStatus
    reason: str = ""
    deleted_file_ids: list[str] = field(default_factory=list)
    destroyed_scene_ids: list[str] = field(default_factory=list)
    merged_scene_ids: list[str] = field(default_factory=list)
    bytes_reclaimed: int = 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "status": self.status.value,
            "reason": self.reason,
            "deletedFileIds": self.deleted_file_ids,
            "destroyedSceneIds": self.destroyed_scene_ids,
            "mergedSceneIds": self.merged_scene_ids,
            "bytesReclaimed": self.bytes_reclaimed,
        }


@dataclass
class ExecutionResult:
    outcomes: list[GroupOutcome] = field(default_factory=list)

    @property
    def files_deleted(self) -> int:
        return sum(len(o.deleted_file_ids) for o in self.outcomes)

    @property
    def scenes_destroyed(self) -> int:
        return sum(len(o.destroyed_scene_ids) for o in self.outcomes)

    @property
    def bytes_reclaimed(self) -> int:
        return sum(o.bytes_reclaimed for o in self.outcomes)

    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.status.value] = counts.get(outcome.status.value, 0) + 1
        return counts

    def summary(self) -> dict:
        return {
            "groupsProcessed": len(self.outcomes),
            "groupsByStatus": self.counts_by_status(),
            "filesDeleted": self.files_deleted,
            "scenesDestroyed": self.scenes_destroyed,
            "bytesReclaimed": self.bytes_reclaimed,
            "groups": [o.as_dict() for o in self.outcomes],
        }


class Executor:
    def __init__(
        self,
        client: StashClient,
        config: Config,
        audit: AuditLog,
        fs: FileSystem,
        library_paths: Sequence[str],
        logger=None,
    ):
        self.client = client
        self.config = config
        self.audit = audit
        self.fs = fs
        self.library_paths = library_paths
        self.log = logger

    # -- whole-run gates ---------------------------------------------------

    def _assert_runnable(self, plan: Plan, selection: Optional[set[str]]) -> None:
        """Gates that must pass before a single file is touched."""
        confirm = confirm_destructive(self.config)
        if not confirm.allowed:
            raise ExecutionAborted("G_CONFIRM", confirm.reason)

        planned = self._selected_count(plan, selection)
        cap = run_cap(
            RunTotals(
                planned_deletions=planned, library_file_count=plan.library_file_count
            ),
            self.config,
        )
        if not cap.allowed:
            raise ExecutionAborted("G_RUN_CAP", cap.reason)

    @staticmethod
    def _selected_count(plan: Plan, selection: Optional[set[str]]) -> int:
        total = 0
        for group in plan.groups:
            for entry in group.losers:
                if selection is None or entry.candidate.key in selection:
                    total += 1
        return total

    # -- execution ---------------------------------------------------------

    def execute(
        self,
        plan: Plan,
        *,
        selection: Optional[set[str]] = None,
        on_progress: Optional[callable] = None,
    ) -> ExecutionResult:
        """Apply `plan`.

        `selection` restricts execution to specific `scene_id:file_id` keys — the UI
        page passes the operator's checkbox state. `None` means the whole plan. A key
        that is not already a planned loser is ignored, so the UI cannot ask for a
        deletion the guard rails never approved.
        """
        self._assert_runnable(plan, selection)

        result = ExecutionResult()
        total = len(plan.groups)

        for position, group in enumerate(plan.groups):
            # Created up front and mutated in place, so an exception of any type cannot
            # discard the record of operations that already completed. Building a fresh
            # outcome in the handler lost them, and the run summary then reported zero
            # files deleted while files were genuinely gone from disk.
            outcome = GroupOutcome(index=group.index, status=GroupStatus.FAILED)
            try:
                self._execute_group(group, selection, outcome)
            except StashError as exc:
                outcome.status = GroupStatus.PARTIAL if outcome.deleted_file_ids else \
                    GroupStatus.FAILED
                outcome.reason = f"{exc.code}: {exc.message}"
                if self.log:
                    self.log.error(
                        f"group {group.index} failed: {exc.code}: {exc.message}"
                    )
            except Exception as exc:  # one group must not end the run
                outcome.status = GroupStatus.PARTIAL if outcome.deleted_file_ids else \
                    GroupStatus.FAILED
                outcome.reason = f"{type(exc).__name__}: {exc}"
                if self.log:
                    self.log.error(f"group {group.index} failed: {type(exc).__name__}: {exc}")

            # Reflect the result back onto the plan so a re-read reports what happened
            # rather than re-presenting deleted files as pending work.
            if outcome.status is GroupStatus.RESOLVED and outcome.deleted_file_ids:
                group.status = GroupStatus.EXECUTED
                group.reason = (
                    f"Applied: {len(outcome.deleted_file_ids)} file(s) removed, "
                    f"{human_size(outcome.bytes_reclaimed)} reclaimed."
                )
            elif outcome.status is not GroupStatus.RESOLVED:
                group.status = outcome.status
                if outcome.reason:
                    group.reason = outcome.reason

            result.outcomes.append(outcome)
            if on_progress and total:
                on_progress((position + 1) / total)

        return result

    def _execute_group(
        self, group: GroupPlan, selection: Optional[set[str]], outcome: GroupOutcome
    ) -> None:
        """Apply one group, recording progress on `outcome` as it goes.

        Mutates the caller's outcome rather than returning a new one, so an exception
        raised part way through cannot discard the record of what already completed.
        """
        def stop(status: GroupStatus, reason: str, *, audit_status: str = "") -> None:
            outcome.status = status
            outcome.reason = reason
            self.audit.group_skipped(group.index, audit_status or status.value, reason)

        if group.status is not GroupStatus.RESOLVED:
            stop(group.status, group.reason)
            return

        keeper_entry = group.keeper
        if keeper_entry is None:
            stop(GroupStatus.FAILED, "Plan marked the group resolved but recorded no keeper.")
            return

        targets = [
            entry
            for entry in group.losers
            if selection is None or entry.candidate.key in selection
        ]
        if not targets:
            outcome.status = GroupStatus.SKIPPED
            outcome.reason = "No candidates selected in this group."
            return

        # G_PLAN_FRESH — the library must still look as it did when the plan was written.
        live_fingerprint, live_by_id = self._refresh_group(group)
        freshness = plan_fresh(group.fingerprint, live_fingerprint)
        if not freshness.allowed:
            stop(GroupStatus.SKIPPED, f"G_PLAN_FRESH: {freshness.reason}")
            return

        # The plan is untrusted input. Confirm it names the same files, at the same
        # paths, that Stash reports right now.
        mismatch = self._reconcile(group, live_by_id)
        if mismatch:
            stop(GroupStatus.FAILED, f"G_PLAN_RECONCILE: {mismatch}")
            return

        # From here on, every rail and every mutation uses live objects, never the plan's
        # copies. This is also what lets G_METADATA see metadata added since planning.
        for entry in group.candidates:
            entry.candidate = live_by_id[entry.candidate.file.id]

        # G_LAST_COPY — re-asserted against what is actually about to happen.
        survival = last_copy(len(group.candidates), len(targets))
        if not survival.allowed:
            stop(GroupStatus.FAILED, f"G_LAST_COPY: {survival.reason}")
            return

        keeper = keeper_entry.candidate
        approved: list[tuple[CandidatePlan, list[Verdict]]] = []

        # Re-evaluate the per-pair rails against live state before committing to anything.
        for entry in targets:
            verdicts = evaluate_pair(
                keeper, entry.candidate, self.config, self.library_paths, self.fs
            )
            blocked = first_block(verdicts)
            if blocked:
                self.audit.operation(
                    group_index=group.index, action=entry.action or Action.DELETE_FILE,
                    loser=entry.candidate, keeper=keeper, deciding_key=group.deciding_key,
                    verdicts=verdicts, result="blocked",
                    detail=f"{blocked.rail}: {blocked.reason}",
                )
                entry.outcome = "blocked"
                continue
            approved.append((entry, verdicts))

        if not approved:
            stop(
                GroupStatus.SKIPPED,
                "Every selected candidate was blocked by a guard rail on re-check.",
            )
            return

        # G_LAST_COPY once more, now that blocking has reduced the set.
        survival = last_copy(len(group.candidates), len(approved))
        if not survival.allowed:
            stop(GroupStatus.FAILED, f"G_LAST_COPY: {survival.reason}")
            return

        outcome.status = GroupStatus.RESOLVED
        try:
            self._merge_metadata(group, keeper, approved, outcome)
            self._delete_files(group, keeper, approved, outcome)
            self._destroy_scenes(group, keeper, approved, outcome)
        except StashError as exc:
            outcome.status = GroupStatus.PARTIAL
            outcome.reason = (
                f"Aborted part way through: {exc.code}: {exc.message}. "
                f"Any completed operations are recorded in the audit log."
            )
            if self.log:
                self.log.error(f"group {group.index} partially applied: {exc.message}")

    def _refresh_group(self, group: GroupPlan) -> tuple[str, dict[str, Candidate]]:
        """Read the group fresh from Stash.

        Returns the live fingerprint and the live candidates indexed by file id. The
        candidates matter as much as the fingerprint: the plan file is an input, so its
        claimed `(file id, path)` pairing cannot be trusted. Rails must judge — and
        deletions must target — objects built from live state.
        """
        scene_ids = sorted({entry.candidate.scene.id for entry in group.candidates})
        query = """
        query RefreshScenes($ids: [ID!]) {
          findScenes(ids: $ids, filter: {per_page: -1}) {
            scenes { %s }
          }
        }
        """ % SCENE_FRAGMENT
        data = self.client.call(query, {"ids": scene_ids}, operation="findScenes(refresh)")
        scenes = ((data.get("findScenes") or {}).get("scenes")) or []
        live = build_group(scenes)
        return group_fingerprint(live), {c.file.id: c for c in live}

    @staticmethod
    def _reconcile(
        group: GroupPlan, live_by_id: dict[str, Candidate]
    ) -> Optional[str]:
        """Check the plan describes the same files Stash currently reports.

        `G_PLAN_FRESH` alone does not cover this, because the fingerprint it compares
        against is itself a value read out of the plan file. Without this check a plan
        naming a file id that is not in the group at all — paired with the path of some
        unrelated file — passes every rail (they all inspect the fabricated path) and
        then deletes the file the id points to. Returns None when the plan reconciles.
        """
        planned = {entry.candidate.file.id for entry in group.candidates}
        live = set(live_by_id)
        if planned != live:
            missing = sorted(planned - live)
            added = sorted(live - planned)
            return (
                f"Plan describes files {missing or '[]'} that Stash no longer reports, "
                f"and Stash reports {added or '[]'} the plan does not. Re-run the Plan task."
            )
        for entry in group.candidates:
            planned_file = entry.candidate.file
            live_file = live_by_id[planned_file.id].file
            if planned_file.path != live_file.path:
                return (
                    f"File {planned_file.id} is at '{live_file.path}' but the plan says "
                    f"'{planned_file.path}'. Re-run the Plan task."
                )
        return None

    def _merge_metadata(
        self,
        group: GroupPlan,
        keeper: Candidate,
        approved: list[tuple[CandidatePlan, list[Verdict]]],
        outcome: GroupOutcome,
    ) -> None:
        """Fold losing scenes' metadata into the keeper before anything is destroyed."""
        if self.config.metadata_policy != "merge":
            return

        source_scenes = {
            entry.candidate.scene.id: entry.candidate.scene
            for entry, verdicts in approved
            if entry.action is Action.DESTROY_SCENE
            and entry.candidate.scene.id != keeper.scene.id
            and any(v.context.get("merge_required") for v in verdicts)
        }
        sources = sorted(source_scenes)
        if not sources:
            return

        # sceneMerge copies nothing on its own, so the union of metadata has to be
        # computed and passed as `values` or the sources' curation is silently dropped.
        values = merge_values(keeper.scene, [source_scenes[sid] for sid in sources])
        merged = self.client.merge_scenes(sources, keeper.scene.id, values)
        if not merged.get("id"):
            raise StashError(
                "MERGE_UNVERIFIED",
                f"sceneMerge into scene {keeper.scene.id} returned no scene; "
                f"refusing to destroy the sources.",
                {"sources": sources, "destination": keeper.scene.id},
            )
        outcome.merged_scene_ids = sources
        if self.log:
            self.log.info(
                f"group {group.index}: merged metadata from scenes "
                f"{','.join(sources)} into {keeper.scene.id}"
            )

    def _delete_files(
        self,
        group: GroupPlan,
        keeper: Candidate,
        approved: list[tuple[CandidatePlan, list[Verdict]]],
        outcome: GroupOutcome,
    ) -> None:
        """Delete files whose scenes survive.

        `G_GENERATED`: generated assets are left alone here, because the scene's other
        files still use them.
        """
        entries = [(e, v) for e, v in approved if e.action is Action.DELETE_FILE]
        if not entries:
            return

        # If a loser is its scene's primary file, promote a survivor first so the scene
        # is never left pointing at a deleted file.
        for entry, _ in entries:
            candidate = entry.candidate
            if not candidate.is_primary_file:
                continue
            doomed = {e.candidate.file.id for e, _ in entries}
            survivor = next(
                (
                    file_id
                    for file_id in candidate.scene.file_ids
                    if file_id not in doomed
                ),
                None,
            )
            if survivor is None:
                raise StashError(
                    "NO_SURVIVING_PRIMARY",
                    f"Scene {candidate.scene.id} would lose every file including its "
                    f"primary; refusing.",
                    {"scene_id": candidate.scene.id},
                )
            if not self.client.set_primary_file(candidate.scene.id, survivor):
                raise StashError(
                    "PRIMARY_REASSIGN_FAILED",
                    f"Could not promote file {survivor} to primary for scene "
                    f"{candidate.scene.id}; refusing to delete its current primary.",
                    {"scene_id": candidate.scene.id, "file_id": survivor},
                )
            if self.log:
                self.log.info(
                    f"group {group.index}: promoted file {survivor} to primary for "
                    f"scene {candidate.scene.id}"
                )

        file_ids = [entry.candidate.file.id for entry, _ in entries]
        if not self.client.delete_files(file_ids):
            raise StashError(
                "DELETE_FILES_FAILED",
                f"deleteFiles reported failure for {len(file_ids)} files; not recording "
                f"them as deleted.",
                {"file_ids": file_ids},
            )

        for entry, verdicts in entries:
            entry.outcome = "deleted"
            outcome.deleted_file_ids.append(entry.candidate.file.id)
            outcome.bytes_reclaimed += entry.candidate.file.size
            self.audit.operation(
                group_index=group.index, action=Action.DELETE_FILE,
                loser=entry.candidate, keeper=keeper, deciding_key=group.deciding_key,
                verdicts=verdicts, result="deleted",
                detail="deleteFiles (scene retained, generated assets retained)",
            )

    def _destroy_scenes(
        self,
        group: GroupPlan,
        keeper: Candidate,
        approved: list[tuple[CandidatePlan, list[Verdict]]],
        outcome: GroupOutcome,
    ) -> None:
        """Remove the losers whose scene is going away.

        Split by whether the scene was merged, because merging already destroyed it:

        - **Merged**: the scene row is gone and its file now hangs off the keeper scene,
          untouched on disk. `scenesDestroy` would fail with "scene not found" and the
          video would survive, so the file is removed with `deleteFiles` instead.
        - **Not merged**: the scene still exists, so `scenesDestroy(delete_file: true)`
          removes both the row and the file in one call, and takes the generated assets
          with it.
        """
        entries = [(e, v) for e, v in approved if e.action is Action.DESTROY_SCENE]
        if not entries:
            return

        if keeper.scene.id in {entry.candidate.scene.id for entry, _ in entries}:
            raise StashError(
                "KEEPER_SCENE_TARGETED",
                f"Refusing to destroy scene {keeper.scene.id}, which holds the keeper.",
                {"scene_id": keeper.scene.id},
            )

        merged = set(outcome.merged_scene_ids)
        orphaned = [(e, v) for e, v in entries if e.candidate.scene.id in merged]
        intact = [(e, v) for e, v in entries if e.candidate.scene.id not in merged]

        if orphaned:
            file_ids = [entry.candidate.file.id for entry, _ in orphaned]
            if not self.client.delete_files(file_ids):
                raise StashError(
                    "DELETE_FILES_FAILED",
                    f"deleteFiles reported failure for {len(file_ids)} files whose scenes "
                    f"were merged into the keeper.",
                    {"file_ids": file_ids},
                )
            for entry, verdicts in orphaned:
                entry.outcome = "deleted"
                outcome.deleted_file_ids.append(entry.candidate.file.id)
                outcome.bytes_reclaimed += entry.candidate.file.size
                self.audit.operation(
                    group_index=group.index, action=Action.DESTROY_SCENE,
                    loser=entry.candidate, keeper=keeper, deciding_key=group.deciding_key,
                    verdicts=verdicts, result="deleted",
                    detail="sceneMerge destroyed the scene; file removed with deleteFiles",
                )

        if intact:
            scene_ids = sorted({entry.candidate.scene.id for entry, _ in intact})
            if not self.client.destroy_scenes(
                scene_ids,
                delete_file=True,
                delete_generated=generated_flag(is_scene_destroy=True),
            ):
                raise StashError(
                    "DESTROY_SCENES_FAILED",
                    f"scenesDestroy reported failure for scenes {', '.join(scene_ids)}; "
                    f"not recording them as deleted.",
                    {"scene_ids": scene_ids},
                )
            for entry, verdicts in intact:
                entry.outcome = "deleted"
                outcome.destroyed_scene_ids.append(entry.candidate.scene.id)
                outcome.deleted_file_ids.append(entry.candidate.file.id)
                outcome.bytes_reclaimed += entry.candidate.file.size
                self.audit.operation(
                    group_index=group.index, action=Action.DESTROY_SCENE,
                    loser=entry.candidate, keeper=keeper, deciding_key=group.deciding_key,
                    verdicts=verdicts, result="deleted",
                    detail="scenesDestroy(delete_file: true, delete_generated: true)",
                )
