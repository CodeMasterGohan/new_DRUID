"""Live evaluation uses injected transports in CI; no synthetic quality claims."""
from services.retrieval.eval_live import evaluate, load_cases


def test_evaluator_counts_failed_requests_as_misses_and_detects_forbidden_leaks():
    cases = [
        {"query": "invoice details", "expected_document_id": "doc-a",
         "expected_version": "2", "forbidden_document_id": "doc-secret"},
        {"query": "other details", "expected_document_id": "doc-b",
         "expected_version": "1"},
    ]
    def fake(query):
        if query.startswith("other"):
            raise OSError("backend down")
        return {
            "results": [
                {"document_id": "doc-a", "document_version": "2",
                 "source_reference": "report.pdf (p. 1)",
                 "page_start": 1, "page_end": 1},
                {"document_id": "doc-secret", "document_version": "1",
                 "source_reference": "", "page_start": 1, "page_end": 1},
            ]
        }
    result = evaluate(cases, fake)
    assert result["document_recall_at_k"] == 0.5
    assert result["version_hit_at_k"] == 0.5
    assert result["citation_metadata_coverage"] == 0.5
    assert result["forbidden_document_leaks"] == 1
    assert len(result["request_failures"]) == 1


def test_evaluator_does_not_claim_recall_without_gold():
    result = evaluate([{"query": "hello"}], lambda q: {"results": []})
    assert result["document_recall_at_k"] is None
    assert result["version_hit_at_k"] is None
    assert result["citation_metadata_coverage"] is None
