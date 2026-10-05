# Agent Console: remote control for every session, through your own bus

One place to watch and drive every Claude Code session you have running, on every account (`main`, `pecunia`,
`appraiseme`...) and on both PCs. It doesn't depend on Claude's Remote Control, so it doesn't care which account a session
belongs to.

- **Browser:** http://127.0.0.1:8765/console. A sidebar lists the sessions, grouped by PC, and the main pane is a live
  replica of the session's terminal (`›` your prompts, `●` replies and tool calls, `⎿` results; click a result to
  expand it). The prompt box at the bottom works like Claude Code's: <kbd>Enter</kbd> sends, <kbd>Shift</kbd>+<kbd>Enter</kbd>
  adds a new line, <kbd>↑</kbd> recalls your last prompt, and <kbd>Ctrl</kbd>+<kbd>K</kbd> finds a session.
- **Terminal, ssh style:** `python bus.py sessions` lists them, `python bus.py attach <id|name|folder>` (or `attach` alone to
  pick one from a list) streams the session into your terminal and sends every line you type. `--watch` makes it
  read only; `/exit` or Ctrl+C detaches.

```text
 browser /console ─┐                    ┌─ ~\.claude*\projects\<proj>\<session>.jsonl   (read only)
 bus.py attach  ───┼─ Local Notify :8765┤
                   │   /console/*       └─ prompt = bus DM from "user", meta.console ─▶ plugin hooks ─▶ the session
 other PC ◀── encrypted frames ──▶ planner relay ◀── encrypted frames ──▶ this PC   (console-<machine> pseudo-agents)
```

## How a prompt reaches a session

Claude Code has no API for typing into a running interactive session, so a console prompt uses the Agent Bus hooks the
plugin already installs. It arrives framed as your own words (`[Agent Console m…] Cameron typed this prompt…`), never as
an untrusted peer message:

| Session is… | Delivery |
| --- | --- |
| working | at its next tool call (PreToolUse context), or when the turn ends (Stop hook keeps it going) |
| idle, after at least one turn | **woken right away** by the background `bus.py rewake` hook, which stays up to 12 h after each turn, in every state including `sleeping`, with no wake cap for your prompts |
| idle, brand new or idle for more than 12 h | queued: it sees the prompt with its next turn (the console says so) |

What the console can't do (terminal-only): answer permission dialogs, press Esc to interrupt, or run slash commands.
In the transcript your prompt appears as hook feedback, not as a typed prompt; the console shows it as `› … via console`.
Only `POST /console/prompt` can create console prompts: `/bus/send` refuses `meta.console`, and a relayed message
loses the marker unless it came through the encrypted channel below.

## One-time setup on each PC

1. **Restart the service** to load the new code: `.\Start-Notify.ps1 -Restart`.
2. **Let the service read transcripts.** The Windows service runs as `NT SERVICE\LocalNotify`, which can't read your
   profile. Run `powershell -ExecutionPolicy Bypass -File .\Grant-ConsoleAccess.ps1` once and accept the admin prompt.
   It grants read-only (RX) access on each `~\.claude*\projects` folder. Re-run it after adding a new Claude config
   dir; `-Remove` takes the access away (as does `Install-Service.ps1 -Remove`). Sessions started through a per-project
   profile (`CLAUDE_CONFIG_DIR=<project>\.claude`) keep their transcripts in that project: the SessionStart hook grants
   the service the same read-only access to `<project>\.claude\projects` the first time a session starts there (no
   admin prompt, recorded in `data\console-acl.json`), and `python bus.py console-grant` does it for every folder seen so far. A standalone service (`notify.py serve`)
   runs as you and needs nothing.
3. **Cross-PC key** (only for the other PC's sessions): `python notify.py console key`, then restart the service. It reads
   your SSH private key (`~\.ssh\id_ed25519`, or `--file`) **as you**, derives an encryption key from the file, saves
   it as `console.key` in `config.json` and prints a **key id**. Both PCs must show the same key id (this PC:
   `1988555546a7`). The passphrase isn't needed, because the key file itself is the shared secret. If the ids differ,
   the files differ (for example, a passphrase was changed on one PC): copy one file over the other and run it again.
4. `python notify.py console status` shows the URL, key id, and what is being watched or served.

## The other PC: end-to-end encrypted

With the relay paired (`docs/RELAY.md`) and the same key id on both PCs, each service joins the bus as
`console-<machine>`, and the other PC's sessions become clickable in the console (and in `bus.py attach`). Traffic
rides ordinary relay DMs between those two pseudo-agents, but every body is a sealed frame:

- Key: HKDF-SHA256 of the decoded private key file. Encryption: HMAC-SHA256 in counter mode, then an HMAC-SHA256 tag
  over the version, sender→recipient PCs, nonce and ciphertext (encrypt-then-MAC; standard-library Python only, no
  packages). Frames older than 15 minutes, replayed frames, frames for another PC, and anything that doesn't authenticate
  are dropped and logged.
- The planner server stores and forwards ciphertext only. It can see that the two PCs exchanged messages, their size and
  time, but not what a session said, and it can't inject prompts. The frames expire after 15 minutes and are
  removed from each PC's database once read; they are hidden from `bus.py history` and the dashboard. The planner's
  own Agents page may still list them as unreadable DMs between `console-…` ids.
- Viewing uses a lease: the viewer asks for a session, the host sends a snapshot (last 150 events, long tool output
  trimmed to 1,500 characters) and then pushes new lines about once a second while you keep watching (renewed every ~45 s).
  Expect about a second of lag through the relay.

## API

| Route | |
| --- | --- |
| `GET /console` | the app |
| `GET /console/sessions` | every session (both PCs) with `viewable` (transcript readable) and `reachable` (a prompt is delivered now) |
| `GET /console/events?agent=<id>` | the latest events (`cursor`, `start`, `earlier`, `pending` prompts, `state`) |
| `GET /console/events?agent=<id>&after=<cursor>&wait=25` | long-poll for new events |
| `GET /console/events?agent=<id>&before=<start>` | older events |
| `POST /console/prompt` `{"agent": "<id>", "text": "…"}` | send a prompt (≤ 4000 characters); the answer says how it will be delivered |

## Security notes

- The service only listens on 127.0.0.1 and refuses requests from other sites. As with the rest of the bus, **any process
  on this PC** can call these routes (set `api_token` to require a token). The console adds the ability to read session
  transcripts and to prompt sessions as you, which a local process running as you could do anyway.
- `Grant-ConsoleAccess.ps1` gives the service account read-only access to your transcripts and nothing else.
- Whoever holds the SSH private key file can talk to the encrypted channel. That's already the bar for your servers.
  Rotating the SSH key means running `notify.py console key` again on both PCs.
