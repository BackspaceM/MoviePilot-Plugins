#!/usr/bin/env python3
"""MP 备份迁移插件的离线回归测试（复用全仓契约桩，无需 MoviePilot 宿主）。"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "plugins.v3" / "mpbackup"
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
BACKUP = MODULE.backup
RENDERER = MODULE.renderer
RESTORE = MODULE.restore


def make_data_snapshot(directory: pathlib.Path, name: str) -> pathlib.Path:
    """造一份各类都有内容的快照，用来测预览和试运行清单。"""
    path = directory / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "schema": 2, "contains_secrets": True, "counts": {"users": 2},
        }))
        zf.writestr("users.json", json.dumps([
            {"name": "u1", "hashed_password": "$2b$abc"},
            {"name": "<img src=x onerror=alert(1)>", "email": "a@b.c"},
        ]))
        zf.writestr("subscriptions.json", json.dumps([
            {"name": "A", "media_source": "tmdb", "media_id": 1, "season": True},
        ]))
        zf.writestr("subscription_history.json", "[]")
        zf.writestr("sites.json", json.dumps([{"name": "s", "domain": "d.com"}]))
        zf.writestr("site_secrets.json", json.dumps([
            {"domain": "d.com", "cookie": "C", "ua": "U", "apikey": "K", "token": "T"},
        ]))
        zf.writestr("system_config.json", json.dumps({"Downloaders": {"a": 1}}))
        zf.writestr("plugin_configs.json", json.dumps([
            {"instance_id": "p1", "config_data": {"x": 1}},
        ]))
        zf.writestr("download_history.json", json.dumps([
            {"download_hash": "h1", "title": "t"},
        ]))
        zf.writestr("transfer_history.json", "[]")
        zf.writestr("services.json", "[]")
        zf.writestr("settings.json", "{}")
        zf.writestr("plugin_data/", "")
        zf.writestr("plugin_data/demo/k.json", json.dumps({"v": 1}))
    return path


def assert_json_safe(node) -> None:
    """页面组件只能由 dict/list/标量组成，不能混进 datetime/Path/set/tuple/对象。"""
    if isinstance(node, dict):
        for key, value in node.items():
            assert isinstance(key, str)
            assert not isinstance(value, (datetime.datetime, pathlib.Path, set, tuple))
            assert_json_safe(value)
    elif isinstance(node, list):
        for value in node:
            assert_json_safe(value)
    else:
        assert isinstance(node, (str, int, float, bool, type(None))), type(node)


class TestContract(unittest.TestCase):
    """防止按钮点击 404 和接口注册失败。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": self.tmp.name})
        self.plugin.get_data_path = lambda *a, **k: self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_plugin_id_and_metadata(self) -> None:
        self.assertEqual(PLUGIN_ID, "MPBackup")
        expected = {
            "plugin_name": "MP 备份迁移", "plugin_version": "1.2.0",
            "plugin_author": "BackspaceM", "plugin_config_prefix": "mpbackup_",
            "plugin_color": "#6366F1", "system_version": ">=3.0.0", "plugin_order": 95,
        }
        for field, value in expected.items():
            self.assertEqual(getattr(PLUGIN_CLS, field), value, field)
        self.assertIn("icons/mpbackup.png", PLUGIN_CLS.plugin_icon)

    def test_response_is_pydantic_model(self) -> None:
        from pydantic import BaseModel

        self.assertTrue(issubclass(MODULE.Response, BaseModel))
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
        self.plugin.api_backup()  # 有快照才会渲染出每行的删除按钮
        clicks = cc.collect_click_events(self.plugin.get_page())
        self.assertTrue(clicks, "详情页至少应有一个可点击按钮")
        prefix = f"plugin/{PLUGIN_ID}/"
        actions = set()
        for click in clicks:
            self.assertTrue(click["api"].startswith(prefix), click["api"])
            action = click["api"][len(prefix):].split("?", 1)[0].split("/", 1)[0]
            self.assertIn(action, declared, click["api"])
            self.assertIn(click["method"].upper(), declared[action], click["api"])
            actions.add(action)
        for action in ("backup", "compare", "restore-guide", "clean", "refresh",
                       "import-preview", "import", "import-guide", "delete"):
            self.assertIn(action, actions)
        for api in self.plugin.get_api():
            self.assertEqual(api.get("auth"), "bear")

    def test_new_endpoints_are_relative_and_bear(self) -> None:
        paths = {str(a["path"]) for a in self.plugin.get_api()}
        for path in ("/import-preview", "/import", "/import-guide", "/delete"):
            self.assertIn(path, paths)
            self.assertTrue(path.startswith("/"))
        for api in self.plugin.get_api():
            self.assertFalse(str(api["path"]).startswith("plugin/"))

    def test_button_text_is_toplevel_not_in_props(self) -> None:
        def walk(node):
            if isinstance(node, dict):
                if node.get("component") == "VBtn":
                    self.assertTrue(node.get("text"))
                    self.assertNotIn("text", node.get("props", {}),
                                     "props.text 会被 Vuetify 当成 variant")
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.plugin.get_page())

    def test_page_body_is_single_rendered_div(self) -> None:
        divs = [i for i in self.plugin.get_page()
                if i.get("component") == "div" and i.get("html")]
        self.assertEqual(len(divs), 1)
        self.assertIn("MP 备份迁移", divs[0]["html"])
        self.assertIn("mpbk-grid", divs[0]["html"])

    def test_page_components_are_json_safe(self) -> None:
        assert_json_safe(self.plugin.get_page())

    def test_get_page_does_not_touch_host_or_disk(self) -> None:
        with mock.patch.object(BACKUP, "list_snapshots",
                               side_effect=AssertionError("页面期不该读目录")), \
             mock.patch.object(BACKUP, "load_manifest",
                               side_effect=AssertionError("页面期不该读 zip")), \
             mock.patch.object(BACKUP, "compare_snapshots",
                               side_effect=AssertionError("页面期不该对比")), \
             mock.patch.object(BACKUP, "host_ready",
                               side_effect=AssertionError("页面期不该碰宿主")):
            page = self.plugin.get_page()
        self.assertTrue(page)
        assert_json_safe(page)

    def test_get_page_and_form_never_raise_on_bad_cache(self) -> None:
        self.plugin._cache = {
            "snapshots": [{"name": pathlib.Path("x.zip"), "mtime": {1, 2},
                           "size_text": object(), "count": 3}],
            "sources": {"users": datetime.datetime.now()},
            "diff": {"sections": {"subscriptions": {"added": [{"x": object()}]}}},
        }
        page = self.plugin.get_page()
        self.assertIsInstance(page, list)
        self.assertTrue(page)
        with mock.patch.object(BACKUP, "CRON_CHOICES", None):
            form, defaults = self.plugin.get_form()
        self.assertIsInstance(form, list)
        self.assertIsInstance(defaults, dict)

    def test_api_returns_three_part_envelope(self) -> None:
        for name in ("api_snapshot", "api_backup", "api_compare", "api_restore_guide",
                     "api_clean", "api_refresh", "api_import_preview", "api_import",
                     "api_import_guide", "api_delete"):
            result = getattr(self.plugin, name)()
            self.assertIsInstance(result, MODULE.Response)
            self.assertEqual(set(result.model_dump()), {"success", "message", "data"})
            self.assertIsInstance(result.success, bool)
            self.assertIsInstance(result.message, str)
            self.assertIsInstance(result.data, dict)

    def test_state_and_service(self) -> None:
        self.assertIsInstance(self.plugin.get_state(), bool)
        services = self.plugin.get_service()
        self.assertEqual(len(services), 1)
        self.assertIn("trigger", services[0])
        self.assertTrue(callable(services[0]["func"]))

    def test_form_is_simple(self) -> None:
        form, defaults = PLUGIN_CLS().get_form()
        controls = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("component") in ("VSelect", "VSwitch", "VTextField",
                                             "VTextarea", "VFileInput"):
                    controls.append(node["component"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(form)
        self.assertLessEqual(len(controls), 8, f"设置项应精简，实际为 {controls}")
        self.assertEqual(set(defaults),
                         {"enabled", "backup_dir", "keep", "schedule", "sections",
                          "notify", "include_secrets", "import_strategy"})

    def test_backup_dir_hint_and_full_sections(self) -> None:
        form, defaults = self.plugin.get_form()
        text_field = next(item for item in form
                          if item.get("component") == "VTextField"
                          and item["props"].get("model") == "backup_dir")
        self.assertIn("填 MoviePilot 容器里能看到的路径，比如 NAS 映射到容器的目录；"
                      "留空用插件数据目录", text_field["props"]["hint"])
        section_select = next(item for item in form
                              if item.get("component") == "VSelect"
                              and item["props"].get("model") == "sections")
        values = {i["value"] for i in section_select["props"]["items"]}
        self.assertEqual(values, set(BACKUP.ALL_SOURCES))
        self.assertEqual(set(defaults["sections"]), set(BACKUP.ALL_SOURCES))
        for item in section_select["props"]["items"]:
            self.assertIsInstance(item["title"], str)
            self.assertTrue(item["title"])


class TestBackupFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.data = pathlib.Path(self.data_tmp.name)
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": str(self.dir), "keep": 3, "notify": False})

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.data_tmp.cleanup()

    def test_offline_snapshot_is_structurally_complete(self) -> None:
        result = BACKUP.write_snapshot(self.dir, str(self.data),
                                       list(BACKUP.DEFAULT_SECTIONS), 3)
        path = self.dir / result["file"]
        self.assertTrue(path.is_file())
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        for name in BACKUP.ALL_SOURCES:
            if name == "plugin_data":
                self.assertIn("plugin_data/", names)
            else:
                self.assertIn(name + ".json", names)
        self.assertIn("manifest.json", names)
        self.assertNotIn("site_secrets.json", names)

        manifest = result["manifest"]
        self.assertEqual(manifest["schema"], 2)
        self.assertIn("version", manifest)
        self.assertFalse(manifest["contains_secrets"])
        for name in BACKUP.ALL_SOURCES:
            self.assertIn(name, manifest["sources"])
            self.assertIn(name, manifest["counts"])
        self.assertIn("site_secrets", manifest["counts"])

        sources = manifest["sources"]
        for name in BACKUP.HOST_SOURCES:
            self.assertEqual(sources[name]["status"], "unavailable", name)
            self.assertTrue(sources[name]["reason"])

    def test_new_sections_have_own_json(self) -> None:
        result = BACKUP.write_snapshot(self.dir, str(self.data),
                                       list(BACKUP.DEFAULT_SECTIONS), 3)
        with zipfile.ZipFile(self.dir / result["file"]) as zf:
            names = set(zf.namelist())
        for name in ("users.json", "subscription_history.json", "plugin_configs.json",
                     "system_config.json", "download_history.json", "transfer_history.json"):
            self.assertIn(name, names)

    def test_history_defaults_are_full(self) -> None:
        self.assertIn("download_history", BACKUP.DEFAULT_SECTIONS)
        self.assertIn("transfer_history", BACKUP.DEFAULT_SECTIONS)
        self.assertEqual(BACKUP.HISTORY_LIMIT, 5000)
        self.assertEqual(BACKUP.SUBSCRIPTION_HISTORY_LIMIT, 2000)

    def test_secrets_switch_off_by_default(self) -> None:
        result = BACKUP.write_snapshot(self.dir, str(self.data),
                                       list(BACKUP.DEFAULT_SECTIONS), 3)
        with zipfile.ZipFile(self.dir / result["file"]) as zf:
            self.assertNotIn("site_secrets.json", zf.namelist())
        self.assertFalse(result["manifest"]["contains_secrets"])

    def test_secrets_switch_on_writes_plaintext_credentials(self) -> None:
        rows = [{"name": "s", "domain": "d.com", "url": "u", "pri": 1,
                 "is_active": True, "cookie": "COOKIE123", "ua": "UA",
                 "apikey": "AK", "token": "TK"}]
        with mock.patch.object(BACKUP, "host_ready", return_value=True), \
             mock.patch.object(BACKUP, "_sites", return_value=rows):
            result = BACKUP.write_snapshot(self.dir, str(self.data),
                                           list(BACKUP.DEFAULT_SECTIONS), 3, True)
        with zipfile.ZipFile(self.dir / result["file"]) as zf:
            self.assertIn("site_secrets.json", zf.namelist())
            sites = json.loads(zf.read("sites.json"))
            secrets = json.loads(zf.read("site_secrets.json"))
        self.assertNotIn("cookie", sites[0])
        self.assertNotIn("token", sites[0])
        self.assertNotIn("COOKIE123", json.dumps(sites))
        self.assertEqual(secrets[0]["cookie"], "COOKIE123")
        self.assertEqual(secrets[0]["token"], "TK")
        self.assertTrue(result["manifest"]["contains_secrets"])
        self.assertGreaterEqual(result["manifest"]["counts"]["site_secrets"], 1)

    def test_users_password_hash_kept_plain(self) -> None:
        with mock.patch.object(BACKUP, "host_ready", return_value=True), \
             mock.patch.object(BACKUP, "_users", return_value=[
                 {"id": 1, "name": "admin", "hashed_password": "$2b$abc",
                  "otp_secret": "S"}]):
            result = BACKUP.write_snapshot(self.dir, str(self.data),
                                           list(BACKUP.DEFAULT_SECTIONS), 3)
        with zipfile.ZipFile(self.dir / result["file"]) as zf:
            users = json.loads(zf.read("users.json"))
        self.assertEqual(users[0]["hashed_password"], "$2b$abc")
        self.assertEqual(users[0]["otp_secret"], "S")
        self.assertNotIn("***", json.dumps(users[0]))

    def test_plugin_data_is_masked_and_size_limited(self) -> None:
        (self.data / "one.json").write_text(
            json.dumps({"cookie": "should-not-appear", "name": "demo"}), encoding="utf-8")
        (self.data / "big.json").write_text("x" * (BACKUP.PLUGIN_FILE_LIMIT + 1), encoding="utf-8")
        result = BACKUP.write_snapshot(self.dir, str(self.data),
                                       list(BACKUP.DEFAULT_SECTIONS), 3)
        with zipfile.ZipFile(self.dir / result["file"]) as zf:
            names = zf.namelist()
            self.assertIn("plugin_data/one.json", names)
            self.assertNotIn("plugin_data/big.json", names)
            text = zf.read("plugin_data/one.json").decode("utf-8")
        self.assertNotIn("should-not-appear", text)
        self.assertIn("***", text)
        self.assertIn("demo", text)

    def test_rotation_only_removes_own_zips(self) -> None:
        for i in range(6):
            (self.dir / f"mpbackup_2026010{i}_000000_{i:06d}.zip").write_bytes(b"x")
        (self.dir / "other.zip").write_bytes(b"keep")
        (self.dir / "mpbackup_notes.txt").write_bytes(b"keep")
        removed = BACKUP.rotate(self.dir, 3)
        self.assertEqual(len(removed), 3)
        self.assertEqual(len(BACKUP.snapshot_files(self.dir)), 3)
        self.assertTrue((self.dir / "other.zip").is_file())
        self.assertTrue((self.dir / "mpbackup_notes.txt").is_file())

    def test_compare_reports_added_removed_changed(self) -> None:
        def make(name, subs, sites):
            with zipfile.ZipFile(self.dir / name, "w") as zf:
                zf.writestr("subscriptions.json", json.dumps(subs))
                zf.writestr("sites.json", json.dumps(sites))
                zf.writestr("services.json", "[]")
                zf.writestr("plugins.json", "[]")

        make("mpbackup_20260101_000000_000001.zip",
             [{"id": 1, "name": "A", "state": "R"}, {"id": 2, "name": "B"}],
             [{"name": "s1", "domain": "a.com", "pri": 1}])
        make("mpbackup_20260102_000000_000002.zip",
             [{"id": 1, "name": "A", "state": "S"}, {"id": 3, "name": "C"}],
             [{"name": "s1", "domain": "a.com", "pri": 2},
              {"name": "s2", "domain": "b.com", "pri": 1}])
        result = BACKUP.compare_snapshots(self.dir)
        self.assertTrue(result["ok"])
        diff = result["diff"]
        self.assertTrue(diff["has_drift"])
        subs = diff["sections"]["subscriptions"]
        self.assertEqual(len(subs["added"]), 1)
        self.assertEqual(len(subs["removed"]), 1)
        self.assertEqual(len(subs["changed"]), 1)
        self.assertEqual(subs["changed"][0]["changes"][0]["field"], "state")
        sites = diff["sections"]["sites"]
        self.assertEqual(len(sites["added"]), 1)
        self.assertEqual(len(sites["changed"]), 1)

    def test_compare_with_explicit_names(self) -> None:
        for i in range(2):
            with zipfile.ZipFile(self.dir / f"mpbackup_2026010{i}_000000_{i:06d}.zip", "w") as zf:
                for name in ("subscriptions", "sites", "services", "plugins"):
                    zf.writestr(name + ".json", "[]")
        bad = BACKUP.compare_snapshots(self.dir, a="../evil", b="mpbackup_20260100_000000_000000.zip")
        self.assertFalse(bad["ok"])
        good = BACKUP.compare_snapshots(self.dir,
                                        a="mpbackup_20260100_000000_000000.zip",
                                        b="mpbackup_20260101_000000_000001.zip")
        self.assertTrue(good["ok"])

    def test_restore_guide_text(self) -> None:
        name, text = BACKUP.restore_guide(self.dir)
        self.assertTrue(name.startswith("restore_") and name.endswith(".md"))
        self.assertTrue((self.dir / name).is_file())
        self.assertIn("不写回数据库", text)
        for name in ("settings.json", "sites.json", "services.json",
                     "subscriptions.json", "plugins.json", "plugin_data/"):
            self.assertIn(name, text)

    def test_clean_requires_confirm(self) -> None:
        for i in range(5):
            (self.dir / f"mpbackup_2026010{i}_000000_{i:06d}.zip").write_bytes(b"x")
        self.assertFalse(BACKUP.clean_snapshots(self.dir, 2, "")["ok"])
        self.assertEqual(len(BACKUP.snapshot_files(self.dir)), 5)
        ok = BACKUP.clean_snapshots(self.dir, 2, BACKUP.CLEAN_CONFIRM)
        self.assertTrue(ok["ok"])
        self.assertEqual(len(BACKUP.snapshot_files(self.dir)), 2)

    def test_mask_and_html_escape(self) -> None:
        dirty = {"password": "p", "nested": [{"token": "t"}],
                 "note": "Cookie: sid=123; uid=9"}
        out = BACKUP.mask(dirty)
        self.assertEqual(out["password"], "***")
        self.assertEqual(out["nested"][0]["token"], "***")
        self.assertNotIn("sid=123", out["note"])

        state = {
            "offline": True, "enabled": True, "generated_at": "2026-01-01 00:00:00",
            "backup_dir": "/tmp", "cron_label": "每天凌晨 4 点", "keep": 7,
            "sections": [], "sources": {}, "diff": None, "diff_a": "", "diff_b": "",
            "guide": None, "total_size_text": "1.0 KB", "contains_secrets": True,
            "snapshots": [{"name": "<img src=x onerror=alert(1)>.zip",
                           "size_text": "1.0 KB", "mtime": "2026-01-01 00:00:00"}],
            "preview": {
                "ok": True, "file": "<script>bad</script>.zip", "schema": 2,
                "message": "<b>预览</b>", "contains_secrets": True,
                "sections": [{"label": "<img src=y>", "total": 1, "current": "无法核对",
                              "will_add": 1, "will_skip": 0, "will_overwrite": 0,
                              "status": "无法核对"}],
            },
            "restore": {
                "mode": "<i>试运行</i>", "dry_run": True, "strategy": "skip",
                "message": "<script>alert(1)</script>",
                "totals": {"planned": 1},
                "log": ["[用户] 试运行 · <img src=z> · 将新增"],
            },
        }
        html = RENDERER.render(state)
        for bad in ("<img src=x", "<script>bad", "<b>预览</b>", "<img src=y>",
                    "<i>试运行</i>", "<script>alert(1)", "<img src=z>"):
            self.assertNotIn(bad, html)
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;script", html)
        self.assertIn("mpbk-grid", html)
        self.assertIn("导入预览", html)
        self.assertIn("导入结果", html)


class TestApiFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": str(self.dir), "keep": 3, "notify": False})
        self.plugin.get_data_path = lambda *a, **k: str(self.dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_backup_compare_guide_clean_refresh(self) -> None:
        first = self.plugin.api_backup()
        self.assertTrue(first.success)
        self.assertIn("file", first.data)
        self.assertEqual(len(BACKUP.snapshot_files(self.dir)), 1)
        second = self.plugin.api_backup()
        self.assertTrue(second.success)
        self.assertEqual(len(BACKUP.snapshot_files(self.dir)), 2)

        snap = self.plugin.api_snapshot()
        self.assertTrue(snap.success)
        self.assertEqual(len(snap.data["snapshots"]), 2)
        self.assertIn("status", snap.data)

        compare = self.plugin.api_compare()
        self.assertTrue(compare.success)
        self.assertIn("diff", compare.data)

        guide = self.plugin.api_restore_guide()
        self.assertTrue(guide.success)
        self.assertIn("不写回数据库", guide.data["text"])

        no_confirm = self.plugin.api_clean()
        self.assertFalse(no_confirm.success)
        cleaned = self.plugin.api_clean({"confirm": PLUGIN_CLS.CLEAN_CONFIRM})
        self.assertTrue(cleaned.success)

        refresh = self.plugin.api_refresh()
        self.assertTrue(refresh.success)
        self.assertIn("html", refresh.data)

    def test_import_preview_dry_run_and_execute(self) -> None:
        name = self.plugin.api_backup().data["file"]

        preview = self.plugin.api_import_preview({"name": name})
        self.assertTrue(preview.success)
        self.assertIn("preview", preview.data)
        self.assertEqual(preview.data["preview"]["schema"], 2)
        self.assertTrue(preview.data["preview"]["sections"])

        dry = self.plugin.api_import({"name": name, "dry_run": True})
        self.assertTrue(dry.success)
        self.assertTrue(dry.data["restore"]["dry_run"])
        self.assertEqual(dry.data["restore"]["mode"], "试运行")

        run = self.plugin.api_import({
            "name": name, "dry_run": False, "confirm": RESTORE.CONFIRM,
            "strategy": "skip", "sections": list(RESTORE.IMPORT_SECTIONS),
        })
        self.assertTrue(run.success)
        self.assertFalse(run.data["restore"]["dry_run"])
        statuses = {sec["status"] for sec in run.data["restore"]["sections"]}
        self.assertEqual(statuses, {"unavailable"})

        self.assertIn("html", preview.data)
        self.assertIn("html", run.data)

    def test_import_bad_zip_is_tolerated(self) -> None:
        (self.dir / "mpbackup_bad.zip").write_bytes(b"not a zip")
        preview = self.plugin.api_import_preview({"name": "mpbackup_bad.zip"})
        self.assertFalse(preview.success)
        run = self.plugin.api_import({"name": "mpbackup_bad.zip",
                                      "dry_run": False, "confirm": RESTORE.CONFIRM})
        self.assertFalse(run.success)

    def test_import_unknown_name_rejected(self) -> None:
        preview = self.plugin.api_import_preview({"name": "../evil.zip"})
        self.assertFalse(preview.success)

    def test_import_guide(self) -> None:
        result = self.plugin.api_import_guide()
        self.assertTrue(result.success)
        self.assertIn("试运行", result.data["text"])
        self.assertIn("html", result.data)


class TestRestore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.path = make_data_snapshot(self.dir, "mpbackup_20260101_000000_000001.zip")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_preview_is_readonly_and_structured(self) -> None:
        result = RESTORE.preview(self.path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema"], 2)
        self.assertEqual(len(result["sections"]), len(RESTORE.IMPORT_SECTIONS))
        for sec in result["sections"]:
            for field in ("total", "current", "will_add", "will_skip",
                          "will_overwrite", "status"):
                self.assertIn(field, sec)
            self.assertEqual(sec["status"], "无法核对")
        self.assertIn("全程只读", result["message"])

    def test_preview_totals_match_content(self) -> None:
        result = RESTORE.preview(self.path)
        users = next(s for s in result["sections"] if s["key"] == "users")
        self.assertEqual(users["total"], 2)
        self.assertEqual(users["will_add"], 2)

    def test_dry_run_never_writes(self) -> None:
        with mock.patch.object(RESTORE, "_write_section",
                               side_effect=AssertionError("试运行不该写库")):
            result = RESTORE.restore(self.path, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["mode"], "试运行")
        self.assertFalse(result["confirm_ok"])
        self.assertTrue(result["log"], "试运行要列逐条清单")
        for sec in result["sections"]:
            self.assertEqual(sec["status"], "dry_run")
            self.assertEqual(sec["added"], 0)

    def test_execute_requires_confirm(self) -> None:
        without = RESTORE.restore(self.path, dry_run=False, confirm="")
        self.assertTrue(without["dry_run"])
        self.assertFalse(without["confirm_ok"])
        wrong = RESTORE.restore(self.path, dry_run=False, confirm="随便")
        self.assertTrue(wrong["dry_run"])
        with_word = RESTORE.restore(self.path, dry_run=False, confirm=RESTORE.CONFIRM)
        self.assertFalse(with_word["dry_run"])
        self.assertTrue(with_word["confirm_ok"])

    def test_execute_in_stub_goes_unavailable_without_crash(self) -> None:
        result = RESTORE.restore(self.path, dry_run=False, confirm=RESTORE.CONFIRM)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "执行")
        statuses = {sec["status"] for sec in result["sections"]}
        self.assertEqual(statuses, {"unavailable"})
        self.assertTrue(result["log"])

    def test_section_failure_does_not_stop_others(self) -> None:
        with mock.patch.object(RESTORE, "_host_ready", return_value=True), \
             mock.patch.object(RESTORE, "_write_users",
                               side_effect=RuntimeError("boom")):
            result = RESTORE.restore(self.path, sections=["users", "sites"],
                                     dry_run=False, confirm=RESTORE.CONFIRM)
        self.assertEqual(len(result["sections"]), 2)
        self.assertEqual(result["sections"][0]["status"], "error")
        self.assertNotEqual(result["sections"][1]["status"], "error")

    def test_preview_bad_zip_and_missing_items(self) -> None:
        bad = self.dir / "mpbackup_bad.zip"
        bad.write_bytes(b"nope")
        self.assertFalse(RESTORE.preview(bad)["ok"])

        partial = self.dir / "mpbackup_partial.zip"
        with zipfile.ZipFile(partial, "w") as zf:
            zf.writestr("users.json", json.dumps([{"name": "a"}]))
        got = RESTORE.preview(partial)
        self.assertTrue(got["ok"])
        users = next(s for s in got["sections"] if s["key"] == "users")
        sites = next(s for s in got["sections"] if s["key"] == "sites")
        self.assertEqual(users["total"], 1)
        self.assertEqual(sites["total"], 0)

    def test_restore_bad_zip_is_reported(self) -> None:
        bad = self.dir / "mpbackup_bad.zip"
        bad.write_bytes(b"nope")
        result = RESTORE.restore(bad, dry_run=False, confirm=RESTORE.CONFIRM)
        self.assertFalse(result["ok"])
        self.assertIn("快照打不开", result["message"])

    def test_import_guide_text(self) -> None:
        text = RESTORE.import_guide("overwrite")
        self.assertIn("覆盖已存在", text)
        self.assertIn(RESTORE.CONFIRM, text)


class TestSectionsThroughPlugin(unittest.TestCase):
    """init_plugin 里选中的分类真的只写选中项。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": str(self.dir), "notify": False,
                                 "sections": ["users", "sites"]})
        self.plugin.get_data_path = lambda *a, **k: str(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_selected_are_written(self):
        result = self.plugin.api_backup()
        self.assertTrue(result.success)
        manifest = result.data["manifest"]
        self.assertEqual(manifest["sections"], ["users", "sites"])
        for name in BACKUP.ALL_SOURCES:
            if name in ("users", "sites"):
                self.assertNotEqual(manifest["sources"][name]["status"], "skipped", name)
            else:
                self.assertEqual(manifest["sources"][name]["status"], "skipped", name)
        with zipfile.ZipFile(self.dir / result.data["file"]) as zf:
            names = set(zf.namelist())
        self.assertIn("users.json", names)
        self.assertIn("sites.json", names)
        self.assertNotIn("subscriptions.json", names)
        self.assertNotIn("settings.json", names)


class TestCustomPath(unittest.TestCase):
    """备份目录能自己填，缺的目录自动建，建不起来给可读提示。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_absolute_path_is_created_automatically(self):
        target = self.base / "nas" / "mount" / "backups"
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"backup_dir": str(target), "notify": False})
        plugin.get_data_path = lambda *a, **k: str(self.base)
        self.assertFalse(target.exists())
        result = plugin.api_backup()
        self.assertTrue(result.success)
        self.assertTrue(target.is_dir())
        self.assertTrue((target / result.data["file"]).is_file())
        self.assertEqual(plugin._backup_path(), target)

    def test_relative_path_is_anchored_to_data_dir(self):
        plugin = PLUGIN_CLS()
        plugin.get_data_path = lambda *a, **k: str(self.base)
        plugin.init_plugin({"backup_dir": "my_backups", "notify": False})
        result = plugin.api_backup()
        self.assertTrue(result.success)
        target = self.base / "my_backups"
        self.assertTrue(target.is_dir())
        self.assertTrue((target / result.data["file"]).is_file())

    def test_mkdir_failure_gives_readable_message(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"backup_dir": str(self.base / "nope"), "notify": False})
        plugin.get_data_path = lambda *a, **k: str(self.base)
        with mock.patch.object(pathlib.Path, "mkdir",
                               side_effect=OSError("Permission denied")):
            result = plugin.api_backup()
        self.assertFalse(result.success)
        self.assertIn("备份目录建不起来", result.message)
        self.assertIsInstance(result.message, str)


class TestBackupCount(unittest.TestCase):
    """成功才计数，失败不计数，页面看得到次数和最近时间。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": str(self.dir), "keep": 30, "notify": False})
        self.plugin.get_data_path = lambda *a, **k: str(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_increments_and_persists(self):
        self.assertEqual(self.plugin.get_data(BACKUP.BACKUP_COUNT_KEY, 0), 0)
        for i in range(3):
            self.assertTrue(self.plugin.api_backup().success)
            self.assertEqual(self.plugin.get_data(BACKUP.BACKUP_COUNT_KEY), i + 1)
        self.assertEqual(self.plugin._backup_count, 3)
        last = self.plugin.get_data(BACKUP.LAST_BACKUP_KEY, "")
        self.assertTrue(last)
        self.assertEqual(self.plugin._last_backup_at, last)

    def test_failure_does_not_increment(self):
        self.assertTrue(self.plugin.api_backup().success)
        before = self.plugin.get_data(BACKUP.BACKUP_COUNT_KEY)
        with mock.patch.object(BACKUP, "write_snapshot",
                               side_effect=RuntimeError("磁盘炸了")):
            result = self.plugin.api_backup()
        self.assertFalse(result.success)
        self.assertEqual(self.plugin.get_data(BACKUP.BACKUP_COUNT_KEY), before)
        self.assertEqual(self.plugin._backup_count, before)

    def test_count_survives_reinit(self):
        self.plugin.api_backup()
        self.plugin.api_backup()
        again = PLUGIN_CLS()
        again._data = dict(self.plugin._data)
        again.init_plugin({"backup_dir": str(self.dir), "notify": False})
        self.assertEqual(again._backup_count, 2)
        self.assertTrue(again._last_backup_at)

    def test_kpi_shows_count_and_last_time(self):
        self.plugin.api_backup()
        html = RENDERER.render(self.plugin._cache)
        self.assertIn("已备份", html)
        self.assertIn("1 次", html)
        self.assertIn(self.plugin._last_backup_at[5:10], html)
        self.assertIn("累计已备份 1 次", html)
        self.assertIn("最近一次备份", html)


class TestDeleteFlow(unittest.TestCase):
    """两段式删除：第一步出计划，第二步全校验过才真删。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": str(self.dir), "keep": 30, "notify": False})
        self.plugin.get_data_path = lambda *a, **k: str(self.dir)
        self.name = self.plugin.api_backup().data["file"]

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self):
        return self.plugin.api_delete({"name": self.name})

    def test_two_step_delete_works(self):
        first = self._plan()
        self.assertTrue(first.success)
        plan_id = first.data["plan_id"]
        self.assertEqual(first.data["name"], self.name)
        self.assertEqual(first.data["confirm"], "删除")
        self.assertIsNotNone(self.plugin.get_data(BACKUP.DELETE_PLAN_KEY))
        self.assertTrue((self.dir / self.name).is_file())
        second = self.plugin.api_delete({"plan_id": plan_id, "name": self.name,
                                         "confirm": "删除"})
        self.assertTrue(second.success, second.message)
        self.assertFalse((self.dir / self.name).is_file())
        self.assertIsNone(self.plugin.get_data(BACKUP.DELETE_PLAN_KEY))
        self.assertIsNone(self.plugin._delete_plan)

    def test_no_plan_id_never_deletes(self):
        self._plan()
        got = self.plugin.api_delete({"name": self.name, "confirm": "删除"})
        self.assertFalse(got.success)
        self.assertTrue((self.dir / self.name).is_file())

    def test_wrong_confirm_never_deletes(self):
        plan_id = self._plan().data["plan_id"]
        for confirm in ("", "随便", "DELETE", " 删除 "):
            got = self.plugin.api_delete({"plan_id": plan_id, "name": self.name,
                                          "confirm": confirm})
            self.assertFalse(got.success, confirm)
            self.assertTrue((self.dir / self.name).is_file())

    def test_wrong_plan_id_never_deletes(self):
        self._plan()
        got = self.plugin.api_delete({"plan_id": "乱编的", "name": self.name,
                                      "confirm": "删除"})
        self.assertFalse(got.success)
        self.assertTrue((self.dir / self.name).is_file())

    def test_name_mismatch_never_deletes(self):
        plan_id = self._plan().data["plan_id"]
        got = self.plugin.api_delete({"plan_id": plan_id, "name": "别的.zip",
                                      "confirm": "删除"})
        self.assertFalse(got.success)
        self.assertTrue((self.dir / self.name).is_file())

    def test_expired_plan_never_deletes(self):
        first = self._plan()
        plan = self.plugin.get_data(BACKUP.DELETE_PLAN_KEY)
        plan["expires_at"] = 0
        self.plugin.save_data(BACKUP.DELETE_PLAN_KEY, plan)
        got = self.plugin.api_delete({"plan_id": first.data["plan_id"],
                                      "name": self.name, "confirm": "删除"})
        self.assertFalse(got.success)
        self.assertTrue((self.dir / self.name).is_file())
        self.assertIsNone(self.plugin.get_data(BACKUP.DELETE_PLAN_KEY))

    def test_out_of_path_name_is_rejected(self):
        for bad in ("../evil.zip", "/etc/mpbackup_x.zip", "other.zip",
                    "mpbackup_notes.txt", "sub/mpbackup_x.zip"):
            got = self.plugin.api_delete({"name": bad})
            self.assertFalse(got.success, bad)
        self.assertTrue((self.dir / self.name).is_file())

    def test_only_own_zip_is_deleted(self):
        other = self.dir / "other.zip"
        other.write_bytes(b"keep")
        self.assertFalse(self.plugin.api_delete({"name": "other.zip"}).success)
        self.assertTrue(other.is_file())
        plan_id = self._plan().data["plan_id"]
        self.assertTrue(self.plugin.api_delete({
            "plan_id": plan_id, "name": self.name, "confirm": "删除"}).success)
        self.assertTrue(other.is_file())

    def test_delete_failure_is_readable(self):
        plan_id = self._plan().data["plan_id"]
        with mock.patch.object(pathlib.Path, "unlink",
                               side_effect=OSError("readonly")):
            got = self.plugin.api_delete({"plan_id": plan_id, "name": self.name,
                                          "confirm": "删除"})
        self.assertFalse(got.success)
        self.assertIn("删不掉", got.message)
        self.assertTrue((self.dir / self.name).is_file())

    def test_confirm_button_appears_after_first_step(self):
        first = self._plan()
        found = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("component") == "VBtn" and "确认删除" in str(node.get("text") or ""):
                    found.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.plugin.get_page())
        self.assertEqual(len(found), 1)
        click = found[0]["events"]["click"]
        self.assertEqual(click["api"], "plugin/MPBackup/delete")
        self.assertEqual(click["method"], "post")
        self.assertEqual(click["params"], {"plan_id": first.data["plan_id"],
                                           "name": self.name, "confirm": "删除"})

    def test_delete_button_on_each_snapshot_row(self):
        self.plugin.api_backup()
        clicks = cc.collect_click_events(self.plugin.get_page())
        deletes = [c for c in clicks if c["api"].endswith("/delete")]
        self.assertGreaterEqual(len(deletes), 2)


class TestEndpointGuards(unittest.TestCase):
    """任何 endpoint 炸了都只能拿到中文失败响应，绝不冒泡到宿主。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": self.tmp.name, "notify": False})
        self.plugin.get_data_path = lambda *a, **k: self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_endpoints_swallow_internal_errors(self):
        names = ("api_snapshot", "api_backup", "api_compare", "api_restore_guide",
                 "api_clean", "api_refresh", "api_import_preview", "api_import",
                 "api_import_guide", "api_delete")
        for name in names:
            with mock.patch.object(self.plugin, "_collect_state",
                                   side_effect=RuntimeError("boom")):
                result = getattr(self.plugin, name)()
            self.assertIsInstance(result, MODULE.Response, name)
            self.assertFalse(result.success, name)
            self.assertIsInstance(result.message, str, name)
            self.assertTrue(result.message, name)

    def test_odd_payloads_never_raise(self):
        names = ("api_backup", "api_compare", "api_restore_guide", "api_clean",
                 "api_refresh", "api_import_preview", "api_import",
                 "api_import_guide", "api_delete")
        odd = (None, "不是字典", 123, [], {"name": object()},
               {"confirm": ["x"]}, {"sections": {"a": 1}})
        for name in names:
            for payload in odd:
                result = getattr(self.plugin, name)(payload)
                self.assertIsInstance(result, MODULE.Response, name)
                self.assertIsInstance(result.message, str, name)


class TestRenderEscape(unittest.TestCase):
    """新加的计数字段和删除计划也要过 html.escape。"""

    def test_new_fields_are_escaped(self):
        state = {
            "offline": True, "enabled": True, "generated_at": "2026-01-01 00:00:00",
            "backup_dir": "<img src=x>", "cron_label": "每天凌晨 4 点", "keep": 7,
            "sections": [], "sources": {}, "diff": None, "diff_a": "", "diff_b": "",
            "guide": None, "total_size_text": "1.0 KB", "contains_secrets": False,
            "snapshots": [], "backup_count": 3,
            "last_backup_at": "<script>alert(1)</script>",
            "delete_plan": {"plan_id": "p", "name": "<b>坏名字</b>.zip",
                            "expires_at": 9999999999},
        }
        html = RENDERER.render(state)
        for bad in ("<img src=x>", "<script>alert(1)", "<b>坏名字</b>"):
            self.assertNotIn(bad, html)
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;script", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("已备份", html)
        self.assertIn("3 次", html)
        self.assertIn("累计已备份 3 次", html)
        self.assertIn("待确认删除", html)


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
