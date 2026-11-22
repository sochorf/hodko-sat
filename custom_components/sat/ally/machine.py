"""SAT operational state machine, independent of HA and concrete transports.

The host supplies clocks, permissions and I/O ports. Commands are immutable
intents; only the adapter executes them. Async ordering/locks/timeouts deliberately
match hodko.49, including rechecking inputs after every awaited write. No PID,
network client, HA state object, persistence backend or background task lives here.
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import logging
from math import isfinite
from typing import Protocol

from .control import Engine, Reports, Ramp
from .evidence import Telemetry, DHWRecovery, utc, observation_utc

# Preserve the existing diagnostic logger and operating constants.
_LOGGER = logging.getLogger('custom_components.sat.ally.runtime')
MINIMUM_SETPOINT = 10
CS_REFRESH_INTERVAL = 15  # Reserve time for a read and one bounded QoS retry.
CS_REFRESH_DEADLINE = 55
STARTUP_RECOVERY_TIMEOUT = 600
STARTUP_RETRY_INTERVAL = 30
STARTUP_REARM_INTERVAL = 300  # After the bounded boot attempts, one safe probe per 5 min.
HEATING_SEASON_ENTITY = "input_boolean.heating_season"
PASSIVE_WATER_STOPS = frozenset('input_or_transport_error:' + reason for reason in (
    'DHW priority', 'Boiler water outside experimental guard',
    'DHW residual water at or above ceiling'))


@dataclass(frozen=True)
class Command:
    kind: str  # ch / cs / mm; the adapter refuses unknown kinds.
    value: bool | float


class Ports(Protocol):
    control_lock: asyncio.Lock
    closing: bool
    host_running: bool
    heat_requested: bool
    connected: bool
    maximum_setpoint: float
    setpoint: float
    write_timeout: float

    def wall_time(self) -> float: ...
    def monotonic(self) -> float: ...
    def permission(self, key: str) -> bool | None: ...
    def restore_heat(self) -> None: ...
    def state_changed(self) -> None: ...
    def save_reports(self) -> None: ...
    def mqtt_disconnected(self, exc: Exception) -> bool: ...
    def transient_read_error(self, exc: Exception) -> bool: ...
    async def read_telemetry(self): ...
    async def send(self, command: Command) -> None: ...


class OperationalMachine:
    def __init__(self, ports: Ports, settings, policy, limits, modulation_limit):
        self.ports = ports
        self.settings = deepcopy(settings)
        self.policy = policy
        self.limits = limits
        members = self.settings['members']
        self.engine = Engine({m['name']: m['zone'] for m in members.values()}, self.policy)
        self.reports = Reports({k: m['name'] for k, m in members.items()}, self.ports.wall_time())
        self.ramp = Ramp(self.policy.minimum_water, self.policy.maximum_water, self.ports.monotonic(),
                         restart_delay=self.limits.restart_delay, rise_per_minute=self.limits.ramp_rate)
        self.telemetry = Telemetry()
        self.dhw_recovery = DHWRecovery(self.limits)
        self.revision = 0
        self.status = 'off'
        self.last_error_stage = None
        self.monitor_error = None
        self.command_writes = {"ch_on": 0, "ch_off": 0, "cs": 0}
        self.decision = None
        self.pending = None
        self.last_stop_sent = None
        self.last_stop_reason = None
        self.stop_publish_pending = False
        self.stop_confirmation_after = None
        self.last_cs_refresh = None
        self.last_ack = None
        self.cs_keepalive_writes = 0
        self.cs_publish_failures = 0
        self.cs_retry_attempts = 0
        self.cs_retry_successes = 0
        self.telemetry_read_failures = 0
        self.cs_write_seconds = None
        self.cs_write_budget = None
        self.cs_write_overrun = None
        self.last_cycle_mono = None
        self.cycle_gap_seconds = None
        self.command_recovery_attempts = 0
        self.last_cycle_at = None
        self.last_decision_at = None
        self.decision_sequence = None
        self.tripped = None
        self.last_wall = self.ports.wall_time()
        self.last_mono = self.ports.monotonic()
        self.closed = False
        self.startup_ready = True
        self.starting = False
        self.restart_restored = False
        self.startup_recovery_deadline = None
        self.startup_retry_at = 0
        self.startup_resume_heat = False
        self.season_start_pending = False
        self.season_start_deadline = None
        self.boot_ack_pending = False
        self.boot_verify_deadline = None
        self.boot_verify_attempts = 0
        self.startup_rearm_after = None
        self.startup_rearm_barrier = None
        self.startup_rearm_pending = False
        self.startup_rearm_attempts = 0
        self.startup_error = None
        self.report_restore_status = 'empty'
        self.modulation_limit = modulation_limit
        if type(self.modulation_limit) not in (int, float) or not isfinite(self.modulation_limit) or not 0 < self.modulation_limit <= 100:
            raise ValueError('PI/HR requires a positive modulation limit at most 100')
        self.modulation_since = None
        self.modulation_confirmed = False

    async def poll(self):
        return await self.ports.read_telemetry()

    def save_reports(self):
        self.ports.save_reports()

    def invalidate(self, *, preserve_reports=False, cancel_startup_resume=False):
        if cancel_startup_resume:
            self.startup_rearm_after = None
            self.startup_rearm_barrier = None
            self.startup_rearm_pending = False
            self.command_recovery_attempts = 0
            self.boot_verify_deadline = None
            self.boot_ack_pending = False
            self.startup_resume_heat = False
            self.season_start_pending = False
        self.dhw_recovery.reset(self.ports.monotonic())
        self.last_stop_reason = None
        self.last_ack = None
        self.last_cs_refresh = None
        self.revision += 1
        self.modulation_since = None
        self.modulation_confirmed = False
        if not preserve_reports:
            self.reports.invalidate(self.ports.wall_time())
            self.report_restore_status = 'invalidated'
            self.save_reports()
        self.engine.reset()
        self.decision = None
        self.ramp.stop(self.ports.monotonic())


    async def prepare_heat(self):
        """Start a new OFF acknowledgement transaction after an idle period."""
        async with self.ports.control_lock:
            if self.closed or self.tripped:
                raise ValueError('PI/HR runtime unavailable; turn OFF before retrying')
            # A mode change is not a source outage. Preserve original report
            # timestamps/validity; only command evidence must be reacquired.
            self.invalidate(preserve_reports=True)
            self.telemetry = Telemetry()
            self.pending = None
            self.last_stop_sent = None
            try:
                await self.stop('starting_waiting_for_off_confirmation')
            except BaseException:
                self.tripped = 'heat_start_stop_failed'
                self.status = self.tripped
                raise


    def resume_for_season(self):
        """Under control lock; restore permission, never bypass the normal gate."""
        if not self.season_start_pending or self.season_start_deadline is None:
            return
        if self.closed or self.tripped or self.ports.monotonic() >= self.season_start_deadline:
            self.season_start_pending = False
            return
        season = self.ports.permission(HEATING_SEASON_ENTITY)
        if season is False:
            self.season_start_pending = False
            return
        allowed = self.ports.permission(self.settings['enabled_entity'])
        if (self.startup_ready and season is True
                and allowed is True):
            self.season_start_pending = False
            self.startup_resume_heat = False
            self.ports.restore_heat()
            self.status = 'season_start_waiting_for_live_inputs'
            self.ports.state_changed()


    async def recover_startup(self):
        """Called under the control lock; only OFF/low CS may be sent here."""
        now = self.ports.monotonic()
        if now >= self.startup_recovery_deadline:
            self.startup_recovery_deadline = None
            self.tripped = 'startup_recovery_expired'
            self.status = self.tripped
            return
        if now < self.startup_retry_at:
            return
        self.startup_retry_at = now + STARTUP_RETRY_INTERVAL
        try:
            self.telemetry.observe(await self.poll(), self.ports.monotonic())
            if self.telemetry.value('faultindicator', self.ports.monotonic()) != 'Off':
                self.startup_recovery_deadline = None
                self.tripped = 'startup_boiler_fault'
                self.status = self.tripped
                return
            if self.pending is None:
                # Do not acknowledge the initial failed transaction from cache.
                await self.stop('startup_waiting_for_off_confirmation')
                return
            self.confirm(self.ports.monotonic())
            if self.pending is not None:
                await self.stop('startup_waiting_for_off_confirmation')
                return
            self.startup_recovery_deadline = None
            self.startup_ready = True
            self.status = 'off'
            if self.startup_resume_heat:
                self.ports.restore_heat()
                self.restart_restored = True
                self.status = 'restart_waiting_for_live_inputs'
            # Next normal cycle must still obtain fresh inputs and RM proof.
        except Exception as exc:
            self.startup_error = type(exc).__name__
            _LOGGER.warning("SAT startup recovery failed: %s", type(exc).__name__)
            if self.pending is not None and self.ports.monotonic() - self.pending[2] >= 90:
                # Missing telemetry must not suspend the ACK deadline.
                self.tripped = 'command_not_confirmed'
            if self.tripped:
                self.startup_recovery_deadline = None
                self.status = self.tripped
            else:
                self.status = 'startup_retry_wait'


    def gate(self):
        return (self.startup_ready and not self.closed and self.ports.heat_requested and self.tripped is None and
                self.ports.permission(self.settings['enabled_entity']) is True)


    async def stop(self, reason):
        self.status = reason
        # Ordinary waiting is not a new stop transaction. Retain only a proven,
        # currently fresh OFF; operator/safety stops and pending ON never skip.
        # The passive reason is not a new OFF transaction: e.g. no demand ->
        # DHW priority is already safe while fresh OFF/CS<=10 remains proven.
        # Only these reviewed waiting/water reasons may reuse that proof;
        # stale/unsafe evidence, operator stops and pending ON never skip.
        reuse = (reason in ('waiting_for_modulation_confirmation', 'off_or_restart_delay') or
                 reason in PASSIVE_WATER_STOPS)
        if reuse and self.confirmed_off():
            self.last_cs_refresh = None
            self.ramp.stop(self.ports.monotonic())
            self.ports.setpoint = MINIMUM_SETPOINT
            return
        self.last_ack = None
        self.last_cs_refresh = None
        self.ramp.stop(self.ports.monotonic())
        self.ports.setpoint = MINIMUM_SETPOINT
        # An ambiguous publish is an attempt too. Bound retries to the original
        # 90s OFF transaction, at most once per 30s, including failed writes.
        # A pending ON is always interrupted immediately. After OFF expires,
        # recovery must read fresh safe evidence before starting a new transaction.
        now = self.ports.monotonic()
        if self.pending is not None and not self.pending[0]:
            if now-self.pending[2] >= 90:
                return
            if self.last_stop_sent is not None and 0 <= now-self.last_stop_sent < 30:
                return
        self.sent(False, MINIMUM_SETPOINT)
        self.last_stop_sent = now
        self.last_stop_reason = reason
        # A failed/partial write is not a completed OFF transaction. Even when
        # telemetry is already safe, require both publishes to succeed followed
        # by new CH/CS evidence. Preserve the original bounded ACK deadline.
        self.stop_publish_pending = True
        # Already inside the common control lock; do not recursively call shutdown.
        errors = []
        try:
            try:
                async with asyncio.timeout(self.ports.write_timeout):
                    self.command_writes["ch_off"] += 1
                    await self.ports.send(Command('ch', False))
            except Exception as exc:
                errors.append(exc)
        finally:
            try:
                async with asyncio.timeout(self.ports.write_timeout):
                    self.command_writes["cs"] += 1
                    await self.ports.send(Command('cs', MINIMUM_SETPOINT))
            except Exception as exc:
                errors.append(exc)
        if errors:
            # Do not let a later disconnect hide a prior schema/auth failure.
            permanent = [exc for exc in errors if not
                         (isinstance(exc, (TimeoutError, OSError)) or self.ports.mqtt_disconnected(exc))]
            raise (permanent or errors)[0]
        self.stop_confirmation_after = self.ports.monotonic()
        self.stop_publish_pending = False


    def confirmed_off(self):
        """Use fresh safe telemetry plus a completed OFF transaction, never cache alone."""
        if self.stop_publish_pending or self.pending is not None or not self.last_ack or self.last_ack['on'] or self.tripped:
            return False
        try:
            now = self.ports.monotonic()
            return (self.telemetry.value('chenable', now) == 'Off' and
                    self.telemetry.value('faultindicator', now) == 'Off' and
                    0 <= self.telemetry.number('controlsetpoint', now) <= MINIMUM_SETPOINT)
        except (ValueError, TypeError, KeyError):
            return False


    def sent(self, on, water):
        if self.pending is None or self.pending[0] != on:
            self.pending = (on, water, self.ports.monotonic())


    def confirm(self, now):
        # Expiry must run even when fresh telemetry is missing. Otherwise stale
        # input exceptions can leave a pending command alive indefinitely.
        if self.pending is not None and now-self.pending[2] >= 90:
            self.tripped = 'command_not_confirmed'
            raise ValueError(self.tripped)
        enabled = self.telemetry.value('chenable', now)
        water = self.telemetry.number('controlsetpoint', now)
        if enabled not in ('On', 'Off'):
            raise ValueError('CH enable unknown')
        if self.pending:
            on, target, since = self.pending
            if not on and self.stop_publish_pending:
                return
            receipts = [self.telemetry.fields[k][2] for k in ('chenable', 'controlsetpoint')]
            # OFF is a safe-state acknowledgement, not an exact CS echo:
            # fresh CH Off plus CS in [0,10] is safe even if OTGW reports zero.
            # ON still requires the exact requested target (existing tolerance).
            target_matches = abs(target-water) <= 0.6 if on else 0 <= water <= MINIMUM_SETPOINT
            fresh = min(receipts) > since if on else min(receipts) >= since
            if not on and self.stop_confirmation_after is not None:
                fresh = fresh and min(receipts) >= self.stop_confirmation_after
            if fresh and (enabled == 'On') == on and target_matches:
                self.last_ack = {'on': on, 'water': target, 'at': observation_utc(self.ports.wall_time())}
                self.pending = None
            elif now-since >= 90:
                self.tripped = 'command_not_confirmed'
                raise ValueError(self.tripped)


    async def write_heat_cs(self, target, *, keepalive=False, retry=False):
        """Refresh PIC's <=60s CS lease without manufacturing a new ACK epoch.

        Called under the control lock after the normal safety/demand checks.
        A publish failure cannot advance the successful refresh timestamp.
        The timestamp precedes the write: broker ACK delay is not extra lease.
        """
        started = self.ports.monotonic()
        budget = self.ports.write_timeout
        if self.last_cs_refresh is not None:
            remaining = CS_REFRESH_DEADLINE - (started-self.last_cs_refresh)
            if remaining <= 0:
                raise ValueError('CS keepalive deadline exceeded')
            # A late cycle must not spend the whole remaining lease on its
            # first publish. Reserve up to five seconds for the single retry;
            # neither attempt may extend the original deadline. An unconfirmed
            # start has no retry permission and keeps its existing budget.
            if not retry and self.last_ack and self.last_ack['on']:
                remaining -= min(5.0, remaining / 2)
            budget = min(budget, remaining)
        self.cs_write_budget = budget
        try:
            async with asyncio.timeout(budget):
                self.command_writes["cs"] += 1
                if keepalive:
                    self.cs_keepalive_writes += 1
                await self.ports.send(Command('cs', target))
            if self.ports.monotonic()-started >= budget:
                raise TimeoutError('CS refresh completed after deadline')
            self.last_cs_refresh = started
        except Exception:
            self.cs_publish_failures += 1
            raise
        finally:
            self.cs_write_seconds = self.ports.monotonic()-started
            # asyncio deadlines are cooperative. Distinguish the assigned
            # budget from late cancellation/completion on a stalled HA loop.
            self.cs_write_overrun = max(0, self.cs_write_seconds-budget)


    async def refresh_heat_cs(self, target):
        if self.last_cs_refresh is None:
            return
        age = self.ports.monotonic() - self.last_cs_refresh
        if age < 0 or age >= CS_REFRESH_DEADLINE:
            raise ValueError('CS keepalive deadline exceeded')
        if age >= CS_REFRESH_INTERVAL:
            await self.write_heat_cs(target, keepalive=True)


    async def acquire_modulation(self, now):
        """Own RM explicitly; never enable CH on inherited/cache-only evidence.

        RM is a ceiling, not an OFF command. Keep it during DHW and use CH/CS
        for stopping. A mismatch while running first stops CH, then reacquires.
        """
        try:
            value = self.telemetry.number('maxrelmodlvl', now)
            receipt = self.telemetry.fields['maxrelmodlvl'][2]
        except ValueError:
            value, receipt = None, None
        matches = value == self.modulation_limit
        if self.modulation_confirmed and matches:
            return True
        if self.modulation_confirmed:
            self.modulation_since = None
        self.modulation_confirmed = False
        if self.modulation_since is not None:
            if matches and receipt is not None and receipt >= self.modulation_since:
                self.modulation_confirmed = True
                self.modulation_since = None
                return True
            if now - self.modulation_since >= 90:
                self.tripped = 'modulation_not_confirmed'
                raise ValueError(self.tripped)
        await self.stop('waiting_for_modulation_confirmation')
        # Start deadline before publish; retries and publish failures cannot
        # manufacture an acknowledgement or extend the deadline.
        if self.modulation_since is None:
            self.modulation_since = self.ports.monotonic()
        async with asyncio.timeout(self.ports.write_timeout):
            await self.ports.send(Command('mm', self.modulation_limit))
        return False


    def frame(self, wall, mono):
        heads = deepcopy(self.reports.heads)
        for member in self.settings['members'].values():
            value = self.ports.permission(member['eligible_entity'])
            heads.setdefault(member['name'], {})['eligible'] = {
                'value': value, 'observed_at': observation_utc(wall), 'evidence': 'policy_evaluation'}
        outside = self.telemetry.number('outsidetemperature', mono)
        return {'session_id': f'{self.reports.session}:{self.reports.segment}',
                'session_started_at': utc(self.reports.epoch), 'heads': heads,
                'outside': {'value': outside, 'evidence': 'otgw_report',
                            'observed_at': observation_utc(wall - (mono-self.telemetry.fields['outsidetemperature'][2]))}}


    def refresh_demand(self):
        mono, wall = self.ports.monotonic(), self.ports.wall_time()
        self.decision = self.engine.evaluate(self.frame(wall, mono), wall)
        self.last_decision_at = observation_utc(wall)
        self.decision_sequence = self.reports.seq


    def inputs_allow_continuation(self, revision):
        if not self.gate():
            return False
        if revision != self.revision:
            # A fresh report is not a stop request. Re-evaluate policy/validity
            # synchronously before the next write; invalid/no demand still stops.
            self.refresh_demand()
            return self.decision.heat is True
        return True


    def hold_verified_heat(self):
        """Keep an existing ON through a short transport failure, never send/ACK from cache."""
        now, wall = self.ports.monotonic(), self.ports.wall_time()
        if (not self.gate() or self.tripped or not self.modulation_confirmed or
                (self.last_cs_refresh is not None and not 0 <= now-self.last_cs_refresh < CS_REFRESH_DEADLINE) or
                abs((wall-self.last_wall)-(now-self.last_mono)) > 5):
            return False
        if self.pending is not None:
            if now-self.pending[2] >= 90:
                self.tripped = 'command_not_confirmed'
                return False
            if not self.pending[0]:
                return False
        try:
            if any(self.telemetry.value(k, now) != v for k, v in
                   [('chenable', 'On'), ('faultindicator', 'Off'), ('dhwmode', 'Off')]):
                return False
            water = self.telemetry.number('boilertemperature', now)
            cs = self.telemetry.number('controlsetpoint', now)
            cap = min(self.policy.maximum_water, float(self.ports.maximum_setpoint))
            if (not isfinite(cap) or not 10 < cs <= cap or
                    not 0 <= water < self.policy.maximum_water + self.limits.water_margin or
                    self.telemetry.number('maxrelmodlvl', now) != self.modulation_limit):
                return False
            # Never accumulate sustained-demand correction through an unobserved gap.
            self.engine.memory.sustained.clear()
            self.refresh_demand()
            self.engine.memory.sustained.clear()
            return self.decision.heat is True
        except (ValueError, TypeError, KeyError):
            return False


    async def verify_boot_stop(self):
        """Bounded OFF-only recovery. Never renew an expired transaction as an ACK."""
        if self.boot_verify_deadline is None:
            return False
        now = self.ports.monotonic()
        if now >= self.boot_verify_deadline:
            self.boot_verify_deadline = None
            self.tripped = 'startup_recovery_expired'
            await self.stop(self.tripped)
            return True
        try:
            async with asyncio.timeout(10):
                raw = await self.poll()
            if self.boot_verify_deadline is None or self.closed:
                return True  # Operator OFF/shutdown while the read was pending.
            now, wall = self.ports.monotonic(), self.ports.wall_time()
            if abs((wall-self.last_wall)-(now-self.last_mono)) > 5:
                self.telemetry = Telemetry()
                raise ValueError('Wall clock discontinuity')
            self.last_wall, self.last_mono = wall, now
            self.telemetry.observe(raw, now)
            if now >= self.boot_verify_deadline:
                self.boot_verify_deadline = None
                self.tripped = 'startup_recovery_expired'
                await self.stop(self.tripped)
                return True
            if self.telemetry.value('faultindicator', now) != 'Off':
                raise ValueError('Boiler fault')
            if self.pending is not None and not self.pending[0] and now-self.pending[2] >= 90:
                # The old attempt failed. Only restored, fresh safe-state evidence
                # permits a NEW physical stop, never retroactive acknowledgement.
                if self.boot_verify_attempts >= 3:
                    # End this boot episode without extending its ACK/budget.
                    # Later recovery owns separate, rate-limited OFF probes.
                    self.boot_verify_deadline = None
                    self.tripped = 'startup_attempts_exhausted'
                    self.startup_rearm_barrier = now
                    self.startup_rearm_after = now + STARTUP_REARM_INTERVAL
                    self.status = 'startup_recovery_cooldown'
                    return True
                safe = (self.telemetry.value('chenable', now) == 'Off' and
                        0 <= self.telemetry.number('controlsetpoint', now) <= MINIMUM_SETPOINT and
                        min(self.telemetry.fields[k][2] for k in ('chenable','controlsetpoint')) > self.pending[2])
                if not safe:
                    self.status = 'startup_waiting_for_safe_telemetry'
                    return True
                self.pending = None
                self.last_stop_sent = None
                self.boot_verify_attempts += 1
                self.sent(False, MINIMUM_SETPOINT)
                await self.stop('startup_retry_waiting_for_off_confirmation')
                return True
            self.confirm(now)
            if self.pending is not None:
                self.status = 'startup_waiting_for_off_confirmation'
                return True
            self.boot_verify_deadline = None
            self.status = 'startup_off_confirmed'
            return False
        except Exception as exc:
            self.last_error_stage = 'boot_off_verification'
            if self.ports.transient_read_error(exc) or str(exc).startswith('Stale telemetry:'):
                self.status = 'startup_waiting_for_telemetry'
                return True
            self.boot_verify_deadline = None
            self.telemetry = Telemetry()
            self.tripped = str(exc) if isinstance(exc, ValueError) else 'startup_verification_failed'
            await self.stop(self.tripped)
            return True


    async def recover_exhausted_startup(self):
        """Passive recovery after the three boot attempts, never a blind HEAT.

        Keep the fault latched until a NEW OFF publish completes and receives
        fresh confirmation within its own original 90s. Each probe has at least
        five minutes spacing; read errors/cache cannot authorize or renew one.
        No change to other startup faults, the boot budget or operating limits.
        """
        if self.tripped != 'startup_attempts_exhausted' or self.startup_rearm_after is None:
            return False
        allowed = lambda: (self.startup_ready and not self.closed and not self.ports.closing and
                           self.ports.heat_requested and
                           self.tripped == 'startup_attempts_exhausted' and
                           self.startup_rearm_after is not None and
                           self.ports.permission(HEATING_SEASON_ENTITY) is True and
                           self.ports.permission(self.settings['enabled_entity']) is True)
        if not allowed():
            self.status = 'startup_recovery_waiting_for_permission'
            return True
        now = self.ports.monotonic()
        if self.startup_rearm_pending and self.pending is not None and now-self.pending[2] >= 90:
            # Expired is failed, even if late safe telemetry is now available.
            self.startup_rearm_pending = False
            self.startup_rearm_barrier = now
        if not self.startup_rearm_pending and now < self.startup_rearm_after:
            self.status = 'startup_recovery_cooldown'
            return True
        if not self.ports.connected:
            self.status = 'startup_recovery_waiting_for_mqtt'
            return True
        try:
            async with asyncio.timeout(10):
                raw = await self.poll()
            if not allowed() or not self.ports.connected:
                return True
            now, wall = self.ports.monotonic(), self.ports.wall_time()
            if abs((wall-self.last_wall)-(now-self.last_mono)) > 5:
                self.telemetry = Telemetry()
                raise ValueError('Wall clock discontinuity')
            self.last_wall, self.last_mono = wall, now
            self.telemetry.observe(raw, now)
            if self.telemetry.value('faultindicator', now) != 'Off':
                raise ValueError('Boiler fault')
            safe = (self.telemetry.value('chenable', now) == 'Off' and
                    0 <= self.telemetry.number('controlsetpoint', now) <= MINIMUM_SETPOINT and
                    min(self.telemetry.fields[k][2] for k in ('chenable', 'controlsetpoint')) > self.startup_rearm_barrier)
            if self.startup_rearm_pending:
                if self.pending is None or self.pending[0]:
                    raise ValueError('Invalid startup recovery transaction')
                if now-self.pending[2] >= 90:
                    self.startup_rearm_pending = False
                    self.startup_rearm_barrier = now
                    self.status = 'startup_recovery_cooldown'
                    return True
                self.status = 'startup_recovery_waiting_for_off_confirmation'
                if safe:
                    self.confirm(now)
                    if self.pending is None:
                        self.tripped = None
                        self.startup_rearm_pending = False
                        self.startup_rearm_after = None
                        self.startup_rearm_barrier = None
                        self.modulation_since = None
                        self.modulation_confirmed = False
                        self.engine.reset()
                        self.decision = None
                        self.dhw_recovery.reset(self.ports.monotonic())
                        self.status = 'startup_recovery_off_confirmed'
                        # No direct CH ON: next normal cycle rechecks PI/HR,
                        # permissions, DHW/water, RM and the original report ages.
                return True
            if not safe:
                self.status = 'startup_recovery_waiting_for_safe_telemetry'
                return True
            self.pending = None
            self.last_stop_sent = None
            self.startup_rearm_pending = True
            self.startup_rearm_attempts += 1
            self.startup_rearm_after = now + STARTUP_REARM_INTERVAL
            await self.stop('startup_recovery_waiting_for_off_confirmation')
            return True
        except Exception as exc:
            self.last_error_stage = 'startup_rearm'
            if self.stop_publish_pending and self.ports.mqtt_disconnected(exc):
                self.status = 'startup_recovery_waiting_for_mqtt'
                return True
            if self.ports.transient_read_error(exc) or str(exc).startswith('Stale telemetry:'):
                self.status = 'startup_recovery_waiting_for_telemetry'
                return True
            self.startup_rearm_after = None
            self.startup_rearm_pending = False
            self.tripped = str(exc) if isinstance(exc, ValueError) else 'startup_rearm_failed'
            self.telemetry = Telemetry()
            await self.stop(self.tripped)
            return True


    async def recover_command(self):
        """Recover only expired ACKs, through a new, freshly proven safe stop.

        Passive reads may outlast an outage. No expired ACK is accepted and no
        more than three new OFF transactions are allowed in one recovery episode.
        Other faults, operator OFF and permissions never authorize a restart.
        """
        if self.tripped != 'command_not_confirmed':
            return False
        allowed = lambda: (self.startup_ready and not self.closed and
                           self.ports.heat_requested and
                           self.tripped == 'command_not_confirmed' and
                           self.ports.permission(self.settings['enabled_entity']) is True)
        if not allowed():
            return False
        try:
            async with asyncio.timeout(10):
                raw = await self.poll()
            if not allowed():
                return True
            now, wall = self.ports.monotonic(), self.ports.wall_time()
            if abs((wall-self.last_wall)-(now-self.last_mono)) > 5:
                self.telemetry = Telemetry()
                raise ValueError('Wall clock discontinuity')
            self.last_wall, self.last_mono = wall, now
            self.telemetry.observe(raw, now)
            if self.telemetry.value('faultindicator', now) != 'Off':
                raise ValueError('Boiler fault')
            enabled = self.telemetry.value('chenable', now)
            water = self.telemetry.number('controlsetpoint', now)
            receipt = min(self.telemetry.fields[k][2] for k in ('chenable', 'controlsetpoint'))
            if self.pending is None or self.pending[0]:
                # Defensive stop; the normal expiry path has already sent it.
                await self.stop('command_recovery_waiting_for_off')
                return True
            since = self.pending[2]
            safe = enabled == 'Off' and 0 <= water <= MINIMUM_SETPOINT and receipt > since
            if now-since >= 90:
                if self.command_recovery_attempts >= 3:
                    raise ValueError('command_recovery_exhausted')
                if not safe:
                    self.status = 'command_recovery_waiting_for_safe_telemetry'
                    return True
                # Safe current state permits a NEW physical transaction, not an
                # extension or retroactive confirmation of the expired one.
                self.pending = None
                self.last_stop_sent = None
                self.command_recovery_attempts += 1
                await self.stop('command_recovery_waiting_for_off')
                return True
            if not safe:
                self.status = 'command_recovery_waiting_for_off'
                return True
            self.confirm(now)
            if self.pending is None:
                self.tripped = None
                self.command_recovery_attempts = 0
                self.modulation_since = None
                self.modulation_confirmed = False
                self.engine.reset()
                self.decision = None
                self.dhw_recovery.reset(self.ports.monotonic())
                self.status = 'command_recovery_off_confirmed'
                # Next normal cycle checks current PI/HR, DHW, water and RM.
                # Reports retain their original timestamps; no direct HEAT here.
            return True
        except Exception as exc:
            self.last_error_stage = 'command_recovery'
            if self.stop_publish_pending and self.ports.mqtt_disconnected(exc):
                # Keep the original OFF deadline and recovery budget. A later
                # safe read may authorize a NEW bounded stop, never clear this
                # incomplete publish or directly enable heating.
                self.status = 'command_recovery_waiting_for_mqtt'
                return True
            if self.ports.transient_read_error(exc) or str(exc).startswith('Stale telemetry:'):
                self.status = 'command_recovery_waiting_for_telemetry'
                return True
            self.tripped = str(exc) if isinstance(exc, ValueError) else 'command_recovery_failed'
            self.telemetry = Telemetry()
            await self.stop(self.tripped)
            return True


    async def cycle(self):
        async with self.ports.control_lock:
            if self.starting or self.closed or self.ports.closing:
                return
            cycle_mono = self.ports.monotonic()
            self.cycle_gap_seconds = (cycle_mono-self.last_cycle_mono
                                      if self.last_cycle_mono is not None else None)
            self.last_cycle_mono = cycle_mono
            self.last_cycle_at = observation_utc(self.ports.wall_time())
            stage = 'startup'
            try:
                if (self.boot_ack_pending and self.ports.host_running and
                        self.startup_recovery_deadline is not None and self.tripped is None):
                    # Full boot owns one bounded recovery after HA is ready,
                    # including an ambiguous initial safe-stop publish failure.
                    self.startup_recovery_deadline = None
                    self.startup_ready = True
                if self.startup_recovery_deadline is not None:
                    await self.recover_startup()
                    return
                if self.boot_ack_pending:
                    # The initial shutdown protects the long HA setup phase.
                    # Begin the final transaction only when HA can run its loop;
                    # this is one new physical OFF, never renewal of an ACK retry.
                    if not self.ports.host_running:
                        self.status = 'waiting_for_ha_started'
                        return
                    self.boot_ack_pending = False
                    self.pending = None
                    self.last_stop_sent = None
                    self.telemetry = Telemetry()
                    if self.boot_verify_deadline is None:
                        self.boot_verify_deadline = self.ports.monotonic() + STARTUP_RECOVERY_TIMEOUT
                    self.boot_verify_attempts = 1
                    self.sent(False, MINIMUM_SETPOINT)
                    await self.stop('boot_waiting_for_off_confirmation')
                self.resume_for_season()
                if await self.recover_exhausted_startup():
                    return
                if await self.verify_boot_stop():
                    return
                if await self.recover_command():
                    return
                if not self.gate():
                    self.dhw_recovery.reset(self.ports.monotonic())
                    self.engine.reset()
                    await self.stop(self.tripped or 'off_or_permission_unknown')
                    return
                revision = self.revision
                if self.last_cs_refresh is not None and not 0 <= self.ports.monotonic()-self.last_cs_refresh < CS_REFRESH_DEADLINE:
                    raise ValueError('CS keepalive deadline exceeded')
                stage = 'telemetry_read'
                try:
                    # Include shared-reader lock acquisition in the read deadline.
                    async with asyncio.timeout(10):
                        data = await self.poll()
                except Exception as exc:
                    self.telemetry_read_failures += 1
                    # Preserve original receipts, not a new 90s grace period per retry.
                    if self.ports.transient_read_error(exc):
                        if self.hold_verified_heat():
                            # Refresh only the existing, previously confirmed lease.
                            # No new target/ACK; original telemetry and demand TTLs
                            # still bound this permission. Failed reads never re-age them.
                            if self.last_ack and self.last_ack['on'] and self.last_cs_refresh is not None:
                                stage = 'write_keepalive_after_read_error'
                                target = self.pending[1] if self.pending else self.last_ack['water']
                                await self.refresh_heat_cs(target)
                            if not self.inputs_allow_continuation(revision):
                                await self.stop('inputs_changed')
                                return
                            self.status = 'telemetry_retry_holding_heat'
                            return
                    # HTTP loss is not MQTT loss. Require a new OTGW baseline
                    # and advancing epochs, preserving report ages and the
                    # pending command deadline (never acknowledge from cache).
                    if not self.ports.transient_read_error(exc):
                        self.telemetry = Telemetry()
                    self.decision = None
                    raise
                stage = 'validation'
                mono, wall = self.ports.monotonic(), self.ports.wall_time()
                clock_jump = abs((wall-self.last_wall)-(mono-self.last_mono)) > 5
                self.last_wall, self.last_mono = wall, mono
                if clock_jump:
                    self.invalidate()
                    self.telemetry = Telemetry()
                    raise ValueError('Wall clock discontinuity')
                self.telemetry.observe(data, mono)
                self.confirm(mono)
                if self.pending is not None and not self.pending[0]:
                    # OFF/CS10 was already sent. Reads, not another slow publish,
                    # must use the remaining acknowledgement window.
                    self.status = 'waiting_for_off_confirmation'
                    return
                if self.telemetry.value('faultindicator', mono) != 'Off':
                    raise ValueError('Boiler fault')
                water = self.telemetry.number('boilertemperature', mono)
                self.dhw_recovery.check(self.telemetry.value('dhwmode', mono), water, mono,
                                        self.policy.maximum_water + self.limits.water_margin)
                if not self.gate():
                    await self.stop('inputs_changed')
                    return
                self.refresh_demand()
                if self.decision.heat is True and not await self.acquire_modulation(mono):
                    return
                if self.last_cs_refresh is not None and mono-self.last_cs_refresh >= CS_REFRESH_DEADLINE:
                    raise ValueError('CS keepalive deadline exceeded')
                target = self.ramp.target(self.decision, mono)
                if target is None:
                    await self.stop('unknown' if self.decision.heat is None else 'off_or_restart_delay')
                    return
                if self.pending is not None and self.pending[0]:
                    target = self.pending[1]
                    self.ramp.water = target
                cap = float(self.ports.maximum_setpoint)
                if not isfinite(cap) or target > cap:
                    raise ValueError('SAT maximum below demand target')
                if not self.inputs_allow_continuation(revision):
                    await self.stop('inputs_changed')
                    return
                if self.pending is not None and self.pending[0]:
                    # Read for acknowledgement; never republish an in-flight ON.
                    # All demand, permission, DHW and safety checks above still run.
                    stage = 'write_keepalive'
                    await self.refresh_heat_cs(target)
                    if not self.inputs_allow_continuation(revision):
                        await self.stop('inputs_changed')
                        return
                    self.status = 'waiting_for_on_confirmation'
                    return
                if (self.last_ack and self.last_ack['on'] and
                        self.telemetry.value('chenable', mono) == 'On' and
                        abs(target-self.last_ack['water']) < 0.01 and
                        abs(target-self.telemetry.number('controlsetpoint', mono)) <= 0.6):
                    stage = 'write_keepalive'
                    await self.refresh_heat_cs(target)
                    if not self.inputs_allow_continuation(revision):
                        await self.stop('inputs_changed')
                        return
                    self.status = 'degraded' if self.decision.degraded else 'heat'
                    return
                self.ports.setpoint = target
                # A QoS publish timeout is ambiguous: delivery may have happened.
                # Bound that ambiguity from BEFORE the first write, not its return.
                need_ch_on = not (self.last_ack and self.last_ack['on'] and
                                  self.telemetry.value('chenable', mono) == 'On')
                self.sent(True, target)
                stage = 'write_cs'
                await self.write_heat_cs(target)
                if not self.inputs_allow_continuation(revision):
                    await self.stop('inputs_changed')
                    return
                stage = 'write_ch_on'
                if need_ch_on:
                    # A timed-out CH ON may have reached OTGW. Start the one
                    # cooling timer before that ambiguous write, never on retry.
                    self.dhw_recovery.heating_sent(water, mono)
                    async with asyncio.timeout(self.ports.write_timeout):
                        self.command_writes["ch_on"] += 1
                        await self.ports.send(Command('ch', True))
                if not self.inputs_allow_continuation(revision):
                    await self.stop('inputs_changed')
                    return
                self.dhw_recovery.heating_sent(water, mono)
                self.status = 'degraded' if self.decision.degraded else 'heat'
            except asyncio.CancelledError:
                # Shutdown/unload owns the final stop under the same lock.
                raise
            except Exception as exc:
                self.last_error_stage = stage
                if self.boot_verify_deadline is not None and self.ports.transient_read_error(exc):
                    self.status = 'startup_waiting_for_telemetry'
                    return
                # One immediate CS-only retry can use the remaining original
                # lease budget; waiting for another scheduled cycle can miss it.
                # Never retry CH ON, an unconfirmed start or an expired lease.
                if (stage in ('write_cs', 'write_keepalive', 'write_keepalive_after_read_error') and
                        isinstance(exc, (TimeoutError, OSError)) and
                        self.last_ack and self.last_ack['on'] and
                        self.last_cs_refresh is not None and self.hold_verified_heat()):
                    self.cs_retry_attempts += 1
                    stage = 'write_cs_retry'
                    target = self.pending[1] if self.pending else self.last_ack['water']
                    try:
                        await self.write_heat_cs(target, keepalive=True, retry=True)
                    except Exception as retry_error:
                        exc = retry_error
                        self.last_error_stage = stage
                    else:
                        if not self.inputs_allow_continuation(revision):
                            await self.stop('inputs_changed')
                            return
                        self.cs_retry_successes += 1
                        self.status = 'cs_retry_recovered'
                        return
                if (stage in ('write_cs', 'write_ch_on', 'write_keepalive',
                              'write_keepalive_after_read_error', 'write_cs_retry') and
                        isinstance(exc, (TimeoutError, OSError)) and self.hold_verified_heat()):
                    # Only a previously proven running boiler may coast. A start
                    # from OFF, expired evidence, DHW or a safety veto must stop.
                    # Pending keeps its original deadline; cache never ACKs it.
                    self.status = 'write_retry_holding_heat'
                    return
                if (stage in ('telemetry_read', 'write_cs', 'write_ch_on', 'write_keepalive',
                              'write_keepalive_after_read_error', 'write_cs_retry') and
                        self.ports.transient_read_error(exc)):
                    self.dhw_recovery.transport_error(self.ports.monotonic())
                elif (str(exc) not in ('DHW priority', 'DHW residual water at or above ceiling')
                      and self.dhw_recovery.seen_dhw):
                    self.dhw_recovery.reset(self.ports.monotonic())
                self.engine.reset()
                self.status = 'input_or_transport_error:' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
                if self.pending is not None and not self.pending[0] and self.tripped is None:
                    if self.ports.monotonic() - self.pending[2] < 90:
                        # Includes the first HTTP baseline and transient read loss.
                        # Do not spend the bounded ACK window on redundant OFF writes.
                        # No ON can pass until a later fresh matching confirmation.
                        return
                    self.tripped = 'command_not_confirmed'
                    self.status = self.tripped
                await self.stop(self.status)
            finally:
                self.ports.state_changed()

