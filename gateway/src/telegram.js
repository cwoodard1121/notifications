// Telegram Bot API, send-only.
import { redact } from "./util.js";

const TIMEOUT_MS = 10000;

export function telegramConfigured(env) {
  return Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID);
}

function label(client) {
  if (client === "bus") return "";
  return client.charAt(0).toUpperCase() + client.slice(1) + ": ";
}

/** kind "alert": the text channel. kind "fallback": missed-call text. */
export function telegramText(alert, kind) {
  const head = kind === "fallback" ? "📵 Missed urgent call — " : "🚨 ";
  return `${head}${label(alert.client)}${alert.title}\n\n${alert.message}`;
}

/** Returns {ok:true, message_id} | {ok:false, detail}. Never throws. */
export async function sendTelegram(fetchFn, env, text) {
  let res;
  try {
    res = await fetchFn(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text, disable_web_page_preview: true }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (e) {
    return { ok: false, detail: redact(`Telegram request failed: ${e && e.name ? e.name : "error"}`, env) };
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.ok && data && data.ok && data.result && data.result.message_id !== undefined) {
    return { ok: true, message_id: String(data.result.message_id) };
  }
  const desc = data && data.description ? data.description : `HTTP ${res.status}`;
  return { ok: false, detail: redact(`Telegram ${res.status}: ${desc}`, env) };
}
