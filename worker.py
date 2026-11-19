"""
Scan worker process
===================
Claims scans and upload checks from the shared job queue (the database) and
runs them. Run one per machine that has GPUs / model servers - normally as
the "ConflictScanWorker" Windows service (service/install_services.ps1).

    python worker.py [--scans N] [--checks N]

When the web server and the workers are separate, set
CONFLICTSCAN_EMBEDDED_WORKER=0 for the web server.

Windows compatible.
"""

import config  # noqa: F401 - settings file first

import argparse
import signal
import threading

import jobs
import storage


def run(scan_threads: int = None, inspect_threads: int = 2, stop_event: threading.Event = None) -> None:
    storage.init_db()
    pool = jobs.WorkerPool(scan_threads=scan_threads or jobs.WORKERS, inspect_threads=inspect_threads).start()
    print(f"[worker] {jobs.WORKER_NAME}: {scan_threads or jobs.WORKERS} scan thread(s), "
          f"{inspect_threads} upload-check thread(s). Waiting for jobs...")
    stop_event = stop_event or threading.Event()
    try:
        while not stop_event.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    print("[worker] stopping - running scans go back to the queue if they do not finish")
    pool.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--scans", type=int, default=None)
    p.add_argument("--checks", type=int, default=2)
    a = p.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run(a.scans, a.checks, stop)
