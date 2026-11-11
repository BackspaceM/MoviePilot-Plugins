"""媒体库体检 —— 统一 HTML 渲染层。

沿用仓库「MP 总览」的视觉语言：lc- 前缀作用域、半透明灰自动适配深浅主题、
蓝紫渐变 KPI、所有动态文本统一 html.escape。

当前视图输出**全量明细**（不截断前几条），另外两个视图在顶部给摘要；
操作日志常驻页面并支持一键复制。交互只用固定 inline JS，不拼接动态路径。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Sequence

_STYLE = """
<style>
.lc{color:inherit;font-family:inherit;line-height:1.55;font-size:14px}
.lc *{box-sizing:border-box}
.lc-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:2px 0 16px}
.lc-brand{display:flex;align-items:center;gap:12px;min-width:0}
.lc-logo{width:42px;height:42px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#4F46E5,#7C3AED);box-shadow:0 6px 18px rgba(99,102,241,.32)}
.lc-logo svg{width:23px;height:23px}
.lc-title{font-size:19px;font-weight:700;letter-spacing:.3px}
.lc-sub{font-size:12px;opacity:.62;margin-top:1px}
.lc-time{font-size:12px;opacity:.58;white-space:nowrap}
.lc-pill{font-size:11px;font-weight:600;padding:4px 10px;border-radius:8px;
  background:rgba(99,102,241,.13);color:#8B8BF5;white-space:nowrap}
.lc-pill-safe{background:rgba(16,185,129,.15);color:#34D399}
.lc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:11px;margin-bottom:16px}
.lc-card{background:rgba(127,127,127,.075);border:1px solid rgba(127,127,127,.16);
  border-radius:14px;padding:13px 14px;min-width:0}
.lc-label{font-size:12px;opacity:.66;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lc-num{font-size:26px;font-weight:750;line-height:1.2;letter-spacing:-.5px;margin-top:6px;
  background:linear-gradient(135deg,#6366F1,#A855F7);-webkit-background-clip:text;background-clip:text;color:transparent}
.lc-hint{font-size:11px;opacity:.52;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lc-section{margin-bottom:16px}
.lc-sec-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:650;margin:0 0 9px}
.lc-sec-title:before{content:'';width:3px;height:13px;border-radius:3px;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.lc-sec-note{font-size:11px;font-weight:400;opacity:.5;margin-left:auto;text-align:right}
.lc-alert{display:flex;align-items:flex-start;gap:9px;padding:10px 12px;border-radius:10px;font-size:12px;
  margin-bottom:9px;background:rgba(127,127,127,.08);border:1px solid rgba(127,127,127,.14)}
.lc-alert-warn{background:rgba(245,158,11,.10);border-color:rgba(245,158,11,.28)}
.lc-alert-info{background:rgba(99,102,241,.09);border-color:rgba(99,102,241,.22)}
.lc-table-wrap{border:1px solid rgba(127,127,127,.16);border-radius:12px;overflow:auto;max-height:520px}
.lc-table{width:100%;border-collapse:collapse;font-size:12px}
.lc-table th{position:sticky;top:0;z-index:1;background:rgba(127,127,127,.14);backdrop-filter:blur(6px);
  text-align:left;font-weight:650;padding:9px 10px;white-space:nowrap;border-bottom:1px solid rgba(127,127,127,.18)}
.lc-table td{padding:8px 10px;border-bottom:1px solid rgba(127,127,127,.10);vertical-align:top;word-break:break-all}
.lc-table tr:last-child td{border-bottom:0}
.lc-table tr:hover td{background:rgba(99,102,241,.05)}
.lc-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;opacity:.86}
.lc-tag{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:6px;
  white-space:nowrap;background:rgba(99,102,241,.16);color:#8B8BF5}
.lc-tag-high{background:rgba(239,68,68,.16);color:#F87171}
.lc-tag-medium{background:rgba(245,158,11,.16);color:#FBBF24}
.lc-tag-low{background:rgba(59,130,246,.15);color:#60A5FA}
.lc-tag-ok{background:rgba(16,185,129,.15);color:#34D399}
.lc-tag-grey{background:rgba(127,127,127,.16);opacity:.85}
.lc-risk{font-size:11px;opacity:.8;line-height:1.6}
.lc-risk div{margin-bottom:2px}
.lc-copy{font-size:11px;padding:4px 10px;border-radius:7px;border:1px solid rgba(127,127,127,.28);
  background:rgba(127,127,127,.08);color:inherit;cursor:pointer}
.lc-copy:hover{background:rgba(99,102,241,.14)}
.lc-log{background:rgba(127,127,127,.07);border:1px solid rgba(127,127,127,.14);
  border-radius:10px;padding:10px 12px;max-height:240px;overflow:auto;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;white-space:pre-wrap;word-break:break-all;margin:0}
.lc-foot{font-size:11px;opacity:.48;margin-top:14px;line-height:1.75}
.lc-empty{font-size:12px;opacity:.55;padding:14px 0}
.lc-inline{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;margin-bottom:9px}
.lc-check{accent-color:#6366F1}
.lc-bar{height:6px;border-radius:99px;background:rgba(127,127,127,.17);overflow:hidden;margin-top:6px}
.lc-bar > i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#4F46E5,#7C3AED)}
</style>
"""

_LOGO_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 7.5 12 3l9 4.5-9 4.5-9-4.5Z"/>'
    '<path d="M3 12l9 4.5 9-4.5"/><path d="M3 16.5 12 21l9-4.5"/>'
    '</svg>'
)

_COPY_JS = (
    "var p=this.parentNode.querySelector('pre');var t=p?p.textContent:'';"
    "var self=this;var done=function(){self.textContent='已复制';};"
    "var fb=function(){var x=document.createElement('textarea');x.value=t;"
    "document.body.appendChild(x);x.select();document.execCommand('copy');"
    "document.body.removeChild(x);done();};"
    "var C=navigator.clipboard;if(C){if(C.writeText){C.writeText(t);done();}else{fb();}}else{fb();}"
)
_FILTER_JS = (
    "var b=this.checked;var s=this.closest('.lc-section');"
    "var r=s?s.querySelectorAll('tr[data-level]'):[];for(var i=0;i<r.length;i++){"
    "r[i].style.display=b?(r[i].getAttribute('data-level')==='ok'?'none':''):'';}"
)

LEVEL_LABEL = {"high": "高风险", "medium": "需留意", "low": "提示", "ok": "无风险"}
CATEGORY_LABEL = {
    "missing_in_some": "部分家缺集",
    "only_one": "仅一家存在",
    "multi_version": "多版本/重复",
    "single_source": "仅单家可核验",
    "consistent": "三端一致",
}


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _kpi(items: Sequence[tuple]) -> str:
    cells = []
    for label, value, hint in items:
        cells.append(
            f'<div class="lc-card"><div class="lc-label">{_e(label)}</div>'
            f'<div class="lc-num">{_e(value)}</div>'
            f'<div class="lc-hint">{_e(hint)}</div></div>'
        )
    return f'<div class="lc-grid">{"".join(cells)}</div>'


def _table(headers: Sequence[str], rows: Sequence[str], empty: str = "暂无数据") -> str:
    if not rows:
        return f'<div class="lc-empty">{_e(empty)}</div>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    return (
        '<div class="lc-table-wrap"><table class="lc-table"><thead><tr>'
        f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _section(title: str, note: str, body: str) -> str:
    return (
        f'<div class="lc-section"><div class="lc-sec-title">{_e(title)}'
        f'<span class="lc-sec-note">{_e(note)}</span></div>{body}</div>'
    )


def _alert(text: str, kind: str = "") -> str:
    cls = f"lc-alert {kind}".strip()
    return f'<div class="{cls}"><span>ℹ️</span><div>{text}</div></div>'


def _copy_block(label: str, text: str) -> str:
    return (
        f'<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin:8px 0 6px">'
        f'<span class="lc-label">{_e(label)}</span>'
        f'<button class="lc-copy" onclick="{_COPY_JS}">一键复制</button></div>'
        f'<pre class="lc-log">{_e(text)}</pre>'
    )


def _preview_view(a: Dict[str, Any]) -> str:
    s = a.get("summary") or {}
    parts = [
        _kpi([
            ("预演文件", s.get("total", 0), "只读扫描到的媒体文件"),
            ("高风险", s.get("high", 0), "可能覆盖已有入库文件"),
            ("需留意", s.get("medium", 0), "跨盘/不可见/重复入库"),
            ("目标冲突", s.get("conflict", 0), "目标同名已存在"),
            ("跨盘退化", s.get("cross_device", 0), "硬链接会变成复制"),
            ("重复入库", s.get("duplicate", 0), "已有整理记录/同名"),
        ]),
        _alert(
            f"预演目录：{_e('、'.join(a.get('roots') or []) or '自动推断失败')}"
            f"（{'自动从整理历史推断' if a.get('roots_auto') else '设置里指定'}）"
            f" · 参考库目录：{_e('、'.join(a.get('library_dirs') or []) or '未核验')}"
            f" · 预计整理方式：{_e(a.get('mode_hint'))}",
            "lc-alert-info",
        ),
    ]
    if a.get("reason"):
        parts.append(_alert(_e(a["reason"]), "lc-alert-warn"))
    if a.get("truncated"):
        parts.append(_alert(
            f"文件数达到单次上限 {_e(a.get('max_files'))}，本次为部分结果；请缩小预演目录后再看。",
            "lc-alert-warn",
        ))
    parts.append(
        '<div class="lc-inline"><label class="lc-inline">'
        f'<input class="lc-check" type="checkbox" onclick="{_FILTER_JS}">'
        '只看有风险的文件</label>'
        '<span style="opacity:.55">全量明细已展开，表格可滚动</span></div>'
    )
    rows = []
    for f in a.get("files") or []:
        level = str(f.get("level") or "low")
        tag = f'<span class="lc-tag lc-tag-{_e(level)}">{_e(LEVEL_LABEL.get(level, level))}</span>'
        ident = " ".join(x for x in (f.get("title"), f.get("year"),
                                     f.get("season"), f.get("episode")) if x)
        risks = "".join(f"<div>· {_e(x)}</div>" for x in (f.get("risks") or []))
        if not risks:
            risks = "<div>· 未发现冲突或跨盘风险</div>"
        rows.append(
            f'<tr data-level="{_e(level)}">'
            f'<td><b>{_e(f.get("name"))}</b><div class="lc-mono">{_e(f.get("path"))}</div>'
            f'<div class="lc-mono">{_e(f.get("size_text"))} · {_e(ident)}</div></td>'
            f'<td class="lc-mono">{_e(f.get("predicted_path"))}</td>'
            f'<td>{tag}<div class="lc-mono" style="margin-top:4px">'
            f'目标目录 {_e(f.get("target_dir"))}</div></td>'
            f'<td class="lc-risk">{risks}</td></tr>'
        )
    parts.append(_section(
        "A · 整理预演明细（全量）",
        f"共 {len(rows)} 个文件 · 按风险排序 · 未改动任何文件",
        _table(["源文件 / 识别结果", "保守预测目标路径", "等级 / 目标目录", "风险与说明"], rows,
               "没有可预演的媒体文件"),
    ))
    parts.append(_alert(_e(a.get("note") or "")))
    return "".join(parts)


def _ledger_view(b: Dict[str, Any]) -> str:
    s = b.get("summary") or {}
    parts = [
        _kpi([
            ("历史记录", s.get("total", 0), "分页读取的整理历史"),
            ("真硬链接", s.get("hardlink_ok", 0), "同设备同 inode"),
            ("退化复制", s.get("degraded_copy", 0), "声明 link 但 inode 不同"),
            ("孤儿库文件", s.get("orphan", 0), "源已删 · 库仍在"),
            ("应删未删", s.get("src_leftover", 0), "源在 · 目标缺失"),
            ("未核验", s.get("unverified", 0), "远程/不可见路径"),
            ("外部登记缺失", s.get("external_gap", 0), "库里有 · 历史里没"),
            ("剧集组", s.get("groups", 0), "按标题/季聚合"),
        ]),
    ]
    if b.get("reason"):
        parts.append(_alert(_e(b["reason"]), "lc-alert-warn"))

    group_rows = []
    for g in b.get("groups") or []:
        statuses = "".join(
            f'<span class="lc-tag lc-tag-grey" style="margin:1px 3px 1px 0">{_e(k)} {v}</span>'
            for k, v in (g.get("statuses") or {}).items()
        )
        group_rows.append(
            f'<tr><td><b>{_e(g.get("title"))}</b> {_e(g.get("season"))}</td>'
            f'<td>{_e(g.get("count"))}</td><td>{statuses}</td>'
            f'<td class="lc-mono">{_e(" ⟷ ".join((g.get("src") or [])[:2]) or "—")}<br>'
            f'{_e(" ⟷ ".join((g.get("dest") or [])[:2]) or "—")}</td></tr>'
        )
    parts.append(_section(
        "B · src ↔ dest 链接拓扑（全量）", "按剧集 / 季聚合，列出每组的源与目标链条",
        _table(["标题 / 季", "记录数", "状态分布", "源 ⟷ 目标示例"], group_rows, "暂无拓扑分组"),
    ))

    rec_rows = []
    for r in b.get("records") or []:
        status = str(r.get("status") or "unverified")
        cls = {"degraded_copy": "high", "src_leftover": "high", "both_missing": "medium",
               "orphan": "low", "unverified": "grey", "hardlink_ok": "ok",
               "copy_ok": "low"}.get(status, "grey")
        notes = "".join(f"<div>· {_e(x)}</div>" for x in (r.get("notes") or []))
        rec_rows.append(
            f'<tr data-level="{_e("ok" if status in ("hardlink_ok", "copy_ok") else "low")}">'
            f'<td><b>{_e(r.get("title"))}</b> {_e(r.get("year"))} '
            f'{_e(r.get("season"))}{_e(r.get("episode"))}<div class="lc-mono">'
            f'#{_e(r.get("id"))} · {_e(r.get("date"))} · {_e(r.get("mode"))}</div></td>'
            f'<td class="lc-mono">{_e(r.get("src"))}<br><span style="opacity:.6">'
            f'{_e(r.get("src_state"))} · dev {_e(r.get("src_dev"))} · ino {_e(r.get("src_ino"))}</span></td>'
            f'<td class="lc-mono">{_e(r.get("dest"))}<br><span style="opacity:.6">'
            f'{_e(r.get("dest_state"))} · dev {_e(r.get("dest_dev"))} · ino {_e(r.get("dest_ino"))}</span></td>'
            f'<td><span class="lc-tag lc-tag-{cls}">{_e(r.get("status_label"))}</span>'
            f'<div class="lc-risk">{notes}</div></td></tr>'
        )
    parts.append(_section(
        "B · 入库账本明细（全量）", f"共 {len(rec_rows)} 条 · 按状态严重程度排序",
        _table(["媒体 / 记录", "源文件 src", "库文件 dest", "核验结论"], rec_rows, "暂无整理历史"),
    ))

    ext_rows = [
        f'<tr><td><b>{_e(x.get("name"))}</b><div class="lc-mono">{_e(x.get("path"))}</div></td>'
        f'<td>{_e(x.get("title"))} {_e(x.get("season"))}{_e(x.get("episode"))}</td>'
        f'<td><span class="lc-tag lc-tag-low">疑似手工入库</span></td></tr>'
        for x in (b.get("external") or [])
    ]
    parts.append(_section(
        "B · 疑似外部登记缺失（全量）",
        "库目录里有实体文件，但整理历史里找不到对应 dest；可能是手工 ln 入库，不代表异常"
        + ("（已截断）" if b.get("external_truncated") else ""),
        _table(["库目录文件", "识别结果", "说明"], ext_rows, "没有发现未登记的库文件"),
    ))
    parts.append(_alert(_e(b.get("note") or "")))
    return "".join(parts)


def _parity_view(c: Dict[str, Any]) -> str:
    s = c.get("summary") or {}
    parts = [
        _kpi([
            ("媒体身份", s.get("total", 0), "三库对齐后的唯一条目"),
            ("部分家缺集", s.get("missing_in_some", 0), "至少一家没有"),
            ("仅一家存在", s.get("only_one", 0), "其余两家都没有"),
            ("多版本/重复", s.get("multi_version", 0), "同家出现多个条目"),
            ("三端一致", s.get("consistent", 0), "所有可核验家都有"),
            ("可核验服务", f'{s.get("servers_ok", 0)}/{s.get("servers_total", 0)}', "连接成功才参与对比"),
        ]),
    ]
    if c.get("reason"):
        parts.append(_alert(_e(c["reason"]), "lc-alert-warn"))

    srv_cards = []
    for srv in c.get("servers") or []:
        state = ('<span class="lc-tag lc-tag-ok">已核验</span>' if srv.get("ok")
                 else '<span class="lc-tag lc-tag-high">未核验</span>')
        srv_cards.append(
            f'<tr><td><b>{_e(srv.get("name"))}</b></td><td>{_e(srv.get("type"))}</td>'
            f'<td class="lc-mono">{_e(srv.get("url_host"))}</td>'
            f'<td>{_e(srv.get("count"))}</td><td>{state}'
            f'<div class="lc-risk">{_e(srv.get("reason"))}</div></td></tr>'
        )
    parts.append(_section(
        "C · 媒体服务器连接状态", "只显示主机名，不暴露完整地址与密钥；失败的家不参与缺集判定",
        _table(["实例", "类型", "主机", "条目数", "状态"], srv_cards, "未发现已连接实例"),
    ))

    successful = [str(x) for x in (c.get("successful") or [])]
    headers = ["媒体身份", "年 / 季集", "类型"] + [f"在 {x}" for x in successful] + ["结论", "缺失实例"]
    rows = []
    for r in c.get("rows") or []:
        marks = []
        for name in successful:
            count = (r.get("in_servers") or {}).get(name, 0)
            marks.append(
                f'<span class="lc-tag {"lc-tag-ok" if count else "lc-tag-high"}">'
                f'{"有" if count else "缺"}{" ×" + str(count) if count > 1 else ""}</span>'
            )
        cat = str(r.get("category") or "consistent")
        cls = {"missing_in_some": "medium", "only_one": "high", "multi_version": "low",
               "single_source": "grey", "consistent": "ok"}.get(cat, "grey")
        rows.append(
            "<tr>"
            f'<td><b>{_e(r.get("title"))}</b><div class="lc-mono">{_e(r.get("media_key") or r.get("key"))}</div></td>'
            f'<td>{_e(r.get("year"))} {_e(r.get("season"))}{_e(r.get("episode"))}</td>'
            f'<td>{_e("剧集" if r.get("kind") == "tv" else "电影")}</td>'
            + "".join(f"<td>{m}</td>" for m in marks)
            + f'<td><span class="lc-tag lc-tag-{cls}">{_e(CATEGORY_LABEL.get(cat, cat))}</span>'
            f'<div class="lc-risk">{_e("、".join(r.get("missing_in") or []) or "—")}</div></td>'
            f'<td class="lc-mono">{_e("、".join(r.get("missing_in") or []) or "—")}</td></tr>'
        )
    if not successful:
        headers = ["媒体身份", "年 / 季集", "类型", "结论", "缺失实例"]
    parts.append(_section(
        "C · 跨服差异矩阵（全量）",
        f"共 {len(rows)} 个媒体身份 · 可核验 {len(successful)} 家"
        + ("（已截断）" if s.get("truncated") else ""),
        _table(headers, rows, "没有可对齐的媒体条目"),
    ))
    parts.append(_alert(_e(c.get("note") or "")))
    return "".join(parts)


def render(data: Dict[str, Any], compact: bool = False) -> str:
    a = data.get("preview") or {}
    b = data.get("ledger") or {}
    c = data.get("parity") or {}
    view = str(data.get("view") or "preview")
    sa, sb, sc = a.get("summary") or {}, b.get("summary") or {}, c.get("summary") or {}

    head = (
        f'<div class="lc-head"><div class="lc-brand"><div class="lc-logo">{_LOGO_SVG}</div>'
        f'<div><div class="lc-title">媒体库体检</div>'
        f'<div class="lc-sub">A 整理预演 · B 入库账本 · C 三家一致性对账</div></div></div>'
        f'<div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">'
        f'<span class="lc-pill lc-pill-safe">全程只读</span>'
        f'<span class="lc-time">{_e(data.get("generated_at"))}</span></div></div>'
    )
    overview = _kpi([
        ("A · 预演文件", sa.get("total", 0), f"高风险 {sa.get('high', 0)} · 跨盘 {sa.get('cross_device', 0)}"),
        ("B · 历史记录", sb.get("total", 0), f"退化复制 {sb.get('degraded_copy', 0)} · 未核验 {sb.get('unverified', 0)}"),
        ("C · 媒体身份", sc.get("total", 0), f"缺集 {sc.get('missing_in_some', 0)} · 可核验 {sc.get('servers_ok', 0)} 家"),
    ])
    if compact:
        return (
            f'<div class="lc">{_STYLE}{head}{overview}'
            f'<div class="lc-foot">{_e(data.get("generated_at"))} · 插件详情页可切换 A / B / C 三个视图查看全量明细</div></div>'
        )

    view_label = {"preview": "A · 整理预演沙盘", "ledger": "B · 入库账本与链接拓扑",
                  "parity": "C · Emby / Jellyfin / Plex 一致性对账"}.get(view, "A · 整理预演沙盘")
    payloads = {"preview": a, "ledger": b, "parity": c}
    body = {"preview": _preview_view, "ledger": _ledger_view, "parity": _parity_view}.get(
        view, _preview_view
    )(payloads.get(view, a))

    switch_note = _alert(
        "当前视图：<b>" + _e(view_label) + "</b>。点页面上方三个按钮切换 A / B / C，"
        "切换后当前视图会输出全量明细；顶部 KPI 始终是三视图汇总。", "lc-alert-info",
    )
    notes = "".join(f"<div>· {_e(x)}</div>" for x in (data.get("notes") or []))
    log_text = "\n".join(str(x) for x in (data.get("log") or []))
    return (
        f'<div class="lc">{_STYLE}{head}{overview}{switch_note}{body}'
        + _section(
            "操作日志（可复制）",
            "同时写入宿主插件日志 logs/plugins/librarycheckup.log，插件卡片点「日志」也能看到",
            _copy_block("最近 200 条", log_text or "暂无日志"),
        )
        + f'<div class="lc-foot">{notes}'
        f'<div style="margin-top:6px">整理历史 {_e((data.get("histories") or {}).get("total"))} 条 · '
        f'推断下载目录 {_e(len((data.get("dirs") or {}).get("download") or []))} 个 · '
        f'媒体库目录 {_e(len((data.get("dirs") or {}).get("library") or []))} 个 · '
        f'已连接媒体服务器 {_e(data.get("servers_found"))} 家</div></div></div>'
    )
