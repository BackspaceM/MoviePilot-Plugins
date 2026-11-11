#!/usr/bin/env python3
"""入库对账插件的离线回归测试（无需 MoviePilot 宿主）。

复用全仓契约校验器的宿主 stub，覆盖：
- 按钮 404 的根因（Response 必须是 Pydantic 模型、接口可注册、路径/方法匹配）；
- 整理历史对账状态分类，尤其是「不可核验 ≠ 已删除」的防误报约束；
- 主动扫描：下载器种子名录 ↔ 映射目录实体文件、路径映射、零匹配禁用删除；
- 全量清单与日志：种子名称可复制、明细不截断、离线也显示日志；
- 删除安全门槛：只读拒绝、令牌校验、预览不删、二次确认、下载器失败整批取消、
  媒体库文件保护、默认可恢复隔离、直接删除需显式选择。

用法：python3 tests/v3/transferaudit/run_stub_tests.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]  # tests/v3/transferaudit -> 仓库根
PLUGIN_DIR = ROOT / "plugins.v3" / "transferaudit"
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
AUDITOR = MODULE.auditor
RENDERER = MODULE.renderer
SCANNER = MODULE.scanner

PRESENT = {"kind": "file", "dev": 1, "ino": 11, "size": 1024, "mtime": 0.0}
MISSING = {"kind": "missing", "dev": None, "ino": None, "size": 0, "mtime": 0.0}


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from walk(value)


def hist(**kwargs):
    base = {
        "id": 1,
        "src": "/download/anime/E01.mkv",
        "src_storage": "local",
        "dest": "/media/anime/某番 S01E01.mkv",
        "dest_storage": "local",
        "mode": "link",
        "title": "某番",
        "year": "2026",
        "seasons": "S01",
        "episodes": "E01",
        "download_hash": "hash1",
        "date": "2026-11-01 12:00:00",
        "status": True,
    }
    base.update(kwargs)
    return base


def snapshot_for(records, live=(), probed=True, probes=None, config=None,
                 torrents=None, torrent_probed=False):
    """用可控的假数据构造快照，不触碰宿主与真实文件系统。"""
    def fake_probe(path, storage):
        if probes is not None:
            return probes(str(path), str(storage))
        return None if storage not in ("local", "", None) else dict(PRESENT)

    with mock.patch.object(AUDITOR, "probe_path", side_effect=fake_probe), mock.patch.object(
        AUDITOR, "probe_seed_hashes", return_value=(set(live), probed)
    ), mock.patch.object(
        SCANNER, "fetch_torrents",
        return_value=(list(torrents or []), bool(torrent_probed), 1 if torrent_probed else 0),
    ), mock.patch.object(SCANNER, "set_seed_checklist"):
        return AUDITOR.build_snapshot(records, config or {})


class TestContract(unittest.TestCase):
    """防止再次出现「按钮点击 Not Found」。"""

    def setUp(self) -> None:
        self.plugin = PLUGIN_CLS()

    def test_plugin_id_matches_directory(self) -> None:
        self.assertEqual(PLUGIN_ID, "TransferAudit")
        self.assertEqual(PLUGIN_DIR.name, "transferaudit")

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
        clicks = cc.collect_page_clicks(self.plugin)
        self.assertTrue(clicks, "详情页至少应有一个可点击按钮")
        prefix = f"plugin/{PLUGIN_ID}/"
        for click in clicks:
            self.assertTrue(click["api"].startswith(prefix), click["api"])
            action = click["api"][len(prefix):].split("?", 1)[0].split("/", 1)[0]
            self.assertIn(action, declared)
            self.assertIn(click["method"].upper(), declared[action])

    def test_apis_declare_bearer_auth(self) -> None:
        for api in self.plugin.get_api():
            self.assertEqual(api.get("auth"), "bear")

    def test_api_surface(self) -> None:
        paths = {a["path"] for a in self.plugin.get_api()}
        self.assertEqual(paths, {"/snapshot", "/refresh", "/scan", "/log", "/prepare", "/delete"})

    def test_form_has_minimal_safe_defaults(self) -> None:
        form, defaults = self.plugin.get_form()
        models = [n["props"]["model"] for n in walk(form)
                  if isinstance(n.get("props"), dict) and n["props"].get("model")]
        self.assertEqual(set(models), {"enabled", "scan_dirs", "path_map", "allow_delete", "delete_mode"})
        self.assertLessEqual(len(models), 5, "设置项要精简，别繁琐")
        self.assertFalse(defaults["allow_delete"], "删除能力默认必须关闭")
        self.assertEqual(defaults["delete_mode"], "trash", "默认必须可恢复")
        self.assertEqual(defaults["scan_dirs"], "")

    def test_page_and_dashboard_structure(self) -> None:
        page = self.plugin.get_page()
        self.assertTrue(any(n.get("component") == "VBtn" for n in walk(page)))
        self.assertTrue(any(n.get("html") for n in walk(page)))
        cols, attrs, elements = self.plugin.get_dashboard()
        self.assertIn("md", cols)
        self.assertIsInstance(attrs.get("refresh"), int)
        self.assertTrue(elements[0]["html"].startswith("<div"))
        self.assertEqual(self.plugin.get_dashboard_meta()[0]["key"], "audit")

    def test_snapshot_refresh_scan_log_return_envelope(self) -> None:
        snap = self.plugin.api_snapshot()
        self.assertTrue(snap.success)
        self.assertIn("counts", snap.data)
        for name in ("api_refresh", "api_scan", "api_log"):
            res = getattr(self.plugin, name)()
            self.assertTrue(res.success, name)
        prepare = self.plugin.api_prepare({})
        self.assertFalse(prepare.success, "只读模式下生成预览必须被拒绝")
        delete = self.plugin.api_delete({})
        self.assertFalse(delete.success, "只读模式下删除必须被拒绝")

    def test_enabled_by_default(self) -> None:
        self.assertTrue(self.plugin.get_state())
        self.plugin.init_plugin({"enabled": False})
        self.assertFalse(self.plugin.get_state())

    def test_delete_buttons_only_appear_when_explicitly_enabled(self) -> None:
        self.assertEqual(
            {c["api"].rsplit("/", 1)[-1] for c in cc.collect_page_clicks(self.plugin)},
            {"refresh", "scan"},
        )
        self.plugin.init_plugin({"allow_delete": True})
        AUDITOR.invalidate()
        actions = {c["api"].rsplit("/", 1)[-1] for c in cc.collect_page_clicks(self.plugin)}
        self.assertEqual(actions, {"refresh", "scan", "prepare"})

    def test_preview_and_confirm_buttons_carry_safe_params(self) -> None:
        import time

        self.plugin.init_plugin({"allow_delete": True})
        SCANNER._STATE["plan"] = {
            "id": "deadbeef", "ts": time.time(), "items": [], "mode": "trash",
            "roots": [], "protected": [], "path_map": [], "count": 2, "bytes": 1024,
        }
        AUDITOR.invalidate()
        buttons = [n for n in walk(self.plugin.get_page()) if n.get("component") == "VBtn"]
        by_action = {
            ((n.get("events") or {}).get("click") or {}).get("api", "").rsplit("/", 1)[-1]:
                ((n.get("events") or {}).get("click") or {})
            for n in buttons
        }
        self.assertEqual(by_action["prepare"].get("params", {}).get("scope"), "all")
        self.assertTrue(by_action["prepare"].get("params", {}).get("token"))
        self.assertEqual(by_action["delete"].get("params", {}).get("plan_id"), "deadbeef")
        self.assertEqual(by_action["delete"].get("params", {}).get("confirm"), SCANNER.DELETE_CONFIRM)
        SCANNER.clear_plan()
        AUDITOR.invalidate()

    def test_icon_files_exist(self) -> None:
        png = ROOT / "icons" / "transferaudit.png"
        svg = ROOT / "icons" / "transferaudit.svg"
        self.assertTrue(png.is_file())
        self.assertTrue(svg.is_file())
        self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(PLUGIN_CLS.plugin_icon.rsplit("/", 1)[-1], "transferaudit.png")


class TestClassification(unittest.TestCase):
    """对账状态分类：核心是绝不把「查不到」当成「已删除」。"""

    def test_all_status_branches(self) -> None:
        item = hist()
        cases = [
            (dict(MISSING), dict(PRESENT), "absent", "orphan"),
            (dict(PRESENT), dict(PRESENT), "absent", "leftover"),
            (dict(PRESENT), dict(PRESENT), "present", "healthy"),
            (dict(PRESENT), dict(PRESENT), "unknown", "pending"),
            (dict(PRESENT), dict(MISSING), "present", "missing"),
            (dict(MISSING), dict(MISSING), "absent", "stale"),
            (None, dict(PRESENT), "absent", "remote"),
            (dict({"kind": "invalid"}), dict(PRESENT), "absent", "invalid"),
        ]
        for src, dst, seed, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(AUDITOR.classify(item, src, dst, seed), expected)

    def test_source_gone_takes_priority_over_seed_state(self) -> None:
        self.assertEqual(AUDITOR.classify(hist(), dict(MISSING), dict(PRESENT), "unknown"), "orphan")

    def test_remote_storage_is_never_reported_as_deleted(self) -> None:
        self.assertIsNone(AUDITOR.probe_path("/remote/a.mkv", "u115"))
        self.assertIsNone(AUDITOR.probe_path("/remote/a.mkv", "rclone"))
        self.assertEqual(AUDITOR.classify(hist(), None, dict(PRESENT), "absent"), "remote")

    def test_unreachable_path_space_is_not_missing(self) -> None:
        with mock.patch.object(AUDITOR.os.path, "isdir", return_value=False):
            self.assertIsNone(AUDITOR.probe_path("/unmounted/share/anime/E01.mkv", "local"))

    def test_existing_parent_allows_missing_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(AUDITOR.probe_path(os.path.join(tmp, "gone.mkv"), "local")["kind"], "missing")

    def test_local_storage_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "x")
            open(probe, "w", encoding="utf-8").close()
            self.assertEqual(AUDITOR.probe_path(probe, None)["kind"], "file")

    def test_hardlink_detection(self) -> None:
        self.assertTrue(AUDITOR._hardlink(dict(PRESENT), dict(PRESENT)))
        self.assertFalse(AUDITOR._hardlink(dict(PRESENT), dict(PRESENT, ino=99)))
        self.assertFalse(AUDITOR._hardlink(dict(PRESENT, kind="dir"), dict(PRESENT)))


class TestSnapshot(unittest.TestCase):
    def test_aggregates_by_series_and_season(self) -> None:
        records = [hist(id=1, episodes="E01"), hist(id=2, episodes="E02", download_hash="hash2"),
                   hist(id=3, episodes="E03", title="另一部", download_hash="hash3")]
        data = snapshot_for(records, live={"hash1", "hash2", "hash3"})
        self.assertEqual(data["scanned"], 3)
        self.assertEqual(data["group_total"], 2)
        self.assertEqual(data["groups"][0]["counts"]["healthy"], 2)

    def test_leftover_paths_and_counts(self) -> None:
        records = [hist(id=1, src="/dl/E01.mkv"), hist(id=2, src="/dl/E02.mkv", download_hash="hash2")]
        data = snapshot_for(records, live=set())
        self.assertEqual(data["counts"]["leftover"], 2)
        self.assertEqual(len(data["leftover_paths"]), 2)

    def test_seed_not_probed_never_claims_leftover(self) -> None:
        data = snapshot_for([hist()], live=set(), probed=False)
        self.assertEqual(data["counts"]["leftover"], 0)
        self.assertEqual(data["counts"]["pending"], 1)

    def test_pack_hint_when_collection_and_single_sources_coexist(self) -> None:
        records = [hist(id=1, episodes="E01-E12", src="/dl/pack", download_hash="packhash"),
                   hist(id=2, episodes="E05", src="/dl/E05.mkv", download_hash="e05hash")]
        data = snapshot_for(records, live=set())
        self.assertIn("合集", data["groups"][0]["pack_hint"])

    def test_remote_and_unverifiable_are_counted_separately(self) -> None:
        def probes(path, storage):
            return None if storage == "u115" else dict(PRESENT)

        records = [hist(id=1, src_storage="u115"), hist(id=2)]
        data = snapshot_for(records, live=set(), probes=probes)
        self.assertEqual(data["counts"]["remote"], 1)

    def test_offline_snapshot_has_complete_structure(self) -> None:
        with mock.patch.object(AUDITOR, "_load_queries", return_value=None), mock.patch.object(
            SCANNER, "fetch_torrents", return_value=([], False, 0)
        ), mock.patch.object(SCANNER, "set_seed_checklist"):
            data = AUDITOR.build_snapshot()
        self.assertTrue(data["offline"])
        for key in ("scan", "files", "candidates", "torrents", "checklist", "log", "plan", "seed_list"):
            self.assertIn(key, data)
        self.assertTrue(RENDERER.render(data))

    def test_torrent_title_is_joined_to_history(self) -> None:
        torrents = [{"hash": "hash1", "title": "[PT]某番 S01E01", "content_path": "/dl/E01.mkv"}]
        data = snapshot_for([hist()], live=set(), torrents=torrents, torrent_probed=True)
        self.assertEqual(data["issues"][0]["torrent_title"], "[PT]某番 S01E01")
        self.assertIn("[PT]某番 S01E01", data["seed_list"])

    def test_all_issues_are_reported_without_truncation(self) -> None:
        records = [hist(id=i, download_hash=f"h{i}") for i in range(300)]
        data = snapshot_for(records, live=set())
        self.assertEqual(len(data["issues"]), 300)
        self.assertEqual(data["issue_total"], 300)
        html = RENDERER.render(data)
        self.assertIn("整理历史需关注明细（全量 · 300 条）", html)
        self.assertNotIn("此处仅展示前", html)


class TestFetcher(unittest.TestCase):
    class _Page:
        def __init__(self, items, total):
            self.items, self.total = items, total

    class _Queries:
        MAX_QUERY_PAGE_SIZE = 200

        def __init__(self, pages, total):
            self.pages, self.total, self.calls = pages, total, []

        def list_transfer_history(self, filters, page):
            self.calls.append((filters, dict(page)))
            return TestFetcher._Page(self.pages.get(page["page"], []), self.total)

    def test_empty_library_is_not_offline(self) -> None:
        with mock.patch.object(AUDITOR, "_load_queries", return_value=self._Queries({}, 0)):
            items, total, ok = AUDITOR.fetch_histories()
        self.assertEqual(items, [])
        self.assertTrue(ok)

    def test_invalid_response_is_offline(self) -> None:
        class Broken:
            def list_transfer_history(self, filters, page):
                return None

        with mock.patch.object(AUDITOR, "_load_queries", return_value=Broken()):
            _, _, ok = AUDITOR.fetch_histories()
        self.assertFalse(ok)

    def test_paginates_until_last_page(self) -> None:
        first = [hist(id=i) for i in range(200)]
        second = [hist(id=200 + i) for i in range(3)]
        queries = self._Queries({1: first, 2: second}, 203)
        with mock.patch.object(AUDITOR, "_load_queries", return_value=queries):
            items, total, ok = AUDITOR.fetch_histories()
        self.assertTrue(ok)
        self.assertEqual((total, len(items), len(queries.calls)), (203, 203, 2))


class TestConfigAndMapping(unittest.TestCase):
    def test_parse_config_is_idempotent(self) -> None:
        once = SCANNER.parse_config({"scan_dirs": "/a, /b", "path_map": "/x=/y\n/m => /n"})
        twice = SCANNER.parse_config(once)
        self.assertEqual(once["scan_dirs"], twice["scan_dirs"])
        self.assertEqual(once["path_map"], twice["path_map"])

    def test_map_path(self) -> None:
        pairs = [("/volume1/dl", "/downloads"), ("/volume2/x", "/media")]
        self.assertEqual(SCANNER.map_path("/volume1/dl/Anime/E01.mkv", pairs), "/downloads/Anime/E01.mkv")
        self.assertEqual(SCANNER.map_path("/other/E01.mkv", pairs), "/other/E01.mkv")
        self.assertEqual(SCANNER.map_path("", pairs), "")

    def test_dangerous_roots_are_rejected(self) -> None:
        for root in ("/", "/etc", "/etc/passwd", "/proc/1", "/app/x", "/config/x", "/volume1"):
            with self.subTest(root=root):
                self.assertFalse(SCANNER._is_safe_root(root))

    def test_normal_download_roots_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "downloads")
            os.makedirs(root)
            self.assertTrue(SCANNER._is_safe_root(root))
        self.assertIn("/downloads", SCANNER.ALLOWED_TOP_ROOTS)


class TestActiveScan(unittest.TestCase):
    """真实临时目录 + 模拟下载器，验证主动扫描与零匹配禁用删除。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self.tmp.name, "downloads")
        self.lib = os.path.join(self.tmp.name, "media")
        self.pack = os.path.join(self.dl, "Pack")
        for folder in (self.dl, self.lib, self.pack):
            os.makedirs(folder)
        self.paths = {}
        for name, folder in (("E01.mkv", self.dl), ("E02.mkv", self.dl),
                             ("E03.mkv", self.dl), ("E04.mkv", self.pack)):
            path = os.path.join(folder, name)
            with open(path, "wb") as fh:
                fh.write(b"x" * 4096)
            self.paths[name] = path
        with open(os.path.join(self.lib, "E02.mkv"), "wb") as fh:
            fh.write(b"x" * 4096)
        self.lib_file = os.path.join(self.lib, "E02.mkv")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _torrents(self):
        return [
            {"hash": "h1", "title": "[PT]某番 S01E01", "name": "某番",
             "content_path": self.paths["E01.mkv"], "save_path": self.dl,
             "size": 4096, "state": "seeding"},
            {"hash": "h2", "title": "[PT]某番 S01 合集", "name": "某番",
             "content_path": self.pack, "save_path": self.dl,
             "size": 4096, "state": "seeding"},
        ]

    def _records(self):
        return [
            hist(id=1, src=self.paths["E01.mkv"], dest=os.path.join(self.lib, "E01.mkv"), download_hash="h1"),
            hist(id=2, src=self.paths["E02.mkv"], dest=self.lib_file, download_hash="h2"),
        ]

    def _services(self, torrents=None):
        payload = self._torrents() if torrents is None else torrents

        class Inst:
            def list_torrents(self, **kwargs):
                return list(payload)

        class Svc:
            name = "qb"
            instance = Inst()

        return [Svc()]

    def _build(self, cfg, torrents=None, services=None):
        with mock.patch.object(SCANNER, "_load_downloader_services",
                               return_value=self._services(torrents) if services is None else services), \
             mock.patch.object(AUDITOR, "probe_seed_hashes", return_value=({"h1"}, True)), \
             mock.patch.object(SCANNER, "set_seed_checklist"):
            return AUDITOR.build_snapshot(self._records(), cfg)

    def test_finds_residual_files_and_seed_names(self) -> None:
        cfg = SCANNER.parse_config({"scan_dirs": self.dl})
        data = self._build(cfg)
        self.assertTrue(data["scan"]["ok"])
        self.assertEqual(data["file_total"], 4)
        self.assertEqual(data["scan"]["matched"], 2)
        names = {os.path.basename(f["path"]) for f in data["candidates"]}
        self.assertEqual(names, {"E02.mkv", "E03.mkv"})
        self.assertIn("[PT]某番 S01E01", data["seed_list"])
        self.assertGreaterEqual(data["checklist_total"], 4)
        self.assertTrue(data["log"])

    def test_zero_match_disables_delete(self) -> None:
        """路径映射全错时，一个文件都匹配不上种子 → 必须禁用删除，防止误删整库。"""
        cfg = SCANNER.parse_config({"scan_dirs": self.dl, "allow_delete": True})
        data = self._build(cfg, torrents=[])
        self.assertFalse(data["scan"]["ok"])
        self.assertIn("路径映射可能不正确", data["scan"]["reason"])
        self.assertEqual(data["candidates"], [])

    def test_downloader_failure_disables_delete(self) -> None:
        class Broken:
            name = "qb"
            instance = type("I", (), {"list_torrents": lambda self, **kw: (_ for _ in ()).throw(RuntimeError("offline"))})()

        cfg = SCANNER.parse_config({"scan_dirs": self.dl, "allow_delete": True})
        data = self._build(cfg, services=[Broken()])
        self.assertFalse(data["scan"]["ok"])
        self.assertEqual(data["candidates"], [])
        self.assertIn("未能读取下载器种子名录", data["scan"]["reason"])

    def test_media_library_file_is_never_a_candidate(self) -> None:
        cfg = SCANNER.parse_config({"scan_dirs": self.dl, "allow_delete": True})
        data = self._build(cfg)
        self.assertNotIn(os.path.realpath(self.lib_file), {os.path.realpath(f["path"]) for f in data["candidates"]})
        self.assertTrue(all(f["path"] != self.lib_file for f in data["candidates"]))

    def test_path_map_translates_downloader_paths(self) -> None:
        mapped = SCANNER.map_path("/volume1/dl/E01.mkv", [("/volume1/dl", self.dl)])
        cfg = SCANNER.parse_config({
            "scan_dirs": self.dl, "path_map": f"/volume1/dl={self.dl}", "allow_delete": True,
        })
        torrents = [{"hash": "h1", "title": "T1", "content_path": "/volume1/dl/E01.mkv",
                     "save_path": "/volume1/dl", "size": 4096, "state": "seeding"}]
        data = self._build(cfg, torrents=torrents)
        self.assertEqual(mapped, self.paths["E01.mkv"])
        self.assertTrue(data["scan"]["ok"])
        self.assertNotIn("E01.mkv", {os.path.basename(f["path"]) for f in data["candidates"]})


class TestDeleteSafety(unittest.TestCase):
    """删除的所有安全门槛：只读、令牌、预览、二次确认、复检、隔离恢复。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self.tmp.name, "downloads")
        os.makedirs(self.dl)
        self.a = os.path.join(self.dl, "A.mkv")
        self.b = os.path.join(self.dl, "B.mkv")
        self.keep = os.path.join(self.dl, "KEEP.mkv")
        for p in (self.a, self.b, self.keep):
            with open(p, "wb") as fh:
                fh.write(b"x" * 2048)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _torrents(self):
        return [{"hash": "live", "title": "仍在做种", "content_path": self.keep,
                 "save_path": self.dl, "size": 2048, "state": "seeding"}]

    def _patch(self):
        inst = type("I", (), {"list_torrents": lambda _self, **kw: self._torrents()})()
        svc = type("S", (), {"name": "qb", "instance": inst})()
        return mock.patch.object(SCANNER, "_load_downloader_services", return_value=[svc])

    def _scan(self, **overrides):
        raw = {"scan_dirs": self.dl, "allow_delete": True, "delete_mode": "trash"}
        raw.update(overrides)
        cfg = SCANNER.parse_config(raw)
        result = SCANNER.scan_files(cfg, [], self._torrents(), True, AUDITOR._field)
        token = SCANNER.register_scan(result, cfg)
        return cfg, token, result

    def test_readonly_mode_rejects_prepare_and_execute(self) -> None:
        cfg, token, _ = self._scan(allow_delete=False)
        self.assertFalse(SCANNER.prepare(cfg, token, scope="all")["ok"])
        self.assertFalse(SCANNER.execute(cfg, "whatever", SCANNER.DELETE_CONFIRM)["ok"])
        self.assertTrue(os.path.exists(self.a))

    def test_wrong_token_is_rejected(self) -> None:
        cfg, _, _ = self._scan()
        self.assertFalse(SCANNER.prepare(cfg, "deadbeef", scope="all")["ok"])

    def test_prepare_never_deletes(self) -> None:
        cfg, token, _ = self._scan()
        with self._patch():
            pre = SCANNER.prepare(cfg, token, scope="all")
        self.assertTrue(pre["ok"])
        self.assertTrue(os.path.exists(self.a))
        self.assertTrue(os.path.exists(self.b))
        self.assertIsNotNone(SCANNER.current_plan())
        self.assertIn("未删除任何文件", pre["message"])

    def test_execute_requires_exact_confirm_phrase(self) -> None:
        cfg, token, _ = self._scan()
        with self._patch():
            SCANNER.prepare(cfg, token, scope="all")
            plan = SCANNER.current_plan()
            self.assertFalse(SCANNER.execute(cfg, plan["id"], "YES")["ok"])
            self.assertFalse(SCANNER.execute(cfg, plan["id"], "")["ok"])
        self.assertTrue(os.path.exists(self.a))

    def test_execute_requires_valid_plan(self) -> None:
        cfg, _, _ = self._scan()
        with self._patch():
            self.assertFalse(SCANNER.execute(cfg, "no-such-plan", SCANNER.DELETE_CONFIRM)["ok"])
        self.assertTrue(os.path.exists(self.a))

    def test_downloader_failure_cancels_whole_batch(self) -> None:
        cfg, token, _ = self._scan()
        with self._patch():
            SCANNER.prepare(cfg, token, scope="all")
            plan = SCANNER.current_plan()
        broken = type("S", (), {"name": "qb", "instance": type(
            "I", (), {"list_torrents": lambda _s, **kw: (_ for _ in ()).throw(RuntimeError("offline"))})()})()
        with mock.patch.object(SCANNER, "_load_downloader_services", return_value=[broken]):
            result = SCANNER.execute(cfg, plan["id"], SCANNER.DELETE_CONFIRM)
        self.assertFalse(result["ok"])
        self.assertTrue(os.path.exists(self.a))
        self.assertTrue(os.path.exists(self.b))

    def test_trash_mode_is_recoverable_and_keeps_seeding_file(self) -> None:
        cfg, token, _ = self._scan()
        with self._patch():
            SCANNER.prepare(cfg, token, scope="all")
            plan = SCANNER.current_plan()
            res = SCANNER.execute(cfg, plan["id"], SCANNER.DELETE_CONFIRM)
        self.assertTrue(res["ok"])
        self.assertFalse(os.path.exists(self.a))
        self.assertFalse(os.path.exists(self.b))
        self.assertTrue(os.path.exists(self.keep), "仍在做种的文件绝不能被删")
        trash = os.path.join(self.dl, SCANNER.TRASH_DIRNAME)
        moved = [os.path.join(base, f) for base, _, files in os.walk(trash) for f in files]
        self.assertEqual(len(moved), 2, "隔离目录里应能找回被移走的文件")

    def test_permanent_mode_must_be_explicit(self) -> None:
        cfg, token, _ = self._scan(delete_mode="permanent")
        with self._patch():
            SCANNER.prepare(cfg, token, scope="all")
            plan = SCANNER.current_plan()
            res = SCANNER.execute(cfg, plan["id"], SCANNER.DELETE_CONFIRM)
        self.assertTrue(res["ok"])
        self.assertFalse(os.path.exists(self.a))
        self.assertFalse(os.path.isdir(os.path.join(self.dl, SCANNER.TRASH_DIRNAME)))

    def test_plan_is_cleared_after_execution(self) -> None:
        cfg, token, _ = self._scan()
        with self._patch():
            SCANNER.prepare(cfg, token, scope="all")
            plan = SCANNER.current_plan()
            SCANNER.execute(cfg, plan["id"], SCANNER.DELETE_CONFIRM)
        self.assertIsNone(SCANNER.current_plan(), "预览必须一次性，防止重复点击误删")

    def test_out_of_root_path_is_rejected_by_prepare(self) -> None:
        cfg, token, result = self._scan()
        outside = os.path.join(self.tmp.name, "outside.mkv")
        with open(outside, "wb") as fh:
            fh.write(b"x" * 1024)
        with self._lock_hack(outside):
            with self._patch():
                pre = SCANNER.prepare(cfg, token, scope="all")
        self.assertFalse(pre["ok"], "扫描根之外的文件必须被整批拒绝")
        self.assertTrue(os.path.exists(outside))

    @staticmethod
    def _lock_hack(path):
        """把一条越界路径塞进候选清单，验证服务端复检而不是信任前端。"""
        from contextlib import contextmanager

        @contextmanager
        def patcher():
            with SCANNER._lock():
                old = list(SCANNER._STATE["candidates"])
                SCANNER._STATE["candidates"] = [dict(old[0], path=path)] if old else []
            try:
                yield
            finally:
                with SCANNER._lock():
                    SCANNER._STATE["candidates"] = old

        return patcher()

    def test_log_records_every_scan_and_delete(self) -> None:
        SCANNER.emit("单测日志标记-XYZ")
        self.assertIn("单测日志标记-XYZ", SCANNER.log_text())
        cfg, token, _ = self._scan()
        with self._patch():
            SCANNER.prepare(cfg, token, scope="all")
            plan = SCANNER.current_plan()
            SCANNER.execute(cfg, plan["id"], SCANNER.DELETE_CONFIRM)
        text = SCANNER.log_text()
        self.assertIn("已生成删除预览", text)
        self.assertIn("已处理源文件", text)


class TestLogsAndChecklist(unittest.TestCase):
    def test_log_contains_copyable_seed_names(self) -> None:
        SCANNER.set_seed_checklist(["[PT]番剧 A S01E01", "[PT]番剧 B S01E02"])
        text = SCANNER.log_text()
        self.assertIn("可复制的种子名称清单", text)
        self.assertIn("[PT]番剧 A S01E01", text)
        self.assertIn("种子名称 |", text)

    def test_log_has_placeholder_when_empty(self) -> None:
        with SCANNER._lock():
            SCANNER._LOG.clear()
        self.assertIn("暂无操作记录", SCANNER.log_text())


class TestRenderer(unittest.TestCase):
    def setUp(self) -> None:
        self.data = snapshot_for(
            [hist(id=1, download_hash="h1"), hist(id=2, download_hash="h2")], live=set(),
        )

    def test_escapes_dynamic_text(self) -> None:
        self.data["issues"] = [dict(self.data["issues"][0], title="<script>alert(1)</script>")]
        html = RENDERER.render(self.data)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_full_view_has_all_sections_and_no_truncation(self) -> None:
        html = RENDERER.render(self.data)
        for key in ("需关注明细", "源残留路径清单", "只读报告", "种子名称清单",
                    "完整核对清单", "映射目录扫描结果", "下载器种子名录", "操作日志", "扫描状态"):
            self.assertIn(key, html)
        self.assertNotIn("此处仅展示前", html)
        self.assertIn("复制清单", html)

    def test_compact_view_omits_detail_tables(self) -> None:
        html = RENDERER.render(self.data, compact=True)
        self.assertNotIn("需关注明细", html)
        self.assertNotIn("源残留路径清单", html)
        self.assertIn("扫描状态", html)

    def test_offline_placeholder_still_shows_logs(self) -> None:
        offline = dict(self.data, offline=True, log="2026-11-12 [INFO] 扫描完成")
        html = RENDERER.render(offline)
        self.assertIn("离线预览", html)
        self.assertIn("操作日志", html)
        self.assertIn("扫描完成", html)

    def test_readonly_mode_has_no_delete_controls(self) -> None:
        html = RENDERER.render(self.data)
        self.assertNotIn('class="taud-sel"', html)
        self.assertNotIn("此操作非常危险", html)
        self.assertNotIn("taud-delbtn", html.replace(".taud-delbtn", ""))

    def test_delete_enabled_shows_prominent_warning_and_checkboxes(self) -> None:
        data = dict(self.data, delete_enabled=True, delete_mode="trash",
                    candidates=[{"index": 0, "name": "E02.mkv", "path": "/dl/E02.mkv",
                                 "status": "residue", "deletable": True, "size": 1024,
                                 "size_text": "1.0 KB", "known": True, "covered": False}],
                    files=[{"index": 0, "name": "E02.mkv", "path": "/dl/E02.mkv",
                            "status": "residue", "deletable": True, "size": 1024,
                            "size_text": "1.0 KB", "known": True, "covered": False}])
        html = RENDERER.render(data)
        self.assertIn("⚠️ 此操作非常危险", html)
        self.assertIn("会删除", html)
        self.assertIn('class="taud-sel"', html)
        self.assertIn("生成删除预览", html)
        self.assertIn("两次确认", html)

    def test_delete_js_has_no_dynamic_paths_or_ampersands(self) -> None:
        js = RENDERER._delete_js("abc123")
        self.assertIn("'abc123'", js)
        self.assertNotIn("&", js)
        self.assertNotIn('"', js)
        self.assertIn("confirm(", js)


class TestEndToEnd(unittest.TestCase):
    """用真实临时目录与真实硬链接复现 PT 连载番剧删种后的五种状态。"""

    def test_pt_anime_scenario_with_real_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            dl = os.path.join(root, "download")
            lib = os.path.join(root, "media")
            os.makedirs(dl)
            os.makedirs(lib)

            def make(name, remove_src=False):
                src, dst = os.path.join(dl, name), os.path.join(lib, name)
                with open(src, "wb") as fh:
                    fh.write(b"x" * 128)
                os.link(src, dst)
                if remove_src:
                    os.remove(src)
                return src, dst

            src1, dst1 = make("E01.mkv", remove_src=True)
            src2, dst2 = make("E02.mkv")
            src3, dst3 = make("E03.mkv")
            src4, dst4 = make("E04.mkv")
            os.remove(dst4)

            records = [
                hist(id=1, src=src1, dest=dst1, episodes="E01", download_hash="h1"),
                hist(id=2, src=src2, dest=dst2, episodes="E02", download_hash="h2"),
                hist(id=3, src=src3, dest=dst3, episodes="E03", download_hash="h3"),
                hist(id=4, src=src4, dest=dst4, episodes="E04", download_hash="h4"),
                hist(id=5, src="/remote/E05.mkv", src_storage="u115",
                     dest=dst3, episodes="E05", download_hash="h5"),
            ]
            with mock.patch.object(AUDITOR, "probe_seed_hashes", return_value=({"h3"}, True)), \
                 mock.patch.object(SCANNER, "fetch_torrents", return_value=([], False, 0)), \
                 mock.patch.object(SCANNER, "set_seed_checklist"):
                data = AUDITOR.build_snapshot(records)

            self.assertEqual(data["counts"]["orphan"], 1)
            self.assertEqual(data["counts"]["leftover"], 1)
            self.assertEqual(data["counts"]["healthy"], 1)
            self.assertEqual(data["counts"]["missing"], 1)
            self.assertEqual(data["counts"]["remote"], 1)
            self.assertEqual(data["leftover_paths"], [src2])
            self.assertEqual(data["linked_count"], 2)
            self.assertIn("入库对账", RENDERER.render(data))


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
