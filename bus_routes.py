"""Agent Bus HTTP routes. service.Handler dispatches /bus/*, /state and /ui here (and /hooks/claude via its own reader)."""
import json
import re
import select
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

import asks as asks_mod
import bus_console
import bus_hooks
from bus_core import TOOLS, BusError, agent_view, clean, integer, number
from bus_ident import folder_name

UI = Path(__file__).resolve().parent / "ui" / "index.html"
CONSOLE_UI = Path(__file__).resolve().parent / "ui" / "console.html"
UI_HEADERS = {"Content-Security-Policy": "default-src 'self' 'unsafe-inline'; connect-src 'self'",
              "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
POST_PATHS = ("/bus/register", "/bus/heartbeat", "/bus/deregister", "/bus/send", "/bus/claim", "/state", "/console/prompt",
              "/bus/ask", "/bus/ask-allow", "/bus/ask-deny")
ASK_PATH = re.compile(r"/bus/asks/(?:ask-)?([0-9a-f]{16})/(answer|cancel)")
NONCE_SLOT = b'__ASK_NONCE__"'
ENDPOINTS = ["POST /bus/register", "POST /bus/heartbeat", "POST /bus/deregister", "GET /bus/agents", "GET /bus/agents?remote=1", "POST /bus/send",
             "GET /bus/inbox?agent=<id>&mark=1", "GET /bus/wait?agent=<id>&timeout=25[&urgent=1]", "POST /bus/claim",
             "GET /bus/messages", "GET /bus/thread/<id>", "GET /bus/held", "GET /bus/relay", "GET /state", "POST /state",
             "POST /hooks/claude", "GET /ui", "GET /console", "GET /console/sessions",
             "GET /console/events?agent=<id>[&after=<cursor>&wait=25 | &before=<start>]", "POST /console/prompt",
             "POST /bus/ask", "GET /bus/asks[?agent=<id>&open=1]", "GET /bus/asks/<id>[?wait=50]", "POST /bus/asks/<id>/cancel",
             "POST /bus/asks/<id>/answer (Cameron: nonce)", "POST /bus/ask-allow (Cameron: nonce)", "POST /bus/ask-deny (Cameron: nonce)"]
WAKE_LEAD = "[Agent Bus] You were idle, but a message needs your attention. Handle it (reply if asked), then stop."


def owns(path):
    return path.startswith(("/bus/", "/console")) or path in ("/state", "/ui", "/hooks/claude")


def console(app):
    """The Agent Console (bus_console.Console), created on first use."""
    if getattr(app, "console", None) is None:
        app.console = bus_console.Console(app)
    return app.console


def page(handler, path, nonce=None):
    try:
        body = path.read_bytes()
    except OSError:
        raise BusError(404, f"The bus UI is not installed (ui/{path.name} is missing)") from None
    if nonce:
        # Cameron-only actions (answering asks, the ask allowlist) need this per-start nonce; only the page carries it.
        body = body.replace(NONCE_SLOT, nonce.encode() + b'"', 1)
    handler.send_body(200, body, "text/html; charset=utf-8", UI_HEADERS)


def arg(query, name, default=None):
    values = query.get(name)
    return values[0].strip() if values and values[0].strip() else default


def flag(query, name, default=False):
    value = arg(query, name)
    return default if value is None else value.lower() in ("1", "true", "yes", "on")


def connected(handler):
    """False once a long-poll client has hung up, so nothing gets marked delivered into a dead socket."""
    try:
        readable, _, _ = select.select([handler.connection], [], [], 0)
        return not readable or handler.connection.recv(1, socket.MSG_PEEK) != b""
    except (OSError, ValueError):
        return False


def agent_fields(data):
    name = clean(data.get("name"), "name", 60)
    if name is not None and ("," in name or name[0] in "@#*" or name.lower() in ("all", "bus", "user", "planner")
                             or name.lower().startswith("tool:")):
        raise ValueError("name must not contain commas, start with @ # * or tool:, or be all/bus/user/planner")
    tool = clean(data.get("tool"), "tool", 40)
    if tool is not None and tool not in TOOLS:
        raise ValueError("tool must be one of " + ", ".join(TOOLS))
    status = clean(data.get("status"), "status", 20)
    if status is not None and status not in ("working", "idle"):
        raise ValueError("status must be working or idle")
    cwd = clean(data.get("cwd"), "cwd", 500)
    folder = clean(data.get("folder"), "folder", 100) or (folder_name(cwd) if cwd else None)
    account = clean(data.get("account"), "account", 40)
    meta = data.get("meta")
    if meta is not None and (not isinstance(meta, dict) or len(json.dumps(meta)) > 2000):
        raise ValueError("meta must be a JSON object of at most 2000 characters")
    return {"name": name, "tool": tool, "status": status, "cwd": cwd, "folder": folder.lower() if folder else None,
            "account": account.lower() if account else None, "activity": clean(data.get("activity"), "activity", 300),
            "session_id": clean(data.get("session_id"), "session_id", 100), "meta": meta}


def get(handler, app, path, query):
    """Returns (status, payload) for JSON routes; writes /ui itself and returns None."""
    bus = app.bus
    if path in ("/ui", "/console"):
        page(handler, UI if path == "/ui" else CONSOLE_UI, app.asks.nonce if path == "/ui" else None)
        return None
    if path == "/bus/asks":
        return 200, app.asks.listing(arg(query, "agent"), integer(arg(query, "limit"), "limit", 1, 200, 50), flag(query, "open"))
    if path.startswith("/bus/asks/") and len(path) > len("/bus/asks/"):
        wait = number(arg(query, "wait"), "wait", 0, 55) or 0
        return 200, app.asks.get(unquote(path[len("/bus/asks/"):]), wait)
    if path == "/console/sessions":
        return 200, console(app).sessions()
    if path == "/console/events":
        after = integer(arg(query, "after"), "after", 0, 10 ** 13)
        before = integer(arg(query, "before"), "before", 0, 10 ** 13)
        wait = number(arg(query, "wait"), "wait", 0, 55) or 0
        return 200, console(app).events(arg(query, "agent"), after, before, wait, lambda: connected(handler))
    if path == "/state":
        return 200, bus.state_payload()
    if path == "/bus/agents":
        # Local only by default (the Planner desktop's snapshot); ?remote=1 appends the other PCs' agents with `machine`.
        return 200, {"agents": bus.agents(flag(query, "include_ended"), arg(query, "account"), arg(query, "folder"), arg(query, "tool"),
                                          remote=flag(query, "remote")),
                     "now": time.time()}
    if path == "/bus/relay":
        return 200, app.relay_health()
    if path in ("/bus/inbox", "/bus/wait"):
        agent = arg(query, "agent")
        if not agent:
            raise ValueError("agent is required")
        limit = integer(arg(query, "limit"), "limit", 1, 200, 20)
        if path == "/bus/inbox":
            return 200, bus.inbox(agent, flag(query, "mark", True), limit)
        timeout = number(arg(query, "timeout"), "timeout", 0, 3600)
        urgent = flag(query, "urgent")
        result = bus.wait(agent, 25 if timeout is None else min(timeout, 55), limit, urgent, lambda: connected(handler))
        if urgent and result["messages"]:
            result["text"] = bus_hooks.injection(result, WAKE_LEAD)
        return 200, result
    if path == "/bus/messages":
        return 200, bus.history(integer(arg(query, "limit"), "limit", 1, 500, 50), integer(arg(query, "since_seq"), "since_seq", 0, 10 ** 12, 0),
                                arg(query, "thread"), arg(query, "from"), arg(query, "to"), arg(query, "kind"))
    if path.startswith("/bus/thread/") and len(path) > len("/bus/thread/"):
        return 200, bus.thread(unquote(path[len("/bus/thread/"):]))
    if path == "/bus/held":
        rows = app.store.held(integer(arg(query, "limit"), "limit", 1, 200, 50))
        return 200, {"events": rows, "count": len(rows)}
    raise BusError(404, "Endpoint not found")


def post(app, path, data):
    bus = app.bus
    if path in ("/bus/register", "/bus/heartbeat"):
        agent_id = clean(data.get("id"), "id", 80, path == "/bus/heartbeat") or "other-" + uuid.uuid4().hex[:8]
        fields = agent_fields(data)
        return 200, agent_view(bus.upsert(agent_id, rename=fields["name"] is not None, **fields))
    if path == "/bus/deregister":
        return 200, {"ok": bus.end(clean(data.get("id"), "id", 80, True))}
    if path == "/bus/send":
        result = bus.send(data)
        return (200 if result["duplicate"] else 201), result
    if path == "/bus/claim":
        return 200, bus.claim(data.get("message_id"), data.get("agent"))
    if path == "/state":
        bus.set_state(data)
        return 200, bus.state_payload()
    if path == "/console/prompt":
        return 201, console(app).prompt(data)
    if path == "/bus/ask":
        return 201, {"ask": app.asks.create(data)}
    if path in ("/bus/ask-allow", "/bus/ask-deny"):
        app.asks.check_nonce(data)
        target = clean(data.get("target"), "target", 61, True)
        by = clean(data.get("by"), "by", 20) or "ui"
        if path == "/bus/ask-deny":
            return 200, app.asks.remove_allow(target, by)
        return 200, app.asks.set_allow(target, asks_mod.parse_duration(clean(data.get("for"), "for", 20)), by)
    match = ASK_PATH.fullmatch(path)
    if match:
        if match[2] == "answer":
            return 200, app.asks.answer_ui(match[1], data)
        return 200, app.asks.cancel(match[1], data)
    raise BusError(404, "Endpoint not found")
