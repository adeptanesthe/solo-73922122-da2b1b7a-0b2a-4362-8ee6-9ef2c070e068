"""Reference implementations used to cross-check the production code."""

from itertools import combinations_with_replacement
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple


def brute_force_isotonic(
    counts: Dict[int, Tuple[int, int]]
) -> Dict[int, Fraction]:
    """Independent exact isotonic oracle, in O(n^3).

    Uses the min-max characterization of the weighted isotonic regression:
    ``theta_i = max_{a<=i} min_{b>=i} mean(a..b)``.  This shares no code with
    the PAVA implementation under test.
    """
    scores = sorted(counts)
    if not scores:
        return {}
    totals = [counts[s][1] for s in scores]
    positives = [counts[s][0] for s in scores]
    n = len(scores)

    def mean(a: int, b: int) -> Fraction:
        return Fraction(sum(positives[a : b + 1]), sum(totals[a : b + 1]))

    rates: Dict[int, Fraction] = {}
    for i in range(n):
        rates[scores[i]] = max(
            min(mean(a, b) for b in range(i, n)) for a in range(0, i + 1)
        )
    return rates


def reference_rates(
    counts: Dict[int, Tuple[int, int]]
) -> Optional[Dict[int, Fraction]]:
    """Fit plus the left-nearest / lowest fallback, independently written."""
    fitted = brute_force_isotonic(counts)
    if not fitted:
        return None
    inspected = sorted(fitted)
    out: Dict[int, Fraction] = {}
    for score in range(256):
        candidates = [s for s in inspected if s <= score]
        source = candidates[-1] if candidates else inspected[0]
        out[score] = fitted[source]
    return out


def reference_queue(
    panels: Sequence[Tuple[int, int, int, int]],
    rates: Dict[int, Fraction],
    k: int,
) -> List[Tuple[int, Fraction, Fraction]]:
    """Brute-force the top K by scanning every pending board."""
    rows = []
    for panel_id, score, loss, inspected in panels:
        if inspected != -1:
            continue
        rate = rates[score]
        rows.append((panel_id, rate, rate * loss))
    rows.sort(key=lambda r: (-r[2], r[0]))
    return rows[:k]


# ------------------------------------------------------------- audit oracle
def brute_force_audit(
    counts: Dict[int, Tuple[int, int]],
    checks: Sequence[Tuple[int, int]],
    grid_max: int = 100,
) -> Fraction:
    """Exact posterior probability by enumerating every monotone grid vector.

    Independent of the production DP: it walks every non-decreasing tuple of
    integer percentages (``combinations_with_replacement``), weights it with
    the exact Bernoulli likelihood as a :class:`Fraction`, and sums the
    weights that satisfy *all* caps after applying the left/lowest fallback.
    Only usable for small component counts / grids (tests only).
    """
    if not counts:
        raise ValueError("audit oracle requires at least one inspected score")

    inspected = sorted(counts)
    ordered_counts = [counts[s] for s in inspected]

    def component_for(score: int) -> int:
        left = [i for i, s in enumerate(inspected) if s <= score]
        return left[-1] if left else 0

    # Several queried scores may map to one component: keep the tightest cap,
    # because all caps must hold simultaneously.
    caps: Dict[int, int] = {}
    for score, cap in checks:
        component = component_for(score)
        caps[component] = min(cap, caps.get(component, cap))

    total = Fraction(0)
    accepted = Fraction(0)
    levels = range(grid_max + 1)
    for vector in combinations_with_replacement(levels, len(inspected)):
        weight = Fraction(1)
        for level, (positives, totals) in zip(vector, ordered_counts):
            rate = Fraction(level, grid_max)
            weight *= rate**positives * (1 - rate) ** (totals - positives)
        total += weight
        if all(vector[component] <= cap for component, cap in caps.items()):
            accepted += weight
    assert total > 0
    return accepted / total
