// Questions: agents ask Cameron by text, he answers by text. Pure helpers (validation, secret stripping, the SMS
// body, reply parsing); the state lives in core.js. Contract: ../docs/PHONE-GATEWAY.md "Questions".
import { cutWords } from "./speech.js";
import { smsClean } from "./sms.js";
import { cpLength } from "./util.js";

export const MAX_QUESTIONS = 5;
export const QUESTION_MAX = 300;
export const QUESTION_SMS_MAX = 140;
export const AGENT_MAX = 40;
export const OPTION_MAX = 30;
export const MAX_OPTIONS = 6;
export const REASON_MAX = 80;
export const ANSWER_MAX = 1000;
export const DEFAULT_EXPIRES_MINUTES = 1440;
export const MAX_EXPIRES_MINUTES = 10080;
const REF_RE = /^[A-Za-z0-9_.:@#-]{1,120}$/;

// Token-like runs: well-known key prefixes, JWTs, and any 32+ character run of letters/digits/_-+/= without spaces.
const SECRET_RES = [
  /\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}/g,
  /\b(?:ghp|gho|ghu|ghs|ghr|github_pat|gw|xox[abpr]|glpat)_[A-Za-z0-9_-]{8,}/g,
  /\bAKIA[0-9A-Z]{12,}/g,
  /\bAIza[0-9A-Za-z_-]{20,}/g,
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)?/g,
  /[A-Za-z0-9_\-+/=]{32,}/g,
];

/** Replaces anything that looks like a secret with [redacted]. */
export function stripSecrets(text) {
  let s = String(text ?? "");
  for (const re of SECRET_RES) s = s.replace(re, "[redacted]");
  return s;
}

function str(v, name, min, max, required) {
  if (v === undefined || v === null) return required ? { error: `${name} is required` } : { value: null };
  if (typeof v !== "string") return { error: `${name} must be a string` };
  const n = cpLength(v);
  if (v.trim() === "" || n < min || n > max) return { error: `${name} must be ${min}-${max} characters` };
  return { value: v };
}

/** Validates the POST /v1/questions body. Returns {value: {questions, call}} or {error}. */
export function validateQuestions(b) {
  if (!Array.isArray(b.questions) || b.questions.length < 1 || b.questions.length > MAX_QUESTIONS) {
    return { error: `questions must be an array of 1-${MAX_QUESTIONS}` };
  }
  if (b.call !== undefined && b.call !== null && typeof b.call !== "boolean") return { error: "call must be true or false" };
  if (b.call === true && b.questions.length !== 1) return { error: "call needs exactly one question" };
  const out = [];
  const refs = new Set();
  for (const [i, q] of b.questions.entries()) {
    const at = (m) => ({ error: `questions[${i}]: ${m}` });
    if (!q || typeof q !== "object" || Array.isArray(q)) return at("must be an object");
    if (typeof q.ref !== "string" || !REF_RE.test(q.ref)) return at("ref must be 1-120 characters of [A-Za-z0-9_.:@#-]");
    if (refs.has(q.ref)) return at("ref repeats");
    refs.add(q.ref);
    const agent = str(q.agent, "agent", 1, AGENT_MAX, true);
    if (agent.error) return at(agent.error);
    const question = str(q.question, "question", 1, QUESTION_MAX, true);
    if (question.error) return at(question.error);
    const reason = str(q.urgent_reason, "urgent_reason", 1, REASON_MAX, false);
    if (reason.error) return at(reason.error);
    let options = [];
    if (q.options !== undefined && q.options !== null) {
      if (!Array.isArray(q.options) || q.options.length > MAX_OPTIONS) return at(`options must be an array of at most ${MAX_OPTIONS}`);
      for (const o of q.options) {
        const r = str(o, "each option", 1, OPTION_MAX, true);
        if (r.error) return at(r.error);
      }
      options = q.options.map((o) => stripSecrets(o.trim()));
    }
    let expires = DEFAULT_EXPIRES_MINUTES;
    if (q.expires_minutes !== undefined && q.expires_minutes !== null) {
      if (!Number.isInteger(q.expires_minutes) || q.expires_minutes < 1 || q.expires_minutes > MAX_EXPIRES_MINUTES) {
        return at(`expires_minutes must be an integer 1-${MAX_EXPIRES_MINUTES}`);
      }
      expires = q.expires_minutes;
    }
    out.push({
      ref: q.ref,
      agent: stripSecrets(agent.value.trim()),
      question: stripSecrets(question.value.trim()),
      options,
      urgent_reason: reason.value ? stripSecrets(reason.value.trim()) : null,
      expires_minutes: expires,
    });
  }
  return { value: { questions: out, call: b.call === true } };
}

/** "1 ship, 2 wait" or "reply w/ answer". */
export function optionsText(options, clean = smsClean) {
  if (!options || !options.length) return "reply w/ answer";
  return options.map((o, i) => `${i + 1} ${clean(o)}`).join(", ");
}

/** One line: "Q3 appraiseme: ship tonight or wait? 1 ship, 2 wait" (urgent: "Q3 URGENT appraiseme (reason): ..."). */
export function questionLine(q) {
  const agent = smsClean(q.agent);
  const head = q.urgent_reason ? `Q${q.num} URGENT ${agent} (${smsClean(q.urgent_reason)})` : `Q${q.num} ${agent}`;
  return `${head}: ${cutWords(smsClean(q.question), 0, QUESTION_SMS_MAX)} ${optionsText(q.options)}`;
}

/** The SMS for a batch of questions. */
export function questionsSms(questions) {
  const lines = questions.map(questionLine);
  if (questions.length > 1) lines.push(`reply like: Q${questions[0].num} 1`);
  return lines.join("\n");
}

/** What the question call says. */
export function questionSpeech(q, clean) {
  const ask = `${clean(q.agent)} needs an answer. ${clean(q.question)}`;
  const end = /[.!?]$/.test(ask) ? ask : `${ask}.`;
  if (!q.options || !q.options.length) return `${end} Text your answer to this number. 1 if you got it.`;
  const keys = q.options.map((o, i) => `${i + 1} for ${clean(o)}`).join(", ");
  return `${end} ${keys}. 0 if it's not urgent.`;
}

// "allow <name> [2h|30m|today|always]" or "stop <name>": a name, at most one duration word, nothing else.
const COMMAND_RE =
  /^\s*(?:allow\s+[@#]?[a-z0-9][\w.:-]{0,59}(?:\s+(?:for\s+)?(?:\d{1,4}\s*(?:m|min|mins|h|hr|hrs|hour|hours|d|day|days)|today|always))?|stop\s+[@#]?[a-z0-9][\w.:-]{0,59})\s*$/i;
const NOT_NAMES = new Set(["please", "it", "now", "calling", "this", "that", "me", "texting", "all", "everything"]);
const TAG_RE = /^\s*(?:q\s*(\d{1,3})|#\s*(q_[0-9a-f]{16}))(?![0-9a-z_])[\s:.,;-]*([\s\S]*)$/i;

/** "allow appraiseme 2h" / "stop appraiseme" (owner commands for the bus). */
export function isOwnerCommand(body) {
  const text = String(body ?? "");
  if (!COMMAND_RE.test(text)) return false;
  const name = text.trim().split(/\s+/)[1].replace(/^[@#]/, "").toLowerCase();
  return !NOT_NAMES.has(name);
}

/** A reply tagged "Q3 ..." or "#q_... ...": {num, id, rest} or null. */
export function parseTag(body) {
  const m = TAG_RE.exec(String(body ?? ""));
  if (!m) return null;
  return { num: m[1] ? Number(m[1]) : null, id: m[2] ? m[2].toLowerCase() : null, rest: m[3].trim() };
}

/** Maps a reply to an answer: {answer, option, not_urgent}. */
export function parseAnswer(q, text) {
  const t = String(text ?? "").trim();
  const word = t.toLowerCase().replace(/[.!]+$/, "").trim();
  if (word === "0" || word === "not urgent") return { answer: "not urgent", option: null, not_urgent: true };
  if (/^\d$/.test(word) && q.options && q.options.length) {
    const n = Number(word);
    if (n >= 1 && n <= q.options.length) return { answer: q.options[n - 1], option: n, not_urgent: false };
  }
  return { answer: [...t].slice(0, ANSWER_MAX).join(""), option: null, not_urgent: false };
}
