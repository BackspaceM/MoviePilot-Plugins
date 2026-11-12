"""MP 备份迁移 —— 把 MoviePilot 能读到的状态打包存档，不写回数据库。"""

from __future__ import annotations

import datetime as _dt
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

from . import backup, renderer


class Response(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None


class MPBackup(_PluginBase):
    # 元数据一律字符串字面量，官方校验脚本用 AST 直接取值
    plugin_name = "MP 备份迁移"
    plugin_desc = (
        "把 MoviePilot 当前能读到的配置、订阅、站点、服务、插件清单和几类历史打包成一份 zip 快照，"
        "存在插件数据目录里按份数轮转，不会写回数据库。可以对比两份快照看新增、删除和字段变化，"
        "也能生成一份恢复清单，逐项说明每个文件是干什么的、按什么顺序手工恢复。"
        "支持每天定时备份，结果发通知。敏感字段统一打码，不落盘。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/mpbackup.png"
    plugin_version = "1.0.0"
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
        self._cfg: Dict[str, Any] = {}
        self._last_diff: Optional[Dict[str, Any]] = None
        self._last_guide: Optional[str] = None

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._cfg = cfg
        self._enabled = bool(cfg.get("enabled", True))
        self._cron = backup.clean_cron(cfg.get("schedule"))
        self._keep = backup.clean_keep(cfg.get("keep"))
        self._backup_dir = str(cfg.get("backup_dir") or "")
        self._sections = backup.clean_sections(cfg.get("sections"))
        self._notify = bool(cfg.get("notify", True))
        self._last_diff = None
        self._last_guide = None

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        self._last_diff = None
        self._last_guide = None

    def close(self) -> None:
        self.stop_service()

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        defaults: Dict[str, Any] = {
            "enabled": True,
            "backup_dir": "",
            "keep": 7,
            "schedule": backup.DEFAULT_CRON,
            "sections": list(backup.DEFAULT_SECTIONS),
            "notify": True,
        }
        cron_items = [{"title": label, "value": cron} for cron, label in backup.CRON_CHOICES]
        section_items = [{"title": label, "value": key} for key, label in backup.SECTION_OPTIONS]
        form: List[Dict[str, Any]] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
             "text": "快照存在插件数据目录里，只导出不写回。站点和服务的密钥会打码，恢复时照着清单手工填。"},
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
                "items": section_items, "hint": "历史类最多存 2000 条，避免 zip 太大",
            }},
            {"component": "VSwitch", "props": {
                "model": "notify", "label": "结果通知", "color": "primary",
                "hint": "备份成功发一条；失败不管开关都会告警",
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
                directory, str(self.get_data_path()), self._sections, self._keep)
            rotated = backup.rotate(directory, self._keep)
            self._send_notice("MP 备份迁移",
                              f"已生成 {result['file']}，顺手清掉 {len(rotated)} 份旧快照。")
            result["rotated"] = rotated
            return result
        except Exception as err:
            reason = f"{type(err).__name__}: {err}"
            self._send_notice("MP 备份迁移 · 备份失败", f"这次备份没完成：{reason}", force=True)
            return {"ok": False, "message": f"备份失败：{reason}"}

    def _state(self) -> Dict[str, Any]:
        directory = self._backup_path()
        snapshots = backup.list_snapshots(directory)
        sources: Dict[str, Any] = {}
        if snapshots:
            sources = backup.load_manifest(directory / snapshots[0]["name"]).get("sources", {}) or {}
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
        return {
            "enabled": self._enabled,
            "offline": not backup.host_ready(),
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "backup_dir": str(directory),
            "cron": self._cron,
            "cron_label": backup.cron_label(self._cron),
            "keep": self._keep,
            "sections": self._sections,
            "snapshots": snapshots,
            "total_size": total,
            "total_size_text": backup.human_bytes(total),
            "sources": sources,
            "diff": diff,
            "diff_a": diff_a,
            "diff_b": diff_b,
            "guide": self._last_guide,
        }

    def _render(self) -> str:
        return renderer.render(self._state())

    def api_snapshot(self) -> Response:
        state = self._state()
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
            return Response(success=False, message=result["message"], data={"html": self._render()})
        self._last_diff = result
        return Response(success=True, message=result["message"],
                        data={"a": result["a"], "b": result["b"], "diff": result["diff"],
                              "html": self._render()})

    def api_restore_guide(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        name, text = backup.restore_guide(self._backup_path())
        self._last_guide = text
        return Response(success=True, message=f"已生成 {name}",
                        data={"file": name, "text": text, "html": self._render()})

    def api_clean(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        body = payload if isinstance(payload, dict) else {}
        result = backup.clean_snapshots(self._backup_path(), self._keep,
                                        str(body.get("confirm") or ""))
        return Response(success=bool(result["ok"]), message=result["message"],
                        data={"removed": result.get("removed", []), "html": self._render()})

    def api_refresh(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        state = self._state()
        return Response(success=True, message="已刷新",
                        data={"snapshots": state["snapshots"], "diff": state["diff"],
                              "html": renderer.render(state)})

    def get_page(self) -> List[Dict[str, Any]]:
        buttons = [
            ("立即备份", "backup", "post", {}, "mdi-backup-restore", "primary", "tonal"),
            ("对比最近两份", "compare", "post", {}, "mdi-compare-horizontal", "primary", "outlined"),
            ("恢复清单", "restore-guide", "post", {}, "mdi-file-document-outline", "primary", "outlined"),
            ("清理旧快照", "clean", "post", {"confirm": self.CLEAN_CONFIRM},
             "mdi-delete-sweep-outline", "warning", "tonal"),
            ("刷新", "refresh", "post", {}, "mdi-refresh", "primary", "tonal"),
            ("读取状态", "snapshot", "get", {}, "mdi-information-outline", "primary", "text"),
        ]
        cols = []
        for text, action, method, params, icon, color, variant in buttons:
            cols.append({
                "component": "VCol", "props": {"cols": 12, "md": 4, "lg": 2},
                "content": [{
                    "component": "VBtn", "text": text,
                    "props": {"color": color, "variant": variant, "prependIcon": icon,
                              "block": True, "class": "mb-2"},
                    "events": {"click": {
                        "api": f"plugin/MPBackup/{action}", "method": method, "params": params,
                    }},
                }],
            })
        return [
            {"component": "VRow", "content": cols},
            {"component": "div", "html": renderer.render(self._state())},
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
