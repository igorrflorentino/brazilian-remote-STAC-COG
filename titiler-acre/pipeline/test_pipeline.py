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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import make_links  # noqa: E402
import process_all  # noqa: E402
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

    def test_link_builder_keeps_the_bucket_prefix(self):
        self.assertTrue(make_links.gs_url("20260829_236_125").startswith(make_links.GS_PREFIX + "/v001/"))


if __name__ == "__main__":
    unittest.main()
