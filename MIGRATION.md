# Migration and rollback — preparation only

No live operation is authorized by this document. This local candidate does not
replace files in an installation and does not change the current Git owner.

## Before publication

Review and commit this independent repository; run both pinned test profiles and
the release guard. Approve the repository identity and update publication policy;
only then separately create the public fork and a matching `4.2.1.51` tag/release.
Do not use `+hodko.N` for sequential updates: the standard version comparator
ignores build metadata. Later fork revisions use `4.2.1.52`, etc.

## Before the first installation

1. Confirm the running repaired .51, completed membership migrations, the exact
   active/shadow entries and original-time report cache. Record limits, transport,
   heating-season state and the independent DHW schedule. Do not export secrets
   into this repository. A missing migration is a stop condition, not permission
   to inject private device identities into the public package.
2. Obtain and verify a complete HA backup including code, Git index, registries,
   Marketplace records and cache. Preserve the exact private .51 package for return.
3. Prepare a separately approved maintenance window and Git ownership handoff on
   the installation host. The serving files must survive removal **from the index**.
   A deletion commit made on a workstation can remove the serving files on pull;
   do not use it for this transition. Temporarily stop automated Git deploy before
   the handoff, resolve its failure/reset behavior and validate the complete chain.
4. Use the standard Marketplace repository swap. Two repositories must not both
   own domain `sat`. Keep entries/entities; do not remove the integration itself.
   Uninstalling the old repository removes files. If the subsequent first install
   fails, that uninstall is not automatically undone by the new transaction.
5. Restart only as part of the agreed change. Verify loaded version and file
   inventory, automatic HEAT, progressing decisions, original-time PI/HR, fresh
   CH/CS confirmation, actual CH/flame and the saved limit. Observe a natural DHW
   cycle; if none occurs, record the untested return rather than forcing a cycle.
   Validate shadow as a pre-ramp proposal, not necessarily equal active CS.

## Return

Before restart, a failed first install needs explicit restoration of the preserved
.51 files and original ownership; never leave a partial component directory.
After restart, prefer the verified full backup if entry/cache/manager state changed.
Keep automated Git deploy disabled until its history and owner match the returned
installation. Standard subsequent fork-version downgrade is tested offline but
does not reverse external HA configuration changes or restore arbitrary backups.

Never activate a second boiler writer. Leave DHW automation and saved limits alone.
Offline transaction tests are not an authorization or proof of a live restore.
