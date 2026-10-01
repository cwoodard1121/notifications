// Text cleanup before TTS, and XML escaping for TwiML.
import { cpLength, cpSlice, plural } from "./util.js";

export const MESSAGE_SPEECH_MAX = 700;

/**
 * Cleans text for speech: markdown links keep their label, URLs become "link",
 * markdown symbols and control characters are removed, whitespace collapses,
 * and the result is cut to `max` code points. Not XML-escaped (see xmlEscape).
 */
export function cleanSpeech(text, max = 0) {
  let s = String(text ?? "");
  // [label](url) -> label
  s = s.replace(/\[([^\]\n]*)\]\(\s*[^)\s]*\s*\)/g, "$1");
  // bare URLs -> "link"
  s = s.replace(/\b(?:https?:\/\/|www\.)[^\s<>"'()\[\]{}]+/gi, "link");
  // blockquote markers at line starts
  s = s.replace(/^[ \t]*>+[ \t]?/gm, "");
  // control and invisible formatting characters (includes newlines and tabs)
  s = s.replace(/[\u0000-\u001F\u007F-\u009F\u200B-\u200F\u2028\u2029\u2060\uFEFF]/g, " ");
  // markdown symbols
  s = s.replace(/[*_`#~|\[\]]/g, " ");
  s = s.replace(/\s+/g, " ").trim();
  if (max > 0 && cpLength(s) > max) s = cpSlice(s, max).trimEnd();
  return s;
}

export function xmlEscape(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function sentence(s) {
  if (!s) return "";
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

export function opener(client) {
  if (client === "bus") return "your agents";
  if (client === "planner") return "your planner";
  return client;
}

/** The full spoken prompt (plain text; escape when rendering). */
export function alertSpeech({ client, title, message, snoozeMinutes }) {
  const parts = [
    `Urgent alert from ${opener(client)}.`,
    sentence(cleanSpeech(title)),
    sentence(cleanSpeech(message, MESSAGE_SPEECH_MAX)),
    "Press 1 to acknowledge.",
    `Press 2 to snooze for ${plural(snoozeMinutes, "minute")}.`,
    "Press 3 to hear it again.",
  ];
  return parts.filter(Boolean).join(" ");
}
