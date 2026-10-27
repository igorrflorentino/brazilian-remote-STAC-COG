"""Report which scenes have valid pixels inside the state of Acre (IBGE code 12).

The STAC footprint of each scene is only its bounding box, so the valid-data
footprint is vectorised from the COG's smallest overview mask (a few HTTP
range requests per scene) and intersected with the IBGE boundary in the
scene's UTM CRS.

Usage: python acre_scenes.py <SCENES_TXT> <OUT_CSV>
"""

import argparse
import csv
import gzip
import json
import sys
import time
import urllib.request
from pathlib import Path

import rasterio
from rasterio.features import shapes
from rasterio.warp import transform_geom
from shapely.geometry import shape
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import cog_url  # noqa: E402

IBGE_ACRE = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/estados/12"
    "?formato=application/vnd.geo+json&qualidade=maxima"
)
IBGE_CRS = "EPSG:4674"  # SIRGAS 2000
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": 8,
    "GDAL_HTTP_RETRY_DELAY": 5,
}
PAUSE_S = 2  # between scenes, to stay well below INPE's rate limit


def acre_geometry() -> dict:
    with urllib.request.urlopen(IBGE_ACRE, timeout=60) as r:
        body = r.read()
    if body[:2] == b"\x1f\x8b":  # IBGE gzips the body even when not asked to
        body = gzip.decompress(body)
    fc = json.loads(body)
    return unary_union([shape(f["geometry"]) for f in fc["features"]]).__geo_interface__


def valid_footprint(url: str):
    """Valid-data polygon (scene CRS) from the smallest overview mask."""
    with rasterio.open(url) as ds:
        level = len(ds.overviews(1)) - 1
    with rasterio.open(url, overview_level=level) as ds:
        mask = ds.dataset_mask()
        polys = [shape(g) for g, _ in shapes(mask, mask=mask > 0, transform=ds.transform)]
        return unary_union(polys).buffer(0), ds.crs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenes", type=Path)
    parser.add_argument("out_csv", type=Path)
    args = parser.parse_args()

    scenes = [s.strip() for s in args.scenes.read_text().splitlines() if s.strip()]
    acre = acre_geometry()
    rows = []
    with rasterio.Env(**GDAL_ENV):
        for scene in scenes:
            footprint, crs = valid_footprint(cog_url(scene))
            acre_local = shape(transform_geom(IBGE_CRS, crs, acre))
            inside = footprint.intersection(acre_local).area
            rows.append(
                {
                    "scene": scene,
                    "valid_km2": round(footprint.area / 1e6, 1),
                    "in_acre_km2": round(inside / 1e6, 1),
                    "fraction_in_acre": round(inside / footprint.area, 4),
                    "keep": inside > 0,
                }
            )
            print(f"{scene}: {rows[-1]['in_acre_km2']:>8} km2 in Acre ({rows[-1]['fraction_in_acre']:.1%})", flush=True)
            time.sleep(PAUSE_S)

    with args.out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    kept = [r["scene"] for r in rows if r["keep"]]
    print(f"\n{len(kept)} of {len(rows)} scenes intersect Acre; outside: {[r['scene'] for r in rows if not r['keep']]}")


if __name__ == "__main__":
    main()
