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
HORAS_AUTOMATICAS = "cada hora (a los :17 min)"   # informativo, igual que el workflow
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
ARCH_MANUALES = CARPETA / "alertas_manuales.csv"   # Nivel 4 u otros ajustes manuales

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
        est["obs"].append({"ts": ts, "temp": float(temp), "wx": wx,
                           "lluvia": any(c in wx for c in LLUVIA_METAR), "raw": m.get("rawOb", "")})
    for est in out.values():
        est["obs"].sort(key=lambda o: o["ts"])
        ult = est["obs"][-1]
        u24 = [o for o in est["obs"] if (ahora - o["ts"]).total_seconds() <= 24 * 3600]
        est.update({"ultima": ult, "tmax24": max(o["temp"] for o in u24) if u24 else None,
                    "tmin24": min(o["temp"] for o in u24) if u24 else None,
                    "lluvia_6h": [o for o in est["obs"] if o["lluvia"] and (ahora - o["ts"]).total_seconds() <= 6 * 3600],
                    "serie": [o["temp"] for o in u24][-24:]})
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
        t = (f"{c['ahora']:.0f} °C a las {c['hora']} en el aeropuerto de {c['nombre'].split(' – ')[0]} (CORPAC); "
             f"últimas 24 h: máxima {r['tmax']:.0f} °C, mínima {r['tmin']:.0f} °C")
        if c["lluvia"]:
            t += f". <b>Lluvia observada</b> (último reporte con lluvia: {c['lluvia_hora']})"
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
        if r["tmax"] is None and datos.get("corpac"):
            cand = sorted(((haversine_km(t.lat, t.lon, a["lat"], a["lon"]), c, a) for c, a in datos["corpac"].items()
                           if a.get("lat") is not None), key=lambda x: x[0])
            if cand and cand[0][0] <= DIST_MAX_AEROPUERTO_KM and cand[0][2]["tmax24"] is not None:
                dist, icao, a = cand[0]
                r["tmax"], r["tmin"], r["prec"] = a["tmax24"], a["tmin24"], None
                r["serie_tmax"], r["serie_tmin"] = a["serie"], []
                r["prom_tmax"] = r["tend_tmax"] = r["prom_tmin"] = r["tend_tmin"] = None
                hora = a["ultima"]["ts"].strftime("%H:%M")
                r["corpac"] = {"icao": icao, "nombre": a["nombre"], "dist": dist, "ahora": a["ultima"]["temp"],
                               "hora": hora, "lluvia": bool(a["lluvia_6h"]),
                               "lluvia_hora": a["lluvia_6h"][-1]["ts"].strftime("%H:%M") if a["lluvia_6h"] else ""}
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

    # Tabs de zonas
    tabs = ['<button class="tab activo" data-zona="todas">Todas <span>' + str(len(filas)) + '</span></button>']
    for _, z, _fr in zonas:
        n = sum(f["zona"] == z for f in filas)
        alerta = sum(f["zona"] == z and f["nivel_plan"] >= 2 for f in filas)
        punto = '<i class="dot"></i>' if alerta else ""
        tabs.append(f'<button class="tab" data-zona="{e(z)}">{e(z)} <span>{n}</span>{punto}</button>')

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
            est_txt = f["estacion"] or f"sin estación a menos de {DIST_MAX_ESTACION_KM} km"
            c = f.get("corpac")
            if c:
                metrica3 = (f'<div><small>Ahora ({c["hora"]})</small><strong>{c["ahora"]:.0f}°</strong>'
                            + ('<span class="delta sube">lluvia observada</span>' if c["lluvia"] else "") + "</div>")
                etq_max, etq_min, etq_serie = "Máx 24 h", "Mín 24 h", "Temperatura horaria últimas 24 h"
            else:
                prec_txt = "—" if f["prec"] is None else f"{f['prec']:.1f}<em>mm</em>"
                metrica3 = f"<div><small>Lluvia</small><strong>{prec_txt}</strong></div>"
                etq_max, etq_min, etq_serie = "T. máx", "T. mín", "T. máx últimos 15 registros"
            tarjetas.append(f"""
      <article class="card sem-{f['semaforo']}" id="t-{e(f['cod_p'])}" data-zona="{e(z)}">
        <header><div><h3>{e(f['tienda'])}</h3><p class="muted">{e(f['ciudad'])} · {e(f['distrito'].title())} · {e(f['cod_p'])}</p>
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
        </div>
        <details><summary>Ver detalle técnico</summary>
          <p class="muted est">Estación SENAMHI: {e(est_txt)}</p>
          <div class="fila"><span>{etq_serie}</span>{sparkline(f['serie_tmax'])}</div>
          <div class="fila"><span>T. mín últimos 15 registros</span>{sparkline(f['serie_tmin'])}</div>
          <div class="fila"><span>Promedio 15 registros · T. máx / T. mín</span><span>{pmx} / {pmn}</span></div>
          <div class="fila"><span>Pronóstico SENAMHI T. mín 10 días</span>{sparkline(f['tmin10'])}</div>
          <div class="fila"><span>Próximo mes · T. máx / T. mín / Lluvia</span><span class="chips">{chip_escenario(f['pm_tmax_esc'])}{chip_escenario(f['pm_tmin_esc'])}{chip_escenario(f['pm_pp_esc'])}</span></div>
          <div class="fila"><span>Lluvia verano {e(f['verano_meta']).replace('_', '–').title()}</span>{chip_escenario(f['verano_esc'])}</div>
          {proy}
          {avisos}
        </details>
      </article>""")
        bloques.append(f'<section class="zona" data-zona="{e(z)}"><h2>Zona {e(z)} <span class="frec">Reporte {e(frec).lower()}</span></h2><div class="grid">{"".join(tarjetas)}</div></section>')

    err_html = ""
    if errores:
        otros_err = [k for k in errores if k != "SENAMHI (IDESEP)"]
        partes = []
        if "SENAMHI (IDESEP)" in errores:
            partes.append("SENAMHI no es accesible desde el servidor: los avisos se toman de INDECI, el riesgo por distrito "
                          "de CENEPRED y la temperatura de los aeropuertos CORPAC.")
        if otros_err:
            partes.append("<b>No respondieron en esta actualización:</b> " + ", ".join(e(k) for k in otros_err) + ".")
        err_html = '<div class="aviso-sistema">' + " ".join(partes) + "</div>"

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
        f'<li><b>Aviso N° {e(a["numero"])}</b>{" · " + e(a["evento"]) if a["evento"] else ""}'
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
        ("CORPAC", "Reportes meteorológicos horarios de los aeropuertos: temperatura actual y lluvia observada."),
        ("Mar Niño 1+2 (NOAA)", "Temperatura del mar frente a la costa norte comparada con lo normal; sobre +1 °C favorece calor y lluvias."),
        ("Riesgo de la instalación", "Clasificación fija de la Matriz Nacional de Riesgo del plan (Crítico, Alto, Medio Alto, Medio, Bajo)."),
    ]
    glos = "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in glosario)

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monitor FEN · Falabella Retail Perú</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700;900&display=swap" rel="stylesheet">
<style>
:root{{--fala:#aad500;--fala-osc:#5c7a00;--fala-suave:#f3f9dd;--bg:#f5f6f4;--card:#fff;--tx:#333a36;--mut:#6b736e;--bd:#e3e6e1;
--rojo:#c00000;--ambar:#e07b00;--amar:#ffc000;--verde:#70ad47;--azul:#1f5fa8;--negro:#111;}}
*{{box-sizing:border-box}} html{{scroll-behavior:smooth}}
body{{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 Lato,"Segoe UI",system-ui,sans-serif}}
a{{color:var(--fala-osc);font-weight:700}}
.muted{{color:var(--mut)}} .small{{font-size:11.5px}}
/* ---------- cabecera ---------- */
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
.btn{{display:inline-block;font:700 13px Lato,sans-serif;text-decoration:none;padding:8px 16px;border-radius:22px;background:var(--fala);color:#1d2a00;border:0;cursor:pointer}}
.btn:hover{{filter:brightness(.95)}} .btn.sec{{background:#fff;color:var(--fala-osc);border:1.5px solid var(--fala)}}
.vistas{{max-width:1280px;margin:10px auto 0;padding:0 16px;display:flex;gap:4px;width:100%}}
.vista{{background:none;border:0;border-bottom:3px solid transparent;padding:10px 14px;font:700 14px Lato,sans-serif;color:var(--mut);cursor:pointer}}
.vista.activo{{color:var(--tx);border-bottom-color:var(--fala-osc)}}
.wrap{{max-width:1280px;margin:0 auto;padding:18px 16px 40px}}
section.panel{{display:none}} section.panel.activo{{display:block;animation:aparece .35s ease}}
@keyframes aparece{{from{{opacity:0;transform:translateY(6px)}}to{{opacity:1;transform:none}}}}
h2{{font-size:18px;font-weight:900;margin:26px 0 10px}} h2 small{{font-weight:400;color:var(--mut);font-size:12.5px;margin-left:6px}}
/* ---------- resumen ---------- */
.estado{{display:grid;grid-template-columns:2fr 1fr 1.4fr 1.4fr;gap:12px}}
.caja{{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px 16px}}
.caja small{{color:var(--mut);display:block;font-size:12px}} .caja strong{{display:block;font-size:20px;font-weight:900;margin:2px 0}}
.caja.fen{{border-left:6px solid var(--mut)}} .caja.fen.rojo{{border-left-color:var(--rojo)}} .caja.fen.amarillo{{border-left-color:var(--amar)}}
.explica{{font-size:12px;color:var(--mut);margin:6px 0 0}}
.niveles{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:6px}}
.nv{{border-radius:10px;padding:8px 4px;text-align:center;font-size:22px;font-weight:900}} .nv em{{display:block;font-size:10.5px;font-style:normal;font-weight:700}}
.nv.n1,.pill.n1{{background:var(--verde);color:#fff}} .nv.n2,.pill.n2{{background:var(--amar);color:#111}} .nv.n3,.pill.n3{{background:var(--rojo);color:#fff}} .nv.n4,.pill.n4{{background:var(--negro);color:#fff}}
.pill{{display:inline-block;font-size:11.5px;font-weight:700;padding:3px 10px;border-radius:12px;white-space:nowrap}}
.tabla-wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--bd);border-radius:14px}}
.tabla{{width:100%;border-collapse:collapse;font-size:13px}}
.tabla th{{text-align:left;font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--mut);background:var(--fala-suave);padding:10px 12px}}
.tabla td{{padding:10px 12px;border-top:1px solid var(--bd);vertical-align:top}}
.fila-alerta{{cursor:pointer;transition:background .15s}} .fila-alerta:hover{{background:var(--fala-suave)}}
.ver{{color:var(--fala-osc);font-weight:700;white-space:nowrap}}
.vacio{{background:var(--fala-suave);border:1px solid var(--fala);border-radius:14px;padding:16px;font-size:14px}}
.tres{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.lista{{list-style:none;padding:0;margin:6px 0 0;display:grid;gap:8px;font-size:12.5px}}
.lista li{{border-left:3px solid var(--fala);padding:2px 0 2px 10px}}
.cambio{{font-size:11px;font-weight:700}} .cambio.sube{{color:var(--rojo)}} .cambio.baja{{color:var(--verde)}}
.aviso-sistema{{background:#fff7e6;border:1px solid var(--ambar);border-radius:10px;padding:9px 12px;margin-top:12px;font-size:12.5px}}
/* ---------- por zona ---------- */
.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 6px}}
.tab{{border:1.5px solid var(--bd);background:#fff;color:var(--tx);border-radius:20px;padding:6px 13px;cursor:pointer;font:700 13px Lato,sans-serif}}
.tab span{{color:var(--mut);margin-left:4px;font-weight:400}} .tab.activo{{background:var(--fala);border-color:var(--fala);color:#1d2a00}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ambar);margin-left:6px;vertical-align:middle}}
.frec{{font-size:12px;font-weight:400;color:var(--mut);margin-left:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--bd);border-top:5px solid var(--verde);border-radius:14px;padding:14px;scroll-margin-top:140px}}
.card.sem-rojo{{border-top-color:var(--rojo)}} .card.sem-amarillo{{border-top-color:var(--amar)}} .card.sem-negro{{border-top-color:var(--negro)}}
.card.resalta{{animation:resalta 1.6s ease}} @keyframes resalta{{0%,60%{{box-shadow:0 0 0 4px var(--fala)}}100%{{box-shadow:none}}}}
.card header{{display:flex;justify-content:space-between;gap:8px}} .card h3{{font-size:15px;margin:0;font-weight:900}} .card header p{{margin:2px 0 0;font-size:12px}}
.sem-badge{{font-size:11px;font-weight:700;white-space:nowrap;padding:3px 9px;border-radius:12px;height:fit-content}}
.sem-verde .sem-badge{{background:var(--verde);color:#fff}} .sem-rojo .sem-badge{{background:var(--rojo);color:#fff}} .sem-amarillo .sem-badge{{background:var(--amar);color:#111}} .sem-negro .sem-badge{{background:#111;color:#fff}}
.badges{{display:flex;flex-direction:column;align-items:flex-end;gap:4px}}
.tags{{margin:4px 0 0!important;display:flex;gap:4px;flex-wrap:wrap}}
.riesgo,.tipo{{font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:9px;background:var(--bd)}}
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
.chips{{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}} .chip{{font-size:11px;padding:2px 7px;border-radius:10px;background:var(--bd)}}
.esc-sup{{background:#fde8e8;color:var(--rojo)}} .esc-inf{{background:#e6eef9;color:var(--azul)}}
.avisos{{list-style:none;padding:0;margin:8px 0 0;display:grid;gap:6px}}
.avisos li{{font-size:12px;border-left:3px solid var(--amar);padding:4px 8px;background:#fafaf8;border-radius:0 6px 6px 0}}
.avisos li.nv4,.avisos li.nv5{{border-left-color:var(--rojo)}} .avisos li.nv2{{border-left-color:var(--verde)}} .avisos li span{{color:var(--mut)}}
.sin-aviso{{font-size:12px;color:var(--verde);margin:8px 0 0}}
/* ---------- guia ---------- */
.guia{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}}
.nivel-card{{background:var(--card);border:1px solid var(--bd);border-top:6px solid;border-radius:14px;padding:14px 16px}}
.nivel-card.n1{{border-top-color:var(--verde)}} .nivel-card.n2{{border-top-color:var(--amar)}} .nivel-card.n3{{border-top-color:var(--rojo)}} .nivel-card.n4{{border-top-color:var(--negro)}}
.nivel-card header{{display:flex;align-items:center;gap:8px}} .nivel-card h3{{margin:0;font-size:15px;font-weight:900}}
.nivel-card .que{{font-weight:700;margin:10px 0}} .nivel-card h4{{font-size:12px;text-transform:uppercase;color:var(--fala-osc);margin:10px 0 4px;letter-spacing:.03em}}
.nivel-card ul{{margin:0;padding-left:18px;font-size:12.5px}}
.glosario{{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px 16px;font-size:13px}}
.glosario dt{{font-weight:900;color:var(--fala-osc)}} .glosario dd{{margin:0}}
footer{{margin-top:36px;font-size:12px;color:var(--mut);border-top:1px solid var(--bd);padding-top:12px}}
@media (max-width:900px){{.estado,.tres{{grid-template-columns:1fr 1fr}}}}
@media (max-width:700px){{.barra{{position:static}} .tabla thead{{display:none}} .tabla,.tabla tbody,.tabla tr,.tabla td{{display:block;width:100%}}
.tabla tr{{border-top:1px solid var(--bd);padding:6px 0}} .tabla td{{border:0;padding:4px 14px}} .tabla td.ver{{text-align:right}}}}
@media (max-width:560px){{.estado,.tres{{grid-template-columns:1fr}} .act{{align-items:flex-start}} .marca h1{{font-size:17px}} .metricas strong{{font-size:17px}}}}
</style></head><body>
<div class="barra"><div class="in">
  <div class="marca"><img class="logo" src="logo.png" alt="" onerror="this.remove()">
    <div><h1>Monitor FEN <span>·</span> Falabella Retail Perú</h1>
    <p>Actualizado {HOY:%d/%m/%Y %H:%M} · ENFEN, SENAMHI, INDECI, CENEPRED, CORPAC, NOAA · Niveles del Plan Integral FEN 2026/2027</p></div></div>
  <div class="act">{boton_actualizar}</div>
</div>
<nav class="vistas"><button class="vista activo" data-panel="resumen">Resumen</button><button class="vista" data-panel="zonas">Por zona</button><button class="vista" data-panel="guia">Guía de alertas</button></nav>
</div>

<div class="wrap">
<section class="panel activo" id="resumen">
  <div class="estado">
    <div class="caja fen {estado_cls}"><small>Estado ENFEN</small><strong>{e(estado)}</strong>
      <p class="explica">{e(fen_txt)}</p>
      <span class="muted small">{e(enfen.get('comunicado'))} · {e(enfen.get('fecha'))}</span>
      {f' · <a class="small" href="{e(enfen.get("url_pdf"))}" target="_blank" rel="noopener">comunicado</a>' if enfen.get('url_pdf') else ''}</div>
    <div class="caja"><small>Mar frente a la costa norte (Niño 1+2)</small><strong>{noaa_txt}</strong><span class="muted small">{noaa_sub}</span><div>{noaa_spark}</div></div>
    <div class="caja"><small>Instalaciones por nivel</small>
      <div class="niveles"><span class="nv n1">{cuenta[1]}<em>Verde</em></span><span class="nv n2">{cuenta[2]}<em>Amarilla</em></span><span class="nv n3">{cuenta[3]}<em>Roja</em></span><span class="nv n4">{cuenta[4]}<em>Negra</em></span></div>
      <p class="explica">de {len(filas)} instalaciones</p></div>
    <div class="caja"><small>Calendario corporativo FEN</small><strong style="font-size:15px">{e(fase)}</strong><p class="explica">{e(fase_obj)}</p></div>
  </div>
  {err_html}
  <h2>Instalaciones con alerta <small>sobre Nivel 1 · clic para ver el detalle</small></h2>
  {tabla_alertas}
  <h2>Cambios desde la última actualización</h2>
  <ul class="lista caja">{cambios_html}</ul>
  <h2>Situación oficial vigente</h2>
  <div class="tres">
    <div class="caja"><small>Avisos SENAMHI (vía INDECI)</small><ul class="lista">{av_html}</ul></div>
    <div class="caja"><small>Escenarios de riesgo CENEPRED</small><ul class="lista">{esc_li}</ul></div>
    <div class="caja"><small>Emergencias INDECI por lluvias cerca de una instalación</small><ul class="lista">{ind_li}</ul></div>
  </div>
</section>

<section class="panel" id="zonas">
  <nav class="tabs">{''.join(tabs)}</nav>
  {''.join(bloques)}
</section>

<section class="panel" id="guia">
  <h2>¿Qué significa cada alerta?</h2>
  <div class="guia">{guia}</div>
  <h2>Fuentes y términos</h2>
  <dl class="glosario">{glos}</dl>
</section>

<footer><p>Temperatura: estación SENAMHI cercana si responde; si no, aeropuerto CORPAC a {DIST_MAX_AEROPUERTO_KM} km o menos; si no, referencia regional NASA POWER. Un nivel alcanzado se mantiene {PERSISTENCIA_HORAS} h. Riesgo de cada instalación según la Matriz Nacional de Riesgo del plan.</p>
<p>Fuentes: {fuentes}</p></footer>
</div>
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
  document.querySelectorAll('section.panel').forEach(s=>s.classList.toggle('activo',s.id===id));}}
document.querySelectorAll('.vista').forEach(v=>v.addEventListener('click',()=>{{panel(v.dataset.panel);window.scrollTo({{top:0}});}}));
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('activo'));b.classList.add('activo');
  const z=b.dataset.zona;document.querySelectorAll('section.zona').forEach(s=>s.style.display=(z==='todas'||s.dataset.zona===z)?'':'none');
}}));
document.querySelectorAll('.fila-alerta').forEach(tr=>tr.addEventListener('click',()=>{{
  panel('zonas');document.querySelector('.tab[data-zona="todas"]').click();
  const c=document.getElementById(tr.dataset.ir);if(c){{c.scrollIntoView({{behavior:'smooth',block:'start'}});c.classList.remove('resalta');void c.offsetWidth;c.classList.add('resalta');}}
}}));
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
    if sin_temp:
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
                                  {"avisos_indeci": datos.get("avisos_indeci", []), "indeci": datos.get("indeci", [])}), encoding="utf-8")
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
