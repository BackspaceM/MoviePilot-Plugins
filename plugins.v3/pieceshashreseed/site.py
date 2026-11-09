"""站点侧能力：站点配置读取、pieces-hash 支持检测、批量查询与种子下载。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from app.db.oper.site import SiteOper
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

from .core import (
    DEFAULT_API_PATHS,
    DEFAULT_DOWNLOAD_TPL,
    PROBE_PIECES_HASH,
    SiteCredential,
    build_api_url,
    build_torrent_url,
    chunked,
    classify_probe,
    is_torrent_content,
    normalize_site_url,
    parse_pieces_response,
)

LOG_TAG = "[PiecesHashReseed] "


@dataclass
class SiteConfig:
    """一个可辅种站点的完整运行配置。"""

    name: str
    url: str
    cookie: str = ""
    ua: str = ""
    proxy: bool = False
    passkey: str = ""
    api_path: str | None = None
    gap: int = 1
    download_tpl: str = DEFAULT_DOWNLOAD_TPL


@dataclass
class SiteSupportResult:
    """站点 pieces-hash 接口探测结果，用于配置页展示与自动过滤。"""

    name: str
    supported: bool
    api_path: str | None
    reason: str
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        """转成可持久化、可渲染的普通字典。"""
        return asdict(self)


def site_options() -> list[dict[str, str]]:
    """返回配置页站点下拉项，使用站点名作为稳定 value。"""
    options: list[dict[str, str]] = []
    try:
        sites = SiteOper().list_order_by_pri() or []
    except Exception as err:  # noqa: BLE001 - 宿主数据读取失败不应阻断配置页
        logger.error(f"{LOG_TAG}读取站点列表失败：{err}")
        return options
    for site in sites:
        name = str(getattr(site, "name", "") or "").strip()
        if not name:
            continue
        domain = str(getattr(site, "domain", "") or "").strip()
        options.append({"title": f"{name}（{domain}）" if domain else name, "value": name})
    return options


def load_sites(
    credentials: dict[str, SiteCredential],
    selected: Sequence[str],
    support_cache: dict[str, Any] | None = None,
) -> list[SiteConfig]:
    """合并宿主站点数据、用户凭据与探测缓存，生成可用站点配置列表。"""
    wanted = set(selected or [])
    cache = support_cache or {}
    configs: list[SiteConfig] = []
    try:
        sites = SiteOper().list_order_by_pri() or []
    except Exception as err:  # noqa: BLE001
        logger.error(f"{LOG_TAG}读取站点列表失败：{err}")
        return configs
    for site in sites:
        name = str(getattr(site, "name", "") or "").strip()
        if not name or (wanted and name not in wanted):
            continue
        if not bool(getattr(site, "is_active", True)):
            continue
        credential = credentials.get(name)
        if not credential:
            continue
        cached = cache.get(name) if isinstance(cache.get(name), dict) else {}
        url = normalize_site_url(
            str(getattr(site, "url", "") or "") or str(getattr(site, "domain", "") or "")
        )
        if not url:
            logger.warning(f"{LOG_TAG}站点 {name} 缺少有效域名，跳过")
            continue
        configs.append(
            SiteConfig(
                name=name,
                url=url,
                cookie=str(getattr(site, "cookie", "") or ""),
                ua=str(getattr(site, "ua", "") or ""),
                proxy=bool(getattr(site, "proxy", False)),
                passkey=credential.passkey,
                api_path=credential.api_path or cached.get("api_path"),
                gap=credential.gap,
                download_tpl=credential.download_tpl,
            )
        )
    return configs


def _client_kwargs(site: SiteConfig, timeout: int) -> dict[str, Any]:
    """构造站点请求参数，统一处理 UA、Cookie、代理与超时。"""
    ua = site.ua or str(getattr(settings, "USER_AGENT", "") or "") or "MoviePilot/PiecesHashReseed"
    proxy_setting = getattr(settings, "PROXY", None) if site.proxy else None
    proxies = proxy_setting if isinstance(proxy_setting, dict) else None
    return {
        "headers": {
            "User-Agent": ua,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "referer": site.url,
        },
        "cookies": site.cookie or None,
        "proxies": proxies,
        "timeout": timeout,
    }


def _response_body(response: Any) -> tuple[int, str, Any]:
    """提取状态码、Content-Type 与 JSON/文本响应体。"""
    status = int(getattr(response, "status_code", 0) or 0)
    content_type = ""
    try:
        content_type = str(response.headers.get("Content-Type", "") or "")
    except Exception:  # noqa: BLE001
        pass
    body: Any = None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        try:
            body = str(getattr(response, "text", "") or "")[:1000]
        except Exception:  # noqa: BLE001
            body = None
    return status, content_type, body


def _candidate_paths(site: SiteConfig) -> list[str]:
    """优先使用已探测/自定义路径，再依次尝试内置候选路径。"""
    candidates = [site.api_path] if site.api_path else []
    candidates.extend(DEFAULT_API_PATHS)
    return list(dict.fromkeys(path for path in candidates if path))


def probe_site(site: SiteConfig, timeout: int = 15) -> SiteSupportResult:
    """探测站点是否支持 pieces-hash 接口，并确定可用接口路径。

    依次尝试候选路径：先 POST 一个不存在的 pieces_hash 观察响应结构，
    再以 GET 405 作为接口存在的补充判据（NexusPHP 常见行为）。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {"passkey": site.passkey, "pieces_hash": [PROBE_PIECES_HASH]}
    last_reason = "未找到可用的 pieces-hash 接口"
    for path in _candidate_paths(site):
        url = build_api_url(site.url, path)
        if not url:
            continue
        safe_url = url.split("?", 1)[0]
        try:
            client = RequestUtils(**_client_kwargs(site, timeout))
            response = client.post_res(url, json=payload)
            if response is not None:
                status, content_type, body = _response_body(response)
                supported, reason = classify_probe(status, content_type, body)
                if supported:
                    logger.info(f"{LOG_TAG}站点 {site.name} 支持辅种：{safe_url}（{reason}）")
                    return SiteSupportResult(site.name, True, path, reason, now)
                last_reason = f"{path}：{reason}"
            else:
                last_reason = f"{path}：请求失败或超时"

            # GET 返回 405 通常说明接口存在，仅接受 POST
            get_response = RequestUtils(**_client_kwargs(site, timeout)).get_res(
                url, allow_redirects=False
            )
            if get_response is not None and int(getattr(get_response, "status_code", 0) or 0) == 405:
                logger.info(f"{LOG_TAG}站点 {site.name} GET 探测命中 405：{safe_url}")
                return SiteSupportResult(site.name, True, path, "接口存在（GET 返回 405）", now)
        except Exception as err:  # noqa: BLE001 - 单站点异常不影响其它站点
            last_reason = f"{path}：{type(err).__name__}"
            logger.warning(f"{LOG_TAG}站点 {site.name} 探测异常 {safe_url}：{err}")
    logger.warning(f"{LOG_TAG}站点 {site.name} 不支持辅种：{last_reason}")
    return SiteSupportResult(site.name, False, None, last_reason, now)


def probe_all(
    sites: Sequence[SiteConfig],
    timeout: int = 15,
    progress: Callable[[SiteSupportResult], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """批量探测站点并把结果回调给调用方（用于实时写缓存）。"""
    results: dict[str, dict[str, Any]] = {}
    for site in sites:
        result = probe_site(site, timeout)
        results[site.name] = result.to_dict()
        if progress:
            progress(result)
    return results


def query_pieces(
    site: SiteConfig,
    pieces_hashes: Sequence[str],
    batch_size: int = 200,
    timeout: int = 20,
) -> dict[str, str]:
    """按批次向站点查询本地 pieces_hash 对应的站点种子 ID。"""
    if not site.api_path:
        return {}
    url = build_api_url(site.url, site.api_path)
    if not url:
        return {}
    matched: dict[str, str] = {}
    for batch in chunked(list(pieces_hashes), batch_size):
        payload = {"passkey": site.passkey, "pieces_hash": list(batch)}
        try:
            response = RequestUtils(**_client_kwargs(site, timeout)).post_res(url, json=payload)
            if response is None:
                logger.warning(f"{LOG_TAG}站点 {site.name} 查询失败：请求无响应")
                continue
            status = int(getattr(response, "status_code", 0) or 0)
            if status >= 400:
                logger.warning(f"{LOG_TAG}站点 {site.name} 查询返回 HTTP {status}")
                continue
            _, _, body = _response_body(response)
            matched.update(parse_pieces_response(body))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}站点 {site.name} 查询异常：{err}")
        if site.gap:
            time.sleep(site.gap)
    return matched


def download_torrent(site: SiteConfig, torrent_id: str, timeout: int = 30) -> bytes | None:
    """下载站点种子文件内容，校验其确为可解析的 torrent。"""
    try:
        url = build_torrent_url(site.url, site.download_tpl, torrent_id, site.passkey)
    except (KeyError, IndexError, ValueError) as err:
        logger.error(f"{LOG_TAG}站点 {site.name} 下载模板无效：{err}")
        return None
    if not url:
        return None
    safe_url = url.split("?", 1)[0]
    try:
        # 种子下载使用默认表单/二进制请求头，不复用 JSON 请求头
        kwargs = _client_kwargs(site, timeout)
        kwargs["headers"] = {
            "User-Agent": kwargs["headers"]["User-Agent"],
            "Accept": "*/*",
        }
        response = RequestUtils(**kwargs).get_res(url)
        if response is None:
            logger.warning(f"{LOG_TAG}站点 {site.name} 种子 {torrent_id} 下载失败：无响应")
            return None
        status = int(getattr(response, "status_code", 0) or 0)
        content = getattr(response, "content", None)
        if status != 200 or not is_torrent_content(content):
            logger.warning(f"{LOG_TAG}站点 {site.name} 种子 {torrent_id} 不是有效种子（HTTP {status}）")
            return None
        return content
    except Exception as err:  # noqa: BLE001
        logger.warning(f"{LOG_TAG}站点 {site.name} 种子 {torrent_id} 下载异常 {safe_url}：{err}")
        return None


def summarize_support(results: dict[str, Any]) -> tuple[list[str], list[str]]:
    """把探测结果拆成可用站点名列表与不可用原因文本列表。"""
    supported = [name for name, item in (results or {}).items() if isinstance(item, dict) and item.get("supported")]
    unsupported = [
        f"{name}：{item.get('reason', '未知原因')}"
        for name, item in (results or {}).items()
        if isinstance(item, dict) and not item.get("supported")
    ]
    return supported, unsupported
