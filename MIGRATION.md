# Candidate 5.0.0.1: subsequent fork update and return

Preparation only: no live operation is authorized here. Existing `sat` ownership
stays with `sochorf/hodko-sat` in Marketplace. Do not deploy this component from
ha-config, repeat the owner repair, uninstall the integration or create new entries.

1. After a separately approved publication, take and verify a fresh full backup
   of HA, installed 4.2.1.51 files, entries/registries, original report cache and
   Marketplace state. Record active/shadow options and disabled/ignored entries.
2. Verify the exact candidate release/CI/archive before the normal Marketplace
   update. Keep season/global permissions, 55 °C, room targets, TUV and Spook as-is.
   Schedule the one HA restart separately. No Z2M or OTGW restart is required.
3. After restart verify manifest 5.0.0.1 and full hashes. Existing entries should
   still be version10, same identities, same 13 saved heads and original PI/HR times.
   Automatic HEAT, progressing decisions, startup OFF/CS10 confirmation, modulation,
   CH ON/CS ACK and real CH/flame must be checked separately. Successful publish is
   not ACK. No native room PID/PWM or new water-heater entity should be running for
   these entries. Verify no new OFF repetitions, NoACK or counter errors.
4. Observe a natural TUV cycle and bounded recovery/mixing without forcing it.
   Record absence of a cycle as untested. Compare shadow before ramp using the same
   report times/history; equal final CS is not the sole test. Keep max request55;
   hotter actual residual DHW water is a separate quantity.

Return: use Marketplace's normal reinstall/version selection for **4.2.1.51**,
verify its exact 80-file inventory, then the agreed HA restart and startup checks.
The candidate deliberately leaves existing version10 entries, identifiers and
cache schema unchanged. Offline tests restore the genuine old file tree and verify
unchanged entry/registry inputs. They do not prove a physical/live restore.
If configuration/registries were changed outside that path, use the verified
backup instead of inventing a downgrade. Newly created native version13 entries
are not readable by .51 and require their own recorded rollback/backup.

Do not leave mixed old/new files; on partial install failure inspect Marketplace's
rollback result. HA should retain one boiler writer. Do not bypass safety by
repeated manual HEAT. The original first-install notes below are historical.

---

# Migration and rollback — preparation only

No live operation is authorized by this document. This local candidate does not
replace files in an installation and does not change the current Git owner.

## Before publication

Follow [PUBLISHING.md](PUBLISHING.md): review and commit the publication metadata,
verify the tested component/test hashes, then separately publish the public fork
and a matching `4.2.1.51` tag/release from the exact reviewed commit.
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
