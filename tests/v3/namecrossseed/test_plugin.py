"""名称辅种插件：站点解析、匹配判定与 V3 插件契约测试。

测试不访问网络、不依赖下载器实例：站点客户端只测纯解析与相似度函数，
插件主类用 ``object.__new__`` 绕过宿主插件管理器，仅设置被测字段。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List

import pytest

from app.plugins.namecrossseed import NameCrossSeed
from app.plugins.namecrossseed.site import (
    SiteTorrent,
    TorrentMeta,
    best_similarity,
    build_search_key,
    looks_logged_in,
    looks_rate_limited,
    parse_list_page,
    parse_size,
    parse_torrent,
    split_tokens,
)
from app.plugins.namecrossseed.rank import rank_candidates, size_diff_ratio

MOVIE = "Jojo.Rabbit.2019.1080p.MA.WEBRip.DD+5.1.x264-Porco"
OTHER = "The.Mandalorian.S02.1080p.WEB-DL.x264-Group"
SIZE = 15161507260


# ---------------------------------------------------------------------------
# 构造被测对象
# ---------------------------------------------------------------------------

def _plugin(**overrides: Any) -> NameCrossSeed:
    """构造只带被测字段的插件实例，避免触发宿主插件管理器。"""
    plugin = object.__new__(NameCrossSeed)
    plugin._enabled = True
    plugin._notify = False
    plugin._cron = "0 4 * * *"
    plugin._downloaders = []
    plugin._site_url = "https://hdwing.org"
    plugin._cookie = "hdchina=<REDACTED>; PHPSESSID=<REDACTED>"
    plugin._user_agent = "UA"
    plugin._max_requests = 10
    plugin._max_tasks = 5
    plugin._max_candidates = 3
    plugin._min_score = 0.6
    plugin._size_tolerance = 2.0
    plugin._match_mode = "strict"
    plugin._tag = "辅种"
    plugin._skip_tags = ["辅种"]
    plugin._skip_paths = []
    plugin._min_size_gb = 0.0
    plugin._cache_days = 7
    plugin._budget = 5
    plugin._search_cache = {}
    for key, value in overrides.items():
        setattr(plugin, key, value)
    return plugin


def _view(**overrides: Any) -> Any:
    """构造一个已完成任务视图。"""
    from app.plugins.namecrossseed import _TorrentView

    data = {
        "hash": "localhash", "name": MOVIE, "size": SIZE,
        "save_path": "/downloads/movie",
        "tags": [], "tracker": "http://tracker.other.org/announce", "added_on": 1,
    }
    data.update(overrides)
    return _TorrentView(**data)


class _Service:
    """模拟下载器服务对象。"""

    def __init__(self, kind: str = "qbittorrent", files: Any = None, existing: Any = None):
        self.type = kind
        self.instance = self
        self._files = files
        self._existing = existing or []

    def get_files(self, tid: str):
        return self._files

    def get_torrents(self, ids=None, status=None, tags=None):
        return ([{"hash": item} for item in self._existing], False)


def _bencode(value: Any) -> bytes:
    """最小 bencode 编码器，供构造测试用种子。"""
    if isinstance(value, bool):
        return b"i%de" % int(value)
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        return _bencode(value.encode())
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        body = b"".join(_bencode(key) + _bencode(item) for key, item in value.items())
        return b"d" + body + b"e"
    raise TypeError(type(value))


def _torrent_bytes(files: Dict[str, int], name: str = "example") -> bytes:
    """拼一个只含必要字段的 .torrent，返回内容与标准 info_hash 对应的字节流。"""
    info = {
        b"files": [
            {b"length": size, b"path": [part.encode() for part in path.split("/")]}
            for path, size in files.items()
        ],
        b"name": name.encode(),
    }
    return _bencode({b"info": info})


# ---------------------------------------------------------------------------
# 站点解析
# ---------------------------------------------------------------------------

def test_parse_size_handles_units() -> None:
    assert abs(parse_size("14.12 GB") - 14.12 * (1 << 30)) <= 1
    assert abs(parse_size("1,024.5MB") - 1024.5 * (1 << 20)) <= 1
    assert parse_size("800 KB") == 800 * 1024
    assert parse_size("") == 0
    assert parse_size("未知") == 0


def test_parse_list_page_extracts_rows() -> None:
    html = (
        "<table>"
        '<td class="t_pin"><a href="details.php?id=101">'
        '<h3><a href="details.php?id=101" title="%s">%s</a></h3>'
        "<h4>中字</h4><a href=\"download.php?hash=abc\">下载</a>"
        '<td class="t_size">14.12&nbsp;GB</td>'
        '<td class="t_torrents"><a href="x">8</a></td>'
        '<td class="t_leech"><a href="x">2</a></td>'
        '<td class="t_completed"><a href="x">30</a></td>'
        "</td>"
        '<td class="t_pin"><a href="details.php?id=102">'
        "<h3><a href=\"details.php?id=102\" title=\"Other\">Other</a></h3>"
        "</td>"
        "</table>"
    ) % (MOVIE, MOVIE)
    rows = parse_list_page(html)
    assert [row.tid for row in rows] == ["101", "102"]
    first = rows[0]
    assert first.title == MOVIE
    assert first.subtitle == "中字"
    assert first.size == parse_size("14.12 GB")
    assert first.seeders == 8
    assert first.leechers == 2
    assert first.completed == 30
    assert first.download_path == "download.php?hash=abc"
    assert rows[1].download_path == ""
    assert parse_list_page("") == []


def test_login_and_rate_limit_detection() -> None:
    logged = '<a href="userdetails.php?id=9">我</a><a href="logout.php">退出</a>'
    assert looks_logged_in(logged) is True
    assert looks_logged_in("<form action='takelogin.php'>") is False
    assert looks_logged_in("") is False

    limited, wait = looks_rate_limited("访问过于频繁，请 12 秒后再试")
    assert limited is True and wait == 12
    limited, wait = looks_rate_limited("请不要过于频繁")
    assert limited is True and wait >= 5
    assert looks_rate_limited("正常页面") == (False, 0)


def test_parse_torrent_hash_and_files() -> None:
    content = _torrent_bytes({"Movie/a.mkv": 1000, "Movie/b.srt": 24})
    meta = parse_torrent(content)
    assert meta is not None
    assert meta.total_size == 1024
    assert meta.files == {"Movie/a.mkv": 1000, "Movie/b.srt": 24}
    assert meta.file_count == 2
    assert meta.name == "example"
    expected = hashlib.sha1(_bencode({
        b"files": [
            {b"length": 1000, b"path": [b"Movie", b"a.mkv"]},
            {b"length": 24, b"path": [b"Movie", b"b.srt"]},
        ],
        b"name": b"example",
    })).hexdigest()
    assert meta.info_hash == expected
    assert parse_torrent(b"") is None
    assert parse_torrent(b"not-a-torrent") is None


# ---------------------------------------------------------------------------
# 名称归一与候选判定
# ---------------------------------------------------------------------------

def test_tokens_and_search_key() -> None:
    tokens = split_tokens(MOVIE + ".mkv")
    assert "jojo" in tokens and "rabbit" in tokens
    assert "1080p" in tokens and "x264" in tokens
    assert "porco" not in tokens
    assert "mkv" not in tokens

    primary, fallback = build_search_key(MOVIE + ".mkv")
    assert "jojo" in primary and "rabbit" in primary
    assert fallback and fallback.lower() != primary.lower()


def test_similarity_and_candidate_gate() -> None:
    assert best_similarity(MOVIE, MOVIE) > 0.95
    assert best_similarity(MOVIE, OTHER) < 0.3
    candidate = SiteTorrent(tid="1", title=MOVIE, size=SIZE)
    assert best_similarity(MOVIE, candidate.title) >= 0.6
    # 标题相近但体积差距过大时不应判定为同源
    wrong_size = SiteTorrent(tid="2", title=MOVIE, size=SIZE // 2)
    assert size_diff_ratio(SIZE, wrong_size.size) > 0.02


def test_rank_candidates_prefers_healthy_and_similar() -> None:
    exact = SiteTorrent(tid="1", title=MOVIE, size=SIZE, seeders=10)
    near = SiteTorrent(tid="2", title=MOVIE, size=int(SIZE * 1.01), seeders=50)
    ranked = rank_candidates(MOVIE, SIZE, [near, exact], min_score=0.6)
    assert ranked and ranked[0][2].tid == "1"
    assert rank_candidates(MOVIE, SIZE, [], min_score=0.6) == []


# ---------------------------------------------------------------------------
# 插件判定逻辑
# ---------------------------------------------------------------------------

def test_eligible_skips_cached_tagged_and_own_site() -> None:
    plugin = _plugin()
    now = time.time()
    assert plugin._eligible(_view(), {}, "qb") is True

    cached = {"qb|localhash": {"t": now, "r": "added"}}
    assert plugin._eligible(_view(), cached, "qb") is False

    expired = {"qb|localhash": {"t": now - 8 * 86400, "r": "failed"}}
    assert plugin._eligible(_view(), expired, "qb") is True

    assert plugin._eligible(_view(tags=["辅种"]), {}, "qb") is False
    assert plugin._eligible(_view(tracker="https://tracker.hdwing.org/announce"), {}, "qb") is False
    assert plugin._eligible(_view(save_path="/downloads/skip"), {"qb|localhash": {}}, "qb") is True


def test_eligible_respects_size_and_path_filters() -> None:
    plugin = _plugin(_min_size_gb=20.0, _skip_paths=["/downloads/skip"])
    assert plugin._eligible(_view(), {}, "qb") is False
    plugin = _plugin(_skip_paths=["/downloads/skip"])
    assert plugin._eligible(_view(save_path="/downloads/skip/movie"), {}, "qb") is False
    assert plugin._eligible(_view(save_path="/downloads/keep"), {}, "qb") is True


def test_files_match_requires_identical_layout() -> None:
    plugin = _plugin()
    meta = TorrentMeta(
        info_hash="sitehash",
        name=MOVIE,
        total_size=SIZE,
        files={f"{MOVIE}/a.mkv": 1000, f"{MOVIE}/b.srt": 24},
    )
    same = _Service(files=[{"name": f"{MOVIE}/a.mkv", "size": 1000}, {"name": f"{MOVIE}/b.srt", "size": 24}])
    assert plugin._files_match(same, _view(), meta) is True

    diff = _Service(files=[{"name": f"{MOVIE}/a.mkv", "size": 1000}, {"name": f"{MOVIE}/b.srt", "size": 25}])
    assert plugin._files_match(diff, _view(), meta) is False

    missing = _Service(files=[{"name": f"{MOVIE}/a.mkv", "size": 1000}])
    assert plugin._files_match(missing, _view(), meta) is False

    assert plugin._files_match(_Service(files=None), _view(), meta) is False
    assert plugin._files_match(same, _view(), TorrentMeta()) is False


def test_files_match_single_file_tolerates_relative_name() -> None:
    plugin = _plugin()
    meta = TorrentMeta(info_hash="h", name="a.mkv", total_size=1000, files={"a.mkv": 1000})
    service = _Service(files=[{"name": f"{MOVIE}/a.mkv", "size": 1000}])
    assert plugin._files_match(service, _view(), meta) is True


def test_exists_checks_downloader() -> None:
    plugin = _plugin()
    assert plugin._exists(_Service(existing=["abc"]), "abc") is True
    assert plugin._exists(_Service(existing=[]), "abc") is False


# ---------------------------------------------------------------------------
# V3 插件契约
# ---------------------------------------------------------------------------

def test_plugin_metadata_and_hooks() -> None:
    assert NameCrossSeed.plugin_version
    assert NameCrossSeed.plugin_config_prefix == "namecrossseed_"
    assert NameCrossSeed.get_state(_plugin()) is True
    assert NameCrossSeed.get_state(_plugin(_enabled=False)) is False

    for name in ("init_plugin", "get_state", "get_api", "get_form", "get_page",
                 "get_service", "stop_service", "get_command"):
        assert callable(getattr(NameCrossSeed, name)), name

    commands = NameCrossSeed.get_command()
    assert commands and commands[0]["cmd"] == "/name_seed"
    assert commands[0]["data"]["action"] == "namecrossseed_run"

    apis = {item["path"] for item in _plugin().get_api()}
    assert {"/run", "/check", "/clear_cache", "/status"} <= apis


def test_service_registration() -> None:
    assert _plugin(_enabled=False).get_service() == []
    services = _plugin().get_service()
    assert len(services) == 1
    service = services[0]
    assert service["id"] == "NameCrossSeed"
    assert service["func"] is not None
    assert "trigger" in service and "kwargs" in service and "func_kwargs" in service

    broken = _plugin(_cron="not-a-cron").get_service()
    assert broken and broken[0]["trigger"] is not None


def test_get_form_and_page_render() -> None:
    plugin = _plugin()
    form, default = plugin.get_form()
    assert isinstance(form, list) and form
    assert "cookie" in default and "cron" in default and "downloaders" in default

    plugin.get_data = lambda key=None: {  # type: ignore[method-assign]
        "stats": {"last_run": 1700000000, "last_scanned": 3, "last_matched": 2,
                  "last_added": 1, "last_failed": 0, "last_message": "完成"},
        "history": [],
        "cache": {},
    }.get(key)
    page = plugin.get_page()
    assert isinstance(page, list) and page


# ---------------------------------------------------------------------------
# 全量扫描 / 实时日志 / 精简设置（v1.2.0）
# ---------------------------------------------------------------------------

def _walk_models(node: Any, out: List[str]) -> None:
    if isinstance(node, dict):
        props = node.get("props") or {}
        if "model" in props:
            out.append(props["model"])
        for value in node.values():
            _walk_models(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_models(value, out)


def _scan_plugin(count: int = 60):
    """构造一个不联网、不碰下载器的插件，模拟 count 个已完成任务。"""
    import threading
    import types

    from app.plugins.namecrossseed import logview

    plugin = _plugin()
    store: Dict[str, Any] = {}
    plugin.get_data = lambda key=None, **kw: store.get(key)  # type: ignore[method-assign]
    plugin.save_data = lambda key, value: store.__setitem__(key, value)  # type: ignore[method-assign]
    logview.clear(plugin)

    plugin._stop_event = threading.Event()
    plugin._budget = 10000
    plugin._max_requests = 10000
    plugin._max_tasks = 0
    plugin._max_candidates = 3
    plugin._min_score = 0.6
    plugin._size_tolerance = 2.0
    plugin._match_mode = "strict"
    plugin._skip_tags = []
    plugin._skip_paths = []
    plugin._min_size_gb = 0.0
    plugin._cache_days = 7
    plugin._site_url = "https://hdwing.org"
    plugin._cookie = "test-cookie"
    plugin._notify = False
    plugin._search_cache = {}
    plugin._tag = "辅种"
    plugin._skip_checking = True
    plugin._add_paused = False
    plugin._interval = 6.0
    plugin._user_agent = "UA"
    plugin._downloaders = []
    plugin._name_rules = ""

    torrents = [
        {"hash": f"h{i}", "name": f"Some.Movie.{i}.2020.1080p.WEB-DL",
         "size": 1_000_000 + i, "save_path": "/downloads", "tags": [],
         "tracker": "http://tracker.other.org/announce", "added_on": i}
        for i in range(count)
    ]
    client = types.SimpleNamespace(check_login=lambda: (True, "ok"))
    plugin._services = lambda: {"qb.main": object()}  # type: ignore[method-assign]
    plugin._completed_torrents = lambda service: torrents  # type: ignore[method-assign]
    plugin._build_client = lambda: client  # type: ignore[method-assign]
    plugin._handle = lambda d, s, v, c: ("nomatch", "站点无搜索结果", "")  # type: ignore[method-assign]
    return plugin, store


def test_display_name_and_simple_form() -> None:
    assert NameCrossSeed.plugin_name == "名称辅种"
    form, default = _plugin().get_form()
    models: List[str] = []
    _walk_models(form, models)
    assert set(models) == {"enabled", "notify", "cron", "max_tasks", "interval",
                           "max_requests", "downloaders", "site_url", "user_agent",
                           "cookie", "name_rules"}
    assert len(models) <= 11, "设置项又变多了"
    assert default["name_rules"] == "", "名称清洗规则默认留空即用内置规则"
    assert default["max_tasks"] == 0, "默认必须不限数量，扫全下载器"


def test_api_has_log_and_page_shows_log_and_rows() -> None:
    assert "/log" in {item["path"] for item in _plugin().get_api()}
    plugin, store = _scan_plugin(3)
    store["stats"] = {"last_run_text": "刚刚", "last_scanned": 3,
                      "last_eligible": 3, "last_processed": 3}
    page = plugin.get_page()
    html = "".join(str(x.get("html") or "") for x in page)
    # 默认只给成功 / 失败两栏，完整日志与全量名录都折叠
    assert 'id="cs-ok"' in html and 'id="cs-bad"' in html and "cs-cols" in html
    assert "完整运行日志" in html and "cs-log" in html
    assert "本轮扫描名录" in html and '<details class="cs-fold">' in html
    assert "<details open" not in html
    assert "__csPoll" in html and "NameCrossSeed/log" in html


def test_log_emit_persists_every_line() -> None:
    from app.plugins.namecrossseed import logview

    plugin, store = _scan_plugin(0)
    logview.emit(plugin, "第一条")
    logview.emit(plugin, "第二条")
    text = logview.log_text(plugin)
    assert "第一条" in text and "第二条" in text
    assert store["log"][-1].endswith("第二条")
    assert "下载器种子 7 个" in logview.progress({"last_scanned": 7}, False)
    assert "&lt;script&gt;" in logview.render(
        {}, "<script>", [{"name": "<b>x</b>", "state": "added",
                          "message": "ok", "downloader": "qb", "size_text": "1 GB"}]
    )


def test_api_log_returns_text_and_progress() -> None:
    from app.plugins.namecrossseed import logview

    plugin, store = _scan_plugin(0)
    logview.emit(plugin, "开始跑")
    store["stats"] = {"last_run_text": "刚刚", "last_scanned": 12,
                      "last_eligible": 10, "last_processed": 8}
    response = plugin.api_log()
    assert response.success
    assert "开始跑" in response.data["text"]
    assert "下载器种子 12 个" in response.data["progress"]


def test_full_scan_has_no_truncation() -> None:
    plugin, store = _scan_plugin(60)
    plugin._run_once("手动")
    rows = store["scan"]
    stats = store["stats"]
    assert len(rows) == 60, "名录必须全量，不能只留前几个"
    assert stats["last_scanned"] == 60
    assert stats["last_eligible"] == 60
    assert stats["last_processed"] == 60
    assert stats["last_groups"] == 60
    assert all(r["state"] == "nomatch" for r in rows)
    assert "本轮结束：下载器 60 个种子" in "\n".join(store["log"])
    assert "搜索词" in "\n".join(store["log"])


def test_limit_marks_remaining_as_pending_but_still_lists_all() -> None:
    plugin, store = _scan_plugin(10)
    plugin._max_tasks = 3
    plugin._run_once("手动")
    rows = store["scan"]
    assert len(rows) == 10
    assert sum(1 for r in rows if r["state"] == "pending") == 7
    assert store["stats"]["last_processed"] == 3
    assert store["stats"]["last_scanned"] == 10
    assert "达到本轮处理上限" in store["stats"]["last_message"]


def test_cached_items_are_still_listed() -> None:
    plugin, store = _scan_plugin(5)
    store["cache"] = {"qb.main|h0": {"r": "added", "t": time.time()}}
    plugin._run_once("手动")
    rows = store["scan"]
    assert len(rows) == 5
    assert sum(1 for r in rows if r["state"] == "cached") == 1
    assert store["stats"]["last_scanned"] == 5
    assert store["stats"]["last_eligible"] == 4


def test_budget_message_tells_how_to_raise_it() -> None:
    """预算用尽不再是干巴巴四个字，要带上限数值和去哪调大。"""
    plugin, store = _scan_plugin(6)
    plugin._max_requests = 2
    plugin._handle = lambda d, s, v, c: (  # type: ignore[method-assign]
        setattr(plugin, "_budget", plugin._budget - 1) or ("nomatch", "无结果", "")
    )
    plugin._run_once("手动")
    message = store["stats"]["last_message"]
    assert "每轮请求上限" in message
    assert "下轮" in message


def test_summarize_ok_column_lists_added_only() -> None:
    """成功栏只放真正添加了的；站内已有单独计数，不冒充成功。"""
    from app.plugins.namecrossseed import logview

    rows = [
        {"name": "种A", "state": "added", "message": "添加成功"},
        {"name": "种B", "state": "exists", "message": "组内已有该站点的种子"},
        {"name": "种C", "state": "exists", "message": ""},
        {"name": "种D", "state": "failed", "message": "下载失败"},
        {"name": "种E", "state": "nomatch", "message": ""},
    ]
    summary = logview.summarize(rows)
    assert summary["ok_total"] == 1
    assert "种A" in summary["ok"][0]
    assert summary["exists_total"] == 2
    assert summary["bad_total"] == 1
    joined = "\n".join(summary["ok"])
    assert "种B" not in joined and "种C" not in joined


def test_interval_and_request_budget_settings_parse() -> None:
    """新增的搜索间隔与每轮请求上限要从配置读进来，间隔最小 5 秒。"""
    plugin = NameCrossSeed()
    plugin.init_plugin({
        "enabled": True, "interval": "3", "max_requests": "80",
    })
    assert plugin._interval == 5.0, "低于 5 秒要顶到 5"
    assert plugin._max_requests == 80
    form, default = plugin.get_form()
    assert default["interval"] == 6
    assert default["max_requests"] == 300
    models = []
    _walk_models(form, models)
    assert {"interval", "max_requests"} <= set(models)


def test_budget_exhaustion_is_visible_in_list() -> None:
    plugin, store = _scan_plugin(6)
    plugin._max_requests = 2
    plugin._handle = lambda d, s, v, c: (  # type: ignore[method-assign]
        setattr(plugin, "_budget", plugin._budget - 1) or ("nomatch", "无结果", "")
    )
    plugin._run_once("手动")
    rows = store["scan"]
    assert len(rows) == 6
    assert any(r["state"] == "pending" for r in rows)
    assert "请求预算用尽" in store["stats"]["last_message"]


def test_large_size_gap_still_downloads_candidate() -> None:
    """整季合集 / 单集体积差几十倍时，不能靠体积砍掉候选，必须走文件清单定案。

    同时回归 `self._download` 那个笔误：真实入口是站点客户端的 download()。
    """
    import threading
    import types

    from app.plugins.namecrossseed import logview

    plugin, store = _scan_plugin(0)
    logview.clear(plugin)
    plugin._stop_event = threading.Event()
    plugin._budget = 10
    plugin._search_cache = {}
    plugin._known_hashes = set()
    plugin._min_score = 0.0
    plugin._max_candidates = 3
    content = _torrent_bytes({"Movie/a.mkv": 1000})
    candidate = SiteTorrent(tid="1", title=MOVIE, size=SIZE // 200, seeders=5,
                            download_path="download.php?hash=abc")
    plugin._search = lambda client, keys: [candidate]  # type: ignore[method-assign]
    downloads: List[str] = []
    client = types.SimpleNamespace(
        download=lambda torrent: (downloads.append(torrent.title) or content)
    )
    plugin._judge = lambda service, view, meta: "files"  # type: ignore[method-assign]
    plugin._exists = staticmethod(lambda service, info_hash: False)  # type: ignore[assignment]
    plugin._add = lambda service, view, data: (True, "已添加")  # type: ignore[method-assign]
    del plugin._handle  # 用回真实实现
    view = _view()
    state, message, title = plugin._handle("qb", None, view, client)
    assert downloads == [MOVIE], "体积差不能阻止下载候选种子"
    assert state == "added", message
    assert title == MOVIE
    assert size_diff_ratio(SIZE, SIZE // 200) <= 1.0, "体积差必须收敛到 100% 以内"


def test_site_hash_already_in_downloader_is_not_added_again() -> None:
    """下载器里已经有这个站点种子时，整组跳过，不再重复插入。"""
    import threading
    import types

    plugin, store = _scan_plugin(0)
    plugin._stop_event = threading.Event()
    plugin._budget = 10
    plugin._search_cache = {}
    plugin._min_score = 0.0
    content = _torrent_bytes({"Movie/a.mkv": 1000})
    info_hash = parse_torrent(content).info_hash
    plugin._known_hashes = {info_hash}
    candidate = SiteTorrent(tid="1", title=MOVIE, size=SIZE, seeders=5,
                            download_path="download.php?hash=abc")
    plugin._search = lambda client, keys: [candidate]  # type: ignore[method-assign]
    client = types.SimpleNamespace(download=lambda torrent: content)
    plugin._add = lambda *a, **kw: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("已存在的种子不应再添加")
    )
    del plugin._handle
    state, message, _ = plugin._handle("qb", None, _view(), client)
    assert state == "exists", message
    assert "已在下载器" in message



# ---------------------------------------------------------------------------
# 名称清洗正则
# ---------------------------------------------------------------------------

def test_builtin_name_rules_normalize_dots_and_group_suffix() -> None:
    from app.plugins.namecrossseed import grouping

    name = "武神主宰.The.God.of.War.Dominates.S01.2020.2160p.WEB-DL.H265.AAC-ADWeb"
    out = grouping.clean_name(name)
    assert "." not in out, "内置规则要把点转成空格"
    assert "-ADWeb" not in out, "内置规则要去掉发布组后缀"
    assert "The God of War Dominates" in out
    keys = grouping.search_keys(name)
    assert keys and all("." not in key for key in keys)


def test_custom_regex_removes_chinese_prefix() -> None:
    from app.plugins.namecrossseed import grouping

    rules = (
        r"^[\u4e00-\u9fff]+[.\s_-]+ => <empty>" + "\n"
        + r"[._]+ => <space>"
    )
    name = "武神主宰.The.God.of.War.Dominates.S01.2020.2160p.WEB-DL"
    out = grouping.clean_name(name, rules)
    assert "武神主宰" not in out
    assert out.startswith("The")
    keys = grouping.search_keys(name, rules)
    assert keys and all("武神主宰" not in key for key in keys)
    assert grouping.clean_name(name, "") != out, "填了规则必须覆盖内置规则"


def test_invalid_regex_is_skipped_without_crash() -> None:
    from app.plugins.namecrossseed import grouping

    rules = "[ => <empty>\n[._]+ => <space>"
    assert grouping.invalid_rules(rules) == ["["]
    assert grouping.clean_name("A.B.C", rules) == "A B C"


def test_custom_rule_with_backreference() -> None:
    from app.plugins.namecrossseed import grouping

    rules = r"^\[([^]]+)\]\.? => \1<space>"
    assert grouping.clean_name("[PT]Some.Movie.2020", rules).startswith("PT Some")


def test_mixed_chinese_name_also_gets_latin_search_key() -> None:
    from app.plugins.namecrossseed import grouping

    keys = grouping.search_keys(
        "武神主宰.The.God.of.War.Dominates.S01.2020.2160p.WEB-DL"
    )
    assert any("武神主宰" not in key and "god" in key for key in keys), (
        "站点只有英文标题时，要能自动给出纯英文搜索词"
    )


def test_grouping_merges_same_content_reseeds() -> None:
    from app.plugins.namecrossseed import grouping

    name = "Anime.Show.S01.2023.2160p.WEB-DL"
    keys = {grouping.group_key(name, 20_000_000_000, "", f"h{i}") for i in range(5)}
    assert len(keys) == 1, "同内容不同 hash 的辅种必须归为一组"
    other = grouping.group_key(name, 30_000_000_000, "", "x")
    assert other not in keys, "同名不同体积是不同版本，不能误合并"
    zero = {grouping.group_key(name, 0, "", f"z{i}") for i in range(3)}
    assert len(zero) == 3, "体积取不到时不能只按标题误合并"


# ---------------------------------------------------------------------------
# 两阶段扫描：去重、异常隔离、不重复插入
# ---------------------------------------------------------------------------

def _reseed_torrents(count: int, name: str, size: int, tracker: str =
                     "http://tracker.other.org/announce") -> List[Dict[str, Any]]:
    return [
        {"hash": f"h{i}", "name": name, "size": size, "save_path": "/downloads",
         "tags": [], "tracker": tracker, "added_on": i}
        for i in range(count)
    ]


def test_duplicate_reseeds_are_grouped_and_searched_once() -> None:
    plugin, store = _scan_plugin(0)
    plugin._completed_torrents = lambda service: _reseed_torrents(  # type: ignore[method-assign]
        4, "Anime.Show.S01.2023.2160p.WEB-DL", 20_000_000_000
    )
    calls = {"n": 0}

    def fake_handle(downloader, service, view, client):
        calls["n"] += 1
        return "nomatch", "站点无搜索结果", ""

    plugin._handle = fake_handle  # type: ignore[method-assign]
    plugin._run_once("手动")
    rows = store["scan"]
    assert len(rows) == 4, "名录里每个种子都要有一行"
    assert store["stats"]["last_scanned"] == 4
    assert store["stats"]["last_groups"] == 1
    assert store["stats"]["last_processed"] == 1
    assert calls["n"] == 1, "同内容的 4 个辅种只能搜一次站点"
    assert all("内容组 #1" in r["group_text"] for r in rows)
    assert "共 4 个辅种" in rows[0]["group_text"]
    assert all(r["state"] == "nomatch" for r in rows)
    assert "同组代表" in rows[1]["message"]
    assert "搜索词" in "\n".join(store["log"])


def test_group_with_site_tracker_is_skipped_without_searching() -> None:
    plugin, store = _scan_plugin(0)
    torrents = _reseed_torrents(3, "Anime.Show.S01.2023.2160p.WEB-DL", 20_000_000_000)
    torrents[1]["tracker"] = "https://tracker.hdwing.org/announce"
    plugin._completed_torrents = lambda service: torrents  # type: ignore[method-assign]
    calls = {"n": 0}

    def fake_handle(*args):
        calls["n"] += 1
        return "nomatch", "无结果", ""

    plugin._handle = fake_handle  # type: ignore[method-assign]
    plugin._run_once("手动")
    assert calls["n"] == 0, "组里已有该站点种子就不必再搜"
    assert all(r["state"] == "exists" for r in store["scan"])
    assert "已有该站点" in store["scan"][0]["message"]


def test_group_cache_prevents_repeating_site_requests() -> None:
    from app.plugins.namecrossseed import grouping

    plugin, store = _scan_plugin(0)
    name = "Anime.Show.S01.2023.2160p.WEB-DL"
    size = 20_000_000_000
    plugin._completed_torrents = lambda service: _reseed_torrents(2, name, size)  # type: ignore[method-assign]
    key = "g:" + grouping.short_key(grouping.group_key(name, size, "", "h0"))
    store["cache"] = {key: {"r": "added", "t": time.time()}}
    calls = {"n": 0}
    plugin._handle = lambda *a: (calls.__setitem__("n", calls["n"] + 1) or  # type: ignore[method-assign]
                                ("nomatch", "无结果", ""))
    plugin._run_once("手动")
    assert calls["n"] == 0
    assert all(r["state"] == "cached" for r in store["scan"])


def test_one_group_exception_does_not_abort_round() -> None:
    plugin, store = _scan_plugin(0)
    torrents = [
        {"hash": f"h{i}", "name": f"Some.Movie.{i}.2020.1080p.WEB-DL",
         "size": 1_000_000 + i, "save_path": "/downloads", "tags": [],
         "tracker": "http://tracker.other.org/announce", "added_on": i}
        for i in range(3)
    ]
    plugin._completed_torrents = lambda service: torrents  # type: ignore[method-assign]
    calls = {"n": 0}

    def fake_handle(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟单组异常")
        return "nomatch", "无结果", ""

    plugin._handle = fake_handle  # type: ignore[method-assign]
    plugin._run_once("手动")
    assert calls["n"] == 3, "一组异常不能中断整轮"
    assert store["stats"]["last_processed"] == 3
    assert sum(1 for r in store["scan"] if r["state"] == "error") == 1
    assert "处理异常" in "\n".join(store["log"])


def test_renamed_english_identifiers_are_consistent() -> None:
    from app.plugins.namecrossseed import logview

    assert NameCrossSeed.__name__ == "NameCrossSeed"
    assert NameCrossSeed.plugin_name == "名称辅种"
    assert NameCrossSeed.plugin_config_prefix == "namecrossseed_"
    assert NameCrossSeed.plugin_version == "2.1.0"
    assert "namecrossseed.png" in NameCrossSeed.plugin_icon
    cmd = NameCrossSeed.get_command()[0]
    assert cmd["cmd"] == "/name_seed"
    assert cmd["data"]["action"] == "namecrossseed_run"
    assert _plugin().get_service()[0]["id"] == "NameCrossSeed"
    assert logview.PLUGIN_ID == "NameCrossSeed"
    assert "plugin/NameCrossSeed/log" in logview._POLL_JS
    assert "HDWinG" not in NameCrossSeed.plugin_desc
