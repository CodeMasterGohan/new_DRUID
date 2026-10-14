"""Artifact manifest validation and verification (spec §3).

Every approved model/dependency carries a manifest with a pinned upstream
revision, sha256 checksums, and a reviewed license. This module:

- parses and structurally validates manifests,
- enforces the source allowlist (prohibits modelscope.cn, Chinese mirrors,
  and unapproved endpoints),
- verifies sha256 checksums of staged files.

Staging-time only. Nothing here runs in production services.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

import yaml

MANIFEST_VERSION = "1"

# Hosts permitted in ``upstream_source``. Staging operators extend this list;
# production never downloads, so this only gates the staging workstation.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs.hf.co",
    "github.com",
    "raw.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
)

# Prohibited host patterns (matched against the URL host and its parent
# domains). This list is the floor, not the ceiling — operators extend it.
# modelscope.cn and Chinese mirror repositories are prohibited by spec §1.
DEFAULT_PROHIBITED_HOST_PATTERNS: tuple[str, ...] = (
    "modelscope.cn",
    "hf-mirror.com",
    "hf-mirror.aliyuncs.com",
)

# Revision values that are NOT immutable pins and must be rejected.
_MUTABLE_REVISIONS = frozenset(
    {"main", "master", "latest", "stable", "dev", "develop", "head"}
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ManifestError(ValueError):
    """Raised when a manifest is structurally invalid or violates policy."""


@dataclass(frozen=True)
class ManifestFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class ArtifactManifest:
    artifact_name: str
    upstream_source: str
    upstream_revision: str
    internal_location: str
    license: str
    files: tuple[ManifestFile, ...]
    approval_record: str


def _require_str(mapping: dict, key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}: '{key}' must be a non-empty string")
    return value.strip()


def load_manifest(path: str | Path) -> ArtifactManifest:
    """Parse a manifest YAML file and structurally validate it."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict) or "artifact_manifest" not in raw:
        raise ManifestError(f"{path}: top-level 'artifact_manifest' mapping required")
    doc = raw["artifact_manifest"]
    if not isinstance(doc, dict):
        raise ManifestError(f"{path}: 'artifact_manifest' must be a mapping")

    files_raw = doc.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise ManifestError(f"{path}: 'files' must be a non-empty list")
    files: list[ManifestFile] = []
    for i, entry in enumerate(files_raw):
        if not isinstance(entry, dict):
            raise ManifestError(f"{path}: files[{i}] must be a mapping")
        file_path = _require_str(entry, "path", f"{path}: files[{i}]")
        sha256 = _require_str(entry, "sha256", f"{path}: files[{i}]")
        if not _SHA256_RE.match(sha256):
            raise ManifestError(
                f"{path}: files[{i}].sha256 must be a 64-char hex digest"
            )
        p_posix = PurePosixPath(file_path)
        p_win = PureWindowsPath(file_path)
        if (
            p_posix.is_absolute()
            or p_win.is_absolute()
            or file_path.startswith(("/", "\\"))
            or ".." in p_posix.parts
            or ".." in p_win.parts
        ):
            raise ManifestError(
                f"{path}: files[{i}].path must be a relative path without '..'"
            )
        files.append(ManifestFile(path=file_path, sha256=sha256.lower()))

    return ArtifactManifest(
        artifact_name=_require_str(doc, "artifact_name", str(path)),
        upstream_source=_require_str(doc, "upstream_source", str(path)),
        upstream_revision=_require_str(doc, "upstream_revision", str(path)),
        internal_location=_require_str(doc, "internal_location", str(path)),
        license=_require_str(doc, "license", str(path)),
        files=tuple(files),
        approval_record=_require_str(doc, "approval_record", str(path)),
    )


def _host_matches(host: str, pattern: str) -> bool:
    host = host.lower()
    pattern = pattern.lower()
    return host == pattern or host.endswith("." + pattern)


def check_source_policy(
    manifest: ArtifactManifest,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
    prohibited_patterns: tuple[str, ...] = DEFAULT_PROHIBITED_HOST_PATTERNS,
) -> None:
    """Enforce the staging source policy. Raises ManifestError on violation."""
    parsed = urlparse(manifest.upstream_source)
    if parsed.scheme != "https":
        raise ManifestError(
            f"upstream_source must be an https URL, got: {manifest.upstream_source!r}"
        )
    host = parsed.hostname or ""
    if not host:
        raise ManifestError(f"upstream_source has no host: {manifest.upstream_source!r}")
    for pattern in prohibited_patterns:
        if _host_matches(host, pattern):
            raise ManifestError(
                f"upstream_source host {host!r} is prohibited "
                f"(matches prohibited pattern {pattern!r})"
            )
    if not any(_host_matches(host, allowed) for allowed in allowed_hosts):
        raise ManifestError(
            f"upstream_source host {host!r} is not on the staging allowlist"
        )


def check_revision_pinned(manifest: ArtifactManifest) -> list[str]:
    """Return warnings for revisions that are not immutable pins."""
    warnings: list[str] = []
    rev = manifest.upstream_revision
    if rev.lower() in _MUTABLE_REVISIONS:
        warnings.append(
            f"upstream_revision {rev!r} is a mutable ref, not an immutable pin"
        )
    if rev.startswith("<") and rev.endswith(">"):
        warnings.append(
            f"upstream_revision {rev!r} looks like an unfilled template placeholder"
        )
    return warnings


def validate_manifest(
    manifest: ArtifactManifest,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
    prohibited_patterns: tuple[str, ...] = DEFAULT_PROHIBITED_HOST_PATTERNS,
) -> list[str]:
    """Full policy validation. Raises ManifestError on violation; returns warnings."""
    check_source_policy(
        manifest, allowed_hosts=allowed_hosts, prohibited_patterns=prohibited_patterns
    )
    return check_revision_pinned(manifest)


def verify_checksums(manifest: ArtifactManifest, root: str | Path) -> list[str]:
    """Verify sha256 of every staged file under ``root``.

    Returns a list of error strings; empty means all files verified.
    """
    root = Path(root)
    errors: list[str] = []
    seen: set[str] = set()
    for entry in manifest.files:
        if entry.path in seen:
            errors.append(f"duplicate file entry: {entry.path}")
            continue
        seen.add(entry.path)
        target = root / entry.path
        if not target.is_file():
            errors.append(f"missing staged file: {entry.path}")
            continue
        digest = hashlib.sha256()
        with target.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != entry.sha256:
            errors.append(
                f"checksum mismatch: {entry.path} "
                f"(expected {entry.sha256[:12]}…, got {actual[:12]}…)"
            )
    return errors


def check_redirects(manifest: ArtifactManifest, timeout: float = 10.0) -> list[str]:
    """Staging-time check: resolve the upstream URL and verify the final host
    is still policy-compliant after redirects. Requires network — run on the
    staging workstation only, never in production."""
    import urllib.request

    warnings: list[str] = []
    request = urllib.request.Request(
        manifest.upstream_source, method="HEAD", headers={"User-Agent": "druid-staging/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            final_host = urlparse(response.geturl()).hostname or ""
    except Exception as exc:  # staging-time diagnostic, not a policy failure
        warnings.append(f"could not resolve upstream URL: {exc}")
        return warnings
    try:
        check_source_policy(
            ArtifactManifest(
                artifact_name=manifest.artifact_name,
                upstream_source=f"https://{final_host}/",
                upstream_revision=manifest.upstream_revision,
                internal_location=manifest.internal_location,
                license=manifest.license,
                files=manifest.files,
                approval_record=manifest.approval_record,
            )
        )
    except ManifestError as exc:
        warnings.append(f"redirect target violates source policy: {exc}")
    return warnings
