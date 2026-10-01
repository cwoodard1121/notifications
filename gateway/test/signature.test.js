import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { twilioSignature, validateTwilioSignature } from "../src/twilio.js";
import { setup, sign, AUTH } from "./helpers.js";

test("matches Twilio's published example vector", async () => {
  // https://www.twilio.com/docs/usage/security (Validating Signatures)
  const params = new URLSearchParams({
    CallSid: "CA1234567890ABCDE",
    Caller: "+14158675310",
    Digits: "1234",
    From: "+14158675310",
    To: "+18005551212",
  });
  const url = "https://example.com/myapp.php?foo=1&bar=2";
  assert.equal(await twilioSignature("12345", url, params), "L/OH5YylLD5NRKLltdqwSvS0BnU=");
  assert.equal(await validateTwilioSignature("12345", url, params, "L/OH5YylLD5NRKLltdqwSvS0BnU="), true);
  assert.equal(await validateTwilioSignature("12345", url, params, "L/OH5YylLD5NRKLltdqwSvS0BnV="), false);
});

test("matches an independent node:crypto HMAC-SHA1 over url + sorted params", async () => {
  const url = "https://gw.example.workers.dev/twilio/gather?alert=al_0123456789abcdef&attempt=2";
  const params = { Digits: "1", CallSid: "CAabc", From: "+15195550199", AccountSid: "AC1", Caller: "a b&c=d" };
  const sorted = Object.keys(params).sort();
  const data = url + sorted.map((k) => k + params[k]).join("");
  const expected = createHmac("sha1", "secret-token").update(data).digest("base64");
  // Order of the form body must not matter.
  const shuffled = new URLSearchParams([...Object.entries(params)].reverse());
  assert.equal(await twilioSignature("secret-token", url, shuffled), expected);
  assert.equal(await validateTwilioSignature("secret-token", url, shuffled, expected), true);
  assert.equal(await validateTwilioSignature("other-token", url, shuffled, expected), false);
  assert.equal(await validateTwilioSignature("", url, shuffled, expected), false);
  assert.equal(await validateTwilioSignature("secret-token", url, shuffled, null), false);
  // The URL is part of the signature, including the query string.
  assert.equal(await validateTwilioSignature("secret-token", url.replace("attempt=2", "attempt=3"), shuffled, expected), false);
});

test("webhooks with a missing or wrong signature are 403 and change nothing", async () => {
  const h = setup();
  const { json } = await h.create();
  const id = json.alert.id;
  const before = JSON.stringify([...h.storage.map]);
  const url = `/twilio/gather?alert=${id}&attempt=1`;
  const params = { CallSid: "CAx", Digits: "1" };
  for (const signature of [null, "", "bm9wZQ==", sign("wrong-token", `https://agent-bus-phone.example.workers.dev${url}`, params)]) {
    const r = await h.twilio(url, params, { signature });
    assert.equal(r.status, 403);
  }
  // Signed for a different URL (attempt=2) but sent to attempt=1.
  const other = sign(AUTH, `https://agent-bus-phone.example.workers.dev/twilio/gather?alert=${id}&attempt=2`, params);
  assert.equal((await h.twilio(url, params, { signature: other })).status, 403);
  // Tampered body.
  const good = sign(AUTH, `https://agent-bus-phone.example.workers.dev${url}`, params);
  assert.equal((await h.twilio(url, { ...params, Digits: "2" }, { signature: good })).status, 403);
  for (const path of ["/twilio/voice?alert=x&attempt=1&pass=0", "/twilio/status?alert=x&attempt=1", "/twilio/incoming"]) {
    assert.equal((await h.twilio(path, { From: "+1" }, { signature: "bad" })).status, 403);
  }
  assert.equal(JSON.stringify([...h.storage.map]), before);
  assert.equal((await h.get(id)).status, "calling");
  // The correctly signed request is accepted.
  const ok = await h.twilio(url, params);
  assert.equal(ok.status, 200);
  assert.equal((await h.get(id)).status, "acked");
});

test("webhooks are rejected when TWILIO_AUTH_TOKEN is not set", async () => {
  const h = setup({ env: { TWILIO_AUTH_TOKEN: undefined } });
  const r = await h.twilio("/twilio/incoming", { From: "+15195550199" });
  assert.equal(r.status, 403);
});
