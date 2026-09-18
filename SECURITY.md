# Security

This plugin deletes files. A bug in ranking, guard rails, or path handling can cost
someone their media, so security and correctness reports are both welcome.

## Reporting

Report privately via GitHub's [private vulnerability
reporting](https://github.com/GrayAreaTools/CustomizableDuplicateRemover/security/advisories/new),
or email `grayareatools@fastmail.us`.

Please do not open a public issue for anything that could cause data loss until there
is a fix available.

Include the plugin version, your Stash version, the relevant settings (`rankOrder`,
`codecPreference`, `tieBreaker`, the guard rail thresholds), and the plan JSON if you
have it. Redact file paths if they are sensitive — the group structure and file
metadata are usually enough to reproduce a ranking bug.

Expect an initial response within a week.

## Scope

In scope:

- Any path by which the plugin deletes a file it should not have, including guard rail
  bypasses and ranking errors that select the wrong keeper
- Path traversal or deletion outside Stash's configured library paths
- Metadata loss during a merge
- Injection into the plan, report, audit log, or Stash's log stream through file paths
  or other library-derived data
- Credential or session cookie leakage through logs, reports, or the plugin page

Out of scope:

- Vulnerabilities in Stash itself — report those to
  [stashapp/stash](https://github.com/stashapp/stash)
- Data loss caused by running `Execute Plan` without reviewing the plan first, with
  `confirmDestructive` deliberately enabled

## Handling of sensitive data

The plugin holds no credentials of its own. Stash passes a session cookie in
`server_connection` for the lifetime of the subprocess; it is never written to a plan,
report, log, or the audit file.

Plans, reports, and `audit.jsonl` do contain absolute file paths for the whole
duplicate set. They are written under `reportDir` inside the plugin directory and are
never transmitted anywhere. Only `reportDir` is mapped into the plugin page's asset
route, so the source, plans, and audit log are not served to anyone who can reach the
Stash UI.
