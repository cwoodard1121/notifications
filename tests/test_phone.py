import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import bus
import config
import notify
import phone
import phone_setup
import providers
from service import App, Server, normalize
from store import Store

TOKEN = "gw_bus_" + "t" * 43
AGENT = "claude-1234abcd"


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        url, gw = urlsplit(self.path), self.server
        gw.requests.append({"method": method, "path": url.path, "query": parse_qs(url.query),
                            "auth": self.headers.get("Authorization"), "body": body})
        if method == "POST" and url.path == "/v1/alerts":
            if gw.scripted:
                return self.reply(*gw.scripted.pop(0))
            alert_id = f"al_{len(gw.alerts) + 1}"
            if body["channels"] == ["text"]:
                alert = {"id": alert_id, "client": "bus", "ref": body["ref"], "status": "sent", "call": None,
                         "text": {"status": "sent", "message_id": "SM" + "0" * 32}}
            else:
                alert = {"id": alert_id, "client": "bus", "ref": body["ref"], "status": "calling",
                         "call": {"attempts": 1, "digit": None, "via": None, "next_call_at": None}}
            gw.alerts[alert["id"]] = alert
            return self.reply(201, {"alert": alert})
        if method == "GET" and url.path == "/v1/updates":
            after = int(parse_qs(url.query).get("after", ["0"])[0])
            items = [u for u in gw.updates if u["seq"] > after]
            return self.reply(200, {"updates": items, "next": items[-1]["seq"] if items else after})
        match = re.fullmatch(r"/v1/alerts/([^/]+)(/ack)?", url.path)
        if match and match[1] in gw.alerts:
            if match[2]:
                gw.alerts[match[1]]["status"] = "acked"
                gw.alerts[match[1]]["call"]["via"] = "client"
            return self.reply(200, {"alert": gw.alerts[match[1]]})
        self.reply(404, {"error": "not_found"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


class FakeGateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), GatewayHandler)
        self.requests, self.scripted, self.updates, self.alerts = [], [], [], {}
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server_port}"

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join()

    def posted(self, path="/v1/alerts"):
        return [r for r in self.requests if r["method"] == "POST" and r["path"] == path]

    def polls(self):
        return [r for r in self.requests if r["path"] == "/v1/updates"]


class PhoneBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = config.load(self.root / "config.json")
        self.cfg["codex"]["enabled"] = False
        self.cfg["provider"] = "ntfy"
        self.cfg["ntfy"]["topic"] = "test-topic"
        self.gateway = FakeGateway()
        self.cfg["gateway"].update(url=self.gateway.url, token=TOKEN)
        self.app = App(self.cfg, self.root, sender=lambda cfg, row: "sent")
        self.now = [time.time()]
        self.app.phone.clock = lambda: self.now[0]

    def tearDown(self):
        self.app.close()
        self.gateway.close()
        self.temp.cleanup()

    def texts(self):
        return [r for r in self.app.store.recent(200) if r["event"] == "escalation"]

    def dms(self, to=AGENT):
        return [m for m in self.app.bus.history(200, sender="bus", kind="system")["messages"] if m["to"] == [to]]

    def only_call(self):
        calls = self.app.store.calls(10)
        self.assertEqual(len(calls), 1)
        return calls[0]


class EscalationTests(PhoneBase):
    def test_normalize_call_flag(self):
        self.assertIsNone(normalize({"message": "x"})["call"])
        self.assertEqual((normalize({"message": "x", "call": True, "priority": 2})["priority"]), 5)
        self.assertEqual(normalize({"message": "x", "call": False, "priority": 5})["priority"], 5)
        self.assertIsNone(normalize({"message": "x", "call": None})["call"])
        for bad in ("yes", "true", 1, 0, [], {}):
            with self.assertRaises(ValueError):
                normalize({"message": "x", "call": bad})

    def test_priority_ladder_with_sms(self):
        self.assertEqual(self.cfg["phone"]["text_channel"], "sms")  # the default
        self.assertNotIn("escalation", self.app.notify({"message": "p3", "priority": 3}))
        self.assertEqual(self.app.notify({"message": "p4", "priority": 4})["escalation"], {"text": "queued"})
        # A call carries the text itself: no separate SMS row.
        self.assertEqual(self.app.notify({"message": "p5", "priority": 5})["escalation"],
                         {"text": "sent with the call", "call": "queued"})
        self.assertEqual(self.app.notify({"message": "forced", "priority": 3, "call": True})["escalation"],
                         {"text": "sent with the call", "call": "queued"})
        self.assertEqual(self.app.notify({"message": "quiet", "priority": 5, "call": False})["escalation"], {"text": "queued"})
        rows = {r["message"]: r for r in self.app.store.recent(50) if r["event"] != "escalation"}
        self.assertEqual(rows["forced"]["priority"], 5)
        texts = self.texts()
        self.assertEqual(sorted(t["message"] for t in texts), ["p4", "quiet"])
        self.assertTrue(all(t["provider"] == "sms" and t["status"] == "queued" for t in texts))
        calls = self.app.store.calls(10)
        self.assertEqual(sorted(c["message"] for c in calls), ["forced", "p5"])
        self.assertTrue(all(c["with_text"] == 1 for c in calls))

    def test_p5_submits_one_call_alert_with_text_and_call(self):
        self.app.notify({"message": "Health check failing", "title": "Pecunia prod is down", "priority": 5})
        call = self.only_call()
        self.app.phone.step()
        sent = self.gateway.posted()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["body"], {"ref": "bus-" + call["id"], "title": "Pecunia prod is down",
                                           "message": "Health check failing", "channels": ["text", "call"],
                                           "retry": True, "fallback_text": False})
        self.assertEqual(self.texts(), [])

    def test_text_copy_keeps_fields_and_dedupe_suffix(self):
        self.app.notify({"message": "Deploy needs approval", "title": "Pecunia", "source": AGENT, "priority": 4, "dedupe_key": "k1"})
        text = self.texts()[0]
        self.assertEqual((text["title"], text["source"], text["priority"], text["dedupe_key"], text["provider"]),
                         ("Pecunia", AGENT, 4, "k1#text", "sms"))
        self.app.notify({"message": "No key", "priority": 4})
        self.assertIsNone([t for t in self.texts() if t["message"] == "No key"][0]["dedupe_key"])

    def test_held_and_duplicate_notifications_never_escalate(self):
        self.app.bus.set_state({"mode": "sleeping"})
        held = self.app.notify({"message": "p4 at night", "priority": 4})
        self.assertEqual(held["status"], "held")
        self.assertNotIn("escalation", held)
        urgent = self.app.notify({"message": "prod down", "call": True})
        self.assertNotEqual(urgent["status"], "held")  # call:true passes every state, including sleeping
        self.assertEqual(urgent["escalation"], {"text": "sent with the call", "call": "queued"})
        self.app.bus.set_state({"mode": "studying"})
        self.assertNotIn("escalation", self.app.notify({"message": "p3 studying", "priority": 3}))
        self.assertEqual(self.app.notify({"message": "act", "priority": 4, "dedupe_key": "d"})["escalation"], {"text": "queued"})
        again = self.app.notify({"message": "act", "priority": 4, "dedupe_key": "d"})
        self.assertTrue(again["duplicate"])
        self.assertNotIn("escalation", again)
        self.assertEqual(len(self.texts()), 1)
        self.assertEqual(len(self.app.store.calls(10)), 1)

    def test_skips_and_dry_run(self):
        self.cfg["gateway"]["url"] = ""
        self.assertEqual(self.app.notify({"message": "a", "priority": 4})["escalation"],
                         {"text": "skipped: phone gateway not set up (python notify.py setup phone)"})
        result = self.app.notify({"message": "d", "call": True})
        self.assertEqual(result["escalation"], {"text": "skipped: phone gateway not set up (python notify.py setup phone)",
                                                "call": "dry-run: gateway not configured (python notify.py setup gateway)"})
        self.assertEqual(self.only_call()["status"], "dry-run")
        self.assertEqual(self.texts(), [])
        self.cfg["gateway"].update(url=self.gateway.url, token="")
        self.assertEqual(self.app.notify({"message": "e", "priority": 4})["escalation"]["text"],
                         "skipped: phone gateway not set up (python notify.py setup phone)")
        self.assertEqual(self.app.phone.step(), 30)
        self.assertEqual(self.gateway.requests, [])
        self.cfg["gateway"]["token"] = TOKEN
        self.cfg["phone"]["text_channel"] = "off"
        self.assertEqual(self.app.notify({"message": "c", "priority": 4})["escalation"], {"text": "skipped: phone.text_channel is off"})
        self.assertEqual(self.app.notify({"message": "f", "call": True})["escalation"],
                         {"text": "skipped: phone.text_channel is off", "call": "queued"})
        self.app.phone.step()
        self.assertEqual(self.gateway.posted()[-1]["body"]["channels"], ["call"])
        self.assertEqual(self.texts(), [])

    def test_legacy_telegram_channel_still_works(self):
        self.cfg["phone"]["text_channel"] = "telegram"
        self.assertEqual(self.app.notify({"message": "a", "priority": 4})["escalation"],
                         {"text": "skipped: telegram not configured (python notify.py setup telegram)"})
        self.cfg["telegram"].update(bot_token="123:abc", chat_id="42")
        self.assertEqual(self.app.notify({"message": "b", "priority": 5})["escalation"], {"text": "queued", "call": "queued"})
        self.assertEqual([(t["message"], t["provider"]) for t in self.texts()], [("b", "telegram")])
        self.app.phone.step()
        self.assertEqual(self.gateway.posted()[-1]["body"]["channels"], ["call"])
        self.assertEqual(self.only_call()["with_text"], 0)
        self.cfg["provider"] = "telegram"
        self.assertEqual(self.app.notify({"message": "c", "priority": 4})["escalation"],
                         {"text": "skipped: primary provider is already telegram"})
        self.assertIn("telegram_configured", self.app.phone.health())

    def test_call_caps_count_real_calls_only_and_fall_back_to_sms(self):
        self.cfg["phone"].update(max_calls_per_hour=2, max_calls_per_day=3)
        notify_call = lambda: self.app.notify({"message": "urgent", "call": True})["escalation"]
        self.assertEqual([notify_call()["call"], notify_call()["call"]], ["queued", "queued"])
        self.assertEqual(notify_call(), {"text": "queued", "call": "skipped: call limit reached (2 per hour)"})
        self.assertEqual([t["provider"] for t in self.texts()], ["sms"], "the capped call still texts")
        self.now[0] += 3601
        self.assertEqual([notify_call()["call"], notify_call()["call"]], ["queued", "skipped: call limit reached (3 per day)"])
        self.cfg["gateway"]["url"] = ""
        self.assertTrue(notify_call()["call"].startswith("dry-run"))
        self.cfg["gateway"]["url"] = self.gateway.url
        self.assertEqual(self.app.store.call_count(self.now[0] - 86400), 3)
        self.cfg["phone"]["max_calls_per_hour"] = 0
        self.assertEqual(notify_call()["call"], "skipped: call limit reached (0 per hour)")
        self.assertEqual(len(self.texts()), 3)

    def test_recap_excludes_escalation_copies(self):
        self.app.notify({"message": "needs you", "priority": 4})
        self.assertEqual(len(self.texts()), 1)
        recap = self.app.recap(24)
        self.assertEqual(recap["count"], 1)
        self.assertEqual(recap["message"].count("needs you"), 1)


class SmsProviderTests(PhoneBase):
    def setUp(self):
        super().setUp()
        self.cfg["provider"] = "local"  # main rows stay local: only SMS rows are queued, and nothing reaches ntfy.sh

    def row(self, **over):
        self.app.notify(dict({"message": "Deploy needs approval", "title": "Pecunia", "priority": 4}, **over))
        return [r for r in self.app.store.recent(50) if r["provider"] == "sms"][0]  # the newest

    def deliver(self, row, **kwargs):
        return providers.send(self.cfg, row, **kwargs)

    def test_sent_returns_the_alert_id(self):
        row = self.row()
        self.assertEqual(row["provider"], "sms")
        self.assertEqual(self.deliver(row), "al_1")
        [request] = self.gateway.posted()
        self.assertEqual(request["auth"], "Bearer " + TOKEN)
        self.assertEqual(request["body"], {"ref": "bus-sms-" + row["id"], "title": "Pecunia",
                                           "message": "Deploy needs approval", "channels": ["text"]})
        # A retry after a lost response gets the same alert back (200, duplicate).
        self.gateway.scripted.append((200, {"alert": {"id": "al_1", "status": "sent"}, "duplicate": True}))
        self.assertEqual(self.deliver(row), "al_1")
        long = self.row(message="m" * 3000, title="t" * 150)
        self.deliver(long)
        self.assertEqual(len(self.gateway.posted()[-1]["body"]["message"]), 1500)

    def test_failures_map_to_retry_or_permanent(self):
        row = self.row()
        twilio_detail = "Twilio 400: Attempt to send to unsubscribed recipient [redacted] (code 21610)"
        cases = [
            ((201, {"alert": {"id": "al_9", "status": "text_failed",
                              "text": {"status": "failed", "detail": twilio_detail}}}), False, 0, "SMS not sent: " + twilio_detail),
            ((429, {"error": "rate_limited", "retry_after": 120}, {"Retry-After": "120"}), True, 120, "gateway text limit"),
            ((503, {"error": "not_configured", "detail": "twilio"}), False, 0, "gateway: twilio not configured"),
            ((401, {"error": "unauthorized"}), False, 0, "refused the bus token"),
            ((400, {"error": "invalid", "detail": f"title too long {TOKEN}"}), False, 0, "gateway rejected the text: title too long ***"),
            ((500, {"error": "internal"}), True, 0, "gateway returned HTTP 500"),
            ((404, {"error": "not_found"}), False, 0, "check gateway.url"),
        ]
        for answer, retry, delay, message in cases:
            with self.subTest(status=answer[0]):
                self.gateway.scripted.append(answer)
                with self.assertRaises(providers.DeliveryError) as caught:
                    self.deliver(row)
                self.assertEqual((caught.exception.retry, caught.exception.delay), (retry, delay))
                self.assertIn(message, str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))

        def down(*args, **kwargs):
            raise providers.GatewayDown("gateway connection failed or timed out")
        with self.assertRaises(providers.DeliveryError) as caught:
            self.deliver(row, gateway=down)
        self.assertTrue(caught.exception.retry)
        self.assertNotIn(self.gateway.url, str(caught.exception))
        self.cfg["gateway"]["token"] = ""
        with self.assertRaises(providers.DeliveryError) as caught:
            self.deliver(row)
        self.assertFalse(caught.exception.retry)
        self.assertIn("phone gateway not set up", str(caught.exception))

    def test_queue_worker_delivers_sms_rows(self):
        self.app.sender = providers.send
        self.row()
        self.app.deliver_one()
        self.assertIsNone(self.app.store.pending())
        sms = [r for r in self.app.store.recent(10) if r["provider"] == "sms"][0]
        self.assertEqual((sms["status"], sms["provider_id"]), ("accepted", "al_1"))
        self.gateway.scripted.append((201, {"alert": {"id": "al_2", "status": "text_failed",
                                                      "text": {"detail": "Twilio 400: Permission to send an SMS has not been enabled (code 21408)"}}}))
        self.row(message="second")
        while self.app.store.pending():
            self.app.deliver_one()
        failed = [r for r in self.app.store.recent(10) if r["provider"] == "sms" and r["message"] == "second"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("code 21408", failed["error"])


class GatewayTests(PhoneBase):
    def call(self, source=AGENT):
        self.app.notify({"message": "Health check failing", "title": "Pecunia prod is down", "source": source, "call": True})
        return self.only_call()

    def update(self, seq, call, kind, status, **extra):
        self.gateway.updates.append(dict({"seq": seq, "alert_id": call["gateway_id"], "ref": "bus-" + call["id"], "type": kind,
                                          "status": status, "attempt": 1, "at": "2026-10-01T18:04:05Z"}, **extra))

    def test_submission_updates_cursor_and_one_dm_per_outcome(self):
        self.app.bus.upsert(AGENT, folder="pecunia")
        call = self.call()
        self.app.phone.step()
        sent = self.gateway.posted()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["auth"], "Bearer " + TOKEN)
        self.assertEqual(sent[0]["body"], {"ref": "bus-" + call["id"], "title": "Pecunia prod is down", "message": "Health check failing",
                                           "channels": ["text", "call"], "retry": True, "fallback_text": False})
        call = self.app.store.call(call["id"])
        self.assertEqual((call["status"], call["gateway_id"], call["submit_attempts"]), ("calling", "al_1", 1))
        self.update(41, call, "alert.snoozed", "snoozed", digit="2", via="call", next_call_at="2026-10-01T18:14:31Z")
        self.app.phone.step()
        self.assertEqual(self.app.store.call(call["id"])["status"], "snoozed")
        self.assertEqual(len(self.dms()), 1)
        self.assertIn("Cameron snoozed the call (pressed 2): Pecunia prod is down", self.dms()[0]["body"])
        self.assertIn(phone.hhmm("2026-10-01T18:14:31Z"), self.dms()[0]["body"])
        self.update(42, call, "call.dialing", "calling", attempt=2)
        self.update(43, call, "alert.acked", "acked", digit="1", via="call")
        self.app.phone.step()
        call = self.app.store.call(call["id"])
        self.assertEqual((call["status"], call["digit"], call["via"]), ("acked", "1", "call"))
        self.assertEqual(self.app.store.get_state("gateway_cursor"), 43)
        self.app.phone.apply(self.gateway.updates[-1])  # a repeated update changes nothing
        self.app.phone.report(call["id"])
        dms = self.dms()
        self.assertEqual(len(dms), 2)
        self.assertIn("Cameron acknowledged the call (pressed 1): Pecunia prod is down", dms[1]["body"])
        self.assertEqual(self.app.phone.step(), 30)  # nothing open: no more polling
        # A restarted service resumes the feed after the stored cursor.
        restarted = App(self.cfg, self.root)
        restarted.phone.poll()
        self.assertEqual(self.gateway.requests[-1]["query"]["after"], ["43"])
        restarted.close()

    def test_sms_snooze_and_pc_ack_wording(self):
        self.app.bus.upsert(AGENT)
        call = self.call()
        self.app.phone.step()
        call = self.app.store.call(call["id"])
        self.update(1, call, "alert.snoozed", "snoozed", via="sms", next_call_at="2026-10-01T18:14:31Z")
        self.app.phone.step()
        self.assertIn("Cameron snoozed the call (texted back): Pecunia prod is down", self.dms()[-1]["body"])
        result = self.app.phone.ack(call["id"])
        self.assertEqual((result["gateway"], result["call"]["status"], result["call"]["via"]), ("acked", "acked", "client"))
        self.assertIn("Cameron acknowledged the call on the PC: Pecunia prod is down", self.dms()[-1]["body"])
        self.assertEqual(len(self.dms()), 2)

    def test_late_sms_ack_after_unanswered_sends_one_more_dm(self):
        self.app.bus.upsert(AGENT)
        call = self.call()
        self.app.phone.step()
        call = self.app.store.call(call["id"])
        self.update(5, call, "alert.unanswered", "unanswered", attempt=2)
        self.app.phone.step()
        self.assertEqual(self.app.store.call(call["id"])["status"], "unanswered")
        self.assertEqual(len(self.dms()), 1)
        self.assertIn("did not pick up or acknowledge the call after 2 tries (he also got the text and ntfy)", self.dms()[0]["body"])
        self.assertIn("If he replies 1 to the text later, you will get another message.", self.dms()[0]["body"])
        # Still polling for a while after unanswered; trailing updates do not reopen it.
        self.now[0] += 1800
        self.update(6, call, "call.ended", "unanswered", attempt=2, call_status="no-answer")
        polls = len(self.gateway.polls())
        self.assertEqual(self.app.phone.step(), 5)
        self.assertEqual(len(self.gateway.polls()), polls + 1)
        self.assertEqual(self.app.store.call(call["id"])["status"], "unanswered")
        self.update(7, call, "alert.acked", "acked", attempt=2, via="sms")
        self.app.phone.step()
        row = self.app.store.call(call["id"])
        self.assertEqual((row["status"], row["via"]), ("acked", "sms"))
        dms = self.dms()
        self.assertEqual(len(dms), 2)
        self.assertEqual(dms[1]["subject"], "Phone call acked")
        self.assertIn("Cameron acknowledged the call (texted back): Pecunia prod is down", dms[1]["body"])
        self.app.phone.step()
        self.assertEqual(len(self.dms()), 2)
        # Nothing open and nothing recently unanswered: polling stops.
        polls = len(self.gateway.polls())
        self.assertEqual(self.app.phone.step(), 30)
        self.assertEqual(len(self.gateway.polls()), polls)

    def test_polling_for_late_acks_stops_after_two_hours(self):
        call = self.call(source="agent")
        self.app.phone.step()
        self.update(5, self.app.store.call(call["id"]), "alert.unanswered", "unanswered", attempt=2)
        self.app.phone.step()
        self.now[0] += 7199
        polls = len(self.gateway.polls())
        self.app.phone.step()
        self.assertEqual(len(self.gateway.polls()), polls + 1)
        self.now[0] += 2
        self.assertEqual(self.app.phone.step(), 30)
        self.assertEqual(len(self.gateway.polls()), polls + 1)

    def test_unknown_source_gets_no_dm_and_unanswered_text(self):
        call = self.call(source="agent")
        self.app.phone.step()
        self.update(5, self.app.store.call(call["id"]), "alert.unanswered", "unanswered", attempt=2)
        self.app.phone.step()
        self.assertEqual(self.app.store.call(call["id"])["status"], "unanswered")
        self.assertEqual(self.app.bus.history(200, sender="bus", kind="system")["messages"], [])
        self.app.bus.upsert(AGENT)
        text = self.app.phone.outcome(dict(self.app.store.call(call["id"]), source=AGENT))
        self.assertIn("did not pick up or acknowledge the call after 2 tries (he also got the text and ntfy)", text)

    def test_rate_limit_keeps_the_call_queued(self):
        call = self.call()
        self.gateway.scripted.append((429, {"error": "rate_limited", "retry_after": 120}, {"Retry-After": "120"}))
        self.app.phone.step()
        call = self.app.store.call(call["id"])
        self.assertEqual((call["status"], call["submit_attempts"], call["next_attempt"]), ("queued", 0, self.now[0] + 120))
        self.app.phone.step()
        self.assertEqual(len(self.gateway.posted()), 1)  # not due yet
        self.now[0] += 121
        self.app.phone.step()
        self.assertEqual(self.app.store.call(call["id"])["status"], "calling")

    def test_refused_submissions_fail_and_tell_the_agent(self):
        self.app.bus.upsert(AGENT)
        for status, body, expected in ((401, {"error": "unauthorized"}, "refused the bus token"),
                                       (503, {"error": "not_configured", "detail": "twilio"}, "gateway: twilio not configured"),
                                       (400, {"error": "invalid", "detail": "title too long"}, "title too long")):
            with self.subTest(status=status):
                self.gateway.scripted.append((status, body))
                self.app.notify({"message": f"m{status}", "source": AGENT, "call": True})
                self.app.phone.step()
                call = [c for c in self.app.store.calls(10) if c["message"] == f"m{status}"][0]
                self.assertEqual(call["status"], "failed")
                self.assertIn(expected, call["error"])
                self.assertNotIn(TOKEN, call["error"])
                self.assertIn("could not be placed", self.dms()[-1]["body"])
        self.assertEqual(len(self.dms()), 3)
        self.assertEqual(self.app.phone.health()["last_error"], "gateway rejected the call: title too long")

    def test_network_and_server_errors_back_off_then_fail(self):
        answers = [phone.GatewayDown("gateway connection failed or timed out"), (502, {}, 0), (500, {}, 0), (504, {}, 0),
                   phone.GatewayDown("gateway connection failed or timed out"), (500, {}, 0)]

        def http(method, url, token, body=None):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.app.phone.http = http
        call = self.call()
        for delay in (5, 10, 20, 40, 80):
            self.app.phone.step()
            row = self.app.store.call(call["id"])
            self.assertEqual((row["status"], row["next_attempt"] - self.now[0]), ("queued", delay))
            self.now[0] += delay
        self.app.phone.step()
        row = self.app.store.call(call["id"])
        self.assertEqual((row["status"], row["submit_attempts"]), ("failed", 6))
        self.assertEqual(answers, [])


class StoreMigrationTests(unittest.TestCase):
    def test_old_calls_table_gains_the_new_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "notifications.sqlite3"
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE calls (
                id TEXT PRIMARY KEY, event_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                title TEXT NOT NULL, message TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                submit_attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                gateway_id TEXT, digit TEXT, error TEXT, notified TEXT, next_call_at TEXT,
                call_attempts INTEGER NOT NULL DEFAULT 0)""")
            db.execute("INSERT INTO calls (id, created, updated, title, message, source, status) "
                       "VALUES ('abcd', 1, 1, 't', 'm', 's', 'acked')")
            db.commit()
            db.close()
            store = Store(path)
            old = store.call("abcd")
            self.assertEqual((old["with_text"], old["via"], old["status"]), (0, None, "acked"))
            Store(path)  # idempotent
            store.add_call({"id": "ef01", "created": 2, "updated": 2, "title": "t", "message": "m", "source": "s",
                            "status": "queued", "with_text": 1})
            self.assertEqual(store.call("ef01")["with_text"], 1)


class HTTPTests(PhoneBase):
    def setUp(self):
        super().setUp()
        self.server = Server(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_calls_list_ack_forwarding_and_health(self):
        status, result = self.request("/notify", {"message": "Prod down", "call": True})
        self.assertEqual((status, result["escalation"]), (202, {"text": "sent with the call", "call": "queued"}))
        self.app.phone.step()
        status, listing = self.request("/phone/calls?limit=5")
        call = listing["calls"][0]
        self.assertEqual((status, call["status"], call["ref"], call["with_text"]), (200, "calling", "bus-" + call["id"], 1))
        self.assertEqual(self.request("/phone/calls?limit=0")[0], 400)
        status, acked = self.request(f"/phone/calls/{call['id']}/ack", {})
        self.assertEqual((status, acked["call"]["status"], acked["call"]["via"], acked["gateway"]), (200, "acked", "client", "acked"))
        self.assertEqual(len(self.gateway.posted("/v1/alerts/al_1/ack")), 1)
        self.cfg["gateway"]["url"] = ""
        self.request("/notify", {"message": "dry", "call": True})
        dry = [c for c in self.app.store.calls(5) if c["status"] == "dry-run"][0]
        status, acked = self.request(f"/phone/calls/{dry['id'][:8]}/ack", {})
        self.assertEqual((status, acked["call"]["status"]), (200, "acked"))
        self.assertEqual(len(self.gateway.posted("/v1/alerts/al_1/ack")), 1)
        self.assertEqual(self.request("/phone/calls/ffffffff/ack", {})[0], 404)
        self.assertEqual(self.request("/phone/calls/nothex!/ack", {})[0], 404)
        status, health = self.request("/health")
        self.assertEqual(set(health["phone"]), {"text_channel", "gateway_configured", "open_calls", "last_poll", "last_error"})
        self.assertEqual((health["phone"]["text_channel"], health["phone"]["gateway_configured"]), ("sms", False))
        self.assertNotIn(TOKEN, json.dumps(health))
        self.assertIn("GET /phone/calls?limit=50", self.request("/")[1]["endpoints"])

    def test_ack_reports_gateway_trouble(self):
        self.request("/notify", {"message": "Prod down", "call": True})
        self.app.phone.step()
        call = self.only_call()
        self.app.phone.http = lambda *a, **k: (_ for _ in ()).throw(phone.GatewayDown("gateway connection failed or timed out"))
        status, result = self.request(f"/phone/calls/{call['id']}/ack", {})
        self.assertEqual(status, 502)
        self.assertIn("may still ring", result["error"])
        self.assertEqual(self.app.store.call(call["id"])["status"], "calling")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def live(self):
        # Shaped like the live config.json: ntfy primary, blank telegram, no phone or gateway keys.
        return {"port": 8765, "api_token": "", "provider": "ntfy",
                "ntfy": {"server": "https://ntfy.sh", "topic": "agents-0123456789abcdef0123456789abcdef", "token": ""},
                "telegram": {"bot_token": "", "chat_id": ""}, "codex": {"enabled": False, "auto_notify": False},
                "recap": {"daily_at": "", "hours": 24}, "bus": {"mailbox": {"enabled": False, "dirs": []}}}

    def test_live_shaped_config_still_loads(self):
        self.write(self.live())
        cfg = config.load(self.path)
        self.assertEqual(cfg["phone"]["text_channel"], "sms")
        self.assertEqual((cfg["gateway"]["url"], cfg["gateway"]["token"]), ("", ""))
        for url in ("https://agent-bus-phone.cameron.workers.dev", "http://127.0.0.1:8787", "http://localhost:8787/"):
            self.write(dict(self.live(), gateway={"url": url, "token": TOKEN}))
            self.assertEqual(config.load(self.path)["gateway"]["url"], url)
        for channel in ("sms", "telegram", "off"):
            self.write(dict(self.live(), phone={"text_channel": channel}))
            self.assertEqual(config.load(self.path)["phone"]["text_channel"], channel)

    def test_bad_phone_values_are_rejected(self):
        for section, values in (("phone", {"text_channel": "whatsapp"}), ("phone", {"text_channel": "SMS"}),
                                ("phone", {"text_min_priority": 0}),
                                ("phone", {"call_min_priority": 6}), ("phone", {"call_min_priority": True}),
                                ("phone", {"max_calls_per_hour": 101}), ("phone", {"max_calls_per_day": -1}),
                                ("phone", {"poll_seconds": 1}), ("phone", {"poll_seconds": "5"}),
                                ("gateway", {"url": "http://example.com"}), ("gateway", {"url": "ftp://x"}),
                                ("gateway", {"url": "https://a b"}), ("gateway", {"token": 5})):
            with self.subTest(section=section, values=values):
                self.write(dict(self.live(), **{section: values}))
                with self.assertRaises(ValueError):
                    config.load(self.path)


class CLITests(unittest.TestCase):
    def run_cli(self, *argv):
        sent = []
        with patch.object(notify, "request", lambda path, payload=None, *a, **k: sent.append((path, payload)) or {"ok": True}), \
                patch("sys.argv", ["notify.py", *argv]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(notify.main(), 0)
        return sent

    def test_call_flags(self):
        self.assertIs(self.run_cli("send", "hi", "--call")[0][1]["call"], True)
        self.assertIs(self.run_cli("send", "hi", "--no-call")[0][1]["call"], False)
        self.assertNotIn("call", self.run_cli("send", "hi")[0][1])
        self.assertIs(self.run_cli("phone", "test", "--call")[0][1]["call"], True)
        test_text = self.run_cli("phone", "test")[0][1]
        self.assertEqual(test_text["priority"], 4)
        self.assertIn("SMS", test_text["message"])
        self.assertEqual(self.run_cli("phone", "ack", "abcd1234")[0], ("/phone/calls/abcd1234/ack", {}))
        self.assertEqual(self.run_cli("phone", "calls")[0][0], "/phone/calls?limit=20")
        self.assertIs(bus.build_parser("notify").parse_args(["x", "--call"]).call, True)
        self.assertIsNone(bus.build_parser("notify").parse_args(["x"]).call)

    def test_setup_commands_route_to_the_right_flows(self):
        seen = []
        with patch.object(phone_setup, "cli", lambda function, *a, **k: seen.append((function.__name__, a, k)) or 0):
            for argv in (["setup", "phone"], ["setup", "gateway", "--rotate"], ["setup", "twilio"], ["setup", "telegram"],
                         ["gateway", "add-client", "planner"]):
                with patch("sys.argv", ["notify.py", *argv]):
                    self.assertEqual(notify.main(), 0)
        self.assertEqual(seen, [("setup_phone", (), {}), ("setup_gateway", (), {"rotate": True}), ("setup_twilio", (), {}),
                                ("setup_telegram", (), {"primary": False}), ("add_client", ("planner",), {})])

    def test_bus_op_notify_and_format(self):
        posted = []
        client = SimpleNamespace(post=lambda path, payload: posted.append(payload) or {})
        bus.op_notify(client, {"id": AGENT}, "down", priority=3, call=True)
        bus.op_notify(client, {"id": AGENT}, "fyi", call=False)
        self.assertEqual((posted[0]["call"], posted[1]["call"]), (True, False))
        text = bus.format_notify({"id": "e1", "status": "queued", "provider": "ntfy", "duplicate": False,
                                  "escalation": {"text": "sent with the call", "call": "queued"}})
        self.assertIn("SMS text: sent with the call", text)
        self.assertIn("phone call: queued", text)
        self.assertNotIn("Telegram", text)


class FakeRun:
    def __init__(self, outputs=None):
        self.calls, self.outputs = [], outputs or {}

    def __call__(self, cmd, cwd=None, input=None, **kwargs):
        self.calls.append({"cmd": cmd, "cwd": cwd, "input": input, "stdin": kwargs.get("stdin"), "timeout": kwargs.get("timeout")})
        out = self.outputs.get(cmd[3], "")
        return SimpleNamespace(returncode=out[0] if isinstance(out, tuple) else 0, stdout=out[1] if isinstance(out, tuple) else out,
                               stderr="")

    def secrets(self):
        return {c["cmd"][-1]: c["input"] for c in self.calls if c["cmd"][3:5] == ["secret", "put"]}


VOICE_SMS = {"voice": True, "sms": True, "mms": True, "fax": False}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        (self.root / "gateway").mkdir()
        self.patch = patch.object(phone_setup, "GATEWAY_DIR", self.root / "gateway")
        self.patch.start()
        self.printed = []
        self.answers = []
        self.sid = "AC" + "1" * 32

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def io(self, run=None, http=None, secret=""):
        return phone_setup.IO(ask=lambda prompt: self.printed.append(prompt) or self.answers.pop(0), ask_secret=lambda prompt: secret,
                              run=run or FakeRun(), http=http, print=lambda *a: self.printed.append(" ".join(map(str, a))),
                              which=lambda name: "C:/Program Files/nodejs/npx.cmd", sleep=lambda s: None, clock=time.monotonic)

    def output(self):
        return "\n".join(self.printed)

    def save(self, **sections):
        cfg = config.load(self.path, validate=False)
        for name, values in sections.items():
            cfg[name].update(values)
        config.save(cfg, self.path)

    def test_add_client_uploads_and_prints_the_token_once(self):
        self.save(gateway={"url": "https://agent-bus-phone.cam.workers.dev", "token": TOKEN})
        run = FakeRun()
        self.assertEqual(phone_setup.add_client(self.io(run), "planner", self.path), 0)
        token = run.secrets()["TOKEN_PLANNER"]
        self.assertTrue(token.startswith("gw_planner_") and len(token) == len("gw_planner_") + 43)
        self.assertEqual(run.calls[0]["cmd"], ["C:/Program Files/nodejs/npx.cmd", "--yes", "wrangler@4", "secret", "put", "TOKEN_PLANNER"])
        self.assertEqual(run.calls[0]["cwd"], str(self.root / "gateway"))
        self.assertEqual(self.output().count(token), 1)
        self.assertIn("PHONE_GATEWAY_URL=https://agent-bus-phone.cam.workers.dev", self.output())
        self.assertNotIn(token, self.path.read_text())
        for bad in ("bus", "Planner", "1x", "a-b", "x" * 33):
            with self.assertRaises(phone_setup.SetupError):
                phone_setup.add_client(self.io(), bad, self.path)

    def test_wrangler_output_is_scrubbed(self):
        run = FakeRun({"secret": (1, "error uploading SECRETVALUE123")})
        with self.assertRaises(phone_setup.SetupError) as error:
            phone_setup.put_secret(self.io(run), "TOKEN_X", "SECRETVALUE123")
        self.assertNotIn("SECRETVALUE123", str(error.exception))

    def test_setup_gateway(self):
        # A saved (legacy) Telegram bot is no longer uploaded: the Worker texts by Twilio SMS.
        self.save(telegram={"bot_token": "123456:" + "A" * 35, "chat_id": "777"})
        http = lambda method, url, *a, **k: (200, {"ok": True, "twilio": False, "text_channel": "sms", "dry_run": False})
        run = FakeRun({"whoami": "You are logged in with an OAuth Token", "deploy": "Uploaded agent-bus-phone\n  https://agent-bus-phone.cam-1.workers.dev\n"})
        self.assertEqual(phone_setup.setup_gateway(self.io(run, http), path=self.path), 0)
        cfg = config.load(self.path)
        self.assertEqual(cfg["gateway"]["url"], "https://agent-bus-phone.cam-1.workers.dev")
        token = cfg["gateway"]["token"]
        self.assertTrue(token.startswith("gw_bus_"))
        self.assertEqual(run.secrets(), {"TOKEN_BUS": token})
        self.assertEqual([c["cmd"][3] for c in run.calls][:2], ["whoami", "deploy"])
        # Nothing wrangler runs can wait on a prompt: no-secret commands get no stdin, secrets arrive on a pipe; all time out.
        deploy = run.calls[1]
        self.assertEqual((deploy["stdin"], deploy["input"]), (subprocess.DEVNULL, None))
        self.assertTrue(all(c["timeout"] and c["timeout"] <= 300 for c in run.calls))
        self.assertTrue(all(c["input"] and c["stdin"] is None for c in run.calls if c["cmd"][3] == "secret"))
        self.assertNotIn(token, self.output())
        self.assertIn("calls and SMS texts not set up yet (python notify.py setup twilio)", self.output())
        phone_setup.setup_gateway(self.io(run, http), path=self.path)
        self.assertEqual(config.load(self.path)["gateway"]["token"], token)  # reused
        phone_setup.setup_gateway(self.io(run, http), rotate=True, path=self.path)
        self.assertNotEqual(config.load(self.path)["gateway"]["token"], token)
        self.assertFalse([c for c in run.calls if "TELEGRAM" in " ".join(c["cmd"])])
        self.printed.clear()
        logged_out = FakeRun({"whoami": "You are not authenticated. Please run `wrangler login`."})
        self.assertEqual(phone_setup.setup_gateway(self.io(logged_out, http), path=self.path), 1)
        self.assertIn("npx wrangler login", self.output())
        self.assertEqual(len(logged_out.calls), 1)

    def test_setup_gateway_without_a_workers_dev_subdomain(self):
        http = lambda method, url, *a, **k: self.fail("no health check without a URL")
        no_subdomain = ("\x1b[33m▲ [WARNING]\x1b[0m Uploaded agent-bus-phone\n✘ [ERROR] You need to register a workers.dev "
                        "subdomain before publishing to workers.dev (key cf-SECRETKEY-123)\n")
        for code in (1, 0):
            with self.subTest(code=code):
                self.printed.clear()
                run = FakeRun({"whoami": "You are logged in", "deploy": (code, no_subdomain)})
                with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "cf-SECRETKEY-123"}):
                    self.assertEqual(phone_setup.setup_gateway(self.io(run, http), path=self.path), 1)
                self.assertIn("You need to register a workers.dev subdomain", self.output())
                self.assertIn("Open https://dash.cloudflare.com → Workers & Pages once and choose a workers.dev subdomain, "
                              "then run this again.", self.output())
                self.assertNotIn("cf-SECRETKEY-123", self.output())
                self.assertNotIn("\x1b[", self.output())
                self.assertEqual([c["cmd"][3] for c in run.calls], ["whoami", "deploy"])  # no secrets uploaded
                self.assertEqual(config.load(self.path, validate=False)["gateway"]["url"], "")

    def twilio_http(self, requests, owned=(), sms=None):
        def http(method, url, body=None, headers=None, form=None, timeout=20):
            requests.append((method, url, form if form is not None else body))
            if "api.twilio.com" in url:
                self.assertTrue(headers["Authorization"].startswith("Basic "))
            if url.endswith(f"{self.sid}.json"):
                return 200, {"type": "Trial", "status": "active"}
            if "IncomingPhoneNumbers.json?" in url:
                return 200, {"incoming_phone_numbers": list(owned)}
            if "AvailablePhoneNumbers" in url:
                self.assertIn("VoiceEnabled=true", url)
                self.assertIn("SmsEnabled=true", url)
                found = [{"phone_number": "+12265550100", "capabilities": {"voice": True, "SMS": True, "MMS": True}}]
                return 200, {"available_phone_numbers": found if "AreaCode=226" in url else []}
            if url.endswith("IncomingPhoneNumbers.json") and method == "POST":
                return 201, {"sid": "PN1", "phone_number": "+12265550100", "capabilities": VOICE_SMS}
            if re.search(r"IncomingPhoneNumbers/PN\d\.json$", url):
                return 200, {"sid": url.rsplit("/", 1)[1][:3]}
            if url.endswith("/v1/alerts") and method == "POST":
                return sms or (201, {"alert": {"id": "al_1", "status": "sent", "text": {"status": "sent", "message_id": "SM1"}}})
            if "/v1/alerts/" in url:
                return 200, {"alert": {"id": "al_2", "status": "acked"}}
            return 200, {"ok": True, "twilio": True, "text_channel": "sms"}
        return http

    def test_setup_twilio_buys_only_after_yes_and_points_voice_and_sms_at_the_gateway(self):
        auth = "f" * 32
        self.save(gateway={"url": "https://agent-bus-phone.cam.workers.dev", "token": TOKEN})
        before = self.path.read_text()
        requests, run = [], FakeRun()
        self.answers = [self.sid, "n"]
        self.assertEqual(phone_setup.setup_twilio(self.io(run, self.twilio_http(requests), auth), self.path), 1)
        self.assertFalse([r for r in requests if r[0] == "POST"])
        self.assertEqual(run.calls, [])
        self.answers = [self.sid, "y", "519-555-0123"]
        self.assertEqual(phone_setup.setup_twilio(self.io(run, self.twilio_http(requests), auth), self.path), 0)
        posts = [r for r in requests if r[0] == "POST"]
        self.assertEqual(posts[0][2], {"PhoneNumber": "+12265550100", "FriendlyName": "Agent Bus"})
        self.assertEqual(posts[1][2], {"VoiceUrl": "https://agent-bus-phone.cam.workers.dev/twilio/incoming", "VoiceMethod": "POST",
                                       "SmsUrl": "https://agent-bus-phone.cam.workers.dev/twilio/sms", "SmsMethod": "POST"})
        self.assertEqual(run.secrets(), {"TWILIO_ACCOUNT_SID": self.sid, "TWILIO_AUTH_TOKEN": auth,
                                         "TWILIO_FROM": "+12265550100", "OWNER_NUMBER": "+15195550123"})
        self.assertIn("TRIAL", self.output())
        self.assertIn("only number the gateway will ever call or text", self.output())
        self.assertIn("texting 1 acknowledges it", self.output())
        self.assertIn("Save +12265550100 in your contacts as 'Agent Bus'", self.output())
        self.assertNotIn(auth, self.output())
        self.assertEqual(self.path.read_text(), before)  # nothing Twilio-related on disk

    def test_setup_twilio_reuses_only_numbers_that_can_call_and_text(self):
        auth = "f" * 32
        self.save(gateway={"url": "https://agent-bus-phone.cam.workers.dev", "token": TOKEN})
        voice_only = {"sid": "PN7", "phone_number": "+15195550111", "capabilities": {"voice": True, "sms": False}}
        capable = {"sid": "PN8", "phone_number": "+15195550122", "capabilities": VOICE_SMS}
        requests = []
        self.answers = [self.sid, "5195550123"]
        self.assertEqual(phone_setup.setup_twilio(self.io(FakeRun(), self.twilio_http(requests, [voice_only, capable]), auth),
                                                  self.path), 0)
        self.assertIn("Skipping your number +15195550111: it cannot both call and text.", self.output())
        self.assertIn("Using your existing Twilio number +15195550122.", self.output())
        self.assertFalse([r for r in requests if "AvailablePhoneNumbers" in r[1]])
        self.assertTrue(requests[-1][1].endswith("/v1/health"))
        updated = [r for r in requests if r[0] == "POST" and "IncomingPhoneNumbers/" in r[1]]
        self.assertEqual([r[1].rsplit("/", 1)[1] for r in updated], ["PN8.json"])
        # Only a voice-only number: search for one that does both (and buy only after yes).
        requests.clear()
        self.answers = [self.sid, "n"]
        self.assertEqual(phone_setup.setup_twilio(self.io(FakeRun(), self.twilio_http(requests, [voice_only]), auth), self.path), 1)
        self.assertTrue([r for r in requests if "AvailablePhoneNumbers" in r[1]])

    def phone_flow(self, sms=None):
        self.save(telegram={"bot_token": "", "chat_id": ""})
        requests = []
        run = FakeRun({"whoami": "You are logged in", "deploy": "Uploaded\n https://agent-bus-phone.cam.workers.dev\n"})
        self.answers = [self.sid, "y", "519 555 0123", "n"]
        result = phone_setup.setup_phone(self.io(run, self.twilio_http(requests, sms=sms), "f" * 32), path=self.path)
        return result, requests, run

    def test_setup_phone_is_gateway_then_twilio_then_a_test_text(self):
        result, requests, run = self.phone_flow()
        self.assertEqual(result, 0)
        self.assertEqual(self.answers, [])
        self.assertFalse([r for r in requests if "telegram" in r[1]], "no Telegram step")
        self.assertEqual(set(run.secrets()), {"TOKEN_BUS", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "OWNER_NUMBER"})
        [test] = [r for r in requests if r[1].endswith("/v1/alerts")]
        self.assertEqual(test[2]["channels"], ["text"])
        self.assertTrue(test[2]["ref"].startswith("bus-test-sms-"))
        self.assertIn("Sent a test text to your cell.", self.output())
        self.assertIn("Place a test call now? [y/N]: ", self.output())
        self.assertLess(self.output().index("Sent a test text"), self.output().index("Place a test call now?"))
        self.assertNotIn("Telegram", self.output())

    def test_setup_phone_explains_a_failed_test_text(self):
        detail = "Twilio 400: Permission to send an SMS has not been enabled for the region (code 21408)"
        failed = (201, {"alert": {"id": "al_1", "status": "text_failed", "text": {"status": "failed", "detail": detail}}})
        result, requests, run = self.phone_flow(sms=failed)
        self.assertEqual(result, 0)
        self.assertIn(f"The test text failed: {detail}", self.output())
        self.assertIn("Canadian numbers sometimes need SMS registration. Check Twilio Console > Monitor > Logs > Messaging; "
                      "calls still work.", self.output())
        self.assertIn("Place a test call now? [y/N]: ", self.output())

    def test_test_call_from_setup_phone(self):
        self.save(gateway={"url": "https://agent-bus-phone.cam.workers.dev", "token": TOKEN})
        requests = []
        phone_setup.test_call(self.io(http=self.twilio_http(requests)), self.path)
        self.assertEqual(requests[0][2]["channels"], ["call"])
        self.assertIn("It works.", self.output())

    def test_setup_telegram_is_legacy_and_uploads_nothing(self):
        token = "123456:" + "B" * 35
        self.save(ntfy={"topic": "abc"}, gateway={"url": "https://agent-bus-phone.cam.workers.dev", "token": TOKEN})
        cfg = config.load(self.path, validate=False)
        cfg["provider"] = "ntfy"
        config.save(cfg, self.path)
        calls = []

        def http(method, url, body=None, headers=None, form=None, timeout=20):
            calls.append((method, url.split(token)[-1], body))
            if url.endswith("getMe"):
                return 200, {"ok": True, "result": {"username": "agentbus_bot"}}
            if "getUpdates" in url:
                if not any("deleteWebhook" in c[1] for c in calls):
                    return 409, {"ok": False}
                return 200, {"ok": True, "result": [{"update_id": 5, "message": {"chat": {"id": 7, "type": "group"}}},
                                                    {"update_id": 6, "message": {"chat": {"id": 777, "type": "private", "first_name": "Cameron"}}}]}
            return 200, {"ok": True, "result": {"message_id": 1}}
        run = FakeRun()
        self.assertEqual(phone_setup.setup_telegram(self.io(run, http, token), path=self.path), 0)
        saved = config.load(self.path)
        self.assertEqual((saved["telegram"]["chat_id"], saved["telegram"]["bot_token"], saved["provider"]), ("777", token, "ntfy"))
        self.assertEqual(saved["phone"]["text_channel"], "sms", "texts stay SMS")
        self.assertIn("@agentbus_bot", self.output())
        self.assertIn('set phone.text_channel to "telegram"', self.output())
        self.assertEqual(calls[-1][2]["text"], "✅ Agent Bus texts are set up.")
        self.assertEqual(run.secrets(), {}, "the Worker no longer takes Telegram keys")
        self.assertNotIn(token, self.output())
        phone_setup.setup_telegram(self.io(run, http, token), primary=True, path=self.path)
        self.assertEqual(config.load(self.path)["provider"], "telegram")
        with self.assertRaises(phone_setup.SetupError):
            phone_setup.setup_telegram(self.io(run, http, "not-a-token"), path=self.path)


@unittest.skipUnless(os.environ.get("GATEWAY_E2E_URL") and os.environ.get("GATEWAY_E2E_TOKEN"),
                     "set GATEWAY_E2E_URL and GATEWAY_E2E_TOKEN (a DRY_RUN=1 gateway, e.g. npx wrangler dev) to run")
class GatewayE2ETests(unittest.TestCase):
    def setUp(self):
        self.url, self.token = os.environ["GATEWAY_E2E_URL"].rstrip("/"), os.environ["GATEWAY_E2E_TOKEN"]
        self.temp = tempfile.TemporaryDirectory()
        cfg = config.load(Path(self.temp.name) / "config.json")
        cfg["codex"]["enabled"] = False
        cfg["gateway"].update(url=self.url, token=self.token)
        self.app = App(cfg, Path(self.temp.name))

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def simulate(self, gateway_id, body):
        status, data, _ = phone.gateway_http("POST", f"{self.url}/v1/alerts/{gateway_id}/simulate", self.token, body)
        self.assertEqual(status, 200, data)
        return data

    def wait(self, call_id, done, seconds=30):
        deadline = time.time() + seconds
        while not done(self.app.store.call(call_id)) and time.time() < deadline:
            self.app.phone.step()
            time.sleep(0.5)
        return self.app.store.call(call_id)

    def test_call_true_is_acked_through_a_dry_run_gateway(self):
        self.assertEqual(self.app.notify({"message": "End-to-end test", "title": "E2E", "call": True})["escalation"]["call"], "queued")
        call_id = self.app.store.calls(1)[0]["id"]
        gateway_id = self.wait(call_id, lambda c: c["gateway_id"])["gateway_id"]
        self.assertTrue(gateway_id, self.app.store.call(call_id)["error"])
        # Twilio reports the hang-up after the key press; without it the gateway's line stays busy and later calls queue.
        for body in ({"event": "answer"}, {"event": "digits", "digits": "1"}, {"event": "status", "call_status": "completed"}):
            self.simulate(gateway_id, body)
        call = self.wait(call_id, lambda c: c["status"] == "acked")
        self.assertEqual((call["status"], call["via"]), ("acked", "call"))

    def test_sms_text_then_late_sms_reply_acks_an_unanswered_call(self):
        self.app.bus.upsert(AGENT, folder="e2e")
        # Priority 4: one SMS through the gateway, delivered by the queue worker.
        self.assertEqual(self.app.notify({"message": "Needs approval", "title": "E2E text", "priority": 4})["escalation"],
                         {"text": "queued"})
        while self.app.store.pending():
            self.app.deliver_one()
        sms = [r for r in self.app.store.recent(10) if r["provider"] == "sms"][0]
        self.assertEqual(sms["status"], "accepted", sms["error"])
        self.assertTrue(sms["provider_id"].startswith("al_"))
        # Priority 5 from an agent: one call alert that also texts.
        result = self.app.notify({"message": "Prod is down", "title": "E2E call", "source": AGENT, "priority": 5})
        self.assertEqual(result["escalation"], {"text": "sent with the call", "call": "queued"})
        call_id = self.app.store.calls(1)[0]["id"]
        gateway_id = self.wait(call_id, lambda c: c["gateway_id"])["gateway_id"]
        alert = phone.gateway_http("GET", f"{self.url}/v1/alerts/{gateway_id}", self.token)[1]["alert"]
        self.assertEqual((alert["channels"], alert["text"]["status"], alert["status"]), (["text", "call"], "sent", "calling"))
        # No answer, the retry, no answer again: unanswered.
        self.simulate(gateway_id, {"event": "status", "call_status": "no-answer"})
        self.assertEqual(self.simulate(gateway_id, {"event": "advance", "seconds": 180})["alert"]["call"]["attempts"], 2)
        self.assertEqual(self.simulate(gateway_id, {"event": "status", "call_status": "no-answer"})["alert"]["status"], "unanswered")
        call = self.wait(call_id, lambda c: c["status"] == "unanswered")
        self.assertEqual(call["status"], "unanswered")
        # Cameron texts "1" later: the bus sees the late ack and tells the agent once.
        reply = self.simulate(gateway_id, {"event": "sms", "body": "1"})
        self.assertEqual(reply["twiml"], "<Response><Message>Got it: E2E call</Message></Response>")
        self.assertEqual((reply["alert"]["id"], reply["alert"]["status"], reply["alert"]["call"]["via"]), (gateway_id, "acked", "sms"))
        call = self.wait(call_id, lambda c: c["status"] == "acked")
        self.assertEqual((call["status"], call["via"]), ("acked", "sms"))
        dms = [m for m in self.app.bus.history(50, sender="bus", kind="system")["messages"] if m["to"] == [AGENT]]
        self.assertEqual([m["subject"] for m in dms], ["Phone call unanswered", "Phone call acked"])
        self.assertIn("Cameron acknowledged the call (texted back): E2E call", dms[-1]["body"])


if __name__ == "__main__":
    unittest.main()
