"""Cross-layer contracts on the real HA entity; all transport endpoints are fakes."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.ally.shadow import Observer
from tests.test_ally_ha import SETTINGS, clock_and_mqtt, ready
from tests.test_ally_mode import event
from custom_components.sat.const import MODE_FAKE

CONFIG = deepcopy(SETTINGS)
CONFIG['policy'].update(maximum_water=55, water_cold=55)
CONFIG['members']['0x0000000000000002'] = dict(name='b', zone='B', eligible_entity='binary_sensor.b_eligible')
pytestmark = pytest.mark.parametrize('domains,data,options,config', [
    ([], {'mode': MODE_FAKE}, {'hodko_control_mode':'ally_demand', 'hodko_ally':CONFIG, 'maximum_setpoint':55}, {})])


async def test_real_runtime_shadow_agree_before_ramp_at_55(ready, hass):
    c, r, k, clock = ready
    hass.states.async_set('binary_sensor.b_eligible', 'on')
    r.poll.return_value['outsidetemperature']['value'] = -10
    shadow = Observer(CONFIG['members'], CONFIG['enabled_entity'], 1000, r.policy, r.limits)
    shadow.reports.accept(deepcopy(r.reports.last_event), 1200)
    shadow.telemetry = deepcopy(r.telemetry)
    result = shadow.evaluate({CONFIG['enabled_entity']:'on', 'binary_sensor.a_eligible':'on',
                              'binary_sensor.b_eligible':'on'}, deepcopy(r.poll.return_value), 1200, 1200)
    await c.async_control_heating_loop()
    assert result['proposed_water'] == r.decision.water == 55
    assert result['zones'] == r.decision.zones
    assert result['writes'] is False
    # Shadow proposal is deliberately not the command after startup ramp.
    k.async_set_control_setpoint.assert_awaited_with(41)
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.ON)


async def test_bad_other_head_does_not_stop_confirmed_heat_or_restart_ramp(ready, hass):
    c, r, k, clock = ready
    hass.states.async_set('binary_sensor.b_eligible', 'on')
    await c.async_control_heating_loop()
    for seq, now in ((2, 1215), (3, 1230), (4, 1245)):
        clock[0] = now
        bad = event(seq=seq, at=now, heat_required=dict(value=None, valid=False))
        bad['device_ieee'] = '0x0000000000000002'
        revision = r.revision
        r.message(SimpleNamespace(payload=json.dumps(bad), retain=False))
        assert r.revision == revision + 1
        r.poll.return_value['chenable']['value'] = 'On'
        r.poll.return_value['controlsetpoint']['value'] = c._setpoint
        for field in r.poll.return_value.values(): field['epoch'] = seq + 1
        await c.async_control_heating_loop()
        assert r.decision.heat is True and r.decision.degraded
        assert r.tripped is None and c._setpoint <= 55
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.ON)
    assert c._setpoint > 41  # A bad member did not reset the water ramp.


async def test_successful_publish_is_not_a_boiler_ack(ready):
    c, r, k, clock = ready
    await c.async_control_heating_loop()
    original = r.pending[2]
    for epoch, now in enumerate((1230, 1260, 1291), start=3):
        clock[0] = now
        # New telemetry, but the boiler still reports OFF/CS10.
        for field in r.poll.return_value.values(): field['epoch'] = epoch
        await c.async_control_heating_loop()
        if now < original + 90: assert r.pending[2] == original
    assert r.tripped == 'command_not_confirmed'
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_with(10)
