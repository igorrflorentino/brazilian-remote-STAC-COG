"""Compare lossy COG variants with the original scene as the service renders them.

The variants must hold raw values, as compress_cog.py's command line writes
them; for the stretched copies in the bucket see compare_served.py. Tiles are
WebMercatorQuad 256 px read with rio-tiler, bilinear like the service's
defaults. Reports file sizes, PSNR/SSIM on interior tiles at zooms 12/14/16
(raw and after the scene's own 2-98 % rescale, not the links' common one),
checks border tiles for lost transparency or dark fringes (with the internal
mask and with nodata=0), and writes side-by-side PNGs over a magenta background.

Usage: python compare_quality.py <ORIGINAL_TIF> <OUT_DIR> label=path [label=path ...]
"""

import argparse
import json
import random
from pathlib import Path

import morecantile
import numpy as np
from PIL import Image, ImageDraw
from rasterio.crs import CRS
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader
from skimage.metrics import peak_signal_noise_ratio, structural_similarity
from skimage.morphology import dilation

from scene_stats import MAX_SIZE, widen

TMS = morecantile.tms.get("WebMercatorQuad")
QUALITY_ZOOMS = (12, 14, 16)
EDGE_ZOOMS = (9, 12, 14, 16)
INTERIOR_PER_ZOOM = 8
EDGE_PER_ZOOM = 6
MAX_TRIES = 3000
DARK = 8  # max RGB value counted as a black fringe pixel
FRINGE_PX = 3  # distance from the transparent area checked for fringes
MAGENTA = np.array([255, 0, 255], dtype=np.uint8)


def rescale_ranges(src: Reader) -> list[tuple[int, int]]:
    """Per-band 2-98 % ranges, rounded and widened as scene_stats.py --inpe does."""
    stats = src.statistics(max_size=MAX_SIZE, percentiles=[2, 98], indexes=(1, 2, 3))
    return [widen(stats[b].model_dump()["percentile_2"], stats[b].model_dump()["percentile_98"]) for b in ("b1", "b2", "b3")]


def to_rgb(img, ranges=None) -> np.ndarray:
    """HxWx3 uint8 array from an ImageData, optionally rescaled."""
    data = img.data.astype("float32")
    if ranges:
        for i, (lo, hi) in enumerate(ranges):
            data[i] = (data[i] - lo) / (hi - lo) * 255
    return np.clip(data, 0, 255).astype("uint8").transpose(1, 2, 0)


def read_tile(src: Reader, tile, nodata=None):
    kwargs = {"nodata": nodata} if nodata is not None else {}
    return src.tile(tile.x, tile.y, tile.z, tilesize=256, indexes=(1, 2, 3),
                    resampling_method="bilinear", reproject_method="bilinear", **kwargs)


def sample_tiles(src: Reader, bounds, zoom: int, rng: random.Random):
    """Random interior (fully valid) and border (partly valid) tiles."""
    tiles = list(TMS.tiles(*bounds, [zoom]))
    rng.shuffle(tiles)
    interior, edge = [], []
    for tile in tiles[:MAX_TRIES]:
        if len(interior) >= INTERIOR_PER_ZOOM and len(edge) >= EDGE_PER_ZOOM:
            break
        try:
            img = read_tile(src, tile)
        except TileOutsideBounds:
            continue
        valid = (img.mask > 0).mean()
        if valid == 1 and len(interior) < INTERIOR_PER_ZOOM:
            interior.append((tile, img))
        elif 0.05 < valid < 0.95 and len(edge) < EDGE_PER_ZOOM:
            edge.append((tile, img))
    return interior, edge


def border_stats(orig, img) -> dict:
    """Transparency changes and near-black pixels next to the transparent area."""
    visible = img.mask > 0
    near_edge = dilation(~visible, footprint=np.ones((2 * FRINGE_PX + 1,) * 2, bool))
    dark = visible & (img.data.max(axis=0) <= DARK)
    return {
        "visible": int(visible.sum()),
        "lost": int(((orig.mask > 0) & ~visible).sum()),
        "extra": int(((orig.mask == 0) & visible).sum()),
        "fringe": int((dark & near_edge).sum()),
    }


def cell(img, ranges, label: str, scale: int = 2) -> Image.Image:
    rgb = to_rgb(img, ranges)
    rgb[img.mask == 0] = MAGENTA
    im = Image.fromarray(rgb).resize((256 * scale,) * 2, Image.NEAREST)
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, 8 * len(label) + 12, 22), fill=(0, 0, 0))
    draw.text((6, 5), label, fill=(255, 255, 255))
    return im


def save_row(cells: list[Image.Image], path: Path) -> None:
    w, h = cells[0].size
    row = Image.new("RGB", (w * len(cells) + 4 * (len(cells) - 1), h), (255, 255, 255))
    for i, c in enumerate(cells):
        row.paste(c, (i * (w + 4), 0))
    row.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("original", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("variants", nargs="+", help="label=path")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    variants = [(v.split("=", 1)[0], Path(v.split("=", 1)[1])) for v in args.variants]
    rng = random.Random(args.seed)

    result = {"sizes_gb": {"original": args.original.stat().st_size / 1e9}}
    for label, path in variants:
        result["sizes_gb"][label] = path.stat().st_size / 1e9
    result["ratio"] = {k: result["sizes_gb"]["original"] / v for k, v in result["sizes_gb"].items()}

    with Reader(args.original) as orig:
        bounds = orig.get_geographic_bounds(CRS.from_epsg(4326))
        ranges = rescale_ranges(orig)
        result["rescale_original"] = ranges
        samples = {z: sample_tiles(orig, bounds, z, rng) for z in sorted(set(QUALITY_ZOOMS + EDGE_ZOOMS))}
        result["tiles"] = {z: {"interior": len(i), "edge": len(e)} for z, (i, e) in samples.items()}

        result["quality"], result["border"] = {}, {}
        readers = {label: Reader(path) for label, path in variants}
        try:
            for label, src in readers.items():
                result.setdefault("rescale_variant", {})[label] = rescale_ranges(src)
                q = result["quality"][label] = {}
                for z in QUALITY_ZOOMS:
                    vals = {"psnr": [], "ssim": [], "psnr_rescaled": [], "ssim_rescaled": []}
                    for tile, o in samples[z][0]:
                        v = read_tile(src, tile)
                        for suffix, rr in (("", None), ("_rescaled", ranges)):
                            a, b = to_rgb(o, rr), to_rgb(v, rr)
                            vals["psnr" + suffix].append(peak_signal_noise_ratio(a, b, data_range=255))
                            vals["ssim" + suffix].append(
                                structural_similarity(a, b, channel_axis=-1, data_range=255)
                            )
                    q[z] = {k: round(float(np.mean(x)), 4) for k, x in vals.items() if x}
                b = result["border"][label] = {}
                for z in EDGE_ZOOMS:
                    for mode, nodata in (("mask", None), ("nodata0", 0)):
                        agg = {"visible": 0, "lost": 0, "extra": 0, "fringe": 0}
                        for tile, o in samples[z][1]:
                            s = border_stats(o, read_tile(src, tile, nodata))
                            agg = {k: agg[k] + s[k] for k in agg}
                        b[f"z{z}_{mode}"] = agg
            base = result["border"]["original"] = {}
            for z in EDGE_ZOOMS:
                agg = {"visible": 0, "lost": 0, "extra": 0, "fringe": 0}
                for tile, o in samples[z][1]:
                    s = border_stats(o, o)
                    agg = {k: agg[k] + s[k] for k in agg}
                base[f"z{z}_mask"] = agg

            # Side-by-side rows: most textured interior tile per zoom, plus border tiles.
            cases = []
            for z in QUALITY_ZOOMS:
                if samples[z][0]:
                    tile, o = max(samples[z][0], key=lambda t: t[1].data.std())
                    cases.append((f"z{z}_interior", tile, o, None))
            for z in (12, 16):
                if samples[z][1]:
                    tile, o = samples[z][1][0]
                    cases.append((f"z{z}_border", tile, o, None))
                    cases.append((f"z{z}_border_nodata0", tile, o, 0))
            for name, tile, o, nodata in cases:
                cells = [cell(o, ranges, f"original {name}")]
                cells += [cell(read_tile(src, tile, nodata), ranges, label) for label, src in readers.items()]
                save_row(cells, args.out_dir / f"compare_{name}.png")
        finally:
            for src in readers.values():
                src.close()

    (args.out_dir / "results.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
