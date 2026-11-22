"""SAT5 package boundary: preserve the existing PI/HR controller and its schema."""
from copy import deepcopy
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import HANDLERS, ConfigEntryDisabler
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er
try:
    from pytest_homeassistant.common import MockConfigEntry
except ModuleNotFoundError as exc:
    if exc.name != 'pytest_homeassistant': raise
    from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import sat
from custom_components.sat import compat
from custom_components.sat.config_flow import SatFlowHandler
from custom_components.sat.compat.config_flow import SatOptionsFlowHandler, ShadowOptionsFlow
from custom_components.sat.routing import uses_compat, assert_no_compat_owner
from custom_components.sat.water_heater import async_setup_entry as setup_dhw
from tests.const import DEFAULT_USER_DATA


def settings():
    return {
        'schema_version': 1, 'topic': 'example/ally/events',
        'enabled_entity': 'binary_sensor.heating_allowed',
        'monitor_url': 'http://gateway.invalid/api/v2/otgw/otmonitor',
        'members': {str(i): {'name': f'head_{i}', 'zone': f'room_{i}',
                           'eligible_entity': f'binary_sensor.allowed_{i}'} for i in range(13)},
        'policy': {'maximum_water': 55, 'ttl': 3900},
        'operating_limits': {'mixing_time': 60, 'dhw_recovery_timeout': 900},
        'hodko_tv2_membership': 1, 'hodko_wc_membership': 1,
    }


@pytest.mark.parametrize('kind', ['active', 'shadow_data', 'shadow_options', 'disabled', 'ignore'])
async def test_existing_entries_and_registry_survive_migration_exactly(hass, kind):
    data = dict(DEFAULT_USER_DATA, device='test-gateway')
    options = {'maximum_setpoint': 55, 'hodko_control_mode': 'ally_demand', 'hodko_ally': settings()}
    kwargs = {}
    if kind.startswith('shadow'):
        data = {'mode': 'ally_shadow', 'hodko_shadow': settings()}
        options = {'hodko_shadow': settings()} if kind == 'shadow_options' else {}
    elif kind == 'disabled':
        options = {'maximum_setpoint': 55}
        kwargs['disabled_by'] = ConfigEntryDisabler.USER
    elif kind == 'ignore':
        data = {}; options = {}; kwargs['source'] = 'ignore'
    entry = MockConfigEntry(domain='sat', version=10, minor_version=0, data=data, options=options, **kwargs)
    entry.add_to_hass(hass)
    registry = er.async_get(hass); devices = dr.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={('sat','old-name')})
    entity = registry.async_get_or_create('sensor', 'sat', 'old-unique', config_entry=entry, device_id=device.id)
    before = deepcopy((dict(entry.data), dict(entry.options), entry.version, entry.entry_id,
                       entry.disabled_by, entry.source, entity.as_partial_dict, device.identifiers))
    assert await sat.async_migrate_entry(hass, entry)
    after = (dict(entry.data), dict(entry.options), entry.version, entry.entry_id,
             entry.disabled_by, entry.source, registry.async_get(entity.entity_id).as_partial_dict,
             devices.async_get(device.id).identifiers)
    assert after == before
    assert entry.version == 10  # the installed .51 accepts the unchanged schema
    assert uses_compat(entry)


@pytest.mark.parametrize('mode,version', [('ally_demand',10), ('ally_demand',13), ('ally_shadow',10), ('ally_shadow',13)])
async def test_public_setup_never_constructs_native_control_for_hodko(hass, monkeypatch, mode, version):
    entry = MockConfigEntry(domain='sat', version=version,
        data={'mode': 'ally_shadow' if mode == 'ally_shadow' else 'mqtt_opentherm'},
        options={'hodko_control_mode':mode} if mode != 'ally_shadow' else {})
    legacy = AsyncMock(return_value=True)
    monkeypatch.setattr(compat, 'async_setup_entry', legacy)
    for name in ['SatDataUpdateCoordinatorFactory','SatHeatingControl','SatDemandControl']:
        monkeypatch.setattr(sat, name, Mock(side_effect=AssertionError('Second controller')))
    monkeypatch.setattr(sat, 'async_migrate_identifiers', AsyncMock(side_effect=AssertionError('Identity migration')))
    assert await sat.async_setup_entry(hass, entry)
    legacy.assert_awaited_once_with(hass, entry)
    add = Mock()
    await setup_dhw(hass, entry, add)
    add.assert_not_called()


def test_importing_compat_flow_does_not_replace_public_handler():
    assert HANDLERS['sat'] is SatFlowHandler
    from custom_components.sat.compat.config_flow import SatFlowHandler as LegacyFlow
    assert LegacyFlow.VERSION == 10
    assert HANDLERS['sat'] is SatFlowHandler


@pytest.mark.parametrize('shadow', [False, True])
def test_existing_options_keep_limits_ui(shadow):
    entry = MockConfigEntry(domain='sat', version=10,
        data={'mode': 'ally_shadow' if shadow else 'mqtt_opentherm'}, options={})
    flow = SatFlowHandler.async_get_options_flow(entry)
    assert isinstance(flow, ShadowOptionsFlow if shadow else SatOptionsFlowHandler)


@pytest.mark.parametrize('disabled,shadow,other_device', [(False,False,False),(True,False,False),(False,True,False),(False,False,True)])
def test_new_native_entry_cannot_take_existing_gateway(hass, disabled, shadow, other_device):
    old = MockConfigEntry(domain='sat', version=10,
        data={'device':'old-gateway', 'mode':'ally_shadow' if shadow else 'mqtt_opentherm'},
        disabled_by=ConfigEntryDisabler.USER if disabled else None)
    old.add_to_hass(hass)
    new = MockConfigEntry(domain='sat', version=13,
        data={'device':'other-gateway' if other_device else 'old-gateway','mode':'mqtt_opentherm'})
    assert not uses_compat(new)
    if not (disabled or shadow or other_device):
        with pytest.raises(ConfigEntryError): assert_no_compat_owner(hass,new)
    else: assert_no_compat_owner(hass,new)


async def test_reload_preserves_failed_unload_barrier(hass, monkeypatch):
    entry = MockConfigEntry(domain='sat',version=10,data=DEFAULT_USER_DATA)
    setup = AsyncMock()
    monkeypatch.setattr(compat,'async_unload_entry',AsyncMock(return_value=False))
    monkeypatch.setattr(compat,'async_setup_entry',setup)
    with pytest.raises(RuntimeError): await sat.async_reload_entry(hass,entry)
    setup.assert_not_called()


def test_portable_policy_and_machine_are_unchanged_from_release51():
    # Fixed independent reference hashes; never regenerate to bless behavior edits.
    expected = {
        'kernel.py':'7d718449abb5deb1477e57bdf79b93bf3bd3ed747e6d0c24f44d83bdc5a3ed47',
        'control.py':'a323da45085d78bb8d4bb2eb904da9ab4f974669e1b814cea9eeee526df7106f',
        'evidence.py':'37ba980ed561528f2a5586a551304ebe82970770003b7f6e1b57fc4d8c68c9b9',
        'machine.py':'be270b87ecca7b3402583ab97ad21cf655bee973154065396eb4e9066437c56a',
    }
    root=Path(__file__).resolve().parents[1]/'custom_components/sat/ally'
    for name, digest in expected.items():
        assert hashlib.sha256((root/name).read_bytes()).hexdigest() == digest
