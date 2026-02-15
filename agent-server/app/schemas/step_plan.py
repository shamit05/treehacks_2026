# app/schemas/step_plan.py
# Owner: Eng 3 (Agent Server)
#
# Pydantic models matching shared/step_plan_schema.json.
# This is the server-side source of truth for plan validation.
# Keep in sync with the JSON schema and Swift Codable models.

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AdvanceType(str, Enum):
    click_in_target = "click_in_target"
    text_entered_or_next = "text_entered_or_next"
    manual_next = "manual_next"
    wait_for_ui_change = "wait_for_ui_change"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


<<<<<<< Current (Your changes)
class TargetType(str, Enum):
    som_marker = "som_marker"
    bbox_norm = "bbox_norm"
=======
# ---------------------------------------------------------------------------
# Shared / leaf models (no forward references)
# ---------------------------------------------------------------------------


class ImageSize(BaseModel):
    """Dimensions of the captured screenshot."""

    w: int = Field(..., ge=1)
    h: int = Field(..., ge=1)

    model_config = {"extra": "forbid"}
>>>>>>> Incoming (Background Agent changes)


class TargetRect(BaseModel):
    """A target represented either by a SoM marker id or normalized bbox."""

    type: TargetType
    marker_id: Optional[int] = Field(None, ge=0, description="Marker id when type=som_marker")
    x: Optional[float] = Field(None, ge=0.0, le=1.0, description="Normalized x (top-left origin)")
    y: Optional[float] = Field(None, ge=0.0, le=1.0, description="Normalized y (top-left origin)")
    w: Optional[float] = Field(None, gt=0.0, le=1.0, description="Normalized width")
    h: Optional[float] = Field(None, gt=0.0, le=1.0, description="Normalized height")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    label: Optional[str] = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_target_shape(self) -> "TargetRect":
        if self.type == TargetType.som_marker:
            if self.marker_id is None:
                raise ValueError("marker_id is required when type='som_marker'")
            return self

        if None in (self.x, self.y, self.w, self.h):
            raise ValueError("x, y, w, h are required when type='bbox_norm'")

        if self.x + self.w > 1.0 or self.y + self.h > 1.0:
            raise ValueError("bbox_norm rect must satisfy x+w<=1 and y+h<=1")
        return self


class BBoxNorm(BaseModel):
    """Normalized bbox using top-left origin in [0,1]."""

    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    w: float = Field(..., gt=0.0, le=1.0)
    h: float = Field(..., gt=0.0, le=1.0)
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    label: Optional[str] = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_bounds(self) -> "BBoxNorm":
        if self.x + self.w > 1.0 or self.y + self.h > 1.0:
            raise ValueError("bbox rect must satisfy x+w<=1 and y+h<=1")
        return self


class CropRectNorm(BaseModel):
    """Crop rectangle in full-image normalized coordinates."""

    cx: float = Field(..., ge=0.0, le=1.0)
    cy: float = Field(..., ge=0.0, le=1.0)
    cw: float = Field(..., gt=0.0, le=1.0)
    ch: float = Field(..., gt=0.0, le=1.0)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_bounds(self) -> "CropRectNorm":
        if self.cx + self.cw > 1.0 or self.cy + self.ch > 1.0:
            raise ValueError("crop rect must satisfy cx+cw<=1 and cy+ch<=1")
        return self


class Advance(BaseModel):
    """How the step advances to the next one."""

    type: AdvanceType
    notes: Optional[str] = None

    model_config = {"extra": "forbid"}


class Safety(BaseModel):
    """Optional safety metadata for a step."""

    requires_confirmation: Optional[bool] = None
    risk_level: Optional[RiskLevel] = None

    model_config = {"extra": "forbid"}


class AppContext(BaseModel):
    """Optional context about the frontmost application."""

    app_name: str
    bundle_id: Optional[str] = None
    window_title: Optional[str] = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# SoM (Set-of-Mark) models — used by the SoM + Zoom + Stitch pipeline
# ---------------------------------------------------------------------------


class SoMMarker(BaseModel):
    """A numbered marker overlaid on the screenshot grid."""

    id: int = Field(..., ge=0, description="Marker ID (grid index)")
    cx: float = Field(..., ge=0.0, le=1.0, description="Normalized center x")
    cy: float = Field(..., ge=0.0, le=1.0, description="Normalized center y")
    radius: float = Field(..., gt=0.0, le=1.0, description="Normalized marker radius")

    model_config = {"extra": "forbid"}


class SoMTarget(BaseModel):
    """Model output: which marker(s) to target for a step."""

    marker_id: int = Field(..., ge=0, description="Selected marker ID")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    label: Optional[str] = None

    model_config = {"extra": "forbid"}


class CropRect(BaseModel):
    """A crop rectangle in full-image normalized coordinates."""

    cx: float = Field(..., ge=0.0, le=1.0, description="Crop top-left x")
    cy: float = Field(..., ge=0.0, le=1.0, description="Crop top-left y")
    cw: float = Field(..., gt=0.0, le=1.0, description="Crop width")
    ch: float = Field(..., gt=0.0, le=1.0, description="Crop height")

    model_config = {"extra": "forbid"}


class RefineResponse(BaseModel):
    """Bbox returned by the /refine endpoint, in crop-normalized coords."""

    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    w: float = Field(..., gt=0.0, le=1.0)
    h: float = Field(..., gt=0.0, le=1.0)
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    label: Optional[str] = None

    model_config = {"extra": "forbid"}


class SoMStep(BaseModel):
    """A step as returned by the SoM planner (marker IDs, not raw coords)."""

    id: str = Field(..., min_length=1)
    instruction: str = Field(..., min_length=1)
    som_targets: list[SoMTarget] = Field(..., min_length=1)
    advance: Advance
    safety: Optional[Safety] = None

    model_config = {"extra": "forbid"}


class SoMStepPlan(BaseModel):
    """Plan returned by the SoM planner — steps reference marker IDs."""

    version: str = Field(..., pattern=r"^v\d+$")
    goal: str = Field(..., min_length=1)
    image_size: ImageSize
    steps: list[SoMStep] = Field(..., min_length=1, max_length=10)

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Standard Step / StepPlan models (original API response format)
# ---------------------------------------------------------------------------


class Step(BaseModel):
    """A single guidance step with targets and advancement rules."""

    id: str = Field(..., min_length=1)
    instruction: str = Field(..., min_length=1)
    targets: list[TargetRect] = Field(..., min_length=1, max_length=5)
    advance: Advance
    safety: Optional[Safety] = None

    model_config = {"extra": "forbid"}


class StepPlan(BaseModel):
    """
    The complete step plan returned by the agent.
    Must match shared/step_plan_schema.json exactly.
    """

    version: str = Field(..., pattern=r"^v\d+$")
    goal: str = Field(..., min_length=1)
    app_context: Optional[AppContext] = None
    image_size: ImageSize
    steps: list[Step] = Field(..., min_length=1, max_length=10)

    model_config = {"extra": "forbid"}
