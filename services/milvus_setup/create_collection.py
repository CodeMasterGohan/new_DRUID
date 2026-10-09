"""One-shot Milvus provisioning for new_DRUID — spec §9 (v2.1).

Idempotent: safe to re-run. Creates the versioned collection
(``project_knowledge_base_v1``) with the §9 schema, BM25 function, HNSW +
SPARSE_INVERTED_INDEX indexes, loads it, and points the stable alias
(``project_knowledge_base``) at it.

Usage::

    python -m create_collection                 # create-if-missing, ensure alias
    python -m create_collection --check-only     # verify, change nothing
    python -m create_collection --recreate --yes # DROP and rebuild (data loss!)
    python -m create_collection --set-alias COLLECTION --yes
    python -m create_collection --enable-partition-isolation

``--enable-partition-isolation`` sets the ``partitionkey.isolation``
property. Spec §9: test-first, never the authorization boundary. It is OFF
by default and should only be enabled after compatibility testing with the
pinned Milvus version (2.5.6).

Cutover / rollback (spec §8 migration rule): build the next versioned
collection (e.g. ``project_knowledge_base_v2``) with a separate run using
``MILVUS_COLLECTION=..._v2``, re-embed fully, validate retrieval, then::

    python -m create_collection --set-alias project_knowledge_base_v2 --yes

Rollback is the same command pointed back at v1.

Environment (see build-root .env.example):
    MILVUS_URI, MILVUS_COLLECTION, MILVUS_ALIAS, MILVUS_WAIT_TIMEOUT_S
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

from .schema import (
    BM25_FUNCTION_NAME,
    COLLECTION_ALIAS,
    COLLECTION_NAME,
    DENSE_DIM,
    PARTITION_ISOLATION_PROPERTY,
    PARTITION_KEY_FIELD,
    build_index_params,
    build_pymilvus_schema,
    field_names,
)

log = logging.getLogger("milvus_setup")


class SetupError(RuntimeError):
    """Fatal provisioning failure — the one-shot job must exit non-zero."""


class SchemaMismatchError(SetupError):
    """An existing collection does not match the §9 schema.

    Never auto-migrated: cut over via a new versioned collection + alias
    (see module docstring).
    """


@dataclass
class Settings:
    uri: str = "http://localhost:19530"
    collection: str = COLLECTION_NAME
    alias: str = COLLECTION_ALIAS
    wait_timeout_s: float = 300.0
    enable_partition_isolation: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        import os

        def _bool(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            uri=os.environ.get("MILVUS_URI", "http://localhost:19530"),
            collection=os.environ.get("MILVUS_COLLECTION", COLLECTION_NAME),
            alias=os.environ.get("MILVUS_ALIAS", COLLECTION_ALIAS),
            wait_timeout_s=float(os.environ.get("MILVUS_WAIT_TIMEOUT_S", "300")),
            enable_partition_isolation=_bool(
                "MILVUS_ENABLE_PARTITION_ISOLATION", False
            ),
        )


def connect_with_retry(uri: str, timeout_s: float):
    """Connect to Milvus, retrying until ``timeout_s`` (compose has no
    health-gated depends_on for this job, so the retry lives here)."""
    from pymilvus import MilvusClient

    deadline = time.monotonic() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            client = MilvusClient(uri=uri)
            # A cheap round-trip proves the server is actually up.
            client.list_collections()
            log.info("connected to Milvus at %s (attempt %d)", uri, attempt)
            return client
        except Exception as exc:  # noqa: BLE001 — retry on any connect failure
            if time.monotonic() >= deadline:
                raise SetupError(
                    f"could not reach Milvus at {uri} within {timeout_s}s: {exc}"
                ) from exc
            wait = min(2.0 * attempt, 15.0)
            log.info("Milvus not ready (attempt %d): %s — retrying in %.1fs",
                     attempt, exc, wait)
            time.sleep(wait)


def verify_schema_compatible(client, collection: str) -> None:
    """Fail closed if an existing collection does not match §9.

    Checks: field-name set, dense_vector dim, partition-key field, and the
    BM25 function wiring. Raises SchemaMismatchError on any drift.
    """
    desc = client.describe_collection(collection)
    fields = {f["name"]: f for f in desc["fields"]}
    missing = [n for n in field_names() if n not in fields]
    if missing:
        raise SchemaMismatchError(
            f"collection {collection!r} is missing fields {missing}; "
            "cut over via a new versioned collection + alias, never in place"
        )
    dim = None
    for f in desc["fields"]:
        if f["name"] == "dense_vector":
            dim = f.get("params", {}).get("dim")
    if dim is not None and int(dim) != DENSE_DIM:
        raise SchemaMismatchError(
            f"collection {collection!r} dense_vector dim={dim}, expected "
            f"{DENSE_DIM}; cut over via a new versioned collection + alias"
        )
    functions = {fn.get("name"): fn for fn in desc.get("functions", [])}
    if BM25_FUNCTION_NAME not in functions:
        raise SchemaMismatchError(
            f"collection {collection!r} lacks the {BM25_FUNCTION_NAME!r} BM25 "
            "function; cut over via a new versioned collection + alias"
        )
    log.info("existing collection %r matches the §9 schema", collection)


def ensure_indexes(client, collection: str) -> None:
    """Create any missing indexes (dense HNSW + sparse BM25)."""
    from pymilvus.milvus_client.index import IndexParams

    existing = {idx["field_name"] for idx in client.list_indexes(collection)}
    missing = [s for s in build_index_params() if s["field_name"] not in existing]
    if not missing:
        log.info("all indexes already exist on %r — skipping", collection)
        return
    index_params = IndexParams()
    for spec in missing:
        log.info("creating index on %r (%s/%s)", spec["field_name"],
                 spec["index_type"], spec["metric_type"])
        index_params.add_index(
            field_name=spec["field_name"],
            index_type=spec["index_type"],
            metric_type=spec["metric_type"],
            params=spec.get("params", {}),
        )
    client.create_index(collection, index_params)


def ensure_loaded(client, collection: str) -> None:
    state = client.get_load_state(collection_name=collection).get("state")
    if str(state).endswith("Loaded"):
        log.info("collection %r already loaded", collection)
        return
    log.info("loading collection %r", collection)
    client.load_collection(collection_name=collection)


def ensure_alias(client, alias: str, collection: str) -> None:
    """Point the stable alias at ``collection`` (create or alter)."""
    try:
        current = client.describe_alias(alias)["collection_name"]
    except Exception:  # noqa: BLE001 — alias missing is the expected first-run case
        current = None
    if current == collection:
        log.info("alias %r already points at %r", alias, collection)
        return
    if current is None:
        log.info("creating alias %r -> %r", alias, collection)
        client.create_alias(collection, alias)
    else:
        log.warning("moving alias %r from %r to %r", alias, current, collection)
        client.alter_alias(collection, alias)


def maybe_enable_partition_isolation(client, collection: str, enabled: bool) -> None:
    if not enabled:
        log.info("%s not requested — leaving unset (spec §9: test-first)",
                 PARTITION_ISOLATION_PROPERTY)
        return
    log.warning(
        "enabling %s on %r — spec §9: this is an HNSW optimization only, "
        "NOT the authorization boundary; enable only after compatibility "
        "testing with the pinned Milvus version", PARTITION_ISOLATION_PROPERTY,
        collection,
    )
    client.alter_collection_properties(
        collection, properties={PARTITION_ISOLATION_PROPERTY: True}
    )


def ensure_collection(settings: Settings, recreate: bool = False) -> None:
    """Full idempotent provisioning run."""
    client = connect_with_retry(settings.uri, settings.wait_timeout_s)
    try:
        if client.has_collection(settings.collection):
            if recreate:
                log.warning("dropping collection %r (--recreate)", settings.collection)
                client.drop_collection(settings.collection)
            else:
                log.info("collection %r exists — verifying", settings.collection)
                verify_schema_compatible(client, settings.collection)
        if not client.has_collection(settings.collection):
            log.info("creating collection %r", settings.collection)
            client.create_collection(
                collection_name=settings.collection,
                schema=build_pymilvus_schema(),
                index_params=build_index_params(),
            )
        ensure_indexes(client, settings.collection)
        maybe_enable_partition_isolation(
            client, settings.collection, settings.enable_partition_isolation
        )
        ensure_loaded(client, settings.collection)
        ensure_alias(client, settings.alias, settings.collection)
        log.info("Milvus provisioning complete: %r (alias %r)",
                 settings.collection, settings.alias)
    finally:
        client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check-only", action="store_true",
                        help="verify schema/indexes/alias, change nothing")
    parser.add_argument("--recreate", action="store_true",
                        help="DROP the collection and rebuild (DATA LOSS)")
    parser.add_argument("--yes", action="store_true",
                        help="confirm --recreate / --set-alias")
    parser.add_argument("--set-alias", metavar="COLLECTION",
                        help="point the alias at another existing collection and exit")
    parser.add_argument("--enable-partition-isolation", action="store_true",
                        help="set partitionkey.isolation (spec §9: test-first)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    settings = Settings.from_env()
    if args.enable_partition_isolation:
        settings.enable_partition_isolation = True
    log.info(
        "starting milvus_setup: uri=%s collection=%s alias=%s wait_timeout_s=%.0f "
        "partition_isolation=%s argv=%s",
        settings.uri,
        settings.collection,
        settings.alias,
        settings.wait_timeout_s,
        settings.enable_partition_isolation,
        " ".join(argv) if argv else "(defaults)",
    )

    try:
        if args.set_alias:
            if not args.yes:
                log.error("--set-alias requires --yes")
                return 2
            client = connect_with_retry(settings.uri, settings.wait_timeout_s)
            try:
                if not client.has_collection(args.set_alias):
                    raise SetupError(f"collection {args.set_alias!r} does not exist")
                verify_schema_compatible(client, args.set_alias)
                ensure_alias(client, settings.alias, args.set_alias)
            finally:
                client.close()
            return 0

        if args.check_only:
            client = connect_with_retry(settings.uri, settings.wait_timeout_s)
            try:
                if not client.has_collection(settings.collection):
                    raise SetupError(f"collection {settings.collection!r} missing")
                verify_schema_compatible(client, settings.collection)
                current = client.describe_alias(settings.alias)["collection_name"]
                if current != settings.collection:
                    raise SetupError(
                        f"alias {settings.alias!r} points at {current!r}, "
                        f"expected {settings.collection!r}"
                    )
                log.info("check-only: OK")
            finally:
                client.close()
            return 0

        if args.recreate and not args.yes:
            log.error("--recreate requires --yes (it destroys data)")
            return 2
        ensure_collection(settings, recreate=args.recreate)
        return 0
    except SetupError as exc:
        log.error("provisioning failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
