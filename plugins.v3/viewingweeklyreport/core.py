"""观影周报 / 转码诊断 —— 零宿主依赖的纯逻辑层。

所有可被单测覆盖的解析、时间窗、周报聚合、转码诊断规则与文本格式化都放这里，
避免与 MoviePilot 宿主耦合，方便脱离 MP 运行环境做单元测试。

核心概念：
- PlayRecord：一次播放记录（条目、用户、时长、是否转码/远程、编解码信息、转码原因）。
- 周报（report）：在给定时间窗内对记录做多维聚合（总量 / 用户 / 服务器 / 每日 / 类型 / 榜单）。
- 转码诊断（diagnose）：按规则引擎扫描转码记录，给出可执行的优化建议（分级）。
- 详情页所有动态文本统一经 md_safe 转义，避免半角方括号被当 Markdown 参考式链接
  渲染成"空药丸"（用户截图红框问题的根因）。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 媒体服务器配置解析（与 playbackscheduler 一致的通用约定）
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
    """解析路径映射，格式：服务端路径::本地路径，分号或换行分隔，# 开头为注释。"""
    result: List[Tuple[str, str]] = []
    for raw in re.split(r"[\n;]", text or ""):
        line = raw.strip()
        if not line or line.startswith("#") or "::" not in line:
            continue
        src, dst = line.split("::", 1)
        src, dst = normalize_path(src.strip()), normalize_path(dst.strip())
        if src:
            result.append((src, dst))
    return result


def apply_path_mapping(path: str, mappings: List[Tuple[str, str]]) -> str:
    """将服务端路径按映射转换为本地路径，最长前缀优先。"""
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
    return dst if norm == src else dst + norm[len(src):]


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
    """解析媒体服务器清单。每行：类型|名称|地址|密钥|路径映射|启用。"""
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


def _ip_is_private(host: str) -> bool:
    """粗略判断地址是否为私有/保留网段（用于判定内网 / 远程播放）。"""
    host = str(host or "").strip()
    if not host:
        return False
    if host in ("::1", "localhost"):
        return True
    host = host.strip("[]").split(":")[0]
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        # 非 IP（域名）按内网对待，避免误判为远程
        return True


# ---------------------------------------------------------------------------
# 播放记录
# ---------------------------------------------------------------------------

MEDIA_MOVIE = "movie"
MEDIA_TV = "tv"

# 转码原因代码 → 中文标签
REASON_LABELS: Dict[str, str] = {
    "hevc": "HEVC/H.265 视频需转码",
    "audio": "音频编码不兼容（TrueHD/DTS/E-AC3 等）",
    "subtitle": "字幕烧录触发转码",
    "remote": "远程播放触发转码",
    "cpu": "软件转码（未启用硬件加速）",
    "4k": "4K/HDR 高负载转码",
    "bitrate": "高码率转码",
    "other": "其它原因",
}


@dataclass
class PlayRecord:
    key: str = ""
    server: str = ""
    user: str = ""
    title: str = ""
    sub_title: str = ""
    media_type: str = MEDIA_MOVIE
    started: float = 0.0
    duration_sec: float = 0.0
    is_transcode: bool = False
    is_remote: bool = False
    client: str = ""
    device: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    resolution: str = ""
    bitrate_kbps: int = 0
    hwaccel: bool = False
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "server": self.server, "user": self.user,
            "title": self.title, "sub_title": self.sub_title, "media_type": self.media_type,
            "started": self.started, "duration_sec": self.duration_sec,
            "is_transcode": self.is_transcode, "is_remote": self.is_remote,
            "client": self.client, "device": self.device,
            "video_codec": self.video_codec, "audio_codec": self.audio_codec,
            "resolution": self.resolution, "bitrate_kbps": self.bitrate_kbps,
            "hwaccel": self.hwaccel, "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PlayRecord":
        return cls(
            key=str(d.get("key", "")), server=str(d.get("server", "")),
            user=str(d.get("user", "")), title=str(d.get("title", "")),
            sub_title=str(d.get("sub_title", "")),
            media_type=str(d.get("media_type", MEDIA_MOVIE)),
            started=float(d.get("started", 0.0)),
            duration_sec=float(d.get("duration_sec", 0.0)),
            is_transcode=bool(d.get("is_transcode", False)),
            is_remote=bool(d.get("is_remote", False)),
            client=str(d.get("client", "")), device=str(d.get("device", "")),
            video_codec=str(d.get("video_codec", "")),
            audio_codec=str(d.get("audio_codec", "")),
            resolution=str(d.get("resolution", "")),
            bitrate_kbps=int(d.get("bitrate_kbps", 0) or 0),
            hwaccel=bool(d.get("hwaccel", False)),
            reasons=[str(x) for x in (d.get("reasons") or [])],
        )

    @property
    def display_title(self) -> str:
        return f"{self.title}{' · ' + self.sub_title if self.sub_title else ''}"

    @property
    def media_type_label(self) -> str:
        return "电影" if self.media_type == MEDIA_MOVIE else "剧集"


# ---------------------------------------------------------------------------
# 转码原因推断（供客户端层在构造记录时调用）
# ---------------------------------------------------------------------------

_CODEC_HEVC = {"hevc", "h265", "h.265", "x265", "hev1", "hvc1"}
_CODEC_PROBLEM_AUDIO = {"truehd", "dts", "dts-hd", "dtsma", "eac3", "e-ac3", "ac3", "flac", "alac", "pcm"}


def _norm_codec(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def infer_transcode_reasons(
    video_codec: Any = "",
    audio_codec: Any = "",
    resolution: Any = "",
    is_remote: bool = False,
    subtitle_burn: bool = False,
    hwaccel: bool = False,
    bitrate_kbps: int = 0,
    is_transcode: bool = True,
) -> List[str]:
    """根据编解码与播放环境推断转码原因代码列表。"""
    if not is_transcode:
        return []
    reasons: List[str] = []
    vc = _norm_codec(video_codec)
    ac = _norm_codec(audio_codec)
    res = str(resolution or "").lower().replace(" ", "")
    if vc in _CODEC_HEVC or "h265" in vc or "hevc" in vc:
        reasons.append("hevc")
    if ac in _CODEC_PROBLEM_AUDIO or any(x in ac for x in ("truehd", "dts", "eac3", "e-ac3")):
        reasons.append("audio")
    if subtitle_burn:
        reasons.append("subtitle")
    if is_remote:
        reasons.append("remote")
    if any(token in res for token in ("2160", "4k", "3840")):
        reasons.append("4k")
    if not hwaccel:
        reasons.append("cpu")
    if bitrate_kbps and bitrate_kbps >= 20000:
        reasons.append("bitrate")
    if not reasons:
        reasons.append("other")
    # 去重保序
    seen: set = set()
    out: List[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# 时间窗
# ---------------------------------------------------------------------------

WEEK_THIS = "this_week"
WEEK_LAST = "last_week"
WEEK_LAST7 = "last_7_days"
WEEK_MODES = (WEEK_THIS, WEEK_LAST, WEEK_LAST7)
WEEK_MODE_LABELS = {
    WEEK_THIS: "本周（周一至今）",
    WEEK_LAST: "上周（周一至周日）",
    WEEK_LAST7: "最近 7 天",
}


def week_range(mode: str = WEEK_THIS, now: Optional[float] = None,
               week_start: int = 0) -> Tuple[float, float, str]:
    """计算统计时间窗 (start_ts, end_ts, label)。

    week_start: 0=周一为一周起点（ISO），6=周日为起点。
    - this_week: 本周一 00:00:00 到 now
    - last_week: 上周一 00:00:00 到上周日 23:59:59
    - last_7_days: now-7*86400 到 now
    """
    now = float(now) if now is not None else datetime.now().timestamp()
    end_dt = datetime.fromtimestamp(now)
    # 归一化到当天 00:00
    midnight = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    weekday = midnight.weekday()  # 0=周一 ... 6=周日
    # 距本周起点的天数
    days_since_start = (weekday - int(week_start)) % 7

    if mode == WEEK_LAST7:
        start = now - 7 * 86400.0
        return start, now, WEEK_MODE_LABELS[WEEK_LAST7]
    if mode == WEEK_LAST:
        this_start = midnight - timedelta(days=days_since_start)
        last_start = this_start - timedelta(days=7)
        last_end = this_start - timedelta(seconds=1)
        return last_start.timestamp(), last_end.timestamp(), WEEK_MODE_LABELS[WEEK_LAST]
    # this_week（默认）
    start_dt = midnight - timedelta(days=days_since_start)
    return start_dt.timestamp(), now, WEEK_MODE_LABELS[WEEK_THIS]


def in_range(record: PlayRecord, start: float, end: float) -> bool:
    """记录是否落在统计窗内（以开始时间计，兼容历史导入的 0 时间记录）。"""
    ts = float(record.started or 0.0)
    if ts <= 0:
        return False
    return start <= ts <= end


def _day_key(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return "未知日期"


# ---------------------------------------------------------------------------
# 周报聚合
# ---------------------------------------------------------------------------

def build_weekly_report(records: Sequence[PlayRecord], start: float, end: float,
                        top_n: int = 10) -> Dict[str, Any]:
    """在时间窗内聚合播放记录，返回结构化周报。"""
    selected = [r for r in records if in_range(r, start, end)]

    total_plays = len(selected)
    total_seconds = round(sum(r.duration_sec for r in selected), 1)
    transcode_plays = sum(1 for r in selected if r.is_transcode)
    remote_plays = sum(1 for r in selected if r.is_remote)
    movie_plays = sum(1 for r in selected if r.media_type == MEDIA_MOVIE)
    tv_plays = sum(1 for r in selected if r.media_type == MEDIA_TV)

    def _bucket() -> Dict[str, Dict[str, float]]:
        return {}

    def _add(bucket: Dict[str, Dict[str, float]], key: str, rec: PlayRecord) -> None:
        item = bucket.setdefault(key or "未知", {"plays": 0, "seconds": 0.0, "transcodes": 0})
        item["plays"] += 1
        item["seconds"] += rec.duration_sec
        item["transcodes"] += 1 if rec.is_transcode else 0

    per_user: Dict[str, Dict[str, float]] = _bucket()
    per_server: Dict[str, Dict[str, float]] = _bucket()
    per_day: Dict[str, Dict[str, float]] = _bucket()
    # 按"服务器 + 片名 + 副标题"聚合，而非记录 key（key 含时间戳，
    # 同一影片不同时间播放会被误当成两部）。这样"影片 N 部"才是去重后的部数，
    # 多次播放也能正确累加到同一条目。
    titles: Dict[str, Dict[str, Any]] = {}
    for r in selected:
        _add(per_user, r.user, r)
        _add(per_server, r.server, r)
        _add(per_day, _day_key(r.started), r)
        ckey = f"{r.server}|{r.title}|{r.sub_title}"
        item = titles.setdefault(ckey, {
            "title": r.display_title, "server": r.server, "media_type": r.media_type,
            "plays": 0, "seconds": 0.0, "transcodes": 0,
        })
        item["plays"] += 1
        item["seconds"] += r.duration_sec
        item["transcodes"] += 1 if r.is_transcode else 0

    def _finalize(bucket: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        out = []
        for key, v in bucket.items():
            out.append({
                "name": key, "plays": int(v["plays"]),
                "seconds": round(v["seconds"], 1), "transcodes": int(v["transcodes"]),
            })
        out.sort(key=lambda x: (x["seconds"], x["plays"]), reverse=True)
        return out

    top_titles = sorted(
        titles.values(),
        key=lambda x: (x["seconds"], x["plays"]), reverse=True,
    )[:max(1, top_n)]
    for t in top_titles:
        t["seconds"] = round(t["seconds"], 1)

    per_day_list = sorted(per_day.items(), key=lambda kv: kv[0])
    per_day_out = [
        {"day": day, "plays": int(v["plays"]), "seconds": round(v["seconds"], 1)}
        for day, v in per_day_list
    ]

    transcode_ratio = round(100.0 * transcode_plays / total_plays, 1) if total_plays else 0.0
    return {
        "start": start, "end": end,
        "total_plays": total_plays,
        "total_seconds": total_seconds,
        "unique_titles": len(titles),  # 已按内容去重（同片多次播放只算一部）
        "unique_users": len([k for k in per_user if k and k != "未知"]),
        "server_count": len([k for k in per_server if k and k != "未知"]),
        "movie_plays": movie_plays, "tv_plays": tv_plays,
        "transcode_plays": transcode_plays, "remote_plays": remote_plays,
        "transcode_ratio": transcode_ratio,
        "per_user": _finalize(per_user),
        "per_server": _finalize(per_server),
        "per_day": per_day_out,
        "top_titles": top_titles,
        "record_count_in_window": total_plays,
    }


def health_level(report: Dict[str, Any]) -> Tuple[str, str]:
    """根据转码占比给出健康等级 (等级, 说明)。"""
    ratio = float(report.get("transcode_ratio", 0.0) or 0.0)
    if ratio == 0:
        return "优", "全部直接播放，零转码"
    if ratio < 15:
        return "良", "转码占比较低"
    if ratio < 35:
        return "中", "转码偏多，建议优化客户端与硬件加速"
    return "差", "转码占比过高，CPU 压力大，建议尽快优化"


# ---------------------------------------------------------------------------
# 转码诊断规则引擎
# ---------------------------------------------------------------------------

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITY_INFO = "info"
SEVERITY_ORDER = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2, SEVERITY_INFO: 3}
SEVERITY_LABELS = {
    SEVERITY_HIGH: "严重", SEVERITY_MEDIUM: "建议",
    SEVERITY_LOW: "提示", SEVERITY_INFO: "良好",
}


@dataclass
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    suggestion: str
    count: int = 0
    samples: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code, "severity": self.severity, "title": self.title,
            "detail": self.detail, "suggestion": self.suggestion,
            "count": self.count, "samples": list(self.samples),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Finding":
        return cls(
            code=str(d.get("code", "")), severity=str(d.get("severity", SEVERITY_LOW)),
            title=str(d.get("title", "")), detail=str(d.get("detail", "")),
            suggestion=str(d.get("suggestion", "")), count=int(d.get("count", 0) or 0),
            samples=[str(x) for x in (d.get("samples") or [])],
        )


def diagnose_transcodes(records: Sequence[PlayRecord],
                        high_bitrate_kbps: int = 20000) -> List[Finding]:
    """扫描转码记录，按规则产出分级诊断建议。"""
    transcodes = [r for r in records if r.is_transcode]
    findings: List[Finding] = []

    def collect(predicate, code, severity, title, detail, suggestion) -> None:
        hits = [r for r in transcodes if predicate(r)]
        if hits:
            samples = []
            for r in hits[:3]:
                samples.append(r.display_title)
            findings.append(Finding(
                code=code, severity=severity, title=title,
                detail=detail.format(count=len(hits)),
                suggestion=suggestion, count=len(hits), samples=samples,
            ))

    collect(lambda r: any(x == "hevc" for x in r.reasons) or "hevc" in _norm_codec(r.video_codec),
            "hevc", SEVERITY_MEDIUM, "HEVC/H.265 视频触发转码",
            "共 {count} 次播放因 HEVC/H.265 编码转码。",
            "确认客户端是否支持 HEVC 硬解；开启硬件加速（Intel QSV / NVIDIA NVENC / VAAPI），或预处理为 H.264 兼容版。")
    collect(lambda r: any(x == "audio" for x in r.reasons) or _norm_codec(r.audio_codec) in _CODEC_PROBLEM_AUDIO,
            "audio", SEVERITY_MEDIUM, "音频编码不兼容触发转码",
            "共 {count} 次播放因 TrueHD/DTS/E-AC3 等音频转码。",
            "在客户端开启音频直通（Pass-through），或在服务端/预处理阶段转 AAC；如无需次世代音轨可直接选用 AAC 版本。")
    collect(lambda r: any(x == "subtitle" for x in r.reasons),
            "subtitle", SEVERITY_LOW, "字幕烧录导致转码",
            "共 {count} 次播放因字幕烧录（ASS/PGS/特效字幕）转码。",
            "优先使用外挂 SRT/ASS 字幕并开启客户端软字幕；避免内嵌 PGS/杜比视界特效字幕。")
    collect(lambda r: r.is_remote,
            "remote", SEVERITY_MEDIUM, "远程播放触发服务端转码",
            "共 {count} 次为远程播放转码，占用上行与 CPU。",
            "远程场景限制码率（如 4~8 Mbps）、启用硬件加速；公网访问建议走反代 + HTTPS 并确保上行充足。")
    collect(lambda r: "4k" in r.reasons,
            "4k", SEVERITY_HIGH, "4K/HDR 高负载转码",
            "共 {count} 次 4K/HDR 转码，软件转码极易占满 CPU。",
            "务必启用显卡硬件加速；为远程/弱端准备 1080p 转码版本，避免实时 4K → 1080p 软转。")
    collect(lambda r: (not r.hwaccel) and r.is_transcode,
            "cpu", SEVERITY_HIGH, "软件转码（未启用硬件加速）",
            "共 {count} 次为纯 CPU 软件转码。",
            "在媒体服务器启用硬件加速：Intel 核显用 QSV、N 卡用 NVENC、Linux 用 VAAPI；可显著降低 CPU 占用与卡顿。")
    collect(lambda r: r.bitrate_kbps and r.bitrate_kbps >= high_bitrate_kbps,
            "bitrate", SEVERITY_LOW, "高码率流转码",
            f"共 {{count}} 次播放源码率超过 {high_bitrate_kbps} kbps。",
            "对远程客户端设置码率上限（如 8~20 Mbps），减轻服务端与网络压力。")

    if not transcodes:
        findings.append(Finding(
            code="healthy", severity=SEVERITY_INFO, title="无转码，播放健康",
            detail="统计窗内全部为直接播放。",
            suggestion="保持当前客户端与网络配置即可。", count=0,
        ))

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), -f.count))
    return findings


# ---------------------------------------------------------------------------
# 详情页文本安全（杜绝"空药丸"）
# ---------------------------------------------------------------------------

def md_safe(text: Any) -> str:
    """把动态文本转义为可在详情页 text 中原样展示的纯文本。

    MoviePilot 前端会把详情页组件 text 当 Markdown 渲染：成对的半角方括号
    `[x][y]` 会被解析成"参考式链接"而只剩空白占位（截图里的药丸）。
    统一替换为视觉等价的全角字符，保证所见即所得。
    """
    s = str(text if text is not None else "")
    table = str.maketrans({
        "[": "［", "]": "］",
        "`": "ˋ", "*": "＊",
        "<": "‹", ">": "›",
        "_": "＿",
    })
    return s.translate(table)


def format_log_line(time_text: str, level: str, message: str) -> str:
    """详情页单行日志：用全角 ｜ 分隔，杜绝 [time][level] 被当 Markdown 链接。"""
    return md_safe(f"{time_text} ｜ {level:<5} ｜ {message}")


def format_duration(seconds: float) -> str:
    """把秒数格式化为 X小时Y分 / Y分Z秒。"""
    sec = int(round(float(seconds or 0)))
    if sec <= 0:
        return "0 分钟"
    hours, rem = divmod(sec, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def format_report_text(report: Dict[str, Any]) -> str:
    """把周报结构化为多行中文文本（用于详情页与通知）。"""
    if not report or not report.get("total_plays"):
        return "本时间窗内暂无观影记录。可点击「生成周报」或先运行一次采集。"
    level, level_note = health_level(report)
    lines: List[str] = []
    lines.append(f"统计区间：{_day_key(report['start'])} 至 {_day_key(report['end'])}")
    lines.append(
        f"总播放 {report['total_plays']} 次 ｜ 观看时长 {format_duration(report['total_seconds'])} ｜ "
        f"影片 {report['unique_titles']} 部"
    )
    lines.append(
        f"电影 {report['movie_plays']} 次 ｜ 剧集 {report['tv_plays']} 次 ｜ "
        f"转码 {report['transcode_plays']} 次（{report['transcode_ratio']}%）｜ 远程 {report['remote_plays']} 次"
    )
    lines.append(f"播放健康度：{level}（{level_note}）")
    if report.get("per_day"):
        lines.append("每日趋势：")
        for d in report["per_day"]:
            lines.append(f"  【{d['day']}】 {d['plays']} 次 ｜ {format_duration(d['seconds'])}")
    if report.get("per_user"):
        lines.append("用户排行：")
        for idx, u in enumerate(report["per_user"][:8], 1):
            lines.append(f"  {idx}. {u['name'] or '未知用户'}：{u['plays']} 次 ｜ {format_duration(u['seconds'])}")
    if report.get("per_server"):
        lines.append("服务器分布：")
        for s in report["per_server"]:
            lines.append(f"  【{s['name']}】 {s['plays']} 次 ｜ {format_duration(s['seconds'])}")
    if report.get("top_titles"):
        lines.append("热门影片 TOP：")
        for idx, t in enumerate(report["top_titles"], 1):
            tc = f" ｜ 转码 {t['transcodes']} 次" if t["transcodes"] else ""
            lines.append(f"  {idx}. {t['title']}（{t['media_type'] == MEDIA_MOVIE and '电影' or '剧集'}）："
                         f"{t['plays']} 次 ｜ {format_duration(t['seconds'])}{tc}")
    # 片名/用户名来自外部媒体服务器，可能含 [] * _ 等；就地全角化，
    # 使本函数无论被详情页还是通知/API 使用都是安全的纯文本。
    return md_safe("\n".join(lines))


def format_findings_text(findings: Sequence[Finding]) -> str:
    """把诊断建议结构化为多行中文文本。"""
    if not findings:
        return "暂无诊断结果，点击「转码诊断」开始分析。"
    lines: List[str] = []
    for f in findings:
        lines.append(f"【{SEVERITY_LABELS.get(f.severity, '提示')}】 {f.title}")
        lines.append(f"  现象：{f.detail}")
        lines.append(f"  建议：{f.suggestion}")
        if f.samples:
            lines.append("  涉及：" + "、".join(f.samples))
    return md_safe("\n".join(lines))


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def validate_cron(cron: str) -> Tuple[bool, str]:
    """校验 5 段 crontab 表达式；第二个返回值为规范化后的表达式。"""
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


def mask_secrets(text: str, secrets: Optional[Sequence[str]] = None) -> str:
    """对日志/通知中的密钥与凭据做脱敏。"""
    result = str(text)
    if secrets:
        for secret in secrets:
            if secret and len(secret) >= 4:
                result = result.replace(secret, secret[:2] + "***" + secret[-1:])
    result = re.sub(r"(https?://)[^@/\s]+:[^@/\s]+@", r"\1***:***@", result)
    result = re.sub(r"(api_key=|apikey=|token=|X-Plex-Token=)([A-Za-z0-9_\-]{6,})",
                    r"\1***", result, flags=re.IGNORECASE)
    return result


def merge_records(existing: Sequence[Dict[str, Any]],
                  incoming: Sequence[PlayRecord],
                  max_records: int = 2000) -> List[Dict[str, Any]]:
    """按 key 合并播放记录（新记录覆盖同 key 旧记录），按 started 倒序裁剪。"""
    index: Dict[str, Dict[str, Any]] = {}
    for d in existing or []:
        try:
            rec = PlayRecord.from_dict(d)
        except Exception:  # noqa: BLE001
            continue
        if rec.key:
            index[rec.key] = rec.to_dict()
    for rec in incoming:
        if rec.key:
            index[rec.key] = rec.to_dict()
    ordered = sorted(index.values(), key=lambda d: float(d.get("started", 0.0)), reverse=True)
    return ordered[:max(1, int(max_records or 2000))]


def prune_records(records: Sequence[Dict[str, Any]], now: float,
                  retention_days: int) -> Tuple[List[Dict[str, Any]], int]:
    """裁剪超过保留天数的记录，返回 (保留, 淘汰数量)。retention_days<=0 表示永久保留。"""
    if retention_days <= 0:
        return list(records), 0
    cutoff = float(now) - retention_days * 86400.0
    keep, removed = [], 0
    for d in records or []:
        if float(d.get("started", 0.0)) < cutoff:
            removed += 1
        else:
            keep.append(d)
    return keep, removed
