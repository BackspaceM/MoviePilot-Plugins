"""MP 备份迁移：把状态渲染成可塞进 v-html 的页面片段。"""

from __future__ import annotations

import html
from typing import Any, Dict

_STYLE = """
<style>
.mpbk{color:inherit;font-family:inherit;line-height:1.55;font-size:14px}
.mpbk *{box-sizing:border-box}
.mpbk-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:2px 0 18px}
.mpbk-brand{display:flex;align-items:center;gap:12px;min-width:0}
.mpbk-logo{width:42px;height:42px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#4F46E5,#7C3AED);box-shadow:0 6px 18px rgba(99,102,241,.32)}
.mpbk-logo svg{width:23px;height:23px}
.mpbk-title{font-size:19px;font-weight:700;letter-spacing:.3px}
.mpbk-sub{font-size:12px;opacity:.62;margin-top:1px}
.mpbk-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.mpbk-time{font-size:12px;opacity:.58;white-space:nowrap}
.mpbk-pill{font-size:11px;font-weight:600;padding:4px 10px;border-radius:8px;
  background:rgba(99,102,241,.13);color:#8B8BF5;white-space:nowrap}
.mpbk-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:12px;margin-bottom:16px}
.mpbk-card{background:rgba(127,127,127,.075);border:1px solid rgba(127,127,127,.16);border-radius:14px;padding:14px 15px;min-width:0}
.mpbk-kpi-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:9px}
.mpbk-label{font-size:12px;opacity:.66;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mpbk-kpi-icon{width:19px;height:19px;opacity:.62;flex:none}
.mpbk-num{font-size:27px;font-weight:750;line-height:1.15;letter-spacing:-.5px;
  background:linear-gradient(135deg,#6366F1,#A855F7);-webkit-background-clip:text;background-clip:text;color:transparent}
.mpbk-hint{font-size:11px;opacity:.55;margin-top:5px}
.mpbk-section{margin-bottom:14px}
.mpbk-sec-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:650;margin:0 0 10px}
.mpbk-sec-title:before{content:'';width:3px;height:13px;border-radius:3px;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.mpbk-sec-note{font-size:11px;font-weight:400;opacity:.5;margin-left:auto}
.mpbk-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid rgba(127,127,127,.12);font-size:12px}
.mpbk-row:last-child{border-bottom:0}
.mpbk-row b{font-weight:700}
.mpbk-table{width:100%;border-collapse:collapse;font-size:12px}
.mpbk-table th{text-align:left;font-weight:600;opacity:.65;padding:8px;border-bottom:1px solid rgba(127,127,127,.22)}
.mpbk-table td{padding:8px;border-bottom:1px solid rgba(127,127,127,.1);vertical-align:top;overflow-wrap:anywhere}
.mpbk-table tr:last-child td{border-bottom:0}
.mpbk-scroll{overflow:auto;border-radius:12px;border:1px solid rgba(127,127,127,.16);background:rgba(127,127,127,.045)}
.mpbk-empty{font-size:12px;opacity:.5;padding:14px 0}
.mpbk-tag{font-size:10px;padding:2px 7px;border-radius:6px;background:rgba(99,102,241,.16);color:#8B8BF5;font-weight:600;white-space:nowrap}
.mpbk-tag.warn{background:rgba(245,158,11,.14);color:#F59E0B}
.mpbk-tag.ok{background:rgba(16,185,129,.14);color:#10B981}
.mpbk-tag.err{background:rgba(239,68,68,.14);color:#EF4444}
.mpbk-tag.info{background:rgba(99,102,241,.14);color:#8B8BF5}
.mpbk-pre{font-family:var(--font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:11.5px;
  line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:12px 14px;border-radius:12px;
  background:rgba(127,127,127,.08);border:1px solid rgba(127,127,127,.18)}
.mpbk-log{max-height:240px;overflow:auto}
.mpbk-note{font-size:12px;opacity:.74;line-height:1.85}
.mpbk-note b{font-weight:700}
.mpbk-foot{font-size:11px;opacity:.46;margin-top:14px;line-height:1.7}
.mpbk-badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:6px;
  background:rgba(99,102,241,.16);color:#8B8BF5;margin-left:8px;vertical-align:middle}
@media (max-width:620px){.mpbk-num{font-size:23px}.mpbk-head{gap:10px}}
</style>
"""

_ICONS = {
    "box": '<path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7M12 11v10"/>',
    "disk": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14h18M7 18h.01"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "swap": '<path d="M4 8h13m0 0-3-3m3 3-3 3"/><path d="M20 16H7m0 0 3-3m-3 3 3 3"/>',
}

_SECTION_NAMES = {
    "subscriptions": "订阅", "download_history": "下载历史", "transfer_history": "整理历史",
    "sites": "站点", "services": "服务", "plugin_data": "插件数据", "users": "用户",
    "subscription_history": "订阅历史", "system_config": "系统配置",
    "plugin_configs": "插件配置",
}

_IMPORT_NOTICE = (
    "导入迁移：先点某份快照的「预览导入」看差异，这一步只读、零风险。"
    "导入默认试运行，只列出将要执行的动作，不写回时数据库一点没动。"
    "真要写回必须点「执行导入」并确认，才会按设置里的冲突策略落库。"
    "开了「含明文凭据」的快照里有 Cookie / UA / Apikey / Token，别外传，用完及时从备份目录清掉。"
)


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _icon(name: str) -> str:
    return (
        '<svg class="mpbk-kpi-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        + _ICONS.get(name, _ICONS["box"]) + "</svg>"
    )


def _kpi(label: str, value: Any, hint: str, icon: str) -> str:
    return (
        '<div class="mpbk-card"><div class="mpbk-kpi-top">'
        f'<span class="mpbk-label">{_e(label)}</span>{_icon(icon)}</div>'
        f'<div class="mpbk-num">{_e(value)}</div>'
        f'<div class="mpbk-hint">{_e(hint)}</div></div>'
    )


def _status_block(data: Dict[str, Any]) -> str:
    sources = data.get("sources") or {}
    ok = sum(1 for v in sources.values() if v.get("status") == "ok")
    bad = sum(1 for v in sources.values() if v.get("status") == "unavailable")
    sections = "、".join(_SECTION_NAMES.get(s, s) for s in (data.get("sections") or [])) or "未选择"
    rows = [
        ("插件状态", "启用中" if data.get("enabled") else "已停用"),
        ("执行时间", data.get("cron_label") or "—"),
        ("保留份数", f'{data.get("keep", 7)} 份'),
        ("备份目录", data.get("backup_dir") or "—"),
        ("备份内容", sections),
        ("数据来源", f"{ok} 项正常 · {bad} 项不可用" if sources else "还没生成过快照"),
    ]
    body = "".join(
        f'<div class="mpbk-row"><span class="mpbk-label">{_e(label)}</span>'
        f"<b>{_e(value)}</b></div>"
        for label, value in rows
    )
    return ('<div class="mpbk-section"><div class="mpbk-card">'
            '<div class="mpbk-sec-title">运行状态</div>' + body + "</div></div>")


def _snapshots_block(data: Dict[str, Any]) -> str:
    rows = data.get("snapshots") or []
    if not rows:
        body = '<div class="mpbk-empty">备份目录里还没有快照，点上面的「立即备份」生成第一份。</div>'
    else:
        trs = "".join(
            "<tr>"
            f'<td>{_e(r.get("name"))}</td>'
            f'<td>{_e(r.get("size_text"))}</td>'
            f'<td>{_e(r.get("count") if r.get("count") is not None else "—")}</td>'
            f'<td>{_e(r.get("mtime"))}</td>'
            "</tr>"
            for r in rows[:20]
        )
        body = ('<div class="mpbk-scroll"><table class="mpbk-table"><thead><tr>'
                '<th>文件名</th><th>大小</th><th>条目数</th><th>生成时间</th></tr></thead><tbody>'
                + trs + "</tbody></table></div>")
    note = f'共 {len(rows)} 份' if rows else ""
    return ('<div class="mpbk-section"><div class="mpbk-card">'
            '<div class="mpbk-sec-title">快照列表'
            f'<span class="mpbk-sec-note">{_e(note)}</span></div>'
            + body + "</div></div>")


def _diff_block(data: Dict[str, Any]) -> str:
    diff = data.get("diff")
    if not diff:
        body = '<div class="mpbk-empty">至少要有两份快照才能对比，先多备份一次。</div>'
    else:
        rows = []
        details = []
        for name, sec in (diff.get("sections") or {}).items():
            rows.append(
                "<tr>"
                f'<td>{_e(sec.get("label") or name)}</td>'
                f'<td><span class="mpbk-tag ok">{len(sec.get("added") or [])}</span></td>'
                f'<td><span class="mpbk-tag warn">{len(sec.get("removed") or [])}</span></td>'
                f'<td>{len(sec.get("changed") or [])}</td>'
                "</tr>"
            )
            for ch in (sec.get("changed") or [])[:6]:
                for c in (ch.get("changes") or [])[:4]:
                    details.append(
                        f'{sec.get("label")} · {ch.get("key")} · {c.get("field")}：'
                        f'{c.get("old")} → {c.get("new")}'
                    )
        table = ('<div class="mpbk-scroll"><table class="mpbk-table"><thead><tr>'
                 '<th>分类</th><th>新增</th><th>删除</th><th>字段变化</th></tr></thead><tbody>'
                 + "".join(rows) + "</tbody></table></div>")
        detail_html = ""
        if details:
            detail_html = ('<div class="mpbk-pre" style="margin-top:10px">'
                           + _e("\n".join(details[:30])) + "</div>")
        body = ('<div class="mpbk-row" style="border-bottom:0;padding-top:0">'
                f'<span class="mpbk-label">对比 {_e(data.get("diff_a"))} → {_e(data.get("diff_b"))}</span>'
                f'<b>{_e(diff.get("summary"))}</b></div>'
                + table + detail_html)
    return ('<div class="mpbk-section"><div class="mpbk-card">'
            '<div class="mpbk-sec-title">最近对比</div>' + body + "</div></div>")


def _guide_block(data: Dict[str, Any]) -> str:
    guide = data.get("guide")
    if guide:
        body = '<div class="mpbk-pre">' + _e(guide) + "</div>"
    else:
        body = ('<div class="mpbk-empty">还没生成恢复清单。点上面的「恢复清单」会得到一份 '
                'restore_*.md，逐项写明每个 JSON 的用途和恢复顺序，可复制保存。</div>')
    return ('<div class="mpbk-section"><div class="mpbk-card">'
            '<div class="mpbk-sec-title">恢复清单</div>' + body + "</div></div>")


def _import_notice_block(data: Dict[str, Any]) -> str:
    text = data.get("import_guide") or _IMPORT_NOTICE
    flag = ""
    if data.get("contains_secrets"):
        flag = ('<div class="mpbk-row" style="border-bottom:0;padding-top:0">'
                '<span class="mpbk-label">最近这份快照</span>'
                '<span class="mpbk-tag err">含明文凭据，别外传</span></div>')
    return ('<div class="mpbk-section"><div class="mpbk-card">'
            '<div class="mpbk-sec-title">导入迁移'
            '<span class="mpbk-sec-note">预览 → 确认 → 写回</span></div>'
            + flag
            + f'<div class="mpbk-note">{_e(text)}</div></div></div>')


def _status_tag(status: Any) -> str:
    text = str(status or "")
    tone = "ok" if text == "ok" else ("err" if text in ("error", "unavailable") else "info")
    return f'<span class="mpbk-tag {tone}">{_e(text)}</span>'


def _preview_block(data: Dict[str, Any]) -> str:
    preview = data.get("preview")
    if not preview:
        body = ('<div class="mpbk-empty">还没预览过。点快照行上的「预览导入」或上面的'
                '「预览最近快照」，这里会列出每类将新增、将跳过、将覆盖的条数。</div>')
    elif not preview.get("ok"):
        body = '<div class="mpbk-empty">' + _e(preview.get("message") or "预览失败") + "</div>"
    else:
        trs = "".join(
            "<tr>"
            f'<td>{_e(sec.get("label"))}</td>'
            f'<td>{_e(sec.get("total"))}</td>'
            f'<td>{_e(sec.get("current"))}</td>'
            f'<td><span class="mpbk-tag ok">{_e(sec.get("will_add"))}</span></td>'
            f'<td>{_e(sec.get("will_skip"))}</td>'
            f'<td>{_e(sec.get("will_overwrite"))}</td>'
            f'<td>{_status_tag(sec.get("status"))}</td>'
            "</tr>"
            for sec in (preview.get("sections") or [])
        )
        table = ('<div class="mpbk-scroll"><table class="mpbk-table"><thead><tr>'
                 '<th>分类</th><th>快照条数</th><th>现有条数</th><th>将新增</th>'
                 '<th>将跳过</th><th>将覆盖</th><th>状态</th></tr></thead><tbody>'
                 + trs + "</tbody></table></div>")
        secret = ""
        if preview.get("contains_secrets"):
            secret = ('<div class="mpbk-note" style="margin-top:8px">'
                      '<span class="mpbk-tag err">含明文凭据</span> 这份快照带站点凭据，保管好。</div>')
        body = ('<div class="mpbk-row" style="border-bottom:0;padding-top:0">'
                f'<span class="mpbk-label">预览 {_e(preview.get("file"))} · schema {_e(preview.get("schema"))}</span>'
                f'<b>{_e(preview.get("message"))}</b></div>'
                + table + secret)
    return ('<div class="mpbk-section"><div class="mpbk-card">'
            '<div class="mpbk-sec-title">导入预览'
            '<span class="mpbk-sec-note">只读，不改数据</span></div>'
            + body + "</div></div>")


def _restore_block(data: Dict[str, Any]) -> str:
    result = data.get("restore")
    if not result:
        body = ('<div class="mpbk-empty">还没跑过导入。默认试运行只列清单不写库；'
                '确认后点「执行导入」才会写回，结果和逐条日志会显示在这里。</div>')
    else:
        mode = _e(result.get("mode"))
        head = ('<div class="mpbk-row" style="border-bottom:0;padding-top:0">'
                f'<span class="mpbk-label">本次是<b>{mode}</b>'
                f' · 策略 {_e(result.get("strategy"))}</span>'
                f'<span class="mpbk-tag {"ok" if result.get("dry_run") else "warn"}">'
                f'{"没写库" if result.get("dry_run") else "已写回"}</span></div>')
        totals = result.get("totals") or {}
        summary = ("".join(
            f'<div class="mpbk-row"><span class="mpbk-label">{_e(label)}</span>'
            f'<b>{_e(totals.get(key, 0))}</b></div>'
            for key, label in (("planned", "将处理"), ("added", "新增"),
                               ("skipped", "跳过"), ("overwritten", "覆盖"),
                               ("failed", "失败"))
        ))
        logs = result.get("log") or []
        log_html = ('<div class="mpbk-pre mpbk-log" style="margin-top:10px">'
                    + _e("\n".join(str(item) for item in logs[:400])) + "</div>") if logs else ""
        body = (f'<div class="mpbk-note" style="margin-bottom:8px">{_e(result.get("message"))}</div>'
                + head + summary + log_html)
    return ('<div class="mpbk-section"><div class="mpbk-card">'
            '<div class="mpbk-sec-title">导入结果'
            '<span class="mpbk-sec-note">试运行 / 执行</span></div>'
            + body + "</div></div>")


def render(data: Dict[str, Any]) -> str:
    offline = bool(data.get("offline"))
    snapshots = data.get("snapshots") or []
    diff = data.get("diff")
    badge = '<span class="mpbk-badge">离线预览</span>' if offline else ""
    totals = (diff or {}).get("totals") or {}
    changed = totals.get("added", 0) + totals.get("removed", 0) + totals.get("changed", 0)
    hint = (diff or {}).get("summary") if diff else "还没有对比结果"
    kpis = [
        _kpi("快照份数", len(snapshots), "保存在备份目录", "box"),
        _kpi("占用空间", data.get("total_size_text") or "—", "所有快照合计", "disk"),
        _kpi("保留份数", data.get("keep", 7), "超出的旧快照会自动清掉", "clock"),
        _kpi("最近变更", changed, hint, "swap"),
    ]
    foot = ("备份默认脱敏；导入默认试运行，确认后才写回，不写回时零风险。"
            "含凭据的快照含明文，别外传，用完及时清掉。")
    return "".join([
        _STYLE,
        '<div class="mpbk">',
        '<div class="mpbk-head"><div class="mpbk-brand">',
        '<div class="mpbk-logo"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.9" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M4 8h13m0 0-3-3m3 3-3 3"/><path d="M20 16H7m0 0 3-3m-3 3 3 3"/></svg></div>',
        f'<div><div class="mpbk-title">MP 备份迁移{badge}</div>'
        '<div class="mpbk-sub">能读到的配置和记录都打包，也能预览后导入回来</div>',
        '</div><div class="mpbk-actions">',
        f'<span class="mpbk-time">更新于 {_e(data.get("generated_at"))}</span>',
        '<span class="mpbk-pill">可导出 · 可导入</span>',
        '</div></div>',
        '<div class="mpbk-grid">' + "".join(kpis) + "</div>",
        _status_block(data),
        _snapshots_block(data),
        _import_notice_block(data),
        _preview_block(data),
        _restore_block(data),
        _diff_block(data),
        _guide_block(data),
        f'<div class="mpbk-foot">{_e(foot)}</div></div>',
    ])
