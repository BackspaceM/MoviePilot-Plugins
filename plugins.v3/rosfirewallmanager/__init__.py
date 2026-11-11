"""RouterOS 防火墙可视化管理器 —— MoviePilot V3 插件主类。

把 RouterOS 防火墙从"记命令"变成"点选"：
- 读取 filter/nat/mangle/raw 四张表全部规则，按执行顺序可视化；
- 自动检测影子规则（放行被兜底 drop 遮挡）、被架空的拒绝规则、重复规则、缺失兜底；
- 对单条规则做 启用/禁用/上移/下移/置顶/置底/删除，或按表单新增（默认插到兜底 drop 之前，
  避免变成死规则）；操作前可一键在 RouterOS 留备份，快照存 MP 便于回滚；
- 定时扫描隐患并推送 MP 通知。

定位是"管理器"而非"期望状态对齐器"：绝不批量删除用户真实规则，所有破坏性操作
都作用于用户在详情页明确点选的单条规则，并带二次确认。

详情页每个按钮都绑定真实 API endpoint（含带参的启停/移动/删除/新增），
不存在"点了没反应"的按钮；所有动态文本经 md_safe 转义，避免被当 Markdown 渲染成空药丸。
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
    from schemas import Response
except Exception:  # noqa: BLE001
    class Response:  # type: ignore
        def __init__(self, success: bool = True, message: str = "ok", data: Any = None):
            self.success = success
            self.message = message
            self.data = data

try:
    from fastapi import Body
    _BODY_DEFAULT = Body(default=None)
except Exception:  # noqa: BLE001
    _BODY_DEFAULT = None  # type: ignore

try:
    _PLUGIN_MTYPE = MessageType.Plugin
except Exception:  # noqa: BLE001
    _PLUGIN_MTYPE = None

from . import importer, roslink
from .clients import RouterOSFirewallClient, build_firewall_client
from .core import (
    ACTION_OPTIONS,
    CONNSTATE_OPTIONS,
    PLACE_BOTTOM,
    PROTOCOL_OPTIONS,
    SEV_HIGH,
    TABLES,
    TABLE_LABELS,
    Finding,
    Rule,
    build_add_payload,
    choose_insert_place,
    compute_move_place,
    detect_findings,
    format_findings_text,
    format_log_line,
    format_preview_text,
    format_rules_text,
    md_safe,
    md_safe_command,
    mask_secrets,
    now_text,
    preview_command,
    summarize_rules,
    ts_text,
    validate_cron,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PLUGIN_NAME = "RosFirewallManager"
PLUGIN_ICON = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/rosfirewallmanager.png"
PLUGIN_DESCRIPTION = "RouterOS 防火墙可视化管理器：表格化查看 filter/nat/mangle/raw 全部规则并检测影子规则·重复·缺失兜底，逐条启停/上下移/删除或按表单新增（自动插到兜底 drop 前），操作前可留备份，定时扫描隐患并推送 MP 通知。"
PLUGIN_AUTHORS = "BackspaceM"
AUTHOR_URL = "https://github.com/BackspaceM"
PLUGIN_COLOR = "#E67E22"
SYSTEM_VERSION = ">=3.0.0"

_LOG_PREFIX = "ROS防火墙管理"
_ONCE_JOB_ID = "rfm_once"
_MAX_LOGS = 200
_DEFAULT_CRON = "0 9 * * 1"          # 每周一 09:00 扫描一次
_DEFAULT_TABLES = ["filter", "nat"]
_DEFAULT_LIMIT = 10
_DEFAULT_IMPORT_LIMIT = 12

# plugindata keys
_KEY_RULES = "rfm_rules"
_KEY_FINDINGS = "rfm_findings"
_KEY_LOGS = "rfm_logs"
_KEY_LAST_SCAN = "rfm_last_scan"
_KEY_LAST_OP = "rfm_last_op"
_KEY_SNAPSHOT = "rfm_snapshot"
_KEY_CONN = "rfm_conn"
_KEY_IMPORT = "rfm_import"

# 「上传文件」按钮：MP 前端会把 props.onclick 的字符串透传为 DOM onclick 执行
# （官方 certmanager 用同样的 window.MoviePilotAPI.post 方式）。
_UPLOAD_JS = (
    "function(){var i=document.createElement('input');i.type='file';"
    "i.accept='.rsc,.txt,.cfg,.conf,text/plain';"
    "i.onchange=function(e){var f=e.target.files&&e.target.files[0];if(!f)return;"
    "var r=new FileReader();r.onload=function(){window.MoviePilotAPI.post("
    "'plugin/RosFirewallManager/import_text',{text:String(r.result||''),name:f.name})"
    ".then(function(x){alert(x&&x.message?x.message:'已解析，请刷新页面')})"
    ".catch(function(err){console.error(err);alert('上传解析失败，请查看日志')})};"
    "r.readAsText(f)};i.click()}"
)

_NOTIFY_LABELS = {
    "issue": "仅隐患/异常（推荐）",
    "all": "每次扫描",
    "none": "关闭",
}
_PLACE_LABELS = {
    "before_drop": "兜底 drop 之前（推荐）",
    "top": "置顶",
    "bottom": "末尾",
    "before_id": "指定规则之前",
}

# 新增规则的额外要点（写进表单字段说明）
_NEW_RULE_FIELDS = (
    "new_rule_table", "new_rule_chain", "new_rule_action", "new_rule_protocol",
    "new_rule_src_address", "new_rule_dst_address", "new_rule_src_port", "new_rule_dst_port",
    "new_rule_in_interface", "new_rule_out_interface", "new_rule_connection_state",
    "new_rule_comment", "new_rule_place", "new_rule_before_id", "new_rule_disabled",
    "new_rule_log", "new_rule_log_prefix",
)


def now_text_local() -> str:
    return now_text()


class RosFirewallManager(_PluginBase):
    # 类级元数据一律用字符串字面量：官方 check_plugin_versions.py 以 AST 提取，
    # 常量引用会被判为"未声明"（与 plugin_version 同理）。
    plugin_name = "RosFirewallManager"
    plugin_desc = ("RouterOS 防火墙可视化管理器：表格化查看 filter/nat/mangle/raw 全部规则并检测影子规则·重复·缺失兜底，"
                   "逐条启停/上下移/删除或按表单新增（自动插到兜底 drop 前），操作前可留备份，定时扫描隐患并推送 MP 通知。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/rosfirewallmanager.png"
    plugin_version = "1.2.4"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "rosfirewallmanager_"
    plugin_color = "#E67E22"
    system_version = ">=3.0.0"
    plugin_order = 58

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
        # 默认配置（保证 init_plugin 前调用 get_form/get_page 也不崩）
        self._address = ""
        self._port = 443
        self._use_ssl = True
        self._username = ""
        self._password = ""
        self._timeout = 10
        self._tables: List[str] = list(_DEFAULT_TABLES)
        self._scan_enabled = False
        self._scan_cron = _DEFAULT_CRON
        self._notify_mode = "issue"
        self._dry_run = False
        self._before_backup = True
        self._comment_prefix = ""
        self._display_limit = _DEFAULT_LIMIT
        self._import_table = "filter"
        self._import_place = "before_drop"
        self._import_disabled = False
        self._new_rule: Dict[str, Any] = {}

    # -- 配置装载 ----------------------------------------------------------

    def init_plugin(self, config: dict) -> None:
        config = dict(config or {})
        self.stop_service()
        self._enabled = bool(config.get("enabled"))

        # 连接中心：选中已保存的路由器就自动带入连接参数；不选/找不到仍用手填
        overrides, note = roslink.connection_overrides(
            config.get("ros_connection"), "std")
        conn = dict(config)
        conn.update(overrides)
        if note:
            (logger.info if overrides else logger.warning)(
                f"{_LOG_PREFIX}｜连接中心：{note}")

        self._address = str(conn.get("router_address") or "").strip()
        try:
            self._port = int(conn.get("router_port") or 443)
        except (TypeError, ValueError):
            self._port = 443
        self._use_ssl = bool(conn.get("router_ssl", True))
        self._username = str(conn.get("router_username") or "").strip()
        self._password = str(conn.get("router_password") or "")
        try:
            self._timeout = max(3, min(60, int(config.get("timeout") or 10)))
        except (TypeError, ValueError):
            self._timeout = 10

        tables = config.get("manage_tables")
        if isinstance(tables, str):
            tables = [t.strip().lower() for t in tables.replace("，", ",").split(",")]
        parsed_tables = [t for t in (tables or []) if t in TABLES]
        self._tables = parsed_tables or list(_DEFAULT_TABLES)

        self._scan_enabled = bool(config.get("scan_enabled"))
        self._scan_cron = str(config.get("scan_cron") or _DEFAULT_CRON).strip() or _DEFAULT_CRON
        if not validate_cron(self._scan_cron)[0]:
            self._scan_cron = _DEFAULT_CRON
        self._notify_mode = str(config.get("notify_mode") or "issue").strip() or "issue"
        self._dry_run = bool(config.get("dry_run"))
        self._before_backup = bool(config.get("before_change_backup", True))
        self._comment_prefix = str(config.get("comment_prefix") or "").strip()
        self._display_limit = max(3, min(80, int(config.get("display_limit") or _DEFAULT_LIMIT)))

        # 批量导入选项
        self._import_table = str(config.get("import_table") or "filter").strip().lower()
        if self._import_table not in TABLES:
            self._import_table = "filter"
        self._import_disabled = bool(config.get("import_disabled"))
        self._import_place = str(config.get("import_place") or "before_drop").strip()
        if self._import_place not in _PLACE_LABELS:
            self._import_place = "before_drop"

        # 配置页粘贴的文本在保存时自动生成中文解析预览（上传文件走 API）
        preview = importer.build_import_preview(config)
        if preview.get("source") or preview.get("errors"):
            preview["created"] = now_text()
            preview["place"] = self._import_place
            self.save_data(_KEY_IMPORT, preview)
            self._log("INFO", f"导入解析｜{preview['source']}｜{len(preview['rules'])} 条")

        # 兼容旧版「立即添加一条规则」开关；新版配置页不再暴露这些字段
        self._new_rule = {key: config.get(key) for key in _NEW_RULE_FIELDS}
        self._new_rule["table"] = str(config.get("new_rule_table") or "filter").strip().lower()
        if config.get("add_once"):
            config["add_once"] = False
            try:
                self.update_config(config)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"{_LOG_PREFIX}：复位新增开关失败 {err}")
            self._perform_add_from_config()

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
        if not self.get_state() or not self._scan_enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._scan_cron)
        except (ValueError, TypeError):
            trigger = CronTrigger.from_crontab(_DEFAULT_CRON)
        return [{
            "id": PLUGIN_NAME,
            "name": "RouterOS 防火墙隐患扫描",
            "trigger": trigger,
            "func": self.start_scan,
            # kwargs 是展开给 APScheduler.add_job 的调度参数，绝不能含 trigger；
            # 插件 func 由 MP 无参调用，触发来源/线程偏好放在 func 默认值里。
            "kwargs": {},
        }]

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """精简设置页：选路由器 → 粘贴/上传规则 → 回主界面看中文解释。

        刻意不再提供「逐条填 chain/action/端口」的表单：已知规则效果的话，
        直接用 WinBox 导入更快；本插件的价值是把规则翻译成人话。
        """
        defaults: Dict[str, Any] = {
            "enabled": self._enabled,
            "ros_connection": "",
            "router_address": self._address,
            "router_port": self._port,
            "router_ssl": self._use_ssl,
            "router_username": self._username,
            "router_password": self._password,
            "import_rules": "",
            "import_table": getattr(self, "_import_table", "filter"),
            "import_place": getattr(self, "_import_place", "before_drop"),
            "import_disabled": getattr(self, "_import_disabled", False),
            "scan_enabled": self._scan_enabled,
            "scan_cron": self._scan_cron,
            "notify_mode": self._notify_mode,
            "dry_run": self._dry_run,
            "before_change_backup": self._before_backup,
            "manage_tables": list(self._tables),
            "display_limit": self._display_limit,
        }

        def fld(model: str, label: str, props: Optional[dict] = None) -> dict:
            p = {"model": model, "label": label}
            if props:
                p.update(props)
            return {"component": "VTextField", "props": p}

        def sel(model: str, label: str, items: List[dict], props: Optional[dict] = None) -> dict:
            p = {"model": model, "label": label, "items": items}
            if props:
                p.update(props)
            return {"component": "VSelect", "props": p}

        def sw(model: str, label: str, hint: str) -> dict:
            return {"component": "VSwitch",
                    "props": {"model": model, "label": label, "hint": hint}}

        def col(md: int, nodes: List[dict]) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": nodes}

        conn_items = roslink.connection_items() or [
            {"title": "未找到连接中心配置，用下面手填即可", "value": ""}]
        table_items = [{"title": TABLE_LABELS[t], "value": t} for t in TABLES]
        place_items = [{"title": v, "value": k} for k, v in _PLACE_LABELS.items()]

        content: List[dict] = [
            # 1) 连接：下拉优先，手填兜底
            {"component": "VRow", "content": [
                col(6, [sel("ros_connection", "已保存的路由器（连接中心）", conn_items,
                           {"hint": "从连接中心选一台即可自动带入连接参数；不选就用下面手填的",
                            "persistent-hint": True})]),
                col(3, [fld("router_address", "RouterOS 地址",
                            {"placeholder": "192.168.88.1 或 https://ros.local"})]),
                col(3, [fld("router_port", "端口", {"type": "number"})]),
            ]},
            {"component": "VRow", "content": [
                col(4, [fld("router_username", "API 用户名",
                            {"hint": "建议单独建只读/最小权限账号", "persistent-hint": True})]),
                col(4, [fld("router_password", "API 密码", {"type": "password"})]),
                col(4, [sw("router_ssl", "使用 HTTPS", "关闭则走 HTTP，密码会明文传输")]),
            ]},
            # 2) 批量导入（核心）
            {"component": "VRow", "props": {"class": "mt-2"}, "content": [
                col(12, [{"component": "VTextarea", "props": {
                    "model": "import_rules", "label": "粘贴 RouterOS 防火墙规则 / 导出脚本",
                    "rows": 6, "auto-grow": True, "persistent-hint": True,
                    "hint": "支持 .rsc 导出、/ip firewall filter print 输出，或直接 "
                            "chain=input action=accept ... 规则；保存后自动翻成中文解释。"
                            "也可在主界面点「上传 .rsc 文件」。",
                    "placeholder": "/ip firewall filter\n"
                                   "add chain=input action=accept protocol=tcp dst-port=8291\n"
                                   "add chain=input action=drop in-interface-list=WAN"}}]),
            ]},
            {"component": "VRow", "content": [
                col(4, [sel("import_table", "未标注表时的默认表", table_items)]),
                col(4, [sel("import_place", "导入位置", place_items,
                            {"hint": "默认插到兜底 drop 之前，避免新规则成为死规则",
                             "persistent-hint": True})]),
                col(4, [sw("import_disabled", "导入后先禁用",
                           "先看懂解释并核对，再到详情页逐条启用，更安全")]),
            ]},
            # 3) 定时与通知
            {"component": "VRow", "props": {"class": "mt-2"}, "content": [
                col(3, [sw("enabled", "启用插件", "关闭后不定时扫描，详情页仍可手动操作")]),
                col(3, [sw("scan_enabled", "定时扫描隐患", "按右侧 cron 周期扫描并推送")]),
                col(3, [fld("scan_cron", "扫描周期(cron)",
                            {"placeholder": "0 9 * * 1  每周一 09:00"})]),
                col(3, [sel("notify_mode", "通知方式",
                            [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()])]),
            ]},
            # 4) 安全与显示
            {"component": "VRow", "content": [
                col(3, [sw("dry_run", "试运行", "只预览命令，不真正改动 RouterOS")]),
                col(3, [sw("before_change_backup", "改动前留备份",
                           "批量导入前在 RouterOS 执行 /system/backup/save")]),
                col(3, [sel("manage_tables", "管理范围（可多选）", table_items,
                            {"multiple": True})]),
                col(3, [fld("display_limit", "详情页显示条数", {"type": "number",
                            "hint": "导入解释与逐条操作都只显示前 N 条",
                            "persistent-hint": True})]),
            ]},
            {"component": "div", "props": {
                "class": "text-caption text-medium-emphasis mt-2"},
             "text": "保存后回主界面：每条规则都会说明「会做什么、影响谁、有什么风险」；"
                     "看懂并核对后再点「确认批量导入」。"},
        ]
        return content, defaults

    # ------------------------------------------------------------------
    # 执行控制
    # ------------------------------------------------------------------

    @staticmethod
    def _run_coroutine(coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=180)

    def _make_client(self) -> Tuple[Optional[RouterOSFirewallClient], Any]:
        if AsyncRequestUtils is None or not self._address:
            return None, None
        http = AsyncRequestUtils(verify=self._use_ssl is False)
        client = build_firewall_client(
            self._address, self._username, self._password, http,
            port=self._port, use_ssl=self._use_ssl, timeout=self._timeout,
            dry_run=self._dry_run,
        )
        return client, http

    def start_scan(self, trigger: str = "定时扫描", prefer_thread: bool = True) -> bool:
        with self._state_lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()

        def _task() -> None:
            try:
                result = self._run_coroutine(self._async_scan(trigger))
                self.save_data(_KEY_LAST_SCAN, time.time())
                self._notify_scan(result, trigger)
            except Exception as err:  # noqa: BLE001
                self._log("ERROR", f"扫描任务异常：{err}\n{traceback.format_exc()[-800:]}")
                self._send_message("防火墙扫描异常", f"触发：{trigger}\n错误：{err}")
            finally:
                with self._state_lock:
                    self._running = False

        if prefer_thread:
            self._thread = threading.Thread(target=_task, daemon=True, name="RFM-Scan")
            self._thread.start()
        else:
            _task()
        return True

    async def _async_scan(self, trigger: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "trigger": trigger, "ok": False, "rules": [], "findings": [],
            "summary": {}, "conn": "", "high": 0, "medium": 0, "message": "",
        }
        if AsyncRequestUtils is None or not self._address:
            result["message"] = "未配置 RouterOS 地址或网络模块不可用"
            self._log("WARN", result["message"])
            return result

        client, http = self._make_client()
        ok, msg = await client.check(client=http)
        self._log("INFO" if ok else "WARN", f"【{trigger}】连接：{msg}")
        result["conn"] = msg
        try:
            self.save_data(_KEY_CONN, {"ok": ok, "msg": msg, "ts": time.time()})
        except Exception:  # noqa: BLE001
            pass
        if not ok:
            result["message"] = msg
            return result

        rules = await client.list_all(client=http, tables=self._tables)
        findings = detect_findings(rules)
        summary = summarize_rules(rules)
        result.update({
            "ok": True, "rules": [r.to_dict() for r in rules],
            "findings": [f.to_dict() for f in findings], "summary": summary,
            "high": sum(1 for f in findings if f.severity == SEV_HIGH),
            "medium": sum(1 for f in findings if f.severity == "medium"),
            "message": f"读取 {len(rules)} 条规则，发现 {len(findings)} 项隐患"
                       f"（高危 {result['high']}）",
        })
        self.save_data(_KEY_RULES, result["rules"])
        self.save_data(_KEY_FINDINGS, result["findings"])
        for f in findings:
            if f.severity == SEV_HIGH:
                self._log("WARN", f"【{trigger}】高危：{f.title}")
        self._log("INFO", f"【{trigger}】{result['message']}")
        return result

    def _new_rule_fields(self) -> Dict[str, Any]:
        """把配置页的 new_rule_* 长名映射为 build_add_payload 期望的短名。"""
        src = dict(self._new_rule or {})
        mapping = {
            "chain": "new_rule_chain", "action": "new_rule_action",
            "protocol": "new_rule_protocol", "src_address": "new_rule_src_address",
            "dst_address": "new_rule_dst_address", "src_port": "new_rule_src_port",
            "dst_port": "new_rule_dst_port", "in_interface": "new_rule_in_interface",
            "out_interface": "new_rule_out_interface",
            "connection_state": "new_rule_connection_state", "comment": "new_rule_comment",
            "disabled": "new_rule_disabled", "log": "new_rule_log",
            "log_prefix": "new_rule_log_prefix",
        }
        out: Dict[str, Any] = {
            "table": str(src.get("table") or "filter").strip().lower(),
            "place": str(src.get("new_rule_place") or "before_drop"),
            "before_id": str(src.get("new_rule_before_id") or ""),
        }
        for short, long in mapping.items():
            out[short] = src.get(long)
        return out

    def _apply_comment_prefix(self, payload: Dict[str, Any]) -> None:
        if not self._comment_prefix:
            return
        prefix = self._comment_prefix.rstrip("-:") + "-"
        comment = str(payload.get("comment") or "")
        if not comment.startswith(prefix):
            payload["comment"] = prefix + comment if comment else prefix.rstrip("-")

    def _perform_add_from_config(self) -> None:
        """由配置页「立即添加这条规则」开关触发。"""
        if not self._enabled:
            self._log("WARN", "插件未启用，已忽略「立即添加这条规则」")
            return
        fields = self._new_rule_fields()
        table = str(fields.get("table") or "filter").strip().lower()
        ok, payload, err = build_add_payload(table, fields)
        if not ok:
            self._log("WARN", f"新增规则参数不完整：{err}")
            self._send_message("防火墙新增失败", f"参数不完整：{err}")
            return
        self._apply_comment_prefix(payload)
        self._run_add(table, payload, str(fields.get("place") or "before_drop"),
                      str(fields.get("before_id") or ""), trigger="配置页新增")

    def _run_add(self, table: str, payload: Dict[str, Any], place_mode: str,
                 before_id: str, trigger: str) -> Dict[str, Any]:
        client, http = self._make_client()
        if client is None:
            msg = "网络模块不可用或未配置 RouterOS 地址"
            self._log("WARN", f"【{trigger}】{msg}")
            return {"ok": False, "message": msg}

        # 读取现状以确定插入点（置顶/置底/插到兜底 drop 前）
        rules = self._run_coroutine(client.list_all(client=http, tables=[table]))
        place, note = choose_insert_place(rules, str(payload.get("chain") or ""),
                                          place_mode, before_id)
        preview = preview_command(table, payload, place)

        if self._before_backup and not self._dry_run:
            bok, bmsg = self._run_coroutine(
                client.save_routeros_backup(f"rfm-{time.strftime('%Y%m%d-%H%M%S')}", client=http))
            self._log("INFO" if bok else "WARN", f"【{trigger}】改动前备份：{bmsg}")

        ok, msg = self._run_coroutine(client.add_rule(table, payload, place, client=http))
        self._log("INFO" if ok else "WARN", f"【{trigger}】{msg}")
        result = {"ok": ok, "message": msg, "table": table, "place": place,
                  "note": note, "preview": preview, "dry_run": self._dry_run}
        self.save_data(_KEY_LAST_OP, result)
        if ok:
            # 更新本地规则缓存
            fresh = self._run_coroutine(client.list_all(client=http, tables=self._tables))
            self.save_data(_KEY_RULES, [r.to_dict() for r in fresh])
        self._send_message("防火墙规则已新增" if ok else "防火墙新增失败",
                           f"{msg}\n位置：{note}\n命令：{preview}")
        return result

    def _run_single_op(self, op: str, table: str, rule_id: str,
                       mode: str = "", disabled: bool = False,
                       trigger: str = "手动") -> Dict[str, Any]:
        client, http = self._make_client()
        if client is None:
            msg = "网络模块不可用或未配置 RouterOS 地址"
            self._log("WARN", f"【{trigger}】{msg}")
            return {"ok": False, "message": msg}
        if table not in TABLES:
            return {"ok": False, "message": f"未知表 {table}"}

        rules = self._run_coroutine(client.list_all(client=http, tables=[table]))
        if op == "toggle":
            ok, msg = self._run_coroutine(
                client.set_disabled(table, rule_id, bool(disabled), client=http))
        elif op == "move":
            ok, msg = self._run_coroutine(
                client.move_rule(table, rules, rule_id, mode, client=http))
        elif op == "remove":
            if self._before_backup and not self._dry_run:
                bok, bmsg = self._run_coroutine(
                    client.save_routeros_backup(f"rfm-{time.strftime('%Y%m%d-%H%M%S')}", client=http))
                self._log("INFO" if bok else "WARN", f"【{trigger}】改动前备份：{bmsg}")
            ok, msg = self._run_coroutine(client.remove_rule(table, rule_id, client=http))
        else:
            return {"ok": False, "message": f"未知操作 {op}"}

        self._log("INFO" if ok else "WARN", f"【{trigger}】{table}｜{rule_id}：{msg}")
        result = {"ok": ok, "message": msg, "op": op, "table": table, "rule_id": rule_id}
        self.save_data(_KEY_LAST_OP, result)
        if ok:
            fresh = self._run_coroutine(client.list_all(client=http, tables=self._tables))
            self.save_data(_KEY_RULES, [r.to_dict() for r in fresh])
            self.save_data(_KEY_FINDINGS,
                           [f.to_dict() for f in detect_findings(fresh)])
        return result

    # ------------------------------------------------------------------
    # 数据 / 日志 / 通知
    # ------------------------------------------------------------------

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

    def _notify_scan(self, result: Dict[str, Any], trigger: str) -> None:
        mode = self._notify_mode
        if mode == "none" or not result.get("ok"):
            if mode != "none" and not result.get("ok"):
                self._send_message("防火墙扫描失败", result.get("message", "未知错误"))
            return
        high, medium = int(result.get("high", 0)), int(result.get("medium", 0))
        if mode == "issue" and not (high or medium):
            return
        findings = [Finding.from_dict(f) for f in (result.get("findings") or [])]
        lines = [f"触发：{trigger}", result.get("message", ""),
                 f"规则统计：{len(result.get('rules') or [])} 条"]
        if high or medium:
            lines.append(format_findings_text([f for f in findings
                                               if f.severity in (SEV_HIGH, "medium")], max_rows=8))
        title = "⚠ 防火墙高危隐患" if high else ("防火墙隐患提醒" if medium else "防火墙扫描完成")
        self._send_message(title, "\n".join(lines))

    def current_status(self) -> Dict[str, Any]:
        rules_raw = list(self.get_data(_KEY_RULES) or [])
        rules = [Rule.from_dict(d) for d in rules_raw]
        findings = list(self.get_data(_KEY_FINDINGS) or [])
        conn = dict(self.get_data(_KEY_CONN) or {})
        return {
            "enabled": self.get_state(), "running": bool(getattr(self, "_running", False)),
            "router": self._address, "tables": self._tables, "dry_run": self._dry_run,
            "scan_enabled": self._scan_enabled, "scan_cron": self._scan_cron,
            "notify_mode": self._notify_mode, "display_limit": self._display_limit,
            "rules": rules, "findings": findings, "conn": conn,
            "summary": summarize_rules(rules),
            "last_scan": ts_text(self.get_data(_KEY_LAST_SCAN)),
            "last_op": dict(self.get_data(_KEY_LAST_OP) or {}),
        }

    # ------------------------------------------------------------------
    # 详情页（每个按钮都绑定真实 API，且不使用 disabled）
    # ------------------------------------------------------------------

    def _action_button(self, label: str, api: str, *, params: Optional[dict] = None,
                       confirm: bool = False, variant: str = "tonal",
                       color: Optional[str] = None, size: str = "small") -> dict:
        props: Dict[str, Any] = {"variant": variant, "size": size}
        if color:
            props["color"] = color
        event: Dict[str, Any] = {
            "api": f"plugin/{PLUGIN_NAME}/{api}", "method": "post",
            "params": params or {}, "confirm": confirm,
        }
        return {"component": "VBtn", "props": props, "text": label,
                "events": {"click": event}}

    def _rule_op_row(self, r: Rule) -> dict:
        """为单条规则生成一行：简述 + 启停/上移/下移/置顶/置底/删除按钮。"""
        label = md_safe(f"{r.id}｜{r.chain} {r.action}｜{r.comment or r.matcher_summary(46)}")
        info = md_safe(("禁用 " if r.disabled else "") + ("动态 " if r.dynamic else "")
                       + r.matcher_summary(40))
        buttons = [
            self._action_button("启用" if r.disabled else "禁用", "toggle",
                                params={"table": r.table, "id": r.id, "disabled": not r.disabled},
                                size="x-small"),
            self._action_button("上移", "move",
                                params={"table": r.table, "id": r.id, "mode": "up"}, size="x-small"),
            self._action_button("下移", "move",
                                params={"table": r.table, "id": r.id, "mode": "down"}, size="x-small"),
            self._action_button("置顶", "move",
                                params={"table": r.table, "id": r.id, "mode": "top"}, size="x-small"),
            self._action_button("置底", "move",
                                params={"table": r.table, "id": r.id, "mode": "bottom"},
                                confirm=True, size="x-small"),
            self._action_button("删除", "remove",
                                params={"table": r.table, "id": r.id},
                                confirm=True, color="error", size="x-small"),
        ]
        return {"component": "VRow", "props": {"class": "my-1", "align": "center"}, "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                {"component": "div", "text": label,
                 "props": {"style": "font-size:12px"}},
                {"component": "div", "text": info,
                 "props": {"style": "font-size:11px;opacity:.7"}},
            ]},
            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                {"component": "div", "props": {"style": "display:flex;gap:4px;flex-wrap:wrap"},
                 "content": buttons},
            ]},
        ]}

    def get_page(self) -> List[dict]:
        """主界面：状态行 + 导入规则的中文解释 + 两行操作按钮 + 现有规则逐条操作。"""
        status = self.current_status()
        rules: List[Rule] = status["rules"]
        findings: List[Finding] = [Finding.from_dict(f) for f in status["findings"]]
        conn = status["conn"]
        limit = int(status["display_limit"])

        def kv(label: str, value: Any) -> dict:
            return {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                {"component": "div", "text": md_safe(f"{label}：{value}"),
                 "props": {"style": "font-size:13px"}},
            ]}

        def line(value: str, style: str = "white-space:pre-wrap;font-size:12px;opacity:.8") -> dict:
            return {"component": "div", "text": md_safe(value),
                    "props": {"style": style}}

        high = sum(1 for f in findings if f.severity == SEV_HIGH)
        preview = dict(self.get_data(_KEY_IMPORT) or {})
        rule_count = len(preview.get("rules") or [])
        if preview:
            brief = importer.format_import_brief(preview, limit)
        else:
            brief = ("还没有导入内容：在设置页粘贴 RouterOS 规则，"
                     "或点下方「上传 .rsc 文件」，导入后每条规则都会翻成中文解释。")

        import_buttons = [
            {"component": "VBtn", "props": {
                "variant": "flat", "size": "small", "color": "primary",
                "class": "ma-1", "prepend-icon": "mdi-file-upload-outline",
                "onclick": _UPLOAD_JS},
             "text": "上传 .rsc / 规则文件"},
            self._action_button("确认批量导入", "import_apply",
                                confirm=True, variant="flat", color="warning"),
            self._action_button("清空导入", "import_clear", confirm=True),
        ]
        manage_buttons = [
            self._action_button("立即扫描隐患", "scan", variant="flat", color="primary"),
            self._action_button("刷新规则", "refresh"),
            self._action_button("测试连接", "test"),
            self._action_button("RouterOS 留备份", "router_backup"),
            self._action_button("停止任务", "stop", confirm=True),
        ]

        conn_text = "正常" if conn.get("ok") else ("异常" if conn else "未测试")
        by_table = {t: sum(1 for r in rules if r.table == t) for t in status["tables"]}
        table_text = "、".join(f"{TABLE_LABELS.get(t, t)} {n} 条" for t, n in by_table.items())
        nodes: List[dict] = [
            {"component": "VRow", "content": [
                kv("插件状态", "已启用" if status["enabled"] else "未启用"),
                kv("连接", conn_text),
                kv("现存规则", status["summary"].get("total", 0)),
                kv("待导入 / 隐患", f"{rule_count} 条 / {len(findings)} 项（高危 {high}）"),
            ]},
            line(table_text or "尚未读取规则"),
            {"component": "div", "props": {
                "class": "mt-3",
                "style": "white-space:pre-line;font-size:12px;line-height:1.65"},
             "text": brief},
            {"component": "VRow", "props": {"class": "mt-2"}, "content": [
                {"component": "VCol", "content": import_buttons}]},
            {"component": "VRow", "content": [
                {"component": "VCol", "content": manage_buttons}]},
        ]

        shown = rules[:limit]
        if shown:
            nodes.append(line(f"现有规则逐条操作（前 {len(shown)} 条；删除/置底需确认）"))
            nodes.extend(self._rule_op_row(r) for r in shown)
            if len(rules) > limit:
                nodes.append(line(f"另有 {len(rules) - limit} 条未显示，可调大设置页「详情页显示条数」"))
        return [{"component": "VCard", "props": {"variant": "tonal"}, "content": [
            {"component": "VCardText", "content": nodes}]}]

    # ------------------------------------------------------------------
    # API（一一对应详情页按钮 + 配置页新增）
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 用相对路径：MP get_plugin_apis 会自动补 /{pid}，
        # 写成 /plugin/{NAME}/xxx 会重复前缀导致按钮 Not Found。
        return [
            {"path": "/scan", "auth": "bear", "endpoint": self.api_scan, "methods": ["POST"], "summary": "立即扫描隐患"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh, "methods": ["POST"], "summary": "刷新四表规则"},
            {"path": "/test", "auth": "bear", "endpoint": self.api_test, "methods": ["POST"], "summary": "测试 RouterOS 连接"},
            {"path": "/toggle", "auth": "bear", "endpoint": self.api_toggle, "methods": ["POST"], "summary": "启用/禁用单条规则"},
            {"path": "/move", "auth": "bear", "endpoint": self.api_move, "methods": ["POST"], "summary": "移动单条规则顺序"},
            {"path": "/remove", "auth": "bear", "endpoint": self.api_remove, "methods": ["POST"], "summary": "删除单条规则"},
            {"path": "/add_preview", "auth": "bear", "endpoint": self.api_add_preview, "methods": ["POST"], "summary": "预览新增规则命令"},
            {"path": "/add", "auth": "bear", "endpoint": self.api_add, "methods": ["POST"], "summary": "按表单新增规则"},
            {"path": "/backup", "auth": "bear", "endpoint": self.api_backup, "methods": ["POST"], "summary": "导出规则快照到 MP"},
            {"path": "/router_backup", "auth": "bear", "endpoint": self.api_router_backup, "methods": ["POST"], "summary": "在 RouterOS 创建配置备份"},
            {"path": "/stop", "auth": "bear", "endpoint": self.api_stop, "methods": ["POST"], "summary": "停止当前任务"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status, "methods": ["GET"], "summary": "获取当前状态"},
            {"path": "/import_text", "auth": "bear", "endpoint": self.api_import_text,
             "methods": ["POST"], "summary": "上传文件或文本，解析并生成中文解释预览"},
            {"path": "/import_apply", "auth": "bear", "endpoint": self.api_import_apply,
             "methods": ["POST"], "summary": "确认后批量写入预览中的规则"},
            {"path": "/import_clear", "auth": "bear", "endpoint": self.api_import_clear,
             "methods": ["POST"], "summary": "清空导入预览"},
        ]

    def api_scan(self, _=None) -> Response:
        started = self.start_scan(trigger="API 立即扫描", prefer_thread=True)
        return Response(success=True,
                        message="已触发扫描" if started else "已有扫描任务在运行",
                        data={"started": started})

    def api_refresh(self, _=None) -> Response:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置地址")
        rules = self._run_coroutine(client.list_all(client=http, tables=self._tables))
        self.save_data(_KEY_RULES, [r.to_dict() for r in rules])
        self.save_data(_KEY_FINDINGS, [f.to_dict() for f in detect_findings(rules)])
        return Response(success=True, message=f"已刷新 {len(rules)} 条规则",
                        data={"count": len(rules)})

    def api_test(self, _=None) -> Response:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置地址")
        ok, msg = self._run_coroutine(client.check(client=http))
        self.save_data(_KEY_CONN, {"ok": ok, "msg": msg, "ts": time.time()})
        return Response(success=bool(ok), message=mask_secrets(msg, self._secrets()))

    def api_toggle(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        table, rid, disabled = _parse_op_payload(payload)
        if not table or not rid:
            return Response(success=False, message="缺少 table 或 id")
        res = self._run_single_op("toggle", table, rid, disabled=disabled, trigger="API 启停")
        return Response(success=bool(res.get("ok")), message=res.get("message", ""), data=res)

    def api_move(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        table, rid, _ = _parse_op_payload(payload)
        mode = str((payload or {}).get("mode") or "").strip().lower() if isinstance(payload, dict) else ""
        if not table or not rid or mode not in ("up", "down", "top", "bottom"):
            return Response(success=False, message="缺少 table/id 或 mode 非法")
        res = self._run_single_op("move", table, rid, mode=mode, trigger="API 排序")
        return Response(success=bool(res.get("ok")), message=res.get("message", ""), data=res)

    def api_remove(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        table, rid, _ = _parse_op_payload(payload)
        if not table or not rid:
            return Response(success=False, message="缺少 table 或 id")
        res = self._run_single_op("remove", table, rid, trigger="API 删除")
        return Response(success=bool(res.get("ok")), message=res.get("message", ""), data=res)

    def api_add_preview(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        fields = dict(payload) if isinstance(payload, dict) else self._new_rule_fields()
        table = str(fields.get("table") or "filter").strip().lower()
        ok, body, err = build_add_payload(table, fields)
        if not ok:
            return Response(success=False, message=f"参数不完整：{err}")
        place_mode = str(fields.get("place") or "before_drop")
        before_id = str(fields.get("before_id") or "")
        client, http = self._make_client()
        rules: List[Rule] = []
        if client is not None:
            rules = self._run_coroutine(client.list_all(client=http, tables=[table]))
        place, note = choose_insert_place(rules, str(body.get("chain") or ""), place_mode, before_id)
        text = format_preview_text(body, table, place, note)
        return Response(success=True, message="命令预览", data={
            "table": table, "payload": body, "place": place, "note": note, "text": text,
        })

    def api_add(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        fields = dict(payload) if isinstance(payload, dict) else self._new_rule_fields()
        table = str(fields.get("table") or "filter").strip().lower()
        ok, body, err = build_add_payload(table, fields)
        if not ok:
            return Response(success=False, message=f"参数不完整：{err}")
        self._apply_comment_prefix(body)
        place_mode = str(fields.get("place") or "before_drop")
        before_id = str(fields.get("before_id") or "")
        res = self._run_add(table, body, place_mode, before_id, trigger="API 新增")
        return Response(success=bool(res.get("ok")), message=res.get("message", ""), data=res)

    def api_backup(self, _=None) -> Response:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置地址")
        snap = self._run_coroutine(client.export_snapshot(client=http, tables=self._tables))
        self.save_data(_KEY_SNAPSHOT, snap)
        return Response(success=True, message=f"已导出快照：{snap.get('count', 0)} 条规则",
                        data={"count": snap.get("count", 0), "ts": snap.get("ts")})

    def api_router_backup(self, _=None) -> Response:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置地址")
        ok, msg = self._run_coroutine(
            client.save_routeros_backup(f"rfm-{time.strftime('%Y%m%d-%H%M%S')}", client=http))
        return Response(success=bool(ok), message=mask_secrets(msg, self._secrets()))

    def api_stop(self, _=None) -> Response:
        self.stop_service()
        return Response(success=True, message="已停止")

    def api_import_text(self, payload: Optional[dict] = _BODY_DEFAULT) -> Any:
        """上传文件 / API 文本导入：解析后保存预览，返回条数供前端提示。"""
        text = ""
        name = ""
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("content")
                       or payload.get("import_rules") or "")
            name = str(payload.get("name") or "")
        elif isinstance(payload, str):
            text = payload
        text = importer.extract_upload(text)
        if not text.strip():
            return Response(success=False, message="文件内容为空，未生成预览")
        cfg = {"import_table": self._import_table,
               "import_disabled": self._import_disabled,
               "import_place": self._import_place}
        source = "上传文件"
        if name:
            source += f"（{name}）"
        preview = importer.build_import_preview(cfg, text_override=text,
                                                source_override=source)
        preview["created"] = now_text()
        preview["place"] = self._import_place
        self.save_data(_KEY_IMPORT, preview)
        self._log("INFO", f"导入解析｜{source}｜{len(preview['rules'])} 条｜"
                          f"提示 {len(preview['errors'])} 条")
        return Response(success=True,
                        message=(f"已解析 {len(preview['rules'])} 条规则，"
                                 f"回主界面查看中文解释（刷新页面后可见）"),
                        data={"count": len(preview["rules"]),
                              "errors": preview["errors"]})

    def api_import_apply(self) -> Any:
        """确认后把预览里的规则批量写入 RouterOS。"""
        preview = dict(self.get_data(_KEY_IMPORT) or {})
        items = [importer.ImportRule.from_dict(r)
                 for r in (preview.get("rules") or [])]
        if not items:
            return Response(success=False, message="没有可导入的规则，先粘贴内容或上传文件")
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置 RouterOS 地址")
        mode = str(preview.get("place") or self._import_place)

        if self._before_backup and not self._dry_run:
            bok, bmsg = self._run_coroutine(client.save_routeros_backup(
                f"rfm-import-{time.strftime('%Y%m%d-%H%M%S')}", client=http))
            self._log("INFO" if bok else "WARN", f"【批量导入】改动前备份：{bmsg}")

        all_rules = self._run_coroutine(
            client.list_all(client=http, tables=self._tables))
        ok_count = 0
        fail_count = 0
        details: List[str] = []
        for item in items:
            place = item.place_before
            if not place:
                place, _ = choose_insert_place(all_rules, item.chain, mode, "")
            body = dict(item.payload)
            if self._comment_prefix:
                prefix = self._comment_prefix.rstrip("-:") + "-"
                comment = str(body.get("comment") or "")
                if not comment.startswith(prefix):
                    body["comment"] = prefix + comment if comment else prefix.rstrip("-")
            ok, msg = self._run_coroutine(
                client.add_rule(item.table, body, place, client=http))
            if ok:
                ok_count += 1
            else:
                fail_count += 1
            details.append(f"{item.index}. {'成功' if ok else '失败'}｜{msg}")
            self._log("INFO" if ok else "WARN",
                      f"【批量导入】{item.index}/{len(items)}｜{msg}")

        fresh = self._run_coroutine(client.list_all(client=http, tables=self._tables))
        self.save_data(_KEY_RULES, [r.to_dict() for r in fresh])
        result = {"ok": fail_count == 0, "total": len(items),
                  "success": ok_count, "failed": fail_count,
                  "dry_run": self._dry_run, "details": details}
        self.save_data(_KEY_LAST_OP, result)
        message = (f"批量导入完成｜成功 {ok_count}｜失败 {fail_count}"
                   + ("（试运行，未写入）" if self._dry_run else ""))
        self._send_message("防火墙批量导入结果", message + "\n" + "\n".join(details[:8]))
        return Response(success=result["ok"], message=message, data=result)

    def api_import_clear(self) -> Any:
        self.save_data(_KEY_IMPORT, {})
        self._log("INFO", "已清空导入预览")
        return Response(success=True, message="已清空导入预览")

    def api_status(self, _=None) -> Response:
        st = self.current_status()
        st["rules"] = [r.to_dict() for r in st["rules"]]
        return Response(success=True, data=st)

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {"cmd": "/rfw_scan", "event": EventType.PluginAction, "desc": "立即扫描 RouterOS 防火墙隐患并推送", "category": "MoviePilot"},
            {"cmd": "/rfw_status", "event": EventType.PluginAction, "desc": "查看防火墙规则数与隐患概况", "category": "MoviePilot"},
            {"cmd": "/rfw_refresh", "event": EventType.PluginAction, "desc": "刷新四表规则缓存", "category": "MoviePilot"},
            {"cmd": "/rfw_backup", "event": EventType.PluginAction, "desc": "导出防火墙规则快照到 MP", "category": "MoviePilot"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data
        cmd = str(data.get("cmd") or "").lower()
        channel = data.get("channel")
        userid = data.get("user_id")
        static = {
            "/rfw_scan": self._cmd_scan, "/rfw_status": self._cmd_status,
            "/rfw_refresh": self._cmd_refresh, "/rfw_backup": self._cmd_backup,
        }
        if cmd in static:
            static[cmd](channel, userid)

    def _cmd_scan(self, channel, userid) -> None:
        started = self.start_scan(trigger="远程命令", prefer_thread=True)
        self._send_message("防火墙扫描", "已触发扫描" if started else "已有扫描在运行",
                           channel=channel, userid=userid)

    def _cmd_status(self, channel, userid) -> None:
        st = self.current_status()
        high = sum(1 for f in st["findings"] if f.get("severity") == SEV_HIGH)
        text = (f"规则：{st['summary'].get('total', 0)} 条"
                f"（禁用 {st['summary'].get('disabled', 0)}）\n"
                f"隐患：{len(st['findings'])} 项（高危 {high}）\n"
                f"上次扫描：{st['last_scan']}")
        self._send_message("防火墙 · 状态", text, channel=channel, userid=userid)

    def _cmd_refresh(self, channel, userid) -> None:
        client, http = self._make_client()
        if client is None:
            self._send_message("防火墙", "网络模块不可用或未配置地址", channel=channel, userid=userid)
            return
        rules = self._run_coroutine(client.list_all(client=http, tables=self._tables))
        self.save_data(_KEY_RULES, [r.to_dict() for r in rules])
        self.save_data(_KEY_FINDINGS, [f.to_dict() for f in detect_findings(rules)])
        self._send_message("防火墙 · 刷新", f"已刷新 {len(rules)} 条规则",
                           channel=channel, userid=userid)

    def _cmd_backup(self, channel, userid) -> None:
        client, http = self._make_client()
        if client is None:
            self._send_message("防火墙", "网络模块不可用或未配置地址", channel=channel, userid=userid)
            return
        snap = self._run_coroutine(client.export_snapshot(client=http, tables=self._tables))
        self.save_data(_KEY_SNAPSHOT, snap)
        self._send_message("防火墙 · 快照", f"已导出 {snap.get('count', 0)} 条规则到 MP",
                           channel=channel, userid=userid)


def _parse_op_payload(payload: Optional[dict]) -> Tuple[str, str, bool]:
    """解析启停/移动/删除的 POST body，兼容下划线与连字符键名。"""
    if not isinstance(payload, dict):
        return "", "", False
    table = str(payload.get("table") or "").strip().lower()
    rid = str(payload.get("id") or payload.get("rule_id") or "").strip()
    disabled = bool(payload.get("disabled", False))
    return table, rid, disabled
