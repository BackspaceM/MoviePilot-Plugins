"""入库对账：把对账快照渲染成可塞进 v-html 的 HTML 片段。

约定：
- 所有动态文本统一在渲染层 html.escape，采集层只保留原始值；
- 明细、种子名称、路径清单、日志一律全量输出不截断，方便直接复制核对；
- 折叠用原生 <details>，主题通过 currentColor / rgba 自适应深浅主题；
- 内联脚本只使用固定代码 + 十六进制令牌，绝不把动态文本拼进 JS；
- compact=True 用于首页仪表盘，只保留 KPI、状态分布与关键提示。
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

_FILE_STATUS = {
    "residue": ("种子已删 · 源残留", "#7C6BF2"),
    "unregistered": ("无种子 · 无入库记录", "#E8833A"),
    "active": ("种子仍在", "#1D9E75"),
    "unverified": ("未核验", "#6B8DB5"),
}

_CSS = """
.taud{font-family:inherit;color:inherit;line-height:1.6;font-size:13px}
.taud *{box-sizing:border-box}
.taud-head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:10px;margin-bottom:16px}
.taud-title{font-size:17px;font-weight:600;letter-spacing:.2px;margin:0}
.taud-sub{font-size:12px;opacity:.6;margin:2px 0 0}
.taud-badge{font-size:11px;font-weight:600;padding:4px 10px;border-radius:999px;
  background:rgba(124,107,242,.12);color:#7C6BF2;border:1px solid rgba(124,107,242,.3);white-space:nowrap}
.taud-badge.danger{background:rgba(226,75,74,.12);color:#E24B4A;border-color:rgba(226,75,74,.35)}
.taud-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:18px}
.taud-kpi{border-radius:14px;padding:14px 16px;color:#fff;background:linear-gradient(135deg,#4F46E5,#8B5CF6);min-height:86px}
.taud-kpi.alt{background:rgba(127,127,127,.09);border:1px solid rgba(127,127,127,.18);color:inherit}
.taud-kpi .l{font-size:11px;opacity:.82;white-space:nowrap}
.taud-kpi .n{font-size:25px;font-weight:650;line-height:1.25;margin-top:4px}
.taud-kpi .d{font-size:11px;opacity:.72;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.taud-sec{font-size:13px;font-weight:600;margin:20px 0 10px;display:flex;flex-wrap:wrap;align-items:center;gap:8px}
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
.taud-table th{text-align:left;font-weight:600;opacity:.65;padding:7px 8px;border-bottom:1px solid rgba(127,127,127,.22);
  position:sticky;top:0;background:rgba(127,127,127,.10);backdrop-filter:blur(4px)}
.taud-table td{padding:7px 8px;border-bottom:1px solid rgba(127,127,127,.1);vertical-align:top;
  overflow-wrap:anywhere;word-break:break-all}
.taud-table tr:last-child td{border-bottom:none}
.taud-scroll{max-height:430px;overflow:auto;border-radius:12px;border:1px solid rgba(127,127,127,.16);
  background:rgba(127,127,127,.045)}
.taud-tag{font-size:10.5px;padding:2px 7px;border-radius:6px;color:#fff;white-space:nowrap;display:inline-block}
.taud-path{font-family:var(--font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:11px;opacity:.8}
.taud-pre{font-family:var(--font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:11px;
  line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;padding:12px 14px;
  border-radius:12px;background:rgba(127,127,127,.08);border:1px solid rgba(127,127,127,.18);max-height:320px;overflow:auto}
.taud-note{font-size:11.5px;opacity:.68;margin:8px 0 0;line-height:1.7}
.taud-empty{text-align:center;padding:22px 10px;opacity:.6;font-size:12.5px}
.taud-foot{margin-top:18px;padding-top:12px;border-top:1px solid rgba(127,127,127,.18);font-size:11px;opacity:.6}
.taud-copy{font:inherit;font-size:11px;font-weight:600;padding:4px 12px;border-radius:8px;cursor:pointer;
  border:1px solid rgba(127,127,127,.28);background:rgba(127,127,127,.09);color:inherit}
.taud-copy:hover{background:linear-gradient(135deg,#4F46E5,#8B5CF6);color:#fff;border-color:transparent}
.taud-fbtn{font:inherit;font-size:11px;padding:3px 10px;border-radius:999px;cursor:pointer;
  border:1px solid rgba(127,127,127,.26);background:rgba(127,127,127,.07);color:inherit}
.taud-fbtn.on{background:rgba(124,107,242,.16);border-color:rgba(124,107,242,.45);color:#7C6BF2;font-weight:600}
.taud-danger{border:1px solid rgba(226,75,74,.5);background:rgba(226,75,74,.09);
  border-radius:14px;padding:14px 16px;margin:14px 0;font-size:12px;line-height:1.8}
.taud-danger strong{color:#E24B4A;font-size:13px}
.taud-danger code{font-family:var(--font-mono,ui-monospace,SFMono-Regular,Menlo,monospace)}
.taud-delbtn{font:inherit;font-size:12px;font-weight:600;padding:8px 16px;border-radius:9px;cursor:pointer;
  border:1px solid rgba(226,75,74,.55);background:rgba(226,75,74,.14);color:#E24B4A}
.taud-delbtn:hover{background:#E24B4A;color:#fff}
.taud-tip{font-size:11.5px;margin-top:8px;white-space:pre-line;color:#7C6BF2}
.taud-sel{cursor:pointer;width:15px;height:15px;accent-color:#E24B4A}
"""

# 固定的复制脚本：只读取同一容器里的 <pre>，不拼任何动态文本。
_COPY_JS = (
    "var p=this.parentNode.querySelector('pre');"
    "var t=p?p.textContent:'';"
    "var C=navigator.clipboard;"
    "if(C){C.writeText(t);}else{"
    "var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);"
    "a.select();document.execCommand('copy');document.body.removeChild(a);}"
    "var b=this;b.textContent='已复制';"
    "setTimeout(function(){b.textContent='复制清单';},1500)"
)

# 固定的筛选脚本：按行上的 data-s 过滤，同样不拼动态文本。
_FILTER_JS = (
    "var n=this.parentNode,f=this.getAttribute('data-f');"
    "var r=document.querySelectorAll('.taud-file-row');"
    "for(var i=0;i<r.length;i++){var s=r[i].getAttribute('data-s');"
    "if(f==='all'){r[i].style.display='';}else{"
    "if(s===f){r[i].style.display='';}else{r[i].style.display='none';}}}"
    "var c=n.querySelectorAll('button');"
    "for(var j=0;j<c.length;j++){c[j].className='taud-fbtn';}"
    "this.className='taud-fbtn on'"
)


def _delete_js(token: str) -> str:
    """勾选删除：固定脚本 + 只含十六进制的令牌，绝不把路径/名称拼进 JS。"""
    safe_token = "".join(c for c in str(token or "") if c.isalnum())[:32]
    return (
        "var s=document.querySelectorAll('.taud-sel:checked'),ids=[],i;"
        "for(i=0;i<s.length;i++){ids.push(Number(s[i].value));}"
        "if(!ids.length){alert('请先勾选要删除的源文件');return;}"
        "var tip=document.getElementById('taudDelTip');"
        "if(!confirm('确认要为选中的 '+ids.length+' 个源文件生成删除预览吗？此步不会删除任何文件。')){return;}"
        "var A=window.MoviePilotAPI;"
        "if(!A){if(tip){tip.textContent='前端 API 不可用，请改用页面上方的「生成删除预览」按钮';}return;}"
        "A.post('plugin/TransferAudit/prepare',{token:'" + safe_token + "',indices:ids,scope:'selected'})"
        ".then(function(r){"
        "var d=null,ok=false;if(r){d=r.data;if(r.success){ok=true;}}"
        "if(!ok||!d||!d.plan_id){if(tip){tip.textContent=(r?r.message:'预览生成失败');}return;}"
        "var m=d.mode==='permanent'?'直接删除，不可恢复':'移入隔离目录，可恢复';"
        "if(!confirm('二次确认：即将处理 '+d.count+' 个源文件（'+d.size+'）。模式：'+m+'。确认后立即执行，取消则什么都不做。')){return;}"
        "A.post('plugin/TransferAudit/delete',{plan_id:d.plan_id,confirm:'DELETE_SOURCE_FILES'})"
        ".then(function(x){if(tip){tip.textContent=x?x.message:'已执行，请点重新扫描刷新';}})"
        ".catch(function(e){if(tip){tip.textContent='删除请求失败：'+(e?e.message:e);}});"
        "}).catch(function(e){if(tip){tip.textContent='预览请求失败：'+(e?e.message:e);}});"
    )


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


def _label(key: str) -> str:
    labels = {
        "orphan": "源已删", "leftover": "源残留", "missing": "库文件缺失", "stale": "双端已无",
        "remote": "未核验", "pending": "待核对", "healthy": "正常", "invalid": "记录不完整",
    }
    return labels.get(key, key)


def _group_block(group: Dict[str, Any]) -> str:
    title = _e(group.get("title") or "未识别媒体")
    year = _e(group.get("year") or "")
    season = _e(group.get("season") or "")
    head = title + (f" · {year}" if year else "") + (f" · {season}" if season else "")
    counts = group.get("counts") or {}
    chips = [f'<span class="taud-chip">{_num(group.get("total"))} 条记录</span>']
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


def _issues_table(issues: List[Dict[str, Any]]) -> str:
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
        seed_name = _e(item.get("torrent_title") or "—")
        rows.append(
            "<tr>"
            f'<td style="width:17%">{subject}</td>'
            f'<td style="width:17%">{seed_name}</td>'
            f'<td style="width:13%"><span class="taud-tag" style="background:{color}">'
            f'{_e(item.get("status_label") or key)}</span></td>'
            f'<td style="width:11%">{_e(item.get("seed_state"))}<br>'
            f'<span style="opacity:.6">{_e(item.get("mode"))}{" · 硬链接" if item.get("hardlink") else ""}</span></td>'
            f'<td style="width:21%"><span class="taud-path">{_e(item.get("src"))}</span></td>'
            f'<td style="width:21%"><span class="taud-path">{_e(item.get("dest"))}</span></td>'
            "</tr>"
        )
    return (
        '<div class="taud-scroll"><table class="taud-table"><thead><tr>'
        '<th>媒体 / 季集</th><th>种子名称（可复制核对）</th><th>状态</th><th>种子 / 方式</th>'
        '<th>源文件</th><th>媒体库文件</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    )


def _file_table(files: List[Dict[str, Any]], delete_enabled: bool) -> str:
    if not files:
        return (
            '<div class="taud-empty">没有扫描到媒体源文件。请在设置里填写「源目录（容器内可见路径）」，'
            "若下载器与 MoviePilot 的路径不一致，再补一条「路径映射」。</div>"
        )
    rows: List[str] = []
    for f in files:
        key = str(f.get("status") or "unverified")
        label, color = _FILE_STATUS.get(key, ("未核验", "#6B8DB5"))
        check = ""
        if delete_enabled and f.get("deletable"):
            check = (
                f'<input type="checkbox" class="taud-sel" value="{int(f.get("index") or 0)}" '
                'title="选中后可加入删除预览">'
            )
        known_note = "<br><span style='opacity:.6'>已登记整理历史</span>" if f.get("known") else ""
        rows.append(
            f'<tr class="taud-file-row" data-s="{_e(key)}">'
            f'<td style="width:4%">{check}</td>'
            f'<td style="width:20%">{_e(f.get("name"))}</td>'
            f'<td style="width:9%"><span class="taud-tag" style="background:{color}">{_e(label)}</span></td>'
            f'<td style="width:8%">{_e(f.get("size_text") or _num(f.get("size")))}</td>'
            f'<td style="width:59%"><span class="taud-path">{_e(f.get("path"))}</span>'
            + known_note + "</td></tr>"
        )
    filters = "".join(
        f'<button type="button" class="taud-fbtn{" on" if key == "all" else ""}" '
        f'data-f="{_e(key)}" onclick="{_FILTER_JS}">{_e(text)}</button>'
        for key, text in (
            ("all", "全部"), ("residue", "源残留"), ("unregistered", "无记录"),
            ("active", "做种中"), ("unverified", "未核验"),
        )
    )
    return (
        '<div class="taud-card" style="padding:8px 10px;margin-bottom:8px;display:flex;'
        'flex-wrap:wrap;gap:6px;align-items:center"><span style="font-size:11.5px;opacity:.7">筛选：</span>'
        + filters
        + "</div>"
        + '<div class="taud-scroll"><table class="taud-table"><thead><tr>'
        '<th>选</th><th>文件名</th><th>对比结果</th><th>大小</th><th>源文件路径</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    )


def _torrent_table(torrents: List[Dict[str, Any]]) -> str:
    if not torrents:
        return '<div class="taud-empty">未读取到下载器种子名录；请先连接下载器，或在设置里检查路径映射。</div>'
    rows: List[str] = []
    for t in torrents:
        rows.append(
            "<tr>"
            f'<td style="width:30%">{_e(t.get("title") or "—")}</td>'
            f'<td style="width:18%"><span class="taud-path">{_e(t.get("hash") or "—")}</span></td>'
            f'<td style="width:12%">{_e(t.get("state") or "—")}</td>'
            f'<td style="width:12%">{_e(t.get("downloader") or "—")}</td>'
            f'<td style="width:28%"><span class="taud-path">{_e(t.get("content_path") or "—")}</span></td>'
            "</tr>"
        )
    return (
        '<div class="taud-scroll"><table class="taud-table"><thead><tr>'
        '<th>种子名称</th><th>Hash</th><th>状态</th><th>下载器</th><th>内容路径</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    )


def _copy_block(title: str, text: str, hint: str, opened: bool = False) -> str:
    content = _e(text or "（暂无内容）")
    return (
        '<div class="taud-sec">' + _e(title) + f'<button type="button" class="taud-copy" onclick="{_COPY_JS}">复制清单</button></div>'
        f'<div><pre class="taud-pre">{content}</pre></div>'
        f'<div class="taud-note">{_e(hint)}</div>'
    )


def _danger_block(data: Dict[str, Any]) -> str:
    """仅在用户显式开启删除能力时渲染的高危提示 + 勾选删除入口。"""
    plan = data.get("plan") or {}
    mode = data.get("delete_mode") or "trash"
    mode_text = "直接删除（不可恢复）" if mode == "permanent" else "移入隔离目录（可恢复）"
    plan_text = ""
    if plan:
        plan_text = (
            f'<div class="taud-hint">已生成删除预览 <code>{_e(plan.get("id"))}</code>：'
            f'{_num(plan.get("count"))} 个源文件，合计 {_e(plan.get("size"))}，模式：{_e(mode_text)}。'
            "请点页面上方的「② 确认删除」按钮才会真正执行；也可重新生成预览。</div>"
        )
    items = plan.get("items") or []
    preview = "\n".join(f"{i + 1}. {x.get('path')}" for i, x in enumerate(items)) if items else "（暂无预览清单）"
    return (
        '<div class="taud-danger">'
        "<strong>⚠️ 此操作非常危险，可能导致不可逆的数据丢失！</strong><br>"
        "本操作会删除<strong>下载目录里的源文件</strong>（不是媒体库文件）。"
        f"当前模式：<code>{_e(mode_text)}</code>；每批最多 {_num(data.get('delete_batch'))} 个文件。<br>"
        "流程强制分三步：<b>① 扫描出完整列表 → ② 生成删除预览（不删任何文件） → ③ 二次确认后才执行</b>。"
        "下载器查不通、路径映射可疑、或任一条复检失败，整批都会自动拒绝。<br>"
        "取消、关闭页面、或什么都不点，都不会有任何文件被处理。"
        "</div>"
        + plan_text
        + (
            '<div class="taud-sec">删除预览清单</div><pre class="taud-pre">' + _e(preview) + "</pre>"
            if items else ""
        )
        + '<div style="margin-top:12px">'
        f'<button type="button" class="taud-delbtn" onclick="{_delete_js(data.get("scan_token"))}">'
        "删除勾选项（需两次确认）</button></div>"
        '<div class="taud-tip" id="taudDelTip">勾选上方「映射目录扫描结果」里带复选框的行，再点此按钮；'
        "默认只有「种子已删」且复检通过的文件才会出现复选框。</div>"
    )


def render(data: Dict[str, Any], compact: bool = False) -> str:
    """渲染完整或紧凑看板。所有明细类内容在完整视图里一律全量输出。"""
    counts = data.get("counts") or {}
    offline = bool(data.get("offline"))
    generated = _e(data.get("generated_at") or "—")
    delete_enabled = bool(data.get("delete_enabled"))

    badge = (
        '<span class="taud-badge danger">⚠️ 删除能力已开启 · 需二次确认</span>'
        if delete_enabled else
        '<span class="taud-badge">只读报告 · 不删除任何文件</span>'
    )
    head = (
        '<div class="taud-head"><div>'
        '<div class="taud-title">入库对账</div>'
        '<div class="taud-sub">源文件 · 整理历史 · 媒体库 · 下载器种子　|　更新于 '
        + generated
        + '</div></div>' + badge + '</div>'
    )

    scan = data.get("scan") or {}
    kpis = (
        '<div class="taud-kpis">'
        + _kpi("源已删 · 库仍在", counts.get("orphan"), "打包删种后的孤儿入库")
        + _kpi("源残留 · 种已删", counts.get("leftover"), "整理历史中的残留")
        + _kpi("目录扫描残留", len(data.get("candidates") or []), "种子名录比对出的可清理文件", alt=True)
        + _kpi("库文件缺失", counts.get("missing"), "目标文件已不在媒体库", alt=True)
        + _kpi("本次扫描", data.get("scanned"), f"整理历史共 {_num(data.get('total'))} 条", alt=True)
        + _kpi("下载器种子", data.get("torrent_total"), f"已连接 {_num(data.get('downloader_count'))} 台下载器", alt=True)
        + "</div>"
    )

    notes: List[str] = []
    offline_notice = ""
    if offline:
        # 即使整理历史接口不可用，下载器名录、映射目录扫描、清单与日志也必须照常展示，
        # 否则「点日志没内容」「看不全」的问题依旧存在。
        offline_notice = (
            '<div class="taud-card"><div class="taud-empty">'
            "当前无法读取整理历史（离线预览或宿主接口不可用），历史相关的分组与明细为空。<br>"
            "下载器种子名录、映射目录扫描结果、种子名称清单与操作日志仍照常显示。"
            "</div></div>"
        )
    if not data.get("seed_probed"):
        notes.append("未能核验下载器种子列表，所有「源库齐全」条目一律标为待核对，不推断种子已删。")
    if data.get("remote_count"):
        notes.append(f"{_num(data.get('remote_count'))} 条记录涉及远程存储或容器内不可见的路径，已标为未核验。")
    if data.get("truncated"):
        notes.append("整理历史较多，本次只扫描了前一批记录；结论为局部快照。")
    if data.get("linked_count"):
        notes.append(f"{_num(data.get('linked_count'))} 条记录的源与库文件仍是同一 inode（硬链接）。")
    if scan.get("truncated"):
        notes.append("映射目录文件过多，目录扫描为局部结果，请勿据此批量清理。")
    if delete_enabled:
        notes.append("删除能力已开启；所有删除都必须先生成预览并二次确认，默认移入隔离目录以便恢复。")
    else:
        notes.append("当前为只读模式，本插件不会删除、移动任何文件，也不会改动整理历史或下载器任务。")

    parts: List[str] = [
        f"<div class='taud'><style>{_CSS}</style>", head, kpis, offline_notice, _distribution(data),
    ]

    if compact:
        parts.append('<div class="taud-sec">扫描状态</div>')
        parts.append('<div class="taud-card" style="font-size:11.5px">'
                     + _e(scan.get("reason") or "—") + "</div>")
        groups = (data.get("groups") or [])[:5]
        if groups:
            parts.append('<div class="taud-sec">按剧集 / 季聚合</div>')
            parts.append("".join(_group_block(g) for g in groups))
        parts.append('<div class="taud-foot">' + "<br>".join(_e(n) for n in notes[:2]) + "</div>")
        parts.append("</div>")
        return "".join(parts)

    # ---- 完整视图 ----
    parts.append('<div class="taud-sec">扫描状态（下载器 + 映射目录）</div>')
    roots = scan.get("roots") or []
    root_text = "\n".join(roots) if roots else "（未配置 / 未推断出可用目录）"
    parts.append(
        '<div class="taud-card" style="font-size:11.5px;line-height:1.9">'
        f'目录文件 <b>{_num(data.get("file_total"))}</b> 个，被种子覆盖 <b>{_num(scan.get("matched"))}</b> 个，'
        f'种子名录 <b>{_num(data.get("torrent_total"))}</b> 条，'
        f'扫描校验：<b style="color:{"#1D9E75" if scan.get("ok") else "#E8833A"}">'
        f'{"通过" if scan.get("ok") else "未通过（删除已禁用）"}</b><br>'
        f'校验说明：{_e(scan.get("reason") or "—")}<br>'
        f'<span class="taud-path">扫描目录：{_e(root_text)}</span>'
        + (
            '<div class="taud-hint">若下载器返回的是宿主机路径（如 /volume1/download），'
            "而 MoviePilot 容器内看到的是 /downloads，请在设置里加一条路径映射 "
            "<code>/volume1/download=/downloads</code>；映射不对时插件会自动禁用删除。</div>"
            if not scan.get("ok") else ""
        )
        + "</div>"
    )

    if delete_enabled:
        parts.append(_danger_block(data))

    parts.append(_copy_block(
        "种子名称清单（全量 · 可复制到下载器核对）",
        "\n".join(data.get("seed_list") or []),
        f"共 {_num(len(data.get('seed_list') or []))} 个种子名称。复制后可直接粘到下载器搜索框，"
        "逐个确认种子是否真的已经删除；确认后再决定是否清理硬盘上的源文件。",
    ))
    parts.append(_copy_block(
        "完整核对清单（全量 · 不截断）",
        data.get("checklist") or "",
        f"共 {_num(data.get('checklist_total'))} 行，包含种子名称、hash、状态、源文件路径与媒体库路径。"
        "整理历史异常项与目录扫描残留都在这里，逐行可核对。",
    ))

    parts.append('<div class="taud-sec">映射目录扫描结果（全量 · '
                 + _num(data.get("file_total")) + " 个文件）</div>")
    parts.append(_file_table(data.get("files") or [], delete_enabled))

    parts.append('<div class="taud-sec">下载器种子名录（全量 · '
                 + _num(data.get("torrent_total")) + " 条）</div>")
    parts.append(_torrent_table(data.get("torrents") or []))

    groups = data.get("groups") or []
    parts.append('<div class="taud-sec">按剧集 / 季聚合（全量 · '
                 + _num(data.get("group_total")) + " 组）</div>")
    parts.append(
        "".join(_group_block(g) for g in groups)
        if groups else '<div class="taud-empty">暂无整理记录。</div>'
    )

    parts.append('<div class="taud-sec">整理历史需关注明细（全量 · '
                 + _num(data.get("issue_total")) + " 条）</div>")
    parts.append(_issues_table(data.get("issues") or []))

    paths = data.get("leftover_paths") or []
    if paths:
        parts.append('<div class="taud-sec">源残留路径清单（全量 · '
                     + _num(len(paths)) + " 条 · 可复制自行核对）</div>")
        parts.append('<div><pre class="taud-pre">' + _e("\n".join(paths)) + "</pre></div>")
        parts.append(
            '<div class="taud-note">以上路径均满足「源文件在、库文件在、下载器已无对应种子」。'
            "在只读模式下插件不会代为清理；如需由插件处理，请先在设置里开启删除能力。</div>"
        )

    parts.append(_copy_block(
        "操作日志（全量）",
        data.get("log") or "",
        "每次扫描、生成预览、删除与拒绝都会在这里留痕，同时写入 MoviePilot 的插件日志"
        "（logs/plugins/transferaudit.log），在插件卡片点「日志」也能看到。",
    ))

    parts.append('<div class="taud-foot">' + "<br>".join(_e(n) for n in notes) + "</div>")
    parts.append("</div>")
    return "".join(parts)
