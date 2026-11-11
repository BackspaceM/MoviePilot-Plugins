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

from .core import (
    Downloader,
    MediaServer,
    Session,
    WatchedItem,
    compute_limits,
    eligible_for_cleanup,
    format_sessions,
    mask_secrets,
    md_safe,
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

_LOG_PREFIX = "播放调度"
_ONCE_JOB_ID = "playback_once"
_MAX_LOGS = 200
_MAX_HISTORY = 500
_DEFAULT_CRON = "*/5 * * * *"
_DEFAULT_TAG = "playback-protect"
_LIVE_KEY = "playback_live"
_LAST_KEY = "playback_last"
_LOG_KEY = "playback_logs"
_HISTORY_KEY = "playback_history"

# MP 不可用时降级
# V3 宿主用 FastAPI 动态注册插件路由：endpoint 的返回注解必须是 Pydantic
# 可识别的类型，普通类会导致 add_api_route 失败、路由被跳过、按钮 404。
from pydantic import BaseModel


class Response(BaseModel):
    """插件 API 统一响应：success / message / data 三段式。

    V3 宿主用 FastAPI 动态注册插件路由，endpoint 的返回注解必须是 Pydantic
    可识别的类型。若降级成普通 Python 类，add_api_route 会抛 FastAPIError，
    宿主捕获后跳过该路由，详情页按钮就会得到 404 Not Found。
    自带模型可避免依赖宿主内部模块布局，离线测试同样能完成路由注册。
    """

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
    plugin_name = 'PlaybackScheduler'
    plugin_desc = '读取 Emby/Plex 在播会话，按转码/远程状态对下载器限速并可选联动 RouterOS，保护在播文件、观影后自动删种。'
    plugin_icon = 'https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/playbackscheduler.png'
    plugin_version = "1.1.4"
    plugin_author = 'BackspaceM'
    author_url = 'https://github.com/BackspaceM'
    plugin_config_prefix = "playbackscheduler_"
    plugin_color = '#5DADE2'
    system_version = '>=3.0.0'
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

    def init_plugin(self, config: dict) -> None:
        config = dict(config or {})
        self.stop_service()
        self._enabled = bool(config.get("enabled"))
        self._mediaservers_text = str(config.get("mediaservers") or "")
        self._downloaders_text = str(config.get("downloaders") or "")
        self._limits_text = str(config.get("limits") or "")
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
        self._limits = parse_limits(self._limits_text)
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
            # kwargs 是展开给 APScheduler.add_job 的调度参数，绝不能含 trigger；
            # 插件 func 由 MP 无参调用，触发来源/线程偏好放在 func 默认值里。
            "kwargs": {},
        }]

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        content: List[dict] = [{
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件", "hint": "关闭后不再轮询与限速"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即执行一次", "hint": "保存后触发一轮检查并自动复位"}}]},
            ],
        }, {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VTextarea", "props": {"model": "mediasservers", "label": "媒体服务器",
                     "rows": 4, "placeholder": "emby|客厅Emby|http://192.168.1.10:8096|APIKEY|/media::/volume1/emby|1"}}]},
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VTextarea", "props": {"model": "downloaders", "label": "下载器",
                     "rows": 3, "placeholder": "qbittorrent|QB|http://192.168.1.20:8080|admin|pass|1"}}]},
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VTextarea", "props": {"model": "limits", "label": "各状态限速(Mbps，下载,上传)",
                     "rows": 4, "placeholder": "idle:0,0\ndirectplay:0,50\ntranscode:0,30\nremote_transcode:0,15"}}]},
            ],
        }, {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSwitch", "props": {"model": "router_enabled", "label": "联动 RouterOS 队列"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VTextField", "props": {"model": "router_queue_max", "label": "RouterOS 队列上限",
                     "placeholder": "留空按上传限速推导，如 50M/500M"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "router_address", "label": "RouterOS 地址"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "router_username", "label": "用户名"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "router_password", "type": "password", "label": "密码"}}]},
            ],
        }, {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "protect_playing", "label": "保护在播文件（打标签）"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "protect_tag", "label": "保护标签名"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "auto_cleanup", "label": "观影后自动删种"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "cleanup_days", "label": "保留天数", "type": "number"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "cleanup_ratio", "label": "最小做种率", "type": "number"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "cleanup_tag", "label": "删种候选标签"}}]},
            ],
        }, {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "cron", "label": "轮询周期(cron)", "placeholder": _DEFAULT_CRON}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSelect", "props": {"model": "notify_mode", "label": "通知方式",
                     "items": [{"title": "全部通知", "value": "all"}, {"title": "仅异常", "value": "error"}, {"title": "关闭", "value": "none"}]}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "dry_run", "label": "试运行（不实际修改）"}}]},
            ],
        }]

        defaults: Dict[str, Any] = {
            "enabled": False,
            "onlyonce": False,
            "mediaservers": "",
            "downloaders": "",
            "limits": "idle:0,0\ndirectplay:0,50\ntranscode:0,30\nremote_transcode:0,15",
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
            "cron": "*/5 * * * *",
            "notify_mode": "all",
            "dry_run": False,
        }
        return content, defaults

    # ------------------------------------------------------------------
    # 执行控制
    # ------------------------------------------------------------------

    def start_run(self, trigger: str = "定时轮询", prefer_thread: bool = True) -> bool:
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

    def get_page(self) -> List[dict]:
        """精简主界面：状态行 + 一行结果 + 操作按钮。
        运行日志由 MP 插件卡片右上角三圆点的实时日志查看，不在此重复渲染。"""
        live = dict(self.get_data(_LIVE_KEY) or {})
        last = dict(self.get_data(_LAST_KEY) or {})
        history = list(self.get_data(_HISTORY_KEY) or [])
        state = live.get("state", STATE_IDLE)

        def kv(label: str, value: Any) -> dict:
            return {"component": "VCol", "props": {"cols": 6, "md": 4}, "content": [
                {"component": "div", "text": md_safe(f"{label}：{value}"),
                 "props": {"style": "font-size:13px"}},
            ]}

        buttons = []
        for label, api in (("立即检查并应用", "run"), ("恢复默认限速", "reset"),
                           ("查看当前状态", "status"), ("清空观看历史", "clear_history"),
                           ("停止", "stop")):
            buttons.append({
                "component": "VBtn", "text": label,
                              "props": {"variant": "tonal", "size": "small"},
                "events": {"click": {"api": f"plugin/{PLUGIN_NAME}/{api}", "method": "post",
                                     "params": {}, "confirm": label in ("停止", "清空观看历史")}},
            })

        nodes: List[dict] = [
            {"component": "VRow", "content": [
                kv("播放状态", state_label(state)),
                kv("观看历史", f"{len(history)} 条"),
                kv("最近结果", last.get("message") or "尚未执行"),
            ]},
            {"component": "VRow", "props": {"class": "mt-3"}, "content": [
                {"component": "VCol", "content": buttons}]},
        ]
        return [{"component": "VCard", "props": {"variant": "tonal"}, "content": [
            {"component": "VCardText", "content": nodes}]}]

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "auth": "bear",
            "endpoint": self.api_run, "methods": ["POST"],
            "summary": "立即执行一轮播放感知检查",
        }, {
            "path": "/reset",
            "auth": "bear",
            "endpoint": self.api_reset, "methods": ["POST"],
            "summary": "恢复下载器与 RouterOS 默认限速",
        }, {
            "path": "/status",
            "auth": "bear",
            # 详情页「查看当前状态」按钮用 POST；同时保留 GET 供外部查询。
            "endpoint": self.api_status, "methods": ["GET", "POST"],
            "summary": "获取当前播放与限速状态",
        }, {
            "path": "/stop",
            "auth": "bear",
            "endpoint": self.api_stop, "methods": ["POST"],
            "summary": "停止当前任务",
        }, {
            "path": "/clear_history",
            "auth": "bear",
            "endpoint": self.api_clear, "methods": ["POST"],
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
        self._send_message("播放调度", "已触发一轮检查", channel=channel, userid=userid)

    def _cmd_reset(self, channel, userid) -> None:
        res = self.reset_limits()
        self._send_message("播放感知调度 · 重置", f"完成：{res.get('ok')}", channel=channel, userid=userid)

    def _cmd_clear(self, channel, userid) -> None:
        self.clear_history()
        self._send_message("播放调度", "观看历史已清空", channel=channel, userid=userid)
