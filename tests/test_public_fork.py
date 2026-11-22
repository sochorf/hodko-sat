"""Publication deltas: preserve saved members; no house-specific injection/reporting."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ModuleNotFoundError as exc:
    if exc.name != 'pytest_homeassistant_custom_component':
        raise
    from pytest_homeassistant.common import MockConfigEntry

from custom_components import sat
from custom_components.sat.ally import shadow_adapter

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode,location', [('mqtt_opentherm', 'options'), ('ally_shadow', 'data'), ('ally_shadow', 'options')])
@pytest.mark.parametrize('migrated', [True, False])
@pytest.mark.parametrize('explicit_removal', [True, False])
async def test_setup_preserves_saved_members_limits_and_markers(hass, monkeypatch, mode, location, migrated, explicit_removal):
    members = {f'0x000000000001{i:04x}': {
        'name': f'head_{i}', 'zone': f'ROOM_{i}',
        'eligible_entity': f'binary_sensor.head_{i}_allowed'} for i in range(13)}
    if explicit_removal:
        del members['0x000000000001000c']
    settings = {'schema_version': 1, 'topic': 'example/reports/events',
        'enabled_entity': 'binary_sensor.heating_allowed', 'members': members,
        'monitor_url': 'http://gateway.invalid/api/v2/otgw/otmonitor',
        'policy': {'ttl': 3900, 'minimum_water': 41, 'maximum_water': 55, 'water_warm': 41},
        'operating_limits': {'mixing_time': 60}}
    if migrated:
        settings.update(hodko_tv2_membership=1, hodko_wc_membership=1)
    shadow_adapter.validate(settings)
    key = 'hodko_shadow' if mode == 'ally_shadow' else 'hodko_ally'
    data = {'mode': mode}
    options = {'maximum_setpoint': 55, 'hodko_control_mode': 'ally_demand', 'error_monitoring': False}
    if mode == 'ally_shadow':
        data[key] = deepcopy(settings)
    (data if location == 'data' else options)[key] = deepcopy(settings)
    entry = MockConfigEntry(domain='sat', version=10, data=data, options=options)
    entry.add_to_hass(hass)
    previous = deepcopy((dict(entry.data), dict(entry.options), entry.version, entry.entry_id))
    adapter = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    constructor = Mock(return_value=adapter)
    monkeypatch.setattr(shadow_adapter, 'Adapter', constructor)
    coordinator = SimpleNamespace(async_setup=AsyncMock())
    factory = Mock();factory.resolve.return_value = coordinator
    monkeypatch.setattr(sat, 'SatDataUpdateCoordinatorFactory', Mock(return_value=factory))
    monkeypatch.setattr(hass.config_entries, 'async_forward_entry_setups', AsyncMock())
    monkeypatch.setattr(sat, 'async_register_services', AsyncMock())
    monkeypatch.setattr(type(hass.services), 'async_call', AsyncMock(side_effect=AssertionError('No actuator call')))
    assert await sat.async_setup_entry(hass, entry)
    assert (dict(entry.data), dict(entry.options), entry.version, entry.entry_id) == previous
    if mode == 'ally_shadow':
        constructor.assert_called_once_with(hass, settings)
        factory.resolve.assert_not_called()
    else:
        assert factory.resolve.call_args.kwargs['options'][key] == settings
        coordinator.async_setup.assert_awaited_once()
    hass.services.async_call.assert_not_called()


def test_legacy_error_reporting_never_initializes_sentry(hass, monkeypatch):
    import sentry_sdk
    factory = Mock(side_effect=AssertionError('No external telemetry'))
    monkeypatch.setattr(sentry_sdk, 'Client', factory)
    monkeypatch.setattr(sentry_sdk, 'init', factory)
    before = dict(hass.data)
    sat.initialize_sentry(hass)
    assert dict(hass.data) == before
    factory.assert_not_called()


@pytest.mark.parametrize('damage', ['control', 'extra_file', 'hidden_extra_file', 'missing_file', 'test', 'manifest', 'private_path'])
def test_release_guard_rejects_unreviewed_changes(tmp_path, damage):
    # Compact copy of the public tree only, with a local index for the scanner.
    import subprocess
    for name in ['custom_components', 'tests', 'scripts']:
        shutil.copytree(ROOT / name, tmp_path / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ['release-policy.json', 'hacs.json', 'LICENSE', '.gitignore']:
        shutil.copyfile(ROOT / name, tmp_path / name)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    component = tmp_path / 'custom_components/sat'
    if damage == 'control':
        with (component / 'ally/runtime.py').open('a') as f:
            f.write('\n# unreviewed change\n')
    elif damage == 'extra_file':
        (component / 'extra.py').write_text('# extra')
    elif damage == 'hidden_extra_file':
        (component / 'testing_config').mkdir()
        (component / 'testing_config/extra.py').write_text('# extra')
    elif damage == 'missing_file':
        (component / 'ally/runtime.py').unlink()
    elif damage == 'test':
        (tmp_path / 'tests/test_public_fork.py').write_text('# removed regressions')
    elif damage == 'manifest':
        data = json.loads((component / 'manifest.json').read_text());data['domain'] = 'other'
        (component / 'manifest.json').write_text(json.dumps(data))
    else:
        (tmp_path / '.storage').mkdir();(tmp_path / '.storage/private.json').write_text('{}')
    spec = importlib.util.spec_from_file_location('guard', ROOT / 'scripts/check_release.py')
    guard = importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    with pytest.raises(ValueError):
        guard.check(tmp_path)
