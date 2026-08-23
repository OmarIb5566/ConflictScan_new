# ConflictScan / RFI Automation

A LangGraph agent that compares **any two documents from different stages of
the same project**, reports the discrepancies between them, and drafts an
**RFI (Request For Information)** for each one so the engineer can send it to
the client for clarification.

The pipeline is document-agnostic. It has no notion of BOQs, specifications, or
material lists — the two inputs are just "document A" and "document B", each
carrying a stage label the user types in (`Tender BOQ`, `IFC Specifications`,
`100% DD Drawings`, `Addendum No. 2`, …). Those labels flow verbatim through
every prompt, every finding, and every RFI.

## Flow

```
receive_input          validate both documents, normalise their stage labels
      |
analyze_documents      chunk and profile EACH document independently
      |                (every chunk is sent to the LLM - nothing is sampled)
conflict_scan          compare the two profiles -> discrepancy register
      |
 (any findings?) --no--> export
      | yes
draft_rfi              one draft RFI per discrepancy, filled into the .docx form
      |
export                 discrepancy register + RFI log
      |
     END
```

### Why each document is profiled alone

`analyze_documents` reads each document without knowing what the other says.
That keeps extraction honest: the model records what a document asserts rather
than hunting for agreement with its counterpart. Comparison is a separate,
explicit step.

### How the comparison works

`conflict_scan` runs four passes:

1. **Alignment** — requirements from A and B are paired by keyword overlap on
   topic and statement.
2. **Pair review** — candidate pairs go to the LLM in batches; it decides which
   pairs genuinely conflict and writes the finding with both sides quoted.
   Clause references are re-attached from our own records, not from the model.
3. **Coverage review** — requirements present in only one document are reviewed
   as potential omissions, in both directions.
4. **Quantities** — quantity items matched by description are compared
   arithmetically. Amount deviations beyond 1% and unit-of-measure mismatches
   are raised **without an LLM call**, so those findings are exact and
   reproducible.

Findings are de-duplicated, ranked critical → major → minor, and numbered
`D-001`, `D-002`, …

The scan summary is a factual tally computed from the register itself. The
pipeline deliberately does **not** generate a unified prose description of the
project — the deliverable is the discrepancy register and the RFIs.

### RFI drafting

`draft_rfi` turns each discrepancy into a formal RFI. The model is instructed
to **ask, never decide**: an RFI sets out both positions neutrally and puts one
answerable question to the client. It never asserts which document governs.

Each RFI is then written into the project's own `.docx` RFI form by `{{token}}`
substitution, so the client's letterhead, tables, and footer survive untouched.
See [templates_rfi/README.md](templates_rfi/README.md) for the token list and
how to install your own form.

If no form is present, drafting still runs — the RFI text appears on screen for
review and editing, and only the Word output is skipped.

## Quick start

```bash
pip install -r requirements.txt
```

The agent uses a local Ollama model (`qwen3:14b` by default — see
`nodes/llm_client.py`):

```bash
ollama serve
```

```bash
python api.py
```

Then open http://localhost:5000.

## Web interface

1. Enter the project name.
2. Fill both document slots — upload a file (PDF / Word / Excel / TXT) or paste
   text, and give each one a stage label.
3. Optionally set a review focus to narrow what the scan looks at.
4. Run the scan.

Results show the discrepancy register side by side (what A says vs what B
says, with references), followed by the draft RFIs. **Every RFI field is
editable in the browser** — edit it, click *Regenerate Word document*, and the
revised RFI is written to `exports/`.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Web interface |
| `/api/health` | GET | Health check, including whether an RFI form is loaded |
| `/scan` | POST | Run a conflict scan over two documents |
| `/api/rfi-template` | GET | Which RFI form is in use, and its `{{tokens}}` |
| `/api/rfi-template` | POST | Upload the project's `.docx` RFI form |
| `/api/rfi/export` | POST | Regenerate one RFI `.docx` from edited fields |
| `/exports/<filename>` | GET | Download a register or an RFI document |

### `/scan` form fields

| Field | Notes |
|---|---|
| `document_a_file` *or* `document_a_text` | One of the two is required |
| `document_a_label` | Stage label, e.g. `Tender BOQ` |
| `document_b_file` *or* `document_b_text` | One of the two is required |
| `document_b_label` | Stage label, e.g. `IFC Specifications` |
| `project_name` | Optional |
| `review_focus` | Optional free text steering the scan |
| `user_id` | Optional; recorded as the RFI originator |

```bash
curl -X POST http://localhost:5000/scan \
  -F "project_name=Borg El Arab" \
  -F "document_a_label=Tender BOQ" \
  -F "document_a_file=@trial/trial_boq.pdf" \
  -F "document_b_label=IFC Specifications" \
  -F "document_b_file=@trial/trial_specs.pdf"
```

## Project structure

```
ConflictScan_RFI_automation/
├── api.py                    # Flask API; owns all file reading (PDF/DOCX/XLSX/TXT)
├── graph.py                  # LangGraph builder + run_conflict_scan()
├── state.py                  # ConflictScanState, DocumentProfile, Discrepancy, RFIItem
├── edges.py                  # Conditional-edge routers
│
├── nodes/
│   ├── input_processing.py   # receive_input: validate the pair, normalise labels
│   ├── document_analysis.py  # chunking + per-document profiling
│   ├── conflict_scan.py      # cross-document comparison -> discrepancy register
│   ├── rfi_drafting.py       # discrepancy -> draft RFI -> filled .docx
│   ├── docx_template.py      # {{token}} substitution that survives Word run splits
│   ├── export.py             # discrepancy register + RFI log (xlsx, CSV fallback)
│   └── llm_client.py         # Ollama client
│
├── templates/index.html      # Web interface
├── templates_rfi/            # The .docx RFI form(s) - drop the client's form here
├── exports/                  # Generated registers and RFI documents
└── uploads/                  # Transient upload scratch space
```

## Outputs

- `exports/<project>_conflict_scan_<timestamp>.xlsx` — two sheets:
  *Discrepancy Register* and *RFI Log*. Falls back to CSV when pandas or
  openpyxl are unavailable.
- `exports/<project>_RFI-00n_<subject>.docx` — one file per RFI.
- `exports/RFI-00n_edited.docx` — written when an RFI is edited in the browser
  and regenerated, leaving the original draft intact.

## Tuning

| Setting | File | Default |
|---|---|---|
| Chunk size / overlap | `nodes/document_analysis.py` | 8,000 / 500 chars |
| Pairs per LLM call | `nodes/conflict_scan.py` | 12 |
| Keyword overlap to call two topics related | `nodes/conflict_scan.py` | 2 |
| Quantity tolerance before a mismatch is raised | `nodes/conflict_scan.py` | 1% |
| Cap on omission review | `nodes/conflict_scan.py` | 120 items |
| RFIs per LLM call | `nodes/rfi_drafting.py` | 6 |
| Response allowance quoted on the RFI | `nodes/rfi_drafting.py` | 7 days |
| Model | `nodes/llm_client.py` | `qwen3:14b` |

## Degraded behaviour

Every stage fails soft, so a run always produces something reviewable:

- An LLM chunk call that fails leaves that chunk empty and the run continues.
- A document the LLM yields nothing for falls back to regex extraction of
  `key: value` lines, quantities, and standard references.
- An RFI the model does not return is built from the discrepancy record itself.
- Quantity mismatches never depend on the LLM at all.

Warnings raised along the way are surfaced in the response and on screen.

## Troubleshooting

**Port already in use (Windows)**
```cmd
netstat -ano | findstr :5000
```

**No text extracted from a PDF** — it is probably a scan with no text layer.
The scan warns when a document yields no requirements or quantities.

**`{{placeholder}}` text appears in a generated RFI** — that token is not one
the drafting node produces. Check it against the table in
`templates_rfi/README.md`.

---

**Version**: 3.0.0
**Framework**: LangGraph
**Backend**: Flask + Python 3.10+ + Ollama
**Platform**: Windows compatible
