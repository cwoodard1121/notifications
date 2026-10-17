"""Remote ops (docs/REMOTE-OPS.md): the allowlist, the encrypted round trip between two PCs through the (fake) relay,
refusals (unknown actions, bad names/refs, reboot without confirm, wrong key, replay, stale, helper not installed),
the privileged helper's actions with the OS layer mocked, and the real named pipe's ACL."""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import bus_crypto
import bus_remote
import remote_ops
import remote_ops_helper as helper
from bus_core import BusError
from test_relay import FakeRelay, Node, RelayData, http_json, wait_for


class AllowlistTests(unittest.TestCase):
    def test_allowed_requests(self):
        self.assertEqual(remote_ops.validate("status"), ("status", {}))
        self.assertEqual(remote_ops.validate("service-restart", {"name": "Parsec"}), ("service-restart", {"name": "parsec"}))
        self.assertEqual(remote_ops.validate("reboot", {"confirm": True}), ("reboot", {"confirm": True}))
        self.assertEqual(remote_ops.validate("self-update", {"ref": "ops-20261018-1"})[1], {"ref": "ops-20261018-1"})
        self.assertEqual(remote_ops.validate("self-update", {"ref": "a" * 40})[1], {"ref": "a" * 40})

    def test_refusals(self):
        bad = [("shell", {}), ("status", {"cmd": "whoami"}), ("service-start", {"name": "spooler"}),
               ("service-start", {"name": "parsec; shutdown"}), ("service-restart", {}), ("reboot", {}),
               ("reboot", {"confirm": "yes"}), ("self-update", {"ref": "main"}), ("self-update", {"ref": "--upload-pack=x"}),
               ("self-update", {"ref": "ops-20261018-1 && calc"}), ("self-update", {"ref": "A" * 40}),
               ("tailscale-up", {"flags": "--reset"}), (None, {}), ("status", "x")]
        for action, args in bad:
            with self.subTest(action=action, args=args), self.assertRaises(ValueError):
                remote_ops.validate(action, args)

    def test_service_sid_matches_windows(self):
        # Well-known: NT SERVICE\TrustedInstaller.
        self.assertEqual(helper.service_sid("TrustedInstaller"),
                         "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
        self.assertEqual(helper.pipe_sddl("S-1-5-80-1"), "D:P(A;;GA;;;SY)(A;;GRGW;;;S-1-5-80-1)")


class FakeScm:
    def __init__(self, states):
        self.states, self.calls = dict(states), []

    def state(self, name):
        return self.states.get(name)

    def start(self, name):
        self.calls.append(("start", name))
        self.states[name] = "running"
        return True

    def stop(self, name):
        self.calls.append(("stop", name))
        self.states[name] = "stopped"
        return True

    def wait(self, name, want, timeout):
        return self.states.get(name) == want


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data = Path(self.temp.name)
        self.scm = FakeScm({"ParsecSvc": "stopped", "Tailscale": "running", "sshd": "running", "LocalNotify": "running"})
        self.commands = []
        self.outputs = {}

        def runner(argv, timeout):
            self.commands.append(list(argv))
            key = os.path.basename(argv[0]).lower() + " " + (argv[1] if len(argv) > 1 else "")
            handler = self.outputs.get(key)
            return handler(argv) if callable(handler) else (handler or (0, ""))
        self.ex = helper.Executor(scm=self.scm, runner=runner, data_dir=self.data,
                                  find_image=lambda image: "ParsecSvc" if image == "pservice.exe" else None,
                                  user=lambda: "DESKTOP\\camer", uptime=lambda: 3600, sleep=lambda s: None, restart_delay=0)

    def tearDown(self):
        self.temp.cleanup()

    def test_status(self):
        self.outputs["tailscale.exe status"] = (0, json.dumps({"BackendState": "Running",
                                                              "Self": {"TailscaleIPs": ["100.64.1.2", "fd7a::1"]}}))
        answer = self.ex.handle({"action": "status"})
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]
        self.assertEqual(result["services"]["parsec"], {"service": "ParsecSvc", "state": "stopped"})
        self.assertEqual(result["services"]["crd"]["state"], "not installed")
        self.assertEqual(result["tailscale"], {"installed": True, "backend": "Running", "ipv4": "100.64.1.2"})
        self.assertEqual(result["console_user"], "DESKTOP\\camer")
        self.assertFalse(result["self_update"])

    def test_service_restart_and_missing_service(self):
        answer = self.ex.handle({"action": "service-restart", "args": {"name": "parsec"}})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.scm.calls, [("start", "ParsecSvc")])  # it was stopped: no stop needed
        self.scm.calls.clear()
        self.ex.handle({"action": "service-restart", "args": {"name": "tailscale"}})
        self.assertEqual(self.scm.calls, [("stop", "Tailscale"), ("start", "Tailscale")])
        missing = self.ex.handle({"action": "service-start", "args": {"name": "crd"}})
        self.assertEqual((missing["ok"], missing["status"]), (False, 404))
        self.assertIn("not installed", missing["error"])

    def test_helper_validates_again(self):
        for request in ({"action": "run", "args": {"cmd": "calc"}}, {"action": "service-start", "args": {"name": "spooler"}},
                        {"action": "reboot"}, "not an object"):
            answer = self.ex.handle(request)
            self.assertEqual((answer["ok"], answer["status"]), (False, 400), request)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.scm.calls, [])

    def test_reboot_uses_fixed_argv(self):
        answer = self.ex.handle({"action": "reboot", "args": {"confirm": True}})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.commands[0][1:], ["/r", "/t", "60", "/c", "Agent Bus remote reboot"])
        self.assertTrue(self.commands[0][0].lower().endswith("shutdown.exe"))

    def test_tailscale_up_falls_back_to_set(self):
        self.outputs["tailscale.exe up"] = (1, "Error: changing settings via 'tailscale up' requires mentioning all "
                                               "non-default flags. To proceed, either re-run your command with --reset")
        answer = self.ex.handle({"action": "tailscale-up"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([c[1:] for c in self.commands[:2]], [["up", "--unattended", "--timeout=40s"], ["set", "--unattended=true"]])
        self.assertEqual(answer["result"]["command"], "tailscale set --unattended=true")

    def test_self_update_needs_allowed_signers(self):
        answer = self.ex.handle({"action": "self-update", "args": {"ref": "ops-20261018-1"}})
        self.assertEqual((answer["ok"], answer["status"]), (False, 403))
        self.assertIn("allowed_signers", answer["error"])
        self.assertEqual(self.commands, [])

    def test_self_update_runs_the_task_and_restarts_localnotify(self):
        (self.data / "allowed_signers").write_text('* namespaces="git" ssh-ed25519 AAAA\n')

        def task(argv):  # stands in for the update task in Cameron's session
            self.assertEqual(argv[1:], ["/Run", "/TN", "AgentBusOps Self-Update"])
            request = json.loads((self.data / "update" / "request.json").read_text())
            self.assertEqual(request["ref"], "ops-20261018-1")
            (self.data / "update" / "result.json").write_text(json.dumps(
                {"id": request["id"], "ok": True, "changed": True, "before": "a" * 40, "after": "b" * 40}))
            return 0, "SUCCESS"
        self.outputs["schtasks.exe /Run"] = task
        answer = self.ex.handle({"action": "self-update", "args": {"ref": "ops-20261018-1"}})
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["result"]["changed"])
        wait_for(lambda: ("start", "LocalNotify") in self.scm.calls, timeout=5, message="LocalNotify restarted")

    def test_self_update_refused_by_the_task(self):
        (self.data / "allowed_signers").write_text('* namespaces="git" ssh-ed25519 AAAA\n')

        def task(argv):
            request = json.loads((self.data / "update" / "request.json").read_text())
            (self.data / "update" / "result.json").write_text(json.dumps(
                {"id": request["id"], "ok": False, "error": "signature check failed for ops-20261018-1"}))
            return 0, ""
        self.outputs["schtasks.exe /Run"] = task
        answer = self.ex.handle({"action": "self-update", "args": {"ref": "ops-20261018-1"}})
        self.assertEqual((answer["ok"], answer["status"]), (False, 409))
        self.assertIn("signature", answer["error"])
        self.assertNotIn(("stop", "LocalNotify"), self.scm.calls)


class RelayOpsTests(unittest.TestCase):
    """MSI (viewer) asks DESKTOP (host) through the fake planner relay; the host's helper pipe is mocked."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"AGENT_CONSOLE_KEY": bus_crypto.encode_key(os.urandom(32))})
        self.env.start()
        self.data = RelayData()
        self.fake = FakeRelay(self.data)
        self.host = Node(self.root / "host", self.fake.url, self.data.mint("desktop-pc"), "desktop-pc")
        self.viewer = Node(self.root / "viewer", self.fake.url, self.data.mint("msi"), "msi")
        for here, there in ((self.host, "console-msi"), (self.viewer, "console-desktop-pc")):
            wait_for(lambda: any(r["id"] == there for r in here.bus.remote_rows()), message=f"{there} on {here.machine}")
        self.calls = []

        def helper_pipe(request, timeout=None):
            self.calls.append(request)
            if request["action"] == "status":
                return {"ok": True, "result": {"uptime_seconds": 60, "services": {"parsec": {"state": "running"}}}}
            return {"ok": True, "result": {"service": "Parsec", "running": True}}
        self.host.app.console.remote.ops_call = helper_pipe

    def tearDown(self):
        for node in (self.host, self.viewer):
            node.stop()
        self.fake.close()
        for node in (self.host, self.viewer):
            node.close()
        self.env.stop()
        self.temp.cleanup()

    def bus_lines(self, node):
        return [m["body"] for m in node.bus.history(200)["messages"] if (m.get("meta") or {}).get("remote_ops")]

    def test_round_trip_logged_on_both_pcs_and_opaque_to_the_relay(self):
        status, answer = http_json(self.viewer.port, "/remote/ops", {"machine": "DESKTOP-PC", "action": "status"})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["machine"], "desktop-pc")
        self.assertEqual(answer["result"]["services"]["parsec"]["state"], "running")
        self.assertEqual(answer["result"]["bus"]["machine"], "desktop-pc")
        status, answer = http_json(self.viewer.port, "/remote/ops", {"machine": "desktop-pc", "action": "service-restart",
                                                                    "args": {"name": "parsec"}})
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.calls[-1], {"action": "service-restart", "args": {"name": "parsec"}, "origin": "msi"})
        # Logged on both PCs (file + a bus line that is not injected into agents).
        for node in (self.host, self.viewer):
            log = (self.root / ("host" if node is self.host else "viewer") / "remote-ops.log").read_text(encoding="utf-8")
            self.assertIn("msi -> desktop-pc service-restart parsec: ok", log)
        wait_for(lambda: any("service-restart parsec: ok" in b for b in self.bus_lines(self.host)), timeout=5,
                 message="host bus line")
        self.assertTrue(any("service-restart parsec: ok" in b for b in self.bus_lines(self.viewer)))
        stored = json.dumps(self.data.envelopes, default=str)
        for secret in ("service-restart", "parsec", "uptime"):
            self.assertNotIn(secret, stored)

    def test_refusals_come_back_with_their_status(self):
        status, answer = http_json(self.viewer.port, "/remote/ops", {"machine": "desktop-pc", "action": "reboot"})
        self.assertEqual(status, 400, answer)  # no confirm: refused before anything is sent
        status, answer = http_json(self.viewer.port, "/remote/ops", {"machine": "desktop-pc", "action": "exec",
                                                                    "args": {"cmd": "calc"}})
        self.assertEqual(status, 400, answer)
        status, answer = http_json(self.viewer.port, "/remote/ops", {"machine": "nowhere", "action": "status"})
        self.assertEqual(status, 409, answer)
        self.assertEqual(self.calls, [])

    def test_helper_not_installed(self):
        def absent(request, timeout=None):
            raise remote_ops.OpsError(503, "not-installed")
        self.host.app.console.remote.ops_call = absent
        status, answer = http_json(self.viewer.port, "/remote/ops", {"machine": "desktop-pc", "action": "status"})
        self.assertEqual(status, 503, answer)
        self.assertIn("remote ops not installed on desktop-pc", answer["error"])

    def test_wrong_key_is_dropped_and_never_reaches_the_helper(self):
        remote = self.viewer.app.console.remote
        remote.key = os.urandom(32)
        answer = remote.request("desktop-pc", {"t": "ops", "action": "status", "args": {}, "nonce": uuid.uuid4().hex,
                                               "ts": time.time()}, timeout=3)
        self.assertIsNone(answer)
        wait_for(lambda: self.host.app.console.remote.dropped >= 1, timeout=5, message="frame dropped")
        self.assertEqual(self.calls, [])

    def test_replayed_and_stale_requests_are_refused_even_after_a_restart(self):
        remote = self.host.app.console.remote
        p = {"t": "ops", "action": "status", "args": {}, "nonce": uuid.uuid4().hex, "ts": time.time()}
        self.assertIn("bus", remote.serve_ops("msi", dict(p)))
        with self.assertRaises(BusError) as replay:
            remote.serve_ops("msi", dict(p))
        self.assertEqual(replay.exception.status, 409)
        remote.ops_nonces = bus_remote.OpsNonces(remote.ops_nonces.path)  # a service restart: the file remembers
        with self.assertRaises(BusError):
            remote.serve_ops("msi", dict(p))
        with self.assertRaises(BusError) as stale:
            remote.serve_ops("msi", dict(p, nonce=uuid.uuid4().hex, ts=time.time() - bus_remote.OPS_FRESH - 5))
        self.assertEqual(stale.exception.status, 400)
        with self.assertRaises(BusError):
            remote.serve_ops("msi", dict(p, nonce="not-hex"))
        self.assertEqual(len(self.calls), 1)


def current_user_sid():
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    return out.strip().split(",")[-1].strip('"')


@unittest.skipUnless(os.name == "nt", "named pipes are Windows-only")
class PipeTests(unittest.TestCase):
    def serve(self, sddl, handler):
        name = r"\\.\pipe\agent-bus-remote-ops-test-" + uuid.uuid4().hex[:8]
        server = helper.PipeServer(handler, name=name, sddl=sddl)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(5)))
        return name

    def test_round_trip_for_an_allowed_account(self):
        seen = []
        name = self.serve(f"D:P(A;;GA;;;SY)(A;;GRGW;;;{current_user_sid()})",
                          lambda req: seen.append(req) or {"ok": True, "result": {"echo": req.get("action")}})
        for _ in range(2):  # the same pipe instance serves one connection after another
            self.assertEqual(remote_ops.call({"action": "status", "args": {}}, timeout=10, pipe=name),
                             {"ok": True, "result": {"echo": "status"}})
        self.assertEqual(len(seen), 2)

    def test_the_acl_refuses_everyone_but_localnotify(self):
        name = self.serve(helper.pipe_sddl(), lambda req: {"ok": True})
        with self.assertRaises(remote_ops.OpsError) as refused:
            remote_ops.call({"action": "status", "args": {}}, timeout=10, pipe=name)
        self.assertEqual(refused.exception.status, 403)

    def test_absent_pipe(self):
        with self.assertRaises(remote_ops.OpsError) as absent:
            remote_ops.call({"action": "status"}, timeout=5, pipe=r"\\.\pipe\agent-bus-remote-ops-none-" + uuid.uuid4().hex)
        self.assertEqual(absent.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
