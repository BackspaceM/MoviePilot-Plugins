"""媒体库精准刷新（MoviePilot V3 插件）。

与 MoviePilot 内置的「按剧集/库目录刷新、匹配不到就全库扫描」不同，本插件：
1. 监听整理完成 / 字幕完成 / 删除事件，提取真实文件路径；
2. 按每台服务器配置的路径映射，把 MP 容器路径换算成媒体服务器侧路径；
3. Emby/Jellyfin 用 `POST /Library/Media/Updated` 做单文件级增量刷新（支持
   Created/Modified/Deleted），Plex 用 `/library/sections/{key}/refresh?path=` 局部扫描；
4. 精准失败时按「父目录 → 库级 → 全库」分级回退，失败项带指数退避重试；
5. 合并窗口去重（短时间的批量入库合并成一次请求），定时对账兜底；
6. 关键节点全部写 INFO 级宿主日志，MP「查看日志」窗口通过 SSE 实时推送，
   详情页另有逐服务器明细、待刷新队列与运行日志。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import html as _html
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Sequence, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.schemas.types import EventType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

try:
    from app.schemas.types import MessageType as _MessageType
    _PLUGIN_MTYPE = getattr(_MessageType, "Plugin", None)
except Exception:  # noqa: BLE001 - 旧宿主或离线桩环境没有该枚举
    _PLUGIN_MTYPE = None

from .clients import ServerResult, build_client
from .core import (
    CREATED,
    DELETED,
    DEFAULT_MEDIA_EXTS,
    DEFAULT_SUBTITLE_EXTS,
    FALLBACK_LEVELS,
    MODIFIED,
    NOTIFY_MODES,
    RefreshItem,
    ServerConfig,
    debounce_delay,
    dedupe_items,
    extract_event_paths,
    filter_paths,
    format_items,
    make_items,
    mask_secrets,
    next_attempt,
    normalize_path,
    now_text,
    parse_extensions,
    parse_servers,
    retry_delay,
    should_force_dispatch,
    validate_cron,
)

_QUEUE_KEY = "refresh_queue"
_QUEUE_META_KEY = "refresh_queue_meta"
_LOG_KEY = "run_logs"
_STATS_KEY = "stats"
_HISTORY_KEY = "history"
_CHECK_KEY = "server_check"
_LIVE_KEY = "live"
_MAX_LOGS = 300
_MAX_HISTORY = 40
_ONCE_JOB_ID = "MediaLibraryRefresh.Debounced"
_CMD_RUN = "mlr_run"
_CMD_CHECK = "mlr_check"
_CMD_QUEUE = "mlr_queue"
_CMD_ALL = "mlr_all"
_CMD_STOP = "mlr_stop"
_LOG_PREFIX = "媒体库精准刷新"

_MEDIA_TYPES = ("emby", "jellyfin", "plex")


def _cfg_attr(cfg: Any, *names: str) -> Any:
    """兼容 dict / 对象两种媒体服务器配置载体读取字段。"""
    for name in names:
        value = cfg.get(name) if isinstance(cfg, dict) else getattr(cfg, name, None)
        if value not in (None, ""):
            return value
    return None


_TRANSFER_EVENTS = ("transfer.complete",)
_SUBTITLE_EVENTS = ("transfer.subtitle.complete",)
_DELETE_EVENTS = ("downloadfile.deleted", "download.deleted", "history.deleted")


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _event_type_name(event: Any) -> str:
    value = getattr(event, "event_type", "")
    return str(getattr(value, "value", value) or "").lower()


class MediaLibraryRefresh(_PluginBase):
    """Emby / Jellyfin / Plex 精准刷新插件。"""

    plugin_name = "媒体库精准刷新"
    plugin_desc = "监听整理入库事件，按文件级增量刷新 Emby/Jellyfin/Plex，支持多服务器、路径映射、合并去重与分级回退"
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/medialibraryrefresh.png"
    plugin_version = "1.1.0"
    plugin_label = "媒体库,Emby,Plex,Jellyfin,刷新"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "medialibraryrefresh_"
    plugin_order = 78

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 运行态全部为实例属性，兼容虚拟分身
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._enabled = False
        self._servers: List[ServerConfig] = []
        self._server_errors: List[str] = []
        self._notify_mode = "全部"
        self._dry_run = False
        self._cron = "0 5 * * *"
        self._servers_text = ""
        self._media_exts: Tuple[str, ...] = tuple(DEFAULT_MEDIA_EXTS)
        self._subtitle_exts: Tuple[str, ...] = tuple(DEFAULT_SUBTITLE_EXTS)
        self._exclude_keywords: List[str] = []
        self._merge_window = 20.0
        self._max_wait = 120.0
        self._batch_size = 200
        self._max_retries = 3
        self._retry_base = 30.0
        self._fallback = "parent"
        self._request_timeout = 8.0
        self._verify_ssl = False
        self._on_transfer = True
        self._on_subtitle = True
        self._on_delete = False
        self._scheduled_full_refresh = False
        self._log_paths = True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """载入配置并复位运行态；允许重复调用。"""
        self.stop_service()
        self._enabled = False
        self._notify_mode = "全部"
        self._dry_run = False
        self._cron = "0 5 * * *"
        self._servers_text = ""
        self._servers, self._server_errors = [], []
        self._media_exts = tuple(DEFAULT_MEDIA_EXTS)
        self._subtitle_exts = tuple(DEFAULT_SUBTITLE_EXTS)
        self._exclude_keywords = []
        self._merge_window = 20.0
        self._max_wait = 120.0
        self._batch_size = 200
        self._max_retries = 3
        self._retry_base = 30.0
        self._fallback = "parent"
        self._request_timeout = 8.0
        self._verify_ssl = False
        self._on_transfer = True
        self._on_subtitle = True
        self._on_delete = False
        self._scheduled_full_refresh = False
        self._log_paths = True
        if not config:
            self._log("INFO", "未载入配置，插件保持停用")
            return

        self._enabled = bool(config.get("enabled"))
        mode = str(config.get("notify_mode") or "").strip()
        self._notify_mode = mode if mode in NOTIFY_MODES else "全部"
        self._dry_run = bool(config.get("dry_run"))
        cron = str(config.get("cron") or "0 5 * * *").strip()
        valid, normalized = validate_cron(cron)
        self._cron = normalized if valid else "0 5 * * *"
        self._servers_text = str(config.get("servers") or "")
        combined, auto_errors = self._merge_server_config(config)
        self._servers, self._server_errors = parse_servers(combined)
        self._server_errors = auto_errors + self._server_errors
        self._media_exts = parse_extensions(config.get("media_exts"), DEFAULT_MEDIA_EXTS)
        self._exclude_keywords = [
            normalize_path(part) for part in str(config.get("exclude_keywords") or "").replace(",", "\n").splitlines()
            if part.strip()
        ]
        self._merge_window = min(max(_to_float(config.get("merge_window"), 20.0), 1.0), 600.0)
        self._max_wait = min(max(_to_float(config.get("max_wait"), 120.0), 5.0), 3600.0)
        self._batch_size = min(max(_to_int(config.get("batch_size"), 200), 1), 2000)
        self._max_retries = min(max(_to_int(config.get("max_retries"), 3), 1), 10)
        self._retry_base = min(max(_to_float(config.get("retry_base"), 30.0), 1.0), 300.0)
        fallback = str(config.get("fallback_level") or "parent").strip().lower()
        self._fallback = fallback if fallback in FALLBACK_LEVELS else "parent"
        self._request_timeout = min(max(_to_float(config.get("request_timeout"), 8.0), 1.0), 60.0)
        self._verify_ssl = bool(config.get("verify_ssl"))
        self._on_transfer = bool(config.get("on_transfer", True))
        self._on_subtitle = bool(config.get("on_subtitle", True))
        self._on_delete = bool(config.get("on_delete"))
        self._scheduled_full_refresh = bool(config.get("scheduled_full_refresh"))
        self._log_paths = bool(config.get("log_paths", True))

        enabled_servers = [s for s in self._servers if s.enabled]
        self._log(
            "INFO",
            f"配置已载入 v{self.plugin_version}：启用={self._enabled}，服务器 {len(enabled_servers)} 台，"
            f"合并窗口={self._merge_window:.0f}s，最大等待={self._max_wait:.0f}s，"
            f"批量={self._batch_size}，回退={self._fallback}，周期={self._cron}，试运行={self._dry_run}",
        )
        for error in self._server_errors[:5]:
            self._log("WARN", f"服务器配置：{error}")
        if not enabled_servers:
            self._log("WARN", "未配置任何启用的媒体服务器，事件将只入队不刷新")
        if config.get("run_now"):
            reset = dict(config)
            reset["run_now"] = False
            try:
                self.update_config(reset)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"{_LOG_PREFIX}：复位立即执行开关失败 {err}")
            if not self._enabled:
                self._log("WARN", "插件未启用，已忽略「立即执行一次」")
                self._send_message("媒体库精准刷新", "插件未启用，请先启用后再执行")
            elif self.start_run(trigger="配置页", prefer_thread=True):
                self._log("INFO", "配置页触发「立即执行一次」（独立线程）")
            else:
                self._log("WARN", "配置页触发被跳过：已有任务在运行")

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """设置中止信号并取消待执行的一次性任务。"""
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remove_once):
            try:
                remove_once(self.__class__.__name__, _ONCE_JOB_ID)
            except Exception as err:  # noqa: BLE001
                logger.debug(f"{_LOG_PREFIX}：取消一次性任务失败 {err}")
        thread = getattr(self, "_thread", None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        with self._state_lock:
            self._running = False

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时对账服务。"""
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except (ValueError, TypeError):
            trigger = CronTrigger.from_crontab("0 5 * * *")
        return [{
            "id": "MediaLibraryRefresh",
            "name": "媒体库精准刷新 · 定时对账",
            "trigger": trigger,
            "func": self.run_scheduled,
            "kwargs": {},
            "func_kwargs": {},
        }]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {"cmd": "/mlr_run", "event": EventType.PluginAction,
             "desc": "立即刷新待处理队列（媒体库精准刷新）", "category": "媒体库",
             "data": {"action": _CMD_RUN}},
            {"cmd": "/mlr_check", "event": EventType.PluginAction,
             "desc": "检测所有媒体服务器连通与媒体库", "category": "媒体库",
             "data": {"action": _CMD_CHECK}},
            {"cmd": "/mlr_queue", "event": EventType.PluginAction,
             "desc": "查看待刷新队列", "category": "媒体库",
             "data": {"action": _CMD_QUEUE}},
            {"cmd": "/mlr_all", "event": EventType.PluginAction,
             "desc": "对所有媒体服务器发起全库刷新", "category": "媒体库",
             "data": {"action": _CMD_ALL}},
            {"cmd": "/mlr_stop", "event": EventType.PluginAction,
             "desc": "中止正在执行的刷新任务", "category": "媒体库",
             "data": {"action": _CMD_STOP}},
        ]

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event) -> None:
        """远程命令入口，按来源渠道回复。"""
        data = getattr(event, "event_data", None) or {}
        action = data.get("action")
        channel, userid = data.get("channel"), data.get("userid")
        if action == _CMD_RUN:
            ok = self.start_run(trigger="远程命令", prefer_thread=True)
            self._send_message("媒体库精准刷新", "已触发队列刷新" if ok else "已有任务在运行，已忽略",
                               channel=channel, userid=userid)
        elif action == _CMD_CHECK:
            results = self._check_servers_sync()
            self._send_message("媒体库服务器检测", "\n".join(results) or "未配置服务器",
                               channel=channel, userid=userid)
        elif action == _CMD_QUEUE:
            queue = self._load_queue()
            text = format_items(queue, 15) if queue else "待刷新队列为空"
            self._send_message("媒体库刷新队列", f"共 {len(queue)} 项\n{text}",
                               channel=channel, userid=userid)
        elif action == _CMD_ALL:
            ok = self.start_run(trigger="远程全库刷新", full_refresh=True, prefer_thread=True)
            self._send_message("媒体库全库刷新", "已触发全库刷新" if ok else "已有任务在运行，已忽略",
                               channel=channel, userid=userid)
        elif action == _CMD_STOP:
            self._stop_event.set()
            self._send_message("媒体库精准刷新", "已发送中止信号", channel=channel, userid=userid)
        else:
            return

    @eventmanager.register([
        EventType.TransferComplete,
        EventType.SubtitleTransferComplete,
        EventType.DownloadFileDeleted,
        EventType.DownloadDeleted,
    ])
    def on_transfer_event(self, event: Event) -> None:
        """整理/字幕/删除事件 → 入队并按合并窗口调度。"""
        if not self._enabled:
            return
        name = _event_type_name(event)
        if name in _SUBTITLE_EVENTS:
            if not self._on_subtitle:
                return
            update_type, allowed = MODIFIED, tuple(self._subtitle_exts) + tuple(self._media_exts)
        elif name in _DELETE_EVENTS:
            if not self._on_delete:
                return
            update_type, allowed = DELETED, tuple(self._media_exts)
        elif name in _TRANSFER_EVENTS:
            if not self._on_transfer:
                return
            update_type, allowed = CREATED, tuple(self._media_exts)
        else:
            return
        data = getattr(event, "event_data", None) or {}
        paths = extract_event_paths(data)
        paths = filter_paths(paths, allowed, self._exclude_keywords, allow_dirs=True)
        if not paths:
            return
        items = make_items(paths, update_type, source=name)
        self._log("INFO", f"收到 {name} 事件，提取 {len(items)} 个路径："
                          + ("、".join(p for p in paths[:3]) if self._log_paths else "（路径日志已关闭）"))
        self._enqueue(items)

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------

    @staticmethod
    def _col(cols: int, md: int, content: List[dict]) -> dict:
        return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": content}

    # ------------------------------------------------------------------
    # 已连接媒体服务器（MoviePilot 服务门面）
    # ------------------------------------------------------------------

    @staticmethod
    def _connected_media_configs() -> Dict[str, Any]:
        """读取 MoviePilot 中已连接的媒体服务器配置。"""
        try:
            from app.sdk.services import MediaServerHelper
            configs = MediaServerHelper().get_configs() or {}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}：读取已连接媒体服务器失败：{err}")
            return {}
        out: Dict[str, Any] = {}
        for key, cfg in (configs.items() if isinstance(configs, dict) else []):
            stype = str(_cfg_attr(cfg, "type", "server_type") or "").lower()
            if stype in _MEDIA_TYPES:
                out[str(key)] = cfg
        return out

    def _connected_items(self) -> List[Dict[str, str]]:
        """生成配置页「已连接媒体服务器」下拉项。"""
        items: List[Dict[str, str]] = []
        for name, cfg in self._connected_media_configs().items():
            stype = str(_cfg_attr(cfg, "type", "server_type") or "").lower()
            items.append({"title": f"{name}（{stype}）", "value": name})
        return items

    def _selected_server_lines(self, selected: Any) -> Tuple[List[str], List[str]]:
        """把下拉勾选的已连接实例转换成 parse_servers 可识别的配置行。"""
        lines: List[str] = []
        errors: List[str] = []
        configs = self._connected_media_configs()
        raw = selected if isinstance(selected, (list, tuple, set)) else (
            [selected] if isinstance(selected, str) and selected.strip() else [])
        for name in [str(n) for n in raw if str(n).strip()]:
            cfg = configs.get(name)
            if cfg is None:
                errors.append(f"已连接实例「{name}」当前不存在，请重新勾选或改到手动补充框")
                continue
            stype = str(_cfg_attr(cfg, "type", "server_type") or "").lower()
            url = _cfg_attr(cfg, "url", "address", "host", "base_url")
            token = _cfg_attr(cfg, "apikey", "api_key", "token", "access_token")
            if not url or not token:
                errors.append(f"已连接实例「{name}」未暴露地址或密钥，请在手动补充框按格式补全")
                continue
            lines.append(f"{stype}|{name}|{url}|{token}||1")
        return lines, errors

    def _merge_server_config(self, config: Dict[str, Any]) -> Tuple[str, List[str]]:
        """合并下拉选择与手动配置；手动同名配置优先（用于指定路径映射）。"""
        manual_text = str(config.get("servers") or "")
        auto_lines, errors = self._selected_server_lines(config.get("connected_servers"))
        manual_names = set()
        for raw in manual_text.splitlines():
            line = raw.strip()
            if line.startswith("#") or line.count("|") < 3:
                continue
            parts = line.split("|")
            if len(parts) > 1 and parts[1].strip():
                manual_names.add(parts[1].strip())
        auto_lines = [line for line in auto_lines
                      if line.split("|")[1].strip() not in manual_names]
        return "\n".join([manual_text] + auto_lines).strip(), errors

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        content: List[dict] = [
            {"component": "VRow", "content": [
                self._col(12, 4, [{"component": "VSwitch", "props": {
                    "model": "run_now", "label": "立即执行一次", "color": "primary",
                    "hint": "保存后立即处理待刷新队列，开关会自动复位；也可在详情页点「立即刷新」",
                    "persistentHint": True}}]),
                self._col(12, 4, [{"component": "VSwitch", "props": {
                    "model": "enabled", "label": "启用插件"}}]),
                self._col(12, 4, [{"component": "VSelect", "props": {
                    "model": "notify_mode", "label": "通知方式",
                    "items": [
                        {"title": "全部通知（成功与异常）", "value": "全部"},
                        {"title": "仅异常通知", "value": "仅异常"},
                        {"title": "关闭通知", "value": "关闭"},
                    ],
                    "hint": "走 MoviePilot 全局通知开关与收件人路由，点击可跳回插件页",
                    "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 12, [{"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": "媒体服务器直接在下方下拉勾选 MoviePilot 已连接的实例即可，无需手填地址与密钥；"
                            "只有当需要「路径映射」或实例不在 MP 内时，才在文本框按同名补充一行。"}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 12, [{"component": "VSelect", "props": {
                    "model": "connected_servers", "label": "已连接媒体服务器（可多选）",
                    "items": self._connected_items(), "multiple": True, "chips": True,
                    "clearable": True, "persistentHint": True,
                    "hint": "从 MoviePilot 已连接的 Emby / Jellyfin / Plex 中勾选；地址与密钥自动带入"}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 12, [{"component": "VTextarea", "props": {
                    "model": "servers", "label": "路径映射 / 手动补充媒体服务器（可选）", "rows": 4,
                    "placeholder": "emby|客厅Emby|http://192.168.1.10:8096|API_KEY|/media::/volume1/media|1\n"
                                   "plex|客厅Plex|http://192.168.1.11:32400|PLEX_TOKEN|/media::/mnt/media|1",
                    "hint": "每行一台：类型(emby/jellyfin/plex)|名称|地址|密钥|路径映射|启用(1/0)。"
                            "与上方下拉同名的行会覆盖自动配置，用于指定 MP 容器路径::媒体服务器路径；"
                            "多个映射用分号分隔，# 开头为注释",
                    "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 4, [{"component": "VTextField", "props": {
                    "model": "cron", "label": "定时对账周期（cron）", "placeholder": "0 5 * * *",
                    "hint": "处理到期重试项与遗漏队列；默认每天 05:00", "persistentHint": True}}]),
                self._col(12, 4, [{"component": "VSelect", "props": {
                    "model": "fallback_level", "label": "失败回退策略",
                    "items": [
                        {"title": "不回退（只做精准刷新）", "value": "none"},
                        {"title": "父目录 / 局部目录（推荐）", "value": "parent"},
                        {"title": "全库扫描", "value": "full"},
                    ],
                    "hint": "精准刷新失败时逐级降级，避免入库后媒体库不出现",
                    "persistentHint": True}}]),
                self._col(12, 4, [{"component": "VSwitch", "props": {
                    "model": "scheduled_full_refresh", "label": "定时全库刷新",
                    "hint": "每个周期额外做一次全库扫描，默认关闭（精准刷新不需要）",
                    "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 3, [{"component": "VTextField", "props": {
                    "model": "merge_window", "label": "合并窗口（秒）", "type": "number",
                    "hint": "最后一次事件后静默多久才刷新，批量入库合并成一次请求",
                    "persistentHint": True}}]),
                self._col(12, 3, [{"component": "VTextField", "props": {
                    "model": "max_wait", "label": "最大等待（秒）", "type": "number",
                    "hint": "持续有事件时最迟多久必须执行一次", "persistentHint": True}}]),
                self._col(12, 3, [{"component": "VTextField", "props": {
                    "model": "batch_size", "label": "单轮最多文件数", "type": "number",
                    "hint": "超出的留到下一轮，防止一次请求过大", "persistentHint": True}}]),
                self._col(12, 3, [{"component": "VTextField", "props": {
                    "model": "max_retries", "label": "失败重试次数", "type": "number",
                    "hint": "超过次数后丢弃并告警", "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 3, [{"component": "VTextField", "props": {
                    "model": "retry_base", "label": "重试基准（秒）", "type": "number",
                    "hint": "指数退避：base, 2×base, 4×base…", "persistentHint": True}}]),
                self._col(12, 3, [{"component": "VTextField", "props": {
                    "model": "request_timeout", "label": "请求超时（秒）", "type": "number",
                    "placeholder": "8", "persistentHint": True}}]),
                self._col(12, 3, [{"component": "VSwitch", "props": {
                    "model": "verify_ssl", "label": "校验 HTTPS 证书"}}]),
                self._col(12, 3, [{"component": "VSwitch", "props": {
                    "model": "dry_run", "label": "试运行（不发请求）",
                    "hint": "首次调通建议开启，确认路径映射正确后再关闭",
                    "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 4, [{"component": "VSwitch", "props": {
                    "model": "on_transfer", "label": "监听整理完成事件"}}]),
                self._col(12, 4, [{"component": "VSwitch", "props": {
                    "model": "on_subtitle", "label": "监听字幕完成事件"}}]),
                self._col(12, 4, [{"component": "VSwitch", "props": {
                    "model": "on_delete", "label": "监听删除事件（Deleted）"}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 6, [{"component": "VTextField", "props": {
                    "model": "media_exts", "label": "媒体扩展名白名单",
                    "placeholder": "mkv, mp4, nfo",
                    "hint": "留空使用内置列表；逗号/空格/分号分隔", "persistentHint": True}}]),
                self._col(12, 6, [{"component": "VTextField", "props": {
                    "model": "exclude_keywords", "label": "排除路径关键字",
                    "placeholder": "/tmp, /downloads",
                    "hint": "逗号或换行分隔，命中即跳过", "persistentHint": True}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, 12, [{"component": "VSwitch", "props": {
                    "model": "log_paths", "label": "日志显示映射后路径",
                    "hint": "关闭后只记录数量，适合路径含敏感信息时", "persistentHint": True}}]),
            ]},
        ]
        defaults: Dict[str, Any] = {
            "enabled": False, "run_now": False, "notify_mode": "全部",
            "connected_servers": [], "servers": "", "cron": "0 5 * * *", "fallback_level": "parent",
            "scheduled_full_refresh": False, "merge_window": 20, "max_wait": 120,
            "batch_size": 200, "max_retries": 3, "retry_base": 30,
            "request_timeout": 8, "verify_ssl": False, "dry_run": False,
            "on_transfer": True, "on_subtitle": True, "on_delete": False,
            "media_exts": "", "exclude_keywords": "", "log_paths": True,
        }
        return content, defaults

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    @staticmethod
    def _stat_card(title: str, value: str, icon: str) -> dict:
        return {
            "component": "VCol", "props": {"cols": 12, "md": 3},
            "content": [{"component": "VCard", "props": {"variant": "tonal"},
                         "content": [
                             {"component": "VCardTitle", "props": {"class": "text-subtitle-2"}, "text": title},
                             {"component": "VCardText", "props": {"class": "text-h6"}, "text": value},
                             {"component": "VIcon", "props": {"icon": icon, "size": "small"}},
                         ]}],
        }

    @staticmethod
    def _html_block(title: str, lines: Sequence[str], limit: int = 60) -> dict:
        body = "\n".join(_html.escape(str(item)) for item in list(lines)[:limit]) or "暂无数据"
        return {
            "component": "VCard", "props": {"variant": "tonal", "class": "mt-2"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": title},
                {"component": "VCardText", "props": {"class": "text-caption"},
                 "html": f'<pre style="white-space:pre-wrap;word-break:break-all;margin:0">{body}</pre>'},
            ],
        }

    def _action_button(self, text: str, icon: str, path: str, color: str,
                       method: str = "post") -> dict:
        return {
            "component": "VBtn",
            "props": {"variant": "tonal", "color": color, "size": "small",
                      "class": "me-2 mb-2", "prepend-icon": icon},
            "text": text,
            "events": {"click": {"api": f"plugin/{self.__class__.__name__}{path}", "method": method}},
        }

    def get_page(self) -> List[dict]:
        stats = dict(self.get_data(_STATS_KEY) or {})
        last = dict(stats.get("last_result") or {})
        live = dict(self.get_data(_LIVE_KEY) or {})
        logs = list(self.get_data(_LOG_KEY) or [])
        history = list(self.get_data(_HISTORY_KEY) or [])
        checks = list(self.get_data(_CHECK_KEY) or [])
        queue = self._load_queue()
        content: List[dict] = []
        if not self._enabled:
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mb-2",
                "text": "插件未启用：请先在配置中启用并填写媒体服务器清单。下方按钮仍可用于检测与手动刷新。"}})
        if self._server_errors:
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mb-2",
                "text": "服务器配置有问题：" + "；".join(self._server_errors[:3])}})
        content.append({"component": "VRow", "content": [
            self._stat_card("累计刷新文件", str(stats.get("total_files", 0)), "mdi-file-check-outline"),
            self._stat_card("成功 / 失败轮次", f"{stats.get('ok_runs', 0)} / {stats.get('fail_runs', 0)}",
                            "mdi-check-circle-outline"),
            self._stat_card("待刷新队列", str(len(queue)), "mdi-tray-full"),
            self._stat_card("上轮耗时", f"{stats.get('last_duration', '-')}s", "mdi-timer-outline"),
        ]})
        content.append({"component": "VRow", "content": [self._col(12, 12, [
            {"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-2",
                "text": f"上次运行：{stats.get('last_run_text', '尚未运行')}　触发：{stats.get('last_trigger', '-')}　"
                        f"文件 {last.get('files', 0)} / 服务器 {last.get('servers', 0)} / "
                        f"成功 {last.get('servers_ok', 0)}　说明：{last.get('message', '-')}"}}])]})
        content.append({"component": "VRow", "content": [self._col(12, 12, [
            {"component": "VAlert", "props": {
                "type": "primary" if live.get("running") else "secondary",
                "variant": "tonal", "class": "mt-2", "title": "实时运行状态",
                "text": (f"运行中：{live.get('stage', '-')}　当前服务器：{live.get('current', '-')}　"
                         f"开始：{live.get('started_text', '-')}"
                         if live.get("running") else
                         "空闲。MP 插件卡片「查看日志」窗口通过 SSE 实时推送逐服务器明细；"
                         "本页点「刷新页面」拉取最新结果。")}}])]})
        content.append({"component": "VRow", "content": [self._col(12, 12, [
            self._action_button("立即刷新", "mdi-refresh", "/run", "primary"),
            self._action_button("检测服务器", "mdi-lan-connect", "/check", "info"),
            self._action_button("全库刷新", "mdi-library", "/refresh_all", "warning"),
            self._action_button("清空队列", "mdi-tray-arrow-up", "/clear_queue", "secondary"),
            self._action_button("清空日志", "mdi-delete-outline", "/clear_logs", "secondary"),
            self._action_button("刷新页面", "mdi-page-refresh-outline", "/status", "info", "get"),
            self._action_button("中止任务", "mdi-stop-circle-outline", "/stop", "error"),
        ])]})
        server_lines = []
        for server in self._servers:
            server_lines.append(
                f"{'[启用]' if server.enabled else '[停用]'} {server.label}　{server.url}　"
                f"映射 {len(server.mappings)} 条"
            )
        check_lines = [f"{item.get('time', '')}　{item.get('server', '')}　"
                       f"{'成功' if item.get('ok') else '失败'}　{item.get('message', '')}"
                       for item in checks]
        last_lines = [f"{item.get('server', '')}　{item.get('level_label', '')}　"
                      f"{'成功' if item.get('ok') else '失败'}　{item.get('message', '')}"
                      for item in last.get("server_results", [])]
        queue_lines = [f"{item.update_type}　{item.path}　重试 {item.attempts}　来源 {item.source}"
                       for item in queue[:40]]
        log_lines = [f"{item.get('time', '')} [{item.get('level', '')}] {item.get('message', '')}"
                     for item in logs[:60]]
        history_lines = [f"{item.get('time', '')}　{item.get('trigger', '')}　"
                         f"文件 {item.get('files', 0)}　成功服务器 {item.get('servers_ok', 0)}/{item.get('servers', 0)}"
                         for item in history[:20]]
        content.append({"component": "VRow", "content": [
            self._col(12, 6, [self._html_block("媒体服务器", server_lines or ["未配置"])]),
            self._col(12, 6, [self._html_block("最近连通性检测", check_lines or ["尚未检测"])])]})
        content.append({"component": "VRow", "content": [
            self._col(12, 6, [self._html_block("上轮逐服务器结果", last_lines or ["暂无数据"])]),
            self._col(12, 6, [self._html_block(f"待刷新队列（共 {len(queue)} 项）", queue_lines or ["队列为空"])])]})
        content.append({"component": "VRow", "content": [
            self._col(12, 6, [self._html_block("运行日志", log_lines or ["暂无日志"])]),
            self._col(12, 6, [self._html_block("运行历史", history_lines or ["暂无历史"])])]})
        return content

    # ------------------------------------------------------------------
    # 插件 API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/run", "endpoint": self.api_run, "methods": ["POST"],
             "auth": "bear", "summary": "立即处理待刷新队列"},
            {"path": "/check", "endpoint": self.api_check, "methods": ["POST"],
             "auth": "bear", "summary": "检测所有媒体服务器"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询运行状态与统计（详情页刷新入口）"},
            {"path": "/queue", "endpoint": self.api_queue, "methods": ["GET"],
             "auth": "bear", "summary": "查询待刷新队列"},
            {"path": "/logs", "endpoint": self.api_logs, "methods": ["GET"],
             "auth": "bear", "summary": "查询运行日志"},
            {"path": "/refresh_all", "endpoint": self.api_refresh_all, "methods": ["POST"],
             "auth": "bear", "summary": "对所有服务器发起全库刷新"},
            {"path": "/clear_queue", "endpoint": self.api_clear_queue, "methods": ["POST"],
             "auth": "bear", "summary": "清空待刷新队列"},
            {"path": "/clear_logs", "endpoint": self.api_clear_logs, "methods": ["POST"],
             "auth": "bear", "summary": "清空运行日志"},
            {"path": "/stop", "endpoint": self.api_stop, "methods": ["POST"],
             "auth": "bear", "summary": "中止正在执行的任务"},
        ]

    def api_run(self) -> schemas.Response:
        ok = self.start_run(trigger="详情页", prefer_thread=True)
        return schemas.Response(success=ok, message="已触发队列刷新" if ok else "已有任务在运行")

    def api_refresh_all(self) -> schemas.Response:
        ok = self.start_run(trigger="详情页全库", full_refresh=True, prefer_thread=True)
        return schemas.Response(success=ok, message="已触发全库刷新" if ok else "已有任务在运行")

    def api_check(self) -> schemas.Response:
        results = self._check_servers_sync()
        return schemas.Response(success=True, message="；".join(results) or "未配置服务器",
                                data=self.get_data(_CHECK_KEY) or [])

    def api_status(self) -> schemas.Response:
        stats = dict(self.get_data(_STATS_KEY) or {})
        stats["queue"] = len(self._load_queue())
        stats["live"] = dict(self.get_data(_LIVE_KEY) or {})
        stats["servers"] = [{"name": s.name, "type": s.type, "enabled": s.enabled,
                             "url": s.url} for s in self._servers]
        return schemas.Response(success=True, message="ok", data=stats)

    def api_queue(self) -> schemas.Response:
        queue = self._load_queue()
        return schemas.Response(success=True, message=f"共 {len(queue)} 项",
                                data=[item.to_dict() for item in queue])

    def api_logs(self) -> schemas.Response:
        return schemas.Response(success=True, message="ok", data=list(self.get_data(_LOG_KEY) or []))

    def api_clear_queue(self) -> schemas.Response:
        self.save_data(_QUEUE_KEY, [])
        self.save_data(_QUEUE_META_KEY, {})
        self._log("INFO", "待刷新队列已清空")
        return schemas.Response(success=True, message="队列已清空")

    def api_clear_logs(self) -> schemas.Response:
        self.save_data(_LOG_KEY, [])
        self._log("INFO", "运行日志已清空")
        return schemas.Response(success=True, message="日志已清空")

    def api_stop(self) -> schemas.Response:
        self._stop_event.set()
        self._log("WARN", "已收到中止信号")
        return schemas.Response(success=True, message="已发送中止信号")

    # ------------------------------------------------------------------
    # 队列
    # ------------------------------------------------------------------

    def _load_queue(self) -> List[RefreshItem]:
        """读取到期可处理的队列项；重试项未到退避时间前不参与本轮。"""
        raw = self.get_data(_QUEUE_KEY) or []
        now = time.time()
        items = []
        for entry in raw:
            item = RefreshItem.from_dict(entry)
            if item and (item.attempts == 0 or item.ts <= now + 1):
                items.append(item)
        return dedupe_items(items)

    def _save_queue(self, items: Sequence[RefreshItem]) -> None:
        self.save_data(_QUEUE_KEY, [item.to_dict() for item in items])
        if items:
            meta = {
                "first_ts": min(item.ts for item in items),
                "last_ts": max(item.ts for item in items),
                "updated": time.time(),
            }
        else:
            meta = {}
        self.save_data(_QUEUE_META_KEY, meta)

    def _enqueue(self, items: Sequence[RefreshItem]) -> None:
        with self._state_lock:
            pending = dedupe_items(self._load_queue() + list(items))
            self._save_queue(pending)
            count = len(pending)
            meta = dict(self.get_data(_QUEUE_META_KEY) or {})
            first_ts = float(meta.get("first_ts") or time.time())
            now = time.time()
            force, reason = should_force_dispatch(
                count, first_ts, now, self._max_wait, self._batch_size
            )
        if force:
            self._log("INFO", f"队列 {count} 项，{reason}，立即执行")
            self.start_run(trigger="事件合并", prefer_thread=True)
            return
        delay = debounce_delay(first_ts, now, self._merge_window, self._max_wait)
        self._schedule_debounce(delay)
        self._log("INFO", f"队列累计 {count} 项，{delay}s 后合并刷新（{reason}）")

    def _schedule_debounce(self, delay: int) -> None:
        add_once = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if callable(add_once):
            try:
                added = add_once(
                    self.__class__.__name__, _ONCE_JOB_ID, self.run_scheduled,
                    "媒体库精准刷新 · 合并队列", delay_seconds=delay,
                )
                if added is not False:
                    return
            except Exception as err:  # noqa: BLE001
                logger.debug(f"{_LOG_PREFIX}：一次性任务登记失败 {err}")
        # 调度器不可用时用一次性守护线程兜底
        def _timer() -> None:
            if self._stop_event.wait(delay):
                return
            self.start_run(trigger="合并窗口兜底", prefer_thread=True)
        threading.Thread(target=_timer, daemon=True, name="MLR-Debounce").start()

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def run_scheduled(self) -> None:
        """cron 对账 / debounce 一次性任务共用入口。"""
        self.start_run(trigger="定时对账", prefer_thread=True)

    def start_run(self, trigger: str = "手动", full_refresh: bool = False,
                  prefer_thread: bool = False) -> bool:
        """启动一轮刷新；已在运行时返回 False。"""
        with self._state_lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()

        def _task() -> None:
            try:
                self._run_wrapper(trigger, full_refresh)
            finally:
                with self._state_lock:
                    self._running = False

        if prefer_thread:
            self._thread = threading.Thread(target=_task, daemon=True, name="MLR-Run")
            self._thread.start()
        else:
            _task()
        return True

    def _run_wrapper(self, trigger: str, full_refresh: bool = False) -> None:
        try:
            result = self._run_coroutine(self._run_async(trigger, full_refresh))
            self._save_result(result, trigger)
            self._notify_result(result)
        except Exception as err:  # noqa: BLE001
            self._log("ERROR", f"刷新任务异常：{err}\n{traceback.format_exc()[-800:]}")
            self._send_message("媒体库精准刷新异常", f"触发：{trigger}\n错误：{err}")
        finally:
            self._update_live(running=False, stage="空闲")

    async def _run_async(self, trigger: str, full_refresh: bool = False) -> Dict[str, Any]:
        started = time.time()
        queue = self._load_queue()
        batch = queue[:self._batch_size]
        remaining = queue[self._batch_size:]
        enabled = [s for s in self._servers if s.enabled]
        result: Dict[str, Any] = {
            "trigger": trigger, "files": len(batch), "total_queued": len(queue),
            "servers": len(enabled), "servers_ok": 0,
            "server_results": [], "failed_items": [], "dropped_items": [],
            "message": "", "full_refresh": bool(full_refresh),
            "full_results": [], "warning": False,
            "duration": 0.0, "dry_run": self._dry_run,
        }
        self._update_live(running=True, stage="准备刷新", started_text=now_text(started),
                          current="-", done=0, total=len(enabled))
        self._log("INFO", f"开始刷新：触发={trigger}，待处理 {len(batch)} 项，"
                          f"队列剩余 {len(remaining)} 项，服务器 {len(enabled)} 台"
                          + ("（试运行）" if self._dry_run else ""))
        if not enabled:
            result["message"] = "未配置任何启用的媒体服务器，队列保留"
            result["warning"] = True
            self._save_queue(queue)
            result["duration"] = time.time() - started
            return result
        do_full = bool(full_refresh or (self._scheduled_full_refresh and trigger == "定时对账"))
        if not batch and not do_full:
            result["message"] = "队列为空，无需刷新"
            result["servers"] = 0
            result["duration"] = time.time() - started
            return result

        server_results: List[ServerResult] = []
        full_results: List[Dict[str, Any]] = []
        for index, server in enumerate(enabled, 1):
            if self._stop_event.is_set():
                self._log("WARN", "收到中止信号，停止后续服务器刷新")
                break
            self._update_live(stage="正在刷新服务器", current=server.label,
                              done=index - 1, total=len(enabled))
            client = build_client(
                server, timeout=self._request_timeout, verify=self._verify_ssl,
                dry_run=self._dry_run, fallback=self._fallback,
            )
            server_result = await client.refresh(batch) if batch else None
            if do_full:
                full_result = await client.refresh_all()
                full_results.append(full_result.to_dict())
                self._log("INFO", f"[服务器 {index}/{len(enabled)}] {server.label}｜"
                                  f"全库刷新｜{'成功' if full_result.ok else '失败'}｜{full_result.message}")
                if server_result is None:
                    server_result = full_result
            if server_result is None:
                continue
            server_results.append(server_result)
            self._log("INFO", f"[服务器 {index}/{len(enabled)}] {server.label}｜"
                              f"层级={server_result.level_label}｜"
                              f"结果={'成功' if server_result.ok else '失败'}｜"
                              f"文件={server_result.requested}｜{server_result.message}")
            for detail in server_result.details:
                self._log("INFO", f"  └ {server.label}：{detail}")
            if self._log_paths and batch:
                self._log("INFO", f"  └ 路径明细：\n{format_items(batch, 8, server)}")
            if server_result.ok:
                result["servers_ok"] += 1
            self._update_live(done=index, total=len(enabled), current=server.label)

        result["server_results"] = [r.to_dict() for r in server_results]
        result["full_results"] = full_results
        all_ok = bool(server_results) and all(r.ok for r in server_results)
        failed_items: List[RefreshItem] = []
        dropped: List[RefreshItem] = []
        if batch and not all_ok and not self._dry_run:
            for item in batch:
                retry = next_attempt(item, self._max_retries)
                if retry:
                    retry.ts = time.time() + retry_delay(item.attempts, self._retry_base, 900.0)
                    failed_items.append(retry)
                else:
                    dropped.append(item)
            if failed_items:
                self._log("WARN", f"{len(failed_items)} 项进入重试队列，"
                                  f"{len(dropped)} 项超过重试上限已丢弃")
            if dropped:
                self._log("ERROR", "已丢弃：" + "、".join(item.path for item in dropped[:5]))
        self._save_queue(remaining + failed_items)
        result["failed_items"] = len(failed_items)
        result["dropped_items"] = len(dropped)
        result["duration"] = round(time.time() - started, 3)
        result["message"] = (
            f"{'成功' if all_ok else '部分失败'}：{result['servers_ok']}/{len(server_results)} 台服务器，"
            f"处理 {len(batch)} 个文件，重试 {len(failed_items)}，丢弃 {len(dropped)}"
        )
        self._log("INFO", f"本轮完成：{result['message']}，耗时 {result['duration']}s")
        if remaining or failed_items:
            self._schedule_debounce(max(int(self._merge_window), 5))
        return result

    # ------------------------------------------------------------------
    # 服务器检测
    # ------------------------------------------------------------------

    async def _check_servers_async(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for server in self._servers:
            client = build_client(server, timeout=self._request_timeout,
                                  verify=self._verify_ssl, fallback=self._fallback)
            try:
                ok, message = await client.check()
            except Exception as err:  # noqa: BLE001
                ok, message = False, f"{type(err).__name__}: {err}"
            results.append({"server": server.label, "type": server.type,
                            "url": server.url, "ok": ok,
                            "message": mask_secrets(message, self._secrets()),
                            "time": now_text()})
            self._log("INFO" if ok else "WARN",
                      f"服务器检测：{server.label}｜{'成功' if ok else '失败'}｜{message}")
        self.save_data(_CHECK_KEY, results)
        return results

    def _check_servers_sync(self) -> List[str]:
        try:
            results = self._run_coroutine(self._check_servers_async())
        except Exception as err:  # noqa: BLE001
            self._log("ERROR", f"服务器检测异常：{err}")
            return [f"检测异常：{err}"]
        return [f"{'成功' if r['ok'] else '失败'} {r['server']}：{r['message']}" for r in results]

    # ------------------------------------------------------------------
    # 结果、通知、日志
    # ------------------------------------------------------------------

    def _save_result(self, result: Dict[str, Any], trigger: str) -> None:
        stats = dict(self.get_data(_STATS_KEY) or {})
        stats["last_result"] = result
        stats["last_run_text"] = now_text()
        stats["last_trigger"] = trigger
        stats["last_duration"] = result.get("duration", 0)
        stats["total_files"] = int(stats.get("total_files") or 0) + int(result.get("files") or 0)
        if result.get("server_results"):
            if result.get("servers_ok") == result.get("servers"):
                stats["ok_runs"] = int(stats.get("ok_runs") or 0) + 1
            else:
                stats["fail_runs"] = int(stats.get("fail_runs") or 0) + 1
        self.save_data(_STATS_KEY, stats)
        history = list(self.get_data(_HISTORY_KEY) or [])
        history.insert(0, {
            "time": now_text(), "trigger": trigger,
            "files": result.get("files", 0), "servers": result.get("servers", 0),
            "servers_ok": result.get("servers_ok", 0), "message": result.get("message", ""),
        })
        self.save_data(_HISTORY_KEY, history[:_MAX_HISTORY])

    def _update_live(self, **kwargs: Any) -> None:
        live = dict(self.get_data(_LIVE_KEY) or {})
        live.update(kwargs)
        live["updated"] = time.time()
        try:
            self.save_data(_LIVE_KEY, live)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"{_LOG_PREFIX}：写入实时状态失败 {err}")

    def _notify_result(self, result: Dict[str, Any]) -> None:
        if self._notify_mode == "关闭":
            return
        server_results = result.get("server_results") or []
        failed = bool(server_results) and result.get("servers_ok", 0) < len(server_results)
        if self._notify_mode == "仅异常" and not failed and not result.get("dropped_items"):
            return
        if not result.get("server_results") and not (failed or result.get("warning")):
            return
        lines = [
            f"触发：{result.get('trigger', '-')}",
            f"文件 {result.get('files', 0)} 个｜服务器 {result.get('servers_ok', 0)}/{result.get('servers', 0)} 成功",
            f"耗时 {result.get('duration', 0)}s｜队列剩余 {len(self._load_queue())} 项",
        ]
        for entry in result.get("server_results", [])[:8]:
            lines.append(f"· {entry.get('server')}｜{entry.get('level_label')}｜"
                         f"{'成功' if entry.get('ok') else '失败'}｜{entry.get('message', '')}")
        if result.get("dropped_items"):
            lines.append(f"⚠ {result['dropped_items']} 项超过重试上限已丢弃，请查看日志")
        title = "媒体库精准刷新异常" if failed else "媒体库精准刷新完成"
        self._send_message(title, "\n".join(lines))

    def _secrets(self) -> List[str]:
        return [s.token for s in self._servers if s.token]

    def _send_message(self, title: str, text: str, *,
                      channel: Any = None, userid: Any = None) -> None:
        payload: Dict[str, Any] = {"title": title, "text": mask_secrets(text, self._secrets())}
        if _PLUGIN_MTYPE is not None:
            payload["mtype"] = _PLUGIN_MTYPE
        if channel:
            payload["channel"] = channel
        if userid:
            payload["userid"] = userid
        try:
            self.post_message(**payload)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}：通知发送失败 {err}")

    def _log(self, level: str, message: str) -> None:
        content = mask_secrets(str(message), self._secrets())
        try:
            logs = list(self.get_data(_LOG_KEY) or [])
            logs.insert(0, {"time": now_text(), "level": level, "message": content})
            self.save_data(_LOG_KEY, logs[:_MAX_LOGS])
        except Exception as err:  # noqa: BLE001
            logger.debug(f"{_LOG_PREFIX}：写入运行日志失败 {err}")
        line = f"{_LOG_PREFIX}：{content}"
        if level == "ERROR":
            logger.error(line)
        elif level == "WARN":
            logger.warning(line)
        elif level == "DEBUG":
            logger.debug(line)
        else:
            logger.info(line)

    @staticmethod
    def _run_coroutine(coro: Any) -> Any:
        """已有事件循环时改在独立线程执行，避免 asyncio.run 嵌套报错。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=120)
