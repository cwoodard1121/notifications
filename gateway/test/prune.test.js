import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, START, MIN } from "./helpers.js";

const DAY = 24 * 60 * MIN;

test("alerts, ref index and updates older than 7 days are pruned", async () => {
  const h = setup();
  const old = (await h.create({ ref: "old-ref", channels: ["text"] })).json.alert;
  const call = (await h.create({ ref: "old-call" })).json.alert;
  await h.press(call.id, 1, "1");
  await h.status(call.id, 1, "completed");
  assert.equal(await h.storage.get("prune_at"), START + 6 * 60 * MIN);

  await h.advance(5 * DAY);
  const recent = (await h.create({ ref: "recent", channels: ["text"] })).json.alert;
  await h.advance(2 * DAY + 7 * 60 * MIN);

  assert.equal((await h.api("GET", `/v1/alerts/${old.id}`)).status, 404);
  assert.equal((await h.api("GET", `/v1/alerts/${call.id}`)).status, 404);
  assert.equal((await h.get(recent.id)).status, "sent");
  assert.deepEqual(h.storage.keys("ref:"), ["ref:bus:recent"]);
  const ups = (await h.updates()).updates;
  assert.deepEqual(ups.map((u) => u.alert_id), [recent.id]);

  // A pruned ref can be used again.
  const again = await h.create({ ref: "old-ref", channels: ["text"] });
  assert.equal(again.status, 201);
  assert.notEqual(again.json.alert.id, old.id);

  // Once everything is gone, the prune alarm stops.
  await h.advance(8 * DAY);
  assert.deepEqual(h.storage.keys("alert:"), []);
  assert.deepEqual(h.storage.keys("upd:"), []);
  assert.equal(await h.storage.get("prune_at"), undefined);
  assert.equal(h.storage.alarm, null);
});

test("a text-only workload still arms the prune alarm", async () => {
  const h = setup();
  const only = (await h.create({ ref: "text-only", channels: ["text"] })).json.alert;
  assert.equal(h.storage.alarm, START + 6 * 60 * MIN);
  await h.advance(7 * DAY + 6 * 60 * MIN);
  assert.equal((await h.api("GET", `/v1/alerts/${only.id}`)).status, 404);
  assert.deepEqual(h.storage.keys("alert:"), []);
  assert.equal(h.storage.alarm, null);
});
