"""
AI fleet in the admin console
=============================
    GET  /api/admin/fleet          active profile, every role -> configured / actually used model,
                                   models installed per server, last-24h calls per role, RFI memory size
    PUT  /api/admin/fleet          {"profile": "...", "roles": {"drafter": "qwen3:14b", ...}} - saved in the
                                   database, takes effect for the next scan (no restart)
    POST /api/admin/fleet/test     {"role": "..."} - one tiny request to that role's model, with timing

Windows compatible.
"""

import time

from flask import jsonify, request
from sqlalchemy import Integer, func, select

import storage
from webcore import admin_required, audit


def register(app):

    @app.route("/api/admin/fleet", methods=["GET", "PUT"])
    @admin_required
    def admin_fleet():
        from fleet import client, memory, profile
        from adapters import model_servers
        if request.method == "PUT":
            body = request.get_json(silent=True) or {}
            current = storage.get_setting("fleet", {}) or {}
            if "profile" in body:
                if body["profile"] and body["profile"] not in profile.profile_names():
                    return jsonify({"error": "Unknown profile."}), 400
                current["profile"] = body["profile"] or None
            if "roles" in body and isinstance(body["roles"], dict):
                roles = dict(current.get("roles") or {})
                for k, v in body["roles"].items():
                    if v and str(v).endswith(":cloud"):
                        return jsonify({"error": "Cloud models are not allowed - only local models."}), 400
                    roles[k] = (str(v).strip() or None) if v else None
                current["roles"] = {k: v for k, v in roles.items() if v}
            storage.set_setting("fleet", current, request.user["id"])
            audit("admin.fleet", "settings", "fleet", summary=f"AI fleet: {current}")
        prof = profile.active()
        servers = model_servers.health(max_age=10)
        since = storage.ago(hours=24)
        from db import model_calls
        with storage.tx() as conn:
            stats = conn.execute(select(
                model_calls.c.role, model_calls.c.model, func.count(), func.avg(model_calls.c.ms),
                func.sum(model_calls.c.cached.cast(Integer)), func.sum((~model_calls.c.ok).cast(Integer)))
                .where(model_calls.c.at >= since).group_by(model_calls.c.role, model_calls.c.model)).all()
        usage = [{"role": r[0], "model": r[1], "calls": r[2], "avg_ms": int(r[3] or 0), "cached": int(r[4] or 0),
                  "failed": int(r[5] or 0)} for r in stats]
        roles = []
        console = storage.get_setting("fleet", {}) or {}
        for name in sorted(prof["roles"]):
            cfg = profile.role(name)
            cands = client.resolve(name)
            roles.append({"role": name, "configured": cfg["model"], "fallback": cfg.get("fallback", []),
                          "parallel": cfg.get("parallel"), "used": cands[0][0] if cands else None,
                          "server": cands[0][1]["name"] if cands else None,
                          "overridden": bool((console.get("roles") or {}).get(name))})
        try:
            mem = memory.count()
        except Exception:  # noqa: BLE001
            mem = {}
        try:
            from nodes.reviewer_feedback import summary as learning_summary
            learning = learning_summary()
        except Exception:  # noqa: BLE001
            learning = {}
        return jsonify({"profile": prof["name"], "mode": prof["mode"], "profiles": profile.profile_names(),
                        "features": {f: prof.get(f) for f in profile.FEATURES}, "roles": roles,
                        "servers": servers, "usage_24h": usage, "rfi_memory": mem, "learning": learning,
                        "console": console})

    @app.route("/api/admin/fleet/test", methods=["POST"])
    @admin_required
    def admin_fleet_test():
        from fleet import client
        role = (request.get_json(silent=True) or {}).get("role")
        started = time.time()
        try:
            if role == "embedder":
                ok = bool(client.embed([f"ping {time.time()}"])[0])
                reply = "embedding returned" if ok else "no embedding"
            else:
                reply = client.call(role, "Reply with JSON only.", f'Return {{"ok": true}} ({time.time()})',
                                    max_retries=0, cache=False)
            return jsonify({"ok": True, "ms": int((time.time() - started) * 1000), "model": client.resolved_model(role),
                            "reply": reply[:200]})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "ms": int((time.time() - started) * 1000), "error": str(exc)[:300]})
