# Remote ops: a few fixed actions on your other PC, through the bus

When the second PC (DESKTOP-5BEGSB5, "main-pc") is unreachable by Tailscale, SSH or Parsec, its Agent Bus service is
often still online through the planner relay. Remote ops use that path to run a short, fixed list of recovery actions,
like restarting Parsec or bringing Tailscale back up. **It is not a shell.** There is no free-form command, and no
string you type ever reaches a command line.

```text
python bus.py remote DESKTOP-5BEGSB5 status
python bus.py remote DESKTOP-5BEGSB5 service-restart parsec      # parsec | tailscale | sshd | docker | crd
python bus.py remote DESKTOP-5BEGSB5 service-start tailscale
python bus.py remote DESKTOP-5BEGSB5 tailscale-up                # tailscale up --unattended
python bus.py remote DESKTOP-5BEGSB5 reboot --confirm            # restarts in 60 s; `shutdown /a` there cancels
python bus.py remote DESKTOP-5BEGSB5 self-update ops-20261018-1  # fast-forward to a SIGNED tag, restart LocalNotify
```

`--json` prints the raw answer. The machine name is the other PC's relay id (as `bus.py agents` shows it), in any case.

**This only works once it's installed on that PC.** SETUP-HEADLESS (`scripts/second-pc/`) installs it. Until Cameron
runs that at the desk, `bus.py remote` answers "remote ops not installed on <machine>". The PC also needs the relay
paired and the same console key as this one (`python notify.py console key`, see [AGENT-CONSOLE.md](AGENT-CONSOLE.md)).

## The allowlist

| Action | Arguments | What runs on the other PC |
| --- | --- | --- |
| `status` | none | uptime, the console user (or nobody at the login screen), the state of every service below plus LocalNotify, Tailscale BackendState and IPv4, the bus's git commit, whether self-update is on |
| `service-start` | `name` | Service Control Manager start of one service, then waits for it to run |
| `service-restart` | `name` | stop (if running), then start |
| `tailscale-up` | none | `tailscale up --unattended --timeout=40s`; if other flags are set on the node (hostname, `--ssh=false`...), it falls back to `tailscale set --unattended=true` so those are kept |
| `reboot` | `confirm: true` (required) | `shutdown /r /t 60 /c "Agent Bus remote reboot"` |
| `self-update` | `ref`: `ops-YYYYMMDD-N` or a 40-hex sha | `git fetch`, verify the SSH signature, fast-forward `main`, restart LocalNotify (below) |

Service names: `parsec` is the service whose ImagePath contains `pservice.exe` (the per-computer install's service,
normally `Parsec`), `tailscale` is `Tailscale`, `sshd` is `sshd`, `docker` is `com.docker.service` (Docker Desktop),
and `crd` is `chromoting` (Chrome Remote Desktop host). A missing one gets "not installed".

The allowlist lives in `remote_ops.py` and is checked **twice**: LocalNotify validates a request before it forwards it,
and the helper validates it again before it acts. Anything else (unknown action, extra argument, wrong name, a ref
with spaces or options, a reboot without confirm) is refused with 400 and logged.

## Privilege split

```text
 MSI                                   planner relay                 DESKTOP-5BEGSB5
 bus.py remote ──▶ LocalNotify ──▶ sealed "ops" frame ──▶ (ciphertext) ──▶ LocalNotify (NT SERVICE\LocalNotify)
                   POST /remote/ops   console key, AAD,                     checks freshness + one-time nonce,
                                      replay + age checks                   validates the allowlist
                                                                                   │ \\.\pipe\agent-bus-remote-ops
                                                                                   ▼ (ACL: SYSTEM + LocalNotify only)
                                                                            AgentBusOps (LocalSystem)
                                                                            validates again, runs the fixed action
                                                                                   │ self-update only
                                                                                   ▼
                                                                            task "AgentBusOps Self-Update"
                                                                            (Cameron, not elevated): git
```

**LocalNotify** runs as the virtual account `NT SERVICE\LocalNotify`. That's least privilege. It reads its own folder,
writes `data\`, and reads the transcript folders it was granted. It **can't** start or stop other services, reboot,
change Tailscale, or launch anything in Cameron's session. That's deliberate: it talks to the internet (the relay), so it
should hold nothing worth stealing. Remote ops don't change that. LocalNotify only gets to *ask*.

**AgentBusOps** (`remote_ops_helper.py`, display name "Agent Bus Remote Ops") does the privileged part. It runs as
**LocalSystem**, because each action needs admin-level rights anyway:

- starting/stopping Parsec, Tailscale, sshd, Docker and Chrome Remote Desktop means SERVICE_START/STOP on each of them;
- `shutdown /r` needs SeShutdownPrivilege;
- `tailscale up/set` talks to tailscaled's local API, which only admins and SYSTEM may change.

A dedicated account with per-service SCM rights could cover the service actions alone. It would still need the shutdown
privilege and Tailscale admin access, though, and it would have a password to manage. So it would be about as powerful
in practice, with more moving parts. The real boundary is what the helper agrees to do, and that's the fixed allowlist
plus a pipe only LocalNotify can open:

- The pipe's DACL is protected (`D:P(A;;GA;;;SY)(A;;GRGW;;;<LocalNotify service SID>)`). Only SYSTEM and
  `NT SERVICE\LocalNotify` can open it, not Administrators and not Everyone. It also refuses remote clients
  (`PIPE_REJECT_REMOTE_CLIENTS`), and it's created as the first and only instance and then reused, so another process
  can't squat on the name. `python -I -B remote_ops_helper.py --sid` prints the descriptor.
- One JSON request per connection, at most 4 KB in and 64 KB out.
- The helper runs from `C:\Program Files\AgentBusOps` (SYSTEM and Administrators write, Users read), with its own copy
  of `remote_ops.py`, under `python -I` (no PYTHON* variables, no user site, no script-folder imports). A `git pull` or
  a `self-update` of the repo never changes privileged code. Re-run `Install-RemoteOps.ps1` (elevated) to update it.
- Caveat: `python.exe` is the per-user install under `%LOCALAPPDATA%\Programs\Python`, so it's writable by Cameron.
  That isn't an escalation, because Cameron is an administrator on that PC anyway. Point `-PythonPath` at an
  all-users Python if that ever changes.

**Self-update** is split once more. The notifications repo on GitHub is private, and SYSTEM has no GitHub credentials
(and shouldn't). So the helper writes `C:\ProgramData\AgentBusOps\update\request.json` and runs the on-demand task
**AgentBusOps Self-Update**. That task runs as Cameron with his normal, non-elevated token, so it works while he's signed
in (autologon keeps him signed in). The task runs `remote_ops_update.ps1` from the admin-only folder, which:

1. refuses unless the checkout is on `main` with no tracked changes;
2. `git fetch --tags origin`;
3. `git verify-tag` / `git verify-commit` against `C:\ProgramData\AgentBusOps\allowed_signers` (admin-only; written by
   the installer from `-SigningKeyFile`), then refuses anything not signed by that key;
4. refuses anything that isn't a fast-forward of the current `main`;
5. `git merge --ff-only`. Every git call runs with `core.hooksPath=<empty folder>` and `core.fsmonitor=false`, so
   nothing in the repo runs during the update.

The helper answers first and restarts LocalNotify 8 seconds later, because LocalNotify carries the answer back. The
service also restarts by itself when its `.py` files change. Without an `allowed_signers` file, self-update is off.

## Signing a tag on MSI (once, then per update)

```powershell
git config --global gpg.format ssh
git config --global user.signingkey "$HOME\.ssh\id_ed25519.pub"
# each update, from the notifications repo on main, after pushing the commits:
git tag -s ops-20261018-1 -m "ops update"
git push origin ops-20261018-1
python bus.py remote DESKTOP-5BEGSB5 self-update ops-20261018-1
```

The other PC needs the matching public key: SETUP-HEADLESS passes the same `.pub` as `-SigningKeyFile`. Or run
`Install-RemoteOps.ps1 -SigningKeyFile <file>` elevated there.

## Install / remove (on the PC that accepts remote ops)

```powershell
# elevated, from the notifications repo; idempotent; -WhatIf shows what it would do
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-RemoteOps.ps1 -SigningKeyFile C:\path\id_ed25519.pub
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-RemoteOps.ps1 -Remove
```

It creates the `AgentBusOps` service (Automatic, restart on failure), the pipe, the task, `C:\Program Files\AgentBusOps`
and `C:\ProgramData\AgentBusOps`. Check it with `Get-Service AgentBusOps`, and from MSI with
`python bus.py remote <machine> status`.

## Logging

Every request and its outcome are logged on **both** PCs:

- `data\remote-ops.log` next to each bus (`msi -> desktop-5begsb5 service-restart parsec: sent` / `: ok` /
  `: refused: ...`), plus the service log;
- a bus line from `bus` ("Remote ops") on each PC, visible in `/ui` and `bus.py history`. It's marked `noinject`, so it
  doesn't interrupt running agents;
- the helper's own audit log, `C:\ProgramData\AgentBusOps\helper.log` (action, caller PC, result, duration).

## Threat model

- **Who can trigger an action:** whoever has the console key, which means a copy of Cameron's SSH private key file
  (bus_crypto.py) and a paired bus. The relay server only ever stores ciphertext. It can't read, forge or change a frame,
  because every frame is sealed with encrypt-then-MAC and its AAD binds the sender and recipient PCs.
- **Replay:** console frames are refused after 15 minutes and deduplicated in memory. An ops request also carries a
  one-time nonce and a timestamp. The host refuses anything older than 120 s, plus any nonce it has seen in the last
  4 minutes. Those nonces are kept on disk (`data\remote-ops-nonces.json`), so restarting LocalNotify doesn't reopen a
  replay window (a replayed `reboot` can't cause a boot loop). Keep both PCs' clocks synced (Windows time is enough).
- **Worst case with the key:** someone can restart those five services, run `tailscale up`, reboot the PC once per
  request, or fast-forward the repo to a commit **you signed**. They can't run code of their choosing, read files, or
  change the allowlist. The allowlist only changes through an elevated `Install-RemoteOps.ps1` on that PC.
- **Local processes on that PC** can't use the pipe unless they run as SYSTEM or as the LocalNotify service.

## Files

`remote_ops.py` (allowlist, pipe client), `remote_ops_helper.py` (the AgentBusOps service), `remote_ops_update.ps1` (the
self-update task), `Install-RemoteOps.ps1`, the `ops` frame in `bus_remote.py`, `POST /remote/ops` in `bus_routes.py`,
`bus.py remote`, and `tests/test_remote_ops.py`.
