"""Clip INPE's Sentinel-2 3-month mosaic of Brazil to Acre, as a JPEG COG.

The mosaic (BDC collection mosaic-s2-brazil-3m-1: 10 m, true colour, one
DEFLATE COG of ~237 GB per quarter) is too slow to tile straight from
data.inpe.br. This reads the window around Acre with range requests, keeps
the pixels within BUFFER_DEG of IBGE's boundary, and writes a YCbCr JPEG 85
COG with an internal mask through compress_cog.py.

The "visual" asset is already colour-balanced 8-bit RGB, so its link needs no
rescale; a 2-98 % stretch only turns it blue and brings out the seams.

Usage: python s2_mosaic.py <ITEM_ID> <WORK_DIR>
  e.g. python s2_mosaic.py MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_P20250916 work_s2
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.features import geometry_mask
from rasterio.windows import Window, from_bounds
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acre_scenes import acre_geometry  # noqa: E402
from compress_cog import GDAL_ENV, describe, write_variant  # noqa: E402

STAC_ITEM = "https://data.inpe.br/bdc/stac/v1/collections/mosaic-s2-brazil-3m-1/items/{}"
BUFFER_DEG = 0.02  # ~2 km, so the state's edge is not cut
STRIP_ROWS = 1024  # multiple of the 512 px block size
REMOTE_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": 8,
    "GDAL_HTTP_RETRY_DELAY": 5,
}


def visual_href(item_id: str) -> str:
    with urllib.request.urlopen(STAC_ITEM.format(item_id), timeout=60) as r:
        return json.load(r)["assets"]["visual"]["href"]


def write_clip(url: str, area, rgba_path: Path) -> None:
    """Tiled RGBA GeoTIFF of the window around `area`, alpha = 255 inside it where any band != 0."""
    with rasterio.Env(**REMOTE_ENV), rasterio.open(f"/vsicurl/{url}") as src:
        window = from_bounds(*area.bounds, src.transform).round_offsets().round_lengths()
        width, height = int(window.width), int(window.height)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 4,
            "dtype": "uint8",
            "crs": src.crs,
            "transform": src.window_transform(window),
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
            "interleave": "pixel",
            "photometric": "RGB",
            "alpha": "YES",
            "compress": "ZSTD",
            "zstd_level": 1,
            "predictor": 2,
            "bigtiff": "YES",
            "num_threads": "ALL_CPUS",
        }
        with rasterio.open(rgba_path, "w", **profile) as dst:
            dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
            for row in range(0, height, STRIP_ROWS):
                strip = Window(window.col_off, window.row_off + row, width, min(STRIP_ROWS, height - row))
                rgb = src.read(window=strip)
                # The mosaic is EPSG:4326 and IBGE's boundary SIRGAS 2000; they differ by well under a pixel.
                inside = geometry_mask([area], rgb.shape[1:], src.window_transform(strip), invert=True)
                rgb[:, ~inside] = 0  # zeros compress to almost nothing
                alpha = np.where(inside & (rgb != 0).any(axis=0), 255, 0).astype("uint8")
                out = Window(0, row, width, strip.height)
                dst.write(rgb, indexes=[1, 2, 3], window=out)
                dst.write(alpha, indexes=4, window=out)
                print(f"  rows {row + strip.height}/{height}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("item_id")
    parser.add_argument("work_dir", type=Path)
    args = parser.parse_args()

    args.work_dir.mkdir(parents=True, exist_ok=True)
    url = visual_href(args.item_id)
    area = shape(acre_geometry()).buffer(BUFFER_DEG)
    rgba = args.work_dir / f"{args.item_id}_ACRE_rgba.tif"
    out = args.work_dir / f"{args.item_id}_ACRE.tif"
    print(f"source {url}", flush=True)

    t0 = time.monotonic()
    write_clip(url, area, rgba)
    print(f"RGBA intermediate: {rgba.stat().st_size / 1e9:.2f} GB in {time.monotonic() - t0:.0f}s", flush=True)
    t0 = time.monotonic()
    with rasterio.Env(**GDAL_ENV):
        write_variant(rgba, out, "jpeg", 85)
    print(f"{describe(out)} in {time.monotonic() - t0:.0f}s", flush=True)
    rgba.unlink()


if __name__ == "__main__":
    main()
