// Shared test harness: fake DO storage, fake clock, recording fake fetch (Twilio calls and SMS), signed Twilio webhooks.
// (node --test also loads this file; it defines no tests.)
import { createHmac } from "node:crypto";
import { Gateway } from "../src/core.js";

export const ORIGIN = "https://agent-bus-phone.example.workers.dev";
export const START = Date.parse("2026-10-01T18:00:00Z");
export const AUTH = "test-twilio-auth-token-0123456789";
export const OWNER = "+15195550199";
export const FROM = "+15195550100";
export const TOKENS = {
  bus: "gw_bus_" + "a".repeat(43),
  planner: "gw_planner_" + "b".repeat(43),
};
export const MIN = 60 * 1000;

export class FakeStorage {
  constructor() {
    this.map = new Map();
    this.alarm = null;
  }
  async get(key) {
    const v = this.map.get(key);
    return v === undefined ? undefined : structuredClone(v);
  }
  async put(key, value) {
    this.map.set(key, structuredClone(value));
  }
  async delete(key) {
    return this.map.delete(key);
  }
  async list({ prefix = "", start, startAfter, end, limit, reverse } = {}) {
    let keys = [...this.map.keys()].filter((k) => k.startsWith(prefix)).sort();
    if (start !== undefined) keys = keys.filter((k) => k >= start);
    if (startAfter !== undefined) keys = keys.filter((k) => k > startAfter);
    if (end !== undefined) keys = keys.filter((k) => k < end);
    if (reverse) keys.reverse();
    if (limit !== undefined) keys = keys.slice(0, limit);
    return new Map(keys.map((k) => [k, structuredClone(this.map.get(k))]));
  }
  async getAlarm() {
    return this.alarm;
  }
  async setAlarm(t) {
    this.alarm = typeof t === "number" ? t : t.getTime();
  }
  async deleteAlarm() {
    this.alarm = null;
  }
  keys(prefix = "") {
    return [...this.map.keys()].filter((k) => k.startsWith(prefix)).sort();
  }
}

function jsonRes(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function makeFetch() {
  const calls = [];
  let sidN = 0;
  let msgN = 800;
  const handlers = {
    twilioCreate: () => jsonRes(201, { sid: `CA${String(++sidN).padStart(32, "0")}`, status: "queued" }),
    twilioUpdate: () => jsonRes(200, { status: "completed" }),
    twilioSms: () => jsonRes(201, { sid: `SM${String(++msgN).padStart(32, "0")}`, status: "queued" }),
  };
  const fn = async (url, init = {}) => {
    url = String(url);
    const rec = { url, method: init.method, headers: init.headers || {}, body: init.body };
    calls.push(rec);
    if (url.startsWith("https://api.twilio.com/")) {
      rec.form = new URLSearchParams(init.body || "");
      if (/\/Calls\.json$/.test(url)) {
        rec.kind = "twilio.create";
        return handlers.twilioCreate(rec);
      }
      if (/\/Messages\.json$/.test(url)) {
        rec.kind = "twilio.sms";
        return handlers.twilioSms(rec);
      }
      rec.kind = "twilio.update";
      return handlers.twilioUpdate(rec);
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  fn.calls = calls;
  fn.handlers = handlers;
  fn.of = (kind) => calls.filter((c) => c.kind === kind);
  return fn;
}

export function baseEnv(over = {}) {
  const env = {
    DRY_RUN: "0",
    TTS_VOICE: "Polly.Matthew-Neural",
    RING_SECONDS: "25",
    RETRY_MINUTES: "3",
    SNOOZE_MINUTES: "10",
    MAX_ATTEMPTS: "2",
    MAX_SNOOZES: "3",
    CLIENT_DAILY_CALLS: "10",
    MAX_DIALS_PER_DAY: "30",
    MAX_TEXTS_PER_DAY: "100",
    TWILIO_ACCOUNT_SID: "AC" + "1".repeat(32),
    TWILIO_AUTH_TOKEN: AUTH,
    TWILIO_FROM: FROM,
    OWNER_NUMBER: OWNER,
    TOKEN_BUS: TOKENS.bus,
    TOKEN_PLANNER: TOKENS.planner,
    ...over,
  };
  for (const k of Object.keys(env)) if (env[k] === undefined) delete env[k];
  return env;
}

/** Twilio's algorithm, computed independently with node:crypto. */
export function sign(authToken, url, params) {
  let data = url;
  for (const k of Object.keys(params).sort()) data += k + params[k];
  return createHmac("sha1", authToken).update(data).digest("base64");
}

let refN = 0;

export function setup({ env: over = {}, start = START } = {}) {
  const storage = new FakeStorage();
  const clock = { t: start };
  const fetch = makeFetch();
  const env = baseEnv(over);
  const errors = [];
  const gw = new Gateway({ storage, env, now: () => clock.t, fetch, log: { error: (...a) => errors.push(a) } });

  const h = {
    gw,
    storage,
    clock,
    fetch,
    env,
    errors,

    async api(method, path, { token = TOKENS.bus, body, raw, headers = {} } = {}) {
      const init = { method, headers: { ...headers } };
      if (token) init.headers.Authorization = `Bearer ${token}`;
      if (raw !== undefined) init.body = raw;
      else if (body !== undefined) {
        init.body = JSON.stringify(body);
        init.headers["Content-Type"] = "application/json";
      }
      const res = await gw.handle(new Request(ORIGIN + path, init));
      const text = await res.text();
      let json = null;
      try {
        json = JSON.parse(text);
      } catch {
        json = null;
      }
      return { status: res.status, json, text, headers: res.headers };
    },

    create(body = {}, opts = {}) {
      return h.api("POST", "/v1/alerts", {
        body: {
          ref: `ref-${++refN}`,
          title: "Pecunia prod is down",
          message: "Health check failing for 6 minutes.",
          channels: ["call"],
          ...body,
        },
        ...opts,
      });
    },

    async get(id, token = TOKENS.bus) {
      return (await h.api("GET", `/v1/alerts/${id}`, { token })).json?.alert;
    },

    async updates({ token = TOKENS.bus, after = 0, limit } = {}) {
      const q = `?after=${after}` + (limit ? `&limit=${limit}` : "");
      return (await h.api("GET", `/v1/updates${q}`, { token })).json;
    },

    async types(token = TOKENS.bus) {
      return (await h.updates({ token, limit: 200 })).updates.map((u) => u.type);
    },

    /** POST a signed Twilio webhook. `pathOrUrl` may be a full URL (e.g. the Url sent to Twilio). */
    async twilio(pathOrUrl, params = {}, { signature, authToken = AUTH } = {}) {
      const url = pathOrUrl.startsWith("http") ? pathOrUrl : ORIGIN + pathOrUrl;
      const sig = signature !== undefined ? signature : sign(authToken, url, params);
      const headers = { "Content-Type": "application/x-www-form-urlencoded" };
      if (sig !== null) headers["X-Twilio-Signature"] = sig;
      const res = await gw.handle(new Request(url, { method: "POST", headers, body: new URLSearchParams(params).toString() }));
      return { status: res.status, text: await res.text(), headers: res.headers };
    },

    answer(id, attempt, pass = 0) {
      return h.twilio(`/twilio/voice?alert=${id}&attempt=${attempt}&pass=${pass}`, {
        CallSid: "CAx",
        CallStatus: "in-progress",
        From: FROM,
        To: OWNER,
      });
    },

    press(id, attempt, digits) {
      return h.twilio(`/twilio/gather?alert=${id}&attempt=${attempt}`, { CallSid: "CAx", Digits: digits });
    },

    status(id, attempt, callStatus) {
      return h.twilio(`/twilio/status?alert=${id}&attempt=${attempt}`, { CallSid: "CAx", CallStatus: callStatus });
    },

    /** Cameron (or `from`) texting the Twilio number. */
    sms(body, from = OWNER) {
      return h.twilio("/twilio/sms", { MessageSid: "SMin", AccountSid: "AC1", From: from, To: FROM, Body: body, NumMedia: "0" });
    },

    /** Bodies of the SMS sent through Twilio so far. */
    texts() {
      return fetch.of("twilio.sms").map((c) => c.form.get("Body"));
    },

    /** Moves the clock forward, firing the alarm at each time it is due (like the DO runtime). */
    async advance(ms) {
      const target = clock.t + ms;
      for (let i = 0; i < 1000; i++) {
        if (storage.alarm === null || storage.alarm > target) break;
        clock.t = Math.max(clock.t, storage.alarm);
        storage.alarm = null;
        await gw.alarm();
      }
      clock.t = target;
    },
  };
  return h;
}
