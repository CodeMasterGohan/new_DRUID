"""Run inside the CPU embedding image, with its model snapshot mounted.

Compares actual CPU SentenceTransformers vectors with the shared vLLM
endpoint before retaining an existing index. Prints timings, not benchmark claims.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path

from services.embedding.settings import EmbeddingServiceSettings
from services.embedding.vllm_backend import VllmEmbeddingBackend


def cosine(a, b):
    return sum(x*y for x, y in zip(a, b)) / (
        math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(y*y for y in b))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--base-url", default="http://vllm-embedding:8000/v1")
    parser.add_argument("--image", type=Path, required=True, help="Representative page PNG")
    parser.add_argument("--min-cosine", type=float, default=0.999)
    args = parser.parse_args()
    # Matches the existing production CPU backend, including its modality config.
    from sentence_transformers import SentenceTransformer
    from PIL import Image

    cpu = SentenceTransformer(
        args.model_path, device="cpu", trust_remote_code=False,
        config_kwargs={"audio_config": None},
    )
    gpu = VllmEmbeddingBackend(EmbeddingServiceSettings(
        backend="vllm", vllm_base_url=args.base_url,
        vllm_api_key=os.environ.get("EMBEDDING_VLLM_API_KEY"),
    ))
    texts = [
        "task: question answering | query: How do I create a Kubernetes deployment?",
        "task: code retrieval | query: Retry an HTTP request with exponential backoff",
        "task: search result | query: Terraform remote state locking",
        "title: Deployment | text: apiVersion: apps/v1\nkind: Deployment",
        "title: examples/retry.py | text: def retry(fn):\n    return fn()",
        "title: Long document | text: " + "A deployment manages application replicas. " * 350,
    ]
    try:
        started = time.perf_counter()
        reference = cpu.encode(texts, normalize_embeddings=True).tolist()
        cpu_seconds = time.perf_counter() - started
        started = time.perf_counter()
        actual = gpu.embed_texts(texts)
        gpu_seconds = time.perf_counter() - started
        with Image.open(args.image) as image:
            image_ref = cpu.encode([image.convert("RGB")], normalize_embeddings=True).tolist()[0]
        image_actual = gpu.embed_images([args.image.read_bytes()])[0]
        similarities = [cosine(a, b) for a, b in zip(reference, actual)]
        similarities.append(cosine(image_ref, image_actual))
        passed = all(x >= args.min_cosine for x in similarities)
        print(json.dumps({
            "passed": passed, "minimum_required_cosine": args.min_cosine,
            "cosines_text_then_image": similarities,
            "cpu_text_seconds": cpu_seconds, "vllm_text_seconds": gpu_seconds,
            "note": "Small correctness sample, not a throughput benchmark.",
        }, indent=2))
        if not passed:
            raise SystemExit("Parity failed: retain CPU serving and investigate before cutover.")
    finally:
        gpu.close()


if __name__ == "__main__":
    main()
