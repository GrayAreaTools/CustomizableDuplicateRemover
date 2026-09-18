# CustomizableDuplicateRemover — Specification

A StashApp plugin that resolves phash duplicate groups using a configurable ranking
policy, with codec as a first-class ranking key, and deletes the losing files only
after the operator has reviewed an explicit plan.

- **Status:** draft
- **Target:** Stash `develop` schema (see [Verified API surface](#appendix-a--verified-api-surface))
- **Runtime:** Stash in Docker on unRAID, plugin dir `/root/.stash/plugins/CustomizableDuplicateRemover/`

---

## 1. Problem

Stash ships a Scene Duplicate Checker (Settings → Tools) that groups scenes by
perceptual hash and offers a fixed set of bulk selections:

- Select None
- Select every file in each duplicated group, except the file with highest resolution
- Select every file in each duplicated group, except the largest file
- Select the oldest file in the duplicate group
- Select the youngest file in the duplicate group

None of these express "keep the HEVC copy." In a library being continuously
re-encoded by Unmanic (H.264 → HEVC, 4K → 1080p), the desired keeper is frequently
both **smaller** and **lower resolution** than the file to be removed, so every
built-in selection picks the wrong file.

The existing community plugin `DupFileManager` ranks codecs, but:

- resolution outranks codec, which is backwards for this pipeline
- the codec ranking lists are edited in `DupFileManager_config.py`, not exposed as settings
- it reads only `scene['files'][0]`, so scenes holding more than one file are handled incorrectly
- there is no reviewable plan artifact between "decide" and "delete"

## 2. Goals

- G1. Rank duplicate candidates by an operator-defined ordered key list, with codec
  available as the primary key.
- G2. Handle both duplicate shapes: separate scenes sharing a phash, and a single
  scene holding several files.
- G3. Never destroy the last surviving copy of a scene's content, under any
  configuration or failure mode.
- G4. Produce a human-reviewable plan before any destructive call, and refuse to
  execute a plan that no longer matches the library on disk.
- G5. Preserve scene metadata (tags, performers, rating, play counts, markers) when
  the losing scene carries metadata the keeper lacks.
- G6. Leave a durable audit trail of every deletion.

## 3. Non-goals

- Re-encoding, or any interaction with Unmanic. This plugin only chooses and removes.
- Duplicate detection by any means other than Stash's phash. The plugin consumes
  `findDuplicateScenes`; it does not compute hashes.
- Replacing the built-in Duplicate Checker UI. Phase 3 adds a companion page; the
  built-in page keeps working untouched.
- Image or gallery duplicates. Scenes only.

## 4. Concepts

**Group** — one element of the `[[Scene!]!]!` returned by `findDuplicateScenes`: a set
of scenes whose phashes fall within `distance` and whose durations fall within
`duration_diff`.

**Candidate** — a `(scene, file)` pair. A group with 3 scenes where one scene holds 2
files yields 4 candidates. Ranking operates on candidates, not scenes.

**Keeper** — the single highest-ranked candidate in a group.

**Loser** — any candidate in the group that is not the keeper and is not excluded by a
guard rail.

**Plan** — a JSON document listing, per group, the keeper and every loser, with the
evidence used to decide and the guard rails that fired. A plan is inert; it deletes
nothing.

**Action** — the operation applied to a loser, determined by shape:

| Situation | Action |
|---|---|
| Loser file belongs to a scene that has other surviving files | `deleteFiles([file_id])` |
| Loser file is its scene's last surviving file, nothing to merge | `scenesDestroy(delete_file: true)` |
| Loser file is its scene's last surviving file, metadata to merge | `sceneMerge` with an explicit `values` union (it copies nothing by itself), then `deleteFiles` — merge destroys the scene, so a following `scenesDestroy` would error and the file would survive |
| Loser file is the only file of the only scene in the group | never — guard rail `G_LAST_COPY` |

## 5. Ranking policy

### 5.1 Rank keys

A run is configured with an ordered list of rank keys. The first key that
distinguishes two candidates decides between them; ties fall through to the next key.

| Key | Direction | Source field |
|---|---|---|
| `codec` | by `codecPreference` position, earlier is better | `VideoFile.video_codec` |
| `resolution` | higher `width * height` is better | `VideoFile.width`, `.height` |
| `resolution_asc` | lower `width * height` is better | as above |
| `bitrate` | higher is better | `VideoFile.bit_rate` |
| `bitrate_asc` | lower is better | as above |
| `size` | larger is better | `VideoFile.size` |
| `size_asc` | smaller is better | as above |
| `framerate` | higher is better | `VideoFile.frame_rate` |
| `duration` | longer is better | `VideoFile.duration` |
| `age` | older `mod_time` is better | `VideoFile.mod_time` |
| `age_desc` | newer `mod_time` is better | as above |
| `audio_codec` | by `audioCodecPreference` position | `VideoFile.audio_codec` |
| `path_priority` | earlier match in `preferredPaths` is better | `VideoFile.path` |
| `organized` | scene marked organized is better | `Scene.organized` |
| `file_id` | lower id is better — terminal determinism key | `VideoFile.id` |

`file_id` is appended implicitly to every rank order so ranking is always total and
a run is always reproducible.

### 5.2 Default rank order

```
codec,resolution,bitrate,size,age
```

Chosen for the Unmanic pipeline: an HEVC 1080p file outranks an H.264 4K file, because
the 4K original is what the pipeline is deliberately retiring.

### 5.3 Codec preference

Default: `av1,hevc,h264,vp9,mpeg4,vc1,wmv3,msmpeg4v3,mpeg2video`

VC-1 Advanced Profile outranks WMV9, which outranks the much older WMV7/8.

Values are matched against ffmpeg's codec names as Stash reports them —
HEVC appears as `hevc`, not `h265`. The plugin normalises a small alias table
(`h265`→`hevc`, `x265`→`hevc`, `avc`/`x264`→`h264`, `divx`/`xvid`→`mpeg4`) so operator
input is forgiving. A codec absent from the preference list sorts last, and the
group is flagged `UNKNOWN_CODEC` in the plan.

### 5.4 Tie resolution

When every rank key ties, `tieBreaker` decides:

| Value | Behaviour |
|---|---|
| `skip` (default) | leave the group untouched, record it in the plan as `AMBIGUOUS` |
| `smallest_of_highest_resolution` | among candidates at the highest resolution, keep the smallest file |
| `largest_of_highest_resolution` | among candidates at the highest resolution, keep the largest file |
| `smallest` | keep the smallest file |
| `largest` | keep the largest file |
| `oldest` | keep the lowest `mod_time` |
| `newest` | keep the highest `mod_time` |

`skip` is the default because an unresolved tie usually means the ranking policy does
not yet describe the operator's intent, and silently guessing hides that.

## 6. Guard rails

Every guard rail is evaluated during planning and **re-evaluated immediately before
each destructive call**. A guard rail that fires during execution aborts that group
and continues to the next; it never aborts mid-group leaving a half-applied change.

| ID | Rule | Failure mode it prevents |
|---|---|---|
| `G_LAST_COPY` | Each group must end with at least one surviving file. The keeper set is computed, asserted non-empty, and the loser set is asserted to be a strict subset of the group. | Deleting every copy of a scene. |
| `G_KEEPER_INTACT` | Before deleting a loser, `stat` the keeper: it must exist, be readable, and have `size > 0` and `size` matching the DB within 1%. | Deleting the alternative to a keeper that is already truncated, zero-length, or on an unmounted share. |
| `G_LIBRARY_SCOPE` | Every path targeted for deletion must lie beneath one of Stash's configured library paths, resolved through symlinks. | A malformed path, path traversal, or bad DB row causing deletion outside the library. |
| `G_PROTECTED_PATHS` | Paths matching `protectedPaths` are never deleted; if a group's only viable keeper is protected, the group still resolves normally, but a protected loser downgrades the group to `SKIPPED`. | Losing irreplaceable originals kept in a specific folder. |
| `G_DURATION` | Keeper and loser durations must agree within `durationTolerance` seconds (default 1.0). | Deleting a full-length file in favour of a truncated or partial encode that happens to share a phash. |
| `G_QUALITY_FLOOR` | Keeper's bits-per-pixel-per-frame must be at least `qualityFloorRatio` (default 0.35) of the loser's, unless the keeper's codec ranks strictly better, in which case the allowance is `qualityFloorRatioCrossCodec` (default 0.15) to account for HEVC's efficiency. | Deleting a good file in favour of a botched, over-compressed transcode. |
| `G_METADATA` | If a losing scene carries tags, performers, rating, `o_counter`, markers, or a URL the keeper lacks, apply `metadataPolicy`: `merge` (default, `sceneMerge` into the keeper first), `skip`, or `ignore`. | Silently losing curation work. |
| `G_RUN_CAP` | A run deletes at most `maxDeletionsPerRun` files (default 100) and at most `maxFractionOfLibrary` of all files (default 0.05). Exceeding either aborts the run before any deletion. | A misconfigured rank order wiping a large share of the library in one pass. |
| `G_PLAN_FRESH` | Execution recomputes a fingerprint over every candidate's `(file_id, size, mod_time, path)`. Any mismatch against the plan aborts that group. | Executing a stale plan after Unmanic, a rescan, or another tool has moved on. |
| `G_CONFIRM` | Hard deletion requires both the explicit `Execute Plan` task and `confirmDestructive: true` in settings. Neither alone deletes anything. | An accidental click on the wrong task. |
| `G_GENERATED` | `delete_generated` is passed as `true` only for scene destruction, never for file deletion, so sprites and previews belonging to a surviving scene are preserved. | Destroying generated assets still in use. |

### 6.1 Ordering invariant

Within a group, deletions are applied **losers-last**: metadata merge first, then
file deletions, then scene destruction. If any step fails, remaining steps for that
group are abandoned and the group is reported as `PARTIAL` with the exact operations
that succeeded.

## 7. Workflow

The plugin models the Duplicate Checker's decide-then-delete rhythm as three tasks.

```
┌─ Task: Plan ────────────────────────────────────────────────┐
│ findDuplicateScenes → build candidates → rank → guard rails │
│ writes  plan-<runid>.json   (machine)                       │
│ writes  plan-<runid>.html   (review)                        │
│ writes  plan-<runid>.csv    (review)                        │
│ optionally tags scenes:  [CDR: Keep] / [CDR: Delete]        │
│ deletes nothing                                             │
└─────────────────────────────────────────────────────────────┘
                          ↓  operator reads the HTML report,
                             or filters by tag in the Stash UI
┌─ Task: Execute Plan ────────────────────────────────────────┐
│ loads latest plan (or planFile setting)                     │
│ re-checks every guard rail, incl. G_PLAN_FRESH              │
│ requires confirmDestructive: true                           │
│ merges metadata, deletes files, destroys scenes             │
│ appends to audit.jsonl                                      │
└─────────────────────────────────────────────────────────────┘

┌─ Task: Clear Tags ──────────────────────────────────────────┐
│ removes [CDR: *] tags from all scenes                       │
└─────────────────────────────────────────────────────────────┘
```

A fourth task, `Plan and Execute`, runs both phases back to back for operators who
have validated their policy and want an unattended pass. It still honours every guard
rail and still requires `confirmDestructive`.

### 7.1 Review report

`plan-<runid>.html` is a self-contained file — no external assets, per project
standards — showing per group:

- a row per candidate, keeper marked, losers marked, skipped reason if any
- codec, resolution, bitrate, size, duration, frame rate, mod time, path
- the rank key that decided the keeper, so a surprising choice is traceable
- which guard rails fired
- run totals: groups seen, groups resolved, groups skipped, files to delete, bytes to reclaim

Light and dark themes, WCAG AA contrast, and a status column that does not rely on
colour alone.

## 8. Settings

Stash plugin settings are limited to `STRING`, `NUMBER`, and `BOOLEAN`, so list and
enum settings are comma-separated strings, parsed and validated at task start. An
invalid setting aborts the run with a specific message naming the setting and the
accepted values — it never falls back to a default silently.

| Setting | Type | Default | Notes |
|---|---|---|---|
| `rankOrder` | STRING | `codec,resolution,bitrate,size,age` | Validated against the key table in §5.1 |
| `codecPreference` | STRING | `av1,hevc,h264,vp9,mpeg4,msmpeg4v3,wmv3,vc1,mpeg2video` | |
| `audioCodecPreference` | STRING | `opus,aac,ac3,mp3` | Only used if `audio_codec` is in `rankOrder` |
| `tieBreaker` | STRING | `skip` | §5.4 |
| `phashDistance` | NUMBER | `0` | 0 exact, 4 high, 8 medium, 10 low |
| `durationDiff` | NUMBER | `1.0` | Passed to `findDuplicateScenes` |
| `durationTolerance` | NUMBER | `1.0` | `G_DURATION` |
| `qualityFloorRatio` | NUMBER | `0.35` | `G_QUALITY_FLOOR` |
| `qualityFloorRatioCrossCodec` | NUMBER | `0.15` | `G_QUALITY_FLOOR` |
| `protectedPaths` | STRING | *(empty)* | Comma-separated prefixes |
| `preferredPaths` | STRING | *(empty)* | Ordered, for `path_priority` |
| `metadataPolicy` | STRING | `merge` | `merge` \| `skip` \| `ignore` |
| `maxDeletionsPerRun` | NUMBER | `100` | `G_RUN_CAP` |
| `maxFractionOfLibrary` | NUMBER | `0.05` | `G_RUN_CAP` |
| `applyTags` | BOOLEAN | `false` | Write `[CDR: Keep]` / `[CDR: Delete]` during planning |
| `confirmDestructive` | BOOLEAN | `false` | `G_CONFIRM` — must be set by hand |
| `reportDir` | STRING | `{pluginDir}/reports` | Plans, reports, audit log |

## 9. Observability

No `print` to stdout except the single plugin result envelope.

- **Logs** — structured records on stderr through Stash's log protocol, at `info` for
  group decisions, `warning` for guard rails that fired, `error` for failures. Every
  record carries the run id and group index.
- **Progress** — `log.progress(i / total)` per group, driving the Stash job bar.
- **Audit** — `audit.jsonl`, one object per destructive operation, appended with
  `fsync`: run id, timestamp, group index, operation, file id, path, size, phash,
  keeper file id, keeper path, rank key that decided, guard rails evaluated, result.
  This file is append-only and is never rewritten by the plugin.
- **Run summary** — returned as the plugin's `output` object: counts by outcome, bytes
  reclaimed, and the effective configuration after parsing.

No file paths are treated as sensitive, but the audit log is local-only and never
transmitted.

## 10. Failure handling

- A GraphQL call that fails is retried up to 3 times with exponential backoff for
  transport errors only. A schema or validation error is not retried.
- Every external call carries a timeout (default 30 s, 120 s for `sceneMerge`).
- A group that raises is caught, logged with the group index and full context, marked
  `FAILED` in the summary, and the run continues. One bad group does not end a run.
- The run exits non-zero with an `error` payload only for conditions that invalidate
  the whole run: unparseable settings, `G_RUN_CAP` breach, unreachable server,
  unreadable plan file.
- Interruption (Stash job cancel, container stop) leaves the audit log consistent —
  every entry describes an operation that already completed.

## 11. Compatibility

- Stash's version is read and recorded in every plan for forensics, but there is **no**
  minimum-version gate yet; an incompatible server fails at the first call that uses a
  missing field. See docs/ToDo.md.
- `stashapp-tools` `destroy_scene` hardcodes `delete_generated: true` and exposes only
  `delete_file`. Where the plugin needs different flags it calls `call_GQL` directly
  against the verified mutation shapes in Appendix A.

## 12. Test plan

Ranking, guard rails, and plan generation are pure functions over candidate data, so
the bulk of the suite runs against fixtures with no Stash instance.

**Unit — ranking**
- codec-first order keeps 1080p HEVC over 4K H.264
- resolution-first order keeps 4K H.264 over 1080p HEVC
- unknown codec sorts last and flags the group
- codec aliases normalise (`h265`, `x265`, `HEVC` → `hevc`)
- every rank key sorts in the documented direction
- `file_id` makes ranking total: shuffled input yields identical output
- each `tieBreaker` value picks the documented candidate

**Unit — settings validation**
- valid, invalid, empty, whitespace-padded, duplicated, and oversized inputs for every
  setting; unknown rank key names the offending token
- numeric settings reject negatives, non-numbers, and out-of-range values
- every setting has a case covering the exception path

**Unit — guard rails**
- one case per guard rail proving it fires, and one proving it permits the safe case
- `G_LAST_COPY` holds for a single-file group, an all-protected group, and a group
  where every candidate is skipped
- property test: for any generated group and any valid configuration, the surviving
  file count is never zero

**Integration — against a disposable Stash container**
- multi-file scene → `deleteFiles`, scene survives, `primary_file_id` still valid
- separate scenes → metadata merged, loser scene destroyed, keeper retains its own tags
- plan then mutate a file on disk then execute → `G_PLAN_FRESH` aborts that group
- `confirmDestructive: false` → execute deletes nothing and says why
- run cap exceeded → nothing deleted
- audit log replays to exactly the observed library state

**Determinism** — the same fixture and configuration produce a byte-identical plan
across runs, excluding the run id and timestamps.

---

## Appendix A — Verified API surface

Verified against `stashapp/stash@develop` on 2026-07-31.

```graphql
findDuplicateScenes(
  distance: Int
  duration_diff: Float
  scene_filter: SceneFilterType
): [[Scene!]!]!

type VideoFile implements BaseFile {
  id: ID!
  path: String!
  basename: String!
  mod_time: Time!
  size: Int64!
  fingerprint(type: String!): String
  fingerprints: [Fingerprint!]!
  format: String!
  width: Int!
  height: Int!
  duration: Float!
  video_codec: String!
  audio_codec: String!
  frame_rate: Float!
  bit_rate: Int!
  scenes: [Scene!]!
  created_at: Time!
  updated_at: Time!
}

type Fingerprint { type: String!  value: String! }
# phash is the fingerprint whose type == "phash"

input SceneDestroyInput {
  id: ID!
  delete_file: Boolean
  delete_generated: Boolean
  destroy_file_entry: Boolean
}
input ScenesDestroyInput {
  ids: [ID!]!
  delete_file: Boolean
  delete_generated: Boolean
  destroy_file_entry: Boolean
}

deleteFiles(ids: [ID!]!): Boolean!    # removes from disk and DB
destroyFiles(ids: [ID!]!): Boolean!   # removes DB entries only
moveFiles(input: MoveFilesInput!): Boolean!

sceneMerge(input: SceneMergeInput!): Scene
sceneUpdate(input: SceneUpdateInput!): Scene       # primary_file_id, tag_ids (replaces)
bulkSceneUpdate(input: BulkSceneUpdateInput!): [Scene!]
# additive tagging: tag_ids: { ids: [...], mode: ADD }
tagCreate(input: TagCreateInput!): Tag
```

`fileSetPrimary` does not exist. Set a scene's primary file with
`sceneUpdate(primary_file_id:)` or attach with `sceneAssignFile(input: {scene_id, file_id})`.

`size` is the `Int64` scalar and arrives as a string in some clients — parse
explicitly, never assume `int`.

## Appendix B — Plugin runtime facts

- Manifest keys: `name`, `description`, `version`, `url`, `ui`, `settings`, `exec`,
  `interface`, `errLog`, `tasks`, `hooks`. `interface: raw` for an external Python
  plugin; `interface` defaults to `raw`.
- Setting types are exactly `STRING`, `NUMBER`, `BOOLEAN`.
- `{pluginDir}` is substituted in `exec`. The process's working directory is Stash's,
  not the plugin's, so every path must be built from `{pluginDir}` or
  `server_connection.PluginDir`.
- Input is one JSON object on stdin: `server_connection`
  (`Scheme`, `Host`, `Port`, `SessionCookie`, `Dir`, `PluginDir`) and `args`.
- Output is one JSON object on stdout: `{"output": ..., "error": ...}`.
- Logging is on stderr, framed as `\x01` + level char + `\x02` + message, level chars
  `t d i w e p`; `p` carries progress in `0..1`.
- Plugin dir in the official Docker image is `/root/.stash/plugins/<Name>/`, which on
  unRAID is the host path mapped to `/root/.stash`. Reload with Settings → Plugins →
  Reload Plugins, or the `reloadPlugins` mutation.
