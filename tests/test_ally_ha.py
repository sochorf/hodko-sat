import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from homeassistant.components.climate import HVACMode
from custom_components.sat.compat.const import MODE_FAKE
from custom_components.sat.compat.coordinator import DeviceState
from custom_components.sat.ally import runtime
from custom_components.sat.ally.monitor import MonitorUnavailable
from tests.test_ally_mode import event, ts

SETTINGS=dict(schema_version=1, topic='test/ally/events', monitor_url='http://test.invalid/api/v2/otgw/otmonitor',
    enabled_entity='input_boolean.heat_allowed', members={'0x0000000000000001':dict(name='a',zone='A',eligible_entity='binary_sensor.a_eligible')},
    policy=dict(ttl=3900, minimum_water=41, maximum_water=50, water_warm=41, water_cold=50))
PARAMS=[([],{'mode':MODE_FAKE}, {'hodko_control_mode':'ally_demand','hodko_ally':SETTINGS,'maximum_setpoint':50}, {})]
pytestmark=pytest.mark.parametrize('domains,data,options,config',PARAMS)

@pytest.fixture(autouse=True)
def clock_and_mqtt(monkeypatch):
    clock=[1000.0]
    monkeypatch.setattr(runtime,'time',lambda:clock[0])
    monkeypatch.setattr(runtime,'monotonic',lambda:clock[0])
    monkeypatch.setattr(runtime.mqtt,'async_subscribe',AsyncMock(return_value=lambda:None))
    return clock

@pytest.fixture
async def ready(hass,climate,coordinator,clock_and_mqtt):
    c=climate; r=c._ally_runtime
    c.schedule_control_heating_loop=lambda *a:None
    c.pid.update=Mock(side_effect=AssertionError('PID must not run'))
    c._async_control_pid=Mock(side_effect=AssertionError('PID must not run'))
    hass.states.async_set('input_boolean.heat_allowed','on')
    hass.states.async_set('binary_sensor.a_eligible','on')
    await hass.async_block_till_done()
    await c.async_set_hvac_mode(HVACMode.HEAT)
    coordinator.async_set_heater_state=AsyncMock()
    coordinator.async_set_control_setpoint=AsyncMock()
    r.pending=None
    r.modulation_confirmed=True
    coordinator.async_set_control_max_relative_modulation=AsyncMock()
    clock_and_mqtt[0]=1200
    r.reports.accept(event(at=1200,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=100,valid=True)),1200)
    values=dict(outsidetemperature=5,chenable='Off',controlsetpoint=10,boilertemperature=40,faultindicator='Off',dhwmode='Off',maxrelmodlvl=100)
    r.telemetry.observe({k:dict(value=v,epoch=1) for k,v in values.items()},1190)
    r.poll=AsyncMock(return_value={k:dict(value=v,epoch=2) for k,v in values.items()})
    return c,r,coordinator,clock_and_mqtt

async def test_real_entity_uses_pi_and_no_room_pid(ready):
    c,r,k,clock=ready
    assert c.current_temperature is None
    await c.async_control_heating_loop()
    k.async_set_control_setpoint.assert_awaited_with(41)
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert r.status=='heat'
    c._async_control_pid.assert_not_called()

async def test_room_target_rejected(ready):
    c,r,k,clock=ready
    with pytest.raises(ValueError):await c.async_set_target_temperature(30)
    with pytest.raises(ValueError):await c.async_set_preset_mode('comfort')
    k.async_set_heater_state.assert_not_awaited()

async def test_expired_hr_stops_and_recovers_only_with_evidence(ready):
    c,r,k,clock=ready
    r.reports.heads['a']['heat_required']['observed_at']=ts(1)
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    assert r.status=='unknown'
    clock[0]=1210
    r.reports.accept(event(seq=2,at=1210,heat_required=dict(value=True,valid=True)),1210)
    for field in r.poll.return_value.values():field['epoch']=3
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)

@pytest.mark.parametrize('wait_at',['poll','setpoint','on'])
async def test_off_during_each_await_has_no_on_after_completed_off(ready,wait_at):
    c,r,k,clock=ready
    entered,release=asyncio.Event(),asyncio.Event()
    target=r.poll if wait_at=='poll' else k.async_set_control_setpoint if wait_at=='setpoint' else k.async_set_heater_state
    original=r.poll.return_value
    async def pause(*args):
        entered.set();await release.wait();return original
    target.side_effect=pause
    calls=[]
    if wait_at!='on':k.async_set_heater_state.side_effect=lambda state:calls.append(state)
    work=asyncio.create_task(c.async_control_heating_loop());await entered.wait()
    off=asyncio.create_task(c.async_set_hvac_mode(HVACMode.OFF));await asyncio.sleep(0)
    release.set();await work;await off
    assert c.hvac_mode==HVACMode.OFF
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    before=k.async_set_heater_state.await_count
    await c.async_control_heating_loop()
    assert k.async_set_heater_state.await_args.args==(DeviceState.OFF,)
    assert all(call.args!=(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list[before:])

async def test_publish_error_attempts_both_stop_commands(ready):
    c,r,k,clock=ready
    k.async_set_heater_state.side_effect=RuntimeError('offline fake')
    with pytest.raises(RuntimeError):await c.async_control_heating_loop()
    k.async_set_control_setpoint.assert_awaited_with(10)
    assert r.ramp.on is False

async def test_gate_change_during_write_stops(ready,hass):
    c,r,k,clock=ready
    async def change(value):
        hass.states.async_set('input_boolean.heat_allowed','off')
    k.async_set_control_setpoint.side_effect=change
    await c.async_control_heating_loop()
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)

async def test_boot_does_not_restore_heat(hass,climate):
    assert climate.hvac_mode==HVACMode.OFF
    assert climate._ally_runtime.reports.heads=={}

async def test_pending_ack_not_extended_or_confirmed_by_old_cache(ready):
    c,r,k,clock=ready
    r.sent(True,41);clock[0]=1291
    r.sent(True,43)
    assert r.pending[2]==1200
    r.telemetry.fields['chenable']=(2,'On',1190)
    r.telemetry.fields['controlsetpoint']=(2,41,1190)
    r.confirm(1270)
    assert r.pending is not None
    r.telemetry.fields['chenable']=(3,'Off',1290)
    r.telemetry.fields['controlsetpoint']=(3,10,1290)
    with pytest.raises(ValueError):r.confirm(1291)
    assert r.tripped=='command_not_confirmed'

@pytest.mark.parametrize('key,value',[('faultindicator','On'),('dhwmode','On'),('boilertemperature',61),('outsidetemperature',None)])
async def test_telemetry_guards_stop_without_room_fallback(ready,key,value):
    c,r,k,clock=ready
    r.poll.return_value[key]['value']=value
    await c.async_control_heating_loop()
    assert all(call.args==(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)
    c._async_control_pid.assert_not_called()

async def test_reconnect_invalidates_evidence(ready):
    c,r,k,clock=ready
    r.connection_changed(False)
    assert r.reports.heads=={} and r.telemetry.fields=={}
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

async def test_invalid_report_clears_evidence(ready):
    c,r,k,clock=ready
    r.message(SimpleNamespace(payload='{}',retain=False))
    assert r.reports.heads=={}
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

async def test_freeze_setpoint_until_new_telemetry_confirms(ready):
    c,r,k,clock=ready
    await c.async_control_heating_loop()
    clock[0]=1230
    for field in r.poll.return_value.values():field['epoch']=3
    await c.async_control_heating_loop()
    k.async_set_control_setpoint.assert_awaited_with(41)
    assert r.pending[2]==1200
    clock[0]=1260
    for field in r.poll.return_value.values():field['epoch']=4
    r.poll.return_value['chenable']['value']='On'
    r.poll.return_value['controlsetpoint']['value']=41
    await c.async_control_heating_loop()
    assert r.c._setpoint<=43 and r.pending[2]==1260

async def test_timeout_still_attempts_both_stop_commands(ready,monkeypatch):
    c,r,k,clock=ready
    monkeypatch.setattr(runtime,'WRITE_TIMEOUT',0.01)
    async def blocked(value):
        if value!=10:await asyncio.Event().wait()
    k.async_set_control_setpoint.side_effect=blocked
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_with(10)

async def test_shutdown_cannot_restart(ready):
    c,r,k,clock=ready
    await r.shutdown()
    assert r.closed and c.hvac_mode==HVACMode.HEAT
    await c.async_control_heating_loop()
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)

async def test_mqtt_publish_errors_propagate_only_in_new_mode(hass,domains,data,options,config):
    from custom_components.sat.compat.mqtt import SatMqttCoordinator
    from unittest.mock import patch
    obj=SimpleNamespace(hass=hass,_simulation=False,_hodko_strict_publish=True,
                        _get_topic_for_publishing=lambda:'test/commands')
    with patch('custom_components.sat.compat.mqtt.mqtt.async_publish',AsyncMock(side_effect=RuntimeError('fake broker failure'))):
        with pytest.raises(RuntimeError):await SatMqttCoordinator._publish_command(obj,'CH=0',wait_time=0)
        obj._hodko_strict_publish=False
        await SatMqttCoordinator._publish_command(obj,'CH=0',wait_time=0)

@pytest.mark.parametrize('change',[{'schema_version':2},{'topic':'bad/#'},{'monitor_url':'http://test.invalid/command'},{'members':{}},{'policy':{'maximum_water':51,'water_cold':45}}])
async def test_invalid_mode_configuration_refused(ready,change):
    c,r,k,clock=ready
    with pytest.raises(ValueError):runtime.Runtime(c,{**SETTINGS,**change})

async def test_changed_sat_cap_cannot_send_above_limit(ready):
    from unittest.mock import patch,PropertyMock
    c,r,k,clock=ready
    with patch.object(type(k),'maximum_setpoint',new_callable=PropertyMock,return_value=40):
        await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_with(10)

async def test_clock_jump_invalidates_once_then_can_collect_again(ready,monkeypatch):
    c,r,k,clock=ready
    monkeypatch.setattr(runtime,'time',lambda:clock[0]+800)
    await c.async_control_heating_loop()
    segment=r.reports.segment
    assert not r.reports.heads
    clock[0]+=30
    await c.async_control_heating_loop()
    assert r.reports.segment==segment


async def test_http_failure_requires_new_otgw_proof_not_new_mqtt(ready):
    from copy import deepcopy
    c,r,k,clock=ready
    await c.async_control_heating_loop()
    before=deepcopy(vars(r.reports))
    evidence=deepcopy(r.telemetry.fields)
    clock[0]=1210;r.poll.side_effect=TimeoutError()
    await c.async_control_heating_loop()
    assert r.telemetry.fields==evidence and r.decision is None
    assert vars(r.reports)==before
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_with(10)
    off_deadline=r.pending[2]
    r.poll.side_effect=None
    for now in (1220,1230):
        clock[0]=now;await c.async_control_heating_loop()
        assert r.pending[2]==off_deadline
        k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    clock[0]=1240
    for field in r.poll.return_value.values():field['epoch']=3
    await c.async_control_heating_loop()
    assert r.tripped is None
    assert vars(r.reports)==before
    # Minimum OFF time is preserved; then fresh OTGW can resume with original PI/HR.
    for now in range(1270,1451,30):
        clock[0]=now
        for field in r.poll.return_value.values():field['epoch']+=1
        await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert vars(r.reports)==before


async def test_http_outage_does_not_extend_off_confirmation_deadline(ready):
    c,r,k,clock=ready
    r.poll.side_effect=TimeoutError()
    await c.async_control_heating_loop()
    for now in (1230,1260,1291):
        clock[0]=now;await c.async_control_heating_loop()
    assert r.pending[2]==1200
    r.poll.side_effect=None
    clock[0]=1300;await c.async_control_heating_loop()  # New baseline only.
    clock[0]=1330
    for field in r.poll.return_value.values():field['epoch']+=1
    r.poll.return_value['chenable']['value']='On'
    r.poll.return_value['controlsetpoint']['value']=41
    await c.async_control_heating_loop()
    assert r.tripped=='command_not_confirmed'
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)


async def test_active_fractional_clock_permissions_are_not_future(ready,hass):
    from custom_components.sat.ally.control import stamp
    c,r,k,clock=ready
    wall=1792426764.7852535
    mono=1200
    # Isolate serialization from the separate wall-clock discontinuity guard.
    r.reports.epoch=wall-10
    for field in r.reports.heads['a'].values():field['observed_at']=ts(wall-2)
    for key,(epoch,value,receipt) in r.telemetry.fields.items():
        r.telemetry.fields[key]=(epoch,value,mono)
    frame=r.frame(wall,mono)
    assert stamp(frame['heads']['a']['eligible']['observed_at'])<=wall
    assert stamp(frame['outside']['observed_at'])<=wall
    assert r.engine.evaluate(frame,wall).heat is True
    hass.states.async_set('binary_sensor.a_eligible','off')
    assert r.engine.evaluate(r.frame(wall,mono),wall).heat is False
    hass.states.async_set('binary_sensor.a_eligible','on')
    r.reports.heads['a']['heat_required']['observed_at']=ts(wall+1)
    assert r.engine.evaluate(r.frame(wall,mono),wall).heat is None


async def test_takeover_inherited_zero_requires_fresh_rm_ack(ready):
    c,r,k,clock=ready
    r.modulation_confirmed=False
    r.poll.return_value['maxrelmodlvl']['value']=0
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    k.async_set_control_max_relative_modulation.assert_awaited_with(100)
    assert r.modulation_since==1200
    clock[0]=1230
    for f in r.poll.return_value.values(): f['epoch']=3
    r.poll.return_value['maxrelmodlvl']['value']=100
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert r.modulation_confirmed


async def test_modulation_timeout_does_not_retry_forever(ready):
    c,r,k,clock=ready
    r.modulation_confirmed=False
    r.poll.return_value['maxrelmodlvl']['value']=0
    await c.async_control_heating_loop()
    for i in range(1,4):
        clock[0]=1200+i*30
        for f in r.poll.return_value.values(): f['epoch']=2+i
        await c.async_control_heating_loop()
    assert r.tripped=='modulation_not_confirmed'
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)


async def test_dhw_complete_cycle_recovers_without_room_target_change(ready):
    c,r,k,clock=ready
    await c.async_control_heating_loop()
    async def tick(at, **values):
        clock[0]=at
        for f in r.poll.return_value.values(): f['epoch']+=1
        for key,value in values.items(): r.poll.return_value[key]['value']=value
        await c.async_control_heating_loop()
    await tick(1230, chenable='On',controlsetpoint=41,dhwmode='On',boilertemperature=80)
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    await tick(1260, chenable='Off',controlsetpoint=10)
    await tick(1290, dhwmode='Off',boilertemperature=75)
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    await tick(1320, boilertemperature=40)
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    await tick(1380)
    await tick(1410)
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    k.async_set_control_setpoint.assert_awaited_with(41)
    assert r.tripped is None
    # TUV is not owned by this runtime; no SW/HW calls or RM0 on stop.
    k.async_set_control_max_relative_modulation.assert_not_awaited()

async def test_cached_rm_value_cannot_ack_takeover(ready):
    c,r,k,clock=ready
    r.modulation_confirmed=False
    await c.async_control_heating_loop()
    clock[0]=1230
    for name,f in r.poll.return_value.items():
        if name!='maxrelmodlvl': f['epoch']=3
    await c.async_control_heating_loop()
    assert not r.modulation_confirmed
    assert r.modulation_since==1200
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)

async def test_rm_publish_failure_keeps_ch_off(ready):
    c,r,k,clock=ready
    r.modulation_confirmed=False
    k.async_set_control_max_relative_modulation.side_effect=RuntimeError('publish failed')
    await c.async_control_heating_loop()
    assert not r.modulation_confirmed
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)
    assert r.modulation_since==1200

async def test_dhw_end_with_expired_demand_does_not_restart(ready):
    c,r,k,clock=ready
    r.poll.return_value['dhwmode']['value']='On'
    await c.async_control_heating_loop()
    clock[0]=1230
    for f in r.poll.return_value.values(): f['epoch']=3
    r.poll.return_value['dhwmode']['value']='Off'
    r.reports.heads['a']['heat_required']['observed_at']=ts(1)
    await c.async_control_heating_loop()
    assert r.status=='unknown'
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)

@pytest.mark.parametrize('startup_failed',[False,True])
async def test_mode_flow_return_requires_two_fresh_off_reports(ready,startup_failed):
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    c,r,k,clock=ready
    r.startup_ready = not startup_failed
    original=c._config_entry
    entry=SimpleNamespace(entry_id=original.entry_id, data={**original.data,'mode':'mqtt_opentherm'}, options={**original.options,
        'hodko_room_backup':{'options':{'maximum_setpoint':50},'target':23}})
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    c.hass.data['sat'][entry.entry_id]['climate']=c
    result=await flow.async_step_control_mode({'control_mode':'room_pid'})
    assert result['step_id']=='control_mode_confirm'
    flow._read_mode_monitor=AsyncMock(return_value={'chenable':{'epoch':1,'value':'Off'},'controlsetpoint':{'epoch':1,'value':10}})
    result=await flow.async_step_control_mode_confirm({})
    assert result['errors']['base']=='waiting_for_stop'
    result=await flow.async_step_control_mode_confirm({})
    assert result['errors']['base']=='waiting_for_stop'
    flow._read_mode_monitor.return_value={'chenable':{'epoch':2,'value':'Off'},'controlsetpoint':{'epoch':2,'value':10}}
    result=await flow.async_step_control_mode_confirm({})
    assert result['type']=='create_entry'
    assert result['data']['hodko_return_target']==23
    assert result['data']['hodko_control_mode']=='room_pid'
    with pytest.raises(ValueError): await c.async_set_hvac_mode(HVACMode.HEAT)

async def test_mode_flow_invalid_settings_never_stops(ready):
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    c,r,k,clock=ready
    original=c._config_entry
    entry=SimpleNamespace(entry_id=original.entry_id,data={**original.data,'mode':'mqtt_opentherm'},options={})
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    c.hass.data['sat'][entry.entry_id]['climate']=c
    result=await flow.async_step_control_mode({'control_mode':'ally_demand','settings':'{}'})
    assert result['errors']['base']=='invalid_control_mode'
    k.async_set_heater_state.assert_not_awaited()


async def test_mode_flow_forward_backups_and_failed_stop_do_not_save(ready):
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    import json
    c,r,k,clock=ready
    original=c._config_entry
    entry=SimpleNamespace(entry_id=original.entry_id,data={**original.data,'mode':'mqtt_opentherm'},options={'maximum_setpoint':50})
    c._target_temperature=23
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    c.hass.data['sat'][entry.entry_id]['climate']=c
    result=await flow.async_step_control_mode({'control_mode':'ally_demand','settings':json.dumps(SETTINGS)})
    assert result['step_id']=='control_mode_confirm'
    assert flow._mode_target['hodko_room_backup']=={'options':{'maximum_setpoint':50},'target':23}
    assert c.hvac_mode==HVACMode.HEAT  # Selecting alone does not stop.
    k.async_set_heater_state.side_effect=RuntimeError('failed stop')
    result=await flow.async_step_control_mode_confirm({})
    assert result['errors']['base']=='mode_transition_failed'
    assert entry.options=={'maximum_setpoint':50}

async def test_reload_failure_does_not_start_second_owner(ready,hass,monkeypatch):
    import custom_components.sat.compat as integration
    unload=AsyncMock(return_value=False);setup=AsyncMock()
    with monkeypatch.context() as scoped:
        scoped.setattr(integration,'async_unload_entry',unload)
        scoped.setattr(integration,'async_setup_entry',setup)
        with pytest.raises(RuntimeError): await integration.async_reload_entry(hass,Mock())
        setup.assert_not_awaited()

async def test_return_ignores_previous_heat_and_restores_target_once(ready):
    from homeassistant.core import State
    c,r,k,clock=ready
    c._config_entry=SimpleNamespace(options={'hodko_return_id':'test','hodko_return_target':23})
    c.async_get_last_state=AsyncMock(return_value=State('climate.test','heat',{'temperature':7}))
    c.async_write_ha_state=Mock()
    await c._restore_previous_state_or_set_defaults()
    assert c.hvac_mode==HVACMode.OFF
    assert c.target_temperature==23
    c.async_get_last_state=AsyncMock(return_value=State('climate.test','heat',{'temperature':24,'hodko_return_id':'test'}))
    c._target_temperature=None
    await c._restore_previous_state_or_set_defaults()
    assert c.hvac_mode==HVACMode.HEAT
    assert c.target_temperature==24

async def test_failed_start_keeps_off_entity_and_blocks_heat(ready,monkeypatch):
    c,r,k,clock=ready
    with monkeypatch.context() as scoped:
        scoped.setattr(c,'_async_shutdown',AsyncMock(side_effect=TimeoutError()))
        scoped.setattr(k,'async_added_to_hass',AsyncMock())
        await r.start()
    assert c.hvac_mode==HVACMode.OFF
    assert r.tripped is None
    assert r.startup_recovery_deadline is not None
    assert not r.startup_ready
    with pytest.raises(ValueError): await c.async_set_hvac_mode(HVACMode.HEAT)
    k.async_set_heater_state.reset_mock()
    await c.async_control_heating_loop()
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)
    assert not r.gate()
    # Explicit OFF still works for the rollback wizard despite startup failure.
    await c.async_set_hvac_mode(HVACMode.OFF, cascade=False)
    assert c.hvac_mode==HVACMode.OFF
    assert not r.startup_ready

async def test_close_blocks_delayed_room_and_ally_cycles(ready):
    c,r,k,clock=ready
    unsub=Mock();c._control_heating_loop_unsub=unsub
    await c.async_close_control()
    unsub.assert_called_once()
    k.async_set_heater_state.reset_mock();k.async_set_control_setpoint.reset_mock()
    await r.cycle()
    c._ally_runtime=None  # A queued room-loop callback must also be inert.
    await c.async_control_heating_loop()
    type(c).schedule_control_heating_loop(c, force=True)
    assert c._control_heating_loop_unsub is None
    k.async_set_heater_state.assert_not_awaited()
    k.async_set_control_setpoint.assert_not_awaited()
    with pytest.raises(ValueError):await c.async_set_hvac_mode(HVACMode.HEAT)

async def test_real_mqtt_cs_pm_sequence_fits_shutdown_budget(ready):
    from custom_components.sat.compat.mqtt.opentherm import SatOpenThermMqttCoordinator
    c,r,k,clock=ready
    mqtt_coordinator=object.__new__(SatOpenThermMqttCoordinator)
    async def publish(payload):
        await asyncio.sleep(3)  # 6 seconds for CS+PM: exceeds old 5s budget.
    mqtt_coordinator._publish_command=AsyncMock(side_effect=publish)
    k.async_set_control_setpoint=mqtt_coordinator.async_set_control_setpoint
    await c._async_shutdown()
    assert [x.args[0] for x in mqtt_coordinator._publish_command.await_args_list]==['CS=10','PM=25']

async def test_mqtt_unload_removes_subscriptions_even_if_save_fails(ready):
    from custom_components.sat.compat.mqtt import SatMqttCoordinator
    c,r,k,clock=ready
    # Exercise the actual implementation on a minimal coordinator-shaped object.
    unsub=Mock();obj=SimpleNamespace(_mqtt_unsubs=[unsub],_save_data=AsyncMock(side_effect=RuntimeError('store')))
    with pytest.raises(RuntimeError):await SatMqttCoordinator.async_will_remove_from_hass(obj)
    unsub.assert_called_once();assert obj._mqtt_unsubs==[]


async def test_close_drains_running_room_writer_before_final_off(ready):
    c,r,k,clock=ready
    c._ally_runtime=None
    entered,release=asyncio.Event(),asyncio.Event()
    async def running(_time=None):
        entered.set()
        await release.wait()
        await k.async_set_heater_state(DeviceState.ON)
    c._async_control_heating_loop=running
    task=asyncio.create_task(c.async_control_heating_loop())
    await entered.wait()
    close=asyncio.create_task(c.async_close_control())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await task; await close
    assert k.async_set_heater_state.await_args_list[-1].args==(DeviceState.OFF,)
    count=k.async_set_heater_state.await_count
    await c.async_control_heating_loop()
    assert k.async_set_heater_state.await_count==count

async def test_residual_dhw_heat_restarts_ch_at_77_5_without_raising_target(ready):
    c,r,k,clock=ready
    async def tick(at, **values):
        clock[0]=at
        for f in r.poll.return_value.values(): f['epoch']+=1
        for key,value in values.items(): r.poll.return_value[key]['value']=value
        await c.async_control_heating_loop()
    await tick(1200, dhwmode='On', boilertemperature=80.2)
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    await tick(1230, dhwmode='Off', boilertemperature=77.5)
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert r.attributes['ally_dhw_recovery'] is True
    assert r.c._setpoint == 41
    for at,water in [(1260,77),(1290,76),(1320,75),(1350,74),(1380,73),(1410,72)]:
        await tick(at, chenable='On',controlsetpoint=r.c._setpoint,boilertemperature=water)
        k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert all(call.args[0] <= 50 for call in k.async_set_control_setpoint.await_args_list)
    await tick(1440, controlsetpoint=r.c._setpoint,boilertemperature=59)
    assert not r.dhw_recovery.active and not r.dhw_recovery.seen_dhw
    k.async_set_control_max_relative_modulation.assert_not_awaited()
    c._async_control_pid.assert_not_called()

@pytest.mark.parametrize('failure', ['fault', 'unknown_dhw', 'stale_water', 'stale_hr', 'permission', 'no_cooling', 'rise', 'reconnect'])
async def test_residual_dhw_cannot_bypass_other_guards(ready,hass,failure):
    c,r,k,clock=ready
    r.poll.return_value['dhwmode']['value']='On'
    r.poll.return_value['boilertemperature']['value']=80
    await c.async_control_heating_loop()
    clock[0]=1230
    for f in r.poll.return_value.values(): f['epoch']+=1
    r.poll.return_value['dhwmode']['value']='Off'
    r.poll.return_value['boilertemperature']['value']=77.5
    if failure=='fault': r.poll.return_value['faultindicator']['value']='On'
    if failure=='unknown_dhw': r.poll.return_value['dhwmode']['value']='unknown'
    if failure=='stale_water': r.poll.return_value['boilertemperature']['epoch']=0
    if failure=='stale_hr': r.reports.heads['a']['heat_required']['observed_at']=ts(1)
    if failure=='permission': hass.states.async_set('input_boolean.heat_allowed','off')
    if failure=='reconnect': r.connection_changed(False)
    await c.async_control_heating_loop()
    if failure in ('no_cooling','rise'):
        k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
        for at in range(1260,1440,30):
            clock[0]=at
            for f in r.poll.return_value.values(): f['epoch']+=1
            r.poll.return_value['chenable']['value']='On' if r.ramp.on else 'Off'
            r.poll.return_value['controlsetpoint']['value']=r.c._setpoint
            if failure=='rise': r.poll.return_value['boilertemperature']['value']=79.6
            await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_with(10)

@pytest.mark.parametrize('water',[60,77.5,79.9,80,90,float('nan'),-1])
async def test_hot_water_without_observed_dhw_never_starts(ready,water):
    c,r,k,clock=ready
    r.poll.return_value['boilertemperature']['value']=water
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

async def test_dhw_recovery_ceiling_deadline_and_no_rearming_without_new_dhw(ready):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError,match='DHW priority'): g.check('On',83,0,60)
    with pytest.raises(ValueError,match='ceiling'): g.check('Off',80,30,60)
    g.check('Off',77.5,60,60)
    g.heating_sent(77.5,60)
    g.check('Off',76,240,60)
    with pytest.raises(ValueError,match='expired'): g.check('Off',70,930,60)
    with pytest.raises(ValueError,match='experimental guard'): g.check('Off',70,960,60)

@pytest.mark.parametrize('values', [dict(dhw_ceiling=81),dict(cooling_time=0),dict(ramp_rate=3),dict(restart_delay=0),dict(water_margin=float('nan')),dict(dhw_window=200),dict(residual_rise=True)])
async def test_operating_limits_reject_unsafe_values(ready,values):
    with pytest.raises(ValueError):runtime.OperatingLimits(**values)

async def test_parameterized_recovery_boundaries(ready):
    limits=runtime.OperatingLimits(dhw_ceiling=75,dhw_window=600,cooling_time=60,mixing_time=30,cooling_drop=2,residual_rise=1)
    limits.validate_water(50)
    g=runtime.DHWRecovery(limits)
    with pytest.raises(ValueError,match='priority'):g.check('On',80,0,60)
    with pytest.raises(ValueError,match='ceiling'):g.check('Off',75,30,60)
    g.check('Off',74,60,60);g.heating_sent(74,60)
    with pytest.raises(ValueError,match='not cooling'):g.check('Off',73,120,60)
    with pytest.raises(ValueError):runtime.OperatingLimits(water_margin=20,dhw_ceiling=65).validate_water(50)

async def test_limits_runtime_defaults_and_custom_ramp(ready):
    c,r,k,clock=ready
    assert r.attributes['ally_operating_limits']['normal_water_guard']==60
    settings={**SETTINGS,'operating_limits':dict(restart_delay=240,ramp_rate=.5,water_margin=8)}
    candidate=runtime.Runtime(c,settings)
    assert candidate.ramp.delay==240 and candidate.ramp.rate==.5
    assert candidate.attributes['ally_operating_limits']['normal_water_guard']==58

async def test_limits_flow_validates_before_writes_and_preserves_backup(ready):
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    from dataclasses import asdict
    c,r,k,clock=ready
    original=c._config_entry
    entry=SimpleNamespace(entry_id=original.entry_id,data={**original.data,'mode':'mqtt_opentherm'},
        options={**original.options,'hodko_room_backup':{'options':{'maximum_setpoint':50},'target':23}})
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    result=await flow.async_step_operating_limits({**asdict(runtime.OperatingLimits()),'maximum_water':61})
    assert result['errors']['base']=='invalid_operating_limits'
    k.async_set_heater_state.assert_not_awaited()
    result=await flow.async_step_operating_limits({**asdict(runtime.OperatingLimits()),'maximum_water':50,'dhw_ceiling':78})
    assert result['step_id']=='control_mode_confirm'
    k.async_set_heater_state.assert_not_awaited()
    flow._read_mode_monitor=AsyncMock(return_value={'chenable':{'epoch':1,'value':'Off'},'controlsetpoint':{'epoch':1,'value':10}})
    result=await flow.async_step_control_mode_confirm({})
    assert result['errors']['base']=='waiting_for_stop'
    assert c.hvac_mode==HVACMode.OFF
    flow._read_mode_monitor.return_value={'chenable':{'epoch':2,'value':'Off'},'controlsetpoint':{'epoch':2,'value':10}}
    result=await flow.async_step_control_mode_confirm({})
    assert result['type']=='create_entry'
    assert result['data']['hodko_room_backup']==entry.options['hodko_room_backup']
    assert result['data']['hodko_ally']['operating_limits']['dhw_ceiling']==78
    assert result['data']['hodko_ally']['topic']==SETTINGS['topic']

@pytest.mark.parametrize('off_water',[0,10])
async def test_full_start_with_delayed_off_and_rm_reports(ready,off_water):
    """Real sequence, no pre-confirmed RM; publication consumes wall time."""
    c,r,k,clock=ready
    r.modulation_confirmed=False
    r.pending=None
    r.ramp.off_at=1200
    async def publish(*args):clock[0]+=1
    k.async_set_heater_state.side_effect=publish
    k.async_set_control_setpoint.side_effect=publish
    k.async_set_control_max_relative_modulation.side_effect=publish
    await r.stop('initial_off')
    r.poll.return_value['boilertemperature']['value']=55.2
    for i in range(1,10):
        clock[0]=1200+i*30
        for field in r.poll.return_value.values():field['epoch']=i+2
        r.poll.return_value['controlsetpoint']['value']=off_water
        await c.async_control_heating_loop()
        if r.ramp.on:break
    assert r.tripped is None
    assert r.modulation_confirmed
    assert r.ramp.on
    assert r.c._setpoint==41
    assert clock[0]>=1380
    assert k.async_set_heater_state.await_args.args==(DeviceState.ON,)
    assert r.pending[0] is True

async def test_pending_deadline_survives_stale_telemetry(ready):
    c,r,k,clock=ready
    r.sent(False,10)
    r.telemetry.fields.clear()
    clock[0]=1291
    with pytest.raises(ValueError):r.confirm(clock[0])
    assert r.tripped=='command_not_confirmed'

@pytest.mark.parametrize('water',[-1,11,41])
async def test_off_confirmation_rejects_unsafe_water_target(ready,water):
    c,r,k,clock=ready
    r.sent(False,10)
    r.telemetry.fields['chenable']=(3,'Off',1201)
    r.telemetry.fields['controlsetpoint']=(3,water,1201)
    r.confirm(1202)
    assert r.pending is not None

async def test_repeated_off_is_bounded_and_cannot_postpone_deadline(ready):
    c,r,k,clock=ready
    await r.stop('test')
    first=r.pending[2]
    clock[0]=1210
    await r.stop('waiting')
    assert k.async_set_heater_state.await_count==1
    clock[0]=1230
    await r.stop('waiting')
    assert k.async_set_heater_state.await_count==2
    assert r.pending[2]==first
    r.telemetry.fields.clear()
    with pytest.raises(ValueError):r.confirm(1290)
    assert r.tripped=='command_not_confirmed'

async def test_recent_off_never_suppresses_stop_of_pending_on(ready):
    c,r,k,clock=ready
    await r.stop('test')
    r.pending=None
    r.sent(True,41)
    clock[0]=1201
    await r.stop('permission_lost')
    assert k.async_set_heater_state.await_count==2
    assert r.pending[0] is False

async def test_cached_zero_does_not_acknowledge_off(ready):
    c,r,k,clock=ready
    r.sent(False,10)
    r.telemetry.fields['chenable']=(3,'Off',1199)
    r.telemetry.fields['controlsetpoint']=(3,0,1199)
    r.confirm(1201)
    assert r.pending is not None

async def test_zero_target_cannot_acknowledge_on(ready):
    c,r,k,clock=ready
    r.sent(True,41)
    r.telemetry.fields['chenable']=(3,'On',1201)
    r.telemetry.fields['controlsetpoint']=(3,0,1201)
    r.confirm(1202)
    assert r.pending is not None

@pytest.mark.parametrize('off_water',[0,10])
@pytest.mark.parametrize('report_before_heat',[False,True])
async def test_long_off_to_heat_runs_real_transition_and_confirms_on(ready,off_water,report_before_heat):
    c,r,k,clock=ready
    await c.async_set_hvac_mode(HVACMode.OFF)
    # No fixture resets of pending/telemetry/RM after OFF: emulate ten minutes idle.
    clock[0]=1800
    await c.async_control_heating_loop()
    if report_before_heat:
        r.reports.accept(event(seq=2,at=1800,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=95,valid=True)),1800)
        evidence=repr(r.reports.heads)
        evidence_epoch=r.reports.epoch
    clock[0]=1801
    await c.async_set_hvac_mode(HVACMode.HEAT)
    assert r.pending and not r.pending[0] and r.pending[2]>=1801
    assert not r.modulation_confirmed
    assert r.telemetry.fields=={}
    if report_before_heat:
        assert repr(r.reports.heads)==evidence
        assert r.reports.epoch==evidence_epoch
    else:
        r.reports.accept(event(seq=2,at=1802,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=95,valid=True)),1802)
    first_on = None
    on_confirmed = False
    for i in range(1,12):
        clock[0]=1801+i*15
        for f in r.poll.return_value.values():f['epoch']=100+i
        r.poll.return_value['chenable']['value']='On' if r.ramp.on else 'Off'
        r.poll.return_value['controlsetpoint']['value']=r.c._setpoint if r.ramp.on else off_water
        r.poll.return_value['boilertemperature']['value']=50
        await c.async_control_heating_loop()
        assert r.tripped is None
        if r.ramp.on:
            if first_on is None:
                first_on = r.pending[2]
            elif r.pending is None or r.pending[2] > first_on:
                on_confirmed = True
                break
    assert on_confirmed, 'Fresh CH ON and the requested CS must be acknowledged'
    assert r.ramp.on and r.modulation_confirmed
    assert 41<=r.c._setpoint<=50
    assert r.tripped is None
    assert k.async_set_heater_state.await_args.args==(DeviceState.ON,)

async def test_repeated_heat_does_not_reset_pending_or_reports(ready):
    c,r,k,clock=ready
    r.sent(True,41)
    before=r.pending
    heads=repr(r.reports.heads)
    clock[0]+=5
    await c.async_set_hvac_mode(HVACMode.HEAT)
    assert r.pending==before
    assert repr(r.reports.heads)==heads
    k.async_set_heater_state.assert_not_awaited()

async def test_heat_start_failed_off_stays_off_and_blocks_retry(ready):
    c,r,k,clock=ready
    await c.async_set_hvac_mode(HVACMode.OFF)
    k.async_set_heater_state.side_effect=TimeoutError('publish failed')
    with pytest.raises(TimeoutError):
        await c.async_set_hvac_mode(HVACMode.HEAT)
    assert c.hvac_mode==HVACMode.OFF
    assert r.tripped=='heat_start_stop_failed'
    assert k.async_set_control_setpoint.await_args.args==(10,)
    with pytest.raises(ValueError):
        await c.async_set_hvac_mode(HVACMode.HEAT)
    assert c.hvac_mode==HVACMode.OFF

async def test_heat_start_missing_new_epochs_times_out_without_on(ready):
    c,r,k,clock=ready
    await c.async_set_hvac_mode(HVACMode.OFF)
    clock[0]=1800
    await c.async_set_hvac_mode(HVACMode.HEAT)
    for at in (1815,1830,1845,1860,1875,1891):
        clock[0]=at
        await c.async_control_heating_loop()
    assert r.tripped=='command_not_confirmed'
    assert not r.ramp.on
    assert all(call.args==(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)

@pytest.mark.parametrize('case',['expired','invalid'])
async def test_preserved_reports_cannot_bypass_expiry_or_invalidity(ready,case):
    c,r,k,clock=ready
    await c.async_set_hvac_mode(HVACMode.OFF)
    r.reports.accept(event(seq=2,at=1201,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=95,valid=True)),1201)
    if case=='invalid':
        r.reports.accept(event(seq=3,at=1202,heat_required=dict(value=None,valid=False)),1202)
    clock[0]=6000 if case=='expired' else 1800
    start=clock[0]
    evidence=repr(r.reports.heads)
    await c.async_set_hvac_mode(HVACMode.HEAT)
    assert repr(r.reports.heads)==evidence
    for i in range(1,9):
        clock[0]=start+i*15
        for f in r.poll.return_value.values(): f['epoch']=100+i
        await c.async_control_heating_loop()
    assert not r.ramp.on
    assert all(call.args==(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)

@pytest.mark.parametrize('saved_mode,key_matches,fault,expected', [
    ('heat',True,None,'heat'), ('off',True,None,'off'),
    ('heat',False,None,'off'), ('heat',True,'transport_fault','off'),
    ('unavailable',True,None,'off'),
])
async def test_restart_restores_only_matching_operator_intent(ready, monkeypatch, saved_mode,key_matches,fault,expected):
    from homeassistant.core import State
    c,old,k,clock=ready
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    attrs={'control_mode':'ally_demand','ally_restart_key':r.restore_key if key_matches else 'old', 'ally_fault':fault}
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,saved_mode,attrs)))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    await r.start()
    assert c.hvac_mode==expected
    assert r.restart_restored==(expected=='heat')
    assert not r.reports.heads
    assert not r.telemetry.fields
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    # Mode alone is insufficient: with missing live evidence no ON command.
    k.async_set_heater_state.reset_mock()
    r.poll=AsyncMock(return_value={})
    await c.async_control_heating_loop()
    assert all(call.args[0]!=DeviceState.ON for call in k.async_set_heater_state.await_args_list)

async def test_lifecycle_shutdown_preserves_heat_but_explicit_off_does_not(ready):
    c,r,k,clock=ready
    assert c.hvac_mode==HVACMode.HEAT
    await r.shutdown()
    assert c.hvac_mode==HVACMode.HEAT
    assert r.closed
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    await c.async_set_hvac_mode(HVACMode.OFF)
    assert c.hvac_mode==HVACMode.OFF

async def test_restart_transport_failure_does_not_restore_heat(ready,monkeypatch):
    from homeassistant.core import State
    c,old,k,clock=ready
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,'heat',{'control_mode':'ally_demand','ally_restart_key':r.restore_key})))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock(side_effect=TimeoutError()))
    await r.start()
    assert not r.startup_ready
    assert c.hvac_mode==HVACMode.OFF
    assert not r.restart_restored

async def interrupted_start(ready, monkeypatch, saved='heat', matches=True):
    from homeassistant.core import State
    c,old,k,clock=ready
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,saved,{
        'control_mode':'ally_demand','ally_restart_key':r.restore_key if matches else 'different'})))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    with monkeypatch.context() as scoped:
        scoped.setattr(c,'_async_shutdown',AsyncMock(side_effect=TimeoutError()))
        await r.start()
    r.poll=AsyncMock(return_value={key:dict(value=value,epoch=10) for key,value in
        dict(chenable='Off',controlsetpoint=10,faultindicator='Off',
             outsidetemperature=5,boilertemperature=40,dhwmode='Off',maxrelmodlvl=100).items()})
    r.telemetry.observe(r.poll.return_value,clock[0])
    for f in r.poll.return_value.values():f['epoch']+=1
    k.async_set_heater_state.reset_mock()
    return c,r,k,clock

@pytest.mark.parametrize('saved,matches,cancel,expected',[
    ('heat',True,False,'heat'),('off',True,False,'off'),
    ('heat',False,False,'off'),('heat',True,True,'off')])
async def test_startup_retry_restores_only_authorized_intent_after_new_ack(ready,monkeypatch,saved,matches,cancel,expected):
    c,r,k,clock=await interrupted_start(ready,monkeypatch,saved,matches)
    if cancel:
        await c.async_set_hvac_mode(HVACMode.OFF)
        # OFF at1200 can already be confirmed by a new epoch after1200.
        clock[0]+=30
        await c.async_control_heating_loop()
        assert r.startup_ready and not r.restart_restored
        assert c.hvac_mode==HVACMode.OFF
        assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)
        return
    clock[0]+=30
    await c.async_control_heating_loop()
    assert not r.startup_ready
    # Unchanged telemetry must not acknowledge the safe-state transaction.
    clock[0]+=30
    await c.async_control_heating_loop()
    assert not r.startup_ready
    for f in r.poll.return_value.values():f['epoch']+=1
    clock[0]+=30
    await c.async_control_heating_loop()
    assert r.startup_ready and r.tripped is None
    assert c.hvac_mode==expected
    assert r.restart_restored==(expected=='heat')
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)
    # No recovered/cached PI or HR was manufactured.
    assert not r.reports.heads

async def test_startup_retries_are_bounded_and_never_enable_ch(ready,monkeypatch):
    c,r,k,clock=await interrupted_start(ready,monkeypatch)
    k.async_set_heater_state.side_effect=TimeoutError()
    deadline=r.startup_recovery_deadline
    for t in [1201,1229,1230,1231,1259,1260]:
        clock[0]=t
        await c.async_control_heating_loop()
    assert k.async_set_heater_state.await_count==2
    assert r.startup_recovery_deadline==deadline
    clock[0]=deadline
    await c.async_control_heating_loop()
    assert r.tripped=='startup_recovery_expired'
    assert not r.startup_ready and c.hvac_mode==HVACMode.OFF

async def test_startup_new_off_ack_deadline_is_not_extended(ready,monkeypatch):
    c,r,k,clock=await interrupted_start(ready,monkeypatch)
    for t in [1230,1260,1290,1320]:
        clock[0]=t
        await c.async_control_heating_loop()
    assert r.tripped=='command_not_confirmed'
    assert r.startup_recovery_deadline is None
    assert not r.startup_ready

async def test_startup_fault_and_unexpected_exception_do_not_auto_resume(ready,monkeypatch):
    c,r,k,clock=await interrupted_start(ready,monkeypatch)
    r.poll.return_value['faultindicator']['value']='On'
    clock[0]+=30
    await c.async_control_heating_loop()
    assert r.tripped=='startup_boiler_fault'
    assert r.startup_recovery_deadline is None
    k.async_set_heater_state.assert_not_awaited()

async def test_unexpected_initial_stop_error_remains_latched(ready,monkeypatch):
    c,r,k,clock=ready
    monkeypatch.setattr(c,'_async_shutdown',AsyncMock(side_effect=ValueError('bad setup')))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    await r.start()
    assert r.tripped=='startup_stop_failed'
    assert r.startup_recovery_deadline is None

async def test_startup_recovery_preserves_new_report_timestamps(ready,monkeypatch):
    c,r,k,clock=await interrupted_start(ready,monkeypatch)
    r.reports.accept(event(seq=5,at=1210,heat_required=dict(value=True,valid=True),
                           pi_heating_demand=dict(value=95,valid=True)),1210)
    evidence=repr(r.reports.heads)
    clock[0]=1230
    await c.async_control_heating_loop()
    for f in r.poll.return_value.values():f['epoch']+=1
    clock[0]=1260
    await c.async_control_heating_loop()
    assert r.startup_ready and c.hvac_mode==HVACMode.HEAT
    assert repr(r.reports.heads)==evidence

async def test_recovered_start_completes_heat_and_ack_with_live_demand(ready,monkeypatch):
    c,r,k,clock=await interrupted_start(ready,monkeypatch)
    r.reports.accept(event(seq=5,at=1210,heat_required=dict(value=True,valid=True),
                           pi_heating_demand=dict(value=95,valid=True)),1210)
    first_on=None
    acknowledged=False
    for i in range(1,23):
        clock[0]=1200+i*30
        for f in r.poll.return_value.values():f['epoch']=10+i
        r.poll.return_value['chenable']['value']='On' if r.ramp.on else 'Off'
        r.poll.return_value['controlsetpoint']['value']=c._setpoint if r.ramp.on else 10
        await c.async_control_heating_loop()
        assert r.tripped is None
        if r.ramp.on:
            assert r.startup_ready and r.modulation_confirmed
            assert 41<=c._setpoint<=50
            if first_on is None:first_on=r.pending[2]
            elif r.pending is None or r.pending[2]>first_on:
                acknowledged=True
                break
    assert acknowledged
    assert r.restart_restored and c.hvac_mode==HVACMode.HEAT

async def test_mqtt_telemetry_binding_must_match_command_gateway(ready):
    c,r,k,clock=ready
    k._get_topic_for_subscription=lambda leaf:'OTGW/value/right/'+leaf
    settings={**SETTINGS,'telemetry_transport':'mqtt','otgw_mqtt_topic':'OTGW/value/wrong'}
    with pytest.raises(ValueError,match='command gateway'):runtime.Runtime(c,settings)
    settings['otgw_mqtt_topic']='OTGW/value/right'
    candidate=runtime.Runtime(c,settings)
    assert candidate.attributes['ally_telemetry_transport']=='mqtt'
    assert candidate.attributes['ally_telemetry_time_basis']=='mqtt_receipt'

async def test_mqtt_mode_return_rejects_pre_stop_cached_state(ready,monkeypatch):
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    from custom_components.sat.ally import mode_flow,mqtt_monitor
    c,r,k,clock=ready
    monkeypatch.setattr(mode_flow,'monotonic',lambda:clock[0])
    original=c._config_entry
    entry=SimpleNamespace(entry_id=original.entry_id,data={**original.data,'mode':'mqtt_opentherm'},
        options={**original.options,'hodko_room_backup':{'options':{'maximum_setpoint':50},'target':23}})
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    c.hass.data['sat'][entry.entry_id]['climate']=c
    await flow.async_step_control_mode({'control_mode':'room_pid'})
    flow._read_mode_monitor=AsyncMock(return_value=mqtt_monitor.MqttSnapshot({
        'chenable':(1,'Off',1199),'controlsetpoint':(2,10,1199)}))
    result=await flow.async_step_control_mode_confirm({})
    assert result['errors']['base']=='waiting_for_stop'
    clock[0]=1230
    flow._read_mode_monitor.return_value=mqtt_monitor.MqttSnapshot({
        'chenable':(3,'Off',1220),'controlsetpoint':(4,0,1221)})
    result=await flow.async_step_control_mode_confirm({})
    assert result['type']=='create_entry' and result['data']['hodko_control_mode']=='room_pid'

async def test_mqtt_snapshot_drives_real_loop_then_stale_data_stops(ready):
    from custom_components.sat.ally.mqtt_monitor import MqttSnapshot
    c,r,k,clock=ready
    values=dict(outsidetemperature=5,chenable='Off',controlsetpoint=10,
                boilertemperature=40,faultindicator='Off',dhwmode='Off',maxrelmodlvl=100)
    r.poll.return_value=MqttSnapshot({key:(i+1,value,1199) for i,(key,value) in enumerate(values.items())})
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert c._setpoint <= 50
    clock[0]=1290
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

async def test_same_pihr_mode_changes_transport_without_losing_backup(ready,monkeypatch):
    import json
    from copy import deepcopy
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    from custom_components.sat.ally import mode_flow,mqtt_monitor
    c,r,k,clock=ready
    monkeypatch.setattr(mode_flow,'monotonic',lambda:clock[0])
    k._get_topic_for_subscription=lambda leaf:'OTGW/value/right/'+leaf
    original=c._config_entry
    backup={'options':{'maximum_setpoint':45},'target':23}
    entry=SimpleNamespace(entry_id=original.entry_id,data={**original.data,'mode':'mqtt_opentherm'},
        options={**original.options,'hodko_room_backup':backup,'custom_marker':'preserved'})
    c.hass.data['sat'][entry.entry_id]['climate']=c
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    settings={**deepcopy(SETTINGS),'telemetry_transport':'mqtt','otgw_mqtt_topic':'OTGW/value/right'}
    result=await flow.async_step_control_mode({'control_mode':'ally_demand','settings':json.dumps(settings)})
    assert result['step_id']=='control_mode_confirm'
    k.async_set_heater_state.assert_not_awaited()
    flow._read_mode_monitor=AsyncMock(return_value=mqtt_monitor.MqttSnapshot({
        'chenable':(1,'Off',1199),'controlsetpoint':(2,10,1199)}))
    result=await flow.async_step_control_mode_confirm({})
    assert result['errors']['base']=='waiting_for_stop' and c.hvac_mode==HVACMode.OFF
    clock[0]=1230
    flow._read_mode_monitor.return_value=mqtt_monitor.MqttSnapshot({
        'chenable':(3,'Off',1220),'controlsetpoint':(4,10,1221)})
    result=await flow.async_step_control_mode_confirm({})
    assert result['type']=='create_entry'
    assert result['data']=={**entry.options,'hodko_ally':settings}
    assert result['data']['hodko_room_backup']==backup

async def test_same_pihr_identical_settings_do_not_stop(ready):
    import json
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    c,r,k,clock=ready
    original=c._config_entry
    entry=SimpleNamespace(entry_id=original.entry_id,data={**original.data,'mode':'mqtt_opentherm'},options=original.options)
    c.hass.data['sat'][entry.entry_id]['climate']=c
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    result=await flow.async_step_control_mode({'control_mode':'ally_demand','settings':json.dumps(SETTINGS)})
    assert result['type']=='abort' and result['reason']=='mode_unchanged'
    k.async_set_heater_state.assert_not_awaited()

@pytest.mark.parametrize('running,season,saved_fault,expected', [
    (False,'on',None,'heat'), (False,'off',None,'off'),
    (False,'unknown',None,'off'), (True,'on',None,'off'),
    (False,'on','command_not_confirmed','heat'),
])
async def test_season_boot_overrides_saved_off_only_on_full_ha_start(ready,monkeypatch,running,season,saved_fault,expected):
    from homeassistant.core import State
    c,old,k,clock=ready
    monkeypatch.setattr(type(c.hass),'is_running',property(lambda self:running))
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,'off',{'ally_fault':saved_fault})))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,season)
    await r.start()
    assert c.hvac_mode==HVACMode.OFF
    r.resume_for_season()  # still before HA started
    assert c.hvac_mode==HVACMode.OFF
    r.season_boot_ready(None)
    r.resume_for_season()
    assert c.hvac_mode==expected
    assert not r.reports.heads and not r.telemetry.fields
    k.async_set_heater_state.reset_mock()
    r.poll=AsyncMock(return_value={})
    await c.async_control_heating_loop()
    assert all(call.args[0]!=DeviceState.ON for call in k.async_set_heater_state.await_args_list)

@pytest.mark.parametrize('cancel', ['manual_off','expired','season_off','fault','shutdown'])
async def test_pending_season_start_cannot_override_cancellation(ready,cancel):
    c,r,k,clock=ready
    r.season_start_pending=True;r.season_start_deadline=clock[0]+600
    c._hvac_mode=HVACMode.OFF
    c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,'unknown')
    r.resume_for_season();assert r.season_start_pending
    if cancel=='manual_off':await c.async_set_hvac_mode(HVACMode.OFF)
    elif cancel=='expired':clock[0]+=601
    elif cancel=='season_off':
        c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,'off');r.resume_for_season()
    elif cancel=='fault':r.tripped='command_not_confirmed'
    else:r.closed=True
    c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,'on')
    r.resume_for_season()
    assert c.hvac_mode==HVACMode.OFF and not r.season_start_pending

async def test_season_start_waits_for_recovery_and_global_permission(ready):
    c,r,k,clock=ready
    c._hvac_mode=HVACMode.OFF
    r.season_start_pending=True;r.season_start_deadline=clock[0]+600
    c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,'on')
    r.startup_ready=False
    r.resume_for_season();assert c.hvac_mode==HVACMode.OFF
    r.startup_ready=True
    c.hass.states.async_set(SETTINGS['enabled_entity'],'unavailable')
    r.resume_for_season();assert c.hvac_mode==HVACMode.OFF
    c.hass.states.async_set(SETTINGS['enabled_entity'],'on')
    r.resume_for_season()
    assert c.hvac_mode==HVACMode.HEAT and not r.season_start_pending

async def test_scheduler_is_ha_event_loop_callback(ready):
    from homeassistant.core import HassJob, HassJobType
    c, r, k, clock = ready
    job = HassJob(type(c).schedule_control_heating_loop.__get__(c))
    assert job.job_type is HassJobType.Callback

async def test_scheduled_callback_releases_marker_before_waiting_for_writer(ready, monkeypatch):
    from custom_components.sat.compat import climate as cm
    c, r, k, clock = ready
    callbacks = []
    monkeypatch.setattr(cm, 'async_call_later', lambda hass, delay, job: callbacks.append(job) or (lambda: None))
    c._control_heating_loop_unsub = None
    schedule = type(c).schedule_control_heating_loop.__get__(c)
    schedule()
    await c._control_lock.acquire()
    c.hass.async_run_hass_job(callbacks.pop())
    await asyncio.sleep(0)
    try:
        assert c._control_heating_loop_unsub is None
    finally:
        c._control_lock.release()
        await c.hass.async_block_till_done()

async def test_forced_cycle_without_existing_timer_is_immediate(ready, monkeypatch):
    from custom_components.sat.compat import climate as cm
    c, r, k, clock = ready
    timer = Mock()
    monkeypatch.setattr(cm, 'async_call_later', timer)
    c._control_heating_loop_unsub = None
    type(c).schedule_control_heating_loop(c, force=True)
    await c.hass.async_block_till_done()
    timer.assert_not_called()
    assert r.status == 'heat'

async def test_scheduled_morning_demand_after_idle_and_dhw_off(ready, monkeypatch):
    """Morning observations: idle decision, fresh PI42/HRtrue, DHWoff, water55.2."""
    import json
    from custom_components.sat.compat import climate as cm
    c, r, k, clock = ready
    callbacks = []
    monkeypatch.setattr(cm, 'async_call_later', lambda hass, delay, job: callbacks.append(job) or (lambda: None))
    c.schedule_control_heating_loop = type(c).schedule_control_heating_loop.__get__(c)
    c._control_heating_loop_unsub = None
    r.reports.accept(event(seq=2, at=1200, heat_required=dict(value=False, valid=True)), 1200)
    async def run_scheduled():
        c.schedule_control_heating_loop()
        assert len(callbacks) == 1
        c.hass.async_run_hass_job(callbacks.pop())
        await c.hass.async_block_till_done()
    await run_scheduled()
    assert not r.decision.heat
    assert r.decision_sequence == 2
    clock[0] = 1230
    for value in r.poll.return_value.values(): value['epoch'] += 1
    r.poll.return_value['controlsetpoint']['value'] = 0
    r.poll.return_value['boilertemperature']['value'] = 55.2
    report = event(seq=3, at=1230, heat_required=dict(value=True,valid=True), pi_heating_demand=dict(value=42,valid=True))
    r.message(SimpleNamespace(payload=json.dumps(report), retain=False))
    await run_scheduled()
    assert r.decision.heat is True
    assert r.decision_sequence == 3
    assert r.last_decision_at == runtime.observation_utc(1230)
    assert r.reports.heads['a']['heat_required']['observed_at'] == ts(1230)
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert 10 < k.async_set_control_setpoint.await_args.args[0] <= 50
    # Only fresh post-command OTGW data can acknowledge the requested ON.
    clock[0] = 1260
    for value in r.poll.return_value.values(): value['epoch'] += 1
    r.poll.return_value['chenable']['value'] = 'On'
    r.poll.return_value['controlsetpoint']['value'] = r.pending[1]
    await run_scheduled()
    assert r.status == 'waiting_for_on_confirmation'
    # HTTP epochs conservatively carry the preceding poll time: require
    # one more advance instead of ACKing the pre-write timestamp equality.
    clock[0] = 1270
    for value in r.poll.return_value.values(): value['epoch'] += 1
    await run_scheduled()
    assert r.status == 'heat' and r.tripped is None

async def test_read_deadline_preserves_reports_and_releases_writer(ready, monkeypatch):
    c,r,k,clock = ready
    # Use a short test-only deadline, leaving the production timeout unchanged.
    real_timeout = asyncio.timeout
    monkeypatch.setattr(runtime.asyncio, 'timeout', lambda seconds: real_timeout(.01 if seconds == 10 else seconds))
    async def stuck(): await asyncio.Event().wait()
    r.poll.side_effect = stuck
    await c.async_control_heating_loop()
    assert 'TimeoutError' in r.status
    assert not c._control_lock.locked()
    assert r.reports.heads['a']['heat_required']['value'] is True
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    assert r.decision is None

async def test_boot_setup_does_not_consume_final_off_ack_window(ready, monkeypatch):
    from homeassistant.core import State
    c,old,k,clock=ready
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,'heat',{'control_mode':'ally_demand','ally_restart_key':r.restore_key})))
    async def setup(): clock[0]+=40
    monkeypatch.setattr(k,'async_added_to_hass',setup)
    await r.start()
    assert r.pending[2] == 1240
    data={key:dict(value=value,epoch=10) for key,value in dict(chenable='Off',controlsetpoint=10,faultindicator='Off',outsidetemperature=5,boilertemperature=49,dhwmode='Off',maxrelmodlvl=100).items()}
    r.poll=AsyncMock(return_value=data)
    # First read is only a baseline, even though it already says OFF/CS10.
    clock[0]=1290
    await c.async_control_heating_loop()
    assert r.tripped is None and r.pending is not None
    clock[0]=1310
    for v in data.values():v['epoch']+=1
    r.reports.accept(event(seq=2,at=1310,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=92,valid=True)),1310)
    await c.async_control_heating_loop()
    assert r.tripped is None
    # Follow fresh OFF/MM, minimum downtime, ON and actual echo to completion.
    acknowledged=False
    first_on=None
    for i in range(1,15):
        clock[0]=1310+i*20
        for v in data.values():v['epoch']+=1
        data['chenable']['value']='On' if r.ramp.on else 'Off'
        data['controlsetpoint']['value']=c._setpoint if r.ramp.on else 10
        await c.async_control_heating_loop()
        assert r.tripped is None
        if r.ramp.on:
            assert 10<c._setpoint<=50
            if first_on is None:first_on=r.pending[2]
            elif r.pending is None or r.pending[2]>first_on:
                acknowledged=True;break
    assert acknowledged

async def test_no_cycle_writes_during_coordinator_setup(ready,monkeypatch):
    c,old,k,clock=ready
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=None))
    async def setup():
        before=k.async_set_heater_state.await_count
        await r.cycle()
        assert k.async_set_heater_state.await_count == before
    monkeypatch.setattr(k,'async_added_to_hass',setup)
    await r.start()
    assert r.startup_ready and r.tripped is None

async def test_final_start_stop_publish_failure_requires_recovery(ready,monkeypatch):
    c,old,k,clock=ready
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=None))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    k.async_set_heater_state.side_effect=[None, TimeoutError()]
    await r.start()
    assert not r.startup_ready and not r.starting
    assert r.startup_recovery_deadline is not None
    assert c.hvac_mode == HVACMode.OFF
    k.async_set_control_setpoint.assert_awaited_with(10)

async def test_final_start_off_cannot_ack_from_unchanged_epochs(ready,monkeypatch):
    c,old,k,clock=ready
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=None))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    await r.start()
    data={key:dict(value=value,epoch=10) for key,value in dict(chenable='Off',controlsetpoint=10).items()}
    for t in [1210,1240,1270]:
        r.telemetry.observe(data,t)
        try:r.confirm(t)
        except ValueError:pass
        assert r.pending is not None
    with pytest.raises(ValueError,match='command_not_confirmed'):r.confirm(1290)
    assert r.tripped=='command_not_confirmed'

@pytest.mark.parametrize('stage',['poll','setpoint','on'])
async def test_fresh_valid_report_during_io_does_not_cycle_ch(ready,stage):
    import json
    c,r,k,clock=ready
    data=r.poll.return_value
    called=False
    async def update(*args):
        nonlocal called
        if not called:
            called=True
            r.message(SimpleNamespace(payload=json.dumps(event(seq=2,at=1200,
                pi_heating_demand=dict(value=95,valid=True))),retain=False))
        return data
    target={'poll':r.poll,'setpoint':k.async_set_control_setpoint,'on':k.async_set_heater_state}[stage]
    target.side_effect=update
    await c.async_control_heating_loop()
    assert r.status=='heat'
    assert r.decision_sequence==2
    assert all(x.args==(DeviceState.ON,) for x in k.async_set_heater_state.await_args_list)
    assert 10<c._setpoint<=50

@pytest.mark.parametrize('change',['hr_false','invalid_pi','room_block','disconnect'])
async def test_unsafe_input_change_during_cs_still_stops(ready,change):
    import json
    c,r,k,clock=ready
    called=False
    async def update(*args):
        nonlocal called
        if called:return
        called=True
        if change=='room_block':
            c.hass.states.async_set('binary_sensor.a_eligible','off')
            r.policy_changed(None)
        elif change=='disconnect':r.connection_changed(False)
        else:
            fields=({'heat_required':dict(value=False,valid=True)} if change=='hr_false'
                else {'pi_heating_demand':dict(value=None,valid=False)})
            r.message(SimpleNamespace(payload=json.dumps(event(seq=2,at=1200,**fields)),retain=False))
    k.async_set_control_setpoint.side_effect=update
    await c.async_control_heating_loop()
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)
    assert not r.ramp.on

async def test_repeated_reports_keep_ch_on_and_water_ramp(ready):
    import json
    c,r,k,clock=ready
    data=r.poll.return_value
    seq=1
    async def publish(value):
        nonlocal seq
        seq+=1
        r.message(SimpleNamespace(payload=json.dumps(event(seq=seq,at=clock[0],
            pi_heating_demand=dict(value=95,valid=True))),retain=False))
    k.async_set_control_setpoint.side_effect=publish
    targets=[]
    for i in range(12):
        clock[0]=1200+i*30
        for v in data.values():v['epoch']+=1
        data['chenable']['value']='On' if r.ramp.on else 'Off'
        data['controlsetpoint']['value']=c._setpoint if r.ramp.on else 10
        await c.async_control_heating_loop()
        assert r.status in ('heat','waiting_for_on_confirmation') and r.tripped is None
        targets.append(c._setpoint)
    assert all(x.args==(DeviceState.ON,) for x in k.async_set_heater_state.await_args_list)
    assert targets[-1]>targets[0] and max(targets)<=50

async def test_season_boot_with_saved_fault_still_rejects_current_boiler_fault(ready,monkeypatch):
    from homeassistant.core import State
    c,old,k,clock=ready
    monkeypatch.setattr(type(c.hass),'is_running',property(lambda self:False))
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,'off',{'ally_fault':'command_not_confirmed'})))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,'on')
    await r.start()
    monkeypatch.setattr(type(c.hass),'is_running',property(lambda self:True))
    r.season_boot_ready(None);r.resume_for_season()
    assert c.hvac_mode==HVACMode.HEAT
    data={key:dict(value=value,epoch=10) for key,value in dict(chenable='Off',controlsetpoint=10,faultindicator='On',outsidetemperature=5,boilertemperature=40,dhwmode='Off',maxrelmodlvl=100).items()}
    r.poll=AsyncMock(return_value=data)
    await r.cycle()  # final boot OFF plus baseline
    for v in data.values():v['epoch']+=1
    clock[0]+=20
    await c.async_control_heating_loop()
    assert 'Boiler fault' in r.status
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)


async def test_pending_off_baseline_does_not_spend_ack_window_on_republish(ready):
    c,r,k,clock=ready
    r.telemetry=runtime.Telemetry()
    r.pending=(False,10,1200)
    r.last_stop_sent=1200
    async def slow_write(*args): clock[0]+=25
    k.async_set_heater_state.side_effect=slow_write
    k.async_set_control_setpoint.side_effect=slow_write
    clock[0]=1250
    await r.cycle()  # First HTTP read is only a baseline.
    assert clock[0]==1250
    k.async_set_heater_state.assert_not_awaited()
    k.async_set_control_setpoint.assert_not_awaited()
    clock[0]=1280
    for f in r.poll.return_value.values(): f['epoch']+=1
    r.reports.heads.clear()  # Confirm OFF even without all reports; no ON.
    await r.cycle()
    assert r.tripped is None
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)

async def test_pending_off_read_failure_does_not_renew_or_republish(ready):
    c,r,k,clock=ready
    r.pending=(False,10,1200);r.last_stop_sent=1200
    r.poll.side_effect=TimeoutError()
    clock[0]=1250
    await r.cycle()
    assert r.pending==(False,10,1200)
    k.async_set_heater_state.assert_not_awaited()
    clock[0]=1291
    await r.cycle()
    assert r.tripped=='command_not_confirmed'
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)

async def test_long_ha_boot_starts_one_final_ack_transaction_after_ready(ready, monkeypatch):
    from homeassistant.core import State
    c,old,k,clock=ready
    running=[False]
    monkeypatch.setattr(type(c.hass),'is_running',property(lambda self:running[0]))
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,'off')))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,'on')
    await r.start()
    before=k.async_set_heater_state.await_count
    clock[0]+=180
    await r.cycle()
    assert r.tripped is None
    assert k.async_set_heater_state.await_count==before
    running[0]=True;r.season_boot_ready(None)
    data={key:dict(value=value,epoch=10) for key,value in dict(chenable='Off',controlsetpoint=10,faultindicator='Off',outsidetemperature=5,boilertemperature=40,dhwmode='Off',maxrelmodlvl=100).items()}
    r.poll=AsyncMock(return_value=data)
    await r.cycle()
    assert r.pending==(False,10,1380)
    assert c.hvac_mode==HVACMode.HEAT and r.tripped is None
    assert k.async_set_heater_state.await_count==before+1
    clock[0]+=20
    for v in data.values():v['epoch']+=1
    # No reports: confirm OFF, but do not invent a heat demand.
    await r.cycle()
    assert r.tripped is None and r.telemetry.value('chenable',clock[0])=='Off'
    assert all(x.args==(DeviceState.OFF,) for x in k.async_set_heater_state.await_args_list)
    r.poll.side_effect=TimeoutError()
    original=r.pending
    clock[0]+=100
    await r.cycle()
    assert r.tripped=='command_not_confirmed' and r.pending==original
    assert not r.boot_ack_pending

async def test_long_boot_then_valid_demand_reaches_confirmed_heat(ready, monkeypatch):
    c,r,k,clock=ready
    r.boot_ack_pending=True
    r.pending=(False,10,900)
    monkeypatch.setattr(type(c.hass),'is_running',property(lambda self:True))
    r.telemetry=runtime.Telemetry()
    data=r.poll.return_value
    for i in range(12):
        clock[0]=1200+i*20
        r.reports.accept(event(seq=i+2,at=clock[0],heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=92,valid=True)),clock[0])
        for v in data.values():v['epoch']+=1
        data['chenable']['value']='On' if r.ramp.on else 'Off'
        data['controlsetpoint']['value']=c._setpoint if r.ramp.on else 10
        await r.cycle()
        assert r.tripped is None
    assert r.status in ('heat','waiting_for_on_confirmation') and r.modulation_confirmed
    assert 10<c._setpoint<=50
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)

async def test_report_store_roundtrip_restores_hr_only_after_live_continuity(ready, monkeypatch):
    from homeassistant.core import State
    c,old,k,clock=ready
    await old.report_store.async_save({'identity':old.restore_key,'reports':old.reports.snapshot(clock[0])})
    clock[0]+=10
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,'off')))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    await r.start()
    assert r.report_restore_status=='waiting_for_source_continuity'
    assert not r.reports.heads
    clock[0]+=1
    r.message(SimpleNamespace(payload=__import__('json').dumps(event(seq=2,at=clock[0],pi_heating_demand=dict(value=94,valid=True))),retain=False))
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1200)
    assert r.attributes['ally_restored_reports']['a']['heat_required']['age_seconds']==11
    assert r.report_restore_status=='restored'
    assert r.pending is not None  # Restore is never a command acknowledgement.
    r.message(SimpleNamespace(payload=__import__('json').dumps(event(seq=3,at=clock[0],heat_required=dict(value=None,valid=False))),retain=False))
    assert r.reports.heads['a']['heat_required']['value'] is None
    assert not r.reports.restored_fields

async def test_report_store_changed_config_is_not_loaded(ready,monkeypatch):
    from homeassistant.core import State
    c,old,k,clock=ready
    await old.report_store.async_save({'identity':'different','reports':old.reports.snapshot(clock[0])})
    r=runtime.Runtime(c,SETTINGS);c._ally_runtime=r
    monkeypatch.setattr(c,'async_get_last_state',AsyncMock(return_value=State(c.entity_id,'off')))
    monkeypatch.setattr(k,'async_added_to_hass',AsyncMock())
    await r.start()
    assert r.reports.restore_pending is None and not r.reports.heads

async def test_restored_hr_drives_heat_without_new_hr(ready):
    c,r,k,clock=ready
    saved=r.reports.snapshot(clock[0])
    clock[0]=1210
    r.reports=runtime.Reports(r.reports.mapping,clock[0])
    assert r.reports.stage_restore(saved,clock[0],r.policy.ttl)
    data=r.poll.return_value
    for i in range(12):
        clock[0]=1211+i*20
        r.message(SimpleNamespace(payload=__import__('json').dumps(event(seq=i+2,at=clock[0],pi_heating_demand=dict(value=94,valid=True))),retain=False))
        for v in data.values():v['epoch']+=1
        data['chenable']['value']='On' if r.ramp.on else 'Off'
        data['controlsetpoint']['value']=c._setpoint if r.ramp.on else 10
        await r.cycle()
        assert r.tripped is None
    assert r.status in ('heat','waiting_for_on_confirmation') and 10<c._setpoint<=50
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1200)
    assert ('a','heat_required') in r.reports.restored_fields


async def test_source_snapshot_active_restores_preboot_without_boiler_bypass(ready,monkeypatch):
    from tests.test_ally_mode import source_snapshot
    from custom_components.sat.ally.control import Reports
    c,r,coordinator,clock=ready
    r.reports=Reports({'0x0000000000000001':'a'},1190)
    monkeypatch.setattr(runtime.mqtt,'is_connected',lambda h:True)
    snap=source_snapshot([event(at=1100,heat_required=dict(value=True,valid=True),
                                pi_heating_demand=dict(value=99,valid=True))],1200)
    r.snapshot_message(SimpleNamespace(payload=__import__('json').dumps(snap),retain=False))
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1100)
    assert r.attributes['ally_restored_reports']['a']['heat_required']['source']=='verified_source_snapshot'
    coordinator.async_set_heater_state.assert_not_called()
    coordinator.async_set_control_setpoint.assert_not_called()
    # Normal cycle still owns policy and new command acknowledgements.
    await r.cycle()
    assert r.status=='heat' and 10<c._setpoint<=50
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1100)
    c.hass.states.async_set('binary_sensor.a_eligible','off')
    for v in r.poll.return_value.values():v['epoch']+=1
    clock[0]+=30
    await r.cycle()
    coordinator.async_set_heater_state.assert_awaited_with(DeviceState.OFF)


async def verified_running(ready):
    c,r,k,clock=ready
    await c.async_control_heating_loop()
    clock[0]=1210
    values=dict(outsidetemperature=5,chenable='On',controlsetpoint=41,
                boilertemperature=45,faultindicator='Off',dhwmode='Off',maxrelmodlvl=100)
    r.telemetry=runtime.Telemetry()
    r.telemetry.observe({key:dict(epoch=2,value=value) for key,value in values.items()},1205)
    r.telemetry.observe({key:dict(epoch=3,value=value) for key,value in values.items()},1210)
    r.pending=None
    k.async_set_heater_state.reset_mock();k.async_set_control_setpoint.reset_mock()
    r.poll.side_effect=TimeoutError()
    return c,r,k,clock


async def test_read_timeout_holds_verified_on_without_writes_or_reaging(ready):
    from copy import deepcopy
    c,r,k,clock=await verified_running(ready)
    proof=deepcopy(r.telemetry.fields)
    for t in (1210,1240):
        clock[0]=t
        await c.async_control_heating_loop()
        assert r.status=='telemetry_retry_holding_heat'
        assert r.telemetry.fields==proof
        k.async_set_heater_state.assert_not_awaited()
        k.async_set_control_setpoint.assert_not_awaited()
    clock[0]=1255
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_with(10)


@pytest.mark.parametrize('veto',['fault','dhw','hot','permission','hr','expired_hr','ack_deadline','off_pending','mm','cs'])
async def test_read_failure_never_hides_safety_or_expired_ack(ready,veto):
    c,r,k,clock=await verified_running(ready)
    if veto in ('fault','dhw','hot','mm','cs'):
        key,val={'fault':('faultindicator','On'),'dhw':('dhwmode','On'),
                 'hot':('boilertemperature',60),'mm':('maxrelmodlvl',0),
                 'cs':('controlsetpoint',51)}[veto]
        r.telemetry.fields[key]=(3,val,1205)
    elif veto=='permission':c.hass.states.async_set('input_boolean.heat_allowed','off')
    elif veto=='hr':r.reports.heads['a']['heat_required']['value']=False
    elif veto=='expired_hr':r.reports.heads['a']['heat_required']['observed_at']=ts(-3000)
    elif veto=='ack_deadline':r.pending=(True,41,1120)
    elif veto=='off_pending':r.pending=(False,10,1200)
    await c.async_control_heating_loop()
    assert r.status!='telemetry_retry_holding_heat'
    k.async_set_heater_state.assert_not_awaited() if veto=='off_pending' else k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    if veto=='ack_deadline':assert r.tripped=='command_not_confirmed'


async def test_recovery_after_read_gap_uses_original_baseline(ready):
    c,r,k,clock=await verified_running(ready)
    r.pending=(True,41,1208)
    await c.async_control_heating_loop()
    assert r.pending==(True,41,1208)  # Cache did not acknowledge it.
    clock[0]=1220;r.poll.side_effect=None
    r.poll.return_value={key:dict(epoch=4,value=v[1]) for key,v in r.telemetry.fields.items()}
    await c.async_control_heating_loop()
    assert r.status=='heat' and r.tripped is None
    assert r.pending[2]==1220  # Fresh ACK permits the changed ramp target.
    k.async_set_heater_state.assert_not_awaited()  # Already confirmed ON.


@pytest.mark.parametrize('error',[MonitorUnavailable('OTGW monitor backoff'),
                                  MonitorUnavailable('OTGW monitor HTTP 429')])
async def test_bounded_monitor_backoff_does_not_restart_pump(ready,error):
    c,r,k,clock=await verified_running(ready)
    r.poll.side_effect=error
    await c.async_control_heating_loop()
    assert r.status=='telemetry_retry_holding_heat'
    k.async_set_heater_state.assert_not_awaited()


async def test_schema_failure_is_not_read_grace(ready):
    from custom_components.sat.ally.monitor import MonitorUnavailable
    c,r,k,clock=await verified_running(ready)
    r.poll.side_effect=MonitorUnavailable('OTGW monitor schema')
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)


async def test_pending_on_refreshes_cs_but_not_ch_at_1807_timeline(ready):
    c,r,k,clock=await verified_running(ready)
    r.poll.side_effect=None
    r.poll.return_value={key:dict(epoch=v[0],value=v[1]) for key,v in r.telemetry.fields.items()}
    r.pending=(True,41,1208)
    original=r.pending
    # Original receipts predate the command: fresh enough for safety, not ACK.
    for now, writes in ((1249,1),(1263,1),(1272,2)):
        clock[0]=now
        await r.cycle()
        assert r.status=='waiting_for_on_confirmation' and r.pending==original
        assert k.async_set_control_setpoint.await_count==writes  # CS lease, original ACK unchanged.
        k.async_set_control_setpoint.assert_awaited_with(41)
        k.async_set_heater_state.assert_not_awaited()
    clock[0]=1298
    await r.cycle()
    assert r.tripped=='command_not_confirmed'
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)


@pytest.mark.parametrize('stage',['write_cs','write_ch_on'])
async def test_write_timeout_running_keeps_original_deadline_until_live_ack(ready,stage):
    c,r,k,clock=await verified_running(ready)
    r.poll.side_effect=None
    r.poll.return_value={key:dict(epoch=4,value=v[1]) for key,v in r.telemetry.fields.items()}
    async def timeout(*args):
        clock[0]+=15
        raise TimeoutError()
    writer=k.async_set_control_setpoint if stage=='write_cs' else k.async_set_heater_state
    writer.side_effect=timeout
    await r.cycle()
    assert r.status=='write_retry_holding_heat'
    assert r.attributes['ally_last_error_stage']==stage
    pending=r.pending
    assert pending[0] and pending[2]==1210
    assert all(call.args!=(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)
    writer.side_effect=None
    k.async_set_control_setpoint.reset_mock();k.async_set_heater_state.reset_mock()
    clock[0]=1240
    await r.cycle()
    assert r.pending==pending and r.status=='waiting_for_on_confirmation'
    k.async_set_heater_state.assert_not_awaited()
    # A genuinely newer matching pair, not cached proof, releases the transaction.
    clock[0]=1250
    for v in r.poll.return_value.values():v['epoch']+=1
    r.poll.return_value['controlsetpoint']['value']=pending[1]
    await r.cycle()
    assert r.status=='heat' and r.pending[2]==1250


@pytest.mark.parametrize('veto',['off','permission','expired','dhw','fault','hot','mm','hr'])
async def test_write_timeout_does_not_hide_veto(ready,veto):
    c,r,k,clock=await verified_running(ready)
    r.poll.side_effect=None
    r.poll.return_value={key:dict(epoch=4,value=v[1]) for key,v in r.telemetry.fields.items()}
    async def failed(value):
        if value==10:return
        if veto=='permission':c.hass.states.async_set('input_boolean.heat_allowed','off')
        elif veto=='expired':clock[0]+=91
        elif veto=='hr':r.reports.heads['a']['heat_required']['value']=False
        else:
            key,val={'off':('chenable','Off'),'dhw':('dhwmode','On'),
                     'fault':('faultindicator','On'),'hot':('boilertemperature',60),
                     'mm':('maxrelmodlvl',0)}[veto]
            r.telemetry.fields[key]=(5,val,clock[0])
        raise TimeoutError()
    k.async_set_control_setpoint.side_effect=failed
    await r.cycle()
    assert r.status!='write_retry_holding_heat'
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)


async def boot_verifier(ready):
    c,r,k,clock=ready
    r.boot_verify_deadline=1800
    r.boot_verify_attempts=1
    r.pending=(False,10,1200)
    r.last_stop_sent=1200
    return c,r,k,clock


async def test_boot_replay_185430_read_gap_then_new_attempt_and_heat(ready):
    c,r,k,clock=await boot_verifier(ready)
    r.telemetry=runtime.Telemetry()
    r.poll.return_value['boilertemperature']['value']=51.7
    await r.cycle()  # 18:54:30: baseline only.
    assert r.status=='startup_waiting_for_telemetry'
    proof=dict(r.telemetry.fields)
    for t in (1229,1233,1263):  # actual 18:54:59,18:55:03,18:55:33 backoff
        clock[0]=t;r.poll.side_effect=MonitorUnavailable('OTGW monitor backoff')
        await r.cycle()
        assert r.telemetry.fields==proof and r.pending==(False,10,1200)
    clock[0]=1286;r.poll.side_effect=None
    for v in r.poll.return_value.values():v['epoch']+=1
    r.poll.return_value['controlsetpoint']['value']=0
    r.poll.return_value['boilertemperature']['value']=52.7
    await r.cycle()  # Preserved baseline proves the advancing OFF epoch in time.
    assert r.tripped is None and r.boot_verify_deadline is None
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert 10<c._setpoint<=50
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1200)


async def test_boot_late_safe_evidence_starts_new_off_not_late_ack(ready):
    c,r,k,clock=await boot_verifier(ready)
    r.telemetry=runtime.Telemetry()
    clock[0]=1286
    r.poll.return_value['boilertemperature']['value']=52.7
    await r.cycle()  # 18:55:56 new baseline after a long loss.
    clock[0]=1299
    for v in r.poll.return_value.values():v['epoch']+=1
    await r.cycle()  # 18:56:09, old90s expired, fresh safe OFF -> new attempt.
    assert r.tripped is None and r.boot_verify_attempts==2
    assert r.pending==(False,10,1299) and r.boot_verify_deadline==1800
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_once_with(10)
    clock[0]=1310
    await r.cycle()  # unchanged snapshot cannot ACK a new attempt
    assert r.pending==(False,10,1299)
    assert k.async_set_heater_state.await_count==1
    clock[0]=1320
    r.poll.return_value['boilertemperature']['value']=55.7
    for v in r.poll.return_value.values():v['epoch']+=1
    await r.cycle()
    assert r.boot_verify_deadline is None and r.tripped is None
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON)
    assert 10<c._setpoint<=50


async def test_boot_recovery_budget_does_not_renew_during_outage(ready):
    c,r,k,clock=await boot_verifier(ready)
    r.poll.side_effect=TimeoutError()
    for t in (1250,1300,1500,1799):
        clock[0]=t;await r.cycle()
        assert r.boot_verify_deadline==1800 and r.tripped is None
        k.async_set_heater_state.assert_not_awaited()
    clock[0]=1800;await r.cycle()
    assert r.tripped=='startup_recovery_expired'
    # The prior OFF has expired; a terminal fault cannot restart its retries.
    k.async_set_heater_state.assert_not_awaited()


@pytest.mark.parametrize('veto',['fault','ch_on','cs_high','schema','manual_off','global_off','attempts'])
async def test_boot_retry_never_bypasses_safety(ready,veto):
    c,r,k,clock=await boot_verifier(ready)
    clock[0]=1300
    r.telemetry=runtime.Telemetry()
    r.telemetry.observe({key:dict(epoch=1,value=v['value']) for key,v in r.poll.return_value.items()},1290)
    if veto=='fault':r.poll.return_value['faultindicator']['value']='On'
    elif veto=='ch_on':r.poll.return_value['chenable']['value']='On'
    elif veto=='cs_high':r.poll.return_value['controlsetpoint']['value']=30
    elif veto=='schema':r.poll.side_effect=MonitorUnavailable('OTGW monitor schema')
    elif veto=='attempts':r.boot_verify_attempts=3
    elif veto=='global_off':c.hass.states.async_set('input_boolean.heat_allowed','off')
    elif veto=='manual_off':await c.async_set_hvac_mode(HVACMode.OFF)
    await r.cycle()
    assert all(call.args!=(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)
    if veto=='manual_off':assert r.boot_verify_deadline is None and c.hvac_mode==HVACMode.OFF
    if veto=='attempts':assert r.tripped=='startup_attempts_exhausted'

async def test_manual_off_during_boot_read_cancels_recovery(ready):
    c,r,k,clock=await boot_verifier(ready)
    entered,release=asyncio.Event(),asyncio.Event()
    data=r.poll.return_value
    async def pause():entered.set();await release.wait();return data
    r.poll.side_effect=pause
    work=asyncio.create_task(r.cycle());await entered.wait()
    off=asyncio.create_task(c.async_set_hvac_mode(HVACMode.OFF));await asyncio.sleep(0)
    release.set();await work;await off
    assert c.hvac_mode==HVACMode.OFF and r.boot_verify_deadline is None
    assert r.tripped is None
    assert all(call.args!=(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)

async def test_boot_fixed_deadline_survives_slow_initial_stop(ready,monkeypatch):
    c,r,k,clock=ready
    monkeypatch.setattr(type(c.hass),'is_running',property(lambda self:True))
    r.season_start_pending=True;r.boot_ack_pending=True
    r.season_boot_ready(None)
    assert r.boot_verify_deadline==1800
    before=dict(r.command_writes)
    clock[0]=1300
    await r.cycle()
    assert r.boot_verify_deadline==1800 and r.boot_verify_attempts==1
    assert r.command_writes['ch_on']==before['ch_on']
    assert r.command_writes['ch_off']==before['ch_off']+1

async def test_full_boot_initial_write_timeout_enters_bounded_verification(ready,monkeypatch):
    c,r,k,clock=ready
    monkeypatch.setattr(type(c.hass),'is_running',property(lambda self:True))
    r.boot_ack_pending=True;r.season_start_pending=True;r.startup_ready=False
    r.startup_recovery_deadline=1700
    r.season_boot_ready(None)
    await r.cycle()
    assert r.startup_recovery_deadline is None and r.startup_ready
    assert r.boot_verify_deadline==1800 and r.boot_verify_attempts==1
    assert r.tripped is None
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)

@pytest.mark.parametrize('veto',['hot','dhw','expired_hr'])
async def test_successful_boot_ack_does_not_bypass_normal_guards(ready,veto):
    c,r,k,clock=await boot_verifier(ready)
    r.pending=(False,10,1180)  # fresh OFF proof at1190 can complete this attempt
    if veto=='hot':r.poll.return_value['boilertemperature']['value']=60.1
    elif veto=='dhw':r.poll.return_value['dhwmode']['value']='On'
    else:r.reports.heads['a']['heat_required']['observed_at']=ts(-3000)
    await r.cycle()
    assert r.boot_verify_deadline is None
    assert all(call.args!=(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)
    if veto in ('hot', 'dhw'):
        # The just-confirmed startup OFF is sufficient during passive water
        # blocking. Keep the guard, without starting a second OFF transaction.
        expected = 'Boiler water outside experimental guard' if veto == 'hot' else 'DHW priority'
        assert r.status == 'input_or_transport_error:' + expected
        assert r.confirmed_off() and r.pending is None and not r.ramp.on
        assert c._setpoint == 10
        k.async_set_heater_state.assert_not_awaited()
        k.async_set_control_setpoint.assert_not_awaited()
    else:
        k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

# 21 Oct 19:40:26Z: pending ON 46.5 aged 63.2s, tolerated read gap.
# 19:40:57Z: expiry, CH OFF; 22 Oct 07:05Z: same fault after 811 stop
# retries. These tests replay the observed timing/state inputs; post-fix
# acknowledgements are synthetic, not evidence of physical boiler recovery.
async def test_overnight_ack_expiry_recovers_without_rejuvenating_reports(ready):
    c,r,k,clock=ready
    r.pending=(True,46.5,1136.8)
    r.poll.side_effect=TimeoutError()
    clock[0]=1231
    await r.cycle()
    assert r.tripped=='command_not_confirmed' and r.pending==(False,10,1231)
    writes=k.async_set_heater_state.await_count
    for t in range(1261,42300,30):
        clock[0]=t
        await r.cycle()
    assert k.async_set_heater_state.await_count==writes
    assert r.poll.await_count>1000  # fault does not suppress read-only recovery
    clock[0]=42308
    r.reports.accept(event(seq=2,at=42200,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=99,valid=True)),42308)
    original=__import__("copy").deepcopy(r.reports.heads)
    r.poll.side_effect=None
    for f in r.poll.return_value.values():f['epoch']=10
    await r.cycle()  # First HTTP response after the gap is only a baseline.
    assert r.pending==(False,10,1231)
    clock[0]+=20
    for f in r.poll.return_value.values():f['epoch']+=1
    await r.cycle()
    assert r.tripped=='command_not_confirmed'
    assert r.pending==(False,10,42328) and r.command_recovery_attempts==1
    # Same cached epochs cannot confirm the newly issued OFF.
    clock[0]+=10
    await r.cycle()
    assert r.tripped=='command_not_confirmed'
    for f in r.poll.return_value.values():f['epoch']+=1
    clock[0]+=10
    await r.cycle()
    assert r.tripped is None and r.pending is None
    assert r.reports.heads==original
    assert not r.modulation_confirmed
    assert all(call.args==(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)
    # A new RM confirmation and normal safety/demand evaluation precede ON.
    for i in range(12):
        clock[0]+=20
        for f in r.poll.return_value.values():f['epoch']+=1
        r.poll.return_value['chenable']['value']='On' if r.ramp.on else 'Off'
        r.poll.return_value['controlsetpoint']['value']=c._setpoint if r.ramp.on else 10
        await r.cycle()
    assert r.tripped is None and r.modulation_confirmed
    assert any(call.args==(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)
    assert 10<c._setpoint<=50

async def test_stop_publish_timeouts_are_bounded_even_without_return(ready):
    c,r,k,clock=ready
    k.async_set_heater_state.side_effect=TimeoutError()
    k.async_set_control_setpoint.side_effect=TimeoutError()
    for t in range(1200,1800,5):
        clock[0]=t
        try:await r.stop('transport_error')
        except TimeoutError:pass
    assert k.async_set_heater_state.await_count==3
    assert k.async_set_control_setpoint.await_count==3
    assert r.pending==(False,10,1200)

@pytest.mark.parametrize('block',['cached','unsafe_cs','ch_on','fault','manual_off','global_off','expired_demand','dhw','hot_water'])
async def test_command_recovery_never_bypasses_safety(ready,block):
    c,r,k,clock=ready
    r.tripped='command_not_confirmed';r.pending=(False,10,1000)
    data=r.poll.return_value
    if block=='cached':
        for f in data.values():f['epoch']=1
    if block=='unsafe_cs':data['controlsetpoint']['value']=11
    if block=='ch_on':data['chenable']['value']='On'
    if block=='fault':data['faultindicator']['value']='On'
    if block=='manual_off':c._hvac_mode=HVACMode.OFF
    if block=='global_off':c.hass.states.async_set('input_boolean.heat_allowed','off')
    if block=='expired_demand':r.reports.heads.clear()
    if block=='dhw':data['dhwmode']['value']='On'
    if block=='hot_water':data['boilertemperature']['value']=65
    for i in range(15):
        clock[0]+=20
        if block!='cached':
            for f in data.values():f['epoch']+=1
        await r.cycle()
    assert all(call.args==(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)

async def test_recovery_has_three_new_transactions_not_infinite_deadline_renewal(ready):
    c,r,k,clock=ready
    r.tripped='command_not_confirmed';r.pending=(False,10,1000)
    # MQTT receipts are fresh, but each transaction is already expired when read.
    values={k:v['value'] for k,v in r.poll.return_value.items()}
    for i in range(8):
        clock[0]=1200+i*100
        r.poll.return_value=runtime.MqttSnapshot({k:(i+2,v,clock[0]-1) for k,v in values.items()})
        await r.cycle()
    assert r.tripped=='command_recovery_exhausted'
    assert r.command_recovery_attempts==3
    assert k.async_set_heater_state.await_count==3

async def test_operator_off_during_recovery_read_cancels_restart(ready):
    c,r,k,clock=ready
    r.tripped='command_not_confirmed';r.pending=(False,10,1000)
    entered,release=asyncio.Event(),asyncio.Event()
    raw=r.poll.return_value
    async def read():
        entered.set();await release.wait();return raw
    r.poll.side_effect=read
    work=asyncio.create_task(r.cycle());await entered.wait()
    off=asyncio.create_task(c.async_set_hvac_mode(HVACMode.OFF));await asyncio.sleep(0)
    release.set();await work;await off
    assert c.hvac_mode==HVACMode.OFF
    assert all(call.args==(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)

@pytest.mark.parametrize('fault',['modulation_not_confirmed','startup_recovery_expired','command_recovery_exhausted','Boiler fault'])
async def test_command_recovery_does_not_clear_other_faults(ready,fault):
    c,r,k,clock=ready
    r.tripped=fault;r.pending=(False,10,1000)
    await r.cycle()
    assert r.tripped==fault
    r.poll.assert_not_awaited()
    k.async_set_heater_state.assert_not_awaited()

async def test_fresh_safety_stop_ack_recovers_without_an_extra_stop(ready):
    c,r,k,clock=ready
    r.tripped='command_not_confirmed';r.pending=(False,10,1190)
    values={key:v['value'] for key,v in r.poll.return_value.items()}
    r.poll.return_value=runtime.MqttSnapshot({key:(2,v,1199) for key,v in values.items()})
    await r.cycle()
    assert r.tripped is None and r.pending is None
    k.async_set_heater_state.assert_not_awaited()
    assert not r.modulation_confirmed

async def test_explicit_off_resets_recovery_episode_budget(ready):
    c,r,k,clock=ready
    r.command_recovery_attempts=3
    r.tripped='command_recovery_exhausted'
    await c.async_set_hvac_mode(HVACMode.OFF)
    assert r.command_recovery_attempts==0
    assert c.hvac_mode==HVACMode.OFF

# PIC CS>=8 requires a sub-minute lease refresh even with an unchanged target.
async def confirmed_keepalive(ready):
    c,r,k,clock=ready
    r.ramp.target=Mock(return_value=41)
    await c.async_control_heating_loop()
    clock[0]=1205
    values={key:item['value'] for key,item in r.poll.return_value.items()}
    values.update(chenable='On',controlsetpoint=41)
    r.poll.return_value={key:dict(value=v,epoch=3) for key,v in values.items()}
    await c.async_control_heating_loop()
    clock[0]=1206
    r.poll.return_value={key:dict(value=v,epoch=4) for key,v in values.items()}
    await c.async_control_heating_loop()
    assert r.pending is None
    k.async_set_control_setpoint.reset_mock();k.async_set_heater_state.reset_mock()
    return c,r,k,clock,values


async def test_oct30_partial_http_mm_preserves_heat_and_original_expiry(ready):
    # Reconstruct the missing-MM shape from runtime evidence at16:50CET;
    # the original raw HTTP response was not recorded. No live devices.
    c,r,k,clock,values=await confirmed_keepalive(ready)
    proof=r.telemetry.fields['maxrelmodlvl']
    ack=dict(r.last_ack)
    for now in (1241,1260,1280,1295):
        clock[0]=now
        r.poll.return_value={key:dict(value=v,epoch=now) for key,v in values.items()
                             if key!='maxrelmodlvl'}
        await c.async_control_heating_loop()
        assert r.status=='heat' and r.pending is None and r.last_ack==ack
        assert r.telemetry.fields['maxrelmodlvl']==proof
        assert r.attributes['ally_http_missing_fields']==['maxrelmodlvl']
        assert r.modulation_confirmed
        k.async_set_heater_state.assert_not_awaited()
    clock[0]=1295.001
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    assert not r.modulation_confirmed and r.pending[:2]==(False,10)


@pytest.mark.parametrize('field,value', [('faultindicator','On'),('dhwmode','On'),
                                         ('boilertemperature',80)])
async def test_partial_http_does_not_hide_new_fault_dhw_or_hot_water(ready,field,value):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1210
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()
                         if key!='maxrelmodlvl'}
    r.poll.return_value[field]['value']=value
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_with(10)


async def test_missing_http_ch_cs_cannot_ack_a_new_command(ready):
    c,r,k,clock=ready
    await c.async_control_heating_loop()
    pending=r.pending
    clock[0]=1210
    r.poll.return_value={key:dict(value=v['value'],epoch=3)
                         for key,v in r.poll.return_value.items()
                         if key not in ('chenable','controlsetpoint')}
    await c.async_control_heating_loop()
    assert r.pending==pending and r.last_ack is None

async def test_keepalive_unchanged_target_keeps_ack_and_does_not_toggle_ch(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    ack=dict(r.last_ack)
    for t in (1210,1220,1225,1235,1250):
        clock[0]=t
        r.poll.return_value={key:dict(value=v,epoch=t) for key,v in values.items()}
        await c.async_control_heating_loop()
        assert r.pending is None and r.last_ack==ack
        assert r.status=='heat'
    assert k.async_set_control_setpoint.await_count==3
    k.async_set_heater_state.assert_not_awaited()
    assert r.cs_keepalive_writes==3

async def test_keepalive_does_not_extend_original_pending_ack(ready):
    c,r,k,clock=ready
    await c.async_control_heating_loop()
    pending=r.pending
    k.async_set_control_setpoint.reset_mock();k.async_set_heater_state.reset_mock()
    # Repeated old epoch is not an ACK. Normal safe inputs remain valid.
    for t in (1225,1250,1275):
        clock[0]=t
        await c.async_control_heating_loop()
        assert r.pending==pending
    assert k.async_set_control_setpoint.await_count==3
    k.async_set_heater_state.assert_not_awaited()
    clock[0]=1290
    await c.async_control_heating_loop()
    assert r.tripped=='command_not_confirmed'
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

async def test_changed_target_creates_ack_without_redundant_ch_on(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1220
    # Force an otherwise valid ramp proposal; real gate/telemetry still runs.
    r.ramp.target=Mock(return_value=42)
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    await c.async_control_heating_loop()
    assert r.pending[:2]==(True,42)
    k.async_set_control_setpoint.assert_awaited_once_with(42)
    k.async_set_heater_state.assert_not_awaited()

async def test_keepalive_failure_never_reages_refresh_or_ack(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1225
    k.async_set_control_setpoint.side_effect=TimeoutError()
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    await c.async_control_heating_loop()
    assert r.last_cs_refresh==1200 and r.pending is None
    assert r.status=='write_retry_holding_heat'
    k.async_set_heater_state.assert_not_awaited()
    clock[0]=1255;k.async_set_control_setpoint.side_effect=None
    r.poll.return_value={key:dict(value=v,epoch=6) for key,v in values.items()}
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    assert r.last_cs_refresh is None

@pytest.mark.parametrize('veto',['off','permission','hr','dhw','fault'])
async def test_keepalive_never_bypasses_veto(ready,veto):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1225
    if veto=='off':c._hvac_mode=HVACMode.OFF
    if veto=='permission':c.hass.states.async_set('input_boolean.heat_allowed','off')
    if veto=='hr':
        r.reports.heads['a']['heat_required']['value']=False
        r.ramp.target=type(r.ramp).target.__get__(r.ramp)
    if veto=='dhw':values['dhwmode']='On'
    if veto=='fault':values['faultindicator']='On'
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    await c.async_control_heating_loop()
    assert r.cs_keepalive_writes==0
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)


async def test_keepalive_late_completion_is_not_a_new_lease(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1250
    async def delayed(value):
        if value!=10:clock[0]+=6
    k.async_set_control_setpoint.side_effect=delayed
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    assert r.last_cs_refresh is None

async def test_keepalive_replay_91_second_historical_ack_gap(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    # Historical gap was 91s between CS attempts. Refresh cadence is now
    # independent of target changes; model continuing fresh OTGW reports.
    sent=[]
    async def write(value):sent.append((clock[0],value))
    k.async_set_control_setpoint.side_effect=write
    for t in range(1210,1301,10):
        clock[0]=t
        r.poll.return_value={key:dict(value=v,epoch=t) for key,v in values.items()}
        await c.async_control_heating_loop()
    assert sent==[(1220,41),(1240,41),(1260,41),(1280,41),(1300,41)]
    assert r.pending is None
    k.async_set_heater_state.assert_not_awaited()


async def test_cs_only_is_independent_of_pm25_failure(ready):
    from custom_components.sat.compat.mqtt.opentherm import SatOpenThermMqttCoordinator
    c,r,k,clock,values=await confirmed_keepalive(ready)
    obj=object.__new__(SatOpenThermMqttCoordinator)
    async def publish(payload):
        if payload == 'PM=25':
            raise TimeoutError('optional temperature query')
    obj._publish_command=AsyncMock(side_effect=publish)
    k.async_set_control_setpoint_only=obj.async_set_control_setpoint_only
    ack=dict(r.last_ack);clock[0]=1230
    await r.write_heat_cs(41,keepalive=True)
    assert r.last_cs_refresh==1230 and r.last_ack==ack and r.pending is None
    assert [x.args[0] for x in obj._publish_command.await_args_list]==['CS=41']
    # The legacy room PID sequence still includes PM25 and exposes its error.
    with pytest.raises(TimeoutError):await obj.async_set_control_setpoint(41)
    assert obj._publish_command.await_args_list[-1].args==('PM=25',)

async def test_cs_only_failure_does_not_renew_lease(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    k.async_set_control_setpoint_only=AsyncMock(side_effect=TimeoutError('CS publish'))
    before=r.last_cs_refresh;clock[0]=1230
    with pytest.raises(TimeoutError):await r.write_heat_cs(41,keepalive=True)
    assert r.last_cs_refresh==before

async def test_read_timeout_refreshes_confirmed_cs_without_reaging_evidence(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    ack=dict(r.last_ack);fields=dict(r.telemetry.fields)
    r.poll.side_effect=TimeoutError('HTTP')
    for t in (1230,1260,1280):
        clock[0]=t
        await c.async_control_heating_loop()
        assert r.status=='telemetry_retry_holding_heat'
        assert r.pending is None and r.last_ack==ack and r.telemetry.fields==fields
    assert k.async_set_control_setpoint.await_count==3
    k.async_set_heater_state.assert_not_awaited()
    # Original telemetry expires at 90s, irrespective of successful CS refresh.
    clock[0]=1297
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

@pytest.mark.parametrize('veto',['permission','hr','dhw','fault'])
async def test_read_failure_keepalive_respects_veto(ready,veto):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    r.poll.side_effect=TimeoutError();clock[0]=1230
    if veto=='permission':c.hass.states.async_set('input_boolean.heat_allowed','off')
    elif veto=='hr':r.reports.accept(event(seq=2,at=1230,heat_required=dict(value=False,valid=True)),1230)
    else:
        values['dhwmode' if veto=='dhw' else 'faultindicator']='On'
        r.telemetry.observe({key:dict(value=v,epoch=5) for key,v in values.items()},1229)
    await c.async_control_heating_loop()
    assert r.cs_keepalive_writes==0
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

async def test_historical_1434_read_gap_does_not_starve_cs(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    ack=dict(r.last_ack)
    r.poll.side_effect=TimeoutError('historical HTTP read gap')
    # Recorded 14:34:03 CS age34.655, then age48.899 without a CS attempt.
    for elapsed in (34.655,48.899,59.329):
        clock[0]=1200+elapsed
        await c.async_control_heating_loop()
        assert r.status=='telemetry_retry_holding_heat'
    assert r.cs_keepalive_writes==2
    assert r.telemetry_read_failures==3 and r.cs_publish_failures==0
    assert r.last_cs_refresh==1259.329 and r.last_ack==ack
    k.async_set_heater_state.assert_not_awaited()

async def test_read_failure_refresh_does_not_extend_pending_ack(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    r.pending=(True,42,1206)
    r.poll.side_effect=TimeoutError('HTTP')
    clock[0]=1230
    await c.async_control_heating_loop()
    k.async_set_control_setpoint.assert_awaited_with(42)
    assert r.pending==(True,42,1206)
    assert r.last_ack['water']==41


async def test_historical_1714_cs_retry_before_original_deadline(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    ack=dict(r.last_ack);clock[0]=1236.256
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    attempts=[]
    async def publish(target):
        attempts.append(clock[0])
        if len(attempts)==1:
            clock[0]=1248.557
            raise TimeoutError('No ACK')
        clock[0]+=1
    k.async_set_control_setpoint.side_effect=publish
    await c.async_control_heating_loop()
    assert attempts==[1236.256,1248.557]
    assert r.last_cs_refresh==1248.557 and r.last_ack==ack and r.pending is None
    assert r.cs_retry_attempts==r.cs_retry_successes==1
    assert r.cs_publish_failures==1 and r.status=='cs_retry_recovered'
    k.async_set_heater_state.assert_not_awaited()

async def test_nov6_unconfirmed_start_cs_timeout_stops_at_original_lease(ready):
    """6 Nov 18:11:52 CET: failure1, pending ON, last ACK still OFF.

    HTTP's conservative lower bound preceded the command by ~42ms, so the
    observed On/41 could not ACK it. 15.227s publish failed at CS age49.661;
    next observed cycle age56.418 exceeded55. No invented broker ACK/retry.
    """
    c,r,k,clock,values=await confirmed_keepalive(ready)
    use_55_degree_test_limits(ready)
    r.pending=(True,41,1200)
    r.last_ack={'on':False,'water':10,'at':ts(1130)}
    r.last_cs_refresh=1200
    for key,value in values.items():
        r.telemetry.fields[key]=(3,value,1199.95845)
        r.telemetry.http_seen[key]=1199.95845
    r.poll.return_value={key:dict(value=v,epoch=4) for key,v in values.items()}
    clock[0]=1234.4331183
    async def fail_keepalive(target):
        if target>10:
            clock[0]=1249.660524
            raise TimeoutError('observed 15.227s CS attempt')
    k.async_set_control_setpoint.side_effect=fail_keepalive
    await c.async_control_heating_loop()
    assert r.status=='write_retry_holding_heat'
    assert r.cs_publish_failures==1 and r.cs_retry_attempts==0
    assert r.pending==(True,41,1200) and not r.last_ack['on']
    assert r.cs_write_seconds==pytest.approx(15.2274057)
    assert r.cs_write_budget==15 and r.last_cs_refresh==1200
    k.async_set_heater_state.assert_not_awaited()
    clock[0]=1256.418152
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    assert r.pending[0] is False and r.last_cs_refresh is None
    assert r.status=='input_or_transport_error:CS keepalive deadline exceeded'
    assert [call.args[0] for call in k.async_set_control_setpoint.await_args_list]==[41,10]


async def test_nov6_confirmed_heat_single_retry_fits_remaining_lease(ready):
    """6 Nov 18:17:30 CET: confirmed ON; retry budget3.707/duration2.525.

    Retry overwrote the failed attempt's timings in the live attributes, so
    the first attempt start below is a simulation, not an observed timestamp.
    Original55s lease and pending ACK deadline remain fixed throughout.
    """
    c,r,k,clock,values=await confirmed_keepalive(ready)
    use_55_degree_test_limits(ready)
    r.pending=(True,42.69,1200)
    ack=dict(r.last_ack)
    clock[0]=1237
    # Fresh CH but CS41 does not confirm pending42.69.
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    calls=[]
    async def publish(target):
        calls.append(target)
        if len(calls)==1:
            clock[0]=1251.2927144
            raise TimeoutError('failed first attempt; duration not archived')
        clock[0]+=2.525488009
    k.async_set_control_setpoint.side_effect=publish
    await c.async_control_heating_loop()
    assert calls==[42.69,42.69]
    assert r.cs_write_budget==pytest.approx(3.7072856)
    assert r.cs_write_seconds==pytest.approx(2.525488009)
    assert r.cs_retry_attempts==r.cs_retry_successes==1
    assert r.cs_publish_failures==1 and r.status=='cs_retry_recovered'
    assert r.pending==(True,42.69,1200) and r.last_ack==ack
    assert r.last_cs_refresh==pytest.approx(1251.2927144)
    k.async_set_heater_state.assert_not_awaited()


async def test_cs_retry_failure_stops_at_original_deadline(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1236
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    attempts=[]
    async def publish(target):
        if target==10:return
        attempts.append(clock[0]);clock[0]=1248 if len(attempts)==1 else 1255
        raise TimeoutError('No ACK')
    k.async_set_control_setpoint.side_effect=publish
    await c.async_control_heating_loop()
    assert len(attempts)==2 and r.cs_retry_successes==0
    assert r.last_cs_refresh is None
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)


async def dhw_tick(ready, at, **values):
    """Advance original field epochs; a test tick alone is not an OTGW ACK."""
    c,r,k,clock=ready
    clock[0]=at
    for field in r.poll.return_value.values():field['epoch']+=1
    for key,value in values.items():r.poll.return_value[key]['value']=value
    await c.async_control_heating_loop()


def use_55_degree_test_limits(ready):
    from dataclasses import replace
    c,r,k,clock=ready
    k._options={**k._options,'maximum_setpoint':55}
    r.policy=replace(r.policy,maximum_water=55)
    r.engine.policy=r.policy
    r.ramp.maximum=55
    assert r.attributes['ally_operating_limits']['normal_water_guard']==65


@pytest.mark.parametrize('failure',['cs','ch_on','read'])
async def test_morning_dhw_transport_timeout_keeps_original_return_window(ready,failure):
    """23 Oct 07:49:26 -> 07:58:56 -> failed return; not a thermal forecast.

    1200 maps to 07:49:26 CEST. 72.4/72.9 are the archived flow samples at
    07:58:56/07:59:26. Later successful writes/temperature are counterfactual.
    """
    c,r,k,clock=ready
    use_55_degree_test_limits(ready)
    for at in range(1200,1770,30):
        await dhw_tick(ready,at,dhwmode='On',boilertemperature=75)
    if failure in ('cs','ch_on'):
        async def fail(value):
            if (failure=='cs' and value!=10) or (failure=='ch_on' and value==DeviceState.ON):
                clock[0]+=15.535
                raise TimeoutError('23 Oct return publish timeout')
        target=k.async_set_control_setpoint if failure=='cs' else k.async_set_heater_state
        target.side_effect=fail
    await dhw_tick(ready,1770,dhwmode='Off',boilertemperature=72.4)
    if failure=='read':
        r.poll.side_effect=TimeoutError('temporary return telemetry outage')
        clock[0]=1785
        await c.async_control_heating_loop()
    assert r.dhw_recovery.ended==1770
    assert r.dhw_recovery.seen_dhw and not r.dhw_recovery.active
    assert r.pending is not None and r.pending[0] is False
    failed_stop_at=r.pending[2]
    initial_started=r.dhw_recovery.started
    if failure=='ch_on':assert initial_started==1770
    k.async_set_control_setpoint.side_effect=None
    k.async_set_heater_state.side_effect=None
    r.poll.side_effect=None
    # Original OFF confirmation and 180s restart delay still apply. There is
    # no cached ACK, repeated HEAT, synthetic new HR, or raised water ceiling.
    for at in range(1800,2011,15):
        # With a possibly delivered ON, the original cooling timer must keep
        # running. Successful cooling below is simulated, not observed history.
        water=72.9 if initial_started is None else 72.4-max(0,(at-1800)/120)
        await dhw_tick(ready,at,chenable='Off',controlsetpoint=10,boilertemperature=water)
        if r.pending is not None and r.pending[0]:break
    assert r.pending[0] is True and r.dhw_recovery.active
    assert r.dhw_recovery.ended==1770 and clock[0]<1770+900
    assert r.last_ack['on'] is False
    assert r.pending[2]>=failed_stop_at+180 and r.cs_retry_attempts==0
    assert r.c._setpoint<=55
    assert all(call.args[0]<=55 for call in k.async_set_control_setpoint.await_args_list)
    if initial_started is not None:assert r.dhw_recovery.started==initial_started
    assert r.reports.heads['a']['pi']['observed_at']==ts(1200)
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1200)


@pytest.mark.parametrize('reason',[
    'input_or_transport_error:DHW priority',
    'input_or_transport_error:Boiler water outside experimental guard',
    'input_or_transport_error:DHW residual water at or above ceiling'])
async def test_same_passive_water_block_reuses_fresh_confirmed_off(ready,reason):
    c,r,k,clock=ready
    await r.stop(reason)
    r.telemetry.observe(r.poll.return_value,1201)
    for field in r.poll.return_value.values():field['epoch']+=1
    r.telemetry.observe(r.poll.return_value,1210)
    r.confirm(1210)
    ack=dict(r.last_ack)
    for at in range(1230,1831,30):
        clock[0]=at
        for field in r.poll.return_value.values():field['epoch']+=1
        r.telemetry.observe(r.poll.return_value,at)
        await r.stop(reason)
    assert r.pending is None and r.last_ack==ack
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_once_with(10)
    assert r.last_cs_refresh is None


@pytest.mark.parametrize('unsafe',[
    'stale','ch_on','high_cs','fault','pending_on','no_ack','tripped','operator','unknown_block'])
async def test_passive_block_dedup_never_hides_first_or_unsafe_stop(ready,unsafe):
    c,r,k,clock=ready
    reason='input_or_transport_error:DHW priority'
    r.last_stop_reason=reason
    r.last_ack={'on':False,'water':10,'at':ts(1190)}
    if unsafe=='stale':clock[0]=1300
    if unsafe=='ch_on':r.telemetry.fields['chenable']=(1,'On',1190)
    if unsafe=='high_cs':r.telemetry.fields['controlsetpoint']=(1,41,1190)
    if unsafe=='fault':r.telemetry.fields['faultindicator']=(1,'On',1190)
    if unsafe=='pending_on':r.sent(True,41)
    if unsafe=='no_ack':r.last_ack=None
    if unsafe=='tripped':r.tripped='Boiler fault'
    if unsafe=='operator':reason='operator_off'
    if unsafe=='unknown_block':reason='input_or_transport_error:unreviewed_condition'
    await r.stop(reason)
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_once_with(10)
    assert r.pending[0] is False


async def test_dhw_priority_and_unproven_hot_water_do_not_repeat_safe_stop(ready):
    c,r,k,clock=ready
    before=dict(r.command_writes)  # The real HA fixture already performed boot OFFs.
    for at in range(1200,1801,30):
        await dhw_tick(ready,at,dhwmode='On',boilertemperature=75)
    assert r.command_writes['ch_off']==before['ch_off']+1
    # A lost context must not bypass the normal guard, but a confirmed stop
    # must not turn into a fresh OFF transaction every control cycle either.
    r.dhw_recovery.reset()
    for at in range(1830,2431,30):
        await dhw_tick(ready,at,dhwmode='Off',boilertemperature=72.9)
    assert r.command_writes['ch_off']==before['ch_off']+1
    assert r.command_writes['cs']==before['cs']+1
    assert r.command_writes['ch_on']==before['ch_on']
    assert r.last_ack['on'] is False and r.pending is None


@pytest.mark.parametrize('guard',['window','cooling','rise','ceiling'])
async def test_transport_pause_does_not_renew_dhw_safety_limits(ready,guard):
    c,r,k,clock=ready
    await dhw_tick(ready,1200,dhwmode='On',boilertemperature=75)
    await dhw_tick(ready,1230,dhwmode='Off',boilertemperature=72.4)
    assert r.dhw_recovery.started==1230
    r.poll.side_effect=TimeoutError('temporary read failure')
    clock[0]=1260
    await c.async_control_heating_loop()
    assert r.dhw_recovery.ended==r.dhw_recovery.started==1230
    r.poll.side_effect=None
    g=r.dhw_recovery
    assert g.initial==g.lowest==72.4
    at,water,expected={
        'window':(2130,70,'expired'), 'cooling':(1410,72.4,'not cooling'),
        'rise':(1350,74.5,'rising'), 'ceiling':(1350,80,'ceiling|rising')}[guard]
    # Exercise the original bounds directly so an unrelated pending ACK or
    # lack of simulated boiler response cannot make this safety test pass.
    with pytest.raises(ValueError,match=expected):g.check('Off',water,at,60)
    assert not g.active


@pytest.mark.parametrize('veto',['fault','stale_hr','permission','reconnect','unknown_dhw','operator'])
async def test_paused_dhw_context_cannot_bypass_new_safety_veto(ready,veto):
    c,r,k,clock=ready
    await dhw_tick(ready,1200,dhwmode='On',boilertemperature=75)
    async def fail(target):
        if target!=10:raise TimeoutError('return CS failed')
    k.async_set_control_setpoint.side_effect=fail
    await dhw_tick(ready,1230,dhwmode='Off',boilertemperature=72.4)
    assert r.dhw_recovery.ended==1230
    k.async_set_control_setpoint.side_effect=None
    if veto=='fault':r.poll.return_value['faultindicator']['value']='On'
    if veto=='stale_hr':r.reports.heads['a']['heat_required']['observed_at']=ts(1)
    if veto=='permission':c.hass.states.async_set('input_boolean.heat_allowed','off')
    if veto=='reconnect':r.connection_changed(False)
    if veto=='unknown_dhw':r.poll.return_value['dhwmode']['value']='unknown'
    if veto=='operator':await c.async_set_hvac_mode(HVACMode.OFF)
    for at in range(1260,1501,30):
        await dhw_tick(ready,at,chenable='Off',controlsetpoint=10)
    assert not any(call.args==(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)
    assert r.pending is None or not r.pending[0]


@pytest.mark.parametrize('case',['no_end','deadline','after_deadline','reversal'])
async def test_transport_pause_cannot_create_or_reage_a_dhw_episode(ready,case):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError,match='priority'):g.check('On',75,100,65)
    if case!='no_end':
        g.check('Off',72.4,130,65)
        g.heating_sent(72.4,130)
    at={'no_end':140,'deadline':1030,'after_deadline':1040,'reversal':129}[case]
    g.transport_error(at)
    assert not g.active and not g.seen_dhw
    assert g.ended is None and g.started is None
    with pytest.raises(ValueError,match='experimental guard'):g.check('Off',70,1050,65)

@pytest.mark.parametrize('veto',['off','permission','hr','fault','dhw'])
async def test_no_cs_retry_when_veto_arrives_during_failed_write(ready,veto):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1230
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    async def failed(target):
        if target==10:return
        if veto=='off':c._hvac_mode=HVACMode.OFF
        elif veto=='permission':c.hass.states.async_set('input_boolean.heat_allowed','off')
        elif veto=='hr':r.reports.accept(event(seq=2,at=1230,heat_required=dict(value=False,valid=True)),1230)
        else:
            values['faultindicator' if veto=='fault' else 'dhwmode']='On'
            r.telemetry.observe({key:dict(value=v,epoch=6) for key,v in values.items()},1230)
        raise TimeoutError()
    k.async_set_control_setpoint.side_effect=failed
    await c.async_control_heating_loop()
    assert r.cs_retry_attempts==0
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

async def test_cs_retry_preserves_new_target_pending_deadline(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1230;r.ramp.target=Mock(return_value=42)
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    k.async_set_control_setpoint.side_effect=[TimeoutError(),None]
    await c.async_control_heating_loop()
    assert r.pending==(True,42,1230) and r.last_ack['water']==41
    assert r.cs_retry_successes==1
    k.async_set_heater_state.assert_not_awaited()


async def test_historical_1624_cycle_has_earlier_refresh_reserve(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    # Last refresh16:23:26; successful cycle16:23:40 saw age15.025.
    clock[0]=1215.025
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    await c.async_control_heating_loop()
    assert r.last_cs_refresh==1215.025
    # Next recorded cycle was much later. One failed publish still leaves
    # time for a single retry before the unchanged lease deadline.
    clock[0]=1241.116
    attempts=[]
    async def publish(target):
        attempts.append(clock[0])
        if len(attempts)==1:
            clock[0]+=13.884
            raise TimeoutError('historical delayed ACK')
        clock[0]+=1
    k.async_set_control_setpoint.side_effect=publish
    r.poll.return_value={key:dict(value=v,epoch=6) for key,v in values.items()}
    await c.async_control_heating_loop()
    assert r.cs_retry_successes==1 and len(attempts)==2
    k.async_set_heater_state.assert_not_awaited()


@pytest.mark.parametrize('maximum,margin,back_max', [(55,10,50),(60,5,55),(60,10,55)])
async def test_unified_water_and_correction_form_is_atomic_and_reversible(ready,maximum,margin,back_max):
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    from dataclasses import asdict
    from copy import deepcopy
    c,r,k,clock=ready
    entry=SimpleNamespace(entry_id=c._config_entry.entry_id,
        data={**c._config_entry.data,'mode':'mqtt_opentherm'},
        options={**c._config_entry.options,'hodko_room_backup':{'target':22,'options':{'maximum_setpoint':50}}})
    original=deepcopy(entry.options)
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    form=await flow.async_step_operating_limits()
    assert len(form['data_schema'].schema)==18
    values={**asdict(runtime.OperatingLimits()),'maximum_water':maximum,'water_margin':margin,'sustained_delay':900,'sustained_max':4}
    result=await flow.async_step_operating_limits(values)
    assert result['step_id']=='control_mode_confirm'
    assert entry.options==original and k.maximum_setpoint==50
    k.async_set_control_setpoint.assert_not_awaited()
    k.async_set_heater_state.assert_not_awaited()
    flow._read_mode_monitor=AsyncMock(return_value={'chenable':{'epoch':1,'value':'Off'},'controlsetpoint':{'epoch':1,'value':10}})
    waiting=await flow.async_step_control_mode_confirm({})
    assert waiting['errors']['base']=='waiting_for_stop' and entry.options==original
    flow._read_mode_monitor.return_value={'chenable':{'epoch':2,'value':'Off'},'controlsetpoint':{'epoch':2,'value':10}}
    result=await flow.async_step_control_mode_confirm({})
    saved=result['data']
    assert saved['maximum_setpoint']==saved['hodko_ally']['policy']['maximum_water']==maximum
    assert saved['hodko_room_backup']==original['hodko_room_backup']
    assert saved['hodko_ally']['members']==SETTINGS['members']
    assert saved['hodko_ally']['policy']['sustained_delay']==900
    assert saved['hodko_ally']['policy']['sustained_max']==4
    assert c.hvac_mode==HVACMode.OFF
    # Only validation of a proposed cap may bypass the old live cap.
    with pytest.raises(ValueError):runtime.Runtime(c,saved['hodko_ally'])
    candidate=runtime.Runtime(c,saved['hodko_ally'],validation_cap=maximum)
    assert candidate.policy.maximum_water==maximum
    assert candidate.limits.water_margin==margin
    # Reopening with saved settings preserves correction and supports returning50.
    back=SatOptionsFlowHandler(SimpleNamespace(entry_id=entry.entry_id,data=entry.data,options=saved));back.hass=c.hass
    result=await back.async_step_operating_limits({**asdict(runtime.OperatingLimits()),'maximum_water':back_max})
    assert result['step_id']=='control_mode_confirm'
    assert back._mode_target['maximum_setpoint']==back_max
    assert back._mode_target['hodko_ally']['policy']['sustained_delay']==900

@pytest.mark.parametrize('change',[
    {'maximum_water':61},{'maximum_water':60.01},{'maximum_water':True},{'maximum_water':float('nan')},
    {'maximum_water':60,'water_margin':20,'dhw_ceiling':80},
    {'maximum_water':60,'water_margin':10,'dhw_ceiling':70},
    {'maximum_water':55,'water_margin':20,'dhw_ceiling':75},
    {'sustained_start':65,'sustained_release':65},{'sustained_delay':0},
    {'sustained_rise':2},{'sustained_max':11},{'sustained_pi_age':float('inf')},
    {'sustained_gap':91},{'sustained_start':True}])
async def test_unified_limits_invalid_form_never_stops_heating(ready,change):
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    from dataclasses import asdict
    c,r,k,clock=ready
    entry=SimpleNamespace(entry_id=c._config_entry.entry_id,data={**c._config_entry.data,'mode':'mqtt_opentherm'},options=dict(c._config_entry.options))
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    result=await flow.async_step_operating_limits({**asdict(runtime.OperatingLimits()),'maximum_water':50,**change})
    assert result['errors']['base']=='invalid_operating_limits'
    k.async_set_heater_state.assert_not_awaited()
    k.async_set_control_setpoint.assert_not_awaited()


async def test_expanded_form_preserves_saved_55_and_margin_without_writes(ready):
    from copy import deepcopy
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    c,r,k,clock=ready
    options=deepcopy(dict(c._config_entry.options))
    options['maximum_setpoint']=55
    options['hodko_ally']['policy']['maximum_water']=55
    entry=SimpleNamespace(entry_id=c._config_entry.entry_id,
        data={**c._config_entry.data,'mode':'mqtt_opentherm'},options=options)
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    form=await flow.async_step_operating_limits()
    fields={key.schema:(key.default(),value) for key,value in form['data_schema'].schema.items()}
    assert fields['maximum_water'][0]==55 and fields['maximum_water'][1].config['max']==60
    assert fields['water_margin'][0]==10
    assert entry.options==options
    k.async_set_heater_state.assert_not_awaited()
    k.async_set_control_setpoint.assert_not_awaited()


@pytest.mark.parametrize('margin,water,allowed',[(5,64.9,True),(5,65,False),(10,69.9,True),(10,70,False)])
async def test_60_water_guard_and_shadow_use_same_boundary(ready,margin,water,allowed):
    from dataclasses import replace
    from custom_components.sat.ally.shadow import Observer
    c,r,k,clock=ready
    k._options={**k._options,'maximum_setpoint':60}
    r.policy=replace(r.policy,maximum_water=60)
    r.engine.policy=r.policy;r.ramp.maximum=60
    r.limits=runtime.OperatingLimits(water_margin=margin)
    r.dhw_recovery=runtime.DHWRecovery(r.limits)
    o=Observer(SETTINGS['members'],SETTINGS['enabled_entity'],1000,r.policy,r.limits)
    o.accept(event(at=1200,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=100,valid=True)),1200)
    o.telemetry=r.telemetry
    r.poll.return_value['boilertemperature']['value']=water
    result=o.evaluate({'input_boolean.heat_allowed':'on','binary_sensor.a_eligible':'on'},r.poll.return_value,1200,1200)
    await c.async_control_heating_loop()
    assert (result['status']=='heat') is allowed
    k.async_set_heater_state.assert_awaited_with(DeviceState.ON if allowed else DeviceState.OFF)
    assert r.attributes['ally_operating_limits']['normal_water_guard']==60+margin


@pytest.mark.parametrize('margin', [5,10])
async def test_60_dhw_exception_retains_original_deadlines(ready,margin):
    limits=runtime.OperatingLimits(water_margin=margin)
    limits.validate_water(60)
    g=runtime.DHWRecovery(limits);guard=60+margin
    with pytest.raises(ValueError,match='experimental guard'):g.check('Off',72,0,guard)
    with pytest.raises(ValueError,match='priority'):g.check('On',75,0,guard)
    g.check('Off',74,30,guard);g.heating_sent(74,30)
    g.transport_error(60)
    assert g.ended==g.started==30
    g.check('Off',73,210,guard)
    assert g.active
    with pytest.raises(ValueError,match='expired'):g.check('Off',72,930,guard)
    g=runtime.DHWRecovery(limits)
    with pytest.raises(ValueError,match='priority'):g.check('On',80,0,guard)
    with pytest.raises(ValueError,match='ceiling'):g.check('Off',80,30,guard)


@pytest.mark.parametrize('maximum', [55,60])
async def test_coordinator_sends_saved_maximum_only_to_otgw_sh(ready,maximum):
    from custom_components.sat.compat.mqtt.opentherm import SatOpenThermMqttCoordinator
    c,r,k,clock=ready
    obj=object.__new__(SatOpenThermMqttCoordinator)
    obj._options={'maximum_setpoint':maximum};obj._heating_system='radiators'
    obj._publish_command=AsyncMock()
    from custom_components.sat.compat.coordinator import SatDataUpdateCoordinator
    await SatDataUpdateCoordinator.async_added_to_hass(obj)
    obj._publish_command.assert_awaited_once_with(f'SH={float(maximum)}')


@pytest.mark.parametrize('maximum', [55,60])
async def test_active_request_and_ramp_respect_selected_cap(ready,maximum):
    from dataclasses import replace
    c,r,k,clock,values=await confirmed_keepalive(ready)
    k._options={**k._options,'maximum_setpoint':maximum}
    r.policy=replace(r.policy,maximum_water=maximum,water_cold=maximum)
    r.engine.policy=r.policy;r.ramp.maximum=maximum
    del r.ramp.target  # Restore the real ramp after keepalive fixture setup.
    r.ramp.on=True;r.ramp.water=maximum-1;r.ramp.last=1200
    values['outsidetemperature']=-10
    clock[0]=1230
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    await c.async_control_heating_loop()
    k.async_set_control_setpoint.assert_awaited_with(maximum)
    k.async_set_heater_state.assert_not_awaited()
    assert r.pending[:2]==(True,maximum)

# 22 Oct 19:04–19:12 .36: PM25 timeout during shutdown, then repeated
# confirmed OFF/CS10 while waiting for MM and the 180s minimum OFF time.
@pytest.mark.parametrize('path', ['runtime', 'shutdown'])
async def test_pihr_safe_stop_never_queries_pm25(ready,path):
    from custom_components.sat.compat.mqtt.opentherm import SatOpenThermMqttCoordinator
    c,r,k,clock=ready
    obj=object.__new__(SatOpenThermMqttCoordinator)
    async def publish(payload):
        if payload=='PM=25':raise TimeoutError('historical optional query timeout')
    obj._publish_command=AsyncMock(side_effect=publish)
    k.async_set_control_setpoint_only=obj.async_set_control_setpoint_only
    k.async_set_control_setpoint=obj.async_set_control_setpoint
    if path=='runtime':await r.stop('safe_stop')
    else:await c._async_shutdown()
    assert [x.args[0] for x in obj._publish_command.await_args_list]==['CS=10']
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

@pytest.mark.parametrize('reason',['waiting_for_modulation_confirmation','off_or_restart_delay'])
async def test_confirmed_off_wait_does_not_republish_or_reset_ack(ready,reason):
    c,r,k,clock=ready
    await r.stop('initial_off')
    clock[0]=1210
    r.telemetry.observe(r.poll.return_value,1201)
    for v in r.poll.return_value.values():v['epoch']+=1
    r.telemetry.observe(r.poll.return_value,1210);r.confirm(1210)
    assert r.pending is None
    ack=dict(r.last_ack)
    for t in (1220,1240,1260):
        clock[0]=t
        await r.stop(reason)
    assert k.async_set_heater_state.await_count==1
    assert k.async_set_control_setpoint.await_count==1
    assert r.pending is None and r.last_ack==ack

@pytest.mark.parametrize('unsafe',['stale','ch_on','high_cs','fault','pending_on','no_ack','tripped','operator'])
async def test_off_wait_suppression_never_hides_unsafe_or_manual_stop(ready,unsafe):
    c,r,k,clock=ready
    r.last_ack={'on':False,'water':10,'at':ts(1190)}
    if unsafe=='stale':clock[0]=1300
    if unsafe=='ch_on':r.telemetry.fields['chenable']=(1,'On',1190)
    if unsafe=='high_cs':r.telemetry.fields['controlsetpoint']=(1,41,1190)
    if unsafe=='fault':r.telemetry.fields['faultindicator']=(1,'On',1190)
    if unsafe=='pending_on':r.sent(True,41)
    if unsafe=='no_ack':r.last_ack=None
    if unsafe=='tripped':r.tripped='Boiler fault'
    await r.stop('operator_off' if unsafe=='operator' else 'off_or_restart_delay')
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_once_with(10)
    assert r.pending[0] is False

async def test_replay36_modulation_and_restart_wait_use_one_off_transaction(ready):
    c,r,k,clock=ready
    r.modulation_confirmed=False;r.ramp.off_at=1200
    await r.stop('initial_off')
    for t in range(1220,1381,20):
        clock[0]=t
        for v in r.poll.return_value.values():v['epoch']=t
        await r.cycle()
    assert r.modulation_confirmed and r.pending[0] is True
    assert [v.args[0] for v in k.async_set_heater_state.await_args_list]==[DeviceState.OFF,DeviceState.ON]
    assert [v.args[0] for v in k.async_set_control_setpoint.await_args_list]==[10,41]
    # Fresh exact ON evidence, not PUBACK, completes the transition.
    clock[0]=1390
    for v in r.poll.return_value.values():v['epoch']=1390
    r.poll.return_value['chenable']['value']='On'
    r.poll.return_value['controlsetpoint']['value']=41
    await r.cycle()
    clock[0]=1400
    for v in r.poll.return_value.values():v['epoch']=1400
    await r.cycle()
    assert r.last_ack['on'] is True and r.tripped is None

async def test_reconnect_partial_event_then_snapshot_restores_demand_without_epoch_error(ready,monkeypatch):
    from tests.test_ally_mode import source_snapshot
    import json
    c,r,k,clock=ready
    monkeypatch.setattr(runtime.mqtt,'is_connected',lambda h:True)
    first=event(at=1200,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=100,valid=True))
    clock[0]=1210;r.connection_changed(False)
    clock[0]=1211;r.connection_changed(True)
    partial=event(seq=2,at=1220,pi_heating_demand=dict(value=99,valid=True))
    clock[0]=1220;r.message(SimpleNamespace(payload=json.dumps(partial),retain=False))
    values=r.poll.return_value
    r.telemetry.observe(values,1219)
    for v in values.values():v['epoch']+=1
    r.telemetry.observe(values,1220)
    r.refresh_demand();assert r.decision.heat is None
    # The intervening unknown evaluation must not prevent a valid snapshot repair.
    clock[0]=1221
    r.snapshot_message(SimpleNamespace(payload=json.dumps(source_snapshot([first,partial],1221)),retain=False))
    r.refresh_demand();assert r.decision.heat is True
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1200)
    assert r.pending is None and r.last_ack is None and not r.modulation_confirmed
    k.async_set_heater_state.assert_not_awaited()
    k.async_set_control_setpoint.assert_not_awaited()
    c.hass.states.async_set('binary_sensor.a_eligible','off')
    r.refresh_demand();assert r.decision.heat is False


@pytest.mark.parametrize('age',[43.8,51.0])
async def test_late_cs_publish_reserves_one_retry_inside_original_lease(ready,monkeypatch,age):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1200+age
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    ack=dict(r.last_ack); budgets=[];active=[];attempts=[]
    class Budget:
        def __init__(self,seconds):self.seconds=seconds
        async def __aenter__(self):active.append(self.seconds)
        async def __aexit__(self,*args):active.pop()
    monkeypatch.setattr(runtime.asyncio,'timeout',Budget)
    async def publish(target):
        if target==10:return
        attempts.append(clock[0]);budgets.append(active[-1])
        if len(attempts)==1:
            clock[0]+=active[-1]
            raise TimeoutError('No ACK until first attempt deadline')
        clock[0]+=0.5
    k.async_set_control_setpoint.side_effect=publish
    await c.async_control_heating_loop()
    assert len(attempts)==2
    assert budgets[0]==pytest.approx((55-age)-min(5,(55-age)/2))
    assert budgets[1]==pytest.approx(1255-attempts[1])
    assert clock[0]<1255 and r.last_cs_refresh==attempts[1]
    assert r.cs_retry_successes==1 and r.last_ack==ack and r.pending is None
    k.async_set_heater_state.assert_not_awaited()

async def test_late_cs_failed_retry_still_expires_original_lease(ready,monkeypatch):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    clock[0]=1244
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    active=[];attempts=[]
    class Budget:
        def __init__(self,seconds):self.seconds=seconds
        async def __aenter__(self):active.append(self.seconds)
        async def __aexit__(self,*args):active.pop()
    monkeypatch.setattr(runtime.asyncio,'timeout',Budget)
    async def publish(target):
        if target==10:return
        attempts.append(clock[0]);clock[0]+=active[-1]
        raise TimeoutError('No ACK')
    k.async_set_control_setpoint.side_effect=publish
    await c.async_control_heating_loop()
    assert attempts==[1244,1250] and clock[0]==1255
    assert r.cs_retry_attempts==1 and r.cs_retry_successes==0
    assert r.last_cs_refresh is None
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)


async def test_due_cs_tick_bypasses_debounce_and_cancels_old_job(ready,monkeypatch):
    from custom_components.sat.compat import climate as cm
    c,r,k,clock,values=await confirmed_keepalive(ready)
    c.schedule_control_heating_loop=type(c).schedule_control_heating_loop.__get__(c)
    jobs=[];cancel=Mock()
    monkeypatch.setattr(cm,'async_call_later',lambda h,delay,job:jobs.append(job) or cancel)
    c._control_heating_loop_unsub=None
    clock[0]=1211;r.control_tick()
    assert len(jobs)==1  # Ordinary non-due work may still debounce.
    clock[0]=1220
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    ack=dict(r.last_ack)
    r.control_tick()
    await c.hass.async_block_till_done()
    cancel.assert_called_once()
    assert len(jobs)==1 and c._control_heating_loop_unsub is None
    k.async_set_control_setpoint.assert_awaited_once_with(41)
    k.async_set_heater_state.assert_not_awaited()
    assert r.pending is None and r.last_ack==ack and r.last_cs_refresh==1220
    assert r.cycle_gap_seconds==14  # Previous completed cycle was at1206.


async def test_registered_periodic_tick_wakes_due_cs_without_report(ready):
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    try:
        from pytest_homeassistant_custom_component.common import async_fire_time_changed
    except ModuleNotFoundError as exc:
        if exc.name != 'pytest_homeassistant_custom_component': raise
        from pytest_homeassistant.common import async_fire_time_changed
    c,r,k,clock,values=await confirmed_keepalive(ready)
    c.schedule_control_heating_loop=Mock()
    clock[0]=1220
    async_fire_time_changed(c.hass,dt_util.utcnow()+timedelta(seconds=11))
    await c.hass.async_block_till_done()
    assert any(call.kwargs.get('force') is True
               for call in c.schedule_control_heating_loop.call_args_list)
    k.async_set_control_setpoint.assert_not_awaited()  # Callback schedules, never writes.


async def test_due_tick_does_not_queue_behind_writer_or_after_close(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    c.schedule_control_heating_loop=Mock();clock[0]=1220
    async with c._control_lock:
        for _ in range(5):r.control_tick()
    c.schedule_control_heating_loop.assert_not_called()
    r.control_tick();c.schedule_control_heating_loop.assert_called_once_with(force=True)
    c.schedule_control_heating_loop.reset_mock();r.closed=True;r.control_tick()
    r.closed=False;c._control_closing=True;r.control_tick()
    c.schedule_control_heating_loop.assert_not_called()
    k.async_set_heater_state.assert_not_awaited()


@pytest.mark.parametrize('veto',['off','permission','hr','expired_hr','dhw','fault','hot_water','expired_ack','expired_cs'])
async def test_due_cs_tick_still_runs_all_safety_checks(ready,veto):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    c.schedule_control_heating_loop=type(c).schedule_control_heating_loop.__get__(c)
    r.ramp.target=type(r.ramp).target.__get__(r.ramp)
    clock[0]=1220
    if veto=='off':c._hvac_mode=HVACMode.OFF
    elif veto=='permission':c.hass.states.async_set('input_boolean.heat_allowed','off')
    elif veto=='hr':r.reports.heads['a']['heat_required']['value']=False
    elif veto=='expired_hr':r.reports.heads['a']['heat_required']['observed_at']=ts(1-3900)
    elif veto=='dhw':values['dhwmode']='On'
    elif veto=='fault':values['faultindicator']='On'
    elif veto=='hot_water':values['boilertemperature']=60
    elif veto=='expired_ack':r.pending=(True,42,1130)
    elif veto=='expired_cs':clock[0]=1255
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    r.control_tick();await c.hass.async_block_till_done()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    assert all(x.args==(10,) for x in k.async_set_control_setpoint.await_args_list)
    assert r.cs_keepalive_writes==0


@pytest.mark.parametrize('wake',['periodic','already_late'])
async def test_oct23_1604_delay_replay_with_original_cs_deadline(ready,monkeypatch,wake):
    """Observed relative times; successful earlier retry is counterfactual.

    Last CS start16:02:55.188; late cycle+44.685s, failed read+0.404s,
    CS await14.185s despite ~4.955s assigned. No captured packet proves
    why HA resumed late. Reproduce that delay, never assume a new PUBACK.
    """
    c,r,k,clock,values=await confirmed_keepalive(ready)
    use_55_degree_test_limits(ready)
    r.ramp.target=Mock(return_value=51.61)
    r.last_ack['water']=51.61;c._setpoint=51.61
    values.update(controlsetpoint=51.62,boilertemperature=51.7)
    r.telemetry.observe({key:dict(value=v,epoch=8) for key,v in values.items()},1206)
    ack=dict(r.last_ack);fields=dict(r.telemetry.fields)
    attempts=[];budgets=[]
    async def failed_read():
        clock[0]+=.404
        raise TimeoutError('observed read failure')
    async def delayed_publish(target):
        if target==10:return
        attempts.append(clock[0]);budgets.append(r.cs_write_budget)
        if len(attempts)==1:
            clock[0]+=14.185
            raise TimeoutError('observed late completion/cancellation')
        clock[0]+=1  # Simulated timely retry; not observed live.
    r.poll.side_effect=failed_read
    k.async_set_control_setpoint.side_effect=delayed_publish
    if wake=='periodic':
        c.schedule_control_heating_loop=type(c).schedule_control_heating_loop.__get__(c)
        clock[0]=1220;r.control_tick();await c.hass.async_block_till_done()
        assert attempts==pytest.approx([1220.404,1234.589])
        assert budgets==[15,15]
        assert r.last_cs_refresh==pytest.approx(1234.589)
        assert r.cs_retry_attempts==r.cs_retry_successes==1
        assert r.status=='cs_retry_recovered' and r.last_ack==ack and r.pending is None
        assert r.telemetry.fields==fields  # Neither read failure nor CS renews evidence.
        k.async_set_heater_state.assert_not_awaited()
    else:
        clock[0]=1244.685;await r.cycle()
        assert attempts==pytest.approx([1245.089])
        assert budgets==pytest.approx([4.9555])
        assert r.cs_write_overrun==pytest.approx(9.2295)
        assert r.cs_retry_attempts==0 and r.last_cs_refresh is None
        k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
        assert r.pending[:2]==(False,10)
    assert r.cs_publish_failures==1


async def test_early_tick_failed_retry_still_stops_at_55s(ready):
    c,r,k,clock,values=await confirmed_keepalive(ready)
    c.schedule_control_heating_loop=type(c).schedule_control_heating_loop.__get__(c)
    attempts=[]
    async def no_ack(target):
        if target==10:return
        attempts.append(clock[0]);clock[0]=1235 if len(attempts)==1 else 1255
        raise TimeoutError('unavailable broker; late second cancellation')
    k.async_set_control_setpoint.side_effect=no_ack
    r.poll.return_value={key:dict(value=v,epoch=5) for key,v in values.items()}
    clock[0]=1220;r.control_tick();await c.hass.async_block_till_done()
    assert attempts==[1220,1235] and r.cs_retry_attempts==1
    assert r.cs_retry_successes==0 and r.last_cs_refresh is None
    assert r.cs_write_overrun==5
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)

# hodko.43: observed 26/28 Oct post-DHW transition, replayed without network.
# Times below are relative; source: docs/gas-dhw-analysis-2026-10-28.md.
async def test_dhw_waiting_before_ch_does_not_consume_mixing_test(ready):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError,match='priority'):g.check('On',74,0,65)
    g.check('Off',72.2,10,65)
    g.check('Off',75.3,119,65)  # 28 Oct: pre-circulation growth was rejected.
    assert g.active and g.phase=='waiting_for_heat'
    assert g.started is None and g.ended==10 and g.lowest is None
    assert g.counts==dict(episodes=1,starts=0,completed=0,blocked=0)
    g.check('Off',79.9,800,65)
    with pytest.raises(ValueError,match='expired'):g.check('Off',79,910,65)
    assert g.counts['blocked']==1 and g.last_result=='window_expired'
    with pytest.raises(ValueError,match='guard'):g.check('Off',70,930,65)
    assert g.counts['blocked']==1  # repeated stops are not new failed episodes


async def test_dhw_below_normal_guard_retains_proof_for_observed_hot_pocket(ready):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError,match='priority'):g.check('On',74,0,65)
    g.check('Off',64.6,10,65);g.heating_sent(64.6,10)
    # 26 Oct: first CH ON -> 70.2 C after 18 s -> 34.2 C after 50 s.
    g.check('Off',70.2,28,65)
    assert g.phase=='mixing' and g.active and g.ended==10
    g.check('Off',34.2,60,65)
    assert g.active  # do not discard episode on the first cold pocket either
    g.check('Off',34,70,65)
    assert not g.active and not g.seen_dhw
    assert g.counts==dict(episodes=1,starts=1,completed=1,blocked=0)
    assert g.last_peak==70.2 and g.last_duration==60 and g.last_result=='cooled'
    # No second unbounded exception after completion.
    with pytest.raises(ValueError,match='guard'):g.check('Off',70,80,65)


@pytest.mark.parametrize('water',[80,82.2,90])
async def test_mixing_never_bypasses_absolute_water_ceiling(ready, water):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError):g.check('On',75,0,65)
    g.check('Off',72,10,65);g.heating_sent(72,10)
    with pytest.raises(ValueError,match='ceiling'):g.check('Off',water,11,65)
    assert not g.active and not g.seen_dhw and g.counts['blocked']==1


async def test_ceiling_wait_before_start_does_not_extend_window(ready):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError):g.check('On',75,0,65)
    with pytest.raises(ValueError,match='ceiling'):g.check('Off',82.2,10,65)
    assert g.phase=='ceiling_wait' and g.ended==10 and g.started is None
    assert g.diagnostics(10,65)['effective_ceiling']==80
    g.check('Off',79.9,500,65);g.heating_sent(79.9,500)
    assert g.ended==10 and g.started==500
    g.check('Off',79.5,560,65)
    with pytest.raises(ValueError,match='expired'):g.check('Off',70,910,65)


@pytest.mark.parametrize('fault', ['rise','not_cooling'])
async def test_after_mixing_original_relative_and_cooling_guards_apply(ready, fault):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError):g.check('On',75,0,65)
    g.check('Off',72,10,65);g.heating_sent(72,10)
    g.check('Off',74.5,40,65)  # tolerated only during mixing
    at,water,reason=(70,74.5,'rising') if fault=='rise' else (190,72,'not cooling')
    with pytest.raises(ValueError,match=reason):g.check('Off',water,at,65)
    assert g.last_duration==at-10 and g.counts['blocked']==1


async def test_mixing_timeout_retry_cannot_restart_either_timer(ready):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError):g.check('On',75,0,65)
    g.check('Off',72,10,65);g.heating_sent(72,10)
    g.transport_error(30)
    assert not g.active
    g.check('Off',74,50,65);g.heating_sent(74,50)
    assert g.ended==g.started==10 and g.counts['starts']==1
    with pytest.raises(ValueError,match='rising'):g.check('Off',74.5,71,65)


@pytest.mark.parametrize('mixing,cooling',[(True,180),(-1,180),(121,180),(61,60),(float('nan'),180)])
async def test_mixing_parameter_rejects_invalid_bounds(ready, mixing,cooling):
    with pytest.raises(ValueError):runtime.OperatingLimits(mixing_time=mixing,cooling_time=cooling)


async def test_real_runtime_dhw_return_mixes_without_extra_off_or_on(ready):
    c,r,k,clock=ready
    use_55_degree_test_limits(ready)
    await dhw_tick(ready,1200,dhwmode='On',boilertemperature=75)
    # Fresh OFF acknowledgement before the first return, demand originally 1200.
    await dhw_tick(ready,1230,dhwmode='Off',boilertemperature=64.6)
    assert r.pending[0] is True and r.dhw_recovery.started==1230
    before=dict(r.command_writes)
    await dhw_tick(ready,1248,chenable='On',controlsetpoint=c._setpoint,boilertemperature=70.2)
    assert r.dhw_recovery.phase=='mixing' and not r.tripped
    await dhw_tick(ready,1280,chenable='On',controlsetpoint=c._setpoint,boilertemperature=34.2)
    await dhw_tick(ready,1290,chenable='On',controlsetpoint=c._setpoint,boilertemperature=34)
    assert r.command_writes['ch_on']==before['ch_on']
    assert r.command_writes['ch_off']==before['ch_off']
    assert r.dhw_recovery.last_result=='cooled'
    assert r.dhw_recovery.last_duration==60
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1200)
    assert all(call.args[0]<=55 for call in k.async_set_control_setpoint.await_args_list)


async def test_unknown_demand_preserves_dhw_context_but_never_forces_ch(ready):
    c,r,k,clock=ready
    use_55_degree_test_limits(ready)
    r.reports.heads['a']['heat_required']['observed_at']=ts(1)
    await dhw_tick(ready,1200,dhwmode='On',boilertemperature=75)
    await dhw_tick(ready,1230,dhwmode='Off',boilertemperature=72.2)
    await dhw_tick(ready,1260,chenable='Off',controlsetpoint=10,boilertemperature=75.3)
    assert r.dhw_recovery.ended==1230 and r.dhw_recovery.started is None
    assert not any(call.args==(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)
    r.reports.accept(event(seq=2,at=1270,heat_required=dict(value=True,valid=True)),1270)
    for at in range(1290,1501,30):
        await dhw_tick(ready,at,chenable='Off',controlsetpoint=10,boilertemperature=74)
        if r.dhw_recovery.started is not None:break
    assert r.dhw_recovery.ended==1230 and r.dhw_recovery.started is not None
    assert r.reports.heads['a']['heat_required']['observed_at']==ts(1270)
    assert r.dhw_recovery.counts['starts']==1


async def test_mixing_form_exposes_default_and_preserves_saved_limits(ready):
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    c,r,k,clock=ready
    entry=SimpleNamespace(entry_id=c._config_entry.entry_id,data={**c._config_entry.data,'mode':'mqtt_opentherm'},options=dict(c._config_entry.options))
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    form=await flow.async_step_operating_limits()
    fields={key.schema:(key.default(),value) for key,value in form['data_schema'].schema.items()}
    assert fields['mixing_time'][0]==60 and fields['mixing_time'][1].config['max']==120
    assert fields['dhw_ceiling'][0]==80 and fields['maximum_water'][0]==50
    k.async_set_heater_state.assert_not_awaited()


async def test_normal_range_completion_is_not_reported_as_measured_cooling(ready):
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError):g.check('On',75,0,65)
    g.check('Off',40,10,65);g.heating_sent(40,10)
    g.check('Off',40,70,65)
    assert g.last_result=='normal_range' and g.counts['completed']==1
    assert not g.active


async def test_late_return_after_original_window_gets_no_hidden_extension(ready):
    """Actual 26 Oct late CH was beyond 900s; do not claim a full thermal replay."""
    g=runtime.DHWRecovery()
    with pytest.raises(ValueError):g.check('On',75,0,65)
    g.check('Off',72,10,65)
    with pytest.raises(ValueError,match='expired'):g.check('Off',75,910,65)
    g.check('Off',64.6,2681,65);g.heating_sent(64.6,2681)
    assert g.started is None and not g.active
    with pytest.raises(ValueError,match='guard'):g.check('Off',70.2,2699,65)

@pytest.mark.parametrize('kind',['foreign','bad_member','explicit_invalid','bad_snapshot'])
async def test_one_head_cannot_reset_start_or_confirmed_heat(ready,monkeypatch,kind):
    import json
    from copy import deepcopy
    from tests.test_ally_mode import source_snapshot
    c,r,k,clock=ready
    monkeypatch.setattr(runtime.mqtt,'is_connected',lambda h:True)
    if kind!='foreign':
        r.settings['members']['0x0000000000000002']=dict(name='b',zone='B',eligible_entity='binary_sensor.b_eligible')
        r.reports.mapping['0x0000000000000002']='b';r.engine.inventory['b']='B'
        c.hass.states.async_set('binary_sensor.b_eligible','on')
    first=event(at=1200,pi_heating_demand=dict(value=100,valid=True),heat_required=dict(value=True,valid=True))
    source=[first];data=r.poll.return_value;proof=deepcopy(r.reports.heads['a'])
    for i in range(12):
        clock[0]=1200+i*30
        e=event(seq=i+2,at=clock[0]);e['device_ieee']='0x0000000000000002'
        if kind=='bad_member':e['fields']['pi_heating_demand']['value']=999
        if kind=='explicit_invalid':e['fields']['pi_heating_demand']=dict(value=None,valid=False)
        source.append(e)
        ack=deepcopy(r.last_ack);pending=r.pending;ramp=(r.ramp.on,r.ramp.off_at,r.ramp.water)
        if kind=='bad_snapshot':
            snapshot=source_snapshot(source,clock[0]);snapshot['heads'][e['device_ieee']]['pi_heating_demand']['value']=999
            r.snapshot_message(SimpleNamespace(payload=json.dumps(snapshot),retain=False))
        else:r.message(SimpleNamespace(payload=json.dumps(e),retain=False))
        assert r.last_ack==ack and r.pending==pending
        assert (r.ramp.on,r.ramp.off_at,r.ramp.water)==ramp
        assert r.reports.heads['a']==proof
        for v in data.values():v['epoch']+=1
        data['chenable']['value']='On' if r.ramp.on else 'Off'
        data['controlsetpoint']['value']=c._setpoint if r.ramp.on else 10
        await c.async_control_heating_loop()
        assert r.status in ('heat','degraded','waiting_for_on_confirmation') and r.tripped is None
    assert all(x.args==(DeviceState.ON,) for x in k.async_set_heater_state.await_args_list)
    assert 41<c._setpoint<=50 and r.last_ack is not None
    # The one remaining healthy room's window still dominates its own demand.
    c.hass.states.async_set('binary_sensor.a_eligible','off')
    clock[0]+=30
    for v in data.values():v['epoch']+=1
    await c.async_control_heating_loop()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)

@pytest.mark.parametrize('known',[False,True])
async def test_isolated_report_does_not_reset_witnessed_dhw_return_window(ready,known):
    import json
    from copy import deepcopy
    c,r,k,clock=ready
    if known:r.reports.mapping['0x0000000000000002']='b'
    with pytest.raises(ValueError):r.dhw_recovery.check('On',75,1190,65)
    r.dhw_recovery.check('Off',72,1199,65)
    proof=deepcopy(vars(r.dhw_recovery))
    bad=event(seq=2,at=1200);bad['device_ieee']='0x0000000000000002';bad['fields']={}
    r.message(SimpleNamespace(payload=json.dumps(bad),retain=False))
    assert vars(r.dhw_recovery)==proof and r.dhw_recovery.ended==1199
    k.async_set_heater_state.assert_not_awaited()


# hodko.45: the passive reason is not the identity of a safe OFF transaction.
@pytest.mark.parametrize('destination', ['mqtt', 'http'])
async def test_transport_change_and_rollback_keep_55_limits_and_owner(ready,monkeypatch,destination):
    import json
    from copy import deepcopy
    from dataclasses import asdict
    from custom_components.sat.compat.config_flow import SatOptionsFlowHandler
    from custom_components.sat.ally import mode_flow,mqtt_monitor
    c,r,k,clock=ready
    use_55_degree_test_limits(ready)
    monkeypatch.setattr(mode_flow,'monotonic',lambda:clock[0])
    k._get_topic_for_subscription=lambda leaf:'OTGW/value/right/'+leaf
    http={**deepcopy(SETTINGS),'policy':asdict(r.policy),'operating_limits':asdict(r.limits)}
    mqtt={**deepcopy(http),'telemetry_transport':'mqtt','otgw_mqtt_topic':'OTGW/value/right'}
    before,proposed=(http,mqtt) if destination=='mqtt' else (mqtt,http)
    original=c._config_entry
    options={**original.options,'maximum_setpoint':55,'hodko_ally':before,
             'hodko_room_backup':{'options':{'maximum_setpoint':50},'target':22},
             'unrelated_option':'keep'}
    entry=SimpleNamespace(entry_id=original.entry_id,
        data={**original.data,'mode':'mqtt_opentherm'},options=deepcopy(options))
    c.hass.data['sat'][entry.entry_id]['climate']=c
    flow=SatOptionsFlowHandler(entry);flow.hass=c.hass
    result=await flow.async_step_control_mode({'control_mode':'ally_demand','settings':json.dumps(proposed)})
    assert result['step_id']=='control_mode_confirm'
    k.async_set_heater_state.assert_not_awaited()
    def proof(epoch,at):
        if destination=='mqtt':return mqtt_monitor.MqttSnapshot({
            'chenable':(epoch,'Off',at),'controlsetpoint':(epoch,10,at)})
        return {'chenable':{'epoch':epoch,'value':'Off'},
                'controlsetpoint':{'epoch':epoch,'value':10}}
    flow._read_mode_monitor=AsyncMock(return_value=proof(1,1199))
    result=await flow.async_step_control_mode_confirm({})
    assert result['errors']['base']=='waiting_for_stop'
    clock[0]=1230
    flow._read_mode_monitor.return_value=proof(2,1225)
    result=await flow.async_step_control_mode_confirm({})
    assert result['type']=='create_entry' and result['data']=={**options,'hodko_ally':proposed}
    assert entry.options==options  # Flow result, not a hidden direct settings write.
    assert c.hvac_mode==HVACMode.OFF and c._hodko_transition_pending
    assert result['data']['maximum_setpoint']==proposed['policy']['maximum_water']==55
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)


@pytest.mark.parametrize('transport', ['http','mqtt'])
@pytest.mark.parametrize('previous', [
    'off_or_restart_delay', 'waiting_for_modulation_confirmation',
    'input_or_transport_error:TimeoutError',
    *sorted(runtime.PASSIVE_WATER_STOPS)])
@pytest.mark.parametrize('reason', sorted(runtime.PASSIVE_WATER_STOPS))
async def test_changed_passive_reason_keeps_confirmed_off(ready, previous, reason, transport):
    c,r,k,clock=ready
    await r.stop(previous)
    # New live evidence after the first stop, not a restored/cached ACK.
    r.telemetry.observe(r.poll.return_value,1201)
    clock[0]=1210
    for v in r.poll.return_value.values():v['epoch']+=1
    if transport=='mqtt':
        from custom_components.sat.ally.mqtt_monitor import MqttSnapshot
        r.telemetry.observe(MqttSnapshot({key:(1,row['value'],1209)
            for key,row in r.poll.return_value.items()}),1210)
    else:r.telemetry.observe(r.poll.return_value,1210)
    r.confirm(1210)
    ack=dict(r.last_ack);off_at=r.ramp.off_at
    clock[0]=1220
    await r.stop(reason)
    assert r.status==reason and r.pending is None and r.last_ack==ack
    assert r.last_stop_reason==previous  # Last actual command, not latest status.
    assert r.last_cs_refresh is None and r.ramp.off_at==off_at
    assert c._setpoint==10 and not r.ramp.on
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_once_with(10)


@pytest.mark.parametrize('unsafe', ['stale','missing_receipt','pending_on','no_ack',
    'ch_on','high_cs','fault','tripped','operator','unknown_reason'])
async def test_changed_reason_cannot_hide_unsafe_or_explicit_stop(ready,unsafe):
    c,r,k,clock=ready
    r.last_stop_reason='off_or_restart_delay'
    r.last_ack={'on':False,'water':10,'at':ts(1190)}
    reason='input_or_transport_error:DHW priority'
    if unsafe=='stale':clock[0]=1300
    if unsafe=='missing_receipt':r.telemetry.fields['chenable']=(1,'Off',None)
    if unsafe=='pending_on':r.sent(True,41)
    if unsafe=='no_ack':r.last_ack=None
    if unsafe=='ch_on':r.telemetry.fields['chenable']=(1,'On',1190)
    if unsafe=='high_cs':r.telemetry.fields['controlsetpoint']=(1,41,1190)
    if unsafe=='fault':r.telemetry.fields['faultindicator']=(1,'On',1190)
    if unsafe=='tripped':r.tripped='Boiler fault'
    if unsafe=='operator':reason='operator_off'
    if unsafe=='unknown_reason':reason='input_or_transport_error:unreviewed_condition'
    await r.stop(reason)
    assert r.pending[0] is False
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    k.async_set_control_setpoint.assert_awaited_once_with(10)


async def test_reason_changes_keep_original_pending_off_deadline(ready):
    c,r,k,clock=ready
    await r.stop('input_or_transport_error:TimeoutError')
    since=r.pending[2]
    for t,reason in [(1220,'input_or_transport_error:DHW priority'),
                     (1230,'input_or_transport_error:Boiler water outside experimental guard'),
                     (1260,'off_or_restart_delay'),
                     (1290,'input_or_transport_error:DHW priority'),
                     (1320,'off_or_restart_delay')]:
        clock[0]=t
        await r.stop(reason)
        assert r.pending[2]==since and r.last_ack is None
    assert k.async_set_heater_state.await_count==3  # t=1200,1230,1260 only.
    with pytest.raises(ValueError,match='command_not_confirmed'):r.confirm(clock[0])


async def test_replay_oct29_night_reason_switch_and_http_proof_loss(ready):
    """Observed sequence; reduced time origin, no synthetic physical response claim.

    22:33:11 OFF, confirmed 22:33:39; 22:38:13 HTTP receipt=None.
    The latter must still force a stop: changing the reason is not its cause.
    """
    c,r,k,clock=ready
    await r.stop('off_or_restart_delay')
    clock[0]=1228
    for key,row in r.poll.return_value.items():
        r.telemetry.fields[key]=(3,row['value'],1227)
    r.confirm(1228)
    for t in (1245,1275,1305,1335,1365,1395,1425,1455,1485):
        clock[0]=t
        for key,row in r.poll.return_value.items():r.telemetry.fields[key]=(4,row['value'],t-5)
        await r.stop('off_or_restart_delay')
    assert k.async_set_heater_state.await_count==1
    clock[0]=1502
    for key in ('chenable','controlsetpoint','maxrelmodlvl'):
        seq,value,_=r.telemetry.fields[key];r.telemetry.fields[key]=(seq,value,None)
    await r.stop('input_or_transport_error:Stale telemetry: chenable')
    assert k.async_set_heater_state.await_count==2
    assert r.pending[2]==1502 and r.last_ack is None


async def test_mqtt_shared_runtime_shadow_off_heat_dhw_and_no_http(ready,monkeypatch):
    """Real HA control loop + passive adapter, simulated boiler state feedback.

    Verifies transport/ownership, not a prediction of physical boiler response.
    """
    from copy import deepcopy
    from dataclasses import asdict
    from custom_components.sat.ally import mqtt_monitor,shadow_adapter
    c,r,k,clock=ready
    use_55_degree_test_limits(ready)
    namespace='OTGW/value/offline-test'
    settings={**deepcopy(SETTINGS),'telemetry_transport':'mqtt','otgw_mqtt_topic':namespace,
              'policy':asdict(r.policy),'operating_limits':asdict(r.limits)}
    r.settings=settings;r.mqtt_namespace=namespace;r.telemetry=runtime.Telemetry()
    r.poll=runtime.Runtime.poll.__get__(r)
    r.modulation_confirmed=False
    monkeypatch.setattr(mqtt_monitor,'monotonic',lambda:clock[0])
    monkeypatch.setattr(shadow_adapter,'time',lambda:clock[0])
    monkeypatch.setattr(shadow_adapter,'monotonic',lambda:clock[0])
    monkeypatch.setattr(mqtt_monitor.mqtt,'is_connected',lambda h:True)
    pub=AsyncMock(side_effect=AssertionError('Only fake SAT coordinator may write'))
    monkeypatch.setattr(mqtt_monitor.mqtt,'async_publish',pub)
    def forbidden(*args):raise AssertionError('MQTT transport must not use HTTP')
    monkeypatch.setattr(runtime,'shared_reader',forbidden)
    monkeypatch.setattr(shadow_adapter,'shared_reader',forbidden)
    a=shadow_adapter.Adapter(c.hass,settings);a.connected=True
    a.observer.reports.accept(event(at=1200,heat_required=dict(value=True,valid=True),
                                   pi_heating_demand=dict(value=100,valid=True)),1200)
    reader=mqtt_monitor.shared_mqtt_reader(c.hass,namespace)
    await reader.start()
    data={'ch_enable':'OFF','TSet':'10','MaxRelModLevelSetting':'100',
          'fault':'OFF','domestichotwater':'OFF','Tboiler':'40','Toutside':'5'}
    def feed(at,**values):
        clock[0]=at;data.update(values)
        for leaf,payload in data.items():reader.message(SimpleNamespace(
            topic=namespace+'/'+leaf,payload=str(payload),retain=False))
    async def tick(at,**values):
        feed(at,**values);clock[0]=at+1
        await r.cycle();await a.refresh()
    # MQTT bootstrap ignores retained. There is no old HTTP fallback.
    reader.message(SimpleNamespace(topic=namespace+'/TSet',payload='10',retain=True))
    assert not await reader.read()
    await tick(1201)
    assert r.pending and not r.pending[0] and not r.modulation_confirmed
    await tick(1216)
    assert r.modulation_confirmed and r.pending[0]
    assert 41<=c._setpoint<=55 and a.attributes['writes'] is False
    await tick(1231,ch_enable='ON',TSet=c._setpoint)
    assert r.last_ack['on'] and r.tripped is None
    assert r.decision.zones==a.observer.result['zones']
    original=deepcopy(r.reports.heads)
    # A passive DHW priority change stops ON exactly once, then waits safely.
    await tick(1246,domestichotwater='ON',Tboiler='70')
    off_count=r.command_writes['ch_off']
    await tick(1261,ch_enable='OFF',TSet='0')
    assert r.last_ack['on'] is False
    await tick(1276)
    assert r.command_writes['ch_off']==off_count
    assert a.observer.result['status']=='unknown'  # passive shadow has no DHW return exception
    await tick(1291,domestichotwater='OFF',Tboiler='72')
    for at in range(1306,1517,15):
        await tick(at,ch_enable='ON' if r.ramp.on else 'OFF',
                   TSet=c._setpoint if r.ramp.on else 0,
                   Tboiler='70' if at<1450 else '68' if at<1480 else '60')
    assert r.dhw_recovery.counts['starts']==1
    assert r.dhw_recovery.counts['completed']==1
    assert r.last_ack['on'] and r.tripped is None
    assert r.decision.zones==a.observer.result['zones']
    assert r.reports.heads==original
    assert all(call.args[0]<=55 for call in k.async_set_control_setpoint.await_args_list)
    # A missing modulation report cannot be replaced by the current power or
    # an event echo. All remaining fields are fresh; active mode still stops.
    clock[0]=1521
    old=reader.evidence.fields['maxrelmodlvl']
    reader.evidence.fields['maxrelmodlvl']=(old[0],old[1],clock[0]-91)
    for leaf in data:
        if leaf!='MaxRelModLevelSetting':reader.message(SimpleNamespace(
            topic=namespace+'/'+leaf,payload=str(data[leaf]),retain=False))
    await r.cycle()
    k.async_set_heater_state.assert_awaited_with(DeviceState.OFF)
    assert r.modulation_confirmed is False
    pub.assert_not_called()
    reader.close();await a.stop()

@pytest.mark.parametrize('failed_command', ['ch', 'cs', 'both'])
@pytest.mark.parametrize('error_kind', ['disconnected', 'connection_lost', 'timeout'])
async def test_mqtt_recovery_requires_new_complete_stop_and_new_ack(ready, failed_command, error_kind):
    c,r,k,clock=ready
    def failure():
        if error_kind == 'timeout':
            return TimeoutError()
        code = runtime.MQTT_ERR_NO_CONN if error_kind == 'disconnected' else runtime.MQTT_ERR_CONN_LOST
        return runtime.HomeAssistantError(translation_domain='mqtt', translation_key='mqtt_broker_error',
            translation_placeholders={'error_message': runtime.error_string(code)})
    r.tripped='command_not_confirmed';r.pending=(False,10,1000)
    values={key:v['value'] for key,v in r.poll.return_value.items()}
    def sample(receipt, epoch):
        r.poll.return_value=runtime.MqttSnapshot({key:(epoch,v,receipt) for key,v in values.items()})
    if failed_command in ('ch','both'):k.async_set_heater_state.side_effect=failure()
    if failed_command in ('cs','both'):k.async_set_control_setpoint.side_effect=failure()
    sample(1199,2)
    await r.cycle()
    assert r.tripped=='command_not_confirmed'
    assert r.stop_publish_pending and r.last_ack is None
    assert r.command_recovery_attempts==1 and r.pending==(False,10,1200)
    assert k.async_set_heater_state.await_count==1
    assert k.async_set_control_setpoint.await_count==1
    # Even fresh safe telemetry cannot acknowledge a partial/failed publish.
    clock[0]=1220;sample(1219,3)
    await r.cycle()
    assert r.pending==(False,10,1200) and r.last_ack is None
    assert r.tripped=='command_not_confirmed'
    k.async_set_heater_state.side_effect=None
    k.async_set_control_setpoint.side_effect=None
    clock[0]=1291;sample(1290,4)
    await r.cycle()
    assert r.pending==(False,10,1291) and r.command_recovery_attempts==2
    assert not r.stop_publish_pending and r.last_ack is None
    assert k.async_set_heater_state.await_count==2
    # Cached data, or only one new field, cannot confirm the new stop.
    clock[0]=1292
    await r.cycle()
    assert r.last_ack is None
    r.poll.return_value=runtime.MqttSnapshot({key:(5,v,1292 if key!='controlsetpoint' else 1290)
                                            for key,v in values.items()})
    clock[0]=1293
    await r.cycle()
    assert r.last_ack is None
    clock[0]=1294;sample(1293,6)
    await r.cycle()
    assert r.tripped is None and r.pending is None and r.last_ack['on'] is False
    assert not r.modulation_confirmed
    assert all(call.args==(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)

async def test_repeated_mqtt_disconnect_exhausts_recovery_budget(ready):
    c,r,k,clock=ready
    r.tripped='command_not_confirmed';r.pending=(False,10,1000)
    k.async_set_heater_state.side_effect=runtime.HomeAssistantError(
        translation_domain='mqtt', translation_key='mqtt_broker_error',
        translation_placeholders={'error_message':runtime.error_string(runtime.MQTT_ERR_NO_CONN)})
    values={key:v['value'] for key,v in r.poll.return_value.items()}
    for i in range(8):
        clock[0]=1200+100*i
        r.poll.return_value=runtime.MqttSnapshot({key:(i+2,v,clock[0]-1) for key,v in values.items()})
        await r.cycle()
    assert r.tripped=='command_recovery_exhausted'
    assert r.command_recovery_attempts==3
    assert k.async_set_heater_state.await_count==3
    assert r.last_ack is None

@pytest.mark.parametrize('exc', [
    runtime.HomeAssistantError(translation_domain='mqtt', translation_key='mqtt_broker_error',
                              translation_placeholders={'error_message':'Connection Refused: not authorised.'}),
    runtime.HomeAssistantError('The client is not currently connected.'),
    ValueError('invalid payload'),
])
async def test_mqtt_recovery_does_not_reclassify_nontransport_failures(ready, exc):
    c,r,k,clock=ready
    r.tripped='command_not_confirmed';r.pending=(False,10,1000)
    k.async_set_heater_state.side_effect=exc
    values={key:v['value'] for key,v in r.poll.return_value.items()}
    r.poll.return_value=runtime.MqttSnapshot({key:(2,v,1199) for key,v in values.items()})
    await r.cycle()
    assert r.tripped==('invalid payload' if isinstance(exc,ValueError) else 'command_recovery_failed')
    assert r.last_ack is None and r.stop_publish_pending
    before=k.async_set_heater_state.await_count
    clock[0]=1300
    await r.cycle()
    assert k.async_set_heater_state.await_count==before

async def test_stop_ack_must_follow_completion_of_both_publishes(ready):
    c,r,k,clock=ready
    async def slow_cs(value):clock[0]=1205
    k.async_set_control_setpoint.side_effect=slow_cs
    await r.stop('test_stop')
    assert r.pending==(False,10,1200)
    for receipt in (1201,1204):
        for key,value in [('chenable','Off'),('controlsetpoint',10)]:
            r.telemetry.fields[key]=(2,value,receipt)
        r.confirm(1206)
        assert r.pending is not None and r.last_ack is None
    for key,value in [('chenable','Off'),('controlsetpoint',10)]:
        r.telemetry.fields[key]=(3,value,1206)
    r.confirm(1207)
    assert r.pending is None and r.last_ack['on'] is False

async def test_stop_does_not_mask_payload_error_with_later_disconnect(ready):
    c,r,k,clock=ready
    k.async_set_heater_state.side_effect=ValueError('invalid payload')
    k.async_set_control_setpoint.side_effect=runtime.HomeAssistantError(
        translation_domain='mqtt', translation_key='mqtt_broker_error',
        translation_placeholders={'error_message':runtime.error_string(runtime.MQTT_ERR_NO_CONN)})
    with pytest.raises(ValueError,match='invalid payload'):
        await r.stop('test_stop')
    assert r.stop_publish_pending and r.last_ack is None
    k.async_set_control_setpoint.assert_awaited_with(10)

@pytest.mark.parametrize('veto',['manual_off','global_off','boiler_fault'])
async def test_mqtt_reconnect_preserves_safety_veto(ready,veto):
    c,r,k,clock=ready
    r.tripped='command_not_confirmed';r.pending=(False,10,1000)
    k.async_set_heater_state.side_effect=runtime.HomeAssistantError(
        translation_domain='mqtt', translation_key='mqtt_broker_error',
        translation_placeholders={'error_message':runtime.error_string(runtime.MQTT_ERR_NO_CONN)})
    values={key:v['value'] for key,v in r.poll.return_value.items()}
    r.poll.return_value=runtime.MqttSnapshot({key:(2,v,1199) for key,v in values.items()})
    await r.cycle()
    assert r.stop_publish_pending
    k.async_set_heater_state.side_effect=None
    if veto=='manual_off':c._hvac_mode=HVACMode.OFF
    if veto=='global_off':c.hass.states.async_set('input_boolean.heat_allowed','off')
    if veto=='boiler_fault':values['faultindicator']='On'
    for i in range(4):
        clock[0]=1300+i*100
        r.poll.return_value=runtime.MqttSnapshot({key:(i+3,v,clock[0]-1) for key,v in values.items()})
        await r.cycle()
    assert r.last_ack is None and r.tripped is not None
    assert all(call.args==(DeviceState.OFF,) for call in k.async_set_heater_state.await_args_list)
    if veto!='boiler_fault':assert k.async_set_heater_state.await_count==1


async def exhausted_startup(ready, monkeypatch):
    """Recorded 7 Nov 08:57:10.987446Z: three attempts, expired partial OFF.

    Normalize that instant to monotonic1300. Source: bounded Influx fault/
    counter query and preflight snapshot in spook-ab-20261107. Future replies
    below are simulated inputs, not a claim of an earlier actual ignition.
    """
    c,r,k,clock=await boot_verifier(ready)
    use_55_degree_test_limits(ready)
    monkeypatch.setattr(runtime.mqtt,'is_connected',lambda hass:True)
    c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,'on')
    r.boot_verify_attempts=3
    r.stop_publish_pending=True
    clock[0]=1300
    r.telemetry=runtime.Telemetry()
    r.telemetry.observe({key:dict(epoch=1,value=row['value'])
                         for key,row in r.poll.return_value.items()},1290)
    await r.cycle()
    assert r.tripped=='startup_attempts_exhausted' and r.boot_verify_deadline is None
    k.async_set_heater_state.assert_not_awaited()
    return ready


async def startup_rearm_tick(ready, at, transport='http', **values):
    c,r,k,clock=ready
    clock[0]=at
    fields=dict(outsidetemperature=5,chenable='Off',controlsetpoint=10,
                boilertemperature=38.8,faultindicator='Off',dhwmode='Off',maxrelmodlvl=100)
    fields.update(values)
    r.poll.return_value=(runtime.MqttSnapshot({key:(int(at),value,at-1)
                         for key,value in fields.items()}) if transport=='mqtt' else
                         {key:dict(value=value,epoch=100000+int(at)) for key,value in fields.items()})
    await r.cycle()


@pytest.mark.parametrize('transport',['http','mqtt'])
async def test_nov7_startup_exhaustion_recovers_without_operator_reset(ready,monkeypatch,transport):
    from copy import deepcopy
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    original=deepcopy(r.reports.heads)
    deadline=r.startup_rearm_after
    for at in (1310,1400,1599):
        await startup_rearm_tick(ready,at,transport)
        assert r.startup_rearm_after==deadline
        k.async_set_heater_state.assert_not_awaited()
    # HTTP needs a new baseline after the gap. MQTT has original receipts.
    await startup_rearm_tick(ready,1600,transport)
    await startup_rearm_tick(ready,1610,transport)
    assert r.startup_rearm_attempts==1
    if transport=='http':
        assert r.pending==(False,10,1610) and r.tripped=='startup_attempts_exhausted'
    await startup_rearm_tick(ready,1620,transport)
    assert r.tripped is None and not r.modulation_confirmed
    assert r.last_ack['on'] is False and r.pending is None
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    assert clock[0]-r.ramp.off_at>=r.limits.restart_delay
    for at in range(1630,1731,10):
        await startup_rearm_tick(ready,at,transport,
            chenable='On' if r.ramp.on else 'Off',controlsetpoint=c._setpoint)
    assert r.modulation_confirmed
    assert any(call.args==(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)
    assert r.last_ack['on'] is True and r.tripped is None
    assert r.reports.heads==original
    assert all(call.args[0]<=55 for call in k.async_set_control_setpoint.await_args_list)
    assert r.boot_verify_attempts==3 and r.startup_rearm_attempts==1


@pytest.mark.parametrize('error', ['timeout','disconnect'])
async def test_startup_rearm_failed_publish_cannot_ack_or_storm(ready,monkeypatch,error):
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    exc=TimeoutError() if error=='timeout' else runtime.HomeAssistantError(
        translation_domain='mqtt',translation_key='mqtt_broker_error',
        translation_placeholders={'error_message':runtime.error_string(runtime.MQTT_ERR_NO_CONN)})
    k.async_set_heater_state.side_effect=exc
    for at in range(1600,2501,10):
        await startup_rearm_tick(ready,at,'mqtt')
        assert r.tripped=='startup_attempts_exhausted' and r.last_ack is None
    assert r.startup_rearm_attempts==4  # 1600,1900,2200,2500 only
    assert k.async_set_heater_state.await_count==4
    assert k.async_set_control_setpoint.await_count==4
    assert all(call.args==(10,) for call in k.async_set_control_setpoint.await_args_list)
    assert r.pending==(False,10,2500) and r.stop_publish_pending
    # Transport returning cannot confirm the incomplete fourth publish.
    k.async_set_heater_state.side_effect=None
    await startup_rearm_tick(ready,2510,'mqtt')
    assert r.tripped=='startup_attempts_exhausted' and r.last_ack is None
    await startup_rearm_tick(ready,2800,'mqtt')
    assert r.last_ack is None  # A report before the new failure barrier is insufficient.
    await startup_rearm_tick(ready,2810,'mqtt')
    await startup_rearm_tick(ready,2820,'mqtt')
    assert r.tripped is None and r.last_ack['on'] is False


@pytest.mark.parametrize('veto',['season_off','season_unknown','global_off','mqtt_off',
                               'manual_off','closed','ch_on','cs_high','fault','schema','auth'])
async def test_startup_rearm_respects_permission_and_safe_state(ready,monkeypatch,veto):
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    values={}
    if veto.startswith('season_'):c.hass.states.async_set(runtime.HEATING_SEASON_ENTITY,veto[7:])
    if veto=='global_off':c.hass.states.async_set('input_boolean.heat_allowed','off')
    if veto=='mqtt_off':monkeypatch.setattr(runtime.mqtt,'is_connected',lambda hass:False)
    if veto=='closed':r.closed=True
    if veto=='manual_off':
        await c.async_set_hvac_mode(HVACMode.OFF)
        k.async_set_heater_state.reset_mock();k.async_set_control_setpoint.reset_mock()
        assert r.startup_rearm_after is None
    if veto=='ch_on':values['chenable']='On'
    if veto=='cs_high':values['controlsetpoint']=40
    if veto=='fault':values['faultindicator']='On'
    if veto in ('schema','auth'):r.poll.side_effect=MonitorUnavailable(
        'OTGW monitor schema' if veto=='schema' else 'OTGW monitor HTTP 401')
    for at in (1600,1610,1620):await startup_rearm_tick(ready,at,'mqtt',**values)
    assert r.startup_rearm_attempts==0
    assert all(call.args!=(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)
    if veto not in ('manual_off',):k.async_set_heater_state.assert_not_awaited()
    if veto=='fault':assert r.tripped=='Boiler fault'


@pytest.mark.parametrize('kind',['cached','partial','late','slow_read'])
async def test_startup_rearm_never_accepts_cache_or_late_ack(ready,monkeypatch,kind):
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    await startup_rearm_tick(ready,1600,'mqtt')
    assert r.pending==(False,10,1600)
    raw=r.poll.return_value
    if kind=='partial':
        raw['chenable']=(1601,'Off',1601)  # CS still predates completed publish.
    if kind=='late':
        raw=runtime.MqttSnapshot({key:(1700,row[1],1699) for key,row in raw.items()})
    async def read():
        if kind=='slow_read':clock[0]=1690
        return raw
    r.poll.side_effect=read
    clock[0]=1690 if kind=='late' else 1689
    await r.cycle()
    assert r.tripped=='startup_attempts_exhausted' and r.last_ack is None
    assert r.pending==(False,10,1600) and r.startup_rearm_after==1900
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)


@pytest.mark.parametrize('veto',['dhw','hot','expired_hr','no_demand','room_blocked','rm'])
async def test_startup_rearm_off_ack_still_needs_normal_heat_gates(ready,monkeypatch,veto):
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    await startup_rearm_tick(ready,1600,'mqtt')
    await startup_rearm_tick(ready,1610,'mqtt')
    assert r.tripped is None and not r.modulation_confirmed
    values={}
    if veto=='dhw':values['dhwmode']='On'
    if veto=='hot':values['boilertemperature']=66
    if veto=='rm':values['maxrelmodlvl']=50
    if veto=='expired_hr':r.reports.heads['a']['heat_required']['observed_at']=ts(-3000)
    if veto=='no_demand':
        r.reports.heads['a']['heat_required']['value']=False
        r.reports.heads['a']['pi']['value']=0
    if veto=='room_blocked':c.hass.states.async_set('binary_sensor.a_eligible','off')
    for at in range(1620,1851,10):await startup_rearm_tick(ready,at,'mqtt',**values)
    assert all(call.args!=(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)


@pytest.mark.parametrize('during',['read','write'])
async def test_operator_off_cancels_startup_rearm_during_await(ready,monkeypatch,during):
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    entered,release=asyncio.Event(),asyncio.Event()
    async def pause(*args):
        entered.set();await release.wait()
        return r.poll.return_value
    target=r.poll if during=='read' else k.async_set_heater_state
    target.side_effect=pause
    task=asyncio.create_task(startup_rearm_tick(ready,1600,'mqtt'))
    await entered.wait()
    off=asyncio.create_task(c.async_set_hvac_mode(HVACMode.OFF))
    await asyncio.sleep(0)
    release.set();await task;await off
    assert c.hvac_mode==HVACMode.OFF and r.startup_rearm_after is None
    assert not r.startup_rearm_pending and r.tripped is None
    assert all(call.args!=(DeviceState.ON,) for call in k.async_set_heater_state.await_args_list)


async def test_nov7_recorded_safe_state_replays_past_latched_startup(ready,monkeypatch):
    """Actual fault and later safe samples; new command delivery is simulated.

    No raw MQTT publication/ACK trace covers the first three failures. Seed
    the observed exhausted state, not invented publication start times. The
    11:22 CET samples were read BEFORE the real manual reset at11:24–11:25;
    they are NOT historical acknowledgements of this counterfactual new OFF.
    """
    from datetime import datetime
    from copy import deepcopy
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    origin=datetime.fromisoformat('2026-11-07T08:57:10.987446+00:00').timestamp()
    def normalized(at):return 1300+datetime.fromisoformat(at).timestamp()-origin
    # PR2 original PI/HR from the archived11:21 preflight, anonymized as a.
    r.reports.heads['a']['pi']['observed_at']=ts(normalized('2026-11-07T10:14:21.937+00:00'))
    r.reports.heads['a']['heat_required']['observed_at']=ts(normalized('2026-11-07T09:27:23.417+00:00'))
    reports=deepcopy(r.reports.heads)
    fields=('outsidetemperature','chenable','controlsetpoint','boilertemperature',
            'faultindicator','dhwmode','maxrelmodlvl')
    values=(3.3,'Off',0.0,39.2,'Off','Off',100.0)
    for at,epochs in (
        ('2026-11-07T10:22:05.986536+00:00',(55734,55738,55740,55739,55738,55738,55736)),
        ('2026-11-07T10:22:16.054610+00:00',(55750,55745,55747,55746,55745,55745,55751)),
        ('2026-11-07T10:22:26.101249+00:00',(55750,55761,55756,55755,55761,55761,55752)),
    ):
        clock[0]=normalized(at)
        r.poll.return_value={key:dict(value=value,epoch=epoch)
                             for key,value,epoch in zip(fields,values,epochs)}
        await r.cycle()
    # Fails on .48: startup_attempts_exhausted remains latched forever.
    assert r.tripped is None and r.last_ack['on'] is False
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)
    at=clock[0]
    for delta in range(10,121,10):
        await startup_rearm_tick(ready,at+delta,
            chenable='On' if r.ramp.on else 'Off',controlsetpoint=c._setpoint)
    assert r.last_ack['on'] is True and r.tripped is None
    assert r.reports.heads==reports
    assert r.command_writes['ch_on']==1
    assert all(call.args[0]<=55 for call in k.async_set_control_setpoint.await_args_list)


@pytest.mark.parametrize('veto',['clock','payload','auth','session'])
async def test_startup_rearm_permanent_error_stays_latched(ready,monkeypatch,veto):
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    if veto=='clock':monkeypatch.setattr(runtime,'time',lambda:clock[0]+20)
    if veto=='session':r.poll.side_effect=ValueError('OTGW MQTT session changed')
    if veto=='payload':k.async_set_heater_state.side_effect=ValueError('invalid payload')
    if veto=='auth':k.async_set_heater_state.side_effect=runtime.HomeAssistantError(
        translation_domain='mqtt',translation_key='mqtt_broker_error',
        translation_placeholders={'error_message':'Connection Refused: not authorised.'})
    await startup_rearm_tick(ready,1600,'mqtt')
    assert r.tripped not in (None,'startup_attempts_exhausted')
    assert r.startup_rearm_after is None
    before=k.async_set_heater_state.await_count
    await startup_rearm_tick(ready,2000,'mqtt')
    assert k.async_set_heater_state.await_count==before and r.last_ack is None


async def test_startup_rearm_slow_publish_cannot_extend_off_deadline(ready,monkeypatch):
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    async def slow(value):clock[0]=1700
    k.async_set_control_setpoint.side_effect=slow
    await startup_rearm_tick(ready,1600,'mqtt')
    assert r.pending==(False,10,1600) and r.stop_confirmation_after==1700
    k.async_set_control_setpoint.side_effect=None
    await startup_rearm_tick(ready,1710,'mqtt')
    assert r.tripped=='startup_attempts_exhausted' and r.last_ack is None
    assert r.startup_rearm_after==1900 and not r.startup_rearm_pending
    k.async_set_heater_state.assert_awaited_once_with(DeviceState.OFF)


@pytest.mark.parametrize('proof',['stale','no_receipt','only_ch'])
async def test_startup_rearm_needs_fresh_safe_proof_before_new_publish(ready,monkeypatch,proof):
    c,r,k,clock=await exhausted_startup(ready,monkeypatch)
    fields={key:(2,row['value'],1299) for key,row in r.poll.return_value.items()}
    if proof=='no_receipt':fields={key:(row[0],row[1],None) for key,row in fields.items()}
    if proof=='only_ch':fields['chenable']=(3,'Off',1600)
    r.poll.return_value=runtime.MqttSnapshot(fields)
    clock[0]=1610
    await r.cycle()
    assert r.tripped=='startup_attempts_exhausted' and r.startup_rearm_attempts==0
    k.async_set_heater_state.assert_not_awaited()


async def test_counter_wrap_cache_cannot_ack_a_new_ch_command(ready):
    from custom_components.sat.ally.monitor import Snapshot
    c,r,k,clock=ready
    r.telemetry=runtime.Telemetry()
    def sample(epoch,at):
        return Snapshot({'chenable':dict(value='On',epoch=epoch),
                         'controlsetpoint':dict(value=41,epoch=epoch)},at,65535)
    r.telemetry.observe(sample(65525,1195),1195)
    r.telemetry.observe(sample(65530,1200),1200)
    r.sent(True,41)
    wrapped=sample(5,1210)
    r.telemetry.observe(wrapped,1210);r.confirm(1210)
    assert r.pending==(True,41,1200)  # Lower bound equals send time, not after it.
    r.telemetry.observe(wrapped,1220);r.confirm(1220)
    assert r.pending==(True,41,1200)
    r.telemetry.observe(sample(20,1225),1225);r.confirm(1225)
    assert r.pending is None
    assert r.telemetry.fields['chenable']==(20,'On',1210)
    k.async_set_heater_state.assert_not_awaited()
    k.async_set_control_setpoint.assert_not_awaited()
