"""Group members own their uploads; managers may delete group documents."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.catalog.models import IngestionState
from services.common.request_context import RequestContext
from routes import documents as documents_routes


class FakeRepo:
    def __init__(self, actor: str, roles, uploader: str):
        self.context = RequestContext(
            user_id=actor, group_id="engineering", roles=tuple(roles)
        )
        self.document = SimpleNamespace(id=uuid.uuid4(), created_by=uploader)
    def get_document(self, document_id):
        assert document_id == self.document.id
        return self.document


def _call(repo, monkeypatch):
    monkeypatch.setattr(
        documents_routes, "create_deletion_job",
        lambda repo, document_id, queue_name: SimpleNamespace(
            id=uuid.uuid4(), state=IngestionState.DELETING,
        ),
    )
    monkeypatch.setattr(documents_routes, "enqueue_rq", lambda *args, **kwargs: None)
    session = SimpleNamespace(commit=lambda: None)
    settings = SimpleNamespace(
        druid_auth_mode="keycloak", delete_queue="delete",
        redis_url="redis://redis:6379/0",
    )
    return documents_routes.delete_document(
        repo.document.id, repo=repo, session=session, settings=settings,
    )


def test_member_may_not_delete_another_member_document(monkeypatch):
    repo = FakeRepo("bob", ("user", "member"), "alice")
    with pytest.raises(HTTPException) as caught:
        _call(repo, monkeypatch)
    assert caught.value.status_code == 403


@pytest.mark.parametrize("actor,roles,owner", [
    ("alice", ("user", "member"), "alice"),
    ("bob", ("user", "member", "group_admin"), "alice"),
    ("root", ("user", "admin"), "alice"),
])
def test_uploader_or_group_manager_may_delete(actor, roles, owner, monkeypatch):
    repo = FakeRepo(actor, roles, owner)
    response = _call(repo, monkeypatch)
    assert response.document_id == str(repo.document.id)
