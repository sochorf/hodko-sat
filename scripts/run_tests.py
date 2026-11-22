"""Run SAT tests with fake HA/boiler and disabled network sockets. No deployment."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('checkout', nargs='?', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--suite', choices=['all', 'upstream', 'regression', 'contract'], default='all')
parser.add_argument('--profile', choices=['baseline', 'ha-2026.11', 'ha-2026.11.2'], default='ha-2026.11.2')
parser.add_argument('--ha-overlay', type=Path, help='Optional unpacked, pinned HA wheel for offline testing')
args = parser.parse_args()
if args.ha_overlay:
    args.ha_overlay = args.ha_overlay.resolve()
    sys.path.insert(0, str(args.ha_overlay))
root = args.checkout.resolve()
spec = json.loads((root / 'hodko/source.json').read_text())
if platform.python_version() != spec['python']:
    raise SystemExit(f"Use Python {spec['python']}; found {platform.python_version()}")
dependencies = [('homeassistant', spec['homeassistant']), ('pytest-homeassistant-custom-component', spec['pytest_homeassistant_custom_component'])]
if args.profile.startswith('ha-2026.11'):
    dependencies = [('homeassistant', '2026.11.2' if args.profile.endswith('.2') else '2026.11.1'), ('pytest-homeassistant', '2026.11.1')]
for package, version in dependencies:
    if importlib.metadata.version(package) != version:
        raise SystemExit('Wrong dependency version: ' + package)
with tempfile.TemporaryDirectory(prefix='sat-offline-test-') as tmp:
    base = Path(tmp) / 'base'
    for name in ['climate.py', 'flame.py', 'const.py', 'mqtt/opentherm.py']:
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(subprocess.check_output(['git', 'show', spec['commit'] + ':custom_components/sat/' + name], cwd=root))
    home = Path(tmp) / 'home'
    home.mkdir()
    env = {k: os.environ[k] for k in ['PATH', 'SYSTEMROOT', 'LANG', 'TMPDIR'] if k in os.environ}
    env.update(HOME=str(home), SAT_SOURCE_DIR=str(base), PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=os.pathsep.join([*([str(args.ha_overlay)] if args.ha_overlay else []), str(root / 'tests')]))
    tests = ['tests']
    if args.suite == 'upstream':
        tests = ['tests/test_climate.py', 'tests/test_init.py', 'tests/test_config_flow.py', 'tests/test_manufacturer.py']
    elif args.suite == 'contract':
        tests = ['tests/test_sat_core_contract.py', 'tests/test_sat_operational_machine.py', 'tests/test_ally_boundaries.py']
    elif args.suite == 'regression':
        tests = ['tests/test_sat_shutdown.py', 'tests/test_sat_flame_health.py', 'tests/test_hodko_runtime.py', 'tests/test_ally_mode.py', 'tests/test_ally_ha.py', 'tests/test_ally_kernel.py', 'tests/test_ally_shadow.py', 'tests/test_shadow_adapter.py', 'tests/test_otgw_monitor.py', 'tests/test_mqtt_monitor.py', 'tests/test_sat_core_contract.py', 'tests/test_sat_operational_machine.py', 'tests/test_ally_boundaries.py']
    cmd = [sys.executable, '-m', 'pytest', '-q', '--disable-socket', '--allow-unix-socket', '--timeout=30',
           '-o', 'asyncio_mode=auto', '-o', 'asyncio_default_fixture_loop_scope=function',
           '-o', 'cache_dir=.pytest_cache/' + args.profile,
           '--junitxml=hodko-results-' + args.profile + '-' + args.suite + '.xml', *tests]
    if args.profile.startswith('ha-2026.11'):
        # Official plugin uses respx in selected HA tests only; this suite never
        # imports those tests and must not load respx's optional fixture there.
        cmd[3:3] = ['-p', 'no:respx']
    if args.profile != 'ha-2026.11.2':
        cmd.extend(['--ignore=tests/test_marketplace.py'])
    raise SystemExit(subprocess.run(cmd, cwd=root, env=env).returncode)
