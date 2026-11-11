"""RouterOS 统一工作台 —— 线上两个问题的回归测试。

线上报错：
1. 插件 RouterOS 统一工作台 服务注册失败
   BaseScheduler.add_job() got multiple values for argument 'trigger'
2. 概况后面的防火墙、queue、备份、分流按钮，显示「页签不对」
"""

from __future__ import annotations

import ast
import inspect

from app.plugins.routerosworkbench import (
    PLUGIN_ID,
    RouterOSWorkbench,
    _BODY_DEFAULT,
    _KEY_TAB,
)
from app.plugins.routerosworkbench import sections
from app.sdk.plugin import _PluginBase


def _fresh() -> RouterOSWorkbench:
    _PluginBase.POSTED.clear()
    _PluginBase.CONFIGS.clear()
    p = RouterOSWorkbench()
    p._data = {}
    p._config = {}
    return p


def _init(p: RouterOSWorkbench, **kwargs) -> None:
    cfg = {"enabled": True, "cron": "*/10 * * * *", "router_address": "192.168.1.1"}
    cfg.update(kwargs)
    p.init_plugin(cfg)


# ---------------------------------------------------------------------------
# Bug 1：服务注册失败
# ---------------------------------------------------------------------------

def test_get_service_kwargs_has_no_trigger():
    """service["kwargs"] 绝不能含 trigger —— MP 会把它展开给 add_job 导致撞名。"""
    p = _fresh()
    _init(p)
    services = p.get_service()
    assert len(services) == 1
    assert services[0]["kwargs"] == {}
    assert "trigger" not in services[0]["kwargs"]
    assert services[0]["func"] == p.refresh_once


def test_mp_service_registration_does_not_crash():
    """按 MP scheduler.py:427 的真实调用形式模拟注册，不应抛 TypeError。"""
    def mp_start(job_id, *args, **kwargs):
        return None

    def add_job(func, trigger=None, args=None, kwargs=None, id=None, name=None,
                misfire_grace_time=None, coalesce=None, max_instances=None,
                next_run_time=None, jobstore="default", executor="default",
                replace_existing=False, **trigger_args):
        if trigger is not None and not isinstance(trigger, str) and trigger_args:
            raise TypeError("The trigger argument cannot be combined with trigger arguments")
        return {"ok": True}

    p = _fresh()
    _init(p)
    for svc in p.get_service():
        # 完全照抄 MP 源码：add_job(self.start, service["trigger"], ...,
        #                        **service["kwargs"], kwargs={"job_id": ...})
        add_job(mp_start, svc["trigger"], id=svc["id"], name=svc["name"],
                **svc["kwargs"], kwargs={"job_id": svc["id"]})


def test_original_buggy_kwargs_would_crash():
    """反向用例：确认测试用例确实能抓到修复前的写法（防止假阳性）。"""
    def add_job(func, trigger=None, *args, **kwargs):
        return None

    bad = {"trigger": "定时刷新"}
    try:
        add_job(lambda: None, "CronTrigger", **bad)
    except TypeError as err:
        assert "multiple values" in str(err)
    else:
        raise AssertionError("修复前的 kwargs 应触发撞名错误")


def test_func_defaults_carry_scheduled_trigger():
    """MP 无参调用 func，触发来源必须放在默认值里（原来是「手动刷新」）。"""
    p = _fresh()
    sig = inspect.signature(p.refresh_once)
    assert sig.parameters["trigger"].default == "定时刷新"
    # 手动 API 仍显式传自己的触发来源，不受默认值影响
    src = inspect.getsource(p.api_refresh)
    assert 'refresh_once("手动刷新")' in src


# ---------------------------------------------------------------------------
# Bug 2：页签按钮「页签不对」
# ---------------------------------------------------------------------------

def test_api_tab_declares_body_injection():
    """api_tab 的默认参数必须是 Body(default=None)，否则 POST body 永远绑不上。"""
    import fastapi
    assert isinstance(_BODY_DEFAULT, fastapi._BodyMarker)
    sig = inspect.signature(RouterOSWorkbench.api_tab)
    assert sig.parameters["payload"].default is _BODY_DEFAULT


def test_api_tab_accepts_all_tab_names():
    """前端 POST body 形如 {"name": key}，五个页签都应切换成功。"""
    p = _fresh()
    _init(p)
    for key, _label in sections.TABS:
        resp = p.api_tab({"name": key})
        assert resp.success is True, f"{key}: {resp.message}"
        assert resp.data["active_tab"] == key
        assert p.get_data(_KEY_TAB) == key
        assert p._active_tab == key


def test_api_tab_accepts_alias_keys_and_string():
    p = _fresh()
    _init(p)
    assert p.api_tab({"tab": "queue"}).success is True
    assert p.api_tab({"active_tab": "backup"}).success is True
    assert p.api_tab("firewall").success is True


def test_api_tab_rejects_invalid_with_clear_message():
    p = _fresh()
    _init(p)
    for bad in ({}, {"name": "nope"}, None, {"name": ""}):
        resp = p.api_tab(bad)
        assert resp.success is False
        assert "页签不对" in resp.message
    # 大写/空白容错
    assert p.api_tab({"name": "  FIREWALL  "}).success is True


def test_tab_buttonsbind_registered_api_with_valid_params():
    """详情页 5 个页签按钮：参数必须在 TAB_KEYS，且 api 已注册（杜绝点了没反应）。"""
    p = _fresh()
    _init(p)
    buttons = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("component") == "VBtn":
                buttons.append(node)
            content = node.get("content")
            if isinstance(content, list):
                for c in content:
                    walk(c)
            elif isinstance(content, dict):
                walk(content)
        elif isinstance(node, list):
            for c in node:
                walk(c)

    for card in p.get_page():
        walk(card)

    registered_last = {a["path"].rstrip("/").split("/")[-1] for a in p.get_api()}
    tab_buttons = []
    for b in buttons:
        click = (b.get("events") or {}).get("click") or {}
        api = str(click.get("api") or "")
        if api.endswith("/tab"):
            tab_buttons.append(b)
            assert api.rstrip("/").split("/")[-1] in registered_last
            assert click.get("method") == "post"
            assert "disabled" not in (b.get("props") or {})
            name = (click.get("params") or {}).get("name")
            assert name in sections.TAB_KEYS, f"页签按钮参数非法: {name}"
    assert len(tab_buttons) == len(sections.TABS) == 5


def test_tab_button_paths_match_get_api():
    """按钮 api = plugin/<ID>/tab，get_api 注册 /tab（MP 自动补 /<pid> 前缀）。"""
    p = _fresh()
    _init(p)
    paths = [a["path"] for a in p.get_api()]
    assert "/tab" in paths
    # 所有按钮的 api 末段都在注册清单里
    buttons = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("component") == "VBtn":
                buttons.append(node)
            c = node.get("content")
            if isinstance(c, list):
                for x in c:
                    walk(x)
            elif isinstance(c, dict):
                walk(c)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for card in p.get_page():
        walk(card)
    registered_last = {a["path"].rstrip("/").split("/")[-1] for a in p.get_api()}
    for b in buttons:
        click = (b.get("events") or {}).get("click") or {}
        api = str(click.get("api") or "")
        if api:
            assert api.rstrip("/").split("/")[-1] in registered_last, api


# ---------------------------------------------------------------------------
# 契约 / 结构
# ---------------------------------------------------------------------------

def test_contract_metadata():
    assert len(RouterOSWorkbench.plugin_version.split(".")) == 3 and all(p.isdigit() for p in RouterOSWorkbench.plugin_version.split("."))
    assert RouterOSWorkbench.plugin_author == "BackspaceM"
    assert RouterOSWorkbench.plugin_config_prefix == "routerosworkbench_"
    assert RouterOSWorkbench.plugin_icon.startswith("https://")


def test_class_metadata_are_string_literals():
    """官方版本门禁以 AST 提取类级元数据，必须是字面量。"""
    import pathlib
    src = pathlib.Path(inspect.getsourcefile(RouterOSWorkbench)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "RouterOSWorkbench")
    required = {"plugin_name", "plugin_desc", "plugin_icon", "plugin_version",
                "plugin_author", "author_url", "plugin_config_prefix"}
    for node in cls.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in required:
                assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str), \
                    f"{name} 必须是字符串字面量"


def test_get_form_fields_match_defaults():
    p = _fresh()
    form, defaults = p.get_form()
    assert isinstance(form, list) and isinstance(defaults, dict)
    models = set()

    def walk(node):
        if isinstance(node, dict):
            m = (node.get("props") or {}).get("model")
            if isinstance(m, str):
                models.add(m)
            c = node.get("content")
            if isinstance(c, list):
                for x in c:
                    walk(x)
            elif isinstance(c, dict):
                walk(c)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(form)
    assert models == set(defaults), f"表单字段与默认值不一致: {models ^ set(defaults)}"


def test_no_disabled_buttons_on_page():
    p = _fresh()
    _init(p)
    bad = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("component") == "VBtn":
                props = node.get("props") or {}
                if "disabled" in props or props.get("disabled") is True:
                    bad.append(props.get("text"))
            c = node.get("content")
            if isinstance(c, list):
                for x in c:
                    walk(x)
            elif isinstance(c, dict):
                walk(c)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for card in p.get_page():
        walk(card)
    assert bad == [], f"存在禁用按钮: {bad}"


def test_page_text_no_blank_pill_triggers():
    """详情页动态文本不应含会触发 Markdown 的半角标记（空药丸）。"""
    p = _fresh()
    _init(p)
    p._log("INFO", "测试 [x][y] *粗* `码` <tag> _斜_")
    texts = []

    def walk(node):
        if isinstance(node, dict):
            t = node.get("text")
            if isinstance(t, str):
                texts.append(t)
            c = (node.get("props") or {}).get("text")
            if isinstance(c, str):
                texts.append(c)
            content = node.get("content")
            if isinstance(content, list):
                for x in content:
                    walk(x)
            elif isinstance(content, dict):
                walk(content)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for card in p.get_page():
        walk(card)
    # 按钮文字是纯中文标签，允许；仅检查含方括号对的参考式链接模式
    assert not any("[x][y]" in t for t in texts)
