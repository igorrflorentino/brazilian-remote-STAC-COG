"""Compare the tiles the service serves from the bucket with INPE's lossless original.

The reference is the original read at 4x the tile size, box-averaged 4x4 over
valid pixels, then rescaled and gamma-corrected by rio-tiler, the same code the
service runs. Prints the mean bias and PSNR per scene at the centre of the
scene, at zooms 12 and 15, then the medians. compare_quality.py compares
codec variants on local files instead.

Reads a few MB per scene from data.inpe.br over range requests.

Usage: python compare_served.py <BASE_URL> [scene ...]
"""

import io
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("VSI_CACHE", "TRUE")

import morecantile  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from rio_tiler.io import Reader  # noqa: E402
from rio_tiler.models import ImageData  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import make_links as ml  # noqa: E402

TMS = morecantile.tms.get("WebMercatorQuad")
TILE = 256
K = 4
ZOOMS = (12, 15)


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=90) as r:
        return r.read()


def query(gs: str, rescale) -> str:
    q = f"url={quote(gs, safe='')}&bidx=1&bidx=2&bidx=3"
    q += "".join(f"&rescale={round(lo, 2):g},{round(hi, 2):g}" for lo, hi in rescale)
    return q + f"&color_formula={quote(ml.COLOR_FORMULA)}"


def served(base: str, q: str, t) -> np.ndarray:
    data = get(f"{base}/cog/tiles/WebMercatorQuad/{t.z}/{t.x}/{t.y}.{ml.TILE_FORMAT}?{q}")
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGBA")).astype(np.float64)


def reference(src: Reader, t, ranges) -> tuple[np.ndarray, np.ndarray]:
    img = src.tile(t.x, t.y, t.z, tilesize=TILE * K, indexes=(1, 2, 3),
                   resampling_method="bilinear", reproject_method="bilinear")
    valid = img.mask > 0
    data = img.data.astype(np.float64) * valid
    s = data.reshape(3, TILE, K, TILE, K).sum(axis=(2, 4))
    c = valid.reshape(TILE, K, TILE, K).sum(axis=(1, 3))
    full = c == K * K
    avg = s / np.maximum(c, 1)
    arr = np.ma.MaskedArray(avg, mask=np.repeat(~full[None], 3, axis=0))
    ref = ImageData(arr).rescale(in_range=ranges)
    ref.apply_color_formula(ml.COLOR_FORMULA)
    return np.moveaxis(np.asarray(ref.data, dtype=np.float64), 0, -1), full


def psnr(err: np.ndarray) -> float:
    mse = float((err ** 2).mean())
    return 10 * np.log10(255 ** 2 / mse) if mse else float("inf")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python compare_served.py <BASE_URL> [scene ...]")
    base = sys.argv[1].rstrip("/")
    stats = ml.read_stats(ml.HERE / "stats.txt")
    stretch = ml.read_stats(ml.STRETCH_TXT)
    scenes = sys.argv[2:] or (ml.HERE / "scenes_acre.txt").read_text(encoding="utf-8").split()
    totals = {z: [] for z in ZOOMS}
    for scene in scenes:
        ranges = stats[scene]
        q = query(ml.gs_url(scene), [
            (ml.to_stored(lo, low, high), ml.to_stored(hi, low, high))
            for (lo, hi), (low, high) in zip(ranges, stretch[scene])
        ])
        w, s, e, n = json.loads(get(f"{base}/cog/WebMercatorQuad/tilejson.json?{q}"))["bounds"]
        lon, lat = (w + e) / 2, (s + n) / 2
        parts = []
        with Reader(ml.cog_url(scene)) as src:
            for z in ZOOMS:
                t = TMS.tile(lon, lat, z)
                ref, full = reference(src, t, ranges)
                tile = served(base, q, t)
                ok = full & (tile[..., 3] > 0)
                err = (tile[..., :3] - ref)[ok]
                bias = err.mean(axis=0)
                p = psnr(err)
                totals[z].append((p, np.abs(bias).max()))
                parts.append(f"z{z} {p:4.1f} dB bias {bias[0]:+.1f}/{bias[1]:+.1f}/{bias[2]:+.1f}")
        print(f"{scene}: " + " | ".join(parts), flush=True)
    for z in ZOOMS:
        ps, bs = (np.array(v) for v in zip(*totals[z]))
        print(f"z{z}: PSNR median {np.median(ps):.1f} dB (min {ps.min():.1f}), "
              f"max |bias| median {np.median(bs):.1f} (max {bs.max():.1f})")


if __name__ == "__main__":
    main()
