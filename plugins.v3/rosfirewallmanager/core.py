"""RouterOS 防火墙可视化管理器 —— 零宿主依赖的纯逻辑层。

所有可被单测覆盖的解析、规则规范化、影子/冲突检测、排序目标计算、
命令预览与格式化都放这里，避免与 MoviePilot 宿主耦合。

本插件定位是"管理器"而非"期望状态对齐器"：
- 读取 RouterOS 上 filter/nat/mangle/raw 四张表的全部规则并可视化；
- 检测会被前置规则遮挡的"死规则"、重复规则、缺失兜底等隐患；
- 用户按需对单条规则做 启用/禁用/上移/下移/置顶/置底/删除，或按表单新增，
  绝不批量删除用户的真实规则（安全优先）。

关键 RouterOS 语义（已源码/文档核实）：
- 规则顺序即执行顺序（first match wins），数组顺序是评估顺序；
- 新增用 PUT，默认追加到末尾，必须带 place-before 才能插到兜底 drop 之前；
- place-before=<第一条 id> 置顶、=*ffffffff(最后一条的 .nextid) 置底；
- 终止动作 accept/drop/reject/tarpit/return/jump/fasttrack-connection 匹配后停止；
  log/passthrough/add-*-to-address-list/mark-* 属非终止，会继续往下评估。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 四张可管理表 -> REST 路径片段
TABLES: Tuple[str, ...] = ("filter", "nat", "mangle", "raw")
TABLE_LABELS: Dict[str, str] = {
    "filter": "过滤 (filter)",
    "nat": "地址转换 (nat)",
    "mangle": "标记 (mangle)",
    "raw": "原始 (raw)",
}

# 终止性动作：匹配后停止评估后续规则
TERMINATING_ACTIONS: Tuple[str, ...] = (
    "accept", "drop", "reject", "tarpit", "return", "jump", "fasttrack-connection",
)
# 明确非终止的动作（用于提示，不参与"终止性"判断）
NON_TERMINATING_ACTIONS: Tuple[str, ...] = (
    "log", "passthrough", "add-src-to-address-list", "add-dst-to-address-list",
    "mark-connection", "mark-packet", "mark-routing", "mark-prerouting",
    "set-priority", "sniff",
)

# 所有"匹配条件"字段（用于判断是否 catch-all / 计算规则签名）
MATCHER_FIELDS: Tuple[str, ...] = (
    "src-address", "dst-address", "src-address-list", "dst-address-list",
    "src-mac-address", "protocol", "port", "src-port", "dst-port",
    "in-interface", "out-interface", "in-interface-list", "out-interface-list",
    "connection-state", "connection-nat-state", "icmp-options", "tcp-flags",
    "limit", "content", "layer7-protocol", "ttl", "dscp", "packet-size",
    "p2p", "hotspot", "time", "distance",
)
# "收窄型"匹配字段：去掉带 ! 前缀的 interface-list（!LAN 是宽松兜底，不算收窄）
_NARROW_EXCLUDE = {"in-interface-list", "out-interface-list"}

# 表单里可选的动作清单（详情页新增规则用）
ACTION_OPTIONS: Tuple[str, ...] = (
    "accept", "drop", "reject", "log", "passthrough", "add-src-to-address-list",
    "add-dst-to-address-list", "fasttrack-connection", "tarpit", "jump", "return",
)
PROTOCOL_OPTIONS: Tuple[str, ...] = ("tcp", "udp", "icmp", "gre", "esp", "all")
CONNSTATE_OPTIONS: Tuple[str, ...] = (
    "established,related", "new", "established", "related", "invalid", "untracked",
)

# 置底哨兵：最后一条规则的 .nextid（RouterOS 内部最大引用）
PLACE_BOTTOM = "*ffffffff"

# 隐患级别
SEV_HIGH, SEV_MED, SEV_INFO = "high", "medium", "info"


# ---------------------------------------------------------------------------
# 规则数据模型
# ---------------------------------------------------------------------------

def _bool(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in ("true", "yes", "1", "on")


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass
class Rule:
    """一条规范化后的 RouterOS 防火墙规则。"""

    id: str = ""
    table: str = "filter"
    chain: str = ""
    action: str = ""
    comment: str = ""
    disabled: bool = False
    dynamic: bool = False
    invalid: bool = False
    log: bool = False
    log_prefix: str = ""
    bytes: str = "0"
    packets: str = "0"
    matchers: Dict[str, str] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any], table: str) -> "Rule":
        raw = dict(raw or {})
        matchers: Dict[str, str] = {}
        for key in MATCHER_FIELDS:
            val = _clean(raw.get(key))
            if val:
                matchers[key] = val
        return cls(
            id=_clean(raw.get(".id") or raw.get("id")),
            table=table,
            chain=_clean(raw.get("chain")),
            action=_clean(raw.get("action")),
            comment=_clean(raw.get("comment")),
            disabled=_bool(raw.get("disabled")),
            dynamic=_bool(raw.get("dynamic")),
            invalid=_bool(raw.get("invalid")),
            log=_bool(raw.get("log")),
            log_prefix=_clean(raw.get("log-prefix")),
            bytes=_clean(raw.get("bytes")) or "0",
            packets=_clean(raw.get("packets")) or "0",
            matchers=matchers,
            raw=raw,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "table": self.table, "chain": self.chain,
            "action": self.action, "comment": self.comment, "disabled": self.disabled,
            "dynamic": self.dynamic, "invalid": self.invalid, "log": self.log,
            "log_prefix": self.log_prefix, "bytes": self.bytes, "packets": self.packets,
            "matchers": dict(self.matchers), "raw": dict(self.raw),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Rule":
        d = dict(d or {})
        r = Rule(
            id=_clean(d.get("id")), table=_clean(d.get("table")) or "filter",
            chain=_clean(d.get("chain")), action=_clean(d.get("action")),
            comment=_clean(d.get("comment")), disabled=_bool(d.get("disabled")),
            dynamic=_bool(d.get("dynamic")), invalid=_bool(d.get("invalid")),
            log=_bool(d.get("log")), log_prefix=_clean(d.get("log_prefix")),
            bytes=_clean(d.get("bytes")) or "0", packets=_clean(d.get("packets")) or "0",
            matchers={str(k): str(v) for k, v in (d.get("matchers") or {}).items()},
            raw=dict(d.get("raw") or {}),
        )
        return r

    # -- 语义判定 -----------------------------------------------------------

    def is_terminating(self) -> bool:
        return self.action in TERMINATING_ACTIONS

    def is_catchall(self) -> bool:
        """终止性且无任何匹配条件（真正的"全放行/全丢弃"）。"""
        return self.is_terminating() and not self.matchers

    def _narrowing_matchers(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for k, v in self.matchers.items():
            if k in _NARROW_EXCLUDE and v.startswith("!"):
                continue  # !LAN 等排除型 interface-list 属宽松兜底，不算收窄
            out[k] = v
        return out

    def is_broad_drop(self) -> bool:
        """宽泛拒绝：drop/reject 且无收窄条件（含 defconf 的 in-interface-list=!LAN）。

        这类规则通常作为 chain 末尾的兜底；出现在它之后的 accept 基本不可达。
        """
        return self.action in ("drop", "reject") and not self._narrowing_matchers()

    def is_broad_accept(self) -> bool:
        """宽泛放行：accept 且无收窄条件（含仅靠 !LAN 排除的 accept）。

        出现在它之后的 drop 基本不可达，安全策略形同虚设。
        """
        return self.action == "accept" and not self._narrowing_matchers()

    def signature(self) -> Tuple[Any, ...]:
        """规则签名（忽略序号/.id/备注/计数/启停），用于重复检测。"""
        return (
            self.table, self.chain, self.action,
            tuple(sorted((k, v) for k, v in self.matchers.items())),
        )

    # -- 展示 ---------------------------------------------------------------

    def matcher_summary(self, max_len: int = 72) -> str:
        if not self.matchers:
            return "任意流量"
        parts = [f"{k}={v}" for k, v in self.matchers.items()]
        text = " ".join(parts)
        return text if len(text) <= max_len else text[: max_len - 1] + "…"

    def display_line(self, index: int) -> str:
        flag = []
        if self.disabled:
            flag.append("禁用")
        if self.dynamic:
            flag.append("动态")
        if self.invalid:
            flag.append("无效")
        flag_text = "、".join(flag) if flag else "启用"
        comment = f"｜{self.comment}" if self.comment else ""
        return (f"{index:>2}. [{flag_text}] {self.chain}｜{self.action}"
                f"｜{self.matcher_summary()}{comment}")


# ---------------------------------------------------------------------------
# 隐患检测（核心）
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    table: str
    chain: str
    rule_ids: List[str] = field(default_factory=list)
    suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code, "severity": self.severity, "title": self.title,
            "detail": self.detail, "table": self.table, "chain": self.chain,
            "rule_ids": list(self.rule_ids), "suggestion": self.suggestion,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Finding":
        d = dict(d or {})
        return Finding(
            code=_clean(d.get("code")), severity=_clean(d.get("severity")) or SEV_INFO,
            title=_clean(d.get("title")), detail=_clean(d.get("detail")),
            table=_clean(d.get("table")), chain=_clean(d.get("chain")),
            rule_ids=[str(x) for x in (d.get("rule_ids") or [])],
            suggestion=_clean(d.get("suggestion")),
        )


def detect_findings(rules: Sequence[Rule]) -> List[Finding]:
    """按 chain 内的执行顺序检测死规则 / 重复 / 缺失兜底。

    检测项：
    1. shadowed_accept（高）：accept 位于更早的"宽泛 drop"之后 → 永不匹配；
    2. dead_drop（高）：drop/reject 位于更早的"宽泛 accept/catch-all"之后 → 安全失效；
    3. duplicate（中）：同表同链签名相同的规则 → 后者冗余；
    4. no_default_drop（提示）：input/forward 链存在但无兜底 drop；
    5. unreachable（中）：catch-all 终止规则之后的其它非动态规则 → 整体不可达。
    """
    findings: List[Finding] = []

    # 按 (table, chain) 分组，保持原始顺序（RouterOS 数组顺序 = 执行顺序）
    groups: Dict[Tuple[str, str], List[Rule]] = {}
    for r in rules:
        if r.dynamic:
            continue  # 动态规则不参与静态隐患判定
        groups.setdefault((r.table, r.chain), []).append(r)

    for (table, chain), seq in groups.items():
        # 1 & 2 & 5：顺序遮挡
        seen_broad_drop: Optional[Rule] = None
        seen_broad_accept: Optional[Rule] = None
        for idx, r in enumerate(seq):
            if r.disabled:
                continue  # 禁用规则不参与遮挡
            if seen_broad_drop is not None and r.action in ("accept", "log") and r.is_terminating():
                findings.append(Finding(
                    code="shadowed_accept", severity=SEV_HIGH,
                    title=f"{table}｜{chain} 放行规则被前置拒绝遮挡",
                    detail=f"规则 {r.id}（{r.comment or r.matcher_summary()}）位于兜底拒绝规则 "
                           f"{seen_broad_drop.id}（{seen_broad_drop.comment or 'drop'}）之后，永不匹配。",
                    table=table, chain=chain, rule_ids=[r.id, seen_broad_drop.id],
                    suggestion="用「上移」把该放行规则移到兜底 drop 之前，或检查是否应禁用它。",
                ))
            if seen_broad_accept is not None and r.action in ("drop", "reject"):
                findings.append(Finding(
                    code="dead_drop", severity=SEV_HIGH,
                    title=f"{table}｜{chain} 拒绝规则被前置放行架空",
                    detail=f"规则 {r.id}（{r.comment or 'drop'}）位于宽泛放行规则 "
                           f"{seen_broad_accept.id}（{seen_broad_accept.comment or 'accept all'}）之后，"
                           "匹配流量已被前面放行，安全策略失效。",
                    table=table, chain=chain, rule_ids=[r.id, seen_broad_accept.id],
                    suggestion="确认全放行规则是否过宽；把拒绝规则上移到它之前，或给放行规则增加精确匹配条件。",
                ))
            if r.is_broad_drop() and seen_broad_drop is None:
                seen_broad_drop = r
            if r.is_broad_accept() and seen_broad_accept is None:
                seen_broad_accept = r

            # 5：catch-all 终止规则之后的任何非动态规则都不可达（去重：仅在该 catch-all 的下一条报一次）
            if idx + 1 < len(seq) and r.is_catchall():
                nxt = seq[idx + 1]
                if not nxt.disabled:
                    findings.append(Finding(
                        code="unreachable", severity=SEV_MED,
                        title=f"{table}｜{chain} 存在全匹配规则，后续规则不可达",
                        detail=f"规则 {r.id}（action={r.action}，无匹配条件）会匹配该链的全部剩余流量，"
                               f"其后的 {len(seq) - idx - 1} 条规则（自 {nxt.id} 起）不会被执行。",
                        table=table, chain=chain,
                        rule_ids=[r.id] + [x.id for x in seq[idx + 1:]][:10],
                        suggestion="若这是兜底 drop 且位于末尾则属正常；否则把需要生效的规则移到它之前。",
                    ))

        # 3：重复规则
        by_sig: Dict[Tuple[Any, ...], Rule] = {}
        for r in seq:
            sig = r.signature()
            if sig in by_sig:
                first = by_sig[sig]
                findings.append(Finding(
                    code="duplicate", severity=SEV_MED,
                    title=f"{table}｜{chain} 疑似重复规则",
                    detail=f"规则 {r.id}（{r.comment or r.matcher_summary()}）与 {first.id}"
                           " 的动作和匹配条件完全相同，后者不会被匹配。",
                    table=table, chain=chain, rule_ids=[r.id, first.id],
                    suggestion="核对是否误重复添加；确认后可删除其中一条。",
                ))
            else:
                by_sig[sig] = r

        # 4：缺少兜底 drop（仅 filter 的 input/forward 有强安全含义）
        if table == "filter" and chain in ("input", "forward"):
            if not any(r.is_broad_drop() and not r.disabled for r in seq):
                findings.append(Finding(
                    code="no_default_drop", severity=SEV_INFO,
                    title=f"{table}｜{chain} 缺少兜底拒绝规则",
                    detail=f"{chain} 链没有发现兜底 drop（宽泛拒绝），未显式放行的流量可能按默认策略通过。",
                    table=table, chain=chain, rule_ids=[],
                    suggestion="如尚未配置，通常在链末尾添加 action=drop 的兜底规则，并先放行必要管理端口。",
                ))

    # 严重级别排序：高 -> 中 -> 提示；同级按表/链
    order = {SEV_HIGH: 0, SEV_MED: 1, SEV_INFO: 2}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.table, f.chain, f.code))
    return findings


# ---------------------------------------------------------------------------
# 排序 / 插入位置计算（纯函数，客户端据此发 PATCH/PUT）
# ---------------------------------------------------------------------------

def compute_move_place(ids: Sequence[str], rule_id: str, mode: str) -> Tuple[bool, Optional[str], str]:
    """计算把 rule_id 移动到指定位置时应使用的 place-before 目标。

    :param ids: 该表内的规则 id 顺序列表（含全部规则，顺序即执行顺序）。
    :param rule_id: 待移动规则 id。
    :param mode: up / down / top / bottom。
    :return: (是否需要移动, place-before 值, 说明)。place-before 语义为"插到该 id 之前"。
    """
    seq = [str(x) for x in ids]
    if rule_id not in seq:
        return False, None, "规则不在当前列表中"
    idx = seq.index(rule_id)
    last = len(seq) - 1

    if mode == "top":
        if idx == 0:
            return False, None, "已在顶部"
        return True, seq[0], f"置顶（插到 {seq[0]} 之前）"
    if mode == "bottom":
        if idx == last:
            return False, None, "已在底部"
        return True, PLACE_BOTTOM, "置底（末条之后）"
    if mode == "up":
        if idx == 0:
            return False, None, "已在顶部"
        return True, seq[idx - 1], f"上移一位（插到 {seq[idx - 1]} 之前）"
    if mode == "down":
        if idx == last:
            return False, None, "已在底部"
        # 插到下下条之前 == 与下一条交换；若当前是倒数第二条则置底
        target = seq[idx + 2] if idx + 2 < len(seq) else PLACE_BOTTOM
        return True, target, "下移一位"
    return False, None, f"未知移动方式 {mode}"


def choose_insert_place(rules: Sequence[Rule], chain: str, mode: str,
                        before_id: str = "") -> Tuple[Optional[str], str]:
    """为"新增规则"选择 place-before 目标，避免落进兜底 drop 之后。

    :param mode: before_drop（默认，插到该链首个宽泛 drop 前）/ top / bottom / before_id。
    """
    seq = [r for r in rules if r.chain == chain and not r.dynamic]
    ids = [r.id for r in seq]
    if mode == "before_id":
        target = _clean(before_id)
        if target and target in ids:
            return target, f"插到指定规则 {target} 之前"
        return None, "指定的前置规则不存在，改为追加到末尾"
    if mode == "top":
        if ids:
            return ids[0], f"置顶（插到 {ids[0]} 之前）"
        return None, "空链，直接添加"
    if mode == "bottom":
        return None, "追加到末尾"
    # before_drop（默认）
    for r in seq:
        if r.is_broad_drop() and not r.disabled:
            return r.id, f"插到兜底拒绝规则 {r.id} 之前"
    return None, "未发现兜底 drop，追加到末尾（请确认不会成为死规则）"


# ---------------------------------------------------------------------------
# 新增规则 payload 构造 / 校验
# ---------------------------------------------------------------------------

def build_add_payload(table: str, fields: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], str]:
    """把表单字段构造为 RouterOS PUT payload。

    必填：chain、action。空值字段自动剔除；disabled 归一化为布尔。
    """
    table = _clean(table).lower()
    if table not in TABLES:
        return False, {}, f"未知表 {table}"
    chain = _clean(fields.get("chain"))
    action = _clean(fields.get("action")).lower()
    if not chain:
        return False, {}, "缺少链 chain"
    if not action:
        return False, {}, "缺少动作 action"

    payload: Dict[str, Any] = {"chain": chain, "action": action}
    passthrough_keys = (
        "src-address", "dst-address", "src-address-list", "dst-address-list",
        "src-mac-address", "protocol", "port", "src-port", "dst-port",
        "in-interface", "out-interface", "in-interface-list", "out-interface-list",
        "connection-state", "connection-nat-state", "icmp-options", "tcp-flags",
        "limit", "content", "layer7-protocol", "ttl", "dscp", "packet-size",
        "jump-target", "address-list", "address-list-timeout", "to-addresses", "to-ports",
    )
    for key in passthrough_keys:
        val = _clean(fields.get(key) or fields.get(key.replace("-", "_")))
        if val:
            payload[key] = val
    comment = _clean(fields.get("comment"))
    if comment:
        payload["comment"] = comment
    payload["disabled"] = _bool(fields.get("disabled"))
    if _bool(fields.get("log")):
        payload["log"] = True
        lp = _clean(fields.get("log_prefix") or fields.get("log-prefix"))
        if lp:
            payload["log-prefix"] = lp
    return True, payload, ""


def preview_command(table: str, payload: Dict[str, Any], place_before: Optional[str]) -> str:
    """生成人类可读的等价 RouterOS 命令（用于"命令预览"，不执行）。"""
    parts = [f"/ip firewall {table} add"]
    for key, val in payload.items():
        if key == "disabled":
            parts.append(f"disabled={'yes' if val else 'no'}")
            continue
        parts.append(f'{key}="{val}"')
    if place_before:
        parts.append(f"place-before={place_before}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 文本安全与格式化（杜绝详情页"空药丸"）
# ---------------------------------------------------------------------------

def md_safe(text: Any) -> str:
    """把会被前端当 Markdown 解析的半角符号转全角，避免空药丸/错乱。"""
    s = str(text if text is not None else "")
    table = str.maketrans({
        "[": "［", "]": "］",
        "`": "ˋ", "*": "＊",
        "<": "‹", ">": "›",
        "_": "＿",
    })
    return s.translate(table)


def format_log_line(time_text: str, level: str, message: str) -> str:
    """日志行用全角 ｜ 分隔，杜绝 [时间][级别] 被当参考式链接渲染成空药丸。"""
    return md_safe(f"{time_text} ｜ {level:<5} ｜ {message}")


def md_safe_command(text: Any) -> str:
    """命令预览专用安全转义：保留 RouterOS 的 ``*id``（如 place-before=*5）。

    空药丸根因是 ``[x][y]`` 被当 Markdown 参考式链接；单个 ``*`` 不会触发该问题，
    而命令里的 ``*id`` 必须原样准确显示才能核对/复制，故这里不转义 ``*``。
    其余会触发行内代码/加粗/HTML/链接的符号仍转全角。
    """
    s = str(text if text is not None else "")
    table = str.maketrans({
        "[": "［", "]": "］",
        "`": "ˋ", "*": "*",  # 保留 *（RouterOS 内部 id 前缀）
        "<": "‹", ">": "›",
        "_": "＿",
    })
    return s.translate(table)


def format_rules_text(rules: Sequence[Rule], table: str, max_rows: int = 200) -> str:
    """把一个表的规则渲染为有序文本（自身已 md_safe）。"""
    seq = [r for r in rules if r.table == table]
    if not seq:
        return f"{TABLE_LABELS.get(table, table)}：暂无规则"
    lines = [f"{TABLE_LABELS.get(table, table)}｜共 {len(seq)} 条（自上而下即执行顺序）："]
    for idx, r in enumerate(seq[:max_rows], 1):
        lines.append("  " + md_safe(r.display_line(idx)))
    if len(seq) > max_rows:
        lines.append(f"  ……共 {len(seq)} 条，仅显示前 {max_rows} 条")
    return "\n".join(lines)


def format_findings_text(findings: Sequence[Finding], max_rows: int = 30) -> str:
    if not findings:
        return "未发现明显隐患（静态规则顺序、重复与兜底检查均通过）"
    icon = {SEV_HIGH: "⚠", SEV_MED: "!", SEV_INFO: "i"}
    lines = [f"共发现 {len(findings)} 项（⚠高危 / !中危 / i提示）："]
    for f in findings[:max_rows]:
        lines.append(f"  {icon.get(f.severity, '·')} {md_safe(f.title)}")
        lines.append(f"      {md_safe(f.detail)}")
        if f.suggestion:
            lines.append(f"      建议：{md_safe(f.suggestion)}")
    if len(findings) > max_rows:
        lines.append(f"  ……共 {len(findings)} 项，仅显示前 {max_rows} 项")
    return "\n".join(lines)


def format_preview_text(payload: Dict[str, Any], table: str,
                        place_before: Optional[str], note: str = "") -> str:
    lines = [f"目标表：{TABLE_LABELS.get(table, table)}",
             "等价命令（仅供核对，实际由插件通过 REST 下发）：",
             "  " + md_safe_command(preview_command(table, payload, place_before))]
    if note:
        lines.append("位置说明：" + md_safe(note))
    lines.append("确认无误后点击「确认新增」写入 RouterOS。")
    return "\n".join(lines)


def summarize_rules(rules: Sequence[Rule]) -> Dict[str, int]:
    """按表统计总数 / 启用 / 禁用 / 动态。"""
    out: Dict[str, int] = {}
    for table in TABLES:
        seq = [r for r in rules if r.table == table]
        out[f"{table}_total"] = len(seq)
        out[f"{table}_disabled"] = sum(1 for r in seq if r.disabled)
        out[f"{table}_dynamic"] = sum(1 for r in seq if r.dynamic)
    out["total"] = len(rules)
    out["disabled"] = sum(1 for r in rules if r.disabled)
    out["dynamic"] = sum(1 for r in rules if r.dynamic)
    return out


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def validate_cron(cron: str) -> Tuple[bool, str]:
    import re
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
    import re
    result = str(text)
    for secret in secrets or []:
        if secret and len(secret) >= 4:
            result = result.replace(secret, secret[:2] + "***" + secret[-1:])
    result = re.sub(r"(https?://)[^@/\s]+:[^@/\s]+@", r"\1***:***@", result)
    result = re.sub(r"(api_key=|apikey=|token=|password=)([^\s&,]{4,})", r"\1***",
                    result, flags=re.IGNORECASE)
    return result


def now_text() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def ts_text(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    import time
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return str(ts)
