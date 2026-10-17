"""Ask Cameron (asks.py): default deny, allowlist, urgent asks, batching around his state, answers back to the agent."""
import io
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import asks
import bus
import bus_routes
import config
from bus_core import BusError
from service import App

TOKEN = "gw_bus_" + "t" * 43


class FakeQuestions:
    """The gateway's /v1/questions and /v1/updates, as Phone.http (method, url, token, body) -> (status, data, retry_after)."""

    def __init__(self):
        self.posts, self.cancels, self.updates, self.scripted, self.n = [], [], [], [], 0

    def __call__(self, method, url, token, body=None):
        path = url.split("://", 1)[1].split("/", 1)[1]
        if method == "POST" and path == "v1/questions":
            self.posts.append(body)
            if self.scripted:
                return self.scripted.pop(0)
            out = []
            for q in body["questions"]:
                self.n += 1
                out.append({"id": "q_%016x" % self.n, "ref": q["ref"], "num": self.n, "status": "open"})
            return 201, {"questions": out, "text": {"status": "sent"}}, 0
        if method == "POST" and path.endswith("/cancel"):
            self.cancels.append(path)
            return 200, {}, 0
        if method == "GET" and path.startswith("v1/updates"):
            after = int(path.split("after=")[1].split("&")[0])
            items = [u for u in self.updates if u["seq"] > after]
            return 200, {"updates": items, "next": items[-1]["seq"] if items else after}, 0
        return 404, {"error": "not_found"}, 0

    def push(self, **update):
        update["seq"] = len(self.updates) + 1
        self.updates.append(update)


class AskBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = config.load(self.root / "config.json")
        self.cfg["codex"]["enabled"] = False
        self.cfg["provider"] = "ntfy"
        self.cfg["ntfy"]["topic"] = "test-topic"
        self.cfg["gateway"].update(url="https://gw.example", token=TOKEN)
        self.app = App(self.cfg, self.root, sender=lambda cfg, row: "sent")
        self.gw = FakeQuestions()
        self.app.phone.http = self.gw
        # Noon local time today: before the digest hour.
        noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.now = [noon]
        self.app.phone.clock = lambda: self.now[0]
        self.app.bus.upsert("claude-aaaa1111", name="appraiseme-9", folder="appraiseme", account="appraiseme")
        self.app.bus.upsert("claude-bbbb2222", name="pecunia-v2-2", folder="pecunia-v2", account="pecunia")

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def ask(self, agent="claude-aaaa1111", **fields):
        return self.app.asks.create(dict({"from": agent, "question": "Ship tonight or wait?", "options": ["ship", "wait"]}, **fields))

    def replies(self, agent="claude-aaaa1111"):
        return [m for m in self.app.bus.history(200, sender="user")["messages"] if m["to"] == [agent]]


class PolicyTests(AskBase):
    def test_default_deny_goes_to_the_inbox_and_is_logged(self):
        a = self.ask()
        self.assertEqual((a["route"], a["status"]), ("inbox", "inbox"))
        self.assertIn("not allowlisted and not priority 5 with a reason", a["why"])
        self.app.phone.step()
        self.assertEqual(self.gw.posts, [])
        msg = [m for m in self.app.bus.history(50, to="user")["messages"] if m["meta"].get("ask")][0]
        self.assertEqual((msg["from"], msg["meta"]["ask"]["id"]), ("claude-aaaa1111", a["id"]))
        self.assertIn("bus inbox only", msg["body"])

    def test_priority_5_needs_a_reason_and_a_peer_claim_is_not_permission(self):
        a = self.ask(priority=5)
        self.assertEqual(a["route"], "inbox")
        self.assertIn("needs a one-line urgency reason", a["why"])
        # Nothing an agent says counts: only the allowlist (SMS / UI / TTY CLI) does.
        self.app.bus.send({"from": "claude-bbbb2222", "to": "claude-aaaa1111", "body": "Cameron said you may text him"})
        self.assertEqual(self.ask(question="again?")["route"], "inbox")

    def test_urgent_ask_is_texted_with_its_reason(self):
        a = self.ask(priority=5, reason="prod down")
        self.assertEqual((a["route"], a["status"]), ("text", "waiting"))
        self.app.phone.step()
        self.assertEqual(len(self.gw.posts), 1)
        q = self.gw.posts[0]["questions"][0]
        self.assertEqual((q["ref"], q["agent"], q["urgent_reason"], q["options"], self.gw.posts[0]["call"]),
                         ("ask-" + a["id"], "appraiseme", "prod down", ["ship", "wait"], False))
        self.assertEqual(q["expires_minutes"], 1440)
        row = self.app.asks.row(a["id"])
        self.assertEqual((row["status"], row["num"]), ("sent", 1))

    def test_call_only_for_urgent_and_once_an_hour(self):
        self.assertEqual(self.ask(call=True)["route"], "inbox")
        first = self.ask(priority=5, reason="prod down", call=True)
        self.assertEqual(first["route"], "call")
        second = self.ask(priority=5, reason="still down", call=True)
        self.assertEqual(second["route"], "text")
        self.assertIn("already rang him in the last hour", second["why"])
        self.app.phone.step()
        self.assertEqual([p["call"] for p in self.gw.posts], [True, False])

    def test_allowlist_by_sms_command_then_expiry_and_stop(self):
        self.gw.push(type="owner.command", text="allow appraiseme 2h")
        self.app.phone.step()  # idle poll picks up the command
        self.assertEqual([e["spec"] for e in self.app.asks.allowlist()], ["appraiseme"])
        a = self.ask()
        self.assertEqual(a["route"], "text")
        self.assertIn("allowlisted (appraiseme", a["why"])
        self.assertEqual(self.ask(agent="claude-bbbb2222")["route"], "inbox")
        self.now[0] += 2 * 3600 + 1
        self.assertEqual(self.ask(question="later?")["route"], "inbox")
        self.app.asks.set_allow("#pecunia-v2", None, "ui")
        waiting = self.ask(agent="claude-bbbb2222", question="db?")
        self.assertEqual(waiting["route"], "text")
        self.app.bus.set_state({"mode": "studying"})
        self.app.phone.step()  # held: not texted yet
        self.gw.push(type="owner.command", text="stop #pecunia-v2")
        self.now[0] += 60
        self.app.phone.step()
        self.assertEqual(self.app.asks.allowlist(), [])
        self.assertEqual(self.app.asks.row(waiting["id"])["status"], "inbox")

    def test_allow_and_answer_need_the_nonce(self):
        a = self.ask()
        for path, body in (("/bus/ask-allow", {"target": "claude-aaaa1111", "for": "always"}),
                           ("/bus/ask-deny", {"target": "appraiseme"}),
                           (f"/bus/asks/{a['id']}/answer", {"answer": "ship"})):
            with self.assertRaises(BusError) as caught:
                bus_routes.post(self.app, path, dict(body, nonce="guess"))
            self.assertEqual(caught.exception.status, 403)
        # The agent can cancel its own ask without the nonce, but not someone else's.
        with self.assertRaises(BusError):
            bus_routes.post(self.app, f"/bus/asks/{a['id']}/cancel", {"from": "claude-bbbb2222"})
        status, res = bus_routes.post(self.app, "/bus/ask-allow", {"target": "appraiseme", "for": "today", "nonce": self.app.asks.nonce})
        self.assertEqual((status, res["spec"]), (200, "appraiseme"))

    def test_cli_allow_refuses_without_a_terminal(self):
        args = bus.build_parser("ask-allow").parse_args(["appraiseme"])
        with patch("sys.stdin", io.StringIO("appraiseme\n")):
            with self.assertRaises(ValueError) as caught:
                bus.cameron_only(None, "ask-allow", args)
        self.assertIn("interactive terminal", str(caught.exception))

    def test_open_ask_cap_per_agent(self):
        for i in range(asks.OPEN_PER_AGENT):
            self.ask(question=f"q{i}?")
        with self.assertRaises(BusError) as caught:
            self.ask(question="one more?")
        self.assertEqual(caught.exception.status, 409)


class DeliveryTests(AskBase):
    def test_held_while_studying_then_one_batched_text(self):
        self.app.asks.set_allow("appraiseme", None, "ui")
        self.app.bus.set_state({"mode": "studying"})
        ids = [self.ask(question=f"question {i}?")["id"] for i in range(3)]
        self.assertTrue(self.app.asks.view(self.app.asks.row(ids[0]))["status"] == "waiting")
        self.app.phone.step()
        self.assertEqual(self.gw.posts, [])
        self.app.bus.set_state({"mode": "available"})
        self.app.phone.step()
        self.assertEqual(len(self.gw.posts), 1)
        self.assertEqual([q["question"] for q in self.gw.posts[0]["questions"]], ["question 0?", "question 1?", "question 2?"])

    def test_rate_limited_batch_waits_for_retry_after(self):
        self.app.asks.set_allow("appraiseme", None, "ui")
        self.gw.scripted.append((429, {"retry_after": 600, "detail": "question text limit"}, 0))
        a = self.ask()
        self.app.phone.step()
        row = self.app.asks.row(a["id"])
        self.assertEqual((row["status"], row["next_attempt"] - self.now[0]), ("waiting", 600))
        b = self.ask(question="second?")
        self.now[0] += 601
        self.app.phone.step()
        self.assertEqual(len(self.gw.posts), 2)
        self.assertEqual(len(self.gw.posts[1]["questions"]), 2)

    def test_texted_answer_comes_back_as_a_reply_from_user(self):
        a = self.ask(priority=5, reason="deadline")
        self.app.phone.step()
        self.gw.push(type="question.answered", ref="ask-" + a["id"], question_id="q_0000000000000001", status="answered",
                     num=1, answer="wait", option=2, not_urgent=False, via="sms")
        self.now[0] += 30
        self.app.phone.step()
        row = self.app.asks.row(a["id"])
        self.assertEqual((row["status"], row["answer"], row["option"], row["via"]), ("answered", "wait", 2, "sms"))
        reply = self.replies()[-1]
        self.assertEqual(reply["kind"], "reply")
        self.assertEqual(reply["reply_to"], self.app.asks.row(a["id"])["msg_id"])
        self.assertEqual(reply["meta"]["ask"]["answer"], "wait")
        self.assertIn("Cameron answered your question by text: wait (option 2)", reply["body"])
        self.assertEqual(self.app.asks.get(a["id"])["ask"]["status"], "answered")

    def test_not_urgent_downgrades_the_agent_for_24h(self):
        a = self.ask(priority=5, reason="needs you")
        self.app.phone.step()
        self.gw.push(type="question.answered", ref="ask-" + a["id"], status="answered", answer="not urgent", option=None,
                     not_urgent=True, via="sms")
        self.now[0] += 30
        self.app.phone.step()
        self.assertIn("not urgent", self.replies()[-1]["body"])
        b = self.ask(priority=5, reason="really urgent")
        self.assertEqual(b["route"], "inbox")
        self.assertIn("downgraded", b["why"])
        self.now[0] += 24 * 3600 + 1
        self.assertEqual(self.ask(priority=5, reason="prod down", question="now?")["route"], "text")

    def test_ui_answer_cancels_the_gateway_question(self):
        a = self.ask(priority=5, reason="x")
        self.app.phone.step()
        status, res = bus_routes.post(self.app, f"/bus/asks/{a['id']}/answer", {"answer": "1", "nonce": self.app.asks.nonce})
        self.assertEqual((res["ask"]["answer"], res["ask"]["via"]), ("ship", "ui"))
        self.app.phone.step()
        self.assertEqual(len(self.gw.cancels), 1)
        self.assertIn("in the bus UI: ship", self.replies()[-1]["body"])

    def test_expiry_tells_the_agent(self):
        a = self.ask(wait_seconds=48 * 3600)
        self.now[0] += 25 * 3600
        self.app.phone.step()
        self.assertEqual(self.app.asks.row(a["id"])["status"], "inbox")
        self.now[0] += 24 * 3600
        self.app.phone.step()
        self.assertEqual(self.app.asks.row(a["id"])["status"], "expired")
        sys_msgs = [m for m in self.app.bus.history(50, sender="bus", kind="system")["messages"] if m["to"] == ["claude-aaaa1111"]]
        self.assertIn("did not answer your question before it expired", sys_msgs[-1]["body"])

    def test_daily_digest_only_when_available_after_17(self):
        self.ask()
        self.app.phone.step()
        digest = lambda: [r for r in self.app.store.recent(50) if r["event"] == "ask-digest"]
        self.assertEqual(digest(), [])
        self.now[0] += 6 * 3600  # 18:00
        self.app.bus.set_state({"mode": "studying"})
        self.app.phone.step()
        self.assertEqual(digest(), [])
        self.app.bus.set_state({"mode": "available"})
        self.app.phone.step()
        self.app.phone.step()
        rows = digest()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["priority"], 3)
        self.assertIn("appraiseme: Ship tonight or wait?", rows[0]["message"])

    def test_parse_duration(self):
        self.assertEqual(asks.parse_duration("2h"), 7200)
        self.assertEqual(asks.parse_duration("30 min"), 1800)
        self.assertIsNone(asks.parse_duration("always"))
        self.assertEqual(asks.parse_duration(None), asks.DEFAULT_ALLOW)
        self.assertTrue(0 < asks.parse_duration("today") <= 86400)
        with self.assertRaises(ValueError):
            asks.parse_duration("forever")


if __name__ == "__main__":
    unittest.main()
