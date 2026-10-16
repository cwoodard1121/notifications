# Phone calls: the last step (Twilio)

Everything else is already done: the gateway Worker is live (`agent-bus-phone`, not dry-run), and the planner server
and this PC both have gateway tokens. Only Twilio is missing. Takes about 10 minutes.

## 1. Twilio account (trial is fine)

1. Sign up at https://www.twilio.com/try-twilio. Signup verifies your cell by SMS code, and that's the only number the
   gateway ever calls, so a **trial account works**.
2. Upgrading is optional (Console > Billing > Upgrade, US$20 prepay, about CA$28). It only removes the trial
   extras: on trial, each call first plays a short "Twilio trial account" notice and asks you to **press any key**
   before your message, and texts start with a trial prefix. Trial credit (about US$15) pays for the number and
   calls until it runs out.

## 2. Copy two values

Console home page (https://console.twilio.com), **Account Info** box:
- **Account SID** (starts with `AC`)
- **Auth Token** (click "show")

## 3. Run the setup (PowerShell)

```powershell
cd C:\Users\camer\Desktop\Projects\notifications
python notify.py setup twilio
```

It asks, in order:
1. **Account SID**: paste.
2. **Auth Token**: paste (nothing shows while you paste; that's normal). On a trial it prints a TRIAL warning, which
   you can ignore.
3. If you don't own a number yet: **"Buy +1 519/226/548 ... for about US$1.15/month? [y/N]"**: type `y`.
4. **Your cell number**: e.g. `519 555 0123` or `+15195550123` (E.164). Use the same cell you verified at signup.

It uploads the keys to the Worker and prints the health check. Save the Twilio number in your contacts as
**Agent Bus** so calls show a name and get past "Silence unknown callers" / Focus modes. Add it to your Do Not
Disturb exceptions if you want it to ring through.

## 4. Test call

```powershell
python notify.py phone test --call
```

Your phone rings (on a trial, press any key after the notice). Press **1**. The terminal prints `It works.`
Optional SMS check: `python notify.py phone test`.

## What a call says

"Yo, you have to lock in on this now." Then what the planner wrote for that item, then:

| Key | Says | What happens |
|---|---|---|
| 1 | "Press 1 if you're on it." | acknowledged; it stops calling |
| 2 | "Press 2 to snooze ten minutes." | calls again in 10 min (up to 3 snoozes) |
| 3 | "Press 3 to hear it again." | repeats |
| 9 | "Press 9 if it's already done." | marked done; the planner ticks the item off |

**How it chases you:** it **texts first** (same message, "Reply 1 = on it, 2 = snooze 10 min, 9 = done"), then
calls about 45 s later. If you don't pick up, or voicemail does (it hangs up on voicemail without leaving anything),
it **calls again every 2 minutes**, and re-texts every 3rd missed call so the text stays on top. Normal alerts stop
after 5 calls (about 10 min). **Urgent ones keep going until you press 1/2/9 or text back 1/2/9**, or the planner
sees you started, for at most 60 min / 25 calls. Snooze (2) pauses 10 min, then the calls resume.
**Calling the Twilio number yourself** reads the latest pending alert with the same keys, or "Nothing urgent right
now". You can't talk to it; it's keys only.

## When the planner calls you

**Calls work with your PC off.** The path is planner server (DigitalOcean droplet) -> gateway (Cloudflare Worker) ->
Twilio -> your phone. Your key presses and text replies go back the same way. None of it touches this PC, the local
Agent Bus or a Claude session. The droplet restarts its containers on its own after a reboot. Only the *agents'*
alerts (`notify.py`, bus) start on this PC.

Calls are **opt-in per item**: you turn calling on for an item in the planner. The **"Auto-call T1"** toggle (off by
default) turns it on for every top-tier item. For an item with calls on:
- **Midterms:** the night before, and 90 min before.
- **Deliverables worth 5% or more:** 3 h, 1 h and 20 min before they're due.
- **Top-priority study block:** 10 min after it should have started, if you haven't started it.
- **Savva package:** Sunday 11:00 and 12:30.

Urgent (T1 / wake-up) calls redial every 2 min until you press 1, 2 or 9 (up to 60 min per alert). No calls 00:30 to 08:30 (the planner decides that).
Caps: gateway 30 call alerts per client per day, 100 dials per day in total. This PC's agents: 6/hour, 20/day.

## Canadian SMS caveat

Calls work right away. Texts from a Canadian local number sometimes get filtered by carriers or need extra
registration (Twilio's rules change; the console tells you if your number needs it). If texts don't arrive, check
Console > Monitor > Logs > Messaging for the error code. Calls aren't affected.

## Cost (approx.)

| Item | Per month |
|---|---|
| Canadian local number (voice + SMS) | US$1.15 (about CA$1.60) |
| Calls: about US$0.014/min plus about 1 cent of neural TTS per answered call. Unanswered calls are usually not billed | a few dollars |
| Texts: about 1.3 cents each (sent or received) | cents |
| **Typical (a few calls a day)** | **about US$3 to 5 (CA$4 to 7)** |
| **Worst case (20+ answered calls every day)** | **about US$15 (CA$20)** |

Trial credit or the US$20 prepay covers the first few months. Turn on auto-recharge or a low-balance email under
Billing so the calls don't stop when it runs out.
