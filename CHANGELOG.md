# Changelog

Notable changes to this plugin. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The published index appends the short
commit hash to each version, e.g. `0.2.0-3da02d5`.

## [0.4.0] - 2026-10-15

### Added

- `G_VISUAL_MATCH` / `visualCheck` (default off): compares what the two files show
  before deleting, using the ffmpeg in Stash's container. Both are decoded at the same
  timestamps and compared with SSIM, over the whole clip under 20 seconds and at five
  2-second samples otherwise. The worst sample must reach `minVisualSimilarity` (default
  0.90). Pairs it cannot judge are blocked: different aspect ratios, near-blank frames,
  or an ffmpeg failure. Re-run immediately before each deletion.
- A visual match clears `G_QUALITY_FLOOR`, whose bitrate ratio blocked visually
  identical encodes, and `G_MIN_DURATION` for clips of at least `visualMinDuration`
  (default 5 seconds), so short duplicates can be cleaned up.
- All three settings are settings-only; the page cannot override them. If `visualCheck`
  is on and ffmpeg cannot be found, the run fails with `VISUAL_CHECK_UNAVAILABLE`
  instead of skipping the check.

### Notes

- Calibration on 32 pairs from a 1,883-group plan: known duplicates (same stash ID)
  scored 0.897-0.997; clearly different scenes 0.09-0.74. 5 of 12 `G_QUALITY_FLOOR`
  blocks scored 0.976 or higher, one at a bitrate ratio of 0.048. 6 of 10 stash-ID
  conflicts were visually the same video, so most conflicts are identification errors
  rather than different scenes.
- Short clips, measured with whole-clip comparison: 102 known duplicates under 60
  seconds (same stash ID) had a median SSIM of 0.978, and 86 reached 0.90. 40 pairings
  of unrelated clips with the same aspect ratio and similar length never exceeded 0.43.
  Phash-matched pairs that were different scenes scored up to 0.74, which is why the
  default stays at 0.90 rather than lower.
- Known limitation: copies padded or cropped by a few pixels (e.g. 1080x2066 vs
  1080x2034) score low, because scaling both to one size shifts the picture. Those pairs
  are blocked, not deleted.
- Cost is about 1-2 seconds per short clip and 3-7 seconds per sampled long file on a
  loaded server. The probe runs only where its answer can change the outcome.

## [0.3.0] - 2026-10-13

### Added

- `G_RESOLUTION` never deletes a higher-resolution file to keep a lower one,
  independent of `rankOrder`. The plan page re-sends its selected preset each run, and a
  codec-first preset replaced 1080p/4K originals with 720p/360p re-encodes.
- `allowResolutionDowngrade` (default off) turns `G_RESOLUTION` off for a deliberate
  downscaling pipeline, such as 4K re-encoded to 1080p HEVC. It is settings-only; the
  page cannot override it.

### Changed

- With default settings, codec-first ranking no longer keeps a lower-resolution file
  over a higher-resolution one. Such groups are reported as skipped. To restore 0.2.x
  behaviour, turn on `allowResolutionDowngrade`.

## [0.2.1] - 2026-10-09

### Fixed

- `G_STASH_ID_CONFLICT` now also compares losers against each other. Two losers linked
  to different stash IDs on the same endpoint would make the keeper's merge fail mid-run
  (Stash rejects two IDs per endpoint), leaving the group partially applied. The code
  change shipped in 0.2.0; this entry records the failure it prevents.

## [0.2.0] - 2026-10-09

### Fixed

- Merges no longer strip identification. `sceneMerge` copies no metadata itself, and
  `merge_values` only carried tags, performers, URLs and rating, so studio, title, code,
  date, details, director and stash-box IDs were lost whenever the deleted copy was the
  identified one. With codec-first ranking that was often the case, because the
  unidentified HEVC re-encode won. Stash IDs are now unioned. When the loser is stash-box
  identified and the keeper is not, the loser's identity fields replace the keeper's
  (whose title is usually just a filename). Otherwise they only fill gaps.
- Merges keep the Organized flag. A pair differing only in `organized` previously raised
  no merge at all, so the flag was silently dropped. A merge now sets it on the keeper
  when any merged scene had it, and never clears it.

### Added

- `G_MIN_DURATION` / `minDuration` (default 60s, range 0-3600): skips groups involving
  clips shorter than this, because phash cannot distinguish very short clips. Measured on
  a 118k-scene library at distance 4: 1-13s clips paired with unrelated full scenes.
  Settings only; the page cannot override it.
- `G_STASH_ID_CONFLICT`: blocks a loser whose scene links to a different stash ID, on the
  same stash-box endpoint, than any other scene in its group. Either they are different
  scenes or one identification is wrong; both need a human, so the group is reported
  instead of merged. The check covers the whole group rather than only the keeper,
  because an unidentified keeper conflicts with nothing: two losers linked to different
  entries would otherwise both be merged into it.

### Notes

- Measured precision against stash-box links (same ID = true duplicate):

  | `phashDistance` | Groups | Precision |
  |---|---|---|
  | 0 | 242 | 98.9% |
  | 4 | 5,045 | 96.4% |
  | 8 | 6,036 | 94.7% |
  | 10 | 6,325 | 93.3% |

  `durationDiff` 1 vs 5 changes group counts by about 1%.
- Recommend the "Keep the highest resolution" preset (`resolution,codec,bitrate,size`)
  over codec-first. Codec-first kept 360p-720p HEVC copies over 1080p H.264 originals (93
  cases, 143 GB) on the test library.
- The page preset overrides the saved `rankOrder` for each run, and the page opens with
  the codec preset selected. Changing the setting alone therefore does not take effect
  from the page. Consider making the page default to the saved setting.
- Plans written by 0.1.0 still load; the new scene fields default to empty.

## [0.1.0] - 2026-09-18

Initial release.
