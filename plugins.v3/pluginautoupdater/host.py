"""插件自动更新 —— 宿主适配层。

只在函数被调用时才去 import 宿主模块：导入期不联网、不碰 app.*、不抛异常。
扫描走 app.sdk.plugin.PluginManager，拿不到再试公开的插件列表端点；安装走
app.api.endpoints.plugin.install（同步或异步都能接）。异常都转成可读消息返回。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple

# 离线桩测试可以替换掉扫描 / 安装实现，不依赖真实宿主
_scan_hook: Optional[Callable[[], Tuple[List[Any], List[Any], str]]] = None
_install_hook: Optional[Callable[[str, str, bool], Tuple[bool, str, str]]] = None


def set_test_hooks(scan: Optional[Callable] = None,
                   install: Optional[Callable] = None) -> None:
    """注入替换实现；传 None 表示恢复真实宿主适配。"""
    global _scan_hook, _install_hook
    _scan_hook = scan
    _install_hook = install


def _records_from(result: Any) -> List[Any]:
    """把宿主各种返回形状摊平成记录列表。"""
    if result is None:
        return []
    if hasattr(result, "data") and not isinstance(result, (dict, list, tuple, set)):
        return _records_from(result.data)
    if isinstance(result, (list, tuple, set)):
        return [r for r in result if r is not None]
    if isinstance(result, dict):
        for key in ("plugins", "data", "items", "records", "results"):
            value = result.get(key)
            if isinstance(value, (list, tuple)):
                return [r for r in value if r is not None]
            if isinstance(value, dict):
                return _records_from(value)
        out: List[Any] = []
        for key, value in result.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("id", key)
                out.append(row)
            elif isinstance(value, (list, tuple)):
                out.extend([r for r in value if r is not None])
        return out
    return [result]


def _instance(manager_cls: Any) -> Any:
    """V3 的 PluginManager 用实例方法；实例化不了就退回类本身（兼容类方法形态）。"""
    try:
        return manager_cls()
    except Exception:  # noqa: BLE001
        return manager_cls


def _manager_scan(manager: Any, label: str) -> Tuple[List[Any], List[Any], str]:
    installed: List[Any] = []
    online: List[Any] = []
    note = ""
    for fname in ("get_installed_plugins", "get_local_plugins"):
        fn = getattr(manager, fname, None)
        if not callable(fn):
            continue
        try:
            rows = _records_from(fn())
        except Exception as err:  # noqa: BLE001
            note = f"{label}.{fname}() 读取失败：{err}"
            continue
        if rows:
            installed = rows
            note = f"{label}.{fname}()"
            break

    fn = getattr(manager, "get_online_plugins", None)
    if callable(fn):
        try:
            online = _records_from(fn(force=True))
        except Exception as err:  # noqa: BLE001
            note += f"；在线清单读取失败：{err}"
            online = []
    else:
        note += "；没有在线清单接口"
    return installed, online, note


def scan_plugins() -> Tuple[List[Any], List[Any], str]:
    """返回 (本机记录, 在线记录, 来源说明)。拿不到就给空清单，不抛异常。"""
    if _scan_hook is not None:
        try:
            installed, online, note = _scan_hook()
            return list(installed or []), list(online or []), str(note or "")
        except Exception as err:  # noqa: BLE001
            return [], [], f"扫描桩异常：{err}"

    managers: List[Tuple[Any, str]] = []
    try:
        from app.sdk.plugin import PluginManager  # type: ignore
        managers.append((_instance(PluginManager), "app.sdk.plugin.PluginManager"))
    except Exception:  # noqa: BLE001
        pass

    last_note = ""
    for manager, label in managers:
        try:
            installed, online, note = _manager_scan(manager, label)
        except Exception as err:  # noqa: BLE001
            last_note = f"{label} 扫描异常：{err}"
            continue
        last_note = note or label
        if installed:
            return installed, online, last_note

    # 最后回退：公开插件列表端点（V3 是 async，老宿主是同步，都要能接）
    try:
        from app.api.endpoints.plugin import all_plugins  # type: ignore
        result = all_plugins(state="installed", _=None, response=None)
        if inspect.isawaitable(result):
            result = _run_coroutine(result)
        installed = _records_from(result)
        if installed:
            return installed, [], "app.api.endpoints.plugin.all_plugins（仅本机清单）"
    except Exception as err:  # noqa: BLE001
        last_note = f"all_plugins 读取失败：{err}"

    return [], [], last_note or "没读到插件清单"


def _run_coroutine(coro: Any) -> Any:
    """有运行中的事件循环时放到线程池里跑，避免嵌套 asyncio.run。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=180)


def _parse_install_result(result: Any) -> Tuple[bool, str]:
    if result is None:
        return False, "安装入口没有返回"
    if isinstance(result, dict):
        return bool(result.get("success")), str(result.get("message")
                                                     or result.get("msg") or "")
    ok = bool(getattr(result, "success", False))
    msg = str(getattr(result, "message", "") or "")
    return ok, msg


def _reload(plugin_id: str) -> None:
    try:
        from app.sdk.plugin import PluginManager  # type: ignore
        manager = _instance(PluginManager)
        reload_fn = getattr(manager, "reload_plugin", None)
        if callable(reload_fn):
            reload_fn(plugin_id)
    except Exception:  # noqa: BLE001
        return


def install_plugin(plugin_id: str, repo_url: str = "",
                   force: bool = True) -> Tuple[bool, str, str]:
    """返回 (是否成功, 消息, 使用的机制)。异常一律吞掉转成可读消息。"""
    if _install_hook is not None:
        try:
            ok, msg, mechanism = _install_hook(plugin_id, repo_url, bool(force))
            return bool(ok), str(msg), str(mechanism)
        except Exception as err:  # noqa: BLE001
            return False, f"安装桩异常：{err}", "test_hook"

    try:
        from app.api.endpoints.plugin import install  # type: ignore
        result = install(plugin_id, repo_url=repo_url or "",
                         force=bool(force), _=None)
        if inspect.isawaitable(result):
            result = _run_coroutine(result)
        ok, msg = _parse_install_result(result)
        if ok:
            return True, msg or "已安装", "app.api.endpoints.plugin.install"
        return False, msg or "宿主安装入口返回失败", "app.api.endpoints.plugin.install"
    except Exception as err:  # noqa: BLE001
        return False, f"宿主安装入口异常：{err}", "无可用安装入口"


def reload_plugin(plugin_id: str) -> None:
    """可选辅助：让宿主重载某个插件，失败不影响主流程。"""
    try:
        _reload(plugin_id)
    except Exception:  # noqa: BLE001
        return
