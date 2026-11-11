"""RouterOS 站点智能分流 / DNS（B3）—— MoviePilot V3 插件主类。

从 MoviePilot 站点管理读出站点和 Tracker 域名，一次生成 static DNS 正向记录、
地址列表、mangle 连接/包标记、可选策略路由与 filter 放行，配合已有的 CF 优选 IP
一起用。站点增删按标签增量同步，确认后批量下发。

只新增带 ``MP-B3[<站点id>:<用途>]`` 标签的条目，同标签幂等跳过；回滚只删本次
新建条目；部署前留系统备份，失败逆序自动回滚；高危动作需显式放行，试运行零写入。
连接参数可从「RouterOS 连接中心」下拉带入，不安装时手填行为不变。
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

from . import engine, planner, roslink, sites
from .clients import RouterOSConfigClient, build_config_client
from .planner import (DEFAULT_PREFIX, RISK_COLORS, RISK_LABELS, actions_from_plan,
                      md_safe, now_text)

PLUGIN_ID = "RouterOSSiteRouting"
_LOG_PREFIX = "ROS站点分流"
_MAX_LOGS = 200
_MAX_SNAPSHOTS = 10

_KEY_PLAN = "b3_plan"
_KEY_LAST_DEPLOY = "b3_last_deploy"
_KEY_ROLLBACK = "b3_rollback_entries"
_KEY_LOGS = "b3_logs"
_KEY_CONN = "b3_conn"
_KEY_SNAPSHOTS = "b3_snapshots"
_KEY_CLEANUP = "b3_cleanup"

_NOTIFY_LABELS = {"issue": "仅异常/失败", "all": "每次部署", "none": "关闭"}


def _esc(value: Any) -> str:
    """动态文本转义：防 XSS，并避免 [xxx] 被前端当 Markdown 参考式链接渲染。"""
    return (_html.escape(str(value if value is not None else ""), quote=False)
            .replace("[", "［").replace("]", "］"))


def _cmd_esc(value: Any) -> str:
    return md_safe(_html.escape(str(value if value is not None else ""), quote=False),
                   keep_star=True)


class RouterOSSiteRouting(_PluginBase):
    # 类级元数据一律用字符串字面量：官方版本检查以 AST 提取，常量引用会被判为「未声明」
    plugin_name = "站点分流"
    plugin_desc = ("从 MoviePilot 站点管理读出站点与 Tracker 域名，自动生成 static DNS、"
                   "地址列表、mangle 标记与策略路由，配合 CF 优选 IP；站点增删按标签增量"
                   "同步，预览中文含义后批量写入，只新增带 MP-B3 标签的条目。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/routerossiterouting.png"
    plugin_version = "1.0.2"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "routerossiterouting_"
    plugin_color = "#1565C0"
    system_version = ">=3.0.0"
    plugin_order = 54

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cfg: Dict[str, Any] = {}
        self._enabled = True
        self._onlyonce = False
        self._address = ""
        self._port = 443
        self._use_ssl = True
        self._verify = False
        self._username = ""
        self._password = ""
        self._timeout = 10
        self._selected_sites: Any = []
        self._sites_manual = ""
        self._site_configs: Dict[str, Any] = {}
        self._sites: List[sites.Site] = []
        self._site_errors: List[str] = []
        self._dns_target_ip = ""
        self._address_list_name = "mp-pt-sites"
        self._conn_mark = "mp-pt-conn"
        self._packet_mark = "mp-pt-pkt"
        self._routing_table = "mp-pt-route"
        self._gateway = ""
        self._enable_mangle = False
        self._enable_policy_route = False
        self._enable_filter_accept = False
        self._allow_high_risk = False
        self._cleanup_stale = False
        self._prefix = DEFAULT_PREFIX
        self._dry_run = False
        self._auto_backup = True
        self._require_backup = True
        self._auto_rollback = True
        self._notify_mode = "issue"
        self._cron = "0 5 * * *"
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

        self._enabled = bool(cfg.get("enabled", True))
        self._onlyonce = bool(cfg.get("onlyonce"))
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

        self._selected_sites = cfg.get("selected_sites") or []
        self._sites_manual = str(cfg.get("sites_manual") or "")
        self._site_configs = sites.load_site_configs()

        self._dns_target_ip = str(cfg.get("dns_target_ip") or "").strip()
        self._address_list_name = str(cfg.get("address_list_name") or "mp-pt-sites").strip()
        self._conn_mark = str(cfg.get("conn_mark") or "mp-pt-conn").strip()
        self._packet_mark = str(cfg.get("packet_mark") or "mp-pt-pkt").strip()
        self._routing_table = str(cfg.get("routing_table") or "mp-pt-route").strip()
        self._gateway = str(cfg.get("gateway") or "").strip()
        self._enable_mangle = bool(cfg.get("enable_mangle"))
        self._enable_policy_route = bool(cfg.get("enable_policy_route"))
        self._enable_filter_accept = bool(cfg.get("enable_filter_accept"))
        self._allow_high_risk = bool(cfg.get("allow_high_risk"))
        self._cleanup_stale = bool(cfg.get("cleanup_stale", False))
        self._prefix = str(cfg.get("comment_prefix") or DEFAULT_PREFIX).strip() or DEFAULT_PREFIX
        self._dry_run = bool(cfg.get("dry_run"))
        self._auto_backup = bool(cfg.get("auto_backup", True))
        self._require_backup = bool(cfg.get("require_backup", True))
        self._auto_rollback = bool(cfg.get("auto_rollback", True))
        self._notify_mode = str(cfg.get("notify_mode") or "issue")
        self._cron = str(cfg.get("cron") or "0 5 * * *").strip()
        try:
            self._display_limit = max(10, min(200, int(cfg.get("display_limit") or 60)))
        except (TypeError, ValueError):
            self._display_limit = 60

        self._rediscover()
        plan = self._make_plan()
        self.save_data(_KEY_PLAN, plan)
        self._log("INFO", f"配置已载入：站点 {len(self._sites)} 个，"
                          f"待确认动作 {len(plan.get('actions') or [])} 条"
                          + ("；校验未通过" if plan.get("blocked") else ""))
        if self._onlyonce and self._enabled:
            self._log("INFO", "已勾选仅运行一次，加载后先同步一次")
            try:
                self.sync_once()
            except Exception as err:  # noqa: BLE001
                self._log("WARN", f"仅运行一次同步没成功：{err}")

    def _rediscover(self) -> None:
        self._sites, self._site_errors = sites.discover_sites(
            self._selected_sites, self._sites_manual, self._site_configs)
        for err in self._site_errors:
            self._log("WARN", f"站点发现：{err}")

    def _planner_options(self) -> Dict[str, Any]:
        return {
            "dns_target_ip": self._dns_target_ip,
            "address_list_name": self._address_list_name,
            "conn_mark": self._conn_mark, "packet_mark": self._packet_mark,
            "routing_table": self._routing_table, "gateway": self._gateway,
            "enable_mangle": self._enable_mangle,
            "enable_policy_route": self._enable_policy_route,
            "enable_filter_accept": self._enable_filter_accept,
            "cron": self._cron,
        }

    def _make_plan(self, existing_domains: Any = ()) -> Dict[str, Any]:
        plan = planner.render_plan(self._sites, self._planner_options(),
                                   self._prefix, self._allow_high_risk,
                                   existing_domains)
        if self._site_errors:
            plan["errors"] = list(plan.get("errors") or []) + list(self._site_errors)
            plan["blocked"] = True
        return plan

    def get_state(self) -> bool:
        return self._enabled

    def _scheduler_sdk(self) -> Any:
        """在几个可能的宿主位置里找带 remove_plugin_once_job 的调度对象。"""
        candidates: List[Any] = [getattr(self, "scheduler_sdk", None)]
        try:
            from app.sdk import scheduler as mod  # type: ignore
            candidates.append(mod)
        except Exception:  # noqa: BLE001
            pass
        try:
            from app import scheduler as mod  # type: ignore
            candidates.append(mod)
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.scheduler import scheduler_sdk as mod  # type: ignore
            candidates.append(mod)
        except Exception:  # noqa: BLE001
            pass
        for mod in candidates:
            if callable(getattr(mod, "remove_plugin_once_job", None)):
                return mod
        return None

    def stop_service(self) -> None:
        """停服时移除一次任务；宿主没有 scheduler sdk 就静默跳过。"""
        sdk = self._scheduler_sdk()
        remover = getattr(sdk, "remove_plugin_once_job", None)
        if not callable(remover):
            remover = getattr(self, "remove_plugin_once_job", None)
        if callable(remover):
            try:
                remover(PLUGIN_ID)
            except Exception:  # noqa: BLE001
                pass

    def get_service(self) -> List[Dict[str, Any]]:
        """enabled 且 cron 合法时注册一个定时增量同步服务。"""
        if not self._enabled:
            return []
        ok, cron = planner.validate_cron(self._cron)
        if not ok:
            return []
        try:
            from apscheduler.triggers.cron import CronTrigger  # type: ignore
            trigger = CronTrigger.from_crontab(cron)
        except Exception as err:  # noqa: BLE001
            self._log("WARN", f"定时服务没注册上，本机缺 apscheduler：{err}")
            return []
        return [{
            "id": f"{PLUGIN_ID}_sync",
            "name": "PT 站点智能分流定时增量同步",
            "trigger": trigger,
            "func": self.sync_once,
            "kwargs": {},
        }]

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
            "backup_name": "mp-b3-" + time.strftime("%Y%m%d-%H%M%S"),
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
    # 现状读取 / 增量清理
    # ------------------------------------------------------------------

    def _collect_existing(self, client: Any, http: Any) -> Tuple[
            Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], List[str]]:
        """读全部白名单分区，返回 (分区行, 扁平行, 非本插件的已存在 DNS 域名)。"""
        by_section: Dict[str, List[Dict[str, Any]]] = {}
        for section in planner.SECTIONS:
            try:
                rows = self._run_coroutine(client.list_section(section, client=http))
            except Exception:  # noqa: BLE001
                rows = None
            by_section[section] = list(rows or [])
        flat = [row for rows in by_section.values() for row in rows]
        marker = self._prefix + "["
        existing_domains: List[str] = []
        for row in by_section.get(planner.SEC_DNS, []):
            if marker in str(row.get("comment") or ""):
                continue
            name = str(row.get("name") or "")
            if name:
                existing_domains.append(name)
        return by_section, flat, existing_domains

    @staticmethod
    def _tagged_row_id(rows: Any, tag: str, prefix: str) -> str:
        """只认带本插件标签的行，手工规则即便备注里提到标签也不碰。"""
        marker = prefix + "["
        for row in rows or []:
            comment = str(row.get("comment") or "")
            if marker in comment and tag in comment:
                rid = str(row.get(".id") or row.get("id") or "")
                if rid:
                    return rid
        return ""

    def _do_cleanup(self) -> Dict[str, Any]:
        """删除失效标签条目：先备份，只删带标签的，结果记入报告。"""
        client, http = self._make_client()
        report: Dict[str, Any] = {
            "ok": False, "message": "", "created": now_text(),
            "stale": [], "deleted": 0, "errors": [], "details": [],
            "backup": {"ok": False, "message": "未执行"},
            "dry_run": self._dry_run,
        }
        if client is None:
            report["message"] = "没配 RouterOS 地址或网络模块不可用"
            self.save_data(_KEY_CLEANUP, report)
            return report
        try:
            by_section, flat, existing_domains = self._collect_existing(client, http)
            self._rediscover()
            plan = self._make_plan(existing_domains)
            if plan.get("blocked") or not plan.get("actions"):
                report["message"] = "当前站点清单校验没通过或没有动作，先修好再清理"
                self.save_data(_KEY_CLEANUP, report)
                return report
            stale = planner.stale_tags(flat, planner.plan_tags(plan), self._prefix)
            report["stale"] = stale
            if not stale:
                report["ok"] = True
                report["message"] = "没有失效条目，不用清理"
                self.save_data(_KEY_CLEANUP, report)
                return report

            # 删除前必须留备份，失败默认中止
            bname = "mp-b3-cleanup-" + time.strftime("%Y%m%d-%H%M%S")
            bok, bmsg = self._run_coroutine(client.save_backup(bname, client=http))
            report["backup"] = {"ok": bok, "message": bmsg}
            self._log("INFO" if bok else "WARN", f"【清理前备份】{bmsg}")
            if not bok and self._require_backup:
                report["message"] = "清理前备份失败，已中止删除：" + bmsg
                self.save_data(_KEY_CLEANUP, report)
                return report

            for tag in stale:
                hit = False
                for section, rows in by_section.items():
                    rid = self._tagged_row_id(rows, tag, self._prefix)
                    if not rid:
                        continue
                    hit = True
                    if self._dry_run:
                        ok, msg = True, f"试运行：将删除 {tag}"
                    else:
                        ok, msg = self._run_coroutine(
                            client.delete(section, rid, client=http))
                    if ok:
                        report["deleted"] += 1
                        rows[:] = [r for r in rows
                                   if str(r.get(".id") or r.get("id") or "") != rid]
                    else:
                        report["errors"].append(f"{tag}：{msg}")
                    report["details"].append({"tag": tag, "section": section,
                                              "ok": ok, "message": msg})
                    self._log("INFO" if ok else "WARN", f"【清理】{msg}")
                    break
                if not hit:
                    report["details"].append({"tag": tag, "section": "",
                                              "ok": True, "message": "标签已不在设备上"})
            report["ok"] = not report["errors"]
            report["message"] = (f"清理完成：失效标签 {len(stale)} 个，"
                                 f"删除 {report['deleted']} 条，"
                                 f"失败 {len(report['errors'])} 条"
                                 + ("（试运行，未实际删除）" if self._dry_run else ""))
            self.save_data(_KEY_CLEANUP, report)
            self._log("INFO", f"失效条目清理：{report['message']}")
            self._send_message("RouterOS 站点分流清理", report["message"])
            return report
        except Exception as err:  # noqa: BLE001
            report["message"] = f"清理异常：{err}"
            report["errors"].append(str(err))
            self.save_data(_KEY_CLEANUP, report)
            self._log("ERROR", report["message"])
            return report

    # ------------------------------------------------------------------
    # 定时增量同步
    # ------------------------------------------------------------------

    def sync_once(self) -> Dict[str, Any]:
        """重新发现站点 → 生成计划 → 比对现状 → 无校验错误才部署 → 记录 / 通知。

        异常只记日志，不向上抛出（定时任务里抛异常会刷屏）。
        """
        try:
            self._rediscover()
            client, http = self._make_client()
            existing_domains: List[str] = []
            if client is not None:
                _, _, existing_domains = self._collect_existing(client, http)
            plan = self._make_plan(existing_domains)
            if plan.get("blocked") or plan.get("errors"):
                msg = "增量同步跳过：校验没通过，" + "；".join(
                    str(e) for e in (plan.get("errors") or [])[:3])
                self._log("WARN", msg)
                self.save_data(_KEY_PLAN, plan)
                return {"ok": False, "message": msg}
            if client is None:
                msg = "增量同步跳过：没配 RouterOS 地址或网络模块不可用"
                self._log("WARN", msg)
                self.save_data(_KEY_PLAN, plan)
                return {"ok": False, "message": msg}
            plan = self._run_coroutine(
                engine.refresh_plan(client, http, plan, log=self._log))
            self.save_data(_KEY_PLAN, plan)
            if not plan.get("actions"):
                self._log("INFO", "增量同步：当前没有待写入动作")
                return {"ok": True, "message": "没有待写入动作"}
            report = self._run_coroutine(engine.deploy_plan(
                client, http, plan, self._options(),
                log=self._log, snapshot_saver=self._save_snapshot))
            self.save_data(_KEY_LAST_DEPLOY, report)
            self.save_data(_KEY_ROLLBACK, report.get("created") or [])
            self._notify_deploy(report, "RouterOS 站点分流同步结果")
            self._log("INFO", f"增量同步：{report.get('message')}")
            if self._cleanup_stale and report.get("ok"):
                cleanup = self._do_cleanup()
                self._log("INFO", f"失效条目清理：{cleanup.get('message')}")
            return report
        except Exception as err:  # noqa: BLE001
            self._log("ERROR", f"增量同步异常：{err}")
            return {"ok": False, "message": f"同步异常：{err}"}

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

    def _site_items(self) -> List[Dict[str, str]]:
        return sites.site_items(self._site_configs)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        site_items = self._site_items()
        conn_items = roslink.connection_items()
        content: List[dict] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                              "class": "mb-4"},
             "text": "读 MP 站点管理里已配好的站点和 Tracker 域名，自动写 static DNS "
                                 "和地址列表，配合 CF 优选 IP 一起用。只加带 MP-B3 标签的条目，"
                                 "写之前会留备份，中途失败自动回滚。"},
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
            self._section_title("选站点"),
            {"component": "VRow", "content": [
                self._col(12, [self._sel("selected_sites", "已配置站点（可多选）",
                                         site_items or [{"title": "没读到站点，用下面的手动补充", "value": ""}],
                                         "下拉里是 MP 站点管理的记录，主域名和 Tracker 域名自动读出",
                                         multiple=True)]),
            ]},
            {"component": "VRow", "content": [
                self._col(12, [{"component": "VTextarea", "props": {
                    "model": "sites_manual", "label": "手动补充站点（每行一个）", "rows": 4,
                    "autoGrow": True, "persistentHint": True,
                    "hint": "名称|主域名|Tracker域名(多个用逗号分隔)|备注；同名会覆盖上面的下拉项",
                    "placeholder": "馒头|https://pt.example.com|tracker.example.com,api.example.com|备用\n"
                                   "猫站|pt.cat.com|tracker.cat.com|"}}]),
            ]},
            self._section_title("解析与标记"),
            {"component": "VRow", "content": [
                self._col(3, [self._fld("dns_target_ip", "解析目标 IP",
                                        "static DNS 指到哪，通常填 CF 优选出来的 IP", "1.1.1.1")]),
                self._col(3, [self._fld("address_list_name", "地址列表名", "", "mp-pt-sites")]),
                self._col(3, [self._fld("conn_mark", "连接标记名", "", "mp-pt-conn")]),
                self._col(3, [self._fld("packet_mark", "包标记名", "", "mp-pt-pkt")]),
            ]},
            {"component": "VRow", "content": [
                self._col(3, [self._fld("routing_table", "路由表名", "", "mp-pt-route")]),
                self._col(3, [self._fld("gateway", "策略路由网关",
                                        "开了策略路由才要填", "192.168.88.2")]),
                self._col(2, [self._sw("enable_mangle", "生成 mangle 标记",
                                       "连接标记 + 包标记")]),
                self._col(2, [self._sw("enable_policy_route", "生成策略路由",
                                       "要填网关")]),
                self._col(2, [self._sw("enable_filter_accept", "生成 filter 放行",
                                       "排在兜底 drop 之前")]),
            ]},
            self._section_title("写入与定时"),
            {"component": "VRow", "content": [
                self._col(3, [self._sw("allow_high_risk", "允许高风险动作",
                                       "mangle / 策略路由要勾这个才生成")]),
                self._col(3, [self._sw("cleanup_stale", "清理失效站点条目",
                                       "默认关；开了才允许删除旧标签条目")]),
                self._col(3, [self._fld("comment_prefix", "管理标签前缀", "", "MP-B3")]),
                self._col(3, [self._sel("notify_mode", "通知方式",
                                        [{"title": v, "value": k} for k, v in _NOTIFY_LABELS.items()])]),
            ]},
            {"component": "VRow", "content": [
                self._col(2, [self._sw("dry_run", "试运行（不写入）", "只走流程，核对预览用")]),
                self._col(2, [self._sw("auto_backup", "部署前自动备份", "在 RouterOS 上留 .backup")]),
                self._col(2, [self._sw("require_backup", "备份失败必须中止", "备份没成功就别往下写")]),
                self._col(2, [self._sw("auto_rollback", "失败自动回滚", "逆序删掉本次新建条目")]),
                self._col(2, [self._sw("enabled", "启用插件", "关掉就不注册定时服务")]),
                self._col(2, [self._sw("onlyonce", "加载后同步一次", "下次加载时跑一遍")]),
            ]},
            {"component": "VRow", "content": [
                self._col(4, [self._fld("cron", "定时增量同步（cron）",
                                        "5 位，例如 0 5 * * *", "0 5 * * *")]),
                self._col(4, [self._fld("display_limit", "详情页最多显示行数",
                                        "", "60", "number")]),
            ]},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                                              "class": "mt-3"},
             "text": "保存后到详情页点「生成 / 刷新预览」，核对完再点「确认一键部署」。"
                                 "开了 mangle 或策略路由，还要勾「允许高风险动作」，网关别写错。"},
            {"component": "VAlert", "props": {"type": "secondary", "variant": "text",
                                              "class": "mt-2"},
             "text": "RouterOS v6 没有 REST，这里只认 v7。规则顺序以设备上现有链为准，"
                                 "跟 CF 优选 IP 插件共用 static DNS 时，同域名不会重复新增。"},
        ]
        defaults: Dict[str, Any] = {
            "enabled": True, "onlyonce": False,
            "ros_connection": "", "router_address": "", "router_port": 443,
            "router_ssl": True, "router_username": "", "router_password": "",
            "timeout": 10, "verify": False,
            "selected_sites": [], "sites_manual": "",
            "dns_target_ip": "", "address_list_name": "mp-pt-sites",
            "conn_mark": "mp-pt-conn", "packet_mark": "mp-pt-pkt",
            "routing_table": "mp-pt-route", "gateway": "",
            "enable_mangle": False, "enable_policy_route": False,
            "enable_filter_accept": False, "allow_high_risk": False,
            "cleanup_stale": False, "comment_prefix": DEFAULT_PREFIX,
            "dry_run": False, "auto_backup": True, "require_backup": True,
            "auto_rollback": True, "notify_mode": "issue",
            "cron": "0 5 * * *", "display_limit": 60,
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
        site_rows = list(plan.get("sites") or [])
        rollback_entries = list(self.get_data(_KEY_ROLLBACK) or [])
        summary = dict(plan.get("summary") or {})
        stale = list(plan.get("stale_tags") or [])
        limit = self._display_limit

        content: List[dict] = [
            {"component": "VRow", "content": [
                self._stat_card("站点", f"{len(site_rows)} 个",
                                "、".join(str(s.get("name")) for s in site_rows[:3]) or "还没选",
                                "primary"),
                self._stat_card("待写入动作", f"{len(actions)} 条",
                                f"待写入 {summary.get('to_write', len(actions))} 条 · "
                                f"涉及 {summary.get('sections', 0)} 个分区", "info"),
                self._stat_card("涉及分区", f"{summary.get('sections', 0)} 个",
                                "、".join(str(planner.SECTIONS.get(s, {}).get("label") or s)
                                          for s in (plan.get("sections") or [])[:2]) or "无",
                                "secondary"),
                self._stat_card("高危动作", f"{summary.get('high', 0)} 条",
                                "需显式放行才生成", "error" if summary.get("high") else "success"),
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mt-3"}, "content": [
                {"component": "VCardTitle", "text": "操作"},
                {"component": "VCardText", "content": [
                    self._action_button("生成 / 刷新预览", "preview", "primary",
                                        "mdi-file-search-outline"),
                    self._action_button("确认一键部署", "apply", "error",
                                        "mdi-rocket-launch-outline", confirm=True),
                    self._action_button("清理失效站点条目", "cleanup", "warning",
                                        "mdi-tag-remove-outline", confirm=True),
                    self._action_button("回滚最近一次", "rollback", "error",
                                        "mdi-undo-variant", confirm=True),
                    self._action_button("测试连接", "test", "info", "mdi-router-wireless"),
                    self._action_button("仅备份", "backup", "secondary", "mdi-backup-restore"),
                    self._action_button("清空计划", "clear", "warning", "mdi-delete-outline"),
                    self._action_button("立即同步一次", "sync", "success", "mdi-sync"),
                    self._action_button("刷新状态", "status", "info", "mdi-refresh", method="get"),
                ]},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": ("当前是试运行，所有写入都不生效。" if self._dry_run else
                              "部署前会在 RouterOS 留备份，失败时逆序回滚本次新建的条目。")
                              + (f" 可回滚条目 {len(rollback_entries)} 条。" if rollback_entries else "")
                              + (f" 失效标签 {len(stale)} 个。" if stale else "")},
                ]},
            ]},
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
        if stale:
            content.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-2",
                "title": "有失效标签"},
                "text": f"设备上还有 {len(stale)} 个带 {_esc(self._prefix)} 标签、"
                                    f"但当前站点清单里没有的条目。要清掉先在设置页打开清理开关。"})


        rows = []
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
                                          {"component": "th", "text": "站点"},
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
                "text": "在设置页选好站点保存，回来点「生成 / 刷新预览」。"})
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
            {"path": "/sites", "auth": "bear", "endpoint": self.api_sites,
             "methods": ["GET"], "summary": "查询站点下拉与当前站点清单"},
            {"path": "/preview", "auth": "bear", "endpoint": self.api_preview,
             "methods": ["POST"], "summary": "生成 / 刷新预览（比对现状）"},
            {"path": "/plan", "auth": "bear", "endpoint": self.api_plan,
             "methods": ["GET"], "summary": "查询待确认部署计划"},
            {"path": "/apply", "auth": "bear", "endpoint": self.api_apply,
             "methods": ["POST"], "summary": "确认并批量下发（含备份与失败回滚）"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空待确认计划"},
            {"path": "/rollback", "auth": "bear", "endpoint": self.api_rollback,
             "methods": ["POST"], "summary": "回滚最近一次部署新建的条目"},
            {"path": "/cleanup", "auth": "bear", "endpoint": self.api_cleanup,
             "methods": ["POST"], "summary": "清理失效站点标签条目"},
            {"path": "/backup", "auth": "bear", "endpoint": self.api_backup,
             "methods": ["POST"], "summary": "仅创建 RouterOS 备份与 MP 快照"},
            {"path": "/test", "auth": "bear", "endpoint": self.api_test,
             "methods": ["POST"], "summary": "测试 RouterOS 连接"},
            {"path": "/sync", "auth": "bear", "endpoint": self.api_sync,
             "methods": ["POST"], "summary": "立即执行一次增量同步"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status,
             "methods": ["GET"], "summary": "查询插件状态与最近结果"},
            {"path": "/logs", "auth": "bear", "endpoint": self.api_logs,
             "methods": ["GET"], "summary": "查询运行日志"},
        ]

    def api_sites(self, _=None) -> Any:
        return Response(success=True, data={
            "items": self._site_items(),
            "sites": [s.to_dict() for s in self._sites],
            "errors": list(self._site_errors),
        })

    def api_preview(self, _=None) -> Any:
        self._rediscover()
        client, http = self._make_client()
        flat: List[Dict[str, Any]] = []
        existing_domains: List[str] = []
        if client is not None:
            _, flat, existing_domains = self._collect_existing(client, http)
        plan = self._make_plan(existing_domains)
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
            plan["stale_tags"] = planner.stale_tags(flat, planner.plan_tags(plan),
                                                    self._prefix)
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
            return Response(success=False, message="没有可部署的动作：请到设置页选站点并保存")
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
        self._notify_deploy(report, "RouterOS 站点分流部署结果")
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
        self._send_message("RouterOS 站点分流回滚", str(rb.get("message") or ""))
        return Response(success=not rb.get("fail_count"),
                        message=str(rb.get("message") or ""), data=rb)

    def api_cleanup(self, _=None) -> Any:
        if not self._cleanup_stale:
            return Response(success=False,
                            message="清理开关没开：先到设置页勾选「清理失效站点条目」")
        report = self._do_cleanup()
        return Response(success=bool(report.get("ok")),
                        message=str(report.get("message") or ""), data=report)

    def api_sync(self, _=None) -> Any:
        report = self.sync_once()
        return Response(success=bool(report.get("ok")),
                        message=str(report.get("message") or "已触发一次增量同步"),
                        data=report)

    def api_backup(self, _=None) -> Any:
        client, http = self._make_client()
        if client is None:
            return Response(success=False, message="没配 RouterOS 地址或网络模块不可用")
        ok, msg = self._run_coroutine(client.check(client=http))
        if not ok:
            return Response(success=False, message=msg)
        bname = "mp-b3-manual-" + time.strftime("%Y%m%d-%H%M%S")
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
            "enabled": self._enabled, "onlyonce": self._onlyonce,
            "router": self._address, "port": self._port, "ssl": self._use_ssl,
            "username": self._username,
            "dry_run": self._dry_run, "allow_high_risk": self._allow_high_risk,
            "cleanup_stale": self._cleanup_stale, "cron": self._cron,
            "auto_backup": self._auto_backup, "auto_rollback": self._auto_rollback,
            "sites": [s.to_dict() for s in self._sites],
            "site_errors": list(self._site_errors),
            "prefix": self._prefix, "plan": self._plan(),
            "last_deploy": dict(self.get_data(_KEY_LAST_DEPLOY) or {}),
            "cleanup": dict(self.get_data(_KEY_CLEANUP) or {}),
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
            {"cmd": "/b3_preview", "event": EventType.PluginAction,
             "desc": "生成 RouterOS 站点分流预览", "category": "MoviePilot"},
            {"cmd": "/b3_apply", "event": EventType.PluginAction,
             "desc": "确认执行 RouterOS 站点分流部署", "category": "MoviePilot"},
            {"cmd": "/b3_sync", "event": EventType.PluginAction,
             "desc": "立即执行一次站点增量同步", "category": "MoviePilot"},
            {"cmd": "/b3_cleanup", "event": EventType.PluginAction,
             "desc": "清理失效站点标签条目", "category": "MoviePilot"},
            {"cmd": "/b3_rollback", "event": EventType.PluginAction,
             "desc": "回滚最近一次站点分流部署", "category": "MoviePilot"},
            {"cmd": "/b3_status", "event": EventType.PluginAction,
             "desc": "查看站点分流计划与最近结果", "category": "MoviePilot"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Any) -> None:
        if not event or not getattr(event, "event_data", None):
            return
        data = event.event_data or {}
        cmd = str(data.get("cmd") or "").lower()
        handlers = {
            "/b3_preview": self.api_preview,
            "/b3_apply": self.api_apply,
            "/b3_sync": self.api_sync,
            "/b3_cleanup": self.api_cleanup,
            "/b3_rollback": self.api_rollback,
            "/b3_status": self.api_status,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return
        result = handler()
        text = f"{getattr(result, 'message', '') or ''}"
        if cmd.startswith("/b3_status"):
            plan = self._plan()
            text = (f"站点 {len(plan.get('sites') or [])} 个，"
                    f"待确认 {len(plan.get('actions') or [])} 条动作；"
                    f"最近部署：{(self.get_data(_KEY_LAST_DEPLOY) or {}).get('message') or '无'}")
        try:
            self.post_message(title="RouterOS 站点分流", text=_esc(text),
                              channel=data.get("channel"), userid=data.get("userid"),
                              **({"mtype": _PLUGIN_MTYPE} if _PLUGIN_MTYPE is not None else {}))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{_LOG_PREFIX}：远程命令回复失败 {err}")
