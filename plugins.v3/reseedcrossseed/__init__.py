"""Reseed 辅种（MoviePilot V3）。

与「名称辅种」「Hash 辅种」是三条独立路线：这里不读种子缓存，只扫描下载目录生成
「文件名 + 大小」索引，交给 Reseed 服务匹配各站同内容种子，下载种子后复核文件清单，
一致才按原路径加进下载器做种。

- 索引不变时复用上次命中结果，避开官方服务每小时 5 次 / 每天 10 次的限流
- 添加前二次复核文件清单，服务端 ±5% 的模糊命中不会误加
- 所有按钮端点都有兜底，页面只读缓存，不在渲染期联网或读下载器
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.schemas.types import EventType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from . import core, renderer, site

LOG_TAG = "[ReseedCrossSeed] "
_CACHE_KEY = "reseed_cache_v1"
_LAST_KEY = "reseed_last_v1"
_SERVICE_KEY = "reseed_service_v1"
_SUCCESS_KEY = "reseed_success_v1"
_CACHE_TTL = 24 * 3600
_MAX_SUCCESS = 3000
_CMD_ACTION = "reseedcrossseed_run"


def _api_fail(action: str, exc: Exception) -> schemas.Response:
    logger.warning(f"{LOG_TAG}{action}异常 {type(exc).__name__}: {exc}")
    return schemas.Response(
        success=False,
        message=f"{action}失败：{type(exc).__name__}，请查看插件日志",
    )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ReseedCrossSeed(_PluginBase):
    """按本地文件索引 + Reseed 服务端匹配做跨站辅种。"""

    plugin_name = "Reseed 辅种"
    plugin_desc = "扫描下载目录生成文件名与大小索引，交给 Reseed 服务匹配各站同内容种子，复核文件清单后自动加种。"
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/reseedcrossseed.png"
    plugin_version = "1.0.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_label = "做种,辅种,索引匹配"
    plugin_config_prefix = "reseedcrossseed_"
    plugin_order = 21
    auth_level = 1

    _enabled = False
    _dry_run = False
    _notify = False
    _include_warning = False
    _skip_checking = True
    _auto_start = True

    def __init__(self) -> None:
        """运行状态全部放在实例上，避免插件分身之间共享。"""
        super().__init__()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._running = False

        self._api = core.DEFAULT_API
        self._username = ""
        self._password = ""
        self._dirs: list[str] = []
        self._bindings: dict[str, core.SiteBinding] = {}
        self._sites: list[str] = []
        self._target_downloader = ""
        self._seed_tag = core.DEFAULT_SEED_TAG
        self._max_per_run = 50
        self._timeout = 30
        self._schedule_mode = "禁用"
        self._cron = core.DEFAULT_CRON
        self._interval_minutes = 720

        self._cache: dict[str, Any] = {}
        self._success: list[str] = []
        self._last: dict[str, Any] = {}
        self._service: dict[str, Any] = {}

    # ------------------------------------------------------------------ 生命周期

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并复位运行态。这里刻意不调 stop_service，避免重装后残留「任务被中止」。"""
        config = dict(config or {})
        if not self._running:
            self._stop_event.clear()

        self._enabled = bool(config.get("enabled"))
        self._dry_run = bool(config.get("dry_run"))
        self._notify = bool(config.get("notify"))
        self._include_warning = bool(config.get("include_warning"))
        self._skip_checking = bool(config.get("skip_checking", True))
        self._auto_start = bool(config.get("auto_start", True))
        self._api = core.normalize_api_url(config.get("api"))
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._dirs = core.parse_dirs(config.get("dirs"))
        self._bindings = core.parse_bindings(config.get("bindings"))
        self._sites = core.parse_list(config.get("sites"))
        self._target_downloader = str(config.get("target_downloader") or "").strip()
        self._seed_tag = str(config.get("seed_tag") or core.DEFAULT_SEED_TAG).strip()
        self._max_per_run = self._bounded_int(config.get("max_per_run"), 50, 1, 2000)
        self._timeout = self._bounded_int(config.get("timeout"), 30, 5, 120)
        self._schedule_mode = str(config.get("schedule_mode") or "禁用")
        self._interval_minutes = self._bounded_int(config.get("interval_minutes"), 720, 30, 10080)
        valid, cron_text = core.validate_cron(config.get("cron"))
        self._cron = cron_text if valid else core.DEFAULT_CRON
        if not valid:
            logger.warning(f"{LOG_TAG}执行周期无效（{cron_text}），回退为 {core.DEFAULT_CRON}")

        self._last = dict(self.get_data(_LAST_KEY) or {})
        self._service = dict(self.get_data(_SERVICE_KEY) or {})
        cache = self.get_data(_CACHE_KEY)
        self._cache = dict(cache) if isinstance(cache, dict) else {}
        success = self.get_data(_SUCCESS_KEY)
        self._success = [str(x) for x in success] if isinstance(success, list) else []

        if self._enabled and not (self._username and self._password):
            logger.warning(f"{LOG_TAG}尚未填 Reseed 账号密码，定时辅种不会执行")
        if self._enabled and not self._dirs:
            logger.warning(f"{LOG_TAG}尚未填扫描目录，定时辅种不会执行")
        if self._enabled and not self._bindings:
            logger.warning(f"{LOG_TAG}尚未填站点映射，定时辅种不会执行")

    def get_state(self) -> bool:
        return bool(self._enabled)

    def stop_service(self) -> None:
        self._stop_event.set()
        remover = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remover):
            try:
                remover(self.__class__.__name__, "run")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------ 服务与命令

    def get_service(self) -> list[dict]:
        if not self._enabled or self._schedule_mode == "禁用":
            return []
        base = {
            "id": f"{self.__class__.__name__}Reseed",
            "name": "Reseed 辅种",
            "func": self.run,
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
        return [
            {
                "cmd": "/reseed",
                "event": EventType.PluginAction,
                "desc": "立即执行一次 Reseed 辅种",
                "category": "插件命令",
                "data": {"action": _CMD_ACTION},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def remote_action(self, event: Event) -> None:
        data = getattr(event, "event_data", None) or {}
        if data.get("action") != _CMD_ACTION:
            return
        self._start_run(force_dry=False)
        try:
            self.post_message(
                channel=data.get("channel"),
                title="Reseed 辅种",
                text="已收到命令，辅种任务已开始。",
                userid=data.get("user"),
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 配置与详情页

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        try:
            return renderer.form()
        except Exception as err:  # noqa: BLE001 - 渲染异常不能让前端报「数据加载失败」
            logger.error(f"{LOG_TAG}配置页渲染失败：{err}")
            return renderer.form()

    def get_page(self) -> list[dict]:
        try:
            last = self._last or dict(self.get_data(_LAST_KEY) or {})
            service = self._service or dict(self.get_data(_SERVICE_KEY) or {})
            last_text = (
                f'{last.get("finished_at", "—")}　命中 {last.get("hits", 0)} 个，'
                f'成功 {last.get("added", 0)} 个，已存在 {last.get("exists", 0)} 个，'
                f'失败 {last.get("failed", 0)} 个'
                + ("（试运行）" if last.get("dry_run") else "")
                if last else "还没跑过"
            )
            schedule_text = (
                f"计划任务 {self._cron}" if self._schedule_mode == "计划任务"
                else f"每 {self._interval_minutes} 分钟" if self._schedule_mode == "固定间隔"
                else "定时已禁用，用下方按钮手动运行"
            )
            return renderer.render_page({
                "running": self._running,
                "last": last,
                "service": service,
                "last_text": last_text,
                "schedule_text": schedule_text,
                "dry_run": self._dry_run,
            })
        except Exception as err:  # noqa: BLE001
            logger.error(f"{LOG_TAG}详情页渲染失败：{err}")
            return [{"component": "div", "html": '<div style="padding:12px">Reseed 辅种</div>'}]

    def get_api(self) -> list[dict]:
        return [
            {"path": "/run", "endpoint": self.api_run, "methods": ["POST"],
             "auth": "bear", "summary": "立即执行一次辅种"},
            {"path": "/dryrun", "endpoint": self.api_dryrun, "methods": ["POST"],
             "auth": "bear", "summary": "试运行一次，只统计不添加种子"},
            {"path": "/probe", "endpoint": self.api_probe, "methods": ["POST"],
             "auth": "bear", "summary": "检测 Reseed 服务与账号"},
            {"path": "/clear_cache", "endpoint": self.api_clear_cache, "methods": ["POST"],
             "auth": "bear", "summary": "清除命中缓存与已辅种记录"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询运行状态"},
        ]

    # ------------------------------------------------------------------ 按钮端点

    def api_run(self) -> schemas.Response:
        try:
            return self._start_run(force_dry=False)
        except Exception as exc:  # noqa: BLE001
            return _api_fail("启动辅种", exc)

    def api_dryrun(self) -> schemas.Response:
        try:
            return self._start_run(force_dry=True)
        except Exception as exc:  # noqa: BLE001
            return _api_fail("启动试运行", exc)

    def api_probe(self) -> schemas.Response:
        try:
            if not (self._username and self._password):
                return schemas.Response(success=False, message="先填 Reseed 账号和密码再检测")
            client = site.ReseedClient(self._api, self._username, self._password,
                                       timeout=min(self._timeout, 15))
            auth = client.login()
            if not auth.ok:
                self._save_service(False, auth.message, 0)
                return schemas.Response(success=False, message=auth.message)
            result = client.sites_info()
            names = result.data if result.ok and isinstance(result.data, list) else []
            self._save_service(result.ok, result.message if result.ok else "登录成功，但站点列表读取失败",
                               len(names))
            if names:
                preview = "、".join(str(item.get("name") or "") for item in names[:8] if item.get("name"))
                return schemas.Response(
                    success=True,
                    message=f"服务可用，收录 {len(names)} 个站点：{preview}{'…' if len(names) > 8 else ''}",
                )
            return schemas.Response(success=False, message=result.message or "站点列表为空")
        except Exception as exc:  # noqa: BLE001
            return _api_fail("检测服务", exc)

    def api_clear_cache(self) -> schemas.Response:
        try:
            self._cache = {}
            self._success = []
            self._last = {}
            for key in (_CACHE_KEY, _SUCCESS_KEY, _LAST_KEY):
                self.save_data(key, {} if key == _CACHE_KEY else ([] if key == _SUCCESS_KEY else {}))
            logger.info(f"{LOG_TAG}已清除命中缓存与辅种记录")
            return schemas.Response(success=True, message="已清除命中缓存与辅种记录，下次运行会重新查询")
        except Exception as exc:  # noqa: BLE001
            return _api_fail("清除缓存", exc)

    def api_status(self) -> schemas.Response:
        try:
            last = self._last or {}
            text = (
                f'运行中：{self._running}｜最近 {last.get("finished_at", "还没跑过")}｜'
                f'成功 {last.get("added", 0)}｜已存在 {last.get("exists", 0)}｜失败 {last.get("failed", 0)}'
            )
            return schemas.Response(success=True, message=text)
        except Exception as exc:  # noqa: BLE001
            return _api_fail("读取状态", exc)

    # ------------------------------------------------------------------ 运行入口

    def _start_run(self, force_dry: bool) -> schemas.Response:
        if not self._enabled:
            return schemas.Response(success=False, message="插件未启用")
        if self._running:
            return schemas.Response(success=False, message="已有任务在运行")
        if not (self._username and self._password):
            return schemas.Response(success=False, message="请先填 Reseed 账号和密码")
        if not self._dirs:
            return schemas.Response(success=False, message="请先填扫描目录")
        if not self._bindings:
            return schemas.Response(success=False, message="请先填站点映射与 Passkey")
        self._running = True
        self._stop_event.clear()
        thread = threading.Thread(target=self._run_thread, args=(force_dry,), daemon=True,
                                  name="ReseedCrossSeedRun")
        thread.start()
        return schemas.Response(
            success=True,
            message="已开始试运行，结果看插件日志" if force_dry or self._dry_run else "已开始辅种，结果看插件日志",
        )

    def run(self) -> None:
        """定时服务入口：已在运行则跳过本轮。"""
        if self._running:
            logger.info(f"{LOG_TAG}上一轮还没结束，跳过本轮")
            return
        self._start_run(force_dry=False)

    def _run_thread(self, force_dry: bool) -> None:
        """后台线程入口：优先绑定插件实例，让实例级 INFO 覆盖对自建线程也生效。"""
        binder = None
        try:
            from app.runtime.log import bind_plugin_instance  # 宿主内部能力，旧版可能没有
            binder = bind_plugin_instance
        except Exception:  # noqa: BLE001
            pass
        try:
            if binder is not None:
                with binder(self.__class__.__name__):
                    self._run_impl(force_dry)
            else:
                self._run_impl(force_dry)
        except Exception as err:  # noqa: BLE001
            logger.error(f"{LOG_TAG}本轮异常终止：{type(err).__name__}: {err}")
        finally:
            self._running = False

    # ------------------------------------------------------------------ 主流程

    def _build_merged_index(self) -> tuple[dict[str, Any], dict[str, core.IndexItem]]:
        """合并多个下载目录的顶层项；重名只保留第一个，避免路径歧义。"""
        result: dict[str, Any] = {}
        items: dict[str, core.IndexItem] = {}
        base_dir = ""
        for directory in self._dirs:
            index, found = core.build_index(directory)
            if not base_dir:
                base_dir = str(index.get("base_dir") or "")
            for item in found:
                if item.name in items:
                    logger.warning(f"{LOG_TAG}顶层项重名，跳过 {directory}/{item.name}")
                    continue
                items[item.name] = item
                result[item.name] = item.files if item.files is not None else item.size
        return {"base_dir": base_dir, "result": result}, items

    def _selected_sites(self, client: site.ReseedClient) -> tuple[list[str], int, str]:
        """确定提交给服务端的站点名，顺便记录服务可用性。"""
        probe = client.sites_info()
        reseed_names = probe.data if probe.ok and isinstance(probe.data, list) else []
        available = [str(item.get("name") or "").strip() for item in reseed_names if item.get("name")]
        configured = self._sites or [name for name in available if name.lower() in self._bindings]
        if not configured:
            configured = [binding.reseed_name for binding in self._bindings.values()]
        selected = []
        for name in configured:
            if name.lower() in self._bindings and name not in selected:
                selected.append(name)
        message = probe.message if probe.ok else (probe.message or "站点列表读取失败，按映射直连")
        self._save_service(bool(probe.ok), message, len(available))
        return selected, len(available), message

    def _save_service(self, ok: bool, message: str, count: int) -> None:
        self._service = {"ok": bool(ok), "message": str(message or ""),
                         "sites": int(count or 0), "checked_at": _now()}
        try:
            self.save_data(_SERVICE_KEY, self._service)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}保存服务状态失败：{err}")

    def _run_impl(self, force_dry: bool) -> None:
        dry_run = bool(self._dry_run or force_dry)
        stats = {"indexed": 0, "hits": 0, "added": 0, "exists": 0, "failed": 0}
        reason = ""

        index, items = self._build_merged_index()
        stats["indexed"] = len(items)
        logger.info(f"{LOG_TAG}开始{'试运行' if dry_run else '辅种'}：索引顶层项 {len(items)} 个，"
                    f"目录 {len(self._dirs)} 个")
        if not items:
            reason = "扫描目录里没有可用文件，检查目录路径是否映射进容器"
            self._finish(stats, dry_run, reason)
            return

        client = site.ReseedClient(self._api, self._username, self._password, timeout=self._timeout)
        selected, _, service_message = self._selected_sites(client)
        if not selected:
            reason = "没有可提交的站点：检查站点映射，以及服务返回的站点名是否一致"
            self._finish(stats, dry_run, reason)
            return

        digest = core.index_digest(index)
        cached = self._cache.get(digest)
        hits: list[core.Hit] = []
        if (isinstance(cached, dict) and time.time() - float(cached.get("at", 0) or 0) < _CACHE_TTL
                and isinstance(cached.get("hits"), list)):
            hits = [core.Hit(**row) for row in cached["hits"] if isinstance(row, dict)]
            logger.info(f"{LOG_TAG}索引未变化，复用上次命中结果 {len(hits)} 条，不重复上传")
        else:
            result = client.upload(index, selected)
            if not result.ok:
                reason = f"查询失败：{result.message}（{service_message}）"
                logger.warning(f"{LOG_TAG}{reason}")
                self._finish(stats, dry_run, reason)
                return
            hits = list(result.data or [])
            self._cache = {digest: {
                "at": time.time(),
                "hits": [
                    {"name": h.name, "site": h.site, "torrent_id": h.torrent_id,
                     "full_match": bool(h.full_match)} for h in hits
                ],
            }}
        stats["hits"] = len(hits)

        gateway = site.DownloaderGateway(self._target_downloader)
        downloader_ok = gateway.resolve().ok
        existing = gateway.all_hashes() if downloader_ok else set()
        if not downloader_ok and not dry_run:
            logger.warning(f"{LOG_TAG}下载器不可用，本轮只做复核统计")

        mp_sites = site.load_mp_sites()
        success_keys = set(self._success)
        processed = 0
        for hit in hits:
            if self._stop_event.is_set():
                reason = "本轮已停止，剩余命中下轮继续"
                break
            if processed >= self._max_per_run:
                reason = f"已达单次上限 {self._max_per_run} 个，剩余命中下轮继续"
                break
            if not hit.full_match and not self._include_warning:
                continue
            processed += 1
            try:
                outcome = self._process_one(hit, items, mp_sites, existing, gateway,
                                             success_keys, dry_run, downloader_ok)
                stats[outcome] = stats.get(outcome, 0) + 1
            except Exception as err:  # noqa: BLE001 - 单条异常不中断整轮
                stats["failed"] += 1
                logger.warning(f"{LOG_TAG}处理 {hit.key} 异常：{type(err).__name__}: {err}")

        self._finish(stats, dry_run, reason)

    def _process_one(
        self,
        hit: core.Hit,
        items: dict[str, core.IndexItem],
        mp_sites: dict[str, site.MpSite],
        existing: set[str],
        gateway: site.DownloaderGateway,
        success_keys: set[str],
        dry_run: bool,
        downloader_ok: bool,
    ) -> str:
        """处理一条命中，返回统计分类：added / exists / failed。"""
        local = items.get(hit.name)
        binding = self._bindings.get(hit.site.lower())
        if local is None or binding is None:
            logger.info(f"{LOG_TAG}跳过 {hit.key}：本地条目或站点映射缺失")
            return "exists" if hit.key in success_keys else "failed"
        mp_site = mp_sites.get(binding.mp_name.lower())
        if mp_site is None:
            logger.warning(f"{LOG_TAG}跳过 {hit.key}：MoviePilot 里找不到站点 {binding.mp_name}")
            return "failed"
        if hit.key in success_keys:
            return "exists"

        content = site.download_torrent(mp_site, binding, hit.torrent_id, self._timeout)
        if content is None:
            logger.warning(f"{LOG_TAG}种子下载失败或返回非种子内容：{hit.key}")
            return "failed"
        meta = core.parse_torrent(content)
        if meta.info_hash and meta.info_hash in existing:
            logger.info(f"{LOG_TAG}种子已在下载器里：{hit.key}（{meta.info_hash}）")
            self._remember(hit.key)
            return "exists"
        if not core.verify_match(local, meta):
            logger.warning(f"{LOG_TAG}文件清单复核不通过，拒绝添加：{hit.key}")
            return "failed"

        if dry_run or not downloader_ok:
            logger.info(f"{LOG_TAG}命中可辅种：{hit.key} → {local.save_path}（{meta.name}）")
            return "added"

        torrent_hash = gateway.add(
            content=content,
            save_path=local.save_path,
            tag=self._seed_tag,
            name=meta.name,
            skip_checking=self._skip_checking,
            auto_start=self._auto_start,
        )
        if not torrent_hash:
            logger.warning(f"{LOG_TAG}添加到下载器失败：{hit.key}")
            return "failed"
        if meta.info_hash:
            existing.add(meta.info_hash)
        self._remember(hit.key)
        logger.info(f"{LOG_TAG}辅种成功：{hit.key} → {local.save_path}（{torrent_hash}）")
        return "added"

    def _remember(self, key: str) -> None:
        if key not in self._success:
            self._success.append(key)
        if len(self._success) > _MAX_SUCCESS:
            self._success = self._success[-_MAX_SUCCESS:]

    def _finish(self, stats: dict[str, Any], dry_run: bool, reason: str) -> None:
        """一轮结束时统一落盘一次，日志不逐条写库。"""
        self._last = {**stats, "finished_at": _now(), "dry_run": bool(dry_run), "reason": reason}
        try:
            self.save_data(_LAST_KEY, self._last)
            self.save_data(_CACHE_KEY, self._cache)
            self.save_data(_SUCCESS_KEY, self._success[-_MAX_SUCCESS:])
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}保存运行结果失败：{err}")
        summary = (
            f"索引 {stats.get('indexed', 0)} 项，命中 {stats.get('hits', 0)} 条，"
            f"{'可辅种' if dry_run else '辅种成功'} {stats.get('added', 0)} 个，"
            f"已存在 {stats.get('exists', 0)} 个，失败 {stats.get('failed', 0)} 个"
        )
        if reason:
            summary += f"；{reason}"
        logger.info(f"{LOG_TAG}本轮结束：{summary}")
        if self._notify:
            try:
                self.post_message(title="Reseed 辅种", text=summary)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"{LOG_TAG}发送通知失败：{err}")
