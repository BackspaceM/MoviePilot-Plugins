"""RouterOS 连接中心（MoviePilot V3 插件）。

解决 4 个 RouterOS 插件「各填一遍地址/账号/密码、字段名还不统一」的门槛：
1. 一处保存 1~N 台 RouterOS 连接，自动广播给同目录下的其他 RouterOS 插件；
2. 其余插件设置页出现「已保存路由器」下拉，选中即自动带入连接参数，不选则行为不变；
3. 一键体检：配置完整性 → 端口连通 → REST 可用性 → 认证权限 → 版本 → 业务分区读权限，
   失败项直接给出可复制到 RouterOS 终端执行的修复命令。

配置真相源是本插件自身的配置（由宿主持久化），广播文件只是通道，可随时重建。
不使用任何宿主私有 API，只用标准库，便于离线测试。
"""

from __future__ import annotations

import concurrent.futures
import html as _html
from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

try:
    from app import schemas

    _Response = schemas.Response
except Exception:  # noqa: BLE001 - 离线桩环境
    try:
        from pydantic import BaseModel
    except Exception:  # noqa: BLE001
        BaseModel = object  # type: ignore

    class _Response(BaseModel):  # type: ignore
        success: bool = True
        message: str = ""
        data: Any = None


from . import roslink
from .prober import LEVEL_FAIL, LEVEL_INFO, LEVEL_OK, LEVEL_WARN, collect_fixes, probe_one

PLUGIN_ID = "RouterOSConnectionCenter"
_LOG_PREFIX = "连接中心"
_KEY_PROBE = "rcc_probe_result"

_LEVEL_COLOR = {
    LEVEL_OK: "success",
    LEVEL_WARN: "warning",
    LEVEL_FAIL: "error",
    LEVEL_INFO: "info",
}
_LEVEL_ICON = {
    LEVEL_OK: "mdi-check-circle",
    LEVEL_WARN: "mdi-alert-circle",
    LEVEL_FAIL: "mdi-close-circle",
    LEVEL_INFO: "mdi-information",
}


def _esc(value: Any) -> str:
    """动态文本转义：防 XSS，并避免 [xxx] 被前端当 Markdown 参考式链接渲染。"""
    return (_html.escape(str(value if value is not None else ""), quote=False)
            .replace("[", "［").replace("]", "］"))


class RouterOSConnectionCenter(_PluginBase):
    # 类级元数据一律用字符串字面量：官方版本检查以 AST 提取，常量引用会被判为「未声明」
    plugin_name = "连接中心"
    plugin_desc = ("一处保存多台 RouterOS 连接并自动同步给同库的 RouterOS 插件（设置页下拉即可选择，"
                   "不用再逐插件重复填地址/账号/密码）；内置一键体检，逐项检查端口、REST、权限与版本，"
                   "失败时给出可直接复制到 RouterOS 终端执行的修复命令。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/routerosconnectioncenter.png"
    plugin_version = "1.0.3"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_config_prefix = "routerosconnectioncenter_"
    plugin_color = "#1976D2"
    system_version = ">=3.0.0"
    plugin_order = 50

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._routers: List[Dict[str, Any]] = []
        self._errors: List[str] = []
        self._timeout: float = 8.0
        self._verify: bool = False
        self._broadcast: bool = True
        self._deep: bool = True
        self._shared_path: str = ""
        self._broadcast_ok: bool = False
        self._broadcast_error: str = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._verify = bool(cfg.get("verify", False))
        self._broadcast = bool(cfg.get("broadcast_enabled", True))
        self._deep = bool(cfg.get("deep_check", True))
        try:
            self._timeout = max(2.0, min(30.0, float(cfg.get("timeout") or 8)))
        except (TypeError, ValueError):
            self._timeout = 8.0

        routers, errors = roslink.parse_routers(cfg.get("routers_text"))
        # 全局「校验证书」下发到每一行，供 cfrouteros 的 router_verify 使用
        for row in routers:
            row["verify"] = self._verify
        self._routers = routers
        self._errors = errors
        self._sync_shared()
        if errors:
            logger.warning(f"{_LOG_PREFIX}：配置有 {len(errors)} 处问题：{'；'.join(errors[:3])}")
        logger.info(f"{_LOG_PREFIX}：已载入 {len(routers)} 台路由器，广播"
                    f"{'成功' if self._broadcast_ok else '未启用/失败'}（{self._shared_path or '-'}）")

    def _sync_shared(self) -> Tuple[bool, str]:
        """把当前连接清单广播到共享文件；关闭广播时写入空清单以清除旧数据。"""
        if not self._broadcast:
            ok, path = roslink.write_shared([])
            self._broadcast_ok = False
            self._broadcast_error = "广播已关闭（其他插件将使用各自手填的连接参数）"
            self._shared_path = path if ok else ""
            return ok, path
        ok, result = roslink.write_shared(self._routers)
        self._broadcast_ok = ok
        if ok:
            self._shared_path = result
            self._broadcast_error = ""
        else:
            self._shared_path = ""
            self._broadcast_error = result
            logger.error(f"{_LOG_PREFIX}：广播失败：{result}")
        return ok, result

    def get_state(self) -> bool:
        return True  # 纯配置/工具型插件，常驻可用，不注册定时任务

    def stop_service(self) -> None:
        return None

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------

    @staticmethod
    def _col(md: int, content: List[dict]) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": content}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        count = len(self._routers)
        status = (f"已保存 {count} 台，广播文件：{_esc(self._shared_path or '未生成')}"
                  if self._broadcast_ok else
                  f"广播未生效：{_esc(self._broadcast_error or '尚未保存配置')}")
        content: List[dict] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                              "class": "mb-4"},
             "text": "路由器的地址、账号、密码在这里填一次，其他 RouterOS 插件就能直接选。"
                                 "连不上时点详情页的「一键体检」，会告诉你要开哪个服务、该执行哪条命令。"},
            {"component": "VAlert", "props": {"type": "success" if self._broadcast_ok else "warning",
                                              "variant": "tonal", "class": "mb-4", "title": "广播状态"},
             "text": status},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"},
             "text": "路由器清单"},
            {"component": "VRow", "content": [
                self._col(12, [{"component": "VTextarea", "props": {
                    "model": "routers_text", "label": "已保存的 RouterOS 路由器", "rows": 8,
                    "placeholder": roslink.ROUTERS_EXAMPLE,
                    "hint": "每行一台：名称｜地址｜端口｜用户名｜密码｜HTTPS(1/0)｜备注（备注可省略）；"
                            "# 开头为注释。也支持直接粘贴 JSON 数组。名称同名时后一行覆盖前一行。",
                    "persistentHint": True}}]),
            ]},
            {"component": "VAlert", "props": {"type": "warning", "variant": "text", "class": "mb-2"},
             "text": "密码是明文存的，别用 admin 主账号，单独建个最小权限的 API 账号。"},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mt-2 mb-1"},
             "text": "体检与广播"},
            {"component": "VRow", "content": [
                self._col(3, [{"component": "VTextField", "props": {
                    "model": "timeout", "label": "体检超时（秒）", "type": "number",
                    "placeholder": "8", "hint": "单台每次请求的超时"}}]),
                self._col(3, [{"component": "VSwitch", "props": {
                    "model": "verify", "label": "校验证书",
                    "hint": "自签证书请关闭（关闭后不进行 TLS 校验）"}}]),
                self._col(3, [{"component": "VSwitch", "props": {
                    "model": "deep_check", "label": "检查业务分区读权限",
                    "hint": "额外探测 /rest/interface，判断写入类插件能否正常工作"}}]),
                self._col(3, [{"component": "VSwitch", "props": {
                    "model": "broadcast_enabled", "label": "广播给其他插件",
                    "hint": "关闭后其他插件自动回退到各自手填的连接参数"}}]),
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VAlert", "props": {"type": "secondary", "variant": "tonal",
                                              "class": "mb-2"},
             "text": "保存后去详情页点「一键体检」，再在其他插件里选中这台就行。"},
        ]
        defaults: Dict[str, Any] = {
            "routers_text": "",
            "timeout": 8,
            "verify": False,
            "deep_check": True,
            "broadcast_enabled": True,
        }
        return content, defaults

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def _action_button(self, label: str, api: str, color: str, icon: str,
                       method: str = "post", confirm: bool = False) -> dict:
        return {"component": "VBtn",
                "props": {"variant": "tonal", "size": "small", "class": "ma-1",
                          "color": color, "prepend-icon": icon},
                "text": label,
                "events": {"click": {"api": f"plugin/{PLUGIN_ID}/{api}",
                                     "method": method, "params": {}, "confirm": confirm}}}

    @staticmethod
    def _stat_card(title: str, value: str, icon: str, color: str) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
            {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": _esc(title)},
                    {"component": "div", "props": {"class": f"text-h6 font-weight-bold text-{color}"},
                     "text": _esc(value)},
                    {"component": "div", "props": {"class": "text-caption mt-1"},
                     "text": f"{icon}"},
                ]},
            ]},
        ]}

    def _probe_data(self) -> Dict[str, Any]:
        data = self.get_data(_KEY_PROBE) or {}
        return data if isinstance(data, dict) else {}

    def get_page(self) -> List[dict]:
        probe = self._probe_data()
        results: List[Dict[str, Any]] = list(probe.get("results") or [])
        failed = len([r for r in results if r.get("level") == LEVEL_FAIL])
        warned = len([r for r in results if r.get("level") == LEVEL_WARN])
        overall = "有失败项" if failed else ("有注意项" if warned else ("全部通过" if results else "未体检"))

        content: List[dict] = [
            {"component": "VCard", "props": {"variant": "tonal", "class": "mt-3"}, "content": [
                {"component": "VCardTitle", "text": "操作"},
                {"component": "VCardText", "content": [
                    self._action_button("一键体检全部", "check", "primary", "mdi-stethoscope"),
                    self._action_button("重新广播配置", "sync", "secondary", "mdi-broadcast"),
                    self._action_button("刷新状态", "status", "info", "mdi-refresh", method="get"),
                    self._action_button("清空体检结果", "clear", "warning", "mdi-delete-outline",
                                        confirm=True),
                ]},
            ]}
        ]

        if self._errors:
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mt-3", "title": "这几行填错了"},
                "text": "；".join(_esc(e) for e in self._errors[:8])})

        if not self._routers:
            content.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-3",
                "title": "还没有路由器"},
                "text": "到「设置」里按示例填一行：名称｜地址｜端口｜用户名｜密码｜1｜备注，"
                                    "保存后回来点「一键体检」。"})
            return content

        # 每台路由器一张卡片
        by_name = {str(r.get("name")): r for r in results}
        for row in self._routers:
            pub = roslink.public_router(row)
            result = by_name.get(pub["name"]) or {}
            level = str(result.get("level") or LEVEL_INFO)
            checks: List[Dict[str, Any]] = list(result.get("checks") or [])
            rows: List[dict] = []
            for chk in checks:
                lvl = str(chk.get("level") or LEVEL_INFO)
                rows.append({"component": "tr", "content": [
                    {"component": "td", "props": {"class": "text-caption"},
                     "text": _esc(chk.get("label"))},
                    {"component": "td", "props": {"class": f"text-caption font-weight-bold text-{_LEVEL_COLOR.get(lvl, 'info')}"},
                     "text": _esc(chk.get("level_text"))},
                    {"component": "td", "props": {"class": "text-caption"},
                     "text": _esc(chk.get("detail"))},
                ]})
            body: List[dict] = [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                 "text": f"{_LEVEL_ICON.get(level, 'mdi-information')}  {_esc(pub['name'])}"},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mb-2"},
                     "text": (f"{_esc(pub['address'])}:{pub['port']} · "
                              f"{'HTTPS' if pub['ssl'] else 'HTTP'} · 账号 {_esc(pub['username'])} · "
                              f"密码 {_esc(pub['password'])}"
                              + (f" · 备注 {_esc(pub['note'])}" if pub["note"] else ""))},
                ]},
            ]
            if checks:
                body.append({"component": "VTable", "props": {"density": "compact", "hover": True},
                             "content": [
                                 {"component": "thead", "content": [
                                     {"component": "tr", "content": [
                                         {"component": "th", "text": "检查项"},
                                         {"component": "th", "text": "结果"},
                                         {"component": "th", "text": "说明"},
                                     ]}]},
                                 {"component": "tbody", "content": rows},
                             ]})
            else:
                body.append({"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": "尚未体检，点上方「一键体检全部」开始。"},
                ]})
            content.append({"component": "VCard", "props": {
                "variant": "outlined", "class": "mt-3"}, "content": body})


        return content

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        # path 为相对路径：宿主自动补齐 /api/v1/plugin/<PluginID> 前缀
        return [
            {"path": "/check", "auth": "bear", "endpoint": self.api_check,
             "methods": ["POST"], "summary": "一键体检全部路由器"},
            {"path": "/sync", "auth": "bear", "endpoint": self.api_sync,
             "methods": ["POST"], "summary": "重新广播连接配置"},
            {"path": "/status", "auth": "bear", "endpoint": self.api_status,
             "methods": ["GET"], "summary": "查询路由器清单、广播状态与最近体检结果"},
            {"path": "/clear", "auth": "bear", "endpoint": self.api_clear,
             "methods": ["POST"], "summary": "清空体检结果"},
        ]

    def _run_probe(self) -> Dict[str, Any]:
        if not self._routers:
            return {"results": [], "fixes": [], "checked_at": "无路由器可体检"}
        workers = min(4, max(1, len(self._routers)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(probe_one, row, None, self._timeout, self._deep)
                       for row in self._routers]
            results = [f.result() for f in futures]
        return {
            "results": results,
            "fixes": collect_fixes(results),
            "checked_at": results[0].get("checked_at", "") if results else "",
        }

    def api_check(self) -> Any:
        if not self._routers:
            return _Response(success=False, message="请先在设置里填写路由器清单")
        try:
            data = self._run_probe()
            self.save_data(_KEY_PROBE, data)
            failed = len([r for r in data["results"] if r.get("level") == LEVEL_FAIL])
            return _Response(success=True,
                             message=(f"体检完成：{len(data['results'])} 台，{failed} 台有失败项"
                                      if failed else f"体检完成：{len(data['results'])} 台全部通过"),
                             data=data)
        except Exception as err:  # noqa: BLE001
            logger.error(f"{_LOG_PREFIX}：体检异常：{err}")
            return _Response(success=False, message=f"体检异常：{err}")

    def api_sync(self) -> Any:
        ok, result = self._sync_shared()
        return _Response(success=ok,
                         message=(f"已广播 {len(self._routers)} 台路由器到 {result}" if ok
                                  else f"广播失败：{result}"),
                         data={"count": len(self._routers), "path": result})

    def api_clear(self) -> Any:
        self.save_data(_KEY_PROBE, {})
        return _Response(success=True, message="体检结果已清空")

    def api_status(self) -> Any:
        probe = self._probe_data()
        return _Response(success=True, data={
            "routers": [roslink.public_router(r) for r in self._routers],
            "count": len(self._routers),
            "errors": self._errors,
            "broadcast": self._broadcast_ok,
            "shared_path": self._shared_path,
            "broadcast_error": self._broadcast_error,
            "verify": self._verify,
            "timeout": self._timeout,
            "probe": probe,
        })
