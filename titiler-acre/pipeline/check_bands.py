"""Check a fused scene's bands against INPE's unfused bands of the same pass.

CB4A-WPM-PCA-FUSED is INPE's own PCA fusion and it is not always right: the
red band of 20251107_233_126 had no relation to the real red, which turned
roofs blue and rivers orange (see the README, "Scene choice"). The unfused
collection CB4A-WPM-L4-DN keeps the bands apart (BAND1 blue, BAND2 green and
BAND3 red at 8 m, BAND0 pan at 2 m), so each fused band can be checked against
its own.

Samples 10 windows of 1.5 km inside the scene's valid data, averages the fused
pixels 4x4 to the multispectral 8 m, and correlates fused R, G and B with BAND3,
BAND2 and BAND1. `gap` is how far the red correlation trails the mean of the
green and blue ones. Haze lowers all three alike, so it leaves the gap alone.
Over 49 scene dates in Acre, 45 have a gap of 0.05 or less; three hazy or
borderline dates sit at 0.09-0.13 and need a look at the image; the defective
20251107_233_126 has 0.32.

Usage: python check_bands.py <SCENE_ID> [<SCENE_ID> ...]
Exits with 1 if any scene is defective. Reads a few MB per scene from
data.inpe.br over range requests.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "6")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "3")

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio import Affine  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.vrt import WarpedVRT  # noqa: E402
from rasterio.windows import Window  # noqa: E402
from rasterio.windows import transform as window_transform  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import SCENE_RE, cog_url  # noqa: E402

STAC_ITEM = "https://data.inpe.br/bdc/stac/v1/collections/CB4A-WPM-L4-DN-1/items/CBERS_4A_WPM_{date}_{path}_{row}_L4"
UNFUSED_BANDS = (3, 2, 1)  # the fused file's bands 1, 2, 3 are R, G, B = unfused BAND3, BAND2, BAND1
WINDOWS = 10
WINDOW_PX = 768  # 1.5 km at 2 m
DECIMATE = 4  # 2 m -> the 8 m of the multispectral bands
COARSE = 64  # the valid-data mask is read at 1/64 to place the windows
GAP_CHECK = 0.08  # look at the image
GAP_REJECT = 0.25  # defective


def band_correlations(fused: np.ndarray, unfused: np.ndarray) -> list[float]:
    """Correlation of each fused band with its own unfused band; both are 3 x N."""
    return [float(np.corrcoef(fused[b], unfused[b])[0, 1]) for b in range(3)]


def red_gap(corr: list[float]) -> float:
    """How far the red correlation trails the mean of the green and blue ones."""
    return (corr[1] + corr[2]) / 2 - corr[0]


def verdict(gap: float) -> str:
    return "defective" if gap >= GAP_REJECT else "check the image" if gap >= GAP_CHECK else "ok"


def all_valid(mask: np.ndarray, size: int) -> np.ndarray:
    """True where the size x size block centred on the pixel is all True (edges False)."""
    view = np.lib.stride_tricks.sliding_window_view
    out = view(mask, size, axis=0).all(axis=-1)
    out = view(out, size, axis=1).all(axis=-1)
    half = size // 2
    return np.pad(out, ((half, size - 1 - half), (half, size - 1 - half)))


def sample(scene: str) -> tuple[np.ndarray, np.ndarray]:
    """Pooled valid pixels (3 x N) of the fused scene and of its unfused bands, on the same 8 m grid."""
    m = SCENE_RE.match(scene)
    rng = np.random.default_rng(0)  # the same windows for a scene on every run
    with urllib.request.urlopen(STAC_ITEM.format(date=m["date"], path=m["path"], row=m["row"]), timeout=60) as r:
        assets = json.load(r)["assets"]
    size = WINDOW_PX // DECIMATE
    fused, unfused = [], []
    unfused_src = {b: rasterio.open("/vsicurl/" + assets[f"BAND{b}"]["href"]) for b in set(UNFUSED_BANDS)}
    try:
        with rasterio.open(cog_url(scene)) as f:
            coarse = f.read(1, out_shape=(f.height // COARSE, f.width // COARSE), resampling=Resampling.nearest) > 0
            block = WINDOW_PX // COARSE + 1
            ys, xs = np.nonzero(all_valid(coarse, block))
            for i in rng.choice(len(ys), size=min(WINDOWS, len(ys)), replace=False):
                row = (int(ys[i]) - block // 2) * COARSE
                col = (int(xs[i]) - block // 2) * COARSE
                window = Window(col, row, WINDOW_PX, WINDOW_PX)
                pixels = f.read(window=window, out_shape=(3, size, size), resampling=Resampling.average).astype(float)
                transform = window_transform(window, f.transform) * Affine.scale(DECIMATE)
                bands = []
                for b in UNFUSED_BANDS:
                    with WarpedVRT(
                        unfused_src[b], crs=f.crs, transform=transform, width=size, height=size,
                        resampling=Resampling.average, nodata=0,
                    ) as vrt:
                        bands.append(vrt.read(1).astype(float))
                other = np.array(bands)
                valid = (pixels.min(axis=0) > 0) & (other.min(axis=0) > 0)
                fused.append(pixels[:, valid])
                unfused.append(other[:, valid])
    finally:
        for src in unfused_src.values():
            src.close()
    return np.concatenate(fused, axis=1), np.concatenate(unfused, axis=1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenes", nargs="+", help="YYYYMMDD_path_row")
    args = parser.parse_args()

    defective = []
    for scene in args.scenes:
        if not SCENE_RE.match(scene):
            sys.exit(f"Invalid scene id {scene!r}, expected YYYYMMDD_path_row")
        try:
            corr = band_correlations(*sample(scene))
        except urllib.error.HTTPError as e:
            print(f"{scene}: no unfused item to compare with (HTTP {e.code})", flush=True)
            continue
        gap = red_gap(corr)
        print(
            f"{scene}: R/G/B correlation with the unfused bands {corr[0]:+.2f} {corr[1]:+.2f} {corr[2]:+.2f}, "
            f"gap {gap:+.2f}, {verdict(gap)}",
            flush=True,
        )
        if verdict(gap) == "defective":
            defective.append(scene)
    sys.exit(1 if defective else 0)


if __name__ == "__main__":
    main()
