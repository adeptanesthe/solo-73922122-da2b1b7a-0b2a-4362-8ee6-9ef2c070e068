"""Isotonic (non-decreasing) calibration of defect rates.

The inspected boards are grouped by defect score.  Boards sharing a score must
share one defect rate, so each score ``s`` contributes a weighted observation
``(positives_s, total_s)``.  Minimising ``sum_i (y_i - rate(score_i))**2``
over the inspected boards is therefore exactly a weighted isotonic regression
of the per-score averages, solved here with pool-adjacent-violators (PAVA).
All arithmetic is exact (integers / :class:`fractions.Fraction`).
"""

from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Mapping, Tuple

SCORE_MIN = 0
SCORE_MAX = 255
SCORE_COUNT = SCORE_MAX - SCORE_MIN + 1


class CalibrationMissing(LookupError):
    """Raised when a ranking is requested without any inspected board."""


@dataclass(frozen=True)
class Calibration:
    """Immutable fitted calibration ready for read-only use."""

    version: int
    inspected_panels: int
    # Rate for scores that actually have inspections.
    fitted_rates: Mapping[int, Fraction]
    # Rate used for ranking a board at score 0..255, including the
    # "nearest inspected score on the left, else lowest inspected score" rule.
    effective_rates: Tuple[Fraction, ...]
    # "fitted", "left" or "lowest", for diagnostics.
    effective_sources: Tuple[str, ...]
    lowest_inspected_score: int


def fit_isotonic(
    counts: Mapping[int, Tuple[int, int]],
) -> Dict[int, Fraction]:
    """Fit the least-squares non-decreasing rates.

    ``counts`` maps score -> (positives, inspected_total); only scores with at
    least one inspected board are present.  Returns score -> fitted rate.
    """
    if not counts:
        return {}

    items = sorted(counts.items())
    # A block is [start_index, end_index, positives, total].
    blocks = []  # type: list[list[int]]

    for index, (_score, (positives, total)) in enumerate(items):
        blocks.append([index, index, positives, total])
        # Pool while the previous block mean exceeds the current block mean.
        while len(blocks) >= 2:
            left = blocks[-2]
            right = blocks[-1]
            # left.pos/left.total > right.pos/right.total, cross-multiplied.
            if left[2] * right[3] > right[2] * left[3]:
                blocks[-2] = [
                    left[0],
                    right[1],
                    left[2] + right[2],
                    left[3] + right[3],
                ]
                blocks.pop()
            else:
                break

    rates = {}  # type: Dict[int, Fraction]
    for start, end, positives, total in blocks:
        rate = Fraction(positives, total)
        for index in range(start, end + 1):
            rates[items[index][0]] = rate
    return rates


def build_calibration(
    counts: Mapping[int, Tuple[int, int]],
    version: int,
) -> Calibration:
    """Fit rates and apply the left-nearest / lowest fallback per score."""
    fitted = fit_isotonic(counts)
    if not fitted:
        raise CalibrationMissing("no inspected boards")

    inspected_scores = sorted(fitted)
    lowest = inspected_scores[0]

    effective = []  # type: list[Fraction]
    sources = []  # type: list[str]
    for score in range(SCORE_COUNT):
        if score in fitted:
            effective.append(fitted[score])
            sources.append("fitted")
            continue
        # Nearest inspected score strictly to the left; otherwise the lowest
        # inspected score (which is also the left fallback if it is above).
        position = bisect_left(inspected_scores, score) - 1
        if position >= 0:
            source_score = inspected_scores[position]
            sources.append("left")
        else:
            source_score = lowest
            sources.append("lowest")
        effective.append(fitted[source_score])

    return Calibration(
        version=version,
        inspected_panels=sum(total for _, total in counts.values()),
        fitted_rates=dict(fitted),
        effective_rates=tuple(effective),
        effective_sources=tuple(sources),
        lowest_inspected_score=lowest,
    )
