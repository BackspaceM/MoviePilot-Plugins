"""媒体库体检 —— 公共只读工具层。

A / B / C 三个视图共用同一套「只读采集 → 对齐 → 出报告」骨架。
本模块**绝不执行任何写操作**：不创建/删除/移动文件，不调用任何宿主写入口。

宿主依赖一律放在函数内部 try/except，离线 stub 测试也能完整加载。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOG_PREFIX = "[LibraryCheckup]"
PLUGIN_LOGGER_NAME = "app.plugins.librarycheckup"

MEDIA_EXTS = frozenset({
    ".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".iso", ".rmvb", ".rm", ".mov",
    ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".vob", ".mk3d",
    ".strm", ".mp3", ".flac", ".mka", ".aac", ".wav",
})

SKIP_DIR_NAMES = frozenset({
    "@eaDir", ".recycle", ".Trash", "#recycle", ".transferaudit-trash",
    ".@__thumb", "node_modules", ".git", "__pycache__", "System Volume Information",
})

PAGE_SIZE = 200
MAX_HISTORY = 5000
MAX_SCAN_FILES = 3000
MAX_SCAN_DEPTH = 6
LOG_MAX = 400
HTTP_TIMEOUT = 8.0

_LOG: List[Dict[str, str]] = []


# ----------------------------------------------------------------------
# 日志：页面常驻 + 宿主插件日志（插件卡片点「日志」可见）
# ----------------------------------------------------------------------

def emit(message: str, level: str = "info") -> None:
    text = str(message or "").strip()
    if not text:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    _LOG.append({"time": stamp, "level": level, "text": text})
    if len(_LOG) > LOG_MAX:
        del _LOG[: len(_LOG) - LOG_MAX]
    line = f"{LOG_PREFIX} {text}"
    try:
        from app.runtime.log import logger  # type: ignore
        getattr(logger, level if level in ("info", "warning", "error", "debug") else "info")(line)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.log import logger as legacy_logger  # type: ignore
        legacy_logger.info(line)
        return
    except Exception:  # noqa: BLE001
        pass
    import logging
    getattr(logging.getLogger(PLUGIN_LOGGER_NAME),
            "warning" if level in ("warning", "error") else "info")(line)


def log_lines(limit: int = 200) -> List[str]:
    return [f"[{x['time']}] {x['text']}" for x in _LOG[-max(1, int(limit)):]]


def log_text(limit: int = 200) -> str:
    return "\n".join(log_lines(limit))


# ----------------------------------------------------------------------
# 通用归一与只读探测
# ----------------------------------------------------------------------

def cfg_attr(obj: Any, *names: str) -> Any:
    """从 dict / pydantic 模型 / 普通对象里按顺序取第一个非空属性。"""
    for name in names:
        if obj is None:
            return None
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value not in (None, "", 0, False, [], {}):
            return value
    return None


def as_dict(obj: Any) -> Dict[str, Any]:
    """把快照模型 / dict 统一成普通字典，绝不修改原对象。"""
    if isinstance(obj, dict):
        return dict(obj)
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:  # noqa: BLE001
                continue
    out: Dict[str, Any] = {}
    for key in dir(obj):
        if key.startswith("_"):
            continue
        try:
            value = getattr(obj, key)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(value, (str, int, float, bool, type(None), dict, list, tuple)):
            out[key] = value
    return out


def is_media_file(name: str) -> bool:
    return os.path.splitext(str(name or ""))[1].lower() in MEDIA_EXTS


def fmt_size(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.1f} {units[idx]}" if idx else f"{int(size)} B"


def probe(path: Any, storage: Any = "") -> Dict[str, Any]:
    """只读探测路径。

    state 三态：present（确凿存在）/ missing（父目录可见但文件确实不在）/
    unknown（远程存储、路径不可见、无权限等，一律不推断为已删除）。
    """
    result: Dict[str, Any] = {
        "state": "unknown", "dev": None, "ino": None, "size": 0, "mtime": 0.0,
    }
    text = str(path or "").strip()
    store = str(storage or "").strip().lower()
    if store and store not in ("local", "none", ""):
        return result
    if not text.startswith("/"):
        return result
    try:
        stat = os.stat(text)
        result.update({
            "state": "present", "dev": stat.st_dev, "ino": stat.st_ino,
            "size": stat.st_size, "mtime": stat.st_mtime,
        })
        return result
    except (FileNotFoundError, NotADirectoryError):
        parent = os.path.dirname(text.rstrip("/")) or "/"
        if os.path.isdir(parent):
            result["state"] = "missing"
        return result
    except OSError:
        return result


def parse_filename(name: str) -> Dict[str, Any]:
    """保守解析文件名：标题 / 年份 / 季集 / 类型。不调用宿主识别，避免写操作。"""
    raw = os.path.splitext(str(name or ""))[0]
    season = episode = ""
    match = re.search(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})", raw)
    if match:
        season, episode = f"S{int(match.group(1)):02d}", f"E{int(match.group(2)):02d}"
    else:
        match = re.search(r"(?:第\s*)?(\d{1,3})\s*(?:集|话|話)", raw)
        if match:
            episode = f"E{int(match.group(1)):02d}"
        else:
            match = re.search(r"[\s._\[(](\d{2,3})[\s._\])]", raw)
            if match:
                episode = f"E{int(match.group(1)):02d}"
    year_match = re.search(r"(?:19|20)\d{2}", raw)
    year = year_match.group(0) if year_match else ""
    title = re.sub(r"\[[^]]*\]|\([^)]*\)|【[^】]*】", " ", raw)
    title = re.sub(r"[Ss]\d{1,2}[\s._-]*[Ee]\d{1,3}.*$", " ", title, flags=re.I)
    title = re.sub(r"\b(?:19|20)\d{2}\b", " ", title)
    title = re.sub(
        r"\b(?:2160p|1080p|720p|480p|4K|x264|x265|H\.?264|H\.?265|HEVC|AVC|"
        r"AAC|FLAC|DDP|TrueHD|WEB-?DL|BluRay|BDRip|HDTV|10bit|HDR|REMUX)\b",
        " ", title, flags=re.I,
    )
    title = re.sub(r"[._-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -_[]()")
    return {
        "title": title or raw.strip(),
        "year": year,
        "season": season,
        "episode": episode,
        "kind": "tv" if (season or episode) else "movie",
    }


def normalize_title(text: Any) -> str:
    """跨三家媒体服务器对齐用的宽松标题键。"""
    value = str(text or "").lower()
    value = re.sub(r"\[[^]]*\]|\([^)]*\)|【[^】]*】", " ", value)
    value = re.sub(r"\b(?:19|20)\d{2}\b", " ", value)
    value = re.sub(
        r"\b(?:2160p|1080p|720p|480p|4k|x264|x265|h264|h265|hevc|"
        r"web-?dl|bluray|bdrip|10bit|hdr|remux|aac|flac)\b", " ", value,
    )
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def walk_files(root: str, max_files: int = MAX_SCAN_FILES,
               max_depth: int = MAX_SCAN_DEPTH) -> Tuple[List[str], bool]:
    """只读遍历目录，返回 (绝对路径列表, 是否截断)。

    不跟随符号链接目录，跳过回收站/缩略图等系统目录。
    """
    found: List[str] = []
    truncated = False
    if not root or not os.path.isdir(root):
        return found, False
    base_depth = root.rstrip("/").count("/")
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs
                         if d not in SKIP_DIR_NAMES and not d.startswith(".transferaudit"))
        if current.count("/") - base_depth >= max_depth:
            dirs[:] = []
        for name in sorted(files):
            if not is_media_file(name):
                continue
            full = os.path.join(current, name)
            if os.path.islink(full):
                continue
            found.append(full)
            if len(found) >= max_files:
                truncated = True
                return found, truncated
    return found, truncated


# ----------------------------------------------------------------------
# 宿主只读数据源
# ----------------------------------------------------------------------

def fetch_histories(limit: int = MAX_HISTORY) -> Tuple[List[Dict[str, Any]], bool, str]:
    """分页读取全量整理历史快照。失败时返回已取到的部分并明确标为未核验。"""
    try:
        from app.sdk import queries  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return [], False, f"宿主 SDK 不可用（{type(exc).__name__}）"
    fn = getattr(queries, "list_transfer_history", None)
    if not callable(fn):
        return [], False, "宿主未提供 list_transfer_history"
    out: List[Dict[str, Any]] = []
    page_no = 1
    reason = ""
    while len(out) < limit:
        count = min(PAGE_SIZE, limit - len(out))
        try:
            page = fn(page={"page": page_no, "count": count})
        except Exception as exc:  # noqa: BLE001
            reason = f"第 {page_no} 页读取失败（{type(exc).__name__}），仅展示已取到的部分"
            break
        items = list(getattr(page, "items", None) or [])
        total = int(getattr(page, "total", len(items)) or 0)
        for item in items:
            out.append(as_dict(item))
        if not items or len(items) < count or (total and len(out) >= total):
            break
        page_no += 1
    return out, not reason, reason


def infer_dirs(histories: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
    """从整理历史反推下载目录与媒体库目录，做到零配置可用。"""
    src_counter: Dict[str, int] = {}
    dest_counter: Dict[str, int] = {}
    for hist in histories:
        if str(cfg_attr(hist, "src_storage") or "local").lower() not in ("local", "", "none"):
            src = ""
        else:
            src = str(cfg_attr(hist, "src") or "")
        dest = str(cfg_attr(hist, "dest") or "")
        if str(cfg_attr(hist, "dest_storage") or "local").lower() not in ("local", "", "none"):
            dest = ""
        for path, counter in ((src, src_counter), (dest, dest_counter)):
            if not path.startswith("/"):
                continue
            parent = os.path.dirname(path.rstrip("/"))
            if parent and parent != "/":
                counter[parent] = counter.get(parent, 0) + 1

    def top(counter: Dict[str, int], limit: int = 6) -> List[str]:
        # 优先保留更靠近根且出现次数多的目录，避免同一剧集几十集把列表挤满
        ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].count("/"), kv[0]))
        return [path for path, _ in ordered[:limit]]

    return {"download": top(src_counter), "library": top(dest_counter)}


def fetch_servers() -> Tuple[List[Dict[str, Any]], bool, str]:
    """自动发现已连接的 Emby / Jellyfin / Plex，零配置。密钥只留在内存。"""
    try:
        from app.sdk.services import MediaServerHelper  # type: ignore
        configs = MediaServerHelper().get_configs() or {}
    except Exception as exc:  # noqa: BLE001
        return [], False, f"读取已连接媒体服务器失败（{type(exc).__name__}）"
    servers: List[Dict[str, Any]] = []
    items = configs.items() if isinstance(configs, dict) else []
    for key, cfg in items:
        inner = cfg_attr(cfg, "config") or {}
        if not isinstance(inner, dict):
            inner = as_dict(inner)
        stype = str(cfg_attr(cfg, "type", "server_type")
                    or cfg_attr(inner, "type", "server_type") or "").lower()
        if stype not in ("emby", "jellyfin", "plex"):
            continue
        url = (cfg_attr(cfg, "url", "address", "host", "base_url")
               or cfg_attr(inner, "url", "address", "host", "base_url"))
        apikey = (cfg_attr(cfg, "apikey", "api_key", "token", "access_token")
                  or cfg_attr(inner, "apikey", "api_key", "token", "access_token"))
        servers.append({
            "name": str(key), "type": stype, "url": str(url or "").rstrip("/"),
            "apikey": str(apikey or ""),
        })
    return servers, True, ""


def http_get(url: str, params: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None,
             timeout: float = HTTP_TIMEOUT) -> Any:
    """只读 GET，返回解析后的 JSON；异常向上抛，由调用方降级为「未核验」。"""
    query = urllib.parse.urlencode(
        {k: v for k, v in (params or {}).items() if v is not None}
    )
    full = url + (("?" + query) if query else "")
    req = urllib.request.Request(full, headers=dict(headers or {}), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return {}
