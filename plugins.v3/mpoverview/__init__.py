"""MP 总览 —— MoviePilot V3 功能增强插件（零配置只读看板）。

给 MoviePilot 补一个原生没有的「一页总览」：系统负载 / 存储水位 / 订阅 /
入库 / 下载 / 服务连接 / 插件状态，全部自动从 V3 稳定只读接口获取。

三个入口：
- 首页仪表盘：get_dashboard() + attrs.refresh，宿主按间隔自动重绘；
- 插件详情页：get_page()，点「刷新数据」由宿主 @action 重新拉取页面；
- 只读 API：/snapshot 返回 JSON 快照，/refresh 强制重新采集。

V3 API 合同要点（本项目踩过 404 的坑，勿改）：
- Response 必须是 pydantic.BaseModel；普通 Python 类会让 FastAPI 注册失败，
  宿主捕获异常后静默跳过路由，按钮点击就会 Not Found；
- get_api() 用相对路径，宿主自动补 /api/v1/plugin/<PluginID>；
- 按钮 api 写 plugin/MPOverview/<action>，method 与 get_api() 的 methods 一致；
- release 必须为 false：本仓库没有 Release 资产，true 会先失败再回退。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

try:
    from app.sdk.plugin import _PluginBase
except Exception:  # noqa: BLE001 - 离线 stub / 文档检查时也能导入
    class _PluginBase:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._data: Dict[str, Any] = {}

from . import collector, renderer


def _enabled_from(cfg: Dict[str, Any]) -> bool:
    """设置页「启用插件」的值；没配过就默认开，保持装好即用。"""
    value = cfg.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "off", "no", "")
    return bool(value)


class Response(BaseModel):
    """插件 API 统一响应：success / message / data 三段式。

    必须是 Pydantic 模型：endpoint 的返回注解会被 FastAPI 用于响应模型推导，
    普通类会触发 FastAPIError 并导致宿主跳过该路由（按钮 404）。
    """

    success: bool = True
    message: str = ""
    data: Any = None


class MPOverview(_PluginBase):
    # 类体元数据一律用字符串字面量：官方校验脚本用 AST 直接提取字面量。
    plugin_name = "MPOverview"
    plugin_desc = (
        "给 MoviePilot V3 补一个零配置只读总览看板：一页聚合系统负载、磁盘水位、"
        "订阅状态、近期入库、下载记录、已连接服务与插件状态。"
        "同时提供首页仪表盘组件与自动刷新，不修改任何配置或媒体文件。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/mpoverview.png"
    plugin_version = "1.0.1"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "mpoverview_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 90
    _enabled: bool = True

    _MIN_REFRESH = 30
    _MAX_REFRESH = 300
    _DEFAULT_REFRESH = 60

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._refresh = self._DEFAULT_REFRESH

    # ------------------------------------------------------------------
    # 生命周期 / 配置
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        # 默认启用，保证「装好即用」；宿主显式传 False 时停用。
        self._enabled = _enabled_from(cfg)
        try:
            value = int(cfg.get("refresh_seconds") or self._DEFAULT_REFRESH)
        except (TypeError, ValueError):
            value = self._DEFAULT_REFRESH
        self._refresh = max(self._MIN_REFRESH, min(self._MAX_REFRESH, value))
        collector.invalidate()
        if not self._enabled:
            return

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        collector.invalidate()

    def close(self) -> None:
        collector.invalidate()

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """设置页只有一项：自动刷新间隔。其余全部自动识别。"""
        form: List[Dict[str, Any]] = [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VSwitch", "props": {
                        "model": "enabled", "label": "启用插件", "color": "primary",
                        "hint": "关掉后不再采集数据、不显示首页组件，页面按钮也会停用",
                        "persistentHint": True,
                    }},
                ]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 7}, "content": [
                    {"component": "VSelect", "props": {
                        "model": "refresh_seconds",
                        "label": "首页自动刷新",
                        "items": [
                            {"title": "30 秒", "value": 30},
                            {"title": "60 秒（推荐）", "value": 60},
                            {"title": "2 分钟", "value": 120},
                            {"title": "5 分钟", "value": 300},
                        ],
                        "hint": "仅影响首页仪表盘；详情页随时点「刷新数据」即可",
                        "persistentHint": True,
                    }},
                ]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                    }, "text": (
                        "零配置：媒体服务器、下载器、订阅、入库与插件状态全部自动读取，"
                        "无需填写地址和密钥。本插件只读，不会修改任何配置或媒体文件。"
                        "在首页仪表盘添加「MP 总览」组件即可常驻查看。"
                    )},
                ]},
            ]},
        ]
        return form, {"enabled": self._enabled, "refresh_seconds": self._refresh}

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 用相对路径：宿主会自动补 /api/v1/plugin/MPOverview。
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取当前只读快照（JSON）"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "清空缓存并强制重新采集"},
        ]

    def api_snapshot(self) -> Response:
        if not self._enabled:
            return Response(success=False, message="插件已停用，请到设置页打开「启用插件」")
        return Response(success=True, data=collector.collect())

    def api_refresh(self) -> Response:
        if not self._enabled:
            return Response(success=False, message="插件已停用，请到设置页打开「启用插件」")
        collector.invalidate()
        data = collector.collect(force=True)
        return Response(
            success=True,
            message=f"已重新采集，更新于 {data['generated_at']}",
            data={
                "generated_at": data["generated_at"],
                "html": renderer.render(data),
            },
        )

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def get_page(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return [{"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "title": "插件已停用"},
                "text": "到「设置」里打开「启用插件」后，本页的按钮和数据才会恢复。"}]
        data = collector.collect()
        return [
            {
                "component": "VBtn",
                # 按钮文字必须放组件顶层 text；放进 props.text 会被 Vuetify 当成 variant。
                "text": "刷新数据",
                "props": {
                    "color": "primary",
                    "variant": "tonal",
                    "prependIcon": "mdi-refresh",
                    "class": "mb-4",
                },
                "events": {
                    "click": {
                        "api": "plugin/MPOverview/refresh",
                        "method": "post",
                        "params": {},
                    }
                },
            },
            {"component": "div", "props": {"id": "mpov-root"}, "html": renderer.render(data)},
        ]

    # ------------------------------------------------------------------
    # 首页仪表盘
    # ------------------------------------------------------------------

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        if not self._enabled:
            return []
        return [{"key": "overview", "name": "MP 总览"}]

    def get_dashboard(
        self, key: str = "overview", **kwargs: Any
    ) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
        if not self._enabled:
            return ({"cols": 12, "md": 8},
                    {"border": False, "title": "MP 总览", "subtitle": "插件已停用"},
                    [{"component": "div", "html":
                      "<div style='padding:12px;opacity:.7'>插件已停用，到设置里打开「启用插件」后恢复。</div>"}])
        data = collector.collect()
        cols = {"cols": 12, "md": 8}
        attrs = {
            "refresh": self._refresh,
            "border": False,
            "title": "MP 总览",
            "subtitle": "系统 · 存储 · 订阅 · 入库",
        }
        elements = [{"component": "div", "html": renderer.render(data, compact=True)}]
        return cols, attrs, elements
