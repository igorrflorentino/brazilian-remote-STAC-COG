"""Write lossy COG variants of an RGB CBERS-4A scene where 0 = no data.

Step 1 writes a tiled RGBA GeoTIFF whose alpha marks valid pixels (any band
!= 0), optionally mapping the RGB values through a lookup table such as
linear_lut's stretch. Step 2 lets the GDAL COG driver (as in gdal_translate
-of COG) write each variant from it: JPEG turns the alpha into an internal
1-bit mask and uses YCbCr, WEBP keeps the alpha channel. Overviews use AVERAGE.

jpeg_rgb is JPEG without YCbCr, for scenes with a narrow rescale: the COG
driver always writes 3-band JPEG as YCbCr, so it goes through the GTiff
driver instead (see write_jpeg_rgb).

write_stretched_cog is the pipeline's path for the linearly stretched copies
(process_all.py): it averages the overviews from the raw values and only then
stretches, so the stretch's clipping does not lift or darken them.

Usage: python compress_cog.py <SRC_TIF> <OUT_DIR> jpeg:85 jpeg_rgb:85 webp:90 ...
"""

import argparse
import time
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.shutil import copy as rio_copy
from rasterio.windows import Window

STRIP_ROWS = 1024  # multiple of the 512 px block size
GDAL_ENV = {"GDAL_CACHEMAX": 1024, "GDAL_NUM_THREADS": "ALL_CPUS"}
RAW16_SCALE = 64  # raw x 64 in UInt16: the averaged overviews keep 6 bits of fraction


def linear_lut(low: list[int], high: list[int]) -> np.ndarray:
    """3 x 256 table stretching each band linearly from low-high to 0-255, clipped."""
    values = np.arange(256, dtype=float)
    return np.stack(
        [np.clip(np.round((values - lo) * 255 / (hi - lo)), 0, 255) for lo, hi in zip(low, high)]
    ).astype("uint8")


def write_rgba(src_path: Path, rgba_path: Path, lut: np.ndarray | None = None) -> None:
    """Copy RGB to a tiled RGBA GeoTIFF with alpha = 255 where any band != 0.

    lut (3 x 256), if given, maps the RGB values; the alpha still comes from
    the raw values, so a valid pixel stretched to 0 keeps its alpha.
    """
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
                if lut is not None:
                    rgb = np.stack([lut[b][rgb[b]] for b in range(3)])
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


def stretch_intermediates(out_path: Path) -> tuple[Path, Path]:
    """Temporary files write_stretched_cog keeps next to out_path: raw16 GeoTIFF and VRT."""
    return out_path.with_name(f"{out_path.stem}_raw16.tif"), out_path.with_name(f"{out_path.stem}_stretch.vrt")


def write_raw16(src_path: Path, raw16_path: Path) -> None:
    """Copy RGB as raw x RAW16_SCALE UInt16, internal mask where any band != 0, AVERAGE overviews."""
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(src_path) as src:
        profile = src.profile
        profile.update(
            driver="GTiff",
            dtype="uint16",
            nodata=None,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            interleave="pixel",
            photometric="RGB",
            compress="ZSTD",
            zstd_level=1,
            predictor=2,
            bigtiff="YES",
            num_threads="ALL_CPUS",
        )
        with rasterio.open(raw16_path, "w", **profile) as dst:
            for row in range(0, src.height, STRIP_ROWS):
                window = Window(0, row, src.width, min(STRIP_ROWS, src.height - row))
                rgb = src.read(window=window)
                dst.write(rgb.astype("uint16") * RAW16_SCALE, window=window)
                dst.write_mask(np.where((rgb != 0).any(axis=0), 255, 0).astype("uint8"), window=window)
            dst.build_overviews(overview_factors(src.width, src.height), Resampling.average)


def stretch_vrt(raw16_path: Path, low: list[int], high: list[int]) -> str:
    """VRT reading raw16 as Byte, each band stretched from low-high to 0-255 and clamped.

    A linear scale rather than a <LUT>, which was much slower in tests. The
    VRT's implicit overviews apply it to raw16's averaged overviews.
    """
    with rasterio.open(raw16_path) as src:
        width, height, t = src.width, src.height, src.transform
        srs = escape(src.crs.to_wkt())
    source = f'<SourceFilename relativeToVRT="0">{escape(str(raw16_path))}</SourceFilename>'
    bands = "".join(
        f'  <VRTRasterBand dataType="Byte" band="{b}"><ColorInterp>{color}</ColorInterp>\n'
        f"    <ComplexSource>{source}<SourceBand>{b}</SourceBand>"
        f"<ScaleOffset>{-lo * 255 / (hi - lo)!r}</ScaleOffset>"
        f"<ScaleRatio>{255 / ((hi - lo) * RAW16_SCALE)!r}</ScaleRatio></ComplexSource>\n"
        "  </VRTRasterBand>\n"
        for b, (color, lo, hi) in enumerate(zip(("Red", "Green", "Blue"), low, high), start=1)
    )
    return (
        f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">\n'
        f"  <SRS>{srs}</SRS>\n"
        f"  <GeoTransform>{t.c!r}, {t.a!r}, {t.b!r}, {t.f!r}, {t.d!r}, {t.e!r}</GeoTransform>\n"
        f"{bands}"
        f'  <MaskBand><VRTRasterBand dataType="Byte"><SimpleSource>{source}'
        f"<SourceBand>mask,1</SourceBand></SimpleSource></VRTRasterBand></MaskBand>\n"
        "</VRTDataset>\n"
    )


def write_stretched_cog(src_path: Path, out_path: Path, low: list[int], high: list[int], quality: int, rgb: bool) -> None:
    """Write a JPEG COG stretched linearly per band (low-high -> 0-255) with an internal mask.

    The overviews are averaged from the raw values and stretched afterwards.
    Stretching first, as write_rgba + write_variant do, clips pixels below low
    to 0 before the average: in dark, hazy scenes (8-12 % of the 2 m pixels
    below low) that lifted the overviews by 4-8 display levels. rgb=True keeps
    JPEG in RGB, for scenes where YCbCr's 4:2:0 chroma gives a colour cast.
    """
    raw16, vrt = stretch_intermediates(out_path)
    try:
        write_raw16(src_path, raw16)
        vrt.write_text(stretch_vrt(raw16, low, high), encoding="utf-8")
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
            rio_copy(
                vrt,
                out_path,
                driver="GTiff",
                COPY_SRC_OVERVIEWS="YES",
                TILED="YES",
                BLOCKXSIZE=512,
                BLOCKYSIZE=512,
                COMPRESS="JPEG",
                JPEG_QUALITY=quality,
                PHOTOMETRIC="RGB" if rgb else "YCBCR",
                BIGTIFF="IF_SAFER",
                NUM_THREADS="ALL_CPUS",
            )
    finally:
        vrt.unlink(missing_ok=True)
        raw16.unlink(missing_ok=True)


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
