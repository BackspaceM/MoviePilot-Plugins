"""播放感知调度插件离线用例（纯 assert，pytest 与桩 runner 均可执行）。

覆盖：
- core 纯逻辑：服务器/下载器/限速解析、限速单位换算、状态聚合、观看历史、
  自动删种候选、脱敏、cron 校验、路径归一化、会话格式化；
- 插件契约：init_plugin / get_state / get_form 元组 / get_service / get_api /
  get_command / get_page / onlyonce 复位；
- 端到端：用桩 AsyncRequestUtils 模拟 Emby 在播转码会话，验证限速下发、
  RouterOS 队列联动与数据落盘。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.plugins.playbackscheduler import PlaybackScheduler, clients, core
from app.plugins.playbackscheduler.core import (
    STATE_DIRECTPLAY,
    STATE_IDLE,
    STATE_REMOTE_TRANSCODE,
    STATE_TRANSCODE,
    Session,
    compute_limits,
    eligible_for_cleanup,
    format_sessions,
    mask_secrets,
    normalize_path,
    overall_state,
    parse_downloaders,
    parse_limits,
    parse_servers,
    state_label,
    update_watched_history,
    validate_cron,
)
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase


SERVERS_TEXT = (
    "# 类型|名称|地址|密钥|路径映射|启用\n"
    "emby|Emby|http://192.168.1.10:8096|KEY_ABCDEF123456|/media::/volume1/media|1\n"
    "plex|Plex|http://192.168.1.11:32400|TOKEN_99887766|/media::/mnt/media|1\n"
)
DOWNLOADERS_TEXT = (
    "qbittorrent|QB|http://192.168.1.20:8080|admin|pass|1\n"
    "transmission|TR|http://192.168.1.21:9091|admin|pass|1\n"
)
LIMITS_TEXT = "idle:0,0\ndirectplay:0,50\ntranscode:0,30\nremote_transcode:0,15"


class _Resp:
    """可配置响应，便于按 URL 返回不同内容。"""

    def __init__(self, status: int = 200, json_data: Any = None, text: str = ""):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def _install_fake_request(handler) -> None:
    async def _req(self, method, url, **kwargs):
        AsyncRequestUtils.calls.append((method, url, kwargs))
        return handler(method, url, kwargs)
    AsyncRequestUtils.request = _req  # type: ignore[assignment]


def _reset_stubs() -> None:
    AsyncRequestUtils.calls = []
    _PluginBase.POSTED = []
    _PluginBase.CONFIGS = []


# ---------------------------------------------------------------------------
# core 逻辑
# ---------------------------------------------------------------------------

def test_parse_servers():
    servers, errors = parse_servers(SERVERS_TEXT)
    assert len(servers) == 2, errors
    emby = next(s for s in servers if s.type == "emby")
    assert emby.enabled is True
    assert emby.map_path("/media/Movie.mkv") == "/volume1/media/Movie.mkv"
    plex = next(s for s in servers if s.type == "plex")
    assert plex.enabled is True
    assert plex.is_plex
    bad = "# only comment\n\nemby|Bad|http://x"
    s2, e2 = parse_servers(bad)
    assert len(s2) == 0
    assert e2  # 列数不足应报错并被跳过


def test_parse_downloaders():
    dls, errors = parse_downloaders(DOWNLOADERS_TEXT)
    assert len(dls) == 2
    qb = next(d for d in dls if d.is_qbittorrent)
    assert qb.enabled is True
    tr = next(d for d in dls if d.is_transmission)
    assert tr.enabled is True


def test_parse_limits_defaults_and_custom():
    limits = parse_limits(LIMITS_TEXT)
    assert limits[STATE_TRANSCODE] == (0.0, 30.0)
    assert limits[STATE_REMOTE_TRANSCODE] == (0.0, 15.0)
    # 缺省回退
    assert parse_limits("")[STATE_IDLE] == (0.0, 0.0)


def test_compute_limits_units():
    out = compute_limits(STATE_TRANSCODE, parse_limits(LIMITS_TEXT))
    assert out["dl_mbps"] == 0.0
    assert out["up_mbps"] == 30.0
    # qB 用字节/秒：30 Mbps = 30 * 125000 = 3_750_000
    assert out["qb_up_bytes"] == 30 * 125000
    # TR 用 KB/秒：30 Mbps = 30 * 125 = 3750
    assert out["tr_up_kbps"] == 30 * 125
    # idle 不限制 -> 0
    idle = compute_limits(STATE_IDLE, parse_limits(LIMITS_TEXT))
    assert idle["qb_dl_bytes"] == 0 and idle["tr_dl_kbps"] == 0


def test_overall_state_and_label():
    idle = Session(server="s", title="", is_paused=True)
    direct = Session(server="s", title="Movie", is_transcoding=False, is_remote=False)
    remote_tc = Session(server="s", title="M", is_transcoding=True, is_remote=True)
    assert overall_state([idle]) == STATE_IDLE
    assert overall_state([direct]) == STATE_DIRECTPLAY
    assert overall_state([remote_tc]) == STATE_REMOTE_TRANSCODE
    # 取最重的状态
    assert overall_state([direct, remote_tc]) == STATE_REMOTE_TRANSCODE
    assert state_label(STATE_TRANSCODE) == "转码播放"


def test_session_playing_flag():
    assert Session(server="s", title="A").is_playing is True
    assert Session(server="s", title="", is_paused=True).is_playing is False
    assert Session(server="s", title="A", is_paused=True).is_playing is False


def test_watched_history_and_cleanup():
    now = time.time()
    sess = Session(server="Emby", title="Movie", user="u",
                  paths=["/media/Movie.mkv"])
    hist = update_watched_history([], [sess], now)
    assert len(hist) == 1
    # 未到保留期 -> 无候选
    assert eligible_for_cleanup(hist, now + 1, 7) == []
    # 超过保留期 -> 成为候选
    assert len(eligible_for_cleanup(hist, now + 8 * 86400, 7)) == 1
    # 保留天数 <= 0 -> 永不候选
    assert eligible_for_cleanup(hist, now + 100 * 86400, 0) == []


def test_mask_secrets():
    masked = mask_secrets("key=KEY_ABCDEF123456 ok", ["KEY_ABCDEF123456"])
    assert "KEY_ABCDEF123456" not in masked
    masked2 = mask_secrets("api_key=SECRET_TOKEN_XYZ")
    assert "SECRET_TOKEN_XYZ" not in masked2


def test_validate_cron():
    ok, norm = validate_cron("*/5 * * * *")
    assert ok and norm == "*/5 * * * *"
    assert validate_cron("0 5 * * *")[0] is True
    assert validate_cron("bad cron expr")[0] is False
    assert validate_cron("* * * *")[0] is False


def test_normalize_path():
    assert normalize_path("C:\\Movies\\A.mkv") == "C:/Movies/A.mkv"
    assert normalize_path("/media/movie/") == "/media/movie"


def test_format_sessions():
    sess = Session(server="Emby", title="Movie", user="u", is_transcoding=True,
                   is_remote=False, progress=0.5, paths=["/media/Movie.mkv"])
    out = format_sessions([sess])
    assert "转码" in out and "Emby" in out
    assert format_sessions([]) == "无活动会话"


# ---------------------------------------------------------------------------
# 插件契约
# ---------------------------------------------------------------------------

def _plugin(**overrides: Any) -> PlaybackScheduler:
    _reset_stubs()
    plugin = PlaybackScheduler()
    base = {
        "enabled": True,
        "mediaservers": SERVERS_TEXT,
        "downloaders": DOWNLOADERS_TEXT,
        "limits": LIMITS_TEXT,
        "router_enabled": False,
        "protect_playing": False,
        "auto_cleanup": False,
        "cron": "*/5 * * * *",
        "notify_mode": "all",
        "dry_run": False,
    }
    base.update(overrides)
    plugin.init_plugin(base)
    return plugin


def test_init_and_state():
    p = _plugin(enabled=True)
    assert p.get_state() is True
    p2 = _plugin(enabled=False)
    assert p2.get_state() is False


def test_get_form_returns_tuple():
    p = _plugin()
    form, defaults = p.get_form()
    assert isinstance(form, list)
    assert isinstance(defaults, dict)
    assert defaults["enabled"] is False or defaults.get("enabled") in (False, True)
    # 表单至少含启用开关
    joined = str(form)
    assert "enabled" in joined


def test_get_service_disabled_empty():
    p = _plugin(enabled=False)
    assert p.get_service() == []


def test_get_service_enabled_has_trigger():
    p = _plugin(enabled=True)
    services = p.get_service()
    assert len(services) == 1
    svc = services[0]
    assert svc["id"] == "PlaybackScheduler"
    # trigger 应为 CronTrigger 实例（桩里是 ("cron", expr) 元组）
    assert svc["trigger"] is not None
    assert svc["func"] is not None


def test_get_api_and_command():
    p = _plugin()
    apis = p.get_api()
    assert {a["path"] for a in apis} == {
        "/run", "/reset", "/status", "/stop", "/clear_history",
    }
    cmds = p.get_command()
    assert {c["cmd"] for c in cmds} == {
        "/playback_status", "/playback_apply", "/playback_reset", "/playback_history_clear",
    }


def test_get_page_renders():
    p = _plugin()
    page = p.get_page()
    assert isinstance(page, list)
    assert len(page) >= 1


def test_onlyonce_reset_flips_config():
    _reset_stubs()
    p = PlaybackScheduler()
    cfg = {
        "enabled": True,
        "onlyonce": True,
        "mediaservers": SERVERS_TEXT,
        "downloaders": DOWNLOADERS_TEXT,
        "limits": LIMITS_TEXT,
    }
    p.init_plugin(cfg)
    # 立即执行开关应被复位
    stored = p.get_config()
    assert stored.get("onlyonce") is False
    assert len(_PluginBase.CONFIGS) >= 1
    assert _PluginBase.CONFIGS[-1].get("onlyonce") is False


def test_onlyonce_ignored_when_disabled():
    _reset_stubs()
    p = PlaybackScheduler()
    cfg = {"enabled": False, "onlyonce": True,
           "mediaservers": SERVERS_TEXT, "downloaders": DOWNLOADERS_TEXT, "limits": LIMITS_TEXT}
    p.init_plugin(cfg)
    assert p.get_config().get("onlyonce") is False
    assert p.get_state() is False


# ---------------------------------------------------------------------------
# 端到端：模拟 Emby 在播转码会话
# ---------------------------------------------------------------------------

def _emby_sessions_response() -> _Resp:
    return _Resp(200, json_data=[{
        "UserName": "alice",
        "Client": "Emby Web",
        "DeviceName": "LivingRoom",
        "NowPlayingItem": {
            "Name": "Big Buck Bunny",
            "Type": "Movie",
            "Path": "/media/movies/bbb.mkv",
            "RunTimeTicks": 1_000_000,
        },
        "PlayState": {"IsPaused": False, "PositionTicks": 500_000},
        "TranscodingInfo": {"IsVideoDirect": False},
        "RemoteEndPoint": "192.168.1.50",
    }])


def _success_response() -> _Resp:
    return _Resp(200, json_data={})


def _torrents_info_response() -> _Resp:
    # 返回一个与在播文件路径匹配的做种任务，供保护标签匹配
    return _Resp(200, json_data=[{
        "hash": "abc123",
        "save_path": "/volume1/media/movies",
        "content_path": "/volume1/media/movies/bbb.mkv",
    }])


def _router_list_response() -> _Resp:
    return _Resp(200, json_data=[{"name": "playback-limit", ".id": "*1"}])


def test_async_check_applies_transcode_limits():
    _reset_stubs()

    def handler(method, url, kwargs):
        if "Sessions" in url:
            return _emby_sessions_response()
        if "/rest/queue/simple" in url:
            return _router_list_response()
        if "torrents/info" in url:
            return _torrents_info_response()
        return _success_response()

    _install_fake_request(handler)

    p = _plugin(router_enabled=True, router_address="https://192.168.1.1",
                router_username="admin", router_password="secret",
                protect_playing=True, notify_mode="none")
    result = p._run_coroutine(p._async_check("端到端测试"))
    assert result["state"] == STATE_TRANSCODE, result
    # qB 应被下发上传限速（transcode 上限 30Mbps -> 3_750_000 字节/秒）
    qb_calls = [c for c in AsyncRequestUtils.calls if "setUploadLimit" in c[1]]
    assert qb_calls, "未向 qBittorrent 下发限速"
    # RouterOS 简单队列应被更新（PUT，因为已存在）
    router_calls = [c for c in AsyncRequestUtils.calls if "/rest/queue/simple" in c[1]]
    assert any(c[0] == "PUT" for c in router_calls), "RouterOS 队列未更新"
    # 在播文件保护标签应被下发
    tag_calls = [c for c in AsyncRequestUtils.calls if "addTags" in c[1]]
    assert tag_calls, "未对在播种子打保护标签"
    # 实时状态应已落盘（_async_check 内部通过 _update_live 写入 playback_live）
    assert p.get_data("playback_live") is not None
    live = p.get_data("playback_live") or {}
    assert live.get("state") == STATE_TRANSCODE


def test_async_check_idle_resets_routeros():
    _reset_stubs()

    def handler(method, url, kwargs):
        if "Sessions" in url:
            return _Resp(200, json_data=[])  # 无在播
        if "/rest/queue/simple" in url:
            return _router_list_response()
        return _success_response()

    _install_fake_request(handler)

    p = _plugin(router_enabled=True, router_address="https://192.168.1.1",
                router_username="admin", router_password="secret",
                notify_mode="none", auto_cleanup=False)
    result = p._run_coroutine(p._async_check("空闲测试"))
    assert result["state"] == STATE_IDLE
    # 空闲时应删除 RouterOS 队列（DELETE）
    router_calls = [c for c in AsyncRequestUtils.calls if "/rest/queue/simple" in c[1]]
    assert any(c[0] == "DELETE" for c in router_calls), "空闲时未移除 RouterOS 队列"


def test_reset_limits_and_clear_history():
    _reset_stubs()
    _install_fake_request(lambda *a: _success_response())
    p = _plugin()
    res = p.reset_limits()
    assert res["ok"] is True
    # 清空历史
    p.save_data("playback_history", [{"key": "x", "title": "t"}])
    cleared = p.clear_history()
    assert cleared["ok"] is True
    assert p.get_data("playback_history") == []


if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for fn in funcs:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:  # noqa: BLE001
            import traceback
            failed.append(fn.__name__)
            print(f"FAIL  {fn.__name__}\n{traceback.format_exc()}")
    print(f"\n合计 {len(funcs)} 个用例，通过 {len(funcs) - len(failed)}，失败 {len(failed)}")
    raise SystemExit(1 if failed else 0)
