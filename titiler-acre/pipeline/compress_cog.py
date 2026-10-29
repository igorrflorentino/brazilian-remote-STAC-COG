"""Write lossy COG variants of an RGB CBERS-4A scene where 0 = no data.

Step 1 writes a tiled RGBA GeoTIFF whose alpha marks valid pixels (any band
!= 0). Step 2 lets the GDAL COG driver (as in gdal_translate -of COG) write
each variant from it: JPEG turns the alpha into an internal 1-bit mask and
uses YCbCr, WEBP keeps the alpha channel. Overviews use AVERAGE.

jpeg_rgb is JPEG without YCbCr, for scenes with a narrow rescale: the COG
driver always writes 3-band JPEG as YCbCr, so it goes through the GTiff
driver instead (see write_jpeg_rgb).

Usage: python compress_cog.py <SRC_TIF> <OUT_DIR> jpeg:85 jpeg_rgb:85 webp:90 ...
"""

import argparse
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
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
            alpha="YES",  # unassociated alpha
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


def overview_factors(width: int, height: int, block: int = 512) -> list[int]:
    """Powers of 2 down to the first level that fits in one block, as the COG driver picks them."""
    factors, size = [], max(width, height)
    while size > block:
        size = -(-size // 2)
        factors.append(2 ** (len(factors) + 1))
    return factors


def write_jpeg_rgb(rgba_path: Path, out_path: Path, quality: int, lossless_path: Path) -> None:
    """Write a 3-band JPEG COG kept in RGB, i.e. without YCbCr and its 4:2:0 chroma.

    YCbCr's colour errors are 1-2 levels, but a narrow rescale (20 levels
    stretched to 255) turns them into visible 16 px blotches and a colour
    cast. The COG driver forces YCbCr for JPEG, so write a lossless masked
    copy with AVERAGE overviews first, then let the GTiff driver copy it with
    COPY_SRC_OVERVIEWS, which gives the same COG layout.
    """
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        with rasterio.open(rgba_path) as src:
            profile = src.profile
            profile.update(count=3, photometric="RGB", compress="ZSTD", zstd_level=1, predictor=2, bigtiff="YES")
            with rasterio.open(lossless_path, "w", **profile) as dst:
                for row in range(0, src.height, STRIP_ROWS):
                    window = Window(0, row, src.width, min(STRIP_ROWS, src.height - row))
                    dst.write(src.read([1, 2, 3], window=window), window=window)
                    dst.write_mask(src.read(4, window=window), window=window)
                dst.build_overviews(overview_factors(src.width, src.height), Resampling.average)
        rio_copy(
            lossless_path,
            out_path,
            driver="GTiff",
            COPY_SRC_OVERVIEWS="YES",
            TILED="YES",
            BLOCKXSIZE=512,
            BLOCKYSIZE=512,
            COMPRESS="JPEG",
            JPEG_QUALITY=quality,
            PHOTOMETRIC="RGB",
            BIGTIFF="IF_SAFER",
            NUM_THREADS="ALL_CPUS",
        )
    lossless_path.unlink()


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
    parser.add_argument("variants", nargs="+", help="codec:quality, e.g. jpeg:85 jpeg_rgb:85 webp:90")
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
            if codec == "jpeg_rgb":
                write_jpeg_rgb(rgba, out, int(quality), out.with_name(f"{out.stem}_lossless.tif"))
            else:
                write_variant(rgba, out, codec, int(quality))
            print(f"{describe(out)} in {time.monotonic() - t0:.0f}s", flush=True)
    if not args.keep_rgba:
        rgba.unlink()


if __name__ == "__main__":
    main()
