"""绿联 NAS 守护：读一段状态 JSON，按几条规则看着下载别出事。"""

import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

try:
    from app.sdk.plugin import _PluginBase
except Exception:  # 离线桩 / 文档检查时也要能加载
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

from . import nas, renderer

_COOLDOWN = 1800
_STATE_KEY = "ugn_state"
_LOG_KEY = "ugn_log"
_SNAPSHOT_KEY = "ugn_snapshot"
_MAX_LOG = 60
_PAUSE_METHODS = ("pause_torrents", "stop_torrents", "pause_all")
_RESUME_METHODS = ("start_torrents", "resume_torrents", "resume_all")


class Response(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None


def _now_text() -> str:
    import datetime

    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("false", "0", "no", "off", "否", "关"):
        return False
    if text in ("true", "1", "yes", "on", "是", "开"):
        return True
    return default


class UGreenGuard(_PluginBase):
    plugin_name = "绿联 NAS 守护"
    plugin_desc = (
        "绿联 NAS 的开放接口是 HTTPS + protobuf，还要按权限节点发令牌，目前令牌只放开了 docker ps，"
        "storage、taskmgr、ctl 都返回 403，MP 容器里不方便直连。这个插件走只读状态桥："
        "放在 NAS 外面的一个只读小服务把状态整理成一段简单 JSON，插件只消费 JSON，"
        "没配桥时用容器内自检兜底。按水位、盘温、容器和 UPS 设几条规则，默认试运行只记录不改动，"
        "可切到自动处置去暂停或恢复下载。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/ugreenguard.png"
    plugin_version = "1.0.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "ugreenguard_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 96

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._bridge_url = ""
        self._status_file = ""
        self._water = 88
        self._temp = 52
        self._mode = "试运行"
        self._interval = 5
        self._watch: List[str] = list(nas.DEFAULT_CONTAINERS)
        self._cooldown = _COOLDOWN

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._enabled = _to_bool(cfg.get("enabled", True))
        self._bridge_url = str(cfg.get("bridge_url") or "").strip()
        self._status_file = str(cfg.get("status_file") or "").strip()
        self._water = _to_int(cfg.get("water_threshold"), 88)
        self._temp = _to_int(cfg.get("temp_threshold"), 52)
        mode = str(cfg.get("mode") or "试运行")
        self._mode = mode if mode in ("仅提醒", "试运行", "自动处置") else "试运行"
        self._interval = max(1, min(59, _to_int(cfg.get("interval_minutes"), 5)))
        watch = cfg.get("watch_containers")
        if isinstance(watch, str) and watch.strip():
            self._watch = [x.strip() for x in watch.replace("，", ",").split(",") if x.strip()]
        elif isinstance(watch, (list, tuple)) and watch:
            self._watch = [str(x).strip() for x in watch if str(x).strip()]
        else:
            self._watch = list(nas.DEFAULT_CONTAINERS)
        self._cooldown = _to_int(cfg.get("cooldown"), _COOLDOWN)

    def get_state(self) -> bool:
        return self._enabled

    def _cfg(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "bridge_url": self._bridge_url,
            "status_file": self._status_file,
            "water_threshold": self._water,
            "temp_threshold": self._temp,
            "mode": self._mode,
            "interval_minutes": self._interval,
            "watch_containers": list(self._watch),
            "cooldown": self._cooldown,
        }

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        expr = f"*/{max(1, min(59, self._interval))} * * * *"
        try:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(expr)
        except Exception:  # 没有调度器时至少把巡检表达式留着
            trigger = expr
        return [{
            "id": "UGreenGuard",
            "name": "绿联 NAS 守护 · 周期巡检",
            "trigger": trigger,
            "func": self.run_scheduled,
            "kwargs": {},
        }]

    def run_scheduled(self) -> None:
        self.run_check(trigger="定时巡检")

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        def col(cols: int, md: int, component: Dict[str, Any]) -> Dict[str, Any]:
            return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": [component]}

        form: List[Dict[str, Any]] = [
            {"component": "VRow", "content": [
                col(12, 6, {"component": "VSwitch", "props": {
                    "model": "enabled", "label": "启用守护", "color": "primary",
                    "hint": "关掉之后定时巡检也会停", "persistentHint": True}}),
                col(12, 6, {"component": "VTextField", "props": {
                    "model": "bridge_url", "label": "状态桥地址",
                    "placeholder": "http://127.0.0.1:8787/status",
                    "hint": "留空则用容器自检", "persistentHint": True, "clearable": True}}),
            ]},
            {"component": "VRow", "content": [
                col(12, 6, {"component": "VSelect", "props": {
                    "model": "water_threshold", "label": "空间到多少就暂停",
                    "items": [{"title": f"{v}%", "value": v} for v in (80, 85, 88, 90, 95)],
                    "hint": "存储卷使用率到了这条线就先停下载", "persistentHint": True}}),
                col(12, 6, {"component": "VSelect", "props": {
                    "model": "temp_threshold", "label": "硬盘到多少度就暂停",
                    "items": [{"title": f"{v}℃", "value": v} for v in (45, 50, 52, 55, 60)],
                    "hint": "温度或 SMART 告警命中时停下载", "persistentHint": True}}),
            ]},
            {"component": "VRow", "content": [
                col(12, 6, {"component": "VSelect", "props": {
                    "model": "mode", "label": "怎么处理",
                    "items": [
                        {"title": "仅提醒", "value": "仅提醒"},
                        {"title": "试运行（只记录）", "value": "试运行"},
                        {"title": "自动处置", "value": "自动处置"},
                    ],
                    "hint": "默认试运行，只记动作和日志，不动下载器", "persistentHint": True}}),
                col(12, 6, {"component": "VSelect", "props": {
                    "model": "interval_minutes", "label": "多久巡检一次",
                    "items": [{"title": f"{v} 分钟", "value": v} for v in (1, 5, 15, 30)],
                    "hint": "同一条规则 30 分钟内不重复提醒", "persistentHint": True}}),
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert", "props": {"type": "info", "variant": "tonal"}, "text": (
                        "插件不直连 NAS，只读外部小服务给的一段 JSON。没配地址时，用容器内看到的目录水位兜底，"
                        "磁盘、容器、UPS 这些没数据就显示未上报。"
                    )},
                ]},
            ]},
        ]
        defaults = {
            "enabled": self._enabled,
            "bridge_url": self._bridge_url,
            "water_threshold": self._water,
            "temp_threshold": self._temp,
            "mode": self._mode,
            "interval_minutes": self._interval,
        }
        return form, defaults

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "当前状态、命中规则与动作日志"},
            {"path": "/check", "auth": "bear", "endpoint": self.api_check,
             "methods": ["POST"], "summary": "立即巡检一次"},
            {"path": "/resume", "auth": "bear", "endpoint": self.api_resume,
             "methods": ["POST"], "summary": "手动恢复下载"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空动作日志（需确认）"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "重新读状态并刷新页面"},
        ]

    def api_snapshot(self) -> Response:
        return Response(success=True, message="ok", data=self._view())

    def api_check(self) -> Response:
        view = self.run_check(trigger="手动巡检")
        return self._payload_from(view, "巡检完成")

    def api_resume(self) -> Response:
        lines = self._call_downloaders(_RESUME_METHODS, "恢复", "该下载器不支持恢复，仅告警")
        text = "；".join(lines)
        self._append_log("INFO", f"[手动] 恢复下载：{text}")
        self._notify("绿联 NAS 守护 · 手动恢复", text)
        self.save_data(_STATE_KEY, {"paused": False, "last_alert": {}, "active_rules": []})
        return self._payload_from(self._refresh_view(), "已执行恢复", result=text)

    def api_clear(self, confirm: str = "") -> Response:
        if str(confirm).strip().lower() not in ("yes", "1", "true", "确认"):
            return Response(success=False, message="需要确认才会清空")
        self.save_data(_LOG_KEY, [])
        return self._payload_from(self._view(), "动作日志已清空")

    def api_refresh(self) -> Response:
        return self._payload_from(self._refresh_view(), "页面已刷新")

    def get_page(self) -> List[Dict[str, Any]]:
        self._ensure_snapshot()
        data = self._view()
        prefix = f"plugin/{self.__class__.__name__}/"

        def button(text: str, icon: str, action: str, method: str,
                   color: str = "primary", params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            click: Dict[str, Any] = {"api": prefix + action, "method": method}
            if params:
                click["params"] = params
            return {
                "component": "VBtn",
                "text": text,
                "props": {"color": color, "variant": "tonal", "size": "small",
                          "class": "me-2 mb-2", "prependIcon": icon},
                "events": {"click": click},
            }

        buttons = [
            button("立即巡检", "mdi-refresh", "check", "post"),
            button("刷新页面", "mdi-page-refresh-outline", "refresh", "post", "info"),
            button("查看快照", "mdi-code-json", "snapshot", "get", "info"),
            button("恢复下载", "mdi-play-outline", "resume", "post", "success"),
            button("清空日志", "mdi-delete-outline", "clear", "post", "warning", {"confirm": "yes"}),
        ]
        return buttons + [{"component": "div", "props": {"class": "ugn-root"}, "html": renderer.render(data)}]

    # 巡检与处置

    def _load_state(self) -> Dict[str, Any]:
        raw = self.get_data(_STATE_KEY)
        state = dict(raw) if isinstance(raw, dict) else {}
        last_alert = state.get("last_alert")
        state["last_alert"] = dict(last_alert) if isinstance(last_alert, dict) else {}
        active = state.get("active_rules")
        state["active_rules"] = list(active) if isinstance(active, (list, tuple)) else []
        return state

    def _append_log(self, level: str, message: str) -> None:
        logs = self.get_data(_LOG_KEY)
        logs = list(logs) if isinstance(logs, list) else []
        logs.insert(0, {"time": _now_text(), "level": level, "message": message})
        self.save_data(_LOG_KEY, logs[:_MAX_LOG])
        try:
            from app.sdk.logging import logger
            getattr(logger, {"WARN": "warning", "ERROR": "error"}.get(level, "info"))(
                f"绿联 NAS 守护：{message}")
        except Exception:
            pass

    def _notify(self, title: str, text: str) -> None:
        try:
            self.post_message(title=title, text=text)
        except Exception:
            pass

    def _downloaders(self) -> List[Any]:
        try:
            from app.sdk.services import DownloaderHelper
            services = DownloaderHelper().get_services() or {}
        except Exception:
            return []
        if isinstance(services, dict):
            return list(services.values())
        if isinstance(services, (list, tuple)):
            return list(services)
        return []

    def _call_downloaders(self, method_names: Tuple[str, ...], word: str,
                          unsupported: str) -> List[str]:
        downloaders = self._downloaders()
        if not downloaders:
            return ["没找到已配置的下载器实例"]
        lines = []
        for index, service in enumerate(downloaders, 1):
            target = None
            for name in method_names:
                fn = getattr(service, name, None)
                if callable(fn):
                    target = (name, fn)
                    break
            if target is None:
                lines.append(unsupported)
                continue
            try:
                target[1]()
                lines.append(f"下载器 {index} 已{word}（{target[0]}）")
            except Exception as err:
                lines.append(f"下载器 {index} {word}失败：{err}")
        return lines

    def _apply_hit(self, hit: Dict[str, Any], cfg: Dict[str, Any], trigger: str) -> None:
        mode = cfg["mode"]
        detail = hit["detail"]
        if hit["action"] == "notify":
            self._append_log("WARN", f"[{mode}] {detail}（触发：{trigger}）")
            self._notify("绿联 NAS 守护 · 容器异常", f"{detail}\n当前处置：{mode}，先记一笔提醒")
            return
        if mode == "仅提醒":
            self._append_log("WARN", f"[仅提醒] 本应暂停下载：{detail}")
            self._notify("绿联 NAS 守护", f"发现异常，按设置只提醒，不改动\n{detail}")
            return
        if mode == "试运行":
            self._append_log("WARN", f"[试运行] 本应暂停下载：{detail}（未实际改动）")
            self._notify("绿联 NAS 守护", f"发现异常，试运行只记录不改动\n{detail}")
            return
        lines = self._call_downloaders(_PAUSE_METHODS, "暂停", "该下载器不支持暂停，仅告警")
        self._append_log("WARN", f"[自动处置] 暂停下载：{detail}｜{'；'.join(lines)}")
        self._notify("绿联 NAS 守护", f"已按规则暂停下载\n{detail}\n{'；'.join(lines)}")

    def _apply_resume(self, cfg: Dict[str, Any], trigger: str) -> None:
        mode = cfg["mode"]
        if mode == "试运行":
            self._append_log("INFO", f"[试运行] 状态恢复正常，本应恢复下载（触发：{trigger}）")
            self._notify("绿联 NAS 守护", "各项状态恢复正常\n试运行只记录，未实际改动")
            return
        lines = self._call_downloaders(_RESUME_METHODS, "恢复", "该下载器不支持恢复，仅告警")
        self._append_log("INFO", f"[自动处置] 恢复下载：{'；'.join(lines)}（触发：{trigger}）")
        self._notify("绿联 NAS 守护", f"各项状态恢复正常，已恢复下载\n{'；'.join(lines)}")

    def run_check(self, trigger: str = "定时巡检") -> Dict[str, Any]:
        cfg = self._cfg()
        collected = nas.collect(cfg)
        status = collected["status"]
        hits = nas.evaluate(status, cfg)
        state = self._load_state()
        last_alert = state["last_alert"]
        paused = bool(state.get("paused"))
        prev_active = set(state["active_rules"])
        now = time.time()
        cooldown = max(0, int(self._cooldown))

        alerts = [h for h in hits if h["action"] in ("pause_download", "notify")]
        current = {h["rule"] for h in alerts}
        fresh = current - prev_active
        pause_alerts = [h for h in alerts if h["action"] == "pause_download"]
        resume_hit = any(h["action"] == "resume_download" for h in hits)

        for hit in alerts:
            if hit["rule"] not in fresh:
                continue
            if now - float(last_alert.get(hit["rule"]) or 0) < cooldown:
                continue
            self._apply_hit(hit, cfg, trigger)
            last_alert[hit["rule"]] = now

        if cfg["mode"] == "仅提醒":
            paused = False
        elif pause_alerts:
            paused = True
        elif resume_hit and not alerts and paused:
            if now - float(last_alert.get("resume") or 0) >= cooldown:
                self._apply_resume(cfg, trigger)
                last_alert["resume"] = now
            paused = False

        self.save_data(_STATE_KEY, {
            "paused": paused,
            "last_alert": last_alert,
            "active_rules": sorted(current),
        })
        self._save_snapshot(status, hits, collected)
        return self._view(status=status, hits=hits, collected=collected)

    def _save_snapshot(self, status: Dict[str, Any], hits: List[Dict[str, Any]],
                       collected: Dict[str, Any]) -> None:
        self.save_data(_SNAPSHOT_KEY, {
            "generated_at": _now_text(),
            "status": status,
            "hits": hits,
            "source": collected["source"],
            "services": collected.get("services"),
        })

    def _refresh_view(self) -> Dict[str, Any]:
        cfg = self._cfg()
        collected = nas.collect(cfg)
        status = collected["status"]
        hits = nas.evaluate(status, cfg)
        self._save_snapshot(status, hits, collected)
        return self._view(status=status, hits=hits, collected=collected)

    def _ensure_snapshot(self) -> None:
        snap = self.get_data(_SNAPSHOT_KEY)
        if not isinstance(snap, dict) or not snap:
            self.run_check(trigger="打开页面")

    def _view(self, status: Any = None, hits: Any = None,
              collected: Any = None) -> Dict[str, Any]:
        stored = self.get_data(_SNAPSHOT_KEY)
        stored = dict(stored) if isinstance(stored, dict) else {}
        if status is None:
            status = stored.get("status") if isinstance(stored.get("status"), dict) else nas.parse_status({})
        if hits is None:
            hits = stored.get("hits") if isinstance(stored.get("hits"), list) else []
        if collected is None:
            collected = {"source": stored.get("source") or nas.MISSING,
                         "services": stored.get("services")}
        state = self._load_state()
        logs = self.get_data(_LOG_KEY)
        logs = list(logs) if isinstance(logs, list) else []
        return {
            "generated_at": stored.get("generated_at") or _now_text(),
            "status": status,
            "hits": hits,
            "source": collected["source"],
            "services": collected.get("services"),
            "log": logs,
            "paused": bool(state.get("paused")),
            "mode": self._mode,
            "cfg": {"water_threshold": self._water, "temp_threshold": self._temp,
                    "watch_containers": list(self._watch)},
            "cooldown": self._cooldown,
        }

    def _payload_from(self, view: Dict[str, Any], message: str, **extra: Any) -> Response:
        data = dict(view)
        data["html"] = renderer.render(view)
        data.update(extra)
        return Response(success=True, message=message, data=data)
