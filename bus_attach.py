"""`bus.py attach` / `bus.py sessions`: the Agent Console in a terminal, ssh style.

attach <agent> prints the session the way Claude Code shows it (› prompts, ● replies and tool calls, ⎿ results), keeps
following it live, and sends every line you type as a console prompt (your own words, not a peer message). The
browser version is http://127.0.0.1:8765/console; both use the service's /console/* routes (bus_console.py).
"""
import os
import shutil
import sys
import textwrap
import threading
import time

RESET, BOLD, DIM, GREEN, RED, AMBER, GREY = "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[90m"
HELP = """Agent Console (terminal)
  python bus.py sessions                 sessions on this PC (and other PCs) with what the console can do
  python bus.py attach [agent]           watch a session live and prompt it; no agent = pick from a list
      --watch                            read only: never sends what you type
      --lines N                          how many recent events to show first (default 60)
Inside attach: type a line + Enter to prompt the session, /exit (or Ctrl+C) to leave, /help for this text."""


def color_ok():
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # turns on ANSI escape handling in the Windows console
    return True


class Painter:
    def __init__(self, colors):
        self.c = colors
        self.lock = threading.Lock()

    def s(self, code, text):
        return f"{code}{text}{RESET}" if self.c else text

    def width(self):
        return max(40, min(shutil.get_terminal_size((100, 30)).columns, 160))

    def block(self, glyph, text, code="", indent=2):
        width = self.width() - indent
        lines = []
        for raw in str(text).replace("\r", "").split("\n"):
            lines.extend(textwrap.wrap(raw, width, replace_whitespace=False, drop_whitespace=False) or [""])
        head = self.s(code, glyph) + " " if glyph else " " * indent
        return head + ("\n" + " " * indent).join(lines)

    def event(self, e, tools):
        k = e.get("k")
        if k in ("user", "console"):
            tag = " (via console)" if k == "console" else " (queued)" if e.get("queued") else ""
            return "\n" + self.block("›", e.get("text", ""), GREY) + self.s(AMBER, tag)
        if k == "assistant":
            return "\n" + self.block("●", e.get("text", ""))
        if k == "tool":
            tools[e.get("tool")] = e
            return "\n" + self.s(GREEN, "●") + " " + self.s(BOLD, e.get("name", "tool")) + f"({e.get('summary', '')})"
        if k == "result":
            text = str(e.get("text") or "").replace("\r", "").strip("\n")
            lines = text.split("\n") if text else []
            shown = "\n".join(line[: self.width() - 6] for line in lines[:3]) or "(no output)"
            more = f"\n… +{len(lines) - 3} lines" if len(lines) > 3 else ""
            body = self.block("", shown + more, indent=5)[5:]
            return "  " + self.s(GREY, "⎿  ") + self.s(RED if e.get("error") else DIM, body)
        if k == "command":
            return "\n" + self.s(GREY, "› " + e.get("text", ""))
        if k == "system":
            return self.s(DIM, self.block("※", e.get("text", "")))
        if k == "title":
            return self.s(DIM, f"── {e.get('text')} ──")
        return None

    def out(self, text):
        with self.lock:
            sys.stdout.write("\r\x1b[2K" if self.c else "\n")
            sys.stdout.write(text + "\n")
            sys.stdout.flush()


def ago(seconds):
    if seconds is None:
        return "-"
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if seconds < limit:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds // 86400)}d"


def where(s, here):
    return f"{s['machine'] + ':' if s.get('remote') else ''}{s.get('account') or '?'}/{s.get('folder') or '?'}"


def sessions_text(data):
    rows = [s for s in data.get("sessions") or [] if s.get("state") in ("working", "idle") or s.get("remote")]
    if not rows:
        return "No live sessions."
    out = [f"  {'#':>2}  {'ID':<16} {'WHERE':<34} {'STATE':<8} {'AGE':>4}  CONSOLE"]
    for i, s in enumerate(rows, 1):
        if s.get("remote") and not s.get("viewable"):
            can = "other PC: needs `notify.py console key` there"
        else:
            can = (("view + " if s.get("viewable") else "prompt only, ") +
                   ("prompt now" if s.get("reachable") else "prompt (queued to next turn)") +
                   (" (encrypted relay)" if s.get("remote") else ""))
        name = f" ({s['name']})" if s.get("name") and s["name"] not in (s.get("folder"), s["id"]) else ""
        out.append(f"  {i:>2}  {s['id']:<16} {(where(s, data.get('machine')) + name)[:34]:<34} {s.get('state', ''):<8} "
                   f"{ago(s.get('age_seconds')):>4}  {can}")
    return "\n".join(out)


def pick(client, wanted):
    data = client.get("/console/sessions")
    sessions = data.get("sessions") or []
    if wanted:
        low = wanted.lower()
        for test in (lambda s: s["id"].lower() == low, lambda s: (s.get("name") or "").lower() == low,
                     lambda s: s["id"].lower().startswith(low), lambda s: (s.get("folder") or "").lower() == low):
            hits = [s for s in sessions if test(s) and (not s.get("remote") or s.get("viewable"))]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ValueError(f"'{wanted}' matches {len(hits)} sessions: " + ", ".join(s["id"] for s in hits))
        raise ValueError(f"no session '{wanted}' to attach to. Run: python bus.py sessions")
    live = [s for s in sessions if s.get("state") in ("working", "idle") or s.get("remote")]
    print(sessions_text(data))
    if not live:
        raise ValueError("nothing to attach to")
    answer = input("\nAttach to # (or id): ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(live):
        chosen = live[int(answer) - 1]
        if chosen.get("remote") and not chosen.get("viewable"):
            raise ValueError(f"{chosen['machine']} has no Agent Console key yet: run python notify.py console key there")
        return chosen
    return pick(client, answer)


def follow(client, agent_id, cursor, painter, tools, stop):
    failures = 0
    while not stop.is_set():
        try:
            data = client.get("/console/events", {"agent": agent_id, "after": cursor, "wait": 25}, timeout=40)
        except Exception as exc:  # service restart, network blip: keep trying quietly, say so once
            failures += 1
            if failures == 2:
                painter.out(painter.s(AMBER, f"[console] lost the service ({exc}); retrying…"))
            stop.wait(min(15, 2 ** failures))
            continue
        if failures >= 2:
            painter.out(painter.s(AMBER, "[console] reconnected"))
        failures = 0
        cursor = data.get("cursor", cursor)
        for e in data.get("events") or []:
            text = painter.event(e, tools)
            if text:
                painter.out(text)


def attach(client, wanted=None, watch=False, lines=60):
    painter = Painter(color_ok())
    session = pick(client, wanted)
    agent_id = session["id"]
    tools, cursor = {}, None
    head = f"{agent_id} · {where(session, None)} · {session.get('cwd') or ''}"
    print(painter.s(BOLD, "Agent Console") + painter.s(DIM, f"  {head}"))
    try:
        data = client.get("/console/events", {"agent": agent_id}, timeout=40)
    except Exception as exc:  # no transcript (yet): you can still prompt it
        print(painter.s(AMBER, f"[console] can't show this session: {getattr(exc, 'data', {}).get('error') or exc}"))
        data = None
    if data:
        cursor = data.get("cursor")
        for e in (data.get("events") or [])[-max(1, lines):]:
            text = painter.event(e, tools)
            if text:
                print(text)
    status = ("working: your prompt lands at its next step" if session.get("state") == "working" else
              "idle: sending wakes it" if session.get("reachable") else "idle with no wake listener: prompts wait for its next turn")
    print(painter.s(DIM, f"\n── attached ({status}). {'Read only. ' if watch else 'Type to prompt, '}/exit or Ctrl+C to leave ──"))
    stop = threading.Event()
    if cursor is not None:
        threading.Thread(target=follow, args=(client, agent_id, cursor, painter, tools, stop), daemon=True).start()
    try:
        while True:
            try:
                line = input()
            except EOFError:
                if watch:
                    while True:
                        time.sleep(3600)
                return 0
            text = line.strip()
            if not text:
                continue
            if text in ("/exit", "/quit", "/detach"):
                return 0
            if text == "/help":
                painter.out(HELP)
                continue
            if watch:
                painter.out(painter.s(DIM, "[console] read only (--watch): not sent"))
                continue
            try:
                res = client.post("/console/prompt", {"agent": agent_id, "text": text})
                painter.out(painter.s(AMBER, f"[console] sent ({res.get('note')})"))
            except Exception as exc:
                painter.out(painter.s(RED, f"[console] not sent: {getattr(exc, 'data', {}).get('error') or exc}"))
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()
        print(painter.s(DIM, "── detached ──"))


def main(command, argv, client):
    import argparse
    if command == "sessions":
        data = client.get("/console/sessions")
        print(sessions_text(data))
        print("\nAttach: python bus.py attach <id|name>   ·   browser: http://127.0.0.1:8765/console")
        return 0
    parser = argparse.ArgumentParser(prog="bus.py attach", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agent", nargs="?")
    parser.add_argument("--watch", action="store_true", help="read only")
    parser.add_argument("--lines", type=int, default=60)
    args = parser.parse_args(argv)
    return attach(client, args.agent, args.watch, args.lines)
