"""Build WMTS GetCapabilities links for every scene in scenes_acre.txt.

Links point at the JPEG COG copies in the private bucket and carry each
scene's per-band rescale from stats.txt, plus a gamma that lifts the dark
forest without clipping rivers and pasture. They omit nodata=0: the COGs
carry an internal mask, and nodata=0 would bypass it and show JPEG edge
noise as a black fringe.

The copies are stored linearly stretched (stretch.txt), so the rescale, kept
in INPE's raw values in stats.txt, is converted to stored values here. Tiles
are lossless WebP.

Usage: python make_links.py <BASE_URL>
Writes wmts_links.csv (columns: scene, wmts_url) next to this script.
"""

import csv
import re
import sys
from pathlib import Path
from urllib.parse import quote

INPE_ROOT = "https://data.inpe.br/bdc/data/CB4A-WPM-PCA-FUSED"
GS_ROOT = "gs://brazilian-remote-stac-cog-cbers4a-wpm"
# Linearly stretched copies (see stretch.txt); INPE's layout continues below this prefix.
GS_PREFIX = f"{GS_ROOT}/linear"
SCENE_RE = re.compile(r"^(?P<date>(?P<yyyy>\d{4})(?P<mm>\d{2})\d{2})_(?P<path>\d{3})_(?P<row>\d{3})$")
STATS_RE = re.compile(r"^(?P<scene>\d{8}_\d{3}_\d{3}):(?P<ranges>\d+-\d+(?:\|\d+-\d+){2})$")
COLOR_FORMULA = "gamma RGB 1.3"  # rio-color, applied by TiTiler after the rescale
TILE_FORMAT = "webp"  # lossless in app.py: PNG's pixels at about half the size
HERE = Path(__file__).resolve().parent
STRETCH_TXT = HERE / "stretch.txt"


def scene_path(scene: str) -> str:
    """Path below the collection root for a scene id in YYYYMMDD_path_row form."""
    m = SCENE_RE.match(scene)
    if not m:
        raise ValueError(f"Invalid scene id {scene!r}, expected YYYYMMDD_path_row")
    # INPE month folders are not zero-padded (.../2023/7/...), as listed in the
    # BDC STAC collection CB4A-WPM-PCA-FUSED-1; the bucket keeps the same layout.
    month = int(m["mm"])
    return (
        f"v001/{m['path']}/{m['row']}/{m['yyyy']}/{month}/"
        f"CBERS4A_WPM_PCA_RGB321_{m['date']}_{m['path']}_{m['row']}.tif"
    )


def cog_url(scene: str) -> str:
    """INPE URL of the original (lossless) COG."""
    return f"{INPE_ROOT}/{scene_path(scene)}"


def gs_url(scene: str) -> str:
    """Bucket URL of the JPEG COG copy."""
    return f"{GS_PREFIX}/{scene_path(scene)}"


def to_stored(raw: float, low: int, high: int) -> float:
    """Raw value -> value in the stretched copy: low maps to 0 and high to 255."""
    return (raw - low) * 255 / (high - low)


def to_raw(stored: float, low: int, high: int) -> float:
    """Value in the stretched copy -> raw value (inverse of to_stored).

    The scale is computed first: NumPy 2 keeps uint8 * int as uint8, so
    stored * (high - low) would wrap around on a uint8 array.
    """
    return low + stored * ((high - low) / 255)


def read_stats(path: Path) -> dict[str, list[tuple[int, int]]]:
    """Parse stats.txt or stretch.txt lines of the form YYYYMMDD_path_row:lo-hi|lo-hi|lo-hi."""
    stats = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        m = STATS_RE.match(line.strip())
        if not m:
            raise ValueError(f"Invalid stats line {line!r}")
        stats[m["scene"]] = [tuple(int(v) for v in r.split("-")) for r in m["ranges"].split("|")]
    return stats


def wmts_url(base_url: str, scene: str, ranges: list[tuple[int, int]], stretch: list[tuple[int, int]]) -> str:
    """Return the WMTS GetCapabilities URL for a scene (raw-value ranges, the copy's stretch)."""
    encoded = quote(gs_url(scene), safe="")
    rescale = "".join(
        f"&rescale={round(to_stored(lo, low, high), 2):g},{round(to_stored(hi, low, high), 2):g}"
        for (lo, hi), (low, high) in zip(ranges, stretch)
    )
    return (
        f"{base_url}/cog/WMTSCapabilities.xml?url={encoded}&bidx=1&bidx=2&bidx=3{rescale}"
        f"&color_formula={quote(COLOR_FORMULA)}&tile_format={TILE_FORMAT}"
    )


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python make_links.py <BASE_URL>")
    base_url = sys.argv[1].rstrip("/")

    scenes = [
        line.strip()
        for line in (HERE / "scenes_acre.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stats = read_stats(HERE / "stats.txt")
    stretch = read_stats(STRETCH_TXT)
    for name, table in (("stats.txt", stats), ("stretch.txt", stretch)):
        missing = [s for s in scenes if s not in table]
        if missing:
            sys.exit(f"{name} has no ranges for: {', '.join(missing)}")

    out = HERE / "wmts_links.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scene", "wmts_url"])
        for scene in scenes:
            writer.writerow([scene, wmts_url(base_url, scene, stats[scene], stretch[scene])])

    print(f"Wrote {len(scenes)} links to {out}")


if __name__ == "__main__":
    main()
