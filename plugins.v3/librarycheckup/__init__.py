"""媒体库体检 —— MoviePilot V3 媒体管理增强插件。

三个页面共用同一套只读采集：整理前先预演目标路径与冲突、跨盘硬链接退化风险；
再用 inode / 设备号核对入库记录，画出 src ↔ dest 链接账本；
最后把 Emby / Jellyfin / Plex 三家的条目对齐，列出缺集与重复。

三个页面全部**只读报告**，零配置（媒体服务器与目录自动发现），
不创建/移动/重命名/删除任何文件，不改动整理历史、下载器任务与媒体服务器。

V3 合同要点（本项目踩过按钮 404 的坑，勿改）：
- Response 必须是 pydantic.BaseModel；普通类会让 FastAPI 注册失败，
  宿主捕获异常后静默跳过路由，按钮点击就是 Not Found；
- get_api() 用相对路径，宿主自动补 /api/v1/plugin/<PluginID>；
- 按钮 api 写 plugin/LibraryCheckup/<action>，method 与 get_api() methods 一致；
- POST 端点用 Body(default=None) 接收前端 params；
- release 必须为 false：本仓库没有 Release 资产。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

try:
    from fastapi import Body

    _BODY_DEFAULT = Body(default=None)
except Exception:  # noqa: BLE001 - 纯文档/离线导入时也能工作
    _BODY_DEFAULT = None  # type: ignore[assignment]

try:
    from app.sdk.plugin import _PluginBase
except Exception:  # noqa: BLE001 - 离线 stub / 文档检查时也能导入
    class _PluginBase:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._data: Dict[str, Any] = {}

from . import collector, common, renderer


class Response(BaseModel):
    """插件 API 统一响应：success / message / data 三段式。

    必须是 Pydantic 模型：endpoint 的返回注解会被 FastAPI 用于响应模型推导，
    普通类会触发 FastAPIError 并导致宿主跳过该路由（按钮 404）。
    """

    success: bool = True
    message: str = ""
    data: Any = None


VIEW_BUTTONS: Tuple[Tuple[str, str, str], ...] = (
    ("preview", "整理预演", "mdi-file-search-outline"),
    ("ledger", "入库账本", "mdi-link-variant"),
    ("parity", "媒体库对比", "mdi-server-network"),
)


class LibraryCheckup(_PluginBase):
    # 类体元数据一律用字符串字面量：官方校验脚本用 AST 直接提取字面量。
    plugin_name = "媒体库体检"
    plugin_desc = (
        "整理前先看看文件会存到哪、会不会覆盖，顺带核对硬链接是不是真链接；"
        "再把 Emby、Jellyfin、Plex 三家的媒体条目对齐，找出缺集和重复。"
        "媒体服务器与目录自动识别，装好即用，明细不截断、日志可复制，全程只读。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/librarycheckup.png"
    plugin_version = "1.0.1"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "librarycheckup_"
    plugin_color = "#6366F1"
    system_version = ">=3.0.0"
    plugin_order = 92

    _DASHBOARD_REFRESH = 120

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._cfg: Dict[str, Any] = {}
        self._view_state = "preview"

    # ------------------------------------------------------------------
    # 生命周期 / 配置（零配置：只有一个可选的预演目录）
    # ------------------------------------------------------------------

    def _effective_config(self) -> Dict[str, Any]:
        """合并宿主持久配置与 init_plugin 传入配置。

        方法名不能叫 _config：宿主/stub 基类有 self._config 字典属性，
        会把同名方法覆盖成 dict，调用时抛 TypeError。
        """
        cfg: Dict[str, Any] = {}
        getter = getattr(self, "get_config", None)
        if callable(getter):
            try:
                remote = getter()
                if isinstance(remote, dict):
                    cfg.update(remote)
            except Exception:  # noqa: BLE001
                pass
        if self._cfg:
            cfg.update(self._cfg)
        return cfg

    def _view(self) -> str:
        """读取当前页面。

        宿主 _PluginBase.get_data 的签名是 get_data(key, plugin_id=None)，
        第二个参数是插件 ID 而**不是默认值**。曾误写成 get_data("view", "preview")，
        等于去读名为 preview 的插件的数据，结果永远读不到、页面永远停在整理预演。
        这里只传 key，并用实例属性再兜一层底。
        """
        try:
            value = self.get_data("view")
        except Exception:  # noqa: BLE001
            value = None
        if value not in collector.VIEWS:
            value = getattr(self, "_view_state", None)
        return value if value in collector.VIEWS else "preview"

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._cfg = dict(config or {})
        self._enabled = bool(self._cfg.get("enabled", True))
        collector.invalidate()
        cfg = collector.parse_config(self._effective_config())
        common.emit(
            "配置已生效：预演目录="
            + ("自定义" if cfg["preview_dir"] else "自动从整理历史推断")
            + "；三个视图全程只读，不会修改任何文件、配置或整理历史。"
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        collector.invalidate()

    def close(self) -> None:
        collector.invalidate()

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        form: List[Dict[str, Any]] = [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                    {"component": "VTextField", "props": {
                        "model": "preview_dir",
                        "label": "预演目录（可选）",
                        "placeholder": "/downloads",
                        "hint": "留空自动从整理历史推断；多个目录用换行或逗号分隔。只读扫描，不会改动文件",
                        "persistentHint": True,
                        "clearable": True,
                    }},
                ]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                    }, "text": (
                        "媒体服务器、下载目录和媒体库目录自动识别，一般不用填任何东西。"
                        "整理预演、入库账本、媒体库对比三页都只是报告，不会删除、移动、"
                        "重命名文件，也不会刷新或写回媒体服务器。首页还能加个组件常看汇总。"
                    )},
                ]},
            ]},
        ]
        return form, collector.parse_config(self._effective_config())

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 用相对路径：宿主会自动补 /api/v1/plugin/LibraryCheckup。
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取三个页面的完整只读快照（JSON）"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "清空缓存并强制重新体检"},
            {"path": "/view", "auth": "bear", "endpoint": self.api_view,
             "methods": ["POST"], "summary": "切换页面"},
            {"path": "/preview", "auth": "bear", "endpoint": self.api_preview,
             "methods": ["POST"], "summary": "切回 A 并重新预演（只读）"},
            {"path": "/log", "auth": "bear", "endpoint": self.api_log,
             "methods": ["GET"], "summary": "读取可复制的操作日志"},
        ]

    def api_snapshot(self) -> Response:
        return Response(success=True, data=collector.collect(
            config=self._effective_config(), view=self._view()))

    def api_refresh(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        collector.invalidate()
        data = collector.collect(force=True, config=self._effective_config(), view=self._view())
        common.emit("用户点了「重新体检」，已强制重新采集（只读）。")
        return Response(success=True, message=f"已重新体检，更新于 {data['generated_at']}",
                        data={"generated_at": data["generated_at"],
                              "html": renderer.render(data),
                              "summary": {
                                  "preview": data["preview"]["summary"],
                                  "ledger": data["ledger"]["summary"],
                                  "parity": data["parity"]["summary"],
                              }})

    def api_view(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        body = payload if isinstance(payload, dict) else {}
        view = str(body.get("view") or "")
        if view not in collector.VIEWS:
            return Response(success=False, message="未知的页面", data={})
        self._view_state = view
        try:
            self.save_data("view", view)
        except Exception:  # noqa: BLE001
            pass
        label = dict((k, t) for k, t, _ in VIEW_BUTTONS).get(view, view)
        common.emit(f"切换到「{label}」（只读报告）。")
        return Response(success=True, message=f"已切换到 {label}", data={"view": view})

    def api_preview(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        self._view_state = "preview"
        try:
            self.save_data("view", "preview")
        except Exception:  # noqa: BLE001
            pass
        collector.invalidate()
        data = collector.collect(force=True, config=self._effective_config(), view="preview")
        common.emit("用户点了「重新预演」，已重扫目录（只读，未改动任何文件）。")
        return Response(success=True,
                        message=f"预演完成：{data['preview']['summary']['total']} 个文件，未改动任何文件",
                        data={"generated_at": data["generated_at"],
                              "html": renderer.render(data)})

    def api_log(self) -> Response:
        return Response(success=True, data={"text": common.log_text(300),
                                           "lines": common.log_lines(300)})

    # ------------------------------------------------------------------
    # 详情页 / 首页仪表盘
    # ------------------------------------------------------------------

    def get_page(self) -> List[Dict[str, Any]]:
        current = self._view()
        data = collector.collect(config=self._effective_config(), view=current)
        elements: List[Dict[str, Any]] = []
        for key, text, icon in VIEW_BUTTONS:
            active = key == current
            elements.append({
                "component": "VBtn",
                "text": text,
                "props": {
                    "color": "primary" if active else "default",
                    "variant": "flat" if active else "tonal",
                    "prependIcon": icon,
                    "class": "mr-2 mb-3",
                    "size": "small",
                },
                "events": {"click": {
                    "api": "plugin/LibraryCheckup/view",
                    "method": "post",
                    "params": {"view": key},
                }},
            })
        elements.append({
            "component": "VBtn",
            "text": "重新体检",
            "props": {
                "color": "primary", "variant": "tonal",
                "prependIcon": "mdi-refresh", "class": "mr-2 mb-3", "size": "small",
            },
            "events": {"click": {
                "api": "plugin/LibraryCheckup/refresh", "method": "post", "params": {},
            }},
        })
        if current == "preview":
            elements.append({
                "component": "VBtn",
                "text": "重新预演",
                "props": {
                    "color": "secondary", "variant": "tonal",
                    "prependIcon": "mdi-file-search-outline", "class": "mb-3", "size": "small",
                },
                "events": {"click": {
                    "api": "plugin/LibraryCheckup/preview", "method": "post", "params": {},
                }},
            })
        elements.append({"component": "div", "props": {"id": "lc-root"},
                         "html": renderer.render(data)})
        return elements

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        return [{"key": "checkup", "name": "媒体库体检"}]

    def get_dashboard(self, key: str = "checkup", **kwargs: Any
                      ) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
        data = collector.collect(config=self._effective_config(), view=self._view())
        cols = {"cols": 12, "md": 8}
        attrs = {
            "refresh": self._DASHBOARD_REFRESH,
            "border": False,
            "title": "媒体库体检",
            "subtitle": "整理预演 · 入库账本 · 媒体库对比",
        }
        elements = [{"component": "div", "html": renderer.render(data, compact=True)}]
        return cols, attrs, elements
