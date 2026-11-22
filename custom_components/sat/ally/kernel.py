"""Offline decision prototype. No HA, MQTT, network or actuator interface.

Times are UTC epoch seconds; observed_at means verified source measurement,
NOT HA last_updated or an arbitrary MQTT payload/availability heartbeat.
All policy numbers are experimental; this module is not a deployable controller.
"""
from dataclasses import dataclass, field
from math import isfinite


@dataclass(frozen=True)
class Reading:
    value: float | bool | None
    observed_at: float | None
    verified: bool = False


@dataclass(frozen=True)
class Head:
    name: str
    zone: str
    pi: Reading
    heat_required: Reading
    # Explicit eligibility from existing mode/window/season policy; not heat_available.
    eligible: Reading


# Reviewed software envelope; operating maximum remains a saved parameter.
MAXIMUM_ALLOWED_WATER = 60
CORRECTION_RANGES = {
    'sustained_start': (1, 100, 1, '%'),
    'sustained_release': (0, 99, 1, '%'),
    'sustained_delay': (60, 3600, 1, 's'),
    'sustained_rise': (.01, 1, .01, '°C/min'),
    'sustained_fall': (.01, 1, .01, '°C/min'),
    'sustained_max': (0, 10, .5, '°C'),
    'sustained_pi_age': (60, 7200, 1, 's'),
    'sustained_gap': (10, 90, 1, 's'),
}


@dataclass(frozen=True)
class Policy:
    ttl: float = 3600
    start: float = 20
    stop: float = 10
    residual: float = 5
    wait: float = 600
    minimum_water: float = 30
    maximum_water: float = 45
    # Two experimentally selected weather-curve anchors, not a fitted house model.
    outside_cold: float = -10
    water_cold: float = 45
    outside_warm: float = 15
    water_warm: float = 30
    boost: float = 5
    sustained_start: float = 85
    sustained_release: float = 65
    sustained_delay: float = 600
    sustained_rise: float = 0.2  # degrees per minute after the delay
    sustained_fall: float = 0.3
    sustained_max: float = 5
    # Correction may use the same verified PI lifetime as heating. The normal
    # freshness check still caps this by ttl; no source timestamp is renewed.
    sustained_pi_age: float = 3900
    sustained_gap: float = 90

    def __post_init__(self):
        if not all(isfinite(v) for v in vars(self).values()):
            raise ValueError('Policy must be finite')
        if not (self.ttl > 0 and self.wait > 0 and
                0 <= self.residual <= self.stop < self.start <= 100 and
                0 < self.minimum_water <= self.water_warm <= self.water_cold <= self.maximum_water and
                self.outside_cold < self.outside_warm and self.boost >= 0 and
                0 <= self.sustained_release < self.sustained_start <= 100 and
                self.sustained_delay > 0 and self.sustained_rise > 0 and
                self.sustained_fall > 0 and self.sustained_max >= 0 and
                self.sustained_pi_age > 0 and self.sustained_gap > 0):
            raise ValueError('Invalid policy')


@dataclass
class Memory:
    calling: dict[str, bool] = field(default_factory=dict)
    waiting: dict[str, float] = field(default_factory=dict)
    last_time: float | None = None
    sustained: dict[str, tuple[float | None, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    # None = insufficient evidence; not an actuator instruction to hold forever.
    heat: bool | None
    water: float | None
    zones: dict[str, bool | None]
    reasons: dict[str, str]
    degraded: bool
    corrections: dict[str, float] = field(default_factory=dict)


def fresh(r: Reading, now: float, epoch: float, ttl: float) -> bool:
    return (r.verified and r.observed_at is not None and
            isfinite(r.observed_at) and epoch <= r.observed_at <= now and
            now - r.observed_at <= ttl)


def decide(heads: list[Head], outside: Reading, now: float, epoch: float,
           memory: Memory, policy: Policy = Policy(), *, enabled: bool = True) -> Decision:
    """Return a shadow proposal; explicit disable dominates all sensor states.

    epoch is the current observer session start. Restored retained data cannot
    re-arm the prototype. Missing heads must remain present as unknown inputs.
    No room temperature or room target participates in this calculation.
    """
    if not isfinite(now) or not isfinite(epoch) or epoch > now:
        raise ValueError('Invalid time')
    if memory.last_time is not None and now < memory.last_time:
        raise ValueError('Replay must be chronological')
    previous_time = memory.last_time
    dt = 0 if previous_time is None else now - previous_time
    if dt > policy.sustained_gap:
        memory.sustained.clear()
        dt = 0
    memory.last_time = now
    if not enabled:
        memory.calling.clear()
        memory.waiting.clear()
        memory.sustained.clear()
        return Decision(False, None, {}, {'controller': 'disabled'}, False)
    if len({h.name for h in heads}) != len(heads):
        raise ValueError('Duplicate head')
    values: dict[str, list[float | None]] = {}
    recent_demands = {}
    reasons = {}
    for h in heads:
        value = None
        if not fresh(h.eligible, now, epoch, policy.ttl) or type(h.eligible.value) is not bool:
            reason = 'eligibility_unknown'
        elif not h.eligible.value:
            value, reason = 0.0, 'blocked_by_room_policy'
        elif not fresh(h.heat_required, now, epoch, policy.ttl) or type(h.heat_required.value) is not bool:
            reason = 'heat_required_unknown'
        elif not h.heat_required.value:
            value, reason = 0.0, 'head_requests_no_heat'
        elif (not fresh(h.pi, now, epoch, policy.ttl) or type(h.pi.value) not in (int, float)
              or not isfinite(h.pi.value) or not 0 <= h.pi.value <= 100):
            reason = 'pi_unknown'
        else:
            value, reason = float(h.pi.value), 'valid_demand'
            if now - h.pi.observed_at <= policy.sustained_pi_age:
                recent_demands[h.zone] = max(recent_demands.get(h.zone, 0), value)
        reasons[h.name] = reason
        values.setdefault(h.zone, []).append(value)
    # Explicit zone mapping: PR can have two heads; do not infer from HA areas.
    zones = {}
    accepted = []
    corrections = {}
    for zone, inputs in values.items():
        known = [v for v in inputs if v is not None]
        demand = max(known, default=0.0)
        previous = memory.calling.get(zone, False)
        if demand >= policy.start or (previous and demand > policy.stop):
            call = True
            memory.waiting.pop(zone, None)
        elif demand > policy.residual:
            since = memory.waiting.setdefault(zone, now)
            call = now - since >= policy.wait
        else:
            memory.waiting.pop(zone, None)
            call = False
        if not call and None in inputs:
            zones[zone] = None
            # Do not preserve demand or elapsed wait through unobserved gaps.
            memory.calling.pop(zone, None)
            memory.waiting.pop(zone, None)
        else:
            zones[zone] = call
            memory.calling[zone] = call
        if call:
            since, correction = memory.sustained.get(zone, (None, 0.0))
            recent = recent_demands.get(zone)
            if recent is None:
                since, correction = None, 0.0
            elif recent >= policy.sustained_start:
                if since is None:
                    since = now
                active_dt = min(dt, max(0, now - since - policy.sustained_delay))
                correction = min(policy.sustained_max, correction + active_dt * policy.sustained_rise / 60)
            else:
                since = None
                if recent <= policy.sustained_release:
                    correction = max(0, correction - dt * policy.sustained_fall / 60)
            memory.sustained[zone] = (since, correction)
            corrections[zone] = round(correction, 3)
            accepted.append(policy.boost * demand / 100 + correction)
        else:
            memory.sustained.pop(zone, None)
    for zone in set(memory.calling) | set(memory.waiting):
        if zone not in values:
            memory.calling.pop(zone, None)
            memory.waiting.pop(zone, None)
    for zone in set(memory.sustained) - set(values):
        memory.sustained.pop(zone, None)
    degraded = not heads or any(v is None for group in values.values() for v in group)
    if not accepted:
        heat = None if degraded else False
        return Decision(heat, None, zones, reasons, degraded)
    if (not fresh(outside, now, epoch, policy.ttl) or type(outside.value) not in (int, float)
            or not isfinite(outside.value) or not -50 <= outside.value <= 60):
        memory.sustained.clear()
        reasons['water'] = 'outside_unknown'
        return Decision(None, None, zones, reasons, True)
    fraction = min(1.0, max(0.0, (policy.outside_warm - outside.value) /
                            (policy.outside_warm - policy.outside_cold)))
    base = policy.water_warm + fraction * (policy.water_cold - policy.water_warm)
    water = min(policy.maximum_water, max(policy.minimum_water,
                base + max(accepted)))
    return Decision(True, round(water, 2), zones, reasons, degraded, corrections)
