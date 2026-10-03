"""Agent Bus relay client: carries bus messages and Cameron's state between PCs through the planner server.

Each PC pushes the envelopes other PCs need and long-polls the ones meant for it, over outbound HTTPS only (the server
never reaches a PC). Guide: docs/RELAY.md. Wire contract: the planner repo's docs/BUS-RELAY.md (sections 3, 4, 7).
Stdlib only; three daemon threads (push, pull, directory) that never exit on errors.
"""
import http.client
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from bus_core import SERVER_ONLY, iso, loads, wire_message

LOG = logging.getLogger("notify")
STATE_FIELDS = ("mode", "label", "note", "directive", "phone_min_priority", "set_by", "set_at", "expires_at")
AGENT_CAPS = (("name", 60), ("tool", 40), ("account", 40), ("folder", 100), ("activity", 300), ("status", 20), ("state", 20))
MAX_BATCH, MAX_BYTES = 100, 400_000  # server limits: 100 envelopes, 512 KB per push
PUSH_TIMEOUT, PULL_TIMEOUT = 15, 45
UNAUTHORIZED_RETRY = 300
CURSOR_KEYS = ("relay_push_seq", "relay_pull_seq", "relay_state_version", "relay_state_pending", "relay_identity")


class RelayError(Exception):
    """status None = network trouble (no answer); otherwise the HTTP status the server answered."""

    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def server_origin(url):
    """scheme://host[:port] only: what status output may show (never a path or token)."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def wire_state(state):
    """Cameron's state in the relay wire format (docs/BUS-RELAY.md section 3)."""
    out = {key: state.get(key) for key in STATE_FIELDS}
    out["mode"] = state.get("mode") or "available"
    return out


def printable(value, limit):
    if not isinstance(value, str):
        return None
    value = "".join(c for c in value if c.isprintable() and not 0x202a <= ord(c) <= 0x202e and not 0x2066 <= ord(c) <= 0x2069)
    return value.strip()[:limit] or None


def disabled_status(cfg):
    url, _, machine, _ = config.relay_settings(cfg)
    return {"enabled": False, "status": "off", "machine": machine, "server": server_origin(url), "connected": False,
            "last_push_at": None, "last_pull_at": None, "last_directory_at": None, "last_error": None, "push_cursor": None,
            "pull_cursor": None, "state_version": None, "pending_state": None, "remote_machines": []}


class RelayClient:
    def __init__(self, app, backoff=(2.0, 60.0), directory_every=30.0, pull_wait=25, idle=2.0):
        self.app, self.bus, self.store = app, app.bus, app.store
        self.url, self.token, self.machine, _ = config.relay_settings(app.cfg)
        self.server = server_origin(self.url)
        self.backoff_min, self.backoff_max = backoff
        self.directory_every, self.pull_wait, self.idle = directory_every, pull_wait, idle
        self.stop_event = threading.Event()
        self.push_event = threading.Event()
        self.state_lock = threading.RLock()
        self.threads = []
        self.logged = {}
        self.seen_state_version = 0
        self.last_ok = self.last_push_at = self.last_pull_at = self.last_directory_at = None
        self.last_error, self.health_status = None, "starting"
        self.remote_machines = []
        loopback = urllib.parse.urlsplit(self.url).hostname in ("127.0.0.1", "localhost")
        # Loopback (tests, a second instance) must never go through a proxy; the real server honours system proxies.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if loopback else urllib.request.build_opener()
        self.bus.relay_machine = self.machine
        self.bus.on_local_state = self.on_local_state
        self.bus.listeners.append(self.push_event)
        self.reset_if_new_identity()

    # ---- lifecycle
    def start(self):
        for name, target in (("relay-push", self.push_loop), ("relay-pull", self.pull_loop),
                             ("relay-directory", self.directory_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)
        LOG.info("Relay client started: machine %s, server %s", self.machine, self.server)

    def close(self, timeout=2.0):
        """Stops the threads; a pull still waiting on the server is abandoned (daemon thread)."""
        self.stop_event.set()
        self.push_event.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if self.push_event in self.bus.listeners:
            self.bus.listeners.remove(self.push_event)

    # ---- small store helpers (cursors live in the store's state table)
    def get(self, key, default=None):
        return self.store.get_state(key, default)

    def put(self, key, value):
        if value is None:
            with self.store.connect() as db:
                db.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            self.store.set_state(key, value)

    def reset_if_new_identity(self):
        """Cursors belong to one server + machine; pairing elsewhere starts at the head again (no history replay)."""
        identity = f"{self.server}|{self.machine.lower()}"
        if self.get("relay_identity") != identity:
            for key in ("relay_pull_seq", "relay_state_version"):
                self.put(key, None)
            self.put("relay_push_seq", self.bus.latest)
            self.put("relay_identity", identity)
        elif not isinstance(self.get("relay_push_seq"), int):
            self.put("relay_push_seq", self.bus.latest)

    # ---- HTTP
    def call(self, method, path, payload=None, query=None, timeout=PUSH_TIMEOUT):
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": "Bearer " + self.token, "X-Planner": "1", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": "agent-bus-relay/1.0"}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(16 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(4000) or b"{}")
                detail = detail.get("error") if isinstance(detail, dict) else None
            except (ValueError, OSError):
                detail = None
            retry = exc.headers.get("Retry-After") if exc.headers else None
            raise RelayError(exc.code, f"HTTP {exc.code}" + (f" {str(detail)[:120]}" if detail else ""),
                             float(retry) if retry and retry.strip().isdigit() else None) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise RelayError(None, f"server not reachable ({type(reason).__name__ if not isinstance(reason, str) else reason})") from None
        try:
            answer = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise RelayError(502, "the server answered with something that is not JSON") from None
        if not isinstance(answer, dict):
            raise RelayError(502, "the server answered with an unexpected JSON value")
        return answer

    def succeeded(self):
        self.last_ok = time.time()
        self.health_status, self.last_error = "connected", None

    def failure(self, exc, failures):
        """Record a failed call; returns how long the thread waits before trying again."""
        self.last_error = str(exc)
        if exc.status in (401, 403):
            self.health_status = "unauthorized"
            self.warn("auth", "Relay: the server refused this PC's bus token (%s); pair again: python notify.py relay pair", exc)
            return UNAUTHORIZED_RETRY
        self.health_status = "offline" if exc.status is None else "error"
        self.warn(str(exc.status), "Relay: %s", exc)
        delay = min(self.backoff_max, self.backoff_min * 2 ** max(0, failures - 1))
        delay += random.uniform(0, delay / 4)
        if exc.retry_after:
            delay = max(delay, min(exc.retry_after, 300))
        return delay

    def warn(self, key, message, *args):
        text = message % args
        if time.time() - self.logged.get((key, text), 0) > 300:
            self.logged[(key, text)] = time.time()
            LOG.warning(message, *args)

    def connected(self):
        return bool(self.last_ok and time.time() - self.last_ok < 90 and self.health_status == "connected")

    # ---- push
    def on_local_state(self, state):
        """Bus.set_state hook (HTTP, CLI, MCP, mailbox); never called for expiry or relayed states."""
        with self.state_lock:
            self.put("relay_state_pending", wire_state(state))
        self.push_event.set()

    def push_loop(self):
        failures, limit = 0, MAX_BATCH
        while not self.stop_event.is_set():
            self.push_event.clear()
            try:
                result = self.push_once(limit)
            except RelayError as exc:
                if exc.status in (400, 413, 422):
                    limit = self.refused_batch(limit, exc)
                    self.stop_event.wait(self.backoff_min)
                    continue
                failures += 1
                self.stop_event.wait(self.failure(exc, failures))
                continue
            except Exception:
                LOG.exception("Relay push failed")
                self.stop_event.wait(5)
                continue
            failures, limit = 0, MAX_BATCH
            if result == "idle" and self.push_event.wait(self.idle) and not self.stop_event.is_set():
                self.stop_event.wait(0.1)  # let a burst of sends land in one batch

    def refused_batch(self, limit, exc):
        """A 4xx for a whole batch: halve it to find the bad envelope, then skip that one (or drop a refused state)."""
        if limit > 1:
            return max(1, limit // 2)
        attempt = getattr(self, "attempt", {}) or {}
        if attempt.get("seq") is not None:
            LOG.warning("Relay refused message seq %s (%s); skipping it", attempt["seq"], exc)
            self.put("relay_push_seq", attempt["seq"])
        elif attempt.get("state") is not None:
            LOG.warning("Relay refused the state change (%s); dropping it", exc)
            with self.state_lock:
                if self.get("relay_state_pending") == attempt["state"]:
                    self.put("relay_state_pending", None)
        return MAX_BATCH

    def envelope(self, row, agents, remote, now):
        if row["origin"] is not None or row["from_id"] == "bus" or row["kind"] == "system":
            return None
        if row["expires"] and row["expires"] <= now:
            return None
        forward, dest = self.bus.route(loads(row["to_spec"], []), agents, remote)
        if not forward:
            return None
        return {"id": f"{self.machine}/{row['id']}", "type": "message", "dest": dest, "message": wire_message(row)}

    def push_once(self, limit=MAX_BATCH):
        cursor = self.get("relay_push_seq")
        cursor = cursor if isinstance(cursor, int) else self.bus.latest
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE seq>? ORDER BY seq LIMIT ?", (cursor, limit)).fetchall()
        with self.state_lock:
            pending = self.get("relay_state_pending")
        now, agents, remote = time.time(), self.bus.agent_rows(), self.bus.remote_rows()
        envelopes, last, size, first = [], cursor, 0, None
        for row in rows:
            envelope = self.envelope(row, agents, remote, now)
            if envelope is not None:
                bytes_ = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
                if envelopes and size + bytes_ > MAX_BYTES:
                    break
                envelopes.append(envelope)
                size += bytes_
                first = row["seq"] if first is None else first
            last = row["seq"]
        if not envelopes and pending is None:
            if last != cursor:
                self.put("relay_push_seq", last)
            return "idle"
        body = {}
        if envelopes:
            body["envelopes"] = envelopes
        if pending is not None:
            body["state"] = pending
        self.attempt = {"seq": first, "state": pending}
        answer = self.call("POST", "/api/bus/relay/push", body)
        self.succeeded()
        for item in answer.get("rejected") or []:
            if isinstance(item, dict):
                self.warn("rejected", "Relay refused envelope %s: %s", item.get("id"), item.get("error"))
        self.put("relay_push_seq", last)
        if pending is not None:
            version = (answer.get("state") or {}).get("version") if isinstance(answer.get("state"), dict) else None
            with self.state_lock:
                if type(version) is int:
                    self.put("relay_state_version", max(version, int(self.get("relay_state_version", 0) or 0)))
                if self.get("relay_state_pending") == pending:
                    self.put("relay_state_pending", None)
        self.last_push_at = time.time()
        return "sent"

    # ---- pull
    def pull_loop(self):
        failures = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                changed = self.pull_once()
            except RelayError as exc:
                failures += 1
                self.stop_event.wait(self.failure(exc, failures))
                continue
            except Exception:
                LOG.exception("Relay pull failed")
                self.stop_event.wait(5)
                continue
            failures = 0
            if not changed and time.monotonic() - started < 1:
                self.stop_event.wait(1)  # a server that answers at once with nothing new must not make us spin

    def pull_once(self):
        cursor = self.get("relay_pull_seq")
        with self.state_lock:
            pending = self.get("relay_state_pending")
            applied = int(self.get("relay_state_version", 0) or 0)
        # While our own change waits to be pushed, tell the server what we have seen so the long-poll can wait.
        known = max(applied, self.seen_state_version) if pending is not None else applied
        query = {"wait": self.pull_wait, "state": known}
        if type(cursor) is int and cursor >= 0:
            query = {"after": cursor, **query}
        answer = self.call("GET", "/api/bus/relay/pull", query=query, timeout=PULL_TIMEOUT)
        self.succeeded()
        return self.handle_pull(answer)

    def handle_pull(self, answer):
        """Ingest pulled envelopes (duplicates are ignored), move the cursor, then apply the state if it is newer."""
        envelopes = answer.get("envelopes") if isinstance(answer.get("envelopes"), list) else []
        highest = None
        for envelope in envelopes:
            if not isinstance(envelope, dict):
                continue
            origin = envelope.get("origin")
            try:
                if not isinstance(origin, str) or not origin:
                    raise ValueError("no origin")
                if origin.lower() != self.machine.lower():
                    self.bus.ingest_remote(envelope.get("message"), origin)
            except ValueError as exc:
                self.warn("envelope", "Relay skipped envelope %s: %s", str(envelope.get("id"))[:200], exc)
            if type(envelope.get("seq")) is int:
                highest = max(highest or 0, envelope["seq"])
        latest = answer.get("latest")
        if answer.get("more") is True and highest is not None:
            self.put("relay_pull_seq", highest)
        elif type(latest) is int and latest >= 0:
            self.put("relay_pull_seq", latest)
        elif highest is not None:
            self.put("relay_pull_seq", highest)
        self.last_pull_at = time.time()
        return self.handle_state(answer.get("state")) or bool(envelopes)

    def handle_state(self, server):
        """Last writer wins by the server's global version; a local change still waiting to be pushed goes first."""
        if not isinstance(server, dict) or type(server.get("version")) is not int or server["version"] < 0:
            return False
        version = server["version"]
        # Same lock order as Bus.set_state (write_lock, then state_lock): a local change cannot interleave.
        with self.bus.write_lock, self.state_lock:
            self.seen_state_version = version
            if self.get("relay_state_pending") is not None:
                return False
            if version == 0:
                local = self.bus.current_state()
                if local["mode"] != "available":
                    self.put("relay_state_pending", wire_state(local))  # first connection: the server has no state yet
                    self.push_event.set()
                return False
            if version <= int(self.get("relay_state_version", 0) or 0):
                return False
            origin = server.get("origin")
            if not (isinstance(origin, str) and origin.lower() == self.machine.lower()):
                try:
                    self.bus.apply_remote_state(server.get("state"), origin)
                except ValueError as exc:
                    self.warn("state", "Relay skipped state version %s: %s", version, exc)
            self.put("relay_state_version", version)
        return True

    # ---- directory
    def directory_loop(self):
        failures = 0
        while not self.stop_event.is_set():
            try:
                self.sync_directory()
                failures, delay = 0, self.directory_every
            except RelayError as exc:
                failures += 1
                delay = max(self.failure(exc, failures), self.directory_every)
            except Exception:
                LOG.exception("Relay directory failed")
                delay = self.directory_every
            self.stop_event.wait(delay)

    def local_agents(self):
        agents = []
        for agent in self.bus.agents():
            if agent["state"] == "stale" or agent["id"] in SERVER_ONLY:
                continue
            entry = {"id": agent["id"]}
            entry.update({key: printable(agent.get(key), limit) for key, limit in AGENT_CAPS})
            entry.update(last_seen=agent.get("last_seen"), started=agent.get("started"))
            agents.append(entry)
        return agents[:100]

    def sync_directory(self):
        answer = self.call("POST", "/api/bus/relay/agents", {"agents": self.local_agents()})
        self.succeeded()
        machines = answer.get("machines") if isinstance(answer.get("machines"), list) else []
        self.bus.set_remote_agents(machines)
        mine = self.machine.lower()
        self.remote_machines = [{"id": m["id"], "online": m.get("online") is True,
                                 "agents": len(m["agents"]) if isinstance(m.get("agents"), list) else 0}
                                for m in machines if isinstance(m, dict) and isinstance(m.get("id"), str)
                                and m["id"].lower() != mine]
        self.last_directory_at = time.time()

    # ---- health
    def status(self):
        with self.state_lock:
            pending = self.get("relay_state_pending")
            version = self.get("relay_state_version")
        connected = self.connected()
        status = self.health_status if not (self.health_status == "connected" and not connected) else "connecting"
        return {"enabled": True, "status": status, "machine": self.machine, "server": self.server, "connected": connected,
                "last_push_at": iso(self.last_push_at), "last_pull_at": iso(self.last_pull_at),
                "last_directory_at": iso(self.last_directory_at), "last_error": self.last_error,
                "push_cursor": self.get("relay_push_seq"), "pull_cursor": self.get("relay_pull_seq"),
                "state_version": version, "pending_state": pending.get("mode") if isinstance(pending, dict) else None,
                "remote_machines": list(self.remote_machines)}
