"""Tests for the panel review-ranking service."""

import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.calibration import build_calibration, fit_isotonic
from app.main import app, store
from app.store import MAX_PANELS, PanelStore, ValidationError
from tests.reference import (
    brute_force_isotonic,
    reference_queue,
    reference_rates,
)


def fresh_client() -> TestClient:
    store.__init__()  # type: ignore[misc]
    return TestClient(app)


# ------------------------------------------------------------------ calibration
def test_isotonic_pools_adjacent_violators():
    counts = {10: (1, 3), 20: (3, 3), 30: (0, 3)}  # means 1/3, 1, 0
    rates = fit_isotonic(counts)
    assert rates[10] == Fraction(1, 3)
    # 20 and 30 violate monotonicity and must be pooled: (3+0)/(3+3) = 1/2.
    assert rates[20] == Fraction(1, 2)
    assert rates[30] == Fraction(1, 2)


def test_isotonic_matches_convex_minorant_reference_random():
    rng = random.Random(20240517)
    for _ in range(400):
        scores = rng.sample(range(256), rng.randint(1, 8))
        counts = {s: (rng.randint(0, 5), rng.randint(1, 5)) for s in scores}
        counts = {s: (min(p, t), t) for s, (p, t) in counts.items()}
        assert fit_isotonic(counts) == brute_force_isotonic(counts)


def test_isotonic_minimises_squared_error_on_boards():
    # Exhaustively confirm PAVA reaches <= any other non-decreasing candidate.
    counts = {0: (1, 3), 1: (3, 3), 2: (0, 3), 3: (2, 3)}
    fitted = fit_isotonic(counts)

    def sse(rate_of):
        total = Fraction(0)
        for score, (pos, tot) in counts.items():
            rate = rate_of[score]
            total += pos * (1 - rate) ** 2 + (tot - pos) * rate**2
        return total

    best = sse(fitted)
    grid = [Fraction(n, 12) for n in range(13)]
    candidates = list(grid)
    for a in candidates:
        for b in candidates:
            if b < a:
                continue
            for c in candidates:
                if c < b:
                    continue
                for d in candidates:
                    if d < c:
                        continue
                    assert best <= sse({0: a, 1: b, 2: c, 3: d})


def test_fallback_left_then_lowest():
    fitted = build_calibration({10: (1, 4), 50: (2, 4)}, version=1)
    # Scores below the lowest inspected use the lowest inspected rate.
    for score in range(0, 10):
        assert fitted.effective_rates[score] == Fraction(1, 4)
        assert fitted.effective_sources[score] == "lowest"
    # Scores between inspected scores borrow the one on the left.
    for score in range(10, 50):
        assert fitted.effective_rates[score] == Fraction(1, 4)
        expected_source = "fitted" if score == 10 else "left"
        assert fitted.effective_sources[score] == expected_source
    # Scores above all inspections borrow the highest (also the left value).
    assert fitted.effective_rates[255] == Fraction(1, 2)


def test_single_inspected_band_applies_everywhere():
    fitted = build_calibration({77: (1, 2)}, version=1)
    assert len(fitted.effective_rates) == 256
    assert all(r == Fraction(1, 2) for r in fitted.effective_rates)
    assert fitted.effective_sources[0] == "lowest"
    assert fitted.effective_sources[77] == "fitted"
    assert fitted.effective_sources[255] == "left"


# ------------------------------------------------------------------------ queue
def test_queue_matches_brute_force_random():
    rng = random.Random(7)
    for trial in range(60):
        s = PanelStore()
        count = rng.randint(5, 60)
        ids = rng.sample(range(1, 100_000), count)
        entries = []
        for pid in ids:
            score = rng.choice([0, 5, 5, 10, 40, 200, 255])
            loss = rng.randint(1, 50)
            result = rng.choice([None, None, None, 0, 1])
            entries.append((pid, score, loss, result))
        s.register(entries)
        if s.calibration is None:
            continue
        panels = [
            (p.panel_id, p.score, p.loss, p.inspected) for p in s._panels.values()
        ]
        rates = reference_rates(
            {score: (b[0], b[1]) for score, b in s._counts.items()}
        )
        for k in (1, 3, 7, 20):
            got = [(pid, r, e) for pid, r, e in s.top_k(k)]
            want = reference_queue(panels, rates, k)
            assert got == want, (trial, k, got, want)


def test_queue_orders_by_expected_loss():
    s = PanelStore()
    # Score 10 rate 1/2 (one defect, one clean); score 20 shares the fitted
    # rate via monotonicity.  Expected losses: 9->50, 3->50, 7->25.
    s.register(
        [
            (9, 10, 100, None),
            (3, 10, 100, None),
            (7, 20, 50, None),
            (1, 10, 5, 0),
            (2, 10, 5, 1),
        ]
    )
    assert s.calibration.effective_rates[10] == Fraction(1, 2)
    assert s.calibration.effective_rates[20] == Fraction(1, 2)
    assert [(r[0], r[2]) for r in s.top_k(3)] == [
        (3, Fraction(50)),
        (9, Fraction(50)),
        (7, Fraction(25)),
    ]


def test_queue_ties_prefer_smaller_panel_id():
    s = PanelStore()
    # Both scores fit to rate 1, so both candidates expect 100; ids 3 < 9.
    s.register(
        [
            (9, 10, 100, None),
            (3, 20, 100, None),
            (1, 10, 1, 1),
            (2, 20, 1, 1),
        ]
    )
    assert s.calibration.effective_rates[10] == 1
    assert s.calibration.effective_rates[20] == 1
    assert [r[0] for r in s.top_k(2)] == [3, 9]
    assert [r[2] for r in s.top_k(2)] == [Fraction(100), Fraction(100)]


def test_queue_zero_rate_ties_by_panel_id():
    s = PanelStore()
    # A zero fitted rate makes every candidate's expected loss 0, so the queue
    # must fall back to ascending panel id -- even against a zero-rate board
    # with a much larger loss.
    s.register(
        [
            (500, 10, 10_000, None),
            (7, 20, 50_000, None),
            (9, 10, 1, None),
            (1, 10, 5, 0),
            (2, 20, 5, 0),
        ]
    )
    assert s.calibration.effective_rates[10] == 0
    assert s.calibration.effective_rates[20] == 0
    assert [(r[0], r[2]) for r in s.top_k(3)] == [
        (7, Fraction(0)),
        (9, Fraction(0)),
        (500, Fraction(0)),
    ]


def test_queue_excludes_inspected_boards():
    s = PanelStore()
    s.register([(1, 10, 100, None), (2, 10, 100, 1), (3, 10, 1, 0)])
    assert [r[0] for r in s.top_k(5)] == [1]


def test_queue_repeatable():
    s = PanelStore()
    s.register([(i, i % 4, 100 - i, None) for i in range(1, 30)] + [(500, 0, 1, 1)])
    first = s.top_k(10)
    assert s.top_k(10) == first
    assert s.top_k(10) == first


def test_calibration_fit_after_initial_register_without_inspections():
    s = PanelStore()
    s.register([(i, 10, i, None) for i in range(1, 5)])
    assert s.calibration is None
    s.set_inspection(2, 1)
    assert s.calibration is not None
    assert s.calibration.inspected_panels == 1


# --------------------------------------------------------- invalidation/atomicity
def test_invalid_update_keeps_previous_queue():
    s = PanelStore()
    s.register([(1, 10, 100, None), (2, 20, 100, None), (3, 10, 1, 0), (4, 20, 1, 1)])
    before = s.top_k(5)
    version = s.calibration.version

    with pytest.raises(ValidationError):
        s.set_inspection(1, 7)
    assert s.top_k(5) == before
    assert s.calibration.version == version

    with pytest.raises(LookupError):
        s.set_inspection(999, 1)
    assert s.top_k(5) == before
    assert s.calibration.version == version


def test_invalid_registration_is_atomic():
    s = PanelStore()
    s.register([(1, 10, 100, 0)])
    with pytest.raises(ValidationError):
        s.register([(2, 10, 100, None), (2, 11, 1, None)])  # duplicate in batch
    with pytest.raises(ValidationError):
        s.register([(3, 10, 100, None), (1, 11, 1, None)])  # duplicate existing
    with pytest.raises(ValidationError):
        s.register([(3, 300, 1, None)])  # score out of range
    with pytest.raises(ValidationError):
        s.register([(3, 10, 0, None)])  # loss must be positive
    assert s.total_panels == 1
    assert s.get_panel(1).loss == 100


def test_registration_cap():
    s = PanelStore()
    s.register([(i, 1, 1, None) for i in range(MAX_PANELS)])
    with pytest.raises(ValidationError):
        s.register([(MAX_PANELS + 1, 1, 1, None)])
    assert s.total_panels == MAX_PANELS


def test_correction_triggers_refit_and_reorders():
    # Score 10 is clean (0/1), score 90 defective (1/1).  The score-90 board
    # has a modest loss; the score-10 board a huge one.  Raising the score-10
    # rate by correcting its inspection (while staying <= score 90's rate, as
    # isotonic monotonicity requires) flips which pending board leads.
    s = PanelStore()
    s.register(
        [
            (1, 10, 1_000_000, None),
            (2, 90, 1000, None),
            (10, 10, 1, 0),
            (11, 10, 1, 0),
            (14, 90, 1, 1),
        ]
    )
    assert s.calibration.effective_rates[10] == 0
    assert s.calibration.effective_rates[90] == 1
    assert [r[0] for r in s.top_k(1)] == [2]

    # Correct one of the two clean score-10 inspections to a defect: the rate
    # goes 0/2 -> 1/2, still below score 90's 1/1, and the huge-loss board 1
    # now leads.
    assert s.set_inspection(10, 1) is True
    assert s.calibration.effective_rates[10] == Fraction(1, 2)
    assert [r[0] for r in s.top_k(1)] == [1]

    # Filling a pending board removes it from the queue.
    assert s.set_inspection(1, 0) is True
    assert [r[0] for r in s.top_k(5)] == [2]


def test_noop_correction_does_not_bump_version():
    s = PanelStore()
    s.register([(1, 10, 100, None), (2, 10, 1, 1)])
    version = s.calibration.version
    assert s.set_inspection(2, 1) is False
    assert s.calibration.version == version


# ----------------------------------------------------------- no-pending-scan proof
def test_selection_does_not_scan_pending_population():
    s = PanelStore()
    # 20k pending boards spread over 256 scores, plus a few inspections.
    s.register([(i, i % 256, (i * 7919) % 1000 + 1, None) for i in range(20_000)])
    s.register([(100_000 + i, i % 256, 1, i % 2) for i in range(300)])
    s.reset_stats()
    s.top_k(20)
    # A merge visits at most one heap top per score plus K popped entries (and
    # any lazily-deleted tops), nowhere near the pending population.
    touched = s.stats["heap_entries_popped"] + s.stats["heap_entries_dropped"]
    assert touched <= 20 + 256
    assert s.pending_panels > 19_000


def test_inspection_update_cost_independent_of_pending_population():
    large = PanelStore()
    large.register([(i, i % 256, i + 1, None) for i in range(20_000)])
    large.register([(500_000, 10, 1, 0)])
    large.reset_stats()
    # Filling/correcting an inspection touches only the counts, never the
    # pending heaps, regardless of how many pending boards exist.
    assert large.set_inspection(7, 1) is True
    assert large.set_inspection(500_000, 1) is True
    assert large.stats == {"heap_entries_dropped": 0, "heap_entries_popped": 0}
    assert large.calibration.inspected_panels == 2


# -------------------------------------------------------------------------- API
def test_api_missing_calibration_message():
    client = fresh_client()
    client.post("/panels", json={"panels": [{"panel_id": 1, "score": 5, "loss": 10}]})
    response = client.get("/queue?k=3")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "calibration_missing"
    assert "inspection" in body["detail"]


def test_api_register_validate_and_queue():
    client = fresh_client()
    response = client.post(
        "/panels",
        json={
            "panels": [
                {"panel_id": 1, "score": 10, "loss": 500},
                {"panel_id": 2, "score": 10, "loss": 500},
                {"panel_id": 3, "score": 200, "loss": 50},
                {"panel_id": 4, "score": 10, "loss": 5, "inspection_result": 1},
            ]
        },
    )
    assert response.status_code == 201
    assert response.json()["inspected_panels"] == 1
    queue = client.get("/queue?k=2").json()
    assert [item["panel_id"] for item in queue["items"]] == [1, 2]
    assert queue["items"][0]["defect_rate"] == 1.0
    assert queue["items"][0]["expected_loss"] == 500.0


def test_api_invalid_inputs_are_rejected():
    client = fresh_client()
    client.post("/panels", json={"panels": [{"panel_id": 1, "score": 5, "loss": 10, "inspection_result": 1}]})
    bad_bodies = [
        {"panels": [{"panel_id": 1, "score": 5, "loss": 10}]},  # duplicate id
        {"panels": [{"panel_id": 2, "score": 256, "loss": 10}]},
        {"panels": [{"panel_id": 2, "score": -1, "loss": 10}]},
        {"panels": [{"panel_id": 2, "score": 5, "loss": 0}]},
        {"panels": [{"panel_id": 2, "score": 5, "loss": 10, "inspection_result": 2}]},
        {"panels": [{"panel_id": 2, "score": 5.5, "loss": 10}]},
        {"panels": [{"panel_id": "2", "score": 5, "loss": 10}]},
        {"panels": [{"panel_id": 2, "score": 5, "loss": 10, "extra": 1}]},
        {"panels": []},
    ]
    for body in bad_bodies:
        assert client.post("/panels", json=body).status_code in (400, 422), body

    for k in (0, 21):
        assert client.get(f"/queue?k={k}").status_code == 422

    # Validation failures must not have registered anything.
    assert client.get("/health").json()["total_panels"] == 1


def test_api_correction_changes_queue_and_invalid_keeps_it():
    client = fresh_client()
    client.post(
        "/panels",
        json={
            "panels": [
                {"panel_id": 1, "score": 10, "loss": 1000},
                {"panel_id": 2, "score": 90, "loss": 1000},
                {"panel_id": 10, "score": 10, "loss": 1, "inspection_result": 0},
                {"panel_id": 11, "score": 10, "loss": 1, "inspection_result": 0},
                {"panel_id": 12, "score": 90, "loss": 1, "inspection_result": 1},
                {"panel_id": 13, "score": 90, "loss": 1, "inspection_result": 1},
            ]
        },
    )
    first = client.get("/queue?k=1").json()
    assert first["items"][0]["panel_id"] == 2

    invalid = client.put("/panels/11/inspection", json={"inspection_result": 5})
    assert invalid.status_code == 422
    assert client.get("/queue?k=1").json() == first

    for panel_id, result in ((12, 0), (13, 0), (10, 1), (11, 1)):
        assert (
            client.put(
                f"/panels/{panel_id}/inspection", json={"inspection_result": result}
            ).status_code
            == 200
        )
    second = client.get("/queue?k=1").json()
    assert second["items"][0]["panel_id"] == 1
    assert second["calibration_version"] > first["calibration_version"]


def test_api_unknown_panel_is_404():
    client = fresh_client()
    assert client.put("/panels/42/inspection", json={"inspection_result": 1}).status_code == 404


# --------------------------------------------------- differential stress test
def test_differential_random_mutations_vs_brute_force():
    """Interleave registrations and corrections, checking the queue against a
    full scan after every mutation, including duplicate expectations."""
    rng = random.Random(99)
    s = PanelStore()
    known = []  # (panel_id, score, loss, inspected)
    next_id = 1
    for step in range(220):
        action = rng.random()
        if action < 0.45 or len(known) < 4:
            batch = []
            for _ in range(rng.randint(1, 6)):
                pid = next_id
                next_id += 1
                score = rng.choice([0, 1, 5, 5, 10, 77, 200, 255])
                loss = rng.choice([1, 2, 5, 5, 10, 100])
                result = rng.choice([None, None, None, 0, 1])
                batch.append((pid, score, loss, result))
                known.append([pid, score, loss, -1 if result is None else result])
            s.register(batch)
        else:
            index = rng.randrange(len(known))
            pid, score, loss, inspected = known[index]
            result = rng.choice([0, 1])
            changed = s.set_inspection(pid, result)
            if inspected != -1 and inspected == result:
                assert changed is False
            known[index][3] = result
        if s.calibration is None:
            continue
        panels = [(pid, score, loss, insp) for pid, score, loss, insp in known]
        rates = reference_rates(
            {score: (b[0], b[1]) for score, b in s._counts.items()}
        )
        for k in (1, 4, 20):
            got = s.top_k(k)
            want = reference_queue(panels, rates, k)
            assert got == want, (step, k, got, want)


# ------------------------------------------------------- weighted SSE oracle
def test_isotonic_minimises_weighted_sse():
    """Same 4-score setup, but with unequal group sizes, compared against the
    weighted min-max oracle (each board contributes one squared error)."""
    counts = {0: (1, 1), 1: (0, 1), 2: (0, 3), 3: (2, 2)}
    fitted = fit_isotonic(counts)
    assert fitted == brute_force_isotonic(counts)

    def sse(rate_of):
        total = Fraction(0)
        for score, (pos, tot) in counts.items():
            rate = rate_of[score]
            total += pos * (1 - rate) ** 2 + (tot - pos) * rate**2
        return total

    best = sse(fitted)
    grid = [Fraction(n, 6) for n in range(7)]
    for a in grid:
        for b in grid:
            if b < a:
                continue
            for c in grid:
                if c < b:
                    continue
                for d in grid:
                    if d < c:
                        continue
                    assert best <= sse({0: a, 1: b, 2: c, 3: d})


def test_concurrent_readers_and_writers_stay_consistent():
    import threading

    s = PanelStore()
    s.register([(i, i % 256, i + 1, None) for i in range(2000)])
    s.register([(10_000 + i, i % 256, 5, i % 2) for i in range(20)])

    errors = []

    def writer(writer_id):
        try:
            for step in range(200):
                pid = 10_000 + (writer_id * 200 + step) % 20
                s.set_inspection(pid, (step + writer_id) % 2)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader():
        try:
            for _ in range(200):
                rows = s.top_k(20)
                assert all(isinstance(e, Fraction) for _, _, e in rows)
                assert rows == sorted(rows, key=lambda r: (-r[2], r[0]))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_pending_only_registration_does_not_bump_calibration():
    s = PanelStore()
    s.register([(1, 10, 5, 1)])
    version = s.calibration.version
    s.register([(2, 10, 5, None), (3, 20, 5, None)])
    assert s.calibration.version == version
    s.register([(4, 20, 5, 0)])
    assert s.calibration.version == version + 1
