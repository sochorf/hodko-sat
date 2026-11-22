"""Offline-ready passive observer. No HA services, coordinator or transport writes.

Feed actual report events, read-only OTGW snapshots and HA permission states.
This is deliberately not Runtime: no start/stop boiler command, confirmation,
ramp simulation or room PID. The future HA sensor adapter owns subscriptions.
"""
from copy import deepcopy
from math import isfinite
from .control import Engine, Reports
from .kernel import Policy, MAXIMUM_ALLOWED_WATER
from .evidence import Telemetry, OperatingLimits, utc, observation_utc


class Observer:
    def __init__(self, members, enabled_entity, started, policy=None, limits=None):
        if not members or not enabled_entity:
            raise ValueError('Explicit membership and permission required')
        self.members = deepcopy(members)
        self.enabled_entity = enabled_entity
        self.policy = policy or Policy()
        if not 10 < self.policy.minimum_water <= self.policy.maximum_water <= MAXIMUM_ALLOWED_WATER:
            raise ValueError('Shadow policy must respect the experimental cap')
        if any(not m.get('zone') or not m.get('eligible_entity') for m in members.values()):
            raise ValueError('Explicit zone and room permission required')
        self.limits = limits or OperatingLimits()
        self.limits.validate_water(self.policy.maximum_water)
        self.engine = Engine({m['name']: m['zone'] for m in members.values()}, self.policy)
        self.reports = Reports({ieee: m['name'] for ieee, m in members.items()}, started)
        self.telemetry = Telemetry()
        self.last = None
        self.result = self._result('unknown', 'waiting_for_evidence')

    def _result(self, status, reason, decision=None):
        return {'mode': 'shadow', 'status': status, 'reason': reason,
                'proposed_water': decision.water if decision else None,
                'zones': decision.zones if decision else {},
                'sustained_corrections': decision.corrections if decision else {},
                'reasons': decision.reasons if decision else {},
                'sequence': self.reports.seq, 'writes': False,
                'http_missing_fields': sorted(self.telemetry.http_missing),
                'http_input_issues': dict(self.telemetry.http_issues),
                'http_epoch_modulus': self.telemetry.http_epoch_modulus,
                'http_counter_wraps': dict(self.telemetry.http_counter_wraps),
                'command_confirmation': 'not_applicable'}

    def monitor_failed(self):
        """Discard OTGW proof only; MQTT evidence retains its original TTL."""
        self.engine.reset()
        self.telemetry = Telemetry()
        self.result = self._result('unknown', 'monitor_error')

    def disconnect(self, wall):
        self.reports.invalidate(wall)
        self.engine.reset()
        self.telemetry = Telemetry()
        self.result = self._result('unknown', 'disconnected')

    def accept(self, event, wall, retained=False):
        try:
            return self.reports.accept(event, wall, retained)
        except (ValueError, TypeError, KeyError, AttributeError):
            self.disconnect(wall)
            self.result = self._result('unknown', 'invalid_report')
            return False

    def evaluate(self, states, monitor, wall, mono):
        """Evaluate a read-only snapshot. No missing value is replaced by zero."""
        try:
            if not all(isfinite(v) for v in (wall, mono)):
                raise ValueError('invalid_clock')
            if self.last and (mono < self.last[1] or abs((wall-self.last[0])-(mono-self.last[1])) > 5):
                self.disconnect(wall)
                raise ValueError('clock_discontinuity')
            self.last = (wall, mono)
            gate = states.get(self.enabled_entity)
            if gate != 'on':
                self.engine.reset()
                self.result = self._result('off' if gate == 'off' else 'unknown',
                                           'permission_off' if gate == 'off' else 'permission_unknown')
                return self.result
            if monitor is not None:
                self.telemetry.observe(monitor, mono)
            else:
                # Same original-age boundary as active mode. Never renew receipts.
                self.engine.memory.sustained.clear()
            if any(self.telemetry.value(k, mono) != 'Off' for k in ('faultindicator', 'dhwmode')):
                raise ValueError('fault_or_dhw')
            water = self.telemetry.number('boilertemperature', mono)
            if not 0 <= water < self.policy.maximum_water + self.limits.water_margin:
                raise ValueError('boiler_water_guard')
            heads = deepcopy(self.reports.heads)
            for member in self.members.values():
                heads.setdefault(member['name'], {})['eligible'] = {
                    'value': {'on': True, 'off': False}.get(states.get(member['eligible_entity'])),
                    'observed_at': observation_utc(wall), 'evidence': 'policy_evaluation'}
            outside = self.telemetry.number('outsidetemperature', mono)
            frame = {'session_id': f'{self.reports.session}:{self.reports.segment}',
                     'session_started_at': utc(self.reports.epoch), 'heads': heads,
                     'outside': {'value': outside, 'evidence': 'otgw_report',
                                 'observed_at': observation_utc(wall-(mono-self.telemetry.fields['outsidetemperature'][2]))}}
            decision = self.engine.evaluate(frame, wall)
            status = 'unknown' if decision.heat is None else ('degraded' if decision.degraded else 'heat') if decision.heat else 'off'
            self.result = self._result(status, 'telemetry_retry' if monitor is None else 'evaluated', decision)
            if monitor is None:
                self.engine.memory.sustained.clear()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.engine.reset()
            self.result = self._result('unknown', str(exc))
        return self.result
