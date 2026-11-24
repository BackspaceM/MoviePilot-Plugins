"""熊猫PT 好友买卖：纯规则与解析层。

本模块不依赖 MoviePilot 宿主、不发网络请求，只做三件事：
1. 解析用户配置（预算、开关、策略）；
2. 把站点返回的 JSON 解析成统一的视图对象；
3. 按规则产出「动作计划」（Action 列表），由主类决定试运行还是真的执行。

所有涉及魔力消耗 / 不可逆交易的动作都必须由对应开关显式打开，
默认只做零风险动作（领收益、领任务奖励、结算已完成委托）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------- 常量

DEFAULT_SITE = "https://pandapt.net"

# 互动选项：站点 interactionOptions 的固定 key 与魔力成本（实测，2026-11）
INTERACTIONS: Dict[str, Dict[str, Any]] = {
    "praise": {"label": "夸夸", "cost": 0, "exp": 3},
    "feed": {"label": "投喂", "cost": 100, "exp": 6},
    "pat": {"label": "摸头", "cost": 0, "exp": 2},
    "whisper": {"label": "悄悄话", "cost": 0, "exp": 5},
    "reward": {"label": "小奖励", "cost": 300, "exp": 8},
    "deep_communication": {"label": "深入交流", "cost": 1000, "exp": 12},
}
FREE_INTERACTIONS = ("whisper", "praise", "pat")  # 免费项按关系经验排序

# 工作偏好：站点 work_options 的固定 key。_repo 只负责挑选，不修改站点数据。
WORK_PROFILES: Dict[str, Tuple[str, ...]] = {
    "income": ("accounting", "social", "greeting", "errand", "guard", "tidy", "clean", "private_task", "hair_massage", "close_care", "chat", "rest"),
    "relation": ("private_task", "hair_massage", "chat", "close_care", "close_attendant", "whisper", "greeting", "tidy", "clean", "errand", "guard", "rest"),
    "rest": ("rest", "chat", "close_care"),
    "attribute": (),  # 动态按佣人最高属性匹配
}
# 属性 -> 优先考虑的工作（与 work_options.attributes 对应）
ATTRIBUTE_WORKS: Dict[str, Tuple[str, ...]] = {
    "diligence": ("tidy", "clean", "greeting"),
    "wisdom": ("accounting", "social", "errand", "tidy"),
    "physique": ("guard", "errand", "hair_massage"),
    "charm": ("greeting", "chat", "social", "private_task"),
    "virtue": ("close_care", "guard", "chat"),
}

# 主动互动（我作为佣人）偏好
INITIATIVE_ORDER = ("pillow_talk", "nuzzle", "warm_bed", "extra_shift")

SETTLE_STATUSES = frozenset({"ready", "completed", "finished", "settled", "ended"})


# ---------------------------------------------------------------- 数据结构

@dataclass
class Action:
    """一个待执行动作。execute 层只认 action + params。"""
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    label: str = ""
    risk: str = "safe"  # safe / cost / irreversible

    def as_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "params": self.params, "label": self.label, "risk": self.risk}


@dataclass
class Config:
    """从插件配置字典规整出来的规则。"""
    cookie: str = ""
    site: str = DEFAULT_SITE
    enabled: bool = False
    dry_run: bool = True
    notify: bool = False
    timeout: int = 30
    request_interval: float = 1.0
    max_actions: int = 60

    # 任务开关
    do_income: bool = True
    do_work: bool = False
    do_interact: bool = False
    do_initiative: bool = False
    do_tasks: bool = True
    do_achievements: bool = False
    do_level_posters: bool = False
    do_commission: bool = False
    do_settle: bool = True
    do_posters: bool = False
    do_protect: bool = False
    do_buy: bool = False
    do_release: bool = False

    # 策略与预算
    work_policy: str = "income"
    interact_mode: str = "free"
    interact_budget: int = 300
    protect_minutes: int = 120
    protect_budget: int = 10000
    buy_max_price: int = 0
    buy_min_overall: int = 0
    release_temporary: bool = False
    commission_preference: str = "experience"
    reserved_uids: Tuple[int, ...] = ()
    poster_gift_recipients: Tuple[int, ...] = ()
    poster_auto_redeem: bool = False

    @property
    def interact_cost_limit(self) -> int:
        """本次运行可用的单动作互动预算。"""
        if self.interact_mode == "free":
            return 0
        if self.interact_mode == "unlimited":
            return 10 ** 9
        return max(0, self.interact_budget)


# ---------------------------------------------------------------- 解析

def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip().rstrip(".")))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开", "启用"}


def parse_int_list(value: Any) -> Tuple[int, ...]:
    """解析逗号/空格/换行分隔的 UID 列表，非法项直接丢弃。"""
    if isinstance(value, (list, tuple, set)):
        parts = [str(x) for x in value]
    else:
        parts = re.split(r"[,，;；\s]+", str(value or ""))
    out: List[int] = []
    for part in parts:
        num = _to_int(part, -1)
        if num > 0 and num not in out:
            out.append(num)
    return tuple(out)


def normalize_site(value: Any) -> str:
    site = str(value or DEFAULT_SITE).strip().rstrip("/")
    return site if site.startswith("http") else DEFAULT_SITE


def parse_config(raw: Dict[str, Any]) -> Config:
    """把宿主传入的配置字典规整为 Config；缺省值都取安全侧。"""
    raw = dict(raw or {})
    work_policy = str(raw.get("work_policy") or "income").strip()
    if work_policy not in WORK_PROFILES:
        work_policy = "income"
    interact_mode = str(raw.get("interact_mode") or "free").strip()
    if interact_mode not in {"free", "limit", "unlimited", "off"}:
        interact_mode = "free"
    commission_preference = str(raw.get("commission_preference") or "experience").strip()
    if commission_preference not in {"experience", "magic", "posters"}:
        commission_preference = "experience"

    return Config(
        cookie=str(raw.get("cookie") or "").strip(),
        site=normalize_site(raw.get("site")),
        enabled=parse_bool(raw.get("enabled")),
        dry_run=parse_bool(raw.get("dry_run"), True),
        notify=parse_bool(raw.get("notify")),
        timeout=max(5, min(120, _to_int(raw.get("timeout"), 30))),
        request_interval=max(0.0, min(10.0, _to_float(raw.get("request_interval"), 1.0))),
        max_actions=max(1, min(500, _to_int(raw.get("max_actions"), 60))),
        do_income=parse_bool(raw.get("do_income"), True),
        do_work=parse_bool(raw.get("do_work")),
        do_interact=parse_bool(raw.get("do_interact")),
        do_initiative=parse_bool(raw.get("do_initiative")),
        do_tasks=parse_bool(raw.get("do_tasks"), True),
        do_achievements=parse_bool(raw.get("do_achievements")),
        do_level_posters=parse_bool(raw.get("do_level_posters")),
        do_commission=parse_bool(raw.get("do_commission")),
        do_settle=parse_bool(raw.get("do_settle"), True),
        do_posters=parse_bool(raw.get("do_posters")),
        do_protect=parse_bool(raw.get("do_protect")),
        do_buy=parse_bool(raw.get("do_buy")),
        do_release=parse_bool(raw.get("do_release")),
        work_policy=work_policy,
        interact_mode=interact_mode,
        interact_budget=max(0, _to_int(raw.get("interact_budget"), 300)),
        protect_minutes=max(0, min(10080, _to_int(raw.get("protect_minutes"), 120))),
        protect_budget=max(0, _to_int(raw.get("protect_budget"), 10000)),
        buy_max_price=max(0, _to_int(raw.get("buy_max_price"))),
        buy_min_overall=max(0, min(100, _to_int(raw.get("buy_min_overall")))),
        release_temporary=parse_bool(raw.get("release_temporary")),
        commission_preference=commission_preference,
        reserved_uids=parse_int_list(raw.get("reserved_uids")),
        poster_gift_recipients=parse_int_list(raw.get("poster_gift_recipients")),
        poster_auto_redeem=parse_bool(raw.get("poster_auto_redeem")),
    )


# ---------------------------------------------------------------- 视图

@dataclass
class Servant:
    uid: int
    name: str
    overall: float
    attributes: Dict[str, int]
    talent: str
    rarity: str
    can_work: bool
    can_interact: bool
    current_work: str
    mood: int
    work_options: Dict[str, Any]
    protected: bool = False
    protect_remaining: int = 0
    protect_price: float = 0.0
    can_protect: bool = False
    is_temporary: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def top_attribute(self) -> str:
        if not self.attributes:
            return ""
        return max(self.attributes, key=lambda k: self.attributes[k])


def _attributes_of(trait: Dict[str, Any]) -> Dict[str, int]:
    attrs = (trait or {}).get("attributes") or {}
    out: Dict[str, int] = {}
    for key in ("virtue", "wisdom", "physique", "charm", "diligence"):
        value = attrs.get(key)
        if value is not None:
            out[key] = _to_int(value)
    return out


def parse_servants(home: Dict[str, Any]) -> List[Servant]:
    """从 friendTradeInitOrHome.data 解析佣人列表。"""
    rows = (home or {}).get("my_assets") or []
    out: List[Servant] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        uid = _to_int(item.get("slave_uid"))
        if uid <= 0:
            continue
        trait = item.get("trait_summary") or {}
        cult = item.get("cultivation_summary") or {}
        prot = item.get("protection_summary") or {}
        works_raw = cult.get("available_works") or cult.get("work_options") or {}
        works = {str(k): v for k, v in works_raw.items() if isinstance(v, dict)}
        attrs = _attributes_of(trait)
        out.append(Servant(
            uid=uid,
            name=str(item.get("username") or uid),
            overall=round(sum(attrs.values()) / max(1, len(attrs)), 1),
            attributes=attrs,
            talent=str(trait.get("talent_key") or ""),
            rarity=str(trait.get("rarity") or ""),
            can_work=bool(cult.get("can_work_today")),
            can_interact=bool(cult.get("can_interact_today")),
            current_work=str(cult.get("current_work") or ""),
            mood=_to_int(cult.get("mood")),
            work_options=works,
            protected=bool(prot.get("is_protected")),
            protect_remaining=_to_int(prot.get("remaining_minutes")),
            protect_price=_to_float(prot.get("next_protection_price")),
            can_protect=bool(prot.get("can_buy_protection")),
            is_temporary=bool(item.get("is_temporary")),
            raw=item,
        ))
    return out


def claimable_income(home: Dict[str, Any]) -> float:
    return _to_float((home or {}).get("claimable_income"), 0.0)


def claimable_tasks(home: Dict[str, Any]) -> List[str]:
    """成长任务里已可领取的 task_key。status 以站点为准，另兼容 progress>=target。"""
    out: List[str] = []
    for task in (home or {}).get("task_status") or []:
        if not isinstance(task, dict):
            continue
        key = str(task.get("task_key") or task.get("definition_key") or "")
        if not key:
            continue
        status = str(task.get("status") or "")
        if status in {"claimable", "completed", "finished", "ready"}:
            out.append(key)
            continue
        try:
            if float(task.get("progress", 0)) >= float(task.get("target", 1)) > 0 and not task.get("claimed_at"):
                out.append(key)
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def claimable_posters(board: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for task in (board or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        key = str(task.get("key") or "")
        if key and str(task.get("status") or "") == "claimable":
            out.append(key)
    return list(dict.fromkeys(out))


def claimable_achievements(home: Dict[str, Any]) -> List[str]:
    """从首页/海报数据里找可领成就（结构随站点变化，做宽松兼容）。"""
    out: List[str] = []
    candidates: List[Any] = []
    for key in ("achievements", "achievement_list", "pending_achievements"):
        value = (home or {}).get(key)
        if isinstance(value, list):
            candidates.extend(value)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("achievement_type") or "")
        status = str(item.get("status") or "")
        if kind and (status in {"claimable", "completed", "ready"} or item.get("can_claim")):
            out.append(kind)
    return list(dict.fromkeys(out))


def claimable_level_posters(home: Dict[str, Any]) -> List[int]:
    """等级海报：从 home.business_summary / levelPosterRewards 里找可领等级。"""
    levels: List[int] = []
    summary = (home or {}).get("business_summary") or {}
    rewards = summary.get("level_poster_rewards") or (home or {}).get("level_poster_rewards") or {}
    if isinstance(rewards, dict):
        pending = _to_int(rewards.get("pending_count"))
        if pending > 0:
            for item in rewards.get("pending_levels") or []:
                level = _to_int(item if isinstance(item, (int, str)) else item.get("level"))
                if level > 0:
                    levels.append(level)
    return list(dict.fromkeys(levels))


# ---------------------------------------------------------------- 决策：工作 / 互动

def choose_work(servant: Servant, policy: str) -> str:
    """按策略从可用工作里挑一个 key；没有可用项返回空串。"""
    available = set(servant.work_options.keys())
    if not available:
        return ""
    if policy == "attribute" and servant.top_attribute:
        for key in ATTRIBUTE_WORKS.get(servant.top_attribute, ()):
            if key in available:
                return key
    for key in WORK_PROFILES.get(policy, WORK_PROFILES["income"]):
        if key in available:
            return key
    return ""


def choose_interaction(cfg: Config, servant: Optional[Servant] = None) -> str:
    """按互动模式挑 key。免费模式零消耗；限额模式在预算内选经验最高的付费项。"""
    if cfg.interact_mode == "off":
        return ""
    if cfg.interact_mode == "free":
        return FREE_INTERACTIONS[0] if FREE_INTERACTIONS else ""
    limit = cfg.interact_cost_limit
    best_key = ""
    best_exp = -1
    for key, meta in INTERACTIONS.items():
        if meta["cost"] > limit:
            continue
        if meta["exp"] > best_exp or (meta["exp"] == best_exp and meta["cost"] < INTERACTIONS.get(best_key, {}).get("cost", 10 ** 9)):
            best_key, best_exp = key, meta["exp"]
    return best_key


def choose_initiative(home: Dict[str, Any]) -> str:
    """我作为佣人的主动互动 key：优先站点建议，其次按固定偏好。"""
    owner = (home or {}).get("my_owner") or {}
    summary = owner.get("initiative_summary") or {}
    suggested = str(summary.get("action_key") or "")
    if summary.get("can_initiate_today") and suggested:
        return suggested
    options = owner.get("initiative_options") or []
    keys = [str(x.get("key") or "") for x in options if isinstance(x, dict) and x.get("key")]
    for key in INITIATIVE_ORDER:
        if key in keys:
            return key
    return keys[0] if keys else ""


# ---------------------------------------------------------------- 决策：事务所

def parse_commission(board: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """返回 (可派委托, 可派成员, 可结算任务, 自动派遣状态)。"""
    data = board or {}
    offers = [x for x in (data.get("offers") or []) if isinstance(x, dict)]
    members = [x for x in (data.get("eligible_members") or []) if isinstance(x, dict)]
    running = [x for x in (data.get("running") or []) if isinstance(x, dict)]
    settle = [x for x in running if str(x.get("status") or "").lower() in SETTLE_STATUSES]
    auto = data.get("auto_dispatch") or {}
    return offers, members, settle, auto if isinstance(auto, dict) else {}


def _member_score(member: Dict[str, Any], offer: Dict[str, Any]) -> float:
    """按委托 focus_targets 给成员打分；没有任何属性信息时给中位数。"""
    attrs = _attributes_of(member.get("trait_summary") or {})
    focus = ((offer.get("offer_snapshot_text") or {}).get("focus_targets") or offer.get("focus_targets") or {})
    if not attrs or not focus:
        return 50.0
    total_w = 0.0
    score = 0.0
    for key, weight in focus.items():
        try:
            w = float(weight)
        except (TypeError, ValueError):
            continue
        value = attrs.get(str(key))
        if value is None:
            continue
        # focus_targets 常为 50~92 的目标值，越接近/超过越好，上限按 120 折算
        score += min(120.0, float(value)) * w
        total_w += w
    return score / total_w if total_w else 50.0


def pick_commission_team(offer: Dict[str, Any], members: List[Dict[str, Any]],
                         reserved: Tuple[int, ...], team_size_limit: int = 3) -> List[int]:
    """为一个委托挑选 relationship_id；已保留的佣人不派。"""
    reserved_set = set(reserved)
    snap = offer.get("offer_snapshot_text") or {}
    need = max(1, min(team_size_limit, _to_int(snap.get("recommended_team_size"), 1)))
    candidates = []
    for m in members:
        uid = _to_int(m.get("slave_uid"))
        rid = _to_int(m.get("relationship_id"))
        if uid <= 0 or rid <= 0 or uid in reserved_set:
            continue
        candidates.append((_member_score(m, offer), rid, uid))
    candidates.sort(key=lambda x: (-x[0], x[2]))
    return [rid for _, rid, _ in candidates[:need]]


def plan_commission(cfg: Config, board: Dict[str, Any], running_count: int = 0) -> List[Action]:
    """事务所计划：先结算已完成的，再在有空槽时派出新委托。"""
    offers, members, settle, auto = parse_commission(board)
    actions: List[Action] = []
    for run in settle:
        run_id = _to_int(run.get("id"))
        name = str((run.get("offer") or {}).get("name") or "委托")
        if run_id > 0:
            actions.append(Action("friendTradeCommissionSettle", {"run_id": run_id},
                                  f"结算 {name}", "safe"))

    if not cfg.do_commission:
        return actions
    # 站点原生自动派遣已开启时，不重复派出，避免两边抢同一批佣人
    if auto.get("enabled"):
        return actions
    parallel = _to_int(board.get("parallel_limit"), 1)
    slots = max(0, parallel - max(0, running_count))
    if slots <= 0:
        return actions
    for offer in offers[:slots]:
        oid = _to_int(offer.get("id"))
        name = str((offer.get("offer_snapshot_text") or {}).get("name") or offer.get("template_key") or "委托")
        team = pick_commission_team(offer, members, cfg.reserved_uids)
        if oid > 0 and team:
            actions.append(Action(
                "friendTradeCommissionStart",
                {"offer_id": oid, "relationship_ids": __import__("json").dumps(team, separators=(",", ":"))},
                f"派出 {name}（{len(team)} 人）", "cost"))
    return actions


# ---------------------------------------------------------------- 决策：主计划

def build_plan(cfg: Config, home: Dict[str, Any], board: Dict[str, Any],
               poster_board: Dict[str, Any]) -> List[Action]:
    """汇总各玩法计划。顺序：零风险领取 → 工作/互动 → 派遣 → 海报 → 保护/交易。"""
    actions: List[Action] = []

    if cfg.do_income and claimable_income(home) > 0:
        actions.append(Action("friendTradeClaimIncome", {}, "领取挂机收益", "safe"))

    if cfg.do_tasks:
        for key in claimable_tasks(home):
            actions.append(Action("friendTradeClaimTaskReward", {"task_key": key}, f"领成长任务 {key}", "safe"))

    if cfg.do_level_posters:
        for level in claimable_level_posters(home):
            actions.append(Action("friendTradeLevelPosterClaim", {"level": level}, f"领等级海报 Lv.{level}", "safe"))

    if cfg.do_achievements:
        for kind in claimable_achievements(home):
            actions.append(Action("friendTradeAchievementClaim", {"achievement_type": kind}, f"领成就 {kind}", "safe"))

    servants = parse_servants(home)
    reserved = set(cfg.reserved_uids)

    if cfg.do_work:
        for s in servants:
            if not s.can_work or s.uid in reserved:
                continue
            key = choose_work(s, cfg.work_policy)
            if key and key != s.current_work:
                actions.append(Action("friendTradeWork",
                                      {"target_uid": s.uid, "work_key": key},
                                      f"{s.name} 工作→{(s.work_options.get(key) or {}).get('label', key)}", "safe"))

    if cfg.do_interact and cfg.interact_mode != "off":
        key = choose_interaction(cfg)
        if key:
            for s in servants:
                if not s.can_interact or s.uid in reserved:
                    continue
                meta = INTERACTIONS[key]
                actions.append(Action("friendTradeInteract",
                                      {"target_uid": s.uid, "interaction_key": key},
                                      f"与 {s.name} {meta['label']}（{meta['cost']} 魔力）",
                                      "safe" if meta["cost"] == 0 else "cost"))

    if cfg.do_initiative:
        key = choose_initiative(home)
        if key:
            actions.append(Action("friendTradeSlaveInitiative", {"action_key": key}, f"主动互动 {key}", "safe"))

    if cfg.do_commission or cfg.do_settle:
        running = (board or {}).get("running") or []
        actions.extend(plan_commission(cfg, board, len(running)))

    if cfg.do_posters:
        for key in claimable_posters(poster_board):
            actions.append(Action("friendTradePosterTaskClaim", {"task_key": key}, f"领海报任务 {key}", "safe"))
        # 赠送与兑换需要额外确认，交给 execute 层查状态后再决定，这里只标记意图
        if cfg.poster_auto_redeem:
            actions.append(Action("_poster_redeem", {}, "检查典藏册兑换", "safe"))
        if cfg.poster_gift_recipients:
            actions.append(Action("_poster_gift", {}, "检查海报赠送", "cost"))

    if cfg.do_protect:
        for s in servants:
            if (s.can_protect and not s.protected and s.protect_remaining <= cfg.protect_minutes
                    and s.protect_price <= cfg.protect_budget and s.uid not in reserved):
                actions.append(Action("friendTradeProtect",
                                      {"target_uid": s.uid, "expected_protection_hours": 24},
                                      f"保护 {s.name}（{s.protect_price} 魔力）", "cost"))

    if cfg.do_release and cfg.release_temporary:
        for s in servants:
            if s.is_temporary and s.uid not in reserved:
                actions.append(Action("friendTradeRelease", {"target_uid": s.uid},
                                      f"释放临时佣人 {s.name}", "irreversible"))

    # 买人走单独流程：必须先查详情拿实时价格，build_plan 不产出买入动作
    return actions[:cfg.max_actions]


def merchant_candidates(market: Dict[str, Any], cfg: Config, detail_map: Dict[int, Dict[str, Any]]) -> List[Action]:
    """买人计划：只有查过详情、价格在预算内、属性达标才产出动作。"""
    actions: List[Action] = []
    if not cfg.do_buy or cfg.buy_max_price <= 0:
        return actions
    rows = (market or {}).get("items") or (market or {}).get("list") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = _to_int(row.get("uid"))
        if uid <= 0 or not detail_map.get(uid):
            continue
        detail = detail_map[uid]
        ctx = detail.get("trade_context") or {}
        if not ctx.get("can_buy"):
            continue
        price = _to_float(ctx.get("total_price") or ctx.get("price"))
        if price <= 0 or price > cfg.buy_max_price:
            continue
        trait = detail.get("trait_summary") or {}
        attrs = _attributes_of(trait)
        overall = sum(attrs.values()) / max(1, len(attrs))
        if cfg.buy_min_overall and overall < cfg.buy_min_overall:
            continue
        act = str(ctx.get("action") or "buy")
        action_map = {"buy": "friendTradeBuy", "snatch": "friendTradeSnatch", "reclaim": "friendTradeReclaim"}
        action = action_map.get(act)
        if action:
            actions.append(Action(action,
                                  {"target_uid": uid, "expected_price": str(ctx.get("total_price") or ctx.get("price"))},
                                  f"{ctx.get('name') or detail.get('username') or uid} {price} 魔力", "irreversible"))
    return actions[:cfg.max_actions]


def summarize(actions: List[Action]) -> Dict[str, int]:
    """按风险等级汇总，供页面和消息展示。"""
    out = {"total": len(actions), "safe": 0, "cost": 0, "irreversible": 0}
    for action in actions:
        risk = action.risk if action.risk in out else "safe"
        out[risk] += 1
    return out
