// Questions: agents ask Cameron by text, he answers by text (docs/PHONE-GATEWAY.md "Questions").
import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, TOKENS, MIN } from "./helpers.js";

const MSG = (t) => `<Response><Message>${t}</Message></Response>`;
let n = 0;

function ask(h, questions, extra = {}, token = TOKENS.bus) {
  const qs = (Array.isArray(questions) ? questions : [questions]).map((q) => ({
    ref: `ask-${++n}`,
    agent: "appraiseme",
    question: "Ship tonight or wait?",
    ...q,
  }));
  return h.api("POST", "/v1/questions", { body: { questions: qs, ...extra }, token });
}

async function feed(h, token = TOKENS.bus) {
  return (await h.updates({ token, limit: 200 })).updates;
}

test("one question: one short SMS with its Q tag and options; GET shows it open", async () => {
  const h = setup();
  const r = await ask(h, { options: ["ship", "wait"] });
  assert.equal(r.status, 201);
  const [q] = r.json.questions;
  assert.equal(q.num, 1);
  assert.equal(q.status, "open");
  assert.match(q.id, /^q_[0-9a-f]{16}$/);
  assert.equal(r.json.text.status, "sent");
  assert.deepEqual(h.texts(), ["Q1 appraiseme: Ship tonight or wait? 1 ship, 2 wait"]);
  const got = await h.api("GET", `/v1/questions/${q.id}`);
  assert.equal(got.json.question.status, "open");
  assert.equal(got.json.question._, undefined, "internals are not exposed");
  // Another client cannot see it.
  assert.equal((await h.api("GET", `/v1/questions/${q.id}`, { token: TOKENS.planner })).status, 404);
});

test("free-text, urgent and batched questions; secrets are stripped; a batch is one text", async () => {
  const h = setup();
  const r = await ask(h, [
    { question: "Which db for staging?" },
    { agent: "pecunia", question: "Roll back now? token sk-abcdefghijklmnopqrstu", options: ["yes", "no"], urgent_reason: "prod down" },
  ]);
  assert.equal(r.status, 201);
  assert.equal(h.texts().length, 1);
  assert.equal(
    h.texts()[0],
    "Q1 appraiseme: Which db for staging? reply w/ answer\n" +
      "Q2 URGENT pecunia (prod down): Roll back now? token [redacted] 1 yes, 2 no\n" +
      "reply like: Q1 1",
  );
  assert.equal((await h.storage.get("cnt:qtexts")).length, 1);
});

test("validation, duplicates and conflicts", async () => {
  const h = setup();
  for (const body of [
    { questions: [] },
    { questions: [{ ref: "a", agent: "x" }] },
    { questions: [{ ref: "a", agent: "x", question: "q", options: ["1", "2", "3", "4", "5", "6", "7"] }] },
    { questions: [{ ref: "a", agent: "x", question: "q", expires_minutes: 0 }] },
    { questions: [{ ref: "a", agent: "x", question: "q" }, { ref: "b", agent: "x", question: "q" }], call: true },
  ]) {
    assert.equal((await h.api("POST", "/v1/questions", { body })).status, 400, JSON.stringify(body));
  }
  const first = await h.api("POST", "/v1/questions", { body: { questions: [{ ref: "r1", agent: "a", question: "q?" }] } });
  assert.equal(first.status, 201);
  const again = await h.api("POST", "/v1/questions", { body: { questions: [{ ref: "r1", agent: "a", question: "q?" }] } });
  assert.equal(again.status, 200);
  assert.equal(again.json.duplicate, true);
  const partial = await h.api("POST", "/v1/questions", { body: { questions: [{ ref: "r1", agent: "a", question: "q?" }, { ref: "r2", agent: "a", question: "q?" }] } });
  assert.equal(partial.status, 409);
  assert.equal(h.texts().length, 1);
});

test("QUESTION_TEXTS_PER_DAY: the 11th question text is refused with retry_after and nothing stored", async () => {
  const h = setup();
  for (let i = 0; i < 10; i++) assert.equal((await ask(h, {})).status, 201);
  const r = await ask(h, {});
  assert.equal(r.status, 429);
  assert.ok(r.json.retry_after > 0);
  assert.match(r.json.detail, /question text limit/);
  assert.equal(h.texts().length, 10);
});

test("bare reply with one open question answers it: option digits, free text, 0 = not urgent", async () => {
  const h = setup();
  const q = (await ask(h, { options: ["ship", "wait"] })).json.questions[0];
  assert.equal((await h.sms("2")).text, MSG("got it -&gt; appraiseme"));
  const done = (await h.api("GET", `/v1/questions/${q.id}`)).json.question;
  assert.equal(done.status, "answered");
  assert.equal(done.answer, "wait");
  assert.equal(done.option, 2);
  assert.equal(done.via, "sms");
  const u = (await feed(h)).find((x) => x.type === "question.answered");
  assert.deepEqual(
    { question_id: u.question_id, ref: u.ref, num: u.num, answer: u.answer, option: u.option, not_urgent: u.not_urgent, via: u.via },
    { question_id: q.id, ref: q.ref, num: 1, answer: "wait", option: 2, not_urgent: false, via: "sms" },
  );
  // Free text is passed through verbatim; a digit beyond the options is free text too.
  const f = (await ask(h, { question: "Which db?" })).json.questions[0];
  await h.sms("  Use Postgres, not MySQL!  ");
  assert.equal((await h.api("GET", `/v1/questions/${f.id}`)).json.question.answer, "Use Postgres, not MySQL!");
  const z = (await ask(h, { options: ["a", "b"], urgent_reason: "prod" })).json.questions[0];
  await h.sms("0");
  const nz = (await h.api("GET", `/v1/questions/${z.id}`)).json.question;
  assert.equal(nz.not_urgent, true);
  assert.equal(nz.answer, "not urgent");
  // Nothing open any more.
  assert.equal((await h.sms("1")).text, MSG("nothing open rn"));
});

test("several open questions: a bare reply asks which one; a Q tag or #id picks it", async () => {
  const h = setup();
  const [a, b] = (await ask(h, [{ question: "One?" }, { question: "Two?", options: ["x", "y"] }])).json.questions;
  assert.equal((await h.sms("yes")).text, MSG("which one? Q1 or Q2"));
  assert.equal((await h.api("GET", `/v1/questions/${a.id}`)).json.question.status, "open");
  assert.equal((await h.sms("q2: 1")).text, MSG("got it -&gt; appraiseme"));
  assert.equal((await h.api("GET", `/v1/questions/${b.id}`)).json.question.answer, "x");
  assert.equal((await h.sms("Q7 hi")).text, MSG("no open Q7"));
  assert.equal((await h.sms("Q1")).text, MSG("what&apos;s your answer to Q1?"));
  assert.equal((await h.sms(`#${a.id} later tonight`)).text, MSG("got it -&gt; appraiseme"));
  assert.equal((await h.api("GET", `/v1/questions/${a.id}`)).json.question.answer, "later tonight");
});

test("an active alert keeps bare 1/2/9; free text still answers the question; an old unanswered alert does not steal 1", async () => {
  const h = setup();
  const q = (await ask(h, { question: "Which db?" })).json.questions[0];
  const al = (await h.create()).json.alert;
  assert.equal(al.status, "calling");
  assert.equal((await h.sms("1")).text, MSG("got it"));
  assert.equal((await h.get(al.id)).status, "acked");
  assert.equal((await h.api("GET", `/v1/questions/${q.id}`)).json.question.status, "open");

  // An unanswered alert (not active) + an open question: "1" goes to the question.
  const h2 = setup();
  const old = (await h2.create({ retry: false, fallback_text: false })).json.alert;
  await h2.status(old.id, 1, "no-answer");
  assert.equal((await h2.get(old.id)).status, "unanswered");
  const q2 = (await ask(h2, { options: ["ship", "wait"] })).json.questions[0];
  assert.equal((await h2.sms("1")).text, MSG("got it -&gt; appraiseme"));
  assert.equal((await h2.api("GET", `/v1/questions/${q2.id}`)).json.question.answer, "ship");
  assert.equal((await h2.get(old.id)).status, "unanswered");
});

test("owner commands go to the bus feed only and are confirmed", async () => {
  const h = setup();
  assert.equal((await h.sms("allow appraiseme 2h")).text, MSG("ok: allow appraiseme 2h"));
  assert.equal((await h.sms("Stop pecunia")).text, MSG("ok: Stop pecunia"));
  const bus = (await feed(h)).filter((u) => u.type === "owner.command").map((u) => u.text);
  assert.deepEqual(bus, ["allow appraiseme 2h", "Stop pecunia"]);
  assert.deepEqual((await feed(h, TOKENS.planner)).filter((u) => u.type === "owner.command"), []);
});

test("questions expire; cancel closes them; numbers are reused only when free", async () => {
  const h = setup();
  const q = (await ask(h, { expires_minutes: 30 })).json.questions[0];
  h.clock.t += 31 * MIN;
  assert.equal((await h.sms("sure")).text, MSG("nothing open rn"));
  assert.equal((await h.api("GET", `/v1/questions/${q.id}`)).json.question.status, "expired");
  const c = (await ask(h, {})).json.questions[0];
  assert.equal(c.num, 2);
  const r = await h.api("POST", `/v1/questions/${c.id}/cancel`);
  assert.equal(r.json.question.status, "canceled");
  assert.equal((await h.sms("Q2 yes")).text, MSG("no open Q2"));
});

test("question call: persistent, no text-first; keys pick options; answering stops it", async () => {
  const h = setup({ env: { TEXT_FIRST: "1", RETRY_MINUTES: "2", MAX_ATTEMPTS: "5" } });
  const r = await ask(h, { options: ["ship", "wait"], urgent_reason: "deadline" }, { call: true });
  const q = r.json.questions[0];
  assert.ok(q.call_alert_id);
  assert.equal(h.texts().length, 1, "only the question text");
  assert.equal(h.fetch.of("twilio.create").length, 1, "dials at once");
  const alert = await h.get(q.call_alert_id);
  assert.equal(alert.call.persist, true);
  const said = await h.answer(alert.id, 1, 0);
  assert.match(said.text, /appraiseme needs an answer\. Ship tonight or wait\? 1 for ship, 2 for wait\. 0 if it&apos;s not urgent\./);
  assert.ok(!said.text.includes("snooze"));
  assert.match((await h.press(alert.id, 1, "7")).text, /needs an answer/, "other keys repeat");
  assert.match((await h.press(alert.id, 1, "2")).text, /Got it\. Goodbye\./);
  const a = (await h.api("GET", `/v1/questions/${q.id}`)).json.question;
  assert.deepEqual([a.status, a.answer, a.option, a.via], ["answered", "wait", 2, "call"]);
  assert.equal((await h.get(alert.id)).status, "acked");
  await h.status(alert.id, 1, "completed");
  await h.advance(30 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 1);
});

test("question call: a texted answer ends the ringing call; free-text: 1 only stops the call", async () => {
  const h = setup();
  const q = (await ask(h, { options: ["ship", "wait"] }, { call: true })).json.questions[0];
  await h.sms("1");
  assert.equal((await h.api("GET", `/v1/questions/${q.id}`)).json.question.answer, "ship");
  assert.equal((await h.get(q.call_alert_id)).status, "acked");
  assert.equal(h.fetch.of("twilio.update").length, 1, "the ringing call was ended");

  const f = (await ask(h, { question: "Which db?" }, { call: true })).json.questions[0];
  await h.status(q.call_alert_id, 1, "completed");
  await h.advance(1000);
  const al = await h.get(f.call_alert_id);
  const said = await h.answer(al.id, al.call.attempts, 0);
  assert.match(said.text, /Text your answer to this number\. 1 if you got it\./);
  assert.match((await h.press(al.id, al.call.attempts, "1")).text, /Text your answer/);
  assert.equal((await h.get(al.id)).status, "acked");
  assert.equal((await h.api("GET", `/v1/questions/${f.id}`)).json.question.status, "open");
});
