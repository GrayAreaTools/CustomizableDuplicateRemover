"""G_VISUAL_MATCH and the ffmpeg probe behind it.

The probe is exercised against a fake runner that returns canned ffmpeg logs, so these
tests never decode video. The log lines match what ffmpeg 7 prints for the ssim filter
and for signalstats via metadata=mode=print; the parser was also run against real ffmpeg
inside the Stash container during development.
"""

import subprocess

import pytest
from conftest import FakeFileSystem, make_candidate, make_file

from config_schema import parse_config
from guard_rails import VISUALLY_OVERRIDABLE, evaluate_pair, first_block, visual_match
from models import Verdict
from planner import plan_group
from visual import FfmpegProbe, VisualResult

LIBRARY = ["/media/library"]


def ffmpeg_log(ssim, luma_range=(16, 235), frames=3):
    lines = [f"[Parsed_metadata_5 @ 0x1] lavfi.signalstats.YLOW={luma_range[0]}\n"
             f"[Parsed_metadata_5 @ 0x1] lavfi.signalstats.YHIGH={luma_range[1]}"
             for _ in range(frames)]
    lines.append(f"[Parsed_ssim_8 @ 0x2] SSIM Y:{ssim:.6f} (20.1) All:{ssim:.6f} (20.1)")
    return "\n".join(lines)


class FakeRunner:
    """Returns one canned log per call, in order, and records each command."""

    def __init__(self, *results):
        self.results = list(results)
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append(command)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


def probe_with(*results, **kwargs):
    runner = FakeRunner(*results)
    return FfmpegProbe("/usr/bin/ffmpeg", runner=runner, **kwargs), runner


def option_values(command, flag):
    return [command[i + 1] for i, part in enumerate(command) if part == flag]


# FfmpegProbe

def test_probe_samples_long_files_and_reports_the_worst_sample():
    probe, runner = probe_with(*[(0, ffmpeg_log(s)) for s in (0.99, 0.98, 0.75, 0.99, 0.97)])
    a = make_file("1", duration=600.0)
    b = make_file("2", duration=600.0)

    result = probe.compare(a, b)

    assert result.score == pytest.approx(0.75)
    assert result.mode == "sampled"
    assert len(result.samples) == 5
    starts = [float(v) for c in runner.commands for v in option_values(c, "-ss")[:1]]
    assert starts == pytest.approx([100.0, 200.0, 300.0, 400.0, 500.0])


def test_probe_compares_short_clips_over_their_whole_length():
    probe, runner = probe_with((0, ffmpeg_log(0.97)))
    a = make_file("1", duration=8.0)
    b = make_file("2", duration=8.5)

    result = probe.compare(a, b)

    assert result.mode == "whole-clip"
    assert result.score == pytest.approx(0.97)
    assert len(runner.commands) == 1
    command = runner.commands[0]
    assert option_values(command, "-ss") == ["0.000", "0.000"]
    assert option_values(command, "-t") == ["8.000", "8.000"]
    assert "fps=10" in option_values(command, "-filter_complex")[0]


def test_probe_passes_paths_as_single_arguments_without_a_shell():
    """A hostile filename must stay one argv element; nothing is ever shell-parsed."""
    probe, runner = probe_with((0, ffmpeg_log(0.99)))
    hostile = '/media/library/a"; rm -rf / #.mkv'
    a = make_file("1", path=hostile, duration=8.0)
    b = make_file("2", path="/media/library/b c.mkv", duration=8.0)

    probe.compare(a, b)

    assert option_values(runner.commands[0], "-i") == [hostile, "/media/library/b c.mkv"]


def test_probe_keeps_the_aspect_ratio_when_scaling():
    probe, runner = probe_with((0, ffmpeg_log(0.99)))
    portrait_a = make_file("1", width=1080, height=1920, duration=8.0)
    portrait_b = make_file("2", width=720, height=1280, duration=8.0)

    probe.compare(portrait_a, portrait_b)

    assert "scale=256:456" in option_values(runner.commands[0], "-filter_complex")[0]


def test_probe_refuses_to_judge_different_aspect_ratios():
    probe, runner = probe_with((0, ffmpeg_log(0.99)))
    widescreen = make_file("1", width=1920, height=1080)
    four_three = make_file("2", width=720, height=480)

    result = probe.compare(widescreen, four_three)

    assert result.score is None
    assert "aspect ratios differ" in result.reason
    assert runner.commands == []


@pytest.mark.parametrize(
    "a_kwargs,expected",
    [({"duration": 0.0}, "no known duration"), ({"width": 0}, "no known frame size")],
)
def test_probe_refuses_to_judge_missing_metadata(a_kwargs, expected):
    probe, runner = probe_with((0, ffmpeg_log(0.99)))

    result = probe.compare(make_file("1", **a_kwargs), make_file("2"))

    assert result.score is None
    assert expected in result.reason
    assert runner.commands == []


def test_probe_refuses_to_judge_nearly_uniform_frames():
    """Two black or title-card clips score ~1.0 against each other; that is not a match."""
    probe, _ = probe_with((0, ffmpeg_log(0.999, luma_range=(16, 22))))

    result = probe.compare(make_file("1", duration=6.0), make_file("2", duration=6.0))

    assert result.score is None
    assert "nearly uniform" in result.reason
    assert result.samples == (0.999,)


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ((1, "Invalid data found when processing input"), "no SSIM (exit code 1)"),
        ((0, "no ssim line at all"), "no SSIM (exit code 0)"),
        (subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120), "timed out"),
        (FileNotFoundError("ffmpeg"), "could not run"),
    ],
)
def test_probe_failures_are_reported_not_raised(outcome, expected):
    probe, _ = probe_with(outcome)

    result = probe.compare(make_file("1", duration=8.0), make_file("2", duration=8.0))

    assert result.score is None
    assert expected in result.reason


def test_probe_stops_at_the_first_failed_sample():
    probe, runner = probe_with((0, ffmpeg_log(0.99)), (1, "decode error"))

    result = probe.compare(make_file("1", duration=600.0), make_file("2", duration=600.0))

    assert result.score is None
    assert len(runner.commands) == 2


# G_VISUAL_MATCH rail

class FakeProbe:
    def __init__(self, score, reason=""):
        self.result = VisualResult(score, reason, (score,) if score is not None else (), "sampled")
        self.calls = 0

    def compare(self, a, b):
        self.calls += 1
        return self.result


ON = {"visualCheck": True}


def verdict(rail, allowed, reason="", **context):
    return Verdict(rail=rail, allowed=allowed, reason=reason, context=context)


def pair(**loser_kwargs):
    return make_candidate("1", "10"), make_candidate("2", "20", **loser_kwargs)


def test_visual_match_is_inert_when_disabled(config):
    keeper, loser = pair()
    probe = FakeProbe(0.1)

    verdicts = visual_match(keeper, loser, config, probe, [verdict("G_DURATION", True)])

    assert probe.calls == 0
    assert verdicts[-1].rail == "G_VISUAL_MATCH"
    assert verdicts[-1].allowed
    assert verdicts[-1].context == {"enabled": False}


def test_visual_match_confirms_a_clean_pair():
    keeper, loser = pair()

    verdicts = visual_match(keeper, loser, parse_config(ON), FakeProbe(0.97), [])

    assert verdicts[-1].allowed
    assert verdicts[-1].context["ssim"] == 0.97


def test_visual_match_blocks_a_clean_pair_whose_frames_differ():
    keeper, loser = pair()

    verdicts = visual_match(keeper, loser, parse_config(ON), FakeProbe(0.62), [])

    assert not verdicts[-1].allowed
    assert "0.620" in verdicts[-1].reason and "0.900" in verdicts[-1].reason


def test_visual_match_blocks_a_pair_it_cannot_judge():
    keeper, loser = pair()

    verdicts = visual_match(keeper, loser, parse_config(ON), FakeProbe(None, "aspect ratios differ"), [])

    assert not verdicts[-1].allowed
    assert "aspect ratios differ" in verdicts[-1].reason


def test_visual_match_threshold_is_configurable():
    keeper, loser = pair()
    cfg = parse_config({**ON, "minVisualSimilarity": 0.95})

    assert not visual_match(keeper, loser, cfg, FakeProbe(0.94), [])[-1].allowed
    assert visual_match(keeper, loser, cfg, FakeProbe(0.95), [])[-1].allowed


def test_visual_match_clears_a_quality_floor_block():
    """Calibration found blocked pairs at SSIM 0.976 with a bitrate ratio of 0.048."""
    keeper, loser = pair()
    floor = verdict("G_QUALITY_FLOOR", False, "Keeper quality ratio 0.149 ...", ratio=0.149)

    verdicts = visual_match(keeper, loser, parse_config(ON), FakeProbe(0.976), [floor])

    assert first_block(verdicts) is None
    cleared = verdicts[0]
    assert cleared.rail == "G_QUALITY_FLOOR" and cleared.allowed
    assert cleared.context["overridden_by"] == "G_VISUAL_MATCH"
    assert cleared.context["ratio"] == 0.149
    assert "0.149" in cleared.context["overridden_reason"]


def test_visual_mismatch_leaves_the_quality_floor_block_in_place():
    keeper, loser = pair()
    floor = verdict("G_QUALITY_FLOOR", False, "ratio")

    verdicts = visual_match(keeper, loser, parse_config(ON), FakeProbe(0.7), [floor])

    assert [v.rail for v in verdicts if not v.allowed] == ["G_QUALITY_FLOOR", "G_VISUAL_MATCH"]


def test_visual_match_clears_a_short_clip_at_or_above_visual_min_duration():
    keeper = make_candidate("1", "10", duration=8.0)
    loser = make_candidate("2", "20", duration=8.0)
    short = verdict("G_MIN_DURATION", False, "Clip of 8.0s ...", shortest=8.0)

    verdicts = visual_match(keeper, loser, parse_config(ON), FakeProbe(0.95), [short])

    assert first_block(verdicts) is None


def test_visual_match_never_judges_clips_under_visual_min_duration():
    keeper = make_candidate("1", "10", duration=3.0)
    loser = make_candidate("2", "20", duration=3.0)
    short = verdict("G_MIN_DURATION", False, "Clip of 3.0s ...", shortest=3.0)
    probe = FakeProbe(0.99)

    verdicts = visual_match(keeper, loser, parse_config(ON), probe, [short])

    assert probe.calls == 0
    assert first_block(verdicts).rail == "G_MIN_DURATION"
    assert "visualMinDuration" in verdicts[-1].context["skipped"]


def test_visual_min_duration_can_be_lowered_for_very_short_clips():
    keeper = make_candidate("1", "10", duration=3.0)
    loser = make_candidate("2", "20", duration=3.0)
    short = verdict("G_MIN_DURATION", False, "short", shortest=3.0)
    cfg = parse_config({**ON, "visualMinDuration": 2})

    assert first_block(visual_match(keeper, loser, cfg, FakeProbe(0.97), [short])) is None


@pytest.mark.parametrize(
    "rail", ["G_KEEPER_INTACT", "G_LIBRARY_SCOPE", "G_PROTECTED_PATHS", "G_DURATION",
             "G_RESOLUTION", "G_STASH_ID_CONFLICT", "G_METADATA"],
)
def test_visual_match_cannot_clear_other_rails(rail):
    keeper, loser = pair()
    probe = FakeProbe(0.999)

    verdicts = visual_match(keeper, loser, parse_config(ON), probe, [verdict(rail, False, "no")])

    assert probe.calls == 0, "the probe should not run when its answer cannot matter"
    assert first_block(verdicts).rail == rail


def test_only_proxy_rails_are_visually_overridable():
    assert set(VISUALLY_OVERRIDABLE) == {"G_QUALITY_FLOOR", "G_MIN_DURATION"}


def test_evaluate_pair_runs_the_probe_through_every_rail():
    keeper = make_candidate("1", "10", path="/media/library/a.mkv", size=100, duration=8.0)
    loser = make_candidate("2", "20", path="/media/library/b.mkv", duration=8.0)
    fs = FakeFileSystem({keeper.file.path: 100, loser.file.path: 200})
    cfg = parse_config(ON)

    blocked = evaluate_pair(keeper, loser, cfg, LIBRARY, fs, (), FakeProbe(0.4))
    released = evaluate_pair(keeper, loser, cfg, LIBRARY, fs, (), FakeProbe(0.96))

    assert first_block(blocked).rail == "G_MIN_DURATION"
    assert [v.rail for v in blocked if not v.allowed] == ["G_MIN_DURATION", "G_VISUAL_MATCH"]
    assert first_block(released) is None


def test_short_duplicate_clip_group_resolves_with_a_visual_match():
    keeper = make_candidate("1", "10", codec="hevc", duration=12.0, path="/media/library/a.mkv")
    loser = make_candidate("2", "20", codec="h264", duration=12.0, path="/media/library/b.mkv")
    fs = FakeFileSystem({keeper.file.path: keeper.file.size, loser.file.path: loser.file.size})

    without = plan_group(0, [keeper, loser], parse_config({}), LIBRARY, fs)
    with_check = plan_group(0, [keeper, loser], parse_config(ON), LIBRARY, fs, FakeProbe(0.97))

    assert without.status.value == "SKIPPED"
    assert with_check.status.value == "RESOLVED"
    assert with_check.keeper.candidate is keeper
