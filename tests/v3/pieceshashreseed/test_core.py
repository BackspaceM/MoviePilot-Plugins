"""PiecesHashReseed 纯逻辑单元测试。

在 MoviePilot V3 宿主虚拟环境中运行：
    ../MoviePilot/.venv/bin/python -m pytest tests/v3/pieceshashreseed
"""

import hashlib
import unittest

from app.plugins.pieceshashreseed import core


def _bencode(value):
    """测试用的最小 bencode 编码器。"""
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, dict):
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in sorted(value.items())) + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(v) for v in value) + b"e"
    raise TypeError(value)


PIECES = bytes(range(20)) * 3
SINGLE_INFO = {b"name": b"movie.mkv", b"piece length": 16384, b"pieces": PIECES, b"length": 123456}
MULTI_INFO = {
    b"name": b"folder",
    b"piece length": 16384,
    b"pieces": PIECES,
    b"files": [{b"length": 10, b"path": [b"a.mkv"]}, {b"length": 20, b"path": [b"b.nfo"]}],
}


class TestTorrentFingerprint(unittest.TestCase):
    """验证种子解析与内容指纹计算。"""

    def test_info_hash_uses_raw_info_bytes(self):
        data = _bencode({b"announce": b"https://tracker/a", b"info": SINGLE_INFO})
        fingerprint = core.parse_torrent(data)
        self.assertEqual(fingerprint.info_hash, hashlib.sha1(_bencode(SINGLE_INFO)).hexdigest())
        self.assertEqual(fingerprint.pieces_hash, hashlib.sha1(PIECES).hexdigest())
        self.assertEqual(fingerprint.name, "movie.mkv")
        self.assertEqual(fingerprint.total_size, 123456)
        self.assertEqual(fingerprint.file_count, 1)

    def test_multi_file_torrent(self):
        fingerprint = core.parse_torrent(_bencode({b"info": MULTI_INFO}))
        self.assertEqual(fingerprint.total_size, 30)
        self.assertEqual(fingerprint.file_count, 2)

    def test_site_torrent_same_pieces_different_info_hash(self):
        """不同站点种子 announce/private 不同，但 pieces hash 应保持一致。"""
        local = core.parse_torrent(_bencode({b"announce": b"https://a", b"info": SINGLE_INFO}))
        remote = core.parse_torrent(
            _bencode({b"announce": b"https://b", b"info": {**SINGLE_INFO, b"private": 1}})
        )
        self.assertEqual(local.pieces_hash, remote.pieces_hash)
        self.assertNotEqual(local.info_hash, remote.info_hash)

    def test_invalid_torrent_rejected(self):
        with self.assertRaises(core.BencodeError):
            core.parse_torrent(b"<html>login</html>")
        with self.assertRaises(core.BencodeError):
            core.parse_torrent(_bencode({b"info": {b"name": b"x", b"pieces": b"short"}}))
        self.assertFalse(core.is_torrent_content(b"<html></html>"))
        self.assertTrue(core.is_torrent_content(_bencode({b"info": SINGLE_INFO})))


class TestConfigParsing(unittest.TestCase):
    """验证凭据、列表、路径映射与 cron 配置解析。"""

    def test_credentials(self):
        parsed = core.parse_credentials(
            "青蛙:abcdefgh:3\n憨憨：xyz：nexusapi/pieces-hash\n"
            "自定义:pp:https://pt.site/api/pieces-hash:5\n# 注释\n坏行"
        )
        self.assertEqual(parsed["青蛙"].gap, 3)
        self.assertEqual(parsed["憨憨"].api_path, "nexusapi/pieces-hash")
        self.assertEqual(parsed["自定义"].api_path, "https://pt.site/api/pieces-hash")
        self.assertEqual(parsed["自定义"].gap, 5)
        self.assertNotIn("坏行", parsed)

    def test_cron(self):
        self.assertEqual(core.validate_cron("0 4 * * *"), (True, "0 4 * * *"))
        self.assertEqual(core.validate_cron("0 0 1 ? *"), (True, "0 0 1 * *"))
        self.assertTrue(core.validate_cron("*/15 2-6 * * 1,5")[0])
        self.assertFalse(core.validate_cron("99 4 * * *")[0])
        self.assertFalse(core.validate_cron("0 4 *")[0])

    def test_path_mapping(self):
        mappings = core.parse_path_mapping("/downloads::/volume1/pt\n/movie::/media")
        self.assertEqual(core.apply_path_mapping("/downloads/movie/a.mkv", mappings),
                         "/volume1/pt/movie/a.mkv")
        self.assertEqual(core.apply_path_mapping("/other/a.mkv", mappings), "/other/a.mkv")

    def test_helpers(self):
        self.assertEqual(core.parse_list("a, b\nc,a"), ["a", "b", "c"])
        self.assertEqual(list(core.chunked(list(range(5)), 2)), [[0, 1], [2, 3], [4]])
        self.assertTrue(core.mask_secret("abcdefghijk").startswith("abc***"))


class TestSiteContracts(unittest.TestCase):
    """验证站点 URL 构造、支持判定与响应解析。"""

    def test_urls(self):
        self.assertEqual(core.build_api_url("https://pt.site", "api/pieces-hash"),
                         "https://pt.site/api/pieces-hash")
        self.assertEqual(core.build_api_url("pt.site/", "nexusapi/pieces-hash"),
                         "https://pt.site/nexusapi/pieces-hash")
        self.assertEqual(core.build_api_url("https://pt.site", "https://other.site/x"),
                         "https://other.site/x")
        url = core.build_torrent_url("https://pt.site", core.DEFAULT_DOWNLOAD_TPL, "123", "abc")
        self.assertIn("id=123", url)
        self.assertIn("passkey=abc", url)

    def test_probe_classification(self):
        self.assertTrue(core.classify_probe(405, "text/html", "")[0])
        self.assertTrue(core.classify_probe(200, "application/json", {"data": {}})[0])
        self.assertFalse(core.classify_probe(404, "text/html", "")[0])
        self.assertFalse(core.classify_probe(200, "text/html", "<html>login</html>")[0])

    def test_pieces_response(self):
        key = "a" * 40
        self.assertEqual(
            core.parse_pieces_response({"success": True, "data": {key: "888", "success": "bad"}}),
            {key: "888"},
        )
        self.assertEqual(
            core.parse_pieces_response({"data": [{"pieces_hash": "b" * 40, "torrent_id": 12}]}),
            {"b" * 40: "12"},
        )
        self.assertEqual(core.parse_pieces_response(None), {})


if __name__ == "__main__":
    unittest.main()
