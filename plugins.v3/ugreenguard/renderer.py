"""绿联 NAS 守护详情页的 HTML，动态文本一律先转义再进页面。"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

MISSING = "未上报"

_STYLE = """
<style>
.ugn{color:inherit;font-family:inherit;line-height:1.5;font-size:14px}
.ugn *{box-sizing:border-box}
.ugn-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:2px 0 16px}
.ugn-brand{display:flex;align-items:center;gap:12px;min-width:0}
.ugn-logo{width:42px;height:42px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#4F46E5,#7C3AED);box-shadow:0 6px 18px rgba(99,102,241,.32)}
.ugn-logo svg{width:23px;height:23px}
.ugn-title{font-size:19px;font-weight:700;letter-spacing:.3px}
.ugn-sub{font-size:12px;opacity:.62;margin-top:1px}
.ugn-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.ugn-time{font-size:12px;opacity:.58;white-space:nowrap}
.ugn-pill{font-size:11px;font-weight:600;padding:4px 10px;border-radius:8px;
  background:rgba(99,102,241,.13);color:#8B8BF5;white-space:nowrap}
.ugn-note{background:rgba(127,127,127,.075);border:1px solid rgba(127,127,127,.16);
  border-radius:14px;padding:13px 15px;margin-bottom:16px;font-size:12px;line-height:1.8}
.ugn-note p{margin:0 0 6px}
.ugn-note p:last-child{margin:0}
.ugn-code{display:block;margin-top:8px;padding:9px 11px;border-radius:9px;
  background:rgba(127,127,127,.10);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:11px;line-height:1.6;overflow-x:auto;white-space:pre-wrap;word-break:break-all}
.ugn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.ugn-card{background:rgba(127,127,127,.075);border:1px solid rgba(127,127,127,.16);
  border-radius:14px;padding:14px 15px;min-width:0}
.ugn-kpi-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:9px}
.ugn-label{font-size:12px;opacity:.66;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ugn-kpi-icon{width:19px;height:19px;opacity:.62;flex:none}
.ugn-num{font-size:25px;font-weight:750;line-height:1.15;letter-spacing:-.5px;
  background:linear-gradient(135deg,#6366F1,#A855F7);-webkit-background-clip:text;background-clip:text;color:transparent}
.ugn-hint{font-size:11px;opacity:.55;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ugn-section{margin-bottom:14px}
.ugn-sec-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:650;margin:0 0 10px}
.ugn-sec-title:before{content:'';width:3px;height:13px;border-radius:3px;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.ugn-sec-note{font-size:11px;font-weight:400;opacity:.5;margin-left:auto}
.ugn-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.ugn-table{width:100%;border-collapse:collapse;font-size:12px}
.ugn-table th{text-align:left;font-weight:600;opacity:.55;padding:7px 8px;border-bottom:1px solid rgba(127,127,127,.18);white-space:nowrap}
.ugn-table td{padding:8px;border-bottom:1px solid rgba(127,127,127,.10);vertical-align:middle}
.ugn-table tr:last-child td{border-bottom:0}
.ugn-name{font-weight:600;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ugn-bar{height:7px;border-radius:99px;background:rgba(127,127,127,.17);overflow:hidden;margin-top:6px;min-width:80px}
.ugn-bar > i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#4F46E5,#7C3AED)}
.ugn-warn{background:linear-gradient(90deg,#F59E0B,#EF4444)}
.ugn-mid{background:linear-gradient(90deg,#FBBF24,#F59E0B)}
.ugn-pct{font-weight:700;white-space:nowrap}
.ugn-empty{font-size:12px;opacity:.5;padding:10px 0}
.ugn-alert{display:flex;align-items:flex-start;gap:8px;padding:9px 11px;border-radius:10px;
  font-size:12px;margin-bottom:8px;background:rgba(127,127,127,.08);border:1px solid rgba(127,127,127,.14)}
.ugn-alert:last-child{margin-bottom:0}
.ugn-alert-dot{width:7px;height:7px;border-radius:50%;flex:none;margin-top:5px}
.ugn-alert-body{flex:1;min-width:0}
.ugn-tag{font-size:10px;font-weight:700;padding:2px 7px;border-radius:6px;
  background:rgba(99,102,241,.16);color:#8B8BF5;white-space:nowrap;margin-left:8px}
.ugn-stat{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:6px 0;font-size:12px}
.ugn-stat b{font-weight:700}
.ugn-sys-item{margin-bottom:12px}
.ugn-sys-item:last-child{margin-bottom:0}
.ugn-sys-line{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.ugn-log{font-size:12px}
.ugn-log-row{display:flex;gap:9px;padding:7px 0;border-bottom:1px solid rgba(127,127,127,.12)}
.ugn-log-row:last-child{border-bottom:0}
.ugn-log-time{opacity:.5;white-space:nowrap;font-size:11px}
.ugn-log-msg{flex:1;min-width:0;word-break:break-word}
.ugn-foot{font-size:11px;opacity:.46;margin-top:14px;line-height:1.8}
.ugn-badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:6px;
  background:rgba(99,102,241,.16);color:#8B8BF5;margin-left:8px;vertical-align:middle}
@media (max-width:620px){.ugn-num{font-size:22px}.ugn-head{gap:10px}.ugn-cols{grid-template-columns:1fr}}
</style>
"""

_ICONS = {
    "volume": '<path d="M3 6h18v12H3z"/><path d="M7 10h.01M7 14h.01M11 10h6M11 14h6"/>',
    "temp": '<path d="M14 14.76V4a2 2 0 0 0-4 0v10.76a4 4 0 1 0 4 0z"/>',
    "ups": '<rect x="3" y="7" width="16" height="10" rx="2"/><path d="M21 10v4M7 12h6"/>',
    "alert": '<path d="M12 3 2 20h20L12 3z"/><path d="M12 9v5M12 17h.01"/>',
    "container": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    "system": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 8h8v8H8z"/>',
    "log": '<path d="M4 6h16M4 12h16M4 18h10"/>',
}
_LEVEL_COLORS = {"ok": "#10B981", "info": "#6366F1", "warn": "#F59E0B", "danger": "#EF4444"}
_ACTION_LABELS = {
    "pause_download": "暂停下载",
    "notify": "仅提醒",
    "resume_download": "恢复下载",
    "none": "暂不处理",
}

_JSON_EXAMPLE = (
    '{"volumes":[{"name":"volume1","percent":63}],'
    '"disks":[{"name":"disk1","temp":41,"smart_alert":false}],'
    '"containers":[{"name":"moviepilot","status":"running"}],'
    '"ups":{"on_battery":false,"battery":100}}'
)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: Any, suffix: str = "", digits: int = 1) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    if isinstance(value, int):
        return f"{value}{suffix}"
    if value is None:
        return MISSING
    text = str(value).strip()
    if not text or text == MISSING:
        return MISSING
    return text + suffix


def _human(value: Any) -> str:
    if not _is_number(value) or value < 0:
        return MISSING
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    num = float(value)
    for unit in units:
        if num < 1024 or unit == units[-1]:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return MISSING


def _icon(name: str) -> str:
    return (
        '<svg class="ugn-kpi-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        + _ICONS.get(name, _ICONS["system"])
        + "</svg>"
    )


def _bar(percent: Any) -> str:
    if not _is_number(percent):
        return '<div class="ugn-bar" title="未上报"><i style="width:0%"></i></div>'
    width = max(0.0, min(100.0, float(percent)))
    tone = "ugn-warn" if width >= 90 else ("ugn-mid" if width >= 80 else "")
    return f'<div class="ugn-bar"><i class="{tone}" style="width:{width:.1f}%"></i></div>'


def _kpi(label: str, value: str, hint: str, icon: str) -> str:
    return (
        '<div class="ugn-card"><div class="ugn-kpi-top">'
        f'<span class="ugn-label">{_esc(label)}</span>{_icon(icon)}</div>'
        f'<div class="ugn-num">{_esc(value)}</div>'
        f'<div class="ugn-hint">{_esc(hint)}</div></div>'
    )


def _section(title: str, note: str, inner: str) -> str:
    return (
        '<div class="ugn-section"><div class="ugn-card">'
        f'<div class="ugn-sec-title">{_esc(title)}'
        f'<span class="ugn-sec-note">{_esc(note)}</span></div>'
        + inner + "</div></div>"
    )


def _table(headers: List[str], rows: List[str], empty: str) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(rows) if rows else (
        f'<tr><td colspan="{len(headers)}" class="ugn-empty">{_esc(empty)}</td></tr>'
    )
    return f'<table class="ugn-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _note_block(data: Dict[str, Any]) -> str:
    source = _esc(data.get("source") or MISSING)
    services = data.get("services")
    service_line = ""
    if isinstance(services, dict):
        service_line = (
            f'容器自检里看到已配置媒体服务器 {_esc(_fmt(services.get("media")))} 个、'
            f'下载器 {_esc(_fmt(services.get("download")))} 个。'
        )
    return (
        '<div class="ugn-note">'
        '<p>绿联 NAS 的开放接口是 HTTPS + protobuf，还要按权限节点发令牌，目前令牌只放开了 docker ps，'
        'storage、taskmgr、ctl 都返回 403，MP 容器里不方便直连。</p>'
        '<p>所以这里走只读状态桥：放在 NAS 外面的一个只读小服务，把状态整理成一段简单 JSON，'
        '插件只负责读它，不碰 NAS 本身。没配状态桥时，就用容器内看得见的目录水位兜底，'
        '没上报的部分一律标「未上报」，不会当成正常，也不会乱恢复下载。</p>'
        '<p>状态桥大概给成这样就行，缺哪段就空着：'
        f'<code class="ugn-code">{_esc(_JSON_EXAMPLE)}</code></p>'
        f'<p>当前数据来源：{source}。{service_line}</p>'
        "</div>"
    )


def _hits_block(hits: List[Dict[str, Any]]) -> str:
    rows = []
    for hit in hits:
        level = str(hit.get("level") or "info")
        action = str(hit.get("action") or "none")
        color = _LEVEL_COLORS.get(level, "#6366F1")
        rows.append(
            '<div class="ugn-alert">'
            f'<span class="ugn-alert-dot" style="background:{color}"></span>'
            f'<span class="ugn-alert-body">{_esc(hit.get("detail") or "")}'
            f'<span class="ugn-tag">{_esc(_ACTION_LABELS.get(action, action))}</span></span></div>'
        )
    return "".join(rows) or '<div class="ugn-empty">没有需要处理的项</div>'


def _volumes_block(volumes: List[Dict[str, Any]]) -> str:
    rows = []
    for vol in volumes:
        rows.append(
            "<tr>"
            f'<td class="ugn-name">{_esc(_fmt(vol.get("name")))}</td>'
            f"<td>{_esc(_human(vol.get('used')))}</td>"
            f"<td>{_esc(_human(vol.get('total')))}</td>"
            f'<td><span class="ugn-pct">{_esc(_fmt(vol.get("percent"), "%"))}</span>{_bar(vol.get("percent"))}</td>'
            "</tr>"
        )
    return _table(["名称", "已用", "总量", "水位"], rows, "没有上报存储卷")


def _disks_block(disks: List[Dict[str, Any]]) -> str:
    rows = []
    for disk in disks:
        rows.append(
            "<tr>"
            f'<td class="ugn-name">{_esc(_fmt(disk.get("name")))}</td>'
            f"<td>{_esc(_fmt(disk.get('temp'), '℃'))}</td>"
            f"<td>{_esc(_fmt(disk.get('health')))}</td>"
            f"<td>{_esc(_fmt(disk.get('smart_alert')))}</td>"
            "</tr>"
        )
    return _table(["名称", "温度", "健康", "SMART 告警"], rows, "没有上报磁盘")


def _containers_block(containers: List[Dict[str, Any]]) -> str:
    rows = []
    for container in containers:
        rows.append(
            "<tr>"
            f'<td class="ugn-name">{_esc(_fmt(container.get("name")))}</td>'
            f"<td>{_esc(_fmt(container.get('status')))}</td>"
            f"<td>{_esc(_fmt(container.get('restarts')))}</td>"
            "</tr>"
        )
    return _table(["名称", "状态", "重启次数"], rows, "没有上报容器")


def _ups_system_block(status: Dict[str, Any]) -> str:
    ups = status.get("ups") if isinstance(status.get("ups"), dict) else {}
    system = status.get("system") if isinstance(status.get("system"), dict) else {}
    lines = [
        '<div class="ugn-stat"><span class="ugn-label">电池供电</span>'
        f'<b>{_esc(_fmt(ups.get("on_battery")))}</b></div>',
        '<div class="ugn-stat"><span class="ugn-label">剩余电量</span>'
        f'<b>{_esc(_fmt(ups.get("battery"), "%"))}</b></div>',
    ]
    for label, key in (("CPU 占用", "cpu"), ("内存占用", "mem")):
        value = system.get(key)
        lines.append(
            '<div class="ugn-sys-item"><div class="ugn-sys-line">'
            f'<span class="ugn-label">{_esc(label)}</span>'
            f'<b>{_esc(_fmt(value, "%"))}</b></div>{_bar(value)}</div>'
        )
    return "".join(lines)


def _log_block(logs: List[Dict[str, Any]]) -> str:
    rows = []
    for entry in logs[:10]:
        rows.append(
            '<div class="ugn-log-row">'
            f'<span class="ugn-log-time">{_esc(entry.get("time") or "")}</span>'
            f'<span class="ugn-log-msg">[{_esc(entry.get("level") or "")}] '
            f'{_esc(entry.get("message") or "")}</span></div>'
        )
    return '<div class="ugn-log">' + ("".join(rows) or '<div class="ugn-empty">还没记录过动作</div>') + "</div>"


def render(data: Dict[str, Any]) -> str:
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    hits = data.get("hits") if isinstance(data.get("hits"), list) else []
    logs = data.get("log") if isinstance(data.get("log"), list) else []
    cfg = data.get("cfg") if isinstance(data.get("cfg"), dict) else {}

    volumes = status.get("volumes") if isinstance(status.get("volumes"), list) else []
    disks = status.get("disks") if isinstance(status.get("disks"), list) else []
    containers = status.get("containers") if isinstance(status.get("containers"), list) else []

    percents = [v.get("percent") for v in volumes if isinstance(v, dict) and _is_number(v.get("percent"))]
    temps = [d.get("temp") for d in disks if isinstance(d, dict) and _is_number(d.get("temp"))]
    max_percent = max(percents) if percents else None
    max_temp = max(temps) if temps else None

    ups = status.get("ups") if isinstance(status.get("ups"), dict) else {}
    on_battery = ups.get("on_battery")
    ups_text = "未上报" if not isinstance(on_battery, bool) else ("电池供电" if on_battery else "市电")
    ups_hint = f'电量 {_fmt(ups.get("battery"), "%")}'
    alert_count = len([h for h in hits if isinstance(h, dict) and h.get("action") in ("pause_download", "notify")])

    kpis = [
        _kpi("卷最高水位", _fmt(max_percent, "%"), f'阈值 {_fmt(cfg.get("water_threshold"), "%", 0)}', "volume"),
        _kpi("盘最高温度", _fmt(max_temp, "℃"), f'阈值 {_fmt(cfg.get("temp_threshold"), "℃", 0)}', "temp"),
        _kpi("UPS 供电", ups_text, ups_hint, "ups"),
        _kpi("命中提醒", str(alert_count), f'当前处置 {data.get("mode") or "试运行"}', "alert"),
    ]

    badge = '<span class="ugn-badge">暂停中</span>' if data.get("paused") else ""
    foot = (
        '默认试运行，只记动作和日志不动下载器；切到自动处置才会真的暂停或恢复。'
        '同一条规则 30 分钟内不重复提醒，状态变了才记一笔。容器自检的数据不全，'
        '不会自动恢复下载，需要时点页面上的「恢复下载」。'
    )

    parts = [
        _STYLE,
        '<div class="ugn">',
        '<div class="ugn-head"><div class="ugn-brand">',
        '<div class="ugn-logo"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.9" '
        'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="7" rx="2"/>'
        '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01M12 3v2M12 19v2"/></svg></div>',
        f'<div><div class="ugn-title">绿联 NAS 守护{badge}</div>'
        '<div class="ugn-sub">读一段状态 JSON，盯住水位、盘温、容器和 UPS</div></div>',
        '</div><div class="ugn-actions">',
        f'<span class="ugn-time">更新于 {_esc(data.get("generated_at") or "")}</span>',
        '<span class="ugn-pill">只读状态桥</span>',
        '</div></div>',
        _note_block(data),
        '<div class="ugn-grid">' + "".join(kpis) + "</div>",
        _section("当前命中", "按规则逐条判断", _hits_block(hits)),
        '<div class="ugn-cols">',
        _section("存储卷", f'共 {len(volumes)} 个', _volumes_block(volumes)),
        _section("磁盘", f'共 {len(disks)} 块', _disks_block(disks)),
        _section("关注容器", f'共 {len(containers)} 个', _containers_block(containers)),
        _section("UPS 与系统", "没上报就显示未上报", _ups_system_block(status)),
        "</div>",
        _section("最近动作", f'保留最近 {min(len(logs), 10)} 条', _log_block(logs)),
        f'<div class="ugn-foot">{_esc(foot)}</div></div>',
    ]
    return "".join(parts)
