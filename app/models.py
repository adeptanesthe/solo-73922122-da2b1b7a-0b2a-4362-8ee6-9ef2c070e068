"""Request/response schemas for the review-ranking API."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PANELS = 50_000
MAX_SCORE = 255
MAX_AUDIT_CHECKS = 12
MAX_CAP_PERCENT = 100


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


class CapCheck(BaseModel):
    """One tier of the quality report: a score and its defect-rate cap."""

    model_config = ConfigDict(extra="forbid", strict=True)

    score: int = Field(ge=0, le=MAX_SCORE)
    cap_percent: int = Field(ge=0, le=MAX_CAP_PERCENT)


class AuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    checks: List[CapCheck] = Field(min_length=1, max_length=MAX_AUDIT_CHECKS)
    # Version the caller's report was built on.  When the live calibration has
    # moved on, the response flags the result as needing a re-check.
    calibration_version: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _scores_must_be_distinct(self) -> "AuditRequest":
        scores = [check.score for check in self.checks]
        if len(set(scores)) != len(scores):
            raise ValueError("checks must use distinct defect scores")
        return self


class ResolvedCap(BaseModel):
    score: int
    cap_percent: int
    component_score: int
    source: str


class AuditResponse(BaseModel):
    probability: float
    calibration_version: int
    recheck_required: bool
    warnings: List[str]
    inspected_scores: List[int]
    resolved_checks: List[ResolvedCap]
