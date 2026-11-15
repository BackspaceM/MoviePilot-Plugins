#!/usr/bin/env python3
"""Reseed 辅种纯逻辑层单元测试，不需要 MoviePilot 宿主。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins.v3" / "reseedcrossseed"))

import core  # noqa: E402


def _bstr(text: str) -> bytes:
    raw = text.encode("utf-8")
    return f"{len(raw)}:".encode() + raw


def _bint(value: int) -> bytes:
    return b"i" + str(value).encode() + b"e"


def make_torrent(name: str, files: dict[str, int]) -> bytes:
    """构造一个最小可用的单/多文件种子，用于解析与复核测试。"""
    info = b"d"
    info += _bstr("name") + _bstr(name)
    info += _bstr("piece length") + _bint(262144)
    info += _bstr("pieces") + _bstr("a" * 20)
    if len(files) == 1 and next(iter(files)) == name:
        info += _bstr("length") + _bint(files[name])
    else:
        info += _bstr("files") + b"l"
        for rel, size in files.items():
            info += b"d" + _bstr("length") + _bint(size) + _bstr("path") + b"l"
            for part in rel.split("/"):
                info += _bstr(part)
            info += b"e" + b"e"
        info += b"e"
    info += b"e"
    return b"d" + _bstr("announce") + _bstr("https://tracker.example/announce") + _bstr("info") + info + b"e"


class ConfigTests(unittest.TestCase):
    def test_parse_bindings(self):
        text = (
            "# 注释\n"
            "HDU:青蛙:passkey-1\n"
            "TJUPT：北洋：passkey-2：download.php?id={id}&passkey={passkey}\n"
            "缺字段:只有两个\n"
        )
        bindings = core.parse_bindings(text)
        self.assertEqual(set(bindings), {"hdu", "tjupt"})
        self.assertEqual(bindings["hdu"].mp_name, "青蛙")
        self.assertEqual(bindings["hdu"].download_tpl, core.DEFAULT_DOWNLOAD_TPL)
        self.assertIn("{id}", bindings["tjupt"].download_tpl)

    def test_parse_dirs_and_list(self):
        self.assertEqual(core.parse_dirs("#x\n/downloads\n/downloads/\n/media"), ["/downloads", "/media"])
        self.assertEqual(core.parse_list("HDU,TJUPT,HDU"), ["HDU", "TJUPT"])
        self.assertEqual(core.parse_bindings(None), {})

    def test_urls_and_secrets(self):
        self.assertEqual(core.normalize_api_url(""), core.DEFAULT_API)
        self.assertEqual(core.normalize_api_url("example.org/api"), "https://example.org/api/")
        url = core.build_torrent_url("https://site.example", core.DEFAULT_DOWNLOAD_TPL, "123", "pk&1")
        self.assertEqual(url, "https://site.example/download.php?id=123&passkey=pk%261")
        self.assertNotIn("passkey-abcdefgh", core.mask_secret("passkey-abcdefgh"))

    def test_cron(self):
        self.assertTrue(core.validate_cron("0 5 * * ?")[0])
        self.assertEqual(core.validate_cron("0 5 * * ?")[1], "0 5 * * *")
        self.assertFalse(core.validate_cron("0 5 * *")[0])
        self.assertFalse(core.validate_cron("99 5 * * *")[0])
        self.assertFalse(core.validate_cron("bad")[0])


class IndexTests(unittest.TestCase):
    def test_build_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "single.mkv").write_bytes(b"x" * 100)
            (root / "Pack").mkdir()
            (root / "Pack" / "a.mkv").write_bytes(b"y" * 200)
            (root / "Pack" / "sub").mkdir()
            (root / "Pack" / "sub" / "b.mkv").write_bytes(b"z" * 300)
            (root / "ignored.torrent").write_bytes(b"t")
            (root / ".hidden").write_bytes(b"h")
            index, items = core.build_index(str(root))
            self.assertEqual(index["base_dir"], str(root))
            self.assertEqual(index["result"]["single.mkv"], 100)
            self.assertEqual(index["result"]["Pack"], {"a.mkv": 200, "sub/b.mkv": 300})
            self.assertNotIn("ignored.torrent", index["result"])
            self.assertNotIn(".hidden", index["result"])
            self.assertEqual(len(items), 2)
            by_name = {item.name: item for item in items}
            self.assertIsNone(by_name["single.mkv"].files)
            self.assertEqual(by_name["Pack"].size, 500)

    def test_build_index_missing_dir(self):
        index, items = core.build_index("/definitely/not/here")
        self.assertEqual(index["result"], {})
        self.assertEqual(items, [])

    def test_encode_is_ascii_and_digest_stable(self):
        index = {"base_dir": "/media", "result": {"中文剧集": {"一.mkv": 100}}}
        payload = core.encode_index(index)
        self.assertTrue(all(byte < 128 for byte in payload))
        self.assertIn(b"\\u4e2d", payload)
        self.assertEqual(core.index_digest(index), core.index_digest(dict(index)))
        other = {"base_dir": "/media", "result": {"中文剧集": {"一.mkv": 101}}}
        self.assertNotEqual(core.index_digest(index), core.index_digest(other))


class ResponseTests(unittest.TestCase):
    def test_parse_sites_info(self):
        body = [{"name": "HDU", "base_url": "https://hdu.example/", "_enable": False},
                {"name": "", "base_url": "x"}, "bad"]
        self.assertEqual(core.parse_sites_info(body), [{"name": "HDU", "base_url": "https://hdu.example/"}])
        self.assertEqual(core.parse_sites_info({"data": body})[0]["name"], "HDU")
        self.assertEqual(core.parse_sites_info(None), [])

    def test_parse_site_tokens(self):
        self.assertEqual(
            core.parse_site_tokens("HDU-23456,TJUPT-34567, bad, ABC-x"),
            [("HDU", "23456"), ("TJUPT", "34567")],
        )

    def test_parse_upload_result(self):
        body = {
            "success": True,
            "base_dir": "/downloads",
            "result": [
                {"name": "Pack",
                 "cmp_success": [{"id": 1, "sites": "HDU-23456,TJUPT-34567"}],
                 "cmp_warning": [{"id": 2, "sites": "HDU-99999"}]},
                {"name": "NoSite", "cmp_success": [{"id": 3, "sites": ""}]},
            ],
        }
        hits = core.parse_upload_result(body)
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0].key, "HDU-23456")
        self.assertTrue(hits[0].full_match)
        self.assertFalse(hits[2].full_match)
        self.assertEqual(core.parse_upload_result({"success": False}), [])
        self.assertEqual(core.parse_upload_result(None), [])


class TorrentTests(unittest.TestCase):
    def test_parse_single_file(self):
        meta = core.parse_torrent(make_torrent("Movie.mkv", {"Movie.mkv": 1024}))
        self.assertEqual(len(meta.info_hash), 40)
        self.assertEqual(meta.name, "Movie.mkv")
        self.assertEqual(meta.total_size, 1024)
        self.assertEqual(meta.files, {"Movie.mkv": 1024})
        self.assertTrue(core.is_torrent_content(make_torrent("a", {"a": 1})))
        self.assertFalse(core.is_torrent_content(b"<html>login</html>"))
        self.assertFalse(core.is_torrent_content(None))

    def test_parse_multi_file(self):
        meta = core.parse_torrent(make_torrent("Pack", {"a.mkv": 100, "sub/b.mkv": 200}))
        self.assertEqual(meta.files, {"a.mkv": 100, "sub/b.mkv": 200})
        self.assertEqual(meta.total_size, 300)

    def test_parse_bad(self):
        with self.assertRaises(core.TorrentError):
            core.parse_torrent(b"not-a-torrent")
        with self.assertRaises(core.TorrentError):
            core.parse_torrent("string")

    def test_verify_match_variants(self):
        local = core.IndexItem("Pack", "/downloads", {"a.mkv": 100, "sub/b.mkv": 200}, 300)
        self.assertTrue(core.verify_match(local, core.parse_torrent(
            make_torrent("Pack", {"a.mkv": 100, "sub/b.mkv": 200}))))
        self.assertTrue(core.verify_match(local, core.parse_torrent(
            make_torrent("Pack", {"Pack/a.mkv": 100, "Pack/sub/b.mkv": 200}))))
        self.assertFalse(core.verify_match(local, core.parse_torrent(
            make_torrent("Pack", {"a.mkv": 101, "sub/b.mkv": 200}))))
        self.assertFalse(core.verify_match(local, core.parse_torrent(
            make_torrent("Pack", {"a.mkv": 100}))))
        self.assertTrue(core.verify_match(local, core.parse_torrent(
            make_torrent("Pack", {"a.mkv": 101, "sub/b.mkv": 200})), tolerance=0.05))

    def test_verify_single_file(self):
        local = core.IndexItem("Movie.mkv", "/downloads", None, 1024)
        self.assertTrue(core.verify_match(local, core.parse_torrent(
            make_torrent("Movie.mkv", {"Movie.mkv": 1024}))))
        self.assertFalse(core.verify_match(local, core.parse_torrent(
            make_torrent("Movie.mkv", {"Movie.mkv": 2048}))))

    def test_chunked(self):
        self.assertEqual([list(x) for x in core.chunked([1, 2, 3, 4, 5], 2)], [[1, 2], [3, 4], [5]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
