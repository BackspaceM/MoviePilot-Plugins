"""MP 备份迁移：导入预览与写回。只读预览、试运行不写库、确认口令后才落库。"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import zipfile
from typing import Any, Dict, List, Optional, Tuple

CONFIRM = "导入"

IMPORT_SECTIONS = (
    "users", "subscriptions", "subscription_history", "sites", "system_config",
    "plugin_configs", "plugin_data", "download_history", "transfer_history", "services",
)
IMPORT_LABELS = {
    "users": "用户", "subscriptions": "订阅", "subscription_history": "订阅历史",
    "sites": "站点", "system_config": "系统配置", "plugin_configs": "插件配置",
    "plugin_data": "插件数据", "download_history": "下载历史",
    "transfer_history": "整理历史", "services": "服务",
}

# 宿主自己维护的列，导入时一律剔掉
DROP_KEYS = ("id", "identity_key", "activity_key", "date", "notification_identity")

FIELD_WHITELIST = {
    "users": (
        "name", "email", "hashed_password", "is_active", "is_superuser",
        "avatar", "is_otp", "otp_secret", "permissions", "settings",
    ),
    "subscriptions": (
        "name", "year", "type", "media_source", "media_id", "tmdbid", "imdbid",
        "tvdbid", "doubanid", "bangumiid", "season", "episode", "episode_group",
        "music_type", "total_episode", "start_episode", "state", "filter",
        "include", "exclude", "quality", "resolution", "effect", "codec",
        "source", "subtitle", "username", "site", "downloader", "download_dir",
        "media_category", "note", "best_version", "current_priority", "lack_episode",
    ),
    "subscription_history": (
        "name", "year", "type", "media_source", "media_id", "tmdbid", "imdbid",
        "tvdbid", "doubanid", "bangumiid", "season", "episode", "episode_group",
        "poster", "backdrop", "vote", "overview", "username",
    ),
    "sites": (
        "name", "domain", "url", "pri", "is_active", "cookie", "ua", "apikey",
        "token", "limit_interval", "limit_count", "limit_seconds", "timeout",
        "proxy", "rssurl", "signurl", "note",
    ),
    "download_history": (
        "download_hash", "title", "name", "year", "type", "media_source", "media_id",
        "tmdbid", "imdbid", "tvdbid", "season", "episode", "site", "seeders",
        "progress", "state", "size", "note", "username",
    ),
    "transfer_history": (
        "src", "dest", "title", "name", "year", "type", "category", "tmdbid",
        "imdbid", "tvdbid", "season", "episode", "image", "size", "note", "username",
    ),
}

# 这些列在目标模型里是整数，快照里混进 bool 会写歪，先掰过来
_INT_COLUMNS = {
    "subscriptions": ("year", "tmdbid", "tvdbid", "doubanid", "bangumiid",
                      "season", "episode", "total_episode", "start_episode", "lack_episode"),
    "subscription_history": ("year", "tmdbid", "tvdbid", "doubanid", "bangumiid",
                             "season", "episode"),
    "sites": ("pri", "limit_interval", "limit_count", "limit_seconds", "timeout"),
    "download_history": ("year", "tmdbid", "imdbid", "tvdbid", "season", "episode",
                         "seeders", "size"),
    "transfer_history": ("year", "tmdbid", "imdbid", "tvdbid", "season", "episode", "size"),
}

UNIQUE_KEY = {
    "users": "name", "sites": "domain", "download_history": "download_hash",
    "transfer_history": "src", "system_config": "key", "services": "key",
    "plugin_configs": "instance_id", "plugin_data": "key",
    "subscription_history": "__row__", "subscriptions": "__identity__",
}

_SECTION_FILES = {
    "users": "users.json", "subscriptions": "subscriptions.json",
    "subscription_history": "subscription_history.json", "sites": "sites.json",
    "system_config": "system_config.json", "plugin_configs": "plugin_configs.json",
    "download_history": "download_history.json", "transfer_history": "transfer_history.json",
    "services": "services.json", "settings": "settings.json",
}


def _as_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return dict(obj)
    for name in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                got = fn()
                if isinstance(got, dict):
                    return got
            except Exception:
                pass
    raw = getattr(obj, "__dict__", None)
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if not str(k).startswith("_")}
    return {}


def _rows_of(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.get("items") or value.get("data") or [])
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and isinstance(value[0], (list, tuple)):
            return list(value[0])
        return list(value)
    items = getattr(value, "items", None)
    if isinstance(items, (list, tuple)):
        return list(items)
    return []


def _host_ready() -> bool:
    try:
        app = sys.modules.get("app") or importlib.import_module("app")
        return getattr(app, "__file__", None) is not None
    except Exception:
        return False


def filter_fields(payload: Any, allowed: Any = None, drop: Any = ()) -> Dict[str, Any]:
    """只留目标模型认识的列，顺手丢掉宿主维护的主键和时间戳。"""
    out: Dict[str, Any] = {}
    blocked = set(DROP_KEYS) | set(drop or ())
    allowed_set = set(allowed) if allowed else None
    for key, value in dict(payload or {}).items():
        if key in blocked:
            continue
        if allowed_set is not None and key not in allowed_set:
            continue
        out[key] = value
    return out


def clean_payload(section: str, payload: Any) -> Dict[str, Any]:
    """按分类白名单过一遍，整数列里混进来的 bool 掰成 0/1。"""
    allowed = FIELD_WHITELIST.get(section)
    out = filter_fields(payload, allowed)
    for key in _INT_COLUMNS.get(section, ()):
        if isinstance(out.get(key), bool):
            out[key] = int(out[key])
    return out


def subscription_identity(item: Any) -> str:
    row = _as_dict(item)
    parts = [row.get(k) for k in ("media_source", "media_id", "season",
                                  "episode_group", "music_type", "type")]
    return "|".join("" if value is None else str(value) for value in parts)


def _row_hash(row: Dict[str, Any]) -> str:
    blob = json.dumps(row, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return "row:" + hashlib.md5(blob).hexdigest()[:12]


def _unique(row: Any, section: str) -> str:
    data = _as_dict(row)
    if section == "subscriptions":
        return "sub:" + subscription_identity(data)
    field = UNIQUE_KEY.get(section, "")
    if field in ("", "__row__"):
        return _row_hash(data)
    value = data.get(field)
    if value in (None, ""):
        return _row_hash(data)
    return field + ":" + str(value)


def _brief(row: Any) -> str:
    data = _as_dict(row)
    for key in ("name", "title", "domain", "key", "src", "download_hash",
                "instance_id", "plugin_id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return "（无标题）"


def _clean_sections(sections: Any) -> List[str]:
    if sections is None or not isinstance(sections, (list, tuple, set)):
        return list(IMPORT_SECTIONS)
    return [name for name in IMPORT_SECTIONS if name in sections]


def read_snapshot(snapshot_path: Any) -> Dict[str, Any]:
    """把 zip 里各分项规整成列表；缺项、坏 json、坏 zip 都不让它炸。"""
    result: Dict[str, Any] = {
        "ok": False, "message": "", "file": str(snapshot_path),
        "manifest": {}, "sections": {name: [] for name in IMPORT_SECTIONS},
    }
    try:
        with zipfile.ZipFile(snapshot_path) as zf:
            names = set(zf.namelist())

            def load(arc: str, default: Any = None) -> Any:
                if arc not in names:
                    return default
                try:
                    return json.loads(zf.read(arc))
                except Exception:
                    return default

            manifest = load("manifest.json", {}) or {}
            result["manifest"] = manifest if isinstance(manifest, dict) else {}

            for section, filename in _SECTION_FILES.items():
                data = load(filename, [])
                if isinstance(data, dict):
                    rows = [{"key": str(k), "value": v} for k, v in data.items()]
                elif isinstance(data, list):
                    rows = [row for row in data if isinstance(row, dict)]
                else:
                    rows = []
                result["sections"][section] = rows

            # 开了含凭据开关才有这个文件，按 domain/name 合并回站点行
            secrets = load("site_secrets.json", []) or []
            if isinstance(secrets, list) and secrets:
                index: Dict[str, Dict[str, Any]] = {}
                for row in result["sections"]["sites"]:
                    key = str(row.get("domain") or row.get("name") or "")
                    if key:
                        index[key] = row
                for sec in secrets:
                    if not isinstance(sec, dict):
                        continue
                    key = str(sec.get("domain") or sec.get("name") or "")
                    target = index.get(key)
                    if target is None:
                        continue
                    for field in ("cookie", "ua", "apikey", "token"):
                        if sec.get(field) not in (None, ""):
                            target[field] = sec[field]

            plugin_data: List[Dict[str, Any]] = []
            for arc in sorted(names):
                if not arc.startswith("plugin_data/") or not arc.endswith(".json"):
                    continue
                obj = load(arc, None)
                rel = arc[len("plugin_data/"):-5]
                parts = rel.split("/")
                pid = parts[0] if len(parts) > 1 else ""
                key = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
                plugin_data.append({"plugin_id": pid, "key": key,
                                    "value": obj, "file": arc})
            result["sections"]["plugin_data"] = plugin_data
    except Exception as err:
        result["message"] = f"快照打不开：{type(err).__name__}: {err}"
        result["sections"] = {name: [] for name in IMPORT_SECTIONS}
        return result
    result["ok"] = True
    return result


def _oper_for(section: str) -> Any:
    try:
        if section == "users":
            from app.db.oper.user import UserOper
            return UserOper()
        if section == "subscriptions":
            from app.db.oper.subscribe import SubscribeOper
            return SubscribeOper()
        if section == "subscription_history":
            from app.db.oper.subscribehistory import SubscribeHistoryOper
            return SubscribeHistoryOper()
        if section == "sites":
            from app.db.oper.site import SiteOper
            return SiteOper()
        if section == "download_history":
            from app.db.oper.downloadhistory import DownloadHistoryOper
            return DownloadHistoryOper()
        if section == "transfer_history":
            from app.db.oper.transferhistory import TransferHistoryOper
            return TransferHistoryOper()
        if section == "plugin_configs":
            from app.db.oper.plugininstance import PluginInstanceOper
            return PluginInstanceOper()
        if section == "plugin_data":
            from app.db.oper.plugindata import PluginDataOper
            return PluginDataOper()
        if section in ("system_config", "services"):
            from app.db.oper.systemconfig import SystemConfigOper
            return SystemConfigOper()
    except Exception:
        return None
    return None


def _read_existing(oper: Any, section: str) -> Optional[List[Any]]:
    if section in ("system_config", "services"):
        fn = getattr(oper, "all", None)
        if callable(fn):
            try:
                raw = fn()
            except Exception:
                raw = None
            if isinstance(raw, dict):
                return [{"key": k, "value": v} for k, v in raw.items()]
            if isinstance(raw, (list, tuple)):
                return [row for row in raw if isinstance(row, dict)]
        return None
    if section == "plugin_configs":
        for attr in ("list", "get_all"):
            fn = getattr(oper, attr, None)
            if callable(fn):
                try:
                    rows = _rows_of(fn())
                except Exception:
                    continue
                if rows:
                    return rows
        return None
    if section == "plugin_data":
        return None
    for attr in ("list", "query", "list_by_page", "get_all"):
        fn = getattr(oper, attr, None)
        if not callable(fn):
            continue
        for args in ((), ({"page": 1, "count": 5000},)):
            try:
                rows = _rows_of(fn(*args))
            except Exception:
                continue
            if rows:
                return rows
            break
    return None


def _current_state(section: str) -> Tuple[Optional[int], Optional[set]]:
    """读得到就给 (现有条数, 唯一键集合)，读不到给 (None, None) 让页面标无法核对。"""
    if not _host_ready():
        return None, None
    oper = _oper_for(section)
    if oper is None:
        return None, None
    try:
        rows = _read_existing(oper, section)
        if rows is None:
            return None, None
        return len(rows), {_unique(row, section) for row in rows}
    except Exception:
        return None, None


def preview(snapshot_path: Any, sections: Any = None) -> Dict[str, Any]:
    """只读预览：逐类数条数、和当前库比一比，绝不写任何东西。"""
    wanted = _clean_sections(sections)
    snap = read_snapshot(snapshot_path)
    result: Dict[str, Any] = {
        "ok": snap["ok"], "message": "", "file": snap["file"],
        "schema": (snap["manifest"] or {}).get("schema", 1),
        "contains_secrets": bool((snap["manifest"] or {}).get("contains_secrets")),
        "sections": [],
        "totals": {"total": 0, "will_add": 0, "will_skip": 0, "will_overwrite": 0},
    }
    if not snap["ok"]:
        result["message"] = snap["message"]
        return result

    for section in wanted:
        rows = snap["sections"].get(section) or []
        total = len(rows)
        current, keys = _current_state(section)
        if keys is None:
            status, reason = "无法核对", "当前库读不到，条数先照快照算"
            current_text: Any = "无法核对"
            add, skip, over = total, 0, 0
        else:
            matched = sum(1 for row in rows if _unique(row, section) in keys)
            status, reason = "ok", ""
            current_text = current
            add, skip, over = total - matched, matched, matched
        result["sections"].append({
            "key": section, "label": IMPORT_LABELS.get(section, section),
            "total": total, "current": current_text, "will_add": add,
            "will_skip": skip, "will_overwrite": over, "status": status, "reason": reason,
        })
        result["totals"]["total"] += total
        result["totals"]["will_add"] += add
        result["totals"]["will_skip"] += skip
        result["totals"]["will_overwrite"] += over

    result["message"] = (f"预览 {snap['file']}：{len(wanted)} 类共 {result['totals']['total']} 条，"
                         f"默认跳过策略下预计新增 {result['totals']['will_add']} 条。全程只读，没改数据。")
    return result


def _existing_map(oper: Any, section: str) -> Optional[Dict[str, Any]]:
    rows = _read_existing(oper, section)
    if rows is None:
        return None
    out: Dict[str, Any] = {}
    for row in rows:
        data = _as_dict(row)
        out[_unique(data, section)] = data.get("id")
    return out


def _apply(oper: Any, section: str, rows: List[Dict[str, Any]], strategy: str,
           stat: Dict[str, Any], log: List[str], add_fn: Any,
           update_fn: Any = None, id_map: Optional[Dict[str, Any]] = None) -> None:
    label = stat["label"]
    for row in rows:
        payload = clean_payload(section, row)
        key = _unique(row, section)
        try:
            if id_map is None:
                stat["skipped"] += 1
                log.append(f"[{label}] 读不到现有记录，谨慎跳过：{key}")
                continue
            if key in id_map:
                if strategy == "overwrite" and update_fn is not None:
                    update_fn(id_map[key], payload)
                    stat["overwritten"] += 1
                    log.append(f"[{label}] 覆盖：{key}")
                else:
                    stat["skipped"] += 1
                    log.append(f"[{label}] 已存在，跳过：{key}")
                continue
            add_fn(row, payload)
            stat["added"] += 1
            log.append(f"[{label}] 新增：{key}")
        except Exception as err:
            stat["failed"] += 1
            log.append(f"[{label}] 没写成 {key}：{type(err).__name__}: {err}")


def _write_users(rows: List[Dict[str, Any]], strategy: str,
                 stat: Dict[str, Any], log: List[str]) -> None:
    from app.db.oper.user import UserOper

    oper = UserOper()
    get_by_name = getattr(oper, "get_by_name", None)
    label = stat["label"]
    for row in rows:
        payload = clean_payload("users", row)
        name = payload.get("name")
        try:
            existing = None
            if callable(get_by_name) and name:
                try:
                    existing = get_by_name(name)
                except Exception:
                    existing = None
            if existing:
                rid = _as_dict(existing).get("id")
                if strategy == "overwrite" and rid is not None:
                    oper.update(rid, payload)
                    stat["overwritten"] += 1
                    log.append(f"[{label}] 覆盖用户：{name}")
                else:
                    stat["skipped"] += 1
                    log.append(f"[{label}] 用户已存在，跳过：{name}")
                continue
            oper.add(**payload)
            stat["added"] += 1
            log.append(f"[{label}] 新增用户：{name}")
        except Exception as err:
            stat["failed"] += 1
            log.append(f"[{label}] 没写成 {name}：{type(err).__name__}: {err}")


def _write_subscriptions(rows: List[Dict[str, Any]], strategy: str,
                         stat: Dict[str, Any], log: List[str]) -> None:
    from app.db.oper.subscribe import SubscribeOper

    oper = SubscribeOper()
    id_map = _existing_map(oper, "subscriptions")

    def add(row: Dict[str, Any], payload: Dict[str, Any]) -> None:
        oper.add(subscription_identity(row), payload, username=payload.get("username"))

    _apply(oper, "subscriptions", rows, strategy, stat, log, add,
           lambda rid, payload: oper.update(rid, payload), id_map)


def _write_subscription_history(rows: List[Dict[str, Any]], strategy: str,
                                stat: Dict[str, Any], log: List[str]) -> None:
    from app.db.oper.subscribehistory import SubscribeHistoryOper

    oper = SubscribeHistoryOper()
    exists = getattr(oper, "exists", None)
    label = stat["label"]
    for row in rows:
        payload = clean_payload("subscription_history", row)
        try:
            hit = False
            if callable(exists):
                try:
                    hit = bool(exists(**payload))
                except Exception:
                    hit = False
            if hit:
                stat["skipped"] += 1
                log.append(f"[{label}] 已存在，跳过：{_brief(row)}")
                continue
            oper.add(payload)
            stat["added"] += 1
            log.append(f"[{label}] 新增：{_brief(row)}")
        except Exception as err:
            stat["failed"] += 1
            log.append(f"[{label}] 没写成 {_brief(row)}：{type(err).__name__}: {err}")


def _write_sites(rows: List[Dict[str, Any]], strategy: str,
                 stat: Dict[str, Any], log: List[str]) -> None:
    from app.db.oper.site import SiteOper

    oper = SiteOper()
    id_map = _existing_map(oper, "sites")

    def add(row: Dict[str, Any], payload: Dict[str, Any]) -> None:
        got = oper.add(**payload)
        if isinstance(got, tuple) and got and got[0] is False:
            raise RuntimeError(str(got[1] if len(got) > 1 else "站点新增被拒"))

    _apply(oper, "sites", rows, strategy, stat, log, add,
           lambda rid, payload: oper.update(rid, payload), id_map)


def _write_history(section: str, oper: Any, rows: List[Dict[str, Any]],
                   strategy: str, stat: Dict[str, Any], log: List[str]) -> None:
    label = stat["label"]
    add_fn = getattr(oper, "add", None)
    if not callable(add_fn):
        stat["status"] = "unavailable"
        stat["reason"] = "没有 add 接口，整类跳过"
        log.append(f"[{label}] {stat['reason']}")
        return
    id_map = _existing_map(oper, section)
    for row in rows:
        payload = clean_payload(section, row)
        key = _unique(row, section)
        if id_map is None:
            stat["skipped"] += 1
            log.append(f"[{label}] 读不到现有记录，谨慎跳过：{key}")
            continue
        if key in id_map:
            stat["skipped"] += 1
            log.append(f"[{label}] 已存在，跳过：{key}")
            continue
        try:
            add_fn(**payload)
            stat["added"] += 1
            log.append(f"[{label}] 新增：{key}")
        except Exception as err:
            stat["failed"] += 1
            log.append(f"[{label}] 没写成 {key}：{type(err).__name__}: {err}")


def _write_download_history(rows: List[Dict[str, Any]], strategy: str,
                            stat: Dict[str, Any], log: List[str]) -> None:
    from app.db.oper.downloadhistory import DownloadHistoryOper

    _write_history("download_history", DownloadHistoryOper(), rows, strategy, stat, log)


def _write_transfer_history(rows: List[Dict[str, Any]], strategy: str,
                            stat: Dict[str, Any], log: List[str]) -> None:
    from app.db.oper.transferhistory import TransferHistoryOper

    _write_history("transfer_history", TransferHistoryOper(), rows, strategy, stat, log)


def _write_plugin_configs(rows: List[Dict[str, Any]], strategy: str,
                          stat: Dict[str, Any], log: List[str]) -> None:
    from app.db.oper.plugininstance import PluginInstanceOper

    oper = PluginInstanceOper()
    method = next((name for name in ("save_config_data", "set_config_data", "save")
                   if callable(getattr(oper, name, None))), None)
    label = stat["label"]
    if not method:
        stat["status"] = "unavailable"
        stat["reason"] = "没找到可用的配置写入方法"
        log.append(f"[{label}] {stat['reason']}")
        return
    fn = getattr(oper, method)
    for row in rows:
        iid = row.get("instance_id")
        cfg = row.get("config_data")
        src = row.get("source_plugin_id") or iid
        try:
            if method == "save_config_data":
                fn(instance_id=iid, source_plugin_id=src, config_data=cfg)
            elif method == "set_config_data":
                fn(iid, cfg)
            else:
                fn(iid, cfg)
            stat["added"] += 1
            log.append(f"[{label}] 写回配置：{iid}")
        except Exception as err:
            stat["failed"] += 1
            log.append(f"[{label}] 没写成 {iid}：{type(err).__name__}: {err}")


def _write_plugin_data(rows: List[Dict[str, Any]], strategy: str,
                       stat: Dict[str, Any], log: List[str]) -> None:
    from app.db.oper.plugindata import PluginDataOper

    oper = PluginDataOper()
    save = getattr(oper, "save", None)
    label = stat["label"]
    if not callable(save):
        stat["status"] = "unavailable"
        stat["reason"] = "没找到 save 接口，整类跳过"
        log.append(f"[{label}] {stat['reason']}")
        return
    for row in rows:
        key = row.get("key")
        if key in (None, ""):
            stat["skipped"] += 1
            continue
        try:
            save(row.get("plugin_id"), key, row.get("value"))
            stat["added"] += 1
            log.append(f"[{label}] 写回：{row.get('plugin_id')} / {key}")
        except Exception as err:
            stat["failed"] += 1
            log.append(f"[{label}] 没写成 {key}：{type(err).__name__}: {err}")


def _system_setter(log: List[str], label: str) -> Any:
    from app.db.oper.systemconfig import SystemConfigOper

    oper = SystemConfigOper()
    set_fn = getattr(oper, "set", None)
    if not callable(set_fn):
        log.append(f"[{label}] 没找到 set 接口，整类跳过")
        return None
    return oper, set_fn


def _write_system_config(rows: List[Dict[str, Any]], strategy: str,
                         stat: Dict[str, Any], log: List[str]) -> None:
    label = stat["label"]
    got = _system_setter(log, label)
    if got is None:
        stat["status"] = "unavailable"
        stat["reason"] = "没找到 set 接口"
        return
    oper, set_fn = got
    for row in rows:
        key, value = row.get("key"), row.get("value")
        if not key or value in (None, "", "***"):
            stat["skipped"] += 1
            log.append(f"[{label}] 空值或已脱敏，跳过：{key}")
            continue
        try:
            set_fn(str(key), value)
            stat["added"] += 1
            log.append(f"[{label}] 写回：{key}")
        except Exception as err:
            stat["failed"] += 1
            log.append(f"[{label}] 没写成 {key}：{type(err).__name__}: {err}")
    try:
        oper.load_snapshot()
    except Exception:
        pass


def _write_services(rows: List[Dict[str, Any]], strategy: str,
                    stat: Dict[str, Any], log: List[str]) -> None:
    label = stat["label"]
    got = _system_setter(log, label)
    if got is None:
        stat["status"] = "unavailable"
        stat["reason"] = "没找到 set 接口"
        return
    oper, set_fn = got
    media: Dict[str, Any] = {}
    down: Dict[str, Any] = {}
    for row in rows:
        cfg = row.get("config")
        if not isinstance(cfg, dict):
            continue
        name = str(row.get("name") or "")
        if "媒体服务器" in str(row.get("kind") or ""):
            media[name] = cfg
        else:
            down[name] = cfg
    for key, value in (("MediaServers", media), ("Downloaders", down)):
        if not value:
            continue
        try:
            set_fn(key, value)
            stat["added"] += 1
            log.append(f"[{label}] 写回系统配置键：{key}")
        except Exception as err:
            stat["failed"] += 1
            log.append(f"[{label}] 没写成 {key}：{type(err).__name__}: {err}")
    try:
        oper.load_snapshot()
    except Exception:
        pass


# 存名字不存函数引用，运行时现取，逐类替换或打桩都好使
_WRITERS = {
    "users": "_write_users",
    "subscriptions": "_write_subscriptions",
    "subscription_history": "_write_subscription_history",
    "sites": "_write_sites",
    "system_config": "_write_system_config",
    "plugin_configs": "_write_plugin_configs",
    "plugin_data": "_write_plugin_data",
    "download_history": "_write_download_history",
    "transfer_history": "_write_transfer_history",
    "services": "_write_services",
}


def _dry_stat(section: str, rows: List[Dict[str, Any]], strategy: str,
              log: List[str]) -> Dict[str, Any]:
    label = IMPORT_LABELS.get(section, section)
    stat: Dict[str, Any] = {
        "key": section, "label": label, "status": "dry_run",
        "planned": len(rows), "added": 0, "skipped": 0, "overwritten": 0,
        "failed": 0, "reason": "试运行，没碰数据库",
    }
    shown = 0
    for row in rows:
        if shown >= 50:
            log.append(f"[{label}] 试运行 · 还有 {len(rows) - shown} 条，动作同上")
            break
        action = "覆盖已存在" if strategy == "overwrite" else "跳过已存在／新增"
        log.append(f"[{label}] 试运行 · {_brief(row)} · 将{action}")
        shown += 1
    return stat


def _write_section(section: str, rows: List[Dict[str, Any]], strategy: str,
                   log: List[str]) -> Dict[str, Any]:
    label = IMPORT_LABELS.get(section, section)
    stat: Dict[str, Any] = {
        "key": section, "label": label, "status": "ok", "planned": len(rows),
        "added": 0, "skipped": 0, "overwritten": 0, "failed": 0, "reason": "",
    }
    try:
        if not _host_ready():
            stat["status"] = "unavailable"
            stat["reason"] = "离线或桩环境，宿主数据库不可用，整类跳过"
            stat["skipped"] = len(rows)
            log.append(f"[{label}] {stat['reason']}")
            return stat
        writer = globals().get(_WRITERS.get(section) or "")
        if not callable(writer):
            stat["status"] = "skipped"
            stat["reason"] = "这类没接写回入口"
            log.append(f"[{label}] {stat['reason']}")
            return stat
        writer(rows, strategy, stat, log)
        if stat["failed"] and not (stat["added"] or stat["overwritten"]):
            stat["status"] = "error"
            stat["reason"] = stat["reason"] or "这一类全部没写成功"
    except Exception as err:
        reason = f"{type(err).__name__}: {err}"
        stat["status"] = "error"
        stat["reason"] = reason
        log.append(f"[{label}] 这一类没写成功：{reason}")
    return stat


def restore(snapshot_path: Any, sections: Any = None, strategy: str = "skip",
            dry_run: bool = True, confirm: str = "") -> Dict[str, Any]:
    """默认试运行；只有 dry_run=False 且 confirm 等于确认口令才真的写回。"""
    wanted = _clean_sections(sections)
    strategy = "overwrite" if str(strategy) == "overwrite" else "skip"
    write_back = (not bool(dry_run)) and confirm == CONFIRM
    result: Dict[str, Any] = {
        "ok": True, "message": "", "file": str(snapshot_path),
        "mode": "执行" if write_back else "试运行",
        "dry_run": not write_back, "confirm_ok": confirm == CONFIRM,
        "strategy": strategy, "sections": [], "log": [],
        "totals": {"planned": 0, "added": 0, "skipped": 0, "overwritten": 0, "failed": 0},
    }
    snap = read_snapshot(snapshot_path)
    if not snap["ok"]:
        result["ok"] = False
        result["message"] = snap["message"]
        result["log"].append(snap["message"])
        return result

    for section in wanted:
        rows = snap["sections"].get(section) or []
        if write_back:
            stat = _write_section(section, rows, strategy, result["log"])
        else:
            stat = _dry_stat(section, rows, strategy, result["log"])
        result["sections"].append(stat)
        for key in result["totals"]:
            result["totals"][key] += int(stat.get(key, 0) or 0)

    totals = result["totals"]
    if write_back:
        result["message"] = (f"执行完成：打算处理 {totals['planned']} 条，新增 {totals['added']} · "
                             f"跳过 {totals['skipped']} · 覆盖 {totals['overwritten']} · "
                             f"失败 {totals['failed']}。逐条结果看下面的日志。")
    else:
        result["message"] = (f"试运行：将处理 {totals['planned']} 条，一条都没写回。"
                             f"确认要写回请点「执行导入」并带确认口令「{CONFIRM}」。")
    return result


def import_guide(strategy: str = "skip") -> str:
    label = "覆盖已存在" if strategy == "overwrite" else "跳过已存在（默认）"
    return (
        "导入迁移怎么用：\n"
        "1. 先点某份快照的「预览导入」，表格会把每类现有条数、将新增、将跳过、将覆盖列清楚，"
        "这一步只读，零风险。\n"
        "2. 默认走试运行，只列出将要执行的动作，不会往数据库写任何东西。\n"
        f"3. 真要写回，点「执行导入」并带确认口令「{CONFIRM}」，冲突策略是「{label}」。\n"
        "4. 用户密码哈希、站点凭据这类敏感数据会原样进快照，含凭据的快照含明文，别外传，"
        "用完及时从备份目录清掉。\n"
        "5. 某一类写失败不会拖垮其他类，日志里逐条都有交代。"
    )
