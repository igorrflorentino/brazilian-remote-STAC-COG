"""What changed upstream since the map was built. Read-only: nothing is written, uploaded or deployed.

Four sections, meant for a monthly run:

1. Scenes. For every cell in scenes_acre.txt, the fused scenes INPE has published since the one in
   use, measured on the overview of INPE's original (cloud by the rule of scene_stats.py, and the
   dark level against TYPICAL_DARK, which flags haze and smoke). A newer scene is an upgrade when it
   is clearer than the one in use. The 5 cells built from unfused bands (scenes_pansharpened.txt)
   have no fused scene yet: any fused scene that is clear enough is an upgrade for them. A candidate
   still has to pass `check_bands.py` and be rendered with its own contrast before it is adopted.
2. Snapshots. How many DETER alerts, fire hotspots and CAR properties exist upstream since the date
   each layer was built (snapshots.txt), and the latest PRODES year.
3. Live layers. Whether the INPE (fires, DETER) and MapBiomas WMS layers of the Atlas map still
   answer, the bounding box they declare (Atlas never asks for tiles outside it) and, for DETER,
   the newest month of its time dimension.
4. The service, if --service-url (or $SERVICE_URL) is given: /health.

Usage: python check_updates.py [--service-url URL] [--no-quality] [--json FILE]
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import HERE, cog_url  # noqa: E402
from scene_stats import CLOUD_ABOVE_GROUND, CLOUD_MIN, TYPICAL_DARK  # noqa: E402

STAC_SEARCH = "https://data.inpe.br/bdc/stac/v1/search"
FUSED = "CB4A-WPM-PCA-FUSED-1"
ACRE_BBOX = [-74.0, -11.2, -66.6, -7.1]
ITEM_RE = re.compile(r"_(?P<date>\d{8})_(?P<path>\d{3})_(?P<row>\d{3})$")
READ_PX = 700  # the overview is read at about this width
CLEAR_ENOUGH = {"cloud": 10.0, "haze": 15.0}  # for a cell that has no fused scene yet
BETTER_BY = 1.0  # score points a newer scene must gain to be worth a look
SNAPSHOTS_TXT = HERE / "snapshots.txt"

TB = "https://terrabrasilis.dpi.inpe.br/geoserver/ows"
INPE_FOCOS = "https://terrabrasilis.dpi.inpe.br/queimadas/geoserver/ows"
SICAR = "https://geoserver.car.gov.br/geoserver/sicar/ows"
LIVE_WMS = {
    "INPE focos, mês atual": "https://terrabrasilis.dpi.inpe.br/queimadas/geoserver/dados_abertos/focos_mesatual_br_satref/wms",
    "INPE DETER, histórico": "https://terrabrasilis.dpi.inpe.br/geoserver/deter-amz/wms",
    "MapBiomas alertas": "https://maps.alerta.mapbiomas.org/geoserver/mapbiomas-alertas/dashboard-alerts/wms",
    "MapBiomas imóveis com alerta": "https://maps.alerta.mapbiomas.org/geoserver/mapbiomas-alertas/mv_rural_properties_with_alerts_published/wms",
}


# ---- pure helpers (tested offline)

def parse_item_id(item_id: str) -> tuple[str, str] | None:
    """('YYYYMMDD', 'path_row') of a STAC item id such as CBERS4A_WPM_PCA_RGB321_20250806_233_126."""
    m = ITEM_RE.search(item_id)
    return (m["date"], f"{m['path']}_{m['row']}") if m else None


def cell_of(scene: str) -> str:
    return scene.split("_", 1)[1]


def date_of(scene: str) -> str:
    return scene.split("_", 1)[0]


def last_time(capabilities: str) -> str | None:
    """Latest instant of the layer's `time` dimension in a WMS capabilities document, as YYYY-MM-DD."""
    m = re.search(r'<Dimension[^>]*name="time"[^>]*>([^<]+)</Dimension>', capabilities)
    return m[1].split(",")[-1].strip()[:10] if m else None


def cloud_and_haze(a: np.ndarray) -> dict:
    """Cloud percentage and haze (levels above the typical dark level) of a 3 x H x W preview, 0 = no data."""
    valid = (a != 0).any(axis=0)
    if not valid.any():
        return {"cloud": 100.0, "haze": 0.0, "valid": 0.0}
    darkest = a.min(axis=0)
    cloud = valid & (darkest > max(CLOUD_MIN, np.median(darkest[valid]) + CLOUD_ABOVE_GROUND))
    clear = valid & ~cloud
    if not clear.any():
        return {"cloud": 100.0, "haze": 0.0, "valid": round(100 * valid.mean(), 1)}
    dark = np.percentile(a[:, clear], 5, axis=1)
    return {
        "cloud": round(100 * cloud.sum() / valid.sum(), 1),
        "haze": round(float(np.mean(dark - np.array(TYPICAL_DARK))), 1),
        "valid": round(100 * valid.mean(), 1),
    }


def score(m: dict) -> float:
    """Lower is better: cloud percentage plus half a point per level of haze above the typical dark level."""
    return m["cloud"] + 0.5 * max(0.0, m["haze"])


def is_upgrade(candidate: dict, current: dict | None) -> bool:
    """A newer scene is worth a look if it is clearer than the one in use; for a cell with none, if it is clear enough."""
    if current is None:
        return candidate["cloud"] <= CLEAR_ENOUGH["cloud"] and candidate["haze"] <= CLEAR_ENOUGH["haze"]
    return score(candidate) + BETTER_BY < score(current)


def read_snapshots(path: Path = SNAPSHOTS_TXT) -> dict[str, str]:
    """'name value' lines of snapshots.txt (comments with #) as a dict of strings."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, value = line.split(None, 1)
            out[name] = value.strip()
    return out


# ---- network

def get(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 120, tries: int = 3) -> bytes:
    """GET (POST with data), retrying server errors and network blips: INPE's servers answer 5xx now and then."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "titiler-acre-check/1.0", **(headers or {})})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def curl(url: str, timeout: int = 120) -> bytes:
    """SICAR's server fails the TLS handshake with Python's OpenSSL 3, so it goes through curl."""
    return subprocess.run(["curl", "-sSf", "-m", str(timeout), url], capture_output=True, check=True).stdout


def fused_since(first_date: str) -> dict[str, list[str]]:
    """{cell: [YYYYMMDD, ...]} of the fused items over Acre from first_date on (STAC search, paged)."""
    body = {"collections": [FUSED], "bbox": ACRE_BBOX, "limit": 200,
            "datetime": f"{first_date[:4]}-{first_date[4:6]}-{first_date[6:]}T00:00:00Z/{dt.date.today().year + 1}-12-31T23:59:59Z"}
    found: dict[str, set[str]] = {}
    for _ in range(40):
        page = json.loads(get(STAC_SEARCH, json.dumps(body).encode(), {"Content-Type": "application/json"}))
        for f in page["features"]:
            parsed = parse_item_id(f["id"])
            if parsed:
                found.setdefault(parsed[1], set()).add(parsed[0])
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not nxt or not nxt.get("body"):
            break
        body = {**body, **nxt["body"]}
    return {cell: sorted(dates) for cell, dates in found.items()}


def measure(scene: str) -> dict | None:
    """cloud_and_haze of INPE's original of a fused scene, read from its overviews; None if it cannot be read."""
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_MAX_RETRY=4), \
                rasterio.open(cog_url(scene)) as src:
            k = max(1, src.width // READ_PX)
            a = src.read(out_shape=(3, src.height // k, src.width // k), resampling=Resampling.nearest)
        return cloud_and_haze(a)
    except Exception as e:  # a scene that is not there yet, a blip
        print(f"  could not measure {scene}: {type(e).__name__}", file=sys.stderr)
        return None


def hits(url: str, fetch=get) -> int | None:
    """numberMatched of a WFS resultType=hits request."""
    m = re.search(rb'numberMatched="(\d+)"', fetch(url))
    return int(m.group(1)) if m else None


def wfs(base: str, layer: str, cql: str, **extra) -> str:
    q = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": layer, "CQL_FILTER": cql, **extra}
    return f"{base}?{urllib.parse.urlencode(q)}"


def latest(base: str, layer: str, cql: str, field: str, fetch=get) -> str | None:
    q = wfs(base, layer, cql, count=1, sortBy=f"{field} D", outputFormat="application/json")
    feats = json.loads(fetch(q))["features"]
    return str(feats[0]["properties"][field])[:10] if feats else None


# ---- sections

def scenes_section(measure_quality: bool) -> tuple[list[str], list[dict]]:
    in_use = (HERE / "scenes_acre.txt").read_text(encoding="utf-8").split()
    pansharpened = set((HERE / "scenes_pansharpened.txt").read_text(encoding="utf-8").split())
    cells = {cell_of(s): s for s in in_use}
    first = min(date_of(s) for s in in_use if s not in pansharpened)
    published = fused_since(first)
    lines, upgrades = [], []
    for cell, scene in sorted(cells.items()):
        pan = scene in pansharpened
        newer = [d for d in published.get(cell, []) if pan or d > date_of(scene)]
        if not newer:
            continue
        if not measure_quality:
            lines.append(f"  {cell}: {len(newer)} newer fused scene(s), latest {newer[-1]} (in use {date_of(scene)}{', pansharpened' if pan else ''})")
            continue
        current = None if pan else measure(scene)
        measured = [(d, measure(f"{d}_{cell}")) for d in newer]
        for d, m in measured:
            if m and is_upgrade(m, current):
                upgrades.append({"cell": cell, "in_use": scene, "candidate": f"{d}_{cell}", "pansharpened": pan, **m})
        better = [(d, m) for d, m in measured if m and is_upgrade(m, current)]
        if better:
            best = min(better, key=lambda x: score(x[1]))
            cur = "no fused scene yet" if current is None else f"{date_of(scene)}: cloud {current['cloud']} %, haze {current['haze']:+}"
            lines.append(f"  {cell}: UPGRADE {best[0]} (cloud {best[1]['cloud']} %, haze {best[1]['haze']:+}, {len(better)} of {len(newer)} newer ones qualify); in use {cur}")
        else:
            lines.append(f"  {cell}: {len(newer)} newer scene(s), none clearer than {date_of(scene)}")
    return lines or ["  every cell is up to date"], upgrades


def snapshots_section() -> list[str]:
    snap = read_snapshots()
    out = []
    try:
        built = snap["deter"]
        n = hits(wfs(TB, "deter-amz:deter_amz", f"uf='AC' AND view_date>'{built}'", resultType="hits"))
        last = latest(TB, "deter-amz:deter_amz", "uf='AC'", "view_date")
        out.append(f"  DETER: {n} new alert(s) in Acre since the layer was built on {built}; latest {last}")
    except Exception as e:
        out.append(f"  DETER: could not check ({e})")
    try:
        built = snap["focos"]
        base = "estado='ACRE' AND data_hora_gmt>'%sT00:00:00Z'" % built
        n = hits(wfs(INPE_FOCOS, "dados_abertos:focos_ano_atual_br_satref", base, resultType="hits"))
        out.append(f"  Focos (12-month snapshot): {n} new hotspot(s) since {built}")
    except Exception as e:
        out.append(f"  Focos: could not check ({e})")
    try:
        n = hits(wfs(SICAR, "sicar:sicar_imoveis_ac", "1=1", resultType="hits"), fetch=lambda u: curl(u))
        was = int(snap.get("car_count", 0))
        out.append(f"  CAR: {n} properties in Acre now, {was} when the layer was built on {snap.get('car')} ({n - was:+})")
    except Exception as e:
        out.append(f"  CAR: could not check ({e})")
    try:
        years = latest(TB, "prodes-legal-amz:yearly_deforestation", "state='AC'", "year")
        out.append(f"  PRODES: latest year upstream {years}, layer has through {snap.get('prodes')}")
    except Exception as e:
        out.append(f"  PRODES: could not check ({e})")
    age = (dt.date.today() - dt.date.fromisoformat(snap["deter"])).days
    out.append(f"  the snapshots are {age} day(s) old; rebuild them with hybrid_layers.js (DETER, PRODES) and fundiario_layers.py (CAR, embargoes, hotspots, settlements)")
    return out


def live_section() -> list[str]:
    out = []
    for name, base in LIVE_WMS.items():
        try:
            x = get(f"{base}?service=WMS&version=1.3.0&request=GetCapabilities", timeout=90).decode("utf-8", "ignore")
            west = re.search(r"<westBoundLongitude>([-\d.]+)</westBoundLongitude>", x)
            newest = last_time(x)
            out.append(f"  {name}: answers" + (f" (declared west bound {float(west[1]):.2f}°)" if west else "") + (f", time dimension up to {newest}" if newest else ""))
        except Exception as e:
            out.append(f"  {name}: DOES NOT ANSWER ({type(e).__name__})")
    return out


def service_section(url: str) -> list[str]:
    try:
        status = json.loads(get(url.rstrip("/") + "/health", timeout=60))
        return [f"  /health: {status}"]
    except Exception as e:
        return [f"  /health: FAILED ({type(e).__name__})"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service-url", default=os.environ.get("SERVICE_URL"), help="also check the service's /health")
    parser.add_argument("--no-quality", action="store_true", help="list newer scenes without measuring them (fast)")
    parser.add_argument("--json", type=Path, help="write the upgrades as JSON")
    args = parser.parse_args()

    print(f"Update check of {dt.date.today()}\n")
    print("1. Scenes")
    lines, upgrades = scenes_section(not args.no_quality)
    print("\n".join(lines))
    if upgrades:
        print("  Before adopting one: `python pipeline/check_bands.py <scene>`, render it with its own contrast, then process_all.py.")
    print("\n2. Snapshots")
    print("\n".join(snapshots_section()))
    print("\n3. Live layers")
    print("\n".join(live_section()))
    if args.service_url:
        print("\n4. Service")
        print("\n".join(service_section(args.service_url)))
    if args.json:
        args.json.write_text(json.dumps(upgrades, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
