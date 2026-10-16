import { test } from "node:test";
import assert from "node:assert/strict";
import { smsBody, smsClean, replyIntent, SMS_MAX } from "../src/sms.js";
import { cpLength } from "../src/util.js";
import { setup, sign, TOKENS, OWNER, FROM, ORIGIN, AUTH, START, MIN } from "./helpers.js";

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
const MSG = (t) => `<Response><Message>${t}</Message></Response>`;
const HINT = MSG("Reply 1 to acknowledge, 2 to snooze 10 min, or 9 if done.");

// ------------------------------------------------------------ outbound bodies

test("SMS bodies: prefix per client, URGENT + reply hint with a call, missed-call fallback", () => {
  const call = { attempts: 1 };
  assert.equal(smsBody({ client: "bus", title: "T", message: "M", call: null }, "alert", 10), "[Agent Bus] T\nM");
  assert.equal(
    smsBody({ client: "bus", title: "T", message: "M", call }, "alert", 10),
    "[Agent Bus] URGENT: T\nM\nReply 1 = got it, 2 = snooze 10 min, 9 = done.",
  );
  assert.equal(smsBody({ client: "planner", title: "T", message: "M", call }, "fallback", 10), "[Planner] Missed urgent call: T\nM\nReply 1 = got it, 9 = done.");
  assert.equal(smsBody({ client: "homelab", title: "T", message: "M", call: null }, "alert", 10), "[homelab] T\nM");
  assert.equal(smsBody({ client: "bus", title: "T", message: "M", call }, "alert", 5), "[Agent Bus] URGENT: T\nM\nReply 1 = got it, 2 = snooze 5 min, 9 = done.");
  // A message that cleans to nothing leaves no empty line.
  assert.equal(smsBody({ client: "bus", title: "T", message: "🚀🔥", call: null }, "alert", 10), "[Agent Bus] T");
});

test("SMS text is GSM-7 friendly: ASCII punctuation, no emoji, folded accents, one line", () => {
  assert.equal(smsClean("“Deploy” — it’s ‘done’… ‐ – − • ·"), `"Deploy" - it's 'done'... - - - - -`);
  assert.equal(smsClean("🚀 Ship it 👍🏽 now ✅ 🇨🇦 1️⃣ 👨‍👩‍👧 ©™"), "Ship it now 1");
  assert.equal(smsClean("café naïve façade Ångström ñ ü à"), "café naive facade Ångström ñ ü à");
  assert.equal(smsClean("line one\r\n\tline   two\u00a0end\u200b!"), "line one line two end!");
  assert.equal(smsClean("[x](y) {a} ~b~ |c| €5 ^"), "[x](y) {a} ~b~ |c| €5 ^", "GSM extension characters stay");
  assert.equal(smsClean("日本"), "日本", "characters with no GSM form are kept");
});

test("SMS bodies stay within 600 characters: the message is cut, never the header or the reply hint", () => {
  const title = "T".repeat(150);
  const alert = { client: "bus", title, message: "word ".repeat(400), call: { attempts: 1 } };
  const body = smsBody(alert, "alert", 10);
  assert.equal(cpLength(body), SMS_MAX);
  const lines = body.split("\n");
  assert.equal(lines.length, 3);
  assert.equal(lines[0], `[Agent Bus] URGENT: ${title}`);
  assert.ok(lines[1].endsWith("..."));
  assert.equal(lines[2], "Reply 1 = got it, 2 = snooze 10 min, 9 = done.");
  const fallback = smsBody(alert, "fallback", 10);
  assert.ok(cpLength(fallback) <= SMS_MAX);
  assert.ok(fallback.endsWith("...\nReply 1 = got it, 9 = done."));
  // Emoji are stripped before measuring; short messages are untouched.
  const short = smsBody({ client: "bus", title: "Hi 👋", message: "Fine — ok", call: null }, "alert", 10);
  assert.equal(short, "[Agent Bus] Hi\nFine - ok");
});

test("outbound SMS through the gateway is cleaned and capped", async () => {
  const h = setup();
  await h.create({ channels: ["text", "call"], title: "🚨 Prod “down”", message: "x".repeat(1500) });
  const [body] = h.texts();
  assert.equal(cpLength(body), 600);
  assert.ok(body.startsWith('[Agent Bus] URGENT: Prod "down"\nxxx'));
  assert.ok(body.endsWith("...\nReply 1 = got it, 2 = snooze 10 min, 9 = done."));
});

test("reply words", () => {
  for (const w of ["1", "ok", "OK", "Ack", "got it", " Got  It! ", "y", "YES", "ok."]) assert.equal(replyIntent(w), "ack", w);
  for (const w of ["2", "snooze", "Snooze", " 2 "]) assert.equal(replyIntent(w), "snooze", w);
  for (const w of ["", "3", "12", "no", "okay then", "stop", "1 2"]) assert.equal(replyIntent(w), "other", w);
});

// ------------------------------------------------------------ inbound /twilio/sms

test("texts from other numbers (or with no OWNER_NUMBER) get an empty response and change nothing", async () => {
  const h = setup();
  const a = (await h.create()).json.alert;
  const before = JSON.stringify([...h.storage.map]);
  const r = await h.sms("1", "+15550001111");
  assert.equal(r.status, 200);
  assert.match(r.headers.get("Content-Type"), /text\/xml/);
  assert.equal(r.text, "<Response/>");
  assert.equal(JSON.stringify([...h.storage.map]), before, "no ack, no text counted");
  assert.equal((await h.get(a.id)).status, "calling");

  const noOwner = setup({ env: { OWNER_NUMBER: undefined } });
  assert.equal((await noOwner.sms("1", OWNER)).text, "<Response/>");
});

test("/twilio/sms needs a valid Twilio signature (403 otherwise)", async () => {
  const h = setup();
  const a = (await h.create()).json.alert;
  const before = JSON.stringify([...h.storage.map]);
  const params = { From: OWNER, To: FROM, Body: "1", MessageSid: "SMx" };
  for (const signature of [null, "", "bad", sign("wrong-token", `${ORIGIN}/twilio/sms`, params)]) {
    assert.equal((await h.twilio("/twilio/sms", params, { signature })).status, 403);
  }
  const good = sign(AUTH, `${ORIGIN}/twilio/sms`, params);
  assert.equal((await h.twilio("/twilio/sms", { ...params, Body: "2" }, { signature: good })).status, 403, "tampered body");
  assert.equal(JSON.stringify([...h.storage.map]), before);
  assert.equal((await h.twilio("/twilio/sms", params, { signature: good })).status, 200);
  assert.equal((await h.get(a.id)).status, "acked");
  const unsigned = setup({ env: { TWILIO_AUTH_TOKEN: undefined } });
  assert.equal((await unsigned.sms("1")).status, 403);
});

test("owner with nothing open: 'No open alerts.' (any body)", async () => {
  const h = setup();
  assert.equal((await h.sms("1")).text, MSG("No open alerts."));
  assert.equal((await h.sms("hello")).text, MSG("No open alerts."));
  // Text-only, acked, canceled and failed alerts are never answered.
  await h.create({ channels: ["text"] });
  const a = (await h.create()).json.alert;
  await h.api("POST", `/v1/alerts/${a.id}/cancel`);
  await h.status(a.id, 1, "completed");
  h.fetch.handlers.twilioCreate = () => new Response(JSON.stringify({ code: 21211, message: "bad To" }), { status: 400 });
  const f = (await h.create()).json.alert;
  assert.equal(f.status, "failed");
  assert.equal((await h.sms("1")).text, MSG("No open alerts."));
  assert.equal((await h.storage.get("cnt:texts")).length, 4, "replies count toward MAX_TEXTS_PER_DAY (plus the text-only alert)");
});

test("1 acknowledges a ringing call: ends it, cancels the retry, emits alert.acked via sms", async () => {
  const h = setup();
  const a = (await h.create({ title: "Pecunia & prod <down>", fallback_text: false })).json.alert;
  const r = await h.sms("Got it");
  assert.equal(r.text, MSG("Got it: Pecunia &amp; prod &lt;down&gt;"));
  const cur = await h.get(a.id);
  assert.equal(cur.status, "acked");
  assert.equal(cur.call.via, "sms");
  assert.equal(cur.call.digit, null, "a text is not a key press");
  const [end] = h.fetch.of("twilio.update");
  assert.match(end.url, new RegExp(`/Calls/${a.call.sid}\\.json$`));
  assert.equal(end.form.get("Status"), "completed");
  const u = (await h.updates()).updates.at(-1);
  assert.deepEqual(
    { type: u.type, status: u.status, via: u.via, digit: u.digit },
    { type: "alert.acked", status: "acked", via: "sms", digit: undefined },
  );
  await h.status(a.id, 1, "completed");
  await h.advance(30 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 1, "no retry");
  assert.equal((await h.sms("1")).text, MSG("No open alerts."));
});

test("2 snoozes a pending alert (replacing its retry); past MAX_SNOOZES it counts as 1", async () => {
  const h = setup({ env: { MAX_SNOOZES: "1" } });
  const a = (await h.create({ title: "Server down" })).json.alert;
  await h.status(a.id, 1, "no-answer");
  assert.equal((await h.get(a.id)).status, "waiting_retry");
  h.clock.t += MIN;

  assert.equal((await h.sms("snooze")).text, MSG("Snoozed 10 min: Server down"));
  let cur = await h.get(a.id);
  assert.equal(cur.status, "snoozed");
  assert.equal(cur.call.snoozes, 1);
  assert.equal(cur.call.via, "sms");
  assert.equal(cur.call.digit, null);
  assert.equal(cur.call.next_call_at, iso(START + 11 * MIN));
  const u = (await h.updates()).updates.at(-1);
  assert.deepEqual({ type: u.type, via: u.via, digit: u.digit, next: u.next_call_at }, {
    type: "alert.snoozed",
    via: "sms",
    digit: undefined,
    next: iso(START + 11 * MIN),
  });
  await h.advance(3 * MIN);
  assert.equal(h.fetch.of("twilio.create").length, 1, "the retry was replaced by the snooze");
  await h.advance(7 * MIN);
  assert.equal((await h.get(a.id)).status, "calling");
  assert.equal(h.fetch.of("twilio.create").length, 2);

  // Snoozes used up: 2 acknowledges.
  assert.equal((await h.sms("2")).text, MSG("Got it: Server down"));
  cur = await h.get(a.id);
  assert.equal(cur.status, "acked");
  assert.equal(cur.call.via, "sms");
  assert.equal(h.fetch.of("twilio.update").length, 1, "the ringing snoozed call was ended");
});

test("anything else gets the hint and changes nothing", async () => {
  const h = setup();
  const a = (await h.create()).json.alert;
  for (const body of ["hello?", "3", "", "STOP please"]) {
    assert.equal((await h.sms(body)).text, HINT, JSON.stringify(body));
  }
  assert.equal((await h.get(a.id)).status, "calling");
  assert.deepEqual(await h.types(), ["call.dialing"]);
});

test("a late reply acknowledges an unanswered alert (within 24 h); the newest call alert of any client wins", async () => {
  const h = setup();
  const bus = (await h.create({ title: "Older", retry: false, fallback_text: true })).json.alert;
  await h.status(bus.id, 1, "no-answer");
  assert.equal((await h.get(bus.id)).status, "unanswered");
  assert.equal(h.texts()[0], "[Agent Bus] Missed urgent call: Older\nHealth check failing for 6 minutes.\nReply 1 = got it, 9 = done.");
  h.clock.t += MIN;
  const pl = (await h.create({ title: "Newest", retry: false, fallback_text: false }, { token: TOKENS.planner })).json.alert;
  await h.status(pl.id, 1, "busy");
  h.clock.t += 2 * 60 * MIN;

  assert.equal((await h.sms("yes")).text, MSG("Got it: Newest"));
  const acked = await h.get(pl.id, TOKENS.planner);
  assert.equal(acked.status, "acked");
  assert.equal(acked.call.via, "sms");
  const u = (await h.updates({ token: TOKENS.planner })).updates.at(-1);
  assert.deepEqual([u.type, u.status, u.via], ["alert.acked", "acked", "sms"]);
  assert.equal(h.fetch.of("twilio.update").length, 0, "nothing was ringing");

  // 2 on an unanswered alert counts as 1.
  assert.equal((await h.sms("2")).text, MSG("Got it: Older"));
  assert.equal((await h.get(bus.id)).status, "acked");
  assert.equal((await h.sms("1")).text, MSG("No open alerts."));

  const old = setup();
  const o = (await old.create({ retry: false, fallback_text: false })).json.alert;
  await old.status(o.id, 1, "no-answer");
  old.clock.t += 24 * 60 * MIN + 1000;
  assert.equal((await old.sms("1")).text, MSG("No open alerts."));
  assert.equal((await old.get(o.id)).status, "unanswered");
});

test("replies count toward MAX_TEXTS_PER_DAY: over the cap the action still happens, with no reply", async () => {
  const h = setup({ env: { MAX_TEXTS_PER_DAY: "1" } });
  const a = (await h.create({ fallback_text: false })).json.alert;
  assert.equal((await h.sms("what")).text, HINT);
  assert.equal((await h.storage.get("cnt:texts")).length, 1);
  assert.equal((await h.sms("1")).text, "<Response/>");
  assert.equal((await h.get(a.id)).status, "acked");
  assert.equal((await h.storage.get("cnt:texts")).length, 1);
  // The cap also blocks new text alerts until the window rolls.
  assert.equal((await h.create({ channels: ["text"] })).status, 429);
  h.clock.t += 24 * 60 * MIN;
  assert.equal((await h.sms("1")).text, MSG("No open alerts."));
});
