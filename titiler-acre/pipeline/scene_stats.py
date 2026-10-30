"""Per-band rescale ranges per scene, written as stats.txt lines.

Line format: YYYYMMDD_path_row:lo-hi|lo-hi|lo-hi.

--common (used for the links): one contrast for all scenes, so neighbouring
scenes match. Reads each bucket copy's raw preview through the service, masks
clouds, and measures the scene's dark level (5th percentile of the cloud-free
pixels). Each scene's offset from the typical dark level (haze and smoke add
to it, a low sun takes from it) is removed, the 2-99.5 % range of the pooled
cloud-free pixels gives the common range, and each scene gets that range
shifted by its own offset. Run it over all scenes together.

--inpe / --service: the earlier per-scene 2-98 % ranges (minimum width 20),
from the INPE originals or from the bucket copies (/cog/statistics), both at
max_size=256. Percentiles of a scene full of cloud or of uniform forest give
too wide or too narrow a range, so these are kept for reference only.

Usage:
  python scene_stats.py <SCENES_TXT> <OUT_TXT> --common <BASE_URL>
  python scene_stats.py <SCENES_TXT> <OUT_TXT> --inpe
  python scene_stats.py <SCENES_TXT> <OUT_TXT> --service <BASE_URL>
"""

import argparse
import json
import sys
import time
import urllib.request
import warnings
from pathlib import Path
from urllib.parse import quote

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile
from rio_tiler.io import Reader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import cog_url, gs_url  # noqa: E402

MAX_SIZE = 256
MIN_WIDTH = 20
PAUSE_S = 2  # between INPE scenes, to stay below its rate limit

# --common
PREVIEW_SIZE = 1024
CLOUD_MIN = 110  # a pixel is cloud if its darkest band is above this...
CLOUD_ABOVE_GROUND = 50  # ...and this far above the scene's median darkest band
CLOUD_GROW_PX = 3  # ~300 m at PREVIEW_SIZE, drops the thin cloud at the edges
SAMPLE_PX = 150_000  # cloud-free pixels per scene in the pooled range
# Forest is most of Acre and half the pooled pixels sit within 5 levels of the
# dark end, so a 98 % top falls inside pasture and clips it.
POOLED_PCT = (2, 99.5)
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


def grow(mask: np.ndarray, px: int) -> np.ndarray:
    """Binary dilation by px steps of 4-connectivity."""
    out = mask.copy()
    for _ in range(px):
        step = out.copy()
        step[1:] |= out[:-1]
        step[:-1] |= out[1:]
        step[:, 1:] |= out[:, :-1]
        step[:, :-1] |= out[:, 1:]
        out = step
    return out


def clear_pixels(base_url: str, scene: str) -> np.ndarray:
    """Cloud-free RGB pixels (N x 3, raw values) of the scene's preview."""
    url = f"{base_url}/cog/preview.png?url={quote(gs_url(scene), safe='')}&bidx=1&bidx=2&bidx=3&max_size={PREVIEW_SIZE}"
    with urllib.request.urlopen(url, timeout=180) as r, MemoryFile(r.read()) as mem, warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)  # a PNG has no geotransform
        with mem.open() as png:
            rgba = png.read()
    rgb, valid = rgba[:3].transpose(1, 2, 0), rgba[3] > 0
    darkest = rgb.min(axis=2)
    cloud = valid & (darkest > max(CLOUD_MIN, np.median(darkest[valid]) + CLOUD_ABOVE_GROUND))
    return rgb[valid & ~grow(cloud, CLOUD_GROW_PX)].astype(np.int16)


def common_ranges(base_url: str, scenes: list[str]) -> dict[str, list[tuple[int, int]]]:
    rng = np.random.default_rng(0)
    samples, dark = {}, {}
    for scene in scenes:
        px = clear_pixels(base_url, scene)
        dark[scene] = np.percentile(px, 5, axis=0)
        samples[scene] = px[rng.choice(len(px), size=min(len(px), SAMPLE_PX), replace=False)]
        print(f"{scene}: dark level {dark[scene].round().astype(int).tolist()}", flush=True)
    typical = np.median(list(dark.values()), axis=0)
    offset = {s: dark[s] - typical for s in scenes}
    pooled = np.concatenate([samples[s] - offset[s] for s in scenes])
    lo, hi = (np.percentile(pooled, p, axis=0) for p in POOLED_PCT)
    print(f"common range {list(zip(lo.round().astype(int).tolist(), hi.round().astype(int).tolist()))}", flush=True)
    return {
        s: [(max(0, round(lo[b] + offset[s][b])), min(255, round(hi[b] + offset[s][b]))) for b in range(3)]
        for s in scenes
    }


def stats_line(scene: str, ranges: list[tuple[int, int]]) -> str:
    return f"{scene}:" + "|".join(f"{lo}-{hi}" for lo, hi in ranges)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenes", type=Path)
    parser.add_argument("out", type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--common", metavar="BASE_URL", help="Common range over all scenes, through the service")
    source.add_argument("--inpe", action="store_true", help="Per-scene 2-98 %% from the INPE originals")
    source.add_argument("--service", metavar="BASE_URL", help="Per-scene 2-98 %% from the bucket copies")
    args = parser.parse_args()

    scenes = [s.strip() for s in args.scenes.read_text().splitlines() if s.strip()]
    if args.common:
        ranges = common_ranges(args.common.rstrip("/"), scenes)
        lines = [stats_line(s, ranges[s]) for s in scenes]
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return
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
