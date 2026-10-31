"""Pansharpen an unfused INPE CBERS-4A WPM scene into a fused-like RGB GeoTIFF.

Some cells of Acre have no usable scene in the fused collection
(CB4A-WPM-PCA-FUSED-1). The L4 DN collection (CB4A-WPM-L4-DN-1) keeps the bands
apart: the pan (BAND0, 2 m) and the blue, green and red bands (BAND1-3, 8 m).
This script sharpens the colour bands with SFIM (smoothing filter-based
intensity modulation): they are upsampled to 2 m with a bicubic kernel and
multiplied by the ratio of the pan to its own 4 x 4 mean, so every 8 m pixel
keeps its colour and the 2 m detail comes from the pan. The bands of the L4
product are registered to each other (tested on 2022-10-09, cell 241/124: the
best pan-to-colour shift was 0 px in all three), so no coregistration is done.

The output has INPE's fused layout: 3 bands (R, G, B), UInt8, 0 = no data, in
the same grid as the pan. DN are scaled by GAIN to the range of INPE's fused
scenes, so the rest of the pipeline (process_all.py) treats it like one. The
fused scenes have a gain of their own each, 0.25-0.40 against the DN (median
of four scenes 0.32): INPE rescales every scene. The offset is not matched,
because process_all.py anchors each scene's range on its own dark level.

Usage: python pansharpen_scene.py <SCENE_ID> <OUT_TIF> <WORK_DIR> [--max-mbps 20]
"""

import argparse
import json
import sys
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from scipy import ndimage as ndi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fetch_scene import download  # noqa: E402
from make_links import SCENE_RE  # noqa: E402

STAC_ITEM = "https://data.inpe.br/bdc/stac/v1/collections/CB4A-WPM-L4-DN-1/items/CBERS_4A_WPM_{date}_{path}_{row}_L4"
BANDS = (3, 2, 1)  # R, G, B; BAND0 is the pan and BAND4 the near infrared, which is not used
GAIN = 0.32
RATIO = 4  # the pan is 4 times finer than the colour bands
STRIP_ROWS = 512  # pan rows per strip, a multiple of 4 and of the output's block size
MARGIN = 32  # pan rows read beyond a strip so that the kernels see the neighbouring strips; the cubic
# spline's prefilter decays by 0.27 per colour pixel, so 8 of them leave 3e-5 of an edge step at the seam
RATIO_LIMIT = (0.5, 2.0)  # the pan-to-mean ratio is kept inside this, as SFIM does at edges and clouds
EDGE_PX = 2  # colour pixels eroded from the valid area: the L4 edge pixels may mix in the fill value
WORKERS = 3
GDAL_ENV = {"GDAL_CACHEMAX": 1024}
# The pipeline reads the scene at 1/16 (scene_stats.linear_bounds) and at about 1/55 (preview_ranges). INPE's
# COGs have overviews for that; without them every such read decodes the whole file, once per band.
OVERVIEWS = (16, 32)


def band_urls(scene: str) -> dict[int, str]:
    """URLs of the pan (key 0) and of the colour bands of the scene's L4 DN item."""
    m = SCENE_RE.match(scene)
    with urllib.request.urlopen(STAC_ITEM.format(date=m["date"], path=m["path"], row=m["row"]), timeout=60) as r:
        assets = json.load(r)["assets"]
    return {b: assets[f"BAND{b}"]["href"] for b in (0, *BANDS)}


def sharpen(pan: np.ndarray, ms: np.ndarray, gain: float = GAIN) -> np.ndarray:
    """UInt8 RGB (3 x h x w) from the pan (h x w) and the colour bands (3 x h/4 x w/4), in DN.

    h and w are multiples of 4. Pixels where the pan or any colour band is 0, or
    within EDGE_PX colour pixels of such a pixel, come out 0; every other pixel is
    at least 1, as 0 means no data.
    """
    h, w = pan.shape
    has = (ms > 0).all(axis=0)
    if not has.any():
        return np.zeros((3, h, w), "uint8")
    if not has.all():  # fill the holes with the nearest value, so that the kernel sees no step at a scene's edge
        near = ndi.distance_transform_edt(~has, return_distances=False, return_indices=True)
        ms = ms[:, near[0], near[1]]
    pan = pan.astype("float32", copy=False)
    smooth = ndi.zoom(pan.reshape(h // RATIO, RATIO, w // RATIO, RATIO).mean(axis=(1, 3)), RATIO, order=1, grid_mode=True, mode="nearest")
    ratio = np.clip(pan / np.maximum(smooth, 1), *RATIO_LIMIT)
    out = np.empty((3, h, w), "uint8")
    for b in range(3):
        up = ndi.zoom(ms[b].astype("float32"), RATIO, order=3, grid_mode=True, mode="nearest")
        out[b] = np.clip(np.rint(up * ratio * gain), 1, 255)
    keep = ndi.binary_erosion(has, iterations=EDGE_PX, border_value=1)
    valid = (pan > 0) & np.repeat(np.repeat(keep, RATIO, axis=0), RATIO, axis=1)
    out[:, ~valid] = 0
    return out


def pansharpen(pan_path: Path, ms_paths: list[Path], out_path: Path, gain: float = GAIN, strip: int = STRIP_ROWS) -> None:
    """Write the sharpened scene strip by strip; the pan's last pixels beyond a multiple of 4 stay 0."""
    with rasterio.Env(**GDAL_ENV), rasterio.open(pan_path) as pan_ds:
        ms_ds = [rasterio.open(p) for p in ms_paths]
        try:
            width = min(pan_ds.width // RATIO, *(d.width for d in ms_ds)) * RATIO
            height = min(pan_ds.height // RATIO, *(d.height for d in ms_ds)) * RATIO
            profile = dict(
                driver="GTiff", dtype="uint8", count=3, width=pan_ds.width, height=pan_ds.height, crs=pan_ds.crs,
                transform=pan_ds.transform, nodata=0, tiled=True, blockxsize=512, blockysize=512, interleave="pixel",
                compress="ZSTD", zstd_level=1, predictor=2, bigtiff="YES",
            )
            with rasterio.open(out_path, "w", **profile) as dst, ThreadPoolExecutor(WORKERS) as pool:
                pending = deque()

                def flush(keep: int) -> None:
                    while len(pending) > keep:
                        row, future = pending.popleft()
                        done = future.result()
                        dst.write(done, window=Window(0, row, width, done.shape[1]))

                for row in range(0, height, strip):
                    top, bottom = max(0, row - MARGIN), min(height, row + strip + MARGIN)
                    pan = pan_ds.read(1, window=Window(0, top, width, bottom - top))
                    ms = np.stack([d.read(1, window=Window(0, top // RATIO, width // RATIO, (bottom - top) // RATIO)) for d in ms_ds])
                    own = slice(row - top, row - top + min(strip, height - row))
                    pending.append((row, pool.submit(lambda p, m, s: sharpen(p, m, gain)[:, s], pan, ms, own)))
                    flush(WORKERS)
                flush(0)
        finally:
            for d in ms_ds:
                d.close()


def add_overviews(path: Path) -> None:
    """Nearest overviews at OVERVIEWS, inside the file; they only serve the pipeline's statistics."""
    with rasterio.Env(**GDAL_ENV, COMPRESS_OVERVIEW="ZSTD", PREDICTOR_OVERVIEW=2, GDAL_NUM_THREADS="ALL_CPUS"):
        with rasterio.open(path, "r+") as dst:
            dst.build_overviews(list(OVERVIEWS), Resampling.nearest)


def build(scene: str, out_path: Path, work: Path, max_mbps: float) -> None:
    """Download the scene's pan and colour bands into work, write out_path and delete the downloads.

    The scene is written under out_path's name plus ".part" and renamed when it is whole, with its
    overviews: a run killed half way (SIGTERM, power) leaves a .part that the next run overwrites, never
    a half-written out_path that process_all would take for done.
    """
    urls = band_urls(scene)
    work.mkdir(parents=True, exist_ok=True)
    files = {b: download(u, work / Path(u).name, max_mbps) for b, u in urls.items()}
    part = out_path.with_name(out_path.name + ".part")
    try:
        pansharpen(files[0], [files[b] for b in BANDS], part)
        add_overviews(part)
        part.replace(out_path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        for f in files.values():
            f.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scene", help="YYYYMMDD_path_row")
    parser.add_argument("out", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--max-mbps", type=float, default=20, help="Download throughput cap in MB/s (0 = none)")
    args = parser.parse_args()
    if not SCENE_RE.match(args.scene):
        sys.exit(f"Invalid scene id {args.scene!r}, expected YYYYMMDD_path_row")
    build(args.scene, args.out, args.work, args.max_mbps)


if __name__ == "__main__":
    main()
