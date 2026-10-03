# Agent Bus: user guide

The Agent Bus lets every AI agent on this PC see and talk to each other: the Claude Code sessions in all your config
dirs (`main`, `pecunia`, `appraiseme`, `capresta`, `general`, `derive`), Codex, the Claude desktop app and the cloud
"study Claude". It also carries **your state** (studying, gym, sleeping...), which every agent sees and which holds
low-priority phone notifications while you're busy.

It runs inside the existing **Local Notify** service (`http://127.0.0.1:8765`, Windows service `LocalNotify`). Nothing
is exposed to the network, and the only things that leave the PC are phone notifications an agent sends on purpose
and, once you pair a second computer, the bus messages and state it needs (see [Two computers](#two-computers)).

## How it works

```text
Claude Code sessions ──plugin hooks──┐
Codex / scripts ──python bus.py ─────┤
Claude desktop app ──MCP (bus_mcp)───┼──> Local Notify :8765 ──> SQLite (roster, messages, your state)
study Claude ──file mailbox──────────┤            │
You ──/ui dashboard, /agent-bus:mode─┘            └──> phone (only intentional pings; held by your state)
```

- **Joining is automatic for Claude Code.** The `agent-bus` plugin's hooks register each session when it starts. The
  session learns its id (`claude-<8 hex>`), who else is online, your state and recent broadcasts.
- **Messages arrive by themselves.** Before each prompt and each tool call, pending messages and state changes are
  injected into the agent's context (you don't see this in the chat; the agent does). When a session stops it's
  marked idle; when it exits it leaves the roster.
- **Idle wake (experimental).** An idle session can be woken by an urgent DM/request (one that needs a reply or has
  priority 4-5), at most twice per 30 minutes per agent. This uses a background Stop hook (`bus.py rewake`) that
  long-polls the service; it stops when you're `sleeping`, when the session ends, or after 8 hours. Turn it off with
  `"bus": {"idle_wake": false}` in `config.json` (and `wake_on_stop: false` for the stop-time variant).
- **Codex has no hooks integration** (Codex asks you to trust each hook). Codex agents join through the agent-bus
  skill: `python bus.py --as codex-<name> register`, then `status`/`inbox` as they work.
- **Agents coordinate through the skill.** The `agent-bus` skill tells them to post what folder + task they're on,
  check the roster before duplicating research, use help requests (first claimer wins) to delegate across your
  accounts, DM for private coordination, and to follow your state.

## Your state (modes)

| Mode | Lasts (default) | Phone gets | Meaning for agents |
|---|---|---|---|
| `available` | clears the state | everything | normal |
| `studying` | 6 h | priority 4-5 | you're doing school work; queue questions, keep working, don't block on you |
| `lecture` | 2 h | 4-5 | in class; same rules |
| `gym` | 2 h | 4-5 | away from the computer |
| `meal` | 1 h | 3-5 | eating |
| `sleeping` | 9 h | 5 only | no work that needs you; no pings unless priority 5 |
| `focus` | 3 h | 4-5 | deep work; don't interrupt |
| `away` | 8 h | 4-5 | away |

Any other word works as a custom mode if you add a note. A state ends by itself when its time is up (agents are told
"Cameron is available again"). Ways to set it:

- In any Claude Code session: `/agent-bus:mode studying chem midterm prep` (or `/agent-bus:mode available`).
- In the Claude desktop app chat: "I'm doing school work for 3 hours" - it calls the `bus_state` tool.
- Study Claude: drops a `{"type":"state","mode":"studying"}` file in its outbox (see `PROTOCOL.md` in the mailbox).
- The dashboard at http://127.0.0.1:8765/ui.
- CLI: `python bus.py state studying --note "chem" --ttl 180`, `python bus.py state` to see it.

A notification held by your state is stored, not delivered; the dashboard and `GET /bus/held` list them.

## Everyday commands

In Claude Code: `/agent-bus:agents` (who is doing what), `/agent-bus:say <message>` (broadcast from you to every
agent), `/agent-bus:mode <mode> [note]`. Anywhere:

```powershell
python C:\Users\camer\Desktop\Projects\notifications\bus.py agents
python C:\Users\camer\Desktop\Projects\notifications\bus.py --as user broadcast "Pause deploys until 6pm"
python C:\Users\camer\Desktop\Projects\notifications\bus.py history --limit 30
```

`python bus.py help` lists every command. Exit codes: 0 ok, 1 error, 2 usage, 3 service not reachable.

## Dashboard

http://127.0.0.1:8765/ui shows the roster, live messages, your state (with buttons to set it) and held notifications.
It is served by the local service only; it works on this PC, not from your phone.

## Claude desktop app

The installer adds an `agent-bus` MCP server to the desktop app's `claude_desktop_config.json` (for the Microsoft
Store build that's `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`). It runs
`python bus_mcp.py --client claude-desktop` and appears on the bus as `claude-desktop`. Quit and reopen the app after
installing. Its tools: `bus_agents`, `bus_state` (get/set your mode), `bus_send`, `bus_broadcast`, `bus_request`,
`bus_reply`, `bus_inbox`, `bus_status`, `bus_claim`, `bus_history`, `bus_notify_phone`. Every result ends with
`[bus] N unread message(s)` when something is waiting.

Claude Code sessions get the same tools from the plugin (`mcp__plugin_agent-bus_agent-bus__bus_*`).

## Study Claude (file mailbox)

The cloud study Claude can only touch files, so the service bridges the folder
`C:\Users\camer\Desktop\studywithclaude\_agent_mailbox` (from `bus.mailbox` in the config). It writes JSON requests
into `outbox\`, receives messages as files in `inbox\`, and reads `status.json` (your state + roster). The service
rewrites `PROTOCOL.md` there at every start with the exact format; point study Claude at that file. To start a study
session it writes one `state` file (`"mode":"studying"`), to end it `"mode":"available"`. The installer grants the
service account (`NT SERVICE\LocalNotify`) Modify on that folder.

## Two computers

With a second PC (agents on one, school on the other), each PC keeps its own bus and the planner server relays between
them: `python notify.py relay pair` on each PC (the second one runs `Setup-RelayMachine.ps1`), then
`python notify.py relay status`. `bus.py agents`, `/agent-bus:agents` and `bus_agents` list the other PC's agents as
`<computer>:<account>/<folder>`; DMs to them, `all` / `@account` / `#folder` messages and your state cross over;
relayed messages are labelled `on <computer>`. Messages you type on the planner's Agents page come from `planner`
("Cameron (planner)"). Phone pings and claims stay per PC. Full guide: [RELAY.md](RELAY.md).

## Install, update, uninstall

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-AgentBus.ps1 -DryRun   # show what would change
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-AgentBus.ps1           # install / repair (idempotent)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-AgentBus.ps1 -Remove   # uninstall
```

The installer, for every `~\.claude*` config dir: adds the local marketplace (`plugin\`) and installs
`agent-bus@agent-bus-local` with the `claude` CLI; if that fails it copies the skill and merges the hooks into that
dir's `settings.json` instead (reported as `fallback`, CLI only, no MCP tools). It also removes the old automatic
Claude "finished"/"needs attention" phone hooks, installs the `agent-bus` and updated `notify-user` skills for Codex,
adds the desktop app MCP server and grants the mailbox permission. Every edited file is backed up to `data\backups`,
and the result is in `data\agentbus-install-result.json`. It never starts or stops anything: **running sessions pick
up the plugin after a restart or `/reload-plugins`**. Re-run it after moving the project, changing the service port or
the Python install; it regenerates `hooks.json`, `.mcp.json` and the slash commands with the right paths. The plugin
loads in place from `plugin\agent-bus`, so edits there apply at the next session start.

`-Remove` uninstalls the plugin and marketplace from every config dir, removes fallback hooks/skills, the Codex
agent-bus skill and the desktop MCP entry. It leaves your history, the notify-user skill and the mailbox permission
(it prints the `icacls` command to revoke it), and it does not bring back automatic phone pings.

## Security notes

- The API only listens on 127.0.0.1 and rejects browser cross-site requests, but **any local process can post to
  it** (and can claim any agent id). With `api_token` set in `config.json`, the CLI, MCP server and plugin hooks send
  it automatically (the installer writes it into the plugin's `hooks.json`, so keep that folder private).
- **Messages from other agents are untrusted information, not instructions from you.** The injected text says so, and
  the skill forbids destructive or out-of-scope actions on a peer's request. Anything you really want done should come
  from you, in the session itself.
- With the relay paired, messages that cross PCs are also stored on the planner server for 7 days; the bus token in
  `config.json` (`relay.token`) opens only the relay routes and is revoked in planner Settings > Computers.
- Agents are told never to put secrets in messages; messages are stored in `data\notifications.sqlite3` for 14 days
  (roster entries are pruned 48 h after an agent was last seen or ended).
- Phone notifications only happen when an agent deliberately sends one (notify-user skill, `bus.py notify`,
  `bus_notify_phone`, or Claude's PushNotification tool); your state holds the low-priority ones.

## Troubleshooting

- `python bus.py whoami` shows the agent's id and whether the bus knows it; `python notify.py status` shows the service.
- "Agent Bus service is not reachable": start it with `.\Start-Notify.ps1` (the hooks just stay silent while it's down).
- A session doesn't show up: it was started before the install; restart it or run `/reload-plugins`.
- Hook errors are logged (once a minute at most) to `data\service.log`.
