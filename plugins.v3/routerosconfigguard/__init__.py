"""RouterOS 配置备份与漂移检测 —— MoviePilot V3 插件主类。

定期（或手动）通过 RouterOS REST API 读取关键配置分区并持久化快照，
与基线快照做指纹对比，检测新增/删除并高亮敏感分区；漂移或异常时发 MP 通知，
关键路径 INFO 级日志供 SSE 实时查看。

备份落点可选（backup_target）：
- mp：结构化快照写入 MP 插件数据目录（可对比、可回滚，默认）；
- routeros：在 RouterOS 本机生成 .backup 系统备份 + .rsc 配置导出；
- both：两处都存。
漂移检测所需的基线快照始终是轻量的"检测状态"，固定留在 MP（与备份落点无关）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
import traceback
from pathlib import Path
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
    _PLUGIN_MTYPE = MessageType.Plugin
except Exception:  # noqa: BLE001
    _PLUGIN_MTYPE = None

from .clients import (
    RouterOSGuardClient,
    build_routeros_client,
)
from .core import (
    BACKUP_TARGET_LABELS,
    BACKUP_TARGETS,
    DEFAULT_SECTIONS,
    SECTIONS,
    backup_basename,
    basename_from_types,
    diff_snapshots,
    format_diff,
    format_log_line,
    is_our_backup,
    make_snapshot,
    mask_secrets,
    md_safe,
    prune_routeros_files,
    prune_snapshots,
    section_items,
    snapshot_id,
    summary_text,
    validate_cron,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PLUGIN_NAME = "RouterOSConfigGuard"
PLUGIN_ICON = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/routerosconfigguard.png"
PLUGIN_DESCRIPTION = "定时备份 RouterOS 配置并检测漂移（新增/删除高亮敏感分区）；备份可选存于 MP 插件目录、RouterOS 本机（.backup+.rsc）或双份；漂移即时通知。"
PLUGIN_AUTHORS = "BackspaceM"
AUTHOR_URL = "https://github.com/BackspaceM"
PLUGIN_COLOR = "#E67E22"
SYSTEM_VERSION = ">=3.0.0"

_LOG_PREFIX = "RouterOS配置备份"
_ONCE_JOB_ID = "rosguard_once"
_MAX_LOGS = 200
_MAX_SNAPSHOTS = 60
_DEFAULT_CRON = "0 3 * * *"          # 每天 03:00
_DEFAULT_KEEP = 20
_SNAPSHOT_DIR = "snapshots"

# plugindata keys
_KEY_BASELINE = "rosguard_baseline"      # 基线快照 dict（检测状态，始终留 MP）
_KEY_INDEX = "rosguard_index"            # MP 快照索引列表（倒序）
_KEY_LIVE = "rosguard_live"
_KEY_LAST = "rosguard_last"
_KEY_LOGS = "rosguard_logs"
_KEY_LAST_SNAP = "rosguard_last_snap"    # 最近一轮快照（routeros 模式下供手动设基线）
_KEY_ROS_FILES = "rosguard_ros_files"    # RouterOS 本机备份文件清单

_NOTIFY_LABELS = {
    "all": "全部（含无变化）",
    "drift": "仅漂移/异常",
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
class RouterOSConfigGuard(_PluginBase):
    plugin_name = PLUGIN_NAME
    plugin_desc = PLUGIN_DESCRIPTION
    plugin_icon = PLUGIN_ICON
    plugin_version = "1.3.0"
    plugin_author = PLUGIN_AUTHORS
    author_url = AUTHOR_URL
    plugin_config_prefix = "routerosconfigguard_"
    plugin_color = PLUGIN_COLOR
    system_version = SYSTEM_VERSION
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

        selected_sections = config.get("sections")
        if isinstance(selected_sections, str):
            selected_sections = [x.strip() for x in selected_sections.split(",") if x.strip()]
        self._sections = [k for k in (selected_sections or DEFAULT_SECTIONS) if k in {s.key for s in SECTIONS}] or list(DEFAULT_SECTIONS)

        # 备份落点：mp / routeros / both
        target = str(config.get("backup_target") or "mp").strip().lower()
        self._backup_target = target if target in {"mp", "routeros", "both"} else "mp"
        self._backup_prefix = "".join(
            c for c in str(config.get("backup_prefix") or "rosguard").strip()
            if c.isalnum() or c in "_-") or "rosguard"
        self._backup_password = str(config.get("backup_password") or "")
        self._backup_encrypt = bool(config.get("backup_encrypt", True))

        self._cron = str(config.get("cron") or _DEFAULT_CRON).strip() or _DEFAULT_CRON
        if not validate_cron(self._cron)[0]:
            self._cron = _DEFAULT_CRON
        self._keep = max(1, min(_MAX_SNAPSHOTS, int(config.get("keep") or _DEFAULT_KEEP)))
        self._auto_baseline = bool(config.get("auto_baseline", True))
        self._notify_mode = str(config.get("notify_mode") or "drift").strip() or "drift"
        self._timeout = max(3, min(60, int(config.get("timeout") or 10)))
        self._max_rows = max(50, min(20000, int(config.get("max_rows") or 2000)))
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
        return [s for s in (self._password, getattr(self, "_backup_password", "")) if s]

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
            "name": "RouterOS 配置备份与漂移检测",
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
            "sections": list(DEFAULT_SECTIONS),
            "backup_target": "mp",
            "backup_prefix": "rosguard",
            "backup_encrypt": True,
            "backup_password": "",
            "cron": _DEFAULT_CRON,
            "keep": _DEFAULT_KEEP,
            "auto_baseline": True,
            "notify_mode": "drift",
            "timeout": 10,
            "max_rows": 2000,
            "dry_run": False,
        }

        def sel(model: str, label: str, items: List[Dict[str, str]], hint: str) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                {"component": "VSelect", "props": {
                    "model": model, "label": label, "items": items,
                    "multiple": True, "chips": True, "clearable": True,
                    "hint": hint, "persistent-hint": True,
                }},
            ]}

        def fld(model: str, label: str, props: Optional[dict] = None) -> dict:
            p = {"model": model, "label": label}
            if props:
                p.update(props)
            return {"component": "VTextField", "props": p}

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
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-3",
                                              "title": "备份位置怎么选"},
             "content": {"text": "仅存 MP：生成结构化快照，可直接对比与回滚；仅存 RouterOS 本机：调用系统 "
                                 "/system/backup/save 生成 .backup，并导出 .rsc 配置；两处都存则各保留一份。"
                                 "漂移检测用的「基线」是保存在 MP 内的轻量检测状态，与备份文件放在哪里无关。"}},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mb-3",
                                              "title": "使用前的四点说明"},
             "content": {"text": "① 权限：RouterOS 账号需要 read / write / api 权限，否则读取分区或创建备份会失败；"
                                 "② 加密：.backup 是含账号与证书的系统级备份，选择存到 RouterOS 本机时务必开启加密并保存好密码；"
                                 "③ 基线：更换设备、重装系统或首次启用后，请在详情页点「最近设为基线」，否则每次都会被判为漂移；"
                                 "④ 试运行：首次配置建议先开启「试运行」，确认分区能正常读取后再关闭。"
                                 "保留份数会同时轮转 MP 快照与 RouterOS 本机备份，超出的最旧一份会被删除。"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件", "hint": "关闭后不再定时备份"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即执行一次", "hint": "保存后触发一轮备份+检测"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "auto_baseline", "label": "首次自动设基线", "hint": "无基线时把首份快照设为基线"}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"}, "text": "① RouterOS 连接"},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VTextField", "props": {"model": "router_address", "label": "RouterOS 地址",
                     "placeholder": "192.168.1.1 或 https://ros.local"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [
                    fld("router_port", "端口", {"type": "number"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "router_ssl", "label": "使用 HTTPS", "hint": "关闭则用 HTTP"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("router_username", "用户名", {"placeholder": "需具备 read / api 权限"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("router_password", "密码", {"type": "password"})]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"}, "text": "② 备份分区"},
            {"component": "VRow", "content": [
                sel("sections", "备份分区（可多选）", section_items(), "默认勾选常用分区；敏感分区漂移将高亮告警"),
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"}, "text": "③ 备份位置与加密"},
            {"component": "VLabel", "text": "备份位置"},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "backup_target", "label": "备份存放位置",
                        "items": list(BACKUP_TARGETS),
                        "hint": "决定备份归档文件存哪里；漂移基线始终留 MP", "persistent-hint": True,
                    }}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("backup_prefix", "RouterOS 备份文件名前缀",
                        {"hint": "仅 routeros/both 使用；生成 前缀_时间戳.backup/.rsc", "persistent-hint": True})]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSwitch", "props": {
                        "model": "backup_encrypt", "label": "加密 RouterOS 备份",
                        "hint": "仅 routeros/both；强烈建议开启（.backup 未加密含敏感信息）"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    fld("backup_password", "备份加密密码", {"type": "password",
                        "placeholder": "加密 .backup 用；留空则不加密"})]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"}, "text": "④ 调度与高级"},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("cron", "备份周期(cron)", {"placeholder": "0 3 * * *  每天 03:00"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("keep", "保留份数", {"type": "number",
                        "hint": "同时控制 MP 快照与 RouterOS 本机备份的轮转，超出的最旧一份会被删除",
                        "persistent-hint": True})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("timeout", "请求超时(秒)", {"type": "number"})]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSelect", "props": {"model": "notify_mode", "label": "通知方式",
                     "items": [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()]}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    fld("max_rows", "单分区最大行数", {"type": "number"})]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {"model": "dry_run", "label": "试运行", "hint": "只读取不写基线/快照文件"}}]},
            ]},
        ]
        return content, defaults

    # ------------------------------------------------------------------
    # 执行控制
    # ------------------------------------------------------------------

    def start_run(self, trigger: str = "手动", prefer_thread: bool = False) -> bool:
        with self._state_lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()
        self._update_live(running=True, stage=f"{trigger}：读取配置", trigger=trigger)

        def _task() -> None:
            try:
                result = self._run_coroutine(self._async_run(trigger))
                self.save_data(_KEY_LAST, result)
                self._notify_result(result, trigger)
            except Exception as err:  # noqa: BLE001
                self._log("ERROR", f"备份任务异常：{err}\n{traceback.format_exc()[-800:]}")
                self._send_message("RouterOS 备份异常", f"触发：{trigger}\n错误：{err}")
            finally:
                with self._state_lock:
                    self._running = False
                self._update_live(running=False, stage="待命")

        if prefer_thread:
            self._thread = threading.Thread(target=_task, daemon=True, name="ROSGuard-Run")
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
        # 已在事件循环中（如 API 在异步上下文调用）：丢到独立线程跑，避免嵌套 loop
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=180)

    async def _async_run(self, trigger: str) -> Dict[str, Any]:
        started = time.time()
        result: Dict[str, Any] = {
            "trigger": trigger, "ok": False, "snapshot_id": "", "identity": {},
            "sections": self._sections, "fetch": {}, "diff": None,
            "backup_target": self._backup_target, "ros_backup": {"done": False, "files": []},
            "baseline_ts": None, "baseline_set": False, "duration": 0.0, "message": "",
        }
        if AsyncRequestUtils is None:
            result["message"] = "网络模块不可用"
            self._log("ERROR", "网络模块 AsyncRequestUtils 不可用")
            return result
        if not self._address:
            result["message"] = "未配置 RouterOS 地址"
            self._log("WARN", "未配置 RouterOS 地址，跳过")
            return result

        http = AsyncRequestUtils(verify=self._use_ssl is False)
        client: RouterOSGuardClient = build_routeros_client(
            self._address, self._username, self._password, http,
            port=self._port, use_ssl=self._use_ssl, timeout=self._timeout)

        # 1) 连通性
        ok, msg = await client.check(client=http)
        self._log("INFO" if ok else "WARN", f"[{trigger}] RouterOS 连通性：{msg}")
        if not ok:
            result["message"] = msg
            return result

        # 2) 拉取分区
        self._update_live(running=True, stage=f"{trigger}：拉取 {len(self._sections)} 个分区")
        fetched = await client.fetch(self._sections, client=http, max_rows=self._max_rows)
        result["identity"] = fetched.identity
        result["fetch"] = {"ok": fetched.ok_count, "fail": fetched.fail_count, "errors": fetched.errors}
        for key, err in fetched.errors.items():
            self._log("WARN", f"分区 {key}：{err}")
        if fetched.fail_count == len(self._sections):
            result["message"] = "全部分区读取失败"
            self._log("ERROR", "全部分区读取失败，未生成快照")
            return result

        # 3) 组装快照
        snap = make_snapshot(fetched.rows, self._sections)
        result["fetch"]["total_rows"] = sum(s["count"] for s in snap["sections"].values())

        # 4) 与基线对比
        baseline = self.get_data(_KEY_BASELINE)
        result["baseline_ts"] = (baseline or {}).get("ts") if isinstance(baseline, dict) else None
        diff = diff_snapshots(baseline, snap, self._sections)
        result["diff"] = diff
        self._log("INFO", f"[{trigger}] 快照完成：{len(self._sections)} 分区 / "
                          f"{result['fetch']['total_rows']} 行；漂移={summary_text(diff)}")

        # 5) 持久化（试运行不落盘、不改基线）
        sid = snapshot_id(snap["ts"])
        if self._dry_run:
            self._log("INFO", "试运行：跳过快照落盘、RouterOS 备份与基线更新")
        else:
            # 基线始终是轻量检测状态，固定留 MP（与备份落点无关），且供 routeros 模式手动设基线
            self.save_data(_KEY_LAST_SNAP, snap)
            if baseline is None and self._auto_baseline:
                self.save_data(_KEY_BASELINE, snap)
                result["baseline_set"] = True
                result["baseline_ts"] = snap["ts"]
                self._log("INFO", "已自动将首份快照设为基线")

            # MP 插件目录快照（mp / both）
            if self._backup_target in ("mp", "both"):
                if self._persist_snapshot(sid, snap, diff, fetched.identity):
                    result["snapshot_id"] = sid

            # RouterOS 本机备份（routeros / both）
            if self._backup_target in ("routeros", "both"):
                ros = await self._backup_on_routeros(sid, client, http)
                result["ros_backup"] = ros

        result["ok"] = True
        pieces = []
        if result.get("snapshot_id"):
            pieces.append(f"MP快照 {result['snapshot_id']}")
        if result.get("ros_backup", {}).get("done"):
            pieces.append(f"RouterOS备份 {result['ros_backup'].get('basename')}")
        if not pieces:
            pieces.append("试运行" if self._dry_run else "(无归档)")
        result["message"] = "、".join(pieces) + "；" + summary_text(diff)
        self._update_live(
            state_last=summary_text(diff),
            baseline_ts=result["baseline_ts"],
            last_run=snap["ts"],
            last_snapshot=result.get("snapshot_id", ""),
            backup_target=self._backup_target,
            drift=diff.get("has_drift", False),
            sensitive_hits=diff.get("sensitive_hits", []),
            identity=fetched.identity,
        )
        result["duration"] = round(time.time() - started, 2)
        return result

    async def _backup_on_routeros(self, sid: str, client: RouterOSGuardClient,
                                  http: Any) -> Dict[str, Any]:
        """在 RouterOS 本机生成备份 + 导出，并按前缀轮转旧文件。"""
        base = backup_basename(self._backup_prefix, sid)
        out: Dict[str, Any] = {"done": False, "basename": base, "files": [], "errors": []}
        password = self._backup_password if self._backup_encrypt else ""
        ok_bak, detail_bak = await client.backup_save(base, password=password, client=http)
        self._log("INFO" if ok_bak else "WARN", f"[RouterOS备份] 系统备份：{detail_bak}")
        if ok_bak:
            out["files"].append(detail_bak)
        else:
            out["errors"].append(f"系统备份：{detail_bak}")

        ok_exp, detail_exp = await client.export_config(base, client=http)
        self._log("INFO" if ok_exp else "WARN", f"[RouterOS备份] 配置导出：{detail_exp}")
        if ok_exp:
            out["files"].append(detail_exp)
        else:
            out["errors"].append(f"配置导出：{detail_exp}")

        out["done"] = ok_bak or ok_exp
        if not out["done"]:
            return out

        # 轮转：列举本机文件，按备份基名分组，删除超出 keep 的旧组
        files = await client.list_files(client=http)
        keep_files, removed = prune_routeros_files(files, self._backup_prefix, self._keep)
        for old in removed:
            fid = str(old.get(".id") or old.get("id") or "")
            ok_del, err_del = await client.delete_file(fid, client=http)
            self._log("INFO" if ok_del else "WARN",
                      f"[RouterOS备份] 删除旧文件 {old.get('name', fid)}：{'成功' if ok_del else err_del}")
        self.save_data(_KEY_ROS_FILES, [{
            "name": str(f.get("name") or ""),
            "size": str(f.get("size") or ""),
            "created": str(f.get("creation-time") or f.get("created") or ""),
        } for f in keep_files if is_our_backup(str(f.get("name") or ""), self._backup_prefix)])
        out["kept_groups"] = self._keep
        out["removed"] = len(removed)
        return out

    # ------------------------------------------------------------------
    # 快照持久化
    # ------------------------------------------------------------------

    def _snapshot_dir(self) -> Path:
        path = Path(str(self.get_data_path())) / _SNAPSHOT_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _persist_snapshot(self, sid: str, snap: Dict[str, Any], diff: Dict[str, Any],
                          identity: Dict[str, str]) -> bool:
        """写快照文件并维护索引（倒序，保留最近 keep 份）。"""
        try:
            directory = self._snapshot_dir()
            payload = {
                "id": sid,
                "device": identity,
                "sections": self._sections,
                "drift": summary_text(diff),
                "has_drift": diff.get("has_drift", False),
                "snapshot": snap,
            }
            (directory / f"{sid}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as err:  # noqa: BLE001
            self._log("WARN", f"快照文件写入失败：{err}")
            return False

        index = list(self.get_data(_KEY_INDEX) or [])
        index.insert(0, {
            "id": sid, "ts": snap.get("ts"), "device": identity,
            "rows": sum(s["count"] for s in snap["sections"].values()),
            "has_drift": diff.get("has_drift", False), "drift": summary_text(diff),
            "file": f"{sid}.json",
        })
        kept, removed = prune_snapshots(index, self._keep)
        for old in removed:
            try:
                (directory / str(old.get("file") or "")).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        self.save_data(_KEY_INDEX, kept)
        return True

    def _read_snapshot_file(self, sid: str) -> Optional[Dict[str, Any]]:
        safe = "".join(c for c in str(sid) if c.isalnum() or c in "_-.")
        path = self._snapshot_dir() / f"{safe}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 管理动作
    # ------------------------------------------------------------------

    def set_baseline(self, sid: Optional[str] = None) -> Dict[str, Any]:
        """把某份快照（默认最近一份）设为基线。

        mp/both：从 MP 快照文件读取；routeros：无快照文件时回退到最近一轮内存快照
        （_KEY_LAST_SNAP），保证"仅存 RouterOS"模式下也能手动设基线。
        """
        index = list(self.get_data(_KEY_INDEX) or [])
        target = sid or (index[0].get("id") if index else None)
        snap: Optional[Dict[str, Any]] = None
        label = target or "最近一次检测"
        if target:
            payload = self._read_snapshot_file(str(target))
            if payload and isinstance(payload.get("snapshot"), dict):
                snap = payload["snapshot"]
            elif not isinstance(payload, dict):
                # 文件缺失（如 routeros 模式）：尝试最近一轮内存快照
                snap = self.get_data(_KEY_LAST_SNAP)
        if snap is None:
            snap = self.get_data(_KEY_LAST_SNAP)
        if not isinstance(snap, dict) or not snap.get("sections"):
            return {"ok": False, "message": "暂无快照，请先执行一次备份检测"}
        self.save_data(_KEY_BASELINE, snap)
        self._log("INFO", f"已将 {label} 设为基线")
        self._update_live(baseline_ts=snap.get("ts"),
                          last_snapshot=str(target) if target else "最近检测")
        return {"ok": True, "message": f"已将 {label} 设为基线", "ts": snap.get("ts")}

    def clear_snapshots(self) -> Dict[str, Any]:
        """清空 MP 快照文件/索引与基线（不动 RouterOS 本机文件，避免误删灾备）。"""
        removed = 0
        try:
            directory = self._snapshot_dir()
            index = list(self.get_data(_KEY_INDEX) or [])
            for item in index:
                try:
                    (directory / str(item.get("file") or "")).unlink(missing_ok=True)
                    removed += 1
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        self.save_data(_KEY_INDEX, [])
        self.save_data(_KEY_BASELINE, None)
        self.save_data(_KEY_LAST_SNAP, None)
        self._log("INFO", f"已清空 MP 快照（{removed} 份）与基线；RouterOS 本机文件未改动")
        return {"ok": True, "message": f"已清空 {removed} 份 MP 快照与基线（RouterOS 本机文件保留）"}

    def snapshot_list(self) -> List[Dict[str, Any]]:
        return list(self.get_data(_KEY_INDEX) or [])

    def ros_file_list(self) -> List[Dict[str, Any]]:
        return list(self.get_data(_KEY_ROS_FILES) or [])

    def current_status(self) -> Dict[str, Any]:
        live = dict(self.get_data(_KEY_LIVE) or {})
        baseline = self.get_data(_KEY_BASELINE)
        index = self.snapshot_list()
        last = dict(self.get_data(_KEY_LAST) or {})
        return {
            "enabled": self.get_state(),
            "running": bool(getattr(self, "_running", False)),
            "router": self._address,
            "sections": self._sections,
            "backup_target": self._backup_target,
            "backup_target_label": BACKUP_TARGET_LABELS.get(self._backup_target, self._backup_target),
            "baseline_ts": (baseline or {}).get("ts") if isinstance(baseline, dict) else None,
            "baseline_set": isinstance(baseline, dict) and bool(baseline.get("sections")),
            "snapshots": len(index),
            "latest": index[0] if index else None,
            "last": last,
            "ros_files": self.ros_file_list(),
            "live": live,
        }

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
        mode = self._notify_mode
        if mode == "none":
            return
        diff = result.get("diff") or {}
        drifted = bool(diff.get("has_drift"))
        has_issue = (not result.get("ok")) or result.get("fetch", {}).get("fail", 0) > 0
        ros = result.get("ros_backup") or {}
        ros_failed = bool(ros and ros.get("done") is False and ros.get("errors"))
        if mode == "drift" and not (drifted or has_issue or ros_failed):
            return

        lines = [f"触发：{trigger}"]
        if not result.get("ok"):
            lines.append(f"执行失败：{result.get('message', '')}")
        else:
            ident = result.get("identity") or {}
            lines.append(f"设备：{ident.get('name', 'RouterOS')}（{ident.get('version', '')}）")
            lines.append(f"备份：{BACKUP_TARGET_LABELS.get(result.get('backup_target'), result.get('backup_target'))}"
                         f"｜{result.get('message', '')}")
            if drifted:
                lines.append(format_diff(diff, max_rows=6))
            else:
                lines.append("配置无漂移")
            if ros_failed:
                lines.append("⚠ RouterOS 本机备份异常：" + "；".join(ros.get("errors", [])))
        lines.append(f"耗时 {result.get('duration', 0)}s")
        title = "RouterOS 配置漂移告警" if (drifted or has_issue or ros_failed) else "RouterOS 配置备份完成"
        self._send_message(title, "\n".join(lines))

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def _action_button(self, label: str, api: str, *, confirm: bool = False,
                       color: str = "", icon: str = "", variant: str = "tonal",
                       method: str = "post") -> dict:
        """详情页按钮：文字放组件级 text，带颜色/图标，避免出现灰色空按钮。"""
        props: Dict[str, Any] = {"variant": variant, "size": "small", "class": "ma-1"}
        if color:
            props["color"] = color
        if icon:
            props["prepend-icon"] = icon
        return {"component": "VBtn", "props": props, "text": label,
                "events": {"click": {"api": f"plugin/{PLUGIN_NAME}/{api}",
                                     "method": method, "params": {}, "confirm": confirm}}}

    def get_page(self) -> List[dict]:
        """详情页：卡片分区 + 分组彩色按钮；按钮全部指向真实 API。"""
        status = self.current_status()
        index = self.snapshot_list()
        ros_files = self.ros_file_list()
        logs = list(self.get_data(_KEY_LOGS) or [])
        last = status.get("last") or {}
        diff = last.get("diff") or {}
        target_label = BACKUP_TARGET_LABELS.get(self._backup_target, self._backup_target)
        has_drift = bool(diff.get("has_drift"))

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

        snap_lines = [
            f"{it.get('id')}｜{_ts_text(it.get('ts'))}｜{it.get('rows', 0)} 行｜"
            f"{'⚠ ' + str(it.get('drift', '')) if it.get('has_drift') else '无漂移'}"
            for it in index[:8]
        ] or ["暂无 MP 快照（当前备份位置可能不含 MP）"]
        ros_lines = [
            f"{f.get('name')}｜{f.get('size', '')}｜{f.get('created', '')}"
            for f in ros_files[:8]
        ] or ["暂无 RouterOS 本机备份文件"]
        drift_text = format_diff(diff, max_rows=8) if has_drift else (
            "无漂移" if last else "尚未执行检测")
        log_nodes = [text_row(format_log_line(l.get("time", ""), l.get("level", ""),
                                              l.get("message", "")))
                     for l in logs[:20]] or [text_row("暂无运行日志")]

        primary = [
            self._action_button("立即备份并检测", "run", color="primary", icon="mdi-backup-restore",
                                variant="flat"),
            self._action_button("测试连接", "test", color="info", icon="mdi-lan-connect"),
        ]
        secondary = [
            self._action_button("最近设为基线", "baseline", icon="mdi-flag-outline", confirm=True),
            self._action_button("重载快照列表", "snapshots", icon="mdi-refresh", method="get"),
            self._action_button("刷新 ROS 备份", "rosbackups", icon="mdi-file-refresh-outline"),
        ]
        danger = [
            self._action_button("清空快照", "clear", color="error", icon="mdi-delete-outline",
                                confirm=True),
            self._action_button("停止", "stop", color="error", icon="mdi-stop-circle-outline",
                                confirm=True),
        ]

        return [
            {"component": "VAlert", "props": {
                "type": "warning" if has_drift else "success", "variant": "tonal",
                "class": "mb-3", "title": "漂移检测"},
             "content": {"text": ("检测到配置漂移，详情见下方「最近检测结果」。"
                                  if has_drift else "当前配置与基线一致，未检测到漂移。")}},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": "运行概览"},
                {"component": "VCardText", "content": [
                    {"component": "VRow", "content": [
                        kv("插件状态", "已启用" if status.get("enabled") else "未启用"),
                        kv("当前任务", "运行中" if status.get("running") else "待命"),
                        kv("备份位置", target_label),
                        kv("备份周期", self._cron),
                        kv("监控分区", f"{len(self._sections)} 个：{basename_from_types(self._sections)}"),
                        kv("基线时间", _ts_text(status.get("baseline_ts"))),
                        kv("MP 快照份数", status.get("snapshots", 0)),
                        kv("ROS 备份文件", len(ros_files)),
                    ]},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                 "text": "最近检测结果"},
                {"component": "VCardText", "content": [text_row(drift_text, 13)]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VCard", "props": {"variant": "tonal", "class": "h-100"},
                    "content": [
                        {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                         "text": f"快照清单 · MP（共 {len(index)} 份）"},
                        {"component": "VCardText", "content": [text_row(line) for line in snap_lines]},
                    ]}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VCard", "props": {"variant": "tonal", "class": "h-100"},
                    "content": [
                        {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                         "text": f"备份清单 · RouterOS 本机（共 {len(ros_files)} 个）"},
                        {"component": "VCardText", "content": [text_row(line) for line in ros_lines]},
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
                     "text": "备份与检测"},
                    {"component": "div", "content": primary},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": "基线与列表"},
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
            {"path": "/run", "auth": "bear", "endpoint": self.api_run, "methods": ["POST"], "summary": "立即备份并检测漂移"},
            {"path": "/test", "auth": "bear", "endpoint": self.api_test, "methods": ["POST"], "summary": "测试 RouterOS 连接"},
            {"path": "/baseline", "auth": "bear", "endpoint": self.api_baseline, "methods": ["POST"], "summary": "将最近快照设为基线"},
            {"path": "/snapshots", "auth": "bear", "endpoint": self.api_snapshots, "methods": ["GET"], "summary": "获取 MP 快照索引列表"},
            {"path": "/rosbackups", "auth": "bear", "endpoint": self.api_rosbackups, "methods": ["POST"], "summary": "从 RouterOS 重新读取本机备份文件清单"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status, "methods": ["GET"], "summary": "获取当前状态"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear, "methods": ["POST"], "summary": "清空 MP 快照与基线（保留 ROS 本机文件）"},
            {"path": "/stop", "auth": "bear", "endpoint": self.api_stop, "methods": ["POST"], "summary": "停止当前任务"},
        ]

    def api_run(self, _=None) -> Response:
        started = self.start_run(trigger="API 立即检查", prefer_thread=True)
        return Response(success=True, message="已触发备份检测" if started else "已有任务在运行", data={"started": started})

    def api_test(self, _=None) -> Response:
        if AsyncRequestUtils is None or not self._address:
            return Response(success=False, message="网络模块不可用或未配置地址")
        http = AsyncRequestUtils(verify=self._use_ssl is False)
        client = build_routeros_client(self._address, self._username, self._password, http,
                                       port=self._port, use_ssl=self._use_ssl, timeout=self._timeout)
        ok, msg = self._run_coroutine(client.check(client=http))
        return Response(success=bool(ok), message=mask_secrets(msg, self._secrets()))

    def api_baseline(self, _=None) -> Response:
        data = self.set_baseline()
        return Response(success=bool(data.get("ok")), message=data.get("message", ""), data=data)

    def api_snapshots(self, _=None) -> Response:
        return Response(success=True, data=self.snapshot_list())

    def api_rosbackups(self, _=None) -> Response:
        """实时拉取 RouterOS /file，刷新本机备份文件清单。"""
        if AsyncRequestUtils is None or not self._address:
            return Response(success=False, message="网络模块不可用或未配置地址")
        http = AsyncRequestUtils(verify=self._use_ssl is False)
        client = build_routeros_client(self._address, self._username, self._password, http,
                                       port=self._port, use_ssl=self._use_ssl, timeout=self._timeout)
        files = self._run_coroutine(client.list_files(client=http))
        rows = [{
            "name": str(f.get("name") or ""),
            "size": str(f.get("size") or ""),
            "created": str(f.get("creation-time") or f.get("created") or ""),
        } for f in files if is_our_backup(str(f.get("name") or ""), self._backup_prefix)]
        self.save_data(_KEY_ROS_FILES, rows)
        return Response(success=True, message=f"已刷新 {len(rows)} 个 RouterOS 备份文件", data=rows)

    def api_status(self, _=None) -> Response:
        return Response(success=True, data=self.current_status())

    def api_clear(self, _=None) -> Response:
        data = self.clear_snapshots()
        return Response(success=bool(data.get("ok")), message=data.get("message", ""), data=data)

    def api_stop(self, _=None) -> Response:
        self.stop_service()
        self._update_live(running=False, stage="已停止")
        return Response(success=True, message="已停止")

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {"cmd": "/rosguard_status", "event": EventType.PluginAction, "desc": "查看 RouterOS 备份状态与最近漂移", "category": "MoviePilot"},
            {"cmd": "/rosguard_run", "event": EventType.PluginAction, "desc": "立即执行 RouterOS 备份与漂移检测", "category": "MoviePilot"},
            {"cmd": "/rosguard_baseline", "event": EventType.PluginAction, "desc": "将最近快照设为基线", "category": "MoviePilot"},
            {"cmd": "/rosguard_clear", "event": EventType.PluginAction, "desc": "清空 RouterOS 快照与基线", "category": "MoviePilot"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data
        cmd = str(data.get("cmd") or "").lower()
        channel = data.get("channel")
        userid = data.get("user_id")
        handlers = {
            "/rosguard_status": self._cmd_status,
            "/rosguard_run": self._cmd_run,
            "/rosguard_baseline": self._cmd_baseline,
            "/rosguard_clear": self._cmd_clear,
        }
        handler = handlers.get(cmd)
        if handler:
            handler(channel, userid)

    def _cmd_status(self, channel, userid) -> None:
        status = self.current_status()
        last = status.get("last") or {}
        diff = last.get("diff") or {}
        text = (f"备份位置：{status.get('backup_target_label')}\n"
                f"基线：{_ts_text(status.get('baseline_ts'))}\n"
                f"MP 快照：{status.get('snapshots', 0)} 份｜ROS 备份：{len(status.get('ros_files') or [])} 个\n"
                f"最近：{summary_text(diff) if diff else '尚未检测'}")
        if diff.get("has_drift"):
            text += "\n" + format_diff(diff, max_rows=5)
        self._send_message("RouterOS 备份 · 状态", text, channel=channel, userid=userid)

    def _cmd_run(self, channel, userid) -> None:
        started = self.start_run(trigger="远程命令", prefer_thread=True)
        self._send_message("RouterOS 备份", "已触发备份与漂移检测" if started else "已有任务在运行",
                           channel=channel, userid=userid)

    def _cmd_baseline(self, channel, userid) -> None:
        res = self.set_baseline()
        self._send_message("RouterOS 备份 · 基线", res.get("message", ""), channel=channel, userid=userid)

    def _cmd_clear(self, channel, userid) -> None:
        res = self.clear_snapshots()
        self._send_message("RouterOS 备份", res.get("message", ""), channel=channel, userid=userid)
