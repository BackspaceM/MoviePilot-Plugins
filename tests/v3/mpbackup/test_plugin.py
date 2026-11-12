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


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n合计 {total} 个用例，通过 {total - failed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
