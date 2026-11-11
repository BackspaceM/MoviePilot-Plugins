"""观影周报 / 转码诊断 —— MoviePilot V3 插件主类。

从 MediaPilot 已连接的媒体服务器（Emby / Jellyfin / Plex，下拉勾选 +
手动补充）读取近期播放历史，聚合成"观影周报"（时长 / 排行 / 每日趋势 /
电影剧集占比），并按规则引擎扫描转码记录给出可执行的"转码诊断"建议。

- 支持本周 / 上周 / 最近 7 天三种统计窗，周一或周日为一周起点；
- 定时推送（cron）+ 立即执行 + 详情页手动采集/诊断/推送；
- MP 通知、INFO 级 SSE 实时日志；
- 详情页动态文本统一经 md_safe 转义、日志行用全角 ｜ 分隔，
  杜绝半角方括号被当 Markdown 参考式链接渲染成"空药丸"（截图红框问题）。
- 媒体服务器用"已连接实例下拉多选 + 手动补充框"的通用选型模式。
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
    MEDIA_MOVIE,
    MEDIA_TV,
    SEVERITY_LABELS,
    WEEK_LAST,
    WEEK_LAST7,
    WEEK_MODE_LABELS,
    WEEK_MODES,
    WEEK_THIS,
    Finding,
    MediaServer,
    PlayRecord,
    build_weekly_report,
    diagnose_transcodes,
    format_duration,
    format_findings_text,
    format_log_line,
    format_report_text,
    health_level,
    in_range,
    mask_secrets,
    md_safe,
    merge_records,
    parse_servers,
    prune_records,
    validate_cron,
    week_range,
)
from .clients import build_media_client

PLUGIN_NAME = "ViewingWeeklyReport"
PLUGIN_ICON = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/viewingweeklyreport.png"
PLUGIN_DESCRIPTION = "汇总 Emby/Jellyfin/Plex 近期观影数据生成本周/上周/近 7 天观影周报（时长、排行、每日趋势），并扫描转码记录给出硬件加速、音频直通、字幕与远程码率等可执行的转码诊断建议。"
PLUGIN_AUTHORS = "BackspaceM"
AUTHOR_URL = "https://github.com/BackspaceM"
PLUGIN_COLOR = "#8E44AD"
SYSTEM_VERSION = ">=3.0.0"

_LOG_PREFIX = "观影周报与转码诊断"
_ONCE_JOB_ID = "vwr_once"
_MAX_LOGS = 200
_DEFAULT_CRON = "0 9 * * 1"          # 每周一 09:00 推送周报
_DEFAULT_HISTORY_LIMIT = 200
_DEFAULT_RETENTION_DAYS = 60
_DEFAULT_TOP_N = 10
_DEFAULT_HIGH_BITRATE = 20000        # kbps

# plugindata keys
_KEY_RECORDS = "vwr_records"
_KEY_REPORT = "vwr_report"
_KEY_FINDINGS = "vwr_findings"
_KEY_LIVE = "vwr_live"
_KEY_LOGS = "vwr_logs"
_KEY_LAST = "vwr_last"
_KEY_ACTIVE = "vwr_active"

_MEDIA_TYPES = ("emby", "plex", "jellyfin")

_NOTIFY_LABELS = {
    "all": "周报 + 诊断（推荐）",
    "report": "仅周报",
    "none": "仅站内查看，不推送",
}
_WEEK_START_LABELS = {
    "0": "周一为一周起点（ISO）",
    "6": "周日为一周起点",
}

try:
    from schemas import Response
except Exception:  # noqa: BLE001
    class Response:  # type: ignore
        def __init__(self, success: bool = True, message: str = "ok", data: Any = None):
            self.success = success
            self.message = message
            self.data = data

try:
    _PLUGIN_MTYPE = MessageType.Plugin
except Exception:  # noqa: BLE001
    _PLUGIN_MTYPE = None


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _ts_text(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return str(ts)


def _cfg_attr(cfg: Any, *names: str) -> Any:
    """从配置对象/字典里按顺序取第一个非空属性。"""
    for n in names:
        if isinstance(cfg, dict):
            val = cfg.get(n)
        else:
            val = getattr(cfg, n, None)
        if val not in (None, ""):
            return val
    return None


class ViewingWeeklyReport(_PluginBase):
    # 类体元数据一律用字符串字面量：官方 check_plugin_versions.py 用 AST
    # 直接提取字面量，写成模块常量引用会被判为"未声明"。
    plugin_name = "ViewingWeeklyReport"
    plugin_desc = ("汇总 MoviePilot 已连接媒体服务器（Emby/Jellyfin/Plex）近期观影数据，"
                   "生成本周/上周/近 7 天观影周报（观看时长、用户与服务器排行、每日趋势、"
                   "电影剧集占比、热门影片），并按规则引擎扫描转码记录给出硬件加速、音频直通、"
                   "字幕烧录、远程码率等可执行诊断建议。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/viewingweeklyreport.png"
    plugin_version = "1.1.3"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "viewingweeklyreport_"
    plugin_color = "#8E44AD"
    system_version = ">=3.0.0"
    plugin_order = 60

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
        # 默认配置，保证 init_plugin 前调用 get_form/get_page 不崩
        self._media_servers: List[str] = []
        self._media_servers_manual = ""
        self._week_mode = WEEK_THIS
        self._week_start = 0
        self._history_limit = _DEFAULT_HISTORY_LIMIT
        self._retention_days = _DEFAULT_RETENTION_DAYS
        self._top_n = _DEFAULT_TOP_N
        self._diagnose_live = True
        self._high_bitrate_kbps = _DEFAULT_HIGH_BITRATE
        self._cron = _DEFAULT_CRON
        self._notify_mode = "all"
        self._timeout = 10

    def init_plugin(self, config: dict) -> None:
        config = dict(config or {})
        self.stop_service()
        self._enabled = bool(config.get("enabled"))

        selected = config.get("media_servers") or []
        if isinstance(selected, str):
            selected = [x.strip() for x in selected.split(",") if x.strip()]
        self._media_servers = [str(x) for x in selected]
        self._media_servers_manual = str(config.get("media_servers_manual") or "")

        self._week_mode = str(config.get("week_mode") or WEEK_THIS).strip()
        if self._week_mode not in WEEK_MODES:
            self._week_mode = WEEK_THIS
        try:
            self._week_start = int(config.get("week_start", 0))
        except (TypeError, ValueError):
            self._week_start = 0
        if self._week_start not in (0, 6):
            self._week_start = 0

        self._history_limit = max(20, min(1000, int(config.get("history_limit") or _DEFAULT_HISTORY_LIMIT)))
        self._retention_days = max(0, min(3650, int(config.get("retention_days") or _DEFAULT_RETENTION_DAYS)))
        self._top_n = max(1, min(50, int(config.get("top_n") or _DEFAULT_TOP_N)))
        self._diagnose_live = bool(config.get("diagnose_live", True))
        self._high_bitrate_kbps = max(1000, int(config.get("high_bitrate_kbps") or _DEFAULT_HIGH_BITRATE))
        self._cron = str(config.get("cron") or _DEFAULT_CRON).strip() or _DEFAULT_CRON
        if not validate_cron(self._cron)[0]:
            self._cron = _DEFAULT_CRON
        self._notify_mode = str(config.get("notify_mode") or "all").strip() or "all"
        self._timeout = max(3, min(60, int(config.get("timeout") or 10)))

        self._update_live(enabled=self._enabled, running=False, stage="待命")

        if config.get("onlyonce"):
            config["onlyonce"] = False
            try:
                self.update_config(config)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"{_LOG_PREFIX}：复位立即执行开关失败 {err}")
            if not self._enabled:
                self._log("WARN", "插件未启用，已忽略「立即执行一次」")
            elif self.start_run(trigger="立即执行一次", mode="full", prefer_thread=True):
                self._log("INFO", "配置页触发「立即执行一次」（独立线程）")
            else:
                self._log("WARN", "配置页触发被跳过：已有任务在运行")

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        self._stop_event.set()
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remove_once):
            try:
                remove_once(self.__class__.__name__, _ONCE_JOB_ID)
            except Exception:  # noqa: BLE001
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
            "name": "观影周报 / 转码诊断",
            "trigger": trigger,
            "func": self.start_run,
            # kwargs 是展开给 APScheduler.add_job 的调度参数，绝不能含 trigger；
            # 插件 func 由 MP 无参调用，触发来源/模式/线程偏好放在 func 默认值里。
            "kwargs": {},
        }]

    # ------------------------------------------------------------------
    # 已连接媒体服务器下拉数据源（通用选型模式）
    # ------------------------------------------------------------------

    @staticmethod
    def _connected_media_configs() -> Dict[str, Any]:
        """从 MP 服务门面读取已连接的媒体服务器配置，按类型过滤。"""
        try:
            from app.sdk.services import MediaServerHelper
            configs = MediaServerHelper().get_configs() or {}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}：读取已连接媒体服务器失败 {err}")
            return {}
        out: Dict[str, Any] = {}
        items = configs.items() if isinstance(configs, dict) else []
        for key, cfg in items:
            stype = _cfg_attr(cfg, "type", "server_type")
            if str(stype or "").lower() in _MEDIA_TYPES:
                out[str(key)] = cfg
        return out

    def _connected_items(self) -> List[Dict[str, str]]:
        return [
            {"title": f"{name}（{str(_cfg_attr(cfg, 'type', 'server_type') or '').lower()}）", "value": name}
            for name, cfg in self._connected_media_configs().items()
        ]

    def _resolve_servers(self) -> Tuple[List[MediaServer], List[str]]:
        """把下拉勾选的已连接实例 + 手动补充，解析为可连接的 MediaServer 列表。"""
        servers: List[MediaServer] = []
        warnings: List[str] = []
        cfg_map = self._connected_media_configs()
        for name in self._media_servers:
            cfg = cfg_map.get(name)
            if cfg is None:
                warnings.append(f"已选实例 {name} 在当前连接列表中不存在，请重新勾选或改到手动补充框")
                continue
            url = _cfg_attr(cfg, "url", "address", "host", "base_url")
            apikey = _cfg_attr(cfg, "apikey", "api_key", "token", "access_token")
            stype = str(_cfg_attr(cfg, "type", "server_type") or "").lower()
            if not url or not apikey:
                warnings.append(f"已连接实例 {name} 未暴露地址或密钥，请在「手动补充」框按格式补全")
                continue
            servers.append(MediaServer(type=stype, name=name, url=str(url), apikey=str(apikey)))
        manual, errors = parse_servers(self._media_servers_manual)
        warnings.extend(errors)
        servers.extend([s for s in manual if s.enabled])
        # 按名称去重（手动补充优先覆盖同名下拉项）
        seen: Dict[str, MediaServer] = {}
        for s in servers:
            seen[s.name] = s
        return list(seen.values()), warnings

    def _secrets(self) -> List[str]:
        secrets: List[str] = []
        for name in self._media_servers:
            cfg = self._connected_media_configs().get(name)
            if cfg is not None:
                v = _cfg_attr(cfg, "apikey", "api_key", "token", "access_token")
                if v:
                    secrets.append(str(v))
        manual, _ = parse_servers(self._media_servers_manual)
        secrets.extend([s.apikey for s in manual if s.apikey])
        return secrets

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        defaults: Dict[str, Any] = {
            "enabled": False,
            "onlyonce": False,
            "media_servers": [],
            "media_servers_manual": "",
            "week_mode": WEEK_THIS,
            "week_start": 0,
            "history_limit": _DEFAULT_HISTORY_LIMIT,
            "retention_days": _DEFAULT_RETENTION_DAYS,
            "top_n": _DEFAULT_TOP_N,
            "diagnose_live": True,
            "high_bitrate_kbps": _DEFAULT_HIGH_BITRATE,
            "cron": _DEFAULT_CRON,
            "notify_mode": "all",
            "timeout": 10,
        }

        def fld(model: str, label: str, props: Optional[dict] = None) -> dict:
            p = {"model": model, "label": label}
            if props:
                p.update(props)
            return {"component": "VTextField", "props": p}

        week_items = [{"title": v, "value": k} for k, v in WEEK_MODE_LABELS.items()]
        week_start_items = [{"title": v, "value": int(k)} for k, v in _WEEK_START_LABELS.items()]
        connected_items = self._connected_items()

        content: List[dict] = [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件", "hint": "关闭后不再定时推送"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即生成一次周报",
                                                       "hint": "保存后按当前配置采集并分析，随后自动复位"}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 7}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "media_servers", "label": "已连接媒体服务器（可多选）",
                        "items": connected_items, "multiple": True, "chips": True,
                        "clearable": True, "hint": "从 MoviePilot 已连接的实例中勾选；不选则仅用下方手动补充",
                        "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 5}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "week_mode", "label": "统计周期", "items": week_items,
                        "hint": "周报统计的时间窗", "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VTextarea", "props": {
                        "model": "media_servers_manual", "label": "手动补充媒体服务器（可选）",
                        "rows": 3, "auto-grow": True, "persistent-hint": True,
                        "hint": "每行：类型|名称|地址|密钥|路径映射|启用；映射可省略，启用省略默认 1",
                        "placeholder": "emby|客厅Emby|http://192.168.1.10:8096|APIKEY|/media::/volume1/emby|1\n"
                                       "plex|书房Plex|http://192.168.1.11:32400|PTOKEN||1"}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "week_start", "label": "一周起点", "items": week_start_items}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("history_limit", "每台拉取历史条数", {"type": "number"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("retention_days", "本地记录保留天数", {"type": "number", "hint": "0=永久保留"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("top_n", "热门榜单条数", {"type": "number"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("high_bitrate_kbps", "高码率阈值(kbps)", {"type": "number"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {
                        "model": "diagnose_live", "label": "附带实时转码诊断",
                        "hint": "运行时读取当前在播会话的转码现场"}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("cron", "推送周期(cron)", {"placeholder": "0 9 * * 1  每周一 09:00"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "notify_mode", "label": "通知方式",
                        "items": [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()]}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("timeout", "请求超时(秒)", {"type": "number"})]},
            ]},
        ]
        return content, defaults

    # ------------------------------------------------------------------
    # 执行控制
    # ------------------------------------------------------------------

    def start_run(self, trigger: str = "定时周报", mode: str = "full",
                  prefer_thread: bool = True) -> bool:
        """mode: full=采集+聚合+诊断(+通知)；collect=仅采集；diagnose=实时转码诊断。"""
        with self._state_lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()
        self._update_live(running=True, stage=f"{trigger}：准备数据", mode=mode)

        def _task() -> None:
            try:
                result = self._run_coroutine(self._async_run(trigger, mode))
                self.save_data(_KEY_LAST, result)
                self._notify_result(result, mode)
            except Exception as err:  # noqa: BLE001
                self._log("ERROR", f"任务异常：{err}\n{traceback.format_exc()[-800:]}")
                self._send_message("观影周报异常", f"触发：{trigger}\n错误：{err}")
            finally:
                with self._state_lock:
                    self._running = False
                self._update_live(running=False, stage="待命")

        if prefer_thread:
            self._thread = threading.Thread(target=_task, daemon=True, name="VWR-Run")
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

    def _make_http(self) -> Any:
        if AsyncRequestUtils is None:
            return None
        return AsyncRequestUtils(timeout=self._timeout)

    async def _async_run(self, trigger: str, mode: str) -> Dict[str, Any]:
        started = time.time()
        result: Dict[str, Any] = {
            "trigger": trigger, "mode": mode, "ok": False,
            "servers": [], "collected": 0, "active": 0,
            "report": None, "findings": [], "errors": [], "message": "",
        }
        servers, warnings = self._resolve_servers()
        for w in warnings:
            self._log("WARN", w)
        result["servers"] = [s.name for s in servers]

        if AsyncRequestUtils is None:
            result["message"] = "网络模块不可用"
            self._log("ERROR", "网络模块 AsyncRequestUtils 不可用")
            return result
        if not servers:
            result["message"] = "未配置任何媒体服务器，请在配置页勾选已连接实例或填写手动补充"
            self._log("WARN", result["message"])
            return result

        http = self._make_http()
        incoming: List[PlayRecord] = []
        active: List[PlayRecord] = []

        # 1) 采集近期历史（full / collect）
        if mode in ("full", "collect"):
            for srv in servers:
                try:
                    client = build_media_client(srv, http, timeout=self._timeout)
                    recs = await client.get_recent_records(limit=self._history_limit)
                    incoming.extend(recs)
                    self._log("INFO", f"【{trigger}】 {srv.name}：采集近期播放 {len(recs)} 条")
                except Exception as err:  # noqa: BLE001
                    msg = f"{srv.name} 采集失败：{type(err).__name__} {err}"
                    result["errors"].append(msg)
                    self._log("WARN", f"【{trigger}】 {msg}")
        # 2) 实时转码现场（full / diagnose）
        if mode in ("full", "diagnose") and self._diagnose_live:
            for srv in servers:
                try:
                    client = build_media_client(srv, http, timeout=self._timeout)
                    sess = await client.get_active_records()
                    active.extend(sess)
                    if sess:
                        self._log("INFO", f"【{trigger}】 {srv.name}：在播会话 {len(sess)} 个，"
                                          f"其中转码 {sum(1 for x in sess if x.is_transcode)} 个")
                except Exception as err:  # noqa: BLE001
                    result["errors"].append(f"{srv.name} 会话读取失败：{type(err).__name__} {err}")

        # 3) 合并 & 裁剪历史记录
        existing = list(self.get_data(_KEY_RECORDS) or [])
        merged = merge_records(existing, incoming, max_records=2000)
        kept, removed = prune_records(merged, time.time(), self._retention_days)
        self.save_data(_KEY_RECORDS, kept)
        result["collected"] = len(incoming)
        result["active"] = len(active)
        if removed:
            self._log("INFO", f"【{trigger}】 裁剪过期记录 {removed} 条，本地保留 {len(kept)} 条")

        # 4) 聚合周报（full / collect 都刷新周报数据）
        start, end, label = week_range(self._week_mode, time.time(), self._week_start)
        report = build_weekly_report(
            [PlayRecord.from_dict(d) for d in kept], start, end, top_n=self._top_n,
        )
        self.save_data(_KEY_REPORT, report)
        result["report"] = report

        # 5) 转码诊断（窗口内历史 + 当前在播）
        kept_records = [PlayRecord.from_dict(d) for d in kept]
        window_records = [r for r in kept_records if in_range(r, start, end)]
        diag_records = window_records + active
        findings = diagnose_transcodes(diag_records, high_bitrate_kbps=self._high_bitrate_kbps)
        self.save_data(_KEY_FINDINGS, [f.to_dict() for f in findings])
        self.save_data(_KEY_ACTIVE, [r.to_dict() for r in active])
        result["findings"] = [f.to_dict() for f in findings]

        # 6) 实时状态
        level, level_note = health_level(report)
        self._update_live(
            enabled=self._enabled, window=label, week_mode=self._week_mode,
            record_count=len(kept), total_plays=report["total_plays"],
            watch_seconds=report["total_seconds"], transcode_ratio=report["transcode_ratio"],
            health_level=level, last_run=time.time(),
            findings_count=len([f for f in findings if f.severity != "info"]),
        )

        result["ok"] = not result["errors"]
        result["message"] = (
            f"{label}：{report['total_plays']} 次播放 / {format_duration(report['total_seconds'])} / "
            f"转码 {report['transcode_ratio']}%（{level}）"
        )
        self._log("INFO", f"【{trigger}】 {result['message']}")
        result["duration"] = round(time.time() - started, 2)
        return result

    # ------------------------------------------------------------------
    # 数据 / 日志 / 通知
    # ------------------------------------------------------------------

    def _update_live(self, **kwargs: Any) -> None:
        live = dict(self.get_data(_KEY_LIVE) or {})
        live.update(kwargs)
        self.save_data(_KEY_LIVE, live)

    def _log(self, level: str, message: str) -> None:
        content = mask_secrets(str(message), self._secrets())
        try:
            logs = list(self.get_data(_KEY_LOGS) or [])
            logs.insert(0, {"time": now_text(), "level": level, "message": content})
            self.save_data(_KEY_LOGS, logs[:_MAX_LOGS])
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
        payload: Dict[str, Any] = {
            "title": md_safe(title), "text": md_safe(mask_secrets(text, self._secrets())),
        }
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

    def _notify_result(self, result: Dict[str, Any], mode: str) -> None:
        if mode == "collect" or self._notify_mode == "none":
            return
        report = result.get("report") or {}
        findings = [Finding.from_dict(f) for f in (result.get("findings") or [])]
        level, _ = health_level(report)
        parts: List[str] = []
        if mode in ("full", "diagnose"):
            if self._notify_mode in ("all", "report") and report.get("total_plays"):
                parts.append(format_report_text(report))
            if self._notify_mode == "all":
                issues = [f for f in findings if f.severity != "info"]
                if issues:
                    parts.append("转码诊断：" + "；".join(
                        f"{SEVERITY_LABELS.get(f.severity, '提示')}·{f.title}（{f.count} 次）" for f in issues[:5]))
        if not parts:
            return
        title = "观影周报" if mode == "full" else "转码诊断"
        if mode == "full":
            title += f" · {level}"
        self._send_message(title, "\n\n".join(parts))

    # ------------------------------------------------------------------
    # 详情页（每个按钮都绑定真实 API，且不使用 disabled）
    # ------------------------------------------------------------------

    def _action_button(self, label: str, api: str, *, params: Optional[dict] = None,
                       confirm: bool = False, variant: str = "tonal",
                       color: Optional[str] = None) -> dict:
        props: Dict[str, Any] = {"variant": variant, "size": "small"}
        if color:
            props["color"] = color
        event: Dict[str, Any] = {
            "api": f"plugin/{PLUGIN_NAME}/{api}", "method": "post",
            "params": params or {}, "confirm": confirm,
        }
        return {"component": "VBtn", "props": props, "text": label,
                "events": {"click": event}}

    def get_page(self) -> List[dict]:
        """精简主界面：状态行 + 一行摘要 + 操作按钮。
        运行日志由 MP 插件卡片右上角三圆点的实时日志查看，不在此重复渲染；
        周报/诊断全文通过「立即推送」发送到通知查看。"""
        live = dict(self.get_data(_KEY_LIVE) or {})
        last = dict(self.get_data(_KEY_LAST) or {})
        report = dict(self.get_data(_KEY_REPORT) or {})
        level, level_note = health_level(report)
        servers, _ = self._resolve_servers()

        def kv(label: str, value: Any) -> dict:
            return {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                {"component": "div", "text": md_safe(f"{label}：{value}"),
                 "props": {"style": "font-size:13px"}},
            ]}

        op_buttons = [
            self._action_button("生成周报", "run", variant="flat", color="primary"),
            self._action_button("转码诊断", "diagnose"),
            self._action_button("采集近期历史", "collect"),
            self._action_button("立即推送", "push"),
            self._action_button("测试连接", "test"),
            self._action_button("清空本地记录", "clear", confirm=True),
            self._action_button("停止", "stop", confirm=True),
        ]

        nodes: List[dict] = [
            {"component": "VRow", "content": [
                kv("插件状态", "已启用" if live.get("enabled") else "未启用"),
                kv("统计周期", WEEK_MODE_LABELS.get(self._week_mode, self._week_mode)),
                kv("本周播放", f"{live.get('total_plays', 0)} 次"),
                kv("观看时长", format_duration(live.get("watch_seconds", 0))),
            ]},
            {"component": "div",
             "text": md_safe(
                 f"健康度：{level}（{level_note}）｜转码占比 {live.get('transcode_ratio', 0)}%"
                 f"｜建议 {live.get('findings_count', 0)} 条｜数据源 {len(servers)} 个"
                 + (f"｜上次触发 {last.get('trigger')}" if last.get("trigger") else "")),
             "props": {"style": "white-space:pre-wrap;font-size:12px;opacity:.8"}},
            {"component": "VRow", "props": {"class": "mt-3"}, "content": [
                {"component": "VCol", "content": op_buttons}]},
        ]
        return [{"component": "VCard", "props": {"variant": "tonal"}, "content": [
            {"component": "VCardText", "content": nodes}]}]

    # ------------------------------------------------------------------
    # API（与详情页按钮一一对应）
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 用相对路径：MP get_plugin_apis 会自动补 /{pid}，
        # 写成 /plugin/{NAME}/xxx 会重复前缀导致按钮 Not Found。
        return [
            {"path": "/run", "auth": "bear", "endpoint": self.api_run, "methods": ["POST"], "summary": "采集历史并生成周报与诊断"},
            {"path": "/diagnose", "auth": "bear", "endpoint": self.api_diagnose, "methods": ["POST"], "summary": "读取在播会话做实时转码诊断"},
            {"path": "/collect", "auth": "bear", "endpoint": self.api_collect, "methods": ["POST"], "summary": "仅采集近期播放历史"},
            {"path": "/push", "auth": "bear", "endpoint": self.api_push, "methods": ["POST"], "summary": "按当前数据推送周报与诊断"},
            {"path": "/test", "auth": "bear", "endpoint": self.api_test, "methods": ["POST"], "summary": "测试各媒体服务器连接"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear, "methods": ["POST"], "summary": "清空本地播放记录与周报缓存"},
            {"path": "/stop", "auth": "bear", "endpoint": self.api_stop, "methods": ["POST"], "summary": "停止当前任务"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status, "methods": ["GET"], "summary": "获取当前状态"},
            {"path": "/report", "auth": "bear", "endpoint": self.api_report, "methods": ["GET"], "summary": "获取当前周报"},
            {"path": "/findings", "auth": "bear", "endpoint": self.api_findings, "methods": ["GET"], "summary": "获取转码诊断建议"},
        ]

    def api_run(self, _=None) -> Response:
        started = self.start_run(trigger="API 生成周报", mode="full", prefer_thread=True)
        return Response(success=True, message="已触发周报生成" if started else "已有任务在运行",
                        data={"started": started})

    def api_diagnose(self, _=None) -> Response:
        started = self.start_run(trigger="API 转码诊断", mode="diagnose", prefer_thread=True)
        return Response(success=True, message="已触发转码诊断" if started else "已有任务在运行",
                        data={"started": started})

    def api_collect(self, _=None) -> Response:
        started = self.start_run(trigger="API 采集历史", mode="collect", prefer_thread=True)
        return Response(success=True, message="已触发历史采集" if started else "已有任务在运行",
                        data={"started": started})

    def api_push(self, _=None) -> Response:
        report = dict(self.get_data(_KEY_REPORT) or {})
        findings = [Finding.from_dict(f) for f in (self.get_data(_KEY_FINDINGS) or [])]
        if not report.get("total_plays") and not findings:
            return Response(success=False, message="暂无可推送的数据，请先「生成周报」")
        level, _ = health_level(report)
        parts: List[str] = []
        if report.get("total_plays"):
            parts.append(format_report_text(report))
        issues = [f for f in findings if f.severity != "info"]
        if issues:
            parts.append("转码诊断：" + "；".join(
                f"{SEVERITY_LABELS.get(f.severity, '提示')}·{f.title}（{f.count} 次）" for f in issues[:5]))
        self._send_message(f"观影周报 · {level}", "\n\n".join(parts) if parts else format_findings_text(findings))
        return Response(success=True, message="已推送周报/诊断")

    def api_test(self, _=None) -> Response:
        servers, warnings = self._resolve_servers()
        if AsyncRequestUtils is None:
            return Response(success=False, message="网络模块不可用")
        if not servers:
            return Response(success=False, message="未配置任何媒体服务器：" + "；".join(warnings))
        http = self._make_http()

        async def _check_all():
            out = []
            for srv in servers:
                client = build_media_client(srv, http, timeout=self._timeout)
                ok, msg = await client.check()
                out.append({"name": srv.name, "ok": ok, "message": msg})
            return out

        results = self._run_coroutine(_check_all())
        ok = all(r["ok"] for r in results)
        summary = "；".join(f"{r['name']}：{'正常' if r['ok'] else r['message']}" for r in results)
        self._log("INFO" if ok else "WARN", f"连接测试：{summary}")
        return Response(success=bool(ok), message=summary, data=results)

    def api_clear(self, _=None) -> Response:
        for key in (_KEY_RECORDS, _KEY_REPORT, _KEY_FINDINGS, _KEY_ACTIVE, _KEY_LAST):
            try:
                self.save_data(key, [] if key in (_KEY_RECORDS, _KEY_FINDINGS, _KEY_ACTIVE) else {})
            except Exception:  # noqa: BLE001
                pass
        self._update_live(record_count=0, total_plays=0, watch_seconds=0, transcode_ratio=0, findings_count=0)
        self._log("INFO", "已清空本地播放记录、周报与诊断缓存")
        return Response(success=True, message="已清空本地记录（不影响媒体服务器数据）")

    def api_stop(self, _=None) -> Response:
        self.stop_service()
        self._update_live(running=False, stage="已停止")
        return Response(success=True, message="已停止")

    def api_status(self, _=None) -> Response:
        return Response(success=True, data={
            "live": dict(self.get_data(_KEY_LIVE) or {}),
            "servers": [s.name for s in self._resolve_servers()[0]],
            "week_mode": self._week_mode, "cron": self._cron,
            "record_count": len(self.get_data(_KEY_RECORDS) or []),
        })

    def api_report(self, _=None) -> Response:
        return Response(success=True, data=dict(self.get_data(_KEY_REPORT) or {}))

    def api_findings(self, _=None) -> Response:
        return Response(success=True, data=list(self.get_data(_KEY_FINDINGS) or []))

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {"cmd": "/vwr_report", "event": EventType.PluginAction, "desc": "立即生成观影周报", "category": "MoviePilot"},
            {"cmd": "/vwr_diagnose", "event": EventType.PluginAction, "desc": "实时转码诊断", "category": "MoviePilot"},
            {"cmd": "/vwr_push", "event": EventType.PluginAction, "desc": "推送当前周报与诊断", "category": "MoviePilot"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data
        cmd = str(data.get("cmd") or "").lower()
        channel, userid = data.get("channel"), data.get("user_id")
        if cmd == "/vwr_report":
            started = self.start_run(trigger="远程命令", mode="full", prefer_thread=True)
            self._send_message("观影周报", "已触发周报生成" if started else "已有任务在运行",
                               channel=channel, userid=userid)
        elif cmd == "/vwr_diagnose":
            started = self.start_run(trigger="远程命令", mode="diagnose", prefer_thread=True)
            self._send_message("转码诊断", "已触发诊断" if started else "已有任务在运行",
                               channel=channel, userid=userid)
        elif cmd == "/vwr_push":
            self.api_push()
