import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
import notify
import providers
from service import App, Server, normalize
from store import Store
from watcher import CodexWatcher


def iso(value=None):
    return datetime.fromtimestamp(value or time.time(), timezone.utc).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = config.load(self.root / "config.json")
        self.cfg["codex"]["enabled"] = False
        # Automatic pings are off by default now; these tests cover the opt-in behaviour.
        self.cfg["claude"]["auto_notify"] = self.cfg["codex"]["auto_notify"] = True
        self.app = App(self.cfg, self.root)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()


class QueueTests(Base):
    def test_history_and_dedupe_survive_restart_and_concurrency(self):
        payload = {"message": "Build complete", "dedupe_key": "build:123"}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.app.notify(payload), range(16)))
        self.assertEqual(sum(not r["duplicate"] for r in results), 1)
        reopened = Store(self.root / "notifications.sqlite3")
        self.assertEqual(len(reopened.recent()), 1)
        self.assertEqual(reopened.recent()[0]["status"], "local")

    def test_transient_delivery_failure_retries_persistently(self):
        self.cfg["provider"] = "ntfy"
        self.app.notify({"message": "hello"})
        self.app.sender = lambda *_: (_ for _ in ()).throw(providers.DeliveryError("temporary", True))
        self.app.deliver_one()
        row = self.app.store.recent()[0]
        self.assertEqual((row["status"], row["attempts"]), ("queued", 1))
        self.assertIsNone(self.app.store.pending())
        with self.app.store.connect() as db:
            db.execute("UPDATE events SET next_attempt=0")
        restarted = App(self.cfg, self.root, sender=lambda *_: "provider-123")
        restarted.deliver_one()
        row = restarted.store.recent()[0]
        self.assertEqual((row["status"], row["attempts"], row["provider_id"]), ("accepted", 2, "provider-123"))
        restarted.close()

    def test_permanent_delivery_error_does_not_retry(self):
        self.cfg["provider"] = "telegram"
        self.app.notify({"message": "hello"})
        self.app.sender = lambda *_: (_ for _ in ()).throw(providers.DeliveryError("HTTP 401"))
        self.app.deliver_one()
        self.assertEqual(self.app.store.recent()[0]["status"], "failed")
        self.assertIsNone(self.app.store.pending())

    def test_recaps_exclude_recaps_and_send_once_each_day(self):
        self.app.notify({"message": "App done", "source": "codex"})
        self.cfg["recap"]["daily_at"] = "18:00"
        now = datetime(2026, 9, 10, 19, 0)
        self.app.schedule_tick(now)
        self.app.schedule_tick(now)
        self.assertEqual(len(self.app.store.recent()), 2)
        self.assertEqual(self.app.recap(24)["count"], 1)
        for value in ("nan", "inf", 0, -1, True, "oops"):
            with self.assertRaises(ValueError):
                self.app.recap(value)


class HTTPTests(Base):
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

    def call(self, path, payload=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = urllib.request.Request(self.base + path, data=data, headers=headers or {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_api_roundtrip_trailing_slash_and_recap(self):
        status, result = self.call("/notify/", {"message": "hello 👋", "source": "test", "dedupe_key": "one"})
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "local")
        self.assertEqual(self.call("/notify", {"message": "hello 👋", "dedupe_key": "one"})[0], 200)
        self.assertEqual(self.call("/events")[1]["events"][0]["message"], "hello 👋")
        self.assertEqual(self.call("/recap?hours=24")[1]["count"], 1)
        self.assertEqual(self.call("/recap", {"hours": 24})[0], 202)
        self.assertEqual(self.call("/health")[1]["provider"], "local")

    def test_bad_requests_and_browser_requests_are_rejected(self):
        for body in ({}, [], {"message": 1}, {"message": "hi", "priority": True}, {"message": "x" * 3001}):
            self.assertEqual(self.call("/notify", body)[0], 400)
        self.assertEqual(self.call("/notify", raw=b"{oops")[0], 400)
        self.assertEqual(self.call("/notify", raw=b"x" * 16385)[0], 413)
        self.assertEqual(self.call("/notify", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.call("/notify", {}, {"Content-Type": "application/json", "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("/health", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("/events?limit=201")[0], 400)
        self.assertEqual(self.call("/recap?hours=nan")[0], 400)
        self.assertEqual(self.call("/missing")[0], 404)

    def test_optional_auth_protects_reads_and_writes(self):
        self.cfg["api_token"] = "test-token"
        self.assertEqual(self.call("/health")[0], 401)
        self.assertEqual(self.call("/notify", {"message": "hi"})[0], 401)
        self.assertEqual(self.call("/health", headers={"Authorization": "Bearer test-token"})[0], 200)

    def test_claude_hook_subprocess_posts_to_api_and_exits_cleanly_when_offline(self):
        self.cfg["port"] = self.server.server_port
        self.cfg["api_token"] = "hook-test-token"
        cfg_path = self.root / "hook-config.json"
        cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        env = dict(os.environ, NOTIFY_CONFIG=str(cfg_path), NOTIFY_DATA_DIR=str(self.root / "hook-data"))
        command = [sys.executable, str(config.ROOT / "notify.py"), "hook", "claude"]
        data = json.dumps({"hook_event_name": "Stop", "session_id": "bridge-test", "last_assistant_message": "Complete"})
        result = subprocess.run(command, input=data, text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.app.store.recent()[0]["source"], "claude")
        # A refused endpoint must not turn a completion hook into an agent failure.
        self.cfg["port"] = 1
        cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        result = subprocess.run(command, input=data, text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertTrue((self.root / "hook-data" / "hooks.log").exists())


class WatcherTests(Base):
    def setUp(self):
        super().setUp()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.cfg["codex"]["sessions_dir"] = str(self.sessions)
        self.watcher = CodexWatcher(self.app)

    def write_session(self, name="s", source="cli"):
        path = self.sessions / (name + ".jsonl")
        row = {"type": "session_meta", "payload": {"id": name, "timestamp": iso(time.time() - 60), "source": source, "cwd": str(self.root / "project")}}
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        return path

    def append(self, path, payload, stamp=None):
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"type": "event_msg", "timestamp": stamp or iso(), "payload": payload}) + "\n")

    def test_no_history_replay_partial_write_and_restart(self):
        path = self.write_session()
        self.append(path, {"type": "task_complete", "turn_id": "old"})
        self.watcher.scan()
        self.assertEqual(len(self.app.store.recent()), 0)
        line = json.dumps({"type": "event_msg", "timestamp": iso(), "payload": {"type": "task_complete", "turn_id": "new", "last_agent_message": "private details"}})
        with path.open("a", encoding="utf-8") as file:
            file.write(line[:30])
        self.watcher.scan()
        self.assertEqual(len(self.app.store.recent()), 0)
        with path.open("a", encoding="utf-8") as file:
            file.write(line[30:] + "\n")
        self.watcher.scan()
        rows = self.app.store.recent()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("private", rows[0]["message"])
        CodexWatcher(self.app).scan()
        self.assertEqual(len(self.app.store.recent()), 1)

    def test_subagent_completions_are_ignored(self):
        path = self.write_session(source={"subagent": {}})
        self.watcher.scan()
        self.append(path, {"type": "task_complete", "turn_id": "sub"})
        self.watcher.scan()
        self.assertEqual(len(self.app.store.recent()), 0)

    def test_truncation_and_new_sessions_are_picked_up(self):
        path = self.write_session()
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"type": "unused", "padding": "x" * 5000}) + "\n")
        self.watcher.scan()
        path.write_text("", encoding="utf-8")
        self.append(path, {"type": "task_complete", "turn_id": "after-truncate"})
        self.watcher.scan()
        self.assertEqual(len(self.app.store.recent()), 1)
        new_path = self.sessions / "new-session.jsonl"
        new_path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "new-session", "timestamp": iso()}}) + "\n", encoding="utf-8")
        self.append(new_path, {"type": "task_complete", "turn_id": "new-session-turn"})
        self.watcher.scan()
        self.assertEqual(len(self.app.store.recent()), 2)

    def test_usage_thresholds_freshness_reset_and_no_spam(self):
        reset = time.time() + 3600
        def quota(used, observed=None, resets=reset):
            self.watcher.usage({"limit_id": "codex", "primary": {"used_percent": used, "resets_at": resets, "window_minutes": 10080}}, observed or time.time())
        quota(79)
        quota(81)
        quota(85)
        quota(91)
        quota(96)
        quota(85)
        self.assertEqual(len(self.app.store.recent()), 3)
        quota(100, observed=time.time() - 100)
        quota(100, resets=time.time() - 1)
        self.assertEqual(len(self.app.store.recent()), 3)
        quota(96, resets=reset + 3600)
        self.assertEqual(len(self.app.store.recent()), 4)

    def test_bootstrap_uses_latest_quota_only(self):
        path = self.write_session()
        for used in (99, 98, 30):
            self.append(path, {"type": "token_count", "rate_limits": {"primary": {"used_percent": used, "resets_at": time.time() + 3000}}})
        self.watcher.scan()
        self.assertEqual(len(self.app.store.recent()), 0)
        self.assertEqual(self.app.store.get_state("codex_usage")["codex"]["windows"]["primary"]["remaining_percent"], 70)


class ProviderTests(Base):
    def event(self, provider):
        return dict(normalize({"message": "Hello 👋", "title": "Agent", "priority": 4}), provider=provider)

    def test_ntfy_json_and_acknowledgement(self):
        self.cfg["ntfy"].update(topic="random-topic", token="secret")
        def post(url, body, headers):
            self.assertEqual(url, "https://ntfy.sh/")
            self.assertEqual(json.loads(body)["topic"], "random-topic")
            self.assertEqual(headers["Authorization"], "Bearer secret")
            return {"id": "ntfy-1"}
        self.assertEqual(providers.send(self.cfg, self.event("ntfy"), post), "ntfy-1")

    def test_telegram_and_whatsapp_payloads(self):
        self.cfg["telegram"].update(bot_token="token", chat_id="123")
        def telegram(url, body, headers):
            self.assertEqual(json.loads(body)["chat_id"], "123")
            self.assertNotIn("parse_mode", json.loads(body))
            return {"ok": True, "result": {"message_id": 123}}
        self.assertEqual(providers.send(self.cfg, self.event("telegram"), telegram), "123")
        self.cfg["whatsapp"].update(account_sid="AC" + "a" * 32, auth_token="secret", to="whatsapp:+15551234567", content_sid="HXtest")
        def whatsapp(url, body, headers):
            from urllib.parse import parse_qs
            params = parse_qs(body.decode())
            self.assertEqual(params["ContentSid"], ["HXtest"])
            self.assertEqual(json.loads(params["ContentVariables"][0])["1"], "Agent")
            self.assertNotIn("Body", params)
            return {"sid": "SMtest", "status": "queued"}
        self.assertEqual(providers.send(self.cfg, self.event("whatsapp"), whatsapp), "SMtest")

    def test_http_errors_are_redacted_and_retry_after_is_respected(self):
        class Opener:
            def open(self, *args, **kwargs):
                raise urllib.error.HTTPError("https://secret-token.example", 429, "secret", {"Retry-After": "120"}, io.BytesIO(b"secret"))
        with self.assertRaises(providers.DeliveryError) as error:
            providers.http_post("https://example.com", b"{}", {}, Opener())
        self.assertTrue(error.exception.retry)
        self.assertEqual(error.exception.delay, 120)
        self.assertNotIn("secret", str(error.exception))


class HookTests(Base):
    def test_hook_ignores_background_work_and_extracts_summary_when_enabled(self):
        base = {"hook_event_name": "Stop", "session_id": "123", "last_assistant_message": "Everything is fixed"}
        self.assertIsNone(notify.hook_payload("claude", dict(base, background_tasks=[{"id": "1"}]), self.cfg))
        self.assertIsNone(notify.hook_payload("claude", dict(base, stop_hook_active=True), self.cfg))
        self.cfg["claude"]["include_summary"] = True
        self.assertEqual(notify.hook_payload("claude", base, self.cfg)["message"], "Everything is fixed")
        self.assertEqual(notify.hook_payload("claude", {"hook_event_name": "Notification", "message": "Approve?"}, self.cfg)["priority"], 4)

    def test_hook_install_is_a_no_op_and_remove_deletes_only_the_old_ping_hooks(self):
        path = self.root / ".claude" / "settings.json"
        path.parent.mkdir()
        existing = {"model": "keep-me", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]}}
        ours = {"type": "command", "command": f'"C:/Python/python.exe" "{(config.ROOT / "notify.py").as_posix()}" hook claude'}
        installed = copy.deepcopy(existing)
        installed["hooks"]["Stop"].append({"hooks": [ours]})
        installed["hooks"]["Notification"] = [{"hooks": [ours]}]
        path.write_text(json.dumps(installed), encoding="utf-8")
        with patch.object(config, "DATA_DIR", self.root / "data"):
            self.assertFalse(notify.configure_claude("install", self.root)["installed"])
            self.assertEqual(json.loads(path.read_text()), installed)
            notify.configure_claude("remove", self.root)
            self.assertEqual(json.loads(path.read_text()), existing)


if __name__ == "__main__":
    unittest.main()
