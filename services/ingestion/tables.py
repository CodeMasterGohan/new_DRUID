"""Table-region detection from native line geometry (spec §5).

pdftext's ``table_output`` extracts cell text from caller-supplied bounding
boxes — it does not *detect* tables. This module derives candidate regions
from the geometry pdftext does provide (line bboxes):

* **columnar whitespace gutters** — vertical gaps in the x-projection that
  persist across consecutive rows;
* **repeated row pitch** — lines with consistent vertical spacing and
  aligned x-starts across ≥3 rows;
* **grid alignment** — line x-intervals clustering into stable columns.

Each candidate gets a confidence in [0, 1]. High-confidence regions go to
``pdftext.table_output``; anything below
``RouterThresholds.min_table_confidence`` routes the page to olmOCR, which
reconstructs tables natively.

Honest limitation: true ruling-line detection needs vector graphics, which
pdftext does not expose. These are *text-geometry heuristics* — a proxy
validated on the M1 corpus, not a layout model. If M1 shows native
detection underperforming, the default flips to routing all table-signal
pages to olmOCR (spec §15 decision 3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TableRegion:
    """One candidate table region on a page."""

    # bbox in PDF points: (x1, y1, x2, y2).
    bbox: tuple[float, float, float, float]
    confidence: float  # 0..1
    est_rows: int
    est_cols: int
    # Which heuristics fired, for diagnostics.
    evidence: list[str]


# A row group needs at least this many consecutive aligned rows to count.
_MIN_ROWS = 3
# Two line starts belong to the same column when within this many points.
_COL_TOLERANCE_PTS = 6.0
# A vertical gutter must persist across this many rows to count as columnar.
_GUTTER_MIN_ROWS = 3


def detect_table_regions(
    lines: list[tuple[float, float, float, float]],
    *,
    min_rows: int = _MIN_ROWS,
) -> list[TableRegion]:
    """Detect table regions from line bboxes. Pure function.

    Args:
        lines: (x1, y1, x2, y2) per text line, PDF points.
        min_rows: minimum consecutive aligned rows for a candidate.

    Returns:
        Candidate regions sorted by confidence, highest first.
    """
    if len(lines) < min_rows:
        return []
    rows = _group_rows(lines)
    regions: list[TableRegion] = []
    # Slide over consecutive row runs; each run is one candidate.
    run: list[list[tuple[float, float, float, float]]] = []
    for row in rows:
        if _row_aligns_with_run(row, run):
            run.append(row)
        else:
            if len(run) >= min_rows:
                region = _score_run(run)
                if region is not None:
                    regions.append(region)
            run = [row]
    if len(run) >= min_rows:
        region = _score_run(run)
        if region is not None:
            regions.append(region)
    regions.sort(key=lambda r: r.confidence, reverse=True)
    return _merge_overlapping(regions)


def _group_rows(
    lines: list[tuple[float, float, float, float]],
) -> list[list[tuple[float, float, float, float]]]:
    """Group lines into rows by vertical overlap (top-to-bottom)."""
    # Sort by vertical center, descending y (PDF y grows upward).
    ordered = sorted(lines, key=lambda b: -((b[1] + b[3]) / 2.0))
    rows: list[list[tuple[float, float, float, float]]] = []
    for line in ordered:
        cy = (line[1] + line[3]) / 2.0
        height = abs(line[3] - line[1]) or 1.0
        placed = False
        for row in rows:
            rcy = sum((b[1] + b[3]) / 2.0 for b in row) / len(row)
            if abs(cy - rcy) < height * 0.6:
                row.append(line)
                placed = True
                break
        if not placed:
            rows.append([line])
    return rows


def _column_signature(
    row: list[tuple[float, float, float, float]],
) -> list[float]:
    """Sorted x-starts of a row's lines (the column positions)."""
    return sorted(b[0] for b in row)


def _row_aligns_with_run(
    row: list[tuple[float, float, float, float]],
    run: list[list[tuple[float, float, float, float]]],
) -> bool:
    if not run:
        return True
    prev = _column_signature(run[-1])
    cur = _column_signature(row)
    # Same column count and each column start within tolerance.
    if len(prev) != len(cur) or not cur:
        return False
    return all(abs(a - b) <= _COL_TOLERANCE_PTS for a, b in zip(prev, cur))


def _score_run(
    run: list[list[tuple[float, float, float, float]]],
) -> TableRegion | None:
    """Score one run of aligned rows; None when it is not table-like."""
    n_rows = len(run)
    n_cols = len(_column_signature(run[0]))
    if n_cols < 2:
        return None  # single column of text is not a table

    evidence: list[str] = []
    score = 0.0

    # 1. Repeated row pitch: consistent vertical spacing.
    centers = [
        sum((b[1] + b[3]) / 2.0 for b in row) / len(row) for row in run
    ]
    gaps = [abs(centers[i] - centers[i + 1]) for i in range(len(centers) - 1)]
    if gaps:
        mean_gap = sum(gaps) / len(gaps)
        if mean_gap > 0:
            cv = (sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean_gap
            if cv < 0.35:
                evidence.append(f"repeated-row-pitch cv={cv:.2f}")
                score += 0.35

    # 2. Columnar whitespace gutters: vertical gaps between columns that
    #    persist across rows.
    gutter_score = _gutter_score(run)
    if gutter_score > 0:
        evidence.append(f"columnar-gutters score={gutter_score:.2f}")
        score += 0.35 * gutter_score

    # 3. Grid alignment: column starts stable across the whole run.
    col_starts = [_column_signature(row) for row in run]
    max_dev = 0.0
    for c in range(n_cols):
        vals = [sig[c] for sig in col_starts]
        mean = sum(vals) / len(vals)
        max_dev = max(max_dev, max(abs(v - mean) for v in vals))
    if max_dev <= _COL_TOLERANCE_PTS:
        evidence.append(f"grid-alignment max-dev={max_dev:.1f}pt")
        score += 0.30
    else:
        # Partial credit that decays with deviation.
        score += max(0.0, 0.30 * (1.0 - (max_dev - _COL_TOLERANCE_PTS) / 40.0))

    if not evidence:
        return None

    x1 = min(b[0] for row in run for b in row)
    y1 = min(b[1] for row in run for b in row)
    x2 = max(b[2] for row in run for b in row)
    y2 = max(b[3] for row in run for b in row)
    # Small padding so table_output captures cell edges.
    pad = 2.0
    return TableRegion(
        bbox=(x1 - pad, y1 - pad, x2 + pad, y2 + pad),
        confidence=round(min(1.0, score), 3),
        est_rows=n_rows,
        est_cols=n_cols,
        evidence=evidence,
    )


def _gutter_score(
    run: list[list[tuple[float, float, float, float]]],
) -> float:
    """Fraction of inter-column gaps that persist as whitespace gutters.

    For each adjacent column pair, check that the x-interval between them
    contains no line from any row in the run (a true gutter), across at
    least _GUTTER_MIN_ROWS rows.
    """
    if len(run) < _GUTTER_MIN_ROWS:
        return 0.0
    n_cols = len(_column_signature(run[0]))
    if n_cols < 2:
        return 0.0
    persistent = 0
    total = n_cols - 1
    for c in range(total):
        ok_rows = 0
        for row in run:
            sig = sorted(_column_signature(row))
            if len(sig) != n_cols:
                break
            # Gutter between column c's end and column c+1's start: find the
            # rightmost line starting at sig[c] and check nothing intrudes.
            line_c = max(
                (b for b in row if abs(b[0] - sig[c]) <= _COL_TOLERANCE_PTS),
                key=lambda b: b[2],
                default=None,
            )
            if line_c is None:
                break
            gutter_start, gutter_end = line_c[2], sig[c + 1]
            if gutter_end - gutter_start < 4.0:
                break  # no real whitespace gap
            intrudes = any(
                b[0] < gutter_end - 1.0 and b[2] > gutter_start + 1.0
                for b in row
                if b is not line_c
            )
            if not intrudes:
                ok_rows += 1
        else:
            # loop completed without break
            pass
        if ok_rows >= _GUTTER_MIN_ROWS:
            persistent += 1
    return persistent / total if total else 0.0


def _merge_overlapping(regions: list[TableRegion]) -> list[TableRegion]:
    """Merge regions whose bboxes overlap substantially (keep max confidence)."""
    merged: list[TableRegion] = []
    for region in regions:
        placed = False
        for i, existing in enumerate(merged):
            if _overlap_ratio(region.bbox, existing.bbox) > 0.5:
                if region.confidence > existing.confidence:
                    merged[i] = region
                placed = True
                break
        if not placed:
            merged.append(region)
    return merged


def _overlap_ratio(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)
