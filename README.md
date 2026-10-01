# Local Notify

A tiny local API for agents and scripts to ping your phone. Python 3.11+, **no packages to install**. Runs on Windows, macOS, and Linux; background/startup scripts are provided for Windows.

```text
Agents / scripts ── POST /notify ── local SQLite queue ── (held while you're busy) ── ntfy / Telegram / WhatsApp
                                         └─ priority 4: + Telegram text · priority 5 / call:true: + phone call (gateway)
All agents on this PC ── Agent Bus (/bus/*, /state, /ui) ── roster, broadcasts, DMs, help requests, your state
```

Phone notifications only come from things an agent sends **intentionally**. The old automatic "Claude finished",
"Claude needs attention", Codex-completion and usage alerts are removed (see [Automatic alerts](#automatic-alerts-removed)).

## Agent Bus

The same service hosts the **Agent Bus**: every Claude Code session (all `~\.claude*` config dirs), Codex, the Claude
desktop app and the study Claude can see who is working on what, broadcast, DM, hand out help requests (first claimer
wins) and follow your state (`studying`, `gym`, `sleeping`...), which also holds low-priority phone pings.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-AgentBus.ps1 -DryRun   # preview
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-AgentBus.ps1           # install (-Remove to undo)
python bus.py agents                                                                     # roster
```

Claude Code sessions join automatically through the `agent-bus` plugin (`plugin\`; restart running sessions or run
`/reload-plugins`), with `/agent-bus:mode`, `/agent-bus:agents` and `/agent-bus:say`. Codex uses the `agent-bus` skill
and `python bus.py --as codex-<name> ...`; the desktop app gets the `bus_*` MCP tools (`bus_mcp.py`). Dashboard:
http://127.0.0.1:8765/ui. Full guide: [docs/AGENT-BUS.md](docs/AGENT-BUS.md).

## Phone escalation: texts and calls

Priority 1–3 goes to ntfy only. Priority 4 also sends a Telegram text, and priority 5 (or `"call": true`) also rings
your phone and reads the message aloud, with 1 = acknowledge and 2 = snooze 10 minutes. Your state still holds
things first, but `call: true` gets through every state, sleeping included. Calls go through a free Cloudflare Worker
(`gateway\`) that holds the Twilio keys. The agent that asked hears the outcome on the bus.

```powershell
python notify.py setup phone        # Telegram bot, gateway Worker, Twilio number: asks for keys in hidden prompts
.\Start-Notify.ps1 -Restart
python notify.py phone test         # priority-4 test text;  phone test --call  for a test call
python notify.py phone status       # what is configured; phone calls / phone ack <id>
```

Before the gateway is set up, calls are stored as `dry-run` so you can see what would have rung. Guide (ladder,
keypad, costs of about US$2/month, setup checklist, troubleshooting): [docs/PHONE-ESCALATION.md](docs/PHONE-ESCALATION.md).
Gateway API, also used by the planner server: [docs/PHONE-GATEWAY.md](docs/PHONE-GATEWAY.md).

## Windows service: start at boot

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-Service.ps1
```

Accept the Windows administrator prompt. This installs **LocalNotify** with Automatic startup, so it starts at boot before anyone logs in. It uses a dedicated virtual service account (`NT SERVICE\LocalNotify`), needs no user password, and retries startup after process failures. The installer gives that account read access to this project, the Python installation and your Codex session directory, plus write access to this project's `data/` directory. It removes the older login shortcut only after the service passes its HTTP health check.

The Python service host uses Windows' built-in Service Control Manager API; no wrapper executable or extra Python package is downloaded. The service preserves the existing configuration, history, notification destination and recap schedule. Its Codex path is resolved to your user directory at installation, since the service has a separate profile.

```powershell
Get-Service LocalNotify
.\Start-Notify.ps1 -Restart  # Restart the service and reload config
.\Stop-Notify.ps1           # Stop it until started again or the next boot
.\Start-Notify.ps1          # Start it again
python notify.py status     # runtime should be windows-service
```

The installer allows your Windows account to query, start and stop this service without further elevation. Installation, removal and service reconfiguration require administrator access. To remove it:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-Service.ps1 -Remove
```

Removal revokes the service's file access and leaves your settings and history intact. It does not reinstall login startup. Installation outcome is in `data/service-install-result.json`; details are in `data/service-install.log`. Keep the project and Python installation at their installed paths, or remove and reinstall the service after moving them.

## Quick start without a Windows service

From this folder:

```powershell
python notify.py setup ntfy
.\Start-Notify.ps1
python notify.py send "Your local notifications work" --title "Hello from your PC"
```

The setup command prints a subscription URL. Install the [ntfy phone app](https://docs.ntfy.sh/subscribe/phone/), add a subscription with that server and topic, and allow notifications. No ntfy account is required for an unreserved topic. On public ntfy, **anyone who knows the topic can read and publish to it**; the generated 128-bit random topic is a secret, not an authenticated private channel. You can configure a protected topic and token instead.

Use `python notify.py setup local` for local history only. This does not display desktop toasts or send phone messages. Switching providers affects new events; existing local history is not sent retroactively. Restart after changing configuration.

```powershell
.\Start-Notify.ps1 -Restart      # Reload configuration
.\Stop-Notify.ps1               # Stop the background service
.\Install-Startup.ps1           # Optional: start hidden when you log into Windows
.\Install-Startup.ps1 -Remove   # Undo start at login
python notify.py serve          # Foreground alternative; Ctrl+C to stop
python notify.py status         # Provider, queue, watcher and observed Codex allowance
```

If PowerShell blocks scripts, invoke one with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Start-Notify.ps1`. This applies to that invocation only.

If the Windows service is installed, the Start/Stop scripts control that service. Otherwise, they manage a standalone background process. The login shortcut is an alternative to the boot service.

## Give these instructions to another agent

- [AGENTS-NOTIFICATIONS.md](AGENTS-NOTIFICATIONS.md) is a standalone instruction file. Attach it to an agent conversation or paste its contents into a project's `AGENTS.md` or `CLAUDE.md`.
- [skills/notify-user/SKILL.md](skills/notify-user/SKILL.md) is a reusable `notify-user` skill. It explains when to send a custom alert, the API, deduplication and handling failures.
- Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-Agent-Skill.ps1` to install it into both `~/.codex/skills/notify-user` and `~/.claude/skills/notify-user`. Existing edited versions are backed up. New agent sessions can discover it; invoke `$notify-user` in Codex or `/notify-user` in Claude Code. (`Install-AgentBus.ps1` also refreshes these copies.)
- [skills/agent-bus/SKILL.md](skills/agent-bus/SKILL.md) teaches agents to use the Agent Bus; the plugin ships it for Claude Code and the installer copies it to Codex.

The skill does not contain the ntfy topic or provider credentials. There are no automatic completion alerts any more; every notification is one an agent chooses to send, and the user's bus state can hold low-priority ones.

## The endpoint

`POST http://127.0.0.1:8765/notify` (`/notify/` also works). Only `message` is required.

```powershell
Invoke-RestMethod http://127.0.0.1:8765/notify/ -Method Post `
  -ContentType 'application/json' `
  -Body '{"message":"Build finished","title":"Codex","source":"codex","priority":3}'
```

Or use the helper from any directory:

```powershell
python C:\Users\camer\Desktop\Projects\notifications\notify.py send "Tests passed" --title "Build finished"
```

Node / any JavaScript agent with a local runtime:

```javascript
await fetch('http://127.0.0.1:8765/notify', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ title: 'Agent finished', message: 'The report is ready.' }),
});
```

| Field | Default | Notes |
| --- | --- | --- |
| `message` | required | Up to 3,000 characters |
| `title` | `Notification` | Up to 150 characters |
| `source` | `agent` | App or agent name, up to 80 characters |
| `event` | `message` | Such as `done`, `attention`, or `usage_low` |
| `priority` | `3` | Integer 1–5; ntfy maps these to phone notification priorities. 4 also texts (Telegram), 5 also calls |
| `dedupe_key` | none | Optional unique event identifier, up to 300 characters |
| `call` | none | `true` rings the phone (implies priority 5; emergencies only); `false` never rings |

A successful request returns HTTP 202 after the event is saved, before phone delivery:

```json
{"id":"...","status":"queued","provider":"ntfy","duplicate":false}
```

While you have an Agent Bus state set (for example `studying`), a notification below that state's minimum priority is stored but **not delivered**: HTTP 202 with `"status":"held","suppressed":true` and a `reason`. `GET /bus/held` lists them.

When a notification escalates, the answer also has `"escalation": {"text": "queued", "call": "queued"}` (only the
parts that applied; a value can also be `dry-run: …` or `skipped: …` with the reason). Held and duplicate notifications
never escalate.

A duplicate key returns the original event with HTTP 200. Keys stay reserved as long as the history row is retained (30 days by default). Notification IDs and status are available through `/events`.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Service identity, provider, queue counts, Codex watcher and last observed usage |
| `GET /events?limit=50` | Recent notifications and delivery status (maximum 200) |
| `GET /recap?hours=24` | Preview a recap without sending |
| `POST /recap` | Send a recap; JSON body `{"hours":24}` |
| `GET /phone/calls?limit=50` | Phone calls and their outcome (`acked`, `snoozed`, `unanswered`, `failed`, `dry-run`...) |
| `POST /phone/calls/<id>/ack` | Acknowledge a call from the PC (stops its retries); body `{}` |
| `/bus/*`, `/state`, `/ui` | Agent Bus API, your state and the dashboard (see [docs/AGENT-BUS.md](docs/AGENT-BUS.md); `GET /` lists them) |

`local` means saved locally; `queued` means pending; `accepted` means the provider accepted the message; `failed` means delivery was rejected or retries were exhausted. Provider acceptance does **not** prove the phone displayed or read it. Transient network errors, HTTP 429 and 5xx responses retry up to five attempts, including after a service restart. A timeout after provider acceptance may produce a duplicate: delivery is at least once, not exactly once. Pending messages are kept until accepted or failed; completed history expires after `retention_days`.

The API binds only to `127.0.0.1`, rejects browser-origin requests and non-local Host headers, and limits request bodies. Normal local scripts need no token. Set `api_token` in `config.json` if desired; then direct API calls need `Authorization: Bearer <token>`. The CLI and hooks read the token automatically. Remote/cloud agents need a local execution bridge to reach this endpoint; it is not exposed to the internet.

## Automatic alerts (removed)

Automatic phone pings are gone: the Claude Code `Stop`/`Notification` hooks are no longer installed (`python notify.py hooks install` only prints a note; `python notify.py hooks remove`, which `Install-AgentBus.ps1` runs, deletes old ones), the Codex watcher is disabled in `config.json` (`codex.enabled: false`) and would only notify with `codex.auto_notify: true`, the `notify.py hook claude|codex` bridge only forwards with `claude.auto_notify` / `codex.auto_notify` set, and the daily recap is off (`recap.daily_at: ""`). Those sections below describe the opt-in behaviour if you ever turn it back on.

## Codex notifications (opt-in)

With `codex.enabled` and `codex.auto_notify` both `true`, the server watches `~/.codex/sessions` (or `CODEX_HOME/sessions`). It handles the `task_complete` and `token_count` events observed in this machine's Codex 0.154.0 logs:

- A main Codex turn completes: send an app/project notification.
- Remaining allowance reaches 20%, 10%, or 5%: send a usage warning once per threshold/reset window. These values mean **remaining allowance**, not CPU usage or context remaining.
- Subagent completions are filtered out. Existing completion history is skipped on first startup; saved cursors resume recent events after restarts. Events more than five minutes older than service startup are skipped.
- Usage reflects the last logged snapshot, not a live account query. The status includes `observed_at` and `resets_at`; idle or signed-out clients may stop updating it. Snapshots older than 24 hours or with expired reset windows do not trigger warnings.

No Codex settings changes are needed. This is a best-effort integration with a local log format that can change; a Codex build that does not write these events will need another adapter. The installed desktop app currently writes these events, as does the local CLI. This does not watch remote/cloud sessions without local logs.

Codex also documents a [native `notify` hook](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications) for `agent-turn-complete`. The bridge supports it if you prefer that path:

```toml
# At the TOP LEVEL of ~/.codex/config.toml (before any [table])
notify = ["C:/Users/camer/AppData/Local/Programs/Python/Python312/python.exe", "C:/Users/camer/Desktop/Projects/notifications/notify.py", "hook", "codex"]
```

Use your actual Python and project paths. This installer does not modify Codex's config. If both the watcher and native hook emit an event with the same thread/turn IDs, the deduplication key suppresses a duplicate.

## Claude Code notifications (removed)

```powershell
python notify.py hooks remove   # delete the old Stop/Notification phone hooks (backs up settings.json)
```

Claude Code sessions now talk to the service through the `agent-bus` plugin (Agent Bus hooks, no phone pings). `python notify.py hooks install` no longer installs anything. The `notify.py hook claude` bridge still exists for manual setups and only forwards when `claude.auto_notify` is `true`; the notes below describe that opt-in behaviour.

The [Claude hooks reference](https://code.claude.com/docs/en/hooks) documents `Stop` for finished responses and `Notification` for attention events such as permissions. The bridge ignores continuation stops and stops with outstanding background tasks or scheduled wakeups, and it uses `last_assistant_message` when summaries are enabled. It never blocks completion on a notification failure. If the service is off, a hook logs a failure in `data/hooks.log`; that event is not queued for later delivery.

By default, completions send the app/project name and a short generic message. To include the end-of-turn text, change `codex.include_summary` and/or `claude.include_summary` to `true` in `config.json`. This sends up to 2,500 characters of that text through your configured provider. Claude attention notifications forward the notification message.

## Recaps

```powershell
python notify.py recap                 # Preview last 24 hours
python notify.py recap --hours 8 --send # Send to your phone
```

The automatic daily recap is off (`recap.daily_at` is `""`). To turn it back on, set `recap.daily_at` to `"18:00"` and restart the service. Uses the computer's local time. Sends once per day at or after the chosen time while running (including a late startup); skips days with no recent notifications. A recap counts stored events and includes the latest 10 title/message excerpts; it does not read your full conversations or use an AI summarizer. Enable completion summaries if you want the work details included. `recap.hours` controls the lookback window.

## Telegram or WhatsApp

**Telegram:** create a bot with Telegram's @BotFather, then run `python notify.py setup telegram`. It asks for the bot token (hidden), waits while you press Start in the bot's chat, saves `telegram.bot_token` and `telegram.chat_id` in `config.json` and sends a confirmation text. ntfy stays the main provider (Telegram is then the priority-4+ text channel); add `--primary` to make Telegram the main provider instead. Restart the service. Delivery uses [sendMessage](https://core.telegram.org/bots/api#sendmessage).

**WhatsApp through Twilio:** run `python notify.py setup whatsapp`, then set the account SID, auth token, sender and your recipient in `config.json`. Phone numbers must use `whatsapp:+COUNTRYNUMBER`. Restart after filling them in. For testing, join your account's [Twilio WhatsApp Sandbox](https://www.twilio.com/docs/whatsapp/sandbox) from your phone. Arbitrary text works during the 24-hour window after you message the sender. The sandbox session itself also expires and must be rejoined according to Twilio's current rules.

For unattended WhatsApp alerts outside that window, use a registered sender and an approved template. Set `whatsapp.content_sid` to its Content SID. This adapter expects template variable `1` for the title and `2` for the message. The sandbox's pre-approved templates do not provide a general-purpose notification template; custom templates require a registered sender. Twilio/WhatsApp charges and template approval apply. The adapter uses the [Twilio Messages API](https://www.twilio.com/docs/messaging/api/message-resource); no Twilio SDK or inbound webhook is required to send.

## Files and checks

`config.json` contains local settings and credentials. `data/` contains SQLite history, delivery logs, hook logs, PID and settings backups. Both are gitignored. Do not share them as part of a source archive. The service reads neither Codex nor Claude authentication files.

```powershell
python -m unittest discover -s tests -v
```

Tests exercise the actual HTTP server, phone escalation against a fake gateway, the interactive setup with fake prompts and network, input validation, local access restrictions, durable queue/retries, concurrent deduplication, recap scheduling, log rotation/partial writes, usage thresholds, hook installation, and provider request formats. They use temporary data and mocked provider calls; they do not send real messages.
