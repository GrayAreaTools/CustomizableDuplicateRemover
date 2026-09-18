# Assumptions

Non-obvious decisions and the reasoning behind them.

---

- **Assumption:** Codec outranks resolution by default, so a 1080p HEVC file is kept
  over a 4K H.264 file.
- **Why:** The library is processed by Unmanic, which re-encodes to HEVC and downscales
  4K to 1080p. The 4K H.264 copy is what the pipeline is deliberately retiring, so
  ranking resolution first would delete exactly the file the operator wants to keep.
  `rankOrder` remains configurable for operators whose pipeline differs.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** Ranking operates on `(scene, file)` candidates rather than on scenes.
- **Why:** A Stash scene can hold several files, and `findDuplicateScenes` returns
  scene groups. Ranking scenes by `files[0]` — what `DupFileManager` does — picks
  arbitrarily when a scene holds both a pre- and post-transcode file. Candidate-level
  ranking also makes the file-delete and scene-destroy code paths distinguishable.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** An unresolvable tie skips the group by default instead of guessing.
- **Why:** A tie normally means the configured ranking policy does not yet describe the
  operator's intent. Guessing hides that signal behind an irreversible deletion.
  `tieBreaker` offers automatic resolution for operators who want it.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** Planning and execution are separate tasks communicating through a
  plan file, rather than one task that decides and deletes.
- **Why:** The operator asked for hard deletion, but only after reviewing what would be
  deleted. Splitting the phases makes the review step a real gate rather than a log
  line, and lets the planning phase be iterated against the live library with no risk.
  The freshness fingerprint stops a reviewed plan from being applied to a library that
  has since changed.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** `phashDistance` defaults to 0 (exact) rather than Stash's looser
  presets.
- **Why:** Every guard rail narrows the risk that two files sharing a phash are not
  actually interchangeable, but none eliminates it. A larger distance multiplies the
  consequence of any bug elsewhere in the ranking or guard rail code.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** The quality floor allows a much lower bitrate ratio when the keeper's
  codec ranks strictly better (0.15) than when codecs are equal (0.35).
- **Why:** HEVC reaches comparable perceptual quality at roughly half the bitrate of
  H.264, so a flat ratio would either reject every legitimate HEVC keeper or fail to
  catch botched same-codec transcodes. Both figures are starting points and should be
  revisited once real report data exists.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** The plugin has no third-party dependencies at all; the GraphQL client
  is built on `urllib` rather than using `stashapp-tools`.
- **Why:** The plugin runs inside the Stash container, where the operator cannot be
  assumed to have installed extra packages, and only a dozen calls are needed.
  `stashapp-tools` also hardcodes `delete_generated: true` on scene destruction and
  exposes only `delete_file`, so it cannot express the flag combinations
  `G_GENERATED` requires.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** The UI is a separate plugin page, not an extension of Stash's Scene
  Duplicate Checker.
- **Why:** The built-in checker's component is never wrapped by `PatchComponent` and is
  explicitly excluded from Stash's loadable-component registry, so its selection
  dropdown cannot be patched. Registering a route is the only supported route to a
  comparable page.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** The page never re-implements ranking; it calls the Python backend via
  `runPluginOperation` and renders what comes back.
- **Why:** Ranking in both JavaScript and Python would let the page and the tasks
  disagree about what a policy means, and the disagreement would only surface as a
  wrongly deleted file. Policy presets in the page therefore trigger a backend recompute
  rather than a client-side re-sort.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** Page selections may only narrow the plan, never widen it. A selected
  key that is not already an approved loser is ignored.
- **Why:** Otherwise the page would be a way to request a deletion the guard rails never
  approved. Keeping a different file is a policy change, and routing it through a
  recompute forces it back through the rails.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** An absent `selection` argument means "the whole plan"; an empty one
  means "nothing".
- **Why:** These must not collapse together. A unit test caught the dispatch layer
  converting an empty list to `None`, which would have turned a page sending zero
  selections into a full-plan deletion — the worst available failure mode.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** `PROTECTED` status is reserved for groups blocked solely by
  `G_PROTECTED_PATHS`; every other blocked group is `SKIPPED` with the responsible rails
  named in the reason.
- **Why:** The first end-to-end run labelled quality-floor and duration blocks as
  "protected", which reads as an operator decision rather than a safety refusal and would
  mislead someone scanning the report.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** The review report is a self-contained HTML file built with the
  standard library, not a templating engine.
- **Why:** Dependency minimisation, and the plugin runs inside the Stash container
  where extra packages are an install burden on the operator.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** No HTTP health endpoints are provided.
- **Why:** Liveness and readiness endpoints belong to long-running *services*. This
  plugin is a short-lived subprocess Stash invokes for a task; it has
  no listening socket and no uptime to monitor. Health of the Stash server itself is
  outside this project's scope. The plugin does assert server reachability and schema
  compatibility at startup, which is the equivalent readiness check for its shape.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** No caching or CDN configuration is provided.
- **Why:** Caching and CDN concerns apply to services serving HTTP responses. This
  plugin serves none. Duplicate group data is fetched once per run and
  held in memory for that run only; caching it across runs would risk acting on stale
  file state, which `G_PLAN_FRESH` exists to prevent.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** The executor rebuilds candidates from live Stash state and refuses a
  plan whose `(file id, path)` pairing does not reconcile.
- **Why:** The plan file is an input, not a trusted record. Judging the rails against the
  plan's copies while deleting by the plan's file id meant every rail could validate one
  file while the deletion targeted another — verified with a proof-of-concept plan that
  passed all seven rails and deleted an unrelated file. It also meant `G_METADATA` saw
  stale scene data, so curation added during review was silently destroyed. The
  fingerprint alone cannot close this, because the fingerprint is itself a value read out
  of the plan file.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** Per-request setting overrides use an allowlist, and the page cannot
  override any safety control.
- **Why:** A denylist excluding only `confirmDestructive` left every other guard-rail
  parameter overridable, so a single request could empty `protectedPaths`, zero the
  quality floor, widen the duration tolerance to an hour and lift both caps, and a second
  request would then execute the resulting plan. Overrides are rejected loudly rather
  than ignored, so a mistake cannot silently run under different rails.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** When metadata must be preserved, the sequence is `sceneMerge` with an
  explicit `values` union followed by `deleteFiles` — never `scenesDestroy`.
- **Why:** Verified against Stash's source: `sceneMerge` copies no metadata of its own
  (only markers, files and history migrate; everything else comes from `values`), and it
  destroys the source scene row while moving its file onto the destination without
  touching disk. Merge-then-`scenesDestroy` therefore lost the metadata *and* left the
  duplicate file on disk, while the summary and audit log both reported it deleted.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** Status badges in the page carry their own background colour rather than
  only a foreground.
- **Why:** Stash supports a dark and a light theme. No single foreground colour reaches
  the WCAG AA 4.5:1 minimum against both `#202b33` and `#ffffff` — the entire sRGB cube
  was checked and none exists. Pinning both colours makes the host theme irrelevant. The
  row tints remain as redundant reinforcement only; the disposition text inherits
  `currentColor`.
- **Recorded by:** Claude
- **Date:** 2026-07-31

- **Assumption:** Reorder buttons are never `disabled` at the ends of the list.
- **Why:** Disabling the button the user just pressed moves focus to `document.body`,
  losing their place in the form — which broke the keyboard path that exists precisely to
  make drag-and-drop reordering accessible. At a boundary the buttons no-op and announce
  why instead.
- **Recorded by:** Claude
- **Date:** 2026-07-31
