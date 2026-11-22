"""Transport-neutral OTGW evidence and operating guards. No HA or I/O.

HTTP acquisition and MQTT original receipts are supplied by adapters. Neither
consuming a snapshot nor resetting a controller renews source evidence.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from math import isfinite
from time import monotonic

HTTP_FIELDS = ('outsidetemperature', 'chenable', 'controlsetpoint',
               'boilertemperature', 'faultindicator', 'dhwmode', 'maxrelmodlvl')
# OTGW-firmware v1.7.9 currentTrackedSeconds(): 0xffff is reserved, not a tick.
TRACKED_SECONDS_MODULUS = 65535


def http_epoch_modulus(raw, previous=None):
    """Recognize the compact OTGW REST format; never infer it from low epochs alone.

    This firmware emits a Unix timestamp for sensorsimulation, but rolling
    seconds since boot for Statusflags and the other tracked OpenTherm fields.
    Missing/invalid discriminator fields cannot change an established format.
    """
    marker, status = raw.get('sensorsimulation'), raw.get('chenable')
    if isinstance(marker, dict) and isinstance(status, dict):
        wall, epoch = marker.get('epoch'), status.get('epoch')
        if type(wall) is int and wall >= 1_000_000_000 and type(epoch) is int:
            if 0 <= epoch < TRACKED_SECONDS_MODULUS:
                return TRACKED_SECONDS_MODULUS
            if epoch >= 1_000_000_000:
                return None  # Older/other firmware using Unix time for all fields.
    return previous


class MqttSnapshot(dict):
    """An isolated copy of original receipt evidence; reading never freshens it."""
    def __init__(self, fields, generation=0):
        super().__init__(deepcopy(fields))
        self.generation = generation


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()



def observation_utc(wall):
    """Floor locally generated evidence; source timestamps remain untouched."""
    value = datetime.fromtimestamp(wall, timezone.utc)
    if value.timestamp() > wall:
        value -= timedelta(microseconds=1)
    return value.isoformat()



class Telemetry:
    """An advancing OTGW field epoch proves an update; a GET alone does not."""
    def __init__(self):
        self.fields = {}
        self.last = None
        self.http_seen = {}
        self.http_missing = set()
        self.http_issues = {}
        self.http_epoch_modulus = None
        self.http_counter_wraps = {}

    def observe(self, raw, now):
        if isinstance(raw, MqttSnapshot):
            self.http_seen.clear()
            self.http_missing.clear()
            self.http_issues.clear()
            self.http_epoch_modulus = None
            previous_generation = getattr(self, 'mqtt_generation', None)
            self.mqtt_generation = raw.generation
            if previous_generation is not None and previous_generation != raw.generation:
                self.fields.clear()
                raise ValueError('OTGW MQTT session changed')
            self.fields = deepcopy(dict(raw))
            self.last = now
            return
        # Shared cached snapshots cannot acquire a later receipt on consumption.
        now = getattr(raw, 'acquired_at', now)
        modulus = getattr(raw, 'epoch_modulus', None)
        if modulus != self.http_epoch_modulus:
            # A transport/format transition needs a new baseline, never an ACK.
            self.fields.clear()
            self.http_seen.clear()
            self.http_epoch_modulus = modulus
        self.http_missing.clear()
        self.http_issues.clear()
        for key in HTTP_FIELDS:
            if key not in raw:
                # A partial HTTP snapshot is not an invalid source report.
                # Keep only the existing proof, including its original expiry.
                # Do not advance the per-field observation/ACK lower bound.
                self.http_missing.add(key)
                self.http_issues[key] = 'missing'
                continue
            r = raw[key]
            if not isinstance(r, dict):
                self.fields.pop(key, None)
                self.http_seen.pop(key, None)
                self.http_issues[key] = 'invalid_field'
                continue
            epoch, value = r.get('epoch'), r.get('value')
            old = self.fields.get(key)
            previous = self.http_seen.get(key)
            continuous = previous is not None and 0 <= now - previous <= 90
            receipt = None
            counter = modulus == TRACKED_SECONDS_MODULUS
            if (type(epoch) is not int or epoch < (0 if counter else 1)
                    or (counter and (epoch >= modulus or
                                     (epoch == 0 and not (old and continuous))))):
                self.fields.pop(key, None)
                self.http_seen.pop(key, None)
                self.http_issues[key] = 'invalid_epoch'
                continue
            if old and continuous:
                if counter and epoch != old[0]:
                    delta = (epoch - old[0]) % modulus
                    lower_bound = old[2] if old[2] is not None else previous
                    # One second allows integer clock quantization, not a longer
                    # receipt/ACK lifetime. Wrap needs evidence within the normal
                    # 90s lease. Arbitrary resets and delayed pre-wrap frames fail.
                    budget = now - lower_bound + 1
                    if old[2] is None:
                        # Initial cached values have no measurement-time bound.
                        # A first forward update may span a slow field's entire
                        # reporting interval (e.g. MM 60s), not just the last GET.
                        # Serial half-range still rejects a delayed pre-wrap tick.
                        # The same unknown initial age applies across a wrap.
                        # Its narrower 90s bound below still rejects resets and
                        # long/unprovable wrap intervals; no deadline is renewed.
                        budget = modulus // 2
                    if epoch < old[0]:
                        budget = min(90, budget)
                    if 0 < delta <= budget:
                        receipt = previous
                        if epoch < old[0]:
                            self.http_counter_wraps[key] = self.http_counter_wraps.get(key, 0) + 1
                    else:
                        self.fields.pop(key, None)
                        self.http_seen.pop(key, None)
                        self.http_issues[key] = 'counter_discontinuity'
                        continue
                elif epoch > old[0]:
                    receipt = previous
                elif epoch == old[0] and value == old[1]:
                    receipt = old[2]
                else:
                    self.http_issues[key] = ('epoch_reversed' if epoch < old[0]
                                             else 'value_changed_without_epoch')
            elif old:
                self.http_issues[key] = 'observation_gap'
            self.fields[key] = (epoch, value, receipt)
            self.http_seen[key] = now
        self.last = now

    def value(self, key, now):
        r = self.fields.get(key)
        if r is None or r[2] is None or not 0 <= now-r[2] <= 90:
            raise ValueError('Stale telemetry: ' + key)
        return r[1]

    def number(self, key, now):
        raw = self.value(key, now)
        if type(raw) not in (int, float) or not isfinite(raw):
            raise ValueError('Invalid telemetry: ' + key)
        return raw



@dataclass(frozen=True)
class OperatingLimits:
    """Validated operating parameters; transport and hardware safety stay separate."""
    water_margin: float = 10
    dhw_ceiling: float = 80
    dhw_window: float = 900
    cooling_time: float = 180
    mixing_time: float = 60
    cooling_drop: float = 1
    residual_rise: float = 2
    restart_delay: float = 180
    ramp_rate: float = 2

    def __post_init__(self):
        bounds = {'water_margin': (5, 20), 'dhw_ceiling': (60, 80),
                  'dhw_window': (180, 1800), 'cooling_time': (60, 600),
                  'mixing_time': (0, 120),
                  'cooling_drop': (0.5, 5), 'residual_rise': (0.5, 3),
                  'restart_delay': (180, 900), 'ramp_rate': (0.1, 2)}
        for key, (low, high) in bounds.items():
            value = getattr(self, key)
            if type(value) not in (int, float) or not isfinite(value) or not low <= value <= high:
                raise ValueError('Invalid operating limit: ' + key)
        if self.mixing_time > self.cooling_time:
            raise ValueError('Mixing time must not exceed the cooling deadline')
        if self.dhw_window < self.restart_delay + self.cooling_time:
            raise ValueError('Recovery window must cover restart delay and cooling check')

    def validate_water(self, maximum):
        if maximum + self.water_margin >= self.dhw_ceiling:
            raise ValueError('Normal water guard must be below DHW ceiling')



class DHWRecovery:
    """One bounded return episode after live DHW On -> Off evidence.

    Only the relative-rise check gets a short mixing allowance after the first
    possibly delivered CH ON. The absolute ceiling, original end/window and
    cooling deadline never move, including across a failed write or retry.
    These are software pilot limits, not a boiler hardware safety rating.
    """
    def __init__(self, limits=None):
        self.limits = limits or OperatingLimits()
        self.counts = dict(episodes=0, starts=0, completed=0, blocked=0)
        self.last_result = None
        self.last_duration = None
        self.last_peak = None
        self._clear()

    def _clear(self):
        self.seen_dhw = False
        self.ended = None
        self.started = None
        self.initial = None
        self.lowest = None
        self.peak = None
        self.water = None
        self.active = False
        self.phase = 'idle'

    def _finish(self, result, now, *, blocked=False):
        # Count a terminal outcome once, not on each subsequent guarded cycle.
        if self.ended is not None:
            self.last_result = result
            self.last_duration = max(0, now-self.started) if self.started is not None else None
            self.last_peak = self.peak
            self.counts['completed'] += int(result in ('cooled', 'normal_range'))
            self.counts['blocked'] += int(blocked)
        self._clear()
        self.phase = 'complete' if result in ('cooled', 'normal_range') else 'blocked' if blocked else 'idle'

    def reset(self, now=None):
        self._finish('interrupted', monotonic() if now is None else now)

    def check(self, dhw, water, now, normal_limit):
        self.active = False
        if dhw not in ('On', 'Off') or not isfinite(water) or not 0 <= water < 100:
            self.reset(now)
            raise ValueError('Invalid DHW recovery telemetry')
        if dhw == 'On':
            if not self.seen_dhw or self.ended is not None:
                self.reset(now)
            self.seen_dhw = True
            self.phase = 'dhw'
            raise ValueError('DHW priority')
        if self.seen_dhw and self.ended is None:
            self.ended = now
            self.counts['episodes'] += 1
        if not self.seen_dhw:
            self.phase = 'normal_guard' if water >= normal_limit else 'idle'
            if water >= normal_limit:
                raise ValueError('Boiler water outside experimental guard')
            return
        self.water = water
        if now-self.ended >= self.limits.dhw_window:
            self._finish('window_expired', now, blocked=water >= normal_limit)
            if water >= normal_limit:
                raise ValueError('DHW recovery window expired')
            return
        if water >= self.limits.dhw_ceiling:
            # Before CH ON, wait below the ceiling within the SAME end/window.
            # After a possible ON this is a failed return, never a fresh attempt.
            if self.started is not None:
                self.peak = max(self.peak, water)
                self._finish('ceiling', now, blocked=True)
            else:
                self.phase = 'ceiling_wait'
            raise ValueError('DHW residual water at or above ceiling')
        if self.started is None:
            # No circulation was requested: growth is not a failed cooling test.
            # Keep the observed DHW end even below the normal guard so the first
            # CH ON can move a hot pocket past the sensor without losing proof.
            self.phase = 'waiting_for_heat'
            self.active = True
            return
        self.lowest = min(self.lowest, water)
        self.peak = max(self.peak, water)
        elapsed = now-self.started
        if elapsed < self.limits.mixing_time:
            self.phase = 'mixing'
            self.active = True
            return
        if water < normal_limit:
            result = 'cooled' if water <= self.initial-self.limits.cooling_drop else 'normal_range'
            self._finish(result, now)
            return
        reason = None
        if water > self.lowest + self.limits.residual_rise:
            reason = ('rising', 'DHW residual water rising')
        if elapsed >= self.limits.cooling_time and water > self.initial-self.limits.cooling_drop:
            reason = ('not_cooling', 'DHW residual water not cooling')
        if reason:
            self._finish(reason[0], now, blocked=True)
            raise ValueError(reason[1])
        self.phase = 'cooling'
        self.active = True

    def heating_sent(self, water, now):
        if self.active and self.started is None:
            self.started, self.initial = now, water
            self.lowest = self.peak = water
            self.counts['starts'] += 1
            self.phase = 'mixing' if self.limits.mixing_time else 'cooling'

    def transport_error(self, now):
        """Suspend permission without renewing evidence or a mixing/cooling timer."""
        self.active = False
        self.phase = 'telemetry_wait'
        if self.ended is None or not 0 <= now-self.ended < self.limits.dhw_window:
            self.reset(now)

    def diagnostics(self, now, normal_limit):
        return dict(phase=self.phase, **self.counts,
                    effective_ceiling=self.limits.dhw_ceiling if self.active or self.phase == 'ceiling_wait' else normal_limit,
                    since_dhw_end=max(0, now-self.ended) if self.ended is not None else None,
                    since_ch_on=max(0, now-self.started) if self.started is not None else None,
                    water_change=self.water-self.initial if self.water is not None and self.initial is not None else None,
                    peak=self.peak, last_peak=self.last_peak,
                    last_result=self.last_result, last_duration=self.last_duration)

