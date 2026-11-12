"""单源追剧：扫订阅、选站点和画质，把规则写进 MP 订阅。"""

from __future__ import annotations

import traceback
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
_APPLIED_KEY = "sst_applied"
_CRON = {15: "*/15 * * * *", 30: "*/30 * * * *", 60: "0 * * * *", 120: "0 */2 * * *"}


def _not_zero(value: Any) -> Any:
    """旧配置里存过 0 当作「未选择」，统一改成空串，别让下拉框显示一个 0。"""
    return "" if value in (None, "", 0, "0") else value


def _why(exc: Exception) -> str:
    # 报错只给类型名没法定位，带上原文再写进 MP 日志，方便事后翻
    text = f"{type(exc).__name__}: {exc}"[:180]
    try:
        from app.sdk.logging import logger

        logger.error(f"单源追剧异常：{text}\n{traceback.format_exc()[-1000:]}")
    except Exception:
        pass
    return text


class Response(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None


class SingleSourceTracker(_PluginBase):
    plugin_name = "单源追剧"
    plugin_desc = (
        "先扫描 MP 当前订阅和站点，设置页里下拉选一部剧、选固定站点、勾画质标签、"
        "填制作组之类的关键词，插件自动生成 MP 订阅能用的包含/排除正则，"
        "点「写进 MP 订阅」就把站点范围与过滤规则写回订阅，MP 选种阶段直接过滤。"
        "会写正则的还能用批量规则表；应用前的原设置会记下来，一键还原。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/singlesourcetracker.png"
    plugin_version = "1.2.2"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "singlesource_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 94

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._cfg: Dict[str, Any] = {}
        self._rules: List[Dict[str, Any]] = []
        self._rule_errors: List[str] = []
        self._apply_to_mp = True
        self._action_mode = "notice"
        self._interval = 30

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._cfg = cfg
        self._enabled = bool(cfg.get("enabled", True))
        self._apply_to_mp = bool(cfg.get("apply_to_mp", True))
        self._action_mode = "remove" if str(cfg.get("action_mode") or "") == "remove" else "notice"
        try:
            self._interval = int(cfg.get("interval") or 30)
        except (TypeError, ValueError):
            self._interval = 30
        if self._interval not in _CRON:
            self._interval = 30
        self._rebuild_rules()

    def _rebuild_rules(self) -> None:
        try:
            cat = store.catalog(self)
            sites = cat.get("sites") if isinstance(cat.get("sites"), list) else []
            line_rules, errors = rules.parse_line_rules(self._cfg.get("rules_text"), sites)
            quick_rules, quick_errors = rules.build_quick_rule(self._cfg, cat)
            self._rules = rules.merge_rules(line_rules, quick_rules)
            self._rule_errors = errors + quick_errors
        except Exception as exc:
            self._rules = []
            self._rule_errors = [f"规则读取失败：{type(exc).__name__}"]

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        return None

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        try:
            cat = store.catalog(self)
            subs = [item for item in (cat.get("subscriptions") or [])
                    if isinstance(item, dict) and item.get("id") is not None]
            sites = [item for item in (cat.get("sites") or [])
                     if isinstance(item, dict) and item.get("id") is not None]
            sub_items = [{"title": f"{item.get('name') or '未命名'}（#{item.get('id')}）",
                          "value": item.get("id")} for item in subs]
            site_items = [{"title": str(item.get("name")), "value": item.get("id")}
                          for item in sites]
            # 空目录就留空下拉，不再塞 value=0 的假选项（界面上会显示成一个「0」）
            sub_hint = ("还没扫描过：去详情页点「扫描 MP 订阅」，也可以直接在这里输入剧名"
                        if not sub_items else "下拉选订阅，也可以直接输入剧名")
            site_hint = ("还没扫描过：去详情页点「扫描 MP 订阅」；也可以先不选，不限站点"
                         if not site_items else "只从这一站下载；清空就是不限站点")

            def col(md, component):
                return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [component]}

            form = [
                {"component": "VRow", "content": [
                    col(6, {"component": "VCombobox", "props": {
                        "model": "quick_sub", "label": "这部剧", "items": sub_items,
                        "clearable": True, "hint": sub_hint, "persistentHint": True}}),
                    col(6, {"component": "VSelect", "props": {
                        "model": "quick_site", "label": "固定站点", "items": site_items,
                        "clearable": True, "hint": site_hint, "persistentHint": True}}),
                ]},
                {"component": "VRow", "content": [
                    col(12, {"component": "VSelect", "props": {
                        "model": "quick_tags", "label": "画质标签（勾就行，不用写正则）",
                        "items": rules.TAG_ITEMS, "multiple": True, "chips": True,
                        "hint": "杜比视界、HDR、H.265、DDP5.1 这些直接勾", "persistentHint": True}}),
                ]},
                {"component": "VRow", "content": [
                    col(6, {"component": "VTextField", "props": {
                        "model": "quick_include", "label": "还要包含",
                        "placeholder": "XX制作组, 简体中字",
                        "hint": "制作组、字幕组之类，逗号分开", "persistentHint": True}}),
                    col(6, {"component": "VTextField", "props": {
                        "model": "quick_exclude", "label": "不要包含",
                        "placeholder": "x264, CAM",
                        "hint": "命中一个就不要；;; 分隔可写 re: 正则", "persistentHint": True}}),
                ]},
                {"component": "VRow", "content": [
                    col(12, {"component": "VTextarea", "props": {
                        "model": "rules_text", "label": "批量规则（会写正则的用，可留空）",
                        "rows": 4, "autoGrow": True, "placeholder": rules.EXAMPLE,
                        "hint": "一行一部剧：剧名 => 站点 => 纳入 => 排除",
                        "persistentHint": True}}),
                ]},
                {"component": "VRow", "content": [
                    col(3, {"component": "VSwitch", "props": {
                        "model": "apply_to_mp", "label": "允许写进订阅",
                        "hint": "关掉就只在本插件里看，不改 MP 订阅"}}),
                    col(3, {"component": "VSwitch", "props": {
                        "model": "enabled", "label": "启用插件"}}),
                    col(3, {"component": "VSelect", "props": {
                        "model": "action_mode", "label": "异常处置",
                        "items": [{"title": "只提醒", "value": "notice"},
                                  {"title": "自动移除任务", "value": "remove"}]}}),
                    col(3, {"component": "VSelect", "props": {
                        "model": "interval", "label": "巡检间隔",
                        "items": [{"title": "15 分钟", "value": 15},
                                  {"title": "30 分钟", "value": 30},
                                  {"title": "1 小时", "value": 60},
                                  {"title": "2 小时", "value": 120}]}}),
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": [
                        {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},                         "text": (
                            "保存后回详情页点「写进 MP 订阅」才会改订阅；应用前的原设置会记下来，"
                            "点「还原订阅设置」可退回。「这部剧」下拉选或直接输入剧名都行，"
                            "扫描后要重开一次设置页，下拉才有新内容。"
                        )},
                    ]},
                ]},
            ]
            defaults = {
                "quick_sub": _not_zero(self._cfg.get("quick_sub")),
                "quick_site": _not_zero(self._cfg.get("quick_site")),
                "quick_tags": self._cfg.get("quick_tags")
                if isinstance(self._cfg.get("quick_tags"), list) else [],
                "quick_include": self._cfg.get("quick_include") or "",
                "quick_exclude": self._cfg.get("quick_exclude") or "",
                "rules_text": self._cfg.get("rules_text") or "",
                "apply_to_mp": self._apply_to_mp,
                "enabled": self._enabled,
                "action_mode": self._action_mode,
                "interval": self._interval,
            }
            return form, defaults
        except Exception:
            return [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
                     "text": "配置暂时读不出来，插件还是能跑；重开一次页面再试。"}], {}

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取最近一次巡检结果"},
            {"path": "/scan", "auth": "bear", "endpoint": self.api_scan,
             "methods": ["POST"], "summary": "扫描 MP 当前订阅与站点"},
            {"path": "/apply", "auth": "bear", "endpoint": self.api_apply,
             "methods": ["POST"], "summary": "把规则写进 MP 订阅"},
            {"path": "/restore", "auth": "bear", "endpoint": self.api_restore,
             "methods": ["POST"], "summary": "还原订阅原来的设置"},
            {"path": "/check", "auth": "bear", "endpoint": self.api_check,
             "methods": ["POST"], "summary": "巡检下载历史"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "重新绘制详情页"},
            {"path": "/template", "auth": "bear", "endpoint": self.api_template,
             "methods": ["POST"], "summary": "生成批量规则模板"},
            {"path": "/clean-preview", "auth": "bear", "endpoint": self.api_clean_preview,
             "methods": ["POST"], "summary": "预览要清理的下载任务"},
            {"path": "/clean", "auth": "bear", "endpoint": self.api_clean,
             "methods": ["POST"], "summary": "确认清理下载任务"},
        ]

    def get_command(self) -> List[Dict[str, Any]]:
        return [{"cmd": "/single_source", "event": EventType.PluginAction,
                 "desc": "扫描并巡检单源追剧", "category": "订阅",
                 "data": {"action": "check"}}]

    def get_service(self) -> List[Dict[str, Any]]:
        try:
            from apscheduler.triggers.cron import CronTrigger

            trigger = CronTrigger.from_crontab(_CRON[self._interval])
        except Exception:
            return []
        return [{"id": "SingleSourceTracker", "name": "单源追剧巡检",
                 "trigger": trigger, "func": self.run_scheduled,
                 "kwargs": {}, "func_kwargs": {}}]

    def run_scheduled(self) -> None:
        if self._enabled:
            self._collect(notify=False)

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
            self._collect(notify=False)
        except Exception:
            return

    def _applied(self) -> List[str]:
        raw = self.get_data(_APPLIED_KEY)
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def _save_applied(self, ids: List[str]) -> None:
        self.save_data(_APPLIED_KEY, [str(item) for item in ids])

    def _collect(self, notify: bool = True) -> Dict[str, Any]:
        errors = list(self._rule_errors)
        try:
            subs = store.subscriptions()
            records = store.downloads()
            sites = store.site_options()
        except Exception as exc:
            subs, records, sites = [], [], []
            errors.append(f"读取宿主数据失败：{type(exc).__name__}")
        snapshot = store.inspect(self._rules, subs, records, sites, self._applied(), errors)
        snapshot["scanned_at"] = str(store.catalog(self).get("scanned_at") or "")
        snapshot["mode"] = self._action_mode
        snapshot["log"] = store.log_lines(self)
        snapshot["template"] = store.template(self)
        store.save_snapshot(self, snapshot)
        if notify and snapshot["stats"]["problems"]:
            try:
                self.post_message(title="单源追剧",
                                  text=f"有 {snapshot['stats']['problems']} 条下载不合规则，去详情页看看。")
            except Exception:
                pass
        return snapshot

    def _page_data(self) -> Dict[str, Any]:
        cached = store.last_snapshot(self)
        cat = store.catalog(self)
        if isinstance(cached, dict) and cached.get("checked_at"):
            data = dict(cached)
        else:
            subs = [item for item in (cat.get("subscriptions") or []) if isinstance(item, dict)]
            sites = [item for item in (cat.get("sites") or []) if isinstance(item, dict)]
            data = store.inspect(self._rules, subs, [], sites, self._applied(), self._rule_errors)
            data["checked_at"] = ""
        data["scanned_at"] = str(cat.get("scanned_at") or "")
        data["mode"] = self._action_mode
        data["log"] = store.log_lines(self)
        data["template"] = store.template(self)
        plan = self.get_data(store.PLAN_KEY)
        if isinstance(plan, dict) and plan.get("id"):
            data["notice"] = f"有 {len(plan.get('targets') or [])} 个任务待确认清理，10 分钟内有效。"
        return data

    def _button(self, text: str, action: str, color: str = "primary",
                icon: str = "", params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        props: Dict[str, Any] = {"color": color, "variant": "tonal", "size": "small",
                                "class": "ma-1 mb-3"}
        if icon:
            props["prependIcon"] = icon
        return {"component": "VBtn", "text": text, "props": props,
                "events": {"click": {"api": f"plugin/SingleSourceTracker/{action}",
                                     "method": "post", "params": dict(params or {})}}}

    def get_page(self) -> List[Dict[str, Any]]:
        try:
            html = renderer.render(self._page_data())
        except Exception:
            html = '<div class="sst">单源追剧已就绪，点「扫描 MP 订阅」开始。</div>'
        buttons = [
            self._button("扫描 MP 订阅", "scan", icon="mdi-magnify"),
            self._button("写进 MP 订阅", "apply", color="primary", icon="mdi-content-save"),
            self._button("还原订阅设置", "restore", icon="mdi-undo"),
            self._button("立即巡检", "check", icon="mdi-refresh"),
            self._button("刷新页面", "refresh", icon="mdi-reload"),
            self._button("生成规则模板", "template", icon="mdi-text-box-outline"),
            self._button("预览清理", "clean-preview", color="warning", icon="mdi-delete-outline"),
        ]
        plan = self.get_data(store.PLAN_KEY)
        if isinstance(plan, dict) and plan.get("id"):
            buttons.append(self._button(
                f"确认清理 {len(plan.get('targets') or [])} 个任务", "clean",
                color="error", icon="mdi-delete",
                params={"plan_id": plan.get("id"), "confirm": "清理"}))
        return buttons + [{"component": "div", "html": html}]

    def api_snapshot(self) -> Response:
        try:
            return Response(success=True, data=self._page_data())
        except Exception as exc:
            return Response(success=False, message=f"读取快照失败：{_why(exc)}")

    def api_scan(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            self._rebuild_rules()
            subs = store.subscriptions()
            sites = store.site_options()
            store.save_catalog(self, subs, sites)
            store.append_log(self, f"扫描到 {len(subs)} 条订阅、{len(sites)} 个站点")
            self._rebuild_rules()
            return Response(success=True,
                            message=f"扫到 {len(subs)} 条订阅、{len(sites)} 个站点，重开设置页就能选",
                            data={"html": renderer.render(self._collect(notify=False))})
        except Exception as exc:
            return Response(success=False, message=f"扫描失败：{_why(exc)}")

    def api_apply(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            if not self._apply_to_mp:
                return Response(success=False, message="设置里关了「允许写进订阅」，没有改动任何订阅")
            self._rebuild_rules()
            if not self._rules:
                return Response(success=False,
                                message="还没配规则：设置页选订阅和站点，或填批量规则表")
            subs = store.subscriptions()
            done, applied, messages = store.apply_rules(self, self._rules, subs)
            if applied:
                merged = list(dict.fromkeys(self._applied() + applied))
                self._save_applied(merged)
            text = f"已写进 {done} 条订阅"
            if messages:
                text += "；" + messages[0]
            return Response(success=done > 0, message=text,
                            data={"html": renderer.render(self._collect(notify=False))})
        except Exception as exc:
            return Response(success=False, message=f"写入订阅失败：{_why(exc)}")

    def api_restore(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            done, messages = store.restore_rules(self)
            self._save_applied([])
            text = f"还原了 {done} 条订阅"
            if messages:
                text += "；" + messages[0]
            return Response(success=True, message=text,
                            data={"html": renderer.render(self._collect(notify=False))})
        except Exception as exc:
            return Response(success=False, message=f"还原失败：{_why(exc)}")

    def api_check(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            self._rebuild_rules()
            data = self._collect()
            return Response(success=True,
                            message=f"读到 {data['stats']['subscriptions']} 条订阅，"
                                    f"{data['stats']['problems']} 条下载不合规则",
                            data={"html": renderer.render(data), "stats": data["stats"]})
        except Exception as exc:
            return Response(success=False, message=f"巡检失败：{_why(exc)}")

    def api_refresh(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            return Response(success=True, message="页面已刷新",
                            data={"html": renderer.render(self._page_data())})
        except Exception as exc:
            return Response(success=False, message=f"刷新失败：{_why(exc)}")

    def api_template(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            cat = store.catalog(self)
            subs = [item for item in (cat.get("subscriptions") or []) if isinstance(item, dict)]
            text = rules.template_for(subs, self._rules)
            store.save_template(self, text)
            store.append_log(self, f"生成 {len(text.splitlines())} 行规则模板")
            return Response(success=True, message=f"生成 {len(text.splitlines())} 行，复制去设置页改",
                            data={"template": text, "html": renderer.render(self._page_data())})
        except Exception as exc:
            return Response(success=False, message=f"生成模板失败：{_why(exc)}")

    def api_clean_preview(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            data = self._collect(notify=False)
            plan = store.make_plan(self, data.get("issues", []))
            count = len(plan.get("targets") or [])
            if not count:
                return Response(success=False, message="没有带任务 hash 的异常，没什么可清理")
            return Response(success=True, message=f"已列出 {count} 个任务，点「确认清理」才会动",
                            data={"plan_id": plan.get("id"), "html": renderer.render(self._page_data())})
        except Exception as exc:
            return Response(success=False, message=f"预览失败：{_why(exc)}")

    def api_clean(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        try:
            body = payload or {}
            if str(body.get("confirm") or "") != "清理":
                return Response(success=False, message="请先点「预览清理」再确认")
            plan = store.take_plan(self, body.get("plan_id"))
            if not plan:
                return Response(success=False, message="清理计划不存在或已过期，重新预览一次")
            if self._action_mode != "remove":
                return Response(success=False, message="当前是只提醒模式，没有移除任何任务")
            done, failed = 0, 0
            for item in plan.get("targets") or []:
                ok, note = store.remove_torrent(item)
                done += 1 if ok else 0
                failed += 0 if ok else 1
                store.append_log(self, f"清理 {item.get('name')}：{note}")
            store.clear_plan(self)
            return Response(success=True,
                            message=f"移除 {done} 个任务" + (f"，{failed} 个没成功" if failed else ""),
                            data={"html": renderer.render(self._collect(notify=False)),
                                  "removed": done})
        except Exception as exc:
            return Response(success=False, message=f"清理失败：{_why(exc)}")
