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
                  ops {rid, action, args, nonce, ts} a remote-ops action from the fixed allowlist (remote_ops.py,
                                                     docs/REMOTE-OPS.md), forwarded to the AgentBusOps helper's pipe
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
from pathlib import Path

import bus_console
import remote_ops
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
        data_dir = Path(self.app.store.path).parent
        self.ops_call = remote_ops.call  # the AgentBusOps helper's named pipe (tests swap it)
        self.ops_nonces = OpsNonces(data_dir / "remote-ops-nonces.json")
        self.ops_log_path = data_dir / "remote-ops.log"

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
        if not origin and m.get("from") == "bus":
            return  # this PC's own system notices (state changes, remote-ops lines) reach every agent, us included
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
        elif kind == "ops":
            # Off the receive loop: a service restart can take a minute and console frames must keep flowing.
            threading.Thread(target=self.answer, args=(origin, p, lambda: self.safe_ops(origin, p)), daemon=True,
                             name="remote-ops").start()
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

    # ---- remote ops (docs/REMOTE-OPS.md): a fixed allowlist, never a shell
    def note(self, line, outcome):
        """Log one remote-ops event here: data/remote-ops.log, the service log, and a bus line (not injected)."""
        text = f"remote ops: {line}: {outcome}"
        LOG.info(text)
        try:
            with open(self.ops_log_path, "a", encoding="utf-8") as handle:
                handle.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + text + "\n")
        except OSError:
            LOG.warning("remote ops: could not write %s", self.ops_log_path)
        try:
            self.bus.send({"from": "bus", "to": ["all"], "kind": "system", "subject": "Remote ops", "body": text[:3900],
                           "meta": {"noinject": True, "remote_ops": True}}, internal=True)
        except Exception:
            LOG.exception("remote ops: could not post the bus line")

    def safe_ops(self, origin, p):
        try:
            return self.serve_ops(origin, p)
        except (BusError, ValueError):
            raise
        except Exception as exc:
            LOG.exception("remote ops: request from %s failed", origin)
            raise BusError(500, f"{type(exc).__name__} on {self.machine}; see its service.log") from None

    def serve_ops(self, origin, p):
        """Host side: check freshness and the one-time nonce, validate, forward to the AgentBusOps helper's pipe."""
        try:
            action, args = remote_ops.validate(p.get("action"), p.get("args"))
        except ValueError as exc:
            self.note(f"{origin} -> {self.machine} {str(p.get('action'))[:40]}", f"refused: {exc}")
            raise
        line = f"{origin} -> {self.machine} {remote_ops.summary(action, args)}"
        nonce, ts = p.get("nonce"), p.get("ts")
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce) or type(ts) not in (int, float) \
                or abs(time.time() - ts) > OPS_FRESH:
            self.note(line, "refused: stale or missing nonce")
            raise BusError(400, f"stale request (older than {OPS_FRESH} s, or the PCs' clocks differ)")
        if not self.ops_nonces.use(nonce):
            self.note(line, "refused: replayed request")
            raise BusError(409, "replayed request")
        try:
            answer = self.ops_call({"action": action, "args": args, "origin": origin},
                                   timeout=remote_ops.TIMEOUTS[action] - 10)
        except remote_ops.OpsError as exc:
            message = str(exc)
            if exc.status == 503:
                message = f"remote ops not installed on {self.machine}: run SETUP-HEADLESS / Install-RemoteOps.ps1 there"
            self.note(line, f"failed: {message}")
            raise BusError(exc.status, message) from None
        if not answer.get("ok"):
            error = str(answer.get("error") or "the helper refused")[:500]
            self.note(line, f"refused: {error}")
            raise BusError(answer.get("status") or 502, error)
        result = answer.get("result") if isinstance(answer.get("result"), dict) else {}
        if action == "status":
            result["bus"] = {"machine": self.machine, "runtime": getattr(self.app, "runtime", None), "head": git_head()}
        self.note(line, "ok")
        return result

    def ops(self, machine, action, args=None):
        """Viewer side (`bus.py remote <machine> <action>`): run one allowlisted action on another PC."""
        action, args = remote_ops.validate(action, args)
        peer = self.peer(machine)
        if not peer:
            raise BusError(409, f"{machine} is not reachable for remote ops: it needs the relay paired and the same "
                                "console key (python notify.py console key), and its service running.")
        machine = peer["machine"]
        line = f"{self.machine} -> {machine} {remote_ops.summary(action, args)}"
        self.note(line, "sent")
        wait = remote_ops.TIMEOUTS[action]
        answer = self.request(machine, {"t": "ops", "action": action, "args": args, "nonce": uuid.uuid4().hex,
                                        "ts": time.time()}, timeout=wait)
        if answer is None:
            self.note(line, f"no answer within {wait} s")
            raise BusError(504, f"{machine} did not answer within {wait} s (its service may be offline or busy)")
        if not answer.get("ok"):
            error = str(answer.get("error") or "refused")[:500]
            self.note(line, f"refused: {error}")
            raise BusError(answer.get("status") or 502, f"{machine}: {error}")
        self.note(line, "ok")
        return {"machine": machine, "action": action, "args": args, "result": answer.get("result")}


OPS_FRESH = 120  # seconds an ops request may be old; tighter than console frames because these act on the PC


class OpsNonces:
    """One-time request ids for remote ops, kept on disk so a captured frame cannot be replayed after a service restart
    (the in-memory frame `seen` set alone would forget it). Entries live 2 x OPS_FRESH."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def use(self, nonce):
        with self.lock:
            now = time.time()
            try:
                seen = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(seen, dict):
                    seen = {}
            except (OSError, ValueError):
                seen = {}
            seen = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and now - v < OPS_FRESH * 2}
            if nonce in seen:
                return False
            seen[nonce] = now
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(seen), encoding="utf-8")
            os.replace(tmp, self.path)
            return True


def git_head():
    """This checkout's commit (short sha) read from .git without running git; None when unknown."""
    try:
        git = Path(__file__).resolve().parent / ".git"
        if git.is_file():  # a worktree: "gitdir: <path>"
            git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_file = git / ref
            if not ref_file.exists():
                common = git / "commondir"
                base = (git / common.read_text(encoding="utf-8").strip()) if common.exists() else git
                ref_file = base / ref
                if not ref_file.exists():
                    packed = (base / "packed-refs").read_text(encoding="utf-8")
                    return next((l.split()[0][:12] for l in packed.splitlines() if l.endswith(" " + ref)), None)
            return ref_file.read_text(encoding="utf-8").strip()[:12]
        return head[:12]
    except (OSError, IndexError, ValueError):
        return None


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
