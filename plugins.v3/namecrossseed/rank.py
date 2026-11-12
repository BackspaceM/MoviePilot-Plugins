"""由发布名推导的候选种子排序工具（补充 site 模块）。"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from .site import SiteTorrent, candidate_ok


def size_diff_ratio(local_size: int, candidate_size: int) -> float:
    """返回两个体积的相对差异，0 表示完全一致，最大 1（100%）。

    分母取两者较大值：合集与单集本来就可能差几十倍，用本地体积做分母
    会算出 23697% 这种没有意义的数字，也不能拿它当拒绝依据。
    体积只参与候选排序，同源与否始终由 info_hash / 文件清单判定。
    """
    left = int(local_size or 0)
    right = int(candidate_size or 0)
    if not left or not right:
        return 1.0
    return abs(left - right) / max(left, right, 1)


def rank_candidates(
    local_name: str,
    local_size: int,
    candidates: Iterable[SiteTorrent],
    min_score: float = 0.6,
) -> List[Tuple[float, float, SiteTorrent]]:
    """按“名称相似度优先、体积接近次之”排序候选，返回 (相似度, 体积差, 种子)。"""
    ranked: List[Tuple[float, float, SiteTorrent]] = []
    for candidate in candidates:
        score = candidate_ok(local_name, candidate, min_score)
        if score <= 0:
            continue
        ranked.append((score, size_diff_ratio(local_size, candidate.size), candidate))
    ranked.sort(key=lambda item: (-item[0], item[1], -item[2].seeders))
    return ranked
