"""Per-band rescale ranges per scene, written as stats.txt lines.

Line format: YYYYMMDD_path_row:lo-hi|lo-hi|lo-hi. Ranges are in INPE's raw
values; the bucket copies are stretched (stretch.txt), so values read through
the service are mapped back to raw first.

--common (used for the links): one contrast for all scenes, so neighbouring
scenes match. Reads each bucket copy's preview through the service, masks
clouds, and measures the scene's dark level (5th percentile of the cloud-free
pixels). Each scene's offset from the typical dark level (haze and smoke add
to it, a low sun takes from it) is removed, the 2-99.5 % range of the pooled
cloud-free pixels gives the common range, and each scene gets that range
shifted by its own offset. Run it over all scenes together.

--inpe / --service: the earlier per-scene 2-98 % ranges (minimum width 20),
from the INPE originals or from the bucket copies (/cog/statistics), both at
max_size=256. Percentiles of a scene full of cloud or of uniform forest give
too wide or too narrow a range, so these are kept for reference only.

linear_bounds gives the per-band stretch that process_all.py bakes into the
copies (stretch.txt).

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
from rasterio.enums import Resampling
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile
from rio_tiler.io import Reader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import STRETCH_TXT, cog_url, gs_url, read_stats, to_raw  # noqa: E402

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
TYPICAL_DARK = (40, 52, 70)  # median dark level of the 31 scenes before the first replaced one (see shifted_ranges)
COMMON_RANGE = ((39, 91), (51, 90), (69, 111))  # their common range, recovered from stats.txt

# linear_bounds
STRETCH_PREVIEW = 16  # read the original at 1/16, from its overviews
STRETCH_PCT = (0.01, 99.99)  # of the cloud-free pixels; brighter clouds saturate
STRETCH_MARGIN = 2  # levels beyond those values and beyond the link's rescale range
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


def from_service(base_url: str, scene: str, stretch: list[tuple[int, int]]) -> list[tuple[int, int]]:
    url = f"{base_url}/cog/statistics?url={quote(gs_url(scene), safe='')}&max_size={MAX_SIZE}&p=2&p=98"
    with urllib.request.urlopen(url, timeout=120) as r:
        stats = json.load(r)
    return [
        widen(to_raw(stats[b]["percentile_2"], low, high), to_raw(stats[b]["percentile_98"], low, high))
        for b, (low, high) in zip(("b1", "b2", "b3"), stretch)
    ]


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


def clear_pixels(base_url: str, scene: str, stretch: list[tuple[int, int]]) -> np.ndarray:
    """Cloud-free RGB pixels (N x 3, raw values) of the scene's preview."""
    # nearest, as when stats.txt was computed; the service now defaults to bilinear,
    # whose smoothing would narrow the percentiles and shift every range.
    url = f"{base_url}/cog/preview.png?url={quote(gs_url(scene), safe='')}&bidx=1&bidx=2&bidx=3&max_size={PREVIEW_SIZE}&resampling=nearest"
    with urllib.request.urlopen(url, timeout=180) as r, MemoryFile(r.read()) as mem, warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)  # a PNG has no geotransform
        with mem.open() as png:
            rgba = png.read()
    stored, valid = rgba[:3].transpose(1, 2, 0), rgba[3] > 0
    rgb = np.stack([np.round(to_raw(stored[..., b], low, high)) for b, (low, high) in enumerate(stretch)], axis=2)
    darkest = rgb.min(axis=2)
    cloud = valid & (darkest > max(CLOUD_MIN, np.median(darkest[valid]) + CLOUD_ABOVE_GROUND))
    return rgb[valid & ~grow(cloud, CLOUD_GROW_PX)].astype(np.int16)


def linear_bounds(src_path: Path, ranges: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    """Per-band low/high of the stretch baked into a copy, from the INPE original.

    The cloud-free 0.01 / 99.99 % values, STRETCH_MARGIN levels wider, and never
    inside the link's rescale range plus the same margin, so every value the
    link shows survives the stretch; low stays >= 1 as 0 is no data.
    """
    with rasterio.open(src_path) as src:
        shape = (3, src.height // STRETCH_PREVIEW, src.width // STRETCH_PREVIEW)
        a = src.read(out_shape=shape, resampling=Resampling.nearest)
    valid = (a != 0).any(axis=0)
    darkest = a.min(axis=0)
    cloud = darkest > max(CLOUD_MIN, np.median(darkest[valid]) + CLOUD_ABOVE_GROUND)
    px = a[:, valid & ~cloud]
    lo, hi = (np.percentile(px, p, axis=1) for p in STRETCH_PCT)
    low = [int(max(1, min(v - STRETCH_MARGIN, r[0] - STRETCH_MARGIN))) for v, r in zip(lo, ranges)]
    high = [int(min(255, max(v + STRETCH_MARGIN, r[1] + STRETCH_MARGIN))) for v, r in zip(hi, ranges)]
    return low, high


def shifted_ranges(a: np.ndarray) -> list[tuple[int, int]]:
    """Common range shifted by the scene's own dark level, for a scene outside the last --common run.

    a is the scene's preview (3 x H x W, raw values, 0 = no data). The offset is
    the 5th percentile of its cloud-free pixels minus TYPICAL_DARK, the same
    steps --common takes for every scene, but against fixed numbers: the median
    dark level and the common range of the 31 scenes of the run before the
    pansharpened ones, so that adding a scene does not move the other links.
    """
    valid = (a != 0).any(axis=0)
    darkest = a.min(axis=0)
    cloud = valid & (darkest > max(CLOUD_MIN, np.median(darkest[valid]) + CLOUD_ABOVE_GROUND))
    px = a[:, valid & ~grow(cloud, CLOUD_GROW_PX)]
    offset = np.percentile(px, 5, axis=1) - TYPICAL_DARK
    return [(max(0, round(lo + o)), min(255, round(hi + o))) for (lo, hi), o in zip(COMMON_RANGE, offset)]


def preview_ranges(src_path: Path) -> list[tuple[int, int]]:
    """shifted_ranges of a local scene, read at about PREVIEW_SIZE px with nearest resampling."""
    with rasterio.open(src_path) as src:
        scale = max(src.width, src.height) / PREVIEW_SIZE
        a = src.read(out_shape=(3, round(src.height / scale), round(src.width / scale)), resampling=Resampling.nearest)
    return shifted_ranges(a)


def common_ranges(base_url: str, scenes: list[str]) -> dict[str, list[tuple[int, int]]]:
    rng = np.random.default_rng(0)
    stretch = read_stats(STRETCH_TXT)
    samples, dark = {}, {}
    for scene in scenes:
        px = clear_pixels(base_url, scene, stretch[scene])
        dark[scene] = np.percentile(px, 5, axis=0)
        samples[scene] = px[rng.choice(len(px), size=min(len(px), SAMPLE_PX), replace=False)]
        print(f"{scene}: dark level {dark[scene].round().astype(int).tolist()}", flush=True)
    typical = np.median(list(dark.values()), axis=0)
    offset = {s: dark[s] - typical for s in scenes}
    pooled = np.concatenate([samples[s] - offset[s] for s in scenes])
    lo, hi = (np.percentile(pooled, p, axis=0) for p in POOLED_PCT)
    print(f"common range {list(zip(lo.round().astype(int).tolist(), hi.round().astype(int).tolist()))}", flush=True)
    if (hi - lo < MIN_WIDTH).any():  # never overwrite stats.txt with a degenerate contrast
        sys.exit(f"Common range narrower than {MIN_WIDTH} levels, stats not written")
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
    stretch = {} if args.inpe else read_stats(STRETCH_TXT)
    for scene in scenes:
        ranges = from_inpe(scene) if args.inpe else from_service(args.service.rstrip("/"), scene, stretch[scene])
        lines.append(stats_line(scene, ranges))
        print(lines[-1], flush=True)
        if args.inpe:
            time.sleep(PAUSE_S)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
