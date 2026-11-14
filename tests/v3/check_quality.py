#!/usr/bin/env python3
# 全仓插件质量门禁：静态扫描 plugins.v3/*/*.py，不依赖 MoviePilot 宿主。
# 用法：python3 tests/v3/check_quality.py            报告模式，始终退出 0
#       python3 tests/v3/check_quality.py --strict   超出基线的新问题退出 1
# 基线 BASELINE 记录体检时各插件已存在的问题数；--strict 只允许减少或持平。

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins.v3"

CATEGORIES = (
    ("poll", "注入轮询脚本(setInterval)"),
    ("log_save", "日志函数逐条 save_data"),
    ("net_in_page", "get_page/get_form 网络或宿主 SDK 调用"),
    ("api_no_try", "api_* 方法无 try/except"),
    ("big_html", "页面 HTML/STYLE 常量 > 40KB"),
)
CAT_KEYS = tuple(key for key, _ in CATEGORIES)

HTML_LIMIT = 40 * 1024

NET_MODULES = frozenset({"urllib", "requests", "subprocess", "paramiko"})
NET_NAMES = frozenset({
    "urlopen", "DownloaderHelper", "SiteOper", "SubscribeOper", "MediaServerHelper",
})
PAGE_FUNCS = frozenset({"get_page", "get_form"})
LOG_FUNC_RE = re.compile(
    r"^_?(log|logs|emit|append_log|write_log|add_log|push_log|record_log|"
    r"save_log|debug_log|log_msg|log_message|log_line)(_.*)?$"
)


def _try_nodes():
    nodes = [ast.Try]
    if hasattr(ast, "TryStar"):
        nodes.append(ast.TryStar)
    return tuple(nodes)


TRY_NODES = _try_nodes()

# 基线：体检时各插件已存在的问题数，按分类记；只记非零项，未列出的插件/分类视为 0。
# --strict 时当前值 > 基线即视为新增问题，减少或持平都放行。
# 现状：逐条写库 14、页面宿主 SDK 调用 2、api_* 无兜底 113；轮询脚本/超大常量均为 0。
BASELINE: dict[str, dict[str, int]] = {
    "bandwidthqueuescheduler": {"log_save": 1, "api_no_try": 9},
    "cfrouteros": {"log_save": 1, "api_no_try": 8},
    "librarycheckup": {"api_no_try": 3},
    "medialibraryrefresh": {"log_save": 1, "api_no_try": 9},
    "mpoverview": {"api_no_try": 2},
    "namecrossseed": {"net_in_page": 1},
    "pieceshashreseed": {"net_in_page": 1},
    "playbackscheduler": {"log_save": 1, "api_no_try": 5},
    "pluginautoupdater": {"log_save": 1, "api_no_try": 8},
    "rosfirewallmanager": {"log_save": 1, "api_no_try": 14},
    "routerosconfigguard": {"log_save": 1, "api_no_try": 8},
    "routerosconnectioncenter": {"api_no_try": 3},
    "routerosremoteaccesswizard": {"log_save": 1, "api_no_try": 7},
    "routerosscenariotemplates": {"log_save": 1, "api_no_try": 6},
    "routerossiterouting": {"log_save": 1, "api_no_try": 9},
    "routerosworkbench": {"log_save": 1, "api_no_try": 7},
    "singlesourcetracker": {"log_save": 1},
    "transferaudit": {"api_no_try": 6},
    "ugreenguard": {"log_save": 1},
    "viewingweeklyreport": {"log_save": 1, "api_no_try": 9},
}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _docstring_ids(tree):
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _string_constants(tree):
    docs = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return _dotted(func) or func.attr
    return "?"


def _is_net_call(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in NET_NAMES or func.id in NET_MODULES
    if isinstance(func, ast.Attribute):
        dotted = _dotted(func)
        parts = dotted.split(".")
        return parts[0] in NET_MODULES or bool(NET_NAMES.intersection(parts))
    return False


def _writes_log_data(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "save_data"
    if isinstance(func, ast.Attribute):
        return func.attr == "save_data"
    return False


def scan():
    files = sorted(PLUGIN_ROOT.glob("*/*.py"))
    counts: dict[str, dict[str, int]] = {}
    issues = []
    warnings = []
    for path in files:
        plugin = path.parent.name
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            warnings.append(f"{path.relative_to(ROOT)}: 解析失败 {type(exc).__name__}: {exc}")
            continue
        rel = str(path.relative_to(ROOT))
        counts.setdefault(plugin, {key: 0 for key in CAT_KEYS})

        def add(cat, line, detail):
            counts[plugin][cat] += 1
            issues.append((plugin, cat, rel, line, detail))

        # 1. 注入的 setInterval 轮询脚本；onchange 上传、setTimeout 复制按钮都不算
        for lineno, value in _string_constants(tree):
            if re.search(r"setInterval\s*\(", value):
                extra = "，含 MoviePilotAPI" if "MoviePilotAPI" in value else ""
                add("poll", lineno, f"HTML/JS 字符串里出现 setInterval 轮询脚本{extra}")

        # 5. 页面 HTML / STYLE 常量体积异常
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = getattr(node, "value", None)
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            size = len(value.value.encode("utf-8"))
            if size <= HTML_LIMIT:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            add("big_html", node.lineno, f"常量 {names or '?'} 约 {size} 字节 > {HTML_LIMIT}")

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]

            # 2. 日志函数里直接 save_data（逐条写库）
            if LOG_FUNC_RE.match(name) and any(_writes_log_data(c) for c in calls):
                add("log_save", node.lineno, f"{name}() 里直接调用 save_data")

            # 3. get_page / get_form 里的网络或宿主 SDK 调用；get_data/save_data 缓存不算
            if name in PAGE_FUNCS:
                for call in calls:
                    if _is_net_call(call):
                        add("net_in_page", call.lineno, f"{name}() 调用 {_call_name(call)}")

            # 4. api_* 方法完全没有 try/except
            if name.startswith("api_") and not any(
                    isinstance(n, TRY_NODES) for n in ast.walk(node)):
                add("api_no_try", node.lineno, f"{name}() 无 try/except")
    return files, counts, issues, warnings


def print_report(files, counts, issues, warnings):
    plugins = {f.parent.name for f in files}
    print(f"全仓质量门禁：{len(plugins)} 个插件 / {len(files)} 个 .py 文件")
    print("分类：" + "  ".join(f"{k}={label}" for k, label in CATEGORIES))
    print()
    print("各插件问题数：")
    for plugin in sorted(counts):
        per = counts[plugin]
        total = sum(per.values())
        parts = " ".join(f"{k}={v}" for k, v in per.items() if v)
        print(f"  {plugin:32s} 合计 {total:2d}  {parts or '—'}")
    print()
    print("明细：")
    if not issues:
        print("  （无）")
    for plugin, cat, rel, line, detail in sorted(
            issues, key=lambda x: (x[0], x[1], x[2], x[3])):
        print(f"  [{cat}] {rel}:{line} {detail}")
    totals = {key: sum(c[key] for c in counts.values()) for key in CAT_KEYS}
    print()
    print("合计：" + "  ".join(f"{k}={totals[k]}" for k in CAT_KEYS)
          + f"，共 {sum(totals.values())} 项")
    for item in warnings:
        print(f"  [提示] {item}")


def strict_regressions(counts):
    found = []
    for plugin in sorted(counts):
        base = BASELINE.get(plugin, {})
        for cat in CAT_KEYS:
            cur = counts[plugin][cat]
            allowed = base.get(cat, 0)
            if cur > allowed:
                found.append((plugin, cat, cur, allowed))
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="V3 插件全仓质量门禁")
    parser.add_argument("--strict", action="store_true", help="超出基线的新问题退出 1")
    opts = parser.parse_args(argv)

    files, counts, issues, warnings = scan()
    print_report(files, counts, issues, warnings)

    if not opts.strict:
        print("\n报告模式：不阻塞校验，退出 0。")
        return 0

    regressions = strict_regressions(counts)
    print()
    if regressions:
        print(f"严格模式：{len(regressions)} 项超出基线（只允许减少或持平）")
        for plugin, cat, cur, allowed in regressions:
            print(f"  {plugin} / {cat}：现有 {cur} > 基线 {allowed}")
        return 1
    print("严格模式：无新增问题，全部在基线内，退出 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
