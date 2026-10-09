"""Deploy wiring checks: compose ↔ Dockerfiles ↔ entrypoints ↔ .env.example.

No Docker daemon required — these are static consistency checks over the
files themselves. They catch the class of bug where a compose service
references a Dockerfile CMD target, env var, or port that does not exist.
"""

import os
import re

import pytest
import yaml

BUILD_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load_compose():
    with open(os.path.join(BUILD_ROOT, "deploy", "docker-compose.yml")) as f:
        return yaml.safe_load(f)


def load_env_example():
    vars_ = {}
    with open(os.path.join(BUILD_ROOT, ".env.example")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                vars_[k.strip()] = v.strip()
    return vars_


def read_dockerfile(service):
    with open(os.path.join(BUILD_ROOT, "services", service, "Dockerfile")) as f:
        return f.read()


def test_compose_services_have_dockerfiles():
    compose = load_compose()
    for name, svc in compose["services"].items():
        build = svc.get("build")
        if not build:
            continue  # external images (postgres, redis, ...)
        dockerfile = os.path.join(
            BUILD_ROOT, build["dockerfile"])
        assert os.path.exists(dockerfile), f"{name}: {build['dockerfile']} missing"


def test_milvus_setup_cmd_matches_module():
    """Dockerfile CMD must resolve to the package entrypoint."""
    dockerfile = read_dockerfile("milvus_setup")
    assert "milvus_setup.create_collection" in dockerfile
    assert os.path.exists(
        os.path.join(BUILD_ROOT, "services", "milvus_setup", "create_collection.py")
    )
    assert os.path.exists(
        os.path.join(BUILD_ROOT, "services", "milvus_setup", "__init__.py")
    )


def test_gateway_cmd_matches_module():
    dockerfile = read_dockerfile("gateway")
    assert "app:app" in dockerfile
    assert os.path.exists(os.path.join(BUILD_ROOT, "services", "gateway", "app.py"))


def test_mcp_cmd_matches_factory():
    dockerfile = read_dockerfile("mcp_server")
    assert "mcp_server.server:create_app" in dockerfile


def test_catalog_cmd_matches_alembic_layout():
    dockerfile = read_dockerfile("catalog")
    assert "alembic" in dockerfile and "upgrade" in dockerfile and "head" in dockerfile
    assert os.path.exists(
        os.path.join(BUILD_ROOT, "services", "catalog", "alembic.ini"))
    # env.py does `from services.catalog.models import Base`, so the image
    # must preserve the services tree layout.
    assert "/app/services/catalog/" in dockerfile
    assert "/app/services/common/" in dockerfile


def test_services_tree_layout_wherever_services_star_imports():
    """Any image whose code does `from services.*` must copy the tree."""
    for service in ("gateway", "document_api", "ingestion", "retrieval"):
        dockerfile = read_dockerfile(service)
        svc_dir = os.path.join(BUILD_ROOT, "services", service)
        py_files = [f for f in os.listdir(svc_dir) if f.endswith(".py")]
        if not py_files:
            pytest.skip(f"{service} has no code yet")
        assert "/app/services/common/" in dockerfile, (
            f"{service}: code uses services.* imports but image lacks the tree")


def test_env_example_covers_gateway_settings():
    """Every non-defaulted GatewaySettings field needs a template entry."""
    env = load_env_example()
    for var in ("JWT_ISSUER", "JWT_AUDIENCE", "DOCUMENT_API_URL",
                "MCP_SERVER_URL", "INTERNAL_SECRET", "REDIS_URL"):
        assert var in env, f".env.example missing {var}"


def test_env_example_covers_mcp_settings():
    env = load_env_example()
    for var in ("JWT_SECRET", "MCP_ALLOWED_ORIGINS"):
        assert var in env, f".env.example missing {var}"


def test_env_example_covers_milvus_setup():
    env = load_env_example()
    for var in ("MILVUS_URI", "MILVUS_COLLECTION", "MILVUS_ALIAS"):
        assert var in env, f".env.example missing {var}"


def test_no_host_port_collision_between_compose_projects():
    """gateway and vllm-olmocr run on the same H200 host: host ports must
    not collide (spec §2: single host)."""
    compose = load_compose()
    with open(os.path.join(BUILD_ROOT, "deploy", "docker-compose.gpu.yml")) as f:
        gpu = yaml.safe_load(f)

    def host_ports(svc):
        ports = []
        for p in svc.get("ports", []):
            m = re.match(r"\$\{([A-Z_]+)(?::-(.*?))?\}:(\d+)", str(p))
            if m:
                ports.append(m.group(1))  # env-var-driven host port
            else:
                ports.append(str(p).split(":")[0])
        return ports

    cpu_ports, gpu_ports = [], []
    for svc in compose["services"].values():
        cpu_ports.extend(host_ports(svc))
    for svc in gpu["services"].values():
        gpu_ports.extend(host_ports(svc))
    # Resolve env-var ports via .env.example values.
    env = load_env_example()
    resolved_cpu = {env.get(v, v) for v in cpu_ports}
    resolved_gpu = {env.get(v, v) for v in gpu_ports}
    overlap = resolved_cpu & resolved_gpu - {""}
    assert not overlap, f"host port collision across compose projects: {overlap}"


def test_compose_env_vars_have_template_entries_or_defaults():
    """${VAR} references in compose must exist in .env.example or carry a
    ${VAR:-default} fallback."""
    compose = load_compose()
    text = open(os.path.join(BUILD_ROOT, "deploy", "docker-compose.yml")).read()
    text += open(os.path.join(BUILD_ROOT, "deploy", "docker-compose.gpu.yml")).read()
    env = load_env_example()
    missing = []
    for var, default in re.findall(r"\$\{([A-Z_]+)(?::-([^}]*))?\}", text):
        if var not in env and not default:
            missing.append(var)
    assert not missing, f"compose vars without template entry or default: {missing}"


def test_ingestion_image_preserves_services_tree_for_worker_entrypoints():
    """The RQ worker must import the dotted-path entry points the document
    API enqueues (services.ingestion.pipeline.run_ingestion_job /
    run_deletion_job), so the ingestion image must carry the services tree
    — not a flat copy of the ingestion sources."""
    dockerfile = read_dockerfile("ingestion")
    assert "/app/services/ingestion/" in dockerfile, (
        "ingestion image must copy services/ingestion/ as a tree so "
        "`services.ingestion.pipeline` is importable by the RQ worker")
    assert "PYTHONPATH=/app" in dockerfile or "PYTHONPATH" in dockerfile, (
        "ingestion image needs PYTHONPATH=/app for services.* imports")
    assert os.path.exists(
        os.path.join(BUILD_ROOT, "services", "ingestion", "pipeline.py"))
    for entrypoint in ("services.ingestion.pipeline.run_ingestion_job",
                       "services.ingestion.pipeline.run_deletion_job"):
        module_path, _, _ = entrypoint.rpartition(".")
        rel = os.path.join(BUILD_ROOT, *module_path.split(".")) + ".py"
        assert os.path.exists(rel), f"worker entry point module missing: {rel}"


def test_no_unexpanded_env_vars_in_exec_form_cmd():
    """Docker does not expand ${VAR} in exec-form (JSON-array) CMD/RUN.
    Any ${...} must appear only in shell-form instructions."""
    for service in ("gateway", "document_api", "mcp_server", "catalog",
                    "ingestion", "embedding", "retrieval", "milvus_setup"):
        dockerfile = read_dockerfile(service)
        for line in dockerfile.splitlines():
            stripped = line.strip()
            if re.match(r'^(CMD|ENTRYPOINT)\s*\[', stripped):
                assert "${" not in stripped, (
                    f"{service}: exec-form CMD/ENTRYPOINT contains "
                    f"unexpanded ${{...}}: {stripped}")


def test_dockerignore_keeps_dev_files_and_secrets_out_of_images():
    """.venv/, __pycache__/, tests/ and .env must not ship in images —
    document_api has a 90MB .venv that its Dockerfile would otherwise COPY."""
    path = os.path.join(BUILD_ROOT, ".dockerignore")
    assert os.path.exists(path), "build-root .dockerignore missing"
    content = open(path).read()
    for pattern in (".venv/", "__pycache__/", ".env"):
        assert pattern in content, f".dockerignore missing {pattern}"


def test_shared_network_for_ingestion_to_vllm():
    """Ingestion workers call vLLM by its compose service name, but the CPU
    and GPU planes are separate compose projects (separate default
    networks). Both must attach to one shared external network, and the
    olmocr client's default URL must name the actual gpu service."""
    cpu = load_compose()
    with open(os.path.join(BUILD_ROOT, "deploy", "docker-compose.gpu.yml")) as f:
        gpu = yaml.safe_load(f)
    for name, compose in (("cpu", cpu), ("gpu", gpu)):
        nets = compose.get("networks", {})
        assert "druid-net" in nets, f"{name} compose missing druid-net"
        assert nets["druid-net"].get("external") is True, (
            f"{name}: druid-net must be external (created once per host)")
    assert "druid-net" in (cpu["services"]["ingestion"].get("networks") or []), (
        "ingestion must attach to druid-net to reach vLLM")
    assert "druid-net" in (gpu["services"]["vllm-olmocr"].get("networks") or []), (
        "vllm-olmocr must attach to druid-net")
    # The hostname the client dials must be the real compose service name.
    client = open(os.path.join(
        BUILD_ROOT, "services", "ingestion", "olmocr_client.py")).read()
    assert "http://vllm-olmocr:8000" in client, (
        "olmocr_client default URL must match the gpu compose service name")
    assert "vllm-olmocr" in gpu["services"], "gpu service name changed?"
    env = load_env_example()
    assert env.get("OLMOCR_VLLM_URL", "").startswith("http://vllm-olmocr:"), (
        ".env.example OLMOCR_VLLM_URL must point at the vllm-olmocr service")
