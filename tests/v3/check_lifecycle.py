"""离线检查插件能不能被宿主实例化，免得装到一半才发现少方法。

V3 的 _PluginBase 是 ABCMeta，六个抽象方法缺一个就会 TypeError，
重载不 ACTIVE 时整次安装直接回滚。契约脚本只管路由，这里补上这一层。
用法：python3 tests/v3/check_lifecycle.py
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins.v3"
REQUIRED = {"init_plugin", "get_state", "get_api", "get_form", "get_page", "stop_service"}


def class_info(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any("_PluginBase" in ast.unparse(base) for base in node.bases):
            continue
        methods = {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        fields = {}
        for item in node.body:
            if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                try:
                    fields[item.targets[0].id] = ast.literal_eval(item.value)
                except (ValueError, TypeError):
                    continue
        return node.name, methods, fields
    return None, set(), {}


def main() -> int:
    try:
        package = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"清单读不了：{exc}")
        return 1

    failures, warnings = [], []
    for directory in sorted(p for p in PLUGIN_ROOT.iterdir() if (p / "__init__.py").is_file()):
        name, methods, fields = class_info(directory / "__init__.py")
        if not name:
            failures.append(f"{directory.name}: 没找到继承 _PluginBase 的主类")
            continue
        missing = sorted(REQUIRED - methods)
        if missing:
            failures.append(f"{name}: 缺少宿主要求的接口 {missing}")
        if directory.name != name.lower():
            failures.append(f"{name}: 目录名应为 {name.lower()}，实际 {directory.name}")
        entry = package.get(name)
        if not entry:
            warnings.append(f"{name}: 清单里没有这个插件")
        elif str(entry.get("version")) != str(fields.get("plugin_version")):
            warnings.append(
                f"{name}: 清单 {entry.get('version')} 与类内 {fields.get('plugin_version')} 不一致"
            )
        print(f"  {name:32s} 接口 {len(REQUIRED) - len(missing)}/{len(REQUIRED)}  "
              f"{'OK' if not missing and directory.name == name.lower() else 'FAIL'}")

    for item in warnings:
        print(f"  [提示] {item}")
    if failures:
        print(f"发现 {len(failures)} 个问题：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("全部通过：主类、目录名与宿主要求的六个接口一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
