import { ChangeEvent, DragEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertCircle,
  ArrowUpRight,
  BadgeCheck,
  Check,
  Clock3,
  FileText,
  FileSearch,
  FileStack,
  Files,
  History,
  LoaderCircle,
  Sparkles,
  StopCircle,
  Trash2,
  UploadCloud,
} from "lucide-react";

import {
  DocumentSummary,
  DocumentSetDetail,
  DocumentSetSummary,
  FactDetail,
  FactSummary,
  JobProgress,
  JobStatus,
  correctFact,
  createDocumentSet,
  deleteDocumentSet,
  getFact,
  getDocumentSet,
  listDocumentSets,
  listFacts,
  pageRenderUrl,
  removeDocumentFromSet,
  retryDocumentSet,
  stopDocumentProcessing,
} from "./api";
import "./styles.css";

type ViewName = "workspace" | "facts" | "history";

const stageLabel: Record<string, string> = {
  queued: "In line",
  preparing: "Getting ready",
  triaging: "Looking through pages",
  parsing: "Reading pages",
  extracting_facts: "Finding details",
  resolving_entities: "Connecting names",
  comparing_facts: "Checking reports",
  completed: "Ready",
};

function readableStatus(status: JobStatus): string {
  const labels: Record<string, string> = {
    queued: "In line", running: "In progress", completed: "Ready",
    completed_with_issues: "Needs a look", needs_attention: "Needs a look",
    terminal_failure: "Could not finish",
  };
  return labels[status] ?? status.replaceAll("_", " ");
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function App() {
  const [view, setView] = useState<ViewName>("workspace");
  const [sets, setSets] = useState<DocumentSetSummary[]>([]);
  const [selectedSetId, setSelectedSetId] = useState<string | null>(null);
  const [selectedSet, setSelectedSet] = useState<DocumentSetDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [facts, setFacts] = useState<FactSummary[]>([]);
  const [selectedFact, setSelectedFact] = useState<FactDetail | null>(null);
  const [setName, setSetName] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    try {
      const nextSets = await listDocumentSets();
      setSets(nextSets);
      const nextId = selectedSetId && nextSets.some((item) => item.id === selectedSetId)
        ? selectedSetId
        : nextSets[0]?.id ?? null;
      if (nextId) {
        setSelectedSet(await getDocumentSet(nextId));
        if (nextId !== selectedSetId) setSelectedSetId(nextId);
      } else {
        setSelectedSet(null);
      }
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to contact the API.");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [selectedSetId]);

  useEffect(() => {
    const loadView = async () => {
      try {
        if (view === "facts" && selectedSetId) setFacts(await listFacts(selectedSetId));
      } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : "Unable to load workspace data.");
      }
    };
    if (view !== "workspace") void loadView();
  }, [view, selectedSetId]);

  const openFact = async (factId: string) => {
    try {
      setSelectedFact(await getFact(factId));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to open this fact.");
    }
  };

  const saveCorrection = async (fieldPath: string, correctedValue: string, note: string) => {
    if (!selectedFact) return;
    try {
      const corrected = await correctFact(selectedFact.id, {
        field_path: fieldPath,
        corrected_value: correctedValue,
        note: note || undefined,
      });
      setSelectedFact(corrected);
      setFacts((current) => [corrected, ...current.filter((fact) => fact.stable_id !== corrected.stable_id)]);
      setMessage("Your change has been saved.");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Correction could not be saved.");
    }
  };

  const selectSet = async (setId: string) => {
    try {
      setSelectedSetId(setId);
      setSelectedSet(await getDocumentSet(setId));
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "We could not open this set.");
    }
  };

  const retryFailedWork = async () => {
    if (!selectedSetId) return;
    try {
      const retried = await retryDocumentSet(selectedSetId);
      setSelectedSet(retried);
      setSets((current) => current.map((item) => item.id === retried.id ? retried : item));
      setMessage("We’ll pick up where we left off.");
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "We could not restart this set.");
    }
  };

  const stopPdf = async (documentId: string) => {
    if (!selectedSetId) return;
    try {
      const updated = await stopDocumentProcessing(selectedSetId, documentId);
      setSelectedSet(updated);
      setSets((current) => current.map((item) => item.id === updated.id ? updated : item));
      setMessage("We’ll pause this report after its current step.");
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "We could not pause this report.");
    }
  };

  const removePdf = async (documentId: string, filename: string) => {
    if (!selectedSetId || !window.confirm(`Remove ${filename} and its findings from this set?`)) return;
    try {
      await removeDocumentFromSet(selectedSetId, documentId);
      const updated = await getDocumentSet(selectedSetId);
      setSelectedSet(updated);
      setSets((current) => current.map((item) => item.id === updated.id ? updated : item));
      setMessage("Report removed from this set.");
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "We could not remove this report.");
    }
  };

  const removeSet = async () => {
    if (!selectedSetId || !selectedSet || !window.confirm(`Delete “${selectedSet.name}”? This removes its reports and findings from Factualize.`)) return;
    try {
      await deleteDocumentSet(selectedSetId);
      setSets((current) => current.filter((item) => item.id !== selectedSetId));
      setSelectedSetId(null);
      setSelectedSet(null);
      setFacts([]);
      setMessage("Set deleted.");
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "We could not delete this set.");
    }
  };

  const upload = async (files: File[]) => {
    if (!files.length) return;
    if (files.some((file) => file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf"))) {
      setError("Please choose a PDF report.");
      return;
    }
    setIsUploading(true);
    setError(null);
    try {
      const response = await createDocumentSet(setName, files);
      setSelectedSetId(response.document_set.id);
      setSelectedSet(response.document_set);
      setSets((current) => [
        response.document_set,
        ...current.filter((item) => item.id !== response.document_set.id),
      ]);
      setSetName("");
      setMessage(
        response.reused_document_count
          ? `PDF set created. ${response.reused_document_count} source file(s) reused from local cache.`
          : "Your set is ready to read.",
      );
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The upload could not be completed.");
    } finally {
      setIsUploading(false);
    }
  };

  const onFileInput = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    if (files.length) void upload(files);
    event.target.value = "";
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    const files = Array.from(event.dataTransfer.files);
    if (files.length) void upload(files);
  };

  const activeJobs = useMemo(
    () => selectedSet?.documents.filter((member) => member.job && !["completed", "completed_with_issues"].includes(member.job.status)).length ?? 0,
    [selectedSet],
  );
  const documents = selectedSet?.documents.map((member) => member.document) ?? [];
  const readyReports = selectedSet?.documents.filter((member) => member.job?.status === "completed" || member.job?.status === "completed_with_issues").length ?? 0;
  const reportsNeedingAttention = selectedSet?.documents.filter((member) => member.job?.status === "needs_attention" || member.job?.status === "terminal_failure").length ?? 0;

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand"><Sparkles size={20} /> <span>factualize</span></div>
        <p className="brand-note">Make every report<br />easier to trust.</p>
        <nav aria-label="Main navigation">
          <NavItem icon={<Files />} label="My sets" active={view === "workspace"} onClick={() => setView("workspace")} />
          <NavItem icon={<FileSearch />} label="Findings" active={view === "facts"} onClick={() => setView("facts")} />
          <NavItem icon={<History />} label="Past sets" active={view === "history"} onClick={() => setView("history")} />
        </nav>
        <div className="sidebar-foot"><span className="pulse" /> Your private desk</div>
      </aside>

      <section className="content">
        <header className="topbar">
          <div>
            <p className="eyebrow">Your reading desk</p>
            <h1>{view === "workspace" ? "Your report library" : selectedSet?.name ?? "Choose a set"}</h1>
          </div>
          <div className="topbar-actions">{selectedSet && <span className="current-set"><span /> {selectedSet.name}</span>}<button className="primary-button" onClick={() => inputRef.current?.click()} disabled={isUploading}>
            {isUploading ? <LoaderCircle className="spin" /> : <UploadCloud />}
            Add reports
          </button></div>
          <input ref={inputRef} className="sr-only" type="file" multiple accept="application/pdf,.pdf" onChange={onFileInput} />
        </header>

        {view === "workspace" ? (
          <>
            <section className="hero-grid">
              <div className="intro-card">
                <div className="orb orb-one" /><div className="orb orb-two" />
                <p className="eyebrow">A calmer way to read</p>
                <h2>Bring every report<br /><em>into focus.</em></h2>
                <p>Gather related reports in one place. The details stay close to the page they came from, so you can read with confidence.</p>
                <button className="text-button" onClick={() => inputRef.current?.click()}>Start a new set <ArrowUpRight size={16} /></button>
                <div className="hero-note"><span>✦</span> One place for every source</div>
              </div>
              <div className="upload-card" onDragOver={(event) => event.preventDefault()} onDrop={onDrop}>
                <div className="upload-icon"><UploadCloud size={25} /></div>
                <h3>Drop your reports here</h3>
                <p>Add one file, or a whole reading set.</p>
                <input className="set-name-input" value={setName} onChange={(event) => setSetName(event.target.value)} placeholder="Give this set a name" aria-label="Set name" />
                <button className="soft-button" onClick={() => inputRef.current?.click()} disabled={isUploading}>Choose reports</button>
                <span>PDF files · up to 25 at once</span>
              </div>
            </section>

            {message && <Notice type="success" message={message} onClose={() => setMessage(null)} />}
            {error && <Notice type="error" message={error} onClose={() => setError(null)} />}

            <section className="section-heading">
              <div><p className="eyebrow">Open set</p><h2>{selectedSet?.name ?? "Choose a set to begin"}</h2></div>
              <div className="set-actions">{selectedSet?.status === "completed_with_issues" && <button className="soft-button retry-button" onClick={retryFailedWork}>Try again</button>}{selectedSet && <button className="icon-button delete-set-button" onClick={removeSet} title="Delete this set" aria-label="Delete this set"><Trash2 size={16} /> Delete</button>}</div>
            </section>
            {selectedSet && <section className="set-overview"><OverviewCard label="Reports" value={selectedSet.document_count} icon={<FileStack size={18} />} tone="sun" /><OverviewCard label="Ready to read" value={readyReports} icon={<Check size={18} />} tone="mint" /><OverviewCard label="In progress" value={activeJobs} icon={<LoaderCircle size={18} />} tone="violet" /><OverviewCard label="Needs a look" value={reportsNeedingAttention} icon={<AlertCircle size={18} />} tone="rose" /></section>}
            <section className="job-grid">
              {isLoading ? <LoadingCard /> : documents.length === 0 ? <QueueEmpty /> : selectedSet?.documents.map((member) => (
                <DocumentCard key={`${selectedSet.id}-${member.document.id}`} document={member.document} job={member.job ?? undefined} onStop={() => stopPdf(member.document.id)} onRemove={() => removePdf(member.document.id, member.document.original_filename)} />
              ))}
            </section>
            <SetPicker sets={sets} selectedSetId={selectedSetId} onSelect={selectSet} />
          </>
        ) : view === "facts" ? (
          <FactsView
            facts={facts}
            documents={documents}
            onOpen={openFact}
          />
        ) : (
          <HistoryView sets={sets} selectedSetId={selectedSetId} onSelect={(setId) => { void selectSet(setId); setView("workspace"); }} />
        )}
      </section>
      {selectedFact && <FactDrawer fact={selectedFact} onClose={() => setSelectedFact(null)} onCorrect={saveCorrection} />}
    </main>
  );
}

function NavItem({ icon, label, active, onClick }: { icon: ReactNode; label: string; active: boolean; onClick: () => void }) {
  return <button className={`nav-item ${active ? "active" : ""}`} onClick={onClick}>{icon}<span>{label}</span></button>;
}

function OverviewCard({ label, value, icon, tone }: { label: string; value: number; icon: ReactNode; tone: string }) {
  return <article className={`overview-card ${tone}`}><span className="overview-icon">{icon}</span><div><p>{label}</p><strong>{value}</strong></div><span className="overview-arrow">↗</span></article>;
}

function DocumentCard({ document, job, onStop, onRemove }: { document: DocumentSummary; job?: JobProgress; onStop: () => void; onRemove: () => void }) {
  const status = job?.status ?? "queued";
  const percentage = job?.progress_total ? Math.round((job.progress_current / job.progress_total) * 100) : 0;
  const isFailure = status === "needs_attention" || status === "terminal_failure";
  const isDone = status === "completed" || status === "completed_with_issues";
  const progressMessage = isFailure ? "This report needs a look." : isDone ? "Ready to read." : stageLabel[job?.stage ?? ""] ?? "Reading your report";
  return (
    <article className="document-card">
      <div className="document-top">
        <div className={`document-icon ${isFailure ? "danger" : isDone ? "done" : ""}`}>
          {isFailure ? <AlertCircle /> : isDone ? <Check /> : <FileSearch />}
        </div>
        <span className={`status-badge ${status}`}>{readableStatus(status)}</span>
      </div>
      <h3 title={document.original_filename}>{document.original_filename}</h3>
      <p>{document.page_count ? `${document.page_count} pages` : "Getting ready"} · Added {dateLabel(document.created_at)}</p>
      {job && <>
        <div className="job-description">
          {status === "queued" && job.queue_position ? <Clock3 size={15} /> : <LoaderCircle size={15} className={isDone || isFailure ? "" : "spin"} />}
          <span>{status === "queued" && job.queue_position ? `${job.queue_position - 1} report${job.queue_position === 2 ? "" : "s"} ahead` : progressMessage}</span>
        </div>
        <div className="progress-track"><div className="progress-value" style={{ width: `${isDone ? 100 : percentage}%` }} /></div>
        <div className="card-footer"><span>{isDone ? "Ready to read" : `${percentage}% read`}</span><span>{stageLabel[job.stage] ?? job.stage}</span></div>
      </>}
      <div className="document-actions">{(status === "queued" || status === "running") && <button className="icon-button stop-button" onClick={onStop}><StopCircle size={15} /> Pause</button>}<button className="icon-button delete-document-button" onClick={onRemove}><Trash2 size={15} /> Remove</button></div>
    </article>
  );
}

function Notice({ type, message, onClose }: { type: "success" | "error"; message: string; onClose: () => void }) {
  return <div className={`notice ${type}`}><span>{type === "success" ? <Check size={17} /> : <AlertCircle size={17} />}</span><p>{message}</p><button onClick={onClose}>Dismiss</button></div>;
}

function LoadingCard() { return <article className="loading-card"><LoaderCircle className="spin" /><span>Connecting to your workspace…</span></article>; }
function QueueEmpty() { return <article className="queue-empty"><Files size={28} /><h3>No set open yet</h3><p>Add one or more reports to begin.</p></article>; }

function SetPicker({ sets, selectedSetId, onSelect }: { sets: DocumentSetSummary[]; selectedSetId: string | null; onSelect: (setId: string) => void }) {
  return <section className="set-picker">
    <div className="section-heading"><div><p className="eyebrow">Your library</p><h2>Recent sets</h2></div><span className="count-pill">{sets.length} total</span></div>
    {sets.length === 0 ? <p className="set-picker-empty">No sets yet. Add a report or a related group above.</p> : <div className="set-grid">
      {sets.map((set) => <button key={set.id} className={`set-card ${set.id === selectedSetId ? "selected" : ""}`} onClick={() => onSelect(set.id)}>
        <div><strong>{set.name}</strong><span>{set.document_count} PDF{set.document_count === 1 ? "" : "s"} · {dateLabel(set.created_at)}</span></div>
        <span className={`status-badge ${set.status}`}>{readableStatus(set.status as JobStatus)}</span>
      </button>)}
    </div>}
  </section>;
}

function FactsView({ facts, documents, onOpen }: { facts: FactSummary[]; documents: DocumentSummary[]; onOpen: (id: string) => void }) {
  const nameForDocument = (id: string) => documents.find((document) => document.id === id)?.original_filename ?? "Source document";
  return <section className="data-view">
    <div className="section-heading"><div><p className="eyebrow">What your reports say</p><h2>Findings</h2></div><span className="count-pill">{facts.length} found</span></div>
    <p className="cross-check-intro">Every finding stays connected to its source. When reports speak about the same thing, you’ll see how their stories fit together.</p>
    {facts.length === 0 ? <DataEmpty icon={<FileSearch />} title="Nothing to show yet" body="Findings will appear here after the reports have been read." /> : <div className="facts-list">
      {facts.map((fact) => <button className="fact-row" key={fact.id} onClick={() => onOpen(fact.id)}>
        <div className="fact-confidence">{Math.round(fact.confidence * 100)}<small>%</small></div>
        <div><p className="fact-claim">{fact.claim_text}</p><span>{fact.primary_entity_mention ?? "Unnamed subject"} · {fact.time_period ?? "No date given"}</span><span className={`cross-check-badge ${fact.cross_check_status}`}>{findingLabel(fact.cross_check_status)} · {fact.cross_check_source_count} report{fact.cross_check_source_count === 1 ? "" : "s"}</span><small className="cross-check-explanation">{fact.cross_check_explanation}</small></div>
        <div className="fact-meta"><span>{nameForDocument(fact.document_id)}</span>{fact.correction_state === "corrected" && <BadgeCheck size={16} />}</div>
      </button>)}
    </div>}
  </section>;
}

function HistoryView({ sets, selectedSetId, onSelect }: { sets: DocumentSetSummary[]; selectedSetId: string | null; onSelect: (setId: string) => void }) {
  return <section className="data-view">
    <div className="section-heading"><div><p className="eyebrow">Your reading trail</p><h2>Past sets</h2></div><span className="count-pill">{sets.length} saved</span></div>
    {sets.length === 0 ? <DataEmpty icon={<History />} title="No past sets yet" body="The sets you create will stay here." /> : <div className="set-grid history-set-grid">{sets.map((set) => <button key={set.id} className={`set-card ${set.id === selectedSetId ? "selected" : ""}`} onClick={() => onSelect(set.id)}><div><strong>{set.name}</strong><span>{set.document_count} report{set.document_count === 1 ? "" : "s"} · {readableStatus(set.status as JobStatus)}</span></div><ArrowUpRight size={16} /></button>)}</div>}
  </section>;
}

function FactDrawer({ fact, onClose, onCorrect }: { fact: FactDetail; onClose: () => void; onCorrect: (field: string, value: string, note: string) => void }) {
  const [field, setField] = useState("normalized_value");
  const [value, setValue] = useState(fact.normalized_value ?? fact.object_value ?? "");
  const [note, setNote] = useState("");
  const evidence = fact.evidence[0];
  const bbox = evidence?.bbox;
  const width = bbox ? Math.max(1, (bbox[2] - bbox[0]) * 100) : 0;
  const height = bbox ? Math.max(1, (bbox[3] - bbox[1]) * 100) : 0;
  return <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
    <aside className="fact-drawer" role="dialog" aria-modal="true" aria-label="Fact evidence" onMouseDown={(event) => event.stopPropagation()}>
      <button className="drawer-close" onClick={onClose}>Close</button>
      <p className="eyebrow">Source finding</p><h2>{fact.claim_text}</h2>
      <div className="detail-grid"><Detail label="About" value={fact.subject} /><Detail label="Says" value={fact.predicate} /><Detail label="Value" value={fact.normalized_value ?? fact.object_value} /><Detail label="When" value={fact.time_period} /><Detail label="Where it applies" value={fact.scope} /></div>
      {evidence && <section className="evidence-panel"><div className="evidence-heading"><FileText size={16} /><span>Source page {evidence.page_number}</span></div><div className="page-proof"> <img src={pageRenderUrl(fact.document_id, evidence.page_number)} alt={`Source page ${evidence.page_number}`} />{bbox && <span className="evidence-highlight" style={{ left: `${bbox[0] * 100}%`, top: `${bbox[1] * 100}%`, width: `${width}%`, height: `${height}%` }} />}</div><blockquote>{evidence.quote}</blockquote></section>}
      <section className="correction-panel"><p className="eyebrow">Make a correction</p><div className="correction-grid"><label>What would you like to change?<select value={field} onChange={(event) => setField(event.target.value)}><option value="normalized_value">Value</option><option value="raw_value">Original wording</option><option value="claim_text">Finding</option><option value="subject">Who or what it is about</option><option value="predicate">What it says</option><option value="time_period">Date or period</option><option value="scope">Where it applies</option></select></label><label>Your correction<input value={value} onChange={(event) => setValue(event.target.value)} /></label></div><label>Note (optional)<textarea value={note} onChange={(event) => setNote(event.target.value)} rows={2} /></label><button className="primary-button" onClick={() => onCorrect(field, value, note)} disabled={!value.trim()}>Save change</button></section>
    </aside>
  </div>;
}

function Detail({ label, value }: { label: string; value: string | null }) { return <div><span>{label}</span><strong>{value || "—"}</strong></div>; }
function findingLabel(value: FactSummary["cross_check_status"]): string { return { corroborated: "Supported by another report", reconciled: "Different context, same story", contradicted: "Reports disagree", insufficient_evidence: "Needs more context", no_comparable_source: "Only mentioned here" }[value]; }
function DataEmpty({ icon, title, body }: { icon: ReactNode; title: string; body: string }) { return <article className="data-empty">{icon}<h3>{title}</h3><p>{body}</p></article>; }

createRoot(document.getElementById("root")!).render(<App />);
