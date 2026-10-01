// SMS text bodies (GSM-7 friendly, 600 characters at most) and inbound reply parsing.
import { cpLength, cpSlice } from "./util.js";

export const SMS_MAX = 600;

// GSM 03.38 basic set and extension table. Anything else forces UCS-2, which roughly halves a segment.
const GSM = new Set(
  "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€",
);
const ACK_WORDS = new Set(["1", "ok", "ack", "got it", "y", "yes"]);
const SNOOZE_WORDS = new Set(["2", "snooze"]);

export function smsPrefix(client) {
  if (client === "bus") return "[Agent Bus]";
  if (client === "planner") return "[Planner]";
  return `[${client}]`;
}

/** Folds a non-GSM letter to its GSM base when that is all it takes (á -> a); otherwise keeps it. */
function fold(ch) {
  if (GSM.has(ch)) return ch;
  const base = ch.normalize("NFKD").replace(/\p{M}/gu, "");
  return base && [...base].every((c) => GSM.has(c)) ? base : ch;
}

/**
 * One line of GSM-friendly text: smart quotes, dashes, ellipsis and odd spaces become ASCII, emoji and other
 * pictographs (with their joiners, variation selectors, skin tones and flags) are removed, accented letters
 * outside GSM are folded, control characters become spaces and whitespace collapses.
 */
export function smsClean(text) {
  let s = String(text ?? "").normalize("NFC");
  s = s.replace(/[\u2018\u2019\u201A\u201B\u2032\u00B4`]/g, "'");
  s = s.replace(/[\u201C\u201D\u201E\u201F\u2033\u00AB\u00BB]/g, '"');
  s = s.replace(/[\u2010-\u2015\u2212\u2043\u2022\u00B7]/g, "-");
  s = s.replace(/\u2026/g, "...");
  s = s.replace(/\p{Extended_Pictographic}/gu, "");
  s = s.replace(/[\u200B-\u200D\u2060\uFE00-\uFE0F\u20E3\uFEFF]|[\u{1F3FB}-\u{1F3FF}\u{1F1E6}-\u{1F1FF}\u{E0020}-\u{E007F}]/gu, "");
  s = s.replace(/[\u0000-\u001F\u007F-\u009F\u00A0\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]/g, " ");
  s = s.replace(/\s+/g, " ").trim();
  return Array.from(s, fold).join("");
}

/**
 * kind "alert": the text channel (URGENT + reply hint when the alert also calls). kind "fallback": missed call.
 * The message is cut to keep the whole body within SMS_MAX; the header and reply hint never are.
 */
export function smsBody(alert, kind, snoozeMinutes) {
  const prefix = smsPrefix(alert.client);
  const title = smsClean(alert.title);
  let head;
  let hint = "";
  if (kind === "fallback") {
    head = `${prefix} Missed urgent call: ${title}`;
    hint = "Reply 1 = got it.";
  } else if (alert.call) {
    head = `${prefix} URGENT: ${title}`;
    hint = `Reply 1 = got it, 2 = snooze ${snoozeMinutes} min.`;
  } else {
    head = `${prefix} ${title}`;
  }
  let message = smsClean(alert.message);
  const budget = SMS_MAX - cpLength(head) - (hint ? cpLength(hint) + 1 : 0) - 1;
  if (cpLength(message) > budget) message = cpSlice(message, Math.max(0, budget - 3)).trimEnd() + "...";
  return [head, message, hint].filter(Boolean).join("\n");
}

/** "ack", "snooze" or "other" for an inbound reply (case, surrounding space and trailing . or ! ignored). */
export function replyIntent(body) {
  const word = String(body ?? "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[.!]+$/, "");
  if (ACK_WORDS.has(word)) return "ack";
  if (SNOOZE_WORDS.has(word)) return "snooze";
  return "other";
}
