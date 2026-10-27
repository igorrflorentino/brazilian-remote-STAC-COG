"""Write lossy COG variants of an RGB CBERS-4A scene where 0 = no data.

Step 1 writes a tiled RGBA GeoTIFF whose alpha marks valid pixels (any band
!= 0). Step 2 lets the GDAL COG driver (as in gdal_translate -of COG) write
each variant from it: JPEG turns the alpha into an internal 1-bit mask and
uses YCbCr, WEBP keeps the alpha channel. Overviews use AVERAGE.

Usage: python compress_cog.py <SRC_TIF> <OUT_DIR> jpeg:85 webp:90 ...
"""

import argparse
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.shutil import copy as rio_copy
from rasterio.windows import Window

STRIP_ROWS = 1024  # multiple of the 512 px block size
GDAL_ENV = {"GDAL_CACHEMAX": 1024, "GDAL_NUM_THREADS": "ALL_CPUS"}


def write_rgba(src_path: Path, rgba_path: Path) -> None:
    """Copy RGB to a tiled RGBA GeoTIFF with alpha = 255 where any band != 0."""
    with rasterio.open(src_path) as src:
        profile = src.profile
        profile.update(
            driver="GTiff",
            count=4,
            nodata=None,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            interleave="pixel",
            photometric="RGB",
            alpha="UNASSOCIATED",
            compress="ZSTD",
            zstd_level=1,
            predictor=2,
            bigtiff="YES",
            num_threads="ALL_CPUS",
        )
        with rasterio.open(rgba_path, "w", **profile) as dst:
            dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
            for row in range(0, src.height, STRIP_ROWS):
                window = Window(0, row, src.width, min(STRIP_ROWS, src.height - row))
                rgb = src.read(window=window)
                alpha = np.where((rgb != 0).any(axis=0), 255, 0).astype("uint8")
                dst.write(rgb, indexes=[1, 2, 3], window=window)
                dst.write(alpha, indexes=4, window=window)


def write_variant(rgba_path: Path, out_path: Path, codec: str, quality: int) -> None:
    """Write a COG with the given lossy codec from the RGBA intermediate."""
    rio_copy(
        rgba_path,
        out_path,
        driver="COG",
        COMPRESS=codec.upper(),
        QUALITY=quality,
        BLOCKSIZE=512,
        OVERVIEW_RESAMPLING="AVERAGE",
        BIGTIFF="IF_SAFER",
        NUM_THREADS="ALL_CPUS",
    )


def describe(path: Path) -> str:
    """One-line summary of a written COG."""
    with rasterio.open(path) as ds:
        struct = ds.tags(ns="IMAGE_STRUCTURE")
        return (
            f"{path.name}: {path.stat().st_size / 1e9:.3f} GB, bands={ds.count}, "
            f"compression={struct.get('COMPRESSION')}, photometric={ds.photometric}, "
            f"layout={struct.get('LAYOUT')}, mask_flags={[f.name for f in ds.mask_flag_enums[0]]}, "
            f"overviews={ds.overviews(1)}, block={ds.block_shapes[0]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("src", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("variants", nargs="+", help="codec:quality, e.g. jpeg:85 webp:90")
    parser.add_argument("--keep-rgba", action="store_true", help="Keep the RGBA intermediate")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rgba = args.out_dir / f"{args.src.stem}_rgba.tif"
    with rasterio.Env(**GDAL_ENV):
        if not rgba.exists():
            t0 = time.monotonic()
            write_rgba(args.src, rgba)
            print(f"RGBA intermediate: {rgba.stat().st_size / 1e9:.2f} GB in {time.monotonic() - t0:.0f}s", flush=True)
        for spec in args.variants:
            codec, quality = spec.split(":")
            out = args.out_dir / f"{args.src.stem}_{codec}{quality}.tif"
            t0 = time.monotonic()
            write_variant(rgba, out, codec, int(quality))
            print(f"{describe(out)} in {time.monotonic() - t0:.0f}s", flush=True)
    if not args.keep_rgba:
        rgba.unlink()


if __name__ == "__main__":
    main()
