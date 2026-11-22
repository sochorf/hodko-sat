"""Sensors only: proposed water is not a command or a measured temperature."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import UnitOfTemperature

class ShadowSensor(SensorEntity):
    _attr_should_poll=False
    def __init__(self, adapter, entry_id, water=False):
        self.adapter=adapter
        self.water=water
        suffix='water' if water else 'status'
        self._attr_unique_id=f'{entry_id}_ally_shadow_{suffix}'
        self._attr_name='SAT Ally návrh vody' if water else 'SAT Ally pozorování'
        self.entity_id='sensor.sat_ally_shadow_water' if water else 'sensor.sat_ally_shadow_status'
        if water:
            self._attr_native_unit_of_measurement=UnitOfTemperature.CELSIUS
            self._attr_device_class=SensorDeviceClass.TEMPERATURE

    @property
    def native_value(self):
        r=self.adapter.observer.result
        return r.get('proposed_water') if self.water else r['status']

    @property
    def extra_state_attributes(self):
        return {'writes':False} if self.water else self.adapter.attributes

    async def async_added_to_hass(self):
        self.adapter.listeners.add(self.async_write_ha_state)
        self.async_on_remove(lambda:self.adapter.listeners.discard(self.async_write_ha_state))
