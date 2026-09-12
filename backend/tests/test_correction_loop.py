from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app.correction_memory import CorrectionMemory, pair_context
from backend.app.database import Database
from backend.app.gemini import ExtractionPage, GeminiExtractionError, GeminiGateway
from backend.app.ingestion import PageBlock, PageVisual, PdfIngestionService
from backend.app.relationships import DeterministicComparator, RelationshipService
from backend.app.settings import Settings
from backend.tests.test_database import _fact_payload


def pair(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    uploads = [{"original_filename": f"{i}.pdf", "sha256": str(i)*64, "stored_path": f"/{i}.pdf", "byte_size": 1} for i in (1,2)]
    group,members = db.create_document_set(name="Review", uploads=uploads)
    for document,job,_ in members:
        page = db.create_or_update_page(document_id=document["id"],page_number=1,status="completed")
        unit = db.create_work_unit(job_id=job["id"],document_id=document["id"],unit_type="fact_extraction",unit_key=document["id"],max_attempts=1)
        db.persist_fact_batch(unit_id=unit["id"],document_id=document["id"],set_id=group["id"],batch_key=document["id"],facts=[_fact_payload("Metric was 10 units.","Metric was 10 units.")],page_ids={1:page["id"]},cache_path="unused")
    left,right = db.list_facts(set_id=group["id"])
    relation = db.persist_relationship(left_fact_id=left["id"],right_fact_id=right["id"],set_id=group["id"],label="corroborates",explanation="Values agree in the same context.",confidence=.9,deterministic_context={},reasoning_trace={},entity_resolution_revision=1)
    return db,left,right,relation


def test_fact_memory_and_recheck_follow_latest_revision(tmp_path):
    db,left,right,relation = pair(tmp_path)
    changed = db.correct_fact(fact_id=left["id"],field_path="normalized_value",corrected_value="12",note="Source review changed the value.")
    assert db.fact_cross_checks(left["set_id"])[changed["id"]]["cross_check_status"] == "stale"
    settings=Settings(_env_file=None)
    service=RelationshipService(settings,db,GeminiGateway(settings))
    fresh=service.revise(relation["id"])
    assert fresh["stable_id"] == relation["stable_id"]
    assert fresh["revision"] == 2 and not fresh["stale"]
    assert changed["id"] in (fresh["left_fact_id"],fresh["right_fact_id"])
    assert fresh["label"] == "contradicts"
    assert len(db.list_relationships()) == 1
    assert any(e["event_type"] == "fact_corrected" for e in db.list_history_events(set_id=left["set_id"]))
    payload=_fact_payload("Metric was 10 units.","Metric was 10 units.")
    assert CorrectionMemory(db).apply_facts([payload])[0]["normalized_value"] == "12"
    payload["time_period"]="2025"
    assert CorrectionMemory(db).apply_facts([payload])[0]["normalized_value"] == "10"


def test_relationship_correction_has_revision_and_shared_memory(tmp_path):
    db,left,right,relation=pair(tmp_path)
    settings=Settings(_env_file=None)
    service=RelationshipService(settings,db,GeminiGateway(settings))
    corrected=service.revise(relation["id"],label="not_comparable",note="These numbers refer to separate definitions.")
    assert corrected["revision"] == 2
    memory=CorrectionMemory(db).retrieve("relationship",pair_context(left,right),exact=True)
    assert memory[0]["after"] == "not_comparable"
    fresh=service.revise(corrected["id"])
    assert fresh["label"] == "not_comparable"
    with pytest.raises(KeyError):
        service.revise(relation["id"])


def test_missing_context_does_not_create_false_agreement():
    fact={"normalized_value":"10","unit_or_currency":"units"}
    assert DeterministicComparator().compare(fact,fact)[0] is None


def test_local_similarity_falls_back_without_stopping_candidate_generation(tmp_path, monkeypatch):
    db = Database(tmp_path / "similarity.sqlite3")
    db.initialize()
    settings = Settings(_env_file=None)
    service = RelationshipService(settings, db, GeminiGateway(settings))
    left = {"id": "left", "document_id": "one", "subject": "Acme", "predicate": "revenue", "claim_text": "Acme revenue was 10 units."}
    right = {"id": "right", "document_id": "two", "subject": "Acme", "predicate": "revenue", "claim_text": "Acme revenue was 12 units."}
    monkeypatch.setattr(service.embedder, "embedding_for_fact", lambda _fact: (_ for _ in ()).throw(OSError("model unavailable")))

    pairs = service._candidate_pairs([left, right], "two")

    assert len(pairs) == 1
    assert pairs[0].matching_mode == "local_lexical_fallback"


def test_multi_column_triage_does_not_mistake_indents_for_columns():
    class Tables:
        tables = []

    class Rect:
        width = 600
        height = 800

        @staticmethod
        def get_area():
            return 480_000

    class Page:
        rect = Rect()

        @staticmethod
        def find_tables():
            return Tables()

    text = "A substantive paragraph with enough characters to be counted as body text. " * 2
    indented_single_column = [
        PageBlock(kind="text", text=text, bbox=[offset, index * 50, 560, index * 50 + 30])
        for index, offset in enumerate((30, 45, 60, 75, 90, 105, 120, 135))
    ]
    actual_columns = [
        *[
            PageBlock(kind="text", text=text, bbox=[30, index * 50, 260, index * 50 + 30])
            for index in range(4)
        ],
        *[
            PageBlock(kind="text", text=text, bbox=[320, index * 50, 560, index * 50 + 30])
            for index in range(4)
        ],
    ]

    assert not PdfIngestionService._triage(Page(), text, indented_single_column, []).metrics[
        "likely_multi_column"
    ]
    assert PdfIngestionService._triage(Page(), text, actual_columns, []).metrics[
        "likely_multi_column"
    ]


def test_docling_pending_cache_is_upgraded_on_retry():
    assert PdfIngestionService._needs_docling_upgrade(
        {"status": "completed", "parser_used": "docling_pending"}
    )
    assert not PdfIngestionService._needs_docling_upgrade(
        {"status": "completed", "parser_used": "pymupdf_native"}
    )


def test_data_bearing_visual_is_visible_in_history_when_chart_reading_is_disabled(tmp_path):
    db, left, _, _ = pair(tmp_path)
    job = db.get_latest_job_for_document(left["document_id"])
    service = object.__new__(PdfIngestionService)
    service.database = db
    service.settings = Settings(_env_file=None)

    service._register_skipped_visual_units(
        job_id=job["id"],
        document_id=left["document_id"],
        set_id=left["set_id"],
        document_hash="visual-test",
        page_number=1,
        visuals=[PageVisual(index=0, bbox=[0, 0, 100, 100], data_bearing_candidate=True)],
    )

    unit = next(unit for unit in db.list_work_units(job["id"]) if unit["unit_type"] == "chart_extraction")
    assert unit["status"] == "skipped"
    assert unit["skip_reason"] == "image_data_review_not_enabled"
    assert any(
        event["event_type"] == "visual_needs_review"
        for event in db.list_history_events(set_id=left["set_id"])
    )


def test_daily_budget_persists_across_gateways(tmp_path):
    db=Database(tmp_path / "budget.sqlite3"); db.initialize()
    settings=Settings(_env_file=None,FACTUALIZE_GEMINI_DAILY_REQUEST_BUDGET=1)
    GeminiGateway(settings,db)._reserve_request()
    with pytest.raises(GeminiExtractionError,match="daily quota"):
        GeminiGateway(settings,db)._reserve_request()


def test_relationship_api_revision_and_stale_conflict(tmp_path):
    from backend.app.main import app
    db,left,right,relation = pair(tmp_path)
    settings = Settings(_env_file=None)
    app.state.database = db
    app.state.relationship_service = RelationshipService(settings, db, GeminiGateway(settings))
    client = TestClient(app)  # No lifespan: isolated database and no processing worker.
    detail = client.get(f"/api/v1/relationships/{relation['id']}")
    assert detail.status_code == 200
    assert detail.json()["left"]["evidence"][0]["quote"]
    result = client.post(f"/api/v1/relationships/{relation['id']}/corrections", json={"label":"not_comparable", "note":"Separate definitions were confirmed in the source."})
    assert result.status_code == 200
    assert result.json()["revision"] == 2
    assert client.post(f"/api/v1/relationships/{relation['id']}/recheck").status_code == 404


def test_extraction_rejects_invented_quote(monkeypatch):
    settings = Settings(_env_file=None, GEMINI_API_KEY="test-only", GEMINI_EXTRACTION_MODEL="mock")
    gateway = GeminiGateway(settings)
    monkeypatch.setattr(gateway, "_generate_json", lambda *args: json.dumps({"facts":[_fact_payload("Metric was 10 units.","invented evidence")]}))
    with pytest.raises(GeminiExtractionError, match="absent"):
        gateway.extract_facts([ExtractionPage(1,"Metric was 10 units.")])
