"""订阅、下载记录和站点范围的读写，外加一轮巡检。"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

from . import rules

SNAPSHOT_KEY = "sst_snapshot"
BINDINGS_KEY = "sst_site_backup"
LOG_KEY = "sst_log"
TEMPLATE_KEY = "sst_template"


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


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def bindings(plugin: Any) -> Dict[str, Any]:
    raw = plugin.get_data(BINDINGS_KEY, {})
    return dict(raw) if isinstance(raw, dict) else {}


def save_bindings(plugin: Any, items: Dict[str, Any]) -> None:
    plugin.save_data(BINDINGS_KEY, _jsonable(items))


def last_snapshot(plugin: Any) -> Dict[str, Any]:
    raw = plugin.get_data(SNAPSHOT_KEY, {})
    return dict(raw) if isinstance(raw, dict) else {}


def save_snapshot(plugin: Any, data: Dict[str, Any]) -> None:
    plugin.save_data(SNAPSHOT_KEY, _jsonable(data))


def template(plugin: Any) -> str:
    raw = plugin.get_data(TEMPLATE_KEY, "")
    return str(raw) if isinstance(raw, str) else ""


def save_template(plugin: Any, text: str) -> None:
    plugin.save_data(TEMPLATE_KEY, str(text))


def log_lines(plugin: Any, limit: int = 40) -> List[str]:
    raw = plugin.get_data(LOG_KEY, [])
    return [str(item) for item in raw if isinstance(item, str)][:limit]


def append_log(plugin: Any, text: str, limit: int = 40) -> None:
    stamp = datetime.datetime.now().strftime("%m-%d %H:%M")
    plugin.save_data(LOG_KEY, [f"{stamp} {text}"] + log_lines(plugin, limit)[: max(0, limit - 1)])


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
        out.append({"id": sid, "name": name,
                    "domain": str(get("domain") or "").strip()})
    return out


def find_site(sites: List[Dict[str, Any]], value: Any) -> Optional[Dict[str, Any]]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    for site in sites:
        if text in (str(site.get("id")), str(site.get("name") or "").lower(),
                    str(site.get("domain") or "").lower()):
            return site
    for site in sites:
        if str(site.get("name") or "").lower().find(text) >= 0:
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
                "id": _jsonable(sid),
                "name": str(get("name") or "").strip(),
                "season": _jsonable(get("season")),
                "type": str(get("type") or "").strip(),
                "state": str(get("state") or "").strip(),
                "sites": _jsonable(_as_list(get("sites"))),
                "lack_episode": _jsonable(get("lack_episode")),
            })
        total = int(getattr(page, "total", 0) or 0)
        if (total and len(out) >= total) or len(items) < 200:
            break
        page_no += 1
    return out


def downloads(max_items: int = 3000) -> List[Dict[str, Any]]:
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


def record_matches(record: Dict[str, Any], sub: Dict[str, Any]) -> bool:
    name = rules.normalize(sub.get("name"))
    title = rules.normalize(record.get("title"))
    if not name or not title:
        return False
    if name not in title and title not in name:
        return False
    season = sub.get("season")
    if season in (None, "", 0):
        return True
    seasons = re.findall(r"\d+", str(record.get("seasons") or ""))
    if not seasons:
        return True
    try:
        return str(int(season)) in [str(int(item)) for item in seasons]
    except (TypeError, ValueError):
        return True


def sites_text(sub: Dict[str, Any], sites: List[Dict[str, Any]]) -> str:
    values = sub.get("sites") if isinstance(sub.get("sites"), list) else []
    if not values:
        return "未限制"
    names = []
    for value in values:
        for site in sites:
            if str(site.get("id")) == str(value):
                names.append(str(site.get("name")))
                break
        else:
            names.append(str(value))
    return "、".join(names[:4]) + ("…" if len(names) > 4 else "")


def inspect(rule_list: List[Dict[str, Any]], subs: List[Dict[str, Any]],
            records: List[Dict[str, Any]], sites: List[Dict[str, Any]],
            rule_errors: Optional[List[str]] = None) -> Dict[str, Any]:
    sub_rows: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    rule_rows: List[Dict[str, Any]] = []
    ruled = 0

    for rule in rule_list:
        matched = [sub for sub in subs if rules.rule_matches(rule, sub)]
        rule_rows.append({
            "line": rule.get("line"),
            "target": rule.get("target"),
            "site": rule.get("site") or "不限",
            "summary": rules.brief(rule),
            "matched": len(matched),
            "errors": list(rule.get("errors") or []),
        })

    for sub in subs:
        picked = next((rule for rule in rule_list if rules.rule_matches(rule, sub)), None)
        if picked:
            ruled += 1
        matched_records = [item for item in records if record_matches(item, sub)]
        problems = 0
        for record in matched_records:
            if not picked:
                continue
            reasons: List[str] = []
            site_state = rules.check_site(picked, record.get("torrent_site"), sites)
            if site_state is False:
                reasons.append(f"站点不符：来自 {record.get('torrent_site') or '其他站点'}，"
                               f"规则要 {picked.get('site')}")
            title = record.get("torrent_name") or record.get("title") or ""
            reasons.extend(rules.check_title(picked, title))
            if not reasons:
                continue
            problems += 1
            issues.append({
                "sid": sub.get("id"),
                "name": sub.get("name"),
                "season": sub.get("season"),
                "episode": record.get("episodes") or record.get("seasons") or "-",
                "site": record.get("torrent_site") or "未记录",
                "title": title,
                "hash": record.get("download_hash") or "",
                "downloader": record.get("downloader") or "",
                "reasons": reasons[:6],
            })
        sub_rows.append({
            "id": sub.get("id"),
            "name": sub.get("name"),
            "season": sub.get("season"),
            "state": sub.get("state") or "-",
            "sites": sites_text(sub, sites),
            "rule": rules.brief(picked) if picked else "",
            "rule_target": picked.get("target") if picked else "",
            "records": len(matched_records),
            "problems": problems,
        })

    return {
        "subscriptions": sub_rows,
        "rules": rule_rows,
        "issues": issues,
        "errors": list(rule_errors or []),
        "stats": {
            "subscriptions": len(subs),
            "ruled": ruled,
            "rules": len(rule_list),
            "records": len(records),
            "problems": len(issues),
        },
        "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def lock_sites(plugin: Any, rule_list: List[Dict[str, Any]], subs: List[Dict[str, Any]],
               sites: List[Dict[str, Any]]) -> Tuple[int, List[str]]:
    saved = bindings(plugin)
    done, messages = 0, []
    for sub in subs:
        rule = next((item for item in rule_list if rules.rule_matches(item, sub)), None)
        if not rule or not str(rule.get("site") or "").strip():
            continue
        site = find_site(sites, rule.get("site"))
        if not site:
            messages.append(f"《{sub.get('name')}》的站点「{rule.get('site')}」没在站点列表里找到")
            continue
        key = str(sub.get("id"))
        if key not in saved:
            saved[key] = _jsonable(sub.get("sites") or [])
        ok, reason = set_sites(sub.get("id"), [site.get("id")])
        if ok:
            done += 1
            append_log(plugin, f"《{sub.get('name')}》站点范围锁定为 {site.get('name')}")
        else:
            messages.append(f"《{sub.get('name')}》写入失败：{reason}")
    save_bindings(plugin, saved)
    return done, messages


def unlock_sites(plugin: Any) -> Tuple[int, List[str]]:
    saved = bindings(plugin)
    done, messages = 0, []
    for key, original in saved.items():
        ok, reason = set_sites(key, original)
        if ok:
            done += 1
        else:
            messages.append(f"订阅 {key} 还原失败：{reason}")
    save_bindings(plugin, {})
    append_log(plugin, f"还原了 {done} 条订阅的站点范围")
    return done, messages


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
