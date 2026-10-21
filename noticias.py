"""
noticias.py  -  Alertas por noticias de la zona de cada instalacion (fuentes gratuitas)

Fuentes:  Google News RSS y Bing News RSS (sin clave, sin costo).
Interprete: reglas en espanol (evento actual + lugar + medios distintos + fuente oficial).

Solo cuentan los medios de medios_confiables.csv (por dominio); el resto se muestra aparte
como "otras fuentes" sin subir el nivel. Solo puede SUBIR el nivel de una instalacion:
  - Nivel 2: al menos 1 medio confiable informa un evento actual por lluvias (inundacion, aniego,
             huaico, desborde...) en el distrito de la tienda o a NOTICIAS_RADIO_KM, en las
             ultimas HORAS_NOTICIAS h.
  - Nivel 3: al menos 2 medios confiables distintos, y el hecho es grave (viviendas inundadas,
             damnificados, desborde, huaico...) o alguno cita una fuente oficial (COEN, INDECI...).
Cada alerta lleva los enlaces a las noticias para verificarla en un clic.
"""

from __future__ import annotations

import csv
import html
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HORAS_NOTICIAS = 12          # antiguedad maxima de una noticia para contar
NOTICIAS_RADIO_KM = 8        # distritos vecinos que cuentan como "la zona de la tienda"
NOTICIAS_RADIO_LIMA_KM = 4   # en Lima y Callao los distritos son chicos y estan pegados
DIST_LEJOS_KM = 20           # distrito del mismo departamento que se considera "otra zona"
MIN_MEDIOS_ROJA = 2
ARCH_MEDIOS = Path(__file__).resolve().parent / "medios_confiables.csv"

GOOGLE_RSS = "https://news.google.com/rss/search?q={q}&hl=es-419&gl=PE&ceid=PE:es-419"
BING_RSS = "https://www.bing.com/news/search?format=rss&q={q}"
CONSULTA = '{lugar} (inundación OR inundaciones OR inundan OR aniego OR aniegos OR anegadas OR huaico OR desborde OR "lluvias intensas" OR "fuertes lluvias" OR damnificados)'

# Evento que ya ocurrio o esta ocurriendo (no pronostico)
# "inundacion" como sustantivo solo cuenta con un verbo de hecho ocurrido ("riesgo de inundacion" no es un evento)
EVENTO = [r"INUNDA(N|RON|DO|DA|DOS|DAS)\b",
          r"(PROVOCA|PROVOCAN|PROVOCARON|CAUSA|CAUSAN|CAUSARON|DEJA|DEJAN|DEJARON|GENERA|GENERAN|REPORTA|REPORTAN|REGISTRA|REGISTRAN) (\w+ )?INUNDACION",
          r"\bANIEG", r"\bANEG(A|AN|ARON|ADO|ADA|ADOS|ADAS)\b",
          r"DESBORD(A|AN|O|ARON|E|ES|AMIENTO)\b", r"\bHUA[IY]COS?\b", r"BAJO EL AGUA", r"\bEN RIOS\b",
          r"COLAPS[OA]", r"DAMNIFICAD", r"\bATRAPAD[OA]S\b", r"EVACUA(N|RON|DOS|DAS|CION)\b",
          r"ACTIVACION DE (LA )?QUEBRADA", r"DESLIZAMIENTO"]
# "lluvias intensas" solo cuenta si la nota dice que ocurrieron (no en notas de riesgo o pronostico)
LLUVIA_INTENSA = [r"LLUVIAS? (DE FUERTE INTENSIDAD|INTENSAS?|FUERTES?|TORRENCIALES?)",
                  r"(FUERTES?|INTENSAS?|TORRENCIALES?) LLUVIAS?"]
OCURRIDO = [r"INUNDA(N|RON|DO|DA|DOS|DAS)\b", r"\bANEG(A|AN|ARON|ADO|ADA|ADOS|ADAS)\b",
            r"(PROVOCA|CAUSA|DEJA|GENERA|OCASIONA)\w* (\w+ )?(ANIEGOS?|INUNDACION|DAMNIFICADOS|DESBORDE|TORRENTES?)",
            r"SE DESBORD", r"DESBORDO", r"\bATRAPAD", r"BAJO EL AGUA", r"\bEN RIOS\b", r"(SE )?REGISTRA\w* (\w+ ){0,3}LLUVIA",
            r"LLUVIAS? (\w+ ){0,4}(SE REGISTRA|CAE|CAEN|CAYO|CAYERON|AZOTA|SORPRENDE)", r"\bSOPORTA", r"\bAZOT",
            r"DESDE LA MADRUGADA", r"ESTA MANANA", r"\bANOCHE\b"]
GRAVE = [r"INUNDA\w* (VIVIENDAS|CASAS|HOGARES)", r"DAMNIFICAD", r"DESBORD", r"\bHUA[IY]CO", r"COLAPS",
         r"EVACUA", r"FALLECID", r"ATRAPAD", r"ESTADO DE EMERGENCIA", r"BAJO EL AGUA", r"EN RIOS\b"]
FUTURO = [r"PRONOSTICO", r"COMO ESTARA", r"\bPODRIAN?\b", r"SE INTENSIFICARAN", r"\bAFRONTARIA", r"POSIBLE ACTIVACION",
          r"\b(INUNDA|DESBORDA|AFECTA|ACTIVA|ANEGA)RIAN?\b", r"YA NO SE", r"ENFRENTAN? (\w+ )?RIESGOS?",
          r"\bANTE (EL )?(FENOMENO )?(EL )?NINO\b", r"\bVULNERAB", r"\bAMENAZA", r"\bEN RIESGO\b",
          r"RIESGOS? (DE|POR) (\w+ ){0,2}(DESBORDE|INUNDACION|HUAICO|ACTIVACION)",
          r"TAREAS PENDIENTES", r"\bSIMULACRO", r"\bPREVENCION\b", r"\bPREPARA", r"OBRAS DE MITIGACION",
          r"\bHACE \w+ (ANOS?|MESES)\b", r"\bRECUERDA", r"\bANIVERSARIO"]
# El agua tiene que venir del clima: la noticia debe nombrar la lluvia o un fenomeno asociado...
CLIMA = r"LLUVI|PRECIPITACI|TORMENTA|CHUBASCO|AGUACERO|TEMPORAL|EL NINO|HUA[IY]CO|QUEBRADA|\bRIOS?\b|GRANIZ|CRECIDA|CAUDAL"
# ...y no debe atribuirlo a otra causa (tuberia rota, obra, sismo) sin mencionar lluvia
OTRA_CAUSA = (r"\bFUGA|TUBERIA|MATRIZ|SEDAPAL|SEDALIB|EPS GRAU|DESAGUE|ALCANTARILLADO|BUZON|ROTURA|\bOBRAS?\b|"
              r"MATERIAL DE CONSTRUCCION|DESMONTE|SISMO|TERREMOTO|TEMBLOR|INCENDIO|CISTERNA")
OFICIAL = [r"\bCOEN\b", r"\bINDECI\b", r"\bSENAMHI\b", r"MUNICIPALIDAD", r"GOBIERNO REGIONAL", r"\bALCALDE",
           r"DEFENSA CIVIL", r"\bPREFECT", r"ESTADO DE EMERGENCIA", r"\bCOER\b", r"\bBOMBEROS\b"]
CIUDAD_REGION = r"(REGION|DEPARTAMENTO( DE)?|NORTE DE|SIERRA DE)\s+$"
# Eventos en otro pais no cuentan aunque coincida un nombre (p. ej. "Miraflores")
OTRO_PAIS = r"\b(QUITO|GUAYAQUIL|BOGOTA|MEDELLIN|CARACAS|SANTIAGO DE CHILE|BUENOS AIRES|SAO PAULO|RIO DE JANEIRO|MONTEVIDEO|COCHABAMBA|SANTA CRUZ DE LA SIERRA|CIUDAD DE MEXICO|ECUADOR|COLOMBIA|BOLIVIA|CHILE|BRASIL|ARGENTINA|VENEZUELA|MEXICO|ESPANA|ESTADOS UNIDOS|EE\.? ?UU|GUATEMALA|HONDURAS|PANAMA|URUGUAY|PARAGUAY|CUBA|HAITI|FILIPINAS|INDIA|CHINA|PAKISTAN|ITALIA|FRANCIA|ALEMANIA)\b"


def _norm(txt) -> str:
    t = str(txt or "").upper()
    for a, b in zip("ÁÉÍÓÚÜÀÈÌÒÙ", "AEIOUUAEIOU"):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t)


def _hay(patrones, texto) -> bool:
    return any(re.search(p, texto) for p in patrones)


def _haversine(lat1, lon1, lat2, lon2):
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


# ----------------------------------------------------------------------------
# Lectura de RSS
# ----------------------------------------------------------------------------
def leer_rss(xml: str, origen: str) -> list[dict]:
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def campo(nombre):
            m = re.search(rf"<{nombre}[^>]*>(.*?)</{nombre}>", it, re.S)
            return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""
        titulo, link = campo("title"), campo("link")
        try:
            fecha = parsedate_to_datetime(campo("pubDate")).astimezone()
        except (TypeError, ValueError):
            continue
        medio = campo("source") or campo("News:Source")
        m_src = re.search(r'<source url="([^"]+)"', it)
        if m_src:                                     # Google: dominio del medio
            url_medio = m_src.group(1)
        else:                                         # Bing: enlace de redireccion con url=<original>
            url_medio = (parse_qs(urlparse(html.unescape(link)).query).get("url") or [link])[0]
        if origen == "google" and " - " in titulo:      # Google: "Titular - Medio"
            titulo, medio2 = titulo.rsplit(" - ", 1)
            medio = medio or medio2
        resumen = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", campo("description"))).strip()
        if medio:       # Google repite el medio en el resumen ("... Radio Miraflores")
            resumen = resumen.replace(medio.strip(), " ")
        if titulo and link:
            out.append({"titulo": titulo.strip(), "link": link, "fecha": fecha, "medio": medio.strip() or "?",
                        "dominio": dominio(url_medio), "resumen": resumen[:400], "origen": origen})
    return out


def dominio(url: str) -> str:
    d = urlparse(url if "//" in url else "//" + url).netloc.lower().split(":")[0]
    return d[4:] if d.startswith("www.") else d


def leer_medios(ruta: Path = ARCH_MEDIOS) -> dict:
    """dominio -> nombre de los medios confiables (medios_confiables.csv)."""
    if not ruta.exists():
        return {}
    with open(ruta, encoding="utf-8-sig") as fh:
        return {dominio(f["dominio"].strip()): f["nombre"].strip() for f in csv.DictReader(fh) if f.get("dominio")}


def es_confiable(dom: str, medios: dict) -> str:
    """Dominio de la lista que corresponde (acepta subdominios: noticias.rpp.pe -> rpp.pe); '' si no esta."""
    return next((d for d in medios if dom == d or dom.endswith("." + d)), "")


def buscar(lugar: str, http_get, headers, timeout) -> list[dict]:
    """Noticias de las ultimas 24 h sobre lluvias/inundaciones en un lugar (Google + Bing)."""
    q = CONSULTA.format(lugar=lugar)
    items, errores = [], []
    for origen, url in (("google", GOOGLE_RSS.format(q=quote(q + " when:1d"))), ("bing", BING_RSS.format(q=quote(q)))):
        try:
            r = http_get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            items += leer_rss(r.text, origen)
        except Exception as err:  # noqa: BLE001
            errores.append(f"{origen}: {err}")
    if errores and not items:
        raise ConnectionError("; ".join(errores))
    return items


# ----------------------------------------------------------------------------
# Lugares de cada tienda
# ----------------------------------------------------------------------------
def lugares_tienda(t, ubigeo_filas: list[dict]) -> dict:
    """Nombres que cuentan como la zona de la tienda y nombres del mismo departamento que son otra zona."""
    lima = _norm(t["departamento"]) in ("LIMA", "CALLAO") and _norm(t["ciudad"]) in ("LIMA", "CALLAO")
    radio = NOTICIAS_RADIO_LIMA_KM if lima else NOTICIAS_RADIO_KM
    dep = _norm(t["departamento"])
    cerca, lejos = {_norm(t["distrito"])}, set()
    for u in ubigeo_filas:
        if u["dep"] != dep and not (lima and u["dep"] in ("LIMA", "CALLAO")):
            continue
        if u["lat"] is None:
            continue
        d = _haversine(t["lat"], t["lon"], u["lat"], u["lon"])
        if d <= radio:
            cerca.add(u["dist"])
        elif d >= DIST_LEJOS_KM:
            lejos.add(u["dist"])
    if lima:
        # "Lima" sola no ubica nada; el Cercado se reconoce por su nombre propio
        cerca.discard("LIMA")
        if _norm(t["distrito"]) == "LIMA":
            cerca |= {"CERCADO DE LIMA", "CENTRO DE LIMA"}
        ciudad = None
    else:
        ciudad = _norm(t["ciudad"])
        cerca.add(ciudad)
    lejos -= cerca
    return {"cerca": sorted(cerca, key=len, reverse=True), "lejos": sorted(lejos, key=len, reverse=True),
            "ciudad": ciudad, "consulta": t["ciudad"] if ciudad else _titulo_distrito(t["distrito"])}


def _titulo_distrito(d) -> str:
    return " ".join(w.capitalize() if w not in ("DE", "LA", "EL", "LOS", "LAS", "DEL") else w.lower()
                    for w in str(d).split())


def _menciona(nombre: str, texto: str) -> list[int]:
    return [m.start() for m in re.finditer(rf"(?<![A-Z]){re.escape(nombre)}(?![A-Z])", texto)]


def ubicar(texto: str, lug: dict) -> str:
    """'cerca' si la noticia ubica el evento en la zona de la tienda; 'lejos' si solo en otra zona; '' si no la nombra."""
    for n in lug["cerca"]:
        for pos in _menciona(n, texto):
            # "region Piura" o "departamento de Piura" no ubican el evento en la ciudad
            if n == lug["ciudad"] and re.search(CIUDAD_REGION, texto[:pos]):
                continue
            if n == lug["ciudad"] and any(_menciona(x, texto) for x in lug["lejos"]):
                continue        # "Piura: inundan Chulucanas" -> el evento es en Chulucanas
            return "cerca"
    if any(_menciona(x, texto) for x in lug["lejos"]) or (lug["ciudad"] and _menciona(lug["ciudad"], texto)):
        return "lejos"
    return ""


# ----------------------------------------------------------------------------
# Interprete por reglas
# ----------------------------------------------------------------------------
def clasificar(item: dict, ahora: datetime) -> dict:
    texto = _norm(item["titulo"] + " . " + item["resumen"])
    horas = (ahora - item["fecha"]).total_seconds() / 3600
    evento = _hay(EVENTO, texto) or (_hay(LLUVIA_INTENSA, texto) and _hay(OCURRIDO, texto))
    futuro = _hay(FUTURO, texto)
    viejo = any(int(a) < ahora.year for a in re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", texto))
    extranjero = bool(re.search(OTRO_PAIS, texto)) and not re.search(r"\bPERU", texto)
    clima = bool(re.search(CLIMA, texto))
    otra_causa = bool(re.search(OTRA_CAUSA, texto)) and "LLUVI" not in texto
    actual = (evento and clima and not otra_causa and not futuro and not viejo and not extranjero
              and -1 <= horas <= HORAS_NOTICIAS)
    return {"texto": texto, "horas": horas, "actual": actual,
            "grave": actual and _hay(GRAVE, texto), "oficial": actual and _hay(OFICIAL, texto)}


def evaluar_tienda(items: list[dict], lug: dict, ahora: datetime, medios: dict) -> dict:
    """Noticias relevantes para una tienda y el nivel que justifican (1 = ninguno).
    Solo los medios confiables cuentan para el nivel; los demas quedan en 'otras'."""
    rel, otras, vistos = [], [], set()
    for it in sorted(items, key=lambda x: x["fecha"], reverse=True):
        if it["link"] in vistos:
            continue
        vistos.add(it["link"])
        c = clasificar(it, ahora)
        if c["actual"] and ubicar(c["texto"], lug) == "cerca":
            fila = {**it, "grave": c["grave"], "oficial": c["oficial"], "horas": round(c["horas"], 1)}
            conf = es_confiable(it.get("dominio", ""), medios)
            if conf:
                rel.append({**fila, "medio": medios[conf], "clave_medio": conf})
            else:
                otras.append(fila)
    n_medios = len({x["clave_medio"] for x in rel})
    oficial, grave = any(x["oficial"] for x in rel), any(x["grave"] for x in rel)
    nivel = 1
    if rel:
        nivel = 3 if n_medios >= MIN_MEDIOS_ROJA and (grave or oficial) else 2
    return {"nivel": nivel, "noticias": rel, "otras": otras, "n_medios": n_medios, "oficial": oficial,
            "grave": grave}


def obtener(tiendas, ubigeo_filas, http_get, headers, timeout, log=print, ahora=None) -> dict:
    """Busca una vez por lugar y evalua cada tienda.  Devuelve {cod_p: evaluacion, '_errores': [...]}."""
    ahora = ahora or datetime.now().astimezone()
    lugares = {str(t["cod_p"]): lugares_tienda(t, ubigeo_filas) for t in tiendas}
    cache, errores = {}, []
    for lug in lugares.values():
        q = lug["consulta"]
        if q not in cache:
            try:
                cache[q] = buscar(q, http_get, headers, timeout)
            except Exception as err:  # noqa: BLE001
                cache[q] = []
                errores.append(f"{q}: {err}")
    log(f"     Noticias: {sum(len(v) for v in cache.values())} leídas en {len(cache)} búsquedas"
        + (f"; {len(errores)} búsqueda(s) con error" if errores else ""))
    if errores and len(errores) == len(cache):
        raise ConnectionError("ninguna búsqueda de noticias respondió: " + errores[0])
    medios = leer_medios()
    if not medios:
        raise ValueError("medios_confiables.csv no existe o está vacío: no se puede evaluar noticias")
    out = {cod: evaluar_tienda(cache[lug["consulta"]], lug, ahora, medios) for cod, lug in lugares.items()}
    out["_errores"] = errores
    return out


def motivo(ev: dict) -> str:
    n = ev["n_medios"]
    base = (f"{n} medio{'s' if n != 1 else ''} confiable{'s' if n != 1 else ''} informa{'n' if n != 1 else ''} "
            f"inundación o lluvias intensas en la zona de la tienda")
    if ev["nivel"] >= 3:
        base += ", " + " y ".join(x for x, si in (("hecho grave", ev["grave"]), ("con mención de fuente oficial", ev["oficial"])) if si)
    return base + f" (noticias de las últimas {HORAS_NOTICIAS} h)"
