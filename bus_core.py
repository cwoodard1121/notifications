"""Agent Bus core: roster, messages, claims and the service-enforced user state.

Shares the notifications SQLite file with Store (one connection per call, WAL). Only the service process writes, so
small in-memory caches (latest seq, per-agent "nothing new" marks, the user state) stay coherent and keep hooks cheap.
"""
import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime

from bus_ident import guess_tool

LOG = logging.getLogger("notify")
SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT, session_id TEXT, tool TEXT, account TEXT, cwd TEXT, folder TEXT,
    activity TEXT, status TEXT, meta TEXT, started REAL, last_seen REAL, ended REAL,
    cursor INTEGER DEFAULT 0, state_seen INTEGER DEFAULT 0, wakes TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, ts REAL, from_id TEXT, from_name TEXT, to_spec TEXT,
    kind TEXT, subject TEXT, body TEXT, priority INTEGER, thread TEXT, reply_to TEXT, expects_reply INTEGER,
    meta TEXT, expires REAL, claimed_by TEXT
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS deliveries (seq INTEGER, agent_id TEXT, ts REAL, PRIMARY KEY(seq, agent_id));
CREATE TABLE IF NOT EXISTS remote_agents (machine TEXT, id TEXT, name TEXT, tool TEXT, account TEXT, folder TEXT,
    activity TEXT, status TEXT, state TEXT, last_seen REAL, started REAL, agents_at REAL, online INTEGER,
    PRIMARY KEY (machine, id));
"""
KINDS = ("dm", "broadcast", "request", "reply", "system")
TOOLS = ("claude-code", "codex", "claude-desktop", "claude-desktop-cloud", "other")
# "user" = Cameron on the bus web UI of this PC; "planner" = Cameron on the planner's Agents page (relayed by the server).
SYSTEM_NAMES = {"bus": "Agent Bus", "user": "Cameron (bus UI)", "planner": "Cameron (planner)"}
CAMERON = ("user", "planner")
SERVER_ONLY = ("planner", "planner-app")  # relay targets that live on the planner server, not on a PC
REMOTE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,99}")
REMOTE_REF_RE = re.compile(r"[A-Za-z0-9_.:@-]{1,100}")
REMOTE_TTL = 48 * 3600
FIELDS = ("name", "session_id", "tool", "account", "cwd", "folder", "activity", "status")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,79}")
MODE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
WORKING_AGE, IDLE_AGE, AGENT_TTL, MESSAGE_TTL = 900, 7200, 48 * 3600, 14 * 86400
QUEUE = ("If you have questions for him, queue them (keep a running list and ask when he is back) and keep working on "
         "anything that does not need him. Do not block waiting for him.")
AVAILABLE = {"mode": "available", "label": "Available", "note": "", "directive": "", "phone_min_priority": 1,
             "set_by": None, "set_at": None, "expires_at": None, "version": 0}
RANK = {"working": 0, "idle": 1, "stale": 2, "ended": 3}


class BusError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = dict(extra, error=message)


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


def clean(value, name, maximum, required=False):
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} must be a string of at most {maximum} characters")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} must not be empty")
    return value or None


def integer(value, name, low, high, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,13}", value.strip()):
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def number(value, name, low, high):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ValueError(f"{name} must be a number") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a number from {low:g} to {high:g}")
    return float(value)


def loads(value, default):
    try:
        result = json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
    return result if isinstance(result, type(default)) else default


def agent_state(row, now=None):
    if row["ended"]:
        return "ended"
    age = (now or time.time()) - (row["last_seen"] or 0)
    if row["status"] == "working" and age < WORKING_AGE:
        return "working"
    return "idle" if age < IDLE_AGE else "stale"


def agent_view(row, now=None):
    now = now or time.time()
    return {"id": row["id"], "name": row["name"], "tool": row["tool"], "account": row["account"], "folder": row["folder"],
            "cwd": row["cwd"], "activity": row["activity"], "status": row["status"], "state": agent_state(row, now),
            "session_id": row["session_id"], "meta": loads(row["meta"], {}), "started": row["started"],
            "started_iso": iso(row["started"]), "last_seen": row["last_seen"] or None, "last_seen_iso": iso(row["last_seen"]),
            "age_seconds": round(now - row["last_seen"]) if row["last_seen"] else None,
            "ended": row["ended"], "ended_iso": iso(row["ended"])}


def message_view(row):
    return {"seq": row["seq"], "id": row["id"], "ts": row["ts"], "iso": iso(row["ts"]), "from": row["from_id"],
            "from_name": row["from_name"], "to": loads(row["to_spec"], []), "kind": row["kind"], "subject": row["subject"],
            "body": row["body"], "priority": row["priority"], "thread": row["thread"], "reply_to": row["reply_to"],
            "expects_reply": bool(row["expects_reply"]), "meta": loads(row["meta"], {}), "claimed_by": row["claimed_by"],
            "origin": row["origin"] if "origin" in row.keys() else None}


def wire_message(row):
    """A local message row in the relay wire format (docs/RELAY.md): message_view without seq/iso/claim/origin."""
    view = message_view(row)
    return {k: view[k] for k in ("id", "ts", "from", "from_name", "to", "kind", "subject", "body", "priority", "thread",
                                 "reply_to", "expects_reply", "meta")} | {"expires": row["expires"]}


def remote_view(row, now=None):
    """A remote_agents row shaped like agent_view, plus machine/remote; offline machines show their agents offline."""
    now = now or time.time()
    online = bool(row["online"])
    state = row["state"] if online and row["state"] in ("working", "idle", "stale") else "stale" if online else "offline"
    return {"id": row["id"], "name": row["name"], "tool": row["tool"], "account": row["account"], "folder": row["folder"],
            "cwd": None, "activity": row["activity"], "status": row["status"], "state": state, "session_id": None, "meta": {},
            "started": row["started"], "started_iso": iso(row["started"]), "last_seen": row["last_seen"] or None,
            "last_seen_iso": iso(row["last_seen"]), "age_seconds": round(now - row["last_seen"]) if row["last_seen"] else None,
            "ended": None, "ended_iso": None, "machine": row["machine"], "remote": True, "machine_online": online,
            "agents_at": row["agents_at"]}


def state_view(state, now=None):
    now = now or time.time()
    expires = state.get("expires_at")
    return dict(state, remaining_seconds=max(0, round(expires - now)) if expires else None, expires_at_iso=iso(expires),
                set_at_iso=iso(state.get("set_at")), until=clock(expires) if expires else None)


def noinject(m):
    return bool(loads(m["meta"], {}).get("noinject")) if isinstance(m["meta"], str) else bool((m["meta"] or {}).get("noinject"))


def console_prompt(m):
    """A prompt Cameron typed in the Agent Console (bus_console.py); only Bus.send(internal=True) can create one."""
    meta = m["meta"]
    meta = loads(meta, {}) if isinstance(meta, str) else meta or {}
    return meta.get("console") == "prompt"


def wake_worthy(m):
    """Worth interrupting an idle agent (Stop-block or idle rewake): a targeted message that needs an answer or is urgent,
    or a console prompt."""
    return m["kind"] in ("dm", "request", "reply") and (bool(m["expects_reply"]) or (m["priority"] or 0) >= 4
                                                        or console_prompt(m))


def is_group(token):
    low = token.lower()
    return low in ("all", "*") or low[:1] in ("@", "#") or low.startswith("tool:")


def matches(spec, agent):
    """Evaluated at inbox time against the stored to_spec, so offline ids receive messages when they reappear."""
    for token in spec:
        low = str(token).lower()
        if low in ("all", "*"):
            return True
        if low[:1] == "@":
            hit = (agent["account"] or "").lower() == low[1:]
        elif low[:1] == "#":
            hit = (agent["folder"] or "").lower() == low[1:]
        elif low.startswith("tool:"):
            hit = (agent["tool"] or "").lower() == low[5:]
        else:
            hit = low == agent["id"].lower() or low == (agent["name"] or "").lower()
        if hit:
            return True
    return False


class Bus:
    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg
        self.cond = threading.Condition()
        self.changes = 0
        self.closed = False
        self.write_lock = threading.RLock()
        self.locks = {}
        self.cache = {}
        self.rates = {}
        # Relay hooks (relay_client.RelayClient sets them when config.relay is enabled; docs/RELAY.md).
        self.relay_machine = None  # this PC's relay machine id, None = relay off
        self.on_local_state = None  # called with the new state after a local set_state change (not expiry/remote)
        self.listeners = []  # threading.Events set on every wake()
        self.listening, self.listened = {}, {}  # idle-rewake long-polls per agent (Agent Console "reachable")
        with store.connect() as db:
            db.executescript(SCHEMA)
            # Added with the relay; CREATE TABLE IF NOT EXISTS leaves an existing table alone. NULL = written on this PC.
            if "origin" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN origin TEXT")
            self.latest = db.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()[0]
        self.state = self.load_state()

    # ---- plumbing
    @property
    def settings(self):
        return self.cfg.get("bus") or {}

    @property
    def mailbox_id(self):
        return (self.settings.get("mailbox") or {}).get("agent_id") or "study-claude"

    def modes(self):
        modes = {"available": {k: AVAILABLE[k] for k in ("label", "directive", "phone_min_priority")}}
        modes.update(self.settings.get("modes") or {})
        return modes

    def lock_for(self, agent_id):
        lock = self.locks.get(agent_id)
        return lock if lock is not None else self.locks.setdefault(agent_id, threading.RLock())

    def cache_for(self, agent_id, reset=False):
        cache = self.cache.get(agent_id)
        if cache is None or reset:
            cache = self.cache[agent_id] = {"written": 0, "checked": -1, "state": -1, "direct": True, "status": None}
        return cache

    def wake(self):
        with self.cond:
            self.changes += 1
            self.cond.notify_all()
        for event in self.listeners:
            event.set()

    def close(self):
        self.closed = True
        self.wake()

    # ---- agents
    def unique_name(self, db, base, agent_id):
        base = str(base or agent_id)[:60]
        taken = {r[0].lower() for r in db.execute("SELECT name FROM agents WHERE ended IS NULL AND id!=? AND name IS NOT NULL", (agent_id,))}
        name, n = base, 2
        while name.lower() in taken:
            name, n = f"{base}-{n}", n + 1
        return name

    def upsert(self, agent_id, rename=False, touch=True, meta=None, **values):
        if not isinstance(agent_id, str) or not ID_RE.fullmatch(agent_id):
            raise ValueError("id must be 1-80 characters: letters, digits and _ . : @ -")
        if agent_id in SYSTEM_NAMES:
            raise ValueError(f"'{agent_id}' is a reserved id")
        values = {k: v for k, v in values.items() if k in FIELDS and v is not None}
        now = time.time()
        with self.lock_for(agent_id), self.store.connect() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                state = self.state
                values["name"] = self.unique_name(db, values.get("name") or values.get("folder") or agent_id, agent_id)
                values.setdefault("tool", guess_tool(agent_id))
                values.setdefault("account", "unknown")
                values.setdefault("status", "idle")
                # New members start at the current head: no history replay, only messages sent from now on
                # (plus DMs addressed to their exact id, found by the "direct" scan).
                values.update(id=agent_id, meta=json.dumps(meta or {}), started=now, last_seen=now if touch else 0,
                              cursor=self.latest, wakes="[]",
                              state_seen=state["version"] if state["mode"] == "available" else 0)
                db.execute(f"INSERT INTO agents ({','.join(values)}) VALUES ({','.join('?' * len(values))})", tuple(values.values()))
                reset = True
            else:
                if not rename:
                    values.pop("name", None)
                if meta:
                    values["meta"] = json.dumps(dict(loads(row["meta"], {}), **meta))
                if touch:
                    values["last_seen"] = now
                reset = row["ended"] is not None
                if reset:
                    # A resumed session skips group chatter from while it was gone; DMs to its id still arrive.
                    values.update(ended=None, cursor=max(row["cursor"] or 0, self.latest))
                if values:
                    db.execute(f"UPDATE agents SET {', '.join(k + '=?' for k in values)} WHERE id=?", (*values.values(), agent_id))
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        cache = self.cache_for(agent_id, reset)
        cache["status"] = row["status"]
        if touch:
            cache["written"] = now
        return row

    def ensure(self, agent_id, touch=True):
        cache = self.cache.get(agent_id)
        if cache and cache["status"] != "ended" and (not touch or time.time() - cache["written"] < 10):
            return
        self.upsert(agent_id, touch=touch)

    def agent(self, agent_id):
        with self.store.connect() as db:
            return db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()

    def end(self, agent_id):
        with self.lock_for(agent_id), self.store.connect() as db:
            exists = db.execute("UPDATE agents SET ended=COALESCE(ended, ?), status='idle' WHERE id=?", (time.time(), agent_id)).rowcount
        self.cache_for(agent_id, True)["status"] = "ended"
        self.wake()
        return bool(exists)

    def agents(self, include_ended=False, account=None, folder=None, tool=None, remote=False):
        """Local agents; remote=True also gives them `machine` and appends the other PCs' agents (relay directory)."""
        now = time.time()
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM agents" + ("" if include_ended else " WHERE ended IS NULL")).fetchall()
            others = db.execute("SELECT * FROM remote_agents").fetchall() if remote else []

        def wanted(r):
            return ((not account or (r["account"] or "").lower() == account.lower())
                    and (not folder or (r["folder"] or "").lower() == folder.lower())
                    and (not tool or (r["tool"] or "").lower() == tool.lower()))

        views = [agent_view(r, now) for r in rows if wanted(r)]
        views.sort(key=lambda a: (RANK[a["state"]], -(a["last_seen"] or 0)))
        if remote:
            for view in views:
                view["machine"] = self.relay_machine
            far = [remote_view(r, now) for r in others if wanted(r)]
            far.sort(key=lambda a: (not a["machine_online"], RANK.get(a["state"], 2), -(a["last_seen"] or 0)))
            views += far
        return views

    def agent_rows(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT id, name, account, folder, tool, ended, last_seen, status FROM agents")]

    # ---- relay: the other PCs' agents (docs/RELAY.md)
    def remote_rows(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM remote_agents")]

    def set_remote_agents(self, machines):
        """Replace the remote directory with a relay `agents` answer: [{id, online, agentsAt, agents: [...]}, ...]."""
        def text(value, limit):
            if not isinstance(value, str):
                return None
            value = "".join(c for c in value if c.isprintable()).strip()
            return value[:limit] or None

        def stamp(value):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                return None
            return value / 1000.0 if value > 1e11 else float(value)  # the server speaks epoch ms in camelCase fields

        mine = (self.relay_machine or "").lower()
        rows = []
        for machine in machines if isinstance(machines, list) else []:
            if not isinstance(machine, dict):
                continue
            name = machine.get("id")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", name) or name.lower() == mine:
                continue
            online = 1 if machine.get("online") is True else 0
            agents_at = stamp(machine.get("agentsAt", machine.get("agents_at"))) or time.time()
            for agent in (machine.get("agents") or [])[:100] if isinstance(machine.get("agents"), list) else []:
                if not isinstance(agent, dict) or not isinstance(agent.get("id"), str) or not ID_RE.fullmatch(agent["id"]):
                    continue
                rows.append((name, agent["id"], text(agent.get("name"), 60), text(agent.get("tool"), 40),
                             text(agent.get("account"), 40), text(agent.get("folder"), 100), text(agent.get("activity"), 300),
                             text(agent.get("status"), 20), text(agent.get("state"), 20), stamp(agent.get("last_seen")),
                             stamp(agent.get("started")), agents_at, online))
        with self.write_lock, self.store.connect() as db:
            db.execute("DELETE FROM remote_agents")
            db.executemany("INSERT OR REPLACE INTO remote_agents (machine, id, name, tool, account, folder, activity, status, "
                           "state, last_seen, started, agents_at, online) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def route(self, tokens, agents=None, remote=None):
        """Relay forwarding for a message's to_spec: (forward, dest). dest None = every other PC, [] = the server only."""
        agents = self.agent_rows() if agents is None else agents
        remote = self.remote_rows() if remote is None else remote
        local = {a["id"].lower() for a in agents} | {(a["name"] or "").lower() for a in agents if a["name"]}
        forward, everywhere, machines = False, False, set()
        for token in tokens:
            low = str(token).lower()
            if is_group(low) or low == "user":
                forward = everywhere = True
            elif low in SERVER_ONLY:
                forward = True
            elif low in local:
                continue
            else:
                hits = [r for r in remote if r["id"].lower() == low or (r["name"] or "").lower() == low]
                forward = True
                if hits:
                    machines.update(r["machine"] for r in hits)
                else:
                    everywhere = True
        return forward, None if everywhere else sorted(machines)

    def wakes(self, agent_id, record=False):
        now = time.time()
        with self.lock_for(agent_id), self.store.connect() as db:
            row = db.execute("SELECT wakes FROM agents WHERE id=?", (agent_id,)).fetchone()
            recent = [t for t in loads(row["wakes"] if row else None, []) if isinstance(t, (int, float)) and now - t < 1800]
            if record:
                recent.append(now)
                db.execute("UPDATE agents SET wakes=?, status='working', last_seen=? WHERE id=?", (json.dumps(recent), now, agent_id))
        if record:
            self.cache_for(agent_id)["status"] = "working"
        return len(recent)

    # ---- messages
    def message(self, ref):
        ref = str(ref).strip().lstrip("#")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (ref,)).fetchone()
            if row is None and ref.isdigit():
                row = db.execute("SELECT * FROM messages WHERE seq=?", (int(ref),)).fetchone()
        return row

    @staticmethod
    def tokens(to):
        if isinstance(to, str):
            to = to.split(",")
        if not isinstance(to, list) or not all(isinstance(t, str) for t in to):
            raise ValueError("to must be a string or a list of strings")
        tokens = list(dict.fromkeys("all" if t.strip() == "*" else t.strip() for t in to if t.strip()))
        if not tokens:
            raise ValueError("to is required: all, @account, #folder, tool:<tool>, an agent id, name or unique id prefix")
        if len(tokens) > 20 or any(len(t) > 100 for t in tokens):
            raise ValueError("to accepts at most 20 recipients of at most 100 characters")
        return tokens

    @staticmethod
    def resolve(tokens, agents, remote=()):
        """Pin single-agent tokens (name, id prefix) to an id now; group selectors stay dynamic. Local agents first,
        then the other PCs' agents (relay directory)."""
        resolved = []
        live = [a for a in agents if not a["ended"]]
        remote = list(remote)
        near = [r for r in remote if r.get("online")]
        for token in tokens:
            low = token.lower()
            if is_group(token) or any(a["id"] == token for a in agents) or low in SYSTEM_NAMES or low in SERVER_ONLY:
                resolved.append(token)
                continue
            found = []
            for pool in (live, agents):
                found = [a for a in pool if (a["name"] or "").lower() == low]
                if found:
                    break
            if not found and len(token) >= 4:
                for pool in (live, agents):
                    found = [a for a in pool if a["id"].lower().startswith(low)]
                    if found:
                        break
            if not found and remote:
                found = [r for r in remote if r["id"].lower() == low]
                for pool in (near, remote):
                    if found:
                        break
                    found = [r for r in pool if (r["name"] or "").lower() == low]
                for pool in (near, remote):
                    if found or len(token) < 4:
                        break
                    found = [r for r in pool if r["id"].lower().startswith(low)]
            if len(found) > 1:
                raise BusError(409, f"'{token}' matches {len(found)} agents; address one by id",
                               candidates=[{"id": a["id"], "name": a["name"], "account": a["account"], "folder": a["folder"],
                                            "state": agent_state(a) if "ended" in a else remote_view(a)["state"],
                                            **({"machine": a["machine"]} if "machine" in a else {})} for a in found])
            resolved.append(found[0]["id"] if found else token)
        return list(dict.fromkeys(resolved))

    def throttle(self, sender, kind):
        now = time.time()
        recent = self.rates.setdefault(sender, deque())
        while recent and now - recent[0][0] > 60:
            recent.popleft()
        if len(recent) >= 60:
            raise BusError(429, "Rate limit: at most 60 messages per minute per sender", retry_after=round(61 - (now - recent[0][0])))
        if kind == "broadcast" and sum(k == "broadcast" for _, k in recent) >= 5:
            raise BusError(429, "Rate limit: at most 5 broadcasts per minute per sender")
        recent.append((now, kind))

    def insert(self, db, sender, from_name, tokens, kind, body, subject=None, priority=3, thread=None, reply_to=None,
               expects=False, meta=None, expires=None, msg_id=None, origin=None, ts=None, ignore=False):
        """Returns (seq, id); seq is None when ignore=True and the id already exists (a relayed duplicate)."""
        msg_id = msg_id or "m" + uuid.uuid4().hex[:12]
        cursor = db.execute(f"""INSERT {'OR IGNORE ' if ignore else ''}INTO messages (id,ts,from_id,from_name,to_spec,kind,
                               subject,body,priority,thread,reply_to,expects_reply,meta,expires,origin)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (msg_id, time.time() if ts is None else ts, sender, from_name, json.dumps(tokens), kind, subject,
                             body, priority, thread or msg_id, reply_to, int(bool(expects)), json.dumps(meta or {}), expires,
                             origin))
        return (cursor.lastrowid if cursor.rowcount else None), msg_id

    def relay_receipt(self, sender, tokens, kind, agents=None):
        """The relay part of a send receipt: remote recipients known from the directory, and whether it leaves this PC."""
        if self.relay_machine is None:
            return {"remote": [], "relayed": False}
        remote = self.remote_rows()
        far = [{"id": r["id"], "machine": r["machine"]} for r in remote if r["id"] != sender and matches(tokens, r)]
        relayed = sender != "bus" and kind != "system" and self.route(tokens, agents, remote)[0]
        return {"remote": far if relayed else [], "relayed": bool(relayed)}

    def receipt(self, row, duplicate=True):
        tokens = loads(row["to_spec"], [])
        agents = self.agent_rows()
        recipients = [a["id"] for a in agents if not a["ended"] and a["id"] != row["from_id"] and matches(tokens, a)]
        return dict({"id": row["id"], "seq": row["seq"], "thread": row["thread"], "recipients": recipients, "kind": row["kind"],
                     "to": tokens, "duplicate": duplicate}, **self.relay_receipt(row["from_id"], tokens, row["kind"], agents))

    def send(self, data, internal=False):
        if not isinstance(data, dict):
            raise ValueError("Send a JSON object")
        sender = clean(data.get("from"), "from", 80, True)
        if not ID_RE.fullmatch(sender):
            raise ValueError("from must be an agent id (letters, digits and _ . : @ -)")
        if sender == "bus" and not internal:
            raise ValueError("'bus' is reserved for system messages")
        if sender == "planner" and not internal:
            raise ValueError("'planner' is reserved for Cameron's planner; only the relay delivers its messages")
        body = clean(data.get("body"), "body", 4000, True)
        subject = clean(data.get("subject"), "subject", 150)
        kind = data.get("kind") or None
        if kind is not None and kind not in KINDS:
            raise ValueError("kind must be dm, broadcast, request, reply or system")
        if kind == "system" and not internal and sender not in CAMERON:
            raise ValueError("Only the bus and the user send system messages")
        priority = integer(data.get("priority"), "priority", 1, 5, 3)
        thread = clean(data.get("thread"), "thread", 100)
        reply_to = data.get("reply_to")
        reply_to = clean(str(reply_to) if type(reply_to) is int else reply_to, "reply_to", 100)
        expects = data.get("expects_reply", False)
        if expects not in (True, False, 0, 1, None):
            raise ValueError("expects_reply must be true or false")
        ttl = number(data.get("ttl_seconds"), "ttl_seconds", 1, MESSAGE_TTL)
        meta = data.get("meta") or {}
        if not isinstance(meta, dict) or len(json.dumps(meta)) > 2000:
            raise ValueError("meta must be a JSON object of at most 2000 characters")
        if "console" in meta and not internal:
            raise ValueError("meta.console is reserved for the Agent Console (POST /console/prompt)")
        client_id = clean(data.get("id"), "id", 100)
        if client_id is not None and not ID_RE.fullmatch(client_id):
            raise ValueError("id may only contain letters, digits and _ . : @ -")
        if client_id:
            existing = self.message(client_id)
            if existing is not None and existing["id"] == client_id:
                if existing["from_id"] != sender:
                    raise BusError(409, "That message id is already used by another sender")
                return self.receipt(existing)
        if not internal and sender not in SYSTEM_NAMES:
            self.ensure(sender)
        parent = self.message(reply_to) if reply_to else None
        if parent is not None:
            reply_to = parent["id"]
        to = data.get("to")
        if to in (None, "", []) and parent is not None:
            to = [parent["from_id"]]
        tokens = self.tokens(to)
        with self.write_lock:
            agents = self.agent_rows()
            tokens = self.resolve(tokens, agents, self.remote_rows() if self.relay_machine else ())
            if kind is None:
                kind = "reply" if reply_to else "broadcast" if any(is_group(t) for t in tokens) else "dm"
            if "all" in tokens and kind not in ("request", "system"):
                kind = "broadcast"
            if not internal:
                self.throttle(sender, kind)
            me = next((a for a in agents if a["id"] == sender), None)
            from_name = SYSTEM_NAMES.get(sender) or (me["name"] if me else None) or sender
            if parent is not None:
                thread = parent["thread"] or parent["id"]
            try:
                with self.store.connect() as db:
                    seq, msg_id = self.insert(db, sender, from_name, tokens, kind, body, subject, priority, thread, reply_to,
                                              expects, meta, time.time() + ttl if ttl else None, client_id)
            except sqlite3.IntegrityError:
                existing = self.message(client_id) if client_id else None
                if existing is None:
                    raise
                return self.receipt(existing)
            self.latest = max(self.latest, seq)
        self.wake()
        recipients = [a["id"] for a in agents if not a["ended"] and a["id"] != sender and matches(tokens, a)]
        return dict({"id": msg_id, "seq": seq, "thread": thread or msg_id, "recipients": recipients, "kind": kind,
                     "to": tokens, "duplicate": False}, **self.relay_receipt(sender, tokens, kind, agents))

    def ingest_remote(self, message, origin):
        """Store a message relayed from another PC (or the planner) under its own id; True when it was new.

        It gets a fresh local seq, so local agents receive it through the normal inbox, hooks, MCP tools and mailbox."""
        if not isinstance(message, dict) or not isinstance(origin, str) or not origin:
            raise ValueError("relayed message must be an object with an origin")
        sender, kind = message.get("from"), message.get("kind")
        if not isinstance(sender, str) or not REMOTE_REF_RE.fullmatch(sender) or len(sender) > 80:
            raise ValueError("relayed message has a bad from")
        if sender == "bus" or kind == "system":
            raise ValueError("system messages are never relayed")
        if sender == "planner" and origin != "planner":
            raise ValueError("only the planner server may send as planner")
        if kind not in ("dm", "broadcast", "request", "reply"):
            raise ValueError("relayed message has a bad kind")
        msg_id = message.get("id")
        if not isinstance(msg_id, str) or not REMOTE_ID_RE.fullmatch(msg_id):
            raise ValueError("relayed message has a bad id")
        refs = {}
        for key in ("thread", "reply_to"):
            value = message.get(key)
            if value is not None and (not isinstance(value, str) or not REMOTE_REF_RE.fullmatch(value)):
                raise ValueError(f"relayed message has a bad {key}")
            refs[key] = value
        to = message.get("to")
        if (not isinstance(to, list) or not 1 <= len(to) <= 20
                or not all(isinstance(t, str) and 1 <= len(t) <= 100 for t in to)):
            raise ValueError("relayed message has a bad to")
        body = clean(message.get("body"), "body", 4000, True)
        subject = clean(message.get("subject"), "subject", 150)
        from_name = message.get("from_name").strip()[:100] if isinstance(message.get("from_name"), str) else None
        priority = message.get("priority", 3)
        if type(priority) is not int or not 1 <= priority <= 5:
            raise ValueError("relayed message has a bad priority")
        meta = message.get("meta") or {}
        if not isinstance(meta, dict) or len(json.dumps(meta)) > 2000:
            raise ValueError("relayed message has a bad meta")
        # A relayed message is never a console prompt: those cross PCs only end-to-end encrypted (bus_console.py).
        meta = {k: v for k, v in meta.items() if k != "console"}
        ts = number(message.get("ts"), "ts", 0, 1e11) or time.time()
        expires = number(message.get("expires"), "expires", 0, 1e11)
        if sender in SYSTEM_NAMES:
            from_name = SYSTEM_NAMES[sender]
        with self.write_lock:
            with self.store.connect() as db:
                seq, _ = self.insert(db, sender, from_name or sender, to, kind, body, subject, priority, refs["thread"],
                                     refs["reply_to"], bool(message.get("expects_reply")), meta, expires, msg_id, origin,
                                     ts, ignore=True)
            if seq is None:
                return False
            self.latest = max(self.latest, seq)
        self.wake()
        return True

    def claim(self, message_id, agent_id):
        message_id = clean(str(message_id) if type(message_id) is int else message_id, "message_id", 100, True)
        agent_id = clean(agent_id, "agent", 80, True)
        row = self.message(message_id)
        if row is None:
            raise BusError(404, "Message not found")
        with self.store.connect() as db:
            won = db.execute("UPDATE messages SET claimed_by=? WHERE seq=? AND claimed_by IS NULL", (agent_id, row["seq"])).rowcount
            by = db.execute("SELECT claimed_by FROM messages WHERE seq=?", (row["seq"],)).fetchone()[0]
        if won:
            self.wake()
        return {"claimed": by == agent_id, "by": by, "message_id": row["id"], "seq": row["seq"]}

    def history(self, limit=50, since_seq=0, thread=None, sender=None, to=None, kind=None):
        clauses, params = ["seq>?"], [since_seq]
        for column, value in (("thread", thread), ("from_id", sender), ("kind", kind)):
            if value:
                clauses.append(column + "=?")
                params.append(value)
        if to:
            clauses.append("to_spec LIKE ?")
            params.append("%" + json.dumps(to)[1:-1].replace("%", "").replace("_", "\\_") + "%")
            clauses[-1] += " ESCAPE '\\'"
        clauses.append("meta NOT LIKE '%\"rc\": 1%'")  # Agent Console relay frames (ciphertext) are not conversation
        with self.store.connect() as db:
            rows = db.execute(f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY seq DESC LIMIT ?", (*params, limit)).fetchall()
        return {"messages": [message_view(r) for r in reversed(rows)], "latest_seq": self.latest}

    def thread(self, ref):
        row = self.message(ref)
        key = (row["thread"] or row["id"]) if row is not None else str(ref)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE thread=? OR id=? ORDER BY seq", (key, key)).fetchall()
        if not rows:
            raise BusError(404, "Thread not found")
        return {"thread": key, "messages": [message_view(r) for r in rows]}

    def recent_broadcasts(self, agent_row, hours, limit=3):
        now = time.time()
        with self.store.connect() as db:
            rows = db.execute("""SELECT * FROM messages WHERE kind IN ('broadcast','request') AND ts>=? AND seq<=? AND from_id!=?
                                 ORDER BY seq DESC LIMIT 30""", (now - hours * 3600, agent_row["cursor"], agent_row["id"])).fetchall()
        picked = [m for m in rows if not loads(m["meta"], {}).get("noinject") and self.wanted(m, agent_row, now)][:limit]
        return [message_view(m) for m in reversed(picked)]

    # ---- inbox
    @staticmethod
    def wanted(m, agent, now):
        if m["expires"] and m["expires"] <= now:
            return False
        if m["kind"] == "request" and m["claimed_by"] and m["claimed_by"] != agent["id"]:
            return False
        return matches(loads(m["to_spec"], []), agent)

    def quiet(self, agent_id):
        """True when nothing can be pending for this agent: no DB access needed (PreToolUse fast path)."""
        cache = self.cache.get(agent_id)
        return (bool(cache) and not cache["direct"] and cache["checked"] >= self.latest
                and cache["state"] >= self.current_state()["version"])

    def fetch(self, agent_id, limit=20, free=None):
        """Pending messages in seq order; ones matching `free` (e.g. noinject notices) ride along without using the limit."""
        now = time.time()
        latest = self.latest
        state = self.current_state()
        cache = self.cache_for(agent_id)
        direct = cache["direct"]
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise BusError(404, f"Unknown agent {agent_id}")
            # Idle wakes deliver single messages ahead of the cursor, so receipts are excluded explicitly.
            rows = db.execute("""SELECT * FROM messages m WHERE seq>? AND seq<=? AND from_id!=? AND NOT EXISTS
                                 (SELECT 1 FROM deliveries d WHERE d.seq=m.seq AND d.agent_id=?) ORDER BY seq""",
                              (row["cursor"], latest, agent_id, agent_id)).fetchall()
            if direct:
                # DMs sent to this exact id before it joined (or while it was ended) sit below its cursor.
                older = db.execute("""SELECT * FROM messages m WHERE seq<=? AND to_spec LIKE ? AND from_id!=? AND NOT EXISTS
                                      (SELECT 1 FROM deliveries d WHERE d.seq=m.seq AND d.agent_id=?) ORDER BY seq""",
                                   (row["cursor"], "%" + json.dumps(agent_id) + "%", agent_id, agent_id)).fetchall()
                rows = older + rows
        picked = [m for m in rows if self.wanted(m, row, now)]
        shown, counted = [], 0
        for m in picked:
            if not (free and free(m)):
                if counted == limit:
                    break
                counted += 1
            shown.append(m)
        pending = len(picked) - len(shown)
        changed = state if state["version"] > (row["state_seen"] or 0) else None
        if not picked and not changed:
            cache.update(checked=latest, state=row["state_seen"] or 0, direct=False)
        return {"rows": shown, "messages": [message_view(m) for m in shown], "pending": pending, "cursor": row["cursor"],
                "scanned": shown[-1]["seq"] if pending else latest, "state_changed": changed, "version": state["version"],
                "state_seen": row["state_seen"] or 0, "direct": direct, "agent": row}

    def mark(self, agent_id, fetched):
        if fetched["rows"] or fetched["state_changed"] or fetched["scanned"] > fetched["cursor"]:
            now = time.time()
            with self.store.connect() as db:
                db.executemany("INSERT OR IGNORE INTO deliveries (seq, agent_id, ts) VALUES (?,?,?)",
                               [(m["seq"], agent_id, now) for m in fetched["rows"]])
                db.execute("UPDATE agents SET cursor=MAX(cursor, ?), state_seen=MAX(state_seen, ?) WHERE id=?",
                           (fetched["scanned"], fetched["version"], agent_id))
        cache = self.cache_for(agent_id)
        if fetched["pending"]:
            cache["checked"] = -1
        else:
            cache.update(checked=fetched["scanned"], state=max(fetched["state_seen"], fetched["version"]), direct=False)

    @staticmethod
    def inbox_view(fetched):
        changed = fetched["state_changed"]
        return {"messages": fetched["messages"], "state_changed": state_view(changed) if changed else None,
                "pending": fetched["pending"]}

    def inbox(self, agent_id, mark=True, limit=20, touch=True, free=None):
        if not isinstance(agent_id, str) or not ID_RE.fullmatch(agent_id):
            raise ValueError("agent must be an agent id")
        self.ensure(agent_id, touch=touch)
        with self.lock_for(agent_id):
            fetched = self.fetch(agent_id, limit, free)
            if mark:
                self.mark(agent_id, fetched)
        return self.inbox_view(fetched)

    def deliver(self, agent_id, rows):
        with self.store.connect() as db:
            db.executemany("INSERT OR IGNORE INTO deliveries (seq, agent_id, ts) VALUES (?,?,?)",
                           [(m["seq"], agent_id, time.time()) for m in rows])
        self.cache_for(agent_id)["checked"] = -1

    def wake_check(self, agent_id, limit):
        """One idle-wake probe: a result dict ends the long-poll, None keeps waiting.

        With idle_wake off or Cameron sleeping, only his Agent Console prompts wake a session (and they are never
        capped), so the rewake poller stays alive for them."""
        peers = self.settings.get("idle_wake", True) and self.current_state()["mode"] != "sleeping"
        if self.quiet(agent_id):
            return None
        row = self.agent(agent_id)
        if row is None or row["ended"]:
            return {"ended": True}
        if row["status"] == "working":
            return None
        with self.lock_for(agent_id):
            fetched = self.fetch(agent_id, 500)
            hits = [m for m in fetched["rows"] if wake_worthy(m)][:limit]
            if hits and (not peers or self.wakes(agent_id) >= 2):
                hits = [m for m in hits if console_prompt(m)]
            if not hits:
                return None
            self.deliver(agent_id, hits)
            self.wakes(agent_id, record=True)
        return {"messages": [message_view(m) for m in hits], "pending": fetched["pending"] + len(fetched["rows"]) - len(hits)}

    def wait(self, agent_id, timeout=25, limit=20, urgent=False, alive=None):
        if not isinstance(agent_id, str) or not ID_RE.fullmatch(agent_id):
            raise ValueError("agent must be an agent id")
        deadline = time.monotonic() + min(max(timeout, 0), 55)
        base = {"messages": [], "state_changed": None, "pending": 0, "ended": False, "disabled": False}
        if not urgent:
            self.ensure(agent_id)
            return self.wait_loop(agent_id, deadline, base, limit, urgent, alive)
        # An idle session's rewake poller is waiting: the Agent Console shows it as reachable.
        with self.cond:
            self.listening[agent_id] = self.listening.get(agent_id, 0) + 1
            self.listened[agent_id] = time.time()
        try:
            return self.wait_loop(agent_id, deadline, base, limit, urgent, alive)
        finally:
            with self.cond:
                self.listened[agent_id] = time.time()
                if self.listening.get(agent_id, 0) <= 1:
                    self.listening.pop(agent_id, None)
                else:
                    self.listening[agent_id] -= 1

    def reachable(self, agent_id, grace=20):
        """An idle session wakes for a console prompt: its rewake poller is waiting now (or was between two polls)."""
        with self.cond:
            return bool(self.listening.get(agent_id)) or time.time() - self.listened.get(agent_id, 0) < grace

    def wait_loop(self, agent_id, deadline, base, limit, urgent, alive):
        while True:
            with self.cond:
                seen = self.changes
            if self.closed or (alive is not None and not alive()):
                return base
            if urgent:
                result = self.wake_check(agent_id, limit)
            elif not self.quiet(agent_id):
                result = self.inbox(agent_id, True, limit)
                result = result if result["messages"] or result["state_changed"] else None
            else:
                result = None
            if result is not None:
                return dict(base, **result)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return base
            with self.cond:
                if self.changes == seen and not self.closed:
                    self.cond.wait(remaining)

    def mark_state_seen(self, agent_id, version):
        with self.store.connect() as db:
            db.execute("UPDATE agents SET state_seen=MAX(state_seen, ?) WHERE id=?", (version, agent_id))
        cache = self.cache_for(agent_id)
        cache["state"] = max(cache["state"], version)

    # ---- user state
    def load_state(self):
        saved = self.store.get_state("user_state") or {}
        state = dict(AVAILABLE, **(saved if isinstance(saved, dict) else {}))
        state["version"] = max(int(state.get("version") or 0), int(self.store.get_state("user_state_version", 0) or 0))
        return state

    def current_state(self):
        """The user state, expiring it lazily (every hook call, notify, GET /state and the scheduler tick)."""
        state = self.state
        if state["mode"] != "available" and state.get("expires_at") and time.time() >= state["expires_at"]:
            with self.write_lock:
                if self.state is state:
                    label = state.get("label") or state["mode"]
                    new = dict(AVAILABLE, set_by="expiry", set_at=time.time())
                    self.apply_state(new, "Cameron is available again", f"Cameron is available again ({label.lower()} ended).")
        return dict(self.state)

    def apply_state(self, new, subject, body):
        with self.write_lock:
            version = self.state["version"] + 1
            new = dict(new, version=version)
            meta = {"noinject": True, "user_state": {"mode": new["mode"], "version": version}}
            with self.store.connect() as db:
                upsert = "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                db.execute(upsert, ("user_state", json.dumps(new)))
                db.execute(upsert, ("user_state_version", json.dumps(version)))
                seq, _ = self.insert(db, "bus", SYSTEM_NAMES["bus"], ["all"], "system", body, subject, 3, None, None, False, meta)
            self.state = new
            self.latest = max(self.latest, seq)
        self.wake()
        return dict(new)

    def set_state(self, data):
        if not isinstance(data, dict):
            raise ValueError("Send a JSON object")
        mode = clean(data.get("mode"), "mode", 40, True).lower()
        if not MODE_RE.fullmatch(mode):
            raise ValueError("mode must be lowercase letters, digits, - or _")
        note = clean(data.get("note"), "note", 500)
        by = clean(data.get("by"), "by", 80) or "user"
        ttl = number(data.get("ttl_minutes"), "ttl_minutes", 1, 1440)
        phone_min = integer(data.get("phone_min_priority"), "phone_min_priority", 1, 5)
        now = time.time()
        with self.write_lock:
            previous = self.current_state()
            if mode == "available":
                if previous["mode"] == "available":
                    return previous
                new = dict(AVAILABLE, note=note or "", set_by=by, set_at=now)
                label = previous.get("label") or previous["mode"]
                new = self.apply_state(new, "Cameron is available again",
                                       f"Cameron is available again ({label.lower()} ended)." + (f" Note: {note}" if note else ""))
            else:
                preset = self.preset(mode, note, strict=True)
                minutes = ttl or preset.get("ttl_minutes") or 180
                label = preset.get("label") or mode.capitalize()
                new = {"mode": mode, "label": label, "note": note or "", "directive": preset.get("directive") or "",
                       "phone_min_priority": phone_min or int(preset.get("phone_min_priority") or 1), "set_by": by,
                       "set_at": now, "expires_at": now + minutes * 60}
                new = self.apply_state(new, *self.state_notice(new))
            # Still under write_lock, so a relayed state cannot slip in between this change and the relay seeing it.
            self.local_state_changed(new)
            return new

    def local_state_changed(self, state):
        if self.on_local_state is not None:
            try:
                self.on_local_state(dict(state))
            except Exception:
                LOG.exception("Relay state hook failed")

    def preset(self, mode, note, strict=False):
        preset = self.modes().get(mode)
        if preset is None:
            if strict and not note:
                raise ValueError(f"Unknown mode '{mode}'. Presets: {', '.join(self.modes())}. A custom mode needs a note.")
            busy = f"Cameron is busy ({note})" if note else "Cameron is busy"
            preset = {"label": mode.replace("-", " ").replace("_", " ").capitalize(), "ttl_minutes": 180, "phone_min_priority": 4,
                      "directive": f"{busy} and is not available to answer questions. {QUEUE}"}
        return preset

    @staticmethod
    def state_notice(new):
        label = new.get("label") or new["mode"]
        body = f"Cameron's state: {label.lower()} until {clock(new['expires_at'])}"
        body += (f" ({new['note']})" if new.get("note") else "") + (f" — {new['directive']}" if new.get("directive") else "")
        return f"Cameron is now: {label}", body

    def apply_remote_state(self, payload, origin):
        """Apply Cameron's state relayed from another PC or the planner (relay wire format; None = available).

        The mode's local preset is overlaid with the payload, the notice is the same as for a local change, and it never
        calls on_local_state (no push back). Returns the new state, or None when nothing changed."""
        now = time.time()
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("relayed state must be an object or null")
        payload = payload or {"mode": "available"}
        mode = payload.get("mode") if isinstance(payload.get("mode"), str) else ""
        if not MODE_RE.fullmatch(mode):
            raise ValueError("relayed state has a bad mode")

        def text(key, limit, default=""):
            value = payload.get(key)
            return value.strip()[:limit] if isinstance(value, str) else default

        def stamp(key):
            value = payload.get(key)
            return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None

        note, by, set_at, expires = text("note", 500), text("set_by", 80) or "planner", stamp("set_at") or now, stamp("expires_at")
        origin = str(origin or "")[:80] or None
        with self.write_lock:
            previous = self.current_state()
            if mode == "available" or (expires is not None and expires <= now):
                if previous["mode"] == "available":
                    return None
                label = previous.get("label") or previous["mode"]
                new = dict(AVAILABLE, note=note, set_by=by, set_at=set_at, relay_origin=origin)
                return self.apply_state(new, "Cameron is available again",
                                        f"Cameron is available again ({label.lower()} ended)." + (f" Note: {note}" if note else ""))
            preset = self.preset(mode, note)
            phone_min = payload.get("phone_min_priority")
            if type(phone_min) is not int or not 1 <= phone_min <= 5:
                phone_min = int(preset.get("phone_min_priority") or 1)
            new = {"mode": mode, "label": text("label", 60) or preset.get("label") or mode.capitalize(), "note": note,
                   "directive": text("directive", 2000) or preset.get("directive") or "", "phone_min_priority": phone_min,
                   "set_by": by, "set_at": set_at,
                   "expires_at": expires or set_at + (preset.get("ttl_minutes") or 180) * 60, "relay_origin": origin}
            if new["expires_at"] <= now:
                return None
            return self.apply_state(new, *self.state_notice(new))

    def state_payload(self):
        return dict(state_view(self.current_state()), modes=self.modes())

    # ---- maintenance
    def prune(self):
        now = time.time()
        with self.store.connect() as db:
            db.execute("DELETE FROM agents WHERE id!=? AND ((ended IS NOT NULL AND ended<?) OR COALESCE(last_seen, 0)<?)",
                       (self.mailbox_id, now - AGENT_TTL, now - AGENT_TTL))
            db.execute("DELETE FROM messages WHERE ts<? OR (expires IS NOT NULL AND expires<?)", (now - MESSAGE_TTL, now - 3600))
            db.execute("DELETE FROM deliveries WHERE ts<? OR seq NOT IN (SELECT seq FROM messages)", (now - MESSAGE_TTL,))
            db.execute("DELETE FROM remote_agents WHERE COALESCE(agents_at, 0)<?", (now - REMOTE_TTL,))
        self.cache.clear()

    def health(self):
        now = time.time()
        with self.store.connect() as db:
            rows = db.execute("SELECT status, last_seen, ended FROM agents WHERE ended IS NULL").fetchall()
            count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        states = [agent_state(r, now) for r in rows]
        return {"agents": sum(s != "stale" for s in states), "working": states.count("working"), "messages": count,
                "latest_seq": self.latest, "state_mode": self.current_state()["mode"]}
