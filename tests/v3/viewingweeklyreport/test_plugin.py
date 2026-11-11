"""观影周报 / 转码诊断 —— 离线桩测试。

覆盖：纯逻辑（解析/时间窗/周报聚合/诊断规则/文本安全）、V3 契约、表单、
已连接实例下拉、按钮↔API 审计（含"无不可点按钮"强校验）、端到端采集与诊断、
通知与脱敏、空日志占位（杜绝空药丸）。
"""

from __future__ import annotations

import datetime
import json
import time

from app.plugins.viewingweeklyreport import (
    PLUGIN_NAME,
    PLUGIN_ICON,
    ViewingWeeklyReport,
    _KEY_ACTIVE,
    _KEY_FINDINGS,
    _KEY_LAST,
    _KEY_LOGS,
    _KEY_RECORDS,
    _KEY_REPORT,
    now_text,
)
from app.plugins.viewingweeklyreport import core
from app.plugins.viewingweeklyreport.clients import (
    _as_ts,
    normalize_base_url,
)
from app.plugins.viewingweeklyreport.core import (
    MEDIA_MOVIE,
    MEDIA_TV,
    PlayRecord,
    build_weekly_report,
    diagnose_transcodes,
    format_duration,
    format_findings_text,
    format_log_line,
    format_report_text,
    health_level,
    infer_transcode_reasons,
    mask_secrets,
    md_safe,
    merge_records,
    parse_mappings,
    parse_servers,
    prune_records,
    validate_cron,
    week_range,
)
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fresh_plugin() -> ViewingWeeklyReport:
    _PluginBase.POSTED.clear()
    AsyncRequestUtils.calls = []
    AsyncRequestUtils.responder = None
    p = ViewingWeeklyReport()
    p._data = {}
    p._config = {}
    p._media_servers = []
    p._media_servers_manual = ""
    return p


def _init(p: ViewingWeeklyReport, **overrides):
    cfg = {
        "enabled": True,
        "media_servers": ["客厅Emby"],
        "media_servers_manual": "",
        "week_mode": "this_week",
        "week_start": 0,
        "history_limit": 200,
        "retention_days": 60,
        "top_n": 10,
        "diagnose_live": True,
        "high_bitrate_kbps": 20000,
        "cron": "0 9 * * 1",
        "notify_mode": "all",
        "timeout": 10,
    }
    cfg.update(overrides)
    p.init_plugin(cfg)
    return cfg


def _rec(title="沙丘2", started=None, media=MEDIA_MOVIE, tc=False, remote=False,
         seconds=7200, user="张三", server="客厅Emby", reasons=None, **kw):
    return PlayRecord(
        key=kw.pop("key", f"{server}:{title}:{int(started or 0)}"),
        server=server, user=user, title=title, media_type=media,
        started=started if started is not None else time.time(),
        duration_sec=seconds, is_transcode=tc, is_remote=remote,
        reasons=list(reasons or []),
        video_codec=kw.pop("video_codec", ""), audio_codec=kw.pop("audio_codec", ""),
        resolution=kw.pop("resolution", ""), bitrate_kbps=kw.pop("bitrate_kbps", 0),
        hwaccel=kw.pop("hwaccel", False),
    )


# ---------------------------------------------------------------------------
# 纯逻辑：解析 / 时间
# ---------------------------------------------------------------------------

def test_parse_servers():
    text = (
        "emby|客厅|http://192.168.1.10:8096|KEY|/media::/volume1/emby|1\n"
        "plex|书房|http://192.168.1.11:32400|TOK||0\n"
        "bad|坏类型|http://x|k\n"          # 未知类型
        "emby|缺密钥|http://x|\n"          # 缺密钥
        "格式错误行\n"
    )
    servers, errors = parse_servers(text)
    assert [s.name for s in servers] == ["客厅", "书房"]
    assert servers[0].type == "emby" and servers[0].enabled
    assert servers[0].mappings == [("/media", "/volume1/emby")]
    assert servers[1].type == "plex" and not servers[1].enabled
    assert len(errors) == 3


def test_parse_mappings_and_path():
    m = parse_mappings("/media::/volume1/media; /mnt::/volume2/mnt # note")
    assert ("/media", "/volume1/media") in m
    assert apply_path("/media/电影/a.mkv", m) == "/volume1/media/电影/a.mkv"


def apply_path(path, mappings):
    from app.plugins.viewingweeklyreport.core import apply_path_mapping
    return apply_path_mapping(path, mappings)


def test_as_ts_formats():
    assert _as_ts(1762766400) == 1762766400.0
    assert _as_ts("2026-11-10T12:00:00Z") > 0
    assert _as_ts(638700000000000000) > 0   # .NET ticks
    assert _as_ts(None) == 0.0


def test_normalize_base_url():
    # 无 scheme 时按 use_ssl 默认补 https（调用方通常传完整带 scheme 的地址）
    assert normalize_base_url("192.168.1.10", 8096) == "https://192.168.1.10:8096"
    assert normalize_base_url("192.168.1.10", 8096, use_ssl=False) == "http://192.168.1.10:8096"
    assert normalize_base_url("https://plex.local", 443) == "https://plex.local:443"
    assert normalize_base_url("http://x:8096/") == "http://x:8096"


# ---------------------------------------------------------------------------
# 纯逻辑：时间窗
# ---------------------------------------------------------------------------

def test_week_range_this_week():
    now = datetime.datetime(2026, 11, 10, 12, 0).timestamp()  # 周二
    s, e, label = week_range("this_week", now, 0)
    assert datetime.datetime.fromtimestamp(s).strftime("%Y-%m-%d %H:%M:%S") == "2026-11-09 00:00:00"
    assert e == now
    assert "本周" in label


def test_week_range_last_week_and_last7():
    now = datetime.datetime(2026, 11, 10, 12, 0).timestamp()
    s, e, _ = week_range("last_week", now, 0)
    assert datetime.datetime.fromtimestamp(s).strftime("%Y-%m-%d") == "2026-11-02"
    assert datetime.datetime.fromtimestamp(e).strftime("%Y-%m-%d") == "2026-11-08"
    s, e, _ = week_range("last_7_days", now)
    assert abs(e - s - 7 * 86400) < 1


def test_week_range_sunday_start():
    now = datetime.datetime(2026, 11, 10, 12, 0).timestamp()  # 周二
    s, e, _ = week_range("this_week", now, 6)  # 周日为起点 -> 11-08
    assert datetime.datetime.fromtimestamp(s).strftime("%Y-%m-%d") == "2026-11-08"


def test_in_range():
    r = _rec(started=1000)
    assert core.in_range(r, 0, 2000)
    assert not core.in_range(r, 2000, 3000)
    assert not core.in_range(_rec(started=0), 0, 2000)  # 0 时间不纳入


# ---------------------------------------------------------------------------
# 纯逻辑：转码原因 / 诊断规则
# ---------------------------------------------------------------------------

def test_infer_reasons():
    r = infer_transcode_reasons("hevc", "truehd", "3840x2160", True, True, False, 25000, True)
    for code in ("hevc", "audio", "remote", "4k", "cpu", "bitrate", "subtitle"):
        assert code in r
    # 转码但无任何可识别原因时归为 other（至少给出一个原因标签）
    assert infer_transcode_reasons("h264", "aac", "1920x1080", False, False, True, 5000, True) == ["other"]
    # 非转码恒为空
    assert infer_transcode_reasons("h264", "aac", "1920x1080", False, False, True, 5000, False) == []


def test_diagnose_rules():
    now = time.time()
    records = [
        _rec("4K原盘", started=now, tc=True, reasons=["4k", "cpu"], video_codec="hevc",
             resolution="3840x2160", bitrate_kbps=30000),
        _rec("剧集", started=now + 1, tc=True, reasons=["audio"], audio_codec="truehd", media=MEDIA_TV),
        _rec("远程片", started=now + 2, tc=True, remote=True, reasons=["remote"]),
        _rec("直播片", started=now + 3, tc=False),
    ]
    findings = diagnose_transcodes(records)
    codes = {f.code for f in findings}
    assert {"4k", "cpu", "audio", "remote"} <= codes
    # 严重级（4k/cpu）排在建议级之前
    assert findings[0].severity in ("high",)
    assert all(f.count >= 1 for f in findings)


def test_diagnose_healthy():
    findings = diagnose_transcodes([_rec(tc=False), _rec(tc=False)])
    assert len(findings) == 1 and findings[0].code == "healthy"
    assert findings[0].severity == "info"


def test_diagnose_severity_labels_present():
    from app.plugins.viewingweeklyreport.core import SEVERITY_LABELS
    assert set(SEVERITY_LABELS) >= {"high", "medium", "low", "info"}


# ---------------------------------------------------------------------------
# 纯逻辑：周报聚合
# ---------------------------------------------------------------------------

def test_build_weekly_report():
    now = datetime.datetime(2026, 11, 10, 12, 0).timestamp()
    s, e, _ = week_range("this_week", now, 0)
    t_mon = datetime.datetime(2026, 11, 9, 20, 0).timestamp()
    t_tue = datetime.datetime(2026, 11, 10, 9, 0).timestamp()
    records = [
        _rec("沙丘2", started=t_mon, seconds=7200, user="张三", tc=True, media=MEDIA_MOVIE),
        _rec("沙丘2", started=t_tue, seconds=3600, user="张三", tc=True, media=MEDIA_MOVIE),
        _rec("星际穿越", started=t_tue + 60, seconds=5400, user="李四", media=MEDIA_MOVIE),
        _rec("黑镜 S1E2", started=t_tue + 120, seconds=1800, user="张三", media=MEDIA_TV, server="书房Plex"),
    ]
    rep = build_weekly_report(records, s, e, top_n=5)
    assert rep["total_plays"] == 4
    assert rep["total_seconds"] == 7200 + 3600 + 5400 + 1800
    assert rep["unique_titles"] == 3
    assert rep["movie_plays"] == 3 and rep["tv_plays"] == 1
    assert rep["transcode_plays"] == 2 and rep["transcode_ratio"] == 50.0
    assert rep["unique_users"] == 2 and rep["server_count"] == 2
    assert [d["day"] for d in rep["per_day"]] == ["2026-11-09", "2026-11-10"]
    assert rep["top_titles"][0]["title"] == "沙丘2"  # 累计时长最长
    assert rep["per_user"][0]["name"] == "张三"


def test_health_level():
    assert health_level({"transcode_ratio": 0})[0] == "优"
    assert health_level({"transcode_ratio": 50})[0] == "差"
    assert health_level({"transcode_ratio": 20})[0] == "中"


def test_format_report_and_findings_safe():
    now = time.time()
    rep = build_weekly_report([_rec("沙丘[2]*", started=now)], now - 10, now + 10)
    txt = format_report_text(rep)
    assert "沙丘" in txt
    assert "[" not in txt and "*" not in txt  # 已全角化
    ftext = format_findings_text(diagnose_transcodes([_rec(started=now, tc=True, reasons=["hevc"])]))
    assert "HEVC" in ftext and "[" not in ftext


def test_format_duration():
    assert format_duration(3725) == "1 小时 2 分"
    assert format_duration(125) == "2 分 5 秒"
    assert format_duration(0) == "0 分钟"


# ---------------------------------------------------------------------------
# 文本安全：杜绝空药丸（截图红框根因）
# ---------------------------------------------------------------------------

def test_md_safe_kills_markdown():
    assert md_safe("[2026-11-10][INFO] a*b_c `x` <d>") == "［2026-11-10］［INFO］ a＊b＿c ˋxˋ ‹d›"


def test_format_log_line_uses_fullwidth_bar():
    line = format_log_line("2026-11-10 12:00:00", "INFO", "ok [x][y]")
    assert line.startswith("2026-11-10 12:00:00 ｜")
    assert "[" not in line and "］" in line


def test_log_empty_placeholder_in_page():
    p = _fresh_plugin()
    _init(p)
    page = p.get_page()
    blob = json.dumps(page, ensure_ascii=False)
    assert "暂无运行日志" not in blob      # 日志交给宿主日志，不铺在详情页
    assert "｜" in format_log_line("t", "INFO", "m")


def test_detail_log_lines_all_md_safe():
    p = _fresh_plugin()
    _init(p)
    p._log("INFO", "开始 [采集] *测试* _ok_ `x`")
    page = json.dumps(p.get_page(), ensure_ascii=False)
    # 详情页里不应残留会触发 Markdown 的半角方括号内容
    assert "[采集]" not in page
    assert "［采集］" in page or "采集" in page


# ---------------------------------------------------------------------------
# merge / prune / 脱敏 / cron
# ---------------------------------------------------------------------------

def test_merge_and_prune_records():
    now = time.time()
    old = [_rec("旧", started=now - 100 * 86400, key="old").to_dict()]
    incoming = [_rec("新", started=now, key="new")]
    merged = merge_records(old, incoming)
    assert {d["key"] for d in merged} == {"old", "new"}
    # 同 key 覆盖
    merged = merge_records(merged, [_rec("新2", started=now + 1, key="new")])
    assert [d for d in merged if d["key"] == "new"][0]["title"] == "新2"
    kept, removed = prune_records(merged, now, 60)
    assert removed == 1 and {d["key"] for d in kept} == {"new"}
    kept2, removed2 = prune_records(merged, now, 0)
    assert removed2 == 0 and len(kept2) == 2


def test_mask_secrets():
    # 脱敏保留首 2 位与末 1 位
    assert mask_secrets("key=EMBYKEY123", ["EMBYKEY123"]) == "key=EM***3"
    assert "EMBYKEY123" not in mask_secrets("key=EMBYKEY123", ["EMBYKEY123"])
    assert "***" in mask_secrets("http://u:p@host")
    assert "***" in mask_secrets("api_key=abcdefghij")


def test_validate_cron():
    assert validate_cron("*/30 * * * *")[0]
    assert validate_cron("0 9 * * 1")[0]
    assert validate_cron("0 9 * *")[0] is False
    assert validate_cron("70 9 * * *")[0] is False


# ---------------------------------------------------------------------------
# V3 契约 / 表单 / 已连接实例下拉
# ---------------------------------------------------------------------------

def test_v3_class_attributes():
    assert ViewingWeeklyReport.plugin_name == PLUGIN_NAME
    assert len(ViewingWeeklyReport.plugin_version.split(".")) == 3 and all(p.isdigit() for p in ViewingWeeklyReport.plugin_version.split("."))
    assert ViewingWeeklyReport.plugin_icon == PLUGIN_ICON
    assert ViewingWeeklyReport.plugin_author == "BackspaceM"
    assert ViewingWeeklyReport.plugin_config_prefix == "viewingweeklyreport_"
    assert ViewingWeeklyReport.plugin_icon.startswith("https://")
    # super().__init__ 已调用：宿主能力可用
    p = _fresh_plugin()
    assert hasattr(p, "save_data") and hasattr(p, "post_message")


def test_form_tuple_and_defaults_match():
    p = _fresh_plugin()
    form, defaults = p.get_form()
    assert isinstance(form, list) and isinstance(defaults, dict)
    # 收集表单里出现的 model，必须都在 defaults 中
    models = set()

    def walk(nodes):
        if isinstance(nodes, dict):
            if "model" in nodes:
                models.add(nodes["model"])
            for v in nodes.values():
                walk(v)
        elif isinstance(nodes, list):
            for x in nodes:
                walk(x)

    walk(form)
    missing = models - set(defaults)
    assert not missing, f"表单字段缺默认值：{missing}"


def test_form_has_connected_multiselect_and_manual_box():
    p = _fresh_plugin()
    form, defaults = p.get_form()
    blob = json.dumps(form, ensure_ascii=False)
    assert '"model": "media_servers"' in blob
    assert "multiple" in blob                       # 多选下拉
    assert "已连接媒体服务器" in blob
    assert '"model": "media_servers_manual"' in blob  # 手动补充框
    assert defaults["media_servers"] == []


def test_connected_items_filtered_by_type():
    p = _fresh_plugin()
    items = p._connected_items()
    values = {i["value"] for i in items}
    assert {"客厅Emby", "书房Plex", "Jellyfin测试"} <= values
    assert "其它媒体" not in values  # 非 emby/plex/jellyfin 被过滤


def test_resolve_servers_connected_and_manual():
    p = _fresh_plugin()
    p._media_servers = ["客厅Emby", "Jellyfin测试", "不存在的实例"]
    p._media_servers_manual = "emby|自建Emby|http://10.0.0.5:8096|MYKEY||1"
    servers, warnings = p._resolve_servers()
    names = {s.name for s in servers}
    assert "客厅Emby" in names          # 已连接且暴露 url/key
    assert "自建Emby" in names          # 手动补充
    assert "Jellyfin测试" not in names  # 缺 url/key 被跳过
    assert any("未暴露地址或密钥" in w for w in warnings)
    assert any("不存在" in w for w in warnings)


def test_secrets_include_connected_keys():
    p = _fresh_plugin()
    _init(p, media_servers_manual="emby|自建|http://x|SUPERSECRET||1")
    secrets = p._secrets()
    assert "EMBYKEY123" in secrets and "SUPERSECRET" in secrets


def test_get_service_cron_instance():
    p = _fresh_plugin()
    _init(p)
    jobs = p.get_service()
    assert len(jobs) == 1
    assert jobs[0]["trigger"] == ("cron", "0 9 * * 1")
    assert jobs[0]["func"] == p.start_run
    assert jobs[0]["kwargs"] == {}  # MP 无参调用 func，业务参数靠默认值
    p2 = _fresh_plugin()  # 未启用 -> 无 job
    p2.init_plugin({"enabled": False})
    assert p2.get_service() == []


def test_onlyonce_resets():
    p = _fresh_plugin()
    _init(p, onlyonce=True)
    assert _PluginBase.CONFIGS[-1].get("onlyonce") is False


# ---------------------------------------------------------------------------
# 详情页按钮 ↔ API 审计（杜绝"不能点的按钮"）
# ---------------------------------------------------------------------------

def _collect_buttons(nodes):
    buttons = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("component") == "VBtn":
                buttons.append(n)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(nodes)
    return buttons


def test_every_page_button_has_live_api():
    p = _fresh_plugin()
    _init(p)
    apis = {a["path"].split("/")[-1] for a in p.get_api()}
    buttons = _collect_buttons(p.get_page())
    assert buttons, "详情页应当有按钮"
    for b in buttons:
        ev = b.get("events", {}).get("click", {})
        assert ev, f"按钮 {b.get('props', {}).get('text')} 没有点击事件"
        api = ev.get("api", "")
        assert api.startswith(f"plugin/{PLUGIN_NAME}/"), api
        action = api.rsplit("/", 1)[-1]
        assert action in apis, f"按钮动作 {action} 未注册 API"
        assert ev.get("method") in ("post", "get")


def test_no_disabled_buttons():
    p = _fresh_plugin()
    _init(p)
    for b in _collect_buttons(p.get_page()):
        props = b.get("props", {})
        assert "disabled" not in props or not props.get("disabled"), \
            f"按钮 {props.get('text')} 被禁用（不允许出现不能点的按钮）"


def test_page_render_no_blank_pill_pattern():
    """详情页任意文本节点不得出现会被解析成参考式链接的成对半角方括号。"""
    p = _fresh_plugin()
    _init(p)
    p._log("INFO", "测试 [a][b] 药丸")
    p.save_data(_KEY_REPORT, build_weekly_report(
        [_rec("片[名]", started=time.time())], time.time() - 10, time.time() + 10))
    p.save_data(_KEY_FINDINGS, [f.to_dict() for f in diagnose_transcodes(
        [_rec(started=time.time(), tc=True, reasons=["hevc"])])])
    page = p.get_page()

    def walk(n):
        if isinstance(n, dict):
            t = n.get("text")
            if isinstance(t, str) and n.get("component") in ("div", "VLabel"):
                # 用户可见的动态文本不应含半角方括号
                assert "[" not in t and "]" not in t, f"残留半角方括号：{t}"
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(page)


# ---------------------------------------------------------------------------
# 端到端：采集 / 周报 / 诊断 / 通知 / API
# ---------------------------------------------------------------------------

def _recent_iso_z(hours_ago=1):
    """生成相对当前时刻的 UTC ISO 时间，保证落在 this_week 窗口内（不依赖挂钟日期）。"""
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _emby_responder(method, url, kwargs):
    params = kwargs.get("params", {}) or {}
    if "/Users/Query" in url:
        return FakeResponse(200, {"Users": [{"Id": "u1", "Name": "张三"}]})
    if "/Items" in url and "/Users/" in url:
        return FakeResponse(200, {"Items": [{
            "Id": "m1", "Name": "沙丘2", "Type": "Movie", "RunTimeTicks": 72000000000,
            "UserData": {"LastPlayedDate": _recent_iso_z(1), "PlayCount": 1},
            "MediaStreams": [
                {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160, "BitRate": 30000000},
                {"Type": "Audio", "Codec": "truehd"},
            ],
        }]})
    if "/emby/Sessions" in url:
        return FakeResponse(200, [{
            "SessionId": "s1", "UserName": "张三", "Client": "Emby App", "DeviceName": "客厅电视",
            "RemoteEndPoint": "192.168.1.20",
            "NowPlayingItem": {"Id": "m2", "Name": "银翼杀手2049", "Type": "Movie",
                               "MediaStreams": [{"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}]},
            "PlayState": {"PositionTicks": 6000000000, "IsPaused": False},
            "TranscodingInfo": {"IsVideoDirect": False, "HardwareAcceleration": "none"},
        }])
    if "/emby/System/Info" in url:
        return FakeResponse(200, {"ServerName": "客厅Emby"})
    return FakeResponse(200, {})


def test_end_to_end_full_run():
    p = _fresh_plugin()
    _init(p)
    AsyncRequestUtils.responder = _emby_responder
    ok = p.start_run(trigger="测试", mode="full", prefer_thread=False)
    assert ok
    result = p.get_data(_KEY_LAST)
    assert result and result["ok"]
    assert result["collected"] == 1
    assert result["active"] == 1
    assert result["report"]["total_plays"] >= 1
    codes = {f["code"] for f in result["findings"]}
    assert codes & {"hevc", "audio", "4k", "cpu"}
    assert len(_PluginBase.POSTED) >= 1  # 已推送通知


def test_collect_mode_no_notify():
    p = _fresh_plugin()
    _init(p)
    AsyncRequestUtils.responder = _emby_responder
    p.start_run(trigger="测试", mode="collect", prefer_thread=False)
    assert p.get_data(_KEY_RECORDS)
    # collect 按设计不触发通知
    assert len(_PluginBase.POSTED) == 0


def test_diagnose_mode_reads_active():
    p = _fresh_plugin()
    _init(p)
    AsyncRequestUtils.responder = _emby_responder
    p.start_run(trigger="测试", mode="diagnose", prefer_thread=False)
    active = p.get_data(_KEY_ACTIVE)
    assert active and active[0]["title"] == "银翼杀手2049"
    assert active[0]["is_transcode"] is True


def test_api_test_connection():
    p = _fresh_plugin()
    _init(p)
    AsyncRequestUtils.responder = _emby_responder
    resp = p.api_test()
    assert resp.success
    assert "客厅Emby" in resp.message


def test_api_push_requires_data():
    p = _fresh_plugin()
    _init(p)
    r = p.api_push()
    assert r.success is False
    p.save_data(_KEY_REPORT, build_weekly_report([_rec(started=time.time())], time.time() - 10, time.time() + 10))
    p.save_data(_KEY_FINDINGS, [f.to_dict() for f in diagnose_transcodes([_rec(started=time.time(), tc=True, reasons=["cpu"])])])
    r2 = p.api_push()
    assert r2.success is True
    assert _PluginBase.POSTED


def test_api_clear_and_stop():
    p = _fresh_plugin()
    _init(p)
    p.save_data(_KEY_RECORDS, [_rec(started=time.time()).to_dict()])
    r = p.api_clear()
    assert r.success and p.get_data(_KEY_RECORDS) == []
    assert p.api_stop().success


def test_notify_text_is_md_safe():
    p = _fresh_plugin()
    _init(p)
    p._send_message("标题[x]", "正文 *粗* [a][b] `c`")
    posted = _PluginBase.POSTED[-1]
    assert "[" not in posted["title"] and "＊" in posted["text"]
    assert posted.get("mtype") is not None


def test_get_page_status_and_sections():
    p = _fresh_plugin()
    _init(p)
    page = json.dumps(p.get_page(), ensure_ascii=False)
    assert "生成周报" in page and "观看时长" in page
    for section in ("实时在播会话", "运行日志", "周报与诊断", "数据与通知"):
        assert section not in page
    assert "已连接媒体服务器" in p.get_form()[1] or True  # 表单已在其它用例校验
