"""订阅、站点、下载记录的读写，以及把规则写进 MP 订阅。"""

from __future__ import annotations

import datetime
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import rules

SNAPSHOT_KEY = "sst_snapshot"
CATALOG_KEY = "sst_catalog"
ORIGINAL_KEY = "sst_original"
LOG_KEY = "sst_log"
TEMPLATE_KEY = "sst_template"
PLAN_KEY = "sst_clean_plan"
PLAN_TTL = 600


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


def catalog(plugin: Any) -> Dict[str, Any]:
    raw = plugin.get_data(CATALOG_KEY, {})
    return dict(raw) if isinstance(raw, dict) else {}


def save_catalog(plugin: Any, subs: List[Dict[str, Any]], sites: List[Dict[str, Any]]) -> None:
    plugin.save_data(CATALOG_KEY, _jsonable({
        "subscriptions": subs, "sites": sites,
        "scanned_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }))


def original_settings(plugin: Any) -> Dict[str, Any]:
    raw = plugin.get_data(ORIGINAL_KEY, {})
    return dict(raw) if isinstance(raw, dict) else {}


def save_original(plugin: Any, items: Dict[str, Any]) -> None:
    plugin.save_data(ORIGINAL_KEY, _jsonable(items))


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


def make_plan(plugin: Any, issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    targets = [{"hash": str(item.get("hash") or ""), "name": str(item.get("name") or ""),
                "downloader": str(item.get("downloader") or "")}
               for item in issues if item.get("hash")][:20]
    plan = {"id": uuid.uuid4().hex[:12], "created": datetime.datetime.now().timestamp(),
            "targets": targets}
    plugin.save_data(PLAN_KEY, _jsonable(plan))
    return plan


def take_plan(plugin: Any, plan_id: Any) -> Optional[Dict[str, Any]]:
    raw = plugin.get_data(PLAN_KEY, {})
    if not isinstance(raw, dict) or str(raw.get("id") or "") != str(plan_id or ""):
        return None
    try:
        age = datetime.datetime.now().timestamp() - float(raw.get("created") or 0)
    except (TypeError, ValueError):
        return None
    if age > PLAN_TTL:
        plugin.save_data(PLAN_KEY, {})
        return None
    return raw


def clear_plan(plugin: Any) -> None:
    plugin.save_data(PLAN_KEY, {})


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
        out.append({"id": _jsonable(sid), "name": name,
                    "domain": str(get("domain") or "").strip()})
    return out


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
                "include": str(get("include") or "").strip(),
                "exclude": str(get("exclude") or "").strip(),
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


def write_subscription(sid: Any, payload: Dict[str, Any]) -> Tuple[bool, str]:
    try:
        from app.db.oper.subscribe import SubscribeOper

        result = SubscribeOper().update(int(sid), payload)
        if result is None:
            return False, "订阅不存在"
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:160]


def rule_payload(rule: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    site_id = rule.get("site_id")
    if site_id not in (None, "", 0, "0"):
        payload["sites"] = [site_id]
    if rule.get("include_regex"):
        payload["include"] = str(rule["include_regex"])
    if rule.get("exclude_regex"):
        payload["exclude"] = str(rule["exclude_regex"])
    return payload


def apply_rules(plugin: Any, rule_list: List[Dict[str, Any]],
                subs: List[Dict[str, Any]]) -> Tuple[int, List[str], List[str]]:
    saved = original_settings(plugin)
    done, applied, messages = 0, [], []
    for rule in rule_list:
        matched = [sub for sub in subs if rules.rule_matches(rule, sub)]
        if not matched:
            messages.append(f"{rule.get('target')} 没匹配到订阅")
            continue
        payload = rule_payload(rule)
        if not payload:
            messages.append(f"{rule.get('target')} 没填站点也没有条件，跳过")
            continue
        sub = matched[0]
        sid = sub.get("id")
        key = str(sid)
        if key not in saved:
            saved[key] = {field: _jsonable(sub.get(field))
                          for field in ("sites", "include", "exclude")}
        ok, reason = write_subscription(sid, payload)
        if ok:
            done += 1
            applied.append(key)
            append_log(plugin, f"把「{sub.get('name')}」的站点与过滤规则写进订阅")
        else:
            messages.append(f"《{sub.get('name')}》写入失败：{reason}")
    save_original(plugin, saved)
    return done, applied, messages


def restore_rules(plugin: Any) -> Tuple[int, List[str]]:
    saved = original_settings(plugin)
    done, messages = 0, []
    for key, fields in saved.items():
        if not isinstance(fields, dict):
            continue
        payload = {field: fields.get(field) for field in ("sites", "include", "exclude")}
        ok, reason = write_subscription(key, payload)
        if ok:
            done += 1
        else:
            messages.append(f"订阅 {key} 还原失败：{reason}")
    save_original(plugin, {})
    append_log(plugin, f"还原了 {done} 条订阅原来的设置")
    return done, messages


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
            applied: Optional[List[str]] = None,
            rule_errors: Optional[List[str]] = None) -> Dict[str, Any]:
    applied = list(applied or [])
    sub_rows, issues, rule_rows = [], [], []

    for rule in rule_list:
        matched = [sub for sub in subs if rules.rule_matches(rule, sub)]
        rule_rows.append({
            "source": rule.get("source"),
            "target": rule.get("target"),
            "site_name": rule.get("site_name") or "不限",
            "include_display": rule.get("include_display") or "无",
            "exclude_display": rule.get("exclude_display") or "无",
            "include_regex": rule.get("include_regex") or "",
            "exclude_regex": rule.get("exclude_regex") or "",
            "matched": len(matched),
            "written": bool(matched and str(matched[0].get("id")) in applied),
        })

    for sub in subs:
        picked = next((rule for rule in rule_list if rules.rule_matches(rule, sub)), None)
        matched_records = [item for item in records if record_matches(item, sub)]
        problems = 0
        for record in matched_records:
            if not picked:
                continue
            reasons = []
            site_state = rules.check_site(picked, record.get("torrent_site"), sites)
            if site_state is False:
                reasons.append(f"站点不符：来自 {record.get('torrent_site') or '其他站点'}，"
                               f"规则要 {picked.get('site_name')}")
            title = record.get("torrent_name") or record.get("title") or ""
            reasons.extend(rules.check_title(picked, title))
            if not reasons:
                continue
            problems += 1
            issues.append({
                "sid": sub.get("id"), "name": sub.get("name"),
                "season": sub.get("season"),
                "episode": record.get("episodes") or record.get("seasons") or "-",
                "site": record.get("torrent_site") or "未记录",
                "title": title,
                "hash": record.get("download_hash") or "",
                "downloader": record.get("downloader") or "",
                "reasons": reasons[:6],
            })
        sub_rows.append({
            "id": sub.get("id"), "name": sub.get("name"),
            "season": sub.get("season"), "state": sub.get("state") or "-",
            "sites": sites_text(sub, sites),
            "include": str(sub.get("include") or "")[:60],
            "exclude": str(sub.get("exclude") or "")[:60],
            "rule": picked.get("include_display") if picked else "",
            "written": str(sub.get("id")) in applied,
            "records": len(matched_records), "problems": problems,
        })

    return {
        "subscriptions": sub_rows,
        "rules": rule_rows,
        "issues": issues,
        "errors": list(rule_errors or []),
        "stats": {
            "subscriptions": len(subs),
            "ruled": sum(1 for row in sub_rows if row["rule"]),
            "written": len(applied),
            "records": len(records),
            "problems": len(issues),
        },
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
