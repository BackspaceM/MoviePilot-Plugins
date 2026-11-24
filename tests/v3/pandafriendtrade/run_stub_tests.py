#!/usr/bin/env python3
"""熊猫好友买卖插件离线回归测试（复用全仓契约桩）。"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "plugins.v3" / "pandafriendtrade"
sys.path.insert(0, str(ROOT / "tests" / "v3"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)

CORE = MODULE.core


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({})

    def test_metadata_and_lifecycle(self):
        self.assertEqual(PLUGIN_ID, "PandaFriendTrade")
        self.assertEqual(PLUGIN_CLS.plugin_version, "1.2.0")
        self.assertTrue(PLUGIN_CLS.plugin_icon.endswith("/icons/pandafriendtrade.png"))
        for name in ("init_plugin", "get_state", "get_api", "get_form", "get_page", "stop_service"):
            self.assertTrue(callable(getattr(PLUGIN_CLS, name, None)), name)

    def test_routes_register_in_fastapi(self):
        from fastapi import FastAPI
        app = FastAPI()
        for route in self.plugin.get_api():
            self.assertEqual(route.get("auth"), "bear")
            app.add_api_route(f"/{route['path'].lstrip('/')}", route["endpoint"], methods=route["methods"])
        paths = {route.path for route in app.routes}
        for suffix in ("/run", "/dryrun", "/probe", "/preview", "/status",
                       "/auction_check", "/auction_once", "/auction_monitor", "/auction_status",
                       "/consignment_check", "/consignment_grab"):
            self.assertIn(suffix, paths)

    def test_form_contains_all_rule_switches(self):
        form, defaults = self.plugin.get_form()
        self.assertIsInstance(form, list)
        self.assertFalse(defaults.get("enabled"))
        self.assertTrue(defaults.get("dry_run"), "默认必须试运行")
        self.assertFalse(defaults.get("do_buy"))
        self.assertFalse(defaults.get("do_release"))
        models = {node.get("props", {}).get("model") for node in _walk(form)
                  if isinstance(node.get("props"), dict)}
        for key in ("cookie", "do_income", "do_work", "do_interact", "do_tasks",
                    "do_commission", "do_settle", "do_posters", "do_protect", "do_buy",
                    "do_release", "interact_mode", "interact_budget", "buy_max_price",
                    "reserved_uids", "poster_gift_recipients",
                    "proxy_url", "proxy_bypass", "do_auction", "auction_auto_bid",
                    "auction_budget", "auction_total_budget", "auction_snipe_seconds",
                    "auction_rarities", "auction_poll_seconds", "auction_aggressive_steps",
                    "do_consignment", "consignment_auto_bid", "consignment_budget",
                    "consignment_wanted_poster_ids", "consignment_rarities",
                    "consignment_unowned_only", "poster_claim_rarities"):
            self.assertIn(key, models)
        self.assertEqual(defaults.get("do_auction"), False, "拍卖默认关")
        self.assertEqual(defaults.get("auction_auto_bid"), False, "自动出价默认关")

    def test_page_buttons_all_clickable(self):
        page = self.plugin.get_page()
        buttons = [node for node in _walk(page) if node.get("component") == "VBtn"]
        self.assertEqual(len(buttons), 11)
        declared = {route["path"].lstrip("/") for route in self.plugin.get_api()}
        for button in buttons:
            self.assertTrue(str(button.get("text") or "").strip(), "按钮不能有空文字")
            click = button.get("events", {}).get("click", {})
            self.assertTrue(click.get("api", "").split(f"plugin/{PLUGIN_ID}/")[-1] in declared)

    def test_page_escapes_html(self):
        self.plugin._last = {"finished_at": "<script>", "total": 3, "done": 2,
                             "failed": 1, "summary": {"total": 3, "safe": 3}, "labels": ["<img>"]}
        page = self.plugin.get_page()
        html_text = "".join(str(node.get("html") or "") for node in _walk(page))
        self.assertIn("熊猫好友买卖", html_text)
        self.assertNotIn("<script>", html_text)
        self.assertNotIn("<img>", html_text)

    def test_endpoints_never_raise(self):
        # 未配置 cookie 时所有端点都返回 Response，不抛异常
        for name in ("api_run", "api_dryrun", "api_probe", "api_preview", "api_status",
                     "api_auction_check", "api_auction_once", "api_auction_monitor", "api_auction_status",
                     "api_consignment_check", "api_consignment_grab"):
            result = getattr(self.plugin, name)()
            self.assertIsNotNone(result)
            self.assertTrue(hasattr(result, "success"), name)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"enabled": True, "dry_run": True, "cookie": "fake"})

    def test_dry_run_never_calls_write_actions(self):
        home = {"claimable_income": "100", "task_status": [{"task_key": "t1", "status": "claimable"}],
                "my_assets": [], "my_owner": {}}
        fake_client = mock.MagicMock()
        fake_client.home.return_value = MODULE.site.Result.success(home)
        fake_client.commission_board.return_value = MODULE.site.Result.success({})
        fake_client.poster_task_board.return_value = MODULE.site.Result.success({})
        with mock.patch.object(self.plugin, "_client", return_value=fake_client):
            self.plugin._execute(dry=True)
        self.assertEqual(fake_client.claim_income.call_count, 0)
        self.assertEqual(fake_client.claim_task.call_count, 0)
        self.assertEqual(fake_client.work.call_count, 0)
        self.assertTrue(self.plugin._last.get("dry_run"))
        self.assertGreaterEqual(self.plugin._last.get("total", 0), 2)

    def test_real_run_executes_safe_actions(self):
        home = {"claimable_income": "100", "task_status": [{"task_key": "t1", "status": "claimable"}],
                "my_assets": [], "my_owner": {}}
        fake_client = mock.MagicMock()
        fake_client.home.return_value = MODULE.site.Result.success(home)
        fake_client.commission_board.return_value = MODULE.site.Result.success({})
        fake_client.poster_task_board.return_value = MODULE.site.Result.success({})
        fake_client.claim_income.return_value = MODULE.site.Result.success({}, "ok")
        fake_client.claim_task.return_value = MODULE.site.Result.success({}, "ok")
        with mock.patch.object(self.plugin, "_client", return_value=fake_client):
            self.plugin._execute(dry=False)
        self.assertEqual(fake_client.claim_income.call_count, 1)
        fake_client.claim_task.assert_called_once_with("t1")
        self.assertEqual(self.plugin._last.get("done"), 2)
        self.assertEqual(self.plugin._last.get("failed"), 0)

    def test_failed_write_counts_as_failure(self):
        home = {"claimable_income": "100", "task_status": [], "my_assets": [], "my_owner": {}}
        fake_client = mock.MagicMock()
        fake_client.home.return_value = MODULE.site.Result.success(home)
        fake_client.commission_board.return_value = MODULE.site.Result.success({})
        fake_client.poster_task_board.return_value = MODULE.site.Result.success({})
        fake_client.claim_income.return_value = MODULE.site.Result.fail("登录失效")
        with mock.patch.object(self.plugin, "_client", return_value=fake_client):
            self.plugin._execute(dry=False)
        self.assertEqual(self.plugin._last.get("failed"), 1)

    def test_home_failure_short_circuits(self):
        fake_client = mock.MagicMock()
        fake_client.home.return_value = MODULE.site.Result.fail("HTTP 403")
        with mock.patch.object(self.plugin, "_client", return_value=fake_client):
            self.plugin._execute(dry=False)
        self.assertEqual(fake_client.commission_board.call_count, 0)
        self.assertIn("失败", self.plugin._last.get("message", ""))

    def test_native_auto_dispatch_prevents_duplicate(self):
        cfg = CORE.parse_config({"do_commission": True, "do_settle": True})
        board = {
            "parallel_limit": 2,
            "offers": [{"id": 9, "offer_snapshot_text": {"name": "对账", "recommended_team_size": 1}}],
            "eligible_members": [{"relationship_id": 31, "slave_uid": 1001,
                                  "trait_summary": {"attributes": {"wisdom": 90}}}],
            "running": [],
            "auto_dispatch": {"enabled": True},
        }
        acts = CORE.plan_commission(cfg, board, 0)
        self.assertNotIn("friendTradeCommissionStart", [a.action for a in acts])

    def test_buy_requires_detail(self):
        cfg = CORE.parse_config({"do_buy": True, "buy_max_price": 1000})
        market = {"items": [{"uid": 5}]}
        self.assertEqual(CORE.merchant_candidates(market, cfg, {}), [])
        details = {5: {"trait_summary": {"attributes": {"wisdom": 80}},
                       "trade_context": {"can_buy": True, "action": "buy", "price": "500"}}}
        acts = CORE.merchant_candidates(market, cfg, details)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0].action, "friendTradeBuy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
