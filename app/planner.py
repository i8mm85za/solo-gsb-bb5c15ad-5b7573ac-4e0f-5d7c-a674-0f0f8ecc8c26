"""乐谱安全翻页动态规划核心（single 单页 / spread 双页谱架）。

目标（按字典序依次优化）：
1. 最大化全曲最短的一次实体翻页休止（瓶颈值，越大越安全）；
2. 在达到该瓶颈的方案中，最小化各页留白高度的平方和；
3. 仍并列时，取页数更少的方案。

约束：
- 每页容纳连续若干系统，总高度不得超过页面可用高度；
- single：每次翻页都是实体翻页，只允许在休止秒数 >= 最低安全休止的边界翻页
  （最后一页之后无需翻页）；
- spread：相邻左、右页组成一组同时可见，组内（左页→右页）边界不检查休止；
  只有翻过右页进入下一组才是实体翻页，要求休止 >= 最低安全休止，
  且只有实体翻页参与最短翻页休止最大化。

人工断点约束（系统下标，即“某系统之后”的边界，末系统之后无边界、不可指定）：
- forced_break_after：对应系统之后必须分页。DP 中禁止跨过该断点的边
  （任何从 0 到 n 的路径因此必经过该页末位置）；
- forbidden_break_after：对应系统之后禁止分页。DP 中禁止以该位置作为
  （非末页）页末的边，只能由同一页跨越该边界。
- spread 模式下，强制断出的那一页若是左页，仍是组内边界：不检查休止、
  不算实体翻页、不参与瓶颈；只有右页之后的断点才检查 min_safe_rest 并参与瓶颈。

实现：两阶段精确 DP。
- 阶段一：widest-path DP 求最大可行瓶颈 B*，同时得到各位置可达性；
- 阶段二：仅保留休止 >= B* 的实体翻页边，做 (留白平方和, 页数) 字典序最小的 DP。
spread 模式下 DP 状态额外携带已用页数奇偶：页码奇偶决定该页在左还是右，
从而决定该页之后是否为实体翻页。

无解诊断：
- 若某系统自身高度超过页面可用高度，报告首个这样的系统（无法容纳）；
- 否则沿可行边扫描，报告首个无法被任何可行路径处理的边界 b：
  * 若 b 被 forbidden_break_after 禁止却无法跨越（相邻系统同页必然超高，
    或跨越它的页面无处可结束），报告该禁止断点；
  * 否则 b 是无法安全完成的（实体）翻页边界（single 休止不足；spread 下
    所有可达分页方式都使该页落在右页、需要实体翻页）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import inf
from typing import Any, Optional

_EPS = 1e-9  # 浮点比较容差


@dataclass
class InfeasibleError(Exception):
    """无解诊断：首个无法容纳的系统，或首个无法处理的（实体）翻页边界。"""

    reason: str  # "system_exceeds_page_height" | "unsafe_boundary"
    detail: dict[str, Any] = field(default_factory=dict)


def validate_break_constraints(
    n: int,
    forced_break_after: list[int],
    forbidden_break_after: list[int],
) -> list[dict[str, Any]]:
    """检查人工断点约束本身的冲突，返回冲突列表（空列表表示无冲突）。

    冲突类型：
    - overlap：同一系统下标同时出现在两组约束中；
    - points_to_last_system：下标指向末系统（其后不存在边界，无法分页）；
    - index_out_of_range：下标超出系统范围。
    """
    forced_set = set(forced_break_after)
    forbidden_set = set(forbidden_break_after)
    last = n - 1
    conflicts: list[dict[str, Any]] = []

    for s in sorted(forced_set & forbidden_set):
        conflicts.append(
            {
                "type": "overlap",
                "system_index": s,
                "fields": ["forced_break_after", "forbidden_break_after"],
                "message": (
                    f"系统下标 {s} 同时出现在 forced_break_after 与 "
                    "forbidden_break_after 中：该边界不能既要求分页又禁止分页"
                ),
            }
        )

    for field_name, values in (
        ("forced_break_after", forced_set),
        ("forbidden_break_after", forbidden_set),
    ):
        if last in values:
            conflicts.append(
                {
                    "type": "points_to_last_system",
                    "field": field_name,
                    "system_index": last,
                    "message": (
                        f"{field_name} 中的系统下标 {last} 指向末系统："
                        f"全曲共 {n} 个系统（下标 0..{last}），末系统之后没有边界，"
                        "不能在那里分页"
                    ),
                }
            )
        for s in sorted(v for v in values if v < 0 or v >= n):
            conflicts.append(
                {
                    "type": "index_out_of_range",
                    "field": field_name,
                    "system_index": s,
                    "system_count": n,
                    "message": (
                        f"{field_name} 中的系统下标 {s} 超出范围："
                        f"有效下标为 0..{last}"
                    ),
                }
            )

    return conflicts


def plan_pages(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    layout_mode: str = "single",
    spread_start_side: str = "left",
    forced_break_after: Optional[list[int]] = None,
    forbidden_break_after: Optional[list[int]] = None,
) -> dict[str, Any]:
    """生成完整分页方案；无解时抛出 InfeasibleError，不产生任何部分方案。"""
    n = len(system_heights)
    if n == 0:
        raise ValueError("system_heights must not be empty")
    if len(boundary_rests) != n - 1:
        raise ValueError("boundary_rests must have exactly len(system_heights) - 1 entries")
    if layout_mode not in ("single", "spread"):
        raise ValueError("layout_mode must be 'single' or 'spread'")
    if spread_start_side not in ("left", "right"):
        raise ValueError("spread_start_side must be 'left' or 'right'")

    forced = sorted(set(forced_break_after or []))
    forbidden = sorted(set(forbidden_break_after or []))
    conflicts = validate_break_constraints(n, forced, forbidden)
    if conflicts:
        raise ValueError("; ".join(c["message"] for c in conflicts))

    # 首个无法容纳的系统
    for i, h in enumerate(system_heights):
        if h > page_height + _EPS:
            raise InfeasibleError(
                reason="system_exceeds_page_height",
                detail={
                    "system_index": i,
                    "system_height": h,
                    "page_height": page_height,
                    "message": (
                        f"系统 {i} 的高度 {h} 超过页面可用高度 {page_height}，"
                        "任何页面都无法容纳该系统"
                    ),
                },
            )

    if layout_mode == "spread":
        return _plan_spread(
            system_heights,
            boundary_rests,
            page_height,
            min_safe_rest,
            spread_start_side,
            set(forced),
            set(forbidden),
        )
    return _plan_single(
        system_heights,
        boundary_rests,
        page_height,
        min_safe_rest,
        set(forced),
        set(forbidden),
    )


def _prefix_sums(system_heights: list[float]) -> list[float]:
    prefix = [0.0] * (len(system_heights) + 1)
    for i, h in enumerate(system_heights):
        prefix[i + 1] = prefix[i] + h
    return prefix


def _forced_floor(forced: set[int], n: int) -> list[int]:
    """forced_floor[t] = 不超过 t 的最大强制断点系统下标（无则 -1）。

    边 (i, j) 跨越强制断点 s 当且仅当 i <= s <= j-2；因此 i 一旦
    <= forced_floor[j-2]，该边及更小 i 的边都非法，可直接停止扫描。
    """
    floor = [-1] * n
    last = -1
    for t in range(n):
        if t in forced:
            last = t
        floor[t] = last
    return floor


def _plan_single(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    forced: set[int],
    forbidden: set[int],
) -> dict[str, Any]:
    """single 模式：每次翻页都是实体翻页。"""
    n = len(system_heights)
    prefix = _prefix_sums(system_heights)
    floor = _forced_floor(forced, n)

    def height(i: int, j: int) -> float:
        """系统 [i, j) 的总高度。"""
        return prefix[j] - prefix[i]

    # 边界处理标记：允许的可行边 (i,j) 把组内边界 [i, j-2] 标记为“同页跨越”，
    # 以 j<n 结束时把边界 j-1 标记为“完成一次安全翻页”。差分数组做区间标记。
    span_diff = [0] * n
    break_ok = [False] * (n - 1)

    def mark_edge(i: int, j: int) -> None:
        if i <= j - 2:
            span_diff[i] += 1
            span_diff[j - 1] -= 1  # 覆盖区间 [i, j-2]
        if j < n:
            break_ok[j - 1] = True

    # 阶段一：widest-path，最大化最短翻页休止；同时记录可达性
    widest = [-inf] * (n + 1)
    widest[0] = inf
    for j in range(1, n + 1):
        # 边界 j-1 禁止分页：任何以 j 为（非末页）页末的边都不允许
        if j < n and (j - 1) in forbidden:
            continue
        best = -inf
        for i in range(j - 1, -1, -1):
            if height(i, j) > page_height + _EPS:
                break  # 高度为正，i 再小只会更高
            if j >= 2 and i <= floor[j - 2]:
                break  # 该边（及更小 i）跨过强制断点
            if widest[i] == -inf:
                continue
            if j < n and boundary_rests[j - 1] < min_safe_rest - _EPS:
                break  # 该边界休止不足，对所有 i 都不可断开
            mark_edge(i, j)
            w = widest[i] if j == n else min(widest[i], boundary_rests[j - 1])
            if w > best:
                best = w
        widest[j] = best

    if widest[n] == -inf:
        b = _first_unhandled_boundary(span_diff, break_ok)
        raise _boundary_error(
            b,
            boundary_rests,
            page_height,
            min_safe_rest,
            forced,
            forbidden,
            height,
            physical_turn=True,
            spread=False,
        )

    bottleneck = widest[n]

    # 阶段二：只用休止 >= 瓶颈的翻页边，最小化 (留白平方和, 页数)
    dp: list[Optional[tuple[float, int]]] = [None] * (n + 1)
    parent = [-1] * (n + 1)
    dp[0] = (0.0, 0)
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue
        for i in range(j - 1, -1, -1):
            h = height(i, j)
            if h > page_height + _EPS:
                break
            if j >= 2 and i <= floor[j - 2]:
                break
            if dp[i] is None:
                continue
            if j < n and boundary_rests[j - 1] < bottleneck - _EPS:
                break  # 低于瓶颈的翻页边会破坏第一目标，禁用
            cand = (dp[i][0] + (page_height - h) ** 2, dp[i][1] + 1)
            if dp[j] is None or cand < dp[j]:
                dp[j] = cand
                parent[j] = i

    # 回溯切分点
    cuts: list[tuple[int, int]] = []
    j = n
    while j > 0:
        i = parent[j]
        cuts.append((i, j))
        j = i
    cuts.reverse()

    pages = []
    for idx, (i, j) in enumerate(cuts):
        h = height(i, j)
        last = j == n
        pages.append(
            {
                "page": idx + 1,
                "start_system": i,
                "end_system": j - 1,
                "system_count": j - i,
                "used_height": round(h, 6),
                "slack": round(page_height - h, 6),
                "turn_rest_after": None if last else boundary_rests[j - 1],
                "side": None,  # single 模式不分左右页
                "physical_turn_after": not last,  # single 模式每次翻页都是实体翻页
            }
        )

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(dp[n][0], 6),
        "pages": pages,
    }


def _plan_spread(
    system_heights: list[float],
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    spread_start_side: str,
    forced: set[int],
    forbidden: set[int],
) -> dict[str, Any]:
    """spread 模式：相邻左右页同组可见，仅翻过右页（实体翻页）才检查休止。

    DP 状态携带已用页数奇偶：第 k 页（0 起）的左右由 k 与 spread_start_side
    共同决定，页码奇偶因此直接决定该页之后是否为实体翻页。
    """
    n = len(system_heights)
    prefix = _prefix_sums(system_heights)
    floor = _forced_floor(forced, n)

    def height(i: int, j: int) -> float:
        """系统 [i, j) 的总高度。"""
        return prefix[j] - prefix[i]

    # 第 1 页（下标 0）位于 spread_start_side，之后左右交替
    right_parity = 1 if spread_start_side == "left" else 0

    def is_right_page(page_index: int) -> bool:
        return page_index % 2 == right_parity

    span_diff = [0] * n
    break_ok = [False] * (n - 1)

    def mark_edge(i: int, j: int) -> None:
        if i <= j - 2:
            span_diff[i] += 1
            span_diff[j - 1] -= 1
        if j < n:
            break_ok[j - 1] = True

    # 阶段一：widest-path，状态 = (位置, 已用页数奇偶)，最大化最短实体翻页休止
    widest = [[-inf, -inf] for _ in range(n + 1)]
    widest[0][0] = inf
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue
        for i in range(j - 1, -1, -1):
            if height(i, j) > page_height + _EPS:
                break  # 高度为正，i 再小只会更高
            if j >= 2 and i <= floor[j - 2]:
                break  # 该边（及更小 i）跨过强制断点
            for q in (0, 1):
                if widest[i][q] == -inf:
                    continue
                # 结束于 j 的这一页下标奇偶为 q；右页之后（非末页）才是实体翻页
                physical = j < n and is_right_page(q)
                if physical:
                    rest = boundary_rests[j - 1]
                    if rest < min_safe_rest - _EPS:
                        continue  # 该次实体翻页休止不足
                    w = min(widest[i][q], rest)
                else:
                    w = widest[i][q]  # 组内边界不检查休止，也不影响瓶颈
                mark_edge(i, j)
                if w > widest[j][1 - q]:
                    widest[j][1 - q] = w

    if widest[n][0] == -inf and widest[n][1] == -inf:
        b = _first_unhandled_boundary(span_diff, break_ok)
        raise _boundary_error(
            b,
            boundary_rests,
            page_height,
            min_safe_rest,
            forced,
            forbidden,
            height,
            physical_turn=True,
            spread=True,
        )

    bottleneck = max(widest[n][0], widest[n][1])

    # 阶段二：只用休止 >= 瓶颈的实体翻页边，最小化 (留白平方和, 页数)
    dp: list[list[Optional[tuple[float, int]]]] = [[None, None] for _ in range(n + 1)]
    parent: list[list[Optional[tuple[int, int]]]] = [[None, None] for _ in range(n + 1)]
    dp[0][0] = (0.0, 0)
    for j in range(1, n + 1):
        if j < n and (j - 1) in forbidden:
            continue
        for i in range(j - 1, -1, -1):
            h = height(i, j)
            if h > page_height + _EPS:
                break
            if j >= 2 and i <= floor[j - 2]:
                break
            for q in (0, 1):
                if dp[i][q] is None:
                    continue
                physical = j < n and is_right_page(q)
                if physical and boundary_rests[j - 1] < bottleneck - _EPS:
                    continue  # 低于瓶颈的实体翻页边会破坏第一目标，禁用
                cand = (dp[i][q][0] + (page_height - h) ** 2, dp[i][q][1] + 1)
                if dp[j][1 - q] is None or cand < dp[j][1 - q]:
                    dp[j][1 - q] = cand
                    parent[j][1 - q] = (i, q)

    # 终点两种页数奇偶取 (留白平方和, 页数) 更优者
    best_cost, final_q = min((dp[n][q], q) for q in (0, 1) if dp[n][q] is not None)

    # 回溯切分点
    cuts: list[tuple[int, int]] = []
    j, q = n, final_q
    while j > 0:
        prev = parent[j][q]
        assert prev is not None
        i, q = prev
        cuts.append((i, j))
        j = i
    cuts.reverse()

    pages = []
    for idx, (i, j) in enumerate(cuts):
        h = height(i, j)
        right = is_right_page(idx)
        last = j == n
        physical = right and not last
        pages.append(
            {
                "page": idx + 1,
                "start_system": i,
                "end_system": j - 1,
                "system_count": j - i,
                "used_height": round(h, 6),
                "slack": round(page_height - h, 6),
                "turn_rest_after": boundary_rests[j - 1] if physical else None,
                "side": "right" if right else "left",
                "physical_turn_after": physical,
            }
        )

    return {
        "page_count": len(pages),
        "min_turn_rest": None if bottleneck == inf else bottleneck,
        "total_slack_squared": round(best_cost[0], 6),
        "pages": pages,
    }


def _first_unhandled_boundary(span_diff: list[int], break_ok: list[bool]) -> int:
    """差分还原后返回首个未被任何可行边处理（同页跨越或安全断开）的边界。"""
    running = 0
    for b in range(len(break_ok)):
        running += span_diff[b]
        if running == 0 and not break_ok[b]:
            return b
    raise AssertionError("终点不可达时必然存在未处理边界")  # pragma: no cover


def _boundary_error(
    b: int,
    boundary_rests: list[float],
    page_height: float,
    min_safe_rest: float,
    forced: set[int],
    forbidden: set[int],
    height,
    physical_turn: bool,
    spread: bool,
) -> InfeasibleError:
    """构造边界级无解诊断：禁止断点不可跨越，或（实体）翻页休止不足。"""
    rest = boundary_rests[b]
    base: dict[str, Any] = {
        "boundary_index": b,
        "between_systems": [b, b + 1],
    }

    if b in forbidden:
        # 相邻两系统同页都超高时，分页在该边界确实不可避免；否则跨越它的页面
        # 虽放得下，也无法在休止达标的位置结束（或随后即超高）。
        merged = height(b, b + 2)
        if merged > page_height + _EPS:
            reason_tail = f"系统 {b} 与系统 {b + 1} 同页高度 {merged} 超过页面可用高度 {page_height}，分页在此处不可避免"
        else:
            reason_tail = "跨越该边界的页面要么超过页面可用高度，要么无法在休止达标的边界结束"
        base.update(
            {
                "forbidden_break_after": b,
                "rest_seconds": rest,
                "message": (
                    f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）被 "
                    f"forbidden_break_after 禁止分页，但没有任何可行页面能跨越该边界：{reason_tail}"
                ),
            }
        )
        return InfeasibleError(reason="unsafe_boundary", detail=base)

    if spread:
        message = (
            f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）是首个无法安全完成的实体翻页边界："
            "所有可达的分页方式都使此处结束于右页、需要实体翻页，但休止 "
            f"{rest}s 低于最低安全休止 {min_safe_rest}s"
        )
        base["physical_turn"] = physical_turn
    else:
        message = (
            f"边界 {b}（系统 {b} 与系统 {b + 1} 之间）的休止 {rest}s "
            f"低于最低安全休止 {min_safe_rest}s，此前区间无法安全断开"
        )
    if b in forced:
        message += "；且该边界被 forced_break_after 要求必须分页，无法绕开"
        base["forced_break_after"] = b
    base.update(
        {
            "rest_seconds": rest,
            "min_safe_rest": min_safe_rest,
            "message": message,
        }
    )
    return InfeasibleError(reason="unsafe_boundary", detail=base)
