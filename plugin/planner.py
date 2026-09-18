"""Turn ranked groups into a plan.

Planning is read-only. It emits intent — which file to keep, which to remove, by what
action, and which guard rails were consulted — and nothing else. The executor is the
only module that mutates anything.
"""

from __future__ import annotations

from typing import Optional, Sequence

from config_schema import Config
from guard_rails import (
    FileSystem,
    RunTotals,
    evaluate_pair,
    group_fingerprint,
    last_copy,
    run_cap,
)
from models import (
    Action,
    Candidate,
    CandidatePlan,
    GroupPlan,
    GroupStatus,
    Verdict,
)
from ranking import rank_group


def _action_for(
    loser: Candidate, keeper: Candidate, doomed_file_ids: frozenset[str]
) -> Action:
    """Which operation removes this loser.

    Deleting a file from a scene that keeps at least one other file is a file operation
    and must not destroy the scene or its generated assets. Removing a scene's *last*
    file is a scene operation, because the scene row would otherwise point at nothing.

    `doomed_file_ids` is every file in the group headed for deletion, which is what makes
    this correct for a multi-file scene where several of its files lose at once. Deciding
    per-loser in isolation produced a plan that could never execute: both files of a
    two-file scene were marked DELETE_FILE, the executor then found no survivor to
    promote to primary and aborted, and because nothing changed on disk the group's
    fingerprint still matched so every later run failed identically — deadlocked.
    """
    if keeper.scene.id == loser.scene.id:
        # The keeper is a file on this same scene, so the scene always survives.
        return Action.DELETE_FILE

    survivors = [
        file_id
        for file_id in loser.scene.file_ids
        if file_id != loser.file.id and file_id not in doomed_file_ids
    ]
    return Action.DELETE_FILE if survivors else Action.DESTROY_SCENE


def plan_group(
    index: int,
    candidates: list[Candidate],
    config: Config,
    library_paths: Sequence[str],
    fs: FileSystem,
) -> GroupPlan:
    """Resolve one duplicate group into a `GroupPlan`."""
    fingerprint = group_fingerprint(candidates)

    if len(candidates) < 2:
        return GroupPlan(
            index=index,
            status=GroupStatus.SKIPPED,
            candidates=[CandidatePlan(candidate=c, is_keeper=False) for c in candidates],
            reason="Group has fewer than two files; nothing to decide.",
            fingerprint=fingerprint,
        )

    ranking = rank_group(candidates, config)

    if ranking.keeper is None:
        # A policy tie with tieBreaker=skip. Reported rather than guessed, because a tie
        # usually means the rank order does not yet express the operator's intent.
        return GroupPlan(
            index=index,
            status=GroupStatus.AMBIGUOUS,
            candidates=[CandidatePlan(candidate=c, is_keeper=False) for c in ranking.ordered],
            deciding_key="tie",
            reason=(
                f"{len(ranking.tied)} files tie under rankOrder "
                f"'{','.join(config.rank_order)}' and tieBreaker is 'skip'."
            ),
            fingerprint=fingerprint,
        )

    keeper = ranking.keeper
    entries: list[CandidatePlan] = []
    blocked_count = 0

    # Evaluate the rails for every loser first, so the set of files actually headed for
    # deletion is known before choosing each one's action. A blocked loser survives, so
    # it must not be counted as doomed when deciding whether a scene keeps a file.
    verdicts_by_key: dict[str, list] = {}
    doomed: set[str] = set()
    for candidate in ranking.ordered:
        if candidate is keeper:
            continue
        verdicts = evaluate_pair(keeper, candidate, config, library_paths, fs)
        verdicts_by_key[candidate.key] = verdicts
        if all(v.allowed for v in verdicts):
            doomed.add(candidate.file.id)
    doomed_file_ids = frozenset(doomed)

    for candidate in ranking.ordered:
        if candidate is keeper:
            entries.append(CandidatePlan(candidate=candidate, is_keeper=True))
            continue

        verdicts = verdicts_by_key[candidate.key]
        blocked = [v for v in verdicts if not v.allowed]
        if blocked:
            blocked_count += 1
            entries.append(
                CandidatePlan(candidate=candidate, is_keeper=False, verdicts=verdicts)
            )
        else:
            entries.append(
                CandidatePlan(
                    candidate=candidate,
                    is_keeper=False,
                    action=_action_for(candidate, keeper, doomed_file_ids),
                    verdicts=verdicts,
                )
            )

    loser_count = sum(1 for entry in entries if entry.action and not entry.is_keeper)

    # G_LAST_COPY, asserted before the plan is allowed to exist.
    survival = last_copy(len(candidates), loser_count)
    if not survival.allowed:
        return GroupPlan(
            index=index,
            status=GroupStatus.FAILED,
            candidates=entries,
            deciding_key=ranking.deciding,
            reason=f"G_LAST_COPY: {survival.reason}",
            fingerprint=fingerprint,
        )

    if loser_count == 0:
        blocked_rails = sorted(
            {
                verdict.rail
                for entry in entries
                for verdict in entry.blocked_by
            }
        )
        if not blocked_count:
            status, reason = GroupStatus.SKIPPED, "No files to remove."
        elif blocked_rails == ["G_PROTECTED_PATHS"]:
            # Reserve PROTECTED for an operator-declared protected path, so it does not
            # get confused with a quality or duration rail firing.
            status = GroupStatus.PROTECTED
            reason = f"All {blocked_count} removal candidates are under a protected path."
        else:
            status = GroupStatus.SKIPPED
            reason = (
                f"All {blocked_count} removal candidates were blocked by "
                f"{', '.join(blocked_rails)}."
            )
        return GroupPlan(
            index=index,
            status=status,
            candidates=entries,
            deciding_key=ranking.deciding,
            reason=reason,
            fingerprint=fingerprint,
        )

    return GroupPlan(
        index=index,
        status=GroupStatus.RESOLVED,
        candidates=entries,
        deciding_key=ranking.deciding,
        reason=(
            f"Keeping {keeper.file.basename} ({keeper.file.video_codec}, "
            f"{keeper.file.width}x{keeper.file.height}) on '{ranking.deciding}'."
        ),
        fingerprint=fingerprint,
    )


class Plan:
    """A whole run's intent, plus the cap verdict that gates executing it."""

    def __init__(
        self,
        run_id: str,
        config: Config,
        groups: list[GroupPlan],
        library_file_count: int,
        cap_verdict: Verdict,
        stash_version: str = "",
    ):
        self.run_id = run_id
        self.config = config
        self.groups = groups
        self.library_file_count = library_file_count
        self.cap_verdict = cap_verdict
        self.stash_version = stash_version

    @property
    def resolved(self) -> list[GroupPlan]:
        return [g for g in self.groups if g.status is GroupStatus.RESOLVED]

    @property
    def total_deletions(self) -> int:
        return sum(len(g.losers) for g in self.groups)

    @property
    def bytes_reclaimed(self) -> int:
        return sum(g.bytes_reclaimed for g in self.groups)

    @property
    def is_executable(self) -> bool:
        return self.cap_verdict.allowed

    def counts_by_status(self) -> dict[str, int]:
        counts = {status.value: 0 for status in GroupStatus}
        for group in self.groups:
            counts[group.status.value] += 1
        return counts

    def summary(self) -> dict:
        return {
            "runId": self.run_id,
            "stashVersion": self.stash_version,
            "groups": len(self.groups),
            "groupsByStatus": self.counts_by_status(),
            "filesToDelete": self.total_deletions,
            "bytesReclaimed": self.bytes_reclaimed,
            "libraryFileCount": self.library_file_count,
            "runCap": {
                "allowed": self.cap_verdict.allowed,
                "reason": self.cap_verdict.reason,
            },
            "config": self.config.as_dict(),
        }


def build_plan(
    run_id: str,
    groups: list[list[Candidate]],
    config: Config,
    library_paths: Sequence[str],
    fs: FileSystem,
    *,
    library_file_count: int = 0,
    stash_version: str = "",
    on_progress: Optional[callable] = None,
) -> Plan:
    """Plan every group, then evaluate `G_RUN_CAP` over the whole run.

    The cap is evaluated last and across the entire plan, so a breach is reported
    before anything is deleted rather than stopping a run halfway through.
    """
    group_plans: list[GroupPlan] = []
    total = len(groups)

    for index, candidates in enumerate(groups):
        try:
            group_plans.append(plan_group(index, candidates, config, library_paths, fs))
        except Exception as exc:  # one bad group must not end the run
            group_plans.append(
                GroupPlan(
                    index=index,
                    status=GroupStatus.FAILED,
                    candidates=[
                        CandidatePlan(candidate=c, is_keeper=False) for c in candidates
                    ],
                    reason=f"{type(exc).__name__}: {exc}",
                    fingerprint=group_fingerprint(candidates),
                )
            )
        if on_progress and total:
            on_progress((index + 1) / total)

    planned = sum(len(g.losers) for g in group_plans)
    cap = run_cap(
        RunTotals(planned_deletions=planned, library_file_count=library_file_count), config
    )

    return Plan(
        run_id=run_id,
        config=config,
        groups=group_plans,
        library_file_count=library_file_count,
        cap_verdict=cap,
        stash_version=stash_version,
    )
