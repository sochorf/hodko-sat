"""Portable decision contract: real shipped modules, no HA import or network.

Synthetic boundary cases, not additional measurements of a historical incident.
Run with the standard library: python -m unittest discover -s tests
-p test_sat_core_contract.py. Also copied into the prepared fork's upgrade gate.
"""
import ast
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import subprocess
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'custom_components/sat/ally'


def load_core():
    # Import the actual modules without executing the HA integration initializer.
    package = types.ModuleType('sat_portable_contract')
    package.__path__ = [str(CORE)]
    sys.modules[package.__name__] = package
    modules = []
    for name in ('kernel', 'control'):
        spec = importlib.util.spec_from_file_location(f'{package.__name__}.{name}', CORE / f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def frame(at=1000):
    def field(value, evidence):
        return dict(value=value, observed_at=stamp(at), evidence=evidence)
    return dict(session_id='source:0', session_started_at=stamp(900),
                heads={'good': dict(pi=field(100, 'zcl_attributeReport'),
                                   heat_required=field(True, 'zcl_attributeReport'),
                                   eligible=field(True, 'policy_evaluation'))},
                outside=field(-10, 'otgw_report'))


class CoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kernel, cls.control = load_core()

    def engine(self):
        return self.control.Engine({'good': 'TV', 'missing': 'WC'},
            self.kernel.Policy(ttl=3900, minimum_water=41, maximum_water=55,
                               water_warm=41, water_cold=55))

    def test_shipped_and_preparation_sources_are_identical(self):
        if not (ROOT / 'tools/sat/fork/ally').exists():
            self.skipTest('Self-contained prepared fork; repository equality checked before preparation')
        self.assertEqual((CORE / 'kernel.py').read_bytes(), (ROOT / 'tools/sat/ally_demand.py').read_bytes())
        self.assertEqual((CORE / 'control.py').read_bytes(), (ROOT / 'tools/sat/fork/ally/control.py').read_bytes())

    def test_core_has_only_explicit_inputs_and_stdlib_dependencies(self):
        allowed = {'dataclasses', 'math', 'copy', 'datetime', 'uuid'}
        for name in ('kernel', 'control'):
            for node in ast.walk(ast.parse((CORE / f'{name}.py').read_text())):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(n.name.split('.')[0] in allowed for n in node.names))
                if isinstance(node, ast.ImportFrom):
                    self.assertTrue((node.level == 1 and node.module == 'kernel') or
                                    (node.level == 0 and node.module in allowed), ast.dump(node))
                if isinstance(node, ast.Call):
                    call = ast.unparse(node.func)
                    self.assertNotIn(call, {'open', '__import__', 'eval', 'exec',
                                           'datetime.now', 'datetime.utcnow', 'time', 'monotonic'})

    def test_portable_import_and_decision_without_site_packages(self):
        # -I -S excludes HA/HACS and third party modules, even in the HA test venv.
        program = '''import runpy, sys
ns = runpy.run_path(sys.argv[1])
k, c = ns['load_core']()
d = c.Engine({'good':'TV'}, k.Policy(maximum_water=55, water_cold=55)).evaluate(ns['frame'](), 1000)
assert d.heat is True and d.water == 55
assert not any(n.startswith(('homeassistant', 'paho', 'aiohttp', 'custom_components.smart_thermostat')) for n in sys.modules)
'''
        subprocess.run([sys.executable, '-I', '-S', '-c', program, __file__], check=True, timeout=15)

    def test_one_valid_room_heats_despite_missing_other_room_at_55_cap(self):
        result = self.engine().evaluate(frame(), 1000)
        self.assertTrue(result.heat)
        self.assertTrue(result.degraded)
        self.assertEqual(result.water, 55)

    def test_invalid_room_cannot_poison_other_valid_room(self):
        f = frame()
        f['heads']['missing'] = deepcopy(f['heads']['good'])
        f['heads']['missing']['heat_required']['value'] = None
        result = self.engine().evaluate(f, 1000)
        self.assertTrue(result.heat)
        self.assertEqual(result.reasons['missing'], 'heat_required_unknown')

    def test_repeated_evaluation_does_not_extend_original_report_lifetime(self):
        e, f = self.engine(), frame()
        for now in range(1000, 4901, 30):
            self.assertTrue(e.evaluate(f, now).heat)
        self.assertIsNone(e.evaluate(f, 4901).heat)
        self.assertEqual(f['heads']['good']['heat_required']['observed_at'], stamp(1000))

    def test_new_invalid_or_off_report_overrides_previous_demand(self):
        for value, expected in ((None, None), (False, False)):
            with self.subTest(value=value):
                e, f = self.engine(), frame()
                self.assertTrue(e.evaluate(f, 1000).heat)
                f['heads']['good']['heat_required'].update(value=value, observed_at=stamp(1010))
                # A missing room leaves uncertainty; explicitly disable it here.
                f['heads']['missing'] = {'eligible': dict(value=False, observed_at=stamp(1010), evidence='policy_evaluation')}
                self.assertIs(e.evaluate(f, 1010).heat, expected)

    def test_permission_off_dominates_even_with_missing_weather(self):
        f = frame(); f['outside'] = None
        self.assertFalse(self.engine().evaluate(f, 1000, enabled=False).heat)
        self.assertIsNone(self.engine().evaluate(f, 1000).heat)

    def test_identical_history_gives_identical_decisions_and_no_frame_mutation(self):
        # Active/shadow equivalence is valid only for identical history and gates.
        a, b, f = self.engine(), self.engine(), frame()
        original = deepcopy(f)
        for now in range(1000, 1901, 30):
            self.assertEqual(a.evaluate(f, now), b.evaluate(deepcopy(f), now))
        self.assertEqual(f, original)


if __name__ == '__main__':
    unittest.main()
