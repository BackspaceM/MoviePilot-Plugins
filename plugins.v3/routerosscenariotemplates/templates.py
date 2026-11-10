"""RouterOS 场景化配置模板 —— 声明式模板定义与渲染（零宿主依赖纯逻辑层）。

把「批量导入」从防火墙规则泛化到所有 RouterOS 配置分区：
一个模板 = 一组带分区、payload、中文含义、风险等级与插入位置的部署动作。
所有动作统一带 ``MP-A2[<模板>:<序号>]`` 备注标签，用于幂等跳过与精确回滚。

本模块只用标准库，可在没有 MoviePilot 宿主的环境中直接单测。
"""
from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

DEFAULT_PREFIX = "MP-A2"

# REST 分区 -> 展示元信息。orderable=True 表示支持 place-before（防火墙规则链）
SECTIONS: Dict[str, Dict[str, Any]] = {
    "/rest/ip/firewall/address-list": {"label": "IPv4 防火墙｜地址列表", "cli": "/ip/firewall/address-list", "orderable": False},
    "/rest/ip/firewall/filter": {"label": "IPv4 防火墙｜filter", "cli": "/ip/firewall/filter", "orderable": True},
    "/rest/ip/firewall/nat": {"label": "IPv4 防火墙｜nat", "cli": "/ip/firewall/nat", "orderable": True},
    "/rest/ip/firewall/mangle": {"label": "IPv4 防火墙｜mangle", "cli": "/ip/firewall/mangle", "orderable": True},
    "/rest/queue/simple": {"label": "队列｜simple queue", "cli": "/queue/simple", "orderable": False},
    "/rest/ip/address": {"label": "IP｜地址", "cli": "/ip/address", "orderable": False},
    "/rest/ip/dns/static": {"label": "DNS｜静态解析", "cli": "/ip/dns/static", "orderable": False},
    "/rest/ipv6/dhcp-client": {"label": "IPv6｜DHCPv6-PD 客户端", "cli": "/ipv6/dhcp-client", "orderable": False},
    "/rest/ipv6/address": {"label": "IPv6｜地址", "cli": "/ipv6/address", "orderable": False},
    "/rest/ipv6/firewall/filter": {"label": "IPv6 防火墙｜filter", "cli": "/ipv6/firewall/filter", "orderable": True},
    "/rest/interface/wireguard": {"label": "接口｜WireGuard", "cli": "/interface/wireguard", "orderable": False},
    "/rest/interface/wireguard/peers": {"label": "接口｜WireGuard Peer", "cli": "/interface/wireguard/peers", "orderable": False},
}

RISK_LABELS: Dict[str, str] = {"low": "低", "medium": "中", "high": "高"}
RISK_COLORS: Dict[str, str] = {"low": "success", "medium": "warning", "high": "error"}

# 插入位置策略
PLACE_APPEND = "append"          # 追加到分区末尾（非规则分区）
PLACE_BEFORE_DROP = "before_drop"  # 插到同链首个兜底 drop 之前
PLACE_TOP = "top"                # 插到同链第一条规则之前
PLACE_LABELS: Dict[str, str] = {
    PLACE_APPEND: "追加到分区末尾",
    PLACE_BEFORE_DROP: "插到兜底 drop 之前",
    PLACE_TOP: "插到链首",
}

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_IFACE = re.compile(r"^[A-Za-z0-9_.:/\-]{1,64}$")
_BANDWIDTH = re.compile(r"^\d+(?:\.\d+)?\s*[KMGTkmgt]?$")
_WGKEY = re.compile(r"^[A-Za-z0-9+/=]{40,80}$")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

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
    state: str = "pending"      # pending / exists / done / failed / skipped / rolled_back
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


@dataclass
class TemplateParam:
    """模板可填参数。kind 决定输入校验规则。"""

    key: str
    label: str
    default: str = ""
    hint: str = ""
    required: bool = False
    kind: str = "text"       # text / ip / cidr / port / interface / bandwidth / key / number
    placeholder: str = ""


@dataclass
class StepSpec:
    """模板中的一步：向某个 REST 分区 PUT 一条配置。"""

    section: str
    payload: Dict[str, Any]
    meaning: str
    risk: str = ""
    risk_level: str = "low"
    placement: str = PLACE_APPEND
    optional_param: str = ""   # 该参数为空时跳过本步（可选步骤）


@dataclass
class Template:
    """一套场景模板。"""

    id: str
    name: str
    icon: str
    summary: str
    risk_level: str
    params: Tuple[TemplateParam, ...]
    steps: Tuple[StepSpec, ...]
    notes: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 5 套内置模板
# ---------------------------------------------------------------------------

TEMPLATES: Tuple[Template, ...] = (
    Template(
        id="pt_qos", name="PT 友好 QoS", icon="mdi-speedometer",
        summary="把 PT 下载机登记到地址列表，用简单队列限制其上下行，并给上行流量打标记；避免做种占满上行把远程观影挤卡。",
        risk_level="medium",
        params=(
            TemplateParam("pt_hosts", "PT 主机 IP / 网段", "192.168.88.50",
                          "填下载机地址，可填 CIDR；多个地址用逗号分隔", True, "cidr", "192.168.88.50"),
            TemplateParam("pt_up_limit", "PT 上行上限", "30M",
                          "留足余量给远程观影，单位 K/M/G", True, "bandwidth", "30M"),
            TemplateParam("pt_down_limit", "PT 下行上限", "100M",
                          "下行一般可放宽，单位 K/M/G", True, "bandwidth", "100M"),
        ),
        steps=(
            StepSpec("/rest/ip/firewall/address-list",
                     {"address": "{pt_hosts}", "list": "MP-A2-PT-HOSTS"},
                     "把 {pt_hosts} 登记为 PT 主机地址列表，后续队列与标记只作用于它",
                     "若这里填的是整个内网网段，队列会限制网段内所有设备，请确认范围。", "medium"),
            StepSpec("/rest/queue/simple",
                     {"name": "MP-A2-PT-QOS", "target": "{pt_hosts}",
                      "max-limit": "{pt_up_limit}/{pt_down_limit}", "priority": "8/8"},
                     "为 PT 主机建立简单队列：上行上限 {pt_up_limit}、下行上限 {pt_down_limit}，"
                     "做种不再吃满上行带宽",
                     "中：会与「带宽/Queue 智能调度」插件管理的队列并存，两套限速可能叠加；"
                     "如已启用统一调度，建议不要同时部署本模板。", "medium"),
            StepSpec("/rest/ip/firewall/mangle",
                     {"chain": "forward", "src-address-list": "MP-A2-PT-HOSTS",
                      "action": "mark-packet", "new-packet-mark": "MP_A2_PT_UP",
                      "passthrough": "yes"},
                     "给 PT 主机的转发流量打上 MP_A2_PT_UP 包标记，便于后续按标记做优先级或统计",
                     "仅打标记不改变流量走向；插到链首，避免被已有 passthrough=no 的规则吃掉。", "low",
                     PLACE_TOP),
        ),
        notes=(
            "本模板只新增带 MP-A2 标签的条目，不会修改或删除任何手工队列/规则。",
            "simple queue 的 max-limit 格式为「上行/下行」，RouterOS 里上行在前。",
            "如需「播放时自动让路」的动态调度，请用播放感知调度 / 带宽调度插件，本模板是静态基线。",
        ),
    ),
    Template(
        id="remote_stream", name="远程观影放行", icon="mdi-play-network",
        summary="把媒体服务器通过 DNAT 映射到公网端口，并配套 forward 放行与可选的 NAT 回环，实现在外看片。",
        risk_level="high",
        params=(
            TemplateParam("media_ip", "媒体服务器内网 IP", "",
                          "Emby / Jellyfin / Plex 的内网地址", True, "ip", "192.168.88.10"),
            TemplateParam("media_port", "媒体服务内网端口", "8096",
                          "如 Jellyfin 8096、Emby 8096、Plex 32400", True, "port", "8096"),
            TemplateParam("wan_port", "对外端口", "18096",
                          "建议换一个非默认端口，避免被全网扫描", True, "port", "18096"),
            TemplateParam("wan_if", "WAN 接口名", "ether1",
                          "RouterOS 里 /interface 看到的名称；若用 PPPoE 填 pppoe-out1", True, "interface", "ether1"),
            TemplateParam("lan_subnet", "内网网段（NAT 回环，可留空）", "192.168.88.0/24",
                          "填了才会生成 NAT 回环规则；不需要内网用公网域名访问时留空", False, "cidr", "192.168.88.0/24"),
        ),
        steps=(
            StepSpec("/rest/ip/firewall/nat",
                     {"chain": "dstnat", "in-interface": "{wan_if}", "protocol": "tcp",
                      "dst-port": "{wan_port}", "action": "dst-nat",
                      "to-addresses": "{media_ip}", "to-ports": "{media_port}"},
                     "公网访问 WAN 口的 {wan_port} 时，转发到内网 {media_ip}:{media_port}（媒体服务器）",
                     "高危：把媒体服务直接暴露到公网。请先确认已启用强密码 / HTTPS / 反向代理，"
                     "并尽量限制来源 IP；默认账号或弱口令等同于把整个媒体库送人。", "high"),
            StepSpec("/rest/ip/firewall/filter",
                     {"chain": "forward", "in-interface": "{wan_if}", "protocol": "tcp",
                      "dst-address": "{media_ip}", "dst-port": "{media_port}", "action": "accept"},
                     "在 forward 链放行 WAN → {media_ip}:{media_port} 的转发流量，否则上一条 DNAT 会被防火墙拦下",
                     "已限定单个内网 IP + 单个端口，并插到兜底 drop 之前；请勿改成不限来源。", "medium",
                     PLACE_BEFORE_DROP),
            StepSpec("/rest/ip/firewall/nat",
                     {"chain": "srcnat", "src-address": "{lan_subnet}",
                      "dst-address": "{media_ip}", "protocol": "tcp",
                      "dst-port": "{media_port}", "action": "masquerade"},
                     "内网设备用公网域名访问媒体服务时做源地址伪装（NAT 回环 / Hairpin NAT）",
                     "只影响内网访问该媒体服务的流量；若你用内网 DNS 直接解析到内网 IP，可以留空跳过本步。",
                     "low", PLACE_APPEND, "lan_subnet"),
        ),
        notes=(
            "只有具备公网 IPv4 的环境才能从外网访问；CGNAT / 内网穿透场景请改用 WireGuard 回家模板。",
            "DNAT 与 forward 放行是两条独立规则，缺一不可。",
            "本模板不申请证书：HTTPS 可配合 CertManager 插件或反向代理。",
        ),
    ),
    Template(
        id="guest_isolation", name="访客网络隔离", icon="mdi-account-network",
        summary="禁止访客网段访问内网与路由器管理面，仅保留 DNS 查询；依赖规则顺序，逐条插到兜底 drop 之前。",
        risk_level="medium",
        params=(
            TemplateParam("guest_subnet", "访客网段", "192.168.30.0/24",
                          "访客 Wi-Fi / 访客桥接的网段", True, "cidr", "192.168.30.0/24"),
            TemplateParam("lan_subnet", "受保护的内网网段", "192.168.88.0/24",
                          "NAS、媒体服务器、管理终端所在的网段", True, "cidr", "192.168.88.0/24"),
        ),
        steps=(
            StepSpec("/rest/ip/firewall/address-list",
                     {"address": "{lan_subnet}", "list": "MP-A2-LAN-NETS"},
                     "把 {lan_subnet} 登记为受保护的内网网段，供后续规则引用",
                     "仅新增地址列表条目，不改变现有防火墙规则。", "low"),
            StepSpec("/rest/ip/firewall/filter",
                     {"chain": "forward", "src-address": "{guest_subnet}",
                      "dst-address": "{lan_subnet}", "action": "drop"},
                     "禁止访客网段 {guest_subnet} 访问内网 {lan_subnet}（隔离核心规则，必须先于放行规则）",
                     "中：若访客网段与内网网段重叠，会立刻断掉正常设备，请仔细核对。", "medium",
                     PLACE_BEFORE_DROP),
            StepSpec("/rest/ip/firewall/filter",
                     {"chain": "input", "src-address": "{guest_subnet}",
                      "protocol": "udp", "dst-port": "53", "action": "accept"},
                     "允许访客向路由器发起 DNS 查询，否则访客无法解析域名",
                     "只放行 UDP 53 且来源限定为访客网段；如访客 DNS 指向外部服务器，本步可不用。", "low",
                     PLACE_BEFORE_DROP),
            StepSpec("/rest/ip/firewall/filter",
                     {"chain": "input", "src-address": "{guest_subnet}", "action": "drop"},
                     "禁止访客访问路由器自身的管理服务（WinBox / SSH / WebFig / REST API）",
                     "高：确认你的管理终端不在访客网段，否则可能把自己锁在路由外面；"
                     "部署前请保证有一条来自 LAN 的管理放行规则。", "high",
                     PLACE_BEFORE_DROP),
        ),
        notes=(
            "规则顺序即执行顺序：『禁止访客→内网』在最前，『允许 DNS』在『禁止访客→路由器』之前。",
            "本模板只做隔离，不新增访客上网放行；如访客无法上网，请检查现有 forward 规则。",
            "若你的链里存在更靠前的「全放行」规则，请到防火墙可视化管理器里把本模板规则上移。",
        ),
    ),
    Template(
        id="ipv6_basic", name="IPv6 基础开通", icon="mdi-ip-network",
        summary="WAN 口申请 DHCPv6-PD 前缀、LAN 口下发地址并开启 RA，同时补齐 IPv6 防火墙兜底，避免设备裸奔公网。",
        risk_level="high",
        params=(
            TemplateParam("wan_if", "WAN 接口名", "ether1",
                          "PPPoE 拨号请填 pppoe-out1", True, "interface", "ether1"),
            TemplateParam("lan_if", "LAN 桥接接口名", "bridge",
                          "内网主桥接，通常是 bridge", True, "interface", "bridge"),
            TemplateParam("v6_prefix_len", "申请的前缀长度", "56",
                          "运营商常见 /56 或 /60；填数字即可", True, "number", "56"),
        ),
        steps=(
            StepSpec("/rest/ipv6/dhcp-client",
                     {"interface": "{wan_if}", "request": "prefix",
                      "pool-name": "MP-A2-V6POOL", "pool-prefix-length": "{v6_prefix_len}",
                      "add-default-route": "yes"},
                     "在 {wan_if} 启用 DHCPv6-PD 客户端，向运营商申请 IPv6 前缀存入地址池 MP-A2-V6POOL",
                     "高：需要运营商/光猫支持 DHCPv6-PD；不支持时该条目会一直停在 requesting，"
                     "属于正常现象，可直接回滚本模板。", "high"),
            StepSpec("/rest/ipv6/address",
                     {"address": "::1/64", "from-pool": "MP-A2-V6POOL",
                      "interface": "{lan_if}", "advertise": "yes"},
                     "从地址池取一个 /64 配到 {lan_if} 并开启 RA 通告，内网设备自动获得公网 IPv6 地址",
                     "高：开启后内网设备将拥有公网可达地址，务必让下面 4 条 IPv6 防火墙规则一起生效。", "high"),
            StepSpec("/rest/ipv6/firewall/filter",
                     {"chain": "input", "protocol": "icmpv6", "action": "accept"},
                     "放行 ICMPv6（邻居发现、PMTU 发现必需），全部丢弃会直接导致 IPv6 不通",
                     "IPv6 不像 IPv4 可以关 ICMP，必须放行。", "low", PLACE_BEFORE_DROP),
            StepSpec("/rest/ipv6/firewall/filter",
                     {"chain": "forward", "in-interface": "{wan_if}",
                      "connection-state": "established,related", "action": "accept"},
                     "允许内网主动发起的 IPv6 连接回包进入，保证正常上网",
                     "只放行已建立连接，不放行公网主动发起的新连接。", "low", PLACE_BEFORE_DROP),
            StepSpec("/rest/ipv6/firewall/filter",
                     {"chain": "forward", "in-interface": "{wan_if}", "action": "drop"},
                     "丢弃从公网主动发起的 IPv6 转发连接，避免内网设备直接暴露在公网",
                     "中：会阻断外部主动发起的 IPv6 入站；若需要 IPv6 公网访问服务，请另行精确放行。",
                     "medium", PLACE_BEFORE_DROP),
            StepSpec("/rest/ipv6/firewall/filter",
                     {"chain": "input", "in-interface": "{wan_if}", "action": "drop"},
                     "丢弃从公网主动发往路由器自身的 IPv6 连接，保护管理面",
                     "中：ICMPv6 放行规则排在它之前，不影响 IPv6 基本连通性。", "medium",
                     PLACE_BEFORE_DROP),
        ),
        notes=(
            "部署顺序经过设计：ICMPv6 放行在前、input 兜底 drop 在后；请勿手工调整这几条的相对顺序。",
            "RouterOS 默认 IPv6 防火墙通常是空的，开通 IPv6 却不配防火墙等于把全部内网设备暴露到公网。",
            "如果已有自己的 IPv6 防火墙方案，请只借用地址池与 RA 步骤，部署后用防火墙管理器核对顺序。",
        ),
    ),
    Template(
        id="wireguard_home", name="WireGuard 回家", icon="mdi-vpn",
        summary="创建 WireGuard 接口与隧道网段，放行 UDP 握手端口，并允许 VPN 客户端访问内网；可选登记首个 Peer。",
        risk_level="high",
        params=(
            TemplateParam("wg_port", "WireGuard 监听端口", "13231",
                          "UDP 端口，建议避开常用端口", True, "port", "13231"),
            TemplateParam("wg_ip", "隧道网关地址", "10.10.10.1/24",
                          "WireGuard 接口在路由器上的地址", True, "cidr", "10.10.10.1/24"),
            TemplateParam("wg_net", "隧道网段", "10.10.10.0/24",
                          "登记到受信任地址列表，用于放行回家流量", True, "cidr", "10.10.10.0/24"),
            TemplateParam("wan_if", "WAN 接口名", "ether1",
                          "PPPoE 拨号请填 pppoe-out1", True, "interface", "ether1"),
            TemplateParam("peer_public_key", "首个客户端公钥（可留空）", "",
                          "客户端生成的公钥；留空则只建接口，不生成 Peer", False, "key", "粘贴客户端公钥"),
            TemplateParam("peer_ip", "首个客户端隧道地址", "10.10.10.2/32",
                          "Peer 的 allowed-address", False, "cidr", "10.10.10.2/32"),
        ),
        steps=(
            StepSpec("/rest/interface/wireguard",
                     {"name": "MP-A2-WG", "listen-port": "{wg_port}", "mtu": "1420"},
                     "创建 WireGuard 接口 MP-A2-WG 并监听 UDP {wg_port}（私钥由 RouterOS 自动生成）",
                     "创建后需到 RouterOS 读取接口公钥并填到客户端配置；部署报告会给出查看命令。", "medium"),
            StepSpec("/rest/ip/address",
                     {"address": "{wg_ip}", "interface": "MP-A2-WG"},
                     "给 WireGuard 隧道分配 {wg_ip}，作为回家网络的网关地址",
                     "请确保该网段与现有 LAN / 访客网段不冲突。", "medium"),
            StepSpec("/rest/ip/firewall/filter",
                     {"chain": "input", "in-interface": "{wan_if}", "protocol": "udp",
                      "dst-port": "{wg_port}", "action": "accept"},
                     "放行 WAN 口的 WireGuard 握手流量（UDP {wg_port}）",
                     "高：向公网开放一个 UDP 端口。WireGuard 对未授权包静默丢弃，"
                     "但仍请妥善保管客户端私钥并定期更换。", "high", PLACE_BEFORE_DROP),
            StepSpec("/rest/ip/firewall/address-list",
                     {"address": "{wg_net}", "list": "MP-A2-VPN-NETS"},
                     "把隧道网段 {wg_net} 登记为受信任的 VPN 地址列表",
                     "仅新增地址列表，不改动现有防火墙规则。", "low"),
            StepSpec("/rest/ip/firewall/filter",
                     {"chain": "forward", "src-address-list": "MP-A2-VPN-NETS",
                      "action": "accept"},
                     "允许 VPN 客户端访问内网资源（回家看片 / 管理设备）",
                     "中：放行整个隧道网段的转发流量；如需要细分权限，请在本规则之后追加限制。",
                     "medium", PLACE_BEFORE_DROP),
            StepSpec("/rest/interface/wireguard/peers",
                     {"interface": "MP-A2-WG", "public-key": "{peer_public_key}",
                      "allowed-address": "{peer_ip}", "persistent-keepalive": "25s"},
                     "登记首个客户端 Peer：公钥 {peer_public_key}，允许地址 {peer_ip}",
                     "公钥必须来自客户端生成的密钥对，填错只会握手失败，不会影响现有连接。",
                     "low", PLACE_APPEND, "peer_public_key"),
        ),
        notes=(
            "部署后在 RouterOS 执行 /interface/wireguard/print 查看服务端公钥，填到客户端。",
            "客户端配置里的 Endpoint 需要公网 IP 或 DDNS；CGNAT 环境无法从外部直连。",
            "只登记了首个 Peer；后续客户端请手动在 RouterOS 添加，或再次修改公钥后重新部署。",
        ),
    ),
)

TEMPLATE_MAP: Dict[str, Template] = {t.id: t for t in TEMPLATES}


# ---------------------------------------------------------------------------
# 参数清洗与校验
# ---------------------------------------------------------------------------

def clean_value(value: Any) -> str:
    """去掉控制字符与首尾空白，限制长度；用户输入只进 JSON payload 与文本预览。"""
    text = _CONTROL.sub("", str(value if value is not None else "")).strip()
    return text[:128]


def clean_values(values: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in dict(values or {}).items():
        out[str(key)] = clean_value(value)
    return out


def _validate_one(param: TemplateParam, value: str) -> str:
    """校验单个参数，返回错误说明（空串表示通过）。"""
    if not value:
        return f"「{param.label}」不能为空" if param.required else ""
    kind = param.kind
    if kind == "ip":
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return f"「{param.label}」不是合法 IP 地址：{value}"
    elif kind == "cidr":
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                if "/" in part:
                    ipaddress.ip_network(part, strict=False)
                else:
                    ipaddress.ip_address(part)
            except ValueError:
                return f"「{param.label}」不是合法 IP / CIDR：{part}"
    elif kind == "port":
        try:
            port = int(value)
        except ValueError:
            return f"「{param.label}」必须是数字：{value}"
        if not 1 <= port <= 65535:
            return f"「{param.label}」端口超出 1-65535：{port}"
    elif kind == "number":
        try:
            num = int(value)
        except ValueError:
            return f"「{param.label}」必须是数字：{value}"
        if not 1 <= num <= 128:
            return f"「{param.label}」超出合理范围：{num}"
    elif kind == "interface":
        if not _IFACE.match(value):
            return f"「{param.label}」接口名含非法字符：{value}"
    elif kind == "bandwidth":
        if not _BANDWIDTH.match(value):
            return f"「{param.label}」格式不对（示例 30M / 500K）：{value}"
    elif kind == "key":
        if not _WGKEY.match(value):
            return f"「{param.label}」不像 WireGuard 公钥（应为 44 位左右 base64）"
    return ""


def validate_template(template: Template, values: Dict[str, str]) -> List[str]:
    """校验一套模板的全部参数，返回错误列表。"""
    errors: List[str] = []
    for param in template.params:
        err = _validate_one(param, str(values.get(param.key) or ""))
        if err:
            errors.append(f"[{template.name}] {err}")
    return errors


def all_param_keys() -> List[str]:
    """所有模板参数的 key（按模板顺序去重），供主类生成配置字段。"""
    seen: Dict[str, bool] = {}
    for tpl in TEMPLATES:
        for p in tpl.params:
            seen.setdefault(p.key, True)
    return list(seen)


def template_select_items() -> List[Dict[str, str]]:
    """配置页多选下拉项。"""
    return [{"title": f"{t.name}（风险{RISK_LABELS.get(t.risk_level, '?')}）", "value": t.id}
            for t in TEMPLATES]


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _fmt(text: Any, values: Dict[str, str]) -> str:
    """替换 {param} 占位；未知占位替换为空串，绝不抛 KeyError。"""
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1)) or ""), str(text or ""))


def format_command(section: str, payload: Dict[str, Any], place_before: str = "") -> str:
    """生成等价的 RouterOS CLI 命令（仅用于预览核对，不执行）。"""
    cli = str(SECTIONS.get(section, {}).get("cli") or section.replace("/rest", "", 1))
    parts = [f"{cli} add"]
    for key, value in dict(payload or {}).items():
        if key in ("place-before", ".id", "id"):
            continue
        if isinstance(value, bool):
            text = "yes" if value else "no"
        else:
            text = str(value)
        if not text:
            continue
        if re.search(r"\s", text) or any(c in text for c in "\"'{}[]"):
            text = '"' + text.replace('"', "'") + '"'
        parts.append(f"{key}={text}")
    if place_before:
        parts.append(f"place-before={place_before}")
    return " ".join(parts)


def _render_step(tpl: Template, spec: StepSpec, values: Dict[str, str],
                 tag: str, index: int) -> DeployAction:
    payload: Dict[str, Any] = {}
    for key, value in spec.payload.items():
        rendered = _fmt(value, values) if isinstance(value, str) else value
        payload[str(key)] = rendered
    payload["comment"] = f"{tag} {tpl.name}"
    chain = str(payload.get("chain") or "")
    placement = spec.placement
    if placement != PLACE_APPEND and not bool(SECTIONS.get(spec.section, {}).get("orderable")):
        placement = PLACE_APPEND
    return DeployAction(
        index=index, template_id=tpl.id, template_name=tpl.name, tag=tag,
        section=spec.section, payload=payload, meaning=_fmt(spec.meaning, values),
        risk=_fmt(spec.risk, values), risk_level=spec.risk_level,
        placement=placement, chain=chain,
    )


def render_plan(selected: Any, values: Any, prefix: str = DEFAULT_PREFIX,
                allow_high_risk: bool = False) -> Dict[str, Any]:
    """把用户勾选的模板渲染成一份「待确认部署计划」。

    返回 dict：actions / errors / warnings / sections / high_risk / blocked / created。
    任一模板校验不通过时，只跳过该模板并阻断整份计划（避免半套配置下发）。
    """
    ids = [str(x) for x in (selected or [])] if not isinstance(selected, str) else [
        x.strip() for x in selected.replace("，", ",").split(",") if x.strip()]
    vals = clean_values(values)
    clean_prefix = (clean_value(prefix) or DEFAULT_PREFIX).replace("[", "").replace("]", "")[:24]

    errors: List[str] = []
    warnings: List[str] = []
    actions: List[DeployAction] = []
    chosen: List[str] = []
    sections: List[str] = []

    for tid in ids:
        tpl = TEMPLATE_MAP.get(tid)
        if tpl is None:
            warnings.append(f"未知模板「{tid}」已忽略")
            continue
        chosen.append(tid)
        tpl_errors = validate_template(tpl, vals)
        if tpl.risk_level == "high" and not allow_high_risk:
            tpl_errors.append(f"[{tpl.name}] 属于高危模板，需先在设置页勾选「允许部署高危模板」")
        if tpl_errors:
            errors.extend(tpl_errors)
            continue
        step_no = 0
        for spec in tpl.steps:
            if spec.optional_param and not vals.get(spec.optional_param):
                continue
            step_no += 1
            tag = f"{clean_prefix}[{tpl.id}:{step_no}]"
            action = _render_step(tpl, spec, vals, tag, len(actions) + 1)
            actions.append(action)
            if action.section not in sections:
                sections.append(action.section)

    if not chosen:
        errors.append("尚未选择任何场景模板；请到设置页勾选模板并保存")
    elif not errors and not actions:
        errors.append("所选模板没有生成任何配置动作，请检查参数")

    high_risk = any(a.risk_level == "high" for a in actions)
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    return {
        "created": created,
        "prefix": clean_prefix,
        "templates": chosen,
        "actions": [a.to_dict() for a in actions],
        "errors": errors,
        "warnings": warnings,
        "sections": sections,
        "high_risk": high_risk,
        "blocked": bool(errors),
        "summary": {
            "templates": len(chosen), "actions": len(actions),
            "sections": len(sections), "high": sum(1 for a in actions if a.risk_level == "high"),
            "medium": sum(1 for a in actions if a.risk_level == "medium"),
            "created": created,
        },
    }


def actions_from_plan(plan: Any) -> List[DeployAction]:
    return [DeployAction.from_dict(d) for d in (dict(plan or {}).get("actions") or [])]


# ---------------------------------------------------------------------------
# 展示安全（详情页动态文本统一走这里）
# ---------------------------------------------------------------------------

def md_safe(text: Any, keep_star: bool = False) -> str:
    """半角符号转全角，避免 [xxx] / *xxx* 被前端当 Markdown 渲染成空药丸。"""
    table = str.maketrans({
        "[": "［", "]": "］", "`": "ˋ", "<": "‹", ">": "›", "_": "＿",
        "*": "*" if keep_star else "＊",
    })
    return str(text if text is not None else "").translate(table)


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
