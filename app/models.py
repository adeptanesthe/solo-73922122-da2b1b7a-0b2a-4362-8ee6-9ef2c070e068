"""Request/response schemas for the review-ranking API."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

MAX_PANELS = 50_000
MAX_SCORE = 255


class PanelRegistration(BaseModel):
    """A panel to register.

    ``inspection_result`` is the optional actual inspection outcome (0/1).
    Boards without it are pending and can later be filled in by panel id.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    panel_id: int
    score: int = Field(ge=0, le=MAX_SCORE)
    loss: int = Field(gt=0)
    inspection_result: Optional[int] = Field(default=None, ge=0, le=1)


class PanelBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    panels: List[PanelRegistration] = Field(min_length=1, max_length=MAX_PANELS)


class InspectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    inspection_result: int = Field(ge=0, le=1)


class RegistrationSummary(BaseModel):
    registered: int
    total_panels: int
    inspected_panels: int


class InspectionSummary(BaseModel):
    panel_id: int
    inspection_result: int
    calibration_version: int
    inspected_panels: int
    pending_panels: int


class QueueItem(BaseModel):
    panel_id: int
    score: int
    defect_rate: float
    expected_loss: float


class QueueResponse(BaseModel):
    k: int
    calibration_version: int
    inspected_panels: int
    pending_panels: int
    items: List[QueueItem]
