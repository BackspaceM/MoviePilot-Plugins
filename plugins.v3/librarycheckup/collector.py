"""媒体库体检 —— 快照聚合与缓存。

三个页面共用一次只读采集，不用各查一遍宿主；缓存按配置签名，
配置变化或点「重新体检」时强制重算。全程只读，无写操作。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Optional

from . import common, ledger, parity, preview

CACHE_TTL = 120.0
_CACHE: Dict[str, Any] = {"at": 0.0, "sig": "", "data": None}
VIEWS = ("preview", "ledger", "parity")


def parse_config(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """精简配置：只有一个可选的预演目录，留空全自动。"""
    source = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(source.get("enabled", True)),
        "preview_dir": str(source.get("preview_dir") or "").strip(),
    }


def invalidate() -> None:
    _CACHE.update({"at": 0.0, "sig": "", "data": None})


def collect(force: bool = False, config: Optional[Dict[str, Any]] = None,
            view: str = "preview") -> Dict[str, Any]:
    cfg = parse_config(config)
    sig = hashlib.md5(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    now = time.monotonic()
    cached = _CACHE.get("data")
    if (not force and cached and _CACHE.get("sig") == sig
            and now - float(_CACHE.get("at") or 0) < CACHE_TTL):
        return dict(cached, view=view if view in VIEWS else "preview")

    histories, hist_ok, hist_reason = common.fetch_histories()
    dirs = common.infer_dirs(histories)
    servers, servers_ok, servers_reason = common.fetch_servers()
    common.emit(
        f"开始只读体检：整理历史 {len(histories)} 条"
        f"（{'正常' if hist_ok else hist_reason}）、"
        f"推断下载目录 {len(dirs['download'])} 个 / 媒体库目录 {len(dirs['library'])} 个、"
        f"已连接媒体服务器 {len(servers)} 家。"
    )

    a = preview.build_preview(cfg, histories, dirs)
    b = ledger.build_ledger(cfg, histories, dirs)
    c = parity.build_parity(cfg, servers, servers_ok, servers_reason)

    common.emit(
        f"体检完成：整理预演 {a['summary']['total']} 个文件"
        f"（高风险 {a['summary']['high']} / 跨盘 {a['summary']['cross_device']}）、"
        f"入库账本 {b['summary']['total']} 条记录"
        f"（退化复制 {b['summary']['degraded_copy']} / 未核验 {b['summary']['unverified']}）、"
        f"媒体库对比 {c['summary']['total']} 个媒体身份"
        f"（可核验 {c['summary']['servers_ok']}/{c['summary']['servers_total']} 家）。"
        "全程只读，未修改任何文件、配置或整理历史。"
    )

    snap: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "view": view if view in VIEWS else "preview",
        "config": cfg,
        "readonly": True,
        "histories": {"total": len(histories), "ok": hist_ok, "reason": hist_reason},
        "dirs": dirs,
        "servers_found": len(servers),
        "preview": a,
        "ledger": b,
        "parity": c,
        "log": common.log_lines(200),
        "notes": [
            "本插件全程只读：不创建、移动、重命名、删除任何文件，不改动整理历史、下载器任务与媒体服务器。",
            "远程存储与容器内不可见的路径一律标「未核验」，绝不把查不到当成已删除。",
            "命名与分类规则由宿主整理链决定，整理预演里的目标路径是保守估计，仅供事前核对。",
        ],
    }
    _CACHE.update({"at": now, "sig": sig, "data": snap})
    return dict(snap, view=view if view in VIEWS else "preview")
