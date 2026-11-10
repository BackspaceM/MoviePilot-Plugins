"""媒体库精准刷新插件离线用例（纯 assert，pytest 与桩 runner 均可执行）。"""

from __future__ import annotations

import time
from typing import Any

from app.plugins.medialibraryrefresh import MediaLibraryRefresh, clients, core
from app.sdk.network import AsyncRequestUtils, FakeResponse
from app.sdk.plugin import _PluginBase
from app.sdk.events import Event
from app.schemas.types import EventType

SERVERS = (
    "# 类型|名称|地址|密钥|路径映射|启用\n"
    "emby|Emby|http://emby:8096|KEY_ABCDEFG|/media::/volume1/media|1\n"
    "plex|Plex|http://plex:32400|TOKEN_123456|/media::/mnt/media|1\n"
)
XML = (
    '<?xml version="1.0"?><MediaContainer size="2">'
    '<Directory key="1" type="movie" title="电影"><Location id="1" path="/mnt/media/movies"/></Directory>'
    '<Directory key="2" type="show" title="剧集"><Location id="2" path="/mnt/media/tv"/></Directory>'
    '</MediaContainer>'
)


class _TransferInfo:
    file_list_new = ["/media/movies/A.mkv", "/media/movies/A.txt"]
    target_item = None


class _Target:
    path = "/media/tv/Show/S01E01.mkv"


class _TransferInfo2:
    target_item = _Target()


def _reset(*responses: Any) -> None:
    AsyncRequestUtils.responses = list(responses)
    AsyncRequestUtils.calls = []
    _PluginBase.POSTED = []


def _plugin(**config: Any) -> MediaLibraryRefresh:
    plugin = MediaLibraryRefresh()
    settings = {"enabled": True, "servers": SERVERS}
    settings.update(config)
    plugin.init_plugin(settings)
    return plugin


def _collect_models(nodes: Any) -> list:
    models = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        props = node.get("props") or {}
        if "model" in props:
            models.append(props["model"])
        models.extend(_collect_models(node.get("content") or []))
    return models


def _button_paths(page: Any) -> set:
    found = set()
    for node in page or []:
        if not isinstance(node, dict):
            continue
        events = node.get("events") or {}
        click = events.get("click") or {}
        api = str(click.get("api") or "")
        if api.startswith("plugin/MediaLibraryRefresh/"):
            found.add(api.replace("plugin/MediaLibraryRefresh", "", 1))
        found |= _button_paths(node.get("content") or [])
    return found


# ---------------------------------------------------------------------------
# 纯逻辑层
# ---------------------------------------------------------------------------

def test_normalize_path_and_join_url():
    assert core.normalize_path("C:\\Media\\A.mkv") == "C:/Media/A.mkv"
    assert core.normalize_path("/media//movies/") == "/media/movies"
    assert core.join_url("192.168.1.10:8096", "/emby/System/Info") == "http://192.168.1.10:8096/emby/System/Info"
    assert core.join_url("https://emby.example.com/emby/", "/emby/Library/Refresh") == "https://emby.example.com/emby/Library/Refresh"
    assert core.join_url("http://plex:32400", "/library/sections") == "http://plex:32400/library/sections"


def test_path_mapping_longest_prefix():
    mappings = core.parse_mappings("/media::/volume1/media;/media/tv::/volume2/tv\n# 注释\n坏行")
    assert len(mappings) == 2 and mappings[0][0] == "/media/tv"
    assert core.apply_path_mapping("/media/tv/S01E01.mkv", mappings) == "/volume2/tv/S01E01.mkv"
    assert core.apply_path_mapping("/media/movies/A.mkv", mappings) == "/volume1/media/movies/A.mkv"
    assert core.apply_path_mapping("/media2/A.mkv", mappings) == "/media2/A.mkv"


def test_parse_servers_valid_and_errors():
    text = SERVERS + "jellyfin|JF|http://jf:8096|JFKEY|/media::/data|0\nbad|坏行|http://x|k||1\n短行|只有两列\n"
    servers, errors = core.parse_servers(text)
    assert len(servers) == 3 and len(errors) == 2
    assert servers[0].type == "emby" and servers[1].is_plex and not servers[2].enabled
    assert servers[1].url == "http://plex:32400"
    assert servers[0].map_path("/media/movies/A.mkv") == "/volume1/media/movies/A.mkv"


def test_parse_servers_dedupes_names():
    servers, _ = core.parse_servers("emby|同名|http://a:8096|K1||1\nplex|同名|http://b:32400|K2||1\n")
    assert [s.name for s in servers] == ["同名", "同名-2"]
    assert servers[0].key != servers[1].key


def test_extract_event_paths_from_object():
    paths = core.extract_event_paths({"transferinfo": _TransferInfo(), "file_path": "/media/extra/B.mkv"})
    assert "/media/movies/A.mkv" in paths and "/media/extra/B.mkv" in paths
    assert core.extract_event_paths({"transferinfo": _TransferInfo2()}) == ["/media/tv/Show/S01E01.mkv"]


def test_extract_event_paths_from_dict():
    assert core.extract_event_paths({"transferinfo": {"file_list_new": ["/x/C.mkv"]}, "path": "/x/D.mkv"}) == ["/x/C.mkv", "/x/D.mkv"]
    assert core.extract_event_paths(None) == []


def test_filter_paths():
    exts = core.parse_extensions("mkv, .mp4 nfo")
    assert exts == (".mkv", ".mp4", ".nfo")
    assert core.filter_paths(["/media/A.mkv", "/media/A.txt", "/media/S01", "/skip/B.mkv"], exts, ["/skip"]) == ["/media/A.mkv", "/media/S01"]
    assert core.parse_extensions("") == tuple(core.DEFAULT_MEDIA_EXTS)


def test_dedupe_and_collapse():
    items = core.make_items(["/media/A.mkv", "/media/a.mkv", "/media/B.mkv"], core.CREATED, now=100.0)
    items += core.make_items(["/media/A.mkv"], core.CREATED, now=90.0)
    merged = core.dedupe_items(items)
    assert len(merged) == 2 and merged[0].ts == 90.0
    assert core.collapse_parents(merged) == ["/media"]
    assert core.split_batches(list(range(5)), 2) == [[0, 1], [2, 3], [4]]


def test_should_force_dispatch_and_debounce():
    assert not core.should_force_dispatch(0, 100, 105, 60, 20)[0]
    assert core.should_force_dispatch(20, 100, 105, 60, 20)[0]
    assert core.should_force_dispatch(3, 100, 165, 60, 20)[0]
    assert not core.should_force_dispatch(3, 100, 105, 60, 20)[0]
    assert core.debounce_delay(100, 105, 15, 60) == 15
    assert core.debounce_delay(100, 155, 15, 60) == 5


def test_retry_and_next_attempt():
    assert core.retry_delay(0, 5, 300) == 5 and core.retry_delay(3, 5, 300) == 40
    item = core.RefreshItem("/media/A.mkv", attempts=0, ts=100)
    assert core.next_attempt(item, 3).attempts == 1
    assert core.next_attempt(core.RefreshItem("/media/A.mkv", attempts=2), 3) is None
    assert core.RefreshItem.from_dict(item.to_dict()).path == "/media/A.mkv"
    assert core.RefreshItem.from_dict({"path": ""}) is None


def test_validate_cron():
    assert core.validate_cron("0 4 * * *") == (True, "0 4 * * *")
    assert core.validate_cron("*/15 2-6 * * 1,5")[0]
    assert core.validate_cron("0 0 1 ? *") == (True, "0 0 1 * *")
    assert not core.validate_cron("99 4 * * *")[0] and not core.validate_cron("0 4 *")[0]


def test_mask_secrets():
    masked = core.mask_secrets("http://u:p@host/emby?api_key=KEY_ABCDEFG token=TOKEN_123456", ["KEY_ABCDEFG", "TOKEN_123456"])
    assert "KEY_ABCDEFG" not in masked and "TOKEN_123456" not in masked and "u:p" not in masked
    assert "***" in masked


def test_format_items_and_now_text():
    items = core.make_items(["/media/A.mkv"], core.CREATED, now=100.0)
    assert core.format_items(items, 1).startswith("Created")
    assert core.format_items([]) == "-"
    assert len(core.now_text(0)) >= 10


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

def test_parse_plex_libraries_and_match():
    libs = clients.parse_plex_libraries(XML)
    assert len(libs) == 2 and libs[0]["locations"] == ["/mnt/media/movies"]
    assert clients.match_library("/mnt/media/tv/Show/S01E01.mkv", libs)["key"] == "2"
    assert clients.match_library("/mnt/media/tv2/A.mkv", libs) is None
    assert clients.parse_plex_libraries("<bad") == []


def test_emby_precise_request_payload():
    server, _ = core.parse_servers("emby|E|http://emby:8096|KEY_ABCDEFG|/media::/volume1/media|1")
    items = core.make_items(["/media/movies/A.mkv"], core.CREATED, now=100.0)
    import asyncio
    _reset(FakeResponse(204))
    result = asyncio.run(clients.build_client(server[0]).refresh(items))
    assert result.ok and result.level == "item"
    call = AsyncRequestUtils.calls[-1]
    assert call[1].endswith("/emby/Library/Media/Updated")
    assert call[2]["json"]["Updates"][0] == {"Path": "/volume1/media/movies/A.mkv", "UpdateType": "Created"}
    assert call[2]["headers"]["X-Emby-Token"] == "KEY_ABCDEFG"


def test_emby_fallback_parent_and_full():
    import asyncio
    server, _ = core.parse_servers("emby|E|http://emby:8096|K1234567||1")
    items = core.make_items(["/media/A.mkv"], core.CREATED, now=100.0)
    _reset(FakeResponse(500), FakeResponse(204))
    result = asyncio.run(clients.build_client(server[0]).refresh(items))
    assert result.ok and result.level == "parent"
    _reset(*[FakeResponse(500)] * 6)
    failed = asyncio.run(clients.build_client(server[0], fallback="full").refresh(items))
    assert not failed.ok and len(failed.details) >= 3
    _reset(FakeResponse(500))
    none = asyncio.run(clients.build_client(server[0], fallback="none").refresh(items))
    assert not none.ok and len(AsyncRequestUtils.calls) == 1


def test_plex_precise_and_all_fallback():
    import asyncio
    server, _ = core.parse_servers("plex|P|http://plex:32400|T1234567|/media::/mnt/media|1")
    items = core.make_items(["/media/tv/Show/S01E01.mkv", "/media/tv/Show/S01E02.mkv"], core.CREATED, now=100.0)
    _reset(FakeResponse(200, text=XML), FakeResponse(200))
    result = asyncio.run(clients.build_client(server[0]).refresh(items))
    assert result.ok and result.level == "item"
    refresh_calls = [c for c in AsyncRequestUtils.calls if "refresh" in c[1]]
    assert len(refresh_calls) == 1 and refresh_calls[0][2]["params"]["path"] == "/mnt/media/tv/Show"
    _reset(FakeResponse(200, text=XML), FakeResponse(200), FakeResponse(200))
    bad = core.make_items(["/other/A.mkv"], core.CREATED, now=100.0)
    fallback = asyncio.run(clients.build_client(server[0], fallback="full").refresh(bad))
    assert fallback.ok and fallback.level == "full"


def test_client_dry_run_and_exception():
    import asyncio
    server, _ = core.parse_servers("emby|E|http://emby:8096|K1234567||1")
    items = core.make_items(["/media/A.mkv"], core.CREATED, now=100.0)
    _reset()
    dry = asyncio.run(clients.build_client(server[0], dry_run=True).refresh(items))
    assert dry.ok and dry.level == "dry" and AsyncRequestUtils.calls == []
    _reset(OSError("connection refused"))
    failed = asyncio.run(clients.build_client(server[0], fallback="none").refresh(items))
    assert not failed.ok and "OSError" in " ".join(failed.details)


def test_server_result_serialization():
    result = clients.ServerResult(server="E", server_type="emby", ok=True, level="item", requested=2, succeeded=2)
    data = result.to_dict()
    assert data["level_label"] == "精准单文件" and data["ok"] and data["requested"] == 2


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

def test_plugin_metadata():
    assert MediaLibraryRefresh.plugin_version == "1.0.0"
    assert MediaLibraryRefresh.plugin_author == "BackspaceM"
    assert MediaLibraryRefresh.author_url == "https://github.com/BackspaceM"
    assert MediaLibraryRefresh.plugin_icon.endswith("medialibraryrefresh.png")
    assert MediaLibraryRefresh.plugin_config_prefix == "medialibraryrefresh_"


def test_get_form_defaults_match_models():
    form, defaults = MediaLibraryRefresh().get_form()
    models = set(_collect_models(form))
    assert models == set(defaults), (models - set(defaults), set(defaults) - models)
    assert len(models) >= 20


def test_get_api_paths_unique_and_buttons_registered():
    plugin = _plugin()
    apis = plugin.get_api()
    paths = [item["path"] for item in apis]
    assert len(paths) == len(set(paths))
    assert "/run" in paths and "/check" in paths and "/logs" in paths
    buttons = _button_paths(plugin.get_page())
    assert buttons and buttons <= set(paths), (buttons, set(paths))
    assert all(callable(item["endpoint"]) and item["auth"] == "bear" for item in apis)


def test_get_command_actions():
    commands = MediaLibraryRefresh.get_command()
    actions = {item["data"]["action"] for item in commands}
    assert actions == {"mlr_run", "mlr_check", "mlr_queue", "mlr_all", "mlr_stop"}
    assert all(item["cmd"].startswith("/") for item in commands)


def test_command_handler_routes_all_actions():
    plugin = _plugin()
    plugin.start_run = lambda **kwargs: True
    plugin._check_servers_sync = lambda: ["ok"]
    sent = []
    plugin._send_message = lambda title, text, **kwargs: sent.append((title, text, kwargs))
    for action in ("mlr_run", "mlr_check", "mlr_queue", "mlr_all", "mlr_stop"):
        plugin.handle_plugin_action(Event(EventType.PluginAction, {"action": action}))
    assert len(sent) == 5
    plugin.handle_plugin_action(Event(EventType.PluginAction, {
        "action": "mlr_check", "channel": "telegram", "userid": "123"}))
    assert sent[-1][2]["channel"] == "telegram" and sent[-1][2]["userid"] == "123"


def test_init_plugin_defaults_and_config_loading():
    plugin = MediaLibraryRefresh()
    plugin.init_plugin(None)
    assert not plugin.get_state() and plugin._servers == []
    plugin = _plugin(merge_window=7, max_wait=60, batch_size=50, fallback_level="full",
                     request_timeout=3, verify_ssl=True, dry_run=True)
    assert plugin._merge_window == 7 and plugin._max_wait == 60
    assert plugin._batch_size == 50 and plugin._fallback == "full"
    assert plugin._request_timeout == 3 and plugin._verify_ssl and plugin._dry_run
    assert len(plugin._servers) == 2 and plugin._server_errors == []


def test_invalid_cron_falls_back():
    plugin = _plugin(cron="not a cron")
    assert plugin._cron == "0 5 * * *"
    service = plugin.get_service()
    assert len(service) == 1 and service[0]["trigger"][1] == "0 5 * * *"
    assert _plugin(enabled=False).get_service() == []


def test_server_config_errors_are_reported():
    plugin = _plugin(servers="bad|X|http://x|k||1")
    assert plugin._server_errors and plugin._servers == []


def test_init_run_now_resets_switch():
    plugin = MediaLibraryRefresh()
    plugin.start_run = lambda **kwargs: True
    plugin.init_plugin({"enabled": True, "servers": SERVERS, "run_now": True})
    assert _PluginBase.CONFIGS[-1]["run_now"] is False


def test_event_enqueues_transfer_items():
    plugin = _plugin()
    _reset()
    plugin.on_transfer_event(Event(EventType.TransferComplete, {"transferinfo": _TransferInfo()}))
    queue = plugin._load_queue()
    assert len(queue) == 1 and queue[0].path == "/media/movies/A.mkv"
    assert queue[0].update_type == core.CREATED
    assert "/media/movies/A.txt" not in [item.path for item in queue]


def test_event_subtitle_switch_and_modified_type():
    event = Event(EventType.SubtitleTransferComplete, {"path": "/media/movies/A.srt"})
    off = _plugin(on_subtitle=False)
    off.on_transfer_event(event)
    assert off._load_queue() == []
    on = _plugin(on_subtitle=True)
    on.on_transfer_event(event)
    queue = on._load_queue()
    assert len(queue) == 1 and queue[0].update_type == core.MODIFIED


def test_event_delete_switch_and_deleted_type():
    event = Event(EventType.DownloadFileDeleted, {"path": "/media/movies/A.mkv"})
    off = _plugin(on_delete=False)
    off.on_transfer_event(event)
    assert off._load_queue() == []
    on = _plugin(on_delete=True)
    on.on_transfer_event(event)
    assert on._load_queue()[0].update_type == core.DELETED


def test_event_ignored_when_disabled_or_filtered():
    plugin = _plugin(enabled=False)
    plugin.on_transfer_event(Event(EventType.TransferComplete, {"transferinfo": _TransferInfo()}))
    assert plugin._load_queue() == []
    filtered = _plugin(exclude_keywords="/media/movies")
    filtered.on_transfer_event(Event(EventType.TransferComplete, {"transferinfo": _TransferInfo()}))
    assert filtered._load_queue() == []


def test_run_success_clears_queue_and_logs_details():
    plugin = _plugin()
    _reset(FakeResponse(204), FakeResponse(200, text=XML), FakeResponse(200))
    plugin._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    assert plugin.start_run(trigger="测试", prefer_thread=False)
    stats = plugin.get_data("stats")["last_result"]
    assert stats["servers_ok"] == 2 and stats["files"] == 1
    assert plugin._load_queue() == []
    assert any("Library/Media/Updated" in call[1] for call in AsyncRequestUtils.calls)
    assert any("library/sections/1/refresh" in call[1] for call in AsyncRequestUtils.calls)
    logs = plugin.get_data("run_logs")
    assert any("精准单文件" in item["message"] for item in logs)


def test_run_retry_on_failure():
    plugin = _plugin(fallback="none")
    _reset(FakeResponse(500), FakeResponse(500))
    plugin._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    plugin.start_run(trigger="测试", prefer_thread=False)
    raw = plugin.get_data("refresh_queue")
    assert len(raw) == 1 and raw[0]["attempts"] == 1 and raw[0]["ts"] > time.time()
    assert plugin.get_data("stats")["last_result"]["servers_ok"] == 0


def test_run_drops_items_after_max_retries():
    plugin = _plugin(fallback="none", max_retries=1)
    _reset(FakeResponse(500), FakeResponse(500))
    plugin._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    plugin.start_run(trigger="测试", prefer_thread=False)
    assert plugin.get_data("refresh_queue") == []
    assert plugin.get_data("stats")["last_result"]["dropped_items"] == 1


def test_run_without_servers_keeps_queue():
    plugin = _plugin(servers="")
    _reset()
    plugin._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    plugin.start_run(trigger="测试", prefer_thread=False)
    assert plugin.get_data("stats")["last_result"]["warning"] is True
    assert plugin._load_queue() and AsyncRequestUtils.calls == []


def test_empty_queue_no_requests_and_no_notify():
    plugin = _plugin()
    _reset()
    plugin.start_run(trigger="定时对账", prefer_thread=False)
    assert AsyncRequestUtils.calls == [] and _PluginBase.POSTED == []
    assert plugin.get_data("stats")["last_result"]["message"] == "队列为空，无需刷新"


def test_dry_run_does_not_touch_network():
    plugin = _plugin(dry_run=True)
    _reset()
    plugin._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    plugin.start_run(trigger="试运行", prefer_thread=False)
    assert AsyncRequestUtils.calls == []
    assert plugin.get_data("stats")["last_result"]["servers_ok"] == 2
    assert plugin._load_queue() == []


def test_check_servers_and_status_api():
    plugin = _plugin()
    _reset(FakeResponse(200, {"ServerName": "MyEmby", "Version": "4.8.0"}), FakeResponse(200, text=XML))
    response = plugin.api_check()
    assert response.success and len(response.data) == 2
    assert all(item["ok"] for item in response.data)
    status = plugin.api_status()
    assert status.success and status.data["queue"] == 0 and len(status.data["servers"]) == 2


def test_queue_and_logs_apis():
    plugin = _plugin()
    plugin._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    assert plugin.api_queue().message == "共 1 项"
    assert plugin.api_clear_queue().success and plugin._load_queue() == []
    assert plugin.api_logs().success
    assert plugin.api_clear_logs().success
    assert len(plugin.get_data("run_logs")) == 1


def test_logs_mask_tokens():
    plugin = _plugin()
    plugin._log("INFO", "请求密钥 KEY_ABCDEFG 与 TOKEN_123456")
    message = plugin.get_data("run_logs")[0]["message"]
    assert "KEY_ABCDEFG" not in message and "TOKEN_123456" not in message and "***" in message


def test_notify_modes():
    success = (FakeResponse(204), FakeResponse(200, text=XML), FakeResponse(200))
    closed = _plugin(notify_mode="关闭")
    _reset(*success)
    closed._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    closed.start_run(trigger="测试", prefer_thread=False)
    assert _PluginBase.POSTED == []

    only_fail = _plugin(notify_mode="仅异常")
    _reset(*success)
    only_fail._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    only_fail.start_run(trigger="测试", prefer_thread=False)
    assert _PluginBase.POSTED == []

    failed = _plugin(notify_mode="仅异常", fallback="none")
    _reset(FakeResponse(500), FakeResponse(500))
    failed._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    failed.start_run(trigger="测试", prefer_thread=False)
    assert _PluginBase.POSTED and "异常" in _PluginBase.POSTED[-1]["title"]


def test_full_refresh_records_full_results():
    plugin = _plugin()
    _reset(FakeResponse(204), FakeResponse(200, text=XML), FakeResponse(200), FakeResponse(200))
    plugin.start_run(trigger="全库", full_refresh=True, prefer_thread=False)
    result = plugin.get_data("stats")["last_result"]
    assert len(result["full_results"]) == 2 and result["servers_ok"] == 2


def test_scheduled_full_refresh_only_on_cron_trigger():
    plugin = _plugin(scheduled_full_refresh=True)
    _reset(FakeResponse(204), FakeResponse(200, text=XML), FakeResponse(200))
    plugin._save_queue(core.make_items(["/media/movies/A.mkv"], core.CREATED))
    plugin.start_run(trigger="手动", prefer_thread=False)
    assert plugin.get_data("stats")["last_result"]["full_results"] == []


def test_stop_signal_and_page_sections():
    plugin = _plugin()
    assert plugin.api_stop().success and plugin._stop_event.is_set()
    page_text = str(plugin.get_page())
    assert "媒体服务器" in page_text and "运行日志" in page_text and "待刷新队列" in page_text
    assert "立即刷新" in page_text and "检测服务器" in page_text


def test_html_block_escapes_user_input():
    block = MediaLibraryRefresh._html_block("标题", ["<script>alert(1)</script>", "/media/A.mkv"])
    html = block["content"][1]["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html and "/media/A.mkv" in html
