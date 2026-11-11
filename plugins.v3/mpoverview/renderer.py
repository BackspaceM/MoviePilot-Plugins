"""MP 总览看板：HTML 渲染层。

只生成纯 HTML（内联样式 + 一个 inline onclick），不依赖任何前端构建。
- 样式统一以 ``mpov-`` 前缀作用域，颜色用 inherit / 半透明灰，自动适配深浅主题；
- 主色沿用仓库统一图标的蓝紫渐变 #4F46E5 → #7C3AED；
- 所有动态文本进入 HTML 前必须经 html.escape；
- 交互不写任何自执行脚本：标准按钮由宿主的 @action 重载页面，
  首页仪表盘由 attrs.refresh 定时重绘，全部走原生机制。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

_STYLE = """
<style>
.mpov{color:inherit;font-family:inherit;line-height:1.5;font-size:14px}
.mpov *{box-sizing:border-box}
.mpov-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:2px 0 18px}
.mpov-brand{display:flex;align-items:center;gap:12px;min-width:0}
.mpov-logo{width:42px;height:42px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#4F46E5,#7C3AED);box-shadow:0 6px 18px rgba(99,102,241,.32)}
.mpov-logo svg{width:23px;height:23px}
.mpov-title{font-size:19px;font-weight:700;letter-spacing:.3px}
.mpov-sub{font-size:12px;opacity:.62;margin-top:1px}
.mpov-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.mpov-time{font-size:12px;opacity:.58;white-space:nowrap}
.mpov-pill{font-size:11px;font-weight:600;padding:4px 10px;border-radius:8px;
  background:rgba(99,102,241,.13);color:#8B8BF5;white-space:nowrap}
.mpov-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:12px;margin-bottom:16px}
.mpov-card{background:rgba(127,127,127,.075);border:1px solid rgba(127,127,127,.16);
  border-radius:14px;padding:14px 15px;min-width:0}
.mpov-kpi-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:9px}
.mpov-label{font-size:12px;opacity:.66;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mpov-kpi-icon{width:19px;height:19px;opacity:.62;flex:none}
.mpov-num{font-size:27px;font-weight:750;line-height:1.15;letter-spacing:-.5px;
  background:linear-gradient(135deg,#6366F1,#A855F7);-webkit-background-clip:text;background-clip:text;color:transparent}
.mpov-unit{font-size:12px;font-weight:600;opacity:.55;margin-left:3px}
.mpov-hint{font-size:11px;opacity:.55;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mpov-section{margin-bottom:14px}
.mpov-sec-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:650;margin:0 0 10px}
.mpov-sec-title:before{content:'';width:3px;height:13px;border-radius:3px;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.mpov-sec-note{font-size:11px;font-weight:400;opacity:.5;margin-left:auto}
.mpov-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(268px,1fr));gap:14px}
.mpov-sys{display:flex;flex-wrap:wrap;gap:10px}
.mpov-sys-item{flex:1 1 150px;min-width:0}
.mpov-sys-line{display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:12px}
.mpov-sys-line b{font-size:15px;font-weight:700}
.mpov-bar{height:7px;border-radius:99px;background:rgba(127,127,127,.17);overflow:hidden;margin-top:7px}
.mpov-bar > i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#4F46E5,#7C3AED)}
.mpov-disk{margin-bottom:12px}
.mpov-disk:last-child{margin-bottom:0}
.mpov-disk-top{display:flex;align-items:baseline;justify-content:space-between;gap:10px;font-size:12px}
.mpov-disk-name{font-weight:650;max-width:45%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mpov-disk-meta{opacity:.58;font-size:11px;white-space:nowrap}
.mpov-disk-pct{font-weight:700;font-size:13px}
.mpov-warn{background:linear-gradient(90deg,#F59E0B,#EF4444)}
.mpov-mid{background:linear-gradient(90deg,#FBBF24,#F59E0B)}
.mpov-row{display:flex;align-items:center;gap:9px;padding:7px 0;border-bottom:1px solid rgba(127,127,127,.12);font-size:12px}
.mpov-row:last-child{border-bottom:0}
.mpov-dot{width:7px;height:7px;border-radius:50%;flex:none;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.mpov-row-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.mpov-row-date{opacity:.52;font-size:11px;white-space:nowrap}
.mpov-tag{font-size:10px;padding:2px 7px;border-radius:6px;background:rgba(99,102,241,.16);
  color:#8B8BF5;white-space:nowrap;font-weight:600}
.mpov-empty{font-size:12px;opacity:.5;padding:12px 0}
.mpov-stat{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:6px 0;font-size:12px}
.mpov-stat b{font-weight:700}
.mpov-alert{display:flex;align-items:flex-start;gap:8px;padding:9px 11px;border-radius:10px;
  font-size:12px;margin-bottom:8px;background:rgba(127,127,127,.08);border:1px solid rgba(127,127,127,.14)}
.mpov-alert:last-child{margin-bottom:0}
.mpov-alert-dot{width:7px;height:7px;border-radius:50%;flex:none;margin-top:5px}
.mpov-foot{font-size:11px;opacity:.46;margin-top:14px;line-height:1.7}
.mpov-badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:6px;
  background:rgba(99,102,241,.16);color:#8B8BF5;margin-left:8px;vertical-align:middle}
@media (max-width:620px){.mpov-num{font-size:23px}.mpov-head{gap:10px}}
</style>
"""

_ICONS = {
    "subscribe": '<path d="M4 6h16M4 12h16M4 18h10"/><circle cx="19" cy="18" r="2"/>',
    "import": '<path d="M12 3v12m0 0-4-4m4 4 4-4"/><path d="M4 17v3h16v-3"/>',
    "download": '<path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 20h16"/>',
    "server": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    "plugin": '<path d="M9 3v4M15 3v4M9 21v-4M15 21v-4M3 9h4M3 15h4M21 9h-4M21 15h-4"/><rect x="7" y="7" width="10" height="10" rx="2"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
}
_LEVEL_COLORS = {"ok": "#10B981", "info": "#6366F1", "warn": "#F59E0B", "danger": "#EF4444"}


def _icon(name: str) -> str:
    return (
        '<svg class="mpov-kpi-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        + _ICONS.get(name, _ICONS["server"])
        + "</svg>"
    )


def _num(value: Any, offline: bool = False, host: bool = True) -> str:
    if offline and host:
        return "—"
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _kpi(label: str, value: Any, hint: str, icon: str, offline: bool = False) -> str:
    return (
        '<div class="mpov-card"><div class="mpov-kpi-top">'
        f'<span class="mpov-label">{html.escape(label)}</span>{_icon(icon)}</div>'
        f'<div class="mpov-num">{_num(value, offline)}</div>'
        f'<div class="mpov-hint">{html.escape(hint)}</div></div>'
    )


def _bar(percent: Optional[float], offline: bool = False) -> str:
    if percent is None:
        return '<div class="mpov-bar"><i style="width:0%"></i></div>'
    width = max(0.0, min(100.0, float(percent)))
    tone = "mpov-warn" if width >= 90 else ("mpov-mid" if width >= 80 else "")
    return f'<div class="mpov-bar"><i class="{tone}" style="width:{width:.1f}%"></i></div>'


def _system_block(data: Dict[str, Any]) -> str:
    system = data["system"]
    cpu = system.get("cpu_percent")
    mem = system.get("memory_percent")
    items = [
        ("CPU 负载", f"{cpu:.0f}%" if isinstance(cpu, (int, float)) else "—", cpu,
         f"{system.get('cores', 1)} 核，1 分钟负载 {system.get('load1', '—')}"),
        ("内存占用", f"{mem:.0f}%" if isinstance(mem, (int, float)) else "—", mem,
         system.get("memory_text", "—")),
    ]
    blocks = []
    for label, text, percent, note in items:
        blocks.append(
            '<div class="mpov-sys-item"><div class="mpov-sys-line">'
            f'<span class="mpov-label">{html.escape(label)}</span><b>{html.escape(text)}</b></div>'
            + _bar(percent)
            + f'<div class="mpov-hint">{html.escape(note)}</div></div>'
        )
    return (
        '<div class="mpov-section"><div class="mpov-card">'
        '<div class="mpov-sec-title">系统状态'
        f'<span class="mpov-sec-note">已运行 {html.escape(str(system.get("uptime", "—")))}</span></div>'
        '<div class="mpov-sys">' + "".join(blocks) + "</div></div></div>"
    )


def _storage_block(data: Dict[str, Any]) -> str:
    rows: List[str] = []
    for disk in data["storage"]:
        rows.append(
            '<div class="mpov-disk"><div class="mpov-disk-top">'
            f'<span class="mpov-disk-name">{html.escape(str(disk["name"]))}</span>'
            f'<span class="mpov-disk-meta">剩 {html.escape(disk["free_text"])} / {html.escape(disk["total_text"])}</span>'
            f'<span class="mpov-disk-pct">{disk["percent"]:.0f}%</span></div>'
            + _bar(disk["percent"])
            + f'<div class="mpov-hint">{html.escape(str(disk["path"]))}</div></div>'
        )
    if not rows:
        rows.append('<div class="mpov-empty">未检测到可统计的存储目录</div>')
    return (
        '<div class="mpov-section"><div class="mpov-card">'
        '<div class="mpov-sec-title">存储空间<span class="mpov-sec-note">按使用率排序</span></div>'
        + "".join(rows) + "</div></div>"
    )


def _recent_block(data: Dict[str, Any]) -> str:
    recent = data["imports"].get("recent") or []
    rows: List[str] = []
    for item in recent:
        # 所有动态文本统一在渲染层转义，采集层只提供原始值。
        detail = html.escape(str(item.get("detail") or ""))
        title = html.escape(str(item.get("title") or "未命名媒体"))
        category = html.escape(str(item.get("category") or "媒体"))
        date = html.escape(str(item.get("date") or ""))
        rows.append(
            '<div class="mpov-row"><span class="mpov-dot"></span>'
            f'<span class="mpov-row-title">{title}{(" · " + detail) if detail else ""}</span>'
            f'<span class="mpov-tag">{category}</span>'
            f'<span class="mpov-row-date">{date}</span></div>'
        )
    if not rows:
        rows.append('<div class="mpov-empty">暂无成功入库记录</div>')
    return (
        '<div class="mpov-card"><div class="mpov-sec-title">近期入库'
        f'<span class="mpov-sec-note">今日 {_num(data["imports"].get("today"), data["offline"])} 条'
        f' · 近 7 天 {_num(data["imports"].get("week"), data["offline"])} 条</span></div>'
        + "".join(rows) + "</div>"
    )


def _status_block(data: Dict[str, Any]) -> str:
    sub = data["subscriptions"]
    services = data["services"]
    plugins = data["plugins"]
    offline = data["offline"]
    rows = [
        ("媒体服务器", _num(services["media_servers"], offline), "已连接实例"),
        ("下载器", _num(services["downloaders"], offline), "已连接实例"),
        ("订阅中 / 暂停", f'{_num(sub["active"], offline)} / {_num(sub["paused"], offline)}', "当前状态分布"),
        ("待补集数", _num(sub["lack"], offline), "所有订阅累计"),
        ("插件启用", f'{_num(plugins["enabled"], offline)} / {_num(plugins["total"], offline)}', "启用 / 已安装"),
    ]
    body = "".join(
        f'<div class="mpov-stat"><span class="mpov-label">{html.escape(label)}</span>'
        f'<b>{html.escape(str(value))}<span class="mpov-unit">{html.escape(note)}</span></b></div>'
        for label, value, note in rows
    )
    alerts = "".join(
        '<div class="mpov-alert">'
        f'<span class="mpov-alert-dot" style="background:{_LEVEL_COLORS.get(a["level"], "#6366F1")}"></span>'
        f'<span>{html.escape(a["text"])}</span></div>'
        for a in data.get("alerts") or []
    )
    return (
        '<div class="mpov-card"><div class="mpov-sec-title">运行状态</div>'
        + body
        + '<div style="margin-top:10px">' + alerts + "</div></div>"
    )


def render(data: Dict[str, Any], compact: bool = False) -> str:
    """把快照渲染成可直接塞进 v-html 的 HTML 片段。

    页面由宿主负责重绘：详情页点标准按钮触发 @action 重新拉取 get_page()，
    首页仪表盘按 attrs.refresh 定时重新调用 get_dashboard()。
    """
    offline = bool(data.get("offline"))
    sub = data["subscriptions"]
    imports = data["imports"]
    services = data["services"]
    plugins = data["plugins"]

    badge = '<span class="mpov-badge">离线预览</span>' if offline else ""
    kpis = [
        _kpi("当前订阅", sub["total"], f'订阅中 {_num(sub["active"], offline)} · 暂停 {_num(sub["paused"], offline)}', "subscribe", offline),
        _kpi("累计入库", imports["total"], f'近 7 天 {_num(imports["week"], offline)} 条', "import", offline),
        _kpi("近 7 天入库", imports["week"], "最近一周成功整理", "calendar", offline),
        _kpi("下载记录", data["downloads"]["total"], "历史累计", "download", offline),
    ]
    if not compact:
        kpis += [
            _kpi("已连接服务", services["media_servers"] + services["downloaders"],
                 f'媒体服务器 {_num(services["media_servers"], offline)} · 下载器 {_num(services["downloaders"], offline)}', "server", offline),
            _kpi("插件", plugins["enabled"], f'共安装 {_num(plugins["total"], offline)} 个', "plugin", offline),
        ]

    parts = [
        _STYLE,
        '<div class="mpov">',
        '<div class="mpov-head"><div class="mpov-brand">',
        '<div class="mpov-logo"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.9" '
        'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/>'
        '<rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/>'
        '<path d="M14 17.5h7M17.5 14v7"/></svg></div>',
        f'<div><div class="mpov-title">MP 总览{badge}</div>'
        '<div class="mpov-sub">一页看清系统、存储、订阅与入库</div></div>',
        '</div><div class="mpov-actions">',
        f'<span class="mpov-time">更新于 {html.escape(str(data["generated_at"]))}</span>',
        '<span class="mpov-pill">只读快照</span>',
        '</div></div>',
        '<div class="mpov-grid">' + "".join(kpis) + "</div>",
    ]

    if not compact:
        parts.append(_system_block(data))
    parts.append(_storage_block(data))
    parts.append('<div class="mpov-cols">')
    parts.append(_recent_block(data))
    if not compact:
        parts.append(_status_block(data))
    parts.append("</div>")

    if offline:
        foot = "当前未连接 MoviePilot 宿主数据接口，业务数字显示为占位符；安装启用后自动读取真实数据。"
    else:
        foot = "数据来自 MoviePilot V3 稳定只读接口，全程不修改任何配置或媒体文件。首页仪表盘按设定间隔自动刷新。"
    if compact:
        foot = "MP 总览 · 只读快照 · 详情页可查看完整信息"
    parts.append(f'<div class="mpov-foot">{html.escape(foot)}</div></div>')
    return "".join(parts)
