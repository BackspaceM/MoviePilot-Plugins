"""单源追剧：让一条订阅只从一个站下，画质跟着第一集走。"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body
from pydantic import BaseModel

try:
    from app.sdk.plugin import _PluginBase
except Exception:
    class _PluginBase:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._data: Dict[str, Any] = {}
            self._config: Dict[str, Any] = {}

        def get_data(self, key: str, default: Any = None) -> Any:
            return self._data.get(key, default)

        def save_data(self, key: str, value: Any) -> None:
            self._data[key] = value

        def update_config(self, config: Dict[str, Any]) -> bool:
            self._config = dict(config or {})
            return True

        def get_config(self, key: str = "", default: Any = None) -> Any:
            return self._config.get(key, default) if key else dict(self._config)

        def post_message(self, **kwargs: Any) -> None:
            return None

try:
    from app.sdk.events import Event, eventmanager
    from app.schemas.types import EventType
except Exception:
    Event = Any  # type: ignore[misc,assignment]
    eventmanager = None  # type: ignore[assignment]

    class EventType:  # type: ignore[no-redef]
        PluginAction = "plugin.action"
        DownloadAdded = "download.added"


def _register(event_type: Any):
    if eventmanager is None:
        return lambda func: func
    return eventmanager.register(event_type)


from . import fingerprint, renderer, store

_BODY_DEFAULT = Body(default=None)
_CRON = {15: "*/15 * * * *", 30: "*/30 * * * *", 60: "0 * * * *", 120: "0 */2 * * *"}


class Response(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None


class SingleSourceTracker(_PluginBase):
    plugin_name = "单源追剧"
    plugin_desc = (
        "一条剧集订阅只从一个 PT 站下载，绑定时把订阅的站点范围写成单站；"
        "同时记下第一集标题里的画质标记，后面每一集都比对杜比视界 / HDR、"
        "H.264 / H.265、帧率、高码、分辨率与来源，混用会列出来提醒。"
        "解除绑定会还原原来的站点范围，默认只提醒不删任务。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/singlesourcetracker.png"
    plugin_version = "1.0.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "singlesource_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 94

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._target_site: Any = ""
        self._dimensions: List[str] = list(fingerprint.DEFAULT_DIMENSIONS)
        self._action_mode = "notice"
        self._interval = 30
        self._notify = True

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._enabled = bool(cfg.get("enabled", True))
        self._target_site = cfg.get("target_site") or ""
        raw_dimensions = cfg.get("dimensions")
        if not isinstance(raw_dimensions, (list, tuple)):
            raw_dimensions = [raw_dimensions] if raw_dimensions else []
        picked = [item for item in raw_dimensions if item in fingerprint.DIMENSION_LABELS]
        self._dimensions = picked or list(fingerprint.DEFAULT_DIMENSIONS)
        self._action_mode = "remove" if str(cfg.get("action_mode") or "") == "remove" else "notice"
        try:
            self._interval = int(cfg.get("interval") or 30)
        except (TypeError, ValueError):
            self._interval = 30
        if self._interval not in _CRON:
            self._interval = 30
        self._notify = bool(cfg.get("notify", True))

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        return None

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        sites = store.site_options()
        site_items = [
            {"title": f"{item['name']}（{item['domain']}）" if item.get("domain") else item["name"],
             "value": item.get("id")}
            for item in sites
        ]
        if not site_items:
            site_items = [{"title": "没读到站点，先去 MoviePilot 站点管理里配置", "value": 0}]
        dimension_items = [
            {"title": label, "value": key} for key, label in fingerprint.DIMENSION_LABELS.items()
        ]
        form = [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSwitch", "props": {
                        "model": "enabled", "label": "启用插件",
                        "hint": "关掉后不再定时巡检，也不拦截",
                    }},
                ]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "target_site", "label": "固定用哪个站",
                        "items": site_items,
                        "hint": "绑定剧集时把订阅的站点范围写成这一站",
                        "persistentHint": True,
                    }},
                ]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "dimensions", "label": "画质比对哪些项",
                        "items": dimension_items, "multiple": True, "chips": True,
                        "hint": "标题里没写的项只作提示，不算冲突",
                        "persistentHint": True,
                    }},
                ]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "action_mode", "label": "发现异常怎么办",
                        "items": [
                            {"title": "只提醒（推荐）", "value": "notice"},
                            {"title": "自动移除下载任务", "value": "remove"},
                        ],
                    }},
                ]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "interval", "label": "多久巡检一次",
                        "items": [
                            {"title": "15 分钟", "value": 15},
                            {"title": "30 分钟（推荐）", "value": 30},
                            {"title": "1 小时", "value": 60},
                            {"title": "2 小时", "value": 120},
                        ],
                    }},
                ]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VSwitch", "props": {
                        "model": "notify", "label": "发现冲突时通知",
                    }},
                ]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert", "props": {"type": "info", "variant": "tonal"}, "text": (
                        "设置只管全局规则。具体哪部剧要锁定，在插件详情页点剧名旁边的按钮；"
                        "解除绑定会把原来的站点范围还回去。清理任务要二次确认，不会动媒体库里的文件。"
                    )},
                ]},
            ]},
        ]
        return form, {
            "enabled": self._enabled,
            "target_site": self._target_site,
            "dimensions": self._dimensions,
            "action_mode": self._action_mode,
            "interval": self._interval,
            "notify": self._notify,
        }

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取最近一次巡检结果"},
            {"path": "/check", "auth": "bear", "endpoint": self.api_check,
             "methods": ["POST"], "summary": "立即巡检来源与画质"},
            {"path": "/bind", "auth": "bear", "endpoint": self.api_bind,
             "methods": ["POST"], "summary": "把一条订阅锁定到目标站点"},
            {"path": "/unbind", "auth": "bear", "endpoint": self.api_unbind,
             "methods": ["POST"], "summary": "解除绑定并还原站点范围"},
            {"path": "/apply", "auth": "bear", "endpoint": self.api_apply,
             "methods": ["POST"], "summary": "批量锁定所有未绑定订阅"},
            {"path": "/baseline", "auth": "bear", "endpoint": self.api_baseline,
             "methods": ["POST"], "summary": "重新取一条订阅的画质基线"},
            {"path": "/clean", "auth": "bear", "endpoint": self.api_clean,
             "methods": ["POST"], "summary": "清理冲突的下载任务"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "重新绘制详情页"},
        ]

    def get_command(self) -> List[Dict[str, Any]]:
        return [{
            "cmd": "/single_source", "event": EventType.PluginAction,
            "desc": "巡检单源追剧的来源与画质", "category": "订阅",
            "data": {"action": "check"},
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        try:
            from apscheduler.triggers.cron import CronTrigger

            trigger = CronTrigger.from_crontab(_CRON[self._interval])
        except Exception:
            return []
        return [{
            "id": "SingleSourceTracker", "name": "单源追剧巡检",
            "trigger": trigger, "func": self.run_scheduled,
            "kwargs": {}, "func_kwargs": {},
        }]

    def run_scheduled(self) -> None:
        if self._enabled:
            self._collect()

    @_register(EventType.DownloadAdded)
    def on_download_added(self, event: Event) -> None:
        try:
            data = getattr(event, "event_data", None)
            if not isinstance(data, dict):
                return
            source = data.get("source") or data.get("torrent_site") or ""
            hash_value = str(data.get("hash") or "")[:8]
            store.append_log(self, f"新下载 {source or '站点未知'} {hash_value}".strip())
        except Exception:
            return

    @_register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        try:
            data = getattr(event, "event_data", None)
            action = data.get("action") if isinstance(data, dict) else None
            if action in (None, "check", "status"):
                self._collect(notify=False)
        except Exception:
            return

    def _collect(self, notify: bool = True) -> Dict[str, Any]:
        try:
            snapshot = store.inspect(
                store.bindings(self), store.subscriptions(),
                store.downloads(), self._dimensions,
            )
        except Exception as exc:
            snapshot = store.inspect([], [], [], self._dimensions)
            snapshot["error"] = f"{type(exc).__name__}: {exc}"[:160]
        snapshot["mode"] = self._action_mode
        snapshot["log"] = store.log_lines(self)
        store.save_snapshot(self, snapshot)
        if notify and self._notify and snapshot["stats"]["conflicts"]:
            self.post_message(
                title="单源追剧",
                text=f"发现 {snapshot['stats']['conflicts']} 条来源或画质不一致，去插件详情页看看。",
            )
        return snapshot

    def _button(self, text: str, action: str, params: Optional[Dict[str, Any]] = None,
                color: str = "primary", icon: str = "", confirm: str = "") -> Dict[str, Any]:
        props: Dict[str, Any] = {
            "color": color, "variant": "tonal", "size": "small", "class": "ma-1 mb-3",
        }
        if icon:
            props["prependIcon"] = icon
        click: Dict[str, Any] = {
            "api": f"plugin/SingleSourceTracker/{action}",
            "method": "post", "params": dict(params or {}),
        }
        if confirm:
            click["confirm"] = confirm
        return {"component": "VBtn", "text": text, "props": props, "events": {"click": click}}

    def _page_data(self) -> Dict[str, Any]:
        cached = store.last_snapshot(self)
        if not cached.get("checked_at"):
            try:
                cached = self._collect(notify=False)
            except Exception:
                cached = {}
        cached["mode"] = self._action_mode
        cached["log"] = store.log_lines(self)
        return cached

    def get_page(self) -> List[Dict[str, Any]]:
        data = self._page_data()
        page: List[Dict[str, Any]] = [{"component": "div", "html": renderer.render(data)}]
        page.append(self._button("立即巡检", "check", color="primary", icon="mdi-refresh"))
        page.append(self._button("刷新页面", "refresh", icon="mdi-reload"))
        page.append(self._button("锁定全部未绑定", "apply", color="primary",
                                 icon="mdi-lock", confirm="把当前所有未绑定订阅都锁定到目标站点？"))
        page.append(self._button("清理异常任务", "clean", color="error", icon="mdi-delete-outline",
                                 params={"confirm": "清理"}, confirm="确认移除冲突的下载任务？"))

        pending = data.get("pending") if isinstance(data.get("pending"), list) else []
        for sub in [dict(item) for item in pending if isinstance(item, dict)][:8]:
            name = str(sub.get("name") or "未命名")
            page.append(self._button(f"锁定 {name[:14]}", "bind",
                                     params={"sid": sub.get("id")}, color="primary"))
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        bound = [dict(item) for item in rows if isinstance(item, dict)]
        for row in bound[:6]:
            name = str(row.get("name") or "未命名")
            page.append(self._button(f"解除 {name[:14]}", "unbind",
                                     params={"sid": row.get("sid")}))
        for row in [item for item in bound if int(item.get("conflicts") or 0) > 0][:4]:
            name = str(row.get("name") or "未命名")
            page.append(self._button(f"重取基线 {name[:10]}", "baseline",
                                     params={"sid": row.get("sid")}))
        return page

    def api_snapshot(self) -> Response:
        return Response(success=True, data=self._page_data())

    def api_check(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        data = self._collect()
        return Response(success=True,
                        message=f"巡检完成，{data['stats']['conflicts']} 条冲突",
                        data={"html": renderer.render(data), "stats": data["stats"]})

    def api_refresh(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        data = self._page_data()
        return Response(success=True, message="页面已刷新",
                        data={"html": renderer.render(data)})

    def _bind_one(self, sid: Any) -> Tuple[bool, str]:
        sites = store.site_options()
        site = store.find_site(sites, self._target_site)
        if not site:
            return False, "先在设置里选好目标站点"
        sub = next((item for item in store.subscriptions()
                    if str(item.get("id")) == str(sid)), None)
        if not sub:
            return False, "这条订阅找不到了"
        bound = store.bindings(self)
        if any(str(item.get("sid")) == str(sid) for item in bound):
            return False, "已经绑定过了"
        records = [item for item in store.downloads()
                   if store.record_matches(item, {"name": sub.get("name"), "season": sub.get("season")})]
        ok, reason = store.set_sites(sid, [site.get("id")])
        bound.append({
            "sid": sid,
            "name": sub.get("name"),
            "season": sub.get("season"),
            "site_id": site.get("id"),
            "site_name": site.get("name"),
            "domain": site.get("domain") or "",
            "original_sites": list(sub.get("sites") or []),
            "baseline": store.pick_baseline(records, self._dimensions),
            "locked": ok,
            "locked_note": reason,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        store.save_bindings(self, bound)
        note = "" if ok else "（站点范围写入失败，只做巡检）"
        store.append_log(self, f"绑定《{sub.get('name')}》到 {site.get('name')}{note}")
        return True, f"已绑定《{sub.get('name')}》{note}"

    def api_bind(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        sid = (payload or {}).get("sid")
        if sid in (None, ""):
            return Response(success=False, message="缺少订阅 ID")
        ok, message = self._bind_one(sid)
        data = self._collect(notify=False) if ok else self._page_data()
        return Response(success=ok, message=message, data={"html": renderer.render(data)})

    def api_unbind(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        sid = (payload or {}).get("sid")
        if sid in (None, ""):
            return Response(success=False, message="缺少订阅 ID")
        bound = store.bindings(self)
        target = next((item for item in bound if str(item.get("sid")) == str(sid)), None)
        if not target:
            return Response(success=False, message="这条订阅没有绑定")
        store.set_sites(sid, target.get("original_sites") or [])
        store.save_bindings(self, [item for item in bound if str(item.get("sid")) != str(sid)])
        store.append_log(self, f"解除《{target.get('name')}》，站点范围已还原")
        data = self._collect(notify=False)
        return Response(success=True, message=f"已解除《{target.get('name')}》",
                        data={"html": renderer.render(data)})

    def api_apply(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        subs = store.subscriptions()
        bound = store.bindings(self)
        bound_ids = {str(item.get("sid")) for item in bound}
        pending = [item for item in subs if str(item.get("id")) not in bound_ids][:20]
        if not pending:
            return Response(success=True, message="没有待绑定的订阅",
                            data={"html": renderer.render(self._page_data())})
        done, messages = 0, []
        for sub in pending:
            ok, message = self._bind_one(sub.get("id"))
            if ok:
                done += 1
            else:
                messages.append(message)
        text = f"已锁定 {done} 部" if done else "一部都没锁定"
        if messages:
            text += "；" + messages[0]
        data = self._collect(notify=False)
        return Response(success=done > 0, message=text,
                        data={"html": renderer.render(data)})

    def api_baseline(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        sid = (payload or {}).get("sid")
        bound = store.bindings(self)
        target = next((item for item in bound if str(item.get("sid")) == str(sid)), None)
        if not target:
            return Response(success=False, message="这条订阅没有绑定")
        records = [item for item in store.downloads() if store.record_matches(item, target)]
        if not records:
            return Response(success=False, message="还没有可参照的下载记录")
        target["baseline"] = store.pick_baseline(records, self._dimensions)
        store.save_bindings(self, bound)
        store.append_log(self, f"重取《{target.get('name')}》画质基线")
        data = self._collect(notify=False)
        return Response(success=True, message="基线已更新",
                        data={"html": renderer.render(data)})

    def api_clean(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        if str((payload or {}).get("confirm") or "") != "清理":
            return Response(success=False, message="请先确认清理")
        if self._action_mode != "remove":
            return Response(success=False, message="当前是只提醒模式，没有移除任何任务")
        data = self._collect(notify=False)
        targets = [item for item in data.get("issues", [])
                   if item.get("level") == "conflict" and item.get("hash")][:20]
        done, failed = 0, 0
        for issue in targets:
            ok, note = store.remove_torrent(issue)
            if ok:
                done += 1
            else:
                failed += 1
            store.append_log(self, f"清理 {issue.get('name')}：{note}")
        text = f"移除 {done} 个任务" + (f"，{failed} 个没成功" if failed else "")
        data = self._collect(notify=False)
        return Response(success=True, message=text,
                        data={"html": renderer.render(data), "removed": done})
