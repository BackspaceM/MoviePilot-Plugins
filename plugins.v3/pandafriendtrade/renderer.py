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
    "cron": core.DEFAULT_CRON,
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
    # 合并后的海报低价抢购项目
    "do_poster_snipe": False,
    "poster_snipe_max_price": 0,
    "poster_snipe_rarities": "",
    "poster_snipe_unowned_only": True,
    "poster_snipe_poll_seconds": 15,
    "poster_claim_tasks": "",
    "poster_snipe_rounds": 3,
    "snipe_range_normal": "",
    "snipe_range_excellent": "",
    "snipe_range_rare": "",
    "snipe_range_precious": "",
    "snipe_range_legendary": "",
}


# 稀有度中文多选下拉项（value 用站点 key）
RARITY_ITEMS = [
    {"title": "普通", "value": "normal"},
    {"title": "优秀", "value": "excellent"},
    {"title": "稀有", "value": "rare"},
    {"title": "珍贵", "value": "precious"},
    {"title": "传说", "value": "legendary"},
]


def _multi_select(model: str, label: str, items: list, hint: str = "") -> dict:
    return {
        "component": "VSelect",
        "props": {"model": model, "label": label, "items": items,
                  "multiple": True, "chips": True, "closableChips": True,
                  "clearable": True, "hint": hint, "persistentHint": bool(hint)},
    }


def form(task_options: list | None = None) -> tuple[list[dict], dict[str, Any]]:
    """配置页：按网页分类分区（好友买卖 / 事务所 / 电影图鉴·海报）；默认只开零风险项。"""
    task_items = task_options if isinstance(task_options, list) and task_options else core.parse_task_options(None)
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
        _row(
            _field("VTextField", "cron", "定时执行周期（5 位 crontab）", 12,
                   placeholder=core.DEFAULT_CRON,
                   hint="分 时 日 月 周；默认 0 3,9,18,23 * * * 即凌晨3点、9点、18点、23点整各跑一轮。改完保存即生效", persistentHint=True),
        ),
        _alert("定时任务由插件注册给宿主调度器：只有「启用插件」开启时才会注册；同一天已工作/互动过的佣人不会再被安排。", "info"),

        _section("连接与网络", "Cookie 只存本机；代理按需开启，SOCKS 需容器装 PySocks"),
        _row(
            _field("VTextField", "site", "站点地址", 6, placeholder=core.DEFAULT_SITE),
            _field("VTextField", "timeout", "请求超时(秒)", 3),
            _field("VTextField", "max_actions", "每轮动作上限", 3),
        ),
        _row(
            _field("VTextField", "request_interval", "请求间隔(秒)", 12,
                   hint="默认 1.5 秒，别调太小以免触发站点风控", persistentHint=True),
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
        _alert("请求间隔默认 1.5 秒，别调太小以免触发站点风控。", "info"),

        # ===== 分类一：好友买卖（对应网页「好友买卖」）=====
        _section("① 好友买卖 · 日常", "挂机收益、成长任务、佣人工作与互动；默认只开零风险项"),
        _row(
            _field("VSwitch", "do_income", "领取挂机收益", 4),
            _field("VSwitch", "do_tasks", "领成长任务奖励", 4),
            _field("VSwitch", "do_settle", "结算已完成委托", 4),
        ),
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
            _field("VSwitch", "do_achievements", "领成就奖励", 4),
        ),
        _row(
            _field("VSwitch", "do_level_posters", "领等级海报", 4),
            _field("VTextarea", "reserved_uids", "保留佣人 UID（不参与任何自动操作）", 8, rows=2,
                   placeholder="12349, 107691", hint="逗号/空格/换行分隔", persistentHint=True),
        ),

        # ===== 分类二：事务所（对应网页「事务所」）=====
        _section("② 事务所", "站点已开原生自动派遣时不重复派单，只结算"),
        _row(
            _field("VSwitch", "do_commission", "自动派出委托", 6,
                   hint="站点原生自动派遣开着时自动跳过派出", persistentHint=True),
            _field("VSelect", "commission_preference", "派遣偏好(仅记录)", 6,
                   items=[{"title": "经验优先", "value": "experience"},
                          {"title": "魔力优先", "value": "magic"},
                          {"title": "海报优先", "value": "posters"}]),
        ),

        # ===== 分类三：电影图鉴·海报（对应网页「电影图鉴」）—— 海报项全归这里 =====
        _section("③ 电影图鉴 · 海报", "海报低价抢购 + 领奖档位 + 兑换赠送，全部海报项集中在此"),

        _alert(
            "海报低价抢购：拍卖行和寄售场合并为一个项目，统一按「价格从低到高」捡漏，先抢最便宜的。"
            "荷兰降价品按当前价直接买，竞价品按站点最低有效价出价（暗拍，不重复加价）。"
            "注意：出价即扣手续费，未中标本金退还但手续费不退；已出价过的海报会自动跳过，不再重复扣费。", "info"),
        _row(
            _field("VSwitch", "do_poster_snipe", "开启海报低价抢购", 4,
                   hint="一个开关同时管拍卖行+寄售场；关闭则完全不碰抢购", persistentHint=True),
            _field("VTextField", "poster_snipe_rounds", "抢购轮次（每轮最多出价几笔）", 4,
                   hint="默认 3；一次扫描按价格从低到高最多抢 N 笔，防止一轮把手续费全砸出去", persistentHint=True),
            _field("VTextField", "poster_snipe_max_price", "全局价格上限(魔力)", 4,
                   hint="0=不按全局抢；设了下面的稀有度区间时，本项作为总闸门", persistentHint=True),
        ),
        _row(
            {
                "component": "VCol", "props": {"cols": 12, "md": 6},
                "content": [_multi_select("poster_snipe_rarities", "只抢这些稀有度（不选=全部）",
                                          RARITY_ITEMS, "中文多选；留空不限稀有度")],
            },
            _field("VSwitch", "poster_snipe_unowned_only", "只要未拥有的海报", 3,
                   hint="默认开，避免重复买已有的", persistentHint=True),
            _field("VTextField", "poster_snipe_poll_seconds", "抢购轮询间隔(秒)", 3,
                   hint="建议 15 秒", persistentHint=True),
        ),

        _alert(
            "按稀有度设置价格档位（价格区间）：填「最低-最高」，如优秀填 5000-7000、稀有的填 15000-20000。"
            "只抢价格落在区间内的海报；留空的稀有度按上面的全局上限判断。只填一个数字视为 0 到该价。", "info"),
        _row(
            _field("VTextField", "snipe_range_normal", "普通 · 价格区间", 4,
                   placeholder="如 1000-3000", hint="留空则按全局上限", persistentHint=True),
            _field("VTextField", "snipe_range_excellent", "优秀 · 价格区间", 4,
                   placeholder="如 5000-7000", hint="留空则按全局上限", persistentHint=True),
            _field("VTextField", "snipe_range_rare", "稀有 · 价格区间", 4,
                   placeholder="如 15000-20000", hint="留空则按全局上限", persistentHint=True),
        ),
        _row(
            _field("VTextField", "snipe_range_precious", "珍贵 · 价格区间", 6,
                   placeholder="如 30000-50000", hint="留空则按全局上限", persistentHint=True),
            _field("VTextField", "snipe_range_legendary", "传说 · 价格区间", 6,
                   placeholder="如 80000-120000", hint="留空则按全局上限", persistentHint=True),
        ),

        _alert(
            "领奖档位来自页面海报任务实时扫描。先在详情页点「扫描任务档位」，再回到本页下拉框选择要领的档位；"
            "「一次性典藏」「主题片单交付」都在这个多选框里（带【分类】前缀）。留空=领所有可领档位。",
            "info"),
        _row({
            "component": "VCol", "props": {"cols": 12},
            "content": [_multi_select("poster_claim_tasks", "领奖档位（多选，来自页面任务）",
                                      task_items,
                                      "未扫描时显示内置档位；点详情页「扫描任务档位」后刷新")],
        }),
        _row(
            _field("VSwitch", "do_posters", "自动领海报任务奖励", 4),
            _field("VSwitch", "poster_auto_redeem", "检查并兑换典藏册", 4),
            _field("VTextarea", "poster_gift_recipients", "允许赠送的好友 UID", 4, rows=2,
                   placeholder="只送给这些 UID，留空则不赠送",
                   hint="赠送不可逆，留空最安全", persistentHint=True),
        ),

        # ===== 分类四：消耗与不可逆 =====
        _section("④ 消耗与不可逆（谨慎）", "默认全关；买人会真花魔力，释放不可撤回"),
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
        _button("扫描任务档位", "scan_tasks", "post", "mdi-file-search"),
        _button("检查海报行情", "snipe_check", "post", "mdi-magnify"),
        _button("立即低价抢购", "snipe_once", "post", "mdi-cart-arrow-down", "warning"),
        _button("启动/停止抢购", "snipe_monitor", "post", "mdi-fast-forward", "primary"),
        _button("清空已抢记录", "snipe_reset", "post", "mdi-backup-restore"),
        _button("抢购状态", "auction_status", "get", "mdi-pulse"),
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

    # 海报低价抢购区（单一项目：拍卖行 + 寄售场合并，按价格从低到高；只读缓存）
    snipe = data.get("snipe") if isinstance(data.get("snipe"), dict) else {}
    snipe_on = bool(data.get("snipe_on"))
    s_badge = '<span class="pf-badge on">抢购监控中</span>' if snipe_on else '<span class="pf-badge">监控未启动</span>'
    s_rows = ""
    for lot in (snipe.get("lots") or [])[:10]:
        if not isinstance(lot, dict):
            continue
        source = lot.get("source") or ""
        mode = {"dutch": "荷兰降价", "open": "竞价", "sealed": "封标"}.get(lot.get("mode"), lot.get("mode") or "")
        s_rows += (
            f'<div class="pf-line"><span class="pf-line-v">'
            f'{_e(lot.get("title"))} <small>[{_e(lot.get("rarity"))}] {_e(source)}·{_e(mode)}</small></span>'
            f'<span class="pf-line-k">{_e(lot.get("amount"))} 魔力</span></div>'
        )
    snipe_html = (
        '<div class="pf-sec">电影图鉴 · 海报低价抢购' + s_badge + '</div>'
        + '<div class="pf-card">'
        + f'<div class="pf-line"><span class="pf-line-k">最近行情</span>'
        + f'<span class="pf-line-v">{_e(data.get("snipe_text") or "还没检查过")}</span></div>'
        + f'<div class="pf-line"><span class="pf-line-k">当前余额</span>'
        + f'<span class="pf-line-v">{_e(snipe.get("balance", "—"))} 魔力</span></div>'
        + f'<div class="pf-line"><span class="pf-line-k">本轮可抢</span>'
        + f'<span class="pf-line-v">{_e(snipe.get("candidates", 0))} 件（按价格从低到高，'
        + f'抢购轮次上限 {_e(data.get("snipe_rounds", 3))}）</span></div>'
        + f'<div class="pf-line"><span class="pf-line-k">已抢记录</span>'
        + f'<span class="pf-line-v">{_e(data.get("snipe_attempted", 0))} 条（出过价的不再重复抢）</span></div>'
        + s_rows
        + '</div>'
        + '<div class="pf-note">拍卖行与寄售场已合并为一个项目，统一按价格从低到高捡漏，先抢最便宜的。'
        + '可为每个稀有度单独设价格区间；出价即扣手续费、未中标手续费不退，因此已出价过的海报自动记入'
        + '「已抢记录」不再重复抢（想重新参与请点「清空已抢记录」）。'
        + '领奖档位请先点「扫描任务档位」，再到配置页下拉框选择。</div>'
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
        + snipe_html
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
