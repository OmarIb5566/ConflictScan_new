"""
Windows services for ConflictScan (pywin32)
===========================================
Two services, so the GPU work and the web server restart independently:

    ConflictScanWeb      serve.py   - the web app (waitress), embedded worker off
    ConflictScanWorker   worker.py  - claims scans from the shared queue

Install / remove from an elevated PowerShell (see install_services.ps1):

    python service\winservice.py --startup auto install
    python service\winservice.py start        (both)
    python service\winservice.py remove

Settings come from conflictscan.toml next to the code (or CONFLICTSCAN_CONFIG
set as a machine environment variable). Logs: <data_dir>\logs\*.log

Windows only. Needs: pip install pywin32
"""

import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import servicemanager  # noqa: E402
import win32event  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402


def _log_to(name):
    import config  # noqa: F401
    data = os.environ.get("CONFLICTSCAN_DATA_DIR") or os.path.join(ROOT, "data")
    os.makedirs(os.path.join(data, "logs"), exist_ok=True)
    f = open(os.path.join(data, "logs", f"{name}.log"), "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = f


class _Base(win32serviceutil.ServiceFramework):
    def __init__(self, args):
        super().__init__(args)
        self.stop_handle = win32event.CreateEvent(None, 0, 0, None)
        self.stop_flag = threading.Event()

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_flag.set()
        win32event.SetEvent(self.stop_handle)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ""))
        self.main()


class WebService(_Base):
    _svc_name_ = "ConflictScanWeb"
    _svc_display_name_ = "ConflictScan web server"
    _svc_description_ = "ConflictScan / RFI automation - web application (waitress)."

    def main(self):
        _log_to("web")
        os.environ.setdefault("CONFLICTSCAN_EMBEDDED_WORKER", "0")
        os.environ.setdefault("CONFLICTSCAN_DEBUG", "0")
        from waitress.server import create_server
        import api
        server = create_server(api.app, host=os.environ.get("CONFLICTSCAN_HOST", "127.0.0.1"),
                               port=int(os.environ.get("CONFLICTSCAN_PORT", "5000")),
                               threads=int(os.environ.get("CONFLICTSCAN_THREADS", "16")))
        threading.Thread(target=server.run, daemon=True).start()
        win32event.WaitForSingleObject(self.stop_handle, win32event.INFINITE)
        server.close()


class WorkerService(_Base):
    _svc_name_ = "ConflictScanWorker"
    _svc_display_name_ = "ConflictScan scan worker"
    _svc_description_ = "ConflictScan / RFI automation - runs queued scans on the local model servers."

    def main(self):
        _log_to("worker")
        import worker
        worker.run(stop_event=self.stop_flag)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[-1] in ("install", "remove", "start", "stop", "restart", "update"):
        for cls in (WebService, WorkerService):
            win32serviceutil.HandleCommandLine(cls, argv=[sys.argv[0]] + sys.argv[1:])
    else:
        win32serviceutil.HandleCommandLine(WebService)
