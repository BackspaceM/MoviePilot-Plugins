#!/usr/bin/env python3
"""插件调试插件的离线回归测试（复用全仓契约桩，不需要 MoviePilot 宿主）。"""

from __future__ import annotations

import base64
import io
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "plugins.v3" / "plugindebugger"
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
INSTALLER = MODULE.installer
RENDERER = MODULE.renderer

INIT = '''from app.sdk.plugin import _PluginBase

class DemoPlugin(_PluginBase):
    plugin_name = "示例插件"
    plugin_version = "2.3.4"
'''


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Demo/__init__.py", INIT)
        zf.writestr("Demo/helper.py", "value = 1\n")
    return buf.getvalue()


def clicks(node, out):
    if isinstance(node, dict):
        event = node.get("events")
        if isinstance(event, dict) and isinstance(event.get("click"), dict):
            out.append(event["click"])
        for value in node.values():
            clicks(value, out)
    elif isinstance(node, list):
        for value in node:
            clicks(value, out)
    return out


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({})

    def test_plugin_id_and_metadata(self):
        self.assertEqual(PLUGIN_ID, "PluginDebugger")
        self.assertEqual(PLUGIN_CLS.plugin_version, "1.0.0")
        self.assertTrue(PLUGIN_CLS.plugin_icon.endswith("/icons/plugindebugger.png"))
        for name in ("init_plugin", "get_state", "get_api", "get_form", "get_page", "stop_service"):
            self.assertTrue(callable(getattr(PLUGIN_CLS, name, None)), name)

    def test_routes_register_in_fastapi(self):
        from fastapi import FastAPI

        app = FastAPI()
        for item in self.plugin.get_api():
            self.assertEqual(item.get("auth"), "bear")
            app.add_api_route("/" + str(item["path"]).lstrip("/"), item["endpoint"],
                              methods=item.get("methods") or ["GET"],
                              response_model=MODULE.Response)
        paths = {r.path for r in app.routes}
        for item in self.plugin.get_api():
            self.assertIn("/" + str(item["path"]).lstrip("/"), paths)

    def test_buttons_match_declared_actions(self):
        declared = {}
        for item in self.plugin.get_api():
            declared[str(item["path"]).lstrip("/")] = {
                str(m).upper() for m in (item.get("methods") or ["GET"])}
        for click in clicks(self.plugin.get_page(), []):
            api = str(click.get("api") or "")
            self.assertTrue(api.startswith(f"plugin/{PLUGIN_ID}/"), api)
            action = api.rsplit("/", 1)[-1]
            self.assertIn(action, declared, action)
            self.assertIn(str(click.get("method") or "post").upper(), declared[action])

    def test_form_has_six_settings_and_page_is_json_safe(self):
        form, defaults = self.plugin.get_form()
        models = []
        def walk(node):
            if isinstance(node, dict):
                props = node.get("props")
                if isinstance(props, dict) and "model" in props:
                    models.append(props["model"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(form)
        self.assertEqual(len(models), 6)
        self.assertEqual(set(models), set(defaults))
        json.dumps(self.plugin.get_page(), ensure_ascii=False)

    def test_page_is_static_without_polling(self):
        page = json.dumps(self.plugin.get_page(), ensure_ascii=False)
        for gone in ("setInterval", "__csPoll", "cs-ok", "<table"):
            self.assertNotIn(gone, page)
        self.assertIn("linear-gradient(135deg,#4F46E5,#7C3AED)", page)
        self.assertIn("pdgTip", page)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({})
        self.plugin._data_root = lambda: self.root / "data"  # type: ignore[method-assign]
        self.plugin._plugins_root_override = self.root / "plugins"
        self.plugin._refresh_cache()

    def tearDown(self):
        self.tmp.cleanup()

    def _upload(self, name="demo.zip", raw=None):
        payload = base64.b64encode(raw if raw is not None else make_zip()).decode()
        return self.plugin.api_upload({"name": name, "data": payload})

    def test_upload_rejects_bad_payload(self):
        self.assertFalse(self.plugin.api_upload({"name": "a.txt", "data": "AA=="}).success)
        self.assertFalse(self.plugin.api_upload({"name": "a.zip", "data": "!!!"}).success)
        self.assertFalse(self.plugin.api_upload({"name": "a.zip", "data": ""}).success)
        self.plugin._max_size_mb = 0
        self.assertIn("上限", self.plugin.api_upload(
            {"name": "a.zip", "data": base64.b64encode(make_zip()).decode()}).message)

    def test_upload_install_backup_rollback(self):
        first = self._upload()
        self.assertTrue(first.success, first.message)
        self.assertTrue((self.root / "data" / "uploads" / "demo.zip").is_file())

        installed = self.plugin.api_install({"name": "demo.zip"})
        self.assertTrue(installed.success, installed.message)
        dest = self.root / "plugins" / "demoplugin"
        self.assertTrue((dest / "__init__.py").is_file())
        self.assertIn("DemoPlugin", (dest / "__init__.py").read_text(encoding="utf-8"))
        self.assertEqual(installed.data["plugin_id"], "DemoPlugin")
        self.assertEqual(installed.data["files"], 2)
        self.assertTrue(installed.data["backup"] == "")

        # 第二次安装会备份旧版本
        (dest / "__init__.py").write_text("changed = 1\n", encoding="utf-8")
        second = self.plugin.api_install({"name": "demo.zip"})
        self.assertTrue(second.success, second.message)
        self.assertTrue(second.data["backup"])
        self.assertIn("DemoPlugin", (dest / "__init__.py").read_text(encoding="utf-8"))
        self.assertEqual(self.plugin._read_stats()["install_count"], 2)

        rolled = self.plugin.api_rollback({"plugin_id": "DemoPlugin"})
        self.assertTrue(rolled.success, rolled.message)
        self.assertEqual((dest / "__init__.py").read_text(encoding="utf-8"), "changed = 1\n")

    def test_install_from_container_path(self):
        path = self.root / "local.zip"
        path.write_bytes(make_zip())
        result = self.plugin.api_install({"path": str(path)})
        self.assertTrue(result.success, result.message)

    def test_install_reports_unknown_file_and_bad_zip(self):
        self.assertIn("没有", self.plugin.api_install({"name": "nope.zip"}).message)
        bad = self._upload("bad.zip", b"not a zip")
        self.assertFalse(bad.success)
        self.assertIn("ZIP", bad.message)

    def test_overwrite_switch_blocks_second_install(self):
        self.assertTrue(self._upload().success)
        self.assertTrue(self.plugin.api_install({"name": "demo.zip"}).success)
        self.plugin._allow_overwrite = False
        blocked = self.plugin.api_install({"name": "demo.zip"})
        self.assertFalse(blocked.success)
        self.assertIn("允许覆盖", blocked.message)

    def test_delete_is_two_steps_and_confirm_sits_in_same_row(self):
        self.assertTrue(self._upload().success)
        self.plugin._refresh_cache()
        page = json.dumps(self.plugin.get_page(), ensure_ascii=False)
        self.assertNotIn("确认删除", page)

        first = self.plugin.api_delete({"name": "demo.zip"})
        self.assertTrue(first.success)
        plan = self.plugin._delete_plan
        self.assertTrue(plan["plan_id"])

        page = self.plugin.get_page()
        buttons = []
        def walk_col(node):
            if isinstance(node, dict):
                if node.get("component") == "VBtn" and "确认删除" in str(node.get("text")):
                    buttons.append(node["events"]["click"]["params"])
                for value in node.values():
                    walk_col(value)
            elif isinstance(node, list):
                for value in node:
                    walk_col(value)
        walk_col(page)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["plan_id"], plan["plan_id"])

        self.assertFalse(self.plugin.api_delete(
            {"name": "demo.zip", "plan_id": plan["plan_id"], "confirm": "随便"}).success)
        self.assertFalse(self.plugin.api_delete(
            {"name": "other.zip", "plan_id": plan["plan_id"], "confirm": "删除"}).success)
        self.assertTrue((self.root / "data" / "uploads" / "demo.zip").is_file())

        done = self.plugin.api_delete(
            {"name": "demo.zip", "plan_id": plan["plan_id"], "confirm": "删除"})
        self.assertTrue(done.success, done.message)
        self.assertFalse((self.root / "data" / "uploads" / "demo.zip").is_file())
        self.assertEqual(self.plugin._delete_plan, {})

    def test_expired_plan_is_rejected(self):
        self.assertTrue(self._upload().success)
        first = self.plugin.api_delete({"name": "demo.zip"})
        self.assertTrue(first.success)
        plan = dict(self.plugin._delete_plan)
        self.plugin._delete_plan = dict(plan, expires_at=0)
        self.assertFalse(self.plugin.api_delete(
            {"name": "demo.zip", "plan_id": plan["plan_id"], "confirm": "删除"}).success)

    def test_endpoints_never_raise(self):
        broken = PLUGIN_CLS()
        broken.init_plugin({})
        broken._uploads_dir = lambda: (_ for _ in ()).throw(OSError("boom"))  # type: ignore[method-assign]
        for call in (lambda: broken.api_upload({"name": "x.zip", "data": "AA=="}),
                     lambda: broken.api_delete({"name": "x.zip"}),
                     lambda: broken.api_refresh({}),
                     lambda: broken.api_install({"name": "x.zip"})):
            result = call()
            self.assertIsInstance(result, MODULE.Response)
            self.assertIsInstance(result.message, str)

    def test_render_escapes_uploaded_names(self):
        self.plugin._cache["uploads"] = [{"name": "<img src=x>.zip", "size_text": "1 MB", "mtime": "—"}]
        self.plugin._cache["log"] = ["<script>alert(1)</script>"]
        html = self.plugin.get_page()[-1]["html"]
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert(1)", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
