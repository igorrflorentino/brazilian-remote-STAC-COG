"""How much the old write path (clip at the stretch's low, then average the overviews) lifts the dark end.

write_stretched_cog now averages the overviews from the raw values, but copies written before
that still average after the clip. This tells which of them it matters for.

Reads 12 full-resolution 512 px windows per scene from INPE's original (range requests, a few MB),
and measures the share of valid pixels below the scene's stretch `low` and the mean amount they sit
below it. Averaging after the clip raises an overview's mean by about that amount, in raw DN;
multiplied by 255 / (rescale width) it is a lower bound of the shift in display levels (no gamma).
Measured on 2026-11-03: under 1 level in 20 of the 23 old copies, 1.7-3.7 in the other three,
which were rebuilt (see the README's Compression section).

Usage: python clip_lift.py <SCENES_TXT>
"""

import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_links import STRETCH_TXT, cog_url, read_stats  # noqa: E402
from scene_stats import GDAL_ENV  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
WINDOW = 512
WINDOWS = 12
PREVIEW = 32  # preview factor used to find valid windows
PAUSE_S = 2  # between scenes, to stay gentle with data.inpe.br


def windows_of(src, rng):
    """Full-resolution windows centred on valid preview pixels, in random order."""
    h, w = src.height // PREVIEW, src.width // PREVIEW
    prev = src.read(out_shape=(3, h, w), resampling=Resampling.nearest)
    ys, xs = np.nonzero((prev != 0).all(axis=0))
    for k in rng.permutation(len(ys)):
        y = min(max(ys[k] * PREVIEW - WINDOW // 2, 0), src.height - WINDOW)
        x = min(max(xs[k] * PREVIEW - WINDOW // 2, 0), src.width - WINDOW)
        yield Window(x, y, WINDOW, WINDOW)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python clip_lift.py <SCENES_TXT>")
    scenes = [s.strip() for s in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if s.strip()]
    stats, stretch = read_stats(HERE / "stats.txt"), read_stats(STRETCH_TXT)
    rows = []
    for scene in scenes:
        rng = np.random.default_rng(0)
        low = np.array([lo for lo, _ in stretch[scene]], dtype=float)
        width = np.array([hi - lo for lo, hi in stats[scene]], dtype=float)
        below, lift, valid_px, got = np.zeros(3), np.zeros(3), 0, 0
        with rasterio.Env(**GDAL_ENV), rasterio.open(cog_url(scene)) as src:
            for win in windows_of(src, rng):
                a = src.read(window=win).astype(float)
                valid = (a != 0).all(axis=0)
                if valid.mean() < 0.9:
                    continue
                v = a[:, valid]
                below += (v < low[:, None]).sum(axis=1)
                lift += np.maximum(low[:, None] - v, 0).sum(axis=1)
                valid_px += v.shape[1]
                got += 1
                if got >= WINDOWS:
                    break
        pct = 100 * below / max(valid_px, 1)
        disp = lift / max(valid_px, 1) * 255 / width
        rows.append((scene, got, pct.max(), disp.max()))
        print(f"{scene} windows={got} below_low_max={pct.max():5.2f}% lift_display>={disp.max():4.2f} levels", flush=True)
        time.sleep(PAUSE_S)
    rows.sort(key=lambda r: -r[3])
    print("--- by lift, largest first")
    for scene, got, pct, disp in rows:
        print(f"{scene} {disp:4.2f} levels, {pct:5.2f}% below low ({got} windows)")
    d = np.array([r[3] for r in rows])
    print(f"median {np.median(d):.2f}, max {d.max():.2f}; scenes above 1 level: {(d > 1).sum()}, above 2: {(d > 2).sum()}")


if __name__ == "__main__":
    main()
