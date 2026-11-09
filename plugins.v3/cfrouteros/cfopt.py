"""Cloudflare 优选 IP 的纯逻辑与探测实现。

职责边界：
- 网段解析与归属判断（内置官方段 + 可刷新）
- DoH 解析：判断域名是否真的指向 Cloudflare
- TCP/TLS 握手延迟探测与排序
- DNS static 记录的字段构造

除 asyncio 探测外均为纯函数，便于离线单测。
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import re
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
CF_V4_LIST_URL = "https://www.cloudflare.com/ips-v4"
CF_V6_LIST_URL = "https://www.cloudflare.com/ips-v6"

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


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
# 域名与 DoH
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
