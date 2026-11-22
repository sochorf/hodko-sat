"""Explicit same-entry mode transition. No writes until user confirms stop."""
import asyncio
from copy import deepcopy
import json
from math import isfinite
from time import monotonic
from uuid import uuid4

import voluptuous as vol
from homeassistant.components.climate import HVACMode
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from ..const import DOMAIN, CLIMATE, CONF_MAXIMUM_SETPOINT
from .runtime import Runtime, OperatingLimits
from dataclasses import asdict
from .shadow_adapter import validate
from .kernel import Policy, MAXIMUM_ALLOWED_WATER, CORRECTION_RANGES






async def shadow_limits_flow(flow, user_input=None):
    """Explicitly copy reviewed parameters to this observer; never touch its owner.

    Matching membership and telemetry identity prevents copying another boiler's
    settings. A stale form or a not-yet-loaded active runtime cannot be applied.
    Only this shadow entry's options are saved/reloaded by HA.
    """
    entry = flow.config_entry
    settings = entry.options.get('hodko_shadow', entry.data['hodko_shadow'])
    candidates = {}
    for source in flow.hass.config_entries.async_entries(DOMAIN):
        active = source.options.get('hodko_ally', {})
        if (source.data.get('mode') != 'mqtt_opentherm' or
                source.options.get('hodko_control_mode') != 'ally_demand'):
            continue
        if any(active.get(k) != settings.get(k) for k in
               ('topic', 'members', 'enabled_entity', 'monitor_url', 'otgw_mqtt_topic')):
            continue
        if active.get('telemetry_transport', 'http') != settings.get('telemetry_transport', 'http'):
            continue
        candidates[source.entry_id] = source
    if not candidates:
        return flow.async_abort(reason='shadow_no_matching_sat')
    errors = {}
    if user_input is not None:
        try:
            source = candidates[user_input['source_entry']]
            preview = flow._shadow_limits_preview
            if (dict(entry.options) != preview['options'] or
                    dict(source.options) != preview['sources'].get(source.entry_id)):
                raise ValueError('Settings changed since preview')
            c = flow.hass.data[DOMAIN][source.entry_id][CLIMATE]
            runtime = c._ally_runtime
            active = source.options['hodko_ally']
            if (runtime is None or runtime.closed or runtime.settings != active or
                    c._coordinator.maximum_setpoint != active.get('policy', {}).get('maximum_water', 45)):
                raise ValueError('Active settings have not loaded consistently')
            proposed = deepcopy(settings)
            proposed['policy'] = asdict(runtime.policy)
            proposed['operating_limits'] = asdict(runtime.limits)
            validate(proposed)
            return flow.async_create_entry(title='', data={**entry.options, 'hodko_shadow': proposed})
        except (ValueError, TypeError, KeyError, AttributeError):
            errors['base'] = 'shadow_sync_failed'
    flow._shadow_limits_preview = {
        'options': deepcopy(dict(entry.options)),
        'sources': {key: deepcopy(dict(source.options)) for key, source in candidates.items()}}
    return flow.async_show_form(step_id='shadow_limits', errors=errors, data_schema=vol.Schema({
        vol.Required('source_entry'): selector.SelectSelector(selector.SelectSelectorConfig(options=[
            {'value': key, 'label': source.title} for key, source in candidates.items()]))}))


class ModeFlow:
    async def async_step_operating_limits(self, user_input=None):
        if (self._config_entry.data.get('mode') != 'mqtt_opentherm' or
                self._config_entry.options.get('hodko_control_mode') != 'ally_demand'):
            return self.async_abort(reason='mode_transport')
        errors = {}
        settings = deepcopy(self._config_entry.options['hodko_ally'])
        policy = Policy(**settings.get('policy', {}))
        defaults = {**{k: getattr(policy, k) for k in CORRECTION_RANGES}, **asdict(OperatingLimits()), **settings.get('operating_limits', {}),
                    'maximum_water': settings.get('policy', {}).get('maximum_water', 45)}
        if user_input is not None:
            try:
                values = dict(user_input)
                maximum = values.pop('maximum_water')
                if type(maximum) not in (int, float) or not isfinite(maximum) or not 10 < maximum <= MAXIMUM_ALLOWED_WATER:
                    raise ValueError('Invalid maximum')
                correction = {k: values.pop(k, getattr(policy, k)) for k in CORRECTION_RANGES}
                for k, value in correction.items():
                    low, high, _, _ = CORRECTION_RANGES[k]
                    if type(value) not in (int, float) or not isfinite(value) or not low <= value <= high:
                        raise ValueError('Invalid correction parameter')
                limits = OperatingLimits(**values)
                limits.validate_water(maximum)
                settings['operating_limits'] = asdict(limits)
                settings.setdefault('policy', {}).update(correction, maximum_water=maximum)
                c = self.hass.data[DOMAIN][self._config_entry.entry_id][CLIMATE]
                Runtime(c, settings, validation_cap=maximum)  # Pure proposed-options validation; no live mutation.
                self._mode_original = deepcopy(dict(self._config_entry.options))
                self._mode_target = {**self._mode_original, 'hodko_ally': settings,
                                     CONF_MAXIMUM_SETPOINT: maximum}
                self._mode_settings, self._mode_climate = settings, c
                self._mode_baseline = self._mode_stopped = None
                self._limits_change = True
                return await self.async_step_control_mode_confirm()
            except (ValueError, TypeError, KeyError, AttributeError):
                errors['base'] = 'invalid_operating_limits'
        ranges = {**CORRECTION_RANGES, 'maximum_water': (11, MAXIMUM_ALLOWED_WATER, .5, '°C'),
                  'water_margin': (5, 20, .5, '°C'), 'dhw_ceiling': (60, 80, .5, '°C'),
                  'dhw_window': (180, 1800, 1, 's'), 'cooling_time': (60, 600, 1, 's'),
                  'mixing_time': (0, 120, 1, 's'),
                  'cooling_drop': (.5, 5, .1, '°C'), 'residual_rise': (.5, 3, .1, '°C'),
                  'restart_delay': (180, 900, 1, 's'), 'ramp_rate': (.1, 2, .1, '°C/min')}
        return self.async_show_form(step_id='operating_limits', errors=errors,
            data_schema=vol.Schema({vol.Required(k, default=v): selector.NumberSelector(
                selector.NumberSelectorConfig(min=ranges[k][0], max=ranges[k][1],
                    step=ranges[k][2], unit_of_measurement=ranges[k][3], mode=selector.NumberSelectorMode.BOX))
                for k,v in defaults.items()}))

    async def _read_mode_monitor(self):
        from .mqtt_monitor import topic, shared_mqtt_reader
        if namespace := topic(self._mode_settings):
            # Config flow consumes original receipt evidence as well.
            return await shared_mqtt_reader(self.hass, namespace).read()
        async with asyncio.timeout(5):
            async with async_get_clientsession(self.hass).get(
                self._mode_settings['monitor_url'], allow_redirects=False
            ) as response:
                response.raise_for_status()
                return (await response.json())['otmonitor']

    async def async_step_control_mode(self, user_input=None):
        self._limits_change = False
        errors = {}
        current = self._config_entry.options.get('hodko_control_mode', 'room_pid')
        if self._config_entry.data.get('mode') != 'mqtt_opentherm':
            return self.async_abort(reason='mode_transport')
        default = self._config_entry.options.get('hodko_ally', {})
        if not default:
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if entry.data.get('mode') == 'ally_shadow':
                    default = entry.data.get('hodko_shadow', {})
                    break
        if user_input is not None:
            try:
                mode = user_input['control_mode']
                if mode not in ('room_pid', 'ally_demand'):
                    raise ValueError('mode')
                if mode == current and mode != 'ally_demand':
                    return self.async_abort(reason='mode_unchanged')
                self._mode_original = deepcopy(dict(self._config_entry.options))
                c = self.hass.data[DOMAIN][self._config_entry.entry_id][CLIMATE]
                if mode == 'ally_demand':
                    settings = validate(json.loads(user_input['settings']))
                    Runtime(c, settings)  # Pure validation; no setup/subscription/writes.
                    if mode == current:
                        if settings == self._mode_original.get('hodko_ally'):
                            return self.async_abort(reason='mode_unchanged')
                        # Same owner, settings-only transition: preserve room backup,
                        # limits and all other options. Confirmation still stops SAT.
                        self._mode_target = {**self._mode_original, 'hodko_ally': settings}
                        self._limits_change = True
                    else:
                        target = c.target_temperature
                        if target is None or not isfinite(target):
                            raise ValueError('Missing rollback target')
                        self._mode_target = {**self._mode_original,
                            'hodko_control_mode': mode, 'hodko_ally': settings,
                            'hodko_room_backup': {'options': deepcopy(self._mode_original),
                                                  'target': target}}
                else:
                    backup = self._mode_original['hodko_room_backup']
                    if not isfinite(float(backup['target'])):
                        raise ValueError('Invalid rollback target')
                    self._mode_target = deepcopy(backup['options'])
                    self._mode_target.update(hodko_control_mode='room_pid',
                        hodko_return_id=uuid4().hex, hodko_return_target=float(backup['target']))
                    settings = validate(self._mode_original['hodko_ally'])
                self._mode_settings = settings
                self._mode_climate = c
                self._mode_baseline = None
                self._mode_stopped = None
                return await self.async_step_control_mode_confirm()
            except (ValueError, TypeError, KeyError, AttributeError):
                errors['base'] = 'invalid_control_mode'
        return self.async_show_form(step_id='control_mode', errors=errors, data_schema=vol.Schema({
            vol.Required('control_mode', default=current): selector.SelectSelector(
                selector.SelectSelectorConfig(options=['room_pid','ally_demand'])),
            vol.Optional('settings', default=json.dumps(default, indent=2)):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True))}))

    async def async_step_control_mode_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                c = self._mode_climate
                if dict(self._config_entry.options) != self._mode_original:
                    raise ValueError('Options changed; start over')
                if self.hass.data[DOMAIN][self._config_entry.entry_id][CLIMATE] is not c:
                    raise ValueError('Entry reloaded; start over')
                if self._mode_stopped is None:
                    if 'hodko_room_backup' in self._mode_target and not getattr(self, '_limits_change', False):
                        target = c.target_temperature
                        if target is None or not isfinite(target):
                            raise ValueError('Missing rollback target')
                        self._mode_target['hodko_room_backup']['target'] = target
                    # Explicit user confirmation authorizes stop, not HEAT.
                    await c.async_set_hvac_mode(HVACMode.OFF, cascade=False)
                    self._mode_stopped = monotonic()
                raw = await self._read_mode_monitor()
                from .mqtt_monitor import MqttSnapshot
                if isinstance(raw, MqttSnapshot):
                    now = monotonic()
                    ch, cs = raw.get('chenable'), raw.get('controlsetpoint')
                    valid = bool(ch and cs and ch[1] == 'Off' and 0 <= cs[1] <= 10
                                 and all(self._mode_stopped <= r[2] <= now
                                         and now-r[2] <= 90 for r in (ch, cs)))
                else:
                    fields = {}
                    for key in ('chenable','controlsetpoint'):
                        row = raw[key]
                        if type(row['epoch']) is not int or row['epoch'] <= 0:
                            raise ValueError('Invalid epoch')
                        fields[key] = (row['epoch'], row['value'])
                    now = monotonic()
                    baseline = self._mode_baseline
                    self._mode_baseline = (now, fields)
                    valid = (baseline is not None and 0 < now-baseline[0] <= 90
                        and all(fields[k][0] > baseline[1][k][0] for k in fields)
                        and fields['chenable'][1] == 'Off'
                        and type(fields['controlsetpoint'][1]) in (int,float)
                        and abs(fields['controlsetpoint'][1]-10) <= .6)
                if not valid:
                    errors['base'] = 'waiting_for_stop'
                else:
                    # Serialize final check against manual HEAT. Options reload
                    # unloads the same owner; never creates a second entry.
                    async with c._mode_lock:
                        if self.hass.data[DOMAIN][self._config_entry.entry_id][CLIMATE] is not c:
                            raise ValueError('Entry changed during verification')
                        if dict(self._config_entry.options) != self._mode_original:
                            raise ValueError('Options changed during verification')
                        if c.hvac_mode != HVACMode.OFF:
                            raise ValueError('Mode changed; stop again')
                        c._hodko_transition_pending = True
                        return self.async_create_entry(title='', data=self._mode_target)
            except Exception:
                errors['base'] = 'mode_transition_failed'
        return self.async_show_form(step_id='control_mode_confirm', errors=errors,
                                   data_schema=vol.Schema({}))
