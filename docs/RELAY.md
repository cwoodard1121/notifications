# Agent Bus relay: two computers

Each PC runs its own Agent Bus (Local Notify, `127.0.0.1:8765`). With the relay on, the bus also pushes the messages
other PCs need to the planner server (https://146-190-249-220.sslip.io) and pulls the ones meant for it, over outbound
HTTPS. The server can never reach into a PC; nothing else changes on the PC (the API still listens on 127.0.0.1 only).

The full contract (server tables, routes, wire formats) is `docs/BUS-RELAY.md` in the planner repo
(`gcalfiller-uwo/app`); the step-by-step guide for the second PC is `docs/TWO-COMPUTERS.md` there.

```text
 PC "MSI"                               planner server                               PC "school"
 bus :8765 -- relay_client -- push -->  envelopes, machines, state  <-- pull -- relay_client -- bus :8765
           <-- pull (long-poll 25 s) --   /api/bus/relay/*           -- push -->
```

## Turn it on

This PC (the first one):

```powershell
python notify.py relay pair            # opens the planner in the browser; sign in, the tab says "Paired"
python notify.py relay status          # Relay: connected - this PC is MSI, server https://...
```

Options: `--machine NAME` (default: the Windows computer name, same as the Planner desktop uses), `--url URL`
(default: the one in `config.json`, else the planner server above), `--no-browser` (prints the link to open yourself,
within 5 minutes). `relay status --json` prints the raw health section. `relay unpair` clears the token in
`config.json` (then remove the computer in planner Settings > Computers to revoke it on the server).

Pairing opens a one-shot listener on `127.0.0.1:<random port>` with a random nonce. The planner's link page sends the
browser back to it with a one-time code; the CLI checks the nonce and the Host header, redeems the code for this PC's
**bus token** and writes `relay.url`, `relay.token` and `relay.machine` into `config.json`. The token is never printed
or logged. The Windows service reloads `config.json` within about 15 s; a standalone process (`Start-Notify.ps1`
without the service) needs `.\Start-Notify.ps1 -Restart`.

Another PC: unzip `agent-bus.zip` (planner Settings > Computers > Set up another computer) into
`C:\Users\<you>\Desktop\Projects\notifications`, then in that folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\Setup-RelayMachine.ps1
```

It installs Python 3.12 with winget if needed, creates `config.json` (`setup local`; it switches the study mailbox off
when its folder does not exist on that PC), installs the LocalNotify service (administrator prompt), the Agent Bus
clients (`Install-AgentBus.ps1`), pairs and prints `relay status`. Options: `-Server`, `-Machine`, `-SkipService`
(background process instead of the service), `-NoPair`. Safe to run again.

Config (`config.json`, gitignored; empty in `config.example.json`):

```json
"relay": { "url": "https://146-190-249-220.sslip.io", "token": "<set by relay pair>", "machine": "" }
```

On when `url` and `token` are set. `machine` empty = the computer name (`[A-Za-z0-9][A-Za-z0-9._-]{0,39}`). `url` must
be `https://...` (or `http://127.0.0.1:<port>` for tests). A bad section is refused, so the service keeps the old config.

## What crosses machines

Only messages written on this PC are pushed (relayed ones are never sent back). Per target token:

| Target | Sent to |
|---|---|
| `all`, `*`, `@account`, `#folder`, `tool:x`, `user` | every other PC |
| `planner`, `planner-app` | the server only (Cameron's planner threads) |
| an agent on this PC (id or name, ended ones too) | nobody (stays local) |
| an agent on another PC (id or name from the directory) | that PC |
| anything else (an id nobody knows) | every other PC |

A message with several targets goes to the union. **Never relayed:** phone notifications (each PC pings for its own
agents, so nothing rings twice), held notifications, `bus` system notices, `system` messages, expired messages, and
claims of help requests (a claim is per PC).

A relayed message keeps its id on every PC, so `reply <id>` and threads work across PCs. It arrives in
the normal inbox (hooks, MCP tools, `bus.py inbox`, study mailbox) labelled with its PC:
`from pecunia-2 (claude-9f3a1c22) on MSI`. Messages Cameron types on the planner's Agents page come from `planner`
("Cameron (planner)"), which is treated exactly like `user`; a PC never accepts `from: "planner"` unless the server
originated it, and no local client can send as `planner`. Peer messages stay untrusted information.

`python bus.py agents` (and the `bus_agents` MCP tool, `GET /bus/agents?remote=1`) list the other PCs' agents as
`MSI:pecunia/waitlist`; they show `offline` while their PC is offline. You can address them by id, name or a unique
prefix like local agents; a name on two PCs is ambiguous and the error lists the machines. `bus.py send` prints
`+ relayed to school: claude-...` when a message leaves the PC. `GET /bus/agents` without `?remote=1` and
`bus.py --json agents` (`agents` key) stay local-only; the JSON adds a separate `remote_agents` list.

## Your state

Your state (studying, gym, sleeping, ...) is shared: set it on either PC or the planner and every PC follows (and holds
low-priority phone pings). The server numbers every change; the last change the server accepted wins. A PC applies a
pulled state only if its version is newer and the PC has no change of its own still waiting to be sent. Expiry is not
relayed: every PC ends the state at the same `expires_at` by itself. On the first connection, when the server has no
state yet, a busy local state is pushed.

## How it runs

`relay_client.py`, started by the service when the relay is on (three daemon threads):

- **Push**: woken by every bus change (else every 2 s); 100 messages and at most 400 KB per request, plus the pending
  state. The cursor (`relay_push_seq`) moves only after the server answers 2xx, so nothing is lost while offline and the
  server's idempotent envelope ids drop retried duplicates. A message the server refuses (4xx) is logged and skipped.
- **Pull**: long-poll (25 s, client timeout 45 s), ingests with `INSERT OR IGNORE` (the same message twice is ignored),
  then applies the state.
- **Directory**: every 30 s posts this PC's live agents and replaces the `remote_agents` table with the other PCs'
  agents (rows older than 48 h are pruned if the relay stops).
- Errors back off 2 s to 60 s with jitter (and honour `Retry-After`). 401/403 = **unauthorized**: the token was revoked
  or replaced; it retries every 5 minutes until you pair again.
- A brand-new pairing (or a different server or machine name) starts at the server's current head: no history replay.

Cursors live in the bus database (`state` table): `relay_push_seq`, `relay_pull_seq`, `relay_state_version`,
`relay_state_pending`, `relay_identity`. Relayed rows carry `messages.origin` (NULL = written on this PC).

Health: `GET /health` (`bus.relay`), `GET /bus/relay` and `python notify.py relay status` show `enabled`, `status`
(`off`, `starting`, `connected`, `connecting` = no successful call for 90 s, `offline`, `error`, `unauthorized`), `machine`, `server` (origin only), `connected`
(a successful call in the last 90 s), `last_push_at`, `last_pull_at`, `last_directory_at`, `last_error`, the cursors,
`state_version`, `pending_state` (the mode waiting to be sent) and `remote_machines` (`id`, `online`, `agents`). Never
the token.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Relay: off` | Not paired: `python notify.py relay pair`. |
| `unauthorized` | The computer was removed or paired again elsewhere: `python notify.py relay pair`. |
| `offline` / `last error: URLError` | No internet or the server is down; messages wait in the local database and go out when it is back. |
| `the running service has no relay support yet` | The service runs older code: update this folder; the Windows service restarts itself on `.py` changes (standalone: `.\Start-Notify.ps1 -Restart`). |
| Paired but `status` stays `starting` / `Relay: off` | Wait 15 s for the service to reload `config.json`; check `data\service.log` for `Relay client started`. |
| Other PC missing from `relay status` | It has not posted its agents yet (not paired, off, or offline for long). |
| The pairing tab says "Wrong link" | It came from an older `relay pair` run: run it again and use the new link. |

Logs: `data\service.log` (`Relay ...` lines).

## Limits

- A help request can be claimed once **per PC** (claims are not relayed).
- The server only sees what crosses PCs: a DM between two agents on the same PC never leaves it.
- Fixed ids are per PC: `claude-desktop` and `study-claude` can exist on both, and a message to such an id goes to the
  local one if it exists here. Keep the study mailbox (`bus.mailbox.enabled`) on only on the PC study-Claude uses.
- Phone delivery is configured per PC (copy the `provider`, `ntfy` and `gateway` sections, never `relay`).
- Server retention is 7 days: a PC offline longer misses older messages.
- History before pairing is not replayed, and `bus.py history` shows only what reached this PC.

## Tests

`tests/test_relay.py` runs two real bus services against a fake relay server (push, pull long-poll, agents, pair per
the contract): routing, ids and threads, broadcasts, planner rules, notifications never relayed, offline queue without
duplicates, state replication and last-writer-wins, the directory and `bus.py` output, health, 401, and
`relay pair|status|unpair` end to end with a fake browser.
