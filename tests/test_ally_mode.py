from copy import deepcopy
from datetime import datetime, timezone
import pytest
from custom_components.sat.ally.control import Engine, Reports, Ramp
from custom_components.sat.ally.kernel import Policy, Decision
from custom_components.sat.ally.runtime import Telemetry


def ts(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def reading(value, at=1000, evidence='zcl_attributeReport'):
    return dict(value=value, observed_at=ts(at), evidence=evidence)


def frame(pi=80, hr=True):
    return dict(session_id='one', session_started_at=ts(900), heads={'a': {
        'pi': reading(pi), 'heat_required': reading(hr),
        'eligible': reading(True, evidence='policy_evaluation')}},
        outside=reading(5, evidence='otgw_report'))


def event(seq=1, at=1000, sid='00000000-0000-0000-0000-000000000001', start=900, **fields):
    return dict(schema_version=2, session_id=sid, session_started_at=ts(start), sequence=seq,
                received_at=ts(at), device_ieee='0x0000000000000001', message_type='attributeReport',
                fields=fields or {'pi_heating_demand': {'valid': True, 'value': 80}})


def reports():
    return Reports({'0x0000000000000001': 'a'}, 950)


@pytest.mark.parametrize('pi,hr,heat', [(100, True, True), (100, False, False), (0, True, False), (100, None, None)])
def test_pi_hr_without_room_temperature(pi, hr, heat):
    assert Engine({'a':'A'}).evaluate(frame(pi, hr), 1000).heat is heat


@pytest.mark.parametrize('field', ['pi', 'heat_required', 'eligible'])
@pytest.mark.parametrize('fault', ['missing', 'stale', 'future', 'cache'])
def test_untrusted_or_expired(field, fault):
    f=frame(); r=f['heads']['a'][field]
    if fault=='missing': del f['heads']['a'][field]
    if fault=='stale': r['observed_at']=ts(1)
    if fault=='future': r['observed_at']=ts(1001)
    if fault=='cache': r['evidence']='HA_last_reported'
    assert Engine({'a':'A'}).evaluate(f,1000).heat is None


def test_degraded_and_pr_max_not_sum():
    f=frame(30); f['heads']['b']=deepcopy(f['heads']['a'])
    p=Policy(); single=Engine({'a':'PR'},p).evaluate(frame(30),1000)
    d=Engine({'a':'PR','b':'PR','c':'OTHER'},p).evaluate(f,1000)
    assert d.heat is True and d.degraded and d.water==single.water


def test_disable_dominates_and_no_outside_fallback():
    f=frame(); f['outside']=None
    assert Engine({'a':'A'}).evaluate(f,1000).heat is None
    assert Engine({'a':'A'}).evaluate(f,1000,enabled=False).heat is False


def test_epoch_gap_and_clock_reversal():
    e=Engine({'a':'A'});e.evaluate(frame(),1000)
    assert e.evaluate(frame(),5000).heat is None
    with pytest.raises(ValueError):e.evaluate(frame(),4000)


def test_retained_preboot_duplicates_gap_and_invalid_report():
    r=reports()
    assert not r.accept(event(),1000,retained=True)
    assert not r.accept(event(at=940),1000)
    assert r.accept(event(),1000)
    assert not r.accept(event(),1001)
    assert r.heads['a']['pi']['observed_at']==ts(1000)
    r.accept(event(seq=3,at=1002,heat_required=dict(value=True,valid=True)),1002)
    assert 'pi' not in r.heads['a']
    r.accept(event(seq=4,at=1003,pi_heating_demand=dict(value=None,valid=False)),1003)
    assert r.heads['a']['pi']['value'] is None


def test_new_session_discards_all_and_late_session_ignored():
    r=reports();r.accept(event(),1000)
    r.accept(event(seq=1,at=1010,start=1005,sid='00000000-0000-0000-0000-000000000002',heat_required=dict(value=True,valid=True)),1010)
    assert 'pi' not in r.heads['a']
    assert not r.accept(event(seq=2,at=1011),1011)


def test_conflicting_duplicate_and_future_rejected():
    r=reports();r.accept(event(),1000)
    assert r.accept(event(pi_heating_demand=dict(value=1,valid=True)),1000)
    assert r.errors == {'a':'invalid_report'}
    with pytest.raises(ValueError):r.accept(event(seq=2,at=1002),1001)


def test_reconnect_requires_new_reports():
    r=reports();r.accept(event(),1000);r.invalidate(1005)
    assert not r.accept(event(seq=2,at=1004),1010)
    assert r.heads=={}


def test_telemetry_epoch_not_get_time():
    t=Telemetry();raw={'outsidetemperature':dict(value=5,epoch=10)}
    t.observe(raw,100)
    with pytest.raises(ValueError):t.value('outsidetemperature',100)
    t.observe(raw,110)
    with pytest.raises(ValueError):t.value('outsidetemperature',110)
    raw['outsidetemperature']['epoch']=11;t.observe(raw,120)
    assert t.value('outsidetemperature',120)==5
    t.observe(raw,210)
    with pytest.raises(ValueError):t.value('outsidetemperature',210)
    raw['outsidetemperature']['epoch']=1;t.observe(raw,220)
    with pytest.raises(ValueError):t.value('outsidetemperature',220)


# .46: a partial HTTP snapshot must not erase still-valid original evidence.
@pytest.mark.parametrize('key,value', [
    ('outsidetemperature',5), ('chenable','On'), ('controlsetpoint',48.5),
    ('boilertemperature',49), ('faultindicator','Off'), ('dhwmode','Off'),
    ('maxrelmodlvl',100)])
def test_partial_http_preserves_original_proof_only_until_expiry(key,value):
    t=Telemetry()
    t.observe({key:dict(value=value,epoch=1)},100)
    t.observe({key:dict(value=value,epoch=2)},110)
    proof=t.fields[key]
    for now in (120,150,190):
        t.observe({},now)
        assert t.fields[key]==proof
        assert t.value(key,now)==value
        assert key in t.http_missing
        assert t.http_issues[key]=='missing'
    t.observe({},190.001)
    with pytest.raises(ValueError,match='Stale telemetry'):t.value(key,190.001)


def test_http_field_gap_cannot_manufacture_post_command_evidence():
    t=Telemetry();key='maxrelmodlvl'
    t.observe({key:dict(value=100,epoch=1)},100)
    t.observe({key:dict(value=100,epoch=2)},110)
    for now in (120,130):
        t.observe({'outsidetemperature':dict(value=5,epoch=now)},now)
    t.observe({key:dict(value=100,epoch=3)},140)
    # The field could have advanced at111, before a command sent at115.
    # Polls of unrelated fields are not an observation of this field.
    assert t.fields[key][2]==110 and t.fields[key][2]<115
    t.observe({key:dict(value=100,epoch=4)},150)
    assert t.fields[key][2]==140


@pytest.mark.parametrize('bad', [None, [], {}, {'value':100},
    {'value':100,'epoch':0}, {'value':100,'epoch':True}])
def test_explicit_invalid_http_report_dominates_preserved_value(bad):
    t=Telemetry();key='maxrelmodlvl'
    for now,epoch in [(100,1),(110,2)]:
        t.observe({key:dict(value=100,epoch=epoch)},now)
    t.observe({key:bad},120)
    assert t.http_issues[key]==('invalid_field' if not isinstance(bad,dict) else 'invalid_epoch')
    t.observe({},125)
    with pytest.raises(ValueError):t.number(key,125)
    t.observe({key:dict(value=100,epoch=3)},130)
    with pytest.raises(ValueError):t.number(key,130)  # new baseline only
    t.observe({key:dict(value=100,epoch=4)},140)
    assert t.number(key,140)==100 and t.fields[key][2]==130


@pytest.mark.parametrize('row', [dict(value=50,epoch=2),
    dict(value=100,epoch=1), dict(value=float('nan'),epoch=3)])
def test_conflict_rollback_or_invalid_value_never_falls_back_to_old_http(row):
    t=Telemetry();key='maxrelmodlvl'
    t.observe({key:dict(value=100,epoch=1)},100)
    t.observe({key:dict(value=100,epoch=2)},110)
    t.observe({key:row},120)
    t.observe({},125)
    with pytest.raises(ValueError):t.number(key,125)


def test_long_http_field_gap_requires_advancing_baseline_again():
    t=Telemetry();key='maxrelmodlvl'
    t.observe({key:dict(value=100,epoch=1)},100)
    t.observe({key:dict(value=100,epoch=2)},110)
    t.observe({'chenable':dict(value='On',epoch=50)},200)
    t.observe({key:dict(value=100,epoch=3)},201)
    assert t.http_issues[key]=='observation_gap'
    with pytest.raises(ValueError):t.number(key,201)
    t.observe({key:dict(value=100,epoch=4)},211)
    assert t.number(key,211)==100 and t.fields[key][2]==201


def test_cached_partial_http_snapshot_cannot_refresh_field_receipt():
    from custom_components.sat.ally.monitor import Snapshot
    t=Telemetry();key='maxrelmodlvl'
    t.observe(Snapshot({key:dict(value=100,epoch=1)},100),105)
    t.observe(Snapshot({key:dict(value=100,epoch=2)},110),115)
    partial=Snapshot({'chenable':dict(value='On',epoch=3)},120)
    for now in (125,150,191):
        t.observe(partial,now)
        assert t.fields[key]==(2,100,100) and t.http_seen[key]==110
    with pytest.raises(ValueError):t.number(key,191)


def test_ramp_limit_restart_delay_and_unknown_stop():
    ramp=Ramp(41,50,1000)
    heat=Decision(True,80,{}, {},False)
    assert ramp.target(heat,1179) is None
    assert ramp.target(heat,1180)==41
    assert ramp.target(heat,1240)==43
    assert ramp.target(heat,10000)==45  # long pauses cannot cause a large jump
    ramp.target(Decision(None,None,{}, {},True),10001)
    assert not ramp.on and ramp.target(heat,10100) is None


def test_sustained_correction_resets_with_session_and_engine():
    p=Policy(minimum_water=41, maximum_water=50, water_warm=41, water_cold=45)
    e=Engine({'a':'WR'},p)
    for t in range(1000,2201,30):
        f=frame(92)
        for r in f['heads']['a'].values(): r['observed_at']=ts(t)
        f['outside']['observed_at']=ts(t)
        d=e.evaluate(f,t)
    assert d.corrections['WR']==2
    f['session_id']='new'
    f['session_started_at']=ts(2230)
    for r in f['heads']['a'].values(): r['observed_at']=ts(2230)
    f['outside']['observed_at']=ts(2230)
    assert e.evaluate(f,2230).corrections['WR']==0
    e.reset()
    assert e.evaluate(f,2230).corrections['WR']==0


def saved_reports():
    r=reports()
    r.accept(event(heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=92,valid=True)),1000)
    return r.snapshot(1010)


def test_cache_needs_live_continuity_not_new_hr():
    r=Reports(reports().mapping,1100)
    assert r.stage_restore(saved_reports(),1100,3600)
    assert not r.heads
    assert not r.accept(event(seq=2,at=1101),1101,True)
    assert not r.heads
    assert r.accept(event(seq=2,at=1102),1102)
    assert r.heads['a']['heat_required']['observed_at']==ts(1000)
    assert r.heads['a']['pi']['observed_at']==ts(1102)
    assert r.restored_fields=={('a','heat_required')}


@pytest.mark.parametrize('change', ['gap','session','invalid','expired','future','mapping'])
def test_cache_rejects_loss_invalidation_and_bad_metadata(change):
    d=saved_reports();r=Reports(reports().mapping,1100)
    if change=='future':d['saved_at']=1200
    if change=='mapping':d['mapping']={}
    if change in ('future','mapping'):
        assert not r.stage_restore(d,1100,3600);return
    assert r.stage_restore(d,1100,3600)
    e=event(seq=3 if change=='gap' else 2,at=1102)
    if change=='session':e.update(session_id='00000000-0000-0000-0000-000000000002',session_started_at=ts(1100))
    if change=='invalid':e['fields']={'heat_required':dict(value=None,valid=False)}
    if change=='expired':e['received_at']=ts(5000)
    r.accept(e,5000 if change=='expired' else 1102)
    assert r.heads.get('a',{}).get('heat_required',{}).get('value') is not True


def test_saved_invalid_field_and_global_invalidation_do_not_resurrect():
    r=reports();r.accept(event(heat_required=dict(value=True,valid=True)),1000)
    r.accept(event(seq=2,at=1001,heat_required=dict(value=None,valid=False)),1001)
    restored=Reports(r.mapping,1100)
    assert restored.stage_restore(r.snapshot(1010),1100,3600)
    restored.accept(event(seq=3,at=1102),1102)
    assert restored.heads['a']['heat_required']['value'] is None
    r.invalidate(1002)
    restored=Reports(r.mapping,1100)
    # No pre-invalidation values in the persisted snapshot.
    assert not restored.stage_restore(r.snapshot(1010),1100,3600)


def test_cache_duplicate_does_not_activate_or_extend_age():
    r=Reports(reports().mapping,1100);r.stage_restore(saved_reports(),1100,3600)
    assert not r.accept(event(seq=1,at=1000),1101)
    assert not r.heads
    r.accept(event(seq=2,at=1102),1102)
    at=r.heads['a']['heat_required']['observed_at']
    assert not r.accept(event(seq=2,at=1102),1110)
    assert r.heads['a']['heat_required']['observed_at']==at


def source_snapshot(events, emitted):
    heads = {}
    for e in events:
        row = heads.setdefault(e['device_ieee'], {})
        for key, f in e['fields'].items():
            row[key] = dict(**f, observed_at=e['received_at'], sequence=e['sequence'],
                            evidence='zcl_' + e['message_type'])
    last = deepcopy(events[-1])
    return dict(schema_version=1, complete=True, session_id=last['session_id'],
                session_started_at=last['session_started_at'], sequence=last['sequence'],
                emitted_at=ts(emitted), heads=heads, last_event=last)


def test_source_snapshot_restores_all_eleven_after_restart_and_repairs_gap():
    mapping = {f'0x{i:016x}': f'head{i}' for i in range(1,12)}
    events = []
    for i, ieee in enumerate(mapping,1):
        e=event(seq=i,at=1000+i,pi_heating_demand=dict(value=99,valid=True),
                heat_required=dict(value=i!=2,valid=True));e['device_ieee']=ieee;events.append(e)
    a=Reports(mapping,1100); b=Reports(mapping,1100)
    snapshot=source_snapshot(events,1101)
    for r in (a,b):
        snapshot["emitted_at"]=ts(1101)
        assert r.accept_snapshot(snapshot,1102,3900)
        assert len(r.heads)==11
        assert r.heads['head1']['heat_required']['observed_at']==ts(1001)
        assert r.heads['head2']['heat_required']['value'] is False
        assert r.restore_source=='verified_source_snapshot'
        # Same complete heartbeat cannot refresh the original timestamp.
        snapshot['emitted_at']=ts(1110)
        assert r.accept_snapshot(snapshot,1110,3900)
        assert r.heads['head1']['pi']['observed_at']==ts(1001)
    assert a.heads==b.heads
    # Missing event12 included a real HR=false. Complete13 repairs the gap.
    e=event(seq=12,at=1120,heat_required=dict(value=False,valid=True));events.append(e)
    e=event(seq=13,at=1121);events.append(e)
    assert a.accept(e,1121)
    assert a.heads['head1'].get('heat_required') is None
    assert a.accept_snapshot(source_snapshot(events,1122),1122,3900)
    assert a.heads['head1']['heat_required']['value'] is False


@pytest.mark.parametrize('bad',['retained','stale','future','preboot','invalid','watermark','type'])
def test_source_snapshot_untrusted_inputs_rejected(bad):
    r=Reports({'0x0000000000000001':'a'},1050)
    snap=source_snapshot([event()],1060)
    if bad=='stale':snap['emitted_at']=ts(1051)
    if bad=='future':snap['emitted_at']=ts(1200)
    if bad=='preboot':snap['emitted_at']=ts(1040)
    if bad=='invalid':snap['heads']['0x0000000000000001']['pi_heating_demand']['valid']=False
    if bad=='watermark':snap['last_event']['fields']['pi_heating_demand']['value']=20
    if bad=='type':snap['heads']['0x0000000000000001']['pi_heating_demand']['value']=True
    try: ok=r.accept_snapshot(snap,1120,3900,bad=='retained')
    except (ValueError,TypeError,KeyError):ok=False
    if bad in ('invalid','watermark','type'):
        assert ok and r.errors=={'a':'invalid_report'}
        assert demand_from_reports(r,1120).heat is None
    else:
        assert not ok and r.heads=={}


def test_source_snapshot_expiry_invalidity_and_newer_live_win():
    r=reports();e=event(heat_required=dict(value=True,valid=True));snap=source_snapshot([e],1001)
    assert r.accept_snapshot(snap,1001,50)
    invalid=event(seq=2,at=1002,heat_required=dict(value=None,valid=False))
    assert r.accept(invalid,1002)
    assert not r.accept_snapshot(snap,1003,50)
    fresh=source_snapshot([e,invalid],1003)
    assert r.accept_snapshot(fresh,1003,50)
    assert r.heads['a']['heat_required']['value'] is None
    broken=deepcopy(fresh);broken['heads']['0x0000000000000001']['heat_required']['value']=True
    broken['heads']['0x0000000000000001']['heat_required']['valid']=True
    assert r.accept_snapshot(broken,1004,50)
    assert r.heads['a']['heat_required']['value'] is None
    fresh['emitted_at']=ts(1100)
    assert r.accept_snapshot(fresh,1100,50)
    assert r.heads['a']=={}
    r.invalidate(1101)
    assert not r.accept_snapshot(fresh,1102,50)
    fresh['emitted_at']=ts(1103)
    assert r.accept_snapshot(fresh,1103,50)
    assert r.heads['a']=={}


def test_observed_oct21_all_head_values_keep_individual_expiry():
    # Values/times observed in Influx at 11:45:48Z; a synthetic complete source
    # envelope tests policy only, NOT a claim that archive gaps were verified.
    from custom_components.sat.ally.control import stamp
    rows=[('PR1',1,False,'10:47:38.355','11:42:01.315'),
          ('PR2',1,False,'11:38:48.576','11:45:48.604'),
          ('WR',99,True,'11:39:00.292','11:33:47.057'),
          ('KITCHEN',96,True,'10:54:55.562','11:16:48.345'),
          ('BRTV',1,False,'10:55:20.786','11:10:37.092'),
          ('BR',96,True,'11:27:35.685','10:51:08.221'),
          ('LR',100,True,'11:45:39.690','11:16:12.386'),
          ('BAPP',100,True,'11:24:08.846','10:34:09.000'),
          ('SU',99,True,'11:31:14.278','11:32:24.749'),
          ('DR',100,True,'11:24:42.948','10:34:43.561'),
          ('TV',100,True,'11:30:18.266','11:29:18.278')]
    timestamp=lambda s:stamp('2026-10-21T'+s+'Z')
    mapping={f'0x{i:016x}':row[0] for i,row in enumerate(rows,1)}
    records=[]
    for ieee,(name,pi,hr,pt,ht) in zip(mapping,rows):
        for key,value,at in [('pi_heating_demand',pi,pt),('heat_required',hr,ht)]:
            e=event(at=timestamp(at),start=timestamp('08:00:00'),**{key:dict(value=value,valid=True)})
            e['device_ieee']=ieee;records.append(e)
    records.sort(key=lambda e:e['received_at'])
    for i,e in enumerate(records,1):e['sequence']=i
    wall=timestamp('11:45:49');r=Reports(mapping,timestamp('11:35:00'))
    assert r.accept_snapshot(source_snapshot(records,wall),wall,3900)
    assert 'heat_required' not in r.heads['BAPP'] and 'heat_required' not in r.heads['DR']
    assert r.heads['WR']['heat_required']['observed_at']=='2026-10-21T11:33:47.057000+00:00'
    frame={'session_id':'source','session_started_at':ts(r.epoch),'heads':deepcopy(r.heads),
           'outside':reading(12.6,wall,'otgw_report')}
    for row in frame['heads'].values():row['eligible']=reading(True,wall,'policy_evaluation')
    decision=Engine({name:name for name in mapping.values()},Policy(ttl=3900)).evaluate(frame,wall)
    assert decision.heat is True and decision.degraded and decision.water<=50
    assert decision.reasons['PR1']=='head_requests_no_heat'
    assert decision.reasons['BAPP']=='heat_required_unknown'
    assert decision.reasons['WR']=='valid_demand'

@pytest.mark.parametrize('cause',['disconnect','sequence_gap'])
def test_snapshot_repair_changes_consumer_epoch_identity_once(cause):
    """A partial report may be evaluated before the complete snapshot repairs it."""
    r=reports();engine=Engine({'a':'A'})
    e1=event(at=1000,heat_required=dict(value=True,valid=True),pi_heating_demand=dict(value=99,valid=True))
    assert r.accept(e1,1000)
    if cause=='disconnect':r.invalidate(1010)
    # Gap means a lost HR report; disconnect means all prior evidence was invalidated.
    e2=event(seq=3 if cause=='sequence_gap' else 2,at=1020,pi_heating_demand=dict(value=98,valid=True))
    assert r.accept(e2,1020)
    def evaluate(now):
        heads=deepcopy(r.heads);heads['a']['eligible']=reading(True,now,'policy_evaluation')
        return engine.evaluate(dict(session_id=f'{r.session}:{r.segment}',session_started_at=ts(r.epoch),
             heads=heads,outside=reading(5,now,'otgw_report')),now)
    assert evaluate(1020).heat is None
    before=r.segment
    snapshot=source_snapshot([e1,e2],1021)
    assert r.accept_snapshot(snapshot,1021,3900)
    assert evaluate(1021).heat is True
    assert r.segment==before+1
    assert r.heads['a']['heat_required']['observed_at']==ts(1000)
    repaired=r.segment
    snapshot['emitted_at']=ts(1022)
    assert r.accept_snapshot(snapshot,1022,3900)
    assert evaluate(1022).heat is True and r.segment==repaired
    # No re-aging: once TTL expires, the same source snapshot cannot restore heat.
    snapshot['emitted_at']=ts(5000)
    assert r.accept_snapshot(snapshot,5000,3900)
    assert not r.heads['a']


def oct24_brtv_events():
    """Actual source values/times; see heating-night-observer.md for provenance.

    Only BRTV is replayed. Synthetic complete envelopes below are NOT a claim
    that this fixture contains the intervening reports from other devices.
    """
    from custom_components.sat.ally.control import stamp
    rows = [(110, '19:49:20.175', 'heat_required', True),
            (138, '20:05:34.809', 'pi_heating_demand', 9),
            (196, '20:44:01.622', 'heat_required', False)]
    result = []
    for seq, clock, key, value in rows:
        e = event(seq=seq, at=stamp('2026-10-24T'+clock+'Z'),
                  start=stamp('2026-10-24T18:37:41.983Z'),
                  sid='6fa1159e-e492-4eac-bdc6-3f0017ff95ae',
                  **{key: dict(value=value, valid=True)})
        e['device_ieee'] = '0x0000000000f00006'
        result.append(e)
    return result


def test_oct24_brtv_low_demand_and_new_false_agree_in_engine_and_shadow():
    from custom_components.sat.ally.control import stamp
    from custom_components.sat.ally.shadow import Observer
    members = {'0x0000000000f00006': {
        'name': 'brtv', 'zone': 'BRTV', 'eligible_entity': 'binary_sensor.brtv'}}
    policy = Policy(ttl=3900, minimum_water=41, water_warm=41, maximum_water=55)
    start = stamp('2026-10-24T20:20:00Z')
    observer = Observer(members, 'binary_sensor.global', start-1, policy)
    engine = Engine({'brtv': 'BRTV'}, policy)
    states = {'binary_sensor.global': 'on', 'binary_sensor.brtv': 'on'}
    events = oct24_brtv_events()
    assert observer.reports.accept_snapshot(source_snapshot(events[:2], start), start, policy.ttl)

    def step(now):
        heads = deepcopy(observer.reports.heads)
        heads['brtv']['eligible'] = reading(True, now, 'policy_evaluation')
        f = dict(session_id='replay', session_started_at=ts(observer.reports.epoch),
                 heads=heads, outside=reading(10, now, 'otgw_report'))
        # Fresh synthetic OTGW evidence isolates demand; no transport/boiler replay.
        monitor = {k: dict(value=v, epoch=int(now-start)+1) for k, v in dict(
            outsidetemperature=10, boilertemperature=41,
            faultindicator='Off', dhwmode='Off').items()}
        return engine.evaluate(f, now), observer.evaluate(states, monitor, now, now-start)

    step(start)  # First OTGW receipt establishes the observer's baseline.
    for offset in range(30, 1441, 30):
        active, shadow = step(start+offset)
        assert active.reasons['brtv'] == shadow['reasons']['brtv'] == 'valid_demand'
        # The initial wait in this replay is synthetic, not a reconstructed reset.
        if offset < 600:
            assert active.heat is False and shadow['status'] == 'off'
        elif offset >= 630:
            assert active.heat is True and shadow['status'] == 'heat'
            assert active.water == shadow['proposed_water'] <= 55
    original_hr = observer.reports.heads['brtv']['heat_required']['observed_at']
    assert original_hr == events[0]['received_at']
    now = stamp('2026-10-24T20:44:07.146550Z')  # HA archive receipt, not source time.
    assert observer.reports.accept_snapshot(source_snapshot(events, now), now, policy.ttl)
    active, shadow = step(now)
    assert active.heat is False and shadow['status'] == 'off'
    assert active.reasons['brtv'] == shadow['reasons']['brtv'] == 'head_requests_no_heat'
    assert observer.reports.heads['brtv']['heat_required']['observed_at'] == events[-1]['received_at']
    assert observer.reports.heads['brtv']['pi']['observed_at'] == events[1]['received_at']


@pytest.mark.parametrize('other_demand', [False, True])
def test_oct24_brtv_original_hr_expiry_never_blocks_another_valid_zone(other_demand):
    from custom_components.sat.ally.control import stamp
    events = oct24_brtv_events()
    # Counterfactual boundary: omit the observed HR=false to test expiry alone.
    boundary = stamp(events[0]['received_at'])+3900
    engine = Engine({'brtv': 'BRTV', 'other': 'OTHER'}, Policy(ttl=3900))
    f = dict(session_id='replay', session_started_at=events[0]['session_started_at'],
             heads={'brtv': {
                 'pi': reading(9, stamp(events[1]['received_at'])),
                 'heat_required': reading(True, stamp(events[0]['received_at']))}},
             outside=reading(10, boundary, 'otgw_report'))
    for now in (boundary, boundary+.001):
        f['heads']['brtv']['eligible'] = reading(True, boundary, 'policy_evaluation')
        f['heads']['other'] = dict(pi=reading(80, boundary), heat_required=reading(other_demand, boundary),
                                  eligible=reading(True, boundary, 'policy_evaluation'))
        decision = engine.evaluate(f, now)
        assert decision.reasons['brtv'] == ('valid_demand' if now == boundary else 'heat_required_unknown')
    assert decision.heat is (True if other_demand else None)
    assert decision.degraded  # Expired is unknown, never an invented HR=false.

# hodko.44: one TRV cannot erase other rooms' verified demand.
def multi_reports():
    r=Reports({'0x0000000000000001':'a','0x0000000000000002':'b'},950)
    events=[]
    for seq,ieee in enumerate(r.mapping,1):
        e=event(seq=seq,at=1000+seq,pi_heating_demand=dict(valid=True,value=99),
                heat_required=dict(valid=True,value=True));e['device_ieee']=ieee
        r.accept(e,1000+seq);events.append(e)
    return r,events


def demand_from_reports(r, at, allowed=True):
    heads=deepcopy(r.heads)
    for name in r.mapping.values():
        heads.setdefault(name,{})['eligible']=reading(allowed,at,'policy_evaluation')
    return Engine({n:n for n in r.mapping.values()},Policy(ttl=3900)).evaluate(
        dict(session_id=f'{r.session}:{r.segment}',session_started_at=ts(r.epoch),heads=heads,
             outside=reading(5,at,'otgw_report')),at)


@pytest.mark.parametrize('bad_payload',[False,True])
def test_unknown_trv_consumes_watermark_without_reset_or_membership(bad_payload):
    r,events=multi_reports();before=deepcopy(r.heads);epoch=r.epoch;segment=r.segment
    e=event(seq=3,at=1003);e['device_ieee']='0x0000000000f0000a'
    if bad_payload:e['fields']=None
    assert r.accept(e,1003) and r.seq==3 and r.heads==before
    assert r.epoch==epoch and r.segment==segment and r.ignored_reports==1
    assert not r.accept(e,1004) and r.ignored_reports==1
    assert r.accept(event(seq=4,at=1005),1005)
    assert r.heads['b']==before['b'] and demand_from_reports(r,1005).heat is True
    assert not r.accept(e,1006,retained=True)
    # Roundtrip may end on an unknown member's watermark.
    r,events=multi_reports();r.accept(e,1003)
    restored=Reports(r.mapping,1010);assert restored.stage_restore(r.snapshot(1004),1010,3900)
    assert restored.accept(event(seq=4,at=1011),1011)
    assert restored.heads['b']==before['b']


@pytest.mark.parametrize('fault',['pi_range','pi_bool','hr_type','validity','fields','missing','message_type','missing_value'])
def test_bad_member_isolated_and_not_resurrected_by_partial_or_snapshot(fault):
    r,events=multi_reports();good=deepcopy(r.heads['b']);epoch=r.epoch
    bad=event(seq=3,at=1003,pi_heating_demand=dict(value=99,valid=True),heat_required=dict(value=True,valid=True))
    if fault=='pi_range':bad['fields']['pi_heating_demand']['value']=101
    if fault=='pi_bool':bad['fields']['pi_heating_demand']['value']=True
    if fault=='hr_type':bad['fields']['heat_required']['value']='true'
    if fault=='validity':bad['fields']['pi_heating_demand']['valid']=False
    if fault=='missing_value':bad['fields']['heat_required']={'valid':False}
    if fault=='fields':bad['fields']=[]
    if fault=='missing':bad.pop('fields')
    if fault=='message_type':bad['message_type']='deviceAnnounce'
    assert r.accept(bad,1003) and r.heads['b']==good and r.epoch==epoch
    assert r.errors=={'a':'invalid_report'}
    assert demand_from_reports(r,1003).heat is True
    # Cache cannot restore old positive HR; subsequent PI alone is insufficient.
    restored=Reports(r.mapping,1010);assert restored.stage_restore(r.snapshot(1004),1010,3900)
    restored.accept(event(seq=4,at=1011),1011)
    assert restored.heads['a']['heat_required']['value'] is None
    assert restored.heads['b']==good
    assert restored.accept(event(seq=5,at=1012,heat_required=dict(value=True,valid=True)),1012)
    assert not restored.errors and demand_from_reports(restored,1012).heat is True
    # Replaying the last good snapshot must not roll the stream back.
    assert not r.accept_snapshot(source_snapshot(events,1005),1005,3900)
    assert r.heads['a']['heat_required']['value'] is None
    assert demand_from_reports(r,5000).heat is None  # no infinite last-value hold
    assert demand_from_reports(r,1006,False).heat is False


@pytest.mark.parametrize('fault',['value','timestamp','missing_head','fields','watermark','foreign'])
def test_snapshot_one_bad_row_keeps_other_room(fault):
    r,events=multi_reports();good=deepcopy(r.heads['b'])
    snap=source_snapshot(events,1003)
    if fault=='value':snap['heads'][events[0]['device_ieee']]['pi_heating_demand']['value']=999
    if fault=='timestamp':snap['heads'][events[0]['device_ieee']]['heat_required']['observed_at']=ts(5000)
    if fault=='missing_head':del snap['heads'][events[0]['device_ieee']]
    if fault=='fields':snap['heads'][events[0]['device_ieee']]=None
    if fault=='watermark':
        # Final event belongs to A; mismatch must not discard B.
        last=event(seq=3,at=1003);events.append(last);snap=source_snapshot(events,1004)
        snap['last_event']['fields']['pi_heating_demand']['value']=1
    if fault=='foreign':snap['heads']['0xffffffffffffffff']={'garbage':None}
    assert r.accept_snapshot(snap,1004,3900)
    assert r.heads['b']==good and demand_from_reports(r,1004).heat is True
    if fault!='foreign':assert 'a' in r.errors


def test_duplicate_conflict_is_local_and_complete_snapshot_cannot_revive_it():
    r,events=multi_reports();before=deepcopy(r.heads['a']);bad=deepcopy(events[-1])
    bad['fields']['heat_required']['value']=False
    assert r.accept(bad,1003) and r.heads['a']==before and r.errors=={'b':'invalid_report'}
    assert r.accept_snapshot(source_snapshot(events,1004),1004,3900)
    assert r.heads['b']['heat_required']['value'] is None
    assert r.heads['a']==before and demand_from_reports(r,1004).heat is True


def test_oct28_tv2_minute_stream_leaves_original_demand_and_epoch_intact():
    # Actual reported event identity/time/PI from 28 Oct 16:20 CET; other
    # envelopes are a deterministic replay fixture, not new physical evidence.
    base=datetime.fromisoformat('2026-10-28T15:20:01.030+00:00').timestamp()
    r=Reports({'0x0000000000f00004':'wr'},base-600)
    first=event(seq=205,at=base-5,start=base-1000,pi_heating_demand=dict(value=100,valid=True),heat_required=dict(value=True,valid=True))
    first['device_ieee']='0x0000000000f00004';r.accept(first,base-5)
    proof=deepcopy(r.heads);epoch=r.epoch;segment=r.segment
    for i in range(10):
        e=event(seq=206+i,at=base+i*60,start=base-1000,pi_heating_demand=dict(value=10+i,valid=True))
        e['device_ieee']='0x0000000000f0000a';r.accept(e,base+i*60)
        assert r.heads==proof and r.epoch==epoch and r.segment==segment
        assert demand_from_reports(r,base+i*60).heat is True

@pytest.mark.parametrize('bad_index',range(12))
def test_each_of_twelve_heads_can_fail_without_blocking_healthy_room(bad_index):
    mapping={f'0x{i+1:016x}':f'head{i}' for i in range(12)}
    r=Reports(mapping,950)
    for i,ieee in enumerate(mapping):
        e=event(seq=i+1,at=1000+i,pi_heating_demand=dict(value=99,valid=True),heat_required=dict(value=True,valid=True));e['device_ieee']=ieee
        r.accept(e,1000+i)
    bad=event(seq=13,at=1013);bad['device_ieee']=list(mapping)[bad_index];bad['fields']={'heat_required':{'valid':True,'value':'fault'}}
    before=deepcopy(r.heads);r.accept(bad,1013)
    assert demand_from_reports(r,1013).heat is True
    for i,name in enumerate(mapping.values()):
        if i==bad_index:assert r.heads[name]['heat_required']['value'] is None
        else:assert r.heads[name]==before[name]


def test_same_tv_zone_uses_remaining_valid_head_and_max_not_sum():
    r,events=multi_reports()
    def evaluate():
        f=dict(session_id=r.session,session_started_at=ts(r.epoch),heads=deepcopy(r.heads),outside=reading(5,1003,'otgw_report'))
        for n in f['heads']:f['heads'][n]['eligible']=reading(True,1003,'policy_evaluation')
        return Engine({'a':'TV','b':'TV'}).evaluate(f,1003)
    before=evaluate()
    bad=event(seq=3,at=1003);bad['device_ieee']='0x0000000000000002';bad['fields']={}
    r.accept(bad,1003);after=evaluate()
    assert before.zones==after.zones=={'TV':True}
    assert before.water==after.water and after.degraded
