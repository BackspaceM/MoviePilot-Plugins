"""RouterOS 外网访问向导 —— 连通性测试。

两级检查：
1. 内网：部署前确认 LAN 目标真的在监听，避免把映射指向一个死地址；
2. 外网：部署后用公网地址 / 域名 + 外网端口回连，确认 DNAT 与放行规则生效。

网络层通过注入的 http 客户端发起，纯逻辑函数与网络调用分开，便于离线单测。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .planner import actions_from_plan


def build_probe_targets(plan: Any, include_wan: bool = True) -> List[Dict[str, Any]]:
    """从计划生成待测 URL 列表（纯函数）。"""
    services = list(dict(plan or {}).get("services") or [])
    targets: List[Dict[str, Any]] = []
    for svc in services:
        scheme = str(svc.get("scheme") or "http")
        lan_ip = str(svc.get("lan_ip") or "")
        lan_port = int(svc.get("lan_port") or 0)
        wan_port = int(svc.get("wan_port") or 0)
        name = str(svc.get("name") or "未命名服务")
        if lan_ip and lan_port:
            targets.append({
                "service": name, "kind": "lan",
                "url": f"{scheme}://{lan_ip}:{lan_port}/",
                "label": f"{name} 内网 {lan_ip}:{lan_port}",
            })
        if not include_wan or not wan_port:
            continue
        host = str(svc.get("public_host") or svc.get("domain") or "")
        if not host:
            continue
        targets.append({
            "service": name, "kind": "wan",
            "url": f"{scheme}://{host}:{wan_port}/",
            "label": f"{name} 外网 {host}:{wan_port}",
        })
    return targets


def probe_notes(plan: Any) -> List[str]:
    """给详情页看的说明，解释哪些情况测不通不一定是配置错。"""
    services = list(dict(plan or {}).get("services") or [])
    notes: List[str] = []
    has_wan = any(s.get("public_host") or s.get("domain") for s in services)
    if not has_wan:
        notes.append("没填公网地址或域名，只测内网；想验证外网回连，在设置页补上域名")
    else:
        notes.append("从 MP 容器访问自己的公网地址需要 Hairpin NAT，"
                     "没开的话外网这一项会失败，用手机流量再试一次更准")
    if not actions_from_plan(plan):
        notes.append("还没有可部署的计划，连通性测试仍会按当前服务清单探测内网")
    return notes


async def run_probes(http: Any, targets: Sequence[Dict[str, Any]],
                     timeout: int = 5) -> List[Dict[str, Any]]:
    """逐个 GET；只要拿到 HTTP 响应就算端口通（401/404 也说明服务活着）。"""
    results: List[Dict[str, Any]] = []
    for target in targets or []:
        url = str(target.get("url") or "")
        label = str(target.get("label") or url)
        if not url or http is None:
            results.append({"service": target.get("service", ""),
                            "kind": target.get("kind", ""), "url": url,
                            "label": label, "ok": False,
                            "message": "网络模块不可用或地址为空"})
            continue
        try:
            resp = await http.request("GET", url, timeout=timeout)
            status = int(getattr(resp, "status_code", 0) or 0)
            if status and status < 500:
                ok, msg = True, f"通（HTTP {status}）"
            elif status:
                ok, msg = False, f"端口通但服务报错（HTTP {status}）"
            else:
                ok, msg = False, "没有拿到 HTTP 状态码"
        except Exception as err:  # noqa: BLE001
            ok, msg = False, f"连不上：{type(err).__name__} {err}"
        results.append({
            "service": target.get("service", ""), "kind": target.get("kind", ""),
            "url": url, "label": label, "ok": ok, "message": msg,
        })
    return results


def summarize(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    items = list(results or [])
    ok_count = sum(1 for r in items if r.get("ok"))
    lan = [r for r in items if r.get("kind") == "lan"]
    wan = [r for r in items if r.get("kind") == "wan"]
    return {
        "ok": ok_count == len(items) and bool(items),
        "total": len(items), "ok_count": ok_count,
        "fail_count": len(items) - ok_count,
        "lan_ok": sum(1 for r in lan if r.get("ok")), "lan_total": len(lan),
        "wan_ok": sum(1 for r in wan if r.get("ok")), "wan_total": len(wan),
        "details": items,
        "message": (f"连通性测试：通过 {ok_count}/{len(items)} "
                    f"（内网 {sum(1 for r in lan if r.get('ok'))}/{len(lan)}，"
                    f"外网 {sum(1 for r in wan if r.get('ok'))}/{len(wan)}）"),
    }
