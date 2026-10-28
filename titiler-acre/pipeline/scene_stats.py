"""Per-band 2-98 % rescale ranges per scene, written as stats.txt lines.

Line format: YYYYMMDD_path_row:lo-hi|lo-hi|lo-hi, with a minimum width of 20.
Reads either the INPE originals directly with rio-tiler (--inpe) or the
bucket copies through the TiTiler service (/cog/statistics); both use
max_size=256 and percentiles 2/98, so the results are comparable.

Usage:
  python scene_stats.py <SCENES_TXT> <OUT_TXT> --inpe
  python scene_stats.py <SCENES_TXT> <OUT_TXT> --service <BASE_URL>
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import rasterio
from rio_tiler.io import Reader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import cog_url, gs_url  # noqa: E402

MAX_SIZE = 256
MIN_WIDTH = 20
PAUSE_S = 2  # between INPE scenes, to stay below its rate limit
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": 8,
    "GDAL_HTTP_RETRY_DELAY": 5,
}


def widen(lo: float, hi: float) -> tuple[int, int]:
    """Round to integers and enforce the minimum width, centred and kept in 0-255."""
    lo, hi = round(lo), round(hi)
    if hi - lo < MIN_WIDTH:
        lo = min(max(0, (lo + hi) // 2 - MIN_WIDTH // 2), 255 - MIN_WIDTH)
        hi = lo + MIN_WIDTH
    return lo, hi


def from_inpe(scene: str) -> list[tuple[int, int]]:
    with rasterio.Env(**GDAL_ENV), Reader(cog_url(scene)) as src:
        stats = src.statistics(max_size=MAX_SIZE, percentiles=[2, 98], indexes=(1, 2, 3))
    return [widen(stats[b].model_dump()["percentile_2"], stats[b].model_dump()["percentile_98"]) for b in ("b1", "b2", "b3")]


def from_service(base_url: str, scene: str) -> list[tuple[int, int]]:
    url = f"{base_url}/cog/statistics?url={quote(gs_url(scene), safe='')}&max_size={MAX_SIZE}&p=2&p=98"
    with urllib.request.urlopen(url, timeout=120) as r:
        stats = json.load(r)
    return [widen(stats[b]["percentile_2"], stats[b]["percentile_98"]) for b in ("b1", "b2", "b3")]


def stats_line(scene: str, ranges: list[tuple[int, int]]) -> str:
    return f"{scene}:" + "|".join(f"{lo}-{hi}" for lo, hi in ranges)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenes", type=Path)
    parser.add_argument("out", type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--inpe", action="store_true", help="Read the INPE originals")
    source.add_argument("--service", help="TiTiler base URL serving the bucket copies")
    args = parser.parse_args()

    scenes = [s.strip() for s in args.scenes.read_text().splitlines() if s.strip()]
    lines = []
    for scene in scenes:
        ranges = from_inpe(scene) if args.inpe else from_service(args.service.rstrip("/"), scene)
        lines.append(stats_line(scene, ranges))
        print(lines[-1], flush=True)
        if args.inpe:
            time.sleep(PAUSE_S)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
