import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from custom_components.sat.ally import shadow_adapter as module
from custom_components.sat.ally.runtime import utc
from custom_components.sat.ally.shadow_sensor import ShadowSensor
from custom_components.sat.config_flow import SatFlowHandler
from custom_components import sat

SETTINGS={'schema_version':1,'topic':'test/ally/events','monitor_url':'http://test.invalid/api/v2/otgw/otmonitor',
          'enabled_entity':'binary_sensor.global','members':{'0x0000000000000001':{'name':'a','zone':'A','eligible_entity':'binary_sensor.a'}},
          'policy':{'ttl':3900,'minimum_water':41,'maximum_water':50,'water_warm':41}}

def monitor(epoch):
    return {k:{'value':v,'epoch':epoch} for k,v in {'outsidetemperature':10,'chenable':'On','controlsetpoint':45,
        'boilertemperature':45,'faultindicator':'Off','dhwmode':'Off'}.items()}

def message(at=1001,retain=False):
    e={'schema_version':2,'session_id':'7777eadd-b5a6-45db-bd77-1943e604ee75','session_started_at':utc(900),
       'received_at':utc(at),'sequence':1,'device_ieee':'0x0000000000000001','message_type':'attributeReport',
       'fields':{'pi_heating_demand':{'value':99,'valid':True},'heat_required':{'value':True,'valid':True}}}
    return SimpleNamespace(payload=json.dumps(e),retain=retain)

@pytest.fixture
async def env(hass,monkeypatch):
    clock=[1000.0];unsub=Mock();publish=AsyncMock(side_effect=AssertionError('No MQTT writes'))
    monkeypatch.setattr(module,'time',lambda:clock[0]);monkeypatch.setattr(module,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.mqtt,'async_subscribe',AsyncMock(return_value=unsub))
    monkeypatch.setattr(module.mqtt,'is_connected',lambda h:True)
    monkeypatch.setattr(module.mqtt,'async_publish',publish)
    monkeypatch.setattr(type(hass.services),'async_call',AsyncMock(side_effect=AssertionError('No service calls')))
    hass.states.async_set('binary_sensor.global','on');hass.states.async_set('binary_sensor.a','on')
    a=module.Adapter(hass,SETTINGS);a.poll=AsyncMock(return_value=monitor(1000))
    yield a,clock,unsub,publish
    await a.stop()
    publish.assert_not_called();hass.services.async_call.assert_not_called()

async def test_start_reports_get_decision_sensors_and_stop(env):
    a,clock,unsub,publish=env
    await a.start();await a.task
    assert a.observer.result['status']=='unknown'
    clock[0]=1001;a.message(message())
    clock[0]=1002;a.poll.return_value=monitor(1002);await a.refresh()
    assert a.observer.result['status']=='heat'
    status=ShadowSensor(a,'entry');water=ShadowSensor(a,'entry',True)
    assert status.native_value=='heat' and 41<=water.native_value<=50
    assert status.extra_state_attributes['heads']['a']['pi_age_seconds']==1
    assert status.extra_state_attributes['writes'] is False
    await a.stop();assert a.closed;assert unsub.call_count==2
    calls=a.poll.await_count;a.schedule();assert a.poll.await_count==calls

async def test_disconnect_during_get_discards_result(env):
    a,clock,unsub,publish=env
    await a.start();await a.task
    entered=asyncio.Event();release=asyncio.Event()
    async def delayed():entered.set();await release.wait();return monitor(1002)
    a.poll.side_effect=delayed;a.schedule();await entered.wait()
    a.connection(False);release.set();await a.task
    assert a.observer.result['status']=='unknown'
    assert a.observer.result['reason']=='disconnected'
    a.message(message());assert not a.observer.reports.heads

async def test_stop_cancels_pending_get_and_unsubscribes(env):
    a,clock,unsub,publish=env
    await a.start();await a.task
    entered=asyncio.Event()
    async def blocked():entered.set();await asyncio.Event().wait()
    a.poll.side_effect=blocked;a.schedule();await entered.wait();await a.stop()
    assert a.task.done() and not a.unsubs

async def test_retained_invalid_and_http_failure(env):
    a,clock,unsub,publish=env
    await a.start();await a.task;clock[0]=1001
    a.message(message(retain=True));assert not a.observer.reports.heads
    a.message(SimpleNamespace(payload='bad',retain=False));assert a.observer.result['reason']=='invalid_report'
    a.poll.side_effect=TimeoutError();await a.refresh()
    assert a.observer.result['status']=='unknown'

async def test_entry_never_constructs_boiler_coordinator(hass,monkeypatch,env):
    a,clock,unsub,publish=env
    factory=Mock(side_effect=AssertionError('Shadow must not create coordinator'))
    monkeypatch.setattr(sat,'SatDataUpdateCoordinatorFactory',factory)
    monkeypatch.setattr(module,'Adapter',lambda *args:a)
    forward=AsyncMock();unload=AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries,'async_forward_entry_setups',forward)
    monkeypatch.setattr(hass.config_entries,'async_unload_platforms',unload)
    try:
        from pytest_homeassistant_custom_component.common import MockConfigEntry
    except ModuleNotFoundError as exc:
        if exc.name != 'pytest_homeassistant_custom_component': raise
        from pytest_homeassistant.common import MockConfigEntry
    e=MockConfigEntry(domain='sat',entry_id='shadow',data={'mode':'ally_shadow','hodko_shadow':SETTINGS})
    e.add_to_hass(hass)
    assert await sat.async_setup_entry(hass,e);await a.task
    forward.assert_awaited_once_with(e,['sensor'])
    assert await sat.async_unload_entry(hass,e)
    factory.assert_not_called();assert 'shadow' not in hass.data['sat']

async def test_config_flow_is_offline_and_creates_only_shadow(hass,monkeypatch):
    f=SatFlowHandler();f.hass=hass;f.context={'source':'user'}
    monkeypatch.setattr(f,'async_set_unique_id',AsyncMock())
    monkeypatch.setattr(f,'_abort_if_unique_id_configured',Mock())
    result=await f.async_step_ally_shadow({'settings':json.dumps(SETTINGS)})
    assert result['data']['mode']=='ally_shadow'
    assert result['data']['hodko_shadow']==SETTINGS
    result=await f.async_step_ally_shadow({'settings':'{}'})
    assert result['errors']=={'base':'invalid_shadow_settings'}

@pytest.mark.parametrize('key,value',[('topic','a/#'),('monitor_url','http://a/api/command'),
    ('monitor_url','http://a/api/v2/otgw/otmonitor?command=x'),('enabled_entity','climate.x')])
def test_invalid_settings(key,value):
    with pytest.raises(ValueError):module.validate(SETTINGS|{key:value})

async def test_http_uses_get_no_redirects_and_bounds_timeout(hass,monkeypatch):
    a=module.Adapter(hass,SETTINGS)
    response=SimpleNamespace(status=200,json=AsyncMock(return_value={'otmonitor':monitor(1000)}))
    context=AsyncMock();context.__aenter__.return_value=response
    session=SimpleNamespace(get=Mock(return_value=context),post=Mock(side_effect=AssertionError('No POST')))
    from custom_components.sat.ally import monitor as shared
    clock=[1000.0]
    monkeypatch.setattr(shared,'monotonic',lambda:clock[0])
    monkeypatch.setattr(shared,'async_get_clientsession',lambda h:session)
    assert await a.poll()==monitor(1000)
    session.get.assert_called_once_with(SETTINGS['monitor_url'],allow_redirects=False)
    response.status=302
    clock[0]+=3
    with pytest.raises(ValueError):await a.poll()
    session.post.assert_not_called()

async def test_permission_change_during_get_rejects_old_snapshot(env):
    a,clock,unsub,publish=env
    await a.start();await a.task
    entered=asyncio.Event();release=asyncio.Event()
    async def delayed():entered.set();await release.wait();return monitor(1002)
    a.poll.side_effect=delayed;a.schedule();await entered.wait()
    a.hass.states.async_set('binary_sensor.a','off')
    a.policy_changed(None);release.set();await a.task
    assert a.observer.result['status']=='unknown'
    assert a.observer.result['proposed_water'] is None

async def test_sensor_platform_exposes_actual_ha_states(hass,env):
    import logging
    from homeassistant.helpers.entity_component import EntityComponent
    from custom_components.sat.sensor import async_setup_entry
    a,clock,unsub,publish=env
    await a.start();await a.task
    hass.data.setdefault('sat',{})['shadow']={'ally_shadow':a}
    entities=[]
    await async_setup_entry(hass,SimpleNamespace(entry_id='shadow',data={'mode':'ally_shadow'}),entities.extend)
    component=EntityComponent(logging.getLogger(__name__),'sensor',hass)
    await component.async_add_entities(entities)
    assert hass.states.get('sensor.sat_ally_shadow_status').state=='unknown'
    clock[0]=1001;a.message(message())
    clock[0]=1002;a.poll.return_value=monitor(1002);await a.refresh()
    assert hass.states.get('sensor.sat_ally_shadow_status').state=='heat'
    assert float(hass.states.get('sensor.sat_ally_shadow_water').state)<=50
    assert hass.states.get('sensor.sat_ally_shadow_status').attributes['writes'] is False
    a.connection(False)
    assert hass.states.get('sensor.sat_ally_shadow_status').state=='unknown'
    assert hass.states.get('sensor.sat_ally_shadow_water').state=='unknown'
    for e in entities:await component.async_remove_entity(e.entity_id)
    assert not a.listeners

async def test_dashboard_unknowns_and_example_mapping(hass):
    from pathlib import Path
    import yaml
    from homeassistant.helpers.template import Template
    root=Path(__file__).resolve().parents[1]/'hodko'
    settings=json.loads((root/'shadow-settings.example.json').read_text())
    assert len(module.validate(settings)['members'])==12
    view=yaml.safe_load((root/'shadow-dashboard.yaml').read_text())
    assert 'call-service' not in str(view) and 'perform-action' not in str(view)
    for attrs in [{},{'heads':{'a':{'eligible':'unknown','pi':None,'pi_age_seconds':None,
                          'heat_required':None,'heat_required_age_seconds':None}},'reasons':{'a':'missing_hr'}}]:
        hass.states.async_set('sensor.sat_ally_shadow_status','unknown',attrs)
        for card in view['cards']:
            if card['type']=='markdown':
                assert isinstance(Template(card['content'],hass).async_render(),str)


async def test_dashboard_table_has_contiguous_data_rows(hass):
    from pathlib import Path
    import yaml
    from homeassistant.helpers.template import Template
    view=yaml.safe_load((Path(__file__).resolve().parents[1]/'hodko/shadow-dashboard.yaml').read_text())
    heads={name:{'eligible':eligible,'pi':pi,'pi_age_seconds':age,
                 'heat_required':hr,'heat_required_age_seconds':age}
           for name,eligible,pi,age,hr in [('TV','on',99,12,True),('SU','off',None,None,None)]}
    hass.states.async_set('sensor.sat_ally_shadow_status','degraded',{'heads':heads})
    card=next(c for c in view['cards'] if c.get('title')=='Skutečná hlášení hlavic')
    lines=Template(card['content'],hass).async_render().splitlines()
    start=next(i for i,line in enumerate(lines) if line.startswith('| Hlavice'))
    assert all(line.startswith('|') for line in lines[start:start+4])
    assert '| TV | on | 99 | 12 s | ano | 12 s |' in lines
    assert '| SU | off | — | — | — | — |' in lines


async def test_http_recovery_preserves_original_otgw_and_mqtt_ages(env):
    from copy import deepcopy
    a,clock,_,_=env
    await a.start();await a.task
    clock[0]=1001;a.message(message())
    clock[0]=1002;a.poll.return_value=monitor(1002);await a.refresh()
    reports=a.observer.reports
    before=deepcopy(vars(reports))
    clock[0]=1003;a.poll.side_effect=TimeoutError();await a.refresh()
    assert a.observer.result['reason']=='telemetry_retry'
    assert a.observer.result['proposed_water'] is not None
    assert vars(reports)==before and a.observer.telemetry.fields
    assert a.evaluated_at==1003
    a.poll.side_effect=None
    for now in (1004,1005):
        clock[0]=now;await a.refresh()
        assert a.observer.result['status']=='heat'
    clock[0]=1006;a.poll.return_value=monitor(1006);await a.refresh()
    assert a.observer.result['status']=='heat'
    assert vars(reports)==before
    assert a.attributes['heads']['a']['pi_age_seconds']==5
    # Recovery cannot renew MQTT TTL, even with advancing OTGW fields.
    clock[0]=5000;a.poll.return_value=monitor(5000);await a.refresh()
    clock[0]=5030;a.poll.return_value=monitor(5030);await a.refresh()
    assert a.observer.result['status']=='unknown'
    assert vars(reports)==before
    a.connection(False)
    assert not reports.heads


async def test_source_snapshot_restores_shadow_before_any_new_head_report(env):
    from tests.test_ally_mode import source_snapshot, event
    a,clock,unsub,publish=env
    await a.start();await a.task
    clock[0]=1002
    snap=source_snapshot([event(at=990,heat_required=dict(value=True,valid=True),
                                pi_heating_demand=dict(value=99,valid=True))],1001)
    a.poll.return_value=monitor(1002)
    a.snapshot_message(SimpleNamespace(payload=json.dumps(snap),retain=False))
    await a.task
    assert a.attributes['heads']['a']['pi_age_seconds']==12
    assert a.attributes['heads']['a']['heat_required'] is True
    assert a.attributes['heads']['a']['source']=='verified_source_snapshot'
    assert a.observer.result['status']=='heat', a.observer.result
    publish.assert_not_called()


async def test_policy_notification_does_not_erase_brtv_hysteresis(env):
    a,clock,_,_=env
    await a.start();await a.task
    clock[0]=1001;a.message(message())
    clock[0]=1002;a.poll.return_value=monitor(1002);await a.refresh()
    a.observer.reports.heads['a']['pi']['value']=12
    clock[0]=1003;a.poll.return_value=monitor(1003);await a.refresh()
    assert a.observer.result['zones']['A'] is True
    a.policy_changed(None);await a.task
    assert a.observer.result['zones']['A'] is True
    a.hass.states.async_set('binary_sensor.a','off')
    await a.hass.async_block_till_done()
    await a.refresh()
    assert a.observer.result['zones']['A'] is False


@pytest.fixture
async def shadow_sync(hass,monkeypatch):
    from copy import deepcopy
    try:
        from pytest_homeassistant_custom_component.common import MockConfigEntry
    except ModuleNotFoundError as exc:
        if exc.name != 'pytest_homeassistant_custom_component': raise
        from pytest_homeassistant.common import MockConfigEntry
    from custom_components.sat.config_flow import ShadowOptionsFlow
    from custom_components.sat.ally.runtime import OperatingLimits
    from custom_components.sat.ally.kernel import Policy
    from custom_components.sat.const import CLIMATE
    shadow=MockConfigEntry(domain='sat',title='Observer',data={'mode':'ally_shadow','hodko_shadow':deepcopy(SETTINGS)},options={'unrelated':'preserved'})
    active=deepcopy(SETTINGS)
    active['policy'].update(maximum_water=60,sustained_max=7)
    active['operating_limits']={'water_margin':5}
    source=MockConfigEntry(domain='sat',title='Owner',data={'mode':'mqtt_opentherm'},options={
        'hodko_control_mode':'ally_demand','maximum_setpoint':60,'hodko_ally':active})
    for entry in (shadow,source):entry.add_to_hass(hass)
    runtime=SimpleNamespace(settings=deepcopy(active),policy=Policy(**active['policy']),
        limits=OperatingLimits(**active['operating_limits']),closed=False)
    climate=SimpleNamespace(_ally_runtime=runtime,_coordinator=SimpleNamespace(maximum_setpoint=60))
    hass.data.setdefault('sat',{})[source.entry_id]={CLIMATE:climate}
    flow=ShadowOptionsFlow();flow.hass=hass;flow.handler=shadow.entry_id
    monkeypatch.setattr(module.mqtt,'async_publish',AsyncMock(side_effect=AssertionError('No MQTT writes')))
    monkeypatch.setattr(type(hass.services),'async_call',AsyncMock(side_effect=AssertionError('No service calls')))
    yield flow,shadow,source,runtime,climate
    module.mqtt.async_publish.assert_not_called();hass.services.async_call.assert_not_called()


async def test_shadow_alignment_is_explicit_same_entry_and_read_only(shadow_sync):
    from copy import deepcopy
    from dataclasses import asdict
    f,shadow,source,r,c=shadow_sync
    before=deepcopy(dict(source.options));old=deepcopy(dict(shadow.options))
    form=await f.async_step_init()
    assert form['step_id']=='shadow_limits' and shadow.options==old
    result=await f.async_step_shadow_limits({'source_entry':source.entry_id})
    assert result['type']=='create_entry'
    saved=result['data']['hodko_shadow']
    assert saved['policy']==asdict(r.policy) and saved['operating_limits']==asdict(r.limits)
    assert saved['members']==SETTINGS['members'] and result['data']['unrelated']=='preserved'
    assert shadow.options==old and source.options==before  # Only HA applies returned options.
    adapter=module.Adapter(f.hass,saved)
    assert adapter.attributes['operating_limits']['maximum_water']==60
    assert adapter.attributes['operating_limits']['normal_water_guard']==65
    assert adapter.attributes['policy']['sustained_max']==7
    assert adapter.attributes['writes'] is False
    assert not hasattr(adapter,'coordinator')


@pytest.mark.parametrize('bad', ['source_changed','shadow_changed','runtime_old','runtime_closed','cap_mismatch','missing_runtime','unknown_source'])
async def test_shadow_alignment_rejects_stale_or_unloaded_owner(shadow_sync,bad):
    from copy import deepcopy
    f,shadow,source,r,c=shadow_sync
    await f.async_step_init()
    if bad=='source_changed':f.hass.config_entries.async_update_entry(source,options={**source.options,'maximum_setpoint':55})
    elif bad=='shadow_changed':f.hass.config_entries.async_update_entry(shadow,options={**shadow.options,'unrelated':'changed'})
    elif bad=='runtime_old':r.settings={}
    elif bad=='runtime_closed':r.closed=True
    elif bad=='cap_mismatch':c._coordinator.maximum_setpoint=55
    elif bad=='missing_runtime':c._ally_runtime=None
    before=deepcopy(dict(shadow.options))
    result=await f.async_step_shadow_limits({'source_entry':'missing' if bad=='unknown_source' else source.entry_id})
    assert result['errors']['base']=='shadow_sync_failed' and shadow.options==before


@pytest.mark.parametrize('key,value', [('members',{}),('topic','other/events'),('enabled_entity','binary_sensor.other'),
    ('monitor_url','http://other/api/v2/otgw/otmonitor'),('telemetry_transport','mqtt')])
async def test_shadow_alignment_never_selects_an_unrelated_system(shadow_sync,key,value):
    f,shadow,source,r,c=shadow_sync
    active={**source.options['hodko_ally'],key:value}
    f.hass.config_entries.async_update_entry(source,options={**source.options,'hodko_ally':active})
    result=await f.async_step_init()
    assert result['type']=='abort' and result['reason']=='shadow_no_matching_sat'


async def test_shadow_options_reload_uses_saved_values_without_changing_original_data(hass,monkeypatch):
    try:
        from pytest_homeassistant_custom_component.common import MockConfigEntry
    except ModuleNotFoundError as exc:
        if exc.name != 'pytest_homeassistant_custom_component': raise
        from pytest_homeassistant.common import MockConfigEntry
    from copy import deepcopy
    saved=deepcopy(SETTINGS);saved['policy']['maximum_water']=60;saved['operating_limits']={'water_margin':5}
    entry=MockConfigEntry(domain='sat',data={'mode':'ally_shadow','hodko_shadow':SETTINGS},options={'hodko_shadow':saved})
    entry.add_to_hass(hass)
    adapter=SimpleNamespace(start=AsyncMock(),stop=AsyncMock())
    factory=Mock(return_value=adapter)
    monkeypatch.setattr(module,'Adapter',factory)
    monkeypatch.setattr(sat,'SatDataUpdateCoordinatorFactory',Mock(side_effect=AssertionError('No coordinator')))
    monkeypatch.setattr(hass.config_entries,'async_forward_entry_setups',AsyncMock())
    monkeypatch.setattr(hass.config_entries,'async_unload_platforms',AsyncMock(return_value=True))
    reload=AsyncMock();monkeypatch.setattr(hass.config_entries,'async_reload',reload)
    assert await sat.async_setup_entry(hass,entry)
    factory.assert_called_once_with(hass,saved)
    hass.config_entries.async_update_entry(entry,options={**entry.options,'changed':True})
    await hass.async_block_till_done()
    reload.assert_awaited_once_with(entry.entry_id)
    assert entry.data['hodko_shadow']==SETTINGS
    assert await sat.async_unload_entry(hass,entry)


@pytest.mark.parametrize('margin', [True,float('nan'),4,21])
def test_shadow_rejects_invalid_water_margin(margin):
    with pytest.raises(ValueError):module.validate({**SETTINGS,'operating_limits':{'water_margin':margin}})
