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

    def test_save_auction_state_is_serializable(self):
        view = self._board()
        view["summary"] = "测试行情"
        self.plg._save_auction_state(view)
        self.assertIn("lots", self.plg._auction_state)
        # 状态卡渲染不联网、不抛异常
        page = self.plg.get_page()
        self.assertTrue(page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
