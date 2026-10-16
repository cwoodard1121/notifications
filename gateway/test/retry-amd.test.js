// Answering machine detection, 2-minute redials, and persistent ("spam me till I acknowledge") alerts.
import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, MIN } from "./helpers.js";

// The deployed policy (wrangler.jsonc): redial every 2 min, 5 dials for normal alerts.
const LIVE = { RETRY_MINUTES: "2", MAX_ATTEMPTS: "5" };

async function endCall(h, id, attempt, status = "no-answer") {
  return h.status(id, attempt, status);
}

test("dials ask Twilio for answering machine detection (AMD=0 turns it off)", async () => {
  const h = setup({ env: LIVE });
  await h.create();
  const form = h.fetch.of("twilio.create")[0].form;
  assert.equal(form.get("MachineDetection"), "Enable");
  assert.equal(form.get("MachineDetectionTimeout"), "6");
  assert.equal(form.get("AsyncAmd"), null);

  const off = setup({ env: { ...LIVE, AMD: "0" } });
  await off.create();
  assert.equal(off.fetch.of("twilio.create")[0].form.get("MachineDetection"), null);
});

test("voicemail (AnsweredBy machine_*/fax): hang up without the message, count as no answer, retry in 2 min", async () => {
  for (const answeredBy of ["machine_start", "machine_end_beep", "fax"]) {
    const h = setup({ env: LIVE });
    const id = (await h.create()).json.alert.id;
    const r = await h.twilio(`/twilio/voice?alert=${id}&attempt=1&pass=0`, { CallSid: "CAx", CallStatus: "in-progress", AnsweredBy: answeredBy });
    assert.equal(r.text, "<Response><Hangup/></Response>", answeredBy);
    assert.ok(!r.text.includes("lock in"));
    await endCall(h, id, 1, "completed");
    const a = await h.get(id);
    assert.equal(a.status, "waiting_retry");
    assert.equal(a.call.answered_by, answeredBy);
    assert.equal(Date.parse(a.call.next_call_at), h.clock.t + 2 * MIN);
    const feed = (await h.updates({ limit: 200 })).updates;
    const types = feed.map((u) => u.type);
    assert.ok(types.includes("call.machine"));
    assert.ok(!types.includes("call.answered"), "a machine is not an answer");
    const ended = feed.find((u) => u.type === "call.ended");
    assert.equal(ended.answered_by, answeredBy);
    await h.advance(2 * MIN);
    assert.equal(h.fetch.of("twilio.create").length, 2, "redialed 2 minutes later");
  }
});

test("a human (or unknown) answer hears the message as before", async () => {
  for (const answeredBy of ["human", "unknown", ""]) {
    const h = setup({ env: LIVE });
    const id = (await h.create()).json.alert.id;
    const r = await h.twilio(`/twilio/voice?alert=${id}&attempt=1&pass=0`, { CallSid: "CAx", CallStatus: "in-progress", AnsweredBy: answeredBy });
    assert.match(r.text, /<Gather/);
    assert.match(r.text, /lock in on this now/);
  }
});

test("normal alert: busy / failed / no-answer / canceled all redial every 2 min, 5 dials, then unanswered", async () => {
  const h = setup({ env: LIVE });
  const id = (await h.create({ fallback_text: false })).json.alert.id;
  const statuses = ["no-answer", "busy", "failed", "canceled", "no-answer"];
  for (let i = 0; i < 5; i++) {
    assert.equal(h.fetch.of("twilio.create").length, i + 1);
    await endCall(h, id, i + 1, statuses[i]);
    if (i < 4) {
      const a = await h.get(id);
      assert.equal(a.status, "waiting_retry", `after dial ${i + 1}`);
      assert.equal(Date.parse(a.call.next_call_at) - h.clock.t, 2 * MIN);
      await h.advance(2 * MIN);
    }
  }
  const a = await h.get(id);
  assert.equal(a.status, "unanswered");
  assert.equal(a.call.attempts, 5);
  assert.equal(a.call.persist, false);
  await h.advance(60 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 5);
  // One alert, five dials: the client's daily call count grew by one.
  assert.equal((await h.storage.get("cnt:calls:bus")).length, 1);
  assert.equal((await h.storage.get("cnt:dials")).length, 5);
});

test("persistent alert (priority 5): keeps redialing every 2 min until a key, then stops", async () => {
  const h = setup({ env: LIVE });
  const created = (await h.create({ priority: 5 })).json.alert;
  assert.equal(created.call.persist, true);
  assert.equal(created.call.max_attempts, 25);
  const id = created.id;
  for (let i = 1; i <= 8; i++) {
    await endCall(h, id, i);
    assert.equal((await h.get(id)).status, "waiting_retry", `dial ${i}`);
    await h.advance(2 * MIN);
  }
  assert.equal(h.fetch.of("twilio.create").length, 9);
  await h.answer(id, 9, 0);
  await h.press(id, 9, "1");
  await endCall(h, id, 9, "completed");
  assert.equal((await h.get(id)).status, "acked");
  await h.advance(30 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 9);
});

test("persistent alert ends at max_minutes (window) and at 25 dials; the client can stop it any time", async () => {
  // Window: max_minutes 10 with 2-min spacing -> dials at 0,2,4,6,8, then the next would pass 10 min.
  const h = setup({ env: LIVE });
  const id = (await h.create({ persist: true, max_minutes: 10, fallback_text: false })).json.alert.id;
  for (let i = 1; i <= 10; i++) {
    const a = await h.get(id);
    if (a.status !== "calling") break;
    await endCall(h, id, a.call.attempts);
    await h.advance(2 * MIN);
  }
  let a = await h.get(id);
  assert.equal(a.status, "unanswered");
  assert.equal(a.call.attempts, 5);

  // Dial cap: 1-minute spacing for 60 minutes would be 60 dials; PERSIST_MAX_DIALS stops it at 25.
  const d = setup({ env: { ...LIVE, PERSIST_MAX_DIALS: "25" } });
  const id2 = (await d.create({ persist: true, retry_minutes: 1, fallback_text: false })).json.alert.id;
  for (let i = 0; i < 40; i++) {
    const x = await d.get(id2);
    if (x.status !== "calling") break;
    await endCall(d, id2, x.call.attempts);
    await d.advance(1 * MIN);
  }
  a = await d.get(id2);
  assert.equal(a.status, "unanswered");
  assert.equal(a.call.attempts, 25);

  // Client ack (e.g. the planner sees a started session) stops it.
  const c = setup({ env: LIVE });
  const id3 = (await c.create({ priority: 5 })).json.alert.id;
  await endCall(c, id3, 1);
  const r = await c.api("POST", `/v1/alerts/${id3}/ack`);
  assert.equal(r.json.alert.status, "acked");
  await c.advance(30 * MIN);
  assert.equal(c.fetch.of("twilio.create").length, 1);
});

test("persistent alert: snooze pauses 10 min, then the 2-min cycle resumes", async () => {
  const h = setup({ env: LIVE });
  const id = (await h.create({ priority: 5 })).json.alert.id;
  await h.answer(id, 1, 0);
  await h.press(id, 1, "2");
  await endCall(h, id, 1, "completed");
  let a = await h.get(id);
  assert.equal(a.status, "snoozed");
  assert.equal(Date.parse(a.call.next_call_at) - h.clock.t, 10 * MIN);
  await h.advance(10 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 2);
  await endCall(h, id, 2);
  a = await h.get(id);
  assert.equal(a.status, "waiting_retry");
  assert.equal(Date.parse(a.call.next_call_at) - h.clock.t, 2 * MIN);
  await h.advance(2 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 3);
});

test("persist defaults: off without priority 5, off with retry false; fields are validated", async () => {
  const h = setup({ env: LIVE });
  assert.equal((await h.create()).json.alert.call.persist, false);
  assert.equal((await h.create({ priority: 4 })).json.alert.call.persist, false);
  assert.equal((await h.create({ priority: 5, retry: false })).json.alert.call.persist, false);
  assert.equal((await h.create({ persist: true, retry_minutes: 3 })).json.alert.call.retry_minutes, 3);
  for (const bad of [{ priority: 6 }, { priority: "5" }, { persist: "yes" }, { retry_minutes: 0 }, { max_minutes: 1.5 }]) {
    const r = await h.create(bad);
    assert.equal(r.status, 400, JSON.stringify(bad));
  }
});

// ---- text first (the deployed default: TEXT_FIRST=1)
const TF = { ...LIVE, TEXT_FIRST: "1" };

test("text first: SMS now, the call 45 s later; one text even with channels text+call", async () => {
  const h = setup({ env: TF });
  const a = (await h.create()).json.alert;
  assert.equal(h.texts().length, 1);
  assert.equal(h.texts()[0], "[Agent Bus] URGENT: Pecunia prod is down\nHealth check failing for 6 minutes.\nReply 1 = on it, 2 = snooze 10 min, 9 = done.");
  assert.equal(a.status, "queued");
  assert.equal(Date.parse(a.call.next_call_at) - h.clock.t, 45 * 1000);
  assert.equal(h.fetch.of("twilio.create").length, 0);
  assert.equal(h.storage.alarm, h.clock.t + 45 * 1000);
  await h.advance(44 * 1000);
  assert.equal(h.fetch.of("twilio.create").length, 0);
  await h.advance(1000);
  assert.equal(h.fetch.of("twilio.create").length, 1);
  assert.equal((await h.get(a.id)).status, "calling");

  const both = setup({ env: TF });
  await both.create({ channels: ["text", "call"] });
  assert.equal(both.texts().length, 1);
});

test("text first: replying 1 (or 9) before the call rings cancels the dial", async () => {
  for (const [reply, status] of [["1", "acked"], ["9", "done"]]) {
    const h = setup({ env: TF });
    const id = (await h.create({ priority: 5 })).json.alert.id;
    await h.sms(reply);
    assert.equal((await h.get(id)).status, status);
    await h.advance(30 * MIN);
    assert.equal(h.fetch.of("twilio.create").length, 0);
  }
});

test("text first: re-texts every 3rd unanswered dial while it keeps calling", async () => {
  const h = setup({ env: TF });
  const id = (await h.create({ priority: 5 })).json.alert.id;
  await h.advance(45 * 1000);
  for (let i = 1; i <= 6; i++) {
    await h.status(id, i, "no-answer");
    await h.advance(2 * MIN);
  }
  const texts = h.texts();
  assert.equal(texts.length, 3, "opening text + after dial 3 + after dial 6");
  assert.ok(texts[1].startsWith("[Agent Bus] Still calling you: Pecunia prod is down\n"));
  assert.ok(texts[1].endsWith("Reply 1 = on it, 2 = snooze 10 min, 9 = done."));
  const kinds = (await h.updates({ limit: 200 })).updates.filter((u) => u.type === "text.sent").map((u) => u.kind ?? "alert");
  assert.deepEqual(kinds, ["alert", "reminder", "reminder"]);
});

test("text_first: false (or textFirst: false) skips the text and dials at once", async () => {
  for (const body of [{ text_first: false }, { textFirst: false }]) {
    const h = setup({ env: TF });
    await h.create(body);
    assert.equal(h.texts().length, 0);
    assert.equal(h.fetch.of("twilio.create").length, 1);
  }
});
