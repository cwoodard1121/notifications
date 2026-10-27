// Twilio REST (calls, SMS) and webhook signature validation (WebCrypto only).
import { bytesToBase64, enc, equalBytes, redact } from "./util.js";

const API = "https://api.twilio.com/2010-04-01";
const TIMEOUT_MS = 10000;

export function twilioConfigured(env) {
  return Boolean(env.TWILIO_ACCOUNT_SID && env.TWILIO_AUTH_TOKEN && env.TWILIO_FROM && env.OWNER_NUMBER);
}

/** Base64(HMAC-SHA1(authToken, url + each param name+value in sorted name order)). */
export async function twilioSignature(authToken, url, params) {
  const pairs = [...params].sort((a, b) =>
    a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0,
  );
  let data = url;
  for (const [k, v] of pairs) data += k + v;
  const key = await crypto.subtle.importKey("raw", enc.encode(authToken), { name: "HMAC", hash: "SHA-1" }, false, [
    "sign",
  ]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(data));
  return bytesToBase64(new Uint8Array(sig));
}

/** `params` is an iterable of [name, value] pairs (e.g. URLSearchParams). */
export async function validateTwilioSignature(authToken, url, params, header) {
  if (!authToken || !header) return false;
  const expected = await twilioSignature(authToken, url, params);
  return equalBytes(enc.encode(expected), enc.encode(header));
}

function basicAuth(env) {
  return "Basic " + btoa(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`);
}

function accountUrl(env, path) {
  return `${API}/Accounts/${encodeURIComponent(env.TWILIO_ACCOUNT_SID)}/${path}`;
}

function callsUrl(env, suffix = "") {
  return accountUrl(env, `Calls${suffix}.json`);
}

/** POSTs a form to Twilio. Returns {ok:true, data} | {ok:false, status, detail}; status 0 = network or timeout. */
async function post(fetchFn, env, url, form) {
  let res;
  try {
    res = await fetchFn(url, {
      method: "POST",
      headers: { Authorization: basicAuth(env), "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(form).toString(),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (e) {
    return { ok: false, status: 0, detail: redact(`Twilio request failed: ${e && e.name ? e.name : "error"}`, env) };
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.ok) return { ok: true, data: data || {} };
  const msg = data && data.message ? `${data.message}${data.code ? ` (code ${data.code})` : ""}` : `HTTP ${res.status}`;
  return { ok: false, status: res.status, detail: redact(`Twilio ${res.status}: ${msg}`, env) };
}

/**
 * Places the call. Returns {ok:true, sid} | {ok:false, permanent, detail}.
 * permanent = Twilio refused (4xx other than 408/429); otherwise transient (5xx, network, timeout).
 */
export async function createCall(fetchFn, env, { url, statusCallback, timeout, to }) {
  const r = await post(fetchFn, env, callsUrl(env), {
    To: to || env.OWNER_NUMBER,
    From: env.TWILIO_FROM,
    Url: url,
    Method: "POST",
    StatusCallback: statusCallback,
    StatusCallbackMethod: "POST",
    Timeout: String(timeout),
  });
  if (r.ok) return { ok: true, sid: typeof r.data.sid === "string" ? r.data.sid : null };
  const transient = r.status === 0 || r.status >= 500 || r.status === 408 || r.status === 429;
  return { ok: false, permanent: !transient, detail: r.detail };
}

/**
 * Sends one SMS from TWILIO_FROM to `to` (an alert's own number), else OWNER_NUMBER.
 * Returns {ok:true, message_id} | {ok:false, detail}. Never throws.
 */
export async function sendSms(fetchFn, env, body, to) {
  const r = await post(fetchFn, env, accountUrl(env, "Messages.json"), {
    To: to || env.OWNER_NUMBER,
    From: env.TWILIO_FROM,
    Body: body,
  });
  if (r.ok) return { ok: true, message_id: typeof r.data.sid === "string" ? r.data.sid : null };
  return { ok: false, detail: r.detail };
}

/** Ends a ringing or in-progress call. Best-effort: never throws. */
export async function endCall(fetchFn, env, sid) {
  if (!sid) return false;
  try {
    const res = await fetchFn(callsUrl(env, `/${encodeURIComponent(sid)}`), {
      method: "POST",
      headers: { Authorization: basicAuth(env), "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ Status: "completed" }).toString(),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    return res.ok;
  } catch {
    return false;
  }
}
