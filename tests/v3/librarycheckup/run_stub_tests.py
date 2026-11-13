#!/usr/bin/env python3
"""媒体库体检 A/B/C 三视图离线回归测试（无需 MoviePilot 宿主）。

覆盖：
- 按钮 404 根因（Pydantic Response、接口可注册、路径/方法匹配、三视图按钮）；
- 零配置表单与三视图切换、首页仪表盘三元组；
- A 整理预演：目录自动推断、目标冲突、跨盘退化、重复入库、全量不截断、只读；
- B 入库账本：真硬链接 / 退化复制 / 孤儿 / 应删未删 / 双端已无 / 未核验 /
  外部登记缺失全分支，inode 与设备号核验、全量记录不截断；
- C 跨服对账：三家条目归一化、外部 ID 对齐、缺集/仅一家/多版本、
  连接失败标未核验且不参与缺集判定、不泄露密钥；
- 渲染：HTML 转义、全量章节、深浅主题通用色、固定脚本安全、日志可复制；
- 端到端：真实临时目录 + mock 宿主数据源跑通 collect()，验证零写入。

用法：python3 tests/v3/librarycheckup/run_stub_tests.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]  # tests/v3/librarycheckup -> 仓库根
PLUGIN_DIR = ROOT / "plugins.v3" / "librarycheckup"
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
common = MODULE.common
preview = MODULE.preview
ledger = MODULE.ledger
parity = MODULE.parity
collector = MODULE.collector
renderer = MODULE.renderer


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

def clicks(node):
    if isinstance(node, dict):
        ev = node.get("events")
        if isinstance(ev, dict) and isinstance(ev.get("click"), dict):
            yield ev["click"]
        for value in node.values():
            yield from clicks(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from clicks(value)


def hist(**kwargs):
    base = {
        "id": 1, "src": "/download/anime/E01.mkv", "src_storage": "local",
        "dest": "/media/anime/某番 S01E01.mkv", "dest_storage": "local",
        "mode": "link", "title": "某番", "year": "2026", "seasons": "S01",
        "episodes": "E01", "download_hash": "hash1",
        "date": "2026-11-01 12:00:00", "status": True,
    }
    base.update(kwargs)
    return base


PRESENT = {"state": "present", "dev": 1, "ino": 11, "size": 1024, "mtime": 0.0}
MISSING = {"state": "missing", "dev": None, "ino": None, "size": 0, "mtime": 0.0}
UNKNOWN = {"state": "unknown", "dev": None, "ino": None, "size": 0, "mtime": 0.0}


def fake_probe(states=None, devs=None):
    """states: {path: 'present'|'missing'|'unknown'}；devs: {path: dev}。"""
    states = states or {}
    devs = devs or {}

    def fn(path, storage=""):
        key = str(path or "")
        if storage and str(storage).lower() not in ("local", "", "none"):
            return dict(UNKNOWN)
        state = states.get(key, "missing")
        if state == "present":
            return {"state": "present", "dev": devs.get(key, 1), "ino": 11,
                    "size": 1024, "mtime": 0.0}
        if state == "unknown":
            return dict(UNKNOWN)
        return dict(MISSING)
    return fn


def fake_walk(mapping):
    def fn(root, *args, **kwargs):
        return list(mapping.get(str(root), [])), False
    return fn


def server(name, stype="emby", url="http://127.0.0.1:8096", apikey="SECRET"):
    return {"name": name, "type": stype, "url": url, "apikey": apikey}


def emby_item(name, item_type="Movie", year=2026, tmdb="100", season=None, episode=None, series=None):
    return {
        "Name": name, "Type": item_type, "ProductionYear": year,
        "ProviderIds": {"Tmdb": tmdb} if tmdb else {},
        "ParentIndexNumber": season, "IndexNumber": episode,
        "SeriesName": series, "Path": f"/media/{name}",
    }


# ----------------------------------------------------------------------
# 契约与交互
# ----------------------------------------------------------------------

class TestContract(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()

    def test_response_is_pydantic_model(self):
        from pydantic import BaseModel
        self.assertTrue(issubclass(MODULE.Response, BaseModel))

    def test_plugin_id_matches_directory(self):
        self.assertEqual(PLUGIN_ID, "LibraryCheckup")
        self.assertEqual(PLUGIN_DIR.name, "librarycheckup")

    def test_api_routes_are_registerable(self):
        from fastapi import FastAPI
        app = FastAPI()
        for api in self.plugin.get_api():
            kwargs = dict(api)
            kwargs.pop("endpoint", None)
            kwargs["endpoint"] = api["endpoint"]
            kwargs.pop("auth", None)
            app.add_api_route(**{k: v for k, v in kwargs.items()
                                 if k in ("path", "endpoint", "methods", "summary")})
        self.assertEqual(len(self.plugin.get_api()), 5)

    def test_every_api_declares_bear_auth(self):
        self.assertTrue(all(a.get("auth") == "bear" for a in self.plugin.get_api()))

    def test_buttons_use_correct_prefix_and_method(self):
        declared = {a["path"].lstrip("/"): set(a["methods"]) for a in self.plugin.get_api()}
        found = list(clicks(self.plugin.get_page()))
        self.assertTrue(found)
        for click in found:
            self.assertTrue(click["api"].startswith("plugin/LibraryCheckup/"), click)
            action = click["api"].rsplit("/", 1)[-1]
            self.assertIn(action, declared, click)
            self.assertIn(click["method"].upper(), declared[action], click)

    def test_form_is_zero_config_with_single_optional_input(self):
        form, defaults = self.plugin.get_form()
        models = []

        def walk(node):
            if isinstance(node, dict):
                if "model" in (node.get("props") or {}):
                    models.append(node["props"]["model"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(form)
        self.assertEqual(models, ["enabled", "preview_dir"])
        self.assertEqual(defaults["preview_dir"], "")
        self.assertTrue(defaults["enabled"])

    def test_dashboard_triplet(self):
        self.assertEqual(self.plugin.get_dashboard_meta(),
                         [{"key": "checkup", "name": "媒体库体检"}])
        cols, attrs, elements = self.plugin.get_dashboard("checkup")
        self.assertIn("md", cols)
        self.assertEqual(attrs["refresh"], 120)
        self.assertEqual(len(elements), 1)
        self.assertIn("媒体库体检", elements[0]["html"])

    def test_all_three_views_render_and_have_switch_buttons(self):
        for view in ("preview", "ledger", "parity"):
            self.plugin.save_data("view", view)
            page = self.plugin.get_page()
            apis = [c["api"] for c in clicks(page)]
            self.assertIn("plugin/LibraryCheckup/view", apis)
            self.assertTrue(any("html" in e for e in page))
        self.plugin.save_data("view", "preview")

    def test_view_switch_validates_and_persists(self):
        ok = self.plugin.api_view({"view": "parity"})
        self.assertTrue(ok.success)
        self.assertEqual(self.plugin.get_data("view"), "parity")
        bad = self.plugin.api_view({"view": "nope"})
        self.assertFalse(bad.success)
        self.assertEqual(self.plugin.get_data("view"), "parity")

    def test_all_api_endpoints_return_envelope(self):
        with mock.patch.object(common, "fetch_histories", return_value=([], False, "离线")), \
             mock.patch.object(common, "fetch_servers", return_value=([], False, "离线")):
            for response in (
                self.plugin.api_snapshot(),
                self.plugin.api_log(),
                self.plugin.api_refresh(),
                self.plugin.api_preview(),
            ):
                self.assertTrue(response.success)
                self.assertIsNotNone(response.data)

    def test_readonly_flag_on_every_view_and_snapshot(self):
        with mock.patch.object(common, "fetch_histories", return_value=([], False, "离线")), \
             mock.patch.object(common, "fetch_servers", return_value=([], False, "离线")):
            snap = collector.collect(force=True)
        self.assertTrue(snap["readonly"])
        for key in ("preview", "ledger", "parity"):
            self.assertTrue(snap[key]["readonly"], key)


# ----------------------------------------------------------------------
# 公共只读工具
# ----------------------------------------------------------------------

class TestCommon(unittest.TestCase):
    def test_probe_distinguishes_missing_from_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = os.path.join(tmp, "a.mkv")
            pathlib.Path(existing).write_bytes(b"x")
            self.assertEqual(common.probe(existing)["state"], "present")
            self.assertEqual(common.probe(os.path.join(tmp, "gone.mkv"))["state"], "missing")
            self.assertEqual(common.probe("/definitely/not/mapped/x.mkv")["state"], "unknown")
            self.assertEqual(common.probe("/x", "alipan")["state"], "unknown")

    def test_parse_filename_tv_and_movie(self):
        tv = common.parse_filename("[Group] Some Anime S01E03 1080p x264.mkv")
        self.assertEqual((tv["season"], tv["episode"], tv["kind"]), ("S01", "E03", "tv"))
        self.assertIn("Some Anime", tv["title"])
        movie = common.parse_filename("Big.Movie.2024.2160p.mkv")
        self.assertEqual(movie["year"], "2024")
        self.assertEqual(movie["kind"], "movie")

    def test_normalize_title_ignores_tags_and_year(self):
        self.assertEqual(common.normalize_title("某番 (2026) [1080p]"),
                         common.normalize_title("某番 2026 1080p"))

    def test_cfg_attr_and_as_dict_cover_models_and_dicts(self):
        self.assertEqual(common.cfg_attr({"a": 1, "b": 0}, "b", "a"), 1)
        self.assertEqual(common.as_dict({"x": 1})["x"], 1)

    def test_infer_dirs_from_history(self):
        dirs = common.infer_dirs([
            hist(src="/downloads/a/E01.mkv", dest="/media/tv/A/E01.mkv"),
            hist(src="/downloads/a/E02.mkv", dest="/media/tv/A/E02.mkv"),
        ])
        self.assertIn("/downloads/a", dirs["download"])
        self.assertIn("/media/tv/A", dirs["library"])

    def test_walk_files_skips_symlinks_and_system_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "@eaDir"))
            pathlib.Path(os.path.join(tmp, "a.mkv")).write_bytes(b"a")
            pathlib.Path(os.path.join(tmp, "@eaDir", "b.mkv")).write_bytes(b"b")
            pathlib.Path(os.path.join(tmp, "note.txt")).write_bytes(b"c")
            os.symlink(os.path.join(tmp, "a.mkv"), os.path.join(tmp, "link.mkv"))
            found, cut = common.walk_files(tmp)
            self.assertEqual([os.path.basename(p) for p in found], ["a.mkv"])
            self.assertFalse(cut)

    def test_fetch_histories_offline_degrades(self):
        with mock.patch.dict(sys.modules, {"app.sdk": None}):
            records, ok, reason = common.fetch_histories()
        self.assertFalse(ok)
        self.assertEqual(records, [])
        self.assertTrue(reason)

    def test_http_get_is_mocked_and_parses_json(self):
        class Resp:
            def read(self):
                return b'{"Items":[{"Name":"X"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(common.urllib.request, "urlopen", return_value=Resp()):
            result = common.http_get("http://x/Items", {"Limit": 10}, {})
        self.assertEqual(result["Items"][0]["Name"], "X")


# ----------------------------------------------------------------------
# A 整理预演沙盘
# ----------------------------------------------------------------------

class TestPreview(unittest.TestCase):
    def test_no_roots_gives_clear_reason(self):
        result = preview.build_preview({}, [], {"download": [], "library": []})
        self.assertFalse(result["ok"])
        self.assertIn("没有可预演的目录", result["reason"])

    def test_auto_roots_from_history_and_full_list(self):
        paths = [f"/downloads/anime/E{i:02d}.mkv" for i in range(1, 41)]
        dirs = {"download": ["/downloads/anime"], "library": ["/media/anime"]}
        result = preview.build_preview({}, [hist()], dirs,
                                       probe_fn=fake_probe({p: "present" for p in paths},
                                                           {"/media/anime": 1}),
                                       walk_fn=fake_walk({"/downloads/anime": paths}))
        self.assertTrue(result["roots_auto"])
        self.assertEqual(result["summary"]["total"], 40)
        self.assertEqual(len(result["files"]), 40)
        self.assertFalse(result["truncated"])

    def test_target_conflict_is_high_risk(self):
        target = "/media/anime/E01.mkv"
        states = {"/downloads/E01.mkv": "present", target: "present", "/media/anime": "present"}
        result = preview.build_preview({"preview_dir": "/downloads"}, [],
                                       {"download": [], "library": ["/media/anime"]},
                                       probe_fn=fake_probe(states),
                                       walk_fn=fake_walk({"/downloads": ["/downloads/E01.mkv"]}))
        row = result["files"][0]
        self.assertEqual(row["level"], "high")
        self.assertTrue(row["target_exists"])
        self.assertTrue(any("覆盖" in x for x in row["risks"]))
        self.assertEqual(result["summary"]["conflict"], 1)

    def test_cross_device_detects_hardlink_degradation(self):
        states = {"/downloads/E01.mkv": "present", "/media/anime": "present"}
        devs = {"/downloads/E01.mkv": 1, "/media/anime": 2}
        result = preview.build_preview({"preview_dir": "/downloads"}, [],
                                       {"download": [], "library": ["/media/anime"]},
                                       probe_fn=fake_probe(states, devs),
                                       walk_fn=fake_walk({"/downloads": ["/downloads/E01.mkv"]}))
        self.assertTrue(result["files"][0]["cross_device"])
        self.assertTrue(any("退化为复制" in x for x in result["files"][0]["risks"]))

    def test_duplicate_history_is_flagged(self):
        path = "/downloads/E01.mkv"
        result = preview.build_preview({"preview_dir": "/downloads"},
                                       [hist(src=path)],
                                       {"download": [], "library": ["/media/anime"]},
                                       probe_fn=fake_probe({path: "present", "/media/anime": "present"}),
                                       walk_fn=fake_walk({"/downloads": [path]}))
        self.assertTrue(any("整理记录" in x for x in result["files"][0]["risks"]))
        self.assertEqual(result["summary"]["duplicate"], 1)

    def test_missing_target_dir_is_unverified_not_invented(self):
        path = "/downloads/E01.mkv"
        result = preview.build_preview({"preview_dir": "/downloads"}, [],
                                       {"download": [], "library": []},
                                       probe_fn=fake_probe({path: "present"}),
                                       walk_fn=fake_walk({"/downloads": [path]}))
        self.assertEqual(result["files"][0]["predicted_path"], "未核验")
        self.assertTrue(any("无法推断目标目录" in x for x in result["files"][0]["risks"]))

    def test_real_directory_scan_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "downloads")
            dst = os.path.join(tmp, "media")
            os.makedirs(src)
            os.makedirs(dst)
            path = os.path.join(src, "Show.S01E01.mkv")
            pathlib.Path(path).write_bytes(b"source")
            before = sorted(os.listdir(src)) + sorted(os.listdir(dst))
            before_stat = os.stat(path).st_mtime_ns
            result = preview.build_preview(
                {"preview_dir": src}, [hist(src=path, dest=os.path.join(dst, "Show.S01E01.mkv"))],
                {"download": [src], "library": [dst]})
            self.assertEqual(result["summary"]["total"], 1)
            self.assertTrue(result["readonly"])
            self.assertEqual(sorted(os.listdir(src)) + sorted(os.listdir(dst)), before)
            self.assertEqual(os.stat(path).st_mtime_ns, before_stat)
            self.assertFalse(os.path.exists(os.path.join(dst, "Show.S01E01.mkv")))

    def test_mode_hint_from_history(self):
        result = preview.build_preview({}, [hist(mode="link")],
                                       {"download": [], "library": []})
        self.assertEqual(result["mode_hint"], "硬链接")

    def test_truncation_is_reported(self):
        paths = [f"/d/E{i}.mkv" for i in range(10)]
        result = preview.build_preview({"preview_dir": "/d"}, [],
                                       {"download": [], "library": []},
                                       probe_fn=fake_probe(),
                                       walk_fn=lambda root, *a, **k: (paths, True))
        self.assertTrue(result["truncated"])
        self.assertIn("max_files", result)


# ----------------------------------------------------------------------
# B 入库账本与链接拓扑
# ----------------------------------------------------------------------

class TestLedger(unittest.TestCase):
    def test_classify_all_branches(self):
        link = {"state": "present", "dev": 1, "ino": 7}
        copy = {"state": "present", "dev": 1, "ino": 8}
        cases = [
            ("link", link, {"state": "present", "dev": 1, "ino": 7}, "hardlink_ok"),
            ("link", link, copy, "degraded_copy"),
            ("link", MISSING, link, "orphan"),
            ("link", link, MISSING, "src_leftover"),
            ("link", MISSING, MISSING, "both_missing"),
            ("link", UNKNOWN, link, "unverified"),
            ("copy", link, copy, "copy_ok"),
        ]
        for mode, src, dest, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(ledger.classify(dict(src), dict(dest), mode), expected)

    def test_real_hardlink_is_verified_by_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.mkv")
            dest = os.path.join(tmp, "dest.mkv")
            pathlib.Path(src).write_bytes(b"x")
            os.link(src, dest)
            result = ledger.build_ledger(
                histories=[hist(src=src, dest=dest)],
                dirs={"download": [], "library": []})
            row = result["records"][0]
            self.assertEqual(row["status"], "hardlink_ok")
            self.assertTrue(row["same_inode"])

    def test_real_copy_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.mkv")
            dest = os.path.join(tmp, "dest.mkv")
            pathlib.Path(src).write_bytes(b"x")
            pathlib.Path(dest).write_bytes(b"x")
            result = ledger.build_ledger(
                histories=[hist(src=src, dest=dest)],
                dirs={"download": [], "library": []})
            self.assertEqual(result["records"][0]["status"], "degraded_copy")

    def test_remote_storage_is_unverified_not_deleted(self):
        result = ledger.build_ledger(
            histories=[hist(src_storage="alipan")],
            dirs={"download": [], "library": []},
            probe_fn=fake_probe())
        self.assertEqual(result["records"][0]["status"], "unverified")
        self.assertTrue(any("不把查不到当成已删除" in x for x in result["records"][0]["notes"]))

    def test_external_gap_detection(self):
        lib = "/media/anime"
        external = f"{lib}/Manual.S01E09.mkv"
        result = ledger.build_ledger(
            histories=[hist(dest=f"{lib}/E01.mkv")],
            dirs={"download": [], "library": [lib]},
            probe_fn=fake_probe(),
            walk_fn=fake_walk({lib: [external]}))
        self.assertEqual(result["summary"]["external_gap"], 1)
        self.assertEqual(result["external"][0]["path"], external)

    def test_full_records_not_truncated_and_groups_aggregate(self):
        records = [hist(id=i, src=f"/d/E{i:02d}.mkv", dest=f"/m/E{i:02d}.mkv")
                   for i in range(60)]
        result = ledger.build_ledger(histories=records,
                                     dirs={"download": [], "library": []},
                                     probe_fn=fake_probe())
        self.assertEqual(len(result["records"]), 60)
        self.assertEqual(result["summary"]["total"], 60)
        self.assertTrue(result["groups"])

    def test_ledger_never_touches_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "s.mkv")
            pathlib.Path(src).write_bytes(b"x")
            result = ledger.build_ledger(
                histories=[hist(src=src, dest=os.path.join(tmp, "d.mkv"))],
                dirs={"download": [], "library": []})
            self.assertTrue(result["readonly"])
            self.assertTrue(os.path.exists(src))
            self.assertFalse(os.path.exists(os.path.join(tmp, "d.mkv")))


# ----------------------------------------------------------------------
# C 三家一致性对账
# ----------------------------------------------------------------------

class TestParity(unittest.TestCase):
    def test_normalize_emby_movie_and_episode(self):
        movie = parity.normalize_item(emby_item("Big Movie"), "emby")
        self.assertEqual(movie["media_key"], "tmdb:100")
        self.assertEqual(movie["kind"], "movie")
        episode = parity.normalize_item(
            emby_item("E01", "Episode", tmdb="200", season=1, episode=1, series="Some Anime"), "jellyfin")
        self.assertEqual((episode["season"], episode["episode"]), ("S01", "E01"))
        self.assertEqual(episode["title"], "Some Anime")

    def test_normalize_plex_episode_and_guid(self):
        raw = {"type": "episode", "grandparentTitle": "Some Anime", "title": "Pilot",
               "parentIndex": 1, "index": 1, "year": 2026,
               "Guid": [{"id": "com.plexapp.agents.themoviedb://555?lang=zh"}]}
        item = parity.normalize_item(raw, "plex")
        self.assertEqual(item["media_key"], "tmdb:555")
        self.assertEqual((item["season"], item["episode"]), ("S01", "E01"))

    def test_unknown_type_is_skipped(self):
        self.assertIsNone(parity.normalize_item({"Type": "MusicVideo", "Name": "X"}, "emby"))

    def test_provider_identity_aligns_across_servers(self):
        payload = {"Items": [emby_item("Movie A", tmdb="900")]}
        result = parity.build_parity(
            servers=[server("E", "emby"), server("J", "jellyfin"), server("P", "plex")],
            http_get=lambda url, params=None, headers=None: payload)
        # Plex 走 sections，这里用同一 payload 会拿到 0 条，但仍应成功连接
        self.assertEqual(result["summary"]["total"], 1)
        self.assertGreaterEqual(result["summary"]["servers_ok"], 2)

    def test_missing_in_some_and_only_one(self):
        def get(url, params=None, headers=None):
            if url.startswith("http://jellyfin"):
                return {"Items": [emby_item("Movie A", tmdb="1")]}
            return {"Items": [emby_item("Movie A", tmdb="1"),
                              emby_item("Movie B", tmdb="2")]}

        result = parity.build_parity(
            servers=[server("E", "emby"),
                     server("J", "jellyfin", url="http://jellyfin:8096")], http_get=get)
        cats = {r["title"]: r["category"] for r in result["rows"]}
        self.assertEqual(cats.get("Movie A"), "consistent")
        self.assertEqual(cats.get("Movie B"), "only_one")
        self.assertEqual(result["summary"]["only_one"], 1)
        self.assertEqual(result["summary"]["servers_ok"], 2)

    def test_multi_version_is_reported(self):
        def get(url, params=None, headers=None):
            return {"Items": [emby_item("Movie A", tmdb="1"),
                              emby_item("Movie A 4K", tmdb="1")]}
        result = parity.build_parity(
            servers=[server("E", "emby"), server("J", "jellyfin")], http_get=get)
        self.assertEqual(result["summary"]["multi_version"], 1)

    def test_failed_server_is_unverified_and_not_counted_as_missing(self):
        def get(url, params=None, headers=None):
            if "J" in str(headers.get("X-Emby-Token")):
                raise OSError("boom")
            return {"Items": [emby_item("Movie A", tmdb="1")]}

        result = parity.build_parity(
            servers=[server("E", "emby"), server("J", "jellyfin", apikey="J-SECRET")],
            http_get=get)
        self.assertEqual(result["summary"]["servers_failed"], 1)
        failed = [s for s in result["servers"] if not s["ok"]][0]
        self.assertIn("未核验", result["note"])
        self.assertNotIn("J-SECRET", str(result))
        # 只有 1 家成功：不做跨服缺集判定
        self.assertEqual(result["summary"]["only_one"], 0)
        self.assertEqual(result["summary"]["missing_in_some"], 0)
        self.assertTrue(all(r["category"] == "single_source" for r in result["rows"]))

    def test_no_servers_reports_reason(self):
        result = parity.build_parity(servers=[], servers_ok=False,
                                     servers_reason="未发现已连接实例")
        self.assertFalse(result["ok"])
        self.assertIn("未发现已连接实例", result["reason"])

    def test_apikey_never_leaks_into_report(self):
        result = parity.build_parity(
            servers=[server("E", "emby", apikey="TOP-SECRET")],
            http_get=lambda url, params=None, headers=None: {"Items": []})
        self.assertNotIn("TOP-SECRET", str(result))
        self.assertNotIn("TOP-SECRET", renderer.render({"preview": {}, "ledger": {},
                                                         "parity": result, "view": "parity"}))

    def test_jellyfin_falls_back_to_items_path(self):
        calls = []

        def get(url, params=None, headers=None):
            calls.append(url)
            if url.endswith("/emby/Items"):
                raise OSError("not emby")
            return {"Items": [emby_item("Movie A")]}

        items, ok, reason = parity.fetch_items(server("J", "jellyfin"), http_get=get)
        self.assertTrue(ok)
        self.assertEqual(len(items), 1)
        self.assertTrue(any(u.endswith("/Items") for u in calls))

    def test_fetch_items_missing_credentials_degrades(self):
        items, ok, reason = parity.fetch_items({"type": "emby", "url": "", "apikey": ""})
        self.assertFalse(ok)
        self.assertIn("缺少", reason)


# ----------------------------------------------------------------------
# 渲染与聚合
# ----------------------------------------------------------------------

def full_snapshot(view="preview"):
    a = preview.build_preview({"preview_dir": "/d"}, [],
                              {"download": [], "library": ["/m"]},
                              probe_fn=fake_probe(),
                              walk_fn=fake_walk({"/d": ["/d/Show.S01E01.<img src=x>.mkv"]}))
    b = ledger.build_ledger(histories=[hist(title="<script>alert(1)</script>")],
                            dirs={"download": [], "library": []},
                            probe_fn=fake_probe())
    c = parity.build_parity(servers=[server("E", "emby")],
                            http_get=lambda *a, **k: {"Items": [emby_item("Movie A")]})
    return {
        "generated_at": "2026-11-12 01:00:00", "view": view,
        "config": {"enabled": True, "preview_dir": "/d"},
        "readonly": True, "histories": {"total": 1, "ok": True, "reason": ""},
        "dirs": {"download": ["/d"], "library": ["/m"]}, "servers_found": 1,
        "preview": a, "ledger": b, "parity": c,
        "log": ["[2026-11-12 01:00:00] 开始只读体检"],
        "notes": ["本插件全程只读"],
    }


class TestRenderer(unittest.TestCase):
    def test_dynamic_text_is_escaped(self):
        html_a = renderer.render(full_snapshot("preview"))
        html_b = renderer.render(full_snapshot("ledger"))
        self.assertNotIn("<img src=x>", html_a)
        self.assertIn("&lt;img", html_a)
        self.assertNotIn("<script>alert(1)</script>", html_b)
        self.assertIn("&lt;script&gt;", html_b)

    def test_all_three_views_render_full_tables(self):
        for view in ("preview", "ledger", "parity"):
            with self.subTest(view=view):
                out = renderer.render(full_snapshot(view))
                self.assertIn("lc-table", out)
                self.assertIn("全量", out)
                self.assertIn("全程只读", out)

    def test_compact_view_has_no_tables(self):
        out = renderer.render(full_snapshot(), compact=True)
        self.assertNotIn("<table", out)
        self.assertNotIn("lc-section", out.split("</style>")[-1])
        self.assertIn("媒体库体检", out)

    def test_log_block_and_copy_script_are_present(self):
        out = renderer.render(full_snapshot())
        self.assertIn("一键复制", out)
        self.assertIn("开始只读体检", out)
        self.assertIn("lc-log", out)

    def test_inline_scripts_have_no_attribute_breakers(self):
        self.assertNotIn('"', renderer._COPY_JS)
        self.assertNotIn("&", renderer._COPY_JS)
        self.assertNotIn('"', renderer._FILTER_JS)
        self.assertNotIn("&", renderer._FILTER_JS)

    def test_theme_uses_inherit_not_hardcoded_text_color(self):
        self.assertIn("color:inherit", renderer._STYLE)
        self.assertIn("rgba(127,127,127", renderer._STYLE)

    def test_readonly_note_in_every_view(self):
        for view in ("preview", "ledger", "parity"):
            self.assertIn("只读", renderer.render(full_snapshot(view)))


class TestCollector(unittest.TestCase):
    def test_parse_config_safe_defaults(self):
        cfg = collector.parse_config({"preview_dir": "  /downloads  ", "junk": 1})
        self.assertEqual(cfg, {"enabled": True, "preview_dir": "/downloads"})
        self.assertEqual(collector.parse_config(None)["preview_dir"], "")

    def test_cache_and_invalidate(self):
        collector.invalidate()
        with mock.patch.object(common, "fetch_histories", return_value=([], True, "")) as fh, \
             mock.patch.object(common, "fetch_servers", return_value=([], True, "")):
            collector.collect(force=True)
            collector.collect()
            self.assertEqual(fh.call_count, 1)
            collector.collect(force=True)
            self.assertEqual(fh.call_count, 2)

    def test_config_change_invalidates_cache(self):
        collector.invalidate()
        with mock.patch.object(common, "fetch_histories", return_value=([], True, "")) as fh, \
             mock.patch.object(common, "fetch_servers", return_value=([], True, "")):
            collector.collect(config={"preview_dir": ""})
            collector.collect(config={"preview_dir": "/other"})
            self.assertEqual(fh.call_count, 2)

    def test_snapshot_has_all_three_views_and_log(self):
        with mock.patch.object(common, "fetch_histories", return_value=([], True, "")), \
             mock.patch.object(common, "fetch_servers", return_value=([], True, "")):
            snap = collector.collect(force=True)
        for key in ("preview", "ledger", "parity"):
            self.assertIn(key, snap)
            self.assertIn("summary", snap[key])
        self.assertIsInstance(snap["log"], list)
        self.assertTrue(snap["notes"])

    def test_end_to_end_real_directory_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "downloads")
            lib = os.path.join(tmp, "media")
            os.makedirs(src)
            os.makedirs(lib)
            spath = os.path.join(src, "Show.S01E01.mkv")
            dpath = os.path.join(lib, "Show.S01E01.mkv")
            pathlib.Path(spath).write_bytes(b"source")
            os.link(spath, dpath)
            record = hist(src=spath, dest=dpath)
            with mock.patch.object(common, "fetch_histories", return_value=([record], True, "")), \
                 mock.patch.object(common, "fetch_servers", return_value=([], True, "")), \
                 mock.patch.object(common, "infer_dirs",
                                   return_value={"download": [src], "library": [lib]}):
                snap = collector.collect(force=True, config={"preview_dir": src})
            self.assertEqual(snap["ledger"]["records"][0]["status"], "hardlink_ok")
            self.assertEqual(snap["preview"]["summary"]["total"], 1)
            self.assertTrue(os.path.exists(spath))
            self.assertTrue(os.path.exists(dpath))
            self.assertEqual(snap["histories"]["total"], 1)


class TestHostSignature(unittest.TestCase):
    """离线 stub 的 get_data(key, default) 与宿主真实签名不同，单独锁住切页行为。

    宿主 app/sdk/plugin/base.py 的定义是 get_data(key=None, plugin_id=None)，
    第二个参数是**插件 ID**。曾误写成 get_data("view", "preview")，
    等于去读一个叫 preview 的插件的数据，结果永远读不到、页面一直停在整理预演。
    """

    def _plugin_with_host_api(self):
        store = {}
        plugin = PLUGIN_CLS()

        def host_save_data(key, value, plugin_id=None):
            self.assertIn(plugin_id, (None, "LibraryCheckup"))
            store[key] = value

        def host_get_data(key=None, plugin_id=None):
            # 模拟宿主：传了别的 plugin_id 就是查别人的数据，一律读不到
            if plugin_id and plugin_id != "LibraryCheckup":
                return None
            return store.get(key)

        plugin.save_data = host_save_data
        plugin.get_data = host_get_data
        return plugin, store

    def test_old_wrong_call_would_read_another_plugin(self):
        plugin, store = self._plugin_with_host_api()
        store["view"] = "parity"
        self.assertEqual(plugin.get_data("view"), "parity")
        self.assertIsNone(plugin.get_data("view", "preview"))

    def test_switching_pages_works_with_host_signature(self):
        plugin, _ = self._plugin_with_host_api()
        titles = {
            "preview": "整理预演明细（全量）",
            "ledger": "src ↔ dest 链接拓扑（全量）",
            "parity": "媒体服务器连接状态",
        }
        with mock.patch.object(common, "fetch_histories", return_value=([], True, "")), \
             mock.patch.object(common, "fetch_servers", return_value=([], True, "")):
            for view, marker in titles.items():
                with self.subTest(view=view):
                    response = plugin.api_view({"view": view})
                    self.assertTrue(response.success)
                    self.assertEqual(plugin._view(), view)
                    html = "".join(str(e.get("html") or "") for e in plugin.get_page())
                    self.assertIn(marker, html, "页面没有切到对应的内容")
                    for other, other_marker in titles.items():
                        if other != view:
                            self.assertNotIn(other_marker, html)

    def test_instance_state_is_a_fallback_when_persistence_fails(self):
        plugin = PLUGIN_CLS()
        plugin.save_data = lambda *a, **k: (_ for _ in ()).throw(OSError("db down"))
        self.assertTrue(plugin.api_view({"view": "ledger"}).success)
        self.assertEqual(plugin._view(), "ledger")


class TestWording(unittest.TestCase):
    def test_button_labels_are_plain_and_short(self):
        labels = [text for _, text, _ in MODULE.VIEW_BUTTONS]
        self.assertEqual(labels, ["整理预演", "入库账本", "媒体库对比"])
        for label in labels:
            self.assertNotRegex(label, r"[A-Za-z]")
            self.assertLessEqual(len(label), 6)

    def test_no_letter_stages_in_user_facing_text(self):
        plugin = PLUGIN_CLS()
        with mock.patch.object(common, "fetch_histories", return_value=([], True, "")), \
             mock.patch.object(common, "fetch_servers", return_value=([], True, "")):
            html = "".join(str(e.get("html") or "") for e in plugin.get_page())
        form, _ = plugin.get_form()
        blob = "\n".join((plugin.plugin_desc, html, str(form),
                          json.dumps(json.loads((ROOT / "package.v3.json").read_text(
                              encoding="utf-8"))["LibraryCheckup"], ensure_ascii=False),
                          (ROOT / "README.md").read_text(encoding="utf-8")))
        for bad in ("A → B", "A / B / C", "A · 整理", "B · 入库", "C · ", "三步走"):
            self.assertNotIn(bad, blob, f"面向用户的文案仍含阶段式表述：{bad}")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"合计 {total} 个用例，通过 {total - failed}，失败 {failed}")
    sys.exit(0 if failed == 0 else 1)
