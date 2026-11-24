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
from typing import Any, Dict, Optional

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
                 interval: float = 1.0, opener: Any = None) -> None:
        self.cookie = str(cookie or "").strip()
        self.site = core.normalize_site(site)
        self.timeout = max(5, min(120, int(timeout)))
        self.interval = max(0.0, min(10.0, float(interval)))
        self._opener = opener  # 便于测试注入
        self._last_request = 0.0

    @property
    def ajax_url(self) -> str:
        return f"{self.site}/ajax.php"

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
        # cookie 只放进请求头，不写日志
        headers["Cookie"] = self.cookie

        self._throttle()
        self._last_request = time.monotonic()
        try:
            req = urllib.request.Request(self.ajax_url, data=body, headers=headers, method="POST")
            opener = self._opener or urllib.request.build_opener()
            with opener.open(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            return Result.fail(f"HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001 - 网络异常统一兜底
            return Result.fail(f"{type(exc).__name__}: {exc}")

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
