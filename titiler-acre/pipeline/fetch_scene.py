"""Download one INPE CBERS-4A COG over a single HTTP connection.

A single streamed GET is far gentler on data.inpe.br than the thousands of
range requests a full /vsicurl/ read issues. The download resumes from a
.part file, backs off exponentially on 429/5xx and network errors (honouring
Retry-After), and can cap throughput.

Usage: python fetch_scene.py <SCENE_ID> <OUT_DIR> [--max-mbps 20]
"""

import argparse
import http.client
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import cog_url  # noqa: E402

CHUNK = 8 * 1024 * 1024
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 12
MAX_WAIT = 300


def backoff(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before the next attempt."""
    if retry_after and retry_after.isdigit():
        return min(MAX_WAIT, int(retry_after))
    return min(MAX_WAIT, 5 * 2**attempt) + random.uniform(0, 2)


def download(url: str, dest: Path, max_mbps: float) -> Path:
    """Download url to dest, resuming from dest.part if present."""
    part = dest.with_name(dest.name + ".part")
    total = None
    attempt = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        if total is not None and have >= total:
            break
        req = urllib.request.Request(
            url,
            headers={"Range": f"bytes={have}-", "User-Agent": "titiler-acre-pipeline/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 206:
                    total = int(r.headers["Content-Range"].rsplit("/", 1)[1])
                    mode = "ab"
                else:  # server ignored the range: start over
                    total, have, mode = int(r.headers["Content-Length"]), 0, "wb"
                print(f"  GET from byte {have:,} of {total:,}", flush=True)
                t0, got = time.monotonic(), 0
                with part.open(mode) as f:
                    while chunk := r.read(CHUNK):
                        f.write(chunk)
                        got += len(chunk)
                        if max_mbps:
                            ahead = got / (max_mbps * 1e6) - (time.monotonic() - t0)
                            if ahead > 0:
                                time.sleep(ahead)
                rate = got / 1e6 / max(time.monotonic() - t0, 1e-6)
                print(f"  received {got / 1e9:.2f} GB at {rate:.1f} MB/s", flush=True)
            attempt = 0
        except urllib.error.HTTPError as e:
            if e.code == 416 and total is not None:
                break
            if e.code not in RETRY_STATUS or attempt >= MAX_RETRIES:
                raise
            wait = backoff(attempt, e.headers.get("Retry-After"))
            print(f"  HTTP {e.code}, retry {attempt + 1} in {wait:.0f}s", flush=True)
            time.sleep(wait)
            attempt += 1
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as e:
            if attempt >= MAX_RETRIES:
                raise
            wait = backoff(attempt)
            print(f"  {type(e).__name__}: {e}, retry {attempt + 1} in {wait:.0f}s", flush=True)
            time.sleep(wait)
            attempt += 1

    size = part.stat().st_size
    if size != total:
        raise RuntimeError(f"Size mismatch for {url}: {size} != {total}")
    part.replace(dest)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scene", help="Scene id, YYYYMMDD_path_row")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--max-mbps", type=float, default=20, help="Throughput cap in MB/s (0 = none)")
    args = parser.parse_args()

    url = cog_url(args.scene)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    dest = args.out_dir / Path(url).name
    if dest.exists():
        print(f"{dest} already downloaded")
        return
    print(f"Downloading {url}")
    t0 = time.monotonic()
    download(url, dest, args.max_mbps)
    print(f"Done: {dest} ({dest.stat().st_size / 1e9:.2f} GB in {time.monotonic() - t0:.0f}s)")


if __name__ == "__main__":
    main()
