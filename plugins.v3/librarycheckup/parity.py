"""Emby / Jellyfin / Plex 一致性对账。

自动发现宿主已连接的三家媒体服务器，只读拉取条目，按统一媒体身份对齐：
缺集 / 多版本 / 仅某家存在 / 三端一致。

关键防误报：某家连接或查询失败时标「未核验」，并且**不参与缺集判定**——
绝不把「查不到」当成「这家没有」。全程只读，不触发任何刷新或写回。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import common

MAX_ITEMS_PER_SERVER = 2000
MAX_ROWS = 5000
CATEGORY_ORDER = ("missing_in_some", "only_one", "multi_version", "single_source", "consistent")


def _provider_key(provider_ids: Any) -> str:
    """用外部 ID 构造稳定媒体身份，语义对齐宿主的 build_media_key。"""
    if not isinstance(provider_ids, dict):
        return ""
    for name in ("Tmdb", "TMDB", "tmdb", "Imdb", "IMDB", "imdb", "Tvdb", "TVDB", "tvdb"):
        value = provider_ids.get(name)
        if value not in (None, "", 0, "0"):
            source = {"tmdb": "tmdb", "imdb": "imdb", "tvdb": "tvdb"}[name.lower()]
            return f"{source}:{str(value).strip()}"
    return ""


def _plex_guid(raw: Dict[str, Any]) -> str:
    for guid in raw.get("Guid") or []:
        text = str((guid or {}).get("id") if isinstance(guid, dict) else guid or "")
        match = re.search(r"(themoviedb|imdb|thetvdb)://([^?/]+)", text, re.I)
        if match:
            source = {"themoviedb": "tmdb", "imdb": "imdb", "thetvdb": "tvdb"}[match.group(1).lower()]
            return f"{source}:{match.group(2)}"
    return ""


def normalize_item(raw: Dict[str, Any], stype: str) -> Optional[Dict[str, Any]]:
    """把三家原始条目归一化为统一身份；无法识别的条目直接跳过。"""
    if not isinstance(raw, dict):
        return None
    if stype == "plex":
        item_type = str(raw.get("type") or "").lower()
        if item_type not in ("movie", "show", "episode"):
            return None
        kind = "tv" if item_type in ("show", "episode") else "movie"
        title = str(raw.get("grandparentTitle") or raw.get("title") or "").strip()
        year = str(raw.get("year") or raw.get("parentYear") or "")
        season = str(raw.get("parentIndex") or "")
        episode = str(raw.get("index") or "")
        if kind == "tv" and season:
            season = f"S{int(season):02d}" if season.isdigit() else season
        if episode and episode.isdigit():
            episode = f"E{int(episode):02d}"
        provider = _plex_guid(raw)
        path = str(raw.get("key") or "")
        name = str(raw.get("title") or title)
    else:
        item_type = str(raw.get("Type") or raw.get("type") or "").lower()
        if item_type not in ("movie", "series", "episode", "season"):
            return None
        kind = "tv" if item_type != "movie" else "movie"
        title = str(raw.get("SeriesName") or raw.get("Name") or "").strip()
        year = str(raw.get("ProductionYear") or "")
        season_num = raw.get("ParentIndexNumber")
        episode_num = raw.get("IndexNumber")
        season = f"S{int(season_num):02d}" if isinstance(season_num, (int, float)) and season_num else ""
        episode = f"E{int(episode_num):02d}" if isinstance(episode_num, (int, float)) and episode_num else ""
        provider = _provider_key(raw.get("ProviderIds"))
        path = str(raw.get("Path") or "")
        name = str(raw.get("Name") or title)
    if not title:
        return None
    season = season if kind == "tv" else ""
    episode = episode if kind == "tv" else ""
    key = provider or "|".join((
        common.normalize_title(title), year, season, episode, kind,
    ))
    if provider and (season or episode):
        key = f"{provider}|{season}{episode}"
    return {
        "key": key, "title": title, "name": name, "year": year,
        "season": season, "episode": episode, "kind": kind,
        "media_key": provider, "path": path,
    }


def fetch_items(server: Dict[str, Any], http_get=None) -> Tuple[List[Dict[str, Any]], bool, str]:
    """只读拉取单家条目。失败返回 (部分结果, False, 原因)，原因里不含密钥。"""
    get = http_get or common.http_get
    stype = str(server.get("type") or "").lower()
    url = str(server.get("url") or "").rstrip("/")
    apikey = str(server.get("apikey") or "")
    if not url or not apikey:
        return [], False, "配置缺少地址或密钥"
    headers = {"Accept": "application/json"}
    if stype == "plex":
        headers["X-Plex-Token"] = apikey
    else:
        headers["X-Emby-Token"] = apikey

    try:
        if stype == "plex":
            sections_resp = get(url + "/library/sections", {}, headers)
            container = sections_resp.get("MediaContainer") or {}
            sections = [d for d in (container.get("Directory") or [])
                        if isinstance(d, dict) and str(d.get("type")) in ("movie", "show")]
            raw_items: List[Dict[str, Any]] = []
            for section in sections:
                key = str(section.get("key") or "")
                if not key:
                    continue
                params = {"X-Plex-Container-Size": MAX_ITEMS_PER_SERVER}
                if str(section.get("type")) == "show":
                    params["type"] = 4  # episode
                data = get(f"{url}/library/sections/{key}/all", params, headers)
                metadata = (data.get("MediaContainer") or {}).get("Metadata") or []
                raw_items.extend([x for x in metadata if isinstance(x, dict)][:MAX_ITEMS_PER_SERVER])
                if len(raw_items) >= MAX_ITEMS_PER_SERVER:
                    break
        else:
            params = {
                "IncludeItemTypes": "Movie,Series,Episode",
                "Recursive": "true",
                "Fields": "ProviderIds,ProductionYear,Path,ParentIndexNumber,IndexNumber,SeriesName",
                "Limit": MAX_ITEMS_PER_SERVER,
                "StartIndex": 0,
            }
            try:
                data = get(url + "/emby/Items", params, headers)
                raw_items = [x for x in (data.get("Items") or []) if isinstance(x, dict)]
            except Exception:  # noqa: BLE001 - Jellyfin 也常用 /Items
                data = get(url + "/Items", params, headers)
                raw_items = [x for x in (data.get("Items") or []) if isinstance(x, dict)]
    except Exception as exc:  # noqa: BLE001
        return [], False, f"{stype} 查询失败（{type(exc).__name__}）"

    items: List[Dict[str, Any]] = []
    for raw in raw_items[:MAX_ITEMS_PER_SERVER]:
        item = normalize_item(raw, stype)
        if item:
            items.append(item)
    return items, True, ""


def build_parity(
    cfg: Optional[Dict[str, Any]] = None,
    servers: Optional[Sequence[Dict[str, Any]]] = None,
    servers_ok: bool = True,
    servers_reason: str = "",
    http_get=None,
) -> Dict[str, Any]:
    servers = list(servers or [])
    reports: List[Dict[str, Any]] = []
    present: Dict[str, Dict[str, Any]] = {}
    successful: List[str] = []

    for server in servers:
        name = str(server.get("name") or server.get("type") or "未命名")
        stype = str(server.get("type") or "")
        items, ok, reason = fetch_items(server, http_get)
        reports.append({
            "name": name, "type": stype, "ok": ok, "reason": reason,
            "count": len(items), "url_host": _host_only(server.get("url")),
        })
        if not ok:
            continue
        successful.append(name)
        counts: Dict[str, int] = {}
        for item in items:
            key = item["key"]
            counts[key] = counts.get(key, 0) + 1
            entry = present.setdefault(key, {
                "key": key, "title": item["title"], "year": item["year"],
                "season": item["season"], "episode": item["episode"],
                "kind": item["kind"], "media_key": item["media_key"],
                "in_servers": {}, "paths": [],
            })
            entry["in_servers"][name] = entry["in_servers"].get(name, 0) + 1
            if item["path"] and len(entry["paths"]) < 6:
                entry["paths"].append(f"{name}:{item['path']}")
        for key, count in counts.items():
            if count > 1:
                present[key].setdefault("duplicates", {})[name] = count

    rows: List[Dict[str, Any]] = []
    for entry in present.values():
        covered = set(entry["in_servers"])
        missing = [n for n in successful if n not in covered]
        duplicates = entry.get("duplicates") or {}
        if len(successful) < 2:
            category = "single_source"
        elif len(covered) == 1 and len(successful) > 1:
            category = "only_one"
        elif missing:
            category = "missing_in_some"
        elif duplicates:
            category = "multi_version"
        else:
            category = "consistent"
        rows.append({
            **{k: v for k, v in entry.items() if k != "duplicates"},
            "in_servers": dict(entry["in_servers"]),
            "missing_in": missing,
            "duplicates": duplicates,
            "category": category,
            "truncated_note": "",
        })

    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    rows.sort(key=lambda r: (order.get(r["category"], 9), r["title"], r["season"], r["episode"]))
    truncated = len(rows) > MAX_ROWS
    if truncated:
        rows = rows[:MAX_ROWS]
    summary = {c: sum(1 for r in rows if r["category"] == c) for c in CATEGORY_ORDER}
    summary.update({
        "total": len(rows),
        "servers_total": len(servers),
        "servers_ok": len(successful),
        "servers_failed": len(servers) - len(successful),
        "truncated": truncated,
    })
    if not servers:
        reason = servers_reason or "未发现已连接的 Emby / Jellyfin / Plex 实例"
    elif not successful:
        reason = "所有媒体服务器都未能读取，全部标为未核验；不把连接失败当成「这家没有」"
    elif len(successful) < 2:
        reason = f"当前只有 {len(successful)} 家可核验，列出条目但不做跨服缺集判定"
    else:
        reason = ""
    return {
        "ok": bool(successful),
        "reason": reason,
        "servers": reports,
        "successful": successful,
        "rows": rows,
        "summary": summary,
        "readonly": True,
        "note": (
            "身份键优先用 TMDB/IMDB/TVDB 外部 ID，缺失时回退到「规范化标题 + 年份 + 季集 + 类型」。"
            "连接或查询失败的服务一律标「未核验」，不参与缺集判定，绝不把查不到当成这家没有；"
            "本视图只读，不会触发媒体服务器刷新或写回。"
        ),
    }


def _host_only(url: Any) -> str:
    """页面只显示主机，不暴露完整地址与密钥。"""
    text = str(url or "")
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(text)
        return parsed.hostname or text[:40]
    except Exception:  # noqa: BLE001
        return text[:40]
