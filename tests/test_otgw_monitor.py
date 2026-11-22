"""Shared OTGW HTTP transport, entirely fake sockets/session."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from custom_components.sat.ally import monitor as m
from custom_components.sat.ally.runtime import Telemetry

URL='http://test.invalid/api/v2/otgw/otmonitor'

@pytest.fixture
def env(hass,monkeypatch):
    clock=[100.0]
    monkeypatch.setattr(m,'monotonic',lambda:clock[0])
    monkeypatch.setattr(m,'uniform',lambda a,b:0.5)
    response=SimpleNamespace(status=200,headers={},json=AsyncMock(return_value={
        'otmonitor':{'chenable':{'value':'Off','epoch':1},
                     'controlsetpoint':{'value':10,'epoch':1}}}))
    context=AsyncMock();context.__aenter__.return_value=response
    session=SimpleNamespace(get=Mock(return_value=context))
    monkeypatch.setattr(m,'async_get_clientsession',lambda h:session)
    return m.shared_reader(hass,URL),clock,response,session

async def test_concurrent_clients_single_get_and_isolated_snapshots(env,hass):
    r,clock,response,session=env
    assert r is m.shared_reader(hass,'http://TEST.INVALID:80/api/v2/otgw/otmonitor')
    a,b=await asyncio.gather(r.read(),r.read())
    assert session.get.call_count==1
    a['chenable']['value']='On'
    assert b['chenable']['value']=='Off'
    assert a.acquired_at==b.acquired_at==100

async def test_cache_does_not_renew_telemetry_or_ack_time(env):
    r,clock,response,session=env
    t=Telemetry()
    t.observe(await r.read(),100)
    clock[0]=103
    response.json.return_value['otmonitor']['chenable']['epoch']=2
    t.observe(await r.read(),103)
    assert t.fields['chenable'][2]==100
    clock[0]=104
    t.observe(await r.read(),104)
    assert t.last==103
    assert t.fields['chenable'][2]==100
    # Subsequent advance uses actual acquisition time103, not cached access104.
    clock[0]=106
    response.json.return_value['otmonitor']['chenable']['epoch']=3
    t.observe(await r.read(),106)
    assert t.fields['chenable'][2]==103

@pytest.mark.parametrize('header,body,delay',[('7',{'retry_after':9},9.5),
    (None,{'retry_after':4},4.5),('bad',{},2.5)])
async def test_429_shared_backoff_no_stale_fallback(env,header,body,delay):
    r,clock,response,session=env
    await r.read()
    clock[0]=103
    response.status=429;response.headers={'Retry-After':header}
    response.json.return_value=body
    with pytest.raises(m.MonitorUnavailable,match='429'):await r.read()
    assert r.next_request==103+delay
    with pytest.raises(m.MonitorUnavailable,match='backoff'):await r.read()
    assert session.get.call_count==2
    clock[0]=r.next_request
    response.status=200;response.json.return_value={'otmonitor':{}}
    assert await r.read()=={}
    assert session.get.call_count==3

async def test_cancellation_does_not_leave_task_or_retry_storm(env):
    r,clock,response,session=env
    entered=asyncio.Event()
    async def blocked():
        entered.set()
        await asyncio.Event().wait()
    response.json.side_effect=blocked
    task=asyncio.create_task(r.read())
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    with pytest.raises(m.MonitorUnavailable,match='backoff'):await r.read()
    assert not r.lock.locked() and r.snapshot is None
    assert session.get.call_count==1

@pytest.mark.parametrize('status,payload',[(302,{}),(500,{}),(200,[]),(200,{'otmonitor':[]})])
async def test_errors_do_not_return_old_snapshot(env,status,payload):
    r,clock,response,session=env
    await r.read();clock[0]=103
    response.status=status;response.json.return_value=payload
    with pytest.raises(m.MonitorUnavailable):await r.read()
    assert r.snapshot is None
    with pytest.raises(m.MonitorUnavailable):await r.read()
    assert session.get.call_count==2

async def test_runtime_and_shadow_use_same_reader(env,hass):
    from custom_components.sat.ally.runtime import Runtime
    from custom_components.sat.ally.shadow_adapter import Adapter
    r,clock,response,session=env
    runtime=SimpleNamespace(c=SimpleNamespace(hass=hass),mqtt_namespace=None,settings={'monitor_url':URL})
    shadow=SimpleNamespace(hass=hass,settings={'monitor_url':URL})
    a,b=await asyncio.gather(Runtime.poll(runtime),Adapter.poll(shadow))
    assert a==b and a.acquired_at==b.acquired_at
    assert session.get.call_count==1
    session.get.assert_called_once_with(URL,allow_redirects=False)

async def test_timeout_backoff_and_retry(env,monkeypatch):
    r,clock,response,session=env
    monkeypatch.setattr(m,'REQUEST_TIMEOUT',0.01)
    response.json.side_effect=lambda:None
    async def slow():await asyncio.sleep(1)
    response.json.side_effect=slow
    with pytest.raises(TimeoutError):await r.read()
    assert r.snapshot is None
    with pytest.raises(m.MonitorUnavailable):await r.read()
    clock[0]=103
    response.json.side_effect=None
    assert await r.read()

def test_retry_after_invalid_values_are_bounded():
    assert m.retry_delay('NaN',{'retry_after':-1})>=2

async def test_original_http_error_survives_backoff(env):
    r,clock,response,session=env
    response.status=503
    with pytest.raises(m.MonitorUnavailable):await r.read()
    assert r.last_error=='OTGW monitor HTTP 503'
    with pytest.raises(m.MonitorUnavailable,match='backoff'):await r.read()
    assert r.last_error=='OTGW monitor HTTP 503'


def counter_snapshot(epoch, at, value='On'):
    return m.Snapshot({'chenable': dict(epoch=epoch, value=value)}, at, 65535)


async def test_reader_recognizes_compact_format_and_preserves_it_in_cache(env):
    r,clock,response,session=env
    data=response.json.return_value['otmonitor']
    data['sensorsimulation']=dict(epoch=1794910000,value=False)
    data['chenable']['epoch']=65530
    first=await r.read()
    assert first.epoch_modulus==65535
    clock[0]=101
    cached=await r.read()
    assert cached.epoch_modulus==65535 and cached.acquired_at==100
    clock[0]=103
    del data['chenable'];del data['sensorsimulation']
    assert (await r.read()).epoch_modulus==65535  # Partial payload keeps format.
    clock[0]=106
    data.update(chenable=dict(epoch=1794910006,value='Off'),
                sensorsimulation=dict(epoch=1794910006,value=False))
    assert (await r.read()).epoch_modulus is None  # Unix format remains supported.


def test_low_epoch_alone_does_not_enable_wrap_support():
    from custom_components.sat.ally.evidence import http_epoch_modulus
    assert http_epoch_modulus({'chenable':dict(epoch=65530,value='On')}) is None
    t=Telemetry()
    for epoch,at in ((65520,100),(65530,110),(5,120)):
        t.observe({'chenable':dict(epoch=epoch,value='On')},at)
    assert t.http_issues['chenable']=='epoch_reversed'
    with pytest.raises(ValueError,match='Stale'): t.value('chenable',120)


@pytest.mark.parametrize('epoch', [0,1,5])
def test_counter_wrap_uses_previous_observation_not_get_time(epoch):
    t=Telemetry()
    t.observe(counter_snapshot(65520,100),100)
    t.observe(counter_snapshot(65530,110),110)
    wrapped=counter_snapshot(epoch,120)
    t.observe(wrapped,120)
    assert t.fields['chenable']==(epoch,'On',110)
    assert t.http_counter_wraps=={'chenable':1} and not t.http_issues.get('chenable')
    for now in (121,150,200):
        t.observe(wrapped,now)  # Re-reading this cached result never renews evidence.
        assert t.fields['chenable'][2]==110
    assert t.value('chenable',200)=='On'
    with pytest.raises(ValueError,match='Stale'):t.value('chenable',200.01)


@pytest.mark.parametrize('old,new',[(600,1),(65530,600),(5,65530),(65530,65535)])
def test_counter_reset_impossible_jump_and_delayed_pre_wrap_packet_are_invalid(old,new):
    t=Telemetry()
    t.observe(counter_snapshot(old-2,100),100)
    t.observe(counter_snapshot(old,102),102)
    t.observe(counter_snapshot(new,105),105)
    with pytest.raises(ValueError,match='Stale'):t.value('chenable',105)
    assert not t.http_counter_wraps
    # A rejected frame cannot become the baseline for a subsequent ACK.
    t.observe(counter_snapshot(10,110),110)
    with pytest.raises(ValueError,match='Stale'):t.value('chenable',110)
    t.observe(counter_snapshot(12,112),112)
    assert t.fields['chenable']==(12,'On',110)


def test_counter_zero_is_not_initial_evidence_and_equal_epoch_conflict_fails_closed():
    t=Telemetry()
    for at in (100,105):
        t.observe(counter_snapshot(0,at),at)
        with pytest.raises(ValueError):t.value('chenable',at)
    t.observe(counter_snapshot(1,106),106)
    t.observe(counter_snapshot(2,107),107)
    assert t.value('chenable',107)=='On'
    t.observe(counter_snapshot(2,108,'Off'),108)
    assert t.http_issues['chenable']=='value_changed_without_epoch'
    with pytest.raises(ValueError):t.value('chenable',108)


def test_counter_missing_field_keeps_only_original_lease_and_long_gap_cannot_wrap():
    t=Telemetry()
    t.observe(counter_snapshot(65520,100),100)
    t.observe(counter_snapshot(65530,110),110)
    t.observe(m.Snapshot({},140,65535),140)
    assert t.fields['chenable']==(65530,'On',100)
    t.observe(counter_snapshot(96,211),211)
    with pytest.raises(ValueError):t.value('chenable',211)
    t.observe(counter_snapshot(98,213),213)
    assert t.fields['chenable'][2]==211
    assert not t.http_counter_wraps


def test_http_format_change_cannot_reuse_previous_receipt():
    t=Telemetry()
    for epoch,at in ((65520,100),(65530,110)):
        t.observe(counter_snapshot(epoch,at),at)
    t.observe(m.Snapshot({'chenable':dict(epoch=1794910000,value='On')},120),120)
    with pytest.raises(ValueError):t.value('chenable',120)
    t.observe(m.Snapshot({'chenable':dict(epoch=1794910005,value='On')},125),125)
    assert t.fields['chenable'][2]==120


def test_initial_slow_counter_field_does_not_need_updates_at_get_frequency():
    t=Telemetry()
    # The initial MM report may predate our first GET. Re-reading that cached
    # epoch is not a measurement, nor a reason to reject the next 60s report.
    for at in range(100,146,5):
        t.observe(counter_snapshot(1000,at),at)
        with pytest.raises(ValueError):t.value('chenable',at)
    t.observe(counter_snapshot(1060,150),150)
    assert t.fields['chenable']==(1060,'On',145)
    for at in range(155,206,5):t.observe(counter_snapshot(1060,at),at)
    assert t.fields['chenable'][2]==145
    t.observe(counter_snapshot(1120,210),210)
    assert t.fields['chenable']==(1120,'On',205)


def test_unproven_baseline_still_rejects_delayed_pre_wrap_frame():
    t=Telemetry()
    t.observe(counter_snapshot(5,100),100)
    t.observe(counter_snapshot(65530,105),105)
    assert t.http_issues['chenable']=='counter_discontinuity'
    with pytest.raises(ValueError):t.value('chenable',105)


def test_first_slow_update_across_wrap_uses_original_last_observation():
    t=Telemetry()
    for at in range(100,146,5):
        t.observe(counter_snapshot(65515,at),at)
        with pytest.raises(ValueError):t.value('chenable',at)
    t.observe(counter_snapshot(40,150),150)  # 60s MM period, 5s HTTP polling.
    assert t.fields['chenable']==(40,'On',145)
    assert t.http_counter_wraps=={'chenable':1}
    assert t.value('chenable',235)=='On'
    with pytest.raises(ValueError):t.value('chenable',235.01)


def test_first_wrap_cannot_claim_more_than_normal_telemetry_window():
    t=Telemetry()
    t.observe(counter_snapshot(65400,100),100)
    t.observe(counter_snapshot(5,105),105)  # 140s cannot qualify as fresh wrap.
    assert t.http_issues['chenable']=='counter_discontinuity'
    with pytest.raises(ValueError):t.value('chenable',105)
