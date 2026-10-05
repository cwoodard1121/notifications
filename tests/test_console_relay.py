"""Agent Console across two PCs: watch and prompt a session on the other machine through the (fake) planner relay,
end to end encrypted with the shared key; the relay only ever sees ciphertext."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import bus_crypto
import bus_hooks
from bus_core import BusError
from test_relay import FakeRelay, Node, RelayData, http_json, wait_for

SID = "a1b2c3d4-0000-4000-8000-000000000001"


def line(entry):
    return json.dumps(entry, separators=(",", ":")) + "\n"


def prompt(text):
    return line({"parentUuid": "p", "isSidechain": False, "type": "user", "message": {"role": "user", "content": text},
                 "timestamp": "2026-10-05T17:39:00Z"})


def reply(text):
    return line({"parentUuid": "p", "isSidechain": False, "message": {"model": "m", "role": "assistant",
                 "content": [{"type": "text", "text": text}]}, "type": "assistant", "timestamp": "2026-10-05T17:40:00Z"})


class ConsoleRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.key = bus_crypto.encode_key(os.urandom(32))
        self.env = mock.patch.dict(os.environ, {"AGENT_CONSOLE_KEY": self.key})
        self.env.start()
        self.data = RelayData()
        self.fake = FakeRelay(self.data)
        self.a = Node(self.root / "a", self.fake.url, self.data.mint("agents-pc"), "agents-pc")
        self.b = Node(self.root / "b", self.fake.url, self.data.mint("school-pc"), "school-pc")
        folder = self.root / "home" / ".claude-pecunia" / "projects" / "C--pecunia"
        folder.mkdir(parents=True)
        self.path = folder / f"{SID}.jsonl"
        self.path.write_text(prompt("ship the invoice export") + reply("On it: the export is in reports.py."), encoding="utf-8")
        bus_hooks.handle(self.a.bus, {"hook_event_name": "SessionStart", "session_id": SID, "cwd": "C:\\pecunia",
                                      "transcript_path": str(self.path), "source": "startup"})
        bus_hooks.handle(self.a.bus, {"hook_event_name": "UserPromptSubmit", "session_id": SID, "cwd": "C:\\pecunia",
                                      "transcript_path": str(self.path), "prompt": "ship the invoice export"})
        self.agent = "claude-" + SID[:8]
        for here, there in ((self.a, "console-school-pc"), (self.b, "console-agents-pc"), (self.b, self.agent)):
            wait_for(lambda: any(r["id"] == there for r in here.bus.remote_rows()), message=f"{there} on {here.machine}")

    def tearDown(self):
        for node in (self.a, self.b):
            node.stop()
        self.fake.close()
        for node in (self.a, self.b):
            node.close()
        self.env.stop()
        self.temp.cleanup()

    def test_watch_a_session_on_the_other_pc_live_and_prompt_it(self):
        sessions = http_json(self.b.port, "/console/sessions")[1]
        row = next(s for s in sessions["sessions"] if s["id"] == self.agent)
        self.assertTrue(row["remote"] and row["viewable"])
        self.assertFalse(any(s["id"].startswith("console-") for s in sessions["sessions"]))

        status, first = http_json(self.b.port, f"/console/events?agent={self.agent}")
        self.assertEqual(status, 200, first)
        self.assertEqual([e["text"] for e in first["events"]], ["ship the invoice export", "On it: the export is in reports.py."])
        self.assertTrue(first["remote"])

        def append():
            time.sleep(0.5)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(reply("Export shipped."))
        threading.Thread(target=append).start()
        later = self.b.app.console.events(self.agent, after=first["cursor"], wait=20)
        if not later["events"]:  # a status-only frame can arrive first
            later = self.b.app.console.events(self.agent, after=first["cursor"], wait=20)
        self.assertEqual([e["text"] for e in later["events"]], ["Export shipped."])

        status, sent = http_json(self.b.port, "/console/prompt", {"agent": self.agent, "text": "now add a CSV test"})
        self.assertEqual(status, 201, sent)
        self.assertEqual(sent["machine"], "agents-pc")
        context = bus_hooks.handle(self.a.bus, {"hook_event_name": "PreToolUse", "session_id": SID, "cwd": "C:\\pecunia",
                                                "transcript_path": str(self.path), "tool_name": "Bash"})
        self.assertIn("<<<\nnow add a CSV test\n>>>", context["hookSpecificOutput"]["additionalContext"])

        # The planner server only ever held ciphertext, and the frames are not in either PC's visible history.
        stored = json.dumps(self.data.envelopes, default=str)
        self.assertIn("console-agents-pc", stored)  # the frames did go through the server
        for secret in ("invoice", "Export shipped", "CSV test", "reports.py"):
            self.assertNotIn(secret, stored)
        for node in (self.a, self.b):
            self.assertFalse(any((m["meta"] or {}).get("rc") for m in node.bus.history(500)["messages"]))

    def test_frames_with_another_key_or_in_plain_text_are_dropped(self):
        remote_a = self.a.app.console.remote
        self.b.app.console.remote.key = os.urandom(32)  # the other PC has a different SSH key
        self.b.app.console.remote.send("agents-pc", {"t": "prompt", "rid": "x1", "agent": self.agent, "text": "rm -rf"})
        self.b.bus.send({"from": "console-school-pc", "to": ["console-agents-pc"], "kind": "dm", "body": "plain text",
                         "meta": {"noinject": True, "rc": 1}}, internal=True)
        wait_for(lambda: remote_a.dropped >= 2, message="both frames dropped")
        self.assertEqual(self.a.app.console.pending(self.agent), [])
        # No answer comes back for a request sealed with the wrong key.
        self.assertIsNone(self.b.app.console.remote.request("agents-pc", {"t": "page", "agent": self.agent, "before": 1},
                                                            timeout=2))

    def test_without_the_key_the_other_pc_is_listed_but_not_viewable(self):
        self.a.app.console.remote.close()
        with self.a.bus.store.connect() as db:
            db.execute("DELETE FROM agents WHERE id='console-agents-pc'")
        wait_for(lambda: not any(r["id"] == "console-agents-pc" for r in self.b.bus.remote_rows()), timeout=20,
                 message="console-agents-pc leaves the directory")
        row = next(s for s in http_json(self.b.port, "/console/sessions")[1]["sessions"] if s["id"] == self.agent)
        self.assertFalse(row["viewable"])
        status, error = http_json(self.b.port, f"/console/events?agent={self.agent}")
        self.assertEqual(status, 409)
        self.assertIn("console key", error["error"])


if __name__ == "__main__":
    unittest.main()
