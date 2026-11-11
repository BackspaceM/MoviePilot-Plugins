"""RouterOS 统一工作台（B5）—— MoviePilot V3 插件主类。

一次配置连接，五个页签「概览 / 防火墙 / Queue / 备份 / 分流」共用实时流量与
拓扑卡片。非侵入的只读聚合层：不改任何其它插件，通过 A1 连接中心拿连接参数，
只 GET RouterOS REST 现状与本地插件状态，绝不往路由器写配置。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import html as _html
import time
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

# 详情页带参按钮：前端 PageRender 的 POST 用 api.post(api_path, params) 发 JSON body，
# 必须用 Body(default=None) 声明才能被 FastAPI 注入；裸 =None 会被当 query 参数，
# 导致 payload 恒为 None，页签切换永远返回「页签不对」。
try:
    from fastapi import Body
    _BODY_DEFAULT = Body(default=None)
except Exception:  # noqa: BLE001
    _BODY_DEFAULT = None

from . import aggregator, plugininfo, roslink, sections
from .clients import RouterOSConfigClient, build_config_client

PLUGIN_ID = "RouterOSWorkbench"
_LOG_PREFIX = "ROS工作台"
_MAX_LOGS = 200
_DEFAULT_CRON = "*/10 * * * *"
_ONCE_JOB_ID = "b5_once"

_KEY_SNAPSHOT = "b5_snapshot"
_KEY_LOGS = "b5_logs"
_KEY_CONN = "b5_conn"
_KEY_TAB = "b5_active_tab"

_NOTIFY_LABELS = {"issue": "只在出问题时提醒", "all": "每次刷新都提醒", "none": "不提醒"}

_SHORTCUTS = {
    "RouterOSConnectionCenter": "设置页选存好的路由器",
    "RosFirewallManager": "防火墙页看规则",
    "BandwidthQueueScheduler": "带宽调度页看队列",
    "RouterOSConfigGuard": "配置备份页看备份",
    "CFRouterOS": "CF 优选页看 IP",
    "RouterOSScenarioTemplates": "场景模板页部署",
    "RouterOSRemoteAccessWizard": "外网访问页配映射",
    "RouterOSSiteRouting": "站点分流页看规则",
}


def _esc(value: Any) -> str:
    """动态文本转义：防 XSS，并避免 [xxx] 被前端当 Markdown 参考式链接渲染。"""
    return (_html.escape(str(value if value is not None else ""), quote=False)
            .replace("[", "［").replace("]", "］"))


def _shortcut(pid: Any) -> str:
    return _SHORTCUTS.get(str(pid or ""), "在插件列表里打开")


class RouterOSWorkbench(_PluginBase):
    # 类级元数据一律用字符串字面量：官方版本检查以 AST 提取，常量引用会被判为「未声明」
    plugin_name = "ROS 工作台"
    plugin_desc = ("一次配置连接，五个页签一起看 RouterOS 现状：概览、防火墙、Queue、备份、分流。"
                   "只读聚合，不改其它插件，也不往路由器写配置；顺带列出本地 RouterOS 插件的启用情况。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/routerosworkbench.png"
    plugin_version = "1.0.3"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "routerosworkbench_"
    plugin_color = "#1976D2"
    system_version = ">=3.0.0"
    plugin_order = 55

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cfg: Dict[str, Any] = {}
        self._enabled = True
        self._onlyonce = False
        self._ros_connection = ""
        self._address = ""
        self._port = 443
        self._use_ssl = True
        self._verify = False
        self._username = ""
        self._password = ""
        self._timeout = 10
        self._cron = _DEFAULT_CRON
        self._display_limit = 40
        self._notify_mode = "issue"
        self._active_tab = "overview"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._cfg = cfg
        self._enabled = bool(cfg.get("enabled", True))
        self._onlyonce = bool(cfg.get("onlyonce"))
        self._ros_connection = str(cfg.get("ros_connection") or "").strip()

        overrides, note = roslink.connection_overrides(self._ros_connection, "std")
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

        cron = str(cfg.get("cron") or _DEFAULT_CRON).strip()
        ok, normalized = aggregator.validate_cron(cron)
        self._cron = normalized if ok else _DEFAULT_CRON
        try:
            self._display_limit = max(5, min(200, int(cfg.get("display_limit") or 40)))
        except (TypeError, ValueError):
            self._display_limit = 40
        self._notify_mode = str(cfg.get("notify_mode") or "issue").strip()
        tab = str(cfg.get("active_tab") or self.get_data(_KEY_TAB) or "overview").strip()
        self._active_tab = tab if tab in sections.TAB_KEYS else "overview"

        if self._onlyonce:
            cfg["onlyonce"] = False
            self._onlyonce = False
            try:
                self.update_config(cfg)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"{_LOG_PREFIX}｜复位立即刷新开关失败 {err}")
            if self._enabled:
                self.refresh_once("立即刷新")
            else:
                self._log("WARN", "插件没启用，已跳过立即刷新")
        self._log("INFO", f"配置已载入｜目标 {self._address or '未填'}"
                          f"｜页签 {sections.tab_label(self._active_tab)}｜定时 {self._cron}")

    def get_state(self) -> bool:
        return bool(self._enabled)

    def stop_service(self) -> None:
        """停服时用 getattr 容错清掉 once job，宿主没有调度器就静默跳过。"""
        try:
            from app.sdk import scheduler as scheduler_sdk  # type: ignore
        except Exception:  # noqa: BLE001
            scheduler_sdk = None  # type: ignore
        remover = getattr(scheduler_sdk, "remove_plugin_once_job", None) if scheduler_sdk else None
        if not callable(remover):
            remover = getattr(self, "remove_plugin_once_job", None)
        if callable(remover):
            try:
                remover(self.__class__.__name__, _ONCE_JOB_ID)
            except Exception:  # noqa: BLE001
                pass
        return None

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        ok, cron = aggregator.validate_cron(self._cron)
        if not ok:
            return []
        try:
            from apscheduler.triggers.cron import CronTrigger  # type: ignore
            trigger = CronTrigger.from_crontab(cron)
        except Exception as err:  # noqa: BLE001
            self._log("WARN", f"定时刷新没注册上，本机缺 apscheduler｜{err}")
            return []
        # 注意：MP scheduler.py 用 add_job(self.start, service["trigger"], ...,
        # **service["kwargs"], kwargs={"job_id": ...}) 注册服务：
        # service["kwargs"] 是展开给 APScheduler.add_job() 的「调度参数」，不是传给插件
        # func 的业务参数。曾写成 {"trigger": "定时刷新"}，展开后与第 2 个位置参数
        # trigger 撞名 → BaseScheduler.add_job() got multiple values for argument 'trigger'。
        # 插件 func 由 MP 的 start() 无参调用，触发来源请放在 func 的默认值里。
        return [{
            "id": f"{PLUGIN_ID}_refresh",
            "name": "RouterOS 统一工作台定时刷新",
            "trigger": trigger,
            "func": self.refresh_once,
            "kwargs": {},
        }]

    # ------------------------------------------------------------------
    # 客户端 / 协程
    # ------------------------------------------------------------------

    def _secrets(self) -> List[str]:
        return [self._password] if self._password else []

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
        )
        return client, http

    @staticmethod
    async def _fetch_sections(client: Any, http: Any) -> List[Tuple[str, Any]]:
        async def one(section: str) -> Tuple[str, Any]:
            try:
                rows = await client.list_section(section, client=http)
                return section, rows
            except Exception:  # noqa: BLE001
                return section, None
        return await asyncio.gather(*(one(s) for s in sections.SECTIONS))

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
            logs.insert(0, {"time": aggregator.now_text(), "level": level, "message": text})
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
            logger.warning(f"{_LOG_PREFIX}｜通知发送失败 {err}")

    def _notify(self, snapshot: Dict[str, Any], failed: List[str], trigger: str) -> None:
        mode = self._notify_mode
        if mode == "none":
            return
        if mode == "issue" and not failed:
            return
        text = (f"刷新时间 {snapshot.get('created')}｜分区 {len(snapshot.get('counts') or {})} 个"
                f"｜读不到 {len(failed)} 个｜接口 {len(snapshot.get('interfaces') or [])} 个"
                f"｜触发 {trigger}")
        if failed:
            text += "｜读不到：" + "、".join(sections.label_of(s) for s in failed[:4])
        self._send_message("ROS 工作台", text)

    def _snapshot(self) -> Dict[str, Any]:
        data = self.get_data(_KEY_SNAPSHOT)
        return dict(data) if isinstance(data, dict) else {}

    def _active(self) -> str:
        saved = str(self.get_data(_KEY_TAB) or "").strip()
        tab = saved or self._active_tab or "overview"
        return tab if tab in sections.TAB_KEYS else "overview"

    def refresh_once(self, trigger: str = "定时刷新") -> Any:
        """只读刷新：并发 GET 全部分区，失败分区记 warning 不中断，生成快照存盘。"""
        try:
            client, http = self._make_client()
            raw: Dict[str, List[dict]] = {}
            failed: List[str] = []
            if client is None:
                failed = list(sections.SECTIONS.keys())
                self._log("WARN", "没配 RouterOS 地址或网络模块不可用，只刷新本地插件状态")
            else:
                pairs = self._run_coroutine(self._fetch_sections(client, http))
                for section, rows in pairs:
                    if rows is None:
                        failed.append(section)
                    else:
                        raw[section] = rows

            states, source = plugininfo.load_plugin_states()
            previous = self.get_data(_KEY_SNAPSHOT)
            prev: Optional[dict] = None
            if isinstance(previous, dict) and previous:
                prev = dict(previous)
                try:
                    elapsed = time.time() - float(prev.get("ts") or 0)
                    prev["sample_seconds"] = elapsed if elapsed > 0 else 1.0
                except Exception:  # noqa: BLE001
                    prev["sample_seconds"] = 1.0

            snapshot = aggregator.build_snapshot(raw, states, previous=prev,
                                                 display_limit=self._display_limit)
            snapshot["source"] = source
            snapshot["trigger"] = str(trigger or "")
            for section in failed:
                snapshot["warnings"].append(
                    f"{sections.label_of(section)}（{section}）这次没读到，已跳过")
            self.save_data(_KEY_SNAPSHOT, snapshot)
            self._notify(snapshot, failed, trigger)
            self._log("INFO", f"刷新完成｜分区 {len(raw)} 个｜读不到 {len(failed)} 个"
                              f"｜插件 {len(states)} 个｜触发 {trigger}")
            return snapshot
        except Exception as err:  # noqa: BLE001
            self._log("ERROR", f"刷新失败｜{type(err).__name__} {err}")
            return None

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

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        conn_items = roslink.connection_items()
        tab_items = [{"title": label, "value": key} for key, label in sections.TABS]
        notify_items = [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()]
        content: List[dict] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                              "class": "mb-4"},
             "text": "地址账号填一次，五个页签就能一起看路由器现状。"
                                 "这里只读，不会改路由器上的东西。"},
            {"component": "VRow", "content": [
                self._col(3, [self._sw("enabled", "启用工作台", "关掉就不刷新，也不注册定时")]),
                self._col(3, [self._sw("onlyonce", "立即刷新一次", "保存后读一遍，自动复位")]),
                self._col(3, [self._sel("active_tab", "默认页签", tab_items,
                                        "详情页打开时先看哪个")]),
                self._col(3, [self._sel("notify_mode", "通知方式", notify_items)]),
            ]},
            {"component": "VRow", "content": [
                self._col(4, [self._sel("ros_connection", "已保存的路由器（连接中心）",
                                        conn_items or [{"title": "没找到连接中心配置", "value": ""}],
                                        "选中后自动带入连接参数，下面留空就行；不选就用手填的")]),
                self._col(4, [self._fld("router_address", "RouterOS 地址", "", "192.168.88.1")]),
                self._col(2, [self._fld("router_port", "端口", "", "443", "number")]),
                self._col(2, [self._sw("router_ssl", "使用 HTTPS", "自签证书通常也能连")]),
            ]},
            {"component": "VRow", "content": [
                self._col(4, [self._fld("router_username", "API 用户名", "单独建个最小权限的账号", "mp-api")]),
                self._col(4, [self._fld("router_password", "API 密码", "明文存在插件配置里")]),
                self._col(2, [self._fld("timeout", "超时（秒）", "", "10", "number")]),
                self._col(2, [self._sw("verify", "校验证书", "自签证书请关闭")]),
            ]},
            {"component": "VRow", "content": [
                self._col(6, [self._fld("cron", "定时刷新", "5 位 cron", "*/10 * * * *")]),
                self._col(3, [self._fld("display_limit", "每张表最多显示行数", "", "40", "number")]),
                self._col(3, [{"component": "div", "props": {
                    "class": "text-caption text-medium-emphasis align-self-center"},
                    "text": "速率要刷新两次才有，头一次只看到累计流量。"}]),
            ]},
            {"component": "VAlert", "props": {"type": "secondary", "variant": "tonal",
                                              "class": "mt-3"},
             "text": "页签由服务端记着，点一下就切。备份页签列的是路由器上的文件，"
                                 "本地配置备份插件装没装也一起看。"},
        ]
        defaults: Dict[str, Any] = {
            "enabled": True, "onlyonce": False,
            "ros_connection": "", "router_address": "", "router_port": 443,
            "router_ssl": True, "router_username": "", "router_password": "",
            "verify": False, "timeout": 10,
            "cron": _DEFAULT_CRON, "display_limit": 40,
            "notify_mode": "issue", "active_tab": "overview",
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

    def _tab_button(self, key: str, label: str, active: bool) -> dict:
        return {"component": "VBtn",
                "props": {"variant": "flat" if active else "tonal", "size": "small",
                          "class": "ma-1", "color": "primary" if active else "secondary"},
                "text": label,
                "events": {"click": {"api": f"plugin/{PLUGIN_ID}/tab",
                                     "method": "post", "params": {"name": key},
                                     "confirm": False}}}

    def _table_node(self, table: Dict[str, Any], limit: int) -> List[dict]:
        columns = [str(c) for c in (table.get("columns") or [])]
        rows = (table.get("rows") or [])[:limit]
        body_rows: List[dict] = []
        for row in rows:
            cells = [{"component": "td", "props": {"class": "text-caption"},
                      "text": _esc(cell)}
                     for cell in (row if isinstance(row, (list, tuple)) else [row])]
            body_rows.append({"component": "tr", "content": cells})
        nodes: List[dict] = []
        if table.get("title"):
            nodes.append({"component": "div", "props": {
                "class": "text-subtitle-2 font-weight-bold mt-3 mb-1"},
                "text": _esc(table["title"])})
        if body_rows:
            nodes.append({"component": "VTable", "props": {"density": "compact", "hover": True},
                          "content": [
                              {"component": "thead", "content": [
                                  {"component": "tr", "content": [
                                      {"component": "th", "text": _esc(c)} for c in columns]}]},
                              {"component": "tbody", "content": body_rows},
                          ]})
        else:
            nodes.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "my-2"},
                "text": "这里暂时没有数据"})
        if table.get("note"):
            nodes.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis mt-1"},
                "text": _esc(table["note"])})
        return nodes

    def get_page(self) -> List[dict]:
        snapshot = self._snapshot()
        active = self._active()
        tabs = snapshot.get("tabs") or {}
        tab = dict(tabs.get(active) or {}) if isinstance(tabs, dict) else {}
        conn = dict(self.get_data(_KEY_CONN) or {})
        warnings = list(snapshot.get("warnings") or [])

        content: List[dict] = [
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "text": "操作"},
                {"component": "VCardText", "content": [
                    self._action_button("一键刷新", "refresh", "primary", "mdi-refresh"),
                    self._action_button("测试连接", "test", "info", "mdi-router-wireless"),
                    self._action_button("清空快照", "clear", "warning",
                                        "mdi-delete-outline", confirm=True),
                    self._action_button("仅刷新插件状态", "plugins", "secondary",
                                        "mdi-puzzle-outline"),
                    self._action_button("刷新状态", "status", "info",
                                        "mdi-information-outline", method="get"),
                ]},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": _esc(f"目标 {self._address or '未填'}"
                                  f"｜最近测试 {conn.get('msg') or '还没测'}")},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "text", "class": "mb-3"}, "content": [
                {"component": "VCardText", "content": [
                    self._tab_button(key, label, key == active)
                    for key, label in sections.TABS]},
            ]},
        ]

        cards = [dict(c) for c in (tab.get("cards") or []) if isinstance(c, dict)][:4]
        if cards:
            stat_row = {"component": "VRow", "content": [
                self._stat_card(str(c.get("title") or ""), str(c.get("value") or ""),
                                str(c.get("sub") or ""), str(c.get("color") or "primary"))
                for c in cards]}
        else:
            stat_row = {"component": "VRow", "content": [
                self._stat_card("还没刷新", "点「一键刷新」", "读一次路由器现状", "warning")]}
        content.append(stat_row)

        if warnings:
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mt-3"},
                "text": "；".join(_esc(w) for w in warnings[:6])})

        table_nodes: List[dict] = []
        for t in (tab.get("tables") or []):
            if isinstance(t, dict):
                table_nodes.extend(self._table_node(t, self._display_limit))
        if not table_nodes:
            table_nodes.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-3"},
                "text": "还没刷新，点上面「一键刷新」读一次路由器现状。"})
        content.append({"component": "VCard", "props": {"outlined": True, "class": "mt-3"},
                        "content": [
                            {"component": "VCardTitle", "text": _esc(sections.tab_label(active))},
                            {"component": "VCardText", "content": table_nodes},
                        ]})

        highlights = [str(h) for h in (tab.get("highlights") or [])]
        if highlights:
            content.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-3"},
                "text": "；".join(_esc(h) for h in highlights[:8])})
        notes = [str(n) for n in (tab.get("notes") or [])]
        if notes:
            content.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis mt-2"},
                "text": "；".join(_esc(n) for n in notes[:5])})

        plugins = [dict(p) for p in (snapshot.get("plugins") or []) if isinstance(p, dict)]
        box_rows = [[
            p.get("id"), p.get("name"), p.get("version") or "-",
            "启用" if p.get("enabled") else "未启用",
            "有" if p.get("has_update") else "-",
            _shortcut(p.get("id")),
        ] for p in plugins]
        box_nodes = self._table_node({
            "columns": ["ID", "名称", "版本", "启用", "可更新", "快捷入口"],
            "rows": box_rows[:20],
        }, 20) if box_rows else []
        if box_nodes:
            content.append({"component": "VCard", "props": {"outlined": True, "class": "mt-3"},
                            "content": [
                                {"component": "VCardTitle", "text": "插件工具箱"},
                                {"component": "VCardText", "content": box_nodes},
                            ]})

        content.append({"component": "div", "props": {
            "class": "text-caption text-medium-emphasis mt-2"},
            "text": _esc(f"最近刷新 {snapshot.get('created') or '还没刷新'}"
                         f"｜{sections.tab_label(active)}")})
        return content

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 为相对路径：宿主自动补齐 /api/v1/plugin/<PluginID> 前缀
        return [
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "只读刷新聚合并生成快照"},
            {"path": "/tab", "auth": "bear", "endpoint": self.api_tab,
             "methods": ["POST"], "summary": "切换当前页签"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status,
             "methods": ["GET"], "summary": "查询插件状态与最近快照"},
            {"path": "/plugins", "auth": "bear", "endpoint": self.api_plugins,
             "methods": ["POST"], "summary": "仅刷新本地插件状态"},
            {"path": "/test", "auth": "bear", "endpoint": self.api_test,
             "methods": ["POST"], "summary": "测试 RouterOS 连接"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空当前快照"},
            {"path": "/logs", "auth": "bear", "endpoint": self.api_logs,
             "methods": ["GET"], "summary": "查询运行日志"},
        ]

    def api_refresh(self, _=None) -> Any:
        snapshot = self.refresh_once("手动刷新")
        if snapshot is None:
            return Response(success=False, message="刷新失败，看日志")
        return Response(success=True, message=f"已刷新（{snapshot.get('created')}）",
                        data=snapshot)

    def api_tab(self, payload: Optional[dict] = _BODY_DEFAULT) -> Any:
        """切换页签。前端 POST 的 params 是 JSON body，必须用 Body 声明才能注入。"""
        name = ""
        if isinstance(payload, dict):
            name = str(payload.get("name") or payload.get("tab")
                       or payload.get("active_tab") or "")
        elif isinstance(payload, str):
            name = payload
        elif payload is not None:
            name = str(getattr(payload, "name", "") or "")
        name = name.strip().lower()
        if name not in sections.TAB_KEYS:
            return Response(success=False,
                            message=f"页签不对：{_esc(name) or '未收到 name 参数'}")
        self._active_tab = name
        self.save_data(_KEY_TAB, name)
        self._log("INFO", f"切到页签｜{sections.tab_label(name)}")
        return Response(success=True, message=f"已切到 {sections.tab_label(name)}",
                        data={"active_tab": name})

    def api_plugins(self, _=None) -> Any:
        states, source = plugininfo.load_plugin_states()
        snapshot = self._snapshot()
        if snapshot:
            snapshot["plugins"] = states
            snapshot["source"] = source
            self.save_data(_KEY_SNAPSHOT, snapshot)
        self._log("INFO", f"插件状态已刷新｜{len(states)} 个｜{source}")
        return Response(success=True, message=f"插件状态已刷新（{len(states)} 个）",
                        data={"plugins": states, "source": source})

    def api_test(self, _=None) -> Any:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="没配 RouterOS 地址或网络模块不可用")
        ok, msg = self._run_coroutine(client.check(client=http))
        self.save_data(_KEY_CONN, {"ok": ok, "msg": msg, "ts": time.time()})
        self._log("INFO" if ok else "WARN", f"连接测试｜{msg}")
        return Response(success=ok, message=msg)

    def api_clear(self, _=None) -> Any:
        self.save_data(_KEY_SNAPSHOT, {})
        self._log("INFO", "快照已清空")
        return Response(success=True, message="快照已清空")

    def api_logs(self, _=None) -> Any:
        return Response(success=True, data={"logs": list(self.get_data(_KEY_LOGS) or [])[:100]})

    def api_status(self, _=None) -> Any:
        return Response(success=True, data={
            "plugin": PLUGIN_ID,
            "enabled": self._enabled,
            "active_tab": self._active(),
            "router": self._address, "port": self._port, "ssl": self._use_ssl,
            "username": self._username, "cron": self._cron,
            "notify_mode": self._notify_mode, "display_limit": self._display_limit,
            "snapshot": self._snapshot(),
            "conn": dict(self.get_data(_KEY_CONN) or {}),
            "logs": list(self.get_data(_KEY_LOGS) or [])[:30],
        })

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {"cmd": "/b5_refresh", "event": EventType.PluginAction,
             "desc": "刷新 RouterOS 工作台快照", "category": "MoviePilot"},
            {"cmd": "/b5_status", "event": EventType.PluginAction,
             "desc": "查看工作台状态与最近刷新", "category": "MoviePilot"},
            {"cmd": "/b5_plugins", "event": EventType.PluginAction,
             "desc": "仅刷新本地 RouterOS 插件状态", "category": "MoviePilot"},
            {"cmd": "/b5_tab", "event": EventType.PluginAction,
             "desc": "切换工作台页签", "category": "MoviePilot"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data or {}
        cmd = str(data.get("cmd") or "").lower()
        handlers = {
            "/b5_refresh": self.api_refresh,
            "/b5_status": self.api_status,
            "/b5_plugins": self.api_plugins,
            "/b5_tab": self.api_tab,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return
        result = (handler({"name": str(data.get("name") or "overview")})
                  if cmd == "/b5_tab" else handler())
        text = f"{getattr(result, 'message', '') or ''}"
        if cmd == "/b5_status":
            snap = self._snapshot()
            text = (f"工作台｜启用 {'是' if self._enabled else '否'}"
                    f"｜页签 {sections.tab_label(self._active())}"
                    f"｜最近刷新 {(snap.get('created') or '还没刷新')}")
        try:
            self.post_message(title="ROS 工作台", text=_esc(text),
                              channel=data.get("channel"), userid=data.get("userid"),
                              **({"mtype": _PLUGIN_MTYPE} if _PLUGIN_MTYPE is not None else {}))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}｜远程命令回复失败 {err}")
