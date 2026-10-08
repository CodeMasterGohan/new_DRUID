"""Local CPU rerank of TEXT records only (§10 step 9).

Page-image records carry no rerankable text (their ``text`` field is a page
label, not content). They enter fusion on dense rank only and are never
passed to the cross-encoder.
"""

from __future__ import annotations

from .interfaces import Reranker
from .models import FusedCandidate

# Conservative chars-per-token estimate for the budget guard. Production
# should enforce the budget with the MiniLM tokenizer; this guard only
# prevents pathological inputs from breaking the cross-encoder call.
_CHARS_PER_TOKEN = 4
MINILM_MAX_INPUT_TOKENS = 512
# Reserve room for the query + separators; chunking already targets 420.
MAX_PASSAGE_CHARS = 420 * _CHARS_PER_TOKEN


def truncate_to_budget(text: str, max_chars: int = MAX_PASSAGE_CHARS) -> str:
    """Hard-truncate a passage so query + passage fits the reranker window."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0]


def rerank_text_candidates(
    query: str,
    candidates: list[FusedCandidate],
    reranker: Reranker,
) -> list[tuple[FusedCandidate, float]]:
    """Rerank text-modality candidates; return (candidate, score) desc.

    Only ``modality == "text"`` candidates are scored. Page-image records
    are excluded here by construction — the caller merges them back by
    fusion score.
    """
    text_only = [c for c in candidates if c.record.modality == "text"]
    if not text_only:
        return []
    passages = [truncate_to_budget(c.record.text) for c in text_only]
    scores = reranker.score(query, passages)
    if len(scores) != len(text_only):
        raise ValueError(
            f"reranker returned {len(scores)} scores for {len(text_only)} passages"
        )
    ranked = sorted(
        zip(text_only, scores), key=lambda pair: pair[1], reverse=True
    )
    return ranked
