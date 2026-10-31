"""Offline tests of the pipeline's arithmetic, data files, download and retries.

Usage (from titiler-acre/, in the pipeline environment):
  python -m unittest discover -s pipeline -v
"""

import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_updates  # noqa: E402
import make_links  # noqa: E402
import pansharpen_scene  # noqa: E402
import process_all  # noqa: E402
import scene_stats  # noqa: E402
from check_bands import all_valid, band_correlations, red_gap, verdict  # noqa: E402
from check_service import range_problems, rescale_of  # noqa: E402
from fetch_scene import download  # noqa: E402
from make_links import HERE, STRETCH_TXT, read_stats, to_raw, to_stored, wmts_url  # noqa: E402

BODY = bytes(range(256)) * 40


class Ranged(BaseHTTPRequestHandler):
    """Static file with HTTP range requests, answering 416 past the end as servers do."""

    def do_GET(self):
        start = int(self.headers["Range"].removeprefix("bytes=").removesuffix("-"))
        if start >= len(BODY):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(BODY)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{len(BODY) - 1}/{len(BODY)}")
        self.send_header("Content-Length", str(len(BODY) - start))
        self.end_headers()
        self.wfile.write(BODY[start:])

    def log_message(self, *args):
        pass


class Stretch(unittest.TestCase):
    def test_to_raw_does_not_wrap_on_uint8(self):
        stored = np.array([0, 100, 200, 255], dtype="uint8")
        np.testing.assert_allclose(to_raw(stored, 23, 127), [23, 63.78, 104.57, 127], atol=0.01)

    def test_to_stored_inverts_to_raw(self):
        self.assertAlmostEqual(to_stored(to_raw(100, 23, 127), 23, 127), 100)


class DataFiles(unittest.TestCase):
    def setUp(self):
        self.scenes = (HERE / "scenes_acre.txt").read_text(encoding="utf-8").split()
        self.stats, self.stretch = read_stats(HERE / "stats.txt"), read_stats(STRETCH_TXT)

    def test_ranges_are_sane_and_fit_the_stretch(self):
        for scene in self.scenes:
            self.assertEqual(range_problems(self.stats[scene], self.stretch[scene]), [], scene)

    def test_rgb_scenes_are_in_use(self):
        rgb = process_all.SCENES_RGB_TXT.read_text(encoding="utf-8").split()
        self.assertLessEqual(set(rgb), set(self.scenes))

    def test_links_rescale_within_the_stored_range(self):
        for scene in self.scenes:
            url = wmts_url("https://example.test", scene, self.stats[scene], self.stretch[scene])
            pairs = [[float(v) for v in r.split(",")] for r in rescale_of(url)]
            self.assertEqual(len(pairs), 3, scene)
            self.assertTrue(all(0 <= lo < hi <= 255 for lo, hi in pairs), scene)


class RangeProblems(unittest.TestCase):
    def test_flags_the_one_level_ranges_of_the_overflow_bug(self):
        self.assertEqual(len(range_problems([(27, 28), (39, 40), (60, 61)], [(1, 200)] * 3)), 3)

    def test_flags_a_range_outside_the_stretch(self):
        self.assertEqual(len(range_problems([(30, 90), (30, 90), (30, 90)], [(40, 200), (1, 200), (1, 80)])), 2)


class CheckBands(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(0)
        self.unfused = self.rng.normal(size=(3, 20000))

    def gap(self, fused):
        return red_gap(band_correlations(fused, self.unfused))

    def test_matching_bands_are_ok(self):
        self.assertEqual(verdict(self.gap(self.unfused * 2 + 5)), "ok")

    def test_an_unrelated_red_band_is_defective(self):  # what 20251107_233_126 looks like
        fused = self.unfused.copy()
        fused[0] = self.rng.normal(size=20000)
        self.assertEqual(verdict(self.gap(fused)), "defective")

    def test_haze_noise_on_all_bands_leaves_the_gap(self):
        fused = self.unfused + self.rng.normal(scale=1.5, size=self.unfused.shape)
        self.assertEqual(verdict(self.gap(fused)), "ok")

    def test_all_valid_keeps_only_whole_blocks(self):
        mask = np.ones((6, 7), bool)
        mask[2, 3] = False
        out = all_valid(mask, 3)
        self.assertFalse(out[1:4, 2:5].any())  # every 3 x 3 block touching the hole
        self.assertTrue(out[1, 1] and out[4, 5])
        self.assertFalse(out[0].any() or out[-1].any() or out[:, 0].any() or out[:, -1].any())  # edges


class Pansharpen(unittest.TestCase):
    def test_a_flat_pan_leaves_the_colour_bands_times_the_gain(self):
        pan = np.full((32, 48), 300, "int16")
        ms = np.stack([np.full((8, 12), v, "int16") for v in (100, 150, 200)])
        out = pansharpen_scene.sharpen(pan, ms, gain=0.32)
        self.assertEqual(out[:, 16, 24].tolist(), [32, 48, 64])

    def test_the_pan_detail_is_added_to_every_band(self):
        pan = np.full((32, 48), 300, "int16")
        pan[16, 24] = 600  # a bright 2 m pixel in a flat 8 m one
        ms = np.full((3, 8, 12), 150, "int16")
        out = pansharpen_scene.sharpen(pan, ms, gain=0.32)
        self.assertGreater(out[:, 16, 24].min(), out[:, 16, 20].max() * 1.4)

    def test_no_data_is_zero_and_data_never_is(self):
        rng = np.random.default_rng(1)
        pan = rng.integers(150, 400, (64, 64)).astype("int16")
        ms = rng.integers(60, 200, (3, 16, 16)).astype("int16")
        pan[:, :8] = 0  # a scene edge
        ms[:, :, :2] = 0
        out = pansharpen_scene.sharpen(pan, ms)
        self.assertTrue((out[:, :, :8] == 0).all())
        self.assertTrue((out[:, :, 16:] > 0).all())

    def test_the_bicubic_kernel_does_not_pull_zeros_into_the_edge(self):
        pan = np.full((64, 64), 300, "int16")
        ms = np.full((3, 16, 16), 150, "int16")
        ms[:, :, :4] = 0
        out = pansharpen_scene.sharpen(pan, ms)
        edge = out[:, :, 4 * 4 + 2 * 4 :]  # beyond the eroded pixels
        self.assertEqual(set(np.unique(edge)), {48})

    def test_strips_give_the_same_scene_as_one_pass(self):
        rng = np.random.default_rng(2)
        h4, w4 = 40, 30
        pan = (rng.integers(150, 400, (4 * h4 + 3, 4 * w4 + 1))).astype("int16")  # INPE's pan is 3 rows and 1 column longer
        ms = [rng.integers(60, 200, (h4, w4)).astype("int16") for _ in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)

            def write(path, a, res):
                with rasterio.open(path, "w", driver="GTiff", dtype="int16", count=1, height=a.shape[0], width=a.shape[1],
                                   crs="EPSG:32718", transform=from_origin(500000, 9000000, res, res)) as dst:
                    dst.write(a, 1)

            write(tmp / "pan.tif", pan, 2)
            for i, a in enumerate(ms):
                write(tmp / f"ms{i}.tif", a, 8)
            for name, strip in (("small", 16), ("whole", 4 * h4)):
                pansharpen_scene.pansharpen(tmp / "pan.tif", [tmp / f"ms{i}.tif" for i in range(3)], tmp / f"{name}.tif", strip=strip)
            pansharpen_scene.add_overviews(tmp / "whole.tif")
            with rasterio.open(tmp / "whole.tif") as b:
                self.assertEqual(len(b.overviews(1)), len(pansharpen_scene.OVERVIEWS))  # GDAL rounds the factors on a raster this small
            with rasterio.open(tmp / "small.tif") as a, rasterio.open(tmp / "whole.tif") as b:
                small, whole = a.read().astype(int), b.read().astype(int)
                self.assertEqual((a.count, a.dtypes[0], a.nodata, a.shape), (3, "uint8", 0, pan.shape))
                self.assertEqual(a.transform, b.transform)
        self.assertLessEqual(np.abs(small - whole).max(), 1)
        self.assertLess((small != whole).mean(), 0.01)
        self.assertTrue((whole[:, -3:, :] == 0).all() and (whole[:, :, -1] == 0).all())  # the pixels beyond a multiple of 4
        self.assertTrue((whole[:, :4 * h4, :4 * w4][:, 4:-4, 4:-4] > 0).all())


class PansharpenBuild(unittest.TestCase):
    """build() must never leave a half-written scene under the final name."""

    def build(self, tmp, fake_pansharpen):
        urls = {b: f"https://example.test/band{b}.tif" for b in (0, 3, 2, 1)}

        def fake_download(url, dest, max_mbps):
            dest.write_bytes(b"band")
            return dest

        with (
            mock.patch.object(pansharpen_scene, "band_urls", return_value=urls),
            mock.patch.object(pansharpen_scene, "download", fake_download),
            mock.patch.object(pansharpen_scene, "pansharpen", fake_pansharpen),
            mock.patch.object(pansharpen_scene, "add_overviews"),
        ):
            pansharpen_scene.build("20221009_241_124", tmp / "scene.tif", tmp / "work", 20)
        return sorted(p.name for p in tmp.rglob("*") if p.is_file())

    def test_a_failure_leaves_no_scene_and_no_downloads(self):
        def fail(pan, ms, dest, **kwargs):
            dest.write_bytes(b"half")  # a real one is slow and can be killed here
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                self.build(Path(tmp), fail)
            self.assertEqual(sorted(p.name for p in Path(tmp).rglob("*") if p.is_file()), [])

    def test_the_scene_is_written_under_a_part_name_until_it_is_whole(self):
        seen = []

        def write(pan, ms, dest, **kwargs):
            seen.append(dest.name)
            dest.write_bytes(b"x")

        with tempfile.TemporaryDirectory() as tmp:
            left = self.build(Path(tmp), write)
        self.assertEqual(seen, ["scene.tif.part"])
        self.assertEqual(left, ["scene.tif"])  # renamed, and the downloads are gone


class ShiftedRanges(unittest.TestCase):
    def preview(self, dark, extra=0):
        a = np.zeros((3, 100, 100), "uint8")
        a[:, :, :50] = np.array(dark, "uint8")[:, None, None] + extra  # the right half is no data
        return a

    def test_the_typical_dark_level_gets_the_common_range(self):
        self.assertEqual(scene_stats.shifted_ranges(self.preview(scene_stats.TYPICAL_DARK)), list(scene_stats.COMMON_RANGE))

    def test_haze_shifts_every_band_by_its_own_offset(self):
        dark = np.array(scene_stats.TYPICAL_DARK) + [10, 20, -5]
        want = [(lo + o, hi + o) for (lo, hi), o in zip(scene_stats.COMMON_RANGE, [10, 20, -5])]
        self.assertEqual(scene_stats.shifted_ranges(self.preview(dark)), want)

    def test_clouds_do_not_set_the_dark_level(self):
        a = self.preview(scene_stats.TYPICAL_DARK)
        a[:, :, 10:40] = 250
        self.assertEqual(scene_stats.shifted_ranges(a), list(scene_stats.COMMON_RANGE))

    def test_stats_of_the_data_files_match_the_formula_for_the_replaced_scenes(self):
        # 20250706_233_125 is a July scene: the common range shifted by about -11, -11, -13
        stats = read_stats(HERE / "stats.txt")["20250706_233_125"]
        shifts = [lo - c[0] for (lo, _), c in zip(stats, scene_stats.COMMON_RANGE)]
        self.assertEqual(shifts, [-11, -11, -13])


class CheckUpdates(unittest.TestCase):
    def preview(self, ground, cloud_fraction=0.0, size=100):
        a = np.zeros((3, size, size), "uint8")
        a[:, :, : size // 2] = np.array(ground, "uint8")[:, None, None]  # the right half is no data
        n = int(size * (size // 2) * cloud_fraction)
        flat = a.reshape(3, -1)
        idx = np.flatnonzero((a[0] != 0).reshape(-1))[:n]
        flat[:, idx] = 250
        return flat.reshape(3, size, size)

    def test_item_ids_give_date_and_cell(self):
        self.assertEqual(check_updates.parse_item_id("CBERS4A_WPM_PCA_RGB321_20250806_233_126"), ("20250806", "233_126"))
        self.assertIsNone(check_updates.parse_item_id("something_else"))
        self.assertEqual((check_updates.cell_of("20250806_233_126"), check_updates.date_of("20250806_233_126")), ("233_126", "20250806"))

    def test_a_clear_scene_at_the_typical_dark_level_has_no_cloud_or_haze(self):
        m = check_updates.cloud_and_haze(self.preview((40, 52, 70)))
        self.assertEqual((m["cloud"], m["haze"], m["valid"]), (0.0, 0.0, 50.0))

    def test_cloud_percentage_and_haze_are_measured(self):
        m = check_updates.cloud_and_haze(self.preview((60, 72, 90), cloud_fraction=0.2))
        self.assertAlmostEqual(m["cloud"], 20.0, delta=0.5)
        self.assertAlmostEqual(m["haze"], 20.0, delta=0.5)

    def test_an_empty_scene_counts_as_all_cloud(self):
        self.assertEqual(check_updates.cloud_and_haze(np.zeros((3, 10, 10), "uint8"))["cloud"], 100.0)

    def test_a_newer_scene_is_an_upgrade_only_if_clearer(self):
        current = {"cloud": 6.0, "haze": 10.0, "valid": 60.0}  # score 11
        self.assertTrue(check_updates.is_upgrade({"cloud": 1.0, "haze": 5.0, "valid": 60.0}, current))
        self.assertFalse(check_updates.is_upgrade({"cloud": 5.0, "haze": 12.0, "valid": 60.0}, current))  # smokier: no better
        self.assertFalse(check_updates.is_upgrade({"cloud": 10.5, "haze": 0.0, "valid": 60.0}, current))

    def test_a_cell_with_no_fused_scene_takes_any_clear_enough_one(self):
        self.assertTrue(check_updates.is_upgrade({"cloud": 8.0, "haze": 10.0, "valid": 60.0}, None))
        self.assertFalse(check_updates.is_upgrade({"cloud": 12.0, "haze": 0.0, "valid": 60.0}, None))
        self.assertFalse(check_updates.is_upgrade({"cloud": 1.0, "haze": 40.0, "valid": 60.0}, None))  # smoke

    def test_snapshots_txt_has_what_the_checker_reads(self):
        snap = check_updates.read_snapshots()
        for key in ("deter", "focos", "car", "car_count", "prodes"):
            self.assertIn(key, snap)
        for key in ("deter", "focos", "car"):
            self.assertRegex(snap[key], r"^\d{4}-\d\d-\d\d$")
        self.assertIsInstance(int(snap["car_count"]), int)

    def test_hits_and_latest_read_the_wfs_answers(self):
        xml = b'<wfs:FeatureCollection numberMatched="1234" numberReturned="0"/>'
        self.assertEqual(check_updates.hits("http://x", fetch=lambda u: xml), 1234)
        body = b'{"features": [{"properties": {"view_date": "2026-10-20"}}]}'
        self.assertEqual(check_updates.latest("http://x", "l", "1=1", "view_date", fetch=lambda u: body), "2026-10-20")
        self.assertIsNone(check_updates.latest("http://x", "l", "1=1", "view_date", fetch=lambda u: b'{"features": []}'))

    def test_get_retries_a_server_error_but_not_a_client_error(self):
        calls = []

        def flaky(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise check_updates.urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

            class Ok:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return b"fine"
            return Ok()

        with mock.patch.object(check_updates.urllib.request, "urlopen", flaky), mock.patch.object(check_updates.time, "sleep"):
            self.assertEqual(check_updates.get("http://x"), b"fine")
        self.assertEqual(len(calls), 3)

        def not_found(req, timeout):
            raise check_updates.urllib.error.HTTPError(req.full_url, 404, "gone", {}, None)

        with mock.patch.object(check_updates.urllib.request, "urlopen", not_found), mock.patch.object(check_updates.time, "sleep"):
            with self.assertRaises(check_updates.urllib.error.HTTPError):
                check_updates.get("http://x")


class Download(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Ranged)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name) / "scene.tif"
        self.part = self.dest.with_name(self.dest.name + ".part")

    def fetch(self):
        download(f"http://127.0.0.1:{self.server.server_port}/scene.tif", self.dest, 0)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertFalse(self.part.exists())

    def test_fresh(self):
        self.fetch()

    def test_resumes_partial(self):
        self.part.write_bytes(BODY[:1000])
        self.fetch()

    def test_complete_part_is_kept(self):
        self.part.write_bytes(BODY)
        self.fetch()

    def test_part_longer_than_the_file_restarts(self):
        self.part.write_bytes(BODY + b"stale")
        self.fetch()


class GcloudRetries(unittest.TestCase):
    def setUp(self):
        self.error = subprocess.CalledProcessError(1, "gcloud", stderr="getaddrinfo failed")
        patcher = mock.patch.object(process_all.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_recovers_from_a_blip(self):
        done = subprocess.CompletedProcess("gcloud", 0, stdout="123\n")
        with mock.patch.object(process_all.subprocess, "run", side_effect=[self.error, done]) as run:
            self.assertEqual(process_all.gcloud("storage", "cp"), "123")
        self.assertEqual(run.call_count, 2)

    def test_gives_up_in_the_end(self):
        with mock.patch.object(process_all.subprocess, "run", side_effect=self.error) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                process_all.gcloud("storage", "cp")
        self.assertEqual(run.call_count, process_all.GCLOUD_RETRIES)


class Modifications(unittest.TestCase):
    def test_no_commas_and_the_real_quality(self):  # gcloud splits custom metadata on commas
        text = process_all.MODIFICATIONS.format("RGB")
        self.assertNotIn(",", text)
        self.assertIn(f"quality {process_all.QUALITY} (RGB", text)

    def test_pansharpened_text_has_no_commas_and_says_so(self):
        text = process_all.PANSHARPENED_MODIFICATIONS.format("RGB")
        self.assertNotIn(",", text)
        self.assertIn("Pansharpened", text)
        self.assertIn(f"quality {process_all.QUALITY} (RGB", text)

    def test_link_builder_keeps_the_bucket_prefix(self):
        self.assertTrue(make_links.gs_url("20260829_236_125").startswith(make_links.GS_PREFIX + "/v001/"))


if __name__ == "__main__":
    unittest.main()
