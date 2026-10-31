"""Build the land-tenure and enforcement layers of Acre as GeoJSON for the Atlas map.

Four layers, written to <OUT_DIR>/<key>.geojson (hybrid_layers.js uploads them):

  car           CAR rural properties (SICAR's public WFS, sicar:sicar_imoveis_ac)
  assentamentos INCRA settlements, as published by INPE's BDQueimadas geoserver
  embargos      IBAMA embargoed areas (open-data CSV of all Brazil, UF = AC)
  focos         INPE fire hotspots of the last 12 months (reference satellite)

Geometries are simplified with Douglas-Peucker and rounded to 5 decimals, like
the other hybrid layers. Only non-personal attributes are kept: the IBAMA CSV
also lists the name and CPF/CNPJ of the embargoed person, and neither it nor the
infraction or process numbers are copied. The CAR layer carries every status
(active, pending, suspended, cancelled): it is self-declared data, so the
properties overlap and the cancelled ones are only flagged.

Usage: python fundiario_layers.py <OUT_DIR> [--only car,embargos,...] [--ibama-csv FILE]
Without --ibama-csv the national CSV (about 170 MB) is downloaded into OUT_DIR.
Requests go through curl: SICAR's server fails the TLS handshake with Python's
OpenSSL 3, and curl does not.
"""

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from shapely import wkt
from shapely.geometry import mapping, shape

SICAR = "https://geoserver.car.gov.br/geoserver/sicar/ows"
INPE = "https://terrabrasilis.dpi.inpe.br/queimadas/geoserver/ows"
IBAMA_CSV = "https://dadosabertos.ibama.gov.br/dados/SIFISC/termo_embargo/termo_embargo/termo_embargo.csv"
PAGE = 1000  # SICAR's WFS answers at most 10,000 features a request
PAUSE_S = 0.5  # between requests, to stay gentle with the public services
CAR_STATUS = {"AT": "Ativo", "PE": "Pendente", "SU": "Suspenso", "CA": "Cancelado"}
CAR_TIPO = {"IRU": "Imóvel rural", "AST": "Assentamento", "PCT": "Povos e comunidades tradicionais"}


def get_json(url: str, params: dict, tries: int = 4) -> dict:
    """GET url?params as JSON through curl, retrying on network errors and server errors."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(tries):
        try:
            return json.loads(subprocess.run(["curl", "-sSf", "-m", "180", full], capture_output=True, check=True).stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            if attempt == tries - 1:
                raise
            wait = 5 * 2**attempt
            print(f"  retry {attempt + 1} in {wait}s: {e}", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def wfs(url: str, layer: str, **extra) -> dict:
    return get_json(url, {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeName": layer,
                          "outputFormat": "application/json", "srsName": "EPSG:4326", **extra})


def clean(geometry, tol: float):
    """Simplified geometry as a GeoJSON dict with 5-decimal coordinates, or None if it vanishes."""
    g = geometry if hasattr(geometry, "simplify") else shape(geometry)
    g = g.simplify(tol, preserve_topology=True)
    if g.is_empty:
        return None

    def ring(r):
        out = [[round(x, 5), round(y, 5)] for x, y, *_ in r]
        if out[0] != out[-1]:
            out.append(out[0])
        return out if len(out) >= 4 else None

    def poly(p):
        rings = [ring(r) for r in p]
        return [r for r in rings if r] if rings[0] else None

    m = mapping(g)
    if m["type"] == "Point":
        return {"type": "Point", "coordinates": [round(m["coordinates"][0], 5), round(m["coordinates"][1], 5)]}
    if m["type"] == "Polygon":
        c = poly(m["coordinates"])
        return {"type": "Polygon", "coordinates": c} if c else None
    if m["type"] == "MultiPolygon":
        c = [p for p in (poly(p) for p in m["coordinates"]) if p]
        return {"type": "MultiPolygon", "coordinates": c} if c else None
    return None


def feature(props: dict, geometry) -> dict:
    return {"type": "Feature", "properties": props, "geometry": geometry}


def collection(feats: list) -> dict:
    return {"type": "FeatureCollection", "features": [f for f in feats if f["geometry"]]}


def date_br(iso: str | None) -> str | None:
    """'2026-10-29T17:34:00Z' -> '29/10/2026'; None for a missing or absurd date."""
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "").replace(" ", "T")[:19])
    except ValueError:
        return None
    return d.strftime("%d/%m/%Y") if 1980 <= d.year <= dt.date.today().year + 1 else None


def build_car() -> dict:
    feats, start = [], 0
    while True:
        page = wfs(SICAR, "sicar:sicar_imoveis_ac", count=PAGE, startIndex=start, sortBy="cod_imovel")["features"]
        for f in page:
            p = f["properties"]
            feats.append(feature({
                "codigo": p["cod_imovel"],
                "situacao": CAR_STATUS.get(p["status_imovel"], p["status_imovel"]),
                "condicao": p["condicao"],
                "tipo": CAR_TIPO.get(p["tipo_imovel"], p["tipo_imovel"]),
                "area_ha": round(p["area"], 2) if p["area"] is not None else None,
                "municipio": p["municipio"],
                "modulos_fiscais": round(p["m_fiscal"], 2) if p["m_fiscal"] is not None else None,
                "atualizado": date_br(p["data_atualizacao"]),
            }, clean(f["geometry"], 0.00005)))
        print(f"  car: {len(feats)} imóveis", flush=True)
        if len(page) < PAGE:
            return collection(feats)
        start += PAGE
        time.sleep(PAUSE_S)


def build_assentamentos() -> dict:
    fs = wfs(INPE, "bdqueimadas:assentamentos", CQL_FILTER="uf='ACRE'")["features"]  # bbox and CQL together give a 500
    return collection([feature({
        "nome": p["nome"], "municipio": p["municipio"], "area_ha": round(float(p["area_hec"]), 1) if p["area_hec"] else None,
        "familias": p["numero_familia"], "capacidade": p["capacidade"], "situacao": p["descricao"],
        "obtencao": p["obtencao"], "data_obtencao": p["data_obtencao"], "sipra": p["sipra"],
    }, clean(f["geometry"], 0.0001)) for f in fs for p in [f["properties"]]])


def build_embargos(csv_path: Path) -> dict:
    csv.field_size_limit(10**8)
    feats, no_polygon = [], 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if row["UF"] != "AC":
                continue
            if not row["GEOM_AREA_EMBARGADA"].strip():
                no_polygon += 1
                continue
            if row["SIT_CANCELADO"] == "S":
                situacao = "Cancelado"
            elif row["SIT_DESEMBARGO"] == "S" or row["DAT_DESEMBARGO"]:
                situacao = "Desembargado"
            else:
                situacao = "Embargado"
            area = row["QTD_AREA_EMBARGADA"].replace(",", ".")
            feats.append(feature({
                "situacao": situacao, "data": date_br(row["DAT_EMBARGO"]), "municipio": row["MUNICIPIO"],
                "area_ha": round(float(area), 2) if area else None, "tipo_area": row["TIPO_AREA"] or None,
                "deter_prodes": row["DETER_PRODES"] or None,
            }, clean(wkt.loads(row["GEOM_AREA_EMBARGADA"]), 0.00005)))
    print(f"  embargos: {len(feats)} com polígono, {no_polygon} só com ponto (fora do layer)", flush=True)
    return collection(feats)


def build_focos() -> dict:
    today = dt.date.today()
    since = today - dt.timedelta(days=365)
    layers = ["focos_ano_atual_br_satref"]
    if since.year < today.year:
        layers.insert(0, f"focos_{since.year}_br_satref")
    cql = f"estado='ACRE' AND data_hora_gmt >= '{since.isoformat()}T00:00:00Z'"
    feats = []
    for layer in layers:
        for f in wfs(INPE, f"dados_abertos:{layer}", CQL_FILTER=cql)["features"]:
            p = f["properties"]
            when = p["data_hora_gmt"] or ""
            feats.append(feature({
                "data": date_br(when), "hora_utc": when[11:16] or None, "satelite": p["satelite"], "municipio": p["municipio"],
                "frp": round(p["frp"], 1) if p["frp"] is not None else None,
                "risco_fogo": round(p["risco_fogo"], 2) if p["risco_fogo"] is not None else None,
                "dias_sem_chuva": p["numero_dias_sem_chuva"],
            }, {"type": "Point", "coordinates": [round(p["longitude"], 5), round(p["latitude"], 5)]}))
        time.sleep(PAUSE_S)
    return collection(feats)


def download(url: str, dest: Path) -> None:
    print(f"  downloading {url} -> {dest}", flush=True)
    subprocess.run(["curl", "-sSf", "-m", "1800", "-o", str(dest), url], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--only", default="car,assentamentos,embargos,focos", help="comma-separated layer keys")
    parser.add_argument("--ibama-csv", type=Path, help="national termo_embargo.csv, if already downloaded")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    keys = [k.strip() for k in args.only.split(",") if k.strip()]

    for key in keys:
        print(key, flush=True)
        if key == "car":
            data = build_car()
        elif key == "assentamentos":
            data = build_assentamentos()
        elif key == "embargos":
            csv_path = args.ibama_csv or args.out_dir / "termo_embargo.csv"
            if not csv_path.exists():
                download(IBAMA_CSV, csv_path)
            data = build_embargos(csv_path)
        elif key == "focos":
            data = build_focos()
        else:
            sys.exit(f"Unknown layer {key!r}")
        out = args.out_dir / f"{key}.geojson"
        out.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        print(f"  {key}: {len(data['features'])} features, {out.stat().st_size / 1e6:.1f} MB -> {out}", flush=True)


if __name__ == "__main__":
    main()
