"""Smoke-test the TiTiler service: WMTS capabilities, tiles and the URL guard.

For each scene: GetCapabilities with the scene's rescale and color formula
(as make_links.py builds it), then the tile at the scene centre at zooms 9, 13 and 16 through
the WMTS template, fetched twice (cold, then warm). Also checks that URLs
outside the bucket get 403, a missing object 404 and oversized tiles 422.

Usage: python check_service.py <BASE_URL> <STATS_TXT> <SCENE> [<SCENE> ...]
"""

import argparse
import statistics
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import morecantile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import GS_ROOT, cog_url, read_stats, wmts_url  # noqa: E402

NS = {"w": "http://www.opengis.net/wmts/1.0", "ows": "http://www.opengis.net/ows/1.1"}
ZOOMS = (9, 13, 16)
TMS = morecantile.tms.get("WebMercatorQuad")


def fetch(url: str):
    """Status, body, seconds and (case-insensitive) headers of a GET; status 0 on network errors."""
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.status, r.read(), time.perf_counter() - t0, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.perf_counter() - t0, e.headers
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, str(e).encode(), time.perf_counter() - t0, {}


def check_scene(base: str, scene: str, ranges) -> dict:
    status, body, t_caps, headers = fetch(wmts_url(base, scene, ranges))
    if status != 200 or "xml" not in headers.get("Content-Type", ""):
        return {"scene": scene, "error": f"capabilities {status}: {body[:200]!r}"}
    layers = ET.fromstring(body).findall(".//w:Contents/w:Layer", NS)
    if not layers:
        return {"scene": scene, "error": "capabilities without layers"}
    layer = layers[0]
    lo = [float(v) for v in layer.find("ows:WGS84BoundingBox/ows:LowerCorner", NS).text.split()]
    hi = [float(v) for v in layer.find("ows:WGS84BoundingBox/ows:UpperCorner", NS).text.split()]
    template = layer.find("w:ResourceURL", NS).get("template")
    result = {
        "scene": scene,
        "layers": len(layers),
        "caps_s": t_caps,
        "template_https": template.startswith("https://") or base.startswith("http://127."),
        "template_rescale": template.count("rescale="),
        "template_color": "color_formula=" in template,
    }
    for z in ZOOMS:
        t = TMS.tile((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, z)
        url = template.replace("{TileMatrix}", str(z)).replace("{TileCol}", str(t.x)).replace("{TileRow}", str(t.y))
        runs = [fetch(url) for _ in range(2)]
        if any(s != 200 or b[:4] != b"\x89PNG" for s, b, _, _ in runs):
            result[f"z{z}"] = f"error {[s for s, *_ in runs]}"
        else:
            result[f"z{z}"] = (runs[0][2], runs[1][2], len(runs[1][1]))
        result["tile_url"] = url  # last zoom; reused by the tile size limit checks
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("stats", type=Path)
    parser.add_argument("scenes", nargs="+")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    stats = read_stats(args.stats)

    cold, warm, ok, tile_url = [], [], True, None
    scenes = [s.strip() for s in args.scenes]  # $(cat scenes.txt) keeps \r on CRLF checkouts
    for scene in scenes:
        r = check_scene(base, scene, stats[scene])
        if "error" in r:
            print(f"FAIL {scene}: {r['error']}")
            ok = False
            continue
        tile_url = tile_url or r["tile_url"]
        cells = []
        for z in ZOOMS:
            v = r[f"z{z}"]
            if isinstance(v, str):
                ok = False
                cells.append(f"z{z} {v}")
            else:
                cold.append(v[0])
                warm.append(v[1])
                cells.append(f"z{z} cold {v[0]:.2f}s warm {v[1]:.2f}s {v[2] // 1024}KB")
        ok &= r["layers"] == 1 and r["template_https"] and r["template_rescale"] == 3 and r["template_color"]
        print(
            f"{scene}: caps {r['caps_s']:.2f}s layers={r['layers']} https={r['template_https']} "
            f"rescale={r['template_rescale']} color={r['template_color']} | " + " | ".join(cells)
        )
    if warm:
        print(f"tiles: cold mean {statistics.mean(cold):.2f}s max {max(cold):.2f}s | warm mean {statistics.mean(warm):.2f}s max {max(warm):.2f}s")

    scene = scenes[0]
    guard = [
        ("INPE URL", cog_url(scene), 403),
        ("other bucket", "gs://some-other-bucket/x.tif", 403),
        ("bucket name prefix trick", GS_ROOT + "-evil/x.tif", 403),
        ("traversal", GS_ROOT + "/v001/../../other/x.tif", 403),
        ("https object URL", "https://storage.googleapis.com/" + GS_ROOT[5:] + "/x.tif", 403),
        ("missing object", GS_ROOT + "/v001/999/999/2026/1/missing.tif", 404),
    ]
    checks = [(name, f"{base}/cog/info?url={quote(url, safe='')}", expected) for name, url, expected in guard]
    if tile_url:
        checks += [
            ("tilesize at limit", tile_url + "&tilesize=1024", 200),
            ("tilesize above limit", tile_url + "&tilesize=1025", 422),
            ("buffer above limit", tile_url + "&buffer=64.5", 422),
            ("padding above limit", tile_url + "&padding=65", 422),
        ]
    else:
        print("FAIL tile size limits: no scene returned a tile")
        ok = False
    for name, url, expected in checks:
        status, body, _, _ = fetch(url)
        good = status == expected
        ok &= good
        print(f"{'OK  ' if good else 'FAIL'} {name}: {status} (expected {expected})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
