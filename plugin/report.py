"""Review artifacts: a self-contained HTML report and a CSV.

The HTML is built with the standard library only — no template engine and no external
assets, so it opens from a file:// path with no network and passes the plugin's own
dependency rules. Both light and dark themes are styled, status is conveyed by text as
well as colour, and wide tables scroll inside their own container.
"""

from __future__ import annotations

import csv
import html
import io
import os
from typing import Optional

from models import CandidatePlan, GroupPlan, GroupStatus
from planner import Plan

STATUS_LABELS = {
    GroupStatus.RESOLVED: ("Resolved", "resolved"),
    GroupStatus.EXECUTED: ("Applied", "executed"),
    GroupStatus.SKIPPED: ("Skipped", "skipped"),
    GroupStatus.AMBIGUOUS: ("Ambiguous", "ambiguous"),
    GroupStatus.PROTECTED: ("Protected", "protected"),
    GroupStatus.FAILED: ("Failed", "failed"),
    GroupStatus.PARTIAL: ("Partial", "partial"),
}

CSS = """
:root {
  --bg: #ffffff; --fg: #1b1f24; --muted: #5c6570; --border: #d7dce2;
  --panel: #f6f8fa; --keep: #0a6b3d; --keep-bg: #e6f4ec;
  --delete: #96341f; --delete-bg: #fceae5; --block: #6a4a00; --block-bg: #fdf3d7;
  --accent: #0b5fa5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a; --fg: #e6e9ec; --muted: #9aa4af; --border: #2f3740;
    --panel: #1c2126; --keep: #7ee0a8; --keep-bg: #102a1c;
    --delete: #ff9d84; --delete-bg: #341612; --block: #f2d492; --block-bg: #302408;
    --accent: #6cb6ff;
  }
}
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #1b1f24; --muted: #5c6570; --border: #d7dce2;
  --panel: #f6f8fa; --keep: #0a6b3d; --keep-bg: #e6f4ec;
  --delete: #96341f; --delete-bg: #fceae5; --block: #6a4a00; --block-bg: #fdf3d7;
  --accent: #0b5fa5;
}
:root[data-theme="dark"] {
  --bg: #14171a; --fg: #e6e9ec; --muted: #9aa4af; --border: #2f3740;
  --panel: #1c2126; --keep: #7ee0a8; --keep-bg: #102a1c;
  --delete: #ff9d84; --delete-bg: #341612; --block: #f2d492; --block-bg: #302408;
  --accent: #6cb6ff;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem; background: var(--bg); color: var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 0; font-weight: 600; }
.sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }
.totals { display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: 1.5rem; }
.tile {
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: .6rem .9rem; min-width: 8rem;
}
.tile .n { font-size: 1.3rem; font-weight: 650; font-variant-numeric: tabular-nums; }
.tile .k { color: var(--muted); font-size: .75rem; text-transform: uppercase;
  letter-spacing: .04em; }
.note code { overflow-wrap: anywhere; }
.note {
  border: 1px solid var(--border); border-left: 3px solid var(--accent);
  background: var(--panel); border-radius: 6px; padding: .7rem .9rem;
  margin-bottom: 1.5rem; font-size: .9rem;
}
.group {
  border: 1px solid var(--border); border-radius: 8px; margin-bottom: 1rem;
  overflow: hidden;
}
.group > header {
  display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: baseline;
  padding: .7rem .9rem; background: var(--panel); border-bottom: 1px solid var(--border);
}
.group > header .why { color: var(--muted); font-size: .85rem; }
.badge {
  font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
  padding: .15rem .45rem; border-radius: 4px; border: 1px solid currentColor;
}
.badge.resolved { color: var(--keep); background: var(--keep-bg); }
.badge.executed { color: var(--muted); background: var(--panel); }
.badge.skipped, .badge.protected { color: var(--block); background: var(--block-bg); }
.badge.ambiguous { color: var(--block); background: var(--block-bg); }
.badge.failed, .badge.partial { color: var(--delete); background: var(--delete-bg); }
.scroll { overflow-x: auto; }
.scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
caption { text-align: left; padding: .4rem .6rem; color: var(--muted);
  font-size: .78rem; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; min-width: 52rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--border);
  white-space: nowrap; }
th { color: var(--muted); font-weight: 600; font-size: .75rem; text-transform: uppercase;
  letter-spacing: .03em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.path { white-space: normal; word-break: break-all; min-width: 18rem; font-size: .8rem;
  color: var(--muted); }
tr.keep { background: var(--keep-bg); }
tr.remove { background: var(--delete-bg); }
tr.blocked { background: var(--block-bg); }
tr.done { background: var(--panel); opacity: .8; }
tr.done .disp { color: var(--muted); text-decoration: line-through; }
.verdict { color: var(--muted); font-size: .78rem; white-space: normal; }
.disp { font-weight: 700; font-size: .72rem; letter-spacing: .04em; }
tr.keep .disp { color: var(--keep); }
tr.remove .disp { color: var(--delete); }
tr.blocked .disp { color: var(--block); }
footer { color: var(--muted); font-size: .8rem; margin-top: 2rem;
  border-top: 1px solid var(--border); padding-top: .8rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; }
"""


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024


def human_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _disposition(entry: CandidatePlan) -> tuple[str, str]:
    """Row label and CSS class. Text carries the meaning, not colour alone."""
    if entry.outcome == "deleted":
        return "DELETED", "done"
    if entry.is_keeper:
        return "KEEP", "keep"
    if entry.is_blocked:
        return "BLOCKED", "blocked"
    if entry.action:
        return "DELETE", "remove"
    return "—", ""


def _row(entry: CandidatePlan) -> str:
    label, css = _disposition(entry)
    file = entry.candidate.file
    scene = entry.candidate.scene

    if entry.is_blocked:
        detail = "; ".join(f"{v.rail}: {v.reason}" for v in entry.blocked_by)
    elif entry.is_keeper:
        detail = "kept"
    else:
        detail = (entry.action.value if entry.action else "")

    cells = [
        f'<td class="disp">{html.escape(label)}</td>',
        f"<td>{html.escape(file.video_codec or '?')}</td>",
        f"<td>{file.width}&times;{file.height}</td>",
        f'<td class="num">{file.bit_rate // 1000 if file.bit_rate else 0}</td>',
        f'<td class="num">{html.escape(human_bytes(file.size))}</td>',
        f'<td class="num">{html.escape(human_duration(file.duration))}</td>',
        f'<td class="num">{file.frame_rate:.2f}</td>',
        f"<td>{html.escape(scene.id)}/{html.escape(file.id)}</td>",
        f'<td class="path">{html.escape(file.path)}</td>',
        f'<td class="verdict">{html.escape(detail)}</td>',
    ]
    return f'<tr class="{css}">' + "".join(cells) + "</tr>"


def _group_section(group: GroupPlan) -> str:
    label, css = STATUS_LABELS.get(group.status, (group.status.value, ""))
    header = (
        f"<header>"
        f'<h2>Group {group.index}</h2>'
        f'<span class="badge {css}">{html.escape(label)}</span>'
        f'<span class="why">{html.escape(group.reason)}</span>'
        f'<span class="why">deciding key: <code>{html.escape(group.deciding_key or "—")}</code></span>'
        f"</header>"
    )
    head = (
        "<thead><tr>"
        '<th scope="col">Action</th><th scope="col">Codec</th>'
        '<th scope="col">Resolution</th><th scope="col">kb/s</th>'
        '<th scope="col">Size</th><th scope="col">Duration</th><th scope="col">FPS</th>'
        '<th scope="col">Scene/File</th><th scope="col">Path</th>'
        '<th scope="col">Detail</th>'
        "</tr></thead>"
    )
    rows = "".join(_row(entry) for entry in group.candidates)
    return (
        f'<section class="group">{header}'
        # tabindex so the horizontally scrolling region is reachable without a pointer.
        f'<div class="scroll" tabindex="0" role="region" '
        f'aria-label="Group {group.index} candidate files">'
        f"<table><caption>Duplicate group {group.index} &mdash; {html.escape(label)}"
        f"</caption>{head}<tbody>{rows}</tbody></table></div>"
        f"</section>"
    )


def render_html(plan: Plan, *, missing_phash: Optional[int] = None) -> str:
    summary = plan.summary()
    by_status = summary["groupsByStatus"]

    tiles = [
        ("Groups", summary["groups"]),
        ("Resolved", by_status.get(GroupStatus.RESOLVED.value, 0)),
        ("Ambiguous", by_status.get(GroupStatus.AMBIGUOUS.value, 0)),
        ("Skipped", by_status.get(GroupStatus.SKIPPED.value, 0)),
        ("Protected", by_status.get(GroupStatus.PROTECTED.value, 0)),
        ("Files to delete", summary["filesToDelete"]),
        ("Already applied", by_status.get(GroupStatus.EXECUTED.value, 0)),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="n">{value}</div><div class="k">{html.escape(key)}</div></div>'
        for key, value in tiles
    ) + (
        f'<div class="tile"><div class="n">{html.escape(human_bytes(summary["bytesReclaimed"]))}'
        f'</div><div class="k">Reclaimable</div></div>'
    )

    notes = []
    if not plan.is_executable:
        notes.append(
            f"<strong>This plan cannot be executed.</strong> {html.escape(plan.cap_verdict.reason)}"
        )
    if not plan.config.confirm_destructive:
        notes.append(
            "<code>confirmDestructive</code> is off, so the Execute task will delete "
            "nothing. Turn it on in the plugin settings once this report looks correct."
        )
    if missing_phash:
        notes.append(
            f"{missing_phash} scenes have no phash and cannot appear in any group. "
            f"Run Generate with phashes enabled for full coverage."
        )
    notes.append(
        "Ranking: <code>" + html.escape(",".join(plan.config.rank_order)) + "</code> "
        "&middot; codecs: <code>" + html.escape(",".join(plan.config.codec_preference))
        + "</code> &middot; tie breaker: <code>" + html.escape(plan.config.tie_breaker) + "</code>"
    )
    note_html = "".join(f'<div class="note">{note}</div>' for note in notes)

    groups_html = "".join(_group_section(group) for group in plan.groups)
    if not groups_html:
        groups_html = '<div class="note">No duplicate groups were returned.</div>'

    # Explicit doctype and charset: the report is opened over file://, contains
    # filenames from disk, and must not depend on encoding sniffing.
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duplicate plan {html.escape(plan.run_id)}</title>
<style>{CSS}</style>
</head>
<body>
<h1>Duplicate removal plan</h1>
<p class="sub">Run <code>{html.escape(plan.run_id)}</code>
&middot; Stash {html.escape(plan.stash_version or "unknown")}
&middot; nothing has been deleted; this is a proposal</p>
<div class="totals">{tile_html}</div>
{note_html}
{groups_html}
<footer>Generated by CustomizableDuplicateRemover. Review the DELETE rows, then run the
Execute Plan task &mdash; or use the plugin page in Stash to deselect individual files
first.</footer>
</body>
</html>
"""


CSV_COLUMNS = [
    "group_index", "group_status", "deciding_key", "disposition", "scene_id", "file_id",
    "video_codec", "width", "height", "bit_rate", "size_bytes", "duration_seconds",
    "frame_rate", "path", "blocked_by", "detail",
]


def render_csv(plan: Plan) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for group in plan.groups:
        for entry in group.candidates:
            label, _ = _disposition(entry)
            file = entry.candidate.file
            writer.writerow([
                group.index,
                group.status.value,
                group.deciding_key,
                label,
                entry.candidate.scene.id,
                file.id,
                file.video_codec,
                file.width,
                file.height,
                file.bit_rate,
                file.size,
                f"{file.duration:.3f}",
                f"{file.frame_rate:.3f}",
                file.path,
                "|".join(v.rail for v in entry.blocked_by),
                "; ".join(v.reason for v in entry.blocked_by),
            ])
    return buffer.getvalue()


def write_reports(
    plan: Plan, directory: str, *, missing_phash: Optional[int] = None
) -> dict[str, str]:
    os.makedirs(directory, exist_ok=True)
    html_path = os.path.join(directory, f"plan-{plan.run_id}.html")
    csv_path = os.path.join(directory, f"plan-{plan.run_id}.csv")

    with open(html_path, "w", encoding="utf-8") as stream:
        stream.write(render_html(plan, missing_phash=missing_phash))
    with open(csv_path, "w", encoding="utf-8") as stream:
        stream.write(render_csv(plan))

    return {"html": html_path, "csv": csv_path}
