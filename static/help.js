/* ConflictScan - help page, first-run tour, "What's new"
 * -----------------------------------------------------------------------
 * The help page is a workspace tab (#/help/main). The tour runs once per
 * person (remembered in their preferences) and can be replayed from Help.
 * "What's new" opens once after each upgrade.
 */
"use strict";
(() => {
const CS = window.CS;
const { $, $$, esc, el, modal, WS, App, savePrefs } = CS;
const VERSION = "6.0";

const NEWS = [
  ["Company sign-in and roles", "Admins create accounts (or connect the company directory). Roles: admin, project manager, engineer, viewer - per project if needed."],
  ["AI fleet", "Each job runs on the local model best at it: OCR, drawing reading, extraction, comparison, a second opinion on every finding, drafting. Admin console -> AI fleet."],
  ["Second opinions", "Findings an independent model doubts are marked 'second opinion: not a conflict' - filter them under Status."],
  ["Revisions that remember", "Edit & re-run keeps your triage, notes, RFI numbers and edited RFI text; the Findings step shows what is new, changed and resolved."],
  ["Replace a file", "Upload a drawing's new version with the &#10227; button - the project's Documents section keeps every version."],
  ["The RFI log", "Number RFIs the house way, approve, issue, record the client's response, and track overdue RFIs. Export to Excel, PDF or Word."],
  ["Comments and notifications", "Comment on findings and RFIs, @mention colleagues, and watch the bell for finished scans, assignments and overdue RFIs."],
  ["Evidence viewer", "'view in document' opens the source page with the quoted words highlighted."],
  ["Bigger uploads", "Drop whole folders or .zip files; large files upload in resumable pieces."],
  ["Trash", "Deleted projects, revisions and RFIs can be restored for 30 days."],
];

const TOUR = [
  ["#new-scan-btn", "Start here", "Create a project and its first scan: name the two document sets, drop their files, run the scan."],
  ["#tabbar", "Tabs", "Every project, scan and the RFI log opens in its own tab - they are remembered for next time."],
  ["#search-trigger", "Find anything", "Ctrl+K searches projects, scans, findings, RFIs and comments, and runs actions."],
  ["#bell-btn", "Notifications", "Finished scans, things assigned to you, mentions and overdue RFIs show up here."],
  ["#help-btn", "Help", "This page, keyboard shortcuts and what's new - any time."],
];

const SECTIONS = [
  ["Scanning two document sets", `<ol><li>Click <strong>+ New scan</strong>, name the project (pick it from the company register if it is there).</li>
    <li><strong>Details</strong>: RFI header fields (all optional, remembered per project).</li>
    <li><strong>Documents</strong>: name Set A and Set B and drop their files - PDF (scanned or not), Word, Excel, CSV, images, DXF; folders and .zip work too.</li>
    <li><strong>Scan</strong>: optional review focus, then <em>Run conflict scan</em>. You can close the tab - you'll be notified.</li></ol>`],
  ["Reviewing findings", `<p>Each finding quotes both sources. <em>view in document</em> opens the page with the words highlighted.
    Accept, dismiss or reopen (keys <kbd>A</kbd> <kbd>D</kbd> <kbd>U</kbd>, move with <kbd>J</kbd> <kbd>K</kbd>), change the severity, add a note,
    assign it, or comment and @mention a colleague. Tick several findings to triage them together. Save a set of filters as a view.</p>
    <p><span class="badge verify">verify</span> means the value was OCR-read or partly legible; <span class="badge disputed">second opinion: not a conflict</span>
    means an independent model doubts it - you decide.</p>`],
  ["RFIs and the RFI log", `<p>The RFIs step holds the drafted RFIs - every field is editable and saves as you type. <em>Regenerate Word document</em> fills the
    project's form; PDF needs no Office. <strong>Add to RFI log</strong> gives the RFI the project's next number and starts its journey:
    draft &rarr; review &rarr; approved &rarr; issued &rarr; response &rarr; closed. Managers approve and issue; anyone on the project records the response.</p>`],
  ["Revisions", `<p><strong>Edit &amp; re-run</strong> makes the next revision with the same files and settings. Add, remove or replace (&#10227;) files - unchanged
    files are not uploaded or re-read. When it finishes, your triage, notes and RFI numbers carry over, and changes are listed (new / changed / resolved).
    <em>Compare&hellip;</em> shows any two revisions side by side.</p>`],
  ["Projects and people", `<p>Team projects are visible to everyone; private ones to their members. A project's <strong>Members</strong> dialog sets each person's role
    there: manager, engineer or viewer. <strong>Settings</strong> holds the RFI header defaults, numbering and the client's RFI form.</p>`],
  ["Keyboard", `<p><kbd>Ctrl</kbd>+<kbd>K</kbd> search &middot; <kbd>Alt</kbd>+<kbd>N</kbd> new scan &middot; <kbd>Alt</kbd>+<kbd>1..9</kbd> switch tab &middot;
    <kbd>Alt</kbd>+<kbd>W</kbd> close tab &middot; <kbd>1..5</kbd> scan steps &middot; <kbd>Ctrl</kbd>+<kbd>Enter</kbd> run the scan &middot; <kbd>?</kbd> all shortcuts</p>`],
  ["Privacy", `<p>Everything stays on the company's servers: documents, results and the AI models. Nothing is sent to the internet.</p>`],
];

class HelpView {
  static canRestore() { return true; }
  constructor() { this.key = "help:main"; this.el = el(`<div class="view"><div class="view-inner" data-inner></div></div>`); }
  tabTitle() { return "Help"; }
  hash() { return "#/help/main"; }
  async load() {
    $("[data-inner]", this.el).innerHTML = `<div class="page-head"><div class="grow"><h1 class="title-input" style="border:none">Help</h1>
        <div class="meta">ConflictScan compares two document sets, finds the conflicts and drafts the RFIs.</div></div>
        <div class="head-actions"><button class="btn" data-tour>Take the tour</button><button class="btn" data-news>What's new</button></div></div>
      ${SECTIONS.map(([t, b]) => `<section class="card help-sec"><h3>${t}</h3>${b}</section>`).join("")}`;
    $("[data-tour]", this.el).onclick = () => tour();
    $("[data-news]", this.el).onclick = () => whatsNew();
  }
}

function whatsNew() {
  modal({ title: `What's new in ConflictScan ${VERSION}`, wide: true,
    body: `<ul class="news">${NEWS.map(([t, b]) => `<li><strong>${t}</strong><br><span class="muted">${b}</span></li>`).join("")}</ul>`,
    actions: [{ label: "Got it", primary: true }], onClose: () => savePrefs({ ui: { ...(App.prefs.ui || {}), seen_version: VERSION } }) });
}

function tour(step = 0) {
  $$(".tour-tip, .tour-ring").forEach((n) => n.remove());
  const visible = TOUR.filter(([sel]) => { const n = $(sel); return n && n.getBoundingClientRect().width; });
  if (step >= visible.length) { savePrefs({ tour: { done: true, at: new Date().toISOString() } }); return; }
  const [sel, title, text] = visible[step];
  const r = $(sel).getBoundingClientRect();
  const ring = el(`<div class="tour-ring"></div>`);
  Object.assign(ring.style, { top: `${r.top - 4}px`, left: `${r.left - 4}px`, width: `${r.width + 8}px`, height: `${r.height + 8}px` });
  const tip = el(`<div class="tour-tip" role="dialog"><div class="small muted">${step + 1} of ${visible.length}</div><strong>${esc(title)}</strong><p>${esc(text)}</p>
    <div class="row"><button class="btn sm ghost" data-skip>Skip</button><span class="grow"></span><button class="btn sm primary" data-next>${step + 1 < visible.length ? "Next" : "Done"}</button></div></div>`);
  document.body.append(ring, tip);
  const below = r.bottom + 220 < innerHeight;
  tip.style.top = `${below ? r.bottom + 12 : Math.max(8, r.top - tip.offsetHeight - 12)}px`;
  tip.style.left = `${Math.min(Math.max(8, r.left), innerWidth - tip.offsetWidth - 8)}px`;
  tip.querySelector("[data-next]").onclick = () => tour(step + 1);
  tip.querySelector("[data-skip]").onclick = () => tour(99);
}

CS.VIEW_TYPES.help = HelpView;
CS.PALETTE_ITEMS.push(() => [{ label: "Help", run: () => WS.open("help", "main") }, { label: "Take the tour", run: () => tour() },
  { label: "What's new", run: () => whatsNew() }]);
CS.BOOT_HOOKS.push(() => {
  $("#help-btn").onclick = () => WS.open("help", "main");
  $("#help-btn").title = "Help (? for shortcuts)";
  setTimeout(() => {
    if (!(App.prefs.tour || {}).done) tour();
    else if ((App.prefs.ui || {}).seen_version !== VERSION) whatsNew();
  }, 900);
});
})();
