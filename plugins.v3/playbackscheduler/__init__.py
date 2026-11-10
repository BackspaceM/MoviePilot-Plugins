"""播放感知调度 —— MoviePilot V3 插件主类。

读取 Emby / Jellyfin / Plex 的活动播放会话，按"是否转码 / 是否远程"判定
播放状态，对下载器（qBittorrent / Transmission）施加对应限速，可选联动
RouterOS 简单队列；支持在播文件保护标签、观影后自动删种、定时轮询与
立即检查、MP 通知与 INFO 级 SSE 实时日志。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from app.schemas.types import EventType, MessageType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

try:
    from app.sdk.network import AsyncRequestUtils
except Exception:  # noqa: BLE001
    AsyncRequestUtils = None  # type: ignore

try:
    from app.sdk.services import DownloaderHelper, MediaServerHelper
except Exception:  # noqa: BLE001
    DownloaderHelper = None  # type: ignore
    MediaServerHelper = None  # type: ignore

from .core import (
    DEFAULT_LIMITS,
    Downloader,
    MediaServer,
    Session,
    WatchedItem,
    compute_limits,
    eligible_for_cleanup,
    format_sessions,
    mask_secrets,
    normalize_path,
    overall_state,
    parse_downloaders,
    parse_limits,
    parse_servers,
    prune_watched_history,
    state_label,
    update_watched_history,
    validate_cron,
    STATE_IDLE,
    STATE_DIRECTPLAY,
    STATE_TRANSCODE,
    STATE_REMOTE_TRANSCODE,
)
from .clients import (
    DownloaderClient,
    MediaClient,
    RouterOSClient,
    build_downloaderclient,
    build_mediaclient,
)

PLUGIN_NAME = "PlaybackScheduler"
PLUGIN_ICON = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/playbackscheduler.png"
PLUGIN_DESCRIPTION = "读取 Emby/Plex 在播会话，按转码/远程状态对下载器限速并可选联动 RouterOS，保护在播文件、观影后自动删种。"
PLUGIN_AUTHORS = "BackspaceM"
AUTHOR_URL = "https://github.com/BackspaceM"
PLUGIN_COLOR = "#5DADE2"
SYSTEM_VERSION = ">=3.0.0"

_LOG_PREFIX = "播放感知调度"
_ONCE_JOB_ID = "playback_once"
_MAX_LOGS = 200
_MAX_HISTORY = 500
_DEFAULT_CRON = "*/5 * * * *"
_DEFAULT_TAG = "playback-protect"
_LIVE_KEY = "playback_live"
_LAST_KEY = "playback_last"
_LOG_KEY = "playback_logs"
_HISTORY_KEY = "playback_history"

# 状态 -> (展示名, 说明, 下载限速字段, 上传限速字段)
_LIMIT_FIELDS = (
    (STATE_IDLE, "空闲", "无人在看，通常不限速", "limit_idle_dl", "limit_idle_up"),
    (STATE_DIRECTPLAY, "原画直连", "内网原画播放", "limit_directplay_dl", "limit_directplay_up"),
    (STATE_TRANSCODE, "本地转码", "内网转码播放", "limit_transcode_dl", "limit_transcode_up"),
    (STATE_REMOTE_TRANSCODE, "远程转码", "外网转码播放", "limit_remote_dl", "limit_remote_up"),
)


def _cfg_attr(cfg: Any, *names: str) -> Any:
    """兼容 dict / 对象两种配置载体读取字段。"""
    for name in names:
        value = cfg.get(name) if isinstance(cfg, dict) else getattr(cfg, name, None)
        if value not in (None, ""):
            return value
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _drop_duplicate_names(auto_lines: List[str], manual_text: str) -> List[str]:
    """手动框中已写同名实例的，跳过自动生成的配置行。"""
    manual_names = set()
    for raw in str(manual_text or "").splitlines():
        line = raw.strip()
        if line.startswith("#") or line.count("|") < 3:
            continue
        parts = line.split("|")
        if len(parts) > 1 and parts[1].strip():
            manual_names.add(parts[1].strip())
    return [line for line in auto_lines if line.split("|")[1].strip() not in manual_names]


# MP 不可用时降级
try:
    from app import schemas

    Response = schemas.Response
except Exception:  # noqa: BLE001
    from pydantic import BaseModel

    class Response(BaseModel):  # type: ignore
        """宿主不可用时的三字段 envelope 降级实现。"""

        success: bool = True
        message: str = ""
        data: Any = None


try:
    _PLUGIN_MTYPE = MessageType.Plugin
except Exception:  # noqa: BLE001
    _PLUGIN_MTYPE = None


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


class PlaybackScheduler(_PluginBase):
    plugin_name = PLUGIN_NAME
    plugin_desc = PLUGIN_DESCRIPTION
    plugin_icon = PLUGIN_ICON
    plugin_version = "1.1.1"
    plugin_author = PLUGIN_AUTHORS
    author_url = AUTHOR_URL
    plugin_config_prefix = "playbackscheduler_"
    plugin_color = PLUGIN_COLOR
    system_version = SYSTEM_VERSION
    plugin_order = 50

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._state_lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_started = 0.0
        self._source_errors: List[str] = []

    def init_plugin(self, config: dict) -> None:
        config = dict(config or {})
        self.stop_service()
        self._enabled = bool(config.get("enabled"))
        self._limits_text = str(config.get("limits") or "")
        self._mediaservers_text, self._downloaders_text = self._merge_sources(config)
        self._router_enabled = bool(config.get("router_enabled"))
        self._router_address = str(config.get("router_address") or "").strip()
        self._router_username = str(config.get("router_username") or "").strip()
        self._router_password = str(config.get("router_password") or "")
        self._router_queue_max = str(config.get("router_queue_max") or "").strip()
        self._protect_playing = bool(config.get("protect_playing"))
        self._protect_tag = str(config.get("protect_tag") or _DEFAULT_TAG).strip() or _DEFAULT_TAG
        self._auto_cleanup = bool(config.get("auto_cleanup"))
        self._cleanup_days = max(0, int(config.get("cleanup_days") or 7))
        self._cleanup_ratio = max(0.0, float(config.get("cleanup_ratio") or 1.0))
        self._cleanup_tag = str(config.get("cleanup_tag") or "watch-cleanup").strip() or "watch-cleanup"
        self._cron = str(config.get("cron") or _DEFAULT_CRON).strip() or _DEFAULT_CRON
        self._dry_run = bool(config.get("dry_run"))
        self._notify_mode = str(config.get("notify_mode") or "all").strip()
        self._timeout = 10

        self._servers: List[MediaServer] = []
        self._downloaders: List[Downloader] = []
        self._limits = self._parse_limits_config(config)
        self._parse_sources()

        # 立即执行一次
        if config.get("onlyonce"):
            config["onlyonce"] = False
            try:
                self.update_config(config)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"{_LOG_PREFIX}：复位立即执行开关失败 {err}")
            if not self._enabled:
                self._log("WARN", "插件未启用，已忽略「立即执行一次」")
            elif self.start_run(trigger="立即执行一次", prefer_thread=True):
                self._log("INFO", "配置页触发「立即执行一次」（独立线程）")
            else:
                self._log("WARN", "配置页触发被跳过：已有任务在运行")

    def _parse_sources(self) -> None:
        servers, _ = parse_servers(self._mediaservers_text)
        self._servers = [s for s in servers if s.enabled]
        downloaders, _ = parse_downloaders(self._downloaders_text)
        self._downloaders = [d for d in downloaders if d.enabled]
        for err in self._source_errors[:5]:
            logger.warning(f"{_LOG_PREFIX}：{err}")

    def _secrets(self) -> List[str]:
        out: List[str] = [s.apikey for s in self._servers if s.apikey]
        out += [d.password for d in self._downloaders if d.password]
        if self._router_password:
            out.append(self._router_password)
        return out

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        self._stop_event.set()
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remove_once):
            try:
                remove_once(self.__class__.__name__, _ONCE_JOB_ID)
            except Exception as err:  # noqa: BLE001
                pass
        thread = getattr(self, "_thread", None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        with self._state_lock:
            self._running = False

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except (ValueError, TypeError):
            trigger = CronTrigger.from_crontab(_DEFAULT_CRON)
        return [{
            "id": PLUGIN_NAME,
            "name": "播放感知调度轮询",
            "trigger": trigger,
            "func": self.start_run,
            "kwargs": {"trigger": "定时轮询", "prefer_thread": True},
        }]

    # ------------------------------------------------------------------
    # 已连接实例（MoviePilot 服务门面）与配置合并
    # ------------------------------------------------------------------

    @staticmethod
    def _connected_configs(kind: str) -> Dict[str, Any]:
        cls = MediaServerHelper if kind == "media" else DownloaderHelper
        if cls is None:
            return {}
        try:
            configs = cls().get_configs() or {}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}：读取已连接 {kind} 实例失败：{err}")
            return {}
        return configs if isinstance(configs, dict) else {}

    def _connected_items(self, kind: str) -> List[Dict[str, str]]:
        """生成配置页下拉项（媒体服务器 / 下载器）。"""
        items: List[Dict[str, str]] = []
        for name, cfg in self._connected_configs(kind).items():
            if kind == "media":
                stype = str(_cfg_attr(cfg, "type", "server_type") or "").lower()
                if stype not in ("emby", "jellyfin", "plex"):
                    continue
                title = f"{name}（{stype}）"
            else:
                stype = str(_cfg_attr(cfg, "type", "downloader", "module") or "").lower()
                title = f"{name}（{stype or '下载器'}）"
            items.append({"title": title, "value": str(name)})
        return items

    def _selected_lines(self, selected: Any, kind: str) -> Tuple[List[str], List[str]]:
        """把下拉勾选的已连接实例转成 parse_servers / parse_downloaders 配置行。"""
        lines: List[str] = []
        errors: List[str] = []
        configs = self._connected_configs(kind)
        raw = selected if isinstance(selected, (list, tuple, set)) else (
            [selected] if isinstance(selected, str) and selected.strip() else [])
        for name in [str(n) for n in raw if str(n).strip()]:
            cfg = configs.get(name)
            if cfg is None:
                errors.append(f"已连接实例「{name}」当前不存在，请重新勾选或用手动补充框填写")
                continue
            if kind == "media":
                stype = str(_cfg_attr(cfg, "type", "server_type") or "").lower()
                url = _cfg_attr(cfg, "url", "address", "host", "base_url")
                key = _cfg_attr(cfg, "apikey", "api_key", "token", "access_token")
                if not url or not key:
                    errors.append(f"媒体服务器「{name}」未暴露地址或密钥，请在手动补充框补全")
                    continue
                lines.append(f"{stype}|{name}|{url}|{key}||1")
            else:
                dtype = str(_cfg_attr(cfg, "type", "downloader", "module") or "").lower()
                url = _cfg_attr(cfg, "url", "host", "address", "base_url")
                user = str(_cfg_attr(cfg, "username", "user", "qb_username") or "")
                pwd = str(_cfg_attr(cfg, "password", "passwd", "qb_password") or "")
                if not url or dtype not in ("qbittorrent", "transmission"):
                    errors.append(f"下载器「{name}」未暴露地址或类型不受支持，请在手动补充框补全")
                    continue
                lines.append(f"{dtype}|{name}|{url}|{user}|{pwd}|1")
        return lines, errors

    def _merge_sources(self, config: Dict[str, Any]) -> Tuple[str, str]:
        """下拉勾选 + 手动补充合并成两份清单文本；手动同名配置优先。"""
        media_auto, media_err = self._selected_lines(config.get("media_servers"), "media")
        dl_auto, dl_err = self._selected_lines(config.get("downloader_servers"), "downloader")
        self._source_errors = media_err + dl_err
        manual_media = str(config.get("mediaservers") or "")
        manual_dl = str(config.get("downloaders") or "")
        media_lines = _drop_duplicate_names(media_auto, manual_media)
        dl_lines = _drop_duplicate_names(dl_auto, manual_dl)
        return ((manual_media + "\n" + "\n".join(media_lines)).strip(),
                (manual_dl + "\n" + "\n".join(dl_lines)).strip())

    def _parse_limits_config(self, config: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
        """结构化速度输入优先；老版本 limits 文本作为兜底。"""
        limits = parse_limits(self._limits_text)
        keys = [key for _, _, _, dl, up in _LIMIT_FIELDS for key in (dl, up)]
        if not any(key in config for key in keys):
            return limits
        out = dict(limits)
        for state, _, _, dl_key, up_key in _LIMIT_FIELDS:
            dl_default, up_default = out.get(state, (0.0, 0.0))
            out[state] = (_to_float(config.get(dl_key), dl_default),
                          _to_float(config.get(up_key), up_default))
        return out

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页：下拉选已连接实例、按状态填速度、分区排版。"""
        defaults: Dict[str, Any] = {
            "enabled": False,
            "onlyonce": False,
            "media_servers": [],
            "downloader_servers": [],
            "mediaservers": "",
            "downloaders": "",
            "limits": "",
            "router_enabled": False,
            "router_queue_max": "",
            "router_address": "",
            "router_username": "",
            "router_password": "",
            "protect_playing": False,
            "protect_tag": "playback-protect",
            "auto_cleanup": False,
            "cleanup_days": 7,
            "cleanup_ratio": 1.0,
            "cleanup_tag": "watch-cleanup",
            "cron": _DEFAULT_CRON,
            "notify_mode": "all",
            "dry_run": False,
        }
        for state, _, _, dl_key, up_key in _LIMIT_FIELDS:
            dl, up = DEFAULT_LIMITS.get(state, (0.0, 0.0))
            defaults[dl_key] = dl
            defaults[up_key] = up

        def col(md: int, content: List[dict]) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": content}

        def field(model: str, label: str, props: Optional[dict] = None) -> dict:
            p: Dict[str, Any] = {"model": model, "label": label}
            if props:
                p.update(props)
            return {"component": "VTextField", "props": p}

        def switch(model: str, label: str, hint: str = "") -> dict:
            p: Dict[str, Any] = {"model": model, "label": label}
            if hint:
                p.update({"hint": hint, "persistent-hint": True})
            return {"component": "VSwitch", "props": p}

        def section(title: str, subtitle: str = "") -> dict:
            return {"component": "div",
                    "props": {"class": "text-subtitle-2 font-weight-bold mt-5 mb-1"},
                    "text": title + (f"　—　{subtitle}" if subtitle else "")}

        # 四个播放状态各一张速度卡：只需填数字，不再写 "状态:下载,上传"
        limit_cards: List[dict] = []
        for state, name, note, dl_key, up_key in _LIMIT_FIELDS:
            card = {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": [{
                    "component": "VCardText",
                    "content": [
                        {"component": "div",
                         "props": {"class": "text-subtitle-2 font-weight-bold"},
                         "text": name},
                        {"component": "div",
                         "props": {"class": "text-caption text-medium-emphasis mb-2"},
                         "text": note},
                        field(dl_key, "下载 Mbps", {"type": "number", "density": "compact",
                                                   "hint": "0 = 不限速"}),
                        field(up_key, "上传 Mbps", {"type": "number", "density": "compact",
                                                   "hint": "0 = 不限速"}),
                    ],
                }],
            }
            limit_cards.append(col(3, [card]))

        notify_items = [
            {"title": "全部通知", "value": "all"},
            {"title": "仅异常", "value": "error"},
            {"title": "关闭", "value": "none"},
        ]

        content: List[dict] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-4"},
             "content": {"text": "看媒体服务器有没有人在看片，自动给下载器限速。"
                                 "媒体服务器和下载器从 MoviePilot 已连接的实例里选，限速填 Mbps 数字就行，0 是不限。"}},
            section("开关"),
            {"component": "VRow", "content": [
                col(4, [switch("enabled", "启用插件", "关闭后不再轮询与限速")]),
                col(4, [switch("onlyonce", "立即执行一次", "保存后触发一轮检查并自动复位")]),
                col(4, [{"component": "VSelect", "props": {
                    "model": "notify_mode", "label": "通知方式", "items": notify_items}}]),
            ]},
            section("数据源", "从 MoviePilot 已连接实例中勾选"),
            {"component": "VRow", "content": [
                col(6, [{"component": "VSelect", "props": {
                    "model": "media_servers", "label": "媒体服务器（可多选）",
                    "items": self._connected_items("media"), "multiple": True, "chips": True,
                    "clearable": True, "persistent-hint": True,
                    "hint": "已连接的 Emby / Jellyfin / Plex，地址与密钥自动带入"}}]),
                col(6, [{"component": "VSelect", "props": {
                    "model": "downloader_servers", "label": "下载器（可多选）",
                    "items": self._connected_items("downloader"), "multiple": True, "chips": True,
                    "clearable": True, "persistent-hint": True,
                    "hint": "已连接的 qBittorrent / Transmission"}}]),
            ]},
            {"component": "VRow", "content": [
                col(6, [{"component": "VTextarea", "props": {
                    "model": "mediaservers", "label": "手动补充媒体服务器（可选）", "rows": 2,
                    "auto-grow": True, "persistent-hint": True,
                    "hint": "每行：类型|名称|地址|密钥|路径映射|启用；同名行会覆盖上面的下拉项",
                    "placeholder": "emby|客厅Emby|http://192.168.1.10:8096|APIKEY|/media::/volume1/emby|1"}}]),
                col(6, [{"component": "VTextarea", "props": {
                    "model": "downloaders", "label": "手动补充下载器（可选）", "rows": 2,
                    "auto-grow": True, "persistent-hint": True,
                    "hint": "每行：类型|名称|地址|用户名|密码|启用；同名行会覆盖上面的下拉项",
                    "placeholder": "qbittorrent|QB|http://192.168.1.20:8080|admin|pass|1"}}]),
            ]},
            section("各状态限速", "单位 Mbps，0 = 不限速；一般只需压上传"),
            {"component": "VRow", "content": limit_cards},
            {"component": "VRow", "content": [
                col(12, [{"component": "VTextarea", "props": {
                    "model": "limits", "label": "限速文本（高级，可选）", "rows": 2,
                    "auto-grow": True, "persistent-hint": True,
                    "hint": "留空则以上方数字输入为准；需要批量定义时按「状态:下载,上传」每行一条填写",
                    "placeholder": "idle:0,0\ndirectplay:0,50\ntranscode:0,30\nremote_transcode:0,15"}}]),
            ]},
            section("RouterOS 联动（可选）"),
            {"component": "VRow", "content": [
                col(4, [switch("router_enabled", "联动 RouterOS 队列")]),
                col(4, [field("router_queue_max", "RouterOS 队列上限",
                              {"placeholder": "留空按上传限速推导，如 50M/500M"})]),
                col(4, [field("router_address", "RouterOS 地址")]),
                col(4, [field("router_username", "用户名")]),
                col(4, [field("router_password", "密码", {"type": "password"})]),
                col(4, [{"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                         "text": "不启用联动时以上信息可留空"}]),
            ]},
            section("在播保护与清理"),
            {"component": "VRow", "content": [
                col(4, [switch("protect_playing", "保护在播文件（打标签）")]),
                col(4, [field("protect_tag", "保护标签名")]),
                col(4, [switch("auto_cleanup", "观影后自动删种")]),
                col(4, [field("cleanup_days", "保留天数", {"type": "number"})]),
                col(4, [field("cleanup_ratio", "最小做种率", {"type": "number"})]),
                col(4, [field("cleanup_tag", "删种候选标签")]),
            ]},
            section("调度与调试"),
            {"component": "VRow", "content": [
                col(6, [field("cron", "轮询周期(cron)", {"placeholder": _DEFAULT_CRON})]),
                col(6, [switch("dry_run", "试运行（不实际修改）")]),
            ]},
        ]
        return content, defaults

    def start_run(self, trigger: str = "手动", prefer_thread: bool = False) -> bool:
        with self._state_lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()

        def _task() -> None:
            try:
                result = self._run_coroutine(self._async_check(trigger))
                self._save_result(result)
                self._notify_result(result, trigger)
            except Exception as err:  # noqa: BLE001
                self._log("ERROR", f"检查任务异常：{err}\n{traceback.format_exc()[-800:]}")
                self._send_message("播放感知调度异常", f"触发：{trigger}\n错误：{err}")
            finally:
                with self._state_lock:
                    self._running = False

        if prefer_thread:
            self._thread = threading.Thread(target=_task, daemon=True, name="PBS-Run")
            self._thread.start()
        else:
            _task()
        return True

    @staticmethod
    def _run_coroutine(coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=180)

    async def _async_check(self, trigger: str) -> Dict[str, Any]:
        started = time.time()
        result: Dict[str, Any] = {
            "trigger": trigger, "state": STATE_IDLE, "sessions": [],
            "downloaders": [], "router": None, "watched_count": 0,
            "cleaned": [], "duration": 0.0, "dry_run": self._dry_run, "message": "",
        }
        if AsyncRequestUtils is None:
            result["message"] = "网络模块不可用"
            return result

        http = AsyncRequestUtils()
        sessions: List[Session] = []
        for srv in self._servers:
            mc = build_mediaclient(srv, http, self._timeout, self._dry_run)
            try:
                ss = await mc.get_sessions()
                sessions.extend(ss)
            except Exception as err:  # noqa: BLE001
                self._log("WARN", f"读取 {srv.name} 会话失败：{err}")
        result["sessions"] = [s.__dict__ for s in sessions]
        state = overall_state(sessions)
        result["state"] = state
        self._update_live(state=state, sessions=sessions, running=True, stage="判定播放状态")
        self._log("INFO", f"[{trigger}] 播放状态={state_label(state)}｜{format_sessions(sessions)}")

        limits = compute_limits(state, self._limits)

        # 下载器限速
        for dl in self._downloaders:
            dc = build_downloaderclient(dl, http, self._timeout, self._dry_run)
            res = await dc.set_limits(limits["qb_dl_bytes"], limits["qb_up_bytes"],
                                      limits["tr_dl_kbps"], limits["tr_up_kbps"])
            result["downloaders"].append({"name": dl.name, "ok": res.ok, "message": res.message})
            self._log("INFO", f"下载器 {dl.name}：{res.message}")
            if self._protect_playing and state != STATE_IDLE:
                paths = [p for s in sessions if s.is_playing for p in s.paths]
                pr = await dc.tag_playing(paths, self._protect_tag, remove=False)
                if pr.message:
                    self._log("INFO", f"下载器 {dl.name}：{pr.message}")

        # RouterOS 队列
        if self._router_enabled and self._router_address:
            rc = RouterOSClient(self._router_address, self._router_username, self._router_password,
                                client=http, timeout=self._timeout, dry_run=self._dry_run)
            if state == STATE_IDLE:
                r = await rc.remove_queue()
            else:
                maxl = self._router_queue_max or f"{limits['dl_mbps']}M/{limits['up_mbps']}M"
                r = await rc.ensure_queue(maxl)
            result["router"] = {"ok": r.ok, "message": r.message}
            self._log("INFO", f"RouterOS：{r.message}")

        # 观看历史
        history = [WatchedItem.from_dict(d) for d in (self.get_data(_HISTORY_KEY) or [])]
        history = update_watched_history([h.to_dict() for h in history], sessions, time.time())
        if state == STATE_IDLE and self._auto_cleanup:
            await self._run_cleanup(http, history, result)
        history, expired = prune_watched_history(history, time.time(), 0)
        self.save_data(_HISTORY_KEY, history[:_MAX_HISTORY])
        result["watched_count"] = len(history)
        self._update_live(state=state, sessions=sessions, router=result["router"],
                          watched=len(history), running=True, stage="完成")
        result["duration"] = round(time.time() - started, 2)
        return result

    async def _run_cleanup(self, http: Any, history: List[Dict[str, Any]], result: Dict[str, Any]) -> None:
        eligible = eligible_for_cleanup(history, time.time(), self._cleanup_days)
        if not eligible:
            return
        for dl in self._downloaders:
            if not dl.is_qbittorrent:
                continue
            dc = build_downloaderclient(dl, http, self._timeout, self._dry_run)
            try:
                resp = await http.request("GET", f"{_base(dl)}/api/v2/torrents/info",
                                          auth=(dl.username, dl.password), timeout=self._timeout)
                torrents = resp.json() if resp else []
            except Exception:  # noqa: BLE001
                continue
            for item in eligible:
                for t in torrents if isinstance(torrents, list) else []:
                    save = normalize_path(t.get("save_path", "") or "")
                    matched = any(normalize_path(p).startswith(save + "/") or normalize_path(p) == save
                                  for p in item.get("paths", []))
                    if not matched:
                        continue
                    ratio = float(t.get("ratio", 0) or 0)
                    if ratio < self._cleanup_ratio:
                        continue
                    if self._dry_run:
                        result["cleaned"].append(f"{item['title']}（试运行，不下种）")
                        continue
                    try:
                        await http.request("POST", f"{_base(dl)}/api/v2/torrents/delete",
                                           data={"hashes": t.get("hash"), "deleteFiles": "false"},
                                           auth=(dl.username, dl.password), timeout=self._timeout)
                        await dc.tag_playing([], self._cleanup_tag, remove=False)
                        result["cleaned"].append(item["title"])
                    except Exception as err:  # noqa: BLE001
                        self._log("WARN", f"删种失败 {item['title']}：{err}")

    # ------------------------------------------------------------------
    # 重置 / 历史
    # ------------------------------------------------------------------

    def reset_limits(self) -> Dict[str, Any]:
        if AsyncRequestUtils is None:
            return {"ok": False, "message": "网络模块不可用"}
        http = AsyncRequestUtils()
        results = []
        for dl in self._downloaders:
            dc = build_downloaderclient(dl, http, self._timeout, self._dry_run)
            res = self._run_coroutine(dc.reset_limits())
            results.append({"name": dl.name, "ok": res.ok, "message": res.message})
            self._log("INFO", f"下载器 {dl.name} 限速已重置：{res.message}")
        if self._router_enabled and self._router_address:
            rc = RouterOSClient(self._router_address, self._router_username, self._router_password,
                                client=http, timeout=self._timeout, dry_run=self._dry_run)
            r = self._run_coroutine(rc.remove_queue())
            self._log("INFO", f"RouterOS 队列已移除：{r.message}")
        return {"ok": True, "results": results}

    def clear_history(self) -> Dict[str, Any]:
        self.save_data(_HISTORY_KEY, [])
        self._log("INFO", "观看历史已清空")
        return {"ok": True, "message": "观看历史已清空"}

    # ------------------------------------------------------------------
    # 数据存取 / 日志 / 通知
    # ------------------------------------------------------------------

    @staticmethod
    def _base(dl: Downloader) -> str:
        from .clients import normalize_base_url
        return normalize_base_url(dl.url, 8080 if dl.is_qbittorrent else 9091)

    def _update_live(self, **kwargs: Any) -> None:
        live = dict(self.get_data(_LIVE_KEY) or {})
        live.update(kwargs)
        self.save_data(_LIVE_KEY, live)

    def _save_result(self, result: Dict[str, Any]) -> None:
        self.save_data(_LAST_KEY, result)

    def _log(self, level: str, message: str) -> None:
        content = mask_secrets(str(message), self._secrets())
        try:
            logs = list(self.get_data(_LOG_KEY) or [])
            logs.insert(0, {"time": now_text(), "level": level, "message": content})
            self.save_data(_LOG_KEY, logs[:_MAX_LOGS])
        except Exception:  # noqa: BLE001
            pass
        line = f"{_LOG_PREFIX}：{content}"
        if level == "ERROR":
            logger.error(line)  # type: ignore[name-defined]
        elif level == "WARN":
            logger.warning(line)  # type: ignore[name-defined]
        elif level == "DEBUG":
            logger.debug(line)  # type: ignore[name-defined]
        else:
            logger.info(line)  # type: ignore[name-defined]

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
            logger.warning(f"{_LOG_PREFIX}：通知发送失败 {err}")  # type: ignore[name-defined]

    def _notify_result(self, result: Dict[str, Any], trigger: str) -> None:
        if self._notify_mode == "none":
            return
        failed = not result.get("downloaders") or any(not d.get("ok") for d in result.get("downloaders", []))
        if self._notify_mode == "error" and not failed:
            return
        lines = [f"触发：{trigger}", f"播放状态：{state_label(result.get('state', STATE_IDLE))}",
                 f"在播：{format_sessions([Session(**s) for s in result.get('sessions', [])])}"]
        for d in result.get("downloaders", []):
            lines.append(f"下载器 {d['name']}：{'成功' if d['ok'] else '失败'}｜{d.get('message','')}")
        if result.get("router"):
            lines.append(f"RouterOS：{result['router'].get('message','')}")
        if result.get("cleaned"):
            lines.append(f"自动删种：{len(result['cleaned'])} 个｜" + "；".join(result["cleaned"][:5]))
        lines.append(f"耗时 {result.get('duration', 0)}s")
        title = "播放感知调度异常" if failed else "播放感知调度执行完成"
        self._send_message(title, "\n".join(lines))

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def _action_button(self, label: str, api: str, *, color: str = "", icon: str = "",
                       confirm: bool = False, variant: str = "tonal",
                       method: str = "post") -> dict:
        """详情页按钮：文字放组件级 text，带颜色/图标，避免出现灰色空按钮。"""
        props: Dict[str, Any] = {"variant": variant, "size": "small", "class": "ma-1"}
        if color:
            props["color"] = color
        if icon:
            props["prepend-icon"] = icon
        return {"component": "VBtn", "props": props, "text": label,
                "events": {"click": {"api": f"plugin/{PLUGIN_NAME}/{api}",
                                     "method": method, "params": {}, "confirm": confirm}}}

    def get_page(self) -> List[dict]:
        """详情页：卡片分区 + 分组彩色按钮，所有按钮指向已注册的真实 API。"""
        live = dict(self.get_data(_LIVE_KEY) or {})
        state = live.get("state", STATE_IDLE)
        sessions = [Session(**s) for s in (live.get("sessions") or [])]
        playing = [s for s in sessions if s.is_playing]
        limits = compute_limits(state, self._limits)

        def kv(label: str, value: Any, md: int = 3) -> dict:
            return {"component": "VCol", "props": {"cols": 6, "md": md}, "content": [
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                 "text": label},
                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"},
                 "text": str(value)},
            ]}

        def text_row(value: str, size: int = 12) -> dict:
            return {"component": "div", "text": str(value),
                    "props": {"style": f"white-space:pre-wrap;font-size:{size}px"}}

        session_lines = [
            f"{s.user}@{s.server}｜{s.title}｜{'远程' if s.is_remote else '内网'}"
            f"/{'转码' if s.is_transcoding else '原画'}/{int(s.progress * 100)}%"
            for s in playing
        ] or ["当前无播放中会话"]

        state_color = {
            STATE_IDLE: "success", STATE_DIRECTPLAY: "primary",
            STATE_TRANSCODE: "warning", STATE_REMOTE_TRANSCODE: "error",
        }.get(state, "info")

        primary = [
            self._action_button("立即检查并应用", "run", color="primary",
                                icon="mdi-play", variant="flat"),
            self._action_button("查看当前状态", "status", color="info",
                                icon="mdi-information-outline", method="get"),
        ]
        secondary = [
            self._action_button("恢复默认限速", "reset", icon="mdi-restore"),
            self._action_button("清空观看历史", "clear_history", color="warning",
                                icon="mdi-delete-outline", confirm=True),
            self._action_button("停止", "stop", color="error",
                                icon="mdi-stop-circle-outline", confirm=True),
        ]

        return [
            {"component": "VAlert", "props": {
                "type": state_color, "variant": "tonal", "class": "mb-3",
                "title": "当前播放状态"},
             "content": {"text": f"{state_label(state)}　|　下载 {limits['dl_mbps']:g} Mbps / "
                                 f"上传 {limits['up_mbps']:g} Mbps　|　在播 {len(playing)} 路"}},
            {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": "操作"},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": "主要操作"},
                    {"component": "div", "content": primary},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": "维护与清理"},
                    {"component": "div", "content": secondary},
                ]},
            ]}
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "auth": "bear", "endpoint": self.api_run, "methods": ["POST"],
            "summary": "立即执行一轮播放感知检查",
        }, {
            "path": "/reset",
            "auth": "bear", "endpoint": self.api_reset, "methods": ["POST"],
            "summary": "恢复下载器与 RouterOS 默认限速",
        }, {
            "path": "/status",
            "auth": "bear", "endpoint": self.api_status, "methods": ["GET"],
            "summary": "获取当前播放与限速状态",
        }, {
            "path": "/stop",
            "auth": "bear", "endpoint": self.api_stop, "methods": ["POST"],
            "summary": "停止当前任务",
        }, {
            "path": "/clear_history",
            "auth": "bear", "endpoint": self.api_clear, "methods": ["POST"],
            "summary": "清空观看历史",
        }]

    def api_run(self, _=None) -> Response:
        self.start_run(trigger="API 立即检查", prefer_thread=True)
        return Response(success=True, message="已触发检查")

    def api_reset(self, _=None) -> Response:
        return Response(success=True, data=self.reset_limits())

    def api_status(self, _=None) -> Response:
        live = dict(self.get_data(_LIVE_KEY) or {})
        return Response(success=True, data=live)

    def api_stop(self, _=None) -> Response:
        self.stop_service()
        return Response(success=True, message="已停止")

    def api_clear(self, _=None) -> Response:
        return Response(success=True, data=self.clear_history())

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------

    def get_command(self) -> List[Dict[str, Any]]:
        return [{
            "cmd": "/playback_status", "event": EventType.PluginAction,
            "desc": "查看当前播放状态与限速", "category": "MoviePilot",
        }, {
            "cmd": "/playback_apply", "event": EventType.PluginAction,
            "desc": "立即执行播放感知检查", "category": "MoviePilot",
        }, {
            "cmd": "/playback_reset", "event": EventType.PluginAction,
            "desc": "恢复默认限速", "category": "MoviePilot",
        }, {
            "cmd": "/playback_history_clear", "event": EventType.PluginAction,
            "desc": "清空观看历史", "category": "MoviePilot",
        }]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data
        cmd = str(data.get("cmd") or "").lower()
        channel = data.get("channel")
        userid = data.get("user_id")
        handlers = {
            "/playback_status": self._cmd_status,
            "/playback_apply": self._cmd_apply,
            "/playback_reset": self._cmd_reset,
            "/playback_history_clear": self._cmd_clear,
        }
        handler = handlers.get(cmd)
        if handler:
            handler(channel, userid)

    def _cmd_status(self, channel, userid) -> None:
        live = dict(self.get_data(_LIVE_KEY) or {})
        sessions = [Session(**s) for s in (live.get("sessions") or [])]
        self._send_message("播放感知调度 · 状态",
                           f"状态：{state_label(live.get('state', STATE_IDLE))}\n在播：{format_sessions(sessions)}",
                           channel=channel, userid=userid)

    def _cmd_apply(self, channel, userid) -> None:
        self.start_run(trigger="远程命令", prefer_thread=True)
        self._send_message("播放感知调度", "已触发一轮检查", channel=channel, userid=userid)

    def _cmd_reset(self, channel, userid) -> None:
        res = self.reset_limits()
        self._send_message("播放感知调度 · 重置", f"完成：{res.get('ok')}", channel=channel, userid=userid)

    def _cmd_clear(self, channel, userid) -> None:
        self.clear_history()
        self._send_message("播放感知调度", "观看历史已清空", channel=channel, userid=userid)
