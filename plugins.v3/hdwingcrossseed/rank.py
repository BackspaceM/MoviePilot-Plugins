"""由发布名推导的候选种子排序工具（补充 hdwing 模块）。"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from .hdwing import SiteTorrent, candidate_ok


def size_diff_ratio(local_size: int, candidate_size: int) -> float:
    """返回两个体积的相对差异，0 表示完全一致。"""
    if not local_size or not candidate_size:
        return 1.0
    return abs(int(local_size) - int(candidate_size)) / max(int(local_size), 1)


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
