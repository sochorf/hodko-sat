"""Real platform startup in a fresh interpreter; never collect under tests/conftest.

Only external MQTT/HTTP I/O is replaced. HA entry forwarding, platform setup,
entity registration, the PI/HR runtime and the read-only shadow are real.
"""
from copy import deepcopy
import sys
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import dhcp, mqtt
from homeassistant.helpers import entity_registry as er
from homeassistant.core import CoreState
try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ModuleNotFoundError as exc:
    if exc.name != "pytest_homeassistant_custom_component":
        raise
    from pytest_homeassistant.common import MockConfigEntry


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def hass_config_dir(tmp_path):
    return str(tmp_path)


@pytest.mark.parametrize("order", ["active", "shadow", "active-shadow", "shadow-active"])
async def test_cold_start(hass, monkeypatch, caplog, order):
    # This assertion makes a warmed-up test environment an explicit failure.
    for name in ("climate", "sensor", "number", "binary_sensor"):
        assert "custom_components.sat.compat." + name not in sys.modules

    # The HA test fixture defaults to RUNNING; exercise a real boot here.
    hass.state = CoreState.not_running
    from custom_components.sat.ally import monitor
    from custom_components.sat.compat.fake import SatFakeCoordinator
    from tests.const import DEFAULT_USER_DATA

    monkeypatch.setattr(dhcp, "async_setup", AsyncMock(return_value=True))
    subscriptions = AsyncMock(return_value=lambda: None)
    publish = AsyncMock(side_effect=AssertionError("Unexpected MQTT write"))
    monkeypatch.setattr(mqtt, "async_subscribe", subscriptions)
    monkeypatch.setattr(mqtt, "is_connected", lambda hass: True)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    monkeypatch.setattr(monitor.Reader, "read", AsyncMock(return_value={}))
    # Include the number platform's real entity, using the fake boiler only.
    monkeypatch.setattr(SatFakeCoordinator, "supports_hot_water_setpoint_management", property(lambda self: True))

    settings = {
        "schema_version": 1, "topic": "test/ally/events",
        "monitor_url": "http://test.invalid/api/v2/otgw/otmonitor",
        "enabled_entity": "input_boolean.heat_allowed",
        "members": {"0x0000000000000001": {
            "name": "a", "zone": "A", "eligible_entity": "binary_sensor.a_eligible"}},
        "policy": {"ttl": 3900, "minimum_water": 41, "maximum_water": 55, "water_warm": 41, "water_cold": 50},
    }
    hass.states.async_set("input_boolean.heat_allowed", "on")
    hass.states.async_set("input_boolean.heating_season", "on")
    hass.states.async_set("binary_sensor.a_eligible", "on")
    entries = []
    before = {}
    for kind in order.split("-"):
        data = (dict(DEFAULT_USER_DATA) if kind == "active" else
                {"mode": "ally_shadow", "hodko_shadow": deepcopy(settings)})
        options = ({"maximum_setpoint": 55, "hodko_control_mode": "ally_demand",
                    "hodko_ally": deepcopy(settings)} if kind == "active" else {})
        entry = MockConfigEntry(domain="sat", version=10, data=data, options=options)
        entries.append((kind, entry))
        before[entry.entry_id] = deepcopy((dict(entry.data), dict(entry.options), entry.version))
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Start HA after setting up the entries, as on a full restart, not a reload.
    await hass.async_start()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for kind, entry in entries:
        assert (dict(entry.data), dict(entry.options), entry.version) == before[entry.entry_id]
        entities = er.async_entries_for_config_entry(registry, entry.entry_id)
        domains = {entity.domain for entity in entities}
        assert "water_heater" not in domains
        if kind == "active":
            assert domains == {"climate", "sensor", "number", "binary_sensor"}
            climate = hass.data["sat"][entry.entry_id]["climate"]
            assert climate._ally_runtime is not None
            assert climate._ally_runtime.startup_ready
            # Run the ordinary scheduled cycle; no manual HEAT or fabricated ACK.
            await climate.async_control_heating_loop()
            assert not climate.coordinator.device_active  # missing evidence cannot heat
            assert climate._ally_runtime.policy.maximum_water == 55
            assert hass.states.get(climate.entity_id).state == "heat"
        else:
            assert domains == {"sensor"}
            assert len(entities) == 2
            assert "coordinator" not in hass.data["sat"][entry.entry_id]
            adapter = hass.data["sat"][entry.entry_id]["ally_shadow"]
            assert adapter.observer.policy.maximum_water == 55
            assert adapter.attributes["writes"] is False
        for entity in entities:
            # Optional boiler measurements may be unknown, but entity creation
            # cannot silently fail just because ConfigEntry reports LOADED.
            assert hass.states.get(entity.entity_id) is not None
    assert "takes 2 positional arguments but 3 were given" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= 40 and "sat" in (r.name + r.getMessage())]
    publish.assert_not_called()
    assert subscriptions.await_count >= 2 * len(entries)
    for _, entry in reversed(entries):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
