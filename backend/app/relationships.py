from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

import numpy as np
from sentence_transformers import SentenceTransformer

from .database import Database, parse_json
from .errors import ProcessingStopped
from .gemini import GeminiConfigurationError, GeminiGateway, GeminiRelationshipError
from .schemas import RelationshipLabel, WorkUnitStatus
from .settings import Settings


@dataclass(frozen=True)
class CandidatePair:
    left: dict[str, Any]
    right: dict[str, Any]
    similarity: float


class LocalEmbeddingService:
    """Persist embeddings on fact rows; similarity remains local and quota-free."""

    model_name = "all-MiniLM-L6-v2"

    def __init__(self, database: Database) -> None:
        self.database = database
        self._model: SentenceTransformer | None = None

    def embedding_for_fact(self, fact: dict[str, Any]) -> np.ndarray:
        existing = parse_json(fact.get("embedding_json"), None)
        if existing:
            return np.asarray(existing, dtype=np.float32)
        if self._model is None:
            self._model = SentenceTransformer(self.model_name)
        claim = " | ".join(
            part
            for part in [
                fact.get("subject"),
                fact.get("predicate"),
                fact.get("claim_text"),
            ]
            if part
        )
        embedding = self._model.encode(claim, normalize_embeddings=True)
        serializable = np.asarray(embedding, dtype=np.float32).tolist()
        self.database.update_fact_embedding(fact["id"], serializable)
        return np.asarray(serializable, dtype=np.float32)


class DeterministicComparator:
    number_pattern = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
    scale_factors = {
        "thousand": 1_000,
        "lakh": 100_000,
        "lac": 100_000,
        "million": 1_000_000,
        "crore": 10_000_000,
        "billion": 1_000_000_000,
    }

    def compare(self, left: dict[str, Any], right: dict[str, Any]) -> tuple[RelationshipLabel | None, dict[str, Any]]:
        left_value = self._numeric_value(left)
        right_value = self._numeric_value(right)
        same_period = self._norm(left.get("time_period")) == self._norm(right.get("time_period"))
        same_scope = self._norm(left.get("scope")) == self._norm(right.get("scope"))
        same_unit = self._norm(left.get("unit_or_currency")) == self._norm(right.get("unit_or_currency"))
        context: dict[str, Any] = {
            "left_numeric_value": left_value,
            "right_numeric_value": right_value,
            "same_period": same_period,
            "same_scope": same_scope,
            "same_unit": same_unit,
        }
        if left_value is not None and right_value is not None and same_unit:
            tolerance = max(abs(left_value), abs(right_value), 1) * 0.005
            if math.isclose(left_value, right_value, abs_tol=tolerance):
                return RelationshipLabel.CORROBORATES, context
            if not same_period or not same_scope:
                return RelationshipLabel.RECONCILED, context
            return RelationshipLabel.CONTRADICTS, context
        if not same_period or not same_scope:
            context["context_mismatch"] = True
        return None, context

    def _numeric_value(self, fact: dict[str, Any]) -> float | None:
        value = fact.get("normalized_value") or fact.get("raw_value") or fact.get("object_value")
        if not value:
            return None
        match = self.number_pattern.search(str(value))
        if not match:
            return None
        numeric = float(match.group(0).replace(",", ""))
        normalized = str(value).casefold()
        for word, factor in self.scale_factors.items():
            if word in normalized:
                return numeric * factor
        return numeric

    @staticmethod
    def _norm(value: str | None) -> str:
        return " ".join((value or "").casefold().split())


class RelationshipService:
    similarity_threshold = 0.72
    max_candidates_per_entity = 100

    def __init__(self, settings: Settings, database: Database, gateway: GeminiGateway) -> None:
        self.settings = settings
        self.database = database
        self.gateway = gateway
        self.embedder = LocalEmbeddingService(database)
        self.comparator = DeterministicComparator()

    def compare_document(self, job_id: str, document_id: str, set_id: str) -> dict[str, int]:
        created = 0
        candidate_count = 0
        gemini_calls = 0
        quota_exhausted = False
        new_facts = [
            fact
            for fact in self.database.list_facts(document_id=document_id, set_id=set_id)
            if fact["primary_entity_id"]
        ]
        entity_ids = {fact["primary_entity_id"] for fact in new_facts}
        for entity_id in entity_ids:
            if self.database.job_should_stop(job_id):
                raise ProcessingStopped("Processing was stopped by the user.")
            all_facts = self.database.facts_for_entity(entity_id, set_id=set_id)
            candidates = self._candidate_pairs(all_facts, document_id)
            for candidate in candidates[: self.max_candidates_per_entity]:
                if self.database.job_should_stop(job_id):
                    raise ProcessingStopped("Processing was stopped by the user.")
                candidate_count += 1
                entity = self.database.entity_by_id(entity_id)
                resolution_revision = int(entity["resolution_revision"]) if entity else 1
                unit = self.database.create_work_unit(
                    job_id=job_id,
                    document_id=document_id,
                    unit_type="relationship_classification",
                    unit_key=self._candidate_key(candidate, resolution_revision),
                    max_attempts=self.settings.gemini_max_automatic_attempts,
                )
                if unit["status"] == WorkUnitStatus.COMPLETED.value:
                    continue
                self.database.update_work_unit(
                    unit["id"],
                    status=WorkUnitStatus.RUNNING,
                    progress_detail="Classifying a cross-document fact pair.",
                    increment_attempts=True,
                )
                left = {**candidate.left, "evidence": self.database.fact_evidence(candidate.left["id"])}
                right = {**candidate.right, "evidence": self.database.fact_evidence(candidate.right["id"])}
                decision, context = self.comparator.compare(left, right)
                trace: dict[str, Any] = {"similarity": candidate.similarity, "mode": "deterministic"}
                try:
                    if decision is None and (
                        quota_exhausted
                        or gemini_calls >= self.settings.relationship_gemini_call_budget
                    ):
                        decision = RelationshipLabel.INSUFFICIENT_EVIDENCE
                        reason = "provider quota was exhausted" if quota_exhausted else "the local reasoning budget was reached"
                        explanation = (
                            "A semantically similar cross-document fact was found, but "
                            f"{reason}; no stronger conclusion is claimed."
                        )
                        confidence = 0.65
                        trace = {"similarity": candidate.similarity, "mode": "local_safe_fallback", "reason": reason}
                    elif decision is None:
                        try:
                            gemini_calls += 1
                            decision_data = self.gateway.classify_relationship(left, right, context)
                            decision = decision_data.label
                            explanation = decision_data.explanation
                            confidence = decision_data.confidence
                            trace = {
                                "similarity": candidate.similarity,
                                "mode": "gemini",
                                "model": decision_data.model,
                            }
                        except RuntimeError as error:
                            if not self._is_quota_error(error):
                                raise
                            quota_exhausted = True
                            decision = RelationshipLabel.INSUFFICIENT_EVIDENCE
                            explanation = (
                                "A semantically similar cross-document fact was found, but the reasoning "
                                "model quota is unavailable; no stronger conclusion is claimed."
                            )
                            confidence = 0.65
                            trace = {"similarity": candidate.similarity, "mode": "local_safe_fallback", "reason": "provider quota exhausted"}
                    else:
                        explanation = self._deterministic_explanation(decision, context)
                        confidence = 0.97 if decision == RelationshipLabel.CORROBORATES else 0.9
                    self.database.persist_relationship(
                        left_fact_id=candidate.left["id"],
                        right_fact_id=candidate.right["id"],
                        set_id=set_id,
                        label=decision.value,
                        explanation=explanation,
                        confidence=confidence,
                        deterministic_context=context,
                        reasoning_trace=trace,
                        entity_resolution_revision=resolution_revision,
                        work_unit_id=unit["id"],
                    )
                    created += 1
                except (GeminiRelationshipError, GeminiConfigurationError, OSError, RuntimeError) as error:
                    self.database.update_work_unit(
                        unit["id"],
                        status=WorkUnitStatus.RETRYABLE_FAILED,
                        progress_detail="Relationship classification needs a manual retry.",
                        error={"type": type(error).__name__, "message": str(error)},
                    )
                    self.database.append_history(
                        "relationship_classification_failed",
                        unit["id"],
                        {"left_fact_id": left["id"], "right_fact_id": right["id"], "message": str(error)},
                    )
        report = {"candidates": candidate_count, "relationships": created}
        self.database.append_history("relationship_comparison_completed", document_id, report, set_id=set_id)
        return report

    @staticmethod
    def _is_quota_error(error: RuntimeError) -> bool:
        message = str(error).casefold()
        return "resource_exhausted" in message or "quota exceeded" in message or " 429" in message

    @staticmethod
    def _candidate_key(candidate: CandidatePair, entity_revision: int) -> str:
        left, right = sorted([candidate.left["id"], candidate.right["id"]])
        return f"relationship:{left}:{right}:entity-v{entity_revision}:v1"

    def _candidate_pairs(self, facts: list[dict[str, Any]], new_document_id: str) -> list[CandidatePair]:
        pairs: list[CandidatePair] = []
        for index, left in enumerate(facts):
            for right in facts[index + 1 :]:
                if left["document_id"] == right["document_id"]:
                    continue
                if left["document_id"] != new_document_id and right["document_id"] != new_document_id:
                    continue
                left_embedding = self.embedder.embedding_for_fact(left)
                right_embedding = self.embedder.embedding_for_fact(right)
                similarity = float(np.dot(left_embedding, right_embedding))
                if similarity >= self.similarity_threshold:
                    pairs.append(CandidatePair(left, right, similarity))
        return sorted(pairs, key=lambda candidate: candidate.similarity, reverse=True)

    @staticmethod
    def _deterministic_explanation(label: RelationshipLabel, context: dict[str, Any]) -> str:
        if label == RelationshipLabel.CORROBORATES:
            return "The normalized values agree within tolerance under the same unit context."
        if label == RelationshipLabel.CONTRADICTS:
            return "The normalized values differ while the extracted period, scope, and unit context match."
        return "The values differ, but the extracted period or scope differs, so context explains the apparent conflict."
