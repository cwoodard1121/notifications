# Phone channels for the agent bus: research and recommendation

> **Update 2026-10-01:** Cameron chose real SMS over Telegram. Texts now go as Twilio SMS from the same number as the calls, through the gateway, and replies (1 = got it, 2 = snooze) acknowledge alerts. See [PHONE-ESCALATION.md](PHONE-ESCALATION.md). The research below is kept as written.

Researched 2026-09-30. Prices are in USD and come from vendor pages read on 2026-09-30 unless noted otherwise. Recipient: Canadian mobile (London, ON). Sender: the Windows 11 PC running the stdlib bus at `127.0.0.1:8765`, behind NAT with no public IP. The bus already has `ntfy`, `telegram` (send only) and Twilio `whatsapp` providers (`providers.py`).

## TL;DR

**Top recommendation:**
1. **Texts:** use a **Telegram bot**. It is free, and replies work because the PC long-polls `getUpdates`, so nothing inbound is needed. Add inline buttons (Approve / Pause / Status) and allow only Cameron's Telegram user ID.
2. **Calls:** use **Twilio Programmable Voice** from a Canadian local number, with `<Say>` for TTS and `<Gather>` for DTMF key presses. Twilio calls a free **Cloudflare Worker "policy gateway"**, which holds the Twilio keys and queues events that the PC pulls. Cost is about $4–7/month.
3. **Two-way voice:** use **ElevenLabs Agents** (Starter plan, $6/month, 75 minutes included). Calls are connected through the same Worker with ElevenLabs' `register-call`, after a caller-ID check and a PIN entered on the keypad. The agent's tools call the Worker, never the PC directly.

**Runner-up:** the same design with **Twilio ConversationRelay** ($0.07/min plus your own LLM, e.g. Claude Haiku, in a Durable Object) in place of ElevenLabs. There is no subscription and only one vendor, but more code to write. For SMS specifically, the runner-up is a Twilio number used only for urgent escalation, because registration is unclear (see 1b).

**Already available at no cost:** Claude Code **Remote Control** plus the Claude app is two-way for Claude Code sessions and has push notifications. It does not cover Codex agents, the bus, phone calls or quiet-hours policy, so treat it as a complement and not the channel.

## 1. Text channels (agent updates + reply)

| Option | Cost/mo (~600 msgs) | Reply path behind NAT | Setup friction | Verdict |
|---|---|---|---|---|
| **Telegram bot** | $0 | Long-poll `getUpdates` (outbound only); inline buttons; voice notes | 2 min with @BotFather | **Pick.** Already a provider |
| Pushover | $4.99 one-time per platform | No free text; emergency priority repeats until acked, receipt can be polled | 5 min | Good loud escalation; no replies |
| ntfy (current) | $0 | Action buttons need an endpoint the phone can reach, which the bus isn't | done | Keep as a silent fallback only |
| Twilio SMS (CA long code) | $1.15 number + $0.0083/msg each way + CA carrier surcharges (about $0.005) ≈ $9–10 | Inbound SMS webhook to the Worker | Signup, card, **possible A2P sole-prop registration (weeks)** | Urgent fallback only |
| Twilio SMS (toll-free) | $2.15 number + $0.0083/msg | Same as above | **Toll-free verification required**; messages are blocked until it passes | Heavier than the long code |
| Plivo (CA) | $0.75 number + $0.0077/msg + surcharges | Webhook | Similar registration rules | Slightly cheaper, one more vendor |
| Telnyx | about $1.10 number + about $0.004/msg + surcharges | Webhook | **eKYC ID verification** before buying numbers | Cheapest per message, most KYC |
| Vonage | about $0.008/msg; Rogers/Fido surcharge CAD 0.0072 (from 2026-04-01) | Webhook | Similar | No advantage |
| WhatsApp Cloud API (Meta) | Free inside the 24-hour window after he messages; utility templates cost about $0.004–0.046 outside it | Webhook needs a public URL | Meta Business portfolio, a spare number, template approval | Fragile. The Twilio sandbox join also lapses |
| Signal | $0 | `signal-cli` is Java, unofficial, and needs its own number | High babysitting | No |
| Email-to-SMS gateways | $0 | none | n/a | **Dead or unreliable.** Bell shut down 2025-12-31; Rogers/Fido/Chatr are discontinued; Telus/Koodo/Public are unsupported and filtered; Freedom is reported discontinued; Virgin is a Bell brand. Do not use |
| Google Voice | n/a | n/a | Personal GV is US-only; Canadian Workspace numbers cannot send SMS | No |
| iMessage bridges | n/a | Need an always-on Mac (BlueBubbles and similar). Apple Messages for Business needs a registered business and an MSP | High | No (Windows PC) |

**1b. Twilio SMS registration for Canada is inconsistent in the sources.**
- Twilio's Canada SMS guidelines list domestic long codes as needing no registration.
- Third-party help centres report that Canadian long codes bought on or after **2025-03-26** need A2P Brand + Campaign registration (or "Persona" verification) before messaging Canadian recipients.
- Canadian carriers filter unregistered A2P traffic either way.
- If registration is enforced, the **Sole Proprietor** path is open to Canadians without a Business Number. It needs a mobile OTP answered within 24 hours, allows one number per campaign, charges brand and vetting fees, and manual vetting "might take several weeks".
- Toll-free verification is free and still required. Since 2026-02-17 a business registration number is required for every business type **except sole proprietorships**.
- **Plan:** buy the long code for voice anyway, then send one test SMS to himself. Only register if Twilio blocks it.

## 2. Voice channels

| Option | All-in $/min to a CA mobile | Latency (vendor claims) | Mid-call tool calls | How the PC gets the callback | Fit |
|---|---|---|---|---|---|
| **Twilio Voice `<Say>` + `<Gather>` DTMF** | $0.014/min outbound, billed per whole minute; TTS: basic voices free, Polly standard $0.0008/100 chars, neural $0.0032/100 chars; DTMF free; speech Gather $0.02/use | n/a (IVR) | `<Gather action=URL>` | One-way needs **no public URL** (inline `Twiml` parameter on Create Call). DTMF needs the Worker | **Phase 2** |
| **ElevenLabs Agents** + Twilio number | $0.08/min after 75 included minutes on Starter ($6); plus LLM (billed by ElevenLabs) and $0.014 Twilio | about 1 s | Server tools (webhooks with secret headers) | `register-call` from the Worker returns TwiML; tools hit the Worker | **Phase 3 pick** |
| Twilio ConversationRelay | $0.07 + $0.014 + your LLM ≈ $0.09/min | Depends on the LLM; about 1 s with Haiku-class models | Your own code on the WebSocket | Public WebSocket needed, e.g. a Worker Durable Object | Runner-up |
| Telnyx AI Assistants | about $0.06/min all-in plus telephony | about 1 s | Webhook tools | Public URL | Cheapest, but eKYC and a new vendor |
| Vapi | $0.05 platform + STT/LLM/TTS/telephony ≈ $0.11–0.33 | 0.5–1 s | Server URL tools | Public URL; outbound needs an imported Twilio/Telnyx number | Pricier, more knobs |
| Retell AI | $0.07 base, about $0.13 typical | about 0.6–1 s | Custom functions (webhook) | Public URL | Pricier |
| Bland | $0.14/min (Start plan) bundled | about 1 s | Webhooks | Public URL | Built for sales-dialer scale; no |
| OpenAI Realtime over SIP | about $0.05–0.15/min + Twilio SIP | Lowest (speech-to-speech) | Realtime function calls | `realtime.call.incoming` webhook needs a public URL; **no native outbound**, so Twilio would dial the SIP URI | DIY, too many parts |
| LiveKit Agents + SIP | $0.003–0.004/min SIP + model costs | Low | In-process: the agent runs **on the PC** and calls `127.0.0.1` directly (outbound WebSocket, NAT-friendly) | none needed | Needs pip packages, a separate process and a SIP trunk. LiveKit numbers are inbound-only. Too much babysitting |
| Claude Remote Control + Claude app | $0 (Pro/Max) | Chat | It *is* the agent | Outbound HTTPS polling by `claude` | Claude Code sessions only; push notifications yes; no PSTN, no Codex, no quiet hours |
| VoiceMode Connect (voicemode.dev) | Free/unclear | Higher than local | MCP `converse` per session | Cloud relay; the iOS or web client must be **open and connected** | Not agent-initiated ringing; docs still list multi-agent/device routing as open questions. Not a phone channel |

**Getting callbacks to a PC behind NAT.** Options compared:
- **Cloudflare Worker + SQLite Durable Object (recommended).** The free plan gives 100k requests/day and free SQLite storage. The PC pulls every 5–10 s, about 9k requests/day. Nothing is ever exposed on the PC.
- **Cloudflare Tunnel.** Free, and can use a stable hostname if a domain (e.g. capresta.ca) is on Cloudflare DNS. It exposes the bus itself to the internet, so it would need a separate listener.
- **Tailscale Funnel.** Free for personal use; same exposure issue.
- **ngrok free.** One static dev domain, 20k requests/month cap.
- **Supabase Edge Function + table.** Works, but it is a second platform for no gain.

**Remote Control verdict:** it already gives Cameron two-way *typed or dictated* control of Claude Code sessions, plus pushes when Claude decides or needs input. It cannot cover Codex agents or bus-wide status, and cannot place calls. Keep using it, but do not build on it.

**VoiceMode verdict:** the local VoiceMode install (Whisper STT) is useful as a **free transcriber for Telegram voice notes**, giving async voice replies for $0. VoiceMode Connect is not a way to ring the phone.

## 3. Architecture (all phases)

```
agents -> bus (127.0.0.1:8765, stdlib) --poll/push HTTPS--> Cloudflare Worker "relay" (policy gateway, holds Twilio/11L keys)
   ^            |  Telegram long-poll (send + getUpdates)            |  POST /Calls (only to owner_number)
   |            v                                                     v
   +---- commands (validated) <---- /pull queue <---- Twilio webhooks (signed) / ElevenLabs tools (secret header)
```
The PC never accepts inbound traffic. Twilio and ElevenLabs keys live only in Worker secrets, so agents that can read `config.json` cannot place arbitrary calls. The Worker hard-codes the owner number, quiet hours and daily caps as a second policy layer.

## Phase 1 (this week): Telegram text + reply

- **Services:** Telegram Bot API. **Cost: $0.** Optionally Pushover ($4.99 once) as the "loud" channel.
- **Accounts:** @BotFather `/newbot` gives the bot token. Cameron sends `/start` so the service can capture `chat_id` and `from.id`.
- **Integration:**
  - Add a poller thread: `getUpdates?timeout=50&offset=N&allowed_updates=["message","callback_query"]`.
  - Drop anything where `from.id` is not in `allowed_user_ids`.
  - Send with `reply_markup.inline_keyboard` (`ack:<event_id>`, `pause:<agent>`, `approve:<req_id>`) and answer taps with `answerCallbackQuery`.
  - Use a strict grammar: `/status`, `/pause <agent>`, `/resume <agent>`, `/approve <id> <PIN>`, `/deny <id>`. Free text becomes a `user_message` event with `source=telegram`, delivered to the named agent's inbox and never executed as a command.
  - Voice notes: `getFile`, then local Whisper, then the same grammar.
- **config.json:** `telegram: {bot_token, chat_id, allowed_user_ids:[...], poll_timeout:50}` and a `pin_hash` (salted PBKDF2 via stdlib `hashlib`).
- **Risks:**
  - The bot token on disk lets someone send *as* the bot but not command the bus, because of the user-ID check.
  - Irreversible approvals always need the PIN.
  - Updates older than 24 hours are dropped by Telegram, so the poller should run as a service.

## Phase 2: scheduled digest calls + urgent escalation

- **Services:** Twilio Voice (Canadian 519/226/548 local number), Cloudflare Worker (free), Telegram for fallback.
- **Cost (about 3 calls/day of about 2 minutes, plus about 20 texts/day on Telegram):**

  | Item | Monthly cost |
  |---|---|
  | Twilio number | $1.15 |
  | 180 min × $0.014 | $2.52 |
  | Neural TTS, about 60k chars | about $2 (or $0 with basic voices) |
  | A few urgent SMS | about $1 |
  | **Total** | **about $4–7** |

  The $20 minimum prepaid top-up lasts 3–4 months. Sending all 20 texts/day by SMS instead would add about $6–9/month.
- **Accounts:**
  - Twilio signup and upgrade with a card. A **$20 minimum top-up is unavoidable**, because trial accounts play a trial message, can only call verified numbers and expire after 30 days.
  - Buy the number (an agent can do this via the API).
  - Voice geo-permissions: **Canada only**.
  - API key (SK…), 2FA, a usage trigger at $10, auto-recharge **off**.
  - Cloudflare free account with one `wrangler login` click. An agent deploys the Worker, and Cameron types the PIN himself into `wrangler secret put`.
- **Call flow:**
  1. At 12:00 and 17:30 America/Toronto, the bus builds the digest from its SQLite event log. It is template-based, at most 900 characters, with agent names and states only, and no raw agent output.
  2. The bus sends `POST {relay}/call {kind:"digest", text}` with its bearer token.
  3. The Worker re-checks policy, then calls Twilio `Calls.json` with `To=owner_number`, `Url={relay}/twilio/answer?id=…` and `StatusCallback`.
  4. The answer TwiML says: "Agent bus update. Press 1 to hear it." This beats voicemail with no machine-detection fee.
  5. After the summary, `<Gather numDigits=1>`: 1 = ack, 2 = repeat, 3 = pause all (asks for the PIN), 9 = talk (Phase 3).
  6. No answer or no key press → send the same digest to Telegram.
- **Worker endpoints:**
  - `POST /twilio/answer|gather|status|sms|voice` validate `X-Twilio-Signature` (HMAC-SHA1 of the full URL plus sorted params, keyed with the auth token).
  - `GET /pull` and `POST /ack` use the bus bearer token.
  - `POST /status` is where the bus pushes a status snapshot each minute for Phase 3.
- **Escalation ladder** (only for agents in `call_allowlist_agents`): urgent event → Telegram now → if not acked in 20 min, Pushover emergency or SMS → if still not acked in 20 min, and allowed, one call. Examples of urgent events: blocked on approval for more than 20 min, repeated failure, spend threshold, "needs keys".
- **Policy** (enforced in the bus; the Worker enforces the bold parts):
  - **No contact 21:00–08:30.** Events queue for the next digest.
  - Lecture blackout from a Google Calendar secret iCal URL, with a title regex such as `lecture|tutorial|lab|exam`. A static timetable is the fallback.
  - **Maximum 3 calls/day**, at least 90 minutes between calls, 1 urgent call per agent per day, 10 SMS per day.
  - No break-glass override unless Cameron adds one himself.
  - Every decision is logged with its reason (`GET /phone/policy`).

## Phase 3: two-way voice ("yes go ahead", "pause the pecunia agent", "what's the status")

- **Services:** ElevenLabs Agents, Starter plan $6/month with 75 minutes included and $0.08/min after. The LLM is chosen inside ElevenLabs. It uses the same Twilio number and Worker.
- **Cost:** about 60 conversational minutes/month gives about $6 + $1 LLM + $0.84 telephony, so **about $12–15/month in total with Phase 2**. Making every daily call conversational (180 minutes) pushes it to about $20 or more, so keep digests as cheap `<Say>` and make talking opt-in (press 9, or he calls in).
- **Flow:**
  - **Inbound:** he calls the number and hits `/twilio/voice`. If `From` is not the owner number, the Worker returns `<Reject/>`. Otherwise it gathers a 4–6 digit PIN on the keypad.
  - **Outbound:** the digest menu option 9 leads to the same PIN gather.
  - On success, the Worker marks the `CallSid` as verified in the Durable Object. It then calls ElevenLabs `register-call` with `direction` and `conversation_initiation_client_data`, which carries the status snapshot as a *data* variable. It returns that TwiML to Twilio.
  - The PIN never passes through the LLM. Three wrong PINs lock voice commands for an hour and send a Telegram alert.
- **Agent tools (server webhooks with a secret header, all to the Worker):**
  - `get_status()` returns the latest snapshot.
  - `send_command(agent, action∈{pause,resume,ack,note}, text?)` works only if the call's `CallSid` is verified, `agent` is on the allow-list, and the rate cap is not exceeded. It is queued for the bus.
  - **No approve or irreversible actions by voice.** Those go through the Telegram button or keypad plus PIN.
  - `note` is delivered to the agent's inbox as a user message tagged `source=voice, verified=pin`.
- **config.json additions (bus side):**
  - `phone: {timezone, quiet_hours, blackout_ics_url, blackout_title_regex, digest_times, limits{...}, call_allowlist_agents, escalation{...}}`
  - `relay: {url, token}`
  - Twilio and ElevenLabs keys are **only in Worker secrets**: `TWILIO_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `OWNER_NUMBER`, `PIN_HASH`, `RELAY_TOKEN`, `ELEVEN_API_KEY`, `ELEVEN_AGENT_ID`, `TOOL_SECRET`.

## Risks and mitigations

- **Toll fraud:**
  - Keys stay off the PC.
  - The Worker only ever dials `OWNER_NUMBER`.
  - Voice geo-permissions are Canada-only.
  - Prepaid balance is low with auto-recharge off.
  - A usage trigger fires at $10.
  - 2FA is on the Twilio account.
- **Spoofed caller ID or SMS sender:**
  - Caller ID is never treated as authentication on its own. It must be the owner number *and* the keypad PIN.
  - SMS commands need `PIN cmd`.
  - Telegram relies on its authenticated `from.id`.
- **Prompt injection:**
  - Agent output reaches the voice LLM only as a capped, URL-stripped status snapshot marked as data.
  - Tools are a small enum, re-validated in the Worker *and* on the bus.
  - A malicious status line can at worst cause a reversible pause, which the Worker rate-limits.
  - Spoken or inbound text is never executed. It becomes an inbox note for an allow-listed agent.
- **Calls at bad times:**
  - Quiet hours are checked in two places (bus and Worker).
  - Daily caps apply.
  - The phone's own Focus/Sleep schedule stays on as a third guard.
- **Spam labelling or missed calls:**
  - Save the Twilio number as the contact "Agent Bus".
  - Carriers may flag unknown A2P numbers.
  - Unanswered calls fall back to Telegram.
- **Voicemail leakage:** "press 1 to hear it" gates the content.
- **Vendor lock-in or shutdown:** each layer can be swapped for its runner-up (ConversationRelay or Telnyx) behind the same Worker API.

## What I need from Cameron

1. **Telegram (Phase 1, 2 min):** create a bot with @BotFather, paste the token into `config.json`, and send `/start` to the bot.
2. **Rules:** confirm quiet hours (proposed 21:00–08:30), the digest times (12:00 and 17:30), and which agents may call. Share the Google Calendar **secret iCal URL** of the calendar that holds lectures.
3. **Twilio (Phase 2, unavoidable signup):**
   - Create the account, verify email and phone, and turn on 2FA.
   - Upgrade with a card and a $20 top-up, with auto-recharge off.
   - Everything after that (number, geo-permissions, API key, usage trigger) can be scripted by an agent from the console credentials he pastes once.
4. **Cloudflare (Phase 2):** a free account and one `wrangler login` browser click. He types the phone PIN into `wrangler secret put PIN_HASH_INPUT` himself.
5. **Phone setup:** save the Twilio number as the contact "Agent Bus". Decide whether it may ring during Do Not Disturb (it should not during Sleep).
6. **ElevenLabs (Phase 3):** account, Starter plan ($6/month) and an API key.
7. **Only if he insists on real SMS:** be ready for Twilio **sole-proprietor A2P registration**, which needs personal details, a mobile OTP within 24 hours and possibly weeks of vetting. Toll-free verification is the alternative.

No business registration or ID-document KYC is needed on the recommended path. Telnyx would require eKYC.

## Sources (read 2026-09-30)

- Twilio SMS pricing, Canada ($0.0083/msg, $1.15/mo number; "carrier fees may apply"): https://www.twilio.com/en-us/sms/pricing/ca
- Twilio Voice pricing, Canada (page dated Aug 2026: $0.014/min outbound, TTS tiers, Gather, ConversationRelay $0.07/min): https://www.twilio.com/en-us/voice/pricing/ca
- Twilio Canada SMS guidelines (domestic long code: no registration listed; toll-free: verification): https://www.twilio.com/en-us/guidelines/ca/sms
- Canadian long codes bought on or after 2025-03-26 need A2P/Persona (third party, unconfirmed by Twilio's page): https://mysupport.bloyal.com/hc/en-us/articles/52678533963411-SMS-Registration-Requirements-for-Canadian-Businesses
- Twilio Sole Proprietor A2P (Canadians without a BN, OTP, one number, weeks of vetting): https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/direct-sole-proprietor-registration-overview
- Twilio toll-free verification (required, free; BRN required from 2026-02-17 except sole proprietors): https://help.twilio.com/articles/5377174717595-Toll-Free-Message-Verification-for-US-Canada and https://www.twilio.com/en-us/legal/service-country-specific-terms/change-log
- Twilio trial limits (verified recipients only, 30 days): https://www.twilio.com/docs/usage/tutorials/how-to-use-your-free-trial-account ; $20 minimum upgrade: https://support.twilio.com/hc/en-us/articles/360036052753-Twilio-Free-Trial-Limitations
- Plivo Canada SMS ($0.0077; Canadian carrier surcharges): https://www.plivo.com/sms/pricing/ca/
- Vonage pass-through fees (Rogers/Fido CAD 0.0072 from 2026-04-01): https://api.support.vonage.com/hc/en-us/articles/5672262029084-SMS-MMS-Pass-through-Fees
- Telnyx messaging pricing: https://telnyx.com/pricing/messaging ; AI voice agents (about $0.06/min all-in): https://telnyx.com/pricing/conversational-ai ; eKYC: https://support.telnyx.com/en/articles/1130595-account-verification
- Bell Text-via-Email end of life 2025-12-31: https://www.txtimpact.com/blog/bell-email-to-sms-shutdown-alternative and https://business.bell.ca/web/SHOP/resources/PDF/Text-via-Email-service-end-of-life-FAQ.pdf ; other Canadian carriers: https://activitymessenger.com/blog/bell-discontinues-text-messaging-service-heres-an-alternative/ and https://productioncommunity.publicmobile.ca/t5/Get-Support/SMS-Gateway/td-p/1190513
- Google Voice in Canada (no SMS on Canadian numbers): https://www.quo.com/blog/google-voice-canada/
- WhatsApp API pricing 2026: https://respond.io/blog/whatsapp-business-api-pricing
- Pushover licensing ($4.99 one-time): https://pushover.net/licensing
- Telegram Bot API (getUpdates, 24-hour retention): https://core.telegram.org/bots/api
- ElevenLabs Agents pricing (Free 15 min; Starter $6 with 75 min; $0.08/min overage; LLM extra): https://elevenlabs.io/pricing/agents ; register-call: https://elevenlabs.io/docs/agents-platform/phone-numbers/twilio-integration/register-call ; outbound-call API: https://elevenlabs.io/docs/api-reference/twilio/outbound-call
- Vapi pricing and outbound: https://www.layer3labs.io/guides/vapi-pricing and https://docs.vapi.ai/calls/outbound-calling
- Retell pricing: https://www.layer3labs.io/guides/retell-ai-pricing ; Bland pricing: https://www.layer3labs.io/guides/bland-ai-pricing
- OpenAI Realtime SIP (no native outbound): https://developers.openai.com/api/docs/guides/voice-sip ; https://www.twilio.com/en-us/blog/developers/tutorials/product/openai-realtime-api-elastic-sip-trunking ; cost: https://www.layer3labs.io/guides/openai-realtime-api-pricing
- LiveKit pricing and phone numbers: https://livekit.com/pricing and https://docs.livekit.io/telephony/start/phone-numbers/
- Claude Code Remote Control (outbound-only, push notifications, Pro/Max): https://code.claude.com/docs/en/remote-control
- VoiceMode and Connect: https://voicemode.dev/ and https://github.com/mbailey/voicemode
- Cloudflare Workers and Durable Objects free tier: https://developers.cloudflare.com/workers/platform/pricing/ and https://developers.cloudflare.com/changelog/2025-04-07-durable-objects-free-tier/ ; tunnel comparison: https://ondelva.com/blog/2026/04/tailscale-vs-cloudflare-tunnel-vs-ngrok-2026
