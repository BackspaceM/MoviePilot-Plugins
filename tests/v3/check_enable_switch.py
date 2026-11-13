#!/usr/bin/env python3
"""离线回归：5 个常驻插件的启用开关 + 名称辅种日志双栏。

用法：python3 tests/v3/check_enable_switch.py
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import check_api_contract as cc  # noqa: E402

ENABLED_PLUGINS = {
    "routerosconnectioncenter": "RouterOSConnectionCenter",
    "routerosscenariotemplates": "RouterOSScenarioTemplates",
    "routerosremoteaccesswizard": "RouterOSRemoteAccessWizard",
    "mpoverview": "MPOverview",
    "librarycheckup": "LibraryCheckup",
}


def models(form):
    """收集设置页所有带 model 的控件。"""
    result = []

    def walk(node):
        if isinstance(node, dict):
            props = node.get("props")
            if isinstance(props, dict) and "model" in props:
                result.append(props["model"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(form)
    return result


def main():
    cc.install_stubs()
    index = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
    checks = 0
    failures = []

    def check(ok, label):
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(label)

    for dirname, plugin_id in ENABLED_PLUGINS.items():
        _module, cls, found = cc.load_plugin(ROOT / "plugins.v3" / dirname)
        version = cls.plugin_version
        check(found == plugin_id, f"{plugin_id}: 插件 ID")
        check(index[plugin_id]["version"] == version, f"{plugin_id}: 索引版本一致")
        check(f"v{version}" in index[plugin_id]["history"], f"{plugin_id}: 更新记录")

        on = cls()
        on.init_plugin({})
        form, defaults = on.get_form()
        check("enabled" in models(form), f"{plugin_id}: 设置页有启用开关")
        check(defaults.get("enabled") is True, f"{plugin_id}: 默认启用")
        check(on.get_state() is True, f"{plugin_id}: 启用时 get_state")
        check(len(cc.collect_click_events(on.get_page())) > 0, f"{plugin_id}: 启用时按钮可用")

        off = cls()
        off.init_plugin({"enabled": False})
        _form, off_defaults = off.get_form()
        check(off_defaults.get("enabled") is False, f"{plugin_id}: 停用后 defaults")
        check(off.get_state() is False, f"{plugin_id}: 停用后 get_state")
        page = off.get_page()
        check("插件已停用" in json.dumps(page, ensure_ascii=False), f"{plugin_id}: 停用提示")
        check(len(cc.collect_click_events(page)) == 0, f"{plugin_id}: 停用后无动作按钮")
        for api in off.get_api():
            ok = False
            try:
                try:
                    resp = api["endpoint"]()
                except TypeError:
                    resp = api["endpoint"]({})
                ok = (getattr(resp, "success", None) is False
                      and "已停用" in str(getattr(resp, "message", "")))
            except Exception as err:  # noqa: BLE001
                failures.append(f"{plugin_id}: {api['path']} 抛异常 {type(err).__name__}: {err}")
            check(ok, f"{plugin_id}: {api['path']} 停用后拒绝执行")
        if "get_dashboard_meta" in cls.__dict__:
            check(off.get_dashboard_meta() == [], f"{plugin_id}: 停用后不注册首页组件")
        if "get_command" in cls.__dict__:
            check(off.get_command() == [], f"{plugin_id}: 停用后不注册远程命令")
        if "get_dashboard" in cls.__dict__:
            _cols, attrs, _elements = off.get_dashboard()
            check(attrs.get("subtitle") == "插件已停用", f"{plugin_id}: 停用后首页不采集")

    # 名称辅种：日志改成成功 / 失败双栏
    module, cls, _ = cc.load_plugin(ROOT / "plugins.v3" / "namecrossseed")
    lv = module.logview
    check(cls.plugin_version == index["NameCrossSeed"]["version"], "名称辅种: 索引版本一致")
    check(lv.SUMMARY_LIMIT == 5, "名称辅种: 每栏 5 条")
    rows = [
        {"name": f"种子{i}", "state": state,
         "message": "完成" if state == "added" else "超时"}
        for i, state in enumerate(
            ["added", "exists", "failed", "error", "nomatch", "skipped", "added", "failed"]
        )
    ]
    summary = lv.summarize(rows)
    check(summary["ok_total"] == 3 and summary["bad_total"] == 3, "名称辅种: 成功/失败分类")
    check(len(summary["ok"]) == 3 and len(summary["bad"]) == 3, "名称辅种: 两栏条数")
    check("种子6" in summary["ok"][0], "名称辅种: 最新记录排在最前")
    html = lv.render({}, "[00:00] 开始\n" * 20, rows)
    check('id="cs-ok"' in html and 'id="cs-bad"' in html, "名称辅种: 双栏渲染")
    check("<details open" not in html and html.count('<details class="cs-fold">') == 2,
          "名称辅种: 完整日志与名录默认折叠")
    check("<script" not in html, "名称辅种: 不注入脚本")
    seed = cls()
    seed.init_plugin({})
    data = seed.api_log().data
    for key in ("ok_text", "bad_text", "ok_total", "bad_total", "text", "progress", "running"):
        check(key in data, f"名称辅种: api_log 返回 {key}")

    print(f"合计 {checks} 项，通过 {checks - len(failures)}，失败 {len(failures)}")
    for item in failures:
        print("  FAIL", item)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
