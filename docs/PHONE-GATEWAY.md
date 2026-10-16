# Phone gateway API (Cloudflare Worker `agent-bus-phone`)

The phone gateway is a Cloudflare Worker in `gateway/`. It holds the Twilio keys and is the only thing that can ring or text Cameron's phone. It places TTS calls with a keypad menu (1 = on it / acknowledge, 2 = snooze 10 minutes, 3 = repeat, 9 = already done), sends SMS texts from the same Twilio number, takes SMS replies (1 = got it, 2 = snooze, 9 = done), and owns the retry and snooze timers. Callers never wait on a call: they create an alert, then read its outcome.

Any number of **clients** can use it. Today there are two:

| Client | Who | Token secret |
|---|---|---|
| `bus` | the Agent Bus / Local Notify service on Cameron's PC (behind NAT, pulls outcomes) | `TOKEN_BUS` |
| `planner` | the planner server at https://146-190-249-220.sslip.io ("spam me" nag escalation) | `TOKEN_PLANNER` |

A client sees and controls only its own alerts.

Base URL: `https://agent-bus-phone.<cloudflare-subdomain>.workers.dev` (printed by `python notify.py setup gateway`; stored on the PC in `config.json` → `gateway.url`).

## Authentication

Every `/v1/*` endpoint except `/v1/health` needs:

```
Authorization: Bearer <client token>
```

- Each client has its own Worker secret named `TOKEN_<CLIENT>`; `<CLIENT>` is `[A-Z0-9_]{1,32}` and the client id is its lowercase form (`TOKEN_PLANNER` → `planner`).
- Token format (convention, not parsed): `gw_<client>_` followed by 43 base64url characters (`secrets.token_urlsafe(32)`), e.g. `gw_planner_Qm9…`. The Worker ignores any `TOKEN_*` value shorter than 32 characters.
- The Worker compares the presented token with every `TOKEN_*` secret in constant time; the matching secret decides the client. No match → `401 {"error":"unauthorized"}`.
- Create or rotate a client token on the PC with `python notify.py gateway add-client <name>`. It generates the token, uploads it with `wrangler secret put TOKEN_<NAME>` (piped on stdin, never written to disk) and prints it once in Cameron's own terminal so he can paste it into that client's environment. Tokens are never sent over the bus, chat, or git.

## Endpoints for clients

All bodies are JSON (`Content-Type: application/json`, max 16 KB). Times are ISO 8601 UTC strings. Errors are `{"error": "<code>", "detail": "<human text>"}`.

### `GET /v1/health` (no auth)

```json
{"ok": true, "service": "agent-bus-phone", "version": 1, "dry_run": false, "twilio": true, "text_channel": "sms"}
```
`twilio` is a boolean saying whether the Twilio secrets (account SID, auth token, `TWILIO_FROM`, `OWNER_NUMBER`) are all set; calls and texts both need them. `text_channel` is always `"sms"`. Nothing else is revealed.

### `POST /v1/alerts` (create)

```json
{
  "ref": "bus-3f9c…",
  "title": "Pecunia prod is down",
  "message": "Health check failing for 6 minutes. Rollback needs your approval.",
  "channels": ["text", "call"],
  "retry": true,
  "fallback_text": false
}
```

| Field | Rules |
|---|---|
| `ref` | Required, 1–120 chars `[A-Za-z0-9_.:@#-]`. The client's idempotency key: a second create with the same `(client, ref)` returns the existing alert with `200` and `"duplicate": true` and never dials or texts again. |
| `title` | Required, 1–150 chars. |
| `message` | Required, 1–1500 chars. |
| `channels` | Required, non-empty subset of `["call", "text"]`. `text` = one SMS now (see [SMS texts](#sms-texts)). `call` = phone call with the keypad menu. With both, the SMS goes out first and says how to reply. |
| `retry` | Optional, default `true`. If the call is not acknowledged (no answer, busy, voicemail, hung up, no key), call once more after `RETRY_MINUTES` (3). |
| `fallback_text` | Optional, default `true`. When a call alert ends `unanswered`, send the SMS "Missed urgent call: <title>" with the message. The bus sends `false` because its calls already include `text`. |

Responses:
- `201 {"alert": Alert}`: created. A call is dialed immediately unless another call is in progress (then `status: "queued"`; it dials as soon as the line is free, checked every 20 s).
- `200 {"alert": Alert, "duplicate": true}`: same `ref` already exists.
- `400 {"error":"invalid", "detail": …}`.
- `401 {"error":"unauthorized"}`.
- `429 {"error":"rate_limited", "detail": …, "retry_after": <seconds>}` plus a `Retry-After` header: a daily cap was hit (see Limits). Nothing was stored.
- `503 {"error":"not_configured", "detail":"twilio"}`: the Twilio secrets are missing (and `DRY_RUN` is off). Both channels need them. Nothing was stored.

### `GET /v1/alerts/{id}`

`200 {"alert": Alert}`, or `404 {"error":"not_found"}` (also for other clients' alerts).

### `POST /v1/alerts/{id}/ack`

The client reports that Cameron acknowledged the alert some other way (e.g. on the PC). Cancels any pending retry or snooze call; a call that is ringing right now is ended best-effort through Twilio. Idempotent. `200 {"alert": Alert}`. An alert already in a final state is returned unchanged.

### `POST /v1/alerts/{id}/cancel`

Same as ack but the final status is `canceled` (the reason went away). `200 {"alert": Alert}`.

### `GET /v1/updates?after=<seq>&limit=<1-200>`

The client's change feed, oldest first. `after` defaults to 0, `limit` to 100.

```json
{"updates": [
  {"seq": 41, "alert_id": "al_9b1e0c2d4f6a8b10", "ref": "bus-3f9c…", "type": "call.dialing", "status": "calling", "attempt": 1, "at": "2026-10-01T18:04:05Z"},
  {"seq": 42, "alert_id": "al_9b1e0c2d4f6a8b10", "ref": "bus-3f9c…", "type": "alert.acked", "status": "acked", "attempt": 1, "digit": "1", "via": "call", "at": "2026-10-01T18:04:31Z"}
 ],
 "next": 42}
```

`next` is the last `seq` returned (or `after` when the list is empty); pass it as `after` next time. Sequence numbers are global and increase, so a client sees gaps. Updates are kept 7 days.

Update `type` values:

| type | meaning |
|---|---|
| `call.dialing` | Twilio accepted the call request for attempt N |
| `call.answered` | the call was picked up and the message is being read (voicemail also counts) |
| `call.ended` | the call ended; `call_status` = Twilio `CallStatus` (`completed`, `no-answer`, `busy`, `failed`, `canceled`) |
| `alert.acked` | final: pressed 1, texted 1, or acked by the client; `via` says which |
| `alert.done` | final: pressed 9 or texted 9 / `done` ("it's already done"); `via` says which, `digit` is `"9"` on the keypad. A client should treat it as acked **and** mark the item complete (e.g. tick the todo). |
| `alert.snoozed` | pressed 2 or texted 2; `via` says which; `next_call_at` says when it rings again |
| `alert.retry_scheduled` | not acknowledged; `next_call_at` says when the retry rings |
| `alert.unanswered` | final: last attempt ended without a key press (a later SMS "1" or call-back can still ack it) |
| `alert.canceled` | final: canceled by the client |
| `alert.failed` | final: Twilio refused the call (detail in `detail`) |
| `text.sent` / `text.failed` | the SMS for this alert (`channels` text or the fallback) |

**`via`** is on every `alert.acked`, `alert.done` and `alert.snoozed` update, and in `call.via` on the alert:

| via | how Cameron answered |
|---|---|
| `call` | a key on the keypad, during the alert's call or when he called the number back (`digit` is set) |
| `sms` | he replied to a text (no `digit`) |
| `client` | the client acked it through the API (e.g. on the PC) |

### Alert object

```json
{
  "id": "al_9b1e0c2d4f6a8b10",
  "client": "bus",
  "ref": "bus-3f9c…",
  "title": "Pecunia prod is down",
  "message": "Health check failing…",
  "channels": ["text", "call"],
  "status": "acked",
  "created": "2026-10-01T18:04:04Z",
  "updated": "2026-10-01T18:04:31Z",
  "call": {"attempts": 1, "max_attempts": 2, "snoozes": 0, "sid": "CA…", "last_call_status": "completed",
           "next_call_at": null, "digit": "1", "via": "call"},
  "text": {"status": "sent", "message_id": "SM…"}
}
```

`call` is `null` for text-only alerts, `text` is `null` when no text was attempted.

Alert `status` values: `queued` (call waiting for a free line), `calling` (call in progress), `snoozed`, `waiting_retry`, and the final states `acked`, `done` (key 9 / texted `done`), `unanswered`, `canceled`, `failed`. Text-only alerts go straight to the final state `sent` or `text_failed`. The one change after a final state: an `unanswered` alert that Cameron acknowledges later (SMS "1" or call-back) becomes `acked` (or `done` with key 9 / texted `done`).

## The call

Twilio dials `OWNER_NUMBER` (and only ever that number) from `TWILIO_FROM` with `Timeout = RING_SECONDS` (25). When answered, a neural TTS voice (`TTS_VOICE`, default `Polly.Matthew-Neural`) says:

> "Yo, you have to lock in on this now. *Title.* *Message.* Press 1 if you're on it. Press 2 to snooze ten minutes. Press 3 to hear it again. Press 9 if it's already done."

The first sentence is `CALL_INTRO` (Worker var; `""` = no intro). For client `planner` the title is **not** spoken (intro → message → menu): the planner's `title` is a short line for the SMS and the planner's own UI, and its `message` is the full sentence read after the intro. Every other client hears intro → title → message → menu. The snooze length is spoken in words up to twenty ("ten minutes"). Before speaking, title and message are cleaned: URLs become "link", markdown symbols and control characters are removed, whitespace collapses, the message is cut to 700 characters, and everything is XML-escaped.

Keypad (DTMF, one digit, 7-second wait, the prompt can be interrupted):

| Key | Effect | Spoken reply |
|---|---|---|
| 1 | `acked` (final, "I'm on it"); pending retries cancelled; `alert.acked` with `digit: "1"` | "Acknowledged. Goodbye." |
| 9 | `done` (final, "it's already done"); pending retries cancelled; `alert.done` with `digit: "9"`, `via: "call"` | "Nice. Marked as done. Goodbye." |
| 2 | `snoozed`; rings again in `SNOOZE_MINUTES` (10). The snoozed call gets its own one retry. After `MAX_SNOOZES` (3) snoozes, 2 counts as 1. | "Snoozed. I will call again in 10 minutes." |
| 3 / any other key | repeats the message | |
| nothing | the message plays a second time; still nothing → "No key pressed. I will call back in 3 minutes." (on the last attempt: "No key pressed. I will text you instead." when `fallback_text` is true and Twilio is set up (or `DRY_RUN`), otherwise "No key pressed. Goodbye.") and hang up | |

No answer, busy, voicemail, hang-up without a key, or a failed dial all count as "not acknowledged": attempt 1 schedules the retry (`waiting_retry`, +`RETRY_MINUTES`), the last attempt ends `unanswered` (plus the fallback text if requested). A call that never reports back within 10 minutes is treated as ended with status `unknown`.

**Calling back:** the Twilio number's Voice URL points at `/twilio/incoming`. When Cameron calls the number from `OWNER_NUMBER`, the gateway reads the newest alert from the last 24 hours that is not acked, done or cancelled, with the same keypad menu; with none it says "Nothing urgent right now. Goodbye." There is no conversation: keys only. Calls from any other number are rejected (`<Reject/>`). Caller ID only gates this read-only replay; acknowledging by call-back is low risk.

## SMS texts

Every text is one SMS from `TWILIO_FROM` to `OWNER_NUMBER` (and only ever that number) through Twilio's Messages API. `text.sent` means Twilio accepted it (`message_id` is the `SM…` sid); carrier delivery problems after that show up only in the Twilio Console (Monitor > Logs > Messaging).

Bodies are kept cheap: GSM-7 friendly and at most 600 characters (a GSM-7 SMS segment is 160 characters, or 153 when split).

| When | Body |
|---|---|
| text-only alert | `<prefix> <title>`<br>`<message>` |
| text that comes with a call | `<prefix> URGENT: <title>`<br>`<message>`<br>`Reply 1 = got it, 2 = snooze 10 min, 9 = done.` |
| missed-call fallback | `<prefix> Missed urgent call: <title>`<br>`<message>`<br>`Reply 1 = got it, 9 = done.` |

- `<prefix>` is `[Agent Bus]` for client `bus`, `[Planner]` for `planner`, otherwise `[<client>]`.
- Title and message are cleaned: smart quotes, dashes, ellipsis and odd spaces become ASCII, emoji and other pictographs are removed (with their joiners, skin tones and flags), accented letters outside the GSM alphabet lose their accent (`á` → `a`, `é` stays), control characters become spaces and whitespace collapses to single spaces. Characters with no GSM form (e.g. CJK) are kept, and Twilio then sends that text as UCS-2.
- Only the message is shortened (ending in `...`) to fit 600 characters; the header and the reply hint are never cut.
- A Twilio refusal (4xx) gives `text_failed` with Twilio's code and message in `detail` (secrets redacted). A Twilio 5xx, network error or timeout is also `text_failed`; a client may retry with a new `ref`.

### SMS replies (`/twilio/sms`)

The Twilio number's Messaging webhook ("A message comes in") points at `/twilio/sms`. Texts from any number other than `OWNER_NUMBER` get an empty `<Response/>` and change nothing. From Cameron, the body is trimmed and lowercased (a trailing `.` or `!` is ignored), and it acts on **the newest call alert of any client that is not final, or that ended `unanswered` within the last 24 hours** (text-only, acked, done, canceled and failed alerts are never picked):

| Reply | Effect | Texted back |
|---|---|---|
| `1`, `ok`, `ack`, `got it`, `y`, `yes` | `acked`, `via: "sms"`; a ringing call is ended, a pending retry or snooze is cancelled. An `unanswered` alert becomes `acked` and emits `alert.acked`. | `Got it: <title>` |
| `2`, `snooze` | `snoozed`, `via: "sms"`, rings again in `SNOOZE_MINUTES`; a ringing call is ended, a pending retry replaced. Past `MAX_SNOOZES`, or on an `unanswered` alert, it counts as 1. | `Snoozed 10 min: <title>` (or `Got it: <title>`) |
| `9`, `done` | `done`, `via: "sms"`, emits `alert.done`; a ringing call is ended, a pending retry or snooze is cancelled (also on an `unanswered` alert). | `Marked done: <title>` |
| anything else | nothing changes | `Reply 1 to acknowledge, 2 to snooze 10 min, or 9 if done.` |
| (no alert to act on) | nothing changes | `No open alerts.` |

Replies are TwiML `<Response><Message>…</Message></Response>` and count toward `MAX_TEXTS_PER_DAY`. Over the cap the ack or snooze still happens, but the answer is an empty `<Response/>` (no text back).

Twilio handles the carrier keywords itself: texting `STOP` to the number blocks all further texts from it (calls still work) until Cameron texts `START`.

## Twilio webhooks (public, signed)

All are `POST` with form-encoded bodies and require a valid `X-Twilio-Signature`: Base64(HMAC-SHA1(`TWILIO_AUTH_TOKEN`, full request URL + each POST param name and value concatenated in sorted name order)). A missing or wrong signature returns `403` and changes nothing. Responses are TwiML (`text/xml`).

| Path | Purpose |
|---|---|
| `/twilio/voice?alert=<id>&attempt=<n>&pass=<0\|1>` | answer TwiML: `<Gather>` wrapping `<Say>`; no input → pass 1 replay → goodbye |
| `/twilio/gather?alert=<id>&attempt=<n>` | handles `Digits` |
| `/twilio/status?alert=<id>&attempt=<n>` | call status callback (`CallStatus`, `CallSid`); callbacks for an older attempt are ignored |
| `/twilio/incoming` | Cameron calling the number (the number's Voice URL) |
| `/twilio/sms` | Cameron texting the number (the number's Messaging URL); see [SMS replies](#sms-replies-twiliosms) |

The callback URLs for calls are built from the origin of the request that created the alert, so no base-URL setting is needed. `python notify.py setup twilio` sets the number's `VoiceUrl` and `SmsUrl`.

## Limits and safety

- Only `OWNER_NUMBER` is ever dialed or texted; requests cannot choose a number. Inbound texts and calls from other numbers are ignored or rejected.
- At most one call rings at a time; others queue.
- `CLIENT_DAILY_CALLS` (30): new call alerts per client per rolling 24 h. `MAX_DIALS_PER_DAY` (100): total dials (including retries and snoozes) per rolling 24 h. `MAX_TEXTS_PER_DAY` (100): SMS sent per rolling 24 h, alert texts, fallbacks and replies together. Hitting a cap on create returns `429` with `retry_after`.
- The gateway has no quiet hours: urgency and Cameron's availability state are decided by the client (the bus holds anything below the state's minimum priority, and only priority 5 / `call: true` places calls). Cameron's phone-level Sleep focus is the last guard.
- Twilio keys never leave the Worker; the PC holds only its own bus token.

## Configuration

Worker **secrets** (`npx wrangler secret put NAME` in `gateway/`, or the setup commands below):

| Secret | Value |
|---|---|
| `TWILIO_ACCOUNT_SID` | `AC…` |
| `TWILIO_AUTH_TOKEN` | Twilio auth token (REST auth and webhook signatures) |
| `TWILIO_FROM` | the Twilio number (voice + SMS), E.164 (`+1519…`) |
| `OWNER_NUMBER` | Cameron's cell, E.164; the only number called or texted, and the only one whose replies count |
| `TOKEN_BUS`, `TOKEN_PLANNER`, … | client tokens |

Worker **vars** (`gateway/wrangler.jsonc`, not secret): `DRY_RUN` ("0"), `TTS_VOICE` ("Polly.Matthew-Neural"), `CALL_INTRO` ("Yo, you have to lock in on this now."; spoken first on every call, "" for none), `RING_SECONDS` ("25"), `RETRY_MINUTES` ("3"), `SNOOZE_MINUTES` ("10"), `MAX_ATTEMPTS` ("2"), `MAX_SNOOZES` ("3"), `CLIENT_DAILY_CALLS` ("30"), `MAX_DIALS_PER_DAY` ("100"), `MAX_TEXTS_PER_DAY` ("100").

PC setup commands (prompt locally; secrets are piped to `wrangler secret put` and never stored on the PC except the bus token in the git-ignored `config.json`):
- `python notify.py setup gateway`: deploys the Worker, creates `TOKEN_BUS`, saves `gateway.url`/`gateway.token`.
- `python notify.py setup twilio`: asks for the Account SID and auth token, checks them, reuses a Canadian number that can both call and text or finds one (`VoiceEnabled` + `SmsEnabled`) and buys it after you confirm, points its Voice URL at `/twilio/incoming` and its Messaging URL at `/twilio/sms`, asks for your cell, uploads the four Twilio secrets.
- `python notify.py setup phone`: gateway, then Twilio, then a test SMS, then offers a test call.
- `python notify.py gateway add-client planner`: creates `TOKEN_PLANNER` and prints it once.

## Dry-run mode (`DRY_RUN = "1"`)

For local testing with `npx wrangler dev` (put test values in `gateway/.dev.vars`, which is git-ignored): no request ever reaches Twilio; call SIDs look like `CAdry…` and SMS ids like `SMdry…`. One extra endpoint exists only in dry-run:

`POST /v1/alerts/{id}/simulate` (client auth) drives the same code paths as the real webhooks:

| body | effect / response |
|---|---|
| `{"event":"answer"}` | as if Twilio fetched `/twilio/voice` for the current attempt; returns `{"twiml": "<Response>…"}` |
| `{"event":"digits","digits":"1"}` | as `/twilio/gather`; returns `{"twiml": …, "alert": Alert}` |
| `{"event":"status","call_status":"no-answer"}` | as `/twilio/status`; returns `{"alert": Alert}` |
| `{"event":"advance","seconds":180}` | moves the dry-run clock forward and runs due timers; returns `{"alert": Alert}` |
| `{"event":"sms","body":"1"}` | as `/twilio/sms` from `OWNER_NUMBER`; returns `{"twiml": …, "alert": Alert}` for the alert it acted on, or `"alert": null` |

## Using it from the planner server

Environment on the planner server (Cameron sets these himself): `PHONE_GATEWAY_URL` (the workers.dev URL) and `PHONE_GATEWAY_TOKEN` (from `python notify.py gateway add-client planner`).

```js
const res = await fetch(`${process.env.PHONE_GATEWAY_URL}/v1/alerts`, {
  method: "POST",
  headers: {"Authorization": `Bearer ${process.env.PHONE_GATEWAY_TOKEN}`, "Content-Type": "application/json"},
  body: JSON.stringify({ref: `nag-${task.id}-${n}`, title: "Overdue: " + task.title,
                        message: "You said to spam you about this. It was due at 3 PM.", channels: ["text", "call"]}),
});
// 201/200 → {alert}; 429 → wait alert.retry_after seconds; 503 → the gateway is not set up yet.
```

- `["text", "call"]` texts first (`[Planner] URGENT: Overdue: …` with "Reply 1 = got it, 2 = snooze 10 min, 9 = done.") and then rings; Cameron can answer either. `["text"]` alone sends a plain `[Planner] Overdue: …` SMS. Escalate to `call` only when the nag is genuinely urgent; the per-client daily cap (`CLIENT_DAILY_CALLS`, 30 call alerts) is enforced.
- Use a new `ref` per escalation step so retries of the same HTTP request never double-text or double-dial.
- Read the outcome with `GET /v1/alerts/{id}` or the `/v1/updates` feed; stop nagging when the status is `acked` or `done` (`done` = he says it's already finished: mark the item complete; `call.via` says whether he pressed 1, texted back, or you acked it). A text-only alert ends `sent` or `text_failed` and is never acknowledged by a reply: replies act on call alerts.
- `POST /v1/alerts/{id}/ack` when Cameron completes the task in the planner, so no retry rings.

## Implementation notes

These are facts about `gateway/` (the Worker) that the contract above leaves open. Clients can rely on them.

**Attempts, cycles and `max_attempts`**
- `call.attempts` counts every dial of the alert and never resets. It is the `attempt` in webhook URLs and in updates, so it grows across snoozes (attempt 1, snooze, attempt 2, its retry 3, …).
- `call.max_attempts` is the number of dials per cycle: `MAX_ATTEMPTS` (2) when `retry` is true, 1 when it is false. A cycle starts at creation and again at each snooze, so after a snooze `attempts` can be larger than `max_attempts`.
- The last attempt of a cycle is the one whose end without a key press makes the alert `unanswered`, and whose no-input goodbye says "I will text you instead." only when the fallback text will be sent (`fallback_text` true and Twilio configured, or `DRY_RUN`); otherwise it says "No key pressed. Goodbye." (the bus sends `fallback_text: false`, so bus calls hear "Goodbye").

**Create**
- Checks run in this order: auth (401), body and validation (400), duplicate `ref` (200 with `duplicate: true`; this wins over caps and configuration), not_configured (503), daily caps (429), then store. When several caps are hit, `retry_after` is the largest of them. It is sent both in the JSON and in the `Retry-After` header.
- Bodies over 16 KB return `400 {"error":"invalid"}`. Character limits count Unicode code points (Python `len()`). `title` and `message` must not be all whitespace. `channels` must not repeat. `retry` and `fallback_text` must be booleans; `null` means the default.
- A text is sent before the call is dialed. An SMS failure on a `["text","call"]` alert emits `text.failed` and the call still goes ahead.
- A Twilio refusal (4xx other than 408 and 429) on the first dial still returns `201`, with `status: "failed"`. 5xx, 408, 429, a network error or a timeout counts as an unacknowledged attempt: `call.ended` with `call_status: "failed"` and a `detail`, then `alert.retry_scheduled` or `alert.unanswered`.
- If the text cap is hit (or the Twilio secrets have disappeared) when a fallback is due, the result is `text.failed` with a `detail`.
- `ref` idempotency lasts as long as the alert is kept (7 days).

**Alert object**
- `call.next_call_at` is set while the alert is `snoozed`, `waiting_retry` or `queued` (for `queued`, it is the time the call became due), and is `null` otherwise.
- `call.digit` is the last state-changing key: `"1"`, `"2"` or `"9"`. SMS replies and client acks leave it unchanged.
- `call.via` is how the last ack, done or snooze arrived (`"call"`, `"sms"`, `"client"`), `null` until then. A client cancel leaves it unchanged.
- `call.last_call_status` is the last terminal `CallStatus`. It can also be `"failed"` for a dial that never connected, or `"unknown"` for a call that never reported back.
- `text` is `{"status":"sent","message_id":"SM…"}` or `{"status":"failed","message_id":null,"detail":"…"}`. If both the text channel and a fallback were sent, it shows the most recent one.
- Twilio error details have every secret value, including `OWNER_NUMBER`, replaced with `[redacted]`. They read `Twilio <HTTP status>: <message> (code <Twilio code>)`, or `Twilio request failed: <error name>` for a network error or timeout.

**Updates**
- Every update has `seq, alert_id, ref, type, status, attempt, at`. `status` is the alert's status when the event happened. `attempt` is `call.attempts`, or 0 for text-only alerts.
- Optional fields: `digit` (on keypad `alert.acked` / `alert.done` / `alert.snoozed`, absent for SMS and client acks), `via` (on every `alert.acked`, `alert.done` and `alert.snoozed`), `call_status` (`call.ended`), `next_call_at` (`alert.snoozed`, `alert.retry_scheduled`), `detail` (`alert.failed`, `text.failed`, and `call.ended` with `failed`/`unknown`), `message_id` (`text.sent`).
- A call's trailing `call.ended` usually comes after `alert.acked` or `alert.snoozed`, because Twilio reports the hang-up after the key press. Moving an alert into `queued` emits no update.
- Timestamps are seconds precision (`2026-10-01T18:04:05Z`).

**Line, queue and timers**
- One call holds the line from the moment it is dialed until its terminal status callback arrives. If no webhook arrives for that call for 10 minutes, the call ends as `unknown`. Voice and gather webhooks reset the 10-minute clock. Pressing 1 or 2 (or replying by SMS) does not free the line by itself; the `completed` callback that follows does.
- When the line frees, the oldest queued call dials immediately. While the line is busy, queued calls are re-checked every 20 s. A retry or snooze that comes due while the line is busy becomes `queued`.
- If `MAX_DIALS_PER_DAY` is reached when a retry, snooze or queued call is due, the alert gets `call.ended` (`call_status: "failed"`, `detail` naming the cap) and then goes straight to `unanswered`, with the fallback text if requested. There is no retry in that case.
- A client `ack` or `cancel` of a ringing call, and an SMS reply that acks or snoozes it, send Twilio `Status=completed`. Keypad answers hang up through TwiML instead.
- Alerts (with their `ref` index entries) and updates are deleted 7 days after creation. The prune runs every 6 hours while any data exists.

**Calling back (`/twilio/incoming`)**
- It replays the newest call alert from any client, created in the last 24 hours, whose status is not `acked`, `done` or `canceled`. This includes `unanswered` and `failed` alerts. Text-only alerts are never replayed.
- Webhook URLs use `attempt=0`.
- Pressing 1 acknowledges the alert (9 marks it `done`), even if it was `unanswered`; with an SMS "1", this is the way a final alert can change, to `acked`.
- Pressing 2 snoozes only an alert that is still pending and has snoozes left. Otherwise 2 counts as 1.
- Acknowledging or snoozing by call-back ends a ringing outbound call for the same alert and cancels any pending retry. The no-input goodbye is "No key pressed. Goodbye." Call-back answers have `via: "call"`.

**SMS replies (`/twilio/sms`)**
- The alert is chosen at the moment the reply arrives: the newest by creation time among call alerts that are `queued`, `calling`, `snoozed` or `waiting_retry` (any age), or `unanswered` and created in the last 24 hours. Unlike call-back, `failed` alerts are not picked.
- A reply that changes an alert runs the due timers right away (e.g. a snooze replaces a pending retry).
- Every reply sent counts one text in `cnt:texts`; texts from other numbers count nothing.

**Dry run**
- `simulate` acts on the current attempt. `answer`, `digits` and `status` on a text-only alert return `400 invalid`. `digits` must be one character from `0-9 * #`. `call_status` must be a Twilio `CallStatus`; non-terminal values are accepted and ignored. `advance.seconds` is an integer from 1 to 2592000.
- `sms` needs `body` as a string of at most 1600 characters and works with any of the client's alert ids (including text-only ones): the reply picks its own alert, which can belong to another client. That alert is acted on but returned as `null` when it is not the caller's.
- The dry-run clock offset is stored and persists, so `created`, `at` and `next_call_at` are in virtual time. Real alarms still fire at the right real moment.
- After `digits`, send `{"event":"status","call_status":"completed"}` the way Twilio would. Otherwise the line stays busy for 10 minutes and other alerts stay `queued`.

**Misc**
- An unknown route, or a known path with the wrong method, returns `404 {"error":"not_found"}`.
- A `/twilio/*` request with a bad or missing signature, or with `TWILIO_AUTH_TOKEN` unset, returns `403` (plain text).
- An unexpected exception returns `500 {"error":"internal"}`.
- If `TOKEN_*` secrets share a value, the alphabetically first secret name wins.
