// Text cleanup before TTS, and XML escaping for TwiML.
import { cpLength, cpSlice } from "./util.js";

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

export const DEFAULT_CALL_INTRO = "Yo, you have to lock in on this now.";

const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
  "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"];

/** "ten minutes", "one minute", "45 minutes" (numbers up to twenty are spelled out for the voice). */
export function spokenMinutes(n) {
  const num = Number.isInteger(n) && n >= 0 && n < WORDS.length ? WORDS[n] : String(n);
  return `${num} minute${n === 1 ? "" : "s"}`;
}

/** The keypad menu, in plain words. */
export function menuSpeech(snoozeMinutes) {
  return [
    "Press 1 if you're on it.",
    `Press 2 to snooze ${spokenMinutes(snoozeMinutes)}.`,
    "Press 3 to hear it again.",
    "Press 9 if it's already done.",
  ].join(" ");
}

/**
 * The full spoken prompt (plain text; escape when rendering): the intro (CALL_INTRO, "" = none), the alert's
 * title (skipped for the planner, whose message is the full spoken sentence), the message, then the keypad menu.
 */
export function alertSpeech({ intro = DEFAULT_CALL_INTRO, client, title, message, snoozeMinutes }) {
  const parts = [
    sentence(cleanSpeech(intro, 200)),
    client === "planner" ? "" : sentence(cleanSpeech(title)),
    sentence(cleanSpeech(message, MESSAGE_SPEECH_MAX)),
    menuSpeech(snoozeMinutes),
  ];
  return parts.filter(Boolean).join(" ");
}
