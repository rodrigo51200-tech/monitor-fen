# Monitor FEN y clima · Tiendas Saga Falabella

Página diaria con el estado del Fenómeno El Niño, avisos y clima por instalación,
con niveles y acciones según el *Plan Integral FEN 2026/2027*.

Fuentes oficiales: ENFEN, SENAMHI (IDESEP), INDECI (COEN), CENEPRED (SIGRID) y NOAA.
Además, noticias de la zona de cada tienda (Google News y Bing News, gratis), leídas por reglas (`noticias.py`);
solo cuentan los medios de `medios_confiables.csv`.

## Cómo funciona
- GitHub Actions ejecuta `monitor_fen.py` cada 15 minutos y publica `docs/index.html` en GitHub Pages.
  El cron de GitHub no es confiable (se salta la mayoría de las corridas), así que cada corrida
  termina con el job `encadenar`, que espera hasta completar 15 min y relanza el workflow. El cron
  queda de respaldo: si la cadena se corta, la próxima corrida programada la reinicia.
- Para apagar la cadena: variable de repositorio `MONITOR_CADENA` = `off`
  (Settings → Secrets and variables → Actions → Variables), o cancelar una corrida a mano.
- Para actualizar a mano: botón **Forzar actualización** de la página (abre **Actions → Monitor FEN → Run workflow**;
  requiere permiso de escritura en el repo).
- El historial diario se acumula en `historial/`; `log_monitor.txt` guarda solo los últimos 3 días (`DIAS_LOG` en `monitor_fen.py`).

## Archivos que se editan a mano
| Archivo | Para qué |
|---|---|
| `zonas_tiendas.csv` | Instalaciones, zona, riesgo del plan, ubicación |
| `medios_confiables.csv` | Medios cuyas noticias pueden subir el nivel: `nombre, dominio, tipo` (las demás se muestran sin efecto) |
| `alertas_manuales.csv` | Nivel 4 (Alerta Negra) u otros ajustes: `cod_p, tienda, nivel, motivo, vigente_hasta` |

`ubigeo_distritos.csv` (INEI) se usa para ubicar la provincia de los reportes de INDECI.

## Reglas de alerta (Plan FEN, niveles 1 a 4)
| Nivel | Se activa cuando |
|---|---|
| 1 · Verde | Base de temporada (monitoreo preventivo) |
| 2 · Amarilla | Aviso SENAMHI amarillo/naranja sobre la tienda · quebrada en activación a ≤ 5 km · evento INDECI por lluvias a ≤ 10 km · lluvia sobre umbral en estación a 15–30 km · distrito en riesgo Alto/Muy alto en escenario CENEPRED · 1 medio confiable informa inundación/aniego/huaico/lluvias intensas en la zona de la tienda (últimas 6 h) |
| 3 · Roja | Aviso SENAMHI rojo · lluvia sobre umbral en estación a ≤ 15 km · emergencia/peligro inminente INDECI en el distrito (últimas 24 h) · 2 o más medios confiables lo informan y es grave (viviendas inundadas, damnificados, desborde, huaico) o citan fuente oficial |
| 4 · Negra | Solo manual (`alertas_manuales.csv`) |

Un nivel alcanzado se mantiene 24 h; uno por noticias dura mientras haya notas confiables de las últimas 6 h y baja solo después. Las noticias solo suben el nivel (nunca lo bajan) y cada alerta muestra los enlaces para verificarla;
no cuentan pronósticos, simulacros ni notas de años anteriores, ni eventos en otra zona del departamento.

Nota: SENAMHI (IDESEP) no responde a conexiones desde GitHub (solo desde Perú, por HTTP); mientras tanto el monitor
usa las demás fuentes y las noticias. Las distancias se ajustan al inicio de `monitor_fen.py`.
