"""Constantes del componente EPE Tarifa."""
from __future__ import annotations

DOMAIN = "epe_tarifa"
VERSION_STORAGE = 1

SENSOR_EPE = "sensor.epe_consumo_diario_total"
SENSOR_HOME = "sensor.home_consumo_diario_total"

DOLAR_URL = "https://dolarapi.com/v1/dolares/oficial"
DOLAR_STORE = "epe_tarifa.dolar"

BLOCKS = 150  # primeros/segundos 150 kWh

DEFAULT_TARIFF = {
    "month": "2026-08",
    "cuota_servicio": 3560.17,
    "p1": 231.7594,
    "p2": 259.4194,
    "p3": 354.1072,
    "pct_6604": 1.5,
    "pct_7797": 6.0,
    "pct_iva": 21.0,
    "ley12692": 230.81,
    "cap_bands": [
        {"desde": 0, "hasta": 120, "valor": 540.39},
        {"desde": 121, "hasta": 240, "valor": 1339.59},
        {"desde": 241, "hasta": 300, "valor": 5473.08},
        {"desde": 301, "hasta": 450, "valor": 8944.22},
        {"desde": 451, "hasta": 999999, "valor": 8944.22},
    ],
}

DEFAULT_PERIOD = {
    "start": "2026-06-22",
    "end": "2026-08-21",
    "dias": 60,
    "kwh_meter": 359,
    "trifasica_dia": 0.0,
}

DEFAULT_DOLAR = {
    "compra": None,
    "venta": None,
    "updated": None,
}

SENSORS = [
    # (key, name, unit, icon, device_class, state_class, precision)
    ("kwh_red_periodo", "EPE kWh red período", "kWh", "mdi:transmission-tower", "energy", "measurement", 2),
    ("kwh_home_periodo", "EPE kWh hogar período", "kWh", "mdi:home", "energy", "measurement", 2),
    ("kwh_trifasica_periodo", "EPE kWh fase trifásica período", "kWh", "mdi:engine", "energy", "measurement", 2),
    ("kwh_trifasica_dia", "EPE kWh trifásica por día (est.)", "kWh", "mdi:engine", "energy", "measurement", 3),
    ("kwh_red_dia_estimado", "EPE kWh red día (est.)", "kWh", "mdi:transmission-tower", "energy", "measurement", 2),
    ("costo_epe_dia", "EPE costo energía día", "ARS", "mdi:cash", "monetary", "measurement", 0),
    ("costo_ahorro_dia", "EPE costo ahorrado día", "ARS", "mdi:hand-coin", "monetary", "measurement", 0),
    ("costo_consumo_dia", "EPE costo consumo hogar día", "ARS", "mdi:home-lightning-bolt", "monetary", "measurement", 0),
    ("blended", "EPE precio medio ($/kWh, est.)", "ARS/kWh", "mdi:cash-multiple", None, "measurement", 2),
    ("total_proyectado_periodo", "EPE total proyectado período", "ARS", "mdi:calculator", "monetary", "measurement", 0),
    ("kwh_red_proyectado", "EPE kWh red proyectados período", "kWh", "mdi:transmission-tower", "energy", "measurement", 2),
    ("total_factura_periodo", "EPE total última factura", "ARS", "mdi:file-document", "monetary", "measurement", 0),
    ("desvio_pct", "EPE desvío estimado vs real", "%", "mdi:compare", None, "measurement", 2),
    ("marginal_puro", "EPE costo marginal kWh (bloque 3)", "ARS/kWh", "mdi:arrow-collapse-up", None, "measurement", 2),
    ("marginal_fiscal", "EPE costo marginal kWh con impuestos", "ARS/kWh", "mdi:arrow-collapse-up", None, "measurement", 2),
    ("kwh_factura_meter", "EPE kWh medidor última factura", "kWh", "mdi:counter", "energy", "measurement", 0),
    ("dolar_oficial", "EPE dólar oficial (venta)", "ARS", "mdi:currency-usd", "monetary", "measurement", 2),
    ("inversion_total_usd", "EPE inversión total", "USD", "mdi:solar-panel", "monetary", "measurement", 2),
    ("inversion_total_ars", "EPE inversión total (ARS)", "ARS", "mdi:solar-panel", "monetary", "measurement", 0),
    ("ahorro_acumulado_usd", "EPE ahorro acumulado", "USD", "mdi:hand-coin", "monetary", "measurement", 2),
    ("amortizacion_pct", "EPE amortización recuperada", "%", "mdi:percent", None, "measurement", 2),
    ("meses_restantes", "EPE meses para amortizar", "meses", "mdi:calendar-clock", None, "measurement", 1),
    ("ledger_count", "EPE compras registradas", "unidades", "mdi:shopping", None, "measurement", 0),
    ("status", "EPE Tarifa datos", None, "mdi:information-outline", None, None, 0),
]

CONF_PATH = "path"
CONF_MONTH = "month"
CONF_START = "start"
CONF_END = "end"
CONF_KWH_METER = "kwh_meter"
CONF_TOTAL_ARS = "total_ars"
CONF_FECHA = "fecha"
CONF_PRODUCTO = "producto"
CONF_USD = "usd"
CONF_ARS = "ars"
CONF_CUOTA = "cuota_servicio"
CONF_P1 = "p1"
CONF_P2 = "p2"
CONF_P3 = "p3"
CONF_CAP_BANDS = "cap_bands"
CONF_LEY12692 = "ley12692"
CONF_PCT_6604 = "pct_6604"
CONF_PCT_7797 = "pct_7797"
CONF_PCT_IVA = "pct_iva"
CONF_AHORRO_KWH = "ahorro_kwh"
CONF_AHORRO_USD = "ahorro_usd"
