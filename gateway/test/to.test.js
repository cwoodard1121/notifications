import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, TOKENS, OWNER, FROM, MIN } from "./helpers.js";

// Per-alert `to` (planner multi-tenant): an invited user's calls and texts go to their own number, and only their own
// number's replies act on their alerts.

const MEMBER = "+15195550123";
const OTHER = "+15195550177";
const planner = { token: TOKENS.planner };

test("health says the gateway takes a per-alert to", async () => {
  const h = setup();
  assert.equal((await h.api("GET", "/v1/health", { token: null })).json.to, true);
});

test("to is stored and used for the call and the SMS; without it both go to OWNER_NUMBER", async () => {
  const h = setup();
  await h.create({ channels: ["text", "call"], to: MEMBER }, planner);
  const [call] = h.fetch.of("twilio.create");
  const [sms] = h.fetch.of("twilio.sms");
  assert.equal(call.form.get("To"), MEMBER);
  assert.equal(sms.form.get("To"), MEMBER);
  // One call at a time: the owner's alert goes on a fresh line.
  const o = setup();
  await o.create({ channels: ["text", "call"] }, planner);
  assert.equal(o.fetch.of("twilio.create")[0].form.get("To"), OWNER);
  assert.equal(o.fetch.of("twilio.sms")[0].form.get("To"), OWNER);
});

test("a missed call's fallback text goes to the alert's number too", async () => {
  const h = setup();
  const a = (await h.create({ channels: ["call"], retry: false, to: MEMBER }, planner)).json.alert;
  await h.status(a.id, 1, "no-answer");
  const texts = h.fetch.of("twilio.sms");
  assert.equal(texts.length, 1);
  assert.equal(texts[0].form.get("To"), MEMBER);
});

test("to that is not E.164 is 400", async () => {
  const h = setup();
  for (const to of ["519-555-0123", "15195550123", "+0123456789", 5195550123, "+1"]) {
    const r = await h.create({ to }, planner);
    assert.equal(r.status, 400, String(to));
  }
  assert.equal((await h.create({ to: null }, planner)).status, 201);
});

test("the alert JSON does not echo the number", async () => {
  const h = setup();
  const r = await h.create({ to: MEMBER }, planner);
  assert.equal(JSON.stringify(r.json).includes(MEMBER), false);
});

test("a reply from the member's number acts only on the member's alert; the owner's reply only on the owner's", async () => {
  const h = setup();
  const own = (await h.create({ channels: ["call"] }, planner)).json.alert;
  h.clock.t += MIN;
  const mem = (await h.create({ channels: ["call"], to: MEMBER }, planner)).json.alert;
  // The member's alert is newer, but the owner's 1 acks the owner's.
  await h.sms("1", OWNER);
  assert.equal((await h.get(own.id, TOKENS.planner)).status, "acked");
  assert.notEqual((await h.get(mem.id, TOKENS.planner)).status, "acked");
  await h.sms("1", MEMBER);
  assert.equal((await h.get(mem.id, TOKENS.planner)).status, "acked");
});

test("a number with no alerts of its own is ignored, as before", async () => {
  const h = setup();
  const mem = (await h.create({ channels: ["call"], to: MEMBER }, planner)).json.alert;
  const r = await h.sms("1", OTHER);
  assert.equal(r.text, "<Response/>");
  assert.equal((await h.get(mem.id, TOKENS.planner)).status, "calling");
});

test("the owner with only member alerts open hears there are none", async () => {
  const h = setup();
  await h.create({ channels: ["call"], to: MEMBER }, planner);
  const r = await h.sms("1", OWNER);
  assert.equal(r.text, "<Response><Message>No open alerts.</Message></Response>");
});

test("a call in from the member's number replays the member's newest alert, never the owner's", async () => {
  const h = setup();
  await h.create({ channels: ["call"], title: "Owner thing" }, planner);
  h.clock.t += MIN;
  await h.create({ channels: ["call"], title: "Sam thing", to: MEMBER }, planner);
  h.clock.t += MIN;
  await h.create({ channels: ["call"], title: "Owner newer" }, planner);
  const r = await h.twilio("/twilio/incoming", { CallSid: "CAin", From: MEMBER, To: FROM, CallStatus: "ringing" });
  assert.match(r.text, /Sam thing/);
  assert.doesNotMatch(r.text, /Owner/);
  const stranger = await h.twilio("/twilio/incoming", { CallSid: "CAin2", From: OTHER, To: FROM, CallStatus: "ringing" });
  assert.equal(stranger.text, "<Response><Reject/></Response>");
});
