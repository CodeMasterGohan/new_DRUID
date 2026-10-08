"""Process-based native PDF extraction workers (spec §5, native path).

HARD CONSTRAINT: PDFium is not thread-safe. Every function here runs
extraction in *processes* — pdftext's built-in ``workers=`` pool (which is
process-based) or an explicit ``multiprocessing`` pool. There is no
threaded path, and none may be added.

Parallelism note (pdftext behavior, documented not assumed): the
``workers=`` pool only engages when each worker would receive at least
``PDFTEXT_WORKER_PAGE_THRESHOLD`` pages (default 10); smaller documents
run serially. Either way the work happens in a child process.

pdftext is imported lazily inside the worker functions so the rest of the
package (router, chunker, tests) imports without the native dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import IngestionConfig
from .router import PageGeometry, TextBlock
from .tables import TableRegion, detect_table_regions


@dataclass
class NativePage:
    """Normalized native extraction of one page."""

    page_index: int
    page_bbox: tuple[float, float, float, float]
    rotation: int
    geometry: PageGeometry
    # Raw pdftext page dict, kept for table_output input construction.
    raw: dict = field(default_factory=dict, repr=False)


def _require_pdftext():
    try:
        import pdftext  # noqa: F401
        from pdftext.extraction import dictionary_output, table_output
    except ImportError as exc:
        raise RuntimeError(
            "pdftext is required for native extraction but is not installed. "
            "Install it from the pinned requirements (no model downloads involved)."
        ) from exc
    return dictionary_output, table_output


def extract_pages_native(
    pdf_path: str,
    *,
    page_range: list[int] | None = None,
    workers: int | None = None,
    config: IngestionConfig | None = None,
) -> list[NativePage]:
    """Extract text+geometry for pages, in worker *processes*.

    Args:
        pdf_path: path to the PDF (must be a path, not a file-like object:
            file-likes cannot cross the process boundary and run serially).
        page_range: pdftext page indices (0-based), or None for all pages.
        workers: process count; defaults to config.pdftext_workers.
        config: ingestion config (thresholds for table detection).

    Returns:
        Pages in document order, each with table regions pre-detected.
    """
    dictionary_output, _ = _require_pdftext()
    config = config or IngestionConfig()
    n_workers = workers if workers is not None else config.pdftext_workers

    kwargs: dict = {"sort": False, "keep_chars": False}
    if page_range is not None:
        kwargs["page_range"] = page_range
    # workers= is process-based in pdftext. Small documents (< threshold
    # pages per worker) transparently run serially — still in a process.
    kwargs["workers"] = max(1, n_workers)

    raw_pages: list[dict] = dictionary_output(pdf_path, **kwargs)
    pages: list[NativePage] = []
    for raw in raw_pages:
        page = _normalize_page(raw)
        # Pre-detect table regions so the router can see table signals.
        lines = [ln for b in page.geometry.blocks for ln in b.lines]
        page.geometry.table_regions = detect_table_regions(lines)
        pages.append(page)
    return pages


def _normalize_page(raw: dict) -> NativePage:
    """Convert one pdftext page dict to NativePage."""
    page_bbox = tuple(raw.get("bbox", (0, 0, 612, 792)))
    blocks: list[TextBlock] = []
    for block in raw.get("blocks", []):
        lines: list[tuple[float, float, float, float]] = []
        texts: list[str] = []
        for line in block.get("lines", []):
            lbbox = tuple(line.get("bbox", (0, 0, 0, 0)))
            lines.append(lbbox)
            span_texts = [
                span.get("text", "") for span in line.get("spans", [])
            ]
            texts.append("".join(span_texts))
        blocks.append(
            TextBlock(
                text="\n".join(texts),
                bbox=tuple(block.get("bbox", (0, 0, 0, 0))),
                lines=lines,
            )
        )
    geometry = PageGeometry(
        page_index=int(raw.get("page", 0)),
        page_bbox=page_bbox,  # type: ignore[arg-type]
        blocks=blocks,
    )
    return NativePage(
        page_index=geometry.page_index,
        page_bbox=geometry.page_bbox,
        rotation=int(raw.get("rotation", 0)),
        geometry=geometry,
        raw=raw,
    )


def extract_tables_native(
    pdf_path: str,
    regions_by_page: dict[int, list[TableRegion]],
    *,
    img_size: tuple[int, int] = (1024, 1024),
) -> dict[int, list[str]]:
    """Run pdftext table_output on high-confidence regions.

    Args:
        pdf_path: path to the PDF.
        regions_by_page: page index → detected TableRegions (only
            regions at/above the confidence threshold should be passed).
        img_size: the ``img_size`` pdftext expects alongside coordinates.
            Coordinates here are in PDF points, so this is informational;
            kept explicit because pdftext requires the key.

    Returns:
        page index → list of extracted table texts (one per region).
    """
    _, table_output = _require_pdftext()
    if not regions_by_page:
        return {}
    table_inputs = []
    pages = sorted(regions_by_page)
    for page_index in pages:
        table_inputs.append(
            {
                "tables": [list(r.bbox) for r in regions_by_page[page_index]],
                "img_size": list(img_size),
            }
        )
    raw = table_output(pdf_path, table_inputs, page_range=pages)
    # pdftext returns per-page lists aligned with page_range order.
    result: dict[int, list[str]] = {}
    if isinstance(raw, list):
        for page_index, page_tables in zip(pages, raw):
            result[page_index] = [str(t) for t in (page_tables or [])]
    elif isinstance(raw, dict):
        for page_index in pages:
            result[page_index] = [str(t) for t in raw.get(page_index, [])]
    return result


def render_page_png(
    pdf_path: str, page_index: int, *, longest_dim_px: int = 1288
) -> bytes:
    """Render one page to PNG bytes (for the visual path / page-image records).

    Uses pypdfium2 directly in the *calling* process — rendering is
    read-only and short-lived; bulk extraction still goes through
    :func:`extract_pages_native`.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "pypdfium2 is required for page rendering but is not installed."
        ) from exc
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        page = pdf[page_index]
        # Scale so the longest dimension equals longest_dim_px.
        width_pt, height_pt = page.get_size()
        scale = longest_dim_px / max(width_pt, height_pt)
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        import io

        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()


def pdftext_worker_threshold() -> int:
    """The per-worker page threshold pdftext uses before parallelizing."""
    return int(os.environ.get("PDFTEXT_WORKER_PAGE_THRESHOLD", 10))
