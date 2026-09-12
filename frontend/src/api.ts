export type JobStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_issues"
  | "needs_attention"
  | "terminal_failure";

export interface DocumentSummary {
  id: string;
  original_filename: string;
  sha256: string;
  page_count: number | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface JobProgress {
  id: string;
  document_id: string;
  set_id: string | null;
  status: JobStatus;
  stage: string;
  progress_current: number;
  progress_total: number;
  progress_detail: string | null;
  queue_position: number | null;
  last_error: { type: string; message: string } | null;
  created_at: string;
  updated_at: string;
}

export interface UploadResponse {
  document: DocumentSummary;
  job: JobProgress;
  reused_existing_document: boolean;
}

export interface DocumentSetSummary {
  id: string;
  name: string;
  status: "queued" | "processing" | "completed" | "completed_with_issues";
  document_count: number;
  created_at: string;
  updated_at: string;
}

export interface SetDocumentMember {
  document: DocumentSummary;
  job: JobProgress | null;
  position: number;
}

export interface DocumentSetDetail extends DocumentSetSummary {
  documents: SetDocumentMember[];
}

export interface SetUploadResponse {
  document_set: DocumentSetDetail;
  reused_document_count: number;
}

export interface DeleteResponse {
  deleted: boolean;
}

export interface EvidenceAnchor {
  id: string;
  page_number: number;
  source_kind: "text" | "table" | "chart";
  quote: string;
  bbox: number[] | null;
  confidence: number;
}

export interface FactSummary {
  id: string;
  stable_id: string;
  revision: number;
  claim_text: string;
  subject: string | null;
  predicate: string | null;
  object_value: string | null;
  time_period: string | null;
  confidence: number;
  primary_entity_mention: string | null;
  primary_entity_id: string | null;
  document_id: string;
  set_id: string | null;
  is_current: boolean;
  correction_state: string;
  cross_check_status: "stale" | "corroborated" | "reconciled" | "contradicted" | "insufficient_evidence" | "no_comparable_source";
  cross_check_explanation: string;
  cross_check_source_count: number;
}

export interface FactDetail extends FactSummary {
  raw_value: string | null;
  normalized_value: string | null;
  unit_or_currency: string | null;
  scope: string | null;
  qualifiers: Record<string, unknown>;
  additional_entity_mentions: string[];
  evidence: EvidenceAnchor[];
}

export interface RelationshipSummary {
  id: string;
  stable_id: string;
  revision: number;
  left_fact_id: string;
  right_fact_id: string;
  label: "corroborates" | "contradicts" | "reconciled" | "insufficient_evidence" | "not_comparable";
  explanation: string;
  confidence: number;
  stale: boolean;
  superseded: boolean;
  is_current: boolean;
  set_id: string | null;
}

export interface HistoryEvent {
  id: string;
  event_type: string;
  related_record_id: string | null;
  set_id: string | null;
  snapshot: Record<string, unknown>;
  created_at: string;
}

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function listDocuments(): Promise<DocumentSummary[]> {
  return request<DocumentSummary[]>("/documents");
}

export function listJobs(): Promise<JobProgress[]> {
  return request<JobProgress[]>("/jobs");
}

export function getJob(jobId: string): Promise<JobProgress> {
  return request<JobProgress>(`/jobs/${jobId}`);
}

export function uploadDocument(file: File): Promise<UploadResponse> {
  const payload = new FormData();
  payload.append("file", file);
  return request<UploadResponse>("/documents", { method: "POST", body: payload });
}

export function createDocumentSet(name: string, files: File[]): Promise<SetUploadResponse> {
  const payload = new FormData();
  payload.append("name", name);
  files.forEach((file) => payload.append("files", file));
  return request<SetUploadResponse>("/sets", { method: "POST", body: payload });
}

export function listDocumentSets(): Promise<DocumentSetSummary[]> {
  return request<DocumentSetSummary[]>("/sets");
}

export function getDocumentSet(setId: string): Promise<DocumentSetDetail> {
  return request<DocumentSetDetail>(`/sets/${setId}`);
}

export function retryDocumentSet(setId: string): Promise<DocumentSetDetail> {
  return request<DocumentSetDetail>(`/sets/${setId}/retry`, { method: "POST" });
}

export function stopDocumentProcessing(setId: string, documentId: string): Promise<DocumentSetDetail> {
  return request<DocumentSetDetail>(`/sets/${setId}/documents/${documentId}/stop`, { method: "POST" });
}

export function removeDocumentFromSet(setId: string, documentId: string): Promise<DeleteResponse> {
  return request<DeleteResponse>(`/sets/${setId}/documents/${documentId}`, { method: "DELETE" });
}

export function deleteDocumentSet(setId: string): Promise<DeleteResponse> {
  return request<DeleteResponse>(`/sets/${setId}`, { method: "DELETE" });
}

export function listFacts(setId: string): Promise<FactSummary[]> {
  return request<FactSummary[]>(`/facts?set_id=${encodeURIComponent(setId)}`);
}

export function getFact(factId: string): Promise<FactDetail> {
  return request<FactDetail>(`/facts/${factId}`);
}

export function correctFact(
  factId: string,
  payload: { field_path: string; corrected_value: string; note?: string },
): Promise<FactDetail> {
  return request<FactDetail>(`/facts/${factId}/corrections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function listRelationships(setId: string): Promise<RelationshipSummary[]> {
  return request<RelationshipSummary[]>(`/relationships?set_id=${encodeURIComponent(setId)}`);
}

export function listHistory(setId?: string, limit = 100): Promise<HistoryEvent[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (setId) params.set("set_id", setId);
  const query = `?${params.toString()}`;
  return request<HistoryEvent[]>(`/history${query}`);
}

export function pageRenderUrl(documentId: string, pageNumber: number): string {
  return `${API_BASE}/documents/${documentId}/pages/${pageNumber}/render`;
}

export interface RelationshipDetail {
  relationship: RelationshipSummary;
  left: FactDetail;
  right: FactDetail;
  revisions: RelationshipSummary[];
  reasoning_trace: Record<string, unknown>;
}
export function getRelationship(id: string): Promise<RelationshipDetail> { return request(`/relationships/${id}`); }
export function recheckRelationship(id: string): Promise<RelationshipSummary> { return request(`/relationships/${id}/recheck`, {method: "POST"}); }
export function correctRelationship(id: string, label: RelationshipSummary["label"], note: string): Promise<RelationshipSummary> {
  return request(`/relationships/${id}/corrections`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({label, note})});
}
