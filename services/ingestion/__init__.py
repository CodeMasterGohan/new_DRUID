"""new_DRUID ingestion workers (spec §5, §6, §7, §8, §12).

Pipeline stages: native PDF extraction (process-based pdftext workers) →
page routing → table-region detection → visual path (olmOCR 2 FP8 via vLLM)
→ canonical typed document tree → structural chunking → EmbeddingGemma 2
embeddings → index sink.

HARD CONSTRAINT: PDFium is not thread-safe. All native extraction runs in
processes (pdftext's built-in ``workers=`` pool or our own process pool).
Never threads.
"""

from .chunker import Chunk, StructuralChunker
from .config import IngestionConfig, RouterThresholds
from .embeddings import (
    EmbeddingBackend,
    MockEmbeddingBackend,
    PrefixContract,
    SentenceTransformersGemmaBackend,
    build_document_text,
    build_query_text,
)
from .identifiers import IdentifierExtractor, extract_identifiers
from .native_worker import NativePage, extract_pages_native, extract_tables_native
from .olmocr_client import OlmOcrClient, OlmOcrConfig, OlmOcrResult
from .pipeline import IndexSink, LoggingIndexSink, ObjectStore, run_ingestion_job
from .router import PageRoute, RouterDecision, route_page
from .tables import TableRegion, detect_table_regions
from .tree import (
    Node,
    NodeType,
    build_tree_from_native,
    build_tree_from_olmocr,
    tree_to_markdown,
)

__all__ = [
    "Chunk",
    "StructuralChunker",
    "IngestionConfig",
    "RouterThresholds",
    "EmbeddingBackend",
    "MockEmbeddingBackend",
    "SentenceTransformersGemmaBackend",
    "PrefixContract",
    "build_document_text",
    "build_query_text",
    "IdentifierExtractor",
    "extract_identifiers",
    "NativePage",
    "extract_pages_native",
    "extract_tables_native",
    "OlmOcrClient",
    "OlmOcrConfig",
    "OlmOcrResult",
    "IndexSink",
    "LoggingIndexSink",
    "ObjectStore",
    "run_ingestion_job",
    "PageRoute",
    "RouterDecision",
    "route_page",
    "TableRegion",
    "detect_table_regions",
    "Node",
    "NodeType",
    "build_tree_from_native",
    "build_tree_from_olmocr",
    "tree_to_markdown",
]
