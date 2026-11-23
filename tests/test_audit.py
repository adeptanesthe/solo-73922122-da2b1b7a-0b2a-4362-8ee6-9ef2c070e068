"""Tests for the Bayesian cap-audit feature."""

import random
import time

import pytest
from fastapi.testclient import TestClient

from app.audit import (
    InsufficientEvidence,
    posterior_cap_probability,
    resolve_constraints,
)
from app.main import app, store
from app.store import PanelStore
from tests.reference import brute_force_audit

# Small grids keep the brute-force oracle (O(grid**m) vectors) tractable while
# exercising the exact same DP code paths as the production 0..100 grid.
SMALL_GRID = 12
TOLERANCE = 1e-12


def _random_counts(rng: random.Random, grid: int) -> dict:
    scores = rng.sample(range(grid * 3), rng.randint(1, 4))
    counts = {}
    for score in scores:
        total = rng.randint(1, 6)
        positives = rng.randint(0, total)
        counts[score] = (positives, total)
    return dict(sorted(counts.items()))


def _random_checks(rng: random.Random, score_universe: int) -> list:
    count = rng.randint(1, 5)
    scores = rng.sample(range(score_universe), count)
    return [(score, rng.randint(0, SMALL_GRID)) for score in scores]


# ------------------------------------------------------------- pure math DP
def test_audit_matches_brute_force_random():
    rng = random.Random(20261123)
    for _ in range(120):
        counts = _random_counts(rng, SMALL_GRID)
        checks = _random_checks(rng, SMALL_GRID * 3)
        result = posterior_cap_probability(counts, checks, 1, grid_max=SMALL_GRID)
        expected = float(brute_force_audit(counts, checks, grid_max=SMALL_GRID))
        assert result.probability == pytest.approx(expected, abs=TOLERANCE), (
            counts,
            checks,
            result.probability,
            expected,
        )


def test_audit_on_full_grid_matches_brute_force_small_m():
    rng = random.Random(424242)
    # Full 0..100 grid, at most two components: the oracle enumerates 5,151
    # vectors, which is still fast and validates the production grid size.
    for _ in range(8):
        scores = rng.sample(range(256), rng.randint(1, 2))
        counts = {}
        for score in scores:
            total = rng.randint(1, 8)
            counts[score] = (rng.randint(0, total), total)
        checks = _random_checks(rng, 256)
        checks = [(s, min(c, 100)) for s, c in checks]
        result = posterior_cap_probability(counts, checks, 1)
        expected = float(brute_force_audit(counts, checks))
        assert result.probability == pytest.approx(expected, abs=TOLERANCE)


def test_tightest_cap_wins_for_shared_component():
    # Two queried scores both fall back to the single inspected component.
    counts = {50: (1, 3)}
    result = posterior_cap_probability(
        counts, [(10, 80), (20, 25), (50, 60)], 1
    )
    # All three constrain component 0; the effective cap is min(80,25,60)=25.
    direct = posterior_cap_probability(counts, [(50, 25)], 1)
    assert result.probability == pytest.approx(direct.probability, abs=TOLERANCE)
    assert {c.source for c in result.constraints} == {"lowest", "fitted"}


def test_simultaneous_caps_are_a_joint_event_not_independent_products():
    # Two inspected components with a monotone coupling.  The joint posterior
    # must equal the brute-force intersection, which is not the product of the
    # marginal cap probabilities in general.
    counts = {10: (2, 5), 40: (4, 6)}
    checks = [(10, 4), (40, 9)]
    joint = posterior_cap_probability(counts, checks, 1, grid_max=SMALL_GRID).probability
    marginal_1 = posterior_cap_probability(
        counts, [(10, 4)], 1, grid_max=SMALL_GRID
    ).probability
    marginal_2 = posterior_cap_probability(
        counts, [(40, 9)], 1, grid_max=SMALL_GRID
    ).probability
    expected = float(brute_force_audit(counts, checks, grid_max=SMALL_GRID))
    assert joint == pytest.approx(expected, abs=TOLERANCE)
    # Monotonicity makes the events dependent: a naive product is wrong.
    assert abs(joint - marginal_1 * marginal_2) > 1e-4


def test_impossible_and_certain_caps():
    counts = {30: (3, 3)}  # every inspected board defective
    # Rate 0 is impossible under this evidence, even with a generous grid.
    assert posterior_cap_probability(counts, [(30, 0)], 1).probability == 0.0
    # A cap at 100% is a tautology.
    assert posterior_cap_probability(counts, [(30, 100)], 1).probability == 1.0

    clean = {30: (0, 4)}
    assert posterior_cap_probability(clean, [(30, 100)], 1).probability == 1.0
    # All-clean evidence still leaves positive posterior mass on rates > 0.
    p = posterior_cap_probability(clean, [(30, 0)], 1).probability
    assert 0.0 < p < 1.0


def test_fallback_resolution_matches_calibration_rule():
    inspected = [10, 50, 90]
    caps, resolved = resolve_constraints(
        [(0, 10), (9, 20), (10, 30), (49, 40), (200, 50)], inspected
    )
    by_score = {c.score: c for c in resolved}
    assert by_score[0].source == "lowest" and by_score[0].component_index == 0
    assert by_score[9].source == "lowest" and by_score[9].component_index == 0
    assert by_score[10].source == "fitted" and by_score[10].component_index == 0
    # 49 sits between 10 and 50: the nearest inspected score on the left is 10.
    assert by_score[49].source == "left" and by_score[49].component_index == 0
    assert by_score[200].source == "left" and by_score[200].component_index == 2
    # Component 0 collects caps min(10,20,30,40)=10; component 2 -> 50.
    assert caps == (10, None, 50)


def test_no_evidence_raises():
    with pytest.raises(InsufficientEvidence):
        posterior_cap_probability({}, [(10, 50)], 1)


def test_accuracy_tolerance_on_heavy_data():
    # Many boards create extreme likelihoods (near-underflow); log-space
    # arithmetic must stay within the required 1e-8 absolute error.  With one
    # component the exact oracle only enumerates 101 vectors, so it stays
    # exact (Fraction) even for thousand-board samples.
    heavy_cases = [
        {20: (480, 1000)},
        {20: (999, 1000)},
        {20: (1, 1000)},
        {20: (500, 1000)},
    ]
    for counts in heavy_cases:
        for cap in (0, 25, 50, 75, 100):
            result = posterior_cap_probability(counts, [(20, cap)], 1)
            exact = float(brute_force_audit(counts, [(20, cap)]))
            assert abs(result.probability - exact) < 1e-8, (counts, cap)
            assert 0.0 <= result.probability <= 1.0


# ------------------------------------------------------------- store/API
def fresh_client() -> TestClient:
    store.__init__()  # type: ignore[misc]
    return TestClient(app)


def _seed(client: TestClient) -> None:
    client.post(
        "/panels",
        json={
            "panels": [
                {"panel_id": 1, "score": 10, "loss": 100},
                {"panel_id": 2, "score": 90, "loss": 100},
                {"panel_id": 10, "score": 10, "loss": 1, "inspection_result": 0},
                {"panel_id": 11, "score": 10, "loss": 1, "inspection_result": 1},
                {"panel_id": 12, "score": 90, "loss": 1, "inspection_result": 1},
                {"panel_id": 13, "score": 90, "loss": 1, "inspection_result": 1},
            ]
        },
    )


def test_api_audit_returns_probability_and_version():
    client = fresh_client()
    _seed(client)
    version = client.get("/health").json()["calibration_version"]
    response = client.post(
        "/audit",
        json={
            "checks": [
                {"score": 10, "cap_percent": 50},
                {"score": 90, "cap_percent": 100},
            ],
            "calibration_version": version,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["calibration_version"] == version
    assert body["recheck_required"] is False
    assert body["warnings"] == []
    assert 0.0 <= body["probability"] <= 1.0
    sources = {c["score"]: c["source"] for c in body["resolved_checks"]}
    assert sources == {10: "fitted", 90: "fitted"}
    assert body["inspected_scores"] == [10, 90]


def test_api_audit_flags_stale_calibration():
    client = fresh_client()
    _seed(client)
    version = client.get("/health").json()["calibration_version"]
    # A correction moves the calibration to a new version.
    client.put("/panels/10/inspection", json={"inspection_result": 1})
    response = client.post(
        "/audit",
        json={
            "checks": [{"score": 10, "cap_percent": 50}],
            "calibration_version": version,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["calibration_version"] == version + 1
    assert body["recheck_required"] is True
    assert body["warnings"] and "re-run" in body["warnings"][0]


def test_api_audit_without_version_never_asks_for_recheck():
    client = fresh_client()
    _seed(client)
    response = client.post("/audit", json={"checks": [{"score": 10, "cap_percent": 50}]})
    assert response.status_code == 200
    body = response.json()
    assert body["recheck_required"] is False
    assert body["calibration_version"] >= 1


def test_api_audit_without_any_inspection_is_409():
    client = fresh_client()
    client.post(
        "/panels",
        json={"panels": [{"panel_id": 1, "score": 10, "loss": 100}]},
    )
    response = client.post("/audit", json={"checks": [{"score": 10, "cap_percent": 50}]})
    assert response.status_code == 409
    assert response.json()["code"] == "evidence_insufficient"


def test_api_audit_validates_request_shape():
    client = fresh_client()
    _seed(client)
    valid = {"checks": [{"score": 10, "cap_percent": 50}]}

    def status(body):
        return client.post("/audit", json=body).status_code

    assert status({"checks": []}) == 422
    too_many = {"checks": [{"score": i, "cap_percent": 50} for i in range(13)]}
    assert status(too_many) == 422
    duplicate = {
        "checks": [
            {"score": 10, "cap_percent": 50},
            {"score": 10, "cap_percent": 20},
        ]
    }
    assert status(duplicate) == 422
    assert status({"checks": [{"score": 256, "cap_percent": 50}]}) == 422
    assert status({"checks": [{"score": 10, "cap_percent": 101}]}) == 422
    assert status({"checks": [{"score": 10, "cap_percent": -1}]}) == 422
    assert status({"checks": [{"score": 10.5, "cap_percent": 50}]}) == 422
    assert status({"checks": [{"score": 10}]}) == 422
    assert status({"checks": valid["checks"], "extra": 1}) == 422
    # The well-formed request still succeeds after the rejected ones.
    assert status(valid) == 200


def test_audit_uses_current_counts_after_corrections():
    client = fresh_client()
    _seed(client)
    before = client.post(
        "/audit", json={"checks": [{"score": 90, "cap_percent": 100}]}
    ).json()
    client.put("/panels/12/inspection", json={"inspection_result": 0})
    after = client.post(
        "/audit", json={"checks": [{"score": 90, "cap_percent": 100}]}
    ).json()
    # Cap at 100% stays a tautology, but the version advanced and the same
    # endpoint recomputed from the corrected counts.
    assert after["calibration_version"] == before["calibration_version"] + 1
    assert after["probability"] == 1.0


def test_audit_holds_under_concurrent_corrections():
    import threading

    panel_store = PanelStore()
    panel_store.register(
        [(i, i % 256, 10, None) for i in range(2000)]
        + [(10_000 + i, i % 256, 5, i % 2) for i in range(40)]
    )
    errors = []

    def writer():
        try:
            for step in range(120):
                pid = 10_000 + (step * 7) % 40
                panel_store.set_inspection(pid, step % 2)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader():
        try:
            for _ in range(120):
                snapshot = panel_store.audit_caps([(10, 50), (200, 90)])
                assert 0.0 <= snapshot["probability"] <= 1.0
                assert snapshot["calibration_version"] >= 1
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors


# ------------------------------------------------------------- performance
def test_audit_scales_with_scores_not_panels():
    # 256 inspected components (every score), 50k panels: one DP sweep must
    # stay well below any request-time budget and never enumerate vectors.
    panel_store = PanelStore()
    entries = []
    panel_id = 1
    per_score = 50_000 // 256
    for score in range(256):
        for j in range(per_score):
            entries.append(
                (
                    panel_id,
                    score,
                    10,
                    1 if j < max(1, score // 8) else 0,
                )
            )
            panel_id += 1
    panel_store.register(entries)
    assert panel_store.total_panels <= 50_000

    start = time.perf_counter()
    snapshot = panel_store.audit_caps(
        [(s, min(100, s // 2 + 10)) for s in range(0, 256, 24)]
    )
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5
    assert 0.0 <= snapshot["probability"] <= 1.0
    assert len(snapshot["inspected_scores"]) == 256
