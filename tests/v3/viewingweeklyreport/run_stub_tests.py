"""在没有 MoviePilot 宿主、没有 pytest / APScheduler 的环境中运行离线用例。

做法：临时目录里生成 app stub 包，把插件源码复制进
app/plugins/viewingweeklyreport，注入最小 SDK 桩（含 app.sdk.services 的
MediaServerHelper），再直接调用 test_plugin.py 里所有 test_ 函数。

网络层用"按 URL 分派"的 responder（而非固定响应队列），避免请求顺序错位
导致误判——这是从 RouterOSConfigGuard 的离线测试踩坑中总结的做法。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_FILE = HERE / "test_plugin.py"
PLUGIN_SRC = HERE.parent.parent.parent / "plugins.v3" / "viewingweeklyreport"

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
    def __init__(self, success=False, message="", data=None):
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
        if not getattr(self, "_data_path", None):
            self._data_path = tempfile.mkdtemp(prefix="vwr-data-")
        return self._data_path
sdk_plugin._PluginBase = _PluginBase
sys.modules["app.sdk.plugin"] = sdk_plugin
sdk.plugin = sdk_plugin

sdk_network = types.ModuleType("app.sdk.network")
class FakeResponse:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text
    def json(self): return self._json
class AsyncRequestUtils:
    responder = None   # func(method, url, kwargs) -> FakeResponse 或抛异常
    calls = []
    last_instance = None
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        AsyncRequestUtils.last_instance = self
    async def request(self, method, url, **kwargs):
        AsyncRequestUtils.calls.append((method, url, kwargs))
        if AsyncRequestUtils.responder is not None:
            return AsyncRequestUtils.responder(method, url, kwargs)
        return FakeResponse(200, {})
sdk_network.AsyncRequestUtils = AsyncRequestUtils
sdk_network.FakeResponse = FakeResponse
sys.modules["app.sdk.network"] = sdk_network
sdk.network = sdk_network

sdk_scheduler = types.ModuleType("app.sdk.scheduler")
JOBS = []
def add_plugin_once_job(*args, **kwargs):
    JOBS.append((args, kwargs)); return True
def remove_plugin_once_job(*args, **kwargs): return True
sdk_scheduler.add_plugin_once_job = add_plugin_once_job
sdk_scheduler.remove_plugin_once_job = remove_plugin_once_job
sdk_scheduler.JOBS = JOBS
sys.modules["app.sdk.scheduler"] = sdk_scheduler
sdk.scheduler = sdk_scheduler

# app.sdk.services —— 媒体服务器门面桩（已连接实例下拉数据源）
sdk_services = types.ModuleType("app.sdk.services")
class _Conf:
    def __init__(self, name, type_, url="", apikey="", enabled=True):
        self.name = name; self.type = type_; self.url = url
        self.apikey = apikey; self.enabled = enabled
class _Instance:
    def __init__(self, inactive=False): self._inactive = inactive
    def is_inactive(self): return self._inactive
class _Service:
    def __init__(self, type_, inactive=False):
        self.type = type_; self.config = None; self.instance = _Instance(inactive)
class MediaServerHelper:
    CONFIGS = {
        "客厅Emby": _Conf("客厅Emby", "emby", "http://192.168.1.10:8096", "EMBYKEY123"),
        "书房Plex": _Conf("书房Plex", "plex", "http://192.168.1.11:32400", "PLEXTOKEN456"),
        "Jellyfin测试": _Conf("Jellyfin测试", "jellyfin"),  # 缺 url/key，用于降级用例
        "其它媒体": _Conf("其它媒体", "other", "http://x", "k"),  # 非目标类型，应被过滤
    }
    def get_configs(self): return dict(MediaServerHelper.CONFIGS)
    def get_services(self, name_filters=None, type_filter=None):
        data = {k: v for k, v in MediaServerHelper.CONFIGS.items() if v.type in ("emby", "plex", "jellyfin")}
        if type_filter: data = {k: v for k, v in data.items() if v.type == type_filter}
        if name_filters: data = {k: v for k, v in data.items() if k in name_filters}
        return {k: _Service(v.type) for k, v in data.items()}
sdk_services.MediaServerHelper = MediaServerHelper
sys.modules["app.sdk.services"] = sdk_services
sdk.services = sdk_services
'''


def _inject_apscheduler() -> None:
    aps = types.ModuleType("apscheduler")
    triggers = types.ModuleType("apscheduler.triggers")
    cron = types.ModuleType("apscheduler.triggers.cron")

    class _CronTrigger:
        @staticmethod
        def from_crontab(expression):
            return ("cron", expression)

    cron.CronTrigger = _CronTrigger
    aps.triggers = triggers
    triggers.cron = cron
    sys.modules["apscheduler"] = aps
    sys.modules["apscheduler.triggers"] = triggers
    sys.modules["apscheduler.triggers.cron"] = cron


def main() -> int:
    work = tempfile.mkdtemp(prefix="vwr-local-")
    try:
        root = Path(work)
        app_dir = root / "app"
        plugins_dir = app_dir / "plugins"
        plugins_dir.mkdir(parents=True)
        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
        shutil.copytree(PLUGIN_SRC, plugins_dir / "viewingweeklyreport",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "conftest.py").write_text(CONFTEST, encoding="utf-8")
        tests_dir = root / "tests"
        tests_dir.mkdir()
        shutil.copy(TEST_FILE, tests_dir / "test_plugin.py")

        os.chdir(work)
        sys.path.insert(0, work)
        sys.path.insert(0, str(tests_dir))
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        _inject_apscheduler()

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
        print(f"\n合计 {len(functions)} 个用例，通过 {len(functions) - len(failed)}，失败 {len(failed)}")
        if failed:
            print("失败用例：", ", ".join(failed))
        return 1 if failed else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
