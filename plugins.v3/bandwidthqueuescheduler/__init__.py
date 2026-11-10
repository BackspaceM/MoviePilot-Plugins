"""带宽/Queue 智能调度 —— MoviePilot V3 插件主类。

在 RouterOS 上按"带宽方案(profile)"智能编排简单队列：
- 用户定义设备(名称=IP|角色)，内置均衡/观影优先/下载优先/夜间全速四套方案，支持自定义 JSON；
- 可按"时段规则"自动切换方案（支持跨午夜），也可在详情页一键切换；
- 每轮调度后读取队列实时速率，并诊断 FastTrack 是否绕过 Queue 导致限速失效；
- 所有队列统一管理前缀，绝不触碰用户手工队列；试运行模式只评估不落盘。

详情页每个按钮都绑定真实 API endpoint（含带参的方案切换），不存在"点了没反应"的按钮；
所有动态文本经 md_safe 转义，避免被前端当 Markdown 渲染成空药丸。
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
    from app import schemas

    Response = schemas.Response
except Exception:  # noqa: BLE001
    from pydantic import BaseModel

    class Response(BaseModel):  # type: ignore
        """宿主不可用时的三字段 envelope 降级实现。"""

        success: bool = True
        message: str = ""
        data: Any = None


# 带参按钮的 POST body 由 FastAPI 注入；离线测试环境降级为 None。
try:
    from fastapi import Body
    _BODY_DEFAULT = Body(default=None)
except Exception:  # noqa: BLE001
    _BODY_DEFAULT = None  # type: ignore

try:
    _PLUGIN_MTYPE = MessageType.Plugin
except Exception:  # noqa: BLE001
    _PLUGIN_MTYPE = None

from .clients import (
    RouterOSQueueClient,
    build_queue_client,
)
from .core import (
    DEFAULT_PROFILE,
    ROLE_LABELS,
    ROLES,
    available_profiles,
    build_queue_specs,
    format_apply_text,
    format_fasttrack_text,
    format_log_line,
    format_rates_text,
    mask_secrets,
    match_profile,
    md_safe,
    merge_profiles,
    parse_bandwidth,
    parse_profile_json,
    parse_rules,
    parse_targets,
    summary_text,
    validate_cron,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PLUGIN_NAME = "BandwidthQueueScheduler"
PLUGIN_ICON = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/bandwidthqueuescheduler.png"
PLUGIN_DESCRIPTION = "在 RouterOS 上按带宽方案智能编排简单队列：内置均衡/观影/下载/夜间四套方案，支持时段自动切换、一键手动切换、实时速率监控与 FastTrack 失效诊断，统一管理前缀不误伤手工队列。"
PLUGIN_AUTHORS = "BackspaceM"
AUTHOR_URL = "https://github.com/BackspaceM"
PLUGIN_COLOR = "#3498DB"
SYSTEM_VERSION = ">=3.0.0"

_LOG_PREFIX = "带宽队列调度"
_ONCE_JOB_ID = "bqs_once"
_MAX_LOGS = 200
_DEFAULT_CRON = "*/5 * * * *"        # 每 5 分钟评估一次时段方案
_DEFAULT_PREFIX = "bqs"
_DEFAULT_TARGETS = (
    "下载机=192.168.1.30/32|download\n"
    "NAS=192.168.1.10/32|nas\n"
    "客厅电视=192.168.1.20/32|media"
)
_DEFAULT_RULES = (
    "00:00-07:00=night\n"
    "07:00-18:00=balanced\n"
    "18:00-24:00=movie"
)

# plugindata keys
_KEY_LIVE = "bqs_live"
_KEY_LOGS = "bqs_logs"
_KEY_LAST = "bqs_last"
_KEY_PROFILE = "bqs_profile"         # 当前生效方案 key
_KEY_RATES = "bqs_rates"             # 最近一次队列速率列表
_KEY_FASTTRACK = "bqs_fasttrack"     # 最近一次 FastTrack 诊断

_NOTIFY_LABELS = {
    "change": "仅变更/异常（推荐）",
    "all": "每次调度",
    "none": "关闭",
}


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _ts_text(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return str(ts)



from . import roslink
class BandwidthQueueScheduler(_PluginBase):
    plugin_name = PLUGIN_NAME
    plugin_desc = PLUGIN_DESCRIPTION
    plugin_icon = PLUGIN_ICON
    plugin_version = "1.2.0"
    plugin_author = PLUGIN_AUTHORS
    author_url = AUTHOR_URL
    plugin_config_prefix = "bandwidthqueuescheduler_"
    plugin_color = PLUGIN_COLOR
    system_version = SYSTEM_VERSION
    plugin_order = 55

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
        # 默认配置，保证 init_plugin 前调用 get_form/get_page 也不崩
        self._address = ""
        self._port = 443
        self._use_ssl = True
        self._username = ""
        self._password = ""
        self._prefix = _DEFAULT_PREFIX
        self._targets: List[Dict[str, str]] = []
        self._custom_profiles: Dict[str, Any] = {}
        self._profiles = merge_profiles()
        self._default_profile = DEFAULT_PROFILE
        self._rules: List[Dict[str, Any]] = []
        self._cron = _DEFAULT_CRON
        self._auto_apply = True
        self._notify_mode = "change"
        self._timeout = 10
        self._dry_run = False

    def init_plugin(self, config: dict) -> None:
        config = dict(config or {})
        self.stop_service()

        # 连接中心：选中已保存路由器时带入连接参数；未选中/找不到则保持原配置不变
        _ccfg = dict(config or {})
        _rcc, _rcc_note = roslink.connection_overrides(_ccfg.get("ros_connection"), "std")
        _ccfg.update(_rcc)
        if _rcc_note:
            (logger.info if _rcc else logger.warning)(f"RouterOS 连接中心：{_rcc_note}")
        self._enabled = bool(config.get("enabled"))

        self._address = str(_ccfg.get("router_address") or "").strip()
        self._port = int(_ccfg.get("router_port") or 443)
        self._use_ssl = bool(_ccfg.get("router_ssl", True))
        self._username = str(_ccfg.get("router_username") or "").strip()
        self._password = str(_ccfg.get("router_password") or "")

        self._prefix = "".join(
            c for c in str(config.get("queue_prefix") or _DEFAULT_PREFIX).strip()
            if c.isalnum() or c in "_-") or _DEFAULT_PREFIX

        self._targets = parse_targets(config.get("targets"))
        self._custom_profiles = parse_profile_json(config.get("custom_profiles"))
        self._profiles = merge_profiles(self._custom_profiles)

        self._default_profile = str(config.get("default_profile") or DEFAULT_PROFILE).strip().lower()
        if self._default_profile not in self._profiles:
            self._default_profile = DEFAULT_PROFILE
        self._rules = parse_rules(config.get("schedule_rules"))

        self._cron = str(config.get("cron") or _DEFAULT_CRON).strip() or _DEFAULT_CRON
        if not validate_cron(self._cron)[0]:
            self._cron = _DEFAULT_CRON
        self._auto_apply = bool(config.get("auto_apply", True))
        self._notify_mode = str(config.get("notify_mode") or "change").strip() or "change"
        self._timeout = max(3, min(60, int(config.get("timeout") or 10)))
        self._dry_run = bool(config.get("dry_run"))

        self._update_live(enabled=self._enabled, running=False, stage="待命")

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

    def _secrets(self) -> List[str]:
        return [self._password] if self._password else []

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
            "name": "带宽/Queue 智能调度",
            "trigger": trigger,
            "func": self.start_run,
            "kwargs": {"trigger": "定时轮询", "prefer_thread": True},
        }]

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        defaults: Dict[str, Any] = {
            "enabled": False,
            "onlyonce": False,
            "router_address": "",
            "ros_connection": "",
            "router_port": 443,
            "router_ssl": True,
            "router_username": "",
            "router_password": "",
            "queue_prefix": _DEFAULT_PREFIX,
            "targets": _DEFAULT_TARGETS,
            "default_profile": DEFAULT_PROFILE,
            "schedule_rules": _DEFAULT_RULES,
            "custom_profiles": "",
            "cron": _DEFAULT_CRON,
            "auto_apply": True,
            "notify_mode": "change",
            "timeout": 10,
            "dry_run": False,
        }

        def fld(model: str, label: str, props: Optional[dict] = None) -> dict:
            p = {"model": model, "label": label}
            if props:
                p.update(props)
            return {"component": "VTextField", "props": p}

        def area(model: str, label: str, hint: str, placeholder: str = "", rows: int = 4) -> dict:
            return {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VTextarea", "props": {
                    "model": model, "label": label, "hint": hint, "persistent-hint": True,
                    "placeholder": placeholder, "rows": rows, "auto-grow": True,
                }},
            ]}

        profile_items = [
            {"title": f"{p['label']}（{p['key']}）", "value": p["key"]}
            for p in available_profiles(getattr(self, "_profiles", None) or merge_profiles())
        ]

        content: List[dict] = [
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-2"},
                 "text": "连接中心"},
                {"component": "VCardText", "content": [
                    {"component": "VSelect", "props": {
                        "model": "ros_connection",
                        "label": "已保存的路由器",
                        "items": roslink.connection_items(),
                        "clearable": True,
                        "hint": "选中后自动带入连接参数，下面可以留空；不选就用手填的。",
                        "persistentHint": True}},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": "下拉是空的就直接手填，不影响使用。"},
                ]},
            ]},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                              "class": "mb-4"},
             "content": {"text": "按时段在 RouterOS 上切换简单队列，也能在详情页手动切。"
                                 "只管前缀开头的队列，你手工建的不动。"}},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                                              "class": "mb-3"},
             "content": {"text": "开了 FastTrack 的流量不走 Queue，限速会看着像没生效，详情页有「FastTrack 诊断」。"
                                 "账号要有 read/write/api 权限，第一次用先开「试运行」看看。"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件", "hint": "关闭后不再定时调度"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即执行一次", "hint": "保存后按当前方案调度一轮"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "dry_run", "label": "试运行", "hint": "只评估差异，不实际改动队列"}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"}, "text": "RouterOS 连接"},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VTextField", "props": {"model": "router_address", "label": "RouterOS 地址",
                     "placeholder": "192.168.1.1 或 https://ros.local"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [
                    fld("router_port", "端口", {"type": "number"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "router_ssl", "label": "使用 HTTPS", "hint": "关闭则用 HTTP"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("router_username", "用户名", {"placeholder": "需具备 read/write/api 权限"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("router_password", "密码", {"type": "password"})]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"}, "text": "队列与默认方案"},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("queue_prefix", "队列名前缀",
                        {"hint": "插件只管理 前缀_ 开头的队列", "persistent-hint": True})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "default_profile", "label": "默认方案", "items": profile_items,
                        "hint": "无时段规则命中时使用", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {
                        "model": "auto_apply", "label": "按规则自动切换", "hint": "关闭则保持当前方案"}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"}, "text": "设备与时段规则"},
            {"component": "VRow", "content": [
                area("targets", "设备定义（每行一个）",
                     "格式：名称=IP/CIDR|角色；角色可选 download/nas/media/other，省略为 other。",
                     _DEFAULT_TARGETS, 5),
            ]},
            {"component": "VRow", "content": [
                area("schedule_rules", "时段自动切换规则（每行一个）",
                     "格式：HH:MM-HH:MM=方案；支持跨午夜；多条重叠时后写的优先。留空则始终用默认方案。",
                     _DEFAULT_RULES, 4),
            ]},
            {"component": "VRow", "content": [
                area("custom_profiles", "自定义方案 JSON（高级，可选）",
                     "同 key 覆盖内置、新 key 新增。留空使用内置四套方案。",
                     '{"movie":{"label":"观影优先·自定义","rules":{"download":{"max":"1M/3M","priority":8}}}}', 4),
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"}, "text": "调度与通知"},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("cron", "调度周期(cron)", {"placeholder": "*/5 * * * *  每 5 分钟"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("timeout", "请求超时(秒)", {"type": "number"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSelect", "props": {"model": "notify_mode", "label": "通知方式",
                     "items": [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()]}}]},
            ]},
            {"component": "VAlert", "props": {"type": "info", "variant": "text",
                                              "class": "mt-2"},
             "content": {"text": "角色：download 是下载机、nas 是服务器、media 是观影设备（不限速）。"
                                 "速率写「上/下行」，如 1M/3M，0/0 不限速；时段 HH:MM-HH:MM=方案，重叠时后写的优先。"}},
        ]
        return content, defaults

    # ------------------------------------------------------------------
    # 执行控制
    # ------------------------------------------------------------------

    def start_run(self, trigger: str = "手动", prefer_thread: bool = False,
                  profile: Optional[str] = None) -> bool:
        with self._state_lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()
        self._update_live(running=True, stage=f"{trigger}：准备队列", trigger=trigger)

        def _task() -> None:
            try:
                result = self._run_coroutine(self._async_run(trigger, profile_override=profile))
                self.save_data(_KEY_LAST, result)
                self._notify_result(result, trigger)
            except Exception as err:  # noqa: BLE001
                self._log("ERROR", f"调度任务异常：{err}\n{traceback.format_exc()[-800:]}")
                self._send_message("带宽调度异常", f"触发：{trigger}\n错误：{err}")
            finally:
                with self._state_lock:
                    self._running = False
                self._update_live(running=False, stage="待命")

        if prefer_thread:
            self._thread = threading.Thread(target=_task, daemon=True, name="BQS-Run")
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

    def _make_client(self) -> Tuple[Optional[RouterOSQueueClient], Any]:
        if AsyncRequestUtils is None or not self._address:
            return None, None
        http = AsyncRequestUtils(verify=self._use_ssl is False)
        client = build_queue_client(
            self._address, self._username, self._password, http,
            prefix=self._prefix, port=self._port, use_ssl=self._use_ssl,
            timeout=self._timeout, dry_run=self._dry_run,
        )
        return client, http

    async def _async_run(self, trigger: str,
                         profile_override: Optional[str] = None) -> Dict[str, Any]:
        started = time.time()
        result: Dict[str, Any] = {
            "trigger": trigger, "ok": False, "profile": "", "auto": False,
            "target_count": len(self._targets), "identity": {},
            "apply": None, "rates": [], "fasttrack": None,
            "duration": 0.0, "message": "",
        }
        if AsyncRequestUtils is None:
            result["message"] = "网络模块不可用"
            self._log("ERROR", "网络模块 AsyncRequestUtils 不可用")
            return result
        if not self._address:
            result["message"] = "未配置 RouterOS 地址"
            self._log("WARN", "未配置 RouterOS 地址，跳过")
            return result

        client, http = self._make_client()

        # 1) 连通性
        ok, msg = await client.check(client=http)
        self._log("INFO" if ok else "WARN", f"【{trigger}】 RouterOS 连通性：{msg}")
        if not ok:
            result["message"] = msg
            return result

        # 2) 选定方案
        if profile_override:
            profile = str(profile_override).strip().lower()
            auto = False
            if profile not in self._profiles:
                result["message"] = f"未知方案 {profile}"
                self._log("WARN", result["message"])
                return result
        elif self._auto_apply:
            profile = match_profile(self._rules, self._default_profile)
            auto = True
        else:
            profile = str(self.get_data(_KEY_PROFILE) or self._default_profile)
            auto = False
        result["profile"] = profile
        result["auto"] = auto
        self._log("INFO", f"【{trigger}】 当前方案：{profile}"
                          f"（{'时段自动' if auto else '手动指定'}）")

        # 3) 构建期望队列并应用差异
        specs = build_queue_specs(profile, self._targets, self._prefix, self._profiles)
        if not specs:
            result["message"] = "未配置任何设备，请先在配置页填写「设备定义」"
            self._log("WARN", result["message"])
            return result
        apply = await client.apply_specs(specs, client=http)
        apply["profile"] = profile
        result["apply"] = apply
        for note in apply.get("notes", []):
            self._log("INFO", f"【{trigger}】 {note}")
        for err in apply.get("errors", []):
            self._log("WARN", f"【{trigger}】 队列操作失败：{err}")

        # 4) 读取实时速率 + FastTrack 诊断（即使有单项失败也尽量读取）
        rates = await client.read_rates(client=http)
        result["rates"] = rates
        fasttrack = await client.check_fasttrack(client=http)
        result["fasttrack"] = fasttrack
        if fasttrack.get("has_fasttrack"):
            self._log("WARN", f"【{trigger}】 检测到 FastTrack，匹配的流量会绕过 Queue："
                              + "；".join(fasttrack.get("rules", [])[:3]))

        # 5) 持久化（试运行不落盘）
        if not self._dry_run:
            self.save_data(_KEY_PROFILE, profile)
            self.save_data(_KEY_RATES, rates)
            self.save_data(_KEY_FASTTRACK, fasttrack)
        self._update_live(
            current_profile=profile, profile_auto=auto,
            last_run=time.time(), rate_count=len(rates),
            fasttrack_warn=bool(fasttrack.get("has_fasttrack")),
        )

        result["ok"] = bool(apply.get("ok"))
        if not result["ok"]:
            result["message"] = "部分队列操作失败：" + "；".join(apply.get("errors", []))
        else:
            result["message"] = summary_text(apply) + (
                "；⚠ FastTrack 可能使限速失效" if fasttrack.get("has_fasttrack") else "")
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

    def _notify_result(self, result: Dict[str, Any], trigger: str) -> None:
        mode = self._notify_mode
        if mode == "none":
            return
        apply = result.get("apply") or {}
        changed = bool(apply.get("created") or apply.get("updated") or apply.get("removed"))
        issue = (not result.get("ok")) or bool(apply.get("errors"))
        if mode == "change" and not (changed or issue):
            return

        lines = [f"触发：{trigger}",
                 f"方案：{result.get('profile', '')}"
                 f"（{'时段自动' if result.get('auto') else '手动指定'}）"]
        if not result.get("ok"):
            lines.append("执行异常：" + result.get("message", ""))
        else:
            lines.append(format_apply_text(apply, max_rows=8))
        fasttrack = result.get("fasttrack") or {}
        if fasttrack.get("has_fasttrack"):
            lines.append("⚠ 检测到 FastTrack，部分流量可能绕过 Queue 导致限速无效")
        lines.append(f"耗时 {result.get('duration', 0)}s")
        title = "带宽调度异常" if issue else ("带宽方案已切换" if changed else "带宽调度完成")
        self._send_message(title, "\n".join(lines))

    def current_status(self) -> Dict[str, Any]:
        live = dict(self.get_data(_KEY_LIVE) or {})
        last = dict(self.get_data(_KEY_LAST) or {})
        return {
            "enabled": self.get_state(),
            "running": bool(getattr(self, "_running", False)),
            "router": self._address,
            "prefix": self._prefix,
            "targets": self._targets,
            "target_count": len(self._targets),
            "profiles": [p["key"] for p in available_profiles(self._profiles)],
            "profile": self.get_data(_KEY_PROFILE) or self._default_profile,
            "default_profile": self._default_profile,
            "rules": self._rules,
            "cron": self._cron,
            "auto_apply": self._auto_apply,
            "dry_run": self._dry_run,
            "last": last,
            "live": live,
        }

    # ------------------------------------------------------------------
    # 详情页（每个按钮都绑定真实 API，且不使用 disabled）
    # ------------------------------------------------------------------

    def _action_button(self, label: str, api: str, *, params: Optional[dict] = None,
                       confirm: bool = False, variant: str = "tonal",
                       color: Optional[str] = None, icon: str = "",
                       method: str = "post", size: str = "small") -> dict:
        """详情页按钮：文字放组件级 text，带颜色/图标，避免出现灰色空按钮。

        所有按钮都指向 get_api() 中已注册的真实 endpoint，不使用 disabled。
        """
        props: Dict[str, Any] = {"variant": variant, "size": size, "class": "ma-1"}
        if color:
            props["color"] = color
        if icon:
            props["prepend-icon"] = icon
        event: Dict[str, Any] = {
            "api": f"plugin/{PLUGIN_NAME}/{api}", "method": method,
            "params": params or {}, "confirm": confirm,
        }
        return {"component": "VBtn", "props": props, "text": label,
                "events": {"click": event}}

    def get_page(self) -> List[dict]:
        """详情页：卡片分区 + 分组彩色按钮，所有按钮指向真实 API。"""
        status = self.current_status()
        last = status.get("last") or {}
        apply = last.get("apply") or {}
        rates = last.get("rates")
        if rates is None:
            rates = self.get_data(_KEY_RATES) or []
        fasttrack = last.get("fasttrack")
        if fasttrack is None:
            fasttrack = self.get_data(_KEY_FASTTRACK)
        if not isinstance(fasttrack, dict):
            fasttrack = None
        logs = list(self.get_data(_KEY_LOGS) or [])
        current = status.get("profile") or "—"

        def kv(label: str, value: Any, md: int = 3) -> dict:
            return {"component": "VCol", "props": {"cols": 6, "md": md}, "content": [
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                 "text": label},
                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"},
                 "text": md_safe(str(value))},
            ]}

        def text_row(value: str, size: int = 12) -> dict:
            return {"component": "div", "text": md_safe(value),
                    "props": {"style": f"white-space:pre-wrap;font-size:{size}px"}}

        profile_buttons = []
        for prof in available_profiles(self._profiles):
            active = prof["key"] == current
            profile_buttons.append(self._action_button(
                f"应用·{prof['label']}", "apply_profile",
                params={"profile": prof["key"]},
                variant="flat" if active else "tonal",
                color="primary" if active else "",
                icon="mdi-check-circle" if active else "mdi-speedometer"))

        primary = [
            self._action_button("立即按规则调度", "run", color="primary",
                                icon="mdi-play", variant="flat"),
            self._action_button("刷新队列状态", "refresh", color="info", icon="mdi-refresh"),
        ]
        secondary = [
            self._action_button("FastTrack 诊断", "fasttrack", icon="mdi-radar"),
            self._action_button("测试连接", "test", icon="mdi-lan-connect"),
        ]
        danger = [
            self._action_button("清空本插件队列", "clear_queues", color="error",
                                icon="mdi-delete-outline", confirm=True),
            self._action_button("停止", "stop", color="error",
                                icon="mdi-stop-circle-outline", confirm=True),
        ]

        rate_text = format_rates_text(rates, self._prefix)
        fast_text = format_fasttrack_text(fasttrack)
        apply_text = format_apply_text(apply) if apply else "尚无调度记录，点击「立即按规则调度」开始"
        log_nodes = [text_row(format_log_line(l.get("time", ""), l.get("level", ""),
                                              l.get("message", "")))
                     for l in logs[:20]] or [text_row("暂无运行日志")]

        return [
            {"component": "VAlert", "props": {
                "type": "warning" if (fasttrack or {}).get("has_fasttrack") else "info",
                "variant": "tonal", "class": "mb-3", "title": "FastTrack"},
             "content": {"text": md_safe(fast_text)}},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": "运行概览"},
                {"component": "VCardText", "content": [
                    {"component": "VRow", "content": [
                        kv("插件状态", "已启用" if status.get("enabled") else "未启用"),
                        kv("当前任务", "运行中" if status.get("running") else "待命"),
                        kv("当前方案", current),
                        kv("方案来源", "时段自动" if last.get("auto") else "手动指定"),
                        kv("调度周期", self._cron),
                        kv("设备数量", status.get("target_count", 0)),
                        kv("队列前缀", self._prefix),
                        kv("试运行", "是" if self._dry_run else "否"),
                    ]},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                 "text": "一键切换带宽方案（点击即刻改写 RouterOS 队列）"},
                {"component": "VCardText", "content": [
                    {"component": "div", "content": profile_buttons},
                ]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VCard", "props": {"variant": "tonal", "class": "h-100"},
                    "content": [
                        {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                         "text": "最近一次调度结果"},
                        {"component": "VCardText", "content": [text_row(apply_text, 13),
                                                              {"component": "VDivider",
                                                               "props": {"class": "my-2"}},
                                                              text_row(rate_text)]},
                    ]}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VCard", "props": {"variant": "tonal", "class": "h-100"},
                    "content": [
                        {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                         "text": "FastTrack 诊断"},
                        {"component": "VCardText", "content": [text_row(fast_text)]},
                    ]}]},
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "my-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                 "text": "运行日志（最近 20 条）"},
                {"component": "VCardText", "content": log_nodes},
            ]},
            {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": "操作"},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": "调度与刷新"},
                    {"component": "div", "content": primary},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": "诊断"},
                    {"component": "div", "content": secondary},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": "清理与停止"},
                    {"component": "div", "content": danger},
                ]},
            ]},
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        # path 为相对路径：宿主自动补齐 /api/v1/plugin/<PluginID> 前缀
        return [
            {"path": "/run", "auth": "bear", "endpoint": self.api_run, "methods": ["POST"], "summary": "按当前规则立即调度"},
            {"path": "/apply_profile", "auth": "bear", "endpoint": self.api_apply_profile, "methods": ["POST"], "summary": "手动切换带宽方案"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh, "methods": ["POST"], "summary": "刷新队列实时速率"},
            {"path": "/fasttrack", "auth": "bear", "endpoint": self.api_fasttrack, "methods": ["POST"], "summary": "诊断 FastTrack 是否绕过 Queue"},
            {"path": "/test", "auth": "bear", "endpoint": self.api_test, "methods": ["POST"], "summary": "测试 RouterOS 连接"},
            {"path": "/clear_queues", "auth": "bear", "endpoint": self.api_clear_queues, "methods": ["POST"], "summary": "删除本插件创建的全部队列"},
            {"path": "/stop", "auth": "bear", "endpoint": self.api_stop, "methods": ["POST"], "summary": "停止当前任务"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status, "methods": ["GET"], "summary": "获取当前状态"},
            {"path": "/profiles", "auth": "bear", "endpoint": self.api_profiles, "methods": ["GET"], "summary": "获取可用带宽方案"},
        ]

    def api_run(self, _=None) -> Response:
        started = self.start_run(trigger="API 立即调度", prefer_thread=True)
        return Response(success=True,
                        message="已触发调度" if started else "已有任务在运行",
                        data={"started": started})

    def api_apply_profile(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        profile = ""
        if isinstance(payload, dict):
            profile = str(payload.get("profile") or "").strip().lower()
        elif isinstance(payload, str):
            profile = payload.strip().lower()
        if not profile:
            return Response(success=False, message="缺少方案参数 profile")
        if profile not in self._profiles:
            return Response(success=False, message=f"未知方案：{profile}")
        started = self.start_run(trigger=f"手动切换-{profile}", prefer_thread=True, profile=profile)
        return Response(success=True,
                        message=f"已触发应用方案 {profile}" if started else "已有任务在运行",
                        data={"started": started, "profile": profile})

    def api_refresh(self, _=None) -> Response:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置地址")
        rates = self._run_coroutine(client.read_rates(client=http))
        self.save_data(_KEY_RATES, rates)
        self._update_live(rate_count=len(rates), stage="待命")
        return Response(success=True, message=f"已刷新 {len(rates)} 条队列速率", data=rates)

    def api_fasttrack(self, _=None) -> Response:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置地址")
        info = self._run_coroutine(client.check_fasttrack(client=http))
        self.save_data(_KEY_FASTTRACK, info)
        if info.get("error"):
            return Response(success=False, message=f"诊断失败：{info['error']}", data=info)
        return Response(success=True,
                        message=("⚠ 检测到 FastTrack，限速可能被绕过" if info.get("has_fasttrack")
                                 else "未发现启用的 FastTrack，Queue 可正常生效"),
                        data=info)

    def api_test(self, _=None) -> Response:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置地址")
        ok, msg = self._run_coroutine(client.check(client=http))
        return Response(success=bool(ok), message=mask_secrets(msg, self._secrets()))

    def api_clear_queues(self, _=None) -> Response:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置地址")
        res = self._run_coroutine(client.clear_our_queues(client=http))
        self.save_data(_KEY_RATES, [])
        self._log("INFO", f"已清空本插件队列 {len(res.get('removed', []))} 条")
        return Response(
            success=bool(res.get("ok")),
            message=f"已删除 {len(res.get('removed', []))} 条本插件队列"
                    + ("；部分删除失败" if res.get("errors") else ""),
            data=res,
        )

    def api_stop(self, _=None) -> Response:
        self.stop_service()
        self._update_live(running=False, stage="已停止")
        return Response(success=True, message="已停止")

    def api_status(self, _=None) -> Response:
        return Response(success=True, data=self.current_status())

    def api_profiles(self, _=None) -> Response:
        return Response(success=True, data=available_profiles(self._profiles))

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------

    def get_command(self) -> List[Dict[str, Any]]:
        commands = [
            {"cmd": "/bqs_status", "event": EventType.PluginAction, "desc": "查看当前带宽方案与队列速率", "category": "MoviePilot"},
            {"cmd": "/bqs_run", "event": EventType.PluginAction, "desc": "按当前规则立即调度队列", "category": "MoviePilot"},
            {"cmd": "/bqs_refresh", "event": EventType.PluginAction, "desc": "刷新队列实时速率", "category": "MoviePilot"},
            {"cmd": "/bqs_fasttrack", "event": EventType.PluginAction, "desc": "诊断 FastTrack 是否绕过 Queue", "category": "MoviePilot"},
        ]
        for prof in available_profiles(self._profiles):
            commands.append({
                "cmd": f"/bqs_{prof['key']}", "event": EventType.PluginAction,
                "desc": f"切换到方案：{prof['label']}", "category": "MoviePilot",
            })
        return commands

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data
        cmd = str(data.get("cmd") or "").lower()
        channel = data.get("channel")
        userid = data.get("user_id")
        static = {
            "/bqs_status": self._cmd_status,
            "/bqs_run": self._cmd_run,
            "/bqs_refresh": self._cmd_refresh,
            "/bqs_fasttrack": self._cmd_fasttrack,
        }
        if cmd in static:
            static[cmd](channel, userid)
            return
        if cmd.startswith("/bqs_"):
            profile = cmd[len("/bqs_"):]
            if profile in self._profiles:
                self._cmd_apply(profile, channel, userid)
            else:
                self._send_message("带宽调度", f"未知方案：{profile}", channel=channel, userid=userid)

    def _cmd_status(self, channel, userid) -> None:
        status = self.current_status()
        last = status.get("last") or {}
        apply = last.get("apply") or {}
        text = (f"当前方案：{status.get('profile')}\n"
                f"设备：{status.get('target_count', 0)} 个｜队列前缀：{status.get('prefix')}\n"
                f"最近：{summary_text(apply) if apply else '尚未调度'}")
        if (last.get("fasttrack") or {}).get("has_fasttrack"):
            text += "\n⚠ 检测到 FastTrack，限速可能被绕过"
        self._send_message("带宽调度 · 状态", text, channel=channel, userid=userid)

    def _cmd_run(self, channel, userid) -> None:
        started = self.start_run(trigger="远程命令", prefer_thread=True)
        self._send_message("带宽调度", "已触发调度" if started else "已有任务在运行",
                           channel=channel, userid=userid)

    def _cmd_refresh(self, channel, userid) -> None:
        client, http = self._make_client()
        if client is None:
            self._send_message("带宽调度", "网络模块不可用或未配置地址", channel=channel, userid=userid)
            return
        rates = self._run_coroutine(client.read_rates(client=http))
        self.save_data(_KEY_RATES, rates)
        self._send_message("带宽调度 · 队列速率", format_rates_text(rates, self._prefix),
                           channel=channel, userid=userid)

    def _cmd_fasttrack(self, channel, userid) -> None:
        client, http = self._make_client()
        if client is None:
            self._send_message("带宽调度", "网络模块不可用或未配置地址", channel=channel, userid=userid)
            return
        info = self._run_coroutine(client.check_fasttrack(client=http))
        self.save_data(_KEY_FASTTRACK, info)
        self._send_message("带宽调度 · FastTrack", format_fasttrack_text(info),
                           channel=channel, userid=userid)

    def _cmd_apply(self, profile: str, channel, userid) -> None:
        started = self.start_run(trigger=f"命令切换-{profile}", prefer_thread=True, profile=profile)
        self._send_message("带宽调度", f"{'已触发应用' if started else '已有任务在运行'}方案 {profile}",
                           channel=channel, userid=userid)
