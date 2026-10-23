# Markdown uploads: behavior and verification

DRUID accepts UTF-8 `.md` and `.markdown` uploads through the browser, direct
multipart API, and registration/ticket API. Each file is one logical page for
page quotas. Byte limits, checksums, tenant ownership, job admission, and version
activation still apply. Markdown uses `parser=markdown`, revision `markdown-v1`.

The existing Markdown block parser preserves headings, paragraphs, pipe tables,
and fenced code in the canonical tree. This is a retrieval parser, not a complete
CommonMark renderer. Source Markdown never receives OCR provenance or invented
geometry. Both CPU and GPU modes use this path without PDF parsing or OCR.

## Deployment prerequisite

Stage the normal embedding model and reranker tokenizer, then measure a reviewed
Markdown corpus with the deployed tokenizer/chunker and import a baseline for
`markdown / markdown-v1` using `services.catalog.baselines`. The existing PDF
baseline does not apply. Do not copy the test baseline: its chunk counts use a
fixture tokenizer. Missing or out-of-range baselines fail activation and leave
the document unsearchable; this change does not weaken that gate. Follow the
existing immutable-baseline procedure when calibration changes.

## Root cause

The requested behavior was a manual Markdown upload that could become searchable.
The original implementation supported only PDF and page images; this was missing
support, rather than a regression in a previously working Markdown path.

At parent commit `1a53fc5`, the exact failing paths were:

* Direct upload: `routes/documents.py:243` called `validation.validate_mime` with
  only the first 16 bytes. `validation.py:60` could recognize only binary file
  signatures and rejected Markdown with HTTP 400, "unrecognized file type".
* Ticket upload: `_validate_declaration` at `routes/documents.py:378` rejected
  `text/markdown` because `validation.py:16` allowed only PDF/images. No worker
  was enqueued.
* Bypassing API validation was insufficient: `_parse_document` at
  `services/ingestion/pipeline.py:445` passed all originals to
  `extract_pages_native`, where PDFium raised `Data format error` on Markdown.
* The browser's `_ALLOWED_EXTENSIONS` in `services/web_ui/routes/partials.py:37`
  also omitted Markdown.

Existing tests covered PDF/image acceptance and malformed binary uploads; none
required a Markdown upload followed by ingestion and search. Recent history
inspected included `9137cdb` (CPU ingestion) and `3fa0116` (production readiness).

## Deterministic reproduction

This test was added and run before production changes:

```python
def test_upload_markdown(client, auth_headers, enqueued):
    content = b'# GitLab CI\n\nUse include:local for reusable pipeline configuration.\n'
    response = client.post('/v1/documents/upload', headers=auth_headers,
        files={'file': ('gitlab.md', content, 'text/markdown')})
    assert response.status_code == 201, response.text
    assert response.json()['page_count'] == 1
    assert len(enqueued) == 1
```

```bash
python -m pytest -q services/document_api/tests/test_markdown_upload.py
python -m pytest -q services/ingestion/tests/test_markdown.py
```

Before the change, the two upload tests failed with the HTTP 400 errors above.
The worker reproduction failed in both processing modes with:

```text
pypdfium2._helpers.misc.PdfiumError: Failed to load document (PDFium: Data format error).
2 failed in 0.40s
```

The ticket-redemption assertion uses the existing HTTP 200 contract; registration
and direct upload use HTTP 201.

## The fix

The route now validates the complete bounded stream, then retains the usual
upload/storage/queue path:

```diff
- mime = validate_mime(data.header, file.content_type)
+ mime = validate_upload(data.stream, data.header, file.content_type, file.filename)
```

Markdown validation requires a Markdown extension, a compatible text/generic MIME
type, valid UTF-8 throughout, nonempty text, and no binary control characters.
Known binary signatures still use the existing signature/MIME checks. Incremental
decoding handles multibyte UTF-8 characters split across read boundaries. The
stream is rewound before storage. Ticket redemption validates the registered
filename/MIME and retains exact size/SHA-256 checks.

The worker branches on validated `content_type=text/markdown`, builds the
canonical tree using the existing Markdown block parser, and persists JSON and
derived Markdown artifacts. Chunking, embedding, indexing, and activation use
their existing stages. The browser extension list and file chooser match the API.

## Verification evidence

Run the backend and UI suites separately because each service uses top-level
module names in its container:

```bash
DRUID_PROCESSING_MODE=cpu python -m pytest -q services tests --ignore=services/web_ui/tests
(cd services/web_ui && python -m pytest -q tests)
```

Captured suite summaries (the backend run enabled disposable PostgreSQL/Redis
integration tests through `DRUID_TEST_DATABASE_URL` and `DRUID_TEST_REDIS_URL`):

```text
547 passed, 13 skipped, 58 warnings in 48.64s
44 passed, 117 warnings in 2.83s
```

Skipped tests require optional services/artifacts; these counts do not claim GPU
or pretrained inference coverage. The warnings concern test dependency
compatibility and fixture JWT key lengths.

The live test additionally requires disposable PostgreSQL, Redis, Milvus, and
S3-compatible storage. Set these environment variables to the test services:

* `DRUID_TEST_DATABASE_URL`, `DRUID_TEST_REDIS_URL`, `DRUID_TEST_MILVUS_URI`
* `DRUID_TEST_S3_ENDPOINT` (host:port), `DRUID_TEST_S3_ACCESS_KEY`,
  `DRUID_TEST_S3_SECRET_KEY`
* Optionally `DRUID_TEST_MARKDOWN_PATH` pointing to the Markdown source to upload.

```bash
DRUID_TEST_MARKDOWN_LIVE=1 python -m pytest -q -s services/document_api/tests/test_markdown_live.py
```

The test creates and removes its own schema, queue, bucket, and collection. It
uses gateway-signed internal headers, the real API routes, real RQ workers, and
real storage/index adapters. It does not validate external OIDC login or semantic
model quality: embeddings and token counting are explicit deterministic fixtures.
The test baseline is measured with those fixtures and exists only in its
disposable schema.

On 2026-10-23, the actual GitLab `doc/ci/yaml/includes.md` source from tag
`v18.4.0-ee` was uploaded through both API flows. Its SHA-256 was
`2f12b69be7aad0160dae073c6b5b2e2391c697cf5d72c8cb128e07d2f7fb81af`.
Captured terminal output:

```text
PASS direct: 19888 bytes uploaded; READY; 155 chunks; BM25 finds GitLab content; foreign tenant blocked
PASS ticket: 19888 bytes uploaded; READY; 155 chunks; BM25 finds GitLab content; foreign tenant blocked
1 passed, 1 warning in 20.76s
```

The separate browser regression test verifies resumable chunk assembly and the
exact Markdown bytes forwarded to the gateway. Invalid UTF-8, binary disguises,
empty text, extension mismatches, stable node IDs, table provenance, and the
separate Markdown activation baseline are covered by deterministic regressions.
