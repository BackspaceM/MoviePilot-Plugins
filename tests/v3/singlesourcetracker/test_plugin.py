"""规则生成、写回订阅与巡检的纯函数用例，跑的时候不需要 MoviePilot。"""

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
SUBS = [
    {"id": 11, "name": "海贼王", "season": 1, "state": "R", "sites": [], "include": "", "exclude": ""},
    {"id": 12, "name": "火影忍者", "season": 1, "state": "R", "sites": [], "include": "", "exclude": ""},
]
CATALOG = {"subscriptions": SUBS, "sites": SITES, "scanned_at": "2026-11-12 18:00:00"}


class RegexTests(unittest.TestCase):
    def test_word_pattern_tolerates_separators(self):
        pattern = rules.word_pattern("H.265")
        self.assertIn("H", pattern)
        self.assertTrue(rules._compile(pattern).search("H.265"))
        self.assertTrue(rules._compile(pattern).search("H 265"))
        self.assertTrue(rules._compile(pattern).search("H265"))

    def test_include_regex_requires_all(self):
        regex, display = rules.build_include_regex(["HDR", "DDP5.1"], "XX制作组")
        pattern = rules._compile(regex)
        self.assertIsNotNone(pattern)
        self.assertTrue(pattern.search("Show.HDR.DDP5.1.XX制作组.1080p"))
        self.assertFalse(pattern.search("Show.HDR.DDP5.1.1080p"))
        self.assertEqual(len(display), 3)

    def test_dolby_vision_and_h265_tags(self):
        regex, _ = rules.build_include_regex(["杜比视界", "H.265"], "")
        pattern = rules._compile(regex)
        self.assertTrue(pattern.search("Show.DV.HDR.x265.2160p"))
        self.assertTrue(pattern.search("Show.Dolby.Vision.HEVC.2160p"))
        self.assertFalse(pattern.search("Show.HDR.x264.2160p"))

    def test_exclude_regex_hits_any(self):
        regex, display, errors = rules.build_exclude_regex("x264;;re:(?i)(HDTV|TS);;CAM")
        self.assertEqual(errors, [])
        pattern = rules._compile(regex)
        self.assertTrue(pattern.search("Show.x264.1080p"))
        self.assertTrue(pattern.search("Show.1080p.HDTV"))
        self.assertFalse(pattern.search("Show.HDR.x265.WEB-DL"))
        self.assertEqual(len(display), 3)

    def test_bad_regex_reports_error(self):
        regex, _, errors = rules.build_exclude_regex("re:[(")
        self.assertEqual(regex, "")
        self.assertTrue(errors)
        regex, errors = rules.build_include_regex([], "", "re:[(")
        self.assertEqual(regex, "")
        self.assertTrue(errors)

    def test_advanced_regex_overrides_tags(self):
        regex, _ = rules.build_include_regex(["HDR"], "XXX", r"(?i)(?=.*WEB-DL)")
        self.assertNotIn("HDR", regex)
        self.assertTrue(rules._compile(regex).search("Show.WEB-DL"))


class RuleTests(unittest.TestCase):
    def test_parse_line_rules(self):
        parsed, errors = rules.parse_line_rules(rules.EXAMPLE, SITES)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0]["site_id"], 1)
        self.assertTrue(parsed[0]["include_regex"].startswith("(?="))
        self.assertIn("x264", parsed[0]["exclude_regex"])

    def test_id_target_and_comments(self):
        text = "# 这是注释\n#12 => B站 => 2160p => 低码"
        parsed, errors = rules.parse_line_rules(text, SITES)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["target_kind"], "id")
        self.assertEqual(errors, [])

    def test_quick_rule_from_catalog(self):
        cfg = {"quick_sub": 11, "quick_site": 1, "quick_tags": ["HDR", "H.265"],
               "quick_include": "XX制作组", "quick_exclude": "x264;;CAM"}
        parsed, errors = rules.build_quick_rule(cfg, CATALOG)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["site_id"], 1)
        self.assertEqual(parsed[0]["sid"], 11)
        self.assertTrue(rules._compile(parsed[0]["include_regex"]))
        merged = rules.merge_rules([], parsed)
        self.assertEqual(len(merged), 1)

    def test_quick_rule_missing_subscription(self):
        parsed, errors = rules.build_quick_rule({"quick_sub": 999}, CATALOG)
        self.assertEqual(parsed, [])
        self.assertTrue(errors)

    def test_quick_rule_typed_name(self):
        cfg = {"quick_sub": "海贼王", "quick_site": 1, "quick_tags": ["HDR"]}
        parsed, errors = rules.build_quick_rule(cfg, CATALOG)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["target_kind"], "name")
        self.assertEqual(parsed[0]["target_name"], "海贼王")
        self.assertTrue(rules.rule_matches(parsed[0], SUBS[0]))
        self.assertFalse(rules.rule_matches(parsed[0], SUBS[1]))

    def test_quick_rule_typed_hash_id(self):
        cfg = {"quick_sub": "#11", "quick_site": 1}
        parsed, errors = rules.build_quick_rule(cfg, CATALOG)
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0]["sid"], 11)

    def test_quick_rule_typed_name_with_season(self):
        cfg = {"quick_sub": "海贼王#S2", "quick_site": 1}
        parsed, _ = rules.build_quick_rule(cfg, CATALOG)
        self.assertEqual(parsed[0]["season"], 2)
        self.assertFalse(rules.rule_matches(parsed[0], SUBS[0]))

    def test_quick_rule_typed_id_without_catalog(self):
        cfg = {"quick_sub": 11, "quick_site": 1}
        parsed, errors = rules.build_quick_rule(cfg, {"subscriptions": [], "sites": SITES})
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0]["sid"], 11)
        self.assertTrue(rules.rule_matches(parsed[0], SUBS[0]))

    def test_quick_rule_id_not_in_catalog_warns(self):
        parsed, errors = rules.build_quick_rule({"quick_sub": 999}, CATALOG)
        self.assertEqual(parsed, [])
        self.assertTrue(errors)

    def test_rule_matches_by_id_and_season(self):
        rule, _ = rules.build_quick_rule({"quick_sub": 11, "quick_site": 1}, CATALOG)
        self.assertTrue(rules.rule_matches(rule[0], SUBS[0]))
        self.assertFalse(rules.rule_matches(rule[0], SUBS[1]))
        season_rule, _ = rules.parse_line_rules("海贼王#S2 => A站 => HDR => ", SITES)
        self.assertFalse(rules.rule_matches(season_rule[0], SUBS[0]))

    def test_site_and_title_checks(self):
        parsed, _ = rules.parse_line_rules(rules.EXAMPLE, SITES)
        rule = parsed[0]
        self.assertTrue(rules.check_site(rule, "A站", SITES))
        self.assertFalse(rules.check_site(rule, "B站", SITES))
        self.assertEqual(rules.check_title(rule, "One.Piece.HDR.DDP5.1.XX制作组.1080p"), [])
        self.assertTrue(rules.check_title(rule, "One.Piece.HDR.DDP5.1.XX制作组.x264"))

    def test_template_skips_configured(self):
        parsed, _ = rules.parse_line_rules(rules.EXAMPLE, SITES)
        text = rules.template_for(SUBS + [{"id": 13, "name": "没配的剧", "season": 3}], parsed)
        self.assertNotIn("海贼王", text)
        self.assertIn("没配的剧#S3", text)


class StoreTests(unittest.TestCase):
    def test_page_rows_object(self):
        class Page:
            items = [{"id": 1}]
            total = 1

        items, total = store.page_rows(Page())
        self.assertEqual(items, [{"id": 1}])
        self.assertEqual(total, 1)

    def test_page_rows_dict(self):
        # 旧形态：直接返回 dict，不能走 .items 属性（那是绑定方法，会 TypeError）
        items, total = store.page_rows({"items": [{"id": 2}], "total": 2})
        self.assertEqual(items, [{"id": 2}])
        self.assertEqual(total, 2)

    def test_page_rows_garbage(self):
        for weird in (None, [], "text", 5, {"total": "x"}, object()):
            items, total = store.page_rows(weird)
            self.assertIsInstance(items, list)
            self.assertIsInstance(total, int)

    def test_page_rows_dict_items_view(self):
        holder = {"items": {"a": 1}.items(), "total": 1}
        items, _ = store.page_rows(holder)
        self.assertEqual(items, [("a", 1)])

    def test_rule_payload_only_three_fields(self):
        parsed, _ = rules.parse_line_rules(rules.EXAMPLE, SITES)
        payload = store.rule_payload(parsed[0])
        self.assertEqual(set(payload), {"sites", "include", "exclude"})
        self.assertEqual(payload["sites"], [1])

    def test_apply_and_restore(self):
        plugin = FakePlugin()
        parsed, _ = rules.parse_line_rules(rules.EXAMPLE, SITES)
        calls = []
        original = store.write_subscription
        store.write_subscription = lambda sid, payload: (calls.append((str(sid), dict(payload))) or (True, ""))
        try:
            done, applied, messages = store.apply_rules(plugin, parsed, SUBS)
            self.assertEqual(done, 2)
            self.assertEqual(set(applied), {"11", "12"})
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(set(payload) == {"sites", "include", "exclude"}
                                for _, payload in calls))
            self.assertEqual(set(store.original_settings(plugin)), {"11", "12"})
            restored, _ = store.restore_rules(plugin)
            self.assertEqual(restored, 2)
            self.assertEqual(store.original_settings(plugin), {})
        finally:
            store.write_subscription = original

    def test_inspect_reports_problems_and_written(self):
        parsed, _ = rules.parse_line_rules(rules.EXAMPLE, SITES)
        records = [
            {"title": "海贼王", "seasons": "1", "episodes": "1", "torrent_site": "A站",
             "download_hash": "aaa", "downloader": "qb",
             "torrent_name": "One.Piece.HDR.DDP5.1.XX制作组.1080p"},
            {"title": "海贼王", "seasons": "1", "episodes": "2", "torrent_site": "B站",
             "download_hash": "bbb", "downloader": "qb",
             "torrent_name": "One.Piece.HDR.DDP5.1.XX制作组.x264"},
        ]
        result = store.inspect(parsed, SUBS, records, SITES, ["11"])
        self.assertEqual(result["stats"]["written"], 1)
        self.assertEqual(len(result["issues"]), 1)
        self.assertIn("站点不符", " ".join(result["issues"][0]["reasons"]))
        restored = store._jsonable(result)
        self.assertEqual(restored["stats"], result["stats"])

    def test_clean_plan_two_steps(self):
        plugin = FakePlugin()
        plan = store.make_plan(plugin, [{"hash": "aaa", "name": "海贼王", "downloader": "qb"}])
        self.assertIsNone(store.take_plan(plugin, "wrong"))
        self.assertEqual(store.take_plan(plugin, plan["id"])["id"], plan["id"])
        store.clear_plan(plugin)
        self.assertIsNone(store.take_plan(plugin, plan["id"]))


class RenderTests(unittest.TestCase):
    def test_escape(self):
        html = renderer.render({
            "subscriptions": [{"name": "<script>alert(1)</script>", "sites": "A&B"}],
            "rules": [{"target": "<img src=x>", "include_regex": "<b>"}],
            "issues": [{"name": "<b>x</b>", "reasons": ["<i>y</i>"], "title": "z"}],
            "log": ["<u>log</u>"], "notice": "<script>notice</script>",
        })
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("A&amp;B", html)
        self.assertNotIn("<img src=x>", html)

    def test_weird_payload(self):
        for value in ({}, {"subscriptions": "oops", "stats": 5}, {"issues": [1, 2]}):
            self.assertIn("单源追剧", renderer.render(value))

    def test_shows_usage_steps(self):
        html = renderer.render({})
        self.assertIn("扫描 MP 订阅", html)
        self.assertIn("写进 MP 订阅", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
