"""Passive evidence tests: no coordinator or boiler object is provided."""
from custom_components.sat.ally.shadow import Observer
from custom_components.sat.ally.runtime import utc
from custom_components.sat.ally.kernel import Policy

MEMBERS={'0x123': {'name':'head','zone':'room','eligible_entity':'binary_sensor.room'}}
STATES={'binary_sensor.global':'on','binary_sensor.room':'on'}

def observer():
    return Observer(MEMBERS,'binary_sensor.global',1000,Policy(ttl=3900,minimum_water=41,water_warm=41,maximum_water=50))

def event(seq=1, at=1001, hr=True):
    return {'schema_version':2,'session_id':'7777eadd-b5a6-45db-bd77-1943e604ee75',
            'session_started_at':utc(900),'received_at':utc(at),'sequence':seq,
            'device_ieee':'0x123','message_type':'attributeReport',
            'fields':{'pi_heating_demand':{'value':99,'valid':True},'heat_required':{'value':hr,'valid':True}}}

def monitor(epoch):
    return {k:{'value':v,'epoch':epoch} for k,v in {'outsidetemperature':10,'chenable':'On',
        'controlsetpoint':45,'boilertemperature':45,'faultindicator':'Off','dhwmode':'Off'}.items()}

def ready(o):
    o.accept(event(),1001)
    assert o.evaluate(STATES,monitor(1000),1002,2)['status']=='unknown'
    return o.evaluate(STATES,monitor(1003),1004,4)

def test_fresh_evidence_proposal_without_writer():
    o=observer();r=ready(o)
    assert r['status']=='heat'
    assert 41<=r['proposed_water']<=50
    assert r['writes'] is False and r['command_confirmation']=='not_applicable'
    assert not hasattr(o,'c') and not hasattr(o,'coordinator')

def test_contact_veto_and_unknown_are_not_heat():
    o=observer();ready(o)
    for state in ['off','unavailable']:
        r=o.evaluate(STATES|{'binary_sensor.room':state},monitor(1005),1006,6)
        assert r['status'] in ('off','unknown') and r['proposed_water'] is None

def test_global_off_and_unknown():
    o=observer()
    for state,status in [('off','off'),('unavailable','unknown')]:
        assert o.evaluate(STATES|{'binary_sensor.global':state},None,1002,2)['status']==status

def test_disconnect_retained_and_old_reports_cannot_restore_heat():
    o=observer();ready(o);o.disconnect(1005)
    assert not o.accept(event(),1006)
    assert not o.accept(event(2,1006),1006,retained=True)
    o.evaluate(STATES,monitor(1006),1007,7)
    assert o.evaluate(STATES,monitor(1008),1009,9)['status']=='unknown'

def test_missing_and_stale_monitor():
    o=observer();ready(o)
    assert o.evaluate(STATES,None,1006,6)['reason']=='telemetry_retry'
    assert o.evaluate(STATES,monitor(1007),1008,8)['status']=='heat'
    assert o.evaluate(STATES,monitor(1009),1010,10)['status']=='heat'
    assert o.evaluate(STATES,monitor(1009),1105,105)['status']=='unknown'


def test_partial_http_shadow_keeps_original_outside_then_expires():
    o=observer();ready(o)
    proof=o.telemetry.fields['outsidetemperature']
    m=monitor(1005);del m['outsidetemperature']
    result=o.evaluate(STATES,m,1006,6)
    assert result['status']=='heat' and result['writes'] is False
    assert 'outsidetemperature' in result['http_missing_fields']
    assert o.telemetry.fields['outsidetemperature']==proof
    for mono in (30,60,90):
        m=monitor(1000+mono);del m['outsidetemperature']
        o.evaluate(STATES,m,1000+mono,mono)
    m=monitor(1100);del m['outsidetemperature']
    result=o.evaluate(STATES,m,1101,101)
    assert result['status']=='unknown' and 'outsidetemperature' in result['reason']

def test_clock_fault_and_dhw_block_proposals():
    o=observer();ready(o)
    assert o.evaluate(STATES,monitor(1100),1100,5)['status']=='unknown'
    for key in ['faultindicator','dhwmode']:
        o=observer();ready(o);m=monitor(1005);m[key]['value']='On'
        assert o.evaluate(STATES,m,1006,6)['status']=='unknown'


def test_fractional_wall_time_does_not_reject_current_permissions():
    # Exact production timestamp whose ISO round-trip rounds into the future.
    base = 1792426764.7852535
    o = Observer(MEMBERS, 'binary_sensor.global', base-10,
                 Policy(ttl=3900, minimum_water=41, water_warm=41, maximum_water=50))
    o.accept(event(at=base-2), base-1)
    o.evaluate(STATES, monitor(int(base-1)), base, 10)
    result = o.evaluate(STATES, monitor(int(base+1)), base+2, 12)
    assert result['status'] == 'heat'
    assert result['reasons']['head'] == 'valid_demand'
    result = o.evaluate(STATES | {'binary_sensor.room':'off'}, monitor(int(base+3)), base+4, 14)
    assert result['status'] == 'off'
    assert result['reasons']['head'] == 'blocked_by_room_policy'


def test_fractional_clock_preserves_stale_and_missing_evidence_guards():
    base = 1792426764.7852535
    o = Observer(MEMBERS, 'binary_sensor.global', base-10)
    o.evaluate(STATES, monitor(int(base-1)), base, 10)
    r = o.evaluate(STATES, monitor(int(base+1)), base+2, 12)
    assert r['reasons']['head'] == 'heat_required_unknown'
    o.accept(event(at=base+3), base+4)
    o.evaluate(STATES, monitor(int(base+4)), base+5, 15)
    r = o.evaluate(STATES, monitor(int(base+4000)), base+4001, 4011)
    r = o.evaluate(STATES, monitor(int(base+4002)), base+4003, 4013)
    assert r['status'] == 'unknown'
    assert r['reasons']['head'] == 'heat_required_unknown'
    assert r['proposed_water'] is None


def test_local_timestamp_never_moves_evidence_into_future():
    from datetime import datetime
    from math import nextafter, inf
    from custom_components.sat.ally.shadow import observation_utc
    for base in [1000.0, 1792426764.7852535, 1792426764.9999998]:
        for t in [nextafter(base,-inf),base,nextafter(base,inf)]:
            encoded=datetime.fromisoformat(observation_utc(t)).timestamp()
            assert encoded <= t
            assert t-encoded < 0.0000011


def test_fractional_clock_does_not_accept_future_source_report():
    base = 1792426764.7852535
    o = Observer(MEMBERS, 'binary_sensor.global', base-10)
    assert not o.accept(event(at=base+1),base)
    assert o.result['reason'] == 'invalid_report'
    assert not o.reports.heads


def test_brtv_hysteresis_survives_short_transport_gap_only():
    o=observer();ready(o)
    e=event(2,1005);e['fields']['pi_heating_demand']['value']=12
    o.accept(e,1005)
    assert o.evaluate(STATES,monitor(1005),1006,6)['zones']['room'] is True
    receipts=dict(o.telemetry.fields)
    assert o.evaluate(STATES,None,1010,10)['zones']['room'] is True
    assert o.telemetry.fields==receipts
    assert o.evaluate(STATES,monitor(1010),1011,11)['zones']['room'] is True
    assert o.evaluate(STATES,None,1200,200)['status']=='unknown'
    assert not o.engine.memory.calling


def test_shadow_supports_55_without_changing_default_or_writing():
    import pytest
    o=Observer(MEMBERS,'binary_sensor.global',1000,
               Policy(ttl=3900,minimum_water=41,water_warm=41,water_cold=50,maximum_water=55))
    result=ready(o)
    assert result['status']=='heat' and result['proposed_water']<=55 and result['writes'] is False
    with pytest.raises(ValueError):
        Observer(MEMBERS,'binary_sensor.global',1000,Policy(maximum_water=61))
    assert observer().policy.maximum_water==50


def test_shadow_snapshot_repairs_partial_reconnect_without_epoch_error():
    from tests.test_ally_mode import source_snapshot
    o=observer();first=event();assert o.accept(first,1001)
    o.disconnect(1005)
    partial=event(2,1006);partial['fields'].pop('heat_required')
    assert o.accept(partial,1006)
    o.evaluate(STATES,monitor(1006),1006,6)
    assert o.evaluate(STATES,monitor(1007),1007,7)['status']=='unknown'
    assert o.reports.accept_snapshot(source_snapshot([first,partial],1008),1008,3900)
    result=o.evaluate(STATES,monitor(1008),1008,8)
    assert result['status']=='heat' and result['writes'] is False
    assert o.reports.heads['head']['heat_required']['observed_at']==first['received_at']


def test_foreign_and_bad_member_reports_preserve_shadow_telemetry_and_other_heat():
    from copy import deepcopy
    o=observer();ready(o)
    proof=deepcopy(o.reports.heads['head']);telemetry=deepcopy(o.telemetry.fields)
    for seq in range(2,8):
        e=event(seq,1004+seq);e['device_ieee']='0x456';e['fields']['heat_required']['value']='bad'
        if seq==4:
            # Now it is a configured but broken member; same isolation rule.
            o.members['0x456']={'name':'bad','zone':'other','eligible_entity':'binary_sensor.bad'}
            o.reports.mapping['0x456']='bad';o.engine.inventory['bad']='other'
        o.accept(e,1004+seq)
        assert o.reports.heads['head']==proof
        assert o.telemetry.fields==telemetry
        result=o.evaluate(STATES|{'binary_sensor.bad':'on'},None,1004+seq,4+seq)
        assert result['status'] in ('heat','degraded') and result['writes'] is False
        assert result['zones']['room'] is True
