"""Remote ops: a small, fixed allowlist of actions Cameron can run on his other PC (docs/REMOTE-OPS.md).

Shared by both halves (stdlib only, no bus imports, so the privileged helper can carry its own copy):
  - LocalNotify (NT SERVICE\\LocalNotify, least privilege) receives `ops` frames over the end-to-end encrypted Agent
    Console channel (bus_remote.py), validates them here and forwards them over a local named pipe;
  - the AgentBusOps helper service (remote_ops_helper.py, LocalSystem) validates them here AGAIN and runs them.
There is no shell and no free-form string ever reaches a command line: every argument is one of the fixed values below.
"""
import json
import re
import threading

PIPE_NAME = r"\\.\pipe\agent-bus-remote-ops"
HELPER_SERVICE = "AgentBusOps"
REQUEST_MAX = 4096          # bytes of one request on the pipe
RESPONSE_MAX = 64 * 1024    # bytes of one answer on the pipe

ACTIONS = ("status", "service-start", "service-restart", "tailscale-up", "reboot", "self-update")
# Short names Cameron types -> how the helper finds the Windows service ("pservice.exe" = match by ImagePath).
SERVICES = {
    "parsec": {"name": "Parsec", "image": "pservice.exe"},
    "tailscale": {"name": "Tailscale"},
    "sshd": {"name": "sshd"},
    "docker": {"name": "com.docker.service"},
    "crd": {"name": "chromoting"},
}
# A signed tag like ops-20261018-1, or a full 40-hex commit sha. Nothing else (no ranges, no options, no refs/...).
REF_RE = re.compile(r"(ops-\d{8}-\d{1,3}|[0-9a-f]{40})")
# How long the viewer waits for an answer, per action (self-update fetches and verifies on the other PC).
TIMEOUTS = {"status": 40, "service-start": 70, "service-restart": 100, "tailscale-up": 70, "reboot": 40, "self-update": 160}


class OpsError(Exception):
    """A refusal or failure with an HTTP-like status (400 bad request, 403 refused, 503 not installed, 504 timeout)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def validate(action, args=None):
    """(action, clean args) for an allowed request; ValueError for anything else. Unknown argument keys are refused."""
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("action must be one of " + ", ".join(ACTIONS))
    args = {} if args is None else args
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    allowed = {"service-start": {"name"}, "service-restart": {"name"}, "reboot": {"confirm"}, "self-update": {"ref"}}
    extra = set(args) - allowed.get(action, set())
    if extra:
        raise ValueError(f"{action} takes no argument(s) {', '.join(sorted(map(str, extra)))}")
    if action in ("service-start", "service-restart"):
        name = args.get("name")
        if not isinstance(name, str) or name.lower() not in SERVICES:
            raise ValueError("name must be one of " + ", ".join(SERVICES))
        return action, {"name": name.lower()}
    if action == "reboot":
        if args.get("confirm") is not True:
            raise ValueError("reboot needs confirm: true (bus.py remote <machine> reboot --confirm)")
        return action, {"confirm": True}
    if action == "self-update":
        ref = args.get("ref")
        if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
            raise ValueError("ref must be a signed tag named ops-YYYYMMDD-N or a full 40-character commit sha")
        return action, {"ref": ref}
    return action, {}


def summary(action, args):
    """One short line for logs and bus notices: `service-restart parsec`, `self-update ops-20261018-1`."""
    extra = args.get("name") or args.get("ref") or ""
    return f"{action} {extra}".strip()


def call(request, timeout=120, pipe=PIPE_NAME, opener=None):
    """Send one request to the helper over its named pipe; returns its answer (a dict).

    OpsError 503 when the helper is not installed (no pipe), 403 when the pipe refuses this account, 504 on timeout."""
    data = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > REQUEST_MAX:
        raise OpsError(400, "request too large")
    opener = opener or (lambda: open(pipe, "r+b", buffering=0))
    box = {}

    def talk():
        try:
            for attempt in range(20):
                try:
                    handle = opener()
                    break
                except FileNotFoundError:
                    box["error"] = OpsError(503, "not-installed")
                    return
                except PermissionError:
                    box["error"] = OpsError(403, "the helper's pipe refused this account (only NT SERVICE\\LocalNotify may call it)")
                    return
                except OSError as exc:  # ERROR_PIPE_BUSY (231): another request is being served
                    if getattr(exc, "winerror", None) != 231 or attempt == 19:
                        raise
                    threading.Event().wait(0.25)
            with handle:
                handle.write(data)
                buf = b""
                while b"\n" not in buf and len(buf) <= RESPONSE_MAX:
                    try:
                        chunk = handle.read(8192)
                    except BrokenPipeError:
                        break
                    if not chunk:
                        break
                    buf += chunk
            line = buf.split(b"\n", 1)[0]
            if not line:
                raise OpsError(502, "the helper closed the pipe without an answer")
            answer = json.loads(line.decode("utf-8"))
            if not isinstance(answer, dict):
                raise OpsError(502, "the helper sent a malformed answer")
            box["answer"] = answer
        except OpsError as exc:
            box["error"] = exc
        except (OSError, ValueError) as exc:
            box["error"] = OpsError(502, f"helper pipe: {type(exc).__name__}: {exc}")

    worker = threading.Thread(target=talk, name="remote-ops-pipe", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise OpsError(504, "the helper did not answer in time")
    if "error" in box:
        raise box["error"]
    return box["answer"]
