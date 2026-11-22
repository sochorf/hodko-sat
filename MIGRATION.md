# Controlled update and return — 4.2.1.51 ↔ 5.0.0.1

Preparation only. Publication is not authorization to install, restart or change
live control. Marketplace remains the sole owner of `sochorf/hodko-sat`, domain
`sat`. Do not repeat owner repair, remove configurations, deploy SAT from the host
configuration Git repository or create another controller for the same boiler.

## Before a separately authorized installation

1. Verify the published 5.0.0.1 tag, exact commit, both hosted CI profiles and
   complete source archive using [PUBLISHING.md](PUBLISHING.md). Require supported
   HA ≥2026.11.2. Record the actual installed version and file inventory afresh.
2. Take and verify a fresh full HA backup, including current SAT files,
   entries/registries, Marketplace records and original-time report cache.
   Store private backup material outside Git. Record entry IDs and versions,
   disabled/ignored entries, active/shadow settings, member list, transport,
   heating-season/global permission and original report timestamps.
3. Preserve the known 4.2.1.51 archive and its 80-file inventory from commit
   `d96348f2a3419baf841c3ce5ddb4bfcb6213ab78`. Do not rely solely on the older
   backup taken before the original ownership migration.
4. Record 55 °C in SAT, shadow and OTGW, current room targets, independent DHW
   permission/schedule and disabled Spook. This update must not change them.
   Record recent MQTT/command-error counters and CH state for comparison.

## Installation and startup acceptance

1. Use the normal Marketplace version update in the existing fork, followed by
   the one separately agreed HA restart. No Z2M/OTGW restart, telemetry transport
   switch, manual parameter rewrite or routine manual OFF/HEAT is required.
   A HA restart is a service interruption; uninterrupted heat is not guaranteed.
2. Verify loaded version **5.0.0.1** and all **161** component hashes, including
   `compat`. Compare entries/registries/options to the fresh backup: existing
   version10 entries, identities and saved members must remain unchanged.
   No new native controller or water-heater writer may own these entries.
3. Verify automatic HEAT with season/global enabled, startup_ready and no fault.
   Check original PI/HR timestamps and remaining validity in active SAT and
   shadow. One eligible demanding zone suffices; do not wait for all heads.
   Restored data is not a new report and must not acquire a newer source time.
4. Check the startup sequence with fresh OFF/CS10, modulation and CH ON/CS
   confirmations. MQTT PUBACK alone is insufficient. Observe CS ≤55, CH enable,
   actual CH status, flame, supply and return temperatures separately. Evaluate
   startup within 10 minutes of valid inputs without a DHW block; diagnose
   concrete blockers instead of repeatedly issuing HEAT.
5. For 30–45 minutes inspect CH continuity, CS keepalive/ACK deadlines, new NoACK,
   counter/session errors, redundant OFF/CS10 and fresh room reports. A change
   of demand must not by itself replay the startup sequence. Compare shadow's
   proposal before the ramp using matching source times/history, not only CS.
6. Observe a natural DHW cycle and return/mixing within the existing configured
   windows. Do not force a cycle; if none occurs, record return as untested.
   Hot residual water is different from the CS request limit. Do not claim
   continuous monitoring from isolated samples; use history for intervening events.

A fault, loss of preserved configuration, second writer, CS above55 or persistent
startup/return failure requires a concrete diagnosis and the agreed rollback.
This document does not independently authorize a new live command or safety bypass.

## Return to 4.2.1.51

- With rollback explicitly authorized, select/reinstall **4.2.1.51** through the
  same Marketplace repository. No integration uninstall, owner repair, Git
  handoff or manual rewriting of registries is needed for unchanged version10
  entries. Verify **all 80 old component files**, with no leftover `compat` or
  other files introduced by 5.0.0.1, before the agreed HA restart.
- Recheck loaded 4.2.1.51, original entries/settings/cache, automatic HEAT,
  original-time demand, command ACK and real CH/flame; preserve 55, DHW and Spook.
  Repeat continuity and natural-DHW-return acceptance as applicable.
- If update/downgrade fails partway, inspect Marketplace's transaction rollback
  and full file hashes before restarting. Never accept a mixed component tree.
- New native version13 entries cannot be loaded by .51. If such entries were
  created, or settings/registries changed beyond this migration, restore the
  corresponding verified pre-update backup instead of assuming a code-only
  downgrade reverses them. Do not restore old command confirmation as fresh proof.

Offline tests use HA 2026.11.2's actual Marketplace transaction code and the genuine
4.2.1.51 tree. They cover failed-install recovery, old-file restoration and
entry/registry preservation. They do not prove physical heat delivery or a full
live backup restore. The historical first-ownership migration is recorded in the
[old version](https://github.com/sochorf/hodko-sat/blob/4.2.1.51/MIGRATION.md);
it is not the procedure for this subsequent update.
