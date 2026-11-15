"""Reseed 辅种插件的宿主交互层。

封装 Reseed 服务 HTTP 接口、MoviePilot 站点配置读取、种子下载与下载器添加。
纯逻辑（索引/解析/复核）在 core.py，本模块只在运行期被调用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.db.oper.site import SiteOper
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import DownloaderHelper

from . import core

LOG_TAG = "[ReseedCrossSeed] "
TOKEN_TTL = 20 * 3600


@dataclass
class MpSite:
    """一个 MoviePilot 站点配置的最小可用视图。"""

    name: str
    url: str
    cookie: str = ""
    ua: str = ""
    proxy: bool = False


@dataclass
class ServiceResult:
    ok: bool
    message: str
    data: Any = None


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _response_json(response: Any) -> Any:
    """安全提取 JSON 响应体。"""
    if response is None:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - 登录页/HTML 不算 JSON
        return None


def load_mp_sites() -> dict[str, MpSite]:
    """读取 MoviePilot 站点管理中的启用站点，按站点名小写索引。"""
    result: dict[str, MpSite] = {}
    try:
        sites = SiteOper().list_order_by_pri() or []
    except Exception as err:  # noqa: BLE001
        logger.error(f"{LOG_TAG}读取站点列表失败：{err}")
        return result
    for site in sites:
        name = str(_get(site, "name", "") or "").strip()
        if not name or not bool(_get(site, "is_active", True)):
            continue
        url = str(_get(site, "url", "") or "").strip() or str(_get(site, "domain", "") or "").strip()
        if not url:
            continue
        result[name.lower()] = MpSite(
            name=name,
            url=url,
            cookie=str(_get(site, "cookie", "") or ""),
            ua=str(_get(site, "ua", "") or ""),
            proxy=bool(_get(site, "proxy", False)),
        )
    return result


def _proxy_kwargs(enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {}
    try:
        proxy = getattr(settings, "PROXY", None)
    except Exception:  # noqa: BLE001
        proxy = None
    return {"proxies": proxy} if isinstance(proxy, dict) else {}


class ReseedClient:
    """Reseed 服务客户端：登录取 token、拉取站点、上传索引拿匹配结果。"""

    def __init__(
        self,
        api: str | None,
        username: str,
        password: str,
        timeout: int = 30,
        proxy: bool = False,
    ) -> None:
        self.api = core.normalize_api_url(api)
        self.username = username or ""
        self.password = password or ""
        self.timeout = max(5, int(timeout or 30))
        self.proxy = bool(proxy)
        self._token = ""
        self._token_at = 0.0

    def _client(self, token: str = "") -> RequestUtils:
        # 不设置 Content-Type：upload_json 走 multipart，交给 requests 自动生成边界
        headers = {
            "User-Agent": core.DEFAULT_UA,
            "Accept": "application/json",
            "referer": self.api,
        }
        if token:
            # 后端 request_loader 使用源码里的 "Bearar" 前缀
            headers["Authorization"] = f"Bearar {token}"
        return RequestUtils(
            headers=headers,
            timeout=self.timeout,
            **_proxy_kwargs(self.proxy),
        )

    def login(self) -> ServiceResult:
        if not self.username or not self.password:
            return ServiceResult(False, "还没填 Reseed 账号或密码")
        try:
            response = self._client().post_res(
                self.api + "login",
                data={"username": self.username, "password": self.password},
            )
        except Exception as err:  # noqa: BLE001
            return ServiceResult(False, f"连接 Reseed 服务失败：{type(err).__name__}")
        if response is None:
            return ServiceResult(False, "连接 Reseed 服务超时或网络不可达")
        status = int(getattr(response, "status_code", 0) or 0)
        body = _response_json(response) or {}
        if status in (200, 201) and isinstance(body, dict) and body.get("success") and body.get("token"):
            self._token = str(body["token"])
            self._token_at = time.time()
            return ServiceResult(True, "登录成功", self._token)
        message = str(body.get("msg") or "") if isinstance(body, dict) else ""
        if status == 429:
            return ServiceResult(False, "Reseed 服务请求过于频繁，稍后再试")
        return ServiceResult(False, message or f"登录失败（HTTP {status}），检查账号密码")

    def _ensure_token(self) -> ServiceResult:
        if self._token and time.time() - self._token_at < TOKEN_TTL:
            return ServiceResult(True, "", self._token)
        return self.login()

    def sites_info(self) -> ServiceResult:
        auth = self._ensure_token()
        if not auth.ok:
            return auth
        try:
            response = self._client(auth.data).get_res(self.api + "sites_info")
        except Exception as err:  # noqa: BLE001
            return ServiceResult(False, f"读取站点列表失败：{type(err).__name__}")
        if response is None:
            return ServiceResult(False, "读取站点列表超时")
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            self._token = ""
            return ServiceResult(False, "登录已过期，请重新检测")
        sites = core.parse_sites_info(_response_json(response))
        return ServiceResult(True, "读取站点列表成功", sites) if sites else ServiceResult(
            False, f"站点列表为空或格式异常（HTTP {status}）"
        )

    def upload(self, index: dict[str, Any], site_names: list[str]) -> ServiceResult:
        auth = self._ensure_token()
        if not auth.ok:
            return auth
        if not site_names:
            return ServiceResult(False, "没有可提交的站点：先在设置里填站点映射")
        payload = core.encode_index(index)
        try:
            response = self._client(auth.data).post_res(
                self.api + "upload_json",
                data={"sites": ",".join(site_names)},
                files={"file": ("index.json", payload, "application/json")},
            )
        except Exception as err:  # noqa: BLE001
            return ServiceResult(False, f"上传索引失败：{type(err).__name__}")
        if response is None:
            return ServiceResult(False, "上传索引超时或网络不可达")
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            self._token = ""
            return ServiceResult(False, "登录已过期，重试一次即可")
        if status == 429:
            return ServiceResult(False, "服务端限流（每小时 5 次 / 每天 10 次），请稍后再试")
        body = _response_json(response)
        if not isinstance(body, dict) or not body.get("success"):
            message = str(body.get("msg") or "") if isinstance(body, dict) else ""
            return ServiceResult(False, message or f"查询失败（HTTP {status}）")
        return ServiceResult(True, "查询完成", core.parse_upload_result(body))


def download_torrent(
    site: MpSite,
    binding: core.SiteBinding,
    torrent_id: str,
    timeout: int = 30,
) -> bytes | None:
    """按站点配置下载 .torrent，返回内容；非种子内容一律视为失败。"""
    url = core.build_torrent_url(site.url, binding.download_tpl, torrent_id, binding.passkey)
    if not url:
        return None
    ua = site.ua or str(getattr(settings, "USER_AGENT", "") or "") or core.DEFAULT_UA
    try:
        client = RequestUtils(
            headers={"User-Agent": ua, "Accept": "*/*", "referer": site.url},
            cookies=site.cookie or None,
            timeout=timeout,
            **_proxy_kwargs(site.proxy),
        )
        response = client.get_res(url)
    except Exception as err:  # noqa: BLE001
        logger.warning(f"{LOG_TAG}下载种子失败 {binding.reseed_name}-{torrent_id}：{type(err).__name__}")
        return None
    if response is None:
        return None
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in (200, 201, 202):
        logger.warning(f"{LOG_TAG}下载种子返回 HTTP {status}：{binding.reseed_name}-{torrent_id}")
        return None
    content = bytes(getattr(response, "content", b"") or b"")
    return content if core.is_torrent_content(content) else None


class DownloaderGateway:
    """把辅种种子加到目标下载器，并提供跨下载器去重所需的 hash 集合。"""

    def __init__(self, target_name: str = "") -> None:
        self.target_name = (target_name or "").strip()
        self.service: Any = None
        self.name = ""

    def resolve(self) -> ServiceResult:
        try:
            filters = [self.target_name] if self.target_name else None
            services = DownloaderHelper().get_services(name_filters=filters) or {}
        except Exception as err:  # noqa: BLE001
            return ServiceResult(False, f"读取下载器失败：{type(err).__name__}")
        for name, service in services.items():
            instance = getattr(service, "instance", None)
            if instance is None:
                continue
            try:
                if instance.is_inactive():
                    continue
            except Exception:  # noqa: BLE001
                pass
            self.service, self.name = service, str(name)
            return ServiceResult(True, f"下载器 {name} 已连接", str(name))
        return ServiceResult(False, "没有可用的下载器，检查 MoviePilot 下载器设置")

    @property
    def instance(self) -> Any:
        return getattr(self.service, "instance", None)

    def _is_qb(self) -> bool:
        dtype = str(getattr(self.service, "type", "") or "").lower()
        if dtype:
            return "qb" in dtype
        return "qbittorrent" in type(self.instance).__name__.lower()

    def all_hashes(self) -> set[str]:
        instance = self.instance
        if instance is None:
            return set()
        try:
            result = instance.get_torrents()
            torrents = result[0] if isinstance(result, tuple) else (result or [])
            return {
                str(_get(item, "hash", "") or _get(item, "hashString", "") or "").lower()
                for item in torrents
            } - {""}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}读取下载器任务失败：{err}")
            return set()

    def add(
        self,
        content: bytes,
        save_path: str,
        tag: str,
        name: str,
        skip_checking: bool = True,
        auto_start: bool = True,
    ) -> str | None:
        instance = self.instance
        if instance is None:
            return None
        tags = [tag] if tag else []
        try:
            if self._is_qb():
                kwargs: dict[str, Any] = {
                    "content": content,
                    "download_dir": save_path,
                    "is_paused": True,
                    "tag": tags or None,
                }
                if skip_checking:
                    kwargs["is_skip_checking"] = True
                result = instance.add_torrent(**kwargs)
                if isinstance(result, tuple):
                    ok, hashes = bool(result[0]), [str(x) for x in (result[1] or [])]
                else:
                    ok, hashes = bool(result), []
                if not ok and not hashes:
                    return None
                torrent_hash = hashes[0] if hashes else self._find_new_hash(name, save_path, tag)
            else:
                torrent = instance.add_torrent(
                    content=content,
                    download_dir=save_path,
                    is_paused=True,
                    labels=tags or None,
                )
                torrent_hash = str(_get(torrent, "hashString", "") or "")
            if not torrent_hash:
                return None
            if not self._is_qb() or not skip_checking:
                try:
                    instance.recheck_torrents(ids=[torrent_hash])
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG}触发重新校验失败：{err}")
            if auto_start:
                try:
                    instance.start_torrents(ids=[torrent_hash])
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG}启动做种失败：{err}")
            return torrent_hash
        except Exception as err:  # noqa: BLE001
            logger.error(f"{LOG_TAG}添加辅种种子失败：{err}")
            return None

    def _find_new_hash(self, name: str, save_path: str, tag: str) -> str | None:
        """旧版下载器不返回新增 hash 时，按名称与路径反查。"""
        instance = self.instance
        try:
            result = instance.get_torrents(tags=[tag]) if tag else instance.get_torrents()
            torrents = result[0] if isinstance(result, tuple) else (result or [])
            for item in torrents:
                if (_get(item, "name") == name
                        and str(_get(item, "save_path", "") or "").rstrip("/") == save_path.rstrip("/")):
                    return str(_get(item, "hash", "") or "") or None
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}反查新增种子失败：{err}")
        return None
