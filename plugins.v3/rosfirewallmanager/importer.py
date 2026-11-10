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

from .core import TABLES, TABLE_LABELS, build_add_payload, preview_command

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


@dataclass
class ImportRule:
    """一条待导入规则及其可读含义。"""

    index: int = 0
    table: str = "filter"
    chain: str = ""
    action: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    meaning: str = ""
    risk: str = ""
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
            "index": self.index, "table": self.table, "chain": self.chain,
            "action": self.action, "payload": dict(self.payload),
            "meaning": self.meaning, "risk": self.risk,
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
            risk=str(d.get("risk") or ""),
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
    """把一条规则翻译成人类可读的中文含义。"""
    chain = _clean(payload.get("chain")) or "?"
    action = _clean(payload.get("action")).lower()
    action_cn = _ACTION_LABELS.get(action, action or "?")
    conditions: List[str] = []
    for key, value in payload.items():
        key_l = str(key).lower()
        if key_l in _SKIP_KEYS:
            continue
        label = _FIELD_LABELS.get(key_l, key_l)
        conditions.append(f"{label}={_clean(value)}")
    cond_text = "、".join(conditions) if conditions else "所有流量（无条件匹配）"
    text = f"在【{chain}】链{action_cn}：{cond_text}"
    comment = _clean(payload.get("comment"))
    if comment:
        text += f"；备注「{comment}」"
    if bool(payload.get("disabled")):
        text += "；导入后默认禁用"
    if bool(payload.get("log")):
        text += "；匹配时写日志"
    return text


def rule_risk(table: str, payload: Dict[str, Any]) -> str:
    """识别可能把自己锁在路由外面的高风险规则。"""
    matchers = [k for k in payload
                if str(k).lower() not in _SKIP_KEYS and _clean(payload.get(k))]
    action = _clean(payload.get("action")).lower()
    chain = _clean(payload.get("chain")).lower()
    if action in ("drop", "reject") and not matchers:
        return "高危：无条件丢弃全部流量，可能立即中断远程管理"
    if chain == "input" and action in ("drop", "reject") and not any(
        str(k).lower() in ("src-address", "src-address-list", "in-interface",
                           "in-interface-list", "src-address-type")
        for k in matchers
    ):
        return "注意：input 链的拒绝规则未限定来源，可能把自己锁在路由外面"
    if action == "accept" and not matchers:
        return "提示：无条件放行，建议补充端口、协议或来源限制"
    return ""


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
        rules.append(ImportRule(
            index=len(rules) + 1,
            table=table,
            chain=_clean(payload.get("chain")),
            action=_clean(payload.get("action")),
            payload=payload,
            meaning=describe_rule(table, payload),
            risk=rule_risk(table, payload),
            place_before=place_before,
            disabled=bool(payload.get("disabled")),
        ))
    return rules, errors


def build_import_preview(config: Dict[str, Any]) -> Dict[str, Any]:
    """解析配置中的导入内容，生成可持久化的预览数据。"""
    text, source, errors = collect_import_text(config)
    default_table = _clean(config.get("import_table")) or "filter"
    disabled = bool(config.get("import_disabled"))
    place = _clean(config.get("import_place")) or "append"
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
