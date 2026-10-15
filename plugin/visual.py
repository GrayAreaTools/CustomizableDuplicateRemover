"""Visual comparison for `G_VISUAL_MATCH`, using the ffmpeg that ships with Stash.

Bitrate (`G_QUALITY_FLOOR`) and duration (`G_MIN_DURATION`) are proxies for "these two
files show the same thing at acceptable quality". This module measures it: both files
are decoded at the same timestamps, scaled to one small grey frame size, and compared
with ffmpeg's `ssim` filter. The score is the lowest SSIM across the samples, so one
mismatched section is enough to fail a pair.

No Python dependencies: ffmpeg does the decoding and the maths, and only its log
output is parsed here.
"""

from __future__ import annotations

import os
import re
import statistics
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from models import FileInfo

_SSIM_ALL = re.compile(r"\bAll:([0-9.]+)")
_LUMA_LOW = re.compile(r"lavfi\.signalstats\.YLOW=([0-9.]+)")
_LUMA_HIGH = re.compile(r"lavfi\.signalstats\.YHIGH=([0-9.]+)")


@dataclass(frozen=True)
class VisualResult:
    """`score` is None when the pair could not be judged; `reason` then says why."""

    score: Optional[float]
    reason: str = ""
    samples: tuple[float, ...] = ()
    mode: str = ""


class VisualProbe(Protocol):
    def compare(self, a: FileInfo, b: FileInfo) -> VisualResult: ...


# (command, timeout) -> (returncode, stderr). Injected so tests never run ffmpeg.
Runner = Callable[[list[str], float], tuple[int, str]]


def _lower_priority() -> None:
    # Planning can compare thousands of pairs on a server that is also transcoding.
    os.nice(19)


def run_ffmpeg(command: list[str], timeout: float) -> tuple[int, str]:
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False,
        preexec_fn=_lower_priority,
    )
    return completed.returncode, completed.stderr


class FfmpegProbe:
    """Compares two files with ffmpeg's `ssim` filter.

    Clips shorter than `whole_clip_under` seconds are compared over their entire length
    at a higher frame rate: they are cheap to decode, and a few sampled seconds would
    not be enough evidence. Longer files are sampled at `samples` evenly spaced points.
    """

    def __init__(
        self,
        ffmpeg: str,
        *,
        runner: Runner = run_ffmpeg,
        timeout: float = 120.0,
        samples: int = 5,
        segment_seconds: float = 2.0,
        whole_clip_under: float = 20.0,
        frame_width: int = 256,
        aspect_tolerance: float = 0.03,
        min_luma_range: float = 24.0,
    ):
        self.ffmpeg = ffmpeg
        self.runner = runner
        self.timeout = timeout
        self.samples = samples
        self.segment_seconds = segment_seconds
        self.whole_clip_under = whole_clip_under
        self.frame_width = frame_width
        self.aspect_tolerance = aspect_tolerance
        self.min_luma_range = min_luma_range

    def compare(self, a: FileInfo, b: FileInfo) -> VisualResult:
        duration = min(a.duration, b.duration)
        if duration <= 0:
            return VisualResult(None, "a file has no known duration")
        if min(a.width, a.height, b.width, b.height) <= 0:
            return VisualResult(None, "a file has no known frame size")

        aspect_a, aspect_b = a.width / a.height, b.width / b.height
        if abs(aspect_a - aspect_b) / max(aspect_a, aspect_b) > self.aspect_tolerance:
            # Scaling both to one size would distort one of them, and SSIM would then
            # read a crop or letterbox difference as different content.
            return VisualResult(
                None,
                f"aspect ratios differ ({a.width}x{a.height} vs {b.width}x{b.height}); "
                "frames cannot be compared at a common size",
            )
        height = max(16, 2 * round(self.frame_width / aspect_a / 2))

        if duration < self.whole_clip_under:
            windows = [(0.0, duration)]
            fps, mode = 10, "whole-clip"
        else:
            points = [(i + 1) / (self.samples + 1) for i in range(self.samples)]
            windows = [(duration * p, self.segment_seconds) for p in points]
            fps, mode = 5, "sampled"

        scores: list[float] = []
        luma_ranges: list[float] = []
        for start, length in windows:
            command = self.command(a.path, b.path, start, length, fps, height)
            try:
                code, log = self.runner(command, self.timeout)
            except subprocess.TimeoutExpired:
                return VisualResult(None, f"ffmpeg timed out after {self.timeout:.0f}s", mode=mode)
            except OSError as exc:
                return VisualResult(None, f"ffmpeg could not run: {exc}", mode=mode)
            found = _SSIM_ALL.findall(log)
            if code != 0 or not found:
                return VisualResult(
                    None, f"ffmpeg reported no SSIM (exit code {code})", mode=mode
                )
            scores.append(float(found[-1]))
            lows = [float(x) for x in _LUMA_LOW.findall(log)]
            highs = [float(x) for x in _LUMA_HIGH.findall(log)]
            luma_ranges.extend(h - lo for lo, h in zip(lows, highs))

        if luma_ranges and statistics.median(luma_ranges) < self.min_luma_range:
            # Black frames and plain title cards score near 1.0 against each other, so
            # a high SSIM here is not evidence the two clips are the same.
            return VisualResult(
                None,
                "frames are nearly uniform (black or a plain card); a match would not "
                "show the clips are the same",
                tuple(scores), mode,
            )
        return VisualResult(min(scores), "", tuple(scores), mode)

    def command(
        self, path_a: str, path_b: str, start: float, length: float, fps: int, height: int,
    ) -> list[str]:
        size = f"{self.frame_width}:{height}"
        graph = (
            f"[0:v]fps={fps},scale={size}:flags=bicubic,format=gray,split[a][stats];"
            "[stats]signalstats,metadata=mode=print[statsout];"
            f"[1:v]fps={fps},scale={size}:flags=bicubic,format=gray[b];"
            "[a][b]ssim[ssimout]"
        )
        seek = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
        return [
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info",
            *seek, "-i", path_a,
            *seek, "-i", path_b,
            "-filter_complex", graph,
            "-map", "[ssimout]", "-f", "null", "-",
            "-map", "[statsout]", "-f", "null", "-",
        ]
