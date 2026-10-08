"""Table-region detection tests (spec §5).

Synthetic line geometry: a clean 4x5 grid must score high-confidence;
scattered prose must produce nothing; overlapping candidates merge.
"""

from services.ingestion.tables import detect_table_regions


def _grid(x0=72.0, y0=700.0, cols=4, rows=5, col_w=100.0, row_h=18.0,
          cell_w=80.0, jitter=0.0):
    """Lines arranged as a text table grid."""
    import random

    rng = random.Random(42)
    lines = []
    for r in range(rows):
        y = y0 - r * row_h
        for c in range(cols):
            jx = rng.uniform(-jitter, jitter)
            lines.append((x0 + c * col_w + jx, y - 12, x0 + c * col_w + cell_w + jx, y))
    return lines


def _prose(n=12):
    return [(72.0, 700.0 - i * 16, 540.0, 712.0 - i * 16) for i in range(n)]


def test_clean_grid_detected_with_high_confidence():
    regions = detect_table_regions(_grid())
    assert len(regions) == 1
    r = regions[0]
    assert r.confidence >= 0.75
    assert r.est_rows == 5
    assert r.est_cols == 4
    assert r.evidence, "expected named evidence for diagnostics"


def test_prose_produces_no_regions():
    assert detect_table_regions(_prose()) == []


def test_too_few_rows_produces_nothing():
    assert detect_table_regions(_grid(rows=2)) == []


def test_single_column_is_not_a_table():
    lines = [(72.0, 700.0 - i * 18, 300.0, 712.0 - i * 18) for i in range(6)]
    assert detect_table_regions(lines) == []


def test_jittered_grid_still_detected():
    regions = detect_table_regions(_grid(jitter=3.0))
    assert regions and regions[0].confidence >= 0.6


def test_overlapping_candidates_merge():
    lines = _grid(rows=6)
    regions = detect_table_regions(lines)
    assert len(regions) == 1


def test_bbox_covers_grid():
    regions = detect_table_regions(_grid())
    x1, y1, x2, y2 = regions[0].bbox
    assert x1 <= 72.0 and x2 >= 72.0 + 3 * 100.0 + 80.0
    assert y1 <= 700.0 - 4 * 18.0 - 12 and y2 >= 700.0
