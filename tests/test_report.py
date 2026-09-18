"""Report rendering: self-contained, escaped, and honest about what it will do."""

import csv
import io
import re

import pytest
from conftest import FakeFileSystem, make_file, make_scene

from config_schema import parse_config
from models import Candidate
from planner import build_plan
from report import human_bytes, human_duration, render_csv, render_html, write_reports

LIBRARY = ["/media/library"]


def plan_with(config, *, keeper_path="/media/library/a.mkv", loser_path="/media/library/b.mkv"):
    keeper = Candidate(
        scene=make_scene("10", file_ids=["1"]),
        file=make_file("1", path=keeper_path, codec="hevc", bit_rate=4_000_000),
    )
    loser = Candidate(
        scene=make_scene("20", file_ids=["2"]),
        file=make_file("2", path=loser_path, codec="h264", bit_rate=8_000_000,
                       width=3840, height=2160, size=8_000_000_000),
    )
    fs = FakeFileSystem({keeper.file.path: keeper.file.size, loser.file.path: loser.file.size})
    return build_plan("run1", [[keeper, loser]], config, LIBRARY, fs,
                      library_file_count=500, stash_version="0.28.0")


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0 B"), (512, "512 B"), (1024, "1.00 KiB"), (1536, "1.50 KiB"),
     (1024 ** 3, "1.00 GiB"), (2 * 1024 ** 4, "2.00 TiB")],
)
def test_human_bytes(value, expected):
    assert human_bytes(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0:00"), (59, "0:59"), (60, "1:00"), (90, "1:30"), (3600, "1:00:00"),
     (3661, "1:01:01")],
)
def test_human_duration(value, expected):
    assert human_duration(value) == expected


def test_html_has_no_external_references(config):
    """A strict rule: the report must open from file:// with no network at all."""
    html = render_html(plan_with(config))

    assert "http://" not in html
    assert "https://" not in html
    assert not re.search(r"<script", html, re.IGNORECASE)
    assert not re.search(r'<link[^>]+rel=["\']?stylesheet', html, re.IGNORECASE)
    assert not re.search(r"""(src|href)=["']//""", html)


def test_html_styles_both_themes(config):
    html = render_html(plan_with(config))

    assert "prefers-color-scheme: dark" in html
    assert 'data-theme="dark"' in html
    assert 'data-theme="light"' in html


def test_html_conveys_status_as_text_not_only_colour(config):
    html = render_html(plan_with(config))

    assert ">KEEP<" in html
    assert ">DELETE<" in html
    assert "Resolved" in html


def test_html_escapes_paths(config):
    """A crafted filename must not become markup."""
    hostile = "/media/library/<script>alert('x')</script>.mkv"
    html = render_html(plan_with(config, loser_path=hostile))

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_html_warns_when_confirm_destructive_is_off(config):
    html = render_html(plan_with(config))

    assert "confirmDestructive" in html
    assert "will delete" in html


def test_html_omits_the_warning_when_confirm_is_on():
    cfg = parse_config({"confirmDestructive": True})
    html = render_html(plan_with(cfg))

    assert "delete\n nothing" not in html
    assert "Turn it on" not in html


def test_html_warns_when_the_plan_cannot_be_executed():
    cfg = parse_config({"maxDeletionsPerRun": 0})
    html = render_html(plan_with(cfg))

    assert "cannot be executed" in html
    assert "maxDeletionsPerRun" in html


def test_html_reports_scenes_missing_a_phash(config):
    html = render_html(plan_with(config), missing_phash=42)

    assert "42 scenes have no phash" in html


def test_html_states_the_ranking_policy(config):
    html = render_html(plan_with(config))

    assert "codec,resolution,bitrate,size,age" in html
    assert "tie breaker" in html


def test_html_wide_tables_scroll_in_their_own_container(config):
    html = render_html(plan_with(config))

    assert "overflow-x: auto" in html
    assert 'class="scroll"' in html


def test_html_handles_an_empty_plan(config):
    plan = build_plan("run1", [], config, LIBRARY, FakeFileSystem())

    html = render_html(plan)

    assert "No duplicate groups" in html


def test_csv_has_one_row_per_candidate(config):
    rows = list(csv.DictReader(io.StringIO(render_csv(plan_with(config)))))

    assert len(rows) == 2
    assert {r["disposition"] for r in rows} == {"KEEP", "DELETE"}
    keep = [r for r in rows if r["disposition"] == "KEEP"][0]
    assert keep["video_codec"] == "hevc"
    assert keep["file_id"] == "1"


def test_csv_records_the_deciding_key(config):
    rows = list(csv.DictReader(io.StringIO(render_csv(plan_with(config)))))

    assert all(r["deciding_key"] == "codec" for r in rows)


def test_csv_records_blocked_rails():
    cfg = parse_config({"protectedPaths": "/media/library/b.mkv"})
    rows = list(csv.DictReader(io.StringIO(render_csv(plan_with(cfg)))))

    blocked = [r for r in rows if r["blocked_by"]][0]
    assert "G_PROTECTED_PATHS" in blocked["blocked_by"]
    assert blocked["disposition"] == "BLOCKED"


def test_csv_quotes_paths_containing_commas(config):
    rows = list(csv.DictReader(io.StringIO(
        render_csv(plan_with(config, loser_path="/media/library/a,b,c.mkv"))
    )))

    assert any(r["path"] == "/media/library/a,b,c.mkv" for r in rows)


def test_write_reports_creates_both_files(tmp_path, config):
    paths = write_reports(plan_with(config), str(tmp_path))

    assert paths["html"].endswith("plan-run1.html")
    assert paths["csv"].endswith("plan-run1.csv")
    with open(paths["html"], encoding="utf-8") as stream:
        assert "Duplicate removal plan" in stream.read()


def test_reports_are_deterministic(tmp_path, config):
    """Same plan, same bytes — so a diff between runs means a real change."""
    plan = plan_with(config)

    assert render_html(plan) == render_html(plan)
    assert render_csv(plan) == render_csv(plan)


def test_html_is_a_complete_document(config):
    """Opened over file://, so it needs a doctype, a language, and an explicit charset."""
    html_out = render_html(plan_with(config))

    assert html_out.startswith("<!doctype html>")
    assert '<html lang="en">' in html_out
    assert '<meta charset="utf-8">' in html_out
    assert '<meta name="viewport"' in html_out
    assert html_out.rstrip().endswith("</html>")


def test_html_tables_are_reachable_and_labelled(config):
    html_out = render_html(plan_with(config))

    assert 'tabindex="0"' in html_out
    assert 'role="region"' in html_out
    assert "<caption>" in html_out
    assert 'scope="col"' in html_out


def test_html_body_does_not_hide_overflow(config):
    """overflow-x: hidden on body removes the only recovery from any overflow."""
    assert "overflow-x: hidden" not in render_html(plan_with(config))


@pytest.mark.parametrize(
    "value,expected",
    [(1024 ** 4, "1.00 TiB"), (1024 ** 5, "1024.00 TiB"), (1024 ** 6, "1048576.00 TiB")],
)
def test_human_bytes_saturates_at_tib(value, expected):
    """Beyond TiB it keeps counting in TiB rather than falling off the unit list."""
    assert human_bytes(value) == expected
