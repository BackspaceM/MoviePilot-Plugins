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
    plugin_desc = ("按你的规则自动完成熊猫PT好友买卖的重复操作：领收益/任务、佣人工作互动、"
                   "事务所派遣结算、海报任务。默认试运行，买人/释放等不可逆动作需显式开启。")
    plugin_icon = "https://raw.githubusercontent.com/BackspaceM/MoviePilot-Plugins/main/icons/pandafriendtrade.png"
    plugin_version = "1.0.0"
    plugin_author = "BackspaceM"
    author_url = "https://github.com/BackspaceM"
    plugin_label = "熊猫PT,好友买卖,自动化,规则引擎"
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
        if self._enabled and not self._cfg.cookie:
            logger.warning(f"{LOG_TAG}尚未填写站点 Cookie，运行会失败")

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
        return site.PandaClient(cfg.cookie, cfg.site, cfg.timeout, cfg.request_interval)

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
