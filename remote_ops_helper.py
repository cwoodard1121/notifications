"""AgentBusOps: the privileged half of remote ops (docs/REMOTE-OPS.md). Stdlib only.

A Windows service running as LocalSystem, installed by Install-RemoteOps.ps1 into an admin-only folder
(C:\\Program Files\\AgentBusOps) together with its own copy of remote_ops.py, so a pull of the notifications repo never
changes privileged code. It serves ONE named pipe (\\\\.\\pipe\\agent-bus-remote-ops) whose security descriptor lets only
SYSTEM and the NT SERVICE\\LocalNotify service SID open it, rejects remote clients, and answers one JSON request per
connection. Every request is validated against the allowlist in remote_ops.py again here; nothing else runs.

  python -I -B remote_ops_helper.py            (by the Service Control Manager)
  python -I -B remote_ops_helper.py --sid      prints the pipe's security descriptor (installer check)
"""
import ctypes
import hashlib
import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script folder from sys.path
import remote_ops  # noqa: E402  (the copy next to this file)

SERVICE_NAME = remote_ops.HELPER_SERVICE
CALLER_SERVICE = "LocalNotify"
DATA_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "AgentBusOps"
UPDATE_TASK = "AgentBusOps Self-Update"
CREATE_NO_WINDOW = 0x08000000
LOG = logging.getLogger("agentbusops")

STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
STATE_NAMES = {1: "stopped", 2: "starting", 3: "stopping", 4: "running", 5: "continuing", 6: "pausing", 7: "paused"}


def service_sid(name):
    """The per-service SID Windows gives NT SERVICE\\<name>: S-1-5-80 + SHA-1 of the upper-case UTF-16LE name."""
    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()
    return "S-1-5-80-" + "-".join(str(v) for v in struct.unpack("<5I", digest))


def pipe_sddl(caller_sid=None):
    """Protected DACL: SYSTEM full control, the caller service read/write, nobody else (no Administrators, no Everyone)."""
    return f"D:P(A;;GA;;;SY)(A;;GRGW;;;{caller_sid or service_sid(CALLER_SERVICE)})"


# ---------------------------------------------------------------------------------------------- Windows plumbing

class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "service_type", "state", "controls", "win32_exit", "specific_exit", "checkpoint", "wait_hint")]


class Scm:
    """Start/stop/query services through the Service Control Manager (ctypes, no sc.exe parsing)."""
    CONNECT, QUERY, START, STOP = 0x0001, 0x0004, 0x0010, 0x0020

    def __init__(self):
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        self.api.OpenSCManagerW.restype = wintypes.HANDLE
        self.api.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        self.api.OpenServiceW.restype = wintypes.HANDLE
        self.api.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
        self.api.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID]
        self.api.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SERVICE_STATUS)]
        self.api.CloseServiceHandle.argtypes = [wintypes.HANDLE]

    def _open(self, name, access):
        scm = self.api.OpenSCManagerW(None, None, self.CONNECT)
        if not scm:
            raise ctypes.WinError(ctypes.get_last_error())
        handle = self.api.OpenServiceW(scm, name, access)
        code = ctypes.get_last_error()
        self.api.CloseServiceHandle(scm)
        if not handle:
            if code == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
                return None
            raise ctypes.WinError(code)
        return handle

    def state(self, name):
        """'running', 'stopped', ... or None when the service does not exist."""
        handle = self._open(name, self.QUERY)
        if handle is None:
            return None
        try:
            status = SERVICE_STATUS()
            if not self.api.QueryServiceStatus(handle, ctypes.byref(status)):
                raise ctypes.WinError(ctypes.get_last_error())
            return STATE_NAMES.get(status.state, str(status.state))
        finally:
            self.api.CloseServiceHandle(handle)

    def start(self, name):
        handle = self._open(name, self.START | self.QUERY)
        if handle is None:
            return False
        try:
            if not self.api.StartServiceW(handle, 0, None):
                code = ctypes.get_last_error()
                if code != 1056:  # ERROR_SERVICE_ALREADY_RUNNING
                    raise ctypes.WinError(code)
            return True
        finally:
            self.api.CloseServiceHandle(handle)

    def stop(self, name):
        handle = self._open(name, self.STOP | self.QUERY)
        if handle is None:
            return False
        try:
            status = SERVICE_STATUS()
            if not self.api.ControlService(handle, 1, ctypes.byref(status)):  # SERVICE_CONTROL_STOP
                code = ctypes.get_last_error()
                if code != 1062:  # ERROR_SERVICE_NOT_ACTIVE
                    raise ctypes.WinError(code)
            return True
        finally:
            self.api.CloseServiceHandle(handle)

    def wait(self, name, want, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state(name) == want:
                return True
            time.sleep(0.5)
        return self.state(name) == want


def find_image_service(image):
    """Name of the service whose ImagePath contains `image` (Parsec's pservice.exe), or None."""
    import winreg
    root = r"SYSTEM\CurrentControlSet\Services"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as services:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(services, index)
            except OSError:
                return None
            index += 1
            try:
                with winreg.OpenKey(services, name) as key:
                    path = str(winreg.QueryValueEx(key, "ImagePath")[0])
            except OSError:
                continue
            if image.lower() in path.lower():
                return name


def console_user():
    """'DOMAIN\\user' signed in at the console, or None at the login screen."""
    try:
        session = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        if session in (0xFFFFFFFF, -1):
            return None
        wts = ctypes.WinDLL("wtsapi32")
        buf, size = wintypes.LPWSTR(), wintypes.DWORD()
        names = []
        for info in (7, 5):  # WTSDomainName, WTSUserName
            if not wts.WTSQuerySessionInformationW(None, session, info, ctypes.byref(buf), ctypes.byref(size)):
                return None
            names.append(buf.value or "")
            wts.WTSFreeMemory(buf)
        return "\\".join(n for n in names if n) if names[1] else None
    except (OSError, AttributeError):
        return None


def uptime_seconds():
    kernel32 = ctypes.windll.kernel32
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return int(kernel32.GetTickCount64() // 1000)


def run(argv, timeout):
    """(exit code, combined output tail); argv is always a fixed list (never a shell)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False,
                              creationflags=CREATE_NO_WINDOW, errors="replace")
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()[-1500:]
    except FileNotFoundError:
        return 127, f"{os.path.basename(argv[0])} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{os.path.basename(argv[0])} did not finish within {timeout} s"


def tailscale_exe():
    return str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe")


def shutdown_exe():
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "shutdown.exe")


def schtasks_exe():
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe")


# ---------------------------------------------------------------------------------------------- the allowlisted actions

class Executor:
    """Runs one validated request. Every OS touch point is injectable for tests."""

    def __init__(self, scm=None, runner=run, data_dir=DATA_DIR, find_image=find_image_service, user=console_user,
                 uptime=uptime_seconds, sleep=time.sleep, restart_delay=8.0):
        self.scm = scm or Scm()
        self.run, self.data_dir, self.find_image = runner, Path(data_dir), find_image
        self.user, self.uptime, self.sleep, self.restart_delay = user, uptime, sleep, restart_delay

    def handle(self, request):
        """Answer dict {ok, result|error, status}; never raises."""
        try:
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            action, args = remote_ops.validate(request.get("action"), request.get("args"))
            result = getattr(self, "do_" + action.replace("-", "_"))(args)
            return {"ok": True, "result": result}
        except ValueError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}
        except remote_ops.OpsError as exc:
            return {"ok": False, "status": exc.status, "error": str(exc)}
        except Exception as exc:  # report, do not crash the service
            LOG.exception("remote ops: %s failed", request.get("action") if isinstance(request, dict) else "?")
            return {"ok": False, "status": 500, "error": f"{type(exc).__name__}: {exc}"}

    def service_name(self, short):
        spec = remote_ops.SERVICES[short]
        if spec.get("image"):
            found = self.find_image(spec["image"])
            if found:
                return found
        return spec["name"]

    def tailscale(self):
        code, out = self.run([tailscale_exe(), "status", "--json"], 20)
        if code == 127:
            return {"installed": False}
        try:
            data = json.loads(out[out.index("{"):]) if "{" in out else {}
        except ValueError:
            data = {}
        ips = (data.get("Self") or {}).get("TailscaleIPs") or (data.get("TailscaleIPs") or [])
        return {"installed": True, "backend": data.get("BackendState") or ("error" if code else None),
                "ipv4": next((ip for ip in ips if ":" not in ip), None)}

    def do_status(self, args):
        services = {}
        for short in remote_ops.SERVICES:
            name = self.service_name(short)
            services[short] = {"service": name, "state": self.scm.state(name) or "not installed"}
        services["localnotify"] = {"service": "LocalNotify", "state": self.scm.state("LocalNotify") or "not installed"}
        return {"uptime_seconds": self.uptime(), "console_user": self.user(), "services": services,
                "tailscale": self.tailscale(), "self_update": (self.data_dir / "allowed_signers").is_file()}

    def do_service_start(self, args):
        name = self.service_name(args["name"])
        if self.scm.state(name) is None:
            raise remote_ops.OpsError(404, f"{args['name']} is not installed on this PC (no service {name})")
        self.scm.start(name)
        ok = self.scm.wait(name, "running", 45)
        return {"service": name, "state": self.scm.state(name), "running": ok}

    def do_service_restart(self, args):
        name = self.service_name(args["name"])
        if self.scm.state(name) is None:
            raise remote_ops.OpsError(404, f"{args['name']} is not installed on this PC (no service {name})")
        if self.scm.state(name) != "stopped":
            self.scm.stop(name)
            self.scm.wait(name, "stopped", 40)
        self.scm.start(name)
        ok = self.scm.wait(name, "running", 45)
        return {"service": name, "state": self.scm.state(name), "running": ok}

    def do_tailscale_up(self, args):
        exe = tailscale_exe()
        code, out = self.run([exe, "up", "--unattended", "--timeout=40s"], 55)
        used = "tailscale up --unattended"
        if code != 0 and ("non-default" in out or "--reset" in out or "mention all" in out):
            # Other flags (hostname, --ssh=false...) are set on this node: keep them, just turn unattended on.
            code, out = self.run([exe, "set", "--unattended=true"], 30)
            used = "tailscale set --unattended=true"
        return {"command": used, "exit": code, "output": out[-600:], "tailscale": self.tailscale()}

    def do_reboot(self, args):
        code, out = self.run([shutdown_exe(), "/r", "/t", "60", "/c", "Agent Bus remote reboot"], 20)
        if code != 0:
            raise remote_ops.OpsError(500, f"shutdown.exe failed ({code}): {out[-300:]}")
        return {"rebooting_in_seconds": 60, "cancel_locally": "shutdown /a"}

    def do_self_update(self, args):
        signers = self.data_dir / "allowed_signers"
        if not signers.is_file():
            raise remote_ops.OpsError(403, "self-update is off on this PC: no allowed_signers file "
                                           "(run Install-RemoteOps.ps1 -SigningKeyFile <your .pub> there)")
        folder = self.data_dir / "update"
        folder.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex[:16]
        result_path = folder / "result.json"
        try:
            result_path.unlink()
        except FileNotFoundError:
            pass
        (folder / "request.json").write_text(json.dumps({"id": request_id, "ref": args["ref"], "ts": time.time()}),
                                             encoding="ascii")
        code, out = self.run([schtasks_exe(), "/Run", "/TN", UPDATE_TASK], 20)
        if code != 0:
            raise remote_ops.OpsError(500, f"could not start the '{UPDATE_TASK}' task ({code}): {out[-300:]}")
        deadline = time.monotonic() + 130
        result = None
        while time.monotonic() < deadline:
            try:
                data = json.loads(result_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict) and data.get("id") == request_id:
                    result = data
                    break
            except (OSError, ValueError):
                pass
            self.sleep(1)
        if result is None:
            raise remote_ops.OpsError(504, "the update task did not finish in time (it runs in Cameron's session: "
                                           "is he signed in on that PC? autologon keeps him signed in)")
        if result.get("ok") and result.get("changed"):
            # Answer first, then restart LocalNotify: it is the one carrying this answer back to the other PC.
            threading.Thread(target=self.restart_localnotify, daemon=True, name="restart-localnotify").start()
            result["localnotify_restart_in_seconds"] = self.restart_delay
        if not result.get("ok"):
            raise remote_ops.OpsError(409, "self-update refused: " + str(result.get("error") or "unknown error")[:400])
        return result

    def restart_localnotify(self):
        self.sleep(self.restart_delay)
        try:
            self.scm.stop("LocalNotify")
            self.scm.wait("LocalNotify", "stopped", 40)
            self.scm.start("LocalNotify")
            LOG.info("remote ops: LocalNotify restarted after self-update")
        except OSError:
            LOG.exception("remote ops: could not restart LocalNotify")


# ---------------------------------------------------------------------------------------------- the pipe server

class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID), ("bInheritHandle", wintypes.BOOL)]


class PipeServer:
    """One pipe instance, reused for every connection (so the name is never free for another process to take)."""
    INVALID = wintypes.HANDLE(-1).value

    def __init__(self, handler, name=remote_ops.PIPE_NAME, sddl=None):
        self.handler, self.name, self.sddl = handler, name, sddl or pipe_sddl()
        self.stop_event = threading.Event()
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)]
        self.k32.CreateNamedPipeW.restype = wintypes.HANDLE
        self.k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        self.k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self.k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self.k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.k32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32.LocalFree.argtypes = [wintypes.HLOCAL]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)]
        self.descriptor = wintypes.LPVOID()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(self.sddl, 1, ctypes.byref(self.descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), self.descriptor, False)
        # PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE (fail if someone already owns the name);
        # byte mode, blocking, PIPE_REJECT_REMOTE_CLIENTS; one instance.
        self.handle = self.k32.CreateNamedPipeW(self.name, 0x00000003 | 0x00080000, 0x00000008, 1,
                                                remote_ops.RESPONSE_MAX, remote_ops.REQUEST_MAX, 0, ctypes.byref(attrs))
        if self.handle in (None, self.INVALID):
            code = ctypes.get_last_error()
            self.k32.LocalFree(self.descriptor)
            raise ctypes.WinError(code)

    def read_request(self):
        buf, chunk, got = b"", ctypes.create_string_buffer(4096), wintypes.DWORD()
        while b"\n" not in buf:
            if not self.k32.ReadFile(self.handle, chunk, len(chunk), ctypes.byref(got), None) or not got.value:
                break
            buf += chunk.raw[:got.value]
            if len(buf) > remote_ops.REQUEST_MAX:
                return None
        line = buf.split(b"\n", 1)[0]
        return line if line else None

    def write(self, payload):
        data = (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode("utf-8")[:remote_ops.RESPONSE_MAX]
        sent = wintypes.DWORD()
        self.k32.WriteFile(self.handle, data, len(data), ctypes.byref(sent), None)
        self.k32.FlushFileBuffers(self.handle)

    def serve_forever(self):
        try:
            while not self.stop_event.is_set():
                if not self.k32.ConnectNamedPipe(self.handle, None):
                    code = ctypes.get_last_error()
                    if code != 535:  # ERROR_PIPE_CONNECTED: the client was quicker than us, fine
                        if self.stop_event.is_set():
                            break
                        LOG.warning("ConnectNamedPipe failed: %s", code)
                        time.sleep(1)
                        continue
                try:
                    if self.stop_event.is_set():
                        break
                    raw = self.read_request()
                    if raw is None:
                        answer = {"ok": False, "status": 400, "error": "empty or oversized request"}
                    else:
                        try:
                            request = json.loads(raw.decode("utf-8"))
                        except ValueError:
                            request = None
                        answer = self.handler(request)
                    self.write(answer)
                finally:
                    self.k32.DisconnectNamedPipe(self.handle)
        finally:
            self.k32.CloseHandle(self.handle)
            self.k32.LocalFree(self.descriptor)

    def shutdown(self):
        """Wake the blocked ConnectNamedPipe by connecting to ourselves (SYSTEM may open it)."""
        self.stop_event.set()
        try:
            with open(self.name, "r+b", buffering=0):
                pass
        except OSError:
            pass


def audited(executor):
    def handle(request):
        started = time.time()
        answer = executor.handle(request)
        what = "?"
        if isinstance(request, dict):
            try:
                what = remote_ops.summary(*remote_ops.validate(request.get("action"), request.get("args")))
            except ValueError:
                what = str(request.get("action"))[:40]
            what += f" (from {str(request.get('origin') or '?')[:40]})"
        LOG.info("remote ops: %s -> %s in %.1fs%s", what, "ok" if answer.get("ok") else "refused/failed",
                 time.time() - started, "" if answer.get("ok") else ": " + str(answer.get("error"))[:200])
        return answer
    return handle


# ---------------------------------------------------------------------------------------------- service host

class HelperService:
    def __init__(self):
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.status = SERVICE_STATUS(0x10, START_PENDING, 0, 0, 0, 1, 30000)
        self.status_handle = None
        self.server = None
        self.main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
        self.handler_type = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
        main_type = self.main_type

        class Entry(ctypes.Structure):
            _fields_ = [("name", wintypes.LPWSTR), ("main", main_type)]

        self.entry_type = Entry
        self.api.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(Entry)]
        self.api.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, self.handler_type, wintypes.LPVOID]
        self.api.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
        self.api.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
        self.main_cb = self.main_type(self.service_main)
        self.handler_cb = self.handler_type(self.control)

    def report(self, state, error=0):
        self.status.state, self.status.win32_exit = state, error
        self.status.controls = 1 | 4 if state == RUNNING else 0  # STOP | SHUTDOWN
        self.status.checkpoint = self.status.checkpoint + 1 if state in (START_PENDING, STOP_PENDING) else 0
        if self.status_handle:
            self.api.SetServiceStatus(self.status_handle, ctypes.byref(self.status))

    def control(self, control, event_type, event_data, context):
        if control in (1, 5):  # STOP, SHUTDOWN
            self.report(STOP_PENDING)
            if self.server:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return 0
        if control == 4:  # INTERROGATE
            self.api.SetServiceStatus(self.status_handle, ctypes.byref(self.status))
            return 0
        return 120

    def service_main(self, argc, argv):
        self.status_handle = self.api.RegisterServiceCtrlHandlerExW(SERVICE_NAME, self.handler_cb, None)
        if not self.status_handle:
            return
        self.report(START_PENDING)
        error = 0
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
                RotatingFileHandler(DATA_DIR / "helper.log", maxBytes=1000000, backupCount=2, encoding="utf-8")])
            self.server = PipeServer(audited(Executor()))
            self.report(RUNNING)
            LOG.info("AgentBusOps serving %s for NT SERVICE\\%s (%s)", remote_ops.PIPE_NAME, CALLER_SERVICE,
                     service_sid(CALLER_SERVICE))
            self.server.serve_forever()
        except Exception:
            error = 1
            LOG.exception("AgentBusOps failed")
        self.report(STOPPED, error)

    def run(self):
        table = (self.entry_type * 2)()
        table[0].name, table[0].main = SERVICE_NAME, self.main_cb
        if not self.api.StartServiceCtrlDispatcherW(table):
            code = ctypes.get_last_error()
            if code == 1063:
                raise RuntimeError("Run this through the AgentBusOps service (Install-RemoteOps.ps1).")
            raise ctypes.WinError(code)


def main(argv):
    if "--sid" in argv:
        print(pipe_sddl())
        return 0
    if os.name != "nt":
        print("Windows only", file=sys.stderr)
        return 2
    try:
        HelperService().run()
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
