# EPE Tarifa y Amortización

Integración personalizada para Home Assistant que controla el **costo de la energía eléctrica de EPE (Empresa Provincial de la Energía, Santa Fe, Argentina)** y el **ahorro generado por el sistema solar**, con cálculo de amortización de la inversión en dólares.

> Proyecto personal, no afiliado a EPE. Los valores del cuadro tarifario se actualizan manualmente (o importando el PDF del CUT) y son responsabilidad del usuario.

## Características

- **Receta de factura EPE** validada al centavo: cuota de servicio + bloques (150/150/excedente) + impuestos Ley 6604 (1,5%) y Ley 7797 (6%) + Cuota Alumbrado Público (CAP por bandas de kWh) + IVA 21% (sobre Básico+CAP) + Ley 12692.
- **Control diario**: costo de energía consumido, costo ahorrado y costo total hogar, valuados a precio medio (blended) del período, con marginal puro y marginal con impuestos como referencia.
- **Fase trifásica no medida**: el medidor de EPE mide todo; HA solo la parte monofásica. La diferencia (kWh de motores trifásicos) se estima por día y se recalcula al registrar cada factura.
- **Doble carril**: proyección diaria (estimativo) vs. factura real registrada, con sensor de **desvío**.
- **Dólar oficial** automático (dolarapi.com) para convertir el ahorro a USD.
- **Ledger de inversión** (compras en ARS + USD) → **% amortizado** y **meses restantes**.

## Instalación (HACS)

1. HACS → ⋯ → Custom repositories
2. URL: `https://github.com/pchdomotichome/epe_tarifa`
3. Categoría: **Integration** → Add
4. Instalar y reiniciar Home Assistant
5. Agregar la integración: **Ajustes → Dispositivos y servicios → Agregar integración → EPE Tarifa**

## Servicios

| Servicio | Qué hace |
|---|---|
| `epe_tarifa.update_tariff` | Actualiza el cuadro tarifario vigente (cuota, P1/P2/P3, CAP bandas, impuestos %, ley 12692). |
| `epe_tarifa.set_period` | Define el período de facturación (inicio/fin, días, kWh del medidor). |
| `epe_tarifa.register_bill` | Registra la factura real emitida: recalcula trifásica, desvío y ahorro en USD. |
| `epe_tarifa.add_purchase` | Agrega una compra al ledger de inversión (fecha, producto, ARS, USD). |
| `epe_tarifa.update_dolar_now` | Refresca el dólar oficial. |
| `epe_tarifa.import_cut_month` | Parsea un PDF del Cuadro Tarifario local (`/config/www/epe/*.pdf`). Best-effort. |

## Sensores principales

- `sensor.epe_costo_epe_dia`, `sensor.epe_costo_ahorro_dia`, `sensor.epe_costo_consumo_dia`
- `sensor.epe_blended` (precio medio ARS/kWh), `sensor.epe_marginal_puro`, `sensor.epe_marginal_fiscal`
- `sensor.epe_total_proyectado_periodo`, `sensor.epe_total_factura_periodo`, `sensor.epe_desvio_pct`
- `sensor.epe_kwh_red_periodo`, `sensor.epe_kwh_trifasica_periodo`, `sensor.epe_kwh_trifasica_dia`
- `sensor.epe_dolar_oficial`, `sensor.epe_inversion_total_usd`, `sensor.epe_ahorro_acumulado_usd`, `sensor.epe_amortizacion_pct`, `sensor.epe_meses_restantes`

## Dependencias

- `pdfminer.six` (solo necesaria para `import_cut_month`; se instala automáticamente).
- Requiere `recorder` y los sensores de contadores diarios de EPE/Home (`sensor.epe_consumo_diario_total`, `sensor.home_consumo_diario_total`) — ver esquema típico con inversor híbrido Sim6000ES.

## Licencia

MIT