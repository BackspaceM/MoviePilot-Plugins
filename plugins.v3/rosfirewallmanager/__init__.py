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
    from fastapi import Body
    _BODY_DEFAULT = Body(default=None)
except Exception:  # noqa: BLE001
    _BODY_DEFAULT = None  # type: ignore

try:
    _PLUGIN_MTYPE = MessageType.Plugin
except Exception:  # noqa: BLE001
    _PLUGIN_MTYPE = None

from .clients import RouterOSFirewallClient, build_firewall_client
from .importer import ImportRule, build_import_preview
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
_DEFAULT_LIMIT = 60

# plugindata keys
_KEY_RULES = "rfm_rules"
_KEY_FINDINGS = "rfm_findings"
_KEY_LOGS = "rfm_logs"
_KEY_LAST_SCAN = "rfm_last_scan"
_KEY_LAST_OP = "rfm_last_op"
_KEY_SNAPSHOT = "rfm_snapshot"
_KEY_CONN = "rfm_conn"
_KEY_IMPORT = "rfm_import_preview"

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
    plugin_version = "1.1.0"
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
        self._new_rule: Dict[str, Any] = {}

    # -- 配置装载 ----------------------------------------------------------

    def init_plugin(self, config: dict) -> None:
        config = dict(config or {})
        self.stop_service()
        self._enabled = bool(config.get("enabled"))

        self._address = str(config.get("router_address") or "").strip()
        self._port = int(config.get("router_port") or 443)
        self._use_ssl = bool(config.get("router_ssl", True))
        self._username = str(config.get("router_username") or "").strip()
        self._password = str(config.get("router_password") or "")
        self._timeout = max(3, min(60, int(config.get("timeout") or 10)))

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
        self._display_limit = max(10, min(200, int(config.get("display_limit") or _DEFAULT_LIMIT)))

        self._new_rule = {
            key: config.get(key) for key in _NEW_RULE_FIELDS
        }
        self._new_rule["table"] = str(config.get("new_rule_table") or "filter").strip().lower()

        # 批量导入：只解析出「待确认」预览，不自动写入 RouterOS。
        # 用户在详情页核对中文含义后，点「确认批量写入」才会一次性下发。
        preview = build_import_preview(config)
        preview["created"] = now_text_local()
        self.save_data(_KEY_IMPORT, preview)
        if preview.get("rules"):
            self._log("INFO", f"批量导入待确认：解析出 {len(preview['rules'])} 条规则"
                              f"（来源：{preview.get('source')}）")
        for err in (preview.get("errors") or [])[:5]:
            self._log("WARN", f"批量导入解析：{err}")

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
            "kwargs": {"trigger": "定时扫描", "prefer_thread": True},
        }]

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        defaults: Dict[str, Any] = {
            "enabled": False,
            "add_once": False,
            "router_address": "",
            "router_port": 443,
            "router_ssl": True,
            "router_username": "",
            "router_password": "",
            "timeout": 10,
            "manage_tables": list(_DEFAULT_TABLES),
            "scan_enabled": False,
            "scan_cron": _DEFAULT_CRON,
            "notify_mode": "issue",
            "dry_run": False,
            "before_change_backup": True,
            "comment_prefix": "",
            "display_limit": _DEFAULT_LIMIT,
            "new_rule_table": "filter",
            "new_rule_chain": "input",
            "new_rule_action": "accept",
            "new_rule_protocol": "",
            "new_rule_src_address": "",
            "new_rule_dst_address": "",
            "new_rule_src_port": "",
            "new_rule_dst_port": "",
            "new_rule_in_interface": "",
            "new_rule_out_interface": "",
            "new_rule_connection_state": "",
            "new_rule_comment": "",
            "new_rule_place": "before_drop",
            "new_rule_before_id": "",
            "new_rule_disabled": False,
            "new_rule_log": False,
            "new_rule_log_prefix": "",
            "import_table": "filter",
            "import_place": "append",
            "import_disabled": False,
            "import_file": None,
            "import_file_path": "",
            "import_rules": "",
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
            return {"component": "VSwitch", "props": {"model": model, "label": label, "hint": hint}}

        table_items = [{"title": TABLE_LABELS[t], "value": t} for t in TABLES]
        action_items = [{"title": a, "value": a} for a in ACTION_OPTIONS]
        proto_items = [{"title": p or "任意", "value": p} for p in PROTOCOL_OPTIONS]
        cs_items = [{"title": c or "不限", "value": c} for c in CONNSTATE_OPTIONS]
        place_items = [{"title": v, "value": k} for k, v in _PLACE_LABELS.items()]

        content: List[dict] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-4"},
             "content": {"text": "在 MP 里可视化管理 RouterOS 防火墙：按执行顺序查看规则、自动揪出被遮挡的死规则与重复规则，"
                                 "逐条启停/排序/删除，或填写表单新增。本插件是「管理器」不是「自动对齐器」，"
                                 "绝不批量删除你的规则；删除、置底等破坏性操作都需二次确认。RouterOS 用户需具备 read/write/api 权限。"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sw("enabled", "启用插件", "关闭后不再定时扫描，但详情页仍可手动操作")]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sw("scan_enabled", "定时扫描隐患", "按下面的 cron 周期扫描并推送")]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sw("dry_run", "试运行", "只预览命令，不真正改动 RouterOS")]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VLabel", "text": "RouterOS 连接"},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("router_address", "RouterOS 地址", {"placeholder": "192.168.1.1 或 https://ros.local"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [
                    fld("router_port", "端口", {"type": "number"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sw("router_ssl", "使用 HTTPS", "关闭则用 HTTP（不推荐，密码明文）")]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("router_username", "用户名", {"placeholder": "需 read/write/api 权限"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("router_password", "密码", {"type": "password"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("timeout", "请求超时(秒)", {"type": "number",
                        "hint": "RouterOS 负载高时可适当调大", "persistent-hint": True})]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sel("manage_tables", "管理范围（可多选）", table_items,
                        {"multiple": True, "hint": "详情页展示与扫描覆盖的表", "persistent-hint": True})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("scan_cron", "扫描周期(cron)", {"placeholder": "0 9 * * 1  每周一 09:00"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sel("notify_mode", "通知方式",
                        [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()])]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sw("before_change_backup", "改动前自动留备份", "在 RouterOS 执行 /system/backup/save")]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("comment_prefix", "新增规则备注前缀", {"placeholder": "可选，如 rfm-  便于识别本插件规则"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("display_limit", "详情页操作按钮上限", {"type": "number",
                        "hint": "仅前 N 条规则生成快捷按钮，避免页面过长", "persistent-hint": True})]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-1"},
             "text": "批量导入规则（推荐）"},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-3"},
             "content": {"text": "把 RouterOS 导出的 .rsc 或防火墙规则粘贴到文本框、选择文件上传，或填写容器内文件路径。"
                                 "保存配置后到详情页查看逐条中文含义与风险提示，确认无误再点「确认批量写入」，"
                                 "一次性写入 RouterOS（不再一条一条添加）。解析阶段不会改动设备，也不会删除已有规则。"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sel("import_table", "默认表",
                        [{"title": TABLE_LABELS[t], "value": t} for t in TABLES],
                        {"hint": "规则文本未写 /ip firewall <表> 时使用",
                         "persistent-hint": True})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sel("import_place", "插入位置", [
                        {"title": "追加到链末尾", "value": "append"},
                        {"title": "插到兜底 drop 之前（推荐）", "value": "before_drop"}],
                        {"hint": "放行规则建议插到兜底 drop 之前，避免变成死规则",
                         "persistent-hint": True})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sw("import_disabled", "导入后先禁用", "建好规则但不生效，便于逐条核对")]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                    {"component": "VFileInput", "props": {
                        "model": "import_file", "label": "上传规则文件（.rsc / .txt）",
                        "accept": ".rsc,.txt,.cfg,text/plain",
                        "prepend-icon": "mdi-file-upload-outline", "clearable": True,
                        "persistent-hint": True,
                        "hint": "选择 RouterOS 导出的 .rsc 文件；若前端不支持选择文件，请直接粘贴到下方文本框"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("import_file_path", "或填容器内文件路径",
                        {"placeholder": "/config/firewall.rsc",
                         "hint": "文件已放进 MP 容器时使用，最大 1MB", "persistent-hint": True})]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VTextarea", "props": {
                        "model": "import_rules", "label": "规则文本 / 粘贴 .rsc 内容", "rows": 5,
                        "auto-grow": True, "persistent-hint": True,
                        "hint": "每行一条：add chain=input action=accept protocol=tcp dst-port=8291 comment=\"allow winbox\"；"
                                "可用 /ip firewall filter、/ip firewall nat 切换表，支持反斜杠续行与 # 注释",
                        "placeholder": "/ip firewall filter\n"
                                       "add chain=input action=accept protocol=tcp dst-port=8291 comment=\"allow winbox\"\n"
                                       "add chain=forward action=drop src-address=10.0.0.0/8"}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-1"},
             "text": "高级：单条新增规则"},
            {"component": "VAlert", "props": {"type": "info", "variant": "text", "class": "mb-2"},
             "content": {"text": "仅需要补一条规则时使用；填好后打开下方开关并保存，即刻写入 RouterOS。"
                                 "多条规则请改用上方「批量导入」。"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sel("new_rule_table", "表", table_items)]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("new_rule_chain", "链 chain", {"placeholder": "input / forward / srcnat…"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    sel("new_rule_action", "动作 action", action_items)]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [sel("new_rule_protocol", "协议", proto_items)]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [fld("new_rule_src_address", "源地址", {"placeholder": "192.168.1.0/24"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [fld("new_rule_dst_address", "目的地址", {"placeholder": "0.0.0.0/0"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [fld("new_rule_dst_port", "目的端口", {"placeholder": "8291 或 80,443"})]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [fld("new_rule_src_port", "源端口")]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [fld("new_rule_in_interface", "入接口", {"placeholder": "ether1 / LAN"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [fld("new_rule_out_interface", "出接口", {"placeholder": "ether1 / WAN"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [sel("new_rule_connection_state", "连接状态", cs_items)]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [fld("new_rule_comment", "备注 comment", {"placeholder": "allow winbox from LAN"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [sel("new_rule_place", "插入位置", place_items)]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [fld("new_rule_before_id", "指定规则 id", {"placeholder": "仅当插入位置=指定规则之前时填写，如 *5"})]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [sw("new_rule_disabled", "添加后即禁用", "先建规则但不生效，便于核对")]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [sw("new_rule_log", "记录日志", "匹配此规则时写日志")]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [fld("new_rule_log_prefix", "日志前缀", {"placeholder": "rfw:"})]},
            ]},
            {"component": "VRow", "props": {"class": "mt-2"}, "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    sw("add_once", "立即添加这条规则", "保存后按上面的字段写入 RouterOS，并自动复位本开关")]},
            ]},
            {"component": "VAlert", "props": {"type": "warning", "variant": "text", "class": "mt-2"},
             "content": {"text": "安全提示：新增放行规则默认插到兜底 drop 之前（否则会变成永不匹配的死规则）。"
                                 "改动防火墙可能导致远程中断，建议开启「改动前自动留备份」，并确认有一条放行你管理 IP/SSH 的规则。"}},
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

    def start_scan(self, trigger: str = "手动", prefer_thread: bool = False) -> bool:
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
        """详情页：卡片分区 + 批量导入预览 + 分组彩色按钮。"""
        status = self.current_status()
        rules: List[Rule] = status["rules"]
        findings: List[Finding] = [Finding.from_dict(f) for f in status["findings"]]
        conn = status["conn"]
        last_op = status["last_op"]
        limit = int(status["display_limit"])
        preview = dict(self.get_data(_KEY_IMPORT) or {})
        import_items = [ImportRule.from_dict(d) for d in (preview.get("rules") or [])]
        import_errors = [str(e) for e in (preview.get("errors") or [])]

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

        high = sum(1 for f in findings if f.severity == SEV_HIGH)
        med = sum(1 for f in findings if f.severity == "medium")
        high_risk = sum(1 for r in import_items if "高危" in r.risk)

        # 批量导入预览表格：含义与风险一目了然，确认后才写入
        import_rows = []
        for r in import_items:
            action_color = ("success" if r.action == "accept" else
                            "error" if r.action in ("drop", "reject") else "primary")
            import_rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-caption"}, "text": f"#{r.index}"},
                    {"component": "td", "props": {"class": "text-caption"}, "text": r.table_label},
                    {"component": "td", "props": {"class": "text-caption"}, "text": r.chain or "-"},
                    {"component": "td", "content": [
                        {"component": "VChip", "props": {"size": "x-small", "variant": "tonal",
                                                         "color": action_color},
                         "text": r.action or "-"}]},
                    {"component": "td", "props": {"class": "text-caption",
                                                  "style": "max-width:420px"},
                     "content": [{"component": "div", "props": {"class": "text-truncate"},
                                  "text": md_safe(r.meaning)}]},
                    {"component": "td", "props": {"class": "text-caption",
                                                  "style": "max-width:180px"},
                     "content": [{"component": "div",
                                  "text": md_safe(r.risk or "-")}]},
                ],
            })

        if import_items:
            place_text = ("插到兜底 drop 之前" if preview.get("place") == "before_drop"
                          else "追加到链末尾")
            import_body: List[dict] = [
                {"component": "VAlert", "props": {
                    "type": "warning" if high_risk else "info", "variant": "tonal", "class": "mb-2"},
                 "content": {"text": md_safe(
                     f"共解析 {len(import_items)} 条规则，高危 {high_risk} 条；"
                     f"来源：{preview.get('source') or '-'}；解析时间：{preview.get('created') or '-'}；"
                     f"写入位置：{place_text}。请逐条核对含义后再确认写入。")}},
                {"component": "VTable", "props": {"density": "compact", "hover": True},
                 "content": [
                     {"component": "thead", "content": [
                         {"component": "tr", "content": [
                             {"component": "th", "text": t}
                             for t in ("#", "表", "链", "动作", "规则含义", "风险提示")]}]},
                     {"component": "tbody", "content": import_rows},
                 ]},
            ]
            if import_errors:
                import_body.append({"component": "VAlert", "props": {
                    "type": "warning", "variant": "text", "class": "mt-2"},
                    "content": {"text": "解析提示：" + "；".join(md_safe(e) for e in import_errors[:5])}})
            import_body.append({"component": "div", "props": {"class": "mt-3"}, "content": [
                self._action_button(f"确认批量写入（{len(import_items)} 条）", "import_apply",
                                    color="primary", icon="mdi-upload-multiple",
                                    variant="flat", confirm=True),
                self._action_button("清空待导入", "import_clear", color="error",
                                    icon="mdi-close-circle-outline"),
            ]})
        else:
            import_body = [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                 "content": {"text": "尚未有待导入规则。在「设置」页粘贴规则文本、上传 .rsc 文件或填写容器内路径并保存后，"
                                     "这里会逐条显示中文含义与风险提示；确认无误再一键批量写入 RouterOS。"}}
            ]
            if import_errors:
                import_body.append({"component": "VAlert", "props": {
                    "type": "warning", "variant": "tonal", "class": "mt-2"},
                    "content": {"text": "解析提示：" + "；".join(md_safe(e) for e in import_errors[:5])}})

        primary = [
            self._action_button("立即扫描隐患", "scan", color="primary",
                                icon="mdi-radar", variant="flat"),
            self._action_button("刷新规则", "refresh", color="info", icon="mdi-refresh"),
        ]
        secondary = [
            self._action_button("测试连接", "test", icon="mdi-lan-connect"),
            self._action_button("备份快照到 MP", "backup", icon="mdi-content-save-outline"),
            self._action_button("RouterOS 留备份", "router_backup", icon="mdi-backup-restore"),
            self._action_button("停止任务", "stop", color="error",
                                icon="mdi-stop-circle-outline", confirm=True),
        ]

        op_rows: List[dict] = []
        for r in rules[:limit]:
            op_rows.append(self._rule_op_row(r))
        truncated = max(0, len(rules) - limit)

        nodes: List[dict] = [
            {"component": "VAlert", "props": {
                "type": "warning" if high else "info", "variant": "tonal", "class": "mb-3",
                "title": "隐患检测"},
             "content": {"text": md_safe(
                 f"隐患 {len(findings)} 项（高危 {high}｜中危 {med}）。"
                 + ("点击「立即扫描隐患」刷新检测。" if not findings else
                    "高危项多是「放行规则被兜底 drop 遮挡」或「拒绝规则被全放行架空」，请优先处理。"))}},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": "运行概览"},
                {"component": "VCardText", "content": [
                    {"component": "VRow", "content": [
                        kv("插件状态", "已启用" if status["enabled"] else "未启用"),
                        kv("当前任务", "运行中" if status["running"] else "待命"),
                        kv("连接状态", ("正常" if conn.get("ok") else
                                        ("异常" if conn else "未测试")) + f"｜{conn.get('msg', '')}"),
                        kv("试运行", "是（不改动）" if status["dry_run"] else "否"),
                        kv("管理表", "、".join(TABLE_LABELS.get(t, t) for t in status["tables"])),
                        kv("扫描周期", status["scan_cron"] if status["scan_enabled"] else "未启用定时"),
                        kv("上次扫描", status["last_scan"]),
                        kv("规则总数", f"{status['summary'].get('total', 0)} 条"
                                       f"（禁用 {status['summary'].get('disabled', 0)}"
                                       f"｜动态 {status['summary'].get('dynamic', 0)}）"),
                    ]},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                 "text": "批量导入 · 待确认规则"},
                {"component": "VCardText", "content": import_body},
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": "快捷操作"},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": "扫描与刷新"},
                    {"component": "div", "content": primary},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": "连接、备份与停止"},
                    {"component": "div", "content": secondary},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                 "text": f"隐患明细（{len(findings)} 项）"},
                {"component": "VCardText", "content": [text_row(format_findings_text(findings))]},
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                 "text": "规则一览（自上而下即执行顺序）"},
                {"component": "VCardText", "content": [
                    text_row(format_rules_text(rules, table)) for table in status["tables"]
                ]},
            ]},
        ]

        if op_rows:
            op_content: List[dict] = [
                {"component": "VLabel", "text": f"逐条操作（前 {min(len(rules), limit)} 条）"},
                *op_rows,
            ]
            if truncated:
                op_content.append(text_row(
                    f"……另有 {truncated} 条规则未显示操作按钮（可调大配置页「详情页操作按钮上限」）", 11))
            nodes.append({"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"},
                          "content": [{"component": "VCardText", "content": op_content}]})
        else:
            nodes.append({"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"},
                          "content": [{"component": "VCardText", "content": [
                              text_row("尚无规则数据，点击「刷新规则」或「立即扫描隐患」加载。")]}]})

        if last_op:
            head = md_safe(
                f"{'成功' if last_op.get('ok') else '失败'}：{last_op.get('message', '')}"
                + (f"\n位置：{last_op.get('note')}" if last_op.get("note") else ""))
            op_last: List[dict] = [
                {"component": "div", "text": head,
                 "props": {"style": "white-space:pre-wrap;font-size:12px"}},
            ]
            if last_op.get("preview"):
                op_last.append({"component": "div",
                                "text": "命令：" + md_safe_command(last_op["preview"]),
                                "props": {"style": "white-space:pre-wrap;font-size:12px"}})
            nodes.append({"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"},
                          "content": [
                              {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                               "text": "最近一次操作"},
                              {"component": "VCardText", "content": op_last},
                          ]})

        logs = list(self.get_data(_KEY_LOGS) or [])
        log_nodes = [text_row(format_log_line(l.get("time", ""), l.get("level", ""),
                                              l.get("message", "")))
                     for l in logs[:20]] or [text_row("暂无日志，点击上方「立即扫描隐患」或「测试连接」后显示。")]
        nodes.append({"component": "VCard", "props": {"variant": "tonal"}, "content": [
            {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
             "text": "运行日志（最近 20 条）"},
            {"component": "VCardText", "content": log_nodes},
        ]})
        return nodes

    def get_api(self) -> List[Dict[str, Any]]:
        # path 为相对路径：宿主自动补齐 /api/v1/plugin/<PluginID> 前缀
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
            {"path": "/import_apply", "auth": "bear", "endpoint": self.api_import_apply,
             "methods": ["POST"], "summary": "确认后批量写入待导入规则"},
            {"path": "/import_clear", "auth": "bear", "endpoint": self.api_import_clear,
             "methods": ["POST"], "summary": "清空待导入规则"},
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

    def api_status(self, _=None) -> Response:
        st = self.current_status()
        st["rules"] = [r.to_dict() for r in st["rules"]]
        return Response(success=True, data=st)

    def api_import_apply(self, _=None) -> Response:
        """用户确认后，一次性把预览中的规则批量写入 RouterOS。"""
        preview = dict(self.get_data(_KEY_IMPORT) or {})
        items = [ImportRule.from_dict(d) for d in (preview.get("rules") or [])]
        if not items:
            return Response(success=False,
                            message="没有待导入规则：请先在设置页粘贴/上传规则并保存，再到此处确认")
        if self._running:
            return Response(success=False, message="已有扫描任务在运行，请稍后再试")
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="网络模块不可用或未配置 RouterOS 地址")

        place_mode = str(preview.get("place") or "append")
        if self._before_backup and not self._dry_run:
            bok, bmsg = self._run_coroutine(
                client.save_routeros_backup(
                    f"rfm-import-{time.strftime('%Y%m%d-%H%M%S')}", client=http))
            self._log("INFO" if bok else "WARN", f"【批量导入】改动前备份：{bmsg}")

        rules_now = self._run_coroutine(client.list_all(client=http, tables=self._tables))
        ok_count = 0
        failures: List[str] = []
        for item in items:
            place = item.place_before or None
            if not place and place_mode == "before_drop":
                place, _ = choose_insert_place(rules_now, item.chain, "before_drop", "")
            payload = dict(item.payload)
            self._apply_comment_prefix(payload)
            ok, msg = self._run_coroutine(
                client.add_rule(item.table, payload, place, client=http))
            if ok:
                ok_count += 1
            else:
                failures.append(f"#{item.index} {msg}")
            self._log("INFO" if ok else "WARN", f"【批量导入】#{item.index} {msg}")

        message = (f"批量写入完成：成功 {ok_count} / {len(items)} 条"
                   + ("（试运行，未实际改动）" if self._dry_run else ""))
        result = {
            "ok": not failures, "total": len(items), "ok_count": ok_count,
            "fail_count": len(failures), "message": message,
            "errors": failures[:10], "dry_run": self._dry_run,
            "preview": items[0].command if items else "",
        }
        self.save_data(_KEY_LAST_OP, result)
        if ok_count and not self._dry_run:
            fresh = self._run_coroutine(client.list_all(client=http, tables=self._tables))
            self.save_data(_KEY_RULES, [r.to_dict() for r in fresh])
            self.save_data(_KEY_FINDINGS, [f.to_dict() for f in detect_findings(fresh)])
        self._clear_import_preview()
        self._send_message("防火墙批量导入完成" if not failures else "防火墙批量导入部分失败",
                           message + ("；失败：" + "；".join(failures[:3]) if failures else ""))
        return Response(success=not failures, message=message, data=result)

    def api_import_clear(self, _=None) -> Response:
        """放弃本次待导入规则。"""
        self._clear_import_preview()
        return Response(success=True, message="已清空待导入规则")

    def _clear_import_preview(self) -> None:
        """清空预览与配置里的导入内容，避免再次保存配置时重复导入。"""
        self.save_data(_KEY_IMPORT, {})
        try:
            cfg = dict(self.get_config() or {})
            changed = False
            for key, empty in (("import_rules", ""), ("import_file", None),
                               ("import_file_path", "")):
                if cfg.get(key) not in (None, ""):
                    cfg[key] = empty
                    changed = True
            if changed:
                self.update_config(cfg)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}：清空导入配置失败：{err}")

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
