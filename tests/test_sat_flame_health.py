"""Offline flame diagnostic + combined shutdown tests; no live HA imports or I/O."""
import ast
import hashlib
import json
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

# Also verifies and applies the shutdown patch in an isolated temporary directory.
from test_sat_shutdown import SOURCE, ROOT, fake, MODE

META = json.loads((ROOT / 'tools/sat/flame-health-source.json').read_text())
BASE = (SOURCE.parent / 'flame.py').read_text()
if hashlib.sha256(BASE.encode()).hexdigest() != META['base_sha256']:
    raise RuntimeError('Unexpected flame.py revision')
if hashlib.sha256((SOURCE.parent / 'const.py').read_bytes()).hexdigest() != META['const_sha256']:
    raise RuntimeError('Unexpected constants')
with tempfile.TemporaryDirectory(prefix='sat-flame-offline-') as directory:
    path = Path(directory) / 'flame.py'
    path.write_text(BASE)
    subprocess.run(['patch', '--batch', '-p1', '-i', str(ROOT / 'tools/sat/flame-health.patch')],
                   cwd=directory, capture_output=True, check=True)
    FIXED = path.read_text()
assert hashlib.sha256(FIXED.encode()).hexdigest() == META['patched_sha256']
compile(FIXED, 'patched/flame.py', 'exec')


def flame(source=FIXED):
    module = types.ModuleType('offline_flame_test')
    sys.modules[module.__name__] = module
    module.__dict__.update({k: v for k, v in runpy.run_path(str(SOURCE.parent / 'const.py')).items() if not k.startswith('__')})
    tree = ast.parse(source)
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.level)]
    exec(compile(tree, 'selected/flame.py', 'exec'), module.__dict__)
    clock = [0.0]
    module.monotonic = lambda: clock[0]
    obj = module.Flame()

    def tick(t, on=False, demand=True, dhw=False, status=None):
        clock[0] = t
        obj.update(NS(flame_active=on, is_active=demand, hot_water_active=dhw,
                      relative_modulation_level=3.0,
                      status=status or (module.BoilerStatus.AT_SETPOINT if demand else module.BoilerStatus.IDLE)))
        return obj.health_status
    return obj, tick, module


class FlameHealthTests(unittest.TestCase):
    def test_original_alarm_survives_ignition(self):
        f, tick, m = flame(BASE)
        tick(100, demand=False)
        self.assertEqual(tick(1500), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(1540, on=True), m.FlameStatus.STUCK_OFF)
        self.assertTrue(f.is_active)

    def test_long_idle_new_demand_gets_full_grace(self):
        f, tick, m = flame()
        tick(0, demand=False)
        self.assertEqual(tick(1500), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(1800), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(1801), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(1810, on=True), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(2970, on=True), m.FlameStatus.HEALTHY)
        self.assertEqual(f.sample_count_4h, 0)

    def test_startup_clock_zero_and_ignition(self):
        f, tick, m = flame()
        self.assertEqual(tick(0), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(301), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(302, on=True), m.FlameStatus.HEALTHY)

    def test_cancelled_demand_clears_alarm_and_restarts_timer(self):
        f, tick, m = flame()
        tick(1)
        self.assertEqual(tick(302), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(303, demand=False), m.FlameStatus.IDLE_OK)
        self.assertEqual(tick(1000), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(1301), m.FlameStatus.STUCK_OFF)

    def test_transients_do_not_consume_ignition_grace(self):
        for status_name in ('PREHEATING', 'PUMP_STARTING'):
            with self.subTest(status=status_name):
                f, tick, m = flame()
                tick(1)
                tick(302)
                self.assertEqual(tick(303, status=getattr(m.BoilerStatus, status_name)), m.FlameStatus.HEALTHY)
                tick(1000, status=getattr(m.BoilerStatus, status_name))
                self.assertEqual(tick(1001), m.FlameStatus.HEALTHY)
                self.assertEqual(tick(1302), m.FlameStatus.STUCK_OFF)

    def test_dhw_uses_30s_from_new_demand(self):
        f, tick, m = flame()
        tick(1, demand=False)
        self.assertEqual(tick(1000, demand=False, dhw=True), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(1030, demand=False, dhw=True), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(1031, demand=False, dhw=True), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(1032, on=True, demand=False, dhw=True), m.FlameStatus.HEALTHY)

    def test_ch_dhw_switch_resets_relevant_deadline(self):
        f, tick, m = flame()
        tick(1)
        self.assertEqual(tick(250, dhw=True), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(281, dhw=True), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(282), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(583), m.FlameStatus.STUCK_OFF)

    def test_flame_loss_starts_new_wait(self):
        f, tick, m = flame()
        tick(1, on=True)
        tick(1000, on=True)
        self.assertEqual(tick(1001), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(1302), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(1303, on=True), m.FlameStatus.HEALTHY)

    def test_mature_history_also_uses_demand_clock(self):
        f, tick, m = flame()
        for start in (1, 1001, 2001):
            tick(start, on=True)
            tick(start + 900, demand=False)
        self.assertEqual(f.sample_count_4h, 3)
        self.assertNotEqual(tick(10000), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(10301), m.FlameStatus.STUCK_OFF)
        self.assertEqual(tick(10302, on=True), m.FlameStatus.HEALTHY)

    def test_stuck_on_without_demand_is_preserved(self):
        f, tick, m = flame()
        tick(1, on=True, demand=False)
        self.assertEqual(tick(122, on=True, demand=False), m.FlameStatus.STUCK_ON)
        self.assertEqual(tick(123, demand=False), m.FlameStatus.IDLE_OK)


class CombinedTests(unittest.IsolatedAsyncioTestCase):
    async def test_patched_loop_ignition_diagnostic_and_shutdown(self):
        x, calls, _, _ = fake()
        f, tick, m = flame()
        tick(1, demand=False)
        self.assertEqual(tick(1000), m.FlameStatus.HEALTHY)
        self.assertEqual(tick(1301), m.FlameStatus.STUCK_OFF)
        async def control(**kwargs):
            tick(1302, on=True)
        x._coordinator.async_control_heating_loop = control
        await x.async_control_heating_loop()
        self.assertEqual(f.health_status, m.FlameStatus.HEALTHY)
        self.assertEqual(calls, [('CS', 45), ('CH', 'on')])
        await x.async_set_hvac_mode(MODE.OFF)
        self.assertEqual(calls[-2:], [('CH', 'off'), ('CS', 10)])
        before = list(calls)
        await x.async_control_heating_loop()
        self.assertEqual(calls, before)
        # A subsequent boiler observation, not OFF itself, updates flame health.
        self.assertEqual(tick(1303, demand=False), m.FlameStatus.IDLE_OK)
