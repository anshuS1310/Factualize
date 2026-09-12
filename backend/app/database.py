from __future__ import annotations

import json
import sqlite3
import uuid
from hashlib import sha256
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .schemas import JobStatus, PipelineStage, WorkUnitStatus


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


class Database:
    """Small SQLite repository with explicit transactions and no ORM magic."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE,
                    stored_path TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    page_count INTEGER,
                    status TEXT NOT NULL DEFAULT 'uploaded',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_sets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS set_documents (
                    set_id TEXT NOT NULL REFERENCES document_sets(id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(set_id, document_id)
                );

                CREATE TABLE IF NOT EXISTS pages (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    page_number INTEGER NOT NULL,
                    printed_page_number TEXT,
                    triage_result TEXT,
                    parser_used TEXT,
                    cache_path TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(document_id, page_number)
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    progress_detail TEXT,
                    last_error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_units (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    unit_type TEXT NOT NULL,
                    unit_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    skip_reason TEXT,
                    progress_detail TEXT,
                    cache_path TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    entity_type TEXT,
                    resolution_revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY,
                    stable_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    claim_text TEXT NOT NULL,
                    subject TEXT,
                    predicate TEXT,
                    predicate_key TEXT,
                    object_value TEXT,
                    raw_value TEXT,
                    normalized_value TEXT,
                    unit_or_currency TEXT,
                    time_period TEXT,
                    period_start TEXT,
                    period_end TEXT,
                    scope TEXT,
                    qualifiers_json TEXT NOT NULL DEFAULT '{}',
                    primary_entity_mention TEXT,
                    primary_entity_id TEXT REFERENCES entities(id),
                    additional_entity_mentions_json TEXT NOT NULL DEFAULT '[]',
                    embedding_json TEXT,
                    confidence REAL NOT NULL,
                    extraction_source TEXT NOT NULL,
                    batch_key TEXT,
                    correction_state TEXT NOT NULL DEFAULT 'original',
                    is_current INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(stable_id, revision)
                );

                CREATE TABLE IF NOT EXISTS evidence_anchors (
                    id TEXT PRIMARY KEY,
                    fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                    page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                    source_kind TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    start_offset INTEGER,
                    end_offset INTEGER,
                    bbox_json TEXT,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relationships (
                    id TEXT PRIMARY KEY,
                    stable_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    left_fact_id TEXT NOT NULL REFERENCES facts(id),
                    right_fact_id TEXT NOT NULL REFERENCES facts(id),
                    label TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    deterministic_context_json TEXT NOT NULL DEFAULT '{}',
                    reasoning_trace_json TEXT NOT NULL DEFAULT '{}',
                    entity_resolution_revision INTEGER NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    superseded INTEGER NOT NULL DEFAULT 0,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(stable_id, revision)
                );

                CREATE TABLE IF NOT EXISTS corrections (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    field_path TEXT NOT NULL,
                    previous_value TEXT NOT NULL,
                    corrected_value TEXT NOT NULL,
                    note TEXT,
                    exact_fingerprint TEXT NOT NULL,
                    semantic_context TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history_events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    related_record_id TEXT,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pages_document ON pages(document_id, page_number);
                CREATE INDEX IF NOT EXISTS idx_set_documents_set ON set_documents(set_id, position);
                CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_work_units_job ON work_units(job_id, status);
                CREATE INDEX IF NOT EXISTS idx_facts_query
                    ON facts(primary_entity_id, predicate_key, period_start, period_end, is_current);
                CREATE INDEX IF NOT EXISTS idx_relationships_label
                    ON relationships(label, is_current, stale);
                CREATE INDEX IF NOT EXISTS idx_relationships_left ON relationships(left_fact_id);
                CREATE INDEX IF NOT EXISTS idx_relationships_right ON relationships(right_fact_id);
                CREATE INDEX IF NOT EXISTS idx_evidence_fact ON evidence_anchors(fact_id, page_id);
                """
            )
            self._ensure_column(conn, "facts", "embedding_json", "TEXT")
            self._ensure_column(conn, "jobs", "set_id", "TEXT")
            self._ensure_column(conn, "facts", "set_id", "TEXT")
            self._ensure_column(conn, "entities", "set_id", "TEXT")
            self._ensure_column(conn, "relationships", "set_id", "TEXT")
            self._ensure_column(conn, "history_events", "set_id", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_correction_memory ON corrections(target_type, exact_fingerprint)")
            conn.execute("CREATE TABLE IF NOT EXISTS provider_usage (day TEXT PRIMARY KEY, requests INTEGER NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS provider_pause (id INTEGER PRIMARY KEY, until_epoch REAL NOT NULL)")
            # Upgrade existing fact correction records without altering their audit values.
            from .correction_memory import fact_context, fingerprint
            for correction in conn.execute("SELECT * FROM corrections WHERE target_type='fact' AND (semantic_context IS NULL OR semantic_context NOT LIKE '{%')").fetchall():
                previous = conn.execute("SELECT old.* FROM facts new JOIN facts old ON old.stable_id=new.stable_id AND old.revision=new.revision-1 WHERE new.id=?", (correction["target_id"],)).fetchone()
                if previous:
                    payload = dict(previous)
                    quote = conn.execute("SELECT quote FROM evidence_anchors WHERE fact_id=? LIMIT 1", (previous["id"],)).fetchone()
                    payload["evidence_quote"] = quote[0] if quote else ""
                    context = fact_context(payload)
                    conn.execute("UPDATE corrections SET exact_fingerprint=?,semantic_context=? WHERE id=?", (fingerprint(context),json.dumps(context),correction["id"]))
            conn.execute("""UPDATE history_events SET set_id=(SELECT f.set_id FROM corrections c JOIN facts f ON f.id=c.target_id WHERE c.id=history_events.related_record_id)
                WHERE set_id IS NULL AND event_type='fact_corrected'""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_set ON jobs(set_id, status, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_set ON facts(set_id, is_current)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_relationships_set ON relationships(set_id, is_current)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_set ON history_events(set_id, created_at)")
            self._migrate_legacy_documents(conn)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _migrate_legacy_documents(conn: sqlite3.Connection) -> None:
        """Put pre-set uploads into one visible legacy set without deleting their history."""
        legacy_docs = conn.execute(
            """
            SELECT d.id FROM documents d
            LEFT JOIN set_documents membership ON membership.document_id = d.id
            WHERE membership.document_id IS NULL
            """
        ).fetchall()
        if not legacy_docs:
            return
        legacy = conn.execute(
            "SELECT id FROM document_sets WHERE name = 'Legacy ungrouped uploads' LIMIT 1"
        ).fetchone()
        now = utc_now()
        legacy_id = legacy["id"] if legacy else str(uuid.uuid4())
        if not legacy:
            conn.execute(
                """
                INSERT INTO document_sets (id, name, status, created_at, updated_at)
                VALUES (?, 'Legacy ungrouped uploads', 'completed_with_issues', ?, ?)
                """,
                (legacy_id, now, now),
            )
        for position, row in enumerate(legacy_docs, start=1):
            conn.execute(
                """
                INSERT OR IGNORE INTO set_documents (set_id, document_id, position, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (legacy_id, row["id"], position, now),
            )
            conn.execute("UPDATE jobs SET set_id = ? WHERE document_id = ? AND set_id IS NULL", (legacy_id, row["id"]))
            conn.execute("UPDATE facts SET set_id = ? WHERE document_id = ? AND set_id IS NULL", (legacy_id, row["id"]))
        conn.execute("UPDATE entities SET set_id = ? WHERE set_id IS NULL", (legacy_id,))
        conn.execute("UPDATE relationships SET set_id = ? WHERE set_id IS NULL", (legacy_id,))

    def recover_incomplete_work(self) -> None:
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE work_units SET status = ?, updated_at = ? WHERE status = ?",
                (WorkUnitStatus.QUEUED.value, now, WorkUnitStatus.RUNNING.value),
            )
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, updated_at = ? WHERE status = ?",
                (JobStatus.QUEUED.value, PipelineStage.QUEUED.value, now, JobStatus.RUNNING.value),
            )
            conn.commit()

    def create_or_reuse_document(
        self, *, original_filename: str, sha256: str, stored_path: str, byte_size: int
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        now = utc_now()
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
            ).fetchone()
            if existing:
                job = conn.execute(
                    "SELECT * FROM jobs WHERE document_id = ? ORDER BY created_at DESC LIMIT 1",
                    (existing["id"],),
                ).fetchone()
                if job is None:
                    raise RuntimeError("A cached document has no job record")
                return dict(existing), dict(job), True

            document_id = str(uuid.uuid4())
            job_id = str(uuid.uuid4())
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO documents (
                    id, original_filename, sha256, stored_path, byte_size, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'uploaded', ?, ?)
                """,
                (document_id, original_filename, sha256, stored_path, byte_size, now, now),
            )
            conn.execute(
                """
                INSERT INTO jobs (id, document_id, status, stage, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    document_id,
                    JobStatus.QUEUED.value,
                    PipelineStage.QUEUED.value,
                    now,
                    now,
                ),
            )
            document = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            self._append_history_in_connection(
                conn,
                "document_uploaded",
                document_id,
                {"document_id": document_id, "sha256": sha256, "filename": original_filename},
            )
            conn.commit()
            return dict(document), dict(job), False

    def create_document_set(
        self, *, name: str, uploads: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any], bool]]]:
        """Create one scoped processing set and one queued job per uploaded source PDF."""
        clean_name = " ".join(name.split()) or "Untitled PDF set"
        now = utc_now()
        set_id = str(uuid.uuid4())
        members: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
        seen_hashes: set[str] = set()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO document_sets (id, name, status, created_at, updated_at)
                VALUES (?, ?, 'queued', ?, ?)
                """,
                (set_id, clean_name, now, now),
            )
            for position, upload in enumerate(uploads, start=1):
                if upload["sha256"] in seen_hashes:
                    continue
                seen_hashes.add(upload["sha256"])
                existing = conn.execute(
                    "SELECT * FROM documents WHERE sha256 = ?", (upload["sha256"],)
                ).fetchone()
                reused = existing is not None
                if existing:
                    document = dict(existing)
                else:
                    document_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO documents (
                            id, original_filename, sha256, stored_path, byte_size, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'uploaded', ?, ?)
                        """,
                        (
                            document_id,
                            upload["original_filename"],
                            upload["sha256"],
                            upload["stored_path"],
                            upload["byte_size"],
                            now,
                            now,
                        ),
                    )
                    document = dict(
                        conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
                    )
                conn.execute(
                    """
                    INSERT INTO set_documents (set_id, document_id, position, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (set_id, document["id"], position, now),
                )
                job_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO jobs (id, document_id, set_id, status, stage, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        document["id"],
                        set_id,
                        JobStatus.QUEUED.value,
                        PipelineStage.QUEUED.value,
                        now,
                        now,
                    ),
                )
                job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
                members.append((document, job, reused))
            set_row = dict(conn.execute("SELECT * FROM document_sets WHERE id = ?", (set_id,)).fetchone())
            self._append_history_in_connection(
                conn,
                "set_created",
                set_id,
                {"set_id": set_id, "name": clean_name, "document_count": len(members)},
                set_id=set_id,
            )
            conn.commit()
        return set_row, members

    def next_queued_job(self) -> dict[str, Any] | None:
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if not job:
                conn.commit()
                return None
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, updated_at = ? WHERE id = ?",
                (JobStatus.RUNNING.value, PipelineStage.PREPARING.value, now, job["id"]),
            )
            conn.execute(
                "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
                ("processing", now, job["document_id"]),
            )
            if job["set_id"]:
                conn.execute(
                    "UPDATE document_sets SET status = 'processing', updated_at = ? WHERE id = ?",
                    (now, job["set_id"]),
                )
            started = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            self._append_history_in_connection(
                conn,
                "job_started",
                job["id"],
                {"job_id": job["id"], "document_id": job["document_id"]},
                set_id=job["set_id"],
            )
            conn.commit()
            return dict(started)

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        stage: PipelineStage | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
        progress_detail: str | None = None,
        last_error: dict[str, Any] | None = None,
    ) -> None:
        updates: list[str] = ["updated_at = ?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            updates.append("status = ?")
            values.append(status.value)
        if stage is not None:
            updates.append("stage = ?")
            values.append(stage.value)
        if progress_current is not None:
            updates.append("progress_current = ?")
            values.append(progress_current)
        if progress_total is not None:
            updates.append("progress_total = ?")
            values.append(progress_total)
        if progress_detail is not None:
            updates.append("progress_detail = ?")
            values.append(progress_detail)
        if last_error is not None:
            updates.append("last_error_json = ?")
            values.append(json.dumps(last_error))
        values.append(job_id)
        with self.connection() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", values)

    def complete_job(self, job_id: str, *, with_issues: bool = False, detail: str | None = None) -> None:
        job = self.get_job(job_id)
        if not job:
            return
        status = JobStatus.COMPLETED_WITH_ISSUES if with_issues else JobStatus.COMPLETED
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE jobs SET status = ?, stage = ?, progress_detail = ?, updated_at = ? WHERE id = ?
                """,
                (status.value, PipelineStage.COMPLETED.value, detail, now, job_id),
            )
            conn.execute(
                "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, job["document_id"]),
            )
            self._append_history_in_connection(
                conn,
                "job_completed_with_issues" if with_issues else "job_completed",
                job_id,
                {"job_id": job_id, "document_id": job["document_id"], "detail": detail},
                set_id=job["set_id"],
            )
            conn.commit()
        if job.get("set_id"):
            self.refresh_set_status(job["set_id"])

    def create_or_update_page(
        self,
        *,
        document_id: str,
        page_number: int,
        triage_result: str | None = None,
        parser_used: str | None = None,
        cache_path: str | None = None,
        status: str = "pending",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO pages (
                    id, document_id, page_number, triage_result, parser_used, cache_path, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, page_number) DO UPDATE SET
                    triage_result = excluded.triage_result,
                    parser_used = excluded.parser_used,
                    cache_path = excluded.cache_path,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    document_id,
                    page_number,
                    triage_result,
                    parser_used,
                    cache_path,
                    status,
                    now,
                    now,
                ),
            )
            page = conn.execute(
                "SELECT * FROM pages WHERE document_id = ? AND page_number = ?",
                (document_id, page_number),
            ).fetchone()
            return dict(page)

    def set_document_page_count(self, document_id: str, page_count: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE documents SET page_count = ?, updated_at = ? WHERE id = ?",
                (page_count, utc_now(), document_id),
            )

    def create_work_unit(
        self,
        *,
        job_id: str,
        document_id: str,
        unit_type: str,
        unit_key: str,
        max_attempts: int,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO work_units (
                    id, job_id, document_id, unit_type, unit_key, status, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unit_key) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    document_id,
                    unit_type,
                    unit_key,
                    WorkUnitStatus.QUEUED.value,
                    max_attempts,
                    now,
                    now,
                ),
            )
            unit = conn.execute("SELECT * FROM work_units WHERE unit_key = ?", (unit_key,)).fetchone()
            return dict(unit)

    def update_work_unit(
        self,
        unit_id: str,
        *,
        status: WorkUnitStatus,
        progress_detail: str | None = None,
        cache_path: str | None = None,
        skip_reason: str | None = None,
        error: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> None:
        updates = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status.value, utc_now()]
        if progress_detail is not None:
            updates.append("progress_detail = ?")
            values.append(progress_detail)
        if cache_path is not None:
            updates.append("cache_path = ?")
            values.append(cache_path)
        if skip_reason is not None:
            updates.append("skip_reason = ?")
            values.append(skip_reason)
        if error is not None:
            updates.append("error_json = ?")
            values.append(json.dumps(error))
        if increment_attempts:
            updates.append("attempts = attempts + 1")
        values.append(unit_id)
        with self.connection() as conn:
            conn.execute(f"UPDATE work_units SET {', '.join(updates)} WHERE id = ?", values)

    def persist_fact_batch(
        self,
        *,
        unit_id: str,
        document_id: str,
        set_id: str,
        batch_key: str,
        facts: list[dict[str, Any]],
        page_ids: dict[int, str],
        cache_path: str,
    ) -> int:
        """Atomically save a validated batch and mark its work unit complete."""
        now = utc_now()
        inserted = 0
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            completed = conn.execute("SELECT status FROM work_units WHERE id=?", (unit_id,)).fetchone()
            if completed and completed["status"] == WorkUnitStatus.COMPLETED.value:
                conn.rollback()
                return 0
            for index, fact in enumerate(facts):
                page_number = fact["evidence_page"]
                page_id = page_ids.get(page_number)
                if not page_id:
                    continue
                quote = fact.get("evidence_quote", "").strip()
                if not quote:
                    continue
                stable_material = "|".join(
                    [
                        set_id,
                        document_id,
                        str(page_number),
                        fact["claim_text"],
                        fact.get("predicate") or "",
                        str(index),
                    ]
                )
                stable_id = sha256(stable_material.encode("utf-8")).hexdigest()[:32]
                fact_id = str(uuid.uuid4())
                predicate = fact.get("predicate")
                predicate_key = " ".join((predicate or "").lower().split()) or None
                conn.execute(
                    """
                    INSERT INTO facts (
                        id, stable_id, revision, document_id, set_id, claim_text, subject, predicate, predicate_key,
                        object_value, raw_value, normalized_value, unit_or_currency, time_period, scope,
                        qualifiers_json, primary_entity_mention, additional_entity_mentions_json, confidence,
                        extraction_source, batch_key, correction_state, is_current, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'gemini', ?, 'original', 1, ?)
                    """,
                    (
                        fact_id,
                        stable_id,
                        document_id,
                        set_id,
                        fact["claim_text"],
                        fact.get("subject"),
                        predicate,
                        predicate_key,
                        fact.get("object_or_value"),
                        fact.get("raw_value"),
                        fact.get("normalized_value"),
                        fact.get("unit_or_currency"),
                        fact.get("time_period"),
                        fact.get("scope"),
                        json.dumps(fact.get("qualifiers", {})),
                        fact.get("primary_entity_mention") or fact.get("subject"),
                        json.dumps(fact.get("additional_entity_mentions", [])),
                        fact["confidence"],
                        batch_key,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO evidence_anchors (
                        id, fact_id, page_id, source_kind, quote, bbox_json, confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        fact_id,
                        page_id,
                        fact.get("evidence_source_kind", "text"),
                        quote,
                        json.dumps(fact["evidence_bbox"]) if fact.get("evidence_bbox") else None,
                        fact["confidence"],
                        now,
                    ),
                )
                inserted += 1
            conn.execute(
                """
                UPDATE work_units
                SET status = ?, cache_path = ?, progress_detail = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    WorkUnitStatus.COMPLETED.value,
                    cache_path,
                    f"Extracted {inserted} evidence-backed fact(s).",
                    now,
                    unit_id,
                ),
            )
            self._append_history_in_connection(
                conn,
                "fact_batch_completed",
                unit_id,
                {"set_id": set_id, "document_id": document_id, "batch_key": batch_key, "fact_count": inserted},
                set_id=set_id,
            )
            conn.commit()
        return inserted

    def list_work_units(self, job_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM work_units WHERE job_id = ? ORDER BY created_at", (job_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            return dict(row) if row else None

    def get_document_by_hash(self, sha256: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
            return dict(row) if row else None

    def list_documents(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def list_pages(self, document_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pages WHERE document_id = ? ORDER BY page_number", (document_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def page_id_map(self, document_id: str) -> dict[int, str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, page_number FROM pages WHERE document_id = ?", (document_id,)
            ).fetchall()
            return {int(row["page_number"]): str(row["id"]) for row in rows}

    def get_work_unit(self, unit_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM work_units WHERE id = ?", (unit_id,)).fetchone()
            return dict(row) if row else None

    def list_facts(
        self, *, document_id: str | None = None, set_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if document_id and set_id:
                rows = conn.execute(
                    """
                    SELECT * FROM facts WHERE document_id = ? AND set_id = ? AND is_current = 1
                    ORDER BY created_at DESC
                    """,
                    (document_id, set_id),
                ).fetchall()
            elif document_id:
                rows = conn.execute(
                    "SELECT * FROM facts WHERE document_id = ? AND is_current = 1 ORDER BY created_at DESC",
                    (document_id,),
                ).fetchall()
            elif set_id:
                rows = conn.execute(
                    "SELECT * FROM facts WHERE set_id = ? AND is_current = 1 ORDER BY created_at DESC",
                    (set_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM facts WHERE is_current = 1 ORDER BY created_at DESC"
                ).fetchall()
            return [dict(row) for row in rows]

    def get_fact(self, fact_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
            return dict(row) if row else None

    def fact_cross_checks(self, set_id: str) -> dict[str, dict[str, Any]]:
        """Summarize stored cross-document decisions for every current fact in a set."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, left_fact_id, right_fact_id, label, explanation
                FROM relationships
                WHERE set_id = ? AND is_current = 1 AND stale = 0
                """,
                (set_id,),
            ).fetchall()
        by_fact: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            by_fact.setdefault(item["left_fact_id"], []).append(item)
            by_fact.setdefault(item["right_fact_id"], []).append(item)
        priority = {
            "contradicts": 4,
            "reconciled": 3,
            "corroborates": 2,
            "insufficient_evidence": 1,
            "not_comparable": 0,
        }
        labels = {
            "contradicts": "contradicted",
            "reconciled": "reconciled",
            "corroborates": "corroborated",
            "insufficient_evidence": "insufficient_evidence",
            "not_comparable": "no_comparable_source",
        }
        summaries: dict[str, dict[str, Any]] = {}
        for fact_id, decisions in by_fact.items():
            chosen = max(decisions, key=lambda item: priority.get(item["label"], -1))
            related_ids = {
                other
                for item in decisions
                for other in (item["left_fact_id"], item["right_fact_id"])
                if other != fact_id
            }
            summaries[fact_id] = {
                "cross_check_status": labels[chosen["label"]],
                "cross_check_explanation": chosen["explanation"],
                "cross_check_source_count": len(related_ids) + 1,
            }
        with self.connection() as conn:
            affected = conn.execute(
                """
                SELECT DISTINCT current.id
                FROM relationships relationship
                JOIN facts old ON old.id = relationship.left_fact_id
                JOIN facts current ON current.stable_id = old.stable_id AND current.is_current = 1
                WHERE relationship.set_id = ? AND relationship.is_current = 1 AND relationship.stale = 1
                UNION
                SELECT DISTINCT current.id
                FROM relationships relationship
                JOIN facts old ON old.id = relationship.right_fact_id
                JOIN facts current ON current.stable_id = old.stable_id AND current.is_current = 1
                WHERE relationship.set_id = ? AND relationship.is_current = 1 AND relationship.stale = 1
                """,
                (set_id, set_id),
            ).fetchall()
        for fact in affected:
            summaries[fact["id"]] = {"cross_check_status": "stale", "cross_check_explanation": "A source or entity changed. Recheck its comparisons before relying on the conclusion.", "cross_check_source_count": 1}
        return summaries

    def fact_evidence(self, fact_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT evidence_anchors.*, pages.page_number
                FROM evidence_anchors INNER JOIN pages ON pages.id = evidence_anchors.page_id
                WHERE evidence_anchors.fact_id = ? ORDER BY evidence_anchors.created_at
                """,
                (fact_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_entities(self, *, set_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if set_id:
                rows = conn.execute(
                    "SELECT * FROM entities WHERE set_id = ? ORDER BY canonical_name COLLATE NOCASE", (set_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM entities ORDER BY canonical_name COLLATE NOCASE").fetchall()
            return [dict(row) for row in rows]

    def create_entity(
        self, canonical_name: str, entity_type: str | None = None, *, set_id: str | None = None
    ) -> dict[str, Any]:
        now = utc_now()
        entity_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO entities (id, canonical_name, entity_type, set_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (entity_id, canonical_name, entity_type, set_id, now, now),
            )
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            return dict(row)

    def assign_fact_entity(self, fact_id: str, entity_id: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE facts SET primary_entity_id = ? WHERE id = ?", (entity_id, fact_id))

    def update_fact_embedding(self, fact_id: str, embedding: list[float]) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE facts SET embedding_json = ? WHERE id = ?", (json.dumps(embedding), fact_id))

    def facts_for_entity(self, entity_id: str, *, set_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if set_id:
                rows = conn.execute(
                    """
                    SELECT * FROM facts WHERE primary_entity_id = ? AND set_id = ?
                    AND is_current = 1 ORDER BY created_at
                    """,
                    (entity_id, set_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM facts WHERE primary_entity_id = ? AND is_current = 1 ORDER BY created_at
                    """,
                    (entity_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def entity_by_id(self, entity_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            return dict(row) if row else None

    def merge_entities(self, *, source_entity_id: str, target_entity_id: str) -> tuple[int, int]:
        """Merge facts into target and return (new revision, affected fact count)."""
        if source_entity_id == target_entity_id:
            target = self.entity_by_id(target_entity_id)
            return (int(target["resolution_revision"]) if target else 1, 0)
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            affected = conn.execute(
                "SELECT COUNT(*) AS count FROM facts WHERE primary_entity_id = ? AND is_current = 1",
                (source_entity_id,),
            ).fetchone()["count"]
            target = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (target_entity_id,)
            ).fetchone()
            if not target:
                conn.rollback()
                raise ValueError("Target entity does not exist")
            next_revision = int(target["resolution_revision"]) + 1
            conn.execute(
                "UPDATE facts SET primary_entity_id = ? WHERE primary_entity_id = ?",
                (target_entity_id, source_entity_id),
            )
            conn.execute(
                "UPDATE entities SET resolution_revision = ?, updated_at = ? WHERE id = ?",
                (next_revision, now, target_entity_id),
            )
            self._append_history_in_connection(
                conn,
                "entities_merged",
                target_entity_id,
                {
                    "source_entity_id": source_entity_id,
                    "target_entity_id": target_entity_id,
                    "resolution_revision": next_revision,
                    "affected_fact_count": affected,
                },
            )
            conn.commit()
            return next_revision, int(affected)

    def persist_relationship(
        self,
        *,
        left_fact_id: str,
        right_fact_id: str,
        set_id: str,
        label: str,
        explanation: str,
        confidence: float,
        deterministic_context: dict[str, Any],
        reasoning_trace: dict[str, Any],
        entity_resolution_revision: int,
        work_unit_id: str | None = None,
        replaces_id: str | None = None,
        correction_note: str | None = None,
    ) -> dict[str, Any]:
        left, right = sorted([left_fact_id, right_fact_id])
        stable_id = sha256(f"{left}|{right}".encode("utf-8")).hexdigest()[:32]
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if replaces_id:
                replaced = conn.execute(
                    "SELECT * FROM relationships WHERE id=? AND set_id=? AND is_current=1",
                    (replaces_id, set_id),
                ).fetchone()
                if not replaced:
                    conn.rollback()
                    raise ValueError("This comparison changed. Refresh before trying again.")
                stable_id = replaced["stable_id"]
                for fact_id in (left_fact_id, right_fact_id):
                    current = conn.execute("SELECT is_current, set_id FROM facts WHERE id=?", (fact_id,)).fetchone()
                    if not current or not current["is_current"] or current["set_id"] != set_id:
                        conn.rollback()
                        raise ValueError("A source changed during the recheck. Refresh and try again.")
            else:
                # Find the lineage by stable fact identities, including pre-upgrade rows.
                lineage = conn.execute("""SELECT r.stable_id FROM relationships r
                    JOIN facts l ON l.id=r.left_fact_id JOIN facts rr ON rr.id=r.right_fact_id
                    WHERE r.set_id=? AND r.is_current=1 AND
                    ((l.stable_id=(SELECT stable_id FROM facts WHERE id=?) AND rr.stable_id=(SELECT stable_id FROM facts WHERE id=?))
                    OR (l.stable_id=(SELECT stable_id FROM facts WHERE id=?) AND rr.stable_id=(SELECT stable_id FROM facts WHERE id=?))) LIMIT 1""", (set_id, left_fact_id, right_fact_id, right_fact_id, left_fact_id)).fetchone()
                if lineage:
                    stable_id = lineage["stable_id"]
            prior = conn.execute(
                """
                SELECT * FROM relationships
                WHERE stable_id = ? AND set_id = ? AND is_current = 1
                ORDER BY revision DESC LIMIT 1
                """,
                (stable_id, set_id),
            ).fetchone()
            revision = (int(prior["revision"]) + 1) if prior else 1
            if prior and not replaces_id and not prior["stale"]:
                if work_unit_id:
                    conn.execute("UPDATE work_units SET status='completed', updated_at=? WHERE id=?", (now, work_unit_id))
                conn.commit()
                return dict(prior)
            if prior:
                conn.execute(
                    "UPDATE relationships SET is_current = 0, superseded = 1 WHERE id = ?",
                    (prior["id"],),
                )
            relationship_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO relationships (
                    id, stable_id, revision, left_fact_id, right_fact_id, set_id, label, explanation, confidence,
                    deterministic_context_json, reasoning_trace_json, entity_resolution_revision,
                    stale, superseded, is_current, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?)
                """,
                (
                    relationship_id,
                    stable_id,
                    revision,
                    left_fact_id,
                    right_fact_id,
                    set_id,
                    label,
                    explanation,
                    confidence,
                    json.dumps(deterministic_context),
                    json.dumps(reasoning_trace),
                    entity_resolution_revision,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM relationships WHERE id = ?", (relationship_id,)).fetchone()
            if correction_note is not None and prior:
                from .correction_memory import pair_context, fingerprint
                left_row = dict(conn.execute("SELECT * FROM facts WHERE id=?", (left_fact_id,)).fetchone())
                right_row = dict(conn.execute("SELECT * FROM facts WHERE id=?", (right_fact_id,)).fetchone())
                context = pair_context(left_row, right_row)
                conn.execute("INSERT INTO corrections (id,target_type,target_id,field_path,previous_value,corrected_value,note,exact_fingerprint,semantic_context,created_at) VALUES (?, 'relationship', ?, 'label', ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), relationship_id, prior["label"], label, correction_note, fingerprint(context), json.dumps(context), now))
            if work_unit_id:
                conn.execute(
                    """
                    UPDATE work_units SET status = ?, progress_detail = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        WorkUnitStatus.COMPLETED.value,
                        f"Classified relationship as {label}.",
                        now,
                        work_unit_id,
                    ),
                )
            self._append_history_in_connection(
                conn,
                "relationship_corrected" if correction_note is not None else "relationship_rechecked" if replaces_id else "relationship_classified",
                relationship_id,
                {"set_id": set_id, "relationship_id": relationship_id, "previous_relationship_id": prior["id"] if prior else None, "left_fact_id": left_fact_id, "right_fact_id": right_fact_id, "label": label, "explanation": explanation, "confidence": confidence, "reasoning_trace": reasoning_trace},
                set_id=set_id,
            )
            conn.commit()
            return dict(row)

    def list_relationships(
        self,
        *,
        set_id: str | None = None,
        label: str | None = None,
        entity_id: str | None = None,
        stale: bool | None = None,
    ) -> list[dict[str, Any]]:
        where = ["r.is_current = 1"]
        values: list[Any] = []
        if set_id:
            where.append("r.set_id = ?")
            values.append(set_id)
        if label:
            where.append("r.label = ?")
            values.append(label)
        if stale is not None:
            where.append("r.stale = ?")
            values.append(1 if stale else 0)
        if entity_id:
            where.append("(left_fact.primary_entity_id = ? OR right_fact.primary_entity_id = ?)")
            values.extend([entity_id, entity_id])
        query = f"""
            SELECT r.* FROM relationships r
            INNER JOIN facts left_fact ON left_fact.id = r.left_fact_id
            INNER JOIN facts right_fact ON right_fact.id = r.right_fact_id
            WHERE {' AND '.join(where)} ORDER BY r.created_at DESC
        """
        with self.connection() as conn:
            rows = conn.execute(query, values).fetchall()
            return [dict(row) for row in rows]

    def correct_fact(
        self, *, fact_id: str, field_path: str, corrected_value: str, note: str | None
    ) -> dict[str, Any]:
        correctable_columns = {
            "claim_text",
            "subject",
            "predicate",
            "object_value",
            "raw_value",
            "normalized_value",
            "unit_or_currency",
            "time_period",
            "scope",
            "primary_entity_mention",
        }
        if field_path not in correctable_columns:
            raise ValueError(f"'{field_path}' cannot be corrected through this endpoint.")
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM facts WHERE id = ? AND is_current = 1", (fact_id,)
            ).fetchone()
            if not current:
                conn.rollback()
                raise KeyError("Current fact revision was not found")
            current_dict = dict(current)
            previous_value = str(current_dict.get(field_path) or "")
            new_fact_id = str(uuid.uuid4())
            next_revision = int(current_dict["revision"]) + 1
            values = {
                "id": new_fact_id,
                "stable_id": current_dict["stable_id"],
                "revision": next_revision,
                "document_id": current_dict["document_id"],
                "set_id": current_dict["set_id"],
                "claim_text": current_dict["claim_text"],
                "subject": current_dict["subject"],
                "predicate": current_dict["predicate"],
                "predicate_key": current_dict["predicate_key"],
                "object_value": current_dict["object_value"],
                "raw_value": current_dict["raw_value"],
                "normalized_value": current_dict["normalized_value"],
                "unit_or_currency": current_dict["unit_or_currency"],
                "time_period": current_dict["time_period"],
                "period_start": current_dict["period_start"],
                "period_end": current_dict["period_end"],
                "scope": current_dict["scope"],
                "qualifiers_json": current_dict["qualifiers_json"],
                "primary_entity_mention": current_dict["primary_entity_mention"],
                "primary_entity_id": current_dict["primary_entity_id"],
                "additional_entity_mentions_json": current_dict["additional_entity_mentions_json"],
                "embedding_json": None,
                "confidence": current_dict["confidence"],
                "extraction_source": "human_correction",
                "batch_key": current_dict["batch_key"],
                "correction_state": "corrected",
                "is_current": 1,
                "created_at": now,
            }
            values[field_path] = corrected_value
            if field_path == "predicate":
                values["predicate_key"] = " ".join(corrected_value.casefold().split())
            if field_path in {"subject", "primary_entity_mention"}:
                # A renamed subject must be resolved again rather than retaining a
                # potentially wrong entity cluster from the prior revision.
                values["primary_entity_id"] = None
            conn.execute("UPDATE facts SET is_current = 0 WHERE id = ?", (fact_id,))
            columns = list(values)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO facts ({', '.join(columns)}) VALUES ({placeholders})",
                [values[column] for column in columns],
            )
            evidence = conn.execute(
                "SELECT * FROM evidence_anchors WHERE fact_id = ?", (fact_id,)
            ).fetchall()
            for anchor in evidence:
                conn.execute(
                    """
                    INSERT INTO evidence_anchors (
                        id, fact_id, page_id, source_kind, quote, start_offset, end_offset,
                        bbox_json, confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        new_fact_id,
                        anchor["page_id"],
                        anchor["source_kind"],
                        anchor["quote"],
                        anchor["start_offset"],
                        anchor["end_offset"],
                        anchor["bbox_json"],
                        anchor["confidence"],
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE relationships SET stale = 1
                WHERE is_current = 1 AND (left_fact_id = ? OR right_fact_id = ?)
                """,
                (fact_id, fact_id),
            )
            from .correction_memory import fact_context, fingerprint as memory_fingerprint
            current_dict["evidence_quote"] = evidence[0]["quote"] if evidence else ""
            context = fact_context(current_dict)
            fingerprint = memory_fingerprint(context)
            correction_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO corrections (
                    id, target_type, target_id, field_path, previous_value, corrected_value,
                    note, exact_fingerprint, semantic_context, created_at
                ) VALUES (?, 'fact', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correction_id,
                    new_fact_id,
                    field_path,
                    previous_value,
                    corrected_value,
                    note,
                    fingerprint,
                    json.dumps(context),
                    now,
                ),
            )
            self._append_history_in_connection(
                conn,
                "fact_corrected",
                correction_id,
                {
                    "correction_id": correction_id,
                    "previous_fact_id": fact_id,
                    "current_fact_id": new_fact_id,
                    "field_path": field_path,
                    "previous_value": previous_value,
                    "corrected_value": corrected_value,
                    "note": note,
                },
                set_id=current_dict.get("set_id"),
            )
            result = conn.execute("SELECT * FROM facts WHERE id = ?", (new_fact_id,)).fetchone()
            conn.commit()
            return dict(result)

    def list_history_events(self, limit: int = 100, *, set_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if set_id:
                rows = conn.execute(
                    "SELECT * FROM history_events WHERE set_id = ? ORDER BY created_at DESC LIMIT ?",
                    (set_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM history_events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def job_should_stop(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return job is None or job["status"] != JobStatus.RUNNING.value

    def get_latest_job_for_document(self, document_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE document_id = ? ORDER BY created_at DESC LIMIT 1", (document_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_latest_jobs(self) -> list[dict[str, Any]]:
        """Return one most-recent job per document for the workspace queue."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT j.* FROM jobs j
                INNER JOIN (
                    SELECT document_id, MAX(created_at) AS latest_created_at
                    FROM jobs GROUP BY document_id
                ) latest
                    ON latest.document_id = j.document_id
                    AND latest.latest_created_at = j.created_at
                ORDER BY j.created_at DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def queue_position(self, job_id: str) -> int | None:
        job = self.get_job(job_id)
        if not job or job["status"] != JobStatus.QUEUED.value:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM jobs
                WHERE status = ? AND created_at < ?
                """,
                (JobStatus.QUEUED.value, job["created_at"]),
            ).fetchone()
            return int(row["count"]) + 1

    def append_history(
        self,
        event_type: str,
        related_record_id: str | None,
        snapshot: dict[str, Any],
        *,
        set_id: str | None = None,
    ) -> None:
        with self.connection() as conn:
            self._append_history_in_connection(conn, event_type, related_record_id, snapshot, set_id=set_id)

    @staticmethod
    def _append_history_in_connection(
        conn: sqlite3.Connection,
        event_type: str,
        related_record_id: str | None,
        snapshot: dict[str, Any],
        *,
        set_id: str | None = None,
    ) -> None:
        conn.execute(
            """
                INSERT INTO history_events (id, event_type, related_record_id, set_id, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
            (str(uuid.uuid4()), event_type, related_record_id, set_id, json.dumps(snapshot), utc_now()),
        )

    def get_document_set(self, set_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM document_sets WHERE id = ?", (set_id,)).fetchone()
            return dict(row) if row else None

    def list_document_sets(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT sets.*, COUNT(membership.document_id) AS document_count
                FROM document_sets sets
                LEFT JOIN set_documents membership ON membership.set_id = sets.id
                GROUP BY sets.id ORDER BY sets.created_at DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_set_documents(self, set_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT documents.*, membership.position, jobs.id AS job_id, jobs.status AS job_status,
                       jobs.stage AS job_stage, jobs.progress_current, jobs.progress_total,
                       jobs.progress_detail, jobs.last_error_json, jobs.created_at AS job_created_at,
                       jobs.updated_at AS job_updated_at
                FROM set_documents membership
                INNER JOIN documents ON documents.id = membership.document_id
                LEFT JOIN jobs ON jobs.document_id = documents.id AND jobs.set_id = membership.set_id
                WHERE membership.set_id = ?
                ORDER BY membership.position ASC, jobs.created_at DESC
                """,
                (set_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def refresh_set_status(self, set_id: str) -> None:
        with self.connection() as conn:
            rows = conn.execute("SELECT status FROM jobs WHERE set_id = ?", (set_id,)).fetchall()
            if not rows:
                return
            statuses = {row["status"] for row in rows}
            if JobStatus.RUNNING.value in statuses or JobStatus.QUEUED.value in statuses:
                next_status = "processing"
            elif JobStatus.NEEDS_ATTENTION.value in statuses or JobStatus.TERMINAL_FAILURE.value in statuses:
                next_status = "completed_with_issues"
            elif JobStatus.COMPLETED_WITH_ISSUES.value in statuses:
                next_status = "completed_with_issues"
            else:
                next_status = "completed"
            conn.execute(
                "UPDATE document_sets SET status = ?, updated_at = ? WHERE id = ?",
                (next_status, utc_now(), set_id),
            )

    def retry_set_failed_work(self, set_id: str) -> bool:
        """Requeue only recoverable failed work for an existing set after a fix or transient outage."""
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            document_set = conn.execute(
                "SELECT id FROM document_sets WHERE id = ?", (set_id,)
            ).fetchone()
            if not document_set:
                conn.rollback()
                return False
            jobs = conn.execute(
                """
                SELECT id FROM jobs WHERE set_id = ?
                AND status IN (?, ?)
                """,
                (set_id, JobStatus.COMPLETED_WITH_ISSUES.value, JobStatus.NEEDS_ATTENTION.value),
            ).fetchall()
            job_ids = [row["id"] for row in jobs]
            if job_ids:
                placeholders = ", ".join("?" for _ in job_ids)
                conn.execute(
                    f"""
                    UPDATE work_units
                    SET status = ?, attempts = 0, error_json = NULL,
                        progress_detail = 'Manually requeued after a recoverable failure.', updated_at = ?
                    WHERE job_id IN ({placeholders})
                      AND status = ?
                    """,
                    [WorkUnitStatus.QUEUED.value, now, *job_ids, WorkUnitStatus.RETRYABLE_FAILED.value],
                )
                conn.execute(
                    f"""
                    UPDATE jobs
                    SET status = ?, stage = ?, progress_current = 0, progress_total = 0,
                        progress_detail = 'Queued to retry recoverable failed work.', last_error_json = NULL,
                        updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [JobStatus.QUEUED.value, PipelineStage.QUEUED.value, now, *job_ids],
                )
            conn.execute(
                "UPDATE document_sets SET status = 'queued', updated_at = ? WHERE id = ?",
                (now, set_id),
            )
            self._append_history_in_connection(
                conn,
                "set_retry_queued",
                set_id,
                {"set_id": set_id, "job_count": len(job_ids), "reason": "recoverable_failed_work"},
                set_id=set_id,
            )
            conn.commit()
            return True

    def stop_set_document_job(self, *, set_id: str, document_id: str) -> dict[str, Any] | None:
        """Stop a queued job immediately, or request a cooperative stop for a running job."""
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM jobs WHERE set_id = ? AND document_id = ? ORDER BY created_at DESC LIMIT 1",
                (set_id, document_id),
            ).fetchone()
            if not job:
                conn.rollback()
                return None
            if job["status"] not in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
                conn.rollback()
                return dict(job)
            conn.execute(
                """
                UPDATE jobs SET status = ?, progress_detail = ?,
                    last_error_json = ?, updated_at = ? WHERE id = ?
                """,
                (
                    JobStatus.NEEDS_ATTENTION.value,
                    "Stopped by you. Retry it when you are ready.",
                    json.dumps({"type": "StoppedByUser", "message": "Processing was stopped by the user."}),
                    now,
                    job["id"],
                ),
            )
            conn.execute(
                """
                UPDATE work_units SET status = ?, progress_detail = ?, updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    WorkUnitStatus.RETRYABLE_FAILED.value,
                    "Stopped by the user; safe to retry.",
                    now,
                    job["id"],
                    WorkUnitStatus.RUNNING.value,
                ),
            )
            self._append_history_in_connection(
                conn, "job_stop_requested", job["id"],
                {"job_id": job["id"], "document_id": document_id}, set_id=set_id,
            )
            conn.commit()
        self.refresh_set_status(set_id)
        return self.get_job(job["id"])

    def remove_document_from_set(self, *, set_id: str, document_id: str) -> dict[str, Any] | None:
        """Remove one document's scoped output and return its orphaned source, if any."""
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            document = conn.execute(
                """
                SELECT documents.* FROM documents INNER JOIN set_documents membership
                ON membership.document_id = documents.id
                WHERE membership.set_id = ? AND documents.id = ?
                """,
                (set_id, document_id),
            ).fetchone()
            if not document:
                conn.rollback()
                return None
            job = conn.execute(
                "SELECT * FROM jobs WHERE set_id = ? AND document_id = ? ORDER BY created_at DESC LIMIT 1",
                (set_id, document_id),
            ).fetchone()
            if job and job["status"] in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
                conn.rollback()
                raise ValueError("Stop this PDF first, then wait for its processing step to finish.")
            conn.execute(
                """
                DELETE FROM relationships WHERE set_id = ? AND (
                    left_fact_id IN (SELECT id FROM facts WHERE set_id = ? AND document_id = ?)
                    OR right_fact_id IN (SELECT id FROM facts WHERE set_id = ? AND document_id = ?)
                )
                """,
                (set_id, set_id, document_id, set_id, document_id),
            )
            conn.execute("DELETE FROM facts WHERE set_id = ? AND document_id = ?", (set_id, document_id))
            conn.execute("DELETE FROM jobs WHERE set_id = ? AND document_id = ?", (set_id, document_id))
            conn.execute("DELETE FROM set_documents WHERE set_id = ? AND document_id = ?", (set_id, document_id))
            remaining = conn.execute(
                "SELECT COUNT(*) AS count FROM set_documents WHERE document_id = ?", (document_id,)
            ).fetchone()["count"]
            orphaned = dict(document) if remaining == 0 else None
            if orphaned:
                conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self._append_history_in_connection(
                conn, "document_removed_from_set", document_id,
                {"set_id": set_id, "document_id": document_id}, set_id=set_id,
            )
            conn.execute(
                "UPDATE document_sets SET status = 'completed', updated_at = ? WHERE id = ?",
                (now, set_id),
            )
            conn.commit()
        self.refresh_set_status(set_id)
        return orphaned or {}

    def delete_document_set(self, set_id: str) -> list[dict[str, Any]] | None:
        """Permanently delete a set and scoped outputs; shared source PDFs are retained."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("SELECT id FROM document_sets WHERE id = ?", (set_id,)).fetchone()
            if not exists:
                conn.rollback()
                return None
            active = conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE set_id = ? AND status IN (?, ?)",
                (set_id, JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchone()["count"]
            if active:
                conn.rollback()
                raise ValueError("Stop all processing PDFs in this set before deleting the set.")
            documents = [dict(row) for row in conn.execute(
                """
                SELECT documents.* FROM documents INNER JOIN set_documents membership
                ON membership.document_id = documents.id WHERE membership.set_id = ?
                """, (set_id,)
            ).fetchall()]
            conn.execute("DELETE FROM relationships WHERE set_id = ?", (set_id,))
            conn.execute("DELETE FROM facts WHERE set_id = ?", (set_id,))
            conn.execute("DELETE FROM entities WHERE set_id = ?", (set_id,))
            conn.execute("DELETE FROM jobs WHERE set_id = ?", (set_id,))
            conn.execute("DELETE FROM history_events WHERE set_id = ?", (set_id,))
            conn.execute("DELETE FROM document_sets WHERE id = ?", (set_id,))
            orphaned: list[dict[str, Any]] = []
            for document in documents:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS count FROM set_documents WHERE document_id = ?", (document["id"],)
                ).fetchone()["count"]
                if remaining == 0:
                    conn.execute("DELETE FROM documents WHERE id = ?", (document["id"],))
                    orphaned.append(document)
            conn.commit()
            return orphaned
