"""Plugin entry point.

Reads one JSON object from stdin, dispatches on `args.mode`, and writes one JSON
envelope to stdout. Every mode is safe to run except `execute` and `plan_execute`, and
those additionally require `confirmDestructive`.

Modes invoked by the UI page are prefixed `ui_` and return their payload as `output` so
the page can render it directly.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Optional

import plugin_log as log
from audit import AuditLog
from candidates import build_groups
from config_schema import (
    CODEC_ALIASES,
    DEFAULTS,
    KNOWN_AUDIO_CODECS,
    KNOWN_VIDEO_CODECS,
    VALID_METADATA_POLICIES,
    VALID_RANK_KEYS,
    VALID_TIE_BREAKERS,
    parse_config,
)
from executor import ExecutionAborted, Executor
from guard_rails import RealFileSystem
from models import ParseError
from plan_io import (
    latest_plan_path,
    list_plan_runs,
    plan_to_dict,
    prune_plans,
    read_plan,
    write_plan,
)
from planner import build_plan
from report import write_reports
from stash_client import StashClient, StashError

PLUGIN_ID = "CustomizableDuplicateRemover"
KEEP_TAG = "CDR: Keep"
DELETE_TAG = "CDR: Delete"
REVIEW_TAG = "CDR: Review"
MANAGED_TAGS = (KEEP_TAG, DELETE_TAG, REVIEW_TAG)

# Every mode, and whether it may mutate anything. Used by dispatch, so a new mode cannot
# be added without declaring which side of the line it falls on.
READ_ONLY_MODES = ("plan", "clear_tags", "ui_plan", "ui_load", "ui_config")
WRITING_MODES = (
    "execute",
    "plan_execute",
    "ui_execute",
    "ui_save_settings",
    "purge_plans",
)
ALL_MODES = READ_ONLY_MODES + WRITING_MODES

# Settings the page may write. Deliberately excludes confirmDestructive, the run caps,
# protectedPaths, and reportDir: a page request must never be able to widen what a later
# run is permitted to delete.
SAVEABLE_SETTINGS = (
    "rankOrder",
    "codecPreference",
    "audioCodecPreference",
    "tieBreaker",
    "metadataPolicy",
)

# Settings a single request may override without saving them. Strictly the keys the
# page's policy presets and scan controls send — never a safety control, and never
# `reportDir`, which decides where plans are written and which plan gets executed.
OVERRIDABLE_SETTINGS = (
    "rankOrder",
    "codecPreference",
    "audioCodecPreference",
    "tieBreaker",
    "phashDistance",
    "durationDiff",
)


def make_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def resolve_report_dir(config_dir: str, plugin_dir: str) -> str:
    """Default report location, inside the plugin dir so `ui.assets` can serve it."""
    base = plugin_dir or config_dir or os.getcwd()
    return os.path.join(base, "reports")


def _plugin_config(client: StashClient) -> dict:
    """Read the operator's settings for this plugin from Stash."""
    return client.plugin_settings(PLUGIN_ID)


def _apply_overrides(settings: dict, overrides: Optional[dict]) -> dict:
    """Merge per-request setting overrides from the UI page.

    An **allowlist**, not a denylist. Only the policy keys the page's presets actually
    send may be overridden per request; everything else — the destructive gate, both run
    caps, the protected paths, the quality floors, the duration tolerance, and the report
    directory — is settings-only.

    A denylist here was a real vulnerability: excluding just `confirmDestructive` left
    every other guard-rail parameter overridable, so one request could empty
    `protectedPaths`, zero the quality floor, widen `durationTolerance` to an hour, and
    raise both caps, then a second request would execute the resulting plan.

    Rejecting loudly rather than ignoring, so a UI mistake cannot silently run under
    different rails than the operator configured.
    """
    merged = dict(settings)
    for key, value in (overrides or {}).items():
        if key not in OVERRIDABLE_SETTINGS:
            raise ParseError(
                "OVERRIDE_NOT_PERMITTED",
                f"Setting '{key}' cannot be overridden per request; change it in "
                f"Settings -> Plugins. Overridable settings are: "
                f"{', '.join(OVERRIDABLE_SETTINGS)}.",
                {"setting": key, "overridable": list(OVERRIDABLE_SETTINGS)},
            )
        merged[key] = value
    return merged


def _gather_plan(client: StashClient, config, run_id: str, logger, fs):
    """Fetch duplicate groups and plan them. Read-only."""
    version = client.version()
    library_paths = client.library_paths()
    if not library_paths:
        logger.warning(
            "Stash reports no library paths; G_LIBRARY_SCOPE will block every deletion."
        )
    scene_count = client.scene_count()

    logger.info(
        f"querying duplicates (distance={config.phash_distance}, "
        f"duration_diff={config.duration_diff})"
    )
    raw_groups = client.duplicate_groups(config.phash_distance, config.duration_diff)
    groups = build_groups(raw_groups)
    logger.info(f"{len(groups)} duplicate groups with two or more files")

    plan = build_plan(
        run_id, groups, config, library_paths, fs,
        library_file_count=scene_count,
        stash_version=version,
        on_progress=logger.progress,
    )
    return plan


def _tag_plan(client: StashClient, plan, logger) -> dict:
    """Mark keepers and losers so the review can happen in Stash's own UI."""
    keep_ids: set[str] = set()
    delete_ids: set[str] = set()
    review_ids: set[str] = set()

    for group in plan.groups:
        if group.status.value == "AMBIGUOUS":
            review_ids.update(entry.candidate.scene.id for entry in group.candidates)
            continue
        keeper = group.keeper
        if keeper:
            keep_ids.add(keeper.candidate.scene.id)
        for entry in group.losers:
            # A scene keeps its Keep tag if it also holds the keeper file.
            if entry.candidate.scene.id not in keep_ids:
                delete_ids.add(entry.candidate.scene.id)

    applied = {}
    for name, ids in ((KEEP_TAG, keep_ids), (DELETE_TAG, delete_ids), (REVIEW_TAG, review_ids)):
        if not ids:
            continue
        tag_id = client.find_or_create_tag(name)
        client.add_tag(sorted(ids), tag_id)
        applied[name] = len(ids)
        logger.info(f"tagged {len(ids)} scenes with '{name}'")
    return applied


# -- modes -----------------------------------------------------------------

def mode_plan(
    client, config, run_id, logger, fs, *, write_files: bool = True
) -> tuple[dict, Any]:
    plan = _gather_plan(client, config, run_id, logger, fs)

    missing_phash = 0
    try:
        missing_phash = client.scenes_missing_phash()
        if missing_phash:
            logger.warning(
                f"{missing_phash} scenes have no phash and cannot appear in any group"
            )
    except StashError as exc:
        logger.warning(f"could not count scenes missing a phash: {exc.message}")

    result: dict[str, Any] = plan.summary()
    result["missingPhash"] = missing_phash

    if write_files and config.report_dir:
        plan_path = write_plan(plan, os.path.join(config.report_dir, f"plan-{run_id}.json"))
        reports = write_reports(plan, config.report_dir, missing_phash=missing_phash)
        result["planFile"] = plan_path
        result["reportFiles"] = reports
        logger.info(f"plan written to {plan_path}")
        logger.info(f"review report: {reports['html']}")

        pruned = prune_plans(config.report_dir, config.plan_retention)
        if pruned:
            result["prunedPlans"] = pruned
            logger.info(
                f"pruned {len(pruned)} older plan(s), keeping the most recent "
                f"{config.plan_retention}"
            )

    if config.apply_tags:
        try:
            result["tagsApplied"] = _tag_plan(client, plan, logger)
        except StashError as exc:
            logger.warning(f"tagging failed: {exc.message}")
            result["tagsApplied"] = {"error": exc.message}

    if not plan.is_executable:
        logger.warning(f"G_RUN_CAP: {plan.cap_verdict.reason}")

    return result, plan


def _resolve_plan_path(config, requested: Optional[str]) -> str:
    """Locate the plan to execute, refusing anything outside the report directory.

    `planFile` arrives in the request, so without containment it is an arbitrary-path
    read that also decides which plan becomes the deletion authority.
    """
    if not requested:
        return latest_plan_path(config.report_dir)

    resolved = os.path.realpath(str(requested))
    base = os.path.realpath(config.report_dir)
    name = os.path.basename(resolved)
    inside = resolved == base or resolved.startswith(base.rstrip(os.sep) + os.sep)
    if not inside or not (name.startswith("plan-") and name.endswith(".json")):
        raise ParseError(
            "PLAN_OUTSIDE_REPORT_DIR",
            f"planFile must be a plan-*.json inside the configured report directory "
            f"({config.report_dir}).",
            {"path": str(requested)},
        )
    return resolved


def mode_execute(client, config, run_id, logger, fs, args) -> dict:
    plan_path = _resolve_plan_path(config, args.get("planFile"))
    plan = read_plan(plan_path)

    # Bind execution to the plan the operator actually reviewed. Without this the page's
    # delete resolves whichever plan file is newest, so a scheduled Plan task or a second
    # tab writing a plan mid-review silently redirects the deletion to a plan nobody saw.
    expected_run_id = args.get("planRunId")
    if expected_run_id and str(expected_run_id) != plan.run_id:
        raise ParseError(
            "PLAN_RUN_ID_MISMATCH",
            f"The plan on disk is run {plan.run_id}, but the request asked to execute "
            f"run {expected_run_id}. A newer plan was written since this one was "
            f"reviewed. Reload the plan and check it again.",
            {"expected": str(expected_run_id), "found": plan.run_id},
        )

    logger.info(f"executing plan {plan.run_id} from {plan_path}")

    # The fractional cap must divide by the library as it is now, not as the plan file
    # remembers it: a stale denominator makes the proportional cap inert.
    try:
        plan.library_file_count = client.scene_count()
    except StashError as exc:
        logger.warning(f"could not re-read the library size: {exc.message}")

    # The saved plan carries the config it was built with; the destructive gate and the
    # caps are read from live settings so they cannot be bypassed by an old plan file.
    plan.config = config

    # An absent selection means the whole plan; an empty one means nothing. These must
    # not collapse together — a page that sends `[]` is asking for no deletions, and
    # treating that as "everything" would be the worst possible failure mode.
    selection = args.get("selection")
    selected: Optional[set[str]] = None if selection is None else set(selection)
    if selected is not None:
        logger.info(f"restricted to {len(selected)} operator-selected files")
        if not selected:
            logger.warning("selection is empty; nothing will be deleted")

    audit = AuditLog(config.report_dir, run_id)
    audit.run_started("execute", config.as_dict(), plan.stash_version)

    executor = Executor(
        client, config, audit, fs, client.library_paths(), logger=logger
    )
    try:
        result = executor.execute(plan, selection=selected, on_progress=logger.progress)
    except ExecutionAborted as exc:
        audit.run_aborted(exc.code, exc.reason)
        raise

    # Persist the outcome back into the plan and regenerate its reports, so a re-read
    # describes what happened instead of re-presenting deleted files as pending work.
    try:
        write_plan(plan, plan_path)
        write_reports(plan, config.report_dir)
        logger.info(f"plan {plan.run_id} updated with execution results")
    except OSError as exc:
        logger.warning(f"could not write back the executed plan: {exc}")

    summary = result.summary()
    summary["planFile"] = plan_path
    summary["planRunId"] = plan.run_id
    audit.run_finished(summary)

    logger.info(
        f"deleted {result.files_deleted} files, destroyed {result.scenes_destroyed} "
        f"scenes, reclaimed {result.bytes_reclaimed} bytes"
    )
    return summary


def mode_purge_plans(config, args, logger) -> dict:
    """Delete old plan artifacts.

    `keep` defaults to the `planRetention` setting; pass 0 to remove every plan. Never
    touches `audit.jsonl`, which is the record of what was actually deleted and has to
    outlive the plans describing it.
    """
    requested = args.get("keep")
    keep = config.plan_retention if requested is None else int(requested)
    if keep < 0:
        raise ParseError(
            "INVALID_KEEP",
            f"keep must be zero or more; got {keep}.",
            {"keep": keep},
        )

    before = list_plan_runs(config.report_dir)
    removed = prune_plans(config.report_dir, keep)
    remaining = list_plan_runs(config.report_dir)
    logger.info(f"purged {len(removed)} of {len(before)} plans; {len(remaining)} remain")
    return {
        "keep": keep,
        "purged": removed,
        "remaining": remaining,
        "auditLogRetained": True,
    }


def mode_clear_tags(client, config, logger) -> dict:
    cleared = {}
    for name in MANAGED_TAGS:
        try:
            tag_id = client.find_or_create_tag(name)
            scene_ids = client.scenes_with_tag(tag_id)
            if scene_ids:
                client.remove_tag(scene_ids, tag_id)
            cleared[name] = len(scene_ids)
            logger.info(f"cleared '{name}' from {len(scene_ids)} scenes")
        except StashError as exc:
            logger.warning(f"could not clear '{name}': {exc.message}")
            cleared[name] = {"error": exc.message}
    return cleared


def mode_ui_plan(client, config, run_id, logger, fs) -> dict:
    """Full plan payload for the plugin page."""
    _, plan = mode_plan(client, config, run_id, logger, fs, write_files=True)
    return plan_to_dict(plan)


def mode_ui_load(config) -> dict:
    """The most recent plan, without recomputing. Keeps the page responsive."""
    plan_path = latest_plan_path(config.report_dir)
    with open(plan_path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def mode_ui_config(config) -> dict:
    """Live effective settings for the page.

    The page must not read `confirmDestructive` from a plan's stored config: that is a
    snapshot of the settings as they were when the plan was written, so toggling the
    setting afterwards would never be noticed and the delete button would stay disabled
    against a gate that is actually open.
    """
    return {
        "config": config.as_dict(),
        "planAvailable": _plan_available(config),
        # The page renders pickers from this rather than hardcoding the option lists,
        # so adding a rank key or a policy in Python cannot leave the UI stale.
        "schema": {
            "validRankKeys": [k for k in VALID_RANK_KEYS if k != "file_id"],
            "validTieBreakers": list(VALID_TIE_BREAKERS),
            "validMetadataPolicies": list(VALID_METADATA_POLICIES),
            "knownCodecs": list(KNOWN_VIDEO_CODECS),
            "knownAudioCodecs": list(KNOWN_AUDIO_CODECS),
            "codecAliases": dict(CODEC_ALIASES),
            "orderedListSettings": ["rankOrder", "codecPreference", "audioCodecPreference"],
            "enumSettings": {
                "tieBreaker": list(VALID_TIE_BREAKERS),
                "metadataPolicy": list(VALID_METADATA_POLICIES),
            },
            "defaults": dict(DEFAULTS),
        },
    }


def mode_ui_save_settings(client: StashClient, config, args, logger) -> dict:
    """Persist the ranking policy edited on the page.

    Two safety properties matter here:

    - Only the policy fields the page edits are writable. The destructive gate, the run
      caps, and the protected paths are deliberately not, so a page request can never
      widen what a later run is allowed to delete.
    - `configurePlugin` overwrites the whole map, so the current settings are read first
      and the requested changes layered on top. Sending only the changed keys would drop
      every other setting back to its default.
    """
    requested = args.get("settings")
    if not isinstance(requested, dict):
        raise ParseError(
            "SETTINGS_NOT_AN_OBJECT",
            "Expected 'settings' to be an object of setting name to value.",
            {"received": type(requested).__name__},
        )

    rejected = sorted(set(requested) - set(SAVEABLE_SETTINGS))
    if rejected:
        raise ParseError(
            "SETTING_NOT_WRITABLE",
            f"These settings cannot be changed from the page: {', '.join(rejected)}. "
            f"Writable settings are: {', '.join(SAVEABLE_SETTINGS)}. Change the rest in "
            f"Settings -> Plugins.",
            {"rejected": rejected, "writable": list(SAVEABLE_SETTINGS)},
        )

    current = _plugin_config(client)
    merged = dict(current)
    merged.update(requested)

    # Validate before writing. An invalid rank key must never reach stored settings,
    # because every later run would then abort until someone edited the YAML by hand.
    validated = parse_config(merged, default_report_dir=config.report_dir)

    client.configure_plugin(PLUGIN_ID, merged)
    logger.info(f"saved settings: {', '.join(sorted(requested))}")

    return {
        "config": validated.as_dict(),
        "saved": sorted(requested),
        "planAvailable": _plan_available(validated),
    }


def _plan_available(config) -> bool:
    try:
        latest_plan_path(config.report_dir)
    except ParseError:
        return False
    return True


# -- dispatch --------------------------------------------------------------

def run(payload: dict) -> Any:
    args = payload.get("args") or {}
    mode = str(args.get("mode") or "plan").strip()
    run_id = make_run_id()
    logger = log.Logger(run_id)

    connection = payload.get("server_connection") or {}
    client = StashClient(connection, logger=logger)
    fs = RealFileSystem()

    default_report_dir = resolve_report_dir(
        connection.get("Dir") or "", connection.get("PluginDir") or ""
    )
    raw_settings = _apply_overrides(_plugin_config(client), args.get("overrides"))
    config = parse_config(raw_settings, default_report_dir=default_report_dir)

    if mode not in ALL_MODES:
        raise ParseError(
            "UNKNOWN_MODE",
            f"Unknown mode '{mode}'. Valid modes: {', '.join(ALL_MODES)}.",
            {"mode": mode, "valid": list(ALL_MODES)},
        )

    logger.info(
        f"mode={mode} ({'read-only' if mode in READ_ONLY_MODES else 'may write'}) "
        f"rankOrder={','.join(config.rank_order)}"
    )

    if mode == "plan":
        summary, _ = mode_plan(client, config, run_id, logger, fs)
        return summary
    if mode == "ui_plan":
        return mode_ui_plan(client, config, run_id, logger, fs)
    if mode == "ui_load":
        return mode_ui_load(config)
    if mode == "ui_config":
        return mode_ui_config(config)
    if mode == "ui_save_settings":
        return mode_ui_save_settings(client, config, args, logger)
    if mode == "clear_tags":
        return mode_clear_tags(client, config, logger)
    if mode == "purge_plans":
        return mode_purge_plans(config, args, logger)
    if mode in ("execute", "ui_execute"):
        return mode_execute(client, config, run_id, logger, fs, args)
    if mode == "plan_execute":
        summary, plan = mode_plan(client, config, run_id, logger, fs)
        if not plan.is_executable:
            raise ExecutionAborted("G_RUN_CAP", plan.cap_verdict.reason)
        execution = mode_execute(
            client, config, run_id, logger, fs,
            {"planFile": summary.get("planFile")},
        )
        return {"plan": summary, "execution": execution}

    # Unreachable: every name in ALL_MODES is handled above, and anything else was
    # rejected before the config was even parsed.
    raise ParseError(
        "MODE_NOT_DISPATCHED",
        f"Mode '{mode}' is declared but has no handler.",
        {"mode": mode},
    )


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        json.dump({"error": f"Plugin input is not valid JSON: {exc}"}, sys.stdout)
        return 1

    try:
        output = run(payload)
    except (ParseError, ExecutionAborted) as exc:
        detail = exc.as_dict() if isinstance(exc, ParseError) else {
            "code": exc.code, "message": exc.reason
        }
        log.error(f"{detail['code']}: {detail['message']}")
        json.dump({"error": detail["message"], "output": detail}, sys.stdout)
        return 1
    except StashError as exc:
        log.error(f"{exc.code}: {exc.message}")
        json.dump({"error": exc.message, "output": exc.as_dict()}, sys.stdout)
        return 1
    except Exception as exc:
        # Nothing may escape uncaught: an unhandled traceback on stdout would corrupt
        # the result envelope Stash parses.
        log.error(f"unhandled {type(exc).__name__}: {exc}")
        log.debug(traceback.format_exc().replace("\n", " | "))
        json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        return 1

    json.dump({"output": output, "error": None}, sys.stdout, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
