"""Public platform services cannot redirect PI/HR to native PID/PWM."""
from unittest.mock import AsyncMock
import pytest
try:
    from pytest_homeassistant.common import MockConfigEntry
except ModuleNotFoundError as exc:
    if exc.name != 'pytest_homeassistant': raise
    from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.test_ally_ha import PARAMS, clock_and_mqtt, ready
from tests.upstream.const import DEFAULT_USER_DATA as NATIVE_DATA

pytestmark = pytest.mark.parametrize('domains,data,options,config', PARAMS)


async def test_native_and_compat_services_target_only_the_named_entity(ready, hass, monkeypatch):
    c,r,coordinator,clock = ready
    entry = MockConfigEntry(domain='sat',version=13,
        data=dict(NATIVE_DATA, name='Native Test',device='separate-fake'), options={'maximum_setpoint':55})
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    native = hass.data['sat'][entry.entry_id].climate
    reset = AsyncMock()
    monkeypatch.setattr(native,'async_reset_integral',reset)
    await hass.services.async_call('sat','reset_integral',{'entity_id':[native.entity_id]},blocking=True)
    reset.assert_awaited_once()
    before = (r.revision, r.pending, coordinator.async_set_heater_state.await_count)
    for service,extra in [('reset_integral',{}),('pulse_width_modulation',{'enabled':True})]:
        with pytest.raises(ValueError,match='PI/HR'):
            await hass.services.async_call('sat',service,{'entity_id':[c.entity_id],**extra},blocking=True)
    assert (r.revision,r.pending,coordinator.async_set_heater_state.await_count)==before
    reset.assert_awaited_once()
