"""请求 / 响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class PlanRequest(BaseModel):
    """按顺序提交的谱面数据与约束。"""

    system_heights: list[float] = Field(
        ..., min_length=1, description="按演奏顺序排列的各谱面系统高度"
    )
    boundary_rests: list[float] = Field(
        default_factory=list,
        description="相邻系统之间边界的休止秒数，长度 = 系统数 - 1",
    )
    page_height: float = Field(..., gt=0, allow_inf_nan=False, description="页面可用高度")
    min_safe_rest: float = Field(..., ge=0, allow_inf_nan=False, description="最低安全休止秒数")
    layout_mode: Literal["single", "spread"] = Field(
        default="single",
        description="排版模式：single 单页逐页翻（默认）；spread 双页谱架，相邻左右页同组可见",
    )
    spread_start_side: Literal["left", "right"] = Field(
        default="left",
        description="spread 模式下第 1 页所在的一侧（left/right，默认 left）；single 模式下忽略",
    )
    forced_break_after: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list,
        description=(
            "系统下标数组（默认空）：对应系统之后必须分页。"
            "spread 模式下若断出的是左页，仍只是组内边界（不检查休止、不算实体翻页）"
        ),
    )
    forbidden_break_after: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list,
        description=(
            "系统下标数组（默认空）：对应系统之后禁止分页，只能由同一页跨越该边界"
        ),
    )

    @model_validator(mode="after")
    def _check_values(self) -> "PlanRequest":
        for h in self.system_heights:
            if h != h or h in (float("inf"), float("-inf")) or h <= 0:
                raise ValueError("system_heights 必须全部为正的有限数值")
        for r in self.boundary_rests:
            if r != r or r in (float("inf"), float("-inf")) or r < 0:
                raise ValueError("boundary_rests 必须全部为非负的有限数值")
        if len(self.boundary_rests) != len(self.system_heights) - 1:
            raise ValueError(
                "boundary_rests 的长度必须等于 len(system_heights) - 1"
            )
        # 去重并规范化顺序，便于保存与回放
        self.forced_break_after = sorted(set(self.forced_break_after))
        self.forbidden_break_after = sorted(set(self.forbidden_break_after))
        return self


class PageOut(BaseModel):
    page: int
    start_system: int
    end_system: int
    system_count: int
    used_height: float
    slack: float
    turn_rest_after: Optional[float] = None
    side: Optional[str] = None  # spread 模式为 "left"/"right"；single 模式为 null
    physical_turn_after: Optional[bool] = None  # 该页之后是否需要实体翻页


class PlanOut(BaseModel):
    id: int
    created_at: str
    system_heights: list[float]
    boundary_rests: list[float]
    page_height: float
    min_safe_rest: float
    layout_mode: str = "single"
    spread_start_side: Optional[str] = None
    forced_break_after: list[int] = Field(default_factory=list)
    forbidden_break_after: list[int] = Field(default_factory=list)
    system_count: int
    page_count: int
    min_turn_rest: Optional[float] = None
    total_slack_squared: float
    pages: list[PageOut]


class PlanSummary(BaseModel):
    id: int
    created_at: str
    system_count: int
    page_count: int
    min_turn_rest: Optional[float] = None
    total_slack_squared: float
    layout_mode: str = "single"
