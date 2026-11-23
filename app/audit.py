"""Bayesian posterior checks for simultaneous defect-rate caps.

Model
-----
Only defect scores with at least one inspected board carry a latent defect
rate.  Ordered by score, those inspected scores are ``s_1 < ... < s_m`` with
rates ``x_1 <= x_2 <= ... <= x_m`` where each ``x_j`` is an integer percentage
on the grid ``0..100``.  *Every* non-decreasing grid vector gets the same
prior weight.  Scores without inspections are not free variables: their rate
is determined by the existing fallback rule (nearest inspected score on the
left, else the lowest inspected score), so a query about such a score is a
query about the component it borrows.

Given per-score inspected counts ``(positives_j, total_j)`` and
board-independent Bernoulli likelihoods, the posterior weight of a vector is

    prod_j (x_j / 100) ** positives_j
           * (1 - x_j / 100) ** (total_j - positives_j).

A check supplies 1..12 distinct scores, each with an integer percentage cap;
the reported probability is the posterior mass of the event "the effective
rate of every queried score is at most its cap", with all caps holding at
once.

Why no vector is ever enumerated
---------------------------------
Both the posterior normaliser and the constrained mass are monotone
lattice sums computed with a prefix-sum dynamic program in ``O(m * 101)``
time and ``O(101)`` memory, independent of the number of panels (up to
50,000) and of the ``~101**m`` vectors that would otherwise be enumerated.
Sums run in log space via a local ``logaddexp`` helper, so likelihoods that
underflow to zero (a rate contradicted by observations) are handled
exactly: ``log 0`` simply drops the term, matching the convention
``0**0 = 1``.
"""

from bisect import bisect_left
from dataclasses import dataclass
from math import exp, inf, log, log1p
from typing import Mapping, Optional, Sequence, Tuple

#: Defect rates are integer percentages from 0% to 100%.
GRID_MAX = 100
NEG_INF = -inf


def _log_weight(level: int, positives: int, total: int, grid_max: int) -> float:
    """Log of the Bernoulli likelihood of one component at a grid level.

    ``level`` is the percentage (0..grid_max); the board-level rate is
    ``level / grid_max``.  A level contradicted by the data (e.g. rate 0 with
    any positive observation) has weight zero, represented as ``-inf``; the
    ``0**0 = 1`` convention makes absent factors contribute nothing.
    """
    negatives = total - positives
    if level == 0:
        return 0.0 if positives == 0 else NEG_INF
    if level == grid_max:
        return 0.0 if negatives == 0 else NEG_INF
    rate_log = log(level) - log(grid_max)
    complement_log = log(grid_max - level) - log(grid_max)
    return positives * rate_log + negatives * complement_log


def _log_normaliser(
    counts: Sequence[Tuple[int, int]],
    caps: Optional[Sequence[Optional[int]]] = None,
    grid_max: int = GRID_MAX,
) -> float:
    """Log of the summed weight over non-decreasing grid vectors.

    ``counts`` follows the ascending inspected-score order; ``caps[j]``, when
    given, restricts component ``j`` to grid levels ``<= caps[j]`` (the
    simultaneous-cap event).  The DP keeps the running prefix sum of the
    previous component's mass, so each component costs one linear sweep:

        prev[x] = weight_j(x) * sum_{y <= x} dp_{j-1}(y)
    """
    levels = range(grid_max + 1)
    # Virtual predecessor with unit mass at level 0 only: the first real
    # component then sees an all-zero log prefix (dp_1(x) = weight_1(x)),
    # while later components pick up the genuine sum over y <= x.
    previous = [NEG_INF] * (grid_max + 1)
    previous[0] = 0.0
    for index, (positives, total) in enumerate(counts):
        cap = caps[index] if caps is not None else None
        prefix = NEG_INF
        current = [NEG_INF] * (grid_max + 1)
        for level in levels:
            prefix = logaddexp(prefix, previous[level])
            if cap is not None and level > cap:
                continue
            weight_log = _log_weight(level, positives, total, grid_max)
            if weight_log == NEG_INF or prefix == NEG_INF:
                continue
            current[level] = weight_log + prefix
        previous = current
    return logaddexp_reduce(previous)


def logaddexp(a: float, b: float) -> float:
    """``log(e**a + e**b)`` that stays finite when either term is ``-inf``."""
    if a == NEG_INF:
        return b
    if b == NEG_INF:
        return a
    if a >= b:
        # b - a <= 0: no overflow, and log1p stays accurate near zero.
        return a + log1p(exp(b - a))
    return b + log1p(exp(a - b))


def logaddexp_reduce(values: Sequence[float]) -> float:
    total = NEG_INF
    for value in values:
        total = logaddexp(total, value)
    return total


@dataclass(frozen=True)
class ResolvedConstraint:
    """One requested cap after mapping the score to its latent component."""

    score: int
    cap_percent: int
    # Index of the inspected-score component that actually determines this
    # score's effective rate, and where that mapping comes from.
    component_index: int
    source_score: int
    source: str  # "fitted" | "left" | "lowest"


@dataclass(frozen=True)
class AuditResult:
    probability: float
    log_posterior_mass: float
    log_normaliser: float
    calibration_version: int
    inspected_scores: Tuple[int, ...]
    constraints: Tuple[ResolvedConstraint, ...]


def resolve_constraints(
    checks: Sequence[Tuple[int, int]],
    inspected_scores: Sequence[int],
) -> Tuple[Tuple[Optional[int], ...], Tuple[ResolvedConstraint, ...]]:
    """Map each queried score onto the component its rate borrows.

    Uninspected scores use the service-wide fallback: the nearest inspected
    score on the left, or the lowest inspected score when none is on the
    left.  Several queries may therefore constrain the same component; the
    tightest (smallest) cap wins, since all caps must hold simultaneously.
    """
    ordered = sorted(inspected_scores)
    lowest = ordered[0]
    index_of = {score: index for index, score in enumerate(ordered)}
    caps: list[Optional[int]] = [None] * len(ordered)
    resolved: list[ResolvedConstraint] = []

    for score, cap in checks:
        if score in index_of:
            component_index = index_of[score]
            source_score = score
            source = "fitted"
        else:
            # Nearest inspected score strictly to the left; otherwise the
            # lowest inspected score (the service-wide fallback rule).
            position = bisect_left(ordered, score) - 1
            if position >= 0:
                component_index = position
                source_score = ordered[position]
                source = "left"
            else:
                component_index = 0
                source_score = lowest
                source = "lowest"
        previous = caps[component_index]
        if previous is None or cap < previous:
            caps[component_index] = cap
        resolved.append(
            ResolvedConstraint(
                score=score,
                cap_percent=cap,
                component_index=component_index,
                source_score=source_score,
                source=source,
            )
        )
    return tuple(caps), tuple(resolved)


def posterior_cap_probability(
    counts: Mapping[int, Tuple[int, int]],
    checks: Sequence[Tuple[int, int]],
    calibration_version: int,
    grid_max: int = GRID_MAX,
) -> AuditResult:
    """Posterior probability that every cap holds simultaneously.

    ``counts`` maps an inspected score to ``(positives, inspected_total)``;
    ``checks`` is the validated list of ``(score, cap_percent)`` pairs with
    distinct scores.  Raises :class:`InsufficientEvidence` when no score has
    any real inspection, since the posterior would then be the uninformative
    prior over an unanchored curve.
    """
    if not counts:
        raise InsufficientEvidence(
            "no inspected panels: the posterior is not anchored by any evidence"
        )

    inspected_scores = sorted(counts)
    ordered_counts = tuple(counts[score] for score in inspected_scores)
    caps, resolved = resolve_constraints(checks, inspected_scores)

    log_denominator = _log_normaliser(ordered_counts, None, grid_max)
    log_numerator = _log_normaliser(ordered_counts, caps, grid_max)
    if log_numerator == NEG_INF:
        probability = 0.0
    else:
        probability = min(1.0, max(0.0, exp(log_numerator - log_denominator)))

    return AuditResult(
        probability=probability,
        log_posterior_mass=log_numerator,
        log_normaliser=log_denominator,
        calibration_version=calibration_version,
        inspected_scores=tuple(inspected_scores),
        constraints=resolved,
    )


class InsufficientEvidence(LookupError):
    """Raised when a check is requested without any inspected board."""
