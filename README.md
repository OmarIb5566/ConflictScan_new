# ConflictScan / RFI Automation

A LangGraph agent that compares **two document sets from different stages or
disciplines of the same project**, reports the discrepancies between them and
within each of them, and drafts **RFIs (Requests For Information)** the
engineer can edit and send to the consultant.

Each side is a *set* of files - drawings, BOQ, specifications, schedules,
sketches - labelled with a stage name the user types in (`Tender BOQ + Specs`,
`IFC Structural`, `Addendum No. 2`, …). Those labels flow verbatim through
every prompt, every finding, and every RFI.

## What the model learned from real RFIs

The detection and drafting logic is driven by a knowledge base built from two
corpora of real contractor RFIs - 612 RFIs from an infrastructure / mixed-use
project and a multi-building business district (logs, forms, attachments and
consultant replies) - written in project-neutral terms; see
`knowledge/rfi_taxonomy.json` and the per-project measurements in
`knowledge/corpus_stats/`.

The second corpus added plan-vs-schedule mismatches (column, footing, door
schedules), architectural-vs-structural alignment (blockwork vs beams, slab
edges, openings and shafts missing from structure, FFL vs slab levels),
inconsistencies between buildings or floors of the same type, and conflicts
between approved submittals / shop drawings and the IFC or BOQ.

| Observed in the corpus | How the pipeline uses it |
|---|---|
| RFIs fall into ~13 recurring issue types: value conflicts between documents, sheet-vs-sheet inconsistencies, cross-discipline mismatches, physical clashes, level/grading conflicts, missing information, BOQ scope gaps, design changes between revisions, tie-ins/scope boundaries, code/constructability problems... | Every finding carries an `issue_class`; prompts list what to look for and what is **not** a discrepancy; severity/type defaults come from the class |
| 45% electrical, 29% mechanical, 23% architectural, 18% civil/structural | Analysis framework and drawing reader are multi-discipline (single-line and riser diagrams, panel schedules, levels, finishes, scope) |
| Median 2, up to 19 attachments per RFI; 65% have more than one | Each side accepts any number of files, each read and cited by name |
| Only 22% of attachment pages have a text layer | Scanned pages are OCR-read (bilingual Arabic/English); OCR-derived findings are flagged *verify* and never critical |
| Many conflicts are *within* one package (one load fed from two transformers on two SLD sheets) | An internal-consistency pass checks each set against itself |
| Conflicts are tied to a physical place (section, axis, building, panel, tag) | Items are compared only when they can describe the same element at the same place |
| Engineers group related points into one RFI (up to 11 numbered points), quote every source with its reference and value, and flag variation-order exposure | RFIs are consolidated by discipline and area into numbered points in that house style |

The knowledge base is meant to grow: run `tools/analyze_rfi_log.py` on another
project's RFI log to measure its issue mix, and extend the taxonomy cues -
nothing in it is specific to the project the corpus came from.

## Flow

```
receive_input          validate both document sets, normalise their stage labels
      |
analyze_documents      read every file of each set:
      |                  documents -> text (OCR for scanned pages) -> chunked extraction
      |                  drawings  -> rendered, tiled, vision-read against a baseline
      |                               built from the other set's documents
conflict_scan          1. location-aware alignment of the two sets
      |                2. pair review          (value conflicts, mismatches, levels...)
      |                3. internal consistency (each set against itself)
      |                4. interface review     (items sharing a place across files/disciplines)
      |                5. coverage review      (scope gaps, missing information, open items)
      |                6. quantities           (arithmetic, no LLM)
      |                -> calibrate, merge near-duplicates, rank -> register
 (any findings?) --no--> export
      | yes
draft_rfi              group related findings into RFIs with numbered points,
      |                draft in house style, fill the .docx form
export                 discrepancy register + RFI log
```

### Why items are only compared at the same place

Different sheets show different places, and their values legitimately differ.
Two items are paired only when nothing says they are at different places
(building, floor, section, grid, element tag), and two *drawing* items must
positively share an anchor - a tag, place, section, grid or the same sheet
number. A position on the sheet ("bottom-right corner") is never a location.
Without this rule, comparing a fence-foundation sheet with a retaining-wall
sheet produced dozens of false "level datum" conflicts; with it, the two
sheets' geometry is reviewed together for the clash an engineer would raise.

### Why each document is profiled alone

`analyze_documents` reads each file without knowing what the other set says,
so extraction records what a document asserts rather than hunting for
agreement. Drawings are the one exception: they are read against a checklist
of *what to look for* from the other set (never the values).

## Quick start

```bash
pip install -r requirements.txt
```

The agent uses local Ollama models (`nodes/llm_client.py`, `nodes/vision_client.py`):

```bash
ollama serve
```

```bash
python api.py
```

Then open http://localhost:5000.

### Models

| Role | Default | Override (environment variable) |
|---|---|---|
| Text extraction, comparison, drafting | `gemma4:e4b` | `CONFLICTSCAN_TEXT_MODEL` |
| Drawing reading (vision) | `gemma4:e4b` | `CONFLICTSCAN_VISION_MODEL` |
| OCR of scanned pages | first installed of `qwen2.5vl:7b`, `glm-ocr`, else the vision model | `CONFLICTSCAN_OCR_MODEL` |
| "Is this scanned page a drawing?" | `qwen2.5vl` when installed, else the vision model | `CONFLICTSCAN_PAGE_KIND_MODEL` |
| Context window requested from Ollama | 16384 tokens | `CONFLICTSCAN_NUM_CTX` |

The context window is always requested explicitly: Ollama otherwise falls back
to a small default and silently drops the start of long prompts - the
instructions.

## Web interface

The web app is a multi-user workspace. Everyone signs in (the first visitor
creates the first account), and all work is saved on the server under
`data/`, so any scan can be reopened later at any stage.

- **Projects and revisions.** A *project* holds every scan of one job. Each
  scan is a *revision* (Rev 1, Rev 2, ...) that keeps its uploaded files,
  settings, results and your edits. Projects are visible to the whole team
  unless the owner marks them private. Only the owner can delete a project or
  change who can see it.
- **Home dashboard.** Shows stats, *Continue where you left off*, and a
  projects table you can search, filter (mine / shared / pinned / archived)
  and sort.
- **Tabs.** Projects and scans open in browser-style tabs. Your open tabs
  are restored the next time you sign in, on any device. You can drag tabs to
  reorder them, middle-click to close one, and right-click for more. Every
  tab has a link you can share (`#/run/<id>/<stage>`).
- **Five stages per scan:** *Details* (RFI form header) -> *Documents* (name
  each set, drag in files; they upload straight away and show the detected
  type) -> *Scan* (focus / precedence / grouping, a readiness checklist, then
  run) -> *Findings* -> *RFIs*. Everything saves automatically as you type.
- **Background scans.** A scan runs on the server. You get live per-stage
  progress and a log, and you can cancel it. You can close the tab, and you
  get a notification (in the app and on the desktop) when the scan finishes.
  The sidebar lists every scan in progress.
- **Edit results without re-running.**
  - On *Findings*, accept or dismiss each finding, change its severity, and
    add reviewer notes. You can filter by severity, status, scope, issue type
    or text, and use keyboard triage (J/K, A/D/U).
  - On *RFIs*, edit any field (autosaved), set a status (draft / ready /
    issued / closed), add an RFI by hand (or *Draft an RFI* from a finding),
    revert to the AI draft, delete RFIs, regenerate one or all Word
    documents, and download everything as a .zip.
  - *Rebuild register* re-writes the Excel register with your edits. Dismissed
    findings are left out.
- **Edit & re-run.** This makes a new revision with the same files and
  settings already filled in. You change only what you need, and the earlier
  revision stays untouched.
- Also included: Ctrl+K search over projects, scans and actions; `?` lists
  the keyboard shortcuts; dark mode; and a layout that works on a phone.

| Setting (environment variable) | Default | Meaning |
|---|---|---|
| `CONFLICTSCAN_DATA_DIR` | `data/` next to the code | Database, uploads, per-run exports and debug logs, scan scratch space |
| `CONFLICTSCAN_WORKERS` | `1` | Scans run at once. Others queue. The local LLM is the bottleneck, so raise this only with more GPU capacity. |
| `CONFLICTSCAN_ALLOW_SIGNUP` | `1` | `0` = only the very first account can self-register |
| `CONFLICTSCAN_SECRET_KEY` | generated into `data/secret_key` | Session-cookie signing key. Set it explicitly when you run more than one server process. |
| `CONFLICTSCAN_MAX_UPLOAD_MB` | `500` | Request size limit (one upload, all files together) |
| `CONFLICTSCAN_MAX_FILE_MB` | `200` | Largest single file |
| `CONFLICTSCAN_MAX_PDF_PAGES` | `400` | Most pages in one PDF (split bigger ones) |
| `CONFLICTSCAN_MAX_IMAGE_MP` | `200` | Largest image, in megapixels |
| `CONFLICTSCAN_DEBUG_LOGS` | `1` | Keep the drawing reader's diagnostics per scan (`data/runs/<id>/debug`) |
| `CONFLICTSCAN_DEBUG` | `1` | `0` for shared use: the debug reloader restarts the server on file changes, which fails any running scan |

### Security

| Setting (environment variable) | Default | Meaning |
|---|---|---|
| `CONFLICTSCAN_HTTPS` | `0` | `1` whenever the app is served over HTTPS: cookies are marked Secure and HSTS is sent |
| `CONFLICTSCAN_ALLOWED_ORIGINS` | empty | Other web origins allowed to call the API with a user's sign-in (comma-separated). Empty = only this app's own pages |
| `CONFLICTSCAN_IDLE_MINUTES` | `480` | Sign out after this long without activity. Background polling doesn't count |
| `CONFLICTSCAN_SESSION_DAYS` | `14` | Longest a "Keep me signed in" session lasts |
| `CONFLICTSCAN_LOGIN_MAX_FAILS` | `5` | Failed sign-ins per username in 15 minutes before it's locked for 15 minutes (4x per client address) |
| `CONFLICTSCAN_MIN_PASSWORD` | `8` | Minimum password length |

- Every endpoint except `/` and `/api/health` needs a signed-in session.
- State-changing requests carry an anti-forgery token: the page reads it
  from the `cs_csrf` cookie and sends it in the `X-CSRF-Token` header.
  Scripts must do the same.
- Pages are sent with a strict Content-Security-Policy and are never framed.
  The app loads nothing from the internet (no CDN, no web fonts).
- **Uploads are checked twice.**
  - While uploading, the content must match the extension (a renamed `.exe`
    is refused) and the file must be within the size limit.
  - In the background, page count, password protection, unsafe Office/zip
    files and oversized images are checked.
  - A file that fails is marked in red and blocks the scan until it's removed.
  - Files are only ever opened by the background workers, never inside the
    upload request.
- Each scan writes into its own folders: a scratch folder deleted when the
  scan ends, and `data/runs/<id>/exports` and `/debug`. Concurrent scans
  never share an output folder.

The original single-page steps map onto the stages like this:

1. Details - RFI form header fields (all optional).
2. Documents - name each set and **add its files** (several at once, drag and
   drop, or add more later). Each file's type is *Auto* by default and can be
   set to *Document* or *Drawing*. A DXF with the same file name as a PDF is
   read together with it.
3. Scan - optionally a review focus, the contract's order of precedence (used
   as context in proposed solutions, never to decide), and whether to group
   related findings into one RFI (default) or draft one RFI per finding. Then
   run the scan.

Results show each set's files (pages, OCR pages, sheets, items), then each
discrepancy with what every document says **verbatim**, side by side
("<Set A> states" / "<Set B> states"), the issue class, scope (*between sets*
or *within set A/B*) and a *verify* tag on OCR/partly legible evidence. Quotes
are checked against the source text: a document's words are shown only when
they really occur in it, otherwise the statement is marked as paraphrased.

Draft RFIs are short: each numbered point is one sentence naming the element
and the problem, followed by one line per source with its reference and its
words in quotation marks, then one question per point. **Every RFI field is editable**. Edits save as you type. Click *Regenerate Word
document* to write the revised RFI into that run's own exports folder.

## API

Workspace endpoints used by the web app. They need a signed-in session cookie
(see `/api/auth/*`).

| Endpoint | Method | Description |
|---|---|---|
| `/api/auth/status`, `/register`, `/login`, `/logout` | GET/POST | Accounts and sessions |
| `/api/me`, `/api/me/prefs` | PATCH / GET, PUT | Display name and password; open tabs, pins, theme |
| `/api/projects` | GET, POST | Projects you can see, with run summaries / create one |
| `/api/projects/<id>` | GET, PATCH, DELETE | Rename, describe, archive, set visibility / delete |
| `/api/projects/<id>/runs` | POST | New draft revision (settings carried over; `from_run_id` copies files too) |
| `/api/runs/<id>` | GET, PATCH, DELETE | One revision: inputs, files, progress, results, review |
| `/api/runs/<id>/files` | POST | Upload files to set `a` or `b` (`slot`, `files`) |
| `/api/runs/<id>/files/<fid>` | PATCH, DELETE | Change a file's kind or set / remove it |
| `/api/runs/<id>/start`, `/cancel`, `/clone` | POST | Queue the scan / stop it / *Edit & re-run* |
| `/api/runs/<id>/progress` | GET | Light status payload for polling |
| `/api/runs/<id>/findings/<did>` | PATCH | Triage: `status`, `severity`, `note` |
| `/api/runs/<id>/rfis[/<no>]` | POST, PUT, DELETE | Add / edit / delete an RFI; `/revert`, `/docx` sub-routes |
| `/api/runs/<id>/docx-all`, `/register` | POST | Regenerate every RFI document / the register with edits |
| `/api/runs/<id>/exports/<file>`, `/bundle` | GET | Download one output / everything as a .zip |
| `/api/jobs` | GET | Your queued/running scans and ones that finished recently |

| `/api/rfi-template` | GET | Which RFI form is in use, and its `{{tokens}}` |
| `/` | GET | Web interface (no sign-in) |
| `/api/health` | GET | Health check (no sign-in) |

The old unauthenticated single-shot endpoints (`/scan`, `/api/rfi/export`,
`/exports/<file>`) were removed: they let anyone on the network run scans and
download outputs. Scripts use the workspace endpoints instead:

1. Create a project with `POST /api/projects`.
2. Upload files with `POST /api/runs/<id>/files`.
3. Start the scan with `POST /api/runs/<id>/start`.
4. Poll `/api/runs/<id>/progress` until the scan finishes.

A script signs in with `POST /api/auth/login` and sends `X-CSRF-Token` with
every request.

To call the pipeline directly from Python, without the web app, use
`graph.run_conflict_scan(..., output_dir=..., debug_dir=...)`.

## Project structure

```
ConflictScan_RFI_automation/
├── api.py                    # Flask API: accounts, projects, runs, result editing
├── storage.py                # SQLite store: users, prefs, projects, runs, run files
├── jobs.py                   # background scan queue, live progress, cancel, upload checks
├── security.py               # same-origin, CSRF, session timeout, sign-in throttling, headers
├── upload_checks.py          # file type sniffing, size/page limits, unsafe-file detection
├── graph.py                  # LangGraph builder + run_conflict_scan()
├── state.py                  # ConflictScanState, SourceFile, Discrepancy, RFIItem
├── edges.py                  # Conditional-edge routers
│
├── knowledge/
│   ├── rfi_taxonomy.json     # issue classes, cues, non-issues, RFI house style
│   └── corpus_stats/         # measured issue/discipline/attachment mix per corpus
├── tools/
│   └── analyze_rfi_log.py    # measure any project's RFI log against the taxonomy
├── tests/                    # python -m pytest tests -q
│
├── nodes/
│   ├── input_processing.py   # receive_input: validate the two sets, normalise labels
│   ├── file_ingest.py        # classify files, read text (OCR for scans), pair PDF+DXF
│   ├── document_analysis.py  # chunked extraction per file, merged set profiles
│   ├── drawing_analysis.py   # render / tile / vision-read drawings, DXF entities
│   ├── conflict_scan.py      # alignment, pair/internal/interface/coverage review
│   ├── rfi_knowledge.py      # loads the taxonomy, prompt fragments, classification
│   ├── analysis_framework.py # multi-discipline categories, evaluation order
│   ├── rfi_drafting.py       # group findings -> draft RFIs -> filled .docx
│   ├── docx_template.py      # {{token}} substitution that survives Word run splits
│   ├── export.py             # discrepancy register + RFI log (xlsx, CSV fallback)
│   ├── run_paths.py          # per-scan output / debug folders
│   ├── llm_client.py         # Ollama text client
│   └── vision_client.py      # Ollama vision client + OCR
│
├── templates/index.html      # Web interface shell
├── static/app.js, app.css    # Web interface (no build step)
├── templates_rfi/            # The .docx RFI form(s) - drop the client's form here
└── data/                     # conflictscan.db, runs/<run>/inputs|exports|debug, scratch/ (git-ignored)
```

## Outputs

All outputs are per revision, in `data/runs/<run>/` (download them from the
web app, one by one or as a zip).

- `exports/<project>_conflict_scan_<timestamp>.xlsx` — two sheets:
  *Discrepancy Register* (issue class, scope, location, all sources quoted,
  verify flag) and *RFI Log* (laid out like a project RFI log: subject,
  description, block/area, attachments, points). Falls back to CSV when pandas
  or openpyxl are unavailable.
- `exports/<project>_RFI-00n_<subject>.docx` — one file per RFI.
- `exports/RFI-00n_edited.docx` — written when an RFI is edited in the browser
  and regenerated, leaving the original draft intact.
- `debug/drawing_*.json` — everything the drawing reader extracted, per
  drawing file (turn off with `CONFLICTSCAN_DEBUG_LOGS=0`).

## Extending the knowledge base

```bash
python tools/analyze_rfi_log.py "<project RFI log>.xlsx" --rfi-folder "<folder of RFI sub-folders>" --out knowledge/corpus_stats/<project>.json --show-unclassified
```

A project with no central log can be read from each RFI's own form instead:

```bash
python tools/analyze_rfi_log.py --forms --rfi-folder "<folder of RFI folders>" --out knowledge/corpus_stats/<project>.json
```

The tool detects the log's header row (any log with a subject and a
description column works) or the SUBJECT / INFORMATION REQUESTED parts of
each form, classifies each RFI against the taxonomy, and
reports the issue, discipline and attachment mix. Unclassified RFIs point to
cues worth adding to `knowledge/rfi_taxonomy.json`. Keep new cues generic -
describe the kind of problem, not one project's element names.

## Tuning

| Setting | File | Default |
|---|---|---|
| Chunk size / overlap | `nodes/document_analysis.py` | 8,000 / 500 chars |
| OCR pages per file | `CONFLICTSCAN_MAX_OCR_PAGES` | 30 |
| Files per set | `CONFLICTSCAN_MAX_FILES_PER_SET` | 40 |
| Pairs per LLM call | `nodes/conflict_scan.py` | 12 |
| Internal-consistency pairs per set | `nodes/conflict_scan.py` | 48 |
| Interface clusters reviewed | `nodes/conflict_scan.py` | 16 |
| Similarity at which two findings merge | `nodes/conflict_scan.py` | 0.6 |
| Quantity tolerance before a mismatch is raised | `nodes/conflict_scan.py` | 1% |
| Cap on omission review | `nodes/conflict_scan.py` | 120 items |
| Points per RFI | `nodes/rfi_drafting.py` | 8 |
| Response allowance quoted on the RFI | `nodes/rfi_drafting.py` | 7 days |

## Degraded behaviour

Every stage fails soft, so a run always produces something reviewable:

- An LLM chunk call that fails leaves that chunk empty and the run continues.
- A document the LLM yields nothing for falls back to regex extraction of
  `key: value` lines, quantities, and standard references.
- A scanned page the OCR model cannot read is reported in the warnings.
- An RFI (or a point) the model does not return is built from the finding itself.
- Quantity mismatches never depend on the LLM at all.

Warnings raised along the way are surfaced in the response and on screen.

## Troubleshooting

**`ModuleNotFoundError: No module named 'langgraph'`** — the Python that runs
`api.py` does not have the requirements installed. Install them into that
interpreter (`python -m pip install -r requirements.txt`), or point
`.claude/launch.json` / your shell at the venv that has them.

**Port already in use (Windows)**
```cmd
netstat -ano | findstr :5000
```

**OCR text looks wrong** — small local OCR models misread some Arabic. Findings
resting on OCR text are tagged *verify*; check the quoted values against the
scan, or install a stronger OCR model and set `CONFLICTSCAN_OCR_MODEL`.

**A file was read as the wrong type** — set its type to *Document* or
*Drawing* in the file list instead of *Auto*.

**`{{placeholder}}` text appears in a generated RFI** — that token is not one
the drafting node produces. Check it against the table in
`templates_rfi/README.md`.

---

**Version**: 4.0.0
**Framework**: LangGraph
**Backend**: Flask + Python 3.10+ + Ollama
**Platform**: Windows compatible
