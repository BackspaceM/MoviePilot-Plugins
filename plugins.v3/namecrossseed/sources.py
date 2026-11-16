"""路径来源扫描：把下载根目录的顶层项转成名称辅种可用的条目。

只依赖标准库，不访问 MoviePilot 宿主、不联网。下载器模式仍然走宿主 API；
路径模式用这里生成的条目代替「已完成任务」，后续搜索、复核、加种流程完全共用。

只扫顶层项：一个文件就是一个条目，一个目录就是一个条目；目录内部按相对路径
记录文件名与字节数，供下载种子后的文件清单复核使用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

MAX_ITEMS = 2000
MAX_FILES = 20000

_IGNORE_EXTS = frozenset({
    ".torrent", ".part", ".partial", ".tmp", ".temp", ".aria2",
    ".nfo", ".sfv", ".log", ".ini", ".db", ".ds_store",
})
_IGNORE_NAMES = frozenset({
    "@eaDir", "#recycle", ".@__thumb", "__pycache__", "lost+found", ".recycle",
})


@dataclass(frozen=True)
class PathItem:
    """一个可拿去站点搜名字的本地顶层项。"""

    root: str
    name: str
    path: str
    size: int = 0
    added_on: int = 0
    files: dict[str, int] = field(default_factory=dict)


def _ignored(name: str) -> bool:
    lower = str(name or "").lower()
    if not lower or lower.startswith(".") or lower in _IGNORE_NAMES:
        return True
    return any(lower.endswith(ext) for ext in _IGNORE_EXTS)


def _mtime(path: str) -> int:
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def scan_paths(
    roots: Iterable[str],
    max_items: int = MAX_ITEMS,
    max_files: int = MAX_FILES,
) -> list[PathItem]:
    """扫描多个根目录的顶层项，返回去重后的条目列表。

    #### 说明
    - 根目录必须是容器内可见的绝对路径；读不到就跳过，不抛异常。
    - 同名顶层项只保留第一次出现的那个，避免两个目录的同名内容互相覆盖缓存。
    - 目录内部最多记录 ``max_files`` 个文件，防止超大目录把内存和日志撑爆。
    """
    result: list[PathItem] = []
    seen: set[str] = set()
    for raw_root in roots or ():
        if len(result) >= max_items:
            break
        root = os.path.abspath(os.path.expanduser(str(raw_root or "")))
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            if len(result) >= max_items:
                break
            if _ignored(entry) or entry in seen:
                continue
            path = os.path.join(root, entry)
            try:
                if os.path.isfile(path):
                    size = os.path.getsize(path)
                    result.append(PathItem(
                        root=root, name=entry, path=path, size=int(size),
                        added_on=_mtime(path), files={entry: int(size)},
                    ))
                    seen.add(entry)
                    continue
                if not os.path.isdir(path):
                    continue
                files: dict[str, int] = {}
                latest = 0
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames[:] = [name for name in dirnames if not _ignored(name)]
                    latest = max(latest, _mtime(dirpath))
                    for filename in filenames:
                        if _ignored(filename):
                            continue
                        full = os.path.join(dirpath, filename)
                        rel = os.path.relpath(full, path).replace(os.sep, "/")
                        try:
                            files[rel] = os.path.getsize(full)
                        except OSError:
                            continue
                        latest = max(latest, _mtime(full))
                        if len(files) >= max_files:
                            break
                    if len(files) >= max_files:
                        break
                if files:
                    result.append(PathItem(
                        root=root, name=entry, path=path, size=sum(files.values()),
                        added_on=latest, files=files,
                    ))
                    seen.add(entry)
            except OSError:
                continue
    return result


def normalize_dirs(value: object) -> list[str]:
    """把文本或列表配置规范成去重的绝对路径列表。"""
    if isinstance(value, (list, tuple, set)):
        lines = [str(item).replace(",", "\n") for item in value]
    else:
        lines = str(value or "").replace(",", "\n").splitlines()
    result: list[str] = []
    for raw in lines:
        line = str(raw or "").strip().rstrip("/")
        if not line or line.startswith("#"):
            continue
        path = os.path.abspath(os.path.expanduser(line))
        if path not in result:
            result.append(path)
    return result
