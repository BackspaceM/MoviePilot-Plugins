"""熊猫PT 好友买卖自动化（MoviePilot V3）。

按用户规则自动完成：领取收益/任务奖励、佣人工作与互动、事务所派遣与结算、
电影图鉴海报任务、保护佣人，以及显式开启后的买入/释放等不可逆操作。

安全设计：
- 默认试运行，只产出计划写日志，不真正执行；
- 不可逆动作（买/抢/追回/释放）默认全关，且买人必须先查详情拿实时价格并过预算门槛；
- Cookie 只在运行时传给 HTTP 客户端，不落日志、不写仓库；
- get_page / get_form 只读内存缓存，不联网、不读宿主 SDK；
- 所有按钮端点 try/except 兜底，后台线程绑定插件实例以便实例级日志等级覆盖生效。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.sdk import scheduler as scheduler_sdk
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from . import core, renderer, site

LOG_TAG = "[PandaFriendTrade] "
_LAST_KEY = "panda_ft_last_v1"
_PROBE_KEY = "panda_ft_probe_v1"
_AUCTION_KEY = "panda_ft_auction_v1"
_CONSIGNMENT_KEY = "panda_ft_consignment_v1"
_CMD_ACTION = "pandafriendtrade_run"
_DEFAULT_CRON = "0 */6 * * *"


def _api_fail(action: str, exc: Exception) -> schemas.Response:
    logger.warning(f"{LOG_TAG}{action}异常 {type(exc).__name__}: {exc}")
    return schemas.Response(success=False,
                            message=f"{action}失败：{type(exc).__name__}，请查看插件日志")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class PandaFriendTrade(_PluginBase):
    """熊猫PT 好友买卖规则自动化。"""

    plugin_name = "熊猫好友买卖"
    plugin_desc = ("按你的规则自动完成熊猫PT好友买卖：领收益/任务、佣人工作互动、事务所派遣结算、"
                   "海报任务，并支持海报拍卖监控与倒计时自动加价抢拍。默认试运行，"
                   "出价/买人/释放等消耗或不可逆动作需显式开启。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/pandafriendtrade.png"
    plugin_version = "1.2.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_label = "熊猫PT,好友买卖,海报拍卖,寄售场,代理,规则引擎"
    plugin_config_prefix = "pandafriendtrade_"
    plugin_order = 22
    auth_level = 1

    _enabled = False
    _dry_run = True
    _notify = False

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._running = False
        self._cfg: core.Config = core.parse_config({})
        self._last: Dict[str, Any] = {}
        self._probe: Dict[str, Any] = {}
        # 拍卖监控线程（独立停止信号，不与普通任务互相干扰）
        self._auction_stop = threading.Event()
        self._auction_thread: Optional[threading.Thread] = None
        self._auction_running = False
        self._auction_counts: Dict[int, int] = {}   # lot_id -> 本轮出价次数
        self._auction_last_bid: Dict[int, int] = {}  # lot_id -> 上次出价时间戳
        self._auction_state: Dict[str, Any] = {}   # 供页面展示，只读缓存
        self._consignment_state: Dict[str, Any] = {}  # 寄售场只读缓存
        self._viewer_uid: int = 0                  # 我的 UID，寄售场需要

    # ------------------------------------------------------------------ 生命周期

    def init_plugin(self, config: dict | None = None) -> None:
        config = dict(config or {})
        if not self._running:
            self._stop_event.clear()
        self._cfg = core.parse_config(config)
        self._enabled = self._cfg.enabled
        self._dry_run = self._cfg.dry_run
        self._notify = self._cfg.notify
        self._last = dict(self.get_data(_LAST_KEY) or {})
        self._probe = dict(self.get_data(_PROBE_KEY) or {})
        self._auction_state = dict(self.get_data(_AUCTION_KEY) or {})
        self._consignment_state = dict(self.get_data(_CONSIGNMENT_KEY) or {})
        if self._enabled and not self._cfg.cookie:
            logger.warning(f"{LOG_TAG}尚未填写站点 Cookie，运行会失败")
        # 配置重载后按开关决定是否常驻拍卖监控线程
        if (self._enabled and self._cfg.cookie
                and (self._cfg.do_auction or self._cfg.do_consignment)):
            self._start_auction_monitor()
        else:
            self._stop_auction_monitor()

    def get_state(self) -> bool:
        return bool(self._enabled)

    def stop_service(self) -> None:
        self._stop_event.set()
        self._stop_auction_monitor()
        remover = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if callable(remover):
            try:
                remover(self.__class__.__name__, "run")
            except Exception:  # noqa: BLE001
                pass

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        try:
            return renderer.form()
        except Exception as err:  # noqa: BLE001 - 渲染期不能抛异常
            logger.error(f"{LOG_TAG}配置页渲染失败：{err}")
            return [], dict(renderer.DEFAULTS)

    def get_page(self) -> list[dict]:
        try:
            return renderer.render_page({
                "running": self._running,
                "last": self._last,
                "probe": self._probe,
                "dry_run": self._dry_run,
                "run_text": "正在运行一轮" if self._running else "空闲",
                "last_text": (f'{self._last.get("finished_at", "—")}　'
                              f'计划 {self._last.get("total", 0)} 项，'
                              f'成功 {self._last.get("done", 0)}，失败 {self._last.get("failed", 0)}'
                              + ("（试运行）" if self._last.get("dry_run") else ""))
                             if self._last else "还没跑过",
                "probe_text": (f'{self._probe.get("checked_at", "")}　{self._probe.get("message", "")}'
                               if self._probe else "还没检测过"),
                "schedule_text": (f"计划任务 {_DEFAULT_CRON}" if self._cfg.enabled
                                  else "定时已禁用，用下方按钮手动运行"),
                "auction": self._auction_state,
                "auction_running": self._auction_running,
                "auction_text": (f'{self._auction_state.get("checked_at", "")}　'
                                 f'{self._auction_state.get("summary", "")}'
                                 if self._auction_state else "还没检查过拍卖"),
                "consignment": self._consignment_state,
                "consignment_text": (f'{self._consignment_state.get("checked_at", "")}　'
                                     f'{self._consignment_state.get("summary", "")}'
                                     if self._consignment_state else "还没检查过寄售场"),
            })
        except Exception as err:  # noqa: BLE001
            logger.error(f"{LOG_TAG}详情页渲染失败：{err}")
            return [{"component": "div", "html": '<div style="padding:12px">熊猫好友买卖</div>'}]

    def get_api(self) -> list[dict]:
        return [
            {"path": "/run", "endpoint": self.api_run, "methods": ["POST"],
             "auth": "bear", "summary": "按规则运行一轮（试运行时只产出计划）"},
            {"path": "/dryrun", "endpoint": self.api_dryrun, "methods": ["POST"],
             "auth": "bear", "summary": "强制试运行一轮，只统计不执行"},
            {"path": "/probe", "endpoint": self.api_probe, "methods": ["POST"],
             "auth": "bear", "summary": "检测站点 Cookie 是否有效"},
            {"path": "/preview", "endpoint": self.api_preview, "methods": ["POST"],
             "auth": "bear", "summary": "读取最近一轮的计划明细"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询运行状态"},
            {"path": "/auction_check", "endpoint": self.api_auction_check, "methods": ["POST"],
             "auth": "bear", "summary": "检查一次拍卖报价（只读，看当前行情与决策）"},
            {"path": "/auction_once", "endpoint": self.api_auction_once, "methods": ["POST"],
             "auth": "bear", "summary": "按规则执行一次拍卖出价判断"},
            {"path": "/auction_monitor", "endpoint": self.api_auction_monitor, "methods": ["POST"],
             "auth": "bear", "summary": "启动/停止拍卖监控循环"},
            {"path": "/auction_status", "endpoint": self.api_auction_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询拍卖监控状态与最近报价"},
            {"path": "/consignment_check", "endpoint": self.api_consignment_check, "methods": ["POST"],
             "auth": "bear", "summary": "检查海报寄售场行情（只读）"},
            {"path": "/consignment_grab", "endpoint": self.api_consignment_grab, "methods": ["POST"],
             "auth": "bear", "summary": "按规则在寄售场抢未拥有/想要的海报"},
        ]

    # ------------------------------------------------------------------ 按钮端点

    def api_run(self) -> schemas.Response:
        try:
            return self._start(force_dry=False)
        except Exception as exc:  # noqa: BLE001
            return _api_fail("启动", exc)

    def api_dryrun(self) -> schemas.Response:
        try:
            return self._start(force_dry=True)
        except Exception as exc:  # noqa: BLE001
            return _api_fail("试运行", exc)

    def api_probe(self) -> schemas.Response:
        try:
            cfg = self._cfg
            if not cfg.cookie:
                return schemas.Response(success=False, message="先在配置页填写站点 Cookie")
            result = site.probe(cfg.cookie, cfg.site, cfg.timeout)
            self._save_probe(result.ok, result.message)
            return schemas.Response(success=result.ok, message=result.message)
        except Exception as exc:  # noqa: BLE001
            return _api_fail("检测 Cookie", exc)

    def api_preview(self) -> schemas.Response:
        try:
            labels = self._last.get("labels") or []
            if not labels:
                return schemas.Response(success=True, message="还没跑过，点「试运行」生成一份计划")
            text = "；".join(str(x) for x in labels[:30])
            return schemas.Response(success=True, message=f"最近一轮 {len(labels)} 项：{text}")
        except Exception as exc:  # noqa: BLE001
            return _api_fail("读取计划", exc)

    def api_status(self) -> schemas.Response:
        try:
            last = self._last or {}
            text = (f"运行中：{self._running}｜最近 {last.get('finished_at', '还没跑过')}｜"
                    f"计划 {last.get('total', 0)}｜成功 {last.get('done', 0)}｜失败 {last.get('failed', 0)}")
            return schemas.Response(success=True, message=text)
        except Exception as exc:  # noqa: BLE001
            return _api_fail("读取状态", exc)

    # ------------------------------------------------------------------ 拍卖端点

    def api_auction_check(self) -> schemas.Response:
        """只读检查一次拍卖行情，输出报价与站点规则，不出价。"""
        try:
            if not self._cfg.cookie:
                return schemas.Response(success=False, message="请先填写站点 Cookie")
            result = self._auction_board_view()
            if not result.ok:
                return schemas.Response(success=False, message=result.message)
            view = result.data
            summary = core.auction_summary(view)
            rules = view.get("rules") or {}
            ext = rules.get("extension_seconds", 180)
            maxext = rules.get("max_extension_seconds", 1800)
            text = (f"余额 {view.get('balance')} 魔力｜{summary}。"
                    f"规则：最后3分钟换领先者延长{ext}秒（最多{ext and maxext}秒）。")
            return schemas.Response(success=True, message=text,
                                    data={"lots": [x.as_dict() for x in view.get("lots", [])]})
        except Exception as exc:  # noqa: BLE001
            return _api_fail("检查拍卖", exc)

    def api_auction_once(self) -> schemas.Response:
        """按规则判断并（非试运行时）出价一次；试运行只列决策。"""
        try:
            if not self._cfg.cookie:
                return schemas.Response(success=False, message="请先填写站点 Cookie")
            result = self._auction_board_view()
            if not result.ok:
                return schemas.Response(success=False, message=result.message)
            view = result.data
            decisions = core.plan_auction(
                self._cfg, view, now=core.time.time(),
                bid_counts=self._auction_counts, last_bid_at=self._auction_last_bid,
            )
            if not decisions:
                reason = ("自动出价未开启" if not self._cfg.auction_auto_bid
                          else "当前没有符合规则的拍品（预算/稀有度/倒计时/已领先等条件未满足）")
                return schemas.Response(success=True, message=reason)
            lines = [d.reason for d in decisions]
            if self._dry_run:
                return schemas.Response(success=True,
                                        message="试运行，未出价。计划：" + "；".join(lines))
            done = self._apply_bid_decisions(view, decisions)
            return schemas.Response(success=True,
                                    message=f"本轮出价 {done}/{len(decisions)} 笔：" + "；".join(lines))
        except Exception as exc:  # noqa: BLE001
            return _api_fail("拍卖出价", exc)

    def api_auction_monitor(self) -> schemas.Response:
        """启动/停止监控循环：已运行则停止，否则启动。"""
        try:
            if not self._cfg.cookie:
                return schemas.Response(success=False, message="请先填写站点 Cookie")
            if self._auction_running:
                self._stop_auction_monitor()
                return schemas.Response(success=True, message="拍卖监控已停止")
            self._start_auction_monitor()
            return schemas.Response(success=True,
                                    message=f"拍卖监控已启动，每 {self._cfg.auction_poll_seconds} 秒检查一次")
        except Exception as exc:  # noqa: BLE001
            return _api_fail("切换拍卖监控", exc)

    def api_auction_status(self) -> schemas.Response:
        try:
            state = self._auction_state or {}
            status = "运行中" if self._auction_running else "已停止"
            text = (f"监控{status}｜每 {self._cfg.auction_poll_seconds} 秒｜"
                    f'最近 {state.get("checked_at", "未检查")}｜{state.get("summary", "无行情")}')
            return schemas.Response(success=True, message=text, data=state)
        except Exception as exc:  # noqa: BLE001
            return _api_fail("读取拍卖状态", exc)

    def api_consignment_check(self) -> schemas.Response:
        """只读检查寄售场行情，展示未拥有的可抢海报。"""
        try:
            if not self._cfg.cookie:
                return schemas.Response(success=False, message="请先填写站点 Cookie")
            result = self._consignment_board_view()
            if not result.ok:
                return schemas.Response(success=False, message=result.message)
            view = result.data
            self._save_consignment_state(view)
            decisions = core.plan_consignment(self._cfg, view, now=int(time.time()))
            text = (f"{view.get('summary')}｜余额 {view.get('balance')} 魔力。"
                    f"符合抢购规则 {len(decisions)} 件")
            if decisions:
                text += "：" + "；".join(d.reason for d in decisions[:5])
            return schemas.Response(success=True, message=text,
                                    data={"lots": [x.as_dict() for x in view.get("lots", [])]})
        except Exception as exc:  # noqa: BLE001
            return _api_fail("检查寄售场", exc)

    def api_consignment_grab(self) -> schemas.Response:
        """按规则在寄售场抢购一次；试运行只列决策不真正买。"""
        try:
            if not self._cfg.cookie:
                return schemas.Response(success=False, message="请先填写站点 Cookie")
            result = self._consignment_board_view()
            if not result.ok:
                return schemas.Response(success=False, message=result.message)
            view = result.data
            decisions = core.plan_consignment(self._cfg, view, now=int(time.time()))
            if not decisions:
                reason = ("自动抢购未开启" if not self._cfg.consignment_auto_bid
                          else "当前没有符合规则的海报（预算/未拥有/档位/模式等条件未满足）")
                return schemas.Response(success=True, message=reason)
            lines = [d.reason for d in decisions]
            if self._dry_run:
                return schemas.Response(success=True,
                                        message="试运行，未抢购。计划：" + "；".join(lines))
            done = self._apply_consignment_decisions(view, decisions)
            return schemas.Response(success=True,
                                    message=f"本轮抢购 {done}/{len(decisions)} 件：" + "；".join(lines))
        except Exception as exc:  # noqa: BLE001
            return _api_fail("寄售场抢购", exc)

    # ------------------------------------------------------------------ 运行入口

    def _start(self, force_dry: bool) -> schemas.Response:
        if not self._enabled:
            return schemas.Response(success=False, message="插件未启用")
        if self._running:
            return schemas.Response(success=False, message="已有一轮在运行")
        if not self._cfg.cookie:
            return schemas.Response(success=False, message="请先填写站点 Cookie")
        with self._lock:
            self._running = True
        self._stop_event.clear()
        thread = threading.Thread(target=self._run_thread, args=(force_dry,), daemon=True,
                                  name="PandaFriendTradeRun")
        thread.start()
        dry = force_dry or self._dry_run
        return schemas.Response(success=True,
                                message="已开始试运行，计划看插件日志" if dry else "已开始执行，结果看插件日志")

    def run(self) -> None:
        """定时入口。启用且配置齐全才跑。"""
        if not self._enabled or not self._cfg.cookie or self._running:
            return
        self._start(force_dry=False)

    def _run_thread(self, force_dry: bool) -> None:
        """后台线程：绑定插件实例，让实例级 INFO 覆盖对自建线程也生效。"""
        binder = None
        try:
            from app.runtime.log import bind_plugin_instance  # 旧版宿主可能没有
            binder = bind_plugin_instance
        except Exception:  # noqa: BLE001
            pass
        restore = None
        try:
            if callable(binder):
                restore = binder(self)
            self._execute(force_dry or self._dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG}运行异常：{type(exc).__name__}: {exc}")
        finally:
            if callable(restore):
                try:
                    restore()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self._running = False

    # ------------------------------------------------------------------ 执行主流程

    def _client(self) -> site.PandaClient:
        cfg = self._cfg
        return site.PandaClient(cfg.cookie, cfg.site, cfg.timeout, cfg.request_interval,
                                proxy_url=cfg.proxy_url, proxy_bypass=cfg.proxy_bypass)

    # ------------------------------------------------------------------ 拍卖 / 寄售内部

    def _ensure_viewer_uid(self, client: site.PandaClient) -> int:
        """缓存我的 UID：寄售场与出价都要 expected_uid，只查一次首页。"""
        if self._viewer_uid > 0:
            return self._viewer_uid
        home = client.home()
        if home.ok and isinstance(home.data, dict):
            self._viewer_uid = core._to_int((home.data.get("me") or {}).get("uid"))
        return self._viewer_uid

    def _auction_board_view(self) -> site.Result:
        """读拍卖看板并解析成视图，顺带记录快照供页面展示。"""
        result = self._client().auction_board()
        if not result.ok:
            return result
        view = core.parse_auction_board(result.data or {}, now=int(time.time()))
        view["summary"] = core.auction_summary(view)
        view["checked_at"] = _now()
        return site.Result.success(view, result.message)

    def _apply_bid_decisions(self, view: Dict[str, Any],
                             decisions: List[core.BidDecision]) -> int:
        """真正执行出价。逐笔校验：金额仍 ≥ 当前最低、预算内、未领先。"""
        client = self._client()
        viewer_uid = self._ensure_viewer_uid(client)
        lots = {lot.id: lot for lot in view.get("lots") or []
                if isinstance(lot, core.AuctionLot)}
        now = int(time.time())
        done = 0
        for d in decisions:
            lot = lots.get(d.lot_id)
            if not lot or lot.is_leading or d.amount < lot.minimum_bid:
                logger.info(f"{LOG_TAG}拍卖跳过 lot {d.lot_id}：状态已变化")
                continue
            if d.amount > self._cfg.auction_budget:
                continue
            result = client.auction_bid(d.lot_id, d.amount, lot.rule_version, viewer_uid)
            if result.ok:
                done += 1
                self._auction_counts[d.lot_id] = self._auction_counts.get(d.lot_id, 0) + 1
                self._auction_last_bid[d.lot_id] = now
                logger.info(f"{LOG_TAG}拍卖出价成功：{d.reason}｜{result.message}")
            else:
                logger.warning(f"{LOG_TAG}拍卖出价失败：{d.reason}｜{result.message}")
        return done

    def _consignment_board_view(self) -> site.Result:
        """读寄售场看板（市场未拥有），解析成与拍卖行一致的视图。"""
        client = self._client()
        uid = self._ensure_viewer_uid(client)
        if not uid:
            return site.Result.fail("读不到我的 UID，无法查寄售场")
        result = client.consignment_board(
            uid, scope="market", unowned_only=self._cfg.consignment_unowned_only)
        if not result.ok:
            return result
        view = core.parse_consignment_board(result.data or {}, now=int(time.time()))
        view["summary"] = core.consignment_summary(view)
        view["checked_at"] = _now()
        return site.Result.success(view, result.message)

    def _apply_consignment_decisions(self, view: Dict[str, Any],
                                     decisions: List[core.ConsignmentDecision]) -> int:
        """执行寄售场抢购：dutch 直接买，open/sealed 竞价。"""
        client = self._client()
        uid = self._ensure_viewer_uid(client)
        lots = {lot.id: lot for lot in view.get("lots") or []
                if isinstance(lot, core.AuctionLot)}
        now = int(time.time())
        done = 0
        for d in decisions:
            lot = lots.get(d.lot_id)
            if not lot or lot.is_mine:
                logger.info(f"{LOG_TAG}寄售跳过 lot {d.lot_id}：已被买走或状态变化")
                continue
            if d.amount > self._cfg.consignment_budget:
                continue
            if d.action_kind == "dutch_buy":
                result = client.dutch_buy(d.lot_id, d.amount, d.rule_version, uid)
            else:
                # 寄售竞价复用拍卖出价接口（同一个后端 action）
                result = client.auction_bid(d.lot_id, d.amount, d.rule_version, uid)
            if result.ok:
                done += 1
                self._auction_counts[d.lot_id] = self._auction_counts.get(d.lot_id, 0) + 1
                self._auction_last_bid[d.lot_id] = now
                logger.info(f"{LOG_TAG}寄售抢购成功：{d.reason}｜{result.message}")
            else:
                logger.warning(f"{LOG_TAG}寄售抢购失败：{d.reason}｜{result.message}")
        return done

    def _start_auction_monitor(self) -> None:
        """启动常驻监控线程；已运行则不重复启动。"""
        with self._lock:
            if self._auction_running and self._auction_thread and self._auction_thread.is_alive():
                return
            self._auction_stop.clear()
            self._auction_running = True
        self._auction_thread = threading.Thread(
            target=self._auction_loop, daemon=True, name="PandaFriendTradeAuction")
        self._auction_thread.start()
        logger.info(f"{LOG_TAG}拍卖监控已启动，间隔 {self._cfg.auction_poll_seconds} 秒")

    def _stop_auction_monitor(self) -> None:
        self._auction_stop.set()
        with self._lock:
            self._auction_running = False

    def _auction_loop(self) -> None:
        """监控循环：绑定实例 → 每轮读看板、按规则出价、落状态。"""
        restore = self._bind_instance()
        try:
            while not self._auction_stop.is_set():
                # 光影拍卖行
                if self._cfg.do_auction:
                    try:
                        view_result = self._auction_board_view()
                        if not view_result.ok:
                            logger.warning(f"{LOG_TAG}拍卖看板读取失败：{view_result.message}")
                        else:
                            view = view_result.data
                            self._save_auction_state(view)
                            decisions = core.plan_auction(
                                self._cfg, view, now=int(time.time()),
                                bid_counts=self._auction_counts,
                                last_bid_at=self._auction_last_bid,
                            )
                            if decisions:
                                if self._dry_run:
                                    logger.info(f"{LOG_TAG}[试运行]拍卖决策：" +
                                                "；".join(d.reason for d in decisions))
                                else:
                                    self._apply_bid_decisions(view, decisions)
                    except Exception as exc:  # noqa: BLE001 - 单轮异常不能终止循环
                        logger.warning(f"{LOG_TAG}拍卖监控单轮异常：{type(exc).__name__}: {exc}")
                # 海报寄售场（光影拍卖行之外的个人寄售）
                if self._cfg.do_consignment:
                    try:
                        c_result = self._consignment_board_view()
                        if not c_result.ok:
                            logger.warning(f"{LOG_TAG}寄售场读取失败：{c_result.message}")
                        else:
                            c_view = c_result.data
                            self._save_consignment_state(c_view)
                            c_decisions = core.plan_consignment(
                                self._cfg, c_view, now=int(time.time()))
                            if c_decisions:
                                if self._dry_run:
                                    logger.info(f"{LOG_TAG}[试运行]寄售抢购决策：" +
                                                "；".join(d.reason for d in c_decisions))
                                else:
                                    self._apply_consignment_decisions(c_view, c_decisions)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"{LOG_TAG}寄售场单轮异常：{type(exc).__name__}: {exc}")
                # 用 wait 而非 sleep，停止信号能立刻生效
                self._auction_stop.wait(max(1, self._cfg.auction_poll_seconds))
        finally:
            if callable(restore):
                try:
                    restore()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self._auction_running = False

    def _bind_instance(self) -> Any:
        """尽力绑定插件实例，让实例级日志等级覆盖对自建线程生效。"""
        try:
            from app.runtime.log import bind_plugin_instance  # 旧版宿主可能没有
            return bind_plugin_instance(self)
        except Exception:  # noqa: BLE001
            return None

    def _save_auction_state(self, view: Dict[str, Any]) -> None:
        """只把可序列化的摘要落盘，页面渲染期不联网。"""
        lots = []
        for lot in view.get("lots") or []:
            if not isinstance(lot, core.AuctionLot):
                continue
            lots.append({
                "id": lot.id, "title": lot.title, "rarity": lot.rarity,
                "current_price": lot.current_price, "minimum_bid": lot.minimum_bid,
                "leader": lot.leader_name, "my_rank": lot.my_bid_rank,
                "ends_at": lot.ends_at, "status": lot.status,
                "my_bids": self._auction_counts.get(lot.id, 0),
            })
        self._auction_state = {
            "checked_at": view.get("checked_at") or _now(),
            "summary": view.get("summary") or "",
            "balance": view.get("balance"),
            "lots": lots,
        }
        try:
            self.save_data(_AUCTION_KEY, self._auction_state)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}保存拍卖状态失败：{exc}")

    def _save_consignment_state(self, view: Dict[str, Any]) -> None:
        """寄售场只读摘要（含 mode/拥有数/降价时间），供页面展示。"""
        lots = []
        for lot in view.get("lots") or []:
            if not isinstance(lot, core.AuctionLot):
                continue
            lots.append({
                "id": lot.id, "title": lot.title, "rarity": lot.rarity,
                "mode": lot.mode, "owned": lot.owned_quantity,
                "price": lot.current_price, "minimum_bid": lot.minimum_bid,
                "next_drop_at": lot.next_drop_at, "floor_price": lot.floor_price,
                "ends_at": lot.ends_at, "status": lot.status,
            })
        self._consignment_state = {
            "checked_at": view.get("checked_at") or _now(),
            "summary": view.get("summary") or "",
            "balance": view.get("balance"),
            "open_count": view.get("open_count", 0),
            "lots": lots,
        }
        try:
            self.save_data(_CONSIGNMENT_KEY, self._consignment_state)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}保存寄售状态失败：{exc}")

    def _execute(self, dry: bool) -> None:
        cfg = self._cfg
        client = self._client()
        started = time.time()
        summary = {"total": 0, "safe": 0, "cost": 0, "irreversible": 0}
        done = failed = 0
        labels: List[str] = []

        logger.info(f"{LOG_TAG}开始一轮（{'试运行' if dry else '执行'}）：读取首页/事务所/海报任务…")
        home_r = client.home()
        if not home_r.ok:
            logger.warning(f"{LOG_TAG}读取首页失败：{home_r.message}")
            self._save_probe(False, home_r.message)
            self._finish(dry, started, summary, done, failed, labels, "读取首页失败：" + home_r.message)
            return
        home = home_r.data if isinstance(home_r.data, dict) else {}
        self._save_probe(True, "连接正常")

        board_r = client.commission_board() if (cfg.do_commission or cfg.do_settle) else site.Result.success({})
        poster_board_r = client.poster_task_board() if cfg.do_posters else site.Result.success({})
        board = board_r.data if board_r.ok and isinstance(board_r.data, dict) else {}
        poster_board = poster_board_r.data if poster_board_r.ok and isinstance(poster_board_r.data, dict) else {}

        actions = core.build_plan(cfg, home, board, poster_board)

        # 买人：先查市场 + 逐个查详情，再按实时价格与属性过滤
        if cfg.do_buy and cfg.buy_max_price > 0:
            market_r = client.market_list()
            if market_r.ok:
                market = market_r.data if isinstance(market_r.data, dict) else {}
                rows = market.get("items") or market.get("list") or []
                details: Dict[int, Dict[str, Any]] = {}
                for row in rows:
                    if self._stop_event.is_set():
                        break
                    if not isinstance(row, dict):
                        continue
                    uid = core._to_int(row.get("uid"))
                    if uid > 0:
                        detail_r = client.detail(uid)
                        if detail_r.ok and isinstance(detail_r.data, dict):
                            details[uid] = detail_r.data
                actions.extend(core.merchant_candidates(market, cfg, details))

        actions = actions[:cfg.max_actions]
        summary = core.summarize(actions)
        labels = [a.label for a in actions]

        if not actions:
            logger.info(f"{LOG_TAG}本轮无符合条件的动作")
            self._finish(dry, started, summary, done, failed, labels, "无符合条件的动作")
            return

        logger.info(f"{LOG_TAG}产出计划 {len(actions)} 项："
                    f"安全 {summary['safe']} / 消耗 {summary['cost']} / 不可逆 {summary['irreversible']}")
        for action in actions:
            logger.info(f"{LOG_TAG}  计划：{action.label}（{action.action}，{action.risk}）")

        if dry:
            logger.info(f"{LOG_TAG}试运行结束，未执行任何动作。确认无误请关闭「试运行」。")
            self._finish(dry, started, summary, 0, 0, labels, "试运行完成，未执行")
            return

        for action in actions:
            if self._stop_event.is_set():
                logger.warning(f"{LOG_TAG}收到停止信号，剩余 {len(actions) - done - failed} 项下轮继续")
                break
            ok, message = self._run_action(client, action)
            if ok:
                done += 1
                logger.info(f"{LOG_TAG}  ✓ {action.label}：{message}")
            else:
                failed += 1
                logger.warning(f"{LOG_TAG}  ✗ {action.label}：{message}")

        # 海报赠送/兑换需要二次确认状态，单独走延时流程
        if cfg.do_posters:
            if any(a.action == "_poster_redeem" for a in actions):
                r = client.album_redeem()
                if r.ok:
                    done += 1
                    logger.info(f"{LOG_TAG}  ✓ 典藏册兑换：{r.message}")
                else:
                    failed += 1
                    logger.warning(f"{LOG_TAG}  ✗ 典藏册兑换：{r.message}")
            self._handle_poster_gift(client, cfg)

        self._finish(dry, started, summary, done, failed, labels, "")

    def _run_action(self, client: site.PandaClient, action: core.Action) -> tuple[bool, str]:
        """把 Action 分派到客户端具体方法。未识别的动作跳过。"""
        a, p = action.action, action.params
        try:
            if a == "friendTradeClaimIncome":
                r = client.claim_income()
            elif a == "friendTradeClaimTaskReward":
                r = client.claim_task(p.get("task_key", ""))
            elif a == "friendTradeLevelPosterClaim":
                r = client.claim_level_poster(core._to_int(p.get("level")))
            elif a == "friendTradeAchievementClaim":
                r = client.claim_achievement(p.get("achievement_type", ""))
            elif a == "friendTradeWork":
                r = client.work(core._to_int(p.get("target_uid")), p.get("work_key", ""))
            elif a == "friendTradeInteract":
                r = client.interact(core._to_int(p.get("target_uid")), p.get("interaction_key", ""))
            elif a == "friendTradeSlaveInitiative":
                r = client.initiative(p.get("action_key", ""))
            elif a == "friendTradeCommissionSettle":
                r = client.commission_settle(core._to_int(p.get("run_id")))
            elif a == "friendTradeCommissionStart":
                r = client.commission_start(core._to_int(p.get("offer_id")), p.get("relationship_ids", []))
            elif a == "friendTradePosterTaskClaim":
                r = client.poster_task_claim(p.get("task_key", ""))
            elif a == "friendTradeProtect":
                r = client.protect(core._to_int(p.get("target_uid")), core._to_int(p.get("expected_protection_hours"), 24))
            elif a == "friendTradeRelease":
                r = client.release(core._to_int(p.get("target_uid")))
            elif a in ("friendTradeBuy", "friendTradeSnatch", "friendTradeReclaim"):
                r = client.trade(a, core._to_int(p.get("target_uid")), p.get("expected_price", ""))
            else:
                return True, "内部标记，无需执行"
            return (r.ok, r.message or ("成功" if r.ok else "失败"))
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def _handle_poster_gift(self, client: site.PandaClient, cfg: core.Config) -> None:
        """海报赠送：只送给白名单 UID，挑可赠送的海报，限量并轮询结果。"""
        if not cfg.poster_gift_recipients:
            return
        allowed = set(cfg.poster_gift_recipients)
        collection = client.poster_collection({"page": 1, "giftability": "giftable"})
        if not collection.ok or not isinstance(collection.data, dict):
            logger.warning(f"{LOG_TAG}读取可赠送海报失败：{collection.message}")
            return
        data = collection.data
        rows = data.get("items") or data.get("list") or []
        sent = 0
        for poster in rows:
            if sent >= 3 or self._stop_event.is_set():  # 每轮最多赠 3 张，避免误操作
                break
            if not isinstance(poster, dict) or not poster.get("can_gift"):
                continue
            poster_id = poster.get("id")
            if poster_id is None:
                continue
            for uid in sorted(allowed):
                r = client.poster_gift(uid, poster_id, 1)
                if not r.ok:
                    logger.warning(f"{LOG_TAG}  ✗ 赠送海报给 {uid}：{r.message}")
                    continue
                logger.info(f"{LOG_TAG}  ✓ 赠送《{poster.get('title_cn') or poster.get('title') or poster_id}》给 {uid}")
                sent += 1
                break

    # ------------------------------------------------------------------ 落盘

    def _save_probe(self, ok: bool, message: str) -> None:
        self._probe = {"ok": ok, "message": message, "checked_at": _now()}
        try:
            self.save_data(_PROBE_KEY, self._probe)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}保存检测状态失败：{exc}")

    def _finish(self, dry: bool, started: float, summary: Dict[str, int],
                done: int, failed: int, labels: List[str], message: str) -> None:
        self._last = {
            "finished_at": _now(),
            "dry_run": bool(dry),
            "total": summary.get("total", 0),
            "summary": summary,
            "done": done,
            "failed": failed,
            "labels": labels,
            "message": message,
            "elapsed": round(time.time() - started, 1),
        }
        try:
            self.save_data(_LAST_KEY, self._last)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}保存运行记录失败：{exc}")
        if self._notify and not dry and (done or failed):
            try:
                self.send_message(
                    title="熊猫好友买卖",
                    text=f"计划 {summary.get('total', 0)} 项，成功 {done}，失败 {failed}。详情看插件日志。",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG}发送通知失败：{exc}")
