"""A · 整理预演沙盘。

选一个目录（留空自动从整理历史推断），只读批量算出：
目标目录 / 保守预测路径 / 命名冲突 / 覆盖风险 / 跨盘导致硬链接退化为复制 /
重复入库风险 / 目标目录不可见。**一个文件都不动。**

命名规则由宿主分类链决定，本模块不假装能精确复刻，因此预测路径按
「推断出的目标目录 + 保持原文件名」保守给出，并在报告里明确标注。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from . import common

RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "ok": 3}


def _roots_from_config(cfg: Dict[str, Any], dirs: Dict[str, List[str]]) -> List[str]:
    raw = str(cfg.get("preview_dir") or "").strip()
    roots: List[str] = []
    if raw:
        for line in raw.replace(",", "\n").splitlines():
            text = line.strip()
            if text.startswith("/") and text not in roots:
                roots.append(text)
    else:
        roots = [p for p in dirs.get("download", []) if p.startswith("/")][:3]
    return roots


def _mode_hint(histories: Sequence[Dict[str, Any]]) -> str:
    counter: Dict[str, int] = {}
    for hist in histories:
        mode = str(common.cfg_attr(hist, "mode") or "").lower()
        if mode:
            counter[mode] = counter.get(mode, 0) + 1
    if not counter:
        return "未核验（缺少整理历史）"
    mode = max(counter.items(), key=lambda kv: kv[1])[0]
    return {"link": "硬链接", "copy": "复制", "move": "移动", "softlink": "软链接"}.get(mode, mode)


def _dest_dirs_by_title(histories: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for hist in histories:
        if str(common.cfg_attr(hist, "dest_storage") or "local").lower() not in ("local", "", "none"):
            continue
        title = common.normalize_title(common.cfg_attr(hist, "title") or "")
        dest = str(common.cfg_attr(hist, "dest") or "")
        parent = os.path.dirname(dest.rstrip("/"))
        if title and parent.startswith("/") and title not in out:
            out[title] = parent
    return out


def build_preview(
    cfg: Optional[Dict[str, Any]] = None,
    histories: Optional[Sequence[Dict[str, Any]]] = None,
    dirs: Optional[Dict[str, List[str]]] = None,
    probe_fn=None,
    walk_fn=None,
) -> Dict[str, Any]:
    cfg = dict(cfg or {})
    histories = list(histories or [])
    dirs = dirs or {"download": [], "library": []}
    probe = probe_fn or common.probe
    walk = walk_fn or common.walk_files

    roots = _roots_from_config(cfg, dirs)
    files: List[str] = []
    truncated = False
    for root in roots:
        got, cut = walk(root)
        files.extend(got)
        truncated = truncated or bool(cut)
    seen: set = set()
    unique: List[str] = []
    for path in files:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    files = unique

    library_dirs = [p for p in dirs.get("library", []) if p.startswith("/")]
    by_title = _dest_dirs_by_title(histories)
    by_src = {str(common.cfg_attr(h, "src") or "") for h in histories if common.cfg_attr(h, "src")}
    by_name: Dict[str, List[str]] = {}
    for hist in histories:
        src = str(common.cfg_attr(hist, "src") or "")
        if src:
            by_name.setdefault(os.path.basename(src.rstrip("/")), []).append(src)

    default_target = library_dirs[0] if library_dirs else ""
    mode_hint = _mode_hint(histories)
    rows: List[Dict[str, Any]] = []

    for path in files:
        name = os.path.basename(path)
        info = common.parse_filename(name)
        src_state = probe(path)
        target_dir = by_title.get(common.normalize_title(info["title"]), "") or default_target
        predicted = os.path.join(target_dir, name) if target_dir else ""
        target_state = probe(predicted) if predicted else {"state": "unknown", "dev": None}
        target_dir_state = probe(target_dir) if target_dir else {"state": "unknown", "dev": None}

        risks: List[str] = []
        level = "ok"
        if not target_dir:
            risks.append("未核验：缺少整理历史，无法推断目标目录，请在设置里填预演目录或先整理一批文件")
            level = "low"
        else:
            if target_dir_state.get("state") != "present":
                risks.append("目标目录在容器内不可见或不存在，可能路径映射不对")
                level = "medium"
            if target_state.get("state") == "present":
                risks.append("目标同名文件已存在，整理时可能覆盖已有入库文件")
                level = "high"
        if path in by_src:
            risks.append("该文件已有整理记录，重复整理可能产生重复入库")
            if RISK_ORDER[level] > RISK_ORDER["medium"]:
                level = "medium"
        elif name in by_name:
            risks.append("整理历史里存在同名文件，注意版本/集数撞车")
            if RISK_ORDER[level] > RISK_ORDER["low"]:
                level = "low"
        src_dev = src_state.get("dev")
        dst_dev = target_dir_state.get("dev")
        cross_device = bool(src_dev and dst_dev and src_dev != dst_dev)
        if cross_device:
            risks.append("源与目标跨设备/跨盘：硬链接会静默退化为复制，占用双倍空间")
            if RISK_ORDER[level] > RISK_ORDER["medium"]:
                level = "medium"
        if src_state.get("state") != "present":
            risks.append("源文件状态未核验，预演结果仅供参考")

        rows.append({
            "path": path,
            "name": name,
            "size": int(src_state.get("size") or 0),
            "size_text": common.fmt_size(src_state.get("size") or 0),
            "title": info["title"],
            "year": info["year"],
            "season": info["season"],
            "episode": info["episode"],
            "kind": info["kind"],
            "target_dir": target_dir or "未核验",
            "predicted_path": predicted or "未核验",
            "src_state": src_state.get("state"),
            "src_dev": src_dev,
            "target_dev": dst_dev,
            "cross_device": cross_device,
            "target_exists": target_state.get("state") == "present",
            "risks": risks,
            "level": level,
        })

    rows.sort(key=lambda r: (RISK_ORDER.get(r["level"], 9), r["path"]))
    summary = {
        "total": len(rows),
        "high": sum(1 for r in rows if r["level"] == "high"),
        "medium": sum(1 for r in rows if r["level"] == "medium"),
        "low": sum(1 for r in rows if r["level"] == "low"),
        "ok": sum(1 for r in rows if r["level"] == "ok"),
        "conflict": sum(1 for r in rows if r["target_exists"]),
        "cross_device": sum(1 for r in rows if r["cross_device"]),
        "duplicate": sum(1 for r in rows if any("整理记录" in x or "同名文件" in x for x in r["risks"])),
    }
    if not roots:
        reason = "没有可预演的目录：请先在设置里填一个容器内可见的下载目录，或先整理过一批文件让插件自动推断"
    elif not rows:
        reason = "预演目录里没有找到媒体文件（只读扫描，未做任何改动）"
    else:
        reason = ""
    return {
        "ok": bool(rows),
        "reason": reason,
        "roots": roots,
        "roots_auto": not bool(str(cfg.get("preview_dir") or "").strip()),
        "library_dirs": library_dirs,
        "mode_hint": mode_hint,
        "truncated": truncated,
        "max_files": common.MAX_SCAN_FILES,
        "files": rows,
        "summary": summary,
        "readonly": True,
        "note": (
            "预测路径按「推断出的目标目录 + 保持原文件名」保守给出；实际命名与分类由宿主整理链决定。"
            "本视图全程只读，不创建、不移动、不重命名、不删除任何文件。"
        ),
    }
