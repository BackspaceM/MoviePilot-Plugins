"""观影周报 / 转码诊断 —— 媒体服务器客户端。

读取 Emby / Jellyfin / Plex 的近期播放历史并归一化为 PlayRecord，
同时提供活动会话读取（用于"立即诊断"实时转码现场）。
所有请求经 app.sdk.network.AsyncRequestUtils，统一做超时、异常收敛与降级。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .core import (
    MEDIA_MOVIE,
    MEDIA_TV,
    MediaServer,
    PlayRecord,
    _ip_is_private,
    _norm_codec,
    infer_transcode_reasons,
)


def normalize_base_url(host: str, port: int = 8096, use_ssl: bool = True) -> str:
    """把用户输入的地址规范化为 http(s)://host[:port] 形式。"""
    text = str(host or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = ("https://" if use_ssl else "http://") + text
    text = text.rstrip("/")
    hostport = text.split("://", 1)[1].split("/")[0]
    if ":" not in hostport:
        text = f"{text}:{port}"
    return text


def _as_ts(value: Any) -> float:
    """把 Emby(.NET ticks / ISO) 或 Plex(epoch) 的时间换算为 Unix 秒。"""
    if value is None or value == "":
        return 0.0
    # .NET ticks
    if isinstance(value, (int, float)) and value > 10_000_000_000_000:
        try:
            return float(value) / 10_000_000.0 - 62135596800.0
        except Exception:  # noqa: BLE001
            return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    # ISO 8601，如 2026-11-10T12:00:00.000Z / +08:00
    try:
        from datetime import datetime, timezone
        t = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _ticks_to_sec(value: Any) -> float:
    """RunTimeTicks（100ns）换算为秒。"""
    try:
        ticks = float(value or 0)
        if ticks <= 0:
            return 0.0
        return ticks / 10_000_000.0
    except Exception:  # noqa: BLE001
        return 0.0


def _media_type(item: Dict[str, Any]) -> str:
    t = str(item.get("Type") or item.get("type") or "").lower()
    if t in ("episode", "tvshow", "season") or item.get("SeriesName") or item.get("grandparentTitle"):
        return MEDIA_TV
    return MEDIA_MOVIE


def _primary_video_stream(item: Dict[str, Any]) -> Dict[str, Any]:
    streams = item.get("MediaStreams")
    if isinstance(streams, list):
        for s in streams:
            if str(s.get("Type", "")).lower() == "video":
                return s or {}
    media_list = item.get("Media")
    if isinstance(media_list, list) and media_list:
        parts = (media_list[0] or {}).get("Part") or []
        if isinstance(parts, list) and parts:
            streams = parts[0].get("Stream") or []
            for s in streams if isinstance(streams, list) else []:
                if str(s.get("type", "")).lower() == "video":
                    return s or {}
    return {}


def _primary_audio_stream(item: Dict[str, Any]) -> Dict[str, Any]:
    streams = item.get("MediaStreams")
    if isinstance(streams, list):
        for s in streams:
            if str(s.get("Type", "")).lower() == "audio":
                return s or {}
    media_list = item.get("Media")
    if isinstance(media_list, list) and media_list:
        parts = (media_list[0] or {}).get("Part") or []
        if isinstance(parts, list) and parts:
            streams = parts[0].get("Stream") or []
            for s in streams if isinstance(streams, list) else []:
                if str(s.get("type", "")).lower() == "audio":
                    return s or {}
    return {}


class MediaClient:
    """读取单个媒体服务器的历史与活动会话，归一化为 PlayRecord。"""

    def __init__(self, server: MediaServer, client: Any, timeout: int = 10):
        self.server = server
        self._client = client
        self._timeout = timeout
        default_port = 443 if server.is_plex else 8096
        self._base = normalize_base_url(server.url, default_port)

    # ------------------------------------------------------------------
    # 通用请求
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        if self.server.is_plex:
            return {"X-Plex-Token": self.server.apikey, "Accept": "application/json"}
        return {"X-Emby-Token": self.server.apikey, "Accept": "application/json"}

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        kwargs: Dict[str, Any] = {"headers": self._headers(), "timeout": self._timeout}
        if self.server.is_plex:
            kwargs["params"] = dict(params or {})
            kwargs["params"].setdefault("X-Plex-Token", self.server.apikey)
        else:
            kwargs["params"] = dict(params or {})
            kwargs["params"].setdefault("api_key", self.server.apikey)
        resp = await self._client.request("GET", f"{self._base}{path}", **kwargs)
        if resp is None or int(getattr(resp, "status_code", 0) or 0) != 200:
            return None
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return None

    async def check(self) -> Tuple[bool, str]:
        try:
            if self.server.is_plex:
                payload = await self._get("/status/sessions")
                ok = payload is not None
            else:
                payload = await self._get("/emby/System/Info")
                ok = isinstance(payload, dict) and bool(payload)
            return ok, "连接正常" if ok else "连接失败或返回非 200"
        except Exception as err:  # noqa: BLE001
            return False, f"连接异常：{type(err).__name__} {err}"

    # ------------------------------------------------------------------
    # 近期播放历史
    # ------------------------------------------------------------------

    async def get_recent_records(self, limit: int = 200) -> List[PlayRecord]:
        """读取近期播放历史，返回归一化记录（尽力而为，失败返回空列表）。"""
        try:
            if self.server.is_plex:
                return await self._plex_history(limit)
            return await self._emby_history(limit)
        except Exception:  # noqa: BLE001
            return []

    async def _emby_history(self, limit: int) -> List[PlayRecord]:
        # 先取用户列表，再逐用户拉取最近播放项
        users = await self._get("/emby/Users/Query", {"IsDisabled": "false"}) or {}
        user_list = users.get("Users") if isinstance(users, dict) else []
        if not isinstance(user_list, list):
            user_list = []
        out: List[PlayRecord] = []
        per_user = max(20, limit // max(1, len(user_list)))
        for u in user_list:
            uid = str(u.get("Id", ""))
            uname = str(u.get("Name", ""))
            items = await self._get(f"/emby/Users/{uid}/Items", {
                "SortBy": "DatePlayed", "SortOrder": "Descending",
                "Filters": "IsPlayed", "Limit": str(per_user),
                "Fields": "DatePlayed,PlayCount,RunTimeTicks,UserData,MediaStreams,Path,ProviderIds",
            })
            rows = items.get("Items") if isinstance(items, dict) else []
            for item in rows if isinstance(rows, list) else []:
                rec = self._emby_item_to_record(item, uname)
                if rec:
                    out.append(rec)
        return out

    def _emby_item_to_record(self, item: Dict[str, Any], uname: str) -> Optional[PlayRecord]:
        user_data = item.get("UserData") or {}
        started = _as_ts(user_data.get("LastPlayedDate") or item.get("DatePlayed"))
        if started <= 0:
            return None
        play_count = int(user_data.get("PlayCount") or 0)
        resume = float(user_data.get("ResumePositionTicks") or 0) / 10_000_000.0
        runtime = _ticks_to_sec(item.get("RunTimeTicks"))
        # 每次播放按单片时长估算；断点续播用 ResumePositionTicks
        duration = resume if resume > 0 else runtime
        vs = _primary_video_stream(item)
        au = _primary_audio_stream(item)
        vcodec = str(vs.get("Codec") or "")
        acodec = str(au.get("Codec") or "")
        width = int(vs.get("Width") or 0)
        height = int(vs.get("Height") or 0)
        resolution = f"{width}x{height}" if width and height else ""
        bitrate = int((vs.get("BitRate") or item.get("Bitrate") or 0)) // 1000  # bps -> kbps
        # Emby 历史本身不标记转码；以编解码不兼容做"潜在转码"推断
        reasons = infer_transcode_reasons(vcodec, acodec, resolution, False, False, False, bitrate, True)
        likely_transcode = any(x in reasons for x in ("hevc", "audio", "4k"))
        title = str(item.get("Name", ""))
        sub = str(item.get("SeriesName", "")) if _media_type(item) == MEDIA_TV else ""
        if item.get("ParentIndexNumber") and item.get("IndexNumber"):
            ep = f"S{int(item['ParentIndexNumber']):02d}E{int(item['IndexNumber']):02d}"
            sub = f"{sub} · {ep}" if sub else ep
        path = str(item.get("Path", ""))
        key = f"{self.server.name}:{item.get('Id', title)}:{int(started)}"
        return PlayRecord(
            key=key, server=self.server.name, user=uname, title=title, sub_title=sub,
            media_type=_media_type(item), started=started,
            duration_sec=float(duration * max(1, min(play_count, 1))),
            is_transcode=likely_transcode, is_remote=False,
            video_codec=vcodec, audio_codec=acodec, resolution=resolution,
            bitrate_kbps=bitrate, hwaccel=False, reasons=reasons if likely_transcode else [],
        )

    async def _plex_history(self, limit: int) -> List[PlayRecord]:
        payload = await self._get("/status/sessions/history/all", {"count": str(limit)}) or {}
        container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
        videos = container.get("Video") if isinstance(container, dict) else []
        out: List[PlayRecord] = []
        for item in videos if isinstance(videos, list) else []:
            started = _as_ts(item.get("viewedAt") or item.get("lastViewedAt"))
            if started <= 0:
                continue
            vs = _primary_video_stream(item)
            au = _primary_audio_stream(item)
            vcodec = str(vs.get("codec") or item.get("videoCodec") or "")
            acodec = str(au.get("codec") or item.get("audioCodec") or "")
            width = int(vs.get("width") or item.get("width") or 0)
            height = int(vs.get("height") or item.get("height") or 0)
            resolution = f"{width}x{height}" if width and height else ""
            bitrate = int(item.get("bitrate") or 0)
            reasons = infer_transcode_reasons(vcodec, acodec, resolution, False, False, False, bitrate, True)
            likely_transcode = any(x in reasons for x in ("hevc", "audio", "4k"))
            duration = _ticks_to_sec(item.get("duration")) / 1000.0 if item.get("duration") else 0.0
            if item.get("duration"):
                # Plex duration 字段是毫秒
                try:
                    duration = float(item["duration"]) / 1000.0
                except Exception:  # noqa: BLE001
                    duration = 0.0
            title = str(item.get("title", ""))
            grand = str(item.get("grandparentTitle", ""))
            sub = grand or ""
            if item.get("parentIndex") and item.get("index"):
                ep = f"S{int(item['parentIndex']):02d}E{int(item['index']):02d}"
                sub = f"{sub} · {ep}" if sub else ep
            user = str((item.get("User") or {}).get("title", ""))
            key = f"{self.server.name}:{item.get('ratingKey', title)}:{int(started)}"
            out.append(PlayRecord(
                key=key, server=self.server.name, user=user, title=title, sub_title=sub,
                media_type=_media_type(item), started=started, duration_sec=float(duration),
                is_transcode=likely_transcode, is_remote=False,
                video_codec=vcodec, audio_codec=acodec, resolution=resolution,
                bitrate_kbps=bitrate, hwaccel=False,
                reasons=reasons if likely_transcode else [],
            ))
        return out

    # ------------------------------------------------------------------
    # 活动会话（实时转码诊断）
    # ------------------------------------------------------------------

    async def get_active_records(self) -> List[PlayRecord]:
        """读取当前正在播放的会话（含真实转码标记），用于实时诊断。"""
        try:
            if self.server.is_plex:
                payload = await self._get("/status/sessions") or {}
                container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
                rows = container.get("Video") if isinstance(container, dict) else []
                return [self._plex_session(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
            rows = await self._get("/emby/Sessions") or []
            out = []
            for raw in rows if isinstance(rows, list) else []:
                item = (raw or {}).get("NowPlayingItem")
                if not item:
                    continue
                out.append(self._emby_session(raw, item))
            return out
        except Exception:  # noqa: BLE001
            return []

    def _emby_session(self, raw: Dict[str, Any], item: Dict[str, Any]) -> PlayRecord:
        play = raw.get("PlayState") or {}
        tc = raw.get("TranscodingInfo") or {}
        is_tc = bool(tc) and not bool(tc.get("IsVideoDirect", True))
        vs = _primary_video_stream(item)
        au = _primary_audio_stream(item)
        vcodec = str(vs.get("Codec") or "")
        acodec = str(au.get("Codec") or "")
        width = int(vs.get("Width") or 0)
        height = int(vs.get("Height") or 0)
        resolution = f"{width}x{height}" if width and height else ""
        bitrate = int(vs.get("BitRate") or 0) // 1000
        remote = not _ip_is_private(str(raw.get("RemoteEndPoint", "")))
        subtitle_burn = bool(tc.get("IsTextSubtitleStreamTranscoding") or tc.get("SubtitleCodec"))
        hwaccel = bool(tc.get("HardwareAcceleration")) and str(tc.get("HardwareAcceleration")).lower() not in ("none", "false", "0")
        reasons = infer_transcode_reasons(vcodec, acodec, resolution, remote, subtitle_burn,
                                          hwaccel, bitrate, is_tc)
        # 活动会话无精确 started 字段，用当前时间近似
        import time
        started = float(time.time())
        sub = str(item.get("SeriesName", "")) if _media_type(item) == MEDIA_TV else ""
        if item.get("ParentIndexNumber") and item.get("IndexNumber"):
            ep = f"S{int(item['ParentIndexNumber']):02d}E{int(item['IndexNumber']):02d}"
            sub = f"{sub} · {ep}" if sub else ep
        return PlayRecord(
            key=f"{self.server.name}:live:{raw.get('SessionId', '')}:{item.get('Id', '')}",
            server=self.server.name, user=str(raw.get("UserName", "")),
            title=str(item.get("Name", "")), sub_title=sub, media_type=_media_type(item),
            started=started, duration_sec=_ticks_to_sec(play.get("PositionTicks")),
            is_transcode=is_tc, is_remote=remote,
            client=str(raw.get("Client", "")), device=str(raw.get("DeviceName", "")),
            video_codec=vcodec, audio_codec=acodec, resolution=resolution,
            bitrate_kbps=bitrate, hwaccel=hwaccel, reasons=reasons,
        )

    def _plex_session(self, raw: Dict[str, Any]) -> PlayRecord:
        player = raw.get("Player") or {}
        sess = raw.get("session") or {}
        decision = str(raw.get("TranscodeDecision", "")).lower()
        is_tc = decision == "transcode"
        remote = str(sess.get("location", "")).lower() == "wan" or str(player.get("local", "1")) == "0"
        vs = _primary_video_stream(raw)
        au = _primary_audio_stream(raw)
        vcodec = str(vs.get("codec") or raw.get("videoCodec") or "")
        acodec = str(au.get("codec") or raw.get("audioCodec") or "")
        width = int(vs.get("width") or raw.get("width") or 0)
        height = int(vs.get("height") or raw.get("height") or 0)
        resolution = f"{width}x{height}" if width and height else ""
        bitrate = int(raw.get("bitrate") or 0)
        reasons = infer_transcode_reasons(vcodec, acodec, resolution, remote, False, False, bitrate, is_tc)
        import time
        sub = str(raw.get("grandparentTitle", ""))
        if raw.get("parentIndex") and raw.get("index"):
            ep = f"S{int(raw['parentIndex']):02d}E{int(raw['index']):02d}"
            sub = f"{sub} · {ep}" if sub else ep
        return PlayRecord(
            key=f"{self.server.name}:live:{raw.get('sessionKey', '')}:{raw.get('ratingKey', '')}",
            server=self.server.name, user=str((raw.get("User") or {}).get("title", "")),
            title=str(raw.get("title", "")), sub_title=sub, media_type=_media_type(raw),
            started=float(time.time()), duration_sec=float(raw.get("viewOffset", 0) or 0) / 1000.0,
            is_transcode=is_tc, is_remote=remote,
            client=str(player.get("title", "")), device=str(player.get("device", "")),
            video_codec=vcodec, audio_codec=acodec, resolution=resolution,
            bitrate_kbps=bitrate, hwaccel=False, reasons=reasons,
        )


def build_media_client(server: MediaServer, client: Any, timeout: int = 10) -> MediaClient:
    return MediaClient(server, client, timeout=timeout)
