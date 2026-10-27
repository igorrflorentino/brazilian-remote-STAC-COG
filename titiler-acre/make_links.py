"""Build WMTS GetCapabilities links for every scene in scenes.txt.

Usage: python make_links.py <BASE_URL>
Writes wmts_links.csv (columns: scene, wmts_url) next to this script.
"""

import csv
import re
import sys
from pathlib import Path
from urllib.parse import quote

COG_ROOT = "https://data.inpe.br/bdc/data/CB4A-WPM-PCA-FUSED/v001"
SCENE_RE = re.compile(r"^(?P<date>(?P<yyyy>\d{4})(?P<mm>\d{2})\d{2})_(?P<path>\d{3})_(?P<row>\d{3})$")
HERE = Path(__file__).resolve().parent


def cog_url(scene: str) -> str:
    """Return the INPE COG URL for a scene id in YYYYMMDD_path_row form."""
    m = SCENE_RE.match(scene)
    if not m:
        raise ValueError(f"Invalid scene id {scene!r}, expected YYYYMMDD_path_row")
    # INPE month folders are not zero-padded (.../2023/7/...), as listed in the
    # BDC STAC collection CB4A-WPM-PCA-FUSED-1.
    month = int(m["mm"])
    return (
        f"{COG_ROOT}/{m['path']}/{m['row']}/{m['yyyy']}/{month}/"
        f"CBERS4A_WPM_PCA_RGB321_{m['date']}_{m['path']}_{m['row']}.tif"
    )


def wmts_url(base_url: str, scene: str) -> str:
    """Return the WMTS GetCapabilities URL for a scene."""
    encoded = quote(cog_url(scene), safe="")
    return f"{base_url}/cog/WMTSCapabilities.xml?url={encoded}&bidx=1&bidx=2&bidx=3"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python make_links.py <BASE_URL>")
    base_url = sys.argv[1].rstrip("/")

    scenes = [
        line.strip()
        for line in (HERE / "scenes.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    out = HERE / "wmts_links.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scene", "wmts_url"])
        for scene in scenes:
            writer.writerow([scene, wmts_url(base_url, scene)])

    print(f"Wrote {len(scenes)} links to {out}")


if __name__ == "__main__":
    main()
