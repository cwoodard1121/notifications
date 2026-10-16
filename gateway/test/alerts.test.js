import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, TOKENS, OWNER, FROM, AUTH, MIN } from "./helpers.js";

test("same (client, ref) returns the existing alert with duplicate:true and never dials again", async () => {
  const h = setup({ env: { CLIENT_DAILY_CALLS: "1" } });
  const first = await h.create({ ref: "bus-call-3f9c" });
  assert.equal(first.status, 201);
  const again = await h.create({ ref: "bus-call-3f9c", title: "different" });
  assert.equal(again.status, 200, "duplicate wins over the daily cap");
  assert.equal(again.json.duplicate, true);
  assert.equal(again.json.alert.id, first.json.alert.id);
  assert.equal(again.json.alert.title, "Pecunia prod is down");
  assert.equal(h.fetch.of("twilio.create").length, 1);
  assert.equal(first.json.duplicate, undefined);

  const other = await h.create({ ref: "bus-call-3f9c" }, { token: TOKENS.planner });
  assert.equal(other.status, 201, "refs are per client");
  assert.notEqual(other.json.alert.id, first.json.alert.id);
  assert.equal(other.json.alert.client, "planner");
});

test("503 not_configured (detail twilio) when Twilio has no secrets, for either channel; nothing stored", async () => {
  for (const missing of ["OWNER_NUMBER", "TWILIO_FROM", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"]) {
    const h = setup({ env: { [missing]: undefined } });
    for (const channels of [["call"], ["text"], ["text", "call"]]) {
      const r = await h.create({ channels });
      assert.equal(r.status, 503, `${missing} ${channels}`);
      assert.deepEqual(r.json, { error: "not_configured", detail: "twilio" });
    }
    assert.deepEqual(h.storage.keys("alert:"), []);
    assert.deepEqual(h.storage.keys("ref:"), []);
    assert.equal(h.fetch.calls.length, 0);
  }
  const dry = setup({ env: { DRY_RUN: "1", TWILIO_ACCOUNT_SID: undefined } });
  assert.equal((await dry.create({ channels: ["text", "call"] })).status, 201);
  assert.equal(dry.fetch.calls.length, 0);
});

test("text-only alert: one SMS from TWILIO_FROM to OWNER_NUMBER, labelled per client", async () => {
  const h = setup();
  const r = await h.create({ channels: ["text"], title: "Build green", message: "All 42 tests pass." });
  assert.equal(r.status, 201);
  const a = r.json.alert;
  assert.equal(a.status, "sent");
  assert.equal(a.call, null);
  assert.deepEqual(a.text, { status: "sent", message_id: "SM" + "0".repeat(29) + "801" });
  const [sms] = h.fetch.of("twilio.sms");
  assert.equal(sms.url, `https://api.twilio.com/2010-04-01/Accounts/AC${"1".repeat(32)}/Messages.json`);
  assert.equal(sms.method, "POST");
  assert.equal(sms.headers["Content-Type"], "application/x-www-form-urlencoded");
  assert.deepEqual(Object.fromEntries(sms.form), { To: OWNER, From: FROM, Body: "[Agent Bus] Build green\nAll 42 tests pass." });
  const ups = (await h.updates()).updates;
  assert.deepEqual(
    ups.map((u) => ({ type: u.type, status: u.status, attempt: u.attempt, message_id: u.message_id })),
    [{ type: "text.sent", status: "sent", attempt: 0, message_id: "SM" + "0".repeat(29) + "801" }],
  );
  assert.equal(h.storage.keys("open").length === 0 || (await h.storage.get("open")).length === 0, true);

  await h.create({ channels: ["text"], title: "Overdue: essay", message: "Due 3 PM" }, { token: TOKENS.planner });
  h.env.TOKEN_HOMELAB = "gw_homelab_" + "c".repeat(43);
  await h.create({ channels: ["text"], title: "Disk 91%", message: "nas1" }, { token: h.env.TOKEN_HOMELAB });
  assert.deepEqual(h.texts().slice(1), ["[Planner] Overdue: essay\nDue 3 PM", "[homelab] Disk 91%\nnas1"]);
});

test("text-only alert: text_failed with Twilio's code and message, without secrets", async () => {
  const h = setup();
  h.fetch.handlers.twilioSms = () =>
    new Response(
      JSON.stringify({ code: 21610, message: `Attempt to send to unsubscribed recipient ${OWNER} (auth ${AUTH})`, status: 400 }),
      { status: 400 },
    );
  const a = (await h.create({ channels: ["text"] })).json.alert;
  assert.equal(a.status, "text_failed");
  assert.equal(a.text.status, "failed");
  assert.equal(a.text.message_id, null);
  assert.equal(a.text.detail, "Twilio 400: Attempt to send to unsubscribed recipient [redacted] (auth [redacted]) (code 21610)");
  const u = (await h.updates()).updates[0];
  assert.equal(u.type, "text.failed");
  assert.equal(u.status, "text_failed");
  assert.equal(u.detail, a.text.detail);

  // 5xx and network errors are text_failed too; the client may retry with a new ref.
  h.fetch.handlers.twilioSms = () => new Response("upstream", { status: 503 });
  const b = (await h.create({ channels: ["text"] })).json.alert;
  assert.equal(b.status, "text_failed");
  assert.equal(b.text.detail, "Twilio 503: HTTP 503");
  h.fetch.handlers.twilioSms = () => {
    throw new TypeError("network down");
  };
  const c = (await h.create({ channels: ["text"] })).json.alert;
  assert.equal(c.status, "text_failed");
  assert.equal(c.text.detail, "Twilio request failed: TypeError");
});

test("text + call: the SMS (URGENT, with the reply hint) goes out first, then the call dials", async () => {
  const h = setup();
  const a = (await h.create({ channels: ["text", "call"], title: "Prod down", message: "Rollback needs you." })).json.alert;
  assert.equal(a.status, "calling");
  assert.equal(a.text.status, "sent");
  assert.deepEqual(await h.types(), ["text.sent", "call.dialing"]);
  assert.deepEqual(h.texts(), ["[Agent Bus] URGENT: Prod down\nRollback needs you.\nReply 1 = got it, 2 = snooze 10 min, 9 = done."]);
  assert.deepEqual(h.fetch.calls.map((c) => c.kind), ["twilio.sms", "twilio.create"]);

  // An SMS failure does not stop the call.
  h.fetch.handlers.twilioSms = () => new Response(JSON.stringify({ code: 21408, message: "Permission denied" }), { status: 400 });
  const b = (await h.create({ channels: ["text", "call"] })).json.alert;
  assert.equal(b.text.status, "failed");
  assert.equal(b.status, "queued", "line busy with the first call, but the alert is live");
});

test("Twilio 4xx -> failed (final) with Twilio's message and no secrets; the line stays free", async () => {
  const h = setup();
  h.fetch.handlers.twilioCreate = () =>
    new Response(
      JSON.stringify({ code: 21211, message: `The 'To' number ${OWNER} is not a valid phone number. auth=${AUTH}`, status: 400 }),
      { status: 400 },
    );
  const a = (await h.create()).json.alert;
  assert.equal(a.status, "failed");
  assert.equal(a.call.attempts, 1);
  const u = (await h.updates()).updates.at(-1);
  assert.equal(u.type, "alert.failed");
  assert.equal(u.status, "failed");
  assert.match(u.detail, /^Twilio 400: The 'To' number \[redacted\] is not a valid phone number\. auth=\[redacted\] \(code 21211\)$/);
  assert.ok(!u.detail.includes(OWNER) && !u.detail.includes(AUTH));
  await h.advance(30 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 1, "no retry after a refusal");
  assert.equal(h.fetch.of("twilio.sms").length, 0);

  h.fetch.handlers.twilioCreate = () => new Response(JSON.stringify({ sid: "CAok" }), { status: 201 });
  assert.equal((await h.create()).json.alert.status, "calling", "line was not held");
});

test("Twilio 5xx or network error -> unacknowledged attempt -> retry", async () => {
  const h = setup();
  h.fetch.handlers.twilioCreate = () => new Response("upstream", { status: 503 });
  const a = (await h.create()).json.alert;
  assert.equal(a.status, "waiting_retry");
  assert.equal(a.call.attempts, 1);
  assert.equal(a.call.last_call_status, "failed");
  const ups = (await h.updates()).updates;
  assert.deepEqual(ups.map((u) => u.type), ["call.ended", "alert.retry_scheduled"]);
  assert.equal(ups[0].call_status, "failed");
  assert.match(ups[0].detail, /^Twilio 503/);

  h.fetch.handlers.twilioCreate = () => {
    throw new TypeError("fetch failed");
  };
  await h.advance(3 * MIN);
  const b = await h.get(a.id);
  assert.equal(b.status, "unanswered");
  assert.equal(b.call.attempts, 2);
  assert.equal(h.fetch.of("twilio.sms").length, 1, "fallback text");
});

test("ack and cancel endpoints", async () => {
  const h = setup();
  // ack while the call is ringing: ends it through Twilio.
  const a = (await h.create()).json.alert;
  let r = await h.api("POST", `/v1/alerts/${a.id}/ack`);
  assert.equal(r.status, 200);
  assert.equal(r.json.alert.status, "acked");
  assert.equal(r.json.alert.call.digit, null);
  assert.equal(r.json.alert.call.via, "client");
  const [end] = h.fetch.of("twilio.update");
  assert.equal(end.url, `https://api.twilio.com/2010-04-01/Accounts/AC${"1".repeat(32)}/Calls/${a.call.sid}.json`);
  assert.equal(end.form.get("Status"), "completed");
  const ackUpdate = (await h.updates()).updates.at(-1);
  assert.equal(ackUpdate.type, "alert.acked");
  assert.equal("digit" in ackUpdate, false);
  assert.equal(ackUpdate.via, "client");
  // idempotent; final alerts are returned unchanged
  const again = await h.api("POST", `/v1/alerts/${a.id}/ack`);
  assert.deepEqual(again.json.alert, r.json.alert);
  const cancelAfter = await h.api("POST", `/v1/alerts/${a.id}/cancel`);
  assert.equal(cancelAfter.json.alert.status, "acked");
  await h.status(a.id, 1, "canceled");
  assert.equal((await h.get(a.id)).call.last_call_status, "canceled");

  // cancel while waiting for the retry: no REST call, no retry.
  const b = (await h.create()).json.alert;
  await h.status(b.id, 1, "no-answer");
  r = await h.api("POST", `/v1/alerts/${b.id}/cancel`);
  assert.equal(r.json.alert.status, "canceled");
  assert.equal(r.json.alert.call.next_call_at, null);
  assert.equal(h.fetch.of("twilio.update").length, 1);
  await h.advance(10 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 2);
  const canceled = (await h.updates()).updates.at(-1);
  assert.equal(canceled.type, "alert.canceled");
  assert.equal("via" in canceled, false);

  // cancel while queued: never dialed.
  const c = (await h.create()).json.alert;
  const d = (await h.create()).json.alert;
  assert.equal(d.status, "queued");
  await h.api("POST", `/v1/alerts/${d.id}/cancel`);
  await h.status(c.id, 1, "completed");
  assert.equal((await h.get(d.id)).status, "canceled");
  assert.equal((await h.get(d.id)).call.attempts, 0);

  // text-only alerts are already final.
  const t = (await h.create({ channels: ["text"] })).json.alert;
  r = await h.api("POST", `/v1/alerts/${t.id}/ack`);
  assert.equal(r.json.alert.status, "sent");
});

test("updates feed: paging, next, global seq with gaps", async () => {
  const h = setup();
  for (let i = 0; i < 3; i++) {
    await h.create({ channels: ["text"] });
    await h.create({ channels: ["text"] }, { token: TOKENS.planner });
  }
  const p1 = await h.updates({ limit: 2 });
  assert.deepEqual(p1.updates.map((u) => u.seq), [1, 3]);
  assert.equal(p1.next, 3);
  const p2 = await h.updates({ after: p1.next, limit: 2 });
  assert.deepEqual(p2.updates.map((u) => u.seq), [5]);
  assert.equal(p2.next, 5);
  const p3 = await h.updates({ after: p2.next });
  assert.deepEqual(p3, { updates: [], next: 5 });
  const planner = await h.updates({ token: TOKENS.planner });
  assert.deepEqual(planner.updates.map((u) => u.seq), [2, 4, 6]);
  assert.equal(planner.next, 6);
  const all = (await h.api("GET", "/v1/updates")).json;
  assert.equal(all.updates.length, 3);
  assert.match(all.updates[0].at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
});
