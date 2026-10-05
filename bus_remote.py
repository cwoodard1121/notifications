"""Agent Console across PCs: watch and prompt sessions on Cameron's other PC through the planner relay, end to end
encrypted with the key both PCs derive from his SSH key (bus_crypto.py).

Each PC with console.key set joins the bus as a pseudo-agent `console-<machine>`. Console traffic is ordinary relayed
DMs between those pseudo-agents ({"noinject": true, "rc": 1}, 15-minute expiry), whose body is a sealed frame: the
relay server stores and forwards ciphertext only, and a frame that does not authenticate under the shared key (or
names the wrong sender/recipient PC, or is stale or replayed) is dropped.

Messages (JSON, zlib, split into frames of <= FRAME_MAX bytes):
  viewer -> host  watch {agent, after|null, lease}   start/renew a lease; the host answers with events (reset=true
                                                     for a snapshot) and keeps pushing new events until it lapses
                  page {rid, agent, before}          older events ("Load earlier")
                  prompt {rid, agent, text}          a console prompt (the host sends it locally as Cameron's)
  host -> viewer  events {agent, reset, events, cursor, start, earlier, pending, state, reachable, viewable, error}
                  answer {rid, ok, result|error}
"""
import json
import logging
import os
import re
import threading
import time
import uuid
import zlib

import bus_console
from bus_core import BusError
from bus_crypto import CryptoError, decode_key, open_, seal

LOG = logging.getLogger("notify")
FRAME_MAX = 2600  # compressed bytes per message: sealed + base64 stays under the bus's 4000-character body limit
TTL = 900
LEASE = 90
FRESH = 900  # seconds a frame may be old (also covers clock skew between the PCs)
REMOTE_EVENTS, REMOTE_RESULT, REMOTE_TEXT, REMOTE_INPUT = 150, 1500, 8000, 600
MIRROR_MAX = 3000


def pseudo_id(machine):
    return "console-" + re.sub(r"[^a-z0-9_.-]", "-", str(machine).lower())[:60]


def offset(event):
    return int(str(event.get("id", "0")).split(".")[0] or 0)


def slim(events, limit=None):
    """Events trimmed for the relay: long tool output and inputs are cut (the host PC still has them in full)."""
    out = []
    for e in events[-limit:] if limit else events:
        e = dict(e)
        if e.get("k") == "result":
            e["text"] = bus_console.clip(e.get("text"), REMOTE_RESULT)
        elif e.get("k") in ("assistant", "user", "console", "system"):
            e["text"] = bus_console.clip(e.get("text"), REMOTE_TEXT)
        if e.get("input"):
            e["input"] = bus_console.clip(e["input"], REMOTE_INPUT)
        out.append(e)
    return out


class Mirror:
    """What this PC (the viewer) knows about one session on another PC."""

    def __init__(self, agent, machine):
        self.agent, self.machine = agent, machine
        self.events, self.cursor, self.start, self.earlier = [], None, None, False
        self.status = {"pending": [], "state": None, "reachable": False, "viewable": False}
        self.error, self.version, self.lease_until, self.ready = None, 0, 0.0, False


class RemoteConsole:
    def __init__(self, console, key):
        self.console, self.bus, self.app = console, console.bus, console.app
        self.key = decode_key(key)
        self.machine = self.bus.relay_machine
        self.me = pseudo_id(self.machine)
        self.cond = threading.Condition()
        self.stop_event = threading.Event()
        self.mirrors, self.answers, self.parts, self.seen = {}, {}, {}, {}
        self.leases = {}  # (viewer machine, agent) -> {"until", "cursor", "thread"}
        self.lease_lock = threading.Lock()
        self.thread = None
        self.dropped = 0

    # ---- lifecycle
    def start(self):
        self.bus.upsert(self.me, tool="other", name=self.me, account="console", folder=None, status="idle",
                        activity="Agent Console relay (end-to-end encrypted)")
        self.thread = threading.Thread(target=self.receive_loop, name="console-remote", daemon=True)
        self.thread.start()
        LOG.info("Agent Console relay on as %s", self.me)

    def close(self):
        self.stop_event.set()
        with self.cond:
            self.cond.notify_all()

    def health(self):
        return {"enabled": True, "id": self.me, "dropped_frames": self.dropped,
                "watching": sorted(f"{m.machine}:{m.agent}" for m in self.mirrors.values() if m.lease_until > time.time()),
                "serving": sorted(f"{v}:{a}" for (v, a), lease in self.leases.items() if lease["until"] > time.time())}

    def peer(self, machine):
        """The other PC's console pseudo-agent when it is in the relay directory (it has console.key set)."""
        want = pseudo_id(machine)
        return next((r for r in self.bus.remote_rows() if r["id"] == want and r["machine"].lower() == machine.lower()), None)

    # ---- wire
    def aad(self, sender, recipient):
        return f"agent-bus-console|{sender.lower()}>{recipient.lower()}".encode("utf-8")

    def send(self, machine, payload):
        data = zlib.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
        chunks = [data[i:i + FRAME_MAX] for i in range(0, len(data), FRAME_MAX)] or [b""]
        frame_id = uuid.uuid4().hex[:16]
        for index, chunk in enumerate(chunks):
            head = json.dumps({"f": frame_id, "i": index, "n": len(chunks), "ts": time.time()}).encode("ascii")
            body = seal(self.key, head + b"\n" + chunk, self.aad(self.machine, machine))
            self.bus.send({"from": self.me, "to": [pseudo_id(machine)], "kind": "dm", "body": body, "ttl_seconds": TTL,
                           "meta": {"noinject": True, "rc": 1}}, internal=True)

    def receive_loop(self):
        while not self.stop_event.is_set():
            try:
                result = self.bus.wait(self.me, timeout=25, limit=100)
                for message in result.get("messages") or []:
                    self.receive(message)
                self.expire()
            except Exception:
                LOG.exception("Agent Console relay: receive failed")
                self.stop_event.wait(5)

    def receive(self, m):
        origin = m.get("origin")
        try:
            if not origin or origin.lower() == self.machine.lower() or m.get("from") != pseudo_id(origin):
                raise CryptoError("not from another PC's console")
            raw = open_(self.key, m.get("body") or "", self.aad(origin, self.machine))
            head, _, chunk = raw.partition(b"\n")
            head = json.loads(head)
            frame_id, index, count, ts = head["f"], int(head["i"]), int(head["n"]), float(head["ts"])
            if abs(time.time() - ts) > FRESH or not 0 <= index < count <= 500:
                raise CryptoError("stale or malformed frame")
            if (frame_id, index) in self.seen:
                raise CryptoError("replayed frame")
            self.seen[(frame_id, index)] = time.time()
        except (CryptoError, ValueError, KeyError, TypeError) as exc:
            self.dropped += 1
            LOG.warning("Agent Console relay: dropped message %s from %s: %s", m.get("id"), origin, exc)
            return
        finally:
            self.forget(m)
        entry = self.parts.setdefault(frame_id, {"n": count, "got": {}, "ts": time.time()})
        entry["got"][index] = chunk
        if len(entry["got"]) < entry["n"]:
            return
        del self.parts[frame_id]
        try:
            payload = json.loads(zlib.decompress(b"".join(entry["got"][i] for i in range(entry["n"]))))
            self.dispatch(origin, payload)
        except Exception:
            LOG.exception("Agent Console relay: could not handle a request from %s", origin)

    def forget(self, m):
        """Relayed console frames are consumed once: drop them from this PC's message table right away."""
        try:
            with self.bus.store.connect() as db:
                db.execute("DELETE FROM deliveries WHERE seq=?", (m["seq"],))
                db.execute("DELETE FROM messages WHERE seq=? AND origin IS NOT NULL", (m["seq"],))
        except Exception:
            pass

    def expire(self):
        now = time.time()
        for key in [k for k, t in self.seen.items() if now - t > FRESH * 2]:
            self.seen.pop(key, None)
        for key in [k for k, v in self.parts.items() if now - v["ts"] > FRESH]:
            self.parts.pop(key, None)

    def dispatch(self, origin, p):
        kind = p.get("t")
        if kind == "watch":
            self.serve_watch(origin, p)
        elif kind == "page":
            self.answer(origin, p, lambda: self.page_result(p))
        elif kind == "prompt":
            self.answer(origin, p, lambda: self.console.prompt({"agent": p.get("agent"), "text": p.get("text")}, local_only=True))
        elif kind == "events":
            self.take_events(origin, p)
        elif kind == "answer":
            with self.cond:
                if p.get("rid") in self.answers:
                    self.answers[p["rid"]] = p
                    self.cond.notify_all()

    # ---- host side: serve this PC's sessions to the other PC
    def answer(self, origin, p, work):
        try:
            reply = {"t": "answer", "rid": p.get("rid"), "ok": True, "result": work()}
        except BusError as exc:
            reply = {"t": "answer", "rid": p.get("rid"), "ok": False, "error": str(exc), "status": exc.status}
        except ValueError as exc:
            reply = {"t": "answer", "rid": p.get("rid"), "ok": False, "error": str(exc), "status": 400}
        self.send(origin, reply)

    def local(self, agent_id):
        view = self.console.agent(agent_id)
        return view, self.console.transcripts.locate(view)

    def page_result(self, p):
        view, path = self.local(p.get("agent"))
        if not path:
            raise BusError(404, "No transcript for this session")
        result = bus_console.read(path, None, int(p.get("before") or 0))
        return {"events": slim(result["events"], REMOTE_EVENTS), "start": result["start"], "earlier": result["earlier"]}

    def snapshot(self, agent_id):
        try:
            view, path = self.local(agent_id)
        except BusError as exc:
            return {"t": "events", "agent": agent_id, "reset": True, "events": [], "error": str(exc)}, None, None
        status = self.console.status(view)
        base = {"t": "events", "agent": agent_id, "reset": True, "state": view["state"],
                "pending": self.console.pending(view["id"]), **status}
        if not path:
            return dict(base, events=[], error="No transcript for this session yet (it appears after its next prompt "
                                                "or tool call)."), None, None
        try:
            result = bus_console.read(path)
        except PermissionError:
            return dict(base, events=[], error="The service on that PC cannot read transcripts yet: run "
                                               "Grant-ConsoleAccess.ps1 there."), None, None
        return dict(base, events=slim(result["events"], REMOTE_EVENTS), cursor=result["cursor"], start=result["start"],
                    earlier=result["earlier"]), path, result["cursor"]

    def serve_watch(self, viewer, p):
        agent_id = str(p.get("agent") or "")
        lease = max(10, min(float(p.get("lease") or LEASE), 300))
        key = (viewer.lower(), agent_id)
        with self.lease_lock:
            current = self.leases.get(key)
            running = current is not None and current["thread"].is_alive()
            if running and p.get("after") is not None:
                current["until"] = time.time() + lease  # a renewal: the running watcher keeps its own cursor
                return
        payload, path, cursor = self.snapshot(agent_id) if p.get("after") is None else (None, None, None)
        if payload is not None:
            self.send(viewer, payload)
            if path is None:
                return
        else:
            try:
                view, path = self.local(agent_id)
            except BusError:
                return
            cursor = int(p["after"])
            if not path:
                return
        with self.lease_lock:
            current = self.leases.get(key)
            if current is not None and current["thread"].is_alive():
                current.update(until=time.time() + lease, cursor=cursor)
                return
            entry = {"until": time.time() + lease, "cursor": cursor, "path": path}
            entry["thread"] = threading.Thread(target=self.watch_loop, args=(viewer, agent_id, entry), daemon=True,
                                               name=f"console-watch-{agent_id}")
            self.leases[key] = entry
            entry["thread"].start()

    def watch_loop(self, viewer, agent_id, lease):
        try:
            self.watching(viewer, agent_id, lease)
        except Exception:
            if not self.stop_event.is_set():
                LOG.exception("Agent Console relay: watching %s for %s failed", agent_id, viewer)

    def watching(self, viewer, agent_id, lease):
        last_status = None
        while not self.stop_event.is_set() and time.time() < lease["until"]:
            grew = bus_console.wait_for_growth(lease["path"], lease["cursor"], 10)
            try:
                view = self.console.agent(agent_id)
            except BusError:
                return
            status = dict(self.console.status(view), state=view["state"], pending=self.console.pending(agent_id))
            events, cursor = [], lease["cursor"]
            if grew:
                time.sleep(0.8)  # let a burst of transcript lines land in one frame
                try:
                    result = bus_console.read(lease["path"], after=lease["cursor"])
                except OSError:
                    return
                events, cursor = slim(result["events"]), result["cursor"]
            if events or status != last_status:
                self.send(viewer, {"t": "events", "agent": agent_id, "reset": False, "events": events, "cursor": cursor,
                                   **status})
                last_status, lease["cursor"] = status, cursor

    # ---- viewer side: watch and prompt sessions on the other PC
    def take_events(self, origin, p):
        agent_id = str(p.get("agent") or "")
        with self.cond:
            mirror = self.mirrors.get(agent_id)
            if mirror is None or mirror.machine.lower() != origin.lower():
                return  # nobody here asked for it
            events = [e for e in p.get("events") or [] if isinstance(e, dict) and "id" in e]
            if p.get("reset"):
                mirror.events, mirror.start, mirror.earlier = events, p.get("start"), bool(p.get("earlier"))
                mirror.error = p.get("error")
            else:
                known = {e["id"] for e in mirror.events[-500:]}
                mirror.events.extend(e for e in events if e["id"] not in known)
                mirror.events = mirror.events[-MIRROR_MAX:]
            if p.get("cursor") is not None:
                mirror.cursor = int(p["cursor"])
            for key in ("pending", "state", "reachable", "viewable"):
                if key in p:
                    mirror.status[key] = p[key]
            mirror.version += 1
            mirror.ready = True
            self.cond.notify_all()

    def request(self, machine, payload, timeout=20):
        rid = uuid.uuid4().hex[:12]
        with self.cond:
            self.answers[rid] = None
        try:
            self.send(machine, dict(payload, rid=rid))
            deadline = time.monotonic() + timeout
            with self.cond:
                while self.answers.get(rid) is None and not self.stop_event.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self.cond.wait(remaining)
                return self.answers.get(rid)
        finally:
            with self.cond:
                self.answers.pop(rid, None)

    def mirror(self, agent_id, machine):
        with self.cond:
            mirror = self.mirrors.get(agent_id)
            if mirror is None or mirror.machine != machine:
                mirror = self.mirrors[agent_id] = Mirror(agent_id, machine)
            return mirror

    def watch(self, mirror, fresh):
        mirror.lease_until = time.time() + LEASE
        self.send(mirror.machine, {"t": "watch", "agent": mirror.agent, "lease": LEASE,
                                   "after": None if fresh else mirror.cursor})

    def view(self, mirror, row, events):
        return {"events": events, "cursor": mirror.cursor if mirror.cursor is not None else 0, "start": mirror.start,
                "earlier": mirror.earlier, "more": False, "remote": True, "machine": mirror.machine,
                "agent": {"id": row["id"], "name": row["name"], "account": row["account"], "folder": row["folder"],
                          "cwd": None, "state": mirror.status.get("state") or row["state"], "activity": row["activity"]},
                "pending": mirror.status.get("pending") or [], "reachable": bool(mirror.status.get("reachable")),
                "viewable": bool(mirror.status.get("viewable"))}

    def events(self, row, after=None, before=None, wait=0, alive=None):
        machine = row["machine"]
        if not self.peer(machine):
            raise BusError(409, f"{machine} has no Agent Console key yet: run `python notify.py console key` there "
                                "(same SSH key as here), then restart its service.")
        mirror = self.mirror(row["id"], machine)
        if before is not None:
            answer = self.request(machine, {"t": "page", "agent": row["id"], "before": before})
            if answer is None:
                raise BusError(504, f"{machine} did not answer in time")
            if not answer.get("ok"):
                raise BusError(answer.get("status") or 502, answer.get("error") or "the other PC refused")
            result = answer["result"]
            return dict(self.view(mirror, row, result["events"]), start=result["start"], earlier=result["earlier"])
        now = time.time()
        if after is None or mirror.cursor is None or mirror.lease_until < now:
            with self.cond:
                seen = mirror.version
            self.watch(mirror, fresh=True)
            deadline = time.monotonic() + 20
            with self.cond:
                while mirror.version == seen and time.monotonic() < deadline and (alive is None or alive()):
                    self.cond.wait(min(1.0, max(0.01, deadline - time.monotonic())))
            if mirror.version == seen:
                raise BusError(504, f"{machine} did not answer: is its service running and paired?")
            if mirror.error and not mirror.events:
                raise BusError(404, f"{mirror.error}", pending=mirror.status.get("pending") or [],
                               reachable=bool(mirror.status.get("reachable")))
            return self.view(mirror, row, list(mirror.events))
        if mirror.lease_until - now < LEASE / 2:
            self.watch(mirror, fresh=False)
        deadline = time.monotonic() + min(max(wait, 0), 55)
        with self.cond:
            seen = mirror.version
            while (mirror.version == seen and time.monotonic() < deadline and not self.stop_event.is_set()
                   and (alive is None or alive())):
                self.cond.wait(min(1.0, max(0.01, deadline - time.monotonic())))
            fresh = [e for e in mirror.events if offset(e) >= after]
        return self.view(mirror, row, fresh)

    def prompt(self, row, text):
        machine = row["machine"]
        if not self.peer(machine):
            raise BusError(409, f"{machine} has no Agent Console key yet: run `python notify.py console key` there.")
        answer = self.request(machine, {"t": "prompt", "agent": row["id"], "text": text}, timeout=25)
        if answer is None:
            return {"id": None, "remote": True, "machine": machine, "reachable": False,
                    "note": f"sent to {machine}, but it has not confirmed yet (its service may be busy or offline)"}
        if not answer.get("ok"):
            raise BusError(answer.get("status") or 502, answer.get("error") or "the other PC refused the prompt")
        return dict(answer["result"], remote=True, machine=machine)

    def status(self, row):
        mirror = self.mirrors.get(row["id"])
        known = mirror.status if mirror is not None and mirror.ready else {}
        return {"viewable": bool(self.peer(row["machine"])) and row.get("machine_online", True),
                "reachable": bool(known.get("reachable")) or row["state"] == "working"}


def from_config(console):
    """A RemoteConsole when the relay is paired and console.key is set; None otherwise (or with a bad key)."""
    settings = console.app.cfg.get("console") if isinstance(console.app.cfg.get("console"), dict) else {}
    key = settings.get("key") or os.environ.get("AGENT_CONSOLE_KEY") or ""
    if not key or not console.bus.relay_machine:
        return None
    try:
        return RemoteConsole(console, key)
    except CryptoError as exc:
        LOG.warning("Agent Console relay off: %s", exc)
        return None
