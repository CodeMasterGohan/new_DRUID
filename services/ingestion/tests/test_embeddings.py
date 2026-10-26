"""Identifier extraction + embedding prefix-contract tests (§8, §9)."""

from services.ingestion.chunker import Chunk
from services.ingestion.embeddings import (
    MockEmbeddingBackend,
    PrefixContract,
    build_document_text,
    build_page_image_label,
    build_query_text,
    make_page_image_record,
)
from services.ingestion.identifiers import IdentifierExtractor, extract_identifiers


def test_production_vectors_normalize_after_low_precision_encode(tmp_path):
    import math
    from types import SimpleNamespace
    from services.ingestion.config import EmbeddingSettings
    from services.ingestion.embeddings import SentenceTransformersGemmaBackend

    # A representative BF16-normalized row can still drift past the API's
    # 1e-3 L2 tolerance after conversion to ordinary Python floats.
    raw = [1.001953125] + [0.0] * 767
    backend = SentenceTransformersGemmaBackend(EmbeddingSettings(), str(tmp_path))
    backend._model = SimpleNamespace(encode=lambda *args, **kwargs: [raw])
    vector = backend.embed_texts(['query'])[0]
    assert len(vector) == 768
    assert abs(math.sqrt(sum(value*value for value in vector))-1) < 1e-6


def _chunk(text, content_type=None, heading_path=None):
    return Chunk(
        chunk_id="c1", document_id="d", document_version="v1", parent_id="n1",
        page_start=0, page_end=0, chunk_order=0,
        heading_path=heading_path or ["Intro"], text=text, identifiers=[],
        metadata={"content_type": content_type} if content_type else {},
    )


# --- identifiers ---


def test_part_number_extracted():
    ids = extract_identifiers("Replace gear 23441-HM8-B60 and fork 24213-HM8-A40.")
    assert "23441-HM8-B60" in ids and "24213-HM8-A40" in ids


def test_invoice_and_clause_refs_extracted():
    ids = extract_identifiers("Invoice INV-2024-00123 per § 4.2(a), see Section 12.3.")
    assert "INV-2024-00123" in ids
    assert any("4.2" in i for i in ids)


def test_identifiers_deduplicated_in_order():
    ids = extract_identifiers(
        "INV-2024-00123, then 23441-HM8-B60, then INV-2024-00123 again")
    assert ids.count("INV-2024-00123") == 1
    assert ids.index("INV-2024-00123") < ids.index("23441-HM8-B60")


def test_custom_patterns_per_tenant():
    extractor = IdentifierExtractor(patterns={"ticket": r"\bTKT-\d{5}\b"})
    assert extractor.extract("see TKT-12345") == ["TKT-12345"]


def test_no_false_positives_on_prose():
    assert extract_identifiers("The quick brown fox jumps.") == []


# --- prefix contract (contractual per §8) ---


def test_prose_document_prefix():
    text = build_document_text(_chunk("body text here"))
    assert text == "title: Intro | text: body text here"


def test_code_chunk_uses_code_prefix_not_qa():
    text = build_document_text(_chunk("def f(): pass", content_type="code"))
    assert text.startswith("title: ")
    assert "| text: def f(): pass" in text
    assert "question answering" not in text


def test_query_prefixes():
    assert build_query_text("what is X?") == \
        "task: question answering | query: what is X?"
    assert build_query_text("sort fn", task="code") == \
        "task: code retrieval | query: sort fn"
    assert build_query_text("honda gear", task="search") == \
        "task: search result | query: honda gear"


def test_prefix_contract_constants_match_spec():
    assert PrefixContract.QUERY_QA == "task: question answering | query: {query}"
    assert PrefixContract.DOC_PROSE_TITLE == "title: {heading} | text: {text}"


# --- page image records ---


def test_page_image_label_is_label_not_ocr():
    label = build_page_image_label(filename="invoice.pdf", page_index=2)
    assert label == "Page 3 of invoice.pdf"


def test_page_image_record_shape():
    rec = make_page_image_record(
        document_id="d", document_version="v1", page_index=4,
        render_ref="t/d/v/renders/page-0004.png", filename="invoice.pdf",
        vector=[0.1] * 768, group_id="tenant-a",
    )
    milvus = rec.to_milvus_record()
    assert milvus["page_start"] == milvus["page_end"] == 4
    assert milvus["metadata"]["modality"] == "page_image"
    assert milvus["text"] == "Page 5 of invoice.pdf"  # label, not OCR
    assert milvus["identifiers"] == []


def test_mock_backend_deterministic_and_normalized():
    backend = MockEmbeddingBackend()
    a = backend.embed_texts(["hello world"])
    b = backend.embed_texts(["hello world"])
    assert a == b
    import math

    norm = math.sqrt(sum(v * v for v in a[0]))
    assert abs(norm - 1.0) < 1e-6
    assert len(a[0]) == 768
    # Image embeddings live in the same space (same dimension).
    assert len(backend.embed_images([b"png-bytes"])[0]) == 768
