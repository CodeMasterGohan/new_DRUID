# artifacts — artifact manifest tooling (spec §3)

Controlled-staging tooling. Runs on the staging workstation only — never in
production services.

## Manifests

`manifests/` holds one YAML per approved artifact. The three examples are
**templates**: sha256 values are zero placeholders and `upstream_revision` is
unfilled. Complete steps 1–5 of the staging process before approval:

1. Resolve the complete artifact set (weights, tokenizer, configs).
2. Verify the final download host + redirects against the allowlist
   (`--check-redirects`).
3. Pin the immutable upstream revision (commit hash, never a branch).
4. Record sha256 checksums and the reviewed license.
5. Publish to the internal registry (`internal_location`).

## Usage

```bash
# Schema + source-policy check (offline)
python -m src.cli validate manifests/olmocr-2-7b-1025-fp8.yaml

# Also verify the redirect target host (needs network, staging only)
python -m src.cli validate manifests/olmocr-2-7b-1025-fp8.yaml --check-redirects

# Verify staged file checksums
python -m src.cli verify manifests/olmocr-2-7b-1025-fp8.yaml \
  --root /models/allenai/olmOCR-2-7B-1025-FP8
```

## Policy

- `upstream_source` must be `https` on an allowlisted host.
- Prohibited: `modelscope.cn` (+ subdomains), `hf-mirror.com`,
  `hf-mirror.aliyuncs.com`, and any host not on the allowlist.
  Extend `DEFAULT_PROHIBITED_HOST_PATTERNS` per operator policy.
- `upstream_revision` must be an immutable pin — branch names (`main`,
  `latest`, …) warn loudly.

## Tests

```bash
pytest services/artifacts/tests/
```
