import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, TOKENS, ORIGIN } from "./helpers.js";
import { matchToken } from "../src/auth.js";
import { frontDoor } from "../src/core.js";

test("health needs no auth and reveals only booleans", async () => {
  const h = setup();
  const r = await h.api("GET", "/v1/health", { token: null });
  assert.equal(r.status, 200);
  assert.deepEqual(r.json, {
    ok: true,
    service: "agent-bus-phone",
    version: 1,
    dry_run: false,
    twilio: true,
    text_channel: "sms",
  });
  const bare = setup({
    env: { TWILIO_ACCOUNT_SID: undefined, DRY_RUN: "1" },
  });
  const r2 = await bare.api("GET", "/v1/health", { token: null });
  assert.deepEqual(
    { dry_run: r2.json.dry_run, twilio: r2.json.twilio, text_channel: r2.json.text_channel },
    { dry_run: true, twilio: false, text_channel: "sms" },
  );
});

test("missing, wrong, malformed and short tokens are 401", async () => {
  const h = setup({ env: { TOKEN_SHORT: "short-token-under-32-chars" } });
  const cases = [
    { token: null },
    { token: "gw_bus_" + "z".repeat(43) },
    { token: TOKENS.bus + "x" },
    { token: TOKENS.bus.slice(0, -1) },
    { token: "short-token-under-32-chars" },
    { token: null, headers: { Authorization: `Basic ${TOKENS.bus}` } },
    { token: null, headers: { Authorization: `Bearer` } },
  ];
  for (const c of cases) {
    const r = await h.api("GET", "/v1/updates", c);
    assert.equal(r.status, 401, JSON.stringify(c));
    assert.deepEqual(r.json, { error: "unauthorized" });
  }
  const ok = await h.api("GET", "/v1/updates", { token: null, headers: { Authorization: `bearer ${TOKENS.bus}` } });
  assert.equal(ok.status, 200);
});

test("matchToken maps TOKEN_<CLIENT> to the lowercase client id", async () => {
  const env = {
    TOKEN_BUS: TOKENS.bus,
    TOKEN_PLANNER: TOKENS.planner,
    TOKEN_MY_APP2: "c".repeat(40),
    token_lower: "d".repeat(40),
    TOKEN_TOO_SHORT: "e".repeat(31),
    OTHER: "f".repeat(40),
  };
  assert.equal(await matchToken(TOKENS.bus, env), "bus");
  assert.equal(await matchToken(TOKENS.planner, env), "planner");
  assert.equal(await matchToken("c".repeat(40), env), "my_app2");
  assert.equal(await matchToken("d".repeat(40), env), null);
  assert.equal(await matchToken("e".repeat(31), env), null);
  assert.equal(await matchToken("f".repeat(40), env), null);
  assert.equal(await matchToken("", env), null);
});

test("a client sees and controls only its own alerts", async () => {
  const h = setup();
  const created = await h.create({ channels: ["call"] });
  assert.equal(created.status, 201);
  const id = created.json.alert.id;

  for (const [method, path] of [
    ["GET", `/v1/alerts/${id}`],
    ["POST", `/v1/alerts/${id}/ack`],
    ["POST", `/v1/alerts/${id}/cancel`],
  ]) {
    const r = await h.api(method, path, { token: TOKENS.planner });
    assert.equal(r.status, 404, `${method} ${path}`);
    assert.equal(r.json.error, "not_found");
  }
  assert.equal((await h.get(id)).status, "calling");
  const theirs = await h.updates({ token: TOKENS.planner });
  assert.deepEqual(theirs.updates, []);
  const mine = await h.updates();
  assert.ok(mine.updates.length >= 1);
  assert.ok(mine.updates.every((u) => u.alert_id === id));
});

test("unknown routes are 404; wrong methods are 404", async () => {
  const h = setup();
  assert.equal((await h.api("GET", "/nope", { token: null })).status, 404);
  assert.equal((await h.api("GET", "/v1/nope")).status, 404);
  assert.equal((await h.api("POST", "/v1/health", { token: null })).status, 404);
  assert.equal((await h.api("GET", "/v1/alerts")).status, 404);
  assert.equal((await h.api("GET", "/v1/alerts/al_0000000000000000")).status, 404);
  assert.equal((await h.api("GET", "/v1/alerts/not-an-id")).status, 404);
  assert.equal((await h.api("GET", "/twilio/voice", { token: null })).status, 404);
  assert.equal((await h.api("POST", "/twilio/other", { token: null })).status, 404);
  // Worker front door: stateless routes answered without the Durable Object.
  assert.equal(frontDoor(new Request(`${ORIGIN}/`), {}).status, 404);
  assert.equal(frontDoor(new Request(`${ORIGIN}/v1/health`), {}).status, 200);
  assert.equal(frontDoor(new Request(`${ORIGIN}/v1/alerts`, { method: "POST" }), {}), null);
  assert.equal(frontDoor(new Request(`${ORIGIN}/twilio/status`, { method: "POST" }), {}), null);
});
