"""Local correction retrieval with conservative, evidence-aware reuse."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from rapidfuzz.fuzz import token_set_ratio


def _normalise(value: object) -> str:
    """Make comparison text stable without discarding the human-readable value."""
    return " ".join(str(value or "").casefold().split())


def fact_context(fact: dict[str, Any]) -> dict[str, str]:
    fields = ("claim_text", "subject", "predicate", "raw_value", "normalized_value",
              "unit_or_currency", "time_period", "scope")
    context = {key: _normalise(fact.get(key)) for key in fields}
    context["object_value"] = _normalise(
        fact.get("object_value") or fact.get("object_or_value")
    )
    context["quote"] = _normalise(fact.get("evidence_quote"))
    return context


def pair_context(left: dict, right: dict) -> dict:
    return {"facts": sorted([fact_context(left), fact_context(right)], key=lambda x: json.dumps(x, sort_keys=True))}


def fingerprint(context: dict) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def _comparison_text(context: dict[str, Any]) -> str:
    """Extract semantic content, not JSON punctuation and field names, for fuzzy recall."""
    if "facts" in context and isinstance(context["facts"], list):
        return " ".join(_comparison_text(item) for item in context["facts"] if isinstance(item, dict))
    return " ".join(
        _normalise(value)
        for key, value in context.items()
        if key not in {"id", "document_id", "set_id", "page_id"} and value is not None
    )


def _memory_payload(item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Keep prompt hints small and inspectable; never expose database-only fields."""
    return {
        "id": item["id"],
        "field": item["field_path"],
        "before": item["previous_value"],
        "after": item["corrected_value"],
        "reason": item["note"],
        "context": context,
    }


class CorrectionMemory:
    def __init__(self, database):
        self.database = database

    def retrieve(self, kind: str, context: dict, *, exact: bool = False) -> list[dict]:
        with self.database.connection() as conn:
            if exact:
                rows = conn.execute("SELECT * FROM corrections WHERE target_type=? AND exact_fingerprint=? ORDER BY created_at DESC", (kind, fingerprint(context))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM corrections WHERE target_type=? AND semantic_context LIKE '{%' ORDER BY created_at DESC LIMIT 250", (kind,)).fetchall()
        query = _comparison_text(context)
        candidates = []
        for row in rows:
            item = dict(row)
            try:
                saved = json.loads(item["semantic_context"])
            except (ValueError, TypeError):
                continue
            score = token_set_ratio(query, _comparison_text(saved))
            if exact or score >= 65:
                candidates.append((score, _memory_payload(item, saved)))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in candidates[:5]]

    def fact_hints_for_source(self, source_text: str) -> list[dict[str, Any]]:
        """Find only well-overlapping past corrections for a new source batch.

        This is intentionally prompt guidance rather than an automatic rewrite: a
        newly extracted fact still has to pass local quote validation, and direct
        reuse happens only via the exact fingerprint path in ``apply_facts``.
        """
        query = _normalise(source_text)
        if not query:
            return []
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM corrections
                WHERE target_type = 'fact' AND semantic_context LIKE '{%'
                ORDER BY created_at DESC LIMIT 100
                """
            ).fetchall()
        matches: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            try:
                saved = json.loads(item["semantic_context"])
            except (TypeError, ValueError):
                continue
            score = token_set_ratio(query, _comparison_text(saved))
            # A source batch is much broader than one saved fact. Keep this high
            # enough that ordinary unrelated reports do not receive a correction.
            if score >= 72:
                matches.append((score, _memory_payload(item, saved)))
        matches.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in matches[:5]]

    def apply_facts(self, facts: list[dict]) -> list[dict]:
        output = []
        for original in facts:
            fact = dict(original)
            applied = []
            seen = set()
            for memory in self.retrieve("fact", fact_context(original), exact=True):
                field = memory["field"]
                target = "object_or_value" if field == "object_value" else field
                if field not in seen and str(fact.get(target) or "") == memory["before"]:
                    fact[target] = memory["after"]
                    seen.add(field)
                    applied.append(memory["id"])
            if applied:
                fact["qualifiers"] = {**fact.get("qualifiers", {}), "correction_memory_ids": applied}
            output.append(fact)
        return output
