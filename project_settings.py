"""
Project settings
================
Stored in projects.settings (JSON), edited by the project's managers:

    rfi_form_meta      RFI header defaults: consultant, employer, project code, to, location ...
    set_a_label / set_b_label   default names of the two document sets
    rfi_grouping       consolidated | single
    response_days      working days allowed for a response (due dates)
    numbering          {"format": "{code}-RFI-{disc}-{seq:04d}", "start": 1,
                        "disciplines": {"electrical": "ELEC", ...}}
    rfi_form           the project's own .docx RFI form (uploaded here) - used for
                       every RFI document of the project instead of the shared one

    POST   /api/projects/<id>/rfi-form            upload the project's .docx form
    DELETE /api/projects/<id>/rfi-form            back to the shared form
    GET    /api/projects/<id>/rfi-form/preview    every {{token}} with the value it would get
    GET    /api/projects/<id>/rfi-form/preview.docx   the form filled with a sample RFI

Windows compatible.
"""

import os
import re
import uuid
from typing import Any, Dict, Optional

from flask import abort, jsonify, request, send_file
from werkzeug.utils import secure_filename

import jobs
import storage
from adapters import files as file_store
from webcore import audit, load_project, login_required

DEFAULT_FORMAT = "{code}-RFI-{seq:04d}"
DISCIPLINE_CODES = {"architectural": "ARCH", "structural": "STR", "civil": "CIV", "electrical": "ELEC",
                    "mechanical": "MECH", "plumbing": "PLMB", "fire": "FF", "hvac": "HVAC", "survey": "SURV",
                    "landscape": "LAND", "general": "GEN", "other": "GEN"}
META_FIELDS = ("consultant", "employer", "project_code", "to", "location", "boq_no", "dwg_no", "level", "specs_no")


def clean(settings: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(settings or {})
    meta = out.get("rfi_form_meta") or {}
    out["rfi_form_meta"] = {k: str(meta.get(k) or "").strip()[:200] for k in META_FIELDS if meta.get(k)}
    for k in ("set_a_label", "set_b_label"):
        if k in out:
            out[k] = str(out[k] or "").strip()[:120]
    if out.get("rfi_grouping") not in (None, "consolidated", "single"):
        out["rfi_grouping"] = "consolidated"
    if "response_days" in out:
        try:
            out["response_days"] = max(1, min(90, int(out["response_days"])))
        except (TypeError, ValueError):
            out.pop("response_days")
    num = out.get("numbering") or {}
    if num:
        fmt = str(num.get("format") or DEFAULT_FORMAT)[:120]
        try:
            format_number(fmt, 1, "electrical", "CODE", num.get("disciplines"))
        except (KeyError, ValueError, IndexError) as exc:
            abort(400, description=f"Numbering format is not valid ({exc}). Use {{code}}, {{disc}}, {{seq:04d}}.")
        out["numbering"] = {"format": fmt, "start": max(1, int(num.get("start") or 1)),
                            "disciplines": {str(k).lower(): str(v)[:10] for k, v in (num.get("disciplines") or {}).items()}}
    return out


def format_number(fmt: str, seq: int, discipline: str, code: str, disciplines: Optional[Dict[str, str]] = None) -> str:
    codes = {**DISCIPLINE_CODES, **{k.lower(): v for k, v in (disciplines or {}).items()}}
    disc = codes.get((discipline or "general").lower(), (discipline or "GEN")[:4].upper())
    return fmt.format(code=code or "PRJ", disc=disc, seq=seq, discipline=(discipline or "general").title())


def project_template(project: Optional[Dict[str, Any]]) -> Optional[str]:
    form = ((project or {}).get("settings") or {}).get("rfi_form") or {}
    if form.get("key") and file_store.store().exists(form["key"]):
        return file_store.store().local_path(form["key"])
    return None


def _scan_extras(run, project):
    path = project_template(project)
    return {"rfi_template_path": path} if path else {}


jobs._SCAN_EXTRAS.append(_scan_extras)


def _sample_rfi(project) -> Dict[str, Any]:
    for r in storage.list_runs(project["id"]):
        if r["status"] == "complete":
            run = storage.get_run(r["id"])
            items = ((run or {}).get("results") or {}).get("rfi_items") or []
            if items:
                return dict(items[0]), run
    return ({"rfi_no": "RFI-001", "subject": "Sample - concrete grade of columns L3-L5",
             "background": "During review of the tender documents the following was noted.",
             "discrepancy_summary": "1. Specification clause 03300-2.1 states C40/50; drawing S-201 notes C35/45.",
             "question": "1. Please confirm the governing concrete grade.", "area": "Block A",
             "discipline": "structural", "priority": "high", "attachments": ["S-201.pdf", "Specs Div 03.pdf"],
             "cost_impact": "To be assessed", "schedule_impact": "To be assessed",
             "response_required_by": "(due date)"}, None)


def register(app):
    import api
    api.SETTINGS_CLEANERS.append(clean)
    api.TEMPLATE_RESOLVERS.append(project_template)

    @app.route("/api/projects/<project_id>/rfi-form", methods=["POST", "DELETE"])
    @login_required
    def project_rfi_form(project_id):
        project, _role = load_project(project_id, "manager")
        settings = dict(project.get("settings") or {})
        store = file_store.store()
        if request.method == "DELETE":
            old = settings.pop("rfi_form", None)
            if old and old.get("key"):
                store.delete(old["key"])
            storage.update_project(project_id, settings=settings)
            audit("project.rfi_form_removed", "project", project_id, project_id, "back to the shared RFI form")
            return jsonify({"rfi_form": None})
        upload = request.files.get("file")
        if not upload or not upload.filename.lower().endswith(".docx"):
            return jsonify({"error": "Upload the RFI form as a Word .docx file with {{token}} placeholders."}), 400
        name = secure_filename(upload.filename) or "rfi_form.docx"
        key = f"projects/{project_id}/rfi_form_{uuid.uuid4().hex[:8]}.docx"
        path = store.local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        upload.save(path)
        import upload_checks
        problem = upload_checks.sniff(path, ".docx")
        tokens = []
        if not problem:
            try:
                from nodes.docx_template import list_template_tokens
                tokens = list_template_tokens(path)
            except Exception as exc:  # noqa: BLE001
                problem = f"could not be read as a Word form ({exc})"
        if problem:
            os.remove(path)
            return jsonify({"error": f"'{name}' {problem}."}), 400
        store.sync(key)
        old = settings.get("rfi_form")
        if old and old.get("key"):
            store.delete(old["key"])
        settings["rfi_form"] = {"key": key, "name": name, "tokens": tokens, "uploaded_at": storage.now(),
                                "uploaded_by": request.user["display_name"]}
        storage.update_project(project_id, settings=settings)
        audit("project.rfi_form", "project", project_id, project_id, f"RFI form {name} ({len(tokens)} placeholders)")
        return jsonify({"rfi_form": settings["rfi_form"]}), 201

    @app.route("/api/projects/<project_id>/rfi-form/preview")
    @login_required
    def project_rfi_form_preview(project_id):
        project, _role = load_project(project_id)
        path = api.template_for(project)
        if not path:
            return jsonify({"error": "No RFI form is loaded for this project."}), 404
        from nodes.docx_template import list_template_tokens
        from nodes.rfi_drafting import build_token_values
        rfi, run = _sample_rfi(project)
        inputs = (run or {}).get("inputs") or {}
        meta = {**((project.get("settings") or {}).get("rfi_form_meta") or {}), **(inputs.get("rfi_form_meta") or {})}
        state_like = {"project_name": project["name"], "user_id": request.user["display_name"], "rfi_form_meta": meta}
        values = build_token_values(rfi, state_like, inputs.get("document_a_label") or "Set A",
                                    inputs.get("document_b_label") or "Set B")
        if rfi.get("log_no"):
            values["rfi_no"] = rfi["log_no"]
        tokens = list_template_tokens(path)
        if request.args.get("format") == "docx":
            from nodes.docx_template import fill_docx_template
            out = os.path.join(storage.DATA_DIR, "scratch", f"preview_{uuid.uuid4().hex[:8]}.docx")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            fill_docx_template(path, out, values)
            return send_file(out, as_attachment=True, download_name="RFI form preview.docx")
        return jsonify({"form": os.path.basename(path), "own_form": bool(project_template(project)),
                        "sample": rfi.get("rfi_no"), "sample_from_scan": bool(run),
                        "tokens": [{"token": t, "value": str(values.get(t, ""))[:500], "filled": bool(values.get(t))}
                                   for t in tokens]})
