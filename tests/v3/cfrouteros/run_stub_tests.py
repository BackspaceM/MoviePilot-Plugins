"""在没有 MoviePilot 宿主的环境中运行 cfrouteros 离线用例。

做法与其他插件一致：临时目录里生成 app stub 包，把插件源码复制进
app/plugins/cfrouteros，再交给 pytest 运行，避免污染仓库与真实环境。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_FILE = os.path.join(HERE, "test_plugin.py")
PLUGIN_SRC = os.path.abspath(os.path.join(HERE, "..", "..", "..", "plugins.v3", "cfrouteros"))

CONFTEST = r'''
import os
import sys
import types

_ROOT = os.path.dirname(os.path.abspath(__file__))

app = types.ModuleType("app")
app.__path__ = [os.path.join(_ROOT, "app")]
sys.modules["app"] = app

schemas = types.ModuleType("app.schemas")


class Response:
    def __init__(self, success=False, message="", data=None):
        self.success = success
        self.message = message
        self.data = data


schemas.Response = Response
sys.modules["app.schemas"] = schemas

schemas_types = types.ModuleType("app.schemas.types")


class EventType:
    PluginAction = "plugin_action"


schemas_types.EventType = EventType
sys.modules["app.schemas.types"] = schemas_types

sdk = types.ModuleType("app.sdk")
sdk.__path__ = []
sys.modules["app.sdk"] = sdk

sdk_logging = types.ModuleType("app.sdk.logging")


class Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    critical = warning = error = debug = info


sdk_logging.logger = Logger()
sys.modules["app.sdk.logging"] = sdk_logging


class AsyncRequestUtils:
    """测试中禁止真实网络访问。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def request(self, *args, **kwargs):
        raise AssertionError("测试不应访问网络")


class RequestUtils:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_res(self, url, **kwargs):
        raise AssertionError("测试不应访问网络：" + url)


class SitesHelper:
    """返回固定的 PT 站点列表，用于验证自动收集逻辑。"""

    def get_indexers(self):
        return [
            {"domain": "hdwing.org", "public": 0, "is_active": True},
            {"domain": "free.example.com", "public": 1, "is_active": True},
            {"domain": "off.example.com", "public": 0, "is_active": False},
        ]


sdk_network = types.ModuleType("app.sdk.network")
sdk_network.AsyncRequestUtils = AsyncRequestUtils
sdk_network.RequestUtils = RequestUtils
sdk_network.SitesHelper = SitesHelper
sys.modules["app.sdk.network"] = sdk_network


class PluginBase:
    def __init__(self, *args, **kwargs):
        self._store = {}
        self.messages = []

    def get_data(self, key, default=None):
        return self._store.get(key, default)

    def save_data(self, key, value):
        self._store[key] = value

    def post_message(self, **kwargs):
        self.messages.append(kwargs)

    def update_config(self, config, plugin_id=None):
        return True


sdk_plugin = types.ModuleType("app.sdk.plugin")
sdk_plugin._PluginBase = PluginBase
sys.modules["app.sdk.plugin"] = sdk_plugin


class Event:
    def __init__(self, event_type, event_data=None, **kwargs):
        self.event_type = event_type
        self.event_data = event_data or {}


REGISTERED = []


class EventManager:
    def register(self, etype, priority=None):
        def wrapper(func):
            REGISTERED.append((etype, func))
            return func

        return wrapper


sdk_events = types.ModuleType("app.sdk.events")
sdk_events.Event = Event
sdk_events.eventmanager = EventManager()
sys.modules["app.sdk.events"] = sdk_events

JOBS = []


def add_plugin_once_job(plugin_id, job_id, func, name, delay_seconds=0, func_kwargs=None):
    JOBS.append({"plugin_id": plugin_id, "job_id": job_id, "name": name})
    return False


def remove_plugin_once_job(plugin_id, job_id):
    JOBS[:] = [job for job in JOBS if not (job["plugin_id"] == plugin_id and job["job_id"] == job_id)]


sdk_scheduler = types.ModuleType("app.sdk.scheduler")
sdk_scheduler.add_plugin_once_job = add_plugin_once_job
sdk_scheduler.remove_plugin_once_job = remove_plugin_once_job
sys.modules["app.sdk.scheduler"] = sdk_scheduler

plugins_pkg = types.ModuleType("app.plugins")
plugins_pkg.__path__ = [os.path.join(_ROOT, "app", "plugins")]
sys.modules["app.plugins"] = plugins_pkg
'''


def main() -> int:
    work = tempfile.mkdtemp(prefix="cfrouteros-stub-")
    try:
        app_dir = os.path.join(work, "app")
        plugins_dir = os.path.join(app_dir, "plugins")
        os.makedirs(plugins_dir)
        open(os.path.join(app_dir, "__init__.py"), "w").close()
        open(os.path.join(plugins_dir, "__init__.py"), "w").close()
        # 不复制 __pycache__：旧 .pyc 的 mtime 与源一致会被误判为有效，导致跑的是旧代码
        shutil.copytree(PLUGIN_SRC, os.path.join(plugins_dir, "cfrouteros"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        os.makedirs(os.path.join(work, "tests"))
        shutil.copy(TEST_FILE, os.path.join(work, "tests", "test_plugin.py"))
        with open(os.path.join(work, "conftest.py"), "w", encoding="utf-8") as handle:
            handle.write(CONFTEST)

        env = dict(os.environ, PYTHONPATH=work, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.call(
            [sys.executable, "-m", "pytest", "tests", "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=work,
            env=env,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
