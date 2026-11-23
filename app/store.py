"""In-memory panel store with incremental calibration.

Design notes
------------
Only inspected boards feed the calibration, so per-score inspected counts are
maintained directly (``score -> [positives, total]``).  A refit therefore costs
O(256) and never touches the pending population.

Selecting a top-K queue without scanning the pending boards is the other hard
constraint.  Pending boards are bucketed by defect score, and each score keeps
two index heaps over the same entries:

* a max-heap of ``(-loss, panel_id)`` -- the queue order for that score when its
  defect rate is positive, because expected loss is then monotone in loss;
* a min-heap of ``panel_id`` -- used when the defect rate is exactly zero,
  because every expected loss in that bucket is 0 and the tie-break is the
  panel id.

A K-way merge of the (at most 256) buckets' active heaps yields the global top
K.  When calibration changes only the 256 rates change -- the buckets and their
heaps stay valid -- so no pending board is visited.  Entries that become
inspected are removed lazily (their ids leave the bucket's live set).

Every mutation is validated before it is applied, so a rejected registration or
update leaves all state -- and therefore the previous queue -- untouched.
"""

import heapq
import threading
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .calibration import Calibration, CalibrationMissing, build_calibration

MAX_PANELS = 50_000
MAX_QUEUE = 20
SCORE_COUNT = 256

# Heap kinds used by the merge in top_k.
_BY_LOSS = 0
_BY_ID = 1


class ValidationError(ValueError):
    """Client error in a registration or inspection update."""


class NotFound(LookupError):
    """The panel id is unknown."""


class Panel:
    __slots__ = ("panel_id", "score", "loss", "inspected")

    def __init__(self, panel_id: int, score: int, loss: int) -> None:
        self.panel_id = panel_id
        self.score = score
        self.loss = loss
        self.inspected = -1  # -1 pending, else 0/1

    @property
    def is_inspected(self) -> bool:
        return self.inspected != -1


class PanelStore:
    def __init__(self) -> None:
        # Serialises mutations against queue reads.  FastAPI runs these sync
        # endpoints in a thread pool, so a queue request must never observe a
        # half-applied registration or refit.
        self._lock = threading.RLock()
        self._panels: Dict[int, Panel] = {}
        self._counts: Dict[int, List[int]] = {}  # score -> [positives, total]
        self._calibration: Optional[Calibration] = None
        # Pending boards per score, under the two orderings needed by the queue.
        self._loss_heaps: List[List[Tuple[int, int]]] = [
            [] for _ in range(SCORE_COUNT)
        ]  # (-loss, panel_id)
        self._id_heaps: List[List[int]] = [[] for _ in range(SCORE_COUNT)]
        # Ids still pending in each bucket; heap entries outside it are stale.
        self._live: List[Set[int]] = [set() for _ in range(SCORE_COUNT)]
        # Instrumentation: pending-heap entries examined by queue selection.
        # Tests use it to prove selection never scans the pending population.
        self.stats = {"heap_entries_popped": 0, "heap_entries_dropped": 0}

    def reset_stats(self) -> None:
        self.stats = {"heap_entries_popped": 0, "heap_entries_dropped": 0}

    # ------------------------------------------------------------- inspect
    @property
    def calibration(self) -> Optional[Calibration]:
        return self._calibration

    @property
    def calibration_version(self) -> int:
        return self._calibration.version if self._calibration else 0

    @property
    def total_panels(self) -> int:
        return len(self._panels)

    @property
    def inspected_panels(self) -> int:
        return sum(total for _, total in self._counts.values())

    @property
    def pending_panels(self) -> int:
        return self.total_panels - self.inspected_panels

    def require_calibration(self) -> Calibration:
        if self._calibration is None:
            raise CalibrationMissing("no inspected boards")
        return self._calibration

    def get_panel(self, panel_id: int) -> Panel:
        try:
            return self._panels[panel_id]
        except KeyError:
            raise NotFound(f"panel {panel_id} is not registered") from None

    # ---------------------------------------------------------- registration
    def register(self, entries: Iterable[Tuple[int, int, int, Optional[int]]]) -> int:
        """Register boards atomically; entries are (id, score, loss, result)."""
        with self._lock:
            return self._register(entries)

    def _register(self, entries: Iterable[Tuple[int, int, int, Optional[int]]]) -> int:
        entries = list(entries)
        if not entries:
            raise ValidationError("at least one panel is required")

        seen: Set[int] = set()
        for panel_id, score, loss, result in entries:
            if panel_id in self._panels or panel_id in seen:
                raise ValidationError(f"panel {panel_id} is already registered")
            seen.add(panel_id)
            if not (0 <= score <= 255):
                raise ValidationError(f"panel {panel_id}: score must be 0..255")
            if loss <= 0:
                raise ValidationError(
                    f"panel {panel_id}: loss must be a positive integer"
                )
            if result is not None and result not in (0, 1):
                raise ValidationError(
                    f"panel {panel_id}: inspection_result must be 0 or 1"
                )
        if len(self._panels) + len(entries) > MAX_PANELS:
            raise ValidationError(f"at most {MAX_PANELS} panels may be registered")

        counts_changed = False
        for panel_id, score, loss, result in entries:
            panel = Panel(panel_id, score, loss)
            self._panels[panel_id] = panel
            if result is None:
                heapq.heappush(self._loss_heaps[score], (-loss, panel_id))
                heapq.heappush(self._id_heaps[score], panel_id)
                self._live[score].add(panel_id)
            else:
                panel.inspected = result
                self._add_count(score, result)
                counts_changed = True
        if counts_changed:
            self._refit()
        return len(entries)

    # ----------------------------------------------------------- inspection
    def set_inspection(self, panel_id: int, result: int) -> bool:
        """Fill in or correct an inspection result.

        Returns True when the calibration changed (the value actually
        differed).  Validation happens first, so an invalid update leaves the
        existing calibration and queue untouched.
        """
        with self._lock:
            return self._set_inspection(panel_id, result)

    def _set_inspection(self, panel_id: int, result: int) -> bool:
        panel = self.get_panel(panel_id)
        if result not in (0, 1):
            raise ValidationError("inspection_result must be 0 or 1")

        if panel.is_inspected:
            if panel.inspected == result:
                return False
            delta = result - panel.inspected
            panel.inspected = result
            self._counts[panel.score][0] += delta  # inspected total unchanged
        else:
            # Lazy delete: drop the id from the live set.  Stale heap entries
            # are skipped if they ever surface at a heap top.
            self._live[panel.score].discard(panel_id)
            panel.inspected = result
            self._add_count(panel.score, result)
        self._refit()
        return True

    # ---------------------------------------------------------- calibration
    def _add_count(self, score: int, result: int) -> None:
        bucket = self._counts.get(score)
        if bucket is None:
            bucket = self._counts[score] = [0, 0]
        bucket[0] += result
        bucket[1] += 1

    def _refit(self) -> None:
        """Recompute calibration in O(256); pending buckets are unaffected."""
        if not self._counts:
            self._calibration = None
            return
        counts = {score: (pos, total) for score, (pos, total) in self._counts.items()}
        self._calibration = build_calibration(counts, self.calibration_version + 1)

    # ---------------------------------------------------------------- queue
    def _peek(self, score: int, kind: int) -> Optional[int]:
        """Return the live head of a bucket heap, dropping stale tops.

        The head identifies a panel id; the caller re-reads its loss.
        """
        live = self._live[score]
        if kind == _BY_LOSS:
            heap = self._loss_heaps[score]
            while heap and heap[0][1] not in live:
                heapq.heappop(heap)
                self.stats["heap_entries_dropped"] += 1
            return heap[0][1] if heap else None
        heap = self._id_heaps[score]
        while heap and heap[0] not in live:
            heapq.heappop(heap)
            self.stats["heap_entries_dropped"] += 1
        return heap[0] if heap else None

    def _pop(self, score: int, kind: int) -> int:
        """Pop the head returned by the preceding :meth:`_peek`."""
        if kind == _BY_LOSS:
            return heapq.heappop(self._loss_heaps[score])[1]
        return heapq.heappop(self._id_heaps[score])

    def _restore(self, score: int, kind: int, panel_id: int) -> None:
        if kind == _BY_LOSS:
            heapq.heappush(self._loss_heaps[score], (-self._panels[panel_id].loss, panel_id))
        else:
            heapq.heappush(self._id_heaps[score], panel_id)

    def queue_snapshot(self, k: int) -> dict:
        """Queue plus the calibration metadata it was computed from, atomically."""
        with self._lock:
            calibration = self.require_calibration()
            items = self._ranked(k)
            return {
                "calibration_version": calibration.version,
                "inspected_panels": calibration.inspected_panels,
                "pending_panels": self.pending_panels,
                "items": items,
            }

    def counts_snapshot(self) -> dict:
        """Consistent registration counters."""
        with self._lock:
            return {
                "total_panels": self.total_panels,
                "inspected_panels": self.inspected_panels,
                "pending_panels": self.pending_panels,
                "calibration_version": self.calibration_version,
            }

    def _ranked(self, k: int) -> List[Tuple[int, int, Fraction, Fraction]]:
        if k < 1 or k > MAX_QUEUE:
            raise ValidationError(f"k must be between 1 and {MAX_QUEUE}")
        rates = self.require_calibration().effective_rates

        # Merge key: (-expected_loss, panel_id).  For a positive rate the loss
        # heap orders the bucket correctly; for a zero rate every expectation
        # ties at 0, so the id heap supplies the tie-break order instead.
        heads: List[Tuple[Fraction, int, int, int]] = []
        for score in range(SCORE_COUNT):
            rate = rates[score]
            kind = _BY_LOSS if rate > 0 else _BY_ID
            panel_id = self._peek(score, kind)
            if panel_id is None:
                continue
            if kind == _BY_LOSS:
                expected = rate * self._panels[panel_id].loss
            else:
                expected = Fraction(0)
            heads.append((-expected, panel_id, score, kind))
        heapq.heapify(heads)

        selected: List[Tuple[int, int, Fraction, Fraction]] = []  # id, score, rate, exp
        popped: List[Tuple[int, int, int]] = []  # (score, kind, panel_id)
        try:
            while heads and len(selected) < k:
                neg_expected, panel_id, score, kind = heapq.heappop(heads)
                popped_id = self._pop(score, kind)
                self.stats["heap_entries_popped"] += 1
                assert popped_id == panel_id
                popped.append((score, kind, panel_id))
                selected.append((panel_id, score, rates[score], -neg_expected))
                rate = rates[score]
                next_id = self._peek(score, kind)
                if next_id is not None:
                    if kind == _BY_LOSS:
                        expected = rate * self._panels[next_id].loss
                    else:
                        expected = Fraction(0)
                    heapq.heappush(heads, (-expected, next_id, score, kind))
        finally:
            # Selection is read-only: restore every consumed entry even if an
            # unexpected error aborts the merge.
            for score, kind, panel_id in popped:
                self._restore(score, kind, panel_id)

        selected.sort(key=lambda item: (-item[3], item[0]))
        return selected

    def top_k(self, k: int) -> List[Tuple[int, Fraction, Fraction]]:
        """Public queue view: (panel_id, defect_rate, expected_loss)."""
        with self._lock:
            return [
                (panel_id, rate, expected)
                for panel_id, _score, rate, expected in self._ranked(k)
            ]
