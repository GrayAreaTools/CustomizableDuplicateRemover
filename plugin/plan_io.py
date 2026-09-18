"""Plan serialisation.

The plan file is the contract between the Plan task, the operator's review, and the
Execute task — and between the Python backend and the UI page. Its shape is versioned
so a future change cannot be misread by an older executor.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from config_schema import parse_config
from models import (
    Action,
    Candidate,
    CandidatePlan,
    FileInfo,
    GroupPlan,
    GroupStatus,
    ParseError,
    SceneInfo,
    Verdict,
)
from planner import Plan

PLAN_FORMAT_VERSION = 1


def _file_to_dict(file: FileInfo) -> dict:
    return {
        "id": file.id,
        "path": file.path,
        "basename": file.basename,
        "size": file.size,
        "modTime": file.mod_time,
        "width": file.width,
        "height": file.height,
        "duration": file.duration,
        "videoCodec": file.video_codec,
        "audioCodec": file.audio_codec,
        "frameRate": file.frame_rate,
        "bitRate": file.bit_rate,
        "phash": file.phash,
        "bitsPerPixelFrame": round(file.bits_per_pixel_frame, 6),
    }


def _scene_to_dict(scene: SceneInfo) -> dict:
    return {
        "id": scene.id,
        "title": scene.title,
        "organized": scene.organized,
        "oCounter": scene.o_counter,
        "rating100": scene.rating100,
        "tagIds": sorted(scene.tag_ids),
        "performerIds": sorted(scene.performer_ids),
        "urls": sorted(scene.urls),
        "markerCount": scene.marker_count,
        "playCount": scene.play_count,
        "fileIds": list(scene.file_ids),
        "primaryFileId": scene.primary_file_id,
    }


def _verdict_to_dict(verdict: Verdict) -> dict:
    return {
        "rail": verdict.rail,
        "allowed": verdict.allowed,
        "reason": verdict.reason,
        "context": verdict.context,
    }


def _candidate_to_dict(entry: CandidatePlan) -> dict:
    return {
        "sceneId": entry.candidate.scene.id,
        "fileId": entry.candidate.file.id,
        "isKeeper": entry.is_keeper,
        "action": entry.action.value if entry.action else None,
        "outcome": entry.outcome,
        "scene": _scene_to_dict(entry.candidate.scene),
        "file": _file_to_dict(entry.candidate.file),
        "verdicts": [_verdict_to_dict(v) for v in entry.verdicts],
        "blockedBy": [v.rail for v in entry.blocked_by],
    }


def plan_to_dict(plan: Plan) -> dict:
    return {
        "formatVersion": PLAN_FORMAT_VERSION,
        "runId": plan.run_id,
        "stashVersion": plan.stash_version,
        "summary": plan.summary(),
        "groups": [
            {
                "index": group.index,
                "status": group.status.value,
                "decidingKey": group.deciding_key,
                "reason": group.reason,
                "fingerprint": group.fingerprint,
                "bytesReclaimed": group.bytes_reclaimed,
                "candidates": [_candidate_to_dict(entry) for entry in group.candidates],
            }
            for group in plan.groups
        ],
    }


def write_plan(plan: Plan, path: str) -> str:
    """Write the plan atomically.

    A truncated plan file would be read by the executor as a smaller set of intended
    deletions, so the write goes to a temporary file in the same directory and is
    renamed into place only once fully flushed.
    """
    payload = json.dumps(plan_to_dict(plan), indent=2, sort_keys=False)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp_path = tempfile.mkstemp(dir=directory, prefix=".plan-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return path


def _file_from_dict(raw: dict) -> FileInfo:
    return FileInfo(
        id=str(raw["id"]),
        path=raw["path"],
        basename=raw.get("basename", ""),
        size=int(raw.get("size") or 0),
        mod_time=float(raw.get("modTime") or 0.0),
        width=int(raw.get("width") or 0),
        height=int(raw.get("height") or 0),
        duration=float(raw.get("duration") or 0.0),
        video_codec=raw.get("videoCodec") or "",
        audio_codec=raw.get("audioCodec") or "",
        frame_rate=float(raw.get("frameRate") or 0.0),
        bit_rate=int(raw.get("bitRate") or 0),
        phash=raw.get("phash"),
    )


def _scene_from_dict(raw: dict) -> SceneInfo:
    return SceneInfo(
        id=str(raw["id"]),
        title=raw.get("title") or "",
        organized=bool(raw.get("organized")),
        o_counter=int(raw.get("oCounter") or 0),
        rating100=raw.get("rating100"),
        tag_ids=frozenset(str(t) for t in raw.get("tagIds") or []),
        performer_ids=frozenset(str(p) for p in raw.get("performerIds") or []),
        urls=frozenset(raw.get("urls") or []),
        marker_count=int(raw.get("markerCount") or 0),
        play_count=int(raw.get("playCount") or 0),
        file_ids=tuple(str(f) for f in raw.get("fileIds") or []),
        primary_file_id=raw.get("primaryFileId"),
    )


def plan_from_dict(raw: dict) -> Plan:
    """Rebuild a `Plan` from its serialised form.

    Rejects an unknown `formatVersion` rather than guessing, because misreading a plan
    means deleting the wrong files.
    """
    version = raw.get("formatVersion")
    if version != PLAN_FORMAT_VERSION:
        raise ParseError(
            "PLAN_FORMAT_UNSUPPORTED",
            f"Plan formatVersion is {version!r}; this build reads version "
            f"{PLAN_FORMAT_VERSION}. Re-run the Plan task.",
            {"found": version, "expected": PLAN_FORMAT_VERSION},
        )

    summary = raw.get("summary") or {}
    config = parse_config(_config_from_summary(summary.get("config") or {}))

    groups: list[GroupPlan] = []
    for raw_group in raw.get("groups") or []:
        entries: list[CandidatePlan] = []
        for raw_entry in raw_group.get("candidates") or []:
            candidate = Candidate(
                scene=_scene_from_dict(raw_entry["scene"]),
                file=_file_from_dict(raw_entry["file"]),
            )
            action_name = raw_entry.get("action")
            entries.append(
                CandidatePlan(
                    candidate=candidate,
                    is_keeper=bool(raw_entry.get("isKeeper")),
                    action=Action(action_name) if action_name else None,
                    outcome=raw_entry.get("outcome"),
                    verdicts=[
                        Verdict(
                            rail=v["rail"],
                            allowed=bool(v["allowed"]),
                            reason=v.get("reason", ""),
                            context=v.get("context") or {},
                        )
                        for v in raw_entry.get("verdicts") or []
                    ],
                )
            )
        groups.append(
            GroupPlan(
                index=int(raw_group.get("index", 0)),
                status=GroupStatus(raw_group.get("status", GroupStatus.SKIPPED.value)),
                candidates=entries,
                deciding_key=raw_group.get("decidingKey", ""),
                reason=raw_group.get("reason", ""),
                fingerprint=raw_group.get("fingerprint", ""),
            )
        )

    cap = summary.get("runCap") or {}
    return Plan(
        run_id=str(raw.get("runId") or ""),
        config=config,
        groups=groups,
        library_file_count=int(summary.get("libraryFileCount") or 0),
        cap_verdict=Verdict(
            rail="G_RUN_CAP",
            allowed=bool(cap.get("allowed", True)),
            reason=cap.get("reason", ""),
        ),
        stash_version=str(raw.get("stashVersion") or ""),
    )


def _config_from_summary(raw: dict) -> dict[str, Any]:
    """Convert a serialised config back into the raw-settings shape `parse_config` reads."""
    converted = dict(raw)
    for key in ("rankOrder", "codecPreference", "audioCodecPreference", "protectedPaths",
                "preferredPaths"):
        value = converted.get(key)
        if isinstance(value, list):
            converted[key] = ",".join(value)
    return converted


def read_plan(path: str) -> Plan:
    if not os.path.exists(path):
        raise ParseError("PLAN_NOT_FOUND", f"No plan file at {path}.", {"path": path})
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except json.JSONDecodeError as exc:
        raise ParseError(
            "PLAN_MALFORMED", f"Plan file at {path} is not valid JSON: {exc}", {"path": path}
        ) from None
    return plan_from_dict(raw)


def latest_plan_path(report_dir: str) -> str:
    """The most recent plan in `report_dir`, by modification time."""
    if not os.path.isdir(report_dir):
        raise ParseError(
            "REPORT_DIR_MISSING",
            f"Report directory does not exist: {report_dir}. Run the Plan task first.",
            {"path": report_dir},
        )
    plans = [
        os.path.join(report_dir, name)
        for name in os.listdir(report_dir)
        if name.startswith("plan-") and name.endswith(".json")
    ]
    if not plans:
        raise ParseError(
            "NO_PLAN_AVAILABLE",
            f"No plan files found in {report_dir}. Run the Plan task first.",
            {"path": report_dir},
        )
    return max(plans, key=os.path.getmtime)


PLAN_SUFFIXES = (".json", ".html", ".csv")


def list_plan_runs(report_dir: str) -> list[str]:
    """Run ids of every plan in the directory, oldest first.

    Ordered by the run id itself, which is a UTC timestamp, rather than by mtime — an
    executed plan is rewritten in place, so mtime no longer reflects when it was built.
    """
    if not os.path.isdir(report_dir):
        return []
    runs = {
        name[len("plan-"):-len(".json")]
        for name in os.listdir(report_dir)
        if name.startswith("plan-") and name.endswith(".json")
    }
    return sorted(runs)


def prune_plans(report_dir: str, keep: int) -> list[str]:
    """Delete all but the `keep` most recent plans and their reports.

    Plan artifacts accumulate without bound otherwise — each carries the absolute path of
    every file in every duplicate group, so they are both large and worth not keeping
    forever. `audit.jsonl` is never touched: it is the forensic record of what was
    deleted and must outlive the plans it describes.
    """
    if keep < 0:
        return []
    runs = list_plan_runs(report_dir)
    doomed = runs if keep == 0 else runs[:-keep]
    removed: list[str] = []
    for run_id in doomed:
        for suffix in PLAN_SUFFIXES:
            path = os.path.join(report_dir, f"plan-{run_id}{suffix}")
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError:
                continue
        removed.append(run_id)
    return removed
