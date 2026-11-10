"""带宽/Queue 智能调度 —— 零宿主依赖的纯逻辑层。

所有可被单测覆盖的解析、方案构建、时段匹配、队列差异与格式化都放这里，
避免与 MoviePilot 宿主耦合，方便脱离 MP 运行环境做单元测试。

核心概念：
- 角色（role）：download(下载设备) / nas(NAS/服务器) / media(观影设备) / other(其它)。
- 设备（target）：用户定义 名称=IP/CIDR|角色，插件据此在 RouterOS 建一条简单队列。
- 带宽方案（profile）：为每个角色规定 max-limit(上行/下行)、limit-at(保证带宽)、priority。
  内置 balanced/movie/download/night 四套，并支持自定义 JSON 覆盖/新增。
- 时段规则（rule）：HH:MM-HH:MM=方案，按当前时间自动挑选应生效的方案（支持跨午夜）。
- 队列差异（diff）：把"期望队列"与 RouterOS 现有（仅本插件前缀）对比，得出新建/更新/删除。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 角色与内置带宽方案
# ---------------------------------------------------------------------------

ROLES: Tuple[str, ...] = ("download", "nas", "media", "other")
ROLE_LABELS: Dict[str, str] = {
    "download": "下载设备",
    "nas": "NAS/服务器",
    "media": "观影设备",
    "other": "其它设备",
}
DEFAULT_PROFILE = "balanced"

# max-limit / limit-at 采用 RouterOS 原生 "上行/下行" 格式；"0/0" 表示不限速。
# priority：1 最高，8 最低（RouterOS 语义）。
PROFILES: Dict[str, Dict[str, Any]] = {
    "balanced": {
        "label": "均衡",
        "desc": "下载适度限速，观影与 NAS 保留充足带宽",
        "rules": {
            "download": {"max": "20M/60M", "limit": "4M/12M", "priority": 8},
            "nas": {"max": "30M/100M", "limit": "8M/24M", "priority": 4},
            "media": {"max": "0/0", "limit": "0/0", "priority": 1},
            "other": {"max": "10M/30M", "limit": "2M/6M", "priority": 6},
        },
    },
    "movie": {
        "label": "观影优先",
        "desc": "强力压制下载，观影设备不限速",
        "rules": {
            "download": {"max": "2M/5M", "limit": "512k/1M", "priority": 8},
            "nas": {"max": "10M/30M", "limit": "4M/12M", "priority": 4},
            "media": {"max": "0/0", "limit": "0/0", "priority": 1},
            "other": {"max": "5M/15M", "limit": "1M/3M", "priority": 6},
        },
    },
    "download": {
        "label": "下载优先",
        "desc": "下载不限速，观影与其它设备适度限流",
        "rules": {
            "download": {"max": "0/0", "limit": "0/0", "priority": 2},
            "nas": {"max": "0/0", "limit": "0/0", "priority": 4},
            "media": {"max": "10M/30M", "limit": "4M/12M", "priority": 3},
            "other": {"max": "20M/60M", "limit": "4M/12M", "priority": 6},
        },
    },
    "night": {
        "label": "夜间全速",
        "desc": "全部不限速，适合夜间无人观影时跑满带宽",
        "rules": {
            "download": {"max": "0/0", "limit": "0/0", "priority": 5},
            "nas": {"max": "0/0", "limit": "0/0", "priority": 5},
            "media": {"max": "0/0", "limit": "0/0", "priority": 5},
            "other": {"max": "0/0", "limit": "0/0", "priority": 5},
        },
    },
}


def merge_profiles(custom: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """合并内置方案与自定义方案：同 key 覆盖角色规则，新 key 直接加入。"""
    out: Dict[str, Dict[str, Any]] = {
        k: {"label": v["label"], "desc": v["desc"], "rules": dict(v["rules"])}
        for k, v in PROFILES.items()
    }
    for key, val in (custom or {}).items():
        k = str(key).strip().lower()
        if not k or not isinstance(val, dict):
            continue
        rules = val.get("rules") or {}
        if k in out:
            out[k]["rules"].update(rules)
            if val.get("label"):
                out[k]["label"] = str(val["label"])
            if val.get("desc"):
                out[k]["desc"] = str(val["desc"])
        else:
            out[k] = {
                "label": str(val.get("label") or k),
                "desc": str(val.get("desc") or ""),
                "rules": dict(rules),
            }
    return out


def available_profiles(profiles: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """给详情页"方案切换"按钮用的方案清单。"""
    profiles = profiles or merge_profiles()
    return [{"key": k, "label": v.get("label", k), "desc": v.get("desc", "")}
            for k, v in profiles.items()]


# ---------------------------------------------------------------------------
# 设备 / 时段 / 方案解析
# ---------------------------------------------------------------------------

def _looks_like_target(text: str) -> bool:
    t = str(text or "").strip()
    return ("." in t or ":" in t or "/" in t
            or t.startswith("ether") or t.startswith("bridge") or t.startswith("vlan"))


def parse_targets(text: str) -> List[Dict[str, str]]:
    """解析设备定义。每行支持：

    - ``名称=192.168.1.10/32|nas``（推荐）
    - ``名称,192.168.1.10/32,nas``（逗号兼容）
    - ``192.168.1.10/32``（名称缺省取 IP）
    - ``#`` 开头为注释；角色省略或非法归为 other。
    """
    out: List[Dict[str, str]] = []
    seen: set = set()
    for raw in str(text or "").replace("，", ",").replace("｜", "|").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, rest = "", ""
        if "=" in line:
            name, rest = line.split("=", 1)
        else:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and _looks_like_target(parts[1]):
                name, rest = parts[0], ",".join(parts[1:])
            elif len(parts) == 1 and _looks_like_target(parts[0]):
                name, rest = parts[0], parts[0]   # 仅填 IP，名称取 IP
            else:
                continue                          # 无法识别的行，跳过
        name = name.strip()
        rest = rest.strip()
        if "|" in rest:
            target, role = rest.split("|", 1)
            role = role.strip().lower()
        elif "," in rest:
            seg = [s.strip() for s in rest.split(",")]
            target = seg[0]
            role = seg[1].lower() if len(seg) > 1 else "other"
        else:
            target, role = rest, "other"
        target = target.strip()
        if role not in ROLES:
            role = "other"
        if not name:
            name = target
        if not target or target in seen:
            continue
        seen.add(target)
        out.append({"name": name, "target": target, "role": role})
    return out


def time_to_minutes(value: str) -> Optional[int]:
    """把 HH:MM 转成分钟；24:00 视为当天结束(1440)；非法返回 None。"""
    s = str(value or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute > 59 or hour > 24:
        return None
    if hour == 24:
        return 1440 if minute == 0 else None
    return hour * 60 + minute


def parse_rules(text: str) -> List[Dict[str, Any]]:
    """解析时段规则：每行 ``HH:MM-HH:MM=方案``；按开始时间排序。"""
    out: List[Dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        window, profile = line.split("=", 1)
        profile = profile.strip().lower()
        if "-" not in window or not profile:
            continue
        left, right = window.split("-", 1)
        start, end = time_to_minutes(left), time_to_minutes(right)
        if start is None or end is None:
            continue
        out.append({
            "start": start, "end": end, "profile": profile,
            "label": f"{left.strip()}-{right.strip()}",
        })
    out.sort(key=lambda r: r["start"])
    return out


def in_window(now_minute: int, start: int, end: int) -> bool:
    """判断当前分钟是否落在时段内，支持跨午夜（如 23:00-06:00）。"""
    if start == end:
        return True
    if start < end:
        return start <= now_minute < end
    return now_minute >= start or now_minute < end


def match_profile(rules: Sequence[Dict[str, Any]], default_profile: str,
                  now: Any = None) -> str:
    """按当前时间挑选方案。多条规则重叠时，后写的优先（便于覆盖特殊时段）。"""
    import datetime  # 局部导入，保持模块顶层零副作用

    current = now or datetime.datetime.now()
    minute = current.hour * 60 + current.minute
    matched = default_profile
    for rule in rules or []:
        if in_window(minute, int(rule.get("start", 0)), int(rule.get("end", 0))):
            matched = str(rule.get("profile") or default_profile)
    return matched


def parse_profile_json(text: str) -> Dict[str, Any]:
    """解析自定义方案 JSON（容错：格式错误返回空，不阻断主流程）。

    结构示例::

        {
          "movie": {
            "label": "观影优先(自定义)",
            "rules": {
              "download": {"max": "1M/3M", "limit": "512k/1M", "priority": 8},
              "media": {"max": "0/0", "priority": 1}
            }
          }
        }
    """
    if not text or not str(text).strip():
        return {}
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    clean: Dict[str, Any] = {}
    for key, val in data.items():
        k = str(key).strip().lower()
        if not k or not isinstance(val, dict):
            continue
        rules: Dict[str, Any] = {}
        raw_rules = val.get("rules")
        if not isinstance(raw_rules, dict):
            # 兼容角色直接写在顶层的写法
            raw_rules = {k: v for k, v in val.items() if k not in ("label", "desc")}
        for role, spec in raw_rules.items():
            r = str(role).strip().lower()
            if r not in ROLES or not isinstance(spec, dict):
                continue
            maxv = str(spec.get("max") or spec.get("max_limit") or "0/0").strip() or "0/0"
            lim = str(spec.get("limit") or spec.get("limit_at") or "0/0").strip() or "0/0"
            try:
                pr = int(spec.get("priority", 5))
            except Exception:  # noqa: BLE001
                pr = 5
            pr = max(1, min(8, pr))
            rules[r] = {"max": maxv, "limit": lim, "priority": pr}
        clean[k] = {
            "label": str(val.get("label") or k),
            "desc": str(val.get("desc") or ""),
            "rules": rules,
        }
    return clean


_BW_RE = re.compile(r"(\d+(?:\.\d+)?)([kKmMgG]?)/(\d+(?:\.\d+)?)([kKmMgG]?)")


def parse_bandwidth(text: str) -> Tuple[bool, str]:
    """校验并规范化 RouterOS 带宽串（上行/下行）。"0/0" 表示不限速。"""
    s = str(text or "").strip().replace(" ", "")
    if not s:
        return False, ""
    if s in ("0", "0/0"):
        return True, "0/0"
    if not _BW_RE.fullmatch(s):
        return False, ""
    return True, s


# ---------------------------------------------------------------------------
# 队列规格构建与差异
# ---------------------------------------------------------------------------

def safe_filename(text: str) -> str:
    # 保留中文等 Unicode 字母数字（RouterOS v7 名称支持 UTF-8），仅剔除空白与特殊符号，
    # 避免中文设备名被清洗成 "-"
    # 导致多条队列同名冲突。
    cleaned = "".join(c for c in str(text or "") if c.isalnum() or c in "_.-").strip("-.")
    return cleaned or "target"


def queue_name(prefix: str, name: str, index: int) -> str:
    # 序号确保唯一（不同设备可能同名），并保留可读的安全名
    return f"{prefix}_{index + 1}_{safe_filename(name)}"


def build_queue_specs(profile_key: str, targets: Sequence[Dict[str, str]],
                      prefix: str, profiles: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """把方案 + 设备列表展开为期望的简单队列规格。"""
    profiles = profiles or merge_profiles()
    prof = profiles.get(profile_key) or profiles.get(DEFAULT_PROFILE) or {}
    rules = prof.get("rules") or {}
    specs: List[Dict[str, Any]] = []
    for idx, target in enumerate(targets or []):
        role = str(target.get("role") or "other")
        r = rules.get(role) or rules.get("other") or {"max": "0/0", "limit": "0/0", "priority": 5}
        try:
            priority = int(r.get("priority", 5))
        except Exception:  # noqa: BLE001
            priority = 5
        priority = max(1, min(8, priority))
        specs.append({
            "name": queue_name(prefix, target.get("name", ""), idx),
            "target": target.get("target", ""),
            "max_limit": str(r.get("max") or "0/0"),
            "limit_at": str(r.get("limit") or "0/0"),
            "priority": priority,
            "comment": f"bqs|{profile_key}|{target.get('name', '')}|{ROLE_LABELS.get(role, role)}",
            "target_name": target.get("name", ""),
            "role": role,
        })
    return specs


def _norm_limit(value: Any) -> str:
    return str(value if value is not None else "0/0").strip()


def diff_queue_specs(current_rows: Sequence[Dict[str, Any]],
                     desired_specs: Sequence[Dict[str, Any]],
                     prefix: str) -> Dict[str, Any]:
    """对比期望队列与 RouterOS 现有队列，产出新建/更新/删除清单。

    仅管理名称以 ``prefix_`` 开头的队列，绝不触碰用户手工创建的其它队列。
    """
    current: Dict[str, Dict[str, Any]] = {}
    for row in current_rows or []:
        name = str(row.get("name") or "")
        if name.startswith(prefix + "_"):
            current[name] = row
    desired = {s.get("name"): s for s in desired_specs or []}

    create: List[Dict[str, Any]] = []
    update: List[Dict[str, Any]] = []
    remove: List[Dict[str, Any]] = []
    unchanged: List[str] = []

    for name, spec in desired.items():
        row = current.get(name)
        if row is None:
            create.append(spec)
            continue
        changes: Dict[str, Any] = {}
        if _norm_limit(row.get("max-limit")) != _norm_limit(spec.get("max_limit")):
            changes["max-limit"] = spec.get("max_limit")
        if _norm_limit(row.get("limit-at")) != _norm_limit(spec.get("limit_at")):
            changes["limit-at"] = spec.get("limit_at")
        if str(row.get("priority") or "") != str(spec.get("priority")):
            changes["priority"] = spec.get("priority")
        cur_target = str(row.get("target") or "")
        want_target = str(spec.get("target") or "")
        if cur_target and want_target and cur_target != want_target:
            # RouterOS 偶尔把 /32 规范化省略，做一次宽松比较
            if cur_target.split("/")[0] != want_target.split("/")[0]:
                changes["target"] = want_target
        if str(row.get("disabled") or "false").lower() in ("true", "yes", "1"):
            changes["disabled"] = False
        if changes:
            update.append({"id": row.get(".id"), "name": name, "changes": changes, "spec": spec})
        else:
            unchanged.append(name)

    for name, row in current.items():
        if name not in desired:
            remove.append({"id": row.get(".id"), "name": name})

    return {
        "create": create, "update": update, "remove": remove, "unchanged": unchanged,
        "has_change": bool(create or update or remove),
    }


# ---------------------------------------------------------------------------
# 详情页文本安全（沿用已验证的 md_safe，杜绝 Markdown 参考式链接空药丸）
# ---------------------------------------------------------------------------

def md_safe(text: Any) -> str:
    s = str(text if text is not None else "")
    table = str.maketrans({
        "[": "［", "]": "］",
        "`": "ˋ", "*": "＊",
        "<": "‹", ">": "›",
        "_": "＿",
    })
    return s.translate(table)


def format_log_line(time_text: str, level: str, message: str) -> str:
    return md_safe(f"{time_text} ｜ {level:<5} ｜ {message}")


def format_rates_text(rows: Sequence[Dict[str, Any]], prefix: str,
                      max_rows: int = 20) -> str:
    mine = [r for r in (rows or []) if str(r.get("name") or "").startswith(prefix + "_")]
    if not mine:
        return "暂无本插件队列（请先在配置页定义设备并启用插件）"
    lines: List[str] = []
    for row in mine[:max_rows]:
        name = row.get("name", "")
        rate = str(row.get("rate") or "—")
        mx = str(row.get("max-limit") or "0/0")
        total = str(row.get("bytes") or "0")
        lines.append(f"{name}｜上/下限 {mx}｜实时 {rate}｜累计 {total} 字节")
    if len(mine) > max_rows:
        lines.append(f"……（共 {len(mine)} 条，已截断）")
    return "\n".join(lines)


def format_fasttrack_text(info: Optional[Dict[str, Any]]) -> str:
    if info is None:
        return "尚未诊断，点击「FastTrack 诊断」检查限速是否会被绕过"
    if not info.get("has_fasttrack"):
        return "未发现启用的 FastTrack 规则，Queue 限速可正常生效"
    rules = info.get("rules") or []
    lines = ["⚠ 检测到启用的 FastTrack 规则，被其匹配的流量会绕过 Queue 导致限速失效："]
    lines += [f"  • {r}" for r in rules[:6]]
    lines.append("建议：在 FastTrack 规则之前加一条 accept 规则放行本插件目标 IP，或先禁用 FastTrack 验证限速效果。")
    return "\n".join(lines)


def format_apply_text(result: Dict[str, Any], max_rows: int = 12) -> str:
    lines = [f"方案：{result.get('profile', '')}"
             + ("（试运行，未实际修改）" if result.get("dry_run") else "")]
    lines.append(f"新建 {len(result.get('created') or [])}｜更新 {len(result.get('updated') or [])}"
                 f"｜删除 {len(result.get('removed') or [])}｜未变 {len(result.get('unchanged') or [])}")
    for spec in (result.get("created") or [])[:max_rows]:
        lines.append(f"  ＋ {spec.get('name')} 上/下限 {spec.get('max_limit')} 目标 {spec.get('target')}")
    for upd in (result.get("updated") or [])[:max_rows]:
        changes = "、".join(f"{k}={v}" for k, v in (upd.get("changes") or {}).items())
        lines.append(f"  ～ {upd.get('name')} 改 {changes}")
    for rem in (result.get("removed") or [])[:max_rows]:
        lines.append(f"  － {rem.get('name')}")
    if result.get("errors"):
        lines.append("异常：" + "；".join(str(e) for e in result["errors"]))
    if not result.get("ok"):
        lines.append("执行失败：" + str(result.get("message", "")))
    return "\n".join(lines)


def summary_text(result: Dict[str, Any]) -> str:
    if not result.get("ok"):
        return "执行失败"
    if result.get("dry_run"):
        return "试运行完成（未修改）"
    return (f"{result.get('profile', '')}：+{len(result.get('created') or [])} "
            f"~{len(result.get('updated') or [])} -{len(result.get('removed') or [])}")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def validate_cron(cron: str) -> Tuple[bool, str]:
    text = (cron or "").strip().replace("?", "*")
    fields = text.split()
    if len(fields) != 5:
        return False, ""
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for chunk, (low, high) in zip(fields, ranges):
        if not re.match(r"^(\*|(\*/\d+)|(\d+(-\d+)?)(/\d+)?(,\d+(-\d+)?(/\d+)?)*)$", chunk):
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
    return True, text


def mask_secrets(text: str, secrets: Optional[Sequence[str]] = None) -> str:
    result = str(text)
    for secret in secrets or []:
        if secret and len(secret) >= 4:
            result = result.replace(secret, secret[:2] + "***" + secret[-1:])
    result = re.sub(r"(https?://)[^@/\s]+:[^@/\s]+@", r"\1***:***@", result)
    result = re.sub(r"(api_key=|apikey=|token=|password=)([^\s&,]{4,})", r"\1***", result, flags=re.IGNORECASE)
    return result


def _ts_text(ts: Any) -> str:
    import datetime  # 局部导入
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return "—"
