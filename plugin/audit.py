"""Append-only audit log.

Every destructive operation is recorded before the next one begins, and each record is
flushed to disk. An interrupted run therefore leaves a log describing exactly the
operations that completed — never one that claims work it did not do.

The file is only ever appended to. Nothing in the plugin rewrites or truncates it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from models import Action, Candidate

AUDIT_FILENAME = "audit.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    def __init__(self, directory: str, run_id: str, *, filename: str = AUDIT_FILENAME):
        self.run_id = run_id
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, filename)

    def _append(self, record: dict) -> None:
        record = {"runId": self.run_id, "timestamp": _now(), **record}
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        # Opened per record so an abrupt process death cannot lose a buffered write.
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def run_started(self, mode: str, config: dict, stash_version: str) -> None:
        self._append(
            {
                "event": "run_started",
                "mode": mode,
                "stashVersion": stash_version,
                "config": config,
            }
        )

    def run_finished(self, summary: dict) -> None:
        self._append({"event": "run_finished", "summary": summary})

    def run_aborted(self, code: str, reason: str) -> None:
        self._append({"event": "run_aborted", "code": code, "reason": reason})

    def operation(
        self,
        *,
        group_index: int,
        action: Action,
        loser: Candidate,
        keeper: Optional[Candidate],
        deciding_key: str,
        verdicts: list,
        result: str,
        detail: str = "",
    ) -> None:
        """One destructive operation, with the evidence behind it.

        Records the keeper alongside the loser so the log answers the only question that
        matters after the fact: what was deleted, and what survived in its place.
        """
        self._append(
            {
                "event": "operation",
                "groupIndex": group_index,
                "action": action.value,
                "result": result,
                "detail": detail,
                "decidingKey": deciding_key,
                "deleted": {
                    "sceneId": loser.scene.id,
                    "fileId": loser.file.id,
                    "path": loser.file.path,
                    "size": loser.file.size,
                    "videoCodec": loser.file.video_codec,
                    "resolution": f"{loser.file.width}x{loser.file.height}",
                    "phash": loser.file.phash,
                },
                "kept": (
                    {
                        "sceneId": keeper.scene.id,
                        "fileId": keeper.file.id,
                        "path": keeper.file.path,
                        "size": keeper.file.size,
                        "videoCodec": keeper.file.video_codec,
                        "resolution": f"{keeper.file.width}x{keeper.file.height}",
                        "phash": keeper.file.phash,
                    }
                    if keeper
                    else None
                ),
                "guardRails": [
                    {"rail": v.rail, "allowed": v.allowed, "reason": v.reason}
                    for v in verdicts
                ],
            }
        )

    def group_skipped(self, group_index: int, status: str, reason: str) -> None:
        self._append(
            {
                "event": "group_skipped",
                "groupIndex": group_index,
                "status": status,
                "reason": reason,
            }
        )
