"""RouterOS 外网访问 / 端口映射向导（B1）—— MoviePilot V3 插件主类。

从 MoviePilot 已连接的媒体服务器、下载器 WebUI 和 MP 本体里挑服务，一次生成
dst-nat 映射 + forward 放行（可选 Hairpin NAT），备注里带上域名和「外网端口 →
内网服务」。确认后批量下发，并支持部署前后连通性测试。

安全边界：
- 只新增带 ``MP-B1[<服务>:<用途>]`` 标签的条目，绝不修改/删除用户已有规则；
- 回滚只删本次新建条目，同标签已存在则跳过（幂等）；
- 公网映射默认拦截，必须显式勾选「允许公网映射」；备份失败默认中止写入；
- 连接参数可从「RouterOS 连接中心」下拉带入，不安装时手填行为不变。

纯工具型插件：不注册定时任务，所有写操作都由用户在详情页明确触发。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import html as _html
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.types import EventType, MessageType
from app.sdk.events import eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

try:
    from app.sdk.network import AsyncRequestUtils
except Exception:  # noqa: BLE001 - 离线桩环境
    AsyncRequestUtils = None  # type: ignore

try:
    from app import schemas

    Response = schemas.Response
except Exception:  # noqa: BLE001
    try:
        from pydantic import BaseModel
    except Exception:  # noqa: BLE001
        BaseModel = object  # type: ignore

    class Response(BaseModel):  # type: ignore
        """宿主不可用时的三字段 envelope 降级实现。"""

        success: bool = True
        message: str = ""
        data: Any = None


try:
    _PLUGIN_MTYPE = MessageType.Plugin
except Exception:  # noqa: BLE001
    _PLUGIN_MTYPE = None

from . import discovery, engine, planner, probe, roslink
from .clients import RouterOSConfigClient, build_config_client
from .planner import (DEFAULT_PREFIX, RISK_COLORS, RISK_LABELS, actions_from_plan,
                      md_safe, now_text)

PLUGIN_ID = "RouterOSRemoteAccessWizard"
_LOG_PREFIX = "ROS外网访问"
_MAX_LOGS = 200
_MAX_SNAPSHOTS = 10

_KEY_PLAN = "b1_plan"
_KEY_LAST_DEPLOY = "b1_last_deploy"
_KEY_ROLLBACK = "b1_rollback_entries"
_KEY_LOGS = "b1_logs"
_KEY_CONN = "b1_conn"
_KEY_SNAPSHOTS = "b1_snapshots"
_KEY_PROBE = "b1_probe"

_NOTIFY_LABELS = {"issue": "仅异常/失败", "all": "每次部署", "none": "关闭"}
_KIND_LABELS = {"media": "媒体服务器", "downloader": "下载器",
                "mp": "MoviePilot", "manual": "手填"}


def _esc(value: Any) -> str:
    """动态文本转义：防 XSS，并避免 [xxx] 被前端当 Markdown 参考式链接渲染。"""
    return (_html.escape(str(value if value is not None else ""), quote=False)
            .replace("[", "［").replace("]", "］"))


def _cmd_esc(value: Any) -> str:
    return md_safe(_html.escape(str(value if value is not None else ""), quote=False),
                   keep_star=True)


class RouterOSRemoteAccessWizard(_PluginBase):
    # 类级元数据一律用字符串字面量：官方版本检查以 AST 提取，常量引用会被判为「未声明」
    plugin_name = "RouterOS 外网访问向导"
    plugin_desc = ("从 MoviePilot 已连接的媒体服务器、下载器和 MP 本体里挑服务，一键生成 "
                   "dst-nat 映射 + 防火墙放行，预览「外网端口 → 内网服务」后批量写入；"
                   "带域名备注、连通性测试、自动备份与失败回滚，只新增带 MP-B1 标签的条目。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/routerosremoteaccesswizard.png"
    plugin_version = "1.0.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "routerosremoteaccesswizard_"
    plugin_color = "#1976D2"
    system_version = ">=3.0.0"
    plugin_order = 52

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cfg: Dict[str, Any] = {}
        self._address = ""
        self._port = 443
        self._use_ssl = True
        self._verify = False
        self._username = ""
        self._password = ""
        self._timeout = 10
        self._selected_services: Any = []
        self._services_manual = ""
        self._mp_host = ""
        self._mp_port = 3000
        self._mp_scheme = "http"
        self._services: List[discovery.Service] = []
        self._service_errors: List[str] = []
        self._media_configs: Dict[str, Any] = {}
        self._downloader_configs: Dict[str, Any] = {}
        self._wan_if = "ether1"
        self._protocol = "tcp"
        self._lan_subnet = ""
        self._domain = ""
        self._public_host = ""
        self._wan_port_start = 18000
        self._probe_timeout = 5
        self._allow_public = False
        self._prefix = DEFAULT_PREFIX
        self._dry_run = False
        self._auto_backup = True
        self._require_backup = True
        self._auto_rollback = True
        self._notify_mode = "issue"
        self._display_limit = 60

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._cfg = cfg

        overrides, note = roslink.connection_overrides(cfg.get("ros_connection"), "std")
        conn = dict(cfg)
        conn.update(overrides)
        if note:
            (logger.info if overrides else logger.warning)(f"{_LOG_PREFIX}｜连接中心：{note}")

        self._address = str(conn.get("router_address") or "").strip()
        self._username = str(conn.get("router_username") or "").strip()
        self._password = str(conn.get("router_password") or "")
        self._use_ssl = bool(conn.get("router_ssl", True))
        self._verify = bool(cfg.get("verify", False))
        try:
            self._port = int(conn.get("router_port") or 443)
        except (TypeError, ValueError):
            self._port = 443
        try:
            self._timeout = max(3, min(60, int(cfg.get("timeout") or 10)))
        except (TypeError, ValueError):
            self._timeout = 10

        self._selected_services = cfg.get("connected_services") or []
        self._services_manual = str(cfg.get("services_manual") or "")
        self._mp_host = str(cfg.get("mp_host") or "").strip()
        self._mp_scheme = "https" if str(cfg.get("mp_scheme") or "http").lower() == "https" else "http"
        try:
            self._mp_port = int(cfg.get("mp_port") or 3000)
        except (TypeError, ValueError):
            self._mp_port = 3000

        self._wan_if = str(cfg.get("wan_if") or "ether1").strip()
        self._protocol = str(cfg.get("protocol") or "tcp").strip().lower()
        self._lan_subnet = str(cfg.get("lan_subnet") or "").strip()
        self._domain = str(cfg.get("domain") or "").strip()
        self._public_host = str(cfg.get("public_host") or "").strip()
        try:
            self._wan_port_start = int(cfg.get("wan_port_start") or 18000)
        except (TypeError, ValueError):
            self._wan_port_start = 18000
        try:
            self._probe_timeout = max(1, min(30, int(cfg.get("probe_timeout") or 5)))
        except (TypeError, ValueError):
            self._probe_timeout = 5
        self._allow_public = bool(cfg.get("allow_public"))
        self._prefix = str(cfg.get("comment_prefix") or DEFAULT_PREFIX).strip() or DEFAULT_PREFIX
        self._dry_run = bool(cfg.get("dry_run"))
        self._auto_backup = bool(cfg.get("auto_backup", True))
        self._require_backup = bool(cfg.get("require_backup", True))
        self._auto_rollback = bool(cfg.get("auto_rollback", True))
        self._notify_mode = str(cfg.get("notify_mode") or "issue")
        try:
            self._display_limit = max(10, min(200, int(cfg.get("display_limit") or 60)))
        except (TypeError, ValueError):
            self._display_limit = 60

        self._media_configs, self._downloader_configs = discovery.load_connected_configs()
        self._rediscover()

        plan = self._make_plan()
        self.save_data(_KEY_PLAN, plan)
        self._log("INFO", f"配置已载入：服务 {len(self._services)} 个，"
                          f"待确认动作 {len(plan.get('actions') or [])} 条"
                          + ("；校验未通过" if plan.get("blocked") else ""))

    def _rediscover(self) -> None:
        self._services, self._service_errors = discovery.discover_services(
            self._selected_services, self._services_manual,
            media_configs=self._media_configs,
            downloader_configs=self._downloader_configs,
            mp_host=self._mp_host, mp_port=self._mp_port,
            mp_scheme=self._mp_scheme,
        )
        for err in self._service_errors:
            self._log("WARN", f"服务发现：{err}")

    def _planner_options(self) -> Dict[str, Any]:
        return {
            "wan_if": self._wan_if, "protocol": self._protocol,
            "lan_subnet": self._lan_subnet, "domain": self._domain,
            "public_host": self._public_host,
            "wan_port_start": self._wan_port_start,
            "probe_timeout": self._probe_timeout,
        }

    def _make_plan(self, occupied_ports: Any = ()) -> Dict[str, Any]:
        plan = planner.render_plan(self._services, self._planner_options(),
                                   self._prefix, self._allow_public, occupied_ports)
        if self._service_errors:
            plan["errors"] = list(plan.get("errors") or []) + list(self._service_errors)
            plan["blocked"] = True
        return plan

    def get_state(self) -> bool:
        return True

    def stop_service(self) -> None:
        return None

    def _secrets(self) -> List[str]:
        return [self._password] if self._password else []

    # ------------------------------------------------------------------
    # 客户端 / 协程
    # ------------------------------------------------------------------

    @staticmethod
    def _run_coroutine(coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=180)

    def _make_client(self) -> Tuple[Optional[RouterOSConfigClient], Any]:
        if AsyncRequestUtils is None or not self._address:
            return None, None
        http = AsyncRequestUtils(verify=self._verify)
        client = build_config_client(
            self._address, self._username, self._password, http,
            port=self._port, use_ssl=self._use_ssl, timeout=self._timeout,
            dry_run=self._dry_run,
        )
        return client, http

    def _options(self) -> Dict[str, Any]:
        return {
            "auto_backup": self._auto_backup,
            "require_backup": self._require_backup,
            "auto_rollback": self._auto_rollback,
            "dry_run": self._dry_run,
            "backup_name": "mp-b1-" + time.strftime("%Y%m%d-%H%M%S"),
        }

    # ------------------------------------------------------------------
    # 日志 / 通知 / 快照
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        text = str(message or "")
        for secret in self._secrets():
            if len(secret) >= 4:
                text = text.replace(secret, secret[:2] + "***" + secret[-1:])
        try:
            logs = list(self.get_data(_KEY_LOGS) or [])
            logs.insert(0, {"time": now_text(), "level": level, "message": text})
            self.save_data(_KEY_LOGS, logs[:_MAX_LOGS])
        except Exception:  # noqa: BLE001
            pass
        line = f"{_LOG_PREFIX}：{text}"
        if level == "ERROR":
            logger.error(line)
        elif level == "WARN":
            logger.warning(line)
        elif level == "DEBUG":
            logger.debug(line)
        else:
            logger.info(line)

    def _send_message(self, title: str, text: str, *, channel: Any = None,
                      userid: Any = None) -> None:
        payload: Dict[str, Any] = {"title": _esc(title), "text": _esc(text)}
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

    def _notify_deploy(self, report: Dict[str, Any], title: str) -> None:
        mode = self._notify_mode
        if mode == "none":
            return
        if mode == "issue" and report.get("ok") and not report.get("dry_run"):
            return
        self._send_message(title, str(report.get("message") or ""))

    def _save_snapshot(self, snapshot: Dict[str, Any]) -> str:
        try:
            base = Path(self.get_data_path()) / "snapshots"
            base.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
            path = base / f"snapshot-{stamp}.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            files = sorted(base.glob("snapshot-*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[_MAX_SNAPSHOTS:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            items = [{"name": p.name, "size": p.stat().st_size,
                      "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(p.stat().st_mtime))}
                     for p in files[:_MAX_SNAPSHOTS]]
            self.save_data(_KEY_SNAPSHOTS, items)
            self._log("INFO", f"配置快照已保存：{path}（{snapshot.get('count', 0)} 条现有条目）")
            return str(path)
        except Exception as err:  # noqa: BLE001
            self._log("WARN", f"配置快照写盘失败：{err}")
            return ""

    def _list_snapshots(self) -> List[Dict[str, Any]]:
        data = self.get_data(_KEY_SNAPSHOTS) or []
        return [dict(x) for x in data if isinstance(x, dict)][:_MAX_SNAPSHOTS]

    def _plan(self) -> Dict[str, Any]:
        data = self.get_data(_KEY_PLAN) or {}
        return dict(data) if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------

    @staticmethod
    def _col(md: int, content: List[dict]) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": content}

    @staticmethod
    def _fld(model: str, label: str, hint: str = "", placeholder: str = "",
             field_type: str = "text") -> dict:
        props: Dict[str, Any] = {"model": model, "label": label,
                                 "hint": hint, "persistentHint": True}
        if placeholder:
            props["placeholder"] = placeholder
        if field_type == "number":
            props["type"] = "number"
        return {"component": "VTextField", "props": props}

    @staticmethod
    def _sw(model: str, label: str, hint: str = "") -> dict:
        return {"component": "VSwitch", "props": {
            "model": model, "label": label, "hint": hint, "persistentHint": True}}

    @staticmethod
    def _sel(model: str, label: str, items: List[Dict[str, Any]],
             hint: str = "", multiple: bool = False) -> dict:
        props: Dict[str, Any] = {"model": model, "label": label, "items": items,
                                 "hint": hint, "persistentHint": True}
        if multiple:
            props.update({"multiple": True, "chips": True, "closableChips": True,
                          "clearable": True})
        return {"component": "VSelect", "props": props}

    @staticmethod
    def _section_title(text: str) -> dict:
        return {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-4 mb-1"},
                "text": text}

    def _service_items(self) -> List[Dict[str, str]]:
        return discovery.service_items(self._media_configs, self._downloader_configs)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        service_items = self._service_items()
        conn_items = roslink.connection_items()
        content: List[dict] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                              "class": "mb-4"},
             "content": {"text": "从 MoviePilot 已连接的服务里挑几个，一次生成 NAT 映射和防火墙放行。"
                                 "只新增带 MP-B1 标签的规则；写之前会留备份，中途失败自动回滚。"}},
            self._section_title("目标路由器"),
            {"component": "VRow", "content": [
                self._col(4, [self._sel("ros_connection", "已保存的路由器（连接中心）",
                                        conn_items or [{"title": "没找到连接中心配置", "value": ""}],
                                        "选中后自动带入连接参数，下面留空就行；不选就用手填的")]),
                self._col(4, [self._fld("router_address", "RouterOS 地址", "", "192.168.88.1")]),
                self._col(2, [self._fld("router_port", "端口", "", "443", "number")]),
                self._col(2, [self._sw("router_ssl", "使用 HTTPS", "自签证书通常仍可连接")]),
            ]},
            {"component": "VRow", "content": [
                self._col(4, [self._fld("router_username", "API 用户名", "单独建个最小权限的账号", "mp-api")]),
                self._col(4, [self._fld("router_password", "API 密码", "明文存在插件配置里")]),
                self._col(2, [self._fld("timeout", "超时（秒）", "", "10", "number")]),
                self._col(2, [self._sw("verify", "校验证书", "自签证书请关闭")]),
            ]},
            self._section_title("选服务"),
            {"component": "VRow", "content": [
                self._col(12, [self._sel("connected_services", "已连接服务（可多选）",
                                         service_items,
                                         "下拉里是 MP 已配好的实例，地址端口自动读出；下拉是空的就用手填",
                                         multiple=True)]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, [{"component": "VTextarea", "props": {
                    "model": "services_manual", "label": "手动补充服务（每行一个）", "rows": 4,
                    "autoGrow": True, "persistentHint": True,
                    "hint": "名称|内网地址|内网端口|外网端口|备注；外网端口留空会自动分配，同名可覆盖上面的下拉项",
                    "placeholder": "客厅Emby|http://192.168.88.10:8096||18096|\nQB|192.168.88.20|8080||"}}]),
            ]},
            {"component": "VRow", "content": [
                self._col(5, [self._fld("mp_host", "MP 本体内网地址",
                                        "勾选了「MoviePilot 本体」才需要填", "192.168.88.5")]),
                self._col(3, [self._fld("mp_port", "MP 端口", "", "3000", "number")]),
                self._col(4, [self._sel("mp_scheme", "MP 协议",
                                        [{"title": "http", "value": "http"},
                                         {"title": "https", "value": "https"}])]),
            ]},
            self._section_title("映射设置"),
            {"component": "VRow", "content": [
                self._col(3, [self._fld("wan_if", "WAN 接口", "接外网的那个口", "ether1")]),
                self._col(3, [self._sel("protocol", "协议",
                                        [{"title": "TCP", "value": "tcp"},
                                         {"title": "UDP", "value": "udp"},
                                         {"title": "TCP + UDP", "value": "both"}])]),
                self._col(3, [self._fld("wan_port_start", "外网端口起始",
                                        "自动分配从这里往后找空位", "18000", "number")]),
                self._col(3, [self._fld("lan_subnet", "内网网段（可选）",
                                        "填了才会生成回环规则", "192.168.88.0/24")]),
            ]},
            {"component": "VRow", "content": [
                self._col(4, [self._fld("domain", "域名（写进备注）", "", "movie.example.com")]),
                self._col(4, [self._fld("public_host", "公网地址（测连用）",
                                        "没填域名时用它回连测试", "1.2.3.4")]),
                self._col(4, [self._fld("probe_timeout", "连通性超时（秒）", "", "5", "number")]),
            ]},
            self._section_title("写入选项"),
            {"component": "VRow", "content": [
                self._col(3, [self._sw("allow_public", "允许公网映射",
                                       "不勾就不生成任何映射规则")]),
                self._col(3, [self._sw("dry_run", "试运行（不写入）", "只走流程，核对预览用")]),
                self._col(3, [self._sw("auto_backup", "部署前自动备份", "在 RouterOS 上留 .backup")]),
                self._col(3, [self._sw("require_backup", "备份失败必须中止", "备份没成功就别往下写")]),
            ]},
            {"component": "VRow", "content": [
                self._col(3, [self._sw("auto_rollback", "失败自动回滚", "逆序删掉本次新建条目")]),
                self._col(3, [self._fld("comment_prefix", "管理标签前缀", "", "MP-B1")]),
                self._col(3, [self._sel("notify_mode", "通知方式",
                                        [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()])]),
                self._col(3, [self._fld("display_limit", "详情页最多显示行数", "", "60", "number")]),
            ]},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                                              "class": "mt-3"},
             "content": {"text": "保存后到详情页点「生成 / 刷新预览」，核对完再点「确认一键部署」。"
                                 "填了内网网段会多一条回环规则，内网设备才能用域名访问。"}},
            {"component": "VAlert", "props": {"type": "secondary", "variant": "text",
                                              "class": "mt-2"},
             "content": {"text": "映射等于把这些服务放到公网，先确认有强密码或 HTTPS。"
                                 "同一个端口别在「场景模板一键部署」里再配一次。"}},
        ]
        defaults: Dict[str, Any] = {
            "ros_connection": "", "router_address": "", "router_port": 443,
            "router_ssl": True, "router_username": "", "router_password": "",
            "timeout": 10, "verify": False,
            "connected_services": [], "services_manual": "",
            "mp_host": "", "mp_port": 3000, "mp_scheme": "http",
            "wan_if": "ether1", "protocol": "tcp", "wan_port_start": 18000,
            "lan_subnet": "", "domain": "", "public_host": "", "probe_timeout": 5,
            "allow_public": False, "dry_run": False,
            "auto_backup": True, "require_backup": True, "auto_rollback": True,
            "comment_prefix": DEFAULT_PREFIX, "notify_mode": "issue",
            "display_limit": 60,
        }
        return content, defaults

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def _action_button(self, label: str, api: str, color: str = "primary",
                       icon: str = "", method: str = "post",
                       confirm: bool = False) -> dict:
        props: Dict[str, Any] = {"variant": "tonal", "size": "small",
                                 "class": "ma-1", "color": color}
        if icon:
            props["prepend-icon"] = icon
        return {"component": "VBtn", "props": props, "text": label,
                "events": {"click": {"api": f"plugin/{PLUGIN_ID}/{api}",
                                     "method": method, "params": {}, "confirm": confirm}}}

    @staticmethod
    def _stat_card(title: str, value: str, sub: str, color: str) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
            {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": _esc(title)},
                    {"component": "div", "props": {"class": f"text-h6 font-weight-bold text-{color}"},
                     "text": _esc(value)},
                    {"component": "div", "props": {"class": "text-caption mt-1"},
                     "text": _esc(sub)},
                ]},
            ]},
        ]}

    def get_page(self) -> List[dict]:
        plan = self._plan()
        actions = actions_from_plan(plan)
        rollback_entries = list(self.get_data(_KEY_ROLLBACK) or [])
        limit = self._display_limit

        content: List[dict] = [
            {"component": "VCard", "props": {"variant": "tonal", "class": "mt-3"}, "content": [
                {"component": "VCardTitle", "text": "操作"},
                {"component": "VCardText", "content": [
                    self._action_button("生成 / 刷新预览", "preview", "primary", "mdi-file-search-outline"),
                    self._action_button("确认一键部署", "apply", "error", "mdi-rocket-launch-outline",
                                        confirm=True),
                    self._action_button("连通性测试", "probe", "success", "mdi-lan-connect"),
                    self._action_button("测试连接", "test", "info", "mdi-router-wireless"),
                    self._action_button("仅备份不下发", "backup", "secondary", "mdi-backup-restore"),
                    self._action_button("清空计划", "clear", "warning", "mdi-delete-outline"),
                    self._action_button("回滚最近一次部署", "rollback", "error",
                                        "mdi-undo-variant", confirm=True),
                    self._action_button("刷新状态", "status", "info", "mdi-refresh", method="get"),
                ]},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": ("当前是试运行，所有写入都不生效。" if self._dry_run else
                              "部署前会在 RouterOS 留备份，失败时逆序回滚本次新建的条目。")
                              + (f" 可回滚条目 {len(rollback_entries)} 条。" if rollback_entries else "")},
                ]},
            ]}
        ]

        if plan.get("errors"):
            content.append({"component": "VAlert", "props": {
                "type": "error", "variant": "tonal", "class": "mt-3",
                "title": "参数有问题，先改再部署"},
                "content": {"text": "；".join(_esc(e) for e in (plan.get("errors") or [])[:8])}})
        if plan.get("warnings"):
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "text", "class": "mt-2"},
                "content": {"text": "；".join(_esc(e) for e in (plan.get("warnings") or [])[:5])}})
        if plan.get("unreadable"):
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "text", "class": "mt-2",
                "title": "有几个分区读不到"},
                "content": {"text": "读不到就没法比对是否已存在，确认 API 账号有读权限："
                                    + _esc("、".join(str(s) for s in plan["unreadable"]))}})

        # 服务映射表：外网端口 → 内网服务

        # 待确认计划表
        rows: List[dict] = []
        for a in actions[:limit]:
            color = RISK_COLORS.get(a.risk_level, "info")
            rows.append({"component": "tr", "content": [
                {"component": "td", "props": {"class": "text-caption"}, "text": str(a.index)},
                {"component": "td", "props": {"class": "text-caption"},
                 "text": _esc(a.template_name)},
                {"component": "td", "props": {"class": "text-caption"},
                 "text": _esc(a.section_label)},
                {"component": "td", "props": {"class": "text-caption"},
                 "text": _esc(a.meaning)},
                {"component": "td", "props": {"class": "text-caption font-weight-bold "
                                                        f"text-{color}"},
                 "text": _esc(f"{RISK_LABELS.get(a.risk_level, '?')}｜{a.risk}")},
                {"component": "td", "props": {"class": "text-caption"},
                 "text": _esc(a.state)},
            ]})
        plan_body: List[dict] = []
        if actions:
            plan_body.append({"component": "div", "props": {"class": "text-caption mb-2"},
                              "text": f"计划生成于 {_esc(plan.get('created') or '-')}，"
                                      f"现状比对 {_esc(plan.get('refreshed') or '未刷新')}；"
                                      "确认部署后才会实际写入 RouterOS。"})
            plan_body.append({"component": "VTable", "props": {"density": "compact", "hover": True},
                              "content": [
                                  {"component": "thead", "content": [
                                      {"component": "tr", "content": [
                                          {"component": "th", "text": "#"},
                                          {"component": "th", "text": "服务"},
                                          {"component": "th", "text": "分区"},
                                          {"component": "th", "text": "中文含义"},
                                          {"component": "th", "text": "风险"},
                                          {"component": "th", "text": "状态"},
                                      ]}]},
                                  {"component": "tbody", "content": rows},
                              ]})
            if len(actions) > limit:
                plan_body.append({"component": "div", "props": {"class": "text-caption mt-2"},
                                  "text": f"共 {len(actions)} 条，只显示前 {limit} 条"})
        else:
            plan_body.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "title": "还没有预览计划"},
                "content": {"text": "在设置页选好服务保存，回来点「生成 / 刷新预览」。"}})
        content.append({"component": "VCard", "props": {"outlined": True, "class": "mt-3"},
                        "content": [{"component": "VCardTitle", "text": "待确认的计划"},
                                    {"component": "VCardText", "content": plan_body}]})




        return content

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 为相对路径：宿主自动补齐 /api/v1/plugin/<PluginID> 前缀
        return [
            {"path": "/services", "auth": "bear", "endpoint": self.api_services,
             "methods": ["GET"], "summary": "查询已连接服务下拉与当前服务清单"},
            {"path": "/preview", "auth": "bear", "endpoint": self.api_preview,
             "methods": ["POST"], "summary": "生成 / 刷新端口映射预览（比对现状）"},
            {"path": "/plan", "auth": "bear", "endpoint": self.api_plan,
             "methods": ["GET"], "summary": "查询待确认部署计划"},
            {"path": "/apply", "auth": "bear", "endpoint": self.api_apply,
             "methods": ["POST"], "summary": "确认并批量下发（含备份与失败回滚）"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空待确认计划"},
            {"path": "/rollback", "auth": "bear", "endpoint": self.api_rollback,
             "methods": ["POST"], "summary": "回滚最近一次部署新建的条目"},
            {"path": "/probe", "auth": "bear", "endpoint": self.api_probe,
             "methods": ["POST"], "summary": "内网 / 外网连通性测试"},
            {"path": "/backup", "auth": "bear", "endpoint": self.api_backup,
             "methods": ["POST"], "summary": "仅创建 RouterOS 备份与 MP 快照"},
            {"path": "/test", "auth": "bear", "endpoint": self.api_test,
             "methods": ["POST"], "summary": "测试 RouterOS 连接"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status,
             "methods": ["GET"], "summary": "查询插件状态与最近结果"},
            {"path": "/logs", "auth": "bear", "endpoint": self.api_logs,
             "methods": ["GET"], "summary": "查询运行日志"},
        ]

    def api_services(self, _=None) -> Any:
        return Response(success=True, data={
            "items": self._service_items(),
            "services": [s.to_dict() for s in self._services],
            "errors": list(self._service_errors),
        })

    def api_preview(self, _=None) -> Any:
        self._rediscover()
        client, http = self._make_client()
        occupied: List[int] = []
        if client is not None:
            try:
                rows = self._run_coroutine(client.list_section(planner.SEC_NAT, client=http))
                occupied = planner.occupied_ports_from_rows(rows or [], self._prefix)
            except Exception as err:  # noqa: BLE001
                self._log("WARN", f"读取现有 NAT 失败，端口自动分配不参考占用情况：{err}")
        plan = self._make_plan(occupied)
        self.save_data(_KEY_PLAN, plan)
        if plan.get("blocked"):
            return Response(success=False,
                            message="参数有问题，已拒绝生成预览：" + "；".join(
                                str(e) for e in (plan.get("errors") or [])[:3]),
                            data=plan)
        if client is None:
            plan["refreshed"] = now_text()
            plan["offline"] = True
            plan["unreadable"] = list(plan.get("sections") or [])
            self._log("WARN", "没配 RouterOS 地址或网络模块不可用，只生成离线预览")
        else:
            try:
                plan = self._run_coroutine(
                    engine.refresh_plan(client, http, plan, log=self._log))
            except Exception as err:  # noqa: BLE001
                self._log("WARN", f"现状比对失败，已降级为离线预览：{err}")
                plan["refreshed"] = now_text()
                plan["offline"] = True
                plan["unreadable"] = list(plan.get("sections") or [])
        self.save_data(_KEY_PLAN, plan)
        msg = (f"预览生成：{len(plan.get('actions') or [])} 条动作，"
               f"已存在 {int((plan.get('summary') or {}).get('exists') or 0)} 条将跳过")
        if plan.get("offline"):
            msg += "（离线预览，未比对现状）"
        return Response(success=True, message=msg, data=plan)

    def api_plan(self, _=None) -> Any:
        return Response(success=True, data=self._plan())

    def api_clear(self, _=None) -> Any:
        self.save_data(_KEY_PLAN, {})
        self._log("INFO", "待确认计划已清空")
        return Response(success=True, message="待确认计划已清空")

    def api_apply(self, _=None) -> Any:
        plan = self._plan()
        if not plan:
            return Response(success=False, message="没有计划：先点「生成 / 刷新预览」")
        if plan.get("blocked"):
            return Response(success=False,
                            message="参数有问题，已拒绝部署：" + "；".join(
                                str(e) for e in (plan.get("errors") or [])[:3]))
        if not plan.get("actions"):
            return Response(success=False, message="没有可部署的动作：请到设置页选服务并保存")
        if not plan.get("refreshed"):
            return Response(success=False,
                            message="预览还没刷新：先点「生成 / 刷新预览」，核对后再确认部署")
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="没配 RouterOS 地址或网络模块不可用")
        try:
            report = self._run_coroutine(engine.deploy_plan(
                client, http, plan, self._options(),
                log=self._log, snapshot_saver=self._save_snapshot))
        except Exception as err:  # noqa: BLE001
            self._log("ERROR", f"部署异常：{err}")
            return Response(success=False, message=f"部署异常：{err}")

        self.save_data(_KEY_LAST_DEPLOY, report)
        self.save_data(_KEY_ROLLBACK, report.get("created") or [])
        if report.get("ok") and not self._dry_run:
            self.save_data(_KEY_PLAN, {})
        self._notify_deploy(report, "RouterOS 外网访问部署结果")
        return Response(success=bool(report.get("ok")),
                        message=str(report.get("message") or ""), data=report)

    def api_rollback(self, _=None) -> Any:
        created = list(self.get_data(_KEY_ROLLBACK) or [])
        if not created:
            return Response(success=False, message="没有可回滚的条目（上次部署可能已自动回滚）")
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="没配 RouterOS 地址或网络模块不可用")
        try:
            rb = self._run_coroutine(
                engine.rollback_entries(client, http, created, log=self._log))
        except Exception as err:  # noqa: BLE001
            return Response(success=False, message=f"回滚异常：{err}")
        if rb.get("fail_count") == 0:
            self.save_data(_KEY_ROLLBACK, [])
        self._send_message("RouterOS 外网访问回滚", str(rb.get("message") or ""))
        return Response(success=not rb.get("fail_count"),
                        message=str(rb.get("message") or ""), data=rb)

    def api_probe(self, _=None) -> Any:
        plan = self._plan() or self._make_plan()
        targets = probe.build_probe_targets(plan)
        if not targets:
            return Response(success=False, message="没有可测的服务：先在设置页选服务并保存")
        if AsyncRequestUtils is None:
            return Response(success=False, message="网络模块不可用，无法测试")
        try:
            http = AsyncRequestUtils(verify=self._verify)
            results = self._run_coroutine(
                probe.run_probes(http, targets, self._probe_timeout))
        except Exception as err:  # noqa: BLE001
            self._log("ERROR", f"连通性测试异常：{err}")
            return Response(success=False, message=f"连通性测试异常：{err}")
        summary = probe.summarize(results)
        summary["notes"] = probe.probe_notes(plan)
        self.save_data(_KEY_PROBE, summary)
        self._log("INFO" if summary.get("ok") else "WARN", str(summary.get("message")))
        return Response(success=bool(summary.get("ok")),
                        message=str(summary.get("message") or ""), data=summary)

    def api_backup(self, _=None) -> Any:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="没配 RouterOS 地址或网络模块不可用")
        ok, msg = self._run_coroutine(client.check(client=http))
        if not ok:
            return Response(success=False, message=msg)
        bname = "mp-b1-manual-" + time.strftime("%Y%m%d-%H%M%S")
        bok, bmsg = self._run_coroutine(client.save_backup(bname, client=http))
        snap = self._run_coroutine(client.export_snapshot(
            list(planner.SECTIONS.keys()), client=http))
        path = self._save_snapshot(snap)
        self._log("INFO" if bok else "WARN", f"【手动备份】{bmsg}；快照 {path or '未生成'}")
        return Response(success=bok, message=f"{bmsg}；MP 快照 {path or '未生成'}",
                        data={"backup": {"ok": bok, "message": bmsg},
                              "snapshot": path, "count": snap.get("count")})

    def api_test(self, _=None) -> Any:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="没配 RouterOS 地址或网络模块不可用")
        ok, msg = self._run_coroutine(client.check(client=http))
        self.save_data(_KEY_CONN, {"ok": ok, "msg": msg, "ts": time.time()})
        self._log("INFO" if ok else "WARN", f"连接测试：{msg}")
        return Response(success=ok, message=msg)

    def api_logs(self, _=None) -> Any:
        return Response(success=True, data={"logs": list(self.get_data(_KEY_LOGS) or [])[:100]})

    def api_status(self, _=None) -> Any:
        return Response(success=True, data={
            "plugin": PLUGIN_ID,
            "router": self._address, "port": self._port, "ssl": self._use_ssl,
            "username": self._username,
            "dry_run": self._dry_run, "allow_public": self._allow_public,
            "auto_backup": self._auto_backup, "auto_rollback": self._auto_rollback,
            "services": [s.to_dict() for s in self._services],
            "service_errors": list(self._service_errors),
            "prefix": self._prefix, "plan": self._plan(),
            "last_deploy": dict(self.get_data(_KEY_LAST_DEPLOY) or {}),
            "probe": dict(self.get_data(_KEY_PROBE) or {}),
            "rollback_entries": list(self.get_data(_KEY_ROLLBACK) or []),
            "snapshots": self._list_snapshots(),
            "conn": dict(self.get_data(_KEY_CONN) or {}),
            "logs": list(self.get_data(_KEY_LOGS) or [])[:30],
        })

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {"cmd": "/b1_preview", "event": EventType.PluginAction,
             "desc": "生成 RouterOS 端口映射预览", "category": "MoviePilot"},
            {"cmd": "/b1_apply", "event": EventType.PluginAction,
             "desc": "确认执行 RouterOS 端口映射部署", "category": "MoviePilot"},
            {"cmd": "/b1_probe", "event": EventType.PluginAction,
             "desc": "测试外网访问连通性", "category": "MoviePilot"},
            {"cmd": "/b1_rollback", "event": EventType.PluginAction,
             "desc": "回滚最近一次端口映射部署", "category": "MoviePilot"},
            {"cmd": "/b1_status", "event": EventType.PluginAction,
             "desc": "查看端口映射计划与最近结果", "category": "MoviePilot"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data or {}
        cmd = str(data.get("cmd") or "").lower()
        handlers = {
            "/b1_preview": self.api_preview,
            "/b1_apply": self.api_apply,
            "/b1_probe": self.api_probe,
            "/b1_rollback": self.api_rollback,
            "/b1_status": self.api_status,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return
        result = handler()
        text = f"{getattr(result, 'message', '') or ''}"
        if cmd.startswith("/b1_status"):
            plan = self._plan()
            text = (f"服务 {len(plan.get('services') or [])} 个，"
                    f"待确认 {len(plan.get('actions') or [])} 条动作；"
                    f"最近部署：{(self.get_data(_KEY_LAST_DEPLOY) or {}).get('message') or '无'}")
        try:
            self.post_message(title="RouterOS 外网访问", text=_esc(text),
                              channel=data.get("channel"), userid=data.get("userid"),
                              **({"mtype": _PLUGIN_MTYPE} if _PLUGIN_MTYPE is not None else {}))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}：远程命令回复失败 {err}")
