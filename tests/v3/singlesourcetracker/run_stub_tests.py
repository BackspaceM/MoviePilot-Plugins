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

    def test_form_sub_is_combobox(self):
        form, _ = self.plugin.get_form()
        text = json.dumps(form, ensure_ascii=False)
        # 这部剧要能下拉也能手填
        self.assertIn("VCombobox", text)
        self.assertEqual(text.count("VCombobox"), 1)

    def test_scan_error_message_has_reason(self):
        # 接口兜底要带具体原因，方便定位，不能只给类型名
        text = MODULE._why(TypeError("列表读不出来"))
        self.assertIn("列表读不出来", text)

        original = MODULE.store.subscriptions
        def boom():
            raise TypeError("订阅接口炸了")

        MODULE.store.subscriptions = boom
        try:
            result = self.plugin.api_scan({})
        finally:
            MODULE.store.subscriptions = original
        self.assertFalse(result.success)
        self.assertIn("订阅接口炸了", result.message)
        self.assertIn("TypeError", result.message)

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


class HostSignatureTests(unittest.TestCase):
    """宿主 _PluginBase.get_data(key=None, plugin_id=None) 没有 default 参数。

    离线 stub 的 get_data(key, default) 有默认值语义，会掩盖「数据不存在返回 None」
    导致的 TypeError。这里按真实签名跑一遍首次扫描，防止回归。
    """

    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin(dict(CONFIG))
        self.db = {}
        self.plugin.get_data = lambda key=None, plugin_id=None: self.db.get(key)
        self.plugin.save_data = lambda key, value, plugin_id=None: self.db.__setitem__(key, value)

    def test_first_scan_with_empty_host_data_succeeds(self):
        result = self.plugin.api_scan({})
        self.assertTrue(result.success, result.message)
        self.assertNotIn("NoneType", result.message)
        self.assertEqual(len(self.db.get("sst_log") or []), 1)

    def test_every_endpoint_survives_empty_host_data(self):
        for name in ("api_snapshot", "api_refresh", "api_template", "api_clean_preview"):
            result = getattr(self.plugin, name)({}) if name != "api_snapshot" else self.plugin.api_snapshot()
            self.assertNotIn("NoneType", str(getattr(result, "message", "")), name)
        self.assertIsInstance(self.plugin.get_page(), list)
        form, defaults = self.plugin.get_form()
        self.assertTrue(form)

    def test_form_has_no_zero_placeholder_or_default(self):
        form, defaults = self.plugin.get_form()
        text = json.dumps(form, ensure_ascii=False)
        self.assertNotIn('"value": 0', text)
        self.assertNotIn("先去详情页点「扫描 MP 订阅」", text)
        self.assertEqual(defaults["quick_sub"], "")
        self.assertEqual(defaults["quick_site"], "")
        self.assertTrue(any("clearable" in json.dumps(node, ensure_ascii=False)
                            for node in [form]))

    def test_store_collections_tolerate_none(self):
        store = MODULE.store
        self.assertEqual(store.log_lines(self.plugin), [])
        store.append_log(self.plugin, "一条日志")
        self.assertEqual(len(store.log_lines(self.plugin)), 1)
        stats = store.inspect(None, None, None, None)["stats"]
        self.assertEqual(stats["subscriptions"], 0)
        self.assertEqual(store.site_options(), [])

    def test_source_has_no_fake_default_argument(self):
        """get_data 第二参数是 plugin_id，不是默认值；源码里不许再出现伪 default。"""
        import re
        for path in (PLUGIN_DIR / "store.py", PLUGIN_DIR / "__init__.py"):
            text = path.read_text(encoding="utf-8")
            bad = re.findall(r"get_data\([^)]*,\s*(?:\{\}|\[\]|\"\"|'')", text)
            self.assertEqual(bad, [], f"{path.name}: {bad}")


if __name__ == "__main__":
    suite = unittest.TestSuite()
    for case in (StubTests, HostSignatureTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)