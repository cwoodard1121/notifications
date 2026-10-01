// Small shared helpers. No Cloudflare-only APIs here: everything runs in plain Node too.

export const enc = new TextEncoder();

/** ISO 8601 UTC with second precision, e.g. "2026-10-01T18:04:05Z". */
export function iso(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Length in Unicode code points (matches Python's len()). */
export function cpLength(s) {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

/** First `max` code points of `s`. */
export function cpSlice(s, max) {
  let out = "";
  let n = 0;
  for (const ch of s) {
    if (n++ >= max) break;
    out += ch;
  }
  return out;
}

export function randomHex(bytes) {
  const b = new Uint8Array(bytes);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

export function bytesToBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

/** Constant-time comparison of two equal-length byte arrays (length is not secret). */
export function equalBytes(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

export async function sha256(str) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(str)));
}

export function json(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...headers },
  });
}

export function error(status, code, detail, extra = {}, headers = {}) {
  const body = { error: code };
  if (detail !== undefined) body.detail = detail;
  return json(status, { ...body, ...extra }, headers);
}

export function xmlResponse(xml) {
  return new Response(xml, { status: 200, headers: { "Content-Type": "text/xml; charset=utf-8" } });
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** Remove every secret value from a string that may be returned to a client. */
export function redact(text, env) {
  let out = String(text ?? "");
  for (const [name, value] of Object.entries(env || {})) {
    if (typeof value !== "string" || value.length < 6) continue;
    if (/^(TWILIO_|TELEGRAM_|TOKEN_)/.test(name) || name === "OWNER_NUMBER") {
      out = out.split(value).join("[redacted]");
    }
  }
  return out;
}
