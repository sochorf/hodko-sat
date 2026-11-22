# Hodko SAT 5.0.0.2 — compatibility startup repair

Offline candidate, not published or deployed. This repairs the failed clean start
of existing PI/HR and shadow entries in published **5.0.0.1**.

## What changes

- Remove HA platform module aliases from the `compat` package. They shadowed the
  actual SAT submodules, routing platform setup to a two-argument HA function
  instead of the three-argument SAT platform. The resulting TypeError prevented
  climate, sensor, number and binary-sensor creation.
- Initialize the compatibility climate platform before its dependent diagnostics.
  When shadow loaded the sensor platform first, parallel active setup could skip
  active sensors before the climate instance existed.
- Add fresh-process startup regressions for active only, shadow only, and both
  orders. They execute real HA forwarding and entity registration without eager
  imports from the legacy conftest. Network I/O is isolated and boiler writes go
  only to the existing fake coordinator.

The complete integration delta from .1 is **one initialization file plus manifest
version metadata**. PI/HR policy, original timestamps/TTL, the operating machine,
command confirmation/retries/keepalive, DHW return/mixing, native SAT5 code and
configuration schema are unchanged. A saved 55 °C limit stays 55. No new writer,
settings migration, transport change or DHW ownership change is introduced.

## Evidence and limits

The four final cold-start cases fail against .1 with the live TypeError. Fixing
only the import leaves one failure when shadow starts first (missing active
sensors); the complete repair passes all four. Tests check actual entities,
automatic HEAT intent, unchanged version10/settings, shadow read-only behavior,
and no boiler enable without fresh evidence. A loaded config entry alone is not
accepted as success. Complete socket-disabled suites passed: **1,497 tests
+53 subtests on HA2026.11.2**, **1,479 +53 on historical HA2026.10.2**, with one
expected duplicate-source skip per profile and no failures/errors. These include
actual Marketplace downgrade to the exact 80-file .51 tree. The reviewed
inventory covers 161 component and 73 test files. Hosted CI for .2 is pending.

Minimum supported HA remains **2026.11.2**, Python 3.14.8. Offline tests do not
certify the deployed installation, actual flame, hydraulic heat or live DHW return.
The .1 test suite's eager platform imports masked this cold-start fault; its old
green CI is not evidence that the faulty release starts correctly.

## Installation and fallback

After separate publication/install approval, update the existing Marketplace fork
and perform the agreed HA restart. Verify every component file, actual active and
shadow entities, preserved inputs/limits, fresh command evidence and real heat.
Do not remove configurations, add a native SAT5 controller or redeploy through
ha-config. Keep the verified pre-.1 backup.

Rollback to **4.2.1.51** remains available through the same Marketplace repository:
existing version10 configuration and identities are unchanged. Newly created
native version13 entries need their own return plan. Published tags and archives
must not be overwritten. See [migration and return](MIGRATION.md) and
[publication gates](PUBLISHING.md).
