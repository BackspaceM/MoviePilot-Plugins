#!/usr/bin/env python3
"""在无宿主环境下运行本插件测试：临时生成 app stub 包，避免污染仓库。

用法：python3 tests/v3/namecrossseed/run_stub_tests.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_SRC = os.path.abspath(os.path.join(HERE, "../../../plugins.v3/namecrossseed"))
TEST_FILE = os.path.join(HERE, "test_plugin.py")

CONFTEST = '''"""为插件测试注入最小宿主 stub：只覆盖 SDK 契约面，不含宿主实现。"""

import os
import sys
import types

_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
app = types.ModuleType("app")
app.__path__ = [_APP]
plugins_pkg = types.ModuleType("app.plugins")
plugins_pkg.__path__ = [os.path.join(_APP, "plugins")]
app.plugins = plugins_pkg


class Response:
    def __init__(self, success=False, message="", data=None):
        self.success = success
        self.message = message
        self.data = data


schemas = types.ModuleType("app.schemas")
schemas.Response = Response
app.schemas = schemas
schemas_types = types.ModuleType("app.schemas.types")


class EventType:
    PluginAction = "plugin.action"


schemas_types.EventType = EventType
schemas.types = schemas_types

sdk = types.ModuleType("app.sdk")
sdk.__path__ = []


class Logger:
    def info(self, msg):
        pass

    warning = error = debug = info


sdk_logging = types.ModuleType("app.sdk.logging")
sdk_logging.logger = Logger()


class RequestUtils:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_res(self, url, **kwargs):
        raise AssertionError("测试不应访问网络：" + url)


sdk_network = types.ModuleType("app.sdk.network")
sdk_network.RequestUtils = RequestUtils


class PluginBase:
    def __init__(self, *args, **kwargs):
        self._store = {}
        self.messages = []

    def get_data(self, key):
        return self._store.get(key)

    def save_data(self, key, value):
        self._store[key] = value

    def post_message(self, **kwargs):
        self.messages.append(kwargs)

    def update_config(self, config, plugin_id=None):
        return True


sdk_plugin = types.ModuleType("app.sdk.plugin")
sdk_plugin._PluginBase = PluginBase


class DownloaderHelper:
    def __init__(self, *args, **kwargs):
        pass

    def get_configs(self):
        return {"qb.main": object()}

    def get_services(self, name_filters=None):
        return {}


sdk_services = types.ModuleType("app.sdk.services")
sdk_services.DownloaderHelper = DownloaderHelper

REGISTERED = []


class Event:
    def __init__(self, event_type, event_data=None, **kwargs):
        self.event_type = event_type
        self.event_data = event_data or {}


class EventManager:
    def register(self, etype, priority=None):
        def wrapper(func):
            REGISTERED.append((etype, func))
            return func

        return wrapper


sdk_events = types.ModuleType("app.sdk.events")
sdk_events.Event = Event
sdk_events.eventmanager = EventManager()

JOBS = []


def add_plugin_once_job(plugin_id, job_id, func, name, delay_seconds=0, func_kwargs=None):
    JOBS.append({"plugin_id": plugin_id, "job_id": job_id, "name": name})
    return False


def remove_plugin_once_job(plugin_id, job_id):
    JOBS[:] = [job for job in JOBS if not (job["plugin_id"] == plugin_id and job["job_id"] == job_id)]


sdk_scheduler = types.ModuleType("app.sdk.scheduler")
sdk_scheduler.add_plugin_once_job = add_plugin_once_job
sdk_scheduler.remove_plugin_once_job = remove_plugin_once_job

sys.modules.update({
    "app": app,
    "app.schemas": schemas,
    "app.schemas.types": schemas_types,
    "app.sdk": sdk,
    "app.sdk.logging": sdk_logging,
    "app.sdk.network": sdk_network,
    "app.sdk.plugin": sdk_plugin,
    "app.sdk.services": sdk_services,
    "app.sdk.events": sdk_events,
    "app.sdk.scheduler": sdk_scheduler,
    "app.plugins": plugins_pkg,
})
'''


def main() -> int:
    work = tempfile.mkdtemp(prefix="namecrossseed-stub-")
    try:
        app_dir = os.path.join(work, "app")
        plugins_dir = os.path.join(app_dir, "plugins")
        os.makedirs(plugins_dir)
        open(os.path.join(app_dir, "__init__.py"), "w").close()
        open(os.path.join(plugins_dir, "__init__.py"), "w").close()
        shutil.copytree(PLUGIN_SRC, os.path.join(plugins_dir, "namecrossseed"))
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
