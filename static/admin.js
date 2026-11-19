/* ConflictScan admin console
 * -----------------------------------------------------------------------
 * Users and roles, the scan queue, system health (database, file store,
 * sign-in source, reference data, model servers, GPUs), the audit log and
 * the effective settings. Admin-only; opens as a workspace tab (#/admin/users).
 */
"use strict";
(() => {
const { $, $$, esc, el, api, toast, toastError, modal, confirmDialog, timeEl, fmtDate, avatar, ROLE_LABEL, isAdmin, WS, App } = window.CS;

const SECTIONS = [["users", "Users"], ["queue", "Scan queue"], ["fleet", "AI fleet"], ["health", "System health"], ["audit", "Audit log"], ["settings", "Settings"]];
const ROLE_HELP = {
  page_triage: "Is a scanned page a drawing or a document?", ocr: "Reads scanned pages",
  drawing_overview: "Title block, system and grids of each sheet", drawing_reader: "Reads values off each drawing tile",
  drawing_second: "Independent second reading of drawing values", extractor: "Pulls requirements and quantities out of documents",
  adjudicator: "Decides which candidate pairs really conflict", verifier: "Second opinion on every finding",
  drafter: "Writes the RFIs", embedder: "Meaning-based matching and the RFI memory",
};
const STATUS_LABEL = { active: "Active", pending: "Waiting for approval", disabled: "Disabled" };

class AdminView {
  static canRestore() { return isAdmin(); }
  constructor() {
    this.section = "users";
    this.key = "admin:main";
    this.timer = null;
    this.auditFilter = { action: "", user: "" };
    this.el = el(`<div class="view"><div class="view-inner">
      <div class="page-head"><div class="grow"><div class="crumbs"><span>Administration</span></div><h1 class="title-input" style="border:none">Admin console</h1></div></div>
      <nav class="subtabs" data-subtabs></nav><div data-body></div></div></div>`);
    this.el.addEventListener("click", (e) => this.onClick(e));
  }
  tabTitle() { return "Admin"; }
  hash() { return `#/admin/main/${this.section}`; }
  setInitialStep(step) { if (SECTIONS.some(([k]) => k === step)) { this.section = step; if (this.loaded) this.render(); } }
  async load() { this.render(); }
  async reload() { this.render(); }
  onShow() { this.sync(); }
  onHide() { clearInterval(this.timer); this.timer = null; }
  destroy() { this.onHide(); }
  sync() {
    clearInterval(this.timer);
    this.timer = null;
    if (WS.active === this.key && (this.section === "queue" || this.section === "health")) {
      this.timer = setInterval(() => this.renderBody(true), this.section === "queue" ? 4000 : 15000);
    }
  }

  render() {
    $("[data-subtabs]", this.el).innerHTML = SECTIONS.map(([k, l]) =>
      `<button class="subtab ${k === this.section ? "on" : ""}" data-section="${k}">${l}</button>`).join("");
    this.renderBody();
    this.sync();
    WS.updateHash();
  }

  async renderBody(quiet = false) {
    const body = $("[data-body]", this.el);
    if (!quiet) body.innerHTML = `<div class="card"><span class="spinner"></span> Loading&hellip;</div>`;
    try {
      body.innerHTML = await this[`html_${this.section}`]();
    } catch (err) {
      if (!quiet) body.innerHTML = `<div class="banner error">${esc(err.message)}</div>`;
    }
  }

  // ---- users ------------------------------------------------------------
  async html_users() {
    this.users = await api("/api/admin/users");
    const pending = this.users.filter((u) => u.status === "pending");
    const roleSel = (u) => `<select data-user-role="${esc(u.id)}">${["admin", "manager", "engineer", "viewer"].map((r) =>
      `<option value="${r}" ${u.role === r ? "selected" : ""}>${ROLE_LABEL[r]}</option>`).join("")}</select>`;
    return `
      ${pending.length ? `<div class="banner warn"><span class="grow"><strong>${pending.length} account request${pending.length > 1 ? "s" : ""} waiting for approval.</strong></span></div>` : ""}
      <div class="section-title"><h3>${this.users.length} users</h3><span class="grow"></span><button class="btn primary" data-act="new-user">+ Add user</button></div>
      <div class="table-wrap"><table class="list">
        <thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Status</th><th>Sign-in</th><th>Last seen</th><th></th></tr></thead>
        <tbody>${this.users.map((u) => `<tr>
          <td><span class="row">${avatar(u.display_name, "sm")} ${esc(u.display_name)}</span>${u.email ? `<div class="desc">${esc(u.email)}</div>` : ""}</td>
          <td class="mono">${esc(u.username)}</td>
          <td>${roleSel(u)}</td>
          <td><span class="badge user-${esc(u.status)}">${esc(STATUS_LABEL[u.status] || u.status)}</span></td>
          <td>${u.auth_source === "local" ? "Local" : "Directory"}</td>
          <td class="muted nowrap">${u.last_seen_at ? timeEl(u.last_seen_at) : "never"}</td>
          <td class="actions nowrap">
            ${u.status === "pending" ? `<button class="btn sm primary" data-user-status="${esc(u.id)}" data-v="active">Approve</button>` : ""}
            ${u.status === "active" && u.id !== App.me.id ? `<button class="btn sm" data-user-status="${esc(u.id)}" data-v="disabled">Disable</button>` : ""}
            ${u.status === "disabled" ? `<button class="btn sm" data-user-status="${esc(u.id)}" data-v="active">Enable</button>` : ""}
            ${u.auth_source === "local" ? `<button class="btn sm ghost" data-user-reset="${esc(u.id)}">Reset password</button>` : ""}
          </td></tr>`).join("")}</tbody></table></div>
      <p class="hint">Roles: <strong>Admin</strong> - everything, including this console. <strong>Project manager</strong> - manages every team project
        (members, deleting, issuing RFIs). <strong>Engineer</strong> - uploads, scans, triage and RFI editing. <strong>Viewer</strong> - read and download only.
        A person's role inside one project can be changed from that project's <em>Members</em> dialog.</p>`;
  }

  newUserDialog() {
    modal({
      title: "Add user",
      body: `<label class="field"><span>Username</span><input name="username" placeholder="e.g. s.hassan" autocomplete="off"></label>
        <label class="field"><span>Full name</span><input name="display_name"></label>
        <label class="field"><span>Email (optional)</span><input name="email" type="email"></label>
        <label class="field"><span>Role</span><select name="role">${["engineer", "viewer", "manager", "admin"].map((r) => `<option value="${r}">${ROLE_LABEL[r]}</option>`).join("")}</select></label>
        <p class="hint">A temporary password is generated. The person must choose their own at first sign-in.</p>`,
      actions: [{ label: "Cancel" }, {
        label: "Add user", primary: true,
        onClick: async (b) => {
          const v = (n) => b.querySelector(`[name=${n}]`).value.trim();
          const r = await api("/api/admin/users", { method: "POST", body: { username: v("username"), display_name: v("display_name"), email: v("email"), role: v("role") } });
          this.renderBody();
          showPassword(r.user.username, r.temporary_password);
        },
      }],
    });
  }

  // ---- queue ------------------------------------------------------------
  async html_queue() {
    const jobs = await api("/api/admin/queue");
    const active = jobs.filter((j) => j.status === "queued" || j.status === "running");
    const recent = jobs.filter((j) => !(j.status === "queued" || j.status === "running"));
    const row = (j, i) => `<tr>
      <td>${j.kind === "scan" ? `<a href="#/run/${esc(j.target_id)}/scan">${esc(j.project_name || "?")} &middot; Rev ${esc(j.run_seq || "?")}</a>` : `<span class="muted">Upload check</span>`}</td>
      <td><span class="badge st-${esc(j.status === "done" ? "complete" : j.status)}">${esc(j.status)}</span>${j.cancel_requested && j.status === "running" ? ' <span class="muted small">stopping&hellip;</span>' : ""}</td>
      <td>${esc(j.by || "")}</td>
      <td class="mono small">${esc(j.locked_by || "")}</td>
      <td>${j.attempts}/${j.max_attempts}</td>
      <td>${j.priority}</td>
      <td class="muted nowrap">${timeEl(j.created_at)}</td>
      <td class="actions nowrap">${j.status === "queued" ? `<button class="btn sm" data-job="${esc(j.id)}" data-job-act="top" ${i === 0 ? "disabled" : ""}>Move to top</button>` : ""}
        ${j.status === "queued" || j.status === "running" ? `<button class="btn sm danger" data-job="${esc(j.id)}" data-job-act="cancel">Cancel</button>` : ""}
        ${j.error && j.status !== "running" ? `<span class="muted small" title="${esc(j.error)}">error &#9432;</span>` : ""}</td></tr>`;
    const head = `<thead><tr><th>Scan</th><th>Status</th><th>Started by</th><th>Worker</th><th>Attempt</th><th>Priority</th><th>Queued</th><th></th></tr></thead>`;
    return `<div class="section-title"><h3>${active.filter((j) => j.status === "running").length} running, ${active.filter((j) => j.status === "queued").length} waiting</h3>
        <span class="hint">Refreshes every few seconds. Scans run in this order; "Move to top" puts one first.</span></div>
      ${active.length ? `<div class="table-wrap"><table class="list">${head}<tbody>${active.map(row).join("")}</tbody></table></div>`
        : `<div class="card empty small">The queue is empty.</div>`}
      <div class="section-title"><h3>Recently finished</h3></div>
      <div class="table-wrap"><table class="list">${head}<tbody>${recent.map(row).join("") || `<tr><td colspan="8" class="muted">Nothing yet.</td></tr>`}</tbody></table></div>`;
  }

  // ---- AI fleet ---------------------------------------------------------
  async html_fleet() {
    const f = await api("/api/admin/fleet");
    this.fleet = f;
    const installed = [...new Set(f.servers.flatMap((s) => s.models || []))].sort();
    const usage = Object.fromEntries(f.usage_24h.map((u) => [u.role, u]));
    const feat = { verify_findings: "Second opinion on findings", semantic_matching: "Meaning-based matching",
      rfi_memory: "Company RFI memory", second_reader: "Second reading of drawing values", vote_critical: "3-way vote on critical findings" };
    return `<div class="card"><div class="row wrap" style="gap:14px">
        <label class="field" style="margin:0"><span>Active profile</span>
          <select data-fleet-profile>${f.profiles.map((p) => `<option value="${esc(p)}" ${p === f.profile ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></label>
        <div class="grow hint">Mode: <strong>${esc(f.mode)}</strong> ${f.mode === "resident" ? "- every model stays loaded, roles run side by side"
          : "- one GPU: models take turns, work is ordered by model"}.<br>
          ${Object.entries(f.features).map(([k, on]) => `<span class="badge ${on ? "user-active" : "outline"}">${on ? "&#10003;" : "&ndash;"} ${esc(feat[k] || k)}</span>`).join(" ")}</div>
        <div class="hint">RFI memory: <strong>${Object.values(f.rfi_memory || {}).reduce((a, b) => a + b, 0)}</strong> past RFIs</div></div>
        <p class="hint" style="margin-top:10px">Changes apply to the next scan. Only models installed on the company's own model servers can be chosen; cloud models are refused.</p></div>
      <datalist id="installed-models">${installed.map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
      <div class="table-wrap"><table class="list"><thead><tr><th>Job</th><th>Configured</th><th>Running on</th><th>Override</th><th>Last 24 h</th><th></th></tr></thead>
      <tbody>${f.roles.map((r) => { const u = usage[r.role]; return `<tr>
        <td><strong>${esc(r.role.replace(/_/g, " "))}</strong><div class="desc">${esc(ROLE_HELP[r.role] || "")}</div></td>
        <td class="mono small">${esc(r.configured)}${r.fallback.length ? `<div class="desc">then ${esc(r.fallback.join(", "))}</div>` : ""}</td>
        <td class="mono small">${r.used ? `${esc(r.used)} <span class="muted">@${esc(r.server || "")}</span>` : '<span class="badge st-failed">not available</span>'}</td>
        <td><input class="mono small" data-fleet-role="${esc(r.role)}" list="installed-models" placeholder="(profile default)"
          value="${esc((f.console.roles || {})[r.role] || "")}" style="min-width:160px"></td>
        <td class="small nowrap">${u ? `${u.calls} calls &middot; ${(u.avg_ms / 1000).toFixed(1)} s avg${u.cached ? ` &middot; ${u.cached} cached` : ""}${u.failed ? ` &middot; <span style="color:var(--danger)">${u.failed} failed</span>` : ""}` : '<span class="muted">-</span>'}</td>
        <td><button class="btn sm" data-fleet-test="${esc(r.role)}">Test</button> <span class="small" data-fleet-result="${esc(r.role)}"></span></td></tr>`; }).join("")}</tbody></table></div>`;
  }

  // ---- health -----------------------------------------------------------
  async html_health() {
    const h = await api("/api/admin/health");
    const stateClass = (c) => c.ok ? (/(stand-in|local|embedded|off)/.test(c.state || "") ? "info" : "ok") : "bad";
    return `<div class="health-grid">${h.checks.map((c) => `<div class="card health ${stateClass(c)}">
        <div class="row"><span class="dot-lg"></span><strong class="grow">${esc(c.name)}</strong><span class="badge outline">${esc(c.state || (c.ok ? "ok" : "problem"))}</span></div>
        <div class="hint">${esc(c.detail || "")}</div></div>`).join("")}</div>
      ${h.gpus && h.gpus.length ? `<div class="section-title"><h3>GPUs on this server</h3></div>
        <div class="table-wrap"><table class="list"><thead><tr><th>#</th><th>GPU</th><th>Memory</th><th>Load</th><th>Temp.</th></tr></thead><tbody>
        ${h.gpus.map((g) => `<tr><td>${g.index}</td><td>${esc(g.name)}</td>
          <td><div class="mini-track" style="width:160px"><div class="mini-fill" style="width:${Math.round(100 * g.memory_used_mb / g.memory_total_mb)}%"></div></div>
            <span class="small muted">${(g.memory_used_mb / 1024).toFixed(1)} / ${(g.memory_total_mb / 1024).toFixed(0)} GB</span></td>
          <td>${g.utilization}%</td><td>${g.temperature}&deg;C</td></tr>`).join("")}</tbody></table></div>` : ""}
      ${h.servers.length ? `<div class="section-title"><h3>Model servers</h3></div>
        <div class="table-wrap"><table class="list"><thead><tr><th>Server</th><th>Address</th><th>Roles</th><th>Loaded in GPU memory</th><th>Installed</th></tr></thead><tbody>
        ${h.servers.map((s) => `<tr><td><span class="dot st-${s.ok ? "complete" : "failed"}"></span> ${esc(s.name)}</td><td class="mono small">${esc(s.url)}</td>
          <td class="small">${esc((s.roles || []).join(", ") || "any")}</td>
          <td class="small">${(s.loaded || []).map((m) => `${esc(m.name)} <span class="muted">${m.vram_gb} GB</span>`).join("<br>") || '<span class="muted">none</span>'}</td>
          <td class="small muted">${s.ok ? (s.models || []).length + " models" : esc(s.error || "")}</td></tr>`).join("")}</tbody></table></div>` : ""}
      <p class="hint">ConflictScan ${esc(h.version)} &middot; refreshes every 15 seconds.</p>`;
  }

  // ---- audit ------------------------------------------------------------
  async html_audit() {
    const q = new URLSearchParams();
    if (this.auditFilter.action) q.set("action", this.auditFilter.action);
    if (this.auditFilter.user) q.set("user", this.auditFilter.user);
    const [rows, users] = await Promise.all([api(`/api/admin/audit?${q}`), this.users ? this.users : api("/api/admin/users")]);
    this.users = users;
    const prefixes = [["", "All actions"], ["auth.", "Sign-in"], ["project.", "Projects"], ["run.", "Scans"], ["finding.", "Findings"],
      ["rfi.", "RFIs"], ["export.", "Downloads"], ["file.", "File downloads"], ["admin.", "Administration"]];
    return `<div class="filters" style="position:static">
        <select data-audit-action>${prefixes.map(([v, l]) => `<option value="${v}" ${this.auditFilter.action === v ? "selected" : ""}>${l}</option>`).join("")}</select>
        <select data-audit-user><option value="">Everyone</option>${users.map((u) => `<option value="${esc(u.id)}" ${this.auditFilter.user === u.id ? "selected" : ""}>${esc(u.display_name)}</option>`).join("")}</select>
        <span class="hint">Latest 200 entries. Kept for the retention period in the settings.</span></div>
      <div class="table-wrap"><table class="list"><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Details</th><th>Address</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td class="nowrap" title="${esc(fmtDate(r.at))}">${timeEl(r.at)}</td><td>${esc(r.username || "-")}</td>
        <td class="mono small">${esc(r.action)}</td>
        <td>${r.project_id ? `<a href="#/project/${esc(r.project_id)}">project</a> &middot; ` : ""}${esc(r.summary || "")}</td>
        <td class="mono small muted">${esc(r.ip || "")}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">No entries.</td></tr>`}</tbody></table></div>`;
  }

  // ---- settings ---------------------------------------------------------
  async html_settings() {
    const s = await api("/api/admin/settings");
    const rows = Object.entries(s.settings);
    return `<div class="card"><div class="stack small">
        <div><strong>Settings file:</strong> <span class="mono">${esc(s.settings_file)}</span> ${s.settings_file_found ? "" : '<span class="badge outline">not found - using defaults</span>'}</div>
        <div><strong>Database:</strong> ${esc(s.database)} &middot; schema revision <span class="mono">${esc(s.schema_revision)}</span></div>
        <div><strong>Files:</strong> ${esc(s.files)}</div></div>
        <p class="hint">Settings are changed in the settings file (or environment variables) by the server administrator, then the services restarted. Secrets are masked.</p></div>
      <div class="table-wrap"><table class="list"><thead><tr><th>Setting</th><th>Value</th></tr></thead>
      <tbody>${rows.map(([k, v]) => `<tr><td class="mono small">${esc(k)}</td><td class="mono small">${v === "" ? '<span class="muted">default</span>' : esc(v)}</td></tr>`).join("")}</tbody></table></div>`;
  }

  // ---- events -----------------------------------------------------------
  async onClick(e) {
    const t = e.target;
    const sec = t.closest("[data-section]");
    if (sec) { this.section = sec.dataset.section; this.render(); return; }
    if (t.closest("[data-act=new-user]")) { this.newUserDialog(); return; }
    const st = t.closest("[data-user-status]");
    if (st) {
      try { await api(`/api/admin/users/${st.dataset.userStatus}`, { method: "PATCH", body: { status: st.dataset.v } }); this.renderBody(true); }
      catch (err) { toastError(err); }
      return;
    }
    const rs = t.closest("[data-user-reset]");
    if (rs) {
      const u = this.users.find((x) => x.id === rs.dataset.userReset);
      if (!(await confirmDialog(`Give ${u.display_name} a new temporary password? Their current password stops working.`, { title: "Reset password", confirmLabel: "Reset" }))) return;
      try { const r = await api(`/api/admin/users/${u.id}/reset-password`, { method: "POST" }); showPassword(u.username, r.temporary_password); }
      catch (err) { toastError(err); }
      return;
    }
    const test = t.closest("[data-fleet-test]");
    if (test) {
      const out = $(`[data-fleet-result="${test.dataset.fleetTest}"]`, this.el);
      out.innerHTML = '<span class="spinner sm"></span>';
      const r = await api("/api/admin/fleet/test", { method: "POST", body: { role: test.dataset.fleetTest } }).catch((err) => ({ ok: false, error: err.message }));
      out.innerHTML = r.ok ? `<span style="color:var(--ok)">&#10003; ${(r.ms / 1000).toFixed(1)} s</span>` : `<span style="color:var(--danger)" title="${esc(r.error || "")}">failed</span>`;
      return;
    }
    const job = t.closest("[data-job]");
    if (job) {
      if (job.dataset.jobAct === "cancel" && !(await confirmDialog("Cancel this job? A running scan stops after its current stage.", { title: "Cancel job", confirmLabel: "Cancel job", danger: true }))) return;
      try { await api(`/api/admin/queue/${job.dataset.job}`, { method: "POST", body: { action: job.dataset.jobAct } }); this.renderBody(true); }
      catch (err) { toastError(err); }
    }
  }
}

function showPassword(username, pw) {
  modal({
    title: "Temporary password",
    body: `<p>Give this to <strong>${esc(username)}</strong> privately. They will be asked to choose their own password when they sign in.</p>
      <div class="temp-pw mono">${esc(pw)}</div>`,
    actions: [{ label: "Copy", onClick: () => { navigator.clipboard.writeText(pw); toast("Copied", { timeout: 1200 }); return false; } }, { label: "Done", primary: true }],
  });
}

document.addEventListener("change", async (e) => {
  const view = WS.views.get(WS.active);
  if (!(view instanceof AdminView)) return;
  const role = e.target.closest("[data-user-role]");
  if (role) {
    try { await api(`/api/admin/users/${role.dataset.userRole}`, { method: "PATCH", body: { role: role.value } }); toast("Role updated", { type: "success", timeout: 1500 }); }
    catch (err) { toastError(err); view.renderBody(true); }
  }
  if (e.target.closest("[data-fleet-profile]")) {
    try { await api("/api/admin/fleet", { method: "PUT", body: { profile: e.target.value } }); toast(`AI profile: ${e.target.value}`, { type: "success" }); view.renderBody(true); }
    catch (err) { toastError(err); }
  }
  const roleInput = e.target.closest("[data-fleet-role]");
  if (roleInput) {
    try { await api("/api/admin/fleet", { method: "PUT", body: { roles: { [roleInput.dataset.fleetRole]: roleInput.value.trim() } } }); toast("Model assignment saved", { type: "success", timeout: 1500 }); view.renderBody(true); }
    catch (err) { toastError(err); view.renderBody(true); }
  }
  if (e.target.closest("[data-audit-action]")) { view.auditFilter.action = e.target.value; view.renderBody(true); }
  if (e.target.closest("[data-audit-user]")) { view.auditFilter.user = e.target.value; view.renderBody(true); }
});

window.CS.VIEW_TYPES.admin = AdminView;
window.CS.NAV_ITEMS.push({ key: "admin:main", label: "Admin", icon: "&#9881;", show: isAdmin, open: () => WS.open("admin", "main", { step: "users" }) });
window.CS.PALETTE_ITEMS.push(() => isAdmin() ? [
  { label: "Admin: users", run: () => WS.open("admin", "main", { step: "users" }) },
  { label: "Admin: scan queue", run: () => WS.open("admin", "main", { step: "queue" }) },
  { label: "Admin: system health", run: () => WS.open("admin", "main", { step: "health" }) },
  { label: "Admin: AI fleet (models)", run: () => WS.open("admin", "main", { step: "fleet" }) },
  { label: "Admin: audit log", run: () => WS.open("admin", "main", { step: "audit" }) },
] : []);
})();
