// Phone gateway core: routing, alert state machine, timers, limits.
// Runs in plain Node (tests) and inside the PhoneGateway Durable Object. It never imports
// cloudflare:workers; storage, env, clock and fetch are injected.
//
// Storage keys (Durable Object KV API):
//   alert:<id>                 alert record (public fields + internal "_")
//   ref:<client>:<ref>         -> alert id (idempotency index)
//   upd:<client>:<seq 12 dig>  update feed entry
//   seq                        last global update seq
//   open                       ids of call alerts that are not final
//   line                       the one call in progress {alert_id, attempt, sid, started_ms, seen_ms}
//   cnt:dials | cnt:texts | cnt:calls:<client>   rolling-24h timestamp lists
//   dry:offset                 dry-run clock offset (ms)
//   prune_at                   next prune time

import { authenticate } from "./auth.js";
import { alertSpeech, DEFAULT_CALL_INTRO } from "./speech.js";
import { menuTwiml, sayHangupTwiml, rejectTwiml, emptyTwiml, messageTwiml } from "./twiml.js";
import { createCall, endCall, sendSms, twilioConfigured, validateTwilioSignature } from "./twilio.js";
import { replyIntent, smsBody, smsClean } from "./sms.js";
import { cpLength, error, iso, json, plural, randomHex, xmlResponse } from "./util.js";

export const SERVICE = "agent-bus-phone";
export const VERSION = 1;
export const MAX_BODY_BYTES = 16 * 1024;

const MINUTE = 60 * 1000;
const DAY = 24 * 60 * MINUTE;
const STUCK_MS = 10 * MINUTE;
const QUEUE_RECHECK_MS = 20 * 1000;
const KEEP_MS = 7 * DAY;
const PRUNE_EVERY_MS = 6 * 60 * MINUTE;
const REPLAY_WINDOW_MS = DAY;
const MAX_ADVANCE_SECONDS = 30 * 24 * 3600;

const FINAL = new Set(["acked", "done", "unanswered", "canceled", "failed", "sent", "text_failed"]);
// Closed by Cameron or the client: never replayed on a call-back.
const CLOSED = new Set(["acked", "done", "canceled"]);
const TERMINAL_CALL_STATUS = new Set(["completed", "busy", "no-answer", "failed", "canceled"]);
const KNOWN_CALL_STATUS = new Set([...TERMINAL_CALL_STATUS, "queued", "initiated", "ringing", "in-progress"]);
const REF_RE = /^[A-Za-z0-9_.:@#-]{1,120}$/;
const ALERT_PATH_RE = /^\/v1\/alerts\/([^/]+)(?:\/([^/]+))?$/;
const ALERT_ID_RE = /^al_[0-9a-f]{16}$/;

const TWILIO_ROUTES = new Set(["/twilio/voice", "/twilio/gather", "/twilio/status", "/twilio/incoming", "/twilio/sms"]);
const MAX_SMS_BODY = 1600;

// ---------------------------------------------------------------- config

function intVar(env, name, def) {
  const raw = env[name];
  if (raw === undefined || raw === null || raw === "") return def;
  const n = Number.parseInt(String(raw), 10);
  return Number.isFinite(n) && n >= 0 ? n : def;
}

export function config(env) {
  return {
    dryRun: String(env.DRY_RUN ?? "0").trim() === "1",
    voice: env.TTS_VOICE || "Polly.Matthew-Neural",
    callIntro: env.CALL_INTRO ?? DEFAULT_CALL_INTRO,
    ringSeconds: intVar(env, "RING_SECONDS", 25),
    retryMinutes: intVar(env, "RETRY_MINUTES", 3),
    snoozeMinutes: intVar(env, "SNOOZE_MINUTES", 10),
    maxAttempts: Math.max(1, intVar(env, "MAX_ATTEMPTS", 2)),
    maxSnoozes: intVar(env, "MAX_SNOOZES", 3),
    clientDailyCalls: intVar(env, "CLIENT_DAILY_CALLS", 10),
    maxDials: intVar(env, "MAX_DIALS_PER_DAY", 30),
    maxTexts: intVar(env, "MAX_TEXTS_PER_DAY", 100),
  };
}

export function healthResponse(env) {
  return json(200, {
    ok: true,
    service: SERVICE,
    version: VERSION,
    dry_run: config(env).dryRun,
    twilio: twilioConfigured(env),
    text_channel: "sms",
  });
}

/**
 * Worker-level routing that needs no state: health, and 404 for paths the gateway never serves.
 * Returns a Response, or null when the request must go to the Durable Object.
 */
export function frontDoor(request, env) {
  const { pathname } = new URL(request.url);
  if (pathname === "/v1/health") return request.method === "GET" ? healthResponse(env) : notFound();
  if (pathname.startsWith("/v1/") || TWILIO_ROUTES.has(pathname)) return null;
  return notFound();
}

function notFound() {
  return error(404, "not_found", "no such route");
}

// ---------------------------------------------------------------- body helpers

async function readBody(request) {
  const len = Number.parseInt(request.headers.get("Content-Length") || "", 10);
  if (Number.isFinite(len) && len > MAX_BODY_BYTES) return { tooLarge: true };
  const buf = await request.arrayBuffer();
  if (buf.byteLength > MAX_BODY_BYTES) return { tooLarge: true };
  return { text: new TextDecoder().decode(buf) };
}

async function readJson(request) {
  const body = await readBody(request);
  if (body.tooLarge) return { error: error(400, "invalid", `body exceeds ${MAX_BODY_BYTES} bytes`) };
  if (!body.text.trim()) return { error: error(400, "invalid", "body must be a JSON object") };
  let value;
  try {
    value = JSON.parse(body.text);
  } catch {
    return { error: error(400, "invalid", "body is not valid JSON") };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { error: error(400, "invalid", "body must be a JSON object") };
  }
  return { value };
}

function validateCreate(b) {
  const bad = (detail) => ({ error: detail });
  if (typeof b.ref !== "string" || !REF_RE.test(b.ref)) return bad("ref must be 1-120 characters of [A-Za-z0-9_.:@#-]");
  for (const [field, max] of [
    ["title", 150],
    ["message", 1500],
  ]) {
    const v = b[field];
    if (typeof v !== "string") return bad(`${field} is required and must be a string`);
    const n = cpLength(v);
    if (n < 1 || n > max || v.trim() === "") return bad(`${field} must be 1-${max} characters`);
  }
  const ch = b.channels;
  if (!Array.isArray(ch) || ch.length === 0) return bad('channels must be a non-empty array of "call" and/or "text"');
  for (const c of ch) {
    if (c !== "call" && c !== "text") return bad(`unknown channel ${JSON.stringify(c)}; use "call" and/or "text"`);
  }
  if (new Set(ch).size !== ch.length) return bad("channels must not repeat");
  for (const f of ["retry", "fallback_text"]) {
    if (b[f] !== undefined && b[f] !== null && typeof b[f] !== "boolean") return bad(`${f} must be true or false`);
  }
  return {
    value: {
      ref: b.ref,
      title: b.title,
      message: b.message,
      channels: [...ch],
      retry: typeof b.retry === "boolean" ? b.retry : true,
      fallback_text: typeof b.fallback_text === "boolean" ? b.fallback_text : true,
    },
  };
}

function pad(seq) {
  return String(seq).padStart(12, "0");
}

function publicAlert(a) {
  if (!a) return null;
  return {
    id: a.id,
    client: a.client,
    ref: a.ref,
    title: a.title,
    message: a.message,
    channels: a.channels,
    status: a.status,
    created: a.created,
    updated: a.updated,
    call: a.call ? { ...a.call } : null,
    text: a.text ? { ...a.text } : null,
  };
}

// ---------------------------------------------------------------- the gateway

export class Gateway {
  /**
   * @param {object} o
   * @param {object} o.storage  DO storage subset: get/put/delete/list({prefix,startAfter,limit}), getAlarm/setAlarm/deleteAlarm
   * @param {object} o.env      vars + secrets
   * @param {() => number} [o.now]  clock in ms
   * @param {typeof fetch} [o.fetch] outbound fetch (Twilio)
   */
  constructor({ storage, env, now = () => Date.now(), fetch: fetchFn, log = console }) {
    this.storage = storage;
    this.env = env;
    this._clock = now;
    this._fetch = fetchFn || ((...args) => globalThis.fetch(...args));
    this.log = log;
    this._offset = 0;
    this.cfg = config(env);
    this._chain = Promise.resolve();
  }

  // All work is serialized: outbound fetches would otherwise let requests interleave.
  _serial(fn) {
    const run = this._chain.then(fn, fn);
    this._chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  async _begin() {
    this.cfg = config(this.env);
    this._offset = this.cfg.dryRun ? (await this.storage.get("dry:offset")) || 0 : 0;
  }

  now() {
    return this._clock() + this._offset;
  }

  handle(request) {
    return this._serial(async () => {
      try {
        await this._begin();
        return await this._route(request);
      } catch (e) {
        this.log.error("gateway error", e && e.stack ? e.stack : e);
        return error(500, "internal", "internal error");
      }
    });
  }

  alarm() {
    return this._serial(async () => {
      try {
        await this._begin();
        await this.runDue();
      } catch (e) {
        this.log.error("alarm error", e && e.stack ? e.stack : e);
        try {
          await this.storage.setAlarm(this._clock() + MINUTE);
        } catch {
          /* ignore */
        }
      }
    });
  }

  // ------------------------------------------------------------ routing

  async _route(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;
    if (path === "/v1/health") return method === "GET" ? healthResponse(this.env) : notFound();
    if (TWILIO_ROUTES.has(path)) return method === "POST" ? this._twilio(request, url) : notFound();
    if (!path.startsWith("/v1/")) return notFound();

    const client = await authenticate(request, this.env);
    if (!client) return error(401, "unauthorized");

    if (path === "/v1/alerts") return method === "POST" ? this._create(request, url, client) : notFound();
    if (path === "/v1/updates") return method === "GET" ? this._updates(url, client) : notFound();

    const m = ALERT_PATH_RE.exec(path);
    if (!m) return notFound();
    const [, id, action] = m;
    if (action === undefined && method !== "GET") return notFound();
    if (action !== undefined && method !== "POST") return notFound();
    if (action !== undefined && !["ack", "cancel", "simulate"].includes(action)) return notFound();
    if (action === "simulate" && !this.cfg.dryRun) return notFound();

    const alert = ALERT_ID_RE.test(id) ? await this._get(id) : null;
    if (!alert || alert.client !== client) return error(404, "not_found", "no such alert");

    if (action === undefined) return json(200, { alert: publicAlert(alert) });
    if (action === "ack" || action === "cancel") return this._clientClose(alert, action === "ack" ? "acked" : "canceled");
    return this._simulate(request, alert, client);
  }

  // ------------------------------------------------------------ storage helpers

  _get(id) {
    return this.storage.get(`alert:${id}`);
  }

  async _save(alert) {
    alert.updated = iso(this.now());
    await this.storage.put(`alert:${alert.id}`, alert);
  }

  async _openIds() {
    return (await this.storage.get("open")) || [];
  }

  async _addOpen(id) {
    const ids = await this._openIds();
    if (!ids.includes(id)) {
      ids.push(id);
      await this.storage.put("open", ids);
    }
  }

  async _removeOpen(id) {
    const ids = await this._openIds();
    const i = ids.indexOf(id);
    if (i >= 0) {
      ids.splice(i, 1);
      await this.storage.put("open", ids);
    }
  }

  async _counter(key) {
    const cutoff = this.now() - DAY;
    return ((await this.storage.get(key)) || []).filter((t) => t > cutoff);
  }

  async _bump(key) {
    const list = await this._counter(key);
    list.push(this.now());
    await this.storage.put(key, list);
  }

  _retryAfter(list, cap) {
    if (cap <= 0 || list.length < cap) return Math.ceil(DAY / 1000);
    const pivot = list[list.length - cap];
    return Math.max(1, Math.ceil((pivot + DAY - this.now()) / 1000));
  }

  async _emit(alert, type, extra = {}) {
    const seq = ((await this.storage.get("seq")) || 0) + 1;
    await this.storage.put("seq", seq);
    const u = {
      seq,
      alert_id: alert.id,
      ref: alert.ref,
      type,
      status: alert.status,
      attempt: alert.call ? alert.call.attempts : 0,
      ...extra,
      at: iso(this.now()),
    };
    await this.storage.put(`upd:${alert.client}:${pad(seq)}`, u);
    return u;
  }

  _setNext(alert, ms) {
    alert._.next_ms = ms;
    if (alert.call) alert.call.next_call_at = ms === null ? null : iso(ms);
  }

  _cycleMax(alert) {
    return alert._.retry ? this.cfg.maxAttempts : 1;
  }

  _isLastAttempt(alert) {
    return alert._.cycle >= this._cycleMax(alert);
  }

  // ------------------------------------------------------------ client API

  async _create(request, url, client) {
    const parsed = await readJson(request);
    if (parsed.error) return parsed.error;
    const v = validateCreate(parsed.value);
    if (v.error) return error(400, "invalid", v.error);
    const input = v.value;
    const wantsCall = input.channels.includes("call");
    const wantsText = input.channels.includes("text");

    const existingId = await this.storage.get(`ref:${client}:${input.ref}`);
    if (existingId) {
      const existing = await this._get(existingId);
      if (existing) return json(200, { alert: publicAlert(existing), duplicate: true });
    }

    // Both channels go through Twilio (calls and SMS from the same number).
    if (!this.cfg.dryRun && !twilioConfigured(this.env)) return error(503, "not_configured", "twilio");

    const hits = [];
    if (wantsCall) {
      const calls = await this._counter(`cnt:calls:${client}`);
      if (calls.length >= this.cfg.clientDailyCalls) {
        hits.push({
          detail: `client daily call limit (${this.cfg.clientDailyCalls} per 24 h) reached`,
          after: this._retryAfter(calls, this.cfg.clientDailyCalls),
        });
      }
      const dials = await this._counter("cnt:dials");
      if (dials.length >= this.cfg.maxDials) {
        hits.push({
          detail: `daily dial limit (${this.cfg.maxDials} per 24 h) reached`,
          after: this._retryAfter(dials, this.cfg.maxDials),
        });
      }
    }
    if (wantsText) {
      const texts = await this._counter("cnt:texts");
      if (texts.length >= this.cfg.maxTexts) {
        hits.push({
          detail: `daily text limit (${this.cfg.maxTexts} per 24 h) reached`,
          after: this._retryAfter(texts, this.cfg.maxTexts),
        });
      }
    }
    if (hits.length) {
      const after = Math.max(...hits.map((h) => h.after));
      return error(
        429,
        "rate_limited",
        hits.map((h) => h.detail).join("; "),
        { retry_after: after },
        { "Retry-After": String(after) },
      );
    }

    const now = this.now();
    const alert = {
      id: `al_${randomHex(8)}`,
      client,
      ref: input.ref,
      title: input.title,
      message: input.message,
      channels: input.channels,
      status: wantsCall ? "queued" : "sending",
      created: iso(now),
      updated: iso(now),
      call: wantsCall
        ? {
            attempts: 0,
            max_attempts: input.retry ? this.cfg.maxAttempts : 1,
            snoozes: 0,
            sid: null,
            last_call_status: null,
            next_call_at: null,
            digit: null,
            via: null,
          }
        : null,
      text: null,
      _: {
        origin: url.origin,
        retry: input.retry,
        fallback_text: input.fallback_text,
        created_ms: now,
        cycle: 0,
        next_ms: null,
        queued_ms: null,
        answered_attempt: null,
        ended_attempt: null,
      },
    };
    await this.storage.put(`alert:${alert.id}`, alert);
    await this.storage.put(`ref:${client}:${alert.ref}`, alert.id);
    if (wantsCall) await this._bump(`cnt:calls:${client}`);
    if ((await this.storage.get("prune_at")) == null) await this.storage.put("prune_at", now + PRUNE_EVERY_MS);

    if (wantsText) await this._sendText(alert, "alert");
    if (wantsCall) {
      alert._.queued_ms = now;
      this._setNext(alert, now);
      await this._save(alert);
      await this._addOpen(alert.id);
    }
    // Also for text-only alerts: arms the alarm for prune_at.
    await this.runDue();
    return json(201, { alert: publicAlert(await this._get(alert.id)) });
  }

  async _clientClose(alert, finalStatus) {
    if (FINAL.has(alert.status)) return json(200, { alert: publicAlert(alert) });
    await this._close(alert, finalStatus, null, { via: finalStatus === "acked" ? "client" : null });
    await this.runDue();
    return json(200, { alert: publicAlert(await this._get(alert.id)) });
  }

  async _updates(url, client) {
    const afterRaw = url.searchParams.get("after");
    const limitRaw = url.searchParams.get("limit");
    let after = 0;
    let limit = 100;
    if (afterRaw !== null && afterRaw !== "") {
      if (!/^\d{1,12}$/.test(afterRaw)) return error(400, "invalid", "after must be a non-negative integer");
      after = Number(afterRaw);
    }
    if (limitRaw !== null && limitRaw !== "") {
      if (!/^\d{1,3}$/.test(limitRaw) || Number(limitRaw) < 1 || Number(limitRaw) > 200) {
        return error(400, "invalid", "limit must be 1-200");
      }
      limit = Number(limitRaw);
    }
    const prefix = `upd:${client}:`;
    const rows = await this.storage.list({ prefix, startAfter: prefix + pad(after), limit });
    const updates = [...rows.values()];
    const next = updates.length ? updates[updates.length - 1].seq : after;
    return json(200, { updates, next });
  }

  // ------------------------------------------------------------ state transitions

  /**
   * acked / done / canceled by the client, keypad, call-back or SMS reply. Ends a ringing call when needed.
   * via ("call" | "sms" | "client") says how an ack or done arrived; it is not set on cancels.
   */
  async _close(alert, finalStatus, digit, { endRinging = true, via = null } = {}) {
    const ringingSid = alert.status === "calling" ? alert.call && alert.call.sid : null;
    alert.status = finalStatus;
    if (alert.call) {
      this._setNext(alert, null);
      if (digit) alert.call.digit = digit;
      if (via) alert.call.via = via;
    }
    await this._save(alert);
    await this._removeOpen(alert.id);
    const extra = {};
    if (digit) extra.digit = digit;
    if (via) extra.via = via;
    const type = finalStatus === "acked" ? "alert.acked" : finalStatus === "done" ? "alert.done" : "alert.canceled";
    await this._emit(alert, type, extra);
    if (endRinging && ringingSid) await this._endCall(ringingSid);
  }

  /** Snooze by keypad (via "call", digit 2) or SMS reply (via "sms", no digit). */
  async _snooze(alert, { endRinging = false, via = "call" } = {}) {
    const ringingSid = alert.status === "calling" ? alert.call.sid : null;
    alert.call.snoozes += 1;
    if (via === "call") alert.call.digit = "2";
    alert.call.via = via;
    alert.status = "snoozed";
    alert._.cycle = 0;
    alert._.queued_ms = null;
    this._setNext(alert, this.now() + this.cfg.snoozeMinutes * MINUTE);
    await this._save(alert);
    await this._addOpen(alert.id);
    const extra = via === "call" ? { digit: "2" } : {};
    await this._emit(alert, "alert.snoozed", { ...extra, via, next_call_at: alert.call.next_call_at });
    if (endRinging && ringingSid) await this._endCall(ringingSid);
  }

  async _endCall(sid) {
    if (this.cfg.dryRun) return;
    if (!twilioConfigured(this.env)) return;
    await endCall(this._fetch, this.env, sid);
  }

  async _sendText(alert, kind) {
    let result;
    const texts = await this._counter("cnt:texts");
    if (texts.length >= this.cfg.maxTexts) {
      result = { ok: false, detail: `daily text limit (${this.cfg.maxTexts} per 24 h) reached` };
    } else if (!this.cfg.dryRun && !twilioConfigured(this.env)) {
      result = { ok: false, detail: "twilio not configured" };
    } else {
      await this._bump("cnt:texts");
      result = this.cfg.dryRun
        ? { ok: true, message_id: `SMdry${randomHex(14)}` }
        : await sendSms(this._fetch, this.env, smsBody(alert, kind, this.cfg.snoozeMinutes));
    }
    alert.text = result.ok
      ? { status: "sent", message_id: result.message_id }
      : { status: "failed", message_id: null, detail: result.detail };
    if (!alert.call) alert.status = result.ok ? "sent" : "text_failed";
    await this._save(alert);
    await this._emit(alert, result.ok ? "text.sent" : "text.failed", result.ok ? { message_id: result.message_id } : { detail: result.detail });
  }

  /** The attempt was not acknowledged: retry within the cycle, or end unanswered. */
  async _notAcked(alert) {
    if (!this._isLastAttempt(alert)) {
      alert.status = "waiting_retry";
      this._setNext(alert, this.now() + this.cfg.retryMinutes * MINUTE);
      await this._save(alert);
      await this._emit(alert, "alert.retry_scheduled", { next_call_at: alert.call.next_call_at });
    } else {
      await this._unanswered(alert);
    }
  }

  async _unanswered(alert) {
    alert.status = "unanswered";
    this._setNext(alert, null);
    await this._save(alert);
    await this._removeOpen(alert.id);
    await this._emit(alert, "alert.unanswered");
    if (alert._.fallback_text) await this._sendText(alert, "fallback");
  }

  async _dial(alert) {
    const now = this.now();
    alert.call.attempts += 1;
    alert._.cycle += 1;
    alert._.queued_ms = null;
    alert.call.sid = null;
    this._setNext(alert, null);
    const attempt = alert.call.attempts;

    const dials = await this._counter("cnt:dials");
    if (dials.length >= this.cfg.maxDials) {
      alert.call.last_call_status = "failed";
      alert._.ended_attempt = attempt;
      await this._save(alert);
      await this._emit(alert, "call.ended", {
        call_status: "failed",
        detail: `daily dial limit (${this.cfg.maxDials} per 24 h) reached`,
      });
      await this._unanswered(alert);
      return;
    }
    if (!this.cfg.dryRun && !twilioConfigured(this.env)) {
      alert.status = "failed";
      await this._save(alert);
      await this._removeOpen(alert.id);
      await this._emit(alert, "alert.failed", { detail: "twilio not configured" });
      return;
    }

    await this._bump("cnt:dials");
    const origin = alert._.origin;
    const q = `alert=${encodeURIComponent(alert.id)}&attempt=${attempt}`;
    const result = this.cfg.dryRun
      ? { ok: true, sid: `CAdry${randomHex(14)}` }
      : await createCall(this._fetch, this.env, {
          url: `${origin}/twilio/voice?${q}&pass=0`,
          statusCallback: `${origin}/twilio/status?${q}`,
          timeout: this.cfg.ringSeconds,
        });

    if (result.ok) {
      alert.status = "calling";
      alert.call.sid = result.sid;
      await this._save(alert);
      await this.storage.put("line", { alert_id: alert.id, attempt, sid: result.sid, started_ms: now, seen_ms: now });
      await this._emit(alert, "call.dialing");
    } else if (result.permanent) {
      alert.status = "failed";
      await this._save(alert);
      await this._removeOpen(alert.id);
      await this._emit(alert, "alert.failed", { detail: result.detail });
    } else {
      alert.call.last_call_status = "failed";
      alert._.ended_attempt = attempt;
      await this._save(alert);
      await this._emit(alert, "call.ended", { call_status: "failed", detail: result.detail });
      await this._notAcked(alert);
    }
  }

  /** A call attempt ended (status callback, simulate, or stuck timeout). Returns false when ignored. */
  async _callEnded(alertId, attempt, callStatus, sid, detail) {
    const line = await this.storage.get("line");
    if (line && line.alert_id === alertId && line.attempt === attempt) await this.storage.delete("line");
    const alert = alertId ? await this._get(alertId) : null;
    if (!alert || !alert.call || attempt !== alert.call.attempts || attempt < 1) return false;
    if (alert._.ended_attempt === attempt) return false;
    alert._.ended_attempt = attempt;
    alert.call.last_call_status = callStatus;
    if (sid && !alert.call.sid) alert.call.sid = sid;
    await this._save(alert);
    await this._emit(alert, "call.ended", detail ? { call_status: callStatus, detail } : { call_status: callStatus });
    if (alert.status === "calling") await this._notAcked(alert);
    return true;
  }

  async _touchLine(alertId, attempt) {
    const line = await this.storage.get("line");
    if (line && line.alert_id === alertId && line.attempt === attempt) {
      line.seen_ms = this.now();
      await this.storage.put("line", line);
    }
  }

  // ------------------------------------------------------------ timers

  /** Runs everything that is due, then sets the alarm to the next due time. */
  async runDue() {
    let now = this.now();

    const line = await this.storage.get("line");
    if (line && now >= line.seen_ms + STUCK_MS) {
      await this._callEnded(line.alert_id, line.attempt, "unknown", null, "no status callback within 10 minutes");
      await this.storage.delete("line");
    }

    for (const id of await this._openIds()) {
      const a = await this._get(id);
      if (!a || FINAL.has(a.status)) {
        await this._removeOpen(id);
        continue;
      }
      if ((a.status === "snoozed" || a.status === "waiting_retry") && a._.next_ms !== null && a._.next_ms <= now) {
        a.status = "queued";
        a._.queued_ms = a._.next_ms;
        await this._save(a);
      }
    }

    // One call at a time: dial the oldest queued alert whenever the line is free.
    while (!(await this.storage.get("line"))) {
      const queued = [];
      for (const id of await this._openIds()) {
        const a = await this._get(id);
        if (a && a.status === "queued") queued.push(a);
      }
      if (!queued.length) break;
      queued.sort((x, y) => x._.queued_ms - y._.queued_ms || x._.created_ms - y._.created_ms);
      await this._dial(queued[0]);
    }

    await this._prune();
    await this._schedule();
  }

  async _schedule() {
    const now = this.now();
    const times = [];
    const line = await this.storage.get("line");
    if (line) times.push(line.seen_ms + STUCK_MS);
    for (const id of await this._openIds()) {
      const a = await this._get(id);
      if (!a) continue;
      if ((a.status === "snoozed" || a.status === "waiting_retry") && a._.next_ms !== null) times.push(a._.next_ms);
      if (a.status === "queued") times.push(line ? now + QUEUE_RECHECK_MS : now);
    }
    const pruneAt = await this.storage.get("prune_at");
    if (pruneAt != null) times.push(pruneAt);
    if (!times.length) {
      await this.storage.deleteAlarm();
      return;
    }
    const due = Math.min(...times);
    // Alarms run on real time; the dry-run clock is real time + offset.
    await this.storage.setAlarm(Math.max(due - this._offset, this._clock()));
  }

  async _prune() {
    const now = this.now();
    const pruneAt = await this.storage.get("prune_at");
    if (pruneAt == null || now < pruneAt) return;
    const cutoff = now - KEEP_MS;
    let remaining = 0;
    const line = await this.storage.get("line");
    const alerts = await this.storage.list({ prefix: "alert:" });
    for (const [key, a] of alerts) {
      if (a._.created_ms < cutoff) {
        await this.storage.delete(key);
        const refKey = `ref:${a.client}:${a.ref}`;
        if ((await this.storage.get(refKey)) === a.id) await this.storage.delete(refKey);
        await this._removeOpen(a.id);
        if (line && line.alert_id === a.id) await this.storage.delete("line");
      } else {
        remaining++;
      }
    }
    const updates = await this.storage.list({ prefix: "upd:" });
    for (const [key, u] of updates) {
      if (Date.parse(u.at) < cutoff) await this.storage.delete(key);
      else remaining++;
    }
    if (remaining) await this.storage.put("prune_at", now + PRUNE_EVERY_MS);
    else await this.storage.delete("prune_at");
  }

  // ------------------------------------------------------------ voice logic (webhooks + simulate)

  /** attempt 0 = Cameron calling back (/twilio/incoming); otherwise must be the current outbound attempt. */
  _playable(alert, attempt) {
    if (!alert || !alert.call) return false;
    if (attempt === 0) return !CLOSED.has(alert.status);
    return attempt === alert.call.attempts && alert.status === "calling";
  }

  _menu(alert, attempt, pass, origin) {
    const q = `alert=${encodeURIComponent(alert.id)}&attempt=${attempt}`;
    let goodbye = "No key pressed. Goodbye.";
    if (attempt > 0 && !this._isLastAttempt(alert)) {
      goodbye = `No key pressed. I will call back in ${plural(this.cfg.retryMinutes, "minute")}.`;
    } else if (attempt > 0 && alert._.fallback_text && (this.cfg.dryRun || twilioConfigured(this.env))) {
      // Only promise a text that _unanswered will actually try to send.
      goodbye = "No key pressed. I will text you instead.";
    }
    return menuTwiml({
      voice: this.cfg.voice,
      speech: alertSpeech({
        intro: this.cfg.callIntro,
        client: alert.client,
        title: alert.title,
        message: alert.message,
        snoozeMinutes: this.cfg.snoozeMinutes,
      }),
      gatherUrl: `${origin}/twilio/gather?${q}`,
      redirectUrl: `${origin}/twilio/voice?${q}&pass=1`,
      pass,
      goodbye,
    });
  }

  _handled() {
    return sayHangupTwiml(this.cfg.voice, "This alert was already handled. Goodbye.");
  }

  async _voice(alertId, attempt, pass, origin) {
    const alert = alertId ? await this._get(alertId) : null;
    if (!this._playable(alert, attempt)) return this._handled();
    if (attempt > 0) {
      await this._touchLine(alert.id, attempt);
      if (pass === 0 && alert._.answered_attempt !== attempt) {
        alert._.answered_attempt = attempt;
        await this._save(alert);
        await this._emit(alert, "call.answered");
      }
    }
    return this._menu(alert, attempt, pass, origin);
  }

  async _gather(alertId, attempt, digits, origin) {
    const alert = alertId ? await this._get(alertId) : null;
    if (!this._playable(alert, attempt)) return this._handled();
    if (attempt > 0) await this._touchLine(alert.id, attempt);
    const d = String(digits || "").charAt(0);
    const viaCallback = attempt === 0;
    if (d === "1") {
      await this._close(alert, "acked", "1", { endRinging: viaCallback, via: "call" });
      return sayHangupTwiml(this.cfg.voice, "Acknowledged. Goodbye.");
    }
    if (d === "9") {
      await this._close(alert, "done", "9", { endRinging: viaCallback, via: "call" });
      return sayHangupTwiml(this.cfg.voice, "Nice. Marked as done. Goodbye.");
    }
    if (d === "2") {
      if (this._canSnooze(alert)) {
        await this._snooze(alert, { endRinging: viaCallback, via: "call" });
        return sayHangupTwiml(
          this.cfg.voice,
          `Snoozed. I will call again in ${plural(this.cfg.snoozeMinutes, "minute")}.`,
        );
      }
      await this._close(alert, "acked", "2", { endRinging: viaCallback, via: "call" });
      return sayHangupTwiml(this.cfg.voice, "Acknowledged. Goodbye.");
    }
    if (d === "") return this._menu(alert, attempt, 1, origin);
    return this._menu(alert, attempt, 0, origin);
  }

  _canSnooze(alert) {
    return alert.call.snoozes < this.cfg.maxSnoozes && !FINAL.has(alert.status);
  }

  async _newestReplayable() {
    const cutoff = this.now() - REPLAY_WINDOW_MS;
    let best = null;
    const alerts = await this.storage.list({ prefix: "alert:" });
    for (const a of alerts.values()) {
      if (!a.call || CLOSED.has(a.status)) continue;
      if (a._.created_ms < cutoff) continue;
      if (!best || a._.created_ms > best._.created_ms) best = a;
    }
    return best;
  }

  /** The alert an SMS reply acts on: the newest call alert that is not final, or unanswered within the last 24 h. */
  async _newestAnswerable() {
    const cutoff = this.now() - REPLAY_WINDOW_MS;
    let best = null;
    const alerts = await this.storage.list({ prefix: "alert:" });
    for (const a of alerts.values()) {
      if (!a.call) continue;
      if (FINAL.has(a.status) && !(a.status === "unanswered" && a._.created_ms >= cutoff)) continue;
      if (!best || a._.created_ms > best._.created_ms) best = a;
    }
    return best;
  }

  /**
   * Cameron texted the number (the sender is already checked). 1/ok/ack/got it/y/yes acknowledges, 2/snooze snoozes.
   * The reply counts toward MAX_TEXTS_PER_DAY; over the cap the action still happens but nothing is texted back.
   * Returns {twiml, alert} where alert is the record acted on, or null.
   */
  async _inboundSms(body) {
    const alert = await this._newestAnswerable();
    let reply;
    let acted = null;
    if (!alert) {
      reply = "No open alerts.";
    } else {
      const intent = replyIntent(body);
      const title = smsClean(alert.title);
      if (intent === "done") {
        await this._close(alert, "done", null, { endRinging: true, via: "sms" });
        reply = `Marked done: ${title}`;
        acted = alert;
      } else if (intent === "snooze" && this._canSnooze(alert)) {
        await this._snooze(alert, { endRinging: true, via: "sms" });
        reply = `Snoozed ${this.cfg.snoozeMinutes} min: ${title}`;
        acted = alert;
      } else if (intent !== "other") {
        // 2 past MAX_SNOOZES, or on an unanswered alert, counts as 1.
        await this._close(alert, "acked", null, { endRinging: true, via: "sms" });
        reply = `Got it: ${title}`;
        acted = alert;
      } else {
        reply = `Reply 1 to acknowledge, 2 to snooze ${this.cfg.snoozeMinutes} min, or 9 if done.`;
      }
    }
    if (acted) await this.runDue();
    const texts = await this._counter("cnt:texts");
    if (texts.length >= this.cfg.maxTexts) return { twiml: emptyTwiml(), alert: acted };
    await this._bump("cnt:texts");
    return { twiml: messageTwiml(reply), alert: acted };
  }

  async _twilio(request, url) {
    const body = await readBody(request);
    if (body.tooLarge) return error(400, "invalid", `body exceeds ${MAX_BODY_BYTES} bytes`);
    const params = new URLSearchParams(body.text);
    const ok = await validateTwilioSignature(
      this.env.TWILIO_AUTH_TOKEN,
      request.url,
      params,
      request.headers.get("X-Twilio-Signature"),
    );
    if (!ok) return new Response("forbidden", { status: 403, headers: { "Content-Type": "text/plain" } });

    const path = url.pathname;
    const origin = url.origin;
    const alertId = url.searchParams.get("alert");
    const attemptRaw = url.searchParams.get("attempt");
    const attempt = /^\d{1,6}$/.test(attemptRaw || "") ? Number(attemptRaw) : -1;

    if (path === "/twilio/sms") {
      const owner = this.env.OWNER_NUMBER;
      if (!owner || params.get("From") !== owner) return xmlResponse(emptyTwiml());
      return xmlResponse((await this._inboundSms(params.get("Body") || "")).twiml);
    }
    if (path === "/twilio/incoming") {
      const owner = this.env.OWNER_NUMBER;
      if (!owner || params.get("From") !== owner) return xmlResponse(rejectTwiml());
      const alert = await this._newestReplayable();
      if (!alert) return xmlResponse(sayHangupTwiml(this.cfg.voice, "Nothing urgent right now. Goodbye."));
      return xmlResponse(this._menu(alert, 0, 0, origin));
    }
    if (path === "/twilio/voice") {
      const pass = url.searchParams.get("pass") === "1" ? 1 : 0;
      return xmlResponse(await this._voice(alertId, attempt, pass, origin));
    }
    if (path === "/twilio/gather") {
      const twiml = await this._gather(alertId, attempt, params.get("Digits"), origin);
      await this.runDue();
      return xmlResponse(twiml);
    }
    // /twilio/status
    const callStatus = params.get("CallStatus") || "";
    if (TERMINAL_CALL_STATUS.has(callStatus) && attempt >= 1) {
      await this._callEnded(alertId, attempt, callStatus, params.get("CallSid"), null);
      await this.runDue();
    }
    return xmlResponse(emptyTwiml());
  }

  // ------------------------------------------------------------ dry-run simulate

  async _simulate(request, alert, client) {
    const parsed = await readJson(request);
    if (parsed.error) return parsed.error;
    const b = parsed.value;
    const event = b.event;
    const origin = alert._.origin;
    if (!["answer", "digits", "status", "advance", "sms"].includes(event)) {
      return error(400, "invalid", 'event must be "answer", "digits", "status", "advance" or "sms"');
    }
    if (event === "sms") {
      // As if OWNER_NUMBER texted the number: it acts on the alert the reply picks, not necessarily this one.
      if (typeof b.body !== "string" || cpLength(b.body) > MAX_SMS_BODY) {
        return error(400, "invalid", `body must be a string of at most ${MAX_SMS_BODY} characters`);
      }
      const r = await this._inboundSms(b.body);
      const acted = r.alert ? await this._get(r.alert.id) : null;
      return json(200, { twiml: r.twiml, alert: acted && acted.client === client ? publicAlert(acted) : null });
    }
    if (event !== "advance" && !alert.call) return error(400, "invalid", "alert has no call");
    const attempt = alert.call ? Math.max(1, alert.call.attempts) : 1;

    if (event === "answer") {
      const twiml = await this._voice(alert.id, attempt, 0, origin);
      return json(200, { twiml });
    }
    if (event === "digits") {
      if (typeof b.digits !== "string" || !/^[0-9*#]$/.test(b.digits)) {
        return error(400, "invalid", "digits must be one of 0-9, * or #");
      }
      const twiml = await this._gather(alert.id, attempt, b.digits, origin);
      await this.runDue();
      return json(200, { twiml, alert: publicAlert(await this._get(alert.id)) });
    }
    if (event === "status") {
      if (typeof b.call_status !== "string" || !KNOWN_CALL_STATUS.has(b.call_status)) {
        return error(400, "invalid", "call_status must be a Twilio CallStatus");
      }
      if (TERMINAL_CALL_STATUS.has(b.call_status)) {
        await this._callEnded(alert.id, attempt, b.call_status, alert.call.sid, null);
        await this.runDue();
      }
      return json(200, { alert: publicAlert(await this._get(alert.id)) });
    }
    // advance
    const s = b.seconds;
    if (!Number.isInteger(s) || s < 1 || s > MAX_ADVANCE_SECONDS) {
      return error(400, "invalid", `seconds must be an integer 1-${MAX_ADVANCE_SECONDS}`);
    }
    this._offset += s * 1000;
    await this.storage.put("dry:offset", this._offset);
    await this.runDue();
    return json(200, { alert: publicAlert(await this._get(alert.id)) });
  }
}
