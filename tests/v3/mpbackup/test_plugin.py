#!/usr/bin/env python3
"""MP 备份迁移：纯函数测试（脱敏 / 归一 / 漂移对比 / 导入清洗），不依赖宿主。"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "mpbackup_backup", ROOT / "plugins.v3" / "mpbackup" / "backup.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_rspec = importlib.util.spec_from_file_location(
    "mpbackup_restore", ROOT / "plugins.v3" / "mpbackup" / "restore.py")
rmod = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(rmod)


class _ModelDump:
    def model_dump(self):
        return {"id": 7, "name": "demo", "token": "x"}


class _PydanticV1:
    def dict(self):
        return {"id": 8, "title": "old"}


class _Orm:
    def __init__(self):
        self.id = 9
        self.name = "site"
        self._sa_instance_state = object()


class TestMask(unittest.TestCase):
    def test_secret_keys_are_masked(self):
        out = mod.mask({
            "cookie": "a", "token": "b", "password": "c", "api_key": "d",
            "secret": "e", "passkey": "f", "authorization": "g", "ua": "h",
            "normal": "ok",
        })
        for key in ("cookie", "token", "password", "api_key", "secret",
                    "passkey", "authorization", "ua"):
            self.assertEqual(out[key], "***", key)
        self.assertEqual(out["normal"], "ok")

    def test_nested_dict_and_list(self):
        out = mod.mask({"a": [{"auth": "z"}, {"name": "n"}], "b": {"API-Key": "k"}})
        self.assertEqual(out["a"][0]["auth"], "***")
        self.assertEqual(out["a"][1]["name"], "n")
        self.assertEqual(out["b"]["API-Key"], "***")

    def test_inline_cookie_string(self):
        out = mod.mask("some Cookie: sid=abc123; uid=9 and Authorization: Bearer tok")
        self.assertNotIn("sid=abc123", out)
        self.assertNotIn("Bearer tok", out)
        self.assertIn("***", out)

    def test_object_is_normalized_then_masked(self):
        out = mod.mask(_ModelDump())
        self.assertEqual(out["name"], "demo")
        self.assertEqual(out["token"], "***")

    def test_primitives_untouched(self):
        self.assertEqual(mod.mask(5), 5)
        self.assertEqual(mod.mask("hello"), "hello")
        self.assertIsNone(mod.mask(None))


class TestAsDict(unittest.TestCase):
    def test_dict_passthrough(self):
        self.assertEqual(mod.as_dict({"a": 1}), {"a": 1})

    def test_model_dump(self):
        self.assertEqual(mod.as_dict(_ModelDump()), {"id": 7, "name": "demo", "token": "x"})

    def test_pydantic_v1_dict(self):
        self.assertEqual(mod.as_dict(_PydanticV1()), {"id": 8, "title": "old"})

    def test_orm_dict_filters_internal(self):
        self.assertEqual(mod.as_dict(_Orm()), {"id": 9, "name": "site"})

    def test_primitive_returns_empty(self):
        self.assertEqual(mod.as_dict(3), {})


class TestDiff(unittest.TestCase):
    def test_added_removed_changed(self):
        old = [{"id": 1, "name": "A", "state": "R"}, {"id": 2, "name": "B"}]
        new = [{"id": 1, "name": "A", "state": "S"}, {"id": 3, "name": "C"}]
        result = mod.diff_section(old, new, ("id", "name"))
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(result["added"][0]["id"], 3)
        self.assertEqual(len(result["removed"]), 1)
        self.assertEqual(result["removed"][0]["id"], 2)
        self.assertEqual(len(result["changed"]), 1)
        change = result["changed"][0]["changes"][0]
        self.assertEqual((change["field"], change["old"], change["new"]), ("state", "R", "S"))

    def test_build_diff_totals(self):
        old = {"subscriptions": [{"id": 1}], "sites": [], "services": [], "plugins": []}
        new = {"subscriptions": [{"id": 1}, {"id": 2}], "sites": [], "services": [], "plugins": []}
        diff = mod.build_diff(old, new)
        self.assertTrue(diff["has_drift"])
        self.assertEqual(diff["totals"]["added"], 1)
        self.assertEqual(diff["sections"]["subscriptions"]["label"], "订阅")
        self.assertIn("新增 1", diff["summary"])

    def test_no_drift(self):
        same = {"subscriptions": [{"id": 1}], "sites": [], "services": [], "plugins": []}
        self.assertFalse(mod.build_diff(same, same)["has_drift"])


class TestPayloadClean(unittest.TestCase):
    """导入前的白名单过滤与 payload 清洗，全是纯函数。"""

    def test_drop_host_managed_keys(self):
        row = {"id": 1, "name": "u", "identity_key": "x", "activity_key": "y",
               "date": "2026", "notification_identity": "z"}
        self.assertEqual(rmod.filter_fields(row, ("name",)), {"name": "u"})

    def test_unknown_columns_removed(self):
        out = rmod.clean_payload("subscriptions",
                                 {"name": "A", "season": 1, "bogus": 9, "id": 3})
        self.assertEqual(out, {"name": "A", "season": 1})

    def test_bool_not_written_into_int_column(self):
        out = rmod.clean_payload("subscriptions",
                                 {"name": "A", "season": True, "episode": False})
        self.assertEqual(out["season"], 1)
        self.assertEqual(out["episode"], 0)
        self.assertIsInstance(out["season"], int)
        self.assertNotIsInstance(out["season"], bool)

    def test_users_keep_password_hash(self):
        out = rmod.clean_payload("users", {
            "name": "admin", "hashed_password": "$2b$xxx",
            "id": 5, "notification_identity": "n",
        })
        self.assertEqual(out["hashed_password"], "$2b$xxx")
        self.assertNotIn("id", out)
        self.assertNotIn("notification_identity", out)

    def test_sites_keep_credentials(self):
        out = rmod.clean_payload("sites", {
            "name": "s", "domain": "d.com", "cookie": "c", "ua": "u",
            "apikey": "k", "token": "t", "weird": 1,
        })
        self.assertEqual(out["cookie"], "c")
        self.assertEqual(out["token"], "t")
        self.assertNotIn("weird", out)

    def test_subscription_identity_parts(self):
        row = {"media_source": "tmdb", "media_id": 123, "season": 2,
               "episode_group": "g", "music_type": "", "type": "电视剧"}
        self.assertEqual(rmod.subscription_identity(row), "tmdb|123|2|g||电视剧")

    def test_filter_fields_extra_drop(self):
        out = rmod.filter_fields({"a": 1, "b": 2, "id": 3}, drop=("b",))
        self.assertEqual(out, {"a": 1})


class TestReadSnapshot(unittest.TestCase):
    def test_missing_sections_are_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "mpbackup_x.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("users.json", json.dumps([{"name": "a"}]))
                zf.writestr("manifest.json", json.dumps({"schema": 2}))
            got = rmod.read_snapshot(path)
            self.assertTrue(got["ok"])
            self.assertEqual(len(got["sections"]["users"]), 1)
            self.assertEqual(got["sections"]["sites"], [])
            self.assertEqual(got["manifest"].get("schema"), 2)

    def test_bad_zip_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "mpbackup_bad.zip"
            path.write_bytes(b"not a zip at all")
            got = rmod.read_snapshot(path)
            self.assertFalse(got["ok"])
            self.assertIn("快照打不开", got["message"])

    def test_broken_json_is_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "mpbackup_broken.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("users.json", "{not json")
            got = rmod.read_snapshot(path)
            self.assertTrue(got["ok"])
            self.assertEqual(got["sections"]["users"], [])

    def test_site_secrets_merged_back(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "mpbackup_sec.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("sites.json", json.dumps([{"name": "s", "domain": "d.com"}]))
                zf.writestr("site_secrets.json", json.dumps([
                    {"domain": "d.com", "cookie": "C", "ua": "U",
                     "apikey": "K", "token": "T"}]))
            got = rmod.read_snapshot(path)
            site = got["sections"]["sites"][0]
            self.assertEqual(site["cookie"], "C")
            self.assertEqual(site["token"], "T")


class TestSections(unittest.TestCase):
    """备份项自选：十二类全覆盖，没勾的不写进 zip 且在清单里标 skipped。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.data = self.dir / "data"
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, sections):
        with mock.patch.object(mod, "host_ready", return_value=False):
            return mod.write_snapshot(self.dir, str(self.data), list(sections), 7)

    def test_options_cover_every_manifest_section(self):
        keys = [key for key, _ in mod.SECTION_OPTIONS]
        self.assertEqual(set(keys), set(mod.ALL_SOURCES))
        self.assertEqual(len(keys), len(mod.ALL_SOURCES))
        for key, label in mod.SECTION_OPTIONS:
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in label), key)
        self.assertEqual(tuple(mod.DEFAULT_SECTIONS), tuple(keys))

    def test_clean_sections_defaults_to_full(self):
        self.assertEqual(mod.clean_sections(None), list(mod.SECTION_KEYS))
        self.assertEqual(mod.clean_sections("坏数据"), list(mod.SECTION_KEYS))
        self.assertEqual(mod.clean_sections([]), [])

    def test_clean_sections_keeps_known_order(self):
        self.assertEqual(mod.clean_sections(["sites", "users", "不存在"]),
                         [k for k in mod.SECTION_KEYS if k in ("sites", "users")])

    def test_unselected_are_skipped_and_not_written(self):
        picked = ["users", "sites"]
        result = self._write(picked)
        manifest = result["manifest"]
        self.assertEqual(manifest["sections"], picked)
        for name in mod.ALL_SOURCES:
            info = manifest["sources"][name]
            if name in picked:
                self.assertNotEqual(info["status"], "skipped", name)
            else:
                self.assertEqual(info["status"], "skipped", name)
                self.assertTrue(info["reason"])
                self.assertEqual(info["count"], 0)
        with zipfile.ZipFile(self.dir / result["file"]) as zf:
            names = set(zf.namelist())
        self.assertIn("users.json", names)
        self.assertIn("sites.json", names)
        self.assertIn("manifest.json", names)
        for name in mod.ALL_SOURCES:
            if name in picked or name == "plugin_data":
                continue
            self.assertNotIn(name + ".json", names, name)
        self.assertNotIn("plugin_data/", names)

    def test_all_selected_writes_every_json(self):
        result = self._write(mod.SECTION_KEYS)
        with zipfile.ZipFile(self.dir / result["file"]) as zf:
            names = set(zf.namelist())
        for name in mod.ALL_SOURCES:
            if name == "plugin_data":
                self.assertIn("plugin_data/", names)
            else:
                self.assertIn(name + ".json", names)
        for name in mod.ALL_SOURCES:
            self.assertNotEqual(result["manifest"]["sources"][name]["status"], "skipped")

    def test_none_selected_only_manifest(self):
        result = self._write([])
        with zipfile.ZipFile(self.dir / result["file"]) as zf:
            names = set(zf.namelist())
        self.assertEqual(names, {"manifest.json"})
        for name in mod.ALL_SOURCES:
            self.assertEqual(result["manifest"]["sources"][name]["status"], "skipped")


class TestDeleteTarget(unittest.TestCase):
    """删除只认本插件的 mpbackup_*.zip，且必须落在备份目录里。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.dir = self.base / "bak"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _own(self, name="mpbackup_20260101_000000_000001.zip"):
        path = self.dir / name
        path.write_bytes(b"x")
        return path

    def test_rejects_other_files(self):
        for name in ("", None, "other.zip", "mpbackup_notes.txt",
                     "mpbackup_x.rar", "notes.json"):
            target, why = mod.resolve_delete_target(self.dir, name)
            self.assertIsNone(target, name)
            self.assertTrue(why)

    def test_rejects_path_traversal(self):
        for name in ("..", ".", "../evil.zip", "sub/mpbackup_x.zip",
                     "/etc/mpbackup_x.zip", "mpbackup_x.zip/../y"):
            target, why = mod.resolve_delete_target(self.dir, name)
            self.assertIsNone(target, name)
            self.assertTrue(why)

    def test_deletes_own_snapshot(self):
        path = self._own()
        target, why = mod.resolve_delete_target(self.dir, path.name)
        self.assertEqual(target, path)
        self.assertEqual(why, "")
        got = mod.delete_snapshot(self.dir, path.name)
        self.assertTrue(got["ok"])
        self.assertFalse(path.exists())
        self.assertFalse(mod.delete_snapshot(self.dir, path.name)["ok"])

    def test_only_own_zip_is_touched(self):
        own = self._own()
        other = self.dir / "other.zip"
        other.write_bytes(b"keep")
        notes = self.dir / "mpbackup_notes.txt"
        notes.write_bytes(b"keep")
        self.assertFalse(mod.delete_snapshot(self.dir, "other.zip")["ok"])
        self.assertTrue(other.is_file())
        self.assertTrue(mod.delete_snapshot(self.dir, own.name)["ok"])
        self.assertTrue(notes.is_file())
        self.assertTrue(other.is_file())

    def test_symlink_outside_is_rejected(self):
        outside = self.base / "mpbackup_outside.zip"
        outside.write_bytes(b"secret")
        link = self.dir / "mpbackup_link.zip"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("这个环境建不了符号链接")
        target, why = mod.resolve_delete_target(self.dir, link.name)
        self.assertIsNone(target)
        self.assertTrue(why)
        self.assertTrue(outside.is_file())


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n合计 {total} 个用例，通过 {total - failed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
