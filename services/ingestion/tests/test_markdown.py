from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.ingestion import pipeline
from services.ingestion.config import IngestionConfig
from services.ingestion.pipeline import MemoryObjectStore, PipelineDeps


@pytest.mark.parametrize('mode', ['cpu', 'gpu'])
def test_markdown_parses_without_pdf_or_ocr(mode):
    store = MemoryObjectStore()
    store.put_bytes('original', b'# GitLab CI\n\nUse include:local.\n\n```yaml\ninclude: local.yml\n```\n')
    deps = PipelineDeps(config=IngestionConfig(processing_mode=mode), object_store=store,
                        token_len=lambda text: len(text.split()))
    result = pipeline._parse_document(
        Mock(), SimpleNamespace(payload={'pdf_key': 'original', 'content_type': 'text/markdown'}),
        SimpleNamespace(document_id='doc', version_number=1), 'tenant/doc/v1', deps, Mock())
    assert result['visual_pages'] == []
    tree = result['trees'][0]
    assert tree.parser == 'markdown'
    assert [node.type.value for node in tree.children[0].children] == [
        'heading', 'paragraph', 'code_block']
    chunks = pipeline._chunk_trees(result['trees'], deps)
    assert any('include:local' in chunk.text for chunk in chunks)
    assert all(chunk.parser == 'markdown' for chunk in chunks)
    assert store.get_bytes('tenant/doc/v1/trees/markdown.json')


def test_markdown_tables_have_source_provenance_and_stable_ids():
    from services.ingestion.tree import build_tree_from_markdown, NodeType

    text = '# GitLab\n\n| Key | Value |\n| --- | --- |\n| include | local |\n'
    tree = build_tree_from_markdown('doc', '1', text)
    assert tree.to_dict() == build_tree_from_markdown('doc', '1', text).to_dict()
    table = tree.children[0].children[-1]
    assert table.type == NodeType.TABLE
    assert table.parser == 'markdown'
    assert table.parser_revision == 'markdown-v1'
    assert table.detection_provenance is None
    assert table.bounding_box is None
    assert table.data['rows'] == [['include', 'local']]
