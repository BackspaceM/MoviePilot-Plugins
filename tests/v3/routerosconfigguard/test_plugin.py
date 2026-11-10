"""RouterOSConfigGuard 离线用例（在无宿主的桩环境中由 run_stub_tests.py 执行）。"""

from __future__ import annotations

import asyncio
import time

from app.plugins.routerosconfigguard import (
    RouterOSConfigGuard, _KEY_BASELINE, _KEY_INDEX, _KEY_ROS_FILES, _KEY_LAST_SNAP,
)
from app.plugins.routerosconfigguard.core import (
    DEFAULT_SECTIONS,
    SECTIONS,
    backup_basename,
    diff_snapshots,
    format_diff,
    format_log_line,
    is_our_backup,
    make_snapshot,
    mask_secrets,
    md_safe,
    normalize_row,
    prune_routeros_files,
    prune_snapshots,
    row_fingerprint,
    section_items,
    snapshot_id,
    validate_cron,
)
from app.plugins.routerosconfigguard.clients import (
    normalize_base_url,
)


def _fresh_plugin():
    p = RouterOSConfigGuard()
    p._data = {}
    p._config = {}
    return p


# ---------------------------------------------------------------------------
# core：归一化与指纹
# ---------------------------------------------------------------------------

def test_normalize_row_drops_volatile_and_id():
    row = {".id": "*1", "name": "dns1", "address": "1.1.1.1",
           "bytes": "1234", "uptime": "1d2h", "disabled": "false"}
    out = normalize_row(row)
    assert ".id" not in out
    assert "bytes" not in out
    assert "uptime" not in out
    assert out["name"] == "dns1"
    assert out["disabled"] == "false"


def test_row_fingerprint_stable_ignores_volatile():
    a = {".id": "*1", "name": "dns1", "address": "1.1.1.1", "bytes": "100"}
    b = {".id": "*99", "name": "dns1", "address": "1.1.1.1", "bytes": "99999"}
    assert row_fingerprint(a) == row_fingerprint(b)
    c = {"name": "dns1", "address": "8.8.8.8"}
    assert row_fingerprint(c) != row_fingerprint(a)


def test_validate_cron():
    assert validate_cron("*/5 * * * *")[0] is True
    assert validate_cron("0 3 * * *")[0] is True
    assert validate_cron("30 4 1,15 * 1-5")[0] is True
    assert validate_cron("bad cron")[0] is False
    assert validate_cron("60 * * * *")[0] is False   # 分钟越界
    assert validate_cron("* * * *")[0] is False       # 段数不足


def test_md_safe_prevents_markdown_link_rendering():
    """详情页 text 里的 [a][b] 会被当参考式链接渲染成空药丸，必须转义。"""
    raw = "[2026-11-10 11:57:58][INFO] 备份完成 [防火墙]"
    safe = md_safe(raw)
    assert "[" not in safe and "]" not in safe
    assert "［" in safe and "］" in safe
    assert "2026-11-10" in safe and "备份完成" in safe


def test_format_log_line_uses_fullwidth_separator():
    line = format_log_line("2026-11-10 11:57:58", "INFO", "快照[完成]")
    assert "｜" in line
    assert "[" not in line and "]" not in line
    assert "INFO" in line


def test_mask_secrets():
    text = "密码是 Sup3rSecret 且 https://admin:hunter2@192.168.1.1/rest"
    masked = mask_secrets(text, ["Sup3rSecret"])
    assert "Sup3rSecret" not in masked
    assert "hunter2" not in masked
    assert "***" in masked


def test_section_definitions_and_items():
    keys = [s.key for s in SECTIONS]
    assert len(keys) == len(set(keys))
    assert "firewall_filter" in DEFAULT_SECTIONS
    assert SECTIONS[0].path.startswith("/")
    items = section_items()
    assert all({"title", "value"} <= set(i) for i in items)
    assert {"title": "系统标识（identity）", "value": "identity"} in items


def test_snapshot_id_and_prune():
    sid = snapshot_id(1690000000.5)
    assert len(sid.split("_")) == 3  # yyyyMMdd_HHMMSS_fff
    kept, removed = prune_snapshots([{"id": str(i)} for i in range(5)], 2)
    assert [k["id"] for k in kept] == ["0", "1"]
    assert len(removed) == 3


# ---------------------------------------------------------------------------
# core：漂移检测
# ---------------------------------------------------------------------------

def _snap(dns_rows, fw_rows=None):
    raw = {"ip_dns_static": dns_rows}
    if fw_rows is not None:
        raw["firewall_filter"] = fw_rows
    return make_snapshot(raw, [k for k in ("ip_dns_static", "firewall_filter") if k in raw])


def test_diff_no_change():
    base = _snap([{"name": "a.local", "address": "1.1.1.1"}])
    cur = _snap([{"name": "a.local", "address": "1.1.1.1"}])
    diff = diff_snapshots(base, cur)
    assert diff["has_drift"] is False
    assert diff["totals"]["added"] == 0


def test_diff_ignores_volatile_fields():
    base = _snap([{".id": "*1", "name": "a.local", "address": "1.1.1.1", "bytes": "10"}])
    cur = _snap([{".id": "*9", "name": "a.local", "address": "1.1.1.1", "bytes": "999"}])
    assert diff_snapshots(base, cur)["has_drift"] is False


def test_diff_added_and_removed():
    base = _snap([{"name": "a.local", "address": "1.1.1.1"},
                  {"name": "old.local", "address": "2.2.2.2"}])
    cur = _snap([{"name": "a.local", "address": "1.1.1.1"},
                 {"name": "new.local", "address": "3.3.3.3"}])
    diff = diff_snapshots(base, cur)
    assert diff["has_drift"] is True
    sec = diff["sections"]["ip_dns_static"]
    assert sec["added_count"] == 1 and sec["removed_count"] == 1
    assert sec["added"][0]["name"] == "new.local"
    assert sec["removed"][0]["name"] == "old.local"


def test_diff_modified_is_remove_plus_add():
    base = _snap([{"name": "a.local", "address": "1.1.1.1"}])
    cur = _snap([{"name": "a.local", "address": "8.8.8.8"}])  # 改了 IP
    diff = diff_snapshots(base, cur)
    sec = diff["sections"]["ip_dns_static"]
    assert sec["added_count"] == 1 and sec["removed_count"] == 1


def test_diff_duplicate_rows_counted():
    base = _snap([{"name": "dup", "address": "1.1.1.1"},
                  {"name": "dup", "address": "1.1.1.1"}])
    cur = _snap([{"name": "dup", "address": "1.1.1.1"}])
    sec = diff_snapshots(base, cur)["sections"]["ip_dns_static"]
    assert sec["removed_count"] == 1  # 少了一条重复行


def test_diff_sensitive_flag_and_format():
    base = _snap([], fw_rows=[{"chain": "input", "action": "accept"}])
    cur = _snap([], fw_rows=[{"chain": "input", "action": "drop"}])
    diff = diff_snapshots(base, cur)
    assert diff["sections"]["firewall_filter"]["sensitive"] is True
    assert "firewall" in " ".join(diff["sensitive_hits"]) or "防火墙" in " ".join(diff["sensitive_hits"])
    text = format_diff(diff)
    assert "漂移" in text and "＋" in text and "－" in text


def test_diff_against_none_baseline():
    cur = _snap([{"name": "a.local", "address": "1.1.1.1"}])
    diff = diff_snapshots(None, cur)
    assert diff["has_drift"] is True
    assert diff["sections"]["ip_dns_static"]["added_count"] == 1


# ---------------------------------------------------------------------------
# clients：URL 规范化与服务发现下拉框
# ---------------------------------------------------------------------------

def test_normalize_base_url():
    assert normalize_base_url("192.168.1.1") == "https://192.168.1.1"
    # 显式默认端口 80（HTTP）会被省略；非默认端口保留
    assert normalize_base_url("192.168.1.1", 80, False) == "http://192.168.1.1"
    assert normalize_base_url("192.168.1.1", 8080, False) == "http://192.168.1.1:8080"
    assert normalize_base_url("https://ros.local/rest") == "https://ros.local"
    assert normalize_base_url("http://192.168.1.1:8080") == "http://192.168.1.1:8080"


def test_backup_basename_and_filter():
    base = backup_basename("rosguard", "20261110_115758_123")
    assert base == "rosguard_20261110_115758_123"
    assert is_our_backup(f"{base}.backup", "rosguard") is True
    assert is_our_backup(f"{base}.rsc", "rosguard") is True
    assert is_our_backup("other_20261110.rsc", "rosguard") is False
    assert is_our_backup("rosguard_20261110_115758_123.txt", "rosguard") is False


def test_prune_routeros_files_keeps_recent_pairs():
    files = []
    for sid in ("20261108_000000_000", "20261109_000000_000", "20261110_000000_000"):
        files.append({".id": f"*b{sid}", "name": f"rosguard_{sid}.backup"})
        files.append({".id": f"*r{sid}", "name": f"rosguard_{sid}.rsc"})
    files.append({".id": "*9", "name": "unrelated.txt"})  # 非备份文件应保留不删
    kept, removed = prune_routeros_files(files, "rosguard", keep_pairs=2)
    kept_names = {f["name"] for f in kept}
    removed_names = {f["name"] for f in removed}
    assert "rosguard_20261110_000000_000.backup" in kept_names
    assert "rosguard_20261109_000000_000.rsc" in kept_names
    assert "rosguard_20261108_000000_000.backup" in removed_names
    assert "rosguard_20261108_000000_000.rsc" in removed_names
    assert "unrelated.txt" in kept_names  # 不碰非插件文件


# ---------------------------------------------------------------------------
# V3 契约
# ---------------------------------------------------------------------------

def test_contract_attributes():
    assert RouterOSConfigGuard.plugin_name == "RouterOSConfigGuard"
    assert RouterOSConfigGuard.plugin_version == "1.1.0"
    assert RouterOSConfigGuard.plugin_author == "BackspaceM"
    assert RouterOSConfigGuard.plugin_desc
    assert RouterOSConfigGuard.plugin_icon.startswith("https://")
    assert RouterOSConfigGuard.plugin_config_prefix == "routerosconfigguard_"


def test_init_and_state():
    p = _fresh_plugin()
    assert p.get_state() is False
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1"})
    assert p.get_state() is True
    assert p._address == "192.168.1.1"
    assert p._sections == DEFAULT_SECTIONS
    assert p._backup_target == "mp"           # 默认备份位置是 MP
    assert p._backup_prefix == "rosguard"


def test_init_backup_target_options():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4", "backup_target": "both",
                   "backup_prefix": "my router!", "backup_encrypt": True,
                   "backup_password": "BkpSecret9"})
    assert p._backup_target == "both"
    assert p._backup_prefix == "myrouter"      # 非法字符被清洗
    assert p._backup_password == "BkpSecret9"
    # 非法目标回退 mp
    p2 = _fresh_plugin()
    p2.init_plugin({"enabled": True, "router_address": "1.2.3.4", "backup_target": "cloud"})
    assert p2._backup_target == "mp"
    # 密码脱敏同时覆盖路由密码与备份密码
    assert "BkpSecret9" not in mask_secrets("pwd=BkpSecret9", p._secrets())


def test_get_form_returns_tuple_with_backup_target_select():
    p = _fresh_plugin()
    form, defaults = p.get_form()
    assert isinstance(form, list) and isinstance(defaults, dict)
    selects = {}
    def walk(nodes):
        for node in nodes or []:
            if node.get("component") == "VSelect":
                selects[node["props"]["model"]] = node["props"].get("items", [])
            walk(node.get("content") if isinstance(node.get("content"), list) else None)
    walk(form)
    # 备份位置下拉存在，含 mp/routeros/both 三选项
    assert "backup_target" in selects
    tgt_values = [i["value"] for i in selects["backup_target"]]
    assert tgt_values == ["mp", "routeros", "both"]
    assert "sections" in selects
    # 不应再有下载器/媒体服务器下拉
    assert "downloaders" not in selects and "mediaservers" not in selects
    # 默认值齐全（含备份位置相关，且无监控字段）
    for key in ("enabled", "router_address", "cron", "keep", "notify_mode", "sections",
                "backup_target", "backup_prefix", "backup_encrypt", "backup_password"):
        assert key in defaults
    for key in ("downloaders", "mediaservers", "downloaders_extra", "mediaservers_extra", "check_services"):
        assert key not in defaults


def test_get_service_disabled_and_enabled():
    p = _fresh_plugin()
    assert p.get_service() == []
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4", "cron": "*/5 * * * *"})
    svc = p.get_service()
    assert len(svc) == 1 and svc[0]["trigger"] == ("cron", "*/5 * * * *")
    assert svc[0]["func"] == p.start_run


def test_get_api_and_command():
    p = _fresh_plugin()
    apis = {a["path"] for a in p.get_api()}
    assert f"/plugin/RouterOSConfigGuard/run" in apis
    assert f"/plugin/RouterOSConfigGuard/baseline" in apis
    assert f"/plugin/RouterOSConfigGuard/status" in apis
    assert f"/plugin/RouterOSConfigGuard/rosbackups" in apis
    cmds = {c["cmd"] for c in p.get_command()}
    assert "/rosguard_run" in cmds and "/rosguard_status" in cmds


def test_get_page_renders_without_markdown_artifacts():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4"})
    # 塞入一条带方括号的日志（模拟曾经渲染成空药丸的输入）
    p.save_data("rosguard_logs", [
        {"time": "2026-11-10 11:57:58", "level": "INFO", "message": "快照[完成] 检测到[防火墙]变更"},
    ])
    page = p.get_page()
    assert isinstance(page, list) and page[0]["component"] == "VCard"
    text = str(page)
    for label in ("立即备份并检测", "测试连接", "最近设为基线", "清空快照", "刷新ROS备份"):
        assert label in text
    assert "备份位置" in text
    # 关键：详情页里不应残留成对半角方括号（会被前端当 Markdown 参考式链接）
    assert "[完成]" not in text and "[防火墙]" not in text
    assert "［完成］" in text
    # 不应再出现关联服务区块
    assert "关联服务在线状态" not in text


def test_onlyonce_reset_flips_config():
    from app.sdk.plugin import _PluginBase
    _PluginBase.CONFIGS.clear()
    p = _fresh_plugin()
    # enabled=False：仍会复位 onlyonce，但不会启动后台线程去消费共享的假响应队列
    # （否则该守护线程会与后续用例竞争 AsyncRequestUtils.responses，导致竞态）
    p.init_plugin({"enabled": False, "router_address": "1.2.3.4", "onlyonce": True})
    assert _PluginBase.CONFIGS, "onlyonce 应触发 update_config 复位"
    assert _PluginBase.CONFIGS[-1].get("onlyonce") is False


# ---------------------------------------------------------------------------
# 端到端：基线 → 改动 → 检测漂移（用假 HTTP 响应驱动 _async_run）
# ---------------------------------------------------------------------------

from app.sdk.network import AsyncRequestUtils, FakeResponse  # noqa: E402


# 每个分区键的默认假数据；测试可覆盖需要发生漂移的分区
_DEFAULT_ROWS = {
    "identity": {"name": "ROS-Test", "version": "7.14"},
    "ip_dns_static": [{"name": "a.local", "address": "1.1.1.1"}],
    "firewall_filter": [{"chain": "input", "action": "accept"}],
}


def _seed_responses(sections, identity_name="ROS-Test", overrides=None, *,
                    backup_target="mp", ros_files=None, deletable_ids=()):
    """按 _async_run 的真实请求顺序注入响应：

    1) check():  GET /system/identity
    2) fetch():  GET /system/identity（元信息）  然后按 sections 顺序逐个 GET
    3) 若 backup_target∈{routeros,both}：
       POST /system/backup/save → POST /export → GET /file →（按需 DELETE /file/{id}）
    返回对象按队列顺序被消费，故顺序必须与插件一致。
    """
    overrides = overrides or {}
    identity = {"name": identity_name, "version": "7.14"}
    responses = [FakeResponse(200, identity), FakeResponse(200, identity)]
    for key in sections:
        sec = next((s for s in SECTIONS if s.key == key), None)
        assert sec is not None, f"未知分区 {key}"
        payload = overrides.get(key, _DEFAULT_ROWS.get(key, []))
        responses.append(FakeResponse(200, payload))
    if backup_target in ("routeros", "both"):
        responses.append(FakeResponse(200, {}))                 # backup/save
        responses.append(FakeResponse(200, {}))                 # export
        responses.append(FakeResponse(200, list(ros_files or [])))  # GET /file
        for _fid in deletable_ids:
            responses.append(FakeResponse(200, {}))            # DELETE /file
    return responses


def test_end_to_end_baseline_then_drift(monkeypatch=None):
    p = _fresh_plugin()
    p.init_plugin({
        "enabled": True, "router_address": "192.168.1.1",
        "router_username": "admin", "router_password": "S3cretPass",
        "sections": ["ip_dns_static", "firewall_filter"],
        "backup_target": "mp", "auto_baseline": True, "notify_mode": "drift",
    })

    # 第 1 轮：建立基线
    AsyncRequestUtils.responses = _seed_responses(
        ["ip_dns_static", "firewall_filter"],
        overrides={
            "ip_dns_static": [{"name": "a.local", "address": "1.1.1.1"}],
            "firewall_filter": [{"chain": "input", "action": "accept"}],
        },
    )
    r1 = asyncio.run(p._async_run("测试-基线"))
    assert r1["ok"] is True
    assert r1["baseline_set"] is True
    assert p.get_data(_KEY_BASELINE) is not None
    assert r1["diff"]["has_drift"] is True  # 相对 None 基线，首轮全算新增
    snap1_id = r1["snapshot_id"]
    assert snap1_id
    assert len(p.get_data(_KEY_INDEX)) == 1
    assert p.get_data(_KEY_LAST_SNAP), "应保存最近快照用于 routeros 模式设基线"

    # 第 2 轮：DNS 改动 + 防火墙新增一条 → 应检测漂移
    AsyncRequestUtils.responses = _seed_responses(
        ["ip_dns_static", "firewall_filter"],
        overrides={
            "ip_dns_static": [{"name": "a.local", "address": "8.8.8.8"}],  # 改 IP
            "firewall_filter": [{"chain": "input", "action": "accept"},
                                {"chain": "input", "action": "drop", "comment": "blocked"}],  # 新增
        },
    )
    r2 = asyncio.run(p._async_run("测试-漂移"))
    assert r2["ok"] is True
    assert r2["baseline_set"] is False
    assert r2["diff"]["has_drift"] is True
    dns_sec = r2["diff"]["sections"].get("ip_dns_static")
    fw_sec = r2["diff"]["sections"].get("firewall_filter")
    assert dns_sec and dns_sec["added_count"] == 1 and dns_sec["removed_count"] == 1
    assert fw_sec and fw_sec["added_count"] == 1
    assert any("防火墙" in h for h in r2["diff"]["sensitive_hits"])

    # 通知由 start_run 的 _notify_result 发出（_async_run 只产出结果），显式触发校验
    from app.sdk.plugin import _PluginBase
    _PluginBase.POSTED.clear()
    p._notify_result(r2, "测试-漂移")
    assert _PluginBase.POSTED, "漂移应触发 MP 通知"
    joined = str(_PluginBase.POSTED[-1])
    assert "S3cretPass" not in joined
    assert "漂移" in _PluginBase.POSTED[-1]["title"]

    # notify_mode=none 时不应发通知
    _PluginBase.POSTED.clear()
    p._notify_mode = "none"
    p._notify_result(r2, "测试-静默")
    assert not _PluginBase.POSTED


def test_end_to_end_routeros_backup_only():
    """仅存 RouterOS：不写 MP 快照文件，但生成 .backup/.rsc，基线仍留 MP。"""
    AsyncRequestUtils.calls.clear()
    p = _fresh_plugin()
    p.init_plugin({
        "enabled": True, "router_address": "192.168.1.1",
        "backup_target": "routeros", "backup_prefix": "rosguard",
        "backup_encrypt": True, "backup_password": "BkpSecret9",
        "sections": ["ip_dns_static"], "auto_baseline": True, "keep": 2,
    })
    ros_files = [
        {".id": "*a", "name": "rosguard_20261110_115758_123.backup", "size": "15KiB", "creation-time": "nov/10/2026 11:57:58"},
        {".id": "*b", "name": "rosguard_20261110_115758_123.rsc", "size": "3KiB", "creation-time": "nov/10/2026 11:57:58"},
    ]
    AsyncRequestUtils.responses = _seed_responses(
        ["ip_dns_static"], backup_target="routeros", ros_files=ros_files,
    )
    r = asyncio.run(p._async_run("ROS备份"))
    assert r["ok"] is True
    assert r["backup_target"] == "routeros"
    assert r["ros_backup"]["done"] is True
    base = r["ros_backup"]["basename"]
    assert base.startswith("rosguard_")
    assert f"{base}.backup" in r["ros_backup"]["files"]
    assert f"{base}.rsc" in r["ros_backup"]["files"]
    assert r["snapshot_id"] == ""          # 仅 ROS 模式不写 MP 快照文件
    assert p.snapshot_list() == []
    assert p.get_data(_KEY_BASELINE) is not None   # 基线始终留 MP
    assert p.get_data(_KEY_ROS_FILES), "应记录 RouterOS 本机备份清单"
    # backup/save 的 json body 含加密密码
    save_calls = [c for c in AsyncRequestUtils.calls if c[1].endswith("/system/backup/save")]
    assert save_calls and save_calls[-1][2].get("json", {}).get("password") == "BkpSecret9"


def test_end_to_end_both_targets_with_rotation():
    """两处都存：MP 写快照，ROS 生成备份并按 keep 轮转删除旧组。"""
    AsyncRequestUtils.calls.clear()
    p = _fresh_plugin()
    p.init_plugin({
        "enabled": True, "router_address": "192.168.1.1",
        "backup_target": "both", "backup_prefix": "rosguard",
        "backup_encrypt": False, "sections": ["ip_dns_static"],
        "auto_baseline": True, "keep": 1,
    })
    ros_files = [
        {".id": "*new", "name": "rosguard_20261110_000000_000.backup"},
        {".id": "*newr", "name": "rosguard_20261110_000000_000.rsc"},
        {".id": "*old", "name": "rosguard_20261101_000000_000.backup"},
        {".id": "*oldr", "name": "rosguard_20261101_000000_000.rsc"},
    ]
    # 新快照 sid 取当前时间，字典序一定大于 20261110（除非跨到更早，测试内即时生成不会）
    AsyncRequestUtils.responses = _seed_responses(
        ["ip_dns_static"], backup_target="both", ros_files=ros_files,
        deletable_ids=("*old", "*oldr"),
    )
    r = asyncio.run(p._async_run("双份"))
    assert r["ok"] is True and r["ros_backup"]["done"] is True
    assert r["snapshot_id"], "both 模式应写 MP 快照"
    assert len(p.get_data(_KEY_INDEX)) == 1
    assert r["ros_backup"]["removed"] == 2, "应删除旧的一组 .backup+.rsc"
    del_calls = [c for c in AsyncRequestUtils.calls if c[0] == "DELETE"]
    assert len(del_calls) == 2


def test_end_to_end_no_drift(monkeypatch=None):
    p = _fresh_plugin()
    p.init_plugin({
        "enabled": True, "router_address": "192.168.1.1",
        "sections": ["ip_dns_static"], "auto_baseline": True, "notify_mode": "drift",
    })
    AsyncRequestUtils.responses = _seed_responses(["ip_dns_static"])
    r1 = asyncio.run(p._async_run("基线"))
    assert r1["ok"] and r1["baseline_set"]

    AsyncRequestUtils.responses = _seed_responses(["ip_dns_static"])
    r2 = asyncio.run(p._async_run("复检"))
    assert r2["ok"] and r2["diff"]["has_drift"] is False
    assert "无变化" in r2["message"]


def test_set_baseline_and_clear():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4", "sections": ["ip_dns_static"]})
    AsyncRequestUtils.responses = _seed_responses(["ip_dns_static"])
    r = asyncio.run(p._async_run("首轮"))
    assert r["ok"]
    p.save_data(_KEY_BASELINE, None)  # 模拟基线被清空
    res = p.set_baseline()
    assert res["ok"] is True
    assert p.get_data(_KEY_BASELINE) is not None
    cleared = p.clear_snapshots()
    assert cleared["ok"] is True
    assert p.get_data(_KEY_BASELINE) is None
    assert p.snapshot_list() == []


def test_dry_run_does_not_persist():
    p = _fresh_plugin()
    p.init_plugin({
        "enabled": True, "router_address": "1.2.3.4",
        "sections": ["ip_dns_static"], "dry_run": True,
    })
    AsyncRequestUtils.responses = _seed_responses(["ip_dns_static"])
    r = asyncio.run(p._async_run("试运行"))
    assert r["ok"] is True
    assert r["snapshot_id"] == ""
    assert p.get_data(_KEY_BASELINE) is None
    assert p.snapshot_list() == []


def test_connection_failure_handled():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "10.0.0.1", "sections": ["ip_dns_static"]})
    AsyncRequestUtils.responses = [FakeResponse(401, {"detail": "Unauthorized"})]
    r = asyncio.run(p._async_run("失败测试"))
    assert r["ok"] is False
    assert "认证失败" in r["message"] or "失败" in r["message"]
