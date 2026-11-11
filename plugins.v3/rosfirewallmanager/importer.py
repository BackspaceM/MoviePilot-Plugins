"""RouterOS 防火墙规则批量导入 —— 解析脚本 / 规则文本并生成可读含义。

支持三类输入：
- RouterOS `/ip firewall <table>` export 脚本（含 add 行与反斜杠续行）；
- 直接粘贴的 `chain=input action=accept ...` 规则文本；
- 通过配置页上传的文件内容（前端可能以纯文本、base64 或 data URL 传递）。

解析结果只做预览，写入 RouterOS 由主类在用户确认后一次性批量执行。
"""
from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .core import (TABLES, TABLE_LABELS, build_add_payload, md_safe,
                   md_safe_command, preview_command)

_MAX_IMPORT = 100

_FIELD_LABELS: Dict[str, str] = {
    "src-address": "源地址", "dst-address": "目的地址",
    "src-address-list": "源地址列表", "dst-address-list": "目的地址列表",
    "src-mac-address": "源 MAC", "protocol": "协议", "port": "端口",
    "src-port": "源端口", "dst-port": "目的端口",
    "in-interface": "入接口", "out-interface": "出接口",
    "in-interface-list": "入接口列表", "out-interface-list": "出接口列表",
    "connection-state": "连接状态", "connection-nat-state": "NAT 状态",
    "tcp-flags": "TCP 标志", "icmp-options": "ICMP 选项",
    "limit": "速率限制", "content": "内容匹配", "layer7-protocol": "L7 协议",
    "ttl": "TTL", "dscp": "DSCP", "packet-size": "包大小",
    "jump-target": "跳转目标", "address-list": "地址列表",
    "address-list-timeout": "地址列表超时", "to-addresses": "转换到地址",
    "to-ports": "转换到端口", "log-prefix": "日志前缀",
    "src-address-type": "源地址类型", "dst-address-type": "目的地址类型",
}

_ACTION_LABELS: Dict[str, str] = {
    "accept": "放行", "drop": "丢弃", "reject": "拒绝", "tarpit": "粘滞",
    "return": "返回上级链", "jump": "跳转到自定义链", "log": "记录日志",
    "passthrough": "继续匹配", "masquerade": "源地址伪装（MASQUERADE）",
    "src-nat": "源地址转换（SNAT）", "dst-nat": "目的地址转换（DNAT）",
    "redirect": "重定向到本机", "netmap": "网段映射",
    "fasttrack-connection": "FastTrack 加速", "mark-connection": "标记连接",
    "mark-packet": "标记数据包", "mark-routing": "标记路由",
    "add-src-to-address-list": "加入源地址列表",
    "add-dst-to-address-list": "加入目的地址列表",
    "set-priority": "设置优先级", "sniff": "抓包",
}

_SKIP_KEYS = {"chain", "action", "disabled", "comment", "log", "log-prefix", "place-before"}

# 链的自然语言含义：让用户先明白「这条规则管的是哪类流量」
_CHAIN_CN: Dict[str, str] = {
    "input": "发往路由器本机的流量（管理后台、路由器自身服务）",
    "forward": "经过路由器转发的流量（内网 ↔ 外网、内网互访）",
    "output": "路由器自身发出的流量",
    "srcnat": "出方向 NAT（内网访问外网时的地址转换）",
    "dstnat": "入方向 NAT（外部访问内网服务的端口转发）",
    "prerouting": "路由决策前的流量",
    "postrouting": "路由决策后的流量",
}

# 常见端口 → 服务名，用来把「dst-port=8291」翻译成「WinBox 管理」
_PORT_SERVICES: Dict[str, str] = {
    "21": "FTP", "22": "SSH", "23": "Telnet", "53": "DNS",
    "67": "DHCP", "68": "DHCP", "80": "HTTP 网页", "123": "NTP",
    "161": "SNMP", "443": "HTTPS 网页", "500": "IPsec/IKE",
    "1194": "OpenVPN", "1701": "L2TP", "1723": "PPTP",
    "32400": "Plex", "5000": "群晖 DSM", "5001": "群晖 DSM(HTTPS)",
    "51820": "WireGuard", "8080": "HTTP 管理页", "8096": "Emby / Jellyfin",
    "8123": "Home Assistant", "8291": "WinBox 管理",
    "8728": "RouterOS API", "8729": "RouterOS API(SSL)",
    "9000": "Portainer / 管理页",
}

# 这些端口一旦对任意来源 open，属于管理面暴露
_ADMIN_PORTS = {"22", "23", "80", "443", "8291", "8728", "8729", "8080", "9000"}

_SOURCE_MATCHERS = {"src-address", "src-address-list", "in-interface",
                    "in-interface-list", "src-mac-address", "src-address-type"}


def _conditions(payload: Dict[str, Any]) -> str:
    """把匹配条件翻成中文短句。"""
    parts: List[str] = []
    for key, value in payload.items():
        key_l = str(key).lower()
        if key_l in _SKIP_KEYS or not _clean(value):
            continue
        parts.append(f"{_FIELD_LABELS.get(key_l, key_l)}={_clean(value)}")
    return "、".join(parts) if parts else "所有流量（无条件匹配）"


def _source_limited(payload: Dict[str, Any]) -> bool:
    return any(str(k).lower() in _SOURCE_MATCHERS and _clean(payload.get(k))
               for k in payload)


def _port_and_service(payload: Dict[str, Any]) -> Tuple[str, str]:
    raw = _clean(payload.get("dst-port") or payload.get("port")
                 or payload.get("to-ports") or payload.get("src-port"))
    first = raw.replace("，", ",").split(",")[0].strip().split("-")[0].strip()
    return raw, _PORT_SERVICES.get(first, "")


def explain_rule(table: str, payload: Dict[str, Any]) -> Dict[str, str]:
    """把一条规则翻译成「会做什么 / 影响谁 / 风险 / 建议」。

    这是本插件的核心价值：用户不必先懂 RouterOS 命令，导入后就能看懂每条规则。
    """
    chain = _clean(payload.get("chain")).lower() or "?"
    action = _clean(payload.get("action")).lower()
    action_cn = _ACTION_LABELS.get(action, action or "?")
    chain_cn = _CHAIN_CN.get(chain, f"【{chain}】链")
    cond = _conditions(payload)
    src = "指定来源" if _source_limited(payload) else "任意来源"
    port, service = _port_and_service(payload)
    proto = _clean(payload.get("protocol")) or "任意协议"
    port_text = (f"{proto} {port}" + (f"（{service}）" if service else "")) if port else proto
    to_addr = _clean(payload.get("to-addresses"))
    to_port = _clean(payload.get("to-ports"))
    out_if = _clean(payload.get("out-interface") or payload.get("out-interface-list"))
    jump = _clean(payload.get("jump-target"))
    addr_list = _clean(payload.get("address-list"))
    comment = _clean(payload.get("comment"))

    meaning = effect = risk = advice = ""

    if action in ("dst-nat", "redirect", "netmap"):
        target = to_addr + (f":{to_port}" if to_port else "")
        meaning = (f"把{src}访问路由器 {port_text}的流量转发到 "
                   f"{target or '路由器本机'}")
        effect = f"外部访问该端口时，实际由 {target or '本机服务'} 响应；这是端口映射/内网穿透"
        risk = "该服务将直接暴露到外网，若未限制来源，任何人都能尝试连接"
        advice = "确认目标服务已设强密码或 HTTPS；建议用 src-address-list 限制允许的国家/管理机 IP"
    elif action in ("masquerade", "src-nat"):
        meaning = (f"把{src}经 {out_if or '外网接口'} 发出的流量，源地址改写成 "
                   f"{to_addr or '路由器的公网 IP'}（内网共享上网）")
        effect = "内网设备访问互联网时，对方看到的是路由器公网地址，回包能正确送回"
        risk = ""
        advice = "这是家用/办公网络让内网能上网的常规规则，一般无需改动"
    elif action == "fasttrack-connection":
        meaning = "对匹配的连接启用 FastTrack 快速转发，降低 CPU 占用、提升吞吐"
        effect = "这些流量会走快速路径，绕过 Queue 队列限速与部分 mangle 标记"
        risk = "若你的带宽/Queue 智能调度依赖队列，FastTrack 会让限速失效"
        advice = "需要限速的流量请加 connection-state 例外，或改用 accept + 明确标记"
    elif action.startswith("add-") and "address-list" in action:
        meaning = f"把{src}触发本规则的 IP 加入地址列表「{addr_list or '未命名'}」"
        effect = "配合后续的 drop 规则可实现动态黑名单（多次失败自动封禁的思路）"
        risk = ""
        advice = "记得另配一条引用该地址列表的 drop 规则，否则加入列表不会产生拦截效果"
    elif action == "jump":
        meaning = f"把匹配的流量跳转到自定义链「{jump or '未指定'}」继续检查"
        effect = "本链后续规则不再评估，由目标链决定最终放行还是丢弃"
    elif action == "log":
        meaning = (f"把匹配的流量记入系统日志"
                   f"（前缀 {_clean(payload.get('log-prefix')) or '无'}），然后继续往下匹配")
        effect = "只记录不拦截，方便排查谁在访问、被哪条规则处理"
    elif action == "return":
        meaning = "结束当前自定义链，返回调用它的上级链继续匹配"
        effect = "不改变放行/丢弃结果，仅控制规则检查流程"
    elif action == "accept" and "established" in _clean(
            payload.get("connection-state")).lower():
        meaning = "允许已建立的连接及其关联回包通过（内网主动访问外网后的返回数据）"
        effect = "没有它，内网设备打开网页时的返回数据会被拦下；这是防火墙最常见的放行规则"
        advice = advice or "通常排在所有规则最前面，避免每个回包都要走一遍后续检查"
    elif action in ("drop", "reject") and (
            _clean(payload.get("in-interface"))
            or _clean(payload.get("in-interface-list"))):
        iface = (_clean(payload.get("in-interface"))
                 or _clean(payload.get("in-interface-list")))
        meaning = f"阻断从 {iface} 进入的{chain_cn}流量，防止外网主动连入"
        effect = "该接口上进来的新连接会被拦下；内网主动发起、已有连接不受影响"
    elif chain == "input" and action == "accept" and port:
        meaning = f"允许{src}通过 {port_text}访问路由器本机的服务"
        effect = (f"能连到路由器的设备都可以访问该服务"
                  + (f"（识别为 {service}）" if service else ""))
        if port.split(",")[0].strip() in _ADMIN_PORTS and not _source_limited(payload):
            risk = "管理端口对任意来源开放，可能遭到扫描或暴力破解"
            advice = "建议用 src-address / src-address-list 把来源限制为管理机所在网段"
    elif action in ("drop", "reject") and cond == "所有流量（无条件匹配）":
        meaning = f"在{chain_cn}中{action_cn}所有流量（兜底规则）"
        effect = "前面没有放行的流量到这里全部被拦下，是防火墙的默认保护"
        risk = ("高危：无条件丢弃全部流量，若排在放行规则之前，可能立即中断远程管理"
                if action == "drop" else "")
        advice = "确认它位于所有 allow 规则之后；远程操作前先留备份"
    else:
        meaning = f"在{chain_cn}中{action_cn}：{cond}"
        effect = f"仅对满足上述条件的{src}流量生效，其余流量继续往下匹配"

    # 通用风险（未被特化分支覆盖时）
    if not risk:
        matchers = [k for k in payload
                    if str(k).lower() not in _SKIP_KEYS and _clean(payload.get(k))]
        if action in ("drop", "reject") and not matchers:
            risk = "高危：无条件丢弃全部流量，可能立即中断远程管理"
        elif chain == "input" and action in ("drop", "reject") \
                and not _source_limited(payload):
            risk = "input 链的拒绝规则未限定来源，可能把自己锁在路由外面"
        elif action == "accept" and not matchers:
            risk = "无条件放行，会让排在它后面的拒绝规则永远匹配不到"
            advice = advice or "建议补充端口、协议或来源限制，并放在兜底 drop 之前"
    if bool(payload.get("disabled")):
        risk = (risk + "；" if risk else "") + "该规则导入后默认禁用，不会立即生效"
        advice = advice or "确认无误后到详情页手动启用"
    if not advice and risk:
        advice = "建议先用试运行预览，确认无误再批量导入；导入前会自动留备份"

    if comment:
        meaning += f"（备注：{comment}）"

    return {
        "meaning": meaning,
        "effect": effect or cond,
        "scope": f"来源：{src} ｜ 匹配：{cond}",
        "risk": risk,
        "advice": advice,
    }


@dataclass
class ImportRule:
    """一条待导入规则及其可读含义。"""

    index: int = 0
    table: str = "filter"
    chain: str = ""
    action: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    meaning: str = ""
    effect: str = ""
    scope: str = ""
    risk: str = ""
    advice: str = ""
    place_before: str = ""
    disabled: bool = False

    @property
    def command(self) -> str:
        return preview_command(self.table, self.payload, self.place_before or None)

    @property
    def table_label(self) -> str:
        return TABLE_LABELS.get(self.table, self.table)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "table": self.table,
            "table_label": self.table_label, "chain": self.chain,
            "action": self.action, "payload": dict(self.payload),
            "meaning": self.meaning, "effect": self.effect,
            "scope": self.scope, "risk": self.risk, "advice": self.advice,
            "place_before": self.place_before, "disabled": self.disabled,
            "command": self.command,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ImportRule":
        d = dict(data or {})
        return cls(
            index=int(d.get("index") or 0),
            table=str(d.get("table") or "filter"),
            chain=str(d.get("chain") or ""),
            action=str(d.get("action") or ""),
            payload=dict(d.get("payload") or {}),
            meaning=str(d.get("meaning") or ""),
            effect=str(d.get("effect") or ""),
            scope=str(d.get("scope") or ""),
            risk=str(d.get("risk") or ""),
            advice=str(d.get("advice") or ""),
            place_before=str(d.get("place_before") or ""),
            disabled=bool(d.get("disabled")),
        )


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _decode_base64(text: str) -> str:
    """尝试把 base64 文本还原为 UTF-8 内容；失败则返回原文本。"""
    try:
        raw = base64.b64decode(text, validate=True)
        decoded = raw.decode("utf-8")
        return decoded
    except Exception:  # noqa: BLE001
        return text


def extract_upload(value: Any) -> str:
    """兼容前端 VFileInput 的多种取值形态，取出文件文本内容。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", "replace")
    if isinstance(value, str):
        s = value.strip()
        if s.lower().startswith("data:") and "," in s:
            return _decode_base64(s.split(",", 1)[1])
        if len(s) > 64 and all(
            c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r "
            for c in s
        ):
            decoded = _decode_base64(s)
            if decoded != s:
                return decoded
        return s
    if isinstance(value, dict):
        for key in ("content", "text", "data", "base64", "value"):
            if value.get(key):
                got = extract_upload(value[key])
                if got:
                    return got
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            got = extract_upload(item)
            if got:
                return got
        return ""
    return ""


def collect_import_text(config: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    """按「上传文件 > 粘贴文本 > 容器内文件路径」优先级取出导入内容。"""
    errors: List[str] = []
    text = extract_upload(config.get("import_file"))
    source = "上传文件"
    if not text:
        text = str(config.get("import_rules") or "")
        source = "粘贴规则文本"
    if not text:
        path = _clean(config.get("import_file_path"))
        if path:
            try:
                p = Path(path)
                if not p.is_file():
                    errors.append(f"文件路径不存在：{path}")
                elif p.stat().st_size > 1024 * 1024:
                    errors.append(f"文件超过 1MB，已跳过：{path}")
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    source = "容器内文件"
            except Exception as err:  # noqa: BLE001
                errors.append(f"读取文件失败：{err}")
    return text, source, errors


def describe_rule(table: str, payload: Dict[str, Any]) -> str:
    """兼容旧调用：返回自然语言含义。完整解释见 explain_rule。"""
    return explain_rule(table, payload)["meaning"]


def rule_risk(table: str, payload: Dict[str, Any]) -> str:
    """兼容旧调用：返回风险提示。完整解释见 explain_rule。"""
    return explain_rule(table, payload)["risk"]


def logical_lines(text: str) -> List[str]:
    """合并 RouterOS export 中以反斜杠结尾的续行。"""
    out: List[str] = []
    buf = ""
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1].rstrip() + " "
            continue
        out.append((buf + line).strip())
        buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def parse_firewall_script(text: str, default_table: str = "filter", *,
                          disabled: bool = False,
                          max_rules: int = _MAX_IMPORT) -> Tuple[List[ImportRule], List[str]]:
    """解析防火墙规则文本，返回（待导入规则, 错误信息）。"""
    rules: List[ImportRule] = []
    errors: List[str] = []
    table = default_table if default_table in TABLES else "filter"

    for lineno, line in enumerate(logical_lines(text), 1):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        if s.startswith("/"):
            low = s.lower()
            for candidate in TABLES:
                if f"/ip firewall {candidate}" in low or low.endswith(
                    f"/ip/firewall/{candidate}"
                ):
                    table = candidate
                    break
            continue
        if s.lower().startswith(("print", "monitor", ":if", ":foreach")):
            continue
        try:
            tokens = shlex.split(s, posix=True)
        except ValueError as err:
            errors.append(f"第 {lineno} 行引号不配对，已跳过：{err}")
            continue
        if tokens and tokens[0].lower() == "add":
            tokens = tokens[1:]
        if not tokens:
            continue
        fields: Dict[str, str] = {}
        place_before = ""
        for token in tokens:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.strip().lower().lstrip("-")
            value = value.strip()
            if key in ("place-before", "place_before"):
                place_before = value
                continue
            if key:
                fields[key] = value
        if "chain" not in fields or "action" not in fields:
            errors.append(f"第 {lineno} 行缺少 chain 或 action，已跳过：{s[:70]}")
            continue
        if disabled:
            fields["disabled"] = "yes"
        ok, payload, err = build_add_payload(table, fields)
        if not ok:
            errors.append(f"第 {lineno} 行：{err}")
            continue
        if len(rules) >= max_rules:
            errors.append(f"已达到 {max_rules} 条上限，后续规则未解析")
            break
        explain = explain_rule(table, payload)
        rules.append(ImportRule(
            index=len(rules) + 1,
            table=table,
            chain=_clean(payload.get("chain")),
            action=_clean(payload.get("action")),
            payload=payload,
            meaning=explain["meaning"],
            effect=explain["effect"],
            scope=explain["scope"],
            risk=explain["risk"],
            advice=explain["advice"],
            place_before=place_before,
            disabled=bool(payload.get("disabled")),
        ))
    return rules, errors


def build_import_preview(config: Dict[str, Any], text_override: str = "",
                         source_override: str = "") -> Dict[str, Any]:
    """解析导入内容，生成可持久化的预览数据。

    text_override 供「上传文件」等 API 直接传入文本内容；不传时按
    「上传文件 > 粘贴文本 > 容器内文件路径」的优先级从配置里取。
    """
    if text_override and str(text_override).strip():
        text, source, errors = str(text_override), source_override or "上传文件", []
    else:
        text, source, errors = collect_import_text(config)
    default_table = _clean(config.get("import_table")) or "filter"
    disabled = bool(config.get("import_disabled"))
    place = _clean(config.get("import_place")) or "before_drop"
    if not text.strip():
        return {"rules": [], "errors": errors, "source": "", "place": place,
                "text_len": 0, "created": ""}
    rules, parse_errors = parse_firewall_script(text, default_table, disabled=disabled)
    errors.extend(parse_errors)
    return {
        "rules": [r.to_dict() for r in rules],
        "errors": errors,
        "source": source,
        "place": place,
        "text_len": len(text),
        "created": "",
    }


def format_import_brief(preview: Dict[str, Any], limit: int = 12) -> str:
    """详情页展示：把每条导入规则翻成中文「会做什么 / 影响 / 风险 / 建议」。

    文本在这里就地做 Markdown 安全转义；等价命令单独用 md_safe_command，
    保留 RouterOS 的 *id（单个 * 不会造成空药丸，根因是 [x][y] 参考式链接）。
    """
    rules = list(preview.get("rules") or [])
    errors = list(preview.get("errors") or [])
    lines: List[str] = []
    source = _clean(preview.get("source"))
    if source:
        lines.append(md_safe(
            f"导入来源：{source} ｜ 解析 {len(rules)} 条 ｜ 提示 {len(errors)} 条"))
    if not rules:
        lines.append("还没有导入内容：在设置页粘贴 RouterOS 规则，或点下方「上传 .rsc 文件」。")
    for row in rules[:max(1, limit)]:
        lines.append(md_safe(
            f"{row.get('index', 0)}. {row.get('table_label', row.get('table', ''))}"
            f" ｜ {row.get('chain', '')} ｜ {row.get('action', '')}"))
        lines.append(md_safe(f"   会做什么：{row.get('meaning', '')}"))
        if row.get("effect"):
            lines.append(md_safe(f"   影响：{row['effect']}"))
        if row.get("risk"):
            lines.append(md_safe(f"   注意：{row['risk']}"))
        if row.get("advice"):
            lines.append(md_safe(f"   建议：{row['advice']}"))
        lines.append("   命令：" + md_safe_command(str(row.get("command", ""))))
    if len(rules) > limit:
        lines.append(md_safe(f"另有 {len(rules) - limit} 条未显示，可调大设置页「详情页显示上限」"))
    for note in errors[:5]:
        lines.append(md_safe(f"解析提示：{note}"))
    return "\n".join(lines)


# 构成匹配条件的常见 RouterOS 参数；drop 规则不带这些就是无条件丢弃
_CONDITION_KEYS = frozenset({
    "protocol", "dst-port", "src-port", "in-interface", "out-interface",
    "in-interface-list", "out-interface-list", "src-address", "dst-address",
    "src-address-list", "dst-address-list", "connection-state", "connection-nat-state",
    "tcp-flags", "limit", "comment", "content", "layer7-protocol",
})


def summarize_import_effect(preview: Dict[str, Any], existing_count: int = 0,
                            dry_run: bool = False) -> str:
    """整体摘要：回答「这批规则写进 WinBox 后，防火墙会怎样」。

    与 format_import_brief 的逐条翻译互补，这里给出数量、动作分布、
    插入位置、整体影响与高危提示，供详情页和上传接口就地展示。
    """
    rules = list(preview.get("rules") or [])
    total = len(rules)
    existing = max(0, int(existing_count or 0))
    lines: List[str] = [
        "导入后会发生什么（WinBox / RouterOS 批量写入整体预览）",
        md_safe(f"共 {total} 条待导入；现存 {existing} 条，写入后约 {existing + total} 条。"),
    ]

    actions: Dict[str, int] = {}
    for row in rules:
        action = str(row.get("action") or "未识别")
        actions[action] = actions.get(action, 0) + 1
    lines.append("动作：" + ("、".join(f"{k} {v} 条" for k, v in actions.items()) or "无"))

    place = str(preview.get("place") or "before_drop")
    place_text = {
        "before_drop": "插到兜底 drop 之前（新规则能被匹配到）",
        "top": "插到规则链顶部",
        "bottom": "追加到规则链末尾",
    }.get(place, place)
    lines.append(f"位置：{place_text}")

    unconditional_drops = sum(
        1 for row in rules
        if str(row.get("action") or "").lower() == "drop"
        and not any(key in (row.get("payload") or {}) for key in _CONDITION_KEYS)
    )
    if unconditional_drops:
        lines.append("影响：" + md_safe(
            f"有 {unconditional_drops} 条无条件 drop，会丢弃其后所有未匹配流量；"
            "建议放在规则链末尾并先确认放行管理端口。"))
        lines.append("高危：无条件 drop 可能把自己锁在路由器外，务必先核对位置。")
    else:
        lines.append("影响：" + md_safe(
            "按预览位置写入后，命中流量的处理方式随上述动作变化；"
            "未列出的流量仍按现有规则处理。"))

    for row in rules[:3]:
        if row.get("meaning"):
            lines.append(md_safe(f"会做什么｜第 {row.get('index', 0)} 条：{row['meaning']}"))
    if dry_run:
        lines.append("试运行已开启：本次只生成预览，不会写入 RouterOS。")
    return "\n".join(lines)
