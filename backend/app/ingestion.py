from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from pydantic import BaseModel, Field

from .database import Database
from .errors import DocumentProcessingError, ProcessingStopped
from .facts import FactExtractionService
from .entities import EntityResolutionService
from .relationships import RelationshipService
from .schemas import JobStatus, PipelineStage, WorkUnitStatus
from .settings import Settings
from .storage import LocalDocumentStorage


class PageBlock(BaseModel):
    kind: str
    text: str = ""
    bbox: list[float]


class PageVisual(BaseModel):
    index: int
    bbox: list[float]
    candidate_reason: str | None = None
    data_bearing_candidate: bool


class PageArtifact(BaseModel):
    schema_version: int = 1
    page_number: int
    page_width: float
    page_height: float
    triage_result: str
    parser_used: str
    extracted_text: str
    blocks: list[PageBlock] = Field(default_factory=list)
    visuals: list[PageVisual] = Field(default_factory=list)
    metrics: dict[str, float | int | bool]


@dataclass(frozen=True)
class PageTriage:
    result: str
    parser: str
    metrics: dict[str, float | int | bool]


class PdfIngestionService:
    """Page-level PDF triage and native extraction with durable typed page artifacts.

    Docling is deliberately kept behind the parser selection boundary. Its exact
    document-level adapter is added once its installed version is verified, so a
    guessed API cannot make the initial queue unstable.
    """

    chart_caption_pattern = re.compile(
        r"\b(chart|figure|trend|growth|forecast|comparison|revenue|inflation|percent|%)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        settings: Settings,
        database: Database,
        storage: LocalDocumentStorage,
        fact_extraction: FactExtractionService,
        entity_resolution: EntityResolutionService,
        relationship_service: RelationshipService,
    ) -> None:
        self.settings = settings
        self.database = database
        self.storage = storage
        self.fact_extraction = fact_extraction
        self.entity_resolution = entity_resolution
        self.relationship_service = relationship_service

    def process(self, job_id: str) -> None:
        job = self.database.get_job(job_id)
        if not job:
            raise DocumentProcessingError("The queued job no longer exists.")
        document = self.database.get_document(job["document_id"])
        if not document:
            raise DocumentProcessingError("The queued document no longer exists.")
        set_id = job.get("set_id")
        if not set_id:
            raise DocumentProcessingError("The document does not belong to a PDF set.")

        source_path = Path(document["stored_path"])
        if not source_path.is_file():
            raise DocumentProcessingError("The uploaded PDF is missing from local storage.")

        try:
            pdf = fitz.open(source_path)
        except (fitz.FileDataError, RuntimeError) as error:
            raise DocumentProcessingError("PyMuPDF could not open this PDF.") from error

        try:
            self._ensure_not_stopped(job_id)
            page_count = pdf.page_count
            if page_count <= 0:
                raise DocumentProcessingError("The PDF contains no pages.")
            if page_count > self.settings.max_pages:
                raise DocumentProcessingError(
                    f"The PDF has {page_count} pages, above the configured {self.settings.max_pages}-page limit."
                )

            self.database.set_document_page_count(document["id"], page_count)
            self.database.update_job(
                job_id,
                stage=PipelineStage.TRIAGING,
                progress_current=0,
                progress_total=page_count,
                progress_detail=f"Inspecting {page_count} pages",
            )
            existing_pages = {page["page_number"]: page for page in self.database.list_pages(document["id"])}

            for page_index in range(page_count):
                self._ensure_not_stopped(job_id)
                page_number = page_index + 1
                unit = self.database.create_work_unit(
                    job_id=job_id,
                    document_id=document["id"],
                    unit_type="page_extraction",
                    unit_key=f"page:{document['sha256']}:{page_number}:v1:set:{set_id}",
                    max_attempts=self.settings.gemini_max_automatic_attempts,
                )
                if unit["status"] == WorkUnitStatus.COMPLETED.value:
                    self.database.update_job(
                        job_id,
                        progress_current=page_number,
                        progress_total=page_count,
                        progress_detail=f"Reused cached page {page_number} of {page_count}",
                    )
                    continue
                cached_page = existing_pages.get(page_number)
                if (
                    cached_page
                    and cached_page["status"] == "completed"
                    and cached_page["cache_path"]
                    and Path(cached_page["cache_path"]).is_file()
                ):
                    self.database.update_work_unit(
                        unit["id"],
                        status=WorkUnitStatus.COMPLETED,
                        progress_detail=f"Reused parsed page {page_number} of {page_count}",
                        cache_path=cached_page["cache_path"],
                    )
                    self.database.update_job(
                        job_id,
                        progress_current=page_number,
                        progress_total=page_count,
                        progress_detail=f"Reused cached page {page_number} of {page_count}",
                    )
                    continue

                self.database.update_work_unit(
                    unit["id"],
                    status=WorkUnitStatus.RUNNING,
                    progress_detail=f"Extracting page {page_number} of {page_count}",
                    increment_attempts=True,
                )
                self.database.update_job(
                    job_id,
                    stage=PipelineStage.PARSING,
                    progress_current=page_number - 1,
                    progress_total=page_count,
                    progress_detail=f"Parsing page {page_number} of {page_count}",
                )
                try:
                    artifact = self._extract_page(pdf[page_index])
                    cache_path = self.storage.page_cache_path(document["sha256"], page_number)
                    cache_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
                    self._render_page(pdf[page_index], document["sha256"], page_number)
                    self.database.create_or_update_page(
                        document_id=document["id"],
                        page_number=page_number,
                        triage_result=artifact.triage_result,
                        parser_used=artifact.parser_used,
                        cache_path=str(cache_path),
                        status="completed",
                    )
                    self.database.update_work_unit(
                        unit["id"],
                        status=WorkUnitStatus.COMPLETED,
                        progress_detail=f"Parsed page {page_number} of {page_count}",
                        cache_path=str(cache_path),
                    )
                    self._register_skipped_visual_units(
                        job_id=job_id,
                        document_id=document["id"],
                        set_id=set_id,
                        document_hash=document["sha256"],
                        page_number=page_number,
                        visuals=artifact.visuals,
                    )
                except Exception as error:  # one bad page should not stop the whole document
                    self.database.create_or_update_page(
                        document_id=document["id"], page_number=page_number, status="failed"
                    )
                    self.database.update_work_unit(
                        unit["id"],
                        status=WorkUnitStatus.RETRYABLE_FAILED,
                        progress_detail=f"Page {page_number} could not be parsed",
                        error={"type": type(error).__name__, "message": str(error)},
                    )
                    self.database.append_history(
                        "page_extraction_failed",
                        unit["id"],
                        {"page_number": page_number, "message": str(error)},
                        set_id=set_id,
                    )
                finally:
                    self.database.update_job(
                        job_id,
                        progress_current=page_number,
                        progress_total=page_count,
                        progress_detail=f"Processed page {page_number} of {page_count}",
                    )

            failed_pages = [
                unit
                for unit in self.database.list_work_units(job_id)
                if unit["unit_type"] == "page_extraction"
                and unit["status"] in {
                    WorkUnitStatus.RETRYABLE_FAILED.value,
                    WorkUnitStatus.TERMINAL_FAILED.value,
                }
            ]
            if not self.settings.gemini_is_configured:
                self.database.complete_job(
                    job_id,
                    with_issues=True,
                    detail="PDF pages are parsed. Configure Gemini in .env to enable fact extraction.",
                )
            else:
                self._ensure_not_stopped(job_id)
                extracted_count, failed_batches = self.fact_extraction.extract_for_document(
                    job_id, document, set_id
                )
                self._ensure_not_stopped(job_id)
                self.database.update_job(
                    job_id,
                    stage=PipelineStage.RESOLVING_ENTITIES,
                    progress_detail="Resolving entity mentions across documents.",
                )
                entity_report = self.entity_resolution.resolve_document(document["id"], set_id)
                self._ensure_not_stopped(job_id)
                self.database.update_job(
                    job_id,
                    stage=PipelineStage.COMPARING_FACTS,
                    progress_detail="Comparing viable cross-document fact candidates.",
                )
                relationship_report = self.relationship_service.compare_document(job_id, document["id"], set_id)
                failed_relationships = [
                    unit
                    for unit in self.database.list_work_units(job_id)
                    if unit["unit_type"] == "relationship_classification"
                    and unit["status"] in {
                        WorkUnitStatus.RETRYABLE_FAILED.value,
                        WorkUnitStatus.TERMINAL_FAILED.value,
                    }
                ]
                with_issues = bool(failed_pages or failed_batches or failed_relationships)
                self.database.complete_job(
                    job_id,
                    with_issues=with_issues,
                    detail=(
                        (
                            f"Extracted {extracted_count} fact(s), resolved {entity_report['assigned']} entity "
                            f"mention(s), and created {relationship_report['relationships']} relationship(s)."
                        )
                        if not with_issues
                        else (
                            f"Extracted {extracted_count} fact(s) with {len(failed_pages)} failed page(s) "
                            f"and {failed_batches} failed fact batch(es), plus "
                            f"{len(failed_relationships)} failed relationship classification(s)."
                        )
                    ),
                )
        finally:
            pdf.close()

    def _ensure_not_stopped(self, job_id: str) -> None:
        if self.database.job_should_stop(job_id):
            raise ProcessingStopped("Processing was stopped by the user.")

    def _extract_page(self, page: fitz.Page) -> PageArtifact:
        raw_text = page.get_text("text").strip()
        blocks = self._text_blocks(page)
        visuals = self._visuals(page, raw_text)
        triage = self._triage(page, raw_text, blocks, visuals)
        # The initial native parser is reliable for simple pages. Complex pages retain
        # their triage signal and will route through the Docling adapter next.
        parser = "pymupdf_native" if triage.parser == "pymupdf4llm" else "docling_pending"
        return PageArtifact(
            page_number=page.number + 1,
            page_width=page.rect.width,
            page_height=page.rect.height,
            triage_result=triage.result,
            parser_used=parser,
            extracted_text=raw_text,
            blocks=blocks,
            visuals=visuals,
            metrics=triage.metrics,
        )

    @staticmethod
    def _text_blocks(page: fitz.Page) -> list[PageBlock]:
        blocks: list[PageBlock] = []
        for block in page.get_text("blocks"):
            x0, y0, x1, y1, text, block_no, block_type = block[:7]
            if block_type != 0:
                continue
            cleaned = " ".join(text.split())
            if cleaned:
                blocks.append(PageBlock(kind="text", text=cleaned, bbox=[x0, y0, x1, y1]))
        return blocks

    def _visuals(self, page: fitz.Page, raw_text: str) -> list[PageVisual]:
        visuals: list[PageVisual] = []
        for index, image in enumerate(page.get_images(full=True)):
            xref = image[0]
            rectangles = page.get_image_rects(xref)
            for rectangle in rectangles:
                area_ratio = (rectangle.width * rectangle.height) / max(page.rect.get_area(), 1)
                candidate = area_ratio >= 0.08 or bool(self.chart_caption_pattern.search(raw_text))
                visuals.append(
                    PageVisual(
                        index=index,
                        bbox=[rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1],
                        candidate_reason="visual_area_or_caption" if candidate else None,
                        data_bearing_candidate=candidate,
                    )
                )
        return visuals

    @staticmethod
    def _triage(
        page: fitz.Page, raw_text: str, blocks: list[PageBlock], visuals: list[PageVisual]
    ) -> PageTriage:
        page_area = max(page.rect.get_area(), 1)
        visual_area = sum(
            max(0.0, (visual.bbox[2] - visual.bbox[0]) * (visual.bbox[3] - visual.bbox[1]))
            for visual in visuals
        )
        text_density = len(raw_text) / (page_area / 1000)
        x_starts = {round(block.bbox[0] / max(page.rect.width, 1), 1) for block in blocks}
        likely_multi_column = len(blocks) >= 8 and len(x_starts) >= 3
        image_ratio = min(1.0, visual_area / page_area)
        is_complex = (
            len(raw_text) < 80
            or image_ratio >= 0.25
            or likely_multi_column
            or len(visuals) >= 3
        )
        metrics: dict[str, float | int | bool] = {
            "text_characters": len(raw_text),
            "text_density": round(text_density, 3),
            "text_blocks": len(blocks),
            "visual_count": len(visuals),
            "visual_area_ratio": round(image_ratio, 3),
            "likely_multi_column": likely_multi_column,
        }
        if is_complex:
            return PageTriage("complex", "docling", metrics)
        return PageTriage("simple", "pymupdf4llm", metrics)

    def _register_skipped_visual_units(
        self,
        *,
        job_id: str,
        document_id: str,
        set_id: str,
        document_hash: str,
        page_number: int,
        visuals: list[PageVisual],
    ) -> None:
        for visual in visuals:
            if visual.data_bearing_candidate:
                continue
            unit = self.database.create_work_unit(
                job_id=job_id,
                document_id=document_id,
                unit_type="chart_extraction",
                unit_key=f"visual:{document_hash}:{page_number}:{visual.index}:v1:set:{set_id}",
                max_attempts=self.settings.gemini_max_automatic_attempts,
            )
            if unit["status"] == WorkUnitStatus.QUEUED.value:
                self.database.update_work_unit(
                    unit["id"],
                    status=WorkUnitStatus.SKIPPED,
                    skip_reason="non_data_visual",
                    progress_detail="Visual was classified as decorative or non-data-bearing.",
                )
                self.database.append_history(
                    "visual_skipped",
                    unit["id"],
                    {"page_number": page_number, "reason": "non_data_visual"},
                    set_id=set_id,
                )

    def _render_page(self, page: fitz.Page, document_hash: str, page_number: int) -> None:
        output = self.storage.page_render_path(document_hash, page_number)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        pixmap.save(output)
