"""Mixed result budget must preserve at least one relevant page image."""
from services.retrieval.tests.test_retrieval import (
    FakeStore, make_context, make_record, make_service
)


def test_page_image_survives_text_saturated_top_k():
    texts = [make_record(f"t{i}") for i in range(12)]
    image = make_record("diagram", modality="page_image", text="page rendering")
    store = FakeStore(dense=texts + [image], bm25=texts)
    svc = make_service(store=store, final_results=8, max_page_image_results=2)
    results = svc.search(make_context(), "diagram of the gearbox")
    assert len(results) == 8
    assert [r.chunk_id for r in results].count("diagram") == 1
    assert results[-1].modality == "page_image"
    assert [r.rank for r in results] == list(range(1, 9))


def test_single_slot_prefers_ranked_text_when_both_present():
    text = make_record("text")
    image = make_record("image", modality="page_image")
    store = FakeStore(dense=[text, image])
    svc = make_service(store=store, final_results=1)
    results = svc.search(make_context(), "question")
    assert [r.chunk_id for r in results] == ["text"]
