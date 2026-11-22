import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from custom_components.sat.ally import mqtt_monitor as m
from custom_components.sat.ally.runtime import Telemetry, Runtime
from custom_components.sat.ally.shadow_adapter import Adapter
from test_shadow_adapter import SETTINGS

NS='OTGW/value/otgw-test'

def evidence():
    e=m.Evidence();e.reset(True);return e

@pytest.mark.parametrize('leaf,payload,key,value',[
 ('ch_enable','OFF','chenable','Off'),('TSet','10.00','controlsetpoint',10),
 ('Tboiler','35.5','boilertemperature',35.5),('Toutside','-2.5','outsidetemperature',-2.5),
 ('MaxRelModLevelSetting','80','maxrelmodlvl',80),('fault','ON','faultindicator','On'),
 ('domestichotwater','ON','dhwmode','On')])
def test_mapping_original_receipt(leaf,payload,key,value):
    e=evidence();e.accept(leaf,payload,False,100)
    t=Telemetry();t.observe(e.snapshot(),120)
    assert t.value(key,120)==value and t.fields[key][2]==100
    t.observe(e.snapshot(),185);assert t.fields[key][2]==100
    with pytest.raises(ValueError):t.value(key,191)

@pytest.mark.parametrize('payload',['NaN','inf','-1','101','garbage'])
def test_invalid_new_report_revokes_old_value(payload):
    e=evidence();e.accept('TSet','45',False,100);e.accept('TSet',payload,False,101)
    assert 'controlsetpoint' not in e.snapshot()

def test_no_retained_restore_echo_override_or_wrong_side():
    e=evidence()
    for leaf in ['TSet','TSet_boiler','TSet_thermostat','TSet/override','event_report']:
        e.accept(leaf,'45',True,100)
    for leaf in ['TSet_boiler','TSet_thermostat','TSet/override','event_report']:
        e.accept(leaf,'45',False,101)
    assert not e.snapshot()

def test_disconnect_and_gateway_restart_revoke_all():
    e=evidence();e.accept('TSet','45',False,100)
    e.reset(False);e.accept('TSet','45',False,101);assert not e.snapshot()
    e.reset(True);e.accept('TSet','45',True,102);assert not e.snapshot()
    e.accept('TSet','45',False,103);assert e.snapshot()
    e.accept('','offline',True,104);assert not e.snapshot()
    e.accept('TSet','45',False,105);assert not e.snapshot()
    e.accept('','online',False,106);e.accept('TSet','45',False,107);assert e.snapshot()
    e.accept('','online',False,108);assert not e.snapshot()

def test_snapshot_isolation_and_fault_not_masked_by_cache():
    e=evidence();e.accept('fault','OFF',False,100);s=e.snapshot()
    s.clear();assert e.snapshot()
    e.accept('fault','ON',False,101);e.accept('fault','OFF',True,102)
    assert e.snapshot()['faultindicator'][1]=='On'

def test_confirm_requires_new_state_not_old_cache_or_echo():
    e=evidence();e.accept('ch_enable','OFF',False,99);e.accept('TSet','10',False,99)
    r=SimpleNamespace(pending=(False,10,100),tripped=None,stop_publish_pending=False,stop_confirmation_after=None,telemetry=Telemetry(),
                      ports=SimpleNamespace(wall_time=lambda: 106.0))
    r.telemetry.observe(e.snapshot(),101);Runtime.confirm(r,101);assert r.pending
    e.accept('event_report','CS: 10.00',False,102)
    r.telemetry.observe(e.snapshot(),103);Runtime.confirm(r,103);assert r.pending
    e.accept('ch_enable','OFF',False,104);e.accept('TSet','0',False,105)
    r.telemetry.observe(e.snapshot(),106);Runtime.confirm(r,106);assert r.pending is None

@pytest.mark.parametrize('cs,expected',[(45,True),(46,False)])
def test_on_confirm_matches_new_target(cs,expected):
    e=evidence();e.accept('ch_enable','ON',False,101);e.accept('TSet',str(cs),False,102)
    r=SimpleNamespace(pending=(True,45,100),tripped=None,stop_publish_pending=False,stop_confirmation_after=None,telemetry=Telemetry(),
                      ports=SimpleNamespace(wall_time=lambda: 106.0))
    r.telemetry.observe(e.snapshot(),103);Runtime.confirm(r,103)
    assert (r.pending is None)==expected

def test_missing_states_do_not_extend_deadline():
    r=SimpleNamespace(pending=(False,10,100),tripped=None,stop_publish_pending=False,stop_confirmation_after=None,telemetry=Telemetry(),
                      ports=SimpleNamespace(wall_time=lambda: 106.0))
    with pytest.raises(ValueError):Runtime.confirm(r,190)
    assert r.tripped=='command_not_confirmed'

async def test_shared_single_subscription_no_http_or_publish(hass,monkeypatch):
    sub=AsyncMock(return_value=Mock());pub=AsyncMock(side_effect=AssertionError('No publishing'))
    monkeypatch.setattr(m.mqtt,'async_subscribe',sub);monkeypatch.setattr(m.mqtt,'is_connected',lambda h:True)
    monkeypatch.setattr(m.mqtt,'async_publish',pub)
    a=m.shared_mqtt_reader(hass,NS);b=m.shared_mqtt_reader(hass,NS);assert a is b
    await asyncio.gather(a.read(),b.read());assert sub.await_count==1
    a.message(SimpleNamespace(topic=NS+'/TSet',payload='45',retain=False))
    x=await a.read();y=await b.read();assert x==y and x is not y
    a.message(SimpleNamespace(topic='other/value/id/TSet',payload='10',retain=False))
    assert (await b.read())['controlsetpoint'][1]==45
    a.close();pub.assert_not_called()

async def test_active_and_shadow_use_mqtt_branch(hass,monkeypatch):
    sub=AsyncMock(return_value=Mock());monkeypatch.setattr(m.mqtt,'async_subscribe',sub)
    monkeypatch.setattr(m.mqtt,'is_connected',lambda h:True)
    settings={**deepcopy(SETTINGS),'telemetry_transport':'mqtt','otgw_mqtt_topic':NS}
    a=Adapter(hass,settings)
    r=SimpleNamespace(c=SimpleNamespace(hass=hass),mqtt_namespace=NS,settings=settings)
    from custom_components.sat.ally import runtime,shadow_adapter
    def fail(*args):raise AssertionError('HTTP forbidden')
    monkeypatch.setattr(runtime,'shared_reader',fail);monkeypatch.setattr(shadow_adapter,'shared_reader',fail)
    await Runtime.poll(r);await a.poll();assert sub.await_count==1
    m.shared_mqtt_reader(hass,NS).close()

@pytest.mark.parametrize('settings',[{'telemetry_transport':'bad'},
 {'telemetry_transport':'mqtt'},{'telemetry_transport':'mqtt','otgw_mqtt_topic':'OTGW/value/#'}])
def test_invalid_explicit_settings(settings):
    with pytest.raises(ValueError):m.topic(settings)

def test_gateway_reconnect_between_evaluations_invalidates_dhw_continuity():
    e=evidence();e.accept('domestichotwater','ON',False,100)
    t=Telemetry();t.observe(e.snapshot(),101)
    e.accept('','offline',False,102);e.accept('','online',False,103)
    e.accept('domestichotwater','OFF',False,104)
    with pytest.raises(ValueError,match='session changed'):t.observe(e.snapshot(),105)
    assert not t.fields
    t.observe(e.snapshot(),106);assert t.value('dhwmode',106)=='Off'

async def test_subscription_failure_cleans_up_and_can_retry(hass,monkeypatch):
    unsub=Mock();monkeypatch.setattr(m,'async_dispatcher_connect',lambda *a:unsub)
    monkeypatch.setattr(m.mqtt,'is_connected',lambda h:True)
    sub=AsyncMock(side_effect=RuntimeError('subscribe failed'));monkeypatch.setattr(m.mqtt,'async_subscribe',sub)
    r=m.shared_mqtt_reader(hass,NS)
    with pytest.raises(RuntimeError):await r.start()
    assert not r.started and not r.unsubs;unsub.assert_called_once()
    sub.side_effect=None;sub.return_value=Mock();await r.start();assert r.started;r.close()

# Minimized passive recording 2026-10-21 00:45:10–00:47:15 CEST.
# Offset seconds from 22:45:00 UTC. Not synthetic evidence of active heating.
RECORDED=[(56,'Toutside','8.50'),(61,'Tboiler','35.70'),
 (66,'ch_enable','OFF'),(67,'fault','OFF'),(67,'domestichotwater','OFF'),
 (68,'TSet','10.00'),(73,'MaxRelModLevelSetting','100.00'),
 (120,'Toutside','8.50'),(121,'Tboiler','35.50'),(130,'ch_enable','OFF'),
 (131,'fault','OFF'),(131,'domestichotwater','OFF'),(132,'TSet','10.00')]

def test_passive_recording_replay_confirms_off_without_http():
    e=evidence();t=Telemetry()
    r=SimpleNamespace(pending=(False,10,58.5),tripped=None,stop_publish_pending=False,stop_confirmation_after=None,telemetry=t,
                      ports=SimpleNamespace(wall_time=lambda:135.0))
    for at,leaf,value in RECORDED:e.accept(leaf,value,False,at)
    t.observe(e.snapshot(),135)
    Runtime.confirm(r,135);assert r.pending is None
    assert t.number('maxrelmodlvl',135)==100
    assert t.number('boilertemperature',135)==35.5
    assert t.fields['maxrelmodlvl'][2]==73
    with pytest.raises(ValueError):t.number('maxrelmodlvl',164)


# Passive collector UTC 2026-10-29 21:36:00–21:39:30 (22:36–22:39:30 CET).
# One-second collector resolution, no HA receipt or source timestamp claim.
# Around historical HTTP age=None at 22:38:13; no injected device messages.
OCT29_OFF_RECORDING = [
    (4, 'Tboiler', '50.50'),
    (11, 'TSet', '0.00'),
    (11, 'Tboiler', '50.80'),
    (20, 'Tboiler', '51.10'),
    (25, 'ch_enable', 'OFF'),
    (26, 'domestichotwater', 'OFF'),
    (27, 'Tboiler', '51.40'),
    (31, 'Toutside', '4.90'),
    (36, 'Tboiler', '51.70'),
    (42, 'centralheating', 'OFF'),
    (43, 'Tboiler', '52.10'),
    (48, 'MaxRelModLevelSetting', '100.00'),
    (51, 'fault', 'OFF'),
    (51, 'flame', 'OFF'),
    (52, 'Tboiler', '52.40'),
    (59, 'Tboiler', '52.60'),
    (63, 'Toutside', '4.80'),
    (68, 'Tboiler', '52.90'),
    (75, 'Tboiler', '53.10'),
    (76, 'TSet', '0.00'),
    (79, 'Toutside', '4.90'),
    (84, 'Tboiler', '53.30'),
    (89, 'ch_enable', 'OFF'),
    (90, 'domestichotwater', 'OFF'),
    (91, 'Tboiler', '53.50'),
    (100, 'Tboiler', '53.80'),
    (106, 'centralheating', 'OFF'),
    (107, 'Tboiler', '54.00'),
    (112, 'MaxRelModLevelSetting', '100.00'),
    (115, 'fault', 'OFF'),
    (115, 'flame', 'OFF'),
    (116, 'Tboiler', '54.30'),
    (123, 'Tboiler', '54.50'),
    (127, 'Toutside', '4.80'),
    (132, 'Tboiler', '54.70'),
    (139, 'TSet', '10.00'),
    (139, 'Tboiler', '54.90'),
    (143, 'Toutside', '4.90'),
    (148, 'Tboiler', '55.10'),
    (154, 'ch_enable', 'OFF'),
    (154, 'domestichotwater', 'OFF'),
    (155, 'Tboiler', '55.30'),
    (164, 'Tboiler', '55.50'),
    (170, 'centralheating', 'OFF'),
    (171, 'Tboiler', '55.60'),
    (176, 'MaxRelModLevelSetting', '100.00'),
    (179, 'fault', 'OFF'),
    (179, 'flame', 'OFF'),
    (180, 'Tboiler', '55.80'),
    (187, 'Tboiler', '55.90'),
    (191, 'Toutside', '4.80'),
    (196, 'Tboiler', '56.20'),
    (203, 'Tboiler', '56.30'),
    (204, 'TSet', '0.00'),
]


def test_oct29_passive_mqtt_evidence_survives_http_stale_episode():
    e=evidence();t=Telemetry();expected=set(key for key,_ in m.FIELDS.values())
    i=0
    for now in range(0,211):
        while i<len(OCT29_OFF_RECORDING) and OCT29_OFF_RECORDING[i][0]<=now:
            at,leaf,payload=OCT29_OFF_RECORDING[i]
            e.accept(leaf,payload,False,at);i+=1
        t.observe(e.snapshot(),now)
        if now<66:continue  # Cold bootstrap is not complete at the first topic.
        for field in expected:t.value(field,now)
        assert t.value('chenable',now)=='Off'
        assert 0<=t.number('controlsetpoint',now)<=10
        assert t.value('faultindicator',now)=='Off'
        assert t.number('maxrelmodlvl',now)==100
    # Reading the same snapshot does not turn it into another live report.
    snapshot=e.snapshot();old=dict(t.fields)
    t.observe(snapshot,211);assert t.fields==old
    t.observe(snapshot,301)
    with pytest.raises(ValueError,match='Stale telemetry'):t.value('chenable',301)
