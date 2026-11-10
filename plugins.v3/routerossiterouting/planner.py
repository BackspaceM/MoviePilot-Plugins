"""RouterOS 站点智能分流 / DNS —— 站点计划渲染（零宿主依赖纯逻辑层）。

一个站点 = 若干 static DNS 正向记录 + 地址列表条目（主域名与各 Tracker 域名去重）
+ 可选 mangle 连接/包标记 + 可选策略路由 + 可选 filter 放行。所有条目统一带
``MP-B3[<站点id>:<用途>] <中文含义>`` 备注标签，用于幂等跳过、精确回滚和
失效站点清理；所有文本进 payload 前过 ``clean_value``。

与 A2 场景模板、B1 端口映射共用同一份 DeployAction / 引擎接口，因此预览、
批量下发、备份与失败逆序回滚的行为完全一致。
"""
from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from .sites import Site, extract_domain, sites_from_plan

DEFAULT_PREFIX = "MP-B3"

SEC_DNS = "/rest/ip/dns/static"
SEC_ADDR = "/rest/ip/firewall/address-list"
SEC_MANGLE = "/rest/ip/firewall/mangle"
SEC_ROUTE = "/rest/ip/route"
SEC_FILTER = "/rest/ip/firewall/filter"

# REST 分区 -> 展示元信息。orderable=True 表示支持 place-before（规则链）
SECTIONS: Dict[str, Dict[str, Any]] = {
    SEC_DNS: {"label": "DNS｜静态解析", "cli": "/ip/dns/static", "orderable": False},
    SEC_ADDR: {"label": "IPv4 防火墙｜地址列表", "cli": "/ip/firewall/address-list",
               "orderable": False},
    SEC_MANGLE: {"label": "IPv4 防火墙｜mangle", "cli": "/ip/firewall/mangle",
                 "orderable": True},
    SEC_ROUTE: {"label": "IP｜路由", "cli": "/ip/route", "orderable": False},
    SEC_FILTER: {"label": "IPv4 防火墙｜filter", "cli": "/ip/firewall/filter",
                 "orderable": True},
}

RISK_LABELS: Dict[str, str] = {"low": "低", "medium": "中", "high": "高"}
RISK_COLORS: Dict[str, str] = {"low": "success", "medium": "warning", "high": "error"}

PLACE_APPEND = "append"
PLACE_BEFORE_DROP = "before_drop"
PLACE_TOP = "top"
PLACE_LABELS: Dict[str, str] = {
    PLACE_APPEND: "追加到分区末尾",
    PLACE_BEFORE_DROP: "插到兜底 drop 之前",
    PLACE_TOP: "插到链首",
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ROS_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,63}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
_CRON_PART = re.compile(
    r"^(\*|(\*/\d+)|(\d+(-\d+)?)(/\d+)?(,\d+(-\d+)?(/\d+)?)*)$")


def clean_value(value: Any, limit: int = 120) -> str:
    """去掉控制字符并限长；所有用户文本进 payload / 备注前都要过一遍。"""
    text = _CONTROL.sub("", str(value if value is not None else "")).strip()
    return text[:limit]


@dataclass
class DeployAction:
    """一条待部署配置（预览、下发、回滚共用同一份数据）。"""

    index: int = 0
    template_id: str = ""
    template_name: str = ""
    tag: str = ""
    section: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    meaning: str = ""
    risk: str = ""
    risk_level: str = "low"
    placement: str = PLACE_APPEND
    chain: str = ""
    state: str = "pending"
    detail: str = ""

    @property
    def section_label(self) -> str:
        return str(SECTIONS.get(self.section, {}).get("label") or self.section)

    @property
    def command(self) -> str:
        return format_command(self.section, self.payload)

    @property
    def orderable(self) -> bool:
        return bool(SECTIONS.get(self.section, {}).get("orderable"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "template_id": self.template_id,
            "template_name": self.template_name, "tag": self.tag,
            "section": self.section, "section_label": self.section_label,
            "payload": dict(self.payload), "meaning": self.meaning,
            "risk": self.risk, "risk_level": self.risk_level,
            "placement": self.placement, "chain": self.chain,
            "state": self.state, "detail": self.detail,
            "command": self.command,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DeployAction":
        d = dict(data or {})
        return cls(
            index=int(d.get("index") or 0),
            template_id=str(d.get("template_id") or ""),
            template_name=str(d.get("template_name") or ""),
            tag=str(d.get("tag") or ""),
            section=str(d.get("section") or ""),
            payload=dict(d.get("payload") or {}),
            meaning=str(d.get("meaning") or ""),
            risk=str(d.get("risk") or ""),
            risk_level=str(d.get("risk_level") or "low"),
            placement=str(d.get("placement") or PLACE_APPEND),
            chain=str(d.get("chain") or ""),
            state=str(d.get("state") or "pending"),
            detail=str(d.get("detail") or ""),
        )


def actions_from_plan(plan: Any) -> List[DeployAction]:
    return [DeployAction.from_dict(d) for d in (dict(plan or {}).get("actions") or [])]


def plan_tags(plan: Any) -> List[str]:
    """取出一份计划里所有动作标签，供失效条目比对。"""
    return [str(a.get("tag") or "") for a in (dict(plan or {}).get("actions") or [])]


def format_command(section: str, payload: Dict[str, Any], place_before: str = "") -> str:
    """生成等价 RouterOS CLI 命令（仅供核对，不执行）。"""
    cli = str(SECTIONS.get(section, {}).get("cli") or section.replace("/rest", "", 1))
    parts = [f"{cli} add"]
    for key, value in dict(payload or {}).items():
        if key in ("place-before", ".id", "id"):
            continue
        text = ("yes" if value else "no") if isinstance(value, bool) else str(value)
        if not text:
            continue
        if re.search(r"\s", text) or any(c in text for c in "\"'{}[]"):
            text = '"' + text.replace('"', "'") + '"'
        parts.append(f"{key}={text}")
    if place_before:
        parts.append(f"place-before={place_before}")
    return " ".join(parts)


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def md_safe(text: Any, keep_star: bool = False) -> str:
    """半角符号转全角，避免 [xxx] / *xxx* 被前端当 Markdown 渲染。"""
    table = str.maketrans({
        "[": "［", "]": "］", "`": "ˋ", "<": "‹", ">": "›", "_": "＿",
        "*": "*" if keep_star else "＊",
    })
    return str(text if text is not None else "").translate(table)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _valid_ipv4(text: Any) -> Tuple[bool, str]:
    value = clean_value(text, 64)
    try:
        addr = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError):
        return False, f"「{value}」不是 IPv4 地址"
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return False, f"「{value}」不是可用的 IP 地址"
    return True, ""


def _valid_domain(value: Any) -> Tuple[bool, str]:
    """域名合法性：允许子域，拒绝空格、控制字符、空标签与超长标签。"""
    text = clean_value(value, 253).strip(".").lower()
    if not text or not _HOSTNAME.match(text) or ".." in text:
        return False, f"域名「{text}」格式不对"
    for label in text.split("."):
        if not label or label.startswith("-") or label.endswith("-") or len(label) > 63:
            return False, f"域名「{text}」格式不对"
    return True, ""


def _valid_ident(key: str, default: str, label: str,
                 options: Dict[str, Any], errors: List[str]) -> str:
    value = clean_value(options.get(key) or default, 64)
    if not _ROS_IDENT.match(value):
        errors.append(f"{label}「{value}」只能含字母数字和 _ . : / -")
    return value


def validate_cron(cron: Any) -> Tuple[bool, str]:
    """5 位 crontab；返回 (是否合法, 规范化文本)。"""
    text = clean_value(cron, 80).replace("?", "*")
    fields = text.split()
    if len(fields) != 5:
        return False, ""
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for chunk, (low, high) in zip(fields, ranges):
        if not _CRON_PART.match(chunk):
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


def validate_options(options: Any) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """规范化并校验全局参数，返回 (opts, errors, warnings)。"""
    o = dict(options or {})
    errors: List[str] = []
    warnings: List[str] = []

    dns_target_ip = clean_value(o.get("dns_target_ip"), 64)
    if not dns_target_ip:
        errors.append("static DNS 的解析目标没填，通常是 CF 优选出来的那个 IP")
    else:
        ok, msg = _valid_ipv4(dns_target_ip)
        if not ok:
            errors.append(msg)

    address_list_name = _valid_ident("address_list_name", "mp-pt-sites", "地址列表名",
                                     o, errors)
    conn_mark = _valid_ident("conn_mark", "mp-pt-conn", "连接标记名", o, errors)
    packet_mark = _valid_ident("packet_mark", "mp-pt-pkt", "包标记名", o, errors)
    routing_table = _valid_ident("routing_table", "mp-pt-route", "路由表名", o, errors)

    gateway = clean_value(o.get("gateway"), 64)
    enable_mangle = bool(o.get("enable_mangle"))
    enable_policy_route = bool(o.get("enable_policy_route"))
    enable_filter_accept = bool(o.get("enable_filter_accept"))
    if enable_policy_route and not gateway:
        errors.append("开了策略路由就得填网关 IP")
    if gateway:
        ok, msg = _valid_ipv4(gateway)
        if not ok:
            errors.append(f"网关「{gateway}」不是合法 IPv4 地址")

    ok, cron = validate_cron(str(o.get("cron") or "0 5 * * *"))
    if not ok:
        errors.append("定时 cron 得是 5 位，例如 0 5 * * *")

    return {
        "dns_target_ip": dns_target_ip,
        "address_list_name": address_list_name,
        "conn_mark": conn_mark, "packet_mark": packet_mark,
        "routing_table": routing_table, "gateway": gateway,
        "enable_mangle": enable_mangle,
        "enable_policy_route": enable_policy_route,
        "enable_filter_accept": enable_filter_accept,
        "cron": cron,
    }, errors, warnings


def stale_tags(existing_rows: Any, desired_tags: Sequence[str],
               prefix: str = DEFAULT_PREFIX) -> List[str]:
    """找出设备上带 ``prefix[...]`` 标签、但已不在当前站点清单里的标签。"""
    marker = clean_value(prefix, 24) or DEFAULT_PREFIX
    pattern = re.compile(re.escape(marker) + r"\[[^\]\r\n]+\]")
    desired = {str(t) for t in (desired_tags or []) if str(t)}
    stale = set()
    for row in (existing_rows or []):
        data = row if isinstance(row, dict) else {}
        for tag in pattern.findall(str(data.get("comment") or "")):
            if tag not in desired:
                stale.add(tag)
    return sorted(stale)


# ---------------------------------------------------------------------------
# 计划渲染
# ---------------------------------------------------------------------------

def render_plan(sites: Any, options: Any, prefix: str = DEFAULT_PREFIX,
                allow_high_risk: bool = False,
                existing_domains: Sequence[str] = ()) -> Dict[str, Any]:
    """渲染成一份「待确认部署计划」（结构兼容 A2 / B1 引擎）。

    ``existing_domains`` 是设备上已有的 static DNS 域名（常见于 CF 优选 IP
    插件留下的记录），同域名不再新增解析，避免重名冲突。
    """
    if isinstance(sites, dict):
        site_list = sites_from_plan(sites)
    else:
        site_list = [s if isinstance(s, Site) else Site.from_dict(s)
                     for s in (sites or [])]
    opts, errors, warnings = validate_options(options)
    clean_prefix = (clean_value(prefix) or DEFAULT_PREFIX).replace("[", "").replace("]", "")[:24]
    existing = {extract_domain(d) for d in (existing_domains or []) if extract_domain(d)}

    if not site_list:
        errors.append("还没选站点：到设置页勾选，或在「手动补充站点」里写一行")
    for site in site_list:
        ok, msg = _valid_domain(site.domain)
        if not ok:
            errors.append(f"站点「{site.name}」：{msg}")
        for tracker in (site.tracker_domains or []):
            ok, msg = _valid_domain(tracker)
            if not ok:
                errors.append(f"站点「{site.name}」的 tracker 域名：{msg}")

    # 与 A2 高危模板一致：中高风险动作必须显式放行，否则整份计划拦截
    if (opts["enable_mangle"] or opts["enable_policy_route"] or
            opts["enable_filter_accept"]) and not allow_high_risk:
        errors.append("mangle 标记和策略路由会改流量走向，先在设置页勾选「允许高风险动作」")

    actions: List[DeployAction] = []
    rows: List[Dict[str, Any]] = []
    sections: List[str] = []
    if not errors:
        for site in site_list:
            domains: List[str] = []
            for raw in [site.domain] + list(site.tracker_domains or []):
                host = extract_domain(raw)
                if host and host not in domains:
                    domains.append(host)

            def add(step: str, section: str, payload: Dict[str, Any],
                    meaning: str, risk: str, level: str,
                    placement: str, chain: str) -> None:
                tag = f"{clean_prefix}[{clean_value(site.id, 40)}:{clean_value(step, 80)}]"
                body = dict(payload)
                text = clean_value(meaning, 200)
                body["comment"] = clean_value(f"{tag} {text}", 220)
                action = DeployAction(
                    index=len(actions) + 1, template_id=site.id,
                    template_name=site.name, tag=tag, section=section,
                    payload=body, meaning=text, risk=clean_value(risk, 120),
                    risk_level=level, placement=placement, chain=chain,
                )
                actions.append(action)
                if section not in sections:
                    sections.append(section)

            for domain in domains:
                if domain in existing:
                    warnings.append(f"域名「{domain}」设备上已有解析记录，跳过新增 static DNS")
                else:
                    add(f"dns-{domain}", SEC_DNS,
                        {"name": clean_value(domain, 253), "type": "A",
                         "address": opts["dns_target_ip"]},
                        f"{domain} 指到 {opts['dns_target_ip']}，配合 CF 优选 IP",
                        "只是加一条静态解析", "low", PLACE_APPEND, "")
                add(f"addr-{domain}", SEC_ADDR,
                    {"list": opts["address_list_name"], "address": clean_value(domain, 253)},
                    f"把 {domain} 放进地址列表 {opts['address_list_name']}",
                    "地址列表条目，给 mangle 匹配用", "low", PLACE_APPEND, "")

            if opts["enable_mangle"]:
                add("mangle-conn", SEC_MANGLE,
                    {"chain": "prerouting", "action": "mark-connection",
                     "dst-address-list": opts["address_list_name"],
                     "new-connection-mark": opts["conn_mark"],
                     "passthrough": "yes"},
                    f"访问 {site.name} 的连接打上 {opts['conn_mark']} 标记",
                    "mangle 改连接标记，顺序不对会不生效", "high",
                    PLACE_TOP, "prerouting")
                add("mangle-pkt", SEC_MANGLE,
                    {"chain": "prerouting", "action": "mark-packet",
                     "connection-mark": opts["conn_mark"],
                     "new-packet-mark": opts["packet_mark"],
                     "passthrough": "no"},
                    f"给 {site.name} 的包打上 {opts['packet_mark']} 标记",
                    "mangle 改包标记，顺序不对会不生效", "high",
                    PLACE_TOP, "prerouting")

            if opts["enable_policy_route"]:
                add("route", SEC_ROUTE,
                    {"dst-address": "0.0.0.0/0", "gateway": opts["gateway"],
                     "routing-table": opts["routing_table"]},
                    f"标记过的流量从 {opts['gateway']} 走 {opts['routing_table']} 路由表",
                    "策略路由会改默认走向，网关写错可能断网", "high",
                    PLACE_APPEND, "")

            if opts["enable_filter_accept"]:
                add("fw", SEC_FILTER,
                    {"chain": "forward", "action": "accept",
                     "dst-address-list": opts["address_list_name"]},
                    f"放行到 {site.name} 的流量",
                    "放行规则要排在兜底 drop 之前", "medium",
                    PLACE_BEFORE_DROP, "forward")

            rows.append({
                "id": site.id, "name": site.name, "domain": site.domain,
                "tracker_domains": list(site.tracker_domains or []),
                "domains": domains, "enabled": site.enabled,
                "source": site.source, "note": site.note,
                "actions": sum(1 for a in actions if a.template_id == site.id),
            })

    created = now_text()
    return {
        "created": created, "prefix": clean_prefix,
        "sites": rows, "actions": [a.to_dict() for a in actions],
        "errors": errors, "warnings": warnings, "sections": sections,
        "high_risk": any(a.risk_level == "high" for a in actions),
        "blocked": bool(errors),
        "options": {k: v for k, v in opts.items()},
        "summary": {
            "services": len(rows), "actions": len(actions),
            "sections": len(sections),
            "high": sum(1 for a in actions if a.risk_level == "high"),
            "medium": sum(1 for a in actions if a.risk_level == "medium"),
            "created": created,
        },
    }
