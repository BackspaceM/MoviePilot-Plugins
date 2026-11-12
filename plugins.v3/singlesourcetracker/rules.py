"""规则表的读法和标题的判定，纯函数，不碰宿主。"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List, Tuple

SEP = "=>"
ITEM_SEP = ";;"
RE_PREFIX = "re:"

EXAMPLE = (
    "海贼王 => A站 => HDR;;DDP5.1;;XX制作组 => re:(?i)x264;;CAM\n"
    "火影忍者 => B站 => 杜比视界;;HDR;;H.265 => re:(?i)(HDTV|TS)"
)


def normalize(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[._\-\[\]()]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=256)
def _compile(pattern: str):
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def parse_items(raw: Any) -> Tuple[List[Dict[str, str]], List[str]]:
    items: List[Dict[str, str]] = []
    errors: List[str] = []
    for chunk in str(raw or "").split(ITEM_SEP):
        text = chunk.strip()
        if not text:
            continue
        if text.lower().startswith(RE_PREFIX):
            pattern = text[len(RE_PREFIX):].strip()
            if not pattern:
                errors.append("re: 后面没写正则")
                continue
            if _compile(pattern) is None:
                errors.append(f"正则写错了：{pattern[:40]}")
                continue
            items.append({"kind": "regex", "text": pattern, "display": f"re:{pattern}"})
        else:
            items.append({"kind": "text", "text": text, "display": text})
    return items, errors


def parse_target(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {"kind": "none", "name": "", "season": None}
    if text.startswith("#") and text[1:].strip().isdigit():
        return {"kind": "id", "value": int(text[1:].strip()), "name": text, "season": None}
    name, season = text, None
    if "#" in text:
        head, tail = text.rsplit("#", 1)
        tail = tail.strip().lower().lstrip("s")
        if tail.isdigit():
            name, season = head.strip(), int(tail)
    return {"kind": "name", "name": name, "season": season}


def parse_rules(text: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    rules: List[Dict[str, Any]] = []
    errors: List[str] = []
    for lineno, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            head = line.lstrip("#").strip().split(" ")[0]
            if not head.isdigit():
                continue
        parts = [item.strip() for item in line.split(SEP)]
        if len(parts) < 2:
            errors.append(f"第 {lineno} 行少了分段，至少要「匹配目标 => 站点」")
            continue
        while len(parts) < 4:
            parts.append("")
        target, site, include_raw, exclude_raw = parts[:4]
        include, inc_errors = parse_items(include_raw)
        exclude, exc_errors = parse_items(exclude_raw)
        parsed_target = parse_target(target)
        if parsed_target["kind"] == "none":
            errors.append(f"第 {lineno} 行没写匹配目标")
            continue
        rules.append({
            "line": lineno,
            "raw": line,
            "target": target,
            "target_kind": parsed_target["kind"],
            "target_id": parsed_target.get("value"),
            "target_name": parsed_target.get("name") or target,
            "season": parsed_target.get("season"),
            "site": site,
            "include": include,
            "exclude": exclude,
            "errors": [f"第 {lineno} 行 {item}" for item in inc_errors + exc_errors],
        })
        errors.extend(f"第 {lineno} 行 {item}" for item in inc_errors + exc_errors)
    return rules, errors


def rule_matches(rule: Dict[str, Any], sub: Dict[str, Any]) -> bool:
    if rule.get("target_kind") == "id":
        return str(sub.get("id")) == str(rule.get("target_id"))
    name = normalize(rule.get("target_name"))
    title = normalize(sub.get("name"))
    if not name or not title:
        return False
    if name not in title and title not in name:
        return False
    season = rule.get("season")
    if season is None:
        return True
    try:
        return int(sub.get("season")) == int(season)
    except (TypeError, ValueError):
        return False


def site_candidates(rule_site: Any, sites: List[Dict[str, Any]]) -> List[str]:
    text = str(rule_site or "").strip().lower()
    if not text:
        return []
    found = {text}
    for site in sites:
        values = [str(site.get("id") or ""), str(site.get("name") or "").lower(),
                  str(site.get("domain") or "").lower()]
        if text in values:
            found.update(item for item in values if item)
    return sorted(found)


def check_site(rule: Dict[str, Any], raw_site: Any, sites: List[Dict[str, Any]]) -> Any:
    raw = str(raw_site or "").strip().lower()
    candidates = site_candidates(rule.get("site"), sites)
    if not raw:
        return None
    if not candidates:
        return None
    if raw in candidates:
        return True
    for item in candidates:
        if len(item) >= 3 and (item in raw or raw in item):
            return True
    return False


def check_title(rule: Dict[str, Any], title: Any) -> List[str]:
    text = str(title or "")
    flat = normalize(text)
    problems: List[str] = []
    for item in rule.get("include") or []:
        if item.get("kind") == "regex":
            pattern = _compile(str(item.get("text") or ""))
            if pattern is None or not pattern.search(text):
                problems.append(f"没匹配上纳入 {item.get('display')}")
        elif normalize(item.get("text")) not in flat:
            problems.append(f"缺少必含「{item.get('display')}」")
    for item in rule.get("exclude") or []:
        if item.get("kind") == "regex":
            pattern = _compile(str(item.get("text") or ""))
            if pattern is not None and pattern.search(text):
                problems.append(f"命中排除 {item.get('display')}")
        elif normalize(item.get("text")) in flat:
            problems.append(f"命中排除「{item.get('display')}」")
    return problems


def brief(rule: Dict[str, Any]) -> str:
    include = "、".join(str(item.get("display")) for item in rule.get("include") or [])
    exclude = "、".join(str(item.get("display")) for item in rule.get("exclude") or [])
    parts = [f"站点 {rule.get('site') or '不限'}"]
    if include:
        parts.append(f"必含 {include}")
    if exclude:
        parts.append(f"排除 {exclude}")
    return "；".join(parts)


def template_for(subs: List[Dict[str, Any]], rules: List[Dict[str, Any]]) -> str:
    lines = []
    for sub in subs:
        if any(rule_matches(rule, sub) for rule in rules):
            continue
        name = str(sub.get("name") or "").strip()
        if not name:
            continue
        season = sub.get("season")
        target = f"{name}#S{season}" if season not in (None, "") else name
        lines.append(f"{target} => 站点名 => 必含项;;re:正则 => 排除项")
    return "\n".join(lines)
