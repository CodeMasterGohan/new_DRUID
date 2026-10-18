"""API-only administration exercises the same routes as the DRUID UI."""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.catalog.models import Base, DruidUser
from services.common.request_context import RequestContext
from deps import get_session, get_trusted_context
from routes.identity_admin import router


@pytest.fixture()
def system():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(DruidUser(id=uuid.uuid4(), subject="root", username="Root",
                         is_system_admin=True))
        db.commit()

    app = FastAPI()
    app.include_router(router)
    who = {"sub": "root", "roles": ("admin",)}

    def session():
        with Session(engine) as db:
            yield db

    def context():
        return RequestContext(
            user_id=who["sub"], group_id="__system__", roles=who["roles"]
        )

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_trusted_context] = context
    with TestClient(app) as client:
        yield client, who
    engine.dispose()


def test_admin_can_provision_groups_users_and_multiple_memberships(system):
    client, who = system
    assert client.post("/v1/admin/groups", json={"name": "engineering"}).status_code == 201
    assert client.post("/v1/admin/groups", json={"name": "research"}).status_code == 201
    assert client.post("/v1/admin/users", json={
        "subject": "alice", "username": "Alice",
    }).status_code == 201
    for group in ("engineering", "research"):
        response = client.put(
            f"/v1/admin/groups/{group}/members/alice",
            json={"subject": "alice", "role": "manager" if group == "research" else "member"},
        )
        assert response.status_code == 200, response.text
    data = client.get("/v1/admin/groups/research/members").json()
    assert data["members"][0]["role"] == "manager"
    assert client.delete("/v1/admin/groups/research/members/alice").status_code == 200
    assert client.get("/v1/admin/groups/research/members").json()["members"] == []


def test_non_admin_cannot_modify_or_list_users(system):
    client, who = system
    client.post("/v1/admin/users", json={"subject": "alice"})
    who["sub"], who["roles"] = "alice", ("admin",)  # malicious signed role
    assert client.get("/v1/admin/users").status_code == 403
    assert client.post("/v1/admin/groups", json={"name": "hacked"}).status_code == 403


def test_last_admin_guard_and_invalid_groups(system):
    client, who = system
    assert client.patch("/v1/admin/users/root", json={"is_system_admin": False}).status_code == 409
    assert client.post("/v1/admin/groups", json={"name": "UPPER NAME"}).status_code == 422
    assert client.post("/v1/admin/groups", json={"name": "engineering"}).status_code == 201
    assert client.post("/v1/admin/groups", json={"name": "engineering"}).status_code == 409


def test_group_deactivation_and_user_disabling_have_rest_endpoints(system):
    client, who = system
    assert client.post("/v1/admin/groups", json={"name": "engineering"}).status_code == 201
    assert client.patch("/v1/admin/groups/engineering", json={"is_active": False}).json()["is_active"] is False
    assert client.post("/v1/admin/users", json={"subject": "bob"}).status_code == 201
    assert client.patch("/v1/admin/users/bob", json={"is_active": False}).json()["is_active"] is False
