"""播放感知调度 —— 外部系统客户端。

包含：媒体服务器会话读取（Emby / Jellyfin / Plex 归一化）、
下载器限速（qBittorrent / Transmission）、RouterOS 简单队列联动。
所有客户端均通过 app.sdk.network 的 AsyncRequestUtils 发起请求，
并统一做超时、异常收敛与试运行处理。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .core import (
    MediaServer,
    Session,
    _ip_is_private,
    normalize_path,
)


def normalize_base_url(host: str, port: int = 443, use_ssl: bool = True) -> str:
    """把用户输入的地址规范化为 http(s)://host:port 形式。"""
    text = str(host or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = ("https://" if use_ssl else "http://") + text
    text = text.rstrip("/")
    if "://" in text and (":" not in text.split("://", 1)[1].split("/")[0]):
        text = f"{text}:{port}"
    return text


def basic_auth_header(username: str, password: str) -> Dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


# ---------------------------------------------------------------------------
# 媒体服务器会话读取
# ---------------------------------------------------------------------------

class MediaClient:
    """读取单个媒体服务器的活动会话并归一化为 Session。"""

    def __init__(self, server: MediaServer, client: Any, timeout: int = 10, dry_run: bool = False):
        self.server = server
        self._client = client
        self._timeout = timeout
        self._dry_run = dry_run
        self._base = normalize_base_url(server.url, 443 if server.type == "plex" else 8096)

    async def check(self) -> Tuple[bool, str]:
        try:
            if self.server.is_plex:
                resp = await self._client.request("GET", f"{self._base}/status/sessions",
                                                  headers={"X-Plex-Token": self.server.apikey},
                                                  timeout=self._timeout)
            else:
                resp = await self._client.request("GET", f"{self._base}/emby/Sessions",
                                                  params={"api_key": self.server.apikey},
                                                  timeout=self._timeout)
            ok = resp is not None and int(getattr(resp, "status_code", 0) or 0) in (200, 204)
            return ok, "连接正常" if ok else f"HTTP {getattr(resp, 'status_code', '?')}"
        except Exception as err:  # noqa: BLE001
            return False, f"连接失败：{type(err).__name__} {err}"

    async def get_sessions(self) -> List[Session]:
        try:
            if self.server.is_plex:
                return await self._get_plex()
            return await self._get_emby()
        except Exception as err:  # noqa: BLE001
            return []

    async def _get_emby(self) -> List[Session]:
        resp = await self._client.request(
            "GET", f"{self._base}/emby/Sessions",
            params={"api_key": self.server.apikey}, timeout=self._timeout,
        )
        if resp is None or int(getattr(resp, "status_code", 0) or 0) != 200:
            return []
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return []
        out: List[Session] = []
        for raw in payload if isinstance(payload, list) else []:
            item = (raw or {}).get("NowPlayingItem")
            if not item:
                continue
            play = raw.get("PlayState") or {}
            tc = raw.get("TranscodingInfo")
            is_tc = bool(tc) and not (tc or {}).get("IsVideoDirect", True)
            title = item.get("Name", "")
            sub = ""
            if item.get("Type") == "Episode":
                sub = " · ".join([p for p in (item.get("SeriesName"),
                          (f"S{item.get('ParentIndexNumber')}E{item.get('IndexNumber')}" if item.get("IndexNumber") else "")) if p])
            paths = [item["Path"]] if item.get("Path") else []
            out.append(Session(
                server=self.server.name,
                user=str(raw.get("UserName", "")),
                client=str(raw.get("Client", "")),
                device=str(raw.get("DeviceName", "")),
                title=title, sub_title=sub,
                is_paused=bool(play.get("IsPaused", False)),
                is_transcoding=is_tc,
                is_remote=not _ip_is_private(str(raw.get("RemoteEndPoint", ""))),
                progress=float((play.get("PositionTicks") or 0)) / float((item.get("RunTimeTicks") or 1)) if item.get("RunTimeTicks") else 0.0,
                paths=[self.server.map_path(p) for p in paths],
            ))
        return out

    async def _get_plex(self) -> List[Session]:
        resp = await self._client.request(
            "GET", f"{self._base}/status/sessions",
            headers={"X-Plex-Token": self.server.apikey}, timeout=self._timeout,
        )
        if resp is None or int(getattr(resp, "status_code", 0) or 0) != 200:
            return []
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return []
        out: List[Session] = []
        container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
        for raw in container.get("Video", []) if isinstance(container, dict) else []:
            player = raw.get("Player") or {}
            sess = raw.get("session") or {}
            is_remote = str(sess.get("location", "")).lower() == "wan" or str(player.get("local", "1")) == "0"
            decision = str(raw.get("TranscodeDecision", "")).lower()
            is_tc = decision == "transcode"
            title = raw.get("title", "")
            grand = raw.get("grandparentTitle", "")
            sub = grand if grand else ""
            paths: List[str] = []
            for part in raw.get("Media", []) if isinstance(raw.get("Media"), list) else []:
                for pp in part.get("Part", []) if isinstance(part.get("Part"), list) else []:
                    if pp.get("file"):
                        paths.append(self.server.map_path(pp["file"]))
            out.append(Session(
                server=self.server.name,
                user=str((raw.get("User") or {}).get("title", "")),
                client=str(player.get("title", "")),
                device=str(player.get("device", "")),
                title=title, sub_title=sub,
                is_paused=str(player.get("state", "")).lower() == "paused",
                is_transcoding=is_tc,
                is_remote=is_remote,
                progress=float(raw.get("viewOffset", 0) or 0) / float((raw.get("duration") or 1)) if raw.get("duration") else 0.0,
                paths=paths,
            ))
        return out


# ---------------------------------------------------------------------------
# 下载器限速
# ---------------------------------------------------------------------------

@dataclass
class LimitResult:
    ok: bool
    message: str
    details: List[str] = field(default_factory=list)


class DownloaderClient:
    """对单个下载器设置全局上传/下载限速。"""

    def __init__(self, dl: Any, client: Any, timeout: int = 10, dry_run: bool = False):
        self._dl = dl
        self._client = client
        self._timeout = timeout
        self._dry_run = dry_run
        self._base = normalize_base_url(dl.url, 8080 if dl.is_qbittorrent else 9091)

    async def check(self) -> Tuple[bool, str]:
        try:
            if self._dl.is_qbittorrent:
                resp = await self._client.request(
                    "GET", f"{self._base}/api/v2/app/version",
                    auth=(self._dl.username, self._dl.password), timeout=self._timeout)
            else:
                sid = await self._fetch_tr_session_id()
                if sid is None:
                    return False, "无法获取 Transmission 会话 ID"
                resp = await self._client.request(
                    "POST", f"{self._base}/transmission/rpc",
                    json={"method": "session-get", "arguments": {"fields": ["version"]}},
                    headers={"X-Transmission-Session-Id": sid},
                    auth=(self._dl.username, self._dl.password), timeout=self._timeout)
            ok = resp is not None and int(getattr(resp, "status_code", 0) or 0) in (200, 409)
            return ok, "连接正常" if ok else f"HTTP {getattr(resp, 'status_code', '?')}"
        except Exception as err:  # noqa: BLE001
            return False, f"连接失败：{type(err).__name__} {err}"

    async def _fetch_tr_session_id(self) -> Optional[str]:
        try:
            resp = await self._client.request(
                "POST", f"{self._base}/transmission/rpc",
                json={"method": "session-get"},
                auth=(self._dl.username, self._dl.password), timeout=self._timeout)
            if resp is None:
                return None
            if int(getattr(resp, "status_code", 0) or 0) == 200:
                return ""
            # 409 时从响应头取 session id
            headers = getattr(resp, "headers", {}) or {}
            return headers.get("X-Transmission-Session-Id") or headers.get("x-transmission-session-id")
        except Exception:  # noqa: BLE001
            return None

    async def get_limits(self) -> Dict[str, int]:
        try:
            if self._dl.is_qbittorrent:
                resp = await self._client.request(
                    "GET", f"{self._base}/api/v2/transfer/info",
                    auth=(self._dl.username, self._dl.password), timeout=self._timeout)
                if resp is None or int(getattr(resp, "status_code", 0) or 0) != 200:
                    return {}
                data = resp.json()
                return {"dl": int(data.get("dl_rate_limit", 0)), "up": int(data.get("up_rate_limit", 0))}
            sid = await self._fetch_tr_session_id()
            if sid is None:
                return {}
            resp = await self._client.request(
                "POST", f"{self._base}/transmission/rpc",
                json={"method": "session-get", "arguments": {"fields": ["speed-limit-down", "speed-limit-up"]}},
                headers={"X-Transmission-Session-Id": sid},
                auth=(self._dl.username, self._dl.password), timeout=self._timeout)
            if resp is None or int(getattr(resp, "status_code", 0) or 0) != 200:
                return {}
            args = (resp.json().get("arguments") or {}) if resp else {}
            return {"dl": int((args.get("speed-limit-down") or 0)) * 1000,
                    "up": int((args.get("speed-limit-up") or 0)) * 1000}
        except Exception:  # noqa: BLE001
            return {}

    async def set_limits(self, qb_dl: int, qb_up: int, tr_dl: int, tr_up: int) -> LimitResult:
        details: List[str] = []
        if self._dry_run:
            unit = "字节/秒" if self._dl.is_qbittorrent else "KB/秒"
            val = qb_dl if self._dl.is_qbittorrent else tr_dl
            details.append(f"试运行：将设置下载={val}{unit}，上传={qb_up if self._dl.is_qbittorrent else tr_up}{unit}")
            return LimitResult(ok=True, message="试运行未实际修改", details=details)
        try:
            if self._dl.is_qbittorrent:
                for ep, val in (("setDownloadLimit", qb_dl), ("setUploadLimit", qb_up)):
                    resp = await self._client.request(
                        "POST", f"{self._base}/api/v2/transfer/{ep}",
                        data={"limit": val},
                        auth=(self._dl.username, self._dl.password), timeout=self._timeout)
                    if resp is None or int(getattr(resp, "status_code", 0) or 0) not in (200, 409):
                        details.append(f"{ep} 失败 HTTP {getattr(resp, 'status_code', '?')}")
                ok = not details
            else:
                sid = await self._fetch_tr_session_id()
                if sid is None:
                    return LimitResult(ok=False, message="无法获取会话 ID", details=["Transmission 认证失败"])
                resp = await self._client.request(
                    "POST", f"{self._base}/transmission/rpc",
                    json={"method": "session-set", "arguments": {
                        "speed-limit-down": tr_dl, "speed-limit-down-enabled": tr_dl > 0,
                        "speed-limit-up": tr_up, "speed-limit-up-enabled": tr_up > 0,
                    }},
                    headers={"X-Transmission-Session-Id": sid},
                    auth=(self._dl.username, self._dl.password), timeout=self._timeout)
                ok = resp is not None and int(getattr(resp, "status_code", 0) or 0) == 200
                if not ok:
                    details.append(f"session-set 失败 HTTP {getattr(resp, 'status_code', '?')}")
            if ok:
                details.append(f"已设置下载/上传限速（{'字节/秒' if self._dl.is_qbittorrent else 'KB/秒'}）")
            return LimitResult(ok=ok, message="成功" if ok else "失败", details=details)
        except Exception as err:  # noqa: BLE001
            return LimitResult(ok=False, message=f"异常：{type(err).__name__} {err}", details=[str(err)])

    async def reset_limits(self) -> LimitResult:
        """恢复为不限速（0）。"""
        return await self.set_limits(0, 0, 0, 0)

    async def tag_playing(self, paths: List[str], tag: str, remove: bool = False) -> LimitResult:
        """把正在播放文件对应的种子打上/移除保护标签（仅 qBittorrent 支持）。"""
        if not self._dl.is_qbittorrent:
            return LimitResult(ok=True, message="Transmission 暂不支持路径级保护标签", details=[])
        if not paths:
            return LimitResult(ok=True, message="无在播路径", details=[])
        try:
            resp = await self._client.request(
                "GET", f"{self._base}/api/v2/torrents/info",
                auth=(self._dl.username, self._dl.password), timeout=self._timeout)
            if resp is None or int(getattr(resp, "status_code", 0) or 0) != 200:
                return LimitResult(ok=False, message="获取种子列表失败", details=[])
            torrents = resp.json()
            matched = []
            for t in torrents if isinstance(torrents, list) else []:
                save = normalize_path(t.get("save_path", "") or "")
                content = normalize_path(t.get("content_path", "") or save)
                for p in paths:
                    np = normalize_path(p)
                    if content == np or content.startswith(np + "/") or np.startswith(save + "/") or save == np:
                        matched.append(t.get("hash"))
                        break
            if not matched:
                return LimitResult(ok=True, message="未匹配到在播种子", details=[])
            ep = "removeTags" if remove else "addTags"
            resp = await self._client.request(
                "POST", f"{self._base}/api/v2/torrents/{ep}",
                data={"hashes": "|".join(matched), "tags": tag},
                auth=(self._dl.username, self._dl.password), timeout=self._timeout)
            ok = resp is not None and int(getattr(resp, "status_code", 0) or 0) in (200, 409)
            return LimitResult(ok=ok, message=f"{'移除' if remove else '添加'}保护标签 {tag} 到 {len(matched)} 个种子",
                               details=[])
        except Exception as err:  # noqa: BLE001
            return LimitResult(ok=False, message=f"标签操作异常：{err}", details=[])


# ---------------------------------------------------------------------------
# RouterOS 简单队列联动（复用 CFRouterOS 的 REST 连接模式）
# ---------------------------------------------------------------------------

class RouterOSClient:
    """在 RouterOS 上确保/移除一条简单队列，用于观影时段限速。"""

    def __init__(self, address: str, username: str, password: str,
                 client: Any = None, queue_name: str = "playback-limit",
                 timeout: int = 10, dry_run: bool = False):
        self._address = address
        self._username = username
        self._password = password
        self._client = client
        self._queue_name = queue_name
        self._timeout = timeout
        self._dry_run = dry_run
        self._base = normalize_base_url(address, 443, True)

    def _headers(self) -> Dict[str, str]:
        return basic_auth_header(self._username, self._password)

    async def check(self) -> Tuple[bool, str]:
        try:
            resp = await self._request("GET", "/rest/system/identity")
            ok = resp is not None and int(getattr(resp, "status_code", 0) or 0) == 200
            return ok, "RouterOS 连接正常" if ok else f"HTTP {getattr(resp, 'status_code', '?')}"
        except Exception as err:  # noqa: BLE001
            return False, f"连接失败：{type(err).__name__} {err}"

    async def _request(self, method: str, path: str, client: Any = None, **kwargs):
        if client is None:
            client = self._client
        return await client.request(method, f"{self._base}{path}",
                                    headers=self._headers(), timeout=self._timeout, **kwargs)

    async def ensure_queue(self, max_limit: str) -> LimitResult:
        """确保存在名为 queue_name 的简单队列，max_limit 形如 '50M/500M'（下载/上传）。"""
        if self._dry_run:
            return LimitResult(ok=True, message=f"试运行：将创建队列 {self._queue_name} max-limit={max_limit}", details=[])
        try:
            resp = await self._request("GET", f"/rest/queue/simple")
            existing = {}
            if resp is not None and int(getattr(resp, "status_code", 0) or 0) == 200:
                for row in (resp.json() if resp else []):
                    if row.get("name") == self._queue_name:
                        existing = row
                        break
            if existing:
                resp = await self._request("PUT", f"/rest/queue/simple/{existing['.id']}",
                                           json={"max-limit": max_limit})
            else:
                resp = await self._request("POST", "/rest/queue/simple",
                                           json={"name": self._queue_name, "max-limit": max_limit, "disabled": False})
            ok = resp is not None and int(getattr(resp, "status_code", 0) or 0) in (200, 201)
            return LimitResult(ok=ok, message=f"队列已{'更新' if existing else '创建'}" if ok else "失败",
                               details=[f"max-limit={max_limit}"] if ok else [f"HTTP {getattr(resp, 'status_code', '?')}"])
        except Exception as err:  # noqa: BLE001
            return LimitResult(ok=False, message=f"异常：{err}", details=[])

    async def remove_queue(self) -> LimitResult:
        if self._dry_run:
            return LimitResult(ok=True, message=f"试运行：将移除队列 {self._queue_name}", details=[])
        try:
            resp = await self._request("GET", f"/rest/queue/simple")
            target_id = None
            if resp is not None and int(getattr(resp, "status_code", 0) or 0) == 200:
                for row in (resp.json() if resp else []):
                    if row.get("name") == self._queue_name:
                        target_id = row.get(".id")
                        break
            if not target_id:
                return LimitResult(ok=True, message="队列不存在，无需移除", details=[])
            resp = await self._request("DELETE", f"/rest/queue/simple/{target_id}")
            ok = resp is not None and int(getattr(resp, "status_code", 0) or 0) in (200, 204)
            return LimitResult(ok=ok, message="已移除" if ok else "移除失败",
                               details=[] if ok else [f"HTTP {getattr(resp, 'status_code', '?')}"])
        except Exception as err:  # noqa: BLE001
            return LimitResult(ok=False, message=f"异常：{err}", details=[])


def build_mediaclient(server: MediaServer, client: Any, timeout: int = 10, dry_run: bool = False) -> MediaClient:
    return MediaClient(server, client, timeout=timeout, dry_run=dry_run)


def build_downloaderclient(dl: Any, client: Any, timeout: int = 10, dry_run: bool = False) -> DownloaderClient:
    return DownloaderClient(dl, client, timeout=timeout, dry_run=dry_run)
