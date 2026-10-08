"""Pydantic request/response schemas for the document API (§11)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    document_id: str
    job_id: str
    status: str
    page_count: int
    checksum_sha256: str
    display_name: str


class DocumentSummary(BaseModel):
    document_id: str
    display_name: str
    checksum_sha256: str
    active_version_id: str | None
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    documents: list[DocumentSummary]
    limit: int
    offset: int


class VersionInfo(BaseModel):
    version_id: str
    version_number: int
    status: str
    parser: str
    parser_revision: str
    page_count: int | None
    chunk_count_actual: int | None
    is_active: bool
    created_at: datetime


class ArtifactReference(BaseModel):
    key: str
    download_url: str | None = None


class DocumentDetail(BaseModel):
    document_id: str
    display_name: str
    checksum_sha256: str
    active_version_id: str | None
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
    versions: list[VersionInfo]
    artifacts: dict[str, ArtifactReference] = Field(default_factory=dict)


class JobStatusResponse(BaseModel):
    job_id: str
    job_type: str
    state: str
    document_id: str | None
    document_version_id: str | None
    attempt_count: int
    error: str | None
    created_at: datetime
    updated_at: datetime


class DeleteResponse(BaseModel):
    document_id: str
    job_id: str
    status: str


class ReviewItemSummary(BaseModel):
    item_id: str
    document_id: str
    document_version_id: str
    page_index: int
    status: str
    created_at: datetime


class ReviewItemListResponse(BaseModel):
    items: list[ReviewItemSummary]
    limit: int
    offset: int


class ReviewItemDetail(BaseModel):
    item_id: str
    document_id: str
    document_version_id: str
    page_index: int
    status: str
    page_render_key: str
    page_render_url: str | None = None
    parse_attempts: dict
    routing_diagnostics: dict
    reviewer_decision: dict | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


ReviewDecision = Literal["accepted", "re-routed", "rejected"]


class ResolveReviewRequest(BaseModel):
    decision: ReviewDecision
    detail: dict = Field(default_factory=dict)


class ResolveReviewResponse(BaseModel):
    item_id: str
    status: str
    reviewed_by: str
