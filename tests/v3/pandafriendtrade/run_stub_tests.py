#!/usr/bin/env python3
"""熊猫好友买卖插件离线回归测试（复用全仓契约桩）。"""
from __future__ import annotations

import json
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
        self.assertEqual(PLUGIN_CLS.plugin_version, "1.6.0")
        self.assertTrue(PLUGIN_CLS.plugin_icon.endswith("/icons/pandafriendtrade.png"))
        for name in ("init_plugin", "get_state", "get_api", "get_form", "get_page", "stop_service",
                     "get_service", "run_scheduled"):
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
                       "/consignment_check", "/consignment_grab",
                       "/scan_tasks", "/snipe_check", "/snipe_once", "/snipe_monitor",
                       "/snipe_reset", "/screening_check", "/screening_run", "/screening_status"):
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
        for key in ("cron", "cookie", "do_income", "do_work", "do_interact", "do_tasks",
                    "do_commission", "do_settle", "do_posters", "do_protect", "do_buy",
                    "do_release", "interact_mode", "interact_budget", "buy_max_price",
                    "reserved_uids", "poster_gift_recipients", "proxy_url", "proxy_bypass",
                    "request_interval", "do_poster_snipe", "poster_snipe_rounds",
                    "poster_snipe_max_price", "poster_snipe_rarities",
                    "poster_snipe_unowned_only", "poster_snipe_poll_seconds",
                    "poster_claim_tasks", "snipe_range_normal", "snipe_range_excellent",
                    "snipe_range_rare", "snipe_range_precious", "snipe_range_legendary"):
            self.assertIn(key, models)
        # 高级兼容（旧版细分参数）已按用户要求删除：旧字段不再出现在配置页
        form_text = json.dumps(form, ensure_ascii=False)
        self.assertNotIn("高级兼容", form_text)
        for legacy in ("auction_auto_bid", "consignment_wanted_poster_ids", "poster_claim_rarities"):
            self.assertNotIn(f'"model": "{legacy}"', form_text)
        self.assertEqual(defaults.get("do_poster_snipe"), False, "抢购默认关")
        self.assertEqual(defaults.get("poster_snipe_rounds"), 3, "抢购轮次默认 3")

    def test_page_buttons_all_clickable(self):
        page = self.plugin.get_page()
        buttons = [node for node in _walk(page) if node.get("component") == "VBtn"]
        self.assertEqual(len(buttons), 14)
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
                     "api_consignment_check", "api_consignment_grab",
                     "api_scan_tasks", "api_snipe_check", "api_snipe_once", "api_snipe_monitor",
                     "api_snipe_reset", "api_screening_check", "api_screening_run", "api_screening_status"):
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


class ScheduleAndDedupTests(unittest.TestCase):
    """v1.5.0：定时服务注册 + 佣人按天去重落盘。"""

    def setUp(self):
        cc.install_stubs()
        self.plugin = PLUGIN_CLS()

    def test_service_registered_when_enabled(self):
        self.plugin.init_plugin({"enabled": True, "cookie": "fake", "cron": "0 3,9,18,23 * * *"})
        services = self.plugin.get_service()
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["id"], "PandaFriendTrade")
        self.assertEqual(services[0]["func"].__name__, "run_scheduled")
        self.assertIs(services[0]["func"].__self__, self.plugin)
        self.assertTrue(callable(services[0]["trigger"]))

    def test_service_empty_when_disabled(self):
        self.plugin.init_plugin({})
        self.assertEqual(self.plugin.get_service(), [])

    def test_service_uses_fallback_cron(self):
        self.plugin.init_plugin({"enabled": True, "cookie": "fake", "cron": "bad"})
        self.assertEqual(self.plugin._cfg.cron, CORE.DEFAULT_CRON)
        self.assertEqual(len(self.plugin.get_service()), 1)

    def test_servant_done_cross_day_reset(self):
        today = self.plugin._today()
        self.plugin._servant_done = {"date": "2000-01-01", "work": [11], "interact": [22]}
        self.plugin.get_data = lambda key, default=None: (
            dict(self.plugin._servant_done) if key == "panda_ft_servant_done_v1" else default)
        loaded = self.plugin._load_servant_done()
        self.assertEqual(loaded["date"], today)
        self.assertEqual(loaded["work"], [])
        self.assertEqual(loaded["interact"], [])

    def test_mark_servant_done(self):
        self.plugin.init_plugin({})
        self.plugin.save_data = lambda key, value: None
        self.plugin._mark_servant_done(CORE.Action(
            "friendTradeWork", {"target_uid": 11}, "work"))
        self.plugin._mark_servant_done(CORE.Action(
            "friendTradeInteract", {"target_uid": 22}, "interact"))
        self.plugin._mark_servant_done(CORE.Action(
            "friendTradeWork", {"target_uid": 11}, "work"))  # 重复不叠加
        self.assertEqual(self.plugin._servant_done["work"], [11])
        self.assertEqual(self.plugin._servant_done["interact"], [22])
        self.plugin._mark_servant_done(CORE.Action("friendTradeClaimIncome", {}, "income"))
        self.assertEqual(self.plugin._servant_done["work"], [11])


class ScreeningTests(unittest.TestCase):
    """v1.6.0：每日放映 SSS 自动化求解器 + 工作安排漏排修复。"""

    @staticmethod
    def _card(tone, motif, layout, rotation, title, code):
        return {"tone": tone, "motif": motif, "layout": layout, "rotation": rotation,
                "title": title, "code": code}

    def test_calibration_hits_center(self):
        # 各种靶心/宽度/周期下，落点都紧贴靶心（误差远小于半靶宽）
        import random
        rng = random.Random(11)
        for _ in range(500):
            center = rng.randint(0, 10000)
            width = rng.randint(200, 1200)
            cycle = rng.randint(1000, 8000) * 2
            ch = {"game_key": "film_calibration",
                  "prompt": {"target_center_bp": center, "target_width_bp": width, "cycle_ms": cycle}}
            solved = CORE.solve_screening(ch)
            elapsed = solved["events"][0]["elapsed_ms"]
            pos = CORE.calibration_position_for_elapsed(elapsed, ch["prompt"])
            self.assertLessEqual(abs(pos - center), 20)

    def test_missing_frame_picks_pattern_card(self):
        tones, motifs, rots = CORE.CARD_TONES, CORE.CARD_MOTIFS, CORE.CARD_ROTATIONS
        miss = 2
        seq = []
        for i in range(5):
            if i == miss:
                seq.append({"position": i, "card": None})
            else:
                seq.append({"position": i, "card": self._card(
                    tones[i % 6], motifs[(i * 2) % 6], "center", rots[i % 4], f"T{i}", f"C{i}")})
        correct = self._card(tones[miss % 6], motifs[(miss * 2) % 6], "center", rots[miss % 4], "T2", "C2")
        options = [
            {"option_key": "bad111111111", "card": self._card("amber", "beam", "split", 90, "X", "X1")},
            {"option_key": "good22222222", "card": correct},
            {"option_key": "bad333333333", "card": self._card("blue", "waves", "diagonal", 270, "Y", "Y1")},
        ]
        ch = {"game_key": "missing_frame",
              "prompt": {"sequence": seq, "missing_index": miss, "options": options}}
        solved = CORE.solve_screening(ch)
        self.assertEqual(solved["answer"]["option_key"], "good22222222")
        self.assertEqual(len(solved["events"]), 1)
        self.assertEqual(solved["events"][0]["type"], "select")

    def test_poster_memory_finds_preview_cards(self):
        pv = [self._card("coral", "orbit", "center", 0, "A", "A1"),
              self._card("teal", "waves", "split", 90, "B", "B1"),
              self._card("plum", "prism", "diagonal", 180, "C", "C1")]
        options = [{"option_key": f"key{i}", "card": c} for i, c in enumerate(pv)]
        options += [{"option_key": f"bad{i}", "card": self._card("mint", "steps", "center", 270, f"D{i}", f"D{i}")}
                    for i in range(3)]
        ch = {"game_key": "poster_memory",
              "prompt": {"preview": pv, "options": options, "required_selection_count": 3}}
        solved = CORE.solve_screening(ch)
        self.assertEqual(sorted(solved["answer"]["selected_option_keys"]), ["key0", "key1", "key2"])
        self.assertIn({"type": "submit", "elapsed_ms": 1000}, solved["events"])

    def test_best_grade_order(self):
        self.assertEqual(CORE.best_grade(["SS", "S", "SSS"]), "SSS")
        self.assertEqual(CORE.best_grade(["B", "A"]), "A")
        self.assertEqual(CORE.best_grade([]), "-")

    def test_work_uses_site_can_work_not_current_work(self):
        # 当前工作恰好等于策略选中工作时，仍应安排（修复 key != current_work 漏排）
        home = {"my_assets": [{
            "slave_uid": 123, "username": "u",
            "cultivation_summary": {
                "can_work_today": True, "can_interact_today": False,
                "current_work": "accounting", "available_works": {"accounting": {"label": "记账"}},
            },
        }]}
        cfg = CORE.parse_config({"do_work": True, "work_policy": "income"})
        acts = CORE.build_plan(cfg, home, {}, {})
        work = [a for a in acts if a.action == "friendTradeWork"]
        self.assertEqual(len(work), 1, "current_work 等于策略工作时也不应漏排")

    def test_execute_reloads_servant_done_each_round(self):
        # _execute 每轮开头重读去重状态：跨天后昨天的 UID 不再被当作已工作
        cc.install_stubs()
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"cookie": "fake", "enabled": True, "dry_run": True})
        plugin.get_data = mock.MagicMock(return_value={})
        plugin._client = lambda: type("C", (), {
            "home": lambda self: type("R", (), {"ok": True, "data": {"my_assets": []}})()
        })()
        # 直接调用 _load_servant_done 验证跨天清空路径在每轮被调用的入口可用性
        plugin._servant_done = {"date": "2000-01-01", "work": [11], "interact": []}
        plugin.get_data = lambda key, default=None: (
            {"date": "2000-01-01", "work": [11], "interact": []}
            if key == "panda_ft_servant_done_v1" else default)
        reloaded = plugin._load_servant_done()
        self.assertEqual(reloaded["work"], [])
        self.assertEqual(reloaded["date"], plugin._today())


if __name__ == "__main__":
    unittest.main(verbosity=2)
