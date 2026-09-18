# To do

## Before trusting it with the real library

- [ ] Run the Plan task against the live library and read the HTML report end to end.
      Confirm the keeper in every group and that each skipped group has a defensible
      reason.
- [ ] Answer the open question below about multi-file scenes, which decides whether the
      `deleteFiles` path or the `scenesDestroy` path gets exercised first.
- [ ] Integration run against a throwaway Stash container with copied sample files —
      nothing in `tests/` has touched a real Stash instance yet.
- [ ] First real run with `maxDeletionsPerRun: 5`, then reconcile `audit.jsonl` against
      the resulting file tree before raising it.

## Open questions

1. Does the library contain scenes where Stash attached both the pre- and post-Unmanic
   file to a *single* scene, or are they always separate scenes? Both paths are
   implemented and tested, but knowing which dominates says where to focus the
   integration testing.
2. Are Unmanic outputs identifiable by path or naming convention? If so `preferredPaths`
   with `path_priority` may resolve groups that codec ranking alone leaves ambiguous.
3. Is any 4K source worth keeping? If so, `protectedPaths`, or a `CDR: Protect` tag
   honoured as an additional guard rail.

## Found by review, not yet fixed

From the security, correctness and accessibility review of 2026-07-31. The severe and
critical items are all fixed; these are what remains.

**Correctness / robustness**
- [ ] No minimum Stash version gate. The version is recorded but never compared, so an
      incompatible server fails at the first call using a missing field.
- [ ] `maxFractionOfLibrary` divides by the *scene* count while the numerator counts
      files. The cap fires earlier than documented on libraries with multi-file scenes.
      Needs a verified `findFiles` count.
- [ ] `plan_from_dict` raises bare `KeyError`/`ValueError` on a structurally malformed
      but JSON-valid plan; should be a `ParseError` with a code. `mode_ui_load` bypasses
      the plan reader entirely.
- [ ] `plan_execute` passes `planFile` from the summary, which is `None` when `reportDir`
      is empty; pass the in-memory plan instead.
- [ ] `build_plan` has no logger, so a group that raises during planning is recorded but
      never logged.
- [ ] `_path_priority` compares raw path prefixes; same unresolved-symlink issue that
      `G_PROTECTED_PATHS` had.
- [ ] No caps on `sys.stdin` size, `args["selection"]` length, or plan file size, while
      every *setting* is properly bounded.
- [ ] Settings changes from the page are not written to the audit log.

**Missing tests**
- [ ] `candidates.py` — the main parser of untrusted external input, zero tests. Needs
      valid/invalid/boundary/exception coverage.
- [ ] `StashClient.call` transport layer — retry/backoff split, non-JSON response,
      GraphQL error aggregation. Inject the sleep to keep it fast.
- [ ] `audit.py` and `plugin_log.py`. The newline stripping in `plugin_log._emit` is what
      stops a filename injecting a fake log record, and it is untested.
- [ ] The `G_LAST_COPY` "property test" computes `loser_count` itself, so it is
      tautological. Drive it through `plan_group` instead.

**Scalability** (measured: 209 groups → 0.48 MB plan; 2,090 → 4.81 MB)
- [ ] The whole plan is returned through `runPluginOperation` into the browser with no
      pagination, and the page renders every group with no virtualisation. `eligible()`
      walks the entire plan on every render.
- [ ] Execute does one sequential GraphQL round trip per group against single-writer
      SQLite.
- [ ] No `AbortController` on any UI fetch, so a hung `ui_plan` leaves the page stuck
      busy with no way out.

**Accessibility** — criticals fixed; remaining medium/low items
- [ ] `AddEntry` unmounts when the last option is added, dropping focus.
- [ ] Remove-button focus is not redirected after a row is removed.
- [ ] `.cdr-reorder-item:focus-visible` is a dead rule — the `<li>` has no `tabIndex`.
      Either delete it or implement roving focus with arrow keys.
- [ ] `schema.codecAliases` is sent to the page but unused, so adding `h265` when `hevc`
      is present passes the client dedupe and fails server-side.
- [ ] `selectWhereKeeperIs` compares the raw codec string, so `h265` is missed where
      Python would normalise and match.

**Dead code**
- [ ] `plugin_log.trace` / `structured`, `SceneInfo.is_multi_file`,
      `Action.REASSIGN_PRIMARY`, `Action.MERGE_METADATA`, `Plan.resolved`, and the
      `:root[data-theme]` blocks in `report.py` (nothing sets `data-theme`).
- [ ] Logging is prose f-strings throughout while `spec.md` claims structured records.
      Either route events through `structured()` or correct the spec.

## Deferred

- Richer page features. The page exists and is functional; what is deferred is anything
  that depends more deeply on Stash's internal UI API, which is the
  highest-maintenance surface here.
- Tune `qualityFloorRatio` and `qualityFloorRatioCrossCodec` once real report data
  exists. The current 0.35 and 0.15 are reasoned starting points, not measured ones.
- CI workflow running the test suite on push, plus branch protection.
- Consider submitting to CommunityScripts once it has run clean for a while, noting the
  relationship to `DupFileManager` so users can choose between them.

## Known gaps

- `scene_count()` is used as the denominator for `maxFractionOfLibrary`, but the
  numerator counts files. On a library with many multi-file scenes the fraction reads
  slightly low. Switching to a file count needs a verified `findFiles` count field.
- Progress reporting during `findDuplicateScenes` is not possible; that query is a single
  blocking call inside Stash, so the job bar sits at zero until it returns. On a large
  library this looks like a hang for the first minute or two.
