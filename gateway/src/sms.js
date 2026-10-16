// SMS text bodies (GSM-7 friendly, 600 characters at most) and inbound reply parsing.
import { cpLength, cpSlice } from "./util.js";
import { briefText, cutWords } from "./speech.js";

export const SMS_MAX = 600;

// GSM 03.38 basic set and extension table. Anything else forces UCS-2, which roughly halves a segment.
const GSM = new Set(
  "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€",
);
const ACK_WORDS = new Set(["1", "ok", "ack", "got it", "y", "yes"]);
const SNOOZE_WORDS = new Set(["2", "snooze"]);
const DONE_WORDS = new Set(["9", "done"]);

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

export const SMS_BRIEF_MAX = 120;
export const REPLY_HINT = "reply 1 on it, 2 snooze, 9 done";

/**
 * One short line, no prefix: the alert (planner: its message; others: title + up to 10 words of the message), cut to
 * SMS_BRIEF_MAX characters at a word boundary, then the reply hint when the alert calls.
 * kind "alert" (the text channel / text-first SMS) and "reminder" (re-sent while it keeps calling) look the same;
 * kind "fallback" (missed call) starts with "missed call:". A text-only alert has no reply hint (replies act on calls).
 * (The hint uses commas, not "·", which is outside GSM-7 and would make every text UCS-2: 70-character segments.)
 */
export function smsBody(alert, kind, _snoozeMinutes) {
  const brief = cutWords(briefText(alert, smsClean), 0, SMS_BRIEF_MAX);
  if (kind === "fallback") return `missed call: ${brief} - reply 1 on it, 9 done`;
  if (!alert.call) return brief;
  return `${brief} - ${REPLY_HINT}`;
}

/** "ack", "snooze", "done" or "other" for an inbound reply (case, surrounding space and trailing . or ! ignored). */
export function replyIntent(body) {
  const word = String(body ?? "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[.!]+$/, "");
  if (ACK_WORDS.has(word)) return "ack";
  if (SNOOZE_WORDS.has(word)) return "snooze";
  if (DONE_WORDS.has(word)) return "done";
  return "other";
}
