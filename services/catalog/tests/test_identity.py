"""Real in-memory SQL membership and authorization contract tests."""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.catalog.identity import (
    IdentityDenied, get_user, memberships, provision_first_admin, resolve,
)
from services.catalog.models import Base, Document, DruidUser, GroupMembership, Tenant
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed(session):
    alice = DruidUser(id=uuid.uuid4(), subject="alice-sub", username="Alice")
    bob = DruidUser(id=uuid.uuid4(), subject="bob-sub", username="Bob")
    alpha = Tenant(id=uuid.uuid4(), name="engineering", is_active=True)
    bravo = Tenant(id=uuid.uuid4(), name="research", is_active=True)
    session.add_all([alice, bob, alpha, bravo])
    session.flush()
    session.add_all([
        GroupMembership(id=uuid.uuid4(), user_id=alice.id,
                        tenant_id=alpha.id, role="member"),
        GroupMembership(id=uuid.uuid4(), user_id=alice.id,
                        tenant_id=bravo.id, role="manager"),
        GroupMembership(id=uuid.uuid4(), user_id=bob.id,
                        tenant_id=alpha.id, role="member"),
    ])
    session.flush()
    return alice, bob, alpha, bravo


def test_multiple_groups_have_independent_roles(session):
    _seed(session)
    a = resolve(session, "alice-sub", "engineering")
    b = resolve(session, "alice-sub", "research")
    assert a.group_id == "engineering"
    assert "group_admin" not in a.roles
    assert b.group_id == "research"
    assert "group_admin" in b.roles and "reviewer" in b.roles


def test_forged_group_selection_and_unknown_user_rejected(session):
    _seed(session)
    with pytest.raises(IdentityDenied):
        resolve(session, "bob-sub", "research")
    with pytest.raises(IdentityDenied):
        resolve(session, "not-provisioned", "engineering")


def test_membership_revocation_and_group_disable_apply_immediately(session):
    alice, _, alpha, bravo = _seed(session)
    row = session.query(GroupMembership).filter_by(
        user_id=alice.id, tenant_id=bravo.id
    ).one()
    session.delete(row)
    session.flush()
    with pytest.raises(IdentityDenied):
        resolve(session, "alice-sub", "research")
    alpha.is_active = False
    session.flush()
    with pytest.raises(IdentityDenied):
        resolve(session, "alice-sub", "engineering")


def test_disabled_user_denied_even_with_membership(session):
    alice, *_ = _seed(session)
    alice.is_active = False
    session.flush()
    with pytest.raises(IdentityDenied):
        resolve(session, alice.subject, "engineering")


def test_initial_admin_bootstrap_exact_subject_one_time(session):
    provision_first_admin(session, "root-sub", "root-sub")
    user = get_user(session, "root-sub")
    assert user.is_system_admin
    assert resolve(session, "root-sub").group_id == "__system__"
    with pytest.raises(IdentityDenied):
        provision_first_admin(session, "another-sub", "another-sub")


def test_document_owner_set_from_trusted_context_not_caller_input(session):
    _, _, group, _ = _seed(session)
    ctx = RequestContext(
        user_id="alice-sub", group_id="engineering", roles=("user", "member")
    )
    repo = CatalogRepository(session, ctx)
    doc = repo.create_document(
        display_name="owned.pdf", content_checksum="c" * 64,
        created_by="attacker-sub",
    )
    assert doc.created_by == "alice-sub"
    assert doc.tenant_id == group.id
