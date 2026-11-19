/* ConflictScan web client
 * -----------------------------------------------------------------------
 * Single-page workspace over the /api/* endpoints in api.py:
 *   - sign in / accounts
 *   - browser-style tabs (Home, projects, scan runs), restored per user
 *   - projects dashboard with search, filters, pinning, archiving
 *   - each scan run walks Details -> Documents -> Scan -> Findings -> RFIs;
 *     every stage of every past run can be reopened, and results can be
 *     edited in place (RFI text, finding triage) without re-running
 *   - background scans with live progress + notifications
 * No build step: plain ES2020, loaded by templates/index.html.
 */
"use strict";
(() => {

// =====================================================================
// Utilities
// =====================================================================

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const isTyping = (e) => { const t = e.target; return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)); };
const local = {
  get(k, d) { try { const v = localStorage.getItem("cs." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("cs." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

function timeAgo(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)}d ago`;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: sameYear ? undefined : "numeric" });
}
const fmtDate = (iso) => iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
const timeEl = (iso) => iso ? `<time datetime="${esc(iso)}" title="${esc(fmtDate(iso))}">${esc(timeAgo(iso))}</time>` : "";
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const extOf = (name) => ((name || "").match(/\.([^.]+)$/) || ["", ""])[1].toLowerCase();

const ACTIVE = new Set(["queued", "running"]);
const STATUS_LABEL = { draft: "Draft", queued: "Queued", running: "Running", complete: "Complete", failed: "Failed", cancelled: "Cancelled" };
const statusBadge = (s) => `<span class="badge st-${esc(s)}">${s === "running" ? '<span class="spinner" style="width:10px;height:10px;border-width:2px"></span>' : ""}${esc(STATUS_LABEL[s] || s)}</span>`;
const SEVERITIES = ["critical", "major", "minor"];

function sevChips(c) {
  c = c || {};
  return `<span class="sev-chips" title="critical / major / minor">${SEVERITIES.map((s) =>
    `<span class="sev-chip ${s} ${c[s] ? "" : "zero"}">${c[s] || 0}</span>`).join("")}</span>`;
}

function avatar(name, cls = "") {
  const n = String(name || "?").trim();
  const initials = n.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase() || "?";
  let hash = 0;
  for (const ch of n) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  return `<span class="avatar ${cls}" style="background:hsl(${hash} 50% 45%)" title="${esc(n)}">${esc(initials)}</span>`;
}

function autoGrow(t) {
  if (!t || !t.isConnected) return;
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight + 2, 640) + "px";
}

// Findings / RFI counts, mirroring storage.run_counts on the server.
function countsOf(run) {
  const res = run.results || {};
  const rev = (run.review || {}).findings || {};
  const c = { critical: 0, major: 0, minor: 0, findings: 0, open: 0, dismissed: 0, rfis: (res.rfi_items || []).length };
  for (const d of res.discrepancies || []) {
    c.findings += 1;
    const sev = ((rev[d.id] || {}).severity || d.severity || "minor").toLowerCase();
    if (sev in c) c[sev] += 1;
    const st = (rev[d.id] || {}).status || "open";
    if (st === "open") c.open += 1;
    if (st === "dismissed") c.dismissed += 1;
  }
  return c;
}

// =====================================================================
// API
// =====================================================================

// The server hands the page a per-session anti-forgery token in a readable
// cookie; every state-changing request echoes it back in a header.
function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)cs_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(path, { method = "GET", body, form, _retried = false } = {}) {
  const init = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") init.headers["X-CSRF-Token"] = csrfToken();
  if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  if (form) init.body = form;
  let res;
  try { res = await fetch(path, init); } catch { throw new Error("Network error - is the server running?"); }
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (res.status === 403 && data && data.csrf_failed && !_retried) {
    await fetch("/api/auth/status", { credentials: "same-origin" }).catch(() => {});   // refreshes the token cookie
    return api(path, { method, body, form, _retried: true });
  }
  if (res.status === 401 && data && data.auth_required) { Auth.expired(data.expired); }
  if (!res.ok || (data && data.error)) {
    const err = new Error((data && data.error) || `Request failed (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function uploadFiles(path, form, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", path);
    x.setRequestHeader("X-CSRF-Token", csrfToken());
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    x.onload = () => {
      let d = {};
      try { d = JSON.parse(x.responseText); } catch { /* ignore */ }
      if (x.status === 401) { Auth.expired(d.expired); return reject(new Error("Please sign in.")); }
      if (x.status >= 400 && !(d.added && d.added.length)) {
        return reject(new Error(d.error || (d.errors || []).join(" ") || `Upload failed (${x.status})`));
      }
      resolve(d);
    };
    x.onerror = () => reject(new Error("Network error during upload."));
    x.send(form);
  });
}

// =====================================================================
// UI primitives: toast, modal, menu
// =====================================================================

function toast(message, { type = "info", action, onAction, timeout = 5000 } = {}) {
  const t = el(`<div class="toast ${type}" role="status"><span class="grow">${esc(message)}</span>
    ${action ? `<button class="act">${esc(action)}</button>` : ""}<button class="close" aria-label="Dismiss">&times;</button></div>`);
  const close = () => t.remove();
  t.querySelector(".close").onclick = close;
  if (action) t.querySelector(".act").onclick = () => { close(); if (onAction) onAction(); };
  $("#toasts").appendChild(t);
  const all = $$("#toasts .toast");
  if (all.length > 4) all[0].remove();
  if (timeout) setTimeout(close, timeout);
}
const toastError = (err) => toast(err && err.message ? err.message : String(err), { type: "error", timeout: 7000 });

function showFormError(container, message) {
  let box = container.querySelector(".form-error");
  if (!box) { box = el(`<div class="form-error"></div>`); container.appendChild(box); }
  box.textContent = message;
  box.hidden = false;
}

function modal({ title, body, actions = [], wide = false, cls = "", onOpen, onClose }) {
  const ov = el(`<div class="overlay"><div class="modal ${wide ? "wide" : ""} ${cls}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="modal-head"><h3>${esc(title)}</h3><button class="icon-btn" data-x aria-label="Close">&times;</button></div>
    <div class="modal-body"></div><div class="modal-foot"></div></div></div>`);
  const bodyEl = ov.querySelector(".modal-body");
  if (typeof body === "string") bodyEl.innerHTML = body; else if (body) bodyEl.appendChild(body);
  const foot = ov.querySelector(".modal-foot");
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    ov.remove();
    document.removeEventListener("keydown", onKey, true);
    if (onClose) onClose();
  };
  const run = async (a, btn) => {
    if (btn) btn.disabled = true;
    try {
      const r = a.onClick ? await a.onClick(bodyEl) : undefined;
      if (r !== false) close();
    } catch (err) {
      showFormError(bodyEl, err.message);
    } finally { if (btn) btn.disabled = false; }
  };
  const sticky = /\bno-dismiss\b/.test(cls);
  const onKey = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); if (!sticky) close(); }
    else if (e.key === "Enter" && !e.shiftKey && e.target.tagName !== "TEXTAREA" && e.target.tagName !== "BUTTON") {
      const i = actions.findIndex((a) => a.primary);
      if (i >= 0) { e.preventDefault(); e.stopPropagation(); run(actions[i], foot.children[i]); }
    }
  };
  actions.forEach((a) => {
    const b = el(`<button class="btn ${a.primary ? "primary" : ""} ${a.danger ? "danger solid" : ""}" type="button">${esc(a.label)}</button>`);
    b.onclick = () => run(a, b);
    foot.appendChild(b);
  });
  if (!actions.length) foot.remove();
  ov.addEventListener("mousedown", (e) => { if (e.target === ov && !sticky) close(); });
  if (sticky) ov.querySelector("[data-x]").remove(); else ov.querySelector("[data-x]").onclick = close;
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(ov);
  const focusable = bodyEl.querySelector("[autofocus]") || bodyEl.querySelector("input:not([type=radio]):not([type=checkbox]), textarea, select");
  (focusable || foot.querySelector(".primary, .danger") || ov.querySelector("[data-x]")).focus();
  if (onOpen) onOpen(bodyEl, close);
  return { close, bodyEl };
}

function confirmDialog(message, { title = "Are you sure?", confirmLabel = "Confirm", danger = false } = {}) {
  return new Promise((resolve) => {
    let ok = false;
    modal({
      title, body: `<p style="margin:0">${esc(message)}</p>`,
      actions: [{ label: "Cancel" }, { label: confirmLabel, primary: !danger, danger, onClick: () => { ok = true; } }],
      onClose: () => resolve(ok),
    });
  });
}

function promptDialog(title, { label = "", value = "", placeholder = "", confirmLabel = "Save" } = {}) {
  return new Promise((resolve) => {
    let out = null;
    modal({
      title,
      body: `<label class="field"><span>${esc(label)}</span><input data-v value="${esc(value)}" placeholder="${esc(placeholder)}" autofocus></label>`,
      actions: [{ label: "Cancel" }, {
        label: confirmLabel, primary: true,
        onClick: (b) => { const v = b.querySelector("[data-v]").value.trim(); if (!v) throw new Error("Enter a value."); out = v; },
      }],
      onOpen: (b) => { const i = b.querySelector("[data-v]"); i.focus(); i.select(); },
      onClose: () => resolve(out),
    });
  });
}

function closeMenus() { $$(".menu").forEach((m) => m.remove()); }

function popMenu(anchor, items) {
  closeMenus();
  const m = el(`<div class="menu" role="menu"></div>`);
  for (const it of items) {
    if (!it) continue;
    if (it === "-") { m.appendChild(el("<hr>")); continue; }
    if (it.head) { m.appendChild(el(`<div class="menu-head">${it.head}</div>`)); continue; }
    const b = el(`<button type="button" role="menuitem" class="${it.danger ? "danger" : ""}" ${it.disabled ? "disabled" : ""}
      ${it.title ? `title="${esc(it.title)}"` : ""}>${esc(it.label)}</button>`);
    b.onclick = (e) => { e.stopPropagation(); closeMenus(); it.onClick(); };
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y };
  const w = m.offsetWidth, h = m.offsetHeight;
  let left = Math.min(r.left, window.innerWidth - w - 8);
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  m.style.left = `${Math.max(8, left)}px`;
  m.style.top = `${top}px`;
  const first = m.querySelector("button:not(:disabled)");
  if (first) first.focus();
}
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".menu")) closeMenus(); });
window.addEventListener("resize", closeMenus);

// =====================================================================
// Autosave
// =====================================================================

class Saver {
  constructor(send, { delay = 700, onState } = {}) {
    this.send = send; this.delay = delay; this.onState = onState || (() => {});
    this.pending = null; this.timer = null; this.inflight = null;
    Saver.all.add(this);
  }
  static merge(a, b) {
    const out = { ...(a || {}) };
    for (const [k, v] of Object.entries(b || {})) {
      out[k] = v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object" ? { ...out[k], ...v } : v;
    }
    return out;
  }
  schedule(patch) {
    this.pending = Saver.merge(this.pending, patch);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.flush().catch(() => {}), this.delay);
    this.onState("dirty");
  }
  async flush() {
    clearTimeout(this.timer);
    if (this.inflight) { try { await this.inflight; } catch { /* retried below */ } }
    if (!this.pending) return;
    const patch = this.pending;
    this.pending = null;
    this.onState("saving");
    this.inflight = this.send(patch)
      .then((r) => { this.onState(this.pending ? "dirty" : "saved"); return r; })
      .catch((err) => {
        this.pending = Saver.merge(patch, this.pending);
        this.onState("error", err);
        clearTimeout(this.timer);
        this.timer = setTimeout(() => this.flush().catch(() => {}), 5000);
        throw err;
      })
      .finally(() => { this.inflight = null; });
    return this.inflight;
  }
  get busy() { return !!(this.pending || this.inflight); }
  dispose() { Saver.all.delete(this); }
  static async flushAll() { await Promise.allSettled([...Saver.all].map((s) => s.flush())); }
  static anyBusy() { return [...Saver.all].some((s) => s.busy); }
}
Saver.all = new Set();

function saveStateHtml(state, err) {
  switch (state) {
    case "saving": return `<span class="save-state saving"><span class="spinner" style="width:10px;height:10px"></span>Saving&hellip;</span>`;
    case "dirty": return `<span class="save-state dirty">&bull; Unsaved changes</span>`;
    case "error": return `<span class="save-state error" title="${esc(err && err.message)}">Couldn't save &mdash; retrying</span>`;
    case "saved": return `<span class="save-state">&#10003; All changes saved</span>`;
    default: return "";
  }
}

window.addEventListener("beforeunload", (e) => {
  if (Saver.anyBusy()) { Saver.flushAll(); e.preventDefault(); e.returnValue = ""; }
});

// =====================================================================
// App state
// =====================================================================

const RANK = { viewer: 1, engineer: 2, manager: 3 };
const canRole = (role, need) => (RANK[role] || 0) >= RANK[need];
const ROLE_LABEL = { admin: "Admin", manager: "Project manager", engineer: "Engineer", viewer: "Viewer" };
const isAdmin = () => App.me && App.me.role === "admin";

const App = {
  me: null,
  prefs: {},
  projects: [],
  projectIndex: new Map(),
  runIndex: new Map(),   // run id -> {run, project}
  template: null,
};

function buildIndex() {
  App.projectIndex.clear();
  App.runIndex.clear();
  for (const p of App.projects) {
    App.projectIndex.set(p.id, p);
    for (const r of p.runs || []) App.runIndex.set(r.id, { run: r, project: p });
  }
}

async function refreshProjects() {
  try {
    App.projects = await api("/api/projects");
  } catch (err) { if (err.status !== 401) toastError(err); return; }
  buildIndex();
  Sidebar.render();
  for (const v of WS.views.values()) if (v.onProjectsChanged) v.onProjectsChanged();
  WS.renderBar();
}
const refreshSoon = debounce(refreshProjects, 300);

function savePrefs(patch) {
  Object.assign(App.prefs, patch);
  return api("/api/me/prefs", { method: "PUT", body: patch }).catch(() => {});
}

// =====================================================================
// Auth
// =====================================================================

const Auth = {
  mode: "login",
  status: null,
  show(status) {
    this.status = status || this.status || {};
    $("#app").hidden = true;
    $("#auth").hidden = false;
    $("#auth-first").hidden = !this.status.first_user;
    if (this.status.first_user) this.setMode("register");
    else this.setMode(this.mode);
    $('[data-mode="register"]').hidden = !this.status.signup_allowed;
  },
  expired(idle) {
    if (!App.me) return;
    App.me = null;
    Jobs.stop();
    toast(idle ? "You were signed out after a period of inactivity. Sign in again to continue - your saved work is kept."
               : "Your session has ended. Sign in again to continue.", { type: "error", timeout: 0 });
    this.show();
  },
  setMode(mode) {
    this.mode = mode;
    $$("#auth-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    $$("#auth-form [data-register]").forEach((n) => { n.hidden = mode !== "register"; });
    $$("#auth-form [data-login]").forEach((n) => { n.hidden = mode !== "login"; });
    $("#auth-submit").textContent = mode === "login" ? "Sign in" : "Create account";
    const pw = $('#auth-form [name="password"]');
    pw.autocomplete = mode === "login" ? "current-password" : "new-password";
    pw.minLength = mode === "register" ? ((this.status && this.status.min_password) || 8) : 0;
    pw.placeholder = mode === "register" ? `At least ${pw.minLength} characters` : "";
    $("#auth-error").hidden = true;
    $("#auth-error").classList.remove("ok");
    const hint = $("#auth-dir-hint");
    if (hint) hint.hidden = !(this.status && /ldap/.test(this.status.auth_mode || "")) || mode !== "login";
  },
  init() {
    $$("#auth-mode button").forEach((b) => { b.onclick = () => this.setMode(b.dataset.mode); });
    $("#auth-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const body = {
        username: f.username.value.trim(), password: f.password.value,
        display_name: f.display_name.value.trim(), remember: f.remember.checked,
      };
      const btn = $("#auth-submit");
      btn.disabled = true;
      try {
        const res = await api(this.mode === "login" ? "/api/auth/login" : "/api/auth/register", { method: "POST", body });
        if (res && res.pending) {
          $("#auth-error").textContent = res.message;
          $("#auth-error").hidden = false;
          $("#auth-error").classList.add("ok");
          this.setMode("login");
          btn.disabled = false;
          return;
        }
        location.reload();
      } catch (err) {
        $("#auth-error").textContent = err.message;
        $("#auth-error").hidden = false;
        btn.disabled = false;
      }
    });
  },
};

async function signOut() {
  await Saver.flushAll();
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  location.hash = "";
  location.reload();
}

function forcePasswordChange() {
  modal({
    title: "Choose a new password",
    cls: "no-dismiss",
    body: `<p class="hint">Your account was set up with a temporary password. Choose your own to continue.</p>
      <label class="field"><span>Temporary password</span><input name="current_password" type="password" autocomplete="current-password"></label>
      <label class="field"><span>New password</span><input name="new_password" type="password" autocomplete="new-password" placeholder="At least 8 characters"></label>`,
    actions: [{ label: "Sign out", onClick: () => signOut() }, {
      label: "Save password", primary: true,
      onClick: async (b) => {
        const r = await api("/api/me", { method: "PATCH", body: {
          current_password: b.querySelector("[name=current_password]").value,
          new_password: b.querySelector("[name=new_password]").value } });
        App.me = r.user;
        toast("Password changed", { type: "success" });
      },
    }],
  });
}

function accountDialog() {
  const directory = App.me.auth_source && App.me.auth_source !== "local";
  modal({
    title: "Account",
    body: `
      <label class="field"><span>Display name</span><input name="display_name" value="${esc(App.me.display_name)}"></label>
      <div class="hint">Signed in as <strong>${esc(App.me.username)}</strong> &middot; ${esc(ROLE_LABEL[App.me.role] || App.me.role)}${directory ? " &middot; company directory account" : ""}.
        Your display name appears on scans and edits you make.</div>
      <hr style="border:none;border-top:1px solid var(--border);margin:16px 0">
      ${directory ? `<p class="hint">Your password is managed by the company directory.</p>` : `
      <div class="field-label">Change password</div>
      <label class="field"><span>Current password</span><input name="current_password" type="password" autocomplete="current-password"></label>
      <label class="field"><span>New password</span><input name="new_password" type="password" autocomplete="new-password"></label>`}`,
    actions: [{ label: "Cancel" }, {
      label: "Save", primary: true,
      onClick: async (b) => {
        const body = { display_name: b.querySelector("[name=display_name]").value.trim() };
        const npEl = b.querySelector("[name=new_password]");
        const np = npEl ? npEl.value : "";
        if (np) { body.new_password = np; body.current_password = b.querySelector("[name=current_password]").value; }
        const r = await api("/api/me", { method: "PATCH", body });
        App.me = r.user;
        renderUser();
        toast(np ? "Account updated - password changed" : "Account updated", { type: "success" });
      },
    }],
  });
}

function renderUser() {
  $("#user-avatar").outerHTML = avatar(App.me.display_name).replace('class="avatar ', 'id="user-avatar" class="avatar ');
  $("#user-name").textContent = App.me.display_name;
}

// =====================================================================
// Theme
// =====================================================================

const Theme = {
  get() { try { return localStorage.getItem("cs.theme") || "system"; } catch { return "system"; } },
  apply(t) {
    try { localStorage.setItem("cs.theme", t); } catch { /* ignore */ }
    const dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  },
  set(t) { this.apply(t); savePrefs({ theme: t }); toast(`Theme: ${t[0].toUpperCase() + t.slice(1)}`, { timeout: 1500 }); },
  cycle() {
    const order = ["light", "dark", "system"];
    this.set(order[(order.indexOf(this.get()) + 1) % order.length]);
  },
};
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (Theme.get() === "system") Theme.apply("system"); });

// =====================================================================
// Workspace tabs
// =====================================================================

const WS = {
  tabs: [],
  active: null,
  views: new Map(),
  dragKey: null,

  keyOf(type, id) { return type === "home" ? "home" : `${type}:${id}`; },
  parse(key) { if (key === "home") return { type: "home" }; const [type, id] = key.split(":"); return { type, id }; },

  create(key) {
    const { type, id } = this.parse(key);
    const Ctor = VIEW_TYPES[type];
    const view = Ctor ? new Ctor(id) : null;
    if (!view) return null;
    view.el.hidden = true;
    $("#views").appendChild(view.el);
    this.views.set(key, view);
    return view;
  },

  open(type, id, opts = {}) {
    const key = this.keyOf(type, id);
    let view = this.views.get(key);
    if (!view) {
      view = this.create(key);
      if (!view) return null;
      if (opts.replace && this.tabs.includes(opts.replace) && opts.replace !== "home") {
        this.tabs.splice(this.tabs.indexOf(opts.replace), 1, key);
        this.destroy(opts.replace);
      } else {
        this.tabs.push(key);
      }
    }
    if (opts.step && view.setInitialStep) view.setInitialStep(opts.step);
    if (opts.rfi && view.selectRfi) view.selectedRfi = opts.rfi;
    if (opts.activate !== false) this.activate(key);
    this.renderBar();
    this.persist();
    return view;
  },

  async ensureLoaded(view) {
    if (view.loaded || view.loading) return;
    view.loading = true;
    try {
      await view.load();
      view.loaded = true;
    } catch (err) {
      if (err.status === 404) {
        toast("That item no longer exists, or you don't have access to it.", { type: "error" });
        this.close(view.key, { force: true });
      } else {
        view.el.innerHTML = `<div class="view-inner"><div class="empty"><div class="big">&#9888;</div><h3>Couldn't load this</h3>
          <p>${esc(err.message)}</p><button class="btn" data-retry>Try again</button></div></div>`;
        view.el.querySelector("[data-retry]").onclick = () => { this.ensureLoaded(view); };
      }
    } finally {
      view.loading = false;
    }
    this.renderBar();
  },

  activate(key) {
    if (!this.views.has(key)) return;
    if (this.active && this.active !== key) {
      const prev = this.views.get(this.active);
      if (prev) { prev.el.hidden = true; if (prev.onHide) prev.onHide(); }
    }
    this.active = key;
    const view = this.views.get(key);
    view.el.hidden = false;
    this.ensureLoaded(view);
    if (view.onShow) view.onShow();
    this.renderBar();
    this.updateHash();
    Sidebar.highlight();
    $("#app").classList.remove("nav-open");
    this.persist();
  },

  activateIndex(i) { if (this.tabs[i]) this.activate(this.tabs[i]); },

  async close(key, { force = false } = {}) {
    if (key === "home" || !this.tabs.includes(key)) return;
    const view = this.views.get(key);
    if (!force && view && view.flushAll) await view.flushAll();
    const idx = this.tabs.indexOf(key);
    this.tabs.splice(idx, 1);
    this.destroy(key);
    if (this.active === key) {
      this.active = null;
      this.activate(this.tabs[Math.min(idx, this.tabs.length - 1)] || "home");
    }
    this.renderBar();
    this.persist();
  },

  closeOthers(key) { for (const k of [...this.tabs]) if (k !== key && k !== "home") this.close(k); },
  closeRight(key) { const i = this.tabs.indexOf(key); for (const k of this.tabs.slice(i + 1)) this.close(k); },

  destroy(key) {
    const v = this.views.get(key);
    if (!v) return;
    if (v.destroy) v.destroy();
    v.el.remove();
    this.views.delete(key);
  },

  renderBar() {
    const bar = $("#tabbar");
    bar.innerHTML = "";
    for (const key of this.tabs) {
      const v = this.views.get(key);
      if (!v) continue;
      const status = v.tabStatus ? v.tabStatus() : null;
      const icon = key === "home" ? `<span aria-hidden="true">&#8962;</span>`
        : status ? `<span class="dot st-${esc(status)}" title="${esc(STATUS_LABEL[status] || status)}"></span>`
        : `<span aria-hidden="true" class="muted">&#9638;</span>`;
      const t = el(`<div class="tab ${key === this.active ? "on" : ""}" draggable="${key !== "home"}" data-key="${esc(key)}"
        title="${esc(v.tabTooltip ? v.tabTooltip() : v.tabTitle())}" role="tab" aria-selected="${key === this.active}">
        ${icon}<span class="t">${esc(v.tabTitle())}</span>${key !== "home" ? `<span class="x" title="Close tab (Alt+W)">&times;</span>` : ""}</div>`);
      t.addEventListener("click", (e) => { if (e.target.closest(".x")) this.close(key); else this.activate(key); });
      t.addEventListener("auxclick", (e) => { if (e.button === 1) { e.preventDefault(); this.close(key); } });
      t.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        popMenu({ x: e.clientX, y: e.clientY }, [
          key !== "home" && { label: "Close tab", onClick: () => this.close(key) },
          { label: "Close other tabs", onClick: () => this.closeOthers(key) },
          { label: "Close tabs to the right", onClick: () => this.closeRight(key) },
          v.copyLink && "-",
          v.copyLink && { label: "Copy link", onClick: () => v.copyLink() },
          v.reload && { label: "Refresh", onClick: () => v.reload() },
        ]);
      });
      t.addEventListener("dragstart", (e) => { this.dragKey = key; t.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
      t.addEventListener("dragend", () => { this.dragKey = null; this.renderBar(); });
      t.addEventListener("dragover", (e) => {
        if (!this.dragKey || key === "home") return;
        e.preventDefault();
        $$(".tab.drop-before").forEach((n) => n.classList.remove("drop-before"));
        t.classList.add("drop-before");
      });
      t.addEventListener("drop", (e) => {
        e.preventDefault();
        if (!this.dragKey || this.dragKey === key) return;
        this.tabs.splice(this.tabs.indexOf(this.dragKey), 1);
        this.tabs.splice(this.tabs.indexOf(key), 0, this.dragKey);
        this.renderBar();
        this.persist();
      });
      bar.appendChild(t);
    }
    const plus = el(`<button class="tab-new" title="New scan (Alt+N)" aria-label="New scan">+</button>`);
    plus.onclick = () => newScanDialog();
    bar.appendChild(plus);
    const activeTab = bar.querySelector(".tab.on");
    if (activeTab) activeTab.scrollIntoView({ block: "nearest", inline: "nearest" });
    updateDocTitle();
  },

  persist: debounce(function () {
    savePrefs({ workspace: { tabs: WS.tabs, active: WS.active } });
  }, 800),

  updateHash() {
    const v = this.views.get(this.active);
    const hash = v && v.hash ? v.hash() : "#/";
    if (location.hash !== hash) history.replaceState(null, "", hash);
  },

  openFromHash() {
    const m = location.hash.match(/^#\/(\w+)\/([\w-]+)(?:\/([\w-]+))?/);
    if (m && !VIEW_TYPES[m[1]]) return false;
    if (!m) return false;
    this.open(m[1], m[2], { step: m[3] });
    return true;
  },

  restore() {
    const ws = App.prefs.workspace || {};
    this.open("home", null, { activate: false });
    for (const key of ws.tabs || []) {
      if (key === "home" || this.tabs.includes(key)) continue;
      const { type, id } = this.parse(key);
      const Ctor = VIEW_TYPES[type];
      const exists = type === "project" ? App.projectIndex.has(id) : type === "run" ? App.runIndex.has(id)
        : !!(Ctor && Ctor.canRestore && Ctor.canRestore(id));
      if (exists) this.open(type, id, { activate: false });
    }
    if (!this.openFromHash()) this.activate(this.tabs.includes(ws.active) ? ws.active : "home");
  },
};

window.addEventListener("hashchange", () => {
  if (!App.me) return;
  if (!WS.openFromHash() && (location.hash === "" || location.hash === "#/")) WS.activate("home");
});

function updateDocTitle() {
  const n = Jobs.activeCount();
  const v = WS.views.get(WS.active);
  const base = v && WS.active !== "home" ? `${v.tabTitle()} - ConflictScan` : "ConflictScan";
  document.title = n ? `(${n} running) ${base}` : base;
}

// =====================================================================
// Sidebar
// =====================================================================

const Sidebar = {
  item(p) {
    const r = p.latest_run;
    return `<a class="side-item" data-project="${esc(p.id)}" title="${esc(p.name)}">
      <span class="dot st-${esc(r ? r.status : "draft")}"></span>
      <span class="t">${esc(p.name)}</span>
      <span class="sub">${r ? `Rev ${r.seq}` : ""}</span></a>`;
  },
  render() {
    const live = App.projects.filter((p) => !p.archived);
    const pinned = live.filter((p) => p.pinned);
    const recent = live.filter((p) => !p.pinned).slice(0, 7);
    $("#side-pinned").innerHTML = pinned.length ? pinned.map((p) => this.item(p)).join("")
      : `<div class="side-empty">Pin projects from their &#8943; menu.</div>`;
    $("#side-recent").innerHTML = recent.length ? recent.map((p) => this.item(p)).join("")
      : `<div class="side-empty">No projects yet.</div>`;
    this.highlight();
  },
  highlight() {
    const v = WS.views.get(WS.active);
    const pid = v ? (v.projectId ? v.projectId() : null) : null;
    $$("#sidebar [data-project]").forEach((a) => a.classList.toggle("on", a.dataset.project === pid));
    $("#nav-home").classList.toggle("on", WS.active === "home");
    $$("#sidebar [data-nav-key]").forEach((a) => a.classList.toggle("on", a.dataset.navKey === WS.active));
  },
  renderNav() {
    const nav = $(".side-nav");
    $$("[data-nav-key]", nav).forEach((n) => n.remove());
    for (const item of NAV_ITEMS) {
      if (item.show && !item.show()) continue;
      const a = el(`<a data-nav-key="${esc(item.key)}">${item.icon || ""}&nbsp; ${esc(item.label)}${item.badge ? `<span class="nav-badge" data-nav-badge="${esc(item.key)}"></span>` : ""}</a>`);
      a.onclick = () => item.open();
      nav.appendChild(a);
    }
  },
  renderJobs(list) {
    const active = list.filter((r) => ACTIVE.has(r.status));
    $("#side-jobs").innerHTML = active.length ? active.map((r) => {
      const stages = (r.progress && r.progress.stages) || [];
      const done = stages.filter((s) => s.status === "done" || s.status === "skipped").length;
      const cur = stages.find((s) => s.status === "running");
      const sub = r.status === "queued" ? `Queued${r.queue_position ? ` - #${r.queue_position} in line` : ""}` : (cur ? cur.label : "Starting...");
      const pct = r.status === "queued" ? 0 : Math.max(6, Math.round((done / (stages.length || 5)) * 100));
      return `<div class="side-job" data-run="${esc(r.id)}" title="Open scan">
        <div class="t">${esc(r.project ? r.project.name : "")} &middot; Rev ${r.seq}</div>
        <div class="s">${esc(sub)}</div>
        <div class="mini-track"><div class="mini-fill" style="width:${pct}%"></div></div></div>`;
    }).join("") : `<div class="side-empty">Nothing running.</div>`;
  },
  init() {
    $("#sidebar").addEventListener("click", (e) => {
      const p = e.target.closest("[data-project]");
      if (p) { WS.open("project", p.dataset.project); return; }
      const j = e.target.closest("[data-run]");
      if (j) { WS.open("run", j.dataset.run, { step: "scan" }); return; }
      if (e.target.closest("[data-nav=home]")) WS.activate("home");
    });
    $("#new-scan-btn").onclick = () => newScanDialog();
  },
};

// =====================================================================
// Background jobs: progress + "scan finished" notifications
// =====================================================================

const Jobs = {
  list: [],
  known: new Map(),
  timer: null,
  first: true,
  stopped: false,

  activeCount() { return this.list.filter((r) => ACTIVE.has(r.status)).length; },

  async poll() {
    clearTimeout(this.timer);
    if (this.stopped) return;
    try {
      const list = await api("/api/jobs");
      for (const r of list) {
        const prev = this.known.get(r.id);
        if (!this.first && prev && ACTIVE.has(prev) && !ACTIVE.has(r.status)) this.finished(r);
        this.known.set(r.id, r.status);
        const view = WS.views.get(`run:${r.id}`);
        if (view && view.run && view.run.status !== r.status && ACTIVE.has(r.status)) {
          view.run.status = r.status;
          view.renderHead();
        }
      }
      // Runs that left the feed while active (e.g. deleted) - forget them.
      for (const [id, st] of this.known) if (ACTIVE.has(st) && !list.some((r) => r.id === id)) this.known.delete(id);
      this.list = list;
      this.first = false;
      Sidebar.renderJobs(list);
      WS.renderBar();
      const home = WS.views.get("home");
      if (home && home.loaded) home.renderStats();
    } catch { /* offline: try again later */ }
    this.timer = setTimeout(() => this.poll(), this.activeCount() ? 3000 : 15000);
  },

  track(run) {
    this.known.set(run.id, run.status);
    this.stopped = false;
    setTimeout(() => this.poll(), 400);
  },

  stop() { this.stopped = true; clearTimeout(this.timer); },

  finished(r) {
    refreshSoon();
    const view = WS.views.get(`run:${r.id}`);
    if (view && view.loaded) view.reload();
    const mine = App.me && (r.created_by === App.me.id || r.updated_by === App.me.id);
    if (!mine) return;
    const name = `${r.project ? r.project.name : "Scan"} - Rev ${r.seq}`;
    const c = r.counts || {};
    const msg = r.status === "complete"
      ? `Scan finished: ${name} - ${plural(c.findings || 0, "finding")}, ${plural(c.rfis || 0, "RFI")}`
      : r.status === "failed" ? `Scan failed: ${name}` : `Scan cancelled: ${name}`;
    toast(msg, {
      type: r.status === "complete" ? "success" : r.status === "failed" ? "error" : "info",
      action: "Open", timeout: 12000,
      onAction: () => WS.open("run", r.id, { step: r.status === "complete" ? "findings" : "scan" }),
    });
    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      const n = new Notification("ConflictScan", { body: msg, icon: "/static/Rowad-Logo.png" });
      n.onclick = () => { window.focus(); WS.open("run", r.id); n.close(); };
    }
  },
};

// =====================================================================
// Command palette (Ctrl+K)
// =====================================================================

const Palette = {
  open() {
    if ($(".palette")) return;
    const actions = [
      { k: "Action", t: "New scan", run: () => newScanDialog() },
      { k: "Action", t: "New project", run: () => newProjectDialog() },
      { k: "Action", t: "Go to Home", run: () => WS.activate("home") },
      { k: "Action", t: "Toggle dark mode", run: () => Theme.cycle() },
      { k: "Action", t: "Keyboard shortcuts", run: () => showShortcuts() },
      { k: "Action", t: "Account settings", run: () => accountDialog() },
      { k: "Action", t: "Sign out", run: () => signOut() },
    ];
    for (const f of PALETTE_ITEMS) for (const it of f() || []) actions.push({ k: "Action", t: it.label, sub: it.sub, run: it.run });
    const projects = App.projects.map((p) => ({
      k: p.archived ? "Archived" : "Project", t: p.name, sub: p.owner_name, hay: `${p.name} ${p.description} ${p.owner_name}`,
      run: () => WS.open("project", p.id),
    }));
    const runs = [];
    for (const p of App.projects) for (const r of p.runs || []) {
      runs.push({
        k: STATUS_LABEL[r.status], t: `${p.name} › Rev ${r.seq}${r.title ? " · " + r.title : ""}`,
        sub: timeAgo(r.updated_at), status: r.status, hay: `${p.name} rev ${r.seq} ${r.title} ${r.created_by_name}`,
        run: () => WS.open("run", r.id),
      });
    }
    let sel = 0;
    let shown = [];
    const { close, bodyEl } = modal({
      title: "Search", cls: "palette",
      body: `<input data-q placeholder="Search projects, scans and actions&hellip;" autofocus autocomplete="off">
        <div class="palette-list" data-list></div>
        <div class="palette-foot"><span><kbd>&uarr;</kbd><kbd>&darr;</kbd> move</span><span><kbd>Enter</kbd> open</span><span><kbd>Esc</kbd> close</span></div>`,
    });
    const m = bodyEl.closest(".modal");
    m.querySelector(".modal-head").remove();
    bodyEl.style.padding = "0";
    const input = bodyEl.querySelector("[data-q]");
    const list = bodyEl.querySelector("[data-list]");
    const render = () => {
      const q = input.value.toLowerCase().trim();
      const terms = q.split(/\s+/).filter(Boolean);
      const match = (it) => terms.every((t) => (it.hay || it.t).toLowerCase().includes(t));
      const groups = [
        ["Projects", projects.filter(match).slice(0, q ? 12 : 6)],
        ["Scans", runs.filter(match).slice(0, q ? 25 : 6)],
        ["Actions", actions.filter(match)],
      ];
      shown = [];
      list.innerHTML = groups.filter(([, items]) => items.length).map(([g, items]) =>
        `<div class="palette-group">${g}</div>` + items.map((it) => {
          shown.push(it);
          const i = shown.length - 1;
          return `<div class="palette-item ${i === sel ? "on" : ""}" data-i="${i}">
            ${it.status ? `<span class="dot st-${esc(it.status)}"></span>` : ""}
            <span class="t">${esc(it.t)}${it.sub ? ` <span class="muted small">&middot; ${esc(it.sub)}</span>` : ""}</span>
            <span class="k">${esc(it.k)}</span></div>`;
        }).join("")).join("") || `<div class="empty">No matches.</div>`;
      const on = list.querySelector(".palette-item.on");
      if (on) on.scrollIntoView({ block: "nearest" });
    };
    const choose = (i) => { const it = shown[i]; if (it) { close(); it.run(); } };
    input.addEventListener("input", () => { sel = 0; render(); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, shown.length - 1); render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); render(); }
      else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); choose(sel); }
    });
    list.addEventListener("click", (e) => { const it = e.target.closest("[data-i]"); if (it) choose(+it.dataset.i); });
    render();
  },
};

function showShortcuts() {
  const row = (k, d) => `<span>${d}</span><span>${k}</span>`;
  modal({
    title: "Keyboard shortcuts",
    body: `<div class="shortcut-list">
      <div class="g">Anywhere</div>
      ${row("<kbd>Ctrl</kbd> <kbd>K</kbd>", "Search projects, scans &amp; actions")}
      ${row("<kbd>Alt</kbd> <kbd>N</kbd>", "New scan")}
      ${row("<kbd>Alt</kbd> <kbd>1</kbd>&ndash;<kbd>9</kbd>", "Switch to tab 1&ndash;9")}
      ${row("<kbd>Alt</kbd> <kbd>W</kbd>", "Close current tab")}
      ${row("<kbd>Ctrl</kbd> <kbd>S</kbd>", "Save now (changes also save automatically)")}
      ${row("<kbd>?</kbd>", "This list")}
      <div class="g">In a scan</div>
      ${row("<kbd>1</kbd>&ndash;<kbd>5</kbd>", "Details / Documents / Scan / Findings / RFIs")}
      ${row("<kbd>Ctrl</kbd> <kbd>Enter</kbd>", "Run the scan (on the Scan step)")}
      <div class="g">Findings</div>
      ${row("<kbd>J</kbd> / <kbd>K</kbd>", "Next / previous finding")}
      ${row("<kbd>Enter</kbd>", "Expand or collapse")}
      ${row("<kbd>A</kbd> / <kbd>D</kbd> / <kbd>U</kbd>", "Accept / dismiss / reset to open")}
      ${row("<kbd>/</kbd>", "Search findings")}
      <div class="g">RFIs</div>
      ${row("<kbd>J</kbd> / <kbd>K</kbd>", "Next / previous RFI")}
      ${row("<kbd>/</kbd>", "Search RFIs")}
    </div>
    <p class="hint">Tabs: middle-click to close, drag to reorder, right-click for more.</p>`,
  });
}

// =====================================================================
// Project & scan actions (shared by views, menus, palette)
// =====================================================================

function projectOptions(selectedId) {
  return App.projects.filter((p) => !p.archived).map((p) =>
    `<option value="${esc(p.id)}" ${p.id === selectedId ? "selected" : ""}>${esc(p.name)}${p.is_owner ? "" : ` (${esc(p.owner_name)})`}</option>`).join("");
}

function newScanDialog(projectId) {
  const hasProjects = App.projects.some((p) => !p.archived);
  let choice = projectId && hasProjects ? "existing" : "new";
  modal({
    title: "New scan",
    body: `
      <div class="choice ${choice === "new" ? "on" : ""}" data-c="new">
        <input type="radio" name="c" value="new" ${choice === "new" ? "checked" : ""}>
        <div class="grow stack">
          <div><div class="t">New project</div><div class="hint">Start a fresh piece of work.</div></div>
          <input name="name" placeholder="Project name - type to search the company register" list="ref-projects" autocomplete="off">
          <datalist id="ref-projects"></datalist>
          <select name="visibility"><option value="team">Visible to the whole team</option><option value="private">Private to me</option></select>
        </div>
      </div>
      ${hasProjects ? `
      <div class="choice ${choice === "existing" ? "on" : ""}" data-c="existing">
        <input type="radio" name="c" value="existing" ${choice === "existing" ? "checked" : ""}>
        <div class="grow stack">
          <div><div class="t">New revision of an existing project</div><div class="hint">RFI header details and settings carry over from its latest scan.</div></div>
          <select name="project">${projectOptions(projectId)}</select>
          <label class="row small"><input type="checkbox" name="copy" style="width:auto"> Also copy the latest scan's files</label>
        </div>
      </div>` : ""}`,
    actions: [{ label: "Cancel" }, {
      label: "Create scan", primary: true,
      onClick: async (b) => {
        let run;
        let step = "details";
        if (choice === "new") {
          const name = b.querySelector("[name=name]").value.trim();
          if (!name) throw new Error("Give the project a name.");
          const r = await api("/api/projects", { method: "POST", body: { name, visibility: b.querySelector("[name=visibility]").value, reference: refPick(b, name) } });
          run = r.run;
        } else {
          const pid = b.querySelector("[name=project]").value;
          const p = App.projectIndex.get(pid);
          const copy = b.querySelector("[name=copy]").checked && p && p.latest_run;
          run = await api(`/api/projects/${pid}/runs`, { method: "POST", body: copy ? { from_run_id: p.latest_run.id } : {} });
          if (copy) step = "documents";
        }
        await refreshProjects();
        WS.open("run", run.id, { step });
      },
    }],
    onOpen: (b) => {
      const sync = () => {
        $$(".choice", b).forEach((c) => c.classList.toggle("on", c.dataset.c === choice));
        $$("input[type=radio]", b).forEach((r) => { r.checked = r.value === choice; });
      };
      $$(".choice", b).forEach((c) => c.addEventListener("click", () => { choice = c.dataset.c; sync(); }));
      (choice === "new" ? b.querySelector("[name=name]") : b.querySelector("[name=project]")).focus();
      bindReferenceLookup(b);
    },
  });
}

function newProjectDialog() {
  modal({
    title: "New project",
    body: `
      <label class="field"><span>Name</span><input name="name" placeholder="Type to search the company project register" list="ref-projects" autocomplete="off" autofocus>
        <datalist id="ref-projects"></datalist></label>
      <div class="hint" data-ref-hint></div>
      <label class="field"><span>Description (optional)</span><textarea name="description" rows="2" placeholder="Scope, client, anything the team should know"></textarea></label>
      <label class="field"><span>Who can see it</span><select name="visibility"><option value="team">The whole team</option><option value="private">Only me and the members I add</option></select></label>`,
    onOpen: (b) => bindReferenceLookup(b),
    actions: [{ label: "Cancel" }, {
      label: "Create project", primary: true,
      onClick: async (b) => {
        const name = b.querySelector("[name=name]").value.trim();
        if (!name) throw new Error("Give the project a name.");
        const r = await api("/api/projects", { method: "POST", body: {
          name, description: b.querySelector("[name=description]").value, visibility: b.querySelector("[name=visibility]").value, create_run: false,
          reference: refPick(b, name),
        } });
        await refreshProjects();
        WS.open("project", r.project.id);
      },
    }],
  });
}

// Company project register (read-only): typing a name offers matching projects;
// picking one carries its code, client and consultant into the RFI form header.
function bindReferenceLookup(b) {
  const input = b.querySelector("[name=name]");
  const list = b.querySelector("#ref-projects");
  if (!input || !list) return;
  b._refs = [];
  const search = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) return;
    try {
      b._refs = await api(`/api/reference/projects?q=${encodeURIComponent(q)}`);
      list.innerHTML = b._refs.map((p) => `<option value="${esc(p.name)}">${esc(p.code)} &middot; ${esc(p.client || "")}</option>`).join("");
    } catch { /* register unavailable: plain typing still works */ }
  }, 250);
  input.addEventListener("input", () => {
    search();
    const hit = refPick(b, input.value.trim());
    const hint = b.querySelector("[data-ref-hint]");
    if (hint) hint.innerHTML = hit ? `From the project register: <strong>${esc(hit.code)}</strong>${hit.client ? ` &middot; ${esc(hit.client)}` : ""}${hit.consultant ? ` &middot; ${esc(hit.consultant)}` : ""} &mdash; filled into the RFI form header.` : "";
  });
}
function refPick(b, name) { return (b._refs || []).find((p) => p.name === name) || null; }

async function patchProject(p, body, msg) {
  try {
    await api(`/api/projects/${p.id}`, { method: "PATCH", body });
    if (msg) toast(msg, { type: "success", timeout: 2500 });
    await refreshProjects();
  } catch (err) { toastError(err); }
}

async function renameProject(p) {
  const name = await promptDialog("Rename project", { label: "Project name", value: p.name });
  if (name && name !== p.name) patchProject(p, { name }, "Project renamed");
}

function togglePin(p) {
  const pinned = new Set(App.prefs.pinned || []);
  if (pinned.has(p.id)) pinned.delete(p.id); else pinned.add(p.id);
  savePrefs({ pinned: [...pinned] }).then(refreshProjects);
  toast(pinned.has(p.id) ? `Pinned "${p.name}"` : `Unpinned "${p.name}"`, { timeout: 2000 });
}

function setArchived(p, archived) {
  patchProject(p, { archived }, archived ? `Archived "${p.name}"` : `Restored "${p.name}"`);
  if (archived) toast(`"${p.name}" moved to Archived.`, { action: "Undo", onAction: () => patchProject(p, { archived: false }) });
}

async function deleteProject(p) {
  const ok = await confirmDialog(
    `Delete "${p.name}" and all ${plural(p.run_count || 0, "scan")}, uploads and generated documents? This can't be undone.`,
    { title: "Delete project", confirmLabel: "Delete project", danger: true });
  if (!ok) return;
  try {
    await api(`/api/projects/${p.id}`, { method: "DELETE" });
    for (const key of [...WS.tabs]) {
      const v = WS.views.get(key);
      if (v && v.projectId && v.projectId() === p.id) WS.close(key, { force: true });
    }
    toast(`Deleted "${p.name}"`);
    refreshProjects();
  } catch (err) { toastError(err); }
}

function projectMenu(p, anchor) {
  popMenu(anchor, [
    { label: "Open", onClick: () => WS.open("project", p.id) },
    { label: "New scan in this project", onClick: () => newScanDialog(p.id) },
    p.latest_run && { label: "Open latest scan", onClick: () => WS.open("run", p.latest_run.id) },
    "-",
    { label: p.pinned ? "Unpin" : "Pin to sidebar", onClick: () => togglePin(p) },
    { label: "Rename", onClick: () => renameProject(p) },
    { label: p.archived ? "Restore from archive" : "Archive", onClick: () => setArchived(p, !p.archived) },
    "-",
    { label: "Members & access", onClick: () => membersDialog(p) },
    ...PROJECT_MENU_ITEMS.map((f) => f(p)).filter(Boolean),
    "-",
    { label: "Delete project", danger: true, disabled: !canRole(p.my_role, "manager"), title: canRole(p.my_role, "manager") ? "" : "Only project managers can delete", onClick: () => deleteProject(p) },
  ]);
}

async function membersDialog(p) {
  let data;
  try { data = await api(`/api/projects/${p.id}/members`); } catch (err) { toastError(err); return; }
  const manage = canRole(p.my_role, "manager");
  const users = manage ? await api("/api/users").catch(() => []) : [];
  const roleSel = (uid, role) => `<select data-member-role="${esc(uid)}" ${manage ? "" : "disabled"}>
    ${["manager", "engineer", "viewer"].map((r) => `<option value="${r}" ${r === role ? "selected" : ""}>${ROLE_LABEL[r]}</option>`).join("")}</select>`;
  const render = (b) => {
    const inList = new Set([data.owner && data.owner.id, ...data.members.map((m) => m.user_id)]);
    b.querySelector("[data-members]").innerHTML = `
      ${data.owner ? `<li>${avatar(data.owner.display_name, "sm")}<span class="grow">${esc(data.owner.display_name)} <span class="muted">@${esc(data.owner.username)}</span></span><span class="badge outline">Owner</span></li>` : ""}
      ${data.members.map((m) => `<li>${avatar(m.display_name, "sm")}<span class="grow">${esc(m.display_name)} <span class="muted">@${esc(m.username)}</span></span>
        ${roleSel(m.user_id, m.role)}${manage ? `<button class="icon-btn sm" data-member-remove="${esc(m.user_id)}" title="Remove">&times;</button>` : ""}</li>`).join("")}`;
    const add = b.querySelector("[data-member-add]");
    if (add) add.innerHTML = `<option value="">Add a person&hellip;</option>` + users.filter((u) => !inList.has(u.id))
      .map((u) => `<option value="${esc(u.id)}">${esc(u.display_name)} (@${esc(u.username)})</option>`).join("");
  };
  modal({
    title: `Members of ${p.name}`,
    body: `<p class="hint">${p.visibility === "private" ? "This project is <strong>private</strong>: only these people (and administrators) can see it."
      : "This project is visible to the <strong>whole team</strong> with their usual role. Members listed here get the role you choose instead."}</p>
      <ul class="member-list" data-members></ul>
      ${manage ? `<div class="member-add"><select data-member-add></select>
        <select data-member-new-role>${["engineer", "viewer", "manager"].map((r) => `<option value="${r}">${ROLE_LABEL[r]}</option>`).join("")}</select>
        <button class="btn" data-member-add-btn>Add</button></div>` : `<p class="hint">Only project managers can change members.</p>`}`,
    actions: [{ label: "Done", primary: true }],
    onOpen: (b) => {
      render(b);
      const set = async (uid, role) => {
        try { data.members = (await api(`/api/projects/${p.id}/members/${uid}`, { method: "PUT", body: { role } })).members; render(b); }
        catch (err) { toastError(err); }
      };
      b.addEventListener("change", (e) => { const s = e.target.closest("[data-member-role]"); if (s) set(s.dataset.memberRole, s.value); });
      b.addEventListener("click", async (e) => {
        const rm = e.target.closest("[data-member-remove]");
        if (rm) {
          try { data.members = (await api(`/api/projects/${p.id}/members/${rm.dataset.memberRemove}`, { method: "DELETE" })).members; render(b); }
          catch (err) { toastError(err); }
        }
        if (e.target.closest("[data-member-add-btn]")) {
          const uid = b.querySelector("[data-member-add]").value;
          if (uid) set(uid, b.querySelector("[data-member-new-role]").value);
        }
      });
    },
  });
}

async function cloneRun(runId, { seq } = {}) {
  try {
    const r = await api(`/api/runs/${runId}/clone`, { method: "POST" });
    await refreshProjects();
    WS.open("run", r.id, { step: "documents" });
    toast(`Rev ${r.seq} created${seq ? ` from Rev ${seq}` : ""} with the same files and settings. Change what you need, then run it.`, { type: "success", timeout: 7000 });
  } catch (err) { toastError(err); }
}

async function deleteRun(run, projectName) {
  const ok = await confirmDialog(`Delete Rev ${run.seq} of "${projectName}", with its uploads, findings and RFIs? This can't be undone.`,
    { title: "Delete scan", confirmLabel: "Delete scan", danger: true });
  if (!ok) return false;
  try {
    await api(`/api/runs/${run.id}`, { method: "DELETE" });
    WS.close(`run:${run.id}`, { force: true });
    toast(`Deleted Rev ${run.seq}`);
    refreshProjects();
    return true;
  } catch (err) { toastError(err); return false; }
}

// =====================================================================
// Home view
// =====================================================================

class HomeView {
  constructor() {
    this.key = "home";
    this.q = "";
    this.filter = local.get("home.filter", "all");
    this.sort = local.get("home.sort", "updated");
    this.el = el(`<div class="view"><div class="view-inner">
      <div class="page-head greeting">
        <div><h2 data-greet></h2><div class="hint" data-sub></div></div>
        <div class="head-actions">
          <button class="btn" data-act="new-project">New project</button>
          <button class="btn primary" data-act="new-scan">+ New scan</button>
        </div>
      </div>
      <div class="stats" data-stats></div>
      <div data-recent-wrap>
        <div class="section-title"><h3>Continue where you left off</h3></div>
        <div class="recent-grid" data-recent></div>
      </div>
      <div class="section-title"><h3>Projects</h3></div>
      <div class="toolbar">
        <div class="search-box"><input type="search" placeholder="Filter projects by name, owner, description&hellip;" data-q></div>
        <div class="segmented" data-filter>
          ${[["all", "All"], ["mine", "Mine"], ["shared", "Shared with me"], ["pinned", "Pinned"], ["archived", "Archived"]]
            .map(([v, l]) => `<button data-v="${v}" class="${v === this.filter ? "on" : ""}">${l}</button>`).join("")}
        </div>
        <span class="grow"></span>
        <select data-sort aria-label="Sort projects">
          <option value="updated">Recently updated</option>
          <option value="name">Name A&ndash;Z</option>
          <option value="created">Newest first</option>
          <option value="open">Most open findings</option>
        </select>
      </div>
      <div data-table></div>
    </div></div>`);
    $("[data-sort]", this.el).value = this.sort;
    this.el.addEventListener("click", (e) => this.onClick(e));
    $("[data-q]", this.el).addEventListener("input", (e) => { this.q = e.target.value; this.renderTable(); });
    $("[data-sort]", this.el).addEventListener("change", (e) => { this.sort = e.target.value; local.set("home.sort", this.sort); this.renderTable(); });
  }
  tabTitle() { return "Home"; }
  hash() { return "#/"; }
  projectId() { return null; }
  async load() { this.render(); }
  onProjectsChanged() { if (this.loaded) this.render(); }
  onKey(e) { if (e.key === "/") { e.preventDefault(); $("[data-q]", this.el).focus(); } }

  render() {
    const hr = new Date().getHours();
    const greet = hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
    $("[data-greet]", this.el).textContent = `${greet}, ${App.me.display_name.split(" ")[0]}`;
    $("[data-sub]", this.el).textContent = "Compare document sets, triage the conflicts, and send RFIs.";
    this.renderStats();
    this.renderRecent();
    this.renderTable();
  }

  renderStats() {
    const live = App.projects.filter((p) => !p.archived);
    const runs = live.reduce((n, p) => n + (p.run_count || 0), 0);
    const open = live.reduce((n, p) => n + ((p.latest_counts || {}).open || 0), 0);
    const rfis = live.reduce((n, p) => n + ((p.latest_counts || {}).rfis || 0), 0);
    const stat = (v, l) => `<div class="stat"><div class="v">${v}</div><div class="l">${l}</div></div>`;
    $("[data-stats]", this.el).innerHTML = stat(live.length, "Active projects") + stat(runs, "Scans") +
      stat(open, "Open findings") + stat(rfis, "RFIs drafted") + stat(Jobs.activeCount(), "Scans running now");
  }

  renderRecent() {
    const mine = [];
    for (const p of App.projects) {
      if (p.archived) continue;
      for (const r of p.runs || []) if (r.created_by === App.me.id || r.updated_by === App.me.id) mine.push({ r, p });
    }
    mine.sort((a, b) => (b.r.updated_at || "").localeCompare(a.r.updated_at || ""));
    const top = mine.slice(0, 6);
    $("[data-recent-wrap]", this.el).hidden = !top.length;
    $("[data-recent]", this.el).innerHTML = top.map(({ r, p }) => `
      <div class="recent-card" data-run="${esc(r.id)}" tabindex="0">
        <div class="p">${esc(p.name)}</div>
        <div class="t">Rev ${r.seq}${r.title ? ` &middot; ${esc(r.title)}` : ""}</div>
        <div class="f">${statusBadge(r.status)}
          ${r.status === "complete" ? sevChips(r.counts) : `<span>${esc(r.status === "draft" ? "Not run yet" : "")}</span>`}
          <span>${timeEl(r.updated_at)}</span></div>
      </div>`).join("");
  }

  filtered() {
    const q = this.q.toLowerCase().trim();
    let list = App.projects.filter((p) => {
      switch (this.filter) {
        case "mine": return !p.archived && p.is_owner;
        case "shared": return !p.archived && !p.is_owner;
        case "pinned": return !p.archived && p.pinned;
        case "archived": return !!p.archived;
        default: return !p.archived;
      }
    });
    if (q) list = list.filter((p) => `${p.name} ${p.description} ${p.owner_name}`.toLowerCase().includes(q));
    const by = {
      updated: (a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""),
      created: (a, b) => (b.created_at || "").localeCompare(a.created_at || ""),
      name: (a, b) => a.name.localeCompare(b.name),
      open: (a, b) => ((b.latest_counts || {}).open || 0) - ((a.latest_counts || {}).open || 0),
    }[this.sort];
    return list.sort((a, b) => (b.pinned - a.pinned) || by(a, b));
  }

  renderTable() {
    const list = this.filtered();
    const wrap = $("[data-table]", this.el);
    if (!App.projects.length) {
      wrap.innerHTML = `<div class="card empty"><div class="big">&#128193;</div><h3>No projects yet</h3>
        <p>A project holds every scan of one job, so you can come back to any revision.</p>
        <button class="btn primary lg" data-act="new-scan">Start your first scan</button></div>`;
      return;
    }
    if (!list.length) {
      wrap.innerHTML = `<div class="card empty"><h3>Nothing matches</h3><p>Try a different filter or search.</p></div>`;
      return;
    }
    wrap.innerHTML = `<div class="table-wrap"><table class="list">
      <thead><tr><th>Project</th><th>Owner</th><th>Scans</th><th>Latest</th><th title="From the newest completed revision">Findings</th><th>Open</th><th>RFIs</th><th>Updated</th><th></th></tr></thead>
      <tbody>${list.map((p) => {
        const r = p.latest_run;
        const done = p.latest_complete;
        return `<tr data-project="${esc(p.id)}">
          <td><div class="name">${p.pinned ? '<span title="Pinned" style="color:var(--major)">&#9733;</span> ' : ""}${esc(p.name)}
            ${p.visibility === "private" ? '<span class="badge outline" title="Only the owner can see this">Private</span>' : ""}
            ${p.archived ? '<span class="badge outline">Archived</span>' : ""}</div>
            ${p.description ? `<div class="desc">${esc(p.description)}</div>` : ""}</td>
          <td><span class="row nowrap">${avatar(p.owner_name, "sm")}${esc(p.is_owner ? "You" : p.owner_name)}</span></td>
          <td>${p.run_count || 0}</td>
          <td class="nowrap">${r ? `${statusBadge(r.status)} <span class="muted small">Rev ${r.seq}</span>` : '<span class="muted">&ndash;</span>'}</td>
          <td>${done ? sevChips(done.counts) + (done !== r ? ` <span class="muted small">Rev ${done.seq}</span>` : "") : '<span class="muted">&ndash;</span>'}</td>
          <td>${done ? (done.counts.open || 0) : '<span class="muted">&ndash;</span>'}</td>
          <td>${done ? (done.counts.rfis || 0) : '<span class="muted">&ndash;</span>'}</td>
          <td class="nowrap muted">${timeEl(p.updated_at)}</td>
          <td class="actions"><button class="icon-btn sm" data-menu="${esc(p.id)}" title="More" aria-label="More actions">&#8943;</button></td>
        </tr>`;
      }).join("")}</tbody></table></div>`;
  }

  onClick(e) {
    const act = e.target.closest("[data-act]");
    if (act) {
      if (act.dataset.act === "new-scan") newScanDialog();
      if (act.dataset.act === "new-project") newProjectDialog();
      return;
    }
    const f = e.target.closest("[data-filter] button");
    if (f) {
      this.filter = f.dataset.v;
      local.set("home.filter", this.filter);
      $$("[data-filter] button", this.el).forEach((b) => b.classList.toggle("on", b === f));
      this.renderTable();
      return;
    }
    const menu = e.target.closest("[data-menu]");
    if (menu) { e.stopPropagation(); projectMenu(App.projectIndex.get(menu.dataset.menu), menu); return; }
    const row = e.target.closest("[data-project]");
    if (row) { WS.open("project", row.dataset.project); return; }
    const card = e.target.closest("[data-run]");
    if (card) WS.open("run", card.dataset.run);
  }
}

// =====================================================================
// Project view: settings + revision history
// =====================================================================

class ProjectView {
  constructor(id) {
    this.id = id;
    this.key = `project:${id}`;
    this.data = null;
    this.el = el(`<div class="view"><div class="view-inner"></div></div>`);
    this.inner = $(".view-inner", this.el);
    this.descSaver = new Saver((patch) => api(`/api/projects/${id}`, { method: "PATCH", body: patch }).then((r) => { refreshSoon(); return r; }),
      { onState: (s, err) => { const n = $("[data-save]", this.el); if (n) n.innerHTML = saveStateHtml(s, err); } });
    this.el.addEventListener("click", (e) => this.onClick(e));
  }
  project() { return App.projectIndex.get(this.id) || this.data; }
  tabTitle() { const p = this.project(); return p ? p.name : "Project"; }
  tabTooltip() { return `Project: ${this.tabTitle()}`; }
  hash() { return `#/project/${this.id}`; }
  projectId() { return this.id; }
  copyLink() { navigator.clipboard.writeText(location.origin + "/" + this.hash()).then(() => toast("Link copied", { timeout: 1500 })); }
  async load() { this.data = await api(`/api/projects/${this.id}`); this.render(); }
  async reload() { await this.load(); }
  flushAll() { return this.descSaver.flush().catch(() => {}); }
  destroy() { this.descSaver.dispose(); }
  onProjectsChanged() {
    if (!this.loaded) return;
    if (!App.projectIndex.has(this.id)) return;
    api(`/api/projects/${this.id}`).then((d) => { this.data = d; this.renderRuns(); this.renderHeadMeta(); }).catch(() => {});
  }

  render() {
    const p = this.data;
    this.inner.innerHTML = `
      <div class="page-head">
        <div class="grow" style="min-width:260px">
          <div class="crumbs"><a href="#/" data-home>Projects</a><span>&rsaquo;</span></div>
          <input class="title-input" data-name value="${esc(p.name)}" aria-label="Project name" size="${Math.max(10, p.name.length + 2)}">
          <div class="meta" data-meta></div>
        </div>
        <div class="head-actions">
          <button class="btn" data-act="menu" aria-label="More">&#8943;</button>
          <button class="btn" data-act="members" title="Who can see and edit this project">&#128101; Members</button>
          ${PROJECT_ACTIONS.map((a) => a.show && !a.show(p) ? "" : `<button class="btn" data-project-action="${esc(a.key)}">${a.label}</button>`).join("")}
          ${p.latest_complete ? `<button class="btn" data-act="open-latest">Open latest results</button>` : ""}
          ${p.latest_complete && canRole(p.my_role, "engineer") ? `<button class="btn" data-act="rerun">Edit &amp; re-run Rev ${p.latest_complete.seq}</button>` : ""}
          ${canRole(p.my_role, "engineer") ? `<button class="btn primary" data-act="new-scan">+ New scan</button>` : ""}
        </div>
      </div>
      ${p.my_role === "viewer" ? `<div class="banner info"><span class="grow"><strong>View only.</strong> You can open every revision and download its outputs; ask a project manager for edit access.</span></div>` : ""}
      <div class="card">
        <label class="field"><span>Description</span>
          <textarea data-desc rows="2" ${canRole(p.my_role, "engineer") ? "" : "readonly"} placeholder="Scope, client, package, anything the team should know">${esc(p.description)}</textarea></label>
      </div>
      <div data-project-extras></div>
      <div class="section-title"><h3>Scans</h3><span class="hint">Every revision is kept. Open any one to review or edit its results, or make a new revision from it.</span></div>
      <div data-runs></div>`;
    this.renderHeadMeta();
    this.renderRuns();
    for (const hook of PROJECT_SECTIONS) hook(this, $("[data-project-extras]", this.el));
    const name = $("[data-name]", this.el);
    if (!canRole(p.my_role, "engineer")) name.readOnly = true;
    const saveName = () => {
      const v = name.value.trim();
      if (!v) { name.value = this.data.name; return; }
      if (v !== this.data.name) { this.data.name = v; patchProject(this.data, { name: v }); }
    };
    name.addEventListener("blur", saveName);
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") name.blur(); if (e.key === "Escape") { name.value = this.data.name; name.blur(); } });
    name.addEventListener("input", () => { name.size = Math.max(10, name.value.length + 2); });
    const desc = $("[data-desc]", this.el);
    desc.addEventListener("input", () => { autoGrow(desc); this.descSaver.schedule({ description: desc.value }); });
    requestAnimationFrame(() => autoGrow(desc));
  }

  renderHeadMeta() {
    const p = this.data;
    const meta = $("[data-meta]", this.el);
    if (!meta) return;
    meta.innerHTML = `
      <span class="row">${avatar(p.owner_name, "sm")} Owner: ${esc(p.is_owner ? "you" : p.owner_name)}</span>
      ${p.code ? `<span class="badge outline mono" title="Project code">${esc(p.code)}</span>` : ""}
      <span class="badge role-${esc(p.my_role)}" title="Your role in this project">${esc(ROLE_LABEL[p.my_role] || p.my_role)}</span>
      <span>Created ${timeEl(p.created_at)}</span>
      ${canRole(p.my_role, "manager") ? `<select data-vis style="width:auto;padding:2px 8px;font-size:12px;border-radius:999px">
          <option value="team" ${p.visibility === "team" ? "selected" : ""}>Visible to the team</option>
          <option value="private" ${p.visibility === "private" ? "selected" : ""}>Private (members only)</option></select>`
        : `<span class="badge outline">${p.visibility === "private" ? "Private" : "Team"}</span>`}
      ${p.archived ? `<span class="badge outline">Archived</span> <button class="link-btn" data-act="unarchive">Restore</button>` : ""}
      <span data-save></span>`;
    const vis = $("[data-vis]", meta);
    if (vis) vis.addEventListener("change", () => patchProject(p, { visibility: vis.value },
      vis.value === "private" ? "Only you can see this project now" : "The team can see this project now"));
  }

  renderRuns() {
    const runs = this.data.runs || [];
    const wrap = $("[data-runs]", this.el);
    if (!wrap) return;
    if (!runs.length) {
      wrap.innerHTML = `<div class="card empty"><div class="big">&#128269;</div><h3>No scans yet</h3>
        <p>Upload two document sets and ConflictScan lists the conflicts and drafts the RFIs.</p>
        <button class="btn primary lg" data-act="new-scan">Start a scan</button></div>`;
      return;
    }
    const seqOf = Object.fromEntries(runs.map((r) => [r.id, r.seq]));
    wrap.innerHTML = `<div class="table-wrap"><table class="list">
      <thead><tr><th>Rev</th><th>Title</th><th>Status</th><th>Findings</th><th>Open</th><th>RFIs</th><th>Created by</th><th>Updated</th><th></th></tr></thead>
      <tbody>${runs.map((r) => `<tr data-run="${esc(r.id)}">
        <td class="nowrap"><strong>Rev ${r.seq}</strong></td>
        <td><div class="name">${esc(r.title) || '<span class="muted">Untitled</span>'}</div>
          ${r.parent_run_id && seqOf[r.parent_run_id] ? `<div class="desc">from Rev ${seqOf[r.parent_run_id]}</div>` : ""}
          ${r.status === "failed" && r.error ? `<div class="desc" style="color:var(--danger)" title="${esc(r.error)}">${esc(r.error)}</div>` : ""}</td>
        <td class="nowrap">${statusBadge(r.status)}</td>
        <td>${r.status === "complete" ? sevChips(r.counts) : '<span class="muted">&ndash;</span>'}</td>
        <td>${r.status === "complete" ? r.counts.open : '<span class="muted">&ndash;</span>'}</td>
        <td>${r.status === "complete" ? r.counts.rfis : '<span class="muted">&ndash;</span>'}</td>
        <td><span class="row nowrap">${avatar(r.created_by_name, "sm")}${esc(r.created_by === App.me.id ? "You" : r.created_by_name)}</span></td>
        <td class="nowrap muted">${timeEl(r.updated_at)}</td>
        <td class="actions"><button class="icon-btn sm" data-run-menu="${esc(r.id)}" aria-label="More actions">&#8943;</button></td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  async onClick(e) {
    const p = this.project();
    if (e.target.closest("[data-home]")) { e.preventDefault(); WS.activate("home"); return; }
    const act = e.target.closest("[data-act]");
    if (act) {
      const a = act.dataset.act;
      if (a === "new-scan") {
        try {
          const run = await api(`/api/projects/${this.id}/runs`, { method: "POST", body: {} });
          await refreshProjects();
          WS.open("run", run.id, { step: "details" });
        } catch (err) { toastError(err); }
      }
      if (a === "rerun" && this.data.latest_complete) cloneRun(this.data.latest_complete.id, { seq: this.data.latest_complete.seq });
      if (a === "open-latest" && this.data.latest_complete) WS.open("run", this.data.latest_complete.id, { step: "findings" });
      if (a === "menu" && p) projectMenu(p, act);
      if (a === "unarchive" && p) setArchived(p, false);
      if (a === "members") membersDialog(this.data);
      return;
    }
    const pa = e.target.closest("[data-project-action]");
    if (pa) { const def = PROJECT_ACTIONS.find((x) => x.key === pa.dataset.projectAction); if (def) def.run(this.data, this); return; }
    const menuBtn = e.target.closest("[data-run-menu]");
    if (menuBtn) {
      e.stopPropagation();
      const r = this.data.runs.find((x) => x.id === menuBtn.dataset.runMenu);
      popMenu(menuBtn, [
        { label: "Open", onClick: () => WS.open("run", r.id) },
        r.status === "complete" && { label: "Open findings", onClick: () => WS.open("run", r.id, { step: "findings" }) },
        r.status === "complete" && { label: "Open RFIs", onClick: () => WS.open("run", r.id, { step: "rfis" }) },
        "-",
        { label: r.status === "draft" ? "Duplicate" : "Edit & re-run (new revision)", onClick: () => cloneRun(r.id, { seq: r.seq }) },
        "-",
        { label: "Delete scan", danger: true, disabled: ACTIVE.has(r.status), onClick: () => deleteRun(r, this.data.name).then((ok) => ok && this.reload()) },
      ]);
      return;
    }
    const row = e.target.closest("[data-run]");
    if (row) WS.open("run", row.dataset.run);
  }
}

// =====================================================================
// Run view: one scan revision, all stages
// =====================================================================

const STEPS = [["details", "Details"], ["documents", "Documents"], ["scan", "Scan"], ["findings", "Findings"], ["rfis", "RFIs"]];
const META_FIELDS = [
  ["consultant", "Consultant", "e.g. Dar Al Handasah"], ["employer", "Employer", "e.g. ALSTOM Egypt"],
  ["project_code", "Project code", ""], ["to", "To", "RFI recipient"], ["location", "Location", ""],
  ["boq_no", "BOQ No.", ""], ["dwg_no", "Dwg No.", ""], ["level", "Level", ""], ["specs_no", "Specs No.", ""],
];
const DRAWING_ONLY = ["dxf", "png", "jpg", "jpeg", "tif", "tiff", "bmp"];
const DOCUMENT_ONLY = ["docx", "doc", "xlsx", "xls", "xlsm", "csv", "txt"];
const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.dxf,.doc,.docx,.xlsx,.xls,.xlsm,.csv,.txt";
const DISCIPLINES = [["survey", "Survey"], ["civil", "Civil"], ["structural", "Structural"], ["electrical", "Electrical"],
  ["mechanical", "Mechanical"], ["plumbing", "Plumbing"], ["architectural", "Architectural"], ["other", "Other"]];
const RFI_STATUS = [["draft", "Draft"], ["ready", "Ready for review"], ["issued", "Issued"], ["closed", "Closed"]];
const RFI_FIELDS = [
  ["subject", "Subject", 1], ["area", "Block / area", 1], ["background", "Background", 2],
  ["discrepancy_summary", "Points (as they go on the form)", 8], ["question", "Information requested", 4],
  ["proposed_solution", "Proposed way forward", 2], ["commercial_note", "Commercial note", 2],
  ["cost_impact", "Cost impact", 1], ["schedule_impact", "Programme impact", 1], ["attachments", "Attachments (one per line)", 3],
];

class RunView {
  constructor(id) {
    this.id = id;
    this.key = `run:${id}`;
    this.run = null;
    this.step = null;
    this.filters = { q: "", sev: new Set(SEVERITIES), status: "all", scope: "all", cls: "all", sort: "found" };
    this.expanded = new Set();
    this.focusId = null;
    this.selectedRfi = null;
    this.rfiQuery = "";
    this.rfiSavers = new Map();
    this.noteSavers = new Map();
    this.pollTimer = null;
    this.tickTimer = null;
    this.el = el(`<div class="view"><div class="view-inner">
      <div data-head></div><nav class="stepper" data-stepper aria-label="Scan stages"></nav><div data-body></div></div></div>`);
    const onState = (s, err) => { const n = $("[data-save]", this.el); if (n) n.innerHTML = saveStateHtml(s, err); };
    this.inputSaver = new Saver((patch) => api(`/api/runs/${id}`, { method: "PATCH", body: { inputs: patch, base_inputs_rev: (this.run.inputs || {})._rev || 0 } })
      .then((r) => { this.run.status = r.status; this.run.inputs_editable = r.inputs_editable; this.run.inputs._rev = r.inputs._rev; return r; })
      .catch((err) => this.onInputsConflict(err, patch)), { onState });
    this.presenceTimer = null;
    this.others = [];
    this.titleSaver = new Saver((patch) => api(`/api/runs/${id}`, { method: "PATCH", body: patch }).then((r) => { refreshSoon(); return r; }), { onState });
    this.el.addEventListener("click", (e) => this.onClick(e));
  }

  // ---- tab integration ------------------------------------------------
  info() { return App.runIndex.get(this.id); }
  projectName() { return (this.run && this.run.project && this.run.project.name) || ((this.info() || {}).project || {}).name || "Scan"; }
  seq() { return this.run ? this.run.seq : ((this.info() || {}).run || {}).seq; }
  tabTitle() { const s = this.seq(); return s ? `${this.projectName()} · Rev ${s}` : "Scan"; }
  tabTooltip() { const t = this.run && this.run.title; return `${this.tabTitle()}${t ? " - " + t : ""}`; }
  tabStatus() {
    const j = Jobs.list.find((r) => r.id === this.id);
    return (j && ACTIVE.has(j.status) ? j.status : null) || (this.run ? this.run.status : ((this.info() || {}).run || {}).status) || "draft";
  }
  hash() { return `#/run/${this.id}${this.step ? "/" + this.step : ""}`; }
  projectId() { return this.run ? this.run.project_id : ((this.info() || {}).run || {}).project_id; }
  copyLink() { navigator.clipboard.writeText(location.origin + "/" + this.hash()).then(() => toast("Link copied - anyone on the team with access can open it", { timeout: 2500 })); }
  setInitialStep(step) { if (STEPS.some(([k]) => k === step)) { if (this.loaded) this.goto(step); else this.step = step; } }
  onShow() { this.syncTimers(); this.startPresence(); }
  onHide() { this.syncTimers(true); this.stopPresence(); }
  readOnly() { return this.run && this.run.my_role === "viewer"; }

  // ---- presence: "Sara is viewing this" ---------------------------------------
  startPresence() {
    this.stopPresence(false);
    const ping = async () => {
      try {
        const others = await api("/api/presence", { method: "POST", body: { target: `run:${this.id}`, detail: this.step || "" } });
        const changed = JSON.stringify(others) !== JSON.stringify(this.others);
        this.others = others;
        if (changed) this.renderPresence();
      } catch { /* presence is best-effort */ }
    };
    ping();
    this.presenceTimer = setInterval(ping, 20000);
  }
  stopPresence(leave = true) {
    clearInterval(this.presenceTimer);
    this.presenceTimer = null;
    if (leave) api("/api/presence", { method: "POST", body: { target: `run:${this.id}`, leave: true } }).catch(() => {});
  }
  renderPresence() {
    const n = $("[data-presence]", this.el);
    if (!n) return;
    const STEP_NAME = Object.fromEntries(STEPS);
    n.innerHTML = this.others.length ? `<span class="presence" title="${esc(this.others.map((o) => `${o.display_name}${o.detail ? " - " + (STEP_NAME[o.detail] || o.detail) : ""}`).join("\n"))}">
      ${this.others.slice(0, 4).map((o) => avatar(o.display_name, "sm")).join("")}
      <span>${esc(this.others.length === 1 ? `${this.others[0].display_name} is viewing this` : `${this.others.length} others are viewing this`)}</span></span>` : "";
  }

  // ---- edit conflicts ----------------------------------------------------------
  async onInputsConflict(err, patch) {
    if (!(err.status === 409 && err.data && err.data.conflict)) throw err;
    this.run.inputs = { ...err.data.inputs };
    toast(`${err.data.updated_by_name || "Someone"} changed these settings while you were typing. Their version is shown; re-enter your change if it's still needed.`,
      { type: "error", timeout: 9000 });
    this.renderStep();
    return { inputs: this.run.inputs, status: this.run.status, inputs_editable: this.run.inputs_editable };
  }

  async onRfiConflict(no, err, patch) {
    if (!(err.status === 409 && err.data && err.data.conflict)) throw err;
    const theirs = err.data.rfi;
    const keepMine = await confirmDialog(`${theirs.edited_by || "Someone"} saved ${no} while you were editing it.
      Keep your changes (overwriting theirs), or load their version?`, { title: "Edited by someone else", confirmLabel: "Keep my changes" });
    const items = this.results().rfi_items || [];
    const i = items.findIndex((x) => x.rfi_no === no);
    if (keepMine) {
      const server = await api(`/api/runs/${this.id}/rfis/${encodeURIComponent(no)}`, { method: "PUT", body: patch });
      if (i >= 0) items[i].rev = server.rev;
      return server;
    }
    if (i >= 0) items[i] = theirs;
    if (this.selectedRfi === no) this.renderRfiEditor();
    this.renderRfiList();
    return theirs;
  }
  onProjectsChanged() { if (this.loaded) this.renderHead(); }
  async flushAll() {
    await Promise.allSettled([this.inputSaver, this.titleSaver, ...this.rfiSavers.values(), ...this.noteSavers.values()].map((s) => s.flush()));
  }
  destroy() {
    this.syncTimers(true);
    this.stopPresence();
    [this.inputSaver, this.titleSaver, ...this.rfiSavers.values(), ...this.noteSavers.values()].forEach((s) => s.dispose());
  }

  // ---- data -----------------------------------------------------------
  async load() {
    this.run = await api(`/api/runs/${this.id}`);
    if (!this.step || !this.available(this.step)) this.step = this.defaultStep();
    this.render();
    this.syncTimers();
    if (WS.active === this.key) this.startPresence();
  }
  async reload() {
    await this.flushAll();
    try {
      this.run = await api(`/api/runs/${this.id}`);
    } catch (err) { if (err.status === 404) WS.close(this.key, { force: true }); else toastError(err); return; }
    if (!this.available(this.step)) this.step = this.defaultStep();
    this.render();
    this.syncTimers();
    WS.renderBar();
  }
  defaultStep() {
    const s = this.run.status;
    if (s === "complete") return "findings";
    if (s !== "draft") return "scan";
    return this.run.files.length ? "documents" : "details";
  }
  available(step) { return step !== "findings" && step !== "rfis" ? true : !!this.run.results; }
  inputs() { return this.run.inputs || (this.run.inputs = {}); }
  results() { return this.run.results || {}; }
  review() { return ((this.run.review || (this.run.review = {})).findings) || (this.run.review.findings = {}); }
  labelA() { return (this.results().document_a || {}).label || this.inputs().document_a_label || "Set A"; }
  labelB() { return (this.results().document_b || {}).label || this.inputs().document_b_label || "Set B"; }
  locked() { return !this.run.inputs_editable || this.readOnly(); }

  syncTimers(stop = false) {
    clearInterval(this.pollTimer); this.pollTimer = null;
    clearInterval(this.tickTimer); this.tickTimer = null;
    if (stop || !this.run || !ACTIVE.has(this.run.status) || WS.active !== this.key) return;
    this.pollTimer = setInterval(() => this.pollProgress(), 2000);
    this.tickTimer = setInterval(() => this.tickElapsed(), 1000);
  }

  async pollProgress() {
    let r;
    try { r = await api(`/api/runs/${this.id}/progress`); } catch { return; }
    const was = this.run.status;
    if (!ACTIVE.has(r.status)) {
      Object.assign(this.run, r);
      if (Jobs.known.get(this.id) && ACTIVE.has(Jobs.known.get(this.id))) Jobs.poll();   // let Jobs raise the notification
      this.render();
      this.syncTimers();
      refreshSoon();
      return;
    }
    this.run.status = r.status;
    this.run.progress = r.progress;
    this.run.queue_position = r.queue_position;
    this.run.started_at = r.started_at;
    if (was !== r.status) { this.renderHead(); if (this.step === "scan") this.renderStep(); }
    else if (this.step === "scan") this.renderProgress();
  }

  tickElapsed() {
    const n = $("[data-elapsed]", this.el);
    if (n && this.run.started_at) n.textContent = fmtDuration((Date.now() - new Date(this.run.started_at)) / 1000);
  }

  // ---- layout ---------------------------------------------------------
  render() {
    this.el.classList.toggle("readonly", !!this.readOnly());
    this.renderHead(); this.renderStepper(); this.renderStep();
  }

  renderHead() {
    const r = this.run;
    if (!r) return;
    const head = $("[data-head]", this.el);
    const siblings = (App.projectIndex.get(r.project_id) || {}).runs || [];
    const hasFocus = head.contains(document.activeElement);
    if (hasFocus) { this.renderHeadMetaOnly(); return; }
    head.innerHTML = `
      <div class="page-head">
        <div class="grow" style="min-width:280px">
          <div class="crumbs">
            <a href="#/" data-home>Projects</a><span>&rsaquo;</span>
            <a href="#/project/${esc(r.project_id)}" data-project-link>${esc(this.projectName())}</a><span>&rsaquo;</span>
            ${siblings.length > 1 ? `<select data-rev style="width:auto;padding:1px 6px;font-size:12px;border-radius:6px" aria-label="Switch revision">
              ${siblings.map((s) => `<option value="${esc(s.id)}" ${s.id === r.id ? "selected" : ""}>Rev ${s.seq} &middot; ${esc(STATUS_LABEL[s.status])}</option>`).join("")}
              </select>` : `<span>Rev ${r.seq}</span>`}
          </div>
          <input class="title-input" data-title value="${esc(r.title)}" placeholder="Rev ${r.seq} - add a title" aria-label="Scan title"
            size="${Math.max(18, (r.title || "").length + 2)}">
          <div class="meta" data-meta></div>
        </div>
        <div class="head-actions" data-actions></div>
      </div>`;
    this.renderHeadMetaOnly();
    const title = $("[data-title]", head);
    title.addEventListener("input", () => { title.size = Math.max(18, title.value.length + 2); this.run.title = title.value; this.titleSaver.schedule({ title: title.value }); });
    title.addEventListener("keydown", (e) => { if (e.key === "Enter") title.blur(); });
    const rev = $("[data-rev]", head);
    if (rev) rev.addEventListener("change", () => WS.open("run", rev.value, { replace: this.key }));
  }

  renderHeadMetaOnly() {
    const r = this.run;
    const meta = $("[data-meta]", this.el);
    const acts = $("[data-actions]", this.el);
    if (!meta || !acts) return;
    const status = this.tabStatus();
    meta.innerHTML = `${statusBadge(status)}
      <span class="row">${avatar(r.created_by_name, "sm")} ${esc(r.created_by === App.me.id ? "You" : r.created_by_name)} &middot; created ${timeEl(r.created_at)}</span>
      ${r.updated_by_name ? `<span>edited ${timeEl(r.updated_at)} by ${esc(r.updated_by === App.me.id ? "you" : r.updated_by_name)}</span>` : ""}
      ${r.results && r.results.duration_seconds ? `<span>scan took ${fmtDuration(r.results.duration_seconds)}</span>` : ""}
      ${this.readOnly() ? `<span class="badge role-viewer">View only</span>` : ""}
      <span data-presence></span>
      <span data-save></span>`;
    this.renderPresence();
    const btns = [];
    const ro = this.readOnly();
    if (ACTIVE.has(status) && !ro) btns.push(`<button class="btn danger" data-act="cancel">Cancel scan</button>`);
    for (const extra of RUN_HEAD_ACTIONS) { const h = extra(this); if (h) btns.push(h); }
    if (r.results) btns.push(`<a class="btn" href="/api/runs/${esc(r.id)}/bundle" data-act="bundle" title="Register + every RFI document">Download all (.zip)</a>`);
    if (status === "complete" && !ro) btns.push(`<button class="btn primary" data-act="clone" title="Make a new revision with these files and settings">Edit &amp; re-run</button>`);
    btns.unshift(`<button class="btn" data-act="menu" aria-label="More">&#8943;</button>`);
    acts.innerHTML = btns.join("");
  }

  renderStepper() {
    const r = this.run;
    const c = countsOf(r);
    const nFiles = r.files.length + ["a", "b"].filter((s) => (this.inputs()[`document_${s}_text`] || "").trim()).length;
    const past = r.status !== "draft";
    const badge = {
      documents: nFiles ? `<span class="c">${nFiles}</span>` : "",
      scan: ACTIVE.has(r.status) ? `<span class="spinner" style="width:12px;height:12px"></span>` : "",
      findings: r.results ? `<span class="c" title="${c.open} still open of ${c.findings} findings">${c.findings && c.open ? `${c.open} open` : c.findings}</span>` : "",
      rfis: r.results ? `<span class="c">${c.rfis}</span>` : "",
    };
    const done = { details: past, documents: past, scan: r.status === "complete", findings: r.results && c.findings && !c.open, rfis: false };
    $("[data-stepper]", this.el).innerHTML = STEPS.map(([k, label], i) => `
      <button class="step ${k === this.step ? "on" : ""} ${done[k] ? "done" : ""}" data-step="${k}" ${this.available(k) ? "" : "disabled title=\"Available once the scan completes\""}>
        <span class="n">${done[k] ? "&#10003;" : i + 1}</span>${label}${badge[k] || ""}</button>`).join("");
  }

  goto(step) {
    if (!this.available(step) || step === this.step) { if (step === this.step) this.renderStep(); return; }
    this.step = step;
    this.renderStepper();
    this.renderStep();
    this.el.scrollTop = 0;
    WS.updateHash();
  }

  renderStep() {
    // A fresh container each time, so per-step delegated listeners never stack up.
    const old = $("[data-body]", this.el);
    const body = old.cloneNode(false);
    old.replaceWith(body);
    this[`step_${this.step}`](body);
    requestAnimationFrame(() => $$("textarea", body).forEach(autoGrow));
  }

  lockedBanner() {
    if (this.readOnly()) return `<div class="banner info"><span class="grow"><strong>View only.</strong> You can read this revision and download its outputs.</span></div>`;
    if (!this.locked()) return "";
    if (ACTIVE.has(this.run.status)) return `<div class="banner info"><span class="spinner"></span><span class="grow">A scan is running with these inputs, so they can't be changed right now.</span></div>`;
    return `<div class="banner info"><span class="grow"><strong>These inputs are locked</strong> because this revision has been scanned.
      To change files or settings, make a new revision - its files and settings are copied over.</span>
      <button class="btn sm primary" data-act="clone">Edit &amp; re-run</button></div>`;
  }

  // ---- step 1: details ------------------------------------------------
  step_details(body) {
    const meta = this.inputs().rfi_form_meta || {};
    const dis = this.locked() ? "disabled" : "";
    body.innerHTML = `${this.lockedBanner()}
      <section class="card">
        <div class="card-head"><div><h3>RFI form details</h3>
          <p class="hint">Filled into the header of every RFI generated for this scan. All optional, and carried over from this project's previous scan.</p></div>
          ${!this.locked() ? `<button class="btn sm ghost" data-act="clear-meta">Clear all</button>` : ""}</div>
        <div class="form-grid">${META_FIELDS.map(([k, l, ph]) => `
          <label class="field"><span>${l}</span><input data-meta="${k}" value="${esc(meta[k])}" placeholder="${esc(ph)}" ${dis}></label>`).join("")}
        </div>
        <p class="hint" style="margin-top:14px">RFI form: ${App.template && App.template.loaded
          ? `<strong>${esc(App.template.filename)}</strong> (${App.template.tokens.length} placeholders)`
          : "none loaded - RFIs will be drafted on screen only. Drop the .docx form into templates_rfi/."}</p>
      </section>
      <div class="step-nav"><span></span><button class="btn primary" data-step="documents">Next: Documents &rarr;</button></div>`;
    body.addEventListener("input", (e) => {
      const k = e.target.dataset.meta;
      if (!k) return;
      (this.inputs().rfi_form_meta = this.inputs().rfi_form_meta || {})[k] = e.target.value;
      this.inputSaver.schedule({ rfi_form_meta: { [k]: e.target.value } });
    });
  }

  // ---- step 2: documents ---------------------------------------------
  step_documents(body) {
    body.innerHTML = `${this.lockedBanner()}<div class="slots">${["a", "b"].map((s) => this.slotHtml(s)).join("")}</div>
      <p class="hint" style="margin-top:12px">Type <em>Auto</em> decides per file: sheets larger than A4, sketches, images and DXF are read as drawings;
      BOQs, specs and letters as documents (scanned pages are OCR-read). A DXF with the same file name as a PDF is read together with it.
      DWG isn't supported - save it as DXF first.</p>
      <div class="step-nav"><button class="btn ghost" data-step="details">&larr; Details</button><button class="btn primary" data-step="scan">Next: Scan &rarr;</button></div>`;
    for (const slot of ["a", "b"]) {
      const card = $(`.slot.${slot}`, body);
      this.renderFiles(slot);
      if (this.locked()) continue;
      const input = $("input[type=file]", card);
      const drop = $(".drop", card);
      input.addEventListener("change", () => { this.uploadTo(slot, [...input.files]); input.value = ""; });
      drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); this.uploadTo(slot, [...e.dataTransfer.files]); });
      $(".slot-name", card).addEventListener("input", (e) => {
        this.inputs()[`document_${slot}_label`] = e.target.value;
        this.inputSaver.schedule({ [`document_${slot}_label`]: e.target.value });
      });
      $("textarea", card).addEventListener("input", (e) => {
        autoGrow(e.target);
        this.inputs()[`document_${slot}_text`] = e.target.value;
        this.inputSaver.schedule({ [`document_${slot}_text`]: e.target.value });
        this.renderStepper();
      });
    }
    body.addEventListener("change", (e) => {
      const sel = e.target.closest("select[data-kind]");
      if (sel) this.patchFile(sel.dataset.kind, { kind: sel.value });
    });
  }

  slotHtml(slot) {
    const lab = this.inputs()[`document_${slot}_label`] || "";
    const text = this.inputs()[`document_${slot}_text`] || "";
    const S = slot.toUpperCase();
    const locked = this.locked();
    return `<section class="card slot ${slot}">
      <div class="slot-tag">Set ${S}</div>
      <input class="slot-name" value="${esc(lab)}" placeholder="${slot === "a" ? "Name this set, e.g. Tender BOQ + Specs" : "Name this set, e.g. IFC Drawings"}" ${locked ? "disabled" : ""} aria-label="Set ${S} name">
      <p class="hint">Used to refer to this set in every finding and RFI.</p>
      ${locked ? "" : `<label class="drop"><input type="file" multiple hidden accept="${ACCEPT}">
        <div class="big">&#8682;</div><strong>Drop files here, or click to browse</strong>
        <div class="hint">PDF (scanned or not) &middot; images &middot; DXF &middot; Word &middot; Excel &middot; CSV &middot; TXT</div></label>`}
      <ul class="files" data-files="${slot}"></ul>
      <details ${text ? "open" : ""} style="margin-top:12px"><summary>&hellip;or paste text${text ? ` (${text.length.toLocaleString()} characters)` : ""}</summary>
        <textarea rows="4" placeholder="Paste document text here (added to the set)" ${locked ? "disabled" : ""} style="margin-top:8px">${esc(text)}</textarea></details>
    </section>`;
  }

  renderFiles(slot) {
    const ul = $(`[data-files="${slot}"]`, this.el);
    if (!ul) return;
    const files = this.run.files.filter((f) => f.slot === slot);
    const locked = this.locked();
    const other = slot === "a" ? "B" : "A";
    ul.innerHTML = files.map((f) => {
      const ext = extOf(f.name);
      const guess = f.detected_kind ? ` (${f.detected_kind})` : "";
      const facts = [fmtBytes(f.size)];
      if (f.pages) facts.push(plural(f.pages, ext === "pdf" ? "page" : "frame"));
      if (f.inspecting) facts.push(`<span class="spinner sm"></span> checking&hellip;`);
      else if (f.kind === "auto" && f.detected_reason && !f.problem) facts.push(esc(f.detected_reason));
      return `<li data-file="${esc(f.id)}" class="${f.problem ? "has-problem" : ""}">
        <span class="ext">${esc(ext || "file")}</span>
        <div class="fn"><a href="/api/runs/${esc(this.id)}/files/${esc(f.id)}/download" title="Download ${esc(f.name)}">${esc(f.name)}</a>
          <div class="sub" title="${esc(f.detected_reason || "")}">${facts.join(" &middot; ")}</div>
          ${f.problem ? `<div class="file-problem" role="alert">&#9888; This file ${esc(f.problem)}. Remove it to scan.</div>` : ""}</div>
        <span class="kind-wrap"><select data-kind="${esc(f.id)}" ${locked ? "disabled" : ""} aria-label="How to read this file">
          <option value="auto" ${f.kind === "auto" ? "selected" : ""}>Auto${esc(guess)}</option>
          <option value="document" ${f.kind === "document" ? "selected" : ""} ${DRAWING_ONLY.includes(ext) ? "disabled" : ""}>Document</option>
          <option value="drawing" ${f.kind === "drawing" ? "selected" : ""} ${DOCUMENT_ONLY.includes(ext) ? "disabled" : ""}>Drawing</option>
        </select></span>
        ${locked ? "<span></span><span></span>" : `<button class="icon-btn sm" data-move="${esc(f.id)}" title="Move to Set ${other}">&#8644;</button>
        <button class="icon-btn sm" data-remove="${esc(f.id)}" title="Remove file">&times;</button>`}
      </li>`;
    }).join("") + (files.length ? "" : `<li style="display:block;text-align:center" class="muted small">No files in this set yet.</li>`);
    if (files.some((f) => f.inspecting)) this.watchInspection();
  }

  // Uploaded files are checked in the background (pages, encryption, document vs
  // drawing); refresh their rows until every check is done.
  watchInspection() {
    if (this._inspectTimer) return;
    this._inspectTimer = setTimeout(async () => {
      this._inspectTimer = null;
      if (!this.el.isConnected || this.step !== "documents") return;
      try {
        const run = await api(`/api/runs/${this.id}`);
        const fresh = new Map(run.files.map((f) => [f.id, f]));
        this.run.files = this.run.files.map((f) => fresh.get(f.id) || f);
        this.renderFiles("a"); this.renderFiles("b"); this.renderStepper();
      } catch { /* try again on the next render */ }
    }, 1200);
  }

  async uploadTo(slot, files) {
    if (!files.length) return;
    const existing = new Set(this.run.files.filter((f) => f.slot === slot).map((f) => `${f.name}|${f.size}`));
    const fresh = files.filter((f) => !existing.has(`${f.name.replace(/[^\w.-]+/g, "_")}|${f.size}`) && !existing.has(`${f.name}|${f.size}`));
    if (fresh.length < files.length) toast(`Skipped ${plural(files.length - fresh.length, "file")} already in Set ${slot.toUpperCase()}.`);
    if (!fresh.length) return;
    const ul = $(`[data-files="${slot}"]`, this.el);
    const placeholder = el(`<li class="uploading"><span class="spinner"></span>
      <div class="fn"><span class="nm">Uploading ${plural(fresh.length, "file")}&hellip;</span><div class="sub">${fmtBytes(fresh.reduce((n, f) => n + f.size, 0))}</div></div>
      <span></span><span></span><span></span><div class="bar"><div></div></div></li>`);
    ul.appendChild(placeholder);
    const fd = new FormData();
    fd.append("slot", slot);
    fresh.forEach((f) => fd.append("files", f));
    try {
      await this.inputSaver.flush().catch(() => {});
      const res = await uploadFiles(`/api/runs/${this.id}/files`, fd, (p) => { placeholder.querySelector(".bar > div").style.width = `${Math.round(p * 100)}%`; });
      this.run.files.push(...res.added);
      if (res.errors && res.errors.length) toast(res.errors.join(" "), { type: "error", timeout: 9000 });
      if (this.run.status !== "draft") { this.run.status = "draft"; this.renderHead(); }
    } catch (err) { toastError(err); }
    placeholder.remove();
    this.renderFiles(slot);
    this.renderStepper();
  }

  async patchFile(fileId, body) {
    try {
      const f = await api(`/api/runs/${this.id}/files/${fileId}`, { method: "PATCH", body });
      const i = this.run.files.findIndex((x) => x.id === fileId);
      const oldSlot = this.run.files[i].slot;
      this.run.files[i] = f;
      this.renderFiles(oldSlot);
      if (f.slot !== oldSlot) { this.renderFiles(f.slot); toast(`Moved ${f.name} to Set ${f.slot.toUpperCase()}`, { timeout: 2000 }); }
    } catch (err) { toastError(err); this.renderFiles("a"); this.renderFiles("b"); }
  }

  async removeFile(fileId) {
    const f = this.run.files.find((x) => x.id === fileId);
    if (!f || !(await confirmDialog(`Remove ${f.name} from Set ${f.slot.toUpperCase()}?`, { title: "Remove file", confirmLabel: "Remove", danger: true }))) return;
    try {
      await api(`/api/runs/${this.id}/files/${fileId}`, { method: "DELETE" });
      this.run.files = this.run.files.filter((x) => x.id !== fileId);
      this.renderFiles(f.slot);
      this.renderStepper();
    } catch (err) { toastError(err); }
  }

  // ---- step 3: scan ---------------------------------------------------
  step_scan(body) {
    const r = this.run;
    if (ACTIVE.has(r.status)) return this.scanProgressHtml(body);
    if (r.status === "complete") return this.scanSummaryHtml(body);

    const inp = this.inputs();
    const dis = this.locked() ? "disabled" : "";
    const issues = this.checklist();
    const blocked = issues.some((i) => i.level === "bad");
    body.innerHTML = `
      ${r.status === "failed" ? `<div class="banner error"><span class="grow"><strong>The last attempt failed.</strong> ${esc(r.error || "")}</span></div>` : ""}
      ${r.status === "cancelled" ? `<div class="banner warn"><span class="grow"><strong>The last attempt was cancelled.</strong> You can change anything and start it again.</span></div>` : ""}
      <section class="card">
        <div class="card-head"><div><h3>Scan settings</h3><p class="hint">Steer what the scan pays attention to. All optional.</p></div></div>
        <div class="stack">
          <label class="field"><span>Review focus</span>
            <textarea data-in="review_focus" rows="2" ${dis} placeholder="e.g. concentrate on structural concrete grades and reinforcement cover. Leave blank for a full comparison.">${esc(inp.review_focus)}</textarea></label>
          <div class="form-grid two">
            <label class="field"><span>Contract order of precedence</span>
              <input data-in="document_precedence" value="${esc(inp.document_precedence)}" ${dis} placeholder="e.g. Conditions > Specifications > Drawings > BOQ"></label>
            <label class="field"><span>RFI drafting</span>
              <select data-in="rfi_grouping" ${dis}>
                <option value="consolidated" ${inp.rfi_grouping !== "single" ? "selected" : ""}>Group related findings into one RFI (numbered points)</option>
                <option value="single" ${inp.rfi_grouping === "single" ? "selected" : ""}>One RFI per finding</option>
              </select></label>
          </div>
        </div>
      </section>
      <section class="card">
        <div class="card-head"><div><h3>Ready to scan?</h3></div></div>
        <ul class="checklist">${issues.map((i) => `<li class="${i.level}"><span class="i">${i.level === "ok" ? "&#10003;" : i.level === "warn" ? "!" : "&times;"}</span>
          <span class="grow">${i.html}</span></li>`).join("")}</ul>
        <div class="row wrap" style="margin-top:16px;gap:14px">
          <button class="btn primary lg" data-act="start" ${blocked ? "disabled" : ""}>${r.status === "draft" ? "Run conflict scan" : "Run the scan again"}</button>
          <span class="hint">or press <kbd>Ctrl</kbd> <kbd>Enter</kbd>. The scan runs on the server &mdash; you can close this tab or keep working, and you'll be notified when it finishes.</span>
        </div>
      </section>
      <div class="step-nav"><button class="btn ghost" data-step="documents">&larr; Documents</button><span></span></div>`;
    body.addEventListener("input", (e) => this.onInputField(e));
    body.addEventListener("change", (e) => this.onInputField(e));
  }

  onInputField(e) {
    const k = e.target.dataset.in;
    if (!k) return;
    this.inputs()[k] = e.target.value;
    this.inputSaver.schedule({ [k]: e.target.value });
  }

  checklist() {
    const out = [];
    const inp = this.inputs();
    for (const s of ["a", "b"]) {
      const n = this.run.files.filter((f) => f.slot === s).length;
      const text = (inp[`document_${s}_text`] || "").trim();
      const name = inp[`document_${s}_label`] || `Set ${s.toUpperCase()}`;
      if (n || text) out.push({ level: "ok", html: `<strong>${esc(name)}</strong>: ${n ? plural(n, "file") : ""}${n && text ? " + " : ""}${text ? "pasted text" : ""}` });
      else out.push({ level: "bad", html: `Set ${s.toUpperCase()} is empty &mdash; <button class="link-btn" data-step="documents">add files or paste text</button>` });
    }
    const unnamed = ["a", "b"].filter((s) => !(inp[`document_${s}_label`] || "").trim());
    if (unnamed.length) out.push({ level: "warn", html: `${unnamed.map((s) => "Set " + s.toUpperCase()).join(" and ")} ${unnamed.length > 1 ? "have" : "has"} no name &mdash; findings will say "Document ${unnamed[0].toUpperCase()}". <button class="link-btn" data-step="documents">Name ${unnamed.length > 1 ? "them" : "it"}</button>` });
    if (App.template && !App.template.loaded) out.push({ level: "warn", html: "No RFI .docx form is loaded &mdash; RFIs will be drafted on screen only." });
    else if (App.template) out.push({ level: "ok", html: `RFI form: ${esc(App.template.filename)}` });
    const meta = inp.rfi_form_meta || {};
    if (!Object.values(meta).some((v) => (v || "").trim())) out.push({ level: "warn", html: `RFI header details are blank. <button class="link-btn" data-step="details">Fill them in</button>` });
    return out;
  }

  async start() {
    if (ACTIVE.has(this.run.status) || this.run.status === "complete") return;
    if (this.checklist().some((i) => i.level === "bad")) { toast("Add files to both sets first.", { type: "error" }); return; }
    try {
      await this.inputSaver.flush();
      const r = await api(`/api/runs/${this.id}/start`, { method: "POST" });
      Object.assign(this.run, { status: r.status, progress: r.progress, queue_position: r.queue_position, inputs_editable: false, started_at: null });
      Jobs.track(this.run);
      this.render();
      this.syncTimers();
      refreshSoon();
      if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    } catch (err) { toastError(err); }
  }

  async cancel() {
    if (!(await confirmDialog("Stop this scan? A stage that has already started finishes first, then the scan stops. Inputs stay as they are so you can start it again.",
      { title: "Cancel scan", confirmLabel: "Cancel scan", danger: true }))) return;
    try {
      const r = await api(`/api/runs/${this.id}/cancel`, { method: "POST" });
      this.run.status = r.status;
      toast(r.status === "cancelled" ? "Scan cancelled" : "Stopping after the current stage...");
      this.render();
      this.syncTimers();
      Jobs.poll();
    } catch (err) { toastError(err); }
  }

  scanProgressHtml(body) {
    body.innerHTML = `<section class="card">
      <div class="progress-hero">
        <span class="spinner" style="width:28px;height:28px;border-width:3px"></span>
        <div><div class="big" data-elapsed>0:00</div><div class="hint" data-phase></div></div>
        <span class="grow"></span>
        <button class="btn danger" data-act="cancel">Cancel scan</button>
      </div>
      <div class="progress-track"><div class="progress-fill" data-fill></div></div>
      <div class="hint mono" data-msg style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div>
      <ul class="stages" data-stages style="margin-top:12px"></ul>
      <details style="margin-top:12px" ${local.get("log.open", false) ? "open" : ""} data-logbox><summary>Live log</summary><pre class="log" data-log></pre></details>
      <p class="hint" style="margin-top:14px">You can close this tab or keep working &mdash; the scan keeps running on the server and you'll be notified when it finishes.</p>
    </section>`;
    $("[data-logbox]", body).addEventListener("toggle", (e) => local.set("log.open", e.target.open));
    this.renderProgress();
    this.tickElapsed();
  }

  renderProgress() {
    const r = this.run;
    const p = r.progress || {};
    const stages = p.stages || [];
    const done = stages.filter((s) => s.status === "done" || s.status === "skipped").length;
    const phase = $("[data-phase]", this.el);
    if (!phase) return;
    const queued = r.status === "queued";
    phase.textContent = queued ? `Queued${r.queue_position ? ` - #${r.queue_position} in line` : ""}, waiting for a free scan worker` : `elapsed - stage ${Math.min(done + 1, stages.length || 5)} of ${stages.length || 5}`;
    const fill = $("[data-fill]", this.el);
    fill.classList.toggle("indet", queued);
    fill.style.width = `${Math.max(4, Math.round((done / (stages.length || 5)) * 100))}%`;
    $("[data-msg]", this.el).textContent = p.message || (queued ? "" : "Starting...");
    $("[data-stages]", this.el).innerHTML = stages.map((s) => `<li class="${esc(s.status)}">
      <span class="ic">${s.status === "running" ? '<span class="spinner"></span>' : s.status === "done" ? "&#10003;" : s.status === "failed" ? "&times;" : s.status === "skipped" ? "&ndash;" : ""}</span>
      <span class="lbl grow">${esc(s.label)}</span><span class="muted small">${s.status === "skipped" ? "skipped - no findings" : s.status === "running" ? "in progress" : ""}</span></li>`).join("");
    const log = $("[data-log]", this.el);
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
    log.innerHTML = (p.log || []).map((l) => /error|failed|traceback/i.test(l) ? `<span class="err">${esc(l)}</span>` : esc(l)).join("\n");
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  scanSummaryHtml(body) {
    const res = this.results();
    const inp = this.inputs();
    const c = countsOf(this.run);
    const conf = (res.confidence_level || "medium").toLowerCase();
    const setStat = (doc, cls) => {
      if (!doc) return "";
      const files = (doc.files || []).map((f) => {
        const bits = [f.kind];
        if (f.sheets) bits.push(`${f.sheets} sheet(s)`);
        if (f.pages) bits.push(`${f.pages} page(s)`);
        if (f.ocr_pages) bits.push(`${f.ocr_pages} OCR`);
        if (f.requirements != null) bits.push(`${f.requirements} items`);
        return `<li>${esc(f.name)} <span class="muted">(${esc(bits.join(", "))})</span></li>`;
      }).join("");
      return `<div class="set-stat ${cls}"><div class="v">${esc(doc.label)}</div>
        <div class="hint">${plural(doc.file_count || 1, "file")} &middot; ${doc.requirement_count} items &middot; ${doc.quantity_count} quantities</div>
        ${files ? `<ul>${files}</ul>` : ""}</div>`;
    };
    const log = (this.run.progress || {}).log || [];
    body.innerHTML = `
      <section class="card">
        <div class="card-head"><div><h3>Scan complete</h3>
          <p class="hint">Finished ${timeEl(this.run.finished_at)}${res.duration_seconds ? ` in ${fmtDuration(res.duration_seconds)}` : ""}.</p></div>
          <span class="row"><span class="muted small">Confidence</span><span class="conf ${esc(conf)}">${esc(conf.toUpperCase())}</span></span></div>
        <p style="margin:0">${esc(res.scan_summary)}</p>
        <div class="summary-grid">${setStat(res.document_a, "")}${setStat(res.document_b, "b")}</div>
        ${(res.warnings || []).map((w) => `<div class="banner warn" style="margin:12px 0 0"><span class="grow">${esc(w)}</span></div>`).join("")}
        <div class="downloads">
          <button class="btn primary" data-step="findings">Review ${plural(c.findings, "finding")} &rarr;</button>
          ${c.rfis ? `<button class="btn" data-step="rfis">Edit ${plural(c.rfis, "RFI")}</button>` : ""}
          ${res.register_file ? `<a class="btn" href="/api/runs/${esc(this.id)}/exports/${encodeURIComponent(res.register_file)}">Register (.xlsx)</a>` : ""}
          ${c.findings ? `<button class="btn" data-act="register" title="Re-write the register with your triage, severity changes and RFI edits; dismissed findings are left out">Rebuild register with my edits</button>` : ""}
        </div>
      </section>
      <section class="card">
        <div class="card-head"><div><h3>Settings used</h3></div>
          <button class="btn sm" data-act="clone">Edit &amp; re-run</button></div>
        <dl class="kv">
          <dt>Set A</dt><dd>${esc(this.labelA())} &middot; ${plural(this.run.files.filter((f) => f.slot === "a").length, "file")}</dd>
          <dt>Set B</dt><dd>${esc(this.labelB())} &middot; ${plural(this.run.files.filter((f) => f.slot === "b").length, "file")}</dd>
          <dt>Review focus</dt><dd>${esc(inp.review_focus) || '<span class="muted">Full comparison</span>'}</dd>
          <dt>Precedence</dt><dd>${esc(inp.document_precedence) || '<span class="muted">Not stated</span>'}</dd>
          <dt>RFI drafting</dt><dd>${inp.rfi_grouping === "single" ? "One RFI per finding" : "Related findings grouped"}</dd>
        </dl>
        ${(res.steps_executed || []).length ? `<details style="margin-top:14px"><summary>Pipeline steps (${res.steps_executed.length})</summary>
          <ul class="small" style="margin:8px 0 0">${res.steps_executed.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></details>` : ""}
        ${log.length ? `<details style="margin-top:10px"><summary>Scan log</summary><pre class="log">${log.map(esc).join("\n")}</pre></details>` : ""}
      </section>`;
  }

  async rebuildRegister() {
    try {
      await this.flushAll();
      const r = await api(`/api/runs/${this.id}/register`, { method: "POST" });
      this.results().register_file = r.register_file;
      toast("Register rebuilt with your edits", {
        type: "success", action: "Download",
        onAction: () => { location.href = `/api/runs/${this.id}/exports/${encodeURIComponent(r.register_file)}`; },
      });
      if (this.step === "scan") this.renderStep();
    } catch (err) { toastError(err); }
  }

  // ---- step 4: findings ----------------------------------------------
  rfiByFinding() {
    const map = {};
    for (const rfi of this.results().rfi_items || []) {
      const ids = rfi.discrepancy_ids || String(rfi.discrepancy_id || "").split(/,\s*/).filter(Boolean);
      for (const id of ids) (map[id] = map[id] || []).push(rfi.rfi_no);
    }
    return map;
  }

  step_findings(body) {
    const discs = this.results().discrepancies || [];
    if (!discs.length) {
      body.innerHTML = `<div class="card empty"><div class="big">&#10003;</div><h3>No discrepancies found</h3>
        <p>Nothing conflicted between or within the two document sets.</p>
        <button class="btn" data-act="clone">Edit &amp; re-run with different settings</button></div>`;
      return;
    }
    const classes = this.results().issue_classes || {};
    const usedClasses = [...new Set(discs.map((d) => d.issue_class).filter(Boolean))];
    const f = this.filters;
    if (this.expanded.size === 0 && discs.length <= 6 && !this._expandedOnce) { discs.forEach((d) => this.expanded.add(d.id)); }
    this._expandedOnce = true;
    body.innerHTML = `
      <div class="filters">
        <div class="search-box" style="max-width:280px"><input type="search" data-fq value="${esc(f.q)}" placeholder="Search findings (/)"></div>
        <span data-sevchips></span>
        <select data-fstatus aria-label="Status filter">
          ${[["all", "Any status"], ["open", "Open"], ["accepted", "Accepted"], ["dismissed", "Dismissed"]].map(([v, l]) => `<option value="${v}" ${f.status === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        <select data-fscope aria-label="Scope filter">
          <option value="all">Any scope</option>
          <option value="between" ${f.scope === "between" ? "selected" : ""}>Between the sets</option>
          <option value="within_a" ${f.scope === "within_a" ? "selected" : ""}>Within ${esc(this.labelA())}</option>
          <option value="within_b" ${f.scope === "within_b" ? "selected" : ""}>Within ${esc(this.labelB())}</option>
        </select>
        ${usedClasses.length > 1 ? `<select data-fcls aria-label="Issue type filter"><option value="all">Any issue type</option>
          ${usedClasses.map((k) => `<option value="${esc(k)}" ${f.cls === k ? "selected" : ""}>${esc(classes[k] || k.replace(/_/g, " "))}</option>`).join("")}</select>` : ""}
        <select data-fsort aria-label="Sort"><option value="found" ${f.sort === "found" ? "selected" : ""}>As found</option><option value="severity" ${f.sort === "severity" ? "selected" : ""}>Severity first</option></select>
        <span class="grow"></span>
        <span class="muted small" data-fcount></span>
        <button class="btn sm ghost" data-act="expand-all">Expand all</button>
        <button class="btn sm ghost" data-act="collapse-all">Collapse all</button>
        ${countsOf(this.run).findings ? `<button class="btn sm" data-act="register" title="Re-write the register with your triage and edits">Rebuild register</button>` : ""}
      </div>
      <div data-flist></div>
      <p class="hint" style="margin-top:12px">Tip: <kbd>J</kbd>/<kbd>K</kbd> to move, <kbd>A</kbd> accept, <kbd>D</kbd> dismiss, <kbd>U</kbd> reopen. Triage and notes save automatically and go into the rebuilt register.</p>`;
    const q = $("[data-fq]", body);
    q.addEventListener("input", debounce(() => { f.q = q.value; this.renderFindingList(); }, 150));
    const bindSel = (sel, key) => { const s = $(sel, body); if (s) s.addEventListener("change", () => { f[key] = s.value; this.renderFindingList(); }); };
    bindSel("[data-fstatus]", "status"); bindSel("[data-fscope]", "scope"); bindSel("[data-fcls]", "cls"); bindSel("[data-fsort]", "sort");
    const list = $("[data-flist]", body);
    list.addEventListener("input", (e) => {
      const t = e.target.closest("[data-note]");
      if (!t) return;
      autoGrow(t);
      const id = t.dataset.note;
      (this.review()[id] = this.review()[id] || {}).note = t.value;
      this.noteSaver(id).schedule({ note: t.value });
    });
    list.addEventListener("change", (e) => {
      const s = e.target.closest("[data-sev]");
      if (s) this.setFinding(s.dataset.sev, { severity: s.value || null });
    });
    this.renderFindingList();
  }

  renderSevChips(visibleAll) {
    const wrap = $("[data-sevchips]", this.el);
    if (!wrap) return;
    const counts = { critical: 0, major: 0, minor: 0 };
    for (const d of visibleAll) counts[this.sevOf(d)] = (counts[this.sevOf(d)] || 0) + 1;
    wrap.innerHTML = SEVERITIES.map((s) => `<button class="chip ${s} ${this.filters.sev.has(s) ? "on" : ""}" data-chip="${s}" title="Show/hide ${s}">${s} <strong>${counts[s]}</strong></button>`).join(" ");
  }

  sevOf(d) { return ((this.review()[d.id] || {}).severity || d.severity || "minor").toLowerCase(); }
  statusOf(d) { return (this.review()[d.id] || {}).status || "open"; }

  filteredFindings() {
    const f = this.filters;
    const terms = f.q.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const all = this.results().discrepancies || [];
    const base = all.filter((d) => {
      if (f.status !== "all" && this.statusOf(d) !== f.status) return false;
      if (f.scope !== "all" && (d.scope || "between") !== f.scope) return false;
      if (f.cls !== "all" && d.issue_class !== f.cls) return false;
      if (terms.length) {
        const rv = this.review()[d.id] || {};
        const hay = [d.id, d.topic, d.description, d.location, d.impact, d.suggested_question, d.document_a_says, d.document_b_says, rv.note,
          ...(d.evidence || []).map((e) => `${e.says || ""} ${e.file || ""} ${e.reference || ""}`)].join(" ").toLowerCase();
        if (!terms.every((t) => hay.includes(t))) return false;
      }
      return true;
    });
    let list = base.filter((d) => f.sev.has(this.sevOf(d)));
    if (f.sort === "severity") list = [...list].sort((a, b) => SEVERITIES.indexOf(this.sevOf(a)) - SEVERITIES.indexOf(this.sevOf(b)));
    return { list, base, total: all.length };
  }

  renderFindingList() {
    const wrap = $("[data-flist]", this.el);
    if (!wrap) return;
    const { list, base, total } = this.filteredFindings();
    this.renderSevChips(base);
    $("[data-fcount]", this.el).textContent = list.length === total ? plural(total, "finding") : `Showing ${list.length} of ${total}`;
    const rfiMap = this.rfiByFinding();
    wrap.innerHTML = list.length ? list.map((d) => this.findingHtml(d, rfiMap)).join("")
      : `<div class="card empty"><h3>No findings match these filters</h3><button class="btn" data-act="reset-filters">Clear filters</button></div>`;
    requestAnimationFrame(() => $$("textarea", wrap).forEach(autoGrow));
  }

  findingHtml(d, rfiMap) {
    const labelA = this.labelA(), labelB = this.labelB();
    const classes = this.results().issue_classes || {};
    const rv = this.review()[d.id] || {};
    const status = rv.status || "open";
    const sev = this.sevOf(d);
    const open = this.expanded.has(d.id);
    const rfis = rfiMap[d.id] || [];
    const evidence = d.evidence || [];
    const quote = (e) => {
      const says = (e.says || "").trim();
      if (!says || /^not (addressed|involved)/i.test(says)) return `<div class="says muted">Not addressed.</div>`;
      return e.verbatim === false ? `<div class="says">${esc(says)} <span class="muted">(paraphrased)</span></div>` : `<div class="says">&ldquo;${esc(says)}&rdquo;</div>`;
    };
    const ref = (e) => {
      const where = [e.file, e.reference].filter(Boolean).join(" · ");
      return (where ? `<div class="ref">${esc(where)}</div>` : "") + (e.low_confidence ? `<div class="ref verify-note">verify - OCR / partly legible</div>` : "");
    };
    let cols;
    if (evidence.length && (d.scope === "within_a" || d.scope === "within_b")) {
      const label = d.scope === "within_a" ? labelA : labelB;
      cols = evidence.slice(0, 3).map((e, i) => ({ cls: d.scope === "within_b" ? "side side-b" : "side", title: `${esc(label)} &mdash; ${esc(e.file || "source " + (i + 1))}`, body: quote(e) + ref(e) }));
    } else if (evidence.length) {
      const col = (side, label, cls) => {
        const items = evidence.filter((e) => e.side === side);
        return { cls, title: `${esc(label)} states`, body: items.length ? items.map((e) => quote(e) + ref(e)).join(`<hr class="ev-sep">`) : `<div class="says muted">Not addressed in ${esc(label)}.</div>` };
      };
      cols = [col("a", labelA, "side"), col("b", labelB, "side side-b")];
    } else {
      cols = [
        { cls: "side", title: `${esc(labelA)} states`, body: `<div class="says">${esc(d.document_a_says) || "&ndash;"}</div>${d.document_a_reference ? `<div class="ref">${esc(d.document_a_reference)}</div>` : ""}` },
        { cls: "side side-b", title: `${esc(labelB)} states`, body: `<div class="says">${esc(d.document_b_says) || "&ndash;"}</div>${d.document_b_reference ? `<div class="ref">${esc(d.document_b_reference)}</div>` : ""}` },
      ];
    }
    const scope = d.scope === "within_a" ? `within ${labelA}` : d.scope === "within_b" ? `within ${labelB}` : "";
    return `<article class="finding sev-${esc(sev)} st-${esc(status)} ${open ? "open" : ""} ${this.focusId === d.id ? "focus" : ""}" data-fid="${esc(d.id)}">
      <div class="f-head" data-toggle="${esc(d.id)}">
        <span class="chev">&#9654;</span><span class="f-id">${esc(d.id)}</span><span class="f-topic">${esc(d.topic)}</span>
        <span class="f-tags">
          ${d.issue_class ? `<span class="badge class">${esc(classes[d.issue_class] || d.issue_class.replace(/_/g, " "))}</span>` : ""}
          ${scope ? `<span class="badge scope">${esc(scope)}</span>` : ""}
          ${d.low_confidence ? `<span class="badge verify" title="Rests on OCR text or partly legible drawing values">verify</span>` : ""}
          ${status !== "open" ? `<span class="badge ${status === "accepted" ? "ok" : "outline"}">${esc(status)}</span>` : ""}
          ${rv.note ? `<span class="badge outline" title="${esc(rv.note)}">note</span>` : ""}
          ${rfis.map((no) => `<button class="badge outline" data-goto-rfi="${esc(no)}" title="Open ${esc(no)}">${esc(no)}</button>`).join("")}
          <span class="badge sev-${esc(sev)}" ${rv.severity ? `title="Changed from ${esc(d.severity)}"` : ""}>${esc(sev)}${rv.severity ? "*" : ""}</span>
        </span>
      </div>
      <div class="f-body">
        ${d.location ? `<div class="loc-line">Location: ${esc(d.location)}</div>` : ""}
        <p>${esc(d.description)}</p>
        <div class="side-by-side" style="grid-template-columns:repeat(${cols.length},1fr)">${cols.map((c) => `<div class="${c.cls}"><div class="side-label">${c.title}</div>${c.body}</div>`).join("")}</div>
        ${d.impact ? `<p><strong>Impact:</strong> ${esc(d.impact)}</p>` : ""}
        ${d.suggested_question ? `<p><strong>Clarification sought:</strong> ${esc(d.suggested_question)}</p>` : ""}
        <div class="f-triage">
          <div class="segmented triage" data-triage="${esc(d.id)}">
            ${[["open", "Open"], ["accepted", "Accept"], ["dismissed", "Dismiss"]].map(([v, l]) => `<button data-v="${v}" class="${status === v ? "on" : ""}">${l}</button>`).join("")}
          </div>
          <select data-sev="${esc(d.id)}" aria-label="Severity">
            <option value="">Severity: ${esc(d.severity || "minor")} (as found)</option>
            ${SEVERITIES.map((s) => `<option value="${s}" ${rv.severity === s ? "selected" : ""}>Change to ${s}</option>`).join("")}
          </select>
          ${rfis.length ? `<button class="btn sm" data-goto-rfi="${esc(rfis[0])}">Open ${esc(rfis[0])} &rarr;</button>`
            : `<button class="btn sm" data-new-rfi="${esc(d.id)}">Draft an RFI for this</button>`}
          <textarea data-note="${esc(d.id)}" rows="1" placeholder="Reviewer note (saved automatically)">${esc(rv.note)}</textarea>
          ${rv.by ? `<div class="f-review-meta">Last reviewed by ${esc(rv.by)} ${timeEl(rv.at)}</div>` : ""}
        </div>
      </div>
    </article>`;
  }

  noteSaver(id) {
    if (!this.noteSavers.has(id)) {
      this.noteSavers.set(id, new Saver((patch) => api(`/api/runs/${this.id}/findings/${encodeURIComponent(id)}`, { method: "PATCH", body: patch }),
        { delay: 900, onState: (s, err) => { const n = $("[data-save]", this.el); if (n) n.innerHTML = saveStateHtml(s, err); } }));
    }
    return this.noteSavers.get(id);
  }

  async setFinding(id, patch) {
    const rv = this.review();
    const before = { ...(rv[id] || {}) };
    rv[id] = { ...before, ...patch };
    if (patch.severity === null) delete rv[id].severity;
    this.renderFindingList();
    this.renderStepper();
    try {
      const r = await api(`/api/runs/${this.id}/findings/${encodeURIComponent(id)}`, { method: "PATCH", body: patch });
      rv[id] = { ...rv[id], ...r.finding };
      refreshSoon();
    } catch (err) {
      rv[id] = before;
      this.renderFindingList();
      this.renderStepper();
      toastError(err);
    }
  }

  toggleFinding(id, force) {
    const open = force ?? !this.expanded.has(id);
    if (open) this.expanded.add(id); else this.expanded.delete(id);
    const card = $(`[data-fid="${CSS.escape(id)}"]`, this.el);
    if (card) { card.classList.toggle("open", open); if (open) $$("textarea", card).forEach(autoGrow); }
  }

  focusFinding(delta) {
    const { list } = this.filteredFindings();
    if (!list.length) return;
    let i = list.findIndex((d) => d.id === this.focusId);
    i = i < 0 ? 0 : Math.max(0, Math.min(list.length - 1, i + delta));
    this.focusId = list[i].id;
    $$(".finding.focus", this.el).forEach((n) => n.classList.remove("focus"));
    const card = $(`[data-fid="${CSS.escape(this.focusId)}"]`, this.el);
    if (card) { card.classList.add("focus"); this.toggleFinding(this.focusId, true); card.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  }

  async newRfiFor(discId) {
    try {
      const rfi = await api(`/api/runs/${this.id}/rfis`, { method: "POST", body: { discrepancy_ids: [discId] } });
      this.results().rfi_items.push(rfi);
      this.selectedRfi = rfi.rfi_no;
      toast(`${rfi.rfi_no} created from ${discId}`, { type: "success" });
      this.renderStepper();
      this.goto("rfis");
      refreshSoon();
    } catch (err) { toastError(err); }
  }

  // ---- step 5: RFIs ---------------------------------------------------
  step_rfis(body) {
    const items = this.results().rfi_items || [];
    if (!items.length) {
      body.innerHTML = `<div class="card empty"><div class="big">&#9993;</div><h3>No RFIs</h3>
        <p>${(this.results().discrepancies || []).length ? "No RFIs were drafted for this scan." : "There were no findings to raise RFIs for."} You can still write one by hand.</p>
        <button class="btn primary" data-act="add-rfi">+ Add an RFI</button></div>`;
      return;
    }
    if (!items.some((r) => r.rfi_no === this.selectedRfi)) this.selectedRfi = items[0].rfi_no;
    const tpl = App.template && App.template.loaded;
    body.innerHTML = `
      ${!tpl ? `<div class="banner warn"><span class="grow">No RFI .docx form is loaded, so Word documents can't be generated. Drop the form into templates_rfi/ and refresh.</span></div>` : ""}
      <div class="rfi-layout">
        <aside class="rfi-list">
          <div class="rfi-list-head">
            <div class="search-box" style="max-width:none"><input type="search" data-rq value="${esc(this.rfiQuery)}" placeholder="Search RFIs (/)"></div>
            <div class="row wrap">
              <button class="btn sm" data-act="add-rfi">+ Add</button>
              ${tpl ? `<button class="btn sm" data-act="docx-all" title="Regenerate every RFI's Word document from the current text">Rebuild all .docx</button>` : ""}
              <a class="btn sm" href="/api/runs/${esc(this.id)}/bundle" data-act="bundle">.zip</a>
            </div>
          </div>
          <div class="rfi-list-items" data-rlist></div>
        </aside>
        <div data-editor></div>
      </div>`;
    const q = $("[data-rq]", body);
    q.addEventListener("input", () => { this.rfiQuery = q.value; this.renderRfiList(); });
    this.renderRfiList();
    this.renderRfiEditor();
  }

  rfiMatches(rfi) {
    const terms = this.rfiQuery.toLowerCase().trim().split(/\s+/).filter(Boolean);
    if (!terms.length) return true;
    const hay = [rfi.rfi_no, rfi.subject, rfi.area, rfi.discipline, rfi.discrepancy_id, rfi.question, rfi.discrepancy_summary].join(" ").toLowerCase();
    return terms.every((t) => hay.includes(t));
  }

  renderRfiList() {
    const wrap = $("[data-rlist]", this.el);
    if (!wrap) return;
    const items = (this.results().rfi_items || []).filter((r) => this.rfiMatches(r));
    wrap.innerHTML = items.map((r) => `
      <div class="rfi-li ${r.rfi_no === this.selectedRfi ? "on" : ""}" data-rfi="${esc(r.rfi_no)}" tabindex="0">
        <div class="top"><span class="mono">${esc(r.rfi_no)}</span>
          <span class="badge rfi-${esc(r.status || "draft")}">${esc((RFI_STATUS.find(([v]) => v === (r.status || "draft")) || [0, r.status])[1])}</span>
          <span class="grow"></span>
          ${r.edited_at ? `<span class="edited" title="Edited by ${esc(r.edited_by)}">&#9998; edited</span>` : ""}
          ${r.docx_stale && r.docx_file ? `<span class="edited" title="Word document is older than the text">&#8635;</span>` : ""}
        </div>
        <div class="s">${esc(r.subject) || '<span class="muted">No subject</span>'}</div>
      </div>`).join("") || `<div class="empty small">No RFIs match.</div>`;
  }

  rfi() { return (this.results().rfi_items || []).find((r) => r.rfi_no === this.selectedRfi); }

  rfiSaver(no) {
    if (!this.rfiSavers.has(no)) {
      const indicator = (s, err) => { const n = $("[data-rfi-save]", this.el); if (n && this.selectedRfi === no) n.innerHTML = saveStateHtml(s, err); };
      this.rfiSavers.set(no, new Saver((patch) => {
        const cur = (this.results().rfi_items || []).find((r) => r.rfi_no === no) || {};
        return api(`/api/runs/${this.id}/rfis/${encodeURIComponent(no)}`, { method: "PUT", body: { ...patch, base_rev: cur.rev || 0 } })
          .catch((err) => this.onRfiConflict(no, err, patch)).then((server) => {
        const local = (this.results().rfi_items || []).find((r) => r.rfi_no === no);
        if (local && server) for (const k of ["edited_by", "edited_at", "docx_stale", "original", "description", "rev"]) local[k] = server[k];
        this.renderRfiList();
        if (this.selectedRfi === no) this.renderRfiActions();
        return server;
      });
      }, { delay: 800, onState: indicator }));
    }
    return this.rfiSavers.get(no);
  }

  renderRfiEditor() {
    const old = $("[data-editor]", this.el);
    const rfi = this.rfi();
    if (!old || !rfi) return;
    const wrap = old.cloneNode(false);
    old.replaceWith(wrap);
    const val = (k) => Array.isArray(rfi[k]) ? rfi[k].join("\n") : (rfi[k] ?? "");
    const ids = rfi.discrepancy_ids || String(rfi.discrepancy_id || "").split(/,\s*/).filter(Boolean);
    const disc = (rfi.discipline || "").toLowerCase();
    const discOpts = DISCIPLINES.some(([v]) => v === disc) || !disc ? DISCIPLINES : [[disc, disc[0].toUpperCase() + disc.slice(1)], ...DISCIPLINES];
    wrap.innerHTML = `<section class="card rfi-editor">
      <div class="card-head">
        <div><div class="muted small mono">${esc(rfi.rfi_no)}${rfi.manual ? " &middot; added by hand" : ""}${rfi.request_type ? ` &middot; ${esc(String(rfi.request_type).replace(/_/g, " "))}` : ""}</div>
          <h3 data-subj-h>${esc(rfi.subject) || "Untitled RFI"}</h3>
          <div class="hint" data-edited>${rfi.edited_at ? `Edited by ${esc(rfi.edited_by)} ${timeEl(rfi.edited_at)}` : "AI draft - not edited yet"}</div></div>
        <span data-rfi-save></span>
      </div>
      ${ids.length ? `<div class="linked"><span class="muted">From findings:</span>${ids.map((id) => `<button class="badge outline" data-goto-finding="${esc(id)}">${esc(id)}</button>`).join("")}
        ${rfi.low_confidence ? `<span class="badge verify">verify OCR / partly legible values</span>` : ""}</div>` : ""}
      <div class="meta-row">
        <label class="field"><span>Status</span><select data-rf="status">${RFI_STATUS.map(([v, l]) => `<option value="${v}" ${(rfi.status || "draft") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label class="field"><span>Discipline</span><select data-rf="discipline">${discOpts.map(([v, l]) => `<option value="${esc(v)}" ${v === disc ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
        <label class="field"><span>Priority</span><select data-rf="priority">${["high", "medium", "low"].map((p) => `<option value="${p}" ${rfi.priority === p ? "selected" : ""}>${p[0].toUpperCase() + p.slice(1)}</option>`).join("")}</select></label>
        <label class="field"><span>Response required by</span><input data-rf="response_required_by" value="${esc(rfi.response_required_by)}"></label>
      </div>
      <div class="fields">${RFI_FIELDS.map(([k, l, rows]) => `<label class="field"><span>${l}</span>
        <textarea data-rf="${k}" rows="${rows}">${esc(val(k))}</textarea></label>`).join("")}</div>
      <div data-rfi-actions></div>
    </section>`;
    wrap.addEventListener("input", (e) => this.onRfiField(e));
    wrap.addEventListener("change", (e) => { if (e.target.tagName === "SELECT") this.onRfiField(e); });
    this.renderRfiActions();
    requestAnimationFrame(() => $$("textarea", wrap).forEach(autoGrow));
  }

  onRfiField(e) {
    const k = e.target.dataset.rf;
    const rfi = this.rfi();
    if (!k || !rfi) return;
    if (e.target.tagName === "TEXTAREA") autoGrow(e.target);
    const v = e.target.value;
    rfi[k] = k === "attachments" ? v.split("\n").map((s) => s.trim()).filter(Boolean) : v;
    rfi.docx_stale = true;
    if (k === "subject") { $("[data-subj-h]", this.el).textContent = v || "Untitled RFI"; }
    if (k === "subject" || k === "status") this.renderRfiList();
    this.rfiSaver(rfi.rfi_no).schedule({ [k]: v });
  }

  renderRfiActions() {
    const wrap = $("[data-rfi-actions]", this.el);
    const rfi = this.rfi();
    if (!wrap || !rfi) return;
    const tpl = App.template && App.template.loaded;
    const dl = (f, label) => `<a class="btn" href="/api/runs/${esc(this.id)}/exports/${encodeURIComponent(f)}">${label}</a>`;
    wrap.innerHTML = `
      ${rfi.docx_file && rfi.docx_stale ? `<div class="banner warn" style="margin:16px 0 0"><span class="grow">The text has changed since the Word document was generated.</span>
        ${tpl ? `<button class="btn sm primary" data-act="docx">Regenerate now</button>` : ""}</div>` : ""}
      <div class="rfi-actions">
        ${tpl ? `<button class="btn ${!rfi.docx_file || rfi.docx_stale ? "primary" : ""}" data-act="docx">${rfi.docx_file ? "Regenerate" : "Generate"} Word document</button>` : ""}
        ${rfi.docx_file ? dl(rfi.docx_file, "Download .docx") : ""}
        ${rfi.original_docx_file ? dl(rfi.original_docx_file, "AI draft .docx") : ""}
        <span class="grow"></span>
        ${rfi.original ? `<button class="btn ghost" data-act="revert-rfi" title="Discard your edits and restore the AI-drafted text">Revert to AI draft</button>` : ""}
        <button class="btn danger" data-act="delete-rfi">Delete</button>
      </div>`;
    const ed = $("[data-edited]", this.el);
    if (ed) ed.innerHTML = rfi.edited_at ? `Edited by ${esc(rfi.edited_by)} ${timeEl(rfi.edited_at)}` : "AI draft - not edited yet";
  }

  selectRfi(no) {
    if (this.selectedRfi === no && this.step === "rfis") return;
    this.selectedRfi = no;
    if (this.step !== "rfis") { this.goto("rfis"); return; }
    this.renderRfiList();
    this.renderRfiEditor();
    const item = $(`[data-rfi="${CSS.escape(no)}"]`, this.el);
    if (item) item.scrollIntoView({ block: "nearest" });
  }

  moveRfi(delta) {
    const items = (this.results().rfi_items || []).filter((r) => this.rfiMatches(r));
    const i = items.findIndex((r) => r.rfi_no === this.selectedRfi);
    const next = items[Math.max(0, Math.min(items.length - 1, i + delta))];
    if (next) this.selectRfi(next.rfi_no);
  }

  async rfiAction(act) {
    const rfi = this.rfi();
    try {
      if (act === "add-rfi") {
        const created = await api(`/api/runs/${this.id}/rfis`, { method: "POST", body: {} });
        (this.results().rfi_items = this.results().rfi_items || []).push(created);
        this.selectedRfi = created.rfi_no;
        this.renderStepper();
        this.renderStep();
        const s = $('[data-rf="subject"]', this.el);
        if (s) { s.focus(); s.select(); }
        refreshSoon();
        return;
      }
      if (act === "docx-all") {
        await this.flushAll();
        const r = await api(`/api/runs/${this.id}/docx-all`, { method: "POST" });
        this.results().rfi_items = r.rfi_items;
        this.renderRfiList();
        this.renderRfiEditor();
        if (r.errors.length) toast(`Some documents failed: ${r.errors.join("; ")}`, { type: "error", timeout: 9000 });
        else toast(`${plural(r.rfi_items.length, "Word document")} regenerated`, { type: "success" });
        return;
      }
      if (!rfi) return;
      if (act === "docx") {
        await this.rfiSaver(rfi.rfi_no).flush();
        const r = await api(`/api/runs/${this.id}/rfis/${encodeURIComponent(rfi.rfi_no)}/docx`, { method: "POST" });
        Object.assign(rfi, { docx_file: r.docx_file, docx_stale: false, original_docx_file: r.original_docx_file });
        this.renderRfiActions();
        this.renderRfiList();
        toast(`${rfi.rfi_no} Word document ready`, { type: "success", action: "Download", onAction: () => { location.href = `/api/runs/${this.id}/exports/${encodeURIComponent(r.docx_file)}`; } });
      }
      if (act === "revert-rfi") {
        if (!(await confirmDialog(`Discard your edits to ${rfi.rfi_no} and restore the AI-drafted text?`, { title: "Revert RFI", confirmLabel: "Revert" }))) return;
        await this.rfiSaver(rfi.rfi_no).flush();
        const r = await api(`/api/runs/${this.id}/rfis/${encodeURIComponent(rfi.rfi_no)}/revert`, { method: "POST" });
        const items = this.results().rfi_items;
        items[items.indexOf(rfi)] = r;
        this.renderRfiList();
        this.renderRfiEditor();
        toast(`${rfi.rfi_no} reverted to the AI draft`);
      }
      if (act === "delete-rfi") {
        if (!(await confirmDialog(`Delete ${rfi.rfi_no} "${rfi.subject}"? The findings it covered stay in the register.`, { title: "Delete RFI", confirmLabel: "Delete RFI", danger: true }))) return;
        const saver = this.rfiSavers.get(rfi.rfi_no);
        if (saver) { saver.pending = null; saver.dispose(); this.rfiSavers.delete(rfi.rfi_no); }
        await api(`/api/runs/${this.id}/rfis/${encodeURIComponent(rfi.rfi_no)}`, { method: "DELETE" });
        const items = this.results().rfi_items;
        const i = items.indexOf(rfi);
        items.splice(i, 1);
        this.selectedRfi = (items[i] || items[i - 1] || {}).rfi_no || null;
        this.renderStepper();
        this.renderStep();
        toast(`Deleted ${rfi.rfi_no}`);
        refreshSoon();
      }
    } catch (err) { toastError(err); }
  }

  // ---- events ---------------------------------------------------------
  async onClick(e) {
    const t = e.target;
    if (t.closest("[data-home]")) { e.preventDefault(); WS.activate("home"); return; }
    if (t.closest("[data-project-link]")) { e.preventDefault(); WS.open("project", this.run.project_id); return; }
    const step = t.closest("[data-step]");
    if (step && !step.disabled) { this.goto(step.dataset.step); return; }
    const chip = t.closest("[data-chip]");
    if (chip) {
      const s = chip.dataset.chip;
      const set = this.filters.sev;
      if (e.altKey || e.shiftKey) { set.clear(); set.add(s); } else if (set.has(s)) set.delete(s); else set.add(s);
      this.renderFindingList();
      return;
    }
    if (this.readOnly() && t.closest("[data-triage] button, [data-new-rfi], [data-move], [data-remove], [data-act=start], [data-act=clone], [data-act=register], [data-act=add-rfi], [data-act=docx-all], [data-act=docx], [data-act=revert-rfi], [data-act=delete-rfi], [data-act=clear-meta]")) {
      toast("You have view-only access to this project.", { timeout: 2500 });
      return;
    }
    const tri = t.closest("[data-triage] button");
    if (tri) { this.setFinding(tri.closest("[data-triage]").dataset.triage, { status: tri.dataset.v }); return; }
    const gotoRfi = t.closest("[data-goto-rfi]");
    if (gotoRfi) { e.stopPropagation(); this.selectRfi(gotoRfi.dataset.gotoRfi); return; }
    const gotoF = t.closest("[data-goto-finding]");
    if (gotoF) {
      const id = gotoF.dataset.gotoFinding;
      this.filters = { ...this.filters, q: "", status: "all", scope: "all", cls: "all", sev: new Set(SEVERITIES) };
      this.expanded.add(id);
      this.focusId = id;
      this.goto("findings");
      requestAnimationFrame(() => { const c = $(`[data-fid="${CSS.escape(id)}"]`, this.el); if (c) c.scrollIntoView({ block: "center" }); });
      return;
    }
    const newRfi = t.closest("[data-new-rfi]");
    if (newRfi) { this.newRfiFor(newRfi.dataset.newRfi); return; }
    const toggle = t.closest("[data-toggle]");
    if (toggle && !t.closest("button, select, textarea, a")) { this.focusId = toggle.dataset.toggle; this.toggleFinding(toggle.dataset.toggle); return; }
    const rfiItem = t.closest("[data-rfi]");
    if (rfiItem) { this.selectRfi(rfiItem.dataset.rfi); return; }
    const mv = t.closest("[data-move]");
    if (mv) { const f = this.run.files.find((x) => x.id === mv.dataset.move); if (f) this.patchFile(f.id, { slot: f.slot === "a" ? "b" : "a" }); return; }
    const rm = t.closest("[data-remove]");
    if (rm) { this.removeFile(rm.dataset.remove); return; }

    const act = t.closest("[data-act]");
    if (!act) return;
    const a = act.dataset.act;
    switch (a) {
      case "start": this.start(); break;
      case "cancel": this.cancel(); break;
      case "clone": await this.flushAll(); cloneRun(this.id, { seq: this.run.seq }); break;
      case "register": this.rebuildRegister(); break;
      case "bundle": e.preventDefault(); await this.flushAll(); location.href = act.getAttribute("href"); break;
      case "clear-meta": {
        const blank = Object.fromEntries(META_FIELDS.map(([k]) => [k, ""]));
        this.inputs().rfi_form_meta = { ...blank };
        this.inputSaver.schedule({ rfi_form_meta: blank });
        this.renderStep();
        break;
      }
      case "expand-all": this.filteredFindings().list.forEach((d) => this.expanded.add(d.id)); this.renderFindingList(); break;
      case "collapse-all": this.expanded.clear(); this.renderFindingList(); break;
      case "reset-filters":
        this.filters = { q: "", sev: new Set(SEVERITIES), status: "all", scope: "all", cls: "all", sort: this.filters.sort };
        this.renderStep();
        break;
      case "menu": {
        const r = this.run;
        popMenu(act, [
          { label: "Refresh", onClick: () => this.reload() },
          { label: "Copy link", onClick: () => this.copyLink() },
          { label: "Open project", onClick: () => WS.open("project", r.project_id) },
          "-",
          { label: r.status === "draft" ? "Duplicate" : "Edit & re-run (new revision)", onClick: () => cloneRun(r.id, { seq: r.seq }) },
          r.results && { label: "Rebuild register with my edits", onClick: () => this.rebuildRegister() },
          "-",
          { label: "Delete this scan", danger: true, disabled: ACTIVE.has(r.status), onClick: () => deleteRun(r, this.projectName()) },
        ]);
        break;
      }
      default:
        if (["add-rfi", "docx-all", "docx", "revert-rfi", "delete-rfi"].includes(a)) this.rfiAction(a);
    }
  }

  onCtrlEnter() { if (this.step === "scan") this.start(); }

  onKey(e) {
    if (/^[1-5]$/.test(e.key)) { const s = STEPS[+e.key - 1][0]; if (this.available(s)) { e.preventDefault(); this.goto(s); } return; }
    if (this.step === "findings") {
      const k = e.key.toLowerCase();
      if (k === "/") { e.preventDefault(); const q = $("[data-fq]", this.el); if (q) q.focus(); }
      else if (k === "j") { e.preventDefault(); this.focusFinding(1); }
      else if (k === "k") { e.preventDefault(); this.focusFinding(-1); }
      else if (k === "enter" && this.focusId) { e.preventDefault(); this.toggleFinding(this.focusId); }
      else if (this.focusId && (k === "a" || k === "d" || k === "u") && !this.readOnly()) {
        e.preventDefault();
        this.setFinding(this.focusId, { status: { a: "accepted", d: "dismissed", u: "open" }[k] }).then(() => { if (k !== "u") this.focusFinding(1); });
      }
    } else if (this.step === "rfis") {
      const k = e.key.toLowerCase();
      if (k === "/") { e.preventDefault(); const q = $("[data-rq]", this.el); if (q) q.focus(); }
      else if (k === "j") { e.preventDefault(); this.moveRfi(1); }
      else if (k === "k") { e.preventDefault(); this.moveRfi(-1); }
    }
  }
}

// =====================================================================
// Extension points (admin console, RFI log, collaboration... in other files)
// =====================================================================

const VIEW_TYPES = { home: HomeView, project: ProjectView, run: RunView };
const NAV_ITEMS = [];          // {key, label, icon, show(), open()}
const PROJECT_ACTIONS = [];    // {key, label, show(p), run(p, view)}
const PROJECT_MENU_ITEMS = []; // (p) => menu item
const PROJECT_SECTIONS = [];   // (view, container) => void
const RUN_HEAD_ACTIONS = [];   // (runView) => html
const PALETTE_ITEMS = [];      // () => [{label, sub, run}]
const BOOT_HOOKS = [];         // async () => void, after sign-in

window.CS = {
  $, $$, esc, el, debounce, api, uploadFiles, toast, toastError, modal, confirmDialog, promptDialog, popMenu, closeMenus,
  timeEl, timeAgo, fmtDate, fmtBytes, fmtDuration, plural, avatar, statusBadge, sevChips, autoGrow, saveStateHtml,
  Saver, App, WS, Sidebar, Jobs, refreshProjects, refreshSoon, savePrefs, canRole, isAdmin, ROLE_LABEL, STEPS,
  cloneRun, membersDialog, RunView, ProjectView, HomeView,
  VIEW_TYPES, NAV_ITEMS, PROJECT_ACTIONS, PROJECT_MENU_ITEMS, PROJECT_SECTIONS, RUN_HEAD_ACTIONS, PALETTE_ITEMS, BOOT_HOOKS,
};

// =====================================================================
// Global keyboard + boot
// =====================================================================

document.addEventListener("keydown", (e) => {
  if (!App.me) return;
  const mod = e.ctrlKey || e.metaKey;
  const k = e.key.toLowerCase();
  if (mod && k === "k") { e.preventDefault(); Palette.open(); return; }
  if (mod && k === "s") {
    e.preventDefault();
    const had = Saver.anyBusy();
    Saver.flushAll().then(() => toast(had ? "Saved" : "Everything is already saved", { type: "success", timeout: 1600 }));
    return;
  }
  if ($(".overlay")) return;
  if (e.key === "Escape") { closeMenus(); if (isTyping(e)) e.target.blur(); return; }
  const view = WS.views.get(WS.active);
  if (mod && e.key === "Enter") { if (view && view.onCtrlEnter) { e.preventDefault(); view.onCtrlEnter(); } return; }
  if (e.altKey && /^[1-9]$/.test(e.key)) { e.preventDefault(); WS.activateIndex(+e.key - 1); return; }
  if (e.altKey && k === "n") { e.preventDefault(); newScanDialog(); return; }
  if (e.altKey && k === "w") { e.preventDefault(); WS.close(WS.active); return; }
  if (isTyping(e) || mod || e.altKey) return;
  if (e.key === "?") { e.preventDefault(); showShortcuts(); return; }
  if (view && view.onKey) view.onKey(e);
});

// Keep "5m ago" labels fresh.
setInterval(() => { $$("time[datetime]").forEach((t) => { t.textContent = timeAgo(t.getAttribute("datetime")); }); }, 60000);

function initShell() {
  Sidebar.init();
  $("#brand").onclick = () => WS.activate("home");
  $("#search-trigger").onclick = () => Palette.open();
  $("#help-btn").onclick = () => showShortcuts();
  $("#theme-btn").onclick = () => Theme.cycle();
  $("#nav-toggle").onclick = () => $("#app").classList.toggle("nav-open");
  $("#user-btn").onclick = (e) => {
    const cur = Theme.get();
    popMenu(e.currentTarget, [
      { head: `<strong>${esc(App.me.display_name)}</strong><br>@${esc(App.me.username)}` },
      { label: "Account settings", onClick: () => accountDialog() },
      { label: `Theme: light${cur === "light" ? "  ✓" : ""}`, onClick: () => Theme.set("light") },
      { label: `Theme: dark${cur === "dark" ? "  ✓" : ""}`, onClick: () => Theme.set("dark") },
      { label: `Theme: match system${cur === "system" ? "  ✓" : ""}`, onClick: () => Theme.set("system") },
      { label: "Keyboard shortcuts", onClick: () => showShortcuts() },
      "-",
      { label: "Sign out", onClick: () => signOut() },
    ]);
  };
}

async function loadTemplate() {
  try { App.template = await api("/api/rfi-template"); } catch { App.template = null; }
  $("#template-line").innerHTML = App.template
    ? (App.template.loaded ? `RFI form: <strong>${esc(App.template.filename)}</strong>` : "No RFI .docx form loaded")
    : "";
}

async function boot() {
  Auth.init();
  let status;
  try { status = await api("/api/auth/status"); } catch (err) {
    document.body.innerHTML = `<div class="auth-screen"><div class="auth-card"><h3>Can't reach the server</h3><p class="hint">${esc(err.message)}</p>
      <button class="btn primary" onclick="location.reload()">Try again</button></div></div>`;
    return;
  }
  if (!status.user) { Auth.show(status); return; }
  App.me = status.user;
  App.prefs = await api("/api/me/prefs").catch(() => ({}));
  if (App.prefs.theme && App.prefs.theme !== Theme.get()) Theme.apply(App.prefs.theme);
  $("#auth").hidden = true;
  $("#app").hidden = false;
  renderUser();
  initShell();
  Sidebar.renderNav();
  await Promise.all([refreshProjects(), loadTemplate(), ...BOOT_HOOKS.map((h) => Promise.resolve(h()).catch(() => {}))]);
  WS.restore();
  Jobs.poll();
  if (App.me.must_change_password) forcePasswordChange();
}

// Other modules (admin.js, rfilog.js, ...) load after this file and register first.
document.addEventListener("DOMContentLoaded", boot);

})();
