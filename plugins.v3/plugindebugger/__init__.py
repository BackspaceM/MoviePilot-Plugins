import datetime
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body
from pydantic import BaseModel

try:
    from app.sdk.plugin import _PluginBase
except Exception:
    class _PluginBase:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._data: Dict[str, Any] = {}

from . import installer, renderer

_LOG_KEY = "pdg_log"
_STATS_KEY = "pdg_stats"
_HISTORY_KEY = "pdg_history"
_BODY = Body(default=None)
_DELETE_CONFIRM = "删除"
_MAX_LOGS = 40


class Response(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None


class PluginDebugger(_PluginBase):
    plugin_name = "插件调试"
    plugin_desc = (
        "把插件 ZIP 直接装进 MoviePilot，方便本地改完代码打包测试。支持网页选文件上传，"
        "也可以填容器里能看到的 ZIP 路径；会校验包结构、备份旧版本、防目录穿越，"
        "装完尝试自动重载，装错了能一键回滚。页面只读一次状态，不轮询。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/plugindebugger.png"
    plugin_version = "1.0.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "plugindebugger_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 97

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._max_size_mb = 20
        self._auto_backup = True
        self._auto_reload = True
        self._allow_overwrite = True
        self._notify = False
        self._cache: Dict[str, Any] = {}
        self._log_buf: List[str] = []
        self._delete_plan: Dict[str, Any] = {}
        self._plugins_root_override: Optional[Path] = None

    @staticmethod
    def _flag(value: Any, default: bool = True) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "off", "no")
        return bool(value)

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._enabled = self._flag(cfg.get("enabled"), True)
        try:
            self._max_size_mb = max(1, min(200, int(cfg.get("max_size_mb") or 20)))
        except (TypeError, ValueError):
            self._max_size_mb = 20
        self._auto_backup = self._flag(cfg.get("auto_backup"), True)
        self._auto_reload = self._flag(cfg.get("auto_reload"), True)
        self._allow_overwrite = self._flag(cfg.get("allow_overwrite"), True)
        self._notify = self._flag(cfg.get("notify"), False)
        self._refresh_cache()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        self._flush_log()
        self._delete_plan = {}

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取上传列表、安装记录与目录信息"},
            {"path": "/upload", "auth": "bear", "endpoint": self.api_upload,
             "methods": ["POST"], "summary": "接收网页选中的 ZIP"},
            {"path": "/install", "auth": "bear", "endpoint": self.api_install,
             "methods": ["POST"], "summary": "校验并安装某个 ZIP"},
            {"path": "/reload", "auth": "bear", "endpoint": self.api_reload,
             "methods": ["POST"], "summary": "重载插件"},
            {"path": "/delete", "auth": "bear", "endpoint": self.api_delete,
             "methods": ["POST"], "summary": "删除上传的 ZIP（两步确认）"},
            {"path": "/rollback", "auth": "bear", "endpoint": self.api_rollback,
             "methods": ["POST"], "summary": "把某插件恢复成安装前的版本"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "重新读一次状态"},
        ]

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        try:
            def col(component: Dict[str, Any]) -> Dict[str, Any]:
                return {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [component]}

            form = [{"component": "VForm", "content": [
                {"component": "VRow", "content": [
                    col({"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}),
                    col({"component": "VSwitch", "props": {"model": "notify", "label": "安装结果通知"}}),
                ]},
                {"component": "VRow", "content": [
                    col({"component": "VSelect", "props": {
                        "model": "max_size_mb", "label": "单个 ZIP 上限",
                        "items": [{"title": "10 MB", "value": 10}, {"title": "20 MB", "value": 20},
                                  {"title": "50 MB", "value": 50}, {"title": "100 MB", "value": 100}],
                        "hint": "太大的包浏览器上传会慢，可以用容器路径安装", "persistentHint": True}}),
                    col({"component": "VSwitch", "props": {
                        "model": "auto_backup", "label": "安装前备份旧版本",
                        "hint": "备份存在插件数据目录，方便回滚", "persistentHint": True}}),
                ]},
                {"component": "VRow", "content": [
                    col({"component": "VSwitch", "props": {
                        "model": "auto_reload", "label": "装完自动重载",
                        "hint": "没成功也不影响，文件已经放好了", "persistentHint": True}}),
                    col({"component": "VSwitch", "props": {
                        "model": "allow_overwrite", "label": "允许覆盖同名插件",
                        "hint": "反复测试同一个插件时开着", "persistentHint": True}}),
                ]},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact"},
                 "text": "一个包里只放一个插件目录最省事；包里有多个时会挑层级最浅的那个，"
                         "安装结果里会列出其它目录名。"},
            ]}]
            return form, {
                "enabled": True, "notify": False, "max_size_mb": 20,
                "auto_backup": True, "auto_reload": True, "allow_overwrite": True,
            }
        except Exception as err:
            return [{"component": "div", "text": f"设置页读取失败：{type(err).__name__}"}], {}

    def _data_root(self) -> Path:
        try:
            return Path(str(self.get_data_path()))
        except Exception:
            return Path("/tmp") / "plugindebugger"

    def _uploads_dir(self) -> Path:
        path = self._data_root() / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _backups_dir(self) -> Path:
        path = self._data_root() / "backups"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _plugins_root(self) -> Path:
        if self._plugins_root_override is not None:
            return self._plugins_root_override
        try:
            from app.sdk.config import settings
            root = str(getattr(settings, "ROOT_PATH", "") or "")
            if root and Path(root).is_dir():
                return Path(root) / "app" / "plugins"
        except Exception:
            pass
        return self._data_root() / "app" / "plugins"

    def _log(self, text: str) -> None:
        stamp = datetime.datetime.now().strftime("%m-%d %H:%M")
        self._log_buf.append(f"{stamp} {text}")
        if len(self._log_buf) >= 20:
            self._flush_log()

    def _flush_log(self) -> None:
        if not self._log_buf:
            return
        try:
            old = self.get_data(_LOG_KEY)
            lines = [str(x) for x in old if isinstance(x, str)] if isinstance(old, (list, tuple)) else []
            self.save_data(_LOG_KEY, (list(self._log_buf) + lines)[:_MAX_LOGS])
        except Exception:
            pass
        self._log_buf = []

    def _read_stats(self) -> Dict[str, Any]:
        try:
            value = self.get_data(_STATS_KEY)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _read_history(self) -> List[Dict[str, Any]]:
        try:
            value = self.get_data(_HISTORY_KEY)
            return [x for x in value if isinstance(x, dict)][:20] if isinstance(value, (list, tuple)) else []
        except Exception:
            return []

    def _upload_list(self) -> List[Dict[str, Any]]:
        out = []
        try:
            for path in sorted(self._uploads_dir().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                stat = path.stat()
                out.append({
                    "name": path.name,
                    "size_text": f"{stat.st_size / 1048576:.1f} MB",
                    "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        except Exception:
            pass
        return out

    def _refresh_cache(self) -> Dict[str, Any]:
        stats = self._read_stats()
        history = self._read_history()
        try:
            raw = self.get_data(_LOG_KEY)
            log = [str(x) for x in raw if isinstance(x, str)][:8] if isinstance(raw, (list, tuple)) else []
        except Exception:
            log = []
        self._cache = {
            "uploads": self._upload_list(),
            "history": history,
            "log": list(self._log_buf[-8:]) + log,
            "install_count": int(stats.get("install_count") or 0),
            "last_install_at": stats.get("last_install_at") or "",
            "last_install_id": stats.get("last_install_id") or "",
            "plugins_root": str(self._plugins_root()),
            "max_size_text": f"{self._max_size_mb} MB",
        }
        return self._cache

    def _view(self) -> Dict[str, Any]:
        return self._cache if isinstance(self._cache, dict) else {}

    def get_page(self) -> List[Dict[str, Any]]:
        try:
            data = self._view()
            prefix = f"plugin/{self.__class__.__name__}/"

            def button(text: str, action: str, params: Dict[str, Any], color: str = "primary",
                       variant: str = "tonal") -> Dict[str, Any]:
                return {"component": "VBtn", "text": text,
                        "props": {"color": color, "variant": variant, "class": "ma-1", "density": "comfortable"},
                        "events": {"click": {"api": prefix + action, "method": "post", "params": params}}}

            top = [
                button("刷新状态", "refresh", {}),
                button("重载最近安装的插件", "reload", {}, "primary", "outlined"),
            ]
            history = data.get("history") or []
            if history:
                last_id = str(history[0].get("plugin_id") or "")
                if last_id:
                    top.append(button(f"回滚 {last_id}", "rollback", {"plugin_id": last_id}, "warning", "outlined"))

            rows: List[Dict[str, Any]] = []
            plan = self._delete_plan
            plan_fresh = False
            plan_name = ""
            plan_id = ""
            if isinstance(plan, dict):
                try:
                    plan_fresh = float(plan.get("expires_at") or 0) > time.time()
                except (TypeError, ValueError):
                    plan_fresh = False
                plan_name = str(plan.get("name") or "")
                plan_id = str(plan.get("plan_id") or "")

            for row in (data.get("uploads") or [])[:20]:
                name = str(row.get("name") or "")
                if not name:
                    continue
                content: List[Dict[str, Any]] = [
                    {"component": "VAlert", "text": (
                        f"{name} · {row.get('size_text') or '—'} · {row.get('mtime') or '—'}"),
                     "props": {"type": "info", "variant": "tonal", "density": "compact"}},
                    button("安装", "install", {"name": name}, "primary", "tonal"),
                    button("删除", "delete", {"name": name}, "error", "text"),
                ]
                if plan_fresh and name == plan_name:
                    content.append(button(f"确认删除 {name}", "delete",
                                          {"name": name, "plan_id": plan_id,
                                           "confirm": _DELETE_CONFIRM}, "error", "tonal"))
                rows.append({"component": "VCol", "props": {"cols": 12, "md": 6},
                             "content": content})

            page: List[Dict[str, Any]] = [{"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": top}]}]
            if rows:
                page.append({"component": "VRow", "content": rows})
            page.append({"component": "div", "html": renderer.render(data)})
            return page
        except Exception as err:
            return [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
                     "text": f"页面读取失败：{type(err).__name__}，点一下刷新状态重试"},
                    {"component": "div", "html": renderer.render({})}]

    @staticmethod
    def _fail(action: str, err: Exception) -> Response:
        return Response(success=False, message=f"{action}失败：{type(err).__name__}: {err}")

    def _notice(self, title: str, text: str) -> None:
        if not self._notify:
            return
        try:
            self.post_message(title=title, text=text)
        except Exception:
            pass

    def api_snapshot(self) -> Response:
        try:
            return Response(success=True, message="状态已读取", data=self._refresh_cache())
        except Exception as err:
            return self._fail("读取状态", err)

    def api_refresh(self, payload: Optional[dict] = _BODY) -> Response:
        try:
            return Response(success=True, message="状态已刷新", data=self._refresh_cache())
        except Exception as err:
            return self._fail("刷新", err)

    def api_upload(self, payload: Optional[dict] = _BODY) -> Response:
        try:
            body = payload if isinstance(payload, dict) else {}
            name = installer.safe_name(body.get("name"))
            if not name.lower().endswith(".zip"):
                return Response(success=False, message="只收 .zip 文件")
            content, error = installer.decode_upload(body.get("data"), self._max_size_mb * 1048576)
            if error:
                return Response(success=False, message=error)
            target = self._uploads_dir() / name
            target.write_bytes(content)
            self._log(f"上传 {name}（{len(content) // 1024} KB）")
            self._flush_log()
            self._refresh_cache()
            return Response(success=True, message=f"{name} 已上传，点「刷新状态」再点「安装」")
        except Exception as err:
            return self._fail("上传", err)

    def _load_zip(self, body: Dict[str, Any]) -> Tuple[bytes, str]:
        name = installer.safe_name(body.get("name"))
        if name:
            path = self._uploads_dir() / name
            if not path.is_file() or path.suffix.lower() != ".zip":
                return b"", f"上传列表里没有 {name}"
            return path.read_bytes(), ""
        raw_path = str(body.get("path") or "").strip()
        if not raw_path:
            return b"", "要给我一个上传的文件名，或者容器里能看到的 ZIP 路径"
        path = Path(raw_path)
        if not path.is_file():
            return b"", f"路径读不到文件：{raw_path}"
        if path.stat().st_size > self._max_size_mb * 1048576:
            return b"", "这个文件超过设置里的上限"
        return path.read_bytes(), ""

    def _reload(self, plugin_id: str, force: bool = False) -> Tuple[bool, str]:
        if not self._auto_reload and not force:
            return False, "设置里没开自动重载，去插件管理页手动重载一次"
        try:
            try:
                from app.sdk.plugin import PluginManager
            except Exception:
                from app.runtime.extensions.plugin.manager import PluginManager
            status = PluginManager().reload_plugin(plugin_id)
            return True, f"已重载（{status}）"
        except Exception as err:
            return False, (f"自动重载没成功：{type(err).__name__}；文件已经放好了，"
                           "去插件管理页手动重载一次就行")

    def api_install(self, payload: Optional[dict] = _BODY) -> Response:
        try:
            body = payload if isinstance(payload, dict) else {}
            raw, error = self._load_zip(body)
            if error:
                return Response(success=False, message=error)
            info = installer.inspect_zip(raw, str(body.get("subdir") or ""))
            if not info.get("ok"):
                return Response(success=False, message=info.get("error") or "包结构不对")
            plugin_id = str(info.get("id") or "")
            root = self._plugins_root()
            dest = root / plugin_id.lower()
            if dest.exists() and not self._allow_overwrite and not self._flag(body.get("force"), False):
                return Response(success=False,
                                message=f"{plugin_id} 已经装过了，设置里打开「允许覆盖同名插件」再装")
            backup_name = ""
            if dest.exists() and self._auto_backup:
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                archive = self._backups_dir() / f"{plugin_id.lower()}_{stamp}.zip"
                if installer.make_backup(dest, archive):
                    backup_name = archive.name
            count, error = installer.extract_plugin(raw, str(info.get("prefix") or ""), dest)
            if error:
                return Response(success=False, message=error)
            ok, reload_text = self._reload(plugin_id)
            now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            stats = self._read_stats()
            stats["install_count"] = int(stats.get("install_count") or 0) + 1
            stats["last_install_at"] = now_text
            stats["last_install_id"] = plugin_id
            self.save_data(_STATS_KEY, stats)
            history = self._read_history()
            history.insert(0, {"time": now_text, "plugin_id": plugin_id,
                               "version": info.get("version") or "—",
                               "file": installer.safe_name(body.get("name")) or str(body.get("path") or "路径"),
                               "result": "已安装" if ok else "已装待重载"})
            self.save_data(_HISTORY_KEY, history[:20])
            self._log(f"安装 {plugin_id} {info.get('version') or ''}，{count} 个文件，{reload_text}")
            if backup_name:
                self._log(f"旧版本已备份为 {backup_name}")
            self._flush_log()
            self._refresh_cache()
            message = (f"{plugin_id} 装好了（{count} 个文件 → {dest}）。{reload_text}。"
                       f"包里有 {info.get('files')} 个文件，识别到目录 {info.get('prefix') or '根目录'}")
            if len(info.get("candidates") or []) > 1:
                message += "；包里有多个插件目录：" + "、".join(info["candidates"])
            self._notice("插件调试", message)
            return Response(success=True, message=message,
                            data={"plugin_id": plugin_id, "version": info.get("version"),
                                  "dest": str(dest), "files": count, "reloaded": ok,
                                  "backup": backup_name})
        except Exception as err:
            return self._fail("安装", err)

    def api_reload(self, payload: Optional[dict] = _BODY) -> Response:
        try:
            body = payload if isinstance(payload, dict) else {}
            plugin_id = str(body.get("plugin_id") or "").strip()
            if not plugin_id:
                history = self._read_history()
                plugin_id = str(history[0].get("plugin_id") or "") if history else ""
            if not plugin_id:
                return Response(success=False, message="还没有安装记录，先在参数里给一个插件 ID")
            ok, text = self._reload(plugin_id, force=True)
            self._log(f"重载 {plugin_id}：{text}")
            self._flush_log()
            self._refresh_cache()
            return Response(success=ok, message=f"{plugin_id}：{text}")
        except Exception as err:
            return self._fail("重载", err)

    def api_delete(self, payload: Optional[dict] = _BODY) -> Response:
        try:
            body = payload if isinstance(payload, dict) else {}
            name = installer.safe_name(body.get("name"))
            if not name:
                return Response(success=False, message="没指定要删哪个文件")
            plan_id = str(body.get("plan_id") or "")
            if not plan_id:
                path = self._uploads_dir() / name
                if not path.is_file():
                    return Response(success=False, message=f"找不到 {name}")
                self._delete_plan = {"plan_id": uuid.uuid4().hex[:12], "name": name,
                                     "expires_at": time.time() + 600}
                return Response(success=True,
                                message=f"要删的是 {name}，再点它旁边的「确认删除」才会真删，10 分钟内有效")
            plan = self._delete_plan
            if (not isinstance(plan, dict) or str(plan.get("plan_id") or "") != plan_id
                    or str(plan.get("name") or "") != name
                    or str(body.get("confirm") or "") != _DELETE_CONFIRM
                    or float(plan.get("expires_at") or 0) <= time.time()):
                return Response(success=False, message="确认信息不对或已过期，重新点一次「删除」")
            path = self._uploads_dir() / name
            if path.is_file():
                path.unlink()
            self._delete_plan = {}
            self._log(f"删除上传包 {name}")
            self._flush_log()
            self._refresh_cache()
            return Response(success=True, message=f"{name} 已删除")
        except Exception as err:
            return self._fail("删除", err)

    def api_rollback(self, payload: Optional[dict] = _BODY) -> Response:
        try:
            body = payload if isinstance(payload, dict) else {}
            plugin_id = str(body.get("plugin_id") or "").strip()
            if not plugin_id:
                history = self._read_history()
                plugin_id = str(history[0].get("plugin_id") or "") if history else ""
            if not plugin_id:
                return Response(success=False, message="没有可回滚的安装记录")
            archives = sorted(self._backups_dir().glob(f"{plugin_id.lower()}_*.zip"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            if not archives:
                return Response(success=False, message=f"{plugin_id} 没有安装前的备份")
            ok, error = installer.restore_backup(archives[0], self._plugins_root() / plugin_id.lower())
            if not ok:
                return Response(success=False, message=error or "恢复失败")
            _, reload_text = self._reload(plugin_id, force=True)
            self._log(f"回滚 {plugin_id} 到 {archives[0].name}，{reload_text}")
            self._flush_log()
            self._refresh_cache()
            message = f"{plugin_id} 已恢复成 {archives[0].name}。{reload_text}"
            self._notice("插件调试", message)
            return Response(success=True, message=message)
        except Exception as err:
            return self._fail("回滚", err)
