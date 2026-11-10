"""RouterOS 场景模板一键部署（A2）离线用例。

用带状态的假 RouterOS REST 服务验证端到端行为：预览 → 幂等跳过 → 批量写入
→ 备份/快照 → 失败逆序回滚 → 手动回滚，并确保用户手工规则绝不被删改。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from app.plugins.routerosscenariotemplates import (RouterOSScenarioTemplates,
                                                   engine, templates as tplmod)
from app.sdk.events import Event
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase

WG_KEY = "YjQ2NzE5YjM4ZjQ1YTVkNmVjZjM3YjI5YThkMWI0YzU="


class FakeRouterOS:
    """维护各分区状态的假 REST 服务；PUT 真正按 place-before 插入。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self, fail_on_put=None, unreadable=()) -> None:
        self.state = {section: [] for section in tplmod.SECTIONS}
        # 用户手工规则：任何部署/回滚都不能动它们
        self.state["/rest/ip/firewall/filter"] = [
            {".id": "*1", "chain": "input", "action": "accept",
             "connection-state": "established,related", "comment": "手工：放行已建立连接"},
            {".id": "*2", "chain": "forward", "action": "accept",
             "connection-state": "established,related", "comment": "手工：放行已建立连接"},
            {".id": "*3", "chain": "forward", "action": "drop",
             "in-interface-list": "!LAN", "comment": "手工：defconf 兜底 drop"},
        ]
        self.state["/rest/ipv6/firewall/filter"] = [
            {".id": "*1", "chain": "forward", "action": "drop", "comment": "手工：IPv6 兜底"},
        ]
        self.state["/rest/ip/firewall/mangle"] = [
            {".id": "*1", "chain": "forward", "action": "passthrough", "comment": "手工：mangle 首条"},
        ]
        self.next_id = 10
        self.put_count = 0
        self.fail_on_put = fail_on_put
        self.unreadable = set(unreadable)
        self.deletes = []
        self.backups = []
        AsyncRequestUtils.calls = []
        AsyncRequestUtils.responder = self.respond

    def respond(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        path = urlparse(url).path
        if method == "GET" and path == "/rest/system/identity":
            return FakeResponse(200, {"name": "MikroTik", "version": "7.15.3"})
        if method == "POST" and path == "/rest/system/backup/save":
            self.backups.append(str((kwargs.get("json") or {}).get("name") or ""))
            return FakeResponse(200, {})
        # 只模拟「读权限不足」：写入仍可用，用于验证现状比对降级的路径
        if path in self.unreadable and method == "GET":
            return FakeResponse(403, {"detail": "permission denied"})
        if method == "GET":
            if path in self.state:
                return FakeResponse(200, [dict(r) for r in self.state[path]])
            return FakeResponse(404, {"detail": "no such section"})
        if method == "PUT":
            self.put_count += 1
            if self.fail_on_put == self.put_count:
                return FakeResponse(400, {"detail": "模拟写入失败"})
            if path not in self.state:
                return FakeResponse(404, {"detail": "no such section"})
            body = dict(kwargs.get("json") or {})
            place = body.pop("place-before", "")
            rid = f"*{self.next_id}"
            self.next_id += 1
            row = dict(body)
            row[".id"] = rid
            rows = self.state[path]
            if place and place != "*ffffffff":
                idx = next((i for i, r in enumerate(rows) if r.get(".id") == place), None)
                rows.insert(idx, row) if idx is not None else rows.append(row)
            else:
                rows.append(row)
            return FakeResponse(201, {"ret": rid})
        if method == "DELETE":
            base, rid = path.rsplit("/", 1)
            if base in self.state:
                before = len(self.state[base])
                self.state[base] = [r for r in self.state[base] if r.get(".id") != rid]
                if len(self.state[base]) < before:
                    self.deletes.append((base, rid))
                    return FakeResponse(204, {})
            return FakeResponse(404, {"detail": "not found"})
        return FakeResponse(405, {"detail": "unexpected"})

    def tagged_rows(self) -> list:
        return [r for rows in self.state.values() for r in rows
                if "MP-A2[" in str(r.get("comment") or "")]

    def manual_rows(self) -> list:
        return [r for rows in self.state.values() for r in rows
                if "手工" in str(r.get("comment") or "")]


FAKE = FakeRouterOS()


def default_params() -> dict:
    return {key: next(p.default for t in tplmod.TEMPLATES
                      for p in t.params if p.key == key)
            for key in tplmod.all_param_keys()}


def base_config(**overrides) -> dict:
    cfg = {
        "ros_connection": "", "router_address": "192.168.88.1", "router_port": 443,
        "router_ssl": True, "router_username": "mp-api", "router_password": "secret",
        "timeout": 10, "verify": False,
        "templates": ["pt_qos"], "comment_prefix": "MP-A2",
        "allow_high_risk": True, "dry_run": False,
        "auto_backup": True, "require_backup": True, "auto_rollback": True,
        "notify_mode": "issue", "display_limit": 60,
    }
    for key, value in default_params().items():
        cfg[f"param_{key}"] = value
    cfg["param_media_ip"] = "192.168.88.10"
    cfg.update(overrides)
    return cfg


def make_plugin(**overrides) -> RouterOSScenarioTemplates:
    plugin = RouterOSScenarioTemplates()
    plugin.init_plugin(base_config(**overrides))
    return plugin


def all_ids() -> list:
    return [t.id for t in tplmod.TEMPLATES]


# ---------------------------------------------------------------------------
# 模板层
# ---------------------------------------------------------------------------

def test_all_templates_render() -> None:
    FAKE.reset()
    plan = tplmod.render_plan(all_ids(), default_params() | {"media_ip": "192.168.88.10"},
                              allow_high_risk=True)
    assert not plan["blocked"], plan["errors"]
    assert len(plan["templates"]) == 5
    assert len(plan["actions"]) == 21
    assert len(plan["sections"]) == 10
    tags = [a["tag"] for a in plan["actions"]]
    assert len(set(tags)) == len(tags), "标签必须全局唯一，否则回滚会误删"
    for a in plan["actions"]:
        assert a["section"] in tplmod.SECTIONS
        assert a["payload"]["comment"].startswith("MP-A2[")
        assert a["meaning"] and a["risk"] != "（无）"
        assert a["command"].startswith(tplmod.SECTIONS[a["section"]]["cli"] + " add")
    # 远程观影必填参数被替换、可选 hairpin 步骤存在
    remote = [a for a in plan["actions"] if a["template_id"] == "remote_stream"]
    assert len(remote) == 3
    assert any("192.168.88.10" in str(a["payload"].get("to-addresses")) for a in remote)
    # IPv6 规则顺序：ICMPv6 放行在 input 兜底 drop 之前
    v6 = [a for a in plan["actions"] if a["template_id"] == "ipv6_basic"]
    assert v6[2]["payload"]["protocol"] == "icmpv6"
    assert v6[-1]["payload"]["action"] == "drop"
    assert all(a["placement"] == tplmod.PLACE_BEFORE_DROP
               for a in v6[2:] if a["section"] == "/rest/ipv6/firewall/filter")


def test_param_validation() -> None:
    FAKE.reset()
    vals = default_params() | {"media_ip": "192.168.88.10"}
    bad_cases = [
        ("remote_stream", {"media_ip": ""}),
        ("remote_stream", {"wan_port": "99999"}),
        ("remote_stream", {"wan_if": "ether1;/system reboot"}),
        ("pt_qos", {"pt_up_limit": "30Mbps?"}),
        ("pt_qos", {"pt_hosts": "not-an-ip"}),
        ("wireguard_home", {"peer_public_key": "abc"}),
        ("ipv6_basic", {"v6_prefix_len": "999"}),
    ]
    for tid, patch in bad_cases:
        plan = tplmod.render_plan([tid], vals | patch, allow_high_risk=True)
        assert plan["blocked"], f"{tid} {patch} 应被拦截"
        assert plan["errors"]
    # 控制字符清洗：不进入 payload
    plan = tplmod.render_plan(["remote_stream"], vals | {"media_ip": "192.168.88.10\n\r"},
                              allow_high_risk=True)
    assert not plan["blocked"]
    assert "\n" not in plan["actions"][0]["payload"]["to-addresses"]


def test_optional_step_and_high_risk_guard() -> None:
    FAKE.reset()
    vals = default_params() | {"media_ip": "192.168.88.10"}
    blank = tplmod.render_plan(["wireguard_home"], vals, allow_high_risk=True)
    filled = tplmod.render_plan(["wireguard_home"], vals | {"peer_public_key": WG_KEY},
                                allow_high_risk=True)
    assert len(blank["actions"]) == 5, "公钥留空应跳过 Peer 步骤"
    assert len(filled["actions"]) == 6
    assert filled["actions"][-1]["section"] == "/rest/interface/wireguard/peers"

    guarded = tplmod.render_plan(["remote_stream", "ipv6_basic", "wireguard_home"],
                                 vals, allow_high_risk=False)
    assert guarded["blocked"]
    assert sum(1 for e in guarded["errors"] if "高危模板" in e) == 3
    safe = tplmod.render_plan(["pt_qos", "guest_isolation"], vals, allow_high_risk=False)
    assert not safe["blocked"]


def test_place_before_logic() -> None:
    FAKE.reset()
    rows = FAKE.state["/rest/ip/firewall/filter"]
    drop = tplmod.DeployAction(section="/rest/ip/firewall/filter",
                               placement=tplmod.PLACE_BEFORE_DROP, chain="forward",
                               payload={"chain": "forward"})
    place, note = engine.choose_place_before(drop, rows)
    assert place == "*3" and "兜底" in note, (place, note)

    top = tplmod.DeployAction(section="/rest/ip/firewall/mangle",
                              placement=tplmod.PLACE_TOP, chain="forward",
                              payload={"chain": "forward"})
    place, note = engine.choose_place_before(top, FAKE.state["/rest/ip/firewall/mangle"])
    assert place == "*1"

    append = tplmod.DeployAction(section="/rest/queue/simple",
                                 placement=tplmod.PLACE_BEFORE_DROP,
                                 payload={"name": "x"})
    assert engine.choose_place_before(append, [])[0] == "", "非规则分区不应插入排序"
    plain = tplmod.DeployAction(section="/rest/ip/firewall/filter",
                                placement=tplmod.PLACE_APPEND, payload={})
    assert engine.choose_place_before(plain, rows)[0] == ""


# ---------------------------------------------------------------------------
# 预览 / 幂等
# ---------------------------------------------------------------------------

def test_preview_marks_existing_as_skipped() -> None:
    FAKE.reset()
    FAKE.state["/rest/ip/firewall/address-list"].append({
        ".id": "*99", "address": "192.168.88.50", "list": "MP-A2-PT-HOSTS",
        "comment": "MP-A2[pt_qos:1] PT 友好 QoS",
    })
    plugin = make_plugin(templates=["pt_qos"])
    result = plugin.api_preview()
    assert result.success, result.message
    plan = result.data
    assert plan["actions"][0]["state"] == "exists"
    assert plan["summary"]["exists"] == 1
    assert plan["summary"]["to_write"] == 2
    assert plan["refreshed"] and not plan.get("offline")

    deploy = plugin.api_apply()
    assert deploy.success, deploy.message
    assert deploy.data["skipped"] == 1
    assert deploy.data["written"] == 2
    # 3 条带标签条目 = 1 条预置 + 2 条本次新建
    assert len(FAKE.tagged_rows()) == 3
    assert sum(1 for r in FAKE.tagged_rows() if r[".id"] != "*99") == 2


def test_preview_reports_unreadable_section() -> None:
    FAKE.reset(unreadable={"/rest/queue/simple"})
    plugin = make_plugin(templates=["pt_qos"])
    plan = plugin.api_preview().data
    assert "/rest/queue/simple" in plan["unreadable"]
    # 幂等检查不完整但部署仍可继续（不误判成「已存在」）
    deploy = plugin.api_apply()
    assert deploy.success, deploy.message
    assert deploy.data["written"] == 3


# ---------------------------------------------------------------------------
# 部署 / 备份 / 回滚
# ---------------------------------------------------------------------------

def test_deploy_success_with_backup_and_snapshot() -> None:
    FAKE.reset()
    plugin = make_plugin(templates=["pt_qos"])
    assert plugin.api_preview().success
    result = plugin.api_apply()
    assert result.success, result.message
    report = result.data
    assert report["written"] == 3 and report["failed"] == 0
    assert report["backup"]["ok"] and len(FAKE.backups) == 1
    assert report["snapshot"]["path"] and Path(report["snapshot"]["path"]).is_file()
    snap = json.loads(Path(report["snapshot"]["path"]).read_text(encoding="utf-8"))
    # 快照只覆盖本计划涉及的分区：pt_qos 涉及 mangle（预置了 1 条手工规则）
    assert "/rest/ip/firewall/mangle" in snap["sections"]
    assert any("手工" in str(r.get("comment") or "")
               for r in snap["sections"]["/rest/ip/firewall/mangle"])
    assert snap["count"] >= 1
    assert len(report["created"]) == 3
    assert all(item["id"].startswith("*") for item in report["created"])
    # mangle 标记规则按 PLACE_TOP 插到链首
    assert FAKE.state["/rest/ip/firewall/mangle"][0]["comment"].startswith("MP-A2[pt_qos:3]")
    assert len(FAKE.manual_rows()) == 5, "手工规则不得被改动"
    assert len(FAKE.tagged_rows()) == 3
    # 成功后清空计划，避免误重复部署
    assert not plugin.api_plan().data.get("actions")


def test_deploy_failure_auto_rollback() -> None:
    FAKE.reset(fail_on_put=3)
    plugin = make_plugin(templates=["pt_qos"])
    plugin.api_preview()
    result = plugin.api_apply()
    assert not result.success
    report = result.data
    assert report["failed"] == 1
    assert report["rollback"]["done"] is True
    assert report["rollback"]["ok_count"] == 2
    assert report["created"] == []
    assert len(FAKE.deletes) == 2
    assert not FAKE.tagged_rows(), "失败后不应留下任何半套配置"
    assert len(FAKE.manual_rows()) == 5
    assert plugin.api_plan().data.get("actions"), "失败后应保留计划便于修正重试"


def test_manual_rollback_only_removes_own_entries() -> None:
    FAKE.reset()
    plugin = make_plugin(templates=["pt_qos"])
    plugin.api_preview()
    assert plugin.api_apply().success
    assert not FAKE.deletes
    result = plugin.api_rollback()
    assert result.success, result.message
    assert len(FAKE.deletes) == 3
    assert not FAKE.tagged_rows()
    assert len(FAKE.manual_rows()) == 5
    assert plugin.api_rollback().success is False, "已清空后不应重复回滚"


def test_rollback_disabled_continues_after_failure() -> None:
    FAKE.reset(fail_on_put=2)
    plugin = make_plugin(templates=["pt_qos"], auto_rollback=False)
    plugin.api_preview()
    report = plugin.api_apply().data
    assert not report["ok"]
    assert report["rollback"]["done"] is False
    assert len(FAKE.tagged_rows()) == 2, "关闭自动回滚时保留已写入的条目"
    assert len(FAKE.deletes) == 0


def test_backup_failure_aborts_before_writes() -> None:
    FAKE.reset()

    def blocking_responder(method, url, kwargs):
        if method == "POST" and urlparse(url).path == "/rest/system/backup/save":
            return FakeResponse(500, {"detail": "backup failed"})
        return FAKE.respond(method, url, kwargs)

    AsyncRequestUtils.responder = blocking_responder
    plugin = make_plugin(templates=["pt_qos"])
    plugin.api_preview()
    result = plugin.api_apply()
    assert not result.success and "备份失败" in result.message
    assert not FAKE.tagged_rows(), "备份失败必须中止，不得写入任何配置"
    assert FAKE.put_count == 0


def test_dry_run_never_writes() -> None:
    FAKE.reset()
    plugin = make_plugin(templates=["pt_qos"], dry_run=True)
    plugin.api_preview()
    result = plugin.api_apply()
    assert result.success, result.message
    assert result.data["dry_run"] is True
    assert not any(m == "PUT" for m, _, _ in AsyncRequestUtils.calls)
    assert not any(m == "POST" for m, _, _ in AsyncRequestUtils.calls)
    assert not FAKE.tagged_rows()
    assert plugin.api_plan().data.get("actions"), "试运行后计划应保留，便于真正部署"


# ---------------------------------------------------------------------------
# API / 页面 / 命令
# ---------------------------------------------------------------------------

def test_apply_guards() -> None:
    FAKE.reset()
    plugin = make_plugin(templates=["pt_qos"])
    res = plugin.api_apply()
    assert not res.success and "预览" in res.message

    blocked = make_plugin(templates=["remote_stream"], allow_high_risk=False)
    blocked.api_preview()
    res = blocked.api_apply()
    assert not res.success and "校验" in res.message

    offline = make_plugin(router_address="")
    offline.api_preview()
    res = offline.api_apply()
    assert not res.success and "网络模块" in res.message


def test_backup_and_status_apis() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert plugin.api_test().success
    backup = plugin.api_backup()
    assert backup.success, backup.message
    assert len(FAKE.backups) == 1
    status = plugin.api_status().data
    assert status["plugin"] == "RouterOSScenarioTemplates"
    assert status["snapshots"] and status["plan"]["templates"] == ["pt_qos"]
    assert plugin.api_logs().success
    assert plugin.api_clear().success
    assert not plugin.api_plan().data
    assert len(plugin.get_command()) == 4


def test_form_page_buttons_and_escaping() -> None:
    FAKE.reset()
    plugin = make_plugin(templates=all_ids())
    form, defaults = plugin.get_form()
    assert form and "param_media_ip" in defaults
    assert defaults["param_media_ip"] == "", "必填项默认留空，由用户填写"
    assert defaults["param_wg_port"] == "13231"
    assert defaults["comment_prefix"] == "MP-A2"

    plugin.api_preview()
    page = plugin.get_page()
    assert page
    buttons = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("component") == "VBtn":
                buttons.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(page)
    assert len(buttons) >= 7
    for button in buttons:
        click = button["events"]["click"]
        assert click["api"].startswith("plugin/RouterOSScenarioTemplates/")
        assert click["method"] in ("get", "post")
        assert button["props"].get("disabled") is not True, "不允许出现点了没反应的按钮"

    texts = []

    def collect(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                texts.append(node["text"])
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(page)
    assert not any("[" in t and "MP-A2" in t for t in texts), "标签须转全角，避免 Markdown 空药丸"
    api_paths = [x["path"] for x in plugin.get_api()]
    assert len(api_paths) == len(set(api_paths))


def test_remote_command_replies() -> None:
    FAKE.reset()
    plugin = make_plugin()
    _PluginBase.POSTED = []
    plugin.handle_command(Event(event_type="plugin.action",
                                event_data={"cmd": "/a2_status", "channel": "telegram"}))
    assert _PluginBase.POSTED
    assert "待确认" in str(_PluginBase.POSTED[-1].get("text") or "")
