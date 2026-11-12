"""绑定表、订阅站点范围和下载记录的读写。"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

from . import fingerprint

BINDINGS_KEY = "sst_bindings"
LAST_KEY = "sst_last"
LOG_KEY = "sst_log"


def _getter(row: Any):
    if isinstance(row, dict):
        return lambda key, default=None: row.get(key, default)
    if hasattr(row, "model_dump"):
        try:
            data = row.model_dump()
            return lambda key, default=None: data.get(key, default)
        except Exception:
            pass
    return lambda key, default=None: getattr(row, key, default)


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return []


def bindings(plugin: Any) -> List[Dict[str, Any]]:
    raw = plugin.get_data(BINDINGS_KEY, [])
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def save_bindings(plugin: Any, items: List[Dict[str, Any]]) -> None:
    plugin.save_data(BINDINGS_KEY, list(items))


def last_snapshot(plugin: Any) -> Dict[str, Any]:
    raw = plugin.get_data(LAST_KEY, {})
    return dict(raw) if isinstance(raw, dict) else {}


def save_snapshot(plugin: Any, data: Dict[str, Any]) -> None:
    plugin.save_data(LAST_KEY, data)


def log_lines(plugin: Any, limit: int = 40) -> List[str]:
    raw = plugin.get_data(LOG_KEY, [])
    return [str(item) for item in raw if isinstance(item, str)][:limit]


def append_log(plugin: Any, text: str, limit: int = 40) -> None:
    lines = log_lines(plugin, limit)
    stamp = datetime.datetime.now().strftime("%m-%d %H:%M")
    plugin.save_data(LOG_KEY, [f"{stamp} {text}"] + lines[: max(0, limit - 1)])


def site_options() -> List[Dict[str, Any]]:
    try:
        from app.db.oper.site import SiteOper

        rows = SiteOper().list_order_by_pri() or []
    except Exception:
        return []
    out = []
    for row in rows:
        get = _getter(row)
        name = str(get("name") or "").strip()
        sid = get("id")
        if not name or sid is None:
            continue
        out.append({
            "id": sid,
            "name": name,
            "domain": str(get("domain") or "").strip(),
            "pri": get("pri") or 0,
        })
    return out


def find_site(sites: List[Dict[str, Any]], value: Any) -> Optional[Dict[str, Any]]:
    text = str(value or "").strip()
    if not text:
        return None
    for site in sites:
        if text in (str(site.get("id")), str(site.get("name")), str(site.get("domain"))):
            return site
    for site in sites:
        if text.lower() in str(site.get("name") or "").lower():
            return site
    return None


def subscriptions(max_pages: int = 10) -> List[Dict[str, Any]]:
    try:
        from app.sdk import queries
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    page_no = 1
    while page_no <= max_pages:
        try:
            page = queries.list_subscriptions(page={"page": page_no, "count": 200})
        except Exception:
            break
        items = list(getattr(page, "items", None) or [])
        if not items:
            break
        for item in items:
            get = _getter(item)
            sid = get("id")
            if sid is None:
                continue
            out.append({
                "id": sid,
                "name": str(get("name") or "").strip(),
                "season": get("season"),
                "type": str(get("type") or "").strip(),
                "state": str(get("state") or "").strip(),
                "sites": _as_list(get("sites")),
                "total_episode": get("total_episode"),
                "lack_episode": get("lack_episode"),
            })
        total = int(getattr(page, "total", 0) or 0)
        if (total and len(out) >= total) or len(items) < 200:
            break
        page_no += 1
    return out


def downloads(max_items: int = 2000) -> List[Dict[str, Any]]:
    try:
        from app.sdk import queries
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    page_no = 1
    while len(out) < max_items:
        try:
            page = queries.list_download_history(page={"page": page_no, "count": 200})
        except Exception:
            break
        items = list(getattr(page, "items", None) or [])
        if not items:
            break
        for item in items:
            get = _getter(item)
            out.append({
                "title": str(get("title") or "").strip(),
                "seasons": str(get("seasons") or "").strip(),
                "episodes": str(get("episodes") or "").strip(),
                "torrent_name": str(get("torrent_name") or "").strip(),
                "torrent_site": str(get("torrent_site") or "").strip(),
                "download_hash": str(get("download_hash") or "").strip(),
                "downloader": str(get("downloader") or "").strip(),
                "media_id": str(get("media_id") or "").strip(),
                "date": str(get("date") or "").strip(),
            })
        total = int(getattr(page, "total", 0) or 0)
        if (total and len(out) >= total) or len(items) < 200:
            break
        page_no += 1
    return out[:max_items]


def set_sites(sid: Any, sites: Any) -> Tuple[bool, str]:
    try:
        from app.db.oper.subscribe import SubscribeOper

        result = SubscribeOper().update(int(sid), {"sites": sites})
        if result is None:
            return False, "订阅不存在"
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:160]


def record_matches(record: Dict[str, Any], binding: Dict[str, Any]) -> bool:
    name = fingerprint.normalize(binding.get("name"))
    title = fingerprint.normalize(record.get("title"))
    if not name or not title:
        return False
    if name not in title and title not in name:
        return False
    season = binding.get("season")
    if season in (None, "", 0):
        return True
    seasons = re.findall(r"\d+", str(record.get("seasons") or ""))
    if not seasons:
        return True
    return str(int(season)) in [str(int(num)) for num in seasons]


def site_state(raw: Any, binding: Dict[str, Any]) -> Optional[bool]:
    text = fingerprint.normalize(raw)
    if not text:
        return None
    choices = [
        str(binding.get("site_id") or ""),
        fingerprint.normalize(binding.get("site_name")),
        fingerprint.normalize(binding.get("domain")),
    ]
    choices = [item for item in choices if item]
    if not choices:
        return None
    if text in choices:
        return True
    for choice in choices:
        if len(choice) >= 3 and (choice in text or text in choice):
            return True
    return False


def pick_baseline(records: List[Dict[str, Any]], dimensions: List[str]) -> Dict[str, str]:
    for record in reversed(records):
        fp = fingerprint.parse(record.get("torrent_name") or "")
        if any(fp.get(dim) for dim in dimensions):
            return fp
    return {key: "" for key in fingerprint.DIMENSION_LABELS}


def inspect(bound: List[Dict[str, Any]], subs: List[Dict[str, Any]],
            records: List[Dict[str, Any]], dimensions: List[str]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    for binding in bound:
        matched = [item for item in records if record_matches(item, binding)]
        conflict_count = 0
        for record in matched:
            state = site_state(record.get("torrent_site"), binding)
            diffs = fingerprint.compare(
                binding.get("baseline") or {},
                fingerprint.parse(record.get("torrent_name") or ""),
                dimensions,
            )
            hard = [item for item in diffs if item["level"] == "conflict"]
            soft = [item for item in diffs if item["level"] in ("missing", "extra")]
            if state is False:
                hard.append({"dimension": "site", "label": "站点", "level": "conflict",
                             "baseline": str(binding.get("site_name") or ""),
                             "actual": str(record.get("torrent_site") or ""),
                             "text": f"站点不符：来自 {record.get('torrent_site') or '其他站点'}"})
            if not hard and not soft and state is not False:
                continue
            if hard:
                conflict_count += 1
            issues.append({
                "sid": binding.get("sid"),
                "name": binding.get("name"),
                "season": binding.get("season"),
                "episode": record.get("episodes") or record.get("seasons") or "-",
                "site": record.get("torrent_site") or "未记录",
                "title": record.get("torrent_name") or record.get("title") or "",
                "hash": record.get("download_hash") or "",
                "downloader": record.get("downloader") or "",
                "level": "conflict" if hard else "warn",
                "reasons": [item["text"] for item in hard + soft][:5],
            })
        rows.append({
            "sid": binding.get("sid"),
            "name": binding.get("name"),
            "season": binding.get("season"),
            "site_name": binding.get("site_name"),
            "baseline": fingerprint.brief(binding.get("baseline") or {}, dimensions),
            "records": len(matched),
            "conflicts": conflict_count,
            "locked": bool(binding.get("locked")),
        })
    stats = {
        "bindings": len(bound),
        "locked": sum(1 for row in rows if row["locked"]),
        "conflicts": sum(1 for item in issues if item["level"] == "conflict"),
        "warnings": sum(1 for item in issues if item["level"] == "warn"),
        "records": len(records),
        "subscriptions": len(subs),
    }
    pending = [sub for sub in subs if not any(str(b.get("sid")) == str(sub.get("id")) for b in bound)]
    return {
        "rows": rows,
        "issues": issues,
        "pending": pending,
        "stats": stats,
        "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def remove_torrent(issue: Dict[str, Any]) -> Tuple[bool, str]:
    hash_value = str(issue.get("hash") or "")
    if not hash_value:
        return False, "没有任务 hash"
    try:
        from app.sdk.services import DownloaderHelper

        services = DownloaderHelper().get_services() or []
    except Exception:
        return False, "读取下载器失败"
    if isinstance(services, dict):
        services = list(services.values())
    target = str(issue.get("downloader") or "").lower()
    for service in services:
        name = str(_getter(service)("name") or "")
        if target and name and target not in name.lower():
            continue
        instance = getattr(service, "instance", None) or service
        for method in ("delete_torrents", "remove_torrents"):
            fn = getattr(instance, method, None)
            if not callable(fn):
                continue
            try:
                fn(ids=[hash_value], delete_files=False)
                return True, f"{name or '下载器'} 已移除"
            except Exception:
                continue
    return False, "下载器没有可用的移除方法"
