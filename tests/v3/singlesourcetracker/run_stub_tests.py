"""离线把插件装起来，检查生命周期、路由、按钮和接口兜底。"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()

PLUGIN_DIR = ROOT / "plugins.v3" / "singlesourcetracker"
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)

from fastapi import FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from pydantic import BaseModel  # noqa: E402

CONFIG = {
    "enabled": True, "apply_to_mp": True, "quick_sub": 0, "quick_site": 0,
    "quick_tags": [], "quick_include": "", "quick_exclude": "", "rules_text": "",
    "action_mode": "notice", "interval": 30,
}


class StubTests(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin(dict(CONFIG))

    def test_plugin_id_and_abstract_methods(self):
        self.assertEqual(PLUGIN_ID, "SingleSourceTracker")
        self.assertTrue(issubclass(MODULE.Response, BaseModel))
        for name in ("init_plugin", "get_state", "get_api", "get_form", "get_page", "stop_service"):
            self.assertTrue(callable(getattr(self.plugin, name, None)), name)

    def test_routes_register(self):
        app = FastAPI()
        apis = self.plugin.get_api()
        self.assertTrue(all(item.get("auth") == "bear" for item in apis))
        for api in apis:
            route = dict(api)
            route.pop("auth", None)
            route.pop("allow_anonymous", None)
            route["path"] = f"/api/v1/plugin/{PLUGIN_ID}{route['path']}"
            route.setdefault("route_class_override", APIRoute)
            app.router.add_api_route(**route, tags=["plugin"])
        paths = {item.path for item in app.routes}
        self.assertIn(f"/api/v1/plugin/{PLUGIN_ID}/apply", paths)
        json.dumps(self.plugin.get_page(), ensure_ascii=False)

    def test_buttons_match_api(self):
        declared = {}
        for api in self.plugin.get_api():
            declared[api["path"].lstrip("/")] = {m.upper() for m in (api.get("methods") or ["GET"])}
        clicks = cc.collect_page_clicks(self.plugin)
        self.assertTrue(clicks)
        prefix = f"plugin/{PLUGIN_ID}/"
        for click in clicks:
            path = click["api"].lstrip("/")
            self.assertTrue(path.startswith(prefix), path)
            action = path[len(prefix):].split("?", 1)[0].split("/", 1)[0]
            self.assertIn(action, declared, action)
            self.assertIn(click["method"].upper(), declared[action])

    def test_form_is_ten_items(self):
        form, defaults = self.plugin.get_form()
        models = []

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("props"), dict) and "model" in node["props"]:
                    models.append(node["props"]["model"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(form)
        self.assertEqual(len(models), 10)
        self.assertEqual(set(defaults), set(models))
        self.assertIsInstance(defaults["quick_tags"], list)

    def test_page_never_raises(self):
        for value in (None, {}, {"x": 1}, [{"x": 1}], "text", 1, True):
            self.plugin._data = cc._AnyData(value)
            page = self.plugin.get_page()
            self.assertIsInstance(page, list)
            self.assertTrue(all(item.get("text") for item in page[:-1]))
        self.plugin._data = {}

    def test_page_shows_usage_steps(self):
        html = self.plugin.get_page()[-1]["html"]
        self.assertIn("MP 当前订阅", html)
        self.assertIn("怎么用", html)

    def test_page_escapes_names(self):
        self.plugin.save_data("sst_snapshot", {
            "checked_at": "2026-11-12 18:00:00",
            "subscriptions": [{"name": "<img src=x onerror=alert(1)>", "sites": "A"}],
            "stats": {"problems": 0},
        })
        html = self.plugin.get_page()[-1]["html"]
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_every_endpoint_never_raises(self):
        methods = [
            "api_snapshot", "api_scan", "api_apply", "api_restore", "api_check",
            "api_refresh", "api_template", "api_clean_preview", "api_clean",
        ]
        for name in methods:
            fn = getattr(self.plugin, name)
            for payload in ({}, None, "bad", [1, 2], {"confirm": "清理"}):
                try:
                    result = fn() if name == "api_snapshot" else fn(payload)
                except Exception as exc:
                    self.fail(f"{name} 抛异常了：{type(exc).__name__}: {exc}")
                self.assertIsInstance(result, MODULE.Response)
                self.assertIsInstance(result.message, str)

    def test_clean_requires_two_steps(self):
        denied = self.plugin.api_clean({"confirm": "清理"})
        self.assertFalse(denied.success)
        preview = self.plugin.api_clean_preview({})
        self.assertIsInstance(preview, MODULE.Response)
        plan = self.plugin.get_data("sst_clean_plan", {})
        if isinstance(plan, dict) and plan.get("id"):
            ok = self.plugin.api_clean({"plan_id": plan["id"], "confirm": "清理"})
            self.assertIsInstance(ok, MODULE.Response)

    def test_scan_then_form_items(self):
        self.plugin.save_data("sst_catalog", {
            "subscriptions": [{"id": 11, "name": "海贼王", "season": 1}],
            "sites": [{"id": 1, "name": "A站"}],
            "scanned_at": "2026-11-12 18:00:00",
        })
        form, _ = self.plugin.get_form()
        text = json.dumps(form, ensure_ascii=False)
        self.assertIn("海贼王", text)
        self.assertIn("A站", text)

    def test_service_and_command(self):
        self.assertIsInstance(self.plugin.get_service(), list)
        self.assertEqual(self.plugin.get_command()[0]["cmd"], "/single_source")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StubTests)
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
