# Factualize

A local-first system that ingests arbitrary PDFs, extracts grounded facts, links every fact back to its exact source evidence, and reasons across documents to determine whether facts **corroborate**, **contradict**, or can be **reconciled through context** — with a human correction layer and a full, revisitable history of how the system arrived at every conclusion.

---

## Setup and Run Instructions

### Prerequisites
- **Python**: 3.11 (tested on 3.11.x)
- **Node.js**: 18+ LTS
- **Gemini API Key**: A free Gemini API key from [Google AI Studio](https://aistudio.google.com/) (no billing required)

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
GEMINI_EXTRACTION_MODEL=gemini-2.5-flash
GEMINI_REASONING_MODEL=gemini-2.5-flash
```

Start the backend server:

```powershell
python -m backend.app
```

The backend starts an asynchronous FastAPI server on `http://127.0.0.1:8000`.
- **API Health Check**: `http://127.0.0.1:8000/api/v1/health`
- **Interactive Swagger Docs**: `http://127.0.0.1:8000/docs` (you can upload PDFs and inspect every endpoint directly from the browser)

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

- **Workspace (My Sets)**: Create a named set and drag-and-drop one or multiple PDF reports (e.g. from `starter-datasets/`). Watch each PDF move through the queue in real-time (`looking through pages` → `reading pages` → `finding details` → `connecting names` → `checking reports` → `ready`).
- **Findings**: Browse extracted claims across documents. Filter by agreement state (`Supported by another report`, `Reports disagree`, `Different context, same story`, or `Only mentioned here`).
- **Evidence Proof & Bounding Boxes**: Click any finding to open the Fact Drawer. It displays the structured claim alongside the rendered source page with the exact text bounding box highlighted.
- **Human Corrections**: Select any field (Value, Original wording, Finding, Subject, Predicate, Time period, Scope) to submit a correction with an optional note. The previous revision is preserved, and dependent relationships are cascaded to `stale = 1`.
- **Past Sets (History)**: Inspect the immutable audit trail of every document ingestion, fact batch completion, entity merge, and correction event.
- **Retry Failed Work**: If provider rate limits or network issues occur, click **Try again** on a set to resume only the failed work units without re-extracting completed pages.

> No Docker, no external database server, no cloud deployment — everything runs locally against SQLite and the local filesystem, with Gemini's free API as the only external dependency.

---

## Video Demo

[Watch the 3-minute demo](https://drive.google.com/file/d/1vOQTeWb9qLlhG6zxl0AAyuFaR2FU6vmv/view?usp=drive_link)

The video walks through a PDF being uploaded and processed end-to-end, and demonstrates all four required cases directly from real processed data:
1. **Corroborated fact**: Cross-document claims that agree materially under identical context.
2. **Genuine contradiction**: Incompatible claims where time period, scope, and units match.
3. **Apparent contradiction reconciled through context**: Differing values explained by different time periods, accounting scopes (e.g., standalone vs. consolidated), or units.
4. **Honest extraction / uncertainty handling**: An extraction or comparison flagged as `insufficient_evidence` when ambiguity is high or free-tier reasoning quota is reached.

---

## Approach

### Architecture Overview

The system is built as four sequential stages plus one cross-cutting history layer, each handing off a clean, structured artifact to the next:

1. **Step 1 — Extraction & Triage**:
   Every PDF is triaged per-page by actual layout and density using PyMuPDF (`fitz`), measuring character counts, block positions, and visual image area. Decorative images are filtered out automatically based on area thresholds; data-bearing visual candidates (charts/figures) are preserved. Pages are rendered to high-resolution PNGs for UI display. Extraction is batched into quota-efficient page groups (up to 8 pages or 24,000 characters) and passed to Gemini in zero-temperature structured JSON mode. Quotes are mapped back to parsed page blocks to compute exact normalized bounding boxes (`[x0, y0, x1, y1]`).

2. **Step 2 — Cross-Document Reasoning**:
   Facts are grouped in two independent local stages:
   - **Entity Resolution**: Generic similarity matching via `rapidfuzz` (fuzz ratio and token-set ratio) collapses entity name variants (e.g., stripping corporate suffixes like `Inc`, `Ltd`, `Corp`, `LLC`) into canonical entities at zero LLM cost.
   - **Semantic Candidate Selection**: Local sentence embeddings (`all-MiniLM-L6-v2`) generate 384-dimensional vectors on CPU to select cross-document candidate pairs exceeding a 0.72 cosine similarity threshold.
   - **Deterministic Comparators**: Before any LLM call, deterministic code handles arithmetic checks, scale factors (`thousand`, `lakh`, `crore`, `million`, `billion`), unit matching, and date/period alignments within a 0.5% tolerance.
   - **LLM Reasoning Fallback**: Pre-qualified ambiguous pairs reach Gemini with deterministic context hints, classifying them as `corroborates`, `contradicts`, `reconciled`, or `insufficient_evidence`.

3. **Step 3 — API and UI**:
   FastAPI exposes clean REST endpoints for sets, documents, jobs, facts, relationships, and history. The React 19 frontend consumes this API with real-time polling (every 2.5s), responsive status badges, and interactive bounding box proof overlays on rendered PDF pages.

4. **Step 4 — Human Correction Layer**:
   Users can correct any field on a fact. The existing record is never overwritten; it is preserved with `is_current = 0` while a new revision (`revision + 1`) is inserted with `is_current = 1`. Any relationships referencing the corrected fact are flagged as `stale = 1`, and a permanent audit record is added to `corrections`.

5. **History & Event Sourcing**:
   Every significant lifecycle event (upload, batch extraction, entity merge, relationship judgment, correction) is written to an append-only `history_events` table with JSON snapshots. Reconstructing the system's exact state at any point in time is a simple SQL query.

---

### Key Engineering Decisions and Trade-offs

- **SQLite over a Graph Database**: Facts and relationships are stored in plain relational tables with SQLite WAL mode and foreign-key constraints. Reasoning happens explicitly in the pipeline; the database serves as a transparent, queryable storage layer without the operational overhead of a graph database.
- **Two Cheap Local Filters Before Every LLM Call**: Entity resolution (fuzzy matching) and semantic blocking (`all-MiniLM-L6-v2`) run 100% locally on CPU. This prevents quadratic cross-product comparisons, keeping free-tier API usage minimal.
- **Atomic, Restart-Safe Processing**: Each page, fact batch, and relationship pair is tracked as an individual `work_unit` with transactional commits. If interrupted, the queue resumes safely on restart without losing completed work.
- **Incremental by Construction**: Adding a new PDF to an existing set evaluates candidates against existing entity clusters and fact embeddings without reprocessing previously ingested documents.

---

### AI Tools Used

- **Design & Architecture**: Architecture decisions, schema designs, and failure-handling strategies were refined through design discussions with Claude (Anthropic), specifically evaluating entity resolution scaling, revision immutability, and deterministic unit handling.
- **Runtime Inference**: Google Gemini (`google-genai` SDK) is used exclusively at runtime for structured fact extraction and ambiguous cross-document relationship reasoning under zero temperature and JSON schema mode.

---

## Limitations and Next Steps

### Handling the Gemini Free Tier
This project is built to run on free-tier infrastructure. Because free Gemini API keys enforce strict requests-per-minute (RPM) and daily quotas, the pipeline is engineered around these limits:
- A rate-limiting mutex enforces a minimum interval between calls.
- Failed calls back off exponentially (`2^attempts`).
- When free-tier rate limits or daily quotas are reached, relationship classification degrades gracefully to `insufficient_evidence` with an honest trace explanation, rather than aborting the pipeline.

### Known Limitations
- **Currency Conversion**: Without source-backed historical currency exchange rates, cross-currency comparisons with differing currencies remain uncertain rather than guessed.
- **Local Embedding Trade-off**: `all-MiniLM-L6-v2` runs fast on CPU, but occasional nuanced claims may score below the 0.72 similarity threshold and go uncompared.
- **Single-Worker Queue**: The job runner processes one document at a time to prevent quota starvation and memory spikes on consumer hardware.

### Next Steps
- **Docling Deep Integration**: Connect complex scanned layouts to Docling's OCR pipeline behind the existing parser interface.
- **Dynamic FX Rates**: Incorporate source-attributed historical currency conversion tables.
- **Multi-Document Concurrency**: Add configurable parallel worker pools when higher-tier API quotas are available.

---

## Additional Notes

- **Zero Hardcoded Data**: The repository contains no hardcoded facts, entity aliases, or page rules. The PDFs in `starter-datasets/` (`delhivery` and `india-macroeconomy`) are inputs for testing and manual verification, not application code.
- **No Heavy Infrastructure**: Runs entirely on local Python and Node runtimes with SQLite. No Docker daemon, external database servers, or cloud credentials are required.
