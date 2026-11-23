# Upstream SAT watch — GitHub Actions

Prepared locally on 23 November 2026. **Not published or enabled yet.**
The SAT component and its version remain unchanged; no HA update is required.

## What runs

`.github/workflows/upstream-watch.yml` runs on GitHub-hosted Ubuntu, daily
at **09:15 Europe/Prague**, or manually. It reads public Alexwijn/SAT metadata:

- stable and prerelease releases since the reviewed SAT5 release, changed
  release notes, removed releases and moved release tags;
- the default development branch HEAD, including a rewind or branch change;
- new/updated upstream issues and PRs, including merge status and PR head SHA.

It does not execute upstream code or decide whether a change is compatible.
One changed scan creates at most one review issue assigned to `sochorf`, with
links and the PI/HR / compat / shadow / native-SAT5 regression checklist.
An unchanged scan creates nothing. Delivery of GitHub/email notifications
depends on the user's notification settings; it is not an HA/iPhone notification.

The initial checkpoint is `hodko/upstream-watch.json`, checked against the
fork's `release-policy.json` upstream commit. Known 5.0.0, development HEAD and
issues/PRs 204–207 are seeded from the 23 November review. Future adoption of
another upstream commit requires reviewing this checkpoint too.

After the first changed scan, the newest watcher issue stores a hashed JSON
checkpoint. Both open and closed issues count. Preserve that hidden block and
do not delete watcher issues. A missing/corrupt block fails the run rather
than silently losing continuity. A POST timeout is never retried in the same
run: the next complete scan checks whether GitHub already created the issue.

All pages are read before writing. API/schema/size/page-limit failures fail
the workflow; they are not a successful "no changes" result. An unchanged
scan does not advance the durable checkpoint, so a later scan rechecks from
the last recorded point. The 20-page/1000-item/body-size bounds require a
reviewed baseline compaction if eventually reached. Release/tag reads are
live observations, not an atomic GitHub snapshot. Issue updates use a fixed
UTC cutoff; changes after it remain for the next run.

## Permissions and activation

The workflow can read repository contents and write issues **only in this
fork**. It has no contents-write permission, HA secrets or MQTT access.
The client allowlist rejects releases, merges, comments, upstream writes and
redirects. Preview is the default. Even a manual apply requires the repository
variable `SAT_UPSTREAM_ISSUES_ENABLED=true` and the expected repository/default
branch Actions context. No PAT or additional secret is needed.

1. Commit the watcher, tests and documentation in `sochorf/hodko-sat`.
2. Push to its default branch `codex/hodko-sat`; confirm the watcher test job
   and existing release checks pass. Keep the issues variable unset/false.
3. In Actions, run **Watch upstream SAT changes**, `create_issues=false`.
   Verify the summary and source links. No new SAT release/install/restart.
4. Once notification behavior is approved, set repository Actions variable
   `SAT_UPSTREAM_ISSUES_ENABLED=true`. A scheduled run can then create a digest;
   a manual write run additionally requires `create_issues=true`.
5. Verify the first real changed scan, issue assignment and deduplication on
   the next run. Do not invent a source change just to force a public issue.
6. Only after the GitHub replacement is verified, pause the existing Codex
   daily upstream watcher to avoid duplicate monitoring. Code review remains
   a separate task; this script does not replace that assessment.

Rollback: set the variable false to return to read-only preview; Disable
workflow to stop all runs. Check/cancel a currently running job separately;
an in-flight POST may already have succeeded. Preserve existing checkpoint
issues. Nothing in HA or the running heating controller needs rollback.

## Validation and platform limits

`python3 scripts/test_watch_upstream.py` is entirely offline and tests change
detection, closed-issue deduplication, tag/branch reversal, partial scans,
pagination, post-timeout reconciliation and restrictions on API writes.
`python3 scripts/watch_upstream.py` is a live **read-only** preview.
`python3 scripts/check_release.py` verifies the unchanged component inventory.

Local acceptance on 23 November 2026: **23 offline tests passed**, release
inventory still **161/161 component files**, YAML schedule/permissions checked.
The read-only API preview at **2026-11-23T00:39:54Z** completed with zero changes
and zero writes. An earlier preview caught URL encoding in the read allowlist;
that was fixed and covered by the generated-URL regression. Hosted CI, scheduled
execution and actual issue delivery remain to be verified after publication.

GitHub schedules run from the default branch and can be delayed or dropped.
Public-repository schedules can be disabled after 60 days without activity;
monitor the Actions run history/failed-run notifications and re-enable when
needed. Do not create dummy commits or issues to disguise inactivity.
This watcher is maintenance support, not a safety-critical heating monitor.

References: [scheduled workflows and time zones](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[GITHUB_TOKEN permissions](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token),
[issues API](https://docs.github.com/en/rest/issues/issues).
