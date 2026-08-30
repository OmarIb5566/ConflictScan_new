# RFI form template

`F-PQP-01-03 Request For Information.docx` is ROWAD's official RFI form and is
used for every scan. Every discrepancy the scan raises is written into a copy
of it, one file per RFI, saved to `exports/`.

The form is never redrawn. Only `{{token}}` placeholders (added into the
relevant cells of the form) are replaced, so the letterhead, logo, table
borders, fonts, and footer survive exactly as they are.

## Changing the form

1. Open `F-PQP-01-03 Request For Information.docx` in Word.
2. Edit the `{{token_name}}` placeholders already in place, or add new ones
   wherever a value should be filled in (see the table below for the
   available names).
3. Save it back into this folder under the same filename.

If several `.docx` files are present, `F-PQP-01-03 Request For Information.docx`
wins; otherwise the first file alphabetically is used.

Placeholders may appear anywhere: body paragraphs, table cells, nested tables,
headers, and footers are all substituted. Word often splits a typed string
across several internal runs, so the filler joins each paragraph before
substituting — a token still resolves even if Word has fragmented it.

## Available tokens

| Token | Filled with |
|---|---|
| `{{rfi_no}}` | Sequential number for this scan, `RFI-001`, `RFI-002`, … |
| `{{rfi_date}}` | Date the RFI was generated, e.g. `20 August 2026` |
| `{{response_required_by}}` | Date the reply is requested by (7 days by default) |
| `{{project_name}}` | Project name entered on the scan form |
| `{{consultant}}` | Consultant name entered on the scan form |
| `{{employer}}` | Employer name entered on the scan form |
| `{{project_code}}` | Project code entered on the scan form |
| `{{to}}` | RFI recipient entered on the scan form |
| `{{location}}` | Location entered on the scan form |
| `{{boq_no}}` | BOQ number entered on the scan form |
| `{{dwg_no}}` | Drawing number entered on the scan form |
| `{{level}}` | Level entered on the scan form |
| `{{specs_no}}` | Specs number entered on the scan form |
| `{{originator}}` | User who ran the scan |
| `{{discrepancy_id}}` | Register reference for the underlying finding, `D-001` |
| `{{subject}}` | One-line subject of the RFI |
| `{{discipline}}` | civil / structural / architectural / mechanical / electrical / plumbing / contractual / general |
| `{{priority}}` | high / medium / low, derived from the discrepancy severity |
| `{{document_a_label}}` | Stage label of the first document, e.g. `Tender BOQ` |
| `{{document_a_reference}}` | Clause, section, or item reference quoted in that document |
| `{{document_b_label}}` | Stage label of the second document, e.g. `IFC Specifications` |
| `{{document_b_reference}}` | Clause, section, or item reference quoted in that document |
| `{{background}}` | Context: which documents are involved and why it matters |
| `{{discrepancy_summary}}` | Both positions stated neutrally |
| `{{question}}` | The single clarification put to the client |
| `{{proposed_solution}}` | Contractor's suggested way forward, offered for approval |
| `{{cost_impact}}` | Cost consequence, or "To be assessed on receipt of clarification" |
| `{{schedule_impact}}` | Programme consequence, or the same fallback |

A token your form does not use is simply ignored. A token in your form that the
drafting node does not produce is replaced with an empty string, so no raw
`{{placeholder}}` text ever reaches the client.

## Discipline checkboxes

The form's Discipline row uses real Word checkbox content controls, not text
tokens. `fill_docx_template()` ticks the one matching `{{discipline}}` (Survey
/ Civil / Structural / Electrical / Mechanical / Plumbing / Arch / Other,
matched in that order) and leaves the rest unchecked - `architectural` maps to
`Arch`, anything unrecognised maps to `Other`. This only touches the eight
checkboxes already in the form; it does not require or add any token.

## Files here

- `F-PQP-01-03 Request For Information.docx` — ROWAD's official RFI form,
  with `{{token}}` placeholders already inserted into it.
