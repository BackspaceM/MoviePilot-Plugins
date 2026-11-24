#!/usr/bin/env python3
"""熊猫好友买卖核心规则单测：不依赖宿主、不发网络。"""
from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "plugins.v3", "pandafriendtrade"))

import core  # noqa: E402

SAMPLE_DIR = "/private/tmp/pandapt_probe"


def load(name):
    path = os.path.join(SAMPLE_DIR, name)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


class TestParsing(unittest.TestCase):
    def test_parse_config_defaults(self):
        cfg = core.parse_config({})
        self.assertEqual(cfg.interact_mode, "free")
        self.assertEqual(cfg.work_policy, "income")
        self.assertTrue(cfg.dry_run)
        self.assertTrue(cfg.do_income)
        self.assertFalse(cfg.do_buy)
        self.assertFalse(cfg.do_release)
        self.assertEqual(cfg.interact_cost_limit, 0)

    def test_config_budget_modes(self):
        self.assertEqual(core.parse_config({"interact_mode": "limit", "interact_budget": 300}).interact_cost_limit, 300)
        self.assertGreater(core.parse_config({"interact_mode": "unlimited"}).interact_cost_limit, 100000)

    def test_parse_int_list(self):
        self.assertEqual(core.parse_int_list("123, 456;789\n12"), (123, 456, 789, 12))
        self.assertEqual(core.parse_int_list("abc,-1,0"), ())

    def test_parse_bool(self):
        for v in ("1", "true", "YES", "on", "是", True):
            self.assertTrue(core.parse_bool(v))
        for v in ("0", "false", "no", "", None, False):
            self.assertFalse(core.parse_bool(v))

    def test_site_normalize(self):
        self.assertEqual(core.normalize_site(""), core.DEFAULT_SITE)
        self.assertEqual(core.normalize_site("https://pandapt.net/"), "https://pandapt.net")
        self.assertEqual(core.normalize_site("javascript:foo"), core.DEFAULT_SITE)


class TestViews(unittest.TestCase):
    def test_servants_from_live_data(self):
        home = load("home.json")
        if not home:
            self.skipTest("live sample not available")
        data = home.get("data", {})
        servants = core.parse_servants(data)
        self.assertEqual(len(servants), 8)
        s = servants[0]
        self.assertEqual(s.uid, 12349)
        self.assertEqual(s.name, "hoher")
        self.assertIn("diligence", s.attributes)
        self.assertIn("clean", s.work_options)
        self.assertFalse(s.can_work)

    def test_servants_empty(self):
        self.assertEqual(core.parse_servants({}), [])
        self.assertEqual(core.parse_servants({"my_assets": None}), [])

    def test_claimable_tasks(self):
        home = load("home.json")
        if home:
            keys = core.claimable_tasks(home.get("data", {}))
            self.assertIsInstance(keys, list)
        self.assertEqual(core.claimable_tasks({"task_status": [
            {"task_key": "a", "status": "claimed"},
            {"task_key": "b", "status": "claimable"},
            {"task_key": "c", "progress": 3, "target": 3},
        ]}), ["b", "c"])

    def test_claimable_posters(self):
        board = {"tasks": [
            {"key": "x", "status": "claimable"},
            {"key": "y", "status": "in_progress"},
        ]}
        self.assertEqual(core.claimable_posters(board), ["x"])


class TestDecisions(unittest.TestCase):
    def test_choose_work_income(self):
        s = core.Servant(uid=1, name="t", overall=60, attributes={"diligence": 90}, talent="",
                         rarity="", can_work=True, can_interact=True, current_work="", mood=80,
                         work_options={"clean": {}, "accounting": {}, "rest": {}})
        self.assertEqual(core.choose_work(s, "income"), "accounting")
        self.assertEqual(core.choose_work(s, "rest"), "rest")

    def test_choose_work_attribute(self):
        s = core.Servant(uid=1, name="t", overall=60, attributes={"diligence": 90, "wisdom": 20}, talent="",
                         rarity="", can_work=True, can_interact=True, current_work="", mood=80,
                         work_options={"clean": {}, "errand": {}})
        self.assertEqual(core.choose_work(s, "attribute"), "clean")

    def test_choose_work_empty(self):
        s = core.Servant(uid=1, name="t", overall=0, attributes={}, talent="", rarity="",
                         can_work=False, can_interact=False, current_work="", mood=0, work_options={})
        self.assertEqual(core.choose_work(s, "income"), "")

    def test_choose_interaction_modes(self):
        free = core.parse_config({"interact_mode": "free"})
        self.assertEqual(core.choose_interaction(free), "whisper")
        off = core.parse_config({"interact_mode": "off"})
        self.assertEqual(core.choose_interaction(off), "")
        limited = core.parse_config({"interact_mode": "limit", "interact_budget": 150})
        self.assertEqual(core.choose_interaction(limited), "feed")
        rich = core.parse_config({"interact_mode": "limit", "interact_budget": 500})
        self.assertEqual(core.choose_interaction(rich), "reward")

    def test_choose_initiative(self):
        home = {"my_owner": {"initiative_summary": {"can_initiate_today": True, "action_key": "nuzzle"},
                             "initiative_options": [{"key": "nuzzle"}, {"key": "pillow_talk"}]}}
        self.assertEqual(core.choose_initiative(home), "nuzzle")
        home2 = {"my_owner": {"initiative_summary": {"can_initiate_today": False},
                              "initiative_options": [{"key": "pillow_talk"}, {"key": "nuzzle"}]}}
        self.assertEqual(core.choose_initiative(home2), "pillow_talk")

    def test_commission_team_and_settle(self):
        board = load("office.json")
        if board:
            data = board.get("data", {})
            offers, members, settle, auto = core.parse_commission(data)
            self.assertGreater(len(offers), 0)
            self.assertGreater(len(members), 0)
            team = core.pick_commission_team(offers[0], members, ())
            self.assertGreater(len(team), 0)
        board2 = {
            "parallel_limit": 2,
            "offers": [{"id": 9, "offer_snapshot_text": {"name": "夜间对账", "recommended_team_size": 1}}],
            "eligible_members": [{"relationship_id": 31, "slave_uid": 1001, "trait_summary": {"attributes": {"wisdom": 90}}}],
            "running": [{"id": 77, "status": "running"}, {"id": 78, "status": "ready", "offer": {"name": "跑腿"}}],
            "auto_dispatch": {"enabled": False},
        }
        cfg = core.parse_config({"do_commission": True, "do_settle": True})
        actions = core.plan_commission(cfg, board2, 1)
        acts = [a.action for a in actions]
        self.assertIn("friendTradeCommissionSettle", acts)
        self.assertIn("friendTradeCommissionStart", acts)
        team_action = [a for a in actions if a.action == "friendTradeCommissionStart"][0]
        self.assertEqual(json.loads(team_action.params["relationship_ids"]), [31])
        # 站点原生自动派遣开着时不重复派出
        board2["auto_dispatch"]["enabled"] = True
        acts2 = [a.action for a in core.plan_commission(cfg, board2, 1)]
        self.assertNotIn("friendTradeCommissionStart", acts2)

    def test_build_plan_safe_default(self):
        home = {"claimable_income": "123.4", "task_status": [
            {"task_key": "t1", "status": "claimable"}], "my_assets": [], "my_owner": {}}
        cfg = core.parse_config({})
        actions = core.build_plan(cfg, home, {}, {})
        acts = [a.action for a in actions]
        self.assertIn("friendTradeClaimIncome", acts)
        self.assertIn("friendTradeClaimTaskReward", acts)
        self.assertTrue(all(a.risk == "safe" for a in actions))

    def test_build_plan_risk_flags(self):
        home = {"claimable_income": "50", "task_status": [], "my_assets": [
            {"slave_uid": 11, "username": "u", "is_temporary": True,
             "protection_summary": {"can_buy_protection": True, "remaining_minutes": 10, "next_protection_price": "500"},
             "cultivation_summary": {"can_work_today": True, "can_interact_today": True, "work_options": {"clean": {}}, "available_works": {"clean": {}}}},
        ], "my_owner": {}}
        cfg = core.parse_config({"do_work": True, "do_protect": True, "do_release": True,
                                 "release_temporary": True, "protect_budget": 1000})
        actions = core.build_plan(cfg, home, {}, {})
        risks = {a.action: a.risk for a in actions}
        self.assertEqual(risks.get("friendTradeProtect"), "cost")
        self.assertEqual(risks.get("friendTradeRelease"), "irreversible")

    def test_merchant_candidates(self):
        cfg = core.parse_config({"do_buy": True, "buy_max_price": 1000, "buy_min_overall": 50})
        market = {"items": [{"uid": 5}, {"uid": 6}]}
        details = {
            5: {"username": "cheap", "trait_summary": {"attributes": {"wisdom": 80, "virtue": 70}},
                "trade_context": {"can_buy": True, "action": "buy", "price": "500"}},
            6: {"username": "expensive", "trait_summary": {"attributes": {"wisdom": 80, "virtue": 70}},
                "trade_context": {"can_buy": True, "action": "buy", "price": "5000"}},
        }
        actions = core.merchant_candidates(market, cfg, details)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].params["target_uid"], 5)
        self.assertEqual(actions[0].action, "friendTradeBuy")

    def test_summarize(self):
        actions = [core.Action("a", risk="safe"), core.Action("b", risk="cost"),
                   core.Action("c", risk="irreversible"), core.Action("d", risk="weird")]
        self.assertEqual(core.summarize(actions), {"total": 4, "safe": 2, "cost": 1, "irreversible": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
