"""Agent Bus relay: two real App instances (machines A and B) talking through an in-process fake planner server."""
import contextlib
import http.client
import io
import json
import math
import re
import secrets
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import bus
import bus_hooks
import config
import notify
from bus_core import Bus, BusError
from service import App, Server
from store import Store

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,99}")
REF = re.compile(r"[A-Za-z0-9_.:@-]{1,100}")
ENVELOPE_ID = re.compile(r"[A-Za-z0-9_.:@/-]{1,200}")
MACHINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")


def bad_message(m):
    """The server's envelope validation (docs/BUS-RELAY.md section 3); returns an error or None."""
    if not isinstance(m, dict):
        return "message"
    if not isinstance(m.get("id"), str) or not ID.fullmatch(m["id"]):
        return "id"
    for key in ("thread", "reply_to"):
        if m.get(key) is not None and (not isinstance(m[key], str) or not REF.fullmatch(m[key])):
            return key
    if type(m.get("ts")) not in (int, float) or not math.isfinite(m["ts"]):
        return "ts"
    if not isinstance(m.get("from"), str) or not REF.fullmatch(m["from"]) or len(m["from"]) > 80 or m["from"] in ("planner", "bus"):
        return "from"
    if m.get("from_name") is not None and (not isinstance(m["from_name"], str) or len(m["from_name"]) > 100):
        return "from_name"
    if not isinstance(m.get("to"), list) or not 1 <= len(m["to"]) <= 20 or not all(isinstance(t, str) and 1 <= len(t) <= 100 for t in m["to"]):
        return "to"
    if m.get("kind") not in ("dm", "broadcast", "request", "reply"):
        return "kind"
    if m.get("subject") is not None and (not isinstance(m["subject"], str) or len(m["subject"]) > 150):
        return "subject"
    if not isinstance(m.get("body"), str) or not 1 <= len(m["body"]) <= 4000:
        return "body"
    if type(m.get("priority")) is not int or not 1 <= m["priority"] <= 5:
        return "priority"
    if type(m.get("expects_reply")) is not bool:
        return "expects_reply"
    if not isinstance(m.get("meta"), dict) or len(json.dumps(m["meta"])) > 2000:
        return "meta"
    if m.get("expires") is not None and type(m["expires"]) not in (int, float):
        return "expires"
    return None


class RelayData:
    """Everything the fake server stores; it survives a server restart on the same port."""

    def __init__(self):
        self.cond = threading.Condition()
        self.envelopes, self.ids, self.seq = [], set(), 0
        self.state = {"version": 0, "origin": None, "updatedAt": None, "state": None}
        self.machines, self.tokens, self.codes, self.requests = {}, {}, {}, []
        self.fail = {}  # (machine lower or "*", route) -> HTTP status
        self.accept_then_fail = set()  # machines whose next push is stored but answered with 500
        self.closing = False
        self.pull_limit = 200

    def machine(self, name):
        key = name.lower()
        if key not in self.machines:
            self.machines[key] = {"id": name, "agents": None, "agents_at": None, "seen": None}
        return self.machines[key]

    def mint(self, name):
        token = secrets.token_urlsafe(32)
        self.tokens = {t: m for t, m in self.tokens.items() if m.lower() != name.lower()}
        self.tokens[token] = self.machine(name)["id"]
        return token

    def add(self, origin, message, dest=None):
        envelope_id = f"{origin}/{message['id']}"
        with self.cond:
            if envelope_id in self.ids:
                return False
            self.seq += 1
            self.ids.add(envelope_id)
            self.envelopes.append({"seq": self.seq, "id": envelope_id, "origin": origin, "dest": dest, "message": message})
            self.cond.notify_all()
        return True

    def set_state(self, origin, state):
        with self.cond:
            self.state = {"version": self.state["version"] + 1, "origin": origin, "updatedAt": int(time.time() * 1000),
                          "state": state}
            self.cond.notify_all()
            return self.state["version"]

    def visible(self, machine, after):
        low = machine.lower()
        return [e for e in self.envelopes if e["seq"] > after and e["origin"].lower() != low
                and (e["dest"] is None or low in [d.lower() for d in e["dest"]])]

    def by_message(self, msg_id):
        return [e for e in self.envelopes if e["message"]["id"] == msg_id]


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def caller(self, data, route):
        auth = self.headers.get("Authorization", "")
        name = data.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        if name is None:
            self.reply(401, {"error": "auth"})
            return None
        status = data.fail.get((name.lower(), route)) or data.fail.get(("*", route))
        if status:
            self.reply(status, {"error": "injected"})
            return None
        data.machine(name)["seen"] = time.time()
        return name

    def do_GET(self):
        data, url = self.server.data, urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        data.requests.append({"method": "GET", "path": url.path, "query": query, "headers": dict(self.headers)})
        if url.path != "/api/bus/relay/pull":
            return self.reply(404, {"error": "not_found"})
        name = self.caller(data, "pull")
        if name is None:
            return
        after = int(query["after"]) if query.get("after", "").isdigit() else None
        wait = min(25, max(0, int(query.get("wait") or 0)))
        known = int(query["state"]) if query.get("state", "").isdigit() else None
        deadline = time.monotonic() + wait
        with data.cond:
            items = []
            while after is not None:
                items = data.visible(name, after)
                if items or data.closing or (known is not None and data.state["version"] != known):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                data.cond.wait(remaining)
            page = items[:data.pull_limit]
            answer = {"envelopes": [{k: e[k] for k in ("seq", "id", "origin", "message")} for e in page],
                      "latest": data.seq, "more": len(items) > len(page), "state": dict(data.state), "now": int(time.time() * 1000)}
        self.reply(200, answer)

    def do_POST(self):
        data, path = self.server.data, urlsplit(self.path).path
        body = self.body()
        data.requests.append({"method": "POST", "path": path, "body": body, "headers": dict(self.headers)})
        if self.headers.get("X-Planner") != "1":
            return self.reply(403, {"error": "csrf"})
        if path == "/api/bus/relay/pair":
            code, machine = body.get("code"), body.get("machine")
            if not isinstance(machine, str) or not MACHINE.fullmatch(machine):
                return self.reply(400, {"error": "bad_machine"})
            if code not in data.codes:
                return self.reply(400, {"error": "bad_code"})
            if data.codes[code] == "used":
                return self.reply(410, {"error": "code_used"})
            data.codes[code] = "used"
            stored = data.machine(machine)["id"]
            return self.reply(200, {"token": data.mint(stored), "machine": stored, "createdAt": int(time.time() * 1000)})
        if path == "/api/bus/relay/push":
            name = self.caller(data, "push")
            if name is None:
                return
            accepted, duplicates, rejected = [], [], []
            for envelope in body.get("envelopes") or []:
                error = bad_message(envelope.get("message"))
                if not ENVELOPE_ID.fullmatch(str(envelope.get("id"))) or envelope.get("type") != "message":
                    error = error or "envelope"
                if error:
                    rejected.append({"id": envelope.get("id"), "error": error})
                    continue
                dest = envelope.get("dest")
                dest = None if dest is None else [d for d in dest if d.lower() != name.lower()]
                (accepted if data.add(name, envelope["message"], dest) else duplicates).append(envelope["id"])
            state = {"version": data.set_state(name, body["state"])} if "state" in body else None
            if name.lower() in data.accept_then_fail:
                data.accept_then_fail.discard(name.lower())
                return self.reply(500, {"error": "lost answer"})
            return self.reply(200, {"accepted": accepted, "duplicates": duplicates, "rejected": rejected, "state": state,
                                    "latest": data.seq})
        if path == "/api/bus/relay/agents":
            name = self.caller(data, "agents")
            if name is None:
                return
            me = data.machine(name)
            me.update(agents=body.get("agents") or [], agents_at=time.time())
            machines = [{"id": m["id"], "online": bool(m["seen"] and time.time() - m["seen"] < 90),
                         "busSeenAt": int((m["seen"] or 0) * 1000), "agentsAt": int(m["agents_at"] * 1000), "agents": m["agents"]}
                        for key, m in data.machines.items() if key != name.lower() and m["agents"] is not None]
            return self.reply(200, {"ok": True, "machines": machines})
        self.reply(404, {"error": "not_found"})


class FakeRelay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, data, port=0):
        super().__init__(("127.0.0.1", port), RelayHandler)
        self.data = data
        data.closing = False
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server_port}"

    def close(self):
        with self.data.cond:
            self.data.closing = True  # ends the long-polls in flight
            self.data.cond.notify_all()
        self.shutdown()
        self.server_close()
        self.thread.join()


def wait_for(check, timeout=15.0, message="condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        time.sleep(0.05)


def http_json(port, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


class Node:
    """One PC: an App with the relay enabled and its own HTTP server."""

    def __init__(self, root, url, token, machine):
        root.mkdir(parents=True, exist_ok=True)
        cfg = config.load(root / "config.json")
        cfg["codex"]["enabled"] = False
        # Never let a test touch the real study mailbox folder from config.example.json.
        cfg["bus"]["mailbox"] = {"enabled": False, "dirs": [], "agent_id": "study-claude", "poll_seconds": 1}
        cfg["relay"] = {"url": url, "token": token, "machine": machine}
        self.cfg, self.machine = cfg, machine
        self.app = App(cfg, root)
        relay = self.app.relay
        relay.backoff_min, relay.backoff_max, relay.directory_every, relay.pull_wait, relay.idle = 0.1, 0.5, 0.3, 3, 0.5
        self.server = Server(("127.0.0.1", 0), self.app)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.app.start()

    @property
    def bus(self):
        return self.app.bus

    def stop(self):
        self.app.relay.stop_event.set()

    def close(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def agent(self, agent_id, name, account, folder):
        return self.bus.upsert(agent_id, rename=True, name=name, account=account, folder=folder, status="working")

    def send(self, **payload):
        status, result = http_json(self.port, "/bus/send", payload)
        if status >= 400:
            raise AssertionError(f"send failed: {status} {result}")
        return result

    def inbox(self, agent_id):
        return self.bus.inbox(agent_id, mark=False, limit=100, touch=False)["messages"]

    def pending(self):
        return self.app.store.get_state("relay_state_pending")


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.data = RelayData()
        self.fake = FakeRelay(self.data)
        self.a = Node(self.root / "a", self.fake.url, self.data.mint("agents-pc"), "agents-pc")
        self.b = Node(self.root / "b", self.fake.url, self.data.mint("school-pc"), "school-pc")
        self.a.agent("claude-aaaa0001", "pecunia-1", "pecunia", "pecunia")
        self.a.agent("claude-aaaa0002", "pecunia-2", "pecunia", "pecunia")
        self.a.agent("claude-aaaa0003", "capresta", "capresta", "capresta")
        self.b.agent("claude-bbbb0001", "waitlist-bot", "appraiseme", "waitlist")
        self.b.agent("claude-bbbb0002", "pecunia-web", "pecunia", "pecunia-web")
        for here, there in ((self.a, "claude-bbbb0002"), (self.b, "claude-aaaa0003")):
            wait_for(lambda: any(r["id"] == there for r in here.bus.remote_rows()), message=f"directory on {here.machine}")
        wait_for(lambda: self.a.app.relay.connected() and self.b.app.relay.connected(), message="both connected")

    def tearDown(self):
        for node in (self.a, self.b):
            node.stop()
        self.fake.close()
        for node in (self.a, self.b):
            node.close()
        self.temp.cleanup()

    def ids(self, node, agent_id):
        return [m["id"] for m in node.inbox(agent_id)]

    # ---- messages
    def test_dm_to_a_remote_agent_and_the_reply_keep_their_ids_and_thread(self):
        sent = self.a.send(**{"from": "claude-aaaa0001", "to": "claude-bbbb0001", "body": "Is the waitlist export done?",
                              "expects_reply": True})
        self.assertEqual((sent["recipients"], sent["relayed"]), ([], True))
        self.assertEqual(sent["remote"], [{"id": "claude-bbbb0001", "machine": "school-pc"}])
        got = wait_for(lambda: [m for m in self.b.inbox("claude-bbbb0001") if m["id"] == sent["id"]], message="DM on B")[0]
        self.assertEqual((got["origin"], got["from"], got["from_name"], got["thread"], got["expects_reply"]),
                         ("agents-pc", "claude-aaaa0001", "pecunia-1", sent["id"], True))
        envelope = self.data.by_message(sent["id"])
        self.assertEqual([(e["id"], e["dest"]) for e in envelope], [(f"agents-pc/{sent['id']}", ["school-pc"])])
        reply = self.b.send(**{"from": "claude-bbbb0001", "reply_to": sent["id"], "body": "Yes, shipped."})
        self.assertEqual(reply["to"], ["claude-aaaa0001"])
        self.assertEqual(reply["remote"], [{"id": "claude-aaaa0001", "machine": "agents-pc"}])
        back = wait_for(lambda: [m for m in self.a.inbox("claude-aaaa0001") if m["id"] == reply["id"]], message="reply on A")[0]
        self.assertEqual((back["kind"], back["thread"], back["reply_to"], back["origin"]), ("reply", sent["id"], sent["id"], "school-pc"))
        self.assertEqual([m["id"] for m in self.a.bus.thread(sent["id"])["messages"]], [sent["id"], reply["id"]])
        self.assertEqual([m["id"] for m in self.b.bus.thread(sent["id"])["messages"]], [sent["id"], reply["id"]])
        # The reply never echoes back to B, and A never re-pushes what it pulled.
        time.sleep(0.5)
        self.assertEqual(len(self.data.by_message(reply["id"])), 1)
        self.assertEqual(len(self.data.by_message(sent["id"])), 1)
        # Labels: hook injection, CLI inbox and the mailbox file all name the machine.
        self.assertIn("from pecunia-1 (claude-aaaa0001) on agents-pc", bus_hooks.injection({"messages": [got]}))
        self.assertIn("on agents-pc", bus.message_head(got))

    def test_broadcast_and_group_targets_reach_both_machines(self):
        cast = self.a.send(**{"from": "claude-aaaa0001", "to": "all", "body": "Restarting the shared DB in 2 min"})
        self.assertEqual(cast["kind"], "broadcast")
        self.assertEqual(set(cast["recipients"]), {"claude-aaaa0002", "claude-aaaa0003"})
        self.assertEqual({r["id"] for r in cast["remote"]}, {"claude-bbbb0001", "claude-bbbb0002"})
        for agent in ("claude-bbbb0001", "claude-bbbb0002"):
            wait_for(lambda: cast["id"] in self.ids(self.b, agent), message=f"broadcast for {agent}")
        self.assertIn(cast["id"], self.ids(self.a, "claude-aaaa0003"))
        self.assertIsNone(self.data.by_message(cast["id"])[0]["dest"])
        acct = self.a.send(**{"from": "claude-aaaa0003", "to": "@pecunia", "body": "pecunia folks: rebase on main"})
        folder = self.a.send(**{"from": "claude-aaaa0003", "to": "#waitlist", "body": "waitlist: export ok?"})
        wait_for(lambda: acct["id"] in self.ids(self.b, "claude-bbbb0002"), message="@pecunia on B")
        wait_for(lambda: folder["id"] in self.ids(self.b, "claude-bbbb0001"), message="#waitlist on B")
        self.assertEqual(set(acct["recipients"]), {"claude-aaaa0001", "claude-aaaa0002"})
        self.assertNotIn(acct["id"], self.ids(self.b, "claude-bbbb0001"))
        self.assertNotIn(folder["id"], self.ids(self.b, "claude-bbbb0002"))
        for agent in ("claude-aaaa0001", "claude-aaaa0002"):
            self.assertNotIn(folder["id"], self.ids(self.a, agent))

    def test_local_messages_stay_local_and_unknown_ids_go_everywhere(self):
        local = self.a.send(**{"from": "claude-aaaa0001", "to": "pecunia-2", "body": "local only"})
        self.assertEqual((local["to"], local["relayed"], local["remote"]), (["claude-aaaa0002"], False, []))
        unknown = self.a.send(**{"from": "claude-aaaa0001", "to": "codex-somewhere", "body": "anyone know codex-somewhere?"})
        self.assertTrue(unknown["relayed"])
        wait_for(lambda: self.data.by_message(unknown["id"]), message="unknown id pushed")
        self.assertIsNone(self.data.by_message(unknown["id"])[0]["dest"])
        wait_for(lambda: self.a.app.store.get_state("relay_push_seq") >= unknown["seq"], message="push cursor")
        self.assertEqual(self.data.by_message(local["id"]), [])
        self.assertIsNone(self.b.bus.message(local["id"]))
        wait_for(lambda: self.b.bus.message(unknown["id"]), message="unknown id on B")

    def test_planner_targets_go_to_the_server_only_and_planner_messages_need_the_planner_origin(self):
        to_planner = self.a.send(**{"from": "claude-aaaa0001", "to": "planner", "body": "Done with the deck"})
        to_app = self.a.send(**{"from": "claude-aaaa0001", "to": "planner-app", "body": "Queued the sync"})
        self.assertTrue(to_planner["relayed"] and to_app["relayed"])
        wait_for(lambda: self.data.by_message(to_planner["id"]) and self.data.by_message(to_app["id"]), message="planner pushes")
        self.assertEqual([e["dest"] for e in self.data.by_message(to_planner["id"]) + self.data.by_message(to_app["id"])], [[], []])
        # Cameron writes from the planner's Agents page: origin "planner", from "planner".
        note = {"id": "m0123456789ab", "ts": time.time(), "from": "planner", "from_name": "Cameron (planner)",
                "to": ["claude-bbbb0001"], "kind": "dm", "subject": None, "body": "Ship the export tonight", "priority": 3,
                "thread": "m0123456789ab", "reply_to": None, "expects_reply": True, "meta": {}, "expires": None}
        self.data.add("planner", note, ["school-pc"])
        got = wait_for(lambda: [m for m in self.b.inbox("claude-bbbb0001") if m["id"] == note["id"]], message="planner DM")[0]
        self.assertEqual((got["from"], got["from_name"], got["origin"]), ("planner", "Cameron (planner)", "planner"))
        latest = self.data.seq
        wait_for(lambda: (self.b.app.store.get_state("relay_pull_seq") or 0) >= latest, message="B cursor past planner traffic")
        self.assertIsNone(self.b.bus.message(to_planner["id"]))
        self.assertIsNone(self.b.bus.message(to_app["id"]))
        answer = self.b.send(**{"from": "claude-bbbb0001", "reply_to": note["id"], "body": "Will do"})
        self.assertEqual(answer["to"], ["planner"])
        wait_for(lambda: self.data.by_message(answer["id"]), message="reply to the planner pushed")
        self.assertEqual(self.data.by_message(answer["id"])[0]["dest"], [])
        # A PC can never speak as the planner: refused locally, and refused from a machine envelope.
        status, error = http_json(self.a.port, "/bus/send", {"from": "planner", "to": "all", "body": "fake Cameron"})
        self.assertEqual(status, 400)
        with self.assertRaises(ValueError):
            self.b.bus.ingest_remote(dict(note, id="m0000000000ff"), "agents-pc")
        self.assertIsNone(self.b.bus.message("m0000000000ff"))

    def test_notify_and_system_messages_are_never_relayed(self):
        before = len(self.data.envelopes)
        result = self.a.app.notify({"message": "Prod is down", "priority": 5, "source": "claude-aaaa0001", "call": True})
        self.assertIn(result["status"], ("local", "queued"))
        self.a.bus.set_state({"mode": "studying"})  # writes a `bus` system notice locally
        wait_for(lambda: self.data.state["version"] >= 1, message="state push")
        time.sleep(0.5)
        self.assertEqual(len(self.data.envelopes), before)
        self.assertEqual(self.b.app.store.recent(), [])
        self.assertTrue(all(m["from"] != "bus" or m["origin"] is None for m in self.b.bus.history(200)["messages"]))

    def test_offline_server_queues_and_retries_without_loss_or_duplicates(self):
        port = self.fake.server_port
        self.fake.close()
        sent = [self.a.send(**{"from": "claude-aaaa0001", "to": "claude-bbbb0001", "body": f"queued {n}"})["id"] for n in range(3)]
        # Windows takes ~2 s to refuse a loopback connection.
        wait_for(lambda: self.a.app.relay.status()["status"] == "offline", message="A offline")
        self.assertEqual(self.data.by_message(sent[0]), [])
        self.data.accept_then_fail.add("agents-pc")  # the first push after the restart is stored but its answer is lost
        self.fake = FakeRelay(self.data, port)
        for msg_id in sent:
            wait_for(lambda: msg_id in self.ids(self.b, "claude-bbbb0001"), message=f"{msg_id} after the restart")
        time.sleep(0.5)
        for msg_id in sent:
            self.assertEqual(len(self.data.by_message(msg_id)), 1)
        with self.b.app.store.connect() as db:
            counts = db.execute(f"SELECT id, COUNT(*) FROM messages WHERE id IN ({','.join('?' * len(sent))}) GROUP BY id", sent).fetchall()
        self.assertEqual(sorted(tuple(r) for r in counts), sorted((msg_id, 1) for msg_id in sent))
        self.assertTrue(any(r["path"] == "/api/bus/relay/push" for r in self.data.requests))
        wait_for(lambda: self.a.app.relay.status()["connected"], message="A reconnected")

    def test_duplicate_pull_envelopes_are_ignored(self):
        message = {"id": "mfeedfacecafe", "ts": time.time(), "from": "claude-aaaa0001", "from_name": "pecunia-1",
                   "to": ["claude-bbbb0001"], "kind": "dm", "subject": None, "body": "twice", "priority": 3,
                   "thread": "mfeedfacecafe", "reply_to": None, "expects_reply": False, "meta": {}, "expires": None}
        envelope = {"seq": 10 ** 6, "id": "agents-pc/mfeedfacecafe", "origin": "agents-pc", "message": message}
        relay = self.b.app.relay
        with self.b.app.store.connect() as db:
            before = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        relay.handle_pull({"envelopes": [envelope, envelope], "latest": self.data.seq, "more": False, "state": None})
        relay.handle_pull({"envelopes": [envelope], "latest": self.data.seq, "more": False, "state": None})
        with self.b.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], before + 1)
        self.assertEqual(self.ids(self.b, "claude-bbbb0001").count("mfeedfacecafe"), 1)

    # ---- state
    def test_state_replicates_and_holds_phone_pings_on_the_other_pc(self):
        self.a.bus.set_state({"mode": "studying", "note": "calc midterm"})
        state = wait_for(lambda: (lambda s: s if s["mode"] == "studying" else None)(self.b.bus.current_state()),
                         message="studying on B")
        self.assertEqual((state["note"], state["set_by"], state["relay_origin"], state["phone_min_priority"]),
                         ("calc midterm", "user", "agents-pc", 4))
        self.assertEqual(state["expires_at"], self.a.bus.current_state()["expires_at"])
        held = self.b.app.notify({"message": "routine", "priority": 3})
        self.assertEqual(held["status"], "held")
        self.assertNotEqual(self.b.app.notify({"message": "needs him", "priority": 4})["status"], "held")
        notices = [m for m in self.b.bus.history(50, kind="system")["messages"] if m["subject"] == "Cameron is now: Studying"]
        self.assertEqual(len(notices), 1)
        self.assertIsNone(self.b.pending())  # a relayed state is never pushed back
        self.a.bus.set_state({"mode": "available"})
        wait_for(lambda: self.b.bus.current_state()["mode"] == "available", message="available on B")
        self.assertEqual(self.data.state["version"], 2)

    def test_concurrent_changes_converge_on_the_servers_last_writer(self):
        self.data.fail[("*", "push")] = 503
        self.a.bus.set_state({"mode": "gym"})
        self.b.bus.set_state({"mode": "focus"})
        time.sleep(0.4)
        self.assertEqual((self.a.pending()["mode"], self.b.pending()["mode"]), ("gym", "focus"))
        del self.data.fail[("*", "push")]
        wait_for(lambda: self.data.state["version"] == 2, message="both pushed")
        winner = self.data.state["state"]["mode"]
        wait_for(lambda: self.a.bus.current_state()["mode"] == winner == self.b.bus.current_state()["mode"],
                 message="both on the server's last writer")
        wait_for(lambda: self.a.pending() is None and self.b.pending() is None, message="nothing pending")
        time.sleep(0.5)
        self.assertEqual(self.data.state["version"], 2)
        self.assertEqual(self.a.bus.current_state()["mode"], self.b.bus.current_state()["mode"])

    def test_a_pending_local_change_is_not_overwritten_by_an_older_pulled_state(self):
        self.data.fail[("school-pc", "push")] = 503
        self.b.bus.set_state({"mode": "focus", "note": "essay"})
        self.a.bus.set_state({"mode": "gym"})
        wait_for(lambda: self.data.state["version"] == 1, message="A's push")
        wait_for(lambda: self.b.app.relay.seen_state_version == 1, message="B saw version 1")
        time.sleep(0.3)
        self.assertEqual(self.b.bus.current_state()["mode"], "focus")
        self.assertEqual(self.b.pending()["mode"], "focus")
        del self.data.fail[("school-pc", "push")]
        wait_for(lambda: self.data.state["version"] == 2, message="B's push")
        wait_for(lambda: self.a.bus.current_state()["mode"] == "focus", message="focus on A")
        self.assertEqual(self.b.bus.current_state()["mode"], "focus")
        self.assertEqual(self.a.bus.current_state()["relay_origin"], "school-pc")

    def test_expiry_is_never_pushed(self):
        self.a.bus.set_state({"mode": "studying"})
        wait_for(lambda: self.data.state["version"] == 1 and self.a.pending() is None, message="studying pushed")
        with self.a.bus.write_lock:
            self.a.bus.state = dict(self.a.bus.state, expires_at=time.time() - 1)
        self.assertEqual(self.a.bus.current_state()["set_by"], "expiry")
        time.sleep(0.6)
        self.assertIsNone(self.a.pending())
        self.assertEqual(self.data.state["version"], 1)

    def test_first_connection_pushes_a_busy_local_state_to_an_empty_server(self):
        relay = self.a.app.relay
        relay.close()
        self.a.bus.on_local_state = None  # a state set before the relay was configured
        self.a.bus.set_state({"mode": "lecture"})
        self.assertIsNone(self.a.pending())
        self.assertFalse(relay.handle_state({"version": 0, "origin": None, "updatedAt": None, "state": None}))
        self.assertEqual(self.a.pending()["mode"], "lecture")
        with self.a.app.store.connect() as db:
            db.execute("DELETE FROM state WHERE key='relay_state_pending'")
        self.a.bus.set_state({"mode": "available"})
        self.assertFalse(relay.handle_state({"version": 0, "origin": None, "updatedAt": None, "state": None}))
        self.assertIsNone(self.a.pending())

    # ---- directory, CLI, health
    def test_remote_agents_are_listed_only_with_remote_and_named_by_machine(self):
        status, local = http_json(self.a.port, "/bus/agents")
        self.assertEqual(status, 200)
        self.assertEqual({a["id"] for a in local["agents"]}, {"claude-aaaa0001", "claude-aaaa0002", "claude-aaaa0003"})
        self.assertTrue(all("machine" not in a for a in local["agents"]))
        status, both = http_json(self.a.port, "/bus/agents?remote=1")
        far = [a for a in both["agents"] if a.get("remote")]
        self.assertEqual({a["id"] for a in far}, {"claude-bbbb0001", "claude-bbbb0002"})
        self.assertTrue(all(a["machine"] == "school-pc" and a["state"] == "working" for a in far))
        self.assertTrue(all(a["machine"] == "agents-pc" for a in both["agents"] if not a.get("remote")))
        client = bus.Client(port=self.a.port, token="")
        me = {"id": "claude-aaaa0001", "source": "--as"}
        data, text = bus.execute("agents", bus.build_parser("agents").parse_args([]), client, me)
        self.assertIn("school-pc:appraiseme/waitlist (waitlist-bot)", text)
        self.assertIn("pecunia/pecunia (pecunia-1)", text)
        self.assertNotIn("agents-pc:", text)
        self.assertEqual({a["id"] for a in data["agents"]}, {"claude-aaaa0001", "claude-aaaa0002", "claude-aaaa0003"})
        self.assertEqual({a["id"] for a in data["remote_agents"]}, {"claude-bbbb0001", "claude-bbbb0002"})
        sent, text = bus.execute("send", bus.build_parser("send").parse_args(["waitlist-bot", "hello", "there"]), client, me)
        self.assertEqual(sent["to"], ["claude-bbbb0001"])
        self.assertIn("+ relayed to school-pc: claude-bbbb0001", text)
        wait_for(lambda: sent["id"] in self.ids(self.b, "claude-bbbb0001"), message="name-resolved DM on B")
        # B goes offline: its agents show offline on A after the next directory sync.
        self.data.machines["school-pc"]["seen"] = time.time() - 600
        self.b.app.relay.stop_event.set()
        self.data.fail[("school-pc", "pull")] = self.data.fail[("school-pc", "agents")] = 503
        wait_for(lambda: {a["state"] for a in self.a.bus.agents(remote=True) if a.get("remote")} == {"offline"},
                 message="offline remote agents")

    def test_health_and_relay_endpoint_never_show_the_token(self):
        token = self.a.cfg["relay"]["token"]
        status, health = http_json(self.a.port, "/health")
        relay = health["bus"]["relay"]
        self.assertEqual((relay["enabled"], relay["machine"], relay["server"], relay["connected"]),
                         (True, "agents-pc", self.fake.url, True))
        self.assertNotIn(token, json.dumps(health))
        self.assertEqual(relay["remote_machines"], [{"id": "school-pc", "online": True, "agents": 2}])
        status, direct = http_json(self.a.port, "/bus/relay")
        self.assertEqual((status, direct["machine"]), (200, "agents-pc"))
        self.assertNotIn(token, json.dumps(direct))
        self.assertIn("GET /bus/relay", http_json(self.a.port, "/")[1]["endpoints"])
        auth = [r["headers"].get("Authorization") for r in self.data.requests if r["path"].startswith("/api/bus/relay/")]
        self.assertTrue(auth and all(a and a.startswith("Bearer ") for a in auth))
        self.assertTrue(all(r["headers"].get("X-Planner") == "1" for r in self.data.requests))

    def test_a_revoked_token_reports_unauthorized(self):
        self.data.tokens = {t: m for t, m in self.data.tokens.items() if m != "agents-pc"}
        self.a.send(**{"from": "claude-aaaa0001", "to": "all", "body": "anyone?"})
        wait_for(lambda: self.a.app.relay.status()["status"] == "unauthorized", message="unauthorized")
        self.assertFalse(self.a.app.relay.status()["connected"])


class BusRelayUnitTests(unittest.TestCase):
    """Routing and resolution without servers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = config.load(Path(self.temp.name) / "config.json")
        self.bus = Bus(Store(Path(self.temp.name) / "n.sqlite3"), cfg)
        self.bus.relay_machine = "agents-pc"
        self.bus.upsert("claude-aaaa0001", name="pecunia-1", account="pecunia", folder="pecunia")
        self.bus.set_remote_agents([
            {"id": "school-pc", "online": True, "agentsAt": time.time() * 1000, "agents": [
                {"id": "claude-bbbb0001", "name": "twin", "account": "appraiseme", "folder": "waitlist", "state": "idle"},
                {"id": "claude-bbbb0002", "name": "solo", "account": "pecunia", "folder": "web", "state": "working"}]},
            {"id": "lab-pc", "online": False, "agentsAt": time.time() * 1000, "agents": [
                {"id": "claude-cccc0001", "name": "twin", "account": "general", "folder": "lab", "state": "working"}]},
            {"id": "agents-pc", "online": True, "agents": [{"id": "claude-self", "name": "me"}]}])

    def tearDown(self):
        self.temp.cleanup()

    def test_route_table(self):
        route = self.bus.route
        self.assertEqual(route(["claude-aaaa0001"]), (False, []))
        self.assertEqual(route(["pecunia-1"]), (False, []))
        self.assertEqual(route(["claude-bbbb0002"]), (True, ["school-pc"]))
        self.assertEqual(route(["solo", "claude-cccc0001"]), (True, ["lab-pc", "school-pc"]))
        for group in ("all", "*", "@pecunia", "#web", "tool:codex", "user", "nobody-known"):
            self.assertEqual(route([group]), (True, None), group)
        self.assertEqual(route(["planner"]), (True, []))
        self.assertEqual(route(["planner-app", "claude-bbbb0002"]), (True, ["school-pc"]))
        self.assertEqual(route(["planner", "all"]), (True, None))
        self.assertNotIn("claude-self", [r["id"] for r in self.bus.remote_rows()])

    def test_resolution_prefers_local_then_remote_and_reports_ambiguity(self):
        self.assertEqual(self.bus.resolve(["solo"], self.bus.agent_rows(), self.bus.remote_rows()), ["claude-bbbb0002"])
        self.assertEqual(self.bus.resolve(["claude-cccc"], self.bus.agent_rows(), self.bus.remote_rows()), ["claude-cccc0001"])
        with self.assertRaises(BusError):
            self.bus.resolve(["claude-bbbb"], self.bus.agent_rows(), self.bus.remote_rows())
        self.assertEqual(self.bus.resolve(["claude-bbbb0001"], self.bus.agent_rows(), self.bus.remote_rows()), ["claude-bbbb0001"])
        # "twin": one online (school-pc) beats one on an offline machine.
        self.assertEqual(self.bus.resolve(["twin"], self.bus.agent_rows(), self.bus.remote_rows()), ["claude-bbbb0001"])
        self.bus.set_remote_agents([{"id": "school-pc", "online": True, "agents": [{"id": "claude-bbbb0001", "name": "twin"}]},
                                    {"id": "lab-pc", "online": True, "agents": [{"id": "claude-cccc0001", "name": "twin"}]}])
        with self.assertRaises(BusError) as error:
            self.bus.send({"from": "claude-aaaa0001", "to": "twin", "body": "which one?"})
        self.assertEqual(error.exception.status, 409)
        self.assertEqual({c["machine"] for c in error.exception.payload["candidates"]}, {"school-pc", "lab-pc"})
        self.bus.upsert("claude-aaaa0002", name="twin")
        self.assertEqual(self.bus.send({"from": "claude-aaaa0001", "to": "twin", "body": "local wins"})["to"], ["claude-aaaa0002"])

    def test_planner_is_reserved_and_receipts_without_relay_are_unchanged(self):
        with self.assertRaises(ValueError):
            self.bus.upsert("planner")
        with self.assertRaises(ValueError):
            self.bus.send({"from": "planner", "to": "all", "body": "x"})
        self.bus.relay_machine = None
        receipt = self.bus.send({"from": "claude-aaaa0001", "to": "all", "body": "hi"})
        self.assertEqual((receipt["relayed"], receipt["remote"]), (False, []))
        self.assertEqual(self.bus.agents(), [a for a in self.bus.agents() if "machine" not in a])

    def test_old_message_tables_gain_the_origin_column(self):
        path = Path(self.temp.name) / "old.sqlite3"
        Store(path)
        import sqlite3
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, ts REAL, from_id TEXT,
                      from_name TEXT, to_spec TEXT, kind TEXT, subject TEXT, body TEXT, priority INTEGER, thread TEXT,
                      reply_to TEXT, expects_reply INTEGER, meta TEXT, expires REAL, claimed_by TEXT)""")
        db.execute("INSERT INTO messages (id, ts, from_id, to_spec, kind, body, priority, thread, expects_reply, meta) "
                   "VALUES ('mold', ?, 'x', '[\"all\"]', 'broadcast', 'old', 3, 'mold', 0, '{}')", (time.time(),))
        db.commit()
        db.close()
        old = Bus(Store(path), config.load(Path(self.temp.name) / "config.json"))
        self.assertIsNone(old.history(10)["messages"][0]["origin"])
        Bus(Store(path), config.load(Path(self.temp.name) / "config.json"))  # idempotent


def browser_get(port, path, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()
    return response.status, body


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.data = RelayData()
        self.fake = FakeRelay(self.data)
        self.path = self.root / "config.json"
        # A throwaway second instance: its own config file (port 0 = picked below) and data folder.
        self.path.write_text(json.dumps({"provider": "local", "api_token": "", "codex": {"enabled": False},
                                         "bus": {"mailbox": {"enabled": False, "dirs": []}}}), encoding="utf-8-sig")
        self.apps = []

    def tearDown(self):
        for app, server, thread in self.apps:
            if app.relay:
                app.relay.stop_event.set()
        self.fake.close()
        for app, server, thread in self.apps:
            app.close()
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def serve(self):
        cfg = config.load(self.path)
        app = App(cfg, self.root / "data")
        if app.relay:
            app.relay.backoff_min, app.relay.backoff_max, app.relay.directory_every, app.relay.pull_wait = 0.1, 0.5, 0.3, 3
        server = Server(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        app.start()
        self.apps.append((app, server, thread))
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        raw["port"] = server.server_port
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        return app, server

    def browser(self, code, answers):
        def opener(link):
            parts = urlsplit(link)
            route, _, query = parts.fragment.partition("?")
            params = {k: v[0] for k, v in parse_qs(query).items()}
            answers["link"] = (f"{parts.scheme}://{parts.netloc}", route, params)

            def visit():
                port, nonce = int(params["port"]), params["nonce"]
                answers["wrong_nonce"] = browser_get(port, f"/planner-link?code={code}&nonce={'x' * 32}")[0]
                answers["wrong_host"] = browser_get(port, f"/planner-link?code={code}&nonce={nonce}", host=f"localhost:{port}")[0]
                answers["page"] = browser_get(port, f"/planner-link?code={code}&nonce={nonce}")

            threading.Thread(target=visit, daemon=True).start()
            return True
        return opener

    def test_pair_status_and_unpair_end_to_end(self):
        old_app, _ = self.serve()  # running without the relay, like a standalone instance before pairing
        self.assertFalse(old_app.relay_health()["enabled"])
        self.data.codes["link-code-1"] = "fresh"
        answers, out = {}, io.StringIO()
        with contextlib.redirect_stdout(out):
            result = notify.relay_pair("school-pc", self.fake.url, opener=self.browser("link-code-1", answers),
                                       path=self.path, wait=5)
        self.assertEqual(result, 0)
        origin, route, params = answers["link"]
        self.assertEqual((origin, route, params["purpose"], params["machine"]), (self.fake.url, "/link-desktop", "bus", "school-pc"))
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{16,64}", params["nonce"]) and 1024 <= int(params["port"]) <= 65535)
        self.assertEqual((answers["wrong_nonce"], answers["wrong_host"], answers["page"][0]), (403, 403, 200))
        self.assertIn("Paired", answers["page"][1])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((saved["relay"]["url"], saved["relay"]["machine"]), (self.fake.url, "school-pc"))
        token = saved["relay"]["token"]
        self.assertEqual(self.data.tokens[token], "school-pc")
        self.assertEqual((saved["provider"], saved["bus"]["mailbox"]["enabled"]), ("local", False))  # the rest is preserved
        self.assertNotIn(token, out.getvalue())
        self.assertIn(".\\Start-Notify.ps1 -Restart", out.getvalue())  # standalone: no automatic reload
        pair = [r for r in self.data.requests if r["path"] == "/api/bus/relay/pair"]
        self.assertEqual((pair[0]["body"], pair[0]["headers"].get("X-Planner")), ({"code": "link-code-1", "machine": "school-pc"}, "1"))
        # "Restart": a new instance loads the paired config and connects.
        old_app.close()
        app, server = self.serve()
        self.assertTrue(app.relay_health()["enabled"])
        wait_for(lambda: app.relay.connected(), message="paired instance connected")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(notify.relay_status(path=self.path), 0)
            self.assertEqual(notify.relay_status(True, path=self.path), 0)
        text = out.getvalue()
        self.assertIn("Relay: connected - this PC is school-pc", text)
        self.assertIn('"machine": "school-pc"', text)
        self.assertNotIn(token, text)
        # A used code cannot pair twice.
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError) as error:
            notify.relay_pair("school-pc", self.fake.url, opener=self.browser("link-code-1", {}), path=self.path, wait=0)
        self.assertIn("already used", str(error.exception))
        with contextlib.redirect_stdout(io.StringIO()):
            notify.relay_unpair(self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((saved["relay"]["token"], saved["relay"]["machine"]), ("", "school-pc"))
        self.assertFalse(config.relay_settings(config.load(self.path))[3])

    def test_cli_routes_and_no_browser_prints_the_link(self):
        from unittest.mock import patch
        seen = []
        with patch.object(notify, "relay_pair", lambda *a, **k: seen.append(("pair", a)) or 0), \
                patch.object(notify, "relay_status", lambda *a, **k: seen.append(("status", a)) or 0), \
                patch.object(notify, "relay_unpair", lambda *a, **k: seen.append(("unpair", a)) or 0):
            for argv in (["relay", "pair", "--machine", "lab", "--url", "https://x.example", "--no-browser"],
                         ["relay", "status", "--json"], ["relay", "unpair"]):
                with patch("sys.argv", ["notify.py", *argv]):
                    self.assertEqual(notify.main(), 0)
        self.assertEqual(seen, [("pair", ("lab", "https://x.example", True)), ("status", (True,)), ("unpair", ())])
        self.data.codes["link-code-2"] = "fresh"
        out, answers = io.StringIO(), {}
        printed = threading.Event()

        class Watch(io.StringIO):
            def write(self, text):
                found = re.search(r"#/link-desktop\?(\S+)", text)
                if found:
                    answers["query"] = {k: v[0] for k, v in parse_qs(found.group(1)).items()}
                    printed.set()
                return super().write(text)

        def visit():
            printed.wait(10)
            q = answers["query"]
            answers["page"] = browser_get(int(q["port"]), f"/planner-link?code=link-code-2&nonce={q['nonce']}")

        threading.Thread(target=visit, daemon=True).start()
        out = Watch()
        with contextlib.redirect_stdout(out):
            notify.relay_pair("lab-pc", self.fake.url, no_browser=True, opener=lambda link: self.fail("browser opened"),
                              path=self.path, wait=0, fetch=lambda: {"runtime": "windows-service", "bus": {}})
        self.assertEqual(answers["page"][0], 200)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["relay"]["machine"], "lab-pc")
        self.assertIn("no relay support", out.getvalue())
        # The Windows service has not restarted with the new config yet (its restart delay): saved, not a failure.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = notify.relay_wait(self.path, 0, fetch=lambda: {
                "runtime": "windows-service", "bus": {"relay": {"enabled": False, "machine": "lab-pc"}}})
        self.assertEqual(result, 0)
        self.assertIn("Pairing is saved", out.getvalue())
        self.assertNotIn("pair it with", out.getvalue())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.temp.name) / "config.json"

    def tearDown(self):
        self.temp.cleanup()

    def load(self, relay):
        self.path.write_text(json.dumps({"relay": relay}), encoding="utf-8")
        return config.load(self.path)

    def test_relay_section_validation(self):
        self.assertEqual(config.load(self.path)["relay"], {"url": "", "token": "", "machine": ""})
        good = [{"url": "https://146-190-249-220.sslip.io", "token": "a" * 43, "machine": "MSI"},
                {"url": "https://planner.example.com:8443/base/", "token": "Ab_-" * 10},
                {"url": "http://127.0.0.1:9000", "token": "", "machine": "school.pc-2"}, {"url": "http://localhost:8080/"}]
        for relay in good:
            with self.subTest(relay=relay):
                self.load(relay)
        bad = [{"url": "http://planner.example.com"}, {"url": "ftp://x"}, {"url": "https://a b"}, {"url": 5},
               {"token": "short"}, {"token": "a" * 101}, {"token": "has space" * 5}, {"token": None},
               {"machine": "-leading"}, {"machine": "x" * 41}, {"machine": "bad name"}, {"machine": 3}]
        for relay in bad:
            with self.subTest(relay=relay), self.assertRaises(ValueError):
                self.load(relay)
        with self.assertRaises(ValueError):
            self.path.write_text(json.dumps({"relay": []}), encoding="utf-8")
            config.load(self.path)

    def test_default_machine_is_the_cleaned_hostname(self):
        import socket
        from unittest.mock import patch
        for host, expected in (("MSI", "MSI"), ("DESKTOP-ABC 12", "DESKTOP-ABC12"), ("..héllo..", "hllo.."), ("", "pc"),
                               ("x" * 50, "x" * 40), ("___", "pc")):
            with patch.object(socket, "gethostname", return_value=host):
                self.assertEqual(config.default_machine(), expected)
        url, token, machine, enabled = config.relay_settings({"relay": {"url": "https://x.example/", "token": "t" * 20, "machine": ""}})
        self.assertEqual((url, enabled), ("https://x.example", True))
        self.assertEqual(machine, config.default_machine())
        self.assertFalse(config.relay_settings({"relay": {"url": "https://x.example", "token": ""}})[3])


if __name__ == "__main__":
    unittest.main()
