"""Pieces Hash 辅种插件的纯逻辑层。

本模块不依赖 MoviePilot 宿主，可被单元测试直接导入；负责 bencode 解析、
种子指纹（info_hash / pieces_hash）、配置文本解析、URL 构造和站点支持判定。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterator, Sequence
from urllib.parse import quote, urljoin, urlparse

# 站点 pieces-hash 接口的常见路径；憨憨等站点使用 nexusapi 前缀
DEFAULT_API_PATHS: tuple[str, ...] = ("api/pieces-hash", "nexusapi/pieces-hash")
DEFAULT_DOWNLOAD_TPL = "download.php?id={id}&passkey={passkey}"
DEFAULT_CRON = "0 4 * * *"
PROBE_PIECES_HASH = "0" * 40
_CRON_TOKEN = re.compile(r"^[0-9*,/?\-]+$")
# 不在 URL 的 "://" 处切分，允许直接填写完整接口地址
_SPLIT_CONFIG = re.compile(r"[:：](?!//)")


class BencodeError(ValueError):
    """种子内容不是合法的 bencode 数据，或缺少必要字段。"""


class BencodeDict(dict):
    """记录每个键值原始字节区间的字典，用于精确计算 info_hash。"""

    def __init__(self) -> None:
        super().__init__()
        self.spans: dict[str, tuple[int, int]] = {}


def _decode(data: bytes, index: int = 0) -> tuple[Any, int, int]:
    """解析单个 bencode 值，返回 (值, 起始位置, 结束位置)。"""
    start = index
    if index >= len(data):
        raise BencodeError("种子数据意外结束")
    token = data[index : index + 1]

    if token == b"i":
        end = data.find(b"e", index + 1)
        if end < 0:
            raise BencodeError("整数缺少结束符")
        raw = data[index + 1 : end]
        if not re.fullmatch(rb"-?\d+", raw):
            raise BencodeError(f"非法整数：{raw[:20]!r}")
        return int(raw), start, end + 1

    if token in (b"l", b"d"):
        index += 1
        if token == b"l":
            result: list[Any] = []
            while index < len(data) and data[index : index + 1] != b"e":
                value, _, index = _decode(data, index)
                result.append(value)
            if index >= len(data):
                raise BencodeError("列表缺少结束符")
            return result, start, index + 1

        result_dict = BencodeDict()
        while index < len(data) and data[index : index + 1] != b"e":
            key, _, index = _decode(data, index)
            if not isinstance(key, bytes):
                raise BencodeError("字典键必须是字符串")
            value, value_start, index = _decode(data, index)
            key_text = key.decode("utf-8", "surrogateescape")
            result_dict[key_text] = value
            result_dict.spans[key_text] = (value_start, index)
        if index >= len(data):
            raise BencodeError("字典缺少结束符")
        return result_dict, start, index + 1

    if token.isdigit():
        colon = data.find(b":", index)
        if colon < 0:
            raise BencodeError("字符串缺少长度分隔符")
        length_raw = data[index:colon]
        if not length_raw.isdigit():
            raise BencodeError("非法的字符串长度")
        length = int(length_raw)
        end = colon + 1 + length
        if end > len(data):
            raise BencodeError("字符串超出数据范围")
        return data[colon + 1 : end], start, end

    raise BencodeError(f"无法识别的 bencode 标记：{token!r}")


def bdecode(data: bytes) -> BencodeDict:
    """把种子字节解析为带原始区间记录的字典。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise BencodeError("种子内容必须是字节")
    value, _, _ = _decode(bytes(data))
    if not isinstance(value, BencodeDict):
        raise BencodeError("种子根元素必须是字典")
    return value


def _as_text(value: Any) -> str:
    """把 bencode 字节值安全地转成文本。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


@dataclass(frozen=True)
class TorrentFingerprint:
    """种子内容指纹；pieces_hash 是跨站点识别同一份内容的关键。"""

    info_hash: str
    pieces_hash: str
    name: str = ""
    total_size: int = 0
    piece_length: int = 0
    file_count: int = 0
    announce: str = ""


def parse_torrent(data: bytes) -> TorrentFingerprint:
    """解析种子并计算 info_hash 与 pieces_hash，坏种子抛 BencodeError。"""
    torrent = bdecode(data)
    info = torrent.get("info")
    if not isinstance(info, BencodeDict):
        raise BencodeError("种子缺少 info 字典")
    info_span = torrent.spans.get("info")
    if not info_span:
        raise BencodeError("无法定位 info 原始字节")

    pieces = info.get("pieces")
    if not isinstance(pieces, bytes) or not pieces or len(pieces) % 20:
        raise BencodeError("种子 pieces 字段无效")

    files = info.get("files")
    if isinstance(files, list):
        total_size = sum(int(item.get("length", 0)) for item in files if isinstance(item, dict))
        file_count = len(files)
    else:
        total_size = int(info.get("length", 0) or 0)
        file_count = 1

    return TorrentFingerprint(
        info_hash=hashlib.sha1(data[info_span[0] : info_span[1]]).hexdigest(),
        pieces_hash=hashlib.sha1(pieces).hexdigest(),
        name=_as_text(info.get("name", b"")),
        total_size=total_size,
        piece_length=int(info.get("piece length", 0) or 0),
        file_count=file_count,
        announce=_as_text(torrent.get("announce", b"")),
    )


def is_torrent_content(data: bytes | None) -> bool:
    """判断下载到的内容是否为可解析的种子，避免把登录页当成种子。"""
    if not data or not data.startswith((b"d",)):
        return False
    try:
        parse_torrent(data)
    except (BencodeError, ValueError, TypeError):
        return False
    return True


@dataclass
class SiteCredential:
    """一条站点凭据配置：站点名:passkey[:接口路径[:请求间隔[:下载模板]]]。"""

    name: str
    passkey: str
    api_path: str | None = None
    gap: int = 1
    download_tpl: str = DEFAULT_DOWNLOAD_TPL


def parse_credentials(text: str | None) -> dict[str, SiteCredential]:
    """解析多行站点凭据，支持中英文冒号、# 注释和可选字段乱序。"""
    credentials: dict[str, SiteCredential] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in _SPLIT_CONFIG.split(line)]
        name, passkey = parts[0], parts[1] if len(parts) > 1 else ""
        if not name or not passkey:
            continue
        item = SiteCredential(name=name, passkey=passkey)
        for extra in parts[2:]:
            if not extra:
                continue
            if extra.isdigit():
                item.gap = max(0, min(60, int(extra)))
            elif "{id}" in extra:
                item.download_tpl = extra
            elif "/" in extra or extra.startswith("http") or "pieces-hash" in extra:
                item.api_path = extra
        credentials[name] = item
    return credentials


def parse_list(text: str | None, separators: str = ",\n") -> list[str]:
    """把逗号或换行分隔的配置文本切成去重后的字符串列表。"""
    pattern = f"[{re.escape(separators)}]"
    return list(dict.fromkeys(item.strip() for item in re.split(pattern, text or "") if item.strip()))


def parse_path_mapping(text: str | None) -> list[tuple[str, str]]:
    """解析跨下载器路径映射，每行『源前缀::目标前缀』，长前缀优先。"""
    mappings: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        if not line.strip() or line.strip().startswith("#") or "::" not in line:
            continue
        source, target = (part.strip().rstrip("/") for part in line.split("::", 1))
        if source and target:
            mappings.append((source, target))
    return sorted(mappings, key=lambda item: len(item[0]), reverse=True)


def apply_path_mapping(path: str, mappings: Sequence[tuple[str, str]]) -> str:
    """按最长前缀规则把源下载器路径转换为目标下载器路径。"""
    normalized = (path or "").replace("\\", "/").rstrip("/")
    for source, target in mappings:
        if normalized.startswith(source.replace("\\", "/").rstrip("/")):
            suffix = normalized[len(source.replace("\\", "/").rstrip("/")) :]
            return f"{target}{suffix}"
    return path


def normalize_site_url(url: str | None) -> str:
    """规范站点根地址，补齐协议与结尾斜杠。"""
    value = (url or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return value if value.endswith("/") else f"{value}/"


def build_api_url(site_url: str, api_path: str | None) -> str:
    """构造站点 pieces-hash 接口地址，支持完整 URL 与相对路径。"""
    base = normalize_site_url(site_url)
    path = (api_path or DEFAULT_API_PATHS[0]).strip()
    if path.startswith(("http://", "https://")):
        return path
    if not base:
        return ""
    return urljoin(base, path.lstrip("/"))


def build_torrent_url(site_url: str, template: str, torrent_id: str, passkey: str) -> str:
    """按站点模板构造种子下载地址，模板支持 {id} / {passkey} / {site}。"""
    base = normalize_site_url(site_url)
    if not base:
        return ""
    rendered = (template or DEFAULT_DOWNLOAD_TPL).format(
        id=quote(str(torrent_id), safe=""),
        passkey=quote(passkey or "", safe=""),
        site=base,
    )
    return rendered if rendered.startswith(("http://", "https://")) else urljoin(base, rendered.lstrip("/"))


def normalize_cron(expression: str | None) -> str:
    """把 crontab 中的 ? 归一为 *，并统一空白。"""
    return " ".join((expression or "").replace("?", "*").split())


def validate_cron(expression: str | None) -> tuple[bool, str]:
    """校验 5 段 crontab 表达式的字符集、字段数与数值范围。"""
    text = normalize_cron(expression)
    if not text:
        return False, "执行周期为空"
    fields = text.split(" ")
    if len(fields) != 5:
        return False, "crontab 必须是 5 段：分 时 日 月 周"
    limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    for field, (low, high) in zip(fields, limits):
        if not _CRON_TOKEN.fullmatch(field):
            return False, f"非法字符：{field}"
        for number in re.findall(r"\d+", field):
            if not low <= int(number) <= high:
                return False, f"字段 {field} 超出范围 {low}-{high}"
    return True, text


def chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    """按固定大小切分序列，用于批量查询站点接口，避免单次请求过大。"""
    step = max(1, int(size or 1))
    for start in range(0, len(items), step):
        yield items[start : start + step]


def mask_secret(value: str | None) -> str:
    """日志脱敏，避免 passkey / cookie 完整落盘。"""
    text = value or ""
    if len(text) <= 8:
        return "***"
    return f"{text[:3]}***{text[-3:]}"


def classify_probe(status_code: int, content_type: str, body: Any) -> tuple[bool, str]:
    """根据接口响应判定站点是否支持 pieces-hash 查询，返回 (是否支持, 原因)。

    站点对 GET 返回 405、或 POST 返回带 data 字段的 JSON，均视为接口可用；
    404、登录页 HTML、非 JSON 错误页则视为不支持或凭据失效。
    """
    ctype = (content_type or "").lower()
    is_json = "json" in ctype or isinstance(body, (dict, list))
    if status_code == 405:
        return True, "接口存在（GET 返回 405，需 POST 查询）"
    if status_code in (200, 201, 202, 400, 422) and is_json:
        if isinstance(body, dict) and ("data" in body or "success" in body or "error" in body):
            return True, "接口返回标准 JSON 结构"
        if isinstance(body, dict):
            return True, "接口返回 JSON 响应"
    if status_code in (401, 403):
        return False, "凭据无效或权限不足（检查 Cookie / passkey）"
    if status_code == 404:
        return False, "接口不存在（404）"
    if status_code >= 500:
        return False, f"站点服务异常（HTTP {status_code}）"
    raw = body.encode("utf-8", "ignore") if isinstance(body, str) else (body if isinstance(body, bytes) else None)
    if "html" in ctype or (raw is not None and b"<html" in raw.lower()):
        return False, "返回登录页/HTML，Cookie 可能已失效"
    return False, f"未识别的接口响应（HTTP {status_code}）"


def parse_pieces_response(body: Any) -> dict[str, str]:
    """把站点 pieces-hash 响应规范化为 {pieces_hash: 种子ID}。"""
    if not isinstance(body, dict):
        return {}
    data = body.get("data", body)
    result: dict[str, str] = {}

    def accept(key: Any, value: Any) -> None:
        """只接受看起来像 pieces hash 的键，跳过 success/message 等元数据。"""
        text = str(key or "")
        if value is not None and re.fullmatch(r"[0-9a-fA-F]{32,64}", text):
            result[text.lower()] = str(value)

    if isinstance(data, dict):
        for pieces_hash, torrent_id in data.items():
            accept(pieces_hash, torrent_id)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                key = item.get("pieces_hash") or item.get("piecesHash")
                value = item.get("id") or item.get("torrent_id") or item.get("torrentId")
                accept(key, value)
    return result
