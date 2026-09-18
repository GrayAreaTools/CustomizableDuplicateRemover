"""Turn Stash's duplicate groups into `Candidate` lists.

`findDuplicateScenes` returns groups of scenes, but a scene can hold several files.
Flattening to `(scene, file)` pairs here is what lets the rest of the plugin treat a
two-file scene correctly instead of silently ranking `files[0]`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from models import Candidate, FileInfo, ParseError, SceneInfo

# Requested from Stash for every duplicate group. Kept in one place so the fields the
# ranking and guard rails rely on cannot drift apart from what is fetched.
SCENE_FRAGMENT = """
id
title
organized
o_counter
rating100
play_count
urls
tags { id }
performers { id }
scene_markers { id }
files {
  id
  path
  basename
  size
  mod_time
  width
  height
  duration
  video_codec
  audio_codec
  frame_rate
  bit_rate
  fingerprints { type value }
}
"""


def _as_int(value: Any, field: str, *, default: int = 0) -> int:
    """Coerce a GraphQL scalar to int.

    `size` is the `Int64` scalar and arrives as a string from some clients, so this
    never assumes the JSON type.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            raise ParseError(
                "FIELD_NOT_AN_INTEGER",
                f"Field '{field}' is {value!r}, which is not an integer.",
                {"field": field, "value": str(value)},
            ) from None


def _as_float(value: Any, field: str, *, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ParseError(
            "FIELD_NOT_A_NUMBER",
            f"Field '{field}' is {value!r}, which is not a number.",
            {"field": field, "value": str(value)},
        ) from None


def parse_mod_time(value: Any) -> float:
    """Stash `Time` is RFC 3339. Returns epoch seconds; 0.0 when absent or unparseable.

    A missing mod time makes the `age` rank key useless for that file but must not
    abort the run, so it degrades to 0.0 (treated as oldest) rather than raising.
    """
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    # Python's fromisoformat rejects the trailing Z before 3.11.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _phash_of(file_dict: dict) -> Optional[str]:
    for fingerprint in file_dict.get("fingerprints") or []:
        if (fingerprint or {}).get("type") == "phash":
            return str(fingerprint.get("value"))
    return None


def parse_file(raw: dict) -> FileInfo:
    file_id = str(raw.get("id") or "")
    if not file_id:
        raise ParseError("FILE_MISSING_ID", "A file in the response has no id.", {"raw": str(raw)[:200]})
    path = raw.get("path") or ""
    if not path:
        raise ParseError(
            "FILE_MISSING_PATH",
            f"File {file_id} has no path; refusing to consider it for deletion.",
            {"file_id": file_id},
        )
    return FileInfo(
        id=file_id,
        path=path,
        basename=raw.get("basename") or path.rsplit("/", 1)[-1],
        size=_as_int(raw.get("size"), f"file[{file_id}].size"),
        mod_time=parse_mod_time(raw.get("mod_time")),
        width=_as_int(raw.get("width"), f"file[{file_id}].width"),
        height=_as_int(raw.get("height"), f"file[{file_id}].height"),
        duration=_as_float(raw.get("duration"), f"file[{file_id}].duration"),
        video_codec=(raw.get("video_codec") or "").strip(),
        audio_codec=(raw.get("audio_codec") or "").strip(),
        frame_rate=_as_float(raw.get("frame_rate"), f"file[{file_id}].frame_rate"),
        bit_rate=_as_int(raw.get("bit_rate"), f"file[{file_id}].bit_rate"),
        phash=_phash_of(raw),
    )


def _ids(entries: Any) -> frozenset[str]:
    return frozenset(str(entry["id"]) for entry in (entries or []) if entry and entry.get("id"))


def parse_scene(raw: dict) -> tuple[SceneInfo, list[FileInfo]]:
    scene_id = str(raw.get("id") or "")
    if not scene_id:
        raise ParseError("SCENE_MISSING_ID", "A scene in the response has no id.")

    files = [parse_file(entry) for entry in (raw.get("files") or []) if entry]
    file_ids = tuple(f.id for f in files)

    # Stash exposes the primary file as the first element of `files`. There is no
    # `primary_file_id` field on the Scene type, so position is the only signal.
    primary = file_ids[0] if file_ids else None

    scene = SceneInfo(
        id=scene_id,
        title=raw.get("title") or "",
        organized=bool(raw.get("organized")),
        o_counter=_as_int(raw.get("o_counter"), f"scene[{scene_id}].o_counter"),
        rating100=(
            _as_int(raw.get("rating100"), f"scene[{scene_id}].rating100")
            if raw.get("rating100") is not None
            else None
        ),
        tag_ids=_ids(raw.get("tags")),
        performer_ids=_ids(raw.get("performers")),
        urls=frozenset(str(u) for u in (raw.get("urls") or []) if u),
        marker_count=len(raw.get("scene_markers") or []),
        play_count=_as_int(raw.get("play_count"), f"scene[{scene_id}].play_count"),
        file_ids=file_ids,
        primary_file_id=primary,
    )
    return scene, files


def build_group(raw_scenes: Iterable[dict]) -> list[Candidate]:
    """Flatten one duplicate group into candidates.

    Scenes with no files are dropped: they cannot be ranked and there is nothing on
    disk to delete.
    """
    candidates: list[Candidate] = []
    for raw_scene in raw_scenes:
        if not raw_scene:
            continue
        scene, files = parse_scene(raw_scene)
        for file in files:
            candidates.append(Candidate(scene=scene, file=file))
    return candidates


def build_groups(raw_groups: Iterable[Iterable[dict]]) -> list[list[Candidate]]:
    """Flatten the `[[Scene!]!]!` response.

    Groups that collapse to fewer than two candidates are dropped — a group of one is
    not a duplicate and carries no decision to make.
    """
    groups = []
    for raw_group in raw_groups or []:
        group = build_group(raw_group or [])
        if len(group) >= 2:
            groups.append(group)
    return groups
