"""名称辅种插件（MoviePilot V3）。

先把下载器里的种子按内容归并，同一份文件的多个辅种只搜一次站点、只添加一次：
- 阶段一：全量读取下载器已完成任务，建立全局 hash 集合与内容分组，不访问站点
- 阶段二：每组派一个代表去站点找候选；候选 hash 已在下载器里就直接跳过
- 名称只负责召回，是否同源由 info_hash 与文件清单（文件名 + 字节数）定案
- 发布名可用正则规则清洗，解决站点用空格、本地用点/连字符/中文前缀的问题
- 每次运行有请求预算与节流，单条异常隔离，不会中断整轮
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.schemas.types import EventType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper

from . import grouping, logview
from .site import (
    DEFAULT_SITE_URL,
    DEFAULT_USER_AGENT,
    SiteClient,
    HttpResult,
    parse_torrent,
)
from .rank import rank_candidates, size_diff_ratio

_CACHE_KEY = "cache"
_HISTORY_KEY = "history"
_STATS_KEY = "stats"
_MAX_HISTORY = 40
_PAGE_HISTORY = 8
_CMD_ACTION = "namecrossseed_run"
_ONCE_JOB_ID = "NameCrossSeed.RunOnce"
_STALE_RUN_SECONDS = 3 * 3600
_SUCCESS_TTL = 30 * 86400
_DEFAULT_UA = DEFAULT_USER_AGENT
_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8"

_STATE_TEXT = {
    "added": "已辅种",
    "exists": "已存在",
    "nomatch": "未匹配",
    "failed": "失败",
    "skipped": "跳过",
    "error": "异常",
}

_STATE_COLORS = {
    "added": "success",
    "exists": "primary",
    "nomatch": "grey",
    "failed": "error",
    "skipped": "grey",
    "error": "error",
}


def _clip(value: Any, limit: int) -> str:
    """截断长文本，详情页只展示摘要，避免单条记录把表格撑得很长。"""
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 配置与下载器任务的轻量视图
# ---------------------------------------------------------------------------

@dataclass
class _TorrentView:
    """把 qBittorrent / Transmission 的任务对象归一成插件需要的字段。"""

    hash: str = ""
    name: str = ""
    size: int = 0
    save_path: str = ""
    tags: List[str] = field(default_factory=list)
    tracker: str = ""
    added_on: int = 0

    @property
    def content_path(self) -> str:
        if not self.save_path:
            return self.name
        return os.path.join(self.save_path, self.name)


def _pick(item: Any, *keys: str, default: Any = None) -> Any:
    """从 dict 或对象上按候选字段名取值，兼容两种下载器。"""
    for key in keys:
        if isinstance(item, dict):
            value = item.get(key)
        else:
            value = getattr(item, key, None)
        if value is not None and value != "":
            return value
    return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_str_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return []


def _split_lines(value: Any) -> List[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    return _to_str_list(value)


def _format_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} TB"


def _format_time(timestamp: float) -> str:
    if not timestamp:
        return "未知"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _view_of(item: Any) -> Optional[_TorrentView]:
    """解析下载器任务对象，缺关键字段时返回 None。"""
    hash_value = str(
        _pick(item, "hash", "hashString", "hash_string", "info_hash") or ""
    ).strip().lower()
    name = str(_pick(item, "name") or "").strip()
    if not hash_value or not name:
        return None
    tracker = str(_pick(item, "tracker") or "")
    if not tracker:
        trackers = _pick(item, "trackers", default=[]) or []
        for entry in trackers:
            announce = _pick(entry, "announce", "url", default="")
            if announce:
                tracker = str(announce)
                break
    return _TorrentView(
        hash=hash_value,
        name=name,
        size=_to_int(_pick(item, "size", "totalSize", "total_size")),
        save_path=str(
            _pick(item, "save_path", "downloadDir", "download_dir") or ""
        ),
        tags=_to_str_list(_pick(item, "tags", "labels", default=[])),
        tracker=tracker,
        added_on=_to_int(_pick(item, "added_on", "addedDate", "added_date")),
    )


class NameCrossSeed(_PluginBase):
    """按发布名在站点找同源种子的辅种插件。"""

    plugin_name = "名称辅种"
    plugin_desc = (
        "先把下载器里同一份文件的多个辅种归并成一组，每组只搜一次站点、只添加一次；"
        "名称只用来找候选，info_hash 和文件清单完全一致才添加。"
        "发布名可用正则规则清洗，页面有实时日志与全量名录，设置只要填 Cookie。"
    )
    plugin_icon = (
        "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/namecrossseed.png"
    )
    plugin_version = "2.0.0"
    plugin_label = "做种,辅种,名称匹配,实时日志"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "namecrossseed_"
    plugin_order = 88
    auth_level = 1

    # --- 配置 ---
    _enabled: bool = False
    _notify: bool = True
    _cron: str = "0 4 * * *"
    _downloaders: List[str] = []
    _site_url: str = DEFAULT_SITE_URL
    _cookie: str = ""
    _user_agent: str = _DEFAULT_UA
    _interval: float = 6.0
    _max_requests: int = 300
    _max_tasks: int = 0  # 0 = 不限制，全量扫描
    _max_candidates: int = 3
    _min_score: float = 0.6
    _size_tolerance: float = 2.0
    _match_mode: str = "strict"
    _skip_checking: bool = True
    _add_paused: bool = False
    _tag: str = "辅种"
    _skip_tags: List[str] = ["辅种"]
    _skip_paths: List[str] = []
    _min_size_gb: float = 0.0
    _cache_days: int = 7
    _name_rules: str = ""

    # --- 运行态 ---
    _state_lock = threading.Lock()
    _running: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_started: float = 0.0
        self._budget = 0
        self._search_cache: Dict[str, List] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """载入配置并重置运行状态。"""
        self.stop_service()
        self._enabled = False
        self._notify = True
        self._cron = "0 4 * * *"
        self._downloaders = []
        self._site_url = DEFAULT_SITE_URL
        self._cookie = ""
        self._user_agent = _DEFAULT_UA
        self._interval = 6.0
        self._max_requests = 300
        self._max_tasks = 0
        self._max_candidates = 3
        self._min_score = 0.6
        self._size_tolerance = 2.0
        self._match_mode = "strict"
        self._skip_checking = True
        self._add_paused = False
        self._tag = "辅种"
        self._skip_tags = ["辅种"]
        self._skip_paths = []
        self._min_size_gb = 0.0
        self._cache_days = 7
        self._name_rules = ""
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._cron = str(config.get("cron") or "0 4 * * *").strip()
        self._downloaders = _to_str_list(config.get("downloaders"))
        self._site_url = str(config.get("site_url") or DEFAULT_SITE_URL).strip().rstrip("/")
        self._cookie = str(config.get("cookie") or "").strip()
        self._user_agent = str(config.get("user_agent") or _DEFAULT_UA).strip()
        self._interval = max(_to_float(config.get("interval"), 6.0), 5.0)
        self._max_requests = max(_to_int(config.get("max_requests"), 300), 1)
        # 0 表示不限制，扫全下载器；负数也按 0 处理
        self._max_tasks = max(_to_int(config.get("max_tasks"), 0), 0)
        self._max_candidates = max(_to_int(config.get("max_candidates"), 3), 1)
        self._min_score = min(max(_to_float(config.get("min_score"), 0.6), 0.1), 1.0)
        self._size_tolerance = max(_to_float(config.get("size_tolerance"), 2.0), 0.0)
        self._match_mode = str(config.get("match_mode") or "strict")
        self._skip_checking = bool(config.get("skip_checking", True))
        self._add_paused = bool(config.get("add_paused"))
        self._tag = str(config.get("tag") or "").strip()
        self._skip_tags = _to_str_list(config.get("skip_tags")) or ["辅种"]
        self._skip_paths = _split_lines(config.get("skip_paths"))
        self._min_size_gb = max(_to_float(config.get("min_size_gb"), 0.0), 0.0)
        self._cache_days = max(_to_int(config.get("cache_days"), 7), 1)
        self._name_rules = str(config.get("name_rules") or "")
        bad_rules = grouping.invalid_rules(self._name_rules)
        if bad_rules:
            logger.warning(f"名称辅种：忽略无效的名称正则 {bad_rules[:3]}")

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """通知正在运行的任务尽快停止，并释放本轮占用的后台资源。"""
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remove_once):
            try:
                remove_once(self.__class__.__name__, _ONCE_JOB_ID)
            except Exception as err:  # noqa: BLE001 - 调度器未启动时不应阻塞停用
                logger.debug(f"名称辅种：取消待执行调度任务失败 {err}")
        thread = getattr(self, "_thread", None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10)
        with self._state_lock:
            self._running = False

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时辅种服务。"""
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except (ValueError, TypeError):
            trigger = CronTrigger.from_crontab("0 4 * * *")
        return [
            {
                "id": "NameCrossSeed",
                "name": "名称辅种",
                "trigger": trigger,
                "func": self.run_scheduled,
                "kwargs": {},
                "func_kwargs": {},
            }
        ]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令，便于从聊天窗口触发一次辅种。"""
        return [
            {
                "cmd": "/name_seed",
                "event": EventType.PluginAction,
                "desc": "立即跑一次名称辅种",
                "category": "站点",
                "data": {"action": _CMD_ACTION},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event) -> None:
        """消费远程命令事件，按 action 与插件 ID 过滤后触发一次辅种。"""
        if not event:
            return
        data = event.event_data or {}
        if not isinstance(data, dict) or data.get("action") != _CMD_ACTION:
            return
        # 虚拟分身场景下同一份源码会有多个实例，只处理派发给自己的动作
        target = data.get("plugin_id")
        if target and target != self.__class__.__name__:
            return
        if not self._enabled:
            self.post_message(title="名称辅种", text="插件未启用，命令已忽略")
            return
        if not self._cookie:
            self.post_message(title="名称辅种", text="未配置站点 Cookie，无法执行")
            return
        if not self.start_run(trigger="命令"):
            self.post_message(title="名称辅种", text="上一轮任务尚未结束，请稍后再试")

    # ------------------------------------------------------------------
    # 表单
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """设置只留必要的几项；限流、相似度、标签等全部用内置安全默认值。"""
        try:
            downloader_items = [
                {"title": name, "value": name}
                for name in (DownloaderHelper().get_configs() or {}).keys()
            ]
        except Exception as err:  # noqa: BLE001 - 未配置下载器时表单仍需可渲染
            logger.debug(f"名称辅种：读取下载器列表失败 {err}")
            downloader_items = []
        content: List[dict] = [
            {"component": "VRow", "content": [
                self._col(12, 3, [{"component": "VSwitch", "props": {
                    "model": "enabled", "label": "启用插件"}}]),
                self._col(12, 3, [{"component": "VSwitch", "props": {
                    "model": "notify", "label": "结果通知"}}]),
                self._col(12, 3, [{"component": "VTextField", "props": {
                    "model": "cron", "label": "执行周期", "placeholder": "0 4 * * *",
                    "hint": "默认每天 04:00", "persistentHint": True}}]),
                self._col(12, 3, [{"component": "VTextField", "props": {
                    "model": "max_tasks", "label": "每轮处理上限", "placeholder": "0",
                    "type": "number", "hint": "0 = 不限，扫全下载器",
                    "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 12, [{"component": "VSelect", "props": {
                    "model": "downloaders", "label": "下载器", "multiple": True,
                    "chips": True, "clearable": True, "items": downloader_items,
                    "hint": "留空就是全部已启用的下载器", "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 6, [{"component": "VTextField", "props": {
                    "model": "site_url", "label": "站点地址",
                    "placeholder": DEFAULT_SITE_URL}}]),
                self._col(12, 6, [{"component": "VTextField", "props": {
                    "model": "user_agent", "label": "User-Agent",
                    "placeholder": _DEFAULT_UA,
                    "hint": "带 cf_clearance 时要与浏览器一致", "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 12, [{"component": "VTextarea", "props": {
                    "model": "cookie", "label": "站点 Cookie", "rows": 3,
                    "placeholder": "hdchina=...; PHPSESSID=...; cf_clearance=...",
                    "hint": "从浏览器开发者工具复制完整 cookie 串。Cookie 属于个人凭据，只存本机配置",
                    "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 12, [{"component": "VTextarea", "props": {
                    "model": "name_rules", "label": "名称清洗规则（可选）", "rows": 4,
                    "placeholder": "[._]+ => <space>",
                    "hint": (
                        "每行一条「正则 => 替换」；<space> 表示空格（可单独用或嵌在替换文本里，如 \\1<space>），"
                        "替换侧留空表示删除。整个框留空即用内置规则（点/下划线转空格、去方括号标签与发布组后缀）。"
                        "例：去掉中文名前缀写 ^[\\u4e00-\\u9fff]+[.\\s_-]+ => <empty>；"
                        "站点标题用空格时可写 [._-]+ => <space>。填了规则就覆盖内置规则，"
                        "每组的实际搜索词会写进运行日志。"
                    ),
                    "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 12, [{"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal"}, "text": (
                        "装好填 Cookie 就能用。每次先把下载器里同一份文件的多个辅种归并成一组，"
                        "每组只搜一次站点、只添加一次，不会拿着同一个内容反复搜、反复插；"
                        "站点种子已经在下载器里就直接跳过。名称只负责找候选，"
                        "找到后还要解析 info_hash 和文件清单，完全一致才添加。"
                        "运行日志、搜索词和逐条结果在详情页实时显示；想强制重扫点「清空缓存」。")}]),
            ]},
        ]
        return [{"component": "VForm", "content": content}], {
            "enabled": False,
            "notify": True,
            "cron": "0 4 * * *",
            "max_tasks": 0,
            "downloaders": [],
            "site_url": DEFAULT_SITE_URL,
            "user_agent": _DEFAULT_UA,
            "cookie": "",
            "name_rules": "",
        }

    @staticmethod
    def _col(cols: int, md: int, content: List[dict]) -> dict:
        return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": content}

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------

    def get_page(self) -> List[dict]:
        """详情页：操作按钮 + 实时日志 + 本轮全量扫描名录。"""
        stats = self.get_data(_STATS_KEY) or {}
        pid = self.__class__.__name__
        buttons = [
            {"component": "VBtn", "props": {
                "variant": "tonal", "color": "primary",
                "prepend-icon": "mdi-play", "class": "ma-1"},
             "text": "立即辅种",
             "events": {"click": {"api": f"plugin/{pid}/run", "method": "post", "params": {}}}},
            {"component": "VBtn", "props": {
                "variant": "tonal", "prepend-icon": "mdi-account-check", "class": "ma-1"},
             "text": "检测 Cookie",
             "events": {"click": {"api": f"plugin/{pid}/check", "method": "post", "params": {}}}},
            {"component": "VBtn", "props": {
                "variant": "tonal", "color": "warning",
                "prepend-icon": "mdi-delete-sweep", "class": "ma-1"},
             "text": "清空缓存",
             "events": {"click": {"api": f"plugin/{pid}/clear_cache", "method": "post", "params": {}}}},
        ]
        rows = logview.scan_rows(self)
        page_html = logview.render(
            stats=stats,
            log=logview.log_text(self),
            rows=rows,
            running=bool(self._running),
            truncated=len(rows) >= logview.MAX_ROWS,
        )
        return [
            {"component": "div", "content": buttons},
            {"component": "div", "html": page_html},
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件接口。"""
        return [
            {"path": "/run", "endpoint": self.api_run, "methods": ["POST"],
             "auth": "bear", "summary": "立即执行一次辅种"},
            {"path": "/check", "endpoint": self.api_check, "methods": ["POST"],
             "auth": "bear", "summary": "检测站点 Cookie"},
            {"path": "/clear_cache", "endpoint": self.api_clear_cache, "methods": ["POST"],
             "auth": "bear", "summary": "清空辅种缓存"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询运行状态"},
            {"path": "/log", "endpoint": self.api_log, "methods": ["GET"],
             "auth": "bear", "summary": "读取实时运行日志与进度"},
        ]

    def api_run(self) -> schemas.Response:
        """立即在后台执行一次辅种。"""
        if not self._enabled:
            return schemas.Response(success=False, message="插件未启用")
        if not self._cookie:
            return schemas.Response(success=False, message="未配置站点 Cookie")
        if self._running:
            return schemas.Response(success=False, message="已有任务在运行")
        started = self.start_run(trigger="手动")
        return schemas.Response(
            success=started,
            message="已开始，详情页日志会实时更新" if started else "任务启动失败",
        )

    def api_check(self) -> schemas.Response:
        """检测 cookie 是否有效。"""
        if not self._cookie:
            return schemas.Response(success=False, message="未配置站点 Cookie")
        ok, message = self._build_client().check_login()
        return schemas.Response(success=ok, message=message)

    def api_clear_cache(self) -> schemas.Response:
        """清空重试缓存与本轮名录，下次会把全部种子重新核一遍。"""
        self.save_data(_CACHE_KEY, {})
        self.save_data(logview.SCAN_KEY, [])
        stats = dict(self.get_data(_STATS_KEY) or {})
        stats["pending_retry"] = 0
        self.save_data(_STATS_KEY, stats)
        logview.emit(self, "缓存已清空，下一轮会重新核对下载器里的全部种子。")
        return schemas.Response(success=True, message="缓存已清空，下轮全量重扫")

    def api_log(self) -> schemas.Response:
        """页面每 3 秒轮询一次：日志纯文本 + 当前进度。"""
        stats = self.get_data(_STATS_KEY) or {}
        return schemas.Response(success=True, data={
            "text": logview.log_text(self),
            "progress": logview.progress(stats, bool(self._running), as_html=False),
            "running": bool(self._running),
        })

    def api_status(self) -> schemas.Response:
        """返回运行状态概要。"""
        return schemas.Response(success=True, data={
            "enabled": self._enabled,
            "running": self._running,
            "downloaders": self._downloaders,
            "cron": self._cron,
            "stats": self.get_data(_STATS_KEY) or {},
        })

    # ------------------------------------------------------------------
    # 调度入口
    # ------------------------------------------------------------------

    def run_scheduled(self) -> None:
        """定时服务入口。"""
        self.start_run(trigger="定时")

    def start_run(self, trigger: str = "手动") -> bool:
        """
        启动一次辅种执行。

        优先交给宿主调度器的一次性任务执行（复用宿主线程池，插件重载时自动丢弃旧实例任务），
        调度器未运行时才退回插件自建线程；两种方式都不会阻塞调用方。
        """
        with self._state_lock:
            if self._running:
                # 异常退出可能留下未复位标记，超过阈值后允许重新进入
                if time.time() - self._run_started < _STALE_RUN_SECONDS:
                    logger.info("名称辅种：上一轮尚未结束，本次跳过")
                    return False
                logger.warning("名称辅种：上一轮运行状态疑似残留，强制重置后继续")
            self._running = True
            self._run_started = time.time()
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is None:
            self._stop_event = stop_event = threading.Event()
        stop_event.clear()
        logview.emit(self, f"开始一轮辅种（{trigger}），正在读取下载器里的全部已完成任务…")

        add_once = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if callable(add_once):
            try:
                scheduled = add_once(
                    self.__class__.__name__,
                    _ONCE_JOB_ID,
                    self._run_wrapper,
                    "名称辅种执行",
                    delay_seconds=0,
                    func_kwargs={"trigger": trigger},
                )
            except Exception as err:  # noqa: BLE001 - 调度器异常时退回自建线程
                logger.warning(f"名称辅种：提交调度任务失败，改为后台线程执行 {err}")
                scheduled = False
            if scheduled:
                return True
        thread = threading.Thread(
            target=self._run_wrapper,
            args=(trigger,),
            name=f"{self.__class__.__name__}Run",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return True

    def _run_wrapper(self, trigger: str) -> None:
        """执行一轮辅种并兜底复位运行状态。"""
        try:
            self._run_once(trigger)
        except Exception as err:  # noqa: BLE001 - 后台任务必须兜底，避免任务静默消失
            logger.error(f"名称辅种执行异常：{err} - {traceback.format_exc()}")
            logview.emit(self, f"执行异常：{err}", level="error")
            self._save_result({
                "trigger": trigger,
                "scanned": 0, "matched": 0, "added": 0, "failed": 0,
                "skipped": 0, "message": f"执行异常：{err}",
                "records": [],
            })
        finally:
            with self._state_lock:
                self._running = False
            if getattr(self, "_thread", None) is threading.current_thread():
                self._thread = None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def _run_once(self, trigger: str) -> None:
        """两阶段跑一轮：先确定下载器里有哪些内容，再按组去站点搜。

        下载器里同一份文件常常有多个辅种（hash 不同、名称略有差异）。
        逐条搜站会把同一内容反复搜索、反复尝试添加，所以先全量采集并归并，
        每组只派一个代表搜站、只添加一次，组内成员复用结果但各留一行名录。
        """
        started = time.time()
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is None:
            self._stop_event = stop_event = threading.Event()
        stop_event.clear()
        result: Dict[str, Any] = {
            "trigger": trigger,
            "scanned": 0, "groups": 0, "eligible": 0, "processed": 0,
            "matched": 0, "added": 0, "failed": 0, "skipped": 0,
            "message": "", "records": [],
        }
        if not self._cookie:
            result["message"] = "未配置站点 Cookie"
            logview.emit(self, result["message"], level="warning")
            self._save_result(result, started)
            return
        client = self._build_client()
        ok, message = client.check_login()
        if not ok:
            result["message"] = f"Cookie 检测失败：{message}"
            logview.emit(self, result["message"], level="warning")
            self._save_result(result, started)
            self._notify_result(result)
            return

        self._budget = self._max_requests
        self._search_cache = {}
        cache = dict(self.get_data(_CACHE_KEY) or {})
        services = self._services()
        if not services:
            result["message"] = "没有可用的下载器"
            logview.emit(self, result["message"], level="warning")
            self._save_result(result, started)
            return
        host = self._site_host()
        logview.emit(self, f"开始一轮辅种（{trigger}）：先只读盘点下载器，本阶段不访问站点…")

        # ---------------- 阶段一：全量采集，不访问站点 ----------------
        entries: List[Tuple[str, Any, _TorrentView, Dict[str, Any]]] = []
        known_hashes: Set[str] = set()
        for downloader, service in services.items():
            if self._stop_event.is_set():
                break
            torrents = self._completed_torrents(service)
            if torrents is None:
                result["message"] = f"{downloader} 读取任务失败"
                logview.emit(self, result["message"], level="warning")
                continue
            count = 0
            for item in torrents:
                view = _view_of(item)
                if not view:
                    continue
                known_hashes.add(view.hash)
                row: Dict[str, Any] = {
                    "downloader": downloader,
                    "name": view.name[:140],
                    "size_text": _format_size(view.size),
                    "state": "pending",
                    "message": "",
                    "group_text": "",
                }
                entries.append((downloader, service, view, row))
                count += 1
            logview.emit(self, f"下载器 {downloader}：读到 {count} 个已完成任务（全量，不截断）。")

        result["scanned"] = len(entries)
        # _handle 与本流程共享同一个集合：站点候选 hash 只要在里面就直接跳过
        self._known_hashes = known_hashes
        if not entries:
            result["message"] = "下载器里没有可读取的已完成任务"
            logview.emit(self, result["message"], level="warning")
            self._save_result(result, started)
            return

        # ---------------- 分组：同内容的多个辅种归为一组 ----------------
        groups: Dict[str, List[Tuple[str, Any, _TorrentView, Dict[str, Any]]]] = {}
        for entry in entries:
            view = entry[2]
            key = grouping.group_key(view.name, view.size, self._name_rules, view.hash)
            groups.setdefault(key, []).append(entry)
        result["groups"] = len(groups)
        bad_rules = grouping.invalid_rules(self._name_rules)
        if bad_rules:
            logview.emit(self, f"名称规则里有 {len(bad_rules)} 条正则无效，已跳过：{bad_rules[:2]}",
                         level="warning")
        logview.emit(
            self,
            f"盘点完成：{len(entries)} 个种子归并成 {len(groups)} 组内容；"
            "同内容的多个辅种只搜一次站点、只添加一次。",
        )

        # ---------------- 阶段二：逐组搜站 ----------------
        limit = self._max_tasks  # 0 = 不限制组数
        processed = 0
        stopped = ""
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        for number, (key, members) in enumerate(ordered, 1):
            group_label = (f"内容组 #{number} · 共 {len(members)} 个辅种" if len(members) > 1
                           else f"内容组 #{number}")
            for _, _, _, row in members:
                row["group_text"] = group_label
            if self._stop_event.is_set():
                stopped = "任务被中止"
                for _, _, _, row in members:
                    row["state"] = "pending"
                    row["message"] = stopped
                continue
            gkey = "g:" + grouping.short_key(key)
            cached = cache.get(gkey)
            site_covered = any(host and host in (v.tracker or "").lower()
                               for _, _, v, _ in members)
            eligible_members = [e for e in members if self._eligible(e[2], cache, e[0])]
            representative = min(
                eligible_members, key=lambda e: e[2].added_on or 0
            ) if eligible_members else None
            if representative is not None:
                result["eligible"] += 1

            def mark(state: str, text: str) -> None:
                for _, _, _, row in members:
                    row["state"] = state
                    row["message"] = text

            if site_covered:
                mark("exists", "组内已有该站点的种子，无需再辅种")
                continue
            if cached and not self._cache_expired(cached):
                mark("cached", f"近期已核对（{cached.get('r', '')}），不重复请求站点")
                continue
            if representative is None:
                # 兼容旧版按单个种子存的缓存：升级后也别让这些组重复请求站点
                legacy_hit = any(
                    isinstance(cache.get(f"{e[0]}|{e[2].hash}"), dict)
                    and not self._cache_expired(cache[f"{e[0]}|{e[2].hash}"])
                    for e in members
                )
                if legacy_hit:
                    mark("cached", "近期已核对（旧版记录），不重复请求站点")
                else:
                    mark("ignored", "带跳过标签 / 本站种子 / 体积或路径被排除")
                continue
            if limit and processed >= limit:
                stopped = f"达到本轮处理上限 {limit} 组"
                mark("pending", stopped + "，下轮继续")
                continue
            if self._budget <= 0:
                stopped = "站点请求预算用尽"
                mark("pending", stopped + "，下轮继续")
                continue

            downloader, service, view, _ = representative
            processed += 1
            result["processed"] += 1
            keys = grouping.search_keys(view.name, self._name_rules)
            logview.emit(
                self,
                f"[{processed}/{len(ordered)}] 第 {number} 组（{len(members)} 个辅种）"
                f"由「{view.name[:70]}」代表搜站｜搜索词：{' / '.join(keys[:4]) or '（无）'}",
            )
            # 单组异常必须隔离，绝不能像之前那样让一轮任务整段中断
            try:
                state, detail, site_title = self._handle(downloader, service, view, client)
            except Exception as err:  # noqa: BLE001
                logger.error(f"名称辅种：处理分组异常 {err} - {traceback.format_exc()}")
                logview.emit(self, f"处理异常：{err}", level="error")
                state, detail, site_title = "error", f"处理异常：{str(err)[:120]}", ""

            prefix = f"{site_title[:60]}｜" if site_title else ""
            for _, _, member_view, row in members:
                row["state"] = state
                if member_view is view:
                    row["message"] = (prefix + detail)[:160]
                else:
                    row["message"] = (
                        f"{prefix}同组代表「{view.name[:40]}」已处理，本种子不重复搜站"
                    )[:160]
            label = logview.state_label(state)
            logview.emit(
                self,
                f"[{processed}] {label}：{view.name[:80]}"
                + (f" → {site_title[:60]}" if site_title else "")
                + f"（{detail[:100]}）"
                + (f"｜同组 {len(members)} 个种子复用此结果" if len(members) > 1 else ""),
                level="warning" if state in ("failed", "error") else "info",
            )
            if state in ("added", "exists"):
                result["matched"] += 1
            if state == "added":
                result["added"] += 1
            elif state == "failed":
                result["failed"] += 1
            elif state == "skipped":
                result["skipped"] += 1
            cache[gkey] = {
                "r": state, "t": time.time(), "m": detail[:200], "n": view.name[:120],
            }
            result["records"].append({
                "downloader": downloader,
                "local": view.name[:80],
                "site": site_title[:80] if site_title else "-",
                "state": state,
                "message": detail[:160],
            })

        result["message"] = stopped
        self.save_data(_CACHE_KEY, cache)
        self.save_data(logview.SCAN_KEY, [e[3] for e in entries][-logview.MAX_ROWS:])
        result["pending_retry"] = sum(
            1 for entry in cache.values()
            if isinstance(entry, dict) and entry.get("r") in ("nomatch", "failed")
        )
        logview.emit(
            self,
            f"本轮结束：下载器 {len(entries)} 个种子 → {len(groups)} 组内容，"
            f"实际搜站 {processed} 组、新增 {result['added']} 个、失败 {result['failed']} 组"
            + (f"；{stopped}" if stopped else "")
            + "。全量名录与搜索词见上方日志。",
        )
        self._save_result(result, started)
        self._notify_result(result)

    def _services(self) -> Dict[str, Any]:
        """按配置解析下载器服务，只保留支持辅种的类型。"""
        helper = DownloaderHelper()
        names = self._downloaders or list((helper.get_configs() or {}).keys())
        services = helper.get_services(name_filters=names) or {}
        usable: Dict[str, Any] = {}
        for name, service in services.items():
            if not service or not getattr(service, "instance", None):
                continue
            if service.type not in ("qbittorrent", "transmission"):
                logger.info(f"名称辅种：跳过不支持的下载器 {name}（{service.type}）")
                continue
            usable[name] = service
        return usable

    @staticmethod
    def _completed_torrents(service: Any) -> Optional[List[Any]]:
        """
        读取下载器中已完成的任务。

        qBittorrent 的 get_completed_torrents 已覆盖全部已完成任务；
        Transmission 只返回 seeding/seed pending 状态，已停止的完成种子会被漏掉，
        因此对 Transmission 额外做一次全量扫描并按完成度补齐。
        """
        try:
            completed = service.instance.get_completed_torrents()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"名称辅种：读取完成任务异常 {err}")
            return None
        if completed is None:
            return None
        if getattr(service, "type", "") != "transmission":
            return completed
        try:
            all_torrents, error = service.instance.get_torrents()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"名称辅种：读取 Transmission 全量任务异常 {err}")
            return completed
        if error or not all_torrents:
            return completed
        merged: Dict[str, Any] = {}
        for item in list(completed) + list(all_torrents):
            view = _view_of(item)
            if not view:
                continue
            progress = _to_float(
                _pick(item, "percent_done", "percentDone", "progress"), 0.0
            )
            if view.hash in merged:
                continue
            if progress >= 0.999:
                merged[view.hash] = item
        for item in completed:
            view = _view_of(item)
            if view and view.hash not in merged:
                merged[view.hash] = item
        return list(merged.values())

    def _eligible(self, view: _TorrentView, cache: Dict[str, Any], downloader: str) -> bool:
        """判断任务是否值得处理。"""
        key = f"{downloader}|{view.hash}"
        entry = cache.get(key)
        if entry and not self._cache_expired(entry):
            return False
        if self._skip_tags and set(tag.lower() for tag in view.tags) & set(
                tag.lower() for tag in self._skip_tags):
            return False
        if self._min_size_gb and view.size and view.size < self._min_size_gb * (1 << 30):
            return False
        if self._skip_paths:
            content_path = view.content_path
            for prefix in self._skip_paths:
                if content_path.startswith(prefix) or view.save_path.startswith(prefix):
                    return False
        host = self._site_host()
        if host and host in (view.tracker or "").lower():
            return False
        return True

    def _cache_expired(self, entry: Dict[str, Any]) -> bool:
        ttl = _SUCCESS_TTL if entry.get("r") == "added" else self._cache_days * 86400
        return (time.time() - _to_float(entry.get("t"))) > ttl

    def _handle(
        self, downloader: str, service: Any, view: _TorrentView, client: SiteClient
    ) -> Tuple[str, str, str]:
        """处理一组内容；任何异常都收敛成一条 error 结果，不中断整轮。"""
        try:
            return self._handle_group(downloader, service, view, client)
        except Exception as err:  # noqa: BLE001
            logger.error(f"名称辅种：处理分组异常 {err} - {traceback.format_exc()}")
            logview.emit(self, f"本组处理异常：{err}", level="error")
            return "error", f"处理异常：{str(err)[:120]}", ""

    def _handle_group(
        self, downloader: str, service: Any, view: _TorrentView, client: SiteClient
    ) -> Tuple[str, str, str]:
        """名称负责召回，info_hash / 文件清单负责定案，体积只参与排序。"""
        keys = grouping.search_keys(view.name, self._name_rules)
        if not keys:
            return "skipped", "无法从名称提取搜索词", ""
        results = self._search(client, keys)
        if not results:
            return "nomatch", "站点无搜索结果", ""
        ranked = rank_candidates(view.name, view.size, results, self._min_score)
        if not ranked:
            return "nomatch", f"{len(results)} 条结果名称均不匹配", ""

        known = getattr(self, "_known_hashes", None)
        if known is None:
            known = set()
            self._known_hashes = known
        tried = 0
        saw_meta = False
        add_failed = False
        last = "站点候选均与本地文件不一致"
        for _score, diff, candidate in ranked:
            if self._stop_event.is_set() or self._budget <= 0:
                break
            if tried >= self._max_candidates:
                break
            if not getattr(candidate, "download_path", ""):
                continue
            tried += 1
            self._budget -= 1
            # 体积只用来排序，绝不当拒绝条件：整季合集与单集本来就差几十倍，
            # 之前按 10% 硬筛会把真正同源的候选全部砍掉。
            try:
                content = client.download(candidate)
            except Exception as err:  # noqa: BLE001 - 单个候选失败不影响后面的候选
                logview.emit(self, f"  ↳ 下载「{str(candidate.title)[:50]}」失败：{err}",
                             level="warning")
                continue
            if not content:
                continue
            meta = parse_torrent(content)
            if not meta:
                continue
            saw_meta = True
            info_hash = str(meta.info_hash or "").strip().lower()
            if info_hash and info_hash in known:
                return "exists", "站点种子已在下载器里（全局名录命中）", candidate.title
            mode = self._judge(service, view, meta)
            if not mode:
                last = f"{candidate.title[:50]} 文件清单不一致（体积差 {diff * 100:.1f}%）"
                logview.emit(self, f"  ↳ 候选「{candidate.title[:50]}」文件清单不一致，跳过")
                continue
            if self._exists(service, meta.info_hash):
                if info_hash:
                    known.add(info_hash)
                return "exists", f"站点种子已在下载器（{mode}）", candidate.title
            ok, message = self._add(service, view, content)
            if ok:
                if info_hash:
                    known.add(info_hash)
                logview.emit(
                    self,
                    f"名称辅种成功：{view.name} -> {candidate.title}"
                    f"（{mode}，体积差 {diff * 100:.2f}%）",
                )
                return "added", f"匹配方式 {mode}，体积差 {diff * 100:.2f}%", candidate.title
            add_failed = True
            last = message
        if add_failed:
            return "failed", last, ""
        if tried and not saw_meta:
            return "failed", "站点种子下载或解析失败", ""
        return "nomatch", last, ""

    def _search(self, client: SiteClient, keywords: Any) -> List[Any]:
        """按多个关键词依次搜索，带轮内缓存与预算控制。"""
        for keyword in keywords:
            if self._budget <= 0:
                return []
            if keyword in self._search_cache:
                cached = self._search_cache[keyword]
                if cached:
                    return cached
                continue
            self._budget -= 1
            results = client.search(keyword)
            self._search_cache[keyword] = results
            if results:
                return results
        return []

    def _judge(self, service: Any, view: _TorrentView, meta: Any) -> str:
        """判定站点种子是否与本地任务同源，返回匹配方式或空串。"""
        if meta.info_hash and meta.info_hash == view.hash:
            return "hash"
        if self._files_match(service, view, meta):
            return "files"
        if self._match_mode == "loose" and self._size_tolerance > 0:
            if size_diff_ratio(view.size, meta.total_size) <= self._size_tolerance / 100:
                return "size"
        return ""

    def _files_match(self, service: Any, view: _TorrentView, meta: Any) -> bool:
        """逐文件比对文件名与字节数，全部一致才算同源。"""
        if not meta.files:
            return False
        try:
            local_files = service.instance.get_files(view.hash)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"名称辅种：读取文件清单失败 {err}")
            return False
        if not local_files:
            return False
        local_map: Dict[str, int] = {}
        for item in local_files:
            name = _pick(item, "name", "path")
            size = _pick(item, "size", "bytesTotal", "length")
            if not name:
                continue
            local_map[os.path.basename(str(name))] = _to_int(size)
        site_map = {os.path.basename(path): int(size) for path, size in meta.files.items()}
        if not local_map or not site_map:
            return False
        if len(local_map) == 1 and len(site_map) == 1:
            return list(local_map.values())[0] == list(site_map.values())[0]
        if len(local_map) != len(site_map):
            return False
        return local_map == site_map

    @staticmethod
    def _exists(service: Any, info_hash: str) -> bool:
        try:
            torrents, _error = service.instance.get_torrents(ids=[info_hash])
            return bool(torrents)
        except Exception:  # noqa: BLE001
            return False

    def _add(self, service: Any, view: _TorrentView, content: bytes) -> Tuple[bool, str]:
        """把站点种子加入下载器。"""
        download_dir = view.save_path or None
        label = [self._tag] if self._tag else None
        try:
            if service.type == "qbittorrent":
                payload: Dict[str, Any] = {
                    "content": content,
                    "download_dir": download_dir,
                    "is_paused": self._add_paused,
                    "tag": label,
                }
                if self._skip_checking:
                    payload["is_skip_checking"] = True
                ret = service.instance.add_torrent(**payload)
                ok = ret[0] if isinstance(ret, tuple) else bool(ret)
                if not ok and self._skip_checking:
                    # 个别 qB 版本识别不了 seedMode，退回普通添加，让下载器自己校验
                    payload.pop("is_skip_checking", None)
                    ret = service.instance.add_torrent(**payload)
                    ok = ret[0] if isinstance(ret, tuple) else bool(ret)
                return bool(ok), "已添加" if ok else "下载器拒绝添加"
            if service.type == "transmission":
                ret = service.instance.add_torrent(
                    content=content,
                    download_dir=download_dir,
                    is_paused=self._add_paused,
                    labels=label,
                )
                return bool(ret), "已添加" if ret else "下载器拒绝添加"
        except Exception as err:  # noqa: BLE001
            logger.error(f"名称辅种：添加种子异常 {err}")
            return False, f"添加异常：{err}"
        return False, f"不支持的下载器类型 {service.type}"

    # ------------------------------------------------------------------
    # 结果持久化
    # ------------------------------------------------------------------

    def _remember(
        self, cache: Dict[str, Any], downloader: str, view: _TorrentView,
        state: str, message: str,
    ) -> None:
        if state == "skipped":
            return
        cache[f"{downloader}|{view.hash}"] = {
            "r": state,
            "t": time.time(),
            "m": message[:200],
            "n": view.name[:120],
        }

    def _save_result(self, result: Dict[str, Any], started: Optional[float] = None) -> None:
        duration = int(time.time() - started) if started else 0
        stamp = time.time()
        stats = self.get_data(_STATS_KEY) or {}
        stats.update({
            "last_run": stamp,
            "last_run_text": _format_time(stamp),
            "last_trigger": result.get("trigger", ""),
            "last_scanned": result.get("scanned", 0),
            "last_groups": result.get("groups", 0),
            "last_eligible": result.get("eligible", 0),
            "last_processed": result.get("processed", 0),
            "last_matched": result.get("matched", 0),
            "last_added": result.get("added", 0),
            "last_failed": result.get("failed", 0),
            "last_duration": duration,
            "last_message": result.get("message", ""),
            "total_added": _to_int(stats.get("total_added")) + result.get("added", 0),
            "total_failed": _to_int(stats.get("total_failed")) + result.get("failed", 0),
            "total_scanned": _to_int(stats.get("total_scanned")) + result.get("scanned", 0),
            "pending_retry": _to_int(result.get("pending_retry"),
                                     _to_int(stats.get("pending_retry"), 0)),
        })
        self.save_data(_STATS_KEY, stats)
        records = result.get("records") or []
        if records:
            history = self.get_data(_HISTORY_KEY) or []
            stamp_text = _format_time(stamp)
            for record in records:
                history.insert(0, {
                    "time": stamp_text,
                    "downloader": record.get("downloader", ""),
                    "local": record.get("local", ""),
                    "site": record.get("site", "-"),
                    "state": record.get("state", ""),
                    "message": record.get("message", ""),
                })
            self.save_data(_HISTORY_KEY, history[:_MAX_HISTORY])

    def _notify_result(self, result: Dict[str, Any]) -> None:
        if not self._notify:
            return
        added = result.get("added", 0)
        failed = result.get("failed", 0)
        if not added and not failed and not result.get("message"):
            return
        title = "名称辅种完成" if added else "名称辅种未新增"
        lines = [
            f"下载器 {result.get('scanned', 0)} 个种子归并成 {result.get('groups', 0)} 组内容，"
            f"实际搜站 {result.get('processed', 0)} 组，"
            f"新增 {added}，失败 {failed}",
        ]
        if result.get("message"):
            lines.append(str(result["message"]))
        added_records = [r for r in (result.get("records") or [])
                         if r.get("state") == "added"]
        for record in added_records[:10]:
            lines.append(f"· {record.get('local', '')[:50]} → {record.get('site', '')[:50]}")
        if len(added_records) > 10:
            lines.append(f"· 另有 {len(added_records) - 10} 条，详见插件详情页日志")
        try:
            self.post_message(title=title, text="\n".join(lines))
        except Exception as err:  # noqa: BLE001 - 通知失败不影响主流程
            logger.warning(f"名称辅种：发送通知失败 {err}")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _site_host(self) -> str:
        base = (self._site_url or DEFAULT_SITE_URL).lower()
        base = base.split("://")[-1].split("/")[0]
        return base

    def _build_client(self) -> SiteClient:
        site_url = (self._site_url or DEFAULT_SITE_URL).rstrip("/")
        # 注意：RequestUtils 传入 headers 后不再注入 accept_type/referer，需在此显式给出，
        # cf_clearance 与防盗链都依赖 UA/Referer 与浏览器一致。
        headers = {
            "User-Agent": self._user_agent or _DEFAULT_UA,
            "Accept": _ACCEPT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{site_url}/",
        }

        def fetcher(url: str) -> Optional[HttpResult]:
            try:
                response = RequestUtils(
                    headers=headers,
                    cookies=self._cookie,
                    timeout=30,
                    verify=False,
                    use_session=True,
                ).get_res(url)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"名称辅种：站点请求异常：{err}")
                return None
            if not response:
                return None
            return HttpResult(
                status=response.status_code,
                content=response.content or b"",
                url=str(response.url),
            )

        return SiteClient(
            fetcher=fetcher,
            base_url=site_url,
            interval=self._interval,
            stopped=lambda: self._stop_event.is_set(),
        )
