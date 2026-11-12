"""把 NAS 异常交给 MoviePilot 自带的通知模块。

按 V3 宿主 app/sdk/plugin/base.py 的签名：

    post_message(channel: Optional[NotificationChannel] = None,
                 mtype: Optional[MessageType] = None,
                 title: Optional[str] = None, text: Optional[str] = None, ...)

内部走 self.chain.post_message(Message(...))，沿用宿主通知开关和收件人路由。
渠道由 MP「设置 → 通知」里已启用的服务决定（微信 / Telegram / WebPush 等），
插件不自己实现推送。枚举解析和发送都做兜底，拿不到就退回最简调用。
"""

from __future__ import annotations

import datetime as _dt
import importlib
from typing import Any, Dict, List, Optional, Tuple

# NotificationChannel 枚举名；空串表示跟随宿主设置，不指定单一渠道
CHANNELS: Tuple[Tuple[str, str], ...] = (
    ("", "跟随 MP 设置（所有已启用渠道）"),
    ("Wechat", "微信"),
    ("WechatClawBot", "微信 ClawBot"),
    ("Telegram", "Telegram"),
    ("WebPush", "WebPush 浏览器推送"),
    ("AppPush", "App / 主屏推送"),
    ("Web", "网页通知"),
    ("Feishu", "飞书"),
    ("DingTalk", "钉钉"),
    ("Slack", "Slack"),
    ("Discord", "Discord"),
    ("QQ", "QQ"),
    ("SynologyChat", "群晖 Chat"),
    ("VoceChat", "VoceChat"),
)
CHANNEL_VALUES = tuple(value for value, _label in CHANNELS)
CHANNEL_LABELS = dict(CHANNELS)
# MessageType.Plugin = "插件"，归到 MP 通知开关的「插件」场景
MTYPE_NAME = "Plugin"
MTYPE_LABEL = "插件"


def _now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enums() -> Tuple[Optional[Any], Optional[Any]]:
    """解析 V3 的 MessageType / NotificationChannel；旧宿主或离线桩下返回 (None, None)。"""
    for module_name in ("app.schemas.types", "app.sdk.events", "app.sdk.services"):
        try:
            module = importlib.import_module(module_name)
            mtype = getattr(module, "MessageType", None)
            channel = getattr(module, "NotificationChannel", None)
            if mtype is not None and channel is not None:
                return mtype, channel
        except Exception:
            continue
    return None, None


def message_kwargs(channel_name: str = "") -> Dict[str, Any]:
    """组装 post_message 的 channel / mtype 关键字参数，缺哪个就不传哪个。"""
    mtype_cls, channel_cls = enums()
    kwargs: Dict[str, Any] = {}
    if mtype_cls is not None:
        mtype = getattr(mtype_cls, MTYPE_NAME, None)
        if mtype is not None:
            kwargs["mtype"] = mtype
    if channel_cls is not None and channel_name and channel_name in CHANNEL_VALUES:
        channel = getattr(channel_cls, channel_name, None)
        if channel is not None:
            kwargs["channel"] = channel
    return kwargs


def channel_label(channel_name: str) -> str:
    return CHANNEL_LABELS.get(channel_name or "", CHANNEL_LABELS[""])


def send(plugin: Any, title: str, text: str,
         channel_name: str = "") -> Dict[str, Any]:
    """调用基类 post_message，返回一条可展示的回执。

    post_message 返回 None，所以「成功」只表示已提交给宿主通知链，
    不代表微信 / Telegram / WebPush 一定已送达；这点必须写清楚。
    """
    result: Dict[str, Any] = {
        "ok": False,
        "at": _now_text(),
        "channel": channel_name or "",
        "channel_label": channel_label(channel_name),
        "mtype": MTYPE_LABEL,
        "via": "",
        "error": "",
        "test": False,
    }
    fn = getattr(plugin, "post_message", None)
    if not callable(fn):
        result["error"] = "宿主没有 post_message，通知模块不可用"
        return result

    kwargs = message_kwargs(channel_name)
    first_error = ""
    try:
        fn(title=title, text=text, **kwargs)
        result["ok"] = True
        result["via"] = "post_message(channel/mtype)" if kwargs else "post_message(title/text)"
        return result
    except TypeError as exc:
        first_error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        first_error = f"{type(exc).__name__}: {exc}"

    # 签名不认 channel / mtype 时退回最简调用，别让通知整个哑掉
    try:
        fn(title=title, text=text)
        result["ok"] = True
        result["via"] = "post_message(title/text 回退)"
        result["error"] = ""
        return result
    except Exception as exc:
        result["error"] = (f"{first_error}；简版也失败：{type(exc).__name__}: {exc}")[:300]
    return result


def _as_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    for attr in ("model_dump", "dict"):
        fn = getattr(item, attr, None)
        if callable(fn):
            try:
                value = fn()
                if isinstance(value, dict):
                    return dict(value)
            except Exception:
                pass
    raw = getattr(item, "__dict__", None)
    return dict(raw) if isinstance(raw, dict) else {}


def services() -> List[Dict[str, Any]]:
    """只读列出宿主已配置的通知服务；拿不到就返回空列表，绝不抛。"""
    try:
        from app.sdk.services import NotificationHelper

        configs = NotificationHelper().get_configs()
        if isinstance(configs, dict):
            items: Any = list(configs.values())
        elif isinstance(configs, (list, tuple)):
            items = list(configs)
        else:
            return []
        out: List[Dict[str, Any]] = []
        for item in items:
            data = _as_dict(item)
            if not data:
                continue
            name = str(data.get("name") or data.get("title") or data.get("id") or "未命名")
            ctype = str(data.get("type") or data.get("channel") or "未知类型")
            enabled = data.get("enabled")
            if enabled is None:
                state = "未标注"
            elif isinstance(enabled, str):
                state = "启用" if enabled.strip().lower() in ("true", "1", "yes", "on") else "停用"
            else:
                state = "启用" if bool(enabled) else "停用"
            out.append({"name": name, "type": ctype, "state": state})
            if len(out) >= 20:
                break
        return out
    except Exception:
        return []
