"""B · 入库账本与链接拓扑。

分页读全量整理历史 + inode / 设备号核验，画出 src ↔ dest 拓扑，标出：
真硬链接 / 已退化为复制 / 源已删·库仍在（孤儿）/ 应删未删 /
双端已无 / 疑似外部登记缺失 / 未核验。

只读报告，不补录、不删除、不改动整理历史与下载器任务。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from . import common

STATUS_META: Dict[str, str] = {
    "hardlink_ok": "真硬链接",
    "degraded_copy": "已退化为复制",
    "orphan": "源已删 · 库仍在",
    "src_leftover": "应删未删 / 整理失败",
    "both_missing": "双端已无",
    "copy_ok": "复制或移动入库",
    "unverified": "未核验",
}
STATUS_ORDER = ("degraded_copy", "src_leftover", "orphan", "both_missing",
                "unverified", "copy_ok", "hardlink_ok")
MAX_EXTERNAL = 500


def _same_inode(src: Dict[str, Any], dest: Dict[str, Any]) -> bool:
    return bool(
        src.get("dev") is not None
        and dest.get("dev") is not None
        and src.get("dev") == dest.get("dev")
        and src.get("ino") == dest.get("ino")
        and src.get("ino") is not None
    )


def classify(src: Dict[str, Any], dest: Dict[str, Any], mode: str) -> str:
    ss, ds = src.get("state"), dest.get("state")
    if ss == "unknown" or ds == "unknown":
        return "unverified"
    if ss == "present" and ds == "present":
        if mode == "link":
            return "hardlink_ok" if _same_inode(src, dest) else "degraded_copy"
        return "copy_ok"
    if ds == "present" and ss == "missing":
        return "orphan"
    if ss == "present" and ds == "missing":
        return "src_leftover"
    if ss == "missing" and ds == "missing":
        return "both_missing"
    return "unverified"


def _external_gaps(
    histories: Sequence[Dict[str, Any]],
    dirs: Dict[str, List[str]],
    walk_fn,
) -> Tuple[List[Dict[str, Any]], bool]:
    """媒体库目录里有实体文件、但整理历史里没有对应 dest → 疑似外部登记缺失。"""
    known = set()
    for hist in histories:
        dest = str(common.cfg_attr(hist, "dest") or "")
        if dest:
            known.add(os.path.realpath(dest))
    found: List[Dict[str, Any]] = []
    truncated = False
    for root in [p for p in dirs.get("library", []) if p.startswith("/")][:2]:
        paths, cut = walk_fn(root)
        truncated = truncated or bool(cut)
        for path in paths:
            if os.path.realpath(path) in known:
                continue
            info = common.parse_filename(os.path.basename(path))
            found.append({
                "path": path, "name": os.path.basename(path),
                "title": info["title"], "season": info["season"], "episode": info["episode"],
            })
            if len(found) >= MAX_EXTERNAL:
                truncated = True
                break
        if len(found) >= MAX_EXTERNAL:
            break
    return found, truncated


def build_ledger(
    cfg: Optional[Dict[str, Any]] = None,
    histories: Optional[Sequence[Dict[str, Any]]] = None,
    dirs: Optional[Dict[str, List[str]]] = None,
    probe_fn=None,
    walk_fn=None,
) -> Dict[str, Any]:
    histories = list(histories or [])
    dirs = dirs or {"download": [], "library": []}
    probe = probe_fn or common.probe
    walk = walk_fn or common.walk_files

    rows: List[Dict[str, Any]] = []
    groups: Dict[str, Dict[str, Any]] = {}
    for hist in histories:
        src_path = str(common.cfg_attr(hist, "src") or "")
        dest_path = str(common.cfg_attr(hist, "dest") or "")
        src_storage = str(common.cfg_attr(hist, "src_storage") or "local")
        dest_storage = str(common.cfg_attr(hist, "dest_storage") or "local")
        src_state = probe(src_path, src_storage) if src_path else {"state": "unknown", "dev": None, "ino": None}
        dest_state = probe(dest_path, dest_storage) if dest_path else {"state": "unknown", "dev": None, "ino": None}
        mode = str(common.cfg_attr(hist, "mode") or "").lower()
        status = classify(src_state, dest_state, mode)
        notes: List[str] = []
        if status == "hardlink_ok":
            notes.append("src 与 dest 同设备同 inode，硬链接有效")
        elif status == "degraded_copy":
            notes.append("记录声明为硬链接，但两端 inode 不同，已退化为独立副本（占双倍空间）")
        elif status == "orphan":
            notes.append("源文件已不在，媒体库文件仍在；硬链接场景下这通常是正常的，不等于异常")
        elif status == "src_leftover":
            notes.append("源文件仍在但目标文件缺失，可能是整理失败或源应删未删")
        elif status == "both_missing":
            notes.append("两端文件都已不在，仅剩整理历史记录")
        elif status == "unverified":
            notes.append("远程存储或容器内路径不可见，一律标未核验，不把查不到当成已删除")
        if not src_path or not dest_path:
            notes.append("整理历史字段不完整，无法建立完整拓扑")

        row = {
            "id": common.cfg_attr(hist, "id") or "",
            "title": str(common.cfg_attr(hist, "title") or "未命名"),
            "year": str(common.cfg_attr(hist, "year") or ""),
            "season": str(common.cfg_attr(hist, "seasons") or ""),
            "episode": str(common.cfg_attr(hist, "episodes") or ""),
            "src": src_path or "—",
            "dest": dest_path or "—",
            "src_storage": src_storage,
            "dest_storage": dest_storage,
            "mode": mode or "未记录",
            "date": str(common.cfg_attr(hist, "date") or ""),
            "download_hash": str(common.cfg_attr(hist, "download_hash") or ""),
            "src_state": src_state.get("state"),
            "dest_state": dest_state.get("state"),
            "src_dev": src_state.get("dev"),
            "src_ino": src_state.get("ino"),
            "dest_dev": dest_state.get("dev"),
            "dest_ino": dest_state.get("ino"),
            "same_inode": _same_inode(src_state, dest_state),
            "status": status,
            "status_label": STATUS_META[status],
            "notes": notes,
        }
        rows.append(row)

        key = f"{common.normalize_title(row['title'])}|{row['season']}"
        group = groups.setdefault(key, {
            "key": key, "title": row["title"], "season": row["season"],
            "count": 0, "statuses": {}, "src": [], "dest": [],
        })
        group["count"] += 1
        group["statuses"][status] = group["statuses"].get(status, 0) + 1
        if src_path and len(group["src"]) < 8:
            group["src"].append(src_path)
        if dest_path and len(group["dest"]) < 8:
            group["dest"].append(dest_path)

    external, external_truncated = _external_gaps(histories, dirs, walk)
    summary = {key: sum(1 for r in rows if r["status"] == key) for key in STATUS_META}
    summary.update({
        "total": len(rows),
        "groups": len(groups),
        "external_gap": len(external),
        "external_truncated": external_truncated,
    })
    order = {s: i for i, s in enumerate(STATUS_ORDER)}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["title"], r["src"]))
    group_list = sorted(groups.values(), key=lambda g: (-g["count"], g["title"]))
    return {
        "ok": bool(rows) or bool(external),
        "reason": "" if (rows or external) else "整理历史为空或宿主 SDK 不可用，且媒体库目录里没有可扫描的文件",
        "records": rows,
        "groups": group_list,
        "external": external,
        "external_truncated": external_truncated,
        "summary": summary,
        "status_meta": dict(STATUS_META),
        "library_dirs": [p for p in dirs.get("library", []) if p.startswith("/")],
        "readonly": True,
        "note": (
            "inode / 设备号由 os.stat 只读核验；远程存储与容器内不可见路径一律标「未核验」。"
            "疑似外部登记缺失仅表示「库里有文件、历史里没记录」，可能是手工入库，不代表异常。"
        ),
    }
