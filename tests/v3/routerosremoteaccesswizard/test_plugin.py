"""RouterOS 外网访问向导（B1）离线用例。

用带状态的假 RouterOS REST 服务验证端到端行为：服务发现 → 预览 → 批量写入
→ 备份/快照 → 失败逆序回滚 → 手动回滚 → 连通性测试，并确认手工规则不被删改。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from app.plugins.routerosremoteaccesswizard import (RouterOSRemoteAccessWizard,
                                                    discovery, planner, probe)
from app.sdk.events import Event
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase

MANUAL_SERVICES = ("客厅Emby|http://192.168.88.10:8096||18096|\n"
                   "QB|192.168.88.20|8080||")


class FakeRouterOS:
    """维护 nat / filter 两张表状态的假 REST 服务；PUT 真正按 place-before 插入。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self, fail_on_put=None, unreadable=()) -> None:
        self.state = {section: [] for section in planner.SECTIONS}
        # 用户手工规则：任何部署 / 回滚都不能动它们
        self.state[planner.SEC_FILTER] = [
            {".id": "*1", "chain": "forward", "action": "accept",
             "connection-state": "established,related", "comment": "手工：放行已建立连接"},
            {".id": "*2", "chain": "forward", "action": "drop",
             "in-interface-list": "!LAN", "comment": "手工：defconf 兜底 drop"},
        ]
        self.state[planner.SEC_NAT] = [
            {".id": "*1", "chain": "srcnat", "action": "masquerade",
             "out-interface": "ether1", "comment": "手工：上网 masquerade"},
        ]
        self.next_id = 10
        self.put_count = 0
        self.fail_on_put = fail_on_put
        self.unreadable = set(unreadable)
        self.probe_fail_hosts = set()
        self.deletes = []
        self.backups = []
        AsyncRequestUtils.calls = []
        AsyncRequestUtils.responder = self.respond

    def respond(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        parsed = urlparse(url)
        path = parsed.path
        # 连通性测试请求不走 /rest
        if not path.startswith("/rest"):
            if parsed.hostname in self.probe_fail_hosts:
                raise RuntimeError("connection refused")
            return FakeResponse(200, {"ok": True})
        if method == "GET" and path == "/rest/system/identity":
            return FakeResponse(200, {"name": "MikroTik", "version": "7.15.3"})
        if method == "POST" and path == "/rest/system/backup/save":
            self.backups.append(str((kwargs.get("json") or {}).get("name") or ""))
            return FakeResponse(200, {})
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
            idx = next((i for i, r in enumerate(rows) if r.get(".id") == place), None) if place else None
            rows.insert(idx, row) if idx is not None else rows.append(row)
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
                if "MP-B1[" in str(r.get("comment") or "")]

    def manual_rows(self) -> list:
        return [r for rows in self.state.values() for r in rows
                if "手工" in str(r.get("comment") or "")]

    def add_occupied_nat(self, port: int) -> None:
        self.state[planner.SEC_NAT].append({
            ".id": "*9", "chain": "dstnat", "action": "dst-nat",
            "dst-port": str(port), "protocol": "tcp",
            "comment": "手工：已有的映射",
        })


FAKE = FakeRouterOS()


def base_config(**overrides) -> dict:
    cfg = {
        "ros_connection": "", "router_address": "192.168.88.1", "router_port": 443,
        "router_ssl": True, "router_username": "mp-api", "router_password": "secret",
        "timeout": 10, "verify": False,
        "connected_services": [], "services_manual": MANUAL_SERVICES,
        "mp_host": "", "mp_port": 3000, "mp_scheme": "http",
        "wan_if": "ether1", "protocol": "tcp", "wan_port_start": 18000,
        "lan_subnet": "", "domain": "movie.example.com", "public_host": "",
        "probe_timeout": 5,
        "allow_public": True, "dry_run": False,
        "auto_backup": True, "require_backup": True, "auto_rollback": True,
        "comment_prefix": "MP-B1", "notify_mode": "issue", "display_limit": 60,
    }
    cfg.update(overrides)
    return cfg


def make_plugin(**overrides) -> RouterOSRemoteAccessWizard:
    plugin = RouterOSRemoteAccessWizard()
    plugin.init_plugin(base_config(**overrides))
    return plugin


def opts(**overrides) -> dict:
    data = {"wan_if": "ether1", "protocol": "tcp", "lan_subnet": "",
            "domain": "movie.example.com", "public_host": "",
            "wan_port_start": 18000, "probe_timeout": 5}
    data.update(overrides)
    return data


def sample_services():
    return discovery.parse_manual_services(MANUAL_SERVICES)[0]


def preview_and_apply(plugin):
    preview = plugin.api_preview()
    assert preview.success, preview.message
    return plugin.api_apply()


def walk(node):
    """递归遍历详情页组件树，取出所有按钮绑定的 api 路径。"""
    found = []
    if isinstance(node, dict):
        events = node.get("events") or {}
        click = events.get("click") or {}
        if click.get("api"):
            found.append(click["api"])
        for value in node.values():
            found.extend(walk(value))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(walk(item))
    return found


# ---------------------------------------------------------------------------
# 服务发现
# ---------------------------------------------------------------------------

def test_service_discovery_from_helpers() -> None:
    media, downloaders = discovery.load_connected_configs()
    items = discovery.service_items(media, downloaders)
    values = [x["value"] for x in items]
    assert "mp" in values and "media:客厅Emby" in values and "downloader:QB" in values
    services, errors = discovery.discover_services(
        ["mp", "media:客厅Emby", "downloader:QB"], "",
        media_configs=media, downloader_configs=downloaders,
        mp_host="192.168.88.5", mp_port=3000)
    assert not errors, errors
    assert len(services) == 3
    by_name = {s.name: s for s in services}
    assert by_name["客厅Emby"].lan_ip == "192.168.88.10"
    assert by_name["客厅Emby"].lan_port == 8096
    assert by_name["QB"].lan_port == 8080
    assert by_name["MoviePilot"].lan_ip == "192.168.88.5"


def test_manual_service_parsing() -> None:
    services, errors = discovery.parse_manual_services(MANUAL_SERVICES)
    assert not errors, errors
    assert services[0].wan_port == 18096 and services[0].lan_port == 8096
    assert services[1].wan_port == 0 and services[1].lan_port == 8080
    assert services[0].id == "emby-8096", "标签 id 要稳定，重排服务不能变"
    bad, errs = discovery.parse_manual_services("坏行\nA|1.2.3.4|abc||")
    assert not bad and errs, "非法端口应该被拒绝并给出错误"


def test_service_dropdown_in_form() -> None:
    plugin = make_plugin()
    content, defaults = plugin.get_form()
    blob = json.dumps(content, ensure_ascii=False)
    assert "已连接服务（可多选）" in blob
    assert "客厅Emby · 媒体服务器 192.168.88.10:8096" in blob
    assert defaults["comment_prefix"] == "MP-B1"
    assert defaults["wan_port_start"] == 18000
    for phrase in ("插件用途", "这个插件解决什么", "两点", "使用说明", "三步用起来"):
        assert phrase not in blob, f"设置页不应出现 AI 味标题：{phrase}"


# ---------------------------------------------------------------------------
# 计划渲染与校验
# ---------------------------------------------------------------------------

def test_plan_render_two_services() -> None:
    plan = planner.render_plan(sample_services(), opts(), allow_public=True)
    assert not plan["blocked"], plan["errors"]
    assert plan["summary"]["services"] == 2
    assert plan["summary"]["actions"] == 4
    nat = [a for a in plan["actions"] if a["section"] == planner.SEC_NAT]
    fw = [a for a in plan["actions"] if a["section"] == planner.SEC_FILTER]
    assert len(nat) == 2 and len(fw) == 2
    assert nat[0]["payload"]["dst-port"] == "18096"
    assert nat[0]["payload"]["to-addresses"] == "192.168.88.10"
    assert nat[1]["payload"]["dst-port"] == "18000", "没指定外网端口的应自动分配"
    assert all(a["chain"] == "forward" for a in fw)
    assert all(a["placement"] == planner.PLACE_BEFORE_DROP for a in fw)
    assert "18096 → 客厅Emby" in plan["services"][0]["mapping"]
    assert "movie.example.com" in nat[0]["payload"]["comment"], "域名要写进备注"


def test_hairpin_and_protocol() -> None:
    plan = planner.render_plan(sample_services(),
                               opts(lan_subnet="192.168.88.0/24", protocol="both"),
                               allow_public=True)
    assert not plan["blocked"], plan["errors"]
    assert plan["summary"]["actions"] == 6
    hairpins = [a for a in plan["actions"] if ":hairpin]" in a["tag"]]
    assert len(hairpins) == 2
    assert hairpins[0]["payload"]["chain"] == "srcnat"
    assert hairpins[0]["placement"] == planner.PLACE_TOP
    assert all("protocol" not in a["payload"] for a in plan["actions"]), "both 不应写 protocol"


def test_public_guard_blocks() -> None:
    plan = planner.render_plan(sample_services(), opts(), allow_public=False)
    assert plan["blocked"] and not plan["actions"]
    assert any("允许公网映射" in e for e in plan["errors"])
    plugin = make_plugin(allow_public=False)
    preview = plugin.api_preview()
    assert not preview.success
    assert not plugin.api_apply().success
    assert FAKE.put_count == 0


def test_validation_rejects_bad_values() -> None:
    cases = [
        opts(wan_if="ether1;drop all"),
        opts(lan_subnet="不是网段"),
        opts(domain="http://bad host"),
        opts(wan_port_start=70000),
        opts(protocol="sctp"),
    ]
    for bad in cases:
        plan = planner.render_plan(sample_services(), bad, allow_public=True)
        assert plan["blocked"], f"应该被拦截：{bad}"
    svc = sample_services()
    svc[0].lan_ip = "emby.local"
    assert planner.render_plan(svc, opts(), allow_public=True)["blocked"], "域名不能当 to-addresses"
    dup = discovery.parse_manual_services("A|192.168.1.2|80|19000|\nB|192.168.1.3|81|19000|")[0]
    assert any("重复使用" in e for e in
               planner.render_plan(dup, opts(), allow_public=True)["errors"])


def test_port_allocation_skips_occupied() -> None:
    plan = planner.render_plan(sample_services(), opts(), allow_public=True,
                               occupied_ports=[18000, 18001])
    assert not plan["blocked"], plan["errors"]
    ports = [s["wan_port"] for s in plan["services"]]
    assert ports == [18096, 18002], ports
    assert any("已有别的 NAT 规则" in w for w in plan["warnings"]) is False


def test_occupied_ports_from_existing_rules() -> None:
    rows = [
        {"chain": "dstnat", "dst-port": "18096", "comment": "手工：已有映射"},
        {"chain": "dstnat", "dst-port": "18100", "comment": "MP-B1[x:nat] 自己的"},
        {"chain": "srcnat", "dst-port": "18200", "comment": "手工：源地址转换"},
    ]
    assert planner.occupied_ports_from_rows(rows, "MP-B1") == [18096]


# ---------------------------------------------------------------------------
# 预览 / 部署 / 回滚
# ---------------------------------------------------------------------------

def test_preview_refreshes_and_marks_exists() -> None:
    FAKE.reset()
    plugin = make_plugin()
    preview = plugin.api_preview()
    assert preview.success, preview.message
    plan = preview.data
    assert plan["refreshed"] and not plan.get("offline")
    assert plan["summary"]["exists"] == 0
    assert len(plan["actions"]) == 4
    deploy = plugin.api_apply()
    assert deploy.success, deploy.message
    again = plugin.api_preview()
    assert again.success
    assert again.data["summary"]["exists"] == 4, "已有同标签规则应全部标成 exists"
    assert all(a["state"] == "exists" for a in again.data["actions"])


def test_apply_writes_nat_and_filter() -> None:
    FAKE.reset()
    plugin = make_plugin()
    deploy = preview_and_apply(plugin)
    assert deploy.success, deploy.message
    assert deploy.data["written"] == 4
    assert len(FAKE.tagged_rows()) == 4
    assert len(FAKE.manual_rows()) == 3
    dstnat = [r for r in FAKE.state[planner.SEC_NAT]
              if r.get("chain") == "dstnat" and "MP-B1[" in str(r.get("comment"))]
    assert len(dstnat) == 2
    fw = FAKE.state[planner.SEC_FILTER]
    tagged_idx = next(i for i, r in enumerate(fw) if "MP-B1[" in str(r.get("comment")))
    drop_idx = next(i for i, r in enumerate(fw) if "兜底 drop" in str(r.get("comment")))
    assert tagged_idx < drop_idx, "放行规则必须排在兜底 drop 之前"
    assert FAKE.backups, "部署前要在 RouterOS 留备份"


def test_idempotent_skips_existing_tags() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert preview_and_apply(plugin).success
    before = len(FAKE.tagged_rows())
    assert plugin.api_preview().success
    second = plugin.api_apply()
    assert second.success, second.message
    assert second.data["skipped"] == 4 and second.data["written"] == 0
    assert len(FAKE.tagged_rows()) == before, "重复部署不应堆积重复规则"


def test_failure_auto_rollback() -> None:
    FAKE.reset(fail_on_put=2)
    plugin = make_plugin()
    deploy = preview_and_apply(plugin)
    assert not deploy.success
    assert FAKE.put_count == 2, "遇错即停"
    assert len(FAKE.tagged_rows()) == 0, "已写入的条目要逆序回滚掉"
    assert len(FAKE.deletes) == 1
    assert len(FAKE.manual_rows()) == 3, "手工规则不能被动"
    assert deploy.data["rollback"]["ok_count"] == 1


def test_manual_rollback_keeps_manual_rules() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert preview_and_apply(plugin).success
    assert len(FAKE.tagged_rows()) == 4
    result = plugin.api_rollback()
    assert result.success, result.message
    assert len(FAKE.tagged_rows()) == 0
    assert len(FAKE.manual_rows()) == 3
    assert not plugin.api_rollback().success, "回滚后不应留可重复回滚的条目"


def test_dry_run_no_writes() -> None:
    FAKE.reset()
    plugin = make_plugin(dry_run=True)
    deploy = preview_and_apply(plugin)
    assert deploy.success, deploy.message
    assert deploy.data["dry_run"] is True
    assert FAKE.put_count == 0 and not FAKE.backups
    assert len(FAKE.tagged_rows()) == 0


def test_offline_preview() -> None:
    FAKE.reset()
    plugin = make_plugin(router_address="")
    preview = plugin.api_preview()
    assert preview.success, preview.message
    assert preview.data["offline"] is True
    assert len(preview.data["unreadable"]) == 2
    assert FAKE.put_count == 0


def test_preview_skips_occupied_wan_port() -> None:
    FAKE.reset()
    FAKE.add_occupied_nat(18000)
    plugin = make_plugin()
    preview = plugin.api_preview()
    assert preview.success, preview.message
    ports = [s["wan_port"] for s in preview.data["services"]]
    assert ports == [18096, 18001], "自动分配要避开已有 NAT 占用的端口"


# ---------------------------------------------------------------------------
# 连通性测试 / 备份 / 页面 / API
# ---------------------------------------------------------------------------

def test_probe_lan_and_wan() -> None:
    FAKE.reset()
    plugin = make_plugin()
    result = plugin.api_probe()
    assert result.success, result.message
    assert result.data["total"] == 4
    assert result.data["lan_ok"] == 2 and result.data["wan_ok"] == 2
    assert any("movie.example.com:18096" in t["url"]
               for t in result.data["details"])

    FAKE.probe_fail_hosts.add("movie.example.com")
    failed = plugin.api_probe()
    assert not failed.success
    assert failed.data["lan_ok"] == 2 and failed.data["wan_ok"] == 0
    assert failed.data["fail_count"] == 2
    assert any("Hairpin" in n for n in failed.data["notes"])


def test_backup_and_snapshot() -> None:
    FAKE.reset()
    plugin = make_plugin()
    result = plugin.api_backup()
    assert result.success, result.message
    assert FAKE.backups
    path = Path(result.data["snapshot"])
    assert path.exists()
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert snap["count"] >= 3, "快照应包含部署前的手工规则"
    assert planner.SEC_NAT in snap["sections"]


def test_page_buttons_bind_real_apis() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert plugin.api_preview().success
    buttons = walk(plugin.get_page())
    assert len(buttons) >= 8
    registered = {f"plugin/RouterOSRemoteAccessWizard{a['path']}"
                  for a in plugin.get_api()}
    for api in buttons:
        assert api in registered, f"按钮绑到了不存在的 API：{api}"


def test_api_and_commands() -> None:
    FAKE.reset()
    plugin = make_plugin()
    paths = {a["path"] for a in plugin.get_api()}
    assert {"/preview", "/apply", "/rollback", "/probe", "/services",
            "/backup", "/test", "/status", "/logs", "/plan", "/clear"} <= paths
    cmds = {c["cmd"] for c in plugin.get_command()}
    assert {"/b1_preview", "/b1_apply", "/b1_probe", "/b1_rollback", "/b1_status"} <= cmds
    assert plugin.api_status().data["plugin"] == "RouterOSRemoteAccessWizard"
    assert plugin.api_services().data["items"]
    assert plugin.api_logs().success

    _PluginBase.POSTED.clear()
    event = Event(event_type="plugin.action",
                  event_data={"cmd": "/b1_status", "channel": "tg", "userid": "1"})
    plugin.handle_command(event)
    assert _PluginBase.POSTED, "远程命令要回消息"
    assert "服务" in _PluginBase.POSTED[-1]["text"]


def test_status_and_report_after_deploy() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert preview_and_apply(plugin).success
    status = plugin.api_status().data
    assert status["last_deploy"]["ok"] is True
    assert status["dry_run"] is False
    assert len(status["services"]) == 2
    blob = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "待确认的计划" in blob
    assert "最近一次部署报告" not in blob
    assert "外网端口 → 内网服务" not in blob
