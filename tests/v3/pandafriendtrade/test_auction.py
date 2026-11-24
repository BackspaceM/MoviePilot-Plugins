#!/usr/bin/env python3
"""海报拍卖规则单测 + 真实看板数据回归。"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "plugins.v3"))
sys.path.insert(0, os.path.join(ROOT, "tests", "v3"))
import importlib  # noqa: E402
import check_api_contract as cc  # noqa: E402
cc.install_stubs()
auction_core = importlib.import_module("pandafriendtrade.core")


def load_live():
    path = "/private/tmp/pandapt_probe/auctionboard.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


class TestParse(unittest.TestCase):
    def test_parse_live_board(self):
        raw = load_live()
        if not raw or raw.get("ret") != 0:
            self.skipTest("live auction sample unavailable")
        board = auction_core.parse_auction_board(raw.get("data", {}))
        self.assertTrue(board["enabled"])
        self.assertGreater(board["balance"], 0)
        self.assertEqual(len(board["lots"]), 3)
        rules = board["rules"]
        self.assertEqual(rules.get("extension_seconds"), 180)
        self.assertEqual(rules.get("max_extension_seconds"), 1800)
        lot = board["lots"][0]
        self.assertEqual(lot.title, "将军号")
        self.assertEqual(lot.rarity, "normal")
        self.assertEqual(lot.current_price, 2000)
        self.assertEqual(lot.min_increment, 200)
        self.assertEqual(lot.increment, 200)
        self.assertGreater(lot.hard_ends_at, lot.ends_at)
        precious = [x for x in board["lots"] if x.rarity == "precious"][0]
        self.assertEqual(precious.leader_name, "EthanSong")
        self.assertEqual(precious.bid_fee_bps, 800)

    def test_empty(self):
        b = auction_core.parse_auction_board({})
        self.assertEqual(b["lots"], [])
        self.assertEqual(auction_core.auction_summary(b), "暂无进行中的拍卖")

    def test_config_auction_defaults(self):
        cfg = auction_core.parse_config({})
        self.assertFalse(cfg.do_auction)
        self.assertFalse(cfg.auction_auto_bid)
        self.assertEqual(cfg.auction_budget, 0)
        self.assertEqual(cfg.auction_snipe_seconds, 60)

    def test_config_auction_values(self):
        cfg = auction_core.parse_config({
            "do_auction": "1", "auction_auto_bid": "true", "auction_budget": 8000,
            "auction_total_budget": 20000, "auction_snipe_seconds": 30,
            "auction_rarities": "normal, rare, bogus", "auction_max_bids_per_lot": 5,
        })
        self.assertTrue(cfg.do_auction)
        self.assertTrue(cfg.auction_auto_bid)
        self.assertEqual(cfg.auction_budget, 8000)
        self.assertEqual(cfg.auction_total_budget, 20000)
        self.assertEqual(cfg.auction_snipe_seconds, 30)
        self.assertEqual(cfg.auction_rarities, ("normal", "rare"))
        self.assertEqual(cfg.auction_max_bids_per_lot, 5)

    def test_proxy_normalize(self):
        self.assertEqual(auction_core.normalize_proxy(""), "")
        self.assertEqual(auction_core.normalize_proxy("127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(auction_core.normalize_proxy("socks5://1.2.3.4:1080"), "socks5://1.2.3.4:1080")
        self.assertEqual(auction_core.normalize_proxy("ftp://x"), "")


class TestAuctionMath(unittest.TestCase):
    def test_fee_matches_site_rule(self):
        # bps=200：amount=2200 -> floor((2200*200+500)/1000)=440 -> 44.0
        self.assertEqual(auction_core.auction_fee(2200, 200), 44.0)
        # 已出价 2000 再出 2200，只补差额手续费
        self.assertEqual(auction_core.auction_fee(2200, 200, 2000), 4.0)
        self.assertEqual(auction_core.auction_fee(2200, 0), 0.0)

    def test_next_bid_from_live(self):
        raw = load_live()
        if not raw or raw.get("ret") != 0:
            self.skipTest("live sample unavailable")
        board = auction_core.parse_auction_board(raw.get("data", {}))
        lot = board["lots"][0]  # normal, current 2000, inc 200
        # 站点 minimum_bid 已含增量，默认直接用它，不重复加价
        amount = auction_core.next_bid_amount(lot, board["max_bid"])
        self.assertEqual(amount, lot.minimum_bid)  # 2000
        # 激进 1 档 = minimum_bid + increment
        self.assertEqual(auction_core.next_bid_amount(lot, board["max_bid"], 1), 2200)
        # precious: minimum_bid=298888（最高293888+增量5000），直接用
        p = [x for x in board["lots"] if x.rarity == "precious"][0]
        self.assertEqual(auction_core.next_bid_amount(p, board["max_bid"]), 298888)
        self.assertEqual(auction_core.next_bid_amount(p, board["max_bid"], 1), 303888)

    def test_debit(self):
        raw = load_live()
        if not raw or raw.get("ret") != 0:
            self.skipTest("live sample unavailable")
        board = auction_core.parse_auction_board(raw.get("data", {}))
        lot = board["lots"][0]
        # 首次出价 2200：本金 2200 + 手续费 44
        self.assertEqual(auction_core.auction_debit(lot, 2200), 2244.0)


class TestDecisions(unittest.TestCase):
    def _board(self):
        return {
            "enabled": True, "balance": 100000.0, "max_bid": 10 ** 12,
            "lots": [auction_core.AuctionLot(
                id=1, poster_id=10, title="测试海报", rarity="normal", status="open",
                can_bid=True, starting_price=2000, minimum_bid=2000, current_price=2000,
                min_increment=200, highest_bid=0, bidder_count=0,
                ends_at=1000, hard_ends_at=1600, rule_version="poster_auction_v3_normal",
                bid_fee_bps=200, leader_uid=0, leader_name="", my_bid_amount=0,
                my_bid_rank=0, my_fee_total=0.0, owned_quantity=0)],
        }

    def test_disabled_by_default(self):
        cfg = auction_core.parse_config({})
        self.assertEqual(auction_core.plan_auction(cfg, self._board(), now=990), [])

    def test_snipe_window(self):
        cfg = auction_core.parse_config({
            "do_auction": True, "auction_auto_bid": True, "auction_budget": 5000,
            "auction_snipe_seconds": 60,
        })
        # 还剩 200 秒 > 60，不抢
        self.assertEqual(auction_core.plan_auction(cfg, self._board(), now=800), [])
        # 还剩 20 秒，抢
        acts = auction_core.plan_auction(cfg, self._board(), now=980)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0].amount, 2000)

    def test_budget_and_rarity(self):
        cfg = auction_core.parse_config({
            "do_auction": True, "auction_auto_bid": True, "auction_budget": 2100,
            "auction_snipe_seconds": 0,
        })
        # minimum_bid=2000，预算 2100 够，能出价
        acts = auction_core.plan_auction(cfg, self._board(), now=900)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0].amount, 2000)
        # 预算 1999 则跳过
        cfg.auction_budget = 1999
        self.assertEqual(auction_core.plan_auction(cfg, self._board(), now=900), [])
        cfg2 = auction_core.parse_config({
            "do_auction": True, "auction_auto_bid": True, "auction_budget": 5000,
            "auction_snipe_seconds": 0, "auction_rarities": "rare",
        })
        # 稀有度不匹配，跳过
        self.assertEqual(auction_core.plan_auction(cfg2, self._board(), now=900), [])

    def test_skip_leading_and_limits(self):
        cfg = auction_core.parse_config({
            "do_auction": True, "auction_auto_bid": True, "auction_budget": 5000,
            "auction_snipe_seconds": 0, "auction_max_bids_per_lot": 2,
            "auction_min_gap": 60,
        })
        board = self._board()
        # 我领先时不加价
        board["lots"][0].my_bid_rank = 1
        self.assertEqual(auction_core.plan_auction(cfg, board, now=900), [])
        board["lots"][0].my_bid_rank = 2
        # 已加价 2 次达上限
        self.assertEqual(auction_core.plan_auction(cfg, board, now=900,
                                                   bid_counts={1: 2}), [])
        # 距上次出价不足 60 秒
        self.assertEqual(auction_core.plan_auction(cfg, board, now=900,
                                                   last_bid_at={1: 880}), [])
        # 超限时间后可再出价
        self.assertEqual(len(auction_core.plan_auction(cfg, board, now=995,
                                                       bid_counts={1: 1}, last_bid_at={1: 880})), 1)

    def test_total_budget_and_balance(self):
        cfg = auction_core.parse_config({
            "do_auction": True, "auction_auto_bid": True, "auction_budget": 5000,
            "auction_total_budget": 100, "auction_snipe_seconds": 0,
        })
        self.assertEqual(auction_core.plan_auction(cfg, self._board(), now=900), [])
        board = self._board()
        board["balance"] = 10.0
        self.assertEqual(auction_core.plan_auction(cfg, board, now=900), [])

    def test_require_no_leader(self):
        cfg = auction_core.parse_config({
            "do_auction": True, "auction_auto_bid": True, "auction_budget": 5000,
            "auction_snipe_seconds": 0, "auction_require_no_leader": True,
        })
        board = self._board()
        board["lots"][0].leader_uid = 999
        self.assertEqual(auction_core.plan_auction(cfg, board, now=900), [])


class TestPluginIntegration(unittest.TestCase):
    """主类拍卖端点/监控循环集成（假客户端，不联网）。"""
    def setUp(self):
        from unittest import mock
        import importlib
        self.MOD = importlib.import_module("pandafriendtrade")
        self.plg = self.MOD.PandaFriendTrade()
        self.plg.init_plugin({"enabled": True, "dry_run": True, "cookie": "fake",
                              "do_auction": True, "auction_auto_bid": True,
                              "auction_budget": 5000, "auction_snipe_seconds": 0})

    def _board(self):
        raw = load_live()
        if raw and raw.get("ret") == 0:
            return auction_core.parse_auction_board(raw["data"], now=1795503000)
        return {"enabled": True, "balance": 100000.0, "max_bid": 10 ** 12,
                "rules": {}, "lots": [], "recent": [], "summary": "测试"}

    def test_client_uses_proxy_config(self):
        self.plg.init_plugin({"enabled": True, "cookie": "f", "proxy_url": "http://1.2.3.4:8080"})
        c = self.plg._client()
        self.assertEqual(c.proxy_url, "http://1.2.3.4:8080")

    def test_auction_check_readonly(self):
        view = self._board()
        fake = mock.MagicMock()
        fake.auction_board.return_value = self.MOD.site.Result.success(view)
        with mock.patch.object(self.plg, "_client", return_value=fake):
            r = self.plg.api_auction_check()
        self.assertTrue(r.success)
        self.assertIn("余额", r.message)
        self.assertEqual(fake.auction_bid.call_count, 0)

    def test_auction_once_dry_run_no_bid(self):
        view = self._board()
        fake = mock.MagicMock()
        fake.auction_board.return_value = self.MOD.site.Result.success(view)
        with mock.patch.object(self.plg, "_client", return_value=fake):
            r = self.plg.api_auction_once()
        self.assertTrue(r.success)
        self.assertEqual(fake.auction_bid.call_count, 0)

    def test_monitor_start_stop(self):
        fake = mock.MagicMock()
        fake.auction_board.return_value = self.MOD.site.Result.fail("offline")
        with mock.patch.object(self.plg, "_client", return_value=fake):
            self.plg._start_auction_monitor()
            self.assertTrue(self.plg._auction_running)
            self.plg._stop_auction_monitor()
            self.assertFalse(self.plg._auction_running)

    def test_consignment_board_view_parses_items(self):
        raw = load_live_consignment = None
        import os
        path = "/private/tmp/pandapt_probe/consignment_board.json"
        if os.path.exists(path):
            raw = json.load(open(path, encoding="utf-8"))
        if not raw or raw.get("ret") != 0:
            self.skipTest("live consignment sample unavailable")
        fake = mock.MagicMock()
        fake.home.return_value = self.MOD.site.Result.success({"me": {"uid": 10061}})
        fake.consignment_board.return_value = self.MOD.site.Result.success(raw["data"])
        with mock.patch.object(self.plg, "_client", return_value=fake):
            result = self.plg._consignment_board_view()
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["lots"]), 5)
        # 确认带 unowned_only 与 viewer uid
        call = fake.consignment_board.call_args
        self.assertEqual(call[0][0], 10061)
        self.assertTrue(call[1].get("unowned_only"))

    def test_consignment_endpoints_never_raise(self):
        self.plg.init_plugin({"enabled": True, "dry_run": True, "cookie": "fake",
                              "do_consignment": True, "consignment_auto_bid": True,
                              "consignment_budget": 5000})
        r1 = self.plg.api_consignment_check()
        r2 = self.plg.api_consignment_grab()
        self.assertTrue(hasattr(r1, "success"))
        self.assertTrue(hasattr(r2, "success"))

    def test_consignment_grab_dry_run_no_buy(self):
        view = {"enabled": True, "balance": 900000.0, "max_bid": 10 ** 12,
                "summary": "在售1件", "checked_at": "now", "lots": [
                    auction_core.AuctionLot(
                        id=7, poster_id=205, title="七武士", rarity="precious", status="open",
                        can_bid=True, starting_price=250000, minimum_bid=310000,
                        current_price=250000, min_increment=5000, highest_bid=250000,
                        bidder_count=1, ends_at=99999, hard_ends_at=999999,
                        rule_version="poster_auction_v3_precious", bid_fee_bps=800,
                        leader_uid=1, leader_name="x", my_bid_amount=0, my_bid_rank=0,
                        my_fee_total=0.0, owned_quantity=0, mode="open")]}
        cfg = auction_core.parse_config({"cookie": "fake", "dry_run": True,
                                          "do_consignment": True, "consignment_auto_bid": True,
                                          "consignment_budget": 400000})
        self.plg._cfg = cfg
        # 直接 patch 看板视图，专注验证 api 的试运行分流
        with mock.patch.object(self.plg, "_consignment_board_view",
                               return_value=self.MOD.site.Result.success(view)):
            fake = mock.MagicMock()
            with mock.patch.object(self.plg, "_client", return_value=fake):
                r = self.plg.api_consignment_grab()
        self.assertTrue(r.success)
        self.assertIn("试运行", r.message)
        self.assertEqual(fake.auction_bid.call_count, 0)  # 试运行不出价

    def test_save_auction_state_is_serializable(self):
        view = self._board()
        view["summary"] = "测试行情"
        self.plg._save_auction_state(view)
        self.assertIn("lots", self.plg._auction_state)
        # 状态卡渲染不联网、不抛异常
        page = self.plg.get_page()
        self.assertTrue(page)


class TestConsignment(unittest.TestCase):
    """海报寄售场解析与抢购决策。"""
    def _live(self):
        import os
        path = "/private/tmp/pandapt_probe/consignment_board.json"
        if not os.path.exists(path):
            return None
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return None

    def test_parse_live_consignment(self):
        raw = self._live()
        if not raw or raw.get("ret") != 0:
            self.skipTest("live consignment sample unavailable")
        view = auction_core.parse_consignment_board(raw["data"])
        self.assertTrue(view["enabled"])
        self.assertEqual(len(view["lots"]), 5)
        # 全是 open 模式、未拥有
        self.assertTrue(all(x.mode == "open" for x in view["lots"]))
        self.assertTrue(all(x.owned_quantity == 0 for x in view["lots"]))
        self.assertIn("在售", view["summary"] if "summary" in view else auction_core.consignment_summary(view))

    def test_plan_disabled_by_default(self):
        cfg = auction_core.parse_config({})
        view = {"enabled": True, "balance": 100000.0, "max_bid": 10 ** 12, "lots": []}
        self.assertEqual(auction_core.plan_consignment(cfg, view), [])

    def test_plan_unowned_and_budget(self):
        cfg = auction_core.parse_config({
            "do_consignment": True, "consignment_auto_bid": True,
            "consignment_budget": 5000, "consignment_unowned_only": True,
        })
        def lot(lid, pid, owned, price, minbid, mode="open", rarity="normal"):
            return auction_core.AuctionLot(
                id=lid, poster_id=pid, title=f"海报{pid}", rarity=rarity, status="open",
                can_bid=True, starting_price=price, minimum_bid=minbid,
                current_price=price, min_increment=200, highest_bid=price,
                bidder_count=0, ends_at=9999, hard_ends_at=99999,
                rule_version="poster_auction_v3_normal", bid_fee_bps=200,
                leader_uid=0, leader_name="", my_bid_amount=0, my_bid_rank=0,
                my_fee_total=0.0, owned_quantity=owned, mode=mode)
        view = {"enabled": True, "balance": 100000.0, "max_bid": 10 ** 12, "lots": [
            lot(1, 101, 0, 3500, 4000),       # 未拥有，预算内 -> 抢
            lot(2, 102, 1, 3500, 4000),       # 已拥有 -> 跳过
            lot(3, 103, 0, 8000, 8500),       # 超预算 -> 跳过
            lot(4, 104, 0, 3000, 3000, "dutch"),  # 荷兰降价 -> dutch_buy
        ]}
        acts = auction_core.plan_consignment(cfg, view)
        kinds = {d.lot_id: d.action_kind for d in acts}
        self.assertIn(1, kinds)
        self.assertEqual(kinds[1], "bid")
        self.assertNotIn(2, kinds)  # 已有
        self.assertNotIn(3, kinds)  # 超预算
        self.assertEqual(kinds[4], "dutch_buy")

    def test_plan_wanted_ids_and_rarity(self):
        cfg = auction_core.parse_config({
            "do_consignment": True, "consignment_auto_bid": True,
            "consignment_budget": 10000,
            "consignment_wanted_poster_ids": "205",
        })
        def lot(lid, pid, rarity):
            return auction_core.AuctionLot(
                id=lid, poster_id=pid, title="x", rarity=rarity, status="open", can_bid=True,
                starting_price=1000, minimum_bid=1000, current_price=1000, min_increment=200,
                highest_bid=1000, bidder_count=0, ends_at=9999, hard_ends_at=99999,
                rule_version="v3", bid_fee_bps=0, leader_uid=0, leader_name="",
                my_bid_amount=0, my_bid_rank=0, my_fee_total=0.0, owned_quantity=0)
        view = {"enabled": True, "balance": 100000.0, "max_bid": 10 ** 12,
                "lots": [lot(1, 146, "excellent"), lot(2, 205, "precious")]}
        acts = auction_core.plan_consignment(cfg, view)
        self.assertEqual([d.lot_id for d in acts], [2])  # 只要 205
        # 改成按档位：只要 precious
        cfg2 = auction_core.parse_config({
            "do_consignment": True, "consignment_auto_bid": True,
            "consignment_budget": 10000, "consignment_rarities": "precious",
        })
        acts2 = auction_core.plan_consignment(cfg2, view)
        self.assertEqual([d.lot_id for d in acts2], [2])


class TestUnifiedSnipe(unittest.TestCase):
    """v1.3.0：拍卖行+寄售场合并为一个低价抢购项目。"""

    def _lot(self, lid, title, rarity, price, mode="open", owned=0, is_mine=False,
             leading=False, min_bid=None):
        return auction_core.AuctionLot(
            id=lid, poster_id=1000 + lid, title=title, rarity=rarity, status="open",
            can_bid=True, starting_price=price,
            minimum_bid=min_bid if min_bid is not None else price,
            current_price=price, min_increment=200, highest_bid=price, bidder_count=0,
            ends_at=99999, hard_ends_at=999999, rule_version="v3", bid_fee_bps=0,
            leader_uid=9 if leading else 0, leader_name="other" if leading else "",
            my_bid_amount=0, my_bid_rank=1 if leading else 0, my_fee_total=0.0,
            owned_quantity=owned, mode=mode, is_mine=is_mine, can_buy=True)

    def test_task_options_from_page(self):
        board = {"tasks": [
            {"key": "normal_collection_once", "title": "普通海报集合", "group": "once", "status": "claimable"},
            {"key": "animation_night_repeat", "title": "动画电影之夜", "group": "theme_repeat", "status": "claimable"},
            {"key": "growth_journey_repeat", "title": "心灵成长之旅", "group": "theme_repeat", "status": "in_progress"},
        ]}
        items = auction_core.parse_task_options(board)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0], {"title": "【一次性典藏】普通海报集合", "value": "normal_collection_once"})
        self.assertEqual(items[1], {"title": "【主题片单交付】动画电影之夜", "value": "animation_night_repeat"})

    def test_task_options_fallback(self):
        items = auction_core.parse_task_options(None)
        titles = [x["title"] for x in items]
        self.assertIn("【一次性典藏】完全图鉴", titles)
        self.assertIn("【主题片单交付】心灵成长之旅", titles)

    def test_claim_by_task_keys(self):
        board = {"tasks": [
            {"key": "normal_collection_once", "title": "普通海报集合", "status": "claimable"},
            {"key": "animation_night_repeat", "title": "动画电影之夜", "status": "claimable"},
            {"key": "growth_journey_repeat", "title": "心灵成长之旅", "status": "claimable"},
        ]}
        self.assertEqual(auction_core.claimable_posters_by_tasks(board, ("animation_night_repeat",)),
                         ["animation_night_repeat"])
        self.assertEqual(len(auction_core.claimable_posters_by_tasks(board, ())), 3)

    def test_snipe_low_price_first_across_markets(self):
        cfg = auction_core.parse_config({
            "do_poster_snipe": True, "poster_snipe_max_price": 9000,
            "poster_snipe_unowned_only": True,
        })
        a_view = {"enabled": True, "balance": 100000, "lots": [
            self._lot(1, "贵拍卖", "rare", 8000, min_bid=8100),
            self._lot(2, "便宜拍卖", "normal", 2000, min_bid=2100),
        ]}
        c_view = {"enabled": True, "balance": 100000, "lots": [
            self._lot(3, "便宜寄售", "normal", 1000, mode="dutch"),
        ]}
        decisions = auction_core.plan_snipe(cfg, a_view, c_view, now=100)
        self.assertEqual([d.lot_id for d in decisions], [3, 2, 1])  # 价格 1000/2100/8100 升序
        self.assertEqual(decisions[0].action_kind, "dutch_buy")
        self.assertEqual(decisions[0].source, "寄售场")
        self.assertEqual(decisions[1].action_kind, "bid")

    def test_snipe_respects_rarity_owner_and_budget(self):
        cfg = auction_core.parse_config({
            "do_poster_snipe": True, "poster_snipe_max_price": 5000,
            "poster_snipe_rarities": "normal,rare",
        })
        a_view = {"enabled": True, "balance": 100000, "lots": [
            self._lot(1, "传说超预算", "legendary", 100000, mode="dutch"),
            self._lot(2, "普通", "normal", 2000, mode="dutch"),
            self._lot(3, "已有", "normal", 3000, mode="dutch", owned=1),
            self._lot(4, "我的", "normal", 1000, mode="dutch", is_mine=True),
            self._lot(5, "领先中", "rare", 4000, min_bid=4500, leading=True),
        ]}
        decisions = auction_core.plan_snipe(cfg, a_view, None, now=100)
        self.assertEqual([d.lot_id for d in decisions], [2])

    def test_snipe_disabled_by_default(self):
        cfg = auction_core.parse_config({})
        self.assertFalse(cfg.do_poster_snipe)
        self.assertEqual(auction_core.plan_snipe(cfg, {}, {}), [])

    def test_new_config_backward_compat(self):
        cfg = auction_core.parse_config({"do_auction": True, "auction_budget": 7000,
                                         "auction_rarities": "rare"})
        self.assertTrue(cfg.do_poster_snipe)
        self.assertEqual(cfg.poster_snipe_max_price, 7000)
        self.assertEqual(cfg.poster_snipe_rarities, ("rare",))

    def test_consignment_page_size_is_site_valid(self):
        import importlib
        site_mod = importlib.import_module("pandafriendtrade.site")
        import inspect
        sig = inspect.signature(site_mod.PandaClient.consignment_board)
        self.assertEqual(sig.parameters["page_size"].default, 9)

    def test_snipe_summary(self):
        a = {"lots": [self._lot(1, "甲", "normal", 5000)]}
        c = {"lots": [self._lot(2, "乙", "rare", 9000)]}
        text = auction_core.snipe_summary(a, c)
        self.assertIn("最低5000", text)
        self.assertIn("最低9000", text)


class TestSnipeRoundsAndRanges(unittest.TestCase):
    """v1.4.0：抢购轮次 + 每稀有度价格区间 + 已抢去重（手续费不退）。"""

    def _lot(self, lid, rarity, price, mode="dutch", source_price=None):
        return auction_core.AuctionLot(
            id=lid, poster_id=2000 + lid, title=f"海报{lid}", rarity=rarity, status="open",
            can_bid=True, starting_price=price, minimum_bid=price, current_price=price,
            min_increment=200, highest_bid=price, bidder_count=0, ends_at=99999,
            hard_ends_at=999999, rule_version="v3", bid_fee_bps=0,
            leader_uid=0, leader_name="", my_bid_amount=0, my_bid_rank=0,
            my_fee_total=0.0, owned_quantity=0, mode=mode, is_mine=False, can_buy=True)

    def test_parse_ranges(self):
        cfg = auction_core.parse_config({
            "snipe_range_excellent": "5000-7000",
            "snipe_range_rare": "15000~20000",
            "snipe_range_precious": "30000",
            "snipe_range_normal": "",
            "snipe_range_legendary": "颠倒 90000-10000",
        })
        self.assertEqual(cfg.poster_snipe_ranges["excellent"], (5000, 7000))
        self.assertEqual(cfg.poster_snipe_ranges["rare"], (15000, 20000))
        self.assertEqual(cfg.poster_snipe_ranges["precious"], (0, 30000))
        self.assertEqual(cfg.poster_snipe_ranges["legendary"], (10000, 90000))  # 写反自动纠正
        self.assertNotIn("normal", cfg.poster_snipe_ranges)

    def test_rarity_price_range_filter(self):
        cfg = auction_core.parse_config({
            "do_poster_snipe": True,
            "snipe_range_excellent": "5000-7000",
            "snipe_range_rare": "15000-20000",
            "poster_snipe_rounds": 10,
        })
        lots = [
            self._lot(1, "excellent", 4000),   # 低于优秀下限，跳过
            self._lot(2, "excellent", 6000),   # 命中
            self._lot(3, "excellent", 8000),   # 高于优秀上限，跳过
            self._lot(4, "rare", 18000),       # 命中
            self._lot(5, "rare", 25000),       # 高于稀有上限，跳过
            self._lot(6, "normal", 100),       # 未配区间且全局上限=0，跳过
        ]
        view = {"enabled": True, "balance": 1000000, "lots": lots}
        got = [d.lot_id for d in auction_core.plan_snipe(cfg, view, None, now=100)]
        self.assertEqual(got, [2, 4])  # 仍按价格从低到高

    def test_global_cap_is_gate_when_ranges_set(self):
        cfg = auction_core.parse_config({
            "do_poster_snipe": True, "poster_snipe_max_price": 6500,
            "snipe_range_excellent": "5000-7000", "poster_snipe_rounds": 10,
        })
        view = {"enabled": True, "balance": 1000000, "lots": [
            self._lot(1, "excellent", 6000), self._lot(2, "excellent", 6900)]}
        self.assertEqual([d.lot_id for d in auction_core.plan_snipe(cfg, view, None, now=100)], [1])

    def test_rounds_limit(self):
        cfg = auction_core.parse_config({
            "do_poster_snipe": True, "poster_snipe_max_price": 99999,
            "poster_snipe_rounds": 2,
        })
        view = {"enabled": True, "balance": 1000000, "lots": [
            self._lot(i, "normal", 1000 * i) for i in (5, 1, 3, 2, 4)]}
        got = [d.amount for d in auction_core.plan_snipe(cfg, view, None, now=100)]
        self.assertEqual(got, [1000, 2000])  # 只抢最便宜的 2 笔

    def test_rounds_default_and_clamp(self):
        self.assertEqual(auction_core.parse_config({}).poster_snipe_rounds, 3)
        self.assertEqual(auction_core.parse_config({"poster_snipe_rounds": 999}).poster_snipe_rounds, 50)
        self.assertEqual(auction_core.parse_config({"poster_snipe_rounds": 0}).poster_snipe_rounds, 1)

    def test_attempted_skipped(self):
        cfg = auction_core.parse_config({
            "do_poster_snipe": True, "poster_snipe_max_price": 99999,
            "poster_snipe_rounds": 10,
        })
        view = {"enabled": True, "balance": 1000000, "lots": [
            self._lot(1, "normal", 1000), self._lot(2, "normal", 2000)]}
        tried = {auction_core.snipe_dedup_key("拍卖行", 1)}
        got = [d.lot_id for d in auction_core.plan_snipe(cfg, view, None, now=100,
                                                          attempted=tried)]
        self.assertEqual(got, [2])  # 已出价过的不再抢，避免重复扣手续费

    def test_decision_has_dedup_key_and_fee_hint(self):
        cfg = auction_core.parse_config({
            "do_poster_snipe": True, "poster_snipe_max_price": 99999,
        })
        view = {"enabled": True, "balance": 1000000, "lots": [self._lot(7, "normal", 3000)]}
        d = auction_core.plan_snipe(cfg, view, None, now=100)[0]
        self.assertEqual(d.dedup_key, "拍卖行:7")
        self.assertIn("手续费", d.reason)
        self.assertIn("未中标不退", d.reason)
        self.assertEqual(d.as_dict()["dedup_key"], "拍卖行:7")

    def test_no_price_config_no_bid(self):
        cfg = auction_core.parse_config({"do_poster_snipe": True})
        view = {"enabled": True, "balance": 1000000, "lots": [self._lot(1, "normal", 100)]}
        self.assertEqual(auction_core.plan_snipe(cfg, view, None, now=100), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
