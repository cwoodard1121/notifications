import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, TOKENS, START, MIN } from "./helpers.js";

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
const sim = (h, id, body, token) => h.api("POST", `/v1/alerts/${id}/simulate`, { body, token });

test("simulate does not exist outside dry-run", async () => {
  const h = setup();
  const a = (await h.create()).json.alert;
  const r = await sim(h, a.id, { event: "answer" });
  assert.equal(r.status, 404);
  assert.equal(r.json.error, "not_found");
});

test("dry-run: full call flow through simulate, with no outbound fetch at all", async () => {
  const h = setup({ env: { DRY_RUN: "1" } });
  const created = await h.create({ title: "T", message: "M", channels: ["text", "call"] });
  assert.equal(created.status, 201);
  const a = created.json.alert;
  assert.equal(a.status, "calling");
  assert.match(a.call.sid, /^CAdry[0-9a-f]+$/);
  assert.match(a.text.message_id, /^SMdry[0-9a-f]+$/);

  let r = await sim(h, a.id, { event: "answer" });
  assert.equal(r.status, 200);
  assert.ok(r.json.twiml.startsWith("<Response><Gather "));
  assert.ok(r.json.twiml.includes("Yo, you have to lock in on this now. T: M. 1 if you&apos;re on it"));

  r = await sim(h, a.id, { event: "status", call_status: "no-answer" });
  assert.equal(r.json.alert.status, "waiting_retry");

  r = await sim(h, a.id, { event: "advance", seconds: 180 });
  assert.equal(r.json.alert.status, "calling");
  assert.equal(r.json.alert.call.attempts, 2);

  r = await sim(h, a.id, { event: "digits", digits: "2" });
  assert.match(r.json.twiml, /Snoozed/);
  assert.equal(r.json.alert.status, "snoozed");
  assert.equal(r.json.alert.call.next_call_at, iso(START + 3 * MIN + 10 * MIN));
  await sim(h, a.id, { event: "status", call_status: "completed" });

  r = await sim(h, a.id, { event: "advance", seconds: 600 });
  assert.equal(r.json.alert.status, "calling");
  assert.equal(r.json.alert.call.attempts, 3);

  r = await sim(h, a.id, { event: "digits", digits: "1" });
  assert.match(r.json.twiml, /Acknowledged/);
  assert.equal(r.json.alert.status, "acked");
  await sim(h, a.id, { event: "status", call_status: "completed" });

  assert.deepEqual(await h.types(), [
    "text.sent",
    "call.dialing",
    "call.answered",
    "call.ended",
    "alert.retry_scheduled",
    "call.dialing",
    "alert.snoozed",
    "call.ended",
    "call.dialing",
    "alert.acked",
    "call.ended",
  ]);
  assert.equal(h.fetch.calls.length, 0, "dry-run never fetches");
  // The dry-run clock moved; the real alarm is set on real time.
  assert.equal((await h.get(a.id)).updated, iso(START + 13 * MIN));
  assert.ok(h.storage.alarm === null || h.storage.alarm >= START);
});

test("dry-run: ack of a ringing call does not fetch; text-only alerts send nothing", async () => {
  const h = setup({ env: { DRY_RUN: "1" } });
  const a = (await h.create()).json.alert;
  await h.api("POST", `/v1/alerts/${a.id}/ack`);
  const t = (await h.create({ channels: ["text"] })).json.alert;
  assert.equal(t.status, "sent");
  await sim(h, a.id, { event: "advance", seconds: 3600 });
  assert.equal(h.fetch.calls.length, 0);
});

test("simulate validates its body and checks the client", async () => {
  const h = setup({ env: { DRY_RUN: "1" } });
  const a = (await h.create()).json.alert;
  const t = (await h.create({ channels: ["text"] })).json.alert;
  const bad = [
    {},
    { event: "explode" },
    { event: "digits", digits: "12" },
    { event: "digits" },
    { event: "status", call_status: "bogus" },
    { event: "advance", seconds: 0 },
    { event: "advance", seconds: 1.5 },
    { event: "advance", seconds: "60" },
    { event: "advance", seconds: 31 * 24 * 3600 },
  ];
  for (const body of bad) {
    const r = await sim(h, a.id, body);
    assert.equal(r.status, 400, JSON.stringify(body));
    assert.equal(r.json.error, "invalid");
  }
  assert.equal((await sim(h, t.id, { event: "answer" })).status, 400, "text-only alert has no call");
  assert.equal((await sim(h, t.id, { event: "advance", seconds: 5 })).status, 200);
  assert.equal((await sim(h, a.id, { event: "answer" }, TOKENS.planner)).status, 404);
  assert.equal((await sim(h, a.id, { event: "status", call_status: "ringing" })).json.alert.status, "calling");
});

test("dry-run: simulate sms drives /twilio/sms as if OWNER_NUMBER texted, including a late ack", async () => {
  const h = setup({ env: { DRY_RUN: "1", OWNER_NUMBER: undefined } });
  const t = (await h.create({ channels: ["text"] })).json.alert;
  let r = await sim(h, t.id, { event: "sms", body: "1" });
  assert.equal(r.status, 200);
  assert.deepEqual(r.json, { twiml: "<Response><Message>nothing open rn</Message></Response>", alert: null });

  const a = (await h.create({ title: "T", retry: false, fallback_text: false })).json.alert;
  await sim(h, a.id, { event: "status", call_status: "no-answer" });
  assert.equal((await h.get(a.id)).status, "unanswered");
  r = await sim(h, a.id, { event: "sms", body: "hi" });
  assert.deepEqual(r.json, { twiml: "<Response><Message>reply 1 on it, 2 snooze, 9 done</Message></Response>", alert: null });
  r = await sim(h, t.id, { event: "sms", body: "1" });
  assert.equal(r.json.twiml, "<Response><Message>got it</Message></Response>");
  assert.equal(r.json.alert.id, a.id, "acts on the newest call alert, not the URL's alert");
  assert.equal(r.json.alert.status, "acked");
  assert.equal(r.json.alert.call.via, "sms");
  assert.deepEqual((await h.updates()).updates.at(-1).via, "sms");

  // Another client's alert is acted on but not returned.
  const p = (await h.create({ title: "P" }, { token: TOKENS.planner })).json.alert;
  r = await sim(h, a.id, { event: "sms", body: "2" });
  assert.equal(r.json.twiml, "<Response><Message>snoozed 10 min</Message></Response>");
  assert.equal(r.json.alert, null);
  assert.equal((await h.get(p.id, TOKENS.planner)).status, "snoozed");

  for (const body of [{ event: "sms" }, { event: "sms", body: 1 }, { event: "sms", body: "x".repeat(1601) }]) {
    assert.equal((await sim(h, a.id, body)).status, 400, JSON.stringify(body).slice(0, 40));
  }
  assert.equal(h.fetch.calls.length, 0, "dry-run never fetches");
});
