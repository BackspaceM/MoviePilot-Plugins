#!/usr/bin/env python3
"""HTTP 客户端单测：注入假 opener，不发真实请求。"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from urllib.error import HTTPError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "plugins.v3", "pandafriendtrade"))

import importlib  # noqa: E402
# site.py 与标准库同名，用包路径导入；先装宿主桩以便包 __init__ 能加载
sys.path.insert(0, os.path.join(ROOT, "tests", "v3"))
sys.path.insert(0, os.path.join(ROOT, "plugins.v3"))
import check_api_contract as cc  # noqa: E402
cc.install_stubs()
site = importlib.import_module("pandafriendtrade.site")


class FakeResponse:
    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, ret=0, data=None, msg="OK", raw=None):
        self.ret, self.data, self.msg, self.raw = ret, data, msg, raw
        self.last_req = None

    def open(self, req, timeout=None):
        self.last_req = req
        if self.raw is not None:
            return FakeResponse(self.raw.encode())
        return FakeResponse(json.dumps({"ret": self.ret, "msg": self.msg, "data": self.data}).encode())


class RaisingOpener:
    def __init__(self, exc):
        self.exc = exc

    def open(self, req, timeout=None):
        raise self.exc


class TestClient(unittest.TestCase):
    def test_post_encodes_actions_and_params(self):
        op = FakeOpener(data={"ok": 1})
        c = site.PandaClient("c_secure_uid=x; c_secure_pass=y", interval=0, opener=op)
        r = c.post("friendTradeWork", {"target_uid": 123, "work_key": "clean"})
        self.assertTrue(r.ok)
        body = op.last_req.data.decode()
        self.assertIn("action=friendTradeWork", body)
        self.assertIn("params%5Btarget_uid%5D=123", body)
        self.assertIn("params%5Bwork_key%5D=clean", body)
        hdr = {k.lower(): v for k, v in op.last_req.headers.items()}
        self.assertEqual(hdr["cookie"], "c_secure_uid=x; c_secure_pass=y")
        self.assertEqual(hdr["x-requested-with"], "XMLHttpRequest")
        self.assertIn("ajax.php", op.last_req.full_url)

    def test_ret_nonzero_is_failure(self):
        c = site.PandaClient("cookie", interval=0, opener=FakeOpener(ret=1, msg="登录失效"))
        r = c.home()
        self.assertFalse(r.ok)
        self.assertIn("登录失效", r.message)

    def test_invalid_json(self):
        c = site.PandaClient("cookie", interval=0, opener=FakeOpener(raw="<html>login</html>"))
        r = c.home()
        self.assertFalse(r.ok)
        self.assertIn("不是 JSON", r.message)

    def test_http_error(self):
        err = HTTPError("https://pandapt.net/ajax.php", 500, "boom", {}, None)
        c = site.PandaClient("cookie", interval=0, opener=RaisingOpener(err))
        r = c.home()
        self.assertFalse(r.ok)
        self.assertIn("HTTP 500", r.message)

    def test_network_error(self):
        c = site.PandaClient("cookie", interval=0, opener=RaisingOpener(OSError("timed out")))
        r = c.home()
        self.assertFalse(r.ok)
        self.assertIn("OSError", r.message)

    def test_no_cookie_short_circuits(self):
        c = site.PandaClient("", interval=0, opener=FakeOpener())
        self.assertFalse(c.home().ok)

    def test_writes_use_correct_actions(self):
        cases = [
            (lambda c: c.work(1, "clean"), "friendTradeWork"),
            (lambda c: c.interact(1, "praise"), "friendTradeInteract"),
            (lambda c: c.initiative("nuzzle"), "friendTradeSlaveInitiative"),
            (lambda c: c.claim_income(), "friendTradeClaimIncome"),
            (lambda c: c.claim_task("t1"), "friendTradeClaimTaskReward"),
            (lambda c: c.protect(1, 24), "friendTradeProtect"),
            (lambda c: c.release(1), "friendTradeRelease"),
            (lambda c: c.commission_settle(7), "friendTradeCommissionSettle"),
            (lambda c: c.album_redeem(), "friendTradeAlbumRedeem"),
            (lambda c: c.trade("friendTradeBuy", 1, "500"), "friendTradeBuy"),
        ]
        for call, expected in cases:
            op = FakeOpener(data={})
            r = call(site.PandaClient("cookie", interval=0, opener=op))
            self.assertTrue(r.ok, expected)
            self.assertIn(f"action={expected}", op.last_req.data.decode())

    def test_commission_start_serializes_team(self):
        op = FakeOpener(data={})
        site.PandaClient("cookie", interval=0, opener=op).commission_start(9, [11, 12])
        body = op.last_req.data.decode()
        self.assertIn("friendTradeCommissionStart", body)
        self.assertIn("%5B11%2C12%5D", body)  # JSON [11,12]

    def test_uuid_actions(self):
        for call, expected in [
            (lambda c: c.poster_task_claim("x"), "friendTradePosterTaskClaim"),
            (lambda c: c.poster_gift(2, 33), "friendTradePosterGift"),
        ]:
            op = FakeOpener(data={})
            site.PandaClient("cookie", interval=0, opener=op)
            r = call(site.PandaClient("cookie", interval=0, opener=op))
            self.assertTrue(r.ok, expected)
            self.assertIn(f"action={expected}", op.last_req.data.decode())

    def test_proxy_config_http(self):
        c = site.PandaClient("cookie", interval=0, proxy_url="http://127.0.0.1:7890")
        self.assertTrue(c.using_proxy)
        self.assertEqual(c.proxy_url, "http://127.0.0.1:7890")

    def test_proxy_config_socks_without_pysocks(self):
        c = site.PandaClient("cookie", interval=0, proxy_url="socks5://1.2.3.4:1080")
        # 没有 PySocks 时记录提示且不冒充在用代理
        if c.proxy_error:
            self.assertFalse(c.using_proxy)
            self.assertIn("PySocks", c.proxy_error)

    def test_no_proxy_by_default(self):
        c = site.PandaClient("cookie", interval=0)
        self.assertFalse(c.using_proxy)

    def test_auction_and_consignment_actions(self):
        cases = [
            (lambda c: c.auction_bid(1, 2000, "poster_auction_v3_normal", 10061),
             "friendTradePosterAuctionBid"),
            (lambda c: c.dutch_buy(2, 5000, "poster_auction_v3_normal", 10061),
             "friendTradePosterDutchBuy"),
            (lambda c: c.auction_board(), "friendTradePosterAuctionBoard"),
            (lambda c: c.auction_bids(3), "friendTradePosterAuctionBids"),
            (lambda c: c.consignment_board(10061), "friendTradePosterConsignmentBoard"),
            (lambda c: c.consignment_cancel(10061, 4), "friendTradePosterConsignmentCancel"),
        ]
        for call, expected in cases:
            op = FakeOpener(data={})
            r = call(site.PandaClient("cookie", interval=0, opener=op))
            self.assertTrue(r.ok, expected)
            self.assertIn(f"action={expected}", op.last_req.data.decode())

    def test_auction_bid_payload_fields(self):
        op = FakeOpener(data={})
        site.PandaClient("cookie", interval=0, opener=op).auction_bid(7, 8888, "v3", 10061)
        body = op.last_req.data.decode()
        for key in ("auction_id", "amount", "request_uuid", "expected_rule_version", "expected_uid"):
            self.assertIn(f"params%5B{key}%5D", body)

    def test_trade_rejects_unknown_action(self):
        c = site.PandaClient("cookie", interval=0, opener=FakeOpener())
        self.assertFalse(c.trade("friendTradeHack", 1, "1").ok)

    def test_uuid_is_unique(self):
        self.assertNotEqual(site.PandaClient.new_uuid(), site.PandaClient.new_uuid())


if __name__ == "__main__":
    unittest.main(verbosity=2)
