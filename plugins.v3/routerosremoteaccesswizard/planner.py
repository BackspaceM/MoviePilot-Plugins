"""RouterOS 外网访问向导 —— 端口映射计划渲染（零宿主依赖纯逻辑层）。

一个服务 = dst-nat 映射 + forward 放行（可选 Hairpin NAT）。所有条目统一带
``MP-B1[<服务>:<用途>]`` 备注标签，用于幂等跳过与精确回滚；备注里同时写入
域名和「外网端口 → 内网服务」的中文含义，方便以后在路由器上一眼认出。

与 A2 场景模板共用同一份 DeployAction / 引擎接口，因此预览、批量下发、
备份与失败逆序回滚的行为完全一致。
"""
from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from .discovery import Service, clean_value, services_from_plan

DEFAULT_PREFIX = "MP-B1"

SEC_NAT = "/rest/ip/firewall/nat"
SEC_FILTER = "/rest/ip/firewall/filter"

SECTIONS: Dict[str, Dict[str, Any]] = {
    SEC_NAT: {"label": "IPv4 防火墙｜nat", "cli": "/ip/firewall/nat", "orderable": True},
    SEC_FILTER: {"label": "IPv4 防火墙｜filter", "cli": "/ip/firewall/filter", "orderable": True},
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

_IFACE = re.compile(r"^[A-Za-z0-9_.:/\-]{1,64}$")
_CIDR = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_PUBLIC_RISK = "会把内网服务开放到公网，建议先配好强密码 / HTTPS / 访问白名单"
_HAIRPIN_RISK = "内网设备用域名或公网 IP 访问时需要这条回环规则"


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
    table = str.maketrans({
        "[": "［", "]": "］", "`": "ˋ", "<": "‹", ">": "›", "_": "＿",
        "*": "*" if keep_star else "＊",
    })
    return str(text if text is not None else "").translate(table)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _is_port(value: Any) -> bool:
    try:
        return 1 <= int(value) <= 65535
    except (TypeError, ValueError):
        return False


def _valid_ip(text: str) -> Tuple[bool, str]:
    try:
        addr = ipaddress.IPv4Address(str(text).strip())
    except (ipaddress.AddressValueError, ValueError):
        return False, f"「{text}」不是 IPv4 地址（dst-nat 的 to-addresses 需要填 IP，域名请改成 IP）"
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return False, f"「{text}」不是可用的内网服务地址"
    return True, ""


def validate_options(options: Any) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """规范化并校验全局参数，返回 (opts, errors, warnings)。"""
    o = dict(options or {})
    errors: List[str] = []
    warnings: List[str] = []
    wan_if = clean_value(o.get("wan_if") or "ether1", 64)
    if not _IFACE.match(wan_if):
        errors.append(f"WAN 接口名「{wan_if}」含非法字符")
    protocol = clean_value(o.get("protocol") or "tcp", 16).lower()
    if protocol not in ("tcp", "udp", "both"):
        errors.append(f"协议「{protocol}」只支持 tcp / udp / both")
    lan_subnet = clean_value(o.get("lan_subnet"), 64)
    if lan_subnet:
        ok = bool(_CIDR.match(lan_subnet))
        if ok:
            try:
                ipaddress.IPv4Network(lan_subnet, strict=False)
            except (ipaddress.NetmaskValueError, ValueError):
                ok = False
        if not ok:
            errors.append(f"内网网段「{lan_subnet}」不是合法 CIDR，如 192.168.88.0/24")
    domain = clean_value(o.get("domain"), 120)
    public_host = clean_value(o.get("public_host"), 120)
    for label, host in (("域名", domain), ("公网地址", public_host)):
        if host and not _HOSTNAME.match(host) and not _valid_ip(host)[0]:
            errors.append(f"{label}「{host}」格式不对")
    try:
        start = int(o.get("wan_port_start") or 18000)
    except (TypeError, ValueError):
        start = 0
    if not _is_port(start):
        errors.append(f"外网端口起始值「{o.get('wan_port_start')}」需在 1~65535")
    try:
        probe_timeout = max(1, min(30, int(o.get("probe_timeout") or 5)))
    except (TypeError, ValueError):
        probe_timeout = 5
    if not domain:
        warnings.append("没填域名，备注里只会记录外网端口；填了域名后续排查更方便")
    return {
        "wan_if": wan_if, "protocol": protocol, "lan_subnet": lan_subnet,
        "domain": domain, "public_host": public_host,
        "wan_port_start": start, "probe_timeout": probe_timeout,
        "hairpin": bool(lan_subnet),
    }, errors, warnings


def _protocol_payload(protocol: str) -> Dict[str, str]:
    if protocol == "both":
        return {}
    return {"protocol": protocol}


def _public_label(opts: Dict[str, Any]) -> str:
    return opts.get("domain") or opts.get("public_host") or "公网"


def allocate_wan_ports(services: Sequence[Service], start: int,
                       occupied: Sequence[int] = ()) -> Tuple[Dict[str, int], List[str]]:
    """给未指定外网端口的服务分配端口：跳过已占用和已分配端口。"""
    used = {int(p) for p in occupied if _is_port(p)}
    assigned: Dict[str, int] = {}
    errors: List[str] = []
    for svc in services:
        if svc.wan_port:
            if not _is_port(svc.wan_port):
                errors.append(f"「{svc.name}」的外网端口 {svc.wan_port} 不在 1~65535")
                continue
            assigned[svc.id] = int(svc.wan_port)
            used.add(int(svc.wan_port))
    cursor = int(start or 18000)
    for svc in services:
        if svc.id in assigned:
            continue
        while cursor in used or not _is_port(cursor):
            cursor += 1
            if cursor > 65535:
                errors.append("外网端口分配耗尽，请调小起始端口或减少服务数量")
                return assigned, errors
        assigned[svc.id] = cursor
        used.add(cursor)
        cursor += 1
    return assigned, errors


def render_plan(services: Any, options: Any, prefix: str = DEFAULT_PREFIX,
                allow_public: bool = False,
                occupied_ports: Sequence[int] = ()) -> Dict[str, Any]:
    """渲染成一份「待确认部署计划」（结构兼容 A2 引擎）。"""
    if isinstance(services, dict):
        svc_list = services_from_plan(services)
    else:
        svc_list = [s if isinstance(s, Service) else Service.from_dict(s)
                    for s in (services or [])]
    opts, errors, warnings = validate_options(options)
    clean_prefix = (clean_value(prefix) or DEFAULT_PREFIX).replace("[", "").replace("]", "")[:24]

    if not svc_list:
        errors.append("还没选服务：到设置页勾选已连接实例，或在「手动补充」里写一行")
    if not allow_public:
        errors.append("端口映射会把内网服务开放到公网，需先在设置页勾选「允许公网映射」")
    for svc in svc_list:
        ok, msg = _valid_ip(svc.lan_ip)
        if not ok:
            errors.append(f"服务「{svc.name}」：{msg}")
        if not _is_port(svc.lan_port):
            errors.append(f"服务「{svc.name}」的内网端口 {svc.lan_port} 不在 1~65535")

    ports, port_errors = allocate_wan_ports(svc_list, opts["wan_port_start"],
                                            occupied_ports)
    errors.extend(port_errors)
    seen: Dict[int, str] = {}
    for svc in svc_list:
        port = ports.get(svc.id, 0)
        if port in seen and seen[port] != svc.id:
            errors.append(f"外网端口 {port} 被「{seen[port]}」和「{svc.name}」重复使用")
        else:
            seen[port] = svc.id
    occupied = {int(p) for p in occupied_ports if _is_port(p)}
    for svc in svc_list:
        if ports.get(svc.id, 0) in occupied:
            warnings.append(f"外网端口 {ports.get(svc.id)} 上已有别的 NAT 规则，"
                            f"「{svc.name}」可能不会生效，建议换端口")

    actions: List[DeployAction] = []
    rows: List[Dict[str, Any]] = []
    sections: List[str] = []
    if not errors:
        for svc in svc_list:
            wan_port = int(ports[svc.id])
            public = _public_label(opts)
            mapping = f"{public}:{wan_port} → {svc.name}（{svc.lan_ip}:{svc.lan_port}）"
            domain_note = (f"域名:{opts['domain']}" if opts["domain"] else "未填域名")
            base_comment = f"{domain_note}｜{wan_port} → {svc.name} {svc.lan_ip}:{svc.lan_port}"
            proto = _protocol_payload(opts["protocol"])
            proto_text = {"tcp": "TCP", "udp": "UDP", "both": "TCP/UDP"}[opts["protocol"]]

            def add(step: str, section: str, payload: Dict[str, Any], meaning: str,
                    risk: str, level: str, placement: str, chain: str) -> None:
                tag = f"{clean_prefix}[{svc.id}:{step}]"
                body = dict(payload)
                body["comment"] = clean_value(f"{tag} {base_comment}", 220)
                action = DeployAction(
                    index=len(actions) + 1, template_id=svc.id,
                    template_name=svc.name, tag=tag, section=section,
                    payload=body, meaning=meaning, risk=risk,
                    risk_level=level, placement=placement, chain=chain,
                )
                actions.append(action)
                if section not in sections:
                    sections.append(section)

            nat_payload: Dict[str, Any] = {
                "chain": "dstnat", "action": "dst-nat",
                "dst-port": str(wan_port), "in-interface": opts["wan_if"],
                "to-addresses": svc.lan_ip, "to-ports": str(svc.lan_port),
            }
            nat_payload.update(proto)
            add("nat", SEC_NAT, nat_payload,
                f"{proto_text} {mapping}：把外网 {wan_port} 转到内网 {svc.lan_ip}:{svc.lan_port}",
                _PUBLIC_RISK, "high", PLACE_BEFORE_DROP, "dstnat")

            fw_payload: Dict[str, Any] = {
                "chain": "forward", "action": "accept",
                "dst-address": svc.lan_ip, "dst-port": str(svc.lan_port),
                "in-interface": opts["wan_if"],
            }
            fw_payload.update(proto)
            add("fw", SEC_FILTER, fw_payload,
                f"{proto_text} {mapping}：允许转换后的流量进到 {svc.lan_ip}:{svc.lan_port}",
                _PUBLIC_RISK + "；这条要排在兜底 drop 之前", "high",
                PLACE_BEFORE_DROP, "forward")

            if opts["hairpin"]:
                hp_payload: Dict[str, Any] = {
                    "chain": "srcnat", "action": "masquerade",
                    "src-address": opts["lan_subnet"], "dst-address": svc.lan_ip,
                    "dst-port": str(svc.lan_port),
                }
                hp_payload.update(proto)
                add("hairpin", SEC_NAT, hp_payload,
                    f"{proto_text} {mapping}：让内网设备用 {public} 也能访问",
                    _HAIRPIN_RISK, "medium", PLACE_TOP, "srcnat")

            rows.append({
                "id": svc.id, "name": svc.name, "kind": svc.kind,
                "lan_ip": svc.lan_ip, "lan_port": svc.lan_port,
                "wan_port": wan_port, "scheme": svc.scheme,
                "domain": opts["domain"], "public_host": opts["public_host"],
                "mapping": mapping, "note": svc.note, "source": svc.source,
                "actions": sum(1 for a in actions if a.template_id == svc.id),
            })

    created = now_text()
    return {
        "created": created, "prefix": clean_prefix,
        "services": rows, "actions": [a.to_dict() for a in actions],
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


def occupied_ports_from_rows(rows: Sequence[Dict[str, Any]], prefix: str) -> List[int]:
    """从现有 nat 规则里取出被别人占用的外网端口（排除本插件自己的标签）。"""
    out: List[int] = []
    for row in rows or []:
        if prefix in str(row.get("comment") or ""):
            continue
        if str(row.get("chain") or "").lower() != "dstnat":
            continue
        raw = str(row.get("dst-port") or "")
        for part in raw.replace(" ", "").split(","):
            if "-" in part:
                continue
            if part.isdigit() and 1 <= int(part) <= 65535:
                out.append(int(part))
    return out
