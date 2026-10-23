"""CPU mode uses real PDF extraction; OCR inference must never be called."""
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.ingestion import pipeline
from services.ingestion.config import IngestionConfig
from services.ingestion.pipeline import MemoryObjectStore, PipelineDeps, _ReviewRequired


@pytest.mark.parametrize('mode', ['cpu', 'gpu'])
def test_processing_mode_from_env(monkeypatch, mode):
    monkeypatch.setenv('DRUID_PROCESSING_MODE', mode)
    assert IngestionConfig.from_env().processing_mode == mode


def test_processing_mode_defaults_to_existing_gpu_behavior(monkeypatch):
    monkeypatch.delenv('DRUID_PROCESSING_MODE', raising=False)
    assert IngestionConfig.from_env().processing_mode == 'gpu'


def test_invalid_processing_mode_is_rejected(monkeypatch):
    monkeypatch.setenv('DRUID_PROCESSING_MODE', 'automatic')
    with pytest.raises(ValueError, match='DRUID_PROCESSING_MODE'):
        IngestionConfig.from_env()


def test_production_worker_reads_processing_mode(monkeypatch):
    from services.ingestion import embeddings, milvus_sink

    for name in ('EMBEDDING_MODEL_PATH', 'MINILM_TOKENIZER_PATH', 'RUSTFS_ENDPOINT',
                 'RUSTFS_ACCESS_KEY', 'RUSTFS_SECRET_KEY', 'RUSTFS_BUCKET', 'MILVUS_URI'):
        monkeypatch.setenv(name, 'test-fixture')
    monkeypatch.setenv('DRUID_PROCESSING_MODE', 'cpu')
    monkeypatch.setattr(embeddings, 'SentenceTransformersGemmaBackend', Mock())
    monkeypatch.setattr(milvus_sink, 'MilvusIndexSink', Mock())
    monkeypatch.setattr(pipeline, 'RustFSObjectStore', Mock())
    assert pipeline.build_production_deps().config.processing_mode == 'cpu'


def _pdf_bytes(lines):
    pytest.importorskip('pdftext')
    pytest.importorskip('pypdfium2')
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    commands = ['BT /F1 10 Tf 12 TL 50 740 Td']
    commands += [f'({line}) Tj T*' for line in lines]
    commands.append('ET')
    stream.set_data('\n'.join(commands).encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _parse(lines, monkeypatch, *, mode='cpu', ocr_result=None):
    data = _pdf_bytes(lines)
    store = MemoryObjectStore()
    store.put_bytes('original.pdf', data)
    config = IngestionConfig(processing_mode=mode, pdftext_workers=1)
    deps = PipelineDeps(config=config, object_store=store)
    repo = Mock()
    repo.pending_quarantine_count.return_value = 0
    session = Mock()
    job = SimpleNamespace(id='job', payload={'pdf_key': 'original.pdf'})
    version = SimpleNamespace(id='version', document_id='document', version_number=1)
    ocr = Mock(side_effect=AssertionError('CPU mode contacted OCR')) if mode == 'cpu' else Mock()
    if mode == 'gpu':
        ocr.return_value.process_page.return_value = ocr_result
    monkeypatch.setattr(pipeline, 'OlmOcrClient', ocr)
    return repo, session, store, ocr, lambda: pipeline._parse_document(
        repo, job, version, 'tenant/document/version', deps, session)


def test_cpu_mode_parses_actual_text_pdf_without_ocr(monkeypatch):
    lines = ['Native CPU document content for extraction and search testing.'] * 24
    repo, session, store, ocr, parse = _parse(lines, monkeypatch)
    result = parse()
    assert result['trees']
    assert result['visual_pages'] == []
    assert any('Native CPU document' in value.decode() for key, value in store._data.items()
               if key.endswith('.md'))
    ocr.assert_not_called()
    repo.quarantine_page.assert_not_called()


def test_cpu_mode_defers_actual_sparse_pdf_without_ocr(monkeypatch):
    repo, session, store, ocr, parse = _parse(['Sparse page needs OCR'], monkeypatch)
    with pytest.raises(_ReviewRequired, match='CPU mode'):
        parse()
    ocr.assert_not_called()
    item = repo.quarantine_page.call_args.kwargs
    assert item['parse_attempts']['visual']['status'] == 'deferred'
    assert item['routing_diagnostics']['route'] == 'visual'
    assert store.get_bytes(item['page_render_ref']).startswith(b'\x89PNG')
    session.commit.assert_called()


def test_gpu_mode_keeps_visual_ocr_route(monkeypatch):
    from services.ingestion.olmocr_client import OlmOcrResult

    result = OlmOcrResult(page_index=0, markdown='OCR output', metadata={},
        quality_status='accepted', attempts=1, latency_s=0.1)
    repo, session, store, ocr, parse = _parse(['Sparse page needs OCR'], monkeypatch,
        mode='gpu', ocr_result=result)
    assert parse()['visual_pages'] == [0]
    ocr.return_value.process_page.assert_called_once()
    ocr.return_value.close.assert_called_once()
    repo.quarantine_page.assert_not_called()


def test_cpu_mode_parks_tables_when_native_table_api_is_unavailable(monkeypatch):
    from services.ingestion.tables import TableRegion

    extract = pipeline.extract_pages_native

    def with_table(*args, **kwargs):
        pages = extract(*args, **kwargs)
        pages[0].geometry.table_regions = [TableRegion(
            bbox=(50, 50, 500, 500), confidence=1.0, est_rows=4, est_cols=2,
            evidence=['fixture-grid'])]
        return pages

    monkeypatch.setattr(pipeline, 'extract_pages_native', with_table)
    monkeypatch.setattr(pipeline, 'supports_native_tables', lambda: False)
    repo, session, store, ocr, parse = _parse(
        ['Native document content for table capability routing.'] * 24, monkeypatch)
    with pytest.raises(_ReviewRequired):
        parse()
    diagnostics = repo.quarantine_page.call_args.kwargs['routing_diagnostics']
    assert 'native_table_extraction_unavailable' in diagnostics['reasons']
    ocr.assert_not_called()
