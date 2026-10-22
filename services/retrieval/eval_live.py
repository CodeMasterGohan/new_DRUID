"""Live authorized retrieval evaluation (not a mock-backed quality claim).

Run inside the trusted retrieval container or internal network. The corpus
MUST be labeled against real ingested tenant documents; no fabricated gold
answers are shipped. Only aggregate metrics are printed by default.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import statistics
import time
import uuid
from pathlib import Path

import httpx


def load_cases(path: str) -> list[dict]:
    cases = []
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("query"), str) or not row["query"].strip():
            raise ValueError(f"line {lineno}: nonempty query is required")
        cases.append(row)
    if not cases:
        raise ValueError("evaluation dataset is empty")
    return cases


def _p95(samples: list[float]) -> float | None:
    if not samples:
        return None
    samples = sorted(samples)
    return round(samples[max(0, math.ceil(0.95 * len(samples)) - 1)], 2)


def evaluate(cases: list[dict], retrieve, k: int = 8) -> dict:
    """Return metrics calculated from actual responses; failures count as misses."""
    if k < 1:
        raise ValueError("k must be >= 1")
    successes = 0
    hits = 0
    labeled = 0
    versions = 0
    expected_versions = 0
    citation_count = 0
    citation_present = 0
    forbidden_leaks = 0
    forbidden_cases = 0
    latency: list[float] = []
    failures: list[dict] = []
    for i, item in enumerate(cases):
        if item.get("expected_document_id"):
            labeled += 1
        if item.get("expected_version"):
            expected_versions += 1
        if item.get("forbidden_document_id"):
            forbidden_cases += 1
        start = time.monotonic()
        try:
            response = retrieve(item["query"])
            if not isinstance(response, dict) or not isinstance(response.get("results"), list):
                raise ValueError("invalid search response")
            hits_list = response["results"][:k]
            latency.append((time.monotonic() - start) * 1000)
            successes += 1
        except Exception as exc:
            failures.append({"case": item.get("id", i), "error": str(exc)[:200]})
            continue
        if item.get("expected_document_id") and any(
            str(h.get("document_id")) == item["expected_document_id"] for h in hits_list
        ):
            hits += 1
        if item.get("expected_version") and any(
            str(h.get("document_version")) == str(item["expected_version"])
            and (
                not item.get("expected_document_id")
                or str(h.get("document_id")) == item["expected_document_id"]
            )
            for h in hits_list
        ):
            versions += 1
        if item.get("forbidden_document_id") and any(
            str(h.get("document_id")) == item["forbidden_document_id"] for h in hits_list
        ):
            forbidden_leaks += 1
        for passage in hits_list:
            citation_count += 1
            if (passage.get("source_reference") and passage.get("page_start") is not None
                    and passage.get("page_end") is not None):
                citation_present += 1
    return {
        "evaluation_type": "live_authorized_retrieval",
        "cases": len(cases),
        "successful_requests": successes,
        "request_failures": failures,
        "document_recall_at_k": round(hits / labeled, 4) if labeled else None,
        "document_gold_cases": labeled,
        "version_hit_at_k": round(versions / expected_versions, 4) if expected_versions else None,
        "version_gold_cases": expected_versions,
        "citation_metadata_coverage": round(citation_present / citation_count, 4) if citation_count else None,
        "citation_count": citation_count,
        "forbidden_document_leaks": forbidden_leaks,
        "forbidden_document_cases": forbidden_cases,
        "latency_ms": {
            "p50": round(statistics.median(latency), 2) if latency else None,
            "p95": _p95(latency),
        },
    }


def client(url: str, user: str, group: str, secret: str, timeout: float = 30.0):
    if not (url and user and group and secret):
        raise ValueError("url, user, group, and secret are required")
    endpoint = url.rstrip("/") + "/search"
    transport: httpx.Client | None = None

    def retrieve(query: str) -> dict:
        nonlocal transport
        if transport is None:
            transport = httpx.Client(timeout=timeout)
        now = str(time.time())
        request_id = uuid.uuid4().hex
        canonical = f"{user}|{group}|reader|{request_id}|{now}"
        digest = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-Druid-User": user, "X-Druid-Group": group,
            "X-Druid-Roles": "reader",
            "X-Druid-Request-Id": request_id,
            "X-Druid-Timestamp": now,
            "X-Druid-Signature": digest,
        }
        response = transport.post(endpoint, json={"query": query}, headers=headers)
        response.raise_for_status()
        return response.json()
    return retrieve


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--endpoint", default="http://127.0.0.1:8004")
    p.add_argument("--cases", required=True)
    p.add_argument("--user", default=os.getenv("DRUID_EVAL_USER", ""))
    p.add_argument("--group", default=os.getenv("DRUID_EVAL_GROUP", ""))
    p.add_argument("--k", type=int, default=8)
    p.add_argument("--report-out")
    args = p.parse_args()
    data = evaluate(
        load_cases(args.cases),
        client(args.endpoint, args.user, args.group, os.getenv("INTERNAL_SECRET", "")),
        k=args.k,
    )
    report = json.dumps(data, indent=2, sort_keys=True)
    print(report)
    if args.report_out:
        Path(args.report_out).write_text(report + "\n", encoding="utf-8")
    return 1 if data["request_failures"] or data["forbidden_document_leaks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
