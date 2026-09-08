# Factualize

A local-first system that ingests arbitrary PDFs, extracts grounded facts, links every fact back to its exact source evidence, and reasons across documents to determine whether facts **corroborate**, **contradict**, or can be **reconciled through context** — with a human correction layer and a full, revisitable history of how the system arrived at every conclusion.

---

## Setup and Run Instructions

### Prerequisites
- **Python**: 3.11+
- **Node.js**: 18+ LTS
- **Gemini API Key**: A free Gemini API key from [Google AI Studio](https://aistudio.google.com/) (no billing enabled — see Limitations below)

---

### 1. Clone the repository

```bash
git clone https://github.com/anshuS1310/Factualize.git
cd Factualize
```

---

### 2. Backend setup

From the repository root:

**Linux / macOS:**
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

**Windows PowerShell:**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

Open `.env` in any text editor and add your Gemini API key:

```env
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_EXTRACTION_MODEL=gemini-3.5-flash-lite
GEMINI_REASONING_MODEL=gemini-3.5-flash
```

Start the backend server:

```powershell
python -m backend.app
```

The backend starts a local FastAPI server on `http://127.0.0.1:8000`. Interactive API docs (Swagger UI) are available at `http://127.0.0.1:8000/docs` — you can upload a PDF and inspect every endpoint directly from there without the frontend at all.

---

### 3. Frontend setup

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173` in your browser.

---

### 4. Using it

- **Upload & Queue**: Upload any PDF or create a named set from the workspace. Watch each file move through the queue in real time (`looking through pages` → `reading pages` → `finding details` → `connecting names` → `checking reports` → `ready`).
- **Browse Facts & Visual Evidence**: On the **Findings** page, browse extracted claims with their linked evidence. Clicking any finding opens the Fact Drawer showing the exact source page with the text bounding box highlighted in yellow alongside the original quote.
- **Cross-Document Relationships**: View cross-document relationships — corroborations, contradictions, and reconciliations — filterable by classification and confidence.
- **Human Correction**: Click **Make a correction** on any fact to specify which field is wrong, what it should be, and an optional note. The previous revision is preserved, and dependent relationships are flagged as stale.
- **Selective Re-reasoning & Retry**: If provider rate limits or transient errors occur, use **Try again** to reprocess only the failed work units without re-extracting completed pages.
- **Revisitable History**: Visit the **Past sets** view to click through past ingestions, comparisons, and corrections as they actually happened.

> No Docker, no external database server, no cloud deployment — everything runs locally against SQLite and the local filesystem, with Gemini's free API as the only external dependency.

---

## Video Demo

[Watch the 3-minute demo](https://drive.google.com/file/d/1vOQTeWb9qLlhG6zxl0AAyuFaR2FU6vmv/view?usp=drive_link)

The video walks through a PDF being uploaded and processed end to end, and demonstrates all four required cases directly from real processed data (not seeded or hardcoded):
1. **A corroborated fact**: Claims from different documents agreeing materially under the same context.
2. **A genuine contradiction**: Conflicting values where entity, time period, and scope match.
3. **An apparent contradiction reconciled through context**: Figures that appear to conflict at first glance, but are reconciled once differing time periods, scopes, or units are accounted for.
4. **An honest extraction/reasoning failure**: An uncertain comparison or boundary case flagged with low confidence and caught gracefully by the system.

---

## Approach

### Architecture Overview

The system is built as four sequential stages plus one cross-cutting layer, each handing off a clean, structured artifact to the next:

- **Step 1 — Extraction**: Every PDF is triaged per-page by actual structure (selectable text density, block layout, visual image area) rather than by filename or assumption. Simple pages route through a fast, deterministic extractor (`PyMuPDF`) with zero AI cost; complex layouts retain their structure. Visual elements are filtered to keep data-bearing candidates (charts/graphs) while discarding decorative graphics based on visual area metrics. Pages are reassembled with page-level position memory intact, batched for quota efficiency without losing per-fact evidence traceability, and passed through schema-validated fact extraction (subject, predicate, object, raw/normalized values, units, time periods, scope) using Gemini in structured JSON mode with bounded retries.
- **Step 2 — Cross-Document Reasoning**: Facts are grouped in two independent local stages:
  - *Entity resolution* (a local fuzzy-matching process via `rapidfuzz`, entirely free of LLM calls) answers "who or what is this about," normalizing corporate suffixes (`Inc`, `Ltd`, `Corp`, `LLC`) into unified canonical entities.
  - *Local sentence embeddings* (`all-MiniLM-L6-v2`) answer "what is actually being claimed," selecting same-entity candidate pairs using cosine similarity (>0.72) rather than an all-pairs cross-product.
  - *Deterministic code* handles arithmetic, scale multipliers (`thousand`, `lakh`, `crore`, `million`, `billion`), unit matches, and date/period alignments before any LLM is invoked.
  - Only pre-qualified, hint-annotated groups reach Gemini, which classifies each as `corroborates`, `contradicts`, `reconciled`, or `insufficient_evidence`, attaching confidence scores and human-readable explanations.
- **Step 3 — API and UI**: A FastAPI backend reuses Pydantic schema classes for both LLM structured output and API responses, exposing endpoints for facts, filterable relationships, and job status. Heavy work is managed through an asynchronous background queue so uploads never block. A React 19 frontend consumes this API, featuring a page-level evidence viewer that highlights the exact source region from which a fact was extracted.

---

### Step 4 — Human Correction Layer (Unique Addition)

Rather than treating LLM output as final, every fact carries an option for human correction. This is built as an audit-safe, retrieval-augmented correction memory:

1. **Precision Field Correction**: The user selects the exact field that is incorrect (`normalized_value`, `raw_value`, `claim_text`, `subject`, `predicate`, `time_period`, `scope`) and supplies the corrected value and an optional explanation note, with the source PDF page and bounding box visible right beside the form.
2. **Immutable Revisions (Never Overwritten)**: The system never silently overwrites the existing fact. The previous revision is marked `is_current = 0`, and a new revision (`revision = revision + 1`) is created with `is_current = 1`. The original evidence anchors are duplicated and linked to the new revision.
3. **Staleness Cascading**: Any cross-document relationships referencing the modified fact are immediately updated to `stale = 1`. They are not silently deleted or silently assumed correct; the UI flags them so the user knows they need re-reasoning.
4. **Permanent Correction Audit**: A dedicated `corrections` record is stored with the exact fingerprint, previous value, corrected value, and user note.
5. **Retrieval-Augmented Correction Memory**: Because this project runs against a free API with no model fine-tuning or weight training access, the system uses an honest in-context learning mechanism:
   - An exact-match signature catches identical recurring mistakes for free without LLM calls.
   - Saved corrections are indexed so that subsequent extractions and re-reasoning calls can inject past human corrections into future prompts.
6. **Selective Re-reasoning (Redo)**: Re-running reasoning re-evaluates *only* the affected stale relationships—never re-parsing the whole document or re-extracting unaffected pages.

---

### History — Making the System's Own Past Inspectable (Unique Addition)

Rather than only showing the current end-state, every meaningful event — a document uploaded, a fact batch extracted, entities merged, a relationship classified, or a human correction applied — is recorded as an individually revisitable snapshot in an append-only `history_events` table:

- **Literal Evidence Grounding**: Clicking into any past event displays the exact facts and evidence as they existed at that moment in time.
- **Auditable Failure & Correction Sequences**: The required failure case is not a staged confession; it is visible as a real timeline sequence (an initial extraction, followed by the human correction that fixed it and the resulting stale relationship cascade).
- **Zero Reconstruction Guesswork**: Because facts and relationships use append-only revisions with `revision` and `is_current` flags, reconstructing what the system believed at any historical point is a straightforward, reliable query rather than a fragile undo operation.
- **Visible Incremental Growth**: As new PDFs are uploaded, new history entries record the delta without reprocessing prior documents, providing proof of true incremental knowledge accumulation.

---

### Key Engineering Decisions and Trade-offs

- **SQLite Over a Graph Database**: Facts and relationships are stored in relational tables with SQLite WAL mode and foreign-key constraints. This was a deliberate choice: keeping storage in plain SQLite makes it structurally clear that reasoning happens during extraction, blocking, and comparison—storage is simply where conclusions land.
- **Two Cheap Local Filters Before Every LLM Call**: Entity resolution (fuzzy matching) and semantic blocking (`all-MiniLM-L6-v2`) run 100% locally on CPU. This eliminates comparing thousands of unrelated facts against each other, shrinking millions of potential comparisons down to a few dozen pre-qualified pairs and preserving free-tier quota.
- **Atomic, Restart-Safe Processing**: Work is checkpointed at the level of individual pages, fact batches, and relationship pairs. If processing is interrupted, `recover_incomplete_work()` safely resets in-flight units to `queued` on the next startup without corrupting state or losing completed work.
- **Incremental by Construction**: New documents are matched against existing entity clusters and fact embeddings rather than triggering full recomputation.

---

### AI Tools Used

- **Design & Architecture Sounding Board**: Architecture decisions, trade-off analyses, and schema designs were developed through technical discussions with Claude (Anthropic), specifically evaluating entity resolution scaling, revision immutability, and deterministic arithmetic splits.
- **Runtime Inference**: Google Gemini (`google-genai` SDK) is used exclusively at runtime for structured fact extraction and ambiguous cross-document relationship reasoning under zero temperature and structured JSON schemas.

---

## Limitations and Next Steps

### Handling the Gemini Free Tier & Hardware Constraints
- **Hardware Constraints**: This project was developed on standard laptop hardware without a dedicated high-end GPU. Running a local 8B+ reasoning LLM locally at usable speeds was not viable due to RAM/VRAM limitations, and paid cloud APIs were avoided entirely.
- **Gemini Free-Tier Rate Limits**: The entire pipeline relies on Gemini's free API tier (with no billing enabled). Under real-world multi-page processing, free-tier requests-per-minute (RPM) and daily quotas are genuinely hit, and `429 Resource Exhausted` errors do occur during cross-document comparisons.
- **Resilient Degradation**: Rather than crashing or aborting, the pipeline is engineered to absorb rate limits:
  - An internal pacing lock ensures requests maintain a minimum delay.
  - Automatic retries employ progressive exponential backoff.
  - When cross-document reasoning quota is exhausted, ambiguous pairs degrade gracefully to `insufficient_evidence` with an honest reasoning trace explaining that provider quota was reached, while all completed facts, evidence anchors, and deterministic comparisons remain fully intact and viewable.

### Other Known Limitations
- **Currency & Unit Conversion**: Relies on deterministic scale conversions (`lakh`, `crore`, `million`, `billion`). Cross-currency cases with differing currencies remain uncertain unless supported by explicit contextual rates in the source text.
- **Local Embedding Model Size**: `all-MiniLM-L6-v2` was selected for CPU speed and zero memory overhead. While fast, subtle semantic matches may occasionally score below the 0.72 threshold.
- **Single-Worker Queue**: The job runner processes one document at a time to prevent quota spikes and memory pressure on consumer hardware.

### Next Steps (Given More Time)
- Deepen Docling OCR integration for heavily degraded scanned documents.
- Add dynamic historical FX rate tables as a configurable context source.
- Implement multi-document worker pools when higher-tier API quotas are available.

---

## Additional Notes

- **Zero Hardcoded Data**: No document names, entity aliases, page numbers, or facts from the starter PDFs are hardcoded anywhere in the pipeline. All extraction and comparison logic operates dynamically on arbitrary document inputs.
- **Starter Datasets**: The PDFs under `starter-datasets/` (`delhivery` and `india-macroeconomy`) are provided for testing and verification; they are not application data.
- **Lightweight Footprint**: The application intentionally avoids heavy infrastructure (no Docker requirement, no external database servers, no cloud deployment) in favor of a clean, understandable, and verifiable local architecture.
