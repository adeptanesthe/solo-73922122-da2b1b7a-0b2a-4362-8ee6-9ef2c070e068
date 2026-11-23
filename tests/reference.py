"""Reference implementations used to cross-check the production code."""

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
