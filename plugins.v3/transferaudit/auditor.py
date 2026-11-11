"""入库对账：源文件 ↔ 整理历史 ↔ 媒体库文件 ↔ 下载器种子的对账引擎。

面向的典型场景是 PT 连载番剧：单集做种，完结后出合集打包，站点要求删除单集种子，
于是「种子删了、源文件删了或留着、媒体库里硬链接入库的文件还在」。
本模块把整理历史逐条与磁盘现状核对，按剧集/季聚合，并调用 scanner 主动扫描
「下载器种子名录 ↔ 映射路径里的实体源文件」，输出不截断的全量清单。

设计原则（务必保留）：
1. 默认只读。删除能力必须由用户显式开启，且只走 scanner.prepare/execute 两步流程；
2. 只使用 V3 稳定 SDK（app.sdk.queries / app.sdk.services）与标准库；
3. 每个宿主接口延迟导入、独立容错，任一数据源不可用都不影响其它结论；
4. 远程存储（u115 / alipan / rclone / alist 等）一律标为「无法核验」，
   绝不把「查不到」当成「已删除」，避免误导用户误删；
5. 下载器种子状态是三态（在 / 不在 / 未核验），未核验时只提示人工核对；
6. 报告类数据不截断：明细、种子名称、路径清单、日志都完整输出，便于复制核对。

离线（无宿主模块，例如 stub 测试）时返回结构完整的空快照，offline=True。
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import scanner

CACHE_TTL = 120.0
PAGE_SIZE = 200
MAX_RECORDS = 5000
MAX_GROUPS = 5000
MAX_ISSUES = 5000
HASH_BATCH = 120

_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None, "cfg": None}
_LOCK = None

# 状态键 -> 中文短名、说明、是否属于「需关注」
STATUS_META: Dict[str, Tuple[str, str, bool]] = {
    "orphan": ("源已删 · 库仍在", "打包删种后的典型状态：源文件已不在，媒体库文件还在，已无法继续做种", True),
    "leftover": ("源残留 · 种已删", "源文件与库文件都在，但下载器里已无对应种子，可核对后清理源文件", True),
    "pending": ("源库齐全 · 待核对", "源文件与库文件都在，未能核验下载器种子状态，请自行确认", False),
    "healthy": ("源库齐全 · 做种中", "源文件、库文件、下载器种子三者齐全", False),
    "missing": ("库文件缺失", "整理记录存在，但媒体库目标文件已不在，可能是被移动或删除", True),
    "stale": ("双端已无", "源文件与库文件都不在了，只剩一条整理历史", True),
    "remote": ("路径/存储未核验", "非本地存储，或该路径空间在当前容器内不可见（常见于路径映射），插件不猜测文件是否还存在", False),
    "invalid": ("记录不完整", "整理历史缺少源或目标路径，无法核验", True),
}

STATUS_ORDER = ["orphan", "leftover", "missing", "stale", "remote", "pending", "healthy", "invalid"]

_PACK_EPISODE = re.compile(r"E\d+\s*-\s*E\d+", re.IGNORECASE)
_LOCAL_STORAGES = {"", "local", None}


def _lock():
    global _LOCK
    if _LOCK is None:
        import threading

        _LOCK = threading.Lock()
    return _LOCK


def invalidate() -> None:
    """清空快照缓存；插件配置变更、停止或手动刷新时调用。"""
    with _lock():
        _CACHE["ts"] = 0.0
        _CACHE["data"] = None
        _CACHE["cfg"] = None


def _try(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - 单一数据源失败不能让整页挂掉
        return default


def _load_queries():
    """延迟加载稳定只读查询门面；离线时返回 None。"""
    try:
        from app.sdk import queries  # type: ignore[import-not-found]

        return queries
    except Exception:  # noqa: BLE001
        return None


def _load_downloader_services() -> List[Any]:
    """通过 SDK 门面拿到已配置下载器实例（只读查询用）。"""
    try:
        from app.sdk.services import DownloaderHelper  # type: ignore[import-not-found]

        helper = DownloaderHelper()
        services = helper.get_services() or {}
        return [s for s in services.values() if getattr(s, "instance", None)]
    except Exception:  # noqa: BLE001
        return []


def _page_items(result: Any) -> Tuple[List[Any], int, bool]:
    """兼容 QueryPage 模型与字典两种返回；第三位表示「确实是合法查询响应」。

    空库（items=[]）与接口不可用（stub/异常）必须区分，否则离线会被误显示成
    「暂无记录」，掩盖了数据源故障。
    """
    if result is None:
        return [], 0, False
    if isinstance(result, dict):
        items, total = result.get("items"), result.get("total", 0)
    else:
        items, total = getattr(result, "items", None), getattr(result, "total", 0)
    if not isinstance(items, (list, tuple)):
        return [], 0, False
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        total = 0
    return list(items), total, True


def fetch_histories() -> Tuple[List[Any], int, bool]:
    """分页读取成功的整理历史。返回 (记录列表, 库中总数, 是否调通)。"""
    queries = _load_queries()
    if queries is None:
        return [], 0, False
    fn = getattr(queries, "list_transfer_history", None)
    if not callable(fn):
        return [], 0, False

    size = _try(lambda: int(getattr(queries, "MAX_QUERY_PAGE_SIZE", PAGE_SIZE)), PAGE_SIZE)
    size = max(1, min(200, size))
    collected: List[Any] = []
    total = 0
    ok = False
    for page_no in range(1, MAX_RECORDS // size + 2):
        result = _try(
            lambda p=page_no: fn({"status": True}, {"page": p, "count": size}),
            None,
        )
        items, total, valid = _page_items(result)
        if not valid:
            break
        ok = True
        collected.extend(items)
        if not items or len(items) < size:
            break
        if len(collected) >= min(total or 0, MAX_RECORDS) or len(collected) >= MAX_RECORDS:
            break
    return collected, total, ok


def probe_seed_hashes(hashes: List[str]) -> Tuple[set, bool]:
    """核验下载器里是否仍有这些种子。

    返回 (仍存在的 hash 集合, 是否成功核验过至少一台下载器)。
    任一实例查询失败都会降级为「未核验」，绝不把失败当作种子不存在。
    """
    wanted = {str(h).strip().lower() for h in hashes if h and str(h).strip()}
    if not wanted:
        return set(), False
    services = _load_downloader_services()
    if not services:
        return set(), False

    found: set = set()
    probed = False
    ordered = list(wanted)
    for service in services:
        instance = getattr(service, "instance", None)
        lister = getattr(instance, "list_torrents", None)
        if not callable(lister):
            continue
        for start in range(0, len(ordered), HASH_BATCH):
            batch = ordered[start : start + HASH_BATCH]
            # include_all_tags：不带系统标签的种子也要能被查到，否则会把「仍在」
            # 误判成「已删」。老版本实例可能不接受该参数，失败时退回最简调用。
            result = _try(lambda b=batch: lister(hashs=b, include_all_tags=True), None)
            if result is None:
                result = _try(lambda b=batch: lister(hashs=b), None)
            if result is None:
                continue
            probed = True
            if not isinstance(result, (list, tuple)):
                continue
            for torrent in result:
                value = torrent.get("hash") if isinstance(torrent, dict) else getattr(torrent, "hash", None)
                if value:
                    found.add(str(value).strip().lower())
    return found, probed


def _scope_reachable(path: str) -> bool:
    """判断该路径所在的整个挂载空间在插件进程里是否可见。

    若连任何一级祖先目录都不存在，说明是路径映射 / 未挂载，而不是文件被删；
    此时必须标为「不可核验」，不能报「已删除」误导用户清理。
    """
    current = os.path.dirname(path.rstrip("/\\")) or "/"
    for _ in range(32):
        if os.path.isdir(current):
            return True
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
    return False


def probe_path(path: Optional[str], storage: Optional[str]) -> Optional[Dict[str, Any]]:
    """核验一个本地路径的现状；非本地存储或路径空间不可见时返回 None。"""
    if not path or str(storage or "local").lower() not in _LOCAL_STORAGES:
        return None
    text = str(path).strip()
    if not text.startswith("/") and not re.match(r"^[A-Za-z]:[\\/]", text):
        return {"kind": "invalid", "dev": None, "ino": None, "size": 0, "mtime": 0.0}
    try:
        st = os.stat(text)
        is_link = os.path.islink(text)
        kind = "link" if is_link else ("dir" if os.path.isdir(text) else "file")
        return {
            "kind": kind,
            "dev": getattr(st, "st_dev", None),
            "ino": getattr(st, "st_ino", None),
            "size": int(getattr(st, "st_size", 0) or 0),
            "mtime": float(getattr(st, "st_mtime", 0) or 0),
        }
    except (OSError, ValueError):
        if not _scope_reachable(text):
            return None
        return {"kind": "missing", "dev": None, "ino": None, "size": 0, "mtime": 0.0}


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _is_pack(item: Any, src: Optional[Dict[str, Any]]) -> bool:
    """识别合集/打包入库：明确的集数范围，或整季目录形式的记录。"""
    episodes = str(_field(item, "episodes") or "")
    if _PACK_EPISODE.search(episodes):
        return True
    return bool(src and src.get("kind") == "dir" and not episodes)


def classify(
    item: Any,
    src: Optional[Dict[str, Any]],
    dst: Optional[Dict[str, Any]],
    seed_state: str,
) -> str:
    """按四点对账结果给出唯一状态。远程/异常一律不推断为删除。"""
    if src is None or dst is None:
        return "remote"
    if src.get("kind") == "invalid" or dst.get("kind") == "invalid":
        return "invalid"
    src_gone = src.get("kind") == "missing"
    dst_gone = dst.get("kind") == "missing"
    if src_gone and dst_gone:
        return "stale"
    if dst_gone:
        return "missing"
    if src_gone:
        return "orphan"
    if seed_state == "absent":
        return "leftover"
    if seed_state == "present":
        return "healthy"
    return "pending"


def _hardlink(src: Optional[Dict[str, Any]], dst: Optional[Dict[str, Any]]) -> bool:
    if not src or not dst:
        return False
    if src.get("kind") != "file" or dst.get("kind") != "file":
        return False
    if src.get("dev") is None or src.get("ino") is None:
        return False
    return src.get("dev") == dst.get("dev") and src.get("ino") == dst.get("ino")


def _human_size(num: float) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def _group_key(item: Any) -> Tuple[str, str, str]:
    return (
        str(_field(item, "title") or "未识别媒体").strip(),
        str(_field(item, "year") or "").strip(),
        str(_field(item, "seasons") or "").strip(),
    )


def build_snapshot(
    histories: Optional[List[Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造一次完整对账快照。传入 histories / config 可绕过宿主，便于离线测试。"""
    cfg = scanner.parse_config(config)
    if histories is None:
        histories, total, hist_ok = fetch_histories()
    else:
        histories = list(histories)
        total, hist_ok = len(histories), True

    hashes = [str(_field(h, "download_hash") or "").strip() for h in histories]
    live, seed_probed = probe_seed_hashes(hashes)

    # 主动扫描：下载器种子名录 + 映射路径里的实体源文件。
    torrents, torrent_probed, downloader_count = scanner.fetch_torrents(cfg["path_map"])
    seed_names: Dict[str, str] = {}
    for t in torrents:
        key = str(t.get("hash") or "").strip().lower()
        if key and t.get("title"):
            seed_names.setdefault(key, str(t["title"]))

    counts = {key: 0 for key in STATUS_META}
    issues: List[Dict[str, Any]] = []
    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    leftover_paths: List[str] = []
    remote_count = 0
    linked_count = 0

    for item in histories:
        src_path = _field(item, "src")
        dst_path = _field(item, "dest")
        src = probe_path(src_path, _field(item, "src_storage"))
        dst = probe_path(dst_path, _field(item, "dest_storage"))
        if src is None or dst is None:
            remote_count += 1

        raw_hash = str(_field(item, "download_hash") or "").strip().lower()
        if not raw_hash or not seed_probed:
            seed_state = "unknown"
        else:
            seed_state = "present" if raw_hash in live else "absent"

        status = classify(item, src, dst, seed_state)
        counts[status] += 1
        linked = _hardlink(src, dst)
        if linked:
            linked_count += 1

        pack = _is_pack(item, src)
        key = _group_key(item)
        group = groups.setdefault(
            key,
            {
                "key": "|".join(key),
                "title": key[0],
                "year": key[1],
                "season": key[2],
                "total": 0,
                "counts": {k: 0 for k in STATUS_META},
                "has_pack": False,
                "attention": 0,
                "pack_hint": "",
            },
        )
        group["total"] += 1
        group["counts"][status] += 1
        group["has_pack"] = group["has_pack"] or pack

        record = {
            "id": _field(item, "id"),
            "title": str(_field(item, "title") or "未识别媒体"),
            "year": str(_field(item, "year") or ""),
            "season": str(_field(item, "seasons") or ""),
            "episodes": str(_field(item, "episodes") or ""),
            "status": status,
            "status_label": STATUS_META[status][0],
            "src": str(src_path or ""),
            "dest": str(dst_path or ""),
            "src_state": "存在" if src and src.get("kind") != "missing" else ("已删除" if src else "无法核验"),
            "dest_state": "存在" if dst and dst.get("kind") != "missing" else ("已删除" if dst else "无法核验"),
            "seed_state": {"present": "种子仍在", "absent": "种子已删", "unknown": "未核验"}[seed_state],
            "download_hash": raw_hash,
            "torrent_title": seed_names.get(raw_hash, ""),
            "mode": str(_field(item, "mode") or ""),
            "hardlink": linked,
            "pack": pack,
            "size": _human_size((dst or src or {}).get("size") or 0),
            "date": str(_field(item, "date") or ""),
            "src_storage": str(_field(item, "src_storage") or "local"),
            "dest_storage": str(_field(item, "dest_storage") or "local"),
        }
        if STATUS_META[status][2] and len(issues) < MAX_ISSUES:
            issues.append(record)
        if status == "leftover" and record["src"] and record["src"] not in leftover_paths:
            leftover_paths.append(record["src"])

    for group in groups.values():
        group["attention"] = sum(group["counts"][k] for k, meta in STATUS_META.items() if meta[2])
        single_left = group["counts"]["leftover"]
        if group["has_pack"] and single_left:
            group["pack_hint"] = f"该季疑似已有合集入库，另有 {single_left} 条单集源文件残留可核对"

    ordered_groups = sorted(
        groups.values(),
        key=lambda g: (-g["attention"], -g["total"], g["title"]),
    )[:MAX_GROUPS]

    scan = scanner.scan_files(cfg, histories, torrents, torrent_probed, _field)
    token = scanner.register_scan(scan, cfg)
    plan = scanner.current_plan()

    # 全量「种子核对清单」：每条一行，含种子名称 / hash / 状态 / 源路径，可直接复制。
    checklist_lines: List[str] = []
    for idx, item in enumerate(issues, 1):
        name = item.get("torrent_title") or (
            f"{item.get('title', '')} {item.get('season', '')}{item.get('episodes', '')}".strip()
        )
        checklist_lines.append(
            f"[{idx:04d}] {name} | hash={item.get('download_hash') or '-'} | "
            f"{item.get('status_label')} | 源={item.get('src') or '-'} | 库={item.get('dest') or '-'}"
        )
    for f in scan["files"]:
        if f.get("deletable"):
            checklist_lines.append(
                f"[DIR ] {f['name']} | 种子已无 | 源={f['path']} | "
                f"{'已登记整理历史' if f.get('known') else '未找到整理记录，请重点核对'}"
            )

    seed_list = [t["title"] for t in torrents if t.get("title")]
    seed_list.extend(
        item.get("torrent_title") for item in issues
        if item.get("torrent_title") and item["torrent_title"] not in seed_list
    )
    # 让「插件日志」里也能直接复制种子名称去下载器核对，而不只是页面上有清单。
    scanner.set_seed_checklist(seed_list)

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "generated_at": now,
        "scanned": len(histories),
        "total": int(total or len(histories)),
        "truncated": len(histories) < int(total or 0) or len(histories) >= MAX_RECORDS,
        "sources": {"histories": bool(hist_ok), "downloaders": bool(seed_probed)},
        "offline": not hist_ok,
        "seed_probed": bool(seed_probed),
        "counts": counts,
        "status_meta": {k: {"label": v[0], "hint": v[1], "attention": v[2]} for k, v in STATUS_META.items()},
        "status_order": STATUS_ORDER,
        "groups": ordered_groups,
        "group_total": len(groups),
        "issues": issues,
        "issue_total": len(issues),
        "leftover_paths": leftover_paths,
        "linked_count": linked_count,
        "remote_count": remote_count,
        # ---- 主动扫描 / 核对清单 / 删除能力 ----
        "torrents": torrents,
        "torrent_total": len(torrents),
        "torrent_probed": bool(torrent_probed),
        "downloader_count": downloader_count,
        "seed_list": seed_list,
        "checklist": "\n".join(checklist_lines),
        "checklist_total": len(checklist_lines),
        "scan": {
            "ok": bool(scan["ok"]),
            "reason": scan["reason"],
            "roots": scan["roots"],
            "ignored": scan["ignored"],
            "total": scan["file_total"],
            "matched": scan["matched"],
            "truncated": scan["truncated"],
        },
        "files": scan["files"],
        "file_total": scan["file_total"],
        "candidates": [f for f in scan["files"] if f.get("deletable")],
        "scan_token": token,
        "delete_enabled": bool(cfg["allow_delete"]),
        "delete_mode": cfg["delete_mode"],
        "delete_batch": scanner.MAX_BATCH,
        "plan": (
            {
                "id": plan["id"],
                "count": plan["count"],
                "size": scanner._human(plan["bytes"]),
                "mode": plan["mode"],
                "items": plan["items"],
            }
            if plan else None
        ),
        "log": scanner.log_text(200),
    }


def collect(force: bool = False, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """带短时缓存的快照入口；首页仪表盘与详情页共用。"""
    cfg = scanner.parse_config(config)
    sig = repr(sorted((k, str(v)) for k, v in cfg.items()))
    now = time.time()
    with _lock():
        data = _CACHE["data"]
        if (
            not force
            and data is not None
            and _CACHE["cfg"] == sig
            and now - float(_CACHE["ts"] or 0) < CACHE_TTL
        ):
            return data
    snapshot = build_snapshot(config=cfg)
    with _lock():
        _CACHE["ts"] = time.time()
        _CACHE["data"] = snapshot
        _CACHE["cfg"] = sig
    return snapshot
