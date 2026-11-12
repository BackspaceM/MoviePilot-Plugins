"""MP 备份迁移：快照打包、轮转、漂移对比与恢复清单。"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_VERSION = "1.0.0"
SNAPSHOT_PREFIX = "mpbackup_"
RESTORE_PREFIX = "restore_"
CLEAN_CONFIRM = "CLEAN_MPBACKUP"

SETTINGS_KEYS = (
    "CONFIG_DIR", "PLUGIN_DATA_PATH", "DOWNLOAD_PATH", "DOWNLOAD_SAVE_PATH",
    "LIBRARY_PATHS", "MEDIA_PATH", "TRANSFER_PATH",
)

TOGGLEABLE = ("subscriptions", "download_history", "transfer_history",
              "sites", "services", "plugin_data")
ALWAYS_ON = ("settings", "plugins")
ALL_SOURCES = ("settings", "subscriptions", "download_history", "transfer_history",
               "sites", "services", "plugins", "plugin_data")
HOST_SOURCES = ("settings", "subscriptions", "download_history", "transfer_history",
                "sites", "services", "plugins")
DIFF_SECTIONS = ("subscriptions", "sites", "services", "plugins")

DIFF_KEYS = {
    "subscriptions": ("id", "tmdbid", "name", "title"),
    "sites": ("id", "name", "domain", "url"),
    "services": ("name", "id", "type", "url"),
    "plugins": ("id", "plugin_id", "name"),
}
SECTION_LABELS = {
    "subscriptions": "订阅", "sites": "站点", "services": "服务", "plugins": "插件",
}

SECTION_OPTIONS = (
    ("subscriptions", "订阅"), ("download_history", "下载历史"),
    ("transfer_history", "整理历史"), ("sites", "站点"),
    ("services", "服务"), ("plugin_data", "插件数据"),
)
DEFAULT_SECTIONS = ("subscriptions", "sites", "services", "plugin_data")

CRON_CHOICES = (
    ("0 2 * * *", "每天凌晨 2 点"),
    ("0 4 * * *", "每天凌晨 4 点"),
    ("0 5 * * *", "每天凌晨 5 点"),
    ("0 8 * * *", "每天早上 8 点"),
    ("0 12 * * *", "每天中午 12 点"),
    ("0 22 * * *", "每天晚上 10 点"),
)
DEFAULT_CRON = "0 4 * * *"

HISTORY_LIMIT = 2000
PAGE_SIZE = 200
MAX_PAGES = 40
PLUGIN_FILE_LIMIT = 1024 * 1024
PLUGIN_FILE_COUNT = 200
PLUGIN_SCAN_CAP = 1500

_SECRET_WORDS = (
    "cookie", "token", "password", "api_key", "secret", "passkey",
    "authorization", "ua", "passwd", "pwd", "apikey", "user_agent",
    "set_cookie", "access_key", "private_key", "client_secret",
    "auth", "credential", "accesstoken", "refreshtoken",
)
_SECRET_KEYS = {re.sub(r"[^a-z0-9]", "", w) for w in _SECRET_WORDS}
_INLINE_RE = re.compile(
    r"(?i)\b(authorization|set-cookie|cookie|token|password|passwd|pwd|"
    r"api[_-]?key|secret|passkey|user-agent|user_agent|useragent|ua|"
    r"access[_-]?key|private[_-]?key|accesstoken|refreshtoken|auth)\b"
    r"\s*[:=]\s*[^\r\n]*"
)

_GUIDE = """# MP 备份迁移 · 恢复清单

清单对应最近的快照 {snapshot}，整理于 {time}。
插件只导出清单、不写回数据库：下面每个文件都要由你在新的 MoviePilot 环境里
对照着手动恢复，插件不会替你改配置，也不会往数据库里写任何记录。
Cookie、Token、密码、Passkey、Authorization、UA 这类字段已经剔除或打码，
站点和服务的密钥请重新到对应后台获取后再填。

建议按下面的顺序来，先对齐目录，再挂服务，最后补订阅和历史：

1. settings.json
   几个关键目录（配置目录、插件数据目录、下载目录、媒体库目录等）。
   先把新环境的挂载和路径设置对齐，后面的路径类数据才对得上。

2. sites.json
   站点清单，只留了 name / domain / url / pri / is_active。
   照着在站点管理里逐个加回去，Cookie / UA / Passkey 重新获取。

3. services.json
   媒体服务器与下载器的连接配置，已脱敏。
   照着在设置里重新连一遍，密钥类字段重新填。

4. subscriptions.json
   订阅记录。逐条补回，顺手核对季集和状态。

5. plugins.json
   装过哪些插件。按清单去插件市场重装并启用。

6. download_history.json / transfer_history.json
   下载与整理历史，只用来核对，MoviePilot 没有直接导入的入口。

7. plugin_data/
   各插件自己存的小 JSON。装好对应插件后，按需对照放回数据目录。

弄完以后建议立刻手动生成一份新快照，用「对比」看看和这份差在哪。
"""


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_secret_key(key: Any) -> bool:
    return _norm_key(key) in _SECRET_KEYS


def as_dict(obj: Any) -> Dict[str, Any]:
    """把 dict / Pydantic / ORM 对象统一成普通 dict。"""
    if isinstance(obj, dict):
        return dict(obj)
    for name in ("model_dump", "to_dict"):
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                got = fn()
                if isinstance(got, dict):
                    return got
            except Exception:
                pass
    fn = getattr(obj, "dict", None)
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


def mask(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[key] = "***" if _is_secret_key(key) else mask(item)
        return out
    if isinstance(value, (list, tuple, set)):
        return [mask(item) for item in value]
    if isinstance(value, str):
        return _INLINE_RE.sub(lambda m: m.group(1) + ": ***", value)
    plain = as_dict(value)
    if plain:
        return mask(plain)
    return value


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return str(value)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


def human_bytes(num: Optional[float]) -> str:
    if num is None or num < 0:
        return "—"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def cron_label(cron: str) -> str:
    for value, label in CRON_CHOICES:
        if value == cron:
            return label
    return cron


def clean_cron(raw: Any) -> str:
    value = str(raw or "").strip()
    for cron, _ in CRON_CHOICES:
        if cron == value:
            return value
    return DEFAULT_CRON


def clean_keep(raw: Any) -> int:
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 7


def clean_sections(raw: Any) -> List[str]:
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_SECTIONS)
    return [name for name in TOGGLEABLE if name in raw]


def host_ready() -> bool:
    # 离线桩里的 app 是临时造的假模块，没有 __file__；真宿主才带得上
    try:
        app = sys.modules.get("app") or importlib.import_module("app")
        return getattr(app, "__file__", None) is not None
    except Exception:
        return False


def _page_items(page: Any) -> List[Any]:
    if isinstance(page, dict):
        return list(page.get("items") or page.get("data") or [])
    items = getattr(page, "items", None)
    if isinstance(items, (list, tuple)):
        return list(items)
    if isinstance(page, (list, tuple)):
        return list(page)
    return []


def _settings() -> Dict[str, Any]:
    from app.sdk.config import settings

    out: Dict[str, Any] = {}
    for key in SETTINGS_KEYS:
        try:
            out[key] = _plain(getattr(settings, key, None))
        except Exception:
            out[key] = None
    return out


def _subscriptions() -> List[Dict[str, Any]]:
    from app.sdk import queries

    fn = getattr(queries, "list_subscriptions", None)
    if not callable(fn):
        raise RuntimeError("list_subscriptions 不可用")
    rows: List[Dict[str, Any]] = []
    page_no = 1
    while page_no <= MAX_PAGES:
        batch = _page_items(fn(page={"page": page_no, "count": PAGE_SIZE}))
        if not batch:
            break
        rows.extend(as_dict(item) for item in batch)
        if len(batch) < PAGE_SIZE:
            break
        page_no += 1
    return rows


def _history(method: str) -> List[Dict[str, Any]]:
    from app.sdk import queries

    fn = getattr(queries, method, None)
    if not callable(fn):
        raise RuntimeError(f"{method} 不可用")
    rows: List[Dict[str, Any]] = []
    page_no = 1
    while len(rows) < HISTORY_LIMIT and page_no <= MAX_PAGES:
        batch = _page_items(fn(page={"page": page_no, "count": PAGE_SIZE}))
        if not batch:
            break
        rows.extend(as_dict(item) for item in batch)
        if len(batch) < PAGE_SIZE:
            break
        page_no += 1
    return rows[:HISTORY_LIMIT]


def _sites() -> List[Dict[str, Any]]:
    from app.db.oper.site import SiteOper

    rows = SiteOper().list_order_by_pri() or []
    keep = ("name", "domain", "url", "pri", "is_active")
    return [{key: as_dict(row).get(key) for key in keep} for row in rows]


def _services() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    module = importlib.import_module("app.sdk.services")
    for cls, kind in (("MediaServerHelper", "媒体服务器"), ("DownloaderHelper", "下载器")):
        try:
            configs = getattr(module, cls)().get_configs() or {}
            if isinstance(configs, dict):
                pairs = list(configs.items())
            elif isinstance(configs, (list, tuple)):
                pairs = [(f"{kind}-{i}", cfg) for i, cfg in enumerate(configs)]
            else:
                pairs = []
            for name, cfg in pairs:
                detail = as_dict(cfg)
                out.append({"name": name, "kind": kind, "config": detail or _plain(cfg)})
        except Exception:
            continue
    return out


def _plugins() -> List[Dict[str, Any]]:
    from app.sdk.plugin.manager import PluginManager

    manager = PluginManager()
    ids: List[Any] = []
    installed: List[Any] = []
    for attr, bucket in (("get_plugin_ids", ids), ("get_installed_plugins", installed)):
        try:
            fn = getattr(manager, attr, None)
            if callable(fn):
                bucket.extend(list(fn() or []))
        except Exception:
            pass
    if installed:
        return [as_dict(item) for item in installed]
    return [{"id": str(item)} for item in ids]


def _under(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except Exception:
        return False


def _plugin_data(data_path: str, exclude_dir: Path) -> List[Tuple[str, Any]]:
    root = Path(data_path)
    if not root.is_dir():
        return []
    out: List[Tuple[str, Any]] = []
    scanned = 0
    for path in root.rglob("*.json"):
        scanned += 1
        if scanned > PLUGIN_SCAN_CAP or len(out) >= PLUGIN_FILE_COUNT:
            break
        try:
            if path.name.startswith((SNAPSHOT_PREFIX, RESTORE_PREFIX)):
                continue
            if _under(path, exclude_dir):
                continue
            if path.stat().st_size > PLUGIN_FILE_LIMIT:
                continue
            obj = json.loads(path.read_bytes())
            rel = path.relative_to(root)
            out.append(("plugin_data/" + str(rel).replace("\\", "/"), mask(obj)))
        except Exception:
            continue
    return out


def _collect_source(name: str, sections: List[str], data_path: str,
                    exclude_dir: Path) -> Tuple[Any, Dict[str, Any]]:
    if name not in ALWAYS_ON and name not in sections:
        return [], {"status": "skipped", "reason": "设置里没勾选", "count": 0}
    if name in HOST_SOURCES and not host_ready():
        return [], {"status": "unavailable", "reason": "离线环境，宿主 SDK 不可用", "count": 0}
    collectors = {
        "settings": _settings,
        "subscriptions": _subscriptions,
        "download_history": lambda: _history("list_download_history"),
        "transfer_history": lambda: _history("list_transfer_history"),
        "sites": _sites,
        "services": _services,
        "plugins": _plugins,
        "plugin_data": lambda: _plugin_data(data_path, exclude_dir),
    }
    try:
        data = collectors[name]()
        count = len(data) if isinstance(data, (list, dict)) else 0
        return data, {"status": "ok", "reason": "", "count": count}
    except Exception as err:
        return [], {"status": "unavailable", "reason": f"{type(err).__name__}: {err}", "count": 0}


def write_snapshot(backup_dir: Path, data_path: str, sections: List[str],
                   keep: int) -> Dict[str, Any]:
    """写一份 zip 快照，返回文件名和 manifest；不负责轮转。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{SNAPSHOT_PREFIX}{stamp}.zip"
    snapshot_path = backup_dir / filename
    exclude = backup_dir.resolve()

    collected: Dict[str, Any] = {}
    sources: Dict[str, Any] = {}
    for name in ALL_SOURCES:
        data, info = _collect_source(name, sections, data_path, exclude)
        collected[name] = data
        sources[name] = info

    manifest = {
        "plugin": "MP 备份迁移",
        "version": PLUGIN_VERSION,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file": filename,
        "keep": keep,
        "sections": list(sections),
        "sources": sources,
        "limits": {
            "history": HISTORY_LIMIT,
            "plugin_file_bytes": PLUGIN_FILE_LIMIT,
            "plugin_file_count": PLUGIN_FILE_COUNT,
        },
    }

    with zipfile.ZipFile(snapshot_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ALL_SOURCES:
            if name == "plugin_data":
                continue
            payload = collected[name] if isinstance(collected[name], (list, dict)) else []
            zf.writestr(name + ".json", _dump(mask(payload)))
        zf.writestr("plugin_data/", "")
        for arc, obj in (collected["plugin_data"] or []):
            zf.writestr(arc, _dump(obj))
        zf.writestr("manifest.json", _dump(manifest))

    return {
        "ok": True,
        "file": filename,
        "path": str(snapshot_path),
        "manifest": manifest,
        "sources": sources,
    }


def snapshot_files(backup_dir: Path) -> List[Path]:
    if not backup_dir.is_dir():
        return []
    files = [p for p in backup_dir.iterdir()
             if p.is_file() and p.name.startswith(SNAPSHOT_PREFIX) and p.name.endswith(".zip")]
    files.sort(key=lambda p: p.name, reverse=True)
    return files


def rotate(backup_dir: Path, keep: int) -> List[str]:
    keep = max(1, int(keep))
    removed: List[str] = []
    for path in snapshot_files(backup_dir)[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except Exception:
            pass
    return removed


def list_snapshots(backup_dir: Path) -> List[Dict[str, Any]]:
    rows = []
    for path in snapshot_files(backup_dir):
        try:
            stat = path.stat()
            rows.append({
                "name": path.name,
                "size": stat.st_size,
                "size_text": human_bytes(stat.st_size),
                "mtime": _dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception:
            continue
    return rows


def load_manifest(path: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as zf:
            return json.loads(zf.read("manifest.json"))
    except Exception:
        return {}


def load_sections(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {name: [] for name in DIFF_SECTIONS}
    try:
        with zipfile.ZipFile(path) as zf:
            for name in DIFF_SECTIONS:
                try:
                    data = json.loads(zf.read(name + ".json"))
                    if isinstance(data, list):
                        out[name] = [item for item in data if isinstance(item, dict)]
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _row_key(item: Dict[str, Any], fields: Tuple[str, ...]) -> str:
    for field in fields:
        value = item.get(field)
        if value not in (None, ""):
            return field + ":" + str(value)
    digest = hashlib.md5(json.dumps(item, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return "row:" + digest[:12]


def diff_section(old: List[Dict[str, Any]], new: List[Dict[str, Any]],
                 fields: Tuple[str, ...]) -> Dict[str, Any]:
    old_map = {_row_key(item, fields): item for item in old}
    new_map = {_row_key(item, fields): item for item in new}
    added = [new_map[key] for key in new_map if key not in old_map]
    removed = [old_map[key] for key in old_map if key not in new_map]
    changed = []
    for key in old_map:
        if key not in new_map:
            continue
        before, after = old_map[key], new_map[key]
        changes = []
        for field in sorted(set(before) | set(after)):
            if before.get(field) != after.get(field):
                changes.append({"field": field, "old": before.get(field), "new": after.get(field)})
        if changes:
            changed.append({"key": key, "changes": changes})
    return {"added": added, "removed": removed, "changed": changed}


def build_diff(old: Dict[str, List[Dict[str, Any]]],
               new: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    sections = {}
    totals = {"added": 0, "removed": 0, "changed": 0}
    for name in DIFF_SECTIONS:
        result = diff_section(old.get(name, []), new.get(name, []), DIFF_KEYS[name])
        result["label"] = SECTION_LABELS[name]
        sections[name] = result
        totals["added"] += len(result["added"])
        totals["removed"] += len(result["removed"])
        totals["changed"] += len(result["changed"])
    summary = f"新增 {totals['added']} · 删除 {totals['removed']} · 字段变化 {totals['changed']}"
    return {"sections": sections, "totals": totals,
            "has_drift": any(totals.values()), "summary": summary}


def compare_snapshots(backup_dir: Path, a: Optional[str] = None,
                      b: Optional[str] = None) -> Dict[str, Any]:
    files = [p.name for p in snapshot_files(backup_dir)]
    if a and b:
        if a not in files or b not in files:
            return {"ok": False, "message": "指定的快照文件不存在，请检查文件名"}
    else:
        if len(files) < 2:
            return {"ok": False, "message": "快照不足两份，先多备份一次再对比"}
        a, b = files[1], files[0]
    diff = build_diff(load_sections(backup_dir / a), load_sections(backup_dir / b))
    diff["a"], diff["b"] = a, b
    return {"ok": True, "a": a, "b": b, "diff": diff,
            "message": f"对比 {a} → {b}：{diff['summary']}"}


def restore_guide(backup_dir: Path) -> Tuple[str, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now()
    name = f"{RESTORE_PREFIX}{now.strftime('%Y%m%d_%H%M%S_%f')}.md"
    snaps = snapshot_files(backup_dir)
    snapshot = snaps[0].name if snaps else "（还没有快照）"
    text = _GUIDE.format(snapshot=snapshot, time=now.strftime("%Y-%m-%d %H:%M:%S"))
    (backup_dir / name).write_text(text, encoding="utf-8")
    return name, text


def clean_snapshots(backup_dir: Path, keep: int, confirm: str) -> Dict[str, Any]:
    if confirm != CLEAN_CONFIRM:
        return {"ok": False, "removed": [], "message": "确认口令不对，没有清理任何文件"}
    removed = rotate(backup_dir, keep)
    return {"ok": True, "removed": removed,
            "message": f"清理了 {len(removed)} 份旧快照，保留最近 {max(1, int(keep))} 份"}
