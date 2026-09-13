"""锁线装订请求的 Pydantic 模型。

单位约定：长度均为毫米(mm)；孔位为沿书脊自天头(头)起算的距离。
书脊长度：左右装订取成品页高，上下装订取成品页宽。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class StitchType(str, Enum):
    """缝法：chain 链式（帖间锁链结连接）/ tape 绕带式（绕支撑带连接）。"""

    chain = "chain"
    tape = "tape"


class TapeSpec(BaseModel):
    """支撑带：position_mm 为带中心距天头的位置，width_mm 为带宽。

    每条支撑带在书脊上占据 [position-width/2, position+width/2]，
    其两侧边缘各需要一个锚孔（绕带的进出出针孔）。
    """

    position_mm: float = Field(gt=0, description="带中心距天头 mm")
    width_mm: float = Field(gt=0, description="带宽 mm")


class HoleCountRange(BaseModel):
    """孔数范围（每帖基础孔位数，含锁定孔与支撑带锚孔）。"""

    min: int = Field(default=3, ge=2, description="最少孔数")
    max: int = Field(default=8, ge=2, description="最多孔数")

    @model_validator(mode="after")
    def _check(self) -> "HoleCountRange":
        if self.max < self.min:
            raise ValueError("孔数上限不能小于下限")
        return self


class ForbiddenZone(BaseModel):
    """禁打区：沿书脊的闭区间 [start_mm, end_mm]，该帖不得在此打孔。"""

    start_mm: float = Field(ge=0)
    end_mm: float = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> "ForbiddenZone":
        if self.end_mm <= self.start_mm:
            raise ValueError("禁打区终点必须大于起点")
        return self


class SignatureOverride(BaseModel):
    """逐帖标记：禁打区、破损孔与已打孔的锁定位置。"""

    index: int = Field(ge=0, description="帖序号（0 基，与拼版方案一致）")
    forbidden_zones: list[ForbiddenZone] = Field(
        default_factory=list, description="该帖禁打区列表"
    )
    damaged_holes_mm: list[float] = Field(
        default_factory=list, description="破损孔位置（该帖跳过不用）"
    )
    locked_holes_mm: list[float] = Field(
        default_factory=list, description="已打孔的锁定位置（必须保留使用）"
    )


class SewingParams(BaseModel):
    """锁线参数：通过 plan_id 引用拼版方案，描述缝法与打孔约束。"""

    plan_id: str = Field(description="来源拼版方案 id（不可变版本）")
    stitch: StitchType = Field(
        default=StitchType.chain, description="缝法：chain 链式 / tape 绕带式"
    )
    head_margin_mm: float = Field(default=12.0, ge=0, description="天头留量 mm")
    tail_margin_mm: float = Field(default=12.0, ge=0, description="地脚留量 mm")
    hole_count: HoleCountRange = Field(
        default_factory=HoleCountRange, description="孔数范围"
    )
    min_spacing_mm: float = Field(default=12.0, gt=0, description="最小孔距 mm")
    tapes: list[TapeSpec] = Field(
        default_factory=list, description="支撑带位置（绕带式必填）"
    )
    max_segment_length_mm: float = Field(
        default=500.0, gt=0, description="单段最大线长 mm（超限计为违规）"
    )
    signatures: list[SignatureOverride] = Field(
        default_factory=list, description="逐帖标记（禁打区/破损孔/锁定孔）"
    )

    @model_validator(mode="after")
    def _check(self) -> "SewingParams":
        seen = set()
        for o in self.signatures:
            if o.index in seen:
                raise ValueError(f"帖 {o.index} 的标记重复")
            seen.add(o.index)
        return self


class SaveSewingPlanRequest(SewingParams):
    """保存请求：锁线参数 + 选定候选序号 + 可选名称。"""

    candidate_index: int = Field(default=0, ge=0, description="选定候选序号")
    name: Optional[str] = Field(default=None, description="方案名称")

    def to_params(self) -> SewingParams:
        return SewingParams(**self.model_dump())
