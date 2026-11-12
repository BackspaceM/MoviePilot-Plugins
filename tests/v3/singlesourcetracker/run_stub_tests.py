"""离线把插件装起来，检查生命周期、路由、按钮和页面。"""

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

RULES_TEXT = "海贼王 => A站 => HDR;;DDP5.1 => re:(?i)x264\n火影忍者 => B站 => 杜比视界;;H.265 => CAM"


class StubTests(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({
            "enabled": True, "rules_text": RULES_TEXT, "lock_sites": True,
            "action_mode": "notice", "interval": 30, "notify": False,
        })

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
        self.assertIn(f"/api/v1/plugin/{PLUGIN_ID}/snapshot", paths)
        json.dumps(apis and self.plugin.get_page(), ensure_ascii=False)

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

    def test_form_is_compact(self):
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
        self.assertEqual(len(models), 6)
        self.assertEqual(set(defaults), set(models))
        self.assertEqual(defaults["rules_text"], RULES_TEXT)

    def test_rules_are_parsed_from_config(self):
        self.assertEqual(len(self.plugin._rules), 2)
        self.assertEqual(self.plugin._rule_errors, [])
        self.assertEqual(self.plugin._rules[0]["include"][1]["display"], "DDP5.1")

    def test_page_never_raises(self):
        for value in (None, {}, {"x": 1}, [{"x": 1}], "text", 1, True):
            self.plugin._data = cc._AnyData(value)
            page = self.plugin.get_page()
            self.assertIsInstance(page, list)
            self.assertTrue(all(item.get("text") for item in page[:-1]))
        self.plugin._data = {}

    def test_page_shows_current_subscriptions(self):
        page = self.plugin.get_page()
        self.assertIn("MP 当前订阅", page[-1]["html"])
        self.assertIn("规则怎么写", page[-1]["html"])

    def test_page_escapes_names(self):
        self.plugin.save_data("sst_snapshot", {
            "checked_at": "2026-11-12 16:00:00",
            "subscriptions": [{"name": "<img src=x onerror=alert(1)>", "sites": "A"}],
            "stats": {"problems": 0},
        })
        html = self.plugin.get_page()[-1]["html"]
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_api_endpoints(self):
        self.assertIsInstance(self.plugin.api_snapshot(), MODULE.Response)
        self.assertIsInstance(self.plugin.api_check({}), MODULE.Response)
        self.assertIsInstance(self.plugin.api_refresh({}), MODULE.Response)
        self.assertIsInstance(self.plugin.api_template({}), MODULE.Response)
        self.assertFalse(self.plugin.api_clean({}).success)
        self.assertIsInstance(self.plugin.api_unlock({}), MODULE.Response)

    def test_bad_regex_does_not_break_page(self):
        self.plugin.init_plugin({"rules_text": "剧名 => A站 => re:[( => 排除"})
        self.assertTrue(self.plugin._rule_errors)
        self.assertIsInstance(self.plugin.get_page(), list)
        self.assertIsInstance(self.plugin.get_form()[0], list)

    def test_service_and_command(self):
        self.assertIsInstance(self.plugin.get_service(), list)
        self.assertEqual(self.plugin.get_command()[0]["cmd"], "/single_source")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StubTests)
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
