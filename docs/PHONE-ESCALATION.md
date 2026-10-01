# Phone escalation: texts and calls

Priority decides how loudly a notification reaches you. Low priorities stay on ntfy as before. Priority 4 also sends a Telegram text, and priority 5 also rings your phone and reads the message aloud. Texts go straight from the PC. Calls go through a small Cloudflare Worker (the **gateway**, [PHONE-GATEWAY.md](PHONE-GATEWAY.md)), which holds the Twilio keys and runs the retry and snooze timers.

## The ladder

| Priority | What reaches you |
|---|---|
| 1–3 | ntfy only (your main provider) |
| 4 | ntfy **and** a Telegram text |
| 5, or `call: true` | ntfy, a Telegram text **and** a phone call |

- **Your state is checked first.** A notification below the state's minimum is held, and a held notification never escalates. Studying, lecture, gym, focus and away pass 4 and up, so a 4 still texts you. Meal passes 3 and up. Sleeping passes only 5.
- **`call: true`** forces priority 5, so it gets through every state, sleeping included. That is deliberate: a genuinely urgent call should wake you.
- **`call: false`** never rings, even at priority 5. The text still goes.
- **Duplicates** (same `dedupe_key`) never escalate.
- Agents are told to use `call` only for genuine emergencies: production down, data or money at risk, a hard deadline within about an hour that only you can act on, or you asked to be called. Never for "done", progress or questions.

Settings live in `config.json` under `phone` (defaults in `config.example.json`): `text_channel` (`telegram` or `off`), `text_min_priority` (4), `call_min_priority` (5), `max_calls_per_hour` (4), `max_calls_per_day` (12), `poll_seconds` (5).

## The call

Caller ID is your Twilio number (save it as **Agent Bus**). A neural voice says:

> "Urgent alert from your agents. *Title.* *Message.* Press 1 to acknowledge. Press 2 to snooze for 10 minutes. Press 3 to hear it again."

| Key | Effect |
|---|---|
| 1 | Acknowledged. No more calls. |
| 2 | Snoozed: it rings again in 10 minutes. After 3 snoozes, 2 counts as 1. |
| 3 | Repeats the message. |
| nothing | Plays once more. If there is still no key press, it calls back in 3 minutes. |

No answer, voicemail, busy or hanging up without a key all count as "not acknowledged". The gateway tries once more after 3 minutes; after that the call is **unanswered**. You already have the text and the ntfy ping. If you call the number back, you hear the newest open alert with the same keypad menu.

To acknowledge from the PC instead (it stops any pending retry): `python notify.py phone calls`, then `python notify.py phone ack <id>`.

## How the agent hears back

The agent that asked for the call gets a bus message from `bus` when the outcome is known:

- 📞 Cameron acknowledged (pressed 1) the call: *title*
- 📞 Cameron snoozed the call (pressed 2): *title*. The gateway calls again around 18:14.
- 📞 Cameron did not pick up or acknowledge the call after 2 tries (he also got the text and ntfy): *title*
- 📞 The call to Cameron could not be placed: *reason*

That only happens when the notification's `source` is a registered agent id, which is always true for `bus.py notify` and `bus_notify_phone`. `GET /phone/calls` (or `python notify.py phone calls`) lists every call and its status: `queued` (being submitted), `dry-run`, `submitted`, `calling`, `waiting_retry`, `snoozed`, `acked`, `unanswered`, `canceled` or `failed`.

## Why Telegram and not WhatsApp

- Telegram bots are free. WhatsApp business messages cost money, and outside a 24-hour window they need paid, pre-approved templates.
- A Telegram bot needs no Meta business verification, and nothing lapses: the Twilio WhatsApp sandbox has to be rejoined every few days.
- Replies can be added later without a public URL: the PC long-polls Telegram's `getUpdates`, so nothing on the PC is exposed.

## Setup checklist

1. **Telegram bot:** in Telegram open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`, and keep the token it gives you.
2. **Cloudflare:** create a free account at https://dash.cloudflare.com, then log in once from the gateway folder: `cd C:\Users\camer\Desktop\Projects\notifications\gateway ; npx wrangler login` (a browser opens; click Allow).
3. **Twilio:** sign up at https://www.twilio.com and turn on two-factor authentication. Upgrade the account (Console > Billing; $20 minimum), because trial calls play a Twilio notice first. Leave **auto-recharge off**. Under Voice > Settings > Geo permissions, allow **Canada only**.
4. Run everything in order (each step can also be run alone: `setup telegram`, `setup gateway`, `setup twilio`):
   ```powershell
   python notify.py setup phone
   ```
   It asks for the bot token and waits for you to press Start in the bot's chat. Then it deploys the gateway and asks for the Twilio Account SID and Auth Token. It finds or buys (after you say yes) a 519/226/548 number and asks for your cell. Keys are typed into hidden prompts.
5. Restart the service so it uses the new settings: `.\Start-Notify.ps1 -Restart`.
6. Test it: `python notify.py phone test` (a priority-4 text) and `python notify.py phone test --call`.
7. Save the Twilio number in your contacts as **Agent Bus**. Allow that contact in your phone's Sleep and Do Not Disturb focus, or a night-time call will not ring.

## What it costs

| Item | Monthly |
|---|---|
| Telegram bot | free |
| Cloudflare Worker (free plan: 100k requests/day; polling uses a few thousand) | free |
| Twilio Canadian number | about US$1.15 |
| Calls: about US$0.014/min, plus neural speech at about a cent per call | cents |
| **Total with a few urgent calls a month** | **about US$2** |

The $20 prepaid minimum lasts many months.

## Testing without accounts

- **Before the gateway exists**, a priority-5 or `call: true` notification is stored as a `dry-run` call. Nothing rings, but you can see exactly what would have happened: `python notify.py send "Test" --call` answers with `"call": "dry-run: gateway not configured (...)"`, and `python notify.py phone calls` lists it.
- **Against a local dry-run gateway** (no Twilio or Telegram traffic): put test values with `DRY_RUN=1` in `gateway\.dev.vars` and run `npx wrangler dev` in `gateway\`. Then run the end-to-end test, which drives a call to "pressed 1" through the gateway's simulate endpoint:
  ```powershell
  $env:GATEWAY_E2E_URL = "http://127.0.0.1:8787"; $env:GATEWAY_E2E_TOKEN = "<the TOKEN_BUS value from .dev.vars>"
  python -m unittest tests.test_phone -v
  ```

## Limits

- On the PC: at most `max_calls_per_hour` (4) and `max_calls_per_day` (12) real calls. Past that the call is skipped (`"call": "skipped: call limit reached (4 per hour)"`), and the text and ntfy still go.
- In the gateway: 10 call alerts per client and 30 dials in any 24 hours, 100 texts a day, and one call ringing at a time (others wait). It only ever dials the cell number you entered.
- The Telegram text is an ordinary queued notification, retried up to 5 times like any other.

## Where the keys live

| Key | Where |
|---|---|
| Telegram bot token and chat id | `config.json` on the PC (git-ignored) and the Worker secrets |
| The bus's gateway token | `config.json` → `gateway.token` and the Worker secret `TOKEN_BUS` |
| Twilio Account SID, Auth Token, the Twilio number, your cell | Worker secrets only, never on the PC |
| Other clients' tokens (planner) | Worker secret `TOKEN_PLANNER` and that server's environment; printed once, never stored on the PC |

Rotate the bus token with `python notify.py setup gateway --rotate` (then restart the service). Rotate a client's token by running `python notify.py gateway add-client <name>` again.

## Troubleshooting

- `python notify.py phone status` shows `telegram_configured`, `gateway_configured`, `open_calls`, `last_poll` and `last_error`. If `gateway_configured` is false after setup, restart the service.
- Each notification's answer says what happened to the text and the call: `queued`, `dry-run: …`, `skipped: telegram not configured …`, `skipped: primary provider is already telegram`, `skipped: call limit reached …`.
- A call `failed` with "gateway refused the bus token": run `python notify.py setup gateway` (it re-uploads `TOKEN_BUS`), then restart the service. "gateway: twilio not configured": run `python notify.py setup twilio`.
- The call starts with a Twilio trial notice: upgrade the Twilio account.
- No ring at night: allow the Agent Bus contact in your Sleep focus.
- `setup gateway` says to log in: `cd gateway ; npx wrangler login`, then run it again.
- `setup gateway` prints no `workers.dev` URL (a brand-new Cloudflare account has no workers.dev subdomain yet): open https://dash.cloudflare.com → Workers & Pages once, choose a subdomain, then run it again. To watch the Worker live, run `npx wrangler tail` in `gateway\`.

## Other clients (the planner)

The gateway serves other programs too, each with its own token. To create the planner's: `python notify.py gateway add-client planner`. It prints `PHONE_GATEWAY_URL` and `PHONE_GATEWAY_TOKEN` once, for the planner server's environment. The API is in [PHONE-GATEWAY.md](PHONE-GATEWAY.md#using-it-from-the-planner-server).
