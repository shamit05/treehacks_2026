# app/schemas/step_plan.py
# Owner: Eng 3 (Agent Server)
#
# Pydantic models matching shared/step_plan_schema.json.
# This is the server-side source of truth for plan validation.
# Keep in sync with the JSON schema and Swift Codable models.

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class AdvanceType(str, Enum):
    click_in_target = "click_in_target"
    text_entered_or_next = "text_entered_or_next"
    manual_next = "manual_next"
    wait_for_ui_change = "wait_for_ui_change"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class TargetRect(BaseModel):
    """A normalized rectangle identifying a UI target on screen."""

    x: float = Field(..., ge=0.0, le=1.0, description="Normalized x (top-left origin)")
    y: float = Field(..., ge=0.0, le=1.0, description="Normalized y (top-left origin)")
    w: float = Field(..., gt=0.0, le=1.0, description="Normalized width")
    h: float = Field(..., gt=0.0, le=1.0, description="Normalized height")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    label: Optional[str] = None

    model_config = {"extra": "forbid"}


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


class Step(BaseModel):
    """A single guidance step with targets and advancement rules."""

    id: str = Field(..., min_length=1)
    instruction: str = Field(..., min_length=1)
    targets: list[TargetRect] = Field(..., min_length=1, max_length=5)
    advance: Advance
    safety: Optional[Safety] = None

    model_config = {"extra": "forbid"}


class AppContext(BaseModel):
    """Optional context about the frontmost application."""

    app_name: str
    bundle_id: Optional[str] = None
    window_title: Optional[str] = None

    model_config = {"extra": "forbid"}


class ImageSize(BaseModel):
    """Dimensions of the captured screenshot."""

    w: int = Field(..., ge=1)
    h: int = Field(..., ge=1)

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
