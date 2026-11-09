"""HDWinG 辅种插件（MoviePilot V3）。

扫描下载器里已完成的种子，在 HDWinG 站内搜索同源种子并添加到下载器辅种：
- 同 info_hash 直接命中，qBittorrent 可跳过校验秒做种
- 跨站同源按文件清单（文件名 + 字节数）逐一比对，全部一致才添加
- 每次运行有请求预算与节流，避免触发站点频率限制
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.schemas.types import EventType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper

from .hdwing import (
    DEFAULT_SITE_URL,
    DEFAULT_USER_AGENT,
    HDWinGClient,
    HttpResult,
    build_search_key,
    parse_torrent,
)
from .rank import rank_candidates, size_diff_ratio

_CACHE_KEY = "cache"
_HISTORY_KEY = "history"
_STATS_KEY = "stats"
_MAX_HISTORY = 80
_CMD_ACTION = "hdwing_crossseed_run"
_ONCE_JOB_ID = "HDWinGCrossSeed.RunOnce"
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


class HDWinGCrossSeed(_PluginBase):
    """HDWinG 站内辅种插件。"""

    plugin_name = "HDWinG 辅种"
    plugin_desc = (
        "扫描下载器已完成的种子，在 HDWinG 搜索同源种子并添加辅种；"
        "同 hash 或文件清单完全一致才添加，支持定时执行、请求节流与结果通知。"
    )
    plugin_icon = (
        "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/crossseed.png"
    )
    plugin_version = "1.0.0"
    plugin_label = "站点,辅种,HDWinG"
    plugin_author = "BackspaceM"
    plugin_config_prefix = "hdwingcrossseed_"
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
    _max_requests: int = 40
    _max_tasks: int = 20
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
        self._max_requests = 40
        self._max_tasks = 20
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
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._cron = str(config.get("cron") or "0 4 * * *").strip()
        self._downloaders = _to_str_list(config.get("downloaders"))
        self._site_url = str(config.get("site_url") or DEFAULT_SITE_URL).strip().rstrip("/")
        self._cookie = str(config.get("cookie") or "").strip()
        self._user_agent = str(config.get("user_agent") or _DEFAULT_UA).strip()
        self._interval = max(_to_float(config.get("interval"), 6.0), 0.0)
        self._max_requests = max(_to_int(config.get("max_requests"), 40), 1)
        self._max_tasks = max(_to_int(config.get("max_tasks"), 20), 1)
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
                logger.debug(f"HDWinG 辅种：取消待执行调度任务失败 {err}")
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
                "id": "HDWinGCrossSeed",
                "name": "HDWinG 辅种",
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
                "cmd": "/hdwing_seed",
                "event": EventType.PluginAction,
                "desc": "立即执行一次 HDWinG 辅种",
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
            self.post_message(title="HDWinG 辅种", text="插件未启用，命令已忽略")
            return
        if not self._cookie:
            self.post_message(title="HDWinG 辅种", text="未配置站点 Cookie，无法执行")
            return
        if not self.start_run(trigger="命令"):
            self.post_message(title="HDWinG 辅种", text="上一轮任务尚未结束，请稍后再试")

    # ------------------------------------------------------------------
    # 表单
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置表单与默认值。"""
        try:
            downloader_items = [
                {"title": name, "value": name}
                for name in (DownloaderHelper().get_configs() or {}).keys()
            ]
        except Exception as err:  # noqa: BLE001 - 未配置下载器时表单仍需可渲染
            logger.debug(f"HDWinG 辅种：读取下载器列表失败 {err}")
            downloader_items = []
        content: List[dict] = [
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {"model": "notify", "label": "运行结果通知"}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "cron", "label": "执行周期（cron）",
                        "placeholder": "0 4 * * *",
                        "hint": "默认每天 04:00，按容器时区解释", "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "cache_days", "label": "失败重试间隔（天）",
                        "placeholder": "7", "type": "number",
                        "hint": "未匹配/失败的种子在该天数内不再重试",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [{"component": "VSelect", "props": {
                        "model": "downloaders", "label": "参与辅种的下载器",
                        "multiple": True, "chips": True, "clearable": True,
                        "items": downloader_items,
                        "hint": "不选则使用全部已启用的下载器", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 4, [{"component": "VTextField", "props": {
                        "model": "site_url", "label": "站点地址",
                        "placeholder": DEFAULT_SITE_URL}}]),
                    self._col(12, 4, [{"component": "VTextField", "props": {
                        "model": "user_agent", "label": "User-Agent",
                        "placeholder": _DEFAULT_UA,
                        "hint": "需与浏览器提取 cookie 时一致，否则 cf_clearance 可能失效",
                        "persistentHint": True}}]),
                    self._col(12, 4, [{"component": "VTextField", "props": {
                        "model": "interval", "label": "请求间隔（秒）",
                        "placeholder": "6", "type": "number",
                        "hint": "站点有频率限制，建议不低于 5 秒",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [{"component": "VTextarea", "props": {
                        "model": "cookie", "label": "站点 Cookie",
                        "rows": 4,
                        "placeholder": "在此粘贴自己的 Cookie：hdchina=<你的值>; PHPSESSID=<你的值>; cf_clearance=<你的值>",
                        "hint": "从浏览器开发者工具复制完整 cookie 串；含 cf_clearance 时需保持 IP 与 UA 不变。Cookie 属于个人凭据，仅存本机配置，不要提交到仓库或分享",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "max_tasks", "label": "每轮最多处理任务数",
                        "placeholder": "20", "type": "number"}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "max_requests", "label": "每轮请求上限",
                        "placeholder": "40", "type": "number",
                        "hint": "搜索与下载种子都计入，耗尽后本轮停止",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "max_candidates", "label": "每个任务最多尝试种子数",
                        "placeholder": "3", "type": "number"}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "min_score", "label": "名称相似度阈值",
                        "placeholder": "0.6", "type": "number",
                        "hint": "0.1~1.0，越大越严格", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 4, [{"component": "VSelect", "props": {
                        "model": "match_mode", "label": "匹配模式",
                        "items": [
                            {"title": "严格：同 hash 或文件清单完全一致", "value": "strict"},
                            {"title": "宽松：额外允许总体积接近即可", "value": "loose"},
                        ],
                        "hint": "宽松模式可能把内容不同的版本加进来，需自行承担校验失败风险",
                        "persistentHint": True}}]),
                    self._col(12, 4, [{"component": "VTextField", "props": {
                        "model": "size_tolerance", "label": "宽松模式体积容差（%）",
                        "placeholder": "2.0", "type": "number"}}]),
                    self._col(12, 4, [{"component": "VTextField", "props": {
                        "model": "min_size_gb", "label": "最小种子体积（GB）",
                        "placeholder": "0", "type": "number",
                        "hint": "0 表示不限制", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "skip_checking", "label": "qB 跳过校验",
                        "hint": "仅在匹配通过时生效，可秒做种", "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "add_paused", "label": "添加后暂停"}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "tag", "label": "新增任务标签", "placeholder": "辅种"}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "skip_tags", "label": "跳过含这些标签的任务",
                        "placeholder": "辅种",
                        "hint": "逗号分隔", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [{"component": "VTextarea", "props": {
                        "model": "skip_paths", "label": "跳过的保存路径前缀",
                        "rows": 2, "placeholder": "/downloads/临时\n/downloads/待整理",
                        "hint": "每行一个前缀，命中即跳过该任务", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [{"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": "辅种判定：① 下载的站点种子 info_hash 与本地任务一致；"
                                "② 跨站同源时逐文件比对「文件名 + 字节数」全部一致。"
                                "两者都不满足时不添加，避免把不同版本抢来做种导致校验失败。"
                                "站点请求按「请求间隔」串行发送，命中频率限制会自动退避重试。"}}]),
                ],
            },
        ]
        return [{"component": "VForm", "content": content}], {
            "enabled": False,
            "notify": True,
            "cron": "0 4 * * *",
            "downloaders": [],
            "site_url": DEFAULT_SITE_URL,
            "cookie": "",
            "user_agent": _DEFAULT_UA,
            "interval": 6,
            "max_requests": 40,
            "max_tasks": 20,
            "max_candidates": 3,
            "min_score": 0.6,
            "size_tolerance": 2.0,
            "match_mode": "strict",
            "skip_checking": True,
            "add_paused": False,
            "tag": "辅种",
            "skip_tags": "辅种",
            "skip_paths": "",
            "min_size_gb": 0,
            "cache_days": 7,
        }

    @staticmethod
    def _col(cols: int, md: int, content: List[dict]) -> dict:
        return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": content}

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------

    def get_page(self) -> List[dict]:
        """返回插件详情页。"""
        if not self._enabled:
            return [
                {"component": "VAlert", "props": {
                    "type": "warning", "variant": "tonal",
                    "text": "插件未启用，请先在配置中启用并填写站点 Cookie。"}}
            ]
        stats = self.get_data(_STATS_KEY) or {}
        history = self.get_data(_HISTORY_KEY) or []
        cache = self.get_data(_CACHE_KEY) or {}
        pending = len([item for item in cache.values() if item.get("r") in ("nomatch", "failed")])
        content: List[dict] = [
            {
                "component": "VRow",
                "content": [
                    self._stat_card("累计辅种", str(stats.get("total_added", 0)), "mdi-content-duplicate"),
                    self._stat_card("累计失败", str(stats.get("total_failed", 0)), "mdi-alert-circle-outline"),
                    self._stat_card("上次运行", str(stats.get("last_duration", "-")) + "s", "mdi-timer-outline"),
                    self._stat_card("待重试缓存", str(pending), "mdi-cached"),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [
                        {"component": "VAlert", "props": {
                            "type": "info", "variant": "tonal", "class": "mt-2",
                            "text": (f"上次运行：{stats.get('last_run_text', '尚未运行')}　"
                                     f"触发：{stats.get('last_trigger', '-')}　"
                                     f"扫描 {stats.get('last_scanned', 0)} / 命中 {stats.get('last_matched', 0)} / "
                                     f"新增 {stats.get('last_added', 0)} / 失败 {stats.get('last_failed', 0)}"
                                     + (f"　说明：{stats.get('last_message')}" if stats.get("last_message") else ""))}}
                    ]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [
                        {"component": "VBtn", "props": {
                            "variant": "tonal", "color": "primary",
                            "prepend-icon": "mdi-play", "class": "ma-1"},
                         "text": "立即辅种",
                         "events": {"click": {"api": f"plugin/{self.__class__.__name__}/run", "method": "post"}}},
                        {"component": "VBtn", "props": {
                            "variant": "tonal", "prepend-icon": "mdi-account-check", "class": "ma-1"},
                         "text": "检测 Cookie",
                         "events": {"click": {"api": f"plugin/{self.__class__.__name__}/check", "method": "post"}}},
                        {"component": "VBtn", "props": {
                            "variant": "tonal", "color": "warning",
                            "prepend-icon": "mdi-delete-sweep", "class": "ma-1"},
                         "text": "清空缓存",
                         "events": {"click": {"api": f"plugin/{self.__class__.__name__}/clear_cache", "method": "post"}}},
                    ]),
                ],
            },
        ]
        if not self._cookie:
            content.append({"component": "VAlert", "props": {
                "type": "error", "variant": "tonal", "class": "mt-2",
                "text": "未配置站点 Cookie，无法访问 HDWinG。"}})
        if not history:
            content.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-2",
                "text": "暂无运行记录，点击「立即辅种」或等待定时任务执行。"}})
            return content
        headers = ("时间", "下载器", "本地任务", "站点种子", "结果", "说明")
        rows = []
        for item in history[:_MAX_HISTORY]:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-caption"}, "text": item.get("time", "")},
                    {"component": "td", "props": {"class": "text-caption"}, "text": item.get("downloader", "")},
                    {"component": "td", "props": {"class": "text-caption"}, "text": item.get("local", "")},
                    {"component": "td", "props": {"class": "text-caption"}, "text": item.get("site", "-")},
                    {"component": "td", "text": _STATE_TEXT.get(item.get("state", ""), item.get("state", ""))},
                    {"component": "td", "props": {"class": "text-caption"}, "text": item.get("message", "")},
                ],
            })
        content.append({
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mt-3"},
            "content": [
                {"component": "VCardTitle", "text": "最近记录"},
                {"component": "VTable", "props": {"density": "compact", "hover": True},
                 "content": [
                     {"component": "thead", "content": [
                         {"component": "tr", "content": [
                             {"component": "th", "text": text} for text in headers]}]},
                     {"component": "tbody", "content": rows},
                 ]},
            ],
        })
        return content

    def _stat_card(self, title: str, value: str, icon: str) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [
                {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                    {"component": "VCardText", "props": {"class": "d-flex align-center"}, "content": [
                        {"component": "VIcon", "props": {"icon": icon, "size": 30,
                                                         "color": "primary", "class": "me-3"}},
                        {"component": "div", "content": [
                            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                             "text": title},
                            {"component": "div", "props": {"class": "text-h6 font-weight-bold"},
                             "text": value},
                        ]},
                    ]},
                ]},
            ],
        }

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

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
            message="已开始执行，稍后刷新页面查看结果" if started else "任务启动失败",
        )

    def api_check(self) -> schemas.Response:
        """检测 cookie 是否有效。"""
        if not self._cookie:
            return schemas.Response(success=False, message="未配置站点 Cookie")
        ok, message = self._build_client().check_login()
        return schemas.Response(success=ok, message=message)

    def api_clear_cache(self) -> schemas.Response:
        """清空重试缓存，便于下次重新扫描。"""
        self.save_data(_CACHE_KEY, {})
        return schemas.Response(success=True, message="缓存已清空")

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
                    logger.info("HDWinG 辅种：上一轮尚未结束，本次跳过")
                    return False
                logger.warning("HDWinG 辅种：上一轮运行状态疑似残留，强制重置后继续")
            self._running = True
            self._run_started = time.time()
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is None:
            self._stop_event = stop_event = threading.Event()
        stop_event.clear()

        add_once = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if callable(add_once):
            try:
                scheduled = add_once(
                    self.__class__.__name__,
                    _ONCE_JOB_ID,
                    self._run_wrapper,
                    "HDWinG 辅种执行",
                    delay_seconds=0,
                    func_kwargs={"trigger": trigger},
                )
            except Exception as err:  # noqa: BLE001 - 调度器异常时退回自建线程
                logger.warning(f"HDWinG 辅种：提交调度任务失败，改为后台线程执行 {err}")
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
            logger.error(f"HDWinG 辅种执行异常：{err} - {traceback.format_exc()}")
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
        started = time.time()
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is None:
            self._stop_event = stop_event = threading.Event()
        stop_event.clear()
        result: Dict[str, Any] = {
            "trigger": trigger,
            "scanned": 0, "matched": 0, "added": 0, "failed": 0, "skipped": 0,
            "message": "", "records": [],
        }
        if not self._cookie:
            result["message"] = "未配置站点 Cookie"
            self._save_result(result, started)
            return
        client = self._build_client()
        ok, message = client.check_login()
        if not ok:
            result["message"] = f"Cookie 检测失败：{message}"
            self._save_result(result, started)
            self._notify_result(result)
            return

        self._budget = self._max_requests
        self._search_cache = {}
        cache = dict(self.get_data(_CACHE_KEY) or {})
        services = self._services()
        if not services:
            result["message"] = "没有可用的下载器"
            self._save_result(result, started)
            return

        remaining = self._max_tasks
        for downloader, service in services.items():
            if self._stop_event.is_set() or remaining <= 0 or self._budget <= 0:
                break
            torrents = self._completed_torrents(service)
            if torrents is None:
                result["message"] = f"{downloader} 读取任务失败"
                logger.warning(f"HDWinG 辅种：读取下载器 {downloader} 的完成任务失败")
                continue
            views: List[_TorrentView] = []
            for item in torrents:
                view = _view_of(item)
                if not view or not self._eligible(view, cache, downloader):
                    continue
                views.append(view)
            views.sort(key=lambda v: v.added_on, reverse=True)
            for view in views[:remaining]:
                if self._stop_event.is_set() or self._budget <= 0:
                    break
                remaining -= 1
                result["scanned"] += 1
                state, message, site_title = self._handle(downloader, service, view, client)
                if state in ("added", "exists"):
                    result["matched"] += 1
                if state == "added":
                    result["added"] += 1
                elif state == "failed":
                    result["failed"] += 1
                elif state == "skipped":
                    result["skipped"] += 1
                self._remember(cache, downloader, view, state, message)
                result["records"].append({
                    "downloader": downloader,
                    "local": view.name[:80],
                    "site": site_title[:80] if site_title else "-",
                    "state": state,
                    "message": message[:160],
                })
        self.save_data(_CACHE_KEY, cache)
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
                logger.info(f"HDWinG 辅种：跳过不支持的下载器 {name}（{service.type}）")
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
            logger.warning(f"HDWinG 辅种：读取完成任务异常 {err}")
            return None
        if completed is None:
            return None
        if getattr(service, "type", "") != "transmission":
            return completed
        try:
            all_torrents, error = service.instance.get_torrents()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"HDWinG 辅种：读取 Transmission 全量任务异常 {err}")
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
        self, downloader: str, service: Any, view: _TorrentView, client: HDWinGClient
    ) -> Tuple[str, str, str]:
        """处理单个本地任务，返回（状态，说明，站点种子标题）。"""
        primary, fallback = build_search_key(view.name)
        if not primary:
            return "skipped", "无法从名称提取搜索词", ""
        results = self._search(client, primary, fallback)
        if not results:
            return "nomatch", "站点无搜索结果", ""
        ranked = rank_candidates(view.name, view.size, results, self._min_score)
        if not ranked:
            return "nomatch", f"{len(results)} 条结果名称均不匹配", ""
        tried = 0
        last = "候选种子文件不一致"
        for score, diff, candidate in ranked:
            if self._stop_event.is_set() or self._budget <= 0:
                break
            if tried >= self._max_candidates:
                break
            tried += 1
            content = self._download(client, candidate)
            if not content:
                continue
            meta = parse_torrent(content)
            if not meta:
                continue
            mode = self._judge(service, view, meta)
            if not mode:
                last = f"{candidate.title[:50]} 文件不一致"
                continue
            if self._exists(service, meta.info_hash):
                return "exists", f"站点种子已在下载器（{mode}）", candidate.title
            ok, message = self._add(service, view, content)
            if ok:
                logger.info(
                    f"HDWinG 辅种成功：{view.name} -> {candidate.title}（{mode}，{diff * 100:.2f}% 体积差）"
                )
                return "added", f"匹配方式 {mode}，体积差 {diff * 100:.2f}%", candidate.title
            last = message
        return "failed", last, ""

    def _search(self, client: HDWinGClient, primary: str, fallback: str) -> List[Any]:
        """带轮内缓存与预算控制的搜索。"""
        for keyword in [key for key in (primary, fallback) if key]:
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

    def _download(self, client: HDWinGClient, candidate: Any) -> Optional[bytes]:
        if self._budget <= 0:
            return None
        self._budget -= 1
        return client.download(candidate)

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
            logger.warning(f"HDWinG 辅种：读取文件清单失败 {err}")
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
            logger.error(f"HDWinG 辅种：添加种子异常 {err}")
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
            "last_matched": result.get("matched", 0),
            "last_added": result.get("added", 0),
            "last_failed": result.get("failed", 0),
            "last_duration": duration,
            "last_message": result.get("message", ""),
            "total_added": _to_int(stats.get("total_added")) + result.get("added", 0),
            "total_failed": _to_int(stats.get("total_failed")) + result.get("failed", 0),
            "total_scanned": _to_int(stats.get("total_scanned")) + result.get("scanned", 0),
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
        title = "HDWinG 辅种完成" if added else "HDWinG 辅种未新增"
        lines = [
            f"扫描 {result.get('scanned', 0)} 个已完成任务，"
            f"新增 {added}，失败 {failed}，跳过 {result.get('skipped', 0)}",
        ]
        if result.get("message"):
            lines.append(str(result["message"]))
        for record in (result.get("records") or [])[:5]:
            if record.get("state") == "added":
                lines.append(f"· {record.get('local', '')[:50]} → {record.get('site', '')[:50]}")
        try:
            self.post_message(title=title, text="\n".join(lines))
        except Exception as err:  # noqa: BLE001 - 通知失败不影响主流程
            logger.warning(f"HDWinG 辅种：发送通知失败 {err}")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _site_host(self) -> str:
        base = (self._site_url or DEFAULT_SITE_URL).lower()
        base = base.split("://")[-1].split("/")[0]
        return base

    def _build_client(self) -> HDWinGClient:
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
                logger.warning(f"HDWinG 请求异常：{err}")
                return None
            if not response:
                return None
            return HttpResult(
                status=response.status_code,
                content=response.content or b"",
                url=str(response.url),
            )

        return HDWinGClient(
            fetcher=fetcher,
            base_url=site_url,
            interval=self._interval,
            stopped=lambda: self._stop_event.is_set(),
        )
