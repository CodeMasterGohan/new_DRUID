"""Canonical tree tests (spec §6): stable IDs, markdown derivation."""

from services.ingestion.tree import (
    Node,
    NodeType,
    build_tree_from_olmocr,
    collect_diagnostics,
    stable_node_id,
    tree_to_markdown,
)
from services.ingestion.olmocr_client import OlmOcrResult


def _result(page_index, markdown, quality="accepted"):
    return OlmOcrResult(
        page_index=page_index, markdown=markdown, metadata={},
        quality_status=quality, attempts=1, latency_s=0.1,
    )


def test_stable_node_ids_are_deterministic():
    a = stable_node_id("d", "v", 0, "paragraph", 1, "hello")
    b = stable_node_id("d", "v", 0, "paragraph", 1, "hello")
    c = stable_node_id("d", "v", 0, "paragraph", 2, "hello")
    assert a == b and a != c


def test_olmocr_markdown_builds_typed_tree():
    md = (
        "# Introduction\n\nSome prose here.\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\n"
        "```\nprint('hi')\n```\n"
    )
    doc = build_tree_from_olmocr("d", "v", [_result(0, md)], parser_revision="r1")
    assert doc.type == NodeType.DOCUMENT
    page = doc.children[0]
    kinds = [n.type for n in page.children]
    assert kinds == [NodeType.HEADING, NodeType.PARAGRAPH, NodeType.TABLE,
                     NodeType.CODE_BLOCK]
    table = page.children[2]
    assert table.data["headers"] == ["A", "B"]
    assert table.data["rows"] == [["1", "2"], ["3", "4"]]
    assert table.detection_provenance == "olmocr-routed"
    assert page.children[0].heading_path == ["Introduction"]
    # Heading context propagates to later siblings.
    assert page.children[1].heading_path == ["Introduction"]


def test_markdown_derivation_round_trip():
    md = "# T\n\nBody text.\n\n| A |\n|---|\n| 1 |\n"
    doc = build_tree_from_olmocr("d", "v", [_result(0, md)], parser_revision="r1")
    out = tree_to_markdown(doc)
    assert "## T" in out
    assert "Body text." in out
    assert "| A |" in out


def test_complex_table_serializes_as_json_not_lossy():
    md = "| A | B | C | D | E | F | G | H | I |\n" + "|---|" * 9 + "\n" + "|1|" * 9 + "\n"
    doc = build_tree_from_olmocr("d", "v", [_result(0, md)], parser_revision="r1")
    table = doc.children[0].children[0]
    assert table.data["is_simple"] is False
    out = tree_to_markdown(doc)
    assert "```json" in out


def test_tree_serializes_to_dict_and_back():
    doc = build_tree_from_olmocr("d", "v", [_result(0, "# H\n\np")],
                                 parser_revision="r1")
    restored = Node.from_dict(doc.to_dict())
    assert restored.to_dict() == doc.to_dict()


def test_diagnostics_shape():
    doc = build_tree_from_olmocr("d", "v", [_result(0, "hi")], parser_revision="r1")
    diag = collect_diagnostics(doc, {0: {"route": "visual"}})
    assert diag["pages"][0]["routing"] == {"route": "visual"}
    assert diag["parser_revision"] == "r1"
