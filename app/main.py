"""FastAPI application exposing the review-ranking service."""

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .audit import InsufficientEvidence
from .calibration import CalibrationMissing
from .models import (
    AuditRequest,
    AuditResponse,
    InspectionSummary,
    InspectionUpdate,
    PanelBatch,
    QueueItem,
    QueueResponse,
    RegistrationSummary,
    ResolvedCap,
)
from .store import MAX_QUEUE, NotFound, PanelStore, ValidationError

description = """
Rank glass panels for re-inspection by expected missed-defect loss.

* Defect rates are fitted on inspected panels with a non-decreasing
  (isotonic) curve; panels sharing a defect score share one rate.
* Scores with no inspections borrow the nearest inspected score on the left,
  or the lowest inspected score when there is none.
* The queue is the top K pending panels by `defect_rate * loss`, ties broken by
  the smaller panel id.  Both inspection updates and queue selection avoid
  scanning the pending population.
"""

app = FastAPI(title="Panel Re-inspection Ranking", version="1.1.0", description=description)
store = PanelStore()


@app.exception_handler(CalibrationMissing)
async def _missing_calibration(_request: Request, exc: CalibrationMissing) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": "calibration missing: no inspected panels yet; submit at "
            "least one inspection result before requesting a queue",
            "code": "calibration_missing",
        },
    )


@app.exception_handler(InsufficientEvidence)
async def _insufficient_evidence(_request: Request, exc: InsufficientEvidence) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "code": "evidence_insufficient",
        },
    )


@app.exception_handler(NotFound)
async def _not_found(_request: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc), "code": "not_found"})


@app.exception_handler(ValidationError)
async def _invalid(_request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400, content={"detail": str(exc), "code": "invalid_request"}
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", **store.counts_snapshot()}


@app.post("/panels", response_model=RegistrationSummary, status_code=201)
def register_panels(batch: PanelBatch) -> RegistrationSummary:
    """Register a batch of panels (optionally with an inspection result)."""
    entries = [
        (p.panel_id, p.score, p.loss, p.inspection_result) for p in batch.panels
    ]
    count = store.register(entries)
    return RegistrationSummary(
        registered=count,
        total_panels=store.total_panels,
        inspected_panels=store.inspected_panels,
    )


@app.put("/panels/{panel_id}/inspection", response_model=InspectionSummary)
def set_inspection(panel_id: int, update: InspectionUpdate) -> InspectionSummary:
    """Fill in or correct the inspection result of one panel.

    Only inspected panels feed the calibration, so this re-fits it.  An
    invalid request is rejected before any state changes, leaving the previous
    calibration and queue intact.
    """
    store.set_inspection(panel_id, update.inspection_result)
    return InspectionSummary(
        panel_id=panel_id,
        inspection_result=update.inspection_result,
        calibration_version=store.calibration_version,
        inspected_panels=store.inspected_panels,
        pending_panels=store.pending_panels,
    )


@app.get("/queue", response_model=QueueResponse)
def queue(k: int = Query(default=10, ge=1, le=MAX_QUEUE)) -> QueueResponse:
    """Top K pending panels by expected missed-defect loss."""
    snapshot = store.queue_snapshot(k)
    return QueueResponse(
        k=k,
        calibration_version=snapshot["calibration_version"],
        inspected_panels=snapshot["inspected_panels"],
        pending_panels=snapshot["pending_panels"],
        items=[
            QueueItem(
                panel_id=pid,
                score=score,
                defect_rate=float(rate),
                expected_loss=float(expected),
            )
            for pid, score, rate, expected in snapshot["items"]
        ],
    )


@app.get("/calibration")
def calibration() -> dict:
    """Fitted calibration, for auditing the queue."""
    fitted = store.require_calibration()
    return {
        "calibration_version": fitted.version,
        "inspected_panels": fitted.inspected_panels,
        "lowest_inspected_score": fitted.lowest_inspected_score,
        "rates": [
            {
                "score": score,
                "defect_rate": float(rate),
                "source": fitted.effective_sources[score],
            }
            for score, rate in sorted(fitted.fitted_rates.items())
        ],
    }


@app.post("/audit", response_model=AuditResponse)
def audit_caps(request: AuditRequest) -> AuditResponse:
    """Posterior probability that every defect-rate cap holds at once.

    Each check names one distinct defect score (1..12 checks) and an integer
    percentage upper bound.  Boards sharing a score share one rate and rates
    live on the 0..100 integer grid; every non-decreasing rate vector has
    equal prior weight and is updated by board-independent Bernoulli
    likelihoods.  Scores without inspections follow the existing
    left-nearest / lowest-score fallback.

    Pass the ``calibration_version`` of the report you are verifying: if
    inspections have since been added or corrected, ``recheck_required``
    comes back true and a warning explains why.
    """
    checks = [(item.score, item.cap_percent) for item in request.checks]
    snapshot = store.audit_caps(checks, request.calibration_version)
    warnings: list[str] = []
    if snapshot["recheck_required"]:
        warnings.append(
            f"calibration changed: requested version {request.calibration_version} "
            f"is no longer current (live version {snapshot['calibration_version']}); "
            "re-run the check against the new version"
        )
    return AuditResponse(
        probability=snapshot["probability"],
        calibration_version=snapshot["calibration_version"],
        recheck_required=snapshot["recheck_required"],
        warnings=warnings,
        inspected_scores=snapshot["inspected_scores"],
        resolved_checks=[ResolvedCap(**item) for item in snapshot["resolved_checks"]],
    )


@app.get("/panels/{panel_id}")
def panel(panel_id: int) -> dict:
    item = store.get_panel(panel_id)
    return {
        "panel_id": item.panel_id,
        "score": item.score,
        "loss": item.loss,
        "inspection_result": item.inspected if item.is_inspected else None,
        "inspected": item.is_inspected,
    }
