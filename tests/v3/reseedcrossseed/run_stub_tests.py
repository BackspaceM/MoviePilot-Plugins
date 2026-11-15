#!/usr/bin/env python3
"""Reseed 辅种插件离线回归测试（复用全仓契约桩，不需要 MoviePilot 宿主）。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "plugins.v3" / "reseedcrossseed"
sys.path.insert(0, str(ROOT / "tests" / "v3"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
from test_core import make_torrent  # noqa: E402

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
        self.assertEqual(PLUGIN_ID, "ReseedCrossSeed")
        self.assertEqual(PLUGIN_CLS.plugin_version, "1.0.0")
        self.assertTrue(PLUGIN_CLS.plugin_icon.endswith("/icons/reseedcrossseed.png"))
        for name in ("init_plugin", "get_state", "get_api", "get_form", "get_page", "stop_service"):
            self.assertTrue(callable(getattr(PLUGIN_CLS, name, None)), name)

    def test_routes_register_in_fastapi(self):
        from fastapi import FastAPI

        app = FastAPI()
        for route in self.plugin.get_api():
            self.assertEqual(route.get("auth"), "bear")
            app.add_api_route(
                f"/{route['path'].lstrip('/')}", route["endpoint"], methods=route["methods"]
            )
        paths = {route.path for route in app.routes}
        for suffix in ("/run", "/dryrun", "/probe", "/clear_cache", "/status"):
            self.assertIn(suffix, paths)

    def test_form_and_defaults(self):
        form, defaults = self.plugin.get_form()
        self.assertIsInstance(form, list)
        self.assertFalse(defaults.get("enabled"))
        self.assertEqual(defaults.get("api"), CORE.DEFAULT_API)
        models = {
            node.get("props", {}).get("model")
            for node in _walk(form) if isinstance(node.get("props"), dict)
        }
        for key in ("enabled", "username", "password", "dirs", "bindings", "sites", "schedule_mode"):
            self.assertIn(key, models)

    def test_page_buttons_all_clickable(self):
        page = self.plugin.get_page()
        buttons = [node for node in _walk(page) if node.get("component") == "VBtn"]
        self.assertEqual(len(buttons), 5)
        declared = {route["path"].lstrip("/") for route in self.plugin.get_api()}
        for button in buttons:
            self.assertTrue(str(button.get("text") or "").strip(), "按钮不能有空文字")
            click = button.get("events", {}).get("click", {})
            self.assertEqual(click.get("api", "").split(f"plugin/{PLUGIN_ID}/")[-1] in declared, True)

    def test_page_renders_with_state_and_escapes(self):
        self.plugin._last = {"finished_at": "2026-11-15 12:00:00", "indexed": 3, "hits": 2,
                             "added": 1, "exists": 0, "failed": 1, "dry_run": True}
        self.plugin._service = {"ok": True, "message": "<script>ok</script>", "sites": 7,
                                "checked_at": "2026-11-15 12:00:00"}
        page = self.plugin.get_page()
        html_text = "".join(str(node.get("html") or "") for node in _walk(page))
        self.assertIn("Reseed 辅种", html_text)
        self.assertNotIn("<script>ok</script>", html_text)
        self.assertIn("&lt;script&gt;", html_text)

    def test_init_config_roundtrip(self):
        self.plugin.init_plugin({
            "enabled": True, "username": "u", "password": "p",
            "dirs": "/downloads\n# comment\n/media",
            "bindings": "HDU:青蛙:pk1", "sites": "HDU,TJUPT",
            "cron": "bad", "max_per_run": "99999", "dry_run": True,
        })
        self.assertTrue(self.plugin.get_state())
        self.assertEqual(self.plugin._dirs, ["/downloads", "/media"])
        self.assertIn("hdu", self.plugin._bindings)
        self.assertEqual(self.plugin._cron, CORE.DEFAULT_CRON)
        self.assertEqual(self.plugin._max_per_run, 2000)
        self.assertTrue(self.plugin._dry_run)
        self.assertEqual(self.plugin.get_service(), [])

    def test_service_registration(self):
        self.plugin.init_plugin({"enabled": True, "schedule_mode": "计划任务", "cron": "0 5 * * *"})
        services = self.plugin.get_service()
        self.assertEqual(len(services), 1)
        self.assertTrue(callable(services[0]["func"]))
        self.plugin.init_plugin({"enabled": True, "schedule_mode": "固定间隔", "interval_minutes": 120})
        self.assertEqual(self.plugin.get_service()[0]["trigger"], "interval")

    def test_api_guards(self):
        self.assertFalse(self.plugin.api_run().success)
        self.assertFalse(self.plugin.api_dryrun().success)
        self.assertTrue(self.plugin.api_status().success)
        self.assertTrue(self.plugin.api_clear_cache().success)
        self.assertFalse(self.plugin.api_probe().success)

    def test_probe_with_stub_client(self):
        self.plugin.init_plugin({"username": "u", "password": "p"})

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                return MODULE.site.ServiceResult(True, "登录成功", "token")

            def sites_info(self):
                return MODULE.site.ServiceResult(True, "ok", [
                    {"name": "HDU", "base_url": "https://hdu.example/"},
                    {"name": "TJUPT", "base_url": "https://tjupt.example/"},
                ])

        with mock.patch.object(MODULE.site, "ReseedClient", FakeClient):
            response = self.plugin.api_probe()
        self.assertTrue(response.success)
        self.assertIn("HDU", response.message)
        self.assertEqual(self.plugin._service.get("sites"), 2)

    def test_probe_failure_message(self):
        self.plugin.init_plugin({"username": "u", "password": "p"})

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                return MODULE.site.ServiceResult(False, "Invalid username or password!")

        with mock.patch.object(MODULE.site, "ReseedClient", FakeClient):
            response = self.plugin.api_probe()
        self.assertFalse(response.success)
        self.assertIn("Invalid username", response.message)

    def test_merged_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "a" / "One.mkv").write_bytes(b"x" * 100)
            (root / "b" / "Two.mkv").write_bytes(b"y" * 200)
            self.plugin._dirs = [str(root / "a"), str(root / "b")]
            index, items = self.plugin._build_merged_index()
            self.assertEqual(set(index["result"]), {"One.mkv", "Two.mkv"})
            self.assertEqual(items["One.mkv"].save_path, str(root / "a"))
            self.assertEqual(items["Two.mkv"].save_path, str(root / "b"))

    def test_process_one_dry_run(self):
        local = CORE.IndexItem("Pack", "/downloads", {"a.mkv": 100}, 100)
        binding = CORE.SiteBinding("HDU", "青蛙", "pk")
        mp_site = MODULE.site.MpSite("青蛙", "https://hdu.example/")
        hit = CORE.Hit("Pack", "HDU", "12345", True)
        content = make_torrent("Pack", {"a.mkv": 100})
        gateway = MODULE.site.DownloaderGateway()
        self.plugin._timeout = 10
        self.plugin._bindings = {"hdu": binding}
        with mock.patch.object(MODULE.site, "download_torrent", return_value=content):
            result = self.plugin._process_one(
                hit, {"Pack": local}, {"青蛙": mp_site}, set(), gateway, set(), True, False
            )
        self.assertEqual(result, "added")

    def test_process_one_rejects_mismatch(self):
        local = CORE.IndexItem("Pack", "/downloads", {"a.mkv": 100}, 100)
        binding = CORE.SiteBinding("HDU", "青蛙", "pk")
        mp_site = MODULE.site.MpSite("青蛙", "https://hdu.example/")
        hit = CORE.Hit("Pack", "HDU", "12345", True)
        content = make_torrent("Pack", {"a.mkv": 999})
        self.plugin._bindings = {"hdu": binding}
        with mock.patch.object(MODULE.site, "download_torrent", return_value=content):
            result = self.plugin._process_one(
                hit, {"Pack": local}, {"青蛙": mp_site}, set(),
                MODULE.site.DownloaderGateway(), set(), True, False
            )
        self.assertEqual(result, "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
