"""插件自动更新 —— 宿主适配层。

只在函数被调用时才去 import 宿主模块：导入期不联网、不碰 app.*、不抛异常。
本机清单优先读 PluginManager（V3 是 plugins/running_plugins 映射，V2/旧版是
get_installed_plugins / get_local_plugins），在线清单走公开目录接口；同步/异步
都能接，所有宿主调用都放在带超时的后台线程里，读不到就返回空清单。

scan_local() 只碰本机内存里的插件表，不给配置页引入网络等待。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

# 离线桩测试可以替换掉扫描 / 安装实现，不依赖真实宿主
_scan_hook: Optional[Callable[[], Tuple[List[Any], List[Any], str]]] = None
_install_hook: Optional[Callable[[str, str, bool], Tuple[bool, str, str]]] = None

_LOCAL_TIMEOUT = 2.0
_MARKET_TIMEOUT = 6.0


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


def _as_mapping(record: Any) -> Dict[str, Any]:
    """dict / Pydantic / 插件类 / 插件实例统一摊成 dict。"""
    if isinstance(record, dict):
        return dict(record)
    if record is None:
        return {}
    for attr in ("model_dump", "dict"):
        fn = getattr(record, attr, None)
        if callable(fn):
            try:
                out = fn()
            except Exception:  # noqa: BLE001
                out = None
            if isinstance(out, dict):
                return dict(out)
    try:
        raw = dict(record.__dict__)
        if raw:
            return raw
    except Exception:  # noqa: BLE001
        pass
    keys = ("id", "plugin_id", "plugin_name", "name", "plugin_author", "author",
            "plugin_version", "version", "installed", "state", "has_update",
            "repo_url", "update_candidate")
    return {k: getattr(record, k) for k in keys if hasattr(record, k)}


def _instance(manager_cls: Any) -> Any:
    """V3 的 PluginManager 用实例方法；实例化不了就退回类本身（兼容类方法形态）。"""
    try:
        return manager_cls()
    except Exception:  # noqa: BLE001
        return manager_cls


def _manager() -> Any:
    try:
        from app.sdk.plugin import PluginManager  # type: ignore
        return _instance(PluginManager)
    except Exception:  # noqa: BLE001
        return None


def _run_coroutine(coro: Any) -> Any:
    """有运行中的事件循环时放到线程池里跑，避免嵌套 asyncio.run。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=180)


def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return _run_coroutine(value)
    return value


def _guarded(fn: Callable[[], Any], timeout: float) -> Tuple[Any, str]:
    """在守护线程里跑宿主调用，超时就放弃；不阻塞请求线程。"""
    box: Dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = fn()
        except Exception as err:  # noqa: BLE001
            box["error"] = err

    thread = threading.Thread(target=worker, name="PAUHostCall", daemon=True)
    thread.start()
    thread.join(max(0.1, float(timeout)))
    if thread.is_alive():
        return None, "读取超时"
    if "error" in box:
        return None, str(box["error"])
    return box.get("value"), ""


def _plugin_record(pid: Any, obj: Any) -> Dict[str, Any]:
    """把 PluginManager 的 {id: 插件类/实例} 转成扫描记录。"""
    raw = _as_mapping(obj)
    return {
        "id": str(raw.get("id") or pid or ""),
        "plugin_name": str(raw.get("plugin_name") or raw.get("name") or pid or ""),
        "plugin_author": str(raw.get("plugin_author") or raw.get("author") or ""),
        "plugin_version": str(raw.get("plugin_version") or raw.get("version") or ""),
        "installed": True,
        "state": raw.get("state", True),
        "has_update": bool(raw.get("has_update", False)),
        "repo_url": str(raw.get("repo_url") or ""),
    }


def _local_records() -> Tuple[List[Any], str]:
    """只读本机插件表，不发网络请求。"""
    manager = _manager()
    if manager is not None:
        for fname in ("get_installed_plugins", "get_local_plugins"):
            fn = getattr(manager, fname, None)
            if not callable(fn):
                continue
            try:
                rows = _records_from(_maybe_await(fn()))
            except Exception:  # noqa: BLE001
                continue
            if rows:
                return rows, f"PluginManager.{fname}()"
        for attr in ("running_plugins", "plugins"):
            try:
                mapping = getattr(manager, attr)
                if callable(mapping):
                    mapping = mapping()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(mapping, dict) and mapping:
                rows = [_plugin_record(pid, obj) for pid, obj in mapping.items()]
                return rows, f"PluginManager.{attr}"
    try:
        from app.api.endpoints.plugin import all_plugins  # type: ignore
        result = _maybe_await(all_plugins(state="installed", _=None, response=None))
        rows = _records_from(result)
        if rows:
            return rows, "all_plugins(installed)"
    except Exception:  # noqa: BLE001
        pass
    return [], ""


def _online_records() -> Tuple[List[Any], str]:
    """在线清单，用来补新版本与来源仓库；读不到不影响本机清单。"""
    manager = _manager()
    if manager is not None:
        fn = getattr(manager, "get_online_plugins", None)
        if callable(fn):
            try:
                try:
                    rows = _records_from(_maybe_await(fn(force=False)))
                except TypeError:
                    rows = _records_from(_maybe_await(fn()))
                if rows:
                    return rows, "在线插件清单"
            except Exception:  # noqa: BLE001
                pass
    try:
        from app.api.endpoints.plugin import all_plugins  # type: ignore
        try:
            result = all_plugins(state="all", force=False, _=None, response=None)
        except TypeError:
            result = all_plugins(state="all", _=None, response=None)
        rows = _records_from(_maybe_await(result))
        if rows:
            return rows, "插件目录"
    except Exception:  # noqa: BLE001
        pass
    return [], ""


def scan_plugins(with_market: bool = True,
                 timeout: float = _MARKET_TIMEOUT) -> Tuple[List[Any], List[Any], str]:
    """返回 (本机记录, 在线记录, 来源说明)。拿不到就给空清单，不抛异常。"""
    if _scan_hook is not None:
        try:
            installed, online, note = _scan_hook()
            return list(installed or []), list(online or []), str(note or "")
        except Exception as err:  # noqa: BLE001
            return [], [], f"扫描桩异常：{err}"

    local, local_error = _guarded(_local_records, _LOCAL_TIMEOUT)
    installed: List[Any] = []
    source = ""
    if isinstance(local, tuple) and len(local) == 2:
        installed = list(local[0] or [])
        source = str(local[1] or "")
    notes = [source] if source else []
    if local_error:
        notes.append(f"本机清单：{local_error}")

    online: List[Any] = []
    if with_market:
        market, market_error = _guarded(_online_records, timeout)
        online_source = ""
        if isinstance(market, tuple) and len(market) == 2:
            online = list(market[0] or [])
            online_source = str(market[1] or "")
        if online_source:
            notes.append(online_source)
        if market_error:
            notes.append(f"在线清单：{market_error}")
    return installed, online, "｜".join(x for x in notes if x) or "没读到插件清单"


def scan_local() -> List[Any]:
    """配置页用：只取本机插件表，不碰在线目录，绝不长时间等待。"""
    installed, _, _ = scan_plugins(with_market=False, timeout=_LOCAL_TIMEOUT)
    return installed


def _run_coroutine_for_install(coro: Any) -> Any:
    return _run_coroutine(coro)


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
            result = _run_coroutine_for_install(result)
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
