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
        result = float(band.get("valor", 0))
        if kwh <= float(band.get("hasta", 0)):
            return result
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
            self.hass, self._on_interval, dt.timedelta(hours=1)
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

    async def async_refresh_dolar(self) -> bool:
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
                {entity},
                False,        # include_start_time_state
                None,         # significant_changes_only
                True,         # minimal_response
            )
            states = rows.get(entity, [])
            if not states:
                return 0.0
            local = dt_util.get_time_zone(self.hass.config.time_zone)
            by_day: dict[str, float] = {}
            for st in states:
                val = float(st.state) if st.state not in (None, "", "unknown", "unavailable") else 0.0
                day = st.last_updated.astimezone(local).strftime("%Y-%m-%d")
                by_day[day] = max(by_day.get(day, 0.0), val)
            return sum(by_day.values())
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("epe_tarifa: fallo historial %s (%s)", entity, exc)
            return 0.0

    async def _sum_period_stats(self, entity: str, start: dt.datetime, end: dt.datetime) -> float:
        try:
            from homeassistant.components.recorder.statistics import statistics_during_period
        except Exception:  # noqa: BLE001
            return 0.0
        # la firma de statistics_during_period varió entre versiones (units posicional,
        # types/statistics_types); probamos variantes hasta obtener la correcta.
        variants = [
            lambda: statistics_during_period(self.hass, start, end, [entity], "day", None, ["sum"]),
            lambda: statistics_during_period(self.hass, start, end, [entity], "day", None, types=["sum"]),
            lambda: statistics_during_period(
                self.hass, start, end, [entity], "day", None, statistics_types=["sum"]
            ),
            lambda: statistics_during_period(self.hass, start, end, [entity], "day", None),
        ]
        for call in variants:
            try:
                res = await call()
                valid = [
                    float(r["sum"]) for r in res.get(entity, []) if isinstance(r.get("sum"), (int, float))
                ]
                if valid:
                    return max(valid) - min(valid)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("epe_tarifa: variante estadísticas descartada (%s)", exc)
        _LOGGER.warning("epe_tarifa: sin datos de estadísticas para %s en la ventana → 0", entity)
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
        kwh_meter = float(period.get("kwh_meter", 0) or 0)
        trifasica_dia = float(period.get("trifasica_dia", 0) or 0)

        values: dict[str, Any] = {k: 0.0 for k, *_ in SENSORS}
        attrs: dict[str, Any] = {}

        kwh_epe_bill = kwh_home_bill = 0.0
        if period.get("start") and period.get("end"):
            w_s, w_e = self._window(period)
            kwh_epe_bill = await self._sum_period(SENSOR_EPE, w_s, w_e)
            kwh_home_bill = await self._sum_period(SENSOR_HOME, w_s, w_e)

        trifasica = max(0.0, kwh_meter - kwh_epe_bill)
        if trifasica > 0:
            trifasica_dia = trifasica / dias
            period["trifasica_dia"] = round(trifasica_dia, 3)
            await self.period.async_save(period)

        now = dt_util.utcnow()
        roll_start = now - dt.timedelta(days=dias)
        epe_roll = await self._sum_period(SENSOR_EPE, roll_start, now)
        home_roll = await self._sum_period(SENSOR_HOME, roll_start, now)

        kwh_red_proj = epe_roll + trifasica_dia * dias
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
        values["kwh_red_dia_estimado"] = round(epe_diario + trifasica_dia, 2)
        values["kwh_red_proyectado"] = round(kwh_red_proj, 2)
        values["blended"] = round(blended, 2)
        values["total_proyectado_periodo"] = proyectado["total"]
        values["costo_epe_dia"] = round((epe_diario + trifasica_dia) * blended, 0)
        values["costo_ahorro_dia"] = round(max(home_diario - epe_diario, 0) * blended, 0)
        values["costo_consumo_dia"] = round(home_diario * blended, 0)
        values["marginal_puro"] = round(marginal_puro, 2)
        values["marginal_fiscal"] = round(marginal_fiscal, 2)
        values["kwh_factura_meter"] = round(kwh_meter, 0)
        attrs["tariff_month"] = tariff.get("month")
        attrs["tariff"] = tariff

        total_factura = 0.0
        desvio = 0.0
        if self.bills._data.get("list"):
            last = self.bills._data["list"][-1]
            total_factura = float(last.get("total_ars", 0))
            proy = float(last.get("proyectado", 0))
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
            medio = sum(ahorros) / len(ahorros)
            if faltante > 0 and medio > 0:
                values["meses_restantes"] = round(faltante / medio, 1)
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
                    bands.append({"hasta": float(band.get("hasta", 999999)), "valor": float(band.get("valor", 0))})
            if bands:
                self.tariff._data["cap_bands"] = bands
        await self.tariff.async_save(dict(self.tariff._data))
        await self.async_recompute()
        return dict(self.tariff._data)

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
        kwh_meter = float(data[CONF_KWH_METER])
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
        ahorro_ars = ahorro_kwh * blended_real
        dolar = self.dolar._data.get("venta")
        ahorro_usd = ahorro_ars / dolar if dolar else None
        proyectado = fancy_total(kwh_meter, self.tariff._data, dias / 30.0)["total"] if kwh_meter > 0 else 0.0
        desvio = (total_ars - proyectado) / total_ars * 100 if total_ars > 0 else 0.0

        record = {
            "fecha": tstamp,
            "start": start,
            "end": end,
            "dias": dias,
            "kwh_meter": kwh_meter,
            "kwh_epe_ha": round(kwh_epe, 2),
            "kwh_home_ha": round(kwh_home, 2),
            "kwh_trifasica": round(trifasica, 2),
            "total_ars": total_ars,
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
        ok = await self.async_refresh_dolar()
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

    async def svc_import_history_csv(self, call: ServiceCall) -> dict:
        """Importa el histórico de facturas desde un CSV local y siembra el ahorro."""
        path = str(call.data.get(CONF_PATH, "www/epe/epe_historico.csv"))
        full = self.hass.config.path(path)
        dolar = self.dolar._data.get("venta")
        records = await self.hass.async_add_executor_job(self._parse_history_csv, full, dolar)

        added_bills = added_saved = 0
        for rec in records:
            self.bills._data.setdefault("list", []).append(rec["bill"])
            added_bills += 1
            if rec.get("saved"):
                self.savings._data.setdefault("list", []).append(rec["saved"])
                added_saved += 1
        if records:
            self.savings._data["list"].sort(key=lambda x: str(x.get("fecha", "")))
            await self.bills.async_save(dict(self.bills._data))
            await self.savings.async_save(dict(self.savings._data))
        await self.async_recompute()
        return {"filas_parseadas": len(records), "facturas": added_bills, "ahorros": added_saved}

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
            "total": ["total", "total_ars", "importe", "monto", "monto_factura"],
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

        out = []
        for row in reader[1:]:
            if len(row) < 2 or not row[0].strip():
                continue
            kwh = _num(row[col["kwh"]]) if col.get("kwh") is not None and len(row) > col["kwh"] else None
            total = _num(row[col["total"]]) if col.get("total") is not None and len(row) > col["total"] else None
            if not kwh or not total:
                continue
            inicio = _date(row[col["inicio"]]) if col.get("inicio") is not None and len(row) > col.get("inicio", 0) else None
            fin = _date(row[col["fin"]]) if col.get("fin") is not None and len(row) > col.get("fin", 0) else None
            ahorro_kwh = _num(row[col["ahorro_kwh"]]) if col.get("ahorro_kwh") is not None and len(row) > col["ahorro_kwh"] else None
            dolar_row = _num(row[col["dolar"]]) if col.get("dolar") is not None and len(row) > col["dolar"] else None
            nota_idx = col.get("nota")
            nota = row[nota_idx].strip() if nota_idx is not None and len(row) > nota_idx else ""
            fecha_idx = col.get("fecha")
            fecha = _date(row[fecha_idx]) if fecha_idx is not None and len(row) > fecha_idx else inicio or dt.datetime.now().strftime("%Y-%m-%d")

            blended = total / kwh if kwh else 0.0
            dolar_ef = dolar_row or dolar
            saved = None
            if ahorro_kwh and dolar_ef:
                saved = {
                    "fecha": fecha,
                    "start": inicio or "",
                    "end": fin or "",
                    "ahorro_kwh": round(ahorro_kwh, 2),
                    "ahorro_usd": round(ahorro_kwh * blended / float(dolar_ef), 2),
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
                vol.Optional(CONF_CUOTA): cv.positive_float,
                vol.Optional(CONF_P1): cv.positive_float,
                vol.Optional(CONF_P2): cv.positive_float,
                vol.Optional(CONF_P3): cv.positive_float,
                vol.Optional(CONF_LEY12692): cv.positive_float,
                vol.Optional(CONF_PCT_6604): cv.positive_float,
                vol.Optional(CONF_PCT_7797): cv.positive_float,
                vol.Optional(CONF_PCT_IVA): cv.positive_float,
                vol.Optional(CONF_CAP_BANDS): cv.ensure_list,
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
                vol.Required(CONF_KWH_METER): cv.positive_float,
                vol.Required(CONF_TOTAL_ARS): cv.positive_float,
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
                vol.Required(CONF_USD): cv.positive_float,
                vol.Required(CONF_ARS): cv.positive_float,
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
                vol.Required(CONF_AHORRO_KWH): cv.positive_float,
                vol.Required(CONF_AHORRO_USD): cv.positive_float,
                vol.Optional("dolar"): cv.positive_float,
                vol.Optional("nota"): cv.string,
            }
        ),
        coordinator.svc_add_saved_period,
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