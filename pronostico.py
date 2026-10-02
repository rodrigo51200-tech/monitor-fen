"""
pronostico.py  -  Pronostico de lluvia por instalacion y garua en Lima para el Monitor FEN

Fuentes oficiales y gratuitas (solo lectura):
  - ECMWF open data (CC BY 4.0): probabilidades del conjunto de 51 simulaciones (lluvia > 1, 5, 10, 20 mm en 24 h)
    y el modelo deterministico (lluvia cada 6 h) -> https://data.ecmwf.int/forecasts
  - NOAA GFS (dominio publico) via NOMADS, recorte Peru (lluvia cada 6 h)
  - Garua en Lima: reportes METAR del aeropuerto Jorge Chavez (CORPAC) de la vispera y un modelo calibrado
    con junio-setiembre 2023-2025 (modelo_garua.json)

Los pronosticos se recalculan una vez por franja de 4 horas (0, 4, 8, 12, 16 y 20 h de Lima) y se guardan en
cache/pronostico.json; las demas corridas horarias reutilizan ese resultado.
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    import eccodes
except Exception:                      # sin eccodes no hay pronostico de lluvia, pero el monitor sigue
    eccodes = None

LIMA = timezone(timedelta(hours=-5))
ECMWF = "https://data.ecmwf.int/forecasts"
NOMADS = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
HEADERS = {"User-Agent": "MonitorFEN-SagaFalabella/1.0 (uso interno, consulta cada 4 h)"}
TIMEOUT = (10, 60)
HORAS_FRANJA = 4                        # una actualizacion de pronostico por cada franja de 4 h
PARAMS_EP = ("tpg1", "tpg5", "tpg10", "tpg20")
PARTES = [(0, "la mañana"), (6, "la tarde"), (12, "la noche"), (18, "la madrugada")]   # desde las 7 a. m. de Lima
MESES_GARUA = (5, 6, 7, 8, 9, 10, 11)   # temporada de garua en Lima (fuera de ella no se pronostica)


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def _get(url, **kw):
    ultimo = None
    for i in range(3):
        try:
            r = requests.get(url, headers={**HEADERS, **kw.pop("headers", {})}, timeout=TIMEOUT, **kw)
            if r.status_code in (200, 206):
                return r
            if r.status_code == 404:
                return None
            ultimo = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            ultimo = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {ultimo}")


def _indice_grilla(g, lat, lon):
    """Indice del punto de grilla mas cercano en una grilla regular lat/lon (GRIB)."""
    la1 = eccodes.codes_get(g, "latitudeOfFirstGridPointInDegrees")
    lo1 = eccodes.codes_get(g, "longitudeOfFirstGridPointInDegrees")
    di = eccodes.codes_get(g, "iDirectionIncrementInDegrees")
    dj = eccodes.codes_get(g, "jDirectionIncrementInDegrees")
    ni = eccodes.codes_get(g, "Ni")
    nj = eccodes.codes_get(g, "Nj")
    sube = eccodes.codes_get(g, "jScansPositively") == 1
    j = round(((lat - la1) if sube else (la1 - lat)) / dj)
    i = round(((lon - lo1) % 360) / di)
    if not (0 <= i < ni and 0 <= j < nj):
        return None
    return j * ni + i


def _valores(contenido: bytes, puntos):
    """Lee todos los mensajes GRIB de 'contenido' y devuelve [(claves, [valor por punto])]."""
    out = []
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as fh:
        fh.write(contenido)
        ruta = fh.name
    try:
        with open(ruta, "rb") as fh:
            while True:
                g = eccodes.codes_grib_new_from_file(fh)
                if g is None:
                    break
                try:
                    vals = eccodes.codes_get_values(g)
                    claves = {k: eccodes.codes_get(g, k) for k in ("shortName", "stepRange")}
                    fila = []
                    for lat, lon in puntos:
                        k = _indice_grilla(g, lat, lon)
                        fila.append(None if k is None else float(vals[k]))
                    out.append((claves, fila))
                finally:
                    eccodes.codes_release(g)
    finally:
        os.unlink(ruta)
    return out


def _rangos_index(url_index, quiere):
    """Lee un .index de ECMWF (JSON por linea) y devuelve los rangos de bytes de los campos pedidos."""
    r = _get(url_index)
    if r is None:
        return None
    rangos = []
    for linea in r.text.splitlines():
        d = json.loads(linea)
        if quiere(d):
            rangos.append((d["_offset"], d["_offset"] + d["_length"] - 1, d))
    return rangos


def _bajar_rangos(url, rangos):
    partes = []
    for a, b, _ in rangos:
        r = _get(url, headers={"Range": f"bytes={a}-{b}"})
        if r is not None:
            partes.append(r.content)
    return b"".join(partes)


# ----------------------------------------------------------------------------
# ECMWF: probabilidades del conjunto y modelo deterministico
# ----------------------------------------------------------------------------
def _corrida_ecmwf(ahora_utc):
    """Ultima corrida de 00 o 12 UTC con probabilidades publicadas."""
    for horas_atras in range(0, 48, 12):
        t = ahora_utc - timedelta(hours=horas_atras)
        corrida = t.replace(hour=12 if t.hour >= 12 else 0, minute=0, second=0, microsecond=0)
        base = f"{ECMWF}/{corrida:%Y%m%d}/{corrida:%H}z/ifs/0p25"
        nombre = f"{corrida:%Y%m%d%H}0000"
        if _get(f"{base}/enfo/{nombre}-240h-enfo-ep.index") is not None and \
                _get(f"{base}/oper/{nombre}-84h-oper-fc.index") is not None:
            return corrida, base, nombre
    return None


def fuente_ecmwf(puntos, ahora_utc):
    c = _corrida_ecmwf(ahora_utc)
    if c is None:
        raise RuntimeError("ECMWF: no hay corrida reciente publicada")
    corrida, base, nombre = c
    # ventanas de 24 h que empiezan a las 12 UTC (7 a. m. de Lima)
    ini = 0 if corrida.hour == 12 else 12
    ventanas = [f"{ini + 24 * k}-{ini + 24 * (k + 1)}" for k in range(3)]
    url = f"{base}/enfo/{nombre}-240h-enfo-ep"
    rangos = _rangos_index(url + ".index", lambda d: d["param"] in PARAMS_EP and d.get("step") in ventanas)
    prob = {v: {} for v in ventanas}            # ventana -> parametro -> [valor por punto]
    for claves, fila in _valores(_bajar_rangos(url + ".grib2", rangos), puntos):
        if claves["stepRange"] in prob:
            prob[claves["stepRange"]][claves["shortName"]] = fila
    # lluvia deterministica cada 6 h (tp es acumulada desde el inicio, en metros)
    acum = {}
    for paso in range(ini, ini + 72 + 1, 6):
        u = f"{base}/oper/{nombre}-{paso}h-oper-fc"
        rg = _rangos_index(u + ".index", lambda d: d["param"] == "tp")
        if not rg:
            continue
        for _, fila in _valores(_bajar_rangos(u + ".grib2", rg), puntos):
            acum[paso] = fila
    det = {}
    pasos = sorted(acum)
    for a, b in zip(pasos, pasos[1:]):
        fin = corrida + timedelta(hours=b)
        det[fin] = [None if (x is None or y is None) else max(0.0, (y - x) * 1000) for x, y in zip(acum[a], acum[b])]
    inicios = [corrida + timedelta(hours=ini + 24 * k) for k in range(3)]
    return {"corrida": corrida, "ventanas": list(zip(ventanas, inicios)), "prob": prob, "det": det}


# ----------------------------------------------------------------------------
# NOAA GFS via NOMADS (recorte del Peru)
# ----------------------------------------------------------------------------
def fuente_gfs(puntos, ahora_utc):
    ciclo0 = ahora_utc - timedelta(hours=5)          # GFS se publica ~4-5 h despues de cada ciclo
    for k in range(4):
        ciclo = (ciclo0 - timedelta(hours=6 * k)).replace(minute=0, second=0, microsecond=0)
        ciclo = ciclo.replace(hour=ciclo.hour - ciclo.hour % 6)
        det = {}
        for paso in range(6, 90 + 1, 6):
            params = {"dir": f"/gfs.{ciclo:%Y%m%d}/{ciclo:%H}/atmos", "file": f"gfs.t{ciclo:%H}z.pgrb2.0p25.f{paso:03d}",
                      "var_APCP": "on", "lev_surface": "on", "subregion": "", "toplat": 1, "leftlon": 278,
                      "rightlon": 292, "bottomlat": -19}
            r = _get(NOMADS, params=params)
            if r is None or not r.content.startswith(b"GRIB"):
                break
            for claves, fila in _valores(r.content, puntos):
                a, b = (int(x) for x in str(claves["stepRange"]).split("-"))
                if b - a == 6:
                    det[ciclo + timedelta(hours=b)] = fila
            time.sleep(0.3)                                 # NOMADS pide no saturar el servicio
        if len(det) >= 10:
            return {"corrida": ciclo, "det": det}
    raise RuntimeError("GFS: no hay ciclo reciente completo en NOMADS")


# ----------------------------------------------------------------------------
# Interpretacion en lenguaje simple
# ----------------------------------------------------------------------------
def _dia_txt(inicio_lima, ahora_lima):
    d = (inicio_lima.date() - ahora_lima.date()).days
    if d <= 0:
        return "Hoy"
    if d == 1:
        return "Mañana"
    dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    return f"{dias[inicio_lima.weekday()]} {inicio_lima.day}"


def interpretar_ventana(p1, p10, p20, partes_mm, gfs_mm, costa_arida=False):
    """Frase simple para una ventana de 24 h (desde las 7 a. m.) en una instalacion."""
    if p1 is None:
        return {"nivel": -1, "frase": "sin dato"}
    if costa_arida and not (p1 >= 60 and gfs_mm is not None and gfs_mm >= 1):
        # en la costa desertica los modelos confunden la garua con lluvia: solo se avisa si ambos coinciden
        return {"nivel": 0, "frase": "Sin lluvia prevista"}
    if p1 < 30:
        nivel, frase = 0, "Sin lluvia prevista" + (f" ({p1:.0f} %)" if p1 >= 15 else "")
    elif p1 < 60:
        nivel, frase = 1, f"Posible lluvia ({p1:.0f} %)"
    else:
        nivel, frase = 2, f"Lluvia probable ({p1:.0f} %)"
    if nivel >= 1:
        if p20 is not None and p20 >= 30:
            nivel, frase = 3, frase + f", podría ser fuerte (más de 20 mm: {p20:.0f} %)"
        elif p10 is not None and p10 >= 30:
            frase += f", moderada a fuerte (más de 10 mm: {p10:.0f} %)"
        validas = [(mm, nombre) for mm, nombre in partes_mm if mm is not None]
        if validas and max(validas)[0] >= 0.5:
            frase += f", sobre todo en {max(validas)[1]}"
    if gfs_mm is not None:
        if nivel >= 2 and gfs_mm < 0.2:
            frase += ". El modelo de NOAA no prevé lluvia: hay incertidumbre"
        elif nivel == 0 and gfs_mm >= 5:
            frase += f". El modelo de NOAA sí prevé {gfs_mm:.0f} mm: hay incertidumbre"
    return {"nivel": nivel, "frase": frase}


def pronostico_lluvia(puntos, ahora_utc, log=print, aridos=()):
    ec = fuente_ecmwf(puntos, ahora_utc)
    try:
        gfs = fuente_gfs(puntos, ahora_utc)
    except Exception as e:                          # GFS es complementario: si falla, solo ECMWF
        log(f"     GFS no disponible: {e}")
        gfs = {"corrida": None, "det": {}}
    ahora_lima = ahora_utc.astimezone(LIMA)
    res = []
    for i in range(len(puntos)):
        dias = []
        for clave, inicio in ec["ventanas"]:
            pr = ec["prob"].get(clave, {})
            def v(p):
                x = (pr.get(p) or [None] * len(puntos))[i]
                return None if x is None else max(0.0, min(100.0, x))
            partes = []
            for desfase, nombre in PARTES:
                fin = inicio + timedelta(hours=desfase + 6)
                vals = [x[i] for x in (ec["det"].get(fin), gfs["det"].get(fin)) if x is not None and x[i] is not None]
                partes.append((sum(vals) / len(vals) if vals else None, nombre))
            g = [gfs["det"].get(inicio + timedelta(hours=h)) for h in (6, 12, 18, 24)]
            gfs_mm = sum(x[i] for x in g if x and x[i] is not None) if all(x and x[i] is not None for x in g) else None
            ec_mm = sum(mm for mm, _ in partes if mm is not None)
            it = interpretar_ventana(v("tpg1"), v("tpg10"), v("tpg20"), partes, gfs_mm, i in aridos)
            inicio_lima = inicio.astimezone(LIMA)
            dias.append({"dia": _dia_txt(inicio_lima, ahora_lima), "desde": inicio_lima.strftime("%d/%m %H:%M"),
                         "p1": v("tpg1"), "p10": v("tpg10"), "p20": v("tpg20"),
                         "mm_modelos": round(ec_mm, 1), "gfs_mm": None if gfs_mm is None else round(gfs_mm, 1), **it})
        res.append(dias)
    fuentes = {"ecmwf": ec["corrida"].strftime("%d/%m %H UTC"),
               "gfs": gfs["corrida"].strftime("%d/%m %H UTC") if gfs["corrida"] else "no disponible"}
    return res, fuentes


# ----------------------------------------------------------------------------
# Garua en Lima (aeropuerto Jorge Chavez)
# ----------------------------------------------------------------------------
def _rh(t, td):
    a, b = 17.625, 243.04
    return 100 * math.exp(a * td / (b + td)) / math.exp(a * t / (b + t))


def _es_garua(o):
    wx = o.get("wx") or ""
    return "DZ" in wx or "RA" in wx


def garua_lima(obs, modelo, ahora_lima):
    """obs: reportes METAR de SPJC [{ts, temp, dewp, visib, base, wx}]. Devuelve el pronostico para la mañana."""
    if ahora_lima.month not in MESES_GARUA:
        return {"estado": "fuera", "frase": "Fuera de la temporada de garúa (mayo a noviembre)."}
    obs = [dict(o, ts=o["ts"].astimezone(LIMA)) for o in obs]
    h = ahora_lima.hour
    if h < 11:
        objetivo, vispera = ahora_lima.date(), ahora_lima.date() - timedelta(days=1)
    elif h >= 17:
        objetivo, vispera = ahora_lima.date() + timedelta(days=1), ahora_lima.date()
    else:
        return {"estado": "pendiente", "objetivo": (ahora_lima.date() + timedelta(days=1)).isoformat(),
                "frase": "El pronóstico de garúa para mañana temprano se publica en la actualización de las 8 p. m., "
                         "con las observaciones de la tarde."}
    tarde = [o for o in obs if o["ts"].date() == vispera and o["ts"].hour >= 17
             and o.get("temp") is not None and o.get("dewp") is not None]
    dia_ant = [o for o in obs if o["ts"].date() == vispera]
    if len(tarde) < 3:
        return {"estado": "sin_dato", "frase": "Sin suficientes reportes del aeropuerto para estimar la garúa."}
    x = {"rh": sum(_rh(o["temp"], o["dewp"]) for o in tarde) / len(tarde),
         "spread": sum(o["temp"] - o["dewp"] for o in tarde) / len(tarde),
         "techo": min(min((o["base"] for o in tarde if o.get("base") is not None), default=3000), 3000) / 1000,
         "dz_tarde": float(any(_es_garua(o) for o in tarde)),
         "vis": min(o["visib"] for o in tarde if o.get("visib") is not None) if any(o.get("visib") is not None for o in tarde) else 6.21,
         "dz_ayer": float(any(_es_garua(o) for o in dia_ant))}
    z = modelo["intercepto"] + sum(modelo["coef"][k] * (x[k] - modelo["media"][k]) / modelo["desv"][k]
                                   for k in modelo["variables"])
    p = 1 / (1 + math.exp(-z))
    rango, frecuencia = next((nom, fr) for a, b, nom, fr in modelo["rangos"] if a <= p < b)
    cuando = "hoy temprano" if objetivo == ahora_lima.date() else "mañana temprano"
    veces = {"Poco probable": "1 de cada 10", "Posible": "3 de cada 10", "Probable": "1 de cada 2"}[rango]
    return {"estado": "ok", "objetivo": objetivo.isoformat(), "prob": round(p, 3), "rango": rango,
            "frecuencia": frecuencia, "cuando": cuando,
            "frase": f"Garúa {cuando}: {rango.lower()}. En mañanas con condiciones así garuó {veces} veces "
                     f"(registro 2023-2025 del aeropuerto Jorge Chávez)."}


def garua_ahora(obs, ahora_lima):
    """Garua observada en las ultimas 3 h en el aeropuerto."""
    rec = [o for o in obs if (ahora_lima - o["ts"].astimezone(LIMA)).total_seconds() <= 3 * 3600]
    con = [o for o in rec if _es_garua(o)]
    if con:
        u = max(con, key=lambda o: o["ts"])
        tipo = "lluvia" if "RA" in (u.get("wx") or "") else "garúa"
        return {"hay": True, "frase": f"Se observó {tipo} en el aeropuerto Jorge Chávez a las "
                                      f"{u['ts'].astimezone(LIMA):%H:%M}."}
    if rec:
        return {"hay": False, "frase": "Sin garúa en el aeropuerto Jorge Chávez en las últimas 3 horas."}
    return {"hay": None, "frase": ""}


def registrar_garua(ruta: Path, g, obs, ahora_lima):
    """Guarda el pronostico de garua y completa lo observado para medir los aciertos."""
    filas = {}
    if ruta.exists():
        with open(ruta, encoding="utf-8") as fh:
            for f in csv.DictReader(fh):
                filas[f["objetivo"]] = f
    if g and g.get("estado") == "ok":
        f = filas.get(g["objetivo"], {})
        if not f.get("observado"):                         # solo se actualiza antes de conocer el resultado
            filas[g["objetivo"]] = {"objetivo": g["objetivo"], "prob": g["prob"], "rango": g["rango"],
                                    "calculado": ahora_lima.strftime("%Y-%m-%d %H:%M"), "observado": ""}
    for k, f in filas.items():                            # completar lo observado (00-11 h del dia objetivo)
        dia = datetime.fromisoformat(k).date()
        if f.get("observado") == "" and (ahora_lima.date() > dia or ahora_lima.hour >= 12):
            manana = [o for o in obs if o["ts"].astimezone(LIMA).date() == dia and o["ts"].astimezone(LIMA).hour < 12]
            if len(manana) >= 8:
                f["observado"] = "si" if any(_es_garua(o) for o in manana) else "no"
    ruta.parent.mkdir(exist_ok=True)
    with open(ruta, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, ["objetivo", "prob", "rango", "calculado", "observado"])
        w.writeheader()
        for k in sorted(filas):
            w.writerow({c: filas[k].get(c, "") for c in w.fieldnames})
    return aciertos_garua(list(filas.values()))


def aciertos_garua(filas, dias=30):
    hechas = [f for f in filas if f.get("observado") in ("si", "no")]
    hechas = sorted(hechas, key=lambda f: f["objetivo"])[-dias:]
    if not hechas:
        return None
    res = {"n": len(hechas), "por_rango": {}}
    for r in ("Poco probable", "Posible", "Probable"):
        xs = [f for f in hechas if f["rango"] == r]
        res["por_rango"][r] = {"mananas": len(xs), "garuo": sum(f["observado"] == "si" for f in xs)}
    return res


# ----------------------------------------------------------------------------
# Punto de entrada: una actualizacion por franja de 4 horas
# ----------------------------------------------------------------------------
def franja(ahora_lima):
    return ahora_lima.replace(hour=ahora_lima.hour - ahora_lima.hour % HORAS_FRANJA, minute=0, second=0,
                              microsecond=0).strftime("%Y-%m-%d %H:%M")


def obtener(puntos, obs_spjc, carpeta: Path, log=print, forzar=False, aridos=()):
    """puntos: [(cod, lat, lon)]. Devuelve {'lluvia': {cod: [...]}, 'garua': {...}, 'garua_ahora': {...}, ...}."""
    ahora_lima = datetime.now(LIMA)
    ruta_cache = carpeta / "cache" / "pronostico.json"
    cache = {}
    if ruta_cache.exists():
        try:
            cache = json.loads(ruta_cache.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    modelo = json.loads((carpeta / "modelo_garua.json").read_text(encoding="utf-8"))
    actual = franja(ahora_lima)
    res = dict(cache)
    if forzar or cache.get("franja") != actual or not cache.get("lluvia"):
        log(f"     Pronóstico: nueva franja {actual}, se consultan ECMWF y GFS")
        try:
            if eccodes is None:
                raise RuntimeError("falta la librería eccodes")
            lluvia, fuentes = pronostico_lluvia([(la, lo) for _, la, lo in puntos], ahora_lima.astimezone(timezone.utc), log,
                                                {i for i, (cod, _, _) in enumerate(puntos) if cod in aridos})
            res["lluvia"] = {cod: d for (cod, _, _), d in zip(puntos, lluvia)}
            res["fuentes"] = fuentes
            res["error_lluvia"] = ""
        except Exception as e:
            log(f"     Pronóstico de lluvia no disponible: {e}")
            res["error_lluvia"] = str(e)[:200]          # se mantiene el ultimo pronostico guardado
        res["garua"] = garua_lima(obs_spjc, modelo, ahora_lima) if obs_spjc else \
            {"estado": "sin_dato", "frase": "Sin reportes del aeropuerto Jorge Chávez."}
        res["franja"], res["calculado"] = actual, ahora_lima.strftime("%d/%m %H:%M")
        ruta_cache.parent.mkdir(exist_ok=True)
        ruta_cache.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        log(f"     Pronóstico: se reutiliza el de la franja {actual} (calculado {cache.get('calculado')})")
    res["garua_ahora"] = garua_ahora(obs_spjc, ahora_lima) if obs_spjc else {"hay": None, "frase": ""}
    res["aciertos_garua"] = registrar_garua(carpeta / "historial" / "garua_pronostico.csv", res.get("garua"),
                                            obs_spjc, ahora_lima)
    res["proxima"] = (datetime.strptime(actual, "%Y-%m-%d %H:%M") + timedelta(hours=HORAS_FRANJA)).strftime("%H:%M")
    return res
