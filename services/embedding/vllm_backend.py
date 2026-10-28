"""Shared vLLM pooling client, independent of either application's API.

Text inputs already carry the contractual document/query prefixes. This client
never adds another prompt, downloads models, or falls back to local inference.
"""

from __future__ import annotations

import base64
import math
from urllib.parse import urlsplit

import httpx


class VllmEmbeddingBackend:
    def __init__(
        self,
        *,
        model_id: str = "google/embeddinggemma-2",
        base_url: str = "http://vllm-embedding:8000/v1",
        api_key: str = "",
        timeout_s: float = 120.0,
        dimension: int = 768,
        normalize: bool = True,
        modality_config: str = "text+image",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("EMBEDDING_VLLM_URL must be an HTTP(S) base URL without credentials")
        if dimension not in (768, 512, 256, 128):
            raise ValueError("unvalidated Matryoshka width")
        if timeout_s <= 0 or not math.isfinite(timeout_s):
            raise ValueError("vLLM timeout must be positive and finite")
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.dimension = dimension
        self.normalize = normalize
        self.modality_config = modality_config
        self._transport = transport

    def _request(self, inputs: dict, *, count: int) -> list[list[float]]:
        payload = {
            "model": self.model_id,
            "encoding_format": "float",
            # EmbeddingGemma2's upstream config does not advertise
            # is_matryoshka, so vLLM rejects the optional dimensions field.
            # Always request its native 768 rows and truncate locally like ST.
            # Chat requests default to false in vLLM. EmbeddingGemma requires
            # BOS/EOS for both image and text pooling, as in its ST processor.
            "add_special_tokens": True,
            **inputs,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with httpx.Client(
                timeout=self.timeout_s, transport=self._transport, headers=headers,
            ) as client:
                response = client.post(f"{self.base_url}/embeddings", json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            # Do not include upstream response bodies or headers: these can
            # contain private document content or credentials.
            raise RuntimeError(f"vLLM embeddings returned HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError("vLLM embeddings request failed; shared model server unavailable") from exc
        except ValueError as exc:
            raise RuntimeError("vLLM embeddings returned invalid JSON") from exc

        if not isinstance(body, dict) or body.get("model") != self.model_id:
            raise RuntimeError("vLLM embeddings response model does not match configured model")
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != count:
            raise RuntimeError("vLLM embeddings response count does not match inputs")
        vectors: dict[int, list[float]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("vLLM embeddings returned a malformed row")
            index = row.get("index")
            if type(index) is not int or not 0 <= index < count or index in vectors:
                raise RuntimeError("vLLM embeddings returned invalid or duplicate indices")
            raw = row.get("embedding")
            if not isinstance(raw, list) or len(raw) != 768:
                raise RuntimeError("vLLM embedding width does not match native dimension 768")
            if any(type(v) not in (float, int) or not math.isfinite(v) for v in raw):
                raise RuntimeError("vLLM embeddings returned non-finite or non-numeric values")
            vector = [float(v) for v in raw[:self.dimension]]
            if self.normalize:
                norm = math.hypot(*vector)
                if not math.isfinite(norm) or norm == 0:
                    raise RuntimeError("vLLM embeddings returned a zero or invalid vector")
                # Re-normalize after conversion from BF16, including at full
                # width, to preserve the existing application's L2 contract.
                vector = [v / norm for v in vector]
            vectors[index] = vector
        return [vectors[i] for i in range(count)]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._request({"input": texts}, count=len(texts))

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        if self.modality_config != "text+image":
            raise ValueError("image embeddings require text+image modality")
        vectors = []
        for image in images:
            if image.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif image.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif image.startswith((b"GIF87a", b"GIF89a")):
                mime = "image/gif"
            elif image.startswith(b"RIFF") and image[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                raise ValueError("image embedding requires PNG, JPEG, GIF or WebP bytes")
            uri = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
            vectors.extend(self._request({"messages": [{
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": uri}}],
            }]}, count=1))
        return vectors
