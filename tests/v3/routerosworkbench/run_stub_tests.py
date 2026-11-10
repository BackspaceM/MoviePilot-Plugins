"""RouterOS 统一工作台 —— 离线回归测试运行器（无 pytest / APScheduler / fastapi）。

重点回归两个线上问题：
1. 服务注册失败 `add_job() got multiple values for argument 'trigger'`；
2. 详情页页签按钮 POST body 参数注入失败，永远返回「页签不对」。
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
PLUGIN_SRC = HERE.parent.parent.parent / "plugins.v3" / "routerosworkbench"

CONFTEST = r'''
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.abspath(__file__))

app = types.ModuleType("app")
app.__path__ = [os.path.join(_ROOT, "app")]
sys.modules["app"] = app

schemas = types.ModuleType("app.schemas")
class Response:
    def __init__(self, success=True, message="", data=None):
        self.success = success; self.message = message; self.data = data
schemas.Response = Response
app.schemas = schemas
sys.modules["app.schemas"] = schemas
sys.modules["schemas"] = schemas

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
        self.event_type = event_type; self.event_data = event_data or {}
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
        self._data = {}; self._config = {}
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
            self._data_path = tempfile.mkdtemp(prefix="rfw-data-")
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

sdk_scheduler = types.ModuleType("app.sdk.scheduler")
def remove_plugin_once_job(*args, **kwargs): return True
def add_plugin_once_job(*args, **kwargs): return True
sdk_scheduler.remove_plugin_once_job = remove_plugin_once_job
sdk_scheduler.add_plugin_once_job = add_plugin_once_job
sys.modules["app.sdk.scheduler"] = sdk_scheduler
sdk.scheduler = sdk_scheduler

# 提供最小 fastapi 桩：让 `from fastapi import Body` 成功，从而验证 api_tab
# 确实用 Body(default=None) 声明了 POST body 注入（否则 payload 恒为 None）。
fastapi = types.ModuleType("fastapi")
class _BodyMarker:
    def __init__(self, default=None): self.default = default
    def __repr__(self): return "Body(default=%r)" % (self.default,)
def Body(default=None, **kwargs): return _BodyMarker(default)
fastapi.Body = Body
fastapi._BodyMarker = _BodyMarker
sys.modules["fastapi"] = fastapi
'''


def _inject_apscheduler() -> None:
    aps = types.ModuleType("apscheduler")
    triggers = types.ModuleType("apscheduler.triggers")
    cron = types.ModuleType("apscheduler.triggers.cron")

    class _CronTrigger:
        def __init__(self, expr): self.expr = expr

        @staticmethod
        def from_crontab(expression):
            return _CronTrigger(expression)

        def __repr__(self):
            return f"CronTrigger({self.expr})"

    cron.CronTrigger = _CronTrigger
    aps.triggers = triggers
    triggers.cron = cron
    sys.modules["apscheduler"] = aps
    sys.modules["apscheduler.triggers"] = triggers
    sys.modules["apscheduler.triggers.cron"] = cron


def main() -> int:
    work = tempfile.mkdtemp(prefix="rfw-local-")
    try:
        root = Path(work)
        plugins_dir = root / "app" / "plugins"
        plugins_dir.mkdir(parents=True)
        (root / "app" / "__init__.py").write_text("", encoding="utf-8")
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
