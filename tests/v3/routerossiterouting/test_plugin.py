"""RouterOS 站点智能分流 / DNS（B3）离线用例。

用带状态的假 RouterOS REST 服务验证端到端行为：站点数据读取 → 预览 → 批量写入
→ 备份/快照 → 失败逆序回滚 → 手动回滚 → 失效条目清理 → 定时增量同步，
并确认手工规则始终不被删改。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from app.plugins.routerossiterouting import (RouterOSSiteRouting, planner, sites)
from app.sdk.events import Event
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase

MANUAL_SITES = (
    "馒头|https://pt.mantou.example|tracker.mantou.example,api.mantou.example|主站\n"
    "猫站|pt.cat.example:8080/x|tracker.cat.example;tracker2.cat.example|"
)


class FakeRouterOS:
    """维护 5 张表状态的假 REST 服务；PUT 真正按 place-before 插入。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self, fail_on_put=None, unreadable=(), fail_backup=False) -> None:
        self.state = {section: [] for section in planner.SECTIONS}
        # 用户手工规则：任何部署 / 回滚 / 清理都不能动它们
        self.state[planner.SEC_DNS] = [
            {".id": "*1", "name": "cf.manual.example", "address": "9.9.9.9",
             "type": "A", "comment": "手工：CF 优选 IP"},
        ]
        self.state[planner.SEC_ADDR] = [
            {".id": "*1", "list": "manual-list", "address": "manual.example",
             "comment": "手工：别人的地址列表"},
            {".id": "*2", "list": "manual-list", "address": "keep.example",
             "comment": "手工：提到 MP-B3 但没标签"},
        ]
        self.state[planner.SEC_MANGLE] = [
            {".id": "*1", "chain": "prerouting", "action": "mark-connection",
             "comment": "手工：已有的标记"},
        ]
        self.state[planner.SEC_ROUTE] = [
            {".id": "*1", "dst-address": "0.0.0.0/0", "gateway": "192.168.88.1",
             "comment": "手工：默认路由"},
        ]
        self.state[planner.SEC_FILTER] = [
            {".id": "*1", "chain": "forward", "action": "accept",
             "connection-state": "established,related", "comment": "手工：放行已建立连接"},
            {".id": "*2", "chain": "forward", "action": "drop",
             "in-interface-list": "!LAN", "comment": "手工：兜底 drop"},
        ]
        self.next_id = 10
        self.put_count = 0
        self.fail_on_put = fail_on_put
        self.unreadable = set(unreadable)
        self.fail_backup = fail_backup
        self.deletes = []
        self.backups = []
        AsyncRequestUtils.calls = []
        AsyncRequestUtils.responder = self.respond

    def respond(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        path = urlparse(url).path
        if method == "GET" and path == "/rest/system/identity":
            return FakeResponse(200, {"name": "MikroTik", "version": "7.15.3"})
        if method == "POST" and path == "/rest/system/backup/save":
            if self.fail_backup:
                return FakeResponse(500, {"detail": "模拟备份失败"})
            self.backups.append(str((kwargs.get("json") or {}).get("name") or ""))
            return FakeResponse(200, {})
        if not path.startswith("/rest"):
            return FakeResponse(200, {"ok": True})
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

    def all_rows(self) -> list:
        return [r for rows in self.state.values() for r in rows]

    def tagged_rows(self) -> list:
        return [r for r in self.all_rows() if "MP-B3[" in str(r.get("comment") or "")]

    def manual_rows(self) -> list:
        return [r for r in self.all_rows() if "手工" in str(r.get("comment") or "")]

    def add_stale_row(self, tag: str) -> None:
        self.state[planner.SEC_DNS].append({
            ".id": "*99", "name": "old.example", "address": "1.1.1.1",
            "type": "A", "comment": f"{tag} 旧站点的解析",
        })


FAKE = FakeRouterOS()


def base_config(**overrides) -> dict:
    cfg = {
        "enabled": True, "onlyonce": False,
        "ros_connection": "", "router_address": "192.168.88.1", "router_port": 443,
        "router_ssl": True, "router_username": "mp-api", "router_password": "secret",
        "timeout": 10, "verify": False,
        "selected_sites": [], "sites_manual": MANUAL_SITES,
        "dns_target_ip": "1.1.1.1", "address_list_name": "mp-pt-sites",
        "conn_mark": "mp-pt-conn", "packet_mark": "mp-pt-pkt",
        "routing_table": "mp-pt-route", "gateway": "",
        "enable_mangle": False, "enable_policy_route": False,
        "enable_filter_accept": False, "allow_high_risk": False,
        "cleanup_stale": False, "comment_prefix": "MP-B3",
        "dry_run": False, "auto_backup": True, "require_backup": True,
        "auto_rollback": True, "notify_mode": "issue",
        "cron": "0 5 * * *", "display_limit": 60,
    }
    cfg.update(overrides)
    return cfg


def make_plugin(**overrides) -> RouterOSSiteRouting:
    plugin = RouterOSSiteRouting()
    plugin.init_plugin(base_config(**overrides))
    return plugin


def full_plugin(**overrides) -> RouterOSSiteRouting:
    data = {
        "enable_mangle": True, "enable_policy_route": True,
        "enable_filter_accept": True, "allow_high_risk": True,
        "gateway": "192.168.88.2",
    }
    data.update(overrides)
    return make_plugin(**data)


def opts(**overrides) -> dict:
    data = {
        "dns_target_ip": "1.1.1.1", "address_list_name": "mp-pt-sites",
        "conn_mark": "mp-pt-conn", "packet_mark": "mp-pt-pkt",
        "routing_table": "mp-pt-route", "gateway": "",
        "enable_mangle": False, "enable_policy_route": False,
        "enable_filter_accept": False, "cron": "0 5 * * *",
    }
    data.update(overrides)
    return data


def full_opts() -> dict:
    return opts(enable_mangle=True, enable_policy_route=True,
                enable_filter_accept=True, gateway="192.168.88.2")


def sample_sites():
    return sites.parse_manual_sites(MANUAL_SITES)[0]


def preview_and_apply(plugin):
    preview = plugin.api_preview()
    assert preview.success, preview.message
    return plugin.api_apply()


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


def components(node):
    """递归取出组件树里出现过的所有 component 名。"""
    out = set()
    if isinstance(node, dict):
        if node.get("component"):
            out.add(node["component"])
        for value in node.values():
            out |= components(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            out |= components(item)
    return out


BAD_PHRASES = ("插件用途", "这个插件解决什么", "两点说明", "使用说明", "三步用起来",
               "安全机制（重要）", "注意事项", "安全提示：", "工作方式：",
               "属正常现象", "强烈建议")


def _plugin_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "app" / "plugins" / "routerossiterouting"


# ---------------------------------------------------------------------------
# 站点数据层
# ---------------------------------------------------------------------------

def test_manual_site_parsing() -> None:
    parsed, errors = sites.parse_manual_sites(MANUAL_SITES)
    assert not errors, errors
    assert len(parsed) == 2
    assert parsed[0].name == "馒头"
    assert parsed[0].domain == "pt.mantou.example"
    assert parsed[0].tracker_domains == ["tracker.mantou.example", "api.mantou.example"]
    assert parsed[0].note == "主站"
    assert parsed[1].domain == "pt.cat.example"
    assert parsed[1].tracker_domains == ["tracker.cat.example", "tracker2.cat.example"]
    assert parsed[0].id == sites.make_site_id("馒头"), "标签 id 要稳定"
    assert parsed[0].id != parsed[1].id
    bad, errs = sites.parse_manual_sites("坏行\nA|\n|pt.example.com")
    assert not bad and len(errs) >= 3


def test_domain_and_tracker_normalization() -> None:
    assert sites.extract_domain("https://PT.Example.com:8443/login?x=1") == "pt.example.com"
    assert sites.extract_domain("tracker.example.com/announce") == "tracker.example.com"
    assert sites.extract_domain("http://a.b.c:8080/x") == "a.b.c"
    assert sites.extract_domain("") == ""
    assert sites.normalize_trackers("a.com, b.com;c.com  d.com") == [
        "a.com", "b.com", "c.com", "d.com"]
    assert sites.normalize_trackers(["https://x.com:443/p", "x.com", "y.com;z.com"]) == [
        "x.com", "y.com", "z.com"]
    assert sites.normalize_trackers("a.com,a.com") == ["a.com"]
    assert sites.clean_value("a\x00b\x1fc", 10) == "abc"


def test_site_configs_and_discovery() -> None:
    configs = sites.load_site_configs()
    for name in ("馒头", "猫站", "海豚", "用户站点"):
        assert name in configs, f"少读到 {name}"
    items = sites.site_items(configs)
    values = [x["value"] for x in items]
    assert "site:馒头" in values and "site:海豚" in values
    manual = "馒头|https://new.mantou.example|tracker.new.example|覆盖"
    found, errors = sites.discover_sites(["site:馒头", "site:猫站"], manual, configs)
    assert not errors, errors
    assert len(found) == 2, "同名手动行要覆盖下拉项而不是多出一条"
    by_name = {s.name: s for s in found}
    assert by_name["馒头"].domain == "new.mantou.example"
    assert by_name["馒头"].source == "手动补充"
    assert by_name["猫站"].domain == "pt.cat.example"
    assert "tracker.cat.example" in by_name["猫站"].tracker_domains
    assert "rss.cat.example" in by_name["猫站"].tracker_domains
    missing, errs = sites.discover_sites(["site:不存在"], "", configs)
    assert not missing and errs
    assert [s.name for s in sites.sites_from_plan(
        {"sites": [s.to_dict() for s in found]})] == ["馒头", "猫站"]


# ---------------------------------------------------------------------------
# 计划渲染与校验
# ---------------------------------------------------------------------------

def test_plan_render_actions_and_sections() -> None:
    plan = planner.render_plan(sample_sites(), opts())
    assert not plan["blocked"], plan["errors"]
    assert plan["prefix"] == "MP-B3"
    assert plan["summary"]["services"] == 2
    assert plan["summary"]["actions"] == 12
    assert plan["summary"]["sections"] == 2
    assert set(plan["sections"]) == {planner.SEC_DNS, planner.SEC_ADDR}
    dns = [a for a in plan["actions"] if a["section"] == planner.SEC_DNS]
    addr = [a for a in plan["actions"] if a["section"] == planner.SEC_ADDR]
    assert len(dns) == 6 and len(addr) == 6
    assert dns[0]["payload"]["address"] == "1.1.1.1"
    assert addr[0]["payload"]["list"] == "mp-pt-sites"
    assert all(a["tag"].startswith("MP-B3[") and a["tag"].endswith("]")
               for a in plan["actions"])
    assert all(a["payload"]["comment"].startswith(a["tag"] + " ")
               for a in plan["actions"]), "备注要是「标签 + 中文含义」"
    assert all(a["risk_level"] == "low" for a in plan["actions"])
    assert all(a["command"].startswith("/ip/") for a in plan["actions"])
    for key in ("created", "prefix", "sites", "actions", "errors", "warnings",
                "sections", "high_risk", "blocked", "options", "summary"):
        assert key in plan, key
    for key in ("services", "actions", "sections", "high", "medium", "created"):
        assert key in plan["summary"], key


def test_plan_full_sections_and_risk() -> None:
    plan = planner.render_plan(sample_sites(), full_opts(), allow_high_risk=True)
    assert not plan["blocked"], plan["errors"]
    assert plan["summary"]["actions"] == 20
    assert plan["summary"]["sections"] == 5
    assert plan["summary"]["high"] == 6
    assert plan["summary"]["medium"] == 2
    assert plan["high_risk"] is True
    mangle = [a for a in plan["actions"] if a["section"] == planner.SEC_MANGLE]
    assert len(mangle) == 4 and all(a["risk_level"] == "high" for a in mangle)
    route = [a for a in plan["actions"] if a["section"] == planner.SEC_ROUTE]
    assert len(route) == 2
    assert all(a["payload"]["gateway"] == "192.168.88.2" for a in route)
    fw = [a for a in plan["actions"] if a["section"] == planner.SEC_FILTER]
    assert len(fw) == 2
    assert all(a["placement"] == planner.PLACE_BEFORE_DROP for a in fw)
    assert all(a["risk_level"] == "medium" for a in fw)


def test_validation_rejects_bad_values() -> None:
    cases = [
        opts(dns_target_ip=""),
        opts(dns_target_ip="不是IP"),
        opts(dns_target_ip="0.0.0.0"),
        opts(address_list_name="bad list"),
        opts(conn_mark="标记名"),
        opts(gateway="999.1.1.1"),
        opts(cron="0 5 * *"),
    ]
    for bad in cases:
        plan = planner.render_plan(sample_sites(), bad)
        assert plan["blocked"], f"应该被拦截：{bad}"
        assert plan["errors"] and not plan["actions"]
    plan = planner.render_plan(sample_sites(),
                               opts(enable_policy_route=True, gateway=""),
                               allow_high_risk=True)
    assert plan["blocked"] and any("网关" in e for e in plan["errors"])
    bad_sites = sites.parse_manual_sites("坏站|http://bad host||")[0]
    assert planner.render_plan(bad_sites, opts())["blocked"], "带空格域名要拒绝"
    assert any("域名" in e for e in planner.render_plan(bad_sites, opts())["errors"])
    assert planner.render_plan([], opts())["blocked"], "没选站点要拦截"


def test_high_risk_guard() -> None:
    plan = planner.render_plan(sample_sites(), full_opts(), allow_high_risk=False)
    assert plan["blocked"] and not plan["actions"]
    assert any("允许高风险动作" in e for e in plan["errors"])
    FAKE.reset()
    plugin = make_plugin(enable_mangle=True, enable_policy_route=True,
                         enable_filter_accept=True, gateway="192.168.88.2",
                         allow_high_risk=False)
    assert not plugin.api_preview().success
    assert not plugin.api_apply().success
    assert FAKE.put_count == 0


def test_cron_validation() -> None:
    ok, text = planner.validate_cron("0 5 * * *")
    assert ok and text == "0 5 * * *"
    assert planner.validate_cron("*/5 1,2 * * 1-5")[0]
    assert planner.validate_cron("0 5 * * ?")[0]
    for bad in ("0 5 * *", "60 5 * * *", "0 5 * * 9", "* * * * * *", "abc"):
        assert not planner.validate_cron(bad)[0], f"不该通过：{bad}"
    FAKE.reset()
    assert make_plugin(cron="不是cron").get_service() == []
    assert make_plugin(enabled=False).get_service() == []


def test_stale_tags_function() -> None:
    rows = [
        {"comment": "手工：别删我"},
        {"comment": "MP-B3[site1:dns-a.com] 还在"},
        {"comment": "MP-B3[old:dns-b.com] 已删站点"},
        {"comment": "手工：提到 MP-B3 但没标签"},
    ]
    assert planner.stale_tags(rows, ["MP-B3[site1:dns-a.com]"], "MP-B3") == [
        "MP-B3[old:dns-b.com]"]
    assert planner.stale_tags(rows, ["MP-B3[site1:dns-a.com]",
                                     "MP-B3[old:dns-b.com]"], "MP-B3") == []


def test_existing_domains_skip_dns() -> None:
    plan = planner.render_plan(sample_sites(), opts(),
                               existing_domains=["https://pt.mantou.example:443/x"])
    assert not plan["blocked"], plan["errors"]
    assert plan["summary"]["actions"] == 11
    assert any("已有解析记录" in w for w in plan["warnings"])
    dns_names = [a["payload"]["name"] for a in plan["actions"]
                 if a["section"] == planner.SEC_DNS]
    assert "pt.mantou.example" not in dns_names
    addr_names = [a["payload"]["address"] for a in plan["actions"]
                  if a["section"] == planner.SEC_ADDR]
    assert "pt.mantou.example" in addr_names


def test_plan_roundtrip() -> None:
    plan = planner.render_plan(sample_sites(), opts())
    again = planner.render_plan(plan, opts())
    assert again["summary"]["actions"] == plan["summary"]["actions"]
    assert [s.name for s in sites.sites_from_plan(plan)] == ["馒头", "猫站"]
    assert planner.md_safe("[abc]`x`_y_") == "［abc］ˋxˋ＿y＿"


# ---------------------------------------------------------------------------
# 预览 / 部署 / 回滚
# ---------------------------------------------------------------------------

def test_preview_refreshes_and_marks_exists() -> None:
    FAKE.reset()
    plugin = make_plugin()
    preview = plugin.api_preview()
    assert preview.success, preview.message
    assert preview.data["refreshed"] and not preview.data.get("offline")
    assert preview.data["summary"]["exists"] == 0
    assert len(preview.data["actions"]) == 12
    deploy = plugin.api_apply()
    assert deploy.success, deploy.message
    assert deploy.data["written"] == 12
    again = plugin.api_preview()
    assert again.success
    assert again.data["summary"]["exists"] == 12
    assert all(a["state"] == "exists" for a in again.data["actions"])


def test_apply_writes_all_sections() -> None:
    FAKE.reset()
    plugin = full_plugin()
    deploy = preview_and_apply(plugin)
    assert deploy.success, deploy.message
    assert deploy.data["written"] == 20
    assert len(FAKE.tagged_rows()) == 20
    assert len(FAKE.manual_rows()) == 7
    fw = FAKE.state[planner.SEC_FILTER]
    tagged_idx = next(i for i, r in enumerate(fw) if "MP-B3[" in str(r.get("comment")))
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
    assert second.data["skipped"] == 12 and second.data["written"] == 0
    assert len(FAKE.tagged_rows()) == before, "重复部署不应堆积重复规则"


def test_failure_auto_rollback() -> None:
    FAKE.reset(fail_on_put=2)
    plugin = make_plugin()
    deploy = preview_and_apply(plugin)
    assert not deploy.success
    assert FAKE.put_count == 2, "遇错即停"
    assert len(FAKE.tagged_rows()) == 0, "已写入的条目要逆序回滚掉"
    assert len(FAKE.deletes) == 1
    assert len(FAKE.manual_rows()) == 7, "手工规则不能被动"
    assert deploy.data["rollback"]["ok_count"] == 1


def test_manual_rollback_keeps_manual_rules() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert preview_and_apply(plugin).success
    assert len(FAKE.tagged_rows()) == 12
    result = plugin.api_rollback()
    assert result.success, result.message
    assert len(FAKE.tagged_rows()) == 0
    assert len(FAKE.manual_rows()) == 7
    assert not plugin.api_rollback().success, "回滚后不应留可重复回滚的条目"


def test_dry_run_no_writes() -> None:
    FAKE.reset()
    plugin = make_plugin(dry_run=True)
    deploy = preview_and_apply(plugin)
    assert deploy.success, deploy.message
    assert deploy.data["dry_run"] is True
    assert FAKE.put_count == 0 and not FAKE.backups
    assert len(FAKE.tagged_rows()) == 0


def test_backup_failure_aborts_writes() -> None:
    FAKE.reset(fail_backup=True)
    plugin = make_plugin()
    preview = plugin.api_preview()
    assert preview.success, preview.message
    deploy = plugin.api_apply()
    assert not deploy.success
    assert "备份失败" in deploy.message
    assert FAKE.put_count == 0, "备份没成功就别往下写"
    assert len(FAKE.tagged_rows()) == 0


def test_offline_preview() -> None:
    FAKE.reset()
    plugin = make_plugin(router_address="")
    preview = plugin.api_preview()
    assert preview.success, preview.message
    assert preview.data["offline"] is True
    assert len(preview.data["unreadable"]) == 2
    assert FAKE.put_count == 0


# ---------------------------------------------------------------------------
# 失效条目清理
# ---------------------------------------------------------------------------

def test_cleanup_switch_defaults_off() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert preview_and_apply(plugin).success
    FAKE.add_stale_row("MP-B3[old-site:dns-old.example]")
    before = len(FAKE.tagged_rows())
    result = plugin.api_cleanup()
    assert not result.success, "默认关，按钮要拒绝清理"
    assert "清理开关" in result.message
    assert len(FAKE.tagged_rows()) == before


def test_cleanup_only_deletes_tagged_rows() -> None:
    FAKE.reset()
    plugin = make_plugin(cleanup_stale=True)
    assert preview_and_apply(plugin).success
    FAKE.add_stale_row("MP-B3[old-site:dns-old.example]")
    manual_before = len(FAKE.manual_rows())
    result = plugin.api_cleanup()
    assert result.success, result.message
    assert result.data["deleted"] == 1
    assert "MP-B3[old-site:dns-old.example]" in result.data["stale"]
    assert FAKE.backups, "删除前必须走备份"
    assert len(FAKE.manual_rows()) == manual_before, "手工规则绝不能删"
    assert not any("MP-B3[old-site:dns-old.example]" in str(r.get("comment") or "")
                   for r in FAKE.all_rows())
    assert "最近一次清理报告" not in json.dumps(plugin.get_page(), ensure_ascii=False)


def test_cleanup_aborts_when_backup_fails() -> None:
    FAKE.reset(fail_backup=True)
    plugin = make_plugin(cleanup_stale=True)
    FAKE.add_stale_row("MP-B3[old-site:dns-old.example]")
    result = plugin.api_cleanup()
    assert not result.success
    assert result.data["deleted"] == 0
    assert "备份失败" in result.message
    assert any("old-site" in str(r.get("comment") or "") for r in FAKE.all_rows())


def test_cleanup_dry_run_no_delete() -> None:
    FAKE.reset()
    plugin = make_plugin(cleanup_stale=True, dry_run=True)
    FAKE.add_stale_row("MP-B3[old-site:dns-old.example]")
    result = plugin.api_cleanup()
    assert result.success, result.message
    assert result.data["dry_run"] is True
    assert not FAKE.deletes
    assert any("old-site" in str(r.get("comment") or "") for r in FAKE.all_rows())


# ---------------------------------------------------------------------------
# 备份 / 定时同步 / 表单 / API / 远程命令
# ---------------------------------------------------------------------------

def test_backup_and_snapshot() -> None:
    FAKE.reset()
    plugin = make_plugin()
    result = plugin.api_backup()
    assert result.success, result.message
    assert FAKE.backups
    path = Path(result.data["snapshot"])
    assert path.exists()
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert snap["count"] >= 5, "快照应包含部署前的手工规则"
    assert planner.SEC_DNS in snap["sections"]
    assert planner.SEC_FILTER in snap["sections"]


def test_sync_once_idempotent_and_guarded() -> None:
    FAKE.reset()
    plugin = make_plugin()
    first = plugin.api_sync()
    assert first.success, first.message
    assert first.data["written"] == 12
    assert plugin.api_status().data["last_deploy"]["ok"] is True
    assert len(FAKE.tagged_rows()) == 12
    second = plugin.api_sync()
    assert second.data["skipped"] == 12 and second.data["written"] == 0

    FAKE.reset()
    plugin2 = make_plugin(enable_mangle=True, enable_policy_route=True,
                          enable_filter_accept=True, gateway="192.168.88.2",
                          allow_high_risk=False)
    third = plugin2.api_sync()
    assert not third.success, "校验没通过不能部署"
    assert FAKE.put_count == 0


def test_sync_once_swallows_exceptions() -> None:
    FAKE.reset()
    plugin = make_plugin()
    old = AsyncRequestUtils.responder
    try:
        def boom(method, url, kwargs):
            raise RuntimeError("模拟网络炸了")
        AsyncRequestUtils.responder = boom
        report = plugin.sync_once()  # 不许抛出
        assert report["ok"] is False
    finally:
        AsyncRequestUtils.responder = old


def test_get_form_and_page_buttons_bind_real_apis() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert plugin.api_preview().success
    buttons = walk(plugin.get_page())
    assert len(buttons) >= 8
    registered = {f"plugin/RouterOSSiteRouting{a['path']}" for a in plugin.get_api()}
    for api in buttons:
        assert api in registered, f"按钮绑到了不存在的 API：{api}"

    form, defaults = plugin.get_form()
    form_blob = json.dumps(form, ensure_ascii=False)
    assert {"VRow", "VCol", "VSelect", "VTextField", "VTextarea",
            "VSwitch", "VAlert", "div"} <= components(form), "配置页组件要齐"
    assert "已配置站点（可多选）" in form_blob
    assert "site:馒头" in form_blob, "下拉项要来自站点数据桩"
    assert "sites_manual" in form_blob
    assert defaults["cleanup_stale"] is False
    assert defaults["comment_prefix"] == "MP-B3"
    assert defaults["cron"] == "0 5 * * *"
    assert defaults["allow_high_risk"] is False
    page_blob = json.dumps(plugin.get_page(), ensure_ascii=False)
    for phrase in BAD_PHRASES:
        assert phrase not in form_blob, f"设置页不该出现套话：{phrase}"
        assert phrase not in page_blob, f"详情页不该出现套话：{phrase}"
    readme = (_plugin_dir() / "README.md").read_text(encoding="utf-8")
    for phrase in BAD_PHRASES:
        assert phrase not in readme, f"README 不该出现套话：{phrase}"


def test_api_and_commands() -> None:
    FAKE.reset()
    plugin = make_plugin()
    apis = plugin.get_api()
    paths = {a["path"] for a in apis}
    assert {"/sites", "/preview", "/plan", "/apply", "/clear", "/rollback",
            "/cleanup", "/backup", "/test", "/sync", "/status", "/logs"} <= paths
    assert all(a["auth"] == "bear" for a in apis)
    cmds = {c["cmd"] for c in plugin.get_command()}
    assert len(cmds) >= 4
    assert {"/b3_preview", "/b3_apply", "/b3_sync", "/b3_cleanup",
            "/b3_rollback", "/b3_status"} <= cmds
    assert all(c["event"] for c in plugin.get_command())
    status = plugin.api_status().data
    assert status["plugin"] == "RouterOSSiteRouting"
    assert status["enabled"] is True
    assert plugin.get_state() is True
    assert plugin.api_sites().data["items"]
    assert plugin.api_plan().success
    assert plugin.api_logs().success
    assert plugin.api_clear().success

    services = plugin.get_service()
    assert len(services) == 1
    assert services[0]["trigger"] == {"cron": "0 5 * * *"}
    assert services[0]["func"] == plugin.sync_once

    from app.sdk.scheduler import REMOVED_JOBS
    REMOVED_JOBS.clear()
    plugin.stop_service()
    assert REMOVED_JOBS == ["RouterOSSiteRouting"]

    _PluginBase.POSTED.clear()
    event = Event(event_type="plugin.action",
                  event_data={"cmd": "/b3_status", "channel": "tg", "userid": "1"})
    plugin.handle_command(event)
    assert _PluginBase.POSTED, "远程命令要回消息"
    assert "站点" in _PluginBase.POSTED[-1]["text"]


def test_status_and_report_after_deploy() -> None:
    FAKE.reset()
    plugin = make_plugin()
    assert preview_and_apply(plugin).success
    status = plugin.api_status().data
    assert status["last_deploy"]["ok"] is True
    assert status["dry_run"] is False
    assert len(status["sites"]) == 2
    # 部署成功后计划会清空，重新刷新预览才有动作表与等价命令
    assert plugin.api_preview().success
    blob = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "待确认的计划" in blob
    assert "当前站点" not in blob
    assert "最近一次部署报告" not in blob


def test_version_literal_and_metadata() -> None:
    cls = RouterOSSiteRouting
    assert cls.plugin_version == "1.0.0"
    assert cls.plugin_author == "BackspaceM"
    assert cls.plugin_icon == ("https://raw.githubusercontent.com/BackspaceM/"
                               "MoviePilot-Plugins/main/icons/routerossiterouting.png")
    assert cls.system_version == ">=3.0.0"
    assert cls.plugin_order == 54
    text = (_plugin_dir() / "__init__.py").read_text(encoding="utf-8")
    assert re.search(r'^\s*plugin_version\s*=\s*"1\.0\.0"', text, re.MULTILINE), \
        "plugin_version 必须是字面量"
