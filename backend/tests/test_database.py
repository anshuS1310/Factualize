from __future__ import annotations

from backend.app.database import Database
from backend.app.schemas import WorkUnitStatus


def _document_and_page(database: Database) -> tuple[dict, dict, dict]:
    document, job, reused = database.create_or_reuse_document(
        original_filename="generic-source.pdf",
        sha256="a" * 64,
        stored_path="/tmp/generic-source.pdf",
        byte_size=100,
    )
    assert not reused
    page = database.create_or_update_page(document_id=document["id"], page_number=1, status="completed")
    return document, job, page


def _fact_payload(claim: str, quote: str) -> dict:
    return {
        "claim_text": claim,
        "subject": "Example organization",
        "predicate": "reports metric",
        "object_or_value": "10",
        "raw_value": "10",
        "normalized_value": "10",
        "unit_or_currency": "units",
        "time_period": "2024",
        "scope": "group",
        "qualifiers": {},
        "primary_entity_mention": "Example organization",
        "additional_entity_mentions": [],
        "confidence": 0.9,
        "evidence_quote": quote,
        "evidence_page": 1,
        "evidence_bbox": [0.1, 0.1, 0.4, 0.2],
        "evidence_source_kind": "text",
    }


def test_atomic_fact_batch_and_correction_stales_relationship(tmp_path):
    database = Database(tmp_path / "facts.sqlite3")
    database.initialize()
    document, job, page = _document_and_page(database)
    unit = database.create_work_unit(
        job_id=job["id"],
        document_id=document["id"],
        unit_type="fact_extraction",
        unit_key="facts:test:one",
        max_attempts=2,
    )
    saved = database.persist_fact_batch(
        unit_id=unit["id"],
        document_id=document["id"],
        set_id="test-set",
        batch_key="facts:test:one",
        facts=[_fact_payload("Metric was 10 units.", "Metric was 10 units.")],
        page_ids={1: page["id"]},
        cache_path=str(tmp_path / "batch.json"),
    )
    assert saved == 1
    assert database.get_work_unit(unit["id"])["status"] == WorkUnitStatus.COMPLETED.value
    fact = database.list_facts(document_id=document["id"])[0]

    relationship = database.persist_relationship(
        left_fact_id=fact["id"],
        right_fact_id=fact["id"],
        set_id="test-set",
        label="corroborates",
        explanation="Controlled test relationship.",
        confidence=0.9,
        deterministic_context={},
        reasoning_trace={},
        entity_resolution_revision=1,
    )
    cross_check = database.fact_cross_checks("test-set")[fact["id"]]
    assert cross_check["cross_check_status"] == "corroborated"
    corrected = database.correct_fact(
        fact_id=fact["id"],
        field_path="normalized_value",
        corrected_value="12",
        note="Source review corrected the value.",
    )

    assert corrected["revision"] == 2
    assert corrected["is_current"] == 1
    assert database.get_fact(fact["id"])["is_current"] == 0
    stale = database.list_relationships(stale=True)
    assert [item["id"] for item in stale] == [relationship["id"]]


def test_skipped_work_unit_has_explicit_reason(tmp_path):
    database = Database(tmp_path / "work.sqlite3")
    database.initialize()
    document, job, _ = _document_and_page(database)
    unit = database.create_work_unit(
        job_id=job["id"],
        document_id=document["id"],
        unit_type="chart_extraction",
        unit_key="visual:test:one",
        max_attempts=2,
    )
    database.update_work_unit(
        unit["id"],
        status=WorkUnitStatus.SKIPPED,
        skip_reason="non_data_visual",
        progress_detail="Visual was classified as decorative.",
    )
    saved = database.get_work_unit(unit["id"])
    assert saved["status"] == WorkUnitStatus.SKIPPED.value
    assert saved["skip_reason"] == "non_data_visual"


def test_pdf_sets_scope_reused_documents_and_facts(tmp_path):
    database = Database(tmp_path / "sets.sqlite3")
    database.initialize()
    upload = {
        "original_filename": "source.pdf",
        "sha256": "b" * 64,
        "stored_path": "/tmp/source.pdf",
        "byte_size": 100,
    }
    first_set, first_members = database.create_document_set(name="First set", uploads=[upload])
    second_set, second_members = database.create_document_set(name="Second set", uploads=[upload])
    first_document, first_job, first_reused = first_members[0]
    second_document, second_job, second_reused = second_members[0]
    assert not first_reused
    assert second_reused
    assert first_document["id"] == second_document["id"]
    page = database.create_or_update_page(document_id=first_document["id"], page_number=1, status="completed")
    for document_set, job, key in ((first_set, first_job, "one"), (second_set, second_job, "two")):
        unit = database.create_work_unit(
            job_id=job["id"], document_id=first_document["id"], unit_type="fact_extraction",
            unit_key=f"facts:test:{key}:set:{document_set['id']}", max_attempts=2,
        )
        database.persist_fact_batch(
            unit_id=unit["id"], document_id=first_document["id"], set_id=document_set["id"],
            batch_key=f"facts:test:{key}", facts=[_fact_payload("Metric was 10 units.", "Metric was 10 units.")],
            page_ids={1: page["id"]}, cache_path=str(tmp_path / f"{key}.json"),
        )
    assert len(database.list_facts(set_id=first_set["id"])) == 1
    assert len(database.list_facts(set_id=second_set["id"])) == 1
    assert database.list_history_events(set_id=first_set["id"])


def test_removing_a_pdf_keeps_a_shared_source_for_another_set(tmp_path):
    database = Database(tmp_path / "removal.sqlite3")
    database.initialize()
    upload = {
        "original_filename": "shared.pdf",
        "sha256": "c" * 64,
        "stored_path": "/tmp/shared.pdf",
        "byte_size": 100,
    }
    first_set, first_members = database.create_document_set(name="First", uploads=[upload])
    second_set, second_members = database.create_document_set(name="Second", uploads=[upload])
    document_id = first_members[0][0]["id"]
    database.stop_set_document_job(set_id=first_set["id"], document_id=document_id)
    removed = database.remove_document_from_set(set_id=first_set["id"], document_id=document_id)
    assert removed == {}
    assert database.get_document(document_id) is not None
    assert database.get_document_set(first_set["id"])
    assert len(database.list_set_documents(first_set["id"])) == 0
    assert len(database.list_set_documents(second_set["id"])) == 1
    database.stop_set_document_job(set_id=second_set["id"], document_id=second_members[0][0]["id"])
    orphaned = database.delete_document_set(second_set["id"])
    assert orphaned and orphaned[0]["id"] == document_id
    assert database.get_document(document_id) is None
