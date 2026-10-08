"""
Climatologia de lluvia por instalacion ("lo normal") -> clima_normal.json

Se ejecuta a mano una vez al ano (no en cada corrida del monitor).

Para cada instalacion de zonas_tiendas.csv y cada mes calcula, con la lluvia diaria 1991-2020
(periodo normal OMM):
  - media_dia      lluvia promedio por dia (mm)
  - normal_mes     lluvia total promedio del mes (mm)
  - frec_lluvia    % de dias del mes con lluvia >= 1 mm
  - p90/p95/p99    percentiles de los dias con lluvia (>= 1 mm). Son los cortes que usa SENAMHI:
                   > p90 moderadamente lluvioso, > p95 muy lluvioso, > p99 extremadamente lluvioso.

Fuente PROVISIONAL: NASA POWER (PRECTOTCORR, grilla ~50 km).
Cuando se tenga acceso a PISCO (SENAMHI), reemplazar `serie_diaria()` y FUENTE.

Uso:  python climatologia_lluvia.py
"""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

CARPETA = Path(__file__).resolve().parent
ARCH_TIENDAS = CARPETA / "zonas_tiendas.csv"
ARCH_SALIDA = CARPETA / "clima_normal.json"

FUENTE = "NASA POWER 1991-2020 (provisional, se reemplazará por PISCO de SENAMHI)"
NASA_POWER = "https://power.larc.nasa.gov/api/temporal/daily/point"
INICIO, FIN = "19910101", "20201231"

DIA_LLUVIOSO_MM = 1.0          # umbral de dia con lluvia
MIN_DIAS_LLUVIOSOS = 30        # minimo de dias lluviosos para calcular percentiles del mes solo
# Pisos minimos (mm/dia) para costa arida, donde los percentiles salen ~0
PISO = {"p90": 1.0, "p95": 3.0, "p99": 8.0}


def serie_diaria(lat: float, lon: float) -> pd.Series:
    """Lluvia diaria (mm) 1991-2020 en el punto."""
    for intento in range(3):
        try:
            r = requests.get(NASA_POWER, timeout=120, params={
                "parameters": "PRECTOTCORR", "community": "AG", "format": "JSON",
                "latitude": round(lat, 4), "longitude": round(lon, 4), "start": INICIO, "end": FIN})
            r.raise_for_status()
            d = r.json()["properties"]["parameter"]["PRECTOTCORR"]
            s = pd.Series(d, dtype=float)
            s.index = pd.to_datetime(s.index, format="%Y%m%d")
            return s.where(s >= 0)          # -999 = sin dato
        except Exception as ex:             # noqa: BLE001
            print(f"   reintento {intento + 1}: {ex}")
            time.sleep(5)
    raise RuntimeError(f"No se pudo descargar la serie de ({lat}, {lon})")


def estadisticos(s: pd.Series) -> dict:
    s = s.dropna()
    anios = s.index.year.nunique()
    meses = {}
    for m in range(1, 13):
        sm = s[s.index.month == m]
        lluviosos = sm[sm >= DIA_LLUVIOSO_MM]
        # Si el mes tiene pocos dias lluviosos (costa), se usan tambien el mes anterior y el siguiente
        base = lluviosos
        if len(base) < MIN_DIAS_LLUVIOSOS:
            vecinos = {m, (m - 2) % 12 + 1, m % 12 + 1}
            sv = s[s.index.month.isin(vecinos)]
            base = sv[sv >= DIA_LLUVIOSO_MM]
        pct = {k: float(np.percentile(base, q)) if len(base) >= 5 else 0.0
               for k, q in (("p90", 90), ("p95", 95), ("p99", 99))}
        pct = {k: round(max(v, PISO[k]), 1) for k, v in pct.items()}
        # asegurar orden creciente tras los pisos
        pct["p95"] = max(pct["p95"], pct["p90"])
        pct["p99"] = max(pct["p99"], pct["p95"])
        meses[str(m)] = {
            "media_dia": round(float(sm.mean()), 2),
            "normal_mes": round(float(sm.sum() / anios), 1),
            "frec_lluvia": round(100 * len(lluviosos) / max(len(sm), 1), 1),
            "dias_lluviosos_base": int(len(base)),
            **pct,
        }
    return meses


def main():
    t = pd.read_csv(ARCH_TIENDAS, dtype={"cod_p": str})
    # NASA POWER tiene grilla de 0.5 x 0.625 grados: tiendas en la misma celda comparten serie
    t["celda"] = t["lat"].apply(lambda v: round(v * 2) / 2).astype(str) + "," + \
        t["lon"].apply(lambda v: round(v / 0.625) * 0.625).astype(str)
    cache, salida = {}, {}
    for _, f in t.iterrows():
        if f.celda not in cache:
            print(f"Descargando {f.tienda} ({f.lat}, {f.lon})")
            cache[f.celda] = estadisticos(serie_diaria(f.lat, f.lon))
        salida[f.cod_p] = {"tienda": f.tienda, "ciudad": f.ciudad, "meses": cache[f.celda]}
    datos = {"fuente": FUENTE, "periodo": "1991-2020", "dia_lluvioso_mm": DIA_LLUVIOSO_MM,
             "generado": pd.Timestamp.now(tz="America/Lima").strftime("%Y-%m-%d %H:%M"),
             "tiendas": salida}
    ARCH_SALIDA.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK: {ARCH_SALIDA.name} con {len(salida)} instalaciones ({len(cache)} series descargadas)")


if __name__ == "__main__":
    main()
