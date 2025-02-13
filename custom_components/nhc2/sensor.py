"""Support for NHC2 CentralMeter and NHC2 Smart plugs."""
import logging

from homeassistant.components.sensor import PLATFORM_SCHEMA, STATE_CLASS_MEASUREMENT, SensorEntity
from homeassistant.const import POWER_WATT, DEVICE_CLASS_POWER

from .nhccoco.coco import CoCo
from .nhccoco.coco_energy import CoCoEnergyMeter
from .nhccoco.coco_smartplug import CoCoSmartPlug
from .nhccoco.coco_device_class import CoCoDeviceClass

from .const import DOMAIN, ENERGY, SMARTPLUG, KEY_GATEWAY, BRAND
from .helpers import nhc2_entity_processor

KEY_GATEWAY = KEY_GATEWAY

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Load NHC2 energy meters based on a config entry."""
    gateway: CoCo = hass.data[KEY_GATEWAY][config_entry.entry_id]
    _LOGGER.debug('Platform is starting')
    hass.data.setdefault('nhc2_energymeters', {})[config_entry.entry_id] = []
    gateway.get_devices(CoCoDeviceClass.ENERGYMETERS,
                        nhc2_entity_processor(hass,
                                              config_entry,
                                              async_add_entities,
                                              'nhc2_energymeters',
                                              lambda x: NHC2HassEnergyMeter(x))
                        )

    hass.data.setdefault('nhc2_smartplugs', {})[config_entry.entry_id] = []
    gateway.get_devices(CoCoDeviceClass.SMARTPLUGS,
                        nhc2_entity_processor(hass,
                                              config_entry,
                                              async_add_entities,
                                              'nhc2_smartplugs',
                                              lambda x: NHC2HassSmartPlug(x))
                        )


class NHC2HassEnergyMeter(SensorEntity):
    """Representation of an NHC2 Energy Meter."""

    def __init__(self, nhc2energymeter: CoCoEnergyMeter, optimistic=True):
        """Initialize an energy meter."""
        self._nhc2energymeter = nhc2energymeter
        self._state = self._nhc2energymeter.state
        nhc2energymeter.on_change = self._on_change

    @property
    def native_unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return POWER_WATT

    @property
    def device_class(self):
        """Return the device class the sensor belongs to."""
        return DEVICE_CLASS_POWER

    @property
    def state_class(self):
        """Return the state class the sensor belongs to."""
        return STATE_CLASS_MEASUREMENT

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    def _on_change(self):
        self._state = self._nhc2energymeter.state
        self.schedule_update_ha_state()

    @property
    def unique_id(self):
        """Return the energy meters UUID."""
        return self._nhc2energymeter.uuid

    @property
    def uuid(self):
        """Return the energy meters UUID."""
        return self._nhc2energymeter.uuid

    @property
    def should_poll(self):
        """Return false, since the energy meters will push state."""
        return False

    @property
    def name(self):
        """Return the energy meters name."""
        return self._nhc2energymeter.name

    @property
    def device_info(self):
        """Return the device info."""
        return {
            'identifiers': {
                (DOMAIN, self.unique_id)
            },
            'name': self.name,
            'manufacturer': BRAND,
            'model': ENERGY,
            'via_hub': (DOMAIN, self._nhc2energymeter.profile_creation_id),
        }


class NHC2HassSmartPlug(NHC2HassEnergyMeter):
    def __init__(self, nhc2smartplug: CoCoSmartPlug, optimistic=True):
        super().__init__(nhc2smartplug, optimistic)

    @property
    def device_info(self):
        data = super().device_info
        data['model'] = SMARTPLUG
        return data
