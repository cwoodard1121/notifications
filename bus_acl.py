"""Read access for the LocalNotify Windows service to Claude Code transcripts in any config dir (Agent Console).

Grant-ConsoleAccess.ps1 covers ~\\.claude*\\projects. Sessions started through a per-project profile keep their
transcripts in <project>\\.claude\\projects instead, which the service account (NT SERVICE\\LocalNotify) can't read.
Those folders belong to Cameron, so his own SessionStart hook (`bus.py hook`, running as him) can grant the service
read-only access without an admin prompt: once per folder, in a detached `bus.py console-grant` so the hook stays fast.
Granted folders are listed in data/console-acl.json; Grant-ConsoleAccess.ps1 -Remove and Install-Service.ps1 -Remove
revoke them. Standard library only (it runs on the hook's fast path).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVICE_META = ROOT / "data" / "windows-service.json"
GRANTED = ROOT / "data" / "console-acl.json"
# <config dir>/projects/<project slug>/<session id>.jsonl, the config dir being any folder named .claude or .claude-<name>.
TRANSCRIPT_RE = re.compile(r"^(?P<root>.*[\\/]\.claude(?:-[A-Za-z0-9_]+)?)[\\/]projects[\\/][^\\/]+[\\/][0-9A-Za-z-]{8,100}\.jsonl$")


def projects_dir(transcript):
    """<config dir>\\projects for a transcript path, or None when it isn't one."""
    match = TRANSCRIPT_RE.match(str(transcript or ""))
    if not match or ".." in str(transcript):
        return None
    return os.path.normcase(os.path.normpath(os.path.join(match["root"], "projects")))


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def service_account():
    """The service account when the bus runs as the LocalNotify Windows service, else None (a standalone service runs
    as Cameron and reads everything already)."""
    meta = load(SERVICE_META)
    if os.name != "nt" or not isinstance(meta, dict) or not isinstance(meta.get("account"), str):
        return None
    return meta["account"]


def covered():
    """Folders the service can already read: Install-Service / Grant-ConsoleAccess acl_paths plus earlier grants."""
    meta = load(SERVICE_META) or {}
    granted = load(GRANTED) or {}
    paths = list(meta.get("acl_paths") or []) + list(granted.get("paths") or [])
    return {os.path.normcase(os.path.normpath(p)) for p in paths if isinstance(p, str)}


def needs_grant(transcript):
    """The projects folder to grant for this transcript, or None (not a service install, already granted, or gone)."""
    folder = projects_dir(transcript)
    if not folder or service_account() is None or folder in covered():
        return None
    return folder if os.path.isdir(folder) else None


def spawn_grant(transcript):
    """Hook fast path: start `bus.py console-grant <folder>` detached when the folder still needs it. Never raises."""
    try:
        folder = needs_grant(transcript)
        if not folder:
            return False
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([sys.executable, str(ROOT / "bus.py"), "console-grant", folder], cwd=str(ROOT),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
        return True
    except Exception:
        return False


def grant(folder):
    """Give the service read-only (RX, inherited) access to `folder` and record it. Returns (ok, message)."""
    account = service_account()
    if account is None:
        return False, "The bus isn't running as the LocalNotify Windows service, so no grant is needed."
    folder = os.path.normpath(folder)
    if not os.path.isdir(folder) or os.path.basename(folder).lower() != "projects" \
            or not re.fullmatch(r"\.claude(?:-[A-Za-z0-9_]+)?", os.path.basename(os.path.dirname(folder)), re.I):
        return False, f"Not a Claude Code projects folder: {folder}"
    key = os.path.normcase(folder)
    if key in covered():
        return True, f"Already readable by {account}: {folder}"
    result = subprocess.run(["icacls", folder, "/grant", f"{account}:(OI)(CI)RX", "/Q"], capture_output=True, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        return False, f"icacls failed on {folder}: {(result.stderr or result.stdout).strip()}"
    data = load(GRANTED) or {}
    paths = [p for p in data.get("paths") or [] if isinstance(p, str)]
    if key not in {os.path.normcase(os.path.normpath(p)) for p in paths}:
        paths.append(folder)
    tmp = GRANTED.with_suffix(".tmp")
    tmp.write_text(json.dumps({"account": account, "paths": paths}, indent=2), encoding="utf-8")
    os.replace(tmp, GRANTED)
    return True, f"{account} can now read transcripts in {folder}"
