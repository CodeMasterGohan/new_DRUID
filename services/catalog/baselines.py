"""Import reviewed corpus measurements without weakening activation checks.

Usage: python -m services.catalog.baselines measurements.json
The manifest must describe the exact parser revision and contain measured
page/chunk counts. Bounds are derived from those samples, never guessed.
"""
import argparse
import hashlib
import json
import math
import os
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from .models import ParserRevisionStats


def import_baseline(session, manifest):
    parser, revision = manifest['parser'], manifest['parser_revision']
    samples = manifest['samples']
    if not isinstance(parser, str) or not parser or len(parser) > 32:
        raise ValueError('invalid parser')
    if not isinstance(revision, str) or not revision or len(revision) > 64:
        raise ValueError('invalid parser_revision')
    if not isinstance(samples, list) or not samples:
        raise ValueError('measured corpus samples are required')
    ratios = []
    seen = set()
    for sample in samples:
        digest = sample['sha256']
        pages, chunks = sample['pages'], sample['chunks']
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('each sample requires its source SHA-256')
        if digest in seen:
            raise ValueError('duplicate corpus sample')
        seen.add(digest)
        if type(pages) is not int or type(chunks) is not int or pages < 1 or chunks < 1:
            raise ValueError('pages and chunks must be positive integers')
        ratios.append(chunks / pages)
    if not all(math.isfinite(r) for r in ratios):
        raise ValueError('invalid measured ratio')
    row = session.scalar(select(ParserRevisionStats).where(
        ParserRevisionStats.parser == parser, ParserRevisionStats.parser_revision == revision))
    values = (min(ratios), max(ratios), len(samples))
    if row is not None:
        if (row.min_chunks_per_page, row.max_chunks_per_page, row.sample_count) != values:
            raise ValueError('baseline is immutable; use a new parser revision for changed calibration')
        return row
    row = ParserRevisionStats(parser=parser, parser_revision=revision,
        min_chunks_per_page=values[0], max_chunks_per_page=values[1], sample_count=values[2])
    session.add(row)
    session.flush()
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    args = parser.parse_args()
    with open(args.manifest, 'rb') as f:
        raw = f.read()
    with Session(create_engine(os.environ['DATABASE_URL'])) as session:
        row = import_baseline(session, json.loads(raw))
        session.commit()
        print(json.dumps({'parser': row.parser, 'revision': row.parser_revision,
                          'samples': row.sample_count,
                          'manifest_sha256': hashlib.sha256(raw).hexdigest()}))


if __name__ == '__main__':
    main()
