"""RouterOS 外网访问向导 —— 内网服务发现（零宿主依赖纯逻辑层）。

把 MoviePilot 已连接的媒体服务器、下载器 WebUI 以及 MP 本身，统一解析成
「名称 + 内网 IP + 内网端口」的服务清单，供 planner 生成 dst-nat 映射。

宿主 SDK 只在 load_connected_configs() 里延迟导入；其余函数都是纯函数，
可在没有 MoviePilot 的环境里直接单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple
from urllib.parse import urlparse

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SLUG = re.compile(r"[^a-z0-9]+")

DEFAULT_PORTS: Dict[str, int] = {
    "emby": 8096, "jellyfin": 8096, "plex": 32400,
    "qbittorrent": 8080, "transmission": 9091,
    "moviepilot": 3000,
}


def clean_value(value: Any, limit: int = 120) -> str:
    """去掉控制字符并限长；所有用户文本进 payload / 备注前都要过一遍。"""
    text = _CONTROL.sub("", str(value if value is not None else "")).strip()
    return text[:limit]


def cfg_attr(cfg: Any, *names: str) -> Any:
    """兼容 dict / 对象两种配置载体。"""
    for name in names:
        value = cfg.get(name) if isinstance(cfg, dict) else getattr(cfg, name, None)
        if value not in (None, ""):
            return value
    return None


def parse_service_url(url: Any, default_port: int = 80) -> Tuple[str, int, str]:
    """解析服务地址，返回 (主机, 端口, scheme)。

    支持 http(s)://host:port/path、裸 host:port、裸 host。
    主机保持原样（可能是域名），是否为合法 IPv4 由 planner 校验。
    """
    text = clean_value(url, 300).rstrip("/")
    if not text:
        return "", 0, "http"
    scheme = "https" if text.lower().startswith("https://") else "http"
    parsed = urlparse(text if "://" in text else f"{scheme}://{text}")
    if parsed.scheme in ("http", "https"):
        scheme = parsed.scheme
    host = str(parsed.hostname or "")
    try:
        port = int(parsed.port or 0)
    except (TypeError, ValueError):
        port = 0
    if not port:
        port = 443 if scheme == "https" else int(default_port or 80)
    return host, port, scheme


def make_slug(name: str, lan_port: int) -> str:
    """生成稳定、URL/RouterOS 备注安全的 id：重排服务顺序不会改变标签。"""
    base = _SLUG.sub("-", clean_value(name, 60).lower()).strip("-")[:24] or "svc"
    return f"{base}-{int(lan_port or 0)}"


@dataclass
class Service:
    """一个待映射的内网服务。"""

    id: str = ""
    name: str = ""
    kind: str = "manual"          # media / downloader / mp / manual
    lan_ip: str = ""
    lan_port: int = 0
    wan_port: int = 0             # 0 = 由 planner 自动分配
    scheme: str = "http"
    note: str = ""
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "lan_ip": self.lan_ip, "lan_port": self.lan_port,
            "wan_port": self.wan_port, "scheme": self.scheme,
            "note": self.note, "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Service":
        d = dict(data or {})
        try:
            lan_port = int(d.get("lan_port") or 0)
        except (TypeError, ValueError):
            lan_port = 0
        try:
            wan_port = int(d.get("wan_port") or 0)
        except (TypeError, ValueError):
            wan_port = 0
        return cls(
            id=str(d.get("id") or ""), name=str(d.get("name") or ""),
            kind=str(d.get("kind") or "manual"), lan_ip=str(d.get("lan_ip") or ""),
            lan_port=lan_port, wan_port=wan_port,
            scheme=str(d.get("scheme") or "http"),
            note=str(d.get("note") or ""), source=str(d.get("source") or ""),
        )


def _new_service(name: str, kind: str, url: Any, default_port: int,
                 note: str = "", source: str = "", wan_port: int = 0) -> Service:
    host, port, scheme = parse_service_url(url, default_port)
    clean_name = clean_value(name, 60) or "未命名服务"
    return Service(
        id=make_slug(clean_name, port), name=clean_name, kind=kind,
        lan_ip=host, lan_port=port, wan_port=wan_port,
        scheme=scheme, note=clean_value(note), source=clean_value(source, 60),
    )


def parse_manual_services(text: Any) -> Tuple[List[Service], List[str]]:
    """解析手动补充框：每行 名称|内网地址|内网端口|外网端口|备注。

    内网地址可以写 http(s)://IP:端口，此时端口列可留空；外网端口留空表示自动分配。
    """
    services: List[Service] = []
    errors: List[str] = []
    for lineno, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 2:
            errors.append(f"手动服务第 {lineno} 行至少需要「名称|内网地址」两列")
            continue
        name, address = cols[0], cols[1]
        port_text = cols[2] if len(cols) > 2 else ""
        wan_text = cols[3] if len(cols) > 3 else ""
        note = cols[4] if len(cols) > 4 else ""
        try:
            wan_port = int(wan_text) if wan_text else 0
        except ValueError:
            errors.append(f"手动服务「{name}」的外网端口不是数字")
            continue
        host, port, scheme = parse_service_url(address, 0)
        if port_text:
            try:
                port = int(port_text)
            except ValueError:
                errors.append(f"手动服务「{name}」的内网端口不是数字")
                continue
        if not name:
            errors.append(f"手动服务第 {lineno} 行缺少名称")
            continue
        if not host or not port:
            errors.append(f"手动服务「{name}」的地址或端口不完整")
            continue
        services.append(Service(
            id=make_slug(name, port), name=clean_value(name, 60), kind="manual",
            lan_ip=host, lan_port=port, wan_port=wan_port,
            scheme=scheme, note=clean_value(note), source="手动补充",
        ))
    return services, errors


def load_connected_configs() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """读取 MoviePilot 已连接的媒体服务器 / 下载器配置；宿主不可用时返回空字典。"""
    media: Dict[str, Any] = {}
    downloaders: Dict[str, Any] = {}
    try:
        from app.sdk.services import MediaServerHelper  # type: ignore
        got = MediaServerHelper().get_configs() or {}
        if isinstance(got, dict):
            media = got
    except Exception:  # noqa: BLE001 - 离线 / 宿主未安装该 helper
        pass
    try:
        from app.sdk.services import DownloaderHelper  # type: ignore
        got = DownloaderHelper().get_configs() or {}
        if isinstance(got, dict):
            downloaders = got
    except Exception:  # noqa: BLE001
        pass
    return media, downloaders


def service_items(media_configs: Dict[str, Any], downloader_configs: Dict[str, Any],
                  mp_enabled: bool = True) -> List[Dict[str, str]]:
    """配置页「已连接服务」多选下拉项。值带前缀，避免同名实例冲突。"""
    items: List[Dict[str, str]] = []
    if mp_enabled:
        items.append({"title": "MoviePilot 本体", "value": "mp"})
    for name, cfg in (media_configs or {}).items():
        stype = str(cfg_attr(cfg, "type", "server_type") or "").lower()
        url = cfg_attr(cfg, "url", "address", "host", "base_url")
        host, port, _ = parse_service_url(url, DEFAULT_PORTS.get(stype, 8096))
        items.append({"title": f"{name} · 媒体服务器 {host}:{port}",
                      "value": f"media:{name}"})
    for name, cfg in (downloader_configs or {}).items():
        stype = str(cfg_attr(cfg, "type", "downloader", "module") or "").lower()
        url = cfg_attr(cfg, "url", "host", "address", "base_url")
        host, port, _ = parse_service_url(url, DEFAULT_PORTS.get(stype, 8080))
        items.append({"title": f"{name} · 下载器 {host}:{port}",
                      "value": f"downloader:{name}"})
    return items


def discover_services(selected: Any, manual_text: Any,
                      media_configs: Dict[str, Any] = None,
                      downloader_configs: Dict[str, Any] = None,
                      mp_host: str = "", mp_port: int = 3000,
                      mp_scheme: str = "http") -> Tuple[List[Service], List[str]]:
    """把下拉勾选 + 手动补充合成服务清单；同名/同 id 去重，手动行优先。"""
    media_configs = media_configs or {}
    downloader_configs = downloader_configs or {}
    raw = selected if isinstance(selected, (list, tuple, set)) else (
        [selected] if isinstance(selected, str) and selected.strip() else [])
    found: List[Service] = []
    errors: List[str] = []

    for value in [str(x).strip() for x in raw if str(x).strip()]:
        if value == "mp":
            host = clean_value(mp_host, 120)
            if not host:
                errors.append("勾选了 MoviePilot 本体，但没填它的内网地址")
                continue
            found.append(_new_service("MoviePilot", "mp",
                                      f"{mp_scheme}://{host}:{int(mp_port or 3000)}",
                                      3000, source="MoviePilot 本体"))
            continue
        kind, _, name = value.partition(":")
        configs = media_configs if kind == "media" else (
            downloader_configs if kind == "downloader" else {})
        cfg = configs.get(name)
        if cfg is None:
            errors.append(f"已连接服务「{name}」现在找不到了，请重新勾选或改到手动补充")
            continue
        stype = str(cfg_attr(cfg, "type", "server_type", "downloader", "module") or "").lower()
        url = cfg_attr(cfg, "url", "address", "host", "base_url")
        if not url:
            errors.append(f"已连接服务「{name}」没暴露地址，请在手动补充里按格式填写")
            continue
        found.append(_new_service(name, kind, url, DEFAULT_PORTS.get(stype, 80),
                                  source=f"已连接 {kind}"))

    manual, manual_errors = parse_manual_services(manual_text)
    errors.extend(manual_errors)

    # 手动行按 id 覆盖同名自动发现项（用户可用它改正 IP / 指定外网端口）
    by_id: Dict[str, Service] = {}
    order: List[str] = []
    for svc in found + manual:
        if svc.id in by_id:
            by_id[svc.id] = svc
        else:
            by_id[svc.id] = svc
            order.append(svc.id)
    return [by_id[key] for key in order], errors


def services_from_plan(plan: Any) -> List[Service]:
    return [Service.from_dict(d) for d in (dict(plan or {}).get("services") or [])]
