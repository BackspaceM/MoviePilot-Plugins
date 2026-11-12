"""离线把插件装起来，检查路由、按钮和几个接口能不能真的走通。"""

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


class StubTests(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({
            "enabled": True, "target_site": 3, "action_mode": "notice",
            "interval": 30, "notify": False,
        })

    def test_plugin_id_and_response_model(self):
        self.assertEqual(PLUGIN_ID, "SingleSourceTracker")
        self.assertTrue(issubclass(MODULE.Response, BaseModel))

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
                if node.get("component") in ("VSelect", "VTextField", "VSwitch", "VTextarea"):
                    models.append(node["props"]["model"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(form)
        self.assertEqual(len(models), 6)
        self.assertEqual(set(defaults), set(models))

    def test_page_renders(self):
        page = self.plugin.get_page()
        self.assertIsInstance(page, list)
        self.assertIn("单源追剧", page[0]["html"])
        self.assertTrue(all(item.get("text") for item in page[1:]))

    def test_api_endpoints(self):
        self.assertIsInstance(self.plugin.api_snapshot(), MODULE.Response)
        self.assertIsInstance(self.plugin.api_check({}), MODULE.Response)
        self.assertIsInstance(self.plugin.api_refresh({}), MODULE.Response)
        denied = self.plugin.api_clean({})
        self.assertFalse(denied.success)
        bound = self.plugin.api_bind({})
        self.assertFalse(bound.success)

    def test_bind_requires_site(self):
        self.plugin._target_site = ""
        ok, message = self.plugin._bind_one(1)
        self.assertFalse(ok)
        self.assertIn("目标站点", message)

    def test_service_and_command(self):
        self.assertIsInstance(self.plugin.get_service(), list)
        self.assertEqual(self.plugin.get_command()[0]["cmd"], "/single_source")

    def test_page_escapes_binding_names(self):
        self.plugin.save_data("sst_bindings", [{
            "sid": 1, "name": "<img src=x onerror=alert(1)>", "season": 1,
            "site_name": "A", "baseline": {}, "locked": True,
        }])
        html = self.plugin.get_page()[0]["html"]
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StubTests)
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
