import hmac
import json
import logging
import math
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import bus_hooks
import bus_routes
import phone
import providers
from bus_core import Bus, BusError, iso
from store import Store

LOG = logging.getLogger("notify")
MAX_BODY = 16384
HOOK_MAX = 8 * 1024 * 1024
HOOK_DRAIN = 64 * 1024 * 1024


def normalize(payload):
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object")
    result = {}
    for key, default, maximum in (("message", None, 3000), ("title", "Notification", 150),
                                  ("source", "agent", 80), ("event", "message", 80)):
        value = payload.get(key, default)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{key} must be a non-empty string of at most {maximum} characters")
        result[key] = value.strip()
    priority = payload.get("priority", 3)
    if type(priority) is not int or not 1 <= priority <= 5:
        raise ValueError("priority must be an integer from 1 to 5")
    result["priority"] = priority
    key = payload.get("dedupe_key")
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 300):
        raise ValueError("dedupe_key must be a string of 1 to 300 characters")
    result["dedupe_key"] = key
    call = payload.get("call")
    if call is not None and type(call) is not bool:
        raise ValueError("call must be true or false")
    if call:
        result["priority"] = 5  # a phone call is an emergency by definition
    result["call"] = call
    return result


def hours_value(value):
    try:
        hours = float(value)
    except (ValueError, TypeError):
        raise ValueError("hours must be a number between 0 and 720") from None
    if isinstance(value, bool) or not math.isfinite(hours) or not 0 < hours <= 720:
        raise ValueError("hours must be a number between 0 and 720")
    return hours


class App:
    def __init__(self, cfg, data_dir, sender=providers.send):
        self.cfg = cfg
        self.store = Store(data_dir / "notifications.sqlite3")
        self.bus = Bus(self.store, cfg)
        self.sender = sender
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.threads = []
        self.started = time.time()
        self.runtime = "standalone"
        self.watcher_status = {"enabled": cfg["codex"]["enabled"], "last_scan": None, "error": None}
        mailbox = (cfg.get("bus") or {}).get("mailbox") or {}
        self.mailbox_status = {"enabled": bool(mailbox.get("enabled") and mailbox.get("dirs")), "dirs": mailbox.get("dirs") or []}
        self.phone = phone.Phone(self)

    def notify(self, payload):
        event = normalize(payload)
        # The user's state is enforced here so every path (HTTP, CLI, mailbox, watcher, recap) is covered.
        state = self.bus.current_state()
        minimum = state.get("phone_min_priority") or 1
        held = state["mode"] != "available" and event["priority"] < minimum
        row, duplicate = self.store.add(event, self.cfg["provider"], status="held" if held else None)
        result = {"id": row["id"], "status": row["status"], "provider": row["provider"], "duplicate": duplicate}
        if held and not duplicate:
            result.update(suppressed=True, reason=f"user is {state['mode']} until {iso(state.get('expires_at'))}: "
                                                  f"only priority>={minimum} reaches the phone")
        else:
            if not held and not duplicate:
                escalation = self.phone.escalate(event, row)
                if escalation:
                    result["escalation"] = escalation
            self.wake.set()
        return result

    def recap(self, hours):
        hours = hours_value(hours)
        count, rows = self.store.recap_rows(time.time() - hours * 3600)
        lines = [f"{count} notification(s) in the last {hours:g} hours."]
        for row in reversed(rows):
            stamp = datetime.fromtimestamp(row["created"]).strftime("%H:%M")
            lines.append(f"{stamp} [{row['source']}] {row['title']}: {row['message'][:160]}")
        if count > len(rows):
            lines.append(f"Showing the most recent {len(rows)}; full history is available at /events.")
        return {"title": "Your agent recap", "message": "\n".join(lines)[:3000], "source": "notify", "event": "recap", "count": count}

    def deliver_one(self):
        row = self.store.pending()
        if row is None:
            return False
        attempt = row["attempts"] + 1
        try:
            reference = self.sender(self.cfg, row)
            self.store.finish(row["id"], "accepted", attempt, provider_id=reference)
        except providers.DeliveryError as exc:
            status = "queued" if exc.retry and attempt < 5 else "failed"
            self.store.finish(row["id"], status, attempt, str(exc), max(exc.delay, min(300, 5 * 2 ** (attempt - 1))))
            LOG.warning("Notification %s: %s (%s)", row["id"], exc, status)
        except Exception:
            self.store.finish(row["id"], "failed", attempt, "Unexpected provider error; check configuration")
            LOG.error("Unexpected delivery error for notification %s", row["id"])
        return True

    def worker(self):
        while not self.stop_event.is_set():
            try:
                if self.deliver_one():
                    continue
            except Exception:
                LOG.exception("Queue worker error")
            self.wake.wait(1)
            self.wake.clear()

    def schedule_tick(self, now=None):
        self.bus.current_state()  # expires the user state on time even when nobody is asking
        now = now or datetime.now()
        daily_at = self.cfg["recap"]["daily_at"]
        if daily_at and now.strftime("%H:%M") >= daily_at:
            day = now.strftime("%Y-%m-%d")
            if self.store.get_state("last_recap_day") != day:
                recap = self.recap(self.cfg["recap"]["hours"])
                if recap["count"]:
                    self.notify(dict(recap, dedupe_key="daily-recap:" + day))
                self.store.set_state("last_recap_day", day)
        day = now.strftime("%Y-%m-%d")
        if self.store.get_state("last_prune_day") != day:
            self.store.prune(self.cfg["retention_days"])
            self.bus.prune()
            self.store.set_state("last_prune_day", day)

    def scheduler(self):
        while not self.stop_event.is_set():
            try:
                self.schedule_tick()
            except Exception:
                LOG.exception("Recap scheduler error")
            self.stop_event.wait(30)

    def start(self):
        targets = [self.worker, self.scheduler, self.phone.run]
        if self.cfg["codex"]["enabled"]:
            from watcher import CodexWatcher
            targets.append(CodexWatcher(self).run)
        if self.mailbox_status["enabled"]:
            from bus_mailbox import MailboxBridge
            targets.append(MailboxBridge(self).run)
        for target in targets:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.phone.stop()
        self.bus.close()
        for thread in self.threads:
            thread.join(timeout=12)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        super().__init__(address, Handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalNotify/1.0"
    hook_errors = 0.0

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def send_body(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def respond(self, status, payload):
        self.send_body(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def allowed(self):
        # Prevent other browser pages and DNS-rebinding hosts from using the local API; the bus UI is same-origin.
        port = self.server.server_address[1]
        if self.headers.get("Host", "").lower() not in {f"localhost:{port}", f"127.0.0.1:{port}"}:
            self.respond(403, {"error": "Use localhost or 127.0.0.1 with the service port"})
            return False
        origin = self.headers.get("Origin")
        if (origin and origin.lower() not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}) or \
                self.headers.get("Sec-Fetch-Site", "none").lower() not in ("same-origin", "none"):
            self.respond(403, {"error": "Browser requests are only accepted from the bus UI on this port"})
            return False
        token = self.server.app.cfg["api_token"]
        if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
            self.respond(401, {"error": "Bearer token required"})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        app = self.server.app
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)
        try:
            if bus_routes.owns(path):
                result = bus_routes.get(self, app, path, query)
                if result is not None:
                    self.respond(*result)
            elif path == "/":
                self.respond(200, {"service": "Local Notify", "endpoints": ["POST /notify", "GET /health", "GET /events",
                                                                             "GET /recap?hours=24", "POST /recap"] + phone.ENDPOINTS + bus_routes.ENDPOINTS})
            elif path == "/health":
                self.respond(200, {"ok": True, "service": "local-notify", "runtime": app.runtime, "pid": os.getpid(), "provider": app.cfg["provider"],
                                   "uptime_seconds": round(time.time() - app.started), "events": app.store.counts(),
                                   "codex": app.watcher_status, "usage": app.store.get_state("codex_usage", {}),
                                   "bus": dict(app.bus.health(), mailbox=app.mailbox_status), "phone": app.phone.health()})
            elif path == "/events":
                limit = int(query.get("limit", ["50"])[0])
                if not 1 <= limit <= 200:
                    raise ValueError("limit must be between 1 and 200")
                self.respond(200, {"events": app.store.recent(limit)})
            elif path == "/recap":
                self.respond(200, app.recap(query.get("hours", [24])[0]))
            elif path == "/phone/calls":
                limit = int(query.get("limit", ["50"])[0])
                if not 1 <= limit <= 200:
                    raise ValueError("limit must be between 1 and 200")
                self.respond(200, {"calls": app.phone.calls(limit)})
            else:
                self.respond(404, {"error": "Endpoint not found"})
        except BusError as exc:
            self.respond(exc.status, exc.payload)
        except ValueError as exc:
            self.respond(400, {"error": str(exc)})
        except OSError:
            pass  # client went away (long-poll)
        except Exception:
            LOG.exception("GET %s failed", path)
            self.respond(500, {"error": "Internal error; see service.log"})

    def read_hook_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks, total = [], 0
            while True:
                size = int(self.rfile.readline(100).split(b";")[0].strip() or b"0", 16)
                if not size:
                    while self.rfile.readline(1000) not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                total += size
                if total > HOOK_MAX:
                    self.close_connection = True
                    return None
                chunks.append(self.rfile.read(size))
                self.rfile.readline(10)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        if length > HOOK_MAX:
            # Oversize payloads (huge tool inputs) are drained and ignored; hooks never fail visibly.
            self.close_connection = True
            remaining = min(length, HOOK_DRAIN)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                remaining -= len(chunk)
            return None
        raw = self.rfile.read(length)
        return raw if len(raw) == length else None

    def hook(self):
        result = {}
        try:
            raw = self.read_hook_body()
            if raw:
                result = bus_hooks.handle(self.server.app.bus, json.loads(raw.decode("utf-8-sig"))) or {}
        except ValueError:
            result = {}  # malformed body: nothing to log
        except Exception:
            result = {}
            if time.time() - Handler.hook_errors > 60:
                Handler.hook_errors = time.time()
                LOG.warning("Hook adapter error (returned {})", exc_info=True)
        try:
            self.respond(200, result if isinstance(result, dict) else {})
        except OSError:
            pass

    def do_POST(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/hooks/claude":
            self.hook()
            return
        if path not in ("/notify", "/recap") and path not in bus_routes.POST_PATHS and not phone.ACK_PATH.fullmatch(path):
            self.respond(404, {"error": "Endpoint not found"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.respond(400, {"error": "Send Content-Length, not chunked encoding"})
            return
        if self.headers.get_content_type() != "application/json":
            self.respond(415, {"error": "Use Content-Type: application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.respond(413, {"error": "JSON body must be between 1 and 16384 bytes"})
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Send a JSON object")
            if phone.ACK_PATH.fullmatch(path):
                self.respond(200, self.server.app.phone.ack(phone.ACK_PATH.fullmatch(path)[1]))
                return
            if path not in ("/notify", "/recap"):
                self.respond(*bus_routes.post(self.server.app, path, payload))
                return
            if path == "/recap":
                payload = self.server.app.recap(payload.get("hours", 24))
            result = self.server.app.notify(payload)
            self.respond(200 if result["duplicate"] else 202, result)
        except BusError as exc:
            self.respond(exc.status, exc.payload)
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {"error": str(exc)})
        except TimeoutError:
            self.respond(408, {"error": "Request timed out"})
        except Exception:
            LOG.exception("POST %s failed", path)
            self.respond(500, {"error": "Internal error; see service.log"})
