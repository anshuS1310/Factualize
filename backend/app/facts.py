from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .database import Database
from .correction_memory import CorrectionMemory
from .errors import ProcessingStopped
from .gemini import ExtractionPage, GeminiConfigurationError, GeminiExtractionError, GeminiGateway
from .schemas import PipelineStage, WorkUnitStatus
from .settings import Settings


class FactExtractionService:
    """Builds atomic, cached page batches and persists only fully validated facts."""

    max_batch_characters = 24_000
    max_pages_per_batch = 8

    def __init__(self, settings: Settings, database: Database, gateway: GeminiGateway) -> None:
        self.settings = settings
        self.database = database
        self.gateway = gateway

    def extract_for_document(self, job_id: str, document: dict[str, Any], set_id: str) -> tuple[int, int]:
        pages = self._load_pages(document)
        batches = self._batches(pages)
        failed_batches = 0
        extracted_count = 0
        page_ids = self.database.page_id_map(document["id"])
        self.database.update_job(
            job_id,
            stage=PipelineStage.EXTRACTING_FACTS,
            progress_current=0,
            progress_total=len(batches),
            progress_detail=f"Preparing {len(batches)} evidence-preserving fact batches",
        )
        for batch_index, batch in enumerate(batches, start=1):
            if self.database.job_should_stop(job_id):
                raise ProcessingStopped("Processing was stopped by the user.")
            batch_key = self._batch_key(document["sha256"], batch)
            unit = self.database.create_work_unit(
                job_id=job_id,
                document_id=document["id"],
                unit_type="fact_extraction",
                unit_key=f"{batch_key}:set:{set_id}",
                max_attempts=self.settings.gemini_max_automatic_attempts,
            )
            if unit["status"] == WorkUnitStatus.COMPLETED.value:
                self.database.update_job(
                    job_id,
                    progress_current=batch_index,
                    progress_total=len(batches),
                    progress_detail=f"Reused fact batch {batch_index} of {len(batches)}",
                )
                continue

            cached_facts = self._read_batch_cache(document["sha256"], batch_key)
            if cached_facts is not None:
                cache_path = self._batch_cache_path(document["sha256"], batch_key)
                extracted_count += self.database.persist_fact_batch(
                    unit_id=unit["id"],
                    document_id=document["id"],
                    set_id=set_id,
                    batch_key=batch_key,
                    facts=CorrectionMemory(self.database).apply_facts(cached_facts),
                    page_ids=page_ids,
                    cache_path=str(cache_path),
                )
                self.database.update_job(
                    job_id,
                    progress_current=batch_index,
                    progress_total=len(batches),
                    progress_detail=f"Reused cached fact batch {batch_index} of {len(batches)}",
                )
                continue

            success = False
            while int(unit["attempts"]) < self.settings.gemini_max_automatic_attempts:
                self.database.update_work_unit(
                    unit["id"],
                    status=WorkUnitStatus.RUNNING,
                    progress_detail=f"Extracting fact batch {batch_index} of {len(batches)}",
                    increment_attempts=True,
                )
                try:
                    response = self.gateway.extract_facts(batch)
                    prepared = [fact.model_dump() for fact in response.facts]
                    self._ground_text_evidence(document, prepared)
                    cache_path = self._write_batch_cache(document["sha256"], batch_key, prepared)
                    extracted_count += self.database.persist_fact_batch(
                        unit_id=unit["id"],
                        document_id=document["id"],
                        set_id=set_id,
                        batch_key=batch_key,
                        facts=CorrectionMemory(self.database).apply_facts(prepared),
                        page_ids=page_ids,
                        cache_path=str(cache_path),
                    )
                    success = True
                    break
                except GeminiConfigurationError:
                    raise
                except GeminiExtractionError as error:
                    unit = self.database.get_work_unit(unit["id"]) or unit
                    if "quota" in str(error).casefold() or "429" in str(error) or int(unit["attempts"]) >= self.settings.gemini_max_automatic_attempts:
                        self.database.update_work_unit(
                            unit["id"],
                            status=WorkUnitStatus.RETRYABLE_FAILED,
                            progress_detail="Automatic retries exhausted; manual retry is available.",
                            error={"type": type(error).__name__, "message": str(error)},
                        )
                        self.database.append_history(
                            "fact_batch_retryable_failure",
                            unit["id"],
                            {"batch_key": batch_key, "message": str(error)},
                            set_id=set_id,
                        )
                        break
                    time.sleep(min(8, 2 ** int(unit["attempts"])))
            if not success:
                failed_batches += 1
            self.database.update_job(
                job_id,
                progress_current=batch_index,
                progress_total=len(batches),
                progress_detail=f"Processed fact batch {batch_index} of {len(batches)}",
            )
        return extracted_count, failed_batches

    def _load_pages(self, document: dict[str, Any]) -> list[ExtractionPage]:
        pages: list[ExtractionPage] = []
        for page in self.database.list_pages(document["id"]):
            if page["status"] != "completed" or not page["cache_path"]:
                continue
            artifact = json.loads(Path(page["cache_path"]).read_text(encoding="utf-8"))
            text = artifact.get("extracted_text", "").strip()
            if text:
                pages.append(ExtractionPage(page_number=int(page["page_number"]), text=text))
        return pages

    def _ground_text_evidence(self, document: dict[str, Any], facts: list[dict[str, Any]]) -> None:
        """Convert a cited quote into a normalized page rectangle when native text supports it."""
        artifacts: dict[int, dict[str, Any]] = {}
        for page in self.database.list_pages(document["id"]):
            if page["cache_path"] and Path(page["cache_path"]).is_file():
                artifacts[int(page["page_number"])] = json.loads(
                    Path(page["cache_path"]).read_text(encoding="utf-8")
                )
        for fact in facts:
            if fact.get("evidence_source_kind") != "text":
                continue
            artifact = artifacts.get(fact["evidence_page"])
            if not artifact:
                fact["evidence_bbox"] = None
                continue
            quote = self._compact(fact.get("evidence_quote", ""))
            width = float(artifact.get("page_width") or 0)
            height = float(artifact.get("page_height") or 0)
            if not quote or not width or not height:
                fact["evidence_bbox"] = None
                continue
            matching_block = next(
                (
                    block
                    for block in artifact.get("blocks", [])
                    if quote in self._compact(block.get("text", ""))
                    or self._compact(block.get("text", "")) in quote
                ),
                None,
            )
            if not matching_block:
                fact["evidence_bbox"] = None
                continue
            x0, y0, x1, y1 = matching_block["bbox"]
            fact["evidence_bbox"] = [
                round(max(0.0, min(1.0, x0 / width)), 6),
                round(max(0.0, min(1.0, y0 / height)), 6),
                round(max(0.0, min(1.0, x1 / width)), 6),
                round(max(0.0, min(1.0, y1 / height)), 6),
            ]

    @staticmethod
    def _compact(value: str) -> str:
        return "".join(value.casefold().split())

    def _batches(self, pages: list[ExtractionPage]) -> list[list[ExtractionPage]]:
        batches: list[list[ExtractionPage]] = []
        current: list[ExtractionPage] = []
        current_length = 0
        for page in pages:
            if current and (
                len(current) >= self.max_pages_per_batch
                or current_length + len(page.text) > self.max_batch_characters
            ):
                batches.append(current)
                current = []
                current_length = 0
            current.append(page)
            current_length += len(page.text)
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _batch_key(document_hash: str, pages: list[ExtractionPage]) -> str:
        material = "|".join(f"{page.page_number}:{hashlib.sha256(page.text.encode()).hexdigest()}" for page in pages)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return f"facts:{document_hash}:{digest}:schema-v1"

    def _write_batch_cache(
        self, document_hash: str, batch_key: str, facts: list[dict[str, Any]]
    ) -> Path:
        output = self._batch_cache_path(document_hash, batch_key)
        output.write_text(json.dumps({"facts": facts}, indent=2), encoding="utf-8")
        return output

    def _batch_cache_path(self, document_hash: str, batch_key: str) -> Path:
        path = self.settings.cache_dir / document_hash / "fact-batches"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{hashlib.sha256(batch_key.encode()).hexdigest()[:24]}.json"

    def _read_batch_cache(self, document_hash: str, batch_key: str) -> list[dict[str, Any]] | None:
        cache_path = self._batch_cache_path(document_hash, batch_key)
        if not cache_path.is_file():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            facts = payload.get("facts")
            return facts if isinstance(facts, list) else None
        except (OSError, json.JSONDecodeError):
            return None
