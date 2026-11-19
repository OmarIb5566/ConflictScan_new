/* ConflictScan admin console
 * -----------------------------------------------------------------------
 * Users and roles, the scan queue, system health (database, file store,
 * sign-in source, reference data, model servers, GPUs), the audit log and
 * the effective settings. Admin-only; opens as a workspace tab (#/admin/users).
 */
"use strict";
(() => {
const { $, $$, esc, el, api, toast, toastError, modal, confirmDialog, timeEl, fmtDate, avatar, ROLE_LABEL, isAdmin, WS, App } = window.CS;

const SECTIONS = [["users", "Users"], ["queue", "Scan queue"], ["health", "System health"], ["audit", "Audit log"], ["settings", "Settings"]];
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
  if (e.target.closest("[data-audit-action]")) { view.auditFilter.action = e.target.value; view.renderBody(true); }
  if (e.target.closest("[data-audit-user]")) { view.auditFilter.user = e.target.value; view.renderBody(true); }
});

window.CS.VIEW_TYPES.admin = AdminView;
window.CS.NAV_ITEMS.push({ key: "admin:main", label: "Admin", icon: "&#9881;", show: isAdmin, open: () => WS.open("admin", "main", { step: "users" }) });
window.CS.PALETTE_ITEMS.push(() => isAdmin() ? [
  { label: "Admin: users", run: () => WS.open("admin", "main", { step: "users" }) },
  { label: "Admin: scan queue", run: () => WS.open("admin", "main", { step: "queue" }) },
  { label: "Admin: system health", run: () => WS.open("admin", "main", { step: "health" }) },
  { label: "Admin: audit log", run: () => WS.open("admin", "main", { step: "audit" }) },
] : []);
})();
