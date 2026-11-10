"""在没有 MoviePilot 宿主、没有 pytest 的环境中运行 B5 离线用例。

做法：临时目录里生成 app stub 包，把插件源码复制进
app/plugins/routerosworkbench，注入最小 SDK 桩、app.sdk.plugin.PluginManager
桩和假 RouterOS REST 服务（各分区返回若干行），再直接调用
test_plugin.py 里所有 test_ 函数。

另外执行两项提交门禁：Python 编译、插件版本与 package.v3.json 索引一致。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_FILE = HERE / "test_plugin.py"
REPO = HERE.parents[2]
PLUGIN_SRC = REPO / "plugins.v3" / "routerosworkbench"

CONFTEST = r'''
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.abspath(__file__))

app = types.ModuleType("app")
app.__path__ = [os.path.join(_ROOT, "app")]
sys.modules["app"] = app

schemas = types.ModuleType("schemas")
class Response:
    def __init__(self, success=True, message="", data=None):
        self.success = success
        self.message = message
        self.data = data
schemas.Response = Response
sys.modules["schemas"] = schemas

app_schemas = types.ModuleType("app.schemas")
app_schemas.Response = Response
app_schemas.__path__ = []
sys.modules["app.schemas"] = app_schemas
app.schemas = app_schemas

schemas_types = types.ModuleType("app.schemas.types")
class EventType:
    PluginAction = "plugin.action"
class MessageType:
    Plugin = "plugin"
schemas_types.EventType = EventType
schemas_types.MessageType = MessageType
sys.modules["app.schemas.types"] = schemas_types
app.schemas.types = schemas_types

sdk = types.ModuleType("app.sdk")
sdk.__path__ = []
sys.modules["app.sdk"] = sdk

sdk_logging = types.ModuleType("app.sdk.logging")
class Logger:
    def __init__(self): self.lines = []
    def debug(self, *a, **k): pass
    def info(self, *a, **k): self.lines.append(("INFO", a[0] if a else ""))
    def warning(self, *a, **k): self.lines.append(("WARN", a[0] if a else ""))
    def error(self, *a, **k): self.lines.append(("ERROR", a[0] if a else ""))
sdk_logging.logger = Logger()
sys.modules["app.sdk.logging"] = sdk_logging
sdk.logging = sdk_logging

sdk_events = types.ModuleType("app.sdk.events")
class Event:
    def __init__(self, event_type=None, event_data=None):
        self.event_type = event_type
        self.event_data = event_data or {}
class EventManager:
    @staticmethod
    def register(*args, **kwargs):
        def decorator(func): return func
        return decorator
sdk_events.Event = Event
sdk_events.eventmanager = EventManager()
sys.modules["app.sdk.events"] = sdk_events
sdk.events = sdk_events

sdk_plugin = types.ModuleType("app.sdk.plugin")
class _PluginBase:
    POSTED = []
    CONFIGS = []
    def __init__(self, *args, **kwargs):
        self._data = {}
        self._config = {}
        self._data_path = None
    def get_data(self, key): return self._data.get(key)
    def save_data(self, key, value): self._data[key] = value
    def del_data(self, key): self._data.pop(key, None)
    def update_config(self, config):
        self._config = dict(config or {})
        _PluginBase.CONFIGS.append(dict(config or {}))
        return True
    def get_config(self): return dict(self._config)
    def post_message(self, **kwargs): _PluginBase.POSTED.append(kwargs)
    def get_data_path(self, *args, **kwargs):
        if not self._data_path:
            self._data_path = tempfile.mkdtemp(prefix="b5-data-")
        return self._data_path

# 假插件清单：dict 与对象两种记录混着来，覆盖归一化逻辑
class _ObjPlugin:
    def __init__(self, pid, name, author, version, installed, state, has_update, repo_url=""):
        self.id = pid
        self.plugin_name = name
        self.plugin_author = author
        self.plugin_version = version
        self.installed = installed
        self.state = state
        self.has_update = has_update
        self.repo_url = repo_url

class PluginManager:
    RECORDS = [
        {"id": "RouterOSConnectionCenter", "plugin_name": "RouterOS 连接中心",
         "plugin_author": "BackspaceM", "plugin_version": "1.0.0",
         "installed": True, "state": "enabled", "has_update": False,
         "repo_url": "https://github.com/BackspaceM/MoviePilot-Plugins"},
        _ObjPlugin("RosFirewallManager", "RouterOS 防火墙管理", "BackspaceM",
                   "1.2.0", True, "enabled", True),
        _ObjPlugin("RouterOSConfigGuard", "RouterOS 配置备份", "BackspaceM",
                   "1.0.0", True, "disabled", False),
        _ObjPlugin("BandwidthQueueScheduler", "带宽队列调度", "BackspaceM",
                   "1.1.0", True, "enabled", False),
    ]
    @staticmethod
    def get_installed_plugins():
        return list(PluginManager.RECORDS)
    @staticmethod
    def get_local_plugins():
        return list(PluginManager.RECORDS)
    @staticmethod
    def get_plugin_config(pid):
        return {"enabled": pid != "RouterOSConfigGuard"}
    @staticmethod
    def get_plugin_state(pid):
        return pid != "RouterOSConfigGuard"

sdk_plugin._PluginBase = _PluginBase
sdk_plugin.PluginManager = PluginManager
sys.modules["app.sdk.plugin"] = sdk_plugin
sdk.plugin = sdk_plugin

sdk_network = types.ModuleType("app.sdk.network")
class FakeResponse:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text
    def json(self): return self._json

# 假 RouterOS REST 服务：各分区返回若干行，resource / identity 返回单对象
_SAMPLE_ROWS = {
    "/rest/system/resource": {"uptime": "3d2h10m5s", "version": "7.15.3",
                              "cpu-load": "12", "total-memory": "1073741824",
                              "free-memory": "536870912", "cpu-count": "4",
                              "board-name": "RB5009UG+S+"},
    "/rest/system/identity": {"name": "客厅 RB5009"},
    "/rest/interface": [
        {"name": "ether1", "type": "ether", "rx-byte": "1000", "tx-byte": "500", "running": "true"},
        {"name": "ether2", "type": "ether", "rx-byte": "2000", "tx-byte": "800", "running": "true"},
        {"name": "bridge-lan", "type": "bridge", "rx-byte": "300", "tx-byte": "120", "running": "false"},
    ],
    "/rest/ip/address": [
        {"address": "192.168.88.1/24", "interface": "bridge-lan"},
        {"address": "10.0.0.1/24", "interface": "ether2"},
    ],
    "/rest/ip/firewall/filter": [
        {".id": "*1", "chain": "forward", "action": "accept", "comment": "放行已建立", "disabled": "false"},
        {".id": "*2", "chain": "forward", "action": "drop", "comment": "手工兜底", "disabled": "false"},
        {".id": "*3", "chain": "input", "action": "accept", "comment": "限管理口", "disabled": "true"},
    ],
    "/rest/ip/firewall/nat": [
        {".id": "*1", "chain": "srcnat", "action": "masquerade", "comment": "上网", "disabled": "false"},
    ],
    "/rest/ip/firewall/mangle": [
        {".id": "*1", "chain": "prerouting", "action": "mark-routing",
         "comment": "MP-B3[分流:标记]", "disabled": "false"},
    ],
    "/rest/ip/firewall/address-list": [
        {"address": "1.1.1.1", "list": "MP-A2-CF", "comment": "MP-A2 优选"},
        {"address": "8.8.8.8", "list": "dns", "comment": ""},
    ],
    "/rest/queue/simple": [
        {"name": "客厅", "target": "192.168.88.0/24", "max-limit": "10M/10M",
         "rate": "1M/2M", "disabled": "false"},
        {"name": "下载", "target": "192.168.88.20/32", "max-limit": "5M/5M",
         "rate": "0/0", "disabled": "true"},
    ],
    "/rest/ip/dns/static": [
        {"name": "movie.example.com", "address": "192.168.88.10", "comment": "MP-A2 内网解析"},
    ],
    "/rest/ip/route": [
        {"dst-address": "0.0.0.0/0", "gateway": "192.168.88.254", "distance": "1", "comment": ""},
        {"dst-address": "10.0.0.0/24", "gateway": "ether2", "distance": "1", "comment": "MP-B3 旁路由"},
    ],
    "/rest/file": [
        {"name": "auto-backup.backup", "size": "123456", "date": "2026-11-01 10:00:00"},
        {"name": "export.rsc", "size": "2048", "date": "2026-11-02 08:30:00"},
        {"name": "notes.txt", "size": "10", "date": "2026-11-02 09:00:00"},
    ],
}

def _default_ros_responder(method, url, kwargs):
    from urllib.parse import urlparse
    path = urlparse(url).path
    if path in _SAMPLE_ROWS:
        return FakeResponse(200, _SAMPLE_ROWS[path])
    return FakeResponse(200, [])

class AsyncRequestUtils:
    responder = None
    calls = []
    def __init__(self, **kwargs): self.kwargs = kwargs
    async def request(self, method, url, **kwargs):
        AsyncRequestUtils.calls.append((method, url, kwargs))
        if AsyncRequestUtils.responder is not None:
            return AsyncRequestUtils.responder(method, url, kwargs)
        return FakeResponse(200, [])
sdk_network.AsyncRequestUtils = AsyncRequestUtils
sdk_network.FakeResponse = FakeResponse
sys.modules["app.sdk.network"] = sdk_network
sdk.network = sdk_network
AsyncRequestUtils.responder = _default_ros_responder

# apscheduler 桩：让 get_service 能拿到 CronTrigger.from_crontab
aps = types.ModuleType("apscheduler")
aps.__path__ = []
trig = types.ModuleType("apscheduler.triggers")
trig.__path__ = []
cron = types.ModuleType("apscheduler.triggers.cron")
class CronTrigger:
    @staticmethod
    def from_crontab(expr):
        return ("cron", expr)
cron.CronTrigger = CronTrigger
sys.modules["apscheduler"] = aps
sys.modules["apscheduler.triggers"] = trig
sys.modules["apscheduler.triggers.cron"] = cron
'''


def _version_gate() -> None:
    """plugin_version / 索引 version / 最新 history 必须一致。"""
    init_text = (PLUGIN_SRC / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^\s*plugin_version\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    assert match, "未找到 plugin_version 字面量"
    version = match.group(1)
    import json
    index = json.loads((REPO / "package.v3.json").read_text(encoding="utf-8"))
    item = index.get("RouterOSWorkbench")
    assert item, "package.v3.json 缺少 RouterOSWorkbench"
    assert item["version"] == version, f"索引版本 {item['version']} != {version}"
    assert f"v{version}" in item["history"], "history 缺少当前版本条目"
    print(f"PASS  version_gate (v{version})")


def main() -> int:
    work = tempfile.mkdtemp(prefix="b5-local-")
    try:
        root = Path(work)
        app_dir = root / "app"
        plugins_dir = app_dir / "plugins"
        plugins_dir.mkdir(parents=True)
        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
        shutil.copytree(PLUGIN_SRC, plugins_dir / "routerosworkbench",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "conftest.py").write_text(CONFTEST, encoding="utf-8")
        tests_dir = root / "tests"
        tests_dir.mkdir()
        shutil.copy(TEST_FILE, tests_dir / "test_plugin.py")

        os.chdir(work)
        sys.path.insert(0, work)
        sys.path.insert(0, str(tests_dir))
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ["ROUTEROS_WORKBENCH_REPO"] = str(REPO)

        globals_map = {"__file__": str(root / "conftest.py"), "__name__": "conftest"}
        exec(compile(CONFTEST, globals_map["__file__"], "exec"), globals_map)  # noqa: S102
        sys.modules["pytest"] = types.ModuleType("pytest")

        import test_plugin

        functions = [
            getattr(test_plugin, name)
            for name in sorted(dir(test_plugin))
            if name.startswith("test_") and callable(getattr(test_plugin, name))
        ]
        failed = []
        for func in functions:
            try:
                func()
                print(f"PASS  {func.__name__}")
            except Exception:  # noqa: BLE001
                import traceback
                failed.append(func.__name__)
                print(f"FAIL  {func.__name__}\n{traceback.format_exc()}")
        try:
            _version_gate()
        except Exception:  # noqa: BLE001
            failed.append("version_gate")
            import traceback
            print(f"FAIL  version_gate\n{traceback.format_exc()}")
        print(f"\n合计 {len(functions) + 1} 项，通过 {len(functions) + 1 - len(failed)}，失败 {len(failed)}")
        if failed:
            print("失败项：", ", ".join(failed))
        return 1 if failed else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
