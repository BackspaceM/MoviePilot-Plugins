"""带宽/Queue 智能调度 —— 离线桩测试。

覆盖：core 纯逻辑、V3 契约、表单、时段方案、队列差异编排、端到端调度、
试运行不落盘、FastTrack 诊断，以及"详情页每个按钮都有真实可点 API"。
"""

from __future__ import annotations

import asyncio
import datetime

from app.plugins.bandwidthqueuescheduler import (
    PLUGIN_NAME,
    BandwidthQueueScheduler,
    _KEY_FASTTRACK,
    _KEY_LAST,
    _KEY_LOGS,
    _KEY_PROFILE,
    _KEY_RATES,
)
from app.plugins.bandwidthqueuescheduler.clients import (
    RouterOSQueueClient,
    normalize_base_url,
)
from app.plugins.bandwidthqueuescheduler.core import (
    DEFAULT_PROFILE,
    PROFILES,
    ROLES,
    available_profiles,
    build_queue_specs,
    diff_queue_specs,
    format_apply_text,
    format_fasttrack_text,
    format_log_line,
    format_rates_text,
    in_window,
    mask_secrets,
    match_profile,
    md_safe,
    merge_profiles,
    parse_bandwidth,
    parse_profile_json,
    parse_rules,
    parse_targets,
    time_to_minutes,
    validate_cron,
)
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fresh_plugin() -> BandwidthQueueScheduler:
    _PluginBase.POSTED.clear()
    AsyncRequestUtils.calls = []
    AsyncRequestUtils.responder = None
    p = BandwidthQueueScheduler()
    p._data = {}
    p._config = {}
    return p


_TARGETS_TEXT = (
    "下载机=192.168.1.30/32|download\n"
    "NAS=192.168.1.10/32|nas\n"
    "客厅电视=192.168.1.20/32|media\n"
    "手机=192.168.1.50/32"
)


def _state_machine(initial_queues=None, fasttrack=None):
    """构造一个按 URL 分派、维护队列状态的 responder，模拟 RouterOS。"""
    state = {"queues": list(initial_queues or []), "fasttrack": list(fasttrack or []),
             "seq": len(initial_queues or [])}

    def responder(method, url, kwargs):
        if url.endswith("/rest/system/identity"):
            return FakeResponse(200, {"name": "ROS-Test", "version": "7.14"})
        if "/ip/firewall/filter" in url:
            return FakeResponse(200, list(state["fasttrack"]))
        if "/queue/simple" in url:
            if method == "GET":
                return FakeResponse(200, list(state["queues"]))
            if method == "POST":
                payload = dict(kwargs.get("json") or {})
                state["seq"] += 1
                row = dict(payload)
                row[".id"] = f"*{state['seq']}"
                row.setdefault("rate", "0bps/0bps")
                row.setdefault("bytes", "0")
                state["queues"].append(row)
                return FakeResponse(201, row)
            if method in ("PUT", "PATCH"):
                rid = url.rsplit("/", 1)[-1]
                payload = dict(kwargs.get("json") or {})
                for row in state["queues"]:
                    if row.get(".id") == rid:
                        row.update(payload)
                        return FakeResponse(200, row)
                return FakeResponse(404, {})
            if method == "DELETE":
                rid = url.rsplit("/", 1)[-1]
                before = len(state["queues"])
                state["queues"] = [r for r in state["queues"] if r.get(".id") != rid]
                return FakeResponse(204 if len(state["queues"]) < before else 404, {})
        return FakeResponse(200, [])

    return state, responder


# ---------------------------------------------------------------------------
# core：解析
# ---------------------------------------------------------------------------

def test_parse_targets():
    targets = parse_targets(_TARGETS_TEXT + "\n# 注释=行\nbadline-no-target")
    assert len(targets) == 4
    by_name = {t["name"]: t for t in targets}
    assert by_name["下载机"]["role"] == "download"
    assert by_name["下载机"]["target"] == "192.168.1.30/32"
    assert by_name["NAS"]["role"] == "nas"
    assert by_name["客厅电视"]["role"] == "media"
    assert by_name["手机"]["role"] == "other"       # 角色缺省
    # 逗号格式 + 全角分隔符
    t2 = parse_targets("电视,192.168.1.20/32,media\n复制,192.168.1.30/32|download")
    assert t2[0]["name"] == "电视" and t2[0]["role"] == "media"
    assert t2[1]["role"] == "download"
    # 重复 target 去重
    assert len(parse_targets("A=1.2.3.4|nas\nB=1.2.3.4|media")) == 1


def test_time_and_rules():
    assert time_to_minutes("07:30") == 450
    assert time_to_minutes("24:00") == 1440
    assert time_to_minutes("25:00") is None
    assert time_to_minutes("bad") is None
    rules = parse_rules("00:00-07:00=night\n07:00-18:00=balanced\n18:00-24:00=movie\ninvalid-line")
    assert len(rules) == 3
    assert [r["profile"] for r in rules] == ["night", "balanced", "movie"]


def test_in_window_cross_midnight():
    assert in_window(30, 23 * 60, 6 * 60) is True      # 00:30 落在 23:00-06:00
    assert in_window(23 * 60 + 30, 23 * 60, 6 * 60) is True
    assert in_window(12 * 60, 23 * 60, 6 * 60) is False
    assert in_window(10 * 60, 7 * 60, 18 * 60) is True
    assert in_window(18 * 60, 7 * 60, 18 * 60) is False  # 上界开区间


def test_match_profile_auto():
    rules = parse_rules("00:00-07:00=night\n07:00-18:00=balanced\n18:00-24:00=movie")
    assert match_profile(rules, DEFAULT_PROFILE, datetime.datetime(2026, 1, 1, 3, 0)) == "night"
    assert match_profile(rules, DEFAULT_PROFILE, datetime.datetime(2026, 1, 1, 12, 0)) == "balanced"
    assert match_profile(rules, DEFAULT_PROFILE, datetime.datetime(2026, 1, 1, 20, 0)) == "movie"
    assert match_profile([], DEFAULT_PROFILE, datetime.datetime(2026, 1, 1, 20, 0)) == DEFAULT_PROFILE
    # 重叠时后写优先
    overlap = parse_rules("00:00-24:00=balanced\n18:00-24:00=movie")
    assert match_profile(overlap, DEFAULT_PROFILE, datetime.datetime(2026, 1, 1, 20, 0)) == "movie"


def test_parse_bandwidth():
    assert parse_bandwidth("20M/60M") == (True, "20M/60M")
    assert parse_bandwidth("512k/2M") == (True, "512k/2M")
    assert parse_bandwidth("0/0") == (True, "0/0")
    assert parse_bandwidth("0") == (True, "0/0")
    assert parse_bandwidth("") == (False, "")
    assert parse_bandwidth("abc")[0] is False
    assert parse_bandwidth("20M")[0] is False


def test_profile_json_and_merge():
    custom = parse_profile_json(
        '{"movie":{"label":"观影·自定义","rules":{"download":{"max":"1M/3M","priority":8}}},'
        '"turbo":{"label":"极限","rules":{"nas":{"max":"0/0"}}}}'
    )
    assert "movie" in custom and "turbo" in custom
    assert custom["movie"]["rules"]["download"]["max"] == "1M/3M"
    merged = merge_profiles(custom)
    # 内置 movie 的 download 被覆盖，其它角色仍在
    assert merged["movie"]["rules"]["download"]["max"] == "1M/3M"
    assert merged["movie"]["rules"]["media"]["max"] == "0/0"
    assert "turbo" in merged
    # 非法 JSON 容错
    assert parse_profile_json("{not json") == {}
    assert parse_profile_json("") == {}
    keys = [p["key"] for p in available_profiles(merged)]
    assert "balanced" in keys and "turbo" in keys


# ---------------------------------------------------------------------------
# core：队列构建与差异
# ---------------------------------------------------------------------------

def test_build_queue_specs():
    targets = parse_targets(_TARGETS_TEXT)
    specs = build_queue_specs("movie", targets, "bqs")
    assert len(specs) == 4
    by_name = {s["name"]: s for s in specs}
    assert "bqs_" in list(by_name)[0]
    # movie 方案：下载受限、观影不限速
    dl = next(s for s in specs if s["role"] == "download")
    media = next(s for s in specs if s["role"] == "media")
    assert dl["max_limit"] == PROFILES["movie"]["rules"]["download"]["max"]
    assert media["max_limit"] == "0/0"
    assert 1 <= dl["priority"] <= 8
    assert all(s["target"] for s in specs)


def test_diff_queue_specs():
    targets = parse_targets(_TARGETS_TEXT)
    desired = build_queue_specs("balanced", targets, "bqs")
    # 现状：一条同配置、一条过期参数(需更新)、一条多余(需删除)、一条手工队列(不动)
    rows = [
        {".id": "*1", **{k: v for k, v in _spec_to_row(desired[0]).items()}},
        {".id": "*2", "name": desired[1]["name"], "target": desired[1]["target"],
         "max-limit": "1M/1M", "limit-at": "128k/128k", "priority": 8},
        {".id": "*3", "name": "bqs_旧设备", "target": "192.168.1.99/32", "max-limit": "2M/2M"},
        {".id": "*4", "name": "manual_queue", "target": "10.0.0.1/32", "max-limit": "100M/100M"},
    ]
    plan = diff_queue_specs(rows, desired, "bqs")
    assert len(plan["create"]) == 2                        # 电视、手机
    assert len(plan["update"]) == 1                        # NAS 参数不同
    assert plan["update"][0]["name"] == desired[1]["name"]
    assert len(plan["remove"]) == 1 and plan["remove"][0]["name"] == "bqs_旧设备"
    assert plan["has_change"] is True
    # 手工队列不在删除列表
    assert all("manual" not in r["name"] for r in plan["remove"])
    # 无变化时
    same_rows = [_spec_to_row(s) for s in desired]
    plan2 = diff_queue_specs(same_rows, desired, "bqs")
    assert plan2["has_change"] is False


def _spec_to_row(spec):
    return {"name": spec["name"], "target": spec["target"],
            "max-limit": spec["max_limit"], "limit-at": spec["limit_at"],
            "priority": spec["priority"], "disabled": False}


# ---------------------------------------------------------------------------
# 文本安全 / 格式化
# ---------------------------------------------------------------------------

def test_md_safe_and_log_line():
    assert md_safe("[abc][def]") == "［abc］［def］"
    assert "[" not in md_safe("x `code` *b* <tag> _u_")
    line = format_log_line("2026-01-01 00:00:00", "INFO", "方案 [movie] 已应用")
    assert "｜" in line and "[" not in line and "］" in line


def test_format_helpers():
    assert "暂无" in format_rates_text([], "bqs")
    rows = [{"name": "bqs_下载机", "rate": "1Mbps/2Mbps", "max-limit": "2M/5M", "bytes": "123"}]
    assert "bqs_下载机" in format_rates_text(rows, "bqs")
    assert "尚未" in format_fasttrack_text(None)
    assert "正常" in format_fasttrack_text({"has_fasttrack": False})
    assert "⚠" in format_fasttrack_text({"has_fasttrack": True, "rules": ["forward｜fasttrack-connection｜x"]})
    apply = {"ok": True, "profile": "movie", "created": [{"name": "bqs_a", "max_limit": "2M/5M", "target": "1.1.1.1"}],
             "updated": [], "removed": [], "errors": []}
    assert "movie" in format_apply_text(apply)


def test_validate_cron_and_mask():
    assert validate_cron("*/5 * * * *")[0] is True
    assert validate_cron("0 3 * * *")[0] is True
    assert validate_cron("bad")[0] is False
    assert validate_cron("70 * * * *")[0] is False
    assert "S3cret" not in mask_secrets("pass S3cretPass", ["S3cretPass"])


def test_normalize_base_url():
    assert normalize_base_url("192.168.1.1") == "https://192.168.1.1"          # 省略默认端口
    assert normalize_base_url("192.168.1.1", 8080, False) == "http://192.168.1.1:8080"
    assert normalize_base_url("https://ros.local/rest") == "https://ros.local"  # 忽略误带路径
    assert normalize_base_url("http://192.168.1.1:8080") == "http://192.168.1.1:8080"


# ---------------------------------------------------------------------------
# V3 契约
# ---------------------------------------------------------------------------

def test_contract_attributes():
    assert BandwidthQueueScheduler.plugin_version == "1.2.1"
    assert BandwidthQueueScheduler.plugin_name == PLUGIN_NAME
    assert BandwidthQueueScheduler.plugin_author == "BackspaceM"
    assert BandwidthQueueScheduler.plugin_config_prefix == "bandwidthqueuescheduler_"
    assert BandwidthQueueScheduler.plugin_icon.startswith("https://")
    assert hasattr(BandwidthQueueScheduler, "get_form")


def test_init_and_state():
    p = _fresh_plugin()
    assert p.get_state() is False
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "targets": _TARGETS_TEXT, "schedule_rules": "00:00-24:00=movie"})
    assert p.get_state() is True
    assert p._address == "192.168.1.1"
    assert len(p._targets) == 4
    assert len(p._rules) == 1
    assert p._prefix == "bqs"


def test_get_form_returns_tuple():
    p = _fresh_plugin()
    form, defaults = p.get_form()
    assert isinstance(form, list) and isinstance(defaults, dict)
    for key in ("enabled", "router_address", "queue_prefix", "targets",
                "default_profile", "schedule_rules", "cron", "dry_run"):
        assert key in defaults
    # 关键配置组件存在
    text = str(form)
    for model in ("targets", "schedule_rules", "custom_profiles", "router_address"):
        assert model in text


def test_get_service_trigger():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "cron": "*/5 * * * *", "router_address": "1.2.3.4"})
    services = p.get_service()
    assert len(services) == 1
    assert services[0]["trigger"] == ("cron", "*/5 * * * *")
    assert services[0]["func"] == p.start_run
    p2 = _fresh_plugin()
    assert p2.get_service() == []


def test_onlyonce_reset_flips_config():
    p = _fresh_plugin()
    # enabled=False 避免后台线程；复位逻辑与 enabled 无关
    p.init_plugin({"enabled": False, "router_address": "1.2.3.4", "onlyonce": True})
    assert _PluginBase.CONFIGS, "onlyonce 应触发 update_config 复位"
    assert _PluginBase.CONFIGS[-1].get("onlyonce") is False


def test_get_command():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4"})
    cmds = {c["cmd"] for c in p.get_command()}
    for c in ("/bqs_status", "/bqs_run", "/bqs_refresh", "/bqs_fasttrack", "/bqs_movie"):
        assert c in cmds


# ---------------------------------------------------------------------------
# 详情页按钮"可点"校验（本次重点）
# ---------------------------------------------------------------------------

def _walk(node, out):
    if isinstance(node, dict):
        if node.get("component") == "VBtn":
            out.append(node)
        for child in (node.get("content") or []):
            _walk(child, out)
    elif isinstance(node, list):
        for child in node:
            _walk(child, out)


def _page_buttons(p):
    buttons = []
    for card in p.get_page():
        _walk(card, buttons)
    return buttons


def test_get_page_renders_and_md_safe():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4", "targets": _TARGETS_TEXT})
    # 注入一条含方括号的日志，验证详情页转义（杜绝空药丸）
    p.save_data(_KEY_LOGS, [{"time": "2026-01-01 00:00:00", "level": "INFO",
                             "message": "方案 [movie] 已应用"}])
    page = p.get_page()
    assert isinstance(page, list) and page
    text = str(page)
    assert "应用·观影优先" in text
    assert "FastTrack 诊断" in text
    assert "[movie]" not in text          # 日志不再铺在详情页
    assert "运行日志" not in text


def test_every_page_button_has_live_api():
    """详情页每个按钮：① 无 disabled；② 绑定的 API 在 get_api 已注册；③ endpoint 可调用。"""
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "1.2.3.4",
                   "targets": _TARGETS_TEXT, "custom_profiles": ""})
    buttons = _page_buttons(p)
    assert buttons, "详情页应至少有操作按钮"

    api_map = {}
    for entry in p.get_api():
        api_map[f"plugin/BandwidthQueueScheduler{entry['path']}"] = entry["endpoint"]
        assert callable(entry["endpoint"]), f"{entry['path']} 的 endpoint 不可调用"

    for btn in buttons:
        props = btn.get("props") or {}
        label = props.get("text", "?")
        assert "disabled" not in props, f"按钮「{label}」含 disabled 属性（不可点）"
        assert props.get("disabled") is not True, f"按钮「{label}」被禁用"
        click = (btn.get("events") or {}).get("click") or {}
        api = str(click.get("api") or "").lstrip("/")
        assert api, f"按钮「{label}」未绑定 api"
        assert api in api_map, f"按钮「{label}」的 API {api} 未在 get_api 注册（点了没反应）"
        assert click.get("method"), f"按钮「{label}」未声明 method"

    # 方案切换按钮必须带 profile 参数，且 confirm=False（即刻生效）
    apply_btns = [b for b in buttons
                  if str(((b.get("events") or {}).get("click") or {}).get("api", "")).endswith("/apply_profile")]
    assert len(apply_btns) >= len(PROFILES), "每个方案都应有切换按钮"
    for b in apply_btns:
        click = b["events"]["click"]
        assert "profile" in (click.get("params") or {}), "方案按钮缺 profile 参数"
        assert click.get("confirm") is False, "方案切换不应强制二次确认"
    # 破坏性操作有确认
    clear = [b for b in buttons if str(((b.get("events") or {}).get("click") or {}).get("api", "")).endswith("/clear_queues")]
    assert clear and clear[0]["events"]["click"].get("confirm") is True


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------

def test_end_to_end_auto_schedule_and_notify():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "router_username": "admin", "router_password": "S3cretPass",
                   "targets": _TARGETS_TEXT, "queue_prefix": "bqs",
                   "default_profile": "balanced",
                   "schedule_rules": "00:00-24:00=movie",   # 任意时刻都选 movie
                   "auto_apply": True, "notify_mode": "change"})

    state, responder = _state_machine()
    AsyncRequestUtils.responder = responder

    # 首轮：按 movie 方案建 4 条队列
    r1 = asyncio.run(p._async_run("测试-首轮"))
    assert r1["ok"] is True, r1.get("message")
    assert r1["profile"] == "movie" and r1["auto"] is True
    assert len(r1["apply"]["created"]) == 4
    assert len(state["queues"]) == 4
    assert p.get_data(_KEY_PROFILE) == "movie"
    assert len(p.get_data(_KEY_RATES)) == 4
    # 下载受限、观影不限速
    dl = next(q for q in state["queues"] if "下载机" in q["name"])
    media = next(q for q in state["queues"] if "客厅电视" in q["name"])
    assert dl["max-limit"] == PROFILES["movie"]["rules"]["download"]["max"]
    assert media["max-limit"] == "0/0"

    # 手动切到 download 方案 -> 下载变不限速，观影被限
    r2 = asyncio.run(p._async_run("测试-切换", profile_override="download"))
    assert r2["ok"] is True and r2["profile"] == "download" and r2["auto"] is False
    assert len(r2["apply"]["updated"]) >= 2
    dl = next(q for q in state["queues"] if "下载机" in q["name"])
    assert dl["max-limit"] == "0/0"

    # 通知由 start_run 的 _notify_result 发出，显式触发验证脱敏
    _PluginBase.POSTED.clear()
    p._notify_result(r2, "测试-切换")
    assert _PluginBase.POSTED, "方案变更应触发通知"
    joined = str(_PluginBase.POSTED[-1])
    assert "S3cretPass" not in joined
    assert "download" in joined

    # 静默模式不发
    _PluginBase.POSTED.clear()
    p._notify_mode = "none"
    p._notify_result(r2, "测试-静默")
    assert not _PluginBase.POSTED


def test_end_to_end_remove_stale_queue():
    """设备列表缩减后，多余的旧队列应被删除，手工队列不受影响。"""
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "targets": "下载机=192.168.1.30/32|download", "queue_prefix": "bqs",
                   "default_profile": "balanced", "schedule_rules": ""})
    initial = [
        {".id": "*9", "name": "bqs_旧设备", "target": "192.168.1.99/32", "max-limit": "1M/1M"},
        {".id": "*10", "name": "manual", "target": "10.0.0.1/32", "max-limit": "100M/100M"},
    ]
    state, responder = _state_machine(initial_queues=initial)
    AsyncRequestUtils.responder = responder
    r = asyncio.run(p._async_run("测试-清理"))
    assert r["ok"] is True
    assert len(r["apply"]["removed"]) == 1 and r["apply"]["removed"][0]["name"] == "bqs_旧设备"
    names = [q["name"] for q in state["queues"]]
    assert "manual" in names and "bqs_旧设备" not in names


def test_dry_run_does_not_mutate():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "targets": _TARGETS_TEXT, "queue_prefix": "bqs",
                   "default_profile": "balanced", "schedule_rules": "", "dry_run": True})
    state, responder = _state_machine()
    AsyncRequestUtils.responder = responder
    r = asyncio.run(p._async_run("测试-试运行"))
    assert r["ok"] is True
    assert len(state["queues"]) == 0, "试运行不应创建真实队列"
    assert p.get_data(_KEY_PROFILE) is None, "试运行不应写当前方案"
    # 不应发 POST/PUT/DELETE
    mutating = [(m, u) for m, u, _ in AsyncRequestUtils.calls if m in ("POST", "PUT", "PATCH", "DELETE")]
    assert mutating == [], f"试运行出现写操作：{mutating}"


def test_fasttrack_diagnosis():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "targets": _TARGETS_TEXT, "queue_prefix": "bqs",
                   "default_profile": "balanced", "schedule_rules": ""})
    fast = [{"chain": "forward", "action": "fasttrack-connection", "disabled": "false", "comment": "fast"}]
    state, responder = _state_machine(fasttrack=fast)
    AsyncRequestUtils.responder = responder
    r = asyncio.run(p._async_run("测试-FastTrack"))
    assert r["ok"] is True
    assert r["fasttrack"]["has_fasttrack"] is True
    assert p.get_data(_KEY_FASTTRACK)["has_fasttrack"] is True
    assert "FastTrack" in r["message"]
    # 详情页应展示警告
    assert "FastTrack" in str(p.get_page())


def test_api_apply_profile_accepts_body():
    """带参按钮经 POST body 传 profile，endpoint 应正确接收并触发调度。"""
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "targets": _TARGETS_TEXT, "queue_prefix": "bqs", "schedule_rules": ""})
    state, responder = _state_machine()
    AsyncRequestUtils.responder = responder
    # 模拟前端 POST body
    resp = p.api_apply_profile({"profile": "night"})
    assert resp.success is True and resp.data["profile"] == "night"
    # 等待后台线程完成
    p.stop_service()
    assert p.get_data(_KEY_PROFILE) == "night"
    # 非法方案直接拒绝
    bad = p.api_apply_profile({"profile": "not_exist"})
    assert bad.success is False
    # 缺参拒绝
    assert p.api_apply_profile({}).success is False


def test_api_refresh_test_and_clear():
    p = _fresh_plugin()
    p.init_plugin({"enabled": True, "router_address": "192.168.1.1",
                   "targets": _TARGETS_TEXT, "queue_prefix": "bqs", "schedule_rules": ""})
    initial = [{".id": "*1", **_spec_to_row(build_queue_specs("balanced", parse_targets(_TARGETS_TEXT), "bqs")[0])}]
    state, responder = _state_machine(initial_queues=initial)
    AsyncRequestUtils.responder = responder

    assert p.api_test().success is True
    refreshed = p.api_refresh()
    assert refreshed.success is True and len(refreshed.data) == 1
    assert p.api_fasttrack().success is True
    cleared = p.api_clear_queues()
    assert cleared.success is True and len(state["queues"]) == 0
    assert p.api_stop().success is True
    assert p.api_status().success is True
    assert p.api_profiles().success is True
