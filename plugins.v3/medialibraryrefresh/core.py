"""媒体库精准刷新 - 零宿主依赖的纯逻辑层。

本模块不导入 MoviePilot 的任何模块，可直接在离线环境中单测。负责：
1. 从整理完成事件里提取目标文件路径；
2. 路径映射（MP 容器路径 -> 媒体服务器看到的路径）；
3. 服务器清单解析、刷新项去重与按目录合并；
4. 合并窗口/批量上限/最大等待的调度判定；
5. cron 校验、重试退避、凭据脱敏等通用工具。
"""

from __future__ import annotations

import posixpath
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse, urlunparse

DEFAULT_MEDIA_EXTS = (
    ".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".iso", ".rmvb", ".mov",
    ".wmv", ".flv", ".mpg", ".mpeg", ".webm", ".m4v", ".3gp", ".vob",
    ".mp3", ".flac", ".m4a", ".aac", ".wav", ".strm", ".nfo",
)
DEFAULT_SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".sub", ".vtt", ".sup", ".idx")

CREATED = "Created"
MODIFIED = "Modified"
DELETED = "Deleted"
UPDATE_TYPES = (CREATED, MODIFIED, DELETED)

SERVER_TYPES = ("emby", "jellyfin", "plex")
EMBY_TYPES = ("emby", "jellyfin")
FALLBACK_LEVELS = ("none", "parent", "library", "full")
NOTIFY_MODES = ("全部", "仅异常", "关闭")

_CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_TOKEN = re.compile(r"^(\*|\d+|\d+-\d+|\*/\d+)(,(\*|\d+|\d+-\d+|\*/\d+))*$")
_QUERY_SECRET = re.compile(
    r"((?:api[_-]?key|token|secret|pass(?:word)?|pwd)=)[^&\s]+", re.IGNORECASE
)
_URL_CREDENTIAL = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")


@dataclass
class ServerConfig:
    """一台 Emby/Jellyfin/Plex 服务器。"""

    type: str
    name: str
    url: str
    token: str
    mappings: List[Tuple[str, str]] = field(default_factory=list)
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{self.type}:{self.name}"

    @property
    def is_plex(self) -> bool:
        return self.type == "plex"

    @property
    def label(self) -> str:
        return f"{self.name}[{self.type}]"

    def map_path(self, path: str) -> str:
        return apply_path_mapping(path, self.mappings)


@dataclass
class RefreshItem:
    """一条待刷新记录，path 始终是 MP 侧的原始路径。"""

    path: str
    update_type: str = CREATED
    attempts: int = 0
    ts: float = 0.0
    source: str = "event"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "update_type": self.update_type,
            "attempts": self.attempts,
            "ts": self.ts,
            "source": self.source,
        }

    @staticmethod
    def from_dict(data: Any) -> Optional["RefreshItem"]:
        if not isinstance(data, dict):
            return None
        path = str(data.get("path") or "").strip()
        if not path:
            return None
        update_type = str(data.get("update_type") or CREATED)
        if update_type not in UPDATE_TYPES:
            update_type = CREATED
        try:
            attempts = max(int(data.get("attempts") or 0), 0)
        except (TypeError, ValueError):
            attempts = 0
        try:
            ts = float(data.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        return RefreshItem(
            path=path,
            update_type=update_type,
            attempts=attempts,
            ts=ts,
            source=str(data.get("source") or "event"),
        )


# ---------------------------------------------------------------------------
# 路径与 URL
# ---------------------------------------------------------------------------

def normalize_path(path: Any) -> str:
    """统一为 posix 风格绝对路径，去掉尾斜杠与重复分隔符。"""
    text = str(path or "").strip().strip('"\'')
    if not text:
        return ""
    text = text.replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text)
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def normalize_base_url(url: Any) -> str:
    """补全协议、去尾斜杠；保留反代路径前缀。"""
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "http://" + text
    return text


def join_url(base: str, endpoint: str) -> str:
    """拼接 base 与 endpoint；base 已含 /emby 或 /jellyfin 前缀时避免重复。"""
    url = normalize_base_url(base)
    if not url:
        return ""
    parts = urlparse(url)
    base_path = parts.path.rstrip("/")
    ep = "/" + str(endpoint or "").lstrip("/")
    first = ep.strip("/").split("/")[0].lower() if ep.strip("/") else ""
    if first in EMBY_TYPES and base_path.lower().endswith("/" + first):
        base_path = base_path[: -(len(first) + 1)].rstrip("/")
    return urlunparse((parts.scheme, parts.netloc, base_path + ep, "", parts.query, ""))


def parse_mappings(text: Any) -> List[Tuple[str, str]]:
    """解析路径映射：`源::目标`，多个用分号或换行分隔；按源长度降序便于最长前缀匹配。"""
    result: List[Tuple[str, str]] = []
    for raw in re.split(r"[;\n]+", str(text or "")):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.split(r"::|=>|=", line, maxsplit=1)
        if len(match) != 2:
            continue
        src, dst = normalize_path(match[0]), normalize_path(match[1])
        if src and dst:
            result.append((src, dst))
    result.sort(key=lambda pair: len(pair[0]), reverse=True)
    return result


def apply_path_mapping(path: Any, mappings: Sequence[Tuple[str, str]]) -> str:
    """最长前缀匹配；只在前缀边界（相等或后跟 /）命中，避免 /media 误伤 /media2。"""
    target = normalize_path(path)
    if not target or not mappings:
        return target
    for src_raw, dst_raw in mappings:
        src, dst = normalize_path(src_raw), normalize_path(dst_raw)
        if not src or not dst:
            continue
        if target == src:
            return dst
        prefix = src.rstrip("/") + "/"
        if target.startswith(prefix):
            return dst.rstrip("/") + "/" + target[len(prefix):]
    return target


def parse_servers(text: Any) -> Tuple[List[ServerConfig], List[str]]:
    """解析服务器清单，每行：`类型|名称|地址|密钥|路径映射|启用`。

    - 类型：emby / jellyfin / plex
    - 路径映射：可留空，多个用分号分隔，如 `/media::/volume1/media;/tv::/volume2/tv`
    - 启用：1/0/true/false/yes/no，留空默认启用
    """
    servers: List[ServerConfig] = []
    errors: List[str] = []
    for lineno, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [part.strip() for part in line.split("|")]
        if len(fields) < 4:
            errors.append(f"第 {lineno} 行字段不足（需 类型|名称|地址|密钥|映射|启用）")
            continue
        stype = fields[0].lower()
        if stype in ("em", "jf", "jellyfin"):
            stype = "jellyfin" if stype in ("jf", "jellyfin") else "emby"
        if stype not in SERVER_TYPES:
            errors.append(f"第 {lineno} 行类型无效：{fields[0]}")
            continue
        name = fields[1] or f"{stype}-{lineno}"
        url = normalize_base_url(fields[2])
        token = fields[3]
        if not url:
            errors.append(f"第 {lineno} 行地址为空")
            continue
        if not token:
            errors.append(f"第 {lineno} 行密钥为空")
            continue
        mappings = parse_mappings(fields[4]) if len(fields) > 4 else []
        enabled = True
        if len(fields) > 5 and fields[5]:
            enabled = fields[5].lower() not in ("0", "false", "no", "off", "关闭", "否")
        servers.append(ServerConfig(
            type=stype, name=name, url=url, token=token,
            mappings=mappings, enabled=enabled,
        ))
    # 名称去重，避免多服务器结果互相覆盖
    seen: Dict[str, int] = {}
    for server in servers:
        seen[server.name] = seen.get(server.name, 0) + 1
        if seen[server.name] > 1:
            server.name = f"{server.name}-{seen[server.name]}"
    return servers, errors


def parent_dir(path: Any) -> str:
    normalized = normalize_path(path)
    return posixpath.dirname(normalized) or "/"


def file_extension(path: Any) -> str:
    normalized = normalize_path(path)
    name = posixpath.basename(normalized)
    index = name.rfind(".")
    return name[index:].lower() if index > 0 else ""


def parse_extensions(text: Any, default: Sequence[str] = DEFAULT_MEDIA_EXTS) -> Tuple[str, ...]:
    """解析用户填写的扩展名白名单；留空则用默认值。"""
    raw = str(text or "").strip()
    if not raw:
        return tuple(default)
    values = [part.strip().lower() for part in re.split(r"[\s,;]+", raw) if part.strip()]
    values = [v if v.startswith(".") else "." + v for v in values]
    return tuple(dict.fromkeys(values)) or tuple(default)


def is_media_file(path: Any, extensions: Sequence[str]) -> bool:
    return file_extension(path) in set(extensions)


# ---------------------------------------------------------------------------
# 事件路径提取与刷新项
# ---------------------------------------------------------------------------

def _as_path(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return normalize_path(value.get("path") or value.get("file_path") or "")
    attr = getattr(value, "path", None)
    if attr is not None:
        return normalize_path(attr)
    return normalize_path(value)


def _collect(obj: Any, keys: Sequence[str]) -> List[str]:
    """从对象或 dict 里按候选键收集路径，兼容 Path / pydantic 模型 / dict。"""
    found: List[str] = []
    for key in keys:
        value = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            found.extend(_as_path(item) for item in value)
        else:
            found.append(_as_path(value))
    return [p for p in found if p]


def extract_event_paths(event_data: Any) -> List[str]:
    """从 TransferComplete / SubtitleTransferComplete / 删除事件里提取目标路径。

    兼容宿主对象与 dict；优先取 transferinfo.file_list_new，其次 target_item，
    再退回事件本身的常见路径字段。返回值已归一化但未做扩展名过滤。
    """
    if not isinstance(event_data, dict):
        return []
    paths: List[str] = []
    transfer = event_data.get("transferinfo") or event_data.get("transfer_info")
    if transfer is not None:
        paths.extend(_collect(transfer, ("file_list_new", "fileListNew", "file_list", "target_paths")))
        paths.extend(_collect(transfer, ("target_item", "target_path", "target_dir")))
    paths.extend(_collect(event_data, (
        "file_list_new", "file_list", "target_item", "target_path",
        "path", "file_path", "filepath", "src", "source_path", "src_path",
    )))
    result: List[str] = []
    for path in paths:
        normalized = normalize_path(path)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def filter_paths(paths: Iterable[str], extensions: Sequence[str],
                 exclude_keywords: Sequence[str] = (),
                 allow_dirs: bool = True) -> List[str]:
    """过滤出媒体文件；无扩展名的目录路径按 allow_dirs 决定保留与否。"""
    allowed = set(extensions)
    keywords = [kw.strip().lower() for kw in exclude_keywords if kw and kw.strip()]
    result: List[str] = []
    for raw in paths:
        path = normalize_path(raw)
        if not path:
            continue
        lower = path.lower()
        if any(kw in lower for kw in keywords):
            continue
        ext = file_extension(path)
        if ext and ext not in allowed:
            continue
        if not ext and not allow_dirs:
            continue
        result.append(path)
    return result


def make_items(paths: Iterable[str], update_type: str = CREATED,
               source: str = "event", now: Optional[float] = None) -> List[RefreshItem]:
    stamp = time.time() if now is None else float(now)
    kind = update_type if update_type in UPDATE_TYPES else CREATED
    return [RefreshItem(path=normalize_path(p), update_type=kind, ts=stamp, source=source)
            for p in paths if normalize_path(p)]


def dedupe_items(items: Iterable[RefreshItem]) -> List[RefreshItem]:
    """按 (大小写不敏感路径, 更新类型) 去重，保留最早入队时间与最大重试次数。"""
    merged: Dict[Tuple[str, str], RefreshItem] = {}
    for item in items:
        if not isinstance(item, RefreshItem):
            continue
        key = (normalize_path(item.path).lower(), item.update_type)
        current = merged.get(key)
        if current is None:
            merged[key] = RefreshItem(
                path=normalize_path(item.path), update_type=item.update_type,
                attempts=item.attempts, ts=item.ts, source=item.source,
            )
            continue
        current.attempts = max(current.attempts, item.attempts)
        current.ts = min(current.ts or time.time(), item.ts or time.time())
    return sorted(merged.values(), key=lambda x: (x.ts, x.path))


def collapse_parents(items: Iterable[RefreshItem], server: Optional[ServerConfig] = None) -> List[str]:
    """按父目录合并回退目标，返回媒体服务器侧路径。"""
    dirs = set()
    for item in items:
        path = server.map_path(item.path) if server else normalize_path(item.path)
        dirs.add(parent_dir(path))
    return sorted(dirs)


def split_batches(items: Sequence[Any], size: int) -> List[List[Any]]:
    step = max(int(size or 1), 1)
    return [list(items[i:i + step]) for i in range(0, len(items), step)]


# ---------------------------------------------------------------------------
# 调度判定
# ---------------------------------------------------------------------------

def should_force_dispatch(count: int, first_ts: float, now: float,
                          max_wait: float, batch_size: int) -> Tuple[bool, str]:
    """事件到达时判断是否需要立即执行（不再等待合并窗口）。"""
    if count <= 0:
        return False, "队列为空"
    if batch_size > 0 and count >= batch_size:
        return True, "达到批量上限"
    if max_wait > 0 and (now - first_ts) >= max_wait:
        return True, "超过最大等待"
    return False, "等待合并窗口"


def debounce_delay(first_ts: float, now: float, window: float, max_wait: float) -> int:
    """一次性任务的延迟秒数，保证不突破最大等待时间。"""
    remain = max_wait - (now - first_ts) if max_wait > 0 else window
    return max(1, int(min(max(window, 1.0), max(remain, 1.0))))


def retry_delay(attempt: int, base: float = 5.0, cap: float = 300.0) -> float:
    """指数退避，attempt 从 0 开始；返回秒数。"""
    return min(max(float(base), 0.0) * (2 ** max(int(attempt), 0)), max(float(cap), 1.0))


def next_attempt(item: RefreshItem, max_retries: int, now: Optional[float] = None) -> Optional[RefreshItem]:
    """失败后生成下一轮待重试项；超过上限返回 None。"""
    if item.attempts + 1 >= max(1, int(max_retries)):
        return None
    return RefreshItem(
        path=item.path, update_type=item.update_type,
        attempts=item.attempts + 1,
        ts=time.time() if now is None else float(now),
        source=item.source,
    )


def validate_cron(expression: Any) -> Tuple[bool, str]:
    """校验 5 段 crontab；允许 `?` 作为 `*`，返回 (是否有效, 规范化表达式)。"""
    text = str(expression or "").strip().replace("?", "*")
    fields = text.split()
    if len(fields) != 5:
        return False, ""
    for chunk, (low, high) in zip(fields, _CRON_FIELD_RANGES):
        if not _CRON_TOKEN.match(chunk):
            return False, ""
        for part in chunk.split(","):
            if part == "*":
                continue
            if part.startswith("*/"):
                if int(part[2:]) < 1:
                    return False, ""
                continue
            if "-" in part:
                start, end = (int(x) for x in part.split("-", 1))
            else:
                start = end = int(part)
            if start < low or end > high or start > end:
                return False, ""
    return True, text


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

def mask_secrets(text: Any, secrets: Sequence[str] = ()) -> str:
    """脱敏 API Key / Token、URL 内嵌凭据与 query 中的密钥参数。"""
    result = str(text or "")
    for secret in secrets:
        value = str(secret or "").strip()
        if len(value) >= 4:
            result = result.replace(value, "***")
    result = _URL_CREDENTIAL.sub(r"\1***@", result)
    return _QUERY_SECRET.sub(r"\1***", result)


def format_items(items: Sequence[RefreshItem], limit: int = 12,
                 server: Optional[ServerConfig] = None) -> str:
    """把刷新项压缩成可读的一行摘要。"""
    if not items:
        return "-"
    lines = []
    for item in list(items)[:max(int(limit), 1)]:
        shown = server.map_path(item.path) if server else item.path
        lines.append(f"{item.update_type} {shown}")
    if len(items) > limit:
        lines.append(f"... 另有 {len(items) - limit} 项")
    return "\n".join(lines)


def now_text(ts: Optional[float] = None) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d %H:%M:%S")
