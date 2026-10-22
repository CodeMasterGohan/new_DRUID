"""Shared GPU inference; callers own task prefixes, this transport never adds them."""

from __future__ import annotations

import base64
import io
import math

import httpx

from .settings import EmbeddingServiceSettings


class VllmEmbeddingBackend:
    """Thread-safe pooled HTTP transport to native EmbeddingGemma 2 pooling."""

    def __init__(self, settings: EmbeddingServiceSettings, *, client=None) -> None:
        self.settings = settings
        headers = {}
        if settings.vllm_api_key:
            headers["Authorization"] = f"Bearer {settings.vllm_api_key.get_secret_value()}"
        self._client = client or httpx.Client(
            base_url=str(settings.vllm_base_url).rstrip("/") + "/",
            headers=headers,
            timeout=httpx.Timeout(settings.vllm_timeout_seconds, connect=10.0),
            limits=httpx.Limits(
                max_connections=settings.max_concurrency,
                max_keepalive_connections=settings.max_concurrency,
            ),
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def check_ready(self) -> None:
        try:
            response = self._client.get("../health")
            response.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("shared vLLM embedding service is unavailable") from exc

    def _request(self, payload: dict, expected_count: int) -> list[list[float]]:
        try:
            response = self._client.post("embeddings", json={
                "model": self.settings.model_id,
                "encoding_format": "float",
                # Always obtain the native width; truncate and normalize below.
                "dimensions": 768,
                **payload,
            })
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Do not copy upstream bodies, which can contain private input text.
            raise RuntimeError(
                f"vLLM embedding request failed (HTTP {exc.response.status_code})"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("shared vLLM embedding request failed") from exc
        try:
            data = response.json()["data"]
            if len(data) != expected_count:
                raise ValueError("vector count mismatch")
            rows = {}
            for item in data:
                index = item["index"]
                if type(index) is not int or not 0 <= index < expected_count or index in rows:
                    raise ValueError("invalid or duplicate vector index")
                raw = item["embedding"]
                if len(raw) != 768:
                    raise ValueError("native embedding width must be 768")
                if any(type(v) not in (float, int) or not math.isfinite(v) for v in raw):
                    raise ValueError("non-finite or non-numeric embedding")
                vector = [float(v) for v in raw[:self.settings.dimension]]
                norm = math.sqrt(sum(v * v for v in vector))
                if not math.isfinite(norm) or norm <= 0:
                    raise ValueError("zero or invalid embedding norm")
                if self.settings.normalize:
                    vector = [v / norm for v in vector]
                rows[index] = vector
            return [rows[i] for i in range(expected_count)]
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise RuntimeError("vLLM returned an invalid embedding response") from exc

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for i in range(0, len(texts), self.settings.batch_size):
            vectors.extend(self._request(
                {"input": texts[i:i + self.settings.batch_size]},
                len(texts[i:i + self.settings.batch_size]),
            ))
        return vectors

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        if images and self.settings.modality_config == "text-only":
            raise ValueError("image embeddings require text+image modality")
        from PIL import Image, UnidentifiedImageError

        vectors = []
        for image in images:
            if len(image) > 20 * 1024 * 1024:
                raise ValueError("page image exceeds 20 MiB")
            try:
                with Image.open(io.BytesIO(image)) as opened:
                    rgb = opened.convert("RGB")
                    png = io.BytesIO()
                    rgb.save(png, format="PNG")
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise ValueError("invalid page image") from exc
            uri = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode("ascii")
            # The deployment's minimal template emits only <|image|>; no chat
            # roles, generation prompt, task prefix, or OCR/page-label text.
            vectors.extend(self._request({
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": uri}},
                ]}],
                "add_special_tokens": True,
                "add_generation_prompt": False,
            }, 1))
        return vectors
