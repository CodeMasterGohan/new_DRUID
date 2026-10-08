"""olmOCR 2 FP8 visual-path client (spec §5, visual path).

Talks to the vLLM instance serving ``allenai/olmOCR-2-7B-1025-FP8`` over its
OpenAI-compatible ``/v1/chat/completions`` endpoint. The H200 owns the GPU;
this client runs on CPU and ships page renders over HTTP.

Prompt discipline: the prompt text comes from the olmOCR toolkit's builders
(``olmocr.prompts``) — never hand-rolled. If the toolkit is not installed
the client fails closed with an explicit error instead of improvising.

Low-quality or invalid output goes through ``max_retries`` attempts and is
then returned with ``quality_status="review"`` so the pipeline can park the
page in the review queue. It never silently becomes searchable.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field

from .config import OlmOcrSettings


@dataclass
class OlmOcrConfig:
    """Connection config (env-overridable)."""

    base_url: str  # e.g. http://olmocr-vllm:8000
    api_key: str = "not-needed"  # vLLM default; kept for client compat
    settings: OlmOcrSettings = field(default_factory=OlmOcrSettings)

    @classmethod
    def from_env(cls) -> "OlmOcrConfig":
        import os

        return cls(
            base_url=os.environ.get("OLMOCR_VLLM_URL", "http://olmocr-vllm:8000"),
            api_key=os.environ.get("OLMOCR_VLLM_API_KEY", "not-needed"),
        )


@dataclass
class OlmOcrResult:
    """One page processed by olmOCR."""

    page_index: int
    markdown: str
    # Frontmatter metadata: primary_language, is_rotation_valid,
    # rotation_correction, is_table, is_diagram.
    metadata: dict
    quality_status: str  # "accepted" | "review"
    attempts: int
    latency_s: float
    error: str | None = None


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _toolkit_prompt() -> str:
    """Build the prompt with the olmOCR toolkit. Never hand-rolled."""
    try:
        from olmocr.prompts import build_no_anchoring_v4_yaml_prompt
    except ImportError as exc:
        raise RuntimeError(
            "The olmOCR toolkit ('olmocr' package) is required to build "
            "prompts for olmOCR-2-7B-1025-FP8. Prompts must come from the "
            "toolkit's builders to preserve the checkpoint's expected "
            "rendering/prompt/preprocessing behavior — hand-rolled prompts "
            "are not permitted. Install the toolkit from the pinned "
            "requirements."
        ) from exc
    return build_no_anchoring_v4_yaml_prompt()


def _parse_response(markdown: str) -> tuple[dict, str]:
    """Split olmOCR output into frontmatter metadata and body markdown."""
    match = _FRONTMATTER_RE.match(markdown.strip())
    if not match:
        return {}, markdown
    raw_meta, body = match.group(1), match.group(2)
    metadata: dict = {}
    for line in raw_meta.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            if value in ("True", "False"):
                value = value == "True"
            metadata[key.strip()] = value
    return metadata, body.strip()


def _quality_check(metadata: dict, body: str) -> tuple[bool, str]:
    """Validate olmOCR output. Returns (ok, reason)."""
    if not body or len(body.strip()) < 10:
        return False, "empty or trivial output"
    if metadata.get("is_rotation_valid") is False:
        return False, "rotation invalid"
    # Degenerate repetition: one line dominating the output.
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if lines:
        top = max(lines.count(ln) for ln in set(lines))
        if top > max(5, len(lines) // 2):
            return False, "degenerate repetition"
    return True, "ok"


class OlmOcrClient:
    """HTTP client for the vLLM-served olmOCR endpoint."""

    def __init__(self, config: OlmOcrConfig | None = None) -> None:
        self.config = config or OlmOcrConfig.from_env()
        self.settings = self.config.settings

    def _post(self, image_b64: str, prompt: str) -> str:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for the olmOCR client") from exc
        payload = {
            "model": self.settings.checkpoint,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}"
                            },
                        },
                    ],
                }
            ],
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_new_tokens,
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        url = self.config.base_url.rstrip("/") + "/v1/chat/completions"
        with httpx.Client(timeout=self.settings.request_timeout_s) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected vLLM response shape: {data!r:.500}") from exc

    def process_page(
        self, pdf_path: str, page_index: int, *, png_bytes: bytes | None = None
    ) -> OlmOcrResult:
        """Render (if needed) and run olmOCR on one page, with retries."""
        from .native_worker import render_page_png

        started = time.monotonic()
        if png_bytes is None:
            png_bytes = render_page_png(
                pdf_path,
                page_index,
                longest_dim_px=self.settings.render_longest_dim_px,
            )
        image_b64 = base64.b64encode(png_bytes).decode("ascii")
        prompt = _toolkit_prompt()

        last_error: str | None = None
        for attempt in range(1, self.settings.max_retries + 2):
            try:
                raw = self._post(image_b64, prompt)
            except Exception as exc:  # noqa: BLE001 — retried, then parked
                last_error = f"request failed: {exc}"
                continue
            metadata, body = _parse_response(raw)
            ok, reason = _quality_check(metadata, body)
            if ok:
                return OlmOcrResult(
                    page_index=page_index,
                    markdown=body,
                    metadata=metadata,
                    quality_status="accepted",
                    attempts=attempt,
                    latency_s=time.monotonic() - started,
                )
            last_error = f"quality check failed: {reason}"
        # Out of retries: park for review, never silently index.
        metadata, body = _parse_response(last_error or "")
        return OlmOcrResult(
            page_index=page_index,
            markdown=body,
            metadata=metadata,
            quality_status="review",
            attempts=self.settings.max_retries + 1,
            latency_s=time.monotonic() - started,
            error=last_error,
        )
