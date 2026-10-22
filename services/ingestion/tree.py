"""Canonical typed document tree (spec §6).

The authoritative parsed artifact. Markdown is a *derived* representation.

    Document
      Page
        Heading | Paragraph | List | Table | CodeBlock | Figure

Every node carries full metadata: stable node IDs (content-derived, so
retries never create duplicates), parser + revision, quality_status, and
detection provenance (``native-detected`` vs ``olmocr-routed``) on tables.
Geometry is retained when available and never fabricated.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import dataclass, field


class NodeType(str, enum.Enum):
    DOCUMENT = "document"
    PAGE = "page"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    CODE_BLOCK = "code_block"
    FIGURE = "figure"


@dataclass
class Node:
    node_id: str
    type: NodeType
    document_id: str
    document_version: str
    page_index: int
    bounding_box: tuple[float, float, float, float] | None
    heading_path: list[str]
    parser: str  # "native" | "olmocr"
    parser_revision: str
    quality_status: str = "accepted"  # accepted | review | rejected
    # Table detection provenance (§6): "native-detected" | "olmocr-routed" | None
    detection_provenance: str | None = None
    text: str = ""
    # Type-specific payload: table -> {"headers": [...], "rows": [[...]],
    # "is_simple": bool}; list -> {"items": [...]}; figure -> {"render_ref": ...}
    data: dict = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "type": self.type.value,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "page_index": self.page_index,
            "bounding_box": list(self.bounding_box) if self.bounding_box else None,
            "heading_path": self.heading_path,
            "parser": self.parser,
            "parser_revision": self.parser_revision,
            "quality_status": self.quality_status,
            "detection_provenance": self.detection_provenance,
            "text": self.text,
            "data": self.data,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(
            node_id=d["node_id"],
            type=NodeType(d["type"]),
            document_id=d["document_id"],
            document_version=d["document_version"],
            page_index=d["page_index"],
            bounding_box=tuple(d["bounding_box"]) if d["bounding_box"] else None,  # type: ignore[arg-type]
            heading_path=d.get("heading_path", []),
            parser=d["parser"],
            parser_revision=d["parser_revision"],
            quality_status=d.get("quality_status", "accepted"),
            detection_provenance=d.get("detection_provenance"),
            text=d.get("text", ""),
            data=d.get("data", {}),
            children=[cls.from_dict(c) for c in d.get("children", [])],
        )


def stable_node_id(
    document_id: str,
    document_version: str,
    page_index: int,
    node_type: str,
    ordinal: int,
    fingerprint: str = "",
) -> str:
    """Content-derived stable ID — retries produce the same ID (§12)."""
    seed = f"{document_id}|{document_version}|{page_index}|{node_type}|{ordinal}|{fingerprint}"
    return "n_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def build_tree_from_native(
    document_id: str,
    document_version: str,
    pages: list,  # list[NativePage]
    table_texts: dict[int, list[str]],
    *,
    parser_revision: str,
) -> Node:
    """Build the canonical tree from the native path.

    Headings are inferred from font-size prominence (largest span size on
    the page above a ratio threshold); everything else becomes paragraphs.
    Tables come from pdftext table_output keyed by page.
    """
    from .native_worker import NativePage  # noqa: F401  (type only)

    doc = Node(
        node_id=stable_node_id(document_id, document_version, -1, "document", 0),
        type=NodeType.DOCUMENT,
        document_id=document_id,
        document_version=document_version,
        page_index=-1,
        bounding_box=None,
        heading_path=[],
        parser="native",
        parser_revision=parser_revision,
    )
    heading_path: list[str] = []
    for page in pages:
        page_node = Node(
            node_id=stable_node_id(document_id, document_version, page.page_index, "page", 0),
            type=NodeType.PAGE,
            document_id=document_id,
            document_version=document_version,
            page_index=page.page_index,
            bounding_box=page.page_bbox,
            heading_path=list(heading_path),
            parser="native",
            parser_revision=parser_revision,
        )
        # Heading inference: spans whose font size dominates the page.
        sizes = [
            span.get("font", {}).get("size", 0)
            for block in page.raw.get("blocks", [])
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if span.get("font", {}).get("size")
        ]
        median_size = sorted(sizes)[len(sizes) // 2] if sizes else 0
        ordinal = 0
        for block in page.geometry.blocks:
            text = block.text.strip()
            if not text:
                continue
            # Approximate: block's max span size from raw (parallel walk).
            node_type = NodeType.PARAGRAPH
            raw_block = _find_raw_block(page.raw, block)
            max_size = 0.0
            if raw_block:
                for line in raw_block.get("lines", []):
                    for span in line.get("spans", []):
                        size = span.get("font", {}).get("size") or 0
                        max_size = max(max_size, float(size))
            if (
                median_size
                and max_size >= median_size * 1.25
                and len(text) < 200
                and "\n" not in text.strip()
            ):
                node_type = NodeType.HEADING
                heading_path = heading_path[: _heading_level(text)] + [text]
            ordinal += 1
            page_node.children.append(
                Node(
                    node_id=stable_node_id(
                        document_id, document_version, page.page_index,
                        node_type.value, ordinal, text[:64],
                    ),
                    type=node_type,
                    document_id=document_id,
                    document_version=document_version,
                    page_index=page.page_index,
                    bounding_box=block.bbox,
                    heading_path=list(heading_path),
                    parser="native",
                    parser_revision=parser_revision,
                    text=text,
                )
            )
        for i, table_text in enumerate(table_texts.get(page.page_index, [])):
            headers, rows = _parse_table_text(table_text)
            ordinal += 1
            page_node.children.append(
                Node(
                    node_id=stable_node_id(
                        document_id, document_version, page.page_index,
                        "table", ordinal, table_text[:64],
                    ),
                    type=NodeType.TABLE,
                    document_id=document_id,
                    document_version=document_version,
                    page_index=page.page_index,
                    bounding_box=None,  # region bbox known to caller; not fabricated here
                    heading_path=list(heading_path),
                    parser="native",
                    parser_revision=parser_revision,
                    detection_provenance="native-detected",
                    text=table_text,
                    data={"headers": headers, "rows": rows,
                          "is_simple": len(headers) <= 8 and all(len(r) == len(headers) for r in rows)},
                )
            )
        doc.children.append(page_node)
    return doc


def _find_raw_block(raw: dict, block) -> dict | None:
    for rb in raw.get("blocks", []):
        if tuple(rb.get("bbox", ())) == tuple(block.bbox):
            return rb
    return None


def _heading_level(text: str) -> int:
    # Single-level inference is enough for heading_path context; deeper
    # structure comes from the visual path's markdown headings.
    m = re.match(r"^\s*\d+(?:\.(\d+))*\b", text)
    if m:
        prefix = m.group(0).strip().rstrip(".")
        return min(3, prefix.count("."))
    return 0


def _parse_table_text(text: str) -> tuple[list[str], list[list[str]]]:
    """Best-effort parse of pdftext table_output text into headers/rows."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], []
    # pdftext separates cells with newlines per row in reading order; split
    # on 2+ spaces as a column heuristic.
    rows = [re.split(r"\s{2,}", ln) for ln in lines]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return rows[0], rows[1:]


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_FENCE_RE = re.compile(r"^```")


def build_tree_from_olmocr(
    document_id: str,
    document_version: str,
    results: list,  # list[OlmOcrResult]
    *,
    parser_revision: str,
) -> Node:
    """Build the canonical tree from olmOCR markdown (visual path).

    Markdown structure maps directly: # headings, pipe tables, fenced code,
    blank-line-separated paragraphs. Tables are provenance-tagged
    ``olmocr-routed``.
    """
    doc = Node(
        node_id=stable_node_id(document_id, document_version, -1, "document", 0),
        type=NodeType.DOCUMENT,
        document_id=document_id,
        document_version=document_version,
        page_index=-1,
        bounding_box=None,
        heading_path=[],
        parser="olmocr",
        parser_revision=parser_revision,
    )
    heading_path: list[str] = []
    for result in results:
        page_node = Node(
            node_id=stable_node_id(document_id, document_version, result.page_index, "page", 0),
            type=NodeType.PAGE,
            document_id=document_id,
            document_version=document_version,
            page_index=result.page_index,
            bounding_box=None,  # visual path has no text geometry; not fabricated
            heading_path=list(heading_path),
            parser="olmocr",
            parser_revision=parser_revision,
            quality_status=result.quality_status,
        )
        ordinal = 0
        for kind, payload in _split_markdown(result.markdown):
            ordinal += 1
            if kind == "heading":
                level, text = payload
                heading_path = heading_path[: level - 1] + [text]
                node_type = NodeType.HEADING
                data: dict = {}
            elif kind == "table":
                node_type = NodeType.TABLE
                text = payload  # type: ignore[assignment]
                headers, rows = _parse_markdown_table(text)
                data = {"headers": headers, "rows": rows,
                        "is_simple": len(headers) <= 8 and all(len(r) == len(headers) for r in rows)}
            elif kind == "code":
                node_type = NodeType.CODE_BLOCK
                text = payload  # type: ignore[assignment]
                data = {}
            else:
                node_type = NodeType.PARAGRAPH
                text = payload  # type: ignore[assignment]
                data = {}
            node = Node(
                node_id=stable_node_id(
                    document_id, document_version, result.page_index,
                    node_type.value, ordinal, text[:64],
                ),
                type=node_type,
                document_id=document_id,
                document_version=document_version,
                page_index=result.page_index,
                bounding_box=None,
                heading_path=list(heading_path),
                parser="olmocr",
                parser_revision=parser_revision,
                quality_status=result.quality_status,
                detection_provenance="olmocr-routed" if node_type == NodeType.TABLE else None,
                text=text,
                data=data,
            )
            page_node.children.append(node)
        doc.children.append(page_node)
    return doc


def _split_markdown(markdown: str) -> list[tuple[str, object]]:
    """Split markdown into (kind, payload) blocks.

    Kinds: heading -> (level, text); table -> text; code -> text;
    paragraph -> text.
    """
    blocks: list[tuple[str, object]] = []
    lines = markdown.splitlines()
    i = 0
    para: list[str] = []
    table_buf: list[str] = []

    def flush_para() -> None:
        text = "\n".join(para).strip()
        if text:
            blocks.append(("paragraph", text))
        para.clear()

    def flush_table() -> None:
        text = "\n".join(table_buf).strip()
        if text:
            blocks.append(("table", text))
        table_buf.clear()

    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            flush_para(); flush_table()
            i += 1
            code: list[str] = []
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                code.append(lines[i]); i += 1
            i += 1  # closing fence
            blocks.append(("code", "\n".join(code)))
            continue
        if _TABLE_ROW_RE.match(line):
            flush_para()
            table_buf.append(line); i += 1
            continue
        if table_buf:
            flush_table()
        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            blocks.append(("heading", (len(m.group(1)), m.group(2).strip())))
        elif not line.strip():
            flush_para()
        else:
            para.append(line)
        i += 1
    flush_para(); flush_table()
    return blocks


def _parse_markdown_table(text: str) -> tuple[list[str], list[list[str]]]:
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Skip the alignment separator row (:--- etc.).
        if cells and all(re.fullmatch(r":?-+:?", c or "---") for c in cells):
            continue
        rows.append(cells)
    if not rows:
        return [], []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return rows[0], rows[1:]


def tree_to_markdown(doc: Node) -> str:
    """Derive the Markdown serialization from the canonical tree (§6)."""
    out: list[str] = []
    for page in doc.children:
        if page.type != NodeType.PAGE:
            continue
        for node in page.children:
            if node.type == NodeType.HEADING:
                out.append(f"## {node.text}")
            elif node.type == NodeType.PARAGRAPH:
                out.append(node.text)
            elif node.type == NodeType.LIST:
                for item in node.data.get("items", []):
                    out.append(f"- {item}")
            elif node.type == NodeType.TABLE:
                out.append(_table_to_markdown(node))
            elif node.type == NodeType.CODE_BLOCK:
                out.append("```\n" + node.text + "\n```")
            elif node.type == NodeType.FIGURE:
                ref = node.data.get("render_ref", "")
                out.append(f"![figure]({ref})")
            out.append("")
    return "\n".join(out).strip() + "\n"


def _table_to_markdown(node: Node) -> str:
    headers = node.data.get("headers", [])
    rows = node.data.get("rows", [])
    if node.data.get("is_simple", True) and headers:
        lines = ["| " + " | ".join(headers) + " |",
                 "| " + " | ".join("---" for _ in headers) + " |"]
        for row in rows:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)
    # Complex tables: structured JSON, never silently lossy.
    return "```json\n" + json.dumps(
        {"headers": headers, "rows": rows,
         "page": node.page_index, "table_id": node.node_id},
        ensure_ascii=False, indent=1) + "\n```"


def collect_diagnostics(doc: Node, routing: dict[int, dict]) -> dict:
    """Assemble parsing diagnostics (§6 derived artifacts)."""
    return {
        "document_id": doc.document_id,
        "document_version": doc.document_version,
        "parser": doc.parser,
        "parser_revision": doc.parser_revision,
        "pages": [
            {
                "page_index": page.page_index,
                "node_count": len(page.children),
                "routing": routing.get(page.page_index, {}),
            }
            for page in doc.children if page.type == NodeType.PAGE
        ],
    }
