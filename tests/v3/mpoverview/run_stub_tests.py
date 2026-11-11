#!/usr/bin/env python3
"""MP 总览插件的离线回归测试（无需 MoviePilot 宿主）。

复用全仓契约校验器的宿主 stub，覆盖：
- 按钮 404 的根因（Response 必须是 Pydantic 模型、接口可注册、路径/方法匹配）；
- 三个入口（详情页 / 首页仪表盘 / 只读 API）的结构合同；
- 「设置不繁琐」与「零配置」约束；
- 离线采集与渲染的健壮性（缺宿主模块时不能崩）。

用法：python3 tests/v3/mpoverview/run_stub_tests.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]  # tests/v3/mpoverview -> 仓库根
PLUGIN_DIR = ROOT / "plugins.v3" / "mpoverview"
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
COLLECTOR = MODULE.collector
RENDERER = MODULE.renderer


class TestContract(unittest.TestCase):
    """防止再次出现「按钮点击 Not Found」。"""

    def setUp(self) -> None:
        self.plugin = PLUGIN_CLS()

    def test_plugin_id_matches_class_name(self) -> None:
        self.assertEqual(PLUGIN_ID, "MPOverview")
        self.assertEqual(PLUGIN_CLS.plugin_name, "MPOverview")

    def test_response_is_pydantic_model(self) -> None:
        from pydantic import BaseModel

        self.assertTrue(issubclass(MODULE.Response, BaseModel),
                        "普通 Python 类会让 FastAPI 路由注册失败并被宿主静默跳过")
        for field in ("success", "message", "data"):
            self.assertIn(field, MODULE.Response.model_fields)

    def test_all_api_routes_can_register_in_fastapi(self) -> None:
        from fastapi import FastAPI
        from fastapi.routing import APIRoute

        app = FastAPI()
        for api in self.plugin.get_api():
            route = dict(api)
            route.pop("auth", None)
            route.pop("allow_anonymous", None)
            route["path"] = f"/api/v1/plugin/{PLUGIN_ID}{route['path']}"
            route["dependencies"] = list(route.get("dependencies") or ())
            route.setdefault("route_class_override", APIRoute)
            app.router.add_api_route(**route, tags=["plugin"])
        self.assertEqual(len(app.routes), len(self.plugin.get_api()) + 4)

    def test_every_page_button_matches_declared_api(self) -> None:
        declared = {
            str(a["path"]).lstrip("/"): {str(m).upper() for m in (a.get("methods") or ["GET"])}
            for a in self.plugin.get_api()
        }
        clicks = cc.collect_click_events(self.plugin.get_page())
        self.assertTrue(clicks, "详情页至少应有一个可点击按钮")
        prefix = f"plugin/{PLUGIN_ID}/"
        for click in clicks:
            self.assertTrue(click["api"].startswith(prefix), click["api"])
            action = click["api"][len(prefix):].split("?", 1)[0].split("/", 1)[0]
            self.assertIn(action, declared, click["api"])
            self.assertIn(click["method"].upper(), declared[action], click["api"])

    def test_api_returns_three_part_envelope(self) -> None:
        for name in ("api_snapshot", "api_refresh"):
            result = getattr(self.plugin, name)()
            self.assertTrue(result.success)
            self.assertIsInstance(result.message, str)
        snap = self.plugin.api_snapshot()
        self.assertIn("generated_at", snap.data)
        refresh = self.plugin.api_refresh()
        self.assertIn("html", refresh.data)
        self.assertIn("已重新采集", refresh.message)


class TestPageAndDashboard(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = PLUGIN_CLS()
        self.data = COLLECTOR.collect()

    def test_page_has_button_and_rendered_html(self) -> None:
        page = self.plugin.get_page()
        self.assertIsInstance(page, list)
        components = [item.get("component") for item in page]
        self.assertIn("VBtn", components)
        html_nodes = [item for item in page if item.get("html")]
        self.assertEqual(len(html_nodes), 1)
        html = html_nodes[0]["html"]
        self.assertIn("MP 总览", html)
        self.assertIn("mpov-grid", html)
        self.assertIn("mpov-bar", html)

    def test_button_text_is_toplevel_not_in_props(self) -> None:
        button = next(i for i in self.plugin.get_page() if i.get("component") == "VBtn")
        self.assertEqual(button.get("text"), "刷新数据")
        self.assertNotIn("text", button.get("props", {}),
                         "props.text 会被 Vuetify 当作 variant，导致空白按钮")

    def test_html_has_no_inline_script(self) -> None:
        """交互全部走宿主原生 @action / attrs.refresh，不塞自执行脚本。"""
        html = RENDERER.render(self.data)
        for marker in ("<script", "onclick=", "onchange=", "onload=", "javascript:"):
            self.assertNotIn(marker, html, marker)

    def test_dashboard_returns_valid_triplet(self) -> None:
        cols, attrs, elements = self.plugin.get_dashboard(key="overview", user_agent="test")
        self.assertIn("cols", cols)
        self.assertIn("md", cols)
        self.assertEqual(attrs.get("border"), False)
        self.assertIsInstance(attrs.get("refresh"), int)
        self.assertTrue(30 <= attrs["refresh"] <= 300)
        self.assertEqual(len(elements), 1)
        self.assertIn("html", elements[0])
        self.assertIn("近期入库", elements[0]["html"])

    def test_dashboard_meta_keys_are_served(self) -> None:
        meta = self.plugin.get_dashboard_meta()
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0]["key"], "overview")
        cols, attrs, elements = self.plugin.get_dashboard(key=meta[0]["key"])
        self.assertTrue(elements)

    def test_compact_render_hides_system_and_status(self) -> None:
        full = RENDERER.render(self.data)
        compact = RENDERER.render(self.data, compact=True)
        self.assertIn("系统状态", full)
        self.assertNotIn("系统状态", compact)
        self.assertIn("运行状态", full)
        self.assertNotIn("运行状态", compact)
        self.assertLess(len(compact), len(full))

    def test_offline_snapshot_is_explicit(self) -> None:
        self.assertIn("offline", self.data)
        if self.data["offline"]:
            self.assertIn("离线预览", RENDERER.render(self.data))
        for key in ("system", "storage", "services", "subscriptions",
                    "downloads", "imports", "plugins", "alerts", "sources"):
            self.assertIn(key, self.data)

    def test_dynamic_text_is_escaped(self) -> None:
        dirty = dict(self.data)
        dirty["imports"] = dict(self.data["imports"])
        dirty["imports"]["recent"] = [{
            "title": "<img src=x onerror=alert(1)>", "detail": "", "category": "媒体", "date": "",
        }]
        html = RENDERER.render(dirty)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)


class TestSimplicity(unittest.TestCase):
    """「设置最好别这么繁琐」的硬约束。"""

    def test_form_has_exactly_one_input(self) -> None:
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
        self.assertEqual(controls, ["VSelect"], f"设置项应保持极简，实际为 {controls}")
        self.assertEqual(set(defaults), {"refresh_seconds"})

    def test_refresh_seconds_are_clamped(self) -> None:
        plugin = PLUGIN_CLS()
        for raw, expected in ((15, 30), (60, 60), (120, 120), (99999, 300), ("bad", 60), (None, 60)):
            plugin.init_plugin({"refresh_seconds": raw})
            self.assertEqual(plugin.get_dashboard()[1]["refresh"], expected, raw)

    def test_enabled_by_default(self) -> None:
        plugin = PLUGIN_CLS()
        self.assertTrue(plugin.get_state(), "零配置即用：未显式禁用时应为启用")
        plugin.init_plugin({"enabled": False})
        self.assertFalse(plugin.get_state())

    def test_icon_files_exist(self) -> None:
        png = ROOT / "icons" / "mpoverview.png"
        svg = ROOT / "icons" / "mpoverview.svg"
        self.assertTrue(png.is_file())
        self.assertTrue(svg.is_file())
        self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(PLUGIN_CLS.plugin_icon.rsplit("/", 1)[-1], "mpoverview.png")


def main() -> int:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n合计 {total} 个用例，通过 {total - failed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
