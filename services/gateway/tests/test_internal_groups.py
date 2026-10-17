"""Gateway tenant selection is DB-authorized, never taken from Keycloak roles."""
import importlib
import uuid

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.catalog.models import Base, DruidUser, GroupMembership, Tenant

gateway = importlib.import_module("services.gateway.app")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    file = tmp_path / "membership.sqlite"
    url = "sqlite+pysqlite:///" + str(file)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        alice = DruidUser(id=uuid.uuid4(), subject="alice", username="Alice")
        bob = DruidUser(id=uuid.uuid4(), subject="bob", username="Bob")
        eng = Tenant(id=uuid.uuid4(), name="engineering", is_active=True)
        research = Tenant(id=uuid.uuid4(), name="research", is_active=True)
        session.add_all([alice, bob, eng, research])
        session.flush()
        session.add_all([
            GroupMembership(id=uuid.uuid4(), user_id=alice.id, tenant_id=eng.id,
                            role="member"),
            GroupMembership(id=uuid.uuid4(), user_id=alice.id, tenant_id=research.id,
                            role="manager"),
            GroupMembership(id=uuid.uuid4(), user_id=bob.id, tenant_id=eng.id,
                            role="member"),
        ])
        session.commit()

    monkeypatch.setenv("DRUID_AUTH_MODE", "keycloak")
    monkeypatch.delenv("REDIS_URL", raising=False)
    subject = {"sub": "alice"}

    def fake_token(token, jwt_settings, **kwargs):
        assert kwargs["require_group"] is False
        return {"user_id": subject["sub"], "group_id": "forged-token-group",
                "roles": ("admin",)}  # must be discarded by resolver

    async def fake_forward(request, context, upstream, **kwargs):
        return JSONResponse({"user": context.user_id, "group": context.group_id,
                             "roles": list(context.roles)})

    monkeypatch.setattr(gateway, "validate_token", fake_token)
    monkeypatch.setattr(gateway.proxy_module, "forward_request", fake_forward)

    settings = gateway.GatewaySettings(
        database_url=url, jwt_algorithms="RS256",
        jwt_issuer="https://keycloak.example.test/realms/druid",
        jwt_audience="druid-api",
        jwks_url="https://keycloak.example.test/certs",
        internal_secret="0123456789abcdef" * 4,
    )
    with TestClient(gateway.create_app(settings)) as client:
        yield client, subject, engine
    engine.dispose()


def _get(client, group=None):
    headers = {"Authorization": "Bearer test-validated-by-mock"}
    if group:
        headers["X-Group-Selection"] = group
    return client.get("/v1/documents", headers=headers)


def test_gateway_ignores_forged_token_roles_and_group(env):
    client, who, _ = env
    data = _get(client).json()
    assert data["group"] == "engineering"
    assert "admin" not in data["roles"]
    assert "user" in data["roles"]


def test_one_user_accesses_multiple_groups_with_independent_permissions(env):
    client, who, _ = env
    m = _get(client, "research")
    assert m.status_code == 200
    assert "group_admin" in m.json()["roles"]
    assert "reviewer" in m.json()["roles"]


def test_unowned_group_selection_is_rejected(env):
    client, who, _ = env
    who["sub"] = "bob"
    response = _get(client, "research")
    assert response.status_code == 401
    assert "admin" not in response.text


def test_membership_revocation_takes_effect_without_new_jwt(env):
    client, who, engine = env
    assert _get(client, "research").status_code == 200
    with Session(engine) as session:
        group = session.query(Tenant).filter_by(name="research").one()
        session.query(GroupMembership).filter_by(tenant_id=group.id).delete()
        session.commit()
    assert _get(client, "research").status_code == 401
