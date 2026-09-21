"""Plataforma de sensores del componente EPE Tarifa."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SENSORS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN]
    entities = [
        EpeSensor(coordinator, key, name, unit, icon, device_class, state_class, precision)
        for key, name, unit, icon, device_class, state_class, precision in SENSORS
    ]
    coordinator.entities.extend(entities)
    async_add_entities(entities)


class EpeSensor(SensorEntity):
    """Sensor de solo lectura alimentado por el coordinador."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        key: str,
        name: str,
        unit: str | None,
        icon: str | None,
        device_class: str | None,
        state_class: str | None,
        precision: int,
    ) -> None:
        self._coord = coordinator
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_name = name
        self._precision = precision
        if unit:
            self._attr_native_unit_of_measurement = unit
        if icon:
            self._attr_icon = icon
        if device_class:
            self._attr_device_class = device_class
        if state_class:
            self._attr_state_class = state_class

    @property
    def native_value(self):
        value = self._coord.values.get(self._key)
        if value is None:
            return None
        try:
            return round(float(value), self._precision)
        except (TypeError, ValueError):
            return value

    @property
    def extra_state_attributes(self):
        # el sensor 'status' expone el detalle completo de la configuración
        if self._key == "status":
            return dict(self._coord.attrs)
        if self._key == "blended":
            return {"tariff_month": self._coord.attrs.get("tariff_month")}
        return None