# Hodko SAT 5.0.0.1

Combines upstream SAT **5.0.0** with the isolated Hodko **.51 PI/HR** controller.
**Existing installations retain their controller.** This release does not switch
existing PI/HR or shadow entries to the native SAT5 room PID/PWM.

## Changes

- Existing version10 and Hodko PI/HR/shadow entries route to the preserved `compat`
  adapter. Configuration schema, entity identities, original-time report cache,
  saved permissions and water limits remain unchanged.
- The portable decision kernel and operating machine are byte-identical to .51.
  Startup/HEAT recovery, command confirmation, CS keepalive/retry, suppression of
  redundant OFF/CS10, counter wrap and DHW return/mixing retain their behavior.
- New standard version13 configurations use SAT5. Its final CS clamp,
  missing-return-temperature fallback and propagation of failed MQTT publication
  are corrected. Native policy still differs from Hodko and requires a separate
  review before replacing an existing house controller.
- Shared services target their own entity; PI/HR rejects room PID/PWM/preheat
  operations. A native entry cannot take the same configured gateway identifier
  already owned by an enabled compatibility entry. Aliases for one physical
  gateway still require operator review.
- No automatic DHW ownership change, telemetry transport switch, upstream Sentry
  reporting or blueprint activation. Missing new Czech labels fall back to English.

## Compatibility and validation

Minimum supported Home Assistant: **2026.11.2**. Offline suites on Python 3.14.8:
**1,493 passed +53 subtests** on HA 2026.11.2; **1,475 +53** on the historical
HA 2026.10.2 profile. Each has one expected duplicate-source skip, covered by fixed
core hashes and the preserved historical golden traces. No test failures/errors.

Tests use disabled sockets and fake HA/boiler interfaces, with migration/identity,
service isolation, native SAT5 and actual Marketplace update/downgrade coverage.
Release publication also requires successful hosted CI on the exact tag and
verification of all **161** integration files in the GitHub source archive.
Offline results do not certify live startup, hydraulic operation or a full restore.

## Update and rollback

Use the existing fork through Marketplace after a fresh verified backup and a
separately agreed HA restart. Existing version10 entries retain the .51 policy;
no integration removal or Marketplace owner repair is needed. A saved 55 °C limit
stays 55; this release does not write the physical OTGW maximum.

Return through the same repository to **4.2.1.51**, restoring its exact 80-file
component. Unchanged version10 configurations are preserved. Newly created native
version13 entries or unrelated registry changes require their own backup/return
plan and are not covered by a code-only downgrade.

See [source and policy details](SAT5_ADOPTION.md) and
[controlled update and rollback](MIGRATION.md). Publishing this release does not
install it or authorize changes to the boiler or independent DHW automation.
