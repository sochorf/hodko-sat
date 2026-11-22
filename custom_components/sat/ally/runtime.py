"""Experimental SAT PI/HR mode. Sole writer is the existing SAT coordinator.

Not enabled by default. No archive/Influx/Pyscript dependency. Local source
evidence requires consecutive live session proof before restoration. Restores operator mode only for an identical reviewed setup. No room target/temperature enters the controller.
"""
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import json
import logging
from hashlib import sha256
from math import isfinite
from time import time, monotonic
from urllib.parse import urlsplit

from homeassistant.components import mqtt
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from paho.mqtt.client import MQTT_ERR_NO_CONN, MQTT_ERR_CONN_LOST, error_string
from homeassistant.components.mqtt.const import MQTT_CONNECTION_STATE
from homeassistant.helpers.storage import Store
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval, async_track_state_change_event
from homeassistant.components.climate import HVACMode
from .monitor import shared_reader, transient_read_error
from .mqtt_monitor import MqttSnapshot, shared_mqtt_reader, topic as mqtt_topic
from .control import Engine, Reports, Ramp, stamp
from .evidence import Telemetry, OperatingLimits, DHWRecovery, utc, observation_utc
from .machine import (OperationalMachine, Command, CS_REFRESH_INTERVAL,
                      CS_REFRESH_DEADLINE, STARTUP_RECOVERY_TIMEOUT,
                      STARTUP_RETRY_INTERVAL, STARTUP_REARM_INTERVAL,
                      HEATING_SEASON_ENTITY, PASSIVE_WATER_STOPS)
from .kernel import Policy, MAXIMUM_ALLOWED_WATER, CORRECTION_RANGES
from ..compat.coordinator import DeviceState
from ..compat.const import MINIMUM_SETPOINT


_LOGGER = logging.getLogger(__name__)
WRITE_TIMEOUT = 15

def mqtt_disconnected(exc):
    """Only HA's structured transport errors; never auth/payload/schema failures."""
    return (isinstance(exc, HomeAssistantError) and
            exc.translation_domain == 'mqtt' and
            exc.translation_key == 'mqtt_broker_error' and
            (exc.translation_placeholders or {}).get('error_message') in
            (error_string(MQTT_ERR_NO_CONN), error_string(MQTT_ERR_CONN_LOST)))


class HaPorts:
    """HA/OTGW execution boundary; holds no independent controller state."""
    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def c(self): return self.runtime.c
    @property
    def control_lock(self): return self.c._control_lock
    @property
    def closing(self): return self.c._control_closing
    @property
    def host_running(self): return self.c.hass.is_running
    @property
    def heat_requested(self): return self.c.hvac_mode == HVACMode.HEAT
    @property
    def connected(self): return mqtt.is_connected(self.c.hass)
    @property
    def maximum_setpoint(self): return self.c._coordinator.maximum_setpoint
    @property
    def setpoint(self): return self.c._setpoint
    @setpoint.setter
    def setpoint(self, value): self.c._setpoint = value
    @property
    def write_timeout(self): return WRITE_TIMEOUT

    def wall_time(self): return time()
    def monotonic(self): return monotonic()
    def permission(self, key):
        state = self.c.hass.states.get(key)
        return {'on': True, 'off': False}.get(state.state) if state else None
    def restore_heat(self): self.c._hvac_mode = HVACMode.HEAT
    def state_changed(self): self.c.async_write_ha_state()
    def save_reports(self): self.runtime.save_reports()
    def mqtt_disconnected(self, exc): return mqtt_disconnected(exc)
    def transient_read_error(self, exc): return transient_read_error(exc)
    async def read_telemetry(self): return await self.runtime.poll()
    async def send(self, command):
        coordinator = self.c._coordinator
        if command.kind == 'ch':
            await coordinator.async_set_heater_state(DeviceState.ON if command.value else DeviceState.OFF)
        elif command.kind == 'cs':
            await coordinator.async_set_control_setpoint_only(command.value)
        elif command.kind == 'mm':
            await coordinator.async_set_control_max_relative_modulation(command.value)
        else:
            raise ValueError('Unknown SAT command intent')


class Runtime(OperationalMachine):
    def __init__(self, climate, settings, *, validation_cap=None):
        self.c = climate
        if climate._config_entry.data.get('mode') not in ('fake', 'mqtt_opentherm'):
            raise ValueError('PI/HR candidate supports OTGW MQTT only')
        cap = float(climate._coordinator.maximum_setpoint if validation_cap is None else validation_cap)
        if not isfinite(cap) or not 10 < cap <= MAXIMUM_ALLOWED_WATER:
            raise ValueError('Explicit SAT maximum outside supported range')
        if settings.get('schema_version') != 1:
            raise ValueError('Explicit PI/HR settings schema required')
        self.settings = deepcopy(settings)
        members = settings['members']
        names = [m['name'] for m in members.values()]
        if not members or len(set(names)) != len(names) or any(not m.get('eligible_entity') or not m.get('zone') for m in members.values()):
            raise ValueError('Explicit unique members/zones/eligibility required')
        if not settings.get('enabled_entity') or not settings.get('topic') or any(x in settings['topic'] for x in '+#'):
            raise ValueError('Explicit gate and exact report topic required')
        self.mqtt_namespace = mqtt_topic(settings)
        if self.mqtt_namespace:
            expected = climate._coordinator._get_topic_for_subscription('TSet')
            if expected != self.mqtt_namespace + '/TSet':
                raise ValueError('MQTT telemetry must match the command gateway')
        url = urlsplit(settings.get('monitor_url', 'http://unused/api/v2/otgw/otmonitor' if self.mqtt_namespace else ''))
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment or url.path != '/api/v2/otgw/otmonitor':
            raise ValueError('Read-only OTGW monitor URL required, without credentials')
        if climate._radiators or climate.areas.items() or climate.thermostat or climate._push_setpoint_to_thermostat:
            raise ValueError('PI/HR mode cannot own room thermostats')
        self.policy = Policy(**settings.get('policy', {}))
        if not 10 < self.policy.minimum_water <= self.policy.maximum_water <= MAXIMUM_ALLOWED_WATER:
            raise ValueError('Water range must be above OFF and within supported cap')
        if self.policy.maximum_water > cap:
            raise ValueError('Policy cap exceeds SAT maximum')
        self.limits = OperatingLimits(**settings.get('operating_limits', {}))
        self.limits.validate_water(self.policy.maximum_water)
        super().__init__(HaPorts(self), self.settings, self.policy, self.limits,
                         climate._maximum_relative_modulation)
        self.restore_key = sha256(json.dumps({
            'settings': self.settings,
            'entry_id': climate._config_entry.entry_id,
            'return_id': climate._config_entry.options.get('hodko_return_id'),
        }, sort_keys=True).encode()).hexdigest()
        self._report_store = None

    @property
    def attributes(self):
        return {'control_mode': 'ally_demand', 'ally_telemetry_transport': 'mqtt' if self.mqtt_namespace else 'http',
                'ally_telemetry_time_basis': 'mqtt_receipt' if self.mqtt_namespace else 'otgw_epoch', 'ally_status': self.status,
                'ally_last_error_stage': self.last_error_stage,
                'ally_http_missing_fields': sorted(self.telemetry.http_missing),
                'ally_http_input_issues': dict(self.telemetry.http_issues),
                'ally_http_epoch_modulus': self.telemetry.http_epoch_modulus,
                'ally_http_counter_wraps': dict(self.telemetry.http_counter_wraps),
                'ally_command_recovery_attempts': self.command_recovery_attempts,
                'ally_stop_publish_pending': self.stop_publish_pending,
                'ally_monitor_error': self.monitor_error,
                'ally_command_write_attempts': dict(self.command_writes),
                'ally_cs_keepalive_writes': self.cs_keepalive_writes,
                'ally_cs_publish_failures': self.cs_publish_failures,
                'ally_cs_retry_attempts': self.cs_retry_attempts,
                'ally_cs_retry_successes': self.cs_retry_successes,
                'ally_telemetry_read_failures': self.telemetry_read_failures,
                'ally_cs_write_seconds': self.cs_write_seconds,
                'ally_cs_write_budget': self.cs_write_budget,
                'ally_cs_write_overrun': self.cs_write_overrun,
                'ally_cycle_gap_seconds': self.cycle_gap_seconds,
                'ally_cs_refresh_age': max(0, monotonic()-self.last_cs_refresh) if self.last_cs_refresh is not None else None,
                'ally_last_ack': dict(self.last_ack) if self.last_ack else None,
                'ally_boot_verify_attempts': self.boot_verify_attempts,
                'ally_boot_verify_remaining': max(0, self.boot_verify_deadline-monotonic()) if self.boot_verify_deadline is not None else None,
                'ally_startup_rearm_remaining': max(0, self.startup_rearm_after-monotonic()) if self.startup_rearm_after is not None else None,
                'ally_startup_rearm_pending': self.startup_rearm_pending,
                'ally_startup_rearm_attempts': self.startup_rearm_attempts,
                'ally_fault': self.tripped, 'ally_zones': self.decision.zones if self.decision else {},
                'ally_reasons': self.decision.reasons if self.decision else {},
                'ally_proposed_water': self.decision.water if self.decision else None,
                'ally_sustained_corrections': self.decision.corrections if self.decision else {},
                'ally_command_water': self.c._setpoint,
                'ally_confirmation': 'pending' if self.pending else 'no_pending_command',
                'ally_pending_command': ({'on': self.pending[0], 'water': self.pending[1],
                    'age_seconds': max(0, monotonic()-self.pending[2])} if self.pending else None),
                'ally_command_evidence': {key: {
                    'value': field[1],
                    'age_seconds': max(0, monotonic()-field[2]) if field[2] is not None else None,
                    'after_command': (field[2] > self.pending[2] if self.pending[0] else field[2] >= self.pending[2]) if self.pending and field[2] is not None else None,
                } for key, field in self.telemetry.fields.items()
                  if key in ('chenable', 'controlsetpoint', 'maxrelmodlvl')},
                'ally_starting': self.starting,
                'ally_report_restore_status': self.report_restore_status,
                'ally_restored_reports': {name: {key: {
                    'observed_at': self.reports.heads[name][key]['observed_at'],
                    'age_seconds': max(0,time()-stamp(self.reports.heads[name][key]['observed_at'])),
                    'valid_now': (type(self.reports.heads[name][key]['value']) is (bool if key == 'heat_required' else int)
                        and 0<=time()-stamp(self.reports.heads[name][key]['observed_at'])<=self.policy.ttl),
                    'source': self.reports.restore_source or 'local_verified_cache'} for n,key in self.reports.restored_fields if n==name}
                    for name in {n for n,k in self.reports.restored_fields}},
                'ally_report_errors': self.reports.errors,
                'ally_ignored_reports': self.reports.ignored_reports,
                'ally_last_ignored_device': self.reports.last_ignored_device,
                'ally_sequence': self.reports.seq,
                'ally_last_cycle_at': self.last_cycle_at,
                'ally_last_decision_at': self.last_decision_at,
                'ally_decision_sequence': self.decision_sequence,
                'ally_startup_ready': self.startup_ready,
                'ally_restart_key': self.restore_key,
                'ally_restart_restored': self.restart_restored,
                'ally_season_start_pending': self.season_start_pending,
                'ally_startup_recovering': (self.startup_recovery_deadline is not None or
                    self.boot_verify_deadline is not None or self.startup_rearm_after is not None),
                'ally_startup_error': self.startup_error,
                'ally_modulation_limit': self.modulation_limit,
                'ally_modulation_confirmed': self.modulation_confirmed,
                'ally_dhw_recovery': self.dhw_recovery.active,
                'ally_dhw_diagnostics': self.dhw_recovery.diagnostics(
                    monotonic(), self.policy.maximum_water + self.limits.water_margin),
                'ally_correction_parameters': {k: getattr(self.policy, k) for k in CORRECTION_RANGES},
                'ally_operating_limits': {**asdict(self.limits),
                    'sat_maximum': float(self.c._coordinator.maximum_setpoint),
                    'supported_maximum': MAXIMUM_ALLOWED_WATER,
                    'maximum_water': self.policy.maximum_water,
                    'normal_water_guard': self.policy.maximum_water + self.limits.water_margin,
                    'telemetry_age': 90, 'confirmation_time': 90, 'write_timeout': WRITE_TIMEOUT}}

    @property
    def report_store(self):
        if self._report_store is None:
            self._report_store = Store(self.c.hass, 1, 'sat_ally_reports_' + self.c._config_entry.entry_id)
        return self._report_store

    def save_reports(self):
        self.report_store.async_delay_save(lambda: {
            'identity': self.restore_key, 'reports': self.reports.snapshot(time())}, 1)


    async def start(self):
        self.starting = True
        try:
            await self._start()
        finally:
            self.starting = False

    async def _start(self):
        try:
            cached = await self.report_store.async_load()
            if (cached and cached.get('identity') == self.restore_key
                    and self.reports.stage_restore(cached.get('reports'), time(), self.policy.ttl)):
                self.report_restore_status = 'waiting_for_source_continuity'
        except Exception:
            self.report_restore_status = 'cache_load_failed'
            _LOGGER.warning('SAT report cache could not be loaded; waiting for live reports')
        if self.mqtt_namespace:
            await shared_mqtt_reader(self.c.hass, self.mqtt_namespace).start()
        previous = await self.c.async_get_last_state()
        resume_heat = bool(previous is not None and previous.state == HVACMode.HEAT
                           and previous.attributes.get('control_mode') == 'ally_demand'
                           and previous.attributes.get('ally_restart_key') == self.restore_key
                           and not previous.attributes.get('ally_fault'))
        self.startup_resume_heat = resume_heat
        # Only a full HA boot may override saved OFF. Integration reloads and
        # options-flow transitions must retain their existing OFF semantics.
        # Saved fault is diagnostic history, not a permanent veto of the new
        # boot's heating permission. Current setup/fault/ACK guards still apply.
        self.season_start_pending = not self.c.hass.is_running
        self.boot_ack_pending = self.season_start_pending
        if self.season_start_pending:
            self.c.async_on_remove(self.c.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self.season_boot_ready))
        self.c._hvac_mode = HVACMode.OFF
        self.startup_ready = False
        stopped = False
        try:
            await self.c._async_shutdown()
            self.sent(False, MINIMUM_SETPOINT)
            stopped = True
        except Exception as exc:
            # Retry only transport timeouts. Unexpected errors remain latched.
            self.startup_error = type(exc).__name__
            _LOGGER.exception("SAT initial safe stop failed")
            if isinstance(exc, (TimeoutError, ConnectionError)):
                self.startup_recovery_deadline = monotonic() + STARTUP_RECOVERY_TIMEOUT
                self.startup_retry_at = monotonic() + STARTUP_RETRY_INTERVAL
                self.status = 'startup_retry_wait'
            else:
                self.tripped = 'startup_stop_failed'
                self.status = self.tripped
        self.c.async_on_remove(self.c.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.shutdown))
        self.c.async_on_remove(async_dispatcher_connect(self.c.hass, MQTT_CONNECTION_STATE, self.connection_changed))
        self.c.async_on_remove(await mqtt.async_subscribe(self.c.hass, self.settings['topic'], self.message, qos=1))
        self.c.async_on_remove(await mqtt.async_subscribe(self.c.hass, self.settings['topic'] + '/snapshot', self.snapshot_message, qos=1))
        entities = [self.settings['enabled_entity'], HEATING_SEASON_ENTITY] + [m['eligible_entity'] for m in self.settings['members'].values()]
        self.c.async_on_remove(async_track_state_change_event(self.c.hass, entities, self.policy_changed))
        self.c.async_on_remove(async_track_time_interval(self.c.hass, self.control_tick, timedelta(seconds=10)))
        # Coordinator subscription/setup remains SAT's responsibility. It can send
        # its normal boot and maximum-setpoint commands; this is not shadow mode.
        try:
            async with asyncio.timeout(45):
                await self.c._coordinator.async_added_to_hass()
        except Exception:
            # Setup can register listeners: do not repeat it blindly.
            self.startup_recovery_deadline = None
            self.tripped = 'startup_transport_failed'
            self.status = self.tripped
        if stopped and self.tripped is None:
            # The initial stop protects setup, but setup/boot writes and its
            # delays are not part of the final OFF acknowledgement transaction.
            # Start exactly one new transaction after setup, with actual OFF/CS
            # writes; retries in the normal loop never renew its 90s deadline.
            self.pending = None
            self.last_stop_sent = None
            self.telemetry = Telemetry()
            try:
                await self.stop('startup_waiting_for_off_confirmation')
                self.startup_ready = True
            except Exception as exc:
                self.startup_error = type(exc).__name__
                self.status = 'startup_retry_wait'
                if isinstance(exc, (TimeoutError, ConnectionError)):
                    self.startup_recovery_deadline = monotonic() + STARTUP_RECOVERY_TIMEOUT
                    self.startup_retry_at = monotonic() + STARTUP_RETRY_INTERVAL
                else:
                    self.tripped = 'startup_stop_failed'
                    self.status = self.tripped
        # Restore intent, never old command acknowledgements or source evidence.
        # The ordinary loop still requires policy, reports and new OTGW epochs.
        if self.startup_ready and resume_heat and self.tripped is None:
            self.c._hvac_mode = HVACMode.HEAT
            self.restart_restored = True
            self.status = 'restart_waiting_for_live_inputs'
        self.c.async_write_ha_state()
        if self.restart_restored or self.startup_recovery_deadline is not None:
            self.c.schedule_control_heating_loop()

    @callback
    def control_tick(self, _time=None):
        """Do not debounce an already due CS lease behind another ten seconds.

        Use the same safety cycle and sole writer; never publish from this
        callback. A running cycle owns the lock and the next periodic tick
        will recheck the lease. Do not build a queue behind a slow operation.
        """
        if self.closed or self.c._control_closing or self.c._control_lock.locked():
            return
        due = (self.last_cs_refresh is not None and
               not 0 <= monotonic()-self.last_cs_refresh < CS_REFRESH_INTERVAL)
        self.c.schedule_control_heating_loop(force=due)

    @callback
    def season_boot_ready(self, event):
        if self.season_start_pending:
            self.season_start_deadline = monotonic() + STARTUP_RECOVERY_TIMEOUT
            self.boot_verify_deadline = self.season_start_deadline
            self.c.schedule_control_heating_loop()


    async def shutdown(self, event=None):
        # HA lifecycle is not an operator OFF. Stop CH, keep the restorable
        # climate intent intact; explicit OFF still goes through the service.
        self.closed = True
        self.save_reports()
        async with asyncio.timeout(self.c._mode_timeout):
            async with self.c._mode_lock:
                await self.c._async_shutdown()

    @callback
    def connection_changed(self, connected):
        if self.closed:
            return
        self.invalidate(preserve_reports=bool(connected and self.reports.restore_pending is not None))
        self.telemetry = Telemetry()
        self.status = 'mqtt_reconnect_waiting_for_reports' if connected else 'mqtt_disconnected'
        self.c.schedule_control_heating_loop()

    @callback
    def policy_changed(self, event):
        self.revision += 1
        self.c.schedule_control_heating_loop()

    @callback
    def snapshot_message(self, message):
        if self.closed or not mqtt.is_connected(self.c.hass):
            return
        try:
            if self.reports.accept_snapshot(json.loads(message.payload), time(), self.policy.ttl, message.retain):
                self.report_restore_status = 'restored'
                self.save_reports()
                self.revision += 1
                self.c.schedule_control_heating_loop()
        except (ValueError, KeyError, TypeError, AttributeError):
            self.invalidate()
            self.status = 'invalid_source_snapshot'
            self.c.schedule_control_heating_loop()

    @callback
    def message(self, message):
        try:
            if self.reports.accept(json.loads(message.payload), time(), message.retain):
                self.report_restore_status = 'restored' if self.reports.restored_fields else 'live'
                self.save_reports()
                self.revision += 1
                self.c.schedule_control_heating_loop()
        except (ValueError, KeyError, TypeError, AttributeError):
            self.invalidate()
            self.status = 'invalid_report'
            self.c.schedule_control_heating_loop()


    async def poll(self):
        if self.mqtt_namespace:
            return await shared_mqtt_reader(self.c.hass, self.mqtt_namespace).read()
        reader = shared_reader(self.c.hass, self.settings['monitor_url'])
        try:
            return await reader.read()
        finally:
            self.monitor_error = reader.last_error


        # Repeated/ramped commands must not extend the deadline of an unconfirmed command.
