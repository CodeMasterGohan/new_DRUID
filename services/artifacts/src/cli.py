"""CLI for artifact manifest staging checks (spec §3).

Staging-workstation only. Usage:

    python -m src.cli validate manifests/olmocr-2-7b-1025-fp8.yaml
    python -m src.cli verify manifests/olmocr-2-7b-1025-fp8.yaml --root /models/allenai/olmOCR-2-7B-1025-FP8
    python -m src.cli validate manifests/x.yaml --check-redirects
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .artifact_manifest import (
    ManifestError,
    check_redirects,
    load_manifest,
    validate_manifest,
    verify_checksums,
)

log = logging.getLogger(__name__)


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
        warnings = validate_manifest(manifest)
    except ManifestError as exc:
        log.error("manifest INVALID: %s", exc)
        # Verdict line: the CLI result payload (stderr, exit code 1).
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        log.warning("manifest warning: %s", warning)
    if args.check_redirects:
        for warning in check_redirects(manifest):
            log.warning("redirect warning: %s", warning)
    log.info("manifest valid: %s", manifest.artifact_name)
    # Verdict line: the CLI result payload (stdout, exit code 0).
    print(f"OK: {manifest.artifact_name} manifest is valid")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest(manifest)
    except ManifestError as exc:
        log.error("manifest INVALID: %s", exc)
        # Verdict line: the CLI result payload (stderr, exit code 1).
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    errors = verify_checksums(manifest, args.root)
    if errors:
        for error in errors:
            log.error("checksum FAILED: %s", error)
            # Verdict lines: the CLI result payload (stderr, exit code 1).
            print(f"FAILED: {error}", file=sys.stderr)
        return 1
    log.info(
        "checksums verified: %d files for %s",
        len(manifest.files),
        manifest.artifact_name,
    )
    # Verdict line: the CLI result payload (stdout, exit code 0).
    print(f"OK: all {len(manifest.files)} staged files verified for {manifest.artifact_name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="new_DRUID artifact manifest tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Validate manifest schema and source policy")
    p_validate.add_argument("manifest", type=Path)
    p_validate.add_argument(
        "--check-redirects",
        action="store_true",
        help="Resolve upstream URL and check the redirect target (needs network)",
    )
    p_validate.set_defaults(func=_cmd_validate)

    p_verify = sub.add_parser("verify", help="Verify sha256 checksums of staged files")
    p_verify.add_argument("manifest", type=Path)
    p_verify.add_argument("--root", type=Path, required=True, help="Staged artifact root")
    p_verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    log.info(
        "starting artifacts cli: command=%s manifest=%s",
        args.command,
        args.manifest,
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
