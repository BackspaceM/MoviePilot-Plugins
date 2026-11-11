"""入库对账 —— MoviePilot V3 源文件对账与可控清理插件。

面向 PT 连载番剧的真实场景：单集做种，完结后出合集打包，站点要求删掉单集种子，
于是「种子删了、源文件删了或还留着、媒体库里硬链接入库的文件还在」。

两条互补的核对路径：
1. 整理历史驱动：逐条比对「源 ↔ 库 ↔ 种子状态」，按剧集/季聚合；
2. 主动扫描驱动：读取下载器完整种子名录，遍历映射路径里的实体源文件，
   找出「种子已删但源文件还在」的残留，并给出可复制的种子名称清单与操作日志。

默认全程只读。清理源文件必须用户在设置里显式开启，且严格走
「扫描出完整列表 → 生成删除预览（不删任何文件） → 二次确认 → 分批执行」，
默认移入隔离目录可恢复；下载器查不通或路径映射可疑时整批拒绝。

V3 合同要点（本项目踩过按钮 404 的坑，勿改）：
- Response 必须是 pydantic.BaseModel；普通类会让 FastAPI 注册失败，
  宿主捕获异常后静默跳过路由，按钮点击就是 Not Found；
- get_api() 用相对路径，宿主自动补 /api/v1/plugin/<PluginID>；
- 按钮 api 写 plugin/TransferAudit/<action>，method 与 get_api() methods 一致；
- POST 端点用 Body(default=None) 接收前端 params，返回注解保持 Response；
- release 必须为 false：本仓库没有 Release 资产，true 会先失败再回退。
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

from . import auditor, renderer, scanner


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
        "对比下载器种子名录与映射目录里的源文件，找出 PT 打包删种后的源文件残留；"
        "全量清单含可复制的种子名称与操作日志。默认只读，开启后按"
        "「扫描 → 预览 → 二次确认」分批清理源文件，绝不触碰媒体库文件。"
    )
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/transferaudit.png"
    plugin_version = "1.1.0"
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
        self._cfg: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 生命周期 / 配置
    # ------------------------------------------------------------------

    def _effective_config(self) -> Dict[str, Any]:
        """合并宿主持久配置与 init_plugin 传入的配置，缺一不可时安全降级。"""
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

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._cfg = cfg
        self._enabled = bool(cfg.get("enabled", True))
        auditor.invalidate()
        scanner.emit(
            "配置已生效："
            f"删除能力={'开启' if cfg.get('allow_delete') else '关闭（只读）'}，"
            f"删除方式={cfg.get('delete_mode') or 'trash'}，"
            f"扫描目录={'自定义' if cfg.get('scan_dirs') else '自动推断'}，"
            f"路径映射={'已配置' if cfg.get('path_map') else '无'}。"
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        auditor.invalidate()
        scanner.clear_plan()

    def close(self) -> None:
        auditor.invalidate()
        scanner.clear_plan()

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """精简设置：核心只有「扫描目录 / 路径映射 / 删除开关 / 删除方式」。"""
        defaults: Dict[str, Any] = {
            "enabled": True,
            "scan_dirs": "",
            "path_map": "",
            "allow_delete": False,
            "delete_mode": "trash",
        }
        form: List[Dict[str, Any]] = [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal"},
                "text": (
                    "装好即用：留空即自动从下载器保存路径与整理历史推断扫描目录。"
                    "详情页会完整列出所有异常项、种子名称清单与操作日志，可直接复制到下载器核对。"
                ),
            },
            {"component": "VSwitch", "props": {
                "model": "enabled", "label": "启用插件", "color": "primary",
                "hint": "关闭后不再采集与展示对账结果",
            }},
            {"component": "VTextarea", "props": {
                "model": "scan_dirs", "label": "源目录（容器内可见路径，每行一个）",
                "rows": 3, "clearable": True, "autoGrow": True,
                "placeholder": "/downloads\n/volume1/media/downloads",
                "hint": "留空自动推断。只扫这些目录里的媒体文件；系统目录与一级目录会被拒绝",
                "persistent-hint": True,
            }},
            {"component": "VTextarea", "props": {
                "model": "path_map", "label": "路径映射（下载器路径=容器路径，每行一条）",
                "rows": 2, "clearable": True, "autoGrow": True,
                "placeholder": "/volume1/download=/downloads",
                "hint": "下载器与 MoviePilot 容器路径不一致时必填，否则种子名录匹配不上，插件会自动禁用删除",
                "persistent-hint": True,
            }},
            {"component": "VDivider", "props": {"class": "my-4"}},
            {
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal"},
                "text": (
                    "⚠️ 危险区：开启后插件才会具备删除源文件的能力，且每次删除都必须"
                    "先生成预览并二次确认。默认移入隔离目录可恢复；选「直接删除」不可恢复。"
                    "不开启时插件全程只读。"
                ),
            },
            {"component": "VSwitch", "props": {
                "model": "allow_delete", "label": "启用源文件删除能力", "color": "error",
                "hint": "默认关闭。开启后详情页出现高危提示与删除按钮，仍须逐次确认",
                "persistent-hint": True,
            }},
            {"component": "VSelect", "props": {
                "model": "delete_mode", "label": "删除方式",
                "items": [
                    {"title": "移入隔离目录（可恢复，推荐）", "value": "trash"},
                    {"title": "直接删除（不可恢复）", "value": "permanent"},
                ],
                "hint": "隔离目录位于扫描根目录下 .transferaudit-trash/<时间>/，可自行还原或清空",
                "persistent-hint": True,
            }},
        ]
        return form, defaults

    # ------------------------------------------------------------------
    # 插件 API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 用相对路径：宿主会自动补 /api/v1/plugin/TransferAudit。
        return [
            {"path": "/snapshot", "auth": "bear", "endpoint": self.api_snapshot,
             "methods": ["GET"], "summary": "读取完整对账快照（JSON）"},
            {"path": "/refresh", "auth": "bear", "endpoint": self.api_refresh,
             "methods": ["POST"], "summary": "强制重新对账整理历史与映射目录"},
            {"path": "/scan", "auth": "bear", "endpoint": self.api_scan,
             "methods": ["POST"], "summary": "重新扫描下载器种子名录与映射目录"},
            {"path": "/log", "auth": "bear", "endpoint": self.api_log,
             "methods": ["GET"], "summary": "读取可复制的操作日志"},
            {"path": "/prepare", "auth": "bear", "endpoint": self.api_prepare,
             "methods": ["POST"], "summary": "生成删除预览（不删除任何文件）"},
            {"path": "/delete", "auth": "bear", "endpoint": self.api_delete,
             "methods": ["POST"], "summary": "二次确认后执行源文件清理"},
        ]

    def api_snapshot(self) -> Response:
        return Response(success=True, data=auditor.collect(config=self._effective_config()))

    def api_refresh(self, _: Any = None) -> Response:
        auditor.invalidate()
        data = auditor.collect(force=True, config=self._effective_config())
        return Response(
            success=True,
            message=(
                f"已重新对账 {data['scanned']} 条整理记录、扫描 {data['file_total']} 个源文件，"
                f"更新于 {data['generated_at']}"
            ),
            data={
                "generated_at": data["generated_at"],
                "scanned": data["scanned"],
                "file_total": data["file_total"],
                "torrent_total": data["torrent_total"],
                "counts": data["counts"],
                "html": renderer.render(data),
            },
        )

    def api_scan(self, _: Any = None) -> Response:
        auditor.invalidate()
        data = auditor.collect(force=True, config=self._effective_config())
        scan = data["scan"]
        return Response(
            success=True,
            message=(
                f"扫描完成：目录文件 {data['file_total']} 个，种子名录 {data['torrent_total']} 条，"
                f"可清理候选 {len(data['candidates'])} 个。{scan['reason']}"
            ),
            data={
                "scan": scan,
                "file_total": data["file_total"],
                "torrent_total": data["torrent_total"],
                "candidates": data["candidates"],
                "seed_list": data["seed_list"],
                "checklist": data["checklist"],
                "html": renderer.render(data),
            },
        )

    def api_log(self) -> Response:
        return Response(success=True, data=scanner.log_text(400))

    def api_prepare(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        """第一步：只生成预览，绝不删除或移动任何文件。"""
        body = payload if isinstance(payload, dict) else {}
        cfg = scanner.parse_config(self._effective_config())
        result = scanner.prepare(
            cfg,
            token=str(body.get("token") or ""),
            indices=body.get("indices") or [],
            scope=str(body.get("scope") or "selected"),
        )
        return Response(success=bool(result["ok"]), message=result["message"],
                        data=result.get("data"))

    def api_delete(self, payload: Optional[dict] = _BODY_DEFAULT) -> Response:
        """第二步：只有携带正确确认口令与有效预览才执行，并立刻失效缓存。"""
        body = payload if isinstance(payload, dict) else {}
        cfg = scanner.parse_config(self._effective_config())
        result = scanner.execute(
            cfg,
            plan_id=str(body.get("plan_id") or ""),
            confirm=str(body.get("confirm") or ""),
        )
        auditor.invalidate()
        return Response(success=bool(result["ok"]), message=result["message"],
                        data=result.get("data"))

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def get_page(self) -> List[Dict[str, Any]]:
        raw = self._effective_config()
        cfg = scanner.parse_config(raw)
        data = auditor.collect(config=raw)
        token = str(data.get("scan_token") or "")
        plan = data.get("plan") or {}

        page: List[Dict[str, Any]] = [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VBtn", "text": "重新扫描",
                    "props": {"color": "primary", "variant": "tonal",
                              "prependIcon": "mdi-refresh", "block": True, "class": "mb-2"},
                    "events": {"click": {
                        "api": "plugin/TransferAudit/refresh", "method": "post", "params": {},
                    }},
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VBtn", "text": "扫描下载器 / 映射目录",
                    "props": {"color": "primary", "variant": "outlined",
                              "prependIcon": "mdi-folder-search", "block": True, "class": "mb-2"},
                    "events": {"click": {
                        "api": "plugin/TransferAudit/scan", "method": "post", "params": {},
                    }},
                }]},
            ]},
        ]

        if cfg["allow_delete"]:
            page.append({"component": "VBtn", "text": "① 生成删除预览（不删除任何文件）",
                         "props": {"color": "warning", "variant": "tonal",
                                   "prependIcon": "mdi-file-document-outline", "class": "mb-2 mr-2"},
                         "events": {"click": {
                             "api": "plugin/TransferAudit/prepare", "method": "post",
                             "params": {"scope": "all", "token": token},
                         }}})
            if plan.get("id"):
                page.append({
                    "component": "VBtn",
                    "text": f"② 确认删除 {plan.get('count', 0)} 个源文件（危险 · 需谨慎）",
                    "props": {"color": "error", "variant": "tonal",
                              "prependIcon": "mdi-alert", "class": "mb-2"},
                    "events": {"click": {
                        "api": "plugin/TransferAudit/delete", "method": "post",
                        "params": {"plan_id": plan["id"], "confirm": scanner.DELETE_CONFIRM},
                    }},
                })

        page.append({"component": "div", "html": renderer.render(data)})
        return page

    # ------------------------------------------------------------------
    # 首页仪表盘
    # ------------------------------------------------------------------

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        return [{"key": "audit", "name": "入库对账"}]

    def get_dashboard(
        self, key: str = "audit", **kwargs: Any
    ) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
        data = auditor.collect(config=self._effective_config())
        cols = {"cols": 12, "md": 8}
        attrs = {
            "refresh": self._DASHBOARD_REFRESH,
            "border": False,
            "title": "入库对账",
            "subtitle": "源文件 · 媒体库 · 种子",
        }
        elements = [{"component": "div", "html": renderer.render(data, compact=True)}]
        return cols, attrs, elements
