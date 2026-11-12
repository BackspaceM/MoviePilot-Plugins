"""规则和巡检的纯函数用例，跑的时候不需要 MoviePilot。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins.v3"))

from singlesourcetracker import renderer, rules, store  # noqa: E402


class FakePlugin:
    def __init__(self):
        self._data = {}

    def get_data(self, key, default=None):
        return self._data.get(key, default)

    def save_data(self, key, value):
        self._data[key] = value


SITES = [
    {"id": 1, "name": "A站", "domain": "a.example.org"},
    {"id": 2, "name": "B站", "domain": "b.example.org"},
]

RULES_TEXT = (
    "海贼王 => A站 => HDR;;DDP5.1;;XX制作组 => re:(?i)x264;;CAM\n"
    "火影忍者 => B站 => 杜比视界;;HDR;;H.265 => re:(?i)(HDTV|TS)\n"
    "#12 => B站 => 2160p => 低码\n"
)


class RuleTests(unittest.TestCase):
    def test_parse_rules(self):
        parsed, errors = rules.parse_rules(RULES_TEXT)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0]["site"], "A站")
        self.assertEqual(len(parsed[0]["include"]), 3)
        self.assertEqual(parsed[0]["exclude"][0]["kind"], "regex")
        self.assertEqual(parsed[2]["target_kind"], "id")

    def test_bad_lines_are_reported(self):
        parsed, errors = rules.parse_rules("只有一段\n剧名 => A站 => re:[( => 排除\n# 注释")
        self.assertEqual(len(parsed), 1)
        self.assertTrue(any("少了分段" in item for item in errors))
        self.assertTrue(any("正则写错了" in item for item in errors))

    def test_target_variants(self):
        self.assertEqual(rules.parse_target("#12")["kind"], "id")
        self.assertEqual(rules.parse_target("海贼王#S1")["season"], 1)
        self.assertEqual(rules.parse_target("海贼王#2")["season"], 2)
        self.assertEqual(rules.parse_target("海贼王")["kind"], "name")

    def test_match_by_name_id_and_season(self):
        parsed, _ = rules.parse_rules(RULES_TEXT)
        self.assertTrue(rules.rule_matches(parsed[0], {"id": 1, "name": "海贼王", "season": 1}))
        self.assertTrue(rules.rule_matches(parsed[2], {"id": 12, "name": "别的剧", "season": 1}))
        self.assertFalse(rules.rule_matches(parsed[2], {"id": 13, "name": "别的剧", "season": 1}))
        season_rule, _ = rules.parse_rules("海贼王#S2 => A站 => HDR => ")
        self.assertFalse(rules.rule_matches(season_rule[0], {"id": 1, "name": "海贼王", "season": 1}))
        self.assertTrue(rules.rule_matches(season_rule[0], {"id": 1, "name": "海贼王", "season": 2}))

    def test_include_all_exclude_any(self):
        parsed, _ = rules.parse_rules(RULES_TEXT)
        rule = parsed[0]
        self.assertEqual(rules.check_title(rule, "One.Piece.S01E01.HDR.DDP5.1.XX制作组.1080p"), [])
        problems = rules.check_title(rule, "One.Piece.S01E02.DDP5.1.XX制作组.1080p")
        self.assertTrue(any("缺少必含" in item for item in problems))
        problems = rules.check_title(rule, "One.Piece.S01E03.HDR.DDP5.1.XX制作组.x264")
        self.assertTrue(any("命中排除" in item for item in problems))

    def test_chinese_and_dolby(self):
        parsed, _ = rules.parse_rules(RULES_TEXT)
        rule = parsed[1]
        self.assertEqual(rules.check_title(rule, "火影.S01E01.杜比视界.HDR.H.265.2160p"), [])
        self.assertTrue(rules.check_title(rule, "火影.S01E02.杜比视界.HDR.H.265.HDTV"))

    def test_site_check(self):
        parsed, _ = rules.parse_rules(RULES_TEXT)
        rule = parsed[0]
        self.assertTrue(rules.check_site(rule, "A站", SITES))
        self.assertTrue(rules.check_site(rule, "1", SITES))
        self.assertFalse(rules.check_site(rule, "B站", SITES))
        self.assertIsNone(rules.check_site(rule, "", SITES))
        plain, _ = rules.parse_rules("剧名 =>  => HDR => ")
        self.assertIsNone(rules.check_site(plain[0], "任意站", SITES))

    def test_template_skips_configured(self):
        parsed, _ = rules.parse_rules(RULES_TEXT)
        text = rules.template_for([
            {"id": 1, "name": "海贼王", "season": 1},
            {"id": 9, "name": "没配的剧", "season": 3},
        ], parsed)
        self.assertNotIn("海贼王", text)
        self.assertIn("没配的剧#S3", text)


class InspectTests(unittest.TestCase):
    def _data(self):
        parsed, errors = rules.parse_rules(RULES_TEXT)
        subs = [
            {"id": 1, "name": "海贼王", "season": 1, "state": "R", "sites": []},
            {"id": 2, "name": "火影忍者", "season": 1, "state": "R", "sites": [2]},
            {"id": 3, "name": "没配的剧", "season": 1, "state": "N", "sites": []},
        ]
        records = [
            {"title": "海贼王", "seasons": "1", "episodes": "1",
             "torrent_site": "A站", "download_hash": "aaa", "downloader": "qb",
             "torrent_name": "One.Piece.S01E01.HDR.DDP5.1.XX制作组.1080p"},
            {"title": "海贼王", "seasons": "1", "episodes": "2",
             "torrent_site": "B站", "download_hash": "bbb", "downloader": "qb",
             "torrent_name": "One.Piece.S01E02.HDR.DDP5.1.XX制作组.x264"},
            {"title": "没配的剧", "seasons": "1", "episodes": "1",
             "torrent_site": "A站", "download_hash": "ccc", "downloader": "qb",
             "torrent_name": "随便什么标题"},
        ]
        return parsed, errors, subs, records

    def test_inspect_finds_site_and_title_problems(self):
        parsed, errors, subs, records = self._data()
        result = store.inspect(parsed, subs, records, SITES, errors)
        self.assertEqual(result["stats"]["subscriptions"], 3)
        self.assertEqual(result["stats"]["ruled"], 2)
        self.assertEqual(len(result["issues"]), 1)
        issue = result["issues"][0]
        self.assertIn("站点不符", " ".join(issue["reasons"]))
        self.assertTrue(any("命中排除" in item for item in issue["reasons"]))
        self.assertEqual(result["subscriptions"][2]["rule"], "")

    def test_inspect_jsonable(self):
        parsed, errors, subs, records = self._data()
        result = store.inspect(parsed, subs, records, SITES, errors)
        restored = store._jsonable(result)
        self.assertEqual(restored["stats"], result["stats"])
        self.assertIn("单源追剧", renderer.render(restored))

    def test_lock_and_unlock(self):
        parsed, _, subs, _ = self._data()
        plugin = FakePlugin()
        original = {"set_sites": store.set_sites, "append_log": store.append_log}
        calls = []
        store.set_sites = lambda sid, sites: (calls.append((str(sid), sites)) or (True, ""))
        store.append_log = lambda p, text, limit=40: None
        try:
            done, messages = store.lock_sites(plugin, parsed, subs, SITES)
            self.assertEqual(done, 2)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(len(sites) == 1 for _, sites in calls))
            saved = store.bindings(plugin)
            self.assertIn("2", saved)
            unlocked, _ = store.unlock_sites(plugin)
            self.assertEqual(unlocked, 2)
        finally:
            store.set_sites = original["set_sites"]
            store.append_log = original["append_log"]


class RenderTests(unittest.TestCase):
    def test_escape(self):
        html = renderer.render({
            "subscriptions": [{"name": "<script>alert(1)</script>", "sites": "A&B"}],
            "rules": [{"target": "<img src=x>", "summary": "DV"}],
            "issues": [{"name": "<b>x</b>", "reasons": ["<i>y</i>"], "title": "z"}],
            "log": ["<u>log</u>"],
        })
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("A&amp;B", html)
        self.assertNotIn("<img src=x>", html)

    def test_weird_payload(self):
        for value in ({}, {"subscriptions": "oops", "stats": 5}, {"issues": [1, 2]}):
            self.assertIn("单源追剧", renderer.render(value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
