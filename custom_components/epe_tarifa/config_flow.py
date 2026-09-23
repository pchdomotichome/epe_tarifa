"""Config flow del componente EPE Tarifa.

Creación: entrada con valores por defecto.
Reconfiguración: menú de operaciones (agregar compra, actualizar CUT,
registrar factura, eliminar compra, importar CSV, actualizar dólar) que
recopilan los datos y disparan el servicio correspondiente del coordinador.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.selector import (
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_ARS,
    CONF_CUOTA,
    CONF_END,
    CONF_FECHA,
    CONF_KWH_METER,
    CONF_LEY12692,
    CONF_MONTH,
    CONF_P1,
    CONF_P2,
    CONF_P3,
    CONF_PATH,
    CONF_PCT_6604,
    CONF_PCT_7797,
    CONF_PCT_IVA,
    CONF_PRODUCTO,
    CONF_START,
    CONF_TOTAL_ARS,
    CONF_USD,
    DOMAIN,
)

MENU_OPTIONS = [
    "add_purchase",
    "update_tariff",
    "register_bill",
    "remove_purchase",
    "import_csv",
    "update_dolar",
]


def _num(min_v: float = 0, step: float | None = None):
    """Selector de número en modo caja."""
    kwargs: dict = {"min": min_v}
    if step is not None:
        kwargs["step"] = step
    return NumberSelector(NumberSelectorConfig(mode="box", **kwargs))


class EpeTarifaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Flujo de EPE Tarifa: crear entrada o menú de operaciones."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="EPE Tarifa", data={})
        return self.async_show_form(step_id="user")

    async def async_step_import(self, user_input=None):
        return await self.async_step_user(user_input)

    async def async_step_reconfigure(self, user_input=None):
        """Reconfiguración: menú principal con las operaciones disponibles."""
        return await self.async_step_menu()

    async def async_step_menu(self, user_input=None):
        if user_input is not None:
            option = user_input["next_step"]
            if option == "add_purchase":
                return await self.async_step_add_purchase()
            if option == "update_tariff":
                return await self.async_step_update_tariff()
            if option == "register_bill":
                return await self.async_step_register_bill()
            if option == "remove_purchase":
                return await self.async_step_remove_purchase()
            if option == "import_csv":
                return await self.async_step_import_csv()
            if option == "update_dolar":
                try:
                    await self._call_service("update_dolar_now", {})
                    return self.async_abort(reason="dolar_actualizado")
                except Exception:  # noqa: BLE001
                    return self.async_abort(reason="error_servicio")
        return self.async_show_menu(step_id="menu", menu_options=MENU_OPTIONS)

    # ------------------------------------------------------------- helpers

    async def _call_service(self, service: str, data: dict) -> None:
        await self.hass.services.async_call(
            DOMAIN, service, data, blocking=True, context=self._async_current_context()
        )

    # ------------------------------------------------------------ compras

    async def async_step_add_purchase(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._call_service("add_purchase", user_input)
                return self.async_abort(reason="compra_agregada")
            except Exception:  # noqa: BLE001
                errors["base"] = "error_servicio"
        schema = vol.Schema(
            {
                vol.Required(CONF_FECHA): DateSelector(),
                vol.Required(CONF_PRODUCTO): TextSelector(),
                vol.Required(CONF_USD): _num(),
                vol.Required(CONF_ARS): _num(),
            }
        )
        return self.async_show_form(step_id="add_purchase", data_schema=schema, errors=errors)

    async def async_step_remove_purchase(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._call_service("remove_purchase", user_input)
                return self.async_abort(reason="compra_eliminada")
            except Exception:  # noqa: BLE001
                errors["base"] = "error_servicio"
        schema = vol.Schema(
            {
                vol.Required("index"): _num(0, step=1),
            }
        )
        return self.async_show_form(step_id="remove_purchase", data_schema=schema, errors=errors)

    # ----------------------------------------------------------------- CUT

    async def async_step_update_tariff(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._call_service("update_tariff", user_input)
                return self.async_abort(reason="cut_actualizado")
            except Exception:  # noqa: BLE001
                errors["base"] = "error_servicio"
        schema = vol.Schema(
            {
                vol.Required(CONF_MONTH): TextSelector(),
                vol.Required(CONF_CUOTA): _num(),
                vol.Required(CONF_P1): _num(),
                vol.Required(CONF_P2): _num(),
                vol.Required(CONF_P3): _num(),
                vol.Required(CONF_PCT_6604): _num(0, step=0.1),
                vol.Required(CONF_PCT_7797): _num(0, step=0.1),
                vol.Required(CONF_PCT_IVA): _num(0, step=0.1),
                vol.Required(CONF_LEY12692): _num(),
                vol.Optional("cap_bands_csv"): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
            }
        )
        return self.async_show_form(step_id="update_tariff", data_schema=schema, errors=errors)

    # ---------------------------------------------------------- factura

    async def async_step_register_bill(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._call_service("register_bill", user_input)
                return self.async_abort(reason="factura_registrada")
            except Exception:  # noqa: BLE001
                errors["base"] = "error_servicio"
        schema = vol.Schema(
            {
                vol.Required(CONF_START): DateSelector(),
                vol.Required(CONF_END): DateSelector(),
                vol.Optional(CONF_KWH_METER): _num(),
                vol.Optional("medidor_anterior"): _num(),
                vol.Optional("medidor_actual"): _num(),
                vol.Required(CONF_TOTAL_ARS): _num(),
                vol.Optional("total_sin_fv"): _num(),
                vol.Optional("basico"): _num(),
                vol.Optional("ley6604"): _num(),
                vol.Optional("ley7797"): _num(),
                vol.Optional("cap"): _num(),
                vol.Optional("iva"): _num(),
                vol.Optional("ley12692"): _num(),
            }
        )
        return self.async_show_form(step_id="register_bill", data_schema=schema, errors=errors)

    # -------------------------------------------------------------- CSV

    async def async_step_import_csv(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._call_service("import_history_csv", user_input)
                return self.async_abort(reason="historico_importado")
            except Exception:  # noqa: BLE001
                errors["base"] = "error_servicio"
        schema = vol.Schema(
            {
                vol.Required(CONF_PATH, default="www/epe/epe_historico_260921.csv"): TextSelector(),
            }
        )
        return self.async_show_form(step_id="import_csv", data_schema=schema, errors=errors)