"""把选的站点、画质标签和关键词，翻译成 MP 订阅能用的正则。"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List, Tuple

SEP = "=>"
ITEM_SEP = ";;"
RE_PREFIX = "re:"

TAGS = {
    "杜比视界": r"杜比视界|Dolby[._ -]?Vision|\bDV\b|DoVi",
    "HDR": r"\bHDR\b|HDR10",
    "HDR10+": r"HDR10\+|HDR10Plus",
    "H.265": r"H[._ -]?265|x265|HEVC",
    "H.264": r"H[._ -]?264|x264|AVC",
    "4K / 2160p": r"2160[pi]|\b4K\b|UHD",
    "1080p": r"1080[pi]|FHD",
    "60fps": r"60\s*fps|60[pi]\b|高帧率",
    "高码": r"高码|高码率|Hi[- ]?Bit|High[- ]?Bitrate",
    "10bit": r"10[._ -]?bit",
    "DDP5.1": r"DDP[._ -]?5[._ -]?1|DD\+|E-?AC3|EAC3",
    "Atmos": r"Atmos|杜比全景声",
    "TrueHD": r"TrueHD",
    "DTS-HD": r"DTS[- ]?HD|DTS[- ]?MA",
    "Remux": r"\bRemux\b",
    "WEB-DL": r"WEB[- ]?DL",
    "BluRay": r"Blu[- ]?Ray|BDMV|原盘",
}

TAG_ITEMS = [{"title": key, "value": key} for key in TAGS]

EXAMPLE = (
    "海贼王 => A站 => HDR;;DDP5.1;;XX制作组 => x264;;CAM\n"
    "火影忍者 => B站 => 杜比视界;;H.265 => re:(HDTV|TS)"
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


def _strip_inline_flags(pattern: str) -> str:
    # MP 订阅自己用 re.I 编译整条正则，中间再出现 (?i) 会被 Python 判为非法
    return re.sub(r"^\(\?[aiLmsux]+\)", "", str(pattern or "").strip())


def split_words(raw: Any) -> List[str]:
    parts = re.split(r"[,，;；\n]+|\s{2,}", str(raw or ""))
    return [item.strip() for item in parts if item.strip()]


def word_pattern(word: Any) -> str:
    out = []
    for char in str(word or "").strip():
        if char in "._ -":
            out.append(r"[._\s-]*")
        else:
            out.append(re.escape(char))
    return "".join(out)


def build_include_regex(tags: Any, words: Any, advanced: Any = "") -> Tuple[str, List[str]]:
    custom = _strip_inline_flags(advanced)
    if custom:
        if _compile(custom) is None:
            return "", ["高级正则写错了，请检查括号和转义"]
        return custom, [custom]

    groups, display = [], []
    for tag in (tags if isinstance(tags, list) else [tags]):
        pattern = TAGS.get(str(tag))
        if pattern:
            groups.append(f"(?:{pattern})")
            display.append(str(tag))
    for word in split_words(words):
        groups.append(f"(?:{word_pattern(word)})")
        display.append(word)
    if not groups:
        return "", []
    return "".join(f"(?=.*{group})" for group in groups), display


def build_exclude_regex(items: Any) -> Tuple[str, List[str], List[str]]:
    groups, display, errors = [], [], []
    for raw in str(items or "").split(ITEM_SEP):
        text = raw.strip()
        if not text:
            continue
        if text.lower().startswith(RE_PREFIX):
            pattern = _strip_inline_flags(text[len(RE_PREFIX):])
            if _compile(pattern) is None:
                errors.append(f"排除正则写错了：{pattern[:40]}")
                continue
            groups.append(f"(?:{pattern})")
        else:
            groups.append(f"(?:{word_pattern(text)})")
        display.append(text)
    if not groups:
        return "", [], errors
    return "(?:" + "|".join(groups) + ")", display, errors


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


def rule_matches(rule: Dict[str, Any], sub: Dict[str, Any]) -> bool:
    if rule.get("sid") not in (None, ""):
        return str(sub.get("id")) == str(rule.get("sid"))
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


def site_candidates(site_value: Any, sites: List[Dict[str, Any]]) -> List[str]:
    text = str(site_value or "").strip().lower()
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
    candidates = site_candidates(rule.get("site_value") or rule.get("site"), sites)
    if not raw or not candidates:
        return None
    if raw in candidates:
        return True
    for item in candidates:
        if len(item) >= 3 and (item in raw or raw in item):
            return True
    return False


def check_title(rule: Dict[str, Any], title: Any) -> List[str]:
    text = str(title or "")
    problems = []
    include = str(rule.get("include_regex") or "")
    exclude = str(rule.get("exclude_regex") or "")
    if include:
        pattern = _compile(include)
        if pattern is None or not pattern.search(text):
            problems.append("标题不满足必含条件")
    if exclude:
        pattern = _compile(exclude)
        if pattern is not None and pattern.search(text):
            problems.append("标题命中排除条件")
    return problems


def parse_line_rules(text: Any, sites: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    rules, errors = [], []
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
            errors.append(f"第 {lineno} 行少了分段，至少要「剧名 => 站点」")
            continue
        while len(parts) < 4:
            parts.append("")
        target, site_text, include_raw, exclude_raw = parts[:4]
        parsed_target = parse_target(target)
        if parsed_target["kind"] == "none":
            errors.append(f"第 {lineno} 行没写匹配目标")
            continue
        include_items = [item.strip() for item in str(include_raw).split(ITEM_SEP) if item.strip()]
        tags = [item for item in include_items if item in TAGS]
        words = [item for item in include_items
                 if item not in TAGS and not item.lower().startswith(RE_PREFIX)]
        regex_items = [_strip_inline_flags(item[len(RE_PREFIX):]) for item in include_items
                       if item.lower().startswith(RE_PREFIX)]
        advanced = ""
        if regex_items:
            advanced = "".join(f"(?=.*(?:{item}))" for item in regex_items)
        include_regex, display = build_include_regex(tags, ";;".join(words), advanced)
        exclude_regex, exclude_display, exc_errors = build_exclude_regex(exclude_raw)
        errors.extend(f"第 {lineno} 行 {item}" for item in exc_errors)
        site = None
        for item in sites:
            if site_text and site_text.lower() in (
                str(item.get("id")), str(item.get("name") or "").lower(),
                str(item.get("domain") or "").lower()
            ):
                site = item
                break
        rules.append({
            "source": f"第 {lineno} 行",
            "target": target,
            "target_kind": parsed_target["kind"],
            "target_id": parsed_target.get("value"),
            "target_name": parsed_target.get("name") or target,
            "season": parsed_target.get("season"),
            "sid": None,
            "site_value": site_text,
            "site_id": site.get("id") if site else None,
            "site_name": site.get("name") if site else site_text,
            "include_regex": include_regex,
            "exclude_regex": exclude_regex,
            "include_display": "、".join(display),
            "exclude_display": "、".join(exclude_display),
        })
    return rules, errors


def build_quick_rule(cfg: Dict[str, Any], catalog: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    sid = cfg.get("quick_sub")
    if sid in (None, "", 0, "0"):
        return [], []
    if isinstance(sid, str):
        text = sid.strip()
        if not text:
            return [], []
        # 手填 #11 或 11 都当订阅 ID；其他文字当剧名
        if text.lstrip("#").isdigit() and text.lstrip("#"):
            sid = int(text.lstrip("#"))
        else:
            return _rule_for_name(text, cfg, catalog)
    subs = catalog.get("subscriptions") if isinstance(catalog, dict) else []
    sub = next((item for item in (subs or [])
                if isinstance(item, dict) and str(item.get("id")) == str(sid)), None)
    # 目录里没有也照样生成规则，写订阅时会现查一次订阅再匹配
    if sub is None and not (subs or []):
        sub = {"id": sid, "name": f"#{sid}", "season": None}
    elif sub is None:
        return [], ["选的订阅不在扫描结果里，回详情页点一次「扫描 MP 订阅」"]
    sites = catalog.get("sites") if isinstance(catalog, dict) else []
    site_value = cfg.get("quick_site")
    site = next((item for item in (sites or [])
                 if isinstance(item, dict) and str(item.get("id")) == str(site_value)), None)
    include_regex, display = build_include_regex(
        cfg.get("quick_tags"), cfg.get("quick_include"), cfg.get("quick_regex"))
    exclude_regex, exclude_display, errors = build_exclude_regex(cfg.get("quick_exclude"))
    rule = {
        "source": "快捷配置",
        "target": f"#{sid}",
        "target_kind": "id",
        "target_id": sid,
        "target_name": str(sub.get("name") or ""),
        "season": sub.get("season"),
        "sid": sid,
        "site_value": site_value,
        "site_id": site.get("id") if site else None,
        "site_name": site.get("name") if site else (site_value or ""),
        "include_regex": include_regex,
        "exclude_regex": exclude_regex,
        "include_display": "、".join(display),
        "exclude_display": "、".join(exclude_display),
    }
    return [rule], errors


def _rule_for_name(name: str, cfg: Dict[str, Any],
                   catalog: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    # 手填剧名：按剧名模糊匹配订阅，找不到也照常生成规则，扫描后就能匹配上
    parsed = parse_target(name)
    season = parsed.get("season")
    sites = catalog.get("sites") if isinstance(catalog, dict) else []
    site_value = cfg.get("quick_site")
    site = next((item for item in (sites or [])
                 if isinstance(item, dict) and str(item.get("id")) == str(site_value)), None)
    include_regex, display = build_include_regex(
        cfg.get("quick_tags"), cfg.get("quick_include"), cfg.get("quick_regex"))
    exclude_regex, exclude_display, errors = build_exclude_regex(cfg.get("quick_exclude"))
    rule = {
        "source": "快捷配置",
        "target": name,
        "target_kind": "name",
        "target_id": None,
        "target_name": parsed.get("name") or name,
        "season": season,
        "sid": None,
        "site_value": site_value,
        "site_id": site.get("id") if site else None,
        "site_name": site.get("name") if site else (site_value or ""),
        "include_regex": include_regex,
        "exclude_regex": exclude_regex,
        "include_display": "、".join(display),
        "exclude_display": "、".join(exclude_display),
    }
    return [rule], errors


def merge_rules(line_rules: List[Dict[str, Any]], quick_rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for rule in line_rules + quick_rules:
        key = str(rule.get("sid") or rule.get("target"))
        merged[key] = rule
    return list(merged.values())


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
        lines.append(f"{target} => 站点名 => HDR;;制作组 => x264")
    return "\n".join(lines)
