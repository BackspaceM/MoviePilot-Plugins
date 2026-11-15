"""名称辅种 —— 运行日志与静态状态页。

日志只写宿主 logger，也就是插件卡片右上角「…」→ 查看日志能读到的那个文件；
插件数据表里不再存日志，页面也不显示日志。逐组搜索词和结果去查看日志里看。
"""

from __future__ import annotations

import html
import time
from typing import Any, Dict, List, Sequence

PLUGIN_ID = "NameCrossSeed"
SCAN_KEY = "scan"
MAX_LINES = 400
MAX_ROWS = 2000
SUMMARY_LIMIT = 5
_OK_STATES = frozenset({"added"})
_BAD_STATES = frozenset({"failed", "error"})

try:
    from app.sdk.logging import logger as _host_logger  # type: ignore
except Exception:  # noqa: BLE001 - 离线桩里退到标准库，方便本地跑用例
    try:
        import logging

        _host_logger = logging.getLogger("namecrossseed")
    except Exception:  # noqa: BLE001
        _host_logger = None

_LOG_LINES: List[str] = []

_STATE_LABEL = {
    "added": "已辅种", "exists": "已存在", "nomatch": "未匹配",
    "failed": "失败", "skipped": "跳过", "error": "异常",
    "cached": "近期已核对", "ignored": "不符合条件", "pending": "本轮未处理",
}


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def state_label(state: str) -> str:
    return _STATE_LABEL.get(state, state)


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def emit(plugin: Any, message: str, level: str = "info") -> None:
    """每条都交给宿主 logger，插件这边只留一份内存副本给 /log 用。"""
    text = str(message or "").strip()
    if not text:
        return
    _LOG_LINES.append(f"[{_stamp()}] {text}")
    if len(_LOG_LINES) > MAX_LINES:
        del _LOG_LINES[: len(_LOG_LINES) - MAX_LINES]
    if _host_logger is None:
        return
    try:
        method = getattr(_host_logger, "warning" if level in ("warning", "error") else "info")
        method(f"名称辅种：{text}")
    except Exception:  # noqa: BLE001
        pass


def clear(plugin: Any) -> None:
    _LOG_LINES.clear()


def log_lines(plugin: Any) -> List[str]:
    return list(_LOG_LINES[-MAX_LINES:])


def log_text(plugin: Any, limit: int = 300) -> str:
    return "\n".join(log_lines(plugin)[-max(1, int(limit)):])


def log_level_hint() -> str:
    """读不到有效级别就返回空串，别为了一句提示把页面搞挂。"""
    if _host_logger is None:
        return "读不到 MP 的日志组件，逐组日志可能写不进查看日志，升级 MoviePilot 后再试。"
    level = None
    try:
        from app.runtime import log as runtime_log

        getter = getattr(runtime_log, "_effective_context_log_level", None)
        if callable(getter):
            level = getter()
    except Exception:  # noqa: BLE001
        pass
    if not isinstance(level, int):
        try:
            getter = getattr(_host_logger, "_effective_context_log_level", None)
            level = getter() if callable(getter) else getattr(_host_logger, "level", None)
        except Exception:  # noqa: BLE001
            pass
    if not isinstance(level, int):
        try:
            import logging

            level = logging.getLogger().getEffectiveLevel()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(level, int) and 20 < level <= 50:  # INFO = 20
        return ("MP 的日志等级当前高于 INFO，逐组过程日志不会写进文件，查看日志里只剩警告和错误；"
                "到 设置 → 系统 → 日志等级 调回 INFO，或在插件实例管理里清除这个插件的等级覆盖。")
    return ""


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把名录压成「辅种成功 / 失败」两栏；站内已有单独计数不算成功。"""
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


_BRIEF_STYLE = """
<style>
.cs{color:inherit;font-family:inherit;font-size:13px;line-height:1.6}
.cs *{box-sizing:border-box}
.cs-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
  flex-wrap:wrap;margin:4px 0 12px}
.cs-brand{display:flex;align-items:center;gap:11px;min-width:0}
.cs-logo{width:42px;height:42px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#4F46E5,#7C3AED);box-shadow:0 6px 18px rgba(99,102,241,.32)}
.cs-logo svg{width:23px;height:23px}
.cs-title{font-size:17px;font-weight:700}
.cs-sub{font-size:11px;opacity:.6;margin-top:1px}
.cs-pill{font-size:11px;font-weight:700;padding:3px 9px;border-radius:7px;
  background:rgba(99,102,241,.14);color:#8B8BF5}
.cs-card{border:1px solid rgba(127,127,127,.16);border-radius:12px;
  background:rgba(127,127,127,.045);padding:8px 15px;margin-bottom:10px}
.cs-line{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
  padding:7px 0;border-bottom:1px dashed rgba(127,127,127,.16);font-size:12.5px}
.cs-line:last-child{border-bottom:0}
.cs-line span{opacity:.68}
.cs-line b{font-weight:700;font-size:13px}
.cs-note{font-size:12px;padding:9px 12px;border-radius:10px;line-height:1.7;
  background:rgba(127,127,127,.07);border:1px solid rgba(127,127,127,.15);margin-bottom:10px}
.cs-note b{font-weight:700;opacity:.75;margin-right:6px}
.cs-tip{font-size:12px;padding:9px 12px;border-radius:10px;line-height:1.7;margin-bottom:10px;
  background:rgba(245,158,11,.10);border:1px solid rgba(245,158,11,.28)}
.cs-foot{font-size:11px;opacity:.55;line-height:1.8}
@media (max-width:620px){.cs-line{padding:6px 0}}
</style>
"""

_LOGO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M9 7H7a4 4 0 0 0 0 8h2"/><path d="M15 7h2a4 4 0 0 1 0 8h-2"/>'
    '<path d="M8 12h8"/>'
    '</svg>'
)


def render_brief(stats: Dict[str, Any], running: bool = False) -> str:
    """静态状态页：打开时读一次 stats，不注入轮询脚本、不渲染日志和名录。"""
    stats = stats if isinstance(stats, dict) else {}
    rows = [
        ("上次运行", str(stats.get("last_run_text") or "还没跑过")),
        ("盘点 / 归并", f"{int(stats.get('last_scanned') or 0)} 个种子 → "
                   f"{int(stats.get('last_groups') or 0)} 组内容"),
        ("实际搜站", f"{int(stats.get('last_processed') or 0)} 组"),
        ("辅种成功", f"{int(stats.get('last_added') or 0)} 个"),
        ("站内已有", f"{int(stats.get('last_exists') or 0)} 组（不算成功）"),
        ("失败", f"{int(stats.get('last_failed') or 0)} 组"),
    ]
    body = "".join(
        f'<div class="cs-line"><span>{_e(label)}</span><b>{_e(value)}</b></div>'
        for label, value in rows
    )
    message = str(stats.get("last_message") or "").strip()
    if message and ("中止" in message or "停止" in message):
        message += "（这是上一轮的结果，当前已空闲，点「立即辅种」就能重跑）"
    note = (f'<div class="cs-note"><b>上一轮结果</b>{_e(message[:200])}</div>'
            if message else "")
    hint = log_level_hint()
    tip = f'<div class="cs-tip">{_e(hint)}</div>' if hint else ""
    return (
        f'<div class="cs">{_BRIEF_STYLE}'
        '<div class="cs-head"><div class="cs-brand"><div class="cs-logo">' + _LOGO + '</div>'
        '<div><div class="cs-title">名称辅种</div>'
        '<div class="cs-sub">同一份内容只搜一次、只加一次</div></div></div>'
        f'<span class="cs-pill">{"运行中" if running else "空闲"}</span></div>'
        f'<div class="cs-card">{body}</div>{note}{tip}'
        '<div class="cs-foot">这页只在打开时读一次状态，不显示日志也不自动刷新。'
        '逐组搜索词和每条结果都在插件卡片右上角「…」→ 查看日志里；'
        '如果那里只看到警告没有过程记录，是 MP 的日志级别被调高了，调回 INFO 即可。'
        '点上面的按钮会顺带刷新这里。</div></div>'
    )
