"""EPE Tarifa y Amortización — integración personalizada."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_START, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import sensor  # noqa: F401
from .const import *

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


def _block_total(kwh: float, tariff: dict) -> float:
    p1 = float(tariff.get("p1", 0))
    p2 = float(tariff.get("p2", 0))
    p3 = float(tariff.get("p3", 0))
    e1 = min(kwh, BLOCKS) * p1
    e2 = min(max(kwh - BLOCKS, 0), BLOCKS) * p2
    e3 = max(kwh - 2 * BLOCKS, 0) * p3
    return e1 + e2 + e3


def _band_value(kwh: float, bands: list[dict]) -> float:
    result = 0.0
    for band in bands or []:
        desde = float(band.get("desde", 0))
        hasta = float(band.get("hasta", 0))
        valor = float(band.get("valor", 0))
        if desde <= kwh <= hasta:
            return valor
        if kwh <= hasta and not band.get("desde"):
            return valor
    return result


def fancy_total(kwh: float, tariff: dict, meses: float) -> dict[str, float]:
    """Receta EPE: cuota + bloques + impuestos + CAP + IVA + ley 12692."""
    cuota = float(tariff.get("cuota_servicio", 0)) * meses
    basico = cuota + _block_total(kwh, tariff)
    imp = basico * (float(tariff.get("pct_6604", 0)) + float(tariff.get("pct_7797", 0))) / 100
    cap = _band_value(kwh, tariff.get("cap_bands", [])) * meses
    iva = (basico + cap) * float(tariff.get("pct_iva", 0)) / 100
    ley = float(tariff.get("ley12692", 0)) * meses
    total = basico + imp + cap + iva + ley
    return {
        "basico": round(basico, 2),
        "impuestos": round(imp, 2),
        "cap": round(cap, 2),
        "iva": round(iva, 2),
        "ley12692": round(ley, 2),
        "total": round(total, 2),
    }


class EpeStore:
    """Almacenamiento JSON por documento (.storage/epe_tarifa.*)."""

    def __init__(self, hass: HomeAssistant, filename: str, default: dict) -> None:
        self._store = Store(hass, VERSION_STORAGE, filename)
        self._data: dict = dict(default)

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._data.update(data)

    async def async_save(self, data: dict) -> None:
        self._data = data
        await self._store.async_save(data)


class EpeCoordinator:
    """Estado en memoria + actualización de sensores + servicios."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.entities: list = []
        self.values: dict[str, Any] = {}
        self.attrs: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._unsub_interval = None
        self._unsub_state = None
        self.tariff = EpeStore(hass, "epe_tarifa.tariff", dict(DEFAULT_TARIFF))
        self.period = EpeStore(hass, "epe_tarifa.period", dict(DEFAULT_PERIOD))
        self.bills = EpeStore(hass, "epe_tarifa.bills", {"list": []})
        self.ledger = EpeStore(hass, "epe_tarifa.ledger", {"list": []})
        self.savings = EpeStore(hass, "epe_tarifa.savings", {"list": []})
        self.dolar = EpeStore(hass, DOLAR_STORE, dict(DEFAULT_DOLAR))

    # ------------------------------------------------------------------ setup

    async def async_load(self) -> None:
        for store in (self.tariff, self.period, self.bills, self.ledger, self.savings, self.dolar):
            await store.async_load()
        await self.async_recompute()

    async def async_start(self) -> None:
        self._unsub_interval = async_track_time_interval(
            self.hass, self._on_interval, dt.timedelta(hours=2)
        )
        self._unsub_state = async_track_state_change_event(
            self.hass, [SENSOR_EPE, SENSOR_HOME], self._on_state_change
        )
        await self.async_refresh_dolar()

    async def async_stop(self) -> None:
        if self._unsub_interval:
            self._unsub_interval()
        if self._unsub_state:
            self._unsub_state()

    async def _on_interval(self, _now: dt.datetime) -> None:
        await self.async_refresh_dolar()

    async def _on_state_change(self, _event: Event) -> None:
        await self.hass.async_create_task(self.async_recompute())

    def _notify(self) -> None:
        for entity in list(self.entities):
            entity.async_write_ha_state()

    # ------------------------------------------------------------------ dólar

    async def async_refresh_dolar(self, force: bool = False) -> bool:
        # solo consultar la API dentro de la ventana de mercado 08:00–16:00 local,
        # salvo que se fuerce manualmente
        if not force:
            local = dt_util.now().astimezone(dt_util.get_time_zone(self.hass.config.time_zone))
            if not (8 <= local.hour < 16):
                return False
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(DOLAR_URL, timeout=20) as resp:
                if resp.status != 200:
                    raise HomeAssistantError(f"dolarapi HTTP {resp.status}")
                data = await resp.json()
            self.dolar._data["compra"] = float(data.get("compra") or 0)
            self.dolar._data["venta"] = float(data.get("venta") or 0)
            self.dolar._data["updated"] = data.get("fechaActualizacion")
            await self.dolar.async_save(self.dolar._data)
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("epe_tarifa: dólar no actualizable (%s)", exc)
            return False

    # ------------------------------------------------------------------ stats

    async def _sum_period(self, entity: str, start: dt.datetime, end: dt.datetime) -> float:
        """Consumo de un contador dentro de la ventana.

        Estrategia:
        1. Historial de estados reciente (máx por día local). Cubre solo la
           ventana retenida por el recorder (purge ~10 días).
        2. Estadísticas de largo plazo (retención permanente): la serie 'sum'
           de un medidor es acumulada → el consumo de la ventana = último −
           primero.
        """
        total = await self._sum_period_hist(entity, start, end)
        if total > 0:
            return total
        return await self._sum_period_stats(entity, start, end)

    async def _sum_period_hist(self, entity: str, start: dt.datetime, end: dt.datetime) -> float:
        try:
            from homeassistant.components.recorder import get_instance, history
        except Exception:  # noqa: BLE001
            return 0.0
        try:
            inst = get_instance(self.hass)
            rows = await inst.async_add_executor_job(
                history.get_significant_states,
                self.hass,
                start,
                end,
                [entity],
                None,         # filters → None
                False,        # include_start_time_state
                True,         # significant_changes_only
                True,         # minimal_response
                True,         # no_attributes
                False,        # compressed_state_format
            )
            states = rows.get(entity, [])
            if not states:
                return 0.0
            local = dt_util.get_time_zone(self.hass.config.time_zone)
            by_day: dict[str, float] = {}
            for st in states:
                if isinstance(st, dict):
                    val = float(st.get("state")) if st.get("state") not in (None, "", "unknown", "unavailable") else 0.0
                    ts = st.get("last_updated") or st.get("lu") or st.get("last_changed")
                else:
                    val = float(st.state) if st.state not in (None, "", "unknown", "unavailable") else 0.0
                    ts = st.last_updated
                if ts is None:
                    continue
                if isinstance(ts, str):
                    ts = dt_util.parse_datetime(ts)
                if ts is None:
                    continue
                day = ts.astimezone(local).strftime("%Y-%m-%d")
                by_day[day] = max(by_day.get(day, 0.0), val)
            return sum(by_day.values())
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("epe_tarifa: fallo historial %s (%s)", entity, exc)
            return 0.0

    async def _sum_period_stats(self, entity: str, start: dt.datetime, end: dt.datetime) -> float:
        try:
            from homeassistant.components.recorder.statistics import statistics_during_period
            from homeassistant.components.recorder import get_instance
        except Exception:  # noqa: BLE001
            return 0.0
        try:
            inst = get_instance(self.hass)
            # statistics_during_period es síncrona → se ejecuta en el executor del recorder
            res = await inst.async_add_executor_job(
                statistics_during_period, self.hass, start, end, {entity}, "day", None, {"sum"}
            )
            valid = [
                float(r["sum"]) for r in res.get(entity, []) if isinstance(r.get("sum"), (int, float))
            ]
            if valid:
                return max(valid) - min(valid)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("epe_tarifa: fallo estadísticas %s → 0 (%s)", entity, exc)
        return 0.0

    def _window(self, period: dict) -> tuple[dt.datetime, dt.datetime]:
        local = dt_util.get_time_zone(self.hass.config.time_zone)
        start = dt.datetime.strptime(str(period.get("start", "")), "%Y-%m-%d").replace(tzinfo=local)
        end = dt.datetime.strptime(str(period.get("end", "")), "%Y-%m-%d").replace(tzinfo=local)
        end = end + dt.timedelta(days=1)
        return start.astimezone(dt_util.UTC), end.astimezone(dt_util.UTC)

    # ------------------------------------------------------------------ compute

    async def async_recompute(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._compute()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("epe_tarifa: recompute falló: %s", exc)
        self._notify()

    async def _compute(self) -> None:
        tariff = self.tariff._data
        period = self.period._data
        try:
            dias = float(period.get("dias", 60))
        except (TypeError, ValueError):
            dias = 60.0
        meses = dias / 30.0

        values: dict[str, Any] = {k: 0.0 for k, *_ in SENSORS}
        attrs: dict[str, Any] = {}

        # Última factura registrada → fuente del dato real de la trifásica.
        # La trifásica solo se conoce al cierre del período: kwh_factura − kwh_red.
        bills_list = self.bills._data.get("list", [])
        last_bill = bills_list[-1] if bills_list else {}
        kwh_factura = float(last_bill.get("kwh_meter", 0) or 0)
        trifasica = float(last_bill.get("kwh_trifasica", 0) or 0)
        trifasica_dia = trifasica / dias if (trifasica > 0 and dias > 0) else 0.0

        kwh_epe_bill = kwh_home_bill = 0.0
        if period.get("start") and period.get("end"):
            w_s, w_e = self._window(period)
            kwh_epe_bill = await self._sum_period(SENSOR_EPE, w_s, w_e)
            kwh_home_bill = await self._sum_period(SENSOR_HOME, w_s, w_e)

        now = dt_util.utcnow()
        roll_start = now - dt.timedelta(days=dias)
        epe_roll = await self._sum_period(SENSOR_EPE, roll_start, now)
        home_roll = await self._sum_period(SENSOR_HOME, roll_start, now)

        # proyección del período en curso: solo mediciones reales (sin trifásica)
        kwh_red_proj = epe_roll
        proyectado = fancy_total(kwh_red_proj, tariff, meses)

        epe_state = self.hass.states.get(SENSOR_EPE)
        home_state = self.hass.states.get(SENSOR_HOME)
        epe_diario = float(epe_state.state) if epe_state and epe_state.state not in ("unknown", "unavailable") else 0.0
        home_diario = float(home_state.state) if home_state and home_state.state not in ("unknown", "unavailable") else 0.0

        blended = proyectado["total"] / kwh_red_proj if kwh_red_proj > 0 else 0.0

        marginal_puro = float(tariff.get("p3", 0))
        marginal_fiscal = marginal_puro * (1 + (float(tariff.get("pct_6604", 0)) + float(tariff.get("pct_7797", 0))) / 100) * (
            1 + float(tariff.get("pct_iva", 0)) / 100
        )

        values["kwh_red_periodo"] = round(kwh_epe_bill, 2)
        values["kwh_home_periodo"] = round(kwh_home_bill, 2)
        values["kwh_trifasica_periodo"] = round(trifasica, 2)
        values["kwh_trifasica_dia"] = round(trifasica_dia, 3)
        values["kwh_red_dia_estimado"] = round(epe_diario, 2)
        values["kwh_red_proyectado"] = round(kwh_red_proj, 2)
        values["blended"] = round(blended, 2)
        values["total_proyectado_periodo"] = proyectado["total"]
        values["costo_epe_dia"] = round(epe_diario * blended, 0)
        values["costo_ahorro_dia"] = round(max(home_diario - epe_diario, 0) * blended, 0)
        values["costo_consumo_dia"] = round(home_diario * blended, 0)
        values["marginal_puro"] = round(marginal_puro, 2)
        values["marginal_fiscal"] = round(marginal_fiscal, 2)
        values["kwh_factura_meter"] = round(kwh_factura, 0)
        attrs["tariff_month"] = tariff.get("month")
        attrs["tariff"] = tariff

        total_factura = 0.0
        desvio = 0.0
        if last_bill:
            total_factura = float(last_bill.get("total_ars", 0))
            proy = float(last_bill.get("proyectado", 0))
            if total_factura:
                desvio = (total_factura - proy) / total_factura * 100
        values["total_factura_periodo"] = round(total_factura, 0)
        values["desvio_pct"] = round(desvio, 2)

        values["dolar_oficial"] = self.dolar._data.get("venta") or 0.0
        attrs["dolar_compra"] = self.dolar._data.get("compra")
        attrs["dolar_updated"] = self.dolar._data.get("updated")

        inv_usd = sum(float(x.get("usd", 0) or 0) for x in self.ledger._data.get("list", []))
        inv_ars = sum(float(x.get("ars", 0) or 0) for x in self.ledger._data.get("list", []))
        ahorros = [float(x.get("ahorro_usd", 0) or 0) for x in self.savings._data.get("list", [])]
        ahorro_usd = sum(ahorros)
        values["inversion_total_usd"] = round(inv_usd, 2)
        values["inversion_total_ars"] = round(inv_ars, 0)
        values["ahorro_acumulado_usd"] = round(ahorro_usd, 2)
        values["amortizacion_pct"] = round(ahorro_usd / inv_usd * 100, 2) if inv_usd > 0 else 0.0
        values["meses_restantes"] = 0.0
        if inv_usd > 0 and ahorros:
            faltante = inv_usd - ahorro_usd
            medio_periodo = sum(ahorros) / len(ahorros)
            # los ahorros son por período bimestral (~60 días) → base mensual = /2
            medio_mes = medio_periodo / 2.0
            if faltante > 0 and medio_mes > 0:
                values["meses_restantes"] = round(faltante / medio_mes, 1)
        values["ledger_count"] = len(self.ledger._data.get("list", []))
        attrs["bills"] = self.bills._data.get("list", [])
        attrs["ledger"] = self.ledger._data.get("list", [])
        attrs["savings"] = self.savings._data.get("list", [])

        self.values = values
        self.attrs = attrs

    # ------------------------------------------------------------------ servicios

    async def svc_update_tariff(self, call: ServiceCall) -> dict:
        patch = dict(call.data)
        month = patch.pop(CONF_MONTH, None)
        if month:
            self.tariff._data["month"] = str(month)
        for key in (CONF_CUOTA, CONF_P1, CONF_P2, CONF_P3, CONF_LEY12692, CONF_PCT_6604, CONF_PCT_7797, CONF_PCT_IVA):
            if key in patch:
                try:
                    self.tariff._data[key] = float(patch[key])
                except (TypeError, ValueError):
                    raise HomeAssistantError(f"{key} debe ser numérico") from None
        if CONF_CAP_BANDS in patch and patch[CONF_CAP_BANDS]:
            bands = []
            for band in patch[CONF_CAP_BANDS]:
                if isinstance(band, dict):
                    try:
                        bands.append({
                            "desde": float(band.get("desde", 0)),
                            "hasta": float(band.get("hasta", 999999)),
                            "valor": float(band.get("valor", 0)),
                        })
                    except (TypeError, ValueError):
                        continue
            if bands:
                self.tariff._data["cap_bands"] = bands
        if patch.get("cap_bands_csv"):
            bands = self._parse_cap_bands_csv(str(patch["cap_bands_csv"]))
            if bands:
                self.tariff._data["cap_bands"] = bands
        await self.tariff.async_save(dict(self.tariff._data))
        await self.async_recompute()
        return dict(self.tariff._data)

    @staticmethod
    def _parse_cap_bands_csv(csv_text: str) -> list[dict]:
        """Parsea el CSV de bandas del CUT: `kWh_bim_min;kWh_bim_max;ars_cap_mes` por línea."""
        import csv as _csv

        bands = []
        for row in _csv.reader(csv_text.strip().splitlines(), delimiter=";"):
            if len(row) < 3:
                continue
            try:
                desde = float(row[0].strip().replace(",", "."))
                hasta = float(row[1].strip().replace(",", "."))
                valor = float(row[2].strip().replace(",", "."))
            except ValueError:
                continue
            bands.append({"desde": desde, "hasta": hasta, "valor": valor})
        return bands

    async def svc_set_period(self, call: ServiceCall) -> dict:
        data = call.data
        period = dict(self.period._data)
        if data.get(CONF_START):
            period["start"] = data[CONF_START]
        if data.get(CONF_END):
            period["end"] = data[CONF_END]
        if CONF_KWH_METER in data:
            period["kwh_meter"] = float(data[CONF_KWH_METER])
        if data.get("dias"):
            period["dias"] = float(data["dias"])
        await self.period.async_save(period)
        await self.async_recompute()
        return dict(self.period._data)

    async def svc_register_bill(self, call: ServiceCall) -> dict:
        data = call.data
        start = str(data[CONF_START])
        end = str(data[CONF_END])
        med_ant = data.get("medidor_anterior")
        med_act = data.get("medidor_actual")
        kwh_meter = data.get(CONF_KWH_METER)
        if kwh_meter is None and med_ant is not None and med_act is not None:
            kwh_meter = float(med_act) - float(med_ant)
        kwh_meter = float(kwh_meter)
        total_ars = float(data[CONF_TOTAL_ARS])
        tstamp = dt.datetime.now().isoformat(timespec="seconds")

        tmp = dict(self.period._data)
        tmp["start"], tmp["end"] = start, end
        tmp["kwh_meter"] = kwh_meter
        w_s, w_e = self._window(tmp)
        kwh_epe = await self._sum_period(SENSOR_EPE, w_s, w_e)
        kwh_home = await self._sum_period(SENSOR_HOME, w_s, w_e)
        trifasica = max(0.0, kwh_meter - kwh_epe)

        period = dict(self.period._data)
        period["start"], period["end"] = start, end
        period["kwh_meter"] = kwh_meter
        dias = float(period.get("dias", 60))
        if trifasica > 0:
            period["trifasica_dia"] = round(trifasica / dias, 3)
        await self.period.async_save(period)

        blended_real = total_ars / kwh_meter if kwh_meter > 0 else 0.0
        ahorro_kwh = max(kwh_home - kwh_epe, 0.0)
        dolar = self.dolar._data.get("venta")

        # ahorro directo (ARS) si se pasa el total sin FV; si no, estimado por blended
        total_sin_fv = data.get("total_sin_fv")
        if total_sin_fv is not None and float(total_sin_fv) > total_ars:
            ahorro_ars = float(total_sin_fv) - total_ars
        else:
            ahorro_ars = ahorro_kwh * blended_real
        ahorro_usd = ahorro_ars / dolar if dolar else None
        proyectado = fancy_total(kwh_meter, self.tariff._data, dias / 30.0)["total"] if kwh_meter > 0 else 0.0
        desvio = (total_ars - proyectado) / total_ars * 100 if total_ars > 0 else 0.0

        record = {
            "fecha": tstamp,
            "start": start,
            "end": end,
            "dias": dias,
            "medidor_anterior": float(med_ant) if med_ant is not None else None,
            "medidor_actual": float(med_act) if med_act is not None else None,
            "kwh_meter": kwh_meter,
            "kwh_epe_ha": round(kwh_epe, 2),
            "kwh_home_ha": round(kwh_home, 2),
            "kwh_trifasica": round(trifasica, 2),
            "total_ars": total_ars,
            "total_sin_fv": float(total_sin_fv) if total_sin_fv is not None else None,
            "detalle": {
                k: round(float(data.get(k, 0) or 0), 2)
                for k in ("basico", "ley6604", "ley7797", "cap", "iva", "ley12692")
                if data.get(k) is not None
            },
            "blended": round(blended_real, 2),
            "proyectado": round(proyectado, 0),
            "desvio_pct": round(desvio, 2),
            "ahorro_kwh": round(ahorro_kwh, 2),
            "ahorro_ars": round(ahorro_ars, 0),
            "ahorro_usd": round(ahorro_usd, 2) if ahorro_usd is not None else None,
            "dolar": dolar,
        }
        self.bills._data.setdefault("list", []).append(record)
        await self.bills.async_save(dict(self.bills._data))
        if ahorro_usd is not None:
            self.savings._data.setdefault("list", []).append(
                {
                    "fecha": tstamp,
                    "start": start,
                    "end": end,
                    "ahorro_kwh": round(ahorro_kwh, 2),
                    "ahorro_usd": round(ahorro_usd, 2),
                    "dolar": dolar,
                }
            )
            await self.savings.async_save(dict(self.savings._data))
        await self.async_recompute()
        return record

    async def svc_add_purchase(self, call: ServiceCall) -> dict:
        data = call.data
        item = {
            "fecha": str(data.get(CONF_FECHA, "")),
            "producto": str(data.get(CONF_PRODUCTO, "")),
            "usd": float(data.get(CONF_USD, 0)),
            "ars": float(data.get(CONF_ARS, 0)),
        }
        self.ledger._data.setdefault("list", []).append(item)
        await self.ledger.async_save(dict(self.ledger._data))
        await self.async_recompute()
        return item

    async def svc_update_dolar(self, _call: ServiceCall) -> dict:
        ok = await self.async_refresh_dolar(force=True)
        return {"ok": ok, **self.dolar._data}

    async def svc_add_saved_period(self, call: ServiceCall) -> dict:
        """Registra un período de ahorro manual (histórico CSV o factura con datos propios)."""
        data = call.data
        tstamp = data.get("fecha") or dt.datetime.now().isoformat(timespec="seconds")
        record = {
            "fecha": tstamp,
            "start": str(data.get(CONF_START, "")),
            "end": str(data.get(CONF_END, "")),
            "ahorro_kwh": float(data.get(CONF_AHORRO_KWH, 0)),
            "ahorro_usd": float(data.get(CONF_AHORRO_USD, 0)),
            "dolar": data.get("dolar"),
            "nota": data.get("nota", ""),
        }
        self.savings._data.setdefault("list", []).append(record)
        self.savings._data["list"].sort(key=lambda x: str(x.get("fecha", "")))
        await self.savings.async_save(dict(self.savings._data))
        await self.async_recompute()
        return record

    async def svc_remove_purchase(self, call: ServiceCall) -> dict:
        """Elimina una compra del ledger por índice (0-based)."""
        index = int(call.data.get("index", -1))
        items = self.ledger._data.get("list", [])
        if not (0 <= index < len(items)):
            raise HomeAssistantError(f"Índice fuera de rango: {index}")
        removed = items.pop(index)
        await self.ledger.async_save(dict(self.ledger._data))
        await self.async_recompute()
        return {"removido": removed}

    async def svc_import_history_csv(self, call: ServiceCall) -> dict:
        """Sincroniza el histórico de facturas desde un CSV local (reemplaza bills+savings).

        Es un sync completo: al importar se reconstruyen las listas de facturas y
        de ahorros desde cero. Re-ejecutar el servicio tras corregir el CSV o el
        parser repara el estado sin duplicar registros.
        """
        path = str(call.data.get(CONF_PATH, "www/epe/epe_historico.csv"))
        full = self.hass.config.path(path)
        dolar = self.dolar._data.get("venta")
        records = await self.hass.async_add_executor_job(self._parse_history_csv, full, dolar)

        bills = [rec["bill"] for rec in records]
        savings = [rec["saved"] for rec in records if rec.get("saved")]
        savings.sort(key=lambda x: str(x.get("fecha", "")))
        self.bills._data["list"] = bills
        self.savings._data["list"] = savings
        await self.bills.async_save(dict(self.bills._data))
        await self.savings.async_save(dict(self.savings._data))
        await self.async_recompute()
        return {
            "filas_parseadas": len(records),
            "facturas": len(bills),
            "ahorros": len(savings),
            "ahorro_usd_acumulado": round(sum(float(s.get("ahorro_usd", 0) or 0) for s in savings), 2),
        }

    @staticmethod
    def _parse_history_csv(full: str, dolar: float | None) -> list[dict]:
        import csv as _csv
        import os

        if not os.path.exists(full):
            raise HomeAssistantError(f"CSV no encontrado: {full}")
        with open(full, encoding="utf-8-sig", newline="") as fh:
            content = fh.read()
        delimiter = ";" if ";" in content.splitlines()[0] else ","
        reader = list(_csv.reader(content.splitlines(), delimiter=delimiter))
        if not reader:
            return []

        def norm(x: str) -> str:
            return (x or "").strip().lower().replace(" ", "_").replace("á", "a")

        header = [norm(c) for c in reader[0]]
        aliases = {
            "inicio": ["inicio", "desde", "inicio_periodo", "fecha_inicio", "ini", "inicio_periodo"],
            "fin": ["fin", "hasta", "fecha_fin", "fin_periodo"],
            "kwh": ["kwh", "kwh_facturado", "consumo", "kwh_meter", "kwh_medidor", "kwh_factura"],
            "kwh_real": ["kwh_real_consumido", "kwh_real", "kwh_consumido", "kwh_medidor_real", "consumo_real"],
            "total": ["total", "total_ars", "total_factura_ars", "importe", "monto", "monto_factura"],
            "sin_fv": ["total_sin_fv_ars", "total_sin_fv", "sin_fv", "total_factura_sin_fv_ars", "sin_fv_ars"],
            "ahorro_kwh": ["ahorro_kwh", "kwh_ahorrado", "ahorro", "kwh_ahorro"],
            "dolar": ["dolar", "dolar_ref", "usd"],
            "nota": ["nota", "observacion", "periodo", "descripcion"],
            "fecha": ["fecha", "fecha_factura", "fechafactura", "fecha_emision"],
        }
        col = {}
        for key, words in aliases.items():
            for i, h in enumerate(header):
                if h in words:
                    col[key] = i
                    break
        if "kwh" not in col or "total" not in col:
            # template posicional: inicio;fin;kwh;total;ahorro;dolar;nota
            col = {"inicio": 0, "fin": 1, "kwh": 2, "total": 3, "ahorro_kwh": 4, "dolar": 5, "nota": 6}

        def _num(v):
            if v in (None, ""):
                return None
            s = str(v).strip()
            try:
                if "," in s and "." in s and s.rfind(",") > s.rfind("."):
                    s = s.replace(".", "").replace(",", ".")
                elif "," in s:
                    s = s.replace(",", ".")
                return float(s)
            except ValueError:
                return None

        def _date(v):
            if v in (None, ""):
                return None
            s = str(v).strip()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
                try:
                    return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
            return None

        def _get(idx):
            if idx is None or len(row) <= idx:
                return None
            return row[idx].strip() if row[idx] is not None else None

        out = []
        for row in reader[1:]:
            if len(row) < 2 or not row[0].strip():
                continue
            kwh = _num(_get(col.get("kwh")))
            total = _num(_get(col.get("total")))
            if not kwh or not total:
                continue
            inicio = _date(_get(col.get("inicio")))
            fin = _date(_get(col.get("fin")))
            kwh_real = _num(_get(col.get("kwh_real")))
            sin_fv = _num(_get(col.get("sin_fv")))
            ahorro_kwh = _num(_get(col.get("ahorro_kwh")))
            dolar_row = _num(_get(col.get("dolar")))
            nota = _get(col.get("nota")) or ""
            fecha = _date(_get(col.get("fecha"))) or inicio or dt.datetime.now().strftime("%Y-%m-%d")

            blended = total / kwh if kwh else 0.0
            dolar_ef = dolar_row or dolar
            saved = None
            if dolar_ef:
                # ahorro directo (ARS): factura sin FV − factura con FV
                ahorro_ars = (sin_fv - total) if (sin_fv and sin_fv > total) else 0.0
                # ahorro kWh: consumo real medido − kWh facturados
                ahorro_kwh_ef = ahorro_kwh
                if (kwh_real and kwh_real > kwh) and not ahorro_kwh_ef:
                    ahorro_kwh_ef = kwh_real - kwh
                ahorro_usd = (ahorro_ars / float(dolar_ef)) if ahorro_ars else 0.0
                if ahorro_usd or ahorro_kwh_ef:
                    saved = {
                        "fecha": fecha,
                        "start": inicio or "",
                        "end": fin or "",
                        "ahorro_kwh": round(ahorro_kwh_ef or 0.0, 2),
                        "ahorro_usd": round(ahorro_usd, 2),
                        "dolar": float(dolar_ef),
                        "nota": nota,
                    }
            bill = {
                "fecha": fecha,
                "start": inicio or "",
                "end": fin or "",
                "kwh_meter": round(kwh, 0),
                "total_ars": round(total, 0),
                "blended": round(blended, 2),
                "historico": True,
                "nota": nota,
            }
            item = {"bill": bill}
            if saved:
                item["saved"] = saved
            out.append(item)
        return out

    async def svc_import_cut_month(self, call: ServiceCall) -> dict:
        path = str(call.data[CONF_PATH])
        month = call.data.get(CONF_MONTH)
        return await self.hass.async_add_executor_job(self._parse_cut_pdf, path, month)

    def _parse_cut_pdf(self, path: str, month: str | None) -> dict:
        """Parseo best-effort del Cuadro Tarifario (PDF)."""
        try:
            from pdfminer.high_level import extract_text
        except ImportError as exc:
            raise HomeAssistantError("pdfminer.six no instalado") from exc
        text = extract_text(path)
        tariff = dict(self.tariff._data)

        def _num(pattern: str, default: float | None = None) -> float | None:
            m = re.search(pattern, text, re.IGNORECASE)
            if not m:
                return default
            return float(m.group(1).replace(".", "").replace(",", "."))

        cuota = _num(r"Cuota de Servicio.*?([\d.,]+)")
        p1 = _num(r"Primeros 150.*?([\d.,]+)")
        p2 = _num(r"Siguientes 150.*?([\d.,]+)")
        p3 = _num(r"Excedente.*?([\d.,]+)|Excedente de 150.*?([\d.,]+)")
        ley = _num(r"Ley 12692.*?([\d.,]+)")

        bands = []
        for m in re.finditer(
            r"(\d{1,3})\s*[–-]?\s*(\d{1,4})\s*kWh.*?([\d.,]{4,})", text, re.IGNORECASE
        ):
            hasta = int(m.group(2))
            valor = float(m.group(3).replace(".", "").replace(",", "."))
            try:
                if 0 < hasta <= 5000:
                    bands.append({"hasta": hasta, "valor": valor})
            except ValueError:
                continue

        extracted = {
            "cuota_servicio": cuota,
            "p1": p1,
            "p2": p2,
            "p3": p3,
            "cap_bands": bands if len(bands) > 1 else None,
            "ley12692": ley,
            "fuente": path,
        }
        if month:
            tariff["month"] = str(month)
        for key, val in extracted.items():
            if val is not None and key not in ("fuente",):
                tariff[key] = val
        self.tariff._data = tariff
        self.hass.async_create_task(self.tariff.async_save(dict(tariff)))
        return extracted


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migra la entrada de configuración. El entry es data={} y no requiere migración real."""
    _LOGGER.debug(
        "Migrando entrada epe_tarifa de versión %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Crear coordinaador, plataformas y registrar servicios."""
    coordinator = EpeCoordinator(hass)
    await coordinator.async_load()
    hass.data[DOMAIN] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _started(_event: Event) -> None:
        await coordinator.async_start()

    if hass.is_running:
        await coordinator.async_start()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _started)

    def _register(name: str, schema, func) -> None:
        async def _handler(call: ServiceCall) -> None:
            result = await func(call)
            if isinstance(result, dict):
                _LOGGER.info("epe_tarifa.%s → %s", name, result)

        hass.services.async_register(DOMAIN, name, _handler, schema=schema)

    _register(
        "update_tariff",
        vol.Schema(
            {
                vol.Optional(CONF_MONTH): cv.string,
                vol.Optional(CONF_CUOTA): vol.Coerce(float),
                vol.Optional(CONF_P1): vol.Coerce(float),
                vol.Optional(CONF_P2): vol.Coerce(float),
                vol.Optional(CONF_P3): vol.Coerce(float),
                vol.Optional(CONF_LEY12692): vol.Coerce(float),
                vol.Optional(CONF_PCT_6604): vol.Coerce(float),
                vol.Optional(CONF_PCT_7797): vol.Coerce(float),
                vol.Optional(CONF_PCT_IVA): vol.Coerce(float),
                vol.Optional(CONF_CAP_BANDS): cv.ensure_list,
                vol.Optional("cap_bands_csv"): cv.string,
            }
        ),
        coordinator.svc_update_tariff,
    )
    _register(
        "set_period",
        vol.Schema(
            {
                vol.Optional(CONF_START): cv.string,
                vol.Optional(CONF_END): cv.string,
                vol.Optional("dias"): cv.positive_float,
                vol.Optional(CONF_KWH_METER): cv.positive_float,
            }
        ),
        coordinator.svc_set_period,
    )
    _register(
        "register_bill",
        vol.Schema(
            {
                vol.Required(CONF_START): cv.string,
                vol.Required(CONF_END): cv.string,
                vol.Optional(CONF_KWH_METER): vol.Any(vol.Coerce(float), None),
                vol.Required(CONF_TOTAL_ARS): vol.Coerce(float),
                vol.Optional("medidor_anterior"): vol.Any(vol.Coerce(float), None),
                vol.Optional("medidor_actual"): vol.Any(vol.Coerce(float), None),
                vol.Optional("total_sin_fv"): vol.Any(vol.Coerce(float), None),
                vol.Optional("basico"): vol.Any(vol.Coerce(float), None),
                vol.Optional("ley6604"): vol.Any(vol.Coerce(float), None),
                vol.Optional("ley7797"): vol.Any(vol.Coerce(float), None),
                vol.Optional("cap"): vol.Any(vol.Coerce(float), None),
                vol.Optional("iva"): vol.Any(vol.Coerce(float), None),
                vol.Optional("ley12692"): vol.Any(vol.Coerce(float), None),
            }
        ),
        coordinator.svc_register_bill,
    )
    _register(
        "add_purchase",
        vol.Schema(
            {
                vol.Optional(CONF_FECHA): cv.string,
                vol.Optional(CONF_PRODUCTO): cv.string,
                vol.Required(CONF_USD): vol.Coerce(float),
                vol.Required(CONF_ARS): vol.Coerce(float),
            }
        ),
        coordinator.svc_add_purchase,
    )
    _register(
        "add_saved_period",
        vol.Schema(
            {
                vol.Optional("fecha"): cv.string,
                vol.Optional(CONF_START): cv.string,
                vol.Optional(CONF_END): cv.string,
                vol.Required(CONF_AHORRO_KWH): vol.Coerce(float),
                vol.Required(CONF_AHORRO_USD): vol.Coerce(float),
                vol.Optional("dolar"): vol.Coerce(float),
                vol.Optional("nota"): cv.string,
            }
        ),
        coordinator.svc_add_saved_period,
    )
    _register(
        "remove_purchase",
        vol.Schema({vol.Required("index"): vol.Coerce(int)}),
        coordinator.svc_remove_purchase,
    )
    _register("update_dolar_now", vol.Schema({}), coordinator.svc_update_dolar)
    _register(
        "import_history_csv",
        vol.Schema(
            {
                vol.Optional(CONF_PATH): cv.string,
            }
        ),
        coordinator.svc_import_history_csv,
    )
    _register(
        "import_cut_month",
        vol.Schema(
            {
                vol.Required(CONF_PATH): cv.string,
                vol.Optional(CONF_MONTH): cv.string,
            }
        ),
        coordinator.svc_import_cut_month,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: EpeCoordinator = hass.data.get(DOMAIN)
    if coordinator:
        await coordinator.async_stop()
    for service in (
        "update_tariff",
        "set_period",
        "register_bill",
        "add_purchase",
        "remove_purchase",
        "add_saved_period",
        "update_dolar_now",
        "import_history_csv",
        "import_cut_month",
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok
