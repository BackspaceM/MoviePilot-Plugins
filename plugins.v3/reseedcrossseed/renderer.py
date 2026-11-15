"""Reseed 辅种插件的页面层：配置表单与静态详情页。

本模块只做 JSON 组件与 HTML 拼装，不读宿主数据、不联网、不落盘，
保证 get_form / get_page 渲染期不会抛异常。
"""

from __future__ import annotations

import html
from typing import Any

from . import core

PLUGIN_ID = "ReseedCrossSeed"


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
    "dry_run": False,
    "notify": False,
    "api": core.DEFAULT_API,
    "username": "",
    "password": "",
    "dirs": "",
    "bindings": "",
    "sites": "",
    "target_downloader": "",
    "seed_tag": core.DEFAULT_SEED_TAG,
    "skip_checking": True,
    "auto_start": True,
    "include_warning": False,
    "max_per_run": 50,
    "timeout": 30,
    "schedule_mode": "禁用",
    "cron": core.DEFAULT_CRON,
    "interval_minutes": 720,
}


def form() -> tuple[list[dict], dict[str, Any]]:
    """返回配置页组件与安全默认值。"""
    content = [
        _alert(
            "扫描下载目录生成「文件名 + 大小」索引，交给 Reseed 服务找各站同内容种子，"
            "下载种子后复核文件清单，一致才按原路径加进下载器做种。需要 Reseed 账号（官方或自建后端）。",
        ),
        _row(
            _field("VSwitch", "enabled", "启用插件", 3),
            _field("VSwitch", "dry_run", "试运行（只统计不加种）", 3),
            _field("VSwitch", "notify", "发送通知", 3),
            _field("VSwitch", "include_warning", "包含部分匹配结果", 3,
                    hint="默认只处理完全匹配；部分匹配复核不过会自动跳过", persistentHint=True),
        ),
        _alert("① Reseed 服务：官方地址已填好，自建后端直接改成自己的地址。账号密码只用于登录取 token。", "info"),
        _row(
            _field("VTextField", "api", "服务地址", 6, placeholder=core.DEFAULT_API),
            _field("VTextField", "username", "Reseed 账号", 3, autocomplete="off"),
            _field("VTextField", "password", "Reseed 密码", 3, type="password", autocomplete="new-password"),
        ),
        _alert("② 扫描目录与站点映射：目录填容器里能看到的下载根目录；映射把 Reseed 站点名对应到 MoviePilot 里的站点。", "info"),
        _row(
            _field("VTextarea", "dirs", "扫描目录（每行一个）", 12, rows=3,
                   placeholder="/downloads\n/media/movies",
                   hint="只扫顶层项，目录里的文件按相对路径与大小建索引", persistentHint=True),
        ),
        _row(
            _field("VTextarea", "bindings", "站点映射与 Passkey（每行一个）", 12, rows=4,
                   placeholder="HDU:青蛙:xxxxxxxxxx\nTJUPT:北洋:yyyyyyyyyy",
                   hint="格式 Reseed站点名:MP站点名:Passkey；站点名大小写不敏感，留空则不做辅种", persistentHint=True),
        ),
        _row(
            _field("VTextField", "sites", "参与站点（逗号分隔，留空=映射里全部）", 6,
                   placeholder="HDU,TJUPT",
                   hint="填 Reseed 服务里的站点名，可在详情页点「检测服务」查看收录站点", persistentHint=True),
            _field("VCombobox", "target_downloader", "辅种目标下载器", 6,
                   items=[], clearable=True,
                   hint="留空用第一个可用下载器；下载器名可直接填", persistentHint=True),
        ),
        _alert("③ 运行方式：官方服务限制每小时 5 次 / 每天 10 次查询，索引没变化时插件会直接复用上次结果。", "info"),
        _row(
            _field("VSelect", "schedule_mode", "定时方式", 4,
                   items=[{"title": "计划任务（cron）", "value": "计划任务"},
                          {"title": "固定间隔", "value": "固定间隔"},
                          {"title": "禁用", "value": "禁用"}]),
            _field("VTextField", "cron", "执行周期（cron）", 4, placeholder=core.DEFAULT_CRON),
            _field("VTextField", "interval_minutes", "固定间隔（分钟）", 4, placeholder="720"),
        ),
        _row(
            _field("VSwitch", "skip_checking", "qBittorrent 跳过哈希校验", 4),
            _field("VSwitch", "auto_start", "添加后自动开始做种", 4),
            _field("VTextField", "seed_tag", "辅种标签", 4, placeholder=core.DEFAULT_SEED_TAG),
        ),
        _row(
            _field("VTextField", "max_per_run", "单次最多辅种数", 6, placeholder="50"),
            _field("VTextField", "timeout", "请求超时（秒）", 6, placeholder="30"),
        ),
        _alert(
            "与「名称辅种」「Hash 辅种」原理不同：这里不需要种子缓存目录，只看媒体文件本身。"
            "同一站点别和其他辅种插件同时开，避免重复添加。",
            "warning",
        ),
    ]
    return [{"component": "VForm", "content": content}], dict(DEFAULTS)


_STYLE = """
<style>
.rcs{color:inherit;font-family:inherit;line-height:1.55;font-size:14px}
.rcs *{box-sizing:border-box}
.rcs-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:2px 0 14px}
.rcs-logo{width:42px;height:42px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#4F46E5,#7C3AED);box-shadow:0 6px 18px rgba(99,102,241,.28)}
.rcs-logo svg{width:24px;height:24px}
.rcs-title{font-size:19px;font-weight:700;letter-spacing:.3px}
.rcs-sub{font-size:12px;opacity:.6;margin-top:1px}
.rcs-badge{margin-left:auto;font-size:11px;padding:4px 11px;border-radius:999px;
  border:1px solid rgba(127,127,127,.24);background:rgba(127,127,127,.07)}
.rcs-badge.on{color:#0F6E56;border-color:rgba(15,110,86,.35);background:rgba(15,110,86,.1)}
.rcs-sec{display:flex;align-items:baseline;gap:8px;font-size:13px;font-weight:650;margin:16px 0 9px}
.rcs-sec:before{content:'';width:3px;height:13px;border-radius:3px;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.rcs-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:9px}
.rcs-cell{background:rgba(127,127,127,.075);border:1px solid rgba(127,127,127,.16);
  border-radius:12px;padding:11px 12px;min-width:0}
.rcs-k{font-size:11px;opacity:.6;white-space:nowrap}
.rcs-v{font-size:21px;font-weight:700;margin-top:3px;white-space:nowrap;
  background:linear-gradient(135deg,#6366F1,#A855F7);-webkit-background-clip:text;background-clip:text;color:transparent}
.rcs-line{display:flex;align-items:baseline;gap:10px;padding:8px 1px;
  border-bottom:1px solid rgba(127,127,127,.12);font-size:12px}
.rcs-line:last-child{border-bottom:0}
.rcs-line-k{opacity:.58;min-width:88px;white-space:nowrap}
.rcs-line-v{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rcs-card{background:rgba(127,127,127,.055);border:1px solid rgba(127,127,127,.14);border-radius:14px;padding:5px 14px}
.rcs-note{font-size:12px;opacity:.68;line-height:1.8;padding:10px 13px;border-radius:10px;
  background:rgba(127,127,127,.06);border:1px solid rgba(127,127,127,.13)}
.rcs-foot{font-size:11px;opacity:.45;margin-top:12px;line-height:1.7}
@media (max-width:720px){.rcs-grid{grid-template-columns:repeat(2,1fr)}.rcs-badge{margin-left:0}}
</style>
"""

_LOGO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4 5h6"/><path d="M4 10h6"/><path d="M4 15h6"/>'
    '<path d="M14 12h6"/><path d="M17 9l3 3-3 3"/>'
    '<path d="M20 5v14"/>'
    '</svg>'
)


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _button(text: str, action: str, method: str, icon: str, color: str = "") -> dict:
    props: dict[str, Any] = {
        "variant": "tonal", "prepend-icon": icon, "class": "ma-1",
    }
    if color:
        props["color"] = color
    return {
        "component": "VBtn",
        "props": props,
        "text": text,
        "events": {"click": {"api": f"plugin/{PLUGIN_ID}/{action}", "method": method, "params": {}}},
    }


def render_page(state: dict[str, Any]) -> list[dict]:
    """详情页：一行按钮 + 一张静态状态卡，不显示日志、不轮询。"""
    data = state if isinstance(state, dict) else {}
    last = data.get("last") if isinstance(data.get("last"), dict) else {}
    service = data.get("service") if isinstance(data.get("service"), dict) else {}
    running = bool(data.get("running"))

    buttons = [
        _button("立即辅种", "run", "post", "mdi-play", "primary"),
        _button("试运行一次", "dryrun", "post", "mdi-file-search-outline"),
        _button("检测服务", "probe", "post", "mdi-account-check"),
        _button("清除缓存", "clear_cache", "post", "mdi-delete-sweep", "warning"),
        _button("刷新状态", "status", "get", "mdi-refresh"),
    ]

    badge = (
        '<span class="rcs-badge on">正在运行</span>' if running
        else '<span class="rcs-badge">空闲</span>'
    )
    stats = "".join([
        f'<div class="rcs-cell"><div class="rcs-k">索引条目</div><div class="rcs-v">{_e(last.get("indexed", 0))}</div></div>',
        f'<div class="rcs-cell"><div class="rcs-k">服务命中</div><div class="rcs-v">{_e(last.get("hits", 0))}</div></div>',
        f'<div class="rcs-cell"><div class="rcs-k">辅种成功</div><div class="rcs-v">{_e(last.get("added", 0))}</div></div>',
        f'<div class="rcs-cell"><div class="rcs-k">已存在</div><div class="rcs-v">{_e(last.get("exists", 0))}</div></div>',
        f'<div class="rcs-cell"><div class="rcs-k">失败/跳过</div><div class="rcs-v">{_e(last.get("failed", 0))}</div></div>',
    ])

    service_text = (
        f'{_e(service.get("message") or "还没检测过")}'
        f'　·　收录站点 {_e(service.get("sites", 0))} 个'
        f'　·　{_e(service.get("checked_at") or "—")}'
    )
    lines = "".join([
        f'<div class="rcs-line"><span class="rcs-line-k">服务状态</span><span class="rcs-line-v">{service_text}</span></div>',
        f'<div class="rcs-line"><span class="rcs-line-k">最近一轮</span><span class="rcs-line-v">{_e(data.get("last_text") or "还没跑过")}</span></div>',
        f'<div class="rcs-line"><span class="rcs-line-k">运行方式</span><span class="rcs-line-v">{_e(data.get("schedule_text") or "定时已禁用")}</span></div>',
        f'<div class="rcs-line"><span class="rcs-line-k">试运行</span><span class="rcs-line-v">{"开启：只统计不添加种子" if data.get("dry_run") else "关闭：命中且复核通过会添加种子"}</span></div>',
    ])

    card = (
        '<div class="rcs">'
        + _STYLE
        + '<div class="rcs-head">'
        + '<span class="rcs-logo">' + _LOGO + '</span>'
        + '<span><div class="rcs-title">Reseed 辅种</div>'
        + '<div class="rcs-sub">本地文件索引 → 服务端匹配 → 文件清单复核 → 加进下载器做种</div></span>'
        + badge
        + '</div>'
        + '<div class="rcs-sec">最近一轮</div>'
        + f'<div class="rcs-grid">{stats}</div>'
        + '<div class="rcs-sec">运行状态</div>'
        + f'<div class="rcs-card">{lines}</div>'
        + '<div class="rcs-sec">说明</div>'
        + '<div class="rcs-note">逐组过程和失败原因在插件卡片右上角「…」→ 查看日志里，页面不显示日志。'
        + '官方服务有查询次数限制，索引内容没变化时会直接复用上次命中结果，不重复上传。</div>'
        + '<div class="rcs-foot">与名称辅种、Hash 辅种是三条独立路线，不要对同一站点同时开启多个辅种插件。</div>'
        + '</div>'
    )
    return [
        {"component": "div", "content": buttons},
        {"component": "div", "html": card},
    ]
