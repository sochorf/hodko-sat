"""Native SAT5 boundary probes, plus acceptance for explicit fork fixes.

Native limitations here do not describe the separately routed Hodko PI/HR mode.

No network; real upstream controller/coordinator with mocked MQTT publication.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.sat.area import Area
from custom_components.sat.coordinator.fake import SatFakeCoordinator
from custom_components.sat.coordinator.mqtt.opentherm import SatOpenThermMqttCoordinator
from custom_components.sat.demand_control import SatDemandControl
from custom_components.sat.heating_control import HeatingDemand, SatHeatingControl
from custom_components.sat.helpers import timestamp, state_age_seconds
from custom_components.sat.types import HeaterState, PWMStatus
from tests.upstream.const import make_config


@pytest.fixture
def setup(hass, monkeypatch):
    config = make_config(data={'mode': 'mqtt_opentherm', 'device': 'test', 'mqtt_topic': 'OTGW'},
                         options={'maximum_setpoint': 55})
    coordinator = SatOpenThermMqttCoordinator(hass, config)
    publish = AsyncMock()
    monkeypatch.setattr('custom_components.sat.coordinator.mqtt.mqtt.async_publish', publish)
    monkeypatch.setattr('custom_components.sat.coordinator.mqtt.asyncio.sleep', AsyncMock())
    control = SatHeatingControl(hass, coordinator, config)
    return config, coordinator, control, publish


def demand(cs=45, mode=HVACMode.HEAT):
    return HeatingDemand(timestamp=timestamp(), valves_open=True, hvac_mode=mode,
                         requested_setpoint=cs, outside_temperature=5)


def payloads(publish):
    return [c.kwargs['payload'] for c in publish.call_args_list]


def test_existing_hodko_profile_stays_single_room_and_ignores_ally():
    config = make_config(data={'inside_sensor_entity_id': 'sensor.example_room_temperature'},
                         options={'maximum_setpoint': 55, 'hodko_control_mode': 'ally_demand',
                                  'hodko_ally': {'members': {'tv2': {}}}})
    assert config.rooms_only is False
    assert config.rooms == []
    assert config.limits.maximum_setpoint == 55


def test_existing_shadow_mode_is_not_supported():
    with pytest.raises(ValueError, match='ally_shadow'):
        _ = make_config(data={'mode': 'ally_shadow'}).mode


async def test_off_is_published_on_every_control_cycle(setup):
    _, _, control, publish = setup
    await control.update(demand(10, HVACMode.OFF))
    await control.update(demand(10, HVACMode.OFF))
    assert payloads(publish) == ['CS=10.0', 'PM=25', 'MM=100', 'CH=0'] * 2


async def test_cs_timeout_stops_later_writes(setup):
    _, coordinator, control, publish = setup
    publish.side_effect = [TimeoutError('test broker NoACK'), None, None, None]
    with pytest.raises(TimeoutError):
        await control.update(demand())
    assert payloads(publish) == ['CS=45.0']
    assert coordinator._requested_setpoint is None
    assert coordinator.setpoint is None  # No physical boiler acknowledgement.


async def test_cached_mqtt_values_loaded_without_original_timestamps(setup):
    _, coordinator, _, _ = setup
    coordinator._store = AsyncMock()
    coordinator._store.async_load.return_value = {'TSet': '55', 'centralheating': 'ON', 'flame': 'ON'}
    await coordinator._load_stored_data()
    assert coordinator.active and coordinator.flame_active
    assert coordinator.setpoint == 55
    assert set(coordinator.data) == {'TSet', 'centralheating', 'flame'}


def test_pi_hr_attributes_are_not_room_demand(hass):
    area = Area(make_config(), 'climate.ally')
    area._hass = hass
    hass.states.async_set('climate.ally', 'heat', {'temperature': 22, 'current_temperature': 19,
                                                'pi_heating_demand': 100, 'heat_required': True})
    area.pid = SimpleNamespace(available=True, output=10)
    assert area.valve_position is None
    assert area.requires_heat is False
    hass.states.async_set('climate.ally', 'heat', {'temperature': 22, 'current_temperature': 19,
                                                'pi_heating_demand': 0, 'heat_required': False})
    area.pid = SimpleNamespace(available=True, output=45)
    assert area.requires_heat is True


def test_sensor_age_uses_ha_updated_time_not_source_timestamp():
    now = dt_util.utcnow()
    state = State('sensor.source', '19', {'source_reported_at': (now-timedelta(days=2)).isoformat()},
                  last_updated=now, last_changed=now, last_reported=now)
    assert state_age_seconds(state) < 2


def test_restore_off_does_not_infer_heating_season(hass):
    config = make_config()
    coordinator = SatFakeCoordinator(hass, config)
    control = SatHeatingControl(hass, coordinator, config)
    controller = SatDemandControl(hass, coordinator, control, config)
    controller.restore(State('climate.sat', 'off', {'temperature': 21}), 5)
    assert controller.hvac_mode == HVACMode.OFF


async def test_pwm_off_also_sends_ch_off(setup, monkeypatch):
    _, coordinator, control, publish = setup
    coordinator.data.update({'Tboiler': 50, 'flame': 'ON', 'domestichotwater': 'OFF'})
    control._pwm._enabled = True
    control._pwm._status = PWMStatus.OFF
    monkeypatch.setattr(control._pwm, 'update', lambda **kw: None)
    await control.update(demand())
    assert control.control_setpoint == 10
    assert 'CH=0' in payloads(publish)


async def test_continuous_hot_flow_respects_configured_maximum(setup):
    config, coordinator, control, publish = setup
    coordinator.data.update({'Tboiler': 60, 'flame': 'ON', 'domestichotwater': 'OFF'})
    control._control_setpoint = 40
    await control.update(demand(50))
    assert config.limits.maximum_setpoint == 55
    assert control.control_setpoint == 55
    assert 'CS=55.0' in payloads(publish)


async def test_pwm_hot_flow_respects_configured_maximum(setup, monkeypatch):
    config, coordinator, control, publish = setup
    coordinator.data.update({'Tboiler': 60, 'flame': 'ON', 'domestichotwater': 'OFF'})
    control._pwm._enabled = True
    control._pwm._status = PWMStatus.ON
    monkeypatch.setattr(control._pwm, 'update', lambda **kw: None)
    control._device_tracker._last_flame_on_at = timestamp() - 60
    await control.update(demand(50))
    assert config.limits.maximum_setpoint == 55
    assert control.control_setpoint == 55
    assert 'CS=55.0' in payloads(publish)


def test_dhw_enabled_and_active_share_one_topic(setup):
    _, coordinator, _, _ = setup
    coordinator.data['domestichotwater'] = 'ON'
    assert coordinator.hot_water_active and coordinator.hot_water_enabled
    assert 'domestichotwaterenable' not in coordinator.get_tracked_entities()


async def test_pwm_on_without_return_temperature_uses_requested_setpoint(setup, monkeypatch):
    _, coordinator, control, publish = setup
    coordinator.data.update({'Tboiler': 35, 'flame': 'OFF', 'domestichotwater': 'OFF'})
    control._control_setpoint = 10
    control._pwm._enabled = True
    control._pwm._status = PWMStatus.ON
    monkeypatch.setattr(control._pwm, 'update', lambda **kw: None)
    await control.update(demand(45))
    assert coordinator.return_temperature is None
    assert control.control_setpoint == 45
    assert 'CH=1' in payloads(publish)
