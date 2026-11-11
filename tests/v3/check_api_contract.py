"""MoviePilot V3 插件 API 契约离线校验。

复刻宿主的动态路由注册流程：加载每个插件 -> 调用 get_api() ->
用 FastAPI.add_api_route 注册。宿主在注册失败时会捕获异常并跳过路由，
详情页按钮随后就会得到 404 Not Found，所以这里把「注册失败」当成错误。

同时静态核对详情页按钮 api 与 get_api() 声明是否一一对应。

用法：python3 tests/v3/check_api_contract.py
依赖：Python 3.9+、fastapi、pydantic（不需要 MoviePilot 宿主）。
"""

from __future__ import annotations

import ast
import importlib.abc
import importlib.util
import re
import sys
import types
from pathlib import Path
from typing import Any, Generic, Optional, TypeVar

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins.v3"

T = TypeVar("T")


class HostResponse(BaseModel, Generic[T]):
    """模拟宿主 app.schemas.Response 的 Pydantic 泛型响应模型。"""

    success: bool = True
    message: str = ""
    data: Optional[T] = None


class _StubBase:
    """通用桩基类：既能被继承，也能被调用、取属性。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def __call__(self, *args: Any, **kwargs: Any) -> "_StubBase":
        return _StubBase()

    def __getattr__(self, name: str) -> Any:
        return _StubBase()

    def __bool__(self) -> bool:
        return False


class _PluginBase(_StubBase):
    """插件基类桩：提供插件构造期需要的最小数据接口。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._data: dict[str, Any] = {}
        self._config: dict[str, Any] = {}

    def get_data(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def save_data(self, key: str, value: Any) -> None:
        self._data[key] = value

    def del_data(self, key: str) -> None:
        self._data.pop(key, None)

    def get_config(self, key: str = "", default: Any = None) -> Any:
        return self._config.get(key, default) if key else dict(self._config)

    def update_config(self, config: dict[str, Any]) -> bool:
        self._config = dict(config or {})
        return True

    def post_message(self, **kwargs: Any) -> None:
        return None

    def get_data_path(self, *args: Any, **kwargs: Any) -> str:
        return "/tmp"

    def get_state(self) -> bool:
        return False


class _Logger(_StubBase):
    def debug(self, *a: Any, **k: Any) -> None:
        return None

    def info(self, *a: Any, **k: Any) -> None:
        return None

    def warning(self, *a: Any, **k: Any) -> None:
        return None

    def error(self, *a: Any, **k: Any) -> None:
        return None


class _EventManager(_StubBase):
    def register(self, *a: Any, **k: Any) -> Any:
        return lambda func: func


class _CronTrigger(_StubBase):
    @classmethod
    def from_crontab(cls, *a: Any, **k: Any) -> Any:
        return _StubBase()


class _EnumMeta(type):
    """让桩枚举能响应任意 EventType.XXX 属性访问。"""

    def __getattr__(cls, name: str) -> str:
        return name


class _EnumStub(metaclass=_EnumMeta):
    PluginAction = "plugin.action"
    Plugin = "plugin"
    MoviePilot = "MoviePilot"


SPECIAL: dict[str, Any] = {
    "_PluginBase": _PluginBase,
    "logger": _Logger(),
    "eventmanager": _EventManager(),
    "EventType": _EnumStub,
    "MessageType": _EnumStub,
    "CronTrigger": _CronTrigger,
    "Response": HostResponse,
    "settings": _StubBase(),
}


class _StubModule(types.ModuleType):
    """按需生成属性的桩模块，覆盖插件里形形色色的宿主导入。"""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in SPECIAL:
            return SPECIAL[name]
        cached = self.__dict__.get("_stub_cache")
        if cached is None:
            cached = self.__dict__["_stub_cache"] = {}
        if name not in cached:
            cached[name] = type(name, (_StubBase,), {})
        return cached[name]


class _AppModuleFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """为任何未真实定义的 app.* 模块提供桩实现。"""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == "app" or fullname.startswith("app."):
            return importlib.util.spec_from_loader(fullname, self, is_package=True)
        return None

    def create_module(self, spec: Any) -> Any:
        mod = _StubModule(spec.name)
        mod.__path__ = []  # type: ignore[attr-defined]
        return mod

    def exec_module(self, module: Any) -> None:
        return None


def install_stubs() -> None:
    """安装宿主键与 apscheduler 桩，保留真实的 fastapi / pydantic。"""
    sys.meta_path.insert(0, _AppModuleFinder())

    schemas_types = _StubModule("app.schemas.types")
    schemas_types.EventType = _EnumStub  # type: ignore[attr-defined]
    schemas_types.MessageType = _EnumStub  # type: ignore[attr-defined]
    schemas = _StubModule("app.schemas")
    schemas.Response = HostResponse  # type: ignore[attr-defined]
    schemas.types = schemas_types  # type: ignore[attr-defined]

    app_mod = _StubModule("app")
    app_mod.schemas = schemas  # type: ignore[attr-defined]
    app_mod.__path__ = []  # type: ignore[attr-defined]

    for name, mod in {
        "app": app_mod,
        "app.schemas": schemas,
        "app.schemas.types": schemas_types,
    }.items():
        sys.modules[name] = mod

    cron_mod = _StubModule("apscheduler.triggers.cron")
    cron_mod.CronTrigger = _CronTrigger  # type: ignore[attr-defined]
    for name in ("apscheduler", "apscheduler.triggers", "apscheduler.triggers.cron"):
        sys.modules.setdefault(name, _StubModule(name))
    sys.modules["apscheduler.triggers.cron"] = cron_mod


def load_plugin(directory: Path) -> tuple[Any, Any, str]:
    """以包方式加载插件模块，返回 (模块, 主类, 主类名)。"""
    pkg_name = f"mpv3_{directory.name}"
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        directory / "__init__.py",
        submodule_search_locations=[str(directory)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)

    source = (directory / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_names = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any("_PluginBase" in ast.unparse(base) for base in node.bases)
    ]
    if not class_names:
        raise RuntimeError("未找到继承 _PluginBase 的主类")
    name = class_names[0]
    return module, getattr(module, name), name


def collect_click_events(node: Any) -> list[dict[str, str]]:
    """递归收集 get_page() 页面结构里的所有 events.click 声明。"""
    found: list[dict[str, str]] = []
    if isinstance(node, dict):
        events = node.get("events")
        if isinstance(events, dict):
            for event in events.values():
                if isinstance(event, dict) and isinstance(event.get("api"), str):
                    found.append({
                        "api": event["api"],
                        "method": str(event.get("method") or "post"),
                    })
        for value in node.values():
            found.extend(collect_click_events(value))
    elif isinstance(node, (list, tuple)):
        for value in node:
            found.extend(collect_click_events(value))
    return found


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="校验 V3 插件详情页按钮与动态 API 契约")
    parser.add_argument("--plugin-root", default=str(PLUGIN_ROOT), help="插件根目录")
    options = parser.parse_args(argv)
    plugin_root = Path(options.plugin_root).resolve()

    install_stubs()
    failures: list[str] = []
    warnings: list[str] = []
    total_routes = 0

    print(f"插件目录：{plugin_root}\n")
    for directory in sorted(p for p in plugin_root.iterdir() if (p / "__init__.py").is_file()):
        try:
            module, plugin_class, plugin_id = load_plugin(directory)
            plugin = plugin_class()
            apis = plugin.get_api() or []
        except Exception as error:  # noqa: BLE001
            failures.append(f"{directory.name}: 插件加载/实例化失败 {type(error).__name__}: {error}")
            print(f"  {directory.name:32s} 加载失败 {type(error).__name__}: {str(error)[:100]}")
            continue

        app = FastAPI()
        declared: dict[str, set[str]] = {}
        bad = 0
        for api in apis:
            route = dict(api)
            path = str(route.get("path", ""))
            declared[path.lstrip("/")] = {
                str(m).upper() for m in (api.get("methods") or ["GET"])
            }
            route.pop("auth", None)
            route.pop("allow_anonymous", None)
            route["path"] = f"/api/v1/plugin/{plugin_id}{path}"
            route["dependencies"] = list(route.get("dependencies") or ())
            route.setdefault("route_class_override", APIRoute)
            try:
                app.router.add_api_route(**route, tags=["plugin"])
            except Exception as error:  # noqa: BLE001
                bad += 1
                failures.append(
                    f"{plugin_id}{path}: FastAPI 注册失败 {type(error).__name__}: {str(error)[:150]}"
                )
        total_routes += len(apis)

        missing_auth = [str(a.get("path")) for a in apis if a.get("auth") != "bear"]
        if missing_auth:
            failures.append(f"{plugin_id}: 缺少 auth=bear 的接口 {missing_auth}")

        try:
            clicks = collect_click_events(plugin.get_page() or [])
        except Exception as error:  # noqa: BLE001
            clicks = []
            warnings.append(f"{plugin_id}: get_page 离线执行失败 {type(error).__name__}: {error}")

        prefix = f"plugin/{plugin_id}/"
        orphans: list[str] = []
        conflicts: list[str] = []
        for click in clicks:
            api_path = click["api"].lstrip("/")
            if not api_path.startswith(prefix):
                conflicts.append(f"{api_path} 路径前缀应为 {prefix}")
                continue
            action = api_path[len(prefix):].split("?", 1)[0].split("/", 1)[0]
            if action not in declared:
                orphans.append(action)
                continue
            method = click["method"].upper()
            if method not in declared[action]:
                conflicts.append(
                    f"{action}: 按钮 {method} / 接口只声明 {sorted(declared[action])}"
                )
        if orphans:
            failures.append(f"{plugin_id}: 按钮引用了未声明的接口 {sorted(set(orphans))}")
        if conflicts:
            failures.append(f"{plugin_id}: 按钮与接口不匹配 {conflicts}")

        ok = bad == 0 and not missing_auth and not orphans and not conflicts
        print(
            f"  {plugin_id:32s} 接口 {len(apis):2d}  注册失败 {bad:2d}  "
            f"按钮 {len(clicks):2d}  未声明 {len(set(orphans)):2d}  "
            f"方法冲突 {len(conflicts):2d}  {'OK' if ok else 'FAIL'}"
        )

    print(f"\n共检查 {total_routes} 条动态路由。")
    for item in warnings:
        print(f"  [提示] {item}")
    if failures:
        print(f"发现 {len(failures)} 个问题：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("全部通过：按钮路径/方法与接口声明一致，且所有接口都能被 FastAPI 注册。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
