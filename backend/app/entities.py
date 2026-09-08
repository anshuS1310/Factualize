from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from rapidfuzz import fuzz

from .database import Database


def normalize_entity_name(value: str) -> str:
    """Generic comparison normalization; it contains no document-specific aliases."""
    normalized = value.casefold().replace("&", " and ")
    normalized = re.sub(r"\b(incorporated|inc|limited|ltd|llc|plc|corp|corporation|company|co)\b", "", normalized)
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    return " ".join(normalized.split())


@dataclass(frozen=True)
class EntityMatch:
    entity_id: str | None
    confidence: float
    method: str


class EntityResolver(Protocol):
    def match(self, mention: str, entities: list[dict[str, Any]]) -> EntityMatch: ...


class ConservativeSimilarityResolver:
    """High-precision fallback used while the optional Splink adapter is evaluated."""

    exact_confidence = 1.0
    automatic_merge_threshold = 94.0

    def match(self, mention: str, entities: list[dict[str, Any]]) -> EntityMatch:
        normalized = normalize_entity_name(mention)
        if not normalized:
            return EntityMatch(None, 0.0, "empty")
        best: tuple[float, str] | None = None
        for entity in entities:
            candidate = normalize_entity_name(entity["canonical_name"])
            if normalized == candidate:
                return EntityMatch(entity["id"], self.exact_confidence, "normalized_exact")
            score = max(fuzz.ratio(normalized, candidate), fuzz.token_set_ratio(normalized, candidate))
            if best is None or score > best[0]:
                best = (score, entity["id"])
        if best and best[0] >= self.automatic_merge_threshold:
            return EntityMatch(best[1], round(best[0] / 100, 3), "conservative_similarity")
        return EntityMatch(None, round((best[0] if best else 0) / 100, 3), "new_entity")


class EntityResolutionService:
    """Assigns current facts to entities without mixing entity matching with claim similarity."""

    def __init__(self, database: Database, resolver: EntityResolver | None = None) -> None:
        self.database = database
        self.resolver = resolver or ConservativeSimilarityResolver()

    def resolve_document(self, document_id: str, set_id: str) -> dict[str, int]:
        facts = self.database.list_facts(document_id=document_id, set_id=set_id)
        entities = self.database.list_entities(set_id=set_id)
        assigned = 0
        created = 0
        uncertain = 0
        for fact in facts:
            if fact["primary_entity_id"]:
                continue
            mention = (fact["primary_entity_mention"] or fact["subject"] or "").strip()
            if not mention:
                uncertain += 1
                continue
            match = self.resolver.match(mention, entities)
            if match.entity_id:
                entity_id = match.entity_id
            else:
                entity = self.database.create_entity(mention, set_id=set_id)
                entities.append(entity)
                entity_id = entity["id"]
                created += 1
                if match.confidence > 0:
                    uncertain += 1
            self.database.assign_fact_entity(fact["id"], entity_id)
            assigned += 1
        report = {"assigned": assigned, "created": created, "uncertain": uncertain}
        self.database.append_history("entity_resolution_completed", document_id, report, set_id=set_id)
        return report
