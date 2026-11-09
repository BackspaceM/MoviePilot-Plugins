"""Cloudflare 优选 IP 的纯逻辑与探测实现。

职责边界：
- 网段解析与 Cloudflare CDN 归属判断（内置官方段 + 官方刷新 + 自定义池）
- IP 池解析：兼容纯文本 / JSON / CIDR / IP:端口 / IPv6 等第三方维护格式
- 域名判定：默认不做任何 DNS 探测，可选系统 DNS 或多源 DoH 判定
- TCP/TLS 握手延迟探测与排序
- DNS static 记录的字段构造

除 asyncio 探测与系统 DNS 外均为纯函数，便于离线单测。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Cloudflare 官方公布段（https://www.cloudflare.com/ips-v4 / ips-v6）
DEFAULT_IPV4_CIDRS: Tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)

DEFAULT_IPV6_CIDRS: Tuple[str, ...] = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

DEFAULT_DOH_URL = "https://cloudflare-dns.com/dns-query"
# 多源 DoH：仅在「DoH 判定」模式下使用，任一成功即可，失败会自动回退系统 DNS
DEFAULT_DOH_URLS: Tuple[str, ...] = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
    "https://doh.pub/dns-query",
    "https://dns.alidns.com/dns-query",
)
CF_V4_LIST_URL = "https://www.cloudflare.com/ips-v4"
CF_V6_LIST_URL = "https://www.cloudflare.com/ips-v6"

# 站点判定方式：all 不依赖任何 DNS 服务；system 用容器 DNS；doh 用多源 DoH
CHECK_MODES: Tuple[str, ...] = ("all", "system", "doh")
DEFAULT_CHECK_MODE = "all"
DEFAULT_PROBE_SNI = "cloudflare.com"

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_IPV4_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?$")
_IPV6_PORT_RE = re.compile(r"^\[([0-9a-fA-F:.]+)\](?::\d+)?$")
_PREFIX_RE = re.compile(r"^(?:ip|addr|address|host|server|value)\s*[:=]\s*", re.I)
_JSON_IP_KEYS = (
    "ip", "ips", "address", "addresses", "host", "value", "data", "result",
    "list", "records", "ipv4", "ipv6", "cloudflare", "cf", "items", "cdn",
)


# ---------------------------------------------------------------------------
# 网段
# ---------------------------------------------------------------------------

def parse_cidrs(text: Optional[str]) -> List[ipaddress._BaseNetwork]:
    """把多行 CIDR 文本解析为网段列表，非法行忽略。"""
    networks: List[ipaddress._BaseNetwork] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for token in re.split(r"[\s,;]+", line):
            if not token:
                continue
            try:
                networks.append(ipaddress.ip_network(token, strict=False))
            except ValueError:
                continue
    return networks


def build_networks(
    ipv4_text: Optional[str] = None,
    ipv6_text: Optional[str] = None,
    enabled: bool = True,
) -> List[ipaddress._BaseNetwork]:
    """
    合并内置段与用户覆盖段。

    用户提供的内容解析成功时以用户为准（便于跟进官方变更），
    否则回落到内置常量；`enabled=False` 时只返回空列表。
    """
    if not enabled:
        return []
    nets: List[ipaddress._BaseNetwork] = []
    v4 = parse_cidrs(ipv4_text) or parse_cidrs("\n".join(DEFAULT_IPV4_CIDRS))
    nets.extend(v4)
    v6 = parse_cidrs(ipv6_text) or parse_cidrs("\n".join(DEFAULT_IPV6_CIDRS))
    nets.extend(v6)
    return nets


def merge_networks(*groups: Sequence[ipaddress._BaseNetwork]) -> List[ipaddress._BaseNetwork]:
    """合并多组网端并按文本去重（内置 + 官方 + 自定义 CIDR 池）。"""
    result: List[ipaddress._BaseNetwork] = []
    seen: set[str] = set()
    for group in groups:
        for net in group or []:
            key = str(net)
            if key in seen:
                continue
            seen.add(key)
            result.append(net)
    return result


def is_cloudflare_ip(ip: str, networks: Sequence[ipaddress._BaseNetwork]) -> bool:
    """判断单个 IP 是否落在给定网段内。"""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in networks)


def cloudflare_ips(ips: Iterable[str], networks: Sequence[ipaddress._BaseNetwork]) -> List[str]:
    """过滤出属于 Cloudflare 的 IP，保持原顺序并去重。"""
    seen: set[str] = set()
    result: List[str] = []
    for ip in ips:
        value = str(ip).strip()
        if value in seen:
            continue
        seen.add(value)
        if is_cloudflare_ip(value, networks):
            result.append(value)
    return result


def sample_ips(
    networks: Sequence[ipaddress._BaseNetwork],
    count: int,
    *,
    ipv4_only: bool = True,
    seed: Optional[int] = None,
) -> List[str]:
    """从网段中随机采样候选 IP（去重，尽量分散到不同网段）。"""
    if count <= 0:
        return []
    pool = [
        net for net in networks
        if (net.version == 4 if ipv4_only else True)
    ]
    if not pool:
        return []
    rng = random.Random(seed)
    picked: Dict[str, None] = {}
    # 先每段取一个，保证覆盖面，再随机补足
    for net in pool:
        picked[str(_random_host(net, rng))] = None
    guard = 0
    while len(picked) < count and guard < count * 20:
        guard += 1
        net = pool[rng.randrange(len(pool))]
        picked[str(_random_host(net, rng))] = None
    return list(picked.keys())[:count]


def _random_host(net: ipaddress._BaseNetwork, rng: random.Random) -> ipaddress._BaseAddress:
    if net.num_addresses <= 2:
        return net.network_address
    span = net.num_addresses
    offset = rng.randrange(1, span - 1)
    return net.network_address + offset


# ---------------------------------------------------------------------------
# IP 池解析（第三方 / 个人维护的地址列表）
# ---------------------------------------------------------------------------

def is_public_ip(value: str) -> bool:
    """过滤私有、回环、链路本地、组播、保留与未指定地址。"""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def clean_ip_token(token: Any) -> str:
    """清洗单个 IP 片段：去注释、常见前缀、引号与端口。"""
    text = str(token or "").strip()
    if not text:
        return ""
    text = text.split("#")[0].strip()
    text = _PREFIX_RE.sub("", text).strip()
    # [IPv6]:端口必须在剥离方括号之前识别
    match = _IPV6_PORT_RE.match(text)
    if match:
        return match.group(1)
    text = text.strip("\"'`<>(){}[]")
    match = _IPV4_PORT_RE.match(text)
    if match:
        return match.group(1)
    return text


def _collect_json_tokens(value: Any, out: List[str]) -> None:
    """从任意 JSON 结构中递归收集可能是 IP 的字符串。"""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return
    elif isinstance(value, dict):
        hit = False
        for key in _JSON_IP_KEYS:
            if key in value:
                hit = True
                _collect_json_tokens(value[key], out)
        if not hit:
            for item in value.values():
                if isinstance(item, str):
                    out.append(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_json_tokens(item, out)


def parse_ip_pool(text: Optional[str]) -> Dict[str, Any]:
    """
    解析第三方 / 个人维护的 IP 池内容，兼容：

    - 每行一个 IP 或 CIDR，支持逗号/分号/空格分隔与 `#` 注释
    - `IP:端口`、`[IPv6]:端口`、`IP 延迟/ms` 等常见格式
    - JSON 数组、对象（ips/data/list/ipv4/... 字段）与对象数组

    返回 ``{"ips", "v4", "v6", "cidrs", "tokens"}``；
    无效、私有与非公网地址自动忽略，结果保序去重。
    """
    raw = str(text or "").strip()
    result: Dict[str, Any] = {"ips": [], "v4": [], "v6": [], "cidrs": [], "tokens": 0}
    if not raw:
        return result

    tokens: List[str] = []
    if raw.startswith(("{", "[")):
        try:
            _collect_json_tokens(json.loads(raw), tokens)
        except (ValueError, TypeError):
            tokens = []
    if not tokens:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "//")):
                continue
            tokens.extend(re.split(r"[\s,;|]+", line))

    ips: List[str] = []
    cidrs: List[str] = []
    seen_ips: set[str] = set()
    seen_cidrs: set[str] = set()
    for token in tokens:
        cleaned = clean_ip_token(token)
        if not cleaned:
            continue
        try:
            addr = ipaddress.ip_address(cleaned)
        except ValueError:
            try:
                net = ipaddress.ip_network(cleaned, strict=False)
            except ValueError:
                continue
            key = str(net)
            if key not in seen_cidrs:
                seen_cidrs.add(key)
                cidrs.append(key)
            continue
        value = str(addr)
        if value in seen_ips or not is_public_ip(value):
            continue
        seen_ips.add(value)
        ips.append(value)

    result["tokens"] = len(tokens)
    result["ips"] = ips
    result["v4"] = [item for item in ips if ipaddress.ip_address(item).version == 4]
    result["v6"] = [item for item in ips if ipaddress.ip_address(item).version == 6]
    result["cidrs"] = cidrs
    return result


def build_candidate_pool(
    pool_ips: Sequence[str],
    sampled: Sequence[str],
    previous: Sequence[str],
    limit: int,
    *,
    networks: Optional[Sequence[ipaddress._BaseNetwork]] = None,
    strict_cf: bool = False,
) -> List[str]:
    """
    组装本轮测速候选：自定义池 IP 优先 > 上轮优选 > 官方段随机采样。

    `strict_cf=True` 时，自定义池单 IP 也必须落在给定 Cloudflare 网段内。
    """
    if limit <= 0:
        return []
    result: List[str] = []
    seen: set[str] = set()
    for index, group in enumerate((pool_ips, previous, sampled)):
        for raw in group:
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            if index == 0 and strict_cf and networks and not is_cloudflare_ip(value, networks):
                continue
            seen.add(value)
            result.append(value)
            if len(result) >= limit:
                return result
    return result


def format_ip_list(ips: Sequence[str], limit: int = 6) -> str:
    """日志友好的 IP 列表文本。"""
    values = [str(item) for item in (ips or []) if item]
    if not values:
        return "-"
    if len(values) <= limit:
        return ", ".join(values)
    return ", ".join(values[:limit]) + f" …（共 {len(values)} 个）"


# ---------------------------------------------------------------------------
# 域名与解析
# ---------------------------------------------------------------------------

def normalize_domains(text: Optional[Any]) -> List[str]:
    """把多行/逗号分隔的输入规范为去重域名列表（也接受列表输入）。"""
    if isinstance(text, (list, tuple, set)):
        text = "\n".join(str(item) for item in text)
    result: List[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\s,;]+", str(text or "")):
        value = raw.strip().lower()
        if not value or value.startswith("#"):
            continue
        value = re.sub(r"^[a-z]+://", "", value)
        value = value.split("/")[0].split("?")[0]
        if value.count(":") == 1:  # 去掉显式端口
            value = value.split(":")[0]
        value = value.rstrip(".")
        if not _DOMAIN_RE.match(value):
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def parse_doh_answer(payload: Any) -> List[str]:
    """从 DoH JSON 响应中提取 A/AAAA 记录。"""
    if not isinstance(payload, dict):
        return []
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        return []
    result: List[str] = []
    for item in answers:
        if not isinstance(item, dict):
            continue
        try:
            record_type = int(item.get("type"))
        except (TypeError, ValueError):
            continue
        if record_type not in (1, 28):
            continue
        data = str(item.get("data") or "").strip()
        try:
            ipaddress.ip_address(data)
        except ValueError:
            continue
        result.append(data)
    return result


def parse_doh_urls(text: Optional[str]) -> List[str]:
    """解析多行 DoH 地址；为空时返回内置多源列表。"""
    urls: List[str] = []
    for raw in re.split(r"[\s,;]+", str(text or "")):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith(("http://", "https://")) and value not in urls:
            urls.append(value)
    return urls or list(DEFAULT_DOH_URLS)


def classify_domain(
    resolved: Sequence[str],
    networks: Sequence[ipaddress._BaseNetwork],
    *,
    mode: str = DEFAULT_CHECK_MODE,
    fallback: bool = True,
) -> Tuple[bool, str, List[str]]:
    """
    判定域名是否应纳入优选。

    - ``all``：不做任何 DNS 探测，配置的域名一律视为 Cloudflare 站点（默认，零网络依赖）
    - ``system`` / ``doh``：解析出的 IP 是否落在 Cloudflare CDN 网段
    - 判定失败（解析不到 / 不是 CF）时按 ``fallback`` 决定是否仍纳入

    返回 ``(是否纳入, 原因, 命中的 Cloudflare IP)``。
    """
    if mode not in CHECK_MODES or mode == "all":
        return True, "按配置直接纳入（未做 DNS 判定）", []
    cf = cloudflare_ips(resolved, networks)
    if cf:
        return True, f"解析到 Cloudflare CDN IP（{len(cf)} 个）", cf
    if not resolved:
        return bool(fallback), (
            "DNS/DoH 解析失败或无 A 记录" + ("，按配置仍纳入" if fallback else "，已跳过")
        ), []
    return bool(fallback), (
        "解析 IP 不属于 Cloudflare 网段" + ("，按配置仍纳入" if fallback else "，已跳过")
    ), []


async def resolve_system_dns(
    domain: str,
    *,
    timeout: float = 3.0,
    ipv6: bool = False,
) -> List[str]:
    """
    用容器 / 宿主机的系统 DNS 解析域名，不依赖任何 DoH 服务。

    在线程池里执行 getaddrinfo，避免阻塞事件循环；超时或失败返回空列表。
    """
    loop = asyncio.get_running_loop()
    family = socket.AF_UNSPEC if ipv6 else socket.AF_INET

    def _lookup() -> List[str]:
        infos = socket.getaddrinfo(domain, None, family=family, type=socket.SOCK_STREAM)
        found: List[str] = []
        for info in infos:
            value = str(info[4][0])
            if value not in found:
                found.append(value)
        return found

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _lookup), timeout=max(0.5, float(timeout))
        )
    except Exception:  # noqa: BLE001 - DNS 不可用时交由 fallback 处理
        return []


# ---------------------------------------------------------------------------
# 延迟探测
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """单个候选 IP 的探测结果。"""

    ip: str
    latency_ms: float = 0.0
    ok: bool = False
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"ip": self.ip, "latency_ms": round(self.latency_ms, 1), "ok": self.ok, "error": self.error}


async def probe_once(
    ip: str,
    *,
    port: int = 443,
    timeout: float = 2.0,
    use_tls: bool = False,
    sni: str = "",
) -> ProbeResult:
    """对单个 IP 做 TCP（可选 TLS）握手计时。"""
    started = time.perf_counter()
    writer = None
    try:
        if use_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=ip,
                    port=port,
                    ssl=context,
                    server_hostname=sni or None,
                ),
                timeout=timeout,
            )
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host=ip, port=port),
                timeout=timeout,
            )
        elapsed = (time.perf_counter() - started) * 1000
        return ProbeResult(ip=ip, latency_ms=elapsed, ok=True)
    except (asyncio.TimeoutError, OSError, ssl.SSLError) as err:
        return ProbeResult(ip=ip, latency_ms=(time.perf_counter() - started) * 1000, ok=False, error=type(err).__name__)
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 关闭失败不影响测速结果
                pass


async def probe_many(
    ips: Sequence[str],
    *,
    port: int = 443,
    timeout: float = 2.0,
    concurrency: int = 32,
    use_tls: bool = False,
    sni: str = "",
) -> List[ProbeResult]:
    """并发探测一批 IP，返回全部结果（含失败项）。"""
    if not ips:
        return []
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _worker(ip: str) -> ProbeResult:
        async with semaphore:
            return await probe_once(ip, port=port, timeout=timeout, use_tls=use_tls, sni=sni)

    return list(await asyncio.gather(*(_worker(ip) for ip in ips)))


def pick_fastest(results: Sequence[ProbeResult], limit: int) -> List[ProbeResult]:
    """按延迟升序取前 limit 个可用结果。"""
    usable = [item for item in results if item.ok]
    usable.sort(key=lambda item: item.latency_ms)
    return usable[: max(0, limit)]


def format_latency(value: float) -> str:
    """把毫秒值格式化成易读文本。"""
    if value <= 0:
        return "-"
    if value < 10:
        return f"{value:.1f} ms"
    return f"{value:.0f} ms"


# ---------------------------------------------------------------------------
# DNS static 记录
# ---------------------------------------------------------------------------

def build_comment(domain: str, prefix: str = "cfopt") -> str:
    """生成带前缀的备注，便于精确清理自己的记录。"""
    cleaned = (prefix or "cfopt").strip() or "cfopt"
    return f"{cleaned}:{domain}"


def build_record(domain: str, address: str, ttl: str = "10m", prefix: str = "cfopt") -> Dict[str, str]:
    """构造 RouterOS /ip/dns/static 的写入字段（IPv6 自动用 AAAA）。"""
    try:
        record_type = "AAAA" if ipaddress.ip_address(address).version == 6 else "A"
    except ValueError:
        record_type = "A"
    return {
        "name": domain,
        "address": address,
        "type": record_type,
        "ttl": str(ttl or "10m"),
        "comment": build_comment(domain, prefix),
    }
