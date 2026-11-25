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
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------- 常量

DEFAULT_SITE = "https://pandapt.net"

# 定时执行周期：默认凌晨 3/9/18/23 点整（5 位 crontab：分 时 日 月 周）
DEFAULT_CRON = "0 3,9,18,23 * * *"
_CRON_TOKEN = re.compile(r"^[0-9*,/?\-]+$")


def normalize_cron(expression: Any) -> str:
    """归一化 crontab：? 视作 *，压缩空白。"""
    return " ".join(str(expression or "").replace("?", "*").split())


def validate_cron(expression: Any) -> tuple[bool, str]:
    """校验 5 位 crontab 的段数、字符集与数值范围；合法时返回归一化结果。"""
    text = normalize_cron(expression)
    if not text:
        return False, "执行周期为空"
    fields = text.split(" ")
    if len(fields) != 5:
        return False, "必须是 5 位 crontab：分 时 日 月 周"
    limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    for field, (low, high) in zip(fields, limits):
        if not _CRON_TOKEN.fullmatch(field):
            return False, f"非法字符：{field}"
        for number in re.findall(r"\d+", field):
            if not low <= int(number) <= high:
                return False, f"字段 {field} 超出范围 {low}-{high}"
    return True, text

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
    cron: str = DEFAULT_CRON  # 5 位 crontab 定时执行周期

    # 代理：http/https/socks5，格式 [user:pass@]host:port；空则直连
    proxy_url: str = ""
    proxy_bypass: bool = False  # 代理失败时是否自动降级直连

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

    # 海报拍卖
    do_auction: bool = False          # 拍卖监控总开关
    auction_auto_bid: bool = False    # 是否自动出价（否则只监控报警）
    auction_budget: int = 0           # 单个拍品最高最终出价（0=不出价）
    auction_total_budget: int = 0     # 本轮累计最多占用魔力（0=不限）
    auction_snipe_seconds: int = 60   # 进入最后 N 秒才开始抢拍（0=立即）
    auction_rarities: Tuple[str, ...] = ()  # 只抢这些稀有度，空=不限
    auction_max_bids_per_lot: int = 3       # 单个拍品最多自动加价次数
    auction_min_gap: int = 60               # 同一拍品两次出价最小间隔（秒）
    auction_require_no_leader: bool = False  # 只抢无人领先(起拍价)的拍品
    auction_aggressive_steps: int = 0       # 额外加价档数（0=按站点最低有效价，越大越激进）
    auction_poll_seconds: int = 15          # 监控轮询间隔（秒），要抢倒计时建议 ≤20

    # 海报寄售场（光影拍卖行之外的个人寄售）
    do_consignment: bool = False              # 寄售场监控总开关
    consignment_auto_bid: bool = False        # 自动抢购（dutch 直接买 / open 竞价）
    consignment_budget: int = 0               # 单品价格上限（0=不买）
    consignment_unowned_only: bool = True     # 只抢未拥有的海报（默认开）
    consignment_wanted_poster_ids: Tuple[int, ...] = ()  # 想要的指定海报 ID，留空按稀有度
    consignment_rarities: Tuple[str, ...] = ()           # 想要的档位，空=不限
    consignment_modes: Tuple[str, ...] = ("open", "dutch")  # 参与的寄售模式

    # 海报奖励领取档位（囤魔力领完全图鉴）
    poster_claim_rarities: Tuple[str, ...] = ()  # 只领这些档位的任务奖励，空=全部可领

    # ---- 合并后的「海报低价抢购」：拍卖行 + 寄售场归为一个项目 ----
    do_poster_snipe: bool = False            # 总开关；缺省兼容旧 do_auction/do_consignment
    poster_snipe_max_price: int = 0          # 全局单件价格上限(魔力)，0=不按全局抢
    poster_snipe_rarities: Tuple[str, ...] = ()  # 稀有度白名单，空=不限
    poster_snipe_unowned_only: bool = True   # 只抢未拥有
    poster_snipe_poll_seconds: int = 15      # 统一轮询间隔
    poster_claim_tasks: Tuple[str, ...] = ()  # 领奖档位多选（站点 task_key），空=全部可领
    poster_snipe_rounds: int = 3             # 抢购轮次：每轮扫描最多连续出价几笔
    poster_snipe_ranges: Dict[str, Tuple[int, int]] = field(default_factory=dict)  # 每稀有度价格区间

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


def parse_str_list(value: Any) -> Tuple[str, ...]:
    """解析逗号/空格分隔的字符串或列表，保序去重、不做白名单过滤。"""
    if isinstance(value, (list, tuple, set)):
        parts = [str(x).strip() for x in value]
    else:
        parts = [x.strip() for x in re.split(r"[,，;；\s]+", str(value or ""))]
    return tuple(x for x in dict.fromkeys(parts) if x)


def parse_choices(value: Any, allowed: Tuple[str, ...]) -> Tuple[str, ...]:
    """解析逗号/空格分隔的选项，只保留白名单内的值。"""
    if isinstance(value, (list, tuple, set)):
        parts = [str(x).strip().lower() for x in value]
    else:
        parts = [x.strip().lower() for x in re.split(r"[,，;；\s]+", str(value or ""))]
    return tuple(x for x in dict.fromkeys(parts) if x in allowed)


# 每个稀有度的独立价格区间：snipe_range_<rarity> = "最低-最高"，如 excellent = "5000-7000"
SNIPE_RARITY_KEYS: Tuple[str, ...] = ("normal", "excellent", "rare", "precious", "legendary")


def parse_snipe_ranges(raw: Dict[str, Any]) -> Dict[str, Tuple[int, int]]:
    """解析「每个稀有度一个价格区间」配置。

    支持 "5000-7000" / "5000~7000" / 单个 "5000"（视为 0-5000）与
    (low, high) 列表/元组。留空或非法值忽略。最低/最高写反会自动纠正。
    """
    result: Dict[str, Tuple[int, int]] = {}
    for key in SNIPE_RARITY_KEYS:
        value = raw.get(f"snipe_range_{key}")
        low = high = 0
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            low, high = _to_int(value[0]), _to_int(value[1])
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            high = _to_int(value)
        elif value is not None:
            nums = re.findall(r"\d+", str(value))
            if len(nums) >= 2:
                low, high = int(nums[0]), int(nums[1])
            elif len(nums) == 1:
                high = int(nums[0])
        if high <= 0:
            continue
        low = max(0, low)
        if low > high:
            low, high = high, low
        result[key] = (low, high)
    return result


def normalize_proxy(value: Any) -> str:
    """把代理 URL 规整为 urllib 可用形式；只接受常见协议，防止乱填。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    scheme = text.split("://", 1)[0].lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        return ""
    return text


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
    cron_ok, cron = validate_cron(raw.get("cron") or DEFAULT_CRON)
    if not cron_ok:
        cron = DEFAULT_CRON

    return Config(
        cron=cron,
        cookie=str(raw.get("cookie") or "").strip(),
        site=normalize_site(raw.get("site")),
        enabled=parse_bool(raw.get("enabled")),
        dry_run=parse_bool(raw.get("dry_run"), True),
        notify=parse_bool(raw.get("notify")),
        timeout=max(5, min(120, _to_int(raw.get("timeout"), 30))),
        request_interval=max(0.0, min(10.0, _to_float(raw.get("request_interval"), 1.0))),
        max_actions=max(1, min(500, _to_int(raw.get("max_actions"), 60))),
        proxy_url=normalize_proxy(raw.get("proxy_url")),
        proxy_bypass=parse_bool(raw.get("proxy_bypass")),
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
        do_auction=parse_bool(raw.get("do_auction")),
        auction_auto_bid=parse_bool(raw.get("auction_auto_bid")),
        auction_budget=max(0, _to_int(raw.get("auction_budget"))),
        auction_total_budget=max(0, _to_int(raw.get("auction_total_budget"))),
        auction_snipe_seconds=max(0, min(3600, _to_int(raw.get("auction_snipe_seconds"), 60))),
        auction_rarities=parse_choices(raw.get("auction_rarities"),
                                       ("normal", "excellent", "rare", "precious", "legendary")),
        auction_max_bids_per_lot=max(1, min(50, _to_int(raw.get("auction_max_bids_per_lot"), 3))),
        auction_min_gap=max(0, min(3600, _to_int(raw.get("auction_min_gap"), 60))),
        auction_require_no_leader=parse_bool(raw.get("auction_require_no_leader")),
        auction_aggressive_steps=max(0, min(20, _to_int(raw.get("auction_aggressive_steps")))),
        auction_poll_seconds=max(3, min(300, _to_int(raw.get("auction_poll_seconds"), 15))),
        do_consignment=parse_bool(raw.get("do_consignment")),
        consignment_auto_bid=parse_bool(raw.get("consignment_auto_bid")),
        consignment_budget=max(0, _to_int(raw.get("consignment_budget"))),
        consignment_unowned_only=parse_bool(raw.get("consignment_unowned_only"), True),
        consignment_wanted_poster_ids=parse_int_list(raw.get("consignment_wanted_poster_ids")),
        consignment_rarities=parse_choices(raw.get("consignment_rarities"),
                                           ("normal", "excellent", "rare", "precious", "legendary")),
        consignment_modes=tuple(x for x in parse_choices(
            raw.get("consignment_modes", "open,dutch"), ("open", "sealed", "dutch"))),
        poster_claim_rarities=parse_choices(raw.get("poster_claim_rarities"),
                                            ("normal", "excellent", "rare", "precious", "legendary")),
        # 合并项目：新字段优先，缺省回退到旧字段，保证老配置升级后行为不变
        do_poster_snipe=parse_bool(
            raw.get("do_poster_snipe"),
            parse_bool(raw.get("do_auction")) or parse_bool(raw.get("do_consignment"))),
        poster_snipe_max_price=max(0, _to_int(
            raw.get("poster_snipe_max_price"),
            max(_to_int(raw.get("auction_budget")), _to_int(raw.get("consignment_budget"))))),
        poster_snipe_rarities=(parse_str_list(raw.get("poster_snipe_rarities"))
                               or parse_str_list(raw.get("auction_rarities"))
                               or parse_str_list(raw.get("consignment_rarities"))),
        poster_snipe_unowned_only=parse_bool(raw.get("poster_snipe_unowned_only"), True),
        poster_snipe_poll_seconds=max(3, min(300, _to_int(
            raw.get("poster_snipe_poll_seconds"), _to_int(raw.get("auction_poll_seconds"), 15)))),
        poster_claim_tasks=tuple(str(x).strip() for x in
                                 (raw.get("poster_claim_tasks") or ())
                                 if str(x).strip()),
        poster_snipe_rounds=max(1, min(50, _to_int(raw.get("poster_snipe_rounds"), 3))),
        poster_snipe_ranges=parse_snipe_ranges(raw),
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


def poster_rarity_of(task_key: str) -> str:
    """从任务 key 提取档位：normal_collection_once -> normal。"""
    key = str(task_key or "")
    for rarity in ("normal", "excellent", "rare", "precious", "legendary"):
        if key.startswith(rarity + "_"):
            return rarity
    return ""


def claimable_posters(board: Dict[str, Any], rarities: Tuple[str, ...] = ()) -> List[str]:
    """可领的海报任务；rarities 非空时只领指定档位（囤魔力领完全图鉴用）。"""
    out: List[str] = []
    wanted = set(rarities)
    for task in (board or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        key = str(task.get("key") or "")
        if not key or str(task.get("status") or "") != "claimable":
            continue
        if wanted and poster_rarity_of(key) not in wanted:
            continue
        out.append(key)
    return list(dict.fromkeys(out))


# 任务档位：站点 posterTaskBoard 的 group -> 中文分类名（实测 2026-11）
TASK_GROUP_LABELS: Dict[str, str] = {
    "once": "一次性典藏",
    "theme_repeat": "主题片单交付",
    "rarity_repeat": "额外海报",
}
# 兜底档位：扫描失败时配置页仍有可选中文项；key/title 均来自实测任务板
DEFAULT_TASK_OPTIONS: Tuple[Tuple[str, str, str], ...] = (
    ("once", "普通海报集合", "normal_collection_once"),
    ("once", "优秀海报集合", "excellent_collection_once"),
    ("once", "稀有海报集合", "rare_collection_once"),
    ("once", "珍贵海报集合", "precious_collection_once"),
    ("once", "传说海报集合", "legendary_collection_once"),
    ("once", "完全图鉴", "movie_catalog_album"),
    ("theme_repeat", "动画电影之夜", "animation_night_repeat"),
    ("theme_repeat", "悬疑犯罪现场", "crime_scene_repeat"),
    ("theme_repeat", "心灵成长之旅", "growth_journey_repeat"),
)


def parse_task_options(board: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """从 friendTradePosterTaskBoard 扫描领奖档位，生成中文多选下拉项。

    站点任务会更新，选项以扫描结果为准；扫描为空时退回内置兜底档位。
    title 带【分类】前缀，让「一次性典藏 / 主题片单交付」在一个多选框里可区分。
    """
    tasks = ((board or {}).get("tasks") or [])
    items: List[Dict[str, str]] = []
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        key = str(task.get("key") or "").strip()
        title = str(task.get("title") or "").strip()
        if not key or not title or key in seen:
            continue
        seen.add(key)
        group = str(task.get("group") or "")
        prefix = TASK_GROUP_LABELS.get(group, "")
        items.append({"title": (f"【{prefix}】{title}" if prefix else title), "value": key})
    if not items:
        for group, title, key in DEFAULT_TASK_OPTIONS:
            prefix = TASK_GROUP_LABELS.get(group, "")
            items.append({"title": (f"【{prefix}】{title}" if prefix else title), "value": key})
    return items


def claimable_posters_by_tasks(board: Dict[str, Any], task_keys: Tuple[str, ...]) -> List[str]:
    """按站点 task_key 过滤可领海报任务；task_keys 为空则领全部可领项。"""
    wanted = set(str(x) for x in (task_keys or ()))
    out: List[str] = []
    for task in (board or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        key = str(task.get("key") or "")
        if not key or str(task.get("status") or "") != "claimable":
            continue
        if wanted and key not in wanted:
            continue
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
               poster_board: Dict[str, Any],
               done_work: Optional[set] = None,
               done_interact: Optional[set] = None) -> List[Action]:
    """汇总各玩法计划。顺序：零风险领取 → 工作/互动 → 派遣 → 海报 → 保护/交易。

    done_work / done_interact 是当天已执行过工作/互动的佣人 UID，
    传入后这些佣人不再被安排对应动作（站点本身也有一天一次的限制）。
    """
    actions: List[Action] = []
    worked = set(done_work or ())
    interacted = set(done_interact or ())

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
            if not s.can_work or s.uid in reserved or s.uid in worked:
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
                if not s.can_interact or s.uid in reserved or s.uid in interacted:
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
        if cfg.poster_claim_tasks:
            claim_keys = claimable_posters_by_tasks(poster_board, cfg.poster_claim_tasks)
        else:
            claim_keys = claimable_posters(poster_board, cfg.poster_claim_rarities)
        for key in claim_keys:
            rarity = poster_rarity_of(key)
            label = f"[{RARITY_LABELS.get(rarity, rarity)}]" if rarity else ""
            actions.append(Action("friendTradePosterTaskClaim", {"task_key": key},
                                  f"领海报任务 {label} {key}", "safe"))
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


# ---------------------------------------------------------------- 海报拍卖

# 站点拍卖规则（实测 auctionboard.json 的 rules）
AUCTION_PRICES: Dict[str, int] = {
    "normal": 2000, "excellent": 5000, "rare": 15000,
    "precious": 50000, "legendary": 150000,
}
AUCTION_INCREMENTS: Dict[str, int] = {
    "normal": 200, "excellent": 500, "rare": 1500,
    "precious": 5000, "legendary": 15000,
}
RARITY_LABELS: Dict[str, str] = {
    "normal": "普通", "excellent": "优秀", "rare": "稀有",
    "precious": "珍贵", "legendary": "传说",
}


@dataclass
class AuctionLot:
    """从拍卖看板 current[] 解析出的一个拍品视图。"""
    id: int
    poster_id: int
    title: str
    rarity: str
    status: str
    can_bid: bool
    starting_price: int
    minimum_bid: int
    current_price: int
    min_increment: int
    highest_bid: int
    bidder_count: int
    ends_at: int
    hard_ends_at: int
    rule_version: str
    bid_fee_bps: int
    leader_uid: int
    leader_name: str
    my_bid_amount: int
    my_bid_rank: int
    my_fee_total: float
    owned_quantity: int
    # 寄售场专有字段（拍卖行为默认值）
    mode: str = "open"
    is_mine: bool = False
    can_buy: bool = False
    next_drop_at: int = 0
    floor_price: int = 0
    drop_interval_seconds: int = 0
    drop_amount: int = 0

    @property
    def rarity_label(self) -> str:
        return RARITY_LABELS.get(self.rarity, self.rarity)

    @property
    def is_leading(self) -> bool:
        return self.my_bid_rank == 1

    @property
    def increment(self) -> int:
        """优先用站点给的 min_increment，否则按稀有度档位。"""
        return self.min_increment or AUCTION_INCREMENTS.get(self.rarity, 200)

    def as_dict(self) -> Dict[str, Any]:
        """给端点返回可 JSON 序列化的字段（含寄售场可能有的字段）。"""
        return {
            "id": self.id, "poster_id": self.poster_id, "title": self.title,
            "rarity": self.rarity, "rarity_label": self.rarity_label,
            "status": self.status, "can_bid": self.can_bid,
            "mode": getattr(self, "mode", "open"),
            "owned_quantity": self.owned_quantity,
            "current_price": self.current_price, "minimum_bid": self.minimum_bid,
            "min_increment": self.min_increment, "highest_bid": self.highest_bid,
            "leader": self.leader_name, "my_rank": self.my_bid_rank,
            "ends_at": self.ends_at, "hard_ends_at": self.hard_ends_at,
            "is_mine": bool(getattr(self, "is_mine", False)),
            "can_buy": bool(getattr(self, "can_buy", False)),
            "next_drop_at": getattr(self, "next_drop_at", 0),
            "floor_price": getattr(self, "floor_price", 0),
        }


def parse_auction_board(board: Dict[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    """解析 friendTradePosterAuctionBoard.data，返回统一视图。"""
    data = board or {}
    now_ts = int(now if now is not None else time.time())
    rules = data.get("rules") if isinstance(data.get("rules"), dict) else {}
    lots: List[AuctionLot] = []
    for row in data.get("current") or []:
        if not isinstance(row, dict):
            continue
        aid = _to_int(row.get("id"))
        if aid <= 0:
            continue
        poster = row.get("poster") if isinstance(row.get("poster"), dict) else {}
        leader = row.get("leader") if isinstance(row.get("leader"), dict) else {}
        my_bid = row.get("my_bid") if isinstance(row.get("my_bid"), dict) else {}
        lots.append(AuctionLot(
            id=aid,
            poster_id=_to_int(poster.get("id")),
            title=str(poster.get("title_cn") or poster.get("title") or f"#{aid}"),
            rarity=str(poster.get("rarity") or "normal"),
            status=str(row.get("status") or ""),
            can_bid=bool(row.get("can_bid")),
            starting_price=_to_int(row.get("starting_price")),
            minimum_bid=_to_int(row.get("minimum_bid")),
            current_price=_to_int(row.get("current_price")),
            min_increment=_to_int(row.get("min_increment")),
            highest_bid=_to_int(row.get("highest_bid")),
            bidder_count=_to_int(row.get("bidder_count")),
            ends_at=_to_int(row.get("ends_at")),
            hard_ends_at=_to_int(row.get("hard_ends_at")),
            rule_version=str(row.get("rule_version") or ""),
            bid_fee_bps=_to_int(row.get("bid_fee_bps")),
            leader_uid=_to_int(leader.get("uid")),
            leader_name=str(leader.get("username") or ""),
            my_bid_amount=_to_int(my_bid.get("amount")),
            my_bid_rank=_to_int(my_bid.get("rank")),
            my_fee_total=_to_float(my_bid.get("fee_total")),
            owned_quantity=_to_int(row.get("owned_quantity")),
            mode=str(row.get("mode") or "open"),
            is_mine=bool(row.get("is_mine")),
            can_buy=bool(row.get("can_buy")),
            next_drop_at=_to_int(row.get("next_drop_at")),
            floor_price=_to_int(row.get("floor_price")),
            drop_interval_seconds=_to_int(row.get("drop_interval_seconds")),
            drop_amount=_to_int(row.get("drop_amount")),
        ))
    return {
        "available": bool(data.get("available")),
        "enabled": bool(data.get("enabled")),
        "balance": _to_float(data.get("balance")),
        "max_bid": _to_int(data.get("max_bid"), 10 ** 12),
        "server_time": _to_int(data.get("server_time"), now_ts),
        "next_release_at": _to_int(data.get("next_release_at")),
        "rules": rules,
        "lots": lots,
        "recent": data.get("recent") or [],
    }


def seconds_left(lot: AuctionLot, now: int) -> int:
    """距离落槌的秒数（按 can_bid 的 ends_at，不按 hard_ends_at）。"""
    return max(0, lot.ends_at - now)


def auction_fee(amount: int, bps: int, my_previous: int = 0,
                differential: bool = True) -> float:
    """按站点算法算本次手续费：round(value*bps/1000) 之差 /10，累计到 0.1。

    与 auctions.js bidFee 对齐：
      roundedTenths(q) = floor((q*bps+500)/1000)，fee=(rounded(amount)-rounded(prev))/10
    """
    if bps <= 0 or amount <= 0:
        return 0.0
    base = my_previous if differential else 0
    rounded = lambda q: (int(q) * bps + 500) // 1000
    return max(0, rounded(amount) - rounded(base)) / 10.0


def auction_debit(lot: AuctionLot, amount: int) -> float:
    """本次出价实际占用魔力 = 本金差额 + 手续费差额。"""
    principal = max(0, amount - lot.my_bid_amount)
    fee = auction_fee(amount, lot.bid_fee_bps, lot.my_bid_amount, differential=True)
    return round(principal + fee, 1)


def next_bid_amount(lot: AuctionLot, board_max: int = 10 ** 12,
                    aggressive_steps: int = 0) -> int:
    """下一次出价金额。

    站点已在 minimum_bid 里算好了当前最低有效出价（含领先者 + 一档增量），
    例如实测：最高 293888，increment 5000，minimum_bid=298888。
    因此默认直接用 minimum_bid，绝不重复加增量；只有用户显式设置激进档数时，
    才在最低价基础上再叠加 N 档 increment。
    """
    step = lot.increment or AUCTION_INCREMENTS.get(lot.rarity, 200)
    target = max(1, lot.minimum_bid) + max(0, aggressive_steps) * step
    return max(1, min(board_max, target))


@dataclass
class BidDecision:
    lot_id: int
    amount: int
    debit: float
    reason: str
    priority: int = 0


def plan_auction(cfg: Config, board: Dict[str, Any], now: Optional[int] = None,
                 bid_counts: Optional[Dict[int, int]] = None,
                 last_bid_at: Optional[Dict[int, int]] = None) -> List[BidDecision]:
    """产出拍卖出价决策（纯函数，不真正出价）。

    规则：
    - 看板可用 + 开启监控 + 拍品 open/can_bid；
    - 只抢白名单稀有度（配置为空=不限）；
    - 倒计时进入 snipe 窗口才抢（0=不等待，立即）；最后 3 分钟换领先会延长，
      所以只要还没落槌就可以继续跟价，直到 hard_ends_at 也没用（看板只给 ends_at）；
    - 我已领先就不重复出价；
    - 出价金额 = max(minimum_bid, 当前最高+一档增量)，不得超过单品预算；
    - 单品加价次数、两次间隔、本轮总额度都受配置约束。
    """
    if not cfg.do_auction or not cfg.auction_auto_bid or cfg.auction_budget <= 0:
        return []
    data = board if isinstance(board, dict) else {}
    if not data.get("enabled", True):
        return []
    now_ts = int(now if now is not None else time.time())
    balance = _to_float(data.get("balance"))
    max_bid = _to_int(data.get("max_bid"), 10 ** 12)
    bids = bid_counts or {}
    last_at = last_bid_at or {}
    rarities = set(cfg.auction_rarities)

    decisions: List[BidDecision] = []
    for lot in data.get("lots") or []:
        if not isinstance(lot, AuctionLot):
            continue
        if lot.status != "open" or not lot.can_bid or lot.is_leading:
            continue
        if rarities and lot.rarity not in rarities:
            continue
        if cfg.auction_require_no_leader and lot.leader_uid:
            continue
        left = seconds_left(lot, now_ts)
        if left <= 0:
            continue
        if cfg.auction_snipe_seconds > 0 and left > cfg.auction_snipe_seconds:
            continue
        if bids.get(lot.id, 0) >= cfg.auction_max_bids_per_lot:
            continue
        gap = now_ts - _to_int(last_at.get(lot.id))
        if last_at.get(lot.id) and gap < cfg.auction_min_gap:
            continue
        amount = next_bid_amount(lot, max_bid, cfg.auction_aggressive_steps)
        if amount > cfg.auction_budget:
            continue
        debit = auction_debit(lot, amount)
        if debit > balance:
            continue
        if cfg.auction_total_budget > 0:
            used = sum(d.debit for d in decisions)
            if used + debit > cfg.auction_total_budget:
                continue
        urgency = 0 if left <= 60 else 1
        decisions.append(BidDecision(
            lot_id=lot.id, amount=amount, debit=debit,
            reason=(f"{lot.title}（{lot.rarity_label}）当前{lot.current_price}魔力，"
                    f"距落槌{left}秒，领先者{lot.leader_name or '无'}，"
                    f"出价{amount}（占用{debit}）"),
            priority=urgency,
        ))
    decisions.sort(key=lambda d: (d.priority, -d.debit))
    return decisions


def auction_summary(board: Dict[str, Any]) -> str:
    """给页面/日志看的一行拍卖摘要。"""
    data = board if isinstance(board, dict) else {}
    lots = [x for x in data.get("lots") or [] if isinstance(x, AuctionLot) and x.status == "open"]
    if not lots:
        return "暂无进行中的拍卖"
    parts = []
    for lot in lots[:6]:
        lead = lot.leader_name or "无人"
        parts.append(f"{lot.title}({lot.rarity_label}) {lot.current_price}魔力/领先:{lead}")
    return "；".join(parts) + (f"…等{len(lots)}件" if len(lots) > 6 else "")


# ---------------------------------------------------------------- 海报寄售场决策

@dataclass
class ConsignmentDecision:
    """寄售场抢购决策。mode=dutch 用 dutch_buy，mode=open/sealed 用 auction_bid。"""
    lot_id: int
    poster_id: int
    title: str
    rarity: str
    mode: str
    amount: int
    fee: float
    action_kind: str        # dutch_buy / bid
    rule_version: str
    reason: str


def parse_consignment_board(board: Dict[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    """寄售场看板：拍品在 items[]（拍卖行在 current[]），结构一致，复用解析器。"""
    data = dict(board or {})
    # 归一化字段：寄售场用 items，拍卖行用 current
    if "current" not in data and isinstance(data.get("items"), list):
        data["current"] = data["items"]
    view = parse_auction_board(data, now=now)
    view["can_list"] = bool(data.get("can_list"))
    view["open_count"] = _to_int(data.get("open_count"))
    view["inventory_revision"] = _to_int(data.get("inventory_revision"))
    view["next_refresh_at"] = _to_int(data.get("next_refresh_at"))
    return view


def plan_consignment(cfg: Config, board: Dict[str, Any], now: Optional[int] = None) -> List[ConsignmentDecision]:
    """按「未拥有/想要 + 价格上限」产出寄售场抢购决策（纯函数）。

    寄售场有两种买法：
    - dutch（荷兰降价）：按 current_price 直接买（friendTradePosterDutchBuy）；
      next_drop_at 之后会降价，若现价已达标可直接买，否则交给用户设置更低预算等降价。
    - open/sealed（竞价）：和拍卖行一样按 minimum_bid 出价（friendTradePosterAuctionBid）。
    """
    if not cfg.do_consignment or not cfg.consignment_auto_bid or cfg.consignment_budget <= 0:
        return []
    data = board if isinstance(board, dict) else {}
    if not data.get("enabled", True):
        return []
    balance = _to_float(data.get("balance"))
    max_bid = _to_int(data.get("max_bid"), 10 ** 12)
    wanted = set(cfg.consignment_wanted_poster_ids)
    rarities = set(cfg.consignment_rarities)
    modes = set(cfg.consignment_modes) or {"open", "dutch"}
    now_ts = int(now if now is not None else time.time())

    decisions: List[ConsignmentDecision] = []
    for lot in data.get("lots") or []:
        if not isinstance(lot, AuctionLot):
            continue
        if lot.status != "open" or lot.is_mine:
            continue
        if lot.mode not in modes:
            continue
        # 未拥有过滤：已拥有该海报就不重复抢
        if cfg.consignment_unowned_only and lot.owned_quantity > 0:
            continue
        # 想要过滤：指定海报 ID 优先；否则按稀有度档位
        if wanted and lot.poster_id not in wanted:
            continue
        if not wanted and rarities and lot.rarity not in rarities:
            continue

        if lot.mode == "dutch":
            amount = _to_int(lot.current_price)
            action_kind = "dutch_buy"
        else:
            amount = lot.minimum_bid
            action_kind = "bid"
        if amount <= 0 or amount > min(cfg.consignment_budget, max_bid):
            continue
        fee = auction_fee(amount, lot.bid_fee_bps)
        if amount + fee > balance:
            continue
        decisions.append(ConsignmentDecision(
            lot_id=lot.id, poster_id=lot.poster_id, title=lot.title, rarity=lot.rarity,
            mode=lot.mode, amount=amount, fee=fee, action_kind=action_kind,
            rule_version=lot.rule_version,
            reason=(f"寄售 {lot.mode}《{lot.title}》（{lot.rarity_label}）"
                    f"{'未拥有' if lot.owned_quantity == 0 else f'已有{lot.owned_quantity}张'}，"
                    f"{'现价' if lot.mode == 'dutch' else '最低出价'}{amount}魔力"
                    f"（手续费{fee}）"),
        ))
    decisions.sort(key=lambda d: d.amount)
    return decisions


def consignment_summary(board: Dict[str, Any]) -> str:
    data = board if isinstance(board, dict) else {}
    lots = [x for x in data.get("lots") or [] if isinstance(x, AuctionLot) and x.status == "open"]
    if not lots:
        return "寄售场暂无在售海报"
    unowned = sum(1 for x in lots if x.owned_quantity == 0)
    dutch = sum(1 for x in lots if x.mode == "dutch")
    return f"在售 {len(lots)} 件（未拥有 {unowned}，荷兰降价 {dutch}）"


# ---------------------------------------------------------------- 统一低价抢购（拍卖行 + 寄售场）

@dataclass
class SnipeDecision:
    """海报低价抢购决策：source 标来源，action_kind 标买法。"""
    lot_id: int
    poster_id: int
    title: str
    rarity: str
    source: str           # auction=光影拍卖行 / consignment=海报寄售场
    mode: str             # open / sealed / dutch
    amount: int           # 实际支付/出价
    fee: float
    action_kind: str      # dutch_buy / bid
    rule_version: str
    reason: str
    dedup_key: str = ""   # 已抢记录用的去重键（来源:lot_id）

    def as_dict(self) -> Dict[str, Any]:
        return {
            "lot_id": self.lot_id, "poster_id": self.poster_id, "title": self.title,
            "rarity": self.rarity, "source": self.source, "mode": self.mode,
            "amount": self.amount, "fee": self.fee, "action_kind": self.action_kind,
            "rule_version": self.rule_version, "reason": self.reason,
            "dedup_key": self.dedup_key,
        }


def snipe_dedup_key(source: str, lot_id: int) -> str:
    """已出价记录键。两个市场的 lot_id 可能撞号，故带来源前缀。"""
    return f"{source}:{lot_id}"


def snipe_price_allowed(cfg: Config, amount: int, rarity: str) -> bool:
    """价格门槛：优先用「该稀有度价格区间」，未配区间的稀有度退回全局上限。

    全局 poster_snipe_max_price > 0 时始终作为总闸门（不得超过）。
    配了任一稀有度区间后，未配区间的稀有度仍可用全局上限抢；两者都为 0 则不抢。
    """
    if amount <= 0:
        return False
    if cfg.poster_snipe_max_price > 0 and amount > cfg.poster_snipe_max_price:
        return False
    rng = cfg.poster_snipe_ranges.get(rarity)
    if rng is not None:
        low, high = rng
        return low <= amount <= high
    return cfg.poster_snipe_max_price > 0


def plan_snipe(cfg: Config,
               auction_view: Optional[Dict[str, Any]],
               consignment_view: Optional[Dict[str, Any]],
               now: Optional[int] = None,
               attempted: Optional[set] = None) -> List[SnipeDecision]:
    """把拍卖行与寄售场的在售海报合并成一个候选池，按价格从低到高产出抢购决策。

    规则（单一项目、逻辑收敛）：
    - 总开关 do_poster_snipe；价格闸门 = 每稀有度区间，未配的用全局上限（都为 0 则不抢）；
    - 「抢购轮次」poster_snipe_rounds：一次扫描最多连续出价 N 笔，避免一轮把手续费全砸出去；
    - attempted 为已出价记录（含未中标）：跳过，因为出价即扣手续费、未中标不退；
    - 每个候选的「到手价」：dutch 用当前价直接买，open/sealed 用最低有效出价 minimum_bid；
    - 按到手价升序排列——先抢最便宜的；
    - 出价动作仍受单点规则保护：不领先、金额≥站点最低有效价、余额覆盖金额+手续费。
    """
    if not cfg.do_poster_snipe:
        return []
    if cfg.poster_snipe_max_price <= 0 and not cfg.poster_snipe_ranges:
        return []
    now_ts = int(now if now is not None else time.time())
    rarities = set(cfg.poster_snipe_rarities)
    tried = set(attempted or ())
    balance = 0.0
    for view in (auction_view, consignment_view):
        if isinstance(view, dict) and _to_float(view.get("balance")) > balance:
            balance = _to_float(view.get("balance"))

    candidates: List[SnipeDecision] = []
    for view, source in ((auction_view, "拍卖行"), (consignment_view, "寄售场")):
        if not isinstance(view, dict) or not view.get("enabled", True):
            continue
        for lot in view.get("lots") or []:
            if not isinstance(lot, AuctionLot):
                continue
            key = snipe_dedup_key(source, lot.id)
            # 已抢过的（含未中标）不再出价：手续费已扣，重复出价会再扣一次
            if key in tried or str(lot.id) in tried or lot.id in tried:
                continue
            if lot.status != "open" or lot.is_mine or lot.can_bid is False and lot.can_buy is False:
                continue
            if cfg.poster_snipe_unowned_only and lot.owned_quantity > 0:
                continue
            if rarities and lot.rarity not in rarities:
                continue
            if lot.mode == "dutch":
                amount = _to_int(lot.current_price)
                kind = "dutch_buy"
            else:
                amount = _to_int(lot.minimum_bid)
                kind = "bid"
                if lot.is_leading:
                    continue
            if not snipe_price_allowed(cfg, amount, lot.rarity):
                continue
            fee = auction_fee(amount, lot.bid_fee_bps)
            if balance > 0 and amount + fee > balance:
                continue
            candidates.append(SnipeDecision(
                lot_id=lot.id, poster_id=lot.poster_id, title=lot.title, rarity=lot.rarity,
                source=source, mode=lot.mode, amount=amount, fee=fee, action_kind=kind,
                rule_version=lot.rule_version, dedup_key=key,
                reason=(f"[{source}]《{lot.title}》{lot.rarity_label} "
                        f"{'荷兰现价' if kind == 'dutch_buy' else '最低出价'}{amount}魔力"
                        f"｜手续费{fee}魔力（出价即扣，未中标不退）"),
            ))

    # 核心：低价优先。同价时寄售场在前（可直接买、无需等落槌）
    candidates.sort(key=lambda d: (d.amount, 0 if d.source == "寄售场" else 1))
    # 抢购轮次：本轮最多出价 N 笔，按价格从低到高取
    return candidates[:max(1, cfg.poster_snipe_rounds)]


def snipe_summary(auction_view: Optional[Dict[str, Any]],
                  consignment_view: Optional[Dict[str, Any]]) -> str:
    """给页面看的一行合并行情摘要。"""
    parts = []
    for view, name in ((auction_view, "拍卖行"), (consignment_view, "寄售场")):
        lots = [x for x in (view or {}).get("lots") or []
                if isinstance(x, AuctionLot) and x.status == "open"]
        if not lots:
            parts.append(f"{name}无在售")
            continue
        cheapest = min(lots, key=lambda x: x.current_price or x.minimum_bid)
        price = cheapest.current_price or cheapest.minimum_bid
        parts.append(f"{name}{len(lots)}件，最低{price}《{cheapest.title}》")
    return "；".join(parts)
