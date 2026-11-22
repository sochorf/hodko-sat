# Hodko SAT 5.0.0.1 — unpublished candidate

Adopts the pinned upstream SAT 5.0.0 code while routing existing version10 and
Hodko PI/HR/shadow entries to the preserved .51 adapter. Original PI/HR times,
permissions, command confirmation, keepalive/retry, startup/HEAT recovery and
DHW recovery remain. The portable policy and operational machine are unchanged.
No automatic switch to native room PID/PWM, target synchronization or DHW control.

New native SAT5 configurations receive final CS limiting, a bounded missing-return
fallback and MQTT publication error propagation. Native policy remains distinct
and is not accepted as a replacement for this house's PI/HR policy.

Added migration/identity, ownership, shared-service and actual .51 downgrade tests;
expanded the suite with upstream SAT5 tests. Minimum supported HA remains 2026.11.2.
Offline results: **1,493 passed + 53 subtests** on HA2026.11.2 and
**1,475 passed + 53 subtests** on the historical HA2026.10.2 profile, Python3.14.8.
Each has one pre-existing skip of a duplicate-source check (the standalone fork
has only one core source). Fixed independent hashes and historical golden traces
remain checked. No failures or errors. Warnings are deprecations in test/dependency
APIs; hosted CI has not run. Publication is gated off.

See [adoption details](SAT5_ADOPTION.md) and [controlled update/return](MIGRATION.md).
A successful offline test is not proof of live boiler/hydraulic behavior.

---

## Historical first release

# Hodko SAT 4.2.1.51

First maintenance-fork release, based on upstream SAT 4.2.1 and the reviewed
hodko.51 implementation. **This release does not include SAT 5.**

## Included behavior

- Original-time Ally PI/HR processing, individual freshness and recovery, room
  permissions and isolated bad-room handling; one eligible room can request heat.
- Automatic heating-season startup and bounded recovery, boiler command
  confirmation distinct from MQTT PUBACK, bounded CS retry and keepalive.
- Suppression of redundant OFF/CS10, DHW-return recovery and counter-wrap handling.
- A read-only shadow proposal before the active ramp, using the same demand kernel.

## Packaging changes from private hodko.51

The decision kernel, operating machine, runtime and HTTP/MQTT adapters are unchanged.
The 80-file integration keeps 77 files byte-identical; the remaining changes are
version/ownership metadata, retirement of completed house-specific TV2/WC setup
migrations, and disabling upstream Sentry reporting. Five inactive local
pilot/replay/map files are omitted. No private device map is distributed.

Domain `sat`, saved settings, entity identities and original report-cache keys
remain unchanged. Existing water limits, including a configured 55 °C limit, are
preserved. This release does not change defaults or DHW automation and does not
switch the telemetry transport. A new installation needs its own reviewed settings.

## Validation and compatibility

Minimum declared Home Assistant: **2026.11.2**. Local offline validation of the
reviewed component and test bytes: **812 tests + 53 subtests** passed on HA 2026.11.2;
**795 tests + 53 subtests** passed on the historical HA 2026.10.2 test environment.
One standalone-only duplicate-source check is skipped and covered by the builder.
The suite includes startup/DHW/timeout/counter-wrap regressions and Marketplace
update/downgrade/failed-install recovery tests. All use disabled sockets and fake
HA/boiler interfaces. Publication requires successful hosted CI for the release SHA.

## Migration and rollback

This first release requires a controlled ownership migration from a Git-managed
private installation. Confirm saved members and completed membership migrations
first. Do not remove the SAT configuration entries or install two domain owners.
Take a verified full backup before a separately approved installation window.
Uninstall of the old repository is not rolled back automatically when the first
fork installation fails. Return to the exact repaired private .51 or the verified
backup, not unmodified upstream 4.2.1. See [MIGRATION.md](MIGRATION.md).

These offline results do not certify live hydraulic operation, delivery of boiler
commands or a complete HA backup restore. Publishing this release does not deploy it.
