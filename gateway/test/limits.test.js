import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, TOKENS, MIN } from "./helpers.js";

const DAY = 24 * 60 * MIN;

test("CLIENT_DAILY_CALLS: 429 + retry_after, nothing stored, per client, rolling 24 h", async () => {
  const h = setup({ env: { CLIENT_DAILY_CALLS: "2" } });
  const a = (await h.create()).json.alert;
  await h.api("POST", `/v1/alerts/${a.id}/ack`);
  await h.status(a.id, 1, "completed");
  h.clock.t += MIN;
  const b = (await h.create()).json.alert;
  await h.api("POST", `/v1/alerts/${b.id}/ack`);
  await h.status(b.id, 1, "completed");
  h.clock.t += MIN;

  const keysBefore = h.storage.keys();
  const updatesBefore = (await h.updates()).updates.length;
  const r = await h.create({ ref: "third" });
  assert.equal(r.status, 429);
  assert.equal(r.json.error, "rate_limited");
  assert.match(r.json.detail, /client daily call limit/);
  assert.equal(r.json.retry_after, DAY / 1000 - 120);
  assert.equal(r.headers.get("Retry-After"), String(DAY / 1000 - 120));
  assert.deepEqual(h.storage.keys(), keysBefore);
  assert.equal((await h.updates()).updates.length, updatesBefore);
  assert.equal(h.fetch.of("twilio.create").length, 2);

  assert.equal((await h.create({}, { token: TOKENS.planner })).status, 201, "other clients unaffected");
  assert.equal((await h.create({ ref: "text-ok", channels: ["text"] })).status, 201, "text-only not capped by calls");

  h.clock.t += DAY - 2 * MIN;
  const later = await h.create({ ref: "third" });
  assert.equal(later.status, 201, "window rolled; the rejected ref was never stored");
  assert.equal(later.json.duplicate, undefined);
});

test("MAX_DIALS_PER_DAY counts retries; a later dial over the cap ends unanswered", async () => {
  const h = setup({ env: { MAX_DIALS_PER_DAY: "2" } });
  const a = (await h.create({ ref: "a" })).json.alert; // dial 1
  const b = (await h.create({ ref: "b" })).json.alert; // queued
  assert.equal(b.status, "queued");
  await h.status(a.id, 1, "no-answer"); // a waits; b dials (dial 2)
  assert.equal((await h.get(b.id)).status, "calling");

  const r = await h.create({ ref: "c" });
  assert.equal(r.status, 429);
  assert.match(r.json.detail, /daily dial limit/);
  assert.ok(r.json.retry_after > 0);

  await h.press(b.id, 1, "1");
  await h.status(b.id, 1, "completed");
  await h.advance(3 * MIN); // a's retry is due but the cap is hit
  const a2 = await h.get(a.id);
  assert.equal(a2.status, "unanswered");
  assert.equal(a2.call.attempts, 2);
  const ups = (await h.updates()).updates.filter((u) => u.alert_id === a.id);
  const ended = ups.filter((u) => u.type === "call.ended").at(-1);
  assert.equal(ended.call_status, "failed");
  assert.match(ended.detail, /daily dial limit/);
  assert.deepEqual(ups.slice(-2).map((u) => u.type), ["alert.unanswered", "text.sent"]);
  assert.equal(h.fetch.of("twilio.create").length, 2);
});

test("MAX_TEXTS_PER_DAY: 429 for text alerts; fallback over the cap is text.failed", async () => {
  const h = setup({ env: { MAX_TEXTS_PER_DAY: "1" } });
  assert.equal((await h.create({ channels: ["text"] })).status, 201);
  const r = await h.create({ channels: ["text", "call"] });
  assert.equal(r.status, 429);
  assert.match(r.json.detail, /daily text limit/);
  assert.equal(h.fetch.of("twilio.create").length, 0, "nothing dialed for a rejected alert");

  const c = (await h.create({ channels: ["call"], retry: false })).json.alert;
  await h.status(c.id, 1, "no-answer");
  const done = await h.get(c.id);
  assert.equal(done.status, "unanswered");
  assert.equal(done.text.status, "failed");
  assert.match(done.text.detail, /daily text limit/);
  assert.equal(h.fetch.of("twilio.sms").length, 1);
});
