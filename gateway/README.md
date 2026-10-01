# agent-bus-phone (phone gateway Worker)

A Cloudflare Worker with one SQLite-backed Durable Object. It is the only thing that can ring or text Cameron's phone. It holds the Twilio keys, places TTS calls with a keypad menu (1 = acknowledge, 2 = snooze, 3 = repeat), sends SMS texts from the same Twilio number, takes SMS replies (1 = got it, 2 = snooze), and owns the retry, snooze, and queue timers. Clients (the Agent Bus on the PC, the planner server) create alerts with a bearer token and read the outcome back.

The API contract is [../docs/PHONE-GATEWAY.md](../docs/PHONE-GATEWAY.md). Read it before changing behaviour. The "Implementation notes" at the end of that file cover the details the contract leaves open.

## Layout

| File | What it does |
|---|---|
| `src/index.js` | Cloudflare glue only: Worker `fetch`, the `PhoneGateway` Durable Object class, and `alarm()`. |
| `src/core.js` | Routing, validation, the alert state machine, limits, timers, and pruning. Storage, env, clock and fetch are injected. |
| `src/auth.js` | `TOKEN_<CLIENT>` bearer auth with a constant-time compare. |
| `src/twilio.js` | Twilio REST (Calls, Messages) and `X-Twilio-Signature` validation (WebCrypto HMAC-SHA1). |
| `src/sms.js` | SMS bodies (GSM-7 cleanup, `[Agent Bus]`/`[Planner]` prefix, 600-character cap) and reply parsing. |
| `src/speech.js`, `src/twiml.js` | Speech cleanup, XML escaping, TwiML builders. |
| `test/*.test.js` | `node --test` suites using fake storage, a fake clock, and a recording fake fetch. |

Everything except `src/index.js` runs in plain Node with no npm dependencies.

## Tests

```
npm test            # node --test test/*.test.js
npm run check       # wrangler deploy --dry-run --outdir dist (bundles + validates config, no login)
```

## Local dev

```
cp .dev.vars.example .dev.vars     # fake values, DRY_RUN=1 (git-ignored)
npx wrangler dev --ip 127.0.0.1 --port 8787
```

With `DRY_RUN=1`, nothing ever reaches Twilio. Drive calls and SMS replies with `POST /v1/alerts/{id}/simulate` (`answer`, `digits`, `status`, `advance`, `sms`) as described in the contract. After a simulated key press, send `{"event":"status","call_status":"completed"}` the way Twilio would. Until then the single phone line stays busy, and other calls stay `queued`, for up to 10 minutes.

To change a var for one session, pass `--var RETRY_MINUTES:1`.

Windows note: local Durable Object state lives under `.wrangler/state/v3/do/agent-bus-phone-PhoneGateway/<64-hex>.sqlite`. If the checkout path is long, that path can pass the 260-character MAX_PATH limit, and every DO request then fails with `internal error; reference = …`. Use `--persist-to C:/short/path` in that case.

## Deploy

`python notify.py setup gateway` on the PC deploys the Worker (`npx wrangler deploy`), creates `TOKEN_BUS`, and saves `gateway.url` and `gateway.token`. `python notify.py setup twilio` uploads the Twilio secrets and points the number's Voice URL at `/twilio/incoming` and its Messaging URL at `/twilio/sms`; `gateway add-client <name>` adds client tokens. `python notify.py setup phone` runs gateway + twilio and sends a test SMS. See the contract for details.

Setting a secret by hand (run in `gateway/`; the value is prompted for or read from stdin, never put on the command line):

```
npx wrangler secret put TWILIO_ACCOUNT_SID
npx wrangler secret put TWILIO_AUTH_TOKEN
npx wrangler secret put TWILIO_FROM
npx wrangler secret put OWNER_NUMBER
npx wrangler secret put TOKEN_BUS
npx wrangler secret put TOKEN_PLANNER
```

`wrangler.jsonc` holds only the non-secret vars. Never put numbers or tokens in it.
