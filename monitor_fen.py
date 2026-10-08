"""
monitor_fen.py  -  Monitor diario FEN y clima por zonas de tiendas Saga Falabella

Fuentes (solo oficiales, solo lectura):
  - ENFEN (comunicado oficial y estado del sistema de alerta)
  - SENAMHI / IDESEP (servicio WFS publico):
        avisos meteorologicos, aviso de lluvia 24h, activacion de quebradas,
        umbrales de precipitacion, temperatura y lluvia observada por estacion,
        pronostico de Tmin a 10 dias, pronostico mensual y de verano por sector
  - NOAA CPC (respaldo internacional): TSM semanal region Nino 1+2

Entrada:  zonas_tiendas.csv  (misma carpeta)
Salida:   monitor_fen.html          -> pagina para el equipo
          resumen_teams.txt         -> texto corto para el mensaje diario
          historial/*.csv           -> historico diario acumulado
          historial/log_monitor.txt -> registro de cada corrida

Requisitos:  pip install requests pandas pypdf openpyxl
Uso:         python monitor_fen.py
"""

from __future__ import annotations

import html
import io
import json
import math
import os
import re
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------
CARPETA = Path(__file__).resolve().parent
ARCH_TIENDAS = CARPETA / "zonas_tiendas.csv"
ARCH_HTML = Path(os.environ.get("MONITOR_HTML") or CARPETA / "monitor_fen.html")   # en GitHub: docs/index.html
REPO_GITHUB = os.environ.get("GITHUB_REPOSITORY", "")      # lo define GitHub Actions (usuario/repositorio)
URL_ACTUALIZAR = os.environ.get("MONITOR_URL_ACTUALIZAR", "")  # opcional: servicio que dispara la actualizacion
HORAS_AUTOMATICAS = "cada 15 minutos"   # informativo, igual que el workflow
ARCH_RESUMEN = CARPETA / "resumen_teams.txt"
CARPETA_HIST = CARPETA / "historial"

WFS = "https://idesep.senamhi.gob.pe/geoserver/wfs"
ENFEN_HOME = "https://enfen.imarpe.gob.pe/"
NOAA_SST = "https://www.cpc.ncep.noaa.gov/data/indices/wksst9120.for"
INDECI_FEED = "https://portal.indeci.gob.pe/emergencias/feed/?paged={pagina}"
SIGRID = "https://sigrid.cenepred.gob.pe/sigridv3"
HORAS_INDECI = 48              # antiguedad maxima de reportes INDECI a considerar (Amarilla)
HORAS_INDECI_ROJA = 24         # un reporte en el distrito de la tienda cuenta como Roja solo estas horas
# Distancias (km) desde la tienda para que un evento cercano suba el nivel
DIST_QUEBRADA_KM = 5           # zona de activacion de quebrada (SENAMHI)              -> Amarilla
DIST_INDECI_KM = 10            # centro del distrito con evento INDECI por lluvias     -> Amarilla
DIST_UMBRAL_ROJA_KM = 15       # estacion SENAMHI con lluvia sobre su umbral            -> Roja
DIST_UMBRAL_AMARILLA_KM = 30   #   (entre 15 y 30 km)                                   -> Amarilla
PERSISTENCIA_HORAS = 24        # un nivel alcanzado se mantiene al menos estas horas
ARCH_UBIGEO = CARPETA / "ubigeo_distritos.csv"   # ubigeos INEI (+ ubicacion aproximada) para ubicar reportes INDECI
MAX_PAGINAS_INDECI = 40        # tope de paginas del RSS (~14 reportes c/u)
# Eventos de INDECI relacionados a lluvias (se ignoran incendios, sismos, etc.)
EVENTOS_LLUVIA = ("LLUVIA", "INUNDACI", "HUAICO", "HUAYCO", "DESBORDE", "DESLIZAMIENTO", "MOVIMIENTO EN MASA",
                  "ALUVI", "QUEBRADA", "DERRUMBE", "EROSI", "ANEGAMIENTO", "AVENIDA")
RIESGO_SIGRID_TXT = {"MA": "Muy alto", "A": "Alto", "M": "Medio", "B": "Bajo", "MB": "Muy bajo"}
EXPOSICION_TXT = {"MA": "muy alta", "A": "alta", "M": "media", "B": "baja", "MB": "muy baja"}
RIESGO_SIGRID_ORDEN = {"MB": 0, "B": 1, "M": 2, "A": 3, "MA": 4}

HEADERS = {"User-Agent": "MonitorFEN-SagaFalabella/1.0 (uso interno, consulta diaria)"}
HEADERS_WEB = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MonitorFEN-SagaFalabella/1.0"}
TIMEOUT = (10, 45)             # (conexion, lectura) en segundos por consulta
DIST_MAX_ESTACION_KM = 60      # estacion SENAMHI mas lejana aceptada por tienda
DIAS_SERIE = 15                # registros recientes SENAMHI (N=1..15) para tendencia y promedio
MIN_DIAS_PROYECCION = 14       # dias de historial propio necesarios para la estimacion a 3 dias
UMBRAL_CALOR = 30.0            # T. max (C) desde la que se considera dia caluroso
UMBRAL_FRIO = 5.0              # T. min (C) desde la que se considera noche fria
DIF_ANOMALA = 3.0              # diferencia (C) frente al promedio 15 dias para destacarla

ARCH_CACHE_ESTACIONAL = CARPETA / "cache" / "senamhi_estacional.json"  # ultimo pronostico estacional SENAMHI leido
NASA_POWER = "https://power.larc.nasa.gov/api/temporal/daily/point"
# Reportes meteorologicos horarios (METAR) de aeropuertos peruanos (CORPAC), publicados por NOAA
METAR_API = "https://aviationweather.gov/api/data/metar"
AEROPUERTOS_PE = {"SPJC": "Lima – Jorge Chávez", "SPUR": "Piura", "SPHI": "Chiclayo", "SPRU": "Trujillo",
                  "SPEO": "Chimbote", "SPJR": "Cajamarca", "SPHZ": "Huaraz (Anta)", "SPNC": "Huánuco",
                  "SPCL": "Pucallpa", "SPST": "Tarapoto", "SPQT": "Iquitos", "SPZO": "Cusco", "SPQU": "Arequipa",
                  "SPHO": "Ayacucho", "SPTN": "Tacna", "SPSO": "Pisco", "SPJJ": "Jauja", "SPJL": "Juliaca",
                  "SPTU": "Puerto Maldonado", "SPME": "Tumbes"}
DIST_MAX_AEROPUERTO_KM = 60    # aeropuerto mas lejano aceptado para la temperatura de una tienda
LLUVIA_METAR = ("RA", "DZ", "TS", "SH", "GR")   # codigos METAR de lluvia, llovizna, tormenta, chubasco, granizo
HORAS_BOLETIN_AVISO = 72       # boletines INDECI de aviso meteorologico a considerar
ARCH_MANUALES = CARPETA / "alertas_manuales.csv"
COSTA_ARIDA = ("ICA", "CANETE", "TACNA")   # ademas de Lima: costa desertica donde los modelos confunden garua con lluvia   # Nivel 4 u otros ajustes manuales
# Estaciones SENAMHI que SENAMHI envia a la red mundial de la OMM (partes SYNOP), republicadas por OGIMET.
# Se excluyen las de aeropuerto (ya cubiertas por CORPAC). (id OMM: nombre, lat, lon, altitud m)
OGIMET_SYNOP = "https://www.ogimet.com/cgi-bin/getsynop"
ESTACIONES_OMM = {
    "84392": ("Lancones", -4.6428, -80.5472, 136), "84398": ("Chulucanas", -5.1083, -80.1694, 89),
    "84402": ("San Ignacio", -5.1442, -78.9997, 1270), "84403": ("Canchaque", -5.4006, -79.6053, 1270),
    "84404": ("El Virrey", -5.5358, -79.9842, 211), "84420": ("Jaén", -5.6767, -78.7742, 618),
    "84442": ("Motupe", -6.0667, -79.6819, 191), "84446": ("Tinajones", -6.655, -79.4281, 181),
    "84447": ("Chugur", -6.6689, -78.7381, 2748), "84479": ("estación 84479", -7.3225, -78.1725, 2287),
    "84563": ("estación 84563", -9.8789, -76.5908, 3584), "84614": ("Tarma", -11.3967, -75.6903, 3200),
    "84617": ("Matucana", -11.8389, -76.3778, 2417), "84618": ("Marcapomacocha", -11.4044, -76.325, 4443),
    "84619": ("Huaytapallana", -11.9272, -75.0619, 4648), "84620": ("estación 84620 (Lurigancho)", -11.9875, -76.8419, 553),
    "84621": ("Von Humboldt (La Molina)", -12.0822, -76.9394, 247), "84632": ("Carania", -12.3444, -75.8722, 3840),
    "84682": ("Calca", -13.3331, -71.955, 2924), "84696": ("Sicuani", -14.2372, -71.2367, 3534),
    "84711": ("Palca", -15.2358, -70.5931, 4067), "84712": ("Pampahuta", -15.4836, -70.6761, 4311),
    "84738": ("Illpa", -15.6872, -70.08, 3827), "84739": ("Imata", -15.8428, -71.0906, 4475),
    "84742": ("Puno", -15.8261, -70.0119, 3812), "84753": ("Uzuna", -16.5811, -71.3283, 3269),
    "84759": ("estación 84759", -16.3356, -72.1525, 1498), "84761": ("Moquegua", -17.1786, -70.9325, 1440),
    "84762": ("Candarave", -17.2681, -70.2542, 3410), "84763": ("Ayro", -17.5803, -69.6267, 4260),
}
DIST_MAX_OMM_KM = 25           # estacion SENAMHI (OMM) mas lejana aceptada para una tienda
ALT_MAX_OMM_M = 4000           # estaciones de alta montana no representan a una ciudad
HORAS_MAX_OMM = 6              # antiguedad maxima del ultimo reporte para usarlo

# Niveles de alerta y acciones: PLN-PRE Plan Integral FEN 2026/2027, Cap. X (pag. 8)
NIVELES_PLAN = {
    1: {"nombre": "Alerta Verde", "sub": "Preventivo", "clase": "verde",
        "desc": "Posibilidad de lluvias moderadas o incremento estacional de precipitaciones.",
        "acciones": ["Monitoreo de información meteorológica.",
                     "Revisión de Check-list preventivo.",
                     "Inspección visual y Mantto. preventivo de techos, canaletas y drenajes.",
                     "Actualización de directorios de emergencia."]},
    2: {"nombre": "Alerta Amarilla", "sub": "", "clase": "amarillo",
        "desc": "Pronóstico de lluvias intensas o activación de alertas regionales.",
        "acciones": ["Limpieza inmediata de drenajes y canaletas.",
                     "Inspección extraordinaria de cubiertas.",
                     "Verificación de funcionamiento de bombas de achique.",
                     "Coordinación preventiva con centros comerciales.",
                     "Verificación de grupos electrógenos y UPS.",
                     "Activación parcial de brigadas."]},
    3: {"nombre": "Alerta Roja", "sub": "Emergencia", "clase": "rojo",
        "desc": "Lluvias intensas activas con riesgo inminente de inundación.",
        "acciones": ["Activación del Comité de Crisis Local.",
                     "Monitoreo permanente de zonas críticas.",
                     "Protección de activos y mercadería vulnerable.",
                     "Restricción de accesos afectados.",
                     "Reporte continuo a Operaciones Central."]},
    4: {"nombre": "Alerta Negra", "sub": "Crisis", "clase": "negro",
        "desc": "Inundación o afectación directa a la operación de tienda.",
        "acciones": ["Evacuación preventiva según evaluación del riesgo.",
                     "Suspensión parcial o total de actividades.",
                     "Protección de activos críticos y mercadería.",
                     "Coordinación con autoridades y administración del centro comercial.",
                     "Activación del Comité Ejecutivo de Crisis y del plan de recuperación.",
                     "Comunicación corporativa formal."]},
}

# Acciones complementarias por temperatura (no forman parte del plan FEN)
ACCIONES_TEMP = {
    "calor": "Calor: revisar aire acondicionado / chillers y temperatura de cuartos eléctricos y de TI.",
    "frio": "Frío: revisar cortinas de aire, calefacción y aislamiento de accesos.",
}

ORDEN_RIESGO = {"Crítico": 0, "Alto": 1, "Medio Alto": 2, "Medio": 3, "Bajo": 4}

# Calendario corporativo consolidado, Cap. XXI (pag. 16)
FASES = [
    ((10, 11, 12, 1, 2, 3), "Fase 3 · Respuesta (octubre – marzo)",
     "Monitoreo permanente, activación de alertas, protección de mercadería y activación de comités de crisis."),
    ((7, 8, 9), "Fase 2 · Mitigación (julio – setiembre)",
     "Implementar capex, barreras de contención, probar bombas, verificar grupos electrógenos y UPS, comunicaciones cruzadas y simulacro de brigadas con el mall."),
    ((4, 5), "Fase 4 · Recuperación y Fase 1 · Preparación (abril – mayo)",
     "Evaluación de daños y cierre de brechas; en paralelo, identificar vulnerabilidades y preparar la siguiente temporada."),
    ((6,), "Fase 1 · Preparación (abril – junio)",
     "Identificar vulnerabilidades, pruebas de impermeabilización, simulacro de brigadas, mantenimiento preventivo y cierre de hallazgos críticos."),
]

# Niveles de aviso SENAMHI (su propia escala, distinta a la del plan)
NIVEL_TXT = {2: ("Aviso amarillo", "esté atento a la información oficial"),
             3: ("Aviso naranja", "prepárese: es probable que el evento afecte la zona"),
             4: ("Aviso rojo", "actúe: evento peligroso en la zona")}

ESC_TXT = {"pp": {"superior": "más lluvia de lo normal", "inferior": "menos lluvia de lo normal",
                  "bajo": "menos lluvia de lo normal", "normal": "lluvia dentro de lo normal"},
           "tmax": {"superior": "días más calurosos de lo normal", "inferior": "días más frescos de lo normal",
                    "bajo": "días más frescos de lo normal", "normal": "temperaturas diurnas normales"},
           "tmin": {"superior": "noches más cálidas de lo normal", "inferior": "noches más frías de lo normal",
                    "bajo": "noches más frías de lo normal", "normal": "temperaturas nocturnas normales"}}

ESTADO_ENFEN_TXT = {
    "alerta de el niño costero": "ENFEN confirma que el mar frente a la costa norte está más caliente de lo normal y que "
                                 "esta condición continuaría en los próximos meses. Suele traer más calor y lluvias "
                                 "intensas en la costa norte y riesgo de huaicos.",
    "vigilancia de el niño costero": "ENFEN considera probable que se forme un Niño Costero en los próximos meses; "
                                     "todavía no está en curso.",
    "alerta de la niña costera": "ENFEN confirma que el mar frente a la costa está más frío de lo normal; suele traer "
                                 "menos lluvia en la costa norte y temperaturas más bajas.",
    "vigilancia de la niña costera": "ENFEN considera probable que se forme una Niña Costera en los próximos meses.",
    "no activo": "No hay El Niño ni La Niña costera en curso: condiciones del mar normales.",
}

NOTICIAS_Q = '"{ciudad}" (lluvias OR "El Niño" OR huaico OR quebrada OR "ola de calor") (site:gob.pe OR site:andina.pe)'
NIVEL_MIN_AVISO = 2            # escala SENAMHI: 1 verde (sin aviso), 2 amarillo, 3 naranja, 4 rojo

LINKS = {
    "ENFEN comunicados": "https://enfen.imarpe.gob.pe/comunicados/",
    "SENAMHI avisos": "https://www.senamhi.gob.pe/?p=aviso-meteorologico",
    "SENAMHI pronostico": "https://www.senamhi.gob.pe/?p=pronostico-meteorologico",
    "IDESEP geoservicios": "https://idesep.senamhi.gob.pe/",
    "SENAMHI noticias (gob.pe)": "https://www.gob.pe/senamhi",
    "INDECI emergencias": "https://portal.indeci.gob.pe/emergencias/",
    "CENEPRED SIGRID escenarios": "https://sigrid.cenepred.gob.pe/sigridv3/escenarios",
    "NOAA indices ENSO": "https://www.cpc.ncep.noaa.gov/data/indices/",
}

HOY = datetime.now()
LOG: list[str] = []


_HOSTS_CAIDOS: set[str] = set()


def http_get(url, **kw):
    """GET con 'corte rapido': si un servidor no responde (timeout / conexion rechazada),
    las siguientes consultas a ese mismo servidor en esta corrida se omiten de inmediato."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    if host in _HOSTS_CAIDOS:
        raise ConnectionError(f"{host} no respondió antes en esta corrida; se omite")
    import time
    ultimo = None
    for intento in range(3):
        try:
            return requests.get(url, **kw)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as err:
            ultimo = err
            dns = "resolution" in str(err).lower() or "name or service" in str(err).lower()
            if dns and intento < 2:          # fallo temporal de DNS: reintentar
                time.sleep(5)
                continue
            break
    _HOSTS_CAIDOS.add(host)
    log(f"     ! {host} no responde ({type(ultimo).__name__}); se omiten sus demás consultas")
    raise ultimo


def log(msg: str):
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
    print(linea, flush=True)
    LOG.append(linea)


# ----------------------------------------------------------------------------
# Utilidades geograficas
# ----------------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _punto_en_anillo(x, y, anillo):
    dentro = False
    n = len(anillo)
    j = n - 1
    for i in range(n):
        xi, yi = anillo[i][0], anillo[i][1]
        xj, yj = anillo[j][0], anillo[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            dentro = not dentro
        j = i
    return dentro


def punto_en_geometria(lon, lat, geom) -> bool:
    """Punto dentro de Polygon / MultiPolygon GeoJSON (considera huecos)."""
    if not geom:
        return False
    tipo, coords = geom.get("type"), geom.get("coordinates") or []
    poligonos = [coords] if tipo == "Polygon" else coords if tipo == "MultiPolygon" else []
    for poli in poligonos:
        if poli and _punto_en_anillo(lon, lat, poli[0]):
            if not any(_punto_en_anillo(lon, lat, hueco) for hueco in poli[1:]):
                return True
    return False


def dist_a_geometria_km(lat, lon, geom) -> float:
    """Distancia aproximada (km) de un punto a un poligono GeoJSON: 0 si esta dentro,
    si no, distancia al vertice mas cercano (suficiente para zonas pequenas como quebradas)."""
    if not geom:
        return float("inf")
    if punto_en_geometria(lon, lat, geom):
        return 0.0
    mejor = float("inf")
    def recorrer(c):
        nonlocal mejor
        if c and isinstance(c[0], (int, float)):
            mejor = min(mejor, haversine_km(lat, lon, c[1], c[0]))
        else:
            for x in c:
                recorrer(x)
    recorrer(geom.get("coordinates") or [])
    return mejor


def nivel_num(valor) -> int:
    m = re.search(r"(\d)", str(valor or ""))
    return int(m.group(1)) if m else 0


def norm(txt) -> str:
    """Mayusculas sin tildes para comparar nombres de departamento/provincia."""
    t = str(txt or "").upper().strip()
    for a, b in zip("ÁÉÍÓÚÜ", "AEIOUU"):
        t = t.replace(a, b)
    return t


# ----------------------------------------------------------------------------
# Descarga WFS (IDESEP - SENAMHI)
# ----------------------------------------------------------------------------
def wfs(capa: str, cql: str | None = None, maximo: int | None = None) -> list[dict]:
    params = {"service": "WFS", "version": "1.1.0", "request": "GetFeature",
              "typeName": capa, "outputFormat": "application/json"}
    if cql:
        params["CQL_FILTER"] = cql
    if maximo:
        params["maxFeatures"] = maximo
    r = http_get(WFS, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("features", [])


def seguro(nombre: str, funcion, por_defecto):
    """Ejecuta una fuente; si falla la registra y devuelve un valor vacio."""
    try:
        res = funcion()
        log(f"[OK] {nombre}")
        return res, None
    except Exception as e:  # noqa: BLE001
        log(f"[X]  {nombre}: {e}")
        LOG.append(traceback.format_exc(limit=2))
        return por_defecto, str(e)[:200]


# ----------------------------------------------------------------------------
# Fuentes
# ----------------------------------------------------------------------------
def fuente_enfen() -> dict:
    r = http_get(ENFEN_HOME, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    txt = r.text
    info = {"comunicado": "", "fecha": "", "url_pdf": "", "estado": "", "url_noticia": "",
            "informe": "", "url_informe": ""}

    m = re.search(r'<a[^>]+href="([^"]*download/comunicado-oficial-enfen[^"]*)"[^>]*>\s*([^<]+)</a>', txt, re.I)
    if m:
        info["url_pdf"] = html.unescape(m.group(1))
        info["comunicado"] = html.unescape(m.group(2)).strip()
        f = re.search(r"\|\s*(\d{1,2}\s+[A-Za-zé]+,?\s+\d{4})", txt[m.end(): m.end() + 800])
        info["fecha"] = f.group(1) if f else ""

    m = re.search(r'<a[^>]+href="([^"]*download/informe-tecnico-enfen[^"]*)"[^>]*>\s*([^<]+)</a>', txt, re.I)
    if m:
        info["url_informe"] = html.unescape(m.group(1))
        info["informe"] = html.unescape(m.group(2)).strip()

    # 1) Estado desde el PDF del comunicado mas reciente
    if info["url_pdf"]:
        try:
            from pypdf import PdfReader
            pdf = http_get(info["url_pdf"], headers=HEADERS, timeout=TIMEOUT)
            if pdf.content[:4] == b"%PDF":
                texto = " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf.content)).pages[:2])
                texto = re.sub(r"\s+", " ", texto)
                e = re.search(r"Estado del sistema de alerta\s*:?\s*(.{5,60}?)(?:\.|\s{2}|$|La Comisi|El ENFEN|Tras|Luego)", texto, re.I)
                if e:
                    info["estado"] = e.group(1).strip(" :.-")
        except Exception as e:  # noqa: BLE001
            log(f"     ENFEN PDF no leido ({e}); se usa el titular de noticias")

    # 2) Respaldo: titular de la noticia mas reciente con "Estado ... alerta"
    for href, titulo in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]*[Ee]stado[^<]*alerta[^<]*)</a>', txt):
        titulo = html.unescape(titulo).strip()
        if not info["estado"] and ":" in titulo:
            info["estado"] = titulo.split(":")[-1].strip()
        info["url_noticia"] = info["url_noticia"] or href
        break
    return info


def fuente_noaa() -> pd.DataFrame:
    r = http_get(NOAA_SST, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    filas = []
    for linea in r.text.splitlines():
        m = re.match(r"\s*(\d{2}[A-Z]{3}\d{4})(.*)", linea)
        if not m:
            continue
        nums = re.findall(r"-?\d+\.\d", m.group(2))
        if len(nums) >= 2:
            filas.append({"semana": datetime.strptime(m.group(1).title(), "%d%b%Y").date(),
                          "nino12_sst": float(nums[0]), "nino12_anom": float(nums[1])})
    return pd.DataFrame(filas).tail(12).reset_index(drop=True)


def fuente_estaciones(variable: str, n: int) -> pd.DataFrame:
    """monitoreo_meteorologico:{variable}_{n}_all_points  (variable = tmax|tmin|prec)."""
    filas = []
    for f in wfs(f"monitoreo_meteorologico:{variable}_{n}_all_points"):
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        c = g.get("coordinates") or [None, None]
        filas.append({"estacion": p.get("estacion"), "departamento": p.get("departamen"),
                      "provincia": p.get("provincia"), "lon": c[0], "lat": c[1],
                      "valor": p.get(variable), "n": n})
    return pd.DataFrame(filas)


def fuente_poligonos(capa: str) -> list[dict]:
    return wfs(capa)


def fuente_tmin_10d(tiendas: pd.DataFrame) -> dict:
    """Pronostico Tmin 10 dias (grilla 0.1 grados) por punto unico de tienda."""
    res = {}
    for (lat, lon) in tiendas[["lat_r", "lon_r"]].drop_duplicates().itertuples(index=False):
        cql = f"long_min<={lon} AND long_max>{lon} AND lat_min<={lat} AND lat_max>{lat}"
        feats = wfs("movil_v2:tmin_model_num", cql=cql, maximo=1)
        if feats:
            p = feats[0]["properties"]
            res[(lat, lon)] = {"fecha": str(p.get("fecha", "")).rstrip("Z"),
                               "valores": [p.get(f"valor{i}") for i in range(1, 11)]}
    return res


BOLETINES_RAW: list[dict] = []


def es_aviso_lluvia(a) -> bool:
    return any(k in norm(a.get("evento", "")) for k in ("PRECIPIT", "LLUVIA", "TORMENTA", "GRANIZO"))
DEPARTAMENTOS_PE = ["AMAZONAS", "ANCASH", "APURIMAC", "AREQUIPA", "AYACUCHO", "CAJAMARCA", "CALLAO", "CUSCO",
                    "HUANCAVELICA", "HUANUCO", "ICA", "JUNIN", "LA LIBERTAD", "LAMBAYEQUE", "LIMA", "LORETO",
                    "MADRE DE DIOS", "MOQUEGUA", "PASCO", "PIURA", "PUNO", "SAN MARTIN", "TACNA", "TUMBES", "UCAYALI"]


def fuente_avisos_indeci() -> list[dict]:
    """Avisos meteorologicos SENAMHI republicados por INDECI (boletin informativo).
    Devuelve numero de aviso, resumen, vigencia, departamentos expuestos y enlaces."""
    from pypdf import PdfReader
    out, vistos = [], set()
    for b in sorted(BOLETINES_RAW, key=lambda x: x["fecha"], reverse=True):
        txt = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", " ", b["html"]))
        txt = re.sub(r"\s+", " ", txt).strip()
        # el aviso y su vigencia vienen en MAYUSCULAS, seguidos del texto en minusculas
        m = re.search(r"AVISO N[°º]\s*(\d+)\s*:\s*([^a-z]+?)\s+VIGENCIA\s*:\s*([^a-z]+?)(?=\s[A-ZÁÉÍÓÚÑ][a-záéíóúñ]|$)", txt)
        numero = m.group(1) if m else (re.search(r"N[°º]\s*(\d+)", b["titulo"]) or [None, "?"])[1]
        if numero in vistos:
            continue
        vistos.add(numero)
        pdf_url = (re.search(r'href="([^"]+\.pdf)"', b["html"]) or [None, ""])[1]
        if not pdf_url:                     # el PDF esta en la pagina del boletin, no en el RSS
            try:
                pag = http_get(b["link"], headers=HEADERS_WEB, timeout=TIMEOUT).text
                pdfs = [u for u in re.findall(r'href="([^"]+\.pdf)"', pag) if "AVISO" in norm(u)]
                pdf_url = pdfs[0] if pdfs else ""
            except Exception as err:  # noqa: BLE001
                log(f"     boletin {numero}: página no leída ({err})")
        deps = []
        if pdf_url:
            try:
                r = http_get(pdf_url, headers=HEADERS_WEB, timeout=TIMEOUT)
                if r.content[:4] == b"%PDF":
                    t = " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages[:2])
                    d = re.search(r"DEPARTAMENTOS?\s*EXPUESTOS?\s*:\s*(.+?)\s*\.", norm(t), re.S)
                    if d:   # el PDF pega palabras ("PUNO YTACNA"): se buscan los nombres de departamento conocidos
                        pegado = re.sub(r"[^A-Z]", "", d.group(1))
                        deps = [dep for dep in DEPARTAMENTOS_PE if dep.replace(" ", "") in pegado]
            except Exception as err:  # noqa: BLE001
                log(f"     boletin {numero}: PDF no leido ({err})")
        out.append({"numero": numero, "evento": (m.group(2).strip().capitalize() if m else ""),
                    "vigencia": (m.group(3).strip().capitalize() if m else ""),
                    "resumen": txt[:400], "departamentos": deps, "link": b["link"], "pdf": pdf_url,
                    "fecha": b["fecha"].astimezone().strftime("%d/%m %H:%M")})
    lluvia = ("PRECIPIT", "LLUVIA", "TORMENTA", "GRANIZO")
    out.sort(key=lambda a: (not any(k in norm(a["evento"]) for k in lluvia), -int(a["numero"]) if a["numero"].isdigit() else 0))
    return out


def fuente_corpac() -> dict:
    """Ultimas 30 h de reportes METAR de aeropuertos peruanos (CORPAC) via NOAA Aviation Weather Center."""
    r = http_get(METAR_API, params={"ids": ",".join(AEROPUERTOS_PE), "format": "json", "hours": 30},
                 headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    ahora = datetime.now().astimezone()
    out = {}
    for m in r.json():
        icao, temp = m.get("icaoId"), m.get("temp")
        if icao not in AEROPUERTOS_PE or temp is None or not m.get("reportTime"):
            continue
        ts = datetime.fromisoformat(m["reportTime"].replace("Z", "+00:00")).astimezone()
        est = out.setdefault(icao, {"nombre": AEROPUERTOS_PE[icao], "lat": m.get("lat"), "lon": m.get("lon"), "obs": []})
        wx = str(m.get("wxString") or "")
        tipo = ("tormenta" if "TS" in wx else "granizo" if "GR" in wx else "lluvia" if ("RA" in wx or "SH" in wx)
                else "llovizna (garúa)" if "DZ" in wx else "")
        vis = m.get("visib")
        try:
            vis = 6.21 if "+" in str(vis) else float(vis)           # millas; "6+" = 10 km o mas
        except (TypeError, ValueError):
            vis = None
        base = min((c["base"] for c in (m.get("clouds") or []) if c.get("base") is not None), default=None)
        est["obs"].append({"ts": ts, "temp": float(temp), "wx": wx, "lluvia": bool(tipo), "tipo": tipo,
                           "dewp": m.get("dewp"), "visib": vis, "base": base, "raw": m.get("rawOb", "")})
    for est in out.values():
        est["obs"].sort(key=lambda o: o["ts"])
        ult = est["obs"][-1]
        u24 = [o for o in est["obs"] if (ahora - o["ts"]).total_seconds() <= 24 * 3600]
        est.update({"ultima": ult, "tmax24": max(o["temp"] for o in u24) if u24 else None,
                    "tmin24": min(o["temp"] for o in u24) if u24 else None,
                    "lluvia_6h": [o for o in est["obs"] if o["lluvia"] and (ahora - o["ts"]).total_seconds() <= 6 * 3600],
                    "serie": [o["temp"] for o in u24][-24:]})
    return out


def _synop_temp(g):
    """Grupo snTTT -> grados C (None si falta)."""
    if len(g) != 4 or "/" in g or g[0] not in "01":
        return None
    return (-1 if g[0] == "1" else 1) * int(g[1:]) / 10


def _synop_lluvia(g):
    """Grupo RRRt de precipitacion -> (mm, horas)."""
    if len(g) != 4 or "/" in g[:3]:
        return None, None
    rrr = int(g[:3])
    mm = 0.0 if rrr == 990 else (rrr - 990) / 10 if rrr > 990 else float(rrr)
    horas = {"1": 6, "2": 12, "3": 18, "4": 24, "5": 1, "6": 2, "7": 3, "8": 9, "9": 15}.get(g[3])
    return mm, horas


def parse_synop(texto: str) -> dict:
    """Decodifica lo necesario de un parte SYNOP (FM-12): temperatura, max/min, lluvia y tiempo presente."""
    t = texto.replace("==", "").replace("=", "").split()
    if "AAXX" in t:
        t = t[t.index("AAXX") + 1:]
    if len(t) < 4 or "NIL" in t:
        return {}
    t = t[2:]                                    # quita YYGGi y el indicativo de estacion
    sec1, sec3 = (t[:t.index("333")], t[t.index("333") + 1:]) if "333" in t else (t, [])
    if "555" in sec3:
        sec3 = sec3[:sec3.index("555")]
    out = {}
    cuerpo = sec1[2:] if len(sec1) > 2 else []   # salta iRixhVV y Nddff
    if cuerpo and cuerpo[0].startswith("00"):
        cuerpo = cuerpo[1:]                      # viento >= 99 nudos
    for g in cuerpo:
        if len(g) != 5:
            continue
        k, resto = g[0], g[1:]
        if k == "1" and "temp" not in out:
            out["temp"] = _synop_temp(resto)
        elif k == "6":
            out["lluvia"] = _synop_lluvia(resto)
        elif k == "7" and resto[:2].isdigit():
            out["ww"] = int(resto[:2])
    for g in sec3:
        if len(g) != 5:
            continue
        k, resto = g[0], g[1:]
        if k == "1" and "tmax" not in out:
            out["tmax"] = _synop_temp(resto)
        elif k == "2" and "tmin" not in out:
            out["tmin"] = _synop_temp(resto)
        elif k == "6" and "lluvia" not in out:
            out["lluvia"] = _synop_lluvia(resto)
        elif k == "7" and resto.isdigit():
            out["lluvia24"] = int(resto) / 10
    return out


def tipo_ww(ww):
    if ww is None:
        return ""
    return ("tormenta" if 91 <= ww <= 99 or ww in (17, 29) else "chubasco" if 80 <= ww <= 90 else
            "lluvia" if 60 <= ww <= 69 else "llovizna (garúa)" if 50 <= ww <= 59 else "")


def fuente_omm() -> dict:
    """Partes SYNOP de las ultimas 30 h de las estaciones SENAMHI en la red OMM (via OGIMET)."""
    from datetime import timezone, timedelta
    ahora = datetime.now(timezone.utc)
    ini = ahora - timedelta(hours=30)
    r = http_get(OGIMET_SYNOP, params={"begin": ini.strftime("%Y%m%d%H00"), "end": ahora.strftime("%Y%m%d%H%M"),
                                       "state": "Peru"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    if "AAXX" not in r.text:
        raise ValueError("OGIMET no devolvió partes SYNOP: " + r.text[:120])
    out = {}
    for linea in r.text.splitlines():
        p = linea.split(",", 6)
        if len(p) < 7 or p[0] not in ESTACIONES_OMM:
            continue
        ts = datetime(int(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5]), tzinfo=timezone.utc).astimezone()
        d = parse_synop(p[6])
        if not d:
            continue
        nombre, lat, lon, alt = ESTACIONES_OMM[p[0]]
        out.setdefault(p[0], {"nombre": nombre, "lat": lat, "lon": lon, "alt": alt, "obs": []})["obs"].append({"ts": ts, **d})
    ahora_l = ahora.astimezone()
    for est in out.values():
        est["obs"].sort(key=lambda o: o["ts"])
        u24 = [o for o in est["obs"] if (ahora_l - o["ts"]).total_seconds() <= 24 * 3600]
        temps = [o["temp"] for o in u24 if o.get("temp") is not None]
        maxs = temps + [o["tmax"] for o in u24 if o.get("tmax") is not None]
        mins = temps + [o["tmin"] for o in u24 if o.get("tmin") is not None]
        con_t = [o for o in est["obs"] if o.get("temp") is not None]
        l24 = [o["lluvia24"] for o in u24 if o.get("lluvia24") is not None]
        l24 += [o["lluvia"][0] for o in u24 if o.get("lluvia") and o["lluvia"][0] is not None and o["lluvia"][1] == 24]
        ll6 = [o for o in est["obs"] if tipo_ww(o.get("ww")) and (ahora_l - o["ts"]).total_seconds() <= 6 * 3600]
        est.update({"ultima": con_t[-1] if con_t else None, "tmax24": max(maxs) if maxs else None,
                    "tmin24": min(mins) if mins else None, "prec24": max(l24) if l24 else None,
                    "lluvia_6h": [{"ts": o["ts"], "tipo": tipo_ww(o["ww"])} for o in ll6], "serie": temps[-24:]})
    return out


def fuente_nasa_power(tiendas: pd.DataFrame) -> dict:
    """Respaldo de temperatura/lluvia diaria (NASA POWER) por punto de tienda: ultimos 20 dias."""
    fin = datetime.now()
    ini = fin - pd.Timedelta(days=20)
    res = {}
    for (lat, lon) in tiendas[["lat_r", "lon_r"]].drop_duplicates().itertuples(index=False):
        r = http_get(NASA_POWER, params={"parameters": "T2M_MAX,T2M_MIN,PRECTOTCORR", "community": "AG",
                                         "latitude": lat, "longitude": lon, "start": ini.strftime("%Y%m%d"),
                                         "end": fin.strftime("%Y%m%d"), "format": "JSON"},
                     headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        p = r.json()["properties"]["parameter"]
        dias = sorted(k for k, v in p["T2M_MAX"].items() if v is not None and v > -900)
        if not dias:
            continue
        res[(lat, lon)] = {"fecha": dias[-1],
                           "tmax": [p["T2M_MAX"][d] for d in dias][-DIAS_SERIE:],
                           "tmin": [p["T2M_MIN"][d] for d in dias][-DIAS_SERIE:],
                           "prec": p["PRECTOTCORR"].get(dias[-1])}
    return res


def fuente_indeci() -> list[dict]:
    """Reportes COEN-INDECI de las ultimas HORAS_INDECI horas, solo eventos por lluvias."""
    from email.utils import parsedate_to_datetime
    import time
    limite = datetime.now().astimezone().timestamp() - HORAS_INDECI * 3600
    out, vistos = [], set()
    for pagina in range(1, MAX_PAGINAS_INDECI + 1):
        r = http_get(INDECI_FEED.format(pagina=pagina), headers=HEADERS_WEB, timeout=TIMEOUT)
        if r.status_code != 200:
            break
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
        if not items:
            break
        mas_antiguo = None
        for it in items:
            titulo = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", re.search(r"<title>(.*?)</title>", it, re.S).group(1)))
            titulo = re.sub(r"\s+", " ", titulo).strip()
            link = re.search(r"<link>(.*?)</link>", it, re.S).group(1).strip()
            fecha = parsedate_to_datetime(re.search(r"<pubDate>(.*?)</pubDate>", it).group(1))
            mas_antiguo = fecha.timestamp()
            if fecha.timestamp() < limite or link in vistos:
                continue
            vistos.add(link)
            t = norm(titulo)
            if "BOLETIN INFORMATIVO DE AVISO METEOROLOGICO" in t:
                if (datetime.now().astimezone().timestamp() - fecha.timestamp()) / 3600 <= HORAS_BOLETIN_AVISO:
                    cont = re.search(r"<content:encoded>(.*?)</content:encoded>", it, re.S)
                    BOLETINES_RAW.append({"titulo": titulo, "link": link, "fecha": fecha,
                                          "html": cont.group(1) if cont else ""})
                continue
            if not any(k in t for k in EVENTOS_LLUVIA) or "BOLETIN" in t:
                continue
            m = re.search(r"EN (?:EL|LOS) DISTRITOS? DE (.+?)\s+[–—-]\s+([A-ZÑ ]+?)\s*$", t)
            if not m:
                continue
            distritos = [d.strip() for d in re.split(r",| Y ", m.group(1)) if d.strip()]
            tipo = ("Peligro inminente" if "PELIGRO INMINENTE" in t else
                    "Informe de emergencia" if "INFORME DE EMERGENCIA" in t else "Reporte de emergencia")
            ev = re.search(r"\)\s*(?:POR\s+)?(.+?) EN (?:EL|LOS) DISTRITOS? DE", t)
            out.append({"fecha": fecha.astimezone().strftime("%d/%m %H:%M"), "tipo": tipo,
                        "horas": (datetime.now().astimezone().timestamp() - fecha.timestamp()) / 3600,
                        "evento": (ev.group(1).strip().capitalize() if ev else "Lluvias"),
                        "distritos": distritos, "departamento": m.group(2).strip(),
                        "titulo": titulo, "link": link})
        if mas_antiguo is not None and mas_antiguo < limite:
            break
        time.sleep(0.4)
    return out


def fuente_sigrid() -> dict:
    """Escenarios de riesgo por lluvias CENEPRED/SIGRID vigentes (con base en avisos SENAMHI).
    Devuelve riesgo por movimientos en masa y exposicion a inundaciones por (dep, prov, distrito)."""
    r = http_get(f"{SIGRID}/escenario-aviso/data-lluvia", headers=HEADERS_WEB, timeout=TIMEOUT)
    r.raise_for_status()
    filas = r.json()
    filas = filas.get("data", []) if isinstance(filas, dict) else filas
    ahora = pd.Timestamp(datetime.now())
    vigentes = []
    for f in filas:  # valida fechas (hay registros historicos mal digitados, p.ej. "82019-07-18")
        ini = pd.to_datetime(f.get("inicio_vigencia"), errors="coerce", format="%Y-%m-%d %H:%M:%S")
        fin = pd.to_datetime(f.get("fin_vigencia"), errors="coerce", format="%Y-%m-%d %H:%M:%S")
        if pd.isna(ini) or pd.isna(fin) or (fin - ini).days > 15:
            continue
        if fin >= ahora and ini <= ahora + pd.Timedelta(days=3):
            vigentes.append(f)
    res = {"escenarios": [], "mm": {}, "inund": {}}
    for f in sorted(vigentes, key=lambda f: str(f.get("inicio_vigencia"))):
        esc_id = f.get("id")
        res["escenarios"].append({"id": esc_id, "ambito": f.get("ambito", ""),
                                  "inicio": str(f.get("inicio_vigencia", ""))[:10], "fin": str(f.get("fin_vigencia", ""))[:10],
                                  "url": f"{SIGRID}/storage/escenario_aviso/{esc_id}_tabla.xlsx"})
        x = http_get(f"{SIGRID}/storage/escenario_aviso/{esc_id}_tabla.xlsx", headers=HEADERS_WEB, timeout=TIMEOUT)
        if x.status_code != 200 or x.content[:2] != b"PK":
            continue
        libro = pd.read_excel(io.BytesIO(x.content), sheet_name=None, header=None)
        for nombre, df in libro.items():
            n = nombre.lower()
            if n.startswith("distritos"):              # riesgo por movimientos en masa
                for fila in df.itertuples(index=False):
                    niv = str(fila[11]).strip().upper()
                    if niv in RIESGO_SIGRID_ORDEN:
                        k = (norm(fila[0]), norm(fila[1]), norm(fila[3]))
                        if RIESGO_SIGRID_ORDEN[niv] > RIESGO_SIGRID_ORDEN.get(res["mm"].get(k, "MB"), -1):
                            res["mm"][k] = niv
            elif n.startswith("centrospoblados"):      # exposicion a inundaciones por centro poblado
                inv = {"MUY ALTO": "MA", "ALTO": "A", "MEDIO": "M", "BAJO": "B", "MUY BAJO": "MB"}
                for fila in df.iloc[1:].itertuples(index=False):
                    niv = inv.get(norm(fila[8]))
                    if niv:
                        k = (norm(fila[0]), norm(fila[1]), norm(fila[2]))
                        if RIESGO_SIGRID_ORDEN[niv] > RIESGO_SIGRID_ORDEN.get(res["inund"].get(k, "MB"), -1):
                            res["inund"][k] = niv
    return res


def fuente_umbrales() -> pd.DataFrame:
    filas = []
    for f in wfs("g_umbrales:umbrales_precipitacion"):
        p = dict(f.get("properties") or {})
        c = (f.get("geometry") or {}).get("coordinates") or [None, None]
        p["lon"], p["lat"] = c[0], c[1]
        filas.append(p)
    df = pd.DataFrame(filas)
    if df.empty:
        return df
    for c in ["pp", "umbral", "pp_acum", "umb_acum"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["superado"] = (df["pp"] >= df["umbral"]) | (df["pp_acum"] >= df["umb_acum"])
    return df


# ----------------------------------------------------------------------------
# Procesamiento por tienda
# ----------------------------------------------------------------------------
def estacion_cercana(est: pd.DataFrame, lat, lon):
    if est is None or est.empty:
        return None
    d = est.dropna(subset=["lat", "lon", "valor"]).copy()
    if d.empty:
        return None
    d["dist"] = d.apply(lambda r: haversine_km(lat, lon, r["lat"], r["lon"]), axis=1)
    fila = d.loc[d["dist"].idxmin()]
    return fila if fila["dist"] <= DIST_MAX_ESTACION_KM else None


def sector_de(feats: list[dict], lon, lat) -> dict:
    for f in feats:
        if punto_en_geometria(lon, lat, f.get("geometry")):
            return f.get("properties") or {}
    return {}


def avisos_para(feats: list[dict], lon, lat, origen: str) -> list[dict]:
    out = []
    for f in feats:
        if punto_en_geometria(lon, lat, f.get("geometry")):
            p = f.get("properties") or {}
            niv = nivel_num(p.get("nivel") or p.get("nivel_aviso") or p.get("color"))
            if niv and niv < NIVEL_MIN_AVISO:
                continue  # Nivel 1 (verde) = sin necesidad de precaucion
            out.append({"origen": origen, "fuente": "SENAMHI",
                        "plan": 3 if niv >= 4 else 2,
                        "motivo": ("aviso rojo de SENAMHI" if niv >= 4 else
                                   f"{NIVEL_TXT.get(niv, ('aviso',))[0].lower()} de SENAMHI por lluvias"
                                   + (" que pueden activar quebradas" if "quebrada" in origen.lower() else "")),
                        "nivel": niv,
                        "titulo": p.get("titulo") or p.get("nombre") or p.get("evento") or origen,
                        "fecha": str(p.get("fecha") or p.get("fecha_inicio") or "").rstrip("Z"),
                        "detalle": (p.get("descripcion") or p.get("descripcio") or "")[:300]})
    return out


def estadistica_serie(serie):
    """Promedio y pendiente (C/dia) de la serie reciente SENAMHI."""
    pts = [(i, v) for i, v in enumerate(serie) if v is not None]
    if len(pts) < 5:
        return None, None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs) or 1
    pend = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return round(my, 1), round(pend, 2)


def url_noticias(ciudad):
    from urllib.parse import quote
    q = NOTICIAS_Q.format(ciudad=ciudad)
    return f"https://news.google.com/search?q={quote(q)}&hl=es-419&gl=PE&ceid=PE:es-419"


def txt_escenario(esc, var):
    esc = (esc or "").lower()
    if not esc:
        return ""
    if "-" in esc:  # p.ej. "Normal - Superior"
        partes = [ESC_TXT[var].get(p.strip(), "") for p in esc.split("-")]
        return " o ".join(p for p in partes if p) or esc
    return next((v for k, v in ESC_TXT[var].items() if k in esc), esc)


TIPO_AVISO_TXT = {"quebrada": "lluvias que pueden activar quebradas (huaicos)",
                  "umbral": "lluvia que ya superó el nivel de riesgo en estaciones del departamento",
                  "lluvia": "lluvias fuertes"}


def tipo_aviso(origen):
    o = origen.lower()
    return "quebrada" if "quebrada" in o else "umbral" if "umbral" in o else "lluvia"


def interpretar(r, enfen):
    """Lectura en lenguaje simple: lista de (etiqueta, texto) + acciones del plan."""
    lineas, claves = [], []
    nv = NIVELES_PLAN[r["nivel_plan"]]

    # 1) Nivel del plan y su motivo
    lineas.append(("Nivel", f"<b>Nivel {r['nivel_plan']} · {nv['nombre']}</b>. {e(r['motivo_nivel'])}"))
    sen = [a for a in r["avisos"] if a.get("fuente") == "SENAMHI"]
    if sen:
        a = max(sen, key=lambda a: a["nivel"])
        nombre, accion = NIVEL_TXT.get(a["nivel"], ("Aviso", "esté atento"))
        lineas.append(("SENAMHI", f"{nombre} por {TIPO_AVISO_TXT[tipo_aviso(a['origen'])]}. {accion.capitalize()}."))
    elif r.get("avisos_indeci") and not any(es_aviso_lluvia(x) for x in r["avisos_indeci"]):
        otros = r["avisos_indeci"]
        lineas.append(("SENAMHI", f"Sin avisos de lluvia para el departamento ({len(otros)} aviso(s) de otro tipo, p. ej. "
                                  f"N° {e(otros[0]['numero'])}{' ' + e(otros[0]['evento'].lower()) if otros[0]['evento'] else ''})."))
    elif r.get("avisos_indeci"):
        a = next(x for x in r["avisos_indeci"] if es_aviso_lluvia(x))
        lineas.append(("SENAMHI", f"Aviso N° {a['numero']}"
                                  + (f" ({e(a['evento'].lower())})" if a['evento'] else "") + " para el departamento"
                                  + (f", vigencia: {e(a['vigencia'].lower())}" if a['vigencia'] else "") + ". "
                                  f"<a href=\"{e(a['link'])}\" target=\"_blank\" rel=\"noopener\">Ver boletín</a>"))
    else:
        lineas.append(("SENAMHI", "Sin avisos para el departamento."))
    if r.get("indeci"):
        cerca = [x for x in r["indeci"] if x["mismo_distrito"]]
        if cerca:
            lineas.append(("INDECI", f"<b>{len(cerca)} reporte(s) por {cerca[0]['evento'].lower()} en el distrito de la tienda</b> "
                                     f"(últimas {HORAS_INDECI} h)."))
        else:
            otros = sorted({d.title() for x in r["indeci"] for d in x["distritos"]})
            cerca2 = [x for x in r["indeci"] if x["plan"] >= 2]
            donde = (f"a {DIST_INDECI_KM} km o menos de la tienda" if cerca2 else "el departamento, lejos de la tienda")
            lineas.append(("INDECI", f"{len(r['indeci'])} reporte(s) por lluvias en {donde} "
                                     f"({', '.join(otros[:4])}{'…' if len(otros) > 4 else ''}), últimas {HORAS_INDECI} h."))
    else:
        lineas.append(("INDECI", f"Sin emergencias por lluvias en el departamento (últimas {HORAS_INDECI} h)."))
    if r.get("sigrid_mm") or r.get("sigrid_inund"):
        partes = []
        if r.get("sigrid_mm"):
            partes.append(f"riesgo {RIESGO_SIGRID_TXT[r['sigrid_mm']].lower()} por movimientos en masa")
        if r.get("sigrid_inund"):
            partes.append(f"exposición {EXPOSICION_TXT[r['sigrid_inund']]} a inundaciones")
        lineas.append(("CENEPRED", "Escenario vigente: distrito con " + " y ".join(partes) + "."))

    # 2) Hoy
    if r.get("corpac"):
        c = r["corpac"]
        t = (f"{c['ahora']:.0f} °C a las {c['hora']} {c['lugar']}; "
             f"últimas 24 h: máxima {r['tmax']:.0f} °C, mínima {r['tmin']:.0f} °C")
        if c.get("prec24"):
            t += f", lluvia acumulada {c['prec24']:.1f} mm"
        if c["lluvia"]:
            t += f". <b>{c['lluvia_tipo'].capitalize()} observada</b> en las últimas 6 h (último reporte: {c['lluvia_hora']})"
        lineas.append(("Hoy", t + "."))
        if r["tmax"] >= UMBRAL_CALOR:
            claves.append("calor")
        if r["tmin"] is not None and r["tmin"] <= UMBRAL_FRIO:
            claves.append("frio")
    elif r["tmax"] is not None:
        t = (f"Referencia regional NASA ({r['fuente_temp'][6:-1]}): máxima {r['tmax']:.0f} °C" if r.get("fuente_temp", "").startswith("NASA")
             else f"Máxima {r['tmax']:.0f} °C")
        if r["tmin"] is not None:
            t += f", mínima {r['tmin']:.0f} °C"
        if r["prec"]:
            t += f", lluvia {r['prec']:.1f} mm"
        if r.get("prom_tmax") is not None:
            dif = r["tmax"] - r["prom_tmax"]
            t += (f" ({abs(dif):.0f} °C {'más caluroso' if dif > 0 else 'más fresco'} que lo habitual estas semanas)"
                  if abs(dif) >= DIF_ANOMALA else " (normal para estas semanas)")
        lineas.append(("Hoy", t + "."))
        if r["tmax"] >= UMBRAL_CALOR or (r.get("prom_tmax") and r["tmax"] - r["prom_tmax"] >= DIF_ANOMALA):
            claves.append("calor")
        if r["tmin"] is not None and r["tmin"] <= UMBRAL_FRIO:
            claves.append("frio")
    else:
        lineas.append(("Hoy", "Sin estación SENAMHI cercana con dato."))

    # 2b) Pronostico de lluvia (ECMWF conjunto + GFS) y garua en Lima
    if r.get("pron_lluvia"):
        lineas.append(("Lluvia", " · ".join(f"<b>{e(d['dia'])}:</b> {e(d['frase'])}" for d in r["pron_lluvia"])
                       + ' <span class="muted">(modelos ECMWF y NOAA; desde las 7 a. m. de cada día)</span>.'))
        mm_txt = lluvia_vs_normal(r)
        if mm_txt:
            lineas.append(("Lluvia en mm", mm_txt))
    if r.get("garua"):
        g = r["garua"]
        t = e(g.get("frase", ""))
        if (r.get("garua_ahora") or {}).get("hay"):
            t = f"<b>{e(r['garua_ahora']['frase'])}</b> " + t
        lineas.append(("Garúa", t))

    # 3) Tendencia (ultimos 15 registros SENAMHI)
    tend = r.get("tend_tmax")
    if tend is not None:
        semana = tend * 7
        lineas.append(("Tendencia", "Estable." if abs(semana) < 1 else
                       f"{'Subiendo' if semana > 0 else 'Bajando'} ≈{abs(semana):.0f} °C por semana."))

    # 4) Pronostico oficial
    pm = [x for x in (txt_escenario(r["pm_pp_esc"], "pp"), txt_escenario(r["pm_tmax_esc"], "tmax")) if x]
    if pm:
        lineas.append(("Próximo mes", (" y ".join(pm)).capitalize() + " (SENAMHI)."))
    if r.get("estacional_cache") and (pm or r["verano_esc"]):
        lineas.append(("Nota", f"Pronóstico estacional SENAMHI guardado el {e(r['estacional_cache'])}."))
    if r["verano_esc"]:
        lineas.append(("Verano", f"{txt_escenario(r['verano_esc'], 'pp').capitalize()} entre "
                                 f"{str(r['verano_meta']).replace('_', ' y ').lower()} (SENAMHI)."))

    # 5) Estimacion propia con historial
    if r.get("proy_tmax"):
        lineas.append(("Estimación", f"Máxima ≈{r['proy_tmax'][-1]:.0f} °C en 3 días (cálculo propio, referencial)."))

    acciones = list(nv["acciones"])
    acciones += [ACCIONES_TEMP[c] for c in dict.fromkeys(claves) if c in ACCIONES_TEMP]
    return {"lineas": lineas, "acciones": acciones,
            "texto": " ".join(f"{k}: {re.sub('<[^>]+>', '', v)}" for k, v in lineas)}


def proyeccion_historial(filas):
    """Estimacion simple a 3 dias con el historial propio (regresion lineal ultimos N dias)."""
    ruta = CARPETA_HIST / "clima_tiendas.csv"
    h = pd.read_csv(ruta, dtype={"cod_p": str}) if ruta.exists() else pd.DataFrame()
    for f in filas:
        f["proy_tmax"], f["dias_hist"] = None, 0
        if h.empty:
            continue
        d = h[h["cod_p"] == f["cod_p"]].copy()
        d["tmax"] = pd.to_numeric(d["tmax"], errors="coerce")
        d = d.dropna(subset=["tmax"]).sort_values("fecha_consulta").tail(MIN_DIAS_PROYECCION)
        f["dias_hist"] = len(d)
        if len(d) >= MIN_DIAS_PROYECCION:
            fechas = pd.to_datetime(d["fecha_consulta"])
            x = [(fe - fechas.min()).days for fe in fechas]
            _, pend = estadistica_serie(list(d["tmax"]))
            mx, my = sum(x) / len(x), d["tmax"].mean()
            f["proy_tmax"] = [round(my + pend * (x[-1] + k - mx), 1) for k in (1, 2, 3)]


def nivel_del_plan(avisos):
    """Nivel del Plan FEN (1 a 3; el 4 es manual) segun las fuentes oficiales:
    - Nivel 2 Amarilla: aviso SENAMHI amarillo/naranja sobre la tienda, quebradas a <= DIST_QUEBRADA_KM,
      evento INDECI por lluvias a <= DIST_INDECI_KM, umbral superado entre 15 y 30 km,
      o distrito en riesgo Alto/Muy alto (CENEPRED).
    - Nivel 3 Roja: aviso SENAMHI rojo, umbral superado a <= DIST_UMBRAL_ROJA_KM,
      o emergencia / peligro inminente INDECI por lluvias en el mismo distrito (ultimas 24 h)."""
    nivel, motivo = 1, "Temporada de lluvias: monitoreo preventivo."
    for a in sorted(avisos, key=lambda a: -a.get("plan", 1)):
        if a.get("plan", 1) > nivel:
            m = a.get("motivo", a["origen"])
            nivel, motivo = a["plan"], m[0].upper() + m[1:] + "."
    return nivel, motivo


def leer_manuales() -> dict:
    """alertas_manuales.csv: cod_p, nivel (1-4), motivo, vigente_hasta (AAAA-MM-DD, vacio = sin fin)."""
    if not ARCH_MANUALES.exists():
        return {}
    df = pd.read_csv(ARCH_MANUALES, dtype=str).fillna("")
    out = {}
    for fila in df.itertuples(index=False):
        hasta = str(getattr(fila, "vigente_hasta", "")).strip()
        if hasta and hasta < HOY.strftime("%Y-%m-%d"):
            continue
        try:
            niv = int(str(fila.nivel).strip())
        except ValueError:
            continue
        if 1 <= niv <= 4 and str(fila.cod_p).strip():
            out[str(fila.cod_p).strip()] = {"nivel": niv, "motivo": str(getattr(fila, "motivo", "")).strip() or "sin detalle"}
    return out


def leer_ubigeo() -> dict:
    """(departamento, distrito) -> [(provincia, lat, lon)]  desde ubigeo_distritos.csv (INEI)."""
    if not ARCH_UBIGEO.exists():
        return {}
    out = {}
    df = pd.read_csv(ARCH_UBIGEO, dtype=str).fillna("")
    for fila in df.itertuples(index=False):
        lat = float(fila.lat) if getattr(fila, "lat", "") else None
        lon = float(fila.lon) if getattr(fila, "lon", "") else None
        out.setdefault((norm(fila.departamento), norm(fila.distrito)), []).append((norm(fila.provincia), lat, lon))
    return out


def senamhi_caido() -> bool:
    return "idesep.senamhi.gob.pe" in _HOSTS_CAIDOS


CLAVES_ESTACIONAL = [f"{c}_{k}" for c in ("pm_tmax", "pm_tmin", "pm_pp", "verano") for k in ("esc", "sector", "meta")]


def guardar_cache_estacional(filas):
    ARCH_CACHE_ESTACIONAL.parent.mkdir(exist_ok=True)
    data = {str(f["cod_p"]): {**{k: f.get(k, "") for k in CLAVES_ESTACIONAL}, "_emitido": HOY.strftime("%d/%m/%Y")}
            for f in filas if f.get("verano_esc") or f.get("pm_tmax_esc")}
    ARCH_CACHE_ESTACIONAL.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def leer_cache_estacional() -> dict:
    try:
        return json.loads(ARCH_CACHE_ESTACIONAL.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def fase_actual():
    for meses, nombre, objetivo in FASES:
        if HOY.month in meses:
            return nombre, objetivo
    return "", ""


HORAS_LLUVIA_AHORA = 3        # lluvia o garua observada en el aeropuerto en estas ultimas horas

# ----------------------------------------------------------------------------
# Lluvia en mm frente a lo normal (clima_normal.json, generado por climatologia_lluvia.py)
# ----------------------------------------------------------------------------
ARCH_CLIMA_NORMAL = CARPETA / "clima_normal.json"
MESES_TXT = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
             "septiembre", "octubre", "noviembre", "diciembre"]
# Categorias SENAMHI de lluvia diaria por percentiles (dias con lluvia >= 1 mm)
CAT_LLUVIA = [(0, "Sin lluvia significativa", "ll-0"), (1, "Normal para la época", "ll-1"),
              (2, "Moderadamente lluvioso", "ll-2"), (3, "Muy lluvioso", "ll-3"),
              (4, "Extremadamente lluvioso", "ll-4")]
_CLIMA_CACHE = None


def clima_normal() -> dict:
    global _CLIMA_CACHE
    if _CLIMA_CACHE is None:
        try:
            _CLIMA_CACHE = json.loads(ARCH_CLIMA_NORMAL.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _CLIMA_CACHE = {}
    return _CLIMA_CACHE


def categoria_lluvia(mm, n: dict) -> int:
    if mm is None or mm < 1:
        return 0
    if mm > n["p99"]:
        return 4
    if mm > n["p95"]:
        return 3
    if mm > n["p90"]:
        return 2
    return 1


def fmt_mm(v) -> str:
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}".replace(".", ",")


def lluvia_vs_normal(r) -> str | None:
    """Linea 'Lluvia en mm' de la tarjeta: pronostico en mm por dia, ubicado en la escala de la tienda."""
    cn = clima_normal()
    t = (cn.get("tiendas") or {}).get(str(r.get("cod_p")))
    if not t or not r.get("pron_lluvia"):
        return None
    mes = datetime.now().month
    n = t["meses"][str(mes)]
    chips, peor, peor_mm = [], 0, 0.0
    for d in r["pron_lluvia"]:
        vals = [v for v in (d.get("mm_modelos"), d.get("gfs_mm")) if v is not None]
        if not vals:
            continue
        lo, hi = min(vals), max(vals)
        cat = categoria_lluvia(hi, n)          # se clasifica con el modelo mas lluvioso (criterio conservador)
        rango = f"{fmt_mm(lo)}–{fmt_mm(hi)}" if hi - lo >= 1 else fmt_mm(hi)
        chips.append(f'<span class="ll {CAT_LLUVIA[cat][2]}"><b>{e(d["dia"])}</b> {rango} mm · {CAT_LLUVIA[cat][1]}</span>')
        if hi > peor_mm:
            peor, peor_mm = cat, hi
    if not chips:
        return None
    # Barra: 0 .. p99*1.3, con marcas en p90 / p95 / p99 y el dia mas lluvioso previsto
    tope = max(n["p99"] * 1.3, peor_mm * 1.05, 1)
    pos = lambda v: f"{min(v / tope, 1) * 100:.1f}%"  # noqa: E731
    barra = (f'<div class="ll-barra" title="Día más lluvioso previsto: {fmt_mm(peor_mm)} mm">'
             f'<i class="z1" style="width:{pos(n["p90"])}"></i>'
             f'<i class="z2" style="left:{pos(n["p90"])};width:calc({pos(n["p95"])} - {pos(n["p90"])})"></i>'
             f'<i class="z3" style="left:{pos(n["p95"])};width:calc({pos(n["p99"])} - {pos(n["p95"])})"></i>'
             f'<i class="z4" style="left:{pos(n["p99"])};right:0"></i>'
             f'<b class="mk" style="left:{pos(peor_mm)}"></b></div>'
             f'<div class="ll-ejes"><span>0</span><span style="left:{pos(n["p90"])}">{fmt_mm(n["p90"])}</span>'
             f'<span style="left:{pos(n["p95"])}">{fmt_mm(n["p95"])}</span>'
             f'<span style="left:{pos(n["p99"])}">{fmt_mm(n["p99"])} mm</span></div>')
    dias10 = n["frec_lluvia"] / 10
    frec = ("casi nunca llueve" if dias10 < 0.3 else
            f"llueve ~{max(round(dias10), 1)} de cada 10 días")
    ref = (f'<span class="muted">Normal en {MESES_TXT[mes]} aquí: {frec}, ~{fmt_mm(n["normal_mes"])} mm en el mes. '
           f'Día moderadamente lluvioso desde {fmt_mm(n["p90"])} mm, muy lluvioso desde {fmt_mm(n["p95"])} mm, '
           f'extremo desde {fmt_mm(n["p99"])} mm. 1 mm = 1 litro por m². '
           f'Fuente de lo normal: {e(cn.get("fuente", ""))}.</span>')
    return f'<div class="ll-chips">{"".join(chips)}</div>{barra}{ref}'


def lluvia_observada(corpac, lat, lon):
    """Lluvia/garua reportada en las ultimas horas por el aeropuerto mas cercano (a 60 km o menos)."""
    ahora = datetime.now().astimezone()
    cerca = sorted(((haversine_km(lat, lon, a["lat"], a["lon"]), a) for a in corpac.values()
                    if a.get("lat") is not None and a.get("obs")), key=lambda x: x[0])
    if not cerca or cerca[0][0] > DIST_MAX_AEROPUERTO_KM:
        return None
    dist, a = cerca[0]
    rec = [o for o in a["obs"] if o["lluvia"] and (ahora - o["ts"]).total_seconds() <= HORAS_LLUVIA_AHORA * 3600]
    if not rec:
        return None
    u = rec[-1]
    return {"tipo": u["tipo"], "hora": u["ts"].strftime("%H:%M"), "lugar": a["nombre"], "dist": dist}


def procesar(tiendas, datos):
    filas = []
    est = datos["estaciones"]
    for t in tiendas.itertuples(index=False):
        r = {k: getattr(t, k) for k in tiendas.columns}

        # Temperatura / lluvia observada (estacion SENAMHI mas cercana, registro N=1)
        for var in ("tmax", "tmin", "prec"):
            e = estacion_cercana(est.get((var, 1)), t.lat, t.lon)
            r[var] = None if e is None else float(e["valor"])
            if var == "tmax":
                r["estacion"] = "" if e is None else f"{e['estacion']} ({e['dist']:.0f} km)"
            if var in ("tmax", "tmin"):
                serie = []
                if e is not None:
                    for n in range(DIAS_SERIE, 0, -1):          # orden: mas antiguo -> mas reciente
                        d = est.get((var, n))
                        v = None
                        if d is not None and not d.empty:
                            m = d[d["estacion"] == e["estacion"]]
                            v = None if m.empty else float(m["valor"].iloc[0])
                        serie.append(v)
                r[f"serie_{var}"] = serie
                r[f"prom_{var}"], r[f"tend_{var}"] = estadistica_serie(serie)

        # Si no hay estacion SENAMHI: aeropuerto CORPAC cercano; si tampoco, respaldo NASA POWER
        r["fuente_temp"] = "SENAMHI" if r["tmax"] is not None else ""
        r["corpac"] = None
        ahora_l = datetime.now().astimezone()
        cand_omm = sorted(((haversine_km(t.lat, t.lon, a["lat"], a["lon"]), c, a) for c, a in (datos.get("omm") or {}).items()
                           if a["alt"] < ALT_MAX_OMM_M and a.get("ultima") and a["tmax24"] is not None
                           and (ahora_l - a["ultima"]["ts"]).total_seconds() <= HORAS_MAX_OMM * 3600), key=lambda x: x[0])
        cand_omm = [c for c in cand_omm if c[0] <= DIST_MAX_OMM_KM]
        cand_aer = sorted(((haversine_km(t.lat, t.lon, a["lat"], a["lon"]), c, a) for c, a in (datos.get("corpac") or {}).items()
                           if a.get("lat") is not None and a.get("tmax24") is not None), key=lambda x: x[0])
        cand_aer = [c for c in cand_aer if c[0] <= DIST_MAX_AEROPUERTO_KM]
        if r["tmax"] is None and cand_omm and (not cand_aer or cand_omm[0][0] <= cand_aer[0][0]):
            dist, wmo, a = cand_omm[0]
            r["tmax"], r["tmin"], r["prec"] = a["tmax24"], a["tmin24"], a["prec24"]
            r["serie_tmax"], r["serie_tmin"] = a["serie"], []
            r["prom_tmax"] = r["tend_tmax"] = r["prom_tmin"] = r["tend_tmin"] = None
            hora = a["ultima"]["ts"].strftime("%H:%M")
            r["corpac"] = {"icao": wmo, "nombre": a["nombre"], "dist": dist, "ahora": a["ultima"]["temp"], "hora": hora,
                           "lluvia": bool(a["lluvia_6h"]), "lugar": f"en la estación SENAMHI {a['nombre']} (red OMM)",
                           "lluvia_hora": a["lluvia_6h"][-1]["ts"].strftime("%H:%M") if a["lluvia_6h"] else "",
                           "lluvia_tipo": a["lluvia_6h"][-1]["tipo"] if a["lluvia_6h"] else "", "prec24": a["prec24"]}
            r["estacion"] = f"Estación SENAMHI {a['nombre']} ({dist:.0f} km), parte OMM de las {hora}"
            r["fuente_temp"] = "SENAMHI (OMM)"
        if r["tmax"] is None and datos.get("corpac"):
            cand = cand_aer
            if cand:
                dist, icao, a = cand[0]
                r["tmax"], r["tmin"], r["prec"] = a["tmax24"], a["tmin24"], None
                r["serie_tmax"], r["serie_tmin"] = a["serie"], []
                r["prom_tmax"] = r["tend_tmax"] = r["prom_tmin"] = r["tend_tmin"] = None
                hora = a["ultima"]["ts"].strftime("%H:%M")
                r["corpac"] = {"icao": icao, "nombre": a["nombre"], "dist": dist, "ahora": a["ultima"]["temp"],
                               "hora": hora, "lluvia": bool(a["lluvia_6h"]),
                               "lluvia_hora": a["lluvia_6h"][-1]["ts"].strftime("%H:%M") if a["lluvia_6h"] else "",
                               "lluvia_tipo": a["lluvia_6h"][-1]["tipo"] if a["lluvia_6h"] else "",
                               "lugar": f"en el aeropuerto de {a['nombre'].split(' – ')[0]} (CORPAC)"}
                r["estacion"] = f"CORPAC · Aeropuerto {a['nombre']} ({dist:.0f} km), reporte {hora}"
                r["fuente_temp"] = "CORPAC"
        nasa = datos.get("nasa", {}).get((t.lat_r, t.lon_r))
        if r["tmax"] is None and nasa:
            r["tmax"], r["tmin"], r["prec"] = nasa["tmax"][-1], nasa["tmin"][-1], nasa["prec"]
            r["serie_tmax"], r["serie_tmin"] = nasa["tmax"], nasa["tmin"]
            for var in ("tmax", "tmin"):
                r[f"prom_{var}"], r[f"tend_{var}"] = estadistica_serie(r[f"serie_{var}"])
            fch = f"{nasa['fecha'][6:8]}/{nasa['fecha'][4:6]}"
            r["estacion"] = f"NASA POWER, referencia regional (celda de ~50 km), dato del {fch}"
            r["fuente_temp"] = f"NASA ({fch})"

        # Pronostico Tmin 10 dias
        p10 = datos["tmin10"].get((t.lat_r, t.lon_r), {})
        r["tmin10"], r["tmin10_fecha"] = p10.get("valores", []), p10.get("fecha", "")

        # Pronostico estacional por sector SENAMHI
        for clave, capa in (("pm_tmax", "pm_tmax"), ("pm_tmin", "pm_tmin"), ("pm_pp", "pm_pp"), ("verano", "verano")):
            s = sector_de(datos[capa], t.lon, t.lat)
            r[f"{clave}_esc"] = s.get("escenario", "")
            r[f"{clave}_sector"] = s.get("sectores", "")
            r[f"{clave}_meta"] = s.get("mes") or s.get("trimestre") or ""
        cache = datos.get("cache_estacional", {}).get(str(t.cod_p), {})
        if not r["verano_esc"] and not r["pm_tmax_esc"] and cache:      # SENAMHI no disponible: ultimo valor guardado
            for k, v in cache.items():
                if k != "_emitido":
                    r[k] = v
            r["estacional_cache"] = cache.get("_emitido", "")
        r["sector"] = r["verano_sector"] or r["pm_tmax_sector"]

        # Avisos SENAMHI republicados por INDECI (solo informativo, a nivel departamento)
        r["avisos_indeci"] = [a for a in datos.get("avisos_indeci", [])
                              if norm(t.departamento) in [norm(d) for d in a["departamentos"]]]

        # Avisos
        av = []
        av += avisos_para(datos["aviso_met"], t.lon, t.lat, "Aviso meteorológico SENAMHI")
        av += avisos_para(datos["aviso_24h"], t.lon, t.lat, "Aviso lluvia 24 h / quebradas")
        q = []
        for f in datos["quebradas"]:
            p = f.get("properties") or {}
            if nivel_num(p.get("nivel")) < NIVEL_MIN_AVISO or norm(p.get("nombdep")) != norm(t.departamento):
                continue            # filtro rapido por departamento antes de medir distancias
            d = dist_a_geometria_km(t.lat, t.lon, f.get("geometry"))
            if d <= DIST_QUEBRADA_KM:
                q.append({**p, "_dist": d})
        if q:
            niv = max(nivel_num(p.get("nivel")) for p in q)
            dmin = min(p["_dist"] for p in q)
            distritos = sorted({str(p.get("nombdist", "")).title() for p in q})
            av.append({"origen": f"Activación de quebradas a {dmin:.1f} km", "nivel": niv, "fuente": "SENAMHI",
                       "plan": 2, "motivo": f"posible activación de quebradas a {dmin:.1f} km de la tienda (SENAMHI)",
                       "titulo": f"{len(q)} zona(s) en {', '.join(distritos[:6])}{'…' if len(distritos) > 6 else ''}",
                       "fecha": str(q[0].get("fecha", "")).rstrip("Z"), "detalle": q[0].get("descripcion", "")[:300]})
        u = datos["umbrales"]
        if not u.empty:
            uu = u[u["superado"] & u["lat"].notna()].copy()
            if not uu.empty:
                uu["dist"] = uu.apply(lambda x: haversine_km(t.lat, t.lon, float(x["lat"]), float(x["lon"])), axis=1)
                uu = uu[uu["dist"] <= DIST_UMBRAL_AMARILLA_KM].sort_values("dist")
            if not uu.empty:
                dmin = float(uu["dist"].iloc[0])
                roja = dmin <= DIST_UMBRAL_ROJA_KM
                av.append({"origen": f"Lluvia sobre el umbral a {dmin:.0f} km", "nivel": 4 if roja else 3,
                           "fuente": "SENAMHI", "plan": 3 if roja else 2,
                           "motivo": f"lluvia por encima del umbral de riesgo en una estación a {dmin:.0f} km (SENAMHI)",
                           "titulo": ", ".join(f"{n} ({d:.0f} km)" for n, d in zip(uu["nombre"].astype(str).head(4), uu["dist"].head(4))),
                           "fecha": str(uu["fecha"].iloc[0]), "detalle": ""})
        # INDECI: emergencias por lluvias (mismo distrito -> Roja; mismo departamento -> Amarilla)
        rel = []
        ubi = datos.get("ubigeo", {})
        for rep in datos["indeci"]:
            if norm(rep["departamento"]) != norm(t.departamento):
                continue
            ubic = [u for d in rep["distritos"] for u in ubi.get((norm(rep["departamento"]), norm(d)), [])]
            mismo = any(norm(d) == norm(t.distrito) for d in rep["distritos"]) and \
                (not ubic or any(u[0] == norm(t.provincia) for u in ubic))
            dist = min((haversine_km(t.lat, t.lon, u[1], u[2]) for u in ubic if u[1] is not None), default=None)
            reciente = rep["horas"] <= HORAS_INDECI_ROJA
            if mismo and reciente:
                plan, ambito = 3, "en el distrito de la tienda"
            elif mismo:
                plan, ambito = 2, f"en el distrito de la tienda (hace {rep['horas']:.0f} h)"
            elif dist is not None and dist <= DIST_INDECI_KM:
                plan, ambito = 2, f"a {dist:.0f} km de la tienda"
            else:
                plan, ambito = 1, ("en el departamento" if dist is None else f"a {dist:.0f} km, en el departamento")
            rel.append({**rep, "mismo_distrito": mismo, "ambito": ambito, "plan": plan})
            av.append({"origen": f"INDECI · {rep['tipo']}", "fuente": "INDECI", "nivel": 0,
                       "plan": plan,
                       "motivo": (f"{rep['tipo'].lower()} de INDECI por {rep['evento'].lower()} {ambito}"
                                  + ("" if mismo else f" ({', '.join(d.title() for d in rep['distritos'][:3])})")),
                       "titulo": rep["titulo"], "fecha": rep["fecha"], "link": rep["link"], "detalle": ""})
        r["indeci"] = sorted(rel, key=lambda x: not x["mismo_distrito"])

        # CENEPRED / SIGRID: riesgo del distrito en escenarios vigentes
        k = (norm(t.departamento), norm(t.provincia), norm(t.distrito))
        r["sigrid_mm"], r["sigrid_inund"] = datos["sigrid"]["mm"].get(k, ""), datos["sigrid"]["inund"].get(k, "")
        for clave, txt in (("sigrid_mm", "movimientos en masa"), ("sigrid_inund", "exposición a inundaciones")):
            if r[clave] in ("MA", "A"):
                av.append({"origen": f"CENEPRED/SIGRID · {txt}", "fuente": "SIGRID", "nivel": 0, "plan": 2,
                           "motivo": (f"escenario de riesgo CENEPRED: distrito en riesgo {RIESGO_SIGRID_TXT[r[clave]].lower()} por {txt}"
                                      if clave == "sigrid_mm" else
                                      f"escenario de riesgo CENEPRED: distrito con exposición {EXPOSICION_TXT[r[clave]]} a inundaciones"),
                           "titulo": f"Riesgo {RIESGO_SIGRID_TXT[r[clave]]}", "fecha": "", "link": LINKS["CENEPRED SIGRID escenarios"],
                           "detalle": ""})

        r["avisos"] = av
        r["nivel_plan"], r["motivo_nivel"] = nivel_del_plan(av)
        man = datos.get("manuales", {}).get(t.cod_p)
        r["manual"] = bool(man)
        if man:
            r["nivel_plan"], r["motivo_nivel"] = man["nivel"], f"Registro manual: {man['motivo']}"
        r["semaforo"] = NIVELES_PLAN[r["nivel_plan"]]["clase"]
        r["url_noticias"] = url_noticias(t.ciudad)
        pron = datos.get("pronostico") or {}
        r["pron_lluvia"] = (pron.get("lluvia") or {}).get(str(t.cod_p), [])
        r["garua"] = pron.get("garua") if t.zona == "Lima Metropolitana" else None
        r["garua_ahora"] = pron.get("garua_ahora") if t.zona == "Lima Metropolitana" else None
        r["lluvia_ahora"] = lluvia_observada(datos.get("corpac") or {}, t.lat, t.lon)
        filas.append(r)
    return filas


# ----------------------------------------------------------------------------
# Historico
# ----------------------------------------------------------------------------
def guardar_historial(filas, enfen, noaa):
    CARPETA_HIST.mkdir(exist_ok=True)
    fecha = HOY.strftime("%Y-%m-%d")

    def anexar(nombre, df, claves):
        ruta = CARPETA_HIST / nombre
        if ruta.exists():
            viejo = pd.read_csv(ruta, dtype=str)
            viejo = viejo[~viejo.set_index(claves).index.isin(df.astype(str).set_index(claves).index)]
            df = pd.concat([viejo, df.astype(str)], ignore_index=True)
        df.to_csv(ruta, index=False, encoding="utf-8-sig")

    anexar("clima_tiendas.csv", pd.DataFrame([{
        "fecha_consulta": fecha, "cod_p": f["cod_p"], "tienda": f["tienda"], "zona": f["zona"],
        "tmax": f["tmax"], "tmin": f["tmin"], "prec": f["prec"], "estacion": f["estacion"],
        "semaforo": f["semaforo"], "nivel_plan": f["nivel_plan"], "riesgo_plan": f["riesgo_plan"],
        "n_avisos": len(f["avisos"]),
        "max_nivel_aviso": max([a["nivel"] for a in f["avisos"]], default=0)} for f in filas]),
        ["fecha_consulta", "cod_p"])
    anexar("enfen.csv", pd.DataFrame([{"fecha_consulta": fecha, **enfen}]), ["fecha_consulta"])
    if noaa is not None and not noaa.empty:
        anexar("noaa_nino12.csv", noaa.assign(semana=noaa["semana"].astype(str)), ["semana"])


def persistencia_y_cambios(filas):
    """(a) Mantiene por PERSISTENCIA_HORAS el nivel mas alto calculado recientemente.
    (c) Marca las instalaciones que cambiaron de nivel desde la corrida anterior.
    Registra cada corrida en historial/niveles.csv."""
    CARPETA_HIST.mkdir(exist_ok=True)
    ruta = CARPETA_HIST / "niveles.csv"
    h = pd.read_csv(ruta, dtype={"cod_p": str}) if ruta.exists() else pd.DataFrame()
    ahora = pd.Timestamp(HOY)
    if not h.empty:
        h["ts"] = pd.to_datetime(h["fecha_hora"], errors="coerce")
    nuevas = []
    for f in filas:
        f["nivel_calculado"], motivo_calc = f["nivel_plan"], f["motivo_nivel"]
        f["cambio"], f["nivel_anterior"] = None, None
        hf = h[h["cod_p"] == f["cod_p"]].dropna(subset=["ts"]) if not h.empty else pd.DataFrame()
        if not hf.empty:
            f["nivel_anterior"] = int(hf.sort_values("ts").iloc[-1]["nivel_final"])
            rec = hf[hf["ts"] >= ahora - pd.Timedelta(hours=PERSISTENCIA_HORAS)]
            if not rec.empty and not f.get("manual"):
                fila = rec.loc[rec["nivel_calculado"].astype(int).idxmax()]
                if int(fila["nivel_calculado"]) > f["nivel_plan"]:
                    f["nivel_plan"] = int(fila["nivel_calculado"])
                    f["motivo_nivel"] = (f"Se mantiene por {PERSISTENCIA_HORAS} h desde el {fila['ts']:%d/%m %H:%M}: "
                                         f"{str(fila['motivo']).rstrip('.')}.")
                    f["semaforo"] = NIVELES_PLAN[f["nivel_plan"]]["clase"]
            if f["nivel_anterior"] is not None and f["nivel_plan"] != f["nivel_anterior"]:
                f["cambio"] = "sube" if f["nivel_plan"] > f["nivel_anterior"] else "baja"
        nuevas.append({"fecha_hora": HOY.strftime("%Y-%m-%d %H:%M"), "cod_p": f["cod_p"], "tienda": f["tienda"],
                       "nivel_calculado": f["nivel_calculado"], "nivel_final": f["nivel_plan"], "motivo": motivo_calc})
    out = pd.concat([h.drop(columns=["ts"], errors="ignore"), pd.DataFrame(nuevas)], ignore_index=True)
    out.to_csv(ruta, index=False, encoding="utf-8-sig")


def delta_vs_ayer(filas):
    ruta = CARPETA_HIST / "clima_tiendas.csv"
    if not ruta.exists():
        return
    h = pd.read_csv(ruta, dtype={"cod_p": str})
    h = h[h["fecha_consulta"] < HOY.strftime("%Y-%m-%d")]
    if h.empty:
        return
    ult = h.sort_values("fecha_consulta").groupby("cod_p").tail(1).set_index("cod_p")
    for f in filas:
        if f["cod_p"] in ult.index and f["tmax"] is not None:
            prev = pd.to_numeric(ult.loc[f["cod_p"], "tmax"], errors="coerce")
            f["delta_tmax"] = None if pd.isna(prev) else round(f["tmax"] - float(prev), 1)


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
def e(x):
    return html.escape("" if x is None else str(x))


def sparkline(valores, ancho=110, alto=28):
    v = [x for x in valores if x is not None]
    if len(v) < 2:
        return '<span class="muted">—</span>'
    lo, hi = min(v), max(v)
    rango = (hi - lo) or 1
    paso = ancho / (len(valores) - 1)
    pts = [f"{i * paso:.1f},{alto - 3 - (x - lo) / rango * (alto - 6):.1f}"
           for i, x in enumerate(valores) if x is not None]
    ux, uy = pts[-1].split(",")
    return (f'<svg class="spark" viewBox="0 0 {ancho} {alto}" width="{ancho}" height="{alto}" '
            f'role="img" aria-label="min {lo} max {hi}"><polyline points="{" ".join(pts)}" />'
            f'<circle cx="{ux}" cy="{uy}" r="2.5"/></svg>'
            f'<span class="spark-rango">{lo:.0f}–{hi:.0f}°</span>')


ESC_CLASE = {"superior": "esc-sup", "inferior": "esc-inf", "bajo": "esc-inf", "normal": "esc-nor"}


def chip_escenario(esc):
    if not esc:
        return '<span class="muted">—</span>'
    clase = next((c for k, c in ESC_CLASE.items() if k in esc.lower()), "esc-nor")
    return f'<span class="chip {clase}">{e(esc)}</span>'


WMS_SENAMHI = "https://idesep.senamhi.gob.pe/geoserver/wms"
CAPAS_MAPA_SENAMHI = [   # (capa WMS, nombre visible, encendida al abrir)
    ("g_aviso:view_aviso", "Avisos meteorológicos SENAMHI", True),
    ("g_acti_quebrada:view_av_activ_qdra", "Quebradas en posible activación (SENAMHI)", True),
    ("g_prono_pp_24h:view_aviso24h", "Aviso de lluvia 24 h (SENAMHI)", False),
]


FILTROS_CSS = """
.pulso-pais{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:16px 18px 12px;margin-bottom:14px}
.pp-cab{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.pp-cab h2{margin:0;font-size:22px} .pp-cab p{margin:0;font-size:12.5px;color:var(--mut)}
.pp-barra{display:flex;gap:14px;margin-top:14px;align-items:flex-end}
.pp-grupo{min-width:84px;display:flex;flex-direction:column;gap:6px}
.pp-segs{display:flex;gap:3px;align-items:flex-end;height:60px;border-bottom:2px solid var(--grafito)}
.pp-seg{flex:1 1 0;min-width:5px;max-width:26px;border-radius:3px 3px 0 0;background:var(--verde);height:22px;transform-origin:bottom;animation:crece .7s cubic-bezier(.2,.8,.2,1) both}
.pp-seg.n2{background:var(--amar);height:36px} .pp-seg.n3{background:var(--rojo);height:48px} .pp-seg.n4{background:var(--negro);height:58px}
.pp-seg:hover{filter:brightness(.88)}
.pp-zona{font-size:12px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis} .pp-zona em{display:block;font-style:normal;font-weight:400;color:var(--mut);font-size:11.5px}
@keyframes crece{from{transform:scaleY(0)}to{transform:scaleY(1)}}
.pp-grupo:nth-child(2) .pp-seg{animation-delay:.08s} .pp-grupo:nth-child(3) .pp-seg{animation-delay:.16s} .pp-grupo:nth-child(4) .pp-seg{animation-delay:.24s} .pp-grupo:nth-child(5) .pp-seg{animation-delay:.32s} .pp-grupo:nth-child(6) .pp-seg{animation-delay:.4s}
@media (prefers-reduced-motion:reduce){.pp-seg{animation:none}}
.lnk{background:none;border:0;padding:0;font:700 13px Lato,sans-serif;color:var(--fala-osc);cursor:pointer;text-decoration:underline;text-underline-offset:3px}
h2 .lnk.der{float:right;font-size:12.5px;margin-top:4px}
.filtros{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:12px 14px;margin:4px 0 14px;display:grid;gap:10px;position:sticky;top:118px;z-index:4;box-shadow:0 2px 8px rgba(43,47,44,.06)}
.f-fila{display:flex;gap:10px 14px;flex-wrap:wrap;align-items:center;justify-content:space-between}
.f-niveles{display:flex;gap:6px;flex-wrap:wrap}
.fchip{display:inline-flex;align-items:center;gap:6px;border:1.5px solid var(--bd);background:#fff;color:var(--tx);border-radius:6px;padding:6px 11px;cursor:pointer;font:700 13px Lato,sans-serif}
.fchip b{font-weight:400;color:var(--mut)} .fchip .b{width:10px;height:10px;border-radius:2px;display:inline-block}
.b.n1{background:var(--verde)} .b.n2{background:var(--amar)} .b.n3{background:var(--rojo)} .b.n4{background:var(--negro)}
.fchip[aria-pressed="true"]{background:var(--grafito);border-color:var(--grafito);color:#fff} .fchip[aria-pressed="true"] b{color:#cfd5ce}
.fchip.alerta[aria-pressed="true"]{background:var(--fala);border-color:var(--fala);color:#1d2a00} .fchip.alerta[aria-pressed="true"] b{color:#3c4d00}
.buscar{display:flex;align-items:center;gap:6px;border:1.5px solid var(--bd);border-radius:6px;padding:0 10px;color:var(--mut);background:#fff;flex:0 1 300px}
.buscar input{border:0;outline:0;font:14px Lato,sans-serif;padding:7px 0;width:100%;background:none;color:var(--tx)}
.sel{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--mut)}
.sel select{font:700 13px Lato,sans-serif;color:var(--tx);border:1.5px solid var(--bd);border-radius:6px;padding:5px 8px;background:#fff}
.check{display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer}
.check input{accent-color:var(--fala-osc);width:16px;height:16px}
.f-estado{display:flex;gap:16px;align-items:center;font-size:13px;border-top:1px solid var(--bd);padding-top:8px}
#f-cuenta{font-weight:700;margin-right:auto}
.leyenda-sen{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px 18px;background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:12px 14px;margin-top:10px;font-size:12.5px}
.leyenda-sen h3{grid-column:1/-1;margin:0;font-size:14px;font-weight:900}
.leyenda-sen div{display:flex;gap:9px;align-items:flex-start} .leyenda-sen i{flex:0 0 26px;height:16px;border-radius:3px;margin-top:2px;border:1px solid rgba(0,0,0,.12)}
.leyenda-sen p{grid-column:1/-1;margin:2px 0 0;color:var(--mut)}
.info-sen{width:300px;height:190px;border:0;display:block;background:#fff}
@media (max-width:900px){.filtros{position:static}}
@media (max-width:560px){.pp-barra{flex-wrap:wrap;gap:14px 12px} .pp-grupo{flex:1 1 40%!important} .pp-segs{height:50px}
.buscar{flex-basis:100%} .f-estado{flex-wrap:wrap} .f-fila:nth-child(2){display:grid;grid-template-columns:1fr 1fr;gap:8px}
.f-fila:nth-child(2) .sel{flex-direction:column;align-items:stretch;gap:2px} .sel select{width:100%} .f-fila:nth-child(2) .check{grid-column:1/-1}
.vistas{overflow-x:auto} .vista{white-space:nowrap;padding:10px 10px}}
"""

FILTROS_JS = r"""
const F={niveles:new Set(),alerta:false,zona:'',riesgo:'',tipo:'',cambios:false,q:'',orden:'nivel'};
const normz=t=>String(t||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase();
const $=id=>document.getElementById(id);
function filtroActivo(){return F.niveles.size||F.alerta||F.zona||F.riesgo||F.tipo||F.cambios||F.q;}
function pasa(d,sinZona){
  if(F.alerta&&d.nv<2)return false;
  if(F.niveles.size&&!F.niveles.has(d.nv))return false;
  if(!sinZona&&F.zona&&d.z!==F.zona)return false;
  if(F.riesgo&&d.r!==F.riesgo)return false;
  if(F.tipo&&d.tipo!==F.tipo)return false;
  if(F.cambios&&!d.cb)return false;
  if(F.q&&!d.txt.includes(F.q))return false;
  return true;}
function datoCard(c){const x=c.dataset;return{nv:+x.nivel,z:x.zona,r:x.riesgo,tipo:x.tipo,cb:x.cambio,txt:normz(x.txt+' '+x.nombre),
  n:x.nombre,or:+x.ordenRiesgo,t:x.tmax===''?-99:parseFloat(x.tmax)};}
function cmp(a,b){const A=datoCard(a),B=datoCard(b);
  if(F.orden==='riesgo')return A.or-B.or||B.nv-A.nv||A.n.localeCompare(B.n);
  if(F.orden==='nombre')return A.n.localeCompare(B.n);
  if(F.orden==='tmax')return B.t-A.t||A.n.localeCompare(B.n);
  return B.nv-A.nv||A.or-B.or||A.n.localeCompare(B.n);}
function aEnlace(){const q=new URLSearchParams();
  if(F.niveles.size)q.set('nivel',[...F.niveles].sort().join('.'));if(F.alerta)q.set('alerta','1');
  if(F.zona)q.set('zona',F.zona);if(F.riesgo)q.set('riesgo',F.riesgo);if(F.tipo)q.set('tipo',F.tipo);
  if(F.cambios)q.set('cambios','1');if(F.q)q.set('q',F.q);if(F.orden!=='nivel')q.set('orden',F.orden);
  const t=q.toString();return '#zonas'+(t?'?'+t:'');}
function deEnlace(h){const i=h.indexOf('?');const q=new URLSearchParams(i<0?'':h.slice(i+1));
  F.niveles=new Set((q.get('nivel')||'').split('.').filter(Boolean).map(Number));F.alerta=q.get('alerta')==='1';
  F.zona=q.get('zona')||'';F.riesgo=q.get('riesgo')||'';F.tipo=q.get('tipo')||'';F.cambios=q.get('cambios')==='1';
  F.q=normz(q.get('q')||'');F.orden=q.get('orden')||'nivel';}
function pintarControles(){
  document.querySelectorAll('.fchip[data-nivel]').forEach(b=>b.setAttribute('aria-pressed',F.niveles.has(+b.dataset.nivel)));
  document.querySelector('.fchip[data-alerta]').setAttribute('aria-pressed',F.alerta);
  $('f-zona').value=F.zona;$('f-riesgo').value=F.riesgo;$('f-tipo').value=F.tipo;$('f-orden').value=F.orden;$('f-cambios').checked=F.cambios;
  if(normz($('f-buscar').value)!==F.q)$('f-buscar').value=F.q;}
function aplicar(guardar){
  const cards=[...document.querySelectorAll('#zonas article.card')];let vis=0;
  cards.forEach(c=>{const ok=pasa(datoCard(c));c.hidden=!ok;if(ok)vis++;});
  document.querySelectorAll('#zonas section.zona').forEach(sec=>{const g=sec.querySelector('.grid');
    [...g.children].sort(cmp).forEach(x=>g.appendChild(x));sec.hidden=![...g.children].some(c=>!c.hidden);});
  $('f-cuenta').textContent=`Mostrando ${vis} de ${cards.length} instalaciones`;
  $('f-limpiar').hidden=!filtroActivo();$('f-vacio').hidden=vis>0;
  pintarControles();
  if(guardar&&document.getElementById('zonas').classList.contains('activo'))history.replaceState(null,'',aEnlace());
  if(typeof filtrarMapa==='function')filtrarMapa();}
function limpiar(){F.niveles.clear();F.alerta=false;F.zona=F.riesgo=F.tipo=F.q='';F.cambios=false;F.orden='nivel';aplicar(true);}
document.querySelectorAll('.fchip[data-nivel]').forEach(b=>b.addEventListener('click',()=>{const n=+b.dataset.nivel;
  F.niveles.has(n)?F.niveles.delete(n):F.niveles.add(n);F.alerta=false;aplicar(true);}));
document.querySelector('.fchip[data-alerta]').addEventListener('click',()=>{F.alerta=!F.alerta;if(F.alerta)F.niveles.clear();aplicar(true);});
[['f-zona','zona'],['f-riesgo','riesgo'],['f-tipo','tipo'],['f-orden','orden']].forEach(([id,k])=>$(id).addEventListener('change',ev=>{F[k]=ev.target.value||(k==='orden'?'nivel':'');aplicar(true);}));
$('f-cambios').addEventListener('change',ev=>{F.cambios=ev.target.checked;aplicar(true);});
let tBus;$('f-buscar').addEventListener('input',ev=>{clearTimeout(tBus);tBus=setTimeout(()=>{F.q=normz(ev.target.value.trim());aplicar(true);},150);});
$('f-limpiar').addEventListener('click',limpiar);
document.querySelectorAll('[data-limpiar]').forEach(b=>b.addEventListener('click',limpiar));
$('f-copiar').addEventListener('click',async()=>{const u=location.origin+location.pathname+aEnlace();
  try{await navigator.clipboard.writeText(u);$('f-copiar').textContent='Enlace copiado';}catch(e){prompt('Copia este enlace:',u);}
  setTimeout(()=>{$('f-copiar').textContent='Copiar enlace de esta vista';},2500);});
function irA(destino){
  if(destino.startsWith('#zonas')){deEnlace(destino);panel('zonas');aplicar(false);history.replaceState(null,'',aEnlace());window.scrollTo({top:0});return true;}
  const id=destino.replace('#','');if(['resumen','mapa','guia'].includes(id)){panel(id);history.replaceState(null,'','#'+id);return true;}
  return false;}
document.querySelectorAll('.vista').forEach(v=>v.addEventListener('click',()=>{
  history.replaceState(null,'',v.dataset.panel==='zonas'?aEnlace():'#'+v.dataset.panel);
  if(v.dataset.panel==='zonas')aplicar(false);}));
document.addEventListener('click',ev=>{const a=ev.target.closest('a[href^="#zonas"]');if(!a)return;ev.preventDefault();irA(a.getAttribute('href'));});
document.querySelectorAll('[data-ir]').forEach(el=>el.addEventListener('click',ev=>{ev.preventDefault();
  const c=document.getElementById(el.dataset.ir);if(!c)return;
  if(c.hidden)limpiar();panel('zonas');history.replaceState(null,'',aEnlace());
  c.scrollIntoView({behavior:'smooth',block:'start'});c.classList.remove('resalta');void c.offsetWidth;c.classList.add('resalta');}));
if(location.hash&&location.hash.length>1)irA(decodeURIComponent(location.hash));
window.addEventListener('hashchange',()=>{if(location.hash.length>1)irA(decodeURIComponent(location.hash));});
aplicar(false);
"""


MODERNO_CSS = """
:root{--bg:#ffffff;--bd:#e7eae6;--tx:#1f2421;--mut:#646b66;--fala-osc:#3f5c00;--fala-suave:#f5f9ea}
body{font-size:15px}
.franja{font-size:11.5px} .franja .in{padding:4px 16px}
.barra{border-bottom:3px solid var(--fala);box-shadow:none}
.barra .in{padding:10px 16px;flex-wrap:nowrap;align-items:center;gap:20px}
.marca{flex:0 1 auto;min-width:0} .marca h1{font-size:22px;letter-spacing:-.01em} .marca p{margin:0}
.logo{width:40px;height:40px}
.vistas{margin:0 0 0 auto;width:auto;gap:2px;background:#f1f3f0;border-radius:10px;padding:4px;max-width:none}
.vista{border:0;border-radius:8px;padding:7px 14px;color:var(--mut);font-size:14px}
.vista.activo{background:#fff;color:var(--tx);box-shadow:0 1px 2px rgba(31,36,33,.12);border-bottom:0}
.act{flex:0 0 auto} .act .small{display:none} .act .btn{padding:7px 14px}
.wrap{padding:30px 16px 48px}
h2{font-size:20px}
.res-cab{display:flex;justify-content:space-between;align-items:flex-end;gap:16px 32px;flex-wrap:wrap;padding-bottom:22px;border-bottom:1px solid var(--bd)}
.titular{font-size:clamp(30px,4.4vw,46px);font-weight:900;line-height:1.05;margin:0;letter-spacing:-.015em;max-width:none;text-wrap:balance}
.col-zona.larga{grid-column:span 2} .col-zona.larga ul{columns:2;column-gap:36px} .col-zona.larga li{break-inside:avoid}
.contexto{display:flex;gap:16px 32px;margin:0;flex-wrap:wrap}
.contexto dt{font-size:12.5px;color:var(--mut)} .contexto dd{margin:2px 0 0;font-weight:700;font-size:15px}
.contexto dd.rojo{color:var(--rojo)} .contexto dd.amarillo{color:#9a6b00}
.puntos{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:30px 36px;margin-top:26px}
.col-zona h3{font-size:15px;font-weight:900;margin:0 0 6px;display:flex;justify-content:space-between;align-items:baseline;gap:8px;border-bottom:2px solid var(--tx);padding-bottom:6px}
.col-zona h3 span{font-weight:400;color:var(--mut);font-size:12.5px}
.col-zona ul{list-style:none;margin:0;padding:0}
.col-zona a{display:grid;grid-template-columns:12px 1fr;column-gap:10px;align-items:center;padding:5px 6px;margin:0 -6px;border-radius:6px;text-decoration:none;color:var(--tx);font-weight:400;font-size:14.5px}
.col-zona a:hover,.col-zona a:focus-visible{background:#f1f4ef}
.col-zona a.alerta .nom{font-weight:900}
.col-zona .por{grid-column:2;font-size:12.5px;color:var(--mut);line-height:1.3}
.pt{width:11px;height:11px;border-radius:50%;display:inline-block;background:var(--verde);flex:0 0 auto}
.pt.n2{background:var(--amar);box-shadow:0 0 0 3px rgba(255,192,0,.28)} .pt.n3{background:var(--rojo);box-shadow:0 0 0 3px rgba(192,0,0,.22)} .pt.n4{background:#000;box-shadow:0 0 0 3px rgba(0,0,0,.18)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.leyenda-puntos{display:flex;gap:8px 20px;flex-wrap:wrap;font-size:12.5px;margin:30px 0 0;padding-top:14px;border-top:1px solid var(--bd)}
.leyenda-puntos span{display:inline-flex;align-items:center;gap:7px}
.novedades{margin-top:22px;border:1px solid var(--bd);border-radius:10px}
.novedades summary{padding:14px 16px;font-weight:900;font-size:15px;cursor:pointer;color:var(--tx)}
.novedades summary .muted{font-weight:400;font-size:13px;margin-left:8px}
.novedades[open] summary{border-bottom:1px solid var(--bd)}
.nov-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:22px 30px;padding:16px 16px 20px}
.nov-grid h3{font-size:14px;margin:0 0 6px} .nov-grid p{font-size:13.5px;margin:0 0 6px}
.nota-fuentes{font-size:12.5px;color:var(--mut);margin:0 0 8px}
.card,.card.sem-rojo,.card.sem-amarillo,.card.sem-negro{border:1px solid var(--bd);box-shadow:none;border-radius:10px;padding:16px;scroll-margin-top:96px}
.interp{background:#f6f8f3}
.filtros{box-shadow:none;border-radius:10px;position:static}
.fchip,.tab,.sel select,.buscar{border-width:1px}
.caja,.nivel-card,.glosario,.leyenda-sen,#mapa-div,#mapa-info,.pulso-pais{border-radius:10px}
.ico-omm{background:var(--fala-osc);color:#fff;border-radius:10px;font:700 11px/18px Lato,sans-serif;text-align:center;border:1.5px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.3)}
@media (max-width:980px){.barra .in{flex-wrap:wrap} .vistas{order:3;width:100%;margin:2px 0 0;overflow-x:auto} .act{margin-left:auto}}
@media (max-width:560px){.col-zona.larga{grid-column:auto} .wrap{padding-top:20px} .contexto{gap:10px 20px} .puntos{grid-template-columns:1fr;gap:22px} .vista{padding:7px 10px}}
"""

PRON_CSS = """
.pron-res{margin:28px 0 8px;padding-top:18px;border-top:1px solid var(--bd)}
.pron-res h2{margin:0 0 12px}
.pron-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px 28px}
.pron-grid h3{font-size:14px;margin:0 0 6px} .pron-grid ul{margin:0;padding-left:18px;font-size:14px;line-height:1.7}
.pron-grid p{margin:0 0 4px;font-size:14px}
.garua-res{border-left:4px solid var(--azul);padding-left:12px}
.guia-pron{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;background:var(--card);
  border:1px solid var(--bd);border-radius:10px;padding:16px 18px;font-size:13.5px}
.guia-pron h3{margin:0 0 6px;font-size:15px} .guia-pron p{margin:0 0 8px}
.tabla.mini{width:auto;font-size:12.5px} .tabla.mini td,.tabla.mini th{padding:5px 12px}
"""

MAPA_CSS = """
.mapa-top{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:4px 0 10px}
.leyenda{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--mut)} .leyenda i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:4px;vertical-align:-1px;border:1.5px solid #fff;box-shadow:0 0 0 1px #ccc}
.mapa-grid{display:grid;grid-template-columns:1fr 390px;gap:12px}
#mapa-div{height:72vh;min-height:430px;border-radius:8px;border:1px solid var(--bd);background:#e9ecef;z-index:1}
#mapa-info{max-height:72vh;min-height:430px;overflow:auto;background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:14px}
#mapa-info .card{border-left:0;border-right:0;border-bottom:0;border-radius:0;padding:12px 0 0;margin-top:10px}
.alrededor{list-style:none;padding:0;margin:6px 0 0;display:grid;gap:6px;font-size:12.5px}
.alrededor li{border-left:3px solid var(--fala);padding:2px 0 2px 9px} .alrededor li.ind{border-left-color:var(--rojo)} .alrededor li.aer{border-left-color:var(--azul)}
.mapa-ayuda h3{margin:0 0 6px;font-size:16px;font-weight:900} .mapa-ayuda ol{padding-left:18px;font-size:13px;margin:6px 0}
.sel-cab{display:flex;justify-content:space-between;align-items:flex-start;gap:8px} .sel-cab h3{margin:0;font-size:16px;font-weight:900}
.cerrar{border:0;background:var(--bd);border-radius:50%;width:26px;height:26px;cursor:pointer;font-weight:900}
.nota-wms{font-size:12px;margin:8px 0 0;padding:7px 10px;border-radius:8px;background:var(--fala-suave)} .nota-wms.mal{background:#fff7e6;border:1px solid var(--ambar)}
.ico-indeci{background:var(--rojo);color:#fff;border-radius:50%;font:900 12px/17px Lato,sans-serif;text-align:center;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4)}
.ico-aer{background:var(--azul);color:#fff;border-radius:10px;font:700 11px/18px Lato,sans-serif;text-align:center;border:1.5px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.3)}
.pulso{animation:pulso 1.8s ease-in-out infinite} @keyframes pulso{0%,100%{stroke-width:2}50%{stroke-width:9;stroke-opacity:.35}}
@media (prefers-reduced-motion:reduce){.pulso{animation:none}}
.leaflet-popup-content{font:13px/1.45 Lato,sans-serif} .leaflet-container a{color:var(--fala-osc)}
@media (max-width:900px){.mapa-grid{grid-template-columns:1fr} #mapa-div{height:60vh;min-height:360px} #mapa-info{max-height:none;min-height:0}}
"""

MAPA_JS = r"""
const M=JSON.parse(document.getElementById('datos-mapa').textContent);
const COL={1:'#70ad47',2:'#ffc000',3:'#c00000',4:'#111111'}, NOM={1:'Verde',2:'Amarilla',3:'Roja',4:'Negra'};
let mapa=null, capaSel=null, gTie=null, wmsOk=0, wmsMal=0, capasWms=[];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function km(a,b,c,d){const r=Math.PI/180,x=Math.sin((c-a)*r/2)**2+Math.cos(a*r)*Math.cos(c*r)*Math.sin((d-b)*r/2)**2;return 12742*Math.asin(Math.sqrt(x));}
const ayuda=document.getElementById('mapa-info').innerHTML;
function notaWms(){const n=document.getElementById('nota-wms');if(!n)return;
  if(wmsOk){n.className='nota-wms';n.textContent='✔ Capas oficiales de SENAMHI cargadas en vivo desde SENAMHI.';}
  else if(wmsMal>3){n.className='nota-wms mal';n.textContent='Las capas de SENAMHI no cargaron: SENAMHI solo las entrega a conexiones desde Perú. Los puntos, niveles e INDECI sí están actualizados.';}}
function iniciarMapa(){
  if(mapa){mapa.invalidateSize();return;}
  if(!window.L){document.getElementById('mapa-div').innerHTML='<p style="padding:16px">No se pudo cargar el mapa. Revisa la conexión y recarga la página.</p>';return;}
  mapa=L.map('mapa-div',{preferCanvas:false}).setView([-9.5,-75.5],5);
  const ESRI='https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
  L.tileLayer(ESRI+'World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',{maxZoom:16,
    attribution:'Mapa base © Esri, HERE, Garmin, © OpenStreetMap'}).addTo(mapa);
  mapa.createPane('nombres');mapa.getPane('nombres').style.zIndex=350;mapa.getPane('nombres').style.pointerEvents='none';
  L.tileLayer(ESRI+'World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}',{maxZoom:16,pane:'nombres'}).addTo(mapa);
  const over={};
  M.capas.forEach(c=>{const l=L.tileLayer.wms(M.wms,{layers:c.id,format:'image/png',transparent:true,opacity:.55,attribution:'Capas: SENAMHI'});
    l.on('tileload',()=>{wmsOk++;notaWms();});l.on('tileerror',()=>{wmsMal++;notaWms();});
    if(c.on)l.addTo(mapa);over[c.n]=l;capasWms.push({id:c.id,n:c.n,l:l});});
  const gInd=L.layerGroup(M.indeci.map(r=>L.marker([r.lat,r.lon],{icon:L.divIcon({className:'ico-indeci',html:'!',iconSize:[21,21]}),
    title:r.tipo+' · '+r.dist}).bindPopup(`<b>${esc(r.tipo)} · INDECI</b><br>${esc(r.ev)} en ${esc(r.dist)} (${esc(r.dep)})<br>
    <span class="muted">${esc(r.f)} · hace ${Math.round(r.h)} h</span><br><a href="${esc(r.link)}" target="_blank" rel="noopener">Ver reporte</a>`)));
  gInd.addTo(mapa);over[`Emergencias INDECI por lluvias (${M.indeci.length})`]=gInd;
  const gAer=L.layerGroup(M.aeropuertos.map(a=>L.marker([a.lat,a.lon],{icon:L.divIcon({className:a.ll?'ico-aer lluvia':'ico-aer',html:(a.ll?'<svg width="8" height="10" viewBox="0 0 8 10" style="margin-right:3px;vertical-align:-1px"><path d="M4 0C4 0 0 4.6 0 6.4A4 4 0 0 0 8 6.4C8 4.6 4 0 4 0z" fill="#fff"/></svg>':'')+Math.round(a.t)+'°',iconSize:[a.ll?44:34,21],iconAnchor:[-9,10]}),
    title:a.n}).bindPopup(`<b>Aeropuerto ${esc(a.n)}</b> · CORPAC<br>Ahora (${esc(a.h)}): <b>${a.t.toFixed(0)} °C</b><br>
    Últimas 24 h: máx ${a.mx==null?'—':a.mx.toFixed(0)+'°'} · mín ${a.mn==null?'—':a.mn.toFixed(0)+'°'}${a.ll?'<br>Observado: <b>'+esc(a.ll)+'</b>':''}`)));
  gAer.addTo(mapa);over['Temperatura en aeropuertos (CORPAC)']=gAer;
  const gOmm=L.layerGroup((M.estaciones||[]).map(a=>L.marker([a.lat,a.lon],{icon:L.divIcon({className:'ico-omm',html:Math.round(a.t)+'°',iconSize:[34,21],iconAnchor:[-9,10]}),
    title:a.n}).bindPopup(`<b>Estación SENAMHI ${esc(a.n)}</b><br><span class="muted">Red OMM · ${a.alt} m s. n. m.</span><br>Último parte (${esc(a.h)}): <b>${a.t.toFixed(1)} °C</b><br>
    Últimas 24 h: máx ${a.mx==null?'—':a.mx.toFixed(1)+'°'} · mín ${a.mn==null?'—':a.mn.toFixed(1)+'°'}${a.pp!=null?'<br>Lluvia 24 h: <b>'+a.pp.toFixed(1)+' mm</b>':''}${a.ll?'<br>Observado: <b>'+esc(a.ll)+'</b>':''}`)));
  gOmm.addTo(mapa);over[`Temperatura en estaciones SENAMHI (red OMM, ${(M.estaciones||[]).length})`]=gOmm;
  mapa.createPane('tiendas');mapa.getPane('tiendas').style.zIndex=640;
  gTie=L.featureGroup();
  M.puntos.slice().sort((a,b)=>a.nv-b.nv).forEach(p=>{
    const m=L.circleMarker([p.lat,p.lon],{radius:p.nv>1?10:7,color:'#fff',weight:2,fillColor:COL[p.nv],fillOpacity:.95,pane:'tiendas',bubblingMouseEvents:false,className:p.nv>1?'pulso':''});
    m.bindTooltip(`<b>${esc(p.n)}</b><br>Nivel ${p.nv} · ${NOM[p.nv]}`,{direction:'top',offset:[0,-6]});
    m.on('click',()=>seleccionar(p.cod));p._m=m;gTie.addLayer(m);});
  gTie.addTo(mapa);over['Instalaciones Saga Falabella']=gTie;
  L.control.layers(null,over,{collapsed:window.innerWidth<900}).addTo(mapa);
  L.control.scale({imperial:false}).addTo(mapa);
  mapa.fitBounds(gTie.getBounds(),{padding:[24,24]});
  filtrarMapa();
}
function filtrarMapa(){
  if(!gTie||typeof pasa!=='function')return;let n=0;
  M.puntos.forEach(p=>{const ok=pasa({nv:p.nv,z:p.z,r:p.r,tipo:p.tipo,cb:p.cb,txt:normz(p.n+' '+p.c+' '+p.d+' '+p.cod)},true);
    if(ok){n++;if(!gTie.hasLayer(p._m))gTie.addLayer(p._m);}else if(gTie.hasLayer(p._m))gTie.removeLayer(p._m);});
  const nf=document.getElementById('nota-filtro');
  if(nf){nf.hidden=!filtroActivo();nf.innerHTML=`Filtro de “Por zona” aplicado: se muestran ${n} de ${M.puntos.length} instalaciones. <button class="lnk" data-limpiar-mapa>Mostrar todas</button>`;
    const bl=nf.querySelector('[data-limpiar-mapa]');if(bl)bl.onclick=()=>limpiar();}
}
function seleccionar(cod){
  const p=M.puntos.find(x=>x.cod===cod);if(!p)return;
  if(!mapa){panel('mapa');iniciarMapa();}
  if(capaSel)mapa.removeLayer(capaSel);
  capaSel=L.layerGroup([
    L.circle([p.lat,p.lon],{radius:M.radios.indeci*1000,color:'#5c7a00',weight:1.5,fill:false,dashArray:'6 6',interactive:false}),
    L.circle([p.lat,p.lon],{radius:M.radios.quebrada*1000,color:'#5c7a00',weight:2,fillColor:'#aad500',fillOpacity:.10,interactive:false})]).addTo(mapa);
  mapa.flyTo([p.lat,p.lon],12,{duration:.8});
  const ind=M.indeci.map(r=>({...r,d:km(p.lat,p.lon,r.lat,r.lon)})).filter(r=>r.d<=30).sort((a,b)=>a.d-b.d);
  const aer=M.aeropuertos.map(a=>({...a,d:km(p.lat,p.lon,a.lat,a.lon)})).sort((a,b)=>a.d-b.d)[0];
  const vec=M.puntos.filter(x=>x.cod!==p.cod).map(x=>({...x,d:km(p.lat,p.lon,x.lat,x.lon)})).filter(x=>x.d<=5).sort((a,b)=>a.d-b.d);
  let li=ind.map(r=>`<li class="ind"><b>${esc(r.tipo)}</b> · ${esc(r.ev)} en ${esc(r.dist)} · ${r.d.toFixed(0)} km · ${esc(r.f)} · <a href="${esc(r.link)}" target="_blank" rel="noopener">reporte</a></li>`).join('');
  if(!ind.length)li+='<li>Sin emergencias INDECI por lluvias a 30 km o menos (últimas 48 h).</li>';
  if(aer&&aer.d<=60)li+=`<li class="aer">Aeropuerto ${esc(aer.n)} a ${aer.d.toFixed(0)} km: <b>${aer.t.toFixed(0)} °C</b> a las ${esc(aer.h)}${aer.ll?' · '+esc(aer.ll):''}</li>`;
  const om=(M.estaciones||[]).map(a=>({...a,d:km(p.lat,p.lon,a.lat,a.lon)})).sort((a,b)=>a.d-b.d)[0];
  if(om&&om.d<=25)li+=`<li class="aer">Estación SENAMHI ${esc(om.n)} a ${om.d.toFixed(0)} km: <b>${om.t.toFixed(1)} °C</b> (${esc(om.h)})${om.ll?' · '+esc(om.ll):''}</li>`;
  if(vec.length)li+=`<li>Otras instalaciones a 5 km o menos: ${vec.map(x=>esc(x.n)+' ('+x.d.toFixed(1)+' km)').join(', ')}</li>`;
  const info=document.getElementById('mapa-info');
  info.innerHTML=`<div class="sel-cab"><div><h3>${esc(p.n)}</h3><span class="muted small">${esc(p.c)} · ${esc(p.d)} · Zona ${esc(p.z)}</span></div>
    <button class="cerrar" title="Cerrar" aria-label="Cerrar">×</button></div>
    <p style="margin:8px 0"><span class="pill n${p.nv}">Nivel ${p.nv} · ${NOM[p.nv]}</span></p>
    <p style="margin:0 0 6px;font-size:13px">${esc(p.mot)}</p>
    <small class="muted">Alrededor · círculo verde ${M.radios.quebrada} km (quebradas), punteado ${M.radios.indeci} km (INDECI)</small>
    <ul class="alrededor">${li}</ul>`;
  const c=document.getElementById('t-'+cod);
  if(c){const k=c.cloneNode(true);k.removeAttribute('id');k.querySelectorAll('[data-mapa]').forEach(x=>x.remove());
    k.querySelectorAll('header').forEach(x=>x.remove());k.querySelector('details')?.setAttribute('open','');info.appendChild(k);}
  info.querySelector('.cerrar').onclick=()=>{info.innerHTML=ayuda;if(capaSel){mapa.removeLayer(capaSel);capaSel=null;}notaWms();};
  info.scrollTop=0;if(window.innerWidth<900)info.scrollIntoView({behavior:'smooth'});
}
document.querySelectorAll('.zona-mapa').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.zona-mapa').forEach(x=>x.classList.toggle('activo',x===b));
  const z=b.dataset.zona,pts=M.puntos.filter(p=>z==='todas'||p.z===z);
  if(mapa&&pts.length)mapa.fitBounds(L.latLngBounds(pts.map(p=>[p.lat,p.lon])),{padding:[30,30],maxZoom:12});}));
document.addEventListener('click',ev=>{const a=ev.target.closest('[data-mapa]');if(!a)return;ev.preventDefault();
  panel('mapa');window.scrollTo({top:0});setTimeout(()=>{iniciarMapa();seleccionar(a.dataset.mapa);},80);});
"""


def datos_mapa(filas, datos) -> dict:
    """Puntos para la pestana Mapa: instalaciones, reportes INDECI ubicados y aeropuertos CORPAC."""
    puntos = [{"cod": str(f["cod_p"]), "n": f["tienda"], "z": f["zona"], "c": f["ciudad"],
               "d": str(f["distrito"]).title(), "lat": float(f["lat"]), "lon": float(f["lon"]),
               "nv": int(f["nivel_plan"]), "mot": f["motivo_nivel"], "r": f["riesgo_plan"], "tipo": f["tipo"],
               "t": f["tmax"], "tn": f["tmin"], "cb": f.get("cambio") or ""} for f in filas]
    ubi = datos.get("ubigeo", {})
    indeci = []
    for rep in datos.get("indeci", []):
        for d in rep["distritos"]:
            for _prov, lat, lon in ubi.get((norm(rep["departamento"]), norm(d)), [])[:1]:
                if lat is not None:
                    indeci.append({"lat": lat, "lon": lon, "tipo": rep["tipo"], "ev": rep["evento"],
                                   "dist": d.title(), "dep": rep["departamento"].title(), "f": rep["fecha"],
                                   "h": round(rep["horas"], 1), "link": rep["link"]})
    aerop = []
    for icao, a in (datos.get("corpac") or {}).items():
        if a.get("lat") is None or not a.get("obs"):
            continue
        u = a["ultima"]
        rec = [o for o in a["obs"] if o["lluvia"]
               and (datetime.now().astimezone() - o["ts"]).total_seconds() <= HORAS_LLUVIA_AHORA * 3600]
        aerop.append({"icao": icao, "n": a["nombre"], "lat": a["lat"], "lon": a["lon"], "t": u["temp"],
                      "h": u["ts"].strftime("%H:%M"), "mx": a["tmax24"], "mn": a["tmin24"],
                      "ll": f'{rec[-1]["tipo"]} a las {rec[-1]["ts"]:%H:%M}' if rec else ""})
    estac = []
    for wmo, a in (datos.get("omm") or {}).items():
        u = a.get("ultima")
        if not u:
            continue
        estac.append({"id": wmo, "n": a["nombre"], "lat": a["lat"], "lon": a["lon"], "t": u["temp"], "alt": a["alt"],
                      "h": u["ts"].strftime("%d/%m %H:%M"), "mx": a["tmax24"], "mn": a["tmin24"], "pp": a["prec24"],
                      "ll": a["lluvia_6h"][-1]["tipo"] if a["lluvia_6h"] else ""})
    return {"puntos": puntos, "indeci": indeci, "aeropuertos": aerop, "estaciones": estac,
            "capas": [{"id": c, "n": n, "on": on} for c, n, on in CAPAS_MAPA_SENAMHI], "wms": WMS_SENAMHI,
            "radios": {"quebrada": DIST_QUEBRADA_KM, "indeci": DIST_INDECI_KM}}


def generar_html(filas, enfen, noaa, errores, escenarios=(), extra=None):
    zonas = sorted({(f["orden_zona"], f["zona"], f["frecuencia_reporte"]) for f in filas})
    cuenta = {n: sum(f["nivel_plan"] == n for f in filas) for n in (1, 2, 3, 4)}
    fase, fase_obj = fase_actual()

    # NOAA
    if noaa is not None and not noaa.empty:
        u = noaa.iloc[-1]
        noaa_txt = f'{u["nino12_anom"]:+.1f} °C'
        noaa_sub = f'Semana {u["semana"]:%d/%m/%Y} · TSM {u["nino12_sst"]:.1f} °C'
        noaa_spark = sparkline(list(noaa["nino12_anom"]))
    else:
        noaa_txt, noaa_sub, noaa_spark = "—", "Sin datos", ""

    estado = enfen.get("estado") or "Ver comunicado"
    fen_txt = next((v for k, v in ESTADO_ENFEN_TXT.items() if k in estado.lower()), "")
    estado_cls = "rojo" if "niño costero" in estado.lower() and "alerta" in estado.lower() else "amarillo" if "vigilancia" in estado.lower() else "neutro"

    # Filtros de la vista "Por zona"
    n_alerta = sum(f["nivel_plan"] >= 2 for f in filas)
    chips_nivel = "".join(
        f'<button class="fchip" data-nivel="{n}" aria-pressed="false"><i class="b n{n}"></i>'
        f'{e(NIVELES_PLAN[n]["nombre"].replace("Alerta ", ""))} <b>{cuenta_n}</b></button>'
        for n in (1, 2, 3, 4) for cuenta_n in [sum(f["nivel_plan"] == n for f in filas)])
    op_zona = "".join(
        f'<option value="{e(z)}">{e(z)} ({sum(f["zona"] == z for f in filas)}'
        f'{", " + str(a) + " en alerta" if a else ""})</option>'
        for _, z, _fr in zonas for a in [sum(f["zona"] == z and f["nivel_plan"] >= 2 for f in filas)])
    op_riesgo = "".join(f'<option value="{e(r)}">{e(r)}</option>'
                        for r in sorted({f["riesgo_plan"] for f in filas}, key=lambda r: ORDEN_RIESGO.get(r, 9)))
    op_tipo = "".join(f'<option value="{e(t)}">{e(t)}</option>' for t in sorted({f["tipo"] for f in filas}))
    n_cambios = sum(bool(f.get("cambio")) for f in filas)
    filtros_html = f"""
  <div class="filtros" id="filtros">
    <div class="f-fila">
      <div class="f-niveles" role="group" aria-label="Filtrar por nivel">{chips_nivel}
        <button class="fchip alerta" data-alerta aria-pressed="false">Solo con alerta <b>{n_alerta}</b></button></div>
      <label class="buscar"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2"/><path d="M20 20l-3.5-3.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
        <input type="search" id="f-buscar" placeholder="Buscar tienda, ciudad o código" aria-label="Buscar tienda, ciudad o código"></label>
    </div>
    <div class="f-fila">
      <label class="sel">Zona <select id="f-zona"><option value="">Todas</option>{op_zona}</select></label>
      <label class="sel">Riesgo <select id="f-riesgo"><option value="">Todos</option>{op_riesgo}</select></label>
      <label class="sel">Tipo <select id="f-tipo"><option value="">Todos</option>{op_tipo}</select></label>
      <label class="sel">Ordenar por <select id="f-orden"><option value="nivel">Nivel de alerta</option><option value="riesgo">Riesgo de la matriz</option>
        <option value="nombre">Nombre</option><option value="tmax">Temperatura máxima</option></select></label>
      <label class="check"><input type="checkbox" id="f-cambios"> Solo cambios de nivel ({n_cambios})</label>
    </div>
    <div class="f-estado"><span id="f-cuenta" aria-live="polite"></span>
      <button class="lnk" id="f-limpiar" hidden>Limpiar filtros</button><button class="lnk" id="f-copiar">Copiar enlace de esta vista</button></div>
  </div>
  <p class="vacio" id="f-vacio" hidden>Ninguna instalación cumple estos filtros. <button class="lnk" data-limpiar>Limpiar filtros</button></p>"""

    # Barra de estado nacional: una barra por instalacion, agrupadas por zona
    grupos = []
    for _, z, _fr in zonas:
        fz = sorted([f for f in filas if f["zona"] == z], key=lambda f: (-f["nivel_plan"], f["tienda"]))
        segs = "".join(
            f'<a class="pp-seg n{f["nivel_plan"]}" href="#t-{e(f["cod_p"])}" data-ir="t-{e(f["cod_p"])}" '
            f'title="{e(f["tienda"])}: Nivel {f["nivel_plan"]}" aria-label="{e(f["tienda"])}, Nivel {f["nivel_plan"]}"></a>'
            for f in fz)
        al = sum(f["nivel_plan"] >= 2 for f in fz)
        grupos.append(f'<div class="pp-grupo" style="flex:{len(fz)} 1 0"><div class="pp-segs">{segs}</div>'
                      f'<span class="pp-zona">{e(z)}<em>{"sin alertas" if not al else str(al) + " en alerta"}</em></span></div>')
    titulo_pais = (f"{n_alerta} de {len(filas)} instalaciones en alerta" if n_alerta
                   else f"Las {len(filas)} instalaciones en Nivel 1")
    pulso_pais = f"""
  <div class="pulso-pais">
    <div class="pp-cab"><h2>{titulo_pais}</h2>
      <p>Cada barra es una instalación; mientras más alta, mayor el nivel. Presiona una para ver su ficha.</p></div>
    <div class="pp-barra">{"".join(grupos)}</div>
  </div>"""

    # Tarjetas de tienda por zona
    bloques = []
    for _, z, frec in zonas:
        tarjetas = []
        for f in sorted([f for f in filas if f["zona"] == z],
                        key=lambda f: (-f["nivel_plan"], ORDEN_RIESGO.get(f["riesgo_plan"], 9), f["tienda"])):
            delta = f.get("delta_tmax")
            delta_html = "" if delta is None else f'<span class="delta {"sube" if delta > 0 else "baja" if delta < 0 else ""}">{delta:+.1f}° vs ayer</span>'
            avisos = "".join(
                f'<li class="nv{a.get("plan", 1) + 1}"><b>{e(a["origen"])}</b>'
                f'{" · Nivel SENAMHI " + str(a["nivel"]) if a.get("fuente") == "SENAMHI" else ""}'
                f'{" · " + e(a["fecha"]) if a["fecha"] else ""}<br>'
                + (f'<a href="{e(a["link"])}" target="_blank" rel="noopener">{e(a["titulo"])}</a>' if a.get("link")
                   else f'<span>{e(a["titulo"])}</span>') + '</li>'
                for a in sorted(f["avisos"], key=lambda a: -a.get("plan", 1)))
            avisos = f'<ul class="avisos">{avisos}</ul>' if avisos else '<p class="sin-aviso">Sin avisos vigentes</p>'
            it = f["interp"]
            acciones = "".join(f"<li>{e(a)}</li>" for a in it["acciones"])
            if f.get("proy_tmax"):
                proy = ('<div class="fila"><span>Estimación propia T. máx (3 días)</span><span>'
                        + " · ".join(f"{v:.0f}°" for v in f["proy_tmax"]) + "</span></div>")
            else:
                proy = (f'<div class="fila"><span>Estimación propia (historial)</span><span class="muted">'
                        f'{f["dias_hist"]}/{MIN_DIAS_PROYECCION} días acumulados</span></div>')
            pmx = "—" if f.get("prom_tmax") is None else f"{f['prom_tmax']:.1f}°"
            pmn = "—" if f.get("prom_tmin") is None else f"{f['prom_tmin']:.1f}°"
            est_txt = f["estacion"] or "sin estación ni aeropuerto cercano con dato"
            c = f.get("corpac")
            filas_det = [f'<p class="muted est">Fuente de temperatura: {e(est_txt)}</p>']
            if any(v is not None for v in f.get("serie_tmax", [])):
                filas_det.append(f'<div class="fila"><span>{"Temperatura horaria últimas 24 h" if c else "T. máx últimos registros"}</span>{sparkline(f["serie_tmax"])}</div>')
            if any(v is not None for v in f.get("serie_tmin", [])):
                filas_det.append(f'<div class="fila"><span>T. mín últimos registros</span>{sparkline(f["serie_tmin"])}</div>')
            if f.get("prom_tmax") is not None:
                filas_det.append(f'<div class="fila"><span>Promedio reciente · T. máx / T. mín</span><span>{pmx} / {pmn}</span></div>')
            if any(v is not None for v in f.get("tmin10", [])):
                filas_det.append(f'<div class="fila"><span>Pronóstico SENAMHI T. mín 10 días</span>{sparkline(f["tmin10"])}</div>')
            if f["pm_tmax_esc"] or f["pm_tmin_esc"] or f["pm_pp_esc"]:
                filas_det.append(f'<div class="fila"><span>Próximo mes · T. máx / T. mín / Lluvia</span><span class="chips">'
                                 f'{chip_escenario(f["pm_tmax_esc"])}{chip_escenario(f["pm_tmin_esc"])}{chip_escenario(f["pm_pp_esc"])}</span></div>')
            if f["verano_esc"]:
                filas_det.append(f'<div class="fila"><span>Lluvia verano {e(str(f["verano_meta"])).replace("_", " – ").title()}</span>{chip_escenario(f["verano_esc"])}</div>')
            if not (f["verano_esc"] or f["pm_tmax_esc"] or any(v is not None for v in f.get("tmin10", []))):
                filas_det.append('<div class="fila"><span>Pronósticos SENAMHI (10 días, mensual, verano)</span>'
                                 '<span class="muted">no disponibles desde el servidor</span></div>')
            filas_det.append(proy)
            filas_det.append(avisos)
            detalle = "\n          ".join(filas_det)
            if c:
                metrica3 = (f'<div><small>Ahora ({c["hora"]})</small><strong>{c["ahora"]:.0f}°</strong>'
                            + (f'<span class="delta sube">{e(c["lluvia_tipo"])}</span>' if c["lluvia"] else "") + "</div>")
                etq_max, etq_min, etq_serie = "Máx 24 h", "Mín 24 h", "Temperatura horaria últimas 24 h"
            else:
                prec_txt = "—" if f["prec"] is None else f"{f['prec']:.1f}<em>mm</em>"
                metrica3 = f"<div><small>Lluvia</small><strong>{prec_txt}</strong></div>"
                etq_max, etq_min, etq_serie = "T. máx", "T. mín", "T. máx últimos 15 registros"
            la = f.get("lluvia_ahora")
            if la and not (c and c.get("lluvia")):
                metrica3 = metrica3[:-6] + f'<span class="delta sube">{e(la["tipo"])} ({e(la["hora"])})</span></div>'
            tarjetas.append(f"""
      <article class="card sem-{f['semaforo']}" id="t-{e(f['cod_p'])}" data-zona="{e(z)}" data-nivel="{f['nivel_plan']}"
        data-riesgo="{e(f['riesgo_plan'])}" data-orden-riesgo="{ORDEN_RIESGO.get(f['riesgo_plan'], 9)}" data-tipo="{e(f['tipo'])}"
        data-cambio="{e(f.get('cambio') or '')}" data-nombre="{e(f['tienda'])}" data-tmax="{'' if f['tmax'] is None else f['tmax']}"
        data-txt="{e(norm(' '.join(str(x) for x in (f['tienda'], f['ciudad'], f['distrito'], f['cod_p'], f['cod_c']))).lower())}">
        <header><div><h3>{e(f['tienda'])}</h3><p class="muted">{e(f['ciudad'])}, {e(f['distrito'].title())} <span class="codigo">{e(f['cod_p'])}</span></p>
          <p class="tags"><span class="riesgo r-{e(str(f['riesgo_plan']).lower().replace(' ', '-'))}">Riesgo {e(f['riesgo_plan'])}</span>{'' if f['tipo'] == 'Tienda' else f'<span class="tipo">{e(f["tipo"])}</span>'}</p></div>
          <div class="badges"><span class="sem-badge">Nivel {f['nivel_plan']} · {e(NIVELES_PLAN[f['nivel_plan']]['nombre'].replace('Alerta ', ''))}</span>
          {'' if not f.get('cambio') else f'<span class="cambio {f["cambio"]}">{"▲ Subió" if f["cambio"] == "sube" else "▼ Bajó"} desde Nivel {f["nivel_anterior"]}</span>'}</div></header>
        <div class="metricas">
          <div><small>{etq_max}</small><strong>{'—' if f['tmax'] is None else f"{f['tmax']:.1f}°"}</strong>{delta_html}</div>
          <div><small>{etq_min}</small><strong>{'—' if f['tmin'] is None else f"{f['tmin']:.1f}°"}</strong></div>
          {metrica3}
        </div>
        <div class="interp"><dl>{"".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in it['lineas'])}</dl>
          <p class="qh">Qué hacer · acciones del Plan FEN para Nivel {f['nivel_plan']}</p><ul>{acciones}</ul></div>
        <div class="botones">
          <a class="btn" href="{e(f['url_noticias'])}" target="_blank" rel="noopener">Noticias oficiales de {e(f['ciudad'])}</a>
          <a class="btn sec" href="{e(LINKS['SENAMHI avisos'])}" target="_blank" rel="noopener">Avisos SENAMHI</a>
          <a class="btn sec" href="{e(LINKS['INDECI emergencias'])}" target="_blank" rel="noopener">Emergencias INDECI</a>
          <a class="btn sec" href="#mapa" data-mapa="{e(f['cod_p'])}">Ver en mapa</a>
        </div>
        <details><summary>Ver detalle técnico</summary>
          {detalle}
        </details>
      </article>""")
        bloques.append(f'<section class="zona" data-zona="{e(z)}"><h2>Zona {e(z)} <span class="frec">Reporte {e(frec).lower()}</span></h2><div class="grid">{"".join(tarjetas)}</div></section>')

    err_html = ""
    if errores:
        otros_err = [k for k in errores if k != "SENAMHI (IDESEP)"]
        partes = []
        if "SENAMHI (IDESEP)" in errores:
            partes.append("El servidor que arma el monitor está fuera de Perú y SENAMHI no le responde. Por eso la temperatura "
                          "viene de estaciones SENAMHI publicadas en la red de la OMM y de aeropuertos CORPAC, los avisos de los "
                          "boletines de INDECI y el riesgo por distrito de CENEPRED. Los mapas de SENAMHI sí se ven en vivo desde Perú.")
        if otros_err:
            partes.append("<b>No respondieron en esta actualización:</b> " + ", ".join(e(k) for k in otros_err) + ".")
        err_html = '<p class="nota-fuentes">' + " ".join(partes) + "</p>"

    esc_html = " · ".join(
        f'<a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x["ambito"].title())} ({e(x["inicio"][8:10])}/{e(x["inicio"][5:7])} – '
        f'{e(x["fin"][8:10])}/{e(x["fin"][5:7])})</a>' for x in escenarios) or "ninguno vigente."
    if URL_ACTUALIZAR:
        boton_actualizar = (f'<button class="btn" id="btn-act" data-url="{e(URL_ACTUALIZAR)}">Actualizar datos</button>'
                            f'<span class="muted small" id="msg-act">Automático: {HORAS_AUTOMATICAS}</span>')
    else:
        enlace = (f' · <a href="https://github.com/{e(REPO_GITHUB)}/actions/workflows/monitor_fen.yml" target="_blank" '
                  f'rel="noopener">forzar actualización</a>' if REPO_GITHUB else "")
        boton_actualizar = (f'<button class="btn" onclick="location.reload()">Ver última versión</button>'
                            f'<span class="muted small">Se actualiza solo {HORAS_AUTOMATICAS}{enlace}</span>')
    fuentes = " · ".join(f'<a href="{e(u)}" target="_blank" rel="noopener">{e(n)}</a>' for n, u in LINKS.items())
    extra = extra or {}
    mapa = extra.get("mapa") or {"puntos": [], "indeci": [], "aeropuertos": [], "capas": [], "wms": WMS_SENAMHI,
                                 "radios": {"quebrada": DIST_QUEBRADA_KM, "indeci": DIST_INDECI_KM}}
    mapa_json = json.dumps(mapa, ensure_ascii=False, default=str).replace("</", "<\\/")
    chips_mapa = ('<button class="tab zona-mapa activo" data-zona="todas">Todo el país</button>'
                  + "".join(f'<button class="tab zona-mapa" data-zona="{e(z)}">{e(z)}</button>' for _, z, _fr in zonas))

    # ---------------- RESUMEN ----------------
    alertas = sorted([f for f in filas if f["nivel_plan"] >= 2],
                     key=lambda f: (-f["nivel_plan"], ORDEN_RIESGO.get(f["riesgo_plan"], 9), f["tienda"]))
    if alertas:
        filas_tabla = "".join(
            f'<tr class="fila-alerta" data-ir="t-{e(f["cod_p"])}">'
            f'<td><b>{e(f["tienda"])}</b><br><span class="muted small">{e(f["ciudad"])} · Riesgo {e(f["riesgo_plan"])}</span></td>'
            f'<td>{e(f["zona"])}</td>'
            f'<td><span class="pill n{f["nivel_plan"]}">Nivel {f["nivel_plan"]} · {e(NIVELES_PLAN[f["nivel_plan"]]["nombre"].replace("Alerta ", ""))}</span>'
            + ('' if not f.get("cambio") else f'<br><span class="cambio {f["cambio"]}">{"▲ subió" if f["cambio"] == "sube" else "▼ bajó"} desde Nivel {f["nivel_anterior"]}</span>')
            + f'</td><td>{e(f["motivo_nivel"])}</td>'
            f'<td>{e(NIVELES_PLAN[f["nivel_plan"]]["acciones"][0])}</td>'
            f'<td class="ver">Ver ›</td></tr>' for f in alertas)
        tabla_alertas = (f'<div class="tabla-wrap"><table class="tabla"><thead><tr><th>Instalación</th><th>Zona</th><th>Nivel</th>'
                         f'<th>Por qué</th><th>Primera acción del plan</th><th></th></tr></thead><tbody>{filas_tabla}</tbody></table></div>')
    else:
        tabla_alertas = '<div class="vacio">✅ Todas las instalaciones están en <b>Nivel 1 · Verde</b> (monitoreo preventivo).</div>'

    cambios = [f for f in filas if f.get("cambio")]
    cambios_html = ("".join(
        f'<li><span class="cambio {f["cambio"]}">{"▲" if f["cambio"] == "sube" else "▼"}</span> <b>{e(f["tienda"])}</b>: '
        f'Nivel {f["nivel_anterior"]} → Nivel {f["nivel_plan"]} · {e(f["motivo_nivel"])}</li>' for f in cambios)
        or '<li class="muted">Sin cambios de nivel desde la actualización anterior.</li>')

    av_html = "".join(
        f'<li><b>Aviso N° {e(a["numero"])}</b>{" · " + e(a["evento"]) if a["evento"] else " · <span class=muted>tipo en el boletín</span>"}'
        f'{"<br><span class=muted>Vigencia: " + e(a["vigencia"]) + "</span>" if a["vigencia"] else ""}'
        f'{"<br><span class=muted>" + e(", ".join(d.title() for d in a["departamentos"])) + "</span>" if a["departamentos"] else ""}'
        f' · <a href="{e(a["link"])}" target="_blank" rel="noopener">boletín</a></li>'
        for a in extra.get("avisos_indeci", [])[:4]) or '<li class="muted">Sin avisos recientes.</li>'
    esc_li = "".join(
        f'<li><b>{e(x["ambito"].title())}</b><br><span class="muted">{e(x["inicio"][8:10])}/{e(x["inicio"][5:7])} – '
        f'{e(x["fin"][8:10])}/{e(x["fin"][5:7])}</span> · <a href="{e(x["url"])}" target="_blank" rel="noopener">tabla oficial</a></li>'
        for x in escenarios) or '<li class="muted">Ninguno vigente.</li>'
    cercanos = {}
    for f in filas:
        for rep in f.get("indeci", []):
            if rep.get("plan", 1) >= 2:
                cercanos.setdefault(rep["link"], (rep, []))[1].append(f["tienda"])
    ind_li = "".join(
        f'<li><b>{e(rep["tipo"])}</b> · {e(rep["evento"])} en {e(", ".join(d.title() for d in rep["distritos"]))} '
        f'({e(rep["departamento"].title())}) · {e(rep["fecha"])}<br><span class="muted">Cerca de: {e(", ".join(ts))}</span> · '
        f'<a href="{e(rep["link"])}" target="_blank" rel="noopener">reporte</a></li>'
        for rep, ts in cercanos.values()) or \
        f'<li class="muted">Ninguna cerca de una instalación (últimas {HORAS_INDECI} h; {len(extra.get("indeci", []))} reporte(s) por lluvias en el país).</li>'

    # ---------------- RESUMEN: listado de puntos ----------------
    def motivo_corto(f):
        m = norm(f["motivo_nivel"])
        if f.get("manual"): return "Registro manual"
        if "UMBRAL" in m: return "Lluvia sobre el umbral"
        if "QUEBRADA" in m: return "Quebrada cercana"
        if "INDECI" in m: return "Emergencia INDECI cercana"
        if "AVISO" in m: return "Aviso SENAMHI"
        if "INUNDACION" in m: return "Riesgo de inundación"
        if "MOVIMIENTOS EN MASA" in m: return "Riesgo de huaico o deslizamiento"
        return f["motivo_nivel"][:50]
    cols = []
    zonas_res = sorted(zonas, key=lambda x: (-max(f["nivel_plan"] for f in filas if f["zona"] == x[1]),
                                             -sum(f["nivel_plan"] >= 2 for f in filas if f["zona"] == x[1]), x[0]))
    for _, z, _fr in zonas_res:
        fz = sorted([f for f in filas if f["zona"] == z], key=lambda f: (-f["nivel_plan"], f["tienda"]))
        items = "".join(
            f'<li><a href="#t-{e(f["cod_p"])}" data-ir="t-{e(f["cod_p"])}" class="{"alerta" if f["nivel_plan"] >= 2 else ""}">'
            f'<i class="pt n{f["nivel_plan"]}" aria-hidden="true"></i><span class="nom">{e(f["tienda"].replace("FLB ", ""))}</span>'
            + (f'<span class="por">{"▲ " if f.get("cambio") == "sube" else ""}{e(motivo_corto(f))}</span>' if f["nivel_plan"] >= 2 else "")
            + f'<span class="sr">Nivel {f["nivel_plan"]}</span></a></li>' for f in fz)
        al = sum(f["nivel_plan"] >= 2 for f in fz)
        cols.append(f'<section class="col-zona{" larga" if len(fz) > 12 else ""}"><h3>{e(z)}<span>{len(fz)}{" · " + str(al) + " en alerta" if al else ""}</span></h3><ul>{items}</ul></section>')
    lista_puntos = "".join(cols)
    titular = (f"{n_alerta} de {len(filas)} instalaciones en alerta" if n_alerta
               else f"Las {len(filas)} instalaciones están en Nivel 1")
    n_novedades = (f"{len(extra.get('avisos_indeci', []))} avisos SENAMHI, {len(cercanos)} emergencias INDECI cercanas, "
                   f"{len(cambios)} cambios de nivel")

    # ---------------- GUIA ----------------
    disparadores = {
        1: ["Base de la temporada de lluvias: todas las instalaciones parten aquí."],
        2: ["Aviso SENAMHI amarillo o naranja de lluvias sobre la tienda.",
            f"Quebrada en posible activación a {DIST_QUEBRADA_KM} km o menos.",
            f"Emergencia o peligro inminente INDECI por lluvias a {DIST_INDECI_KM} km o menos (48 h).",
            "Distrito en riesgo Alto o Muy alto en un escenario CENEPRED vigente.",
            f"Lluvia sobre el umbral en una estación a {DIST_UMBRAL_ROJA_KM}–{DIST_UMBRAL_AMARILLA_KM} km."],
        3: ["Aviso SENAMHI rojo sobre la tienda.",
            f"Lluvia sobre el umbral de riesgo en una estación a {DIST_UMBRAL_ROJA_KM} km o menos.",
            f"Emergencia o peligro inminente INDECI por lluvias en el mismo distrito ({HORAS_INDECI_ROJA} h)."],
        4: ["Solo por registro manual del equipo (archivo alertas_manuales.csv) ante inundación o afectación directa."],
    }
    guia = "".join(
        f'<article class="nivel-card n{n}"><header><span class="pill n{n}">Nivel {n}</span><h3>{e(v["nombre"])}'
        f'{" · " + e(v["sub"]) if v["sub"] else ""}</h3></header><p class="que">{e(v["desc"])}</p>'
        f'<h4>Se activa en el monitor cuando…</h4><ul>{"".join(f"<li>{e(x)}</li>" for x in disparadores[n])}</ul>'
        f'<h4>Acciones del Plan FEN</h4><ul>{"".join(f"<li>{e(x)}</li>" for x in v["acciones"])}</ul></article>'
        for n, v in NIVELES_PLAN.items())
    glosario = [
        ("ENFEN", "Comisión multisectorial que declara el estado del Fenómeno El Niño en el Perú (vigilancia, alerta, etc.)."),
        ("SENAMHI", "Servicio meteorológico nacional: emite los avisos de lluvias y los pronósticos."),
        ("INDECI / COEN", "Defensa Civil: reporta emergencias y peligros inminentes por distrito y republica los avisos de SENAMHI."),
        ("CENEPRED", "Estima qué distritos están en riesgo (Muy alto, Alto, Medio) con base en cada aviso de SENAMHI."),
        ("Red OMM", "SENAMHI envía los partes de sus estaciones a la red mundial de la Organización Meteorológica Mundial; el monitor los lee desde ahí (OGIMET)."),
        ("CORPAC", "Reportes meteorológicos horarios de los aeropuertos: temperatura actual y lluvia observada."),
        ("Mar Niño 1+2 (NOAA)", "Temperatura del mar frente a la costa norte comparada con lo normal; sobre +1 °C favorece calor y lluvias."),
        ("Riesgo de la instalación", "Clasificación fija de la Matriz Nacional de Riesgo del plan (Crítico, Alto, Medio Alto, Medio, Bajo)."),
    ]
    glos = "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in glosario)

    # ---------------- LLUVIA O GARUA OBSERVADA AHORA ----------------
    obs_ahora = {}
    for f in filas:
        la = f.get("lluvia_ahora")
        if la:
            obs_ahora.setdefault(la["lugar"], la)
    if obs_ahora:
        partes_obs = []
        for lugar, la in sorted(obs_ahora.items(), key=lambda x: x[1]["hora"], reverse=True):
            ciudad, _, aerop = lugar.partition(" – ")
            donde = f"{ciudad} (aeropuerto {aerop})" if aerop else f"{ciudad} (aeropuerto)"
            partes_obs.append(f'<b>{e(la["tipo"].capitalize())}</b> en {e(donde)}, {e(la["hora"])}')
        obs_html = (f'<p class="obs-ahora"><span class="gota" aria-hidden="true"></span><span>Observado en las últimas '
                    f'{HORAS_LLUVIA_AHORA} horas: ' + " · ".join(partes_obs) + '.</span></p>')
    else:
        obs_html = ""

    # ---------------- PRONOSTICO (resumen y guia) ----------------
    pron = extra.get("pronostico") or {}
    fuertes, probables = [], []
    for f in filas:
        for d in (f.get("pron_lluvia") or [])[:2]:
            if d.get("nivel", 0) >= 3 or (d.get("nivel", 0) >= 2 and (d.get("p10") or 0) >= 50):
                fuertes.append((f, d)); break
        else:
            for d in (f.get("pron_lluvia") or [])[:2]:
                if d.get("nivel", 0) >= 2:
                    probables.append((f, d)); break
    def _item(f, d):
        return (f'<li><a href="#t-{e(f["cod_p"])}" data-ir="t-{e(f["cod_p"])}">{e(f["tienda"].replace("FLB ", ""))}</a>'
                f' <span class="muted">{e(d["dia"].lower())}, {d["p1"]:.0f} %</span></li>')
    if pron.get("lluvia"):
        bloques_pron = []
        if fuertes:
            bloques_pron.append('<div><h3>Lluvia fuerte probable</h3><ul>' + "".join(_item(f, d) for f, d in fuertes) + '</ul></div>')
        if probables:
            bloques_pron.append('<div><h3>Lluvia probable</h3><ul>' + "".join(_item(f, d) for f, d in probables) + '</ul></div>')
        if not bloques_pron:
            bloques_pron.append('<div><h3>Sin lluvias importantes previstas</h3><p class="muted">Ninguna instalación con lluvia probable hoy ni mañana.</p></div>')
    else:
        bloques_pron = ['<div><p class="muted">Pronóstico de lluvia no disponible en esta actualización.</p></div>']
    g, ga = pron.get("garua") or {}, pron.get("garua_ahora") or {}
    if g or ga.get("frase"):
        gt = (f'<p><b>{e(ga["frase"])}</b></p>' if ga.get("hay") else "") + f'<p>{e(g.get("frase", ""))}</p>'
        bloques_pron.append(f'<div class="garua-res"><h3>Garúa en Lima</h3>{gt}</div>')
    fuentes_pron = pron.get("fuentes") or {}
    pron_res = (f'<section class="pron-res"><h2>Lluvia en los próximos 2 días</h2><div class="pron-grid">{"".join(bloques_pron)}</div>'
                f'<p class="muted small">Pronóstico calculado el {e(pron.get("calculado", "—"))}; próxima actualización a las '
                f'{e(pron.get("proxima", "—"))}. Modelos ECMWF (corrida {e(fuentes_pron.get("ecmwf", "—"))}) y NOAA GFS '
                f'(corrida {e(fuentes_pron.get("gfs", "—"))}). Es una estimación para anticiparse: '
                f'<b>no cambia el nivel del plan</b>.</p></section>')
    ac = pron.get("aciertos_garua")
    if ac:
        filas_ac = ""
        for k, v in ac["por_rango"].items():
            pct = "—" if not v["mananas"] else f'{v["garuo"] / v["mananas"]:.0%}'
            filas_ac += f'<tr><td>{e(k)}</td><td>{v["mananas"]}</td><td>{v["garuo"]}</td><td>{pct}</td></tr>'
        aciertos_html = (f'<p>Últimas {ac["n"]} mañanas verificadas:</p><table class="tabla mini"><thead><tr><th>Pronóstico</th>'
                         f'<th>Mañanas</th><th>Garuó</th><th>%</th></tr></thead><tbody>{filas_ac}</tbody></table>')
    else:
        aciertos_html = ('<p class="muted">El registro de aciertos empieza con las primeras mañanas verificadas '
                         '(pronóstico calculado la víspera y comparado con lo observado en el aeropuerto).</p>')
    guia_pron = f"""
  <h2>Pronóstico de lluvia y garúa</h2>
  <div class="guia-pron">
    <div><h3>Lluvia (todas las instalaciones)</h3>
      <p>Usa el conjunto de 51 simulaciones del Centro Europeo (ECMWF): la probabilidad es la proporción de simulaciones
      que dan más de 1 mm en el día. Menos de 30 %: sin lluvia prevista. De 30 a 60 %: posible. Más de 60 %: probable.
      Se avisa "moderada a fuerte" o "fuerte" cuando hay 30 % o más de superar 10 o 20 mm. La hora del día sale de los modelos
      ECMWF y NOAA GFS; si NOAA no coincide, se dice expresamente.</p>
      <p>En Lima, Cañete, Ica y Tacna los modelos confunden la garúa con lluvia, por eso ahí solo se avisa lluvia si los dos
      modelos coinciden.</p>
      <p>Se actualiza cada 4 horas desde las 4 a. m. (4, 8, 12, 16, 20 y 0 h). Es informativo: el nivel del plan sigue dependiendo
      de los avisos oficiales.</p></div>
    <div><h3>Garúa en Lima</h3>
      <p>Los modelos globales casi no anticipan la garúa. Por eso se estima con lo que observa el aeropuerto Jorge Chávez la víspera
      (humedad, altura de las nubes, visibilidad y si ya garuó), calibrado con 366 mañanas de junio a setiembre de 2023 a 2025.</p>
      <p><b>Poco probable</b>: en mañanas así garuó 1 de cada 10. <b>Posible</b>: 3 de cada 10. <b>Probable</b>: 1 de cada 2.
      Se calcula desde las 5 p. m. para la mañana siguiente, de mayo a noviembre.</p>
      {aciertos_html}</div>
  </div>"""

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monitor FEN · Falabella Retail Perú</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
:root{{--fala:#aad500;--fala-osc:#4a6b00;--fala-suave:#f4f9e4;--grafito:#2b2f2c;--bg:#f2f4f1;--card:#fff;--tx:#2b2f2c;--mut:#5e6661;--bd:#dfe3dd;
--rojo:#c00000;--ambar:#e07b00;--amar:#ffc000;--verde:#70ad47;--azul:#1f5fa8;--negro:#111;}}
*{{box-sizing:border-box}} html{{scroll-behavior:smooth}}
body{{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 Lato,"Segoe UI",system-ui,sans-serif}}
a{{color:var(--fala-osc);font-weight:700}}
.muted{{color:var(--mut)}} .small{{font-size:11.5px}}
/* ---------- cabecera ---------- */
.franja{{background:var(--grafito);color:#e9ece8;font-size:12px}}
.franja .in{{max-width:1280px;margin:0 auto;padding:6px 16px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}
.franja span:first-child{{font-weight:900;letter-spacing:.01em}} .franja span:last-child{{color:#b9c0b8}}
.barra{{background:#fff;border-bottom:4px solid var(--fala);position:sticky;top:0;z-index:5;box-shadow:0 1px 6px rgba(0,0,0,.05)}}
.barra .in{{max-width:1280px;margin:0 auto;padding:12px 16px 0;display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
.marca{{display:flex;align-items:center;gap:12px;flex:1;min-width:260px}}
.logo{{width:46px;height:46px;border-radius:10px;object-fit:contain;animation:entrada .9s cubic-bezier(.34,1.56,.64,1) both, brillo 6s ease-in-out 1.2s infinite;transform-origin:50% 60%}}
.logo:hover{{animation:giro .8s ease}}
@keyframes entrada{{0%{{opacity:0;transform:translateY(-18px) scale(.6) rotate(-12deg)}}100%{{opacity:1;transform:none}}}}
@keyframes brillo{{0%,85%,100%{{filter:none;transform:none}}90%{{filter:drop-shadow(0 0 8px var(--fala));transform:scale(1.08) rotate(-4deg)}}95%{{transform:scale(1) rotate(3deg)}}}}
@keyframes giro{{from{{transform:rotateY(0)}}to{{transform:rotateY(360deg)}}}}
@media (prefers-reduced-motion:reduce){{.logo{{animation:none}}}}
.marca h1{{font-size:20px;font-weight:900;margin:0;line-height:1.2}} .marca h1 span{{color:var(--fala-osc)}}
.marca p{{margin:2px 0 0;font-size:12px;color:var(--mut)}}
.act{{display:flex;flex-direction:column;align-items:flex-end;gap:3px}}
.btn{{display:inline-block;font:700 13px Lato,sans-serif;text-decoration:none;padding:8px 16px;border-radius:6px;background:var(--fala);color:#1d2a00;border:0;cursor:pointer}}
.btn:hover{{filter:brightness(.95)}} .btn:focus-visible,.fchip:focus-visible,.tab:focus-visible,.lnk:focus-visible,select:focus-visible,input:focus-visible,.pp-seg:focus-visible{{outline:3px solid var(--fala-osc);outline-offset:2px}} .btn.sec{{background:#fff;color:var(--fala-osc);border:1.5px solid var(--fala)}}
.vistas{{max-width:1280px;margin:10px auto 0;padding:0 16px;display:flex;gap:4px;width:100%}}
.vista{{background:none;border:0;border-bottom:3px solid transparent;padding:10px 14px;font:700 14px Lato,sans-serif;color:var(--mut);cursor:pointer}}
.vista.activo{{color:var(--tx);border-bottom-color:var(--fala-osc)}}
.wrap{{max-width:1280px;margin:0 auto;padding:18px 16px 40px}}
section.panel{{display:none}} section.panel.activo{{display:block;animation:aparece .35s ease}}
@keyframes aparece{{from{{opacity:0;transform:translateY(6px)}}to{{opacity:1;transform:none}}}}
h2{{font-size:18px;font-weight:900;margin:26px 0 10px}} h2 small{{font-weight:400;color:var(--mut);font-size:12.5px;margin-left:6px}}
/* ---------- resumen ---------- */
.estado{{display:grid;grid-template-columns:2fr 1fr 1.4fr 1.4fr;gap:12px}}
.caja{{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:14px 16px}}
.caja small{{color:var(--mut);display:block;font-size:12px}} .caja strong{{display:block;font-size:20px;font-weight:900;margin:2px 0}}
.caja.fen{{border-left:6px solid var(--mut)}} .caja.fen.rojo{{border-left-color:var(--rojo)}} .caja.fen.amarillo{{border-left-color:var(--amar)}}
.explica{{font-size:12px;color:var(--mut);margin:6px 0 0}}
.niveles{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:6px}}
.nv{{border-radius:6px;padding:8px 4px;text-align:center;font-size:22px;font-weight:900}} .nv em{{display:block;font-size:10.5px;font-style:normal;font-weight:700}}
.nv.n1,.pill.n1{{background:var(--verde);color:#fff}} .nv.n2,.pill.n2{{background:var(--amar);color:#111}} .nv.n3,.pill.n3{{background:var(--rojo);color:#fff}} .nv.n4,.pill.n4{{background:var(--negro);color:#fff}}
.pill{{display:inline-block;font-size:11.5px;font-weight:700;padding:3px 9px;border-radius:4px;white-space:nowrap}}
.tabla-wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--bd);border-radius:8px}}
.tabla{{width:100%;border-collapse:collapse;font-size:13px}}
.tabla th{{text-align:left;font-size:11.5px;color:var(--mut);background:var(--fala-suave);padding:10px 12px;font-weight:700}}
.tabla td{{padding:10px 12px;border-top:1px solid var(--bd);vertical-align:top}}
.fila-alerta{{cursor:pointer;transition:background .15s}} .fila-alerta:hover{{background:var(--fala-suave)}}
.ver{{color:var(--fala-osc);font-weight:700;white-space:nowrap}}
.vacio{{background:var(--fala-suave);border:1px solid var(--fala);border-radius:8px;padding:16px;font-size:14px}}
.tres{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.lista{{list-style:none;padding:0;margin:6px 0 0;display:grid;gap:8px;font-size:12.5px}}
.lista li{{border-left:3px solid var(--fala);padding:2px 0 2px 10px}}
.cambio{{font-size:11px;font-weight:700}} .cambio.sube{{color:var(--rojo)}} .cambio.baja{{color:var(--verde)}}
.aviso-sistema{{background:#fff7e6;border:1px solid var(--ambar);border-radius:10px;padding:9px 12px;margin-top:12px;font-size:12.5px}}
/* ---------- por zona ---------- */
.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 6px}}
.tab{{border:1.5px solid var(--bd);background:#fff;color:var(--tx);border-radius:6px;padding:6px 13px;cursor:pointer;font:700 13px Lato,sans-serif}}
.tab span{{color:var(--mut);margin-left:4px;font-weight:400}} .tab.activo{{background:var(--fala);border-color:var(--fala);color:#1d2a00}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ambar);margin-left:6px;vertical-align:middle}}
.frec{{font-size:12px;font-weight:400;color:var(--mut);margin-left:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--bd);border-left:6px solid var(--verde);border-radius:8px;padding:14px 14px 14px 16px;scroll-margin-top:140px;box-shadow:0 1px 2px rgba(43,47,44,.06)}}
.card[hidden],section.zona[hidden]{{display:none}}
.card.sem-rojo{{border-left-color:var(--rojo)}} .card.sem-amarillo{{border-left-color:var(--amar)}} .card.sem-negro{{border-left-color:var(--negro)}}
.codigo{{font-size:10.5px;font-weight:700;color:var(--mut);border:1px solid var(--bd);border-radius:4px;padding:0 5px;margin-left:4px;white-space:nowrap}}
.card.resalta{{animation:resalta 1.6s ease}} @keyframes resalta{{0%,60%{{box-shadow:0 0 0 4px var(--fala)}}100%{{box-shadow:none}}}}
.card header{{display:flex;justify-content:space-between;gap:8px}} .card h3{{font-size:15px;margin:0;font-weight:900}} .card header p{{margin:2px 0 0;font-size:12px}}
.sem-badge{{font-size:11px;font-weight:700;white-space:nowrap;padding:3px 9px;border-radius:4px;height:fit-content}}
.sem-verde .sem-badge{{background:var(--verde);color:#fff}} .sem-rojo .sem-badge{{background:var(--rojo);color:#fff}} .sem-amarillo .sem-badge{{background:var(--amar);color:#111}} .sem-negro .sem-badge{{background:#111;color:#fff}}
.badges{{display:flex;flex-direction:column;align-items:flex-end;gap:4px}}
.tags{{margin:4px 0 0!important;display:flex;gap:4px;flex-wrap:wrap}}
.riesgo,.tipo{{font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:4px;background:var(--bd)}}
.r-crítico{{background:#c00000;color:#fff}} .r-alto{{background:#ed7d31;color:#fff}} .r-medio-alto{{background:#ffc000;color:#111}} .r-medio{{background:#ffe699;color:#111}} .r-bajo{{background:#a9d08e;color:#111}}
.tipo{{background:var(--azul);color:#fff}}
.metricas{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0 4px}}
.metricas small{{color:var(--mut);display:block;font-size:11px}} .metricas strong{{font-size:20px;font-weight:900}} .metricas em{{font-size:11px;font-style:normal;color:var(--mut);margin-left:2px}}
.delta{{display:block;font-size:11px;color:var(--mut)}} .delta.sube{{color:var(--rojo)}} .delta.baja{{color:var(--azul)}}
.interp{{margin:10px 0 8px;padding:10px 12px;border-radius:10px;background:var(--fala-suave);font-size:13px}}
.interp dl{{display:grid;grid-template-columns:max-content 1fr;gap:4px 10px;margin:0}} .interp dt{{font-weight:700;font-size:12px;color:var(--fala-osc)}} .interp dd{{margin:0}}
.interp .qh{{margin:8px 0 0;font-weight:700;font-size:12px}} .interp ul{{margin:4px 0 0;padding-left:18px;font-size:12px}}
.botones{{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 4px}} .botones .btn{{font-size:12px;padding:5px 11px}}
details{{margin-top:6px;border-top:1px solid var(--bd);padding-top:6px}} summary{{cursor:pointer;font-size:12px;color:var(--mut)}}
.est{{font-size:11px;margin:0 0 8px}}
.fila{{display:flex;justify-content:space-between;align-items:center;gap:8px;border-top:1px solid var(--bd);padding:6px 0;font-size:12px}} .fila>span:first-child{{color:var(--mut)}}
.spark{{vertical-align:middle}} .spark polyline{{fill:none;stroke:var(--fala-osc);stroke-width:1.8}} .spark circle{{fill:var(--fala-osc)}}
.spark-rango{{font-size:11px;color:var(--mut);margin-left:6px}}
.ll-chips{{display:flex;flex-direction:column;gap:3px;margin-bottom:6px}}
.ll{{display:inline-block;width:max-content;max-width:100%;font-size:12px;padding:2px 8px;border-radius:6px;border-left:4px solid}}
.ll-0{{background:#f1f3f4;border-color:#b0b7bc;color:#4a5358}} .ll-1{{background:#e8f5e9;border-color:#43a047;color:#1b5e20}}
.ll-2{{background:#fff8e1;border-color:#f9a825;color:#6d4c00}} .ll-3{{background:#fff0e0;border-color:#ef6c00;color:#7a3300}}
.ll-4{{background:#fdecea;border-color:#c62828;color:#7f1414}}
.ll-barra{{position:relative;height:10px;border-radius:5px;background:#e8f5e9;overflow:visible;margin:4px 0 2px}}
.ll-barra i{{position:absolute;top:0;bottom:0;display:block}} .ll-barra .z1{{left:0;background:#c8e6c9;border-radius:5px 0 0 5px}}
.ll-barra .z2{{background:#ffe082}} .ll-barra .z3{{background:#ffb74d}} .ll-barra .z4{{background:#ef9a9a;border-radius:0 5px 5px 0}}
.ll-barra .mk{{position:absolute;top:-4px;width:3px;height:18px;margin-left:-1px;background:#212121;border-radius:2px}}
.ll-ejes{{position:relative;height:14px;font-size:10px;color:var(--mut);margin-bottom:4px}} .ll-ejes span{{position:absolute;transform:translateX(-50%);white-space:nowrap}} .ll-ejes span:first-child{{transform:none}}
.ll-ejes span:last-child{{transform:translateX(-85%)}}
.chips{{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}} .chip{{font-size:11px;padding:2px 7px;border-radius:10px;background:var(--bd)}}
.esc-sup{{background:#fde8e8;color:var(--rojo)}} .esc-inf{{background:#e6eef9;color:var(--azul)}}
.avisos{{list-style:none;padding:0;margin:8px 0 0;display:grid;gap:6px}}
.avisos li{{font-size:12px;border-left:3px solid var(--amar);padding:4px 8px;background:#fafaf8;border-radius:0 6px 6px 0}}
.avisos li.nv4,.avisos li.nv5{{border-left-color:var(--rojo)}} .avisos li.nv2{{border-left-color:var(--verde)}} .avisos li span{{color:var(--mut)}}
.sin-aviso{{font-size:12px;color:var(--verde);margin:8px 0 0}}
/* ---------- guia ---------- */
.guia{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}}
.nivel-card{{background:var(--card);border:1px solid var(--bd);border-left:6px solid;border-radius:8px;padding:14px 16px}}
.nivel-card.n1{{border-left-color:var(--verde)}} .nivel-card.n2{{border-left-color:var(--amar)}} .nivel-card.n3{{border-left-color:var(--rojo)}} .nivel-card.n4{{border-left-color:var(--negro)}}
.nivel-card header{{display:flex;align-items:center;gap:8px}} .nivel-card h3{{margin:0;font-size:15px;font-weight:900}}
.nivel-card .que{{font-weight:700;margin:10px 0}} .nivel-card h4{{font-size:13px;color:var(--fala-osc);margin:12px 0 4px}}
.nivel-card ul{{margin:0;padding-left:18px;font-size:12.5px}}
.glosario{{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;font-size:13px}}
.glosario dt{{font-weight:900;color:var(--fala-osc)}} .glosario dd{{margin:0}}
{MAPA_CSS}
{FILTROS_CSS}
{MODERNO_CSS}
{PRON_CSS}
.obs-ahora{{margin:18px 0 0;padding:11px 14px;border-left:4px solid var(--azul);background:#eef4fb;border-radius:6px;font-size:14.5px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
.gota{{width:10px;height:13px;background:var(--azul);border-radius:50% 50% 50% 50% / 60% 60% 40% 40%;clip-path:polygon(50% 0,100% 62%,92% 85%,70% 100%,30% 100%,8% 85%,0 62%);flex:0 0 10px;align-self:center}}
.ico-aer.lluvia{{background:#0b6fd6;box-shadow:0 0 0 3px rgba(11,111,214,.35)}}
footer{{margin-top:36px;font-size:12px;color:var(--mut);border-top:1px solid var(--bd);padding-top:12px}}
@media (max-width:900px){{.estado,.tres{{grid-template-columns:1fr 1fr}}}}
@media (max-width:700px){{.barra{{position:static}} .tabla thead{{display:none}} .tabla,.tabla tbody,.tabla tr,.tabla td{{display:block;width:100%}}
.tabla tr{{border-top:1px solid var(--bd);padding:6px 0}} .tabla td{{border:0;padding:4px 14px}} .tabla td.ver{{text-align:right}}}}
@media (max-width:560px){{.estado,.tres{{grid-template-columns:1fr}} .act{{align-items:flex-start}} .marca h1{{font-size:17px}} .metricas strong{{font-size:17px}}}}
</style></head><body>
<div class="franja"><div class="in"><span>Falabella Retail Perú</span><span>Mantenimiento y Seguridad · Uso interno</span></div></div>
<div class="barra"><div class="in">
  <div class="marca"><img class="logo" src="logo.png" alt="" onerror="this.remove()">
    <div><h1>Monitor FEN</h1>
    <p>Actualizado el {HOY:%d/%m a las %H:%M}</p></div></div>
  <nav class="vistas" aria-label="Vistas"><button class="vista activo" data-panel="resumen">Resumen</button><button class="vista" data-panel="zonas">Por zona</button><button class="vista" data-panel="mapa">Mapa</button><button class="vista" data-panel="guia">Guía de alertas</button></nav>
  <div class="act">{boton_actualizar}</div>
</div>
</div>

<div class="wrap">
<section class="panel activo" id="resumen">
  <div class="res-cab">
    <h2 class="titular">{titular}</h2>
    <dl class="contexto">
      <div><dt>ENFEN</dt><dd class="{estado_cls}">{e(estado)}</dd></div>
      <div><dt>Mar costa norte (Niño 1+2)</dt><dd>{noaa_txt}{' sobre lo normal' if noaa_txt != '—' else ''}</dd></div>
      <div><dt>Calendario del plan</dt><dd>{e(fase)}</dd></div>
    </dl>
  </div>
  {obs_html}
  <div class="puntos">{lista_puntos}</div>
  <p class="leyenda-puntos"><span><i class="pt n1"></i>Nivel 1 Verde</span><span><i class="pt n2"></i>Nivel 2 Amarilla</span><span><i class="pt n3"></i>Nivel 3 Roja</span><span><i class="pt n4"></i>Nivel 4 Negra</span>
    <span class="muted">Presiona una instalación para ver su tarjeta.</span></p>
  {pron_res}
  <details class="novedades"><summary>Novedades oficiales <span class="muted">{n_novedades}</span></summary>
    <div class="nov-grid">
      <div><h3>Qué dice ENFEN</h3><p>{e(fen_txt)}</p><p class="muted small">{e(enfen.get('comunicado'))}, {e(enfen.get('fecha'))}{f' · <a href="{e(enfen.get("url_pdf"))}" target="_blank" rel="noopener">leer comunicado</a>' if enfen.get('url_pdf') else ''}</p></div>
      <div><h3>Cambios de nivel</h3><ul class="lista">{cambios_html}</ul></div>
      <div><h3>Avisos SENAMHI</h3><ul class="lista">{av_html}</ul></div>
      <div><h3>Emergencias INDECI cerca de una instalación</h3><ul class="lista">{ind_li}</ul></div>
      <div><h3>Escenarios de riesgo CENEPRED</h3><ul class="lista">{esc_li}</ul></div>
    </div>
  </details>
</section>

<section class="panel" id="zonas">
  {filtros_html}
  {''.join(bloques)}
</section>

<section class="panel" id="mapa">
  <div class="mapa-top"><nav class="tabs">{chips_mapa}</nav>
    <div class="leyenda"><span><i style="background:#70ad47"></i>Nivel 1</span><span><i style="background:#ffc000"></i>Nivel 2</span>
      <span><i style="background:#c00000"></i>Nivel 3</span><span><i style="background:#111"></i>Nivel 4</span>
      <span><i style="background:#c00000;border-radius:50%"></i>INDECI</span><span><i style="background:#1f5fa8;border-radius:4px"></i>Aeropuerto °C</span><span><i style="background:#3f5c00;border-radius:4px"></i>Estación SENAMHI °C</span></div></div>
  <div class="mapa-grid"><div id="mapa-div" role="region" aria-label="Mapa de instalaciones"></div>
    <aside id="mapa-info"><div class="mapa-ayuda"><h3>¿Cómo está la zona?</h3>
      <ol><li>Presiona un punto para ver el nivel de la instalación, por qué está así y qué hacer.</li>
      <li>Los círculos muestran el radio que revisa el monitor: {DIST_QUEBRADA_KM} km para quebradas y {DIST_INDECI_KM} km para emergencias INDECI.</li>
      <li>Con el botón de capas (arriba a la derecha) prende o apaga los avisos y quebradas de SENAMHI, las emergencias INDECI y la temperatura de aeropuertos.</li></ol>
      <p class="muted small">Puntos: {len(mapa['puntos'])} instalaciones · {len(mapa['indeci'])} ubicaciones con emergencia INDECI por lluvias (48 h) · {len(mapa['aeropuertos'])} aeropuertos con reporte.</p></div></aside></div>
  <p class="nota-wms" id="nota-wms">Las capas de SENAMHI se cargan en vivo desde SENAMHI y solo se ven desde conexiones en Perú.</p>
  <p class="nota-wms" id="nota-filtro" hidden></p>
  <div class="leyenda-sen"><h3>Qué significan las manchas de color</h3>
    <div><i style="background:#f3ef7a"></i><span><b>Avisos meteorológicos SENAMHI.</b> Amarillo: esté atento. Naranja: prepárese. Rojo: actúe, evento peligroso. Cada aviso dice el tipo de evento (lluvias, viento, temperatura).</span></div>
    <div><i style="background:repeating-linear-gradient(90deg,#f3ef7a 0 33%,#f5bf7f 33% 66%,#ee6a5f 66%)"></i><span><b>Aviso de lluvia 24 h</b> (apagado al abrir). Pronóstico de lluvias para hoy con probable activación de quebradas. Escala propia: amarillo es Nivel 1, el más bajo; naranja, Nivel 2; rojo, Nivel 3.</span></div>
    <div><i style="background:repeating-linear-gradient(45deg,#e8572f 0 4px,#f3c04a 4px 8px)"></i><span><b>Manchas pequeñas sobre ríos y quebradas.</b> Zonas con posible activación de quebradas (huaicos).</span></div>
    <p><b>Importante:</b> estas manchas son la referencia visual de SENAMHI y no cambian por sí solas el nivel de las tiendas. El nivel de cada instalación se calcula con los avisos SENAMHI publicados por INDECI, las emergencias INDECI y el riesgo CENEPRED (detalle en la Guía de alertas). Para leer el texto completo de un aviso entra a <a href="{e(LINKS['SENAMHI avisos'])}" target="_blank" rel="noopener">Avisos SENAMHI</a>.</p>
  </div>
</section>

<section class="panel" id="guia">
  <h2>¿Qué significa cada alerta?</h2>
  <div class="guia">{guia}</div>
  {guia_pron}
  <h2>Fuentes y términos</h2>
  <dl class="glosario">{glos}</dl>
</section>

<footer>{err_html}<p>Temperatura: estación SENAMHI más cercana (directa o vía red OMM, a {DIST_MAX_OMM_KM} km o menos) o aeropuerto CORPAC (a {DIST_MAX_AEROPUERTO_KM} km o menos), la que esté más cerca; si no hay ninguna, referencia regional NASA POWER. Un nivel alcanzado se mantiene {PERSISTENCIA_HORAS} h. Riesgo de cada instalación según la Matriz Nacional de Riesgo del plan.</p>
<p>Fuentes: {fuentes}</p></footer>
</div>
<script type="application/json" id="datos-mapa">{mapa_json}</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
const b=document.getElementById('btn-act');
if(b){{b.addEventListener('click',async()=>{{
  const m=document.getElementById('msg-act');b.disabled=true;m.textContent='Solicitando actualización…';
  try{{const r=await fetch(b.dataset.url,{{method:'POST'}});
    m.textContent=r.ok?'Actualizando: la página se recargará en ~4 minutos.':'No se pudo solicitar ('+r.status+').';
    if(r.ok)setTimeout(()=>location.reload(),240000);}}catch(err){{m.textContent='No se pudo solicitar la actualización.';}}
  finally{{setTimeout(()=>{{b.disabled=false}},60000);}}
}});}}
function panel(id){{document.querySelectorAll('.vista').forEach(x=>x.classList.toggle('activo',x.dataset.panel===id));
  document.querySelectorAll('section.panel').forEach(s=>s.classList.toggle('activo',s.id===id));
  if(id==='mapa')setTimeout(iniciarMapa,60);}}
document.querySelectorAll('.vista').forEach(v=>v.addEventListener('click',()=>{{panel(v.dataset.panel);window.scrollTo({{top:0}});}}));
{MAPA_JS}
{FILTROS_JS}
</script>
</body></html>"""


def generar_resumen(filas, enfen, noaa):
    lineas = [f"🌊 Monitor FEN · {HOY:%d/%m/%Y}",
              f"Estado ENFEN: {enfen.get('estado') or 's/d'} ({enfen.get('comunicado', '')} · {enfen.get('fecha', '')})"]
    fen_txt = next((v for k, v in ESTADO_ENFEN_TXT.items() if k in str(enfen.get("estado", "")).lower()), "")
    if fen_txt:
        lineas.append(fen_txt)
    if noaa is not None and not noaa.empty:
        lineas.append(f"Mar frente a la costa norte (NOAA Niño 1+2): {noaa.iloc[-1]['nino12_anom']:+.1f} °C sobre lo normal")
    fase, _ = fase_actual()
    if fase:
        lineas.append(f"Calendario Plan FEN: {fase}")
    cambios_nivel = [f for f in filas if f.get("cambio")]
    if cambios_nivel:
        lineas.append(f"\n🔔 Cambios desde la última actualización ({len(cambios_nivel)}):")
        for f in sorted(cambios_nivel, key=lambda f: -f["nivel_plan"]):
            flecha = "▲" if f["cambio"] == "sube" else "▼"
            lineas.append(f"{flecha} {f['tienda']}: Nivel {f['nivel_anterior']} → Nivel {f['nivel_plan']} "
                          f"{NIVELES_PLAN[f['nivel_plan']]['nombre']} – {f['motivo_nivel']}")
    alertas = [f for f in filas if f["nivel_plan"] >= 2]
    if alertas:
        lineas.append(f"\nInstalaciones sobre Nivel 1 ({len(alertas)}):")
        for f in sorted(alertas, key=lambda f: (-f["nivel_plan"], ORDEN_RIESGO.get(f["riesgo_plan"], 9))):
            nv = NIVELES_PLAN[f["nivel_plan"]]["nombre"]
            lineas.append(f"• {f['tienda']} (Zona {f['zona']}, riesgo {f['riesgo_plan']}) – Nivel {f['nivel_plan']} {nv}: {f['motivo_nivel']}")
        lineas.append(f"\nResto de instalaciones ({len(filas) - len(alertas)}): Nivel 1 Alerta Verde – monitoreo preventivo.")
    else:
        lineas.append(f"\nTodas las instalaciones ({len(filas)}) en Nivel 1 Alerta Verde – monitoreo preventivo.")
    cambios = [f for f in filas if f.get("delta_tmax") is not None and abs(f["delta_tmax"]) >= 3]
    if cambios:
        lineas.append("\nCambios fuertes de T. máx (≥3 °C vs ayer):")
        lineas += [f"• {f['tienda']}: {f['tmax']:.1f}° ({f['delta_tmax']:+.1f}°)" for f in cambios]
    return "\n".join(lineas)


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main():
    log("=== Inicio monitor FEN ===")
    tiendas = pd.read_csv(ARCH_TIENDAS, dtype={"cod_c": str, "cod_p": str}).fillna(
        {"cod_c": "", "tipo": "Tienda", "riesgo_plan": "Sin clasificar", "frecuencia_reporte": "", "distrito": ""})
    tiendas["lat_r"], tiendas["lon_r"] = tiendas["lat"].round(3), tiendas["lon"].round(3)

    errores = {}
    datos = {}

    enfen, err = seguro("ENFEN", fuente_enfen, {})
    if err: errores["ENFEN"] = err
    noaa, err = seguro("NOAA Niño 1+2", fuente_noaa, pd.DataFrame())
    if err: errores["NOAA"] = err

    datos["estaciones"] = {}
    for var in ("tmax", "tmin", "prec"):
        ns = range(1, DIAS_SERIE + 1) if var in ("tmax", "tmin") else [1]
        for n in ns:
            if senamhi_caido():
                datos["estaciones"][(var, n)] = pd.DataFrame()
                continue
            df, err = seguro(f"SENAMHI estaciones {var}_{n}", lambda v=var, k=n: fuente_estaciones(v, k), pd.DataFrame())
            datos["estaciones"][(var, n)] = df
            if err and n == 1 and not senamhi_caido(): errores[f"SENAMHI {var}"] = err

    capas = {"aviso_met": "g_aviso:view_aviso",
             "aviso_24h": "g_prono_pp_24h:view_aviso24h",
             "quebradas": "g_acti_quebrada:view_av_activ_qdra",
             "pm_tmax": "spc:prono_mensual_poligono_tmax1",
             "pm_tmin": "spc:prono_mensual_poligono_tmin1",
             "pm_pp": "spc:prono_mensual_poligono_pp1",
             "verano": "spc:prono_verano_poligono"}
    for clave, capa in capas.items():
        if senamhi_caido():
            datos[clave] = []
            continue
        datos[clave], err = seguro(f"SENAMHI {capa}", lambda c=capa: fuente_poligonos(c), [])
        if err and not senamhi_caido(): errores[f"SENAMHI {clave}"] = err

    datos["ubigeo"], err = seguro("Ubigeos INEI", leer_ubigeo, {})
    datos["indeci"], err = seguro("INDECI emergencias por lluvias", fuente_indeci, [])
    if err: errores["INDECI"] = err
    log(f"     INDECI: {len(datos['indeci'])} reportes por lluvias en las últimas {HORAS_INDECI} h")
    datos["sigrid"], err = seguro("CENEPRED/SIGRID escenarios", fuente_sigrid, {"escenarios": [], "mm": {}, "inund": {}})
    if err: errores["CENEPRED/SIGRID"] = err
    log(f"     SIGRID: {len(datos['sigrid']['escenarios'])} escenario(s) vigente(s)")
    datos["avisos_indeci"], err = seguro("Avisos SENAMHI vía INDECI (boletines)", fuente_avisos_indeci, [])
    log(f"     Avisos vía INDECI: {', '.join('N° ' + a['numero'] + ' ' + '/'.join(a['departamentos']) for a in datos['avisos_indeci']) or 'ninguno'}")
    if senamhi_caido():
        datos["umbrales"], datos["tmin10"] = pd.DataFrame(), {}
        errores["SENAMHI (IDESEP)"] = ("no accesible desde este servidor; se usan avisos vía INDECI, riesgo CENEPRED "
                                       "y temperatura NASA POWER")
        log("     SENAMHI no accesible: se omiten sus capas y se usan fuentes alternativas")
    else:
        datos["umbrales"], err = seguro("SENAMHI umbrales de lluvia", fuente_umbrales, pd.DataFrame())
        if err: errores["SENAMHI umbrales"] = err
        datos["tmin10"], err = seguro("SENAMHI pronóstico Tmin 10 días", lambda: fuente_tmin_10d(tiendas), {})
        if err: errores["SENAMHI Tmin 10d"] = err
    sin_temp = all(d is None or d.empty for d in [datos["estaciones"].get(("tmax", 1))])
    datos["nasa"], datos["corpac"] = {}, {}
    datos["omm"] = {}
    if sin_temp:
        datos["omm"], err = seguro("SENAMHI vía red OMM (OGIMET, partes SYNOP)", fuente_omm, {})
        if err: errores["SENAMHI vía OMM"] = err
        log(f"     SENAMHI vía OMM: {len(datos['omm'])} estaciones con parte")
        datos["corpac"], err = seguro("CORPAC aeropuertos (temperatura horaria)", fuente_corpac, {})
        if err: errores["CORPAC"] = err
        log(f"     CORPAC: {len(datos['corpac'])} aeropuertos con reporte")
        # NASA solo para las tiendas lejos de un aeropuerto con dato
        con_aerop = {c for c, a in datos["corpac"].items() if a.get("lat") is not None}
        lejos = tiendas[[min((haversine_km(t.lat, t.lon, datos["corpac"][c]["lat"], datos["corpac"][c]["lon"])
                              for c in con_aerop), default=9e9) > DIST_MAX_AEROPUERTO_KM for t in tiendas.itertuples()]]
        if not lejos.empty:
            datos["nasa"], err = seguro(f"NASA POWER (respaldo para {len(lejos)} instalaciones lejos de aeropuertos)",
                                        lambda: fuente_nasa_power(lejos), {})
            if err: errores["NASA POWER"] = err
    # Pronostico de lluvia (ECMWF + NOAA GFS) y garua en Lima: se recalcula una vez por franja de 4 h
    import pronostico
    spjc = (datos.get("corpac") or {}).get("SPJC", {}).get("obs", [])
    puntos = [(str(t.cod_p), float(t.lat), float(t.lon)) for t in tiendas.itertuples()]
    aridos = {str(t.cod_p) for t in tiendas.itertuples() if t.zona == "Lima Metropolitana" or norm(t.ciudad) in COSTA_ARIDA}
    datos["pronostico"], err = seguro("Pronóstico de lluvia y garúa (ECMWF, NOAA GFS)",
                                      lambda: pronostico.obtener(puntos, spjc, CARPETA, log, aridos=aridos), {})
    if err: errores["Pronóstico de lluvia"] = err
    if (datos["pronostico"] or {}).get("error_lluvia"):
        errores["Pronóstico de lluvia"] = datos["pronostico"]["error_lluvia"]
    datos["cache_estacional"] = leer_cache_estacional()

    datos["manuales"], err = seguro("Alertas manuales", leer_manuales, {})
    if err: errores["alertas_manuales.csv"] = err
    filas = procesar(tiendas, datos)
    if datos.get("verano"):                      # SENAMHI respondio: guardar pronostico estacional para otras corridas
        guardar_cache_estacional(filas)
    persistencia_y_cambios(filas)
    delta_vs_ayer(filas)
    guardar_historial(filas, enfen, noaa)
    proyeccion_historial(filas)
    for f in filas:
        f["interp"] = interpretar(f, enfen)

    ARCH_HTML.parent.mkdir(parents=True, exist_ok=True)
    ARCH_HTML.write_text(generar_html(filas, enfen, noaa, errores, datos["sigrid"]["escenarios"],
                                  {"avisos_indeci": datos.get("avisos_indeci", []), "indeci": datos.get("indeci", []),
                                   "mapa": datos_mapa(filas, datos), "pronostico": datos.get("pronostico") or {}}),
                       encoding="utf-8")
    ARCH_RESUMEN.write_text(generar_resumen(filas, enfen, noaa), encoding="utf-8")
    log(f"HTML: {ARCH_HTML}")
    log(f"Resumen: {ARCH_RESUMEN}")
    log(f"Instalaciones: {len(filas)} | " + " | ".join(f"Nivel {n}: {sum(f['nivel_plan'] == n for f in filas)}" for n in (1, 2, 3, 4))
        + f" | fuentes con error: {len(errores)}")

    CARPETA_HIST.mkdir(exist_ok=True)
    with open(CARPETA_HIST / "log_monitor.txt", "a", encoding="utf-8") as fh:
        fh.write("\n".join(LOG) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):          # resumen visible en la pagina de GitHub Actions
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("## Monitor FEN\n\n```\n" + "\n".join(l for l in LOG if "|" in l) + "\n```\n")


if __name__ == "__main__":
    main()
