#!/usr/bin/env python3
"""Smoke-test a running shared vLLM EmbeddingGemma 2 service without model downloads.

Run inside its Docker network, or pass a reachable /v1 URL. Set
EMBEDDING_VLLM_API_KEY when the server requires Bearer authentication.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import struct
import sys
import urllib.error
import urllib.request
import zlib


def _test_image() -> str:
    """Create a 224x224 RGB PNG with only the standard library."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    width = height = 224
    rows = b"".join(b"\0" + bytes([160, 80, 40]) * width for _ in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _request(base_url: str, path: str, api_key: str, payload: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def _check_vectors(response: dict, *, model: str, count: int) -> None:
    if response.get("model") != model:
        raise ValueError(f"expected model {model!r}, received {response.get('model')!r}")
    data = response.get("data", [])
    if len(data) != count or sorted(row["index"] for row in data) != list(range(count)):
        raise ValueError("embedding response has missing or duplicate input indices")
    for row in data:
        vector = row.get("embedding", [])
        if len(vector) != 768 or not all(math.isfinite(value) for value in vector):
            raise ValueError("expected a finite, native 768-dimensional vector")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isclose(norm, 1.0, rel_tol=0.01, abs_tol=0.01):
            raise ValueError(f"embedding is not normalized (L2 norm={norm:.6f})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_url", nargs="?",
        default=os.environ.get("EMBEDDING_VLLM_BASE_URL", "http://vllm-embedding:8000/v1"),
        help="reachable vLLM URL including /v1",
    )
    parser.add_argument("--model", default="google/embeddinggemma-2")
    args = parser.parse_args()
    api_key = os.environ.get("EMBEDDING_VLLM_API_KEY", "")
    try:
        models = _request(args.base_url, "/models", api_key).get("data", [])
        if args.model not in [model["id"] for model in models]:
            raise ValueError(f"server does not advertise {args.model!r}")
        # Task prefixes belong to the application adapter. Do not ask vLLM to
        # apply them again, or send dimensions (this checkpoint has no vLLM
        # Matryoshka flag; adapters truncate/normalize full-width results).
        text = _request(args.base_url, "/embeddings", api_key, {
            "model": args.model,
            "encoding_format": "float",
            "input": [
                "task: question answering | query: What is a shared embedding service?",
                "title: Deployment | text: Both applications share one embedding model.",
            ],
            "add_special_tokens": True,
        })
        _check_vectors(text, model=args.model, count=2)
        image = _request(args.base_url, "/embeddings", api_key, {
            "model": args.model,
            "encoding_format": "float",
            "messages": [{"role": "user", "content": [{
                "type": "image_url", "image_url": {"url": _test_image()},
            }]}],
            "add_special_tokens": True,
        })
        _check_vectors(image, model=args.model, count=1)
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        print(f"Embedding smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {args.model}; {len(models)} advertised model(s); text and image vectors are 768-dimensional and normalized")
    return 0


if __name__ == "__main__":
    sys.exit(main())
