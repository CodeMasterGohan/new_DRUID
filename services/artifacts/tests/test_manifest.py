"""Unit tests for artifact manifest validation/verification (spec §3)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from src.artifact_manifest import (
    ArtifactManifest,
    ManifestError,
    ManifestFile,
    check_revision_pinned,
    check_source_policy,
    load_manifest,
    validate_manifest,
    verify_checksums,
)

GOOD_SHA = "a" * 64


def _manifest_doc(**overrides):
    doc = {
        "artifact_name": "org/test-model",
        "upstream_source": "https://huggingface.co/org/test-model",
        "upstream_revision": "abc123def456abc123def456abc123def456abc12345",
        "internal_location": "/models/org/test-model",
        "license": "Apache-2.0",
        "files": [{"path": "config.json", "sha256": GOOD_SHA}],
        "approval_record": "APPROVAL-1",
    }
    doc.update(overrides)
    return {"artifact_manifest": doc}


def _write_manifest(tmp_path: Path, **overrides) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(_manifest_doc(**overrides)), encoding="utf-8")
    return path


def _good_manifest() -> ArtifactManifest:
    return ArtifactManifest(
        artifact_name="org/test-model",
        upstream_source="https://huggingface.co/org/test-model",
        upstream_revision="abc123",
        internal_location="/models/org/test-model",
        license="Apache-2.0",
        files=(ManifestFile(path="config.json", sha256=GOOD_SHA),),
        approval_record="APPROVAL-1",
    )


# ── structural validation ─────────────────────────────────────────────


def test_valid_manifest_loads(tmp_path: Path):
    manifest = load_manifest(_write_manifest(tmp_path))
    assert manifest.artifact_name == "org/test-model"
    assert len(manifest.files) == 1


def test_missing_top_level_key_rejected(tmp_path: Path):
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump({"nope": 1}), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_missing_required_field_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(_write_manifest(tmp_path, license=""))


def test_empty_files_list_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(_write_manifest(tmp_path, files=[]))


def test_bad_sha256_format_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(
            _write_manifest(
                tmp_path, files=[{"path": "a.bin", "sha256": "not-a-hash"}]
            )
        )


def test_absolute_file_path_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(
            _write_manifest(
                tmp_path, files=[{"path": "/etc/passwd", "sha256": GOOD_SHA}]
            )
        )


def test_path_traversal_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(
            _write_manifest(
                tmp_path, files=[{"path": "../escape.bin", "sha256": GOOD_SHA}]
            )
        )


# ── source policy ─────────────────────────────────────────────────────


def test_huggingface_source_allowed():
    check_source_policy(_good_manifest())  # must not raise


def test_modelscope_source_prohibited():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "https://modelscope.cn/org/test-model"
    )
    with pytest.raises(ManifestError, match="prohibited"):
        check_source_policy(manifest)


def test_modelscope_subdomain_prohibited():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "https://www.modelscope.cn/org/test-model"
    )
    with pytest.raises(ManifestError, match="prohibited"):
        check_source_policy(manifest)


def test_mirror_host_prohibited():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "https://hf-mirror.com/org/test-model"
    )
    with pytest.raises(ManifestError, match="prohibited"):
        check_source_policy(manifest)


def test_unapproved_host_rejected():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "https://files.example-cdn.net/model.bin"
    )
    with pytest.raises(ManifestError, match="allowlist"):
        check_source_policy(manifest)


def test_non_https_rejected():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "http://huggingface.co/org/test-model"
    )
    with pytest.raises(ManifestError, match="https"):
        check_source_policy(manifest)


# ── revision pinning ──────────────────────────────────────────────────


def test_mutable_revision_warns():
    manifest = _good_manifest()
    object.__setattr__(manifest, "upstream_revision", "main")
    warnings = check_revision_pinned(manifest)
    assert any("mutable" in w for w in warnings)


def test_template_placeholder_warns():
    manifest = _good_manifest()
    object.__setattr__(manifest, "upstream_revision", "<immutable-commit-hash>")
    warnings = check_revision_pinned(manifest)
    assert warnings, "unfilled template placeholder must warn"


def test_pinned_revision_no_warnings():
    assert check_revision_pinned(_good_manifest()) == []


# ── checksum verification ─────────────────────────────────────────────


def test_checksums_verify_ok(tmp_path: Path):
    root = tmp_path / "staged"
    root.mkdir()
    (root / "config.json").write_bytes(b'{"a": 1}')
    digest = hashlib.sha256(b'{"a": 1}').hexdigest()
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "files", (ManifestFile(path="config.json", sha256=digest),)
    )
    assert verify_checksums(manifest, root) == []


def test_missing_file_reported(tmp_path: Path):
    errors = verify_checksums(_good_manifest(), tmp_path / "empty")
    assert any("missing" in e for e in errors)


def test_checksum_mismatch_reported(tmp_path: Path):
    root = tmp_path / "staged"
    root.mkdir()
    (root / "config.json").write_bytes(b"tampered")
    errors = verify_checksums(_good_manifest(), root)
    assert any("mismatch" in e for e in errors)


def test_duplicate_file_entry_reported(tmp_path: Path):
    root = tmp_path / "staged"
    root.mkdir()
    (root / "config.json").write_bytes(b"x")
    digest = hashlib.sha256(b"x").hexdigest()
    manifest = _good_manifest()
    object.__setattr__(
        manifest,
        "files",
        (
            ManifestFile(path="config.json", sha256=digest),
            ManifestFile(path="config.json", sha256=digest),
        ),
    )
    errors = verify_checksums(manifest, root)
    assert any("duplicate" in e for e in errors)


# ── end-to-end validate ───────────────────────────────────────────────


def test_validate_manifest_ok_on_good_manifest():
    assert validate_manifest(_good_manifest()) == []
