import { test } from "node:test";
import assert from "node:assert/strict";
import { setup } from "./helpers.js";

const good = { ref: "r-1", title: "T", message: "M", channels: ["call"] };

test("invalid create bodies are 400 invalid and store nothing", async () => {
  const h = setup();
  const cases = [
    { ...good, ref: undefined },
    { ...good, ref: "" },
    { ...good, ref: "x".repeat(121) },
    { ...good, ref: "has space" },
    { ...good, ref: "slash/no" },
    { ...good, ref: 12 },
    { ...good, title: undefined },
    { ...good, title: "" },
    { ...good, title: "   " },
    { ...good, title: "t".repeat(151) },
    { ...good, message: undefined },
    { ...good, message: "" },
    { ...good, message: "m".repeat(1501) },
    { ...good, channels: undefined },
    { ...good, channels: [] },
    { ...good, channels: "call" },
    { ...good, channels: ["sms"] },
    { ...good, channels: ["call", "call"] },
    { ...good, retry: "yes" },
    { ...good, fallback_text: 1 },
  ];
  for (const body of cases) {
    const r = await h.api("POST", "/v1/alerts", { body });
    assert.equal(r.status, 400, JSON.stringify(body));
    assert.equal(r.json.error, "invalid");
    assert.equal(typeof r.json.detail, "string");
  }
  for (const raw of ["not json", "[1,2]", "null", "", "42"]) {
    const r = await h.api("POST", "/v1/alerts", { raw, headers: { "Content-Type": "application/json" } });
    assert.equal(r.status, 400, raw);
    assert.equal(r.json.error, "invalid");
  }
  const big = await h.api("POST", "/v1/alerts", { body: { ...good, pad: "x".repeat(17000) } });
  assert.equal(big.status, 400);
  assert.match(big.json.detail, /16384/);

  assert.deepEqual(h.storage.keys("alert:"), []);
  assert.deepEqual(h.storage.keys("ref:"), []);
  assert.equal(h.fetch.calls.length, 0);
});

test("boundary values are accepted; lengths count code points", async () => {
  const h = setup();
  const r = await h.api("POST", "/v1/alerts", {
    body: {
      ref: "A-z_0.9:@#-" + "x".repeat(109),
      title: "🚨".repeat(150),
      message: "é".repeat(1500),
      channels: ["text", "call"],
      retry: null,
      fallback_text: false,
    },
  });
  assert.equal(r.status, 201, r.text);
  assert.deepEqual(r.json.alert.channels, ["text", "call"]);
  assert.equal(r.json.alert.call.max_attempts, 2);
});

test("updates query params are validated", async () => {
  const h = setup();
  for (const q of ["?after=-1", "?after=abc", "?limit=0", "?limit=201", "?limit=x"]) {
    const r = await h.api("GET", `/v1/updates${q}`);
    assert.equal(r.status, 400, q);
    assert.equal(r.json.error, "invalid");
  }
  assert.equal((await h.api("GET", "/v1/updates")).status, 200);
});
