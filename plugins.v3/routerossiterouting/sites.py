"""RouterOS 站点智能分流 / DNS —— 站点数据层（零宿主依赖纯逻辑层）。

只处理站点清单：读 MoviePilot 站点管理配置、解析手填行、合成下拉项、反序列化计划。
宿主 SDK / 数据库只在 ``load_site_configs()`` 里延迟导入，任何异常都吞掉，
模块导入期绝不联网、绝不抛错；其余函数只用 dict / str，可离线单测。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple
from urllib.parse import urlparse

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SLUG = re.compile(r"[^a-z0-9]+")
_SPLIT_TRACKERS = re.compile(r"[,;\s]+")


def clean_value(value: Any, limit: int = 120) -> str:
    """去掉控制字符并限长；所有用户文本进 payload / 备注前都要过一遍。"""
    text = _CONTROL.sub("", str(value if value is not None else "")).strip()
    return text[:limit]


def extract_domain(value: Any) -> str:
    """从 url / 域名里取出主机名：剥掉 http(s)://、端口、路径、查询串，并转小写。"""
    text = clean_value(value, 300).strip()
    if not text:
        return ""
    candidate = text if "://" in text else "http://" + text
    host = ""
    try:
        host = str(urlparse(candidate).hostname or "")
    except Exception:  # noqa: BLE001 - 非法 url 走兜底切分
        host = ""
    if not host:
        host = re.sub(r"^https?://", "", text, flags=re.IGNORECASE).split("/")[0].split(":")[0]
    return clean_value(host, 253).strip(".").lower()


def normalize_trackers(values: Any) -> List[str]:
    """逗号 / 分号 / 空格分隔，去重，剥离 scheme / 端口 / 路径。"""
    if isinstance(values, str) or values is None:
        raw_items: List[str] = _SPLIT_TRACKERS.split(str(values or ""))
    elif isinstance(values, dict):
        raw_items = [str(v) for v in values.values()]
    elif isinstance(values, (list, tuple, set)):
        raw_items = []
        for item in values:
            raw_items.extend(_SPLIT_TRACKERS.split(str(item or "")))
    else:
        raw_items = _SPLIT_TRACKERS.split(str(values or ""))
    out: List[str] = []
    seen = set()
    for item in raw_items:
        host = extract_domain(item)
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


def make_site_id(name: Any) -> str:
    """生成稳定 id：中文名也能区分，重排 / 改名前的同名行可互相覆盖。"""
    text = clean_value(name, 60)
    slug = _SLUG.sub("-", text.lower()).strip("-")[:24] or "site"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    return f"{slug}-{digest}"


@dataclass
class Site:
    """一个 PT 站点：主域名 + 若干 Tracker 域名。"""

    id: str = ""
    name: str = ""
    domain: str = ""
    tracker_domains: List[str] = field(default_factory=list)
    enabled: bool = True
    source: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "domain": self.domain,
            "tracker_domains": list(self.tracker_domains or []),
            "enabled": self.enabled, "source": self.source, "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Site":
        d = dict(data or {})
        return cls(
            id=str(d.get("id") or ""),
            name=clean_value(d.get("name"), 60),
            domain=extract_domain(d.get("domain")),
            tracker_domains=normalize_trackers(d.get("tracker_domains") or []),
            enabled=bool(d.get("enabled", True)),
            source=clean_value(d.get("source"), 60),
            note=clean_value(d.get("note")),
        )


def parse_manual_sites(text: Any) -> Tuple[List[Site], List[str]]:
    """解析手动补充框：每行 ``名称|主域名|Tracker域名(多个用逗号分隔)|备注``。"""
    sites: List[Site] = []
    errors: List[str] = []
    for lineno, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 2:
            errors.append(f"手动站点第 {lineno} 行至少要写「名称|主域名」")
            continue
        name, domain_text = cols[0], cols[1]
        trackers_text = cols[2] if len(cols) > 2 else ""
        note = cols[3] if len(cols) > 3 else ""
        if not name:
            errors.append(f"手动站点第 {lineno} 行缺名称")
            continue
        domain = extract_domain(domain_text)
        if not domain:
            errors.append(f"手动站点「{name}」的主域名没写对")
            continue
        sites.append(Site(
            id=make_site_id(name), name=clean_value(name, 60), domain=domain,
            tracker_domains=normalize_trackers(trackers_text), enabled=True,
            source="手动补充", note=clean_value(note),
        ))
    return sites, errors


# ---------------------------------------------------------------------------
# 宿主站点数据读取（全部延迟导入，异常一律吞掉）
# ---------------------------------------------------------------------------

def _as_dict(row: Any) -> Dict[str, Any]:
    """兼容 dict 与对象两种站点记录载体。"""
    if isinstance(row, dict):
        return dict(row)
    data: Dict[str, Any] = {}
    for key in ("name", "site_name", "id", "domain", "url", "rss", "tracker",
                "ext_domains", "api_domain", "enabled", "note"):
        try:
            value = getattr(row, key, None)
        except Exception:  # noqa: BLE001
            value = None
        if value not in (None, ""):
            data[key] = value
    return data


def _rows_to_configs(rows: Any) -> Dict[str, Dict[str, Any]]:
    """把 list / dict / 生成器形态的站点记录归一成 ``{name: cfg}``。"""
    items: List[Dict[str, Any]] = []
    if isinstance(rows, dict):
        for key, value in rows.items():
            if isinstance(value, dict):
                cfg = dict(value)
                cfg.setdefault("name", key)
                items.append(cfg)
            else:
                items.append({"name": key, "domain": str(value or "")})
    elif isinstance(rows, (list, tuple, set)):
        items = [_as_dict(row) for row in rows]
    configs: Dict[str, Dict[str, Any]] = {}
    for cfg in items:
        name = clean_value(cfg.get("name") or cfg.get("site_name") or
                           cfg.get("id") or cfg.get("domain"), 80)
        if name:
            configs[name] = cfg
    return configs


def _helper_configs(helper_cls: Any) -> Dict[str, Dict[str, Any]]:
    """兼容「类需实例化」与「模块级函数」两种 SitesHelper 形态。"""
    try:
        helper = helper_cls()
    except Exception:  # noqa: BLE001
        helper = helper_cls
    getter = getattr(helper, "get_indexers", None)
    if not callable(getter):
        getter = getattr(helper_cls, "get_indexers", None)
    if not callable(getter):
        return {}
    return _rows_to_configs(getter())


def _load_v3_sites() -> Dict[str, Dict[str, Any]]:
    from app.application.site.sites import SitesHelper  # type: ignore
    return _helper_configs(SitesHelper)


def _load_site_oper() -> Dict[str, Dict[str, Any]]:
    from app.db.oper import SiteOper  # type: ignore
    oper = SiteOper()
    for name in ("list", "get_all", "all", "list_sites", "get_sites"):
        getter = getattr(oper, name, None)
        if not callable(getter):
            continue
        try:
            configs = _rows_to_configs(getter())
        except Exception:  # noqa: BLE001
            continue
        if configs:
            return configs
    return {}


def load_site_configs() -> Dict[str, Dict[str, Any]]:
    """读站点索引资源和用户站点记录，归一成 ``{name: cfg}``。

    先看 app.application.site.sites.SitesHelper 的索引配置，再用
    app.db.oper.SiteOper 补上用户站点里的域名、RSS 与扩展域名。
    全失败返回空字典，绝不联网、绝不向调用方抛错。
    """
    configs: Dict[str, Dict[str, Any]] = {}
    for loader in (_load_v3_sites, _load_site_oper):
        try:
            got = loader()
        except Exception:  # noqa: BLE001 - 宿主没装 / 接口变了都当没有
            got = {}
        if isinstance(got, dict):
            for name, cfg in got.items():
                if isinstance(cfg, dict):
                    configs[clean_value(name, 80)] = cfg
    return configs


def site_items(configs: Dict[str, Any]) -> List[Dict[str, str]]:
    """配置页「已配置站点」多选下拉项，值带 ``site:`` 前缀。"""
    items: List[Dict[str, str]] = []
    for name, cfg in (configs or {}).items():
        row = cfg if isinstance(cfg, dict) else {}
        domain = extract_domain(row.get("domain") or row.get("api_domain") or row.get("url"))
        title = f"{name} · {domain}" if domain else str(name)
        items.append({"title": clean_value(title, 120), "value": f"site:{name}"})
    return items


def _site_from_config(name: str, cfg: Any) -> Tuple[Site, str]:
    """把一条站点配置转成 Site，返回 (站点, 错误说明)。"""
    row = cfg if isinstance(cfg, dict) else _as_dict(cfg)
    domain = extract_domain(row.get("domain") or row.get("url") or
                            row.get("api_domain") or "")
    if not domain:
        return Site(), f"站点「{name}」没读到域名，写到手动补充里"
    raw_trackers: List[str] = []
    for key in ("ext_domains", "tracker", "rss", "api_domain", "url", "domain"):
        value = row.get(key)
        if isinstance(value, (list, tuple, set)):
            raw_trackers.extend(str(x) for x in value)
        elif value not in (None, ""):
            raw_trackers.append(str(value))
    trackers = [t for t in normalize_trackers(raw_trackers) if t != domain]
    return Site(
        id=make_site_id(name), name=clean_value(name, 60), domain=domain,
        tracker_domains=trackers, enabled=bool(row.get("enabled", True)),
        source="MP 站点管理", note=clean_value(row.get("note") or ""),
    ), ""


def discover_sites(selected: Any, manual_text: Any,
                   configs: Dict[str, Any] = None) -> Tuple[List[Site], List[str]]:
    """下拉勾选 + 手动补充合并，同 id 手动行优先覆盖；返回 (站点, 错误说明)。"""
    configs = configs or {}
    if isinstance(selected, str):
        raw = [x.strip() for x in selected.replace("，", ",").split(",") if x.strip()]
    elif isinstance(selected, (list, tuple, set)):
        raw = [str(x).strip() for x in selected if str(x).strip()]
    else:
        raw = []
    found: List[Site] = []
    errors: List[str] = []
    for value in raw:
        name = value[5:] if value.startswith("site:") else value
        cfg = configs.get(name)
        if cfg is None:
            errors.append(f"站点「{name}」现在找不到了，重新勾选或写到手动补充里")
            continue
        site, err = _site_from_config(name, cfg)
        if err:
            errors.append(err)
        else:
            found.append(site)

    manual, manual_errors = parse_manual_sites(manual_text)
    errors.extend(manual_errors)

    by_id: Dict[str, Site] = {}
    order: List[str] = []
    for site in found + manual:
        if site.id not in by_id:
            order.append(site.id)
        by_id[site.id] = site  # 手动行排在后面，同名覆盖下拉项
    return [by_id[key] for key in order], errors


def sites_from_plan(plan: Any) -> List[Site]:
    """从计划里反序列化站点清单（供 render_plan 接受 dict 形态调用）。"""
    return [Site.from_dict(d) for d in (dict(plan or {}).get("sites") or [])]
