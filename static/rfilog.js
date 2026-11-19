/* ConflictScan - project RFI log
 * -----------------------------------------------------------------------
 * One tab per project (#/rfilog/<project id>): every RFI with its house
 * number, status, due date and response. Draft -> in review -> approved ->
 * issued -> response received -> closed. Export to Excel, import an existing
 * log, PDF / Word per RFI, record the client's response, start a new
 * revision from it.
 */
"use strict";
(() => {
const { $, $$, esc, el, api, toast, toastError, modal, confirmDialog, timeEl, fmtDate, canRole, WS, App, uploadFiles, debounce } = window.CS;

const ST_CLASS = { draft: "st-draft", in_review: "st-queued", approved: "st-running", issued: "st-cancelled", responded: "st-complete", closed: "st-complete", void: "outline" };
const ACTION_LABEL = { in_review: "Submit for review", approved: "Approve", issued: "Mark as issued", responded: "Mark response received",
  closed: "Close", void: "Void", draft: "Back to draft" };
const NEED = { in_review: "engineer", approved: "manager", issued: "manager", responded: "engineer", closed: "manager", void: "manager", draft: "engineer" };
const fmtDay = (iso) => iso ? new Date(iso + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";

class RfiLogView {
  static canRestore(id) { return App.projectIndex.has(id); }
  constructor(projectId) {
    this.id = projectId;
    this.key = `rfilog:${projectId}`;
    this.filter = { q: "", status: "all", overdue: false };
    this.el = el(`<div class="view"><div class="view-inner" data-inner></div></div>`);
    this.el.addEventListener("click", (e) => this.onClick(e));
  }
  project() { return App.projectIndex.get(this.id) || {}; }
  tabTitle() { return `RFI log · ${this.project().name || "Project"}`; }
  hash() { return `#/rfilog/${this.id}`; }
  projectId() { return this.id; }
  async load() { this.data = await api(`/api/projects/${this.id}/rfi-log`); this.render(); }
  async reload() { await this.load(); }
  role() { return this.data.my_role; }

  render() {
    const p = this.project();
    const c = this.data.counts;
    const manage = canRole(this.role(), "manager");
    $("[data-inner]", this.el).innerHTML = `
      <div class="page-head"><div class="grow">
          <div class="crumbs"><a href="#/" data-home>Projects</a><span>&rsaquo;</span><a href="#/project/${esc(this.id)}" data-project>${esc(p.name || "")}</a><span>&rsaquo;</span></div>
          <h1 class="title-input" style="border:none">RFI log</h1>
          <div class="meta">Next number: <strong class="mono">${esc(this.data.next_number)}</strong></div></div>
        <div class="head-actions">
          <a class="btn" href="/api/projects/${esc(this.id)}/rfi-log.xlsx">Export to Excel</a>
          ${manage ? `<label class="btn"><input type="file" accept=".xlsx,.xlsm" hidden data-import>Import existing log</label>` : ""}
          ${canRole(this.role(), "engineer") ? `<button class="btn primary" data-new>+ New RFI</button>` : ""}
        </div></div>
      <div class="stat-row">${["draft", "in_review", "approved", "issued", "responded", "closed"].map((s) =>
        `<button class="stat-chip ${this.filter.status === s ? "on" : ""}" data-st="${s}"><span class="n">${c[s] || 0}</span>${esc(this.data.statuses.find((x) => x.key === s).label)}</button>`).join("")}
        <button class="stat-chip danger ${this.filter.overdue ? "on" : ""}" data-overdue><span class="n">${c.overdue || 0}</span>Overdue</button></div>
      <div class="filters" style="position:static"><div class="search-box"><input type="search" data-q placeholder="Search number, subject, response" value="${esc(this.filter.q)}"></div>
        ${this.filter.status !== "all" || this.filter.overdue ? `<button class="btn sm ghost" data-clear>Show all</button>` : ""}</div>
      <div data-table></div>`;
    this.renderTable();
    const q = $("[data-q]", this.el);
    q.addEventListener("input", debounce(() => { this.filter.q = q.value; this.renderTable(); }, 150));
    const imp = $("[data-import]", this.el);
    if (imp) imp.addEventListener("change", () => this.importLog(imp.files[0]));
  }

  rows() {
    const q = this.filter.q.toLowerCase();
    return this.data.entries.filter((e) => (this.filter.status === "all" || e.status === this.filter.status)
      && (!this.filter.overdue || e.overdue)
      && (!q || `${e.log_no} ${e.subject} ${e.response || ""} ${e.discipline || ""}`.toLowerCase().includes(q)));
  }

  renderTable() {
    const rows = this.rows();
    $("[data-table]", this.el).innerHTML = rows.length ? `<div class="table-wrap"><table class="list"><thead><tr>
      <th>RFI No.</th><th>Subject</th><th>Discipline</th><th>Status</th><th>Issued</th><th>Due</th><th>Response</th><th>Assigned to</th></tr></thead><tbody>
      ${rows.map((e) => `<tr data-entry="${esc(e.id)}" class="${e.overdue ? "overdue" : ""}">
        <td class="mono nowrap"><strong>${esc(e.log_no)}</strong>${e.source === "imported" ? '<div class="desc">imported</div>' : ""}</td>
        <td><div class="name">${esc(e.subject)}</div></td>
        <td class="small">${esc((e.discipline || "").replace(/^\w/, (m) => m.toUpperCase()))}</td>
        <td class="nowrap"><span class="badge ${ST_CLASS[e.status] || ""}">${esc(e.status_label)}</span></td>
        <td class="small nowrap">${e.issued_at ? timeEl(e.issued_at) : ""}</td>
        <td class="small nowrap">${e.due_date ? `${fmtDay(e.due_date)}${e.overdue ? ` <span class="badge st-failed">overdue</span>` : e.days_left != null ? ` <span class="muted">(${e.days_left}d)</span>` : ""}` : ""}</td>
        <td class="small">${e.response ? esc(e.response.slice(0, 90)) + (e.response.length > 90 ? "&hellip;" : "") : e.status === "responded" ? "files only" : ""}</td>
        <td class="small">${esc(e.assignee_name || "")}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="card empty"><h3>${this.data.entries.length ? "No RFIs match" : "No RFIs in the log yet"}</h3>
        <p class="hint">Add RFIs from a scan's RFI step (<em>Add to RFI log</em>), create one by hand, or import the project's existing log.</p></div>`;
  }

  async importLog(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await uploadFiles(`/api/projects/${this.id}/rfi-log/import`, fd, () => {});
      toast(`Imported ${r.imported} RFI(s)${r.skipped ? `, ${r.skipped} already in the log` : ""}. Numbering continues after them.`, { type: "success" });
      this.reload();
    } catch (err) { toastError(err); }
  }

  newDialog() {
    modal({
      title: "New RFI", body: `<p class="hint">For an issue the scans did not raise. It gets the next number (${esc(this.data.next_number)}) and starts as a draft.</p>
        <label class="field"><span>Subject</span><input name="subject"></label>
        <div class="form-grid two"><label class="field"><span>Discipline</span><select name="discipline">${["architectural", "structural", "civil", "electrical", "mechanical", "plumbing", "fire", "general"]
          .map((d) => `<option value="${d}">${d[0].toUpperCase() + d.slice(1)}</option>`).join("")}</select></label>
          <label class="field"><span>Area / location</span><input name="area"></label></div>
        <label class="field"><span>Information requested</span><textarea name="question" rows="4"></textarea></label>`,
      actions: [{ label: "Cancel" }, { label: "Create RFI", primary: true, onClick: async (b) => {
        const v = (n) => b.querySelector(`[name=${n}]`).value.trim();
        const e = await api(`/api/projects/${this.id}/rfi-log`, { method: "POST", body: { subject: v("subject"), discipline: v("discipline"), area: v("area"), question: v("question") } });
        await this.reload();
        this.openEntry(e.id);
      } }],
    });
  }

  async openEntry(id) {
    let e;
    try { e = await api(`/api/rfi-log/${id}`); } catch (err) { toastError(err); return; }
    const users = await api("/api/users").catch(() => []);
    const c = e.content || {};
    const editable = ["draft", "in_review"].includes(e.status) && canRole(this.role(), "engineer");
    const field = (k, label, rows = 3) => editable ? `<label class="field"><span>${label}</span><textarea data-c="${k}" rows="${rows}">${esc(Array.isArray(c[k]) ? c[k].join("\n") : c[k] || "")}</textarea></label>`
      : (c[k] ? `<div class="field-label">${label}</div><div class="prev-val">${esc(Array.isArray(c[k]) ? c[k].join("\n") : c[k])}</div>` : "");
    const acts = (e.allowed || []).filter((s) => canRole(this.role(), NEED[s]));
    const { close } = modal({
      title: `${e.log_no}`, wide: true,
      body: `<div class="row wrap" style="gap:8px;margin-bottom:10px"><span class="badge ${ST_CLASS[e.status]}">${esc(e.status_label)}</span>
          ${e.overdue ? '<span class="badge st-failed">overdue</span>' : ""}
          ${e.approved_by_name ? `<span class="small muted">approved by ${esc(e.approved_by_name)} ${timeEl(e.approved_at)}</span>` : ""}
          ${e.issued_by_name ? `<span class="small muted">issued by ${esc(e.issued_by_name)} ${timeEl(e.issued_at)}</span>` : ""}
          ${e.run_id ? `<a class="small" href="#/run/${esc(e.run_id)}/rfis" data-close>from ${esc(e.run_rfi_no || "")} of its revision</a>` : ""}</div>
        <div class="form-grid two">
          <label class="field"><span>Due date</span><input type="date" data-due value="${esc(e.due_date || "")}" ${canRole(this.role(), "engineer") ? "" : "disabled"}></label>
          <label class="field"><span>Assigned to</span><select data-assignee ${canRole(this.role(), "engineer") ? "" : "disabled"}><option value="">-</option>
            ${users.map((u) => `<option value="${esc(u.id)}" ${u.id === e.assignee_id ? "selected" : ""}>${esc(u.display_name)}</option>`).join("")}</select></label></div>
        ${editable ? `<label class="field"><span>Subject</span><input data-c="subject" value="${esc(c.subject || e.subject)}"></label>` : `<h3 style="margin:6px 0">${esc(e.subject)}</h3>`}
        ${field("background", "Background", 2)}${field("discrepancy_summary", "Discrepancies", 4)}${field("question", "Information requested", 4)}${field("proposed_solution", "Proposed solution", 2)}
        ${["issued", "responded", "closed"].includes(e.status) ? `<div class="card response-box"><h4 style="margin:0 0 6px">Client response</h4>
          ${e.response ? `<div class="prev-val">${esc(e.response)}</div>` : ""}
          ${(e.files || []).map((f) => `<div><a href="/api/rfi-log/${esc(e.id)}/files/${esc(f.id)}">&#128206; ${esc(f.name)}</a></div>`).join("")}
          ${e.status !== "closed" && canRole(this.role(), "engineer") ? `<label class="field"><span>${e.response ? "Add to the response" : "Response text"}</span><textarea data-response rows="3"></textarea></label>
            <div class="row" style="gap:8px"><input data-response-from placeholder="From (e.g. consultant)" style="max-width:240px">
            <input type="file" multiple data-response-files><button class="btn primary sm" data-save-response>Save response</button></div>` : ""}
          ${e.status === "responded" && canRole(this.role(), "engineer") ? `<p class="hint" style="margin-top:10px">Does the response change the documents?
            <button class="btn sm" data-new-rev>Start a new revision with the response files</button></p>` : ""}</div>` : ""}`,
      actions: [
        { label: "PDF", onClick: () => { location.href = `/api/rfi-log/${e.id}/pdf`; return false; } },
        { label: "Word", onClick: () => { location.href = `/api/rfi-log/${e.id}/docx`; return false; } },
        ...(editable && e.run_id ? [{ label: "Update from latest revision", onClick: async () => { await api(`/api/rfi-log/${e.id}/sync`, { method: "POST" }); toast("Updated from the latest revision", { type: "success" }); this.reload(); } }] : []),
        ...acts.map((s) => ({ label: ACTION_LABEL[s] || s, primary: s === acts[0] && s !== "void", danger: s === "void",
          onClick: async (b) => {
            if (s === "void" && !(await confirmDialog(`Void ${e.log_no}? It stays in the log, marked void.`, { title: "Void RFI", confirmLabel: "Void", danger: true }))) return false;
            const body = { status: s };
            if (editable) body.content = Object.fromEntries($$("[data-c]", b).map((n) => [n.dataset.c, n.value]));
            await api(`/api/rfi-log/${e.id}`, { method: "PATCH", body });
            toast(`${e.log_no}: ${ACTION_LABEL[s] || s}`, { type: "success" });
            this.reload();
          } })),
        ...(editable ? [{ label: "Save", onClick: async (b) => {
          await api(`/api/rfi-log/${e.id}`, { method: "PATCH", body: { content: Object.fromEntries($$("[data-c]", b).map((n) => [n.dataset.c, n.value])) } });
          toast("Saved", { type: "success", timeout: 1500 }); this.reload();
        } }] : []),
      ],
      onOpen: (b) => {
        const patch = async (body) => { try { await api(`/api/rfi-log/${e.id}`, { method: "PATCH", body }); this.reload(); } catch (err) { toastError(err); } };
        const due = $("[data-due]", b);
        due.addEventListener("change", () => patch({ due_date: due.value }));
        const asg = $("[data-assignee]", b);
        asg.addEventListener("change", () => patch({ assignee_id: asg.value }));
        const close1 = b.querySelector("[data-close]");
        if (close1) close1.addEventListener("click", () => close());
        const save = $("[data-save-response]", b);
        if (save) save.addEventListener("click", async () => {
          const fd = new FormData();
          fd.append("response", $("[data-response]", b).value);
          fd.append("response_from", $("[data-response-from]", b).value);
          [...$("[data-response-files]", b).files].forEach((f) => fd.append("files", f));
          try { await uploadFiles(`/api/rfi-log/${e.id}/response`, fd, () => {}); toast(`Response recorded for ${e.log_no}`, { type: "success" }); close(); this.reload(); this.openEntry(e.id); }
          catch (err) { toastError(err); }
        });
        const nr = $("[data-new-rev]", b);
        if (nr) nr.addEventListener("click", async () => {
          try {
            const r = await api(`/api/rfi-log/${e.id}/new-revision`, { method: "POST", body: { slot: "b" } });
            close();
            await window.CS.refreshProjects();
            WS.open("run", r.run_id, { step: "documents" });
            toast(`Rev ${r.seq} created with ${r.files_added} response file(s) in Set B. Check the files, then run the scan.`, { type: "success", timeout: 8000 });
          } catch (err) { toastError(err); }
        });
      },
    });
  }

  onClick(e) {
    const t = e.target;
    if (t.closest("[data-home]")) { e.preventDefault(); WS.activate("home"); return; }
    if (t.closest("[data-project]")) { e.preventDefault(); WS.open("project", this.id); return; }
    const st = t.closest("[data-st]");
    if (st) { this.filter.status = this.filter.status === st.dataset.st ? "all" : st.dataset.st; this.filter.overdue = false; this.render(); return; }
    if (t.closest("[data-overdue]")) { this.filter.overdue = !this.filter.overdue; this.filter.status = "all"; this.render(); return; }
    if (t.closest("[data-clear]")) { this.filter = { q: "", status: "all", overdue: false }; this.render(); return; }
    if (t.closest("[data-new]")) { this.newDialog(); return; }
    const row = t.closest("[data-entry]");
    if (row) this.openEntry(row.dataset.entry);
  }
}

window.CS.VIEW_TYPES.rfilog = RfiLogView;
window.CS.PROJECT_ACTIONS.unshift({ key: "rfilog", label: "&#128203; RFI log", run: (p) => WS.open("rfilog", p.id) });
window.CS.PROJECT_MENU_ITEMS.push((p) => ({ label: "Open the RFI log", onClick: () => WS.open("rfilog", p.id) }));
window.CS.PALETTE_ITEMS.push(() => App.projects.filter((p) => !p.archived).map((p) => ({ label: `RFI log: ${p.name}`, run: () => WS.open("rfilog", p.id) })));
window.CS.openRfiLogEntry = async (projectId, entryId) => {
  const view = WS.open("rfilog", projectId);
  await WS.ensureLoaded(view);
  view.openEntry(entryId);
};
})();
