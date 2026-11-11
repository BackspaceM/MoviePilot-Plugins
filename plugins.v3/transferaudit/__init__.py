"""入库对账 —— MoviePilot V3 零配置只读对账插件。

面向 PT 连载番剧的真实场景：单集做种，完结后出合集打包，站点要求删掉单集种子，
于是出现「种子删了、源文件删了或还留着、但媒体库里硬链接入库的文件还在」。
本插件把每条整理历史与磁盘现状、下载器种子列表逐条核对，按剧集/季聚合：

- 源已删 · 库仍在：打包删种后的孤儿入库，已无法继续做种；
- 源残留 · 种已删：源文件还占着空间，可核对后自行清理；
- 库文件缺失 / 双端已无 / 路径不可核验 等异常状态一并列出。

三个入口：首页仪表盘、插件详情页（含「重新扫描」按钮）、只读 /snapshot API。

V3 合同要点（本项目踩过按钮 404 的坑，勿改）：
- Response 必须是 pydantic.BaseModel；普通类会让 FastAPI 注册失败，
  宿主捕获异常后静默跳过路由，按钮点击就是 Not Found；
- get_api() 用相对路径，宿主自动补 /api/v1/plugin/<PluginID>；
- 按钮 api 写 plugin/TransferAudit/<action>，method 与 get_api() methods 一致；
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

from . import auditor, renderer


class Response(BaseModel):
    """插件 API 统一响应：success / message / data 三段式。

    必须是 Pydantic 模型：endpoint 的返回注解会被 FastAPI 用于响应模型推导，
    普通类会触发 FastAPIError 并导致宿主跳过该路由（按钮 404）。
    """

    success: bool = True
    message: str = ""
    data: Any = None


class TransferAudit(_PluginBase):
    # 类体元数据一律用字符串字面量：官方校验脚本用 AST 直接提取字面量。
    plugin_name = "入库对账"
    plugin_desc = (
        "零配置只读对账：逐条比对整理历史与源文件、媒体库文件、下载器种子，"
        "按剧集/季聚合找出打包删种后「源已删库仍在」的孤儿入库，"
        "以及「种子已删但源文件仍占空间」的残留。只输出报告，不删除、不移动任何文件。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/transferaudit.png"
    plugin_version = "1.0.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "transferaudit_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 91

    _DASHBOARD_REFRESH = 120

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True

    # ------------------------------------------------------------------
    # 生命周期 / 配置
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        # 装好即用：没有任何必填项，宿主显式传 False 时才停用。
        cfg = dict(config or {})
        self._enabled = bool(cfg.get("enabled", True))
        auditor.invalidate()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        auditor.invalidate()

    def close(self) -> None:
        auditor.invalidate()

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """零配置：一个输入控件都没有，只放一句使用说明。"""
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal"},
                "text": (
                    "无需任何设置。自动读取 MoviePilot 已配置的整理历史、存储与下载器，"
                    "打开插件详情页即可查看对账结果；也可在首页仪表盘添加「入库对账」组件。"
                    "本插件全程只读，只生成报告，不会删除或移动任何文件、不会改动下载器任务。"
                ),
            }
        ], {}

    # ------------------------------------------------------------------
    # 插件 API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 用相对路径：宿主会自动补 /api/v1/plugin/TransferAudit。
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取当前只读对账快照（JSON）"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "清空缓存并强制重新对账"},
        ]

    def api_snapshot(self) -> Response:
        return Response(success=True, data=auditor.collect())

    def api_refresh(self) -> Response:
        auditor.invalidate()
        data = auditor.collect(force=True)
        return Response(
            success=True,
            message=f"已重新对账 {data['scanned']} 条整理记录，更新于 {data['generated_at']}",
            data={
                "generated_at": data["generated_at"],
                "scanned": data["scanned"],
                "counts": data["counts"],
                "html": renderer.render(data),
            },
        )

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def get_page(self) -> List[Dict[str, Any]]:
        data = auditor.collect()
        return [
            {
                "component": "VBtn",
                # 按钮文字必须放组件顶层 text；放进 props.text 会被 Vuetify 当成 variant。
                "text": "重新扫描",
                "props": {
                    "color": "primary",
                    "variant": "tonal",
                    "prependIcon": "mdi-refresh",
                    "class": "mb-4",
                },
                "events": {
                    "click": {
                        "api": "plugin/TransferAudit/refresh",
                        "method": "post",
                        "params": {},
                    }
                },
            },
            {"component": "div", "html": renderer.render(data)},
        ]

    # ------------------------------------------------------------------
    # 首页仪表盘
    # ------------------------------------------------------------------

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        return [{"key": "audit", "name": "入库对账"}]

    def get_dashboard(
        self, key: str = "audit", **kwargs: Any
    ) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
        data = auditor.collect()
        cols = {"cols": 12, "md": 8}
        attrs = {
            "refresh": self._DASHBOARD_REFRESH,
            "border": False,
            "title": "入库对账",
            "subtitle": "源文件 · 媒体库 · 种子",
        }
        elements = [{"component": "div", "html": renderer.render(data, compact=True)}]
        return cols, attrs, elements
