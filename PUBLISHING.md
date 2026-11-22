# Publication preparation — Hodko SAT 5.0.0.2

Prepared on 23 November 2026. The user committed the repair at
`44f1289f9f39949739784df7d1457966ad47c5df` and requested continuation with
publication. `publication_ready=true` records completed technical review;
this document itself never authorizes installation, restart or live control.
The publication commit changes only readiness metadata and documentation.

## Scope and rollback

- Destination remains `sochorf/hodko-sat`, branch `codex/hodko-sat`, domain `sat`.
  Candidate version/tag **5.0.0.2**, minimum HA **2026.11.2**.
- Base is published .1 at `64494198e19a3bca9a093d3079b9e1e0dfb2f360`.
  Only `compat/__init__.py` and manifest version differ in the 161-file component.
  The nine pinned preserved Ally files and every other component file are equal.
- Existing version10 settings/identities, source times, 55 °C, independent DHW and
  disabled Spook are preserved. No change to upstream native SAT5 policy.
- Four isolated startup regressions expose the .1 import collision; shadow-first
  additionally exposes the dependent-sensor setup order. Both are repaired.
- Keep published **4.2.1.51**, commit
  `d96348f2a3419baf841c3ce5ddb4bfcb6213ab78`, as fallback. Its source archive was
  verified before .1 installation: all 136 files, including 80 component files.
  The unchanged version10 schema supports a code-only return. No new version13
  entries may be silently introduced. Keep the verified pre-upgrade backup.

## Local acceptance

Run the complete pinned `ha-2026.11.2` and historical `baseline` profiles with
`scripts/run_tests.py`. Require all tests, including fresh-process startup and
actual Marketplace update/failed-install/downgrade tests. Review full results;
process launch, config entry loaded, or an old CI pass is insufficient.
Run `scripts/check_release.py` for reviewed component/test hashes, preserved core,
licenses and private-data checks. Regression inventory includes the isolated child
cases; the ordinary suite launches each in a fresh interpreter with sockets off.
Record final test counts and commit SHA before requesting publication.

Current supported-profile result: **1,497 tests +53 subtests passed** on
HA2026.11.2, one expected duplicate-source skip, no failures/errors. This includes
all 18 Marketplace cases and four cold-process starts. Historical HA2026.10.2:
**1,479 tests +53 subtests passed**, one expected duplicate-source skip, no
failures/errors. Local integrity checks pass, including all 161 component files
and 73 test files. Hosted branch CI [45083059109](https://github.com/sochorf/hodko-sat/actions/runs/45083059109)
passed both profiles on 44f1289. Its public source archive matched all 271 files.
The final metadata commit and tag still require their own hosted CI/archive checks.
The first local full run exposed empty untracked directories left from the old checkout in a Git-handoff
fixture; removing only those empty remnants made the unchanged test pass.
The baseline uses the pinned SAT5 test extras, as prescribed by its CI setup.

## Later authorized publication

1. Require the user-committed clean candidate, exact SHA, reviewed diff against
   .1 and successful complete offline suites. Recheck remote branch/tag/release.
2. Commit the reviewed publication metadata without code/test changes. Run
   `python scripts/check_release.py --for-publication --tag 5.0.0.2`.
3. Push only the reviewed branch without force. Require both hosted CI profiles
   on the exact SHA; then create/push only annotated tag 5.0.0.2 and require tag CI.
   Never move or replace 5.0.0.1 or 4.2.1.51.
4. Compare every GitHub source-archive file against the exact Git tree and all
   161 component hashes, with no private/cache/log material. Prepare release
   notes from RELEASE_NOTES.md with tagged documentation links, then publish
   only within the explicit authorization. Verify release/version metadata.
5. Follow MIGRATION.md for a separately authorized Marketplace install/restart
   and acceptance of real entities, automatic HEAT, original-time PI/HR, fresh
   ACK/CS, CH/flame, continuity and a natural DHW return. Repeating a .1 restart
   does not fix its import collision.

A failed gate blocks publication. Failure after installation uses the agreed
rollback; no direct file copy, configuration removal or second boiler writer.
Changing GitHub latest metadata is also a separate authorization and does not
restore an installed system. Preparing this candidate does not repair live heat.
