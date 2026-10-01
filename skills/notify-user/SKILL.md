---
name: notify-user
description: Send the user a short phone notification through their local notification service when they request an alert, need to answer a blocking question, or want a significant work update or recap. Nothing pings automatically; only send intentional, important notifications and respect holds from the user's current state.
---

# Notify the user

This computer runs Local Notify at `http://127.0.0.1:8765`. The service routes messages to the user's configured phone destination. Use its local API; agents do not need ntfy, WhatsApp, or Telegram credentials.

## When to notify

- The user explicitly asks for a notification or recap.
- Work is blocked and the user needs to answer a question or take an action.
- A significant milestone merits an update during a long task, or finished work the user asked to be told about.

Nothing pings the phone automatically any more: there are no Claude Code completion/attention hooks, no Codex completion or usage alerts and no scheduled daily recap. Every phone notification is one an agent sends **intentionally**, so send only what the user asked for or truly needs; never a routine "done". Claude's own PushNotification tool is a phone ping too and follows the same rules.

Keep messages concise: identify the project/task, state the result or blocker, and say what the user needs to do. Use priority `3` for normal updates and `4` for attention needed. Reserve `5` for explicitly urgent events. Do not send secrets, credentials, or full transcripts. Notification delivery is an outbound side effect covered by the user's notification request, not permission for unrelated external actions.

## Texts and phone calls

Priority `4` also sends the user a Telegram text, so use it only when they must act. Priority `5` also **rings their phone** and reads the title and message aloud (keypad: 1 acknowledge, 2 snooze).

`call: true` rings the user's phone and reads the message aloud. It implies priority 5, so it gets through every state, sleeping included. Use it **only for genuine emergencies**: production is down, data or money is at risk, a hard deadline within about an hour that only they can act on, or they explicitly asked to be called. Never for "done", progress, questions, or to get past their state. `call: false` never rings, even at priority 5.

Send it with `--call` on either command below or `"call": true` in the JSON. The response's `escalation` field says what happened (`{"text": "queued", "call": "queued"}`, or `dry-run: ...` / `skipped: ...` with the reason). When sent through `bus.py notify` or the `bus_notify_phone` MCP tool, the outcome arrives later as an Agent Bus system message from `bus` ("acknowledged (pressed 1)", "snoozed... calls again around HH:MM", "did not pick up", "could not be placed"). Do not resend a call to get a different outcome; act on what you are told.

## The user's state can hold notifications

The user can set a state on the Agent Bus (studying, lecture, gym, meal, sleeping, focus, away). While one is active the service **holds** notifications below that state's minimum priority: studying, lecture, gym, focus and away pass priority `4` and `5`, meal passes `3`+, sleeping only `5`. A held notification returns HTTP `202` with `"status": "held"`, `"suppressed": true` and a `reason`; it is stored locally (`GET /bus/held`) and is **not** delivered. Do not raise the priority just to get around a hold; use `4` only when the user really needs to act now. Check the state first with `python C:\Users\camer\Desktop\Projects\notifications\bus.py state` (or `GET /state`). If the user is busy, keep your questions in a list and continue with work that does not need them. See the agent-bus skill for the rest of the bus.

## Preferred local command

This helper reads the service's local port and optional API token automatically:

```powershell
python C:\Users\camer\Desktop\Projects\notifications\notify.py send "The report is ready for review." --title "Research finished" --source agent --event milestone --priority 3
```

`python C:\Users\camer\Desktop\Projects\notifications\bus.py notify "..." --priority 4 --title "..."` does the same and prints plainly whether the notification was queued or HELD by the user's state, and whether a text or call was queued. Use it for calls so the outcome comes back to you.

Pass message text as an argument through your execution tool's normal quoting rules. Do not insert untrusted text into executable shell syntax. For dynamic text, use a structured HTTP request or a JSON file.

## Direct HTTP API

```powershell
$payload = @{
  title = 'Input needed: my-project'
  message = 'The implementation is ready. Please choose the deployment target in the agent chat.'
  source = 'agent'
  event = 'attention'
  priority = 4
  dedupe_key = 'my-project:task-123:deployment-question'
} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/notify/' -Method Post -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -TimeoutSec 5
```

Only `message` is required. Limits: message 3,000 characters; title 150; source/event 80; priority integer 1-5; optional `dedupe_key` 300; optional `call` true/false (see above). Choose a distinct stable key per task/event and reuse it if retrying that event. Keys stay reserved while the event remains in local history, normally 30 days.

HTTP `202` means saved/queued (or held, when the body says `"status": "held"`), not displayed on the phone. HTTP `200` with `duplicate: true` means the event was already recorded. The service performs provider retries; do not repeatedly resend a successfully queued request. `GET /events?limit=20` lists status: `local`, `queued`, `accepted` by the provider, `failed`, or `held`.

For a recap, preview with `GET /recap?hours=24`; send with `POST /recap` and JSON `{"hours":24}`. Recaps summarize stored notification events, not full conversations.

## If it fails

Check `GET /health`, or run `python C:\Users\camer\Desktop\Projects\notifications\notify.py status`. If direct HTTP returns `401`, use the local helper so it handles the API token. Do not print or copy credentials into the conversation.

Retry a failed connection once after a brief delay. If still unavailable, continue any independent work and report that the notification could not be queued. Do not change service configuration, destinations, firewalls, or install software just to deliver an alert. Never claim phone delivery based only on queue acceptance.

Loopback refers to this Windows PC. A remote/cloud agent needs execution on this PC to call it. WSL, containers, and sandboxes may have separate loopback networks; use an available Windows execution tool rather than assuming their `localhost` reaches this service.
