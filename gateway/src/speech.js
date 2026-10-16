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

/** The keypad menu: short and casual. 3 (repeat) works but is not announced. */
export const MENU_SPEECH = "1 if you're on it, 2 to snooze, 9 if it's done.";
export const CALL_MAX_WORDS = 20;
export const CALL_MAX_CHARS = 140;
export const TITLE_EXTRA_WORDS = 10;

/**
 * Cuts already-cleaned text to at most `maxWords` words and `maxChars` characters, at a word boundary.
 * Trailing commas, colons, dashes etc. left by the cut are dropped; nothing is appended.
 */
export function cutWords(text, maxWords = 0, maxChars = 0) {
  let words = String(text ?? "").split(" ").filter(Boolean);
  if (maxWords > 0 && words.length > maxWords) words = words.slice(0, maxWords);
  let out = words.join(" ");
  if (maxChars > 0 && cpLength(out) > maxChars) {
    out = cpSlice(out, maxChars + 1);
    const space = out.lastIndexOf(" ");
    // Cut at the last space, unless that throws away most of it (one very long word): then cut hard.
    out = space > maxChars * 0.6 ? out.slice(0, space) : cpSlice(out, maxChars);
  }
  const cut = out !== words.join(" ") || words.length < String(text ?? "").split(" ").filter(Boolean).length;
  return cut ? out.replace(/[\s,;:\-\u2013\u2014(\[]+$/u, "") : out;
}

/**
 * What an alert says, in as few words as possible. The planner's message is the whole thing (its title is a
 * short line for the planner UI). Other clients: the title plus at most TITLE_EXTRA_WORDS words of the message.
 * `clean` is cleanSpeech or smsClean.
 */
export function briefText({ client, title, message }, clean) {
  if (client === "planner") return clean(message);
  const t = clean(title);
  const extra = cutWords(clean(message), TITLE_EXTRA_WORDS);
  if (!extra) return t;
  if (!t) return extra;
  return `${/[.!?:]$/.test(t) ? t : t + ":"} ${extra}`;
}

/**
 * The full spoken prompt (plain text; escape when rendering): the intro (CALL_INTRO, "" = none), the alert cut to
 * CALL_MAX_WORDS words / CALL_MAX_CHARS characters, then the short keypad menu.
 */
export function alertSpeech({ intro = DEFAULT_CALL_INTRO, client, title, message }) {
  const body = cutWords(briefText({ client, title, message }, cleanSpeech), CALL_MAX_WORDS, CALL_MAX_CHARS);
  return [sentence(cleanSpeech(intro, 200)), sentence(body), MENU_SPEECH].filter(Boolean).join(" ");
}
