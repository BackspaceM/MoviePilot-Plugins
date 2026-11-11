"""入库对账：把对账快照渲染成可塞进 v-html 的 HTML 片段。

约定：
- 所有动态文本统一在渲染层 html.escape，采集层只保留原始值；
- 不使用任何脚本，折叠用原生 <details>，主题通过 currentColor / rgba 自适应；
- compact=True 用于首页仪表盘，只保留 KPI、状态分布与前几个剧集组。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

_STATUS_COLORS = {
    "orphan": "#E8833A",
    "leftover": "#7C6BF2",
    "missing": "#E24B4A",
    "stale": "#888780",
    "remote": "#6B8DB5",
    "pending": "#378ADD",
    "healthy": "#1D9E75",
    "invalid": "#B4B2A9",
}

_CSS = """
.taud{font-family:inherit;color:inherit;line-height:1.6;font-size:13px}
.taud *{box-sizing:border-box}
.taud-head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:10px;margin-bottom:16px}
.taud-title{font-size:17px;font-weight:600;letter-spacing:.2px;margin:0}
.taud-sub{font-size:12px;opacity:.6;margin:2px 0 0}
.taud-badge{font-size:11px;font-weight:600;padding:4px 10px;border-radius:999px;
  background:rgba(124,107,242,.12);color:#7C6BF2;border:1px solid rgba(124,107,242,.3);white-space:nowrap}
.taud-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:18px}
.taud-kpi{border-radius:14px;padding:14px 16px;color:#fff;background:linear-gradient(135deg,#4F46E5,#8B5CF6);min-height:86px}
.taud-kpi.alt{background:rgba(127,127,127,.09);border:1px solid rgba(127,127,127,.18);color:inherit}
.taud-kpi .l{font-size:11px;opacity:.82;white-space:nowrap}
.taud-kpi .n{font-size:25px;font-weight:650;line-height:1.25;margin-top:4px}
.taud-kpi .d{font-size:11px;opacity:.72;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.taud-sec{font-size:13px;font-weight:600;margin:20px 0 10px;display:flex;align-items:center;gap:8px}
.taud-sec:before{content:"";width:3px;height:14px;border-radius:2px;background:linear-gradient(180deg,#4F46E5,#8B5CF6)}
.taud-card{background:rgba(127,127,127,.06);border:1px solid rgba(127,127,127,.16);
  border-radius:14px;padding:12px 14px;margin-bottom:10px}
.taud-bar{display:flex;height:10px;border-radius:999px;overflow:hidden;background:rgba(127,127,127,.14);margin-bottom:10px}
.taud-bar span{display:block;height:100%}
.taud-legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:11.5px;opacity:.85}
.taud-legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.taud-group{background:rgba(127,127,127,.055);border:1px solid rgba(127,127,127,.15);
  border-radius:12px;margin-bottom:8px;overflow:hidden}
.taud-group>summary{cursor:pointer;list-style:none;padding:10px 14px;display:flex;flex-wrap:wrap;
  align-items:center;gap:8px 12px;font-size:12.5px}
.taud-group>summary::-webkit-details-marker{display:none}
.taud-group>summary:hover{background:rgba(127,127,127,.07)}
.taud-gname{font-weight:600;flex:1;min-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.taud-chip{font-size:11px;padding:2px 8px;border-radius:999px;white-space:nowrap;
  border:1px solid rgba(127,127,127,.25);background:rgba(127,127,127,.08)}
.taud-chip.hot{color:#7C6BF2;border-color:rgba(124,107,242,.4);background:rgba(124,107,242,.1)}
.taud-chip.warn{color:#E8833A;border-color:rgba(232,131,58,.4);background:rgba(232,131,58,.1)}
.taud-chip.pack{color:#1D9E75;border-color:rgba(29,158,117,.4);background:rgba(29,158,117,.1)}
.taud-gbody{padding:2px 14px 12px;font-size:12px;opacity:.9}
.taud-hint{margin:8px 0 0;padding:8px 10px;border-radius:9px;font-size:11.5px;
  background:rgba(124,107,242,.1);color:#7C6BF2;border:1px solid rgba(124,107,242,.22)}
.taud-table{width:100%;border-collapse:collapse;font-size:11.5px;table-layout:fixed}
.taud-table th{text-align:left;font-weight:600;opacity:.65;padding:7px 8px;border-bottom:1px solid rgba(127,127,127,.22)}
.taud-table td{padding:7px 8px;border-bottom:1px solid rgba(127,127,127,.1);vertical-align:top;
  overflow-wrap:anywhere;word-break:break-all}
.taud-table tr:last-child td{border-bottom:none}
.taud-tag{font-size:10.5px;padding:2px 7px;border-radius:6px;color:#fff;white-space:nowrap;display:inline-block}
.taud-path{font-family:var(--font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:11px;opacity:.8}
.taud-pre{font-family:var(--font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:11px;
  line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;padding:12px 14px;
  border-radius:12px;background:rgba(127,127,127,.08);border:1px solid rgba(127,127,127,.18);max-height:230px;overflow:auto}
.taud-note{font-size:11.5px;opacity:.68;margin:8px 0 0;line-height:1.7}
.taud-empty{text-align:center;padding:22px 10px;opacity:.6;font-size:12.5px}
.taud-foot{margin-top:18px;padding-top:12px;border-top:1px solid rgba(127,127,127,.18);font-size:11px;opacity:.6}
"""


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _num(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def _kpi(label: str, number: Any, desc: str, alt: bool = False) -> str:
    return (
        f'<div class="taud-kpi{" alt" if alt else ""}">'
        f'<div class="l">{_e(label)}</div>'
        f'<div class="n">{_num(number)}</div>'
        f'<div class="d">{_e(desc)}</div></div>'
    )


def _distribution(data: Dict[str, Any]) -> str:
    counts = data.get("counts") or {}
    order = data.get("status_order") or []
    total = max(1, sum(int(counts.get(k, 0) or 0) for k in order))
    bars: List[str] = []
    legend: List[str] = []
    meta = data.get("status_meta") or {}
    for key in order:
        value = int(counts.get(key, 0) or 0)
        if not value:
            continue
        color = _STATUS_COLORS.get(key, "#888780")
        bars.append(
            f'<span style="width:{value * 100 / total:.2f}%;background:{color}" '
            f'title="{_e(meta.get(key, {}).get("label", key))}: {value}"></span>'
        )
        legend.append(
            f'<span><i style="background:{color}"></i>{_e(meta.get(key, {}).get("label", key))} {value}</span>'
        )
    return (
        '<div class="taud-card"><div class="taud-bar">'
        + "".join(bars)
        + '</div><div class="taud-legend">'
        + "".join(legend)
        + "</div></div>"
    )


def _group_block(group: Dict[str, Any]) -> str:
    title = _e(group.get("title") or "未识别媒体")
    year = _e(group.get("year") or "")
    season = _e(group.get("season") or "")
    head = title + (f" · {year}" if year else "") + (f" · {season}" if season else "")
    counts = group.get("counts") or {}
    chips = [
        f'<span class="taud-chip">{_num(group.get("total"))} 条记录</span>',
    ]
    for key in ("orphan", "leftover", "missing", "healthy"):
        value = int(counts.get(key, 0) or 0)
        if not value:
            continue
        cls = "hot" if key == "leftover" else ("warn" if key in ("orphan", "missing") else "")
        color = _STATUS_COLORS.get(key, "#888780")
        chips.append(
            f'<span class="taud-chip {cls}" style="border-color:{color}55">'
            f'{_e(_label(key))} {value}</span>'
        )
    if group.get("has_pack"):
        chips.append('<span class="taud-chip pack">疑似合集已入库</span>')

    body_bits = []
    for key in ("orphan", "leftover", "missing", "stale", "remote", "pending", "healthy"):
        value = int(counts.get(key, 0) or 0)
        if value:
            body_bits.append(f"{_e(_label(key))} {value} 条")
    hint = group.get("pack_hint") or ""
    hint_html = f'<div class="taud-hint">{_e(hint)}</div>' if hint else ""
    return (
        '<details class="taud-group"><summary>'
        f'<span class="taud-gname">{head}</span>'
        + "".join(chips)
        + '</summary><div class="taud-gbody">'
        + (" · ".join(body_bits) or "—")
        + hint_html
        + "</div></details>"
    )


def _label(key: str) -> str:
    labels = {
        "orphan": "源已删", "leftover": "源残留", "missing": "库文件缺失", "stale": "双端已无",
        "remote": "未核验", "pending": "待核对", "healthy": "正常", "invalid": "记录不完整",
    }
    return labels.get(key, key)


def _issues_table(issues: List[Dict[str, Any]], issue_total: int) -> str:
    if not issues:
        return '<div class="taud-empty">没有需要关注的记录，当前源与库是对齐的。</div>'
    rows: List[str] = []
    for item in issues:
        key = item.get("status") or "invalid"
        color = _STATUS_COLORS.get(key, "#888780")
        ep = item.get("episodes") or ""
        season = item.get("season") or ""
        name = item.get("title") or ""
        subject = _e(name + (f" {season}" if season else "") + (f" {ep}" if ep else ""))
        rows.append(
            "<tr>"
            f'<td style="width:20%">{subject}</td>'
            f'<td style="width:15%"><span class="taud-tag" style="background:{color}">'
            f'{_e(item.get("status_label") or key)}</span></td>'
            f'<td style="width:13%">{_e(item.get("seed_state"))}<br>'
            f'<span style="opacity:.6">{_e(item.get("mode"))}{" · 硬链接" if item.get("hardlink") else ""}</span></td>'
            f'<td style="width:26%"><span class="taud-path">{_e(item.get("src"))}</span></td>'
            f'<td style="width:26%"><span class="taud-path">{_e(item.get("dest"))}</span></td>'
            "</tr>"
        )
    more = ""
    if issue_total > len(issues):
        more = (
            f'<div class="taud-note">共 {_num(issue_total)} 条需关注记录，'
            f'此处仅展示前 {_num(len(issues))} 条；完整数据可通过 /snapshot 接口获取。</div>'
        )
    return (
        '<div class="taud-card" style="padding:4px 6px;overflow-x:auto">'
        '<table class="taud-table"><thead><tr>'
        '<th>媒体 / 季集</th><th>状态</th><th>种子 / 方式</th><th>源文件</th><th>媒体库文件</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>" + more
    )


def render(data: Dict[str, Any], compact: bool = False) -> str:
    """渲染完整或紧凑看板。"""
    counts = data.get("counts") or {}
    offline = bool(data.get("offline"))
    generated = _e(data.get("generated_at") or "—")

    head = (
        '<div class="taud-head"><div>'
        '<div class="taud-title">入库对账</div>'
        '<div class="taud-sub">源文件 · 整理历史 · 媒体库 · 下载器种子　|　更新于 '
        + generated
        + '</div></div><span class="taud-badge">只读报告 · 不删除任何文件</span></div>'
    )

    kpis = (
        '<div class="taud-kpis">'
        + _kpi("源已删 · 库仍在", counts.get("orphan"), "打包删种后的孤儿入库")
        + _kpi("源残留 · 种已删", counts.get("leftover"), "可核对清理的源文件")
        + _kpi("库文件缺失", counts.get("missing"), "目标文件已不在媒体库", alt=True)
        + _kpi("本次扫描", data.get("scanned"), f"整理历史共 {_num(data.get('total'))} 条", alt=True)
        + "</div>"
    )

    if offline:
        return (
            f"<div class='taud'><style>{_CSS}</style>" + head + kpis
            + '<div class="taud-card"><div class="taud-empty">'
            "当前无法读取整理历史（离线预览或宿主接口不可用）。<br>"
            "在 MoviePilot 中打开本插件详情页即可自动采集，全程只读。"
            "</div></div></div>"
        )

    notes: List[str] = []
    if not data.get("seed_probed"):
        notes.append("未能核验下载器种子列表，所有「源库齐全」条目一律标为待核对，不推断种子已删。")
    if data.get("remote_count"):
        notes.append(f"{_num(data.get('remote_count'))} 条记录涉及远程存储或容器内不可见的路径，已标为未核验。")
    if data.get("truncated"):
        notes.append("整理历史较多，本次只扫描了前一批记录；结论为局部快照。")
    if data.get("linked_count"):
        notes.append(f"{_num(data.get('linked_count'))} 条记录的源与库文件仍是同一 inode（硬链接）。")
    notes.append("本插件只生成报告，不会删除、移动任何文件，也不会改动整理历史或下载器任务。")

    groups: List[str] = []
    visible_groups = (data.get("groups") or [])[: (5 if compact else 20)]
    for group in visible_groups:
        groups.append(_group_block(group))
    group_more = ""
    if (data.get("group_total") or 0) > len(visible_groups):
        group_more = (
            f'<div class="taud-note">共 {_num(data.get("group_total"))} 个剧集/季分组，'
            f'此处展示需关注程度最高的 {_num(len(visible_groups))} 个。</div>'
        )

    parts: List[str] = [f"<div class='taud'><style>{_CSS}</style>", head, kpis, _distribution(data)]
    parts.append('<div class="taud-sec">按剧集 / 季聚合</div>')
    parts.append("".join(groups) if groups else '<div class="taud-empty">暂无整理记录。</div>')
    parts.append(group_more)

    if not compact:
        parts.append('<div class="taud-sec">需关注明细</div>')
        parts.append(_issues_table(data.get("issues") or [], int(data.get("issue_total") or 0)))
        paths = data.get("leftover_paths") or []
        if paths:
            parts.append('<div class="taud-sec">源残留路径清单（可复制自行核对）</div>')
            parts.append('<pre class="taud-pre">' + _e("\n".join(paths[:200])) + "</pre>")
            parts.append(
                '<div class="taud-note">以上路径均满足「源文件在、库文件在、下载器已无对应种子」。'
                "请自行确认后再决定是否清理；插件不会代为删除。</div>"
            )

    shown_notes = notes[:2] if compact else notes
    parts.append('<div class="taud-foot">' + "<br>".join(_e(n) for n in shown_notes) + "</div>")
    parts.append("</div>")
    return "".join(parts)
