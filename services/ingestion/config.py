"""Central configuration for ingestion (spec §5, §7, §8).

All router thresholds are named, documented, and marked PROVISIONAL: they
are initial values to be calibrated against the M1 eval corpus (§5
"Milestone M1"). No magic numbers are buried in routing logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RouterThresholds:
    """Page-router decision thresholds (§5).

    Every value is PROVISIONAL — calibrate against the M1 eval corpus
    before the router ships (target: ≥95% page-level routing accuracy vs
    human labels). Changing a threshold must not require a code change
    anywhere else; the router reads only this object.
    """

    # Text density: extracted chars per square inch of page area.
    # A dense text page is ~30 chars/in²; scanned/broken pages ≈ 0.
    min_text_density_chars_per_sqin: float = 8.0  # PROVISIONAL (M1)

    # Fraction of extracted chars that are U+FFFD or C0 controls (excl. \t\n).
    max_replacement_char_ratio: float = 0.02  # PROVISIONAL (M1)

    # Fraction of chars in Unicode private-use area (broken ToUnicode maps).
    max_private_use_ratio: float = 0.05  # PROVISIONAL (M1)

    # Fraction of lines whose geometric order disagrees with extraction
    # order (reading-order ambiguity signal).
    max_order_disagreement_ratio: float = 0.35  # PROVISIONAL (M1)

    # Minimum columns before a page counts as multi-column complex.
    multicolumn_min_columns: int = 2

    # Fraction of page area covered by high-confidence table regions
    # before the whole page is considered table-heavy (routes to olmOCR
    # when native table confidence is low).
    table_area_ratio_heavy: float = 0.40  # PROVISIONAL (M1)

    # Table-region confidence below this → page goes to olmOCR instead of
    # native table_output (§5 table-region detection).
    min_table_confidence: float = 0.75  # PROVISIONAL (M1)


@dataclass(frozen=True)
class ChunkerSettings:
    """Structural chunking targets (§7)."""

    prose_target_tokens: int = 420
    prose_overlap_tokens: int = 48
    reranker_max_input_tokens: int = 512
    # Query budget reserved inside the reranker window. The HARD RULE is:
    # token_len(chunk) <= reranker_max_input_tokens - max_query_tokens.
    max_query_tokens: int = 64  # PROVISIONAL (M1)


@dataclass(frozen=True)
class OlmOcrSettings:
    """Visual-path serving configuration (§5)."""

    checkpoint: str = "allenai/olmOCR-2-7B-1025-FP8"
    render_longest_dim_px: int = 1288
    temperature: float = 0.1
    max_new_tokens: int = 8192
    request_timeout_s: float = 300.0
    max_retries: int = 2


@dataclass(frozen=True)
class EmbeddingSettings:
    """EmbeddingGemma 2 configuration (§8)."""

    model_id: str = "google/embeddinggemma-2"
    modality_config: str = "text+image"  # fallback: "text-only"
    dimension: int = 768
    normalize: bool = True
    # Matryoshka widths permitted only after M1 validation (§8).
    allowed_matryoshka_widths: tuple[int, ...] = (768, 512, 256, 128)


@dataclass(frozen=True)
class IngestionConfig:
    """Root ingestion configuration, loadable from environment."""

    router: RouterThresholds = field(default_factory=RouterThresholds)
    chunker: ChunkerSettings = field(default_factory=ChunkerSettings)
    olmocr: OlmOcrSettings = field(default_factory=OlmOcrSettings)
    embeddings: EmbeddingSettings = field(default_factory=EmbeddingSettings)

    # pdftext parallel extraction only engages when each worker gets at
    # least this many pages (pdftext default; overridable via
    # PDFTEXT_WORKER_PAGE_THRESHOLD). Below it, extraction is serial —
    # still in a process, never a thread.
    pdftext_worker_page_threshold: int = 10
    pdftext_workers: int = 4

    # Object-storage key prefixes (tenant-scoped at call time).
    storage_prefix_originals: str = "originals"
    storage_prefix_renders: str = "renders"
    storage_prefix_trees: str = "trees"

    @classmethod
    def from_env(cls) -> "IngestionConfig":
        """Build from environment with documented defaults."""
        import os

        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except ValueError:
                return default

        router = RouterThresholds(
            min_text_density_chars_per_sqin=_float(
                "ROUTER_MIN_TEXT_DENSITY", 8.0
            ),
            max_replacement_char_ratio=_float("ROUTER_MAX_REPLACEMENT_RATIO", 0.02),
            max_private_use_ratio=_float("ROUTER_MAX_PRIVATE_USE_RATIO", 0.05),
            max_order_disagreement_ratio=_float("ROUTER_MAX_ORDER_DISAGREEMENT", 0.35),
            min_table_confidence=_float("ROUTER_MIN_TABLE_CONFIDENCE", 0.75),
        )
        chunker = ChunkerSettings(
            prose_target_tokens=_int("CHUNK_TARGET_TOKENS", 420),
            prose_overlap_tokens=_int("CHUNK_OVERLAP_TOKENS", 48),
        )
        return cls(
            router=router,
            chunker=chunker,
            pdftext_workers=_int("PDFTEXT_WORKERS", 4),
        )
