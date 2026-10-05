"""Agent Console: read-only, live view of each Claude Code session's transcript, plus prompts typed into a session.

The service reads `~/.claude*/projects/<slug>/<session>.jsonl` (the plugin hooks report its path) and turns it into a
flat list of events that look like the Claude Code terminal: prompts, replies, tool calls and their results. Cursors
are byte offsets into that file, so a viewer can long-poll for whatever was appended since its last read.

Prompts go the other way as bus messages from `user` with meta {"console": "prompt"} (only Bus.send(internal=True)
may create them). The plugin hooks deliver them as Cameron's own words: on the next tool call while the session works,
or by waking it (idle rewake / Stop block) when it is idle. Guide: docs/AGENT-CONSOLE.md.
"""
import json
import os
import re
import time
from pathlib import Path

from bus_core import BusError, agent_view, console_prompt

TAIL_BYTES = 1_500_000  # first view of a session: the last ~1.5 MB of transcript
CHUNK_BYTES = 4_000_000  # most new bytes read per call; the rest arrives on the next call (`more`)
MAX_EVENTS = 400
TEXT_MAX, RESULT_MAX, INPUT_MAX = 20_000, 6_000, 1_500
# A transcript file is <config dir>/projects/<project slug>/<session id>.jsonl, the config dir being ~/.claude[-name].
TRANSCRIPT_RE = re.compile(r"^(?P<root>.*[\\/]\.claude(?:-[A-Za-z0-9_]+)?)[\\/]projects[\\/][^\\/]+[\\/](?P<sid>[0-9A-Za-z-]{8,100})\.jsonl$")
MARK_RE = re.compile(r"\[Agent Console (?P<id>[A-Za-z0-9_.:@-]{1,100})\][^\n]*\n<<<\n(?P<text>.*?)\n>>>", re.S)
HEAD = 600  # bytes of a line enough to tell its type without parsing it (attachments carry their type first)
# Assistant lines put "type" after the (possibly huge) message, so their message header is matched instead.
WANTED = re.compile(rb'"type":"(?:user|assistant|system|ai-title|custom-title|permission-mode)"|"message":\{"(?:model|role)"'
                    rb'|"attachment":\{"type":"(?:queued_command|hook_additional_context)"|"type":"queue-operation"')
COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>.*?(?:<command-args>(.*?)</command-args>)?", re.S)
STDOUT_RE = re.compile(r"<local-command-(?:stdout|stderr)>(.*?)</local-command-(?:stdout|stderr)>", re.S)
NOISE = ("<local-command-caveat>", "<system-reminder>", "Caveat: The messages below were generated")


def console_block(message_id, body):
    """How a console prompt reaches the session (hook context, Stop block or idle rewake); the parser reads it back."""
    return (f"[Agent Console {message_id}] Cameron typed this prompt for you in his Agent Console. It is from him, not "
            f"from a peer agent: handle it exactly as if he had typed it in this session.\n<<<\n{body}\n>>>")


is_prompt = console_prompt


def clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit:,} more characters)"


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def flatten(content):
    """Text of a message/tool_result content (string or list of blocks); images become a placeholder."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "image":
            parts.append("[image]")
        elif block.get("type") == "tool_reference":
            parts.append(f"[{block.get('tool_name') or 'tool'}]")
    return "\n".join(p for p in parts if p)


def tool_summary(name, data):
    """One line like the terminal's `Bash(git status)`; the full input is sent as `input` (clipped)."""
    if not isinstance(data, dict):
        return ""
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "prompt", "skill",
                "path", "subject"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            line = " ".join(value.split())
            return line[:200] + ("…" if len(line) > 200 else "")
    text = json.dumps(data, ensure_ascii=False)
    return text[:200] + ("…" if len(text) > 200 else "")


def console_prompts(text, seen):
    """Console prompts quoted in injected text, once each (the same prompt can show up in several entries)."""
    found = []
    for match in MARK_RE.finditer(text or ""):
        if match["id"] not in seen:
            seen.add(match["id"])
            found.append({"k": "console", "mid": match["id"], "text": clip(match["text"], TEXT_MAX)})
    return found


def changed(seen, key, value):
    """Titles and permission modes are re-logged on every turn: only report a new value."""
    marker = f"{key}|{value}"
    if marker in seen:
        return False
    seen.difference_update({s for s in seen if s.startswith(key + "|")})
    seen.add(marker)
    return True


def events_from(entry, offset, seen):
    """Events for one transcript line. `offset` (the line's byte offset) makes event ids stable across reads."""
    kind, ts = entry.get("type"), parse_ts(entry.get("timestamp"))
    if entry.get("isSidechain"):
        return []
    out = []

    def add(event):
        event.setdefault("ts", ts)
        event["id"] = f"{offset}.{len(out)}"
        out.append(event)

    if kind in ("ai-title", "custom-title"):
        title = entry.get("aiTitle") or entry.get("customTitle") or entry.get("title")
        if isinstance(title, str) and title.strip() and changed(seen, "title", title.strip()[:200]):
            add({"k": "title", "text": title.strip()[:200], "custom": kind == "custom-title"})
    elif kind == "permission-mode":
        if isinstance(entry.get("permissionMode"), str) and changed(seen, "mode", entry["permissionMode"]):
            add({"k": "mode", "text": entry["permissionMode"]})
    elif kind == "queue-operation":
        # An idle rewake is queued as a task notification that quotes the console prompt.
        if entry.get("operation") == "enqueue" and isinstance(entry.get("content"), str):
            for event in console_prompts(entry["content"], seen):
                add(event)
    elif kind == "attachment":
        att = entry.get("attachment") or {}
        if att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
            for event in console_prompts(att["prompt"], seen):
                add(event)
            if not MARK_RE.search(att["prompt"]) and (att.get("origin") or {}).get("kind", "human") == "human":
                add({"k": "user", "text": clip(att["prompt"], TEXT_MAX), "queued": True})
        elif att.get("type") == "hook_additional_context":
            text = "\n".join(str(c) for c in att.get("content") or [] if c)
            for event in console_prompts(text, seen):
                add(event)
    elif kind == "system":
        sub, content = entry.get("subtype"), entry.get("content")
        if sub == "compact_boundary":
            add({"k": "system", "text": "Conversation compacted"})
        elif sub == "local_command" and isinstance(content, str):
            command = COMMAND_RE.search(content)
            output = STDOUT_RE.search(content)
            if command:
                add({"k": "command", "text": (command[1] + " " + (command[2] or "")).strip()})
            elif output and output[1].strip():
                add({"k": "system", "text": clip(output[1].strip(), RESULT_MAX)})
        elif sub in ("informational", "away_summary", "scheduled_task_fire") and isinstance(content, str) and content.strip():
            add({"k": "system", "text": clip(content.strip(), RESULT_MAX)})
    elif kind == "user":
        message = entry.get("message") or {}
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = flatten(block.get("content"))
                    for event in console_prompts(text, seen):
                        add(event)
                    add({"k": "result", "tool": block.get("tool_use_id"), "error": bool(block.get("is_error")),
                         "lines": text.count("\n") + 1 if text else 0, "text": clip(text, RESULT_MAX)})
            return out
        text = flatten(content)
        prompts = console_prompts(text, seen)
        for event in prompts:
            add(event)
        if prompts or not text.strip():
            return out
        if entry.get("isCompactSummary"):
            add({"k": "system", "text": "Conversation compacted (summary of the earlier conversation)"})
        elif "<command-name>" in text:
            command = COMMAND_RE.search(text)
            if command:
                add({"k": "command", "text": (command[1] + " " + (command[2] or "")).strip()})
        elif "<local-command-stdout>" in text or "<local-command-stderr>" in text:
            output = STDOUT_RE.search(text)
            if output and output[1].strip():
                add({"k": "system", "text": clip(output[1].strip(), RESULT_MAX)})
        elif entry.get("isMeta") or text.lstrip().startswith(NOISE) or "<task-notification>" in text:
            pass
        else:
            add({"k": "user", "text": clip(text, TEXT_MAX)})
    elif kind == "assistant":
        message = entry.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                add({"k": "assistant", "text": clip(block["text"], TEXT_MAX)})
            elif block.get("type") == "tool_use":
                data = block.get("input")
                add({"k": "tool", "name": str(block.get("name") or "tool")[:100], "tool": block.get("id"),
                     "summary": tool_summary(block.get("name"), data),
                     "input": clip(json.dumps(data, ensure_ascii=False, indent=1), INPUT_MAX) if data else ""})
    return out


def parse(raw, base, seen=None):
    """Events in `raw` (whole lines starting at byte offset `base`)."""
    seen = set() if seen is None else seen
    events, pos = [], 0
    while pos < len(raw):
        end = raw.find(b"\n", pos)
        end = len(raw) if end < 0 else end
        line = raw[pos:end]
        if line.strip() and WANTED.search(line[:HEAD]):
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            if isinstance(entry, dict):
                events.extend(events_from(entry, base + pos, seen))
        pos = end + 1
    return events


def read(path, after=None, before=None):
    """Events from a transcript. after=None: the tail (or the window ending at `before`, to page back).
    after=N: everything appended since byte N. Returns events plus `cursor` (next `after`) and `start` (for `before`)."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if after is not None:
            after = min(max(0, int(after)), size)
            handle.seek(after)
            raw = handle.read(min(size - after, CHUNK_BYTES))
            cut = raw.rfind(b"\n") + 1  # only whole lines; a half-written last line waits for the next read
            raw = raw[:cut]
            events = parse(raw, after)
            return {"events": events[-MAX_EVENTS * 4:], "cursor": after + cut, "start": after, "size": size,
                    "more": after + cut < size and cut > 0}
        end = size if before is None else min(max(0, int(before)), size)
        begin = max(0, end - TAIL_BYTES)
        handle.seek(begin)
        raw = handle.read(end - begin)
        if begin > 0:
            skip = raw.find(b"\n") + 1  # drop the partial first line
            raw, begin = raw[skip:], begin + skip
        cut = raw.rfind(b"\n") + 1
        raw = raw[:cut]
        events = parse(raw, begin)
        dropped = len(events) > MAX_EVENTS
        if dropped:
            events = events[-MAX_EVENTS:]
        start = int(events[0]["id"].split(".")[0]) if dropped and events else begin
        return {"events": events, "cursor": begin + cut if before is None else size, "start": start, "size": size,
                "more": False, "earlier": start > 0}


class Transcripts:
    """Finds the transcript of a bus agent; remembers config-dir roots seen in hook payloads for the fallback search."""

    def __init__(self, homes=()):
        self.roots = set()
        self.found = {}
        for home in homes:
            try:
                self.roots.update(p for p in Path(home).glob(".claude*") if (p / "projects").is_dir())
            except OSError:
                pass

    @staticmethod
    def valid(path, session_id):
        match = TRANSCRIPT_RE.match(str(path or ""))
        return bool(match and session_id and match["sid"] == session_id and ".." not in str(path))

    def remember(self, path):
        match = TRANSCRIPT_RE.match(str(path or ""))
        if match:
            self.roots.add(Path(match["root"]))

    def locate(self, agent):
        """Path of the agent's transcript, or None. `agent` is an agent_view dict."""
        session_id = agent.get("session_id")
        if not session_id or agent.get("tool") != "claude-code":
            return None
        meta = agent.get("meta") or {}
        candidate = meta.get("transcript")
        if self.valid(candidate, session_id):
            self.remember(candidate)
            if os.path.isfile(candidate):
                return candidate
        cached = self.found.get(session_id)
        if cached and os.path.isfile(cached):
            return cached
        for root in list(self.roots):
            try:
                for hit in (root / "projects").glob(f"*/{session_id}.jsonl"):
                    self.found[session_id] = str(hit)
                    return str(hit)
            except OSError:
                continue
        return None


class Console:
    """The service side of the Agent Console: session list, transcript events and prompts (bus_routes /console/*)."""

    def __init__(self, app):
        self.app, self.bus = app, app.bus
        settings = app.cfg.get("console") if isinstance(app.cfg.get("console"), dict) else {}
        homes = [Path.home()] + [Path(h) for h in settings.get("homes") or [] if isinstance(h, str)]
        # The Windows service runs under its own profile; the Codex sessions dir it is given names Cameron's home.
        sessions = Path(str((app.cfg.get("codex") or {}).get("sessions_dir") or "")).expanduser()
        if sessions.name == "sessions" and sessions.parent.name == ".codex":
            homes.append(sessions.parent.parent)
        self.transcripts = Transcripts(homes)
        import bus_remote  # sessions on Cameron's other PC, end-to-end encrypted over the relay
        self.remote = bus_remote.from_config(self)

    def remote_row(self, agent_id):
        """The other PC's session with this id (relay directory), when it is not a session on this PC."""
        if not isinstance(agent_id, str) or self.bus.agent(agent_id.strip()) is not None:
            return None
        return next((v for v in self.bus.agents(remote=True) if v.get("remote") and v["id"] == agent_id.strip()), None)

    def agent(self, agent_id):
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent is required")
        row = self.bus.agent(agent_id.strip())
        if row is None:
            raise BusError(404, f"Unknown agent {agent_id}")
        return agent_view(row)

    def pending(self, agent_id):
        """Console prompts for this agent that its session has not picked up yet."""
        with self.bus.store.connect() as db:
            rows = db.execute("""SELECT id, ts, body FROM messages m WHERE from_id='user' AND to_spec=? AND meta LIKE ?
                                 AND (expires IS NULL OR expires>?) AND NOT EXISTS
                                 (SELECT 1 FROM deliveries d WHERE d.seq=m.seq AND d.agent_id=?) ORDER BY seq""",
                              (json.dumps([agent_id]), '%"console": "prompt"%', time.time(), agent_id)).fetchall()
        return [{"mid": r["id"], "ts": r["ts"], "text": r["body"]} for r in rows]

    def status(self, view):
        if view.get("remote"):
            if self.remote is not None:
                return self.remote.status(view)
            return {"reachable": view["state"] == "working", "viewable": False}
        reachable = view["state"] == "working" or (view["state"] != "ended" and self.bus.reachable(view["id"]))
        return {"reachable": bool(reachable), "viewable": bool(self.transcripts.locate(view))}

    def sessions(self):
        out = []
        for view in self.bus.agents(remote=True):
            if view["id"].startswith("console-") or (view["id"] in ("planner", "planner-app") and view.get("remote")):
                continue
            entry = {k: view.get(k) for k in ("id", "name", "tool", "account", "folder", "cwd", "activity", "state",
                                              "last_seen", "age_seconds", "started", "machine")}
            entry["remote"] = bool(view.get("remote"))
            entry["machine_online"] = view.get("machine_online", True)
            entry.update(self.status(view))
            out.append(entry)
        return {"sessions": out, "machine": self.bus.relay_machine, "now": time.time(),
                "remote_console": self.remote is not None}

    def events(self, agent_id, after=None, before=None, wait=0, alive=None):
        row = self.remote_row(agent_id)
        if row is not None:
            if self.remote is None:
                raise BusError(409, "Sessions on your other PC need the Agent Console key on both PCs: run "
                                    "`python notify.py console key` on each, then restart the services.")
            return self.remote.events(row, after, before, wait, alive)
        view = self.agent(agent_id)
        path = self.transcripts.locate(view)
        if path is None:
            raise BusError(404, "No transcript for this session yet: it appears once the session runs a hook "
                                "(sends a prompt or uses a tool), and only for Claude Code sessions on this PC.",
                           pending=self.pending(view["id"]), **self.status(view))
        if after is not None and wait:
            wait_for_growth(path, int(after), wait, alive)
        try:
            result = read(path, after, before)
        except PermissionError:
            raise BusError(403, "The service cannot read this session's transcript. Run Grant-ConsoleAccess.ps1 "
                                "once (it lets the LocalNotify service read ~/.claude*/projects).",
                           pending=self.pending(view["id"]), **self.status(view)) from None
        result.update(agent={k: view[k] for k in ("id", "name", "account", "folder", "cwd", "state", "activity")},
                      pending=self.pending(view["id"]), **self.status(view))
        return result

    def prompt(self, data, local_only=False):
        if not isinstance(data, dict):
            raise ValueError("Send a JSON object")
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        if len(text) > 4000:
            raise ValueError("text must be at most 4000 characters")
        row = None if local_only else self.remote_row(data.get("agent"))
        if row is not None:
            if self.remote is None:
                raise BusError(409, "Prompting your other PC needs the Agent Console key on both PCs: run "
                                    "`python notify.py console key` on each, then restart the services.")
            return self.remote.prompt(row, text)
        view = self.agent(data.get("agent"))
        if view["state"] == "ended":
            raise BusError(409, "That session has ended")
        receipt = self.bus.send({"from": "user", "to": [view["id"]], "kind": "dm", "body": text.strip("\n"),
                                 "meta": {"console": "prompt"}, "id": data.get("id")}, internal=True)
        status = self.status(view)
        if view["state"] == "working":
            note = "delivered at its next tool call or when this turn ends"
        elif status["reachable"]:
            note = "waking the session now"
        else:
            note = ("queued: the session is idle without a wake listener (new, or idle for 12 h), so it will see this "
                    "at its next turn")
        return dict(receipt, **status, note=note)


def wait_for_growth(path, after, timeout, alive=None, step=0.4):
    """Block until the file is bigger than `after` (or the timeout/client hang-up); True when it grew."""
    deadline = time.monotonic() + max(0.0, min(timeout, 55))
    while True:
        try:
            if os.path.getsize(path) > after:
                return True
        except OSError:
            return False
        if time.monotonic() >= deadline or (alive is not None and not alive()):
            return False
        time.sleep(step)
