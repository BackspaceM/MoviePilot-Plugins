"""RouterOS 统一工作台 —— 只读分区清单与页签映射（纯数据，零宿主依赖）。

这里只登记会用 GET 去读的 /rest 分区。cli 一栏留作对照用，不执行任何命令；
orderable 表示 RouterOS 里这个分区的条目顺序有意义，工作台只按顺序展示。
"""
from __future__ import annotations

from typing import Any, Dict, List

SEC_RESOURCE = "/rest/system/resource"
SEC_IDENTITY = "/rest/system/identity"
SEC_INTERFACE = "/rest/interface"
SEC_ADDRESS = "/rest/ip/address"
SEC_FILTER = "/rest/ip/firewall/filter"
SEC_NAT = "/rest/ip/firewall/nat"
SEC_MANGLE = "/rest/ip/firewall/mangle"
SEC_ADDRESS_LIST = "/rest/ip/firewall/address-list"
SEC_QUEUE = "/rest/queue/simple"
SEC_DNS = "/rest/ip/dns/static"
SEC_ROUTE = "/rest/ip/route"
SEC_FILE = "/rest/file"

SECTIONS: Dict[str, Dict[str, Any]] = {
    SEC_RESOURCE: {"label": "系统资源", "cli": "/system/resource", "orderable": False},
    SEC_IDENTITY: {"label": "设备名称", "cli": "/system/identity", "orderable": False},
    SEC_INTERFACE: {"label": "接口", "cli": "/interface", "orderable": False},
    SEC_ADDRESS: {"label": "IP 地址", "cli": "/ip/address", "orderable": False},
    SEC_FILTER: {"label": "防火墙｜filter", "cli": "/ip/firewall/filter", "orderable": True},
    SEC_NAT: {"label": "防火墙｜nat", "cli": "/ip/firewall/nat", "orderable": True},
    SEC_MANGLE: {"label": "防火墙｜mangle", "cli": "/ip/firewall/mangle", "orderable": True},
    SEC_ADDRESS_LIST: {"label": "防火墙｜地址列表", "cli": "/ip/firewall/address-list",
                        "orderable": True},
    SEC_QUEUE: {"label": "简单队列", "cli": "/queue/simple", "orderable": True},
    SEC_DNS: {"label": "静态 DNS", "cli": "/ip/dns/static", "orderable": True},
    SEC_ROUTE: {"label": "路由", "cli": "/ip/route", "orderable": True},
    SEC_FILE: {"label": "文件（备份）", "cli": "/file", "orderable": False},
}

# 五个页签各读哪些分区。地址列表在防火墙和分流里都会露面，读一次就够了。
TAB_SECTIONS: Dict[str, List[str]] = {
    "overview": [SEC_RESOURCE, SEC_IDENTITY, SEC_INTERFACE, SEC_ADDRESS],
    "firewall": [SEC_FILTER, SEC_NAT, SEC_MANGLE, SEC_ADDRESS_LIST],
    "queue": [SEC_QUEUE],
    "backup": [SEC_FILE],
    "routing": [SEC_DNS, SEC_ADDRESS_LIST, SEC_ROUTE],
}

TABS = [("overview", "概览"), ("firewall", "防火墙"), ("queue", "Queue"),
        ("backup", "备份"), ("routing", "分流")]

TAB_KEYS = [key for key, _ in TABS]
TAB_LABELS = {key: label for key, label in TABS}


def tab_label(key: Any) -> str:
    return TAB_LABELS.get(str(key or ""), str(key or ""))


def label_of(section: str) -> str:
    return str(SECTIONS.get(section, {}).get("label") or section)
