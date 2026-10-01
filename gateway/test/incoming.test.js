import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, TOKENS, OWNER, FROM, ORIGIN, MIN } from "./helpers.js";

const SAY = (t) => `<Say voice="Polly.Matthew-Neural">${t}</Say>`;
const call = (h, from) => h.twilio("/twilio/incoming", { CallSid: "CAin", From: from, To: FROM, CallStatus: "ringing" });

test("calls from other numbers are rejected", async () => {
  const h = setup();
  await h.create();
  const r = await call(h, "+15550001111");
  assert.equal(r.status, 200);
  assert.equal(r.text, "<Response><Reject/></Response>");
  const noOwner = setup({ env: { OWNER_NUMBER: undefined } });
  assert.equal((await call(noOwner, OWNER)).text, "<Response><Reject/></Response>");
});

test("owner with no open alerts hears 'No open alerts'", async () => {
  const h = setup();
  assert.equal((await call(h, OWNER)).text, `<Response>${SAY("No open alerts. Goodbye.")}<Hangup/></Response>`);
  // acked, canceled, text-only and >24 h old alerts don't count
  const a = (await h.create()).json.alert;
  await h.press(a.id, 1, "1");
  await h.status(a.id, 1, "completed");
  const b = (await h.create()).json.alert;
  await h.api("POST", `/v1/alerts/${b.id}/cancel`);
  await h.create({ channels: ["text"] });
  assert.match((await call(h, OWNER)).text, /No open alerts/);
  const old = setup();
  await old.create({ retry: false });
  await old.status((await old.updates()).updates[0].alert_id, 1, "no-answer");
  await old.advance(24 * 60 * MIN + 1000);
  assert.match((await call(old, OWNER)).text, /No open alerts/);
});

test("owner hears the newest open alert and can acknowledge it (even after unanswered)", async () => {
  const h = setup();
  const bus = (await h.create({ title: "Older", retry: false, fallback_text: false })).json.alert;
  await h.status(bus.id, 1, "no-answer");
  h.clock.t += MIN;
  const pl = (await h.create({ title: "Newest", retry: false, fallback_text: false }, { token: TOKENS.planner })).json.alert;
  await h.status(pl.id, 1, "busy");
  assert.equal((await h.get(pl.id, TOKENS.planner)).status, "unanswered");

  const r = await call(h, OWNER);
  assert.ok(r.text.includes("Urgent alert from your planner. Newest."));
  assert.ok(r.text.includes(`action="${ORIGIN}/twilio/gather?alert=${pl.id}&amp;attempt=0"`));
  assert.ok(r.text.includes(`${ORIGIN}/twilio/voice?alert=${pl.id}&amp;attempt=0&amp;pass=1</Redirect>`));
  const p1 = await h.answer(pl.id, 0, 1);
  assert.ok(p1.text.endsWith(`${SAY("No key pressed. Goodbye.")}<Hangup/></Response>`));
  assert.equal((await h.press(pl.id, 0, "3")).text, r.text, "3 repeats");

  const g = await h.press(pl.id, 0, "1");
  assert.equal(g.text, `<Response>${SAY("Acknowledged. Goodbye.")}<Hangup/></Response>`);
  const acked = await h.get(pl.id, TOKENS.planner);
  assert.equal(acked.status, "acked");
  assert.equal(acked.call.digit, "1");
  assert.equal((await h.types(TOKENS.planner)).at(-1), "alert.acked");
  assert.equal(
    (await h.press(pl.id, 0, "2")).text,
    `<Response>${SAY("This alert was already handled. Goodbye.")}<Hangup/></Response>`,
    "an acked alert cannot be replayed or snoozed by call-back",
  );
  assert.match((await h.answer(pl.id, 0, 0)).text, /already handled/);

  // Next call-back replays the older bus alert; 2 on a final alert counts as 1.
  const r2 = await call(h, OWNER);
  assert.ok(r2.text.includes("Urgent alert from your agents. Older."));
  const g2 = await h.press(bus.id, 0, "2");
  assert.match(g2.text, /Acknowledged\. Goodbye\./);
  assert.equal((await h.get(bus.id)).status, "acked");
  assert.match((await call(h, OWNER)).text, /No open alerts/);
});

test("call-back on a pending alert: 2 snoozes it, 1 cancels the pending retry", async () => {
  const h = setup();
  const a = (await h.create()).json.alert;
  await h.status(a.id, 1, "no-answer");
  assert.equal((await h.get(a.id)).status, "waiting_retry");
  assert.match((await call(h, OWNER)).text, /Urgent alert from your agents/);
  const g = await h.press(a.id, 0, "2");
  assert.match(g.text, /Snoozed\. I will call again in 10 minutes\./);
  const s = await h.get(a.id);
  assert.equal(s.status, "snoozed");
  assert.equal(s.call.snoozes, 1);
  await h.advance(3 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 1, "retry replaced by the snooze");
  await h.press(a.id, 0, "1");
  assert.equal((await h.get(a.id)).status, "acked");
  await h.advance(20 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 1);
  assert.equal(h.fetch.of("twilio.update").length, 0);
});
