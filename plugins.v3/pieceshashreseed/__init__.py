"""Pieces Hash 辅种插件（MoviePilot V3）。

以种子的 pieces hash 作为内容指纹，向支持 pieces-hash 接口的站点查询同内容种子，
再把站点种子添加到指定下载器复用已有文件做种；支持站点能力探测、cron 定时、
下载器下拉选择、跳过哈希校验、跨下载器路径映射与运行统计。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app.schemas.types import EventType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper

from . import core
from .site import (
    SiteConfig,
    download_torrent,
    load_sites,
    probe_all,
    probe_site,
    query_pieces,
    site_options,
    summarize_support,
)

LOG_TAG = "[PiecesHashReseed] "
_CACHE_KEY = "cache"
_SUPPORT_KEY = "site_support"
_LASTRUN_KEY = "last_run"
_CACHE_LIMIT = 10000
_MAX_TORRENT_FILES = 30000

# 做种中/已完成状态，用于兼容不同下载器的状态命名
_SEEDING_STATES = {
    "seeding", "seed pending", "seed_pending", "uploading", "stalledup",
    "queuedup", "forcedup", "checkingup", "pausedup", "stoppedup", "pausedUP", "stoppedUP",
}
# 明确不应参与辅种的状态
_BAD_STATES = {
    "error", "missingfiles", "downloading", "metadl", "stalleddl", "allocating",
    "checkingdl", "moving", "queuedforchecking", "checkingresumedata",
}
# 常见容器内种子缓存目录，用户可通过配置覆盖
_DEFAULT_TORRENT_DIRS = {
    "qbittorrent": ["/config/qBittorrent/BT_backup", "/qb/BT_backup", "/qb"],
    "transmission": ["/config/transmission/torrents", "/tr/torrents", "/tr"],
}


class ActionResult(BaseModel):
    """手动触发类 API 的统一返回结构。"""

    success: bool
    message: str = ""


class PluginStatus(BaseModel):
    """插件运行状态快照，供详情页与外部调用读取。"""

    enabled: bool = False
    running: bool = False
    schedule: str = ""
    downloaders: list[str] = Field(default_factory=list)
    supported_sites: list[str] = Field(default_factory=list)
    unsupported_sites: list[str] = Field(default_factory=list)
    last_run: dict[str, Any] = Field(default_factory=dict)


def _field(component: str, model: str, label: str, md: int = 6, **props: Any) -> dict:
    """生成一个带 model 的表单列，减少配置页 JSON 重复。"""
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": md},
        "content": [{"component": component, "props": {"model": model, "label": label, **props}}],
    }


def _row(*columns: dict) -> dict:
    """生成一行表单布局。"""
    return {"component": "VRow", "content": list(columns)}


def _alert(text: str, kind: str = "info") -> dict:
    """生成一行说明性提示。"""
    return _row({
        "component": "VCol",
        "props": {"cols": 12},
        "content": [{"component": "VAlert", "props": {"type": kind, "variant": "tonal"}, "text": text}],
    })


def _get(item: Any, key: str, default: Any = None) -> Any:
    """同时兼容 qBittorrent 字典与 Transmission 对象的字段读取。"""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


class PiecesHashReseed(_PluginBase):
    """基于 pieces hash 的下载器辅种插件。"""

    plugin_name = "Hash 辅种"
    plugin_desc = "按 pieces hash 匹配站点同内容种子并自动辅种，支持站点能力检测、cron 定时与下载器选择。"
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/pieceshashreseed.png"
    plugin_version = "1.0.4"
    plugin_author = "BackspaceM"
    # 前端插件卡片会用该地址拼接 https://github.com/<owner>.png 作为作者头像
    author_url = "https://github.com/BackspaceM"
    plugin_label = "做种,辅种,站点"
    plugin_config_prefix = "pieceshashreseed_"
    plugin_order = 20
    auth_level = 1

    # 运行配置（init_plugin 中按配置重置）
    _enabled = False
    _onlyonce = False
    _checkonce = False
    _clearcache = False
    _downloaders: list[str] = []
    _target_downloader = ""
    _sites: list[str] = []
    _credentials_text = ""
    _torrent_dirs_text = ""
    _schedule_mode = "计划任务"
    _cron = core.DEFAULT_CRON
    _interval_minutes = 360
    _auto_detect = True
    _notify = False
    _skip_checking = True
    _auto_start = True
    _seed_tag = "辅种"
    _category = ""
    _exclude_tags: list[str] = []
    _exclude_paths: list[str] = []
    _path_mapping_text = ""
    _batch_size = 200
    _timeout = 20
    _max_per_run = 200
    _dry_run = False

    def __init__(self) -> None:
        """初始化实例级运行状态，避免虚拟分身之间共享可变状态。"""
        super().__init__()
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._running = False
        self._stopped = False
        self._credentials: dict[str, core.SiteCredential] = {}
        self._torrent_dirs: dict[str, list[str]] = {}
        self._path_mapping: list[tuple[str, str]] = []
        self._success_cache: list[str] = []
        self._error_cache: list[str] = []
        self._permanent_error_cache: list[str] = []
        self._site_support: dict[str, Any] = {}
        self._last_run: dict[str, Any] = {}

    # ------------------------------------------------------------------ 生命周期

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置、恢复缓存并安排一次性任务；允许宿主重复调用。"""
        self.stop_service()
        self._stopped = False
        config = dict(config or {})

        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._checkonce = bool(config.get("checkonce"))
        self._clearcache = bool(config.get("clearcache"))
        self._downloaders = [str(x) for x in (config.get("downloaders") or []) if x]
        self._target_downloader = str(config.get("target_downloader") or "")
        self._sites = [str(x) for x in (config.get("sites") or []) if x]
        self._credentials_text = str(config.get("credentials") or "")
        self._torrent_dirs_text = str(config.get("torrent_dirs") or "")
        self._schedule_mode = str(config.get("schedule_mode") or "计划任务")
        self._cron = str(config.get("cron") or core.DEFAULT_CRON)
        self._interval_minutes = self._bounded_int(config.get("interval_minutes"), 360, 5, 10080)
        self._auto_detect = bool(config.get("auto_detect", True))
        self._notify = bool(config.get("notify"))
        self._skip_checking = bool(config.get("skip_checking", True))
        self._auto_start = bool(config.get("auto_start", True))
        self._seed_tag = str(config.get("seed_tag") or "辅种")
        self._category = str(config.get("category") or "")
        self._exclude_tags = core.parse_list(config.get("exclude_tags"))
        self._exclude_paths = core.parse_list(config.get("exclude_paths"))
        self._path_mapping_text = str(config.get("path_mapping") or "")
        self._batch_size = self._bounded_int(config.get("batch_size"), 200, 10, 1000)
        self._timeout = self._bounded_int(config.get("timeout"), 20, 5, 120)
        self._max_per_run = self._bounded_int(config.get("max_per_run"), 200, 1, 5000)
        self._dry_run = bool(config.get("dry_run"))

        self._credentials = core.parse_credentials(self._credentials_text)
        self._torrent_dirs = self._parse_torrent_dirs(self._torrent_dirs_text)
        self._path_mapping = core.parse_path_mapping(self._path_mapping_text)
        valid_cron, cron_text = core.validate_cron(self._cron)
        if not valid_cron:
            logger.warning(f"{LOG_TAG}执行周期无效（{cron_text}），回退为 {core.DEFAULT_CRON}")
            self._cron = core.DEFAULT_CRON
            config["cron"] = core.DEFAULT_CRON
        else:
            self._cron = cron_text

        self._site_support = dict(self.get_data(_SUPPORT_KEY) or {})
        self._last_run = dict(self.get_data(_LASTRUN_KEY) or {})
        self._load_cache()

        changed = False
        if self._clearcache:
            self._clear_cache()
            self._clearcache = False
            config["clearcache"] = False
            changed = True
        if self._checkonce:
            self._checkonce = False
            config["checkonce"] = False
            self._schedule_once(self.check_sites, "check", "站点支持检测", 3)
            changed = True
        if self._onlyonce:
            self._onlyonce = False
            config["onlyonce"] = False
            self._schedule_once(self.run_reseed, "run", "立即辅种一次", 3)
            changed = True
        if changed:
            self.update_config(config)

        if self._enabled and not self._downloaders:
            logger.warning(f"{LOG_TAG}尚未选择下载器，定时辅种不会执行")
        if self._enabled and not self._credentials:
            logger.warning(f"{LOG_TAG}尚未配置站点 Passkey，定时辅种不会执行")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool(self._enabled)

    def stop_service(self) -> None:
        """通知正在执行的辅种任务中止，并取消未执行的一次性任务。"""
        self._stopped = True
        self._event.set()
        remover = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remover):
            for job_id in ("run", "check"):
                try:
                    remover(self.__class__.__name__, job_id)
                except Exception:  # noqa: BLE001 - 宿主版本差异不应阻断停止
                    pass

    # ------------------------------------------------------------------ 服务与命令

    def get_service(self) -> list[dict]:
        """按 cron 或固定间隔注册辅种服务。"""
        if not self._enabled or self._schedule_mode == "禁用":
            return []
        base = {
            "id": f"{self.__class__.__name__}Reseed",
            "name": "Hash 辅种",
            "func": self.run_reseed,
            "kwargs": {},
        }
        if self._schedule_mode == "固定间隔":
            return [{**base, "trigger": "interval", "kwargs": {"minutes": self._interval_minutes}}]
        try:
            trigger = CronTrigger.from_crontab(core.normalize_cron(self._cron))
        except (ValueError, TypeError) as err:
            logger.error(f"{LOG_TAG}cron 表达式无效，服务未注册：{err}")
            return []
        return [{**base, "trigger": trigger}]

    @staticmethod
    def get_command() -> list[dict]:
        """注册 /辅种 远程命令。"""
        return [
            {
                "cmd": "/辅种",
                "event": EventType.PluginAction,
                "desc": "立即执行一次 Pieces Hash 辅种",
                "category": "插件命令",
                "data": {"action": "pieceshashreseed_run"},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def remote_action(self, event: Event) -> None:
        """响应远程命令，只处理属于本插件的动作。"""
        data = getattr(event, "event_data", None) or {}
        if data.get("action") != "pieceshashreseed_run":
            return
        self._schedule_once(self.run_reseed, "run", "远程触发辅种", 3)
        self.post_message(
            channel=data.get("channel"),
            title="Hash 辅种",
            text="已收到命令，辅种任务将在 3 秒后执行。",
            userid=data.get("user"),
        )

    # ------------------------------------------------------------------ 配置与详情页

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回 Vuetify JSON 配置页与默认配置模型。"""
        try:
            downloader_items = [
                {"title": name, "value": name}
                for name in (DownloaderHelper().get_configs() or {}).keys()
            ]
        except Exception as err:  # noqa: BLE001
            logger.error(f"{LOG_TAG}读取下载器列表失败：{err}")
            downloader_items = []
        downloader_choice = [{"title": "同下载器", "value": ""}] + downloader_items

        return [
            {
                "component": "VForm",
                "content": [
                    _alert(
                        "从下载器已有的种子算出 pieces hash，去站点找内容一样的种子，下下来按原路径加进下载器做种。"
                        "先配好站点 Passkey 和种子缓存目录。",
                    ),
                    _row(
                        _field("VSwitch", "enabled", "启用插件", 3),
                        _field("VSwitch", "notify", "发送通知", 3),
                        _field("VSwitch", "auto_detect", "自动检测站点支持", 3),
                        _field("VSwitch", "dry_run", "试运行（不添加种子）", 3),
                    ),
                    _row(
                        _field("VSelect", "downloaders", "扫描的下载器", 6,
                               multiple=True, chips=True, clearable=True,
                               items=downloader_items,
                               hint="下拉选择需要扫描并辅种的下载器，不选则使用全部已启用下载器",
                               persistentHint=True),
                        _field("VSelect", "target_downloader", "辅种目标下载器", 6,
                               items=downloader_choice,
                               hint="默认添加到原下载器；跨下载器需配置路径映射",
                               persistentHint=True),
                    ),
                    _row(
                        _field("VSelect", "sites", "辅种站点", 12,
                               multiple=True, chips=True, clearable=True,
                               items=site_options(),
                               hint="仅列出的站点会参与辅种，站点需已在 MoviePilot 站点管理中启用",
                               persistentHint=True),
                    ),
                    _row(
                        _field("VTextarea", "credentials", "站点 Passkey", 12,
                               rows=5,
                               placeholder="每行一个：站点名:Passkey[:接口路径[:请求间隔]]\n"
                                           "例如：青蛙:xxxxxxxxxx\n憨憨:yyyyyyyyyy:nexusapi/pieces-hash:2",
                               hint="站点名需与上方选项完全一致；请求间隔单位为秒，用于站点流控",
                               persistentHint=True),
                    ),
                    _row(
                        _field("VTextarea", "torrent_dirs", "种子缓存目录", 12,
                               rows=3,
                               placeholder="每行一个：下载器名称:/qb/BT_backup,/tr/torrents；"
                                           "不带下载器名称时对所有下载器生效",
                               hint="qBittorrent 的 BT_backup / Transmission 的 torrents 目录，留空时尝试常见容器路径",
                               persistentHint=True),
                    ),
                    _alert("定时设置：计划任务使用 5 段 crontab（分 时 日 月 周），例如 0 4 * * * 表示每天 04:00。", "info"),
                    _row(
                        _field("VSelect", "schedule_mode", "定时方式", 4,
                               items=[{"title": "计划任务（cron）", "value": "计划任务"},
                                      {"title": "固定间隔", "value": "固定间隔"},
                                      {"title": "禁用", "value": "禁用"}]),
                        _field("VTextField", "cron", "执行周期（cron）", 4,
                               placeholder=core.DEFAULT_CRON),
                        _field("VTextField", "interval_minutes", "固定间隔（分钟）", 4,
                               placeholder="360"),
                    ),
                    _alert("辅种行为：qBittorrent 支持跳过哈希校验；Transmission 会自动校验。添加默认以暂停状态入库再启动。", "info"),
                    _row(
                        _field("VSwitch", "skip_checking", "qBittorrent 跳过哈希校验", 4),
                        _field("VSwitch", "auto_start", "添加后自动开始做种", 4),
                        _field("VTextField", "seed_tag", "辅种标签", 4,
                               placeholder="辅种"),
                    ),
                    _row(
                        _field("VTextField", "category", "qBittorrent 分类（可选）", 4,
                               placeholder="留空不设置"),
                        _field("VTextField", "exclude_tags", "排除标签", 4,
                               placeholder="逗号分隔，如 刷流,临时"),
                        _field("VTextField", "exclude_paths", "排除路径关键字", 4,
                               placeholder="逗号分隔，如 /tmp,/cache"),
                    ),
                    _row(
                        _field("VTextarea", "path_mapping", "跨下载器路径映射", 12,
                               rows=2,
                               placeholder="每行一个：/downloads::/volume1/downloads",
                               hint="仅当目标下载器与原下载器不同且两者挂载路径不一致时需要",
                               persistentHint=True),
                    ),
                    _row(
                        _field("VTextField", "batch_size", "单次查询条数", 4, placeholder="200"),
                        _field("VTextField", "timeout", "站点请求超时（秒）", 4, placeholder="20"),
                        _field("VTextField", "max_per_run", "单次最多辅种数", 4, placeholder="200"),
                    ),
                    _alert("手动操作：打开下列开关并保存配置即可触发一次；检测结果与运行统计在插件详情页查看。", "info"),
                    _row(
                        _field("VSwitch", "checkonce", "立即检测站点支持", 4),
                        _field("VSwitch", "onlyonce", "立即辅种一次", 4),
                        _field("VSwitch", "clearcache", "清除缓存与检测记录", 4),
                    ),
                    _alert(
                        "分块大小和文件集合得一样，站点也要有 pieces-hash 接口；跨下载器时目标下载器得能看到同一份文件。"
                        "同一站点别跟 IYUU 那类辅种插件一起开。",
                        "warning",
                    ),
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "auto_detect": True,
            "dry_run": False,
            "downloaders": [],
            "target_downloader": "",
            "sites": [],
            "credentials": "",
            "torrent_dirs": "",
            "schedule_mode": "计划任务",
            "cron": core.DEFAULT_CRON,
            "interval_minutes": 360,
            "skip_checking": True,
            "auto_start": True,
            "seed_tag": "辅种",
            "category": "",
            "exclude_tags": "",
            "exclude_paths": "",
            "path_mapping": "",
            "batch_size": 200,
            "timeout": 20,
            "max_per_run": 200,
            "checkonce": False,
            "onlyonce": False,
            "clearcache": False,
        }

    def get_page(self) -> list[dict]:
        """主界面只留一行最近结果，明细看插件日志。"""
        last = self._last_run or dict(self.get_data(_LASTRUN_KEY) or {})
        if last:
            summary = (f"{last.get('finished_at', '-')}｜辅种成功 {last.get('success', 0)} 个"
                       f"｜已存在 {last.get('exist', 0)} 个｜失败 {last.get('failed', 0)} 个")
        else:
            summary = "还没跑过。"
        return [_alert(summary, "success" if last.get("success") else "info")]

    def get_api(self) -> list[dict]:
        """注册状态查询与手动触发接口。"""
        return [
            {"path": "/status", "endpoint": self.get_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询辅种插件状态", "response_model": PluginStatus},
            {"path": "/check-sites", "endpoint": self.trigger_check, "methods": ["POST"],
             "auth": "bear", "summary": "检测站点 pieces-hash 支持情况", "response_model": ActionResult},
            {"path": "/run", "endpoint": self.trigger_run, "methods": ["POST"],
             "auth": "bear", "summary": "立即执行一次辅种", "response_model": ActionResult},
            {"path": "/clear-cache", "endpoint": self.trigger_clear_cache, "methods": ["POST"],
             "auth": "bear", "summary": "清除辅种缓存", "response_model": ActionResult},
        ]

    def get_status(self) -> PluginStatus:
        """返回当前运行状态、支持站点列表与最近运行统计。"""
        supported, unsupported = summarize_support(self._site_support)
        unsupported_names = [text.split("：", 1)[0] for text in unsupported]
        return PluginStatus(
            enabled=self.get_state(),
            running=self._running,
            schedule=f"{self._schedule_mode} {self._cron}" if self._schedule_mode == "计划任务"
            else f"{self._schedule_mode} {self._interval_minutes}分钟",
            downloaders=list(self._downloaders),
            supported_sites=supported,
            unsupported_sites=unsupported_names,
            last_run=dict(self._last_run),
        )

    def trigger_check(self) -> ActionResult:
        """异步触发站点支持检测。"""
        if self._running:
            return ActionResult(success=False, message="辅种任务正在执行，请稍后再试")
        self._schedule_once(self.check_sites, "check", "API 触发站点检测", 2)
        return ActionResult(success=True, message="站点检测已加入调度")

    def trigger_run(self) -> ActionResult:
        """异步触发一次辅种。"""
        if self._running:
            return ActionResult(success=False, message="辅种任务正在执行，请稍后再试")
        self._schedule_once(self.run_reseed, "run", "API 触发辅种", 2)
        return ActionResult(success=True, message="辅种任务已加入调度")

    def trigger_clear_cache(self) -> ActionResult:
        """清除成功/失败缓存与站点检测记录。"""
        self._clear_cache()
        return ActionResult(success=True, message="缓存与检测记录已清除")

    # ------------------------------------------------------------------ 站点检测

    def check_sites(self) -> dict[str, Any]:
        """强制重新检测所有已配置站点的 pieces-hash 接口。"""
        sites = load_sites(self._credentials, self._sites, self._site_support)
        if not sites:
            logger.warning(f"{LOG_TAG}没有可检测的站点，请检查站点选择、启用状态与 Passkey 配置")
            result: dict[str, Any] = {}
        else:
            result = probe_all(sites, self._timeout)
        self._site_support = result
        self.save_data(_SUPPORT_KEY, result)
        supported, unsupported = summarize_support(result)
        text = f"站点支持检测完成：支持 {len(supported)} 个（{', '.join(supported) or '无'}）"
        if unsupported:
            text += "；不支持：" + "；".join(unsupported[:5])
        logger.info(f"{LOG_TAG}{text}")
        if self._notify:
            self.post_message(title="Pieces Hash 辅种 - 站点检测", text=text)
        return result

    def _ensure_support(self, sites: list[SiteConfig]) -> list[SiteConfig]:
        """按缓存与自动检测开关筛出可用站点，并回填接口路径。"""
        if not sites:
            return []
        changed = False
        for site in sites:
            cached = self._site_support.get(site.name) if isinstance(self._site_support.get(site.name), dict) else {}
            if self._auto_detect and not cached.get("supported"):
                result = probe_site(site, self._timeout)
                self._site_support[site.name] = result.to_dict()
                changed = True
                cached = result.to_dict()
            site.api_path = site.api_path or cached.get("api_path") or core.DEFAULT_API_PATHS[0]
        if changed:
            self.save_data(_SUPPORT_KEY, self._site_support)
        supported_names = {name for name, item in self._site_support.items()
                           if isinstance(item, dict) and item.get("supported")}
        usable = [site for site in sites if not self._auto_detect or site.name in supported_names]
        skipped = [site.name for site in sites if site not in usable]
        if skipped:
            logger.warning(f"{LOG_TAG}以下站点未通过支持检测，本次跳过：{', '.join(skipped)}")
        return usable

    # ------------------------------------------------------------------ 辅种主流程

    def run_reseed(self) -> None:
        """执行一次完整辅种：扫描下载器 → 建立指纹 → 查询站点 → 添加种子。"""
        if self._stopped:
            logger.info(f"{LOG_TAG}插件已停止，忽略本次辅种触发")
            return
        if not self._lock.acquire(blocking=False):
            logger.warning(f"{LOG_TAG}已有辅种任务在执行，跳过本次触发")
            return
        self._event.clear()
        self._running = True
        started = time.time()
        stats: dict[str, Any] = {
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "downloaders": 0, "torrents": 0, "indexed_torrents": 0,
            "missing_torrent": 0, "pieces": 0, "sites": 0, "matched": 0,
            "success": 0, "exist": 0, "failed": 0, "skipped": 0,
            "would_seed": 0, "dry_run": self._dry_run, "errors": [],
        }
        try:
            if self._event.is_set():
                return
            services, target_service = self._resolve_services()
            if not services:
                self._add_error(stats, "没有可用的下载器，请检查下载器选择与连接状态")
                return
            sites = self._ensure_support(load_sites(self._credentials, self._sites, self._site_support))
            stats["sites"] = len(sites)
            if not sites:
                self._add_error(stats, "没有可用的辅种站点，请检查站点选择与 Passkey 配置")
                return

            for name, service in services.items():
                if self._event.is_set():
                    break
                stats["downloaders"] += 1
                destination = target_service or service
                self._process_downloader(name, service, destination, sites, stats)
                if stats["success"] + stats["would_seed"] >= self._max_per_run:
                    logger.info(f"{LOG_TAG}已达到单次最多辅种数 {self._max_per_run}，提前结束")
                    break
        except Exception as err:  # noqa: BLE001 - 顶层兜底，保证状态与缓存始终落盘
            logger.error(f"{LOG_TAG}辅种任务异常：{err}")
            self._add_error(stats, f"任务异常：{type(err).__name__}")
        finally:
            stats["duration"] = round(time.time() - started, 1)
            stats["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._last_run = stats
            self.save_data(_LASTRUN_KEY, stats)
            self._save_cache()
            self._running = False
            self._lock.release()
            summary = (
                f"辅种完成：命中 {stats['matched']}、成功 {stats['success']}、已存在 {stats['exist']}、"
                f"失败 {stats['failed']}、跳过 {stats['skipped']}、缺少种子文件 {stats['missing_torrent']}，"
                f"耗时 {stats['duration']} 秒"
            )
            logger.info(f"{LOG_TAG}{summary}")
            if self._notify and (stats["success"] or stats["failed"]):
                self.post_message(title="Hash 辅种", text=summary)

    def _process_downloader(
        self,
        name: str,
        service: Any,
        destination: Any,
        sites: list[SiteConfig],
        stats: dict[str, Any],
    ) -> None:
        """处理单个下载器：构建本地指纹索引并向各站点查询可辅种种子。"""
        seedable = self._list_seedable(service)
        stats["torrents"] += len(seedable)
        if not seedable:
            return
        local_index, scanned = self._load_local_torrents(name, service)
        stats["indexed_torrents"] += scanned
        existing_hashes = {str(t["hash"]).lower() for t in seedable}
        if destination is not service:
            # 跨下载器辅种时，目标下载器已有任务也要纳入去重
            existing_hashes |= self._all_torrent_hashes(destination)

        pieces_paths: dict[str, str] = {}
        for torrent in seedable:
            if self._event.is_set():
                return
            info_hash = str(_get(torrent, "hash", "")).lower()
            fingerprint = local_index.get(info_hash)
            if not fingerprint:
                stats["missing_torrent"] += 1
                continue
            save_path = core.apply_path_mapping(str(_get(torrent, "path", "") or ""), self._path_mapping)
            if not save_path:
                stats["skipped"] += 1
                continue
            pieces_paths.setdefault(fingerprint.pieces_hash, save_path)

        stats["pieces"] += len(pieces_paths)
        if not pieces_paths:
            logger.warning(
                f"{LOG_TAG}下载器 {name} 未找到任何本地种子文件，请配置『种子缓存目录』"
                f"（当前索引 {scanned} 个种子文件）"
            )
            return

        for site in sites:
            if self._event.is_set():
                return
            matched = query_pieces(site, list(pieces_paths), self._batch_size, self._timeout)
            stats["matched"] += len(matched)
            for pieces_hash, torrent_id in matched.items():
                if self._event.is_set():
                    return
                if stats["success"] + stats["would_seed"] >= self._max_per_run:
                    return
                save_path = pieces_paths.get(pieces_hash.lower()) or pieces_paths.get(pieces_hash)
                if not save_path:
                    stats["skipped"] += 1
                    continue
                self._seed_one(site, torrent_id, pieces_hash, save_path, destination, existing_hashes, stats)

    def _seed_one(
        self,
        site: SiteConfig,
        torrent_id: str,
        pieces_hash: str,
        save_path: str,
        service: Any,
        existing_hashes: set[str],
        stats: dict[str, Any],
    ) -> None:
        """下载并添加一个站点种子，完成指纹复核、去重与状态回写。"""
        cache_key = f"{site.name}:{torrent_id}"
        if cache_key in self._success_cache or cache_key in self._permanent_error_cache:
            stats["skipped"] += 1
            return
        if cache_key in self._error_cache:
            stats["skipped"] += 1
            return

        content = download_torrent(site, torrent_id, self._timeout)
        if not content:
            stats["failed"] += 1
            self._remember(self._error_cache, cache_key)
            return
        try:
            remote = core.parse_torrent(content)
        except core.BencodeError as err:
            stats["failed"] += 1
            self._add_error(stats, f"{site.name} 种子 {torrent_id} 解析失败：{err}")
            self._remember(self._error_cache, cache_key)
            return
        # 指纹复核：站点返回的种子必须与本地内容完全一致，避免错配导致校验失败
        if remote.pieces_hash.lower() != pieces_hash.lower():
            stats["failed"] += 1
            self._add_error(stats, f"{site.name} 种子 {torrent_id} 的 pieces hash 与本地不一致，已跳过")
            self._remember(self._permanent_error_cache, cache_key)
            return
        if remote.info_hash.lower() in existing_hashes:
            stats["exist"] += 1
            self._remember(self._success_cache, cache_key)
            return
        if self._dry_run:
            stats["would_seed"] += 1
            return

        torrent_hash = self._add_torrent(service, content, save_path, remote)
        if not torrent_hash:
            stats["failed"] += 1
            self._remember(self._error_cache, cache_key)
            return
        existing_hashes.add(str(torrent_hash).lower())
        stats["success"] += 1
        self._remember(self._success_cache, cache_key)
        logger.info(f"{LOG_TAG}辅种成功：{site.name} 种子 {torrent_id} → {save_path}（{torrent_hash}）")

    def _add_torrent(self, service: Any, content: bytes, save_path: str, fingerprint: Any) -> str | None:
        """把种子以暂停状态添加到下载器，并按类型处理跳验/校验/启动。"""
        instance = getattr(service, "instance", None)
        if instance is None:
            return None
        is_qb = self._is_qb(service)
        tags = [self._seed_tag] if self._seed_tag else []
        try:
            if is_qb:
                kwargs: dict[str, Any] = {
                    "content": content,
                    "download_dir": save_path,
                    "is_paused": True,
                    "tag": tags or None,
                }
                if self._category:
                    kwargs["category"] = self._category
                if self._skip_checking:
                    kwargs["is_skip_checking"] = True
                result = instance.add_torrent(**kwargs)
                if isinstance(result, tuple):
                    ok, hashes = bool(result[0]), [str(x) for x in (result[1] or [])]
                else:
                    ok, hashes = bool(result), []
                if not ok and not hashes:
                    return None
                torrent_hash = hashes[0] if hashes else self._find_new_hash(
                    instance, fingerprint.name, save_path
                )
            else:
                torrent = instance.add_torrent(
                    content=content,
                    download_dir=save_path,
                    is_paused=True,
                    labels=tags or None,
                )
                torrent_hash = str(_get(torrent, "hashString", "") or "")
            if not torrent_hash:
                return None
            # Transmission 不支持跳过校验；qB 关闭跳验时同样显式触发校验
            if not is_qb or not self._skip_checking:
                try:
                    instance.recheck_torrents(ids=[torrent_hash])
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG}触发重新校验失败：{err}")
            if self._auto_start:
                instance.start_torrents(ids=[torrent_hash])
            return torrent_hash
        except Exception as err:  # noqa: BLE001 - 单个种子失败不影响批次
            logger.error(f"{LOG_TAG}添加辅种种子失败：{err}")
            return None

    def _find_new_hash(self, instance: Any, name: str, save_path: str) -> str | None:
        """旧版下载器不返回新增 hash 时，按标签/名称/路径反查任务。"""
        try:
            result = instance.get_torrents(tags=[self._seed_tag]) if self._seed_tag else instance.get_torrents()
            torrents = result[0] if isinstance(result, tuple) else (result or [])
            for item in torrents:
                if (_get(item, "name") == name
                        and str(_get(item, "save_path", "") or "").rstrip("/") == save_path.rstrip("/")):
                    return str(_get(item, "hash", "") or "") or None
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}反查新增种子失败：{err}")
        return None

    # ------------------------------------------------------------------ 辅助方法

    @staticmethod
    def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
        """把配置值约束为整数区间，非法输入回退默认值。"""
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_torrent_dirs(text: str | None) -> dict[str, list[str]]:
        """解析种子缓存目录配置，支持『下载器名:目录』与全局目录两种写法。"""
        result: dict[str, list[str]] = {}
        for line in (text or "").splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            parts = re.split(r"[:：](?!//)", line.strip(), maxsplit=1)
            if len(parts) == 2 and "/" in parts[1]:
                name, paths = parts[0].strip(), core.parse_list(parts[1])
            else:
                name, paths = "*", core.parse_list(line)
            if paths:
                result.setdefault(name, []).extend(paths)
        return {name: list(dict.fromkeys(paths)) for name, paths in result.items()}

    @staticmethod
    def _is_qb(service: Any) -> bool:
        """判断下载器类型，兼容 ServiceInfo.type 与实例类名两种来源。"""
        dtype = str(getattr(service, "type", "") or "").lower()
        if dtype:
            return "qb" in dtype
        return "qbittorrent" in type(getattr(service, "instance", None)).__name__.lower()

    def _resolve_services(self) -> tuple[dict[str, Any], Any | None]:
        """获取已连接的下载器实例，并解析可选的跨下载器目标。"""
        try:
            services = DownloaderHelper().get_services(name_filters=self._downloaders or None) or {}
        except Exception as err:  # noqa: BLE001
            logger.error(f"{LOG_TAG}读取下载器失败：{err}")
            return {}, None
        active: dict[str, Any] = {}
        for name, service in services.items():
            instance = getattr(service, "instance", None)
            if instance is None:
                continue
            try:
                if instance.is_inactive():
                    logger.warning(f"{LOG_TAG}下载器 {name} 未连接，跳过")
                    continue
            except Exception:  # noqa: BLE001
                pass
            active[name] = service
        target = None
        if self._target_downloader:
            target = active.get(self._target_downloader)
            if target is None:
                try:
                    extra = DownloaderHelper().get_services(name_filters=[self._target_downloader]) or {}
                    target = extra.get(self._target_downloader)
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG}获取目标下载器失败：{err}")
            if target is None:
                logger.warning(f"{LOG_TAG}目标下载器 {self._target_downloader} 不可用，改为同下载器辅种")
        return active, target

    def _list_seedable(self, service: Any) -> list[dict[str, Any]]:
        """列出下载器中可用于辅种的已完成任务，并应用标签/路径排除规则。"""
        instance = getattr(service, "instance", None)
        if instance is None:
            return []
        try:
            result = instance.get_torrents()
            torrents = result[0] if isinstance(result, tuple) else (result or [])
            if isinstance(result, tuple) and result[1]:
                logger.warning(f"{LOG_TAG}下载器种子列表读取失败")
                return []
        except Exception as err:  # noqa: BLE001
            logger.error(f"{LOG_TAG}读取下载器种子失败：{err}")
            return []
        is_qb = self._is_qb(service)
        seedable: list[dict[str, Any]] = []
        for item in torrents:
            try:
                if is_qb:
                    info_hash = str(_get(item, "hash", "") or "").lower()
                    state = str(_get(item, "state", "") or "").lower()
                    progress = float(_get(item, "progress", 0) or 0)
                    raw_tags = _get(item, "tags", "") or ""
                    path = str(_get(item, "save_path", "") or "")
                else:
                    info_hash = str(_get(item, "hashString", "") or "").lower()
                    state = str(_get(item, "status", "") or "").lower()
                    progress = float(_get(item, "percent_done", 0) or 0)
                    raw_tags = _get(item, "labels", []) or []
                    path = str(_get(item, "download_dir", "") or "")
                tags = [str(t).strip() for t in (raw_tags.split(",") if isinstance(raw_tags, str) else raw_tags)]
                name = str(_get(item, "name", "") or "")
            except (TypeError, ValueError, AttributeError):
                continue
            if not info_hash or not path:
                continue
            if state in _BAD_STATES or (progress < 0.999 and state not in _SEEDING_STATES):
                continue
            if any(tag in self._exclude_tags for tag in tags):
                continue
            if any(keyword and keyword in path for keyword in self._exclude_paths):
                continue
            seedable.append({"hash": info_hash, "name": name, "path": path, "tags": tags, "state": state})
        return seedable

    @staticmethod
    def _all_torrent_hashes(service: Any) -> set[str]:
        """读取下载器中所有任务的 info_hash，用于跨下载器去重。"""
        instance = getattr(service, "instance", None)
        if instance is None:
            return set()
        try:
            result = instance.get_torrents()
            torrents = result[0] if isinstance(result, tuple) else (result or [])
            return {
                str(_get(item, "hash", "") or _get(item, "hashString", "") or "").lower()
                for item in torrents
            } - {""}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}读取目标下载器任务失败：{err}")
            return set()

    def _load_local_torrents(self, downloader_name: str, service: Any) -> tuple[dict[str, Any], int]:
        """扫描种子缓存目录，建立 info_hash → 指纹 的索引。"""
        dtype = "qbittorrent" if self._is_qb(service) else "transmission"
        dirs = (self._torrent_dirs.get(downloader_name)
                or self._torrent_dirs.get("*")
                or _DEFAULT_TORRENT_DIRS.get(dtype, []))
        index: dict[str, Any] = {}
        scanned = 0
        for raw_dir in dirs:
            directory = Path(raw_dir)
            if not directory.is_dir():
                continue
            try:
                for torrent_file in directory.rglob("*.torrent"):
                    if scanned >= _MAX_TORRENT_FILES or self._event.is_set():
                        break
                    try:
                        fingerprint = core.parse_torrent(torrent_file.read_bytes())
                    except (OSError, core.BencodeError, ValueError):
                        continue
                    index[fingerprint.info_hash] = fingerprint
                    scanned += 1
            except OSError as err:
                logger.warning(f"{LOG_TAG}扫描种子目录 {directory} 失败：{err}")
        logger.info(f"{LOG_TAG}下载器 {downloader_name} 索引种子文件 {scanned} 个（目录 {len(dirs)} 个）")
        return index, scanned

    def _schedule_once(self, func: Callable[[], Any], job_id: str, name: str, delay: int) -> None:
        """优先交给宿主一次性调度器，旧版宿主回退到单次守护线程。"""
        adder = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if callable(adder):
            try:
                if adder(self.__class__.__name__, job_id, func, name, delay_seconds=delay):
                    return
            except Exception as err:  # noqa: BLE001
                logger.warning(f"{LOG_TAG}宿主一次性调度不可用，改用线程：{err}")
        threading.Thread(target=func, name=f"{self.__class__.__name__}-{job_id}-{uuid.uuid4().hex[:6]}",
                         daemon=True).start()

    def _load_cache(self) -> None:
        """从插件数据区恢复去重缓存。"""
        cache = self.get_data(_CACHE_KEY) or {}
        if not isinstance(cache, dict):
            cache = {}
        self._success_cache = [str(x) for x in (cache.get("success") or [])]
        self._error_cache = [str(x) for x in (cache.get("error") or [])]
        self._permanent_error_cache = [str(x) for x in (cache.get("permanent_error") or [])]

    def _save_cache(self) -> None:
        """持久化去重缓存，并限制最大条数防止无限增长。"""
        self.save_data(_CACHE_KEY, {
            "success": self._success_cache[-_CACHE_LIMIT:],
            "error": self._error_cache[-_CACHE_LIMIT:],
            "permanent_error": self._permanent_error_cache[-_CACHE_LIMIT:],
        })

    def _clear_cache(self) -> None:
        """清除成功/失败缓存、站点检测记录与最近运行统计。"""
        self._success_cache, self._error_cache, self._permanent_error_cache = [], [], []
        self._site_support, self._last_run = {}, {}
        self.save_data(_CACHE_KEY, {})
        self.save_data(_SUPPORT_KEY, {})
        self.save_data(_LASTRUN_KEY, {})
        logger.info(f"{LOG_TAG}已清除辅种缓存与检测记录")

    @staticmethod
    def _remember(cache: list[str], key: str) -> None:
        """记录一个已处理的种子，保持列表有序且不超过上限。"""
        if key not in cache:
            cache.append(key)
        if len(cache) > _CACHE_LIMIT:
            del cache[: len(cache) - _CACHE_LIMIT]

    @staticmethod
    def _add_error(stats: dict[str, Any], message: str) -> None:
        """把可读错误写入本次运行统计（不泄露 Cookie / passkey）。"""
        errors: list[str] = stats.setdefault("errors", [])
        if len(errors) < 20:
            errors.append(message)
