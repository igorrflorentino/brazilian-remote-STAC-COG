"""Copy scenes from INPE to a private bucket as JPEG 85 COGs, one at a time.

For each scene: download over a single connection, build the RGBA
intermediate, write the JPEG COG (YCbCr + internal mask), check it, upload
with `gcloud storage cp`, confirm the object size, write a done marker and
delete the temporary files. Failures are logged and the batch moves on;
re-running skips scenes that already have a marker and resumes partial
downloads.

--rgb keeps the JPEG in RGB instead of YCbCr, for the scenes in
scenes_rgb.txt: their narrow rescale turns YCbCr's small colour errors into
visible blotches. RGB files are about 2.8x larger (2.4-2.9x over those 19
scenes).

Usage: python process_all.py <SCENES_TXT> <WORK_DIR> <GS_PREFIX> [--rgb] [--limit N] [--max-mbps 20]
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compress_cog import GDAL_ENV, write_jpeg_rgb, write_rgba, write_variant  # noqa: E402
from fetch_scene import download  # noqa: E402
from make_links import cog_url, scene_path  # noqa: E402

CODEC, QUALITY = "jpeg", 85
PAUSE_S = 30  # between scenes, to stay gentle with data.inpe.br
ATTRIBUTION = "INPE - Brazil Data Cube (CC BY 4.0)"
# CC BY 4.0 asks to say the material was changed. No commas: gcloud splits on them.
MODIFICATIONS = "Recompressed from DEFLATE to JPEG quality 85 ({} with internal mask)"
GCLOUD = shutil.which("gcloud")
log = logging.getLogger("process_all")


def object_uri(gs_prefix: str, scene: str) -> str:
    """Keep INPE's layout below the collection root: v001/{path}/{row}/{YYYY}/{M}/name.tif."""
    return f"{gs_prefix.rstrip('/')}/{scene_path(scene)}"


def check_cog(path: Path, rgb: bool) -> None:
    with rasterio.open(path) as ds:
        struct = ds.tags(ns="IMAGE_STRUCTURE")
        problems = []
        if struct.get("LAYOUT") != "COG":
            problems.append(f"layout={struct.get('LAYOUT')}")
        if struct.get("COMPRESSION") != ("JPEG" if rgb else "YCbCr JPEG"):
            problems.append(f"compression={struct.get('COMPRESSION')}")
        if ds.count != 3 or "per_dataset" not in [f.name for f in ds.mask_flag_enums[0]]:
            problems.append("expected 3 bands with an internal mask")
        if len(ds.overviews(1)) < 5:
            problems.append(f"overviews={ds.overviews(1)}")
    if problems:
        raise RuntimeError(f"{path.name} is not the expected COG: {', '.join(problems)}")


def gcloud(*args: str) -> str:
    return subprocess.run([GCLOUD, *args], check=True, capture_output=True, text=True).stdout.strip()


def process(scene: str, work: Path, gs_prefix: str, max_mbps: float, rgb: bool) -> dict:
    url = cog_url(scene)
    src = work / Path(url).name
    rgba = work / f"{src.stem}_rgba.tif"
    lossless = work / f"{src.stem}_lossless.tif"
    out = work / f"{src.stem}_{CODEC}{QUALITY}.tif"
    uri = object_uri(gs_prefix, scene)
    for stale in (rgba, lossless, out):  # left over by an interrupted run
        stale.unlink(missing_ok=True)

    timings = {}
    t0 = time.monotonic()
    if not src.exists():
        download(url, src, max_mbps)
    timings["download_s"] = round(time.monotonic() - t0)

    t0 = time.monotonic()
    with rasterio.Env(**GDAL_ENV):
        write_rgba(src, rgba)
        if rgb:
            write_jpeg_rgb(rgba, out, QUALITY, lossless)
        else:
            write_variant(rgba, out, CODEC, QUALITY)
    check_cog(out, rgb)
    timings["compress_s"] = round(time.monotonic() - t0)

    t0 = time.monotonic()
    modifications = MODIFICATIONS.format("RGB" if rgb else "YCbCr")
    gcloud(
        "storage", "cp", str(out), uri,
        f"--custom-metadata=source={url},attribution={ATTRIBUTION},modifications={modifications}",
    )
    remote = int(gcloud("storage", "objects", "describe", uri, "--format=value(size)"))
    if remote != out.stat().st_size:
        raise RuntimeError(f"Uploaded size {remote} != local {out.stat().st_size} for {uri}")
    timings["upload_s"] = round(time.monotonic() - t0)

    result = {
        "scene": scene,
        "source": url,
        "uri": uri,
        "photometric": "RGB" if rgb else "YCbCr",
        "original_bytes": src.stat().st_size,
        "cog_bytes": out.stat().st_size,
        **timings,
    }
    for done in (src, rgba, out):
        done.unlink(missing_ok=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenes", type=Path)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("gs_prefix", help="e.g. gs://my-bucket")
    parser.add_argument("--rgb", action="store_true", help="Write RGB JPEG instead of YCbCr (see scenes_rgb.txt)")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N pending scenes")
    parser.add_argument("--max-mbps", type=float, default=20, help="Download throughput cap in MB/s (0 = none)")
    args = parser.parse_args()

    markers = args.work_dir / "done"
    markers.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(args.work_dir / "process.log")],
    )
    if not GCLOUD:
        sys.exit("gcloud not found on PATH")

    scenes = [s.strip() for s in args.scenes.read_text().splitlines() if s.strip()]
    pending = [s for s in scenes if not (markers / f"{s}.json").exists()]
    already_done = len(scenes) - len(pending)
    if args.limit:
        pending = pending[: args.limit]
    log.info("%d scenes, %d already done, processing %d", len(scenes), already_done, len(pending))

    failed = []
    for i, scene in enumerate(pending, 1):
        log.info("[%d/%d] %s", i, len(pending), scene)
        try:
            result = process(scene, args.work_dir, args.gs_prefix, args.max_mbps, args.rgb)
        except Exception as e:  # keep going; the scene is retried on the next run
            detail = e.stderr if isinstance(e, subprocess.CalledProcessError) else e
            log.exception("FAILED %s: %s", scene, detail)
            failed.append(scene)
        else:
            (markers / f"{scene}.json").write_text(json.dumps(result, indent=2))
            log.info(
                "OK %s: %.2f GB -> %.3f GB (%.1fx), download %ss, compress %ss, upload %ss",
                scene, result["original_bytes"] / 1e9, result["cog_bytes"] / 1e9,
                result["original_bytes"] / result["cog_bytes"],
                result["download_s"], result["compress_s"], result["upload_s"],
            )
        if i < len(pending):
            time.sleep(PAUSE_S)

    log.info("Finished: %d ok, %d failed %s", len(pending) - len(failed), len(failed), failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
