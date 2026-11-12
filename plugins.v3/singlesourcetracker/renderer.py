"""把订阅、规则和巡检结果画成详情页里那一段 HTML。"""

from __future__ import annotations

import html
from typing import Any, Dict, List

from . import rules

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
.sst-note{font-size:12px;line-height:1.85;padding:12px 14px;border-radius:12px;
  background:rgba(127,127,127,.07);border:1px solid rgba(127,127,127,.14)}
.sst-note b{font-weight:650}
.sst-pre{font-family:var(--font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:11.5px;
  line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0;padding:12px 14px;border-radius:10px;
  background:rgba(127,127,127,.08);border:1px solid rgba(127,127,127,.18)}
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
    return (f'<div class="sst-card"><div class="sst-label">{_e(label)}</div>'
            f'<div class="sst-num">{_e(value)}</div>'
            f'<div class="sst-hint">{_e(hint)}</div></div>')


def _table(headers: List[str], body: str, empty: str) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    if not body:
        return ('<div class="sst-scroll"><table class="sst-table"><thead><tr>'
                + head + '</tr></thead></table>'
                + f'<div class="sst-empty">{_e(empty)}</div></div>')
    return ('<div class="sst-scroll"><table class="sst-table"><thead><tr>'
            + head + '</tr></thead><tbody>' + body + '</tbody></table></div>')


def render(data: Dict[str, Any]) -> str:
    payload = data if isinstance(data, dict) else {}
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    subs = _rows(payload.get("subscriptions"))
    rule_rows = _rows(payload.get("rules"))
    issues = _rows(payload.get("issues"))
    errors = [str(item) for item in (payload.get("errors") or []) if isinstance(item, str)]
    checked = str(payload.get("checked_at") or "")
    mode = str(payload.get("mode") or "notice")

    parts = [_STYLE, '<div class="sst">']
    parts.append(
        '<div class="sst-head"><div class="sst-brand">'
        f'<div class="sst-logo">{_LOGO}</div><div>'
        '<div class="sst-title">单源追剧</div>'
        '<div class="sst-sub">每部剧自己定站点，标题该有什么、不该有什么</div>'
        '</div></div>'
        f'<div class="sst-time">{_e(checked or "还没巡检过，点下面的按钮读一次")}</div>'
        f'<div class="sst-pill">{"自动移除" if mode == "remove" else "只提醒，不动任务"}</div>'
        '</div>'
    )

    parts.append('<div class="sst-grid">')
    parts.append(_kpi("MP 当前订阅", stats.get("subscriptions", 0), "从订阅列表实时读出"))
    parts.append(_kpi("已配规则", stats.get("ruled", 0), f"共 {len(rule_rows)} 条规则"))
    parts.append(_kpi("不合规则的下载", stats.get("problems", 0), "站点不符或标题没过规则"))
    parts.append(_kpi("已扫描下载记录", stats.get("records", 0), "来自下载历史"))
    parts.append('</div>')

    if errors:
        parts.append('<div class="sst-section"><div class="sst-note sst-tag warn">'
                     + _e("；".join(errors[:4])) + '</div></div>')

    body = ""
    for sub in subs[:40]:
        label = _STATE_LABELS.get(str(sub.get("state") or "").upper(), sub.get("state") or "-")
        rule_text = str(sub.get("rule") or "")
        rule_badge = (f'<span class="sst-tag ok">{_e(rule_text[:38])}</span>' if rule_text
                      else '<span class="sst-tag warn">未配规则</span>')
        problems = int(sub.get("problems") or 0)
        status = (f'<span class="sst-tag bad">{problems} 条</span>' if problems
                  else '<span class="sst-tag ok">正常</span>')
        body += (
            '<tr>'
            f'<td class="sst-name">{_e(sub.get("name"))}'
            f'<div class="sst-muted">ID {_e(sub.get("id"))} · 第 {_e(sub.get("season") or "-")} 季 · {_e(label)}</div></td>'
            f'<td>{_e(sub.get("sites"))}</td>'
            f'<td>{rule_badge}<div class="sst-muted">{_e(sub.get("records") or 0)} 条下载记录</div></td>'
            f'<td>{status}</td>'
            '</tr>'
        )
    if len(subs) > 40:
        body += f'<tr><td colspan="4" class="sst-muted">只列前 40 条，共 {len(subs)} 条订阅</td></tr>'
    parts.append('<div class="sst-section"><div class="sst-sec-title">MP 当前订阅'
                 '<span class="sst-sec-note">在设置页写规则，用剧名或 #订阅ID 对上</span></div>')
    parts.append(_table(["剧集", "当前站点范围", "规则", "巡检"],
                        body, "还没读到订阅，点「立即巡检」"))
    parts.append('</div>')

    body = ""
    for row in rule_rows[:20]:
        err = row.get("errors") if isinstance(row.get("errors"), list) else []
        badge = (f'<span class="sst-tag bad">{_e("；".join(str(x) for x in err[:2]))}</span>' if err
                 else f'<span class="sst-tag ok">匹配 {_e(row.get("matched") or 0)} 条订阅</span>')
        body += (
            '<tr>'
            f'<td class="sst-name">{_e(row.get("target"))}'
            f'<div class="sst-muted">第 {_e(row.get("line"))} 行 · 站点 {_e(row.get("site"))}</div></td>'
            f'<td>{_e(row.get("summary"))}</td>'
            f'<td>{badge}</td>'
            '</tr>'
        )
    parts.append('<div class="sst-section"><div class="sst-sec-title">生效中的规则'
                 '<span class="sst-sec-note">必含项要全满足，排除项命中一个就否掉</span></div>')
    parts.append(_table(["匹配目标", "条件", "状态"], body, "设置页还没写规则"))
    parts.append('</div>')

    body = ""
    for issue in issues[:25]:
        reasons = issue.get("reasons") if isinstance(issue.get("reasons"), list) else []
        body += (
            '<tr>'
            f'<td class="sst-name">{_e(issue.get("name"))}'
            f'<div class="sst-muted">第 {_e(issue.get("episode"))} 集 · 来自 {_e(issue.get("site"))}</div></td>'
            f'<td>{_e("；".join(str(item) for item in reasons[:4]))}</td>'
            f'<td>{_e(issue.get("title"))}</td>'
            '</tr>'
        )
    if len(issues) > 25:
        body += f'<tr><td colspan="3" class="sst-muted">只列前 25 条，共 {len(issues)} 条</td></tr>'
    parts.append('<div class="sst-section"><div class="sst-sec-title">不合规则'
                 '<span class="sst-sec-note">标题没标全的项也会算进来</span></div>')
    parts.append(_table(["剧集 / 来源", "原因", "种子标题"], body, "目前没有异常"))
    parts.append('</div>')

    parts.append(
        '<div class="sst-section"><div class="sst-sec-title">规则怎么写'
        '<span class="sst-sec-note">设置页的「规则表」，一行一部剧</span></div>'
        '<div class="sst-note"><b>格式：</b>匹配目标 => 站点 => 纳入 => 排除<br>'
        '匹配目标写剧名（模糊匹配）或 <b>#订阅ID</b>，加 <b>#S1</b> 只管某一季；'
        '站点写站点名 / ID / 域名，留空就是不限站点。<br>'
        '纳入和排除都用 <b>;;</b> 分开写多项：普通文字按标题包含判断，'
        '前面加 <b>re:</b> 就按正则判断（忽略大小写）。纳入项要全部满足，排除项命中一个就不要。<br>'
        f'<pre class="sst-pre">{_e(rules.EXAMPLE)}</pre></div></div>'
    )

    template_text = str(payload.get("template") or "")
    if template_text:
        parts.append('<div class="sst-section"><div class="sst-sec-title">未配规则的订阅，可以复制去改'
                     '<span class="sst-sec-note">点「生成规则模板」重新生成</span></div>'
                     f'<pre class="sst-pre">{_e(template_text)}</pre></div>')

    log = payload.get("log") if isinstance(payload.get("log"), list) else []
    if log:
        parts.append('<div class="sst-section"><div class="sst-sec-title">最近动作</div>'
                     '<div class="sst-log">' + _e("\n".join(str(item) for item in log[:12])) + '</div></div>')

    parts.append('<div class="sst-foot">锁定站点范围会记下原来的范围，点「还原站点范围」可退回；'
                 '自动移除只动下载器任务，不删媒体库文件。规则写错的行会在上面标出来，不影响其他行。</div>')
    parts.append('</div>')
    return "".join(parts)
