import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from services.catalog.models import Base
from services.catalog.baselines import import_baseline
from services.catalog.activation import get_expected_chunk_range
from services.catalog.errors import ActivationError


def test_missing_baseline_fails_closed_then_measured_import_is_idempotent():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(ActivationError):
            get_expected_chunk_range(session, parser='mixed', parser_revision='router-v1', page_count=2)
        manifest = {'parser': 'mixed', 'parser_revision': 'router-v1',
                    'samples': [{'sha256': 'a'*64, 'pages': 2, 'chunks': 6}]}
        row = import_baseline(session, manifest)
        session.commit()
        assert import_baseline(session, manifest).id == row.id
        assert get_expected_chunk_range(session, parser='mixed', parser_revision='router-v1', page_count=2) == (6,6)
        manifest['samples'][0]['chunks'] = 8
        with pytest.raises(ValueError, match='immutable'):
            import_baseline(session, manifest)


@pytest.mark.parametrize('sample', [
    {'sha256': 'a'*64, 'pages': 0, 'chunks': 1},
    {'sha256': 'invalid', 'pages': 1, 'chunks': 1},
    {'sha256': 'a'*64, 'pages': 1, 'chunks': True},
])
def test_invalid_calibration_rejected(sample):
    with pytest.raises(ValueError):
        import_baseline(None, {'parser': 'mixed', 'parser_revision': 'router-v1', 'samples': [sample]})
