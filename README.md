# Hodko SAT

Local maintenance fork of [Smart Autotune Thermostat](https://github.com/Alexwijn/SAT),
based on **4.2.1**, carrying the reviewed **hodko.51** implementation.
Candidate version: **4.2.1.51**. SAT 5 is not part of this release.

The publication target is `sochorf/hodko-sat`. Publication and installation are
separate operations; preparing this tree does not publish or install it.
The release policy's `published_repository` field records the reviewed destination,
not evidence that a GitHub repository or release already exists. See
[publication procedure](PUBLISHING.md) and [first release notes](RELEASE_NOTES.md).

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

## Differences from the private .51 package

- Five inactive local pilot/replay/map files are excluded; runtime modules remain.
- Completed, house-specific TV2/WC membership migrations are retired. Saved members
  and markers are preserved; this release will not add devices to an unrelated home.
  Complete these migrations on .51 before switching. Regressions check 13 saved
  members, explicit removals, limits and both SAT/shadow storage forms.
- The legacy error-reporting option no longer initializes upstream Sentry. It logs
  a local warning when enabled. No replacement external telemetry is installed.
- Version/owner/documentation metadata, offline tests and release checks are added.

The decision kernel, operating machine, runtime, HTTP/MQTT transports, command
timings and safeguards are **byte-identical to .51**. `release-policy.json`
records the exact baseline and the three reviewed changed component files.

## Validation

Use Python 3.14.8. CI installs hash-pinned dependencies and runs socket-disabled
tests against HA 2026.10.2 (historical regression) and 2026.11.2 (supported minimum).
Run `python scripts/check_release.py` and `python scripts/run_tests.py` in that
environment. Tests cover original upstream behavior, startup, DHW return, lease
expiry, CS retry/keepalive, OFF suppression, counter wrap, original-time PI/HR
recovery, shadow isolation and the real HA Marketplace transaction code.

The historical suite still tests the retired private membership migration in the
source project. Its 24 parametrized cases are explicitly retired here and replaced
by preservation/no-injection tests. No runtime test is silently deselected. The
Marketplace tests use synthetic installations and disabled sockets; they do not
prove live HA startup, hydraulic operation or delivery of a boiler command.

See [migration and rollback](MIGRATION.md), [upstream documentation](README.upstream.md)
and [license](LICENSE). Upstream credits and license notices are retained.
Future SAT 5 adoption is a separate adapter/regression project, not an automatic merge.
