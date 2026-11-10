"""插件自动更新 —— 纯逻辑层。

不依赖 MoviePilot 宿主：全部函数都吃 dict / list / 简单对象，可以在离线环境里
直接单测。负责把宿主扫描回来的插件记录归一化、按作者筛选、挑出真正有新版的目标、
生成运行计划与报告。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ALL_AUTHORS = "__all__"
UNKNOWN_AUTHOR = "未标注作者"

_KNOWN_KEYS = (
    "id", "plugin_id", "plugin_name", "name", "plugin_author", "author",
    "plugin_version", "version", "installed", "state", "has_update",
    "repo_url", "update_candidate", "plugin_desc", "description", "source",
)

_CRON_RE = re.compile(
    r"^(\*|(\*/\d+)|(\d+(-\d+)?)(/\d+)?(,\d+(-\d+)?(/\d+)?)*)$"
)


@dataclass
class InstalledPlugin:
    """一条已安装插件的扫描结果。"""

    id: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    latest_version: str = ""
    has_update: bool = False
    repo_url: str = ""
    enabled: bool = True
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "InstalledPlugin":
        d = dict(data or {})
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            author=str(d.get("author") or ""),
            version=str(d.get("version") or ""),
            latest_version=str(d.get("latest_version") or ""),
            has_update=bool(d.get("has_update")),
            repo_url=str(d.get("repo_url") or ""),
            enabled=bool(d.get("enabled", True)),
            source=str(d.get("source") or ""),
        )


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _pick(raw: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return default


def _to_mapping(record: Any) -> Dict[str, Any]:
    """把 Pydantic 对象 / 普通对象 / dict 统一摊成 dict。"""
    if record is None:
        return {}
    if isinstance(record, dict):
        return dict(record)
    for attr in ("model_dump", "dict"):
        fn = getattr(record, attr, None)
        if callable(fn):
            try:
                out = fn()
            except Exception:  # noqa: BLE001
                out = None
            if isinstance(out, dict):
                return dict(out)
    try:
        raw = dict(record.__dict__)
        if raw:
            return raw
    except Exception:  # noqa: BLE001
        pass
    out: Dict[str, Any] = {}
    for key in _KNOWN_KEYS:
        if hasattr(record, key):
            out[key] = getattr(record, key)
    return out


def unknown_author(author: Any) -> str:
    """空作者统一成一个能显示、能比较的占位名字。"""
    return _text(author) or UNKNOWN_AUTHOR


def record_to_dict(record: Any) -> Dict[str, Any]:
    """把一条宿主记录归一化：dict / Pydantic / 普通对象都吃。

    plugin_name -> name、plugin_author -> author、plugin_version -> version；
    update_candidate 里的 version / repo_url 也拆出来。
    """
    raw = _to_mapping(record)
    candidate = _to_mapping(raw.get("update_candidate")) if raw.get("update_candidate") else {}
    return {
        "id": _text(_pick(raw, "id", "plugin_id")),
        "name": _text(_pick(raw, "plugin_name", "name")),
        "author": _text(_pick(raw, "plugin_author", "author")),
        "version": _text(_pick(raw, "plugin_version", "version")),
        "latest_version": _text(_pick(candidate, "version")),
        "has_update": bool(raw.get("has_update", False)),
        "repo_url": (_text(_pick(candidate, "repo_url"))
                     or _text(_pick(raw, "repo_url"))),
        "installed": raw.get("installed"),
        "state": raw.get("state"),
        "update_candidate": raw.get("update_candidate"),
        "plugin_desc": _text(_pick(raw, "plugin_desc", "description")),
        "source": _text(raw.get("source")),
    }


def _version_parts(text: Any) -> List[int]:
    s = _text(text).lower()
    if s.startswith("v"):
        s = s[1:]
    # 只取开头的数字点分前缀；-beta / +build7 这类后缀整体忽略，
    # 否则 build7 里的 7 会被误当成新的一段
    match = re.match(r"^[0-9.]*", s)
    core = match.group(0) if match else ""
    return [int(chunk) for chunk in core.split(".") if chunk.isdigit()]


def compare_versions(a: Any, b: Any) -> int:
    """语义化版本比较：a<b 返回 -1，相等 0，a>b 返回 1。"""
    pa, pb = _version_parts(a), _version_parts(b)
    size = max(len(pa), len(pb))
    pa += [0] * (size - len(pa))
    pb += [0] * (size - len(pb))
    for x, y in zip(pa, pb):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


def _parse_enabled(state: Any, installed: Any) -> bool:
    for value in (state, installed):
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return bool(value)
        text = _text(value).lower()
        if text in ("true", "1", "yes", "on", "enabled", "running",
                    "installed", "active", "启用"):
            return True
        if text in ("false", "0", "no", "off", "disabled", "stopped",
                    "inactive", "uninstalled", "未安装"):
            return False
    return True


def normalize_plugins(installed_records: Any,
                      online_records: Any) -> List[InstalledPlugin]:
    """按 id 合并本机清单与市场索引，得出可更新版本与来源仓库。

    只保留本机装过的插件；在线清单用来补 latest_version / repo_url，
    同名同 id 去重，最后按名称排序。
    """
    online_map: Dict[str, Dict[str, Any]] = {}
    for record in (online_records or []):
        item = record_to_dict(record)
        if item["id"]:
            online_map[item["id"]] = item

    merged: Dict[str, InstalledPlugin] = {}
    order: List[str] = []
    for record in (installed_records or []):
        item = record_to_dict(record)
        pid = item["id"]
        if not pid or pid in merged:
            continue
        online = online_map.get(pid)

        candidates: List[str] = []
        if online:
            if online["version"]:
                candidates.append(online["version"])
            if online["latest_version"]:
                candidates.append(online["latest_version"])
        if item["latest_version"]:
            candidates.append(item["latest_version"])

        current = item["version"]
        latest = ""
        for cand in candidates:
            if compare_versions(cand, current) > 0:
                if not latest or compare_versions(cand, latest) > 0:
                    latest = cand
        if not latest:
            latest = current

        repo_url = ""
        if online:
            repo_url = online["repo_url"]
        if not repo_url:
            repo_url = item["repo_url"]

        has_update = (bool(item["has_update"])
                      or compare_versions(latest, current) > 0
                      or bool(online and online["has_update"]))

        merged[pid] = InstalledPlugin(
            id=pid,
            name=item["name"] or pid,
            author=unknown_author(item["author"]),
            version=current,
            latest_version=latest,
            has_update=has_update,
            repo_url=repo_url,
            enabled=_parse_enabled(item.get("state"), item.get("installed")),
            source="市场索引" if online else "本机清单",
        )
        order.append(pid)

    result = [merged[pid] for pid in order]
    result.sort(key=lambda p: (p.name or p.id).lower())
    return result


def author_items(plugins: Iterable[InstalledPlugin]) -> List[Dict[str, Any]]:
    """作者单选下拉：第一项固定「全部作者」，后面按作者去重排序并带数量。"""
    plugins = list(plugins or [])
    counts: Dict[str, int] = {}
    for p in plugins:
        author = unknown_author(p.author)
        counts[author] = counts.get(author, 0) + 1
    items: List[Dict[str, Any]] = [
        {"title": "全部作者", "value": ALL_AUTHORS, "count": len(plugins)}
    ]
    for author in sorted(counts):
        items.append({"title": f"{author} · {counts[author]} 个",
                      "value": author, "count": counts[author]})
    return items


def plugin_items(plugins: Iterable[InstalledPlugin],
                 author: str = ALL_AUTHORS) -> List[Dict[str, Any]]:
    """插件多选下拉；传了作者就只列这个作者的插件。"""
    result: List[Dict[str, Any]] = []
    for p in plugins or []:
        p_author = unknown_author(p.author)
        if author and author != ALL_AUTHORS and p_author != author:
            continue
        result.append({"title": f"{p.name} · {p_author} · {p.version}",
                       "value": p.id})
    result.sort(key=lambda x: x["title"])
    return result


def filter_targets(plugins: Iterable[InstalledPlugin], author: str,
                   selected: Any, skip_self_id: str = "",
                   include_self: bool = False) -> Tuple[List[InstalledPlugin], List[str]]:
    """运行时真实筛选：作者单选 + 目标多选 + 默认排除自身。"""
    notes: List[str] = []
    pool = list(plugins or [])
    label = "全部作者" if not author or author == ALL_AUTHORS else str(author)
    if author and author != ALL_AUTHORS:
        pool = [p for p in pool if unknown_author(p.author) == author]
    notes.append(f"作者 {label}｜匹配 {len(pool)} 个")

    if isinstance(selected, str):
        selected = [x.strip() for x in selected.split(",") if x.strip()]
    sel = {str(x).strip() for x in (selected or []) if str(x).strip()}
    if sel:
        pool = [p for p in pool if p.id in sel]
        notes.append(f"勾选 {len(sel)} 个｜命中 {len(pool)} 个")
    else:
        notes.append("未勾选目标｜取该作者下全部插件")

    if not include_self and skip_self_id:
        before = len(pool)
        pool = [p for p in pool if p.id != skip_self_id]
        if before != len(pool):
            notes.append("已跳过自身")

    seen = set()
    targets: List[InstalledPlugin] = []
    for p in pool:
        if p.id in seen:
            continue
        seen.add(p.id)
        targets.append(p)
    return targets, notes


def validate_cron(text: Any) -> Tuple[bool, str]:
    """校验 5 段 crontab 表达式，第二个返回值是规范化后的表达式。"""
    expression = _text(text).replace("?", "*")
    fields = expression.split()
    if len(fields) != 5:
        return False, ""
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for chunk, (low, high) in zip(fields, ranges):
        if not _CRON_RE.match(chunk):
            return False, ""
        for part in chunk.split(","):
            if part == "*":
                continue
            if part.startswith("*/"):
                if int(part[2:]) < 1:
                    return False, ""
                continue
            if "-" in part:
                start, end = (int(x) for x in part.split("-", 1))
            else:
                start = end = int(part)
            if start < low or end > high or start > end:
                return False, ""
    return True, expression


def build_run_plan(targets: Iterable[InstalledPlugin], dry_run: bool = False,
                   max_per_run: int = 10,
                   force_reinstall: bool = False) -> Dict[str, Any]:
    """挑出真正要更新的插件，并限制单次数量。"""
    try:
        limit = max(0, int(max_per_run))
    except (TypeError, ValueError):
        limit = 10
    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []

    for p in targets or []:
        newer = bool(p.latest_version) and compare_versions(p.latest_version, p.version) > 0
        need = bool(p.has_update) or newer or bool(force_reinstall)
        if not need:
            skipped.append({
                "id": p.id, "name": p.name, "author": p.author,
                "from": p.version, "to": p.latest_version,
                "repo_url": p.repo_url, "reason": "已是最新版本",
            })
            continue
        reason = "市场有新版本" if (p.has_update or newer) else "强制重装"
        entry = {
            "id": p.id, "name": p.name, "author": p.author,
            "from": p.version, "to": p.latest_version or p.version,
            "repo_url": p.repo_url, "reason": reason,
        }
        if len(items) >= limit:
            blocked.append({**entry, "reason": "超出单次上限，留到下次"})
        else:
            items.append(entry)

    return {"items": items, "skipped": skipped, "blocked": blocked,
            "dry_run": bool(dry_run)}


def summarize_run(items: Sequence[Dict[str, Any]], results: Any) -> Dict[str, Any]:
    """把逐条安装结果汇总成一份报告（总数 / 成功 / 失败 / 跳过 / 耗时 / 明细）。"""
    if isinstance(results, dict):
        outcomes = list(results.get("outcomes") or [])
        meta = dict(results)
    else:
        outcomes = list(results or [])
        meta = {}
    meta.pop("outcomes", None)

    by_id: Dict[str, Dict[str, Any]] = {}
    for outcome in outcomes:
        if isinstance(outcome, dict) and outcome.get("id"):
            by_id[str(outcome["id"])] = outcome

    details: List[Dict[str, Any]] = []
    success = 0
    failed = 0
    for raw in (items or []):
        item = dict(raw or {})
        pid = str(item.get("id") or "")
        outcome = by_id.get(pid, {})
        ok = bool(outcome.get("ok"))
        details.append({
            "id": pid,
            "name": item.get("name") or pid,
            "author": item.get("author") or "",
            "from": item.get("from") or "",
            "to": item.get("to") or "",
            "repo_url": item.get("repo_url") or "",
            "reason": item.get("reason") or "",
            "ok": ok,
            "message": str(outcome.get("message") or "没有结果"),
            "mechanism": str(outcome.get("mechanism") or ""),
        })
        if ok:
            success += 1
        else:
            failed += 1

    total = len(items or [])
    skipped = int(meta.get("skipped") or 0)
    blocked = int(meta.get("blocked") or 0)
    dry = bool(meta.get("dry_run"))
    if dry:
        message = f"试运行：计划更新 {total} 个，没有真的安装"
    else:
        message = f"更新 {total} 个｜成功 {success}｜失败 {failed}"
        if skipped:
            message += f"｜跳过 {skipped}"
        if blocked:
            message += f"｜留到下次 {blocked}"

    return {
        "ok": failed == 0,
        "message": message,
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "blocked": blocked,
        "duration": float(meta.get("duration") or 0),
        "started": str(meta.get("started") or ""),
        "finished": str(meta.get("finished") or ""),
        "dry_run": dry,
        "trigger": str(meta.get("trigger") or "手动"),
        "source": str(meta.get("source") or ""),
        "scanned": int(meta.get("scanned") or 0),
        "notes": list(meta.get("notes") or []),
        "details": details,
    }


def append_history(history: Any, report: Dict[str, Any],
                   limit: int = 30) -> List[Dict[str, Any]]:
    """有界更新记录，最新的排在最前面。"""
    entries = [dict(x) for x in (history or []) if isinstance(x, dict)]
    entries.insert(0, dict(report or {}))
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 30
    return entries[:cap]
