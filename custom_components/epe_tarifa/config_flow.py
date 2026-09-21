"""Config flow del componente EPE Tarifa (creación con valores por defecto)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class EpeTarifaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Flujo mínimo: crea la entrada con valores por defecto."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="EPE Tarifa", data={})
        return self.async_show_form(step_id="user")

    async def async_step_import(self, user_input=None):
        return await self.async_step_user(user_input)