"""Router signal + decision tests (spec §5).

Synthetic PageGeometry fixtures exercise each routing signal without
pdftext: blank pages skip, scanned/broken layers go visual, clean text
stays native.
"""

import pytest

from services.ingestion.config import RouterThresholds
from services.ingestion.router import (
    PageGeometry,
    PageRoute,
    TextBlock,
    compute_signals,
    route_page,
)
from services.ingestion.tables import TableRegion


def _block(text, x1=72.0, y1=700.0, x2=540.0, y2=716.0, n_lines=1):
    lines = []
    h = (y2 - y1) / n_lines
    for i in range(n_lines):
        lines.append((x1, y2 - (i + 1) * h, x2, y2 - i * h))
    return TextBlock(text=text, bbox=(x1, y1, x2, y2), lines=lines)


def _page(blocks, page_bbox=(0, 0, 612, 792)):
    return PageGeometry(page_index=0, page_bbox=page_bbox, blocks=blocks)


def _dense_text(n_chars=3000):
    return "lorem ipsum dolor sit amet " * (n_chars // 27)


def test_blank_page_skips():
    decision = route_page(_page([]))
    assert decision.route == PageRoute.SKIP_BLANK
    assert decision.signals.is_blank


def test_clean_text_page_stays_native():
    blocks = [
        _block(_dense_text(), y1=700 - i * 20, y2=716 - i * 20) for i in range(5)
    ]
    decision = route_page(_page(blocks))
    assert decision.route == PageRoute.NATIVE
    assert decision.signals.text_density > 8.0


def test_scanned_page_goes_visual():
    # A few stray chars on a full page: image-dominated, no usable layer.
    decision = route_page(_page([_block("a b")]))
    assert decision.route == PageRoute.VISUAL
    assert any("density" in r for r in decision.reasons)


def test_replacement_chars_go_visual():
    text = _dense_text() + "\ufffd" * 200
    decision = route_page(_page([_block(text)]))
    assert decision.route == PageRoute.VISUAL
    assert any("replacement-char" in r for r in decision.reasons)


def test_private_use_glyphs_go_visual():
    text = _dense_text() + "\ue000" * 300
    decision = route_page(_page([_block(text)]))
    assert decision.route == PageRoute.VISUAL
    assert any("private-use" in r for r in decision.reasons)


def test_multicolumn_with_bad_order_goes_visual():
    # Two columns; extraction order interleaves them (content-stream order).
    left = [_block(_dense_text(400), x1=72, x2=280, y1=700 - i * 20, y2=716 - i * 20)
            for i in range(6)]
    right = [_block(_dense_text(400), x1=332, x2=540, y1=700 - i * 20, y2=716 - i * 20)
             for i in range(6)]
    interleaved = [b for pair in zip(left, right) for b in pair]
    # Reverse extraction order entirely to force disagreement.
    page = _page(list(reversed(interleaved)))
    signals = compute_signals(page)
    assert signals.column_count >= 2
    decision = route_page(page)
    assert decision.route == PageRoute.VISUAL


def test_table_heavy_low_confidence_goes_visual():
    blocks = [_block(_dense_text(), y1=100, y2=116)]
    page = _page(blocks)
    page.table_regions = [
        TableRegion(bbox=(72, 200, 540, 700), confidence=0.4,
                    est_rows=10, est_cols=4, evidence=["weak"])
    ]
    decision = route_page(page, RouterThresholds(table_area_ratio_heavy=0.3))
    assert decision.route == PageRoute.VISUAL
    assert any("table-heavy" in r for r in decision.reasons)


def test_high_confidence_tables_stay_native():
    blocks = [_block(_dense_text(), y1=100, y2=116)]
    page = _page(blocks)
    page.table_regions = [
        TableRegion(bbox=(72, 600, 200, 700), confidence=0.95,
                    est_rows=5, est_cols=3, evidence=["grid"])
    ]
    decision = route_page(page)
    assert decision.route == PageRoute.NATIVE


def test_thresholds_are_tunable_not_hardcoded():
    strict = RouterThresholds(min_text_density_chars_per_sqin=1000.0)
    page = _page([_block(_dense_text())])
    assert route_page(page).route == PageRoute.NATIVE
    assert route_page(page, strict).route == PageRoute.VISUAL


def test_signals_serialize_for_diagnostics():
    from services.ingestion.router import signals_to_dict

    d = signals_to_dict(compute_signals(_page([])))
    assert d["is_blank"] is True
    assert "text_density_chars_per_sqin" in d
