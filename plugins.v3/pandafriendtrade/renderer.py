"""熊猫好友买卖页面层：配置表单 + 静态状态卡。

只做 JSON 组件与 HTML 拼装：不读宿主数据、不联网、不落盘。
get_form / get_page 渲染期零 IO，异常不会让前端报「数据加载失败」。
"""

from __future__ import annotations

import html
from typing import Any

from . import core

PLUGIN_ID = "PandaFriendTrade"


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _field(component: str, model: str, label: str, md: int = 6, **props: Any) -> dict:
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": md},
        "content": [{"component": component, "props": {"model": model, "label": label, **props}}],
    }


def _row(*columns: dict) -> dict:
    return {"component": "VRow", "content": list(columns)}


def _alert(text: str, kind: str = "info") -> dict:
    return _row({
        "component": "VCol",
        "props": {"cols": 12},
        "content": [{"component": "VAlert", "props": {"type": kind, "variant": "tonal"}, "text": text}],
    })


DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "dry_run": True,
    "notify": False,
    "cookie": "",
    "site": core.DEFAULT_SITE,
    "timeout": 30,
    "request_interval": 1.5,
    "max_actions": 60,
    "do_income": True,
    "do_work": False,
    "do_interact": False,
    "do_initiative": False,
    "do_tasks": True,
    "do_achievements": False,
    "do_level_posters": False,
    "do_commission": False,
    "do_settle": True,
    "do_posters": False,
    "do_protect": False,
    "do_buy": False,
    "do_release": False,
    "work_policy": "income",
    "interact_mode": "free",
    "interact_budget": 300,
    "protect_minutes": 120,
    "protect_budget": 10000,
    "buy_max_price": 0,
    "buy_min_overall": 0,
    "release_temporary": False,
    "commission_preference": "experience",
    "reserved_uids": "",
    "poster_gift_recipients": "",
    "poster_auto_redeem": False,
}


def form() -> tuple[list[dict], dict[str, Any]]:
    """配置页：分组清晰，默认只开零风险项。"""
    content = [
        _alert(
            "把熊猫PT「好友买卖」的重复操作按你的规则自动化。所有动作分为安全（领取/结算）、"
            "消耗魔力、不可逆（买卖/释放）三类；不可逆动作必须自己显式打开，默认全关。"
            "强烈建议先保持「试运行」开启，看一遍日志确认计划无误再关掉。",
            "warning",
        ),
        _row(
            _field("VSwitch", "enabled", "启用插件", 4),
            _field("VSwitch", "dry_run", "试运行（只统计不执行）", 4,
                   hint="开启时只产出计划写日志，不真正点按钮；确认无误再关掉", persistentHint=True),
            _field("VSwitch", "notify", "发送通知", 4),
        ),
        _alert("① 登录：Cookie 只保存在本机插件配置里，绝不写进代码或日志。失效后到站点重新复制一整串填进来。", "info"),
        _row(
            _field("VTextField", "site", "站点地址", 4, placeholder=core.DEFAULT_SITE),
            _field("VTextField", "timeout", "请求超时(秒)", 2),
            _field("VTextField", "request_interval", "请求间隔(秒)", 2,
                   hint="别调太小，避免触发站点风控", persistentHint=True),
            _field("VTextField", "max_actions", "每轮动作上限", 4),
        ),
        _row(
            _field("VTextarea", "cookie", "站点 Cookie", 12, rows=3,
                   placeholder="c_secure_uid=...; c_secure_pass=...; ...",
                   hint="从浏览器开发者工具复制完整 Cookie，包含所有 c_secure_* 项", persistentHint=True),
        ),
        _alert("② 零风险项（默认可开）：领取挂机收益、领成长任务奖励、结算已完成委托。这些不花魔力、不可逆交易。", "success"),
        _row(
            _field("VSwitch", "do_income", "领取挂机收益", 4),
            _field("VSwitch", "do_tasks", "领成长任务奖励", 4),
            _field("VSwitch", "do_settle", "结算已完成委托", 4),
        ),
        _row(
            _field("VSwitch", "do_achievements", "领成就奖励", 6),
            _field("VSwitch", "do_level_posters", "领等级海报", 6),
        ),
        _alert("③ 佣人互动 / 工作：工作不花魔力；互动可选免费或付费，付费上限由你设定。", "info"),
        _row(
            _field("VSwitch", "do_work", "自动安排工作", 3),
            _field("VSelect", "work_policy", "工作策略", 3,
                   items=[{"title": "收益优先", "value": "income"},
                          {"title": "关系优先", "value": "relation"},
                          {"title": "心情优先", "value": "rest"},
                          {"title": "按天赋属性", "value": "attribute"}]),
            _field("VSwitch", "do_interact", "自动互动", 3),
            _field("VSelect", "interact_mode", "互动预算", 3,
                   items=[{"title": "只用免费", "value": "free"},
                          {"title": "设定上限", "value": "limit"},
                          {"title": "不限制(谨慎)", "value": "unlimited"},
                          {"title": "关闭互动", "value": "off"}]),
        ),
        _row(
            _field("VTextField", "interact_budget", "单次互动魔力上限", 4,
                   hint="仅在「设定上限」模式生效；付费项：投喂100/小奖励300/深入交流1000", persistentHint=True),
            _field("VSwitch", "do_initiative", "自动主动互动(我作为佣人)", 4),
            _field("VTextarea", "reserved_uids", "保留佣人 UID（不参与任何自动操作）", 4, rows=2,
                   placeholder="12349, 107691", hint="逗号/空格/换行分隔", persistentHint=True),
        ),
        _alert("④ 事务所：站点已内置自动派遣时插件不会重复派单，只做结算；想让插件全权派遣请先到站点关掉原生助手。", "info"),
        _row(
            _field("VSwitch", "do_commission", "自动派出委托", 6,
                   hint="站点原生自动派遣开着时自动跳过派出", persistentHint=True),
            _field("VSelect", "commission_preference", "派遣偏好(仅记录)", 6,
                   items=[{"title": "经验优先", "value": "experience"},
                          {"title": "魔力优先", "value": "magic"},
                          {"title": "海报优先", "value": "posters"}]),
        ),
        _alert("⑤ 海报（电影图鉴）：领典藏任务奖励不花魔力；赠送不可逆、兑换按站点规则，默认关闭。", "info"),
        _row(
            _field("VSwitch", "do_posters", "自动领海报任务奖励", 4),
            _field("VSwitch", "poster_auto_redeem", "检查并兑换典藏册", 4),
            _field("VTextarea", "poster_gift_recipients", "允许赠送的好友 UID", 4, rows=2,
                   placeholder="只送给这些 UID，留空则不赠送",
                   hint="赠送不可逆，留空最安全", persistentHint=True),
        ),
        _alert("⑥ 消耗 / 不可逆：默认全关。打开前请想清楚——买人会真实花魔力，释放佣人不可撤回。", "error"),
        _row(
            _field("VSwitch", "do_protect", "自动买保护(消耗魔力)", 3),
            _field("VTextField", "protect_minutes", "保护剩余阈值(分钟)", 3),
            _field("VTextField", "protect_budget", "单次保护魔力上限", 3),
            _field("VSwitch", "do_buy", "自动买入/抢人(不可逆)", 3),
        ),
        _row(
            _field("VTextField", "buy_max_price", "买人价格上限(魔力)", 4,
                   hint="0 = 不买；只有详情实时价格在上限内才会买", persistentHint=True),
            _field("VTextField", "buy_min_overall", "买人属性均值下限", 4,
                   hint="0 = 不限；过滤低属性佣人", persistentHint=True),
            _field("VSwitch", "do_release", "启用释放(不可逆)", 4),
        ),
        _row(
            _field("VSwitch", "release_temporary", "自动释放临时佣人", 12,
                   hint="需同时打开「启用释放」；临时佣人到期会被释放，不可撤回", persistentHint=True),
        ),
        _alert("所有动作都会先产出计划并写进插件日志（卡片右上角「…」→ 查看日志）。试运行时只记录不执行。", "info"),
    ]
    return content, dict(DEFAULTS)


_STYLE = (
    '<style>'
    '.pf{border-radius:14px;padding:16px;color:#1f2933;'
    'background:linear-gradient(135deg,#f5f7ff 0%,#eef2ff 100%);'
    'font-family:system-ui,-apple-system,"PingFang SC",sans-serif}'
    '.pf-head{display:flex;align-items:center;gap:10px;margin-bottom:14px}'
    '.pf-logo{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;'
    'background:linear-gradient(135deg,#4F46E5,#7C3AED);color:#fff;font-size:20px}'
    '.pf-title{font-size:17px;font-weight:700;color:#1f2933}'
    '.pf-sub{font-size:12px;color:#6b7280;margin-top:2px}'
    '.pf-badge{margin-left:auto;border-radius:999px;padding:4px 12px;font-size:12px;'
    'background:#e5e7eb;color:#4b5563}'
    '.pf-badge.on{background:#dcfce7;color:#166534}'
    '.pf-sec{font-size:13px;font-weight:600;color:#4338ca;margin:14px 0 8px}'
    '.pf-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}'
    '.pf-cell{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px;text-align:center}'
    '.pf-k{font-size:12px;color:#6b7280}'
    '.pf-v{font-size:20px;font-weight:700;color:#1f2933;margin-top:4px}'
    '.pf-card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px 12px}'
    '.pf-line{display:flex;gap:10px;padding:5px 0;font-size:13px;border-bottom:1px solid #f3f4f6}'
    '.pf-line:last-child{border-bottom:none}'
    '.pf-line-k{color:#6b7280;min-width:84px}'
    '.pf-line-v{color:#1f2933;flex:1}'
    '.pf-note{font-size:12px;color:#4b5563;line-height:1.7}'
    '.pf-foot{font-size:11px;color:#9ca3af;margin-top:12px}'
    '</style>'
)


def _button(text: str, path: str, method: str, icon: str, color: str = "") -> dict:
    props = {"variant": "tonal", "prepend-icon": icon, "class": "ma-1"}
    if color:
        props["color"] = color
    return {
        "component": "VBtn",
        "props": props,
        "text": text,
        "events": {"click": {"api": f"plugin/{PLUGIN_ID}/{path}", "method": method, "params": {}}},
    }


def render_page(state: dict[str, Any]) -> list[dict]:
    data = state if isinstance(state, dict) else {}
    last = data.get("last") if isinstance(data.get("last"), dict) else {}
    probe = data.get("probe") if isinstance(data.get("probe"), dict) else {}
    running = bool(data.get("running"))

    buttons = [
        _button("运行一轮", "run", "post", "mdi-play", "primary"),
        _button("试运行", "dryrun", "post", "mdi-file-search-outline"),
        _button("检测 Cookie", "probe", "post", "mdi-account-check"),
        _button("查看最近计划", "preview", "post", "mdi-text-search"),
        _button("刷新状态", "status", "get", "mdi-refresh"),
    ]

    badge = '<span class="pf-badge on">正在运行</span>' if running else '<span class="pf-badge">空闲</span>'
    summary = last.get("summary") if isinstance(last.get("summary"), dict) else {}
    cells = "".join([
        f'<div class="pf-cell"><div class="pf-k">动作总数</div><div class="pf-v">{_e(summary.get("total", last.get("total", 0)))}</div></div>',
        f'<div class="pf-cell"><div class="pf-k">安全</div><div class="pf-v">{_e(summary.get("safe", 0))}</div></div>',
        f'<div class="pf-cell"><div class="pf-k">消耗魔力</div><div class="pf-v">{_e(summary.get("cost", 0))}</div></div>',
        f'<div class="pf-cell"><div class="pf-k">不可逆</div><div class="pf-v">{_e(summary.get("irreversible", 0))}</div></div>',
        f'<div class="pf-cell"><div class="pf-k">执行成功</div><div class="pf-v">{_e(last.get("done", 0))}</div></div>',
        f'<div class="pf-cell"><div class="pf-k">失败</div><div class="pf-v">{_e(last.get("failed", 0))}</div></div>',
    ])
    lines = "".join([
        f'<div class="pf-line"><span class="pf-line-k">运行状态</span><span class="pf-line-v">{_e(data.get("run_text") or "空闲")}</span></div>',
        f'<div class="pf-line"><span class="pf-line-k">最近一轮</span><span class="pf-line-v">{_e(data.get("last_text") or "还没跑过")}</span></div>',
        f'<div class="pf-line"><span class="pf-line-k">登录检测</span><span class="pf-line-v">{_e(data.get("probe_text") or "还没检测过")}</span></div>',
        f'<div class="pf-line"><span class="pf-line-k">试运行</span><span class="pf-line-v">{"开启：只产出计划不执行" if data.get("dry_run") else "关闭：确认规则后会真正执行"}</span></div>',
        f'<div class="pf-line"><span class="pf-line-k">运行方式</span><span class="pf-line-v">{_e(data.get("schedule_text") or "定时已禁用，用按钮手动运行")}</span></div>',
    ])
    previews = last.get("labels") or []
    preview_html = ""
    if isinstance(previews, list) and previews:
        items = "".join(f"<li>{_e(x)}</li>" for x in previews[:20])
        preview_html = f'<div class="pf-sec">最近计划明细（最多 20 条）</div><div class="pf-note"><ul>{items}</ul></div>'

    card = (
        '<div class="pf">' + _STYLE
        + '<div class="pf-head"><span class="pf-logo">🐼</span><span>'
        + '<div class="pf-title">熊猫好友买卖</div>'
        + '<div class="pf-sub">规则驱动的佣人互动 · 工作 · 事务所 · 海报自动化</div>'
        + '</span>' + badge + '</div>'
        + '<div class="pf-sec">最近一轮</div>'
        + f'<div class="pf-grid">{cells}</div>'
        + '<div class="pf-sec">运行状态</div>'
        + f'<div class="pf-card">{lines}</div>'
        + preview_html
        + '<div class="pf-sec">安全说明</div>'
        + '<div class="pf-note">不可逆动作（买入/抢人/追回/释放）默认全关，必须自己显式打开并设置价格与属性门槛。'
        + '站点已开启原生自动派遣时，插件不会重复派单，只结算已完成的委托。'
        + '每轮动作都写进插件卡片右上角「…」→ 查看日志，页面不显示日志。</div>'
        + '<div class="pf-foot">Cookie 仅保存在本机插件配置，不写入代码、日志或仓库。</div>'
        + '</div>'
    )
    return [
        {"component": "div", "content": buttons},
        {"component": "div", "html": card},
    ]
