# Phone escalation: texts and calls

Priority decides how loudly a notification reaches you. Low priorities stay on ntfy as before. Priority 4 also sends an SMS text, and priority 5 also rings your phone and reads the message aloud. Texts and calls both come from your Twilio number through a small Cloudflare Worker (the **gateway**, [PHONE-GATEWAY.md](PHONE-GATEWAY.md)), which holds the Twilio keys and runs the retry and snooze timers.

## The ladder

| Priority | What reaches you |
|---|---|
| 1–3 | ntfy only (your main provider) |
| 4 | ntfy **and** an SMS text |
| 5, or `call: true` | ntfy, an SMS text **and** a phone call (the text arrives first and says how to reply) |

- **Your state is checked first.** A notification below the state's minimum is held, and a held notification never escalates. Studying, lecture, gym, focus and away pass 4 and up, so a 4 still texts you. Meal passes 3 and up. Sleeping passes only 5.
- **`call: true`** forces priority 5, so it gets through every state, sleeping included. That is deliberate: a genuinely urgent call should wake you.
- **`call: false`** never rings, even at priority 5. The text still goes.
- **Duplicates** (same `dedupe_key`) never escalate.
- Agents are told to use `call` only for genuine emergencies: production down, data or money at risk, a hard deadline within about an hour that only you can act on, or you asked to be called. Never for "done", progress or questions.

Settings live in `config.json` under `phone` (defaults in `config.example.json`): `text_channel` (`sms`, `telegram` for the old Telegram bot, or `off`), `text_min_priority` (4), `call_min_priority` (5), `max_calls_per_hour` (4), `max_calls_per_day` (12), `poll_seconds` (5).

## The texts

They come from the Agent Bus number and look like this:

```text
[Agent Bus] Pecunia deploy
Approve the migration in the PR before 5 PM.
```

```text
[Agent Bus] URGENT: Pecunia prod is down
Health check failing for 6 minutes. Rollback needs your approval.
Reply 1 = got it, 2 = snooze 10 min.
```

Emoji and fancy punctuation are stripped so each text stays in the cheap GSM alphabet, and a text is at most 600 characters (long messages are cut, the reply line never is).

**Replying** to the number works for urgent alerts (the ones with a call):

| You text | What happens | It texts back |
|---|---|---|
| `1` (or `ok`, `ack`, `got it`, `y`, `yes`) | Acknowledged: the call stops ringing, no retry. Also works after a missed call, up to 24 hours later. | `Got it: <title>` |
| `2` (or `snooze`) | Snoozed: it calls again in 10 minutes. After 3 snoozes, 2 counts as 1. | `Snoozed 10 min: <title>` |
| anything else | nothing | `Reply 1 to acknowledge or 2 to snooze 10 min.` |

A reply always acts on the newest urgent alert that is still open (or was missed in the last 24 hours). With nothing open it answers `No open alerts.` Texts from any other number are ignored. Never text `STOP` to the number: Twilio would block every text from it until you text `START`.

## The call

Caller ID is your Twilio number (save it as **Agent Bus**). A neural voice says:

> "Urgent alert from your agents. *Title.* *Message.* Press 1 to acknowledge. Press 2 to snooze for 10 minutes. Press 3 to hear it again."

| Key | Effect |
|---|---|
| 1 | Acknowledged. No more calls. |
| 2 | Snoozed: it rings again in 10 minutes. After 3 snoozes, 2 counts as 1. |
| 3 | Repeats the message. |
| nothing | Plays once more. If there is still no key press, it calls back in 3 minutes. |

No answer, voicemail, busy or hanging up without a key all count as "not acknowledged". The gateway tries once more after 3 minutes; after that the call is **unanswered**. You already have the text and the ntfy ping. Reply `1` to the text, or call the number back (you hear the newest open alert with the same keypad menu), and it still counts as acknowledged.

To acknowledge from the PC instead (it stops any pending retry): `python notify.py phone calls`, then `python notify.py phone ack <id>`.

## How the agent hears back

The agent that asked for the call gets a bus message from `bus` when the outcome is known:

- 📞 Cameron acknowledged the call (pressed 1): *title*
- 📞 Cameron acknowledged the call (texted back): *title*
- 📞 Cameron acknowledged the call on the PC: *title*
- 📞 Cameron snoozed the call (pressed 2): *title*. The gateway calls again around 18:14. (or "(texted back)")
- 📞 Cameron did not pick up or acknowledge the call after 2 tries (he also got the text and ntfy): *title*, plus "If he replies 1 to the text later, you will get another message."
- 📞 The call to Cameron could not be placed: *reason*

After an unanswered call the PC keeps reading the gateway's updates for 2 hours, so a late "1" by text (or a call-back) still reaches the agent as one more message. That only happens when the notification's `source` is a registered agent id, which is always true for `bus.py notify` and `bus_notify_phone`. `GET /phone/calls` (or `python notify.py phone calls`) lists every call and its status: `queued` (being submitted), `dry-run`, `submitted`, `calling`, `waiting_retry`, `snoozed`, `acked`, `unanswered`, `canceled` or `failed`, with `via` (`call`, `sms` or `client`) once you answered.

## Why SMS

- You asked for real texts, not a chat app.
- It is the same Twilio number and account as the calls: no extra app, bot or account to set up or keep alive.
- It works on any phone, shows up like any other text, and the number can be one contact ("Agent Bus") for both calls and texts.
- Replies acknowledge: texting `1` stops the calls, which also works on the train or in a lecture when you cannot answer a call.

(Telegram is still available as `phone.text_channel: "telegram"` with `python notify.py setup telegram`, but it is no longer part of the setup.)

## Setup checklist

1. **Cloudflare:** create a free account at https://dash.cloudflare.com, then log in once from the gateway folder: `cd C:\Users\camer\Desktop\Projects\notifications\gateway ; npx wrangler login` (a browser opens; click Allow).
2. **Twilio:** sign up at https://www.twilio.com and turn on two-factor authentication. Upgrade the account (Console > Billing; $20 minimum), because trial calls play a Twilio notice first and trial texts carry a trial prefix. Leave **auto-recharge off**. Under Voice > Settings > Geo permissions and Messaging > Settings > Geo permissions, allow **Canada only**.
3. Run everything in order (each step can also be run alone: `setup gateway`, `setup twilio`):
   ```powershell
   python notify.py setup phone
   ```
   It deploys the gateway, then asks for the Twilio Account SID and Auth Token. It reuses your Twilio number if it can both call and text, or finds a 519/226/548 number that can and buys it after you say yes. It points the number's calls and texts at the gateway and asks for your cell. Keys are typed into hidden prompts. Then it sends you a test text and asks "Place a test call now? [y/N]".
4. Restart the service so it uses the new settings: `.\Start-Notify.ps1 -Restart`.
5. Test it: `python notify.py phone test` (a priority-4 text) and `python notify.py phone test --call` (a text and a call).
6. Save the Twilio number in your contacts as **Agent Bus**. Allow that contact in your phone's Sleep and Do Not Disturb focus, or a night-time call will not ring.

## What it costs

| Item | Monthly |
|---|---|
| Cloudflare Worker (free plan: 100k requests/day; polling uses a few thousand) | free |
| Twilio Canadian number (voice + SMS) | about US$1.15 |
| SMS: about US$0.0083 each plus Canadian carrier fees, about 1.3 cents per text (sent or received; a long text counts per 153-character segment) | cents |
| Calls: about US$0.014/min, plus neural speech at about a cent per call | cents |
| **Total with a few urgent alerts a month** | **about US$2** |

The $20 prepaid minimum lasts many months.

## Testing without accounts

- **Before the gateway exists**, a priority-5 or `call: true` notification is stored as a `dry-run` call. Nothing rings, but you can see exactly what would have happened: `python notify.py send "Test" --call` answers with `"call": "dry-run: gateway not configured (...)"` and `"text": "skipped: phone gateway not set up (python notify.py setup phone)"`, and `python notify.py phone calls` lists it.
- **Against a local dry-run gateway** (no Twilio traffic): copy `gateway\.dev.vars.example` to `gateway\.dev.vars` (`DRY_RUN=1`) and run `npx wrangler dev` in `gateway\`. Then run the end-to-end tests, which drive a call to "pressed 1" and an unanswered call to a late SMS "1" through the gateway's simulate endpoint:
  ```powershell
  $env:GATEWAY_E2E_URL = "http://127.0.0.1:8787"; $env:GATEWAY_E2E_TOKEN = "<the TOKEN_BUS value from .dev.vars>"
  python -m unittest tests.test_phone -v
  ```

## Limits

- On the PC: at most `max_calls_per_hour` (4) and `max_calls_per_day` (12) real calls. Past that the call is skipped (`"call": "skipped: call limit reached (4 per hour)"`), and you still get a plain SMS text and the ntfy ping.
- In the gateway: 10 call alerts per client and 30 dials in any 24 hours, 100 texts a day (including replies), and one call ringing at a time (others wait). It only ever calls or texts the cell number you entered.
- A priority-4 text is an ordinary queued notification (provider `sms`), retried up to 5 times like any other when the gateway is unreachable. A text Twilio refuses is not retried. With a call, the text rides along with the call itself (`"text": "sent with the call"`).

## Where the keys live

| Key | Where |
|---|---|
| The bus's gateway token | `config.json` → `gateway.token` and the Worker secret `TOKEN_BUS` |
| Twilio Account SID, Auth Token, the Twilio number, your cell | Worker secrets only, never on the PC |
| Other clients' tokens (planner) | Worker secret `TOKEN_PLANNER` and that server's environment; printed once, never stored on the PC |
| Telegram bot token and chat id (legacy, only if you set it up) | `config.json` on the PC only |

Rotate the bus token with `python notify.py setup gateway --rotate` (then restart the service). Rotate a client's token by running `python notify.py gateway add-client <name>` again.

## Troubleshooting

- `python notify.py phone status` shows `text_channel`, `gateway_configured`, `open_calls`, `last_poll` and `last_error`. If `gateway_configured` is false after setup, restart the service.
- Each notification's answer says what happened to the text and the call: `queued`, `sent with the call`, `dry-run: …`, `skipped: phone gateway not set up …`, `skipped: call limit reached …`. `python notify.py events` shows each text's delivery (`accepted` or `failed` with the reason).
- **Calls work but texts do not arrive** (or the test text says it failed): Canadian numbers sometimes need SMS registration. Check Twilio Console > Monitor > Logs > Messaging for the message and its error code; calls still work meanwhile. Also check Messaging geo permissions allow Canada, and that you never texted `STOP` to the number (text `START` to undo it).
- Replies do nothing: the number's Messaging webhook must be `<gateway>/twilio/sms` (run `python notify.py setup twilio` again), and you must text from the cell number you entered.
- A call `failed` with "gateway refused the bus token": run `python notify.py setup gateway` (it re-uploads `TOKEN_BUS`), then restart the service. "gateway: twilio not configured": run `python notify.py setup twilio`.
- The call starts with a Twilio trial notice: upgrade the Twilio account.
- No ring at night: allow the Agent Bus contact in your Sleep focus.
- `setup gateway` says to log in: `cd gateway ; npx wrangler login`, then run it again.
- `setup gateway` prints no `workers.dev` URL (a brand-new Cloudflare account has no workers.dev subdomain yet): open https://dash.cloudflare.com → Workers & Pages once, choose a subdomain, then run it again. To watch the Worker live, run `npx wrangler tail` in `gateway\`.

## Other clients (the planner)

The gateway serves other programs too, each with its own token. To create the planner's: `python notify.py gateway add-client planner`. It prints `PHONE_GATEWAY_URL` and `PHONE_GATEWAY_TOKEN` once, for the planner server's environment. Its texts start with `[Planner]`. The API is in [PHONE-GATEWAY.md](PHONE-GATEWAY.md#using-it-from-the-planner-server).
