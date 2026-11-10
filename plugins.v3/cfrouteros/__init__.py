"""Cloudflare 优选 IP → RouterOS DNS static（MoviePilot V3 插件）。

流程：
1. 收集目标域名：宿主里已启用的私有 PT 站点 + 用户自定义域名（支持排除列表）；
2. 站点判定：默认「不检测，全部纳入」，彻底不依赖 DoH；可选系统 DNS 或多源 DoH
   判断解析 IP 是否落在 Cloudflare CDN 网段，判定失败仍可按配置纳入；
3. 加载 IP 来源：Cloudflare 官方 ips-v4/ips-v6（自动刷新 + 缓存）+ 第三方/个人
   维护的 IP 池 URL（多个）+ 手动填写的 IP/CIDR，池内 IP 优先参与测速；
4. 并发 TCP(/TLS) 握手测速，取最快若干个；
5. 通过 RouterOS v7 REST 写入 /ip/dns/static（A/AAAA 记录，带 `cfopt:<域名>` 备注），
   先写新记录、成功后再清理旧记录，避免把 DNS 清空；
6. 逐站点的原始 IP、判定、优选 IP 与写入结果全部以 INFO 级写宿主插件日志，
   MP「查看日志」窗口通过 SSE 实时推送，详情页另有实时进度与运行日志。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import html as _html
import re
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.schemas.types import EventType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import AsyncRequestUtils, SitesHelper
from app.sdk.plugin import _PluginBase

try:
    # V3 宿主的插件通知类型；旧宿主或离线桩环境没有该枚举时退化为宿主默认类型
    from app.schemas.types import MessageType as _MessageType
    _PLUGIN_MTYPE = getattr(_MessageType, "Plugin", None)
except Exception:  # noqa: BLE001
    _PLUGIN_MTYPE = None

from .cfopt import (
    CF_V4_LIST_URL,
    CF_V6_LIST_URL,
    CHECK_MODES,
    DEFAULT_IPV4_CIDRS,
    DEFAULT_IPV6_CIDRS,
    DEFAULT_PROBE_SNI,
    ProbeResult,
    build_candidate_pool,
    build_networks,
    build_record,
    classify_domain,
    is_cloudflare_ip,
    format_ip_list,
    format_latency,
    merge_networks,
    normalize_domains,
    parse_cidrs,
    parse_doh_answer,
    parse_doh_urls,
    parse_ip_pool,
    pick_fastest,
    probe_many,
    resolve_system_dns,
    sample_ips,
)
from .routeros import RouterOSClient, normalize_base_url

_STATS_KEY = "stats"
_HISTORY_KEY = "history"
_CIDR_KEY = "cidr_cache"
_LOG_KEY = "run_logs"
_LIVE_KEY = "live"
_POOL_KEY = "pool_sources"
_MAX_HISTORY = 50
_MAX_LOGS = 500
_CMD_ACTION = "cfrouteros_run"
_CMD_CHECK_ACTION = "cfrouteros_check"
_CMD_STOP_ACTION = "cfrouteros_stop"
_CMD_POOL_ACTION = "cfrouteros_pool"
_ONCE_JOB_ID = "CFRouterOS.RunOnce"
_STALE_RUN_SECONDS = 1800
_SITE_ONLY_ERROR = "未配置任何域名"
_NOTIFY_MODES = ("全部", "仅异常", "关闭")
_CIDR_SOURCES = ("official+custom", "official", "custom", "builtin")
_CHECK_LABELS = {
    "all": "按配置直接纳入",
    "system": "系统 DNS 判定",
    "doh": "多源 DoH 判定",
}


# ---------------------------------------------------------------------------
# 配置值转换
# ---------------------------------------------------------------------------

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


def _to_str_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    return []


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mask_secrets(text: str, password: str = "") -> str:
    """日志脱敏：密码、URL 内嵌凭据与常见令牌参数。"""
    content = str(text)
    if password:
        content = content.replace(password, "***")
    content = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1***:***@", content)
    content = re.sub(
        r"((?:token|key|passkey|password|secret|auth)=)[^\s&]+",
        r"\1***", content, flags=re.I,
    )
    return content



from . import roslink
class CFRouterOS(_PluginBase):
    """Cloudflare 优选 IP 写入 RouterOS DNS static。"""

    plugin_name = "Cloudflare 优选 IP → RouterOS"
    plugin_desc = (
        "识别 Cloudflare CDN 站点，实测官方网段与第三方 IP 池内的 IP 延迟，"
        "取最优若干写入 RouterOS 的 /ip/dns/static，实现内网 DNS 优选解析。"
    )
    plugin_icon = (
        "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/cfrouteros.png"
    )
    plugin_version = "1.3.0"
    plugin_label = "网络,Cloudflare,RouterOS,DNS"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "cfrouteros_"
    plugin_order = 77
    auth_level = 1

    # --- 基础 ---
    _enabled: bool = False
    _notify: bool = True
    _notify_mode: str = "全部"
    _cron: str = "0 5 * * *"
    _dry_run: bool = False

    # --- RouterOS ---
    _router_host: str = ""
    _router_port: int = 443
    _router_ssl: bool = True
    _router_verify: bool = False
    _router_user: str = "admin"
    _router_password: str = ""
    _router_timeout: float = 10.0

    # --- 目标域名与站点判定 ---
    _domains: str = ""
    _exclude_domains: str = ""
    _auto_sites: bool = True
    _check_mode: str = "all"
    _check_fallback: bool = True
    _log_resolved_ips: bool = True
    _doh_urls: str = ""
    _doh_url: str = ""
    _doh_timeout: float = 5.0

    # --- IP 来源 ---
    _refresh_cidrs: bool = True
    _cidr_source: str = "official+custom"
    _official_v4_url: str = CF_V4_LIST_URL
    _official_v6_url: str = CF_V6_LIST_URL
    _pool_urls: str = ""
    _pool_text: str = ""
    _pool_refresh_days: int = 7
    _pool_timeout: float = 10.0
    _pool_as_candidates: bool = True
    _pool_strict_cf: bool = False

    # --- 优选 ---
    _ip_version: str = "4"
    _probe_count: int = 80
    _probe_port: int = 443
    _probe_timeout: float = 2.0
    _probe_concurrency: int = 32
    _use_tls: bool = False
    _probe_sni: str = DEFAULT_PROBE_SNI
    _top_n: int = 4
    _keep_previous: bool = True

    # --- 写入 ---
    _ttl: str = "10m"
    _comment_prefix: str = "cfopt"
    _cleanup_names: bool = False
    _min_success: int = 1

    # --- 运行态：全部为实例属性，避免虚拟分身之间共享锁与运行标记 ---

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._state_lock = threading.Lock()
        self._running: bool = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_started: float = 0.0
        # 本轮内已确认不可达的 DoH 地址，避免每个域名重复等待超时
        self._doh_dead: set[str] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """载入配置并复位运行态。"""
        self.stop_service()
        self._log("INFO", f"初始化 Cloudflare 优选插件 v{self.plugin_version}")
        self._enabled = False
        self._notify = True
        self._notify_mode = "全部"
        self._cron = "0 5 * * *"
        self._dry_run = False
        self._router_host = ""
        self._router_port = 443
        self._router_ssl = True
        self._router_verify = False
        self._router_user = "admin"
        self._router_password = ""
        self._router_timeout = 10.0
        self._domains = ""
        self._exclude_domains = ""
        self._auto_sites = True
        self._check_mode = "all"
        self._check_fallback = True
        self._log_resolved_ips = True
        self._doh_urls = ""
        self._doh_url = ""
        self._doh_timeout = 5.0
        self._refresh_cidrs = True
        self._cidr_source = "official+custom"
        self._official_v4_url = CF_V4_LIST_URL
        self._official_v6_url = CF_V6_LIST_URL
        self._pool_urls = ""
        self._pool_text = ""
        self._pool_refresh_days = 7
        self._pool_timeout = 10.0
        self._pool_as_candidates = True
        self._pool_strict_cf = False
        self._ip_version = "4"
        self._probe_count = 80
        self._probe_port = 443
        self._probe_timeout = 2.0
        self._probe_concurrency = 32
        self._use_tls = False
        self._probe_sni = DEFAULT_PROBE_SNI
        self._top_n = 4
        self._keep_previous = True
        self._ttl = "10m"
        self._comment_prefix = "cfopt"
        self._cleanup_names = False
        self._min_success = 1
        if not config:
            self._log("INFO", "未载入配置，插件保持停用")
            return

        # 连接中心：选中已保存路由器时带入连接参数；未选中/找不到则保持原配置不变
        _ccfg = dict(config or {})
        _rcc, _rcc_note = roslink.connection_overrides(_ccfg.get("ros_connection"), "cf")
        _ccfg.update(_rcc)
        if _rcc_note:
            self._log("INFO" if _rcc else "WARN", f"连接中心：{_rcc_note}")
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        mode = str(config.get("notify_mode") or "").strip()
        self._notify_mode = mode if mode in _NOTIFY_MODES else ("全部" if self._notify else "关闭")
        self._cron = str(config.get("cron") or "0 5 * * *").strip()
        self._dry_run = bool(config.get("dry_run"))
        self._router_host = str(_ccfg.get("router_host") or "").strip()
        self._router_port = max(_to_int(_ccfg.get("router_port"), 443), 1)
        self._router_ssl = bool(_ccfg.get("router_ssl", True))
        self._router_verify = bool(_ccfg.get("router_verify"))
        self._router_user = str(_ccfg.get("router_user") or "admin").strip()
        self._router_password = str(_ccfg.get("router_password") or "")
        self._router_timeout = max(_to_float(_ccfg.get("router_timeout"), 10.0), 1.0)
        self._domains = str(config.get("domains") or "")
        self._exclude_domains = str(config.get("exclude_domains") or "")
        self._auto_sites = bool(config.get("auto_sites", True))
        check_mode = str(config.get("check_mode") or "all").strip().lower()
        self._check_mode = check_mode if check_mode in CHECK_MODES else "all"
        self._check_fallback = bool(config.get("check_fallback", True))
        self._log_resolved_ips = bool(config.get("log_resolved_ips", True))
        # 兼容 v1.1 及更早的单 DoH 地址配置
        self._doh_urls = str(config.get("doh_urls") or "").strip()
        self._doh_url = str(config.get("doh_url") or "").strip()
        self._doh_timeout = max(_to_float(config.get("doh_timeout"), 5.0), 1.0)
        self._refresh_cidrs = bool(config.get("refresh_cidrs", True))
        source = str(config.get("cidr_source") or "official+custom").strip()
        self._cidr_source = source if source in _CIDR_SOURCES else "official+custom"
        self._official_v4_url = str(config.get("official_v4_url") or CF_V4_LIST_URL).strip()
        self._official_v6_url = str(config.get("official_v6_url") or CF_V6_LIST_URL).strip()
        self._pool_urls = str(config.get("pool_urls") or "")
        self._pool_text = str(config.get("pool_text") or "")
        self._pool_refresh_days = min(max(_to_int(config.get("pool_refresh_days"), 7), 1), 90)
        self._pool_timeout = min(max(_to_float(config.get("pool_timeout"), 10.0), 2.0), 60.0)
        self._pool_as_candidates = bool(config.get("pool_as_candidates", True))
        self._pool_strict_cf = bool(config.get("pool_strict_cf"))
        self._ip_version = str(config.get("ip_version") or "4")
        self._probe_count = min(max(_to_int(config.get("probe_count"), 80), 1), 1000)
        self._probe_port = min(max(_to_int(config.get("probe_port"), 443), 1), 65535)
        self._probe_timeout = min(max(_to_float(config.get("probe_timeout"), 2.0), 0.2), 15.0)
        self._probe_concurrency = min(max(_to_int(config.get("probe_concurrency"), 32), 1), 256)
        self._use_tls = bool(config.get("use_tls"))
        self._probe_sni = str(config.get("probe_sni") or DEFAULT_PROBE_SNI).strip()
        self._top_n = min(max(_to_int(config.get("top_n"), 4), 1), 8)
        self._keep_previous = bool(config.get("keep_previous", True))
        self._ttl = str(config.get("ttl") or "10m").strip() or "10m"
        self._comment_prefix = str(config.get("comment_prefix") or "cfopt").strip() or "cfopt"
        self._cleanup_names = bool(config.get("cleanup_names"))
        self._min_success = max(_to_int(config.get("min_success"), 1), 1)
        self._log(
            "INFO",
            f"配置已载入：启用={self._enabled}，周期={self._cron}，仅测速={self._dry_run}，"
            f"通知={self._notify_mode}，站点判定={_CHECK_LABELS.get(self._check_mode)}，"
            f"IP来源={self._cidr_source}，IP版本={self._ip_version}，"
            f"路由器={self._router_host or '未配置'}",
        )
        if config.get("run_now"):
            # 官方 onlyonce 模式：保存配置立即执行一轮，并复位开关避免重复触发
            reset = dict(config)
            reset["run_now"] = False
            try:
                self.update_config(reset)
            except Exception as err:  # noqa: BLE001 - 复位失败不应阻断本次执行
                logger.warning(f"Cloudflare 优选：复位立即执行开关失败 {err}")
            if not self._enabled:
                self._log("WARN", "插件未启用，已忽略「立即执行一次」")
                self._send_message("Cloudflare 优选 IP → RouterOS", "插件未启用，请先启用后再执行")
            elif self.start_run(trigger="配置页", prefer_thread=True):
                # 配置保存常伴随插件重载，独立线程可避免一次性调度任务被 stop_service 取消
                self._log("INFO", "配置页触发「立即执行一次」（独立线程）")
            else:
                self._log("WARN", "配置页触发被跳过：已有任务在运行")

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """通知运行中的任务停止，并取消待执行的一次性任务。"""
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remove_once):
            try:
                remove_once(self.__class__.__name__, _ONCE_JOB_ID)
            except Exception as err:  # noqa: BLE001 - 调度器未启动时不应阻塞停用
                logger.debug(f"Cloudflare 优选：取消一次性任务失败 {err}")
        thread = getattr(self, "_thread", None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            # 保存配置不应被上一轮任务长时间阻塞，任务会自行检查中止信号
            thread.join(timeout=3)
        with self._state_lock:
            self._running = False

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时优选服务。"""
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except (ValueError, TypeError):
            trigger = CronTrigger.from_crontab("0 5 * * *")
        return [
            {
                "id": "CFRouterOS",
                "name": "Cloudflare 优选 IP → RouterOS",
                "trigger": trigger,
                "func": self.run_scheduled,
                "kwargs": {},
                "func_kwargs": {},
            }
        ]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令。"""
        return [
            {
                "cmd": "/cfrouteros_run",
                "event": EventType.PluginAction,
                "desc": "立即执行一次 Cloudflare 优选并写入 RouterOS",
                "category": "网络",
                "data": {"action": _CMD_ACTION},
            },
            {
                "cmd": "/cfrouteros_check",
                "event": EventType.PluginAction,
                "desc": "检测 RouterOS 连通与账号权限",
                "category": "网络",
                "data": {"action": _CMD_CHECK_ACTION},
            },
            {
                "cmd": "/cfrouteros_pool",
                "event": EventType.PluginAction,
                "desc": "立即刷新 Cloudflare 官方网段与第三方 IP 池",
                "category": "网络",
                "data": {"action": _CMD_POOL_ACTION},
            },
            {
                "cmd": "/cfrouteros_stop",
                "event": EventType.PluginAction,
                "desc": "中止正在执行的 Cloudflare 优选任务",
                "category": "网络",
                "data": {"action": _CMD_STOP_ACTION},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event) -> None:
        """消费远程命令事件。"""
        if not event:
            return
        data = event.event_data or {}
        if not isinstance(data, dict):
            return
        action = data.get("action")
        if action not in (_CMD_ACTION, _CMD_CHECK_ACTION, _CMD_STOP_ACTION, _CMD_POOL_ACTION):
            return
        target = data.get("plugin_id")
        if target and target != self.__class__.__name__:
            return
        channel, userid = data.get("channel"), data.get("user")
        title = "Cloudflare 优选 IP → RouterOS"
        if action == _CMD_STOP_ACTION:
            self._stop_event.set()
            self._log("WARN", "远程命令请求中止当前任务")
            self._send_message(title, "已发送中止信号，当前轮次将尽快结束",
                               channel=channel, userid=userid)
            return
        if not self._enabled:
            self._send_message(title, "插件未启用，命令已忽略", channel=channel, userid=userid)
            return
        if action == _CMD_CHECK_ACTION:
            ok, message = self._check_router_sync()
            self._log("INFO" if ok else "WARN", f"远程命令检测路由器：{message}")
            self._send_message(title, f"路由器检测：{message}", channel=channel, userid=userid)
            return
        if action == _CMD_POOL_ACTION:
            response = self.api_refresh_pool()
            self._send_message(title, f"IP 池刷新：{response.message}", channel=channel, userid=userid)
            return
        if self.start_run(trigger="命令"):
            self._send_message(title, "已开始执行，完成后将按通知设置推送结果",
                               channel=channel, userid=userid)
        else:
            self._send_message(title, "上一轮任务尚未结束，请稍后再试",
                               channel=channel, userid=userid)

    # ------------------------------------------------------------------
    # 表单
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置表单与默认值。"""
        content: List[dict] = [
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-2"},
                 "text": "连接中心 · 选择已保存的路由器"},
                {"component": "VCardText", "content": [
                    {"component": "VSelect", "props": {
                        "model": "ros_connection",
                        "label": "已保存的路由器",
                        "items": roslink.connection_items(),
                        "clearable": True,
                        "hint": "在「RouterOS 连接中心」统一维护；选中后自动带入地址/端口/账号/密码/HTTPS，下面的手填字段可留空。不选下拉则继续使用手填参数（旧配置不受影响）。",
                        "persistentHint": True}},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": "下拉为空说明尚未安装或保存连接中心，属正常现象，直接按原方式手填即可。"},
                ]},
            ]},
            {
                "component": "VRow",
                "content": [
                    self._col(12, 4, [{"component": "VSwitch", "props": {
                        "model": "run_now", "label": "立即执行一次", "color": "primary",
                        "hint": "保存配置后立即运行一轮，开关会自动复位；也可在插件详情页点「立即执行」",
                        "persistentHint": True}}]),
                    self._col(12, 4, [{"component": "VSelect", "props": {
                        "model": "notify_mode", "label": "通知方式",
                        "items": [
                            {"title": "全部通知（成功与异常）", "value": "全部"},
                            {"title": "仅异常通知", "value": "仅异常"},
                            {"title": "关闭通知", "value": "关闭"},
                        ],
                        "hint": "通知走 MoviePilot 全局通知开关与收件人路由，点击可跳回插件页",
                        "persistentHint": True}}]),
                    self._col(12, 4, [{"component": "VTextField", "props": {
                        "model": "min_success", "label": "最少可用 IP 数",
                        "type": "number", "placeholder": "1",
                        "hint": "可用 IP 少于该值时不写入，避免把已有 DNS 记录清掉",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {"model": "notify", "label": "运行结果通知"}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "dry_run", "label": "仅测速不写入",
                        "hint": "首次调通建议开启，确认优选结果后再写入路由器",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "cron", "label": "执行周期（cron）",
                        "placeholder": "0 5 * * *",
                        "hint": "默认每天 05:00，按容器时区解释", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 4, [{"component": "VTextField", "props": {
                        "model": "router_host", "label": "RouterOS 地址",
                        "placeholder": "192.168.88.1",
                        "hint": "填 IP 或域名，不要带 /rest 路径", "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "router_port", "label": "端口", "placeholder": "443", "type": "number"}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "router_user", "label": "用户名", "placeholder": "admin"}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "router_password", "label": "密码", "type": "password",
                        "hint": "建议单独建只含 read/write/rest-api 权限的账号",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "router_ssl", "label": "使用 HTTPS",
                        "hint": "RouterOS 默认 www-ssl 端口 443", "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "router_verify", "label": "校验证书",
                        "hint": "自签证书请关闭", "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "router_timeout", "label": "REST 超时（秒）",
                        "placeholder": "10", "type": "number"}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "ttl", "label": "DNS TTL",
                        "placeholder": "10m",
                        "hint": "RouterOS 语法，如 10m / 1h / 00:10:00",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 4, [{"component": "VSelect", "props": {
                        "model": "check_mode", "label": "站点判定方式",
                        "items": [
                            {"title": "不检测，全部纳入（推荐）", "value": "all"},
                            {"title": "系统 DNS 判定 CDN IP", "value": "system"},
                            {"title": "多源 DoH 判定 CDN IP", "value": "doh"},
                        ],
                        "hint": "默认零网络依赖，配置里的域名一律视为 Cloudflare 站点，不再出现「DoH 解析失败」跳过",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "check_fallback", "label": "判定失败仍纳入",
                        "hint": "解析失败或 IP 不在 Cloudflare 网段时仍按配置处理，避免全部跳过",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "log_resolved_ips", "label": "日志显示站点原 IP",
                        "hint": "用系统 DNS 查一下站点当前解析 IP 写进日志，仅展示、不参与判定",
                        "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VSelect", "props": {
                        "model": "ip_version", "label": "优选 IP 版本",
                        "items": [
                            {"title": "仅 IPv4", "value": "4"},
                            {"title": "IPv4 + IPv6", "value": "46"},
                        ]}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 8, [{"component": "VTextarea", "props": {
                        "model": "domains", "label": "自定义域名",
                        "rows": 3,
                        "placeholder": "hdwing.org\npt.example.com",
                        "hint": "每行一个；默认全部纳入优选，不需要 DoH 解析",
                        "persistentHint": True}}]),
                    self._col(12, 4, [{"component": "VTextarea", "props": {
                        "model": "exclude_domains", "label": "排除域名",
                        "rows": 3,
                        "placeholder": "tracker.example.com",
                        "hint": "自动收集站点时用，每行一个；命中的域名不参与优选",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "auto_sites", "label": "自动纳入 PT 站点",
                        "hint": "取宿主中已启用的非公开站点域名", "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "doh_timeout", "label": "DoH 超时（秒）",
                        "placeholder": "5", "type": "number",
                        "hint": "仅在 DoH 判定模式下生效", "persistentHint": True}}]),
                    self._col(12, 7, [{"component": "VTextarea", "props": {
                        "model": "doh_urls", "label": "DoH 地址（可多行，仅 DoH 判定模式使用）",
                        "rows": 2,
                        "placeholder": "https://cloudflare-dns.com/dns-query\nhttps://doh.pub/dns-query",
                        "hint": "留空自动使用内置多源；任一成功即可，全部失败自动回退系统 DNS",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 4, [{"component": "VSelect", "props": {
                        "model": "cidr_source", "label": "IP 网段 / IP 池来源",
                        "items": [
                            {"title": "官方 + 自定义（推荐）", "value": "official+custom"},
                            {"title": "仅 Cloudflare 官方", "value": "official"},
                            {"title": "仅自定义 / 第三方池", "value": "custom"},
                            {"title": "仅内置静态（不联网）", "value": "builtin"},
                        ],
                        "hint": "官方 = Cloudflare 公布的 ips-v4 / ips-v6；自定义 = 下面填的第三方或个人 IP 池",
                        "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "pool_refresh_days", "label": "刷新周期（天）",
                        "placeholder": "7", "type": "number"}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "pool_timeout", "label": "拉取超时（秒）",
                        "placeholder": "10", "type": "number"}}]),
                    self._col(12, 2, [{"component": "VSwitch", "props": {
                        "model": "refresh_cidrs", "label": "自动刷新",
                        "hint": "按周期拉取，失败自动用上次缓存与内置段",
                        "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VSwitch", "props": {
                        "model": "pool_as_candidates", "label": "池 IP 直接测速",
                        "hint": "自定义池 IP 优先于随机采样", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 7, [{"component": "VTextarea", "props": {
                        "model": "pool_urls", "label": "第三方 / 个人维护的 Cloudflare IP 池地址",
                        "rows": 3,
                        "placeholder": "https://raw.githubusercontent.com/<user>/<repo>/main/cf-ips.txt\nhttps://example.com/api/cf-ip-pool",
                        "hint": "每行一个 http(s) 地址；支持纯文本/JSON、单 IP、CIDR、IP:端口、IPv6，# 可写注释",
                        "persistentHint": True}}]),
                    self._col(12, 5, [{"component": "VTextarea", "props": {
                        "model": "pool_text", "label": "手动 IP / 网段池（离线兜底）",
                        "rows": 3,
                        "placeholder": "104.16.0.1\n104.16.0.0/13\n2606:4700::1",
                        "hint": "始终生效，不依赖网络；可写自己的优选 IP 或补充网段",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 6, [{"component": "VTextField", "props": {
                        "model": "official_v4_url", "label": "官方 IPv4 网段地址",
                        "placeholder": CF_V4_LIST_URL,
                        "hint": "Cloudflare 官方公布，可换成可用镜像", "persistentHint": True}}]),
                    self._col(12, 6, [{"component": "VTextField", "props": {
                        "model": "official_v6_url", "label": "官方 IPv6 网段地址",
                        "placeholder": CF_V6_LIST_URL}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "probe_count", "label": "候选数量",
                        "placeholder": "80", "type": "number",
                        "hint": "自定义池 IP 用尽后从官方网段随机补足", "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "probe_port", "label": "测速端口",
                        "placeholder": "443", "type": "number"}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "probe_timeout", "label": "单 IP 超时（秒）",
                        "placeholder": "2.0", "type": "number"}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "probe_concurrency", "label": "并发数",
                        "placeholder": "32", "type": "number"}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "top_n", "label": "写入条数",
                        "placeholder": "4", "type": "number",
                        "hint": "每个域名写入几条记录", "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VSwitch", "props": {
                        "model": "use_tls", "label": "TLS 握手测速",
                        "hint": "更贴近真实访问，耗时略高", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "keep_previous", "label": "沿用上轮优选",
                        "hint": "把上轮最优 IP 加入本轮候选，减少抖动",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "pool_strict_cf", "label": "池 IP 限 CF 网段",
                        "hint": "开启后自定义池里不属于 Cloudflare 网段的 IP 会被丢弃",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "cleanup_names", "label": "接管全部同名记录",
                        "hint": "开启后会删除同域名下非本插件写入的 A 记录，谨慎使用",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "probe_sni", "label": "TLS 测速 SNI",
                        "placeholder": DEFAULT_PROBE_SNI,
                        "hint": "TLS 测速使用的 SNI，建议固定为 cloudflare.com",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 6, [{"component": "VTextField", "props": {
                        "model": "comment_prefix", "label": "记录备注前缀",
                        "placeholder": "cfopt",
                        "hint": "最终形如 cfopt:hdwing.org，用于识别本插件记录",
                        "persistentHint": True}}]),
                    self._col(12, 6, [{"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": "工作方式：默认不依赖 DoH，直接把配置的域名当作 Cloudflare CDN 站点；"
                                "官方网段 + 第三方/个人 IP 池合并后并发测速，取延迟最低的 N 个写入 "
                                "/ip/dns/static（同名多条由 RouterOS 轮询返回）。"
                                "先写新记录、成功后再清理旧记录，不会动你手写的其他记录。"
                                "逐站点原始 IP / 优选 IP / 写入结果可在「查看日志」窗口实时查看（SSE）。"
                                "内网客户端需把 DNS 指向这台 RouterOS 才会生效。"}}]),
                ],
            },
        ]
        return [{"component": "VForm", "content": content}], {
            "enabled": False,
            "notify": True,
            "notify_mode": "全部",
            "run_now": False,
            "dry_run": False,
            "cron": "0 5 * * *",
            "router_host": "",
            "ros_connection": "",
            "router_port": 443,
            "router_ssl": True,
            "router_verify": False,
            "router_user": "admin",
            "router_password": "",
            "router_timeout": 10,
            "domains": "",
            "exclude_domains": "",
            "auto_sites": True,
            "check_mode": "all",
            "check_fallback": True,
            "log_resolved_ips": True,
            "doh_urls": "",
            "doh_timeout": 5,
            "refresh_cidrs": True,
            "cidr_source": "official+custom",
            "official_v4_url": CF_V4_LIST_URL,
            "official_v6_url": CF_V6_LIST_URL,
            "pool_urls": "",
            "pool_text": "",
            "pool_refresh_days": 7,
            "pool_timeout": 10,
            "pool_as_candidates": True,
            "pool_strict_cf": False,
            "ip_version": "4",
            "probe_count": 80,
            "probe_port": 443,
            "probe_timeout": 2.0,
            "probe_concurrency": 32,
            "use_tls": False,
            "probe_sni": DEFAULT_PROBE_SNI,
            "top_n": 4,
            "keep_previous": True,
            "ttl": "10m",
            "comment_prefix": "cfopt",
            "cleanup_names": False,
            "min_success": 1,
        }

    @staticmethod
    def _col(cols: int, md: int, content: List[dict]) -> dict:
        return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": content}

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    @staticmethod
    def _html_block(title: str, lines: List[str], limit: int = 60) -> dict:
        """用可换行的纯文本块展示明细，避免 VAlert 把换行压平。"""
        body = "\n".join(_html.escape(str(item)) for item in lines[:limit]) or "暂无数据"
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mt-2"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": title},
                {"component": "VCardText", "props": {"class": "text-caption"},
                 "html": f'<pre style="white-space:pre-wrap;word-break:break-all;margin:0">{body}</pre>'},
            ],
        }

    def _action_button(self, text: str, icon: str, path: str, color: str,
                       method: str = "post") -> dict:
        """V3 详情页按钮：PageRender 只识别 events.<事件>.{api, method, params}。"""
        return {
            "component": "VBtn",
            "props": {
                "variant": "tonal", "color": color, "size": "small",
                "class": "me-2 mb-2", "prepend-icon": icon,
            },
            "text": text,
            "events": {"click": {
                "api": f"plugin/{self.__class__.__name__}{path}",
                "method": method,
            }},
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页；未启用时也保留操作入口。"""
        stats = self.get_data(_STATS_KEY) or {}
        last = stats.get("last_result") or {}
        logs = list(self.get_data(_LOG_KEY) or [])
        history = list(self.get_data(_HISTORY_KEY) or [])
        live = dict(self.get_data(_LIVE_KEY) or {})
        pool = dict(self.get_data(_POOL_KEY) or {})
        content: List[dict] = []
        if not self._enabled:
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mb-2",
                "text": "插件未启用：请先在配置中启用并填写 RouterOS 地址与账号。"
                        "下方仍可手动检测路由器，但定时与立即执行需要先启用。"}})
        content.append({
            "component": "VRow",
            "content": [
                self._stat_card("符合域名", str(last.get("domains_cf", 0)), "mdi-cloud-check-outline"),
                self._stat_card("写入记录", str(last.get("written", 0)), "mdi-dns-outline"),
                self._stat_card("最快延迟",
                                format_latency(float(last.get("best_latency", 0) or 0)),
                                "mdi-speedometer"),
                self._stat_card("上轮耗时", f"{stats.get('last_duration', '-')}s", "mdi-timer-outline"),
            ],
        })
        content.append({
            "component": "VRow",
            "content": [self._col(12, 12, [{"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-2",
                "text": (f"上次运行：{stats.get('last_run_text', '尚未运行')}　"
                         f"触发：{stats.get('last_trigger', '-')}　"
                         f"域名 {last.get('domains_total', 0)} / 纳入 {last.get('domains_cf', 0)} / "
                         f"候选 {last.get('probed', 0)} / 可用 {last.get('alive', 0)} / "
                         f"写入 {last.get('written', 0)}"
                         + (f"　说明：{last.get('message')}" if last.get("message") else ""))}}])],
        })
        content.append({
            "component": "VRow",
            "content": [self._col(12, 12, [{"component": "VAlert", "props": {
                "type": "primary" if live.get("running") else "secondary",
                "variant": "tonal", "class": "mt-2",
                "title": "实时运行状态",
                "text": (f"运行中：{live.get('stage', '-')}　进度 {live.get('done', 0)}/{live.get('total', 0)}　"
                         f"当前：{live.get('current', '-')}　开始：{live.get('started_text', '-')}"
                         if live.get("running") else
                         f"空闲（上次阶段：{live.get('stage', '-')}）。"
                         "插件卡片「查看日志」窗口通过 SSE 实时推送逐站点明细；"
                         "本页点「刷新页面」即可拉取最新进度与日志。")}}])],
        })
        content.append({
            "component": "VRow",
            "content": [self._col(12, 12, [{"component": "VAlert", "props": {
                "type": "success" if last.get("best") else "secondary",
                "variant": "tonal", "class": "mt-2",
                "title": "本轮优选 IP",
                "text": "　".join(
                    f"{item.get('ip')}（{format_latency(float(item.get('latency_ms') or 0))}）"
                    for item in (last.get("best") or [])
                ) or "暂无数据，点击下方「立即执行」。"}}])],
        })
        domain_lines = []
        for item in (live.get("domains") or stats.get("last_domains") or [])[:60]:
            domain_lines.append(
                f"{item.get('domain')}｜{item.get('state', '-')}"
                f"　原IP={format_ip_list(item.get('resolved') or [], 4)}"
                f"　优选IP={format_ip_list(item.get('addresses') or item.get('best_ips') or [], 4)}"
                f"　写入={'是' if item.get('added') else '否'}"
                f"（新增 {item.get('added', 0)} / 清理 {item.get('deleted', 0)}）"
                + (f"　{item.get('message') or item.get('reason')}" if (item.get("message") or item.get("reason")) else "")
            )
        content.append(self._html_block("站点优选明细（原 IP / 优选 IP / RouterOS 写入）", domain_lines, 60))
        pool_lines = [
            f"{item.get('name')}｜{item.get('url') or '-'}｜"
            f"{'成功' if item.get('ok') else '失败'}"
            f"{'（缓存）' if item.get('cached') else ''}｜"
            f"IP {item.get('ips', 0)} / 网段 {item.get('cidrs', 0)}"
            + (f"｜{item.get('error')}" if item.get("error") else "")
            for item in (pool.get("sources") or [])
        ]
        pool_lines.insert(0, f"更新：{pool.get('updated_text', '尚未加载')}　"
                             f"来源模式：{pool.get('source_mode', '-')}　"
                             f"官方段：{'在线' if pool.get('official_ok') else '内置兜底'}　"
                             f"合并网段 {pool.get('network_count', 0)} 段 / 池 IP {pool.get('pool_count', 0)} 个")
        content.append(self._html_block("IP 池 / 网段来源", pool_lines, 25))
        history_lines = [
            f"{item.get('time')}　[{item.get('trigger')}]　纳入 {item.get('domains_cf')}　"
            f"写入 {item.get('written')}　{item.get('message') or ''}"
            for item in history
        ]
        content.append(self._html_block("运行历史（最近 20 条）", history_lines, 20))
        log_lines = [
            f"{item.get('time')}　[{item.get('level')}]　{item.get('message')}"
            for item in logs
        ]
        content.append(self._html_block(
            "运行日志（最近 120 条；插件卡片「查看日志」= SSE 实时 plugins/cfrouteros.log）",
            log_lines, 120))
        content.append({
            "component": "VRow",
            "content": [self._col(12, 12, [
                self._action_button("立即执行", "mdi-play", "/run", "primary"),
                self._action_button("刷新页面", "mdi-refresh", "/status", "secondary", "get"),
                self._action_button("检测路由器", "mdi-lan-connect", "/check", "info"),
                self._action_button("刷新 IP 池", "mdi-cloud-download", "/refresh_pool", "info"),
                self._action_button("中止任务", "mdi-stop", "/stop", "warning"),
                self._action_button("清空日志", "mdi-delete-sweep", "/clear_logs", "error"),
            ])],
        })
        return content

    @staticmethod
    def _stat_card(title: str, value: str, icon: str) -> dict:
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
                            {"component": "div", "props": {"class": "text-h6"}, "text": str(value)},
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
             "auth": "bear", "summary": "立即执行一次优选"},
            {"path": "/check", "endpoint": self.api_check, "methods": ["POST"],
             "auth": "bear", "summary": "检测 RouterOS 连通与账号权限"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询上轮结果（详情页刷新入口）"},
            {"path": "/live", "endpoint": self.api_live, "methods": ["GET"],
             "auth": "bear", "summary": "查询运行中的实时进度与逐站点明细"},
            {"path": "/logs", "endpoint": self.api_logs, "methods": ["GET"],
             "auth": "bear", "summary": "查询插件运行日志"},
            {"path": "/refresh_pool", "endpoint": self.api_refresh_pool, "methods": ["POST"],
             "auth": "bear", "summary": "立即刷新官方网段与第三方 IP 池"},
            {"path": "/clear_cache", "endpoint": self.api_clear_cache, "methods": ["POST"],
             "auth": "bear", "summary": "清空网段缓存与历史"},
            {"path": "/clear_logs", "endpoint": self.api_clear_logs, "methods": ["POST"],
             "auth": "bear", "summary": "清空运行日志"},
            {"path": "/stop", "endpoint": self.api_stop, "methods": ["POST"],
             "auth": "bear", "summary": "中止正在执行的任务"},
        ]

    def api_run(self) -> schemas.Response:
        """后台启动一轮优选。"""
        if not self._enabled:
            return schemas.Response(success=False, message="插件未启用")
        if not self._router_host and not self._dry_run:
            return schemas.Response(success=False, message="未配置 RouterOS 地址")
        if self._running:
            return schemas.Response(success=False, message="已有任务在运行")
        started = self.start_run(trigger="手动")
        if started:
            self._log("INFO", "详情页触发「立即执行」")
        return schemas.Response(
            success=started,
            message="已开始执行，稍后刷新页面查看结果" if started else "任务启动失败",
        )

    def api_check(self) -> schemas.Response:
        """检测 RouterOS 连通性、认证与 REST 可用性，并把结果推送到 MP 通知。"""
        if not self._router_host:
            return schemas.Response(success=False, message="未配置 RouterOS 地址")
        ok, message = self._check_router_sync()
        self._log("INFO" if ok else "WARN", f"检测路由器：{message}")
        self._send_message("Cloudflare 优选 IP → RouterOS", f"路由器检测：{message}")
        return schemas.Response(success=ok, message=message)

    def api_status(self) -> schemas.Response:
        """返回上轮结果与实时进度（详情页「刷新页面」调用）。"""
        return schemas.Response(success=True, data={
            "enabled": self._enabled,
            "running": self._running,
            "cron": self._cron,
            "dry_run": self._dry_run,
            "notify_mode": self._notify_mode,
            "check_mode": self._check_mode,
            "cidr_source": self._cidr_source,
            "log_count": len(self.get_data(_LOG_KEY) or []),
            "stats": self.get_data(_STATS_KEY) or {},
            "live": self.get_data(_LIVE_KEY) or {},
            "pool": self.get_data(_POOL_KEY) or {},
        })

    def api_live(self) -> schemas.Response:
        """返回运行中的实时进度、逐站点明细与最近日志，供外部轮询。"""
        return schemas.Response(success=True, data={
            "running": self._running,
            "live": self.get_data(_LIVE_KEY) or {},
            "logs": list(self.get_data(_LOG_KEY) or [])[:80],
            "updated": time.time(),
        })

    def api_logs(self) -> schemas.Response:
        """返回持久化运行日志，供详情之外直接排查。"""
        return schemas.Response(success=True, data={
            "logfile": f"plugins/{self.__class__.__name__.lower()}.log",
            "logs": list(self.get_data(_LOG_KEY) or []),
            "live": self.get_data(_LIVE_KEY) or {},
        })

    def api_refresh_pool(self) -> schemas.Response:
        """立即强制刷新官方网段与所有自定义 IP 池来源。"""
        try:
            summary = self._run_coroutine(self._refresh_pool_async())
        except Exception as err:  # noqa: BLE001 - 刷新失败不应影响插件
            self._log("ERROR", f"IP 池刷新异常：{err}")
            return schemas.Response(success=False, message=f"IP 池刷新异常：{err}")
        message = (f"网段 {summary.get('network_count', 0)} 段，池 IP {summary.get('pool_count', 0)} 个，"
                   f"来源 {len(summary.get('sources') or [])} 个"
                   + ("" if summary.get("official_ok") else "（官方段使用内置兜底）"))
        self._log("INFO", f"[IP池] 手动刷新完成：{message}")
        return schemas.Response(success=True, message=message,
                               data=dict(self.get_data(_POOL_KEY) or {}))

    def api_clear_cache(self) -> schemas.Response:
        """清空网段缓存与历史记录。"""
        self.save_data(_CIDR_KEY, {})
        self.save_data(_POOL_KEY, {})
        self.save_data(_HISTORY_KEY, [])
        self._log("INFO", "已清空网段缓存、IP 池状态与运行历史")
        return schemas.Response(success=True, message="网段缓存与历史已清空")

    def api_clear_logs(self) -> schemas.Response:
        """清空插件运行日志（不影响 MP 主日志）。"""
        self.save_data(_LOG_KEY, [])
        return schemas.Response(success=True, message="运行日志已清空")

    def api_stop(self) -> schemas.Response:
        """向正在执行的任务发送中止信号。"""
        self._stop_event.set()
        self._log("WARN", "收到中止请求，当前轮次将在下一个检查点退出")
        return schemas.Response(success=True, message="已发送中止信号")

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    def run_scheduled(self) -> None:
        """定时入口。"""
        self.start_run(trigger="定时")

    def start_run(self, trigger: str = "手动", prefer_thread: bool = False) -> bool:
        """异步启动一轮优选；配置页触发时强制用独立线程，避免被插件重载取消。"""
        with self._state_lock:
            if self._running:
                if time.time() - self._run_started < _STALE_RUN_SECONDS:
                    self._log("WARN", "上一轮尚未结束，本次触发已跳过")
                    return False
                self._log("WARN", "运行状态疑似残留，强制重置后继续")
            self._running = True
            self._run_started = time.time()
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is None:
            self._stop_event = stop_event = threading.Event()
        stop_event.clear()
        self._doh_dead.clear()

        add_once = None if prefer_thread else getattr(scheduler_sdk, "add_plugin_once_job", None)
        if callable(add_once):
            try:
                scheduled = add_once(
                    self.__class__.__name__,
                    _ONCE_JOB_ID,
                    self._run_wrapper,
                    "Cloudflare 优选执行",
                    delay_seconds=0,
                    func_kwargs={"trigger": trigger},
                )
            except Exception as err:  # noqa: BLE001 - 调度器异常时退回自建线程
                logger.warning(f"Cloudflare 优选：提交调度任务失败，改为后台线程 {err}")
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
        """执行一轮并兜底复位状态。"""
        try:
            self._run_once(trigger)
        except Exception as err:  # noqa: BLE001 - 后台任务必须兜底
            self._log("ERROR", f"执行异常：{err} - {traceback.format_exc()}")
            failure = {
                "trigger": trigger,
                "domains_total": 0, "domains_cf": 0, "probed": 0, "alive": 0,
                "written": 0, "best_latency": 0, "best": [], "domains": [],
                "message": f"执行异常：{err}",
            }
            self._save_result(failure, time.time())
            self._notify_result(failure)
        finally:
            self._update_live(running=False, stage="已结束", current="-")
            with self._state_lock:
                self._running = False
            if getattr(self, "_thread", None) is threading.current_thread():
                self._thread = None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def _run_once(self, trigger: str) -> None:
        started = time.time()
        self._update_live(
            running=True, stage="准备中", current="-", done=0, total=0,
            trigger=trigger, started_text=_now_text(), domains=[],
        )
        self._log("INFO", f"开始执行（触发：{trigger}，站点判定={_CHECK_LABELS.get(self._check_mode)}，"
                          f"IP来源={self._cidr_source}）")
        result = self._run_coroutine(self._run_async(trigger))
        duration = round(max(time.time() - started, 0.1), 1)
        self._save_result(result, started)
        self._log(
            "INFO",
            f"本轮完成：{result.get('message') or '无说明'}；纳入域名 {result.get('domains_cf', 0)}，"
            f"写入 {result.get('written', 0)} 条，耗时 {duration}s",
        )
        self._notify_result(result, duration)

    async def _run_async(self, trigger: str) -> Dict[str, Any]:
        """完整一轮：收集域名 → 加载 IP 来源 → 逐站点判定 → 测速 → 写入。"""
        stop_event = getattr(self, "_stop_event", None)
        result: Dict[str, Any] = {
            "trigger": trigger,
            "domains_total": 0,
            "domains_cf": 0,
            "probed": 0,
            "alive": 0,
            "written": 0,
            "best_latency": 0,
            "best": [],
            "domains": [],
            "message": "",
            "dry_run": self._dry_run,
            "check_mode": self._check_mode,
        }
        if stop_event is not None and stop_event.is_set():
            result["message"] = "任务已被取消"
            return result

        # 1. 目标域名
        domains = self._collect_domains()
        result["domains_total"] = len(domains)
        if not domains:
            result["message"] = _SITE_ONLY_ERROR
            self._log("WARN", "未配置任何域名，也未从站点收集到域名")
            return result
        self._log("INFO", f"[域名] 共 {len(domains)} 个：{format_ip_list(domains, 12)}")
        self._update_live(stage="加载 IP 来源", total=len(domains), done=0)

        # 2. 官方网段 + 第三方/个人 IP 池
        pool_http = AsyncRequestUtils(timeout=self._pool_timeout, verify=False)
        sources = await self._load_ip_sources(pool_http)
        networks: List[Any] = sources["networks"]
        if not networks:
            result["message"] = "Cloudflare 网段为空"
            self._log("ERROR", "Cloudflare 网段为空，且内置段不可用")
            return result
        self._log(
            "INFO",
            f"[IP池] 合并 {len(networks)} 段（官方 {'在线' if sources['official_ok'] else '内置兜底'}），"
            f"自定义池 {len(sources['pool_ips'])} 个 IP / {len(sources['pool_cidrs'])} 个网段，"
            f"来源 {len(sources['sources'])} 个",
        )

        # 3. 逐站点解析与判定（默认零 DoH 依赖）
        self._update_live(stage="解析并判定站点", done=0, total=len(domains))
        doh_http = AsyncRequestUtils(timeout=self._doh_timeout, verify=False)
        resolved_map, resolve_source = await self._resolve_domains(doh_http, domains)
        eligible: List[str] = []
        rows: Dict[str, Dict[str, Any]] = {}
        for index, domain in enumerate(domains, 1):
            if stop_event is not None and stop_event.is_set():
                result["message"] = "任务已被取消"
                break
            resolved = resolved_map.get(domain) or []
            accepted, reason, cf_ips = classify_domain(
                resolved, networks, mode=self._check_mode, fallback=self._check_fallback,
            )
            row: Dict[str, Any] = {
                "domain": domain,
                "state": "待处理" if accepted else "跳过",
                "resolved": resolved,
                "cf_ips": cf_ips,
                "source": resolve_source.get(domain, "未解析"),
                "reason": reason,
                "addresses": [],
                "added": 0,
                "deleted": 0,
                "errors": [],
                "message": "" if accepted else reason,
            }
            rows[domain] = row
            result["domains"].append(row)
            if accepted:
                eligible.append(domain)
            self._log(
                "INFO" if accepted else "WARN",
                f"[站点 {index}/{len(domains)}] {domain}｜判定={_CHECK_LABELS.get(self._check_mode)}｜"
                f"来源={row['source']}｜原始IP={format_ip_list(resolved)}｜"
                f"CF命中={format_ip_list(cf_ips, 4)}｜{reason}｜结果={'纳入' if accepted else '跳过'}",
            )
            self._update_live(
                stage="解析并判定站点", current=domain, done=index, total=len(domains),
                domains=[dict(item) for item in result["domains"]],
            )
        result["domains_cf"] = len(eligible)
        self._log("INFO", f"[判定] 共 {len(domains)} 个，纳入 {len(eligible)} 个，跳过 {len(domains) - len(eligible)} 个")
        if not eligible:
            result["message"] = "没有域名被纳入，未写入"
            return result

        # 4. 候选 IP：自定义池优先 > 上轮优选 > 官方网段随机采样
        ipv4_only = self._ip_version != "46"
        pool_ips = list(sources["pool_ips"]) if self._pool_as_candidates else []
        if ipv4_only:
            pool_ips = [item for item in pool_ips if ":" not in item]
        # 池 IP 最多占 70%，其余留给上轮优选与官方段采样，避免池内 IP 全失效时整轮无候选
        pool_ips = pool_ips[:max(1, int(self._probe_count * 0.7))]
        sample_networks = networks
        if self._cidr_source == "custom":
            sample_networks = parse_cidrs("\n".join(sources["pool_cidrs"]))
        sampled = sample_ips(sample_networks, self._probe_count, ipv4_only=ipv4_only) \
            if sample_networks else []
        previous: List[str] = []
        if self._keep_previous:
            previous = [str(item.get("ip")) for item in
                        ((self.get_data(_STATS_KEY) or {}).get("last_result") or {}).get("best", [])]
            if ipv4_only:
                previous = [item for item in previous if ":" not in item]
        candidates = build_candidate_pool(
            pool_ips, sampled, previous, self._probe_count,
            networks=networks, strict_cf=self._pool_strict_cf,
        )
        result["probed"] = len(candidates)
        self._log("INFO", f"[候选] 共 {len(candidates)} 个（自定义池 {len(pool_ips)} / "
                          f"上轮沿用 {len(previous)} / 官方采样 {len(sampled)}，去重后取前 {self._probe_count}）")

        # 5. 分批测速，便于中止与实时进度
        self._update_live(stage="测速中", done=0, total=len(candidates), current="-")
        probes: List[ProbeResult] = []
        batch_size = max(self._probe_concurrency, 32)
        for offset in range(0, len(candidates), batch_size):
            if stop_event is not None and stop_event.is_set():
                result["message"] = "任务已被取消"
                break
            batch = candidates[offset:offset + batch_size]
            batch_result = await probe_many(
                batch,
                port=self._probe_port,
                timeout=self._probe_timeout,
                concurrency=self._probe_concurrency,
                use_tls=self._use_tls,
                sni=self._probe_sni or DEFAULT_PROBE_SNI,
            )
            probes.extend(batch_result)
            alive_batch = sum(1 for item in batch_result if item.ok)
            self._log("INFO", f"[测速] 批次 {offset // batch_size + 1}：本批 {len(batch)} 个，可用 {alive_batch} 个")
            self._update_live(stage="测速中", done=min(offset + len(batch), len(candidates)),
                              total=len(candidates), current=f"已测 {len(probes)} 个")
        if stop_event is not None and stop_event.is_set():
            return result

        alive = [item for item in probes if item.ok]
        result["alive"] = len(alive)
        best = pick_fastest(probes, self._top_n)
        result["best"] = [item.as_dict() for item in best]
        best_ips = [item.ip for item in best]
        result["best_latency"] = round(best[0].latency_ms, 1) if best else 0
        self._log(
            "INFO",
            f"[优选] 候选 {len(candidates)}，可用 {len(alive)}，最优="
            + (", ".join(f"{item.ip}({format_latency(item.latency_ms)})" for item in best) or "无"),
        )
        if len(best) < self._min_success:
            result["message"] = f"可用 IP 不足（{len(best)}/{self._min_success}），未写入"
            self._log("WARN", result["message"])
            for row in result["domains"]:
                if row["domain"] in eligible:
                    row["state"] = "未写入（IP 不足）"
                    row["message"] = result["message"]
            return result

        for row in result["domains"]:
            if row["domain"] in eligible:
                row["addresses"] = list(best_ips)
        self._update_live(stage="准备写入", done=0, total=len(eligible), current="-",
                          domains=[dict(item) for item in result["domains"]])

        # 6. 试运行
        if self._dry_run:
            self._log("INFO", "仅测速模式：跳过路由器写入")
            for row in result["domains"]:
                if row["domain"] in eligible:
                    row["state"] = "跳过（仅测速）"
                    row["message"] = "已算出优选 IP，未写入 RouterOS"
                    self._log("INFO", f"[写入] {row['domain']}｜优选IP={format_ip_list(best_ips)}｜结果=跳过（仅测速）")
            result["message"] = "仅测速模式，未写入路由器"
            return result

        if not self._router_host:
            result["message"] = "未配置 RouterOS 地址，仅完成测速"
            self._log("WARN", result["message"])
            for row in result["domains"]:
                if row["domain"] in eligible:
                    row["state"] = "未写入（未配置路由器）"
                    row["message"] = result["message"]
            return result

        # 7. 路由器连通性
        router_http = AsyncRequestUtils(timeout=self._router_timeout, verify=self._router_verify)
        client = RouterOSClient(
            normalize_base_url(self._router_host, self._router_port, self._router_ssl),
            self._router_user,
            self._router_password,
            timeout=self._router_timeout,
            verify=self._router_verify,
        )
        ok, message = await client.check(router_http)
        if not ok:
            result["message"] = f"路由器连接失败：{message}"
            self._log("ERROR", f"[写入] 全部域名未写入：{result['message']}")
            for row in result["domains"]:
                if row["domain"] in eligible:
                    row["state"] = "写入失败（路由器未连接）"
                    row["message"] = message
            return result

        # 8. 逐域名写入
        for index, domain in enumerate(eligible, 1):
            if stop_event is not None and stop_event.is_set():
                result["message"] = "任务已被取消"
                break
            row = rows[domain]
            records = [build_record(domain, item.ip, self._ttl, self._comment_prefix) for item in best]
            applied = await client.apply_domain(
                router_http, domain, best_ips, records,
                cleanup_names=self._cleanup_names,
            )
            added = int(applied.get("added") or 0)
            deleted = int(applied.get("deleted") or 0)
            errors = [str(item) for item in (applied.get("errors") or [])]
            result["written"] += added
            row.update({
                "state": "已写入" if added else "写入失败",
                "addresses": applied.get("written_addresses") or applied.get("addresses") or best_ips,
                "added": added,
                "deleted": deleted,
                "errors": errors[:3],
                "message": "; ".join(errors[:2]),
            })
            self._log(
                "INFO" if added else "ERROR",
                f"[写入 {index}/{len(eligible)}] {domain}｜原始IP={format_ip_list(row['resolved'])}｜"
                f"优选IP={format_ip_list(best_ips)}｜新增={added}｜清理={deleted}｜"
                f"结果={'成功' if added else '失败'}"
                + (f"｜错误={'; '.join(errors)[:200]}" if errors else ""),
            )
            self._update_live(
                stage="写入 RouterOS", current=domain, done=index, total=len(eligible),
                domains=[dict(item) for item in result["domains"]],
            )
        result["message"] = "完成" if result["written"] else "未写入任何记录"
        return result

    def _collect_domains(self) -> List[str]:
        """合并自定义域名与宿主 PT 站点域名，并应用排除列表。"""
        domains = normalize_domains(self._domains)
        if self._auto_sites:
            try:
                indexers = SitesHelper().get_indexers() or []
            except Exception as err:  # noqa: BLE001 - 站点资源缺失时不影响自定义域名
                logger.debug(f"Cloudflare 优选：读取站点列表失败 {err}")
                indexers = []
            for item in indexers:
                if not isinstance(item, dict):
                    continue
                if item.get("public") in (1, True, "1"):
                    continue
                if item.get("is_active") in (False, 0, "0"):
                    continue
                domain = str(item.get("domain") or "").strip()
                if domain:
                    domains.extend(normalize_domains(domain))
        excluded = set(normalize_domains(self._exclude_domains))
        domains = [item for item in dict.fromkeys(domains) if item not in excluded]
        self._log("INFO", f"[域名] 自定义 + 站点自动收集共 {len(domains)} 个，排除 {len(excluded)} 个")
        return domains

    async def _resolve_domains(
        self, http: AsyncRequestUtils, domains: List[str],
    ) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
        """
        批量解析域名，返回 (域名→IP, 域名→来源说明)。

        - all 模式且关闭日志展示时完全不解析（零网络依赖）
        - system / all 展示模式走容器系统 DNS
        - doh 模式先多源 DoH，全部失败自动回退系统 DNS
        """
        mode = self._check_mode
        if mode == "all" and not self._log_resolved_ips:
            return {item: [] for item in domains}, {item: "未解析" for item in domains}
        ipv6 = self._ip_version == "46"
        semaphore = asyncio.Semaphore(8)

        async def _one(domain: str) -> Tuple[str, List[str], str]:
            async with semaphore:
                if mode == "doh":
                    ips = await self._resolve_domain(http, domain)
                    if ips:
                        return domain, ips, "DoH"
                    ips = await resolve_system_dns(domain, timeout=2.0, ipv6=ipv6)
                    return domain, ips, "系统DNS(DoH回退)" if ips else "解析失败"
                ips = await resolve_system_dns(domain, timeout=2.0, ipv6=ipv6)
                return domain, ips, "系统DNS" if ips else "解析失败"

        resolved_map: Dict[str, List[str]] = {}
        source_map: Dict[str, str] = {}
        # 整批解析设总超时：DNS/DoH 全面不可用时快速放行，交给 fallback 处理
        overall = max(10.0, min(60.0, len(domains) * 0.4))
        try:
            answers = await asyncio.wait_for(
                asyncio.gather(*(_one(item) for item in domains)), timeout=overall,
            )
        except asyncio.TimeoutError:
            self._log("WARN", f"[解析] 域名解析整体超时（{overall:.0f}s），按 fallback 设置继续处理")
            return {item: [] for item in domains}, {item: "解析超时" for item in domains}
        for domain, ips, source in answers:
            resolved_map[domain] = ips
            source_map[domain] = source
        return resolved_map, source_map

    async def _resolve_domain(self, http: AsyncRequestUtils, domain: str) -> List[str]:
        """多源 DoH 解析 A/AAAA 记录；任一来源成功即停止尝试该记录类型。"""
        record_types = ("A", "AAAA") if self._ip_version == "46" else ("A",)
        dead = getattr(self, "_doh_dead", set())
        urls = [item for item in parse_doh_urls(self._doh_urls or self._doh_url) if item not in dead]
        found: List[str] = []
        for record_type in record_types:
            for url in urls:
                if url in dead:
                    continue
                try:
                    response = await http.request(
                        "GET", url,
                        params={"name": domain, "type": record_type},
                        headers={"Accept": "application/dns-json"},
                    )
                except Exception as err:  # noqa: BLE001 - 单个来源失败继续下一个
                    logger.debug(f"Cloudflare 优选：DoH 解析 {domain}/{record_type} 失败 {err}")
                    dead.add(url)
                    continue
                if response is None or int(getattr(response, "status_code", 0) or 0) != 200:
                    dead.add(url)
                    continue
                try:
                    answers = parse_doh_answer(response.json())
                except Exception:  # noqa: BLE001
                    dead.add(url)
                    continue
                if answers:
                    found.extend(answers)
                    dead.discard(url)
                    break
                dead.add(url)
        return list(dict.fromkeys(found))

    # ------------------------------------------------------------------
    # IP 来源：官方网段 + 第三方/个人 IP 池
    # ------------------------------------------------------------------

    async def _load_ip_sources(self, http: AsyncRequestUtils, force: bool = False) -> Dict[str, Any]:
        """
        加载并合并所有 IP 来源。

        - 官方：Cloudflare 公布的 ips-v4/ips-v6，按周期刷新并缓存；失败回落到内置常量
        - 自定义：多个第三方/个人维护的 URL，每个源独立缓存，失败继续使用上次结果
        - 手动：配置里的 pool_text 始终生效，不依赖网络
        """
        cache = dict(self.get_data(_CIDR_KEY) or {})
        pool_cache = dict(cache.get("pools") or {})
        custom_urls = _to_str_list_urls(self._pool_urls)
        ttl = self._pool_refresh_days * 86400
        fresh = time.time() - float(cache.get("ts") or 0) < ttl
        source_mode = self._cidr_source
        use_official = source_mode in ("official", "official+custom")
        use_custom = source_mode in ("custom", "official+custom")
        # 缓存里没有官方段或自定义池结果时，即使仍在 TTL 内也重试，避免一次失败后长期不刷新
        missing = (use_official and not cache.get("v4")) or (
            use_custom and bool(custom_urls) and not pool_cache
        )
        need_fetch = force or (self._refresh_cidrs and (not fresh or missing))
        sources: List[Dict[str, Any]] = []

        # Cloudflare 官方公布网段（缓存里只存真正拉到的官方文本，内置段不冒充官方）
        cached_v4 = str(cache.get("v4") or "")
        cached_v6 = str(cache.get("v6") or "")
        v4_text, v6_text = cached_v4, cached_v6
        official_ok = bool(cached_v4) and bool(cache.get("official_ok", True))
        fetched_official = False
        if use_official and need_fetch:
            fetched_v4 = await self._fetch_text(http, self._official_v4_url)
            fetched_v6 = await self._fetch_text(http, self._official_v6_url)
            if fetched_v4:
                v4_text, v6_text, official_ok = fetched_v4, fetched_v6, True
                fetched_official = True
                self._log("INFO", f"[IP池] Cloudflare 官方网段已刷新：IPv4 {len(parse_cidrs(v4_text))} 段，"
                                  f"IPv6 {len(parse_cidrs(v6_text))} 段")
            else:
                official_ok = False
                self._log("WARN", f"[IP池] 官方网段拉取失败（{self._official_v4_url}），"
                                  f"沿用缓存或内置官方段")
        elif not use_official:
            v4_text = ""
            v6_text = ""
            official_ok = False
        if not v4_text:
            v4_text = "\n".join(DEFAULT_IPV4_CIDRS)
            official_ok = False
        if not v6_text:
            v6_text = "\n".join(DEFAULT_IPV6_CIDRS)
        sources.append({
            "name": "Cloudflare 官方 ips-v4/ips-v6" if use_official else "内置官方网段（未联网）",
            "url": self._official_v4_url if use_official else "",
            "ok": official_ok or not use_official,
            "ips": 0,
            "cidrs": len(parse_cidrs(v4_text)) + len(parse_cidrs(v6_text)),
            "error": "" if official_ok or not use_official else "拉取失败，使用内置段或缓存",
            "cached": bool(cached_v4) and not fetched_official,
        })

        pool_ips: List[str] = []
        pool_cidrs: List[str] = []
        seen_ips: set[str] = set()
        seen_cidrs: set[str] = set()

        def _merge(parsed: Dict[str, Any]) -> Tuple[int, int]:
            added_ips = 0
            added_cidrs = 0
            for ip in parsed["ips"]:
                if ip not in seen_ips:
                    seen_ips.add(ip)
                    pool_ips.append(ip)
                    added_ips += 1
            for cidr in parsed["cidrs"]:
                if cidr not in seen_cidrs:
                    seen_cidrs.add(cidr)
                    pool_cidrs.append(cidr)
                    added_cidrs += 1
            return added_ips, added_cidrs

        # 第三方 / 个人维护的 IP 池 URL
        urls = custom_urls
        if use_custom:
            for url in urls:
                entry = dict(pool_cache.get(url) or {})
                text = ""
                ok = False
                error = ""
                cached = False
                if need_fetch:
                    text = await self._fetch_text(http, url)
                    if text:
                        ok = True
                        parsed = parse_ip_pool(text)
                        entry = {
                            "ips": parsed["ips"], "cidrs": parsed["cidrs"],
                            "ts": time.time(),
                        }
                        pool_cache[url] = entry
                    else:
                        error = "拉取失败或内容为空"
                if not ok and entry:
                    cached = True
                ips_count = 0
                cidrs_count = 0
                if ok or cached:
                    ips_count, cidrs_count = _merge({
                        "ips": list(entry.get("ips") or []),
                        "cidrs": list(entry.get("cidrs") or []),
                    })
                sources.append({
                    "name": f"自定义 IP 池 {len(sources)}",
                    "url": url,
                    "ok": ok,
                    "ips": ips_count,
                    "cidrs": cidrs_count,
                    "error": error,
                    "cached": cached,
                })
                self._log(
                    "INFO" if ok else "WARN",
                    f"[IP池] {url}：{'成功' if ok else error}"
                    + (f"，IP {ips_count} / 网段 {cidrs_count}" if (ok or cached) else "")
                    + ("（使用缓存）" if cached and not ok else ""),
                )
        elif urls:
            sources.append({
                "name": "自定义 IP 池（当前来源模式未启用）", "url": ", ".join(urls),
                "ok": False, "ips": 0, "cidrs": 0,
                "error": f"cidr_source={source_mode}，未加载", "cached": False,
            })

        # 手动填写的池始终生效
        manual = parse_ip_pool(self._pool_text)
        manual_ips, manual_cidrs = _merge(manual)
        if self._pool_text.strip():
            sources.append({
                "name": "手动 IP / 网段池", "url": "", "ok": True,
                "ips": manual_ips, "cidrs": manual_cidrs, "error": "", "cached": False,
            })

        # 合并网段；池里的 CIDR 也参与 Cloudflare 归属判定
        networks = merge_networks(
            build_networks(v4_text, v6_text),
            parse_cidrs("\n".join(pool_cidrs)),
        )
        if self._pool_strict_cf:
            pool_ips = [item for item in pool_ips if is_cloudflare_ip(item, networks)]

        if need_fetch:
            # 只有真正拉到数据才推进缓存时间戳；全部失败时 1 小时后自动重试
            succeeded = fetched_official or any(
                item.get("ok") and item.get("url") and str(item.get("name", "")).startswith("自定义")
                for item in sources
            )
            cache = {
                "ts": time.time() if succeeded else time.time() - ttl + 3600,
                "v4": v4_text if fetched_official else cached_v4,
                "v6": v6_text if fetched_official else cached_v6,
                "official_ok": official_ok if use_official else bool(cache.get("official_ok", True)),
                "pools": pool_cache,
            }
            self.save_data(_CIDR_KEY, cache)
        summary = {
            "networks": networks,
            "pool_ips": pool_ips,
            "pool_cidrs": pool_cidrs,
            "sources": sources,
            "official_ok": official_ok,
            "source_mode": source_mode,
            "network_count": len(networks),
            "pool_count": len(pool_ips),
            "updated_text": _now_text(),
            "from_cache": not need_fetch,
        }
        self.save_data(_POOL_KEY, {key: value for key, value in summary.items() if key != "networks"})
        return summary

    @staticmethod
    async def _fetch_text(http: AsyncRequestUtils, url: str) -> str:
        """拉取纯文本 IP 池 / 官方网段，失败返回空串。"""
        try:
            response = await http.request("GET", url)
        except Exception as err:  # noqa: BLE001 - 单个源失败不影响其它源
            logger.debug(f"Cloudflare 优选：拉取 {url} 失败 {err}")
            return ""
        if response is None or int(getattr(response, "status_code", 0) or 0) != 200:
            return ""
        try:
            return str(response.text or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _refresh_pool_async(self) -> Dict[str, Any]:
        """强制刷新一次 IP 来源并返回摘要。"""
        http = AsyncRequestUtils(timeout=self._pool_timeout, verify=False)
        return await self._load_ip_sources(http, force=True)

    async def _check_router_async(self) -> Tuple[bool, str]:
        """供 /check 接口使用的连通性检测。"""
        http = AsyncRequestUtils(timeout=self._router_timeout, verify=self._router_verify)
        client = RouterOSClient(
            normalize_base_url(self._router_host, self._router_port, self._router_ssl),
            self._router_user,
            self._router_password,
            timeout=self._router_timeout,
            verify=self._router_verify,
        )
        return await client.check(http)

    # ------------------------------------------------------------------
    # 结果落库、实时进度与通知
    # ------------------------------------------------------------------

    def _save_result(self, result: Dict[str, Any], started: float) -> None:
        """写入统计与历史。"""
        duration = round(max(time.time() - started, 0.1), 1)
        stats = dict(self.get_data(_STATS_KEY) or {})
        stats.update({
            "last_run_text": _now_text(),
            "last_trigger": result.get("trigger", "-"),
            "last_duration": duration,
            "last_result": {
                "domains_total": result.get("domains_total", 0),
                "domains_cf": result.get("domains_cf", 0),
                "probed": result.get("probed", 0),
                "alive": result.get("alive", 0),
                "written": result.get("written", 0),
                "best_latency": result.get("best_latency", 0),
                "best": result.get("best", []),
                "message": result.get("message", ""),
                "check_mode": result.get("check_mode", self._check_mode),
            },
            "last_domains": result.get("domains", []),
            "total_written": int(stats.get("total_written") or 0) + int(result.get("written") or 0),
            "total_runs": int(stats.get("total_runs") or 0) + 1,
        })
        self.save_data(_STATS_KEY, stats)
        history = list(self.get_data(_HISTORY_KEY) or [])
        history.insert(0, {
            "time": _now_text(),
            "trigger": result.get("trigger", "-"),
            "written": result.get("written", 0),
            "domains_cf": result.get("domains_cf", 0),
            "best": result.get("best", [])[:1],
            "message": result.get("message", ""),
        })
        self.save_data(_HISTORY_KEY, history[:_MAX_HISTORY])

    def _update_live(self, **kwargs: Any) -> None:
        """更新运行中的实时进度，供详情页与 /live 接口读取。"""
        try:
            live = dict(self.get_data(_LIVE_KEY) or {})
            live.update(kwargs)
            live["updated"] = time.time()
            self.save_data(_LIVE_KEY, live)
        except Exception as err:  # noqa: BLE001 - 进度落库失败不影响业务
            logger.debug(f"Cloudflare 优选：更新实时进度失败 {err}")

    def _notify_result(self, result: Dict[str, Any], duration: float = 0.0) -> None:
        """按通知模式把运行结果交给 MoviePilot 通知链。"""
        if self._notify_mode == "关闭":
            return
        written = int(result.get("written") or 0)
        dry_run = bool(result.get("dry_run"))
        message = str(result.get("message") or "")
        abnormal = (
            (not written and not dry_run)
            or int(result.get("domains_cf") or 0) == 0
            or any(word in message for word in
                   ("失败", "异常", "不足", "未配置", "取消", "未写入", "为空", "忽略"))
        )
        if self._notify_mode == "仅异常" and not abnormal:
            return
        best = result.get("best") or []
        title = ("Cloudflare 优选 IP → RouterOS｜执行异常" if abnormal
                 else "Cloudflare 优选 IP → RouterOS｜执行完成")
        lines = [
            f"触发方式：{result.get('trigger', '-')}",
            f"站点判定：{_CHECK_LABELS.get(result.get('check_mode') or self._check_mode)}",
            f"域名：{result.get('domains_total', 0)} 个，纳入 {result.get('domains_cf', 0)} 个",
            f"测速：候选 {result.get('probed', 0)}，可用 {result.get('alive', 0)}",
            f"写入：{written} 条" + ("（仅测速，未写入）" if dry_run else ""),
        ]
        if duration:
            lines.append(f"耗时：{duration}s")
        if best:
            lines.append("优选：" + "，".join(
                f"{item.get('ip')}({format_latency(float(item.get('latency_ms') or 0))})"
                for item in best[:4]
            ))
        domain_rows = result.get("domains") or []
        if domain_rows:
            lines.append("站点明细：")
            for row in domain_rows[:8]:
                lines.append(
                    f"  {row.get('domain')}｜原IP={format_ip_list(row.get('resolved') or [], 3)}"
                    f"｜优选IP={format_ip_list(row.get('addresses') or [], 3)}"
                    f"｜写入={'是' if row.get('added') else '否'}｜{row.get('state', '-')}"
                )
            if len(domain_rows) > 8:
                lines.append(f"  …另有 {len(domain_rows) - 8} 个站点，详见插件详情页")
        if message:
            lines.append(f"说明：{message}")
        lines.append("通知来自 MoviePilot 插件，点击消息可回到插件详情页。")
        if abnormal:
            lines.append("排查：插件卡片「查看日志」（实时 SSE），或详情页「运行日志」。")
        self._send_message(title, "\n".join(lines))

    def _send_message(self, title: str, text: str, *,
                      channel: Any = None, userid: Any = None) -> None:
        """统一走基类 post_message，沿用宿主通知开关、收件人路由与插件跳转链接。"""
        payload: Dict[str, Any] = {"title": title, "text": text}
        if _PLUGIN_MTYPE is not None:
            payload["mtype"] = _PLUGIN_MTYPE
        if channel:
            payload["channel"] = channel
        if userid:
            payload["userid"] = userid
        try:
            self.post_message(**payload)
        except Exception as err:  # noqa: BLE001 - 通知失败不能影响主流程
            logger.warning(f"Cloudflare 优选：通知发送失败 {err}")

    def _log(self, level: str, message: str) -> None:
        """同时写宿主插件日志（SSE 实时）与插件持久化日志缓冲。"""
        content = _mask_secrets(str(message), getattr(self, "_router_password", ""))
        try:
            logs = list(self.get_data(_LOG_KEY) or [])
            logs.insert(0, {"time": _now_text(), "level": level, "message": content})
            self.save_data(_LOG_KEY, logs[:_MAX_LOGS])
        except Exception as err:  # noqa: BLE001 - 日志落库失败不影响业务
            logger.debug(f"Cloudflare 优选：写入运行日志失败 {err}")
        line = f"Cloudflare 优选：{content}"
        if level == "ERROR":
            logger.error(line)
        elif level == "WARN":
            logger.warning(line)
        elif level == "DEBUG":
            logger.debug(line)
        else:
            logger.info(line)

    def _check_router_sync(self) -> Tuple[bool, str]:
        """同步包装路由器检测，兼容在 Web 请求线程中调用。"""
        try:
            return self._run_coroutine(self._check_router_async())
        except Exception as err:  # noqa: BLE001
            return False, f"检测异常：{err}"

    @staticmethod
    def _run_coroutine(coro: Any) -> Any:
        """已有事件循环时（FastAPI 工作线程）改在独立线程执行，避免 asyncio.run 嵌套报错。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=60)


# ---------------------------------------------------------------------------
# 小工具（放在模块尾部，避免与宿主模块名冲突）
# ---------------------------------------------------------------------------

def _to_str_list_urls(text: str) -> List[str]:
    """解析多行 URL 列表，忽略注释与非 http(s) 行。"""
    result: List[str] = []
    for raw in re.split(r"[\s,;]+", str(text or "")):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith(("http://", "https://")) and value not in result:
            result.append(value)
    return result

