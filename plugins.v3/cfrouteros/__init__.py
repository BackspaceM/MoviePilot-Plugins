"""Cloudflare 优选 IP → RouterOS DNS static（MoviePilot V3 插件）。

流程：
1. 收集目标域名：宿主里已启用的私有 PT 站点 + 用户自定义域名；
2. 用 DoH 解析每个域名，只有解析结果落在 Cloudflare 官方网段内才算"符合"；
3. 在 Cloudflare 网段内采样候选 IP，并发做 TCP(/TLS) 握手测速，取最快若干个；
4. 通过 RouterOS v7 REST 写入 /ip/dns/static（A 记录，带 `cfopt:<域名>` 备注），
   先清理本插件旧记录再写入，避免残留过期 IP；
5. 结果写入插件数据并可选通知，支持定时、远程命令与 API 手动触发。
"""

from __future__ import annotations

import asyncio
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

from .cfopt import (
    CF_V4_LIST_URL,
    CF_V6_LIST_URL,
    DEFAULT_DOH_URL,
    ProbeResult,
    build_networks,
    build_record,
    cloudflare_ips,
    format_latency,
    normalize_domains,
    parse_doh_answer,
    pick_fastest,
    probe_many,
    sample_ips,
)
from .routeros import RouterOSClient, normalize_base_url

_STATS_KEY = "stats"
_HISTORY_KEY = "history"
_CIDR_KEY = "cidr_cache"
_MAX_HISTORY = 50
_CMD_ACTION = "cfrouteros_run"
_ONCE_JOB_ID = "CFRouterOS.RunOnce"
_STALE_RUN_SECONDS = 1800
_CIDR_TTL = 7 * 86400
_SITE_ONLY_ERROR = "未配置任何域名"


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


class CFRouterOS(_PluginBase):
    """Cloudflare 优选 IP 写入 RouterOS DNS static。"""

    plugin_name = "Cloudflare 优选 IP → RouterOS"
    plugin_desc = (
        "识别解析到 Cloudflare 的 PT 站点，实测 Cloudflare 官方网段内的 IP 延迟，"
        "取最优若干写入 RouterOS 的 /ip/dns/static，实现内网 DNS 优选解析。"
    )
    plugin_icon = (
        "https://raw.githubusercontent.com/GITHUB_USER/MoviePilot-Plugins/main/icons/cfrouteros.png"
    )
    plugin_version = "1.0.0"
    plugin_label = "网络,Cloudflare,RouterOS,DNS"
    plugin_author = "BackspaceM"
    plugin_config_prefix = "cfrouteros_"
    plugin_order = 77
    auth_level = 1

    # --- 基础 ---
    _enabled: bool = False
    _notify: bool = True
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

    # --- 目标域名 ---
    _domains: str = ""
    _auto_sites: bool = True
    _doh_url: str = DEFAULT_DOH_URL
    _doh_timeout: float = 8.0

    # --- 优选 ---
    _refresh_cidrs: bool = True
    _ip_version: str = "4"
    _probe_count: int = 80
    _probe_port: int = 443
    _probe_timeout: float = 2.0
    _probe_concurrency: int = 32
    _use_tls: bool = False
    _top_n: int = 4
    _keep_previous: bool = True

    # --- 写入 ---
    _ttl: str = "10m"
    _comment_prefix: str = "cfopt"
    _cleanup_names: bool = False
    _min_success: int = 1

    # --- 运行态 ---
    _state_lock = threading.Lock()
    _running: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_started: float = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """载入配置并复位运行态。"""
        self.stop_service()
        self._enabled = False
        self._notify = True
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
        self._auto_sites = True
        self._doh_url = DEFAULT_DOH_URL
        self._doh_timeout = 8.0
        self._refresh_cidrs = True
        self._ip_version = "4"
        self._probe_count = 80
        self._probe_port = 443
        self._probe_timeout = 2.0
        self._probe_concurrency = 32
        self._use_tls = False
        self._top_n = 4
        self._keep_previous = True
        self._ttl = "10m"
        self._comment_prefix = "cfopt"
        self._cleanup_names = False
        self._min_success = 1
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._cron = str(config.get("cron") or "0 5 * * *").strip()
        self._dry_run = bool(config.get("dry_run"))
        self._router_host = str(config.get("router_host") or "").strip()
        self._router_port = max(_to_int(config.get("router_port"), 443), 1)
        self._router_ssl = bool(config.get("router_ssl", True))
        self._router_verify = bool(config.get("router_verify"))
        self._router_user = str(config.get("router_user") or "admin").strip()
        self._router_password = str(config.get("router_password") or "")
        self._router_timeout = max(_to_float(config.get("router_timeout"), 10.0), 1.0)
        self._domains = str(config.get("domains") or "")
        self._auto_sites = bool(config.get("auto_sites", True))
        self._doh_url = str(config.get("doh_url") or DEFAULT_DOH_URL).strip()
        self._doh_timeout = max(_to_float(config.get("doh_timeout"), 8.0), 1.0)
        self._refresh_cidrs = bool(config.get("refresh_cidrs", True))
        self._ip_version = str(config.get("ip_version") or "4")
        self._probe_count = min(max(_to_int(config.get("probe_count"), 80), 1), 1000)
        self._probe_port = min(max(_to_int(config.get("probe_port"), 443), 1), 65535)
        self._probe_timeout = min(max(_to_float(config.get("probe_timeout"), 2.0), 0.2), 15.0)
        self._probe_concurrency = min(max(_to_int(config.get("probe_concurrency"), 32), 1), 256)
        self._use_tls = bool(config.get("use_tls"))
        self._top_n = min(max(_to_int(config.get("top_n"), 4), 1), 8)
        self._keep_previous = bool(config.get("keep_previous", True))
        self._ttl = str(config.get("ttl") or "10m").strip() or "10m"
        self._comment_prefix = str(config.get("comment_prefix") or "cfopt").strip() or "cfopt"
        self._cleanup_names = bool(config.get("cleanup_names"))
        self._min_success = max(_to_int(config.get("min_success"), 1), 1)

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
            thread.join(timeout=10)
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
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event) -> None:
        """消费远程命令事件。"""
        if not event:
            return
        data = event.event_data or {}
        if not isinstance(data, dict) or data.get("action") != _CMD_ACTION:
            return
        target = data.get("plugin_id")
        if target and target != self.__class__.__name__:
            return
        if not self._enabled:
            self.post_message(title="Cloudflare 优选", text="插件未启用，命令已忽略")
            return
        if not self.start_run(trigger="命令"):
            self.post_message(title="Cloudflare 优选", text="上一轮任务尚未结束，请稍后再试")

    # ------------------------------------------------------------------
    # 表单
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置表单与默认值。"""
        content: List[dict] = [
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
                    self._col(12, 12, [{"component": "VTextarea", "props": {
                        "model": "domains", "label": "自定义域名",
                        "rows": 4,
                        "placeholder": "hdwing.org\npt.example.com",
                        "hint": "每行一个；解析结果必须落在 Cloudflare 网段才会被处理",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "auto_sites", "label": "自动纳入 PT 站点",
                        "hint": "取宿主中已启用的非公开站点域名", "persistentHint": True}}]),
                    self._col(12, 5, [{"component": "VTextField", "props": {
                        "model": "doh_url", "label": "DoH 地址",
                        "placeholder": DEFAULT_DOH_URL,
                        "hint": "用于判断域名是否真的指向 Cloudflare，可换成本地可用的 DoH",
                        "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "doh_timeout", "label": "DoH 超时（秒）",
                        "placeholder": "8", "type": "number"}}]),
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
                    self._col(12, 2, [{"component": "VTextField", "props": {
                        "model": "probe_count", "label": "候选数量",
                        "placeholder": "80", "type": "number",
                        "hint": "每轮从官方网段随机采样", "persistentHint": True}}]),
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
                        "hint": "每个域名写入几条 A 记录", "persistentHint": True}}]),
                    self._col(12, 2, [{"component": "VSwitch", "props": {
                        "model": "use_tls", "label": "TLS 握手测速",
                        "hint": "更贴近真实访问，耗时略高", "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "refresh_cidrs", "label": "自动刷新官方网段",
                        "hint": "每 7 天拉取一次 Cloudflare ips-v4/ips-v6",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "keep_previous", "label": "沿用上轮优选",
                        "hint": "把上轮最优 IP 加入本轮候选，减少抖动",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VSwitch", "props": {
                        "model": "cleanup_names", "label": "接管全部同名记录",
                        "hint": "开启后会删除同域名下非本插件写入的 A 记录，谨慎使用",
                        "persistentHint": True}}]),
                    self._col(12, 3, [{"component": "VTextField", "props": {
                        "model": "comment_prefix", "label": "记录备注前缀",
                        "placeholder": "cfopt",
                        "hint": "最终形如 cfopt:hdwing.org，用于识别本插件记录",
                        "persistentHint": True}}]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [{"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": "工作方式：DoH 解析域名 → 判断是否落在 Cloudflare 官方网段 → "
                                "在网段内随机采样并并发 TCP 握手测速 → 取延迟最低的 N 个写入 "
                                "/ip/dns/static（A 记录，同名多条由 RouterOS 轮询返回）。"
                                "每轮会先删除备注为「前缀:域名」的旧记录再写入，不会动你手写的其他记录。"
                                "内网客户端需把 DNS 指向这台 RouterOS 才会生效。"}}]),
                ],
            },
        ]
        return [{"component": "VForm", "content": content}], {
            "enabled": False,
            "notify": True,
            "dry_run": False,
            "cron": "0 5 * * *",
            "router_host": "",
            "router_port": 443,
            "router_ssl": True,
            "router_verify": False,
            "router_user": "admin",
            "router_password": "",
            "router_timeout": 10,
            "domains": "",
            "auto_sites": True,
            "doh_url": DEFAULT_DOH_URL,
            "doh_timeout": 8,
            "refresh_cidrs": True,
            "ip_version": "4",
            "probe_count": 80,
            "probe_port": 443,
            "probe_timeout": 2.0,
            "probe_concurrency": 32,
            "use_tls": False,
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

    def get_page(self) -> List[dict]:
        """返回插件详情页。"""
        if not self._enabled:
            return [
                {"component": "VAlert", "props": {
                    "type": "warning", "variant": "tonal",
                    "text": "插件未启用，请先在配置中启用并填写 RouterOS 地址与账号。"}}
            ]
        stats = self.get_data(_STATS_KEY) or {}
        last = stats.get("last_result") or {}
        content: List[dict] = [
            {
                "component": "VRow",
                "content": [
                    self._stat_card("符合域名", str(last.get("domains_cf", 0)), "mdi-cloud-check-outline"),
                    self._stat_card("写入记录", str(last.get("written", 0)), "mdi-dns-outline"),
                    self._stat_card("最快延迟", format_latency(float(last.get("best_latency", 0) or 0)),
                                    "mdi-speedometer"),
                    self._stat_card("上轮耗时", f"{stats.get('last_duration', '-')}s", "mdi-timer-outline"),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [{"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal", "class": "mt-2",
                        "text": (f"上次运行：{stats.get('last_run_text', '尚未运行')}　"
                                 f"触发：{stats.get('last_trigger', '-')}　"
                                 f"域名 {last.get('domains_total', 0)} / 符合 {last.get('domains_cf', 0)} / "
                                 f"候选 {last.get('probed', 0)} / 可用 {last.get('alive', 0)} / "
                                 f"写入 {last.get('written', 0)}"
                                 + (f"　说明：{last.get('message')}" if last.get("message") else ""))}}
                    ]),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._col(12, 12, [{"component": "VAlert", "props": {
                        "type": "success" if last.get("best") else "secondary",
                        "variant": "tonal", "class": "mt-2",
                        "title": "本轮优选 IP",
                        "text": "　".join(
                            f"{item.get('ip')}（{format_latency(float(item.get('latency_ms') or 0))}）"
                            for item in (last.get("best") or [])
                        ) or "暂无数据，点击下方按钮执行一次。"}}
                    ]),
                ],
            },
        ]
        rows = stats.get("last_domains") or []
        if rows:
            content.append({
                "component": "VRow",
                "content": [
                    self._col(12, 12, [{"component": "VAlert", "props": {
                        "type": "secondary", "variant": "tonal", "class": "mt-2",
                        "title": "域名处理明细",
                        "text": "\n".join(
                            f"{item.get('domain')}：{item.get('state')}"
                            + (f"　{', '.join(item.get('addresses') or [])}" if item.get("addresses") else "")
                            + (f"　{item.get('message')}" if item.get("message") else "")
                            for item in rows
                        )}}
                    ]),
                ],
            })
        content.append({
            "component": "VRow",
            "content": [
                self._col(12, 12, [
                    {"component": "VBtn", "props": {
                        "variant": "tonal", "color": "primary", "class": "me-2",
                        "prependIcon": "mdi-play", "text": "立即执行"},
                     "actions": [{"component": "VOn", "props": {"event": "click",
                                                                "handler": "api_run"}}]},
                    {"component": "VBtn", "props": {
                        "variant": "tonal", "color": "info",
                        "prependIcon": "mdi-lan-connect", "text": "检测路由器"},
                     "actions": [{"component": "VOn", "props": {"event": "click",
                                                                "handler": "api_check"}}]},
                ]),
            ],
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
             "auth": "bear", "summary": "立即执行一次优选"},
            {"path": "/check", "endpoint": self.api_check, "methods": ["POST"],
             "auth": "bear", "summary": "检测 RouterOS 连通与账号权限"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询上轮结果"},
            {"path": "/clear_cache", "endpoint": self.api_clear_cache, "methods": ["POST"],
             "auth": "bear", "summary": "清空网段缓存与历史"},
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
        return schemas.Response(
            success=started,
            message="已开始执行，稍后刷新页面查看结果" if started else "任务启动失败",
        )

    def api_check(self) -> schemas.Response:
        """检测 RouterOS 连通性、认证与 REST 可用性。"""
        if not self._router_host:
            return schemas.Response(success=False, message="未配置 RouterOS 地址")
        try:
            ok, message = asyncio.run(self._check_router_async())
        except Exception as err:  # noqa: BLE001 - 检测失败要返回可读原因
            return schemas.Response(success=False, message=f"检测异常：{err}")
        return schemas.Response(success=ok, message=message)

    def api_status(self) -> schemas.Response:
        """返回上轮结果。"""
        return schemas.Response(success=True, data={
            "enabled": self._enabled,
            "running": self._running,
            "cron": self._cron,
            "dry_run": self._dry_run,
            "stats": self.get_data(_STATS_KEY) or {},
        })

    def api_clear_cache(self) -> schemas.Response:
        """清空网段缓存与历史记录。"""
        self.save_data(_CIDR_KEY, {})
        self.save_data(_HISTORY_KEY, [])
        return schemas.Response(success=True, message="网段缓存与历史已清空")

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    def run_scheduled(self) -> None:
        """定时入口。"""
        self.start_run(trigger="定时")

    def start_run(self, trigger: str = "手动") -> bool:
        """异步启动一轮优选，避免阻塞调用方。"""
        with self._state_lock:
            if self._running:
                if time.time() - self._run_started < _STALE_RUN_SECONDS:
                    logger.info("Cloudflare 优选：上一轮尚未结束，本次跳过")
                    return False
                logger.warning("Cloudflare 优选：运行状态疑似残留，强制重置后继续")
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
            logger.error(f"Cloudflare 优选执行异常：{err} - {traceback.format_exc()}")
            self._save_result({
                "trigger": trigger,
                "domains_total": 0, "domains_cf": 0, "probed": 0, "alive": 0,
                "written": 0, "best_latency": 0, "best": [], "message": f"执行异常：{err}",
            }, time.time())
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
        result = asyncio.run(self._run_async(trigger))
        self._save_result(result, started)
        self._notify_result(result)

    async def _run_async(self, trigger: str) -> Dict[str, Any]:
        """完整一轮：解析 → 测速 → 写入。"""
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
        }
        if stop_event is not None and stop_event.is_set():
            result["message"] = "任务已被取消"
            return result

        domains = self._collect_domains()
        result["domains_total"] = len(domains)
        if not domains:
            result["message"] = _SITE_ONLY_ERROR
            return result

        doh_http = AsyncRequestUtils(timeout=self._doh_timeout, verify=False)
        v4_text, v6_text = await self._cidr_texts(doh_http)
        networks = build_networks(v4_text, v6_text)
        if not networks:
            result["message"] = "Cloudflare 网段为空"
            return result

        eligible: List[str] = []
        for domain in domains:
            if stop_event is not None and stop_event.is_set():
                result["message"] = "任务已被取消"
                break
            resolved = await self._resolve_domain(doh_http, domain)
            matched = cloudflare_ips(resolved, networks)
            if matched:
                eligible.append(domain)
                result["domains"].append({"domain": domain, "state": "符合", "addresses": []})
            else:
                result["domains"].append({
                    "domain": domain,
                    "state": "跳过",
                    "message": "解析未落在 Cloudflare 网段" if resolved else "DoH 解析失败或无 A 记录",
                })
        result["domains_cf"] = len(eligible)
        if not eligible:
            result["message"] = "没有域名解析到 Cloudflare，未写入"
            return result

        ipv4_only = self._ip_version != "46"
        candidates = sample_ips(networks, self._probe_count, ipv4_only=ipv4_only)
        if self._keep_previous:
            previous = [str(item.get("ip")) for item in ((self.get_data(_STATS_KEY) or {}).get("last_result") or {}).get("best", [])]
            candidates = list(dict.fromkeys(previous + candidates))
        result["probed"] = len(candidates)

        probes: List[ProbeResult] = await probe_many(
            candidates,
            port=self._probe_port,
            timeout=self._probe_timeout,
            concurrency=self._probe_concurrency,
            use_tls=self._use_tls,
            sni=eligible[0],
        )
        alive = [item for item in probes if item.ok]
        result["alive"] = len(alive)
        best = pick_fastest(probes, self._top_n)
        result["best"] = [item.as_dict() for item in best]
        result["best_latency"] = round(best[0].latency_ms, 1) if best else 0
        if len(best) < self._min_success:
            result["message"] = f"可用 IP 不足（{len(best)}/{self._min_success}），未写入"
            return result

        if self._dry_run:
            for row in result["domains"]:
                if row["domain"] in eligible:
                    row["addresses"] = [item.ip for item in best]
            result["message"] = "仅测速模式，未写入路由器"
            return result

        if not self._router_host:
            result["message"] = "未配置 RouterOS 地址，仅完成测速"
            return result

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
            return result

        for domain in eligible:
            if stop_event is not None and stop_event.is_set():
                result["message"] = "任务已被取消"
                break
            records = [build_record(domain, item.ip, self._ttl, self._comment_prefix) for item in best]
            applied = await client.apply_domain(
                router_http, domain, [item.ip for item in best], records,
                cleanup_names=self._cleanup_names,
            )
            result["written"] += int(applied.get("added") or 0)
            for row in result["domains"]:
                if row["domain"] == domain:
                    row["state"] = "已写入" if applied.get("added") else "写入失败"
                    row["addresses"] = applied.get("addresses") or []
                    if applied.get("errors"):
                        row["message"] = "; ".join(applied["errors"][:2])
        result["message"] = "完成" if result["written"] else "未写入任何记录"
        return result

    def _collect_domains(self) -> List[str]:
        """合并自定义域名与宿主 PT 站点域名。"""
        domains = normalize_domains(self._domains)
        if not self._auto_sites:
            return domains
        try:
            indexers = SitesHelper().get_indexers() or []
        except Exception as err:  # noqa: BLE001 - 站点资源缺失时不影响自定义域名
            logger.debug(f"Cloudflare 优选：读取站点列表失败 {err}")
            return domains
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
        return list(dict.fromkeys(domains))

    async def _resolve_domain(self, http: AsyncRequestUtils, domain: str) -> List[str]:
        """用 DoH 解析 A 记录。"""
        try:
            response = await http.request(
                "GET",
                self._doh_url,
                params={"name": domain, "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
        except Exception as err:  # noqa: BLE001 - 单个域名失败不阻断整体
            logger.debug(f"Cloudflare 优选：DoH 解析 {domain} 失败 {err}")
            return []
        if response is None or int(getattr(response, "status_code", 0) or 0) != 200:
            return []
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return []
        return parse_doh_answer(payload)

    async def _cidr_texts(self, http: AsyncRequestUtils) -> Tuple[str, str]:
        """返回官方网段文本，按需刷新并缓存 7 天。"""
        cache = self.get_data(_CIDR_KEY) or {}
        fresh = time.time() - float(cache.get("ts") or 0) < _CIDR_TTL
        if cache.get("v4") and fresh:
            return str(cache.get("v4")), str(cache.get("v6") or "")
        if not self._refresh_cidrs:
            return str(cache.get("v4") or ""), str(cache.get("v6") or "")
        v4 = await self._fetch_text(http, CF_V4_LIST_URL)
        v6 = await self._fetch_text(http, CF_V6_LIST_URL)
        if v4:
            self.save_data(_CIDR_KEY, {"ts": time.time(), "v4": v4, "v6": v6})
            return v4, v6
        return str(cache.get("v4") or ""), str(cache.get("v6") or "")

    @staticmethod
    async def _fetch_text(http: AsyncRequestUtils, url: str) -> str:
        try:
            response = await http.request("GET", url)
        except Exception as err:  # noqa: BLE001 - 刷新失败时回落内置段
            logger.debug(f"Cloudflare 优选：拉取 {url} 失败 {err}")
            return ""
        if response is None or int(getattr(response, "status_code", 0) or 0) != 200:
            return ""
        try:
            return str(response.text or "")
        except Exception:  # noqa: BLE001
            return ""

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
    # 结果落库与通知
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

    def _notify_result(self, result: Dict[str, Any]) -> None:
        """按配置发送运行结果通知。"""
        if not self._notify:
            return
        best = result.get("best") or []
        lines = [
            f"域名：{result.get('domains_total', 0)} 个，符合 Cloudflare {result.get('domains_cf', 0)} 个",
            f"测速：候选 {result.get('probed', 0)}，可用 {result.get('alive', 0)}",
            f"写入：{result.get('written', 0)} 条" + ("（仅测速，未写入）" if result.get("dry_run") else ""),
        ]
        if best:
            lines.append("优选：" + "，".join(
                f"{item.get('ip')}({format_latency(float(item.get('latency_ms') or 0))})" for item in best[:4]
            ))
        if result.get("message"):
            lines.append(f"说明：{result['message']}")
        level = "info" if result.get("written") or result.get("dry_run") else "warning"
        self.post_message(title="Cloudflare 优选 IP → RouterOS", text="\n".join(lines), level=level)
