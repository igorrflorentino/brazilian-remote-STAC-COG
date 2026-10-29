"""Build WMTS GetCapabilities links for every scene in scenes_acre.txt.

Links point at the JPEG COG copies in the private bucket and carry each
scene's per-band rescale from stats.txt, plus a gamma that lifts the dark
forest without clipping rivers and pasture. They omit nodata=0: the COGs
carry an internal mask, and nodata=0 would bypass it and show JPEG edge
noise as a black fringe.

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
SCENE_RE = re.compile(r"^(?P<date>(?P<yyyy>\d{4})(?P<mm>\d{2})\d{2})_(?P<path>\d{3})_(?P<row>\d{3})$")
STATS_RE = re.compile(r"^(?P<scene>\d{8}_\d{3}_\d{3}):(?P<ranges>\d+-\d+(?:\|\d+-\d+){2})$")
COLOR_FORMULA = "gamma RGB 1.3"  # rio-color, applied by TiTiler after the rescale
HERE = Path(__file__).resolve().parent


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
    return f"{GS_ROOT}/{scene_path(scene)}"


def read_stats(path: Path) -> dict[str, list[tuple[int, int]]]:
    """Parse stats.txt lines of the form YYYYMMDD_path_row:lo-hi|lo-hi|lo-hi."""
    stats = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        m = STATS_RE.match(line.strip())
        if not m:
            raise ValueError(f"Invalid stats line {line!r}")
        stats[m["scene"]] = [tuple(int(v) for v in r.split("-")) for r in m["ranges"].split("|")]
    return stats


def wmts_url(base_url: str, scene: str, ranges: list[tuple[int, int]]) -> str:
    """Return the WMTS GetCapabilities URL for a scene."""
    encoded = quote(gs_url(scene), safe="")
    rescale = "".join(f"&rescale={lo},{hi}" for lo, hi in ranges)
    return (
        f"{base_url}/cog/WMTSCapabilities.xml?url={encoded}&bidx=1&bidx=2&bidx=3{rescale}"
        f"&color_formula={quote(COLOR_FORMULA)}"
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
    missing = [s for s in scenes if s not in stats]
    if missing:
        sys.exit(f"stats.txt has no ranges for: {', '.join(missing)}")

    out = HERE / "wmts_links.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scene", "wmts_url"])
        for scene in scenes:
            writer.writerow([scene, wmts_url(base_url, scene, stats[scene])])

    print(f"Wrote {len(scenes)} links to {out}")


if __name__ == "__main__":
    main()
