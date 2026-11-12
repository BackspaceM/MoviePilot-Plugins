"""单源追剧：每部剧自己定站点和标题规则。"""

from __future__ import annotations

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


from . import renderer, rules, store

_BODY_DEFAULT = Body(default=None)
_CRON = {15: "*/15 * * * *", 30: "*/30 * * * *", 60: "0 * * * *", 120: "0 */2 * * *"}


class Response(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None


class SingleSourceTracker(_PluginBase):
    plugin_name = "单源追剧"
    plugin_desc = (
        "读取 MP 当前订阅，按剧名或订阅 ID 逐条配置：只从哪个站下、标题必须含什么、"
        "不能含什么。纳入和排除都支持普通关键词与 re: 正则，多个条件用 ;; 分开。"
        "巡检下载历史，来源或标题不合规则就列出来；可选把订阅站点范围锁定成单站，"
        "解除时还原。默认只提醒，不动媒体库文件。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/singlesourcetracker.png"
    plugin_version = "1.1.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "singlesource_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 94

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._rules_text = ""
        self._rules: List[Dict[str, Any]] = []
        self._rule_errors: List[str] = []
        self._lock_sites = True
        self._action_mode = "notice"
        self._interval = 30
        self._notify = True

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._enabled = bool(cfg.get("enabled", True))
        raw = cfg.get("rules_text") or ""
        self._rules_text = raw if isinstance(raw, str) else str(raw)
        self._rules, self._rule_errors = rules.parse_rules(self._rules_text)
        self._lock_sites = bool(cfg.get("lock_sites", True))
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
        try:
            form = [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": [
                        {"component": "VTextarea", "props": {
                            "model": "rules_text", "label": "规则表（一行一部剧）",
                            "rows": 8, "autoGrow": True,
                            "placeholder": rules.EXAMPLE,
                            "hint": "匹配目标 => 站点 => 纳入 => 排除；多项用 ;; 分开，正则前面加 re:",
                            "persistentHint": True,
                        }},
                    ]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VSwitch", "props": {
                            "model": "enabled", "label": "启用插件",
                            "hint": "关掉后不巡检也不处置",
                        }},
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VSwitch", "props": {
                            "model": "lock_sites", "label": "按规则锁定站点范围",
                            "hint": "把订阅的站点范围写成规则里的那一站",
                        }},
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VSwitch", "props": {
                            "model": "notify", "label": "发现异常时通知",
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
                        {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                         "text": "规则在详情页也能看到，写错的行会标出来，不影响其他行。"},
                    ]},
                ]},
            ]
            defaults = {
                "enabled": self._enabled,
                "rules_text": self._rules_text,
                "lock_sites": self._lock_sites,
                "action_mode": self._action_mode,
                "interval": self._interval,
                "notify": self._notify,
            }
            return form, defaults
        except Exception:
            return [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
                     "text": "配置暂时读不出来，插件还是能跑；重开一次页面再试。"}], {}

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取最近一次巡检结果"},
            {"path": "/check", "auth": "bear", "endpoint": self.api_check,
             "methods": ["POST"], "summary": "读取订阅与下载历史并巡检"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "重新绘制详情页"},
            {"path": "/template", "auth": "bear", "endpoint": self.api_template,
             "methods": ["POST"], "summary": "为未配规则的订阅生成模板"},
            {"path": "/lock", "auth": "bear", "endpoint": self.api_lock,
             "methods": ["POST"], "summary": "按规则锁定订阅站点范围"},
            {"path": "/unlock", "auth": "bear", "endpoint": self.api_unlock,
             "methods": ["POST"], "summary": "还原订阅原来的站点范围"},
            {"path": "/clean", "auth": "bear", "endpoint": self.api_clean,
             "methods": ["POST"], "summary": "清理不合规则的下载任务"},
        ]

    def get_command(self) -> List[Dict[str, Any]]:
        return [{
            "cmd": "/single_source", "event": EventType.PluginAction,
            "desc": "按规则巡检单源追剧", "category": "订阅",
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
        errors: List[str] = list(self._rule_errors)
        try:
            subs = store.subscriptions()
            records = store.downloads()
            sites = store.site_options()
        except Exception as exc:
            subs, records, sites = [], [], []
            errors.append(f"读取宿主数据失败：{type(exc).__name__}")
        snapshot = store.inspect(self._rules, subs, records, sites, errors)
        snapshot["mode"] = self._action_mode
        snapshot["lock_sites"] = self._lock_sites
        snapshot["log"] = store.log_lines(self)
        snapshot["template"] = store.template(self)
        store.save_snapshot(self, snapshot)
        if notify and self._notify and snapshot["stats"]["problems"]:
            self.post_message(
                title="单源追剧",
                text=f"有 {snapshot['stats']['problems']} 条下载不合规则，去详情页看看。",
            )
        return snapshot

    def _page_data(self) -> Dict[str, Any]:
        cached = store.last_snapshot(self)
        if isinstance(cached, dict) and cached.get("checked_at"):
            data = dict(cached)
        else:
            try:
                subs = store.subscriptions()
            except Exception:
                subs = []
            data = store.inspect(self._rules, subs, [], [], self._rule_errors)
            data["checked_at"] = ""
        data["mode"] = self._action_mode
        data["lock_sites"] = self._lock_sites
        data["log"] = store.log_lines(self)
        data["template"] = store.template(self)
        return data

    def _button(self, text: str, action: str, color: str = "primary",
                icon: str = "", params: Optional[Dict[str, Any]] = None,
                confirm: str = "") -> Dict[str, Any]:
        click: Dict[str, Any] = {
            "api": f"plugin/SingleSourceTracker/{action}",
            "method": "post", "params": dict(params or {}),
        }
        if confirm:
            click["confirm"] = confirm
        props: Dict[str, Any] = {
            "color": color, "variant": "tonal", "size": "small", "class": "ma-1 mb-3",
        }
        if icon:
            props["prependIcon"] = icon
        return {"component": "VBtn", "text": text, "props": props, "events": {"click": click}}

    def get_page(self) -> List[Dict[str, Any]]:
        try:
            html = renderer.render(self._page_data())
        except Exception:
            html = '<div class="sst">单源追剧已就绪，点「立即巡检」读取 MP 订阅。</div>'
        buttons = [
            self._button("立即巡检", "check", icon="mdi-refresh"),
            self._button("刷新页面", "refresh", icon="mdi-reload"),
            self._button("生成规则模板", "template", icon="mdi-text-box-outline"),
            self._button("按规则锁定站点", "lock", color="primary", icon="mdi-lock",
                         confirm="把已配规则订阅的站点范围改成规则里的那一站？"),
            self._button("还原站点范围", "unlock", icon="mdi-lock-open-outline",
                         confirm="把所有锁定过的订阅还原成原来的站点范围？"),
            self._button("清理异常任务", "clean", color="error", icon="mdi-delete-outline",
                         params={"confirm": "清理"}, confirm="确认移除不合规则的下载任务？"),
        ]
        return buttons + [{"component": "div", "html": html}]

    def api_snapshot(self) -> Response:
        return Response(success=True, data=self._page_data())

    def api_check(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        data = self._collect()
        return Response(success=True,
                        message=f"读到 {data['stats']['subscriptions']} 条订阅，"
                                f"{data['stats']['problems']} 条下载不合规则",
                        data={"html": renderer.render(data), "stats": data["stats"]})

    def api_refresh(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        return Response(success=True, message="页面已刷新",
                        data={"html": renderer.render(self._page_data())})

    def api_template(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            subs = store.subscriptions()
        except Exception:
            subs = []
        text = rules.template_for(subs, self._rules)
        store.save_template(self, text)
        store.append_log(self, f"生成 {len(text.splitlines())} 行规则模板")
        return Response(success=True, message=f"生成 {len(text.splitlines())} 行，复制去设置页改",
                        data={"template": text, "html": renderer.render(self._page_data())})

    def api_lock(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        if not self._lock_sites:
            return Response(success=False, message="设置里没开「按规则锁定站点范围」")
        try:
            subs = store.subscriptions()
            sites = store.site_options()
            done, messages = store.lock_sites(self, self._rules, subs, sites)
        except Exception as exc:
            return Response(success=False, message=f"锁定失败：{type(exc).__name__}")
        text = f"锁定 {done} 条订阅的站点范围"
        if messages:
            text += "；" + messages[0]
        return Response(success=done > 0, message=text,
                        data={"html": renderer.render(self._collect(notify=False))})

    def api_unlock(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            done, messages = store.unlock_sites(self)
        except Exception as exc:
            return Response(success=False, message=f"还原失败：{type(exc).__name__}")
        text = f"还原了 {done} 条订阅"
        if messages:
            text += "；" + messages[0]
        return Response(success=True, message=text,
                        data={"html": renderer.render(self._collect(notify=False))})

    def api_clean(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        if str((payload or {}).get("confirm") or "") != "清理":
            return Response(success=False, message="请先确认清理")
        if self._action_mode != "remove":
            return Response(success=False, message="当前是只提醒模式，没有移除任何任务")
        data = self._collect(notify=False)
        targets = [item for item in data.get("issues", []) if item.get("hash")][:20]
        done, failed = 0, 0
        for issue in targets:
            ok, note = store.remove_torrent(issue)
            done += 1 if ok else 0
            failed += 0 if ok else 1
            store.append_log(self, f"清理 {issue.get('name')}：{note}")
        return Response(success=True, message=f"移除 {done} 个任务" +
                        (f"，{failed} 个没成功" if failed else ""),
                        data={"html": renderer.render(self._collect(notify=False)),
                              "removed": done})
