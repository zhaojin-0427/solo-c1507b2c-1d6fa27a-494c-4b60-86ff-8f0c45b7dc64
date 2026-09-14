"""Pydantic 接口模型：拼版任务的输入与输出。

单位约定：所有长度均为毫米(mm)，坐标原点为纸张左上角，x 向右，y 向下。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class BindingEdge(str, Enum):
    """装订边（成品书脊所在边）。"""

    left = "left"
    right = "right"
    top = "top"
    bottom = "bottom"


class GrainDirection(str, Enum):
    """纸纹方向（相对于纸张：平行于宽度 / 平行于高度）。"""

    horizontal = "horizontal"  # 纸纹平行于纸张宽度（横丝缕）
    vertical = "vertical"  # 纸纹平行于纸张高度（竖丝缕）


class FoldStyle(str, Enum):
    """折法。8 页仅 standard；12 页为 roll(卷折)/z(风琴折)；16/32 页为 standard/cross。"""

    standard = "standard"
    cross = "cross"
    roll = "roll"
    z = "z"


class GripperEdge(str, Enum):
    """咬口所在纸边。"""

    top = "top"
    bottom = "bottom"
    left = "left"
    right = "right"


# ---------------------------------------------------------------------------
# 基础结构
# ---------------------------------------------------------------------------


class PageSize(BaseModel):
    """成品页（裁切后）尺寸。"""

    width: float = Field(gt=0, description="成品页宽 mm")
    height: float = Field(gt=0, description="成品页高 mm")


class Rect(BaseModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class Paper(BaseModel):
    """纸张：开本、厚度（用于爬移）与纸纹方向。"""

    width: float = Field(gt=0, description="纸张宽 mm")
    height: float = Field(gt=0, description="纸张高 mm")
    thickness: float = Field(default=0.1, gt=0, description="纸厚 mm，用于爬移计算")
    grain: GrainDirection = Field(description="纸纹方向")


class Press(BaseModel):
    """印刷机：可用印刷区域与咬口。printable 缺省时为整张纸。"""

    printable: Optional[Rect] = Field(
        default=None, description="可印区域（缺省 = 整张纸）"
    )
    gripper_mm: float = Field(default=10.0, ge=0, description="咬口宽度 mm")
    gripper_edge: GripperEdge = Field(
        default=GripperEdge.bottom, description="咬口所在纸边"
    )


class SignatureSpec(BaseModel):
    """折帖规格：页数(8/12/16/32)、折法、每帖张数。

    每帖张数 sheets > 1 时表示一帖由多张纸套帖而成（如 32 页帖 = 2 张 16 页），
    每张纸的页数必须为 8/12/16/32 之一。
    """

    pages: int = Field(description="每帖页数：8/12/16/32")
    style: FoldStyle = Field(default=FoldStyle.standard, description="折法")
    sheets: int = Field(default=1, ge=1, description="每帖张数")

    @model_validator(mode="after")
    def _check(self) -> "SignatureSpec":
        from .folding import GRIDS, STYLES

        if self.pages not in GRIDS:
            raise ValueError(f"不支持的折帖页数: {self.pages}（仅支持 8/12/16/32）")
        if self.pages % self.sheets != 0:
            raise ValueError("每帖页数必须能被每帖张数整除")
        per_sheet = self.pages // self.sheets
        if per_sheet not in GRIDS:
            raise ValueError(
                f"每张纸 {per_sheet} 页不受支持（每张须为 8/12/16/32 页）"
            )
        if self.style.value not in STYLES[per_sheet]:
            raise ValueError(
                f"{per_sheet} 页/张不支持折法 {self.style.value}，"
                f"可选: {sorted(STYLES[per_sheet])}"
            )
        return self


class CollatingMarksConfig(BaseModel):
    """书脊配帖标（阶梯标）配置。

    每帖一枚黑色小标，印在折好书芯的书脊边上，逐帖沿书脊错开形成阶梯，
    供配帖时核对帖序。标记宽为垂直书脊方向（自书脊边向书芯内延伸），
    高为沿书脊方向；超出每列帖数后另起一列（向书芯内错开列间距）。
    安全余量 = 书脊两端（天头/地脚侧）留白 + 与套准标记的最小净距。
    """

    mark_width_mm: float = Field(gt=0, description="标记宽 mm（垂直书脊方向）")
    mark_height_mm: float = Field(gt=0, description="标记高 mm（沿书脊方向）")
    start_offset_mm: float = Field(
        default=10.0, ge=0, description="首枚标记沿书脊距书脊起点的偏移 mm"
    )
    step_mm: float = Field(gt=0, description="同列相邻标记沿书脊的步距 mm")
    per_column: int = Field(ge=1, description="每列帖数（超出后另起一列）")
    column_spacing_mm: float = Field(
        default=4.0, gt=0, description="列间距 mm（垂直书脊方向）"
    )
    safety_mm: float = Field(
        default=3.0, ge=0, description="安全余量 mm（书脊两端留白与套准标记净距）"
    )


class JobInput(BaseModel):
    """拼版任务输入。"""

    job_name: str = Field(default="未命名", description="任务名称")
    page: PageSize = Field(description="成品页尺寸")
    total_pages: int = Field(ge=1, description="总页数")
    paper: Paper
    press: Press = Field(default_factory=Press)
    binding: BindingEdge = Field(default=BindingEdge.left, description="装订边")
    bleed_mm: float = Field(default=3.0, ge=0, description="出血 mm")
    safety_mm: float = Field(default=3.0, ge=0, description="安全边界 mm")
    marks_margin_mm: float = Field(
        default=5.0, ge=0, description="套准标记所需出血外余量 mm"
    )
    signature_options: list[SignatureSpec] = Field(
        min_length=1, description="允许使用的折帖规格（可混合）"
    )
    locked_signatures: list[SignatureSpec] = Field(
        default_factory=list, description="已确认锁定的折帖（书首起的前缀）"
    )
    collating_marks: Optional[CollatingMarksConfig] = Field(
        default=None, description="书脊配帖标配置（缺省不生成，行为与旧版一致）"
    )

    @model_validator(mode="after")
    def _check(self) -> "JobInput":
        seen = set()
        for s in self.signature_options:
            key = (s.pages, s.style.value, s.sheets)
            if key in seen:
                raise ValueError(f"折帖规格重复: {key}")
            seen.add(key)
        locked_pages = sum(s.pages for s in self.locked_signatures)
        if locked_pages > self.total_pages:
            raise ValueError(
                f"锁定折帖共 {locked_pages} 页，超过总页数 {self.total_pages}"
            )
        return self


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ComputeRequest(BaseModel):
    """计算请求：按候选序号或显式折帖序列计算完整方案。"""

    job: JobInput
    candidate_index: Optional[int] = Field(
        default=None, description="候选方案序号（与 signatures 二选一）"
    )
    signatures: Optional[list[SignatureSpec]] = Field(
        default=None, description="显式折帖序列（含锁定帖在内）"
    )

    @model_validator(mode="after")
    def _check(self) -> "ComputeRequest":
        if self.candidate_index is not None and self.signatures is not None:
            raise ValueError("candidate_index 与 signatures 只能提供一个")
        return self


class SavePlanRequest(ComputeRequest):
    """保存请求：在计算请求基础上加可选名称。"""

    name: Optional[str] = Field(default=None, description="方案名称")
