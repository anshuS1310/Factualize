import { useEffect, useMemo, useState } from "react";

import {
  HistoryEvent,
  RelationshipDetail,
  RelationshipSummary,
  correctRelationship,
  getRelationship,
  listHistory,
  listRelationships,
  recheckRelationship,
} from "./api";

type RelationshipLabel = RelationshipSummary["label"];

const relationshipNames: Record<RelationshipLabel, string> = {
  corroborates: "Reports agree",
  contradicts: "Reports disagree",
  reconciled: "Difference explained",
  insufficient_evidence: "Not enough evidence",
  not_comparable: "Different claims",
};

const demonstrationCategories: RelationshipLabel[] = [
  "corroborates",
  "contradicts",
  "reconciled",
  "insufficient_evidence",
];

const eventNames: Record<string, string> = {
  document_uploaded: "Report added",
  document_processing_started: "Reading started",
  document_processing_completed: "Reading finished",
  document_processing_failed: "Reading needs attention",
  document_processing_stopped: "Reading paused",
  fact_batch_completed: "Findings saved",
  fact_batch_failed: "Finding batch needs attention",
  entity_resolution_completed: "Names connected",
  relationship_classified: "Comparison saved",
  relationship_comparison_completed: "Comparison pass finished",
  relationship_classification_failed: "Comparison needs attention",
  relationship_rechecked: "Comparison rechecked",
  relationship_recheck_failed: "Recheck needs attention",
  relationship_corrected: "Comparison corrected",
  fact_corrected: "Finding corrected",
  entities_merged: "Names merged",
  visual_skipped: "Visual skipped",
  visual_needs_review: "Visual may need review",
};

const snapshotNames: Record<string, string> = {
  field_path: "Changed part",
  previous_value: "Before",
  corrected_value: "After",
  note: "Review note",
  message: "What happened",
  fact_count: "Findings saved",
  candidates: "Possible matches checked",
  relationships: "Comparisons saved",
  label: "Conclusion",
  explanation: "Why",
  confidence: "Confidence",
  reason: "Reason",
};

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}

function snapshotText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.length ? `${value.length} saved item${value.length === 1 ? "" : "s"}` : "None";
  if (typeof value === "object") {
    const mode = (value as Record<string, unknown>).mode;
    return mode ? `Checked using ${String(mode).replaceAll("_", " ")}` : "Saved details";
  }
  return String(value);
}

function timelineSummary(event: HistoryEvent): string | null {
  const snapshot = event.snapshot;
  if (event.event_type === "fact_corrected") {
    return `Updated ${snapshotText(snapshot.field_path)} from “${snapshotText(snapshot.previous_value)}” to “${snapshotText(snapshot.corrected_value)}”.`;
  }
  if (event.event_type === "fact_batch_completed") {
    return `${snapshotText(snapshot.fact_count)} evidence-backed finding(s) were saved.`;
  }
  if (["relationship_classified", "relationship_rechecked", "relationship_corrected"].includes(event.event_type)) {
    return snapshot.explanation ? String(snapshot.explanation) : null;
  }
  if (snapshot.message) return String(snapshot.message);
  return null;
}

function factButtonLabel(key: string): string {
  if (key === "previous_fact_id") return "Open earlier finding";
  if (key === "current_fact_id") return "Open corrected finding";
  if (key === "left_fact_id") return "Open first source";
  return "Open second source";
}

export function Review({
  setId,
  refreshKey,
  onFact,
  onChanged,
}: {
  setId: string | null;
  refreshKey: number;
  onFact: (id: string) => void;
  onChanged: () => void;
}) {
  const [rows, setRows] = useState<RelationshipSummary[]>([]);
  const [detail, setDetail] = useState<RelationshipDetail | null>(null);
  const [filter, setFilter] = useState<"all" | "stale" | RelationshipLabel>("all");
  const [label, setLabel] = useState<RelationshipLabel>("corroborates");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    setDetail(null);
    setError("");
  }, [setId]);

  useEffect(() => {
    let active = true;
    if (!setId) {
      setRows([]);
      setIsLoading(false);
      return () => {
        active = false;
      };
    }
    setIsLoading(true);
    listRelationships(setId)
      .then((value) => {
        if (active) setRows(value);
      })
      .catch((requestError) => {
        if (active) setError(safeError(requestError));
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [setId, refreshKey]);

  const visibleRows = useMemo(
    () => rows.filter((row) => filter === "all" || (filter === "stale" ? row.stale : row.label === filter)),
    [filter, rows],
  );

  async function open(id: string) {
    try {
      setError("");
      const next = await getRelationship(id);
      setDetail(next);
      setLabel(next.relationship.label);
      setNote("");
    } catch (requestError) {
      setError(safeError(requestError));
    }
  }

  async function change(correction: boolean) {
    if (!detail || !setId) return;
    setBusy(true);
    setError("");
    try {
      const next = correction
        ? await correctRelationship(detail.relationship.id, label, note)
        : await recheckRelationship(detail.relationship.id);
      const nextRows = await listRelationships(setId);
      setRows(nextRows);
      await open(next.id);
      onChanged();
    } catch (requestError) {
      setError(safeError(requestError));
    } finally {
      setBusy(false);
    }
  }

  if (!setId) return <p className="data-empty">Open a PDF set to review comparisons.</p>;

  return (
    <section className="data-view">
      <div className="section-heading">
        <div><p className="eyebrow">Evidence across your reports</p><h2>Comparisons & demonstration</h2></div>
        <span className="count-pill">{rows.length} saved</span>
      </div>
      <p className="cross-check-intro">Each example comes from this set. A missing example stays visibly empty; it is never manufactured from test data.</p>

      <div className="demo-grid">
        {demonstrationCategories.map((category) => {
          const currentExample = rows.find((row) => row.label === category && !row.stale);
          const staleExample = rows.find((row) => row.label === category && row.stale);
          const example = currentExample ?? staleExample;
          return (
            <article className={`demo-card ${staleExample && !currentExample ? "needs-recheck" : ""}`} key={category}>
              <h3>{relationshipNames[category]}</h3>
              {example ? (
                <>
                  {example.stale && <span className="stale-badge">Source changed · needs recheck</span>}
                  <p>{example.explanation}</p>
                  <button className="soft-button" disabled={busy} onClick={() => void open(example.id)}>Review example</button>
                </>
              ) : <p>No real example in this set yet.</p>}
            </article>
          );
        })}
      </div>

      <label className="review-filter">Show <select value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}>
        <option value="all">All comparisons</option>
        <option value="stale">Needs recheck</option>
        {Object.entries(relationshipNames).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
      </select></label>

      {error && <p className="notice error" role="alert">{error}</p>}
      <div className={`review-layout ${detail ? "has-detail" : ""}`}>
        <div className="facts-list" aria-busy={isLoading}>
          {isLoading && rows.length === 0 && <p className="loading-inline">Loading comparisons…</p>}
          {visibleRows.map((row) => (
            <button className="comparison-row" key={row.id} disabled={busy} onClick={() => void open(row.id)}>
              <strong>{relationshipNames[row.label]}</strong>
              {row.stale && <span className="stale-badge">Sources changed · needs recheck</span>}
              <p>{row.explanation}</p>
              <small>Revision {row.revision} · {Math.round(row.confidence * 100)}% confidence</small>
            </button>
          ))}
          {!isLoading && rows.length === 0 && <p className="empty-inline">No comparisons yet. Processing notes, if any, appear in the activity timeline.</p>}
          {!isLoading && rows.length > 0 && visibleRows.length === 0 && <p className="empty-inline">No comparisons match this view.</p>}
        </div>

        {detail && <article className="comparison-detail">
          <h3>{relationshipNames[detail.relationship.label]}</h3>
          {detail.relationship.stale && <p className="notice error">This conclusion refers to an older source revision. Recheck before relying on it.</p>}
          <p>{detail.relationship.explanation}</p>
          {[detail.left, detail.right].map((fact, index) => (
            <section className="source-preview" key={`${fact.id}-${index}`}>
              <small>Source {index + 1} · finding revision {fact.revision}</small>
              <h4>{fact.claim_text}</h4>
              <p>{[fact.time_period, fact.scope, fact.unit_or_currency].filter(Boolean).join(" · ") || "Context not stated"}</p>
              {fact.evidence.map((anchor) => <blockquote key={anchor.id}>“{anchor.quote}” <small>— page {anchor.page_number}</small></blockquote>)}
              <button className="icon-button" onClick={() => onFact(fact.id)}>View source page</button>
            </section>
          ))}
          {detail.relationship.is_current && detail.relationship.stale && <button className="soft-button" disabled={busy} onClick={() => void change(false)}>{busy ? "Checking…" : "Recheck current sources"}</button>}
          {detail.relationship.is_current && !detail.relationship.stale && <div className="correction-panel">
            <h4>Correct this conclusion</h4>
            <label>Conclusion<select value={label} onChange={(event) => setLabel(event.target.value as RelationshipLabel)}>{Object.entries(relationshipNames).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
            <label>Why is this the right conclusion?<textarea value={note} onChange={(event) => setNote(event.target.value)} rows={3} /></label>
            <button className="primary-button" disabled={busy || note.trim().length < 10} onClick={() => void change(true)}>Save correction</button>
          </div>}
          <details>
            <summary>Previous conclusions ({detail.revisions.length})</summary>
            {detail.revisions.map((revision) => <button className="comparison-row" key={revision.id} onClick={() => void open(revision.id)}>Revision {revision.revision} · {relationshipNames[revision.label]}{revision.is_current ? " · current" : " · previous"}</button>)}
          </details>
        </article>}
      </div>
    </section>
  );
}

export function Timeline({
  setId,
  refreshKey,
  onFact,
}: {
  setId: string | null;
  refreshKey: number;
  onFact: (id: string) => void;
}) {
  const [events, setEvents] = useState<HistoryEvent[]>([]);
  const [error, setError] = useState("");
  const [limit, setLimit] = useState(100);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    setLimit(100);
  }, [setId]);

  useEffect(() => {
    let active = true;
    if (!setId) {
      setEvents([]);
      setIsLoading(false);
      return () => {
        active = false;
      };
    }
    setIsLoading(true);
    listHistory(setId, limit)
      .then((value) => {
        if (active) setEvents(value);
      })
      .catch((requestError) => {
        if (active) setError(safeError(requestError));
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [setId, limit, refreshKey]);

  const visibleEntries = events.map((event) => Object.entries(event.snapshot).filter(([key]) => (
    !key.endsWith("_id") && key !== "set_id" && key !== "reasoning_trace" && snapshotNames[key]
  )));

  return (
    <section className="data-view">
      <div className="section-heading">
        <div><p className="eyebrow">Selected set</p><h2>Activity & corrections</h2></div>
        {events.length >= limit && <button className="icon-button" disabled={isLoading} onClick={() => setLimit((value) => Math.min(500, value + 100))}>{isLoading ? "Loading…" : "Load more"}</button>}
      </div>
      <p className="cross-check-intro">A dated record of reading, comparisons, issues and human changes for this PDF set.</p>
      {error && <p className="notice error" role="alert">{error}</p>}
      {isLoading && events.length === 0 && <p className="loading-inline">Loading activity…</p>}
      {!isLoading && events.length === 0 && <p className="empty-inline">No recorded activity for this set.</p>}
      <ol className="timeline" aria-busy={isLoading}>
        {events.map((event, index) => {
          const summary = timelineSummary(event);
          const entries = visibleEntries[index];
          return <li key={event.id}><details>
            <summary><strong>{eventNames[event.event_type] ?? event.event_type.replaceAll("_", " ")}</strong><time>{new Date(event.created_at).toLocaleString()}</time></summary>
            {summary && <p className="timeline-summary">{summary}</p>}
            {Object.entries(event.snapshot).filter(([key]) => ["previous_fact_id", "current_fact_id", "left_fact_id", "right_fact_id"].includes(key)).map(([key, id]) => <button className="icon-button" key={key} onClick={() => onFact(String(id))}>{factButtonLabel(key)}</button>)}
            {entries.length > 0 && <dl>{entries.map(([key, value]) => <div key={key}><dt>{snapshotNames[key]}</dt><dd>{snapshotText(value)}</dd></div>)}</dl>}
          </details></li>;
        })}
      </ol>
    </section>
  );
}
