"""
Production web server (waitress - pure Python, no build tools, runs on Windows)
==============================================================================
    python serve.py          host/port/threads from conflictscan.toml [server]

HTTPS: put the app behind the company's reverse proxy (IIS with URL Rewrite +
ARR, see service/iis-web.config) and set server.https = true and
server.behind_proxy = true, or terminate TLS on a load balancer. Waitress
itself speaks plain HTTP and should listen on 127.0.0.1 only in that setup.

Windows compatible.
"""

import config  # noqa: F401 - settings file first

import os

from waitress import serve

os.environ.setdefault("CONFLICTSCAN_DEBUG", "0")
import api  # noqa: E402
import db  # noqa: E402
import jobs  # noqa: E402
from adapters import files as file_store  # noqa: E402

if __name__ == "__main__":
    host = os.environ.get("CONFLICTSCAN_HOST", "127.0.0.1")
    port = int(os.environ.get("CONFLICTSCAN_PORT", "5000"))
    threads = int(os.environ.get("CONFLICTSCAN_THREADS", "16"))
    embedded = os.environ.get("CONFLICTSCAN_EMBEDDED_WORKER", "1").lower() not in ("0", "false", "no")
    print(f"ConflictScan {config.VERSION} on http://{host}:{port} ({threads} threads)")
    print(f"  database: {db.describe()}")
    print(f"  files:    {file_store.store().describe()}")
    print(f"  workers:  {'embedded, ' + str(jobs.WORKERS) + ' scan thread(s)' if embedded else 'separate worker service'}")
    jobs.start_embedded_worker()
    serve(api.app, host=host, port=port, threads=threads, ident="ConflictScan",
          trusted_proxy=os.environ.get("CONFLICTSCAN_TRUSTED_PROXY") or None,
          trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"}
          if os.environ.get("CONFLICTSCAN_TRUSTED_PROXY") else None)
