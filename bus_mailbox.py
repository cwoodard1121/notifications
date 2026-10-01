"""File mailbox bridge: lets the cloud "study Claude" (files only, no localhost) act as the bus agent `study-claude`."""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from bus_core import BusError, agent_view, clean, state_view

LOG = logging.getLogger("notify")
ROOT = Path(__file__).resolve().parent
MAX_FILE = 256 * 1024
STATUS_EVERY, PRUNE_EVERY, INBOX_DAYS, PROCESSED_DAYS, SETTLE = 5, 3600, 7, 30, 0.3
TYPES = ("broadcast", "dm", "request", "reply", "state", "activity", "notify", "ping")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def decode(raw):
    # Windows tools love BOMs (and PowerShell 5 defaults to UTF-16): accept all of them.
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("an outbox file must contain one JSON object")
    return data


def atomic_write(path, text):
    """UTF-8 without BOM, LF; tmp + os.replace, retrying while a reader holds the target open."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as file:
        file.write(text)
    for attempt in range(10):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 9:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.05 * (attempt + 1))


def dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


class MailboxBridge:
    def __init__(self, app):
        self.app = app
        self.bus = app.bus
        settings = app.cfg["bus"]["mailbox"]
        self.agent_id = settings.get("agent_id") or "study-claude"
        self.dirs = [Path(d).expanduser() for d in settings.get("dirs") or []]
        self.poll = float(settings.get("poll_seconds") or 1)
        self.ready = set()
        self.failures = {}
        self.logged = {}
        self.last_status = self.last_prune = 0
        self.status = app.mailbox_status
        self.status.update(enabled=True, agent_id=self.agent_id, dirs=[str(d) for d in self.dirs], running=False,
                           last_tick=None, error=None, processed=0, delivered=0)

    def run(self):
        # Must never exit: the Windows service host treats a dead thread as a crash.
        while not self.app.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.problem("tick", exc)
            self.app.stop_event.wait(self.poll)

    def problem(self, key, exc):
        message = f"{type(exc).__name__}: {exc}"
        self.status["error"] = f"{key}: {message}"
        if time.time() - self.logged.get((key, message), 0) > 300:
            self.logged[(key, message)] = time.time()
            LOG.warning("Mailbox bridge %s: %s", key, message)

    def tick(self):
        now = time.time()
        if self.agent_id not in self.bus.cache:
            # First tick, or the daily prune cleared the cache: make sure the agent row exists (no last_seen bump).
            self.bus.upsert(self.agent_id, touch=False, tool="claude-desktop-cloud", account="desktop",
                            folder="studywithclaude", name=self.agent_id)
        errors = False
        for directory in self.dirs:
            try:
                self.setup(directory)
                self.process_outbox(directory)
            except Exception as exc:
                errors = True
                self.problem(str(directory), exc)
        try:
            self.drain()
        except Exception as exc:
            errors = True
            self.problem("inbox", exc)
        if now - self.last_status >= STATUS_EVERY:
            self.last_status = now
            for directory in self.ready:
                try:
                    self.write_status(directory)
                except Exception as exc:
                    errors = True
                    self.problem(f"{directory} status", exc)
        if now - self.last_prune >= PRUNE_EVERY:
            self.last_prune = now
            for directory in self.ready:
                self.prune(directory)
        self.status.update(running=True, last_tick=now_iso())
        if not errors:
            self.status["error"] = None

    def setup(self, directory):
        if directory in self.ready:
            return
        if not directory.is_dir():
            if not directory.parent.is_dir():
                raise FileNotFoundError(f"mailbox parent folder is missing: {directory.parent}")
            directory.mkdir()
        for name in ("outbox", "inbox", "processed"):
            (directory / name).mkdir(exist_ok=True)
        protocol = (ROOT / "bus_protocol.md").read_text(encoding="utf-8-sig")
        protocol = protocol.replace("{{MAILBOX_DIR}}", str(directory)).replace("{{AGENT_ID}}", self.agent_id)
        atomic_write(directory / "PROTOCOL.md", protocol)
        self.ready.add(directory)
        self.last_status = 0
        LOG.info("Mailbox bridge ready: %s", directory)

    # ---- outbox
    def process_outbox(self, directory):
        now = time.time()
        entries = []
        for path in (directory / "outbox").glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
        for mtime, path in sorted(entries):
            if now - mtime < SETTLE:
                continue
            try:
                if path.stat().st_size > MAX_FILE:
                    self.finish(directory, path, None, f"file is larger than {MAX_FILE} bytes")
                    continue
                data = decode(path.read_bytes())
            except (OSError, ValueError) as exc:
                count = self.failures[path] = self.failures.get(path, 0) + 1
                if count >= 3:
                    self.failures.pop(path, None)
                    self.finish(directory, path, None, f"could not read the file 3 times: {exc}")
                continue
            self.failures.pop(path, None)
            self.finish(directory, path, data)

    def finish(self, directory, path, data, error=None):
        processed = directory / "processed"
        target = processed / path.name
        n = 0
        while target.exists() or target.with_name(target.stem + ".result.json").exists():
            n += 1
            target = processed / f"{path.stem}-{int(time.time())}{'-' + str(n) if n > 1 else ''}.json"
        try:
            # Claim the file first: a failed move (still locked by its writer) is retried next tick, never run twice.
            os.rename(path, target)
        except OSError as exc:
            self.failures[path] = self.failures.get(path, 0) + 1
            if self.failures[path] >= 3:
                self.problem(str(path), exc)
            return
        self.failures.pop(path, None)
        ok, result = False, None
        if error is None:
            try:
                result, ok = self.handle(data), True
            except BusError as exc:
                error = exc.payload.get("error")
                result = {k: v for k, v in exc.payload.items() if k != "error"} or None
            except (ValueError, TypeError, KeyError) as exc:
                error = str(exc)
            except Exception as exc:
                error = "internal error: " + type(exc).__name__
                LOG.exception("Mailbox message %s failed", path.name)
        report = {"ok": ok, "result": result, "processed_at": now_iso()} if ok else {"ok": False, "error": error, "processed_at": now_iso()}
        if not ok and result:
            report["details"] = result
        atomic_write(target.with_name(target.stem + ".result.json"), dump(report))
        self.status["processed"] = self.status.get("processed", 0) + 1

    def handle(self, data):
        kind = data.get("type")
        if kind not in TYPES:
            raise ValueError("type must be one of: " + ", ".join(TYPES))
        client = clean(data.get("id"), "id", 100)
        seen = self.app.store.get_state("mailbox_ids", {})
        if client and client in seen:
            return dict(seen[client], duplicate=True)
        me = self.agent_id
        self.bus.upsert(me, tool="claude-desktop-cloud", account="desktop", folder="studywithclaude")
        common = {k: data[k] for k in ("subject", "priority", "thread", "expects_reply", "ttl_seconds", "meta") if data.get(k) is not None}
        if kind in ("broadcast", "dm", "request", "reply"):
            to = {"broadcast": "all", "request": data.get("to") or "all"}.get(kind, data.get("to"))
            if kind == "dm" and not to:
                raise ValueError("a dm needs \"to\" (an agent id or name, @account or #folder)")
            if kind == "reply" and not data.get("reply_to"):
                raise ValueError("a reply needs \"reply_to\" (the message id from your inbox file)")
            result = self.bus.send(dict(common, **{"from": me, "to": to, "body": data.get("body"), "kind": kind,
                                                   "reply_to": data.get("reply_to")}))
        elif kind == "state":
            state = self.bus.set_state({"mode": data.get("mode"), "note": data.get("note"), "ttl_minutes": data.get("ttl_minutes"),
                                        "phone_min_priority": data.get("phone_min_priority"), "by": me})
            result = {"user_state": state_view(state)}
            if data.get("broadcast_body"):
                result["broadcast"] = self.bus.send({"from": me, "to": "all", "kind": "broadcast", "body": data.get("broadcast_body"),
                                                     "subject": data.get("subject")})
        elif kind == "activity":
            status = data.get("status") if data.get("status") in ("working", "idle") else None
            row = self.bus.upsert(me, activity=clean(data.get("activity"), "activity", 300, True), status=status)
            result = {"agent": agent_view(row)}
        elif kind == "notify":
            payload = {"message": data.get("message"), "title": data.get("title") or "Study Claude",
                       "priority": data.get("priority", 3), "source": me, "event": "message"}
            if client:
                payload["dedupe_key"] = f"{me}:{client}"
            result = self.app.notify(payload)
        else:
            result = {"pong": True, "agent_id": me, "user_state": state_view(self.bus.current_state()),
                      "agents": [self.short(a) for a in self.bus.agents() if a["state"] != "stale"]}
        if client and kind != "ping":
            seen[client] = result
            self.app.store.set_state("mailbox_ids", dict(list(seen.items())[-100:]))
        return result

    # ---- inbox + status
    @staticmethod
    def short(agent):
        return {k: agent[k] for k in ("id", "name", "tool", "account", "folder", "activity", "state", "age_seconds")}

    def drain(self):
        ready = [d for d in self.dirs if d in self.ready]
        if not ready or self.bus.quiet(self.agent_id):
            return
        with self.bus.lock_for(self.agent_id):
            fetched = self.bus.fetch(self.agent_id, 50)
            if fetched["messages"]:
                snapshot = state_view(self.bus.current_state())
                for message in fetched["messages"]:
                    stamp = datetime.fromtimestamp(message["ts"]).strftime("%Y%m%d-%H%M%S")
                    sender = re.sub(r"[^A-Za-z0-9_-]", "_", message["from"])[:40]
                    for directory in ready:
                        # Deterministic names: a retry after a partial failure overwrites instead of duplicating.
                        atomic_write(directory / "inbox" / f"{stamp}-{message['seq']}-{sender}.json", dump(dict(message, user_state=snapshot)))
                self.status["delivered"] = self.status.get("delivered", 0) + len(fetched["messages"])
                self.last_status = 0
            self.bus.mark(self.agent_id, fetched)

    def write_status(self, directory):
        now = time.time()
        state = self.bus.current_state()
        view = state_view(state, now)
        user_state = None if state["mode"] == "available" else {
            "mode": state["mode"], "label": state.get("label"), "note": state.get("note"), "directive": state.get("directive"),
            "phone_min_priority": state.get("phone_min_priority"), "expires_at_iso": view["expires_at_iso"],
            "remaining_seconds": view["remaining_seconds"]}
        count = lambda folder: sum(1 for p in (directory / folder).glob("*.json") if not p.name.startswith("."))
        payload = {"bridge": "running", "heartbeat_iso": now_iso(), "heartbeat_epoch": int(now), "agent_id": self.agent_id,
                   "user_state": user_state, "agents": [self.short(a) for a in self.bus.agents() if a["state"] != "stale"],
                   "inbox_files": count("inbox"), "outbox_pending": count("outbox"), "service": "local-notify"}
        atomic_write(directory / "status.json", dump(payload))

    def prune(self, directory):
        now = time.time()
        for folder, days in (("inbox", INBOX_DAYS), ("processed", PROCESSED_DAYS)):
            for path in (directory / folder).glob("*.json"):
                try:
                    if now - path.stat().st_mtime > days * 86400:
                        path.unlink()
                except OSError:
                    continue
