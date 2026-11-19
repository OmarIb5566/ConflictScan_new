/* ConflictScan - collaboration and everyday conveniences
 * -----------------------------------------------------------------------
 * Notification bell, comment threads with @mentions, project activity,
 * the trash (30-day restore), the evidence viewer, the usage dashboard
 * (admin), saved filter views, and copying a project's settings.
 */
"use strict";
(() => {
const CS = window.CS;
const { $, $$, esc, el, api, toast, toastError, modal, timeEl, fmtDate, avatar, WS, App, isAdmin, debounce } = CS;

// =====================================================================
// Notification bell
// =====================================================================
const Bell = {
  data: { unread: 0, items: [] },
  timer: null,
  init() {
    const btn = el(`<button class="icon-btn bell" id="bell-btn" title="Notifications" aria-label="Notifications">&#128276;<span class="bell-n" hidden></span></button>`);
    $("#user-btn").before(btn);
    btn.addEventListener("click", (e) => { e.stopPropagation(); this.toggle(); });
    document.addEventListener("click", (e) => { if (!e.target.closest(".bell-panel, #bell-btn")) this.close(); });
    this.poll();
    this.timer = setInterval(() => this.poll(), 30000);
  },
  async poll() {
    try { this.data = await api("/api/notifications"); } catch { return; }
    const n = $(".bell-n");
    n.hidden = !this.data.unread;
    n.textContent = this.data.unread > 99 ? "99+" : this.data.unread;
    if ($(".bell-panel")) this.render();
  },
  close() { const p = $(".bell-panel"); if (p) p.remove(); },
  toggle() { if ($(".bell-panel")) this.close(); else { document.body.appendChild(el(`<div class="bell-panel menu"></div>`)); this.render(); } },
  render() {
    const p = $(".bell-panel");
    const b = $("#bell-btn").getBoundingClientRect();
    p.style.top = `${b.bottom + 6}px`;
    p.style.right = `${Math.max(8, innerWidth - b.right)}px`;
    const icon = { scan_done: "&#10003;", scan_failed: "&#9888;", assigned: "&#128100;", mention: "@", rfi_review: "&#128203;", rfi_overdue: "&#9200;", rfi_status: "&#128203;" };
    p.innerHTML = `<div class="bell-head"><strong>Notifications</strong><span class="grow"></span>
      ${this.data.unread ? `<button class="link-btn" data-read-all>Mark all read</button>` : ""}</div>
      <div class="bell-list">${this.data.items.length ? this.data.items.map((n) => `<a class="bell-item ${n.read_at ? "" : "unread"}" data-n="${esc(n.id)}" href="${esc(n.link || "#/")}">
        <span class="bell-ic k-${esc(n.kind)}">${icon[n.kind] || "&#8226;"}</span>
        <span class="grow"><span class="t">${esc(n.title)}</span>${n.body ? `<span class="b">${esc(n.body)}</span>` : ""}<span class="w">${timeEl(n.created_at)}</span></span></a>`).join("")
        : `<div class="side-empty" style="padding:18px">Nothing yet. You'll be told here when a scan finishes, something is assigned to you, or someone mentions you.</div>`}</div>`;
    p.onclick = async (e) => {
      if (e.target.closest("[data-read-all]")) { this.data = await api("/api/notifications/read", { method: "POST", body: { all: true } }); this.poll(); return; }
      const a = e.target.closest("[data-n]");
      if (a) { api("/api/notifications/read", { method: "POST", body: { ids: [a.dataset.n] } }).then(() => this.poll()); this.close(); }
    };
  },
};

// =====================================================================
// Comments (generic thread)
// =====================================================================
async function mountComments(box, target) {
  if (!box) return;
  box.classList.add("comments");
  const q = new URLSearchParams(Object.entries(target).filter(([, v]) => v));
  let list = [];
  try { list = await api(`/api/comments?${q}`); } catch (err) { box.innerHTML = `<span class="muted small">${esc(err.message)}</span>`; return; }
  const render = () => {
    box.innerHTML = `<div class="c-list">${list.map((c) => `<div class="c-item" data-c="${esc(c.id)}">${avatar(c.user_name, "sm")}
        <div class="grow"><div class="c-meta"><strong>${esc(c.user_name)}</strong> ${timeEl(c.created_at)}${c.edited_at ? " (edited)" : ""}
          ${c.user_id === App.me.id ? `<button class="link-btn small" data-c-del="${esc(c.id)}">delete</button>` : ""}</div>
          <div class="c-body">${esc(c.body).replace(/(^|\s)@([\w.-]{3,40})/g, '$1<span class="mention">@$2</span>')}</div></div></div>`).join("")}</div>
      <div class="c-new"><textarea rows="1" placeholder="Add a comment - type @ and a username to notify someone"></textarea><button class="btn sm" data-c-send>Comment</button></div>`;
    const ta = $("textarea", box);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } });
    $("[data-c-send]", box).onclick = send;
    $$("[data-c-del]", box).forEach((b) => b.onclick = async () => {
      await api(`/api/comments/${b.dataset.cDel}`, { method: "DELETE" }).catch(toastError);
      list = list.filter((c) => c.id !== b.dataset.cDel); render();
    });
    async function send() {
      const body = ta.value.trim();
      if (!body) return;
      try { list.push(await api("/api/comments", { method: "POST", body: { ...target, body } })); render(); }
      catch (err) { toastError(err); }
    }
  };
  render();
}

// =====================================================================
// Evidence viewer: the source page with the quoted words highlighted
// =====================================================================
async function showEvidence(runId, file, quote, reference) {
  let page = 0;
  const load = async (b) => {
    const box = $("[data-ev]", b);
    box.innerHTML = `<span class="spinner"></span>`;
    const q = new URLSearchParams({ file, quote: quote || "", reference: reference || "", page: page || "" });
    let r;
    try { r = await api(`/api/runs/${runId}/evidence?${q}`); } catch (err) { box.innerHTML = `<div class="banner warn">${esc(err.message)}</div>`; return; }
    page = r.page || 0;
    if (r.kind === "text") {
      box.innerHTML = `<pre class="ev-text">${esc(r.before)}<mark>${esc(r.match)}</mark>${esc(r.after)}</pre>
        ${r.found ? "" : `<p class="hint">The exact words were not found in the file's text - showing its beginning.</p>`}`;
    } else {
      box.innerHTML = `${r.kind === "page" ? `<div class="row ev-nav"><button class="btn sm" data-p="-1" ${page <= 1 ? "disabled" : ""}>&larr;</button>
        <span>Page ${page} of ${r.pages}${r.highlighted ? ` &middot; <mark>highlighted</mark>` : quote ? " &middot; quote not found on this page" : ""}</span>
        <button class="btn sm" data-p="1" ${page >= r.pages ? "disabled" : ""}>&rarr;</button><span class="grow"></span>
        <a class="btn sm" href="/api/runs/${esc(runId)}/files/${esc(r.file_id)}/download?inline=1" target="_blank" rel="noopener">Open the file</a></div>` : ""}
        <div class="ev-img"><img src="data:image/png;base64,${r.image}" alt="${esc(file)}"></div>`;
      $$("[data-p]", box).forEach((btn) => btn.onclick = () => { page += +btn.dataset.p; load(b); });
    }
  };
  modal({ title: file, wide: true, cls: "ev-modal",
    body: `${quote ? `<p class="hint">Looking for: <q>${esc(quote.slice(0, 200))}</q>${reference ? ` &middot; ${esc(reference)}` : ""}</p>` : ""}<div data-ev></div>`,
    actions: [{ label: "Close", primary: true }], onOpen: (b) => load(b) });
}

// =====================================================================
// Project activity
// =====================================================================
const ACTION_TEXT = {
  "project.create": "created the project", "project.update": "changed the project", "project.member_set": "changed a member",
  "project.member_removed": "removed a member", "run.create": "started a revision", "run.clone": "made a revision",
  "run.start": "started a scan", "run.cancel": "cancelled a scan", "run.files_added": "added files", "run.file_removed": "removed a file",
  "run.file_replaced": "replaced a file", "finding.review": "triaged a finding", "finding.bulk": "triaged findings",
  "rfi.edit": "edited an RFI", "rfi.add": "added an RFI", "rfi.delete": "deleted an RFI", "rfi_log.register": "added an RFI to the log",
  "rfi_log.update": "updated the RFI log", "rfi_log.response": "recorded a response", "comment.add": "commented",
  "export.bundle": "downloaded the outputs", "export.download": "downloaded a document", "run.trash": "deleted a revision",
};
async function renderActivity(view, box) {
  const wrap = el(`<details class="card activity"><summary><strong>Activity</strong> <span class="muted small">who did what in this project</span></summary><div data-act-list><span class="spinner"></span></div></details>`);
  box.appendChild(wrap);
  wrap.addEventListener("toggle", async () => {
    if (!wrap.open || wrap.dataset.loaded) return;
    wrap.dataset.loaded = "1";
    try {
      const rows = await api(`/api/projects/${view.id}/activity?limit=80`);
      $("[data-act-list]", wrap).innerHTML = rows.length ? `<ul class="act-list">${rows.map((a) => `<li>${avatar(a.username || "?", "sm")}
        <span class="grow"><strong>${esc(a.username || "system")}</strong> ${esc(ACTION_TEXT[a.action] || a.action)}${a.summary ? ` <span class="muted">- ${esc(a.summary)}</span>` : ""}</span>
        <span class="muted small nowrap">${timeEl(a.at)}</span></li>`).join("")}</ul>` : `<p class="muted">No activity yet.</p>`;
    } catch (err) { $("[data-act-list]", wrap).textContent = err.message; }
  });
}

// =====================================================================
// Trash
// =====================================================================
class TrashView {
  static canRestore() { return true; }
  constructor() { this.key = "trash:main"; this.el = el(`<div class="view"><div class="view-inner" data-inner></div></div>`); }
  tabTitle() { return "Trash"; }
  hash() { return "#/trash/main"; }
  async load() { this.data = await api("/api/trash"); this.render(); }
  async reload() { await this.load(); }
  render() {
    const items = this.data.items;
    const label = { project: "Project", revision: "Revision", rfi: "RFI" };
    $("[data-inner]", this.el).innerHTML = `<div class="page-head"><div class="grow"><h1 class="title-input" style="border:none">Trash</h1>
      <div class="meta">Deleted projects, revisions and RFIs can be restored for ${this.data.days} days, then they are removed for good.</div></div></div>
      ${items.length ? `<div class="table-wrap"><table class="list"><thead><tr><th>What</th><th>Name</th><th>Deleted</th><th>Removed for good on</th><th></th></tr></thead><tbody>
      ${items.map((i) => `<tr><td><span class="badge outline">${label[i.type]}</span></td><td><strong>${esc(i.title)}</strong>${i.sub ? `<div class="desc">${esc(i.sub)}</div>` : ""}</td>
        <td class="small nowrap">${timeEl(i.deleted_at)}</td><td class="small">${esc(i.purge_after || "")}</td>
        <td class="actions"><button class="btn sm primary" data-restore="${esc(i.type)}|${esc(i.id)}">Restore</button></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="card empty"><div class="big">&#128465;</div><h3>The trash is empty</h3></div>`}`;
    $$("[data-restore]", this.el).forEach((b) => b.onclick = async () => {
      const [type, id] = b.dataset.restore.split("|");
      try { await api(`/api/trash/${type}/${id}/restore`, { method: "POST" }); toast("Restored", { type: "success" }); await CS.refreshProjects(); this.reload(); }
      catch (err) { toastError(err); }
    });
  }
}

// =====================================================================
// Usage dashboard (admin)
// =====================================================================
async function usageHtml(days = 30) {
  const u = await api(`/api/admin/usage?days=${days}`);
  const max = Math.max(1, ...u.daily_model_minutes.map((d) => d.minutes));
  const bars = u.daily_model_minutes;
  const bw = bars.length ? Math.max(4, Math.min(28, Math.floor(640 / bars.length) - 2)) : 10;
  const W = Math.max(320, bars.length * (bw + 2)), H = 140;
  const chart = bars.length ? `<svg class="usage-chart" viewBox="0 0 ${W} ${H + 18}" role="img" aria-label="Model minutes per day">
      <line x1="0" y1="${H}" x2="${W}" y2="${H}" class="axis"/>
      ${bars.map((d, i) => { const h = Math.max(2, Math.round((d.minutes / max) * (H - 10))); const x = i * (bw + 2);
        return `<g class="bar-g"><rect x="${x}" y="${H - h}" width="${bw}" height="${h}" rx="3" class="bar"/><rect x="${x - 1}" y="0" width="${bw + 2}" height="${H}" class="hit"><title>${esc(d.day)}: ${d.minutes} model minutes</title></rect></g>`; }).join("")}
      <text x="0" y="${H + 14}" class="tick">${esc(bars[0].day)}</text><text x="${W}" y="${H + 14}" class="tick" text-anchor="end">${esc(bars[bars.length - 1].day)}</text>
    </svg>` : `<p class="muted">No model calls in this period.</p>`;
  const meter = (v, m) => `<span class="meter"><span style="width:${Math.round((v / Math.max(1, m)) * 100)}%"></span></span>`;
  const maxP = Math.max(1, ...u.users.map((x) => x.pages), ...u.projects.map((x) => x.pages));
  return `<div class="row wrap" style="gap:12px;margin-bottom:14px">
      <select data-usage-days>${[7, 30, 90, 365].map((d) => `<option value="${d}" ${d === u.days ? "selected" : ""}>Last ${d} days</option>`).join("")}</select>
      <div class="stat-chip"><span class="n">${u.totals.scans}</span>scans</div><div class="stat-chip"><span class="n">${u.totals.pages}</span>pages</div>
      <div class="stat-chip"><span class="n">${u.totals.model_minutes}</span>model minutes</div>
      <div class="hint grow">Fair-use limit: ${u.daily_page_limit ? `<strong>${u.daily_page_limit}</strong> pages per person per day (admins exempt)` : "none"} - set <code>scans.daily_page_limit</code> in the settings file.</div></div>
    <div class="card"><h4 style="margin:0 0 8px">Model time per day <span class="muted small">(minutes of GPU work; hover a bar for its value)</span></h4>${chart}</div>
    <div class="usage-grid"><div><div class="section-title"><h3>By person</h3></div><div class="table-wrap"><table class="list"><thead><tr><th>Person</th><th>Scans</th><th>Pages</th><th>Model min</th></tr></thead><tbody>
      ${u.users.map((x) => `<tr><td>${esc(x.user)}</td><td>${x.scans}</td><td>${meter(x.pages, maxP)} ${x.pages}</td><td>${x.model_minutes}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">No scans.</td></tr>'}</tbody></table></div></div>
    <div><div class="section-title"><h3>By project</h3></div><div class="table-wrap"><table class="list"><thead><tr><th>Project</th><th>Scans</th><th>Pages</th><th>Model min</th></tr></thead><tbody>
      ${u.projects.map((x) => `<tr><td>${esc(x.name)}</td><td>${x.scans}</td><td>${meter(x.pages, maxP)} ${x.pages}</td><td>${x.model_minutes}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">No scans.</td></tr>'}</tbody></table></div></div></div>`;
}

// =====================================================================
// Saved filter views (findings)
// =====================================================================
const Views = {
  list() { return ((App.prefs.views || {}).findings) || []; },
  save(name, filters) {
    const all = this.list().filter((v) => v.name !== name);
    all.push({ name, filters: { ...filters, sev: [...filters.sev] } });
    CS.savePrefs({ views: { ...(App.prefs.views || {}), findings: all } });
  },
  remove(name) { CS.savePrefs({ views: { ...(App.prefs.views || {}), findings: this.list().filter((v) => v.name !== name) } }); },
};

// =====================================================================
// Registration
// =====================================================================
CS.mountComments = mountComments;
CS.showEvidence = showEvidence;
CS.Views = Views;
CS.usageHtml = usageHtml;
CS.VIEW_TYPES.trash = TrashView;
CS.NAV_ITEMS.push({ key: "trash:main", label: "Trash", icon: "&#128465;", open: () => WS.open("trash", "main") });
CS.PROJECT_SECTIONS.push((view, box) => renderActivity(view, box));
CS.BOOT_HOOKS.push(() => Bell.init());
CS.PALETTE_ITEMS.push(() => [{ label: "Open the trash", run: () => WS.open("trash", "main") },
  ...(isAdmin() ? [{ label: "Admin: usage", run: () => WS.open("admin", "main", { step: "usage" }) }] : [])]);
})();
