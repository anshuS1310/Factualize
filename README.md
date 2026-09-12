# Factualize

A local PDF review prototype that extracts source-linked facts from named PDF sets, compares claims across documents, and keeps human corrections and earlier conclusions available for inspection. Results are evidence-backed claims, not a guarantee of real-world truth.

---

## Setup and Run Instructions

### Prerequisites

- Python **3.11** (the package currently requires >=3.11,<3.12).
- Node.js compatible with Vite 7 (20.19+ or 22.12+); development has also used Node 24.
- A Gemini API key and model IDs enabled for your account. This project cannot establish your account's quota or billing status.
- Internet for initial Docling/OCR/embedding model downloads and Gemini calls. CPU processing of complex PDFs may be slow.

### 1. Clone the repository

```bash
git clone https://github.com/anshuS1310/Factualize.git
cd Factualize
```

### 2. Backend setup

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Linux / macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

For an existing installation, keep your existing `.env`; run the install command again to pick up dependencies. Never overwrite your settings with the template during an upgrade.

Set these values in `.env`, using actual model IDs available to your account:

```env
GEMINI_API_KEY=your_key_here
GEMINI_EXTRACTION_MODEL=your_available_extraction_model
GEMINI_REASONING_MODEL=your_available_reasoning_model
FACTUALIZE_GEMINI_MIN_INTERVAL_SECONDS=4
FACTUALIZE_GEMINI_DAILY_REQUEST_BUDGET=15
FACTUALIZE_RELATIONSHIP_GEMINI_CALL_BUDGET=8
```

The 15-request limit is a conservative **local ceiling**, not a statement about Google's allowance. It counts attempted calls across both tasks in SQLite by UTC day and survives restarts and key changes. A provider quota error starts a one-hour local cooldown. Adjust the ceiling to your actual allowance; retry only when quota is available. Key changes require a backend restart.

Optional, before uploading a scan, table-heavy, or multi-column PDF, prefetch the local Docling/OCR models once (this does not use Gemini):

```powershell
python -m backend.app.prefetch_docling
```

```powershell
python -m backend.app
```

Backend: `http://127.0.0.1:8000`. Interactive API documentation: `http://127.0.0.1:8000/docs`.

### 3. Frontend setup

In another terminal:

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173`. Explicitly binding the address avoids the Windows IPv4/IPv6 localhost mismatch.

### 4. Using it

1. Create a named set of one or more related PDFs. One document runs at a time; the others wait in the queue.
2. Open **Findings** to inspect claims and their source pages. A source quote is preserved; a rectangle is shown only when available.
3. Open **Comparisons** to inspect agreement, disagreement, reconciliation and uncertainty. The demonstration cards select real current examples in the selected set. An empty category explicitly says no example exists.
4. Open a comparison to inspect both original fact revisions and quotes. Review older conclusions using **Previous conclusions**.
5. Correct a fact from its source drawer. Dependent conclusions become stale. In **Comparisons**, filter **Needs recheck** and click **Recheck current sources**. Rechecking creates a new conclusion revision using current fact revisions; a failed call leaves the earlier stale conclusion intact.
6. If the facts are right but their classification is wrong, use **Correct this conclusion** and provide a reason. Review stale sources first. Corrections are stored locally for reuse across later sets.
7. Use **History of PDFs** to reopen sets and **Activity & corrections** for the selected set's recorded events. Expand an event to read its snapshot and open linked fact revisions where available.
8. Pause processing between work units; retry failed work when available. Remove a report or delete a set after stopping active work. A shared source file is kept while another set references it.

No Docker, Redis, database server or FAISS is required. Uploaded PDF content is sent to Gemini for extraction/reasoning; “local” describes storage and orchestration, not offline inference.

---

## Video Demo

[Existing demo recording](https://drive.google.com/file/d/1vOQTeWb9qLlhG6zxl0AAyuFaR2FU6vmv/view?usp=drive_link)

This link is retained from the previous README; its contents have not been verified against this revision. Record a new walkthrough before submission. Show a real agreement, disagreement, contextual reconciliation and uncertain or failed case if the selected data produces them. Never substitute test fixtures for live demonstration data. A missing category is a visible limitation, not a reason to manufacture a claim.

---

## Approach

### Architecture Overview

- **Step 1 - Extraction:** PyMuPDF inspects pages. Simple pages keep the native text path. Complex pages (including detected tables, image-heavy/scanned pages and likely multiple columns) use a lazy Docling adapter with RapidOCR and table structure recognition. On first use, the necessary layout, table and English OCR artifacts are copied into `data/cache/docling-models`; this avoids Windows symlink-permission failures and is reused thereafter. Markdown preserves reading order and table layout; typed page artifacts also retain table cells, blocks, coordinates and page numbers. A failed complex conversion is a visible failed page, not a silent native-text fallback. See [Docling's pipeline options](https://docling-project.github.io/docling/reference/pipeline_options/).
- **Fact extraction:** Pages are batched for Gemini JSON-mode extraction and validated locally with Pydantic. Returned quotes and page numbers must match the submitted page text. Evidence rectangles are grounded in matching blocks where possible; a model rectangle is not trusted as text grounding. Text-recognized chart labels may produce facts, but this does not reconstruct a chart's visual relationships.
- **Step 2 - Comparison:** RapidFuzz resolves names locally. Local sentence embeddings normally propose same-entity pairs from different PDFs in the same set. If that optional model cannot initialise (for example, a first offline run), conservative lexical similarity keeps candidate discovery available; it never decides the conclusion. Numeric code handles a conservative subset with matching metric, period, scope and unit. Missing/different context requires further reasoning rather than automatically proving agreement or reconciliation. Gemini judges ambiguous pairs within the budget. No Splink, DuckDB or FAISS is used or declared as a direct dependency.
- **Step 3 - API and UI:** FastAPI/Pydantic, SQLite and a single background document runner serve a React/Vite interface. The UI exposes findings, comparisons, both source quotes, stale warnings, correction forms, revisions and event history. No separate relationship database is maintained.

### Step 4 - Human Correction Layer (Unique Addition)

1. Fact edits append a revision, preserve evidence anchors, invalidate embeddings and mark dependent comparisons stale. Original quote text remains unchanged even when a human corrects an interpretation.
2. Relationship edits append a revision in the same relationship lineage, mark the old revision non-current, and store the person's label and reason. Stale pairs must be rechecked before a label correction.
3. Rechecks load the latest revisions of both facts. Persistence checks that the relationship and facts have not changed during reasoning. A failed recheck records an event and does not clear staleness.
4. The existing `corrections` table is the unified memory for both correction types. Fingerprints contain claim, subject, predicate, values, unit, period, scope and, for fact corrections, the source quote. They exclude document IDs so identical contexts can recur across PDFs.
5. Exact matches reuse a human field correction or pair judgment locally. Similar contexts retrieve up to five examples from a bounded recent pool for Gemini prompts. Hints do not override new source evidence. Applied fact-memory IDs are retained in qualifiers; relationship reuse is recorded in the reasoning trace.
6. This is retrieval and in-context guidance, **not model training** and not a guarantee that mistakes will never recur. Changed context prevents automatic exact reuse. Older fact-correction records are upgraded from preserved fact revisions when available.

### History - Making the System's Own Past Inspectable (Unique Addition)

The timeline displays stored events for the chosen set, newest first. New comparison events include explanation, source revision IDs and reasoning mode. Correction events link old and new fact revisions. Existing events retain the information they originally recorded; old minimal snapshots do not magically contain a full past state. Deleted source data is no longer navigable. The UI loads 100 events at a time, up to 500, rather than offering an unlimited audit explorer.

### Key Engineering Decisions and Trade-offs

- **SQLite:** Embedded storage keeps setup understandable. Revisions and corrections are transactional. Two small tables persist request counts and provider cooldowns; no distributed quota infrastructure is needed.
- **Quota control:** Calls are paced, attempted calls count toward a local daily ceiling, extraction retry counts are bounded, and relationship calls have a per-document budget. A quota failure stops stronger model claims; ambiguous pairs may be stored as insufficient evidence with a quota explanation. This is operational uncertainty, not contradictory source evidence.
- **Checkpoints:** Individual pages, complete fact batches and comparison pairs are the units of work. A partly received model response is not a checkpoint. Cached extraction output receives current correction-memory checks before persistence in a later set.
- **Complex documents:** Docling models are loaded only when necessary. OCR/table extraction costs CPU, disk and startup time. Model download or parsing failures remain visible and retryable.
- **Existing data:** Completed work units and saved facts are preserved. Creating a new set with previously uploaded PDFs reuses simple-page caches but reparses old `docling_pending` pages with Docling. The earlier set's saved claims and evidence remain unchanged. There is no automatic rewrite of previously completed sets.

### AI Tools Used

- This implementation and its tests/documentation were developed with OpenAI Codex assistance.
- Earlier architecture discussions were described by the author as involving Claude; this README does not independently verify that history.
- Runtime Gemini is used for factual extraction and ambiguous comparison, in JSON mode with local validation. No model fine-tuning is performed.

---

## Limitations and Next Steps

### Handling the Gemini Free Tier & Hardware Constraints

Model access, quotas and billing vary by account. The app cannot guarantee processing hundreds of pages within a free daily allowance. A budget/cooldown error appears in stored failures; rechecking may still require waiting. There is no paid fallback. First-time local model loading can take minutes and needs network access for downloads.

### Other Known Limitations

- **Charts / Gemini Vision:** No image-based Gemini chart extraction is enabled. The inspected FY24 presentation's page 9 revenue bars repeat the FY23/FY24 values in the page 17 financial table; these do not require an extra vision call for that example. No indispensable chart-only demonstration claim was established during this pass. Decorative visuals are recorded as skipped; a detected visual that may contain data is recorded in the set timeline as needing review. Genuinely image-only chart values are not supported and are never silently described as read.
- **OCR:** Docling integration does not guarantee accurate recognition on every scan, language, rotated layout or merged table. Inspect source evidence before trusting a claim.
- **Candidate coverage:** Similarity and fuzzy name matching can miss aliases. Candidate discovery still performs local pair comparisons before selecting the highest-scoring candidates; it is not a large-scale vector index. Entity merges can leave new candidate discovery until a later processing run.
- **Numbers:** Numeric normalization is limited; no external FX rates or generalized accounting restatement engine. Rounding tolerances and extracted context are imperfect.
- **Correction memory:** Matching is conservative, fuzzy retrieval searches only recent records, and incorrect human corrections can propagate to identical contexts. Memory survives set deletion because it is shared across the local workspace. There is no memory-management screen or per-user isolation.
- **Presentation:** Facts remain individual source claims with cross-check summaries. The app does not synthesize a guaranteed single universal truth from a connected group of comparisons.
- **Operations:** This is a local prototype, without production authentication, deployment hardening, or a proof of bug-free operation. Rechecks are synchronous HTTP requests with pacing and a daily budget; avoid repeated concurrent rechecks.

### Next Steps (Given More Time)

- Add optional budgeted chart vision only for verified chart-only evidence needed by a use case.
- Add selective migration/reprocessing for old complex-page caches and paginated history.
- Add explicit correction-memory review/revocation and broader alias-resolution evaluations.
- Expand OCR/scan evaluations and source-specific quantitative normalization tests.

---

## Additional Notes

- No starter PDF claims, entities or page numbers are hardcoded in extraction or reasoning. Mentioned pages above document a manual inspection only.
- Starter PDFs in `starter-datasets/` are test inputs, not seeded application facts.
- Secrets belong in the ignored `.env`; no live keys belong in this README or frontend.
- Run checks from the root: `python -m pytest -q`. Frontend: `cd frontend` then `npm run build`.
- Tests use isolated SQLite databases and mocked/no-network reasoning for corrections, revisions, stale state, context-sensitive memory and quota persistence. These checks do not prove that live Gemini extraction or every PDF layout is correct.
