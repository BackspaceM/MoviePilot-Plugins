"""单源追剧的纯函数用例，跑的时候不需要 MoviePilot。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins.v3"))

from singlesourcetracker import fingerprint, renderer, store  # noqa: E402

DIMS = list(fingerprint.DEFAULT_DIMENSIONS)


class FingerprintTests(unittest.TestCase):
    def test_dolby_vision_is_not_hdr(self):
        dv = fingerprint.parse("Show.S01E01.2160p.WEB-DL.DV.HDR10+.H.265.Atmos-GROUP")
        hdr = fingerprint.parse("Show.S01E02.2160p.WEB-DL.HDR.H.265.Atmos-GROUP")
        self.assertEqual(dv["hdr"], "DV")
        self.assertEqual(hdr["hdr"], "HDR")
        self.assertNotEqual(dv["hdr"], hdr["hdr"])

    def test_chinese_dolby_vision(self):
        fp = fingerprint.parse("某剧 第1集 杜比视界 4K HEVC")
        self.assertEqual(fp["hdr"], "DV")
        self.assertEqual(fp["codec"], "H.265")
        self.assertEqual(fp["resolution"], "2160p")

    def test_codec_variants(self):
        cases = {
            "Movie.2024.1080p.x264.AAC": "H.264",
            "Movie.2024.1080p.H 264.AAC": "H.264",
            "Movie.2024.1080p.x265.AAC": "H.265",
            "Movie.2024.1080p.H.265.10bit": "H.265",
            "Movie.2024.1080p.AV1": "AV1",
        }
        for title, expected in cases.items():
            self.assertEqual(fingerprint.parse(title)["codec"], expected, title)

    def test_fps_and_bitrate(self):
        fp = fingerprint.parse("Show.S01E03.1080p.60fps.高码.WEB-DL.H264")
        self.assertEqual(fp["fps"], "60fps")
        self.assertEqual(fp["bitrate"], "高码")
        self.assertEqual(fp["codec"], "H.264")

    def test_ten_bit_suffix(self):
        fp = fingerprint.parse("Show.S01E01.2160p.10bit.HDR.x265.Remux")
        self.assertIn("10bit", fp["bitrate"])
        self.assertEqual(fp["source"], "Remux")

    def test_source_priority(self):
        self.assertEqual(fingerprint.parse("A.BluRay.Remux.1080p")["source"], "Remux")
        self.assertEqual(fingerprint.parse("A.WEB-DL.1080p")["source"], "WEB-DL")
        self.assertEqual(fingerprint.parse("A.HDTV.720p")["source"], "HDTV")

    def test_empty_and_noise(self):
        fp = fingerprint.parse("")
        self.assertTrue(all(value == "" for value in fp.values()))
        self.assertEqual(fingerprint.parse("随便一个中文剧名")["hdr"], "")

    def test_compare_levels(self):
        baseline = fingerprint.parse("Show.S01E01.2160p.DV.H.265.WEB-DL")
        actual = fingerprint.parse("Show.S01E02.1080p.HDR.H.264.WEBRip")
        issues = fingerprint.compare(baseline, actual, DIMS)
        self.assertTrue(any(item["level"] == "conflict" for item in issues))
        self.assertGreaterEqual(len([i for i in issues if i["level"] == "conflict"]), 4)

    def test_missing_is_not_conflict(self):
        baseline = fingerprint.parse("Show.S01E01.2160p.DV.H.265.WEB-DL")
        actual = fingerprint.parse("Show.S01E02.2160p.H.265.WEB-DL")
        issues = fingerprint.compare(baseline, actual, DIMS)
        self.assertTrue(any(item["level"] == "missing" for item in issues))
        self.assertFalse(any(item["level"] == "conflict" for item in issues))

    def test_identical_titles(self):
        fp = fingerprint.parse("Show.S01E01.2160p.DV.H.265.WEB-DL")
        self.assertEqual(fingerprint.compare(fp, dict(fp), DIMS), [])
        self.assertIn("2160p", fingerprint.brief(fp, DIMS))


class StoreTests(unittest.TestCase):
    def _binding(self):
        return {
            "sid": 12, "name": "夜航西飞", "season": 1,
            "site_id": 3, "site_name": "某某站", "domain": "example.org",
            "original_sites": [1, 2], "locked": True,
            "baseline": fingerprint.parse("夜航西飞.S01E01.2160p.DV.H.265.WEB-DL"),
        }

    def test_record_matches(self):
        binding = self._binding()
        self.assertTrue(store.record_matches(
            {"title": "夜航西飞", "seasons": "1", "episodes": "1"}, binding))
        self.assertTrue(store.record_matches(
            {"title": "夜航西飞 第一季", "seasons": "S01", "episodes": "2"}, binding))
        self.assertFalse(store.record_matches(
            {"title": "夜航西飞", "seasons": "2", "episodes": "1"}, binding))
        self.assertFalse(store.record_matches(
            {"title": "别的剧", "seasons": "1"}, binding))

    def test_site_state(self):
        binding = self._binding()
        self.assertTrue(store.site_state("3", binding))
        self.assertTrue(store.site_state("某某站", binding))
        self.assertTrue(store.site_state("example.org", binding))
        self.assertFalse(store.site_state("别的站", binding))
        self.assertIsNone(store.site_state("", binding))

    def test_pick_baseline_uses_latest_record(self):
        records = [
            {"torrent_name": "夜航西飞.S01E01.2160p.DV.H.265.WEB-DL"},
            {"torrent_name": "夜航西飞.S01E02.1080p.HDR.H.264.WEBRip"},
        ]
        fp = store.pick_baseline(records, DIMS)
        self.assertEqual(fp["codec"], "H.264")

    def test_inspect_reports_site_and_quality(self):
        binding = self._binding()
        records = [
            {"title": "夜航西飞", "seasons": "1", "episodes": "1",
             "torrent_site": "某某站", "torrent_name": "夜航西飞.S01E01.2160p.DV.H.265.WEB-DL",
             "download_hash": "aaa", "downloader": "qb"},
            {"title": "夜航西飞", "seasons": "1", "episodes": "2",
             "torrent_site": "别的站", "torrent_name": "夜航西飞.S01E02.1080p.HDR.H.264.WEBRip",
             "download_hash": "bbb", "downloader": "qb"},
        ]
        result = store.inspect([binding], [{"id": 12, "name": "夜航西飞", "season": 1}], records, DIMS)
        self.assertEqual(result["stats"]["bindings"], 1)
        self.assertGreaterEqual(result["stats"]["conflicts"], 1)
        issue = result["issues"][0]
        self.assertEqual(issue["level"], "conflict")
        self.assertIn("站点", " ".join(issue["reasons"]))
        self.assertTrue(result["pending"] == [])

    def test_inspect_clean_library(self):
        binding = self._binding()
        records = [
            {"title": "夜航西飞", "seasons": "1", "episodes": "1",
             "torrent_site": "3", "torrent_name": "夜航西飞.S01E01.2160p.DV.H.265.WEB-DL"},
        ]
        result = store.inspect([binding], [], records, DIMS)
        self.assertEqual(result["stats"]["conflicts"], 0)
        self.assertEqual(result["issues"], [])


class RenderTests(unittest.TestCase):
    def test_escape(self):
        html = renderer.render({
            "stats": {"bindings": 1, "conflicts": 0},
            "rows": [{"name": "<script>alert(1)</script>", "site_name": "A&B", "baseline": "DV"}],
            "issues": [], "pending": [], "log": ["<b>x</b>"],
        })
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("A&amp;B", html)
        self.assertNotIn("<b>x</b>", html)

    def test_weird_payload(self):
        for value in ({}, {"rows": "oops", "stats": 5}, {"issues": [1, 2]}):
            self.assertIn("单源追剧", renderer.render(value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
