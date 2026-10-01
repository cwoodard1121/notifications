"""Phone escalation: priority 4 also texts (Telegram from this PC), priority 5 or call:true also rings through the gateway.

The gateway (docs/PHONE-GATEWAY.md) owns the call itself, its retry and snooze timers. This side submits call alerts,
pulls their outcomes from /v1/updates while any call is open, and tells the agent that asked for the call what happened.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

import providers
from bus_core import BusError, iso

LOG = logging.getLogger("notify")
ACK_PATH = re.compile(r"/phone/calls/([0-9a-f]{4,32})/ack")
ENDPOINTS = ["GET /phone/calls?limit=50", "POST /phone/calls/<id>/ack"]
OPEN = ("submitted", "calling", "waiting_retry", "snoozed")
FINAL = ("acked", "unanswered", "canceled", "failed")
TOLD = ("snoozed",) + FINAL
GATEWAY_STATUS = {"queued": "submitted", "calling": "calling", "waiting_retry": "waiting_retry", "snoozed": "snoozed",
                  "acked": "acked", "unanswered": "unanswered", "canceled": "canceled", "failed": "failed"}
PROVIDER_NAMES = {"ntfy": "ntfy", "telegram": "the Telegram message", "whatsapp": "WhatsApp"}
STALE = 900  # an open call with no news for this long is re-read with GET /v1/alerts/<id>


class GatewayDown(Exception):
    pass


def parse(raw):
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def gateway_http(method, url, token, body=None):
    """(status, JSON object, Retry-After seconds). Raises GatewayDown without the URL or token in the text."""
    headers = {"Authorization": "Bearer " + token, "User-Agent": "LocalNotify/1.0", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), providers.NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, parse(response.read(65536)), 0
    except urllib.error.HTTPError as exc:
        retry_after = (exc.headers.get("Retry-After") if exc.headers else None) or ""
        try:
            body = parse(exc.read(65536))
        except OSError:
            body = {}
        return exc.code, body, int(retry_after) if retry_after.isdigit() else 0
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GatewayDown("gateway connection failed or timed out") from None


def hhmm(value):
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return None


class Phone:
    def __init__(self, app, http=gateway_http, clock=time.time):
        self.app, self.store, self.http, self.clock = app, app.store, http, clock
        self.wake = threading.Event()
        self.lock = threading.RLock()  # call rows; held during a gateway request, so App.notify never takes it
        self.cap_lock = threading.Lock()
        self.stopped = False
        self.last_poll = None
        self.last_error = None

    @property
    def settings(self):
        return self.app.cfg.get("phone") or {}

    @property
    def gateway(self):
        gateway = self.app.cfg.get("gateway") or {}
        return (gateway.get("url") or "").rstrip("/"), gateway.get("token") or ""

    def gateway_configured(self):
        return all(self.gateway)

    def telegram_configured(self):
        telegram = self.app.cfg.get("telegram") or {}
        return bool(telegram.get("bot_token") and telegram.get("chat_id"))

    # ---- escalation (called by App.notify for notifications that were neither held nor duplicates)
    def escalate(self, event, row):
        result = {}
        if event["priority"] >= self.settings.get("text_min_priority", 4):
            result["text"] = self.text(event)
        call = event.get("call")
        if call or (call is None and event["priority"] >= self.settings.get("call_min_priority", 5)):
            result["call"] = self.call(event, row)
        return result

    def text(self, event):
        if self.settings.get("text_channel", "telegram") == "off":
            return "skipped: phone.text_channel is off"
        if self.app.cfg["provider"] == "telegram":
            return "skipped: primary provider is already telegram"
        if not self.telegram_configured():
            return "skipped: telegram not configured (python notify.py setup telegram)"
        key = event.get("dedupe_key")
        # The queue worker delivers it like any other row, with the same retries.
        self.store.add(dict(event, event="escalation", dedupe_key=key + "#text" if key else None), "telegram")
        return "queued"

    def call(self, event, row):
        now = self.clock()
        record = {"id": uuid.uuid4().hex[:16], "event_id": row["id"], "created": now, "updated": now, "title": event["title"],
                  "message": event["message"], "source": event["source"], "status": "queued", "next_attempt": now}
        if not self.gateway_configured():
            self.store.add_call(dict(record, status="dry-run"))
            return "dry-run: gateway not configured (python notify.py setup gateway)"
        with self.cap_lock:
            for window, key, label in ((3600, "max_calls_per_hour", "hour"), (86400, "max_calls_per_day", "day")):
                limit = self.settings.get(key, 4)
                if self.store.call_count(now - window) >= limit:
                    LOG.warning("Phone call not placed: limit of %s per %s reached", limit, label)
                    return f"skipped: call limit reached ({limit} per {label})"
            self.store.add_call(record)
        self.wake.set()
        return "queued"

    # ---- background thread
    def run(self):
        while not self.stopped and not self.app.stop_event.is_set():
            self.wake.clear()
            try:
                delay = self.step()
            except Exception:
                LOG.exception("Phone escalation error")
                delay = 30
            self.wake.wait(delay)

    def stop(self):
        self.stopped = True
        self.wake.set()

    def step(self):
        """One pass: submit due calls, poll outcomes while a call is open. Returns seconds until the next pass."""
        if not self.gateway_configured():
            return 30
        for call in reversed(self.store.calls(100, ("queued",), due=self.clock())):
            self.submit(call["id"])
        delay = 30
        if self.store.calls(1, OPEN):
            self.poll()
            self.refresh()
            delay = self.settings.get("poll_seconds", 5)
        queued = self.store.calls(200, ("queued",))
        if queued:
            delay = min(delay, max(0.5, min(c["next_attempt"] for c in queued) - self.clock()))
        return delay

    def problem(self, text):
        if text != self.last_error:
            LOG.warning("Phone gateway: %s", text)
        self.last_error = text

    def detail(self, data):
        text = str(data.get("detail") or data.get("error") or "no detail")[:200]
        token = self.gateway[1]
        return text.replace(token, "***") if token else text

    def submit(self, call_id):
        url, token = self.gateway
        with self.lock:
            call = self.store.call(call_id)
            if call is None or call["status"] != "queued":
                return
            attempt = call["submit_attempts"] + 1
            body = {"ref": "bus-" + call["id"], "title": call["title"][:150], "message": call["message"][:1500],
                    "channels": ["call"], "retry": True, "fallback_text": False}
            try:
                status, data, retry_after = self.http("POST", url + "/v1/alerts", token, body)
                trouble = f"gateway returned HTTP {status}"
            except GatewayDown as exc:
                status, data, retry_after, trouble = None, {}, 0, str(exc)
            now = self.clock()
            alert = data.get("alert") if isinstance(data.get("alert"), dict) else {}
            fields = {"updated": now, "submit_attempts": attempt}
            if status in (200, 201) and alert.get("id"):
                fields.update(self.alert_fields(alert, call), gateway_id=str(alert["id"])[:100], error=None)
                self.last_error = None
            elif status == 429:
                wait = data.get("retry_after") if type(data.get("retry_after")) is int else retry_after or 60
                wait = max(1, min(wait, 86400))
                # Not a failure: the gateway's own cap. Try again when it says.
                fields.update(submit_attempts=call["submit_attempts"], next_attempt=now + wait,
                              error=f"gateway rate limit: trying again in {wait} s")
            elif status == 503:
                fields.update(status="failed", error=f"gateway: {self.detail(data) if data.get('detail') else 'twilio'} not configured "
                                                     "(python notify.py setup twilio)")
            elif status in (401, 403):
                fields.update(status="failed", error=f"gateway refused the bus token (HTTP {status}); run: python notify.py setup gateway")
            elif status == 400:
                fields.update(status="failed", error="gateway rejected the call: " + self.detail(data))
            elif status is None or status >= 500 or status in (200, 201):
                if attempt <= 5:
                    fields.update(next_attempt=now + 5 * 2 ** (attempt - 1), error=trouble + "; retrying")
                else:
                    fields.update(status="failed", error=trouble)
            else:
                fields.update(status="failed", error=trouble + " (check gateway.url)")
            if fields.get("error"):
                self.problem(fields["error"])
            self.store.update_call(call["id"], **fields)
            self.report(call["id"])

    def alert_fields(self, alert, call):
        fields = {}
        status = GATEWAY_STATUS.get(alert.get("status"))
        if status and call["status"] not in FINAL:
            fields["status"] = status
        info = alert.get("call") if isinstance(alert.get("call"), dict) else {}
        if info.get("digit"):
            fields["digit"] = str(info["digit"])[:4]
        if "next_call_at" in info:
            fields["next_call_at"] = str(info["next_call_at"])[:40] if info["next_call_at"] else None
        if type(info.get("attempts")) is int:
            fields["call_attempts"] = info["attempts"]
        return fields

    def poll(self):
        url, token = self.gateway
        for _ in range(5):
            cursor = self.store.get_state("gateway_cursor", 0)
            cursor = cursor if type(cursor) is int and cursor >= 0 else 0
            try:
                status, data, _ = self.http("GET", f"{url}/v1/updates?after={cursor}&limit=100", token)
            except GatewayDown as exc:
                return self.problem(f"updates: {exc}")
            if status != 200 or not isinstance(data.get("updates"), list):
                hint = "; run: python notify.py setup gateway" if status in (401, 403) else ""
                return self.problem(f"updates: gateway returned HTTP {status}{hint}")
            self.last_poll, self.last_error = self.clock(), None
            updates = [u for u in data["updates"] if isinstance(u, dict)]
            for update in updates:
                self.apply(update)
            following = data.get("next")
            if type(following) is int and following > cursor:
                self.store.set_state("gateway_cursor", following)
            if len(updates) < 100:
                return

    def find(self, alert_id, ref):
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM calls WHERE gateway_id=?", (str(alert_id),)).fetchone() if alert_id else None
        if row is None and isinstance(ref, str) and ref.startswith("bus-"):
            return self.store.call(ref[4:]) if re.fullmatch(r"[0-9a-f]{16}", ref[4:]) else None
        return self.store.call(row["id"]) if row else None

    def apply(self, update):
        with self.lock:
            call = self.find(update.get("alert_id"), update.get("ref"))
            if call is None or call["status"] in FINAL + ("dry-run",):
                return
            fields = {"updated": self.clock()}
            status = GATEWAY_STATUS.get(update.get("status"))
            if status:
                fields["status"] = status
            if update.get("alert_id") and not call["gateway_id"]:
                fields["gateway_id"] = str(update["alert_id"])[:100]
            if update.get("digit"):
                fields["digit"] = str(update["digit"])[:4]
            if "next_call_at" in update:
                fields["next_call_at"] = str(update["next_call_at"])[:40] if update["next_call_at"] else None
            if type(update.get("attempt")) is int:
                fields["call_attempts"] = update["attempt"]
            if status == "failed":
                fields["error"] = str(update.get("detail") or "Twilio refused the call")[:200]
            self.store.update_call(call["id"], **fields)
            self.report(call["id"])

    def refresh(self):
        """Safety net for a missed update (e.g. a reset cursor): re-read open calls that have been quiet for a while."""
        url, token = self.gateway
        for call in self.store.calls(20, OPEN):
            if not call["gateway_id"] or self.clock() - call["updated"] < STALE:
                continue
            try:
                status, data, _ = self.http("GET", f"{url}/v1/alerts/{urllib.parse.quote(call['gateway_id'], safe='')}", token)
            except GatewayDown as exc:
                return self.problem(f"alert lookup: {exc}")
            with self.lock:
                current = self.store.call(call["id"])
                if current is None or current["status"] not in OPEN:
                    continue
                fields = {"updated": self.clock()}
                if status == 200 and isinstance(data.get("alert"), dict):
                    fields.update(self.alert_fields(data["alert"], current))
                elif status == 404:
                    fields.update(status="failed", error="the gateway no longer knows this call")
                self.store.update_call(call["id"], **fields)
                self.report(call["id"])

    # ---- outcomes for the agent that asked for the call
    def report(self, call_id):
        with self.lock:
            call = self.store.call(call_id)
            if call is None or call["status"] not in TOLD:
                return
            mark = call["status"] + (f" {call['next_call_at']}" if call["status"] == "snoozed" else "")
            if call["notified"] == mark:
                return
            self.store.update_call(call_id, notified=mark)
        if self.app.bus.agent(call["source"]) is None:
            return  # only registered agents get a DM ("agent", "notify" and CLI sources never do)
        try:
            self.app.bus.send({"from": "bus", "to": [call["source"]], "kind": "system", "subject": f"Phone call {call['status']}",
                               "body": self.outcome(call), "priority": 3 if call["status"] == "snoozed" else 4,
                               "meta": {"phone_call": {"id": call["id"], "status": call["status"]}}}, internal=True)
        except (ValueError, BusError):
            LOG.warning("Could not tell %s about phone call %s", call["source"], call["id"])

    def also(self):
        parts = []
        if (self.settings.get("text_channel") == "telegram" and self.telegram_configured()
                and self.app.cfg["provider"] != "telegram"):
            parts.append("the text")
        if self.app.cfg["provider"] != "local":
            parts.append(PROVIDER_NAMES.get(self.app.cfg["provider"], self.app.cfg["provider"]))
        return f" (he also got {' and '.join(parts)})" if parts else ""

    def outcome(self, call):
        title, status, digit = call["title"], call["status"], call["digit"]
        if status == "acked":
            return f"📞 Cameron acknowledged{f' (pressed {digit})' if digit else ''} the call: {title}"
        if status == "snoozed":
            when = hhmm(call["next_call_at"])
            return (f"📞 Cameron snoozed the call (pressed 2): {title}. The gateway calls again "
                    f"{'around ' + when if when else 'in about 10 minutes'}.")
        if status == "unanswered":
            tries = call["call_attempts"] or 2
            return f"📞 Cameron did not pick up or acknowledge the call after {tries} tries{self.also()}: {title}"
        if status == "failed":
            return f"📞 The call to Cameron could not be placed: {call['error'] or 'unknown error'}. Call: {title}"
        return f"📞 The call to Cameron was canceled: {title}"

    # ---- API
    def view(self, call):
        return dict(call, ref="bus-" + call["id"], created_iso=iso(call["created"]), updated_iso=iso(call["updated"]))

    def calls(self, limit=50):
        return [self.view(c) for c in self.store.calls(limit)]

    def ack(self, call_id):
        with self.lock:
            call = self.store.call(call_id)
            if call is None:
                raise BusError(404, "Call not found")
            gateway = "not needed"
            fields = {"updated": self.clock()}
            if call["status"] in FINAL:
                gateway = "already " + call["status"]
            elif call["gateway_id"]:
                url, token = self.gateway
                try:
                    status, data, _ = self.http("POST", f"{url}/v1/alerts/{urllib.parse.quote(call['gateway_id'], safe='')}/ack",
                                                token, {})
                except GatewayDown as exc:
                    raise BusError(502, f"Could not reach the gateway ({exc}); the call may still ring. Try again or press 1 on the call.")
                if status != 200:
                    raise BusError(502, f"The gateway did not take the ack (HTTP {status}); the call may still ring.")
                alert = data.get("alert") if isinstance(data.get("alert"), dict) else {}
                fields.update(self.alert_fields(alert, call))
                fields.setdefault("status", "acked")
                gateway = "acked"
            else:
                fields["status"] = "acked"  # queued (never submitted) or dry-run
            self.store.update_call(call["id"], **fields)
            self.report(call["id"])
            return {"call": self.view(self.store.call(call["id"])), "gateway": gateway}

    def health(self):
        return {"text_channel": self.settings.get("text_channel", "telegram"), "telegram_configured": self.telegram_configured(),
                "gateway_configured": self.gateway_configured(), "open_calls": len(self.store.calls(200, OPEN)),
                "last_poll": iso(self.last_poll), "last_error": self.last_error}
