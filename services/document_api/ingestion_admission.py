"""Transactional, server-counted admission. PostgreSQL is the source of truth.

The tenant row serializes admissions. Ledger, version and job commit together;
failed transactions charge nothing and retries of an admitted version are free.
"""
import datetime as dt
from fastapi import HTTPException
from sqlalchemy import select, update, func
from services.catalog.models import IngestionAdmission, Tenant


def admit(session, tenant_id, version_id, pages, limit, *, now=None):
    if pages < 1:
        raise HTTPException(400, 'document must have at least one page')
    now = now or dt.datetime.now(dt.timezone.utc)
    day = now.date()
    # UPDATE also serializes SQLite tests; PostgreSQL holds a row lock until commit.
    session.execute(update(Tenant).where(Tenant.id == tenant_id).values(name=Tenant.name))
    if session.get(IngestionAdmission, version_id) is not None:
        return
    used = session.scalar(select(func.coalesce(func.sum(IngestionAdmission.pages), 0)).where(
        IngestionAdmission.tenant_id == tenant_id, IngestionAdmission.quota_day == day))
    if used + pages > limit:
        midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), dt.timezone.utc)
        raise HTTPException(429, 'daily ingestion page quota exhausted',
                            headers={'Retry-After': str(max(1, int((midnight-now).total_seconds())))})
    session.add(IngestionAdmission(version_id=version_id, tenant_id=tenant_id,
                                   quota_day=day, pages=pages))
    session.flush()
