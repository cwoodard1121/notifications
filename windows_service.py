"""Native Windows service host. No downloaded wrapper or Python packages required.

Installed by Install-Service.ps1; Service Control Manager runs this entrypoint.
"""
import argparse
import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading

SERVICE_NAME = "LocalNotify"
STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN = 1, 4
SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_SHUTDOWN = 1, 4, 5


class SourceChanged(Exception):
    pass


def source_state(root):
    state = {}
    for pattern in ("*.py", "bus_protocol.md", "config.json", "config.example.json"):
        for path in root.glob(pattern):
            try:
                state[path.name] = path.stat().st_mtime_ns
            except OSError:
                pass
    return state


def reload_problem(config, names):
    """Return why a reload would leave the service unable to start, or None if it is safe."""
    try:
        for name in names:
            path = config.ROOT / name
            if name.endswith(".py") and path.exists():
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
        # Validate with the new code in a fresh interpreter: this process still holds the old config module.
        check = subprocess.run([sys.executable, "-c", "import config; config.load()"], cwd=config.ROOT,
                               capture_output=True, text=True, timeout=60)
        if check.returncode:
            return (check.stderr.strip().splitlines() or ["config check failed"])[-1]
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


class ServiceStatus(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "service_type", "state", "controls", "win32_exit", "specific_exit", "checkpoint", "wait_hint")]


class WindowsService:
    def __init__(self, sessions_dir):
        self.sessions_dir = sessions_dir
        self.stop = threading.Event()
        self.status_lock = threading.RLock()
        self.status = ServiceStatus(0x10, START_PENDING, 0, 0, 0, 1, 30000)
        self.status_handle = None
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
        self.handler_type = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
        main_type = self.main_type

        class ServiceTableEntry(ctypes.Structure):
            _fields_ = [("name", wintypes.LPWSTR), ("main", main_type)]

        self.table_type = ServiceTableEntry
        self.api.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(ServiceTableEntry)]
        self.api.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
        self.api.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, self.handler_type, wintypes.LPVOID]
        self.api.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
        self.api.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(ServiceStatus)]
        self.api.SetServiceStatus.restype = wintypes.BOOL
        # Keep callbacks alive for the full dispatcher lifetime.
        self.main_callback = self.main_type(self.service_main)
        self.handler_callback = self.handler_type(self.control)

    def report(self, state, error=0, wait_hint=0):
        with self.status_lock:
            self.status.state = state
            self.status.win32_exit = error
            self.status.wait_hint = wait_hint
            self.status.controls = SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN if state == RUNNING else 0
            self.status.checkpoint = self.status.checkpoint + 1 if state in (START_PENDING, STOP_PENDING) else 0
            if self.status_handle:
                if not self.api.SetServiceStatus(self.status_handle, ctypes.byref(self.status)):
                    logging.error("SetServiceStatus failed: Windows error %s", ctypes.get_last_error())

    def control(self, control, event_type, event_data, context):
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            self.report(STOP_PENDING, wait_hint=30000)
            self.stop.set()
            return 0
        if control == SERVICE_CONTROL_INTERROGATE:
            with self.status_lock:
                self.api.SetServiceStatus(self.status_handle, ctypes.byref(self.status))
            return 0
        return 120  # ERROR_CALL_NOT_IMPLEMENTED

    def service_main(self, argc, argv):
        self.status_handle = self.api.RegisterServiceCtrlHandlerExW(SERVICE_NAME, self.handler_callback, None)
        if not self.status_handle:
            return
        self.report(START_PENDING, wait_hint=30000)
        app = server = http_thread = None
        error = 0
        try:
            # Import application code after reporting START_PENDING.
            import config
            from service import App, Server
            os.chdir(config.ROOT)
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
                RotatingFileHandler(config.DATA_DIR / "service.log", maxBytes=1000000, backupCount=2, encoding="utf-8")])
            cfg = config.load()
            # The service has its own profile. Always use the installing user's log path.
            cfg["codex"]["sessions_dir"] = str(self.sessions_dir)
            app = App(cfg, config.DATA_DIR)
            app.runtime = "windows-service"
            server = Server(("127.0.0.1", cfg["port"]), app)
            http_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
            http_thread.start()
            app.start()
            self.report(RUNNING)
            logging.info("Windows service listening on http://127.0.0.1:%s (provider: %s)", cfg["port"], cfg["provider"])
            baseline, pending, warned, ticks = source_state(config.ROOT), None, None, 0
            while not self.stop.wait(1):
                if not http_thread.is_alive() or any(not thread.is_alive() for thread in app.threads):
                    raise RuntimeError("A service worker stopped unexpectedly")
                ticks += 1
                if ticks % 5:
                    continue
                current = source_state(config.ROOT)
                if current == baseline:
                    pending = None
                elif current != pending:
                    pending = current  # still being edited; wait for it to settle
                elif current != warned:
                    names = sorted(n for n in set(current) | set(baseline) if current.get(n) != baseline.get(n))
                    problem = reload_problem(config, names)
                    if problem:
                        warned = current
                        logging.warning("Source changed (%s) but not reloading: %s", ", ".join(names), problem)
                    else:
                        logging.info("Source changed (%s); restarting to load it", ", ".join(names))
                        raise SourceChanged
        except SourceChanged:
            error = 1  # non-zero exit makes Windows restart the service within seconds
        except Exception:
            error = 1
            logging.exception("Windows service failed")
        finally:
            # Keep the control callback quick; the main thread performs cleanup.
            try:
                self.report(STOP_PENDING, error=error, wait_hint=30000)
                if server:
                    if http_thread and http_thread.is_alive():
                        server.shutdown()
                        http_thread.join(timeout=5)
                    server.server_close()
                if app:
                    app.close()
            except Exception:
                error = 1
                logging.exception("Windows service shutdown failed")
            self.report(STOPPED, error=error)

    def run(self):
        table = (self.table_type * 2)()
        table[0].name = SERVICE_NAME
        table[0].main = self.main_callback
        if not self.api.StartServiceCtrlDispatcherW(table):
            code = ctypes.get_last_error()
            if code == 1063:
                raise RuntimeError("Launch this entrypoint through the LocalNotify Windows service. Use Install-Service.ps1 to install it.")
            raise ctypes.WinError(code)


def main():
    parser = argparse.ArgumentParser(description="Local Notify Windows service host")
    parser.add_argument("--sessions-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This entrypoint is for Windows only")
    if not args.sessions_dir.is_absolute():
        parser.error("--sessions-dir must be absolute; services use a different user profile")
    try:
        WindowsService(args.sessions_dir).run()
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
