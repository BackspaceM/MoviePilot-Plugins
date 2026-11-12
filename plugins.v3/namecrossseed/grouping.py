"""下载器种子去重分组与发布名清洗。

插件面对的是「同一份文件在多个站点各有一个辅种」的下载器：hash 不同、名称略有差异、
体积与内容完全一样。若逐条拿去站点搜，会把同一份内容反复搜索、反复尝试添加。

这里做两件事：

1. ``clean_name``：按用户可配置的正则规则清洗发布名，解决站点标题用空格、
   本地发布名用点/连字符/中文前缀的问题；规则无效时安全跳过，绝不抛异常中断整轮。
2. ``group_key``：把「规范化标题 + 总体积」当作内容身份，同一内容的多个辅种
   归并成一组，每组只搜一次站点、只添加一次。

本模块只依赖标准库与 site 模块，不访问宿主、不访问网络。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, List, Tuple

from .site import build_search_keys, name_tokens

# <space> 表示一个空格，留空 / <empty> 表示删除。
# 规则右侧会被 strip，所以不用字面空格，避免用户看不出填了什么。
DEFAULT_RULES_TEXT = """\
# 每行一条：正则 => 替换文本（<space> = 空格，留空 = 删除，支持 \\1 反向引用）
# 这里填了任何规则，就会完全覆盖上面的内置规则
[._]+ => <space>
\\[[^\\]]*\\] => <empty>
【[^】]*】 => <empty>
-[A-Za-z0-9@._]{2,16}$ => <empty>
"""

_RULE_SPLIT = "=>"
_SPACE = "<space>"
_EMPTY_MARKERS = frozenset({"", "<empty>", "<none>", "<blank>"})


def parse_rules(text: Any) -> List[Tuple[str, str]]:
    """把多行配置解析成 (正则, 替换文本) 列表；忽略注释与格式错误的行。"""
    rules: List[Tuple[str, str]] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _RULE_SPLIT not in line:
            continue
        pattern, replacement = line.split(_RULE_SPLIT, 1)
        pattern = pattern.strip()
        if not pattern:
            continue
        replacement = replacement.strip()
        # <space> 可以单独用，也可以嵌在替换文本里（例如 \1<space>），
        # 否则 strip 会把用户想保留的空格吃掉。
        replacement = replacement.replace(_SPACE, " ")
        if replacement.lower() in _EMPTY_MARKERS:
            replacement = ""
        rules.append((pattern, replacement))
    return rules


def invalid_rules(text: Any) -> List[str]:
    """返回无法编译的正则，供设置页提示；不抛异常。"""
    bad: List[str] = []
    for pattern, _ in parse_rules(text):
        try:
            re.compile(pattern)
        except re.error:
            bad.append(pattern)
    return bad


def clean_name(name: Any, rules_text: Any = "") -> str:
    """按规则清洗发布名；用户没填则用内置规则，最后统一压缩空格。

    任何一条规则出错都只跳过这一条，不会中断辅种任务。
    """
    original = str(name or "")
    if not original:
        return ""
    text = str(rules_text or "").strip()
    rules = parse_rules(text) if text else parse_rules(DEFAULT_RULES_TEXT)
    cleaned = original
    for pattern, replacement in rules:
        try:
            cleaned = re.sub(pattern, replacement, cleaned)
        except (re.error, TypeError, ValueError):
            continue
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 规则写错把名字洗没了，回退原名，宁可搜不到也不要误拼
    return cleaned or original


def search_keys(name: Any, rules_text: Any = "") -> List[str]:
    """由清洗后的发布名生成按优先级排列的搜索词。"""
    cleaned = clean_name(name, rules_text)
    if not cleaned:
        return []
    keys = build_search_keys(cleaned)
    # 清洗后的短名也作为一次尝试：用户的正则常常就是为了拼出这个写法
    short = " ".join(name_tokens(cleaned)[:6])
    if short and short not in keys:
        keys.append(short)
    # 中英混合发布名（如「武神主宰.The.God.of.War...」）在站点上往往只有英文标题，
    # 自动补一条纯英文写法，避免开箱即用时第一条就搜不到；正则框可进一步自定义。
    if re.search(r"[\u4e00-\u9fff]", cleaned):
        latin = " ".join(name_tokens(re.sub(r"[\u4e00-\u9fff]+", " ", cleaned))[:6])
        if latin and latin not in keys:
            keys.append(latin)
    return keys


def normalize_title(name: Any, rules_text: Any = "") -> str:
    """内容身份里的标题部分：去技术规格、去组名后缀、小写。"""
    cleaned = clean_name(name, rules_text)
    tokens = name_tokens(cleaned)
    if tokens:
        return " ".join(tokens[:6])
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def group_key(name: Any, size: Any, rules_text: Any = "", fallback: str = "") -> str:
    """同一内容的多个辅种返回同一个键。

    体积取不到时不能只按标题归并（同名不同版本会误合并），
    此时用 fallback（种子 hash）让它们各自成组，宁可多搜也不误合并。
    """
    title = normalize_title(name, rules_text)
    try:
        size_int = int(size or 0)
    except (TypeError, ValueError):
        size_int = 0
    if size_int <= 0:
        return f"single|{fallback or title}"
    return f"group|{title}|{size_int}"


def short_key(key: str) -> str:
    """把长组键压成适合做缓存 key 的短签名。"""
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:20]
