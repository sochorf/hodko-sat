# Publication plan — Hodko SAT 5.0.0.1

Prepared on 22 November 2026. **No push, tag, release or installation is authorized
by this document.** Publication and installation require separate instructions.
The reviewed code/test commit is `e7c2ecd0084f0778265a6ea5920890fce9bfd6d2`.
The final release commit must also contain this publication preparation.

## Scope and evidence

- Existing destination: `sochorf/hodko-sat`, branch `codex/hodko-sat`.
  Manifest and intended tag: **5.0.0.1**; minimum HA: **2026.11.2**.
- Existing version10 / PI/HR / shadow entries keep the .51 compatibility policy.
  New native version13 entries use SAT5. This is not a change of boiler policy.
- The code commit's local source archive contains 269 files, including all
  161 reviewed component files and 71 test files; hashes match the working tree
  and release inventory. Documentation/readiness changes must leave those
  component and test hashes unchanged.
- The published rollback archive **4.2.1.51** was downloaded and checked against
  `d96348f2a3419baf841c3ce5ddb4bfcb6213ab78`: all 136 files match, including the
  exact 80-file component. Keep that release and annotated tag unchanged.
- Public GitHub metadata at preparation time: branch still at `d96348f`, latest
  stable 4.2.1.51; tag and release 5.0.0.1 absent. Recheck before publication.
- Final exact-commit offline reruns: HA2026.11.2 **1,493 tests +53 subtests**
  (also in a fresh environment from the CI dependency locks); historical
  HA2026.10.2 **1,475 +53**. One expected duplicate-source skip in each, no
  failures/errors. After readiness metadata changed, all **18 Marketplace
  tests** passed again, including publication checks and actual .51 file return.
- `publication_ready=true` records completed **local technical preparation**.
  It neither publishes anything nor grants permission to push, tag or install.
  Hosted CI and the eventual GitHub source archive remain publication gates.

Do not include a push of the host HA configuration repository: its pending commit
also contains unrelated scheduler changes and can deploy automatically. Marketplace
already owns SAT. No owner repair, integration removal or Git handoff is needed.

## Final local preflight

1. Commit this mixed publication metadata/documentation preparation. Require a
   clean tree and record the full final SHA. Review changes since `e7c2ecd`:
   only the reviewed publication files may differ. If code, tests, dependency
   locks or CI change, review and rerun the affected checks before proceeding.
2. Run `python scripts/check_release.py --for-publication --tag 5.0.0.1`.
   This validates version, destination, all component/test hashes, preserved
   core hashes, licenses and the built-in private-data checks.
3. Run the pinned `baseline` and `ha-2026.11.2` profiles in
   `.github/workflows/tests.yml`. The baseline is historical compatibility,
   not a lower supported HA version. Check full results, not only process start.
4. Create a local `git archive` from that exact SHA. Check every published path
   against Git, the 161-file inventory, licenses and `hacs.json`. Review that no
   credentials, device maps, logs, caches or local artifacts enter the archive.

## Later explicitly authorized publication

1. Recheck repository identity, existing tags/releases and remote branch. Keep
   upstream pushes disabled. Push only the reviewed `codex/hodko-sat` branch,
   without force, mirror, all-tags, upstream sync, merge or rebase. If the remote
   advanced, stop and inspect it; do not overwrite another change.
2. Require successful hosted CI for **both profiles on the exact final SHA**.
   A queued, skipped or cancelled job is not a pass. Inspect JUnit results and
   confirm no unexpected deselection, failure or skip.
3. Create annotated tag **5.0.0.1** on that same SHA; push only this tag. If it
   already exists, verify its peeled SHA rather than replacing it. Require
   successful tag CI, including `--for-publication --tag 5.0.0.1`.
4. Download the GitHub source archive for that tag. Compare every extracted
   file with `git archive` for the pinned SHA and all 161 component hashes.
   Compare content, not only ZIP bytes: GitHub may regenerate the container.
5. Prepare a draft titled **Hodko SAT 5.0.0.1** using RELEASE_NOTES.md. Convert
   relative documentation links to tagged GitHub links. Do not generate notes
   from unrelated upstream history or attach a second hand-built payload.
6. After CI/archive/notes acceptance, publish as stable/latest if that is within
   the explicit publication authorization. Verify tag SHA, version, release
   URLs and latest metadata. Publishing makes the update discoverable; it is
   not permission to install it, restart HA or alter installed settings.

## Failure before installation

Keep 4.2.1.51 installed. A failed check blocks release publication. Do not move
an existing tag or overwrite an already published archive. If a public release
is found defective, restore 4.2.1.51 as the advertised latest release through a
separately authorized metadata correction and prepare a new reviewed version;
this does not downgrade any installation automatically. A downloaded 5.0.0.1
requires the explicit [installation rollback](MIGRATION.md).

Historical first-publication instructions remain in the
[4.2.1.51 source](https://github.com/sochorf/hodko-sat/blob/4.2.1.51/PUBLISHING.md).
They must not be repeated for this normal fork update.
