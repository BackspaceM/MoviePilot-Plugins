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
        "content": [{"component": "VAlert", "props": {"type": kind, "variant": "tonal", "density": "compact"}, "text": text}],
    })


def _section(title: str, subtitle: str) -> dict:
    """醒目的分区标题条，让长配置页一眼能定位。"""
    return _row({
        "component": "VCol",
        "props": {"cols": 12},
        "content": [{
            "component": "div",
            "props": {"class": "pf-form-sec", "style": "margin:14px 0 6px;padding:10px 14px;border-radius:10px;"
                      "background:linear-gradient(135deg,#eef2ff,#f5f3ff);border-left:4px solid #6366f1"},
            "content": [
                {"component": "div", "props": {"style": "font-weight:700;font-size:15px;color:#3730a3"},
                 "text": f"{title}"},
                {"component": "div", "props": {"style": "font-size:12px;color:#6b7280;margin-top:2px"},
                 "text": subtitle},
            ],
        }],
    })


DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "dry_run": True,
    "notify": False,
    "cookie": "",
    "site": core.DEFAULT_SITE,
    "proxy_url": "",
    "proxy_bypass": True,
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
    "do_auction": False,
    "auction_auto_bid": False,
    "auction_budget": 0,
    "auction_total_budget": 0,
    "auction_snipe_seconds": 60,
    "auction_rarities": "",
    "auction_max_bids_per_lot": 3,
    "auction_min_gap": 60,
    "auction_require_no_leader": False,
    "auction_aggressive_steps": 0,
    "auction_poll_seconds": 15,
    "do_consignment": False,
    "consignment_auto_bid": False,
    "consignment_budget": 0,
    "consignment_unowned_only": True,
    "consignment_wanted_poster_ids": "",
    "consignment_rarities": "",
    "consignment_modes": "open,dutch",
    "poster_claim_rarities": "",
}


def form() -> tuple[list[dict], dict[str, Any]]:
    """配置页：分区清晰、敏感/高风险一眼可辨；默认只开零风险项。"""
    content = [
        _alert(
            "把熊猫PT「好友买卖」的重复操作按你的规则自动化。动作分三档：安全（领取/结算）、"
            "消耗魔力（互动/保护/拍卖）、不可逆（买卖/释放/赠送）。不可逆默认全关。"
            "建议先保持「试运行」，去日志确认计划再正式执行。",
            "warning",
        ),
        _row(
            _field("VSwitch", "enabled", "启用插件", 4),
            _field("VSwitch", "dry_run", "试运行（只出计划不执行）", 4,
                   hint="开启时只产出计划写日志，不真正点按钮；确认无误再关掉", persistentHint=True),
            _field("VSwitch", "notify", "发送通知", 4),
        ),

        _section("① 登录与网络", "Cookie 只存本机；代理按需开启，SOCKS 需容器装 PySocks"),
        _row(
            _field("VTextField", "site", "站点地址", 6, placeholder=core.DEFAULT_SITE),
            _field("VTextField", "timeout", "请求超时(秒)", 3),
            _field("VTextField", "max_actions", "每轮动作上限", 3),
        ),
        _row(
            _field("VTextField", "proxy_url", "代理地址（可选）", 8,
                   placeholder="http://host:port 或 socks5://user:pass@host:port",
                   hint="留空=直连；支持 http/https/socks5", persistentHint=True),
            _field("VSwitch", "proxy_bypass", "代理失败时降级直连", 4),
        ),
        _row(
            _field("VTextarea", "cookie", "站点 Cookie", 12, rows=3,
                   placeholder="c_secure_uid=...; c_secure_pass=...; ...",
                   hint="浏览器开发者工具复制完整 Cookie，含所有 c_secure_* 项", persistentHint=True),
        ),
        _alert("请求间隔在下方「高级」里调，默认 1.5 秒，别调太小以免触发站点风控。", "info"),

        _section("② 海报拍卖（重点）", "监控报价 · 倒计时抢拍 · 自动加价；最后 3 分钟换领先者会延长至 3 分钟",
                 ),
        _alert(
            "站点是「暗拍」：填的是你的最终出价，站点已算好最低有效价 minimum_bid（含领先者+一档增量），"
            "插件默认按这个价出，不会重复加价。出价后不可撤销；未中标本金全退、手续费不退。"
            "最后 3 分钟换领先者会把截止延长到 3 分钟（最多延长 30 分钟），所以进入窗口后可持续跟价。",
            "warning",
        ),
        _row(
            _field("VSwitch", "do_auction", "开启拍卖监控", 4,
                   hint="关闭则完全不碰拍卖；开启后按下面规则决定是否出价", persistentHint=True),
            _field("VSwitch", "auction_auto_bid", "自动出价抢海报", 4,
                   hint="关掉则只监控记录报价、不出价", persistentHint=True),
            _field("VSwitch", "auction_require_no_leader", "只抢无人出价的拍品", 4,
                   hint="开启后不与已有领先者竞价，更省钱", persistentHint=True),
        ),
        _row(
            _field("VTextField", "auction_budget", "单品出价上限(魔力)", 4,
                   hint="0=不出价；超过此价的拍品跳过", persistentHint=True),
            _field("VTextField", "auction_total_budget", "本轮总占用上限(魔力)", 4,
                   hint="0=不限；累计实际占用超过就停", persistentHint=True),
            _field("VTextField", "auction_snipe_seconds", "抢拍窗口(秒)", 4,
                   hint="距落槌≤N秒才出价；0=一发现就抢。设 60 适合最后1分钟跟价", persistentHint=True),
        ),
        _row(
            _field("VTextField", "auction_rarities", "只抢稀有度（留空=全部）", 6,
                   placeholder="rare, precious, legendary",
                   hint="可选 normal/excellent/rare/precious/legendary，逗号分隔", persistentHint=True),
            _field("VTextField", "auction_aggressive_steps", "额外加价档数(0=最低价)", 6,
                   hint="0=按站点最低有效价；每加1档多出一个增量，更激进也更费魔力", persistentHint=True),
        ),
        _row(
            _field("VTextField", "auction_max_bids_per_lot", "单品最多加价次数", 4),
            _field("VTextField", "auction_min_gap", "两次加价最小间隔(秒)", 4),
            _field("VTextField", "auction_poll_seconds", "监控轮询间隔(秒)", 4,
                   hint="要抢最后3分钟的延长，建议 ≤20 秒", persistentHint=True),
        ),

        _section("②b 海报寄售场", "个人寄售的市场：抢「未拥有/想要」的海报，荷兰降价可直接买"),
        _alert(
            "寄售场和光影拍卖行是两个独立市场，都归「启动/停止抢拍」这个监控循环管。"
            "市场看板会按「未拥有」过滤，避免重复买已有的海报；可以只盯指定海报或指定档位。",
            "info",
        ),
        _row(
            _field("VSwitch", "do_consignment", "开启寄售场监控", 4,
                   hint="关闭则完全不碰寄售场", persistentHint=True),
            _field("VSwitch", "consignment_auto_bid", "自动抢购", 4,
                   hint="dutch 降价直接买、open 竞价出价；关掉则只监控", persistentHint=True),
            _field("VSwitch", "consignment_unowned_only", "只要未拥有的海报", 4,
                   hint="默认开，避免重复买已有的", persistentHint=True),
        ),
        _row(
            _field("VTextField", "consignment_budget", "单品价格上限(魔力)", 6,
                   hint="0=不买；超过上限的海报跳过", persistentHint=True),
            _field("VTextField", "consignment_modes", "参与模式（open,dutch）", 6,
                   hint="逗号分隔：open 竞价 / dutch 荷兰降价 / sealed 封标", persistentHint=True),
        ),
        _row(
            _field("VTextarea", "consignment_wanted_poster_ids", "想要的指定海报 ID（留空=按档位）", 6, rows=3,
                   placeholder="146, 205, 1468",
                   hint="填了就只抢这些 ID，忽略档位设置", persistentHint=True),
            _field("VTextField", "consignment_rarities", "想要的档位（ID留空时生效）", 6,
                   placeholder="rare, precious, legendary",
                   hint="可选 normal/excellent/rare/precious/legendary，留空=不限", persistentHint=True),
        ),

        _section("③ 零风险领取", "不花魔力、无交易；默认开启"),
        _row(
            _field("VSwitch", "do_income", "领取挂机收益", 4),
            _field("VSwitch", "do_tasks", "领成长任务奖励", 4),
            _field("VSwitch", "do_settle", "结算已完成委托", 4),
        ),
        _row(
            _field("VSwitch", "do_achievements", "领成就奖励", 6),
            _field("VSwitch", "do_level_posters", "领等级海报", 6),
        ),

        _section("④ 佣人工作与互动", "工作不花魔力；互动可选免费或设预算"),
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
                   hint="仅「设定上限」生效；投喂100/小奖励300/深入交流1000", persistentHint=True),
            _field("VSwitch", "do_initiative", "自动主动互动(我作为佣人)", 4),
            _field("VTextarea", "reserved_uids", "保留佣人 UID（不参与任何自动操作）", 4, rows=2,
                   placeholder="12349, 107691", hint="逗号/空格/换行分隔", persistentHint=True),
        ),

        _section("⑤ 事务所", "站点已开原生自动派遣时不重复派单，只结算"),
        _row(
            _field("VSwitch", "do_commission", "自动派出委托", 6,
                   hint="站点原生自动派遣开着时自动跳过派出", persistentHint=True),
            _field("VSelect", "commission_preference", "派遣偏好(仅记录)", 6,
                   items=[{"title": "经验优先", "value": "experience"},
                          {"title": "魔力优先", "value": "magic"},
                          {"title": "海报优先", "value": "posters"}]),
        ),

        _section("⑥ 海报其它", "典藏任务 / 兑换 / 赠送；赠送不可逆，默认关"),
        _row(
            _field("VSwitch", "do_posters", "自动领海报任务奖励", 4),
            _field("VSwitch", "poster_auto_redeem", "检查并兑换典藏册", 4),
            _field("VTextarea", "poster_gift_recipients", "允许赠送的好友 UID", 4, rows=2,
                   placeholder="只送给这些 UID，留空则不赠送",
                   hint="赠送不可逆，留空最安全", persistentHint=True),
        ),
        _row(
            _field("VTextField", "poster_claim_rarities", "领奖档位（囤魔力用）", 12,
                   placeholder="留空=所有可领档位都领",
                   hint="只领指定档位的典藏任务奖励，如填 normal 就先囤着其它档位的魔力；"
                        "可选 normal/excellent/rare/precious/legendary，逗号分隔", persistentHint=True),
        ),

        _section("⑦ 消耗与不可逆（谨慎）", "默认全关；买人会真花魔力，释放不可撤回", ),
        _row(
            _field("VSwitch", "do_protect", "自动买保护(消耗魔力)", 3),
            _field("VTextField", "protect_minutes", "保护剩余阈值(分钟)", 3),
            _field("VTextField", "protect_budget", "单次保护魔力上限", 3),
            _field("VSwitch", "do_buy", "自动买入/抢人(不可逆)", 3),
        ),
        _row(
            _field("VTextField", "buy_max_price", "买人价格上限(魔力)", 4,
                   hint="0=不买；只有详情实时价在上限内才买", persistentHint=True),
            _field("VTextField", "buy_min_overall", "买人属性均值下限", 4,
                   hint="0=不限；过滤低属性佣人", persistentHint=True),
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
        _button("检查拍卖行情", "auction_check", "post", "mdi-camera-iris"),
        _button("立即判断出价", "auction_once", "post", "mdi-gavel", "warning"),
        _button("启动/停止抢拍", "auction_monitor", "post", "mdi-fast-forward", "primary"),
        _button("拍卖状态", "auction_status", "get", "mdi-pulse"),
        _button("检查寄售场", "consignment_check", "post", "mdi-store-search"),
        _button("立即抢寄售", "consignment_grab", "post", "mdi-cart-arrow-down", "warning"),
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

    # 拍卖行情区（只读缓存，渲染期不联网）
    auction = data.get("auction") if isinstance(data.get("auction"), dict) else {}
    auction_on = bool(data.get("auction_running"))
    a_badge = '<span class="pf-badge on">抢拍监控中</span>' if auction_on else '<span class="pf-badge">监控未启动</span>'
    a_lots = auction.get("lots") or []
    a_rows = ""
    for lot in a_lots[:8]:
        if not isinstance(lot, dict):
            continue
        rank = lot.get("my_rank") or 0
        rank_text = ("我领先" if rank == 1 else f"我第{rank}名") if rank else "未参与"
        a_rows += (
            f'<div class="pf-line"><span class="pf-line-v">'
            f'{_e(lot.get("title"))} <small>[{_e(lot.get("rarity"))}]</small></span>'
            f'<span class="pf-line-k">现价 {_e(lot.get("current_price"))} · '
            f'最低 {_e(lot.get("minimum_bid"))} · {rank_text}</span></div>'
        )
    auction_html = (
        '<div class="pf-sec">海报拍卖' + a_badge + '</div>'
        + '<div class="pf-card">'
        + f'<div class="pf-line"><span class="pf-line-k">最近行情</span>'
        + f'<span class="pf-line-v">{_e(data.get("auction_text") or "还没检查过")}</span></div>'
        + f'<div class="pf-line"><span class="pf-line-k">当前余额</span>'
        + f'<span class="pf-line-v">{_e(auction.get("balance", "—"))} 魔力</span></div>'
        + a_rows
        + '</div>'
        + '<div class="pf-note">站点为暗拍：填最终出价，站点已算好最低有效价；最后 3 分钟换领先者会延长至 3 分钟。'
        + '「检查拍卖行情」只读不出价；要自动抢拍请开启监控循环，出价前确认预算与稀有度规则。</div>'
    )

    # 海报寄售场区（只读缓存）
    cons = data.get("consignment") if isinstance(data.get("consignment"), dict) else {}
    c_rows = ""
    for lot in (cons.get("lots") or [])[:8]:
        if not isinstance(lot, dict):
            continue
        mode = lot.get("mode") or "open"
        mode_label = {"dutch": "荷兰降价", "open": "竞价", "sealed": "封标"}.get(mode, mode)
        owned = lot.get("owned", 0)
        own_text = "未拥有" if not owned else f"已有{owned}张"
        extra = (f" · 下次降价 {_e(lot.get('next_drop_at'))}" if mode == "dutch" and lot.get("next_drop_at") else "")
        c_rows += (
            f'<div class="pf-line"><span class="pf-line-v">'
            f'{_e(lot.get("title"))} <small>[{_e(lot.get("rarity"))}] {mode_label}</small></span>'
            f'<span class="pf-line-k">{own_text} · 现价 {_e(lot.get("price"))}'
            f' · 最低 {_e(lot.get("minimum_bid"))}{extra}</span></div>'
        )
    consignment_html = (
        '<div class="pf-sec">海报寄售场</div>'
        + '<div class="pf-card">'
        + f'<div class="pf-line"><span class="pf-line-k">最近行情</span>'
        + f'<span class="pf-line-v">{_e(data.get("consignment_text") or "还没检查过")}</span></div>'
        + c_rows
        + '</div>'
        + '<div class="pf-note">寄售场与光影拍卖行是两个市场，都由「启动/停止抢拍」统一监控。'
        + '按「未拥有 / 想要的指定海报或档位 + 价格上限」自动抢；荷兰降价品现价合适就直接买。</div>'
    )

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
        + auction_html
        + consignment_html
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
