"""Page-level router: native vs visual (olmOCR) vs skip (spec §5).

Each page is routed independently on computed signals:

* text density (chars per square inch) — scanned/broken pages score ~0
* replacement-character ratio (U+FFFD / stray controls)
* private-use-area ratio (broken ToUnicode maps)
* reading-order disagreement (extraction order vs geometric order)
* multi-column complexity
* table signals (delegated to :mod:`tables`)
* blank-page detection

Decision rules are deliberately simple and every threshold lives in
:class:`RouterThresholds` (config.py), marked PROVISIONAL until the M1
eval corpus calibrates them. The router never hardcodes a magic number.

An intact text layer is evidence, not an automatic pass: a dense page can
still route to olmOCR when glyph mappings are broken or reading order is
ambiguous.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field

from .config import RouterThresholds
from .tables import TableRegion


class PageRoute(str, enum.Enum):
    """Where a page goes."""

    NATIVE = "native"  # pdftext path, incl. native table_output
    VISUAL = "visual"  # render page → olmOCR 2 FP8
    SKIP_BLANK = "skip_blank"  # blank page: nothing to extract


@dataclass
class TextBlock:
    """One pdftext block (approx. a paragraph) with geometry."""

    text: str
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 in PDF points
    # Per-line bboxes, used for column/row analysis. Each is (x1, y1, x2, y2).
    lines: list[tuple[float, float, float, float]] = field(default_factory=list)


@dataclass
class PageGeometry:
    """Normalized native extraction of one page (built by native_worker)."""

    page_index: int
    # Page bbox in PDF points (72 dpi).
    page_bbox: tuple[float, float, float, float]
    blocks: list[TextBlock] = field(default_factory=list)
    # Detected table regions (from tables.detect_table_regions).
    table_regions: list[TableRegion] = field(default_factory=list)


@dataclass
class RouterSignals:
    """Computed per-signal values, stored in parsing diagnostics (§6)."""

    text_density: float = 0.0
    replacement_char_ratio: float = 0.0
    private_use_ratio: float = 0.0
    order_disagreement_ratio: float = 0.0
    column_count: int = 1
    is_blank: bool = False
    table_region_count: int = 0
    table_area_ratio: float = 0.0
    min_table_confidence: float = 1.0
    rotated_span_ratio: float = 0.0


@dataclass
class RouterDecision:
    route: PageRoute
    signals: RouterSignals
    reasons: list[str]
    # Confidence that the chosen route is correct (0..1). Low confidence
    # on VISUAL pages is what feeds the review queue.
    confidence: float


_REPLACEMENT_CHARS = {"\ufffd"}
_CONTROL_WHITELIST = {"\t", "\n", "\r"}


def _is_private_use(ch: str) -> bool:
    cp = ord(ch)
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or 0x100000 <= cp <= 0x10FFFD


def compute_signals(page: PageGeometry) -> RouterSignals:
    """Compute all routing signals for one page. Pure function."""
    signals = RouterSignals()
    x1, y1, x2, y2 = page.page_bbox
    area_sqin = max(1e-9, abs(x2 - x1) * abs(y2 - y1) / (72.0 * 72.0))

    full_text = "\n".join(b.text for b in page.blocks)
    total_chars = len(full_text)
    signals.text_density = total_chars / area_sqin
    signals.is_blank = total_chars == 0

    if total_chars:
        bad = sum(
            1
            for ch in full_text
            if ch in _REPLACEMENT_CHARS or (ord(ch) < 32 and ch not in _CONTROL_WHITELIST)
        )
        signals.replacement_char_ratio = bad / total_chars
        signals.private_use_ratio = (
            sum(1 for ch in full_text if _is_private_use(ch)) / total_chars
        )

    signals.order_disagreement_ratio = _order_disagreement(page.blocks)
    signals.column_count = _column_count(page.blocks)

    if page.table_regions:
        signals.table_region_count = len(page.table_regions)
        page_area = abs(x2 - x1) * abs(y2 - y1)
        table_area = sum(
            abs(r.bbox[2] - r.bbox[0]) * abs(r.bbox[3] - r.bbox[1])
            for r in page.table_regions
        )
        signals.table_area_ratio = table_area / max(1e-9, page_area)
        signals.min_table_confidence = min(r.confidence for r in page.table_regions)

    return signals


def _order_disagreement(blocks: list[TextBlock]) -> float:
    """Fraction of adjacent block pairs whose extraction order contradicts
    top-to-bottom, left-to-right geometric order.

    pdftext extracts in PDF content-stream order, which is frequently not
    reading order (multi-column layouts, floating boxes). High disagreement
    means the native text needs geometric re-sorting at minimum — and often
    the visual path instead.
    """
    if len(blocks) < 2:
        return 0.0
    # Geometric order: sort by vertical center, then horizontal center.
    def _center(b: TextBlock) -> tuple[float, float]:
        x1, y1, x2, y2 = b.bbox
        return ((y1 + y2) / 2.0, (x1 + x2) / 2.0)

    # NOTE: PDF y grows upward; reading order is decreasing y.
    heights = sorted(max(1.0, abs(b.bbox[3] - b.bbox[1])) for b in blocks)
    band_tol = max(4.0, heights[len(heights) // 2] * 0.5)
    geo_order = sorted(
        range(len(blocks)),
        key=lambda i: (-round(_center(blocks[i])[0] / band_tol), _center(blocks[i])[1]),
    )
    widths = [max(1.0, abs(b.bbox[2] - b.bbox[0])) for b in blocks]
    col_tol = max(12.0, (sum(widths) / len(widths)) * 0.5)
    col_order = sorted(
        range(len(blocks)),
        key=lambda i: (
            round(_center(blocks[i])[1] / col_tol),
            -round(_center(blocks[i])[0] / band_tol),
        ),
    )
    col_rank = {idx: pos for pos, idx in enumerate(col_order)}
    col_disagreements = sum(
        1 for i in range(len(blocks) - 1) if col_rank[i] > col_rank[i + 1]
    )
    row_rank = {idx: pos for pos, idx in enumerate(geo_order)}
    row_disagreements = sum(
        1 for i in range(len(blocks) - 1) if row_rank[i] > row_rank[i + 1]
    )
    if min(row_disagreements, col_disagreements) < row_disagreements:
        geo_order = col_order
    rank = {idx: pos for pos, idx in enumerate(geo_order)}
    disagreements = sum(
        1 for i in range(len(blocks) - 1) if rank[i] > rank[i + 1]
    )
    return disagreements / (len(blocks) - 1)


def _column_count(blocks: list[TextBlock]) -> int:
    """Estimate text columns by clustering block x-intervals.

    Blocks whose horizontal spans overlap substantially belong to the same
    column; distinct non-overlapping clusters with vertical overlap indicate
    multiple columns.
    """
    if not blocks:
        return 1
    # Use block horizontal centers; cluster with a simple sweep.
    centers = sorted((b.bbox[0] + b.bbox[2]) / 2.0 for b in blocks)
    widths = [(b.bbox[2] - b.bbox[0]) for b in blocks]
    typical_half_width = (sum(widths) / len(widths)) / 2.0 or 1.0
    clusters: list[list[float]] = []
    for c in centers:
        placed = False
        for cluster in clusters:
            if abs(c - sum(cluster) / len(cluster)) < typical_half_width:
                cluster.append(c)
                placed = True
                break
        if not placed:
            clusters.append([c])
    # A "column" needs vertical overlap between members of different
    # clusters; with block-level data we approximate: keep clusters that
    # hold a meaningful share of blocks.
    significant = [cl for cl in clusters if len(cl) >= max(2, len(blocks) // 4)]
    return max(1, len(significant))


def route_page(
    page: PageGeometry, thresholds: RouterThresholds | None = None
) -> RouterDecision:
    """Route one page. Pure function of (page, thresholds)."""
    thresholds = thresholds or RouterThresholds()
    signals = compute_signals(page)
    reasons: list[str] = []

    if signals.is_blank:
        return RouterDecision(
            route=PageRoute.SKIP_BLANK,
            signals=signals,
            reasons=["no extractable text: blank page"],
            confidence=1.0,
        )

    # Broken text layer → visual, regardless of density.
    if signals.replacement_char_ratio > thresholds.max_replacement_char_ratio:
        reasons.append(
            f"replacement-char ratio {signals.replacement_char_ratio:.3f} > "
            f"{thresholds.max_replacement_char_ratio}"
        )
    if signals.private_use_ratio > thresholds.max_private_use_ratio:
        reasons.append(
            f"private-use ratio {signals.private_use_ratio:.3f} > "
            f"{thresholds.max_private_use_ratio}"
        )
    # Scanned / image-only page.
    if signals.text_density < thresholds.min_text_density_chars_per_sqin:
        reasons.append(
            f"text density {signals.text_density:.1f} chars/in² < "
            f"{thresholds.min_text_density_chars_per_sqin}"
        )
    # Complex layout with unreliable order.
    if (
        signals.column_count >= thresholds.multicolumn_min_columns
        and signals.order_disagreement_ratio > thresholds.max_order_disagreement_ratio
    ):
        reasons.append(
            f"multi-column ({signals.column_count}) with order disagreement "
            f"{signals.order_disagreement_ratio:.2f}"
        )
    # Table-heavy page whose regions we cannot confidently extract natively.
    if (
        signals.table_region_count > 0
        and signals.table_area_ratio >= thresholds.table_area_ratio_heavy
        and signals.min_table_confidence < thresholds.min_table_confidence
    ):
        reasons.append(
            f"table-heavy page (area ratio {signals.table_area_ratio:.2f}) with "
            f"low native table confidence {signals.min_table_confidence:.2f}"
        )

    if reasons:
        # Confidence degrades as more signals fire; a single strong signal
        # (e.g. blank-scan) is more certain than a pile of marginal ones.
        confidence = max(0.5, 1.0 - 0.1 * (len(reasons) - 1))
        return RouterDecision(
            route=PageRoute.VISUAL, signals=signals, reasons=reasons, confidence=confidence
        )

    return RouterDecision(
        route=PageRoute.NATIVE,
        signals=signals,
        reasons=["native extraction acceptable"],
        confidence=0.9,
    )


def signals_to_dict(signals: RouterSignals) -> dict:
    """Serialize signals for parsing diagnostics (§6 derived artifacts)."""
    return {
        "text_density_chars_per_sqin": round(signals.text_density, 2),
        "replacement_char_ratio": round(signals.replacement_char_ratio, 4),
        "private_use_ratio": round(signals.private_use_ratio, 4),
        "order_disagreement_ratio": round(signals.order_disagreement_ratio, 3),
        "column_count": signals.column_count,
        "is_blank": signals.is_blank,
        "table_region_count": signals.table_region_count,
        "table_area_ratio": round(signals.table_area_ratio, 3),
        "min_table_confidence": round(signals.min_table_confidence, 3),
    }
