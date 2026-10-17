# Second PC: headless host setup (DESKTOP-5BEGSB5 / Tailscale `main-pc`)

The power went out at the London house around Oct 14 00:44. The PC came back up and has sat at the Windows login
screen ever since. The Agent Bus service (LocalNotify) is a Windows service, so it came back and is online through the
planner relay. Everything that needs a signed-in user stayed down: Tailscale (not in unattended mode), Parsec (per-user
install), and the Planner runner. There was no way in.

`SETUP-HEADLESS.cmd` fixes that in one run, so the PC can be managed remotely, comes back by itself after a power cut,
and can take the heavy work off MSI: e2e tests through trun, Docker builds, and Claude jobs through the Planner runner.

| File | What it is |
| --- | --- |
| `SETUP-HEADLESS.cmd` | Double-click wrapper. Runs the script, which asks for admin rights itself. Switches pass through. |
| `Setup-HeadlessHost.ps1` | The setup. Windows PowerShell 5.1, idempotent (safe to run again), one line per step. |
| `Set-ReminderWallpaper.ps1` | Renders a "Run SETUP-HEADLESS on the Desktop" wallpaper. Only sets it on DESKTOP-5BEGSB5. |
| `WHEN-YOU-GET-HERE.txt` | The short version of this page, to keep next to the .cmd. |

## Getting the files onto that PC

Nothing reaches that PC until someone is sitting at it, so the files travel with you. Use any one of these:

- **git** (best: it also brings the remote ops code): `cd %USERPROFILE%\Desktop\Projects\notifications` then
  `git pull`. The repo is `github.com/cwoodard1121/notifications` (private). If that folder came from `agent-bus.zip`
  rather than a clone, clone the repo next to it, or copy this folder over.
- **USB stick or OneDrive:** copy `notifications\scripts\second-pc\` from MSI. On its own, that folder does everything
  except remote ops: that needs the updated repo (step 7 tells you).
- **GitHub web, as a ZIP:** works too. Windows marks downloaded files, so SmartScreen may ask about the .cmd: choose
  More info, then Run anyway.

Before you leave MSI, put your **public** keys next to the script so you don't have to paste them. Never copy the
private ones.

```bat
copy %USERPROFILE%\.ssh\id_ed25519.pub  <second-pc folder>\msi-ssh.pub
rem optional, only once MSI has a trun client key (ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\test-runner -N "")
copy %USERPROFILE%\.ssh\test-runner.pub <second-pc folder>\msi-test-runner.pub
```

**Reminder wallpaper.** You can run `Set-ReminderWallpaper.ps1` on MSI now, but there it only renders the PNG: a
wallpaper can only be changed on the PC itself. Run it on that PC once you're signed in, if you want the reminder up
before you start. SETUP-HEADLESS puts the old wallpaper back at the end.

## Run it

Sign in, double-click `SETUP-HEADLESS.cmd`, and accept the admin prompt. The setup continues in the admin window.

| # | Step | What it does |
| --- | --- | --- |
| 1 | Power | `powercfg`: no sleep or hibernate on AC. `/hibernate off` also turns Fast Startup off (`HiberbootEnabled=0`). Reads `Win32_BaseBoard` and prints the BIOS menu path for "Restore on AC Power Loss = Power On" for that board vendor (ASUS, MSI, Gigabyte, ASRock, Dell, HP, Lenovo, Biostar, or a generic fallback). |
| 2 | Tailscale | Installs it if missing (`winget Tailscale.Tailscale`) and sets the service to Automatic. If it's already up, runs `tailscale set --unattended=true --ssh=false`. Otherwise runs `tailscale up --unattended --ssh=false --hostname=main-pc`; the sign-in link shows in the window. Unattended mode keeps it running with nobody signed in. Tailscale SSH stays off because the Windows Tailscale SSH server isn't supported; OpenSSH is used instead. |
| 3 | OpenSSH | Installs the `OpenSSH.Server` capability and sets `sshd` to Automatic. `sshd_config` gets `PasswordAuthentication no` and `PubkeyAuthentication yes` (checked with `sshd -t`, with a backup kept). Adds your MSI key to `C:\ProgramData\ssh\administrators_authorized_keys`, then `icacls` sets it to Administrators + SYSTEM only. The default shell is Windows PowerShell. The firewall rule that allowed port 22 from anywhere is disabled and replaced by `HeadlessHost-SSH-Tailscale`: TCP 22 only from `100.64.0.0/10` and `fd7a:115c:a1e0::/48`. |
| 4 | Autologon | Downloads `live.sysinternals.com/Autologon64.exe` and runs it only if its Authenticode signature is valid and signed `O=Microsoft Corporation`. **You type the password into Autologon's window**; it stores it as an LSA secret. It also turns off "only Windows Hello for Microsoft accounts", which hides the password sign-in Autologon needs. A logon task, `HeadlessHost Lock After Autologon`, runs `rundll32 user32.dll,LockWorkStation` 90 s after sign-in, so the session runs but the desk is locked. It only fires within 8 minutes of boot, so signing in by hand later isn't locked. |
| 5 | Parsec | Needs the **Per Computer** install, whose service hosts the login screen. A per-user install is detected and replaced if you answer Y. The installer is the official `builds.parsec.app/package/parsec-windows.exe`: its signature is checked (`O=Unity Technologies SF`; Unity owns Parsec) and it runs with `/allusers /silent /norun /nocleanuser`. The Parsec service is set to Automatic. |
| 6 | Chrome Remote Desktop | A backup way in that also works at the login screen. Installs `dl.google.com/edgedl/chrome-remote-desktop/chromeremotedesktophost.msi` (signature checked: `O=Google LLC`) and sets the `chromoting` service to Automatic. You register it with your Google account (below). |
| 7 | Agent Bus remote ops | Runs `..\..\Install-RemoteOps.ps1` from the notifications repo. That installs the `AgentBusOps` helper, so MSI can run a fixed allowlist of actions over the encrypted console channel. See [docs/REMOTE-OPS.md](../../docs/REMOTE-OPS.md). Your MSI public key is also what verifies signed self-update tags. |
| 8 | Runner | Installs Git, Node LTS and Python with winget if missing. Docker Desktop (if installed) starts at sign-in. **trun:** finds or clones `cwoodard1121/test-runner` and runs its own `runner\setup-windows.ps1`, which sets up WSL, `.wslconfig` (about 60% of RAM) and the `test-runner WSL` keep-alive task. It then runs `runner/setup-wsl.sh` inside a dedicated distro as runner `main-runner`, with `INSTALL_DOCKER=1` (rootless Docker, tagged `docker`) and `PLAYWRIGHT_VERSION=1.63.0`: two passes, systemd first. It adds the client keys with `runner/add-key.sh` (MSI's, plus this PC's own `~\.ssh\test-runner.pub`). If `Ubuntu-24.04` already exists and isn't a runner, it gets its own `trun-runner` distro instead, because setup-wsl.sh cuts a distro off from Windows. **Planner:** if the `Planner Runner` logon task is missing, it opens `gcalfiller-uwo\app\scripts\second-computer\Setup-PlannerDesktop.ps1` in a normal, non-admin window. It also adds `Planner Runner (pre-login)`, explained below. |
| 9 | Self-check | One green or red line each: tailscale up + IP, sshd listening (plus keys and the firewall rule), Parsec service, Chrome Remote Desktop, autologon, sleep off, docker, runner registered, Planner runner, Agent Bus + ops. Writes `C:\ProgramData\HeadlessHost\status.json`. The log is `C:\ProgramData\HeadlessHost\setup.log`. |

Run it again any time: it only changes what's missing. `SETUP-HEADLESS.cmd -CheckOnly` runs only the self-check, and
also works over SSH. `-WhatIf` is a dry run: it changes nothing and skips the admin prompt. Other switches:
`-SkipRunner`, `-SkipParsec`, `-SkipChromeRemoteDesktop`, `-SkipPlannerPreLogin`, `-NoRunnerDocker`,
`-ReconfigureAutologon`, `-PublicKeyFile`, `-TrunClientKeyFile`, `-RunnerMemoryGB`, `-RunnerSlots`.

## What needs your hands (the script stops at each one)

1. **Autologon:** type your password into its window and click Enable. With a Microsoft account, use the email address
   and the account password, not the PIN.
2. **Public key:** only if `msi-ssh.pub` isn't next to the script. Paste the one line from MSI's
   `%USERPROFILE%\.ssh\id_ed25519.pub`.
3. **Tailscale sign-in links:** for `main-pc` if it's signed out, and for the WSL runner `main-runner`. Then, in the
   [admin console](https://login.tailscale.com/admin/machines), open the `...` menu on both and choose **Disable key
   expiry**. Otherwise the keys expire in 180 days and the PC drops off again.
4. **Parsec:** answer Y if it offers a per-computer reinstall. Then open Parsec once, sign in, and turn hosting on. In
   the tray, keep "Run when computer starts" on. If no monitor stays plugged in, turn on the Parsec virtual display.
5. **Chrome Remote Desktop:** on any PC, go to https://remotedesktop.google.com/headless, then Begin, Next, Authorize.
   Copy the Windows (PowerShell) line and run it in an **admin** PowerShell on this PC; it asks for a PIN of 6 digits
   or more. You can also just sign in at remotedesktop.google.com/access on this PC.
6. **Task Scheduler password prompt** (`schtasks /RP *`) for the pre-login Planner runner. It's the tool's own prompt;
   the script never sees the password.
7. **WSL / Ubuntu first start:** possibly one restart, and a Linux user name and password if Ubuntu asks. Then
   double-click SETUP-HEADLESS again; it carries on from there.
8. **Planner sign-in** in the browser, only if the Planner runner task was missing.
9. **BIOS:** set Restore on AC Power Loss to **Power On**. The script prints the menu path for this board. To get
   there, use `shutdown /r /fw /t 0` from an admin PowerShell, which reboots straight into the BIOS. If the drive is
   BitLocker or device-encrypted (the script checks), first run `manage-bde -protectors -disable C: -rc 1`. A changed
   BIOS can otherwise trigger the recovery-key screen, and no remote path gets past that. Keep the recovery key at
   https://aka.ms/myrecoverykey.

**Time:** about 45 to 75 minutes, mostly waiting on downloads (OpenSSH capability, WSL, apt, Playwright libraries,
Docker). Your own input takes about 10 minutes. Do the BIOS last, because it reboots the PC.

## Afterwards, from MSI

```powershell
ssh camer@main-pc                                   # key only, lands in PowerShell
ssh camer@main-pc 'powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\Desktop\Projects\notifications\scripts\second-pc\Setup-HeadlessHost.ps1 -CheckOnly'
trun add main-runner runner@main-runner; trun runners   # needs MSI's test-runner key added on the runner
python bus.py remote DESKTOP-5BEGSB5 status         # Agent Bus remote ops (docs/REMOTE-OPS.md)
```

If you skipped the trun key at the desk, add it later over SSH:
`wsl -d <runner distro> -u root -- bash /root/trun-setup/runner/add-key.sh '<contents of test-runner.pub>'`.

## Ways back in, and what each one survives

| Path | Works at the login screen | Needs |
| --- | --- | --- |
| SSH over Tailscale | yes (both are services) | Tailscale unattended, your key |
| Parsec | yes (per-computer service) | Parsec signed in once, hosting on |
| Chrome Remote Desktop | yes (`chromoting` service) | registered once with a PIN |
| Agent Bus remote ops | yes (LocalNotify + AgentBusOps are services) | console key on both PCs; this setup has run once |

Remote ops is a fixed allowlist sent over the existing end-to-end encrypted console channel. It can report status,
start or restart Parsec, Tailscale, sshd, Docker or the Chrome Remote Desktop host, run `tailscale up --unattended`,
reboot (with a confirm flag), and self-update to a signed tag. There's no shell. It can only help once SETUP-HEADLESS
has installed it on that PC; this time it couldn't, which is why you have to go there in person.

**Smart plug (recommended).** Put the PC on a smart plug you can switch from your phone. If Windows hangs hard, or
everything above is down, switch the plug off and on again. That's a real power cut, and BIOS "Power On after AC loss"
brings the PC back up. Autologon signs it in, and Tailscale, SSH, Parsec, Chrome Remote Desktop, the runner and the
Agent Bus all come back by themselves. Without the BIOS setting, the plug just leaves the PC off.

## The pre-login Planner runner

The `Planner Runner` task, created by Planner itself, starts `Planner.exe --dispatch-service` at sign-in with your
interactive token, so it can drive your Chrome. With Autologon that happens a minute after boot. `Planner Runner
(pre-login)` is the backup for when Autologon fails, for example after a password change:

- It runs at startup with "Run whether user is logged on or not": your account, with the password stored by Task
  Scheduler.
- It isn't S4U on purpose. The runner token (`%APPDATA%\Planner\runner-token.dat`) is DPAPI CurrentUser, and an S4U
  logon has no credentials to open it, so an S4U runner would come up with no token and do nothing.
- Its wrapper, `C:\ProgramData\HeadlessHost\Planner-PreLogin.ps1` (admin-only folder), starts the runner only while
  nobody is signed in at the console. Once you are signed in, it stops the runner it started (only that process tree)
  and runs the normal `Planner Runner` task.

What runs in that window, in session 0 with no desktop: `service-main.js` opens no window or tray, and it writes its
heartbeat and logs to `%APPDATA%\Planner`. That covers `bus-message` and Claude jobs that don't need Chrome. `owl-sync`
needs your Chrome, so it waits for the signed-in runner. Activity nudges (toasts) and focus sampling have no desktop to
use, so in Planner > Settings > Computers keep **Track my focus** off for this PC and **Run agent jobs** on. Whether
Electron starts cleanly in session 0 is not tested yet (it can't be tested from MSI without changing MSI). The
heartbeat and `%APPDATA%\Planner\logs\planner-prelogin.log` show it after the first reboot. If it doesn't, the logon task still covers
everything once Autologon has signed you in.

## Notes

- **Never stored or printed:** passwords, private keys. Public keys appear only as SHA256 fingerprints. A key file
  holding a private key is refused unread.
- **Docker:** builds were moving to Google Cloud Build (dockeriser) as of Oct 16. The runner's rootless Docker still
  serves trun jobs with `"needs": ["docker"]` and local image checks. Skip it with `-NoRunnerDocker`.
- The separate `trun-runner` distro, used only when a non-runner `Ubuntu-24.04` already exists, is created with
  `wsl --install Ubuntu-24.04 --name trun-runner`, which needs WSL 2.4.4 or newer (`wsl --update`). If that fails, the
  runner step keeps saying "distro pending". Run `wsl --update`, or pass `-RunnerDistro <a free distro name>`.
- `.wslconfig` is shared by every WSL distro and Docker Desktop. trun's setup-windows.ps1 rewrites it (memory cap,
  `vmIdleTimeout=-1`) and keeps a `.bak` copy.
- **Sources:** Parsec install types, "Per Computer" vs "Per User" and login-screen access:
  https://support.parsec.app/hc/en-us/articles/32381368159124-Install-Parsec-App-on-Windows. The Parsec background
  service (UAC and login): https://support.parsec.app/hc/en-us/articles/32381334155412-Why-Is-Parsec-Running-in-the-Background-on-Startup.
  Logon screen support: https://support.parsec.app/hc/en-us/articles/4407194154253-Feature-Matrix. Silent and
  per-machine switches: the `Parsec.Parsec` winget manifest (microsoft/winget-pkgs `manifests/p/Parsec/Parsec`, same
  installer URL, SHA256 checked Oct 16). Chrome Remote Desktop headless setup:
  https://cloud.google.com/architecture/chrome-desktop-remote-windows-compute-engine.
