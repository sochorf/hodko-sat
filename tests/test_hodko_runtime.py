"""Actual patched HA entity tests using the upstream fake coordinator, no devices."""
import socket
import subprocess
import sys
from unittest.mock import AsyncMock
import pytest
import pytest_socket
from homeassistant.components.climate import HVACMode
from custom_components.sat.compat.const import MODE_FAKE, MINIMUM_SETPOINT
from custom_components.sat.compat.coordinator import DeviceState

PARAMETERS = [([], {'mode': MODE_FAKE, 'minimum_setpoint': 41, 'maximum_setpoint': 50}, {}, {})]

@pytest.mark.parametrize('domains,data,options,config', PARAMETERS)
async def test_real_entity_off_with_inactive_telemetry(hass, entry, climate, coordinator):
    climate.schedule_control_heating_loop = lambda *a, **k: None
    coordinator.async_set_heater_state = AsyncMock()
    coordinator.async_set_control_setpoint = AsyncMock()
    await climate.async_set_hvac_mode(HVACMode.OFF)
    coordinator.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    coordinator.async_set_control_setpoint.assert_awaited_with(MINIMUM_SETPOINT)
    assert climate.hvac_mode == HVACMode.OFF

@pytest.mark.parametrize('domains,data,options,config', PARAMETERS)
async def test_real_entity_shutdown_failure_is_visible(hass, entry, climate, coordinator):
    climate.schedule_control_heating_loop = lambda *a, **k: None
    coordinator.async_set_heater_state = AsyncMock(side_effect=RuntimeError('fake transport error'))
    coordinator.async_set_control_setpoint = AsyncMock()
    with pytest.raises(RuntimeError):
        await climate.async_set_hvac_mode(HVACMode.OFF)
    coordinator.async_set_control_setpoint.assert_awaited_with(MINIMUM_SETPOINT)
    assert climate.hvac_mode == HVACMode.OFF

def test_network_guard():
    # Verify the configured guard in isolation: HA treats even an intentionally
    # blocked socket attempt in its own process as an unexpected test error.
    assert socket.socket is not socket.SocketType
    subprocess.run([sys.executable, '-c', """
import socket
import pytest_socket
pytest_socket.disable_socket(allow_unix_socket=True)
for family in (socket.AF_INET, socket.AF_INET6):
    try:
        socket.socket(family, socket.SOCK_STREAM)
    except pytest_socket.SocketBlockedError:
        pass
    else:
        raise AssertionError('Network socket guard did not block')
"""], check=True)


@pytest.fixture
async def startup_pid(hass, climate, coordinator, monkeypatch):
    from custom_components.sat.compat import pid as pid_module
    from homeassistant.core import State
    from custom_components.sat.compat import climate as climate_module
    c = climate
    c.schedule_control_heating_loop = lambda *a, **k: None
    values = dict(inside=None, outside=7.9, target=22.0, water=52.9)
    for attr, key in [('current_temperature','inside'), ('current_outside_temperature','outside'),
                      ('target_temperature','target')]:
        monkeypatch.setattr(type(c), attr, property(lambda self, key=key: values[key]))
    monkeypatch.setattr(type(coordinator), 'boiler_temperature_filtered', property(lambda self: values['water']))
    clock = [0.0]
    monkeypatch.setattr(pid_module, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(climate_module, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(pid_module, 'seconds_since', lambda at: clock[0]-at)
    c.pid._automatic_gains = False
    c.pid._kp, c.pid._ki, c.pid._kd = 23.866667, 0.002841, 14033.600196
    c.pid._sample_time_limit = 10
    # Measured heating curve at the failing startup; keep recorded gains fixed.
    c.heating_curve._last_heating_curve_value = 35.8
    monkeypatch.setattr(c.heating_curve, 'update', lambda *args: None)
    c.pid.restore(State('climate.test','heat',{'error':2.7,'integral':0,'derivative_raw':-0.001,'heating_curve':35.8}))
    c._pid_needs_initial_sample = True
    c._calculated_setpoint = 93.5
    c._hvac_mode = HVACMode.HEAT
    coordinator.async_set_heater_state = AsyncMock()
    coordinator.async_set_control_setpoint = AsyncMock()
    return c, coordinator, values, clock


@pytest.mark.parametrize('domains,data,options,config', PARAMETERS)
async def test_pid_replay_oct20_startup(startup_pid):
    """Observed WR sequence, relative to 08:49:15; not a thermal simulation."""
    c,k,v,clock = startup_pid
    for t in (0,50,109):
        clock[0]=t;c._async_control_pid()
        assert c.pid.last_error==2.7
        assert c.pid.raw_derivative==-0.001  # Missing input never becomes error 0.
        await c.async_control_heating_loop()
    k.async_set_heater_state.assert_not_awaited()
    for t,temp in [(109.023,19.5),(248,19.5),(323,19.7),(757,19.9)]:
        clock[0]=t;v['inside']=temp;c._async_control_pid()
        assert c.pid.last_error==pytest.approx(22-temp)
        assert c.pid.derivative > -20  # No observed spurious -164 contribution.
        assert c.requested_setpoint>28.2
        if t==109.023:
            assert c.pid.derivative==0
            assert c.requested_setpoint==95.5
    assert c.pid.derivative < 0  # Real subsequent warming still contributes D.
    c._calculated_setpoint=c.requested_setpoint
    c._overshoot_protection=False
    await c._async_control_setpoint(c.pwm.state)
    assert k.async_set_control_setpoint.await_args.args[0]<=k.maximum_setpoint
    assert k.async_set_control_setpoint.await_args.args[0]>28.2


@pytest.mark.parametrize('domains,data,options,config', PARAMETERS)
@pytest.mark.parametrize('key,bad',[('inside',None),('outside',None),('target',None),
                                  ('inside',float('nan')),('outside',float('inf'))])
async def test_pid_recovers_missing_input_with_same_error_without_kick(startup_pid,key,bad):
    c,k,v,clock=startup_pid
    v['inside']=19.5;c._async_control_pid()
    c.pid._raw_derivative=-0.012;c._calculated_setpoint=10
    saved=v[key];v[key]=bad;clock[0]=100;c._async_control_pid()
    assert c._pid_needs_initial_sample
    assert c.pid.last_error==2.5
    v[key]=saved;clock[0]=100.001;c._async_control_pid()
    assert not c._pid_needs_initial_sample
    assert c.pid.last_error==2.5 and c.pid.derivative==0
    assert c._calculated_setpoint is None


@pytest.mark.parametrize('domains,data,options,config', PARAMETERS)
async def test_loop_seeds_restored_pid_before_first_command_and_waits_for_water(startup_pid):
    c,k,v,clock=startup_pid
    v['inside']=19.5;v['water']=None
    await c.async_control_heating_loop()
    assert c._pid_needs_initial_sample
    k.async_set_control_setpoint.assert_not_awaited()
    v['water']=52.9
    await c.async_control_heating_loop()
    assert not c._pid_needs_initial_sample
    assert c.pid.last_error==2.5 and c.pid.derivative==0
    assert k.async_set_control_setpoint.await_count


@pytest.mark.parametrize('domains,data,options,config', PARAMETERS)
async def test_unavailable_sensor_event_marks_recovery_even_between_cycles(startup_pid):
    from types import SimpleNamespace
    from homeassistant.core import State
    c,k,v,clock=startup_pid
    v['inside']=19.5;c._async_control_pid()
    c.pid._raw_derivative=-0.012
    await c._async_inside_sensor_changed(SimpleNamespace(data={'new_state':State('sensor.test','unavailable')}))
    assert c._pid_needs_initial_sample
    c._async_control_pid()
    assert c.pid.derivative==0
