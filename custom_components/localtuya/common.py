"""Code shared between all platforms."""
import logging

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_ID,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_PLATFORM,
    CONF_ENTITIES,
)

from . import pytuya
from .const import CONF_LOCAL_KEY, CONF_PROTOCOL_VERSION, DOMAIN, TUYA_DEVICE

_LOGGER = logging.getLogger(__name__)


def prepare_setup_entities(hass, config_entry, platform):
    """Prepare ro setup entities for a platform."""
    entities_to_setup = [
        entity
        for entity in config_entry.data[CONF_ENTITIES]
        if entity[CONF_PLATFORM] == platform
    ]
    if not entities_to_setup:
        return None, None

    tuyainterface = hass.data[DOMAIN][config_entry.entry_id][TUYA_DEVICE]

    return tuyainterface, entities_to_setup


def get_entity_config(config_entry, dps_id):
    """Return entity config for a given DPS id."""
    for entity in config_entry.data[CONF_ENTITIES]:
        if entity[CONF_ID] == dps_id:
            return entity
    raise Exception(f"missing entity config for id {dps_id}")


class TuyaDevice:
    """Cache wrapper for pytuya.TuyaInterface."""

    def __init__(self, hass, config_entry):
        """Initialize the cache."""
        self._hass = hass
        self._config_entry = config_entry
        self._interface = None
        self._status = {}

    @property
    def unique_id(self):
        """Return unique device identifier."""
        return self._config_entry[CONF_DEVICE_ID]

    def update_status(self, status):
        """Update status from device."""
        self._status.update(status["dps"])

        signal = f"localtuya_{self._config_entry[CONF_DEVICE_ID]}"
        async_dispatcher_send(self._hass, signal, self._status)

    async def connect(self):
        """Connet to device if not already connected."""
        if self._interface:
            return

        _LOGGER.debug("Connecting to %s", self._config_entry[CONF_HOST])
        self._interface = await pytuya.connect(
            self._config_entry[CONF_HOST],
            self._config_entry[CONF_DEVICE_ID],
            self._config_entry[CONF_LOCAL_KEY],
            float(self._config_entry[CONF_PROTOCOL_VERSION]),
            self.update_status,
        )

        # This has to be done in case the device type is type_0d
        for entity in self._config_entry[CONF_ENTITIES]:
            self._interface.add_dps_to_request(entity[CONF_ID])

        _LOGGER.debug("Retrieving initial state")
        self.update_status(await self._interface.status())

    async def set_dps(self, state, dps_index):
        """Change value of a DP of the Tuya device and update the cached status."""
        for i in range(5):
            try:
                await self.connect()
                await self._interface.set_dps(state, dps_index)
                return
            except Exception as e:
                print(
                    "Failed to set status of device [{}]: [{}]".format(
                        self._config_entry[CONF_HOST], e
                    )
                )
                if i + 1 == 3:
                    _LOGGER.error(
                        "Failed to set status of device %s",
                        self._config_entry[CONF_HOST],
                    )
                    return


class LocalTuyaEntity(Entity):
    """Representation of a Tuya entity."""

    def __init__(self, device, config_entry, dps_id, **kwargs):
        """Initialize the Tuya entity."""
        self._device = device
        self._config_entry = config_entry
        self._config = get_entity_config(config_entry, dps_id)
        self._dps_id = dps_id
        self._status = {}

    async def async_added_to_hass(self):
        """Subscribe localtuya events."""
        await super().async_added_to_hass()

        def _update_handler(status):
            """Update entity state when status was updated."""
            if status is not None:
                self._status = status
                self.status_updated()
            else:
                self._status = {}

            self.schedule_update_ha_state()

        signal = f"localtuya_{self._config_entry.data[CONF_DEVICE_ID]}"
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, _update_handler)
        )

    @property
    def device_info(self):
        """Return device information for the device registry."""
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, f"local_{self._device.unique_id}")
            },
            "name": self._config_entry.data[CONF_FRIENDLY_NAME],
            "manufacturer": "Unknown",
            "model": "Tuya generic",
            "sw_version": self._config_entry.data[CONF_PROTOCOL_VERSION],
        }

    @property
    def name(self):
        """Get name of Tuya entity."""
        return self._config[CONF_FRIENDLY_NAME]

    @property
    def should_poll(self):
        """Return if platform should poll for updates."""
        return False

    @property
    def unique_id(self):
        """Return unique device identifier."""
        return f"local_{self._device.unique_id}_{self._dps_id}"

    def has_config(self, attr):
        """Return if a config parameter has a valid value."""
        value = self._config.get(attr, "-1")
        return value is not None and value != "-1"

    @property
    def available(self):
        """Return if device is available or not."""
        return bool(self._status)

    def dps(self, dps_index):
        """Return cached value for DPS index."""
        value = self._status.get(str(dps_index))
        if value is None:
            _LOGGER.warning(
                "Entity %s is requesting unknown DPS index %s",
                self.entity_id,
                dps_index,
            )

        return value

    def status_updated(self):
        """Device status was updated.

        Override in subclasses and update entity specific state.
        """
