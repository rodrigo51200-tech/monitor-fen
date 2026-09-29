# Monitor FEN y clima · Tiendas Saga Falabella

Página diaria con el estado del Fenómeno El Niño, avisos y clima por instalación,
con niveles y acciones según el *Plan Integral FEN 2026/2027*.

Fuentes oficiales: ENFEN, SENAMHI (IDESEP), INDECI (COEN), CENEPRED (SIGRID) y NOAA.

## Cómo funciona
- GitHub Actions ejecuta `monitor_fen.py` cada 3 horas, hora Lima (00, 03, 06, 09, 12, 15, 18 y 21 h), y publica `docs/index.html` en GitHub Pages.
- Para actualizar a mano: pestaña **Actions → Monitor FEN → Run workflow**.
- El historial diario se acumula en `historial/`.

## Archivos que se editan a mano
| Archivo | Para qué |
|---|---|
| `zonas_tiendas.csv` | Instalaciones, zona, riesgo del plan, ubicación |
| `alertas_manuales.csv` | Nivel 4 (Alerta Negra) u otros ajustes: `cod_p, tienda, nivel, motivo, vigente_hasta` |

`ubigeo_distritos.csv` (INEI) se usa para ubicar la provincia de los reportes de INDECI.

## Reglas de alerta (Plan FEN, niveles 1 a 4)
| Nivel | Se activa cuando |
|---|---|
| 1 · Verde | Base de temporada (monitoreo preventivo) |
| 2 · Amarilla | Aviso SENAMHI amarillo/naranja sobre la tienda · quebrada en activación a ≤ 5 km · evento INDECI por lluvias a ≤ 10 km · lluvia sobre umbral en estación a 15–30 km · distrito en riesgo Alto/Muy alto en escenario CENEPRED |
| 3 · Roja | Aviso SENAMHI rojo · lluvia sobre umbral en estación a ≤ 15 km · emergencia/peligro inminente INDECI en el distrito (últimas 24 h) |
| 4 · Negra | Solo manual (`alertas_manuales.csv`) |

Un nivel alcanzado se mantiene 24 h. Las distancias se ajustan al inicio de `monitor_fen.py`.
