#!/usr/bin/env python3
"""MP 备份迁移：纯函数测试（脱敏 / 归一 / 漂移对比），不依赖宿主。"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "mpbackup_backup", ROOT / "plugins.v3" / "mpbackup" / "backup.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


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


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n合计 {total} 个用例，通过 {total - failed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
