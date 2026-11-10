"""RouterOS 配置备份与漂移检测 —— 零宿主依赖的纯逻辑层。

所有可被单测覆盖的解析、归一化、指纹、对比与格式化都放在这里，
避免与 MoviePilot 宿主耦合，方便脱离 MP 运行环境做单元测试。

核心概念：
- SECTIONS：可备份的 RouterOS 配置分区（REST 路径 + 是否默认勾选 + 是否敏感）。
- 快照（snapshot）：{section_key: [规范化行, ...]}，行已剔除 .id 与易变统计字段。
- 指纹（fingerprint）：对一行配置做稳定哈希；对比时以"多重集"方式匹配，
  同一配置在基线与当前各出现一次即视为未变，否则记为新增/删除。
- 漂移（drift）：当前快照相对基线的 新增 / 删除 / 修改 三类差异。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 配置分区定义
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    key: str          # 分区内部标识
    path: str         # RouterOS REST 路径（不含 /rest 前缀由客户端拼）
    label: str        # 中文显示名
    default_on: bool  # 是否默认勾选备份
    sensitive: bool   # 是否敏感（漂移时高优先级告警）


# 该列表是插件的"可备份范围"，顺序即详情页展示顺序。
SECTIONS: Tuple[Section, ...] = (
    Section("identity", "/system/identity", "系统标识", True, False),
    Section("ip_address", "/ip/address", "IP 地址", True, False),
    Section("ip_route", "/ip/route", "路由表", True, True),
    Section("ip_dns_static", "/ip/dns/static", "DNS 静态记录", True, True),
    Section("ip_dhcp_server", "/ip/dhcp-server", "DHCP 服务", True, False),
    Section("ip_dhcp_lease", "/ip/dhcp-server/lease", "DHCP 租约", False, False),
    Section("ip_pool", "/ip/pool", "IP 地址池", True, False),
    Section("firewall_filter", "/ip/firewall/filter", "防火墙过滤规则", True, True),
    Section("firewall_nat", "/ip/firewall/nat", "NAT 规则", True, True),
    Section("firewall_mangle", "/ip/firewall/mangle", "Mangle 规则", False, True),
    Section("firewall_addresslist", "/ip/firewall/address-list", "防火墙地址列表", True, True),
    Section("firewall_serviceport", "/ip/firewall/service-port", "服务端口", False, True),
    Section("queue_simple", "/queue/simple", "简单队列", True, False),
    Section("ip_service", "/ip/service", "RouterOS 服务(端口/ACL)", True, True),
    Section("ip_neighbor", "/ip/neighbor/discovery-settings", "邻居发现", False, False),
    Section("system_user", "/system/user", "系统用户", False, True),
    Section("system_scheduler", "/system/scheduler", "计划任务", False, True),
    Section("system_script", "/system/script", "脚本", False, True),
    Section("system_ntp_client", "/system/ntp/client", "NTP 客户端", False, False),
    Section("interface", "/interface", "接口", False, False),
)

SECTION_MAP: Dict[str, Section] = {s.key: s for s in SECTIONS}
DEFAULT_SECTIONS: List[str] = [s.key for s in SECTIONS if s.default_on]

# 易变字段：每次读取都会变化，不纳入指纹，避免"假漂移"。
# 以 "." 开头的键（如 .id）统一剔除。
VOLATILE_KEYS: Tuple[str, ...] = (
    "bytes", "packets", "rx-byte", "tx-byte", "rx-packet", "tx-packet",
    "rx", "tx", "rx-bits-per-second", "tx-bits-per-second", "rate",
    "uptime", "last-seen", "last-linked", "last-activity", "health",
    "cpu-load", "free-memory", "total-memory", "free-hdd-space", "total-hdd-space",
    "actual-mtu", "dynamic", "invalid", "disabled-since",
)


def section_items() -> List[Dict[str, str]]:
    """给配置页下拉框用的分区选项。"""
    return [{"title": f"{s.label}（{s.key}）", "value": s.key} for s in SECTIONS]


def section_label(key: str) -> str:
    s = SECTION_MAP.get(key)
    return s.label if s else key


def is_sensitive(key: str) -> bool:
    s = SECTION_MAP.get(key)
    return bool(s and s.sensitive)


# ---------------------------------------------------------------------------
# 快照归一化与指纹
# ---------------------------------------------------------------------------

def normalize_row(row: Any, extra_volatile: Optional[Sequence[str]] = None) -> Dict[str, str]:
    """把一行 RouterOS REST 响应规范化：剔除易变字段，值统一转字符串并排序。"""
    if not isinstance(row, dict):
        return {}
    drop = set(VOLATILE_KEYS)
    if extra_volatile:
        drop.update(str(x) for x in extra_volatile)
    out: Dict[str, str] = {}
    for key in sorted(row.keys(), key=str):
        k = str(key)
        if k.startswith(".") or k in drop:
            continue
        value = row.get(key)
        if value is None:
            out[k] = ""
        elif isinstance(value, bool):
            out[k] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            out[k] = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        else:
            out[k] = str(value)
    return out


def row_fingerprint(row: Any, extra_volatile: Optional[Sequence[str]] = None) -> str:
    """为一配置行生成稳定指纹（SHA256 前 16 位）。易变字段不参与。"""
    normalized = normalize_row(row, extra_volatile)
    if not normalized:
        # 空行/无效行用一个固定占位，避免整行噪声；调用方可选择跳过空行。
        payload = "{}"
    else:
        payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def rows_to_snapshot(rows: Iterable[Any], extra_volatile: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """把某分区的原始行列表转成规范化快照行（丢弃完全空行）。"""
    out: List[Dict[str, str]] = []
    for raw in rows or []:
        norm = normalize_row(raw, extra_volatile)
        if norm:
            out.append(norm)
    return out


def make_snapshot(raw_sections: Dict[str, List[Any]],
                  section_keys: Sequence[str],
                  extra_volatile: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """把一次抓取的原始分区数据组装成带指纹的快照。

    返回结构::

        {
          "schema": 1,
          "ts": 1690000000.0,
          "sections": {
             "ip_dns_static": {
                "label": "DNS 静态记录",
                "sensitive": true,
                "count": 2,
                "rows": [ {字段...}, ... ],
                "fingerprints": ["ab12...", "cd34..."]
             }, ...
          }
        }
    """
    import time  # 局部导入，保持模块顶层零副作用

    snap: Dict[str, Any] = {"schema": 1, "ts": time.time(), "sections": {}}
    for key in section_keys:
        sec = SECTION_MAP.get(key)
        rows = rows_to_snapshot(raw_sections.get(key) or [], extra_volatile)
        fps = [row_fingerprint(r, extra_volatile) for r in rows]
        snap["sections"][key] = {
            "label": sec.label if sec else key,
            "sensitive": bool(sec and sec.sensitive),
            "count": len(rows),
            "rows": rows,
            "fingerprints": fps,
        }
    return snap


# ---------------------------------------------------------------------------
# 漂移检测（多重集对比）
# ---------------------------------------------------------------------------

def _fingerprint_counts(snap: Optional[Dict[str, Any]], key: str) -> Tuple[Dict[str, int], Dict[str, Dict[str, str]]]:
    """返回某分区 {指纹: 出现次数} 与 {指纹: 规范化行}。"""
    counts: Dict[str, int] = {}
    samples: Dict[str, Dict[str, str]] = {}
    sections = (snap or {}).get("sections", {}) if isinstance(snap, dict) else {}
    sec = sections.get(key) or {}
    fps = sec.get("fingerprints")
    rows = sec.get("rows") or []
    if not isinstance(fps, list):
        # 兼容旧快照：现场按行重算
        fps = [row_fingerprint(r) for r in rows]
    for idx, fp in enumerate(fps):
        fp = str(fp)
        counts[fp] = counts.get(fp, 0) + 1
        if fp not in samples and idx < len(rows):
            samples[fp] = rows[idx] if isinstance(rows[idx], dict) else {}
    return counts, samples


def diff_section(base: Optional[Dict[str, Any]],
                 current: Dict[str, Any],
                 key: str) -> Dict[str, Any]:
    """对比单个分区，返回 {added, removed, changed}。

    - added/removed：按指纹多重集差集计算（重复行按次数计）。
    - changed：暂不单独区分；配置行的"修改"在指纹层面必然表现为
      一行 removed + 一行 added，调用方据此判断即可。
      （这样定义避免把"改了某个字段"误判为未变，语义更强。）
    """
    base_counts, base_samples = _fingerprint_counts(base, key)
    cur_counts, cur_samples = _fingerprint_counts(current, key)

    added: List[Dict[str, str]] = []
    removed: List[Dict[str, str]] = []
    for fp, cnt in cur_counts.items():
        extra = cnt - base_counts.get(fp, 0)
        for _ in range(max(0, extra)):
            added.append(cur_samples.get(fp, {"_fingerprint": fp}))
    for fp, cnt in base_counts.items():
        extra = cnt - cur_counts.get(fp, 0)
        for _ in range(max(0, extra)):
            removed.append(base_samples.get(fp, {"_fingerprint": fp}))

    return {"added": added, "removed": removed, "changed": []}


def diff_snapshots(base: Optional[Dict[str, Any]],
                   current: Dict[str, Any],
                   section_keys: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """对比基线快照与当前快照，返回结构化漂移结果。"""
    base_sections = (base or {}).get("sections", {}) if isinstance(base, dict) else {}
    cur_sections = current.get("sections", {}) if isinstance(current, dict) else {}
    if section_keys is None:
        # 取两边分区键的并集，保持 SECTIONS 的既定顺序
        keys = [s.key for s in SECTIONS if s.key in set(base_sections) | set(cur_sections)]
    else:
        keys = list(section_keys)

    sections: Dict[str, Any] = {}
    total_added = total_removed = 0
    sensitive_hits: List[str] = []
    for key in keys:
        d = diff_section(base, current, key)
        if d["added"] or d["removed"]:
            sections[key] = {
                "label": section_label(key),
                "sensitive": is_sensitive(key),
                "added": d["added"],
                "removed": d["removed"],
                "added_count": len(d["added"]),
                "removed_count": len(d["removed"]),
            }
            total_added += len(d["added"])
            total_removed += len(d["removed"])
            if is_sensitive(key):
                sensitive_hits.append(section_label(key))

    return {
        "has_drift": bool(sections),
        "baseline_ts": (base or {}).get("ts") if isinstance(base, dict) else None,
        "current_ts": current.get("ts") if isinstance(current, dict) else None,
        "sections": sections,
        "totals": {"added": total_added, "removed": total_removed, "sections": len(sections)},
        "sensitive_hits": sensitive_hits,
    }


def describe_row(row: Dict[str, str], preferred: Sequence[str] = ("name", "address", "list", "chain", "comment", "target")) -> str:
    """把一行配置压成一句可读摘要，便于日志/通知展示。"""
    if "_fingerprint" in row:
        return f"指纹 {row['_fingerprint']}"
    for key in preferred:
        if row.get(key):
            extra = ""
            if key in ("address", "target") and row.get("comment"):
                extra = f"（{row['comment']}）"
            return f"{key}={row[key]}{extra}"
    # 退而求其次：取前 2~3 个字段
    items = [f"{k}={v}" for k, v in list(row.items())[:3] if v]
    return ", ".join(items) if items else "(空行)"


def format_diff(diff: Dict[str, Any], max_rows: int = 8) -> str:
    """把漂移结果格式化为多行中文文本（用于通知与详情页）。"""
    if not diff.get("has_drift"):
        return "无配置漂移"
    lines: List[str] = []
    totals = diff.get("totals", {})
    lines.append(f"检测到配置漂移：{totals.get('sections', 0)} 个分区，"
                 f"新增 {totals.get('added', 0)} 项，删除 {totals.get('removed', 0)} 项")
    if diff.get("sensitive_hits"):
        lines.append("⚠ 敏感分区变更：" + "、".join(diff["sensitive_hits"]))
    for key, sec in (diff.get("sections") or {}).items():
        flag = "⚠" if sec.get("sensitive") else "•"
        # 用全角【】而非半角[]：MP 详情页会把 text 当 Markdown 渲染，
        # 连续/成对的半角方括号会被解析成链接引用，导致内容渲染为空白占位（药丸）。
        lines.append(f"{flag} 【{sec.get('label', key)}】 +{sec.get('added_count', 0)} / -{sec.get('removed_count', 0)}")
        for row in sec.get("added", [])[:max_rows]:
            lines.append(f"    ＋ {describe_row(row)}")
        for row in sec.get("removed", [])[:max_rows]:
            lines.append(f"    － {describe_row(row)}")
        if sec.get("added_count", 0) + sec.get("removed_count", 0) > max_rows * 2:
            lines.append("    ……（已截断）")
    return "\n".join(lines)


def summary_text(diff: Dict[str, Any]) -> str:
    """一句话摘要。"""
    if not diff.get("has_drift"):
        return "配置无变化"
    t = diff.get("totals", {})
    return f"漂移 {t.get('sections', 0)} 分区 / +{t.get('added', 0)} -{t.get('removed', 0)}"


# ---------------------------------------------------------------------------
# 详情页文本安全
# ---------------------------------------------------------------------------

def md_safe(text: Any) -> str:
    """把动态文本转义为可在详情页 text 中原样展示的纯文本。

    MoviePilot 前端会把详情页组件里的 text 当 Markdown 渲染：成对的半角
    方括号 `[x][y]` 会被解析成"参考式链接"而只剩空白占位（截图里的药丸），
    反引号/星号等也会触发行内代码或加粗。这里统一替换为视觉等价的全角字符，
    保证日志、差异明细、快照清单等动态内容"所见即所得"。
    """
    s = str(text if text is not None else "")
    table = str.maketrans({
        "[": "［", "]": "］",       # 方括号 → 全角（核心：杜绝参考式链接）
        "`": "ˋ", "*": "＊",        # 反引号 / 星号 → 全角，避免行内代码/加粗
        "<": "‹", ">": "›",        # 尖括号 → 单书名号，避免被当 HTML 标签
        "_": "＿",                 # 下划线 → 全角，避免斜体
    })
    return s.translate(table)


def format_log_line(time_text: str, level: str, message: str) -> str:
    """详情页单行日志：用全角 ｜ 分隔，杜绝 [time][level] 被当 Markdown 链接。"""
    return md_safe(f"{time_text} ｜ {level:<5} ｜ {message}")


# ---------------------------------------------------------------------------
# RouterOS 本机备份文件轮转
# ---------------------------------------------------------------------------

# 备份目标：mp=仅插件数据目录（结构化快照，可对比/回滚）；
# routeros=仅路由器本机（.backup 二进制 + .rsc 文本）；both=两处都存。
BACKUP_TARGETS = (
    {"value": "mp", "title": "仅 MP 插件目录（结构化快照，可对比/回滚）"},
    {"value": "routeros", "title": "仅 RouterOS 本机（系统备份 + 配置导出）"},
    {"value": "both", "title": "两处都存（MP 快照 + RouterOS 本机）"},
)
BACKUP_TARGET_LABELS = {
    "mp": "MP 插件目录",
    "routeros": "RouterOS 本机",
    "both": "MP + RouterOS 双份",
}


def backup_basename(prefix: str, sid: str) -> str:
    """RouterOS 本机备份文件基名（不含 .backup/.rsc 后缀）。"""
    return f"{prefix}_{sid}"


def is_our_backup(name: str, prefix: str) -> bool:
    """判断是否为插件生成的备份文件（.backup / .rsc）。"""
    n = str(name or "")
    return (n.startswith(f"{prefix}_")
            and (n.endswith(".backup") or n.endswith(".rsc")))


def prune_routeros_files(files: Sequence[Dict[str, Any]], prefix: str,
                         keep_pairs: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """对 RouterOS /file 列表按备份基名分组，仅保留最近 keep_pairs 组。

    每个备份基名对应 .backup + .rsc 两个文件，按基名（含 yyyyMMdd_HHMMSS_fff，
    字典序即时间序）倒序保留。返回 (保留, 待删除)。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    others: List[Dict[str, Any]] = []
    for f in files or []:
        name = str(f.get("name") or "")
        if not is_our_backup(name, prefix):
            others.append(f)
            continue
        base = name[:-len(".backup")] if name.endswith(".backup") else name[:-len(".rsc")]
        groups.setdefault(base, []).append(f)
    ordered = sorted(groups.items(), key=lambda kv: kv[0], reverse=True)
    kept: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    for idx, (_base, members) in enumerate(ordered):
        (kept if idx < max(1, int(keep_pairs or 1)) else removed).extend(members)
    return kept + others, removed


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def validate_cron(cron: str) -> Tuple[bool, str]:
    """校验 5 段 crontab 表达式；第二个返回值为规范化后的表达式。"""
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
    """对日志/通知中的密码、token 做脱敏。"""
    result = str(text)
    if secrets:
        for secret in secrets:
            if secret and len(secret) >= 4:
                result = result.replace(secret, secret[:2] + "***" + secret[-1:])
    result = re.sub(r"(https?://)[^@/\s]+:[^@/\s]+@", r"\1***:***@", result)
    result = re.sub(r"(api_key=|apikey=|token=|password=)([^\s&,]{4,})",
                    r"\1***", result, flags=re.IGNORECASE)
    return result


def safe_filename(text: str) -> str:
    """把任意文本清理为安全的文件名片段。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text or "")).strip("-.")
    return cleaned or "snapshot"


def snapshot_id(ts: Optional[float] = None) -> str:
    """生成快照 ID：yyyyMMdd_HHMMSS_fff。"""
    import time  # 局部导入
    value = ts if ts is not None else time.time()
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(value)) + f"_{int((value % 1) * 1000):03d}"


def prune_snapshots(snapshots: List[Dict[str, Any]], keep: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按快照索引列表裁剪，保留最近 keep 个（列表假定按时间倒序），返回 (保留, 已淘汰)。"""
    keep = max(1, int(keep or 1))
    return snapshots[:keep], snapshots[keep:]


def basename_from_types(types: Sequence[str]) -> str:
    """根据分区键生成可读的备份范围描述。"""
    if not types:
        return "全部默认分区"
    if len(types) == len(DEFAULT_SECTIONS) and set(types) == set(DEFAULT_SECTIONS):
        return "默认分区"
    return "、".join(section_label(k) for k in types[:6]) + ("…" if len(types) > 6 else "")
