"""MP 备份迁移 —— 把 MoviePilot 能读到的状态打包存档，也能预览后导入回来。"""

from __future__ import annotations

import datetime as _dt
import html
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

try:
    from fastapi import Body

    _BODY_DEFAULT = Body(default=None)
except Exception:  # noqa: BLE001
    _BODY_DEFAULT = None  # type: ignore[assignment]

try:
    from app.sdk.plugin import _PluginBase
except Exception:  # noqa: BLE001
    class _PluginBase:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._data: Dict[str, Any] = {}

from . import backup, renderer, restore


class Response(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None


class MPBackup(_PluginBase):
    # 元数据一律字符串字面量，官方校验脚本用 AST 直接取值
    plugin_name = "MP 备份迁移"
    plugin_desc = (
        "把 MoviePilot 当前能读到的用户、订阅、订阅历史、站点、服务、系统配置、插件清单与数据、"
        "下载和整理历史打包成一份 zip 快照，存在插件数据目录里按份数轮转。可以对比两份快照，"
        "也能生成恢复清单。用户、订阅记录、插件、站点、下载和整理历史这些都支持预览后导入恢复，"
        "导入默认试运行，确认后才写回。敏感字段默认打码，站点凭据可按需含明文导出。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/mpbackup.png"
    plugin_version = "1.1.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "mpbackup_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 95

    CLEAN_CONFIRM = backup.CLEAN_CONFIRM

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._cron = backup.DEFAULT_CRON
        self._keep = 7
        self._backup_dir = ""
        self._sections = list(backup.DEFAULT_SECTIONS)
        self._notify = True
        self._include_secrets = False
        self._strategy = "skip"
        self._cfg: Dict[str, Any] = {}
        self._last_diff: Optional[Dict[str, Any]] = None
        self._last_guide: Optional[str] = None
        self._last_preview: Optional[Dict[str, Any]] = None
        self._last_restore: Optional[Dict[str, Any]] = None
        self._import_guide: Optional[str] = None
        # 页面渲染期只读这份缓存，不碰宿主 SDK、文件和网络
        self._cache: Dict[str, Any] = self._empty_state()

    def _empty_state(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "offline": True,
            "generated_at": "—",
            "backup_dir": "",
            "cron": self._cron,
            "cron_label": backup.cron_label(self._cron),
            "keep": self._keep,
            "sections": list(self._sections),
            "snapshots": [],
            "total_size": 0,
            "total_size_text": "—",
            "sources": {},
            "contains_secrets": False,
            "diff": None,
            "diff_a": "",
            "diff_b": "",
            "guide": self._last_guide,
            "preview": self._last_preview,
            "restore": self._last_restore,
            "import_guide": self._import_guide,
        }

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._cfg = cfg
        self._enabled = bool(cfg.get("enabled", True))
        self._cron = backup.clean_cron(cfg.get("schedule"))
        self._keep = backup.clean_keep(cfg.get("keep"))
        self._backup_dir = str(cfg.get("backup_dir") or "")
        self._sections = backup.clean_sections(cfg.get("sections"))
        self._notify = bool(cfg.get("notify", True))
        self._include_secrets = bool(cfg.get("include_secrets", False))
        self._strategy = "overwrite" if cfg.get("import_strategy") == "overwrite" else "skip"
        self._last_diff = None
        self._last_guide = None
        self._last_preview = None
        self._last_restore = None
        self._import_guide = None
        self._cache = self._empty_state()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        self._last_diff = None
        self._last_guide = None
        self._last_preview = None
        self._last_restore = None
        self._import_guide = None

    def close(self) -> None:
        self.stop_service()

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        try:
            return self._build_form()
        except Exception:  # noqa: BLE001
            return ([{"component": "VSwitch",
                      "props": {"model": "enabled", "label": "启用插件"}}],
                    {"enabled": True})

    def _build_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        defaults: Dict[str, Any] = {
            "enabled": True,
            "backup_dir": "",
            "keep": 7,
            "schedule": backup.DEFAULT_CRON,
            "sections": list(backup.DEFAULT_SECTIONS),
            "notify": True,
            "include_secrets": False,
            "import_strategy": "skip",
        }
        cron_items = [{"title": label, "value": cron} for cron, label in backup.CRON_CHOICES]
        section_items = [{"title": label, "value": key} for key, label in backup.SECTION_OPTIONS]
        form: List[Dict[str, Any]] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
             "text": "快照默认脱敏，只导出不写回。想导入恢复用详情页的「导入迁移」，"
                     "默认试运行，确认后才写库；含凭据的快照含明文，别外传。"},
            {"component": "VSwitch", "props": {
                "model": "enabled", "label": "启用插件", "color": "primary",
                "hint": "关掉以后定时任务和按钮都不再生成快照",
            }},
            {"component": "VTextField", "props": {
                "model": "backup_dir", "label": "备份目录",
                "placeholder": "留空用插件数据目录 /backups", "clearable": True,
                "hint": "想存到别的挂载目录就填绝对路径，留空即可", "persistentHint": True,
            }},
            {"component": "VSelect", "props": {
                "model": "keep", "label": "保留份数",
                "items": [
                    {"title": "3 份", "value": 3},
                    {"title": "7 份（推荐）", "value": 7},
                    {"title": "15 份", "value": 15},
                    {"title": "30 份", "value": 30},
                ],
                "hint": "只清理本插件生成的旧 zip，别的文件不动",
            }},
            {"component": "VSelect", "props": {
                "model": "schedule", "label": "每天执行时间", "items": cron_items,
                "hint": "到点自动备份，跑完发一条结果通知",
            }},
            {"component": "VSelect", "props": {
                "model": "sections", "label": "备份内容", "multiple": True,
                "items": section_items, "hint": "默认全开；下载和整理历史各最多 5000 条",
            }},
            {"component": "VSwitch", "props": {
                "model": "notify", "label": "结果通知", "color": "primary",
                "hint": "备份成功发一条；失败不管开关都会告警",
            }},
            {"component": "VSwitch", "props": {
                "model": "include_secrets", "label": "导出站点明文凭据", "color": "warning",
                "hint": "打开会把站点 Cookie / UA / Apikey / Token 原样写进 site_secrets.json，"
                        "含明文凭据，别外传，用完及时清掉",
            }},
            {"component": "VSelect", "props": {
                "model": "import_strategy", "label": "导入冲突策略",
                "items": [
                    {"title": "跳过已存在（推荐）", "value": "skip"},
                    {"title": "覆盖已存在", "value": "overwrite"},
                ],
                "hint": "导入撞到重复记录时怎么办，默认跳过不动老数据",
            }},
        ]
        return form, defaults

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取状态、快照列表与最近对比"},
            {"path": "/backup", "auth": "bear", "endpoint": self.api_backup,
             "methods": ["POST"], "summary": "立即生成一份快照"},
            {"path": "/compare", "auth": "bear", "endpoint": self.api_compare,
             "methods": ["POST"], "summary": "对比两份快照（可传 a、b 文件名）"},
            {"path": "/restore-guide", "auth": "bear", "endpoint": self.api_restore_guide,
             "methods": ["POST"], "summary": "生成恢复清单文本"},
            {"path": "/clean", "auth": "bear", "endpoint": self.api_clean,
             "methods": ["POST"], "summary": "清理旧快照（需确认口令）"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "重新读取状态并重绘页面"},
            {"path": "/import-preview", "auth": "bear", "endpoint": self.api_import_preview,
             "methods": ["POST"], "summary": "预览某份快照的导入差异（只读）"},
            {"path": "/import", "auth": "bear", "endpoint": self.api_import,
             "methods": ["POST"], "summary": "导入迁移（默认试运行，确认口令后写回）"},
            {"path": "/import-guide", "auth": "bear", "endpoint": self.api_import_guide,
             "methods": ["POST"], "summary": "查看导入须知与当前冲突策略"},
        ]

    def _backup_path(self) -> Path:
        if self._backup_dir:
            directory = Path(self._backup_dir)
        else:
            directory = Path(str(self.get_data_path())) / "backups"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return directory

    def _send_notice(self, title: str, text: str, force: bool = False) -> None:
        if not self._notify and not force:
            return
        try:
            self.post_message(title=title, text=text)
        except Exception:
            pass

    def _do_backup(self) -> Dict[str, Any]:
        directory = self._backup_path()
        try:
            result = backup.write_snapshot(
                directory, str(self.get_data_path()), self._sections,
                self._keep, self._include_secrets)
            rotated = backup.rotate(directory, self._keep)
            self._send_notice("MP 备份迁移",
                              f"已生成 {result['file']}，顺手清掉 {len(rotated)} 份旧快照。")
            result["rotated"] = rotated
            return result
        except Exception as err:
            reason = f"{type(err).__name__}: {err}"
            self._send_notice("MP 备份迁移 · 备份失败", f"这次备份没完成：{reason}", force=True)
            return {"ok": False, "message": f"备份失败：{reason}"}

    def _collect_state(self) -> Dict[str, Any]:
        # 真正采集只在这里发生：点按钮或定时任务才会走到，页面渲染期不碰
        directory = self._backup_path()
        snapshots = backup.list_snapshots(directory)
        for row in snapshots:
            try:
                manifest = backup.load_manifest(directory / row["name"])
                counts = manifest.get("counts") or {}
                if counts:
                    row["count"] = sum(int(v) for v in counts.values()
                                       if isinstance(v, (int, float)))
                else:
                    sources = manifest.get("sources") or {}
                    row["count"] = sum(int(v.get("count", 0)) for v in sources.values()
                                       if isinstance(v, dict))
            except Exception:
                row["count"] = None
        sources: Dict[str, Any] = {}
        contains_secrets = False
        if snapshots:
            latest = backup.load_manifest(directory / snapshots[0]["name"])
            sources = latest.get("sources") or {}
            contains_secrets = bool(latest.get("contains_secrets"))
        diff = None
        diff_a = diff_b = ""
        if self._last_diff:
            diff = self._last_diff["diff"]
            diff_a, diff_b = self._last_diff["a"], self._last_diff["b"]
        else:
            auto = backup.compare_snapshots(directory)
            if auto["ok"]:
                diff = auto["diff"]
                diff_a, diff_b = auto["a"], auto["b"]
        total = sum(row.get("size", 0) for row in snapshots)
        self._cache = {
            "enabled": self._enabled,
            "offline": not backup.host_ready(),
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "backup_dir": str(directory),
            "cron": self._cron,
            "cron_label": backup.cron_label(self._cron),
            "keep": self._keep,
            "sections": list(self._sections),
            "snapshots": snapshots,
            "total_size": total,
            "total_size_text": backup.human_bytes(total),
            "sources": sources,
            "contains_secrets": contains_secrets,
            "diff": diff,
            "diff_a": diff_a,
            "diff_b": diff_b,
            "guide": self._last_guide,
            "preview": self._last_preview,
            "restore": self._last_restore,
            "import_guide": self._import_guide,
        }
        return self._cache

    def _render(self) -> str:
        return renderer.render(self._cache)

    def _valid_snapshot(self, name: str) -> Optional[Path]:
        if not name:
            return None
        clean = Path(name).name
        allowed = {p.name for p in backup.snapshot_files(self._backup_path())}
        if clean not in allowed:
            return None
        return self._backup_path() / clean

    def _payload_sections(self, body: Dict[str, Any]) -> List[str]:
        raw = body.get("sections")
        if isinstance(raw, (list, tuple)) and raw:
            cleaned = [name for name in restore.IMPORT_SECTIONS if name in raw]
            if cleaned:
                return cleaned
        return list(restore.IMPORT_SECTIONS)

    def api_snapshot(self) -> Response:
        state = self._collect_state()
        return Response(success=True, data={
            "status": {
                "enabled": state["enabled"], "backup_dir": state["backup_dir"],
                "cron": state["cron"], "keep": state["keep"], "sources": state["sources"],
            },
            "snapshots": state["snapshots"],
            "diff": state["diff"],
            "html": renderer.render(state),
        })

    def api_backup(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        result = self._do_backup()
        self._collect_state()
        if not result.get("ok"):
            return Response(success=False, message=result["message"], data={"html": self._render()})
        return Response(
            success=True,
            message=f"已生成 {result['file']}，清理旧快照 {len(result.get('rotated', []))} 份",
            data={"file": result["file"], "manifest": result["manifest"],
                  "rotated": result.get("rotated", []), "html": self._render()},
        )

    def api_compare(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        body = payload if isinstance(payload, dict) else {}
        result = backup.compare_snapshots(
            self._backup_path(),
            str(body.get("a") or "").strip() or None,
            str(body.get("b") or "").strip() or None,
        )
        if not result["ok"]:
            self._collect_state()
            return Response(success=False, message=result["message"], data={"html": self._render()})
        self._last_diff = result
        self._collect_state()
        return Response(success=True, message=result["message"],
                        data={"a": result["a"], "b": result["b"], "diff": result["diff"],
                              "html": self._render()})

    def api_restore_guide(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        name, text = backup.restore_guide(self._backup_path())
        self._last_guide = text
        self._collect_state()
        return Response(success=True, message=f"已生成 {name}",
                        data={"file": name, "text": text, "html": self._render()})

    def api_clean(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        body = payload if isinstance(payload, dict) else {}
        result = backup.clean_snapshots(self._backup_path(), self._keep,
                                        str(body.get("confirm") or ""))
        self._collect_state()
        return Response(success=bool(result["ok"]), message=result["message"],
                        data={"removed": result.get("removed", []), "html": self._render()})

    def api_refresh(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        self._collect_state()
        return Response(success=True, message="已刷新",
                        data={"snapshots": self._cache["snapshots"], "diff": self._cache["diff"],
                              "html": self._render()})

    def api_import_preview(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        body = payload if isinstance(payload, dict) else {}
        name = str(body.get("name") or "").strip()
        if not name:
            snaps = backup.list_snapshots(self._backup_path())
            name = snaps[0]["name"] if snaps else ""
        path = self._valid_snapshot(name)
        if path is None:
            self._collect_state()
            return Response(success=False, message="找不到这份快照，先备份或检查文件名",
                            data={"html": self._render()})
        result = restore.preview(path, self._payload_sections(body))
        self._last_preview = result
        self._collect_state()
        return Response(success=bool(result["ok"]), message=result["message"],
                        data={"preview": result, "html": self._render()})

    def api_import(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        body = payload if isinstance(payload, dict) else {}
        name = str(body.get("name") or "").strip()
        if not name:
            snaps = backup.list_snapshots(self._backup_path())
            name = snaps[0]["name"] if snaps else ""
        path = self._valid_snapshot(name)
        if path is None:
            self._collect_state()
            return Response(success=False, message="找不到这份快照，先备份或检查文件名",
                            data={"html": self._render()})
        strategy = str(body.get("strategy") or self._strategy)
        if strategy not in ("skip", "overwrite"):
            strategy = "skip"
        dry_run = bool(body.get("dry_run", True))
        confirm = str(body.get("confirm") or "")
        result = restore.restore(path, self._payload_sections(body),
                                 strategy, dry_run, confirm)
        self._last_restore = result
        self._collect_state()
        return Response(success=bool(result["ok"]), message=result["message"],
                        data={"restore": result, "html": self._render()})

    def api_import_guide(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        self._import_guide = restore.import_guide(self._strategy)
        self._collect_state()
        return Response(success=True, message="导入说明已更新",
                        data={"text": self._import_guide, "html": self._render()})

    def get_page(self) -> List[Dict[str, Any]]:
        try:
            return self._build_page()
        except Exception:  # noqa: BLE001
            return [
                {"component": "VRow", "content": [{
                    "component": "VCol", "content": [{
                        "component": "VBtn", "text": "读取状态",
                        "props": {"color": "primary"},
                        "events": {"click": {"api": "plugin/MPBackup/snapshot",
                                              "method": "get", "params": {}}},
                    }]}]},
                {"component": "div", "html": '<div class="mpbk">MP 备份迁移</div>'},
            ]

    def _build_page(self) -> List[Dict[str, Any]]:
        state = self._cache if isinstance(getattr(self, "_cache", None), dict) else {}
        snapshots = state.get("snapshots") or []
        latest = str(snapshots[0].get("name") or "") if snapshots else ""
        strategy = self._strategy if self._strategy in ("skip", "overwrite") else "skip"

        top = [
            ("立即备份", "backup", "post", {}, "mdi-backup-restore", "primary", "tonal"),
            ("对比最近两份", "compare", "post", {}, "mdi-compare-horizontal", "primary", "outlined"),
            ("恢复清单", "restore-guide", "post", {}, "mdi-file-document-outline", "primary", "outlined"),
            ("清理旧快照", "clean", "post", {"confirm": self.CLEAN_CONFIRM},
             "mdi-delete-sweep-outline", "warning", "tonal"),
            ("刷新", "refresh", "post", {}, "mdi-refresh", "primary", "tonal"),
            ("读取状态", "snapshot", "get", {}, "mdi-information-outline", "primary", "text"),
        ]
        import_top = [
            ("预览最近快照", "import-preview", "post", {"name": latest},
             "mdi-magnify", "primary", "tonal"),
            ("试运行导入", "import", "post", {"name": latest, "dry_run": True},
             "mdi-play-circle-outline", "primary", "outlined"),
            ("导入说明", "import-guide", "post", {},
             "mdi-help-circle-outline", "primary", "text"),
        ]

        def col(text: str, action: str, method: str, params: Dict[str, Any],
                icon: str, color: str, variant: str) -> Dict[str, Any]:
            return {
                "component": "VCol", "props": {"cols": 12, "md": 4, "lg": 2},
                "content": [{
                    "component": "VBtn", "text": text,
                    "props": {"color": color, "variant": variant, "prependIcon": icon,
                              "block": True, "class": "mb-2"},
                    "events": {"click": {
                        "api": f"plugin/MPBackup/{action}", "method": method, "params": params,
                    }},
                }],
            }

        top_cols = [col(*item) for item in top]
        import_cols = [col(*item) for item in import_top]

        for row in snapshots[:20]:
            name = str(row.get("name") or "")
            label = "{} · {} · {} 条".format(
                row.get("mtime") or "—", row.get("size_text") or "—",
                row.get("count") if row.get("count") is not None else "—")
            run_params = {"name": name, "confirm": restore.CONFIRM,
                          "sections": list(restore.IMPORT_SECTIONS),
                          "strategy": strategy, "dry_run": False}
            import_cols.append({
                "component": "VCol", "props": {"cols": 12, "md": 6, "lg": 4},
                "content": [
                    {"component": "VAlert", "text": html.escape(name + " · " + label),
                     "props": {"type": "info", "variant": "tonal", "density": "compact"}},
                    {"component": "VBtn", "text": "预览导入",
                     "props": {"color": "primary", "variant": "outlined", "class": "mr-2 mb-2"},
                     "events": {"click": {"api": "plugin/MPBackup/import-preview",
                                          "method": "post", "params": {"name": name}}}},
                    {"component": "VBtn", "text": "执行导入",
                     "props": {"color": "warning", "variant": "tonal", "class": "mb-2"},
                     "events": {"click": {"api": "plugin/MPBackup/import",
                                          "method": "post", "params": run_params}}},
                ],
            })

        return [
            {"component": "VRow", "content": top_cols},
            {"component": "VRow", "content": import_cols},
            {"component": "div", "html": renderer.render(state)},
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        try:
            from apscheduler.triggers.cron import CronTrigger

            try:
                trigger = CronTrigger.from_crontab(self._cron)
            except Exception:
                trigger = CronTrigger.from_crontab(backup.DEFAULT_CRON)
        except Exception:
            return []
        return [{
            "id": "MPBackup",
            "name": "MP 备份迁移定时备份",
            "trigger": trigger,
            "func": self._scheduled_backup,
            "kwargs": {},
        }]

    def _scheduled_backup(self) -> None:
        self._do_backup()
