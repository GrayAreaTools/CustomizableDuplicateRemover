"""Stash's plugin log protocol.

Stash reads stderr and interprets a control prefix: SOH, a level character, then STX.
The level characters are t/d/i/w/e for trace through error, and p for progress, whose
payload is a float in 0..1 driving the job bar in the UI.

stdout is reserved for the single result envelope, so nothing here ever prints there.
"""

from __future__ import annotations

import json
import sys
from typing import Any

SOH = "\x01"
STX = "\x02"


def _emit(level: str, message: str) -> None:
    # Newlines would be read as the start of an unprefixed record at the default level.
    text = str(message).replace("\n", " ").replace("\r", " ")
    sys.stderr.write(f"{SOH}{level}{STX}{text}\n")
    sys.stderr.flush()


def trace(message: Any) -> None:
    _emit("t", message)


def debug(message: Any) -> None:
    _emit("d", message)


def info(message: Any) -> None:
    _emit("i", message)


def warning(message: Any) -> None:
    _emit("w", message)


def error(message: Any) -> None:
    _emit("e", message)


def progress(fraction: float) -> None:
    try:
        value = float(fraction)
    except (TypeError, ValueError):
        return
    _emit("p", str(min(max(value, 0.0), 1.0)))


def structured(level: str, event: str, **fields: Any) -> None:
    """Emit a single-line JSON record, so logs are machine-readable.

    Stash's log pane shows the raw line, which stays readable at this size.
    """
    payload = {"event": event, **fields}
    _emit(level, json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


class Logger:
    """Adapter carrying the run id into every record."""

    def __init__(self, run_id: str = ""):
        self.run_id = run_id

    def _with_run(self, message: Any) -> str:
        return f"[{self.run_id}] {message}" if self.run_id else str(message)

    def debug(self, message: Any) -> None:
        debug(self._with_run(message))

    def info(self, message: Any) -> None:
        info(self._with_run(message))

    def warning(self, message: Any) -> None:
        warning(self._with_run(message))

    def error(self, message: Any) -> None:
        error(self._with_run(message))

    def progress(self, fraction: float) -> None:
        progress(fraction)
