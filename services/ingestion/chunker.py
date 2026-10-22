"""Structural chunker (spec §7).

Traverses the canonical document tree and splits along structure —
heading boundaries, paragraph groups, lists, table boundaries, code
blocks — with tokenizer-aware sizing (never character counts).

HARD RULE (§7): no chunk's tokenized length (with the *reranker's*
tokenizer) may exceed ``reranker_max_input_tokens - max_query_tokens``.
Oversized chunks are split, never truncated. The rule is enforced by
assertion on every emitted chunk; a violation is a bug, not a warning.

The tokenizer is injected (``token_len`` callable) so the chunker never
downloads anything: production wires the pre-staged MiniLM tokenizer,
tests inject a deterministic counter.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache

from .config import ChunkerSettings
from .identifiers import IdentifierExtractor
from .tree import Node, NodeType


@dataclass
class Chunk:
    """One retrieval unit, ready for the Milvus schema (§9)."""

    chunk_id: str  # tenant-scoped unique chunk ID (stable, content-derived)
    document_id: str
    document_version: str
    parent_id: str | None  # canonical parent node (for context expansion)
    page_start: int
    page_end: int
    chunk_order: int  # neighbor ordering within the document
    heading_path: list[str]
    text: str  # retrieval text (heading context included for prose)
    identifiers: list[str]  # exact-match identifiers (§9 identifiers field)
    modality: str = "text"  # "text" | "page_image" (page_image in embeddings.py)
    parser: str = ""
    detection_provenance: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_milvus_record(self, group_id: str) -> dict:
        """Shape a Milvus insert record (§9 schema)."""
        return {
            "chunk_id": f"{group_id}:{self.chunk_id}",
            "group_id": group_id,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "parent_id": self.parent_id or "",
            "page_start": self.page_start,
            "page_end": self.page_end,
            "chunk_order": self.chunk_order,
            "text": self.text,
            "identifiers": self.identifiers,
            # dense_vector / sparse_vector filled by the embedding + index stages
            "metadata": {
                **self.metadata,
                "modality": self.modality,
                "heading_path": self.heading_path,
                "parser": self.parser,
                "detection_provenance": self.detection_provenance,
            },
        }


@lru_cache(maxsize=4)
def _cached_tokenizer(path: str):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)


def _default_token_len(text: str) -> int:
    """MiniLM reranker tokenizer, pre-staged artifact only.

    Loads ``cross-encoder/ms-marco-MiniLM-L6-v2``'s tokenizer from the
    local artifact path (``MINILM_TOKENIZER_PATH``). Fails closed when the
    artifact is missing — never downloads (HF_HUB_OFFLINE=1 in production).
    """
    import os

    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "transformers is required for the production token_len; "
            "inject a token_len callable instead."
        ) from exc
    path = os.environ.get("MINILM_TOKENIZER_PATH", "")
    if not path:
        raise RuntimeError(
            "MINILM_TOKENIZER_PATH is not set: the reranker tokenizer must "
            "be pre-staged per the artifact policy (§3). Refusing to download."
        )
    tok = _cached_tokenizer(path)
    return len(tok.encode(text, add_special_tokens=False))


class StructuralChunker:
    """Structure-first, tokenizer-aware chunker."""

    def __init__(
        self,
        settings: ChunkerSettings | None = None,
        token_len=None,  # Callable[[str], int]
        identifier_extractor: IdentifierExtractor | None = None,
    ) -> None:
        self.settings = settings or ChunkerSettings()
        self.token_len = token_len or _default_token_len
        self.identifiers = identifier_extractor or IdentifierExtractor()
        self._budget = (
            self.settings.reranker_max_input_tokens - self.settings.max_query_tokens
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def chunk_document(self, doc: Node, *, start_order: int = 0) -> list[Chunk]:
        """Chunk a whole canonical document tree."""
        chunks: list[Chunk] = []
        seen_ids: set[str] = set()
        order = start_order
        for page in sorted(
            (c for c in doc.children if c.type == NodeType.PAGE),
            key=lambda p: p.page_index,
        ):
            for child in page.children:
                for chunk in self._chunk_node(child, doc, order):
                    if chunk.chunk_id in seen_ids:
                        seed = (
                            f"{doc.document_id}|{doc.document_version}|"
                            f"{child.node_id}|{order}|{chunk.text}"
                        )
                        chunk.chunk_id = (
                            "c_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
                        )
                    seen_ids.add(chunk.chunk_id)
                    chunk.chunk_order = order
                    chunk.metadata["chunk_order"] = order
                    order += 1
                    chunks.append(chunk)
        return chunks

    # ------------------------------------------------------------------
    # node dispatch
    # ------------------------------------------------------------------
    def _chunk_node(self, node: Node, doc: Node, order: int):
        if node.type in (NodeType.PARAGRAPH, NodeType.HEADING, NodeType.LIST):
            yield from self._chunk_prose(node, doc)
        elif node.type == NodeType.TABLE:
            yield from self._chunk_table(node, doc)
        elif node.type == NodeType.CODE_BLOCK:
            yield from self._chunk_code(node, doc)
        elif node.type == NodeType.FIGURE:
            yield from self._chunk_figure(node, doc)
        else:
            # Unknown node types degrade to prose rather than being dropped.
            yield from self._chunk_prose(node, doc)

    # ------------------------------------------------------------------
    # builders
    # ------------------------------------------------------------------
    def _make_chunk(
        self,
        node: Node,
        doc: Node,
        text: str,
        parent_id: str | None = None,
        extra_metadata: dict | None = None,
    ) -> Chunk:
        # Hash the full chunk text (not just text[:128]) so multi-chunk tables
        # and split sections sharing a long header prefix never collide.
        seed = f"{doc.document_id}|{doc.document_version}|{node.node_id}|{text}"
        chunk_id = "c_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
        chunk = Chunk(
            chunk_id=chunk_id,
            document_id=doc.document_id,
            document_version=doc.document_version,
            parent_id=parent_id or node.node_id,
            page_start=node.page_index,
            page_end=node.page_index,
            chunk_order=0,  # assigned by caller
            heading_path=list(node.heading_path),
            text=text,
            identifiers=self.identifiers.extract(text),
            parser=node.parser,
            detection_provenance=node.detection_provenance,
            metadata=dict(extra_metadata or {}),
        )
        self._enforce_budget(chunk)
        return chunk

    def _enforce_budget(self, chunk: Chunk) -> None:
        """HARD RULE: split, never truncate. Raises on violation."""
        n = self.token_len(chunk.text)
        if n > self._budget:
            raise ValueError(
                f"chunk {chunk.chunk_id} is {n} tokens, over the {self._budget} "
                f"token budget (512 - {self.settings.max_query_tokens} query "
                f"reserve). Splitting logic failed to split — this is a bug."
            )

    def _with_heading_context(self, node: Node, text: str) -> str:
        """Include heading context in embedding text (§7)."""
        if node.heading_path:
            prefix = " > ".join(node.heading_path)
            if not text.startswith(prefix):
                return f"{prefix}\n\n{text}"
        return text

    def _heading_token_overhead(self, node: Node) -> int:
        if not node.heading_path:
            return 0
        prefix = " > ".join(node.heading_path)
        n = self.token_len(prefix)
        # Guard against pathological/broken token_len callables.
        if n >= self.settings.prose_target_tokens // 2:
            return 0
        return n

    def _split_text_to_budget(self, text: str, max_tokens: int) -> list[str]:
        """Split oversized text into pieces that each fit within ``max_tokens``.

        Uses ``self.token_len`` directly rather than a fixed 4-chars-per-token
        assumption so dense short-token inputs (e.g. code or numeric tables)
        never exceed ``self._budget``. If even a single word/character exceeds
        ``max_tokens`` (e.g. a broken tokenizer in tests), returns ``[text]``
        so ``_enforce_budget`` surfaces the violation as required by §7.
        """
        if max_tokens < 1:
            return [text]
        if self.token_len(text) <= max_tokens:
            return [text]
        words = text.split()
        if len(words) > 1:
            if self.token_len(words[0]) > max_tokens:
                return [text]
            pieces: list[str] = []
            lo = 0
            n_words = len(words)
            while lo < n_words:
                # Exponential + binary search for the largest word span fitting max_tokens
                step = min(max_tokens, n_words - lo)
                hi = lo + max(1, step)
                while hi < n_words and self.token_len(" ".join(words[lo:hi])) <= max_tokens:
                    step = max(1, step * 2)
                    hi = min(n_words, hi + step)
                # Binary search in [lo + 1, hi]
                left, right = lo + 1, hi
                best = lo + 1
                while left <= right:
                    mid = (left + right) // 2
                    if self.token_len(" ".join(words[lo:mid])) <= max_tokens:
                        best = mid
                        left = mid + 1
                    else:
                        right = mid - 1
                pieces.append(" ".join(words[lo:best]))
                lo = best
            return pieces

        # Single giant whitespace-free token: split by characters if possible.
        if len(text) <= 1 or self.token_len(text[:1]) > max_tokens:
            return [text]
        pieces = []
        lo = 0
        n_chars = len(text)
        while lo < n_chars:
            left, right = lo + 1, min(n_chars, lo + max_tokens * 4)
            best = lo + 1
            while left <= right:
                mid = (left + right) // 2
                if self.token_len(text[lo:mid]) <= max_tokens:
                    best = mid
                    left = mid + 1
                else:
                    right = mid - 1
            pieces.append(text[lo:best])
            lo = best
        return pieces

    # ------------------------------------------------------------------
    # prose: accumulate to target, overlap on paragraph boundaries
    # ------------------------------------------------------------------
    def _chunk_prose(self, node: Node, doc: Node):
        raw_text = node.text if node.type != NodeType.LIST else "\n".join(
            f"- {i}" for i in node.data.get("items", [])
        )
        full_text = self._with_heading_context(node, raw_text)
        if self.token_len(full_text) <= self.settings.prose_target_tokens:
            yield self._make_chunk(node, doc, full_text)
            return

        heading_overhead = self._heading_token_overhead(node)
        body_target = max(16, self.settings.prose_target_tokens - heading_overhead)
        body_budget = max(16, self._budget - heading_overhead)

        # Split raw body into paragraph units and attach heading context to
        # every emitted chunk so later chunks do not lose section provenance.
        units = [u for u in raw_text.split("\n\n") if u.strip()]
        buf: list[str] = []
        buf_tokens = 0
        for unit in units:
            u_tokens = self.token_len(unit)
            if u_tokens > body_target:
                # Single oversized unit: flush buffer, split unit by sentences.
                if buf:
                    yield self._make_chunk(
                        node, doc, self._with_heading_context(node, "\n\n".join(buf))
                    )
                    buf, buf_tokens = [], 0
                yield from self._split_long_unit(node, doc, unit, body_target, body_budget)
                continue
            if buf and buf_tokens + u_tokens > body_target:
                yield self._make_chunk(
                    node, doc, self._with_heading_context(node, "\n\n".join(buf))
                )
                # Overlap: carry the tail (~overlap tokens) forward, bounded so
                # buf_tokens + u_tokens never exceeds body_target / _budget.
                max_overlap = max(0, min(self.settings.prose_overlap_tokens, body_target - u_tokens))
                buf = self._overlap_tail(buf, max_tokens=max_overlap)
                buf_tokens = sum(self.token_len(b) for b in buf)
            buf.append(unit)
            buf_tokens += u_tokens
        if buf:
            yield self._make_chunk(
                node, doc, self._with_heading_context(node, "\n\n".join(buf))
            )

    def _overlap_tail(self, buf: list[str], max_tokens: int | None = None) -> list[str]:
        """Carry ~overlap tokens of trailing context (prose only)."""
        overlap_goal = self.settings.prose_overlap_tokens
        cap = overlap_goal if max_tokens is None else min(overlap_goal, max_tokens)
        if cap <= 0 or not buf:
            return []
        tail: list[str] = []
        total = 0
        for unit in reversed(buf):
            u_tok = self.token_len(unit)
            if total + u_tok <= cap:
                tail.insert(0, unit)
                total += u_tok
                if total >= cap:
                    break
            else:
                remaining = cap - total
                if remaining > 0 and not tail:
                    words = unit.split()
                    if len(words) > remaining:
                        sub = " ".join(words[-remaining:])
                        if self.token_len(sub) <= cap:
                            tail.insert(0, sub)
                break
        return tail

    def _split_long_unit(
        self,
        node: Node,
        doc: Node,
        unit: str,
        body_target: int | None = None,
        body_budget: int | None = None,
    ):
        """Split an oversized prose unit on sentence boundaries."""
        target = body_target or self.settings.prose_target_tokens
        budget = body_budget or self._budget
        sentences = re.split(r"(?<=[.!?])\s+", unit)
        buf: list[str] = []
        buf_tokens = 0
        for sent in sentences:
            s_tokens = self.token_len(sent)
            if s_tokens > budget:
                if buf:
                    yield self._make_chunk(
                        node, doc, self._with_heading_context(node, " ".join(buf))
                    )
                    buf, buf_tokens = [], 0
                for piece in self._split_text_to_budget(sent, target):
                    yield self._make_chunk(
                        node, doc, self._with_heading_context(node, piece)
                    )
                continue
            if buf and buf_tokens + s_tokens > target:
                yield self._make_chunk(
                    node, doc, self._with_heading_context(node, " ".join(buf))
                )
                buf = self._overlap_tail(buf, max_tokens=max(0, min(self.settings.prose_overlap_tokens, target - s_tokens)))
                buf_tokens = sum(self.token_len(b) for b in buf)
            buf.append(sent)
            buf_tokens += s_tokens
        if buf:
            yield self._make_chunk(
                node, doc, self._with_heading_context(node, " ".join(buf))
            )

    # ------------------------------------------------------------------
    # tables: split by row groups, repeat headers, never sliding overlap
    # ------------------------------------------------------------------
    def _chunk_table(self, node: Node, doc: Node):
        headers: list[str] = node.data.get("headers", [])
        rows: list[list[str]] = node.data.get("rows", [])
        header_line = " | ".join(headers) if headers else ""
        # Pack row groups so each chunk (headers + rows) fits the target.
        group: list[list[str]] = []
        for row in rows:
            candidate = group + [row]
            text = self._table_text(header_line, candidate, node)
            if self.token_len(text) > self.settings.prose_target_tokens and group:
                yield self._make_chunk(
                    node, doc, self._table_text(header_line, group, node),
                    extra_metadata={"rows": len(group), "table_id": node.node_id},
                )
                group = []
            group.append(row)
            # Safety: a single row larger than the whole budget must still
            # not violate the hard rule — split the oversized row across
            # row-continuation chunks rather than truncating or crashing.
            if self.token_len(self._table_text(header_line, group, node)) > self._budget:
                oversized_row = group.pop()
                if group:
                    yield self._make_chunk(
                        node, doc, self._table_text(header_line, group, node),
                        extra_metadata={"rows": len(group), "table_id": node.node_id},
                    )
                    group = []
                yield from self._split_oversized_table_row(
                    header_line, oversized_row, node, doc
                )
        if group:
            yield self._make_chunk(
                node, doc, self._table_text(header_line, group, node),
                extra_metadata={"rows": len(group), "table_id": node.node_id},
            )
        if not rows and not headers and node.text:
            # Unparsed table text degrades to prose-style splitting within budget.
            for piece in self._split_text_to_budget(node.text, self.settings.prose_target_tokens):
                yield self._make_chunk(node, doc, piece)

    def _split_oversized_table_row(
        self, header_line: str, row: list[str], node: Node, doc: Node
    ):
        """Split a single table row that exceeds ``self._budget`` into
        header-preserving continuation chunks."""
        shell_tokens = self.token_len(self._table_text(header_line, [[""]], node))
        avail = max(16, self.settings.prose_target_tokens - shell_tokens)
        row_line = " | ".join(row)
        pieces = self._split_text_to_budget(row_line, avail)
        for piece in pieces:
            yield self._make_chunk(
                node,
                doc,
                self._table_text(header_line, [[piece]], node),
                extra_metadata={"rows": 1, "table_id": node.node_id},
            )

    def _table_text(
        self, header_line: str, rows: list[list[str]], node: Node
    ) -> str:
        """Retrieval serialization: repeat headers, rows as text (§6)."""
        lines = []
        if node.heading_path:
            lines.append(" > ".join(node.heading_path))
        if header_line:
            lines.append(header_line)
        for row in rows:
            lines.append(" | ".join(row))
        lines.append(f"[table {node.node_id} p.{node.page_index + 1}]")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # code: split at blank lines (meaningful boundaries)
    # ------------------------------------------------------------------
    def _chunk_code(self, node: Node, doc: Node):
        full_text = self._with_heading_context(node, node.text)
        if self.token_len(full_text) <= self.settings.prose_target_tokens:
            yield self._make_chunk(
                node, doc, full_text, extra_metadata={"content_type": "code"}
            )
            return
        heading_overhead = self._heading_token_overhead(node)
        body_target = max(16, self.settings.prose_target_tokens - heading_overhead)
        body_budget = max(16, self._budget - heading_overhead)
        # Split at blank lines; fall back to tokenizer-aware splits guarded by budget.
        parts = [p for p in node.text.split("\n\n") if p.strip()]
        buf: list[str] = []
        buf_tokens = 0
        for part in parts:
            p_tokens = self.token_len(part)
            if p_tokens > body_budget:
                if buf:
                    yield self._make_chunk(
                        node,
                        doc,
                        self._with_heading_context(node, "\n\n".join(buf)),
                        extra_metadata={"content_type": "code"},
                    )
                    buf, buf_tokens = [], 0
                for piece in self._split_text_to_budget(part, body_target):
                    yield self._make_chunk(
                        node,
                        doc,
                        self._with_heading_context(node, piece),
                        extra_metadata={"content_type": "code"},
                    )
                continue
            if buf and buf_tokens + p_tokens > body_target:
                yield self._make_chunk(
                    node,
                    doc,
                    self._with_heading_context(node, "\n\n".join(buf)),
                    extra_metadata={"content_type": "code"},
                )
                buf, buf_tokens = [], 0
            buf.append(part)
            buf_tokens += p_tokens
        if buf:
            yield self._make_chunk(
                node,
                doc,
                self._with_heading_context(node, "\n\n".join(buf)),
                extra_metadata={"content_type": "code"},
            )

    # ------------------------------------------------------------------
    # figures: reference chunk pointing at the retained render
    # ------------------------------------------------------------------
    def _chunk_figure(self, node: Node, doc: Node):
        render_ref = node.data.get("render_ref", "")
        text = f"[figure p.{node.page_index + 1} render={render_ref}]"
        if node.heading_path:
            text = " > ".join(node.heading_path) + "\n" + text
        yield self._make_chunk(
            node, doc, text, extra_metadata={"content_type": "figure"}
        )
