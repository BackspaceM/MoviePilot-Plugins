#!/usr/bin/env python3
"""绿联 NAS 守护的离线桩测试，不连 MoviePilot 宿主。"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "plugins.v3" / "ugreenguard"
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
NAS = MODULE.nas
RENDERER = MODULE.renderer


def full_raw(**overrides):
    raw = {
        "ts": "2026-11-12 10:00:00",
        "volumes": [
            {"name": "volume1", "total": 2000, "used": 1000, "percent": 50.0},
            {"name": "volume2", "total": 1000, "used": 300, "percent": 30.0},
        ],
        "disks": [{"name": "disk1", "temp": 40, "health": "正常", "smart_alert": False}],
        "containers": [
            {"name": "moviepilot", "status": "running", "restarts": 0},
            {"name": "qbittorrent", "status": "running", "restarts": 2},
        ],
        "ups": {"on_battery": False, "battery": 100},
        "system": {"cpu": 20.0, "mem": 45.0},
    }
    raw.update(overrides)
    return raw


def bridge_collected(raw):
    return {"status": NAS.parse_status(raw), "source": "状态桥", "services": None}


class TestContract(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()

    def test_plugin_id_and_metadata(self):
        self.assertEqual(PLUGIN_ID, "UGreenGuard")
        self.assertEqual(PLUGIN_CLS.plugin_name, "绿联 NAS 守护")
        self.assertEqual(PLUGIN_CLS.plugin_version, "1.0.0")
        self.assertEqual(PLUGIN_CLS.plugin_author, "BackspaceM")
        self.assertEqual(PLUGIN_CLS.plugin_config_prefix, "ugreenguard_")
        self.assertEqual(PLUGIN_CLS.plugin_color, "#6366F1")
        self.assertEqual(PLUGIN_CLS.system_version, ">=3.0.0")
        self.assertEqual(PLUGIN_CLS.plugin_order, 96)
        self.assertTrue(PLUGIN_CLS.plugin_icon.endswith("icons/ugreenguard.png"))

    def test_response_is_pydantic_model(self):
        from pydantic import BaseModel

        self.assertTrue(issubclass(MODULE.Response, BaseModel))
        for field in ("success", "message", "data"):
            self.assertIn(field, MODULE.Response.model_fields)

    def test_all_api_routes_can_register_in_fastapi(self):
        from fastapi import FastAPI
        from fastapi.routing import APIRoute

        app = FastAPI()
        for api in self.plugin.get_api():
            self.assertEqual(api.get("auth"), "bear")
            self.assertTrue(str(api["path"]).startswith("/"))
            route = dict(api)
            route.pop("auth", None)
            route.pop("allow_anonymous", None)
            route["path"] = f"/api/v1/plugin/{PLUGIN_ID}{route['path']}"
            route["dependencies"] = list(route.get("dependencies") or ())
            route.setdefault("route_class_override", APIRoute)
            app.router.add_api_route(**route, tags=["plugin"])
        self.assertEqual(len(app.routes), len(self.plugin.get_api()) + 4)

    def test_every_page_button_matches_declared_api(self):
        declared = {
            str(a["path"]).lstrip("/"): {str(m).upper() for m in (a.get("methods") or ["GET"])}
            for a in self.plugin.get_api()
        }
        prefix = f"plugin/{PLUGIN_ID}/"
        clicks = cc.collect_click_events(self.plugin.get_page())
        self.assertEqual(len(clicks), 5)
        for click in list(clicks) + cc.collect_page_clicks(PLUGIN_CLS()):
            self.assertTrue(click["api"].startswith(prefix), click["api"])
            action = click["api"][len(prefix):].split("?", 1)[0].split("/", 1)[0]
            self.assertIn(action, declared, click["api"])
            self.assertIn(click["method"].upper(), declared[action], click["api"])

    def test_api_returns_three_part_envelope(self):
        for name in ("api_snapshot", "api_check", "api_refresh", "api_resume", "api_clear"):
            result = getattr(self.plugin, name)()
            self.assertIsInstance(result.success, bool)
            self.assertIsInstance(result.message, str)
        self.assertFalse(self.plugin.api_clear().success, "没确认时不该清空")
        self.assertTrue(self.plugin.api_clear(confirm="yes").success)
        snap = self.plugin.api_snapshot()
        for key in ("status", "hits", "log", "source", "mode"):
            self.assertIn(key, snap.data)


class TestPageAndForm(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.page = self.plugin.get_page()

    def test_page_has_buttons_and_one_html_node(self):
        self.assertIsInstance(self.page, list)
        components = [item.get("component") for item in self.page]
        self.assertEqual(components.count("VBtn"), 5)
        html_nodes = [item for item in self.page if item.get("html")]
        self.assertEqual(len(html_nodes), 1)
        self.assertIn("绿联 NAS 守护", html_nodes[0]["html"])
        self.assertIn("ugn-grid", html_nodes[0]["html"])

    def test_button_text_is_toplevel_not_in_props(self):
        for item in self.page:
            if item.get("component") == "VBtn":
                self.assertIsInstance(item.get("text"), str)
                self.assertNotIn("text", item.get("props", {}))

    def test_html_has_no_inline_script(self):
        html = RENDERER.render(self.plugin.api_snapshot().data)
        for marker in ("<script", "onclick=", "onchange=", "onload=", "javascript:"):
            self.assertNotIn(marker, html, marker)

    def test_form_has_exactly_six_controls(self):
        form, defaults = PLUGIN_CLS().get_form()
        controls = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("component") in ("VSelect", "VSwitch", "VTextField", "VTextarea", "VFileInput"):
                    controls.append(node["component"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(form)
        self.assertEqual(len(controls), 6, f"设置项应精简，实际 {controls}")
        self.assertEqual(controls.count("VSwitch"), 1)
        self.assertEqual(controls.count("VTextField"), 1)
        self.assertEqual(controls.count("VSelect"), 4)
        self.assertEqual(set(defaults), {
            "enabled", "bridge_url", "water_threshold", "temp_threshold", "mode", "interval_minutes",
        })

    def test_get_service_registers_cron(self):
        services = PLUGIN_CLS().get_service()
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["id"], "UGreenGuard")
        self.assertIsNotNone(services[0]["trigger"])
        self.assertTrue(callable(services[0]["func"]))
        disabled = PLUGIN_CLS()
        disabled.init_plugin({"enabled": False})
        self.assertFalse(disabled.get_state())
        self.assertEqual(disabled.get_service(), [])


class TestCollectAndInspect(unittest.TestCase):
    def test_local_fallback_is_used_without_bridge(self):
        plugin = PLUGIN_CLS()
        result = NAS.collect(plugin._cfg())
        self.assertEqual(result["source"], "容器自检")
        self.assertTrue(result["status"]["volumes"])
        self.assertIsInstance(result["services"], dict)
        volume = result["status"]["volumes"][0]
        self.assertIsInstance(volume["percent"], (int, float))
        self.assertEqual(result["status"]["disks"], [])
        self.assertEqual(result["status"]["containers"], [])
        self.assertEqual(result["status"]["ups"]["on_battery"], NAS.MISSING)

    def test_bridge_takes_priority_over_local(self):
        raw = full_raw()
        with patch.object(NAS, "_read_bridge", return_value=raw):
            result = NAS.collect({"bridge_url": "http://127.0.0.1:9999/status", "status_file": ""})
        self.assertEqual(result["source"], "状态桥")
        self.assertEqual(len(result["status"]["volumes"]), 2)

    def test_status_file_is_second_choice(self):
        raw = full_raw()
        with patch.object(NAS, "_read_bridge", return_value=None), \
             patch.object(NAS, "_read_status_file", return_value=raw):
            result = NAS.collect({"bridge_url": "http://bad", "status_file": "/tmp/ugn.json"})
        self.assertEqual(result["source"], "状态文件")

    def test_full_check_records_action_and_state(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"mode": "试运行", "cooldown": 0})
        raw = full_raw()
        raw["volumes"][0]["percent"] = 95.0
        with patch.object(NAS, "collect", return_value=bridge_collected(raw)):
            result = plugin.api_check()
        self.assertTrue(result.success)
        self.assertEqual(result.data["source"], "状态桥")
        self.assertIn("html", result.data)
        self.assertIn("volume_high", {h["rule"] for h in result.data["hits"]})
        self.assertTrue(result.data["log"])
        self.assertTrue(any("暂停" in entry["message"] for entry in result.data["log"]))
        self.assertTrue(plugin.get_data("ugn_state")["paused"])

    def test_debounce_and_cooldown(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"mode": "试运行", "cooldown": 99999})
        high = full_raw()
        high["volumes"][0]["percent"] = 95.0
        normal = full_raw()

        with patch.object(NAS, "collect", return_value=bridge_collected(high)):
            plugin.run_check("第一次告警")
        self.assertEqual(len(plugin.get_data("ugn_log")), 1)
        self.assertIn("volume_high", plugin.get_data("ugn_state")["last_alert"])

        # 状态没变，不该再记一笔
        with patch.object(NAS, "collect", return_value=bridge_collected(high)):
            plugin.run_check("状态没变")
        self.assertEqual(len(plugin.get_data("ugn_log")), 1)

        # 恢复正常记一笔
        with patch.object(NAS, "collect", return_value=bridge_collected(normal)):
            plugin.run_check("恢复正常")
        self.assertEqual(len(plugin.get_data("ugn_log")), 2)
        self.assertFalse(plugin.get_data("ugn_state")["paused"])

        # 冷却期内又告警，不该再执行
        with patch.object(NAS, "collect", return_value=bridge_collected(high)):
            plugin.run_check("冷却期内复发")
        self.assertEqual(len(plugin.get_data("ugn_log")), 2)

    def test_no_data_never_resumes(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"mode": "自动处置", "cooldown": 0})
        with patch.object(NAS, "collect", return_value=bridge_collected({})):
            view = plugin.run_check("桥返回空")
        self.assertEqual(view["hits"][0]["action"], "none")
        self.assertFalse(plugin.get_data("ugn_state")["paused"])

    def test_dynamic_text_is_escaped(self):
        dirty = "<img src=x onerror=alert(1)>"
        raw = full_raw()
        raw["volumes"][0]["name"] = dirty
        raw["volumes"][0]["percent"] = 95.0
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"mode": "试运行", "cooldown": 0})
        with patch.object(NAS, "collect", return_value=bridge_collected(raw)):
            view = plugin.run_check("转义")
        html = RENDERER.render(view)
        self.assertNotIn(dirty, html)
        self.assertIn("&lt;img", html)

    def test_no_ai_words_or_emoji_in_page(self):
        html = RENDERER.render(PLUGIN_CLS().api_snapshot().data)
        for word in ("AI", "智能赋能", "一键搞定", "全方位", "本插件旨在"):
            self.assertNotIn(word, html, word)

    def test_schema_shape(self):
        status = NAS.parse_status({})
        self.assertEqual(set(status), {"ts", "volumes", "disks", "containers", "ups", "system"})
        self.assertEqual(status["ts"], NAS.MISSING)
        self.assertEqual(status["ups"], {"on_battery": NAS.MISSING, "battery": NAS.MISSING})
        self.assertEqual(status["system"], {"cpu": NAS.MISSING, "mem": NAS.MISSING})


def main():
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n合计 {total} 个用例，通过 {total - failed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
