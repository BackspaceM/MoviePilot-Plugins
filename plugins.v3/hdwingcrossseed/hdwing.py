"""
HDWinG（NexusPHP）站点交互与种子解析工具。

本模块刻意不依赖 MoviePilot 宿主，只依赖标准库，便于脱离宿主单元测试：
- HTTP 由调用方注入 fetcher，便于复用宿主的请求封装（代理、证书、UA 策略）
- 种子只做只读解析，info 段保留原始字节计算 info_hash，避免重编码改变哈希
"""

from __future__ import annotations

import hashlib
import html as _html
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_SITE_URL = "https://hdwing.org"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 发布名中代表规格的词，遇到即认为片名结束
_TECH_TOKENS: Set[str] = {
    "2160p", "1080p", "1080i", "720p", "576p", "480p", "4k", "8k", "uhd",
    "bluray", "blu-ray", "bd", "bdrip", "brrip", "bd25", "bd50", "remux",
    "web", "webdl", "web-dl", "webrip", "hdtv", "hdtvrip", "dvdrip", "hdrip",
    "dvd", "dvd5", "dvd9", "iso", "m2ts", "mkv", "mp4", "avi", "ts",
    "x264", "x265", "h264", "h265", "h.264", "h.265", "hevc", "avc", "av1",
    "10bit", "8bit", "hdr", "hdr10", "hdr10+", "dovi", "dolby", "vision",
    "atmos", "truehd", "ddp", "dd+", "dd", "dts", "dtshd", "dts-hd", "ma",
    "flac", "aac", "ac3", "eac3", "lpcm", "pcm", "mp3", "opus",
    "proper", "repack", "internal", "complete", "limited", "unrated",
    "extended", "remastered", "criterion", "sub", "subs", "chs", "cht",
    "简", "繁", "中字", "字幕", "国语", "粤语", "双语",
}

# 站点列表页每页条数上限（用于翻页判断）
_PIN_MARK = '<td class="t_pin"'

_VIDEO_EXT = re.compile(r"\.(?:mkv|mp4|avi|ts|m2ts|iso|rmvb|wmv|mov|flv)$", re.I)
_GROUP_SUFFIX = re.compile(r"-[A-Za-z0-9@._]{2,16}$")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_EPISODE = re.compile(r"s(\d{1,2})[ ._-]?e(\d{1,3})", re.I)
_EPISODE_CN = re.compile(r"第\s*(\d{1,4})\s*[集话話]")
_SIZE = re.compile(r"([\d.]+)\s*([KMGT]?B)", re.I)
_UNITS = {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}


@dataclass
class HttpResult:
    """一次 HTTP 交互的最小结果集。"""

    status: int = 0
    content: bytes = b""
    url: str = ""


@dataclass
class SiteTorrent:
    """站点列表页的一行种子。"""

    tid: str = ""
    title: str = ""
    subtitle: str = ""
    size: int = 0
    seeders: int = 0
    leechers: int = 0
    completed: int = 0
    download_path: str = ""

    @property
    def key(self) -> str:
        """站点内稳定标识。"""
        return f"id={self.tid}" if self.tid else f"name={self.title}"


@dataclass
class TorrentMeta:
    """本地 .torrent 文件的关键字段。"""

    info_hash: str = ""
    name: str = ""
    total_size: int = 0
    files: Dict[str, int] = field(default_factory=dict)
    private: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

def parse_size(text: str) -> int:
    """把 `14.12 GB` 这类文本换成字节数，无法识别时返回 0。"""
    if not text:
        return 0
    # 站点偶尔用千分位与不换行空格分隔，先归一化再解析
    normalized = _html.unescape(text).replace("\xa0", " ").replace(",", "")
    match = _SIZE.search(normalized)
    if not match:
        return 0
    try:
        value = float(match.group(1))
    except ValueError:
        return 0
    unit = match.group(2).upper()
    return int(value * _UNITS.get(unit, 1))


def _strip_tags(text: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _bencode_value_end(data: bytes, offset: int) -> int:
    """跳过一个完整的 bencode 值。"""
    marker = data[offset:offset + 1]
    if marker == b"i":
        end = data.index(b"e", offset + 1)
        return end + 1
    if marker in (b"l", b"d"):
        offset += 1
        while data[offset:offset + 1] != b"e":
            if marker == b"d":
                offset = _bencode_value_end(data, offset)  # key
            offset = _bencode_value_end(data, offset)      # value
        return offset + 1
    if marker.isdigit():
        sep = data.index(b":", offset)
        length = int(data[offset:sep])
        return sep + 1 + length
    raise ValueError("invalid bencode value")


def _bencode_items(data: bytes, offset: int = 1) -> Iterable[Tuple[bytes, bytes]]:
    """遍历字典，产出键与值的原始切片。"""
    while data[offset:offset + 1] != b"e":
        sep = data.index(b":", offset)
        key_len = int(data[offset:sep])
        key = data[sep + 1:sep + 1 + key_len]
        value_start = sep + 1 + key_len
        value_end = _bencode_value_end(data, value_start)
        yield key, data[value_start:value_end]
        offset = value_end


def _bdecode(raw: bytes):
    """解码 bencode（只用于取少量标量字段，深度有限）。"""
    if not raw:
        return None
    marker = raw[:1]
    if marker == b"i":
        return int(raw[1:-1])
    if marker == b"l":
        out: List = []
        offset = 1
        while raw[offset:offset + 1] != b"e":
            value, offset = _bdecode_at(raw, offset)
            out.append(value)
        return out
    if marker == b"d":
        out_dict: Dict = {}
        for key, value in _bencode_items(raw, 1):
            out_dict[key] = _bdecode(value)
        return out_dict
    sep = raw.index(b":")
    return raw[sep + 1:]


def _bdecode_at(raw: bytes, offset: int):
    if raw[offset:offset + 1] == b"i":
        end = raw.index(b"e", offset)
        return int(raw[offset + 1:end]), end + 1
    if raw[offset:offset + 1] == b"l":
        out: List = []
        offset += 1
        while raw[offset:offset + 1] != b"e":
            value, offset = _bdecode_at(raw, offset)
            out.append(value)
        return out, offset + 1
    if raw[offset:offset + 1] == b"d":
        out_dict: Dict = {}
        offset += 1
        while raw[offset:offset + 1] != b"e":
            key, offset = _bdecode_at(raw, offset)
            value, offset = _bdecode_at(raw, offset)
            out_dict[key] = value
        return out_dict, offset + 1
    sep = raw.index(b":", offset)
    length = int(raw[offset:sep])
    start = sep + 1
    return raw[start:start + length], start + length


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def parse_torrent(content: bytes) -> Optional[TorrentMeta]:
    """解析 .torrent，返回 info_hash、名称、文件清单与总大小。"""
    if not content or content[:1] != b"d":
        return None
    try:
        info_raw = b""
        private = False
        for key, value in _bencode_items(content, 1):
            if key == b"info":
                info_raw = value
                break
        if not info_raw:
            return None
        info_hash = hashlib.sha1(info_raw).hexdigest()
        fields = {key: value for key, value in _bencode_items(info_raw, 1)}
        name = _text(_bdecode(fields.get(b"name", b""))).strip()
        private = _text(_bdecode(fields.get(b"private", b"i0e"))) == "1"
        files: Dict[str, int] = {}
        if b"files" in fields:
            for item in _bdecode(fields[b"files"]) or []:
                if not isinstance(item, dict):
                    continue
                parts = [_text(part) for part in (item.get(b"path") or [])]
                rel = "/".join(parts)
                if rel:
                    files[rel] = int(item.get(b"length") or 0)
            total = sum(files.values())
        else:
            length = int(_bdecode(fields.get(b"length", b"i0e")) or 0)
            if name:
                files[name] = length
            total = length
        return TorrentMeta(
            info_hash=info_hash,
            name=name,
            total_size=total,
            files=files,
            private=private,
        )
    except (ValueError, IndexError, TypeError, KeyError):
        return None


def looks_logged_in(html_text: str) -> bool:
    """判断列表页是否处于登录态；未登录页会跳登录或出现登录表单。"""
    if not html_text:
        return False
    if "userdetails.php?id=" in html_text and "logout.php" in html_text:
        return True
    return False


def looks_rate_limited(html_text: str) -> Tuple[bool, int]:
    """识别站点的访问频率限制提示，返回（是否被限流，建议等待秒数）。"""
    if not html_text:
        return False, 0
    if "访问过于频繁" not in html_text and "请不要过于频繁" not in html_text:
        return False, 0
    match = re.search(r"(\d+)\s*秒", html_text)
    wait = int(match.group(1)) if match else 30
    return True, max(wait, 5)


def parse_list_page(html_text: str) -> List[SiteTorrent]:
    """解析 torrents.php 结果页，按种子的 `<td class="t_pin">` 分块提取。"""
    if not html_text:
        return []
    blocks = html_text.split(_PIN_MARK)
    results: List[SiteTorrent] = []
    seen: Set[str] = set()
    for block in blocks[1:]:
        tid_match = re.search(r"details\.php\?id=(\d+)", block)
        if not tid_match:
            continue
        tid = tid_match.group(1)
        if tid in seen:
            continue
        title_match = re.search(r'<h3>\s*<a[^>]*title="([^"]*)"', block)
        subtitle_match = re.search(r"<h4>(.*?)</h4>", block, re.S)
        download_match = re.search(r'href="(download\.php\?hash=[^"]+)"', block)
        size_match = re.search(r'class="t_size"[^>]*>(.*?)</td>', block, re.S)
        seeders_match = re.search(r'class="t_torrents"[^>]*>\s*<a[^>]*>(\d+)</a>', block)
        leechers_match = re.search(r'class="t_leech"[^>]*>\s*<a[^>]*>(\d+)</a>', block)
        completed_match = re.search(r'class="t_completed"[^>]*>\s*<a[^>]*>(\d+)</a>', block)
        title = _html.unescape(title_match.group(1)) if title_match else ""
        results.append(
            SiteTorrent(
                tid=tid,
                title=title,
                subtitle=_strip_tags(subtitle_match.group(1)) if subtitle_match else "",
                size=parse_size(_strip_tags(size_match.group(1))) if size_match else 0,
                seeders=int(seeders_match.group(1)) if seeders_match else 0,
                leechers=int(leechers_match.group(1)) if leechers_match else 0,
                completed=int(completed_match.group(1)) if completed_match else 0,
                download_path=_html.unescape(download_match.group(1)) if download_match else "",
            )
        )
        seen.add(tid)
    return results


def has_next_page(html_text: str) -> bool:
    """结果页是否存在下一页链接。"""
    return bool(re.search(r"page=\d+", html_text or ""))


# ---------------------------------------------------------------------------
# 名称归一与相似度
# ---------------------------------------------------------------------------

def split_tokens(name: str) -> List[str]:
    """把发布名拆成小写 token，保留年份与季集，技术规格词单独保留。"""
    text = _html.unescape(name or "")
    # 去掉站内推荐标记与方括号标签，它们是装饰而非片名
    text = re.sub(r"★[^★]*★", " ", text)
    text = re.sub(r"[\[【][^\]】]*[\]】]", " ", text)
    text = _VIDEO_EXT.sub("", text)
    text = _GROUP_SUFFIX.sub("", text)
    text = re.sub(r"blu-?ray", " bluray ", text, flags=re.I)
    text = re.sub(r"web-?dl", " webdl ", text, flags=re.I)
    text = re.sub(r"h\.?(26[45])", r"h\1", text, flags=re.I)
    text = re.sub(r"dts-?hd", " dtshd ", text, flags=re.I)
    text = re.sub(r"dd\+", " ddp ", text, flags=re.I)
    text = re.sub(r"[★☆·•◆■□]", " ", text)
    text = text.replace("+", " ").replace("&", " ")
    raw = re.split(r"[.\s_()（）\-]+", text)
    tokens: List[str] = []
    for token in raw:
        token = token.strip().lower()
        if not token:
            continue
        if _EPISODE.fullmatch(token) or _YEAR.fullmatch(token):
            tokens.append(token)
            continue
        if token in _TECH_TOKENS:
            tokens.append(token)
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            tokens.append(token)
            continue
        tokens.append(token)
    return tokens


def name_tokens(name: str) -> List[str]:
    """片名 token：遇到第一个规格词即截断，最多保留 8 个。"""
    tokens = split_tokens(name)
    result: List[str] = []
    for token in tokens:
        if token in _TECH_TOKENS:
            break
        if re.fullmatch(r"\d+(?:\.\d+)?", token) and not _YEAR.fullmatch(token):
            continue
        result.append(token)
    return result[:8]


def build_search_key(name: str) -> Tuple[str, str]:
    """由发布名推导站点搜索关键词，返回（主关键词，降级关键词）。"""
    tokens = name_tokens(name)
    if not tokens:
        return "", ""
    primary = tokens[:5]
    fallback = [token for token in tokens if not _YEAR.fullmatch(token)][:3]
    return " ".join(primary), " ".join(fallback)


def episode_of(name: str) -> str:
    """提取季集标识，统一成 SxxEyy / 第N集 的形式。"""
    text = name or ""
    match = _EPISODE.search(text)
    if match:
        return f"s{int(match.group(1)):02d}e{int(match.group(2)):02d}"
    match = _EPISODE_CN.search(text)
    if match:
        return f"第{int(match.group(1))}集"
    return ""


def year_of(name: str) -> str:
    """提取发布名中的年份。"""
    match = _YEAR.search(name or "")
    return match.group(0) if match else ""


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def best_similarity(local_name: str, title: str, subtitle: str = "") -> float:
    """本地任务名与站点种子的相似度，副标题里的译名也参与比较。"""
    local = name_tokens(local_name)
    if not local:
        return 0.0
    score = jaccard(local, name_tokens(title))
    for part in re.split(r"[|/／]", subtitle or ""):
        part = re.sub(r"(等)?(中字|字幕|国语|粤语|双语|英语|导评).*$", "", part)
        score = max(score, jaccard(local, name_tokens(part)))
    return round(score, 4)


def candidate_ok(local_name: str, candidate: SiteTorrent, min_score: float = 0.6) -> float:
    """年份/季集冲突时直接判负，否则返回相似度。"""
    local_year, cand_year = year_of(local_name), year_of(candidate.title)
    if local_year and cand_year and local_year != cand_year:
        return 0.0
    local_ep, cand_ep = episode_of(local_name), episode_of(candidate.title)
    if local_ep and cand_ep and local_ep != cand_ep:
        return 0.0
    score = best_similarity(local_name, candidate.title, candidate.subtitle)
    return score if score >= min_score else 0.0


# ---------------------------------------------------------------------------
# 站点客户端
# ---------------------------------------------------------------------------

Fetcher = Callable[[str], Optional[HttpResult]]


class HDWinGClient:
    """带节流与限流退避的 HDWinG 只读客户端。"""

    def __init__(
        self,
        fetcher: Fetcher,
        base_url: str = DEFAULT_SITE_URL,
        interval: float = 6.0,
        max_retry: int = 2,
        stopped: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._fetch = fetcher
        self._base = (base_url or DEFAULT_SITE_URL).rstrip("/")
        self._interval = max(float(interval or 0), 0.0)
        self._max_retry = max(int(max_retry), 1)
        self._stopped = stopped or (lambda: False)
        self._last_at = 0.0
        self.last_error = ""

    @property
    def base_url(self) -> str:
        return self._base

    def _wait(self) -> None:
        if self._interval <= 0:
            return
        elapsed = time.monotonic() - self._last_at
        remain = self._interval - elapsed
        if remain > 0:
            time.sleep(min(remain, 30))

    def _request(self, url: str) -> Optional[HttpResult]:
        for attempt in range(self._max_retry):
            if self._stopped():
                return None
            self._wait()
            try:
                result = self._fetch(url)
            except Exception as err:  # noqa: BLE001 - 网络异常统一退化
                self.last_error = str(err)
                result = None
            self._last_at = time.monotonic()
            if result is None:
                continue
            text = result.content.decode("utf-8", "ignore") if result.content else ""
            limited, wait = looks_rate_limited(text)
            if limited and attempt < self._max_retry - 1:
                self.last_error = f"站点限流，等待 {wait} 秒"
                time.sleep(min(wait, 60))
                continue
            return result
        return None

    def check_login(self) -> Tuple[bool, str]:
        """访问首页确认 cookie 是否有效。"""
        result = self._request(f"{self._base}/torrents.php")
        if not result:
            return False, self.last_error or "无法访问站点"
        text = result.content.decode("utf-8", "ignore")
        if looks_logged_in(text):
            return True, "cookie 有效"
        if "login.php" in text or "takelogin" in text:
            return False, "cookie 已失效，请更新"
        return False, f"页面无法识别登录态（HTTP {result.status}）"

    def search(self, keyword: str, page: int = 0) -> List[SiteTorrent]:
        """按关键词搜索，返回当前页结果。"""
        if not keyword:
            return []
        from urllib.parse import quote_plus

        url = (
            f"{self._base}/torrents.php?search={quote_plus(keyword)}"
            "&search_area=0&search_mode=0&incldead=1&spstate=0&inclbookmarked=0"
        )
        if page:
            url += f"&page={page}"
        result = self._request(url)
        if not result or not result.content:
            return []
        html_text = result.content.decode("utf-8", "ignore")
        if not looks_logged_in(html_text):
            self.last_error = "搜索时登录态失效"
            return []
        return parse_list_page(html_text)

    def download(self, torrent: SiteTorrent) -> Optional[bytes]:
        """下载站点种子文件。"""
        if not torrent.download_path:
            return None
        url = f"{self._base}/{torrent.download_path.lstrip('/')}"
        result = self._request(url)
        if not result or not result.content:
            return None
        if not result.content.startswith(b"d"):
            self.last_error = "返回内容不是种子文件"
            return None
        return result.content
