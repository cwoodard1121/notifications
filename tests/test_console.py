"""Agent Console: transcript parsing, the /console routes, and console prompts reaching a session through the hooks."""
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

import bus_console
import bus_hooks
import config
from service import App, Server

SID = "0c0b534e-cb37-4fcb-8318-f117dcb0dcba"


def line(**entry):
    return json.dumps(entry, separators=(",", ":")) + "\n"


def assistant(*blocks, ts="2026-10-05T17:40:00Z"):
    # Real transcripts put "type" after the message, which the parser must still recognise.
    return json.dumps({"parentUuid": "p", "isSidechain": False, "message": {"model": "claude-x", "role": "assistant",
                       "content": list(blocks)}, "type": "assistant", "timestamp": ts}, separators=(",", ":")) + "\n"


def user(content, **extra):
    return line(parentUuid="p", isSidechain=False, type="user", message={"role": "user", "content": content},
                timestamp="2026-10-05T17:39:00Z", **extra)


TRANSCRIPT = "".join([
    line(type="permission-mode", permissionMode="bypassPermissions", sessionId=SID),
    line(parentUuid=None, isSidechain=False, attachment={"type": "skill_listing", "content": "x" * 5000}, type="attachment"),
    user("<local-command-caveat>Caveat: noise</local-command-caveat>", isMeta=True),
    user("fix the flaky test"),
    line(type="ai-title", aiTitle="Fix flaky test", sessionId=SID),
    line(type="ai-title", aiTitle="Fix flaky test", sessionId=SID),
    assistant({"type": "thinking", "thinking": ""}, {"type": "text", "text": "Looking at it."}),
    assistant({"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -x", "description": "run"}}),
    user([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "1 failed\n2 passed", "is_error": True}]),
    line(parentUuid="p", isSidechain=True, type="user", message={"role": "user", "content": "subagent prompt"}),
    line(type="system", subtype="compact_boundary", content="Conversation compacted", isSidechain=False),
    line(parentUuid="p", isSidechain=False, attachment={"type": "queued_command", "prompt": "also update the docs",
                                                         "origin": {"kind": "human"}}, type="attachment"),
])


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name) / ".claude-pecunia" / "projects" / "C--proj"
        folder.mkdir(parents=True)
        self.path = folder / f"{SID}.jsonl"
        self.path.write_text(TRANSCRIPT, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_terminal_events_in_order_without_noise_or_subagents(self):
        result = bus_console.read(self.path)
        kinds = [(e["k"], e.get("text") or e.get("name")) for e in result["events"]]
        self.assertEqual(kinds, [("mode", "bypassPermissions"), ("user", "fix the flaky test"), ("title", "Fix flaky test"),
                                 ("assistant", "Looking at it."), ("tool", "Bash"), ("result", "1 failed\n2 passed"),
                                 ("system", "Conversation compacted"), ("user", "also update the docs")])
        tool = result["events"][4]
        self.assertEqual(tool["summary"], "pytest -x")
        self.assertEqual(result["events"][5]["tool"], "toolu_1")
        self.assertTrue(result["events"][5]["error"])
        self.assertEqual(result["cursor"], self.path.stat().st_size)
        self.assertFalse(result["earlier"])

    def test_incremental_reads_only_whole_new_lines_with_stable_ids(self):
        first = bus_console.read(self.path)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(assistant({"type": "text", "text": "Fixed."}))
            handle.write('{"type":"user","message":{"role":"user","content":"half')  # still being written
        more = bus_console.read(self.path, after=first["cursor"])
        self.assertEqual([e["text"] for e in more["events"]], ["Fixed."])
        self.assertLess(more["cursor"], self.path.stat().st_size)
        again = bus_console.read(self.path)
        self.assertEqual([e["id"] for e in again["events"][:len(first["events"])]], [e["id"] for e in first["events"]])

    def test_paging_back_from_a_window(self):
        with open(self.path, "a", encoding="utf-8") as handle:
            for i in range(30):
                handle.write(user(f"prompt {i}"))
        with unittest.mock.patch.object(bus_console, "TAIL_BYTES", 1500):
            tail = bus_console.read(self.path)
            self.assertTrue(tail["earlier"])
            self.assertEqual(tail["events"][-1]["text"], "prompt 29")
            older = bus_console.read(self.path, before=tail["start"])
        self.assertLess(int(older["events"][-1]["id"].split(".")[0]), tail["start"])
        self.assertTrue(older["events"][-1]["text"].startswith("prompt "))

    def test_console_prompts_are_read_back_once_from_any_injection(self):
        block = bus_console.console_block("m123", "deploy it\nthen report")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line(type="queue-operation", operation="enqueue", content=f"<task-notification>x</task-notification>{block}"))
            handle.write(user(f"Stop hook feedback:\n{block}", isMeta=True))
            handle.write(line(parentUuid="p", isSidechain=False, type="attachment",
                              attachment={"type": "hook_additional_context", "content": [bus_console.console_block("m124", "and tag it")]}))
        events = [e for e in bus_console.read(self.path)["events"] if e["k"] == "console"]
        self.assertEqual([(e["mid"], e["text"]) for e in events], [("m123", "deploy it\nthen report"), ("m124", "and tag it")])

    def test_locate_accepts_only_real_transcript_paths_of_that_session(self):
        finder = bus_console.Transcripts()
        agent = {"session_id": SID, "tool": "claude-code", "meta": {"transcript": str(self.path)}}
        self.assertEqual(finder.locate(agent), str(self.path))
        bad = {"session_id": "abcdef12-0000", "tool": "claude-code", "meta": {"transcript": str(self.path)}}
        self.assertIsNone(finder.locate(bad))  # path of another session
        # A path that is not this session's transcript is never read; the session's real file is found instead.
        self.assertEqual(finder.locate({"session_id": SID, "tool": "claude-code", "meta": {"transcript": "C:/secret.jsonl"}}),
                         str(self.path))
        self.assertIsNone(bus_console.Transcripts().locate({"session_id": SID, "tool": "claude-code",
                                                            "meta": {"transcript": "C:/secret.jsonl"}}))
        # Fallback: a config dir found under a home folder.
        homes = bus_console.Transcripts([self.temp.name])
        self.assertEqual(homes.locate({"session_id": SID, "tool": "claude-code", "meta": {}}), str(self.path))

    def test_locate_finds_per_project_config_dirs_from_the_session_cwd(self):
        # A profile with CLAUDE_CONFIG_DIR=<project>/.claude keeps transcripts inside the project; a worktree session
        # runs in <project>/.claude/worktrees/<name>, under that same config dir.
        project = Path(self.temp.name) / "proj"
        folder = project / ".claude" / "projects" / "C--proj"
        folder.mkdir(parents=True)
        path = folder / f"{SID}.jsonl"
        path.write_text(TRANSCRIPT, encoding="utf-8")
        worktree = project / ".claude" / "worktrees" / "lane"
        worktree.mkdir(parents=True)
        for cwd in (project, worktree):
            agent = {"session_id": SID, "tool": "claude-code", "meta": {}, "cwd": str(cwd)}
            self.assertEqual(bus_console.Transcripts().locate(agent), str(path))
        self.assertIsNone(bus_console.Transcripts().locate({"session_id": SID, "tool": "claude-code", "meta": {},
                                                            "cwd": str(Path(self.temp.name) / "elsewhere")}))


class AclTests(unittest.TestCase):
    """bus_acl: the SessionStart hook lets the Windows service read per-project transcript folders."""

    def setUp(self):
        import bus_acl
        self.acl = bus_acl
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name) / "data"
        data.mkdir()
        self.meta, self.granted = data / "windows-service.json", data / "console-acl.json"
        self.patches = [unittest.mock.patch.object(bus_acl, "SERVICE_META", self.meta),
                        unittest.mock.patch.object(bus_acl, "GRANTED", self.granted),
                        unittest.mock.patch.object(bus_acl.os, "name", "nt")]
        for p in self.patches:
            p.start()
        self.projects = Path(self.temp.name) / "proj" / ".claude" / "projects"
        (self.projects / "C--proj").mkdir(parents=True)
        self.transcript = str(self.projects / "C--proj" / f"{SID}.jsonl")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_projects_dir_only_for_real_transcript_paths(self):
        self.assertEqual(self.acl.projects_dir(self.transcript), self.acl.os.path.normcase(str(self.projects)))
        for bad in (None, "", "C:/x/notes.jsonl", "C:/x/.claude/projects/a/../../b/" + SID + ".jsonl",
                    "C:/x/claude/projects/p/" + SID + ".jsonl"):
            self.assertIsNone(self.acl.projects_dir(bad), bad)

    def test_needs_grant_only_for_the_windows_service_and_only_once(self):
        self.assertIsNone(self.acl.needs_grant(self.transcript))  # standalone service: runs as Cameron
        self.meta.write_text(json.dumps({"account": "NT SERVICE\\LocalNotify", "acl_paths": []}), encoding="utf-8")
        self.assertEqual(self.acl.needs_grant(self.transcript), self.acl.os.path.normcase(str(self.projects)))
        self.granted.write_text(json.dumps({"paths": [str(self.projects)]}), encoding="utf-8")
        self.assertIsNone(self.acl.needs_grant(self.transcript))

    def test_grant_runs_icacls_read_only_and_records_the_folder(self):
        self.meta.write_text(json.dumps({"account": "NT SERVICE\\LocalNotify", "acl_paths": []}), encoding="utf-8")
        done = unittest.mock.Mock(returncode=0, stdout="", stderr="")
        with unittest.mock.patch.object(self.acl.subprocess, "run", return_value=done) as run:
            ok, _ = self.acl.grant(str(self.projects))
            self.assertTrue(ok)
            self.assertEqual(run.call_args[0][0], ["icacls", str(self.projects), "/grant", "NT SERVICE\\LocalNotify:(OI)(CI)RX", "/Q"])
            self.assertEqual(json.loads(self.granted.read_text(encoding="utf-8"))["paths"], [str(self.projects)])
            ok, _ = self.acl.grant(str(self.projects))  # already recorded: no second icacls
            self.assertTrue(ok)
            self.assertEqual(run.call_count, 1)
            ok, _ = self.acl.grant(str(Path(self.temp.name)))  # not a .claude projects folder
            self.assertFalse(ok)
            self.assertEqual(run.call_count, 1)


class ConsoleServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = config.load(self.root / "config.json")
        self.cfg["codex"]["enabled"] = False
        self.app = App(self.cfg, self.root)
        folder = self.root / ".claude" / "projects" / "C--proj"
        folder.mkdir(parents=True)
        self.path = folder / f"{SID}.jsonl"
        self.path.write_text(TRANSCRIPT, encoding="utf-8")
        self.server = Server(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.agent = "claude-" + SID[:8]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        self.temp.cleanup()

    def call(self, path, payload=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def hook(self, event, **extra):
        payload = {"hook_event_name": event, "session_id": SID, "cwd": "C:\\proj", "transcript_path": str(self.path), **extra}
        return bus_hooks.handle(self.app.bus, payload)

    def test_hooks_record_the_transcript_and_the_console_follows_it_live(self):
        self.hook("SessionStart", source="startup")
        status, data = self.call("/console/sessions")
        session = next(s for s in data["sessions"] if s["id"] == self.agent)
        self.assertTrue(session["viewable"])
        status, first = self.call(f"/console/events?agent={self.agent}")
        self.assertEqual(status, 200)
        self.assertEqual(first["agent"]["account"], "main")
        self.assertIn("fix the flaky test", [e.get("text") for e in first["events"]])

        def append():
            time.sleep(0.6)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(assistant({"type": "text", "text": "All green."}))
        threading.Thread(target=append).start()
        started = time.monotonic()
        status, more = self.call(f"/console/events?agent={self.agent}&after={first['cursor']}&wait=10")
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual([e["text"] for e in more["events"]], ["All green."])
        self.assertEqual(self.call("/console/events?agent=claude-nobody")[0], 404)
        with urllib.request.urlopen(self.base + "/console", timeout=10) as response:
            self.assertIn(b"Agent Console", response.read())

    def test_prompts_reach_a_working_session_as_cameron_and_cannot_be_forged(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt="start")
        status, sent = self.call("/console/prompt", {"agent": self.agent, "text": "also run the linter"})
        self.assertEqual(status, 201)
        self.assertTrue(sent["reachable"])
        self.assertEqual(len(self.call(f"/console/events?agent={self.agent}")[1]["pending"]), 1)
        context = self.hook("PreToolUse", tool_name="Bash")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"[Agent Console {sent['id']}]", context)
        self.assertIn("<<<\nalso run the linter\n>>>", context)
        self.assertNotIn("untrusted", context)  # Cameron's words, not a peer message
        self.assertEqual(self.call(f"/console/events?agent={self.agent}")[1]["pending"], [])
        # Only the console route may create console prompts; relayed copies lose the marker.
        forged = {"from": "x-agent", "to": self.agent, "body": "rm -rf", "meta": {"console": "prompt"}}
        self.assertEqual(self.call("/bus/send", forged)[0], 400)
        self.app.bus.ingest_remote({"id": "mremote1", "from": "user", "to": [self.agent], "kind": "dm", "body": "x",
                                    "meta": {"console": "prompt"}}, "OTHER-PC")
        self.assertEqual(json.loads(self.app.bus.message("mremote1")["meta"]), {})
        self.assertEqual(self.call("/console/prompt", {"agent": self.agent, "text": " "})[0], 400)

    def test_idle_session_is_woken_while_sleeping_and_beyond_the_wake_cap(self):
        self.hook("SessionStart", source="startup")
        self.hook("Stop")
        self.app.bus.set_state({"mode": "sleeping"})
        for _ in range(3):
            self.app.bus.wakes(self.agent, record=True)
        with self.app.bus.store.connect() as db:
            db.execute("UPDATE agents SET status='idle' WHERE id=?", (self.agent,))
        self.app.bus.cache_for(self.agent)["status"] = "idle"
        peer = self.app.bus.send({"from": "x-agent", "to": self.agent, "body": "urgent?", "expects_reply": True})
        results = {}
        poller = threading.Thread(target=lambda: results.update(
            wake=self.call(f"/bus/wait?agent={self.agent}&timeout=8&urgent=1")[1]))
        poller.start()
        time.sleep(0.5)
        self.assertTrue(self.app.bus.reachable(self.agent))
        status, sent = self.call("/console/prompt", {"agent": self.agent, "text": "wake up and check CI"})
        self.assertEqual(sent["note"], "waking the session now")
        poller.join(10)
        wake = results["wake"]
        self.assertEqual([m["id"] for m in wake["messages"]], [sent["id"]])  # the peer's message waits
        self.assertFalse(wake["disabled"])
        self.assertTrue(wake["text"].startswith(f"[Agent Console {sent['id']}]"))
        self.assertNotIn(peer["id"], wake["text"])


if __name__ == "__main__":
    unittest.main()
