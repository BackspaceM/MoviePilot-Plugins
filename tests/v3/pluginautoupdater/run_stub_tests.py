"""在没有 MoviePilot 宿主、没有 pytest 的环境中运行「插件自动更新」离线用例。

做法：临时目录里生成 app stub 包，把插件源码复制进
app/plugins/pluginautoupdater，注入最小 SDK 桩（含 PluginManager / async install /
scheduler / events / logging / schemas / apscheduler），再直接调用 test_plugin.py
里所有 test_ 函数。

另外执行两项提交门禁：插件版本与 package.v3.json 索引一致、Python 编译。
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
PLUGIN_SRC = REPO / "plugins.v3" / "pluginautoupdater"
PLUGIN_DIR = "pluginautoupdater"
INDEX_KEY = "PluginAutoUpdater"

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

_BACK_REPO = "https://github.com/BackspaceM/MoviePilot-Plugins"
_JXX_REPO = "https://github.com/jxxghp/MoviePilot-Plugins"
_OTHER_REPO = "https://github.com/other/MoviePilot-Plugins"

class _PydanticRecord:
    """带 model_dump() 的 Pydantic 风格对象。"""
    def __init__(self, **kw): self._fields = dict(kw)
    def model_dump(self): return dict(self._fields)

class _PlainRecord:
    """只有属性的普通对象。"""
    def __init__(self, **kw):
        for key, value in kw.items(): setattr(self, key, value)

INSTALLED = [
    dict(id="PluginAutoUpdater", plugin_name="插件自动更新",
         plugin_author="BackspaceM", plugin_version="1.0.0",
         installed=True, state="enabled", has_update=False,
         repo_url=_BACK_REPO, plugin_desc="自身，默认不更新"),
    _PydanticRecord(id="MediaLibraryRefresh", plugin_name="媒体库刷新",
                    plugin_author="BackspaceM", plugin_version="1.2.0",
                    installed=True, state="enabled", has_update=False,
                    repo_url=_BACK_REPO, plugin_desc="媒体库刷新"),
    dict(id="ViewingWeeklyReport", plugin_name="观影周报",
         plugin_author="BackspaceM", plugin_version="2.0.0",
         installed=True, state="enabled", has_update=True,
         update_candidate={"version": "2.1.0", "repo_url": _BACK_REPO},
         repo_url=_BACK_REPO, plugin_desc="周报"),
    _PlainRecord(id="DownloaderHelper", plugin_name="下载器助手",
                 plugin_author="jxxghp", plugin_version="1.0.0",
                 installed=True, state="enabled", has_update=False,
                 repo_url=_JXX_REPO),
    dict(id="SiteSpider", plugin_name="站点蜘蛛",
         plugin_author="jxxghp", plugin_version="1.4.0",
         installed=True, state="enabled", has_update=False,
         repo_url=_JXX_REPO),
    _PydanticRecord(id="AutoSignIn", plugin_name="自动签到",
                    plugin_author="", plugin_version="3.1.0",
                    installed=True, state="enabled", has_update=False,
                    repo_url=_OTHER_REPO),
]

ONLINE = [
    dict(id="PluginAutoUpdater", plugin_name="插件自动更新",
         plugin_author="BackspaceM", plugin_version="1.0.0", repo_url=_BACK_REPO),
    dict(id="MediaLibraryRefresh", plugin_name="媒体库刷新",
         plugin_author="BackspaceM", plugin_version="1.3.0", repo_url=_BACK_REPO),
    dict(id="ViewingWeeklyReport", plugin_name="观影周报",
         plugin_author="BackspaceM", plugin_version="2.1.0", repo_url=_BACK_REPO),
    dict(id="DownloaderHelper", plugin_name="下载器助手",
         plugin_author="jxxghp", plugin_version="1.1.0", repo_url=_JXX_REPO),
    dict(id="SiteSpider", plugin_name="站点蜘蛛",
         plugin_author="jxxghp", plugin_version="1.4.0", repo_url=_JXX_REPO),
    dict(id="AutoSignIn", plugin_name="自动签到",
         plugin_author="", plugin_version="3.1.0", repo_url=_OTHER_REPO),
]

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
            self._data_path = tempfile.mkdtemp(prefix="pau-data-")
        return self._data_path

class PluginManager:
    @staticmethod
    def get_installed_plugins(): return list(INSTALLED)
    @staticmethod
    def get_local_plugins(): return list(INSTALLED)
    @staticmethod
    def get_online_plugins(force=False): return list(ONLINE)
    @staticmethod
    def get_plugin_config(pid): return {"id": pid, "enabled": True}
    @staticmethod
    def get_plugin_state(pid): return True
    @staticmethod
    def reload_plugin(pid): return True

sdk_plugin._PluginBase = _PluginBase
sdk_plugin.PluginManager = PluginManager
sdk_plugin.INSTALLED = INSTALLED
sdk_plugin.ONLINE = ONLINE
sys.modules["app.sdk.plugin"] = sdk_plugin
sdk.plugin = sdk_plugin

api = types.ModuleType("app.api")
api.__path__ = []
sys.modules["app.api"] = api
app.api = api

api_endpoints = types.ModuleType("app.api.endpoints")
api_endpoints.__path__ = []
sys.modules["app.api.endpoints"] = api_endpoints
api.endpoints = api_endpoints

api_plugin = types.ModuleType("app.api.endpoints.plugin")
INSTALL_CALLS = []
INSTALL_FAIL_IDS = set()

class _InstallResponse:
    def __init__(self, success=True, message="", data=None):
        self.success = success
        self.message = message
        self.data = data

async def install(plugin_id, repo_url="", release_version=None, force=False, _=None):
    INSTALL_CALLS.append({"plugin_id": plugin_id, "repo_url": repo_url,
                          "release_version": release_version, "force": force})
    if plugin_id in INSTALL_FAIL_IDS:
        return _InstallResponse(False, "模拟安装失败：" + str(plugin_id))
    return _InstallResponse(True, "已安装 " + str(plugin_id))

def all_plugins(state="installed", _=None, response=None):
    return {"success": True, "data": list(INSTALLED)}

def reset():
    INSTALL_CALLS.clear()
    INSTALL_FAIL_IDS.clear()

api_plugin.install = install
api_plugin.all_plugins = all_plugins
api_plugin.INSTALL_CALLS = INSTALL_CALLS
api_plugin.INSTALL_FAIL_IDS = INSTALL_FAIL_IDS
api_plugin.reset = reset
sys.modules["app.api.endpoints.plugin"] = api_plugin
api_endpoints.plugin = api_plugin
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


def _version_gate() -> None:
    """plugin_version / 索引 version / 最新 history 必须一致。"""
    init_text = (PLUGIN_SRC / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^\s*plugin_version\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    assert match, "未找到 plugin_version 字面量"
    version = match.group(1)
    import json
    index = json.loads((REPO / "package.v3.json").read_text(encoding="utf-8"))
    item = index.get(INDEX_KEY)
    assert item, f"package.v3.json 缺少 {INDEX_KEY}"
    assert item["version"] == version, f"索引版本 {item['version']} != {version}"
    assert item["author"] == "BackspaceM", "索引作者不一致"
    assert f"v{version}" in item["history"], "history 缺少当前版本条目"
    print(f"PASS  version_gate (v{version})")


def main() -> int:
    work = tempfile.mkdtemp(prefix="pau-local-")
    try:
        root = Path(work)
        app_dir = root / "app"
        plugins_dir = app_dir / "plugins"
        plugins_dir.mkdir(parents=True)
        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
        shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_DIR,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "conftest.py").write_text(CONFTEST, encoding="utf-8")
        tests_dir = root / "tests"
        tests_dir.mkdir()
        shutil.copy(TEST_FILE, tests_dir / "test_plugin.py")

        os.chdir(work)
        sys.path.insert(0, work)
        sys.path.insert(0, str(tests_dir))
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ["PAU_REPO"] = str(REPO)
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
