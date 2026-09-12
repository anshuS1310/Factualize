from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import numpy as np
from rapidfuzz import fuzz

from .correction_memory import CorrectionMemory, pair_context
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
    matching_mode: str


class LocalEmbeddingService:
    """Persist embeddings on fact rows; similarity remains local and quota-free."""

    model_name = "all-MiniLM-L6-v2"

    def __init__(self, database: Database) -> None:
        self.database = database
        self._model: Any = None
        self._unavailable_reason: str | None = None

    def embedding_for_fact(self, fact: dict[str, Any]) -> np.ndarray:
        existing = parse_json(fact.get("embedding_json"), None)
        if existing:
            return np.asarray(existing, dtype=np.float32)
        if self._model is None:
            from sentence_transformers import SentenceTransformer
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

    def similarity(self, left: dict[str, Any], right: dict[str, Any]) -> tuple[float, str]:
        """Return a local candidate score without making processing depend on a model download.

        The sentence-transformer is preferred and remains the normal path.  If a
        first-run model download or a local runtime fails, lexical overlap is a
        deliberately conservative alternative: it only decides which pairs are
        worth reviewing, never the truth of a fact or relationship.
        """
        if self._unavailable_reason is None:
            try:
                left_embedding = self.embedding_for_fact(left)
                right_embedding = self.embedding_for_fact(right)
                return float(np.dot(left_embedding, right_embedding)), "sentence_transformer"
            except Exception as error:  # Candidate discovery remains available offline.
                self._unavailable_reason = f"{type(error).__name__}: {error}"
        return self._lexical_similarity(left, right), "local_lexical_fallback"

    @staticmethod
    def _lexical_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
        def material(fact: dict[str, Any]) -> str:
            return " ".join(
                str(fact.get(field) or "")
                for field in ("subject", "predicate", "claim_text")
            ).strip()

        return fuzz.token_set_ratio(material(left), material(right)) / 100


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
        # Similar embeddings do not establish the same metric. Missing contexts
        # and merely different periods are not proof of reconciliation.
        if not all(left.get(key) and right.get(key) for key in ("subject", "predicate", "time_period", "scope", "unit_or_currency")):
            return None, context
        if self._norm(left.get("subject")) != self._norm(right.get("subject")):
            return None, context
        if self._norm(left.get("predicate")) != self._norm(right.get("predicate")) or not same_period or not same_scope:
            return None, context
        if left_value is not None and right_value is not None and same_unit:
            tolerance = max(abs(left_value), abs(right_value), 1) * 0.005
            if math.isclose(left_value, right_value, abs_tol=tolerance):
                return RelationshipLabel.CORROBORATES, context
            return RelationshipLabel.CONTRADICTS, context
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
                trace: dict[str, Any] = {
                    "similarity": candidate.similarity,
                    "candidate_matching_mode": candidate.matching_mode,
                    "mode": "deterministic",
                }
                memories = CorrectionMemory(self.database).retrieve("relationship", pair_context(left, right), exact=True)
                try:
                    if memories:
                        decision = RelationshipLabel(memories[0]["after"])
                        explanation = memories[0]["reason"] or "Reused a human correction for an exactly matching fact pair."
                        confidence = 1.0
                        trace = {"mode": "correction_memory", "correction_id": memories[0]["id"]}
                    elif decision is None and (
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
                        trace = {
                            "similarity": candidate.similarity,
                            "candidate_matching_mode": candidate.matching_mode,
                            "mode": "local_safe_fallback",
                            "reason": reason,
                        }
                    elif decision is None:
                        try:
                            gemini_calls += 1
                            decision_data = self.gateway.classify_relationship(left, right, context)
                            decision = decision_data.label
                            explanation = decision_data.explanation
                            confidence = decision_data.confidence
                            trace = {
                                "similarity": candidate.similarity,
                                "candidate_matching_mode": candidate.matching_mode,
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
                            trace = {
                                "similarity": candidate.similarity,
                                "candidate_matching_mode": candidate.matching_mode,
                                "mode": "local_safe_fallback",
                                "reason": "provider quota exhausted",
                            }
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
                        set_id=set_id,
                    )
        report = {"candidates": candidate_count, "relationships": created}
        self.database.append_history("relationship_comparison_completed", document_id, report, set_id=set_id)
        return report

    @staticmethod
    def _is_quota_error(error: RuntimeError) -> bool:
        message = str(error).casefold()
        return "resource_exhausted" in message or "quota" in message or " 429" in message

    def revise(self, relationship_id: str, *, label: str | None = None, note: str | None = None):
        with self.database.connection() as conn:
            stored = conn.execute("SELECT * FROM relationships WHERE id=? AND is_current=1", (relationship_id,)).fetchone()
            if not stored:
                raise KeyError("Current comparison not found")
            old = dict(stored)
            facts = []
            for key in ("left_fact_id", "right_fact_id"):
                row = conn.execute("SELECT new.* FROM facts old JOIN facts new ON old.stable_id=new.stable_id WHERE old.id=? AND new.is_current=1", (old[key],)).fetchone()
                if not row:
                    raise ValueError("A source fact is no longer available.")
                facts.append(dict(row))
        left, right = facts
        for fact in facts:
            fact["evidence"] = self.database.fact_evidence(fact["id"])
        decision, context = self.comparator.compare(left, right)
        memories = CorrectionMemory(self.database).retrieve("relationship", pair_context(left, right), exact=True)
        trace = {"mode": "deterministic"}
        confidence = 0.9
        if label is not None:
            if old["stale"]:
                raise ValueError("Recheck the changed sources before correcting this conclusion.")
            decision = RelationshipLabel(label)
            explanation = note or "Human reviewed this conclusion."
            trace = {"mode": "human_correction"}
            confidence = 1.0
        elif memories:
            decision = RelationshipLabel(memories[0]["after"])
            explanation = memories[0]["reason"] or "Exact prior human correction."
            trace = {"mode": "correction_memory", "correction_id": memories[0]["id"]}
        elif decision is None:
            try:
                result = self.gateway.classify_relationship(left, right, context)
                decision, explanation, confidence = result.label, result.explanation, result.confidence
                trace = {"mode": "gemini", "model": result.model}
            except RuntimeError as error:
                self.database.append_history("relationship_recheck_failed", relationship_id, {"message": str(error), "relationship_id": relationship_id}, set_id=old["set_id"])
                raise
        else:
            explanation = self._deterministic_explanation(decision, context)
        return self.database.persist_relationship(left_fact_id=left["id"], right_fact_id=right["id"], set_id=old["set_id"], label=decision.value, explanation=explanation, confidence=confidence, deterministic_context=context, reasoning_trace=trace, entity_resolution_revision=old["entity_resolution_revision"], replaces_id=relationship_id, correction_note=note if label is not None else None)

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
                similarity, matching_mode = self.embedder.similarity(left, right)
                if similarity >= self.similarity_threshold:
                    pairs.append(CandidatePair(left, right, similarity, matching_mode))
        return sorted(pairs, key=lambda candidate: candidate.similarity, reverse=True)

    @staticmethod
    def _deterministic_explanation(label: RelationshipLabel, context: dict[str, Any]) -> str:
        if label == RelationshipLabel.CORROBORATES:
            return "The normalized values agree within tolerance under the same unit context."
        if label == RelationshipLabel.CONTRADICTS:
            return "The normalized values differ while the extracted period, scope, and unit context match."
        return "The values differ, but the extracted period or scope differs, so context explains the apparent conflict."
