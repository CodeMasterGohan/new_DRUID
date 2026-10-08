"""EmbeddingGemma 2 task-aware query prompts (spec §8).

The task prefix is contractual: the wrong prefix is a quality bug, not a
cosmetic choice. The embedding service applies the matching *document*
prefix at index time; this module builds the *query* side.
"""

from __future__ import annotations

import re

TASK_QUESTION_ANSWERING = "question_answering"
TASK_CODE_RETRIEVAL = "code_retrieval"
TASK_SEARCH_RESULT = "search_result"

_VALID_TASKS = frozenset(
    {TASK_QUESTION_ANSWERING, TASK_CODE_RETRIEVAL, TASK_SEARCH_RESULT}
)

# Heuristic signals that a query is asking about code. Conservative on
# purpose: false "question answering" is the safe default, and callers may
# always pass an explicit task_hint.
_CODE_PATTERNS = (
    re.compile(r"`"),  # inline or fenced code
    # NOTE: `return` deliberately excluded — "return policy" is prose.
    re.compile(r"\b(def|class|function|fn|import|from|const|let|var)\s+\w+"),
    re.compile(r"=>"),
    re.compile(r"\w+\([^)]*\)\s*{"),
    re.compile(r";\s*$", re.MULTILINE),
)


def build_query_prompt(query: str, task: str = TASK_QUESTION_ANSWERING) -> str:
    """Render the contractual EmbeddingGemma 2 query prompt for ``task``."""
    if task not in _VALID_TASKS:
        raise ValueError(f"unknown embedding task {task!r}")
    query = query.strip()
    if task == TASK_CODE_RETRIEVAL:
        return f"task: code retrieval | query: {query}"
    if task == TASK_SEARCH_RESULT:
        return f"task: search result | query: {query}"
    return f"task: question answering | query: {query}"


def detect_task(query: str, task_hint: str | None = None) -> str:
    """Choose the query task: explicit hint wins, else a code heuristic."""
    if task_hint is not None:
        if task_hint not in _VALID_TASKS:
            raise ValueError(f"unknown task_hint {task_hint!r}")
        return task_hint
    if any(p.search(query) for p in _CODE_PATTERNS):
        return TASK_CODE_RETRIEVAL
    return TASK_QUESTION_ANSWERING
