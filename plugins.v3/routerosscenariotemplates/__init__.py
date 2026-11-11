"""RouterOS 场景模板一键部署（A2）—— MoviePilot V3 插件主类。

把高频网络场景做成模板卡片：PT 友好 QoS、远程观影放行、访客网络隔离、
IPv6 基础开通、WireGuard 回家。选中模板 → 填参数 → 预览「写入哪些分区 +
中文含义 + 风险 + 等价命令」→ 确认 → 批量下发 + 自动备份 + 失败逆序回滚。

安全边界：
- 只新增带 ``MP-A2[<模板>:<序号>]`` 备注标签的条目，绝不修改/删除用户已有配置；
- 回滚只删本次新建条目，不整表覆盖；同标签已存在则跳过（幂等）；
- 高危模板需显式勾选「允许部署高危模板」；必填参数缺失或备份失败时拒绝部署；
- 连接参数可直接从「RouterOS 连接中心」下拉带入，不安装连接中心时手填行为不变。

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

from . import engine, roslink, templates as tplmod
from .clients import RouterOSConfigClient, build_config_client
from .templates import (DEFAULT_PREFIX, RISK_COLORS, RISK_LABELS, SECTIONS,
                        actions_from_plan, md_safe, now_text,
                        template_select_items)

PLUGIN_ID = "RouterOSScenarioTemplates"
_LOG_PREFIX = "ROS场景模板"
_MAX_LOGS = 200
_MAX_SNAPSHOTS = 10

_KEY_PLAN = "a2_plan"
_KEY_LAST_DEPLOY = "a2_last_deploy"
_KEY_ROLLBACK = "a2_rollback_entries"
_KEY_LOGS = "a2_logs"
_KEY_CONN = "a2_conn"
_KEY_SNAPSHOTS = "a2_snapshots"

_NOTIFY_LABELS = {"issue": "仅异常/失败", "all": "每次部署", "none": "关闭"}


def _esc(value: Any) -> str:
    """动态文本转义：防 XSS，并避免 [xxx] 被前端当 Markdown 参考式链接渲染。"""
    return (_html.escape(str(value if value is not None else ""), quote=False)
            .replace("[", "［").replace("]", "］"))


def _cmd_esc(value: Any) -> str:
    """命令预览：保留 RouterOS 内部 *id，其余 Markdown 敏感符号转全角。"""
    return md_safe(_html.escape(str(value if value is not None else ""), quote=False),
                   keep_star=True)


class RouterOSScenarioTemplates(_PluginBase):
    # 类级元数据一律用字符串字面量：官方版本检查以 AST 提取，常量引用会被判为「未声明」
    plugin_name = "RouterOS 场景模板一键部署"
    plugin_desc = ("把 PT 友好 QoS、远程观影放行、访客网络隔离、IPv6、WireGuard 回家等高"
                   "频场景做成模板：勾选→预览规则与中文含义→确认→批量下发，写入前自动"
                   "备份，失败按逆序自动回滚；只新增带 MP-A2 标签的条目，不改用户已有配置。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/routerosscenariotemplates.png"
    plugin_version = "1.0.1"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "routerosscenariotemplates_"
    plugin_color = "#1976D2"
    system_version = ">=3.0.0"
    plugin_order = 51

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
        self._selected: Any = []
        self._params: Dict[str, str] = {}
        self._prefix = DEFAULT_PREFIX
        self._allow_high_risk = False
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

        # 连接中心：选中已保存路由器时带入连接参数；未选中/找不到则保持手填配置不变
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

        self._selected = cfg.get("templates") or []
        self._params = self._collect_params(cfg)
        self._prefix = str(cfg.get("comment_prefix") or DEFAULT_PREFIX).strip() or DEFAULT_PREFIX
        self._allow_high_risk = bool(cfg.get("allow_high_risk"))
        self._dry_run = bool(cfg.get("dry_run"))
        self._auto_backup = bool(cfg.get("auto_backup", True))
        self._require_backup = bool(cfg.get("require_backup", True))
        self._auto_rollback = bool(cfg.get("auto_rollback", True))
        self._notify_mode = str(cfg.get("notify_mode") or "issue")
        try:
            self._display_limit = max(10, min(200, int(cfg.get("display_limit") or 60)))
        except (TypeError, ValueError):
            self._display_limit = 60

        # 保存配置后重建静态计划：用户必须重新点「生成/刷新预览」才能部署
        plan = self._make_plan()
        self.save_data(_KEY_PLAN, plan)
        self._log("INFO", f"配置已载入：模板 {len(plan.get('templates') or [])} 套，"
                          f"待确认动作 {len(plan.get('actions') or [])} 条"
                          + ("；校验未通过" if plan.get("blocked") else ""))

    @staticmethod
    def _collect_params(config: Dict[str, Any]) -> Dict[str, str]:
        """取模板参数；前端未回传的字段回落到模板默认值，避免误判必填缺失。"""
        defaults: Dict[str, str] = {}
        for tpl in tplmod.TEMPLATES:
            for param in tpl.params:
                defaults.setdefault(param.key, param.default)
        out: Dict[str, str] = {}
        for key, default in defaults.items():
            value = config.get(f"param_{key}")
            out[key] = str(value).strip() if value is not None and str(value).strip() else default
        return out

    def get_state(self) -> bool:
        return True  # 纯配置/工具型插件，常驻可用，不注册定时任务

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
        """把分区现状快照写到插件数据目录，并按时间轮转保留最近 10 份。"""
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

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------

    def _make_plan(self) -> Dict[str, Any]:
        return tplmod.render_plan(self._selected, self._params, self._prefix,
                                  self._allow_high_risk)

    def _plan(self) -> Dict[str, Any]:
        data = self.get_data(_KEY_PLAN) or {}
        return dict(data) if isinstance(data, dict) else {}

    def _options(self) -> Dict[str, Any]:
        return {
            "auto_backup": self._auto_backup,
            "require_backup": self._require_backup,
            "auto_rollback": self._auto_rollback,
            "dry_run": self._dry_run,
            "backup_name": "mp-a2-" + time.strftime("%Y%m%d-%H%M%S"),
        }

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

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        items = roslink.connection_items()
        conn_hint = "选中后自动带入连接参数，下面留空就行；不选就用手填的。"
        content: List[dict] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                              "class": "mb-4"},
             "text": "勾选场景、核对每条的中文含义和风险，确认后一次性写入。"
                                 "只新增带 MP-A2 标签的条目；写之前会在 RouterOS 留备份，中途失败自动回滚。"},
            self._section_title("目标路由器"),
            {"component": "VRow", "content": [
                self._col(4, [self._sel("ros_connection", "已保存的路由器（连接中心）",
                                        items or [{"title": "未检测到连接中心配置", "value": ""}],
                                        conn_hint)]),
                self._col(4, [self._fld("router_address", "RouterOS 地址", "手填；填了下拉时以下拉为准",
                                        "192.168.88.1")]),
                self._col(2, [self._fld("router_port", "端口", "", "443", "number")]),
                self._col(2, [self._sw("router_ssl", "使用 HTTPS", "自签证书通常仍可连接")]),
            ]},
            {"component": "VRow", "content": [
                self._col(4, [self._fld("router_username", "API 用户名", "单独建个最小权限的账号", "mp-api")]),
                self._col(4, [self._fld("router_password", "API 密码", "保存在本插件配置中，仅容器内可见")]),
                self._col(2, [self._fld("timeout", "超时（秒）", "", "10", "number")]),
                self._col(2, [self._sw("verify", "校验证书", "自签证书请关闭")]),
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            self._section_title("场景模板"),
            {"component": "VRow", "content": [
                self._col(12, [self._sel("templates", "要部署的场景（可多选）",
                                         template_select_items(),
                                         "高危模板（远程观影 / IPv6 / WireGuard）还需在下面勾选「允许部署高危模板」",
                                         multiple=True)]),
            ]},
        ]

        # 模板卡片说明
        cards: List[dict] = []
        for tpl in tplmod.TEMPLATES:
            sections = sorted({str(SECTIONS.get(s.section, {}).get("label") or s.section)
                               for s in tpl.steps})
            cards.append({"component": "VCard", "props": {
                "variant": "tonal", "class": "mb-2"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "text-subtitle-2"},
                     "text": f"{_esc(tpl.name)} · 风险{_esc(RISK_LABELS.get(tpl.risk_level, '?'))} · {len(tpl.steps)} 条"},
                    {"component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "text-caption mb-1"},
                         "text": _esc(tpl.summary)},
                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                         "text": "会改：" + _esc("、".join(sections))},
                    ]},
                ]})
        content.append({"component": "VRow", "content": [
            self._col(6, cards[:3]), self._col(6, cards[3:]),
        ]})

        # 模板参数（同一参数被多个模板共用时只显示一次）
        content.append(self._section_title("模板参数"))
        seen: set = set()
        for tpl in tplmod.TEMPLATES:
            fields: List[dict] = []
            for param in tpl.params:
                if param.key in seen:
                    continue
                seen.add(param.key)
                also = [t.name for t in tplmod.TEMPLATES
                        if t.id != tpl.id and any(p.key == param.key for p in t.params)]
                hint = param.hint + ("；同时用于：" + "、".join(also) if also else "")
                fields.append(self._fld(f"param_{param.key}", param.label,
                                        hint, param.placeholder,
                                        "number" if param.kind in ("port", "number") else "text"))
            if fields:
                content.append({"component": "div", "props": {
                    "class": "text-caption font-weight-bold mt-2 mb-1"},
                    "text": f"{tpl.name} 参数"})
                rows = [self._col(4, [field]) for field in fields]
                # 每行最多 3 列，避免一行塞太多
                for i in range(0, len(rows), 3):
                    content.append({"component": "VRow", "content": rows[i:i + 3]})

        content.extend([
            {"component": "VDivider", "props": {"class": "my-3"}},
            self._section_title("写入选项"),
            {"component": "VRow", "content": [
                self._col(3, [self._sw("allow_high_risk", "允许部署高危模板",
                                       "远程观影 / IPv6 / WireGuard 会把端口或设备暴露到公网")]),
                self._col(3, [self._sw("dry_run", "试运行（不写入）",
                                       "只走流程不实际改动，用于核对预览")]),
                self._col(3, [self._sw("auto_backup", "部署前自动备份",
                                       "在 RouterOS 执行 /system/backup/save")]),
                self._col(3, [self._sw("require_backup", "备份失败必须中止",
                                       "备份没成功就别往下写")]),
            ]},
            {"component": "VRow", "content": [
                self._col(3, [self._sw("auto_rollback", "失败自动回滚",
                                       "中途失败时逆序删除本次新建条目")]),
                self._col(3, [self._fld("comment_prefix", "管理标签前缀",
                                        "所有新增条目的备注前缀，用于幂等与精确回滚", "MP-A2")]),
                self._col(3, [self._sel("notify_mode", "通知方式",
                                        [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()])]),
                self._col(3, [self._fld("display_limit", "详情页最多显示行数",
                                        "", "60", "number")]),
            ]},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                                              "class": "mt-3"},
             "text": "保存后到详情页点「生成 / 刷新预览」，核对完再点「确认一键部署」。"
                                 "想撤销点「回滚最近一次部署」。"},
            {"component": "VAlert", "props": {"type": "secondary", "variant": "text",
                                              "class": "mt-2"},
             "text": "同一个端口别在「外网访问向导」里再配一次。IPv6、访客隔离有顺序要求，"
                                 "部署后到防火墙管理器看一眼位置。"},
        ])

        defaults: Dict[str, Any] = {
            "ros_connection": "", "router_address": "", "router_port": 443,
            "router_ssl": True, "router_username": "", "router_password": "",
            "timeout": 10, "verify": False,
            "templates": [], "comment_prefix": DEFAULT_PREFIX,
            "allow_high_risk": False, "dry_run": False,
            "auto_backup": True, "require_backup": True, "auto_rollback": True,
            "notify_mode": "issue", "display_limit": 60,
        }
        for key, default in self._collect_params({}).items():
            defaults[f"param_{key}"] = default
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
                    self._action_button("测试连接", "test", "info", "mdi-lan-connect"),
                    self._action_button("仅备份不下发", "backup", "secondary", "mdi-backup-restore"),
                    self._action_button("清空待部署计划", "clear", "warning", "mdi-delete-outline"),
                    self._action_button("回滚最近一次部署", "rollback", "error",
                                        "mdi-undo-variant", confirm=True),
                    self._action_button("刷新状态", "status", "info", "mdi-refresh", method="get"),
                ]},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": ("当前为试运行模式：所有写入都不生效，仅验证流程。"
                              if self._dry_run else
                              "部署前会自动创建 RouterOS 系统备份；失败时逆序回滚本次新建条目。")
                              + (f" 最近一次可回滚条目 {len(rollback_entries)} 条。" if rollback_entries else "")},
                ]},
            ]}
        ]

        if plan.get("errors"):
            content.append({"component": "VAlert", "props": {
                "type": "error", "variant": "tonal", "class": "mt-3",
                "title": "部署前需处理以下问题（详见下方说明）"},
                "text": "；".join(_esc(e) for e in (plan.get("errors") or [])[:8])})
        if plan.get("warnings"):
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "text", "class": "mt-2"},
                "text": "；".join(_esc(e) for e in (plan.get("warnings") or [])[:5])})
        if plan.get("unreadable"):
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "text", "class": "mt-2",
                "title": "有几个分区读不到"},
                "text": "读不到就没法比对是否已存在，确认 API 账号有读权限："
                                    + _esc("、".join(str(s) for s in plan["unreadable"]))})


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
                 "text": _esc(f"{RISK_LABELS.get(a.risk_level, '?')}｜{a.risk or '无特别风险'}")},
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
                                          {"component": "th", "text": "模板"},
                                          {"component": "th", "text": "分区"},
                                          {"component": "th", "text": "中文含义"},
                                          {"component": "th", "text": "风险"},
                                          {"component": "th", "text": "状态"},
                                      ]}]},
                                  {"component": "tbody", "content": rows},
                              ]})
            if len(actions) > limit:
                plan_body.append({"component": "div", "props": {"class": "text-caption mt-2"},
                                  "text": f"共 {len(actions)} 条，仅显示前 {limit} 条"})
        else:
            plan_body.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "title": "还没有预览计划"},
                "text": "在设置页勾选场景、填好参数保存，回来点「生成 / 刷新预览」。"})
        content.append({"component": "VCard", "props": {"variant": "outlined", "class": "mt-3"},
                        "content": [{"component": "VCardTitle", "text": "待确认的计划"},
                                    {"component": "VCardText", "content": plan_body}]})

        # 最近一次部署报告

        # 快照清单


        return content

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 为相对路径：宿主自动补齐 /api/v1/plugin/<PluginID> 前缀
        return [
            {"path": "/preview", "auth": "bear", "endpoint": self.api_preview,
             "methods": ["POST"], "summary": "生成/刷新待确认部署计划并比对现状"},
            {"path": "/plan", "auth": "bear", "endpoint": self.api_plan,
             "methods": ["GET"], "summary": "查询待确认部署计划"},
            {"path": "/apply", "auth": "bear", "endpoint": self.api_apply,
             "methods": ["POST"], "summary": "确认并批量下发（含备份与失败回滚）"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空待确认部署计划"},
            {"path": "/rollback", "auth": "bear", "endpoint": self.api_rollback,
             "methods": ["POST"], "summary": "回滚最近一次部署新建的条目"},
            {"path": "/backup", "auth": "bear", "endpoint": self.api_backup,
             "methods": ["POST"], "summary": "仅创建 RouterOS 备份与 MP 快照"},
            {"path": "/test", "auth": "bear", "endpoint": self.api_test,
             "methods": ["POST"], "summary": "测试 RouterOS 连接"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status,
             "methods": ["GET"], "summary": "查询插件状态与最近部署结果"},
            {"path": "/logs", "auth": "bear", "endpoint": self.api_logs,
             "methods": ["GET"], "summary": "查询运行日志"},
        ]

    def api_preview(self, _=None) -> Any:
        plan = self._make_plan()
        client, http = self._make_client()
        if client is None:
            plan["refreshed"] = now_text()
            plan["offline"] = True
            plan["unreadable"] = list(plan.get("sections") or [])
            self._log("WARN", "未配置 RouterOS 地址或网络模块不可用，仅生成离线预览（未比对现状）")
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
        if plan.get("blocked"):
            msg += "；校验未通过，部署会被拒绝：" + "；".join(str(e) for e in plan["errors"][:2])
        if plan.get("offline"):
            msg += "（离线预览，未比对现状）"
        return Response(success=not plan.get("blocked"), message=msg, data=plan)

    def api_plan(self, _=None) -> Any:
        return Response(success=True, data=self._plan())

    def api_clear(self, _=None) -> Any:
        self.save_data(_KEY_PLAN, {})
        self._log("INFO", "待确认部署计划已清空")
        return Response(success=True, message="待确认部署计划已清空")

    def api_apply(self, _=None) -> Any:
        plan = self._plan()
        if not plan:
            return Response(success=False, message="没有待部署计划：请先点「生成 / 刷新预览」")
        # 校验失败要先于「动作数为 0」判断：被拦截的模板不生成动作，
        # 否则用户只会看到「没有待部署计划」，掩盖真正的参数/高危问题。
        if plan.get("blocked"):
            return Response(success=False,
                            message="计划校验未通过，已拒绝部署：" + "；".join(
                                str(e) for e in (plan.get("errors") or [])[:3]))
        if not plan.get("actions"):
            return Response(success=False, message="没有可部署的动作：请到设置页勾选模板并保存")
        if not plan.get("refreshed"):
            return Response(success=False,
                            message="预览尚未刷新：请先点「生成 / 刷新预览」，核对后再确认部署")
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="未配置 RouterOS 地址或网络模块不可用")
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
            self.save_data(_KEY_PLAN, {})  # 成功后清空，避免误重复部署
        self._notify_deploy(report, "RouterOS 场景模板部署结果")
        return Response(success=bool(report.get("ok")),
                        message=str(report.get("message") or ""), data=report)

    def api_rollback(self, _=None) -> Any:
        created = list(self.get_data(_KEY_ROLLBACK) or [])
        if not created:
            return Response(success=False, message="没有可回滚的条目（上次部署可能已自动回滚或尚未部署）")
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="未配置 RouterOS 地址或网络模块不可用")
        try:
            rb = self._run_coroutine(
                engine.rollback_entries(client, http, created, log=self._log))
        except Exception as err:  # noqa: BLE001
            return Response(success=False, message=f"回滚异常：{err}")
        if rb.get("fail_count") == 0:
            self.save_data(_KEY_ROLLBACK, [])
        self._send_message("RouterOS 场景模板回滚", str(rb.get("message") or ""))
        return Response(success=not rb.get("fail_count"),
                        message=str(rb.get("message") or ""), data=rb)

    def api_backup(self, _=None) -> Any:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="未配置 RouterOS 地址或网络模块不可用")
        ok, msg = self._run_coroutine(client.check(client=http))
        if not ok:
            return Response(success=False, message=msg)
        bname = "mp-a2-manual-" + time.strftime("%Y%m%d-%H%M%S")
        bok, bmsg = self._run_coroutine(client.save_backup(bname, client=http))
        snap = self._run_coroutine(client.export_snapshot(
            list(SECTIONS.keys()), client=http))
        path = self._save_snapshot(snap)
        self._log("INFO" if bok else "WARN", f"【手动备份】{bmsg}；快照 {path or '未生成'}")
        return Response(success=bok, message=f"{bmsg}；MP 快照 {path or '未生成'}",
                        data={"backup": {"ok": bok, "message": bmsg},
                              "snapshot": path, "count": snap.get("count")})

    def api_test(self, _=None) -> Any:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="未配置 RouterOS 地址或网络模块不可用")
        ok, msg = self._run_coroutine(client.check(client=http))
        self.save_data(_KEY_CONN, {"ok": ok, "msg": msg, "ts": time.time()})
        self._log("INFO" if ok else "WARN", f"连接测试：{msg}")
        return Response(success=ok, message=msg)

    def api_logs(self, _=None) -> Any:
        return Response(success=True, data={"logs": list(self.get_data(_KEY_LOGS) or [])[:100]})

    def api_status(self, _=None) -> Any:
        plan = self._plan()
        report = dict(self.get_data(_KEY_LAST_DEPLOY) or {})
        return Response(success=True, data={
            "plugin": PLUGIN_ID,
            "router": self._address, "port": self._port, "ssl": self._use_ssl,
            "username": self._username,
            "dry_run": self._dry_run, "allow_high_risk": self._allow_high_risk,
            "auto_backup": self._auto_backup, "auto_rollback": self._auto_rollback,
            "selected": self._selected, "prefix": self._prefix,
            "plan": plan, "last_deploy": report,
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
            {"cmd": "/a2_preview", "event": EventType.PluginAction,
             "desc": "生成 RouterOS 场景模板部署预览", "category": "MoviePilot"},
            {"cmd": "/a2_apply", "event": EventType.PluginAction,
             "desc": "确认执行 RouterOS 场景模板部署", "category": "MoviePilot"},
            {"cmd": "/a2_rollback", "event": EventType.PluginAction,
             "desc": "回滚最近一次场景模板部署", "category": "MoviePilot"},
            {"cmd": "/a2_status", "event": EventType.PluginAction,
             "desc": "查看场景模板计划与最近部署结果", "category": "MoviePilot"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data or {}
        cmd = str(data.get("cmd") or "").lower()
        handlers = {
            "/a2_preview": self.api_preview,
            "/a2_apply": self.api_apply,
            "/a2_rollback": self.api_rollback,
            "/a2_status": self.api_status,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return
        result = handler()
        text = f"{getattr(result, 'message', '') or ''}"
        if cmd.startswith("/a2_status"):
            plan = self._plan()
            text = (f"模板 {len(plan.get('templates') or [])} 套，"
                    f"待确认 {len(plan.get('actions') or [])} 条动作；"
                    f"最近部署：{(self.get_data(_KEY_LAST_DEPLOY) or {}).get('message') or '无'}")
        try:
            self.post_message(title="RouterOS 场景模板", text=_esc(text),
                              channel=data.get("channel"), userid=data.get("userid"),
                              **({"mtype": _PLUGIN_MTYPE} if _PLUGIN_MTYPE is not None else {}))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}：远程命令回复失败 {err}")
