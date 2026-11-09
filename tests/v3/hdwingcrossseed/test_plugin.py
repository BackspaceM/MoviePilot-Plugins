"""HDWinG 辅种插件：站点解析、匹配判定与 V3 插件契约测试。

测试不访问网络、不依赖下载器实例：站点客户端只测纯解析与相似度函数，
插件主类用 ``object.__new__`` 绕过宿主插件管理器，仅设置被测字段。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List

import pytest

from app.plugins.hdwingcrossseed import HDWinGCrossSeed
from app.plugins.hdwingcrossseed.hdwing import (
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
from app.plugins.hdwingcrossseed.rank import rank_candidates, size_diff_ratio

MOVIE = "Jojo.Rabbit.2019.1080p.MA.WEBRip.DD+5.1.x264-Porco"
OTHER = "The.Mandalorian.S02.1080p.WEB-DL.x264-Group"
SIZE = 15161507260


# ---------------------------------------------------------------------------
# 构造被测对象
# ---------------------------------------------------------------------------

def _plugin(**overrides: Any) -> HDWinGCrossSeed:
    """构造只带被测字段的插件实例，避免触发宿主插件管理器。"""
    plugin = object.__new__(HDWinGCrossSeed)
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
    from app.plugins.hdwingcrossseed import _TorrentView

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
    assert HDWinGCrossSeed.plugin_version
    assert HDWinGCrossSeed.plugin_config_prefix == "hdwingcrossseed_"
    assert HDWinGCrossSeed.get_state(_plugin()) is True
    assert HDWinGCrossSeed.get_state(_plugin(_enabled=False)) is False

    for name in ("init_plugin", "get_state", "get_api", "get_form", "get_page",
                 "get_service", "stop_service", "get_command"):
        assert callable(getattr(HDWinGCrossSeed, name)), name

    commands = HDWinGCrossSeed.get_command()
    assert commands and commands[0]["cmd"] == "/hdwing_seed"
    assert commands[0]["data"]["action"] == "hdwing_crossseed_run"

    apis = {item["path"] for item in _plugin().get_api()}
    assert {"/run", "/check", "/clear_cache", "/status"} <= apis


def test_service_registration() -> None:
    assert _plugin(_enabled=False).get_service() == []
    services = _plugin().get_service()
    assert len(services) == 1
    service = services[0]
    assert service["id"] == "HDWinGCrossSeed"
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
