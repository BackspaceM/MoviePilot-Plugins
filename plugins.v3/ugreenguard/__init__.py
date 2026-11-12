"""绿联 NAS 守护：SSH 连上 NAS 持续读状态，按规则自动处置。

采集走 SSH 复合探针（df / 负载 / 内存 / 容器 / 盘温 / SMART / UPS），
读不到的段落一律标未上报。页面是宿主渲染的静态 HTML，不做脚本轮询；
后台巡检由宿主调度器按秒级间隔驱动，异常处置和页面是否打开无关。
"""

import time
import threading
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

try:
    from app.sdk.plugin import _PluginBase
except Exception:  # 离线桩 / 文档检查时也要能加载
    class _PluginBase:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._data: Dict[str, Any] = {}
            self._config: Dict[str, Any] = {}

        def get_data(self, key: str) -> Any:
            return self._data.get(key)

        def save_data(self, key: str, value: Any) -> None:
            self._data[key] = value

        def update_config(self, config: Dict[str, Any]) -> bool:
            self._config = dict(config or {})
            return True

        def get_config(self, key: str = "") -> Any:
            return self._config.get(key) if key else dict(self._config)

        def post_message(self, **kwargs: Any) -> None:
            return None

from . import nas, notify, renderer

_COOLDOWN = 1800
_STATE_KEY = "ugn_state"
_LOG_KEY = "ugn_log"
_SNAPSHOT_KEY = "ugn_snapshot"
_CLEAR_ARM_KEY = "ugn_clear_arm"
_SERIES_KEY = "ugn_series"
_SSH_TEST_KEY = "ugn_ssh_test"
_NOTIFY_KEY = "ugn_notify"
_MAX_LOG = 60
_MAX_SERIES = 40
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


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class UGreenGuard(_PluginBase):
    plugin_name = "绿联 NAS 守护"
    plugin_desc = (
        "SSH 直连绿联 NAS 秒级巡检：一次连接读回磁盘占用空间、CPU、内存、容器、盘温、SMART 和 UPS，"
        "读不到的段落标未上报，不装作正常。只填账号和密码就能连（私钥可选）；paramiko 或容器自带的 "
        "ssh 命令二选一，密码不会进命令行参数。异常经 MoviePilot 自带通知模块推送到微信 / Telegram / "
        "WebPush 等渠道，试运行和仅提醒也会通知，另有「测试通知」自检。按规则自动暂停/恢复下载、"
        "重启异常容器或执行一条自定义命令；远程动作默认全关，所有动作留日志。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/ugreenguard.png"
    plugin_version = "2.1.0"
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
        self._source_mode = "自动"
        self._ssh_host = ""
        self._ssh_port = 22
        self._ssh_user = ""
        self._ssh_password = ""
        self._ssh_key = ""
        self._ssh_sudo = False
        self._ssh_transport = "自动"
        self._notify_enabled = True
        self._notify_channel = ""
        self._water = 88
        self._temp = 52
        self._cpu = 90
        self._mem = 95
        self._mode = "试运行"
        self._interval = 5
        self._poll_seconds = 30
        self._resume_scope = "已上报"
        self._watch: List[str] = list(nas.DEFAULT_CONTAINERS)
        self._cooldown = _COOLDOWN
        self._allow_remote_action = False
        self._restart_containers = False
        self._alert_command = ""
        self._lock = threading.Lock()

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._enabled = _to_bool(cfg.get("enabled", True))
        self._bridge_url = str(cfg.get("bridge_url") or "").strip()
        self._status_file = str(cfg.get("status_file") or "").strip()
        source_mode = str(cfg.get("source_mode") or "自动").strip()
        self._source_mode = source_mode if source_mode in (
            "自动", "状态桥", "本地文件", "SSH", "容器自检") else "自动"
        raw_host = str(cfg.get("ssh_host") or "").strip()
        # 允许直接写 192.168.1.10:2222，省掉一个端口输入框
        host, port_text = raw_host, ""
        if raw_host.startswith("[") and "]" in raw_host:
            end = raw_host.find("]")
            host = raw_host[1:end]
            port_text = raw_host[end + 1:].lstrip(":")
        elif raw_host.count(":") == 1 and not raw_host.startswith("http"):
            host, port_text = raw_host.rsplit(":", 1)
        self._ssh_host = host.strip()
        self._ssh_port = _clamp(_to_int(cfg.get("ssh_port"), 22), 1, 65535)
        if port_text.strip().isdigit():
            self._ssh_port = _clamp(int(port_text.strip()), 1, 65535)
        self._ssh_user = str(cfg.get("ssh_user") or "").strip()
        self._ssh_password = str(cfg.get("ssh_password") or "")
        self._ssh_key = str(cfg.get("ssh_key") or "").strip()
        self._ssh_sudo = _to_bool(cfg.get("ssh_sudo"), False)
        transport = str(cfg.get("ssh_transport") or "自动").strip()
        self._ssh_transport = transport if transport in nas.TRANSPORT_CHOICES else "自动"
        self._notify_enabled = _to_bool(cfg.get("notify_enabled"), True)
        channel = str(cfg.get("notify_channel") or "").strip()
        self._notify_channel = channel if channel in notify.CHANNEL_VALUES else ""
        self._water = _clamp(_to_int(cfg.get("water_threshold"), 88), 50, 99)
        self._temp = _clamp(_to_int(cfg.get("temp_threshold"), 52), 20, 100)
        self._cpu = _clamp(_to_int(cfg.get("cpu_threshold"), 90), 30, 100)
        self._mem = _clamp(_to_int(cfg.get("mem_threshold"), 95), 30, 100)
        mode = str(cfg.get("mode") or "试运行")
        self._mode = mode if mode in ("仅提醒", "试运行", "自动处置") else "试运行"
        self._interval = max(1, min(59, _to_int(cfg.get("interval_minutes"), 5)))
        self._poll_seconds = _clamp(_to_int(cfg.get("poll_seconds"), 30), 5, 300)
        scope = str(cfg.get("resume_scope") or "已上报").strip()
        self._resume_scope = scope if scope in ("已上报", "严格") else "已上报"
        watch = cfg.get("watch_containers")
        if isinstance(watch, str) and watch.strip():
            self._watch = [x.strip() for x in watch.replace("，", ",").split(",") if x.strip()]
        elif isinstance(watch, (list, tuple)) and watch:
            self._watch = [str(x).strip() for x in watch if str(x).strip()]
        else:
            self._watch = list(nas.DEFAULT_CONTAINERS)
        self._cooldown = max(0, _to_int(cfg.get("cooldown"), _COOLDOWN))
        self._allow_remote_action = _to_bool(cfg.get("allow_remote_action"), False)
        self._restart_containers = _to_bool(cfg.get("restart_containers"), False)
        self._alert_command = str(cfg.get("alert_command") or "").strip()[:300]
        # 连接参数和账号可能改了，旧会话不能复用
        nas.close_session()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        nas.close_session()

    def _cfg(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "bridge_url": self._bridge_url,
            "status_file": self._status_file,
            "source_mode": self._source_mode,
            "ssh_host": self._ssh_host,
            "ssh_port": self._ssh_port,
            "ssh_user": self._ssh_user,
            "ssh_password": self._ssh_password,
            "ssh_key": self._ssh_key,
            "ssh_sudo": self._ssh_sudo,
            "ssh_transport": self._ssh_transport,
            "notify_enabled": self._notify_enabled,
            "notify_channel": self._notify_channel,
            "water_threshold": self._water,
            "temp_threshold": self._temp,
            "cpu_threshold": self._cpu,
            "mem_threshold": self._mem,
            "mode": self._mode,
            "interval_minutes": self._interval,
            "poll_seconds": self._poll_seconds,
            "resume_scope": self._resume_scope,
            "watch_containers": list(self._watch),
            "cooldown": self._cooldown,
            "allow_remote_action": self._allow_remote_action,
            "restart_containers": self._restart_containers,
            "alert_command": self._alert_command,
        }

    def get_service(self) -> List[Dict[str, Any]]:
        """秒级巡检交给宿主调度器，不自己起线程，避免插件重载后线程泄漏。"""
        if not self._enabled:
            return []
        trigger = None
        try:
            from apscheduler.triggers.interval import IntervalTrigger
            trigger = IntervalTrigger(seconds=self._poll_seconds)
        except Exception:
            try:
                from apscheduler.triggers.cron import CronTrigger
                expr = f"*/{max(1, min(59, self._interval))} * * * *"
                trigger = CronTrigger.from_crontab(expr)
            except Exception:
                return []
        return [{
            "id": "UGreenGuard",
            "name": "绿联 NAS 守护 · 实时巡检",
            "trigger": trigger,
            "func": self.run_scheduled,
            "kwargs": {},
        }]

    def run_scheduled(self) -> None:
        # 上一次没跑完就跳过，别让慢探针叠成一串
        if not self._lock.acquire(blocking=False):
            return
        try:
            self.run_check(trigger="实时巡检")
        except Exception as exc:
            self._append_log("ERROR", f"实时巡检异常：{self._scrub(exc)}")
        finally:
            self._lock.release()

    # ---------------- 设置页 ----------------

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        try:
            return self._form()
        except Exception:
            return [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
                     "text": "配置暂时读不出来，插件还是能跑；重开一次页面再试。"}], {}

    def _form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        def col(cols: int, md: int, component: Dict[str, Any]) -> Dict[str, Any]:
            return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": [component]}

        def head(text: str) -> Dict[str, Any]:
            return {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                                      "density": "compact"}, "text": text}]}]}

        def switch(model: str, label: str, hint: str) -> Dict[str, Any]:
            return {"component": "VSwitch", "props": {
                "model": model, "label": label, "color": "primary",
                "hint": hint, "persistentHint": True}}

        def field(model: str, label: str, hint: str, placeholder: str = "",
                  password: bool = False) -> Dict[str, Any]:
            props: Dict[str, Any] = {
                "model": model, "label": label, "hint": hint,
                "persistentHint": True, "clearable": True,
            }
            if placeholder:
                props["placeholder"] = placeholder
            if password:
                props["type"] = "password"
            return {"component": "VTextField", "props": props}

        def select(model: str, label: str, items: List[Dict[str, Any]], hint: str) -> Dict[str, Any]:
            return {"component": "VSelect", "props": {
                "model": model, "label": label, "items": items,
                "hint": hint, "persistentHint": True}}

        form: List[Dict[str, Any]] = [
            head("① 采集方式：只填 ② 的地址、账号和密码就能跑；不填也能用状态桥 / 本地文件 / 容器自检兜底。"),
            {"component": "VRow", "content": [
                col(12, 6, switch("enabled", "启用守护", "关掉之后秒级巡检也会停")),
                col(12, 6, select("source_mode", "状态从哪儿来", [
                    {"title": "自动（SSH 优先，逐级降级）", "value": "自动"},
                    {"title": "SSH", "value": "SSH"},
                    {"title": "状态桥", "value": "状态桥"},
                    {"title": "本地文件", "value": "本地文件"},
                    {"title": "容器自检", "value": "容器自检"},
                ], "默认自动，取到哪层算哪层")),
            ]},
            head("② SSH 连接：只填地址、账号、密码就能连，不需要私钥。密码或私钥二选一，"
                 "都只存插件配置，不写日志、不进快照、不出现在进程命令行参数里。"),
            {"component": "VRow", "content": [
                col(12, 6, field("ssh_host", "SSH 地址",
                                 "默认 22 端口；别的端口写成 192.168.1.10:2222", "192.168.1.10")),
                col(12, 6, field("ssh_user", "SSH 账号", "能跑 df 的账号就够")),
            ]},
            {"component": "VRow", "content": [
                col(12, 6, field("ssh_password", "SSH 密码",
                                 "填密码即可登录，不需要私钥；不会写进日志或快照", "", True)),
                col(12, 6, {"component": "VTextarea", "props": {
                    "model": "ssh_key", "label": "SSH 私钥（可选）",
                    "placeholder": "-----BEGIN OPENSSH PRIVATE KEY-----",
                    "rows": 2, "autoGrow": True,
                    "hint": "填了就优先用私钥；密码留空时必填这一项",
                    "persistentHint": True}}),
            ]},
            {"component": "VRow", "content": [
                col(12, 12, select("ssh_transport", "用哪种方式连 SSH", [
                    {"title": "自动（优先 paramiko，没有就用系统 ssh 命令）", "value": "自动"},
                    {"title": "paramiko（需容器里装了 paramiko）", "value": "paramiko"},
                    {"title": "系统 ssh 命令（密码走 sshpass / askpass，或填私钥）", "value": "ssh 命令"},
                ], "两种实现都支持密码登录；点「测试连接」能看到实际用了哪条")),
            ]},
            head("③ 巡检与阈值：后台按间隔持续采集，页面点「刷新页面」看最新一帧。"),
            {"component": "VRow", "content": [
                col(12, 6, select("poll_seconds", "巡检间隔", [
                    {"title": "10 秒", "value": 10},
                    {"title": "15 秒", "value": 15},
                    {"title": "30 秒（推荐）", "value": 30},
                    {"title": "60 秒", "value": 60},
                    {"title": "120 秒", "value": 120},
                ], "由宿主调度器驱动，不依赖页面是否打开")),
                col(12, 6, select("mode", "怎么处理", [
                    {"title": "仅提醒", "value": "仅提醒"},
                    {"title": "试运行（只记录）", "value": "试运行"},
                    {"title": "自动处置", "value": "自动处置"},
                ], "默认试运行，只记动作和日志")),
            ]},
            {"component": "VRow", "content": [
                col(12, 6, select("water_threshold", "磁盘占用到多少就暂停", [
                    {"title": f"{v}%", "value": v} for v in (80, 85, 88, 90, 95)
                ], "取所有卷里最高的那个")),
                col(12, 6, select("temp_threshold", "盘温到多少算异常", [
                    {"title": f"{v}℃", "value": v} for v in (45, 50, 52, 55, 60)
                ], "SMART 告警单独算异常")),
            ]},
            head("④ 自动处置：默认全关，关注的容器是 moviepilot / qbittorrent / transmission / emby / plex。"
                 "远程动作会在 NAS 上真执行命令，先看清风险再开。"),
            {"component": "VRow", "content": [
                col(12, 6, switch("allow_remote_action", "允许 SSH 远程动作",
                                  "总开关：关着时只会提醒和暂停下载")),
                col(12, 6, switch("restart_containers", "容器挂了自动重启",
                                  "只对关注的容器执行 docker restart")),
            ]},
            {"component": "VRow", "content": [
                col(12, 12, field("alert_command", "异常时执行的自定义命令（可留空）",
                                  "留空最安全；只放行单条简单命令，禁止管道、重定向、删除和关机类命令",
                                  "/volume1/scripts/notify.sh")),
            ]},
            head("⑤ 通知：走 MoviePilot 自带的通知模块（微信 / Telegram / WebPush 等由 MP 设置决定），"
                 "仅提醒和试运行命中时也会发。点详情页「测试通知」可以立刻验证通道。"),
            {"component": "VRow", "content": [
                col(12, 6, switch("notify_enabled", "发送 NAS 异常通知",
                                  "关掉后巡检照跑，只是不再推送")),
                col(12, 6, select("notify_channel", "发到哪个渠道", [
                    {"title": title, "value": value} for value, title in notify.CHANNELS
                ], "默认跟随 MP 设置；指定渠道只发给该渠道")),
            ]},
            head("⑥ 备用数据源（可留空）：SSH 连不上时按状态桥 → 本地文件 → 容器自检降级。"),
            {"component": "VRow", "content": [
                col(12, 6, field("bridge_url", "状态桥地址", "留空就跳过状态桥",
                                 "http://127.0.0.1:8787/status")),
                col(12, 6, field("status_file", "本地状态文件", "选本地文件时读这个 JSON",
                                 "/config/ugn_status.json")),
            ]},
        ]
        defaults = {
            "enabled": self._enabled,
            "source_mode": self._source_mode,
            "ssh_host": self._ssh_host,
            "ssh_port": str(self._ssh_port),
            "ssh_user": self._ssh_user,
            "ssh_password": self._ssh_password,
            "ssh_key": self._ssh_key,
            "ssh_sudo": self._ssh_sudo,
            "ssh_transport": self._ssh_transport,
            "notify_enabled": self._notify_enabled,
            "notify_channel": self._notify_channel,
            "poll_seconds": self._poll_seconds,
            "water_threshold": self._water,
            "temp_threshold": self._temp,
            "cpu_threshold": self._cpu,
            "mem_threshold": self._mem,
            "mode": self._mode,
            "resume_scope": self._resume_scope,
            "interval_minutes": self._interval,
            "watch_containers": list(self._watch),
            "cooldown": self._cooldown,
            "allow_remote_action": self._allow_remote_action,
            "restart_containers": self._restart_containers,
            "alert_command": self._alert_command,
            "bridge_url": self._bridge_url,
            "status_file": self._status_file,
        }
        return form, defaults

    # ---------------- 接口与页面 ----------------

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "当前状态、命中规则与动作日志"},
            {"path": "/check", "auth": "bear", "endpoint": self.api_check,
             "methods": ["POST"], "summary": "立即巡检一次"},
            {"path": "/test_ssh", "auth": "bear", "endpoint": self.api_test_ssh,
             "methods": ["POST"], "summary": "分步测试 SSH 连接与状态探针"},
            {"path": "/test_notify", "auth": "bear", "endpoint": self.api_test_notify,
             "methods": ["POST"], "summary": "发一条测试通知，验证 MP 通知模块"},
            {"path": "/resume", "auth": "bear", "endpoint": self.api_resume,
             "methods": ["POST"], "summary": "手动恢复下载"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空动作日志（需确认）"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "重新读状态并刷新页面"},
        ]

    def _scrub(self, text: Any) -> str:
        out = str(text or "")
        for secret in (self._ssh_password, self._ssh_key):
            value = str(secret or "").strip()
            if not value:
                continue
            out = out.replace(value, "***")
            for line in value.splitlines():
                line = line.strip()
                if len(line) > 8:
                    out = out.replace(line, "***")
        return out

    def _fail(self, exc: Exception) -> Response:
        """endpoint 里的异常一律收成具体原因，绝不抛到宿主变成「未知错误」。"""
        return Response(success=False, message=f"具体原因：{self._scrub(exc)}", data=None)

    def api_snapshot(self) -> Response:
        try:
            return Response(success=True, message="ok", data=self._view())
        except Exception as exc:
            return self._fail(exc)

    def api_check(self) -> Response:
        try:
            view = self.run_check(trigger="手动巡检")
            return self._payload_from(view, "巡检完成")
        except Exception as exc:
            return self._fail(exc)

    def api_test_ssh(self) -> Response:
        try:
            report = nas.ssh_diagnostics(self._cfg())
            self.save_data(_SSH_TEST_KEY, report)
            summary = str(report.get("summary") or "测试完成")
            self._append_log("INFO" if report.get("ok") else "WARN", f"[连接测试] {summary}")
            return self._payload_from(self._view(), summary, ssh_test=report)
        except Exception as exc:
            return self._fail(exc)

    def api_test_notify(self) -> Response:
        """不依赖异常状态，直接发一条测试消息，用来确认 MP 通知模块通不通。"""
        try:
            channel = self._notify_channel
            label = notify.channel_label(channel)
            title = "绿联 NAS 守护 · 通知测试"
            text = (
                "这是一条测试消息，用来确认 MP 通知模块是否打通。\n"
                f"时间：{_now_text()}\n"
                f"渠道：{label}\n"
                "消息类型：插件（MessageType.Plugin）\n"
                f"当前处置：{self._mode}\n"
                "收到说明通道正常；没收到请检查 MP「设置 → 通知」里渠道是否启用、"
                "插件类消息的开关与收件人。"
            )
            result = notify.send(self, title, text, channel)
            result["test"] = True
            self.save_data(_NOTIFY_KEY, result)
            ok = bool(result.get("ok"))
            self._append_log("INFO" if ok else "ERROR",
                             f"[通知测试] {'已提交给 MP 通知模块' if ok else '调用失败'}："
                             f"{result.get('via') or result.get('error') or ''}")
            summary = ("已提交给 MP 通知模块，请去看微信 / Telegram / WebPush 有没有收到"
                       if ok else f"通知调用失败：{result.get('error') or '未知原因'}")
            return self._payload_from(self._view(), summary, notify_test=result)
        except Exception as exc:
            return self._fail(exc)

    def api_resume(self) -> Response:
        try:
            lines = self._call_downloaders(_RESUME_METHODS, "恢复", "该下载器不支持恢复，仅告警")
            text = "；".join(lines)
            self._append_log("INFO", f"[手动] 恢复下载：{text}")
            self._notify("绿联 NAS 守护 · 手动恢复", text)
            self.save_data(_STATE_KEY, {"paused": False, "last_alert": {}, "active_rules": []})
            return self._payload_from(self._refresh_view(), "已执行恢复", result=text)
        except Exception as exc:
            return self._fail(exc)

    def api_clear(self, confirm: str = "") -> Response:
        try:
            # 详情页 events.click.confirm 不弹框，别指望它；用 params 口令加两段式后端校验
            token = str(confirm).strip().lower()
            if token not in ("yes", "1", "true", "确认"):
                return Response(success=False, message="需要确认才会清空")
            arm = self.get_data(_CLEAR_ARM_KEY)
            armed = isinstance(arm, dict) and time.time() - float(arm.get("ts") or 0) < 60
            if not armed:
                self.save_data(_CLEAR_ARM_KEY, {"ts": time.time()})
                return Response(success=False, message="再点一次「清空日志」才会真的清空")
            self.save_data(_CLEAR_ARM_KEY, None)
            self.save_data(_LOG_KEY, [])
            return self._payload_from(self._view(), "动作日志已清空")
        except Exception as exc:
            return self._fail(exc)

    def api_refresh(self) -> Response:
        try:
            return self._payload_from(self._refresh_view(), "页面已刷新")
        except Exception as exc:
            return self._fail(exc)

    def get_page(self) -> List[Dict[str, Any]]:
        try:
            return self._page()
        except Exception:
            return [{"component": "div", "props": {"class": "ugn-root"},
                     "html": "<div class='ugn'>页面暂时渲染不出来，重开一次详情页再试。</div>"}]

    def _page(self) -> List[Dict[str, Any]]:
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
            button("测试连接", "mdi-lan-connect", "test_ssh", "post", "info"),
            button("测试通知", "mdi-bell-ring-outline", "test_notify", "post", "info"),
            button("查看快照", "mdi-code-json", "snapshot", "get", "info"),
            button("恢复下载", "mdi-play-outline", "resume", "post", "success"),
            button("清空日志", "mdi-delete-outline", "clear", "post", "warning", {"confirm": "yes"}),
        ]
        body: Dict[str, Any] = {"component": "div", "props": {"class": "ugn-root"},
                                "html": renderer.render(self._view())}
        return buttons + [body]

    # ---------------- 数据与日志 ----------------

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
        logs.insert(0, {"time": _now_text(), "level": level, "message": self._scrub(message)})
        self.save_data(_LOG_KEY, logs[:_MAX_LOG])
        try:
            from app.sdk.logging import logger
            getattr(logger, {"WARN": "warning", "ERROR": "error"}.get(level, "info"))(
                f"绿联 NAS 守护：{self._scrub(message)}")
        except Exception:
            pass

    def _notify(self, title: str, text: str) -> None:
        """把异常交给 MP 自带通知模块；仅提醒 / 试运行时同样会调，用来验证通道。

        这里不额外写日志，免得一次告警记两条，影响冷却和日志计数；
        发送回执单独存起来，在详情页「通知」区块展示。
        """
        if not self._notify_enabled:
            return
        try:
            result = notify.send(self, title, self._scrub(text), self._notify_channel)
            self.save_data(_NOTIFY_KEY, result)
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
                lines.append(f"下载器 {index} {word}失败：{self._scrub(err)}")
        return lines

    # ---------------- 规则动作 ----------------

    def _planned_actions(self, hit: Dict[str, Any], cfg: Dict[str, Any]) -> str:
        parts: List[str] = []
        if hit["action"] == "pause_download":
            parts.append("暂停下载器任务")
        if cfg.get("allow_remote_action"):
            if cfg.get("restart_containers") and hit["rule"] == "container_down":
                for name in [str(x) for x in (hit.get("targets") or [])[:3]]:
                    command = nas.restart_command(cfg, name)
                    if command:
                        parts.append(f"SSH 执行 {command}")
            command = str(cfg.get("alert_command") or "").strip()
            if command and hit["action"] in ("pause_download", "notify"):
                parts.append(f"SSH 执行 {command}")
        return "、".join(parts) or "只记一笔提醒"

    def _action_result(self, cfg: Dict[str, Any], command: str) -> Dict[str, Any]:
        """普通账号跑不动 docker 时，自动重试一次免密 sudo，不额外要一堆配置。"""
        result = nas.run_remote_action(cfg, command)
        blob = f"{result.get('error') or ''} {result.get('output') or ''}".lower()
        if (not result.get("ok") and not command.startswith("sudo -n ")
                and ("permission" in blob or "denied" in blob or "not authorized" in blob)):
            retry = nas.run_remote_action(cfg, "sudo -n " + command)
            if retry.get("ok"):
                return retry
        return result

    def _remote_actions(self, hit: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
        lines: List[str] = []
        if not cfg.get("allow_remote_action"):
            return lines
        if cfg.get("restart_containers") and hit["rule"] == "container_down":
            for name in [str(x) for x in (hit.get("targets") or [])[:3]]:
                command = nas.restart_command(cfg, name)
                if not command:
                    lines.append(f"容器名 {name} 不合法，跳过重启")
                    continue
                result = self._action_result(cfg, command)
                detail = result["output"] if result["ok"] else (result["error"] or "没有返回")
                lines.append(f"重启 {name}：{'成功' if result['ok'] else '失败'}（{detail}）")
        command = str(cfg.get("alert_command") or "").strip()
        if command and hit["action"] in ("pause_download", "notify"):
            result = self._action_result(cfg, command)
            detail = result["output"] if result["ok"] else (result["error"] or "没有返回")
            lines.append(f"自定义命令：{'成功' if result['ok'] else '失败'}（{detail}）")
        return lines

    def _apply_hit(self, hit: Dict[str, Any], cfg: Dict[str, Any], trigger: str) -> None:
        mode = cfg["mode"]
        detail = hit["detail"]
        plan = self._planned_actions(hit, cfg)
        if hit["action"] == "notify":
            if mode == "自动处置" and cfg.get("allow_remote_action"):
                lines = self._remote_actions(hit, cfg)
                text = "；".join(lines) if lines else "未配置远程动作"
                self._append_log("WARN", f"[自动处置] {detail}｜{text}（触发：{trigger}）")
                self._notify("绿联 NAS 守护", f"按规则处理容器/资源异常\n{detail}\n{text}")
            else:
                self._append_log("WARN", f"[{mode}] {detail}｜计划：{plan}（触发：{trigger}）")
                self._notify("绿联 NAS 守护", f"发现异常，当前处置：{mode}\n{detail}\n计划：{plan}")
            return
        if mode == "仅提醒":
            self._append_log("WARN", f"[仅提醒] 本应暂停下载：{detail}｜计划：{plan}")
            self._notify("绿联 NAS 守护", f"发现异常，按设置只提醒，不改动\n{detail}")
            return
        if mode == "试运行":
            self._append_log("WARN", f"[试运行] 本应暂停下载：{detail}｜计划：{plan}（未实际改动）")
            self._notify("绿联 NAS 守护", f"发现异常，试运行只记录不改动\n{detail}\n计划：{plan}")
            return
        lines = self._call_downloaders(_PAUSE_METHODS, "暂停", "该下载器不支持暂停，仅告警")
        lines += self._remote_actions(hit, cfg)
        text = "；".join(lines)
        self._append_log("WARN", f"[自动处置] 暂停下载：{detail}｜{text}（触发：{trigger}）")
        self._notify("绿联 NAS 守护", f"已按规则暂停下载\n{detail}\n{text}")

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
        self._save_series(status, hits, collected.get("source"))
        return self._view(status=status, hits=hits, collected=collected)

    def _save_snapshot(self, status: Dict[str, Any], hits: List[Dict[str, Any]],
                       collected: Dict[str, Any]) -> None:
        cfg = self._cfg()
        ssh = collected.get("ssh") if isinstance(collected.get("ssh"), dict) \
            else nas.ssh_info(cfg)
        self.save_data(_SNAPSHOT_KEY, {
            "generated_at": _now_text(),
            "last_check_epoch": time.time(),
            "status": status,
            "hits": hits,
            "source": collected.get("source"),
            "services": collected.get("services"),
            "ssh": ssh,
            "probe": collected.get("probe"),
            "source_mode": collected.get("source_mode") or self._source_mode,
            "poll_seconds": self._poll_seconds,
        })

    def _save_series(self, status: Dict[str, Any], hits: List[Dict[str, Any]],
                     source: Any) -> None:
        rows = self.get_data(_SERIES_KEY)
        rows = list(rows) if isinstance(rows, list) else []
        volumes = status.get("volumes") if isinstance(status.get("volumes"), list) else []
        disks = status.get("disks") if isinstance(status.get("disks"), list) else []
        system = status.get("system") if isinstance(status.get("system"), dict) else {}

        def pick(items: List[Any], key: str) -> Any:
            values = []
            for item in items:
                if isinstance(item, dict) and isinstance(item.get(key), (int, float)) \
                        and not isinstance(item.get(key), bool):
                    values.append(float(item[key]))
            return round(max(values), 1) if values else None

        rows.append({
            "time": _now_text(),
            "epoch": time.time(),
            "volume": pick(volumes, "percent"),
            "temp": pick(disks, "temp"),
            "cpu": system.get("cpu") if isinstance(system.get("cpu"), (int, float)) else None,
            "mem": system.get("mem") if isinstance(system.get("mem"), (int, float)) else None,
            "alerts": len([h for h in hits if isinstance(h, dict)
                           and h.get("action") in ("pause_download", "notify")]),
            "source": str(source or nas.MISSING),
        })
        self.save_data(_SERIES_KEY, rows[-_MAX_SERIES:])

    def _refresh_view(self) -> Dict[str, Any]:
        cfg = self._cfg()
        collected = nas.collect(cfg)
        status = collected["status"]
        hits = nas.evaluate(status, cfg)
        self._save_snapshot(status, hits, collected)
        return self._view(status=status, hits=hits, collected=collected)

    def _ensure_snapshot(self) -> None:
        # 页面加载不做采集，状态一律等用户点巡检或定时任务来更新
        return None

    def _view(self, status: Any = None, hits: Any = None,
              collected: Any = None) -> Dict[str, Any]:
        cfg = self._cfg()
        stored = self.get_data(_SNAPSHOT_KEY)
        stored = dict(stored) if isinstance(stored, dict) else {}
        if status is None:
            status = stored.get("status") if isinstance(stored.get("status"), dict) else nas.parse_status({})
        if hits is None:
            hits = stored.get("hits") if isinstance(stored.get("hits"), list) else []
        if collected is None:
            collected = {
                "source": stored.get("source") or nas.MISSING,
                "services": stored.get("services"),
                "ssh": stored.get("ssh") if isinstance(stored.get("ssh"), dict)
                       else nas.ssh_info(cfg),
                "probe": stored.get("probe"),
                "source_mode": stored.get("source_mode") or self._source_mode,
            }
        state = self._load_state()
        logs = self.get_data(_LOG_KEY)
        logs = list(logs) if isinstance(logs, list) else []
        series = self.get_data(_SERIES_KEY)
        series = list(series) if isinstance(series, list) else []
        ssh_test = self.get_data(_SSH_TEST_KEY)
        ssh_test = dict(ssh_test) if isinstance(ssh_test, dict) else {}
        notify_result = self.get_data(_NOTIFY_KEY)
        notify_result = dict(notify_result) if isinstance(notify_result, dict) else {}
        ssh = collected.get("ssh") if isinstance(collected.get("ssh"), dict) \
            else nas.ssh_info(cfg)
        last_epoch = stored.get("last_check_epoch")
        try:
            age = max(0, int(time.time() - float(last_epoch))) if last_epoch else None
        except (TypeError, ValueError):
            age = None
        return {
            "generated_at": stored.get("generated_at") or _now_text(),
            "status": status,
            "hits": hits,
            "source": collected.get("source") or nas.MISSING,
            "services": collected.get("services"),
            "ssh": ssh,
            "probe": collected.get("probe"),
            "source_mode": collected.get("source_mode") or self._source_mode,
            "log": logs,
            "series": series,
            "ssh_test": ssh_test,
            "notify": notify_result,
            "notify_enabled": self._notify_enabled,
            "notify_channel": self._notify_channel,
            "notify_channel_label": notify.channel_label(self._notify_channel),
            "notify_services": notify.services(),
            "age_seconds": age,
            "poll_seconds": self._poll_seconds,
            "paused": bool(state.get("paused")),
            "mode": self._mode,
            "cfg": {"water_threshold": self._water, "temp_threshold": self._temp,
                    "watch_containers": list(self._watch)},
            "cooldown": self._cooldown,
            "actions": {
                "mode": self._mode,
                "allow_remote_action": self._allow_remote_action,
                "restart_containers": self._restart_containers,
                "alert_command": self._alert_command,
                "resume_scope": self._resume_scope,
            },
        }

    def _payload_from(self, view: Dict[str, Any], message: str, **extra: Any) -> Response:
        data = dict(view)
        data["html"] = renderer.render(view)
        data.update(extra)
        return Response(success=True, message=message, data=data)
