"""名称辅种 —— 运行日志与详情页渲染。

日志双写：写进插件数据（页面能实时看到，运行中每处理一条就落一次），
同时写宿主 logger（插件卡片点「日志」也能看到）。
页面上的自动刷新只用固定的 inline onload 脚本：v-html 插入的 <script> 不会执行，
但图片的 onload 会，因此用它启动一个 3 秒轮询，动态内容一律走 textContent。
"""

from __future__ import annotations

import html
import time
from typing import Any, Dict, List, Optional, Sequence

PLUGIN_ID = "NameCrossSeed"
LOG_KEY = "log"
SCAN_KEY = "scan"
MAX_LINES = 400
MAX_ROWS = 2000
POLL_MS = 3000
SUMMARY_LIMIT = 5  # 成功 / 失败两栏各显示最近几条
_OK_STATES = frozenset({"added"})
_BAD_STATES = frozenset({"failed", "error"})

try:
    from app.sdk.logging import logger as _host_logger  # type: ignore
except Exception:  # noqa: BLE001 - 离线 stub 环境
    _host_logger = None

_LOG_LINES: List[str] = []

_STATE_LABEL = {
    "added": "已辅种", "exists": "已存在", "nomatch": "未匹配",
    "failed": "失败", "skipped": "跳过", "error": "异常",
    "cached": "近期已核对", "ignored": "不符合条件", "pending": "本轮未处理",
}
_STATE_COLOR = {
    "added": "#16A34A", "exists": "#6366F1", "nomatch": "#64748B",
    "failed": "#DC2626", "skipped": "#64748B", "error": "#DC2626",
    "cached": "#64748B", "ignored": "#94A3B8", "pending": "#D97706",
}


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def state_label(state: str) -> str:
    return _STATE_LABEL.get(state, state)


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def emit(plugin: Any, message: str, level: str = "info") -> None:
    """追加一条运行日志；插件数据、内存镜像、宿主日志三处同步。"""
    text = str(message or "").strip()
    if not text:
        return
    line = f"[{_stamp()}] {text}"
    _LOG_LINES.append(line)
    if len(_LOG_LINES) > MAX_LINES:
        del _LOG_LINES[: len(_LOG_LINES) - MAX_LINES]
    try:
        plugin.save_data(LOG_KEY, list(_LOG_LINES[-MAX_LINES:]))
    except Exception:  # noqa: BLE001 - 持久化失败不能中断辅种
        pass
    if _host_logger is not None:
        try:
            getattr(_host_logger, "warning" if level in ("warning", "error") else "info")(
                f"名称辅种：{text}"
            )
        except Exception:  # noqa: BLE001
            pass


def clear(plugin: Any) -> None:
    _LOG_LINES.clear()
    try:
        plugin.save_data(LOG_KEY, [])
        plugin.save_data(SCAN_KEY, [])
    except Exception:  # noqa: BLE001
        pass


def log_lines(plugin: Any) -> List[str]:
    if _LOG_LINES:
        return list(_LOG_LINES)
    try:
        saved = plugin.get_data(LOG_KEY) or []
    except Exception:  # noqa: BLE001
        saved = []
    return [str(x) for x in saved][-MAX_LINES:]


def log_text(plugin: Any, limit: int = 300) -> str:
    return "\n".join(log_lines(plugin)[-max(1, int(limit)):])


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把本轮名录压成「辅种成功 / 失败」两栏，各留最近几条，其余只报总数。

    成功栏只放真正添加了的（added）；站点种子本来就在下载器里的（exists）
    不算成功，只单独计数。
    """
    ok: List[str] = []
    bad: List[str] = []
    exists_total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "")
        if state == "exists":
            exists_total += 1
            continue
        if state not in _OK_STATES and state not in _BAD_STATES:
            continue
        name = str(row.get("name") or "").strip() or "（未命名）"
        detail = " ".join(str(row.get("message") or "").split())
        line = f"{name} · {_STATE_LABEL.get(state, state)}"
        if detail:
            line += f" · {detail[:80]}"
        (ok if state in _OK_STATES else bad).append(line[:180])
    return {
        "ok": ok[-SUMMARY_LIMIT:][::-1],
        "bad": bad[-SUMMARY_LIMIT:][::-1],
        "ok_total": len(ok),
        "bad_total": len(bad),
        "exists_total": exists_total,
    }


def summary_text(items: Sequence[str], total: int) -> str:
    """两栏里的纯文本，轮询用 textContent 写入，不拼 HTML。"""
    if not items:
        return "暂无记录"
    rest = max(0, int(total) - len(items))
    text = "\n".join(str(x) for x in items)
    return text + (f"\n… 另有 {rest} 条，展开下方「本轮扫描名录」查看" if rest else "")


def scan_rows(plugin: Any) -> List[Dict[str, Any]]:
    try:
        rows = plugin.get_data(SCAN_KEY) or []
    except Exception:  # noqa: BLE001
        rows = []
    return [x for x in rows if isinstance(x, dict)][-MAX_ROWS:]


def progress(stats: Dict[str, Any], running: bool, as_html: bool = True) -> str:
    esc = _e if as_html else (lambda value: str(value if value is not None else ""))
    parts = [
        f"上次运行：{esc(stats.get('last_run_text') or '尚未运行')}",
        f"下载器种子 {int(stats.get('last_scanned') or 0)} 个",
        f"归并 {int(stats.get('last_groups') or 0)} 组内容",
        f"实际搜站 {int(stats.get('last_processed') or 0)} 组",
        f"辅种成功 {int(stats.get('last_added') or 0)}",
        f"站内已有 {int(stats.get('last_exists') or 0)}",
        f"失败 {int(stats.get('last_failed') or 0)}",
    ]
    text = "｜".join(parts)
    if running:
        text = "正在运行…｜" + text
    if stats.get("last_message"):
        text += "｜" + esc(str(stats["last_message"])[:120])
    return text


_COPY_JS = (
    "var p=document.getElementById('cs-log');var t=p?p.textContent:'';"
    "var self=this;var done=function(){self.textContent='已复制';};"
    "var fb=function(){var x=document.createElement('textarea');x.value=t;"
    "document.body.appendChild(x);x.select();document.execCommand('copy');"
    "document.body.removeChild(x);done();};"
    "var C=navigator.clipboard;if(C){if(C.writeText){C.writeText(t);done();}else{fb();}}else{fb();}"
)

_POLL_JS = (
    "if(!window.__csPoll){window.__csPoll=setInterval(function(){"
    "var A=window.MoviePilotAPI;if(!A){return;}if(!A.get){return;}"
    "A.get('plugin/" + PLUGIN_ID + "/log').then(function(r){"
    "var d=r;if(d){if(d.data){d=d.data;}}"
    "if(d){if(d.success!==undefined){if(d.data){d=d.data;}}}"
    "function put(id,t){var p=document.getElementById(id);"
    "if(p){if(p.textContent!==t){p.textContent=t;}}}"
    "if(d){"
    "put('cs-ok',d.ok_text||'');put('cs-bad',d.bad_text||'');"
    "put('cs-ok-count','共 '+(d.ok_total||0)+' 条 · 站内已有 '+(d.exists_total||0)+'（不算成功）');"
    "put('cs-bad-count','共 '+(d.bad_total||0)+' 条');"
    "put('cs-log',d.text||'');put('cs-state',d.progress||'');"
    "put('cs-run',d.running?'运行中':'空闲');"
    "}"
    "}).catch(function(){});},3000);}"
)

_STYLE = """
<style>
.cs{color:inherit;font-family:inherit;font-size:13px;line-height:1.6}
.cs *{box-sizing:border-box}
.cs-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin:4px 0 12px}
.cs-title{font-size:17px;font-weight:700}
.cs-state{font-size:12px;opacity:.72;margin-bottom:10px}
.cs-pill{font-size:11px;font-weight:700;padding:3px 9px;border-radius:7px;background:rgba(99,102,241,.14);color:#8B8BF5}
.cs-sec{font-size:13px;font-weight:650;margin:14px 0 8px;display:flex;align-items:center;gap:8px}
.cs-sec:before{content:'';width:3px;height:12px;border-radius:3px;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.cs-sec small{font-weight:400;opacity:.55;margin-left:auto}
.cs-log{background:rgba(127,127,127,.07);border:1px solid rgba(127,127,127,.15);border-radius:10px;
  padding:10px 12px;max-height:300px;overflow:auto;white-space:pre-wrap;word-break:break-all;margin:0;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;line-height:1.65}
.cs-copy{font-size:11px;padding:4px 10px;border-radius:7px;border:1px solid rgba(127,127,127,.28);
  background:rgba(127,127,127,.08);color:inherit;cursor:pointer}
.cs-wrap{border:1px solid rgba(127,127,127,.16);border-radius:10px;overflow:auto;max-height:420px}
.cs-table{width:100%;border-collapse:collapse;font-size:12px}
.cs-table th{position:sticky;top:0;background:rgba(127,127,127,.14);text-align:left;
  padding:8px 9px;font-weight:650;white-space:nowrap}
.cs-table td{padding:7px 9px;border-bottom:1px solid rgba(127,127,127,.10);vertical-align:top;word-break:break-all}
.cs-table tr:last-child td{border-bottom:0}
.cs-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;opacity:.8}
.cs-tag{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:6px;
  background:rgba(127,127,127,.16);white-space:nowrap}
.cs-foot{font-size:11px;opacity:.52;margin-top:12px;line-height:1.8}
.cs-empty{font-size:12px;opacity:.55;padding:10px 0}
.cs-cols{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cs-col{border:1px solid rgba(127,127,127,.16);border-radius:10px;overflow:hidden}
.cs-col-head{display:flex;align-items:center;gap:7px;padding:8px 10px;font-size:12px;
  font-weight:700;background:rgba(127,127,127,.08)}
.cs-col-head small{margin-left:auto;font-weight:400;opacity:.6;font-size:11px}
.cs-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}
.cs-dot-ok{background:#16A34A}
.cs-dot-bad{background:#DC2626}
.cs-col-body{margin:0;padding:9px 10px;min-height:96px;max-height:190px;overflow:auto;
  white-space:pre-wrap;word-break:break-all;font-size:11px;line-height:1.7;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:rgba(127,127,127,.05)}
.cs-fold{border:1px solid rgba(127,127,127,.16);border-radius:10px;margin-top:12px;padding:0 12px}
.cs-fold>summary{cursor:pointer;padding:9px 0;font-size:12px;font-weight:600;list-style:none}
.cs-fold>summary::-webkit-details-marker{display:none}
.cs-fold>summary:before{content:'▸';display:inline-block;margin-right:7px;opacity:.6}
.cs-fold[open]>summary:before{content:'▾'}
.cs-fold .cs-log{margin-bottom:12px;max-height:260px}
.cs-fold .cs-wrap{margin-bottom:12px;max-height:360px}
@media (max-width:620px){.cs-cols{grid-template-columns:1fr}}
</style>
"""


def _table(rows: Sequence[Dict[str, Any]], truncated: bool) -> str:
    if not rows:
        return '<div class="cs-empty">还没有扫描记录，点「立即辅种」跑一轮。</div>'
    body = []
    for row in rows:
        state = str(row.get("state") or "skipped")
        color = _STATE_COLOR.get(state, "#64748B")
        label = _STATE_LABEL.get(state, state)
        group_note = str(row.get("group_text") or "").strip()
        group_html = f'<div class="cs-mono">{_e(group_note)}</div>' if group_note else ""
        body.append(
            "<tr>"
            f'<td><b>{_e(row.get("name"))}</b><div class="cs-mono">{_e(row.get("downloader"))} · '
            f'{_e(row.get("size_text") or "—")}</div>{group_html}</td>'
            f'<td><span class="cs-tag" style="color:{color}">{_e(label)}</span></td>'
            f'<td>{_e(row.get("message") or "—")}</td></tr>'
        )
    note = "（本轮名录已达显示上限，仅显示最近部分）" if truncated else ""
    return (
        '<div class="cs-wrap"><table class="cs-table"><thead><tr>'
        "<th>下载器里的种子</th><th>结果</th><th>说明 / 站点种子</th>"
        f"</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        + (f'<div class="cs-foot">{_e(note)}</div>' if note else "")
    )


def render(
    stats: Dict[str, Any],
    log: str,
    rows: Sequence[Dict[str, Any]],
    running: bool = False,
    truncated: bool = False,
) -> str:
    """详情页 HTML：默认只给成功 / 失败两栏摘要，完整日志与全量名录折叠。"""
    summary = summarize(rows)
    head = (
        '<div class="cs-head"><div class="cs-title">名称辅种</div>'
        f'<span class="cs-pill" id="cs-run">{"运行中" if running else "空闲"}</span></div>'
    )
    state = f'<div class="cs-state" id="cs-state">{progress(stats, running)}</div>'
    cols_block = (
        f'<div class="cs-sec">本轮辅种成功<small>只列真正添加了的，最近 {SUMMARY_LIMIT} 条</small></div>'
        '<div class="cs-cols">'
        '<div class="cs-col"><div class="cs-col-head">'
        '<span class="cs-dot cs-dot-ok"></span>辅种成功'
        f'<small id="cs-ok-count">共 {summary["ok_total"]} 条 · 站内已有 {summary["exists_total"]}（不算成功）</small></div>'
        f'<pre class="cs-col-body" id="cs-ok">{_e(summary_text(summary["ok"], summary["ok_total"]))}</pre>'
        '</div>'
        '<div class="cs-col"><div class="cs-col-head">'
        '<span class="cs-dot cs-dot-bad"></span>失败'
        f'<small id="cs-bad-count">共 {summary["bad_total"]} 条</small></div>'
        f'<pre class="cs-col-body" id="cs-bad">{_e(summary_text(summary["bad"], summary["bad_total"]))}</pre>'
        '</div></div>'
    )
    log_block = (
        '<details class="cs-fold"><summary>完整运行日志（'
        f'{len(str(log or "").splitlines())} 行）</summary>'
        '<div style="display:flex;justify-content:flex-end;margin:2px 0 6px">'
        f'<button class="cs-copy" onclick="{_COPY_JS}">复制日志</button></div>'
        f'<pre class="cs-log" id="cs-log">{_e(log)}</pre></details>'
    )
    table_block = (
        f'<details class="cs-fold"><summary>本轮扫描名录（共 {len(rows)} 条）</summary>'
        + _table(rows, truncated) + '</details>'
    )
    foot = (
        '<div class="cs-foot">左栏只列本轮真正辅种成功的种子；站点种子本来就在下载器里的'
        '（站内已有）不算成功，只计个数。完整日志和全量名录点上面的标题展开，页面每 3 秒刷新一次。'
        '先把下载器里的种子按内容归并，同内容的多个辅种只搜一次站点、只添加一次；'
        '名称只用来找候选，最终要下载种子解析 info_hash 和文件清单，完全一致才会添加。'
        '想强制重扫一遍，点「清空缓存」后再运行。</div>'
    )
    # v-html 不会执行 <script>，用静态 data URI 图片的 onload 启动轮询
    poll = (
        '<img alt="" style="display:none" '
        'src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" '
        f'onload="{_POLL_JS}">'
    )
    return (f'<div class="cs">{_STYLE}{head}{state}{cols_block}'
            f'{log_block}{table_block}{foot}{poll}</div>')
