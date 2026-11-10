"""播放感知调度 —— 零宿主依赖的纯逻辑层。

所有可被单测覆盖的判定、解析、换算与历史管理都放在这里，
避免与 MoviePilot 宿主耦合，方便脱离 MP 运行环境做单元测试。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 播放状态枚举（内部用，便于日志与判定）
STATE_IDLE = "idle"
STATE_DIRECTPLAY = "directplay"
STATE_TRANSCODE = "transcode"
STATE_REMOTE_TRANSCODE = "remote_transcode"

# 默认各状态限速（Mbps），0 表示不限速
DEFAULT_LIMITS: Dict[str, Tuple[float, float]] = {
    STATE_IDLE: (0.0, 0.0),
    STATE_DIRECTPLAY: (0.0, 50.0),
    STATE_TRANSCODE: (0.0, 30.0),
    STATE_REMOTE_TRANSCODE: (0.0, 15.0),
}

_STATE_LABELS = {
    STATE_IDLE: "空闲",
    STATE_DIRECTPLAY: "原画播放",
    STATE_TRANSCODE: "转码播放",
    STATE_REMOTE_TRANSCODE: "远程转码",
}


def _ip_is_private(host: str) -> bool:
    """粗略判断一个地址是否落在私有/保留网段（用于判定是否为内网播放）。"""
    host = str(host or "").strip()
    if not host:
        return False
    if host in ("::1", "localhost"):
        return True
    # 去掉 IPv6 方括号与端口
    host = host.strip("[]").split(":")[0]
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        # 非 IP（如域名），按内网对待，避免误判为远程
        return True


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """统一路径分隔符并去掉多余斜杠。"""
    if not path:
        return ""
    text = str(path).replace("\\", "/").strip()
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/")


def parse_mappings(text: str) -> List[Tuple[str, str]]:
    """解析路径映射，格式：服务端路径::下载器路径，分号或换行分隔，# 开头为注释。"""
    result: List[Tuple[str, str]] = []
    for raw in re.split(r"[\n;]", text or ""):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "::" not in line:
            continue
        src, dst = line.split("::", 1)
        src, dst = normalize_path(src.strip()), normalize_path(dst.strip())
        if src:
            result.append((src, dst))
    return result


def apply_path_mapping(path: str, mappings: List[Tuple[str, str]]) -> str:
    """将服务端路径按映射转换为下载器路径，最长前缀优先。"""
    if not path or not mappings:
        return path
    norm = normalize_path(path)
    best: Optional[Tuple[str, str]] = None
    for src, dst in mappings:
        if norm == src or norm.startswith(src + "/"):
            if best is None or len(src) > len(best[0]):
                best = (src, dst)
    if best is None:
        return norm
    src, dst = best
    if norm == src:
        return dst or norm
    return dst + norm[len(src):]


@dataclass
class MediaServer:
    type: str
    name: str
    url: str
    apikey: str = ""
    enabled: bool = True
    mappings: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def is_plex(self) -> bool:
        return self.type == "plex"

    @property
    def is_jellyfin(self) -> bool:
        return self.type == "jellyfin"

    def map_path(self, path: str) -> str:
        return apply_path_mapping(path, self.mappings)


def parse_servers(text: str) -> Tuple[List[MediaServer], List[str]]:
    """解析媒体服务器清单。
    每行：类型|名称|地址|密钥|路径映射|启用
    类型：emby / jellyfin / plex
    """
    servers: List[MediaServer] = []
    errors: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("|") < 3:
            errors.append(f"媒体服务器配置格式错误（至少 4 列）：{line}")
            continue
        parts = [p.strip() for p in line.split("|")]
        stype, name, url, apikey = parts[0], parts[1], parts[2], parts[3]
        mapping_text = parts[4] if len(parts) > 4 else ""
        enabled = (parts[5] if len(parts) > 5 else "1") not in ("0", "no", "false", "关", "否")
        stype = stype.lower()
        if stype not in ("emby", "jellyfin", "plex"):
            errors.append(f"未知媒体服务器类型：{stype}")
            continue
        if not url or not apikey:
            errors.append(f"媒体服务器 {name} 缺少地址或密钥")
            continue
        servers.append(MediaServer(
            type=stype, name=name, url=url, apikey=apikey,
            enabled=enabled, mappings=parse_mappings(mapping_text),
        ))
    return servers, errors


@dataclass
class Downloader:
    type: str
    name: str
    url: str
    username: str = ""
    password: str = ""
    enabled: bool = True

    @property
    def is_qbittorrent(self) -> bool:
        return self.type == "qbittorrent"

    @property
    def is_transmission(self) -> bool:
        return self.type == "transmission"


def parse_downloaders(text: str) -> Tuple[List[Downloader], List[str]]:
    """解析下载器清单。
    每行：类型|名称|地址|用户名|密码|启用
    类型：qbittorrent / transmission
    """
    downloaders: List[Downloader] = []
    errors: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("|") < 3:
            errors.append(f"下载器配置格式错误（至少 4 列）：{line}")
            continue
        parts = [p.strip() for p in line.split("|")]
        dtype, name, url = parts[0], parts[1], parts[2]
        username = parts[3] if len(parts) > 3 else ""
        password = parts[4] if len(parts) > 4 else ""
        enabled = (parts[5] if len(parts) > 5 else "1") not in ("0", "no", "false", "关", "否")
        dtype = dtype.lower()
        if dtype not in ("qbittorrent", "transmission"):
            errors.append(f"未知下载器类型：{dtype}")
            continue
        downloaders.append(Downloader(
            type=dtype, name=name, url=url,
            username=username, password=password, enabled=enabled,
        ))
    return downloaders, errors


def parse_limits(text: str) -> Dict[str, Tuple[float, float]]:
    """解析各状态限速（Mbps），格式：状态:下载,上传，每行一个。"""
    limits = dict(DEFAULT_LIMITS)
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        state, rest = line.split(":", 1)
        state = state.strip().lower()
        if state not in DEFAULT_LIMITS:
            continue
        m = re.match(r"\s*([\d.]+)\s*[,，]\s*([\d.]+)\s*", rest)
        if not m:
            continue
        limits[state] = (float(m.group(1)), float(m.group(2)))
    return limits


# ---------------------------------------------------------------------------
# 会话归一化与状态判定
# ---------------------------------------------------------------------------

@dataclass
class Session:
    server: str
    user: str = ""
    client: str = ""
    device: str = ""
    title: str = ""
    sub_title: str = ""
    is_paused: bool = False
    is_transcoding: bool = False
    is_remote: bool = False
    progress: float = 0.0
    paths: List[str] = field(default_factory=list)

    @property
    def is_playing(self) -> bool:
        return bool(self.title) and not self.is_paused


def classify_state(session: Session) -> str:
    """单会话归类到内部状态。"""
    if not session.is_playing:
        return STATE_IDLE
    if session.is_remote and session.is_transcoding:
        return STATE_REMOTE_TRANSCODE
    if session.is_transcoding:
        return STATE_TRANSCODE
    return STATE_DIRECTPLAY


def overall_state(sessions: List[Session]) -> str:
    """聚合所有会话，取最"重"的播放状态。"""
    weights = {STATE_IDLE: 0, STATE_DIRECTPLAY: 1, STATE_TRANSCODE: 2, STATE_REMOTE_TRANSCODE: 3}
    best = STATE_IDLE
    for s in sessions:
        st = classify_state(s)
        if weights.get(st, 0) > weights.get(best, 0):
            best = st
    return best


def state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state)


def compute_limits(state: str, limits: Dict[str, Tuple[float, float]]) -> Dict[str, Any]:
    """把 Mbps 限速换算为各下载器所需单位。"""
    dl_mbps, up_mbps = limits.get(state, (0.0, 0.0))
    return {
        "state": state,
        "dl_mbps": dl_mbps,
        "up_mbps": up_mbps,
        "qb_dl_bytes": int(dl_mbps * 125000) if dl_mbps > 0 else 0,
        "qb_up_bytes": int(up_mbps * 125000) if up_mbps > 0 else 0,
        "tr_dl_kbps": int(dl_mbps * 125) if dl_mbps > 0 else 0,
        "tr_up_kbps": int(up_mbps * 125) if up_mbps > 0 else 0,
    }


# ---------------------------------------------------------------------------
# 观看历史管理
# ---------------------------------------------------------------------------

@dataclass
class WatchedItem:
    key: str
    title: str
    server: str
    paths: List[str]
    last_watched: float
    added: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "server": self.server,
            "paths": list(self.paths),
            "last_watched": self.last_watched,
            "added": self.added,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WatchedItem":
        return cls(
            key=str(d.get("key", "")),
            title=str(d.get("title", "")),
            server=str(d.get("server", "")),
            paths=list(d.get("paths", [])),
            last_watched=float(d.get("last_watched", 0.0)),
            added=float(d.get("added", 0.0)),
        )


def update_watched_history(
    history: List[Dict[str, Any]],
    sessions: List[Session],
    now: float,
) -> List[Dict[str, Any]]:
    """把当前在播条目写入历史，刷新 last_watched；返回新历史。"""
    items = [WatchedItem.from_dict(d) for d in (history or [])]
    index = {it.key: it for it in items}
    for s in sessions:
        if not s.is_playing:
            continue
        key = f"{s.server}:{s.title}"
        if key in index:
            index[key].last_watched = now
            index[key].paths = list(s.paths)
        else:
            index[key] = WatchedItem(
                key=key, title=s.title, server=s.server,
                paths=list(s.paths), last_watched=now, added=now,
            )
    return [it.to_dict() for it in index.values()]


def prune_watched_history(
    history: List[Dict[str, Any]],
    now: float,
    retention_days: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """裁剪超过保留天数的历史，返回 (保留, 已过期)。"""
    if retention_days <= 0:
        return list(history), []
    cutoff = now - retention_days * 86400.0
    keep, expired = [], []
    for d in (history or []):
        if float(d.get("last_watched", 0.0)) < cutoff:
            expired.append(d)
        else:
            keep.append(d)
    return keep, expired


def eligible_for_cleanup(
    history: List[Dict[str, Any]],
    now: float,
    retention_days: int,
) -> List[Dict[str, Any]]:
    """返回已达到"观看后保留天数"的条目（供自动删种候选）。"""
    if retention_days <= 0:
        return []
    cutoff = now - retention_days * 86400.0
    return [d for d in (history or []) if float(d.get("last_watched", 0.0)) < cutoff]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def validate_cron(cron: str) -> Tuple[bool, str]:
    """校验 5 段 crontab 表达式。"""
    text = (cron or "").strip().replace("?", "*")
    fields = text.split()
    if len(fields) != 5:
        return False, ""
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for chunk, (low, high) in zip(fields, ranges):
        if not re.match(r"^(\*|(\*/\d+)|(\d+(-\d+)?)(/\d+)?(,\d+(-\d+)?(/\d+)?)*)$", chunk):
            return False, ""
        for part in chunk.split(","):
            if part == "*":
                continue
            if part.startswith("*/"):
                step = int(part[2:])
                if step < 1:
                    return False, ""
                continue
            if "-" in part:
                start, end = (int(x) for x in part.split("-", 1))
            else:
                start = end = int(part)
            if start < low or end > high or start > end:
                return False, ""
    return True, text


def mask_secrets(text: str, secrets: Optional[List[str]] = None) -> str:
    """对日志/通知中的密钥与凭据做脱敏。"""
    result = str(text)
    if secrets:
        for secret in secrets:
            if secret and len(secret) >= 4:
                result = result.replace(secret, secret[:2] + "***" + secret[-1:])
    # 通用：URL 内嵌 user:pass、api_key、token
    result = re.sub(r"(https?://)[^@/\s]+:[^@/\s]+@", r"\1***:***@", result)
    result = re.sub(r"(api_key=|apikey=|token=|X-Plex-Token=)([A-Za-z0-9_\-]{6,})",
                    r"\1***", result, flags=re.IGNORECASE)
    return result


def format_sessions(sessions: List[Session]) -> str:
    """把会话列表格式化为单行概览（用于日志/通知）。"""
    if not sessions:
        return "无活动会话"
    playing = [s for s in sessions if s.is_playing]
    if not playing:
        return "无播放中会话"
    parts = []
    for s in playing:
        st = classify_state(s)
        remote = "远程" if s.is_remote else "内网"
        tc = "转码" if s.is_transcoding else "原画"
        parts.append(f"{s.user}@{s.server}({remote}/{tc}/{int(s.progress*100)}%)")
    return "；".join(parts)


def split_batches(items: List[Any], size: int) -> List[List[Any]]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]
