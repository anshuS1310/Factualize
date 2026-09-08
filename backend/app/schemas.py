from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    NEEDS_ATTENTION = "needs_attention"
    TERMINAL_FAILURE = "terminal_failure"


class WorkUnitStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"
    SKIPPED = "skipped"


class PipelineStage(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    TRIAGING = "triaging"
    PARSING = "parsing"
    EXTRACTING_FACTS = "extracting_facts"
    RESOLVING_ENTITIES = "resolving_entities"
    COMPARING_FACTS = "comparing_facts"
    COMPLETED = "completed"


class RelationshipLabel(StrEnum):
    CORROBORATES = "corroborates"
    CONTRADICTS = "contradicts"
    RECONCILED = "reconciled"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_COMPARABLE = "not_comparable"


class HealthResponse(BaseModel):
    status: str
    gemini_configured: bool
    data_directory: str


class DocumentSummary(BaseModel):
    id: str
    original_filename: str
    sha256: str
    page_count: int | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class JobProgress(BaseModel):
    id: str
    document_id: str
    set_id: str | None = None
    status: JobStatus
    stage: PipelineStage
    progress_current: int = 0
    progress_total: int = 0
    progress_detail: str | None = None
    queue_position: int | None = None
    last_error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class UploadResponse(BaseModel):
    document: DocumentSummary
    job: JobProgress
    reused_existing_document: bool = False


class DocumentSetSummary(BaseModel):
    id: str
    name: str
    status: str
    document_count: int = 0
    created_at: datetime
    updated_at: datetime


class SetDocumentMember(BaseModel):
    document: DocumentSummary
    job: JobProgress | None = None
    position: int


class DocumentSetDetail(DocumentSetSummary):
    documents: list[SetDocumentMember] = Field(default_factory=list)


class SetUploadResponse(BaseModel):
    document_set: DocumentSetDetail
    reused_document_count: int = 0


class DeleteResponse(BaseModel):
    deleted: bool = True


class WorkUnitSummary(BaseModel):
    id: str
    unit_type: str
    unit_key: str
    status: WorkUnitStatus
    attempts: int
    max_attempts: int
    skip_reason: str | None = None
    progress_detail: str | None = None
    error: dict[str, Any] | None = None


class JobDetail(JobProgress):
    work_units: list[WorkUnitSummary] = Field(default_factory=list)


class PageSummary(BaseModel):
    page_number: int
    printed_page_number: str | None = None
    triage_result: str | None = None
    parser_used: str | None = None
    status: str


class DocumentDetail(DocumentSummary):
    pages: list[PageSummary] = Field(default_factory=list)


class FactSummary(BaseModel):
    id: str
    stable_id: str
    revision: int
    claim_text: str
    subject: str | None = None
    predicate: str | None = None
    object_value: str | None = None
    time_period: str | None = None
    confidence: float
    primary_entity_mention: str | None = None
    primary_entity_id: str | None = None
    document_id: str
    set_id: str | None = None
    is_current: bool
    correction_state: str
    cross_check_status: str = "no_comparable_source"
    cross_check_explanation: str = "No comparable source fact was found in this PDF set."
    cross_check_source_count: int = 1


class EvidenceAnchorResponse(BaseModel):
    id: str
    page_number: int
    source_kind: str
    quote: str
    bbox: list[float] | None = None
    confidence: float


class FactDetail(FactSummary):
    raw_value: str | None = None
    normalized_value: str | None = None
    unit_or_currency: str | None = None
    scope: str | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    additional_entity_mentions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceAnchorResponse] = Field(default_factory=list)


class CorrectionRequest(BaseModel):
    field_path: str = Field(min_length=1, max_length=100)
    corrected_value: str = Field(min_length=1, max_length=10_000)
    note: str | None = Field(default=None, max_length=2_000)


class RelationshipSummary(BaseModel):
    id: str
    stable_id: str
    revision: int
    left_fact_id: str
    right_fact_id: str
    label: RelationshipLabel
    explanation: str
    confidence: float
    stale: bool
    superseded: bool
    is_current: bool
    set_id: str | None = None


class HistoryEventResponse(BaseModel):
    id: str
    event_type: str
    related_record_id: str | None = None
    set_id: str | None = None
    snapshot: dict[str, Any]
    created_at: datetime
