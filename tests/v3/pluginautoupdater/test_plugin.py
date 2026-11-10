"""插件自动更新 —— 离线用例。

自带 app stub（见 run_stub_tests.py），覆盖纯逻辑层、宿主适配、配置页 / 详情页 /
API / 远程命令，以及「立即运行真的装对插件」「试运行零安装」等端到端行为。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app.plugins.pluginautoupdater import PluginAutoUpdater, _esc, host, updater
from app.sdk.events import Event
from app.sdk.plugin import _PluginBase

_STUB = sys.modules["app.api.endpoints.plugin"]
_SDK_PLUGIN = sys.modules["app.sdk.plugin"]
_SCHEDULER = sys.modules["app.sdk.scheduler"]

_BACK_REPO = "https://github.com/BackspaceM/MoviePilot-Plugins"
_JXX_REPO = "https://github.com/jxxghp/MoviePilot-Plugins"

_BAD_PHRASES = (
    "插件用途", "这个插件解决什么", "两点说明", "使用说明", "三步用起来",
    "安全机制", "注意事项", "安全提示：", "工作方式：", "属正常现象", "强烈建议",
)


def reset() -> None:
    host.set_test_hooks(None, None)
    _STUB.reset()
    _PluginBase.POSTED.clear()
    _PluginBase.CONFIGS.clear()
    _SCHEDULER.JOBS.clear()


def base_config(**overrides) -> dict:
    cfg = {
        "enabled": True, "onlyonce": False, "cron": "0 5 * * *",
        "author_filters": [], "target_plugins": [],
        "dry_run": False, "include_self": False, "force_reinstall": False,
        "max_per_run": 10, "notify_mode": "issue", "display_limit": 40,
        "repo_url_override": "", "timeout": 0,
    }
    cfg.update(overrides)
    return cfg


def make_plugin(**overrides) -> PluginAutoUpdater:
    plugin = PluginAutoUpdater()
    plugin.init_plugin(base_config(**overrides))
    return plugin


def sample_plugins():
    return updater.normalize_plugins(_SDK_PLUGIN.INSTALLED, _SDK_PLUGIN.ONLINE)


def _nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _nodes(item)


def _selects(content):
    return [n for n in _nodes(content) if n.get("component") == "VSelect"]


def _button_apis(page):
    return [n["events"]["click"]["api"] for n in _nodes(page)
            if (n.get("events") or {}).get("click", {}).get("api")]


# ---------------------------------------------------------------------------
# 纯逻辑层
# ---------------------------------------------------------------------------

def test_record_to_dict_dict_and_objects() -> None:
    d = updater.record_to_dict({
        "id": "X", "plugin_name": "名字", "plugin_author": "作者",
        "plugin_version": "1.2.0", "repo_url": "u1", "has_update": True,
        "update_candidate": {"version": "1.3.0", "repo_url": "u2"},
    })
    assert d["id"] == "X" and d["name"] == "名字" and d["author"] == "作者"
    assert d["version"] == "1.2.0"
    assert d["latest_version"] == "1.3.0" and d["repo_url"] == "u2"

    class _Pydantic:
        def model_dump(self):
            return {"id": "P", "plugin_name": "P名", "plugin_author": "P作者",
                    "plugin_version": "2.0.0", "repo_url": "up"}

    class _Plain:
        pass

    plain = _Plain()
    plain.id = "Q"
    plain.plugin_name = "Q名"
    plain.plugin_author = "Q作者"
    plain.plugin_version = "3.0.0"
    plain.repo_url = "uq"

    p = updater.record_to_dict(_Pydantic())
    q = updater.record_to_dict(plain)
    assert p["name"] == "P名" and p["version"] == "2.0.0"
    assert q["name"] == "Q名" and q["author"] == "Q作者" and q["version"] == "3.0.0"
    assert updater.record_to_dict(None)["id"] == ""


def test_installed_plugin_roundtrip() -> None:
    p = updater.InstalledPlugin(id="A", name="甲", author="乙", version="1.0",
                                latest_version="1.1", has_update=True,
                                repo_url="u", enabled=False, source="市场索引")
    d = p.to_dict()
    assert d["id"] == "A" and d["has_update"] is True
    back = updater.InstalledPlugin.from_dict(d)
    assert back == p
    assert updater.InstalledPlugin.from_dict({}) == updater.InstalledPlugin()


def test_compare_versions() -> None:
    assert updater.compare_versions("1.2.0", "1.2") == 0
    assert updater.compare_versions("v2.1.0", "2.0.9") == 1
    assert updater.compare_versions("1.0", "1.0.1") == -1
    assert updater.compare_versions("1.3.0-beta", "1.2.9") == 1
    assert updater.compare_versions("1.2.3+build7", "1.2.3") == 0
    assert updater.compare_versions("", "1.0.0") == -1
    assert updater.compare_versions("1.10", "1.9") == 1


def test_normalize_plugins_merges_online() -> None:
    plugins = sample_plugins()
    by = {p.id: p for p in plugins}
    assert len(plugins) == 6
    assert by["MediaLibraryRefresh"].latest_version == "1.3.0"
    assert by["MediaLibraryRefresh"].has_update
    assert by["MediaLibraryRefresh"].repo_url == _BACK_REPO
    assert by["ViewingWeeklyReport"].latest_version == "2.1.0"
    assert by["DownloaderHelper"].latest_version == "1.1.0"
    assert by["DownloaderHelper"].repo_url == _JXX_REPO
    assert not by["SiteSpider"].has_update
    assert by["SiteSpider"].latest_version == "1.4.0"
    assert by["AutoSignIn"].author == "未标注作者"
    assert by["PluginAutoUpdater"].source == "市场索引"
    names = [p.name for p in plugins]
    assert names == sorted(names, key=lambda x: x.lower())


def test_author_items_dedupe() -> None:
    items = updater.author_items(sample_plugins())
    values = [i["value"] for i in items]
    assert values[0] == updater.ALL_AUTHORS
    assert items[0]["title"] == "全部作者"
    assert len(values) == len(set(values)), "作者不能重复"
    assert "BackspaceM" in values and "jxxghp" in values
    assert "未标注作者" in values
    by = {i["value"]: i for i in items}
    assert by["BackspaceM"]["count"] == 3
    assert by[updater.ALL_AUTHORS]["count"] == 6


def test_plugin_items_filter_by_author() -> None:
    plugins = sample_plugins()
    assert len(updater.plugin_items(plugins)) == 6
    jxx = updater.plugin_items(plugins, "jxxghp")
    assert {i["value"] for i in jxx} == {"DownloaderHelper", "SiteSpider"}
    assert all("jxxghp" in i["title"] for i in jxx)
    assert all(" · " in i["title"] for i in jxx)
    assert updater.plugin_items(plugins, "没人") == []


def test_filter_targets_all_when_unselected() -> None:
    plugins = sample_plugins()
    targets, notes = updater.filter_targets(
        plugins, "jxxghp", [], skip_self_id="PluginAutoUpdater")
    assert {p.id for p in targets} == {"DownloaderHelper", "SiteSpider"}
    assert any("未勾选" in n for n in notes)


def test_filter_targets_selected_intersection() -> None:
    plugins = sample_plugins()
    targets, _ = updater.filter_targets(
        plugins, "jxxghp", ["DownloaderHelper", "MediaLibraryRefresh"],
        skip_self_id="PluginAutoUpdater")
    assert {p.id for p in targets} == {"DownloaderHelper"}, "要和作者过滤取交集"


def test_filter_targets_excludes_self() -> None:
    plugins = sample_plugins()
    targets, notes = updater.filter_targets(
        plugins, updater.ALL_AUTHORS, [], skip_self_id="PluginAutoUpdater")
    ids = {p.id for p in targets}
    assert "PluginAutoUpdater" not in ids
    assert len(ids) == 5
    assert any("自身" in n for n in notes)


def test_filter_targets_include_self() -> None:
    plugins = sample_plugins()
    targets, _ = updater.filter_targets(
        plugins, updater.ALL_AUTHORS, [], skip_self_id="PluginAutoUpdater",
        include_self=True)
    assert "PluginAutoUpdater" in {p.id for p in targets}


def test_validate_cron() -> None:
    assert updater.validate_cron("0 5 * * *")[0]
    assert updater.validate_cron("*/10 * * * 1-5")[0]
    assert updater.validate_cron("0 5 * * ?")[0]
    for bad in ("", "0 5 * *", "0 5 * * * *", "99 5 * * *",
                "a b c d e", "0 5 * * 9"):
        assert not updater.validate_cron(bad)[0], f"应该非法：{bad}"


def test_build_run_plan_and_max_per_run() -> None:
    plugins = sample_plugins()
    targets, _ = updater.filter_targets(
        plugins, updater.ALL_AUTHORS, [], skip_self_id="PluginAutoUpdater")
    plan = updater.build_run_plan(targets, False, 2)
    assert len(plan["items"]) == 2
    assert len(plan["blocked"]) == 1
    assert any("上限" in b["reason"] for b in plan["blocked"])
    assert len(plan["skipped"]) == 2
    for item in plan["items"]:
        assert {"id", "name", "author", "from", "to", "repo_url", "reason"} <= set(item)

    forced = updater.build_run_plan(targets, False, 100, True)
    assert len(forced["items"]) == 5
    assert not forced["skipped"]
    assert any(i["reason"] == "强制重装" for i in forced["items"])


def test_summarize_run_and_history_bounded() -> None:
    items = [{"id": "A", "name": "甲", "from": "1.0", "to": "1.1",
              "repo_url": "u", "reason": "市场有新版本"},
             {"id": "B", "name": "乙", "from": "2.0", "to": "2.1",
              "repo_url": "v", "reason": "市场有新版本"}]
    report = updater.summarize_run(items, {
        "outcomes": [{"id": "A", "ok": True, "message": "装好了", "mechanism": "install"},
                     {"id": "B", "ok": False, "message": "挂了", "mechanism": "install"}],
        "duration": 1.25, "dry_run": False, "skipped": 3,
    })
    assert report["total"] == 2 and report["success"] == 1 and report["failed"] == 1
    assert report["skipped"] == 3 and report["duration"] == 1.25
    assert not report["ok"]
    assert len(report["details"]) == 2

    history = []
    for i in range(50):
        history = updater.append_history(history, {"message": f"run{i}"}, 10)
    assert len(history) == 10
    assert history[0]["message"] == "run49"
    assert updater.append_history(None, {"message": "x"}, 5)[0]["message"] == "x"


# ---------------------------------------------------------------------------
# 宿主适配 + 端到端运行
# ---------------------------------------------------------------------------

def test_host_scan_real_stub() -> None:
    reset()
    installed, online, source = host.scan_plugins()
    assert len(installed) == 6 and len(online) == 6
    assert "PluginManager" in source
    assert any(isinstance(r, dict) for r in installed)
    assert any(not isinstance(r, dict) for r in installed)


def test_dry_run_zero_install() -> None:
    reset()
    plugin = make_plugin(dry_run=True, author_filter=updater.ALL_AUTHORS)
    report = plugin.run_once("手动")
    assert report["dry_run"] is True
    assert report["total"] == 3
    assert _STUB.INSTALL_CALLS == [], "试运行完全不能调用安装入口"
    assert all(d["mechanism"] == "dry_run" for d in report["details"])
    assert plugin.get_data("pau_last_scan")["plugins"]


def test_run_calls_correct_ids_and_source() -> None:
    reset()
    plugin = make_plugin(author_filter=updater.ALL_AUTHORS,
                         target_plugins=["MediaLibraryRefresh", "ViewingWeeklyReport"])
    report = plugin.run_once("手动")
    assert report["total"] == 2, report
    called = [c["plugin_id"] for c in _STUB.INSTALL_CALLS]
    assert set(called) == {"MediaLibraryRefresh", "ViewingWeeklyReport"}, called
    for call in _STUB.INSTALL_CALLS:
        assert call["repo_url"] == _BACK_REPO, call
        assert call["force"] is True
        assert call["release_version"] is None
    assert "PluginManager" in report["source"]
    assert plugin.get_data("pau_last_report")["total"] == 2


def test_single_failure_continues() -> None:
    reset()
    _STUB.INSTALL_FAIL_IDS.add("MediaLibraryRefresh")
    plugin = make_plugin(author_filter=updater.ALL_AUTHORS,
                         target_plugins=["MediaLibraryRefresh", "ViewingWeeklyReport",
                                         "DownloaderHelper"])
    report = plugin.run_once("手动")
    assert report["total"] == 3
    assert report["failed"] == 1 and report["success"] == 2
    assert not report["ok"]
    assert len(_STUB.INSTALL_CALLS) == 3, "单个失败要继续装后面的"
    bad = [d for d in report["details"] if not d["ok"]]
    assert len(bad) == 1 and bad[0]["id"] == "MediaLibraryRefresh"
    assert bad[0]["message"], "失败原因要逐条记下来"


def test_scan_empty_is_read_only() -> None:
    reset()
    host.set_test_hooks(scan=lambda: ([], [], "模拟读不到"))
    try:
        plugin = make_plugin()
        report = plugin.run_once("手动")
        assert not report["ok"] and report["total"] == 0
        assert _STUB.INSTALL_CALLS == []
        assert plugin.get_data("pau_last_report") is None
        assert plugin.get_data("pau_last_scan") is None
        assert plugin.get_data("pau_history") in (None, [])
    finally:
        host.set_test_hooks(None, None)


def test_max_per_run_in_real_run() -> None:
    reset()
    plugin = make_plugin(author_filter=updater.ALL_AUTHORS, max_per_run=1)
    report = plugin.run_once("手动")
    assert report["total"] == 1
    assert report["blocked"] == 2
    assert len(_STUB.INSTALL_CALLS) == 1


def test_notify_mode_all_and_none() -> None:
    reset()
    plugin = make_plugin(notify_mode="all", author_filter=updater.ALL_AUTHORS,
                         target_plugins=["MediaLibraryRefresh"])
    plugin.run_once()
    assert _PluginBase.POSTED, "all 模式要发通知"

    reset()
    plugin = make_plugin(notify_mode="none", author_filter=updater.ALL_AUTHORS,
                         target_plugins=["MediaLibraryRefresh"])
    plugin.run_once()
    assert not _PluginBase.POSTED, "none 模式不发通知"


def test_history_record_bounded_in_plugin() -> None:
    reset()
    plugin = make_plugin(author_filter=updater.ALL_AUTHORS,
                         target_plugins=["MediaLibraryRefresh"])
    for _ in range(40):
        plugin.run_once()
    history = list(plugin.get_data("pau_history") or [])
    assert len(history) == 30


# ---------------------------------------------------------------------------
# 配置页 / 详情页 / API / 命令
# ---------------------------------------------------------------------------

def test_form_has_two_multi_selects() -> None:
    reset()
    plugin = make_plugin()
    content, defaults = plugin.get_form()
    sels = {s["props"]["model"]: s for s in _selects(content)}
    assert {"author_filters", "target_plugins"} <= set(sels)
    for key in ("author_filters", "target_plugins"):
        assert sels[key]["props"]["multiple"] is True
        assert sels[key]["props"]["chips"] is True
        assert sels[key]["props"]["clearable"] is True
    authors = {i["value"] for i in sels["author_filters"]["props"]["items"]}
    assert updater.ALL_AUTHORS not in authors, "多选下拉不该再放「全部作者」"
    assert {"BackspaceM", "jxxghp", "未标注作者"} <= authors
    ids = {i["value"] for i in sels["target_plugins"]["props"]["items"]}
    assert {"DownloaderHelper", "SiteSpider", "ViewingWeeklyReport"} <= ids
    assert defaults["author_filters"] == []
    assert defaults["cron"] == "0 5 * * *"
    blob = json.dumps(content, ensure_ascii=False)
    for phrase in _BAD_PHRASES:
        assert phrase not in blob, f"设置页不该出现套话：{phrase}"


def test_form_reads_only_cached_or_local() -> None:
    reset()
    calls = []

    def hook():
        calls.append(1)
        return list(_SDK_PLUGIN.INSTALLED), [], "桩"

    host.set_test_hooks(scan=hook)
    try:
        plugin = make_plugin()
        plugin.save_data("pau_last_scan", {
            "time": "x", "source": "本机清单",
            "plugins": [x.to_dict() for x in sample_plugins()],
        })
        content, _ = plugin.get_form()
        assert not calls, "配置页有缓存时不该再扫一遍"
        assert any(n.get("component") == "VSelect"
                   and n["props"]["model"] == "target_plugins" for n in _nodes(content))
    finally:
        host.set_test_hooks(None, None)


def test_form_offline_gives_short_hint() -> None:
    reset()
    host.set_test_hooks(scan=lambda: ([], [], "模拟离线"))
    try:
        plugin = make_plugin()
        content, _ = plugin.get_form()
        blob = json.dumps(content, ensure_ascii=False)
        assert "还没读到插件清单" in blob
        assert "刷新清单" in blob
    finally:
        host.set_test_hooks(None, None)


def test_multi_author_filter_targets() -> None:
    plugins = sample_plugins()
    targets, _ = updater.filter_targets(
        plugins, ["BackspaceM", "jxxghp"], [], skip_self_id="PluginAutoUpdater")
    assert {p.id for p in targets} == {
        "MediaLibraryRefresh", "ViewingWeeklyReport", "DownloaderHelper", "SiteSpider"}
    assert updater.normalize_authors(["jxxghp", updater.ALL_AUTHORS, "jxxghp", ""]) == ["jxxghp"]
    assert updater.normalize_authors("jxxghp,BackspaceM") == ["jxxghp", "BackspaceM"]


def test_page_is_minimal_and_buttons_are_real() -> None:
    reset()
    plugin = make_plugin()
    plugin.api_scan()
    page = plugin.get_page()
    apis = _button_apis(page)
    registered = {f"plugin/PluginAutoUpdater{a['path']}"
                  for a in plugin.get_api()}
    assert len(apis) == 4, apis
    assert set(apis) <= registered
    assert not [a for a in apis if a.endswith("/filter")]
    blob = json.dumps(page, ensure_ascii=False)
    for term in ("运行日志", "更新记录", "最近一次运行", "扫描到的插件",
                 "<th>", "VTable", "VCardTitle"):
        assert term not in blob, term
    for phrase in _BAD_PHRASES:
        assert phrase not in blob, f"详情页不该出现套话：{phrase}"
    assert "本机 6 个插件" in blob


def test_api_endpoints_have_no_var_keyword() -> None:
    """FastAPI 会把 **kwargs 当成必填查询参数，POST 空 body 直接 422。"""
    import inspect
    reset()
    plugin = make_plugin()
    for api in plugin.get_api():
        for param in inspect.signature(api["endpoint"]).parameters.values():
            assert param.kind is not inspect.Parameter.VAR_KEYWORD, api["path"]
            assert param.default is not inspect.Parameter.empty, api["path"]


def test_filter_api_safe_signature() -> None:
    reset()
    plugin = make_plugin()
    result = plugin.api_filter({"author": "jxxghp"})
    assert result.success and result.data["author"] == "jxxghp"
    assert plugin.api_filter().success


def test_api_paths_and_commands() -> None:
    reset()
    plugin = make_plugin()
    paths = {a["path"] for a in plugin.get_api()}
    assert {"/scan", "/run", "/dryrun", "/filter", "/status",
            "/history", "/clear", "/logs"} <= paths
    assert all(a["auth"] == "bear" for a in plugin.get_api())
    cmds = {c["cmd"] for c in plugin.get_command()}
    assert len(cmds) >= 3
    assert all(c["event"] == "plugin.action" for c in plugin.get_command())
    status = plugin.api_status()
    assert status.success and status.data["plugin"] == "PluginAutoUpdater"
    assert plugin.api_logs().success and plugin.api_history().success
    assert plugin.api_clear().success
    assert plugin.api_logs().data["logs"] == []


def test_api_run_and_dryrun_via_endpoint() -> None:
    reset()
    plugin = make_plugin(author_filter=updater.ALL_AUTHORS,
                         target_plugins=["MediaLibraryRefresh"])
    result = plugin.api_dryrun()
    assert result.success
    assert _STUB.INSTALL_CALLS == [], "试运行端点不能装"
    result = plugin.api_run()
    assert result.success
    assert [c["plugin_id"] for c in _STUB.INSTALL_CALLS] == ["MediaLibraryRefresh"]


def test_remote_command_replies() -> None:
    reset()
    plugin = make_plugin()
    event = Event(event_type="plugin.action",
                  event_data={"cmd": "/pau_status", "channel": "tg", "userid": "1"})
    plugin.handle_command(event)
    assert _PluginBase.POSTED, "远程命令要回消息"
    assert "扫描到" in _PluginBase.POSTED[-1]["text"]


def test_onlyonce_and_get_service() -> None:
    reset()
    plugin = make_plugin(onlyonce=True)
    assert _PluginBase.CONFIGS, "onlyonce 要复位并回写配置"
    assert _PluginBase.CONFIGS[-1]["onlyonce"] is False
    assert _SCHEDULER.JOBS, "onlyonce 要提交一次任务"
    service = plugin.get_service()
    assert len(service) == 1
    assert service[0]["func"] == plugin.run_once
    assert service[0]["kwargs"]["trigger"] == "定时"
    assert service[0]["trigger"] == ("cron", "0 5 * * *")
    assert plugin.get_state() is True
    disabled = make_plugin(enabled=False)
    assert disabled.get_service() == []


def test_esc_and_version_consistency() -> None:
    assert _esc("<x>") == "&lt;x&gt;"
    assert _esc("a[b]") == "a［b］"
    assert PluginAutoUpdater.plugin_version == "1.1.0"
    assert PluginAutoUpdater.plugin_author == "BackspaceM"
    assert PluginAutoUpdater.plugin_order == 56
    assert PluginAutoUpdater.system_version == ">=3.0.0"
    assert PluginAutoUpdater.plugin_icon.endswith("/icons/pluginautoupdater.png")

    repo = os.environ.get("PAU_REPO")
    if repo:
        index = json.loads((Path(repo) / "package.v3.json").read_text(encoding="utf-8"))
        item = index["PluginAutoUpdater"]
        assert item["version"] == PluginAutoUpdater.plugin_version
        assert item["author"] == PluginAutoUpdater.plugin_author
        assert f"v{PluginAutoUpdater.plugin_version}" in item["history"]


def _record_id(record) -> str:
    return str(record.get("id") if isinstance(record, dict)
               else getattr(record, "id", "")) or ""


def test_host_scan_uses_instance_manager() -> None:
    """V3 的 PluginManager 是实例方法，不能拿类直接调。"""
    reset()
    saved = _SDK_PLUGIN.PluginManager

    class InstancePM:
        def get_installed_plugins(self):
            return [{"id": "InstancePlugin", "plugin_name": "实例方法插件",
                     "plugin_author": "BackspaceM", "plugin_version": "1.0.0",
                     "installed": True}]

        def get_online_plugins(self, force=False):
            return [{"id": "InstancePlugin", "plugin_version": "1.1.0"}]

    try:
        _SDK_PLUGIN.PluginManager = InstancePM
        installed, online, source = host.scan_plugins()
        assert "InstancePlugin" in [_record_id(r) for r in installed]
        assert online and "PluginManager" in source
    finally:
        _SDK_PLUGIN.PluginManager = saved


def test_host_install_accepts_sync_endpoint() -> None:
    """宿主安装入口是同步函数时也能用，不能无脑 asyncio.run。"""
    reset()
    saved = _STUB.install
    calls = []

    class Resp:
        success = True
        message = "同步安装完成"

    def sync_install(plugin_id, repo_url="", release_version=None, force=False, _=None):
        calls.append((plugin_id, force))
        return Resp()

    try:
        _STUB.install = sync_install
        ok, msg, mechanism = host.install_plugin("SomePlugin", "https://repo", True)
        assert ok and "同步" in msg
        assert calls == [("SomePlugin", True)]
        assert mechanism == "app.api.endpoints.plugin.install"
    finally:
        _STUB.install = saved


def test_host_scan_async_all_plugins_fallback() -> None:
    """拿不到管理器时，公开列表端点是 async 也要能读。"""
    reset()
    saved_pm = _SDK_PLUGIN.PluginManager
    saved_all = _STUB.all_plugins

    async def async_all(state="installed", _=None, response=None):
        return {"success": True,
                "data": [{"id": "FallbackPlugin", "plugin_name": "回退插件"}]}

    try:
        del _SDK_PLUGIN.PluginManager
        _STUB.all_plugins = async_all
        installed, online, source = host.scan_plugins()
        assert "FallbackPlugin" in [_record_id(r) for r in installed]
        assert "all_plugins" in source
    finally:
        _SDK_PLUGIN.PluginManager = saved_pm
        _STUB.all_plugins = saved_all
