"""RouterOS 统一工作台（B5）离线用例。

用带状态的假 RouterOS REST 服务验证端到端行为：只读刷新 → 聚合快照 → 页签
渲染 → 切页签 → 插件状态；纯逻辑层（aggregator / plugininfo）直接拿 dict 单测。
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from urllib.parse import urlparse

from app.plugins.routerosworkbench import (RouterOSWorkbench, _esc, aggregator,
                                            plugininfo, sections)
from app.plugins.routerosworkbench import clients as clients_mod
from app.sdk.events import Event
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase

# 与 CONFTEST 里的假服务保持一致：各分区若干行，resource / identity 是单对象
SAMPLE_ROWS = {
    "/rest/system/resource": {"uptime": "3d2h10m5s", "version": "7.15.3",
                              "cpu-load": "12", "total-memory": "1073741824",
                              "free-memory": "536870912", "cpu-count": "4",
                              "board-name": "RB5009UG+S+"},
    "/rest/system/identity": {"name": "客厅 RB5009"},
    "/rest/interface": [
        {"name": "ether1", "type": "ether", "rx-byte": "1000", "tx-byte": "500", "running": "true"},
        {"name": "ether2", "type": "ether", "rx-byte": "2000", "tx-byte": "800", "running": "true"},
        {"name": "bridge-lan", "type": "bridge", "rx-byte": "300", "tx-byte": "120", "running": "false"},
    ],
    "/rest/ip/address": [
        {"address": "192.168.88.1/24", "interface": "bridge-lan"},
        {"address": "10.0.0.1/24", "interface": "ether2"},
    ],
    "/rest/ip/firewall/filter": [
        {".id": "*1", "chain": "forward", "action": "accept", "comment": "放行已建立", "disabled": "false"},
        {".id": "*2", "chain": "forward", "action": "drop", "comment": "手工兜底", "disabled": "false"},
        {".id": "*3", "chain": "input", "action": "accept", "comment": "限管理口", "disabled": "true"},
    ],
    "/rest/ip/firewall/nat": [
        {".id": "*1", "chain": "srcnat", "action": "masquerade", "comment": "上网", "disabled": "false"},
    ],
    "/rest/ip/firewall/mangle": [
        {".id": "*1", "chain": "prerouting", "action": "mark-routing",
         "comment": "MP-B3[分流:标记]", "disabled": "false"},
    ],
    "/rest/ip/firewall/address-list": [
        {"address": "1.1.1.1", "list": "MP-A2-CF", "comment": "MP-A2 优选"},
        {"address": "8.8.8.8", "list": "dns", "comment": ""},
    ],
    "/rest/queue/simple": [
        {"name": "客厅", "target": "192.168.88.0/24", "max-limit": "10M/10M",
         "rate": "1M/2M", "disabled": "false"},
        {"name": "下载", "target": "192.168.88.20/32", "max-limit": "5M/5M",
         "rate": "0/0", "disabled": "true"},
    ],
    "/rest/ip/dns/static": [
        {"name": "movie.example.com", "address": "192.168.88.10", "comment": "MP-A2 内网解析"},
    ],
    "/rest/ip/route": [
        {"dst-address": "0.0.0.0/0", "gateway": "192.168.88.254", "distance": "1", "comment": ""},
        {"dst-address": "10.0.0.0/24", "gateway": "ether2", "distance": "1", "comment": "MP-B3 旁路由"},
    ],
    "/rest/file": [
        {"name": "auto-backup.backup", "size": "123456", "date": "2026-11-01 10:00:00"},
        {"name": "export.rsc", "size": "2048", "date": "2026-11-02 08:30:00"},
        {"name": "notes.txt", "size": "10", "date": "2026-11-02 09:00:00"},
    ],
}


class FakeRouterOS:
    """按分区返回若干行；unreadable 里的分区 GET 返回 403，模拟读权限不足。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self, unreadable=()) -> None:
        self.rows = {
            key: (dict(value) if isinstance(value, dict) else [dict(r) for r in value])
            for key, value in SAMPLE_ROWS.items()
        }
        self.unreadable = set(unreadable)
        AsyncRequestUtils.calls = []
        AsyncRequestUtils.responder = self.respond

    def respond(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        path = urlparse(url).path
        if method == "GET" and path in self.unreadable:
            return FakeResponse(403, {"detail": "permission denied"})
        if method == "GET":
            return FakeResponse(200, self.rows.get(path, []))
        return FakeResponse(405, {"detail": "unexpected"})


FAKE = FakeRouterOS()


def sample_raw() -> dict:
    """把假服务的数据整理成 refresh_once 交给聚合层的 raw 形状。"""
    out = {}
    for key, value in SAMPLE_ROWS.items():
        out[key] = [dict(value)] if isinstance(value, dict) else [dict(r) for r in value]
    return out


def base_config(**overrides) -> dict:
    cfg = {
        "enabled": True, "onlyonce": False,
        "ros_connection": "", "router_address": "192.168.88.1", "router_port": 443,
        "router_ssl": True, "router_username": "mp-api", "router_password": "secret",
        "verify": False, "timeout": 10,
        "cron": "*/10 * * * *", "display_limit": 40,
        "notify_mode": "issue", "active_tab": "overview",
    }
    cfg.update(overrides)
    return cfg


def make_plugin(**overrides) -> RouterOSWorkbench:
    plugin = RouterOSWorkbench()
    plugin.init_plugin(base_config(**overrides))
    return plugin


def walk(node):
    """递归遍历详情页组件树，取出所有按钮绑定的 api 路径。"""
    found = []
    if isinstance(node, dict):
        click = (node.get("events") or {}).get("click") or {}
        if click.get("api"):
            found.append(click["api"])
        for value in node.values():
            found.extend(walk(value))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(walk(item))
    return found


def click_events(node):
    found = []
    if isinstance(node, dict):
        click = (node.get("events") or {}).get("click")
        if isinstance(click, dict):
            found.append(click)
        for value in node.values():
            found.extend(click_events(value))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(click_events(item))
    return found


# ---------------------------------------------------------------------------
# 聚合层：结构与各页签
# ---------------------------------------------------------------------------

def test_build_snapshot_structure() -> None:
    states = [{"id": "RouterOSConfigGuard", "name": "配置备份", "version": "1.0.0",
               "enabled": True, "installed": True, "has_update": False, "note": ""}]
    snap = aggregator.build_snapshot(sample_raw(), states)
    for key in ("created", "router", "interfaces", "topology", "tabs", "plugins",
                "counts", "warnings"):
        assert key in snap, key
    assert set(snap["tabs"]) == {"overview", "firewall", "queue", "backup", "routing"}
    assert snap["router"]["version"] == "7.15.3"
    assert snap["router"]["name"] == "客厅 RB5009"
    assert len(snap["interfaces"]) == 3
    assert {"name", "type", "rx", "tx", "rx_rate", "tx_rate", "running"} <= set(
        snap["interfaces"][0])
    assert snap["interfaces"][0]["name"] == "ether1"
    assert snap["counts"][sections.SEC_FILTER] == 3
    assert snap["counts"][sections.SEC_QUEUE] == 2
    assert len(snap["plugins"]) == 1
    kinds = {x["kind"] for x in snap["topology"]}
    assert {"接口", "地址", "默认路由"} <= kinds


def test_overview_cards_and_stats() -> None:
    snap = aggregator.build_snapshot(sample_raw(), [])
    ov = snap["tabs"]["overview"]
    titles = [c["title"] for c in ov["cards"]]
    for word in ("RouterOS 版本", "运行时长", "CPU / 内存", "接口", "规则",
                 "队列", "静态 DNS", "实时流量"):
        assert word in titles, word
    assert ov["stats"]["interfaces"] == 3
    assert ov["stats"]["running"] == 2
    assert ov["stats"]["rules"] == 5
    assert ov["stats"]["queues"] == 2
    assert ov["stats"]["dns"] == 1
    assert snap["traffic"]["mode"] == "cumulative"
    assert len(ov["tables"]) == 2
    assert ov["tables"][0]["columns"][0] == "接口"
    assert ov["tables"][1]["columns"] == ["类别", "内容", "说明"]


def test_firewall_tab_tables_and_stats() -> None:
    snap = aggregator.build_snapshot(sample_raw(), [])
    fw = snap["tabs"]["firewall"]
    assert [t["title"] for t in fw["tables"]] == [
        "防火墙｜filter", "防火墙｜nat", "防火墙｜mangle"]
    assert fw["tables"][0]["columns"] == ["链", "动作", "备注", "状态"]
    assert len(fw["tables"][0]["rows"]) == 3
    assert fw["stats"]["total"] == 5
    assert fw["stats"]["accept"] == 2
    assert fw["stats"]["drop"] == 1
    assert fw["stats"]["disabled"] == 1
    assert fw["stats"]["address_list"] == 2


def test_queue_and_backup_tabs() -> None:
    snap = aggregator.build_snapshot(sample_raw(), [])
    q = snap["tabs"]["queue"]
    assert q["tables"][0]["columns"] == ["名称", "目标", "限速上限", "当前速率", "状态"]
    assert len(q["tables"][0]["rows"]) == 2
    assert q["stats"]["total"] == 2 and q["stats"]["enabled"] == 1
    bk = snap["tabs"]["backup"]
    names = [r[0] for r in bk["tables"][0]["rows"]]
    assert "auto-backup.backup" in names and "export.rsc" in names
    assert "notes.txt" not in names
    assert bk["stats"]["backup"] == 1 and bk["stats"]["rsc"] == 1
    assert "本地配置备份插件" in "；".join(bk["notes"])


def test_routing_tab_highlights() -> None:
    snap = aggregator.build_snapshot(sample_raw(), [])
    rt = snap["tabs"]["routing"]
    assert [t["title"] for t in rt["tables"]] == ["静态 DNS", "地址列表", "路由"]
    assert rt["stats"]["dns"] == 1
    assert rt["stats"]["address_list"] == 2
    assert rt["stats"]["routes"] == 2
    assert rt["stats"]["default_routes"] == 1
    assert rt["stats"]["highlights"] >= 3
    assert any("MP-B3" in h for h in rt["highlights"])
    assert any("MP-A2" in h for h in rt["highlights"])


# ---------------------------------------------------------------------------
# 聚合层：速率 / 限长 / 校验 / 小工具
# ---------------------------------------------------------------------------

def test_rate_from_previous() -> None:
    previous = {
        "interfaces": [
            {"name": "ether1", "rx": 400.0, "tx": 200.0},
            {"name": "ether2", "rx": 1000.0, "tx": 400.0},
            {"name": "bridge-lan", "rx": 100.0, "tx": 50.0},
        ],
        "sample_seconds": 2.0,
    }
    snap = aggregator.build_snapshot(sample_raw(), [], previous=previous)
    by_name = {i["name"]: i for i in snap["interfaces"]}
    i0 = by_name["ether1"]
    assert i0["rx_delta"] == 600.0
    assert i0["tx_delta"] == 300.0
    assert i0["rx_rate"] == 300.0
    assert i0["tx_rate"] == 150.0
    assert snap["traffic"]["mode"] == "rate"
    assert snap["tabs"]["overview"]["traffic"]["mode"] == "rate"
    assert snap["sample_seconds"] == 2.0


def test_no_previous_uses_cumulative() -> None:
    snap = aggregator.build_snapshot(sample_raw(), [])
    assert snap["traffic"]["mode"] == "cumulative"
    assert all(i["rx_rate"] == 0 and i["tx_rate"] == 0 for i in snap["interfaces"])
    assert snap["traffic"]["rx_total"] == 3300.0
    assert any("再刷新一次" in w for w in snap["warnings"])


def test_section_rows_limit() -> None:
    raw = sample_raw()
    rows = aggregator.section_rows(raw, sections.SEC_FILTER, 2)
    assert len(rows) == 2
    assert rows[0]["chain"] == "forward"
    assert aggregator.section_rows(raw, sections.SEC_FILTER, 0) == []
    assert aggregator.section_rows(raw, "不存在的分区", 5) == []
    assert aggregator.count_rows(raw, sections.SEC_FILTER) == 3
    assert aggregator.count_rows(raw, "不存在的分区") == 0


def test_validate_cron() -> None:
    assert aggregator.validate_cron("*/10 * * * *")[0]
    assert aggregator.validate_cron("0 5 * * *")[0]
    assert not aggregator.validate_cron("* * * *")[0]
    assert not aggregator.validate_cron("* * * * * *")[0]
    assert not aggregator.validate_cron("abc def ghi jkl mno")[0]


def test_format_helpers() -> None:
    assert aggregator.human_bytes(1024) == "1.0 KB"
    assert aggregator.human_bytes(1536) == "1.5 KB"
    assert aggregator.human_rate(300) == "300 B/s"
    assert "天" in aggregator.human_uptime("3d2h10m")
    assert "天" in aggregator.human_uptime("1w2d3h4m5s")
    assert aggregator.human_uptime("") == "-"
    assert aggregator.clean_text("  a   b  ") == "a b"
    assert len(aggregator.clean_text("a\x00b" + "x" * 200, 10)) <= 10


# ---------------------------------------------------------------------------
# plugininfo 归一化
# ---------------------------------------------------------------------------

def test_plugininfo_normalize_dict_and_object() -> None:
    dict_row = {"id": "CFRouterOS", "plugin_name": "CF 优选", "plugin_author": "BackspaceM",
                "plugin_version": "2.0.0", "installed": True, "state": "enabled",
                "has_update": True, "repo_url": "http://example.com/repo"}
    a = plugininfo.normalize_record(dict_row)
    assert a["id"] == "CFRouterOS" and a["name"] == "CF 优选"
    assert a["version"] == "2.0.0" and a["author"] == "BackspaceM"
    assert a["enabled"] and a["installed"] and a["has_update"]
    assert a["repo_url"] == "http://example.com/repo"

    class Obj:
        id = "RouterOSSiteRouting"
        plugin_name = "站点分流"
        plugin_author = "BackspaceM"
        plugin_version = "1.3.0"
        installed = True
        state = "disabled"
        has_update = False
        repo_url = ""

    b = plugininfo.normalize_record(Obj())
    assert b["id"] == "RouterOSSiteRouting" and b["version"] == "1.3.0"
    assert b["installed"] and not b["enabled"]
    assert b["note"] == "装了没启用"

    states, source = plugininfo.load_plugin_states()
    assert len(states) == len(plugininfo.WATCHED_IDS) == 8
    assert "app.sdk.plugin.PluginManager" in source
    by_id = {s["id"]: s for s in states}
    assert by_id["RouterOSConnectionCenter"]["version"] == "1.0.0"
    assert by_id["RosFirewallManager"]["version"] == "1.2.0"
    assert by_id["RouterOSConfigGuard"]["enabled"] is False
    assert by_id["CFRouterOS"]["installed"] is False


def test_plugininfo_missing_manager_returns_empty() -> None:
    from app.sdk import plugin as sdk_plugin
    saved = sdk_plugin.PluginManager
    try:
        del sdk_plugin.PluginManager
        states, source = plugininfo.load_plugin_states()
        assert states == [] and "拿不到" in source
    finally:
        sdk_plugin.PluginManager = saved


def test_plugininfo_uses_instance_methods() -> None:
    """V3 宿主 PluginManager 是实例方法，不能拿类直接调。"""
    from app.sdk import plugin as sdk_plugin
    saved = sdk_plugin.PluginManager

    class InstancePM:
        def get_installed_plugins(self):
            return [{"id": "RosFirewallManager", "plugin_name": "实例方法来的",
                     "plugin_version": "9.9.9", "installed": True}]

    try:
        sdk_plugin.PluginManager = InstancePM
        states, source = plugininfo.load_plugin_states()
        assert "app.sdk.plugin.PluginManager" in source
        assert any(s["id"] == "RosFirewallManager" and s["version"] == "9.9.9"
                   for s in states)
    finally:
        sdk_plugin.PluginManager = saved


# ---------------------------------------------------------------------------
# 刷新：只读 / 失败不中断
# ---------------------------------------------------------------------------

def test_refresh_is_readonly() -> None:
    FAKE.reset()
    plugin = make_plugin()
    called = []
    originals = {}
    for name in ("create", "delete", "save_backup"):
        originals[name] = getattr(clients_mod.RouterOSConfigClient, name)

        def make_spy(label):
            async def spy(self, *a, **k):
                called.append(label)
                return (False, "", "") if label == "create" else (False, "")
            return spy

        setattr(clients_mod.RouterOSConfigClient, name, make_spy(name))
    try:
        snap = plugin.refresh_once("只读测试")
    finally:
        for name, fn in originals.items():
            setattr(clients_mod.RouterOSConfigClient, name, fn)
    assert snap is not None
    assert not called, f"写方法被调用了：{called}"
    methods = {c[0] for c in AsyncRequestUtils.calls}
    assert methods == {"GET"}, methods
    assert plugin.api_status().data["snapshot"]["tabs"]


def test_section_failure_does_not_break() -> None:
    bad = [sections.SEC_MANGLE, sections.SEC_FILE]
    FAKE.reset(unreadable=bad)
    plugin = make_plugin()
    snap = plugin.refresh_once("部分失败测试")
    assert snap is not None
    joined = "；".join(snap["warnings"])
    assert sections.SEC_MANGLE in joined
    assert sections.SEC_FILE in joined
    assert snap["counts"][sections.SEC_FILTER] == 3
    assert snap["counts"][sections.SEC_INTERFACE] == 3
    assert len(snap["tabs"]["firewall"]["tables"]) == 3
    assert snap["tabs"]["firewall"]["tables"][2]["rows"] == []
    assert snap["tabs"]["backup"]["tables"][0]["rows"] == []


# ---------------------------------------------------------------------------
# 页签 / 表单 / 页面 / API / 命令
# ---------------------------------------------------------------------------

def test_tab_switch_changes_active_tab() -> None:
    FAKE.reset()
    plugin = make_plugin()
    plugin.refresh_once("切页签前刷新")
    assert plugin.api_status().data["active_tab"] == "overview"
    assert plugin.api_tab({"name": "firewall"}).success
    assert plugin._active_tab == "firewall"
    assert plugin.api_status().data["active_tab"] == "firewall"
    assert plugin.get_data("b5_active_tab") == "firewall"
    assert "防火墙｜filter" in json.dumps(plugin.get_page(), ensure_ascii=False)
    assert plugin.api_tab({"name": "queue"}).success
    assert plugin.api_tab({"name": "nope"}).success is False


def test_form_and_page_buttons_bind_real_apis() -> None:
    FAKE.reset()
    plugin = make_plugin()
    plugin.refresh_once("页面测试")
    content, defaults = plugin.get_form()
    form_blob = json.dumps(content, ensure_ascii=False)
    assert "已保存的路由器（连接中心）" in form_blob
    assert "VSelect" in form_blob and "VTextField" in form_blob and "VSwitch" in form_blob
    assert defaults["cron"] == "*/10 * * * *"
    assert defaults["active_tab"] == "overview"
    for phrase in ("插件用途", "这个插件解决什么", "两点", "使用说明", "三步用起来",
                   "安全机制", "注意事项", "强烈建议", "安全提示：", "工作方式："):
        assert phrase not in form_blob, f"表单不该出现套话：{phrase}"

    page = plugin.get_page()
    page_blob = json.dumps(page, ensure_ascii=False)
    for phrase in ("插件用途", "使用说明", "注意事项", "强烈建议"):
        assert phrase not in page_blob
    buttons = walk(page)
    assert len(buttons) >= 9
    registered = {f"plugin/RouterOSWorkbench{a['path']}" for a in plugin.get_api()}
    for api in buttons:
        assert api in registered, f"按钮绑到了不存在的 API：{api}"

    tab_clicks = [c for c in click_events(page)
                  if c.get("api") == "plugin/RouterOSWorkbench/tab"]
    assert len(tab_clicks) == 5
    assert {c["params"]["name"] for c in tab_clicks} == set(sections.TAB_KEYS)
    assert "插件工具箱" in page_blob
    assert "VTable" in page_blob and "thead" in page_blob and "tbody" in page_blob


def test_api_endpoints_and_commands() -> None:
    FAKE.reset()
    plugin = make_plugin()
    apis = plugin.get_api()
    paths = {a["path"] for a in apis}
    assert {"/refresh", "/tab", "/status", "/plugins", "/test", "/clear", "/logs"} <= paths
    assert all(a["auth"] == "bear" for a in apis)
    cmds = {c["cmd"] for c in plugin.get_command()}
    assert {"/b5_refresh", "/b5_status", "/b5_plugins"} <= cmds
    assert plugin.api_status().data["plugin"] == "RouterOSWorkbench"
    assert plugin.api_logs().success
    assert plugin.api_clear().success
    assert plugin.api_plugins().success
    assert plugin.api_test().success
    assert plugin.api_refresh().success

    _PluginBase.POSTED.clear()
    event = Event(event_type="plugin.action",
                  event_data={"cmd": "/b5_status", "channel": "tg", "userid": "1"})
    plugin.handle_command(event)
    assert _PluginBase.POSTED, "远程命令要回消息"
    assert "工作台" in _PluginBase.POSTED[-1]["text"]


def test_get_service_and_state() -> None:
    plugin = make_plugin()
    assert plugin.get_state() is True
    services = plugin.get_service()
    assert len(services) == 1
    assert services[0]["func"] == plugin.refresh_once
    assert services[0]["kwargs"] == {"trigger": "定时刷新"}
    assert "cron" in str(services[0]["trigger"])
    assert make_plugin(enabled=False).get_service() == []
    assert make_plugin(cron="坏 cron")._cron == "*/10 * * * *"


def test_version_three_places() -> None:
    assert RouterOSWorkbench.plugin_version == "1.0.0"
    assert RouterOSWorkbench.plugin_author == "BackspaceM"
    assert RouterOSWorkbench.plugin_icon.endswith("icons/routerosworkbench.png")
    assert RouterOSWorkbench.plugin_order == 55
    assert RouterOSWorkbench.system_version == ">=3.0.0"
    repo = os.environ.get("ROUTEROS_WORKBENCH_REPO")
    if repo:
        index = json.loads((Path(repo) / "package.v3.json").read_text(encoding="utf-8"))
        item = index["RouterOSWorkbench"]
        assert item["version"] == RouterOSWorkbench.plugin_version
        assert f"v{RouterOSWorkbench.plugin_version}" in item["history"]


def test_esc_and_log_limit() -> None:
    assert _esc("<script>") == "&lt;script&gt;"
    assert "［" in _esc("[MP-B1]") and "］" in _esc("[MP-B1]")
    plugin = make_plugin()
    for i in range(210):
        plugin._log("INFO", f"第 {i} 条")
    assert len(plugin.get_data("b5_logs")) == 200
