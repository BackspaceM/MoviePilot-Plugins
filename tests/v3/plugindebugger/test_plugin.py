#!/usr/bin/env python3
"""插件调试的纯函数测试，不需要 MoviePilot 宿主。"""

from __future__ import annotations

import base64
import io
import pathlib
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins.v3"))

from plugindebugger import installer, renderer  # noqa: E402

INIT = '''from app.sdk.plugin import _PluginBase

class DemoPlugin(_PluginBase):
    plugin_name = "示例插件"
    plugin_version = "2.3.4"
'''


def zip_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


class ZipTests(unittest.TestCase):
    def test_safe_name_strips_path_and_odd_chars(self):
        self.assertEqual(installer.safe_name("../../etc/a b.zip"), "a_b.zip")
        self.assertEqual(installer.safe_name("C:\\\\tmp\\\\demo.zip"), "demo.zip")
        self.assertEqual(installer.safe_name(""), "")

    def test_decode_upload_accepts_data_url_and_rejects_junk(self):
        raw = zip_bytes({"Demo/__init__.py": INIT})
        b64 = base64.b64encode(raw).decode()
        ok, error = installer.decode_upload("data:application/zip;base64," + b64, 20 * 1048576)
        self.assertEqual(ok, raw)
        self.assertEqual(error, "")
        self.assertEqual(installer.decode_upload(b64, 20 * 1048576)[0], raw)
        self.assertIn("base64", installer.decode_upload("not base64 !!", 20 * 1048576)[1])
        self.assertIn("上限", installer.decode_upload(b64, 10)[1])
        self.assertIn("ZIP", installer.decode_upload(base64.b64encode(b"hello").decode(), 1000)[1])

    def test_plugin_meta_reads_class_and_version(self):
        meta = installer.plugin_meta(INIT, "Demo")
        self.assertEqual(meta["id"], "DemoPlugin")
        self.assertEqual(meta["name"], "示例插件")
        self.assertEqual(meta["version"], "2.3.4")
        self.assertEqual(installer.plugin_meta("class X: pass", "some_dir")["id"], "Some_dir")

    def test_inspect_single_plugin(self):
        raw = zip_bytes({"Demo/__init__.py": INIT, "Demo/helper.py": "x = 1\n",
                         "__MACOSX/._Demo": "junk"})
        info = installer.inspect_zip(raw)
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["id"], "DemoPlugin")
        self.assertEqual(info["prefix"], "Demo/")
        self.assertEqual(info["files"], 2)

    def test_inspect_repo_layout_and_prefer(self):
        raw = zip_bytes({
            "plugins.v3/aaa/__init__.py": INIT,
            "plugins.v3/bbb/__init__.py": INIT.replace("DemoPlugin", "OtherPlugin"),
        })
        info = installer.inspect_zip(raw)
        self.assertTrue(info["ok"])
        self.assertEqual(len(info["candidates"]), 2)
        picked = installer.inspect_zip(raw, "plugins.v3/bbb")
        self.assertEqual(picked["id"], "OtherPlugin")
        self.assertIn("没找到", installer.inspect_zip(raw, "plugins.v3/nope")["error"])

    def test_inspect_without_init_reports_readable_error(self):
        info = installer.inspect_zip(zip_bytes({"readme.txt": "hi"}))
        self.assertFalse(info["ok"])
        self.assertIn("__init__.py", info["error"])

    def test_extract_blocks_path_traversal(self):
        raw = zip_bytes({
            "Demo/__init__.py": INIT,
            "Demo/../evil.py": "boom",
            "Demo/../../etc/evil2.py": "boom",
            "Demo/sub/ok.py": "ok",
        })
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "demoplugin"
            count, error = installer.extract_plugin(raw, "Demo/", dest)
            self.assertEqual(error, "")
            self.assertTrue((dest / "__init__.py").is_file())
            self.assertTrue((dest / "sub" / "ok.py").is_file())
            self.assertFalse((dest.parent / "evil.py").exists())
            self.assertFalse((dest.parent / "etc" / "evil2.py").exists())
            self.assertGreaterEqual(count, 2)

    def test_extract_replaces_existing_and_backup_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            dest = root / "demoplugin"
            dest.mkdir()
            (dest / "__init__.py").write_text("old = 1\n", encoding="utf-8")
            archive = root / "backup.zip"
            self.assertTrue(installer.make_backup(dest, archive))
            raw = zip_bytes({"Demo/__init__.py": INIT})
            count, error = installer.extract_plugin(raw, "Demo/", dest)
            self.assertEqual(error, "")
            self.assertIn("DemoPlugin", (dest / "__init__.py").read_text(encoding="utf-8"))
            ok, error = installer.restore_backup(archive, dest)
            self.assertTrue(ok, error)
            self.assertEqual((dest / "__init__.py").read_text(encoding="utf-8"), "old = 1\n")

    def test_restore_rejects_missing_archive(self):
        ok, error = installer.restore_backup(pathlib.Path("/tmp/pdg-nope.zip"),
                                             pathlib.Path("/tmp/pdg-nope-dir"))
        self.assertFalse(ok)
        self.assertIn("不在了", error)


class RenderTests(unittest.TestCase):
    def test_render_escapes_dynamic_text_and_has_no_polling(self):
        html = renderer.render({
            "uploads": [], "history": [], "log": ["<script>alert(1)</script>"],
            "install_count": 3, "plugins_root": "/config/<app>/plugins",
            "max_size_text": "20 MB", "last_install_id": "<b>",
        })
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("&lt;app&gt;", html)
        self.assertIn("linear-gradient(135deg,#4F46E5,#7C3AED)", html)
        self.assertNotIn("setInterval", html)
        self.assertNotIn("__csPoll", html)
        self.assertIn("pdgTip", html)

    def test_render_handles_broken_data(self):
        for bad in (None, {}, {"uploads": "oops", "history": 1, "log": None}):
            self.assertIn("插件调试", renderer.render(bad if isinstance(bad, dict) else {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
