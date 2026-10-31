"""Copy scenes from INPE to a private bucket as stretched JPEG 85 COGs, one at a time.

For each scene: download over a single connection, pick the per-band linear
stretch (scene_stats.linear_bounds), write the JPEG COG with an internal
mask (compress_cog.write_stretched_cog: YCbCr, or RGB for the scenes in
scenes_rgb.txt), check it, upload with `gcloud storage cp`, confirm the
object size, write a done marker, update stretch.txt and delete the
temporary files. Failures are logged and the batch moves on; re-running
skips scenes that already have a marker and resumes partial downloads.

Why stretch: the links stretch the dark, narrow raw range 5-6.5x, which
also stretches JPEG's errors (blocks, and YCbCr colour casts). Stored
stretched, the links stretch only 1.1-2.8x. The overviews are averaged from
the raw values before the stretch, so they match the original's; the rescale
and gamma stay in the links (make_links.py converts the rescale to stored
values).

The scenes in scenes_pansharpened.txt have no fused original: their copy is
built from INPE's L4 DN bands (pansharpen_scene.py), their stats.txt line is
computed from that file (scene_stats.preview_ranges) and added once the scene
is done, and the object's metadata names the L4 item as the source.

Usage: python process_all.py <SCENES_TXT> <WORK_DIR> <GS_PREFIX> [--limit N] [--max-mbps 20]
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
from compress_cog import GDAL_ENV, stretch_intermediates, write_stretched_cog  # noqa: E402
from fetch_scene import backoff, download  # noqa: E402
from make_links import HERE, STRETCH_TXT, SCENE_RE, cog_url, read_stats, scene_path  # noqa: E402
from pansharpen_scene import GAIN, STAC_ITEM, build as pansharpen  # noqa: E402
from scene_stats import linear_bounds, preview_ranges, stats_line  # noqa: E402

QUALITY = 85
# Scenes whose copy is JPEG in RGB: on 4096 px crops, YCbCr's 4:2:0 chroma gave
# them a cast of 4.6-9.0 display levels against 3.2 at most elsewhere (hazy 2026
# dry-season scenes). RGB copies are about 2.7x the size of YCbCr ones.
SCENES_RGB_TXT = HERE / "scenes_rgb.txt"
# Scenes of cells with no usable fused scene, built from the unfused bands.
SCENES_PANSHARPENED_TXT = HERE / "scenes_pansharpened.txt"
STATS_TXT = HERE / "stats.txt"
PAUSE_S = 30  # between scenes, to stay gentle with data.inpe.br
GCLOUD_RETRIES = 6  # about 5 minutes in all; a failed upload costs a recompression otherwise
ATTRIBUTION = "INPE - Brazil Data Cube (CC BY 4.0)"
# CC BY 4.0 asks to say the material was changed. No commas: gcloud splits on them.
MODIFICATIONS = (
    "Stretched linearly per band from linear_stretch (raw low-high) to 0-255 "
    f"and recompressed from DEFLATE to JPEG quality {QUALITY} ({{}} with internal mask)"
)
PANSHARPENED_MODIFICATIONS = (
    f"Pansharpened from the L4 DN pan and colour bands with SFIM (DN x {GAIN}) then stretched linearly per band "
    f"from linear_stretch (raw low-high) to 0-255 and recompressed from DEFLATE to JPEG quality {QUALITY} ({{}} with internal mask)"
)
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
    """Run gcloud, retrying failures such as DNS blips; both uses are safe to repeat."""
    for attempt in range(GCLOUD_RETRIES):
        try:
            return subprocess.run([GCLOUD, *args], check=True, capture_output=True, text=True).stdout.strip()
        except subprocess.CalledProcessError as e:
            if attempt == GCLOUD_RETRIES - 1:
                raise
            wait = backoff(attempt)
            log.warning("gcloud %s failed, retry %d in %.0fs: %s", args[1], attempt + 1, wait, e.stderr.strip()[-300:])
            time.sleep(wait)


def write_stretch_txt(scenes: list[str], markers: Path) -> None:
    """Rewrite stretch.txt from the done markers, keeping lines of scenes not in this batch."""
    table = read_stats(STRETCH_TXT) if STRETCH_TXT.exists() else {}
    for scene in scenes:
        marker = markers / f"{scene}.json"
        if marker.exists():
            table[scene] = [tuple(pair) for pair in json.loads(marker.read_text())["stretch"]]
    order = [s for s in (HERE / "scenes_acre.txt").read_text(encoding="utf-8").split() if s in table]
    order += sorted(s for s in table if s not in order)
    STRETCH_TXT.write_text("\n".join(stats_line(s, table[s]) for s in order) + "\n", encoding="utf-8")


def write_stats_txt(scenes: list[str], markers: Path) -> None:
    """Add to stats.txt the ranges that pansharpened scenes computed from their own pixels."""
    table = read_stats(STATS_TXT)
    for scene in scenes:
        marker = markers / f"{scene}.json"
        if marker.exists() and "ranges" in (done := json.loads(marker.read_text())):
            table[scene] = [tuple(pair) for pair in done["ranges"]]
    order = [s for s in (HERE / "scenes_acre.txt").read_text(encoding="utf-8").split() if s in table]
    order += sorted(s for s in table if s not in order)
    STATS_TXT.write_text("\n".join(stats_line(s, table[s]) for s in order) + "\n", encoding="utf-8")


def process(
    scene: str, work: Path, gs_prefix: str, max_mbps: float, ranges: list[tuple[int, int]] | None, rgb: bool,
    pansharpened: bool = False,
) -> dict:
    m = SCENE_RE.match(scene)
    url = STAC_ITEM.format(date=m["date"], path=m["path"], row=m["row"]) if pansharpened else cog_url(scene)
    src = work / Path(cog_url(scene)).name  # the pansharpened file takes the fused one's name, as the rest of the pipeline expects
    out = work / f"{src.stem}_jpeg{QUALITY}.tif"
    uri = object_uri(gs_prefix, scene)
    for stale in (out, *stretch_intermediates(out)):  # left over by an interrupted run
        stale.unlink(missing_ok=True)

    timings = {}
    t0 = time.monotonic()
    if not src.exists():
        if pansharpened:
            pansharpen(scene, src, work, max_mbps)
        else:
            download(url, src, max_mbps)
    timings["download_s"] = round(time.monotonic() - t0)

    t0 = time.monotonic()
    with rasterio.Env(**GDAL_ENV):
        if pansharpened:
            ranges = preview_ranges(src)
        low, high = linear_bounds(src, ranges)
        write_stretched_cog(src, out, low, high, QUALITY, rgb)
    check_cog(out, rgb)
    timings["compress_s"] = round(time.monotonic() - t0)

    t0 = time.monotonic()
    stretch = "|".join(f"{lo}-{hi}" for lo, hi in zip(low, high))
    modifications = (PANSHARPENED_MODIFICATIONS if pansharpened else MODIFICATIONS).format("RGB" if rgb else "YCbCr")
    gcloud(
        "storage", "cp", str(out), uri,
        f"--custom-metadata=source={url},attribution={ATTRIBUTION},modifications={modifications},"
        f"linear_stretch={stretch}",
    )
    remote = int(gcloud("storage", "objects", "describe", uri, "--format=value(size)"))
    if remote != out.stat().st_size:
        raise RuntimeError(f"Uploaded size {remote} != local {out.stat().st_size} for {uri}")
    timings["upload_s"] = round(time.monotonic() - t0)

    result = {
        "scene": scene,
        "source": url,
        "uri": uri,
        "stretch": [[lo, hi] for lo, hi in zip(low, high)],
        "photometric": "RGB" if rgb else "YCbCr",
        **({"ranges": [[lo, hi] for lo, hi in ranges]} if pansharpened else {}),
        "original_bytes": src.stat().st_size,
        "cog_bytes": out.stat().st_size,
        **timings,
    }
    for done in (src, out):
        done.unlink(missing_ok=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenes", type=Path)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("gs_prefix", help="e.g. gs://my-bucket/linear (make_links.GS_PREFIX)")
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
    rgb_scenes = set(SCENES_RGB_TXT.read_text(encoding="utf-8").split()) if SCENES_RGB_TXT.exists() else set()
    pansharpened = set(SCENES_PANSHARPENED_TXT.read_text(encoding="utf-8").split()) if SCENES_PANSHARPENED_TXT.exists() else set()
    stats = read_stats(STATS_TXT)
    missing = [s for s in scenes if s not in stats and s not in pansharpened]
    if missing:
        sys.exit(f"stats.txt has no ranges for: {', '.join(missing)}")
    pending = [s for s in scenes if not (markers / f"{s}.json").exists()]
    already_done = len(scenes) - len(pending)
    if args.limit:
        pending = pending[: args.limit]
    log.info("%d scenes, %d already done, processing %d", len(scenes), already_done, len(pending))

    failed = []
    for i, scene in enumerate(pending, 1):
        log.info("[%d/%d] %s", i, len(pending), scene)
        try:
            result = process(
                scene, args.work_dir, args.gs_prefix, args.max_mbps, stats.get(scene), scene in rgb_scenes, scene in pansharpened,
            )
        except Exception as e:  # keep going; the scene is retried on the next run
            detail = e.stderr if isinstance(e, subprocess.CalledProcessError) else e
            log.exception("FAILED %s: %s", scene, detail)
            failed.append(scene)
        else:
            (markers / f"{scene}.json").write_text(json.dumps(result, indent=2))
            write_stretch_txt(scenes, markers)
            if "ranges" in result:
                write_stats_txt(scenes, markers)
            log.info(
                "OK %s: %.2f GB -> %.3f GB (%.1fx, %s), stretch %s, download %ss, compress %ss, upload %ss",
                scene, result["original_bytes"] / 1e9, result["cog_bytes"] / 1e9,
                result["original_bytes"] / result["cog_bytes"], result["photometric"], result["stretch"],
                result["download_s"], result["compress_s"], result["upload_s"],
            )
        if i < len(pending):
            time.sleep(PAUSE_S)

    log.info("Finished: %d ok, %d failed %s", len(pending) - len(failed), len(failed), failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
