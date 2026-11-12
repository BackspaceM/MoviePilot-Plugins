#!/usr/bin/env python3
"""MP 备份迁移插件的离线回归测试（复用全仓契约桩，无需 MoviePilot 宿主）。"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "plugins.v3" / "mpbackup"
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
BACKUP = MODULE.backup
RENDERER = MODULE.renderer


class TestContract(unittest.TestCase):
    """防止按钮点击 404 和接口注册失败。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": self.tmp.name})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_plugin_id_and_metadata(self) -> None:
        self.assertEqual(PLUGIN_ID, "MPBackup")
        expected = {
            "plugin_name": "MP 备份迁移", "plugin_version": "1.0.0",
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
        for action in ("backup", "compare", "restore-guide", "clean", "refresh"):
            self.assertIn(action, actions)
        for api in self.plugin.get_api():
            self.assertEqual(api.get("auth"), "bear")

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

    def test_api_returns_three_part_envelope(self) -> None:
        for name in ("api_snapshot", "api_backup", "api_compare", "api_restore_guide",
                     "api_clean", "api_refresh"):
            result = getattr(self.plugin, name)()
            self.assertTrue(hasattr(result, "success"))
            self.assertIsInstance(result.message, str)

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
        self.assertLessEqual(len(controls), 6, f"设置项应保持精简，实际为 {controls}")
        self.assertEqual(set(defaults),
                         {"enabled", "backup_dir", "keep", "schedule", "sections", "notify"})


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
        # 全选，好让每个宿主来源都真正尝试一次，离线时统一记成 unavailable
        result = BACKUP.write_snapshot(self.dir, str(self.data),
                                       list(BACKUP.TOGGLEABLE), 3)
        path = self.dir / result["file"]
        self.assertTrue(path.is_file())
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        for name in ("manifest.json", "settings.json", "subscriptions.json",
                     "download_history.json", "transfer_history.json", "sites.json",
                     "services.json", "plugins.json"):
            self.assertIn(name, names)
        self.assertIn("plugin_data/", names)

        sources = result["manifest"]["sources"]
        for name in BACKUP.ALL_SOURCES:
            self.assertIn(name, sources)
        for name in BACKUP.HOST_SOURCES:
            self.assertEqual(sources[name]["status"], "unavailable", name)
            self.assertTrue(sources[name]["reason"])

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
            "guide": None, "total_size_text": "1.0 KB",
            "snapshots": [{"name": "<img src=x onerror=alert(1)>.zip",
                           "size_text": "1.0 KB", "mtime": "2026-01-01 00:00:00"}],
        }
        html = RENDERER.render(state)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)


class TestApiFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"backup_dir": str(self.dir), "keep": 3, "notify": False})

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
