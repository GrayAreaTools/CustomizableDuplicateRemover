# Customizable Duplicate Remover

A StashApp plugin that resolves phash duplicate groups using a ranking policy you
define, with **codec as a first-class ranking key**, then deletes the losing files only
after you have reviewed exactly what will go.

Built for libraries being continuously re-encoded — Unmanic converting H.264 to HEVC and
downscaling 4K to 1080p — where the file you want to keep is usually both smaller and
lower resolution than the one you want gone.

## Why this exists

Stash's built-in Scene Duplicate Checker offers five bulk selections: none, all but
highest resolution, all but largest file, oldest, and youngest. None of them can express
"keep the HEVC copy." After a transcode pass, every one of them selects the wrong file.

The community plugin `DupFileManager` does rank codecs, but resolution outranks codec,
the ranking sets are edited in a Python config file rather than exposed as settings, and
it inspects only `scene['files'][0]` — so a scene holding both the pre- and
post-transcode file is handled by picking whichever Stash happened to list first.

This plugin ranks `(scene, file)` pairs rather than scenes, puts codec first by default,
and never deletes anything until you have seen the plan.

## How it works

Three stages, and the destructive one is gated twice.

```
Plan  ─────────────────►  Review  ─────────────────►  Execute
findDuplicateScenes        HTML report, CSV,           re-checks every guard rail,
rank candidates            or the plugin page          requires confirmDestructive,
apply guard rails          in Stash                    merges metadata, then deletes
writes a plan file         (deselect anything)         appends to audit.jsonl
DELETES NOTHING
```

Planning contains no destructive code path at all, so you can iterate on the ranking
policy against your live library at zero risk. Execution re-derives every guard rail
from live state and refuses if the library changed since the plan was written.

## Install

The plugin directory is `plugins/` next to Stash's `config.yml`. In the official Docker
image that is `/root/.stash/plugins/`, which on unRAID is whatever host path your
container maps to `/root/.stash` (commonly `/mnt/user/appdata/stash/config`).

```
cp -r plugin /mnt/user/appdata/stash/config/plugins/CustomizableDuplicateRemover
```

Then Settings → Plugins → **Reload Plugins**. The tasks appear under Settings → Tasks,
and the page under Settings → Tools plus a "Dupes" entry in the main nav.

No Python dependencies. The plugin talks to Stash over GraphQL with `urllib` from the
standard library, so nothing needs installing inside the container.

## First run

1. Leave **Confirm Destructive Operations** off. Nothing can delete while it is off.
2. Set **Max Deletions Per Run** to something small, like 5.
3. Run the **Plan** task.
4. Open the HTML report from the report directory, or the plugin page in Stash.
5. Check the keeper in every group, and that each skipped group has a reason you agree
   with. The **deciding key** column tells you which rank key chose the keeper, so a
   surprising choice is traceable rather than mysterious.
6. Only once the report looks right, turn on Confirm Destructive Operations and run
   **Execute Plan**. Raise the deletion cap as confidence builds.

## Ranking

`rankOrder` is an ordered list; the first key that distinguishes two files decides.
Default:

```
codec,resolution,bitrate,size,age
```

so a 1080p HEVC file beats a 4K H.264 one.

| Key | Better means |
|---|---|
| `codec` | earlier in `codecPreference` |
| `resolution` / `resolution_asc` | more / fewer pixels |
| `bitrate` / `bitrate_asc` | higher / lower |
| `size` / `size_asc` | larger / smaller |
| `framerate` | higher |
| `duration` | longer |
| `age` / `age_desc` | older / newer |
| `audio_codec` | earlier in `audioCodecPreference` |
| `path_priority` | earlier in `preferredPaths` |
| `organized` | scene is marked organized |

`file_id` is appended automatically, so ranking is always total and a run is
reproducible regardless of the order Stash returned the group in.

Codec names are ffmpeg's, as Stash reports them — HEVC is `hevc`, never `h265`. Aliases
(`h265`, `x265`, `hvc1`, `x264`, `xvid`, …) are normalised so your input is forgiving.

When every key ties, `tieBreaker` decides: `skip` (default — the group is reported for
manual review rather than guessed at), `smallest_of_highest_resolution`,
`largest_of_highest_resolution`, `smallest`, `largest`, `oldest`, `newest`.

## Guard rails

| ID | What it prevents |
|---|---|
| `G_LAST_COPY` | Deleting every copy in a group. A property test asserts survivors never reach zero, for any group shape and any configuration. |
| `G_KEEPER_INTACT` | Deleting the alternative to a keeper that is missing, unreadable, zero-length, or still being written. Catches an unmounted unRAID share whose database rows still look healthy. |
| `G_LIBRARY_SCOPE` | Deleting outside Stash's configured library paths. Paths are resolved through symlinks first. |
| `G_PROTECTED_PATHS` | Deleting under a path prefix you have declared off-limits. |
| `G_DURATION` | A truncated encode replacing a complete file. Phashes are computed from a sample, so a partial file can match. |
| `G_QUALITY_FLOOR` | A botched, over-compressed transcode winning on codec alone. Compares bits per pixel per frame, with a looser allowance across codecs since HEVC legitimately needs about half H.264's bitrate. |
| `G_METADATA` | Silently losing tags, performers, ratings, play counts, or markers. Merges them into the keeper first, or skips the group. |
| `G_RUN_CAP` | A misconfigured policy deleting a large share of the library in one pass. Two caps: absolute count and fraction of library. A breach aborts before anything is deleted. |
| `G_PLAN_FRESH` | Executing a reviewed plan against a library that has since changed. A fingerprint over every candidate's id, size, mod time, and path must still match. |
| `G_CONFIRM` | An accidental click deleting files. Needs the explicit task *and* the setting. |
| `G_GENERATED` | Destroying sprites and previews that a surviving scene still uses. |

Every rail runs during planning **and again immediately before each destructive call**.

## Duplicate shapes

Stash groups duplicates by scene, but a scene can hold several files. These need
different operations, and conflating them is how metadata gets lost:

| Situation | Operation |
|---|---|
| Loser's scene keeps at least one other file | `deleteFiles` — the scene and its generated assets stay |
| Loser is its scene's last surviving file, no metadata to merge | `scenesDestroy(delete_file: true)` |
| Loser is its scene's last surviving file, metadata to merge | `sceneMerge` with an explicit `values` union, then `deleteFiles` |
| Loser is the only file of the only scene | never — `G_LAST_COPY` |

"Last surviving file" accounts for every file in the group headed for deletion, not just
this one — a scene losing all of its files is a scene destroy, not several file deletes.

## The page

Settings → Tools → **Customizable Duplicate Remover**, or "Dupes" in the main nav.

It reads the last plan instantly, or rebuilds with a different policy. Groups render as
tables with checkboxes, like the built-in checker. The **Keep which file** dropdown
switches policy and recomputes in the backend — it never re-ranks in the browser, so the
page and the tasks can never disagree about what the policy means.

Selecting rows only ever *narrows* what the plan approved. To keep a different file,
change the policy and rebuild, which forces the choice back through the guard rails
instead of around them.

## Settings

Every setting is validated at task start. An invalid value aborts the run naming the
setting, the bad token, and the accepted values; it never silently falls back to a
default, because a typo in `rankOrder` must not quietly delete the wrong files.

| Setting | Type | Default | Notes |
|---|---|---|---|
| `rankOrder` | STRING | `codec,resolution,bitrate,size,age` | Ordered rank keys, highest priority first |
| `codecPreference` | STRING | `av1,hevc,h264,vp9,mpeg4,vc1,wmv3,msmpeg4v3,mpeg2video` | ffmpeg codec names; aliases accepted |
| `audioCodecPreference` | STRING | `opus,aac,ac3,mp3` | Only used when `audio_codec` is in `rankOrder` |
| `tieBreaker` | STRING | `skip` | What to do when every rank key ties |
| `phashDistance` | NUMBER | `0` | 0 exact, 4 high, 8 medium, 10 low |
| `durationDiff` | NUMBER | `1.0` | Passed to `findDuplicateScenes` |
| `durationTolerance` | NUMBER | `1.0` | `G_DURATION` tolerance in seconds |
| `qualityFloorRatio` | NUMBER | `0.35` | `G_QUALITY_FLOOR` same-codec threshold |
| `qualityFloorRatioCrossCodec` | NUMBER | `0.15` | `G_QUALITY_FLOOR` cross-codec threshold |
| `protectedPaths` | STRING | *(empty)* | Comma-separated prefixes never deleted |
| `preferredPaths` | STRING | *(empty)* | Ordered prefixes for `path_priority` |
| `metadataPolicy` | STRING | `merge` | `merge` \| `skip` \| `ignore` |
| `maxDeletionsPerRun` | NUMBER | `100` | `G_RUN_CAP` absolute cap |
| `maxFractionOfLibrary` | NUMBER | `0.05` | `G_RUN_CAP` fractional cap |
| `applyTags` | BOOLEAN | `false` | Tag scenes `CDR: Keep` / `CDR: Delete` during planning |
| `confirmDestructive` | BOOLEAN | `false` | Must be set before any file is deleted |
| `planRetention` | NUMBER | `5` | How many plans to keep before pruning |
| `reportDir` | STRING | `{pluginDir}/reports` | Plans, reports, and audit log |

## Artifacts

Written to `reportDir`, which defaults to `reports/` inside the plugin directory:

- `plan-<runid>.json` — the machine-readable plan, written atomically
- `plan-<runid>.html` — the review report; self-contained, opens with no network access
- `plan-<runid>.csv` — the same data for spreadsheets
- `audit.jsonl` — append-only, `fsync`ed per record: what was deleted, what survived,
  which rank key decided, and every guard rail verdict

## Development

```
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

No Stash instance required. Ranking, guard rails, planning, serialisation, and reporting
are pure functions over fixtures; execution runs against a fake client that records
mutations instead of performing them.

## Status

Phases 1–3 are implemented and unit tested. Not yet exercised against a live Stash
instance — run with `maxDeletionsPerRun` set low and check `audit.jsonl` against your
file tree before trusting it with a large library.

## LLM assistance disclosure

Development of this plugin used LLM assistance. All code was reviewed and tested by a
human maintainer prior to release.
