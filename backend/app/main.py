from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .database import Database, parse_json
from .entities import EntityResolutionService
from .facts import FactExtractionService
from .gemini import GeminiGateway
from .ingestion import PdfIngestionService
from .jobs import LocalJobRunner
from .relationships import RelationshipService
from .schemas import (
    CorrectionRequest,
    DeleteResponse,
    DocumentSetDetail,
    DocumentSetSummary,
    DocumentDetail,
    DocumentSummary,
    EvidenceAnchorResponse,
    FactDetail,
    FactSummary,
    HealthResponse,
    HistoryEventResponse,
    JobDetail,
    JobProgress,
    PageSummary,
    RelationshipSummary,
    RelationshipCorrectionRequest,
    SetDocumentMember,
    SetUploadResponse,
    UploadResponse,
    WorkUnitSummary,
)
from .settings import Settings, get_settings
from .storage import LocalDocumentStorage


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def document_response(row: dict[str, Any]) -> DocumentSummary:
    return DocumentSummary(
        id=row["id"],
        original_filename=row["original_filename"],
        sha256=row["sha256"],
        page_count=row["page_count"],
        status=row["status"],
        created_at=_as_datetime(row["created_at"]),
        updated_at=_as_datetime(row["updated_at"]),
    )


def job_response(database: Database, row: dict[str, Any]) -> JobProgress:
    return JobProgress(
        id=row["id"],
        document_id=row["document_id"],
        set_id=row.get("set_id"),
        status=row["status"],
        stage=row["stage"],
        progress_current=row["progress_current"],
        progress_total=row["progress_total"],
        progress_detail=row["progress_detail"],
        queue_position=database.queue_position(row["id"]),
        last_error=parse_json(row["last_error_json"], None),
        created_at=_as_datetime(row["created_at"]),
        updated_at=_as_datetime(row["updated_at"]),
    )


def fact_response(
    database: Database,
    row: dict[str, Any],
    *,
    detail: bool = False,
    cross_check: dict[str, Any] | None = None,
) -> FactSummary | FactDetail:
    summary = FactSummary(
        id=row["id"],
        stable_id=row["stable_id"],
        revision=row["revision"],
        claim_text=row["claim_text"],
        subject=row["subject"],
        predicate=row["predicate"],
        object_value=row["object_value"],
        time_period=row["time_period"],
        confidence=row["confidence"],
        primary_entity_mention=row["primary_entity_mention"],
        primary_entity_id=row["primary_entity_id"],
        document_id=row["document_id"],
        set_id=row.get("set_id"),
        is_current=bool(row["is_current"]),
        correction_state=row["correction_state"],
        cross_check_status=(cross_check or {}).get("cross_check_status", "no_comparable_source"),
        cross_check_explanation=(cross_check or {}).get(
            "cross_check_explanation", "No comparable source fact was found in this PDF set."
        ),
        cross_check_source_count=int((cross_check or {}).get("cross_check_source_count", 1)),
    )
    if not detail:
        return summary
    evidence = [
        EvidenceAnchorResponse(
            id=anchor["id"],
            page_number=anchor["page_number"],
            source_kind=anchor["source_kind"],
            quote=anchor["quote"],
            bbox=parse_json(anchor["bbox_json"], None),
            confidence=anchor["confidence"],
        )
        for anchor in database.fact_evidence(row["id"])
    ]
    return FactDetail(
        **summary.model_dump(),
        raw_value=row["raw_value"],
        normalized_value=row["normalized_value"],
        unit_or_currency=row["unit_or_currency"],
        scope=row["scope"],
        qualifiers=parse_json(row["qualifiers_json"], {}),
        additional_entity_mentions=parse_json(row["additional_entity_mentions_json"], []),
        evidence=evidence,
    )


def relationship_response(row: dict[str, Any]) -> RelationshipSummary:
    return RelationshipSummary(
        id=row["id"],
        stable_id=row["stable_id"],
        revision=row["revision"],
        left_fact_id=row["left_fact_id"],
        right_fact_id=row["right_fact_id"],
        label=row["label"],
        explanation=row["explanation"],
        confidence=row["confidence"],
        stale=bool(row["stale"]),
        superseded=bool(row["superseded"]),
        is_current=bool(row["is_current"]),
        set_id=row.get("set_id"),
    )


def document_set_response(row: dict[str, Any]) -> DocumentSetSummary:
    return DocumentSetSummary(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        document_count=int(row.get("document_count", 0)),
        created_at=_as_datetime(row["created_at"]),
        updated_at=_as_datetime(row["updated_at"]),
    )


def document_set_detail_response(database: Database, document_set: dict[str, Any]) -> DocumentSetDetail:
    members: list[SetDocumentMember] = []
    for row in database.list_set_documents(document_set["id"]):
        job = None
        if row.get("job_id"):
            job = job_response(
                database,
                {
                    "id": row["job_id"], "document_id": row["id"], "set_id": document_set["id"],
                    "status": row["job_status"], "stage": row["job_stage"],
                    "progress_current": row["progress_current"], "progress_total": row["progress_total"],
                    "progress_detail": row["progress_detail"], "last_error_json": row["last_error_json"],
                    "created_at": row["job_created_at"], "updated_at": row["job_updated_at"],
                },
            )
        members.append(SetDocumentMember(document=document_response(row), job=job, position=int(row["position"])))
    summary = document_set_response(document_set).model_dump()
    summary["document_count"] = len(members)
    return DocumentSetDetail(**summary, documents=members)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()
    storage = LocalDocumentStorage(settings)
    gateway = GeminiGateway(settings, database)
    fact_extraction = FactExtractionService(settings, database, gateway)
    entity_resolution = EntityResolutionService(database)
    relationship_service = RelationshipService(settings, database, gateway)
    ingestion = PdfIngestionService(
        settings,
        database,
        storage,
        fact_extraction,
        entity_resolution,
        relationship_service,
    )
    runner = LocalJobRunner(database, ingestion)
    app.state.settings = settings
    app.state.database = database
    app.state.storage = storage
    app.state.runner = runner
    app.state.relationship_service = relationship_service
    await runner.start()
    try:
        yield
    finally:
        await runner.stop()


app = FastAPI(
    title="Factualize API",
    version="0.1.0",
    description="Evidence-backed PDF fact knowledge layer.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def get_database(request: Request) -> Database:
    return request.app.state.database


@app.get("/api/v1/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        gemini_configured=settings.gemini_is_configured,
        data_directory=str(settings.data_dir),
    )


@app.post("/api/v1/documents", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(request: Request, file: UploadFile = File(...)) -> UploadResponse:
    database = get_database(request)
    storage: LocalDocumentStorage = request.app.state.storage
    upload = await storage.save_pdf(file)
    document, job, reused = database.create_or_reuse_document(
        original_filename=upload.original_filename,
        sha256=upload.sha256,
        stored_path=str(upload.path),
        byte_size=upload.byte_size,
    )
    if not reused:
        request.app.state.runner.notify()
    return UploadResponse(
        document=document_response(document),
        job=job_response(database, job),
        reused_existing_document=reused,
    )


@app.post("/api/v1/sets", response_model=SetUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document_set(
    request: Request,
    files: list[UploadFile] = File(...),
    name: str = Form(default=""),
) -> SetUploadResponse:
    """Create a set and queue every supplied PDF under that single scope."""
    if not files:
        raise HTTPException(status_code=422, detail="Choose at least one PDF for the set.")
    if len(files) > 25:
        raise HTTPException(status_code=422, detail="A PDF set may contain at most 25 files.")
    storage: LocalDocumentStorage = request.app.state.storage
    uploads: list[dict[str, Any]] = []
    for file in files:
        saved = await storage.save_pdf(file)
        uploads.append(
            {
                "original_filename": saved.original_filename,
                "sha256": saved.sha256,
                "stored_path": str(saved.path),
                "byte_size": saved.byte_size,
            }
        )
    database = get_database(request)
    document_set, members = database.create_document_set(name=name, uploads=uploads)
    request.app.state.runner.notify()
    return SetUploadResponse(
        document_set=document_set_detail_response(database, document_set),
        reused_document_count=sum(1 for _, _, reused in members if reused),
    )


@app.get("/api/v1/sets", response_model=list[DocumentSetSummary])
def list_document_sets(request: Request) -> list[DocumentSetSummary]:
    return [document_set_response(row) for row in get_database(request).list_document_sets()]


@app.get("/api/v1/sets/{set_id}", response_model=DocumentSetDetail)
def get_document_set(request: Request, set_id: str) -> DocumentSetDetail:
    database = get_database(request)
    document_set = database.get_document_set(set_id)
    if not document_set:
        raise HTTPException(status_code=404, detail="PDF set not found.")
    database.refresh_set_status(set_id)
    return document_set_detail_response(database, database.get_document_set(set_id) or document_set)


@app.post("/api/v1/sets/{set_id}/retry", response_model=DocumentSetDetail)
def retry_document_set(request: Request, set_id: str) -> DocumentSetDetail:
    database = get_database(request)
    if not database.retry_set_failed_work(set_id):
        raise HTTPException(status_code=404, detail="PDF set not found.")
    request.app.state.runner.notify()
    document_set = database.get_document_set(set_id)
    if not document_set:
        raise HTTPException(status_code=404, detail="PDF set not found.")
    return document_set_detail_response(database, document_set)


@app.post("/api/v1/sets/{set_id}/documents/{document_id}/stop", response_model=DocumentSetDetail)
def stop_document_processing(request: Request, set_id: str, document_id: str) -> DocumentSetDetail:
    database = get_database(request)
    if not database.stop_set_document_job(set_id=set_id, document_id=document_id):
        raise HTTPException(status_code=404, detail="PDF job not found in this set.")
    document_set = database.get_document_set(set_id)
    if not document_set:
        raise HTTPException(status_code=404, detail="PDF set not found.")
    return document_set_detail_response(database, document_set)


@app.delete("/api/v1/sets/{set_id}/documents/{document_id}", response_model=DeleteResponse)
def remove_document_from_set(request: Request, set_id: str, document_id: str) -> DeleteResponse:
    database = get_database(request)
    members = database.list_set_documents(set_id)
    member = next((item for item in members if item["id"] == document_id), None)
    if member and member.get("job_id") and request.app.state.runner.is_processing(member["job_id"]):
        raise HTTPException(status_code=409, detail="The active processing step is stopping. Try removal again shortly.")
    try:
        orphaned = database.remove_document_from_set(set_id=set_id, document_id=document_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if orphaned is None:
        raise HTTPException(status_code=404, detail="PDF not found in this set.")
    if orphaned:
        request.app.state.storage.delete_orphaned_document(orphaned)
    return DeleteResponse()


@app.delete("/api/v1/sets/{set_id}", response_model=DeleteResponse)
def delete_document_set(request: Request, set_id: str) -> DeleteResponse:
    database = get_database(request)
    members = database.list_set_documents(set_id)
    if any(member.get("job_id") and request.app.state.runner.is_processing(member["job_id"]) for member in members):
        raise HTTPException(status_code=409, detail="The active processing step is stopping. Try deleting this set again shortly.")
    try:
        orphaned_documents = database.delete_document_set(set_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if orphaned_documents is None:
        raise HTTPException(status_code=404, detail="PDF set not found.")
    for document in orphaned_documents:
        request.app.state.storage.delete_orphaned_document(document)
    return DeleteResponse()


@app.get("/api/v1/documents", response_model=list[DocumentSummary])
def list_documents(request: Request) -> list[DocumentSummary]:
    database = get_database(request)
    return [document_response(row) for row in database.list_documents()]


@app.get("/api/v1/documents/{document_id}", response_model=DocumentDetail)
def get_document(request: Request, document_id: str) -> DocumentDetail:
    database = get_database(request)
    document = database.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")
    pages = [
        PageSummary(
            page_number=page["page_number"],
            printed_page_number=page["printed_page_number"],
            triage_result=page["triage_result"],
            parser_used=page["parser_used"],
            status=page["status"],
        )
        for page in database.list_pages(document_id)
    ]
    return DocumentDetail(**document_response(document).model_dump(), pages=pages)


@app.get("/api/v1/documents/{document_id}/file")
def get_document_file(request: Request, document_id: str) -> FileResponse:
    document = get_database(request).get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")
    path = Path(document["stored_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file is unavailable.")
    return FileResponse(path, media_type="application/pdf", filename=document["original_filename"])


@app.get("/api/v1/documents/{document_id}/pages/{page_number}/render")
def get_page_render(request: Request, document_id: str, page_number: int) -> FileResponse:
    database = get_database(request)
    document = database.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")
    render = request.app.state.storage.page_render_path(document["sha256"], page_number)
    if not render.is_file():
        raise HTTPException(status_code=404, detail="Page render is not available yet.")
    return FileResponse(render, media_type="image/png")


@app.get("/api/v1/jobs/{job_id}", response_model=JobDetail)
def get_job(request: Request, job_id: str) -> JobDetail:
    database = get_database(request)
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    units = [
        WorkUnitSummary(
            id=unit["id"],
            unit_type=unit["unit_type"],
            unit_key=unit["unit_key"],
            status=unit["status"],
            attempts=unit["attempts"],
            max_attempts=unit["max_attempts"],
            skip_reason=unit["skip_reason"],
            progress_detail=unit["progress_detail"],
            error=parse_json(unit["error_json"], None),
        )
        for unit in database.list_work_units(job_id)
    ]
    return JobDetail(**job_response(database, job).model_dump(), work_units=units)


@app.get("/api/v1/jobs", response_model=list[JobProgress])
def list_jobs(request: Request) -> list[JobProgress]:
    database = get_database(request)
    return [job_response(database, job) for job in database.list_latest_jobs()]


@app.get("/api/v1/facts", response_model=list[FactSummary])
def list_facts(
    request: Request, document_id: str | None = None, set_id: str | None = None
) -> list[FactSummary]:
    database = get_database(request)
    cross_checks = database.fact_cross_checks(set_id) if set_id else {}
    return [
        fact_response(database, row, cross_check=cross_checks.get(row["id"]))
        for row in database.list_facts(document_id=document_id, set_id=set_id)
    ]


@app.get("/api/v1/facts/{fact_id}", response_model=FactDetail)
def get_fact(request: Request, fact_id: str) -> FactDetail:
    database = get_database(request)
    fact = database.get_fact(fact_id)
    if not fact:
        raise HTTPException(status_code=404, detail="Fact not found.")
    return fact_response(database, fact, detail=True)


@app.post("/api/v1/facts/{fact_id}/corrections", response_model=FactDetail)
def correct_fact(request: Request, fact_id: str, correction: CorrectionRequest) -> FactDetail:
    database = get_database(request)
    try:
        corrected = database.correct_fact(
            fact_id=fact_id,
            field_path=correction.field_path,
            corrected_value=correction.corrected_value,
            note=correction.note,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Current fact not found.") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return fact_response(database, corrected, detail=True)


@app.get("/api/v1/relationships", response_model=list[RelationshipSummary])
def list_relationships(
    request: Request,
    set_id: str | None = None,
    label: str | None = None,
    entity_id: str | None = None,
    stale: bool | None = None,
) -> list[RelationshipSummary]:
    database = get_database(request)
    return [
        relationship_response(row)
        for row in database.list_relationships(
            set_id=set_id, label=label, entity_id=entity_id, stale=stale
        )
    ]


@app.get("/api/v1/demonstration/{label}", response_model=RelationshipSummary)
def demonstration_case(request: Request, label: str, set_id: str) -> RelationshipSummary:
    relationships = get_database(request).list_relationships(label=label, stale=False, set_id=set_id)
    if not relationships:
        raise HTTPException(
            status_code=404,
            detail="No real stored example exists for this category yet; none has been manufactured.",
        )
    return relationship_response(relationships[0])


@app.get("/api/v1/relationships/{relationship_id}")
def relationship_detail(request: Request, relationship_id: str):
    database = get_database(request)
    with database.connection() as conn:
        row = conn.execute("SELECT * FROM relationships WHERE id=?", (relationship_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Comparison not found.")
        revisions = conn.execute("SELECT * FROM relationships WHERE stable_id=? ORDER BY revision DESC", (row["stable_id"],)).fetchall()
    return {"relationship": relationship_response(dict(row)), "left": fact_response(database, database.get_fact(row["left_fact_id"]), detail=True), "right": fact_response(database, database.get_fact(row["right_fact_id"]), detail=True), "revisions": [relationship_response(dict(item)) for item in revisions], "reasoning_trace": parse_json(row["reasoning_trace_json"], {})}


@app.post("/api/v1/relationships/{relationship_id}/recheck", response_model=RelationshipSummary)
def recheck_relationship(request: Request, relationship_id: str):
    return revise_relationship(request, relationship_id)


@app.post("/api/v1/relationships/{relationship_id}/corrections", response_model=RelationshipSummary)
def correct_relationship(request: Request, relationship_id: str, correction: RelationshipCorrectionRequest):
    return revise_relationship(request, relationship_id, label=correction.label.value, note=correction.note)


def revise_relationship(request: Request, relationship_id: str, **kwargs):
    try:
        return relationship_response(request.app.state.relationship_service.revise(relationship_id, **kwargs))
    except KeyError as error:
        raise HTTPException(404, "Current comparison not found.") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/v1/history", response_model=list[HistoryEventResponse])
def list_history(
    request: Request, limit: int = 100, set_id: str | None = None
) -> list[HistoryEventResponse]:
    safe_limit = min(max(limit, 1), 500)
    return [
        HistoryEventResponse(
            id=row["id"],
            event_type=row["event_type"],
            related_record_id=row["related_record_id"],
            set_id=row.get("set_id"),
            snapshot=parse_json(row["snapshot_json"], {}),
            created_at=_as_datetime(row["created_at"]),
        )
        for row in get_database(request).list_history_events(safe_limit, set_id=set_id)
    ]
