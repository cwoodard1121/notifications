"""Ask Cameron: an agent asks him a question, and his answer comes back to it as a bus reply from "user".

Default deny. An ask goes to his bus inbox (/ui, plus a once-a-day digest) unless one of these holds:
  * the asking agent is on his allowlist (he adds it: SMS "allow appraiseme 2h", the /ui toggle, or the TTY-only CLI
    `bus.py ask-allow`); agents cannot add themselves and nothing an agent says counts as permission;
  * priority 5 with a one-line urgency reason, and the agent is not downgraded (he answered one of its urgent asks
    with 0 / "not urgent" in the last 24 h).
Those are texted through the phone gateway (POST /v1/questions, docs/PHONE-GATEWAY.md "Questions"), batched, never
while he is studying / in lecture / sleeping (held, then one batched text when that ends). A priority-5 ask with a
reason may also ring (call=true), at most one ask call per agent per hour and within the phone call limits.
Every ask is logged in the asks table with its route and why. Docs: docs/AGENT-BUS.md "Asking Cameron".
"""
import hmac
import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta

from bus_core import BusError, clean, integer, iso, matches

LOG = logging.getLogger("notify")
SCHEMA = """
CREATE TABLE IF NOT EXISTS asks (
    id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL, agent TEXT NOT NULL, label TEXT NOT NULL,
    folder TEXT, account TEXT, question TEXT NOT NULL, options TEXT NOT NULL DEFAULT '[]', priority INTEGER NOT NULL,
    reason TEXT, call INTEGER NOT NULL DEFAULT 0, route TEXT NOT NULL, why TEXT NOT NULL, status TEXT NOT NULL,
    expires REAL NOT NULL, next_attempt REAL NOT NULL DEFAULT 0, msg_id TEXT, gateway_id TEXT, num INTEGER,
    answer TEXT, option INTEGER, not_urgent INTEGER NOT NULL DEFAULT 0, via TEXT, answered_at REAL, error TEXT,
    cancel_gateway INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS asks_status ON asks(status, next_attempt);
CREATE INDEX IF NOT EXISTS asks_agent ON asks(agent, created);
"""
OPEN = ("inbox", "waiting", "sent")
FINAL = ("answered", "expired", "canceled")
TEXTED = ("text", "call")
NO_TEXT_MODES = ("studying", "lecture", "sleeping")
QUESTION_MAX, OPTION_MAX, MAX_OPTIONS, REASON_MAX, LABEL_MAX, ANSWER_MAX = 300, 30, 6, 80, 40, 1000
DEFAULT_OPEN = 24 * 3600
MAX_OPEN = 7 * 24 * 3600
BATCH = 5  # questions per text (the gateway takes 1-5)
OPEN_PER_AGENT = 5  # open asks per agent; more is refused (409) so the inbox cannot be flooded
CALL_GAP = 3600  # one ask call per agent per hour
DOWNGRADE = 24 * 3600
DEFAULT_ALLOW = 4 * 3600
DIGEST_HOUR = 17  # the daily inbox digest goes out once a day after this local hour, only while he is available
IDLE_POLL = 30  # seconds between /v1/updates polls with nothing open (owner commands like "allow appraiseme 2h")
OPEN_POLL = 10  # ... while a texted question is open
WAIT_MAX = 55
TARGET_RE = re.compile(r"[@#]?[a-z0-9][a-z0-9_.:-]{0,59}")
COMMAND_RE = re.compile(r"^\s*(allow|stop)\s+([@#]?[a-z0-9][\w.:-]{0,59})(?:\s+(?:for\s+)?(\S+(?:\s*\S+)?))?\s*$", re.I)
DURATION_RE = re.compile(r"^(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$", re.I)


def name_base(name):
    """appraiseme-9 -> appraiseme (session names are folder plus a counter)."""
    return re.sub(r"-\d+$", "", (name or "").lower())


def parse_duration(text, now=None):
    """'2h', '30m', '1d', 'today' (until local midnight), 'always' -> seconds from now (None = always)."""
    now = time.time() if now is None else now
    word = (text or "").strip().lower()
    if not word:
        return DEFAULT_ALLOW
    if word == "always":
        return None
    if word == "today":
        local = datetime.fromtimestamp(now)
        midnight = datetime(local.year, local.month, local.day) + timedelta(days=1)
        return max(60, midnight.timestamp() - now)
    m = DURATION_RE.fullmatch(word.replace(" ", ""))
    if not m:
        raise ValueError("duration must be like 30m, 2h, 1d, today or always")
    unit = m[2][0].lower()
    seconds = int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[unit]
    if not 60 <= seconds <= 365 * 86400:
        raise ValueError("duration must be between 1 minute and 365 days")
    return seconds


def until_text(until):
    if until is None:
        return "always"
    return "until " + datetime.fromtimestamp(until).strftime("%a %H:%M")


class Asks:
    def __init__(self, app, clock=None):
        self.app, self.store, self.bus = app, app.store, app.bus
        self._clock = clock
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        # Cameron-only actions (answer, allowlist) need this; it is put into the /ui page on each load.
        self.nonce = secrets.token_urlsafe(24)
        with self.store.connect() as db:
            db.executescript(SCHEMA)

    def clock(self):
        if self._clock is not None:
            return self._clock()
        return self.app.phone.clock()

    @property
    def phone(self):
        return self.app.phone

    def check_nonce(self, data):
        given = data.get("nonce") if isinstance(data, dict) else None
        if not isinstance(given, str) or not hmac.compare_digest(given.encode(), self.nonce.encode()):
            raise BusError(403, "Only Cameron can do this (from the bus UI or the interactive CLI); agents cannot.")

    # ---- rows
    def row(self, ask_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM asks WHERE id=?", (str(ask_id).lower().removeprefix("ask-"),)).fetchone()
        return dict(row) if row else None

    def update(self, ask_id, **fields):
        fields.setdefault("updated", self.clock())
        with self.store.connect() as db:
            db.execute(f"UPDATE asks SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), ask_id))
        with self.cond:
            self.cond.notify_all()

    def rows(self, where="1=1", params=(), limit=200):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute(f"SELECT * FROM asks WHERE {where} ORDER BY created DESC, rowid DESC LIMIT ?", (*params, limit))]

    @staticmethod
    def view(row):
        out = dict(row, options=json.loads(row["options"] or "[]"), call=bool(row["call"]), not_urgent=bool(row["not_urgent"]),
                   ref="ask-" + row["id"], created_iso=iso(row["created"]), expires_iso=iso(row["expires"]),
                   answered_iso=iso(row["answered_at"]) if row["answered_at"] else None)
        out.pop("cancel_gateway", None)
        out.pop("next_attempt", None)
        return out

    # ---- allowlist and downgrades (state keys ask_allow / ask_downgrade: [{spec, until}])
    def _entries(self, key):
        now = self.clock()
        items = self.store.get_state(key, []) or []
        return [e for e in items if isinstance(e, dict) and e.get("spec") and (e.get("until") is None or e["until"] > now)]

    def allowlist(self):
        return self._entries("ask_allow")

    def downgrades(self):
        return self._entries("ask_downgrade")

    @staticmethod
    def hits(spec, agent):
        spec = spec.lower()
        if spec[:1] in "@#":
            return matches([spec], agent)
        folder, account, name = (agent.get("folder") or "").lower(), (agent.get("account") or "").lower(), (agent.get("name") or "").lower()
        return spec in (agent["id"].lower(), name, name_base(name), folder, account)

    def allowed(self, agent):
        return next((e for e in self.allowlist() if self.hits(e["spec"], agent)), None)

    def downgraded(self, agent):
        return next((e for e in self.downgrades() if self.hits(e["spec"], agent)), None)

    def set_allow(self, spec, seconds, by):
        spec = (spec or "").strip().lower()
        if not TARGET_RE.fullmatch(spec) or spec in ("all", "user", "bus", "planner"):
            raise ValueError("target must be an agent name or id, #folder or @account")
        now = self.clock()
        items = [e for e in self.allowlist() if e["spec"] != spec]
        until = None if seconds is None else now + seconds
        items.append({"spec": spec, "until": until, "by": by, "at": now})
        self.store.set_state("ask_allow", items)
        self.log_change(f"allowlist: {spec} may text Cameron questions {until_text(until)} (set by {by})")
        return {"spec": spec, "until": until, "until_iso": iso(until) if until else None}

    def remove_allow(self, spec, by):
        spec = (spec or "").strip().lower()
        before = self.allowlist()
        items = [e for e in before if e["spec"] != spec]
        self.store.set_state("ask_allow", items)
        # A stopped agent's waiting (not yet texted) asks go back to his inbox.
        held = 0
        for row in self.rows("status='waiting'"):
            agent = self.agent_info(row["agent"])
            if self.hits(spec, agent) and not self.urgent_ok(row, agent):
                self.update(row["id"], status="inbox", route="inbox", why=f"stopped by Cameron ({by})")
                held += 1
        self.log_change(f"allowlist: {spec} removed (by {by})" + (f"; {held} waiting ask(s) moved to the inbox" if held else ""))
        return {"spec": spec, "removed": len(before) - len(items), "moved_to_inbox": held}

    def downgrade(self, agent):
        spec = (agent.get("folder") or name_base(agent.get("name")) or agent["id"]).lower()
        now = self.clock()
        items = [e for e in self.downgrades() if e["spec"] != spec] + [{"spec": spec, "until": now + DOWNGRADE, "at": now}]
        self.store.set_state("ask_downgrade", items)
        self.log_change(f"downgraded: {spec}'s priority-5 asks only go to the bus inbox for 24 h (Cameron said not urgent)")
        return spec

    def log_change(self, text):
        LOG.info("Asks: %s", text)
        try:
            self.bus.send({"from": "bus", "to": ["user"], "kind": "system", "subject": "Ask Cameron", "body": text,
                           "priority": 2}, internal=True)
        except (ValueError, BusError):
            pass

    # ---- policy
    def agent_info(self, agent_id):
        row = self.bus.agent(agent_id)
        if row is None:
            return {"id": agent_id, "name": agent_id, "folder": None, "account": None, "tool": None}
        return {k: row[k] for k in ("id", "name", "folder", "account", "tool")}

    def urgent_ok(self, ask, agent):
        return ask["priority"] == 5 and bool(ask.get("reason")) and self.downgraded(agent) is None

    def policy(self, agent, priority, reason, call):
        """(route, why): text/call only for allowlisted agents or priority 5 with a reason; otherwise inbox."""
        allow = self.allowed(agent)
        down = self.downgraded(agent)
        urgent = priority == 5 and bool(reason)
        if urgent and down is None:
            if call:
                recent = self.rows("agent=? AND route='call' AND created>=?", (agent["id"], self.clock() - CALL_GAP), 1)
                if recent:
                    return "text", "priority 5 with a reason (texted, not called: this agent already rang him in the last hour)"
                return "call", "priority 5 with a reason, call requested"
            return "text", "priority 5 with a reason"
        if allow is not None:
            note = f"allowlisted ({allow['spec']}, {until_text(allow.get('until'))})"
            if urgent and down is not None:
                note += f"; not called: downgraded {until_text(down['until'])} after Cameron said not urgent"
            return "text", note
        if urgent:
            return "inbox", f"bus inbox only: downgraded {until_text(down['until'])} after Cameron said an earlier one was not urgent"
        if priority == 5:
            return "inbox", "bus inbox only: priority 5 needs a one-line urgency reason to be texted"
        return "inbox", "bus inbox only: not allowlisted and not priority 5 with a reason"

    # ---- create (agents)
    def create(self, data):
        if not isinstance(data, dict):
            raise ValueError("Send a JSON object")
        agent_id = clean(data.get("from"), "from", 80, True)
        if agent_id in ("user", "bus", "planner"):
            raise ValueError("from must be the asking agent's id")
        question = clean(data.get("question"), "question", QUESTION_MAX, True)
        options = data.get("options") or []
        if isinstance(options, str):
            options = [o for o in options.split("|")]
        if not isinstance(options, list) or len(options) > MAX_OPTIONS:
            raise ValueError(f"options must be a list of at most {MAX_OPTIONS}")
        options = [clean(o, "each option", OPTION_MAX, True) for o in options]
        priority = integer(data.get("priority"), "priority", 1, 5, 3)
        reason = clean(data.get("reason") or data.get("urgent_reason"), "reason", REASON_MAX)
        call = data.get("call", False)
        if call not in (True, False, None):
            raise ValueError("call must be true or false")
        wait = data.get("wait_seconds")
        if wait is not None and (type(wait) not in (int, float) or not 0 <= wait <= MAX_OPEN):
            raise ValueError(f"wait_seconds must be 0-{MAX_OPEN}")
        now = self.clock()
        agent = self.agent_info(agent_id)
        with self.lock:
            if len(self.rows("agent=? AND status IN ('inbox','waiting','sent')", (agent_id,), OPEN_PER_AGENT)) >= OPEN_PER_AGENT:
                raise BusError(409, f"You already have {OPEN_PER_AGENT} open questions for Cameron; wait for answers or cancel one "
                                    "(bus.py asks / POST /bus/asks/<id>/cancel).")
            route, why = self.policy(agent, priority, reason, bool(call))
            if call and route != "call" and "not called" not in why:
                why += " (call not allowed: only priority 5 with a reason may ring)"
            label = (agent.get("folder") or name_base(agent.get("name")) or agent_id)[:LABEL_MAX]
            ask_id = uuid.uuid4().hex[:16]
            row = {"id": ask_id, "created": now, "updated": now, "agent": agent_id, "label": label, "folder": agent.get("folder"),
                   "account": agent.get("account"), "question": question, "options": json.dumps(options), "priority": priority,
                   "reason": reason, "call": 1 if route == "call" else 0, "route": route, "why": why,
                   "status": "inbox" if route == "inbox" else "waiting", "expires": now + max(DEFAULT_OPEN, wait or 0),
                   "next_attempt": now}
            with self.store.connect() as db:
                db.execute(f"INSERT INTO asks ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        LOG.info("Ask %s from %s: route %s (%s)", ask_id, agent_id, route, why)
        body = question + ("\n" + ", ".join(f"{i + 1} {o}" for i, o in enumerate(options)) if options else "")
        if reason:
            body += f"\nurgent: {reason}"
        body += f"\n[ask {ask_id} · {'texted to his phone' if route != 'inbox' else 'bus inbox only'}: {why}. Answer it in the Asks panel.]"
        try:
            sent = self.bus.send({"from": agent_id, "to": ["user"], "kind": "dm", "subject": "Question for Cameron",
                                  "body": body[:4000], "priority": priority, "expects_reply": True,
                                  "meta": {"ask": {"id": ask_id, "route": route}}}, internal=True)
            self.update(ask_id, msg_id=sent["id"])
        except (ValueError, BusError):
            LOG.warning("Could not post ask %s to the bus", ask_id)
        if route != "inbox":
            self.phone.wake.set()
        result = self.view(self.row(ask_id))
        mode = self.no_text_mode()
        if route != "inbox" and mode:
            result["held"] = f"Cameron is {mode}: it is texted when that ends"
        return result

    def no_text_mode(self):
        try:
            mode = self.bus.current_state().get("mode")
        except Exception:
            return None
        return mode if mode in NO_TEXT_MODES else None

    def get(self, ask_id, wait=0):
        deadline = time.monotonic() + max(0.0, min(WAIT_MAX, wait or 0))
        while True:
            self.expire()
            row = self.row(ask_id)
            if row is None:
                raise BusError(404, "Ask not found")
            remaining = deadline - time.monotonic()
            if row["status"] in FINAL or remaining <= 0:
                return {"ask": self.view(row)}
            with self.cond:
                self.cond.wait(min(remaining, 5))

    def listing(self, agent=None, limit=50, open_only=False):
        self.expire()
        where, params = [], []
        if agent:
            where.append("agent=?")
            params.append(agent)
        if open_only:
            where.append("status IN ('inbox','waiting','sent')")
        rows = self.rows(" AND ".join(where) or "1=1", params, limit)
        counts = {}
        for r in self.rows("status IN ('inbox','waiting','sent')", (), 500):
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        now = self.clock()
        return {"asks": [self.view(r) for r in rows], "open": counts,
                "allow": [dict(e, until_iso=iso(e["until"]) if e.get("until") else None) for e in self.allowlist()],
                "downgraded": [dict(e, until_iso=iso(e["until"])) for e in self.downgrades()],
                "held_by": self.no_text_mode(), "now": now}

    def cancel(self, ask_id, data):
        row = self.row(ask_id)
        if row is None:
            raise BusError(404, "Ask not found")
        who = data.get("from") if isinstance(data, dict) else None
        if who != row["agent"]:
            self.check_nonce(data)
        if row["status"] in FINAL:
            return {"ask": self.view(row)}
        self.update(row["id"], status="canceled", cancel_gateway=1 if row["gateway_id"] else 0)
        self.phone.wake.set()
        return {"ask": self.view(self.row(row["id"]))}

    # ---- answers
    def parse_answer(self, row, text):
        t = (text or "").strip()
        word = t.lower().rstrip(".!").strip()
        if word in ("0", "not urgent"):
            return "not urgent", None, True
        options = json.loads(row["options"] or "[]")
        if re.fullmatch(r"\d", word) and options and 1 <= int(word) <= len(options):
            return options[int(word) - 1], int(word), False
        return t[:ANSWER_MAX], None, False

    def answer_ui(self, ask_id, data):
        self.check_nonce(data)
        row = self.row(ask_id)
        if row is None:
            raise BusError(404, "Ask not found")
        if row["status"] in FINAL:
            raise BusError(409, f"That question is already {row['status']}")
        text = clean(data.get("answer"), "answer", ANSWER_MAX, True)
        answer, option, not_urgent = self.parse_answer(row, text)
        self.record(row, answer, option, not_urgent, "ui")
        return {"ask": self.view(self.row(row["id"]))}

    def record(self, row, answer, option, not_urgent, via):
        with self.lock:
            current = self.row(row["id"])
            if current is None or current["status"] in FINAL:
                return
            self.update(row["id"], status="answered", answer=answer, option=option, not_urgent=1 if not_urgent else 0,
                        via=via, answered_at=self.clock(), cancel_gateway=1 if (current["gateway_id"] and via != "sms" and via != "call") else 0)
        how = {"sms": " by text", "call": " on the call", "ui": " in the bus UI"}.get(via, "")
        body = f"Cameron answered your question{how}: {answer}"
        if option:
            body += f" (option {option})"
        body += f"\nQ: {current['question'][:300]}"
        if not_urgent and current["priority"] == 5:
            spec = self.downgrade(self.agent_info(current["agent"]))
            body += (f"\nHe said it was not urgent: priority-5 asks from {spec} only go to his bus inbox for the next 24 h. "
                     "Keep urgent asks for real emergencies.")
        try:
            payload = {"from": "user", "to": [current["agent"]], "kind": "reply", "subject": "Answer from Cameron",
                       "body": body, "priority": 4,
                       "meta": {"ask": {"id": current["id"], "answer": answer, "option": option, "not_urgent": bool(not_urgent),
                                        "via": via}}}
            if current["msg_id"]:
                payload["reply_to"] = current["msg_id"]
            self.bus.send(payload, internal=True)
        except (ValueError, BusError):
            LOG.warning("Could not deliver the answer to ask %s", current["id"])
        self.phone.wake.set()

    def on_update(self, update):
        """A gateway feed entry for the bus client: question.answered or owner.command."""
        kind = update.get("type")
        if kind == "owner.command":
            return self.command(str(update.get("text") or ""))
        if kind != "question.answered":
            return None
        ref = update.get("ref")
        row = self.row(ref[4:]) if isinstance(ref, str) and ref.startswith("ask-") else None
        if row is None and update.get("question_id"):
            found = self.rows("gateway_id=?", (str(update["question_id"]),), 1)
            row = found[0] if found else None
        if row is None:
            return None
        answer = str(update.get("answer") or "")[:ANSWER_MAX]
        option = update.get("option") if type(update.get("option")) is int else None
        via = update.get("via") if update.get("via") in ("sms", "call") else "sms"
        self.record(row, answer, option, bool(update.get("not_urgent")), via)
        return row["id"]

    def command(self, text):
        """SMS from Cameron: 'allow appraiseme 2h' / 'allow #pecunia today' / 'stop appraiseme'."""
        m = COMMAND_RE.match(text or "")
        if not m:
            LOG.warning("Asks: ignored owner command %r", text[:80])
            return None
        verb, spec, dur = m[1].lower(), m[2].lower(), m[3]
        try:
            if verb == "stop":
                return self.remove_allow(spec, "sms")
            return self.set_allow(spec, parse_duration(dur, self.clock()), "sms")
        except ValueError as exc:
            self.log_change(f"could not apply texted command '{text[:80]}': {exc}")
            return None

    # ---- background (Phone.step drives it)
    def expire(self):
        now = self.clock()
        for row in self.rows("status IN ('inbox','waiting','sent') AND expires<=?", (now,), 100):
            self.update(row["id"], status="expired")
            try:
                payload = {"from": "bus", "to": [row["agent"]], "kind": "system", "subject": "Question expired",
                           "body": f"Cameron did not answer your question before it expired ({'texted' if row['gateway_id'] else row['route']}): "
                                   f"{row['question'][:200]}\nDecide yourself or ask again later.", "priority": 3,
                           "meta": {"ask": {"id": row["id"], "status": "expired"}}}
                self.bus.send(payload, internal=True)
            except (ValueError, BusError):
                pass

    def tick(self):
        """No gateway needed: expiry, the daily digest, pruning."""
        self.expire()
        self.digest()
        with self.store.connect() as db:
            db.execute("DELETE FROM asks WHERE status IN ('answered','expired','canceled') AND created<?", (self.clock() - 30 * 86400,))

    def digest(self):
        now = self.clock()
        local = datetime.fromtimestamp(now)
        if local.hour < DIGEST_HOUR:
            return
        try:
            if self.bus.current_state().get("mode") != "available":
                return
        except Exception:
            return
        day = local.strftime("%Y-%m-%d")
        if self.store.get_state("ask_digest_day") == day:
            return
        rows = self.rows("status='inbox'", (), 50)
        if not rows:
            return
        self.store.set_state("ask_digest_day", day)
        lines = [f"{r['label']}: {r['question'][:80]}" for r in rows[:5]]
        more = f" (+{len(rows) - 5} more)" if len(rows) > 5 else ""
        try:
            self.app.notify({"title": f"{len(rows)} agent question(s) in the bus UI", "message": "; ".join(lines) + more,
                             "priority": 3, "source": "bus", "event": "ask-digest", "dedupe_key": "ask-digest-" + day})
        except (ValueError, BusError):
            LOG.warning("Could not send the asks digest")

    def has_open_texts(self):
        return bool(self.rows("status='sent'", (), 1))

    def poll_every(self):
        return OPEN_POLL if self.has_open_texts() else IDLE_POLL

    def send_due(self):
        """Text due asks: held while he is studying / in lecture / sleeping; up to 5 per text; a call goes alone."""
        self.send_cancels()
        if self.no_text_mode():
            return None
        now = self.clock()
        due = list(reversed(self.rows("status='waiting' AND next_attempt<=?", (now,), 50)))
        if not due:
            return None
        calls = [r for r in due if r["route"] == "call"]
        texts = [r for r in due if r["route"] != "call"]
        for row in calls:
            if not self.call_room():
                self.update(row["id"], route="text", call=0, why=row["why"] + " (texted, not called: phone call limit reached)")
                texts.append(self.row(row["id"]))
                continue
            if self.submit([row], call=True) == "stop":
                return None
        for start in range(0, len(texts), BATCH):
            if self.submit(texts[start:start + BATCH]) == "stop":
                return None
        return None

    def call_room(self):
        settings = self.phone.settings
        now = self.clock()
        for window, key in ((3600, "max_calls_per_hour"), (86400, "max_calls_per_day")):
            used = self.store.call_count(now - window) + len(self.rows("route='call' AND gateway_id IS NOT NULL AND created>=?",
                                                                       (now - window,), 100))
            if used >= settings.get(key, 4):
                return False
        return True

    def submit(self, rows, call=False):
        url, token = self.phone.gateway
        now = self.clock()
        body = {"questions": [{"ref": "ask-" + r["id"], "agent": r["label"], "question": r["question"],
                               "options": json.loads(r["options"] or "[]"), "urgent_reason": r["reason"],
                               "expires_minutes": max(1, min(10080, int((r["expires"] - now) // 60) or 1))} for r in rows],
                "call": call}
        try:
            status, data, retry_after = self.phone.http("POST", url + "/v1/questions", token, body)
        except Exception as exc:  # GatewayDown and friends: try again in a minute
            for r in rows:
                self.update(r["id"], next_attempt=now + 60, error=f"gateway unreachable: {str(exc)[:120]}")
            return "stop"
        if status in (200, 201) and isinstance(data.get("questions"), list):
            by_ref = {q.get("ref"): q for q in data["questions"] if isinstance(q, dict)}
            for r in rows:
                q = by_ref.get("ask-" + r["id"]) or {}
                fields = {"status": "sent", "gateway_id": str(q.get("id") or "")[:40] or None, "error": None}
                if type(q.get("num")) is int:
                    fields["num"] = q["num"]
                self.update(r["id"], **fields)
                if q.get("status") == "answered":
                    self.record(self.row(r["id"]), str(q.get("answer") or ""), q.get("option"), bool(q.get("not_urgent")),
                                q.get("via") or "sms")
            return "ok"
        if status == 429:
            wait = data.get("retry_after") if type(data.get("retry_after")) is int else retry_after or 600
            wait = max(30, min(int(wait), 86400))
            for r in rows:
                self.update(r["id"], next_attempt=now + wait,
                            error=f"text limit reached; batched and retried in {wait // 60} min")
            return "stop"
        if status == 409 and len(rows) > 1:
            for r in rows:
                self.submit([r])
            return "ok"
        if status == 400:
            for r in rows:
                self.update(r["id"], status="inbox", route="inbox", why=r["why"] + "; gateway refused the text, so bus inbox only",
                            error=self.phone.detail(data))
            return "ok"
        for r in rows:
            self.update(r["id"], next_attempt=now + 120, error=f"gateway returned HTTP {status}; retrying")
        return "stop"

    def send_cancels(self):
        url, token = self.phone.gateway
        for row in self.rows("cancel_gateway=1 AND gateway_id IS NOT NULL", (), 20):
            try:
                status, _, _ = self.phone.http("POST", f"{url}/v1/questions/{urllib.parse.quote(row['gateway_id'], safe='')}/cancel",
                                               token, {})
            except Exception:
                return
            if status in (200, 404):
                self.update(row["id"], cancel_gateway=0)

    def health(self):
        counts = self.listing(limit=1)["open"]
        return {"open": counts, "allowlisted": [e["spec"] for e in self.allowlist()], "held_by": self.no_text_mode()}
