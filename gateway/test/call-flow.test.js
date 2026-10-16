import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, ORIGIN, OWNER, FROM, AUTH, START, MIN } from "./helpers.js";

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
const SAY = (t) => `<Say voice="Polly.Matthew-Neural">${t}</Say>`;

test("happy path: dialing -> answered -> 1 -> acked, retry cancelled", async () => {
  const h = setup();
  const r = await h.create({ ref: "bus-call-1", title: "T", message: "M" });
  assert.equal(r.status, 201);
  const a = r.json.alert;
  const id = a.id;
  assert.match(id, /^al_[0-9a-f]{16}$/);
  assert.equal(a.client, "bus");
  assert.equal(a.status, "calling");
  assert.equal(a.created, iso(START));
  assert.deepEqual(a.call, {
    attempts: 1,
    max_attempts: 2,
    persist: false,
    retry_minutes: 3,
    until: null,
    answered_by: null,
    snoozes: 0,
    sid: "CA" + "0".repeat(31) + "1",
    last_call_status: null,
    next_call_at: null,
    digit: null,
    via: null,
  });
  assert.equal(a.text, null);
  assert.deepEqual(Object.keys(a), ["id", "client", "ref", "title", "message", "channels", "status", "created", "updated", "call", "text"]);

  const [req] = h.fetch.of("twilio.create");
  assert.equal(req.url, `https://api.twilio.com/2010-04-01/Accounts/AC${"1".repeat(32)}/Calls.json`);
  assert.equal(req.method, "POST");
  assert.equal(req.headers.Authorization, "Basic " + btoa(`AC${"1".repeat(32)}:${AUTH}`));
  assert.deepEqual(Object.fromEntries(req.form), {
    To: OWNER,
    From: FROM,
    Url: `${ORIGIN}/twilio/voice?alert=${id}&attempt=1&pass=0`,
    Method: "POST",
    StatusCallback: `${ORIGIN}/twilio/status?alert=${id}&attempt=1`,
    StatusCallbackMethod: "POST",
    Timeout: "25",
    MachineDetection: "Enable",
    MachineDetectionTimeout: "6",
  });

  const v = await h.twilio(req.form.get("Url"), { CallSid: a.call.sid, CallStatus: "in-progress" });
  assert.equal(v.status, 200);
  assert.ok(v.text.includes("<Gather "));
  const g = await h.press(id, 1, "1");
  assert.equal(g.text, `<Response>${SAY("Acknowledged. Goodbye.")}<Hangup/></Response>`);
  let cur = await h.get(id);
  assert.equal(cur.status, "acked");
  assert.equal(cur.call.digit, "1");
  assert.equal(cur.call.via, "call");

  const s = await h.twilio(req.form.get("StatusCallback"), { CallSid: a.call.sid, CallStatus: "completed" });
  assert.equal(s.text, "<Response/>");
  cur = await h.get(id);
  assert.equal(cur.status, "acked");
  assert.equal(cur.call.last_call_status, "completed");

  const feed = await h.updates();
  assert.deepEqual(
    feed.updates.map((u) => u.type),
    ["call.dialing", "call.answered", "alert.acked", "call.ended"],
  );
  const acked = feed.updates[2];
  assert.deepEqual(Object.keys(acked), ["seq", "alert_id", "ref", "type", "status", "attempt", "digit", "via", "at"]);
  assert.equal(acked.digit, "1");
  assert.equal(acked.via, "call");
  assert.equal(acked.ref, "bus-call-1");
  assert.equal(feed.updates[3].call_status, "completed");

  await h.advance(30 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 1, "no retry after ack");
  assert.equal(h.fetch.of("twilio.update").length, 0, "keypad ack hangs up via TwiML, not REST");
  assert.equal(h.fetch.of("twilio.sms").length, 0);
});

test("digit 2 snoozes; the snoozed call rings after 10 min with its own retry; 4th snooze counts as ack", async () => {
  const h = setup();
  const id = (await h.create()).json.alert.id;

  let g = await h.press(id, 1, "2");
  assert.equal(g.text, `<Response>${SAY("Snoozed. I will call again in 10 minutes.")}<Hangup/></Response>`);
  let a = await h.get(id);
  assert.equal(a.status, "snoozed");
  assert.equal(a.call.snoozes, 1);
  assert.equal(a.call.digit, "2");
  assert.equal(a.call.next_call_at, iso(START + 10 * MIN));
  await h.status(id, 1, "completed");
  assert.equal((await h.get(id)).status, "snoozed", "trailing completed keeps snoozed");

  await h.advance(10 * MIN - 1000);
  assert.equal((await h.get(id)).status, "snoozed");
  await h.advance(1000);
  a = await h.get(id);
  assert.equal(a.status, "calling");
  assert.equal(a.call.attempts, 2);
  assert.match(h.fetch.of("twilio.create")[1].form.get("Url"), /attempt=2&pass=0$/);

  // The snoozed call gets its own retry.
  await h.status(id, 2, "no-answer");
  a = await h.get(id);
  assert.equal(a.status, "waiting_retry");
  await h.advance(3 * MIN);
  a = await h.get(id);
  assert.equal(a.status, "calling");
  assert.equal(a.call.attempts, 3);

  // Snoozes 2 and 3.
  await h.press(id, 3, "2");
  await h.status(id, 3, "completed");
  await h.advance(10 * MIN);
  await h.press(id, 4, "2");
  await h.status(id, 4, "completed");
  a = await h.get(id);
  assert.equal(a.call.snoozes, 3);
  await h.advance(10 * MIN);
  assert.equal((await h.get(id)).status, "calling");

  // 4th press of 2 counts as 1.
  g = await h.press(id, 5, "2");
  assert.equal(g.text, `<Response>${SAY("Acknowledged. Goodbye.")}<Hangup/></Response>`);
  a = await h.get(id);
  assert.equal(a.status, "acked");
  assert.equal(a.call.snoozes, 3);
  assert.equal(a.call.digit, "2");
  const ups = (await h.updates()).updates;
  assert.equal(ups.filter((u) => u.type === "alert.snoozed").length, 3);
  const last = ups.at(-1);
  assert.equal(last.type, "alert.acked");
  assert.equal(last.digit, "2");
  assert.equal(last.via, "call");
  assert.deepEqual(
    ups.filter((u) => u.type === "alert.snoozed").map((u) => [u.digit, u.via]),
    [["2", "call"], ["2", "call"], ["2", "call"]],
  );
  assert.equal(ups.find((u) => u.type === "alert.snoozed").next_call_at, iso(START + 10 * MIN));
});

test("no answer -> waiting_retry -> retry after 3 min -> no answer -> unanswered + fallback text", async () => {
  const h = setup();
  const id = (await h.create({ title: "Server down", message: "Fix it" })).json.alert.id;

  await h.status(id, 1, "no-answer");
  let a = await h.get(id);
  assert.equal(a.status, "waiting_retry");
  assert.equal(a.call.last_call_status, "no-answer");
  assert.equal(a.call.next_call_at, iso(START + 3 * MIN));

  await h.advance(3 * MIN - 1000);
  assert.equal(h.fetch.of("twilio.create").length, 1);
  await h.advance(1000);
  a = await h.get(id);
  assert.equal(a.status, "calling");
  assert.equal(a.call.attempts, 2);
  assert.equal(a.call.next_call_at, null);

  await h.status(id, 2, "busy");
  a = await h.get(id);
  assert.equal(a.status, "unanswered");
  assert.equal(a.call.last_call_status, "busy");
  const [sms] = h.fetch.of("twilio.sms");
  assert.equal(sms.url, `https://api.twilio.com/2010-04-01/Accounts/AC${"1".repeat(32)}/Messages.json`);
  assert.equal(sms.headers.Authorization, "Basic " + btoa(`AC${"1".repeat(32)}:${AUTH}`));
  assert.deepEqual(Object.fromEntries(sms.form), {
    To: OWNER,
    From: FROM,
    Body: "[Agent Bus] Missed urgent call: Server down\nFix it\nReply 1 = got it, 9 = done.",
  });
  assert.deepEqual(a.text, { status: "sent", message_id: "SM" + "0".repeat(29) + "801" });

  const ups = (await h.updates()).updates;
  assert.deepEqual(
    ups.map((u) => [u.type, u.status, u.attempt]),
    [
      ["call.dialing", "calling", 1],
      ["call.ended", "calling", 1],
      ["alert.retry_scheduled", "waiting_retry", 1],
      ["call.dialing", "calling", 2],
      ["call.ended", "calling", 2],
      ["alert.unanswered", "unanswered", 2],
      ["text.sent", "unanswered", 2],
    ],
  );
  assert.equal(ups[1].call_status, "no-answer");
  assert.equal(ups[2].next_call_at, iso(START + 3 * MIN));
  assert.equal(ups[6].message_id, "SM" + "0".repeat(29) + "801");

  await h.advance(60 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 2, "no more dials");
});

test("fallback_text false sends no text; retry false makes one attempt", async () => {
  const h = setup();
  const id = (await h.create({ fallback_text: false })).json.alert.id;
  await h.status(id, 1, "no-answer");
  await h.advance(3 * MIN);
  await h.status(id, 2, "no-answer");
  const a = await h.get(id);
  assert.equal(a.status, "unanswered");
  assert.equal(a.text, null);
  assert.equal(h.fetch.of("twilio.sms").length, 0);

  const one = (await h.create({ retry: false, fallback_text: false })).json.alert;
  assert.equal(one.call.max_attempts, 1);
  await h.status(one.id, 1, "completed");
  assert.equal((await h.get(one.id)).status, "unanswered");
  assert.equal(h.fetch.of("twilio.create").length, 3);
});

test("digit 3 and other keys repeat the message", async () => {
  const h = setup();
  const id = (await h.create()).json.alert.id;
  const first = await h.answer(id, 1, 0);
  for (const d of ["3", "4", "*", "#", "0"]) {
    const r = await h.press(id, 1, d);
    assert.equal(r.text, first.text, `digit ${d}`);
  }
  assert.equal((await h.get(id)).status, "calling");
  const types = await h.types();
  assert.equal(types.filter((t) => t === "call.answered").length, 1);
});

test("digit 9 = done: final, emits alert.done with digit 9 + via call, no retry", async () => {
  const h = setup();
  const id = (await h.create()).json.alert.id;
  await h.answer(id, 1, 0);
  const r = await h.press(id, 1, "9");
  assert.equal(r.text, '<Response><Say voice="Polly.Matthew-Neural">Nice. Marked as done. Goodbye.</Say><Hangup/></Response>');
  const a = await h.get(id);
  assert.equal(a.status, "done");
  assert.equal(a.call.digit, "9");
  assert.equal(a.call.via, "call");
  assert.equal(a.call.next_call_at, null);
  const feed = (await h.updates({ limit: 200 })).updates;
  const done = feed.find((u) => u.type === "alert.done");
  assert.equal(done.status, "done");
  assert.equal(done.digit, "9");
  assert.equal(done.via, "call");
  await h.status(id, 1, "completed");
  await h.advance(60 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 1);
  assert.equal((await h.get(id)).status, "done");
  // A call-back no longer replays it; a later "1" by text has nothing to act on.
  const back = await h.twilio("/twilio/incoming", { CallSid: "CAin", From: h.env.OWNER_NUMBER, To: h.env.TWILIO_FROM });
  assert.match(back.text, /Nothing urgent right now/);
});

test("texting 9 or done marks the newest open alert done", async () => {
  const h = setup();
  const id = (await h.create({ channels: ["text", "call"] })).json.alert.id;
  const r = await h.sms("Done!");
  assert.equal(r.text, "<Response><Message>Marked done: Pecunia prod is down</Message></Response>");
  const a = await h.get(id);
  assert.equal(a.status, "done");
  assert.equal(a.call.via, "sms");
  assert.ok((await h.types()).includes("alert.done"));
});

test("no input: pass 0 -> pass 1 -> goodbye; wording differs on the last attempt", async () => {
  const h = setup();
  const id = (await h.create()).json.alert.id;
  const p0 = await h.answer(id, 1, 0);
  assert.ok(p0.text.includes(`<Redirect method="POST">${ORIGIN}/twilio/voice?alert=${id}&amp;attempt=1&amp;pass=1</Redirect>`));
  assert.ok(!p0.text.includes("<Hangup/>"));
  const p1 = await h.answer(id, 1, 1);
  assert.ok(p1.text.includes("<Gather "), "message plays a second time");
  assert.ok(p1.text.endsWith(`</Gather>${SAY("No key pressed. I will call back in 3 minutes.")}<Hangup/></Response>`));
  assert.ok(!p1.text.includes("<Redirect"));
  // Empty Digits (should not happen with actionOnEmptyResult=false) falls back to pass 1.
  assert.equal((await h.press(id, 1, "")).text, p1.text);
  assert.equal((await h.types()).filter((t) => t === "call.answered").length, 1);

  await h.status(id, 1, "completed");
  assert.equal((await h.get(id)).status, "waiting_retry");
  await h.advance(3 * MIN);
  const last = await h.answer(id, 2, 1);
  assert.ok(last.text.endsWith(`${SAY("No key pressed. I will text you instead.")}<Hangup/></Response>`));
});

test("last-attempt goodbye promises a text only when one will be sent", async () => {
  const TEXT = `${SAY("No key pressed. I will text you instead.")}<Hangup/></Response>`;
  const BYE = `${SAY("No key pressed. Goodbye.")}<Hangup/></Response>`;
  const cases = [
    { name: "default", env: {}, body: {}, end: TEXT },
    { name: "fallback_text false (the bus)", env: {}, body: { fallback_text: false }, end: BYE },
    { name: "dry run without twilio", env: { DRY_RUN: "1", TWILIO_ACCOUNT_SID: undefined }, body: {}, end: TEXT },
  ];
  for (const c of cases) {
    const h = setup({ env: c.env });
    const id = (await h.create({ retry: false, ...c.body })).json.alert.id;
    const r = await h.answer(id, 1, 1);
    assert.ok(r.text.endsWith(c.end), `${c.name}: ${r.text}`);
  }
});

test("stale-attempt webhooks are ignored", async () => {
  const h = setup();
  const id = (await h.create()).json.alert.id;
  await h.status(id, 1, "no-answer");
  await h.advance(3 * MIN);
  assert.equal((await h.get(id)).call.attempts, 2);
  const before = (await h.updates()).updates.length;

  assert.equal((await h.status(id, 1, "completed")).text, "<Response/>");
  assert.equal((await h.answer(id, 1, 0)).text, `<Response>${SAY("This alert was already handled. Goodbye.")}<Hangup/></Response>`);
  assert.equal((await h.press(id, 1, "1")).text, `<Response>${SAY("This alert was already handled. Goodbye.")}<Hangup/></Response>`);
  assert.equal((await h.status(id, 2, "ringing")).text, "<Response/>", "non-terminal statuses ignored");
  assert.equal((await h.status("al_ffffffffffffffff", 1, "completed")).status, 200);
  const a = await h.get(id);
  assert.equal(a.status, "calling");
  assert.equal((await h.updates()).updates.length, before);

  await h.status(id, 2, "completed");
  await h.status(id, 2, "completed"); // duplicate callback
  const ups = (await h.updates()).updates;
  assert.equal(ups.filter((u) => u.type === "call.ended" && u.attempt === 2).length, 1);
  assert.equal((await h.get(id)).status, "unanswered");
  assert.equal(h.fetch.of("twilio.sms").length, 1, "one fallback text only");
});

test("a call that never reports back is ended as unknown after 10 minutes", async () => {
  const h = setup();
  const id = (await h.create()).json.alert.id;
  assert.equal(h.storage.alarm, START + 10 * MIN);
  await h.advance(10 * MIN);
  const a = await h.get(id);
  assert.equal(a.status, "waiting_retry");
  assert.equal(a.call.last_call_status, "unknown");
  const ended = (await h.updates()).updates.find((u) => u.type === "call.ended");
  assert.equal(ended.call_status, "unknown");
  assert.ok(ended.detail);
  await h.advance(3 * MIN);
  assert.equal((await h.get(id)).call.attempts, 2);
  // Webhook activity on the live call pushes the timeout out.
  await h.advance(9 * MIN);
  await h.answer(id, 2, 0);
  await h.advance(9 * MIN);
  assert.equal((await h.get(id)).status, "calling");
  await h.advance(1 * MIN);
  assert.equal((await h.get(id)).status, "unanswered");
});

test("one call at a time: the second alert queues and dials when the line is free", async () => {
  const h = setup();
  const a = (await h.create({ ref: "first" })).json.alert;
  h.clock.t += 1000;
  const r = await h.create({ ref: "second" });
  assert.equal(r.status, 201);
  const b = r.json.alert;
  assert.equal(b.status, "queued");
  assert.equal(b.call.attempts, 0);
  assert.equal(b.call.next_call_at, iso(START + 1000));
  assert.equal(h.fetch.of("twilio.create").length, 1);
  assert.equal(h.storage.alarm, START + 1000 + 20 * 1000, "re-check in 20 s");

  await h.advance(20 * 1000);
  assert.equal((await h.get(b.id)).status, "queued");
  assert.equal(h.fetch.of("twilio.create").length, 1);

  await h.press(a.id, 1, "1");
  assert.equal((await h.get(b.id)).status, "queued", "line busy until the call really ends");
  await h.status(a.id, 1, "completed");
  const b2 = await h.get(b.id);
  assert.equal(b2.status, "calling");
  assert.equal(b2.call.attempts, 1);
  assert.equal(h.fetch.of("twilio.create").length, 2);

  // A retry that comes due while the line is busy waits as queued.
  const c = (await h.create({ ref: "third" })).json.alert;
  assert.equal(c.status, "queued");
  await h.status(b.id, 1, "no-answer"); // b -> waiting_retry, c dials
  assert.equal((await h.get(c.id)).status, "calling");
  await h.advance(3 * MIN);
  const b3 = await h.get(b.id);
  assert.equal(b3.status, "queued");
  assert.equal(b3.call.next_call_at, iso(START + 21 * 1000 + 3 * MIN));
  await h.press(c.id, 1, "1");
  await h.status(c.id, 1, "completed");
  const b4 = await h.get(b.id);
  assert.equal(b4.status, "calling");
  assert.equal(b4.call.attempts, 2);
});
