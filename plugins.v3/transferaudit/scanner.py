"""入库对账：下载器种子名录、映射目录主动扫描、核对清单与安全删除引擎。

与 auditor.py 的分工：
- auditor.py 负责「整理历史 ↔ 源/库文件 ↔ 种子状态」的只读对账；
- 本模块负责「下载器种子名录 ↔ 映射路径里的实体源文件」的主动扫描，
  并在用户显式开启后，按「扫描 → 预览 → 二次确认 → 分批执行」处理源文件清理。

安全约束（任何改动都不得放宽）：
1. 默认只读。删除能力必须由用户在设置里显式打开（allow_delete）。
2. 先扫描出完整列表，再单独生成删除预览（prepare，绝不删文件），
   最后才用独立的确认动作（delete）执行；两个动作互不隐式串联。
3. 只有「下载器名录成功取回 + 文件确实未被任何种子覆盖 + 路径确实在本次
   扫描根目录内 + 不是媒体库文件」的普通文件才可进入删除清单。
4. 下载器查不通、路径映射明显不对（扫描到的文件一个都匹配不上种子）、
   或任一条复检失败，整批拒绝执行，不做部分删除。
5. 默认移入隔离目录（可恢复）；「直接删除」必须用户在设置里单独选择。
6. 每批最多 10 个文件，逐条复检、逐条记录日志，失败即停。
7. 全程不触碰媒体库文件、目录、符号链接、远程存储与整理历史。

离线（无宿主模块）时扫描结果为空且 scan_ok=False，删除永远不可用。
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import secrets
import shutil
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# 只处理这些媒体文件，避免把字幕、nfo、图片、脚本等误当源文件。
MEDIA_EXTS = frozenset({
    ".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".iso", ".rmvb", ".rm",
    ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".vob",
    ".mk3d", ".strm", ".mp3", ".flac", ".mka",
})

SKIP_DIR_NAMES = frozenset({
    ".transferaudit-trash", "@eaDir", ".recycle", ".Trash", ".Trash-1000",
    ".git", ".svn", "node_modules", "__pycache__", "#recycle", "System Volume Information",
})

# 系统目录一律拒绝作为扫描根，防止误把整块盘当成「下载目录」。
DENY_ROOTS = frozenset({
    "/", "/etc", "/usr", "/bin", "/sbin", "/proc", "/sys", "/dev",
    "/boot", "/app", "/config", "/System", "/Library", "/Windows",
})

# 容器里常见的一级下载目录（一级目录默认拒绝，仅这些白名单放行）。
ALLOWED_TOP_ROOTS = frozenset({
    "/downloads", "/download", "/media", "/movies", "/tv", "/data",
    "/torrents", "/qbdownloads", "/qbittorrent", "/media_library", "/media-library",
})

MAX_SCAN_FILES = 20000
MAX_SCAN_DEPTH = 8
MAX_ROOTS = 24
MAX_BATCH = 10
PLAN_TTL = 600.0
LOG_MAX = 400
TRASH_DIRNAME = ".transferaudit-trash"
DELETE_CONFIRM = "DELETE_SOURCE_FILES"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "scan_dirs": "",       # 容器内可见的源目录，换行或逗号分隔；留空自动推断
    "path_map": "",        # 下载器路径=容器路径，每行一条
    "allow_delete": False,
    "delete_mode": "trash",  # trash（移入隔离目录，可恢复）| permanent（直接删除，不可恢复）
}

_STATE: Dict[str, Any] = {
    "token": "",
    "candidates": [],       # 本次扫描中允许进入删除清单的文件
    "roots": [],
    "protected": [],        # 媒体库文件 / 目录，删除时额外排除
    "plan": None,
    "ts": 0.0,
}
_LOG: deque = deque(maxlen=LOG_MAX)
_CHECKLIST: List[str] = []
_LOCK: Optional[threading.Lock] = None


def _lock() -> threading.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = threading.Lock()
    return _LOCK


def _try(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - 单一宿主接口失败不能让整页挂掉
        return default


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------
# 日志：同时写宿主插件日志（让「点日志」有内容）与内存日志（供页面/API 读取）
# ------------------------------------------------------------------

def emit(message: str, level: str = "info") -> None:
    """写一条日志。宿主日志器不可用时退回标准库，绝不因日志失败影响业务。"""
    text = f"[入库对账] {message}"
    _LOG.append(f"{_now()} [{level.upper()}] {message}")
    handled = False
    for module in ("app.runtime.log", "app.log"):
        try:
            mod = __import__(module, fromlist=["logger"])
            logger = getattr(mod, "logger", None)
            if logger is not None:
                getattr(logger, level, logger.info)(text)
                handled = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not handled:
        _try(lambda: getattr(logging.getLogger("app.plugins.transferaudit"), level, logging.getLogger("app.plugins.transferaudit").info)(text), None)


def set_seed_checklist(names: List[str]) -> None:
    """保存本次扫描的可复制种子名称清单，并写进宿主插件日志。

    用户可以在 MoviePilot 插件卡片点「日志」，直接复制种子名称去下载器核对。
    """
    global _CHECKLIST
    unique: List[str] = []
    for name in names:
        text = str(name or "").strip()
        if text and text not in unique:
            unique.append(text)
    with _lock():
        _CHECKLIST = unique
    if not unique:
        emit("本次扫描没有可复制的种子名称（下载器未连接或未返回种子）")
        return
    emit(f"本次可复制的种子名称共 {len(unique)} 个，逐条见下方清单：")
    for start in range(0, min(len(unique), 300), 20):
        emit("种子名称 | " + "\n种子名称 | ".join(unique[start:start + 20]))


def log_text(limit: int = 200) -> str:
    """返回可直接复制的日志文本，末尾附完整种子名称清单。"""
    with _lock():
        lines = list(_LOG)[-max(1, int(limit or 200)):]
        names = list(_CHECKLIST)
    if not lines:
        lines = [f"{_now()} [INFO] 暂无操作记录；打开插件详情页即会自动扫描并记录。"]
    text = "\n".join(lines)
    if names:
        text += (
            f"\n\n========== 可复制的种子名称清单（{len(names)} 个，粘到下载器搜索框逐个核对） ==========\n"
            + "\n".join(names)
        )
    return text


def log_lines(limit: int = 200) -> List[str]:
    with _lock():
        return list(_LOG)[-max(1, int(limit or 200)):]


# ------------------------------------------------------------------
# 配置解析与路径映射
# ------------------------------------------------------------------

def parse_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把宿主表单配置归一化；所有字段都有安全默认值。"""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(raw or {})
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["allow_delete"] = bool(cfg.get("allow_delete", False))
    mode = str(cfg.get("delete_mode") or "trash").strip().lower()
    cfg["delete_mode"] = "permanent" if mode in ("permanent", "delete", "direct", "直接删除") else "trash"
    cfg["scan_dirs"] = _split_lines(cfg.get("scan_dirs"))
    cfg["path_map"] = _parse_map(cfg.get("path_map"))
    return cfg


def _split_lines(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = str(value or "").replace(",", "\n").splitlines()
    return [p.strip().strip('"').strip("'") for p in parts if p and p.strip()]


def _parse_map(value: Any) -> List[Tuple[str, str]]:
    """解析「下载器路径=容器路径」，兼容 => 与换行/逗号分隔；可重复调用（幂等）。"""
    pairs: List[Tuple[str, str]] = []
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
        # parse_config 已归一化过的情形，避免二次解析把映射丢掉。
        for item in value:
            if len(item) >= 2:
                left = str(item[0]).strip().rstrip("/").replace("\\", "/")
                right = str(item[1]).strip().rstrip("/").replace("\\", "/")
                if left and right:
                    pairs.append((left, right))
        return pairs
    for line in _split_lines(value):
        text = line.replace("=>", "=")
        if "=" not in text:
            continue
        left, right = text.split("=", 1)
        left, right = left.strip().rstrip("/"), right.strip().rstrip("/")
        if left and right:
            pairs.append((left.replace("\\", "/"), right.replace("\\", "/")))
    return pairs


def map_path(path: Any, mapping: Sequence[Tuple[str, str]]) -> str:
    """把下载器侧路径映射成插件容器内可见的路径；无匹配时原样返回。"""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    for src, dst in mapping:
        if text == src:
            return dst
        if text.startswith(src + "/"):
            return dst + text[len(src):]
    return text


def _norm(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _is_safe_root(path: str) -> bool:
    text = os.path.normpath(str(path or ""))
    if not os.path.isabs(text) or text in DENY_ROOTS:
        return False
    for denied in DENY_ROOTS:
        # "/" 只做等值判断：若拿它做前缀，所有绝对路径都会被误拒绝。
        if denied == "/":
            continue
        if text == denied or text.startswith(denied.rstrip("/") + "/"):
            return False
    # 一级目录（如 /volume1）默认拒绝，避免把整块盘当成下载目录；
    # 只有常见的下载挂载点白名单才放行。
    if text.count(os.sep) == 1 and text not in ALLOWED_TOP_ROOTS:
        return False
    return os.path.isdir(text)


# ------------------------------------------------------------------
# 下载器种子名录
# ------------------------------------------------------------------

def _load_downloader_services() -> List[Any]:
    try:
        from app.sdk.services import DownloaderHelper  # type: ignore[import-not-found]

        services = DownloaderHelper().get_services() or {}
        return [s for s in services.values() if getattr(s, "instance", None)]
    except Exception:  # noqa: BLE001
        return []


def _t(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def fetch_torrents(mapping: Sequence[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], bool, int]:
    """读取全部下载器种子。返回 (名录, 是否成功查询过至少一台, 下载器实例数)。

    「查询失败」与「确实没有种子」严格区分：只有拿到 list 才算 probed。
    """
    services = _load_downloader_services()
    if not services:
        return [], False, 0

    torrents: List[Dict[str, Any]] = []
    probed = False
    for service in services:
        instance = getattr(service, "instance", None)
        lister = getattr(instance, "list_torrents", None)
        if not callable(lister):
            continue
        result = _try(lambda: lister(include_all_tags=True), None)
        if result is None:
            result = _try(lambda: lister(status="all", include_all_tags=True), None)
        if result is None:
            result = _try(lambda: lister(), None)
        if not isinstance(result, (list, tuple)):
            continue
        probed = True
        for item in result:
            raw_content = str(_t(item, "content_path") or _t(item, "path") or "").strip()
            save = str(_t(item, "save_path") or "").strip()
            title = str(_t(item, "title") or _t(item, "name") or "").strip()
            if not raw_content and save and title:
                raw_content = os.path.join(save, title)
            content = map_path(raw_content, mapping)
            torrents.append({
                "hash": str(_t(item, "hash") or "").strip().lower(),
                "title": title,
                "name": str(_t(item, "name") or "").strip(),
                "content_path": content,
                "raw_content_path": raw_content,
                "save_path": map_path(save, mapping),
                "size": float(_t(item, "size") or 0),
                "state": str(_t(item, "state") or "").strip(),
                "downloader": str(_t(item, "downloader") or getattr(service, "name", "") or "").strip(),
                "category": str(_t(item, "category") or "").strip(),
            })
    return torrents, probed, len(services)


def build_coverage(torrents: Sequence[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """从种子名录构造「文件仍被种子覆盖」的路径集合。"""
    files: Set[str] = set()
    dirs: Set[str] = set()
    for t in torrents:
        content = str(t.get("content_path") or "")
        if not content:
            continue
        # 带扩展名的 content_path 视为单文件种子，否则视为种子内容目录。
        if os.path.splitext(content)[1]:
            files.add(_norm(content))
        else:
            dirs.add(_norm(content))
        # 注意：绝不把 save_path（下载根目录）整体加进覆盖目录，
        # 否则所有残留都会被误判成「仍在做种」。
    return {"files": files, "dirs": sorted(dirs, key=len, reverse=True)}


def is_covered(path: str, coverage: Dict[str, Set[str]]) -> bool:
    rp = _norm(path)
    if rp in coverage["files"]:
        return True
    for d in coverage["dirs"]:
        if rp == d or rp.startswith(d + os.sep):
            return True
    return False


# ------------------------------------------------------------------
# 映射目录主动扫描
# ------------------------------------------------------------------

def resolve_roots(
    cfg: Dict[str, Any],
    torrents: Sequence[Dict[str, Any]],
    histories: Sequence[Any],
    field,
) -> Tuple[List[str], List[str]]:
    """返回 (可用扫描根, 被忽略的配置项)。配置优先，留空时从下载器与整理历史推断。"""
    configured = list(cfg.get("scan_dirs") or [])
    roots: List[str] = []
    ignored: List[str] = []
    for item in configured:
        if _is_safe_root(item):
            roots.append(_norm(item))
        else:
            ignored.append(item)

    if not roots:
        candidates: List[str] = []
        for t in torrents:
            content = str(t.get("content_path") or "")
            if content:
                candidates.append(content if os.path.splitext(content)[1] else content)
                candidates.append(os.path.dirname(content))
            if t.get("save_path"):
                candidates.append(str(t["save_path"]))
        for item in histories:
            src = str(field(item, "src") or "")
            if src:
                mapped = map_path(src, cfg.get("path_map") or [])
                candidates.append(os.path.dirname(mapped))
        for item in candidates:
            if _is_safe_root(item):
                roots.append(_norm(item))

    seen: Set[str] = set()
    unique: List[str] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
        if len(unique) >= MAX_ROOTS:
            break
    return unique, ignored


def scan_files(
    cfg: Dict[str, Any],
    histories: Sequence[Any],
    torrents: Sequence[Dict[str, Any]],
    probed: bool,
    field,
) -> Dict[str, Any]:
    """遍历映射目录里的实体媒体文件，与种子名录、整理历史逐条对比。"""
    roots, ignored = resolve_roots(cfg, torrents, histories, field)
    coverage = build_coverage(torrents)
    mapping = cfg.get("path_map") or []
    known_src: Set[str] = set()
    protected: List[str] = []
    for item in histories:
        src = str(field(item, "src") or "")
        dest = str(field(item, "dest") or "")
        if src:
            known_src.add(_norm(map_path(src, mapping)))
        if dest and str(field(item, "dest_storage") or "local").lower() in ("", "local", "none"):
            protected.append(_norm(map_path(dest, mapping)))

    files: List[Dict[str, Any]] = []
    matched = 0
    truncated = False
    limit = int(cfg.get("max_files") or MAX_SCAN_FILES)
    limit = max(100, min(MAX_SCAN_FILES, limit))

    for root in roots:
        base_depth = root.rstrip(os.sep).count(os.sep)
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            if current.rstrip(os.sep).count(os.sep) - base_depth >= MAX_SCAN_DEPTH:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")]
            for filename in filenames:
                if len(files) >= limit:
                    truncated = True
                    break
                if os.path.splitext(filename)[1].lower() not in MEDIA_EXTS:
                    continue
                path = os.path.abspath(os.path.join(current, filename))
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                st = _try(lambda p=path: os.stat(p), None)
                if st is None:
                    continue
                rp = _norm(path)
                covered = is_covered(path, coverage)
                if covered:
                    matched += 1
                known = rp in known_src
                files.append({
                    "index": len(files),
                    "name": filename,
                    "path": path,
                    "root": root,
                    "size": int(getattr(st, "st_size", 0) or 0),
                    "size_text": _human(int(getattr(st, "st_size", 0) or 0)),
                    "mtime": float(getattr(st, "st_mtime", 0) or 0),
                    "covered": covered,
                    "known": known,
                    "status": "active" if covered else ("residue" if known else "unregistered"),
                })
            if truncated:
                break
        if truncated:
            break

    reasons: List[str] = []
    if not probed:
        reasons.append("未能读取下载器种子名录（下载器未连接或查询失败）")
    if not roots:
        reasons.append("没有可用的扫描目录，请在设置里填写容器内可见的源目录（映射路径）")
    if ignored:
        reasons.append(f"有 {len(ignored)} 个配置的目录不可用或属于系统目录，已忽略")
    if files and matched == 0:
        reasons.append("扫描到的文件没有一个能匹配下载器种子，路径映射可能不正确")
    if truncated:
        reasons.append(f"文件数达到 {limit} 条上限，本次为局部扫描")

    # 关键防误删：以上任一异常都不允许生成删除清单。
    scan_ok = bool(probed and roots and files and matched > 0 and not truncated)
    if not reasons:
        reasons.append("下载器名录与映射目录均正常")

    for f in files:
        deletable = bool(
            scan_ok
            and not f["covered"]
            and rp_inside(f["path"], roots)
            and _norm(f["path"]) not in protected
        )
        f["deletable"] = deletable
        if not scan_ok:
            f["status"] = "unverified"

    return {
        "ok": scan_ok,
        "reason": "；".join(reasons),
        "roots": roots,
        "ignored": ignored,
        "files": files,
        "file_total": len(files),
        "matched": matched,
        "truncated": truncated,
        "torrent_total": len(torrents),
        "torrent_probed": bool(probed),
        "protected": protected,
    }


def rp_inside(path: str, roots: Sequence[str]) -> bool:
    """realpath 包含判断，防止符号链接或 .. 把删除范围带出扫描目录。"""
    rp = _norm(path)
    return any(rp == r or rp.startswith(r + os.sep) for r in roots)


# ------------------------------------------------------------------
# 扫描状态 / 删除预览 / 执行
# ------------------------------------------------------------------

def register_scan(result: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    """登记本次扫描的候选清单并签发令牌；删除动作必须带上这个令牌。

    候选清单未变化时复用旧令牌并保留已生成的预览，避免缓存到期重建快照
    把用户刚生成的删除预览冲掉；清单一旦变化就换令牌并作废预览。
    """
    candidates = [dict(f) for f in result["files"] if f.get("deletable")]
    roots = list(result["roots"])
    protected = list(result.get("protected") or [])
    signature = [str(f.get("path") or "") for f in candidates]
    with _lock():
        old_signature = [str(f.get("path") or "") for f in (_STATE.get("candidates") or [])]
        unchanged = bool(_STATE.get("token")) and old_signature == signature
        token = str(_STATE["token"]) if unchanged else secrets.token_hex(8)
        _STATE["token"] = token
        _STATE["candidates"] = candidates
        _STATE["roots"] = roots
        _STATE["protected"] = protected
        _STATE["ts"] = time.time()
        if not unchanged:
            _STATE["plan"] = None
            emit(
                f"扫描完成：目录 {len(roots)} 个，媒体文件 {result['file_total']} 个，"
                f"被种子覆盖 {result['matched']} 个，种子名录 {result['torrent_total']} 条；"
                f"可清理候选 {len(candidates)} 个（本次令牌 {token}）。"
            )
    if not result["ok"]:
        emit(f"扫描未通过安全校验，删除已禁用：{result['reason']}", "warning")
    return token


def current_token() -> str:
    with _lock():
        return str(_STATE.get("token") or "")


def current_plan() -> Optional[Dict[str, Any]]:
    with _lock():
        plan = _STATE.get("plan")
        if not plan:
            return None
        if time.time() - float(plan.get("ts") or 0) > PLAN_TTL:
            _STATE["plan"] = None
            return None
        return dict(plan)


def clear_plan() -> None:
    with _lock():
        _STATE["plan"] = None


def _deny(message: str) -> Dict[str, Any]:
    emit(f"删除被拒绝：{message}", "warning")
    return {"ok": False, "message": message}


def prepare(
    cfg: Dict[str, Any],
    token: str,
    indices: Optional[Sequence[Any]] = None,
    scope: str = "selected",
) -> Dict[str, Any]:
    """第一步：只生成删除预览，绝不删除或移动任何文件。"""
    if not cfg.get("allow_delete"):
        return _deny("删除能力未开启，请先在插件设置里打开「启用源文件删除能力」")
    with _lock():
        expected = str(_STATE.get("token") or "")
        candidates = [dict(f) for f in _STATE.get("candidates") or []]
        roots = list(_STATE.get("roots") or [])
        protected = list(_STATE.get("protected") or [])
    if not expected or str(token or "") != expected:
        return _deny("扫描结果已变化或已过期，请先点「重新扫描」再生成预览")
    if not candidates:
        return _deny("本次扫描没有可清理的残留源文件")

    if str(scope or "").lower() == "all":
        chosen = list(candidates)
    else:
        wanted: Set[int] = set()
        for raw in indices or []:
            try:
                wanted.add(int(raw))
            except (TypeError, ValueError):
                continue
        chosen = [f for f in candidates if int(f["index"]) in wanted]
    if not chosen:
        return _deny("没有选中任何文件，请先在列表里勾选")
    if len(chosen) > MAX_BATCH:
        chosen = chosen[:MAX_BATCH]

    # 预览阶段也复检一次，避免扫描后文件被移动/种子被重新添加。
    fresh, probed, _ = fetch_torrents(cfg.get("path_map") or [])
    if not probed:
        return _deny("下载器种子名录读取失败，无法确认这些文件确实已无种子，禁止删除")
    coverage = build_coverage(fresh)
    verified: List[Dict[str, Any]] = []
    for f in chosen:
        path = str(f.get("path") or "")
        if not _verify_one(path, roots, protected, coverage):
            return _deny(f"复检未通过，已取消整批预览：{path}")
        verified.append(dict(f))

    plan_id = secrets.token_hex(8)
    plan = {
        "id": plan_id,
        "ts": time.time(),
        "items": verified,
        "mode": cfg.get("delete_mode") or "trash",
        "roots": roots,
        "protected": protected,
        "path_map": list(cfg.get("path_map") or []),
        "count": len(verified),
        "bytes": sum(int(f.get("size") or 0) for f in verified),
    }
    with _lock():
        _STATE["plan"] = plan
    emit(
        f"已生成删除预览（未删除任何文件）：{plan['count']} 个源文件，"
        f"合计 {_human(plan['bytes'])}，模式={plan['mode']}，预览编号 {plan_id}。"
        "需再次确认才会执行。"
    )
    return {
        "ok": True,
        "message": f"预览已生成（未删除任何文件）：{plan['count']} 个源文件，需再次确认才会删除",
        "data": {
            "plan_id": plan_id,
            "count": plan["count"],
            "size": _human(plan["bytes"]),
            "mode": plan["mode"],
            "items": [
                {"index": f["index"], "name": f["name"], "path": f["path"], "size": _human(f.get("size") or 0)}
                for f in verified
            ],
        },
    }


def _verify_one(
    path: str,
    roots: Sequence[str],
    protected: Sequence[str],
    coverage: Dict[str, Set[str]],
) -> bool:
    if not path or not os.path.isabs(path):
        return False
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    if not rp_inside(path, roots):
        return False
    rp = _norm(path)
    if rp in {_norm(p) for p in protected}:
        return False
    if is_covered(path, coverage):
        return False
    return True


def _human(num: float) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def execute(cfg: Dict[str, Any], plan_id: str, confirm: str) -> Dict[str, Any]:
    """第二步：只有显式确认 + 有效预览 + 全批复检通过，才真正处理源文件。"""
    if not cfg.get("allow_delete"):
        return _deny("删除能力未开启")
    if str(confirm or "").strip() != DELETE_CONFIRM:
        return _deny("确认口令不正确，未执行任何删除")
    plan = current_plan()
    if not plan or str(plan_id or "") != str(plan.get("id") or ""):
        return _deny("删除预览不存在或已过期，请重新生成预览")
    items = list(plan.get("items") or [])
    if not items:
        return _deny("预览清单为空")
    if len(items) > MAX_BATCH:
        return _deny(f"单批超过 {MAX_BATCH} 个文件上限")

    roots = list(plan.get("roots") or [])
    protected = list(plan.get("protected") or [])
    mapping = list(plan.get("path_map") or [])
    mode = str(plan.get("mode") or "trash")

    # 执行前再取一次种子名录：只要查不通就整批拒绝，绝不误删。
    fresh, probed, _ = fetch_torrents(mapping)
    if not probed:
        return _deny("下载器种子名录读取失败，无法确认种子已删，整批取消")
    coverage = build_coverage(fresh)
    for item in items:
        if not _verify_one(str(item.get("path") or ""), roots, protected, coverage):
            return _deny(f"执行前复检未通过，整批取消：{item.get('path')}")

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    done: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for item in items:
        path = str(item.get("path") or "")
        try:
            if mode == "permanent":
                os.remove(path)
                action = "直接删除（不可恢复）"
            else:
                target = _trash_target(path, roots, stamp)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(path, target)
                action = f"移入隔离目录 {target}"
            done.append({"path": path, "action": action})
            emit(f"已处理源文件：{path} → {action}", "warning")
        except Exception as error:  # noqa: BLE001
            failed.append({"path": path, "error": f"{type(error).__name__}: {error}"})
            emit(f"处理失败，已停止本批：{path} → {type(error).__name__}: {error}", "error")
            break

    clear_plan()
    message = (
        f"本批处理 {len(done)} 个源文件"
        + (f"，失败 {len(failed)} 个" if failed else "")
        + f"；模式：{'直接删除（不可恢复）' if mode == 'permanent' else '移入隔离目录（可恢复）'}"
    )
    emit(message, "warning")
    return {
        "ok": not failed,
        "message": message + "。请点「重新扫描」刷新结果。",
        "data": {"done": done, "failed": failed, "mode": mode, "count": len(done)},
    }


def _trash_target(path: str, roots: Sequence[str], stamp: str) -> str:
    root = next((r for r in roots if rp_inside(path, [r])), roots[0] if roots else os.path.dirname(path))
    rel = os.path.relpath(path, root)
    target = os.path.join(root, TRASH_DIRNAME, stamp, rel)
    base, ext = os.path.splitext(target)
    suffix = 1
    while os.path.exists(target):
        target = f"{base}.{suffix}{ext}"
        suffix += 1
    return target
