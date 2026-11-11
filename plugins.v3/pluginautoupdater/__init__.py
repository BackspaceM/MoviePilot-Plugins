"""插件自动更新 —— MoviePilot V3 插件主类。

设置页里两个下拉都能多选：先勾作者，再勾具体插件；填 cron 就能定时跑，
也可以回主界面点「立即运行」。更新只动勾选过、市场里确实有新版的插件，
走宿主自己的安装入口。配置页只读本机插件表，不联网、不等在线目录。
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

# 带参按钮的 POST 是 JSON body；endpoint 参数必须声明 Body 才能注入，
# 裸 Optional[dict] = None 会被 FastAPI 当 query 参数，值恒为 None。
try:
    from fastapi import Body

    _BODY_DEFAULT = Body(default=None)
except Exception:  # noqa: BLE001 - 离线桩环境没有 fastapi
    _BODY_DEFAULT = None

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


def _split_manual(value: Any) -> List[str]:
    """手动补充框：逗号 / 顿号 / 分号 / 换行分隔，去重且保序。"""
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    out: List[str] = []
    for item in raw:
        text = (str(item).replace("，", ",").replace("、", ",")
                .replace(";", ",").replace("；", ",")
                .replace("\r", ",").replace("\n", ",").replace("\t", ","))
        for part in text.split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class PluginAutoUpdater(_PluginBase):
    # 类级元数据一律用字符串字面量：官方版本检查以 AST 提取
    plugin_name = "插件自动更新"
    plugin_desc = ("扫描已安装插件，设置页多选作者和插件，按 cron 定时或点按钮立即更新；"
                   "只更新选中的、市场里有新版的插件。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/pluginautoupdater.png"
    plugin_version = "1.1.1"
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
        self._author_filters: List[str] = []
        self._target_plugins: List[str] = []
        self._author_manual = ""
        self._plugin_manual = ""
        self._dry_run = False
        self._include_self = False
        self._force_reinstall = False
        self._max_per_run = 10
        self._notify_mode = "issue"
        self._repo_url_override = ""

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

        # 兼容 1.0 的单选作者配置；下拉勾选与手动补充框合并后去重保序
        raw_authors = cfg.get("author_filters")
        if raw_authors is None:
            raw_authors = cfg.get("author_filter")
        self._author_manual = str(cfg.get("author_manual") or "")
        self._plugin_manual = str(cfg.get("plugin_manual") or "")
        self._author_filters = list(dict.fromkeys(
            updater.normalize_authors(raw_authors)
            + _split_manual(self._author_manual)))
        selected = cfg.get("target_plugins") or []
        if isinstance(selected, str):
            selected = [x.strip() for x in selected.split(",") if x.strip()]
        self._target_plugins = list(dict.fromkeys(
            [str(x).strip() for x in selected if str(x).strip()]
            + _split_manual(self._plugin_manual)))

        self._dry_run = bool(cfg.get("dry_run"))
        self._include_self = bool(cfg.get("include_self"))
        self._force_reinstall = bool(cfg.get("force_reinstall"))
        self._max_per_run = _clamp_int(cfg.get("max_per_run"), 10, 1, 100)
        self._notify_mode = str(cfg.get("notify_mode") or "issue")
        if self._notify_mode not in _NOTIFY_LABELS:
            self._notify_mode = "issue"
        self._repo_url_override = str(cfg.get("repo_url_override") or "").strip()

        self._log("INFO", f"配置已载入｜启用 {self._enabled}｜cron {self._cron}｜"
                          f"作者 {self._author_filters or '全部'}｜单次上限 {self._max_per_run}")

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
        # MP scheduler.py 用 **service["kwargs"] 展开给 APScheduler.add_job()：
        # 这里放 trigger 会与位置参数撞名，报
        # "BaseScheduler.add_job() got multiple values for argument 'trigger'"。
        # 且 MP 是 job["func"]() 无参调用，触发来源只能靠 run_once 的参数默认值。
        return [{
            "id": PLUGIN_ID,
            "name": "插件自动更新",
            "trigger": trigger,
            "func": self.run_once,
            "kwargs": {},
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

    def _apply_repo_override(self, plugins: List[updater.InstalledPlugin]) -> None:
        if self._repo_url_override:
            for plugin in plugins:
                plugin.repo_url = self._repo_url_override

    def _scan(self) -> Tuple[List[updater.InstalledPlugin], str]:
        installed, online, source = host.scan_plugins()
        plugins = updater.normalize_plugins(installed, online)
        self._apply_repo_override(plugins)
        return plugins, source

    def _form_plugins(self) -> List[updater.InstalledPlugin]:
        """配置页只认本机清单：先读缓存，缓存空了再读一次内存里的插件表。"""
        snapshot = self._scan_snapshot()
        cached = [updater.InstalledPlugin.from_dict(x)
                  for x in (snapshot.get("plugins") or []) if isinstance(x, dict)]
        if cached:
            return cached
        try:
            local = host.scan_local()
            plugins = updater.normalize_plugins(local, [])
            self._apply_repo_override(plugins)
            if plugins:
                self._save_scan(plugins, "本机清单")
            return plugins
        except Exception as err:  # noqa: BLE001 - 配置页不因为扫描失败而打不开
            self._log("WARN", f"读本机插件表失败｜{err}")
            return []

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

    def run_once(self, trigger: str = "定时",
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
            plugins, self._author_filters, self._target_plugins,
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
        props: Dict[str, Any] = {"model": model, "label": label}
        if hint:
            props.update({"hint": hint, "persistentHint": True})
        if placeholder:
            props["placeholder"] = placeholder
        if field_type == "number":
            props["type"] = "number"
        return {"component": "VTextField", "props": props}

    @staticmethod
    def _sw(model: str, label: str, hint: str = "") -> dict:
        props: Dict[str, Any] = {"model": model, "label": label}
        if hint:
            props.update({"hint": hint, "persistentHint": True})
        return {"component": "VSwitch", "props": props}

    @staticmethod
    def _sel(model: str, label: str, items: List[Dict[str, Any]],
             hint: str = "", multiple: bool = False) -> dict:
        props: Dict[str, Any] = {"model": model, "label": label, "items": items}
        if hint:
            props.update({"hint": hint, "persistentHint": True})
        if multiple:
            props.update({"multiple": True, "chips": True, "closableChips": True,
                          "clearable": True})
        return {"component": "VSelect", "props": props}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 只读本机插件表，不碰在线目录：设置页不能因为网络等待打不开
        plugins = self._form_plugins()
        author_items = updater.multi_author_items(plugins)
        plugin_items = updater.plugin_items(plugins)

        content: List[dict] = [
            {"component": "VRow", "content": [
                self._col(6, [self._sel(
                    "author_filters", "固定作者（可多选）", author_items,
                    "勾几位作者，就自动更新他们名下所有已安装插件（含以后新装的）；"
                    "与下方指定插件取并集", multiple=True)]),
                self._col(6, [self._sel(
                    "target_plugins", "指定插件（可多选）", plugin_items,
                    "额外指定要更新的插件；即使不属于上面选的作者也会更新，两者取并集",
                    multiple=True)]),
            ]},
            {"component": "VRow", "content": [
                self._col(6, [self._fld(
                    "author_manual", "手动补充作者（逗号分隔）",
                    "下拉里没有、或想额外固定的作者名；与上方勾选合并生效",
                    "BackspaceM,OtherAuthor")]),
                self._col(6, [self._fld(
                    "plugin_manual", "手动补充插件 ID（逗号分隔）",
                    "下拉里没列出的插件 ID；与上方勾选合并生效",
                    "SomePlugin,AnotherPlugin")]),
            ]},
            {"component": "VRow", "content": [
                self._col(4, [self._fld("cron", "定时执行（cron）", "", "0 5 * * *")]),
                self._col(3, [self._fld("max_per_run", "单次最多更新", "", "10", "number")]),
                self._col(5, [self._sel("notify_mode", "通知方式",
                                        [{"title": v, "value": k}
                                         for k, v in _NOTIFY_LABELS.items()])]),
            ]},
            {"component": "VRow", "content": [
                self._col(2, [self._sw("enabled", "启用")]),
                self._col(2, [self._sw("onlyonce", "保存后跑一次")]),
                self._col(2, [self._sw("dry_run", "试运行")]),
                self._col(3, [self._sw("include_self", "允许更新自己")]),
                self._col(3, [self._sw("force_reinstall", "强制重装")]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, [self._fld("repo_url_override", "来源仓库覆盖（可选）")]),
            ]},
        ]
        if plugins:
            content.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis mt-1"},
                "text": _esc(f"本机 {len(plugins)} 个插件，{len(author_items)} 位作者")})
        else:
            content.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis mt-1"},
                "text": "还没读到插件清单，回主界面点一下「刷新清单」再来勾。"})

        defaults: Dict[str, Any] = {
            "enabled": self._enabled,
            "onlyonce": False,
            "cron": self._cron,
            "author_filters": list(self._author_filters),
            "target_plugins": list(self._target_plugins),
            "author_manual": self._author_manual,
            "plugin_manual": self._plugin_manual,
            "dry_run": self._dry_run,
            "include_self": self._include_self,
            "force_reinstall": self._force_reinstall,
            "max_per_run": self._max_per_run,
            "notify_mode": self._notify_mode,
            "repo_url_override": self._repo_url_override,
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

    def get_page(self) -> List[dict]:
        """主界面只留按钮和一行状态；日志、记录、清单都在宿主日志和接口里看。"""
        report = dict(self.get_data(_KEY_REPORT) or {})
        snapshot = self._scan_snapshot()
        plugins = list(snapshot.get("plugins") or [])
        if report:
            summary = f"{report.get('finished') or ''}｜{report.get('message') or ''}"
        else:
            summary = "还没跑过"
        union_note = "（并集）" if self._author_filters and self._target_plugins else ""
        lines = [
            _esc(summary),
            _esc(f"本机 {len(plugins)} 个插件｜定时 {self._cron}｜"
                 f"{'已启用' if self._enabled else '没启用'}"),
            _esc(f"更新范围｜固定作者 {len(self._author_filters)} 位"
                 f"｜指定插件 {len(self._target_plugins)} 个{union_note}"),
        ]
        return [
            {"component": "VRow", "content": [
                self._col(12, [
                    self._action_button("刷新清单", "scan", "info", "mdi-refresh"),
                    self._action_button("立即运行", "run", "primary", "mdi-update",
                                        confirm=True),
                    self._action_button("试运行", "dryrun", "secondary", "mdi-play-circle-outline"),
                    self._action_button("清空记录", "clear", "warning", "mdi-delete-outline",
                                        confirm=True),
                ]),
            ]},
            {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
             "content": [
                 {"component": "div", "text": line} for line in lines
             ]},
        ]

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
             "methods": ["POST"], "summary": "兼容旧版：记录作者筛选值"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status,
             "methods": ["GET"], "summary": "查询插件状态与最近结果"},
            {"path": "/history", "auth": "bear", "endpoint": self.api_history,
             "methods": ["GET"], "summary": "查询更新记录"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空运行报告、记录与日志"},
            {"path": "/logs", "auth": "bear", "endpoint": self.api_logs,
             "methods": ["GET"], "summary": "查询运行日志"},
        ]

    def api_scan(self) -> Any:
        plugins, source = self._scan()
        self._save_scan(plugins, source)
        self._log("INFO", f"手动扫描｜{source}｜{len(plugins)} 个插件")
        return Response(success=bool(plugins),
                        message=(f"扫到 {len(plugins)} 个插件：{source}"
                                 if plugins else f"没读到插件清单：{source}"),
                        data={"count": len(plugins), "source": source,
                              "plugins": [p.to_dict() for p in plugins]})

    def api_run(self) -> Any:
        report = self._run_coroutine(self._api_run("手动", None))
        return Response(success=bool(report.get("ok")),
                        message=str(report.get("message") or ""), data=report)

    def api_dryrun(self) -> Any:
        report = self._run_coroutine(self._api_run("试运行", True))
        return Response(success=bool(report.get("ok")),
                        message=str(report.get("message") or ""), data=report)

    def api_filter(self, payload: Optional[dict] = _BODY_DEFAULT) -> Any:
        """保留给旧调用方；签名里不能有 **kwargs，否则 FastAPI 会当成必填参数报 422。"""
        author = ""
        if isinstance(payload, dict):
            author = str(payload.get("author") or "")
        elif isinstance(payload, str):
            author = payload
        elif payload is not None:
            author = str(getattr(payload, "author", "") or "")
        author = author or updater.ALL_AUTHORS
        return Response(success=True, message=f"已记下「{author}」",
                        data={"author": author})

    def api_status(self) -> Any:
        snapshot = self._scan_snapshot()
        return Response(success=True, data={
            "plugin": PLUGIN_ID,
            "enabled": self._enabled,
            "cron": self._cron,
            "author_filters": list(self._author_filters),
            "target_plugins": list(self._target_plugins),
            "author_manual": self._author_manual,
            "plugin_manual": self._plugin_manual,
            "dry_run": self._dry_run,
            "include_self": self._include_self,
            "force_reinstall": self._force_reinstall,
            "max_per_run": self._max_per_run,
            "notify_mode": self._notify_mode,
            "repo_url_override": self._repo_url_override,
            "scan": snapshot,
            "last_report": dict(self.get_data(_KEY_REPORT) or {}),
            "history": list(self.get_data(_KEY_HISTORY) or []),
            "logs": list(self.get_data(_KEY_LOGS) or [])[:30],
        })

    def api_history(self) -> Any:
        return Response(success=True, data={
            "history": list(self.get_data(_KEY_HISTORY) or [])[:_MAX_HISTORY]})

    def api_clear(self) -> Any:
        self.save_data(_KEY_REPORT, {})
        self.save_data(_KEY_HISTORY, [])
        self.save_data(_KEY_LOGS, [])
        logger.info(f"{_LOG_PREFIX}｜运行报告、更新记录和日志已清空")
        return Response(success=True, message="运行报告、更新记录和日志已清空")

    def api_logs(self) -> Any:
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
