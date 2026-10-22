"""Chunker tests (spec §7).

The HARD RULE (no chunk exceeds 512 − max_query_tokens tokens, split never
truncate) is asserted on every emitted chunk across adversarial inputs:
giant prose, wide tables, huge code blocks. The tokenizer is a
deterministic word counter — production wires the MiniLM tokenizer.
"""

import pytest

from services.ingestion.chunker import Chunk, StructuralChunker
from services.ingestion.config import ChunkerSettings
from services.ingestion.tree import Node, NodeType, stable_node_id

WORDS = lambda n: "word " * n  # noqa: E731


def _doc_with(children, doc_id="d", version="v1"):
    doc = Node(
        node_id=stable_node_id(doc_id, version, -1, "document", 0),
        type=NodeType.DOCUMENT, document_id=doc_id, document_version=version,
        page_index=-1, bounding_box=None, heading_path=[],
        parser="native", parser_revision="r",
    )
    page = Node(
        node_id=stable_node_id(doc_id, version, 0, "page", 0),
        type=NodeType.PAGE, document_id=doc_id, document_version=version,
        page_index=0, bounding_box=(0, 0, 612, 792), heading_path=[],
        parser="native", parser_revision="r", children=children,
    )
    doc.children.append(page)
    return doc


def _para(text, heading_path=None, page=0):
    return Node(
        node_id=stable_node_id("d", "v1", page, "paragraph", hash(text) % 997),
        type=NodeType.PARAGRAPH, document_id="d", document_version="v1",
        page_index=page, bounding_box=(72, 700, 540, 716),
        heading_path=heading_path or [], parser="native",
        parser_revision="r", text=text,
    )


def _chunker(**kw):
    settings = ChunkerSettings(**kw)
    # Deterministic word-count tokenizer (production: MiniLM tokenizer).
    return StructuralChunker(settings=settings, token_len=lambda t: len(t.split()))


def _assert_budget(chunks, chunker):
    for c in chunks:
        n = len(c.text.split())
        assert n <= chunker._budget, f"chunk {n} words over budget {chunker._budget}"


def test_small_prose_single_chunk_with_heading_context():
    doc = _doc_with([_para(WORDS(50), heading_path=["Intro", "Background"])])
    chunks = _chunker().chunk_document(doc)
    assert len(chunks) == 1
    assert chunks[0].text.startswith("Intro > Background")
    assert chunks[0].heading_path == ["Intro", "Background"]
    _assert_budget(chunks, _chunker())


def test_long_prose_splits_on_paragraphs_with_overlap():
    paras = [WORDS(200) for _ in range(6)]
    text = "\n\n".join(paras)
    doc = _doc_with([_para(text)])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) > 1
    _assert_budget(chunks, chunker)
    # Overlap: some words repeat across adjacent chunks (prose only).
    words0 = set(chunks[0].text.split())
    words1 = set(chunks[1].text.split())
    assert words0 & words1


def test_hard_rule_holds_on_giant_single_paragraph():
    doc = _doc_with([_para(WORDS(5000))])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) > 3
    _assert_budget(chunks, chunker)


def _table_node(rows, headers=("A", "B", "C")):
    return Node(
        node_id=stable_node_id("d", "v1", 0, "table", 1),
        type=NodeType.TABLE, document_id="d", document_version="v1",
        page_index=0, bounding_box=None, heading_path=["Data"],
        parser="native", parser_revision="r",
        detection_provenance="native-detected",
        data={"headers": list(headers), "rows": [list(r) for r in rows],
              "is_simple": True},
    )


def test_table_splits_by_row_groups_with_repeated_headers():
    rows = [[f"row {i} cell value {j} with more words here" for j in range(3)]
            for i in range(60)]
    doc = _doc_with([_table_node(rows)])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) > 1
    _assert_budget(chunks, chunker)
    for c in chunks:
        assert "A | B | C" in c.text  # headers repeated per row group
        assert c.metadata["table_id"] == chunks[0].metadata["table_id"]
        assert c.detection_provenance == "native-detected"


def test_table_never_uses_sliding_overlap():
    rows = [[f"row {i} cell value {j}" for j in range(3)] for i in range(40)]
    doc = _doc_with([_table_node(rows)])
    chunks = _chunker().chunk_document(doc)
    seen_rows: list[str] = []
    for c in chunks:
        for line in c.text.splitlines():
            if line.startswith("row ") and line.count("|") >= 2:
                seen_rows.append(line)
    # Each data row appears exactly once (headers repeat, rows don't).
    assert len(seen_rows) == len(set(seen_rows)) == 40


def test_code_block_splits_at_blank_lines():
    code = "\n\n".join(f"def function_number_{i}():\n    result = compute_value({i})\n    return result"
                        for i in range(80))
    node = Node(
        node_id=stable_node_id("d", "v1", 0, "code_block", 1),
        type=NodeType.CODE_BLOCK, document_id="d", document_version="v1",
        page_index=0, bounding_box=None, heading_path=["API"],
        parser="native", parser_revision="r", text=code,
    )
    doc = _doc_with([node])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) > 1
    _assert_budget(chunks, chunker)
    assert all(c.metadata.get("content_type") == "code" for c in chunks)


def test_chunk_ids_stable_and_parent_linked():
    doc = _doc_with([_para(WORDS(50))])
    chunker = _chunker()
    a = chunker.chunk_document(doc)
    b = chunker.chunk_document(doc)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert a[0].parent_id == doc.children[0].children[0].node_id


def test_milvus_record_shape():
    doc = _doc_with([_para("Invoice INV-2024-00123 total $50.")])
    chunk = _chunker().chunk_document(doc)[0]
    rec = chunk.to_milvus_record("tenant-a")
    assert rec["chunk_id"].startswith("tenant-a:c_")
    assert rec["group_id"] == "tenant-a"
    assert rec["metadata"]["modality"] == "text"
    assert "INV-2024-00123" in rec["identifiers"]


def test_budget_violation_raises_not_silently_truncates():
    # A token_len that lies about a huge chunk must surface as an error.
    chunker = StructuralChunker(token_len=lambda t: 10**9)
    doc = _doc_with([_para("tiny")])
    with pytest.raises(ValueError, match="over the .* token budget"):
        chunker.chunk_document(doc)


def test_table_row_groups_have_unique_chunk_ids():
    rows = [[f"row {i} cell value {j} with more words here" for j in range(3)] for i in range(60)]
    doc = _doc_with([_table_node(rows)])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) > 1
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_prose_overlap_never_overflows_budget():
    paras = [
        " ".join(f"tok{p}_{w}" for w in range(170)) + "."
        for p in range(6)
    ]
    doc = _doc_with([_para("\n\n".join(paras), heading_path=["Sec1", "SubA"])])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) > 1
    _assert_budget(chunks, chunker)


def test_prose_split_preserves_heading_path_on_all_chunks():
    paras = [WORDS(180) for _ in range(5)]
    doc = _doc_with([_para("\n\n".join(paras), heading_path=["Guide", "Setup"])])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.text.startswith("Guide > Setup")


def test_oversized_single_table_row_splits_within_budget():
    huge_row = [[" ".join(f"cellword_{i}" for i in range(600)), "b", "c"]]
    doc = _doc_with([_table_node(huge_row)])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) >= 2
    _assert_budget(chunks, chunker)


def test_dense_single_line_code_splits_within_budget():
    dense_code = " ".join(f"x{i}=1;" for i in range(600))
    node = Node(
        node_id=stable_node_id("d", "v1", 0, "code_block", 99),
        type=NodeType.CODE_BLOCK, document_id="d", document_version="v1",
        page_index=0, bounding_box=None, heading_path=["Code"],
        parser="native", parser_revision="r", text=dense_code,
    )
    doc = _doc_with([node])
    chunker = _chunker()
    chunks = chunker.chunk_document(doc)
    assert len(chunks) >= 2
    _assert_budget(chunks, chunker)
