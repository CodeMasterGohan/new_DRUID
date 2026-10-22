import pytest
from services.ingestion.milvus_sink import MilvusIndexSink


def test_upserts_are_bounded_and_partial_failure_is_not_success(monkeypatch):
    monkeypatch.setenv('MILVUS_UPSERT_BATCH_SIZE', '2')
    class Client:
        sizes = []
        fail = False
        def upsert(self, *, data, **kwargs):
            self.sizes.append(len(data))
            return {'upsert_count': 0 if self.fail else len(data)}
    client = Client()
    sink = MilvusIndexSink('http://test', 'chunks', client)
    assert sink.index_chunks([{'chunk_id': str(i)} for i in range(5)]) == 5
    assert client.sizes == [2,2,1]
    client.fail = True
    with pytest.raises(RuntimeError, match='mismatch'):
        sink.index_chunks([{'chunk_id':'a'}])
