# Hodko SAT

Local maintenance fork of [Smart Autotune Thermostat](https://github.com/Alexwijn/SAT),
combining **SAT 5.0.0** with an explicitly isolated **hodko.51 PI/HR** controller.
Version: **5.0.0.2** — compatibility startup repair. Publication is separate
from installation; use the verified release and migration procedure.
Published **5.0.0.1** has a confirmed compatibility-platform cold-start defect.
Keep **4.2.1.51** as the verified rollback release; do not retry .1 restarts as a fix.

Read [SAT5 adoption and limits](SAT5_ADOPTION.md) before reviewing this change.
Existing entries retain their controller, version10 schema, entity identities and
original-time cache. Only new native version13 entries use the upstream PID/PWM.
SAT5 features are not automatically activated for existing PI/HR installations.

The publication target is `sochorf/hodko-sat`. Publication and installation are
separate operations; preparing this tree does not publish or install it.
The existing fork publishes releases through standard Marketplace metadata.
See the [publication procedure](PUBLISHING.md) and [5.0.0.2 release notes](RELEASE_NOTES.md).

The `sat` integration domain, saved settings, entity identities and report-cache
keys remain unchanged. Existing water limits are preserved, including a saved
55 °C limit. The default for a new installation has not been raised.

## Control and observation

Ally mode evaluates original PI/HR reports, their individual age, permission and
fault state. The operating state machine owns startup, acknowledged commands,
bounded retries, CS keepalive, water limits and recovery after domestic hot water.
Original timestamps survive recovery; an MQTT PUBACK is not boiler confirmation.
One eligible room can request heat. A bad room must not block valid other rooms.

The optional shadow uses the same demand kernel as a diagnostic **proposal before
the active ramp**. It writes no boiler commands. Its independent state, history,
hysteresis and correction can differ from the active SAT. It is not a second owner.
The HA/transport adapters remain necessary; this is not an independent ESP32 controller.

## Package boundaries

The portable PI/HR decision kernel and operational machine remain byte-identical
with the published .51. The previous HA/transport adapter lives in `compat`.
The .2 repair removes colliding HA platform imports and initializes climate
before its dependent diagnostics, including when shadow starts first.
Public HA setup/platforms route exactly one policy, leaving existing room targets,
DHW ownership and saved 55 °C unchanged. Shadow remains observation only.

The native SAT5 path includes final CS limiting, missing-return fallback and MQTT
error propagation fixes. Its other policy semantics remain distinct from Hodko.
Do not enable it for an existing boiler without a separate migration acceptance.
No upstream Sentry reporting is enabled. See the detailed source/provenance and
maintenance tradeoff in [SAT5_ADOPTION.md](SAT5_ADOPTION.md).

## Validation

Use Python 3.14.8. CI installs hash-pinned dependencies and runs socket-disabled
tests against HA 2026.10.2 (historical regression) and 2026.11.2 (supported minimum).
Install the additional test-only `hodko/requirements-sat5.lock` with hash checking
after the HA lock. Run `python scripts/check_release.py` and `python scripts/run_tests.py` in that
environment. Tests cover original upstream behavior, startup, DHW return, lease
expiry, CS retry/keepalive, OFF suppression, counter wrap, original-time PI/HR
recovery, shadow isolation and the real HA Marketplace transaction code.
Cold-start regressions run each active/shadow order in a fresh interpreter,
without the legacy conftest imports, and require actual entities and automatic
HEAT intent while missing command evidence still prevents boiler enable.

The historical suite still tests the retired private membership migration in the
source project. Its 24 parametrized cases are explicitly retired here and replaced
by preservation/no-injection tests. No runtime test is silently deselected. The
Marketplace tests use synthetic installations and disabled sockets; they do not
prove live HA startup, hydraulic operation or delivery of a boiler command.

See [migration and rollback](MIGRATION.md), [upstream documentation](README.upstream.md)
and [license](LICENSE). Upstream credits and license notices are retained.
Local review is complete and `publication_ready=true`. The repair commit passed
both hosted CI profiles; the final publication commit and tag require their own
CI and public archive verification before release acceptance. Preparation is not
permission to publish, install, restart HA or change the boiler policy.


## Upstream maintenance watch

A separate GitHub Actions watcher is prepared for daily upstream releases,
development and issue/PR changes. It defaults to read-only preview and can
create deduplicated review issues after explicit activation. It cannot merge,
publish or change heating. See [setup, verification and rollback](UPSTREAM_WATCH.md).
