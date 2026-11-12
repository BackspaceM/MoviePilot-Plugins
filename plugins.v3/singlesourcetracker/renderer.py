"""把巡检结果画成详情页里那一段 HTML。"""

from __future__ import annotations

import html
from typing import Any, Dict, List

_STYLE = """
<style>
.sst{color:inherit;font-family:inherit;line-height:1.55;font-size:14px}
.sst *{box-sizing:border-box}
.sst-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:2px 0 18px}
.sst-brand{display:flex;align-items:center;gap:12px;min-width:0}
.sst-logo{width:42px;height:42px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#4F46E5,#7C3AED);box-shadow:0 6px 18px rgba(99,102,241,.32)}
.sst-logo svg{width:24px;height:24px}
.sst-title{font-size:19px;font-weight:700;letter-spacing:.3px}
.sst-sub{font-size:12px;opacity:.62;margin-top:1px}
.sst-time{font-size:12px;opacity:.58;white-space:nowrap}
.sst-pill{font-size:11px;font-weight:600;padding:4px 10px;border-radius:8px;
  background:rgba(99,102,241,.13);color:#8B8BF5;white-space:nowrap}
.sst-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:12px;margin-bottom:16px}
.sst-card{background:rgba(127,127,127,.075);border:1px solid rgba(127,127,127,.16);border-radius:14px;padding:14px 15px;min-width:0}
.sst-label{font-size:12px;opacity:.66;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sst-num{font-size:27px;font-weight:750;line-height:1.2;letter-spacing:-.5px;margin-top:8px;
  background:linear-gradient(135deg,#6366F1,#A855F7);-webkit-background-clip:text;background-clip:text;color:transparent}
.sst-hint{font-size:11px;opacity:.55;margin-top:5px}
.sst-section{margin-bottom:16px}
.sst-sec-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:650;margin:0 0 10px}
.sst-sec-title:before{content:'';width:3px;height:13px;border-radius:3px;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.sst-sec-note{font-size:11px;font-weight:400;opacity:.5;margin-left:auto}
.sst-scroll{overflow:auto;border-radius:12px;border:1px solid rgba(127,127,127,.16);background:rgba(127,127,127,.045)}
.sst-table{width:100%;border-collapse:collapse;font-size:12px}
.sst-table th{text-align:left;font-weight:600;opacity:.65;padding:9px 10px;border-bottom:1px solid rgba(127,127,127,.22);white-space:nowrap}
.sst-table td{padding:9px 10px;border-bottom:1px solid rgba(127,127,127,.1);vertical-align:top;overflow-wrap:anywhere}
.sst-table tr:last-child td{border-bottom:0}
.sst-name{font-weight:600}
.sst-muted{opacity:.55;font-size:11px}
.sst-tag{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;border-radius:6px;
  background:rgba(99,102,241,.16);color:#8B8BF5;white-space:nowrap}
.sst-tag.ok{background:rgba(16,185,129,.14);color:#10B981}
.sst-tag.warn{background:rgba(245,158,11,.14);color:#F59E0B}
.sst-tag.bad{background:rgba(239,68,68,.14);color:#EF4444}
.sst-empty{font-size:12px;opacity:.5;padding:16px;text-align:center}
.sst-note{font-size:12px;line-height:1.8;padding:12px 14px;border-radius:12px;
  background:rgba(127,127,127,.07);border:1px solid rgba(127,127,127,.14)}
.sst-note b{font-weight:650}
.sst-log{font-family:var(--font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:11.5px;
  line-height:1.8;opacity:.72;white-space:pre-wrap;overflow-wrap:anywhere}
.sst-foot{font-size:11px;opacity:.46;margin-top:14px;line-height:1.7}
@media (max-width:620px){.sst-num{font-size:23px}.sst-head{gap:10px}.sst-table th,.sst-table td{padding:8px 7px}}
</style>
"""

_LOGO = (
    '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="2.5" y="5" width="5" height="14" rx="1.5" fill="none" stroke="#fff" stroke-width="1.8"/>'
    '<rect x="9.5" y="5" width="5" height="14" rx="1.5" fill="#fff"/>'
    '<path d="M10.6 12.1l1.4 1.4 2.5-3" fill="none" stroke="#6D5AE6" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<rect x="16.5" y="5" width="5" height="14" rx="1.5" fill="none" stroke="#fff" stroke-width="1.8"/>'
    '</svg>'
)

_STATE_LABELS = {"N": "未开始", "R": "订阅中", "P": "待定", "S": "暂停"}


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _rows(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def _kpi(label: str, value: Any, hint: str) -> str:
    return (
        f'<div class="sst-card"><div class="sst-label">{_e(label)}</div>'
        f'<div class="sst-num">{_e(value)}</div>'
        f'<div class="sst-hint">{_e(hint)}</div></div>'
    )


def _table(headers: List[str], body: str, empty: str) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    if not body:
        return f'<div class="sst-scroll"><table class="sst-table"><thead><tr>{head}</tr></thead></table>' \
               f'<div class="sst-empty">{_e(empty)}</div></div>'
    return f'<div class="sst-scroll"><table class="sst-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render(data: Dict[str, Any]) -> str:
    payload = data if isinstance(data, dict) else {}
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    rows = _rows(payload.get("rows"))
    issues = _rows(payload.get("issues"))
    pending = _rows(payload.get("pending"))
    mode = str(payload.get("mode") or "notice")
    checked = str(payload.get("checked_at") or "还没巡检过")

    parts = [_STYLE, '<div class="sst">']
    parts.append(
        '<div class="sst-head"><div class="sst-brand">'
        f'<div class="sst-logo">{_LOGO}</div><div>'
        '<div class="sst-title">单源追剧</div>'
        '<div class="sst-sub">一部剧固定一个站，画质跟着第一集走</div>'
        '</div></div>'
        f'<div class="sst-time">最近巡检 {_e(checked)}</div>'
        f'<div class="sst-pill">{"自动移除" if mode == "remove" else "只提醒，不动任务"}</div>'
        '</div>'
    )

    parts.append('<div class="sst-grid">')
    parts.append(_kpi("已绑定剧集", stats.get("bindings", 0), "绑定后订阅站点范围锁定为单站"))
    parts.append(_kpi("已锁定站点", stats.get("locked", 0), "成功写入订阅站点范围的数量"))
    parts.append(_kpi("来源 / 画质冲突", stats.get("conflicts", 0), "需要处理的下载记录"))
    parts.append(_kpi("待处理提示", stats.get("warnings", 0), "标题没标全的维度，仅供参考"))
    parts.append('</div>')

    body = ""
    for row in rows:
        state = ('<span class="sst-tag ok">已锁定</span>' if row.get("locked")
                 else '<span class="sst-tag warn">仅巡检</span>')
        conflict = int(row.get("conflicts") or 0)
        badge = (f'<span class="sst-tag bad">{conflict} 条冲突</span>' if conflict
                 else '<span class="sst-tag ok">一致</span>')
        body += (
            '<tr>'
            f'<td class="sst-name">{_e(row.get("name"))}'
            f'<div class="sst-muted">第 {_e(row.get("season") or "-")} 季 · {_e(row.get("records") or 0)} 条记录</div></td>'
            f'<td>{_e(row.get("site_name"))}<div class="sst-muted">{state}</div></td>'
            f'<td>{_e(row.get("baseline"))}</td>'
            f'<td>{badge}</td>'
            '</tr>'
        )
    parts.append('<div class="sst-section"><div class="sst-sec-title">已绑定'
                 '<span class="sst-sec-note">画质基线来自该订阅最近一条下载记录</span></div>')
    parts.append(_table(["剧集", "目标站点", "画质基线", "状态"], body, "还没有绑定剧集，在下面挑一部试试"))
    parts.append('</div>')

    body = ""
    for sub in pending[:12]:
        site_count = len(sub.get("sites") or []) if isinstance(sub.get("sites"), list) else 0
        site_text = f"{site_count} 个站点" if site_count else "未限制"
        label = _STATE_LABELS.get(str(sub.get("state") or "").upper(), sub.get("state") or "-")
        body += (
            '<tr>'
            f'<td class="sst-name">{_e(sub.get("name"))}<div class="sst-muted">第 {_e(sub.get("season") or "-")} 季</div></td>'
            f'<td>{_e(label)}</td>'
            f'<td>{_e(site_text)}</td>'
            '</tr>'
        )
    if len(pending) > 12:
        body += f'<tr><td colspan="3" class="sst-muted">还有 {len(pending) - 12} 部，点「锁定全部未绑定」可一次处理</td></tr>'
    parts.append('<div class="sst-section"><div class="sst-sec-title">待绑定'
                 '<span class="sst-sec-note">按钮一次锁定一部，或批量锁定</span></div>')
    parts.append(_table(["剧集", "订阅状态", "当前站点范围"], body, "所有订阅都绑定好了"))
    parts.append('</div>')

    body = ""
    for issue in issues[:20]:
        badge = ('<span class="sst-tag bad">冲突</span>' if issue.get("level") == "conflict"
                 else '<span class="sst-tag warn">提示</span>')
        reasons = issue.get("reasons") if isinstance(issue.get("reasons"), list) else []
        reason_text = "；".join(str(item) for item in reasons[:3])
        body += (
            '<tr>'
            f'<td class="sst-name">{_e(issue.get("name"))}'
            f'<div class="sst-muted">第 {_e(issue.get("episode"))} 集 · {_e(issue.get("site"))}</div></td>'
            f'<td>{badge}<div class="sst-muted">{_e(reason_text)}</div></td>'
            f'<td>{_e(issue.get("title"))}</td>'
            '</tr>'
        )
    if len(issues) > 20:
        body += f'<tr><td colspan="3" class="sst-muted">仅显示最近 20 条，共 {len(issues)} 条</td></tr>'
    parts.append('<div class="sst-section"><div class="sst-sec-title">最近异常'
                 '<span class="sst-sec-note">未标注的维度只作提示，不算冲突</span></div>')
    parts.append(_table(["剧集 / 来源", "问题", "种子标题"], body, "暂没发现来源或画质不一致"))
    parts.append('</div>')

    parts.append(
        '<div class="sst-note"><b>怎么起作用：</b>绑定时把这条订阅的站点范围写成你选的那一个站，'
        '之后只从它下载；同时记下第一集标题里的画质标记（杜比视界 / HDR、H.264 / H.265、'
        '60fps、高码、分辨率、来源），后面每一集都拿来对。解除绑定会还原原来的站点范围。'
        '<br>站点对不上或画质硬冲突会列在上面。默认只提醒；想让它自己移除任务，去设置里改「异常处置」。'
        '</div>'
    )

    log = payload.get("log") if isinstance(payload.get("log"), list) else []
    if log:
        parts.append('<div class="sst-section" style="margin-top:14px"><div class="sst-sec-title">最近动作</div>')
        parts.append('<div class="sst-log">' + _e("\n".join(str(item) for item in log[:12])) + '</div></div>')

    parts.append('<div class="sst-foot">站点范围写入失败时不会拦下载，只保留巡检；'
                 '清理任务需要二次确认，且不会删媒体库里的文件。</div>')
    parts.append('</div>')
    return "".join(parts)
