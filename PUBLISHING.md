# Candidate 5.0.0.1 — publication not authorized

This task prepares local changes only. `release-policy.json` has
`publication_ready=false`; publication/tag validation must fail until a separate
review explicitly opens that gate. Do not create a tag, release or push now.

Before any later authorized publication: commit the entire mixed code/test/docs
change; run both pinned test profiles and release inventory checks on the exact
commit; review the 5.0.0.1 notes and rollback. Publish through the existing
`sochorf/hodko-sat` repository, never overwrite 4.2.1.51. Verify hosted branch/tag CI
and all source-archive hashes. Publishing and installing remain separate actions.
SAT5 adoption does not require another Marketplace owner repair.

The following is the retained historical first-release procedure, **not the
instructions for this candidate**.

# First publication — 4.2.1.51

Destination: **sochorf/hodko-sat**, a public fork of Alexwijn/SAT.
Release branch: **codex/hodko-sat**. Tag: **4.2.1.51** (exactly the manifest).
The local policy records the reviewed destination; no publication is performed by
the build, checks, tests or CI. The first release notes are in RELEASE_NOTES.md.

## Local preflight

1. Commit the publication metadata and docs. The component, tests, dependency
   locks and original-time behavior must match the reviewed 3ec1b49 baseline.
   Require a clean working tree, verify the final full SHA and inspect its diff.
2. Run `python scripts/check_release.py --for-publication --tag 4.2.1.51`.
   Verify no private paths, credentials, device maps, cache, logs or test artifacts
   are tracked. Preserve upstream copyright and license history.
3. Keep upstream pushes disabled. Do not configure a deployment remote or a
   production HA workflow. Do not push the host HA configuration repository as a
   way to publish this integration.

## Explicitly approved GitHub publication

1. Verify the signed-in account and whether `sochorf/hodko-sat` already exists.
   Reuse only a verified correct fork; never overwrite an unrelated repository.
   If absent, create a real fork of Alexwijn/SAT named `hodko-sat`, with only its
   default branch copied. GitHub may initially copy an upstream SAT 5 branch;
   this is not the release branch and must not become our installable candidate.
2. Configure **origin** to the verified fork URL. Push only the reviewed
   `codex/hodko-sat` branch, without `--force`, `--mirror` or `--tags`.
   Set it as the fork's default branch and verify its full SHA. Do not merge,
   rebase, sync the fork from upstream, or publish upstream tags/releases.
3. Enable the reviewed offline Actions workflow if GitHub initially disables
   workflows in the fork. Run both profiles and inspect their logs for the exact
   pushed SHA. A queued, cancelled or skipped job is not a successful result.
   Ensure Issues is enabled for the manifest's support URL; no issue is created.
4. Only after successful branch CI, create the new annotated `4.2.1.51` tag on
   that same SHA and push **that tag alone**. If the tag already exists, check its
   peeled SHA and never replace it. Wait for successful tag CI, including the
   publication/tag check. Any subsequent code commit needs a fresh review/run.
5. Create a draft release for the verified existing tag. Title: `Hodko SAT 4.2.1.51`.
   Use RELEASE_NOTES.md verbatim, adapting its relative links to the tagged files
   on GitHub. Do not use auto-generated notes pulling in unrelated upstream work.
   Publish as a stable release, not prerelease, and mark it latest only after the
   final review of tag, CI, notes and source archive. No custom binary ZIP is needed.

## Read-only post-publication acceptance

- Confirm repository fork-parent, default branch, branch/tag/peeled commit SHA,
  stable release, version and documentation/issue URLs. A repository page is not
  proof that a release exists or that Actions succeeded.
- Download the published source archive; compare all 80 integration files with
  `release-policy.json` and confirm both licenses, manifest and `hacs.json`.
  Compare extracted file hashes, not only the ZIP checksum: GitHub can regenerate
  archive bytes. No personal fixtures, credentials, cache or logs may be present.
- Confirm Marketplace can discover the dedicated release through normal repository
  metadata before any separately approved installation. This step must not install
  it, reload SAT, change HA ownership or touch heating/DHW settings.

## Failure and return before deployment

If branch/tag CI or archive checks fail, stop before release publication and fix
the local candidate. Do not overwrite an existing release tag. If a release is
already public, clearly mark the problem and prepare a reviewed follow-up version;
do not treat editing release text as a rollback of downloaded code. The current
private .51 remains deployed throughout this publication process.

Sources: [GitHub forking](https://docs.github.com/en/rest/repos/forks),
[GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).
