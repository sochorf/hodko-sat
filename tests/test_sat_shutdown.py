"""Offline SAT 4.2.0 shutdown regressions. No HA imports, network or MQTT.

SAT_SOURCE_DIR=/tmp/sat-shutdown-base python3 -m unittest discover -s tests -p test_sat_shutdown.py -v
Source is hash-checked, copied and patched ONLY in a TemporaryDirectory.
Selected original method bodies execute with fake HA/coordinator dependencies.
This is not the upstream HA integration suite or a physical boiler test.
"""
import ast
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
META = json.loads((ROOT / 'tools/sat/safe-shutdown-source.json').read_text())
if not os.environ.get('SAT_SOURCE_DIR'):
    raise unittest.SkipTest('Set SAT_SOURCE_DIR to the reviewed offline SAT sources')
SOURCE = Path(os.environ['SAT_SOURCE_DIR']) / 'climate.py'
for name, expected in META['dependency_sha256'].items():
    if hashlib.sha256((SOURCE.parent / name).read_bytes()).hexdigest() != expected:
        raise RuntimeError(f'Unexpected dependency: {name}')
BASE = SOURCE.read_text()
if hashlib.sha256(BASE.encode()).hexdigest() != META['base_sha256']:
    raise RuntimeError('Source differs from reviewed SAT revision; refusing patch/test')
with tempfile.TemporaryDirectory(prefix='sat-offline-') as directory:
    path = Path(directory) / 'climate.py'
    path.write_text(BASE)
    subprocess.run(['patch', '--batch', '-p1', '-i', str(ROOT / 'tools/sat/safe-shutdown.patch')],
                   cwd=directory, check=True, capture_output=True)
    FIXED = path.read_text()
assert hashlib.sha256(FIXED.encode()).hexdigest() == META['patched_sha256']
compile(FIXED, 'patched/climate.py', 'exec')
MODE = NS(HEAT='heat', OFF='off')
DEVICE = NS(ON='on', OFF='off')
NAMES = {'async_control_heating_loop', '_async_control_heating_loop',
         'async_set_heater_state', 'async_set_hvac_mode', '_async_set_hvac_mode',
         '_async_shutdown', 'reset_control_state', '_async_control_setpoint'}


def load(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SatClimate')
    cls.bases = []
    cls.decorator_list = []
    cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in NAMES]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    namespace = dict(asyncio=asyncio, HVACMode=MODE, DeviceState=DEVICE,
                     MINIMUM_SETPOINT=10, COLD_SETPOINT=28.2,
                     BoilerStatus=NS(OVERSHOOT_HANDLING='overshoot', HEATING_UP='warming'),
                     ATTR_ENTITY_ID='entity_id', ATTR_HVAC_MODE='hvac_mode', CLIMATE_DOMAIN='climate',
                     SERVICE_SET_HVAC_MODE='set_hvac_mode',
                     PWMStatus=NS(IDLE='idle', ON='on'), _LOGGER=logging.getLogger('sat-test'))
    exec(compile(ast.fix_missing_locations(module), 'selected/climate.py', 'exec'), namespace)
    model = namespace['SatClimate']
    model.hvac_mode = property(lambda self: self._hvac_mode)
    return model


async def noop(*args, **kwargs):
    pass


def fake(source=FIXED, active=False, pause=None, failure=None):
    x = load(source)()
    x._control_lock = asyncio.Lock()
    x._mode_lock = asyncio.Lock()
    x._control_timeout = 0.1
    x._shutdown_timeout = 0.05
    x._mode_timeout = 0.5
    x._hvac_mode = MODE.HEAT
    x._calculated_setpoint = None
    x._setpoint = None
    x._alpha = 0.2
    x.current_temperature = 20.9
    x.target_temperature = 22
    x.current_outside_temperature = 13.6
    x.pulse_width_modulation_enabled = pause == 'pwm'
    x.minimum_setpoint_value = 41
    x.valves_open = True
    x._radiators = []
    x._sync_climates_with_mode = False
    x._async_control_pid = lambda *args: None
    x.async_write_ha_state = lambda: None
    x.schedule_control_heating_loop = lambda: None
    x._calculate_control_setpoint = lambda: 57.4
    x.heating_curve = NS(value=None)
    calls = []
    entered, resume = asyncio.Event(), asyncio.Event()

    async def step(name, value=None):
        if name == pause:
            entered.set()
            await resume.wait()
        if failure == name:
            raise OSError(name)
        if name in ('CH', 'CS'):
            calls.append((name, value))

    async def ch(value):
        await step('CH', value)

    async def cs(value):
        await step('CS', value)

    async def control(**kwargs):
        await step('coordinator')

    async def pwm(**kwargs):
        await step('pwm')

    async def modulation():
        await step('modulation')

    async def areas():
        await step('areas')

    x._coordinator = NS(device_active=active, maximum_setpoint=45, device_status=None,
                        hot_water_active=False, flame_active=False, flame=None, boiler=None,
                        async_control_heating_loop=control, async_set_control_setpoint=cs,
                        async_set_heater_state=ch)
    x.pwm = NS(state=NS(status='idle'), disable=lambda: None, reset=lambda: None, update=pwm)
    x._async_control_relative_modulation = modulation
    x.areas = NS(async_control_heating_loops=areas)
    return x, calls, entered, resume


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_skips_off_with_delayed_telemetry(self):
        x, calls, _, _ = fake(BASE)
        await x.async_set_hvac_mode(MODE.OFF)
        self.assertEqual(calls, [])

    async def test_original_reenables_after_off(self):
        x, calls, entered, resume = fake(BASE, pause='areas')
        loop = asyncio.create_task(x.async_control_heating_loop())
        await entered.wait()
        await x.async_set_hvac_mode(MODE.OFF)
        marker = len(calls)
        resume.set()
        await loop
        self.assertIn(('CH', 'on'), calls[marker:])

    async def test_off_always_sends_stop_and_low_setpoint(self):
        for active in (False, True):
            with self.subTest(active=active):
                x, calls, _, _ = fake(active=active)
                await x.async_set_hvac_mode(MODE.OFF)
                await x.async_set_hvac_mode(MODE.OFF)
                self.assertEqual(calls, [('CH', 'off'), ('CS', 10)] * 2)
                self.assertIsNone(x._calculated_setpoint)

    async def test_off_at_every_control_suspension(self):
        for where in ('coordinator', 'pwm', 'CS', 'modulation', 'areas', 'CH'):
            with self.subTest(where=where):
                x, calls, entered, resume = fake(pause=where)
                loop = asyncio.create_task(x.async_control_heating_loop())
                await entered.wait()
                off = asyncio.create_task(x.async_set_hvac_mode(MODE.OFF))
                await asyncio.sleep(0)
                self.assertEqual(x.hvac_mode, MODE.OFF)
                self.assertFalse(off.done(), 'OFF must wait for in-flight writes')
                resume.set()
                await asyncio.gather(loop, off)
                marker = len(calls)
                await x.async_control_heating_loop()
                self.assertEqual(calls[marker:], [])
                self.assertEqual(calls[-2:], [('CH', 'off'), ('CS', 10)])

    async def test_stalled_cycle_times_out_and_off_proceeds(self):
        x, calls, entered, _ = fake(pause='coordinator')
        loop = asyncio.create_task(x.async_control_heating_loop())
        await entered.wait()
        off = asyncio.create_task(x.async_set_hvac_mode(MODE.OFF))
        with self.assertRaises(TimeoutError):
            await loop
        await asyncio.wait_for(off, 1)
        self.assertEqual(calls, [('CH', 'off'), ('CS', 10)])

    async def test_failure_of_ch_still_attempts_setpoint_and_propagates(self):
        x, calls, _, _ = fake(failure='CH')
        with self.assertRaises(OSError):
            await x.async_set_hvac_mode(MODE.OFF)
        self.assertEqual(calls, [('CS', 10)])
        self.assertEqual(x.hvac_mode, MODE.OFF)

    async def test_failure_of_setpoint_still_sends_off_and_propagates(self):
        x, calls, _, _ = fake(failure='CS')
        with self.assertRaises(OSError):
            await x.async_set_hvac_mode(MODE.OFF)
        self.assertEqual(calls, [('CH', 'off')])

    async def test_shutdown_write_timeouts_are_not_reported_as_success(self):
        for where in ('CH', 'CS'):
            with self.subTest(where=where):
                x, calls, _, _ = fake(pause=where)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(x.async_set_hvac_mode(MODE.OFF), 1)
                self.assertEqual(calls, [('CS', 10)] if where == 'CH' else [('CH', 'off')])

    async def test_concurrent_heat_waits_for_shutdown(self):
        x, calls, entered, resume = fake(pause='CH')
        off = asyncio.create_task(x.async_set_hvac_mode(MODE.OFF))
        await entered.wait()
        heat = asyncio.create_task(x.async_set_hvac_mode(MODE.HEAT))
        await asyncio.sleep(0)
        self.assertFalse(heat.done())
        self.assertEqual(x.hvac_mode, MODE.OFF)
        resume.set()
        await asyncio.gather(off, heat)
        self.assertEqual(calls, [('CH', 'off'), ('CS', 10)])
        self.assertEqual(x.hvac_mode, MODE.HEAT)
        await x.async_control_heating_loop()
        self.assertEqual(calls[-2:], [('CS', 45), ('CH', 'on')])

    async def test_queued_loops_do_not_restart_off(self):
        x, calls, entered, resume = fake(pause='coordinator')
        first = asyncio.create_task(x.async_control_heating_loop())
        await entered.wait()
        queued = [asyncio.create_task(x.async_control_heating_loop()) for _ in range(3)]
        off = asyncio.create_task(x.async_set_hvac_mode(MODE.OFF))
        await asyncio.sleep(0)
        resume.set()
        await asyncio.gather(first, *queued, off)
        self.assertEqual(calls, [('CH', 'off'), ('CS', 10)])

    async def test_heat_clamp_and_closed_valves_unchanged(self):
        for valves in (True, False):
            with self.subTest(valves=valves):
                x, calls, _, _ = fake()
                x.valves_open = valves
                await x.async_control_heating_loop()
                self.assertEqual(calls, [('CS', 45)] + ([('CH', 'on')] if valves else []))

    async def test_real_mqtt_serializer_off_order(self):
        tree = ast.parse((SOURCE.parent / 'mqtt/opentherm.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SatOpenThermMqttCoordinator')
        cls.body = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {'async_set_heater_state', 'async_set_control_setpoint'}]
        class Parent:
            async_set_heater_state = noop
            async_set_control_setpoint = noop
        ns = {'SatMqttCoordinator': Parent, 'DeviceState': DEVICE}
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), 'mqtt-selected.py', 'exec'), ns)
        backend = ns['SatOpenThermMqttCoordinator']()
        commands = []
        async def publish(value):
            commands.append(value)
        backend._publish_command = publish
        x, _, _, _ = fake()
        x._coordinator = backend
        await x.async_set_hvac_mode(MODE.OFF)
        self.assertEqual(commands, ['CH=0', 'CS=10', 'PM=25'])

    async def test_cancelled_cycle_releases_lock_for_off(self):
        x, calls, entered, _ = fake(pause='coordinator')
        loop = asyncio.create_task(x.async_control_heating_loop())
        await entered.wait()
        loop.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await loop
        await asyncio.wait_for(x.async_set_hvac_mode(MODE.OFF), 1)
        self.assertEqual(calls, [('CH', 'off'), ('CS', 10)])

    async def test_stalled_cascade_cannot_block_next_off_forever(self):
        x, calls, _, _ = fake()
        x._mode_timeout = 0.05
        entered = asyncio.Event()
        async def stalled(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        x._radiators = ['climate.child']
        x.hass = NS(states=NS(get=lambda _: NS(attributes={'hvac_modes': ['heat', 'off']})),
                    services=NS(async_call=stalled))
        heat = asyncio.create_task(x.async_set_hvac_mode(MODE.HEAT))
        await entered.wait()
        off = asyncio.create_task(x.async_set_hvac_mode(MODE.OFF, cascade=False))
        with self.assertRaises(TimeoutError):
            await heat
        await asyncio.wait_for(off, 1)
        self.assertEqual(calls, [('CH', 'off'), ('CS', 10)])

    async def test_direct_on_rejected_while_off(self):
        x, calls, _, _ = fake()
        x._hvac_mode = MODE.OFF
        await x.async_set_heater_state(DEVICE.ON)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
