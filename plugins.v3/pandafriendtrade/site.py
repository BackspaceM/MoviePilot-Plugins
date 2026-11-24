"""熊猫PT 好友买卖 HTTP 客户端。

统一走 /ajax.php：POST action=xxx&params[key]=val，返回 {ret, msg, data}。
设计要点：
- cookie 由调用方传入，本模块不保存、不打印、不写日志；
- 每次请求带最小浏览器头，支持超时与节流；
- 读接口与写接口分开封装，写接口统一返回 Result；
- 赠送/领海报任务需要 request_uuid（防重放），这里用标准库 uuid 生成。
"""

from __future__ import annotations

import json
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from . import core


@dataclass
class Result:
    ok: bool
    data: Any = None
    message: str = ""

    @classmethod
    def success(cls, data: Any = None, message: str = "") -> "Result":
        return cls(True, data, message)

    @classmethod
    def fail(cls, message: str) -> "Result":
        return cls(False, None, message)


class PandaClient:
    """轻量 ajax 客户端，纯标准库，宿主外也能单测。"""

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

    def __init__(self, cookie: str, site: str = core.DEFAULT_SITE, timeout: int = 30,
                 interval: float = 1.0, opener: Any = None, proxy_url: str = "",
                 proxy_bypass: bool = False) -> None:
        self.cookie = str(cookie or "").strip()
        self.site = core.normalize_site(site)
        self.timeout = max(5, min(120, int(timeout)))
        self.interval = max(0.0, min(10.0, float(interval)))
        self._opener = opener  # 便于测试注入
        self._last_request = 0.0
        self.proxy_url = core.normalize_proxy(proxy_url)
        self.proxy_bypass = bool(proxy_bypass)
        self.proxy_error = ""
        self._proxy_opener = None
        self._direct_opener = None
        if self.proxy_url and opener is None:
            self._build_proxy_opener()

    def _build_proxy_opener(self) -> None:
        """构造带代理的 opener；SOCKS 需要 PySocks，缺失时记录错误并走直连。"""
        url = self.proxy_url
        try:
            from urllib.request import ProxyHandler, build_opener
            if url.startswith("socks5"):
                try:
                    from sockshandler import SocksiPyHandler  # type: ignore  # PySocks
                    import socks  # type: ignore
                except ImportError:
                    self.proxy_error = "SOCKS 代理需要容器安装 PySocks，已临时直连"
                    return
                host, port = self._proxy_host_port(url)
                if not host:
                    self.proxy_error = "代理地址格式无效，已临时直连"
                    return
                from urllib.parse import urlparse, unquote
                parsed = urlparse(url)
                username = unquote(parsed.username or "")
                password = unquote(parsed.password or "")
                proxy_type = socks.SOCKS5
                handler = SocksiPyHandler(proxy_type, host, port,
                                          username=username or None, password=password or None)
                self._proxy_opener = build_opener(handler)
            else:
                handler = ProxyHandler({"http": url, "https": url})
                self._proxy_opener = build_opener(handler)
        except Exception as exc:  # noqa: BLE001
            self.proxy_error = f"代理配置失败（{type(exc).__name__}），已临时直连"

    @staticmethod
    def _proxy_host_port(url: str) -> Tuple[str, int]:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            return parsed.hostname or "", int(parsed.port or 1080)
        except Exception:
            return "", 0

    @property
    def using_proxy(self) -> bool:
        return bool(self.proxy_url and not self.proxy_error and self._proxy_opener is not None)

    def _get_opener(self) -> Any:
        if self._opener is not None:
            return self._opener
        if self.using_proxy:
            return self._proxy_opener
        return self._direct_opener or urllib.request.build_opener()

    @property
    def ajax_url(self) -> str:
        return f"{self.site}/ajax.php"

    def _openers(self, fresh: bool = False) -> list:
        """候选通道：代理优先，直连兜底；未配代理就只有直连。

        fresh=True 时每轮新建 opener：HTTPConnection 会缓存 socket，站点单方面
        关闭后复用就抛 RemoteDisconnected；换新 opener 可从根上规避。
        """
        if self._opener is not None:
            return [("直连", self._opener)]  # 测试注入，单一通道
        out = []
        if self.using_proxy:
            out.append(("代理", self._proxy_opener))
        direct = (urllib.request.build_opener() if fresh
                  else (self._direct_opener or (urllib.request.build_opener() if not out else None)))
        if direct is not None:
            out.append(("直连", direct))
        return out or [("直连", urllib.request.build_opener())]

    def _send_with_retry(self, body: bytes, headers: dict) -> tuple:
        """发送 POST，网络层错误换通道+退避重试；返回 (raw, err)。"""
        routes = self._openers()
        last_err = ""
        # 每个通道最多尝试 3 轮，轮间短退避，应对站点偶发 RemoteDisconnected。
        # 第 2 轮起新建 opener，彻底丢弃可能失效的 keep-alive 连接。
        for round_no in range(3):
            if round_no >= 1 and self._opener is None:
                routes = self._openers(fresh=True)
            for name, opener in routes:
                self._throttle()
                self._last_request = time.monotonic()
                req = urllib.request.Request(self.ajax_url, data=body,
                                             headers=dict(headers), method="POST")
                try:
                    with opener.open(req, timeout=self.timeout) as response:
                        return response.read().decode("utf-8", "ignore"), None
                except urllib.error.HTTPError as exc:
                    # 5xx 可换通道再试；4xx 是业务/鉴权问题，立即返回
                    last_err = f"HTTP {exc.code}"
                    if exc.code < 500:
                        return "", last_err
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{type(exc).__name__}: {exc}"
                # 代理网络失败后标记，下一轮不再走代理
                if name == "代理" and self.proxy_bypass and last_err.startswith((
                        "RemoteDisconnected", "URLError", "TimeoutError", "ConnectionResetError", "OSError")):
                    self.proxy_error = f"代理不可用（{last_err[:40]}），已降级直连"
                    routes = [("直连", r) for n, r in routes if n == "直连"] or routes
            if round_no == 0:
                time.sleep(min(2.0, max(0.4, self.interval)))
        return "", (last_err or "网络请求失败")

    def _throttle(self) -> None:
        if self.interval <= 0:
            return
        wait = self.interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def post(self, action: str, params: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None) -> Result:
        if not self.cookie:
            return Result.fail("未填写 Cookie")
        payload = [("action", action)]
        for key, value in (params or {}).items():
            if value is None:
                continue
            payload.append((f"params[{key}]", str(value)))
        body = urllib.parse.urlencode(payload).encode("utf-8")
        headers = dict(headers or {})
        headers.setdefault("User-Agent", self.UA)
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        headers.setdefault("X-Requested-With", "XMLHttpRequest")
        headers.setdefault("Referer", f"{self.site}/friend-trade.php")
        # 关闭 keep-alive：站点会在空闲后单方面断连，复用连接会抛 RemoteDisconnected
        headers.setdefault("Connection", "close")
        # cookie 只放进请求头，不写日志
        headers["Cookie"] = self.cookie

        raw, err = self._send_with_retry(body, headers)
        if err is not None:
            return Result.fail(err)

        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return Result.fail("站点返回不是 JSON，可能登录已失效")
        if not isinstance(parsed, dict):
            return Result.fail("站点返回格式异常")
        if parsed.get("ret") != 0:
            return Result.fail(str(parsed.get("msg") or f"ret={parsed.get('ret')}"))
        return Result.success(parsed.get("data"), str(parsed.get("msg") or ""))

    # ------------------------------------------------------------ 读接口
    def home(self) -> Result:
        return self.post("friendTradeInitOrHome")

    def commission_board(self) -> Result:
        return self.post("friendTradeCommissionBoard")

    def poster_task_board(self) -> Result:
        return self.post("friendTradePosterTaskBoard")

    def poster_collection(self, query: Optional[Dict[str, Any]] = None) -> Result:
        params = dict(query or {})
        params.setdefault("page", 1)
        return self.post("friendTradePosterCollection", params)

    def poster_gift_status(self, request_uuid: str) -> Result:
        return self.post("friendTradePosterGiftStatus", {"request_uuid": request_uuid})

    def poster_task_claim_status(self, request_uuid: str) -> Result:
        return self.post("friendTradePosterTaskClaimStatus", {"request_uuid": request_uuid})

    def market_list(self, query: Optional[Dict[str, Any]] = None) -> Result:
        return self.post("friendTradeMarketList", query or {})

    def detail(self, target_uid: int) -> Result:
        return self.post("friendTradeDetail", {"target_uid": target_uid})

    def screening_home(self) -> Result:
        return self.post("friendTradeScreeningHome")

    # ------------------------------------------------------------ 海报拍卖 / 寄售
    # 站点拍卖/寄售组件的所有请求都必须带这个头，否则返回「请从电影图鉴页面使用」
    _AUCTION_HEADERS = {"X-Friend-Trade-Auction": "1"}

    def auction_board(self) -> Result:
        return self.post("friendTradePosterAuctionBoard", headers=self._AUCTION_HEADERS)

    def auction_bids(self, auction_id: int, before_id: int = 0, page_size: int = 10) -> Result:
        return self.post("friendTradePosterAuctionBids",
                         {"auction_id": auction_id, "before_id": before_id, "page_size": page_size},
                         headers=self._AUCTION_HEADERS)

    def auction_history(self, before_id: int = 0, page_size: int = 10) -> Result:
        return self.post("friendTradePosterAuctionHistory",
                         {"before_id": before_id, "page_size": page_size},
                         headers=self._AUCTION_HEADERS)

    def consignment_board(self, viewer_uid: int, scope: str = "market",
                          unowned_only: bool = True, page: int = 1, page_size: int = 9,
                          rarity: str = "", sort: str = "price_asc") -> Result:
        """个人寄售/市场看板。scope=market + unowned_only=1 抓市场上未拥有的海报。"""
        return self.post("friendTradePosterConsignmentBoard", {
            "expected_uid": viewer_uid, "scope": scope, "keyword": "",
            "rarity": rarity, "sort": sort,
            "unowned_only": "1" if unowned_only and scope == "market" else "0",
            "page": page, "page_size": page_size,
        }, headers=self._AUCTION_HEADERS)

    def consignment_recent_prices(self, viewer_uid: int) -> Result:
        return self.post("friendTradePosterConsignmentRecentPrices",
                         {"expected_uid": viewer_uid}, headers=self._AUCTION_HEADERS)

    # ------------------------------------------------------------ 写接口
    @staticmethod
    def new_uuid() -> str:
        return str(uuid.uuid4())

    def claim_income(self) -> Result:
        return self.post("friendTradeClaimIncome")

    def work(self, target_uid: int, work_key: str) -> Result:
        return self.post("friendTradeWork", {"target_uid": target_uid, "work_key": work_key})

    def interact(self, target_uid: int, interaction_key: str) -> Result:
        return self.post("friendTradeInteract", {"target_uid": target_uid, "interaction_key": interaction_key})

    def initiative(self, action_key: str) -> Result:
        return self.post("friendTradeSlaveInitiative", {"action_key": action_key})

    def claim_task(self, task_key: str) -> Result:
        return self.post("friendTradeClaimTaskReward", {"task_key": task_key})

    def accept_task(self, task_key: str) -> Result:
        return self.post("friendTradeAcceptTask", {"task_key": task_key})

    def claim_achievement(self, achievement_type: str) -> Result:
        return self.post("friendTradeAchievementClaim", {"achievement_type": achievement_type})

    def claim_level_poster(self, level: int) -> Result:
        return self.post("friendTradeLevelPosterClaim", {"level": level})

    def accept_seed_relation(self, target_uid: int) -> Result:
        return self.post("friendTradeAcceptSeedRelation", {"target_uid": target_uid})

    def protect(self, target_uid: int, hours: int = 24) -> Result:
        return self.post("friendTradeProtect",
                         {"target_uid": target_uid, "expected_protection_hours": hours})

    def redeem(self, expected_price: Any) -> Result:
        return self.post("friendTradeRedeem", {"expected_price": expected_price})

    def release(self, target_uid: int) -> Result:
        return self.post("friendTradeRelease", {"target_uid": target_uid})

    def buy_capacity_slot(self) -> Result:
        return self.post("friendTradeBuyCapacitySlot")

    def commission_start(self, offer_id: int, relationship_ids: Any) -> Result:
        if isinstance(relationship_ids, (list, tuple)):
            relationship_ids = json.dumps(list(relationship_ids), separators=(",", ":"))
        return self.post("friendTradeCommissionStart",
                         {"offer_id": offer_id, "relationship_ids": relationship_ids})

    def commission_settle(self, run_id: int) -> Result:
        return self.post("friendTradeCommissionSettle", {"run_id": run_id})

    def commission_cancel(self, run_id: int) -> Result:
        return self.post("friendTradeCommissionCancel", {"run_id": run_id})

    def commission_refresh(self, pending: Any) -> Result:
        return self.post("friendTradeCommissionRefresh", pending or {},
                         headers={"X-Friend-Trade-Commission-Refresh": "1"})

    def poster_task_claim(self, task_key: str) -> Result:
        return self.post("friendTradePosterTaskClaim",
                         {"task_key": task_key, "request_uuid": self.new_uuid()})

    def album_redeem(self) -> Result:
        return self.post("friendTradeAlbumRedeem")

    def poster_gift(self, recipient_uid: int, poster_id: Any, quantity: int = 1) -> Result:
        return self.post("friendTradePosterGift", {
            "recipient_uid": recipient_uid,
            "poster_id": poster_id,
            "quantity": quantity,
            "request_uuid": self.new_uuid(),
        })

    def trade(self, action: str, target_uid: int, expected_price: Any) -> Result:
        """买卖统一入口，action 必须是 buy/snatch/reclaim 之一。"""
        if action not in {"friendTradeBuy", "friendTradeSnatch", "friendTradeReclaim"}:
            return Result.fail("未知交易动作")
        return self.post(action, {"target_uid": target_uid, "expected_price": str(expected_price)})

    def screening_start(self, game_key: str, assist_uid: int = 0) -> Result:
        params: Dict[str, Any] = {"game_key": game_key}
        if assist_uid:
            params["assist_uid"] = assist_uid
        return self.post("friendTradeScreeningStart", params)

    def screening_submit(self, challenge_token: str, events_json: str, answer_json: str) -> Result:
        return self.post("friendTradeScreeningSubmit", {
            "challenge_token": challenge_token,
            "events_json": events_json,
            "answer_json": answer_json,
        })

    def auction_bid(self, auction_id: int, amount: int, rule_version: str,
                    viewer_uid: int) -> Result:
        """拍品出价：暗拍最终价，带 UUID 防重放 + 规则版本 + 账号校验。"""
        return self.post("friendTradePosterAuctionBid", {
            "auction_id": auction_id,
            "amount": int(amount),
            "request_uuid": self.new_uuid(),
            "expected_rule_version": rule_version,
            "expected_uid": viewer_uid,
        }, headers=self._AUCTION_HEADERS)

    def dutch_buy(self, auction_id: int, max_price: Any, rule_version: str,
                  viewer_uid: int) -> Result:
        """荷兰式降价买：按当前价成交。"""
        return self.post("friendTradePosterDutchBuy", {
            "expected_uid": viewer_uid,
            "auction_id": auction_id,
            "max_price": str(max_price),
            "request_uuid": self.new_uuid(),
            "expected_rule_version": rule_version,
        }, headers=self._AUCTION_HEADERS)

    def consignment_create(self, viewer_uid: int, poster_id: int, starting_price: int,
                           duration_hours: int, fee_bps: int = 0, listing_fee_bps: int = 0,
                           mode: str = "auction", floor_price: int = 0,
                           drop_interval: int = 0, drop_amount: int = 0) -> Result:
        """个人海报寄售上架。"""
        payload: Dict[str, Any] = {
            "expected_uid": viewer_uid,
            "poster_id": poster_id,
            "starting_price": starting_price,
            "duration_hours": duration_hours,
            "expected_seller_fee_bps": fee_bps,
            "expected_listing_fee_bps": listing_fee_bps,
            "request_uuid": self.new_uuid(),
        }
        if mode == "dutch":
            payload["mode"] = "dutch"
            payload["floor_price"] = floor_price
            payload["drop_interval_seconds"] = drop_interval
            payload["drop_amount"] = drop_amount
        return self.post("friendTradePosterConsignmentCreate", payload,
                         headers=self._AUCTION_HEADERS)

    def consignment_cancel(self, viewer_uid: int, auction_id: int) -> Result:
        return self.post("friendTradePosterConsignmentCancel",
                         {"expected_uid": viewer_uid, "auction_id": auction_id},
                         headers=self._AUCTION_HEADERS)


def probe(cookie: str, site: str = core.DEFAULT_SITE, timeout: int = 20) -> Result:
    """轻量连通性检测：读首页，判断 cookie 是否有效。"""
    client = PandaClient(cookie, site, timeout=timeout, interval=0)
    result = client.home()
    if not result.ok:
        return result
    data = result.data if isinstance(result.data, dict) else {}
    me = data.get("me") or {}
    uid = me.get("uid") if isinstance(me, dict) else None
    name = me.get("username") if isinstance(me, dict) else ""
    if not uid:
        return Result.fail("登录态异常：首页没有用户信息，请检查 Cookie")
    return Result.success({"uid": uid, "username": name}, f"连接成功：{name or uid}")
