#!/usr/bin/env python3
"""入库对账插件的离线回归测试（无需 MoviePilot 宿主）。

复用全仓契约校验器的宿主 stub，覆盖：
- 按钮 404 的根因（Response 必须是 Pydantic 模型、接口可注册、路径/方法匹配）；
- 三个入口（详情页 / 首页仪表盘 / 只读 API）与零配置表单；
- 对账状态分类的完整分支，尤其是「不可核验 ≠ 已删除」的防误报约束；
- 剧集聚合、合集提示、源残留路径清单与渲染层转义。

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

PRESENT = {"kind": "file", "dev": 1, "ino": 11, "size": 1024, "mtime": 0.0}
MISSING = {"kind": "missing", "dev": None, "ino": None, "size": 0, "mtime": 0.0}


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


def snapshot_for(records, live=(), probed=True, probes=None):
    """用可控的假数据构造快照，不触碰宿主与真实文件系统。"""
    def fake_probe(path, storage):
        if probes is not None:
            return probes(str(path), str(storage))
        return None if storage not in ("local", "", None) else dict(PRESENT)

    with mock.patch.object(AUDITOR, "probe_path", side_effect=fake_probe), mock.patch.object(
        AUDITOR, "probe_seed_hashes", return_value=(set(live), probed)
    ):
        return AUDITOR.build_snapshot(records)


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
        clicks = cc.collect_click_events(self.plugin.get_page())
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

    def test_form_has_no_input_controls(self) -> None:
        form, defaults = self.plugin.get_form()

        def walk(node):
            if isinstance(node, dict):
                yield node
                for value in node.values():
                    yield from walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    yield from walk(value)

        models = [n for n in walk(form) if isinstance(n.get("props"), dict) and n["props"].get("model")]
        self.assertEqual(models, [], "设置页必须零配置，不能出现任何输入控件")
        self.assertEqual(defaults, {})

    def test_page_and_dashboard_structure(self) -> None:
        page = self.plugin.get_page()
        self.assertTrue(any(n.get("component") == "VBtn" for n in page))
        self.assertTrue(any(n.get("html") for n in page))
        cols, attrs, elements = self.plugin.get_dashboard()
        self.assertIn("md", cols)
        self.assertIsInstance(attrs.get("refresh"), int)
        self.assertTrue(elements[0]["html"].startswith("<div"))
        self.assertEqual(self.plugin.get_dashboard_meta()[0]["key"], "audit")

    def test_snapshot_and_refresh_return_envelope(self) -> None:
        snap = self.plugin.api_snapshot()
        self.assertTrue(snap.success)
        self.assertIn("counts", snap.data)
        res = self.plugin.api_refresh()
        self.assertTrue(res.success)
        self.assertIn("html", res.data)
        self.assertIn("counts", res.data)

    def test_enabled_by_default(self) -> None:
        self.assertTrue(self.plugin.get_state())
        self.plugin.init_plugin({"enabled": False})
        self.assertFalse(self.plugin.get_state())

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
        """源已删就是孤儿，不因种子状态未知而降级。"""
        self.assertEqual(AUDITOR.classify(hist(), dict(MISSING), dict(PRESENT), "unknown"), "orphan")

    def test_remote_storage_is_never_reported_as_deleted(self) -> None:
        self.assertIsNone(AUDITOR.probe_path("/remote/a.mkv", "u115"))
        self.assertIsNone(AUDITOR.probe_path("/remote/a.mkv", "rclone"))
        self.assertEqual(AUDITOR.classify(hist(), None, dict(PRESENT), "absent"), "remote")

    def test_unreachable_path_space_is_not_missing(self) -> None:
        with mock.patch.object(AUDITOR.os.path, "isdir", return_value=False):
            self.assertIsNone(
                AUDITOR.probe_path("/unmounted/share/anime/E01.mkv", "local"),
                "整条路径链不可见时必须标为不可核验，不能报已删除",
            )

    def test_existing_parent_allows_missing_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "gone.mkv")
            info = AUDITOR.probe_path(target, "local")
            self.assertIsNotNone(info)
            self.assertEqual(info["kind"], "missing")

    def test_local_storage_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "x")
            open(probe, "w", encoding="utf-8").close()
            self.assertEqual(AUDITOR.probe_path(probe, None)["kind"], "file")

    def test_hardlink_detection(self) -> None:
        self.assertTrue(AUDITOR._hardlink(dict(PRESENT), dict(PRESENT)))
        other = dict(PRESENT, ino=99)
        self.assertFalse(AUDITOR._hardlink(dict(PRESENT), other))
        self.assertFalse(AUDITOR._hardlink(dict(PRESENT, kind="dir"), dict(PRESENT)))


class TestSnapshot(unittest.TestCase):
    def test_aggregates_by_series_and_season(self) -> None:
        records = [
            hist(id=1, episodes="E01"),
            hist(id=2, episodes="E02", download_hash="hash2"),
            hist(id=3, episodes="E03", title="另一部", download_hash="hash3"),
        ]
        data = snapshot_for(records, live={"hash1", "hash2", "hash3"}, probed=True)
        self.assertEqual(data["scanned"], 3)
        self.assertEqual(data["group_total"], 2)
        top = data["groups"][0]
        self.assertEqual(top["total"], 2)
        self.assertEqual(top["counts"]["healthy"], 2)

    def test_leftover_paths_and_counts(self) -> None:
        records = [hist(id=1, src="/dl/E01.mkv"), hist(id=2, src="/dl/E02.mkv", download_hash="hash2")]
        data = snapshot_for(records, live=set(), probed=True)
        self.assertEqual(data["counts"]["leftover"], 2)
        self.assertEqual(len(data["leftover_paths"]), 2)
        self.assertIn("/dl/E01.mkv", data["leftover_paths"])

    def test_seed_not_probed_never_claims_leftover(self) -> None:
        data = snapshot_for([hist()], live=set(), probed=False)
        self.assertEqual(data["counts"]["leftover"], 0)
        self.assertEqual(data["counts"]["pending"], 1)
        self.assertFalse(data["seed_probed"])

    def test_pack_hint_when_collection_and_single_sources_coexist(self) -> None:
        records = [
            hist(id=1, episodes="E01-E12", src="/dl/pack", download_hash="packhash"),
            hist(id=2, episodes="E05", src="/dl/E05.mkv", download_hash="e05hash"),
        ]
        data = snapshot_for(records, live=set(), probed=True)
        top = data["groups"][0]
        self.assertTrue(top["has_pack"])
        self.assertIn("合集", top["pack_hint"])

    def test_remote_and_unverifiable_are_counted_separately(self) -> None:
        def probes(path, storage):
            return None if storage == "u115" else dict(PRESENT)

        records = [hist(id=1, src_storage="u115"), hist(id=2)]
        data = snapshot_for(records, live=set(), probed=True, probes=probes)
        self.assertEqual(data["counts"]["remote"], 1)
        self.assertEqual(data["remote_count"], 1)

    def test_offline_snapshot_has_complete_structure(self) -> None:
        with mock.patch.object(AUDITOR, "_load_queries", return_value=None):
            data = AUDITOR.build_snapshot()
        self.assertTrue(data["offline"])
        self.assertEqual(data["scanned"], 0)
        self.assertIn("counts", data)
        self.assertIn("groups", data)
        self.assertTrue(RENDERER.render(data))


class TestFetcher(unittest.TestCase):
    class _Page:
        def __init__(self, items, total):
            self.items = items
            self.total = total

    class _Queries:
        MAX_QUERY_PAGE_SIZE = 200

        def __init__(self, pages, total):
            self.pages = pages
            self.total = total
            self.calls = []

        def list_transfer_history(self, filters, page):
            self.calls.append((filters, dict(page)))
            return TestFetcher._Page(self.pages.get(page["page"], []), self.total)

    def test_empty_library_is_not_offline(self) -> None:
        queries = self._Queries({}, 0)
        with mock.patch.object(AUDITOR, "_load_queries", return_value=queries):
            items, total, ok = AUDITOR.fetch_histories()
        self.assertEqual(items, [])
        self.assertTrue(ok, "空历史库应显示「暂无记录」，不能被误判成数据源故障")

    def test_invalid_response_is_offline(self) -> None:
        class Broken:
            def list_transfer_history(self, filters, page):
                return None

        with mock.patch.object(AUDITOR, "_load_queries", return_value=Broken()):
            items, total, ok = AUDITOR.fetch_histories()
        self.assertFalse(ok)

    def test_paginates_until_last_page(self) -> None:
        first = [hist(id=i) for i in range(200)]
        second = [hist(id=200 + i) for i in range(3)]
        queries = self._Queries({1: first, 2: second}, 203)
        with mock.patch.object(AUDITOR, "_load_queries", return_value=queries):
            items, total, ok = AUDITOR.fetch_histories()
        self.assertTrue(ok)
        self.assertEqual(total, 203)
        self.assertEqual(len(items), 203)
        self.assertEqual(len(queries.calls), 2)


class TestSeedProbe(unittest.TestCase):
    def test_failed_downloader_query_degrades_to_unverified(self) -> None:
        class BrokenInstance:
            def list_torrents(self, **kwargs):
                raise RuntimeError("downloader offline")

        class Service:
            instance = BrokenInstance()

        with mock.patch.object(AUDITOR, "_load_downloader_services", return_value=[Service()]):
            live, probed = AUDITOR.probe_seed_hashes(["hash1"])
        self.assertFalse(probed)
        self.assertEqual(live, set())

    def test_live_hash_is_detected(self) -> None:
        class Instance:
            def list_torrents(self, **kwargs):
                return [{"hash": "HASH1"}]

        class Service:
            instance = Instance()

        with mock.patch.object(AUDITOR, "_load_downloader_services", return_value=[Service()]):
            live, probed = AUDITOR.probe_seed_hashes(["hash1"])
        self.assertTrue(probed)
        self.assertEqual(live, {"hash1"})


class TestRenderer(unittest.TestCase):
    def setUp(self) -> None:
        self.data = snapshot_for(
            [hist(id=1, download_hash="h1"), hist(id=2, download_hash="h2")],
            live=set(), probed=True,
        )

    def test_escapes_dynamic_text(self) -> None:
        self.data["issues"] = [dict(self.data["issues"][0], title="<script>alert(1)</script>")]
        html = RENDERER.render(self.data)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_full_view_has_detail_table_and_path_list(self) -> None:
        html = RENDERER.render(self.data)
        self.assertIn("需关注明细", html)
        self.assertIn("源残留路径清单", html)
        self.assertIn("只读报告", html)

    def test_compact_view_omits_detail_table(self) -> None:
        html = RENDERER.render(self.data, compact=True)
        self.assertNotIn("需关注明细", html)
        self.assertNotIn("源残留路径清单", html)
        self.assertIn("按剧集 / 季聚合", html)

    def test_offline_placeholder(self) -> None:
        offline = dict(self.data, offline=True)
        self.assertIn("离线预览", RENDERER.render(offline))

    def test_never_renders_delete_action(self) -> None:
        html = RENDERER.render(self.data)
        self.assertNotIn("删除文件", html.replace("不删除任何文件", ""))


class TestEndToEnd(unittest.TestCase):
    """用真实临时目录与真实硬链接复现 PT 连载番剧删种后的五种状态。"""

    def test_pt_anime_scenario_with_real_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            dl = os.path.join(root, "download")
            lib = os.path.join(root, "media")
            os.makedirs(dl)
            os.makedirs(lib)

            def make(name, remove_src=False):
                src = os.path.join(dl, name)
                dst = os.path.join(lib, name)
                with open(src, "wb") as fh:
                    fh.write(b"x" * 128)
                os.link(src, dst)
                if remove_src:
                    os.remove(src)
                return src, dst

            # 合集打包后删种：源文件已删，媒体库硬链接仍在
            src1, dst1 = make("E01.mkv", remove_src=True)
            # 删了种子但源文件忘了删，最该被找出来的残留
            src2, dst2 = make("E02.mkv")
            # 种子还在，正常做种
            src3, dst3 = make("E03.mkv")
            # 库文件被移走
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
            with mock.patch.object(AUDITOR, "probe_seed_hashes", return_value=({"h3"}, True)):
                data = AUDITOR.build_snapshot(records)

            self.assertEqual(data["counts"]["orphan"], 1)
            self.assertEqual(data["counts"]["leftover"], 1)
            self.assertEqual(data["counts"]["healthy"], 1)
            self.assertEqual(data["counts"]["missing"], 1)
            self.assertEqual(data["counts"]["remote"], 1)
            self.assertEqual(data["leftover_paths"], [src2])
            self.assertEqual(data["linked_count"], 2, "E02 与 E03 源库仍是同一 inode")
            self.assertEqual(data["groups"][0]["total"], 5)
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
