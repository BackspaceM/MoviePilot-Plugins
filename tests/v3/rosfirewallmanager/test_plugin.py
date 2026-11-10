"""RouterOS 防火墙可视化管理器 —— 离线桩测试。

覆盖：core 纯逻辑（规则规范化 / 影子·重复·缺兜底检测 / 排序目标 / payload）、
V3 契约、表单字段↔defaults 一致、端到端扫描、插到兜底 drop 前、启停/排序/删除、
试运行不落盘、前置备份、通知脱敏，以及"详情页每个按钮都有真实可点 API"。
"""

from __future__ import annotations

import asyncio

from app.plugins.rosfirewallmanager import (
    PLUGIN_NAME,
    RosFirewallManager,
    _KEY_FINDINGS,
    _KEY_LAST_OP,
    _KEY_LOGS,
    _KEY_RULES,
    _KEY_SNAPSHOT,
)
from app.plugins.rosfirewallmanager.clients import (
    RouterOSFirewallClient,
    normalize_base_url,
)
from app.plugins.rosfirewallmanager.core import (
    PLACE_BOTTOM,
    TABLES,
    Rule,
    build_add_payload,
    choose_insert_place,
    compute_move_place,
    detect_findings,
    format_findings_text,
    format_log_line,
    format_preview_text,
    format_rules_text,
    mask_secrets,
    md_safe,
    md_safe_command,
    preview_command,
    summarize_rules,
    validate_cron,
)
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fresh_plugin() -> RosFirewallManager:
    _PluginBase.POSTED.clear()
    _PluginBase.CONFIGS.clear()
    AsyncRequestUtils.calls = []
    AsyncRequestUtils.responder = None
    p = RosFirewallManager()
    p._data = {}
    p._config = {}
    return p


def _raw(rid, chain, action, table="filter", **kw):
    d = {".id": rid, "chain": chain, "action": action}
    d.update(kw)
    d["_table"] = table
    return d


def _stateful_responder(tables=None):
    """维护四表规则数组状态的 responder：PUT 按 place-before 插入，PATCH 移动/改字段，DELETE 删除。"""
    state = {
        "filter": list((tables or {}).get("filter", [])),
        "nat": list((tables or {}).get("nat", [])),
        "mangle": list((tables or {}).get("mangle", [])),
        "raw": list((tables or {}).get("raw", [])),
        "backups": [], "seq": 900,
    }
    backups_called = {"n": 0}

    def responder(method, url, kwargs):
        if url.endswith("/system/identity"):
            return FakeResponse(200, {"name": "ROS-Test", "version": "7.14"})
        if url.endswith("/system/backup/save"):
            backups_called["n"] += 1
            state["backups"].append((kwargs.get("json") or {}).get("name", "?"))
            return FakeResponse(200, {})
        # /rest/ip/firewall/{table}[/{id}]
        marker = "/rest/ip/firewall/"
        if marker in url:
            tail = url.split(marker, 1)[1]
            parts = [p for p in tail.split("/") if p]
            table = parts[0] if parts else "filter"
            if table not in state:
                return FakeResponse(404, {})
            rows = state[table]
            rid = parts[1] if len(parts) > 1 else None
            if rid is None:
                if method == "GET":
                    # 去掉内部 _table 字段，模拟 RouterOS 返回
                    return FakeResponse(200, [{k: v for k, v in r.items() if k != "_table"}
                                              for r in rows])
                if method == "PUT":
                    body = dict(kwargs.get("json") or {})
                    place = body.pop("place-before", None)
                    state["seq"] += 1
                    new = dict(body)
                    new[".id"] = f"*{state['seq']:X}"
                    if place:
                        if place == PLACE_BOTTOM:
                            rows.append(new)
                        else:
                            idx = next((i for i, r in enumerate(rows) if r.get(".id") == place), None)
                            if idx is None:
                                rows.append(new)
                            else:
                                rows.insert(idx, new)
                    else:
                        rows.append(new)
                    return FakeResponse(201, {k: v for k, v in new.items() if k != "_table"})
            else:
                row = next((r for r in rows if r.get(".id") == rid), None)
                if row is None:
                    return FakeResponse(404, {})
                if method == "PATCH":
                    body = dict(kwargs.get("json") or {})
                    if "place-before" in body:
                        place = body["place-before"]
                        idx = rows.index(row)
                        rows.pop(idx)
                        if place == PLACE_BOTTOM:
                            rows.append(row)
                        else:
                            dest = next((i for i, r in enumerate(rows) if r.get(".id") == place), None)
                            rows.insert(dest if dest is not None else len(rows), row)
                    else:
                        row.update(body)
                    return FakeResponse(200, {k: v for k, v in row.items() if k != "_table"})
                if method == "DELETE":
                    rows.remove(row)
                    return FakeResponse(204, {})
        return FakeResponse(200, [])

    return state, responder, backups_called


def _default_tables_state():
    """一条典型 filter：established 放行 → 兜底 drop；其后一条死掉的 winbox 放行。"""
    return {
        "filter": [
            _raw("*1", "input", "accept", connection_state="established,related", comment="allow established"),
            _raw("*5", "input", "drop", **{"in-interface-list": "!LAN"}, comment="drop all not from LAN"),
            _raw("*6", "input", "accept", protocol="tcp", **{"dst-port": "8291"}, comment="winbox(死规则)"),
        ],
        "nat": [
            _raw("*2", "srcnat", "masquerade", table="nat", **{"out-interface": "ether1"}, comment="masq"),
        ],
    }


# ---------------------------------------------------------------------------
# core：规则规范化与隐患检测
# ---------------------------------------------------------------------------

def test_rule_from_raw_and_signature():
    r = Rule.from_raw(_raw("*1", "input", "accept", protocol="tcp", **{"dst-port": "80"}), "filter")
    assert r.id == "*1" and r.chain == "input" and r.action == "accept"
    assert r.matchers == {"protocol": "tcp", "dst-port": "80"}
    assert not r.disabled and not r.dynamic and not r.is_catchall()
    assert "dst-port=80" in r.matcher_summary()
    assert "accept" in r.display_line(1)
    # 序列化往返
    assert Rule.from_dict(r.to_dict()).signature() == r.signature()
    # disabled 解析
    d = Rule.from_raw(_raw("*2", "input", "drop", disabled="true", dynamic="yes"), "filter")
    assert d.disabled and d.dynamic


def test_detect_shadowed_accept():
    rules = [Rule.from_raw(x, "filter") for x in _default_tables_state()["filter"]]
    f = detect_findings(rules)
    codes = [x.code for x in f]
    assert "shadowed_accept" in codes, codes
    sh = next(x for x in f if x.code == "shadowed_accept")
    assert sh.severity == "high" and "*6" in sh.rule_ids and "*5" in sh.rule_ids


def test_detect_dead_drop_and_unreachable():
    rules = [
        Rule.from_raw(_raw("*1", "forward", "accept", comment="accept all"), "filter"),
        Rule.from_raw(_raw("*2", "forward", "drop", comment="block lan"), "filter"),
    ]
    f = detect_findings(rules)
    codes = [x.code for x in f]
    assert "dead_drop" in codes and "unreachable" in codes, codes
    assert next(x for x in f if x.code == "dead_drop").severity == "high"


def test_detect_duplicate_and_no_default_drop():
    dup = [
        Rule.from_raw(_raw("*1", "input", "accept", protocol="tcp", **{"dst-port": "22"}), "filter"),
        Rule.from_raw(_raw("*2", "input", "accept", protocol="tcp", **{"dst-port": "22"}), "filter"),
    ]
    f = detect_findings(dup)
    assert "duplicate" in [x.code for x in f]
    assert "no_default_drop" in [x.code for x in f]  # input 无兜底
    # 带兜底则不报缺兜底
    dup.append(Rule.from_raw(_raw("*3", "input", "drop", comment="default"), "filter"))
    assert "no_default_drop" not in [x.code for x in detect_findings(dup)]


def test_dynamic_rules_ignored_in_detection():
    rules = [
        Rule.from_raw(_raw("*1", "input", "drop", dynamic="true"), "filter"),
        Rule.from_raw(_raw("*2", "input", "accept", protocol="tcp", **{"dst-port": "80"}), "filter"),
    ]
    f = detect_findings(rules)
    # 动态 drop 不参与遮挡，因此不报 shadowed_accept
    assert "shadowed_accept" not in [x.code for x in f]


# ---------------------------------------------------------------------------
# core：排序 / 插入 / payload
# ---------------------------------------------------------------------------

def test_compute_move_place():
    ids = ["*1", "*2", "*3", "*4"]
    assert compute_move_place(ids, "*3", "up") == (True, "*2", "上移一位（插到 *2 之前）")
    down = compute_move_place(ids, "*2", "down")
    assert down[0] is True and down[1] == "*4"  # 插到下下条前 = 与 *3 交换
    assert compute_move_place(ids, "*2", "top") == (True, "*1", "置顶（插到 *1 之前）")
    assert compute_move_place(ids, "*2", "bottom") == (True, PLACE_BOTTOM, "置底（末条之后）")
    # 边界：不需要移动
    assert compute_move_place(ids, "*1", "up")[0] is False
    assert compute_move_place(ids, "*4", "down")[0] is False
    assert compute_move_place(ids, "*1", "top")[0] is False
    assert compute_move_place(ids, "*4", "bottom")[0] is False
    assert compute_move_place(ids, "*9", "up")[0] is False  # 不存在


def test_choose_insert_place_before_drop():
    rules = [Rule.from_raw(x, "filter") for x in _default_tables_state()["filter"]]
    place, note = choose_insert_place(rules, "input", "before_drop")
    assert place == "*5" and "兜底" in note
    assert choose_insert_place(rules, "input", "top")[0] == "*1"
    assert choose_insert_place(rules, "input", "bottom")[0] is None
    assert choose_insert_place(rules, "input", "before_id", "*1")[0] == "*1"
    assert choose_insert_place(rules, "input", "before_id", "*999")[0] is None  # 不存在则追加
    # 无兜底的链 -> 追加
    only = [Rule.from_raw(_raw("*1", "output", "accept"), "filter")]
    assert choose_insert_place(only, "output", "before_drop")[0] is None


def test_build_add_payload_and_preview():
    ok, payload, err = build_add_payload(
        "filter", {"chain": "input", "action": "accept", "protocol": "tcp",
                   "dst_port": "8291", "in_interface": "LAN", "comment": "winbox",
                   "disabled": "false", "log": "true", "log_prefix": "rfw:"})
    assert ok and not err
    assert payload["dst-port"] == "8291" and payload["in-interface"] == "LAN"
    assert "dst_port" not in payload and "in_interface" not in payload
    assert payload["log"] is True and payload["log-prefix"] == "rfw:"
    assert payload["disabled"] is False
    cmd = preview_command("filter", payload, "*5")
    assert "/ip firewall filter add" in cmd and "place-before=*5" in cmd
    # 缺参
    assert build_add_payload("filter", {"action": "accept"})[0] is False
    assert build_add_payload("filter", {"chain": "input"})[0] is False
    assert build_add_payload("bogus", {"chain": "input", "action": "accept"})[0] is False


def test_format_and_safety():
    assert md_safe("[x][y]") == "［x］［y］"
    assert "[" not in md_safe("a*b_c `d` <e>")
    # 命令预览须保留 RouterOS 的 *id（准确可复制），但其它触发符仍转义
    cmd = md_safe_command('filter add place-before=*5 comment="a_b[x]"')
    assert "place-before=*5" in cmd and "a＿b［x］" in cmd
    line = format_log_line("2026-01-01 00:00:00", "INFO", "规则 [x] 已应用")
    assert "｜" in line and "［x］" in line and "[x]" not in line
    rules = [Rule.from_raw(x, "filter") for x in _default_tables_state()["filter"]]
    txt = format_rules_text(rules, "filter")
    assert "执行顺序" in txt and "winbox" in txt
    f = detect_findings(rules)
    assert "高危" in format_findings_text(f)
    assert "未发现" in format_findings_text([])
    assert "等价命令" in format_preview_text({"chain": "input", "action": "drop"}, "filter", "*5", "note")


def test_validate_cron_mask_and_url():
    assert validate_cron("*/30 * * * *")[0] is True
    assert validate_cron("0 9 * * 1")[0] is True
    assert validate_cron("bad")[0] is False
    assert validate_cron("70 * * * *")[0] is False
    assert "S3cret" not in mask_secrets("pass S3cretPass", ["S3cretPass"])
    assert normalize_base_url("192.168.1.1") == "https://192.168.1.1"
    assert normalize_base_url("192.168.1.1", 8080, False) == "http://192.168.1.1:8080"
    assert normalize_base_url("https://ros.local/rest") == "https://ros.local"


# ---------------------------------------------------------------------------
# V3 契约 / 表单
# ---------------------------------------------------------------------------

def test_contract_attributes():
    assert RosFirewallManager.plugin_version == "1.0.0"
    assert RosFirewallManager.plugin_name == PLUGIN_NAME
    assert RosFirewallManager.plugin_author == "BackspaceM"
    assert RosFirewallManager.plugin_config_prefix == "rosfirewallmanager_"
    assert RosFirewallManager.plugin_icon.startswith("https://")
    assert hasattr(RosFirewallManager, "get_form")


def test_init_and_state():
    p = _fresh_plugin()
    assert p.get_state() is False
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "manage_tables": ["filter", "nat"], "timeout": "15"})
    assert p.get_state() is True
    assert p._address == "192.168.1.1" and p._timeout == 15
    assert p._tables == ["filter", "nat"]
    # 非法表被过滤、空则回落默认
    p2 = _fresh_plugin()
    p2.init_plugin({"manage_tables": ["filter", "bogus"]})
    assert p2._tables == ["filter"]


def test_get_form_fields_match_defaults():
    p = _fresh_plugin()
    form, defaults = p.get_form()
    assert isinstance(form, list) and isinstance(defaults, dict)
    # 收集表单里所有 model，必须与 defaults 键完全一致（杜绝空白占位/错乱）
    models = set()

    def walk(node):
        if isinstance(node, dict):
            m = (node.get("props") or {}).get("model")
            if isinstance(m, str):
                models.add(m)
            for child in (node.get("content") or []):
                walk(child)
            if isinstance(node.get("content"), dict):
                walk(node["content"])
        elif isinstance(node, list):
            for c in node:
                walk(c)

    walk(form)
    assert models == set(defaults), f"表单字段与默认值不一致: {models ^ set(defaults)}"
    # 关键字段存在
    for model in ("router_address", "manage_tables", "new_rule_chain", "new_rule_place",
                  "before_change_backup", "add_once", "dry_run", "scan_cron"):
        assert model in models


def test_get_service_trigger():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "scan_enabled": True, "scan_cron": "*/15 * * * *",
                   "router_address": "1.2.3.4"})
    svcs = p.get_service()
    assert len(svcs) == 1
    assert svcs[0]["trigger"] == ("cron", "*/15 * * * *")
    assert svcs[0]["func"] == p.start_scan
    # 未启用定时 -> 无任务
    p2 = _fresh_plugin()
    p2.init_plugin({"enabled": True, "scan_enabled": False, "router_address": "1.2.3.4"})
    assert p2.get_service() == []
    # 非法 cron 回落
    p3 = _fresh_plugin()
    p3.init_plugin({"enabled": True, "scan_enabled": True, "scan_cron": "bad"})
    assert p3.get_service()[0]["trigger"] == ("cron", "0 9 * * 1")


def test_add_once_reset_flips_config():
    p = _fresh_plugin()
    # enabled=False 避免真实下发；复位逻辑与 enabled 无关
    p.init_plugin({"enabled": False, "router_address": "1.2.3.4", "add_once": True})
    assert _PluginBase.CONFIGS, "add_once 应触发 update_config 复位"
    assert _PluginBase.CONFIGS[-1].get("add_once") is False


def test_get_command():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4"})
    cmds = {c["cmd"] for c in p.get_command()}
    assert {"/rfw_scan", "/rfw_status", "/rfw_refresh", "/rfw_backup"} <= cmds


# ---------------------------------------------------------------------------
# 详情页按钮"可点"校验（本次重点）
# ---------------------------------------------------------------------------

def _walk(node, out):
    if isinstance(node, dict):
        if node.get("component") == "VBtn":
            out.append(node)
        content = node.get("content")
        if isinstance(content, list):
            for c in content:
                _walk(c, out)
        elif isinstance(content, dict):
            _walk(content, out)
    elif isinstance(node, list):
        for c in node:
            _walk(c, out)


def _page_buttons(p):
    buttons = []
    for card in p.get_page():
        _walk(card, buttons)
    return buttons


def test_get_page_renders_and_md_safe():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4", "manage_tables": ["filter"]})
    p.save_data(_KEY_RULES, [r.to_dict() for r in
                             [Rule.from_raw(x, "filter") for x in _default_tables_state()["filter"]]])
    p.save_data(_KEY_LOGS, [{"time": "2026-01-01 00:00:00", "level": "INFO",
                             "message": "规则 [x] 已应用"}])
    page = p.get_page()
    assert isinstance(page, list) and page[0]["component"] == "VCard"
    text = str(page)
    assert "立即扫描隐患" in text and "隐患检测" in text
    assert "[x]" not in text and "［x］" in text  # 空药丸防御


def test_page_render_no_blank_pill_pattern():
    """即便规则备注/日志被塞入 Markdown 触发字符，详情页也不应出现半角 [ ] 等。"""
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4", "manage_tables": ["filter"]})
    rules = [Rule.from_raw(_raw("*1", "input", "accept", comment="[坏]备注 *粗* `_x_` <y>"), "filter")]
    p.save_data(_KEY_RULES, [r.to_dict() for r in rules])
    p.save_data(_KEY_LOGS, [{"time": "t", "level": "INFO", "message": "[a][b] *c* `d`"}])
    texts = []

    def walk(n):
        if isinstance(n, dict):
            t = n.get("text")
            if isinstance(t, str):
                texts.append(t)
            c = n.get("content")
            if isinstance(c, list):
                for x in c:
                    walk(x)
            elif isinstance(c, dict):
                walk(c)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    for card in p.get_page():
        walk(card)
    bad = [t for t in texts if any(ch in t for ch in ("[", "]", "*", "`", "<", ">"))]
    assert bad == [], f"详情页出现 Markdown 触发字符: {bad}"


def test_every_page_button_has_live_api():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4", "manage_tables": ["filter"]})
    p.save_data(_KEY_RULES, [r.to_dict() for r in
                             [Rule.from_raw(x, "filter") for x in _default_tables_state()["filter"]]])
    buttons = _page_buttons(p)
    assert buttons, "详情页应至少有操作按钮"

    api_map = {}
    for entry in p.get_api():
        api_map[entry["path"].lstrip("/")] = entry["endpoint"]
        assert callable(entry["endpoint"]), f"{entry['path']} endpoint 不可调用"

    for btn in buttons:
        props = btn.get("props") or {}
        label = props.get("text", "?")
        assert "disabled" not in props, f"按钮「{label}」含 disabled 属性"
        assert props.get("disabled") is not True, f"按钮「{label}」被禁用"
        click = (btn.get("events") or {}).get("click") or {}
        api = str(click.get("api") or "").lstrip("/")
        assert api, f"按钮「{label}」未绑定 api"
        assert api in api_map, f"按钮「{label}」的 API {api} 未在 get_api 注册（点了没反应）"
        assert click.get("method") == "post", f"按钮「{label}」method 非 post"

    # 每条规则的 6 个操作按钮都带 table+id 参数
    rule_btns = [b for b in buttons
                 if str(((b.get("events") or {}).get("click") or {}).get("api", "")).endswith(
                     ("/toggle", "/move", "/remove"))]
    assert len(rule_btns) >= 6 * 3, "三条规则应各有 6 个操作按钮"
    for b in rule_btns:
        params = b["events"]["click"].get("params") or {}
        assert params.get("table") and params.get("id"), f"规则按钮缺 table/id: {b['props'].get('text')}"

    # 破坏性操作有二次确认
    for suffix, expect in (("/remove", True), ("/move", None)):
        group = [b for b in buttons if str(((b.get("events") or {}).get("click") or {}).get("api", "")).endswith(suffix)]
        if suffix == "/remove":
            assert group and all(b["events"]["click"].get("confirm") is True for b in group)
        # move 里只有置底需要 confirm
    bottom_btns = [b for b in buttons
                   if str(((b.get("events") or {}).get("click") or {}).get("api", "")).endswith("/move")
                   and (b["events"]["click"].get("params") or {}).get("mode") == "bottom"]
    assert bottom_btns and all(b["events"]["click"].get("confirm") is True for b in bottom_btns)


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------

def test_end_to_end_scan_detects_shadow():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "router_username": "admin", "router_password": "S3cretPass",
                   "manage_tables": ["filter", "nat"], "notify_mode": "issue"})
    state, responder, _ = _stateful_responder(_default_tables_state())
    AsyncRequestUtils.responder = responder

    result = asyncio.run(p._async_scan("测试-扫描"))
    assert result["ok"] is True, result.get("message")
    assert len(result["rules"]) == 4  # 3 filter + 1 nat
    assert result["high"] >= 1
    assert any(f["code"] == "shadowed_accept" for f in result["findings"])
    # _async_scan 负责持久化连接状态/规则/隐患；_KEY_LAST_SCAN 由 start_scan 的线程包装写入
    assert p.get_data(_KEY_RULES) and p.get_data(_KEY_FINDINGS)
    conn = p.get_data("rfm_conn")
    assert conn and conn.get("ok") is True

    # issue 模式 + 有高危 -> 发通知且脱敏
    _PluginBase.POSTED.clear()
    p._notify_scan(result, "测试-扫描")
    assert _PluginBase.POSTED
    assert "S3cretPass" not in str(_PluginBase.POSTED[-1])


def test_end_to_end_add_before_drop():
    """核心：新增规则必须插到兜底 drop(*5) 之前，而不是落到末尾变死规则。"""
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "manage_tables": ["filter"], "before_change_backup": True})
    state, responder, backups = _stateful_responder(_default_tables_state())
    AsyncRequestUtils.responder = responder

    payload = build_add_payload("filter", {"chain": "input", "action": "accept",
                                           "protocol": "tcp", "dst_port": "53", "comment": "dns"})[1]
    res = p._run_add("filter", payload, "before_drop", "", trigger="测试-新增")
    assert res["ok"] is True and res["place"] == "*5"
    order = [r[".id"] for r in state["filter"]]
    # 新规则（responder 生成的 *385）应插在兜底 drop *5 之前
    new_id = next(r[".id"] for r in state["filter"] if r.get("comment") == "dns")
    assert order.index(new_id) < order.index("*5"), order
    assert order.index("*1") < order.index(new_id), order  # 在 established 放行之后
    # 改动前留了备份
    assert backups["n"] >= 1
    assert res["preview"].startswith("/ip firewall filter add")
    # 缓存已刷新
    assert len(p.get_data(_KEY_RULES)) == 4


def test_end_to_end_toggle_move_remove():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "manage_tables": ["filter"], "before_change_backup": False})
    state, responder, backups = _stateful_responder(_default_tables_state())
    AsyncRequestUtils.responder = responder

    # 禁用 *6
    r = p._run_single_op("toggle", "filter", "*6", disabled=True, trigger="测试")
    assert r["ok"] is True
    assert next(x for x in state["filter"] if x[".id"] == "*6")["disabled"] is True
    # 上移 *6：应在 *5 之前
    r = p._run_single_op("move", "filter", "*6", mode="up", trigger="测试")
    assert r["ok"] is True
    order = [x[".id"] for x in state["filter"]]
    assert order.index("*6") < order.index("*5"), order
    # 边界：已在顶部的 *1 再上移，返回成功提示而非报错
    r = p._run_single_op("move", "filter", "*1", mode="up", trigger="测试")
    assert r["ok"] is True and "顶部" in r["message"]
    # 删除 *6
    r = p._run_single_op("remove", "filter", "*6", trigger="测试")
    assert r["ok"] is True
    assert "*6" not in [x[".id"] for x in state["filter"]]
    # 隐患重算：死规则没了
    assert not any(f["code"] == "shadowed_accept" for f in p.get_data(_KEY_FINDINGS))


def test_dry_run_does_not_mutate():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "manage_tables": ["filter"], "dry_run": True, "before_change_backup": True})
    state, responder, backups = _stateful_responder(_default_tables_state())
    AsyncRequestUtils.responder = responder
    before = len(state["filter"])

    payload = build_add_payload("filter", {"chain": "input", "action": "accept",
                                           "protocol": "tcp", "dst_port": "53"})[1]
    res = p._run_add("filter", payload, "before_drop", "", trigger="测试-试运行")
    assert res["ok"] is True and res["dry_run"] is True
    assert len(state["filter"]) == before, "试运行不应新增真实规则"
    assert backups["n"] == 0, "试运行不应触发备份"
    # 无写操作（PUT/PATCH/DELETE/POST backup）
    mutating = [(m, u) for m, u, _ in AsyncRequestUtils.calls
                if m in ("PUT", "PATCH", "DELETE") or (m == "POST" and "backup" in u)]
    assert mutating == [], f"试运行出现写操作: {mutating}"


def test_api_endpoints_accept_body():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "manage_tables": ["filter"], "before_change_backup": False})
    state, responder, _ = _stateful_responder(_default_tables_state())
    AsyncRequestUtils.responder = responder

    assert p.api_test().success is True
    assert p.api_refresh().success is True
    assert p.api_scan().success is True
    assert p.api_status().success is True
    assert p.api_backup().success is True and p.get_data(_KEY_SNAPSHOT)
    assert p.api_router_backup().success is True
    # 带参操作
    assert p.api_toggle({"table": "filter", "id": "*6", "disabled": True}).success is True
    assert p.api_move({"table": "filter", "id": "*6", "mode": "up"}).success is True
    assert p.api_move({"table": "filter", "id": "*6", "mode": "bad"}).success is False
    assert p.api_move({}).success is False
    assert p.api_remove({"table": "filter", "id": "*6"}).success is True
    assert p.api_toggle({}).success is False
    # 新增预览与新增
    prev = p.api_add_preview({"table": "filter", "chain": "input", "action": "accept",
                              "protocol": "tcp", "dst_port": "53", "place": "before_drop"})
    assert prev.success and "place-before=*5" in prev.data["text"], prev.data["text"]
    add = p.api_add({"table": "filter", "chain": "input", "action": "accept",
                     "protocol": "udp", "dst_port": "53", "comment": "dns", "place": "before_drop"})
    assert add.success and add.data["place"] == "*5"
    assert p.api_add({"table": "filter", "action": "accept"}).success is False
    assert p.api_stop().success is True


def test_add_once_triggers_add_with_prefix():
    """配置页「立即添加这条规则」开关：解析字段、加备注前缀、复位开关。"""
    p = _fresh_plugin()
    state, responder, _ = _stateful_responder(_default_tables_state())
    AsyncRequestUtils.responder = responder
    p.init_plugin({
        "enabled": True, "router_address": "192.168.1.1", "manage_tables": ["filter"],
        "before_change_backup": False, "comment_prefix": "rfm",
        "new_rule_table": "filter", "new_rule_chain": "input", "new_rule_action": "accept",
        "new_rule_protocol": "tcp", "new_rule_dst_port": "8291",
        "new_rule_comment": "winbox from LAN", "new_rule_place": "before_drop",
        "add_once": True,
    })
    # 开关复位
    assert _PluginBase.CONFIGS[-1].get("add_once") is False
    # 规则已新增且其 comment 带前缀
    placed = [r for r in state["filter"] if "rfm-" in str(r.get("comment", ""))]
    assert placed and placed[0]["comment"] == "rfm-winbox from LAN"
    assert [r[".id"] for r in state["filter"]].index(placed[0][".id"]) < \
           [r[".id"] for r in state["filter"]].index("*5")
