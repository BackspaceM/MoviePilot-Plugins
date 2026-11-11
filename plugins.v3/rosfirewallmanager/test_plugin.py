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
    _KEY_IMPORT,
    importer,
    roslink,
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
    assert len(RosFirewallManager.plugin_version.split(".")) == 3 and all(p.isdigit() for p in RosFirewallManager.plugin_version.split("."))
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
    # 关键字段存在：连接中心下拉 + 批量导入 + 扫描/安全
    for model in ("router_address", "ros_connection", "manage_tables",
                  "import_rules", "import_table", "import_place", "import_disabled",
                  "before_change_backup", "dry_run", "scan_cron", "display_limit"):
        assert model in models
    # 产品定位：不再提供「一条一条已知效果往里添加」的表单
    for model in ("new_rule_chain", "new_rule_action", "new_rule_dst_port",
                  "new_rule_log_prefix", "add_once"):
        assert model not in models, f"配置页不该再有逐条录入字段 {model}"
    assert len(defaults) <= 20, f"配置页字段仍然太多：{len(defaults)}"


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
    assert isinstance(page, list) and page
    text = str(page)
    assert "立即扫描隐患" in text
    assert "[x]" not in text              # 日志不再铺在详情页
    assert "运行日志" not in text


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
        api_map[f"plugin/RosFirewallManager{entry['path']}"] = entry["endpoint"]
        assert callable(entry["endpoint"]), f"{entry['path']} endpoint 不可调用"

    for btn in buttons:
        props = btn.get("props") or {}
        label = btn.get("text") or props.get("text", "?")
        assert label and label != "?", "按钮必须有可见文字（text 在组件顶层）"
        assert "disabled" not in props, f"按钮「{label}」含 disabled 属性"
        assert props.get("disabled") is not True, f"按钮「{label}」被禁用"
        click = (btn.get("events") or {}).get("click") or {}
        api = str(click.get("api") or "").lstrip("/")
        assert api, f"按钮「{label}」缺少 events.click.api（点了没反应）"
        assert api in api_map, f"按钮「{label}」的 API {api} 未在 get_api 注册（点了没反应）"
        assert click.get("method") == "post", f"按钮「{label}」method 非 post"

    # 规则级操作仍注册在接口里，明细不再铺在主界面
    assert {"/toggle", "/move", "/remove"} <= {e["path"] for e in p.get_api()}
    rule_btns = [b for b in buttons
                 if str(((b.get("events") or {}).get("click") or {}).get("api", "")).endswith(
                     ("/toggle", "/move", "/remove"))]
    # 逐条规则操作是防火墙管理核心功能，保留；但每个按钮必须命中真实 API、有文字、可点
    api_names = {e["path"].lstrip("/") for e in p.get_api()}
    for b in rule_btns:
        ev = (b.get("events") or {}).get("click") or {}
        assert str(ev.get("api", "")).split("/")[-1] in api_names
        assert b.get("text"), "按钮必须有可见文字（text 需在组件顶层）"
        assert not (b.get("props") or {}).get("disabled"), "不允许禁用按钮"

    # 主界面保留的破坏性操作要有二次确认
    confirm_buttons = [b for b in buttons
                       if ((b.get("events") or {}).get("click") or {}).get("confirm") is True]
    assert confirm_buttons


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


# ---------------------------------------------------------------------------
# 批量导入 / 连接中心 / 中文解释（本插件的核心价值）
# ---------------------------------------------------------------------------

def _all_nodes(node):
    if isinstance(node, dict):
        yield node
        content = node.get("content")
        if isinstance(content, (list, dict)):
            yield from _all_nodes(content)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _all_nodes(item)


_SAMPLE_SCRIPT = (
    "/ip firewall filter\n"
    "add chain=input action=accept protocol=tcp dst-port=8291\n"
    "add chain=forward action=fasttrack-connection "
    "connection-state=established,related\n"
    "/ip firewall nat\n"
    "add chain=srcnat action=masquerade out-interface=ether1\n"
    "add chain=dstnat action=dst-nat protocol=tcp dst-port=8096 "
    "to-addresses=192.168.88.10 to-ports=8096\n"
)


def test_importer_explains_rules_in_chinese():
    """导入后要告诉用户「这条规则会做什么」，而不只是 echo 原始参数。"""
    rules, errors = importer.parse_firewall_script(_SAMPLE_SCRIPT, "filter")
    assert not errors, errors
    assert len(rules) == 4
    assert "WinBox" in rules[0].meaning and "管理端口" in rules[0].risk
    assert "src-address" in rules[0].advice
    assert "FastTrack" in rules[1].meaning and "Queue" in rules[1].risk
    assert "共享上网" in rules[2].meaning
    assert "转发到" in rules[3].meaning and "192.168.88.10:8096" in rules[3].meaning
    preview = importer.build_import_preview(
        {}, text_override=_SAMPLE_SCRIPT, source_override="上传文件")
    brief = importer.format_import_brief(preview, 10)
    for word in ("会做什么", "影响", "注意", "建议", "命令"):
        assert word in brief
    assert "[x][y]" not in brief          # 动态文本已做 Markdown 安全转义
    assert preview["rules"][0]["command"].startswith("/ip firewall filter add")


def test_importer_recognizes_print_output_and_continuation():
    """WinBox 里 print 出来的规则、反斜杠续行也要能解析。"""
    text = (' 0  chain=input action=accept protocol=tcp dst-port=22\n'
            'add chain=input action=drop \\\n'
            '    in-interface-list=WAN comment="wan drop"\n')
    rules, errors = importer.parse_firewall_script(text, "filter")
    assert len(rules) == 2, errors
    assert "SSH" in rules[0].meaning
    assert "阻断" in rules[1].meaning or "丢弃" in rules[1].meaning


def test_pasted_text_generates_preview_on_save():
    """文本框方式：保存配置就自动生成中文解释预览。"""
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4",
                   "import_rules": "chain=input action=accept protocol=tcp dst-port=8291",
                   "import_disabled": True})
    preview = dict(p.get_data(_KEY_IMPORT) or {})
    assert preview["source"] == "粘贴规则文本"
    assert len(preview["rules"]) == 1
    assert preview["rules"][0]["disabled"] is True
    assert "WinBox" in preview["rules"][0]["meaning"]
    import json
    assert "会做什么" in json.dumps(p.get_page(), ensure_ascii=False)


def test_upload_file_api_parses_content():
    """上传文件方式：JS 把文件文本 POST 到 /import_text，兼容 base64/data URL。"""
    import base64
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4"})
    text = "chain=forward action=drop in-interface-list=WAN"
    res = p.api_import_text({"text": text, "name": "firewall.rsc"})
    assert res.success and res.data["count"] == 1
    preview = dict(p.get_data(_KEY_IMPORT) or {})
    assert "上传文件" in preview["source"] and "firewall.rsc" in preview["source"]
    assert "阻断" in preview["rules"][0]["meaning"] or "丢弃" in preview["rules"][0]["meaning"]
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    res2 = p.api_import_text({"text": "data:text/plain;base64," + b64,
                              "name": "encoded.txt"})
    assert res2.success and res2.data["count"] == 1
    assert p.api_import_text({"text": ""}).success is False


def test_upload_button_has_executable_script():
    """上传控件必须是 html 原生 input + inline onchange，不能再用 props.onclick。

    props.onclick 字符串会被 Vue 当 DOM property 赋值（字符串不是函数）→ 点击无反应。
    """
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4"})
    htmls = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("html"):
                htmls.append(str(n["html"]))
            for c in (n.get("content") or []):
                walk(c)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    for card in p.get_page():
        walk(card)
    assert htmls, "详情页缺少上传控件的 html 节点"
    html = "\n".join(htmls)
    assert "上传 .rsc / 规则文件" in html
    assert "onchange=" in html and 'type="file"' in html
    assert "window.MoviePilotAPI" in html and "/import_text" in html
    assert "rfmUploadTip" in html, "上传后要有就地显示中文解释的容器"
    # 页面里不能残留把脚本塞进 props.onclick 的写法
    for b in _page_buttons(p):
        assert "onclick" not in (b.get("props") or {}), "禁止 props.onclick 字符串脚本"


def test_upload_script_has_no_html_attribute_breakers():
    """inline 脚本里不能出现双引号或 & （会破坏 HTML 属性解析）。"""
    from app.plugins.rosfirewallmanager import _UPLOAD_JS, _UPLOAD_HTML
    assert '"' not in _UPLOAD_JS
    assert "&" not in _UPLOAD_JS, "innerHTML 属性值里的 & 可能被当字符引用"
    assert _UPLOAD_JS.startswith("var tip="), "onchange 直接写语句，this 才是 input"
    assert "this.files" in _UPLOAD_JS
    assert _UPLOAD_JS in _UPLOAD_HTML
    assert "A.post(" in _UPLOAD_JS and "rd.readAsText(f)" in _UPLOAD_JS


def test_all_api_routes_can_register_in_fastapi():
    """按宿主真实注册方式逐条 add_api_route，防止再出现「按钮 Not Found」。

    宿主 app/api/endpoints/plugin.py 对每条 get_api() 执行 add_api_route(**api)，
    失败只写日志；旧版 endpoint 的 -> Response 注解是普通 class，FastAPI 抛
    FastAPIError，12 条路由静默注册失败。
    """
    try:
        from fastapi import FastAPI
    except ImportError:
        # 离线环境没有 fastapi 时，退化为契约检查
        for e in _fresh_plugin().get_api():
            assert e.get("response_model", "MISSING") is None, \
                f"{e['path']} 未显式关闭 response_model"
        return
    app = FastAPI()
    entries = _fresh_plugin().get_api()
    failed = []
    for entry in entries:
        api = dict(entry)
        api.pop("auth", None)
        api["path"] = "/api/v1/plugin/RosFirewallManager" + api["path"]
        try:
            app.add_api_route(**api, tags=["plugin"])
        except Exception as err:  # noqa: BLE001
            failed.append((api["path"], type(err).__name__, str(err)[:120]))
    assert not failed, f"以下路由在宿主里注册不了（点击必 404）：{failed}"
    registered = {getattr(r, "path", "") for r in app.routes}
    for name in ("scan", "refresh", "test", "router_backup", "stop",
                 "import_text", "import_apply", "import_clear"):
        assert f"/api/v1/plugin/RosFirewallManager/{name}" in registered


def test_import_effect_summary_explains_overall_change():
    """新增的整体摘要：回答「这批规则写进去后防火墙会怎样」。"""
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4",
                   "import_rules": ("chain=input action=accept protocol=tcp dst-port=8291\n"
                                    "add chain=input action=drop")})
    preview = dict(p.get_data(_KEY_IMPORT) or {})
    text = importer.summarize_import_effect(preview, existing_count=7, dry_run=True)
    assert "导入后会发生什么" in text and "共 2 条" in text
    assert "写入后约 9 条" in text
    assert "位置：" in text and "动作：" in text and "影响：" in text
    assert "高危" in text, "无条件 drop 应被点名为高危"
    assert "试运行已开启" in text
    import json
    assert "导入后会发生什么" in json.dumps(p.get_page(), ensure_ascii=False)


def test_upload_api_returns_brief_for_instant_feedback():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4"})
    res = p.api_import_text({"text": "chain=input action=accept protocol=tcp dst-port=8291",
                             "name": "firewall.rsc"})
    assert res.success
    assert "WinBox" in res.data["brief"] or "会做什么" in res.data["brief"]
    assert "导入后会发生什么" in res.data["brief"]


def test_form_has_file_input_without_model_leak():
    form, defaults = _fresh_plugin().get_form()
    blob = __import__("json").dumps(form, ensure_ascii=False)
    assert "VFileInput" in blob and "onUpdate:modelValue" in blob
    assert "import_rules=String" in blob, "选文件后要把文本写进粘贴框模型"
    assert "import_file_path" in defaults, "保留容器内路径兜底入口"


def test_container_file_path_works_without_browser():
    """浏览器上传不可用时的零 JS 兜底：填容器内的 .rsc 路径也能生成解释。"""
    import os
    import tempfile
    p = _fresh_plugin()
    fd, path = tempfile.mkstemp(suffix=".rsc")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("/ip firewall filter\n"
                    "add chain=input action=accept protocol=tcp dst-port=8291\n")
        p.init_plugin({"enabled": True, "router_address": "1.2.3.4",
                       "import_file_path": path, "import_rules": ""})
        preview = dict(p.get_data(_KEY_IMPORT) or {})
        assert len(preview.get("rules") or []) == 1
        assert "容器内文件" in preview["source"]
        assert "WinBox" in preview["rules"][0]["meaning"]
    finally:
        os.unlink(path)


def test_import_apply_writes_all_rules_before_drop():
    """确认批量导入：一次写入全部规则，且都插在兜底 drop 之前。"""
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "manage_tables": ["filter"], "before_change_backup": True,
                   "import_rules": ("chain=input action=accept protocol=tcp dst-port=8291\n"
                                    "chain=input action=accept protocol=udp dst-port=53")})
    state, responder, backups = _stateful_responder(_default_tables_state())
    AsyncRequestUtils.responder = responder
    before = len(state["filter"])
    res = p.api_import_apply()
    assert res.success and res.data["total"] == 2 and res.data["success"] == 2, res.message
    assert len(state["filter"]) == before + 2
    assert backups["n"] >= 1
    order = [r[".id"] for r in state["filter"]]
    for port in ("8291", "53"):
        rid = next(r[".id"] for r in state["filter"]
                   if str(r.get("dst-port") or "") == port)
        assert order.index(rid) < order.index("*5"), f"{port} 落到了兜底 drop 之后"


def test_import_apply_dry_run_writes_nothing():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "manage_tables": ["filter"], "dry_run": True,
                   "before_change_backup": True,
                   "import_rules": "chain=input action=accept protocol=tcp dst-port=8291"})
    state, responder, backups = _stateful_responder(_default_tables_state())
    AsyncRequestUtils.responder = responder
    before = len(state["filter"])
    res = p.api_import_apply()
    assert res.success and res.data["dry_run"] is True
    assert len(state["filter"]) == before
    assert backups["n"] == 0
    mutating = [(m, u) for m, u, _ in AsyncRequestUtils.calls
                if m in ("PUT", "PATCH", "DELETE")]
    assert mutating == [], mutating


def test_import_clear_and_apply_without_preview():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1"})
    assert p.api_import_apply().success is False, "没有预览时不该写入"
    p.api_import_text({"text": "chain=input action=accept protocol=tcp dst-port=22"})
    assert p.get_data(_KEY_IMPORT)["rules"]
    assert p.api_import_clear().success
    assert not p.get_data(_KEY_IMPORT)


def test_connection_center_overrides_credentials():
    """下拉选中连接中心已保存的路由器，自动带入地址/端口/账号/密码。"""
    p = _fresh_plugin()
    roslink.write_shared([{
        "name": "主路由", "address": "10.0.0.1", "port": 8443,
        "username": "cc-user", "password": "cc-pass", "ssl": True,
    }])
    try:
        p.init_plugin({"enabled": True, "router_address": "1.2.3.4",
                       "router_username": "manual",
                       "ros_connection": "主路由"})
        assert p._address == "10.0.0.1"
        assert p._port == 8443
        assert p._username == "cc-user"
        assert p._password == "cc-pass"
        import json
        form, _ = p.get_form()
        assert "主路由" in json.dumps(form, ensure_ascii=False), "下拉里要列出已保存路由器"
    finally:
        roslink.write_shared([])


def test_connection_center_missing_falls_back_to_manual():
    p = _fresh_plugin()
    roslink.write_shared([])
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4",
                   "router_username": "manual", "ros_connection": "不存在的路由器"})
    assert p._address == "1.2.3.4" and p._username == "manual"


def test_form_is_simplified_and_has_no_bare_labels():
    import json
    p = _fresh_plugin()
    form, defaults = p.get_form()
    blob = json.dumps(form, ensure_ascii=False)
    for model in ("new_rule_chain", "new_rule_action", "new_rule_dst_port",
                  "new_rule_log_prefix", "add_once"):
        assert model not in blob
    assert len(defaults) <= 20
    assert "粘贴 RouterOS" in blob and "已保存的路由器" in blob
    for node in _all_nodes(form):
        assert node.get("component") not in ("VAlert", "VLabel"), "不要只有图标没有说明的元素"
        assert not isinstance(node.get("content"), dict), "content 必须是数组"


def test_page_shows_import_explanation_without_markdown_artifacts():
    import json
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4",
                   "import_rules": 'chain=input action=accept protocol=tcp '
                                   'dst-port=8291 comment="[x][y] *b* _c_`d`"'})
    blob = json.dumps(p.get_page(), ensure_ascii=False)
    assert "会做什么" in blob and "WinBox" in blob
    assert "[x][y]" not in blob and "［x］［y］" in blob
    assert "运行日志" not in blob, "主界面不重复铺运行日志"


def test_import_apis_registered_with_auth():
    p = _fresh_plugin()
    paths = {e["path"] for e in p.get_api()}
    assert {"/import_text", "/import_apply", "/import_clear"} <= paths
    for e in p.get_api():
        assert e.get("auth") == "bear" and callable(e.get("endpoint"))
