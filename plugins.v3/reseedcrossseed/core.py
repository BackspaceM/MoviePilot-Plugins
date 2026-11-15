"""Reseed 辅种插件的纯逻辑层。

不依赖 MoviePilot 宿主，只使用标准库，便于脱离宿主做单元测试。负责：
- 扫描下载目录生成 Reseed 索引（顶层项 -> 大小 / 相对路径 -> 大小）
- 配置文本解析、索引摘要、结果与站点列表解析
- 种子解析、文件清单复核、下载地址构造、cron 校验
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence
from urllib.parse import quote, urljoin, urlparse

DEFAULT_API = "https://reseed-api.tongyifan.me/"
DEFAULT_CRON = "0 5 * * *"
DEFAULT_DOWNLOAD_TPL = "download.php?id={id}&passkey={passkey}"
DEFAULT_SEED_TAG = "Reseed辅种"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_INDEX_ITEMS = 2000
MAX_INDEX_FILES = 20000

# 不参与索引的文件：种子文件、临时文件、隐藏文件与常见无关格式
_IGNORE_EXTS = frozenset({
    ".torrent", ".part", ".partial", "!qb", ".tmp", ".temp", ".aria2",
    ".nfo", ".sfv", ".jpg", ".jpeg", ".png", ".gif", ".txt", ".url",
    ".log", ".db", ".ini", ".ds_store",
})
_IGNORE_NAMES = frozenset({"__pycache__", ".recycle", "@eaDir", ".@__thumb", "lost+found"})
_CRON_TOKEN = re.compile(r"^[0-9*,/?\-]+$")
_SPLIT_CONFIG = re.compile(r"[:：](?!//)")


class TorrentError(ValueError):
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
        raise TorrentError("种子数据意外结束")
    token = data[index:index + 1]

    if token == b"i":
        end = data.find(b"e", index + 1)
        if end < 0:
            raise TorrentError("整数缺少结束符")
        raw = data[index + 1:end]
        if not re.fullmatch(rb"-?\d+", raw):
            raise TorrentError("非法整数")
        return int(raw), start, end + 1

    if token in (b"l", b"d"):
        index += 1
        if token == b"l":
            result: list[Any] = []
            while index < len(data) and data[index:index + 1] != b"e":
                value, _, index = _decode(data, index)
                result.append(value)
            if index >= len(data):
                raise TorrentError("列表缺少结束符")
            return result, start, index + 1

        result_dict = BencodeDict()
        while index < len(data) and data[index:index + 1] != b"e":
            key, _, index = _decode(data, index)
            if not isinstance(key, bytes):
                raise TorrentError("字典键必须是字符串")
            value, value_start, index = _decode(data, index)
            key_text = key.decode("utf-8", "surrogateescape")
            result_dict[key_text] = value
            result_dict.spans[key_text] = (value_start, index)
        if index >= len(data):
            raise TorrentError("字典缺少结束符")
        return result_dict, start, index + 1

    if token.isdigit():
        colon = data.find(b":", index)
        if colon < 0:
            raise TorrentError("字符串缺少长度分隔符")
        length_raw = data[index:colon]
        if not length_raw.isdigit():
            raise TorrentError("非法的字符串长度")
        length = int(length_raw)
        end = colon + 1 + length
        if end > len(data):
            raise TorrentError("字符串超出数据范围")
        return data[colon + 1:end], start, end

    raise TorrentError("无法识别的 bencode 标记")


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


@dataclass(frozen=True)
class TorrentMeta:
    """解析后的种子元数据，用于去重与文件清单复核。"""

    info_hash: str = ""
    name: str = ""
    total_size: int = 0
    files: dict[str, int] = field(default_factory=dict)
    private: bool = False


def parse_torrent(data: bytes) -> TorrentMeta:
    """解析种子，计算 info_hash、名称与文件清单。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TorrentError("种子内容必须是字节")
    root, _, _ = _decode(bytes(data))
    if not isinstance(root, BencodeDict):
        raise TorrentError("种子根元素必须是字典")
    info = root.get("info")
    if not isinstance(info, BencodeDict):
        raise TorrentError("种子缺少 info 字典")
    info_span = root.spans.get("info")
    if not info_span:
        raise TorrentError("无法定位 info 原始字节")

    files: dict[str, int] = {}
    file_list = info.get("files")
    if isinstance(file_list, list):
        for item in file_list:
            if not isinstance(item, BencodeDict):
                continue
            parts = item.get("path")
            if isinstance(parts, list):
                rel = "/".join(_as_text(part).replace("\\", "/") for part in parts)
            else:
                rel = _as_text(parts)
            if rel:
                files[rel.strip("/")] = int(item.get("length", 0) or 0)
        total = sum(files.values())
    else:
        total = int(info.get("length", 0) or 0)
        name = _as_text(info.get("name", b""))
        if name:
            files[name.strip("/")] = total

    name = _as_text(info.get("name", b""))
    return TorrentMeta(
        info_hash=hashlib.sha1(bytes(data)[info_span[0]:info_span[1]]).hexdigest(),
        name=name,
        total_size=total,
        files=files,
        private=bool(info.get("private", 0)),
    )


def is_torrent_content(data: bytes | None) -> bool:
    """判断下载到的内容是否为可解析的种子，避免把登录页当种子。"""
    if not data or not data.startswith(b"d"):
        return False
    try:
        parse_torrent(data)
    except (TorrentError, ValueError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SiteBinding:
    """一条站点映射：Reseed 站点名 -> MoviePilot 站点名 + Passkey。"""

    reseed_name: str
    mp_name: str
    passkey: str
    download_tpl: str = DEFAULT_DOWNLOAD_TPL


def parse_bindings(text: str | None) -> dict[str, SiteBinding]:
    """解析多行映射：``Reseed站点名:MP站点名:Passkey[:下载模板]``。

    支持中英文冒号、``#`` 注释；下载模板需含 ``{id}``，可选字段顺序可打乱。
    """
    bindings: dict[str, SiteBinding] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in _SPLIT_CONFIG.split(line)]
        if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
            continue
        template = DEFAULT_DOWNLOAD_TPL
        for extra in parts[3:]:
            if "{id}" in extra:
                template = extra
        bindings[parts[0].lower()] = SiteBinding(
            reseed_name=parts[0],
            mp_name=parts[1],
            passkey=parts[2],
            download_tpl=template,
        )
    return bindings


def parse_list(text: str | None, separators: str = ",\n") -> list[str]:
    """把逗号或换行分隔的配置文本切成去重后的字符串列表。"""
    pattern = f"[{re.escape(separators)}]"
    return list(dict.fromkeys(item.strip() for item in re.split(pattern, text or "") if item.strip()))


def parse_dirs(text: str | None) -> list[str]:
    """解析扫描目录，每行一个绝对路径，忽略注释与重复项。"""
    result: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().rstrip("/")
        if not line or line.startswith("#"):
            continue
        if line not in result:
            result.append(line)
    return result


def mask_secret(value: str | None) -> str:
    text = value or ""
    return "***" if len(text) <= 8 else f"{text[:3]}***{text[-3:]}"


def normalize_api_url(url: str | None) -> str:
    """规范 Reseed 服务地址，补齐协议与结尾斜杠。"""
    value = (url or "").strip() or DEFAULT_API
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return DEFAULT_API
    return value if value.endswith("/") else f"{value}/"


def normalize_site_url(url: str | None) -> str:
    """规范站点根地址。"""
    value = (url or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return value if value.endswith("/") else f"{value}/"


def build_torrent_url(site_url: str, template: str, torrent_id: str, passkey: str) -> str:
    """按模板构造种子下载地址，支持 ``{id}`` / ``{passkey}`` / ``{site}``。"""
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
    return " ".join((expression or "").replace("?", "*").split())


def validate_cron(expression: str | None) -> tuple[bool, str]:
    """校验 5 段 crontab 表达式。"""
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
    step = max(1, int(size or 1))
    for start in range(0, len(items), step):
        yield items[start:start + step]


# ---------------------------------------------------------------------------
# 索引生成
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IndexItem:
    """一个顶层本地条目，用于结果回查与保存路径定位。"""

    name: str
    save_path: str
    files: dict[str, int] | None
    size: int


def _ignored(name: str) -> bool:
    lower = name.lower()
    if not name or name.startswith(".") or lower in _IGNORE_NAMES:
        return True
    return any(lower.endswith(ext) for ext in _IGNORE_EXTS)


def build_index(
    root: str,
    excludes: Sequence[str] = (),
    max_items: int = MAX_INDEX_ITEMS,
    max_files: int = MAX_INDEX_FILES,
) -> tuple[dict[str, Any], list[IndexItem]]:
    """扫描下载根目录的顶层项，生成 Reseed 索引与本地条目视图。

    Reseed 服务端按「顶层项名称」找候选种子，再逐个比较文件相对路径与大小；
    目录项的键使用 ``/`` 分隔（服务端会把 ``\\`` 归一为 ``/``）。
    """
    base = os.path.abspath(os.path.expanduser(root or ""))
    result: dict[str, Any] = {}
    items: list[IndexItem] = []
    if not base or not os.path.isdir(base):
        return {"base_dir": base, "result": result}, items

    patterns = [item.strip().lower() for item in excludes if item.strip()]
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return {"base_dir": base, "result": result}, items

    for entry in entries:
        if len(result) >= max_items:
            break
        if _ignored(entry) or any(pattern in entry.lower() for pattern in patterns):
            continue
        path = os.path.join(base, entry)
        if os.path.isfile(path):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            result[entry] = size
            items.append(IndexItem(name=entry, save_path=base, files=None, size=size))
        elif os.path.isdir(path):
            files: dict[str, int] = {}
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [name for name in dirnames if not _ignored(name)]
                for filename in filenames:
                    if _ignored(filename):
                        continue
                    full = os.path.join(dirpath, filename)
                    rel = os.path.relpath(full, path).replace(os.sep, "/")
                    try:
                        files[rel] = os.path.getsize(full)
                    except OSError:
                        continue
                    if len(files) >= max_files:
                        break
                if len(files) >= max_files:
                    break
            if files:
                result[entry] = files
                items.append(IndexItem(
                    name=entry, save_path=base, files=dict(files), size=sum(files.values())
                ))
    return {"base_dir": base, "result": result}, items


def encode_index(index: dict[str, Any]) -> bytes:
    """把索引编码为纯 ASCII JSON，兼容服务端对非 ASCII 字符的限制。"""
    return json.dumps(index, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def index_digest(index: dict[str, Any]) -> str:
    """索引内容摘要，用于命中结果缓存，避免重复上传触发服务端限流。"""
    payload = json.dumps(index, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 服务端响应解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Hit:
    """一条 Reseed 命中：某个本地顶层项在某个站点有可辅种种子。"""

    name: str
    site: str
    torrent_id: str
    full_match: bool

    @property
    def key(self) -> str:
        return f"{self.site}-{self.torrent_id}"


def parse_sites_info(body: Any) -> list[dict[str, str]]:
    """把 ``/sites_info`` 响应规范化为 ``[{name, base_url}]``。"""
    data = body.get("data", body) if isinstance(body, dict) else body
    if not isinstance(data, list):
        return []
    result: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            result.append({"name": name, "base_url": str(item.get("base_url") or "").strip()})
    return result


def parse_site_tokens(text: str | None) -> list[tuple[str, str]]:
    """把 ``HDU-23456,TJUPT-34567`` 解析为 [(站点名, 种子ID), ...]。"""
    result: list[tuple[str, str]] = []
    for token in (text or "").split(","):
        value = token.strip()
        if not value or "-" not in value:
            continue
        site, torrent_id = value.rsplit("-", 1)
        if site.strip() and torrent_id.strip().isdigit():
            result.append((site.strip(), torrent_id.strip()))
    return result


def parse_upload_result(body: Any) -> list[Hit]:
    """把 ``/upload_json`` 响应规范化为命中列表。"""
    if not isinstance(body, dict) or not body.get("success"):
        return []
    rows = body.get("result")
    if not isinstance(rows, list):
        return []
    hits: list[Hit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        for key, full_match in (("cmp_success", True), ("cmp_warning", False)):
            entries = row.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for site, torrent_id in parse_site_tokens(str(entry.get("sites") or "")):
                    hits.append(Hit(name=name, site=site, torrent_id=torrent_id, full_match=full_match))
    return hits


def _normalize_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/").lstrip("./")


def _same_size(left: int, right: int, tolerance: float) -> bool:
    if left <= 0 or right <= 0:
        return False
    if tolerance <= 0:
        return left == right
    return abs(left - right) <= max(left, right) * tolerance


def verify_match(local: IndexItem, meta: TorrentMeta, tolerance: float = 0.0) -> bool:
    """复核站点种子的文件清单与本地文件是否一致。

    站点种子可能带或不带顶层目录名，这里依次尝试三种路径对齐方式，
    只要有一种能让文件名集合与大小全部对上即判定同源。
    """
    local_files = local.files if local.files is not None else {local.name: local.size}
    torrent_files = meta.files or ({meta.name: meta.total_size} if meta.name else {})
    left = {_normalize_path(key): int(value) for key, value in local_files.items() if _normalize_path(key)}
    right = {_normalize_path(key): int(value) for key, value in torrent_files.items() if _normalize_path(key)}
    if not left or not right:
        return False

    def strip_first(mapping: dict[str, int]) -> dict[str, int]:
        return {("/".join(key.split("/")[1:]) or key): value for key, value in mapping.items()}

    variants = (
        (left, right),
        (left, strip_first(right)),
        (strip_first(left), right),
        ({f"{local.name}/{key}": value for key, value in left.items()}, right),
        (left, {f"{meta.name}/{key}": value for key, value in right.items()}),
    )
    for lhs, rhs in variants:
        if set(lhs) != set(rhs):
            continue
        if all(_same_size(value, rhs[key], tolerance) for key, value in lhs.items()):
            return True
    return False
