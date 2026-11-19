/* ConflictScan - project settings and document history
 * -----------------------------------------------------------------------
 * Settings dialog (managers): RFI header defaults, set names, drafting,
 * response time, house RFI numbering, the project's own RFI form with a
 * preview of every placeholder. Project page section: each document's
 * versions across revisions.
 */
"use strict";
(() => {
const { $, $$, esc, el, api, toast, toastError, modal, timeEl, fmtBytes, canRole, WS, refreshProjects } = window.CS;

const META = [["consultant", "Consultant"], ["employer", "Employer / client"], ["project_code", "Project code (on the form)"],
  ["to", "To (recipient)"], ["location", "Location"], ["boq_no", "BOQ No."], ["dwg_no", "Dwg No."], ["level", "Level"], ["specs_no", "Specs No."]];

function numberPreview(fmt, start, code) {
  try {
    return fmt.replace(/\{code\}/g, code || "PRJ").replace(/\{disc\}/g, "ELEC").replace(/\{discipline\}/g, "Electrical")
      .replace(/\{seq(?::0?(\d+)d)?\}/g, (_m, w) => String(start || 1).padStart(+w || 0, "0"));
  } catch { return ""; }
}

async function settingsDialog(p) {
  let data;
  try { data = await api(`/api/projects/${p.id}`); } catch (err) { toastError(err); return; }
  const s = data.settings || {};
  const meta = s.rfi_form_meta || {};
  const num = s.numbering || { format: "{code}-RFI-{seq:04d}", start: 1 };
  const manage = canRole(data.my_role, "manager");
  const dis = manage ? "" : "disabled";
  modal({
    title: `Project settings - ${data.name}`, wide: true,
    body: `
      ${manage ? "" : `<div class="banner info"><span class="grow">Only project managers can change these settings.</span></div>`}
      <nav class="subtabs" data-ps-tabs>
        ${[["header", "RFI header"], ["sets", "Sets & drafting"], ["numbering", "RFI numbering"], ["form", "RFI form"]]
          .map(([k, l], i) => `<button class="subtab ${i ? "" : "on"}" data-ps-tab="${k}" type="button">${l}</button>`).join("")}
      </nav>
      <section data-ps="header"><p class="hint">Filled into the header of every RFI of this project. Each revision can still change them.</p>
        <div class="form-grid">${META.map(([k, l]) => `<label class="field"><span>${l}</span><input data-meta="${k}" value="${esc(meta[k] || "")}" ${dis}></label>`).join("")}</div></section>
      <section data-ps="sets" hidden><div class="form-grid two">
        <label class="field"><span>Project code (register)</span><input data-code value="${esc(data.code || "")}" ${dis} placeholder="e.g. TRC02"></label>
        <label class="field"><span>Response time (working days)</span><input data-days type="number" min="1" max="90" value="${esc(s.response_days || 7)}" ${dis}></label>
        <label class="field"><span>Default name of Set A</span><input data-seta value="${esc(s.set_a_label || "")}" ${dis} placeholder="e.g. Tender BOQ + Specs"></label>
        <label class="field"><span>Default name of Set B</span><input data-setb value="${esc(s.set_b_label || "")}" ${dis} placeholder="e.g. IFC drawings"></label>
        <label class="field"><span>RFI drafting</span><select data-grouping ${dis}>
          <option value="consolidated" ${s.rfi_grouping !== "single" ? "selected" : ""}>Group related findings into one RFI</option>
          <option value="single" ${s.rfi_grouping === "single" ? "selected" : ""}>One RFI per finding</option></select></label></div></section>
      <section data-ps="numbering" hidden>
        <p class="hint">House numbering for the project's RFI log. Placeholders: <code>{code}</code> project code, <code>{disc}</code>
          discipline code (ELEC, MECH, STR...), <code>{seq:04d}</code> running number with 4 digits.</p>
        <div class="form-grid two">
          <label class="field"><span>Format</span><input data-fmt class="mono" value="${esc(num.format)}" ${dis}></label>
          <label class="field"><span>Next number</span><input data-start type="number" min="1" value="${esc(num.start || 1)}" ${dis}></label></div>
        <p>Next RFI will be numbered <strong class="mono" data-num-preview></strong></p></section>
      <section data-ps="form" hidden><div data-form-body></div></section>`,
    actions: manage ? [{ label: "Cancel" }, {
      label: "Save settings", primary: true,
      onClick: async (b) => {
        const v = (sel) => b.querySelector(sel).value;
        const body = {
          code: v("[data-code]").trim(),
          settings: {
            rfi_form_meta: Object.fromEntries($$("[data-meta]", b).map((i) => [i.dataset.meta, i.value.trim()])),
            set_a_label: v("[data-seta]"), set_b_label: v("[data-setb]"), rfi_grouping: v("[data-grouping]"),
            response_days: +v("[data-days]") || 7, numbering: { format: v("[data-fmt]").trim(), start: +v("[data-start]") || 1 },
          },
        };
        await api(`/api/projects/${p.id}`, { method: "PATCH", body });
        toast("Project settings saved", { type: "success" });
        refreshProjects();
      },
    }] : [{ label: "Close", primary: true }],
    onOpen: (b) => {
      const tabs = $$("[data-ps-tab]", b);
      tabs.forEach((t) => t.addEventListener("click", () => {
        tabs.forEach((x) => x.classList.toggle("on", x === t));
        $$("[data-ps]", b).forEach((sec) => { sec.hidden = sec.dataset.ps !== t.dataset.psTab; });
        if (t.dataset.psTab === "form") renderForm();
      }));
      const upd = () => { $("[data-num-preview]", b).textContent = numberPreview($("[data-fmt]", b).value, $("[data-start]", b).value, $("[data-code]", b).value); };
      ["[data-fmt]", "[data-start]", "[data-code]"].forEach((sel) => $(sel, b).addEventListener("input", upd));
      upd();
      const renderForm = async () => {
        const box = $("[data-form-body]", b);
        const form = (data.settings || {}).rfi_form;
        box.innerHTML = `<p class="hint">This project's RFI documents are filled into ${form ? `<strong>${esc(form.name)}</strong> (uploaded ${timeEl(form.uploaded_at)} by ${esc(form.uploaded_by || "")})`
          : "the company's shared RFI form"}. Upload the client's own .docx form with <code>{{token}}</code> placeholders to use it instead.</p>
          ${manage ? `<div class="row" style="gap:8px"><label class="btn"><input type="file" accept=".docx" hidden data-form-file>${form ? "Replace form" : "Upload .docx form"}</label>
            ${form ? `<button class="btn ghost" type="button" data-form-remove>Use the shared form</button>` : ""}
            <a class="btn ghost" href="/api/projects/${esc(p.id)}/rfi-form/preview?format=docx">Download a filled sample</a></div>` : ""}
          <div data-preview style="margin-top:12px"><span class="spinner"></span></div>`;
        try {
          const pv = await api(`/api/projects/${p.id}/rfi-form/preview`);
          $("[data-preview]", box).innerHTML = `<p class="small muted">${pv.tokens.length} placeholders, filled with ${pv.sample_from_scan ? `${esc(pv.sample)} from the latest scan` : "a sample RFI"}:</p>
            <div class="table-wrap"><table class="list compact"><thead><tr><th>Placeholder</th><th>Value</th></tr></thead><tbody>
            ${pv.tokens.map((t) => `<tr><td class="mono small">{{${esc(t.token)}}}</td><td class="small">${t.filled ? esc(t.value) : '<span class="badge verify">empty</span>'}</td></tr>`).join("")}</tbody></table></div>`;
        } catch (err) { $("[data-preview]", box).innerHTML = `<div class="banner warn">${esc(err.message)}</div>`; }
        const file = $("[data-form-file]", box);
        if (file) file.addEventListener("change", async () => {
          const fd = new FormData();
          fd.append("file", file.files[0]);
          try {
            const r = await window.CS.uploadFiles(`/api/projects/${p.id}/rfi-form`, fd, () => {});
            data.settings = { ...(data.settings || {}), rfi_form: r.rfi_form };
            toast(`RFI form ${r.rfi_form.name} loaded (${r.rfi_form.tokens.length} placeholders)`, { type: "success" });
            renderForm();
          } catch (err) { toastError(err); }
        });
        const rm = $("[data-form-remove]", box);
        if (rm) rm.addEventListener("click", async () => {
          try { await api(`/api/projects/${p.id}/rfi-form`, { method: "DELETE" }); data.settings.rfi_form = null; renderForm(); }
          catch (err) { toastError(err); }
        });
      };
    },
  });
}

// ---- document history on the project page -----------------------------------
async function renderHistory(view, box) {
  const wrap = el(`<div><div class="section-title"><h3>Documents</h3><span class="hint">Every file of every revision. Unchanged files are shared between revisions - never uploaded or read twice.</span></div>
    <div class="card" data-history><span class="spinner"></span></div></div>`);
  box.appendChild(wrap);
  let rows;
  try { rows = await api(`/api/projects/${view.id}/files`); } catch (err) { $("[data-history]", wrap).innerHTML = esc(err.message); return; }
  if (!rows.length) { $("[data-history]", wrap).innerHTML = '<span class="muted">No files yet.</span>'; return; }
  $("[data-history]", wrap).outerHTML = `<div class="table-wrap"><table class="list"><thead><tr><th>Set</th><th>Document</th><th>Versions</th><th>Used in</th></tr></thead><tbody>
    ${rows.map((h) => `<tr><td><span class="badge ${h.slot === "b" ? "scope" : "outline"}">Set ${esc(h.slot.toUpperCase())}</span></td>
      <td><strong>${esc(h.current_name)}</strong>${h.versions.length > 1 ? `<div class="desc">replaced ${h.versions.length - 1} time(s)</div>` : ""}</td>
      <td class="small">${h.versions.map((v, i) => `<div>${i === h.versions.length - 1 ? "&#9679;" : "&#9675;"}
        <a href="/api/runs/${esc(v.run_id)}/files/${esc(v.file_id)}/download">${esc(v.name)}</a>
        <span class="muted">${fmtBytes(v.size)}${v.pages ? ` &middot; ${v.pages} p` : ""}</span></div>`).join("")}</td>
      <td class="small">${h.versions.map((v) => `<div>Rev ${v.revisions.join(", ")}</div>`).join("")}</td></tr>`).join("")}
    </tbody></table></div>`;
}

window.CS.PROJECT_ACTIONS.push({ key: "settings", label: "&#9881; Settings", run: (p) => settingsDialog(p) });
window.CS.PROJECT_MENU_ITEMS.push((p) => ({ label: "Project settings & RFI form", onClick: () => settingsDialog(p) }));
window.CS.PROJECT_SECTIONS.push((view, box) => renderHistory(view, box));
window.CS.projectSettingsDialog = settingsDialog;
})();
