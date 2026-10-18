"""Rank fusion: reciprocal rank fusion + identifier boost (§10 step 8)."""

from __future__ import annotations

from .models import FusedCandidate, ScoredHit


def reciprocal_rank_fusion(
    branches: dict[str, list[ScoredHit]],
    k: int = 60,
) -> list[FusedCandidate]:
    """Fuse branch rankings with RRF: ``score = sum(1 / (k + rank))``.

    ``branches`` maps branch name ("dense", "bm25", "identifier") to its
    ranked hits. Candidates are deduplicated by ``chunk_id``; ranks are the
    1-based positions inside each branch list.
    """
    scores: dict[str, float] = {}
    records: dict[str, ScoredHit] = {}
    contributors: dict[str, set[str]] = {}
    for branch_name, hits in branches.items():
        for position, hit in enumerate(hits, start=1):
            rank = hit.rank if hit.rank and hit.rank > 0 else position
            key = hit.record.chunk_id
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            records.setdefault(key, hit)
            contributors.setdefault(key, set()).add(branch_name)
    fused = [
        FusedCandidate(
            record=records[key].record,
            fusion_score=score,
            branches=tuple(sorted(contributors[key])),
        )
        for key, score in scores.items()
    ]
    fused.sort(key=lambda c: c.fusion_score, reverse=True)
    return fused


def apply_identifier_boost(
    fused: list[FusedCandidate],
    identifier_hits: list[ScoredHit],
    boost: float,
) -> list[FusedCandidate]:
    """Merge identifier-branch hits with a fixed additive boost.

    Identifier hits already present get ``+ boost`` on their fusion score;
    identifier-only hits enter the pool at ``boost + 1/(k + rank)`` so a
    literal match is never lost to RRF dilution.
    """
    if boost == 0 or not identifier_hits:
        return sorted(fused, key=lambda c: c.fusion_score, reverse=True)
    by_id = {c.record.chunk_id: c for c in fused}
    for position, hit in enumerate(identifier_hits, start=1):
        rank = hit.rank if hit.rank and hit.rank > 0 else position
        key = hit.record.chunk_id
        existing = by_id.get(key)
        if existing is None:
            by_id[key] = FusedCandidate(
                record=hit.record,
                fusion_score=boost + 1.0 / (60 + rank),
                branches=("identifier",),
            )
        else:
            by_id[key] = FusedCandidate(
                record=existing.record,
                fusion_score=existing.fusion_score + boost,
                branches=tuple(sorted(set(existing.branches) | {"identifier"})),
            )
    return sorted(by_id.values(), key=lambda c: c.fusion_score, reverse=True)
