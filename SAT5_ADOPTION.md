# SAT 5 adoption and startup repair — candidate 5.0.0.2

Adoption prepared on 22 November 2026; startup repair prepared on 23 November.
**5.0.0.1 was published and failed its live compatibility-platform startup.**
The .2 candidate is offline only. Its narrow initialization fix and cold-process
regressions are described in RELEASE_NOTES.md; no control policy is changed.

## Source and scope

- Native SAT5 source: Alexwijn/SAT tag 5.0.0, commit
  `a23f3d6a0150986d769ebd5b484d5c2209aae30d`.
- Compatibility source: published Hodko 4.2.1.51, commit
  `d96348f2a3419baf841c3ce5ddb4bfcb6213ab78`.
- `release-policy.json` pins the component inventory, tests, original sources
  and preserved core hashes. The .2 publication guard remains false; publishing
  and installation remain separately authorized actions.

This is an explicit two-policy package, **not conversion of PI/HR into SAT5 room
PID**. Exactly one policy is constructed per entry. SAT5 is available for new
standard configurations; existing installations retain their reviewed policy.
Native preheat, learned room PID, PWM and water-heater control are not silently
enabled for an existing Hodko installation.

## Boundary and preservation

| Entry | Runtime | Migration / writes |
|---|---|---|
| Existing entry version <=10 | `compat` adapter from .51 | Keep version10, settings, identifiers, cache keys and existing policy |
| `hodko_control_mode` / `hodko_ally` entry | `compat` + `ally` | PI/HR engine and operational machine; no SAT5 PID/PWM |
| `ally_shadow` | Existing read-only `ally` adapter | Sensor platforms only, no boiler or DHW writer |
| New native version13 | SAT5 | Native SAT5 configuration and identifier/preset behavior |

The pure `ally/kernel`, `control`, `evidence`, `machine`, shadow and telemetry
reader sources are byte-identical to .51. Runtime imports now point explicitly
to the preserved HA/transport adapter. The frozen adapter retains timing,
source timestamps, individual TTL, original session/sequence proof, counter wrap,
restart/HEAT recovery, command ACK, keepalive and bounded retry, OFF/CS10 suppression,
mixing and the original DHW recovery window. No safety window is retuned.

Saved 55 °C remains 55 °C in both active and shadow options; the physical OTGW
limit is not written by this preparation. This package does not switch HTTP
telemetry to MQTT. One eligible head still suffices; bad other heads cannot poison
its demand. Existing entities and their attributes remain at the same identities.

The public platform delegates before constructing SAT5 controllers or migrating
identifiers. Importing the compatibility options flow cannot register another
`sat` flow. New native entries cannot take a gateway identified as owned by an
enabled compatibility entry, including when that entry is waiting to start.
This guard is based on the configured gateway identifier, not physical discovery
of aliases for the same device. Do not add another writer under a different name.

PID/PWM services are registered once as entity-targeted services for both families.
Compatibility room-PID entities retain their operations; PI/HR entities explicitly
reject room-PID reset/PWM/preheat actions. The previous global legacy registration
must not overwrite the native entry's service handler. These service-boundary
changes do not alter the PI/HR control loop.

## Native SAT5 adaptations

Three narrowly tested fixes address the prior offline review:

1. Final CS clamp applies after continuous/PWM overrides, including water hotter
   than the configured maximum. The prior synthetic CS58/59 at max55 now sends55.
2. PWM ON with no return temperature uses the bounded requested setpoint rather
   than retaining CS10 indefinitely in that branch.
3. MQTT publication exceptions propagate: failed CS does not continue to PM25,
   MM and CH ON as if publication succeeded. This does not manufacture boiler ACK.

Upstream Sentry is disabled, as in .51; ownership and update URLs remain the fork's.
The SAT5 blueprints are included as optional upstream artifacts, never installed
or activated automatically. Missing Czech SAT5 labels fall back to English;
existing custom Ally/shadow labels remain. This is not a full new Czech translation.

The **native** policy still has its upstream semantics for room demand/freshness,
OFF/frost, PWM pump stops, repeated commands and DHW MQTT mapping. It is not
accepted as a replacement controller for this house. Our existing policy is
protected from those differences by routing, not by pretending upstream has the
same guarantees. Enabling a native policy would need a separate migration review.

## Regression evidence and limitations

Run `scripts/run_tests.py` with disabled sockets, fake HA/OTGW and temporary HOME.
Historical assertions and the 15 .49 golden command traces are unchanged. Adapter
imports move to `compat`; the actual HA platform remains the public `sat` platform.
The independent core hashes must not be regenerated to bless behavior changes.

`test_sat5_handoff.py` checks exact entry/registry preservation, custom-mode routing,
no native controller or new DHW platform, options UI, flow registration, duplicate
gateway refusal, failed-unload barrier and fixed core hashes. The Marketplace
suite downgrades through the real transaction implementation to all 80 actual
files from the pinned 4.2.1.51 Git tree, not a renamed copy of this candidate.

The upstream suite is separately namespaced; default mock entries are version13
to model native SAT5, whereas existing installations deliberately remain version10.
The prior 13 review probes are identified separately from upstream tests. Four
undesirable outcomes are changed to acceptance assertions for the explicit fixes;
the other probes document native-policy differences, not Hodko acceptance.

Final full suites: HA 2026.11.2 **1,493 passed +53 subtests**, historical HA 2026.10.2
**1,475 passed +53 subtests**, one expected duplicate-source skip in each. Both
used Python 3.14.8 and disabled sockets. AST comparison of the entire compatibility
adapter found no behavior deltas beyond the reviewed entry/service-boundary
changes in its initializer and climate services; other differences are import
relocation and unregistered legacy config-flow class. See RELEASE_NOTES.md. CI configuration is prepared but no hosted
CI was run in this task. These tests do not prove live startup, MQTT delivery,
hydraulics, a natural DHW return or complete disaster restore.

## Historical .1 committed-package acceptance — 22 November 2026

Reviewed code/test commit: `e7c2ecd0084f0778265a6ea5920890fce9bfd6d2`.
All 161 component and 71 test files match the earlier reviewed candidate exactly.
The final HA 2026.11.2 suite passed again in a fresh environment installed directly
from the hashed CI lock files, without dependency overlays: 1,493 tests +53
subtests, one expected skip, six deprecation warnings, no failures or errors.
The final baseline rerun passed 1,475 tests +53 subtests, one expected skip and
one deprecation warning. After readiness metadata changed, all 18 Marketplace
tests passed again, including the publication gate and actual .51 downgrade.
The whole local code archive has 269 files. The public 4.2.1.51 rollback archive
matches all 136 tracked files and all 80 component hashes at pinned commit `d96348f`.
No live HA read/write, deployment, tag, push or release occurred in this acceptance.
Only publication documentation/readiness metadata is prepared beyond the reviewed
code commit. Hosted CI must run after an explicitly authorized push; the future
published 5.0.0.1 archive must be checked before release/install acceptance.

## Startup regression discovered after .1 deployment

The old conftest imports compat.climate before the test body. That preloading
masked the package-level HA climate/sensor/number/binary_sensor aliases. A fresh
HA process instead resolved public platform forwarding to HA component setup.
The .2 tests use subprocesses with --noconftest and real HA platform creation;
they also cover shadow-first setup and require the dependent active diagnostics.
Only compat initialization plus version metadata change from .1. No preserved
core hashes are updated to bless this repair. .51 remains the return target.

## Maintenance decision

Keeping a fork currently provides a controlled Marketplace update path and the
established domain/identities. SAT5 does not reduce ownership of our PI/HR engine.
This compatibility boundary trades extra source for a reviewable, reversible
transition. Maintaining both policies indefinitely is a real cost. Future work
should choose either a supported external-demand interface upstream or an
independent Hodko integration with a controlled identity/configuration migration.
Do not merge upstream versions merely to advance a version number.
