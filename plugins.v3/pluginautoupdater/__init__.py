"""插件自动更新 —— MoviePilot V3 插件主类。

扫描装了哪些插件（ID / 名称 / 作者 / 当前版本 / 可更新版本 / 来源仓库），
配置页先按作者单选筛一遍，再用另一个下拉多选要更新的插件；可以点「立即运行」，
也能按 cron 定时跑。更新时只挑被选中、市场里确实有新版的插件，走宿主的安装入口。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import html as _html
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.types import EventType, MessageType
from app.sdk.events import eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

try:
    from apscheduler.triggers.cron import CronTrigger
except Exception:  # noqa: BLE001 - 离线桩环境
    CronTrigger = None  # type: ignore

try:
    from app.sdk import scheduler as scheduler_sdk
except Exception:  # noqa: BLE001 - 离线桩环境
    scheduler_sdk = None  # type: ignore

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

from . import host, updater

PLUGIN_ID = "PluginAutoUpdater"
_LOG_PREFIX = "插件自动更新"
_DEFAULT_CRON = "0 5 * * *"
_ONCE_JOB_ID = "pau_once"
_MAX_LOGS = 200
_MAX_HISTORY = 30

_KEY_SCAN = "pau_last_scan"
_KEY_REPORT = "pau_last_report"
_KEY_HISTORY = "pau_history"
_KEY_LOGS = "pau_logs"
_KEY_PAGE_AUTHOR = "pau_page_author"

_NOTIFY_LABELS = {
    "issue": "有更新或失败时",
    "all": "每次运行都通知",
    "none": "不通知",
}


def _esc(value: Any) -> str:
    """动态文本转义：防 XSS，并避免 [xxx] 被前端当 Markdown 参考式链接渲染。"""
    return (_html.escape(str(value if value is not None else ""), quote=False)
            .replace("[", "［").replace("]", "］"))


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class PluginAutoUpdater(_PluginBase):
    # 类级元数据一律用字符串字面量：官方版本检查以 AST 提取
    plugin_name = "插件自动更新"
    plugin_desc = ("扫描已安装插件，按作者筛选后多选要更新的插件，"
                   "点「立即运行」或按 cron 定时执行；只更新选中的、市场里有新版的插件。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/pluginautoupdater.png"
    plugin_version = "1.0.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "pluginautoupdater_"
    plugin_color = "#2E7D32"
    system_version = ">=3.0.0"
    plugin_order = 56

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cfg: Dict[str, Any] = {}
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._enabled = True
        self._onlyonce = False
        self._cron = _DEFAULT_CRON
        self._author_filter = updater.ALL_AUTHORS
        self._target_plugins: List[str] = []
        self._dry_run = False
        self._include_self = False
        self._force_reinstall = False
        self._max_per_run = 10
        self._notify_mode = "issue"
        self._display_limit = 40
        self._repo_url_override = ""
        self._timeout = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._cfg = cfg
        self.stop_service()

        self._enabled = bool(cfg.get("enabled"))
        self._onlyonce = bool(cfg.get("onlyonce"))
        self._cron = str(cfg.get("cron") or _DEFAULT_CRON).strip() or _DEFAULT_CRON
        valid, normalized = updater.validate_cron(self._cron)
        self._cron = normalized if valid else _DEFAULT_CRON

        self._author_filter = str(cfg.get("author_filter") or updater.ALL_AUTHORS)
        selected = cfg.get("target_plugins") or []
        if isinstance(selected, str):
            selected = [x.strip() for x in selected.split(",") if x.strip()]
        self._target_plugins = [str(x) for x in selected]

        self._dry_run = bool(cfg.get("dry_run"))
        self._include_self = bool(cfg.get("include_self"))
        self._force_reinstall = bool(cfg.get("force_reinstall"))
        self._max_per_run = _clamp_int(cfg.get("max_per_run"), 10, 1, 100)
        self._notify_mode = str(cfg.get("notify_mode") or "issue")
        if self._notify_mode not in _NOTIFY_LABELS:
            self._notify_mode = "issue"
        self._display_limit = _clamp_int(cfg.get("display_limit"), 40, 10, 200)
        self._repo_url_override = str(cfg.get("repo_url_override") or "").strip()
        self._timeout = _clamp_int(cfg.get("timeout"), 0, 0, 600)

        self._log("INFO", f"配置已载入｜启用 {self._enabled}｜cron {self._cron}｜"
                          f"作者 {self._author_filter}｜单次上限 {self._max_per_run}")

        if cfg.get("onlyonce"):
            cfg["onlyonce"] = False
            try:
                self.update_config(cfg)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"{_LOG_PREFIX}：复位立即执行开关失败 {err}")
            if not self._enabled:
                self._log("WARN", "插件没启用，先跳过这次立即运行")
            else:
                self._schedule_once("立即执行一次")

    def _schedule_once(self, trigger: str) -> None:
        add_once = getattr(scheduler_sdk, "add_plugin_once_job", None)
        scheduled = False
        if callable(add_once):
            try:
                scheduled = bool(add_once(
                    self.__class__.__name__, _ONCE_JOB_ID,
                    self._once_wrapper, "插件自动更新执行一次",
                    delay_seconds=0, func_kwargs={"trigger": trigger},
                ))
            except Exception as err:  # noqa: BLE001
                self._log("WARN", f"提交一次任务失败，改后台线程｜{err}")
                scheduled = False
        if scheduled:
            self._log("INFO", "已提交一次执行任务")
            return
        thread = threading.Thread(target=self._once_wrapper, args=(trigger,),
                                  name=f"{PLUGIN_ID}Once", daemon=True)
        self._thread = thread
        thread.start()
        self._log("INFO", "已放到后台线程跑一次")

    def _once_wrapper(self, trigger: str) -> None:
        try:
            self.run_once(trigger=trigger)
        except Exception as err:  # noqa: BLE001
            self._log("ERROR", f"执行一次异常｜{err}")

    def get_state(self) -> bool:
        return bool(self._enabled)

    def stop_service(self) -> None:
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remove_once):
            try:
                remove_once(self.__class__.__name__, _ONCE_JOB_ID)
            except Exception:  # noqa: BLE001
                pass
        thread = getattr(self, "_thread", None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=3)
            except Exception:  # noqa: BLE001
                pass

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state() or CronTrigger is None:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except (ValueError, TypeError):
            trigger = CronTrigger.from_crontab(_DEFAULT_CRON)
        return [{
            "id": PLUGIN_ID,
            "name": "插件自动更新",
            "trigger": trigger,
            "func": self.run_once,
            "kwargs": {"trigger": "定时"},
        }]

    # ------------------------------------------------------------------
    # 协程
    # ------------------------------------------------------------------

    @staticmethod
    def _run_coroutine(coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=180)

    async def _api_run(self, trigger: str,
                       dry_run_override: Optional[bool]) -> Dict[str, Any]:
        return self.run_once(trigger=trigger, dry_run_override=dry_run_override)

    # ------------------------------------------------------------------
    # 日志 / 通知
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        text = str(message or "")
        try:
            logs = list(self.get_data(_KEY_LOGS) or [])
            logs.insert(0, {"time": _now_text(), "level": level, "message": text})
            self.save_data(_KEY_LOGS, logs[:_MAX_LOGS])
        except Exception:  # noqa: BLE001
            pass
        line = f"{_LOG_PREFIX}｜{text}"
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
            logger.warning(f"{_LOG_PREFIX}｜通知发送失败｜{err}")

    def _notify(self, report: Dict[str, Any]) -> None:
        mode = self._notify_mode
        if mode == "none":
            return
        if mode == "issue" and not (report.get("success") or report.get("failed")
                                    or report.get("dry_run")):
            return
        self._send_message("插件自动更新", str(report.get("message") or ""))

    # ------------------------------------------------------------------
    # 扫描 / 运行
    # ------------------------------------------------------------------

    def _scan(self) -> Tuple[List[updater.InstalledPlugin], str]:
        installed, online, source = host.scan_plugins()
        plugins = updater.normalize_plugins(installed, online)
        if self._repo_url_override:
            for plugin in plugins:
                plugin.repo_url = self._repo_url_override
        return plugins, source

    def _save_scan(self, plugins: List[updater.InstalledPlugin],
                   source: str) -> None:
        self.save_data(_KEY_SCAN, {
            "time": _now_text(),
            "source": source,
            "plugins": [p.to_dict() for p in plugins],
        })

    def _scan_snapshot(self) -> Dict[str, Any]:
        data = self.get_data(_KEY_SCAN) or {}
        return dict(data) if isinstance(data, dict) else {}

    def run_once(self, trigger: str = "手动",
                 dry_run_override: Optional[bool] = None) -> Dict[str, Any]:
        if not self._state_lock.acquire(blocking=False):
            self._log("WARN", "上一轮还没结束，这次跳过")
            return {"ok": False, "message": "已有任务在运行", "total": 0,
                    "success": 0, "failed": 0, "skipped": 0, "blocked": 0,
                    "duration": 0, "details": [], "dry_run": False,
                    "started": "", "finished": "", "trigger": str(trigger),
                    "source": "", "scanned": 0, "notes": ["并发跳过"]}
        try:
            return self._run_once_inner(trigger, dry_run_override)
        except Exception as err:  # noqa: BLE001 - 异常只记日志不抛出
            self._log("ERROR", f"运行异常｜{err}｜{traceback.format_exc()}")
            return {"ok": False, "message": f"运行异常：{err}", "total": 0,
                    "success": 0, "failed": 0, "skipped": 0, "blocked": 0,
                    "duration": 0, "details": [], "dry_run": False,
                    "started": _now_text(), "finished": _now_text(),
                    "trigger": str(trigger), "source": "", "scanned": 0,
                    "notes": [f"异常｜{err}"]}
        finally:
            self._state_lock.release()

    def _run_once_inner(self, trigger: str,
                        dry_run_override: Optional[bool]) -> Dict[str, Any]:
        started_ts = time.time()
        started = _now_text()
        dry = self._dry_run if dry_run_override is None else bool(dry_run_override)

        plugins, source = self._scan()
        if not plugins:
            # 扫描不到时只读不写：不落盘、不安装、不发通知
            logger.warning(f"{_LOG_PREFIX}｜没读到插件清单，这轮只读")
            return {"ok": False, "message": "没读到插件清单，这轮只读",
                    "total": 0, "success": 0, "failed": 0, "skipped": 0,
                    "blocked": 0, "duration": round(time.time() - started_ts, 3),
                    "details": [], "dry_run": dry, "started": started,
                    "finished": _now_text(), "trigger": str(trigger),
                    "source": source, "scanned": 0, "notes": ["没读到插件清单"]}

        self._save_scan(plugins, source)
        self._log("INFO", f"扫描完成｜{source}｜本机 {len(plugins)} 个插件")

        targets, notes = updater.filter_targets(
            plugins, self._author_filter, self._target_plugins,
            skip_self_id=PLUGIN_ID, include_self=self._include_self)
        plan = updater.build_run_plan(targets, dry, self._max_per_run,
                                      self._force_reinstall)

        outcomes: List[Dict[str, Any]] = []
        for item in plan["items"]:
            if dry:
                outcomes.append({"id": item["id"], "ok": True,
                                 "message": "试运行，没有调用安装", "mechanism": "dry_run"})
                self._log("INFO", f"试运行｜{item['name']}｜{item['from']} → {item['to']}｜跳过安装")
                continue
            ok, msg, mechanism = host.install_plugin(
                item["id"], item["repo_url"], force=True)
            outcomes.append({"id": item["id"], "ok": ok,
                             "message": msg, "mechanism": mechanism})
            self._log("INFO" if ok else "WARN",
                      f"{'成功' if ok else '失败'}｜{item['name']}｜{msg}｜{mechanism}")

        finished = _now_text()
        duration = round(time.time() - started_ts, 3)
        report = updater.summarize_run(plan["items"], {
            "outcomes": outcomes,
            "started": started, "finished": finished, "duration": duration,
            "dry_run": dry, "trigger": str(trigger), "source": source,
            "scanned": len(plugins),
            "skipped": len(plan["skipped"]), "blocked": len(plan["blocked"]),
            "notes": notes,
        })
        report["skipped_items"] = plan["skipped"]
        report["blocked_items"] = plan["blocked"]
        report["plan_notes"] = notes

        self.save_data(_KEY_REPORT, report)
        history = list(self.get_data(_KEY_HISTORY) or [])
        self.save_data(_KEY_HISTORY,
                       updater.append_history(history, report, _MAX_HISTORY))
        self._notify(report)
        self._log("INFO", f"这轮结束｜成功 {report['success']}｜失败 {report['failed']}"
                          f"｜跳过 {report['skipped']}｜耗时 {duration}s")
        return report

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
        plugins, source = self._scan()
        author_items = updater.author_items(plugins)
        current_author = self._author_filter or updater.ALL_AUTHORS
        plugin_items = updater.plugin_items(plugins, current_author)
        offline = not plugins
        if offline:
            plugin_items = [{"title": "没读到插件清单，可以先保存配置，回详情页点扫描",
                             "value": ""}]

        content: List[dict] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                              "class": "mb-4"},
             "content": {"text": "按作者筛完，下面的插件下拉跟着变；不选插件就是更新这个作者的全部。"}},
            {"component": "VRow", "content": [
                self._col(4, [self._sel("author_filter", "按作者筛选（单选）",
                                        author_items,
                                        "选一个作者，下面的插件下拉会跟着收窄")]),
                self._col(3, [self._fld("cron", "定时执行（cron）", "", "0 5 * * *")]),
                self._col(2, [self._fld("max_per_run", "单次最多更新", "", "10", "number")]),
                self._col(3, [self._sel("notify_mode", "通知方式",
                                        [{"title": v, "value": k}
                                         for k, v in _NOTIFY_LABELS.items()])]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, [self._sel("target_plugins", "要更新的插件（可多选）",
                                         plugin_items,
                                         "不选就更新所选作者下的全部插件；只更新确实有新版的",
                                         multiple=True)]),
            ]},
            {"component": "VRow", "content": [
                self._col(2, [self._sw("enabled", "启用", "关掉后定时与立即运行都不跑")]),
                self._col(2, [self._sw("onlyonce", "保存后跑一次", "只跑这一次，开关会自动复位")]),
                self._col(2, [self._sw("dry_run", "试运行", "只走流程，不真的安装")]),
                self._col(3, [self._sw("include_self", "允许更新自己", "默认跳过，避免更新时重载中断")]),
                self._col(3, [self._sw("force_reinstall", "强制重装", "没新版也重装一遍")]),
            ]},
            {"component": "VRow", "content": [
                self._col(3, [self._fld("display_limit", "详情页最多显示行数", "", "40", "number")]),
                self._col(3, [self._fld("timeout", "超时（秒，0 为不限制）", "", "0", "number")]),
                self._col(6, [self._fld("repo_url_override", "来源仓库覆盖（可选）",
                                        "留空就用扫描到的来源仓库")]),
            ]},
        ]
        if offline:
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mt-3"},
                "content": {"text": f"没读到插件清单（{_esc(source)}）。可以先保存配置，"
                                    "回详情页点「扫描已安装插件」再试。"}})
        else:
            content.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis mt-2"},
                "text": _esc(f"这次读到 {len(plugins)} 个插件，来自 {source}")})

        defaults: Dict[str, Any] = {
            "enabled": self._enabled,
            "onlyonce": False,
            "cron": self._cron,
            "author_filter": current_author,
            "target_plugins": list(self._target_plugins),
            "dry_run": self._dry_run,
            "include_self": self._include_self,
            "force_reinstall": self._force_reinstall,
            "max_per_run": self._max_per_run,
            "notify_mode": self._notify_mode,
            "display_limit": self._display_limit,
            "repo_url_override": self._repo_url_override,
            "timeout": self._timeout,
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

    def _filter_button(self, label: str, author: str, active: bool) -> dict:
        return {"component": "VBtn",
                "props": {"variant": "flat" if active else "tonal", "size": "small",
                          "class": "ma-1", "color": "primary" if active else "secondary"},
                "text": _esc(label),
                "events": {"click": {"api": f"plugin/{PLUGIN_ID}/filter",
                                     "method": "post",
                                     "params": {"author": str(author)},
                                     "confirm": False}}}

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
        snapshot = self._scan_snapshot()
        plugins = [updater.InstalledPlugin.from_dict(x)
                   for x in (snapshot.get("plugins") or []) if isinstance(x, dict)]
        report = dict(self.get_data(_KEY_REPORT) or {})
        history = list(self.get_data(_KEY_HISTORY) or [])
        logs = list(self.get_data(_KEY_LOGS) or [])
        page_author = str(self.get_data(_KEY_PAGE_AUTHOR) or updater.ALL_AUTHORS)
        limit = self._display_limit

        author_items = updater.author_items(plugins)
        updatable = [p for p in plugins if p.has_update]
        authors_count = len([a for a in author_items
                             if a["value"] != updater.ALL_AUTHORS])
        if page_author and page_author != updater.ALL_AUTHORS:
            shown = [p for p in plugins
                     if updater.unknown_author(p.author) == page_author]
        else:
            shown = list(plugins)

        content: List[dict] = [
            {"component": "VRow", "content": [
                self._stat_card("已安装插件", f"{len(plugins)} 个",
                                str(snapshot.get("source") or "还没扫描过"), "primary"),
                self._stat_card("作者", f"{authors_count} 位",
                                "从扫描结果里数的", "info"),
                self._stat_card("有更新", f"{len(updatable)} 个",
                                "只更新被选中的", "warning" if updatable else "success"),
                self._stat_card("上次成功", f"{int(report.get('success') or 0)} 个",
                                str(report.get("message") or "还没跑过"), "success"),
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mt-3"}, "content": [
                {"component": "VCardTitle", "text": "操作"},
                {"component": "VCardText", "content": [
                    self._action_button("扫描已安装插件", "scan", "info", "mdi-magnify"),
                    self._action_button("立即运行", "run", "primary", "mdi-update",
                                        confirm=True),
                    self._action_button("试运行", "dryrun", "secondary", "mdi-play-circle-outline"),
                    self._action_button("清空记录", "clear", "warning", "mdi-delete-outline",
                                        confirm=True),
                    self._action_button("查看状态", "status", "info", "mdi-information-outline",
                                        method="get"),
                ]},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": _esc(f"扫描时间 {snapshot.get('time') or '还没扫描'}；"
                                  f"当前表格按「{'全部作者' if page_author == updater.ALL_AUTHORS else page_author}」"
                                  f"显示 {len(shown)} 个")},
                ]},
            ]},
            {"component": "VCard", "props": {"outlined": True, "class": "mt-3"}, "content": [
                {"component": "VCardTitle", "text": "按作者看"},
                {"component": "VCardText", "content": [
                    self._filter_button(item["title"], item["value"],
                                        item["value"] == page_author)
                    for item in author_items
                ]},
            ]},
        ]

        rows: List[dict] = []
        for p in shown[:limit]:
            status = "有新版本" if p.has_update else "已最新"
            if not p.enabled:
                status = "已停用"
            rows.append({"component": "tr", "content": [
                {"component": "td", "props": {"class": "text-caption"}, "text": _esc(p.name)},
                {"component": "td", "props": {"class": "text-caption"}, "text": _esc(p.author)},
                {"component": "td", "props": {"class": "text-caption"}, "text": _esc(p.version)},
                {"component": "td", "props": {"class": "text-caption font-weight-bold text-primary"},
                 "text": _esc(p.latest_version if p.has_update else "-")},
                {"component": "td", "props": {"class": "text-caption"}, "text": _esc(p.repo_url or "-")},
                {"component": "td", "props": {"class": "text-caption"}, "text": _esc(status)},
            ]})
        table_body: List[dict] = []
        if rows:
            table_body.append({"component": "VTable", "props": {
                "density": "compact", "hover": True}, "content": [
                {"component": "thead", "content": [
                    {"component": "tr", "content": [
                        {"component": "th", "text": "名称"},
                        {"component": "th", "text": "作者"},
                        {"component": "th", "text": "当前版本"},
                        {"component": "th", "text": "可更新版本"},
                        {"component": "th", "text": "来源"},
                        {"component": "th", "text": "状态"},
                    ]}]},
                {"component": "tbody", "content": rows},
            ]})
            if len(shown) > limit:
                table_body.append({"component": "div", "props": {
                    "class": "text-caption mt-2"},
                    "text": _esc(f"共 {len(shown)} 个，只显示前 {limit} 个")})
        else:
            table_body.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "title": "还没有扫描结果"},
                "content": {"text": "点上面的「扫描已安装插件」，装了的插件就会列在这里。"}})
        content.append({"component": "VCard", "props": {"outlined": True, "class": "mt-3"},
                        "content": [
                            {"component": "VCardTitle", "text": "扫描到的插件"},
                            {"component": "VCardText", "content": table_body},
                        ]})

        if report:
            lines = [
                f"结果｜{'成功' if report.get('ok') else '有失败'}｜{report.get('message')}",
                f"时间｜{report.get('started')} → {report.get('finished')}｜耗时 {report.get('duration')}s",
                f"触发｜{report.get('trigger')}｜来源｜{report.get('source')}",
                f"计划 {report.get('total')}｜成功 {report.get('success')}"
                f"｜失败 {report.get('failed')}｜跳过 {report.get('skipped')}",
            ]
            for detail in (report.get("details") or [])[:limit]:
                lines.append(f"{'成功' if detail.get('ok') else '失败'}｜"
                             f"{detail.get('name')}｜{detail.get('from')} → {detail.get('to')}"
                             f"｜{detail.get('message')}")
            content.append({"component": "VCard", "props": {
                "variant": "tonal", "class": "mt-3",
                "color": "success" if report.get("ok") else "warning"}, "content": [
                {"component": "VCardTitle", "text": "最近一次运行"},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption mb-1"},
                     "text": _esc(line)} for line in lines]},
            ]})

        if history:
            content.append({"component": "VCard", "props": {
                "variant": "tonal", "class": "mt-3"}, "content": [
                {"component": "VCardTitle", "text": "更新记录"},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption"},
                     "text": _esc(f"{x.get('finished')}｜{x.get('trigger')}｜{x.get('message')}")}
                    for x in history[:_MAX_HISTORY]]},
            ]})

        if logs:
            content.append({"component": "VCard", "props": {
                "variant": "tonal", "class": "mt-3"}, "content": [
                {"component": "VCardTitle", "text": "运行日志（最近 30 条）"},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption"},
                     "text": _esc(f"{x.get('time')}｜{x.get('level')}｜{x.get('message')}")}
                    for x in logs[:30]]},
            ]})
        return content

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/scan", "auth": "bear", "endpoint": self.api_scan,
             "methods": ["POST"], "summary": "扫描已安装插件并刷新清单"},
            {"path": "/run", "auth": "bear", "endpoint": self.api_run,
             "methods": ["POST"], "summary": "立即运行一次更新"},
            {"path": "/dryrun", "auth": "bear", "endpoint": self.api_dryrun,
             "methods": ["POST"], "summary": "试运行，不真的安装"},
            {"path": "/filter", "auth": "bear", "endpoint": self.api_filter,
             "methods": ["POST"], "summary": "按作者筛选详情页表格"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status,
             "methods": ["GET"], "summary": "查询插件状态与最近结果"},
            {"path": "/history", "auth": "bear", "endpoint": self.api_history,
             "methods": ["GET"], "summary": "查询更新记录"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空运行报告、记录与日志"},
            {"path": "/logs", "auth": "bear", "endpoint": self.api_logs,
             "methods": ["GET"], "summary": "查询运行日志"},
        ]

    def api_scan(self, _=None) -> Any:
        plugins, source = self._scan()
        self._save_scan(plugins, source)
        self._log("INFO", f"手动扫描｜{source}｜{len(plugins)} 个插件")
        return Response(success=bool(plugins),
                        message=(f"扫到 {len(plugins)} 个插件：{source}"
                                 if plugins else f"没读到插件清单：{source}"),
                        data={"count": len(plugins), "source": source,
                              "plugins": [p.to_dict() for p in plugins]})

    def api_run(self, _=None) -> Any:
        report = self._run_coroutine(self._api_run("手动", None))
        return Response(success=bool(report.get("ok")),
                        message=str(report.get("message") or ""), data=report)

    def api_dryrun(self, _=None) -> Any:
        report = self._run_coroutine(self._api_run("试运行", True))
        return Response(success=bool(report.get("ok")),
                        message=str(report.get("message") or ""), data=report)

    def api_filter(self, payload: Any = None, **kwargs: Any) -> Any:
        author = ""
        if isinstance(payload, dict):
            author = str(payload.get("author") or "")
        elif isinstance(payload, str):
            author = payload
        elif payload is not None:
            author = str(getattr(payload, "author", "") or "")
        author = author or str(kwargs.get("author") or "")
        author = author or updater.ALL_AUTHORS
        self.save_data(_KEY_PAGE_AUTHOR, author)
        return Response(success=True, message=f"表格已按「{author}」筛选",
                        data={"author": author})

    def api_status(self, _=None) -> Any:
        snapshot = self._scan_snapshot()
        return Response(success=True, data={
            "plugin": PLUGIN_ID,
            "enabled": self._enabled,
            "cron": self._cron,
            "author_filter": self._author_filter,
            "target_plugins": list(self._target_plugins),
            "dry_run": self._dry_run,
            "include_self": self._include_self,
            "force_reinstall": self._force_reinstall,
            "max_per_run": self._max_per_run,
            "notify_mode": self._notify_mode,
            "repo_url_override": self._repo_url_override,
            "timeout": self._timeout,
            "scan": snapshot,
            "last_report": dict(self.get_data(_KEY_REPORT) or {}),
            "history": list(self.get_data(_KEY_HISTORY) or []),
            "logs": list(self.get_data(_KEY_LOGS) or [])[:30],
            "page_author": str(self.get_data(_KEY_PAGE_AUTHOR) or updater.ALL_AUTHORS),
        })

    def api_history(self, _=None) -> Any:
        return Response(success=True, data={
            "history": list(self.get_data(_KEY_HISTORY) or [])[:_MAX_HISTORY]})

    def api_clear(self, _=None) -> Any:
        self.save_data(_KEY_REPORT, {})
        self.save_data(_KEY_HISTORY, [])
        self.save_data(_KEY_LOGS, [])
        logger.info(f"{_LOG_PREFIX}｜运行报告、更新记录和日志已清空")
        return Response(success=True, message="运行报告、更新记录和日志已清空")

    def api_logs(self, _=None) -> Any:
        return Response(success=True, data={
            "logs": list(self.get_data(_KEY_LOGS) or [])[:100]})

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {"cmd": "/pau_scan", "event": EventType.PluginAction,
             "desc": "扫描已安装插件", "category": "MoviePilot"},
            {"cmd": "/pau_run", "event": EventType.PluginAction,
             "desc": "立即运行插件自动更新", "category": "MoviePilot"},
            {"cmd": "/pau_dryrun", "event": EventType.PluginAction,
             "desc": "试运行插件自动更新（不安装）", "category": "MoviePilot"},
            {"cmd": "/pau_status", "event": EventType.PluginAction,
             "desc": "查看插件自动更新状态", "category": "MoviePilot"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data or {}
        cmd = str(data.get("cmd") or "").lower()
        handlers = {
            "/pau_scan": self.api_scan,
            "/pau_run": self.api_run,
            "/pau_dryrun": self.api_dryrun,
            "/pau_status": self.api_status,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return
        result = handler()
        text = f"{getattr(result, 'message', '') or ''}"
        if cmd == "/pau_status":
            report = dict(self.get_data(_KEY_REPORT) or {})
            text = (f"扫描到 {len((self._scan_snapshot().get('plugins') or []))} 个插件；"
                    f"最近一次：{report.get('message') or '还没跑过'}")
        try:
            self.post_message(title="插件自动更新", text=_esc(text),
                              channel=data.get("channel"), userid=data.get("userid"),
                              **({"mtype": _PLUGIN_MTYPE} if _PLUGIN_MTYPE is not None else {}))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}｜远程命令回复失败｜{err}")
