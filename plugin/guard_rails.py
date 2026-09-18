"""Guard rails.

Every rail is a pure function returning a `Verdict`. Rails are evaluated during
planning and re-evaluated immediately before each destructive call, so a library that
changed between the two phases cannot be acted on with stale assumptions.

Filesystem access goes through the injected `FileSystem` protocol rather than calling
`os` directly, which keeps the rails unit-testable without touching disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from config_schema import Config, normalise_codec
from models import Candidate, SceneInfo, Verdict
from ranking import ranks_strictly_better_codec


class FileSystem(Protocol):
    def exists(self, path: str) -> bool: ...
    def size(self, path: str) -> int: ...
    def readable(self, path: str) -> bool: ...
    def realpath(self, path: str) -> str: ...


class RealFileSystem:
    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def size(self, path: str) -> int:
        return os.path.getsize(path)

    def readable(self, path: str) -> bool:
        return os.access(path, os.R_OK)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)


@dataclass
class RunTotals:
    """Accumulated across a whole plan, for `G_RUN_CAP`."""

    planned_deletions: int = 0
    library_file_count: int = 0


def _ok(rail: str, **context) -> Verdict:
    return Verdict(rail=rail, allowed=True, context=context)


def _block(rail: str, reason: str, **context) -> Verdict:
    return Verdict(rail=rail, allowed=False, reason=reason, context=context)


def last_copy(group_size: int, loser_count: int) -> Verdict:
    """`G_LAST_COPY` — a group must always end with at least one surviving file.

    The single invariant that must never fail. Asserted for every group before any
    operation is emitted, and again before execution.
    """
    rail = "G_LAST_COPY"
    if group_size <= 0:
        return _block(rail, "Group contains no candidates.", group_size=group_size)
    if loser_count >= group_size:
        return _block(
            rail,
            f"Plan would delete all {loser_count} of {group_size} files in the group.",
            group_size=group_size,
            loser_count=loser_count,
        )
    if loser_count < 0:
        return _block(rail, "Negative loser count.", loser_count=loser_count)
    return _ok(rail, survivors=group_size - loser_count)


def keeper_intact(keeper: Candidate, fs: FileSystem) -> Verdict:
    """`G_KEEPER_INTACT` — the keeper must be present, readable, and the expected size.

    Catches the case that matters most on unRAID: an unmounted or spun-down share
    making the keeper unavailable while its database row still looks healthy. Deleting
    the alternative in that state destroys the only usable copy.
    """
    rail = "G_KEEPER_INTACT"
    path = keeper.file.path
    if not fs.exists(path):
        return _block(rail, f"Keeper file does not exist on disk: {path}", path=path)
    if not fs.readable(path):
        return _block(rail, f"Keeper file is not readable: {path}", path=path)
    try:
        actual = fs.size(path)
    except OSError as exc:
        return _block(rail, f"Cannot stat keeper file: {exc}", path=path)
    if actual <= 0:
        return _block(rail, f"Keeper file is zero length: {path}", path=path, actual_size=actual)
    expected = keeper.file.size
    if expected <= 0:
        # Without a recorded size the drift check cannot run, and the drift check is the
        # only thing catching a file that is still being written. Say so rather than
        # passing silently.
        return _block(
            rail,
            f"Keeper has no recorded size in the database, so it cannot be verified "
            f"against the {actual} bytes on disk: {path}",
            path=path, actual_size=actual, expected_size=expected,
        )
    if True:
        drift = abs(actual - expected) / expected
        if drift > 0.01:
            return _block(
                rail,
                f"Keeper size on disk ({actual}) differs from the database ({expected}) "
                f"by {drift:.1%}; the file may still be being written.",
                path=path, actual_size=actual, expected_size=expected,
            )
    return _ok(rail, path=path, size=actual)


def library_scope(candidate: Candidate, library_paths: Sequence[str], fs: FileSystem) -> Verdict:
    """`G_LIBRARY_SCOPE` — never delete outside Stash's configured library paths.

    Paths are resolved through symlinks before comparison, so a symlinked file cannot
    be used to reach outside the library. An empty `library_paths` blocks everything
    rather than permitting everything.
    """
    rail = "G_LIBRARY_SCOPE"
    path = candidate.file.path
    if not library_paths:
        return _block(rail, "No Stash library paths are known; refusing to delete anything.")
    try:
        resolved = fs.realpath(path)
    except OSError as exc:
        return _block(rail, f"Cannot resolve path: {exc}", path=path)
    for root in library_paths:
        try:
            resolved_root = fs.realpath(root)
        except OSError:
            continue
        if resolved == resolved_root or resolved.startswith(resolved_root.rstrip(os.sep) + os.sep):
            return _ok(rail, path=resolved, root=resolved_root)
    return _block(
        rail,
        f"Path lies outside every configured library path: {resolved}",
        path=resolved, library_paths=list(library_paths),
    )


def protected_path(candidate: Candidate, config: Config, fs: FileSystem) -> Verdict:
    """`G_PROTECTED_PATHS` — paths the operator has marked as never-delete.

    Resolves symlinks before comparing, mirroring `G_LIBRARY_SCOPE`. Comparing the raw
    path was a real hole on unRAID, where the same content is routinely reachable as
    both `/mnt/user/media/...` and `/mnt/disk1/media/...`: protecting the user-share path
    gave no protection at all to content Stash had scanned through the disk path.

    Also compares on a path boundary, so protecting `/media/Keep` no longer
    accidentally protects `/media/Keepsakes`.
    """
    rail = "G_PROTECTED_PATHS"
    raw = candidate.file.path
    try:
        resolved = fs.realpath(raw)
    except OSError as exc:
        # Cannot tell whether it is protected, so treat it as protected.
        return _block(rail, f"Cannot resolve path, treating as protected: {exc}", path=raw)

    for prefix in config.protected_paths:
        try:
            root = fs.realpath(prefix)
        except OSError:
            root = prefix
        for path in (resolved, raw):
            for base in (root, prefix):
                if path == base or path.startswith(base.rstrip(os.sep) + os.sep):
                    return _block(
                        rail,
                        f"Path is protected by prefix '{prefix}'"
                        + (f" (resolves to {resolved})" if resolved != raw else "")
                        + ".",
                        path=raw, resolved=resolved, prefix=prefix,
                    )
    return _ok(rail, path=raw, resolved=resolved)


def duration_match(keeper: Candidate, loser: Candidate, config: Config) -> Verdict:
    """`G_DURATION` — keeper and loser must be the same length.

    Two files can share a phash while one is truncated, because the hash is computed
    from a sample rather than the whole file. Without this rail a partial encode that
    ranks well on codec could replace a complete file.
    """
    rail = "G_DURATION"
    delta = abs(keeper.file.duration - loser.file.duration)
    if delta > config.duration_tolerance:
        return _block(
            rail,
            f"Durations differ by {delta:.2f}s, over the {config.duration_tolerance:.2f}s "
            f"tolerance (keeper {keeper.file.duration:.2f}s, loser {loser.file.duration:.2f}s).",
            delta=delta, tolerance=config.duration_tolerance,
            keeper_duration=keeper.file.duration, loser_duration=loser.file.duration,
        )
    return _ok(rail, delta=delta)


def quality_floor(keeper: Candidate, loser: Candidate, config: Config) -> Verdict:
    """`G_QUALITY_FLOOR` — reject a keeper that looks like a botched transcode.

    Compares bits per pixel per frame, which is comparable across resolutions. The
    allowance is far looser when the keeper uses a better-ranked codec, because HEVC
    reaches similar perceptual quality at roughly half H.264's bitrate; a flat ratio
    would reject nearly every legitimate HEVC keeper.
    """
    rail = "G_QUALITY_FLOOR"
    keeper_bppf = keeper.file.bits_per_pixel_frame
    loser_bppf = loser.file.bits_per_pixel_frame

    if loser_bppf <= 0 or keeper_bppf <= 0:
        # Missing bitrate or frame rate metadata; the rail cannot judge, so it abstains
        # rather than blocking every group in a library with incomplete scan data.
        return _ok(rail, indeterminate=True, keeper_bppf=keeper_bppf, loser_bppf=loser_bppf)

    # Three distinct situations, which the message must not conflate. Only a genuine
    # codec upgrade earns the looser allowance; a keeper on a *worse* codec that also
    # carries fewer bits is the most suspicious case of all, and keeps the strict floor.
    keeper_codec = normalise_codec(keeper.file.video_codec)
    loser_codec = normalise_codec(loser.file.video_codec)
    upgrade = ranks_strictly_better_codec(keeper, loser, config)
    if upgrade:
        comparison = "codec-upgrade"
        threshold = config.quality_floor_ratio_cross_codec
    elif keeper_codec == loser_codec:
        comparison = "same-codec"
        threshold = config.quality_floor_ratio
    else:
        comparison = "keeper-on-lower-ranked-codec"
        threshold = config.quality_floor_ratio

    ratio = keeper_bppf / loser_bppf
    if ratio < threshold:
        return _block(
            rail,
            f"Keeper quality ratio {ratio:.3f} is below the {threshold:.3f} floor "
            f"({comparison}: keeping {keeper_codec or '?'} over {loser_codec or '?'}); "
            f"the keeper may be a failed transcode.",
            ratio=ratio, threshold=threshold, cross_codec=upgrade,
            comparison=comparison, keeper_codec=keeper_codec, loser_codec=loser_codec,
            keeper_bppf=keeper_bppf, loser_bppf=loser_bppf,
        )
    return _ok(
        rail, ratio=ratio, threshold=threshold, cross_codec=upgrade,
        comparison=comparison, keeper_codec=keeper_codec, loser_codec=loser_codec,
    )


def metadata_preserved(
    keeper_scene: SceneInfo, loser_scene: SceneInfo, config: Config
) -> Verdict:
    """`G_METADATA` — do not silently lose curation work.

    Under `merge` the caller must run `sceneMerge` before destroying the loser; the
    verdict carries what would be lost so the executor knows what to verify landed.
    """
    rail = "G_METADATA"
    if keeper_scene.id == loser_scene.id:
        return _ok(rail, same_scene=True)

    lost = loser_scene.has_metadata_absent_from(keeper_scene)
    if not lost:
        return _ok(rail, lost={})

    if config.metadata_policy == "ignore":
        return _ok(rail, lost=lost, policy="ignore")
    if config.metadata_policy == "merge":
        return _ok(rail, lost=lost, policy="merge", merge_required=True)
    return _block(
        rail,
        "Losing scene carries metadata the keeper lacks and metadataPolicy is 'skip': "
        + ", ".join(sorted(lost)),
        lost=lost, policy="skip",
    )


def run_cap(totals: RunTotals, config: Config) -> Verdict:
    """`G_RUN_CAP` — bound the damage a misconfigured policy can do in one pass.

    Evaluated over the whole plan before any deletion, so a breach aborts the run
    rather than stopping halfway through.
    """
    rail = "G_RUN_CAP"
    if totals.planned_deletions > config.max_deletions_per_run:
        return _block(
            rail,
            f"Plan deletes {totals.planned_deletions} files, over the "
            f"maxDeletionsPerRun limit of {config.max_deletions_per_run}.",
            planned=totals.planned_deletions, limit=config.max_deletions_per_run,
        )
    if totals.library_file_count > 0:
        fraction = totals.planned_deletions / totals.library_file_count
        if fraction > config.max_fraction_of_library:
            return _block(
                rail,
                f"Plan deletes {fraction:.2%} of the library, over the "
                f"maxFractionOfLibrary limit of {config.max_fraction_of_library:.2%}.",
                fraction=fraction, limit=config.max_fraction_of_library,
                planned=totals.planned_deletions, library_size=totals.library_file_count,
            )
    return _ok(rail, planned=totals.planned_deletions)


def confirm_destructive(config: Config) -> Verdict:
    """`G_CONFIRM` — hard deletion needs an explicit opt-in beyond running the task."""
    rail = "G_CONFIRM"
    if not config.confirm_destructive:
        return _block(
            rail,
            "confirmDestructive is false. Enable it in the plugin settings to allow "
            "this task to delete files.",
        )
    return _ok(rail)


def group_fingerprint(candidates: Sequence[Candidate]) -> str:
    """Stable digest of a group's on-disk state, for `G_PLAN_FRESH`.

    Covers the fields that change when a file is re-encoded, moved, or rewritten.
    Sorted by file id so the digest does not depend on the order Stash returned.
    """
    payload = sorted(
        [
            {
                "file_id": c.file.id,
                "size": c.file.size,
                "mod_time": round(c.file.mod_time, 3),
                "path": c.file.path,
                # The scene's file order decides which file is primary, and the executor
                # promotes a survivor before deleting a primary. Leaving these out let a
                # rescan reassign the primary without changing the digest, after which
                # the plan's stale is_primary_file caused the new primary to be deleted
                # with no promotion.
                "scene_id": c.scene.id,
                "scene_file_ids": list(c.scene.file_ids),
                "primary_file_id": c.scene.primary_file_id,
            }
            for c in candidates
        ],
        key=lambda entry: entry["file_id"],
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_fresh(expected: str, actual: str) -> Verdict:
    """`G_PLAN_FRESH` — refuse to execute a plan against a changed library."""
    rail = "G_PLAN_FRESH"
    if not expected:
        return _block(rail, "Plan entry carries no fingerprint.")
    if expected != actual:
        return _block(
            rail,
            "Group changed since the plan was written (a file was moved, re-encoded, "
            "or rescanned). Re-run the Plan task.",
            expected=expected, actual=actual,
        )
    return _ok(rail)


def generated_flag(is_scene_destroy: bool) -> bool:
    """`G_GENERATED` — only scene destruction may remove generated assets.

    Deleting a file from a multi-file scene must leave sprites and previews alone,
    because the surviving files of that scene still use them.
    """
    return bool(is_scene_destroy)


def evaluate_pair(
    keeper: Candidate,
    loser: Candidate,
    config: Config,
    library_paths: Sequence[str],
    fs: FileSystem,
) -> list[Verdict]:
    """Every rail that applies to deleting `loser` in favour of `keeper`."""
    return [
        keeper_intact(keeper, fs),
        library_scope(loser, library_paths, fs),
        protected_path(loser, config, fs),
        duration_match(keeper, loser, config),
        quality_floor(keeper, loser, config),
        metadata_preserved(keeper.scene, loser.scene, config),
    ]


def first_block(verdicts: Sequence[Verdict]) -> Optional[Verdict]:
    for verdict in verdicts:
        if not verdict.allowed:
            return verdict
    return None
