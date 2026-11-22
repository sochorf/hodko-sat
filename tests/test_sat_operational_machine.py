"""HA-free operational replay against pinned, pre-extraction hodko.49 traces.

Golden generation is explicit and requires the original runtime SHA256; normal
runs only read fixtures. Synthetic timing/transport faults, no live endpoints.
"""
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace, ModuleType
import unittest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).with_name('sat_runtime49_traces.json')
BASE_SHA = '5769e728a7bf60414893bd6dd6ac5f269f5347147d7c623094c4b2d428956e86'


def load_portable():
    package = ModuleType('portable_sat_machine')
    package.__path__ = [str(ROOT/'custom_components/sat/ally')]
    sys.modules[package.__name__] = package
    return {name: importlib.import_module(f'{package.__name__}.{name}')
            for name in ('kernel', 'control', 'evidence', 'machine', 'shadow')}


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


SETTINGS = dict(schema_version=1, topic='offline/ally/events',
    monitor_url='http://test.invalid/api/v2/otgw/otmonitor', enabled_entity='allowed',
    members={'0x0000000000000001': dict(name='good', zone='TV', eligible_entity='good_allowed'),
             '0x0000000000000002': dict(name='missing', zone='WC', eligible_entity='missing_allowed')},
    policy=dict(ttl=3900, minimum_water=41, maximum_water=55, water_warm=41, water_cold=55))


class FakePorts:
    def __init__(self):
        self.now = 1000.
        self.wall_shift = 0
        self.control_lock = asyncio.Lock()
        self.closing = False
        self.host_running = True
        self.heat_requested = True
        self.connected = True
        self.maximum_setpoint = 55
        self.setpoint = 10
        self.write_timeout = 15
        self.permissions = {'allowed': True, 'good_allowed': True, 'missing_allowed': True,
                            'input_boolean.heating_season': True}
        self.values = dict(outsidetemperature=-10, chenable='Off', controlsetpoint=10,
            boilertemperature=43, faultindicator='Off', dhwmode='Off', maxrelmodlvl=100)
        self.epoch = 1
        self.commands = []
        self.read_failure = False
        self.write_failures = []
        self.veto_on_cs = False
        self.notifications = 0

    def wall_time(self): return self.now + self.wall_shift
    def monotonic(self): return self.now
    def permission(self, key): return self.permissions.get(key)
    def restore_heat(self): self.heat_requested = True
    def state_changed(self): self.notifications += 1
    def save_reports(self): pass
    def mqtt_disconnected(self, exc): return False
    def transient_read_error(self, exc): return isinstance(exc, (TimeoutError, OSError))
    async def read_telemetry(self):
        if self.read_failure: raise TimeoutError('synthetic read outage')
        self.epoch += 1
        return self.raw()
    def raw(self): return {k: dict(value=v, epoch=self.epoch) for k,v in self.values.items()}
    async def send(self, command):
        self.commands.append([self.now, command.kind, command.value])
        if command.kind == 'cs':
            if self.veto_on_cs:
                self.heat_requested = False
                self.veto_on_cs = False
            if self.write_failures:
                self.now += self.write_failures.pop(0)
                raise TimeoutError('synthetic ambiguous CS publish')


class LegacyClimate:
    """Only the old adapter's input API, no emulated decision logic."""
    def __init__(self, ports, module):
        self.ports = ports
        self._config_entry = SimpleNamespace(data={'mode':'fake'}, options={}, entry_id='offline')
        self._radiators = []
        self.areas = {}
        self.thermostat = None
        self._push_setpoint_to_thermostat = False
        self._maximum_relative_modulation = 100
        async def ch(value): await ports.send(SimpleNamespace(kind='ch', value=value == module.DeviceState.ON))
        async def cs(value): await ports.send(SimpleNamespace(kind='cs', value=value))
        async def mm(value): await ports.send(SimpleNamespace(kind='mm', value=value))
        self._coordinator = SimpleNamespace(maximum_setpoint=55, async_set_heater_state=ch,
            async_set_control_setpoint_only=cs, async_set_control_max_relative_modulation=mm)
        def state(key):
            value = ports.permission(key)
            return None if value is None else SimpleNamespace(state='on' if value else 'off')
        self.hass = SimpleNamespace(is_running=True, states=SimpleNamespace(
            get=state, is_state=lambda key,value: state(key) is not None and state(key).state == value))
    @property
    def hvac_mode(self): return 'heat' if self.ports.heat_requested else 'off'
    @property
    def _hvac_mode(self): return self.hvac_mode
    @_hvac_mode.setter
    def _hvac_mode(self, value): self.ports.heat_requested = value == 'heat'
    @property
    def _control_lock(self): return self.ports.control_lock
    @property
    def _control_closing(self): return self.ports.closing
    @property
    def _setpoint(self): return self.ports.setpoint
    @_setpoint.setter
    def _setpoint(self, value): self.ports.setpoint = value
    def async_write_ha_state(self): self.ports.state_changed()


def report(now, seq=1, valid=True):
    return dict(schema_version=2, session_id='00000000-0000-0000-0000-000000000001',
        session_started_at=stamp(900), sequence=seq, received_at=stamp(now),
        device_ieee='0x0000000000000001', message_type='attributeReport',
        fields=dict(pi_heating_demand=dict(value=100, valid=True),
                    heat_required=dict(value=True if valid else None, valid=valid)))


def snapshot(r, p):
    names=('status','tripped','pending','last_ack','last_cs_refresh','last_stop_sent',
           'stop_publish_pending','stop_confirmation_after','command_writes',
           'cs_retry_attempts','cs_retry_successes','cs_publish_failures','telemetry_read_failures',
           'modulation_confirmed','modulation_since','boot_verify_deadline','boot_verify_attempts',
           'startup_rearm_after','startup_rearm_barrier','startup_rearm_pending','startup_rearm_attempts',
           'command_recovery_attempts','season_start_pending','startup_ready','restart_restored')
    state = {name:deepcopy(getattr(r,name)) for name in names}
    state.update(commands=deepcopy(p.commands), target=p.setpoint, heat=p.heat_requested,
                 dhw=deepcopy(r.dhw_recovery.__dict__), reports=deepcopy(r.reports.heads),
                 ramp=dict(on=r.ramp.on,water=r.ramp.water), notifications=p.notifications)
    state['dhw']['limits'] = asdict(r.dhw_recovery.limits)
    # JSON normalization (tuples -> arrays); preserve unrounded times/float values.
    return json.loads(json.dumps(state,sort_keys=True))


SCENARIOS = {
    'start_and_valid_reports': [{}, {'advance':15,'ack':True}, {'advance':15,'ack':True,'report':2}, {'advance':15,'ack':True,'report':3}],
    'invalid_other_member': [{}, {'advance':15,'ack':True,'bad_other':True}, {'advance':15,'ack':True}],
    'boot_and_season': [{'boot':True}, {'advance':15}, {'advance':15}, {'advance':15,'ack':True}],
    'publish_is_not_ack': [{}, {'advance':30}, {'advance':30}, {'advance':31}, {'advance':15}, {'advance':91}, {'advance':15}],
    'short_read_outage': [{}, {'advance':15,'ack':True}, {'advance':15,'read_failure':True}, {'advance':10,'ack':True}],
    'expired_read_evidence': [{}, {'advance':15,'ack':True}, {'advance':100,'read_failure':True}, {'advance':15}],
    'cs_retry': [{}, {'advance':15,'ack':True}, {'advance':15,'ack':True,'write_failures':[15]}, {'advance':10,'ack':True}],
    'cs_late_completion': [{}, {'advance':15,'ack':True}, {'advance':15,'ack':True,'write_failures':[60]}, {'advance':15}],
    'dhw_timeout_and_return': [{}, {'advance':15,'ack':True}, {'advance':15,'values':{'dhwmode':'On','boilertemperature':77}},
        {'advance':15,'ack':True}, {'advance':15,'values':{'dhwmode':'Off'}}, {'advance':15,'read_failure':True},
        {'advance':15,'ack':True},
        *[{'advance':15,'ack':True,'values':{'boilertemperature':76}} for _ in range(12)],
        {'advance':15,'ack':True,'values':{'boilertemperature':64}},
        *[{'advance':15,'ack':True} for _ in range(8)]],
    'off_reason_change': [{'no_demand':True}, {'advance':15,'ack':True}, {'advance':15,'values':{'dhwmode':'On'}}, {'advance':15,'values':{'dhwmode':'Off'}}],
    'manual_off_during_write': [{'veto':True}, {'advance':15,'ack':True}],
    'clock_jump': [{}, {'advance':15,'ack':True}, {'advance':15,'clock_jump':20}],
    'long_gap_with_expired_source': [{}, {'advance':15,'ack':True}, {'advance':3901,'ack':True}],
    'startup_exhausted_rearm': [{'rearm':True}, {'advance':15}, {'advance':15}, {'advance':15,'ack':True}],
    'modulation_acquisition': [{'modulation':False}, {'advance':15,'ack':True}, {'advance':15,'ack':True}],
}


async def play(steps, legacy=None):
    p = FakePorts()
    if legacy:
        legacy.time = p.wall_time; legacy.monotonic = p.monotonic
        # Needed only by the legacy rearm permission check; no MQTT client exists.
        legacy.mqtt.is_connected = lambda hass: p.connected
        r = legacy.Runtime(LegacyClimate(p,legacy),deepcopy(SETTINGS))
        r.poll = p.read_telemetry; r.save_reports = p.save_reports
    else:
        m=load_portable()
        r=m['machine'].OperationalMachine(p,SETTINGS,m['kernel'].Policy(**SETTINGS['policy']),m['evidence'].OperatingLimits(),100)
    p.now = 1200
    r.reports.accept(report(p.now),p.now)
    r.modulation_confirmed = True
    r.telemetry.observe(p.raw(),1190)
    states=[]
    for step in steps:
        p.now += step.get('advance',0)
        p.read_failure=step.get('read_failure',False)
        p.write_failures=list(step.get('write_failures',[]))
        p.veto_on_cs=step.get('veto',False)
        p.wall_shift += step.get('clock_jump',0)
        if step.get('ack'):
            p.values.update(chenable='On' if r.ramp.on else 'Off', controlsetpoint=p.setpoint)
        p.values.update(step.get('values',{}))
        if step.get('report'): r.reports.accept(report(p.now,step['report']),p.now); r.revision += 1
        if step.get('bad_other'):
            e=report(p.now,2,False);e['device_ieee']='0x0000000000000002'
            r.reports.accept(e,p.now);r.revision+=1
        if step.get('no_demand'): p.permissions['good_allowed']=False;p.permissions['missing_allowed']=False
        if step.get('boot'):
            p.heat_requested=False;r.boot_ack_pending=True;r.season_start_pending=True;r.season_start_deadline=p.now+600
        if step.get('rearm'):
            r.tripped='startup_attempts_exhausted';r.startup_rearm_after=p.now;r.startup_rearm_barrier=1180
            r.pending=(False,10,1100)
        if 'modulation' in step: r.modulation_confirmed=step['modulation']
        await r.cycle()
        states.append(snapshot(r,p))
    return states


class CompactSnapshot(dict):
    """Offline equivalent of the reader's recognized OTGW 1.7.9 snapshot."""
    epoch_modulus = 65535

    def __init__(self, values, uptime, at):
        epoch=int(uptime)%65535
        # v1.7.9 omits numeric OT fields on zero; Statusflags are always emitted.
        super().__init__({k:dict(value=v,epoch=epoch) for k,v in values.items()
                         if epoch or k in ('chenable','faultindicator','dhwmode')})
        self.acquired_at = at


async def play_counter_wrap(uptime_before, veto=None):
    m=load_portable();p=FakePorts()
    r=m['machine'].OperationalMachine(p,SETTINGS,m['kernel'].Policy(**SETTINGS['policy']),m['evidence'].OperatingLimits(),100)
    observer=m['shadow'].Observer(SETTINGS['members'],'allowed',1000,m['kernel'].Policy(**SETTINGS['policy']))
    p.now=1200
    r.reports.accept(report(p.now),p.now);observer.accept(report(p.now),p.now)
    r.modulation_confirmed=True
    def raw():return CompactSnapshot(p.values,uptime_before+p.now-1200,p.now)
    async def poll():return raw()
    p.read_telemetry=poll
    p.now=1190;r.telemetry.observe(raw(),p.now)
    observer.telemetry.observe(raw(),p.now)
    original_times=deepcopy(r.reports.heads)
    states=[]
    for at in range(1200,1561,5):
        p.now=at
        p.values.update(chenable='On' if r.ramp.on else 'Off',controlsetpoint=p.setpoint)
        wrapped=int(uptime_before+at-1200)//65535>int(uptime_before)//65535
        if veto and wrapped:p.values.update(veto)
        await r.cycle()
        observed=observer.evaluate({k:'on' for k in p.permissions},raw(),p.now,p.now)
        states.append((r.status,observed['status'],r.pending,p.setpoint))
        assert r.tripped is None or veto
        assert r.reports.heads==original_times
    return r,p,observer,states


class OperationalMachineTests(unittest.TestCase):
    def test_boot_across_wrap_with_slow_initial_modulation_reports(self):
        async def boot(uptime, expected_fault=None):
            m=load_portable()
            class SlowCounterPorts(FakePorts):
                async def read_telemetry(self):
                    raw={}
                    for key,value in self.values.items():
                        period=60 if key=='maxrelmodlvl' else 5
                        epoch=int(uptime+(self.now//period)*period-1200)%65535
                        if epoch or key in ('chenable','faultindicator','dhwmode'):
                            raw[key]=dict(value=value,epoch=epoch)
                    raw['sensorsimulation']=dict(value=False,epoch=1794940000+int(self.now))
                    sample=CompactSnapshot({},0,self.now)
                    sample.update(raw)
                    sample.epoch_modulus=m['evidence'].http_epoch_modulus(raw)
                    return sample

                async def send(self, command):
                    await super().send(command)
                    key={'ch':'chenable','cs':'controlsetpoint','mm':'maxrelmodlvl'}[command.kind]
                    self.values[key]=('On' if command.value else 'Off') if command.kind=='ch' else command.value

            p=SlowCounterPorts();p.heat_requested=False
            r=m['machine'].OperationalMachine(p,SETTINGS,m['kernel'].Policy(**SETTINGS['policy']),m['evidence'].OperatingLimits(),100)
            p.now=1200;r.reports.accept(report(1200),1200)
            r.boot_ack_pending=True;r.season_start_pending=True;r.season_start_deadline=1800
            for at in range(1200,1801,5):
                p.now=at;await r.cycle()
                if expected_fault is None:self.assertIsNone(r.tripped)
            on=[t for t,k,v in p.commands if k=='ch' and v]
            if expected_fault:
                # At the exact zero tick firmware omits MM. With a deliberately
                # slow 60s source there cannot be two MM observations inside the
                # unchanged 90s command deadline. Keep the existing safe stop.
                self.assertEqual(r.tripped,expected_fault)
                self.assertFalse(on)
                return
            self.assertTrue(p.heat_requested and on)
            self.assertLessEqual(on[0]-1200,600)
            self.assertFalse(any(t>=on[0] and k=='ch' and not v for t,k,v in p.commands))
            self.assertLessEqual(max(v for t,k,v in p.commands if k=='cs'),55)
            self.assertTrue(r.last_ack['on'] and r.modulation_confirmed)
            self.assertEqual(r.reports.heads['good']['pi']['observed_at'],stamp(1200))
        # Normal uptime, wrap during boot/MM, wrap after CH ON.
        for uptime in (959140,65515,65200):
            with self.subTest(uptime=uptime):asyncio.run(boot(uptime))
        asyncio.run(boot(65535,expected_fault='modulation_not_confirmed'))

    def test_six_november_outages_counter_reconstruction_keeps_ch_and_shadow(self):
        # Original pre-event uptime samples from Influx (13–17 Nov). Field epochs
        # were NOT archived: 5s frames below are reconstructed from firmware,
        # not claimed to be a byte-for-byte replay of historical HTTP responses.
        for uptime in (589543,655242,720642,786343,851742,917443):
            with self.subTest(uptime=uptime):
                r,p,o,states=asyncio.run(play_counter_wrap(uptime))
                self.assertFalse(any(k=='ch' and not v for _,k,v in p.commands))
                self.assertFalse(any(k=='cs' and v<=10 for _,k,v in p.commands))
                self.assertEqual(sum(k=='ch' and v for _,k,v in p.commands),1)
                self.assertTrue(all(10<target<=55 for *_,target in states))
                # The intentionally missing second room remains degraded; one
                # valid room is sufficient and must still propose heat.
                self.assertTrue(all(status=='degraded' for _,status,_,_ in states))
                self.assertEqual(o.result['reason'],'evaluated')
                self.assertGreater(o.result['proposed_water'],10)
                self.assertEqual(r.telemetry.http_counter_wraps['chenable'],1)
                self.assertEqual(o.telemetry.http_counter_wraps['chenable'],1)
                self.assertIsNotNone(r.last_ack)
                self.assertGreater(datetime.fromisoformat(r.last_ack['at']).timestamp(),1200)

    def test_counter_wrap_never_suppresses_dhw_fault_or_water_guard(self):
        for veto in ({'dhwmode':'On'}, {'faultindicator':'On'}, {'boilertemperature':81}):
            with self.subTest(veto=veto):
                r,p,o,states=asyncio.run(play_counter_wrap(65520,veto))
                self.assertTrue(any(k=='ch' and not v for _,k,v in p.commands))
                self.assertTrue(any(k=='cs' and v==10 for _,k,v in p.commands))
                self.assertEqual(o.result['status'],'unknown')

    def test_pinned_runtime49_traces(self):
        golden=json.loads(GOLDEN.read_text())
        self.assertEqual(golden['runtime_sha256'],BASE_SHA)
        self.assertEqual(set(golden['scenarios']),set(SCENARIOS))
        for name,steps in SCENARIOS.items():
            with self.subTest(scenario=name):
                self.assertEqual(asyncio.run(play(steps)),golden['scenarios'][name])

    def test_machine_and_shadow_import_without_ha_or_site_packages(self):
        program="""import runpy,sys,asyncio
ns=runpy.run_path(sys.argv[1])
m=ns['load_portable']()
asyncio.run(ns['play'](ns['SCENARIOS']['dhw_timeout_and_return']))
assert not any(n.startswith(('homeassistant','paho','aiohttp','custom_components.sat.ally.runtime')) for n in sys.modules)
"""
        subprocess.run([sys.executable,'-I','-S','-c',program,__file__],check=True,timeout=15)

    def test_core_has_no_hidden_ha_or_transport_references(self):
        import ast
        for name in ('evidence','machine','shadow'):
            tree=ast.parse((ROOT/'custom_components/sat/ally'/f'{name}.py').read_text())
            for node in ast.walk(tree):
                if isinstance(node,(ast.Import,ast.ImportFrom)):
                    imports=[n.name for n in node.names] if isinstance(node,ast.Import) else [node.module or '']
                    self.assertFalse(any(n.startswith(('homeassistant','paho','aiohttp','runtime','mqtt_monitor','monitor')) for n in imports))
                if isinstance(node,ast.Attribute):
                    self.assertNotIn(ast.unparse(node),('self.c','self.hass'))


if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--record-baseline':
        checkout=Path(sys.argv[2]).resolve()
        path=checkout/'custom_components/sat/ally/runtime.py'
        assert hashlib.sha256(path.read_bytes()).hexdigest()==BASE_SHA, 'Not the pinned hodko.49 reference'
        sys.path.insert(0,str(checkout))
        legacy=importlib.import_module('custom_components.sat.ally.runtime')
        result=dict(runtime_sha256=BASE_SHA,source_commit='aa23097',basis='Synthetic offline traces, not field measurements',
                    scenarios={name:asyncio.run(play(steps,legacy)) for name,steps in SCENARIOS.items()})
        GOLDEN.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
        print('Recorded',len(SCENARIOS),'scenarios from unmodified hodko.49')
    else: unittest.main()
