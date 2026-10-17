<#
.SYNOPSIS
  One-shot setup that turns this PC (DESKTOP-5BEGSB5, Tailscale "main-pc") into a headless host that can be managed
  remotely, survives power cuts and works as Cameron's runner for tests, Docker builds and Claude jobs.

.DESCRIPTION
  Double-click SETUP-HEADLESS.cmd (it runs this script, which asks for administrator rights itself). Safe to run again:
  every step checks first and changes only what is missing. Steps:

     1. Power: no sleep/hibernate on AC, Fast Startup off, BIOS "power on after AC loss" instructions for this board.
     2. Tailscale: installed, unattended mode (runs with nobody signed in), Tailscale SSH off.
     3. OpenSSH Server: keys only, port 22 only from the tailnet, your MSI public key, PowerShell as the shell.
     4. Autologon (Sysinternals, signature checked; YOU type the password into its window), then lock at logon.
     5. Parsec per-computer (the service hosts the login screen), service Automatic.
     6. Chrome Remote Desktop host (backup way in; works at the login screen).
     7. Agent Bus remote ops helper (fixed allowlist over the encrypted console channel; docs/REMOTE-OPS.md).
     8. Runner: Git/Node LTS/Python, the trun test runner in its own WSL distro (rootless Docker), Planner runner
        (at logon, plus a pre-login copy that brings Planner jobs back after a power cut).
     9. Self-check: one green/red line each, written to C:\ProgramData\HeadlessHost\status.json.

  The script never asks for, reads, stores or prints a password or a private key. Passwords go into Autologon's own
  window and Task Scheduler's own prompt. Public keys are shown only as SHA256 fingerprints.

.PARAMETER PublicKeyFile
  The MSI public key for SSH logins (e.g. a copy of MSI's C:\Users\camer\.ssh\id_ed25519.pub). Default: msi-ssh.pub
  next to this script; else you are asked to paste it.

.PARAMETER TrunClientKeyFile
  A trun client's public key (the client's ~/.ssh/test-runner.pub). Default: msi-test-runner.pub next to this script;
  else you are asked (Enter skips; it can be added later over SSH). This PC's own test-runner.pub is added too.

.PARAMETER CheckOnly
  Only run the self-check and write status.json.

.PARAMETER WhatIf
  Dry run: shows what would change, changes nothing, does not ask for administrator rights.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup-HeadlessHost.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$PublicKeyFile = '',
  [string]$TrunClientKeyFile = '',
  [string]$TailscaleHostname = 'main-pc',
  [string]$RunnerName = 'main-runner',
  [string]$RunnerDistro = '',
  [int]$RunnerMemoryGB = 0,
  [int]$RunnerSlots = 0,
  [string]$PlaywrightVersion = '1.63.0',
  [string]$TrunRepo = '',
  [string]$NotificationsRepo = '',
  [string]$PlannerSetupScript = '',
  [string]$ExpectedComputer = 'DESKTOP-5BEGSB5',
  [switch]$SkipRunner,
  [switch]$SkipParsec,
  [switch]$SkipChromeRemoteDesktop,
  [switch]$SkipPlannerPreLogin,
  [switch]$NoRunnerDocker,
  [switch]$ReconfigureAutologon,
  [switch]$CheckOnly,
  [switch]$Force,
  [switch]$Relaunched
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$env:WSL_UTF8 = '1'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

$script:DryRun = [bool]$WhatIfPreference
$WhatIfPreference = $false   # every change goes through Invoke-Change instead; read-only cmdlets run normally
$script:Results = New-Object System.Collections.ArrayList
$script:Hands = New-Object System.Collections.ArrayList
$script:BiosText = @()
$script:SshKeyLine = $null
$script:RunnerDistroUsed = $null
$StateDir = Join-Path $env:ProgramData 'HeadlessHost'
$ToolsDir = Join-Path $StateDir 'tools'
$StatusFile = Join-Path $StateDir 'status.json'
if ($script:DryRun) { $LogFile = Join-Path $env:TEMP 'HeadlessHost-dryrun.log' } else { $LogFile = Join-Path $StateDir 'setup.log' }
$TaskLock = 'HeadlessHost Lock After Autologon'
$TaskPlannerPre = 'Planner Runner (pre-login)'
$TaskPlanner = 'Planner Runner'
$TaskTrun = 'test-runner WSL'
$TailnetV4 = '100.64.0.0/10'
$TailnetV6 = 'fd7a:115c:a1e0::/48'
$FwRule = 'HeadlessHost-SSH-Tailscale'
$AutologonUrl = 'https://live.sysinternals.com/Autologon64.exe'
$ParsecUrl = 'https://builds.parsec.app/package/parsec-windows.exe'
$CrdUrl = 'https://dl.google.com/edgedl/chrome-remote-desktop/chromeremotedesktophost.msi'

# ------------------------------------------------------------------------------------------------------ output
function Write-Log([string]$Text) {
  try {
    $dir = Split-Path -Parent $LogFile
    if (-not (Test-Path -LiteralPath $dir)) { [void][IO.Directory]::CreateDirectory($dir) }
    [IO.File]::AppendAllText($LogFile, ('{0:u}  {1}{2}' -f (Get-Date), $Text, [Environment]::NewLine))
  } catch { }
}
function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan; Write-Log "== $Text" }
function Write-Ok([string]$Text) { Write-Host "    OK    $Text" -ForegroundColor Green; Write-Log "OK $Text" }
function Write-Warn([string]$Text) { Write-Host "    !!    $Text" -ForegroundColor Yellow; Write-Log "WARN $Text" }
function Write-Bad([string]$Text) { Write-Host "    FAIL  $Text" -ForegroundColor Red; Write-Log "FAIL $Text" }
function Write-Note([string]$Text) { Write-Host "          $Text"; Write-Log "   $Text" }
function Add-Hands([string]$Text) { if (-not $script:Hands.Contains($Text)) { [void]$script:Hands.Add($Text) } }
function Add-Result([string]$Step, [string]$Status, [string]$Detail) {
  [void]$script:Results.Add([pscustomobject]@{ Step = $Step; Status = $Status; Detail = $Detail })
}

# Every change to the PC goes through here, so -WhatIf can show it and change nothing.
function Invoke-Change([string]$What, [scriptblock]$Do) {
  if ($script:DryRun) { Write-Host "    WHATIF  $What" -ForegroundColor DarkYellow; Write-Log "WHATIF $What"; return $null }
  Write-Log "DO $What"
  return (& $Do)
}

# Runs a program and captures stdout+stderr as text (no exceptions from stderr in Windows PowerShell).
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
  $out = $null
  try { $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" } } catch { $out = @("$($_.Exception.Message)"); return [pscustomobject]@{ Code = 1; Out = ($out -join "`n") } }
  return [pscustomobject]@{ Code = $LASTEXITCODE; Out = (@($out) -join "`n") }
}

# Runs a program attached to this console (its output and prompts show live, nothing is captured); returns its exit code.
function Start-Visible([string]$Exe, [string]$ArgLine) {
  $p = Start-Process -FilePath $Exe -ArgumentList $ArgLine -NoNewWindow -Wait -PassThru
  return $p.ExitCode
}

function Ask-YesNo([string]$Question, [bool]$Default) {
  if ($script:DryRun) { Write-Note "(dry run: would ask '$Question')"; return $false }
  if ($Default) { $suffix = '[Y/n]' } else { $suffix = '[y/N]' }
  $a = Read-Host "    ?     $Question $suffix"
  if (-not $a) { return $Default }
  return ($a -match '^(y|yes)$')
}

function Update-SessionPath {
  $m = [Environment]::GetEnvironmentVariable('Path', 'Machine'); $u = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
}

function Get-Download([string]$Url, [string]$Name) {
  if ($script:DryRun) { $dir = Join-Path $env:TEMP 'HeadlessHost-dryrun' } else { $dir = $ToolsDir }
  if (-not (Test-Path -LiteralPath $dir)) { [void][IO.Directory]::CreateDirectory($dir) }
  $file = Join-Path $dir $Name
  Invoke-WebRequest -Uri $Url -OutFile $file -UseBasicParsing -TimeoutSec 600 -ErrorAction Stop
  return $file
}

# Valid Authenticode signature whose signer subject has O=<Org>.
function Test-Signed([string]$File, [string]$OrgPattern) {
  $sig = Get-AuthenticodeSignature -LiteralPath $File
  $subject = ''
  if ($sig.SignerCertificate) { $subject = $sig.SignerCertificate.Subject }
  $ok = ($sig.Status -eq 'Valid') -and ($subject -match "O=$OrgPattern(,|$)")
  return [pscustomobject]@{ Ok = $ok; Status = "$($sig.Status)"; Subject = $subject }
}

function Get-ServiceSafe([string]$Name) { try { return (Get-Service -Name $Name -ErrorAction Stop) } catch { return $null } }
function Get-TaskSafe([string]$Name) { try { return (Get-ScheduledTask -TaskName $Name -ErrorAction Stop | Select-Object -First 1) } catch { return $null } }

function Set-ServiceAuto([string]$Name) {
  $svc = Get-ServiceSafe $Name
  if (-not $svc) { return $false }
  if ($svc.StartType -ne 'Automatic') { Invoke-Change "service $Name -> Automatic" { Set-Service -Name $Name -StartupType Automatic } | Out-Null }
  if ($svc.Status -ne 'Running') { Invoke-Change "start service $Name" { Start-Service -Name $Name -ErrorAction Stop } | Out-Null }
  return $true
}

# SHA256 fingerprint of an OpenSSH public key line, like ssh-keygen -lf (never print the key itself).
$KeyPattern = '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) ([A-Za-z0-9+/]+={0,2})( .*)?$'
function Get-KeyFingerprint([string]$Line) {
  $m = [regex]::Match($Line.Trim(), $KeyPattern)
  if (-not $m.Success) { return $null }
  try {
    $blob = [Convert]::FromBase64String($m.Groups[3].Value)
    $sha = [Security.Cryptography.SHA256]::Create().ComputeHash($blob)
    return ('{0} SHA256:{1}' -f $m.Groups[1].Value, ([Convert]::ToBase64String($sha).TrimEnd('=')))
  } catch { return $null }
}

# A public key from a file (first valid line), or pasted. Returns the single line or $null.
function Get-PublicKey([string]$File, [string]$DefaultName, [string]$What, [bool]$Optional) {
  $candidates = @()
  if ($File) { $candidates += $File }
  if ($PSScriptRoot) { $candidates += (Join-Path $PSScriptRoot $DefaultName) }
  foreach ($c in $candidates) {
    if (Test-Path -LiteralPath $c -PathType Leaf) {
      foreach ($line in (Get-Content -LiteralPath $c)) {
        if ($line -match 'PRIVATE KEY') { Write-Bad "$c is a PRIVATE key. Use the .pub file; nothing was read from it."; return $null }
        if (Get-KeyFingerprint $line) { Write-Ok "$What from $c"; return $line.Trim() }
      }
      Write-Warn "$c has no OpenSSH public key line"
    } elseif ($c -eq $File) { Write-Warn "not found: $c" }
  }
  if ($script:DryRun) { Write-Note "(dry run: would ask you to paste the $What)"; return $null }
  $hint = ''
  if ($Optional) { $hint = ' (Enter to skip)' }
  for ($i = 0; $i -lt 3; $i++) {
    $line = Read-Host "    ?     Paste the $What (one line starting ssh-ed25519 ...)$hint"
    if (-not $line) { if ($Optional) { return $null } else { continue } }
    if ($line -match 'PRIVATE KEY') { Write-Bad 'That is a private key. Paste the .pub line instead.'; continue }
    if (Get-KeyFingerprint $line) { return $line.Trim() }
    Write-Warn 'That does not look like an OpenSSH public key.'
  }
  return $null
}

# ------------------------------------------------------------------------------------------------------ elevation
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$IsAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin -and -not $script:DryRun) {
  Write-Host 'Asking for administrator rights (accept the Windows prompt)...' -ForegroundColor White
  $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath))
  foreach ($kv in $PSBoundParameters.GetEnumerator()) {
    if ($kv.Value -is [Management.Automation.SwitchParameter]) { if ($kv.Value.IsPresent) { $argList += "-$($kv.Key)" } }
    else { $argList += "-$($kv.Key)"; $argList += ('"{0}"' -f $kv.Value) }
  }
  $argList += '-Relaunched'
  try { Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList | Out-Null }
  catch { Write-Host "Not elevated: $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
  Write-Host 'Continuing in the administrator window.'
  exit 0
}

if (-not $script:DryRun) {
  foreach ($d in @($StateDir, $ToolsDir)) { if (-not (Test-Path -LiteralPath $d)) { [void][IO.Directory]::CreateDirectory($d) } }
  # Only administrators and SYSTEM may change what is in here (scheduled tasks run scripts from it).
  & icacls.exe $StateDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q | Out-Null
}

# ------------------------------------------------------------------------------------------------------ context
$Computer = [Environment]::MachineName
$UserName = $env:USERNAME
$UserFull = "$env:USERDOMAIN\$env:USERNAME"
$consoleUser = $null
try { $consoleUser = (Get-CimInstance Win32_ComputerSystem).UserName } catch { }
$TotalGB = 16
try { $TotalGB = [int][Math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB) } catch { }
if ($RunnerMemoryGB -le 0) { $RunnerMemoryGB = [int][Math]::Max(6, [Math]::Min([Math]::Floor($TotalGB * 0.6), $TotalGB - 6)) }
if ($RunnerSlots -le 0) { $RunnerSlots = [int][Math]::Max(1, [Math]::Min(4, [Math]::Floor($RunnerMemoryGB / 6))) }

function Find-Repo([string]$Given, [string[]]$Candidates, [string]$Marker) {
  if ($Given) { if (Test-Path -LiteralPath (Join-Path $Given $Marker)) { return (Resolve-Path -LiteralPath $Given).Path } else { return $null } }
  foreach ($c in $Candidates) { if ($c -and (Test-Path -LiteralPath (Join-Path $c $Marker))) { return (Resolve-Path -LiteralPath $c).Path } }
  return $null
}
$repoGuess = $null
if ($PSScriptRoot) { $repoGuess = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$NotifRepo = Find-Repo $NotificationsRepo @($repoGuess, (Join-Path $HOME 'Desktop\Projects\notifications')) 'bus.py'

Write-Host ''
Write-Host 'Headless host setup' -ForegroundColor White
if ($script:DryRun) { Write-Host 'DRY RUN (-WhatIf): nothing on this PC is changed.' -ForegroundColor DarkYellow }
Write-Note "Computer $Computer, user $UserFull, $TotalGB GB RAM, Windows PowerShell $($PSVersionTable.PSVersion)"
Write-Note "Log: $LogFile"
Write-Log "start: computer=$Computer user=$UserFull dryrun=$($script:DryRun) checkonly=$CheckOnly"

if (-not $CheckOnly -and -not $script:DryRun -and -not $Force -and $Computer -ne $ExpectedComputer) {
  Write-Warn "This is $Computer, not $ExpectedComputer (the PC this setup is for)."
  if (-not (Ask-YesNo "Set up $Computer as a headless host anyway?" $false)) { Write-Host 'Stopped; nothing changed.'; if ($Relaunched) { Read-Host 'Press Enter to close' | Out-Null }; exit 1 }
}
if ($consoleUser -and $consoleUser -notlike "*\$UserName") {
  Write-Warn "The signed-in user is $consoleUser but this runs as $UserFull. Run SETUP-HEADLESS from your own account, accepting the prompt with your own admin rights."
}

# ================================================================================================ 1. power
function Step-Power {
  Write-Step '1/9  Power: never sleep on AC, survive power cuts'
  Invoke-Change 'powercfg: sleep after 0 (never) on AC' { & powercfg.exe /change standby-timeout-ac 0 } | Out-Null
  Invoke-Change 'powercfg: hibernate after 0 (never) on AC' { & powercfg.exe /change hibernate-timeout-ac 0 } | Out-Null
  # Turning hibernation off also turns Fast Startup off (it is a hibernate file); a cold boot after a power cut is
  # the same either way, but Fast Startup gets in the way of Wake-on-LAN, driver updates and "restart" vs "shut down".
  Invoke-Change 'powercfg /hibernate off (also turns Fast Startup off)' { & powercfg.exe /hibernate off } | Out-Null
  $pk = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'
  $hb = $null
  try { $hb = (Get-ItemProperty -Path $pk -Name HiberbootEnabled -ErrorAction Stop).HiberbootEnabled } catch { }
  if ($hb -ne 0) { Invoke-Change 'Fast Startup off (HiberbootEnabled = 0)' { Set-ItemProperty -Path $pk -Name HiberbootEnabled -Value 0 -Type DWord } | Out-Null }
  else { Write-Ok 'Fast Startup already off' }
  if (-not $script:DryRun) { Write-Ok 'sleep and hibernate off while plugged in' }

  $board = $null
  try { $board = Get-CimInstance Win32_BaseBoard } catch { }
  $vendor = ''; $product = ''
  if ($board) { $vendor = "$($board.Manufacturer)"; $product = "$($board.Product)" }
  Write-Note "Motherboard: $vendor $product"
  $v = $vendor.ToLowerInvariant()
  if ($v -match 'asus') { $key = 'Del (or F2)'; $path = 'F7 (Advanced Mode) > Advanced > APM Configuration > Restore AC Power Loss = Power On' }
  elseif ($v -match 'micro-star|msi') { $key = 'Del'; $path = 'Settings > Advanced > Power Management Setup > Restore after AC Power Loss = Power On' }
  elseif ($v -match 'gigabyte') { $key = 'Del'; $path = 'Settings > Platform Power > AC BACK = Always On  (older boards: Power Management > AC BACK)' }
  elseif ($v -match 'asrock') { $key = 'F2 or Del'; $path = 'Advanced > Chipset Configuration > Restore on AC/Power Loss = Power On' }
  elseif ($v -match 'dell') { $key = 'F2'; $path = 'Power Management > AC Recovery (or AC Behavior) = Power On' }
  elseif ($v -match 'hewlett|hp') { $key = 'F10'; $path = 'Advanced > Power-On Options > After Power Loss = Power On' }
  elseif ($v -match 'lenovo') { $key = 'F1 (ThinkCentre) or F2'; $path = 'Power > After Power Loss = Power On' }
  elseif ($v -match 'biostar') { $key = 'Del'; $path = 'Chipset > South Bridge (or Power) > Restore on AC Power Loss = Power On' }
  else { $key = 'Del or F2'; $path = 'look under Power / APM / Chipset for "Restore on AC Power Loss", "AC Back" or "After Power Loss" and set it to Power On (Always On)' }
  $script:BiosText = @(
    "BIOS ($vendor $product): Restore on AC Power Loss = Power On",
    "  Enter: press $key while the PC starts, or run  shutdown /r /fw /t 0  in an admin PowerShell (reboots straight into the BIOS).",
    "  Path:  $path",
    '  Save and exit (usually F10). Without this the PC stays OFF after a power cut and nothing remote can help.'
  )
  foreach ($l in $script:BiosText) { Write-Note $l }
  # BitLocker / device encryption: a BIOS change can trigger the recovery screen, the one boot state no remote path
  # gets past. Suspend protection for one reboot before going into the BIOS.
  $bl = $null
  try { $bl = Get-CimInstance -Namespace 'root\cimv2\Security\MicrosoftVolumeEncryption' -ClassName Win32_EncryptableVolume -Filter "DriveLetter='$($env:SystemDrive)'" -ErrorAction Stop } catch { }
  if ($bl -and $bl.ProtectionStatus -eq 1) {
    Write-Warn "$($env:SystemDrive) is BitLocker/device-encrypted. Before the BIOS change run (admin):  manage-bde -protectors -disable $($env:SystemDrive) -rc 1"
    Write-Note 'and keep the recovery key reachable (https://aka.ms/myrecoverykey).'
    Add-Hands "BIOS + BitLocker: first  manage-bde -protectors -disable $($env:SystemDrive) -rc 1  (one reboot), recovery key at aka.ms/myrecoverykey."
  }
  Add-Hands "BIOS (do it last, it reboots): set Restore on AC Power Loss = Power On ($path). Key: $key, or shutdown /r /fw /t 0."
  Add-Result 'power' 'ok' 'sleep/hibernate off on AC'
}

# ================================================================================================ 2. tailscale
function Get-TailscaleExe {
  $p = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
  if (Test-Path -LiteralPath $p) { return $p }
  $c = Get-Command tailscale.exe -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  return $null
}
function Get-TailscaleState {
  $exe = Get-TailscaleExe
  if (-not $exe) { return $null }
  $r = Invoke-Native $exe @('status', '--json')
  try { return ($r.Out | ConvertFrom-Json) } catch { return $null }
}

function Step-Tailscale {
  Write-Step '2/9  Tailscale: unattended (stays up with nobody signed in), Tailscale SSH off'
  $exe = Get-TailscaleExe
  if (-not $exe) {
    Invoke-Change 'winget install Tailscale.Tailscale (machine-wide)' {
      Start-Visible 'winget.exe' 'install --id Tailscale.Tailscale -e --scope machine --silent --accept-source-agreements --accept-package-agreements'
    } | Out-Null
    Update-SessionPath
    $exe = Get-TailscaleExe
    if (-not $exe -and -not $script:DryRun) { Write-Bad 'Tailscale did not install. Install it from https://tailscale.com/download/windows and run this again.'; Add-Result 'tailscale' 'fail' 'not installed'; return }
  } else { Write-Ok "installed: $exe" }
  Set-ServiceAuto 'Tailscale' | Out-Null
  if (-not $exe) { Add-Result 'tailscale' 'skip' 'dry run'; return }

  $st = Get-TailscaleState
  $backend = ''
  if ($st) { $backend = "$($st.BackendState)" }
  if ($backend -eq 'Running') {
    Invoke-Change 'tailscale set --unattended=true --ssh=false' { & $exe set --unattended=true --ssh=false } | Out-Null
  } else {
    Write-Note "Tailscale is '$backend'. Signing in: a link appears below (or a browser opens); approve it with your Tailscale account."
    $code = Invoke-Change "tailscale up --unattended --ssh=false --hostname=$TailscaleHostname" {
      Start-Visible $exe "up --unattended --ssh=false --hostname=$TailscaleHostname"
    }
    if ($code -and $code -ne 0) {
      Write-Warn 'tailscale up refused (usually: other settings were set before). Retrying with --reset.'
      Invoke-Change 'tailscale up --reset --unattended --ssh=false' { Start-Visible $exe "up --reset --unattended --ssh=false --hostname=$TailscaleHostname" } | Out-Null
    }
  }
  $st = Get-TailscaleState
  if ($st -and "$($st.BackendState)" -eq 'Running') {
    $ip = (Invoke-Native $exe @('ip', '-4')).Out.Trim()
    Write-Ok "Tailscale up: $($st.Self.HostName) $ip"
    $exp = $null
    try { $exp = $st.Self.KeyExpiry } catch { }
    if ($exp) { Write-Note "Node key expires $exp unless you turn expiry off." }
  } elseif (-not $script:DryRun) { Write-Bad "Tailscale is not up ($($st.BackendState))" }
  Add-Hands "Tailscale admin console (https://login.tailscale.com/admin/machines): '...' menu on $TailscaleHostname (and on $RunnerName once it appears) > Disable key expiry."
  Add-Result 'tailscale' 'ok' ''
}

# ================================================================================================ 3. openssh
function Set-SshdOption([string[]]$Lines, [string]$Key, [string]$Value) {
  # Sets "Key Value" in the global section (before the first Match block), replacing a live or commented line.
  $out = New-Object System.Collections.ArrayList
  $done = $false; $inMatch = $false
  foreach ($l in $Lines) {
    if ($l -match '^\s*Match\s') {
      if (-not $done) { [void]$out.Add("$Key $Value"); $done = $true }
      $inMatch = $true
    }
    if (-not $inMatch -and -not $done -and $l -match "^\s*#?\s*$Key\s") { [void]$out.Add("$Key $Value"); $done = $true; continue }
    if (-not $inMatch -and $done -and $l -match "^\s*$Key\s") { continue }   # a duplicate later in the global section
    [void]$out.Add($l)
  }
  if (-not $done) { [void]$out.Add("$Key $Value") }
  return , $out.ToArray()
}

function Step-OpenSsh {
  Write-Step '3/9  OpenSSH Server: keys only, port 22 from the tailnet only, PowerShell shell'
  $cap = $null
  try { $cap = Get-WindowsCapability -Online -ErrorAction Stop | Where-Object { $_.Name -like 'OpenSSH.Server*' } | Select-Object -First 1 } catch { Write-Warn "could not read Windows capabilities: $($_.Exception.Message)" }
  if ($cap -and $cap.State -eq 'Installed') { Write-Ok 'OpenSSH Server installed' }
  else {
    $capName = 'OpenSSH.Server~~~~0.0.1.0'
    if ($cap) { $capName = $cap.Name }
    Invoke-Change "Add-WindowsCapability $capName (a few minutes)" { Add-WindowsCapability -Online -Name $capName -ErrorAction Stop | Out-Null } | Out-Null
  }
  # Firewall first, before sshd ever starts: the capability's own rule allows 22 from anywhere (and password auth is
  # still on until sshd_config is edited), so swap it for a tailnet-only rule before the first start.
  $sshdExe = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
  $def = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
  if ($def -and "$($def.Enabled)" -eq 'True') { Invoke-Change 'disable firewall rule OpenSSH-Server-In-TCP (port 22 from anywhere)' { Disable-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' } | Out-Null }
  $rule = Get-NetFirewallRule -Name $FwRule -ErrorAction SilentlyContinue
  if (-not $rule) {
    Invoke-Change "firewall: allow TCP 22 only from $TailnetV4 and $TailnetV6" {
      New-NetFirewallRule -Name $FwRule -DisplayName 'OpenSSH Server (Tailscale only)' -Direction Inbound -Protocol TCP -LocalPort 22 `
        -RemoteAddress @($TailnetV4, $TailnetV6) -Action Allow -Profile Any -Program $sshdExe | Out-Null
    } | Out-Null
  } else {
    Invoke-Change "firewall rule ${FwRule}: re-apply tailnet-only scope" {
      Set-NetFirewallRule -Name $FwRule -Enabled True -Direction Inbound -Action Allow -Profile Any -RemoteAddress @($TailnetV4, $TailnetV6) -Protocol TCP -LocalPort 22
    } | Out-Null
  }

  # The first start creates sshd_config and the host keys.
  if (-not (Set-ServiceAuto 'sshd') -and -not $script:DryRun) { Write-Bad 'sshd service missing after install (a reboot may be needed; run this again after it)'; Add-Result 'ssh' 'fail' 'no sshd'; return }
  $changed = $false

  $cfgPath = Join-Path $env:ProgramData 'ssh\sshd_config'
  if (Test-Path -LiteralPath $cfgPath) {
    $lines = @(Get-Content -LiteralPath $cfgPath)
    $new = $lines
    $new = Set-SshdOption $new 'PubkeyAuthentication' 'yes'
    $new = Set-SshdOption $new 'PasswordAuthentication' 'no'
    $new = Set-SshdOption $new 'PermitEmptyPasswords' 'no'
    if (($new -join "`n") -ne ($lines -join "`n")) {
      Invoke-Change 'sshd_config: PasswordAuthentication no, PubkeyAuthentication yes (backup kept)' {
        Copy-Item -LiteralPath $cfgPath -Destination "$cfgPath.before-headless" -Force -ErrorAction SilentlyContinue
        [IO.File]::WriteAllText($cfgPath, (($new -join "`r`n") + "`r`n"), (New-Object Text.ASCIIEncoding))
        $t = Invoke-Native $sshdExe @('-t')
        if ($t.Code -ne 0) { Copy-Item -LiteralPath "$cfgPath.before-headless" -Destination $cfgPath -Force; throw "sshd -t rejected the new config, restored the old one: $($t.Out)" }
      } | Out-Null
      $changed = $true
    } else { Write-Ok 'sshd_config already keys-only' }
  } elseif (-not $script:DryRun) { Write-Warn "no $cfgPath yet" }

  $shellKey = 'HKLM:\SOFTWARE\OpenSSH'
  $ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
  $cur = $null
  try { $cur = (Get-ItemProperty -Path $shellKey -Name DefaultShell -ErrorAction Stop).DefaultShell } catch { }
  if ($cur -ne $ps) {
    Invoke-Change "SSH default shell -> $ps" {
      if (-not (Test-Path $shellKey)) { New-Item -Path $shellKey -Force | Out-Null }
      New-ItemProperty -Path $shellKey -Name DefaultShell -Value $ps -PropertyType String -Force | Out-Null
    } | Out-Null
  } else { Write-Ok 'default shell is Windows PowerShell' }

  # The MSI public key. This user is an administrator, so Windows OpenSSH reads administrators_authorized_keys
  # (the Match Group administrators block in the default sshd_config), which must be owned by Administrators/SYSTEM only.
  $akPath = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
  $existing = @()
  if (Test-Path -LiteralPath $akPath) { $existing = @(Get-Content -LiteralPath $akPath | Where-Object { $_.Trim() }) }
  $installed = @($existing | Where-Object { Get-KeyFingerprint $_ })
  $haveFile = $PublicKeyFile -or ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'msi-ssh.pub')))
  $key = $null
  if ($installed.Count -gt 0 -and -not $haveFile) {
    # A rerun (e.g. after the WSL restart): the key is already there, do not ask again.
    foreach ($l in $installed) { Write-Ok "key installed: $(Get-KeyFingerprint $l)" }
    $key = $installed[0].Trim()
  } else {
    $key = Get-PublicKey $PublicKeyFile 'msi-ssh.pub' 'MSI public key for SSH logins' ($installed.Count -gt 0)
    if ($key) {
      $fp = Get-KeyFingerprint $key
      $blob = ($key -split ' ')[1]
      if ($existing | Where-Object { $_ -like "*$blob*" }) { Write-Ok "key already installed ($fp)" }
      else {
        Invoke-Change "add key $fp to $akPath" {
          $all = @($existing) + @($key)
          [IO.File]::WriteAllText($akPath, (($all -join "`r`n") + "`r`n"), (New-Object Text.ASCIIEncoding))
        } | Out-Null
        $changed = $true
      }
    }
  }
  if ($key) { $script:SshKeyLine = $key }
  elseif ($installed.Count -eq 0) {
    Write-Warn 'no public key installed: SSH logins will fail until one is. Run this again with the key, or paste it then.'
    Add-Hands 'SSH key: run SETUP-HEADLESS again and paste the MSI public key (type %USERPROFILE%\.ssh\id_ed25519.pub on MSI).'
  }
  if (Test-Path -LiteralPath $akPath) {
    Invoke-Change 'icacls administrators_authorized_keys: Administrators + SYSTEM only' {
      & icacls.exe $akPath /inheritance:r /grant:r '*S-1-5-32-544:F' '*S-1-5-18:F' /Q | Out-Null
    } | Out-Null
  }
  # Restart only when something changed, so a rerun over SSH does not cut its own connection.
  if ($changed -and (Get-ServiceSafe 'sshd')) { Invoke-Change 'restart sshd (config or key changed)' { Restart-Service -Name sshd -Force -ErrorAction Stop } | Out-Null }
  Add-Result 'ssh' 'ok' ''
}

# ================================================================================================ 4. autologon
function Step-Autologon {
  Write-Step '4/9  Autologon after reboot, then lock the screen (apps keep running)'
  # Windows 11 "only allow Windows Hello sign-in for Microsoft accounts" hides the password sign-in that Autologon uses.
  $plk = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
  $pl = $null
  try { $pl = (Get-ItemProperty -Path $plk -Name DevicePasswordLessBuildVersion -ErrorAction Stop).DevicePasswordLessBuildVersion } catch { }
  if ($pl -and $pl -ne 0) { Invoke-Change 'allow password sign-in (DevicePasswordLessBuildVersion = 0), needed by Autologon' { Set-ItemProperty -Path $plk -Name DevicePasswordLessBuildVersion -Value 0 -Type DWord } | Out-Null }

  $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
  $w = Get-ItemProperty -Path $wl -ErrorAction SilentlyContinue
  $auto = $false
  if ($w -and ($w.PSObject.Properties['AutoAdminLogon']) -and "$($w.AutoAdminLogon)" -eq '1' -and $w.PSObject.Properties['DefaultUserName']) { $auto = $true }
  if ($auto -and -not $ReconfigureAutologon) {
    Write-Ok "Autologon already on for $($w.DefaultUserName) (-ReconfigureAutologon to redo it)"
  } else {
    $msa = $false
    try { $msa = ((Get-LocalUser -Name $UserName -ErrorAction Stop).PrincipalSource -eq 'MicrosoftAccount') } catch { }
    $exe = $null
    try {
      if ($script:DryRun) { Write-Note "(dry run: downloading Autologon to a temp folder only to check its signature)" }
      $exe = Get-Download $AutologonUrl 'Autologon64.exe'
    } catch { Write-Bad "could not download $AutologonUrl : $($_.Exception.Message)" }
    if ($exe) {
      $sig = Test-Signed $exe 'Microsoft Corporation'
      if (-not $sig.Ok) {
        Write-Bad "Autologon64.exe signature is not a valid Microsoft one ($($sig.Status), $($sig.Subject)). Not running it."
        Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
        $exe = $null
      } else { Write-Ok "Autologon64.exe signed by Microsoft ($($sig.Status))" }
    }
    if ($exe) {
      Write-Host ''
      Write-Host '    Autologon opens now. In ITS window:' -ForegroundColor White
      if ($msa) {
        Write-Note "  Username: the Microsoft account email you sign in with   Domain: leave as is ($Computer)"
        Write-Note '  Password: the Microsoft account password (not the PIN). Then click Enable, OK, and close it.'
        Write-Note '  (If the PC does not sign in by itself after a reboot, run again with -ReconfigureAutologon and try Domain = MicrosoftAccount.)'
      } else {
        Write-Note "  Username: $UserName   Domain: $Computer   Password: your Windows password. Click Enable, OK, close it."
      }
      Write-Note '  Autologon stores the password encrypted as an LSA secret. This script never sees it.'
      Invoke-Change 'run Autologon64.exe (you type the password into it)' { Start-Process -FilePath $exe -ArgumentList '/accepteula' -Wait } | Out-Null
      $w = Get-ItemProperty -Path $wl -ErrorAction SilentlyContinue
      if ($w -and $w.PSObject.Properties['AutoAdminLogon'] -and "$($w.AutoAdminLogon)" -eq '1') { Write-Ok "Autologon on for $($w.DefaultUserName)" }
      elseif (-not $script:DryRun) { Write-Bad 'Autologon is not on (Enable was not clicked?). Run SETUP-HEADLESS again.'; Add-Hands 'Autologon: run SETUP-HEADLESS again and click Enable in Autologon.' }
    }
  }

  # Lock right after the automatic sign-in, once the apps have started. Only shortly after boot, so a normal sign-in at
  # the desk later is not locked.
  $cmd = "if (((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes -lt 8) { rundll32.exe user32.dll,LockWorkStation }"
  $task = Get-TaskSafe $TaskLock
  if ($task) { Write-Ok "task '$TaskLock' exists" }
  else {
    Invoke-Change "scheduled task '$TaskLock' (at logon + 90 s, only within 8 min of boot)" {
      $action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"$cmd`""
      $trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserFull
      $trigger.Delay = 'PT90S'
      $prin = New-ScheduledTaskPrincipal -UserId $UserFull -LogonType Interactive -RunLevel Limited
      $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
      Register-ScheduledTask -TaskName $TaskLock -Action $action -Trigger $trigger -Principal $prin -Settings $set -Description 'Locks the workstation after Autologon so the session runs but the desk is locked. HeadlessHost.' -Force | Out-Null
    } | Out-Null
  }
  Add-Result 'autologon' 'ok' ''
}

# ================================================================================================ 5. parsec
function Get-ParsecInfo {
  $svc = $null
  try { $svc = Get-CimInstance Win32_Service | Where-Object { $_.PathName -match '\\pservice\.exe' -or $_.Name -eq 'Parsec' } | Select-Object -First 1 } catch { }
  $machine = Test-Path -LiteralPath (Join-Path $env:ProgramFiles 'Parsec\parsecd.exe')
  $perUser = (Test-Path -LiteralPath (Join-Path $env:APPDATA 'Parsec\parsecd.exe')) -or ((Test-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Parsec') -and -not $machine)
  return [pscustomobject]@{ Service = $svc; Machine = $machine; PerUser = $perUser }
}

function Step-Parsec {
  Write-Step '5/9  Parsec per-computer (its service hosts the Windows login screen)'
  $p = Get-ParsecInfo
  $install = $false
  if ($p.Machine -and $p.Service) { Write-Ok "Parsec installed per-computer, service '$($p.Service.Name)'" }
  elseif ($p.PerUser) {
    Write-Warn 'Parsec is installed PER USER: it starts only after you sign in, so it cannot host the login screen or survive a reboot.'
    Write-Note 'Parsec: "Per Computer" install = access at the Windows login screen (runs as SYSTEM); "Per User" starts only after sign-in.'
    Write-Note 'It replaces the per-user install. Source: https://support.parsec.app/hc/en-us/articles/32381368159124-Install-Parsec-App-on-Windows'
    $install = Ask-YesNo 'Reinstall Parsec per-computer now?' $true
    if (-not $install) { Add-Hands 'Parsec: reinstall Per Computer (official installer, choose "Per Computer"), or run SETUP-HEADLESS again and answer Y.' }
  } else { Write-Note 'Parsec not installed'; $install = $true }

  if ($install) {
    $exe = $null
    try { $exe = Get-Download $ParsecUrl 'parsec-windows.exe' } catch { Write-Bad "could not download $ParsecUrl : $($_.Exception.Message)" }
    if ($exe) {
      # Parsec belongs to Unity; its installer is signed "O=Unity Technologies SF" (checked Oct 2026).
      $sig = Test-Signed $exe '(Unity Technologies[^,]*|Parsec[^,]*)'
      if (-not $sig.Ok) { Write-Bad "parsec-windows.exe signature not valid/Unity-Parsec ($($sig.Status), $($sig.Subject)); not running it." }
      else {
        Write-Ok "installer signed: $($sig.Subject)"
        # /allusers = per-machine scope, /silent /norun /nocleanuser = silent switches; both from the Parsec.Parsec
        # winget manifest (microsoft/winget-pkgs manifests/p/Parsec/Parsec), the same official installer URL.
        Invoke-Change 'install Parsec per-computer: parsec-windows.exe /allusers /silent /norun /nocleanuser' {
          $pr = Start-Process -FilePath $exe -ArgumentList '/allusers', '/silent', '/norun', '/nocleanuser' -Wait -PassThru
          if ($pr.ExitCode -ne 0) { Write-Warn "Parsec installer exit code $($pr.ExitCode)" }
        } | Out-Null
      }
    }
  }
  $p = Get-ParsecInfo
  if ($p.Service) {
    Set-ServiceAuto $p.Service.Name | Out-Null
    Write-Ok "Parsec service '$($p.Service.Name)' Automatic"
  } elseif (-not $script:DryRun) { Write-Bad 'no Parsec service found' }
  Add-Hands 'Parsec: open it, sign in, Settings > Host: hosting on; tray icon: "Run when computer starts" on. No monitor attached? enable the Parsec Virtual Display.'
  Add-Result 'parsec' 'ok' ''
}

# ================================================================================================ 6. chrome remote desktop
function Get-CrdStartHost {
  foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
    if (-not $base) { continue }
    $p = Join-Path $base 'Google\Chrome Remote Desktop\CurrentVersion\remoting_start_host.exe'
    if (Test-Path -LiteralPath $p) { return $p }
  }
  return $null
}
function Test-CrdConfigured { return (Test-Path -LiteralPath (Join-Path $env:ProgramData 'Google\Chrome Remote Desktop\host.json')) }

function Step-Crd {
  Write-Step '6/9  Chrome Remote Desktop host (backup way in, works at the login screen)'
  if (Get-ServiceSafe 'chromoting') { Write-Ok 'Chrome Remote Desktop host installed' }
  else {
    $msi = $null
    try { $msi = Get-Download $CrdUrl 'chromeremotedesktophost.msi' } catch { Write-Bad "could not download $CrdUrl : $($_.Exception.Message)" }
    if ($msi) {
      $sig = Test-Signed $msi 'Google LLC'
      if (-not $sig.Ok) { Write-Bad "MSI signature not a valid Google one ($($sig.Status), $($sig.Subject)); not installing." }
      else {
        Write-Ok "MSI signed by Google ($($sig.Status))"
        Invoke-Change 'msiexec /i chromeremotedesktophost.msi /qn' {
          $pr = Start-Process -FilePath 'msiexec.exe' -ArgumentList '/i', ('"{0}"' -f $msi), '/qn', '/norestart' -Wait -PassThru
          if ($pr.ExitCode -ne 0 -and $pr.ExitCode -ne 3010) { Write-Warn "msiexec exit code $($pr.ExitCode)" }
        } | Out-Null
      }
    }
  }
  Set-ServiceAuto 'chromoting' | Out-Null
  if (Test-CrdConfigured) { Write-Ok 'host is registered to your Google account' }
  else {
    Write-Warn 'host installed but not registered yet (needs your Google sign-in and a PIN)'
    Write-Note 'On any PC open https://remotedesktop.google.com/headless > Begin > Next > Authorize, copy the Windows (PowerShell)'
    Write-Note 'line and run it in an ADMIN PowerShell here; it asks for a 6+ digit PIN. (Or sign in at remotedesktop.google.com/access on this PC.)'
    Add-Hands 'Chrome Remote Desktop: remotedesktop.google.com/headless > Authorize > run the Windows command in an admin PowerShell on this PC > choose a PIN.'
  }
  Add-Result 'crd' 'ok' ''
}

# ================================================================================================ 7. remote ops helper
function Step-RemoteOps {
  Write-Step '7/9  Agent Bus: LocalNotify service + remote ops helper'
  $ln = Get-ServiceSafe 'LocalNotify'
  if ($ln) { Write-Ok "LocalNotify service $($ln.Status)" } else { Write-Warn 'LocalNotify (Agent Bus) service not found on this PC' }
  if (-not $NotifRepo) { Write-Warn 'notifications repo not found (pass -NotificationsRepo)'; Add-Result 'remote-ops' 'fail' 'no repo'; return }
  $inst = Join-Path $NotifRepo 'Install-RemoteOps.ps1'
  if (-not (Test-Path -LiteralPath $inst)) {
    Write-Warn "$inst is missing: this copy of the notifications repo is older. Update it (git pull) and run SETUP-HEADLESS again."
    Add-Hands 'Remote ops: update the notifications repo on this PC (git pull), then run SETUP-HEADLESS again.'
    Add-Result 'remote-ops' 'fail' 'installer missing'; return
  }
  $args2 = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $inst)
  $tmpKey = $null
  if ($script:SshKeyLine) {
    # The same MSI public key verifies signed self-update tags. Passed through a temp file that is deleted right after.
    $tmpKey = Join-Path $env:TEMP ('headless-signing-{0}.pub' -f [guid]::NewGuid().ToString('N'))
    if (-not $script:DryRun) { [IO.File]::WriteAllText($tmpKey, $script:SshKeyLine + "`n", (New-Object Text.ASCIIEncoding)) }
    $args2 += @('-SigningKeyFile', $tmpKey)
  }
  if ($script:DryRun) { $args2 += '-WhatIf'; Write-Note 'dry run: calling Install-RemoteOps.ps1 -WhatIf' ; & powershell.exe @args2 }
  else {
    & powershell.exe @args2
    if ($LASTEXITCODE -ne 0) { Write-Bad "Install-RemoteOps.ps1 exited $LASTEXITCODE" }
  }
  if ($tmpKey) { Remove-Item -LiteralPath $tmpKey -Force -ErrorAction SilentlyContinue }
  Add-Result 'remote-ops' 'ok' ''
}

# ================================================================================================ 8. runner
function Install-WingetIfMissing([string]$Command, [string]$Id, [string]$Label) {
  if (Get-Command $Command -ErrorAction SilentlyContinue) { Write-Ok "$Label present"; return }
  Invoke-Change "winget install $Id" { Start-Visible 'winget.exe' "install --id $Id -e --silent --accept-source-agreements --accept-package-agreements" } | Out-Null
  Update-SessionPath
}

function Invoke-Wsl([string]$Distro, [string[]]$Arguments) { return (Invoke-Native 'wsl.exe' (@('-d', $Distro, '-u', 'root', '--') + $Arguments)) }
function Get-WslDistros {
  $r = Invoke-Native 'wsl.exe' @('-l', '-q')
  if ($r.Code -ne 0) { return @() }
  return @($r.Out -replace "`0", '' -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
function Test-RunnerDistro([string]$Distro) { return ((Invoke-Wsl $Distro @('test', '-f', '/home/runner/test-runner/runner.json')).Code -eq 0) }

function Step-Runner {
  Write-Step "8/9  Runner: tools, trun test runner '$RunnerName' (WSL, $RunnerMemoryGB GB, $RunnerSlots slots), Planner runner"
  Install-WingetIfMissing 'git.exe' 'Git.Git' 'Git'
  Install-WingetIfMissing 'node.exe' 'OpenJS.NodeJS.LTS' 'Node LTS'
  Install-WingetIfMissing 'python.exe' 'Python.Python.3.12' 'Python'

  # Docker: the runner gets its own rootless Docker inside its WSL distro (trun's INSTALL_DOCKER=1), so jobs with
  # "needs": ["docker"] land here. Docker Desktop, if present, is only made to start at sign-in.
  foreach ($f in @((Join-Path $env:APPDATA 'Docker\settings-store.json'), (Join-Path $env:APPDATA 'Docker\settings.json'))) {
    if (-not (Test-Path -LiteralPath $f)) { continue }
    try {
      $j = Get-Content -LiteralPath $f -Raw | ConvertFrom-Json
      $prop = 'AutoStart'
      if ($f -like '*settings.json') { $prop = 'autoStart' }
      if (-not ($j.PSObject.Properties[$prop]) -or -not $j.$prop) {
        Invoke-Change "Docker Desktop: start at sign-in ($prop in $(Split-Path -Leaf $f))" {
          if ($j.PSObject.Properties[$prop]) { $j.$prop = $true } else { $j | Add-Member -NotePropertyName $prop -NotePropertyValue $true }
          [IO.File]::WriteAllText($f, ($j | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
        } | Out-Null
      } else { Write-Ok 'Docker Desktop starts at sign-in' }
    } catch { Write-Warn "could not read $f" }
    break
  }

  # ---- trun
  $trun = Find-Repo $TrunRepo @((Join-Path $HOME 'Desktop\Projects\test-runner'), (Join-Path $HOME 'Desktop\Projects\testsmemoryshare\incoming\test-runner')) 'runner\setup-wsl.sh'
  if (-not $trun) {
    $dest = Join-Path $HOME 'Desktop\Projects\test-runner'
    Invoke-Change "git clone https://github.com/cwoodard1121/test-runner $dest (private: Git may open a GitHub sign-in)" { Start-Visible 'git.exe' "clone https://github.com/cwoodard1121/test-runner `"$dest`"" } | Out-Null
    $trun = Find-Repo $dest @() 'runner\setup-wsl.sh'
  } else {
    Write-Ok "trun repo: $trun"
    Invoke-Change "git pull --ff-only in $trun" { $r = Invoke-Native 'git.exe' @('-C', $trun, 'pull', '--ff-only'); if ($r.Code -ne 0) { Write-Warn "pull failed, using the current checkout: $($r.Out)" } } | Out-Null
  }
  if (-not $trun) { Write-Bad 'no trun repo; runner skipped'; Add-Result 'runner' 'fail' 'no trun repo'; Step-Planner; return }

  $distros = Get-WslDistros
  $distro = $RunnerDistro
  if (-not $distro) {
    if ($distros -contains 'Ubuntu-24.04' -and -not (Test-RunnerDistro 'Ubuntu-24.04')) {
      $distro = 'trun-runner'   # never take over a distro someone works in: setup-wsl.sh cuts it off from Windows
      Write-Note "Ubuntu-24.04 exists and is not a runner, so the runner gets its own distro '$distro'."
    } else { $distro = 'Ubuntu-24.04' }
  }
  if ($distros -notcontains $distro -and $distro -ne 'Ubuntu-24.04') {
    Invoke-Change "wsl --install Ubuntu-24.04 --name $distro --no-launch" { Start-Visible 'wsl.exe' "--install Ubuntu-24.04 --name $distro --no-launch" } | Out-Null
  }
  $setupWin = Join-Path $trun 'runner\setup-windows.ps1'
  Invoke-Change "trun setup-windows.ps1 -MemoryGB $RunnerMemoryGB -Distro $distro (WSL install/.wslconfig/keep-alive task)" {
    Start-Visible 'powershell.exe' "-NoProfile -ExecutionPolicy Bypass -File `"$setupWin`" -MemoryGB $RunnerMemoryGB -Distro $distro"
  } | Out-Null
  if ($script:DryRun) { Add-Result 'runner' 'skip' 'dry run'; Step-Planner; return }
  if ((Get-WslDistros) -notcontains $distro) {
    Write-Warn "WSL distro $distro is not ready yet. If Windows asked for a restart, restart; finish Ubuntu's first start (pick a Linux user name/password) if it opened, then run SETUP-HEADLESS again."
    Add-Hands 'Runner: restart if asked, finish Ubuntu first start, then run SETUP-HEADLESS again (it continues where it stopped).'
    Add-Result 'runner' 'fail' 'distro pending'; Step-Planner; return
  }

  # Copy the repo into the distro (works even after setup-wsl.sh turns off /mnt/c). cmd, not a PowerShell pipe: Windows
  # PowerShell would re-encode the tar stream.
  $copy = "tar -C `"$trun`" -cf - runner src package.json | wsl.exe -d $distro -u root -- sh -c `"rm -rf /root/trun-setup && mkdir -p /root/trun-setup && tar -C /root/trun-setup -xf -`""
  & cmd.exe /c $copy
  if ($LASTEXITCODE -ne 0) { Write-Bad "could not copy trun into $distro ($LASTEXITCODE)"; Add-Result 'runner' 'fail' 'copy'; Step-Planner; return }

  $envs = @("RUNNER_NAME=$RunnerName", "RUNNER_SLOTS=$RunnerSlots", "PLAYWRIGHT_VERSION=$PlaywrightVersion")
  if (-not $NoRunnerDocker) { $envs += 'INSTALL_DOCKER=1' }
  if (Test-RunnerDistro $distro) {
    Write-Ok "runner already set up in $distro; updating its agent"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $trun 'runner\update-agent.ps1') -Distro $distro
  } else {
    for ($pass = 1; $pass -le 2; $pass++) {
      $hadSystemd = ((Invoke-Wsl $distro @('grep', '-q', '^systemd=true', '/etc/wsl.conf')).Code -eq 0)
      Write-Note "setup-wsl.sh pass $pass (apt, Node, Redis, sshd, rootless Docker, Tailscale: it prints a sign-in link for '$RunnerName')"
      & wsl.exe -d $distro -u root -- env @envs bash /root/trun-setup/runner/setup-wsl.sh
      $code = $LASTEXITCODE
      if ($code -ne 0) { Write-Bad "setup-wsl.sh failed ($code)"; break }
      & wsl.exe --terminate $distro | Out-Null    # systemd on (pass 1) / Windows drives off (pass 2) take effect
      Start-ScheduledTask -TaskName $TaskTrun -ErrorAction SilentlyContinue
      if ($hadSystemd) { break }
    }
  }
  # Client keys: MSI's trun key (file or paste) and this PC's own, if it has one.
  $keys = @()
  $k = Get-PublicKey $TrunClientKeyFile 'msi-test-runner.pub' "trun client key (MSI's ~/.ssh/test-runner.pub)" $true
  if ($k) { $keys += $k }
  $own = Join-Path $HOME '.ssh\test-runner.pub'
  if (Test-Path -LiteralPath $own) { $l = (Get-Content -LiteralPath $own | Select-Object -First 1); if (Get-KeyFingerprint $l) { $keys += $l.Trim() } }
  foreach ($key in $keys) {
    $r = Invoke-Wsl $distro @('bash', '/root/trun-setup/runner/add-key.sh', $key)
    Write-Note "trun key $(Get-KeyFingerprint $key): $($r.Out.Trim())"
  }
  if (-not $k) { Add-Hands "trun: on MSI make a client key (ssh-keygen -t ed25519 -f ~/.ssh/test-runner -N `"`"), add it here (SETUP-HEADLESS -TrunClientKeyFile, or over SSH: wsl -d $distro -u root -- bash /root/trun-setup/runner/add-key.sh '<key>'), then on MSI: trun add $RunnerName runner@$RunnerName" }
  else { Add-Hands "trun: on MSI run  trun add $RunnerName runner@$RunnerName  then  trun runners" }
  $script:RunnerDistroUsed = $distro
  Add-Result 'runner' 'ok' $distro
  Step-Planner
}

# ---- Planner: the logon runner (its own task) plus a pre-login copy for power cuts
function Get-PlannerHeartbeat {
  $f = Join-Path $env:APPDATA 'Planner\runner-service.json'
  if (-not (Test-Path -LiteralPath $f)) { return $null }
  try { return (Get-Content -LiteralPath $f -Raw | ConvertFrom-Json) } catch { return $null }
}
function Test-PlannerAlive {
  $hb = Get-PlannerHeartbeat
  if (-not $hb) { return $false }
  try {
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - [double]$hb.updatedAt
    return ($age -lt 45000 -and -not ($hb.PSObject.Properties['stopped'] -and $hb.stopped) -and (Get-Process -Id ([int]$hb.pid) -ErrorAction SilentlyContinue))
  } catch { return $false }
}

$PreLoginScript = @'
# Planner Runner (pre-login): started by Task Scheduler at boot, as Cameron, with his stored password (so DPAPI can
# open the runner token and Claude/bus.py find his profile). Runs `Planner.exe --dispatch-service` only while nobody is
# signed in at the console, so Planner jobs come back after a power cut even if Autologon fails. Once he is signed in
# (Autologon), it stops the runner it started and hands over to the normal "Planner Runner" logon task, which can also
# drive his Chrome. Written by Setup-HeadlessHost.ps1; HeadlessHost.
$ErrorActionPreference = 'SilentlyContinue'
$exe = Join-Path $env:LOCALAPPDATA 'Programs\Planner\Planner.exe'
# Not ProgramData\HeadlessHost: that folder is admin-only and this task runs with a filtered (non-admin) token.
$logDir = Join-Path $env:APPDATA 'Planner\logs'
$log = Join-Path $logDir 'planner-prelogin.log'
function Log($t) { try { if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }; Add-Content -LiteralPath $log -Value ('{0:u} {1}' -f (Get-Date), $t) } catch { } }
function SignedIn { $u = (Get-CimInstance Win32_ComputerSystem).UserName; return [bool]$u }
if (-not (Test-Path -LiteralPath $exe)) { Log "no $exe"; exit 0 }
if (SignedIn) { Log 'someone is signed in; the logon task runs Planner'; exit 0 }
$p = Start-Process -FilePath $exe -ArgumentList '--dispatch-service' -PassThru
Log "started Planner runner pid $($p.Id) before sign-in"
while ($true) {
  Start-Sleep -Seconds 5
  if ($p.HasExited) { Log "runner exited ($($p.ExitCode))"; exit 1 }
  if (SignedIn) {
    Log 'signed in: stopping the pre-login runner and starting the logon task'
    & taskkill.exe /PID $p.Id /T /F | Out-Null
    Start-Sleep -Seconds 3
    & schtasks.exe /Run /TN 'Planner Runner' | Out-Null
    exit 0
  }
}
'@

function Step-Planner {
  Write-Host ''
  Write-Host '    -- Planner runner' -ForegroundColor Cyan
  $exe = Join-Path $env:LOCALAPPDATA 'Programs\Planner\Planner.exe'
  $task = Get-TaskSafe $TaskPlanner
  if ($task) {
    Write-Ok "task '$TaskPlanner' registered ($($task.State))"
    if ((Test-PlannerAlive)) { Write-Ok 'Planner background runner is alive' }
    else { Invoke-Change "start task '$TaskPlanner'" { Start-ScheduledTask -TaskName $TaskPlanner } | Out-Null }
  } else {
    $ps = $PlannerSetupScript
    if (-not $ps) {
      foreach ($c in @((Join-Path $HOME 'Desktop\Projects\gcalfiller-uwo\app\scripts\second-computer\Setup-PlannerDesktop.ps1'), (Join-Path $PSScriptRoot 'Setup-PlannerDesktop.ps1'))) {
        if (Test-Path -LiteralPath $c) { $ps = $c; break }
      }
    }
    if ($ps) {
      # It installs per user and signs in through the browser, so it must not run elevated: hand a .cmd to Explorer,
      # which starts it with your normal (non-admin) rights in a new window.
      $cmdFile = Join-Path $env:TEMP 'headless-planner-setup.cmd'
      Invoke-Change "run Setup-PlannerDesktop.ps1 (non-elevated window): $ps" {
        [IO.File]::WriteAllText($cmdFile, "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps`"`r`npause`r`n", (New-Object Text.ASCIIEncoding))
        Start-Process -FilePath 'explorer.exe' -ArgumentList ('"{0}"' -f $cmdFile)
      } | Out-Null
      Add-Hands 'Planner: finish the browser sign-in in the Planner setup window; Settings > Computers: this PC = Run agent jobs (Track my focus off).'
    } else {
      Write-Warn 'Planner runner task missing and Setup-PlannerDesktop.ps1 not found'
      Add-Hands 'Planner: get Planner-Setup-*.exe (planner Settings > Computers) and run gcalfiller-uwo\app\scripts\second-computer\Setup-PlannerDesktop.ps1.'
    }
  }

  if ($SkipPlannerPreLogin) { return }
  if (-not (Test-Path -LiteralPath $exe)) { Write-Note "pre-login runner: skipped until Planner is installed ($exe)"; return }
  $pre = Join-Path $StateDir 'Planner-PreLogin.ps1'
  Invoke-Change "write $pre" { [IO.File]::WriteAllText($pre, $PreLoginScript, (New-Object Text.ASCIIEncoding)) } | Out-Null
  if (Get-TaskSafe $TaskPlannerPre) { Write-Ok "task '$TaskPlannerPre' registered"; return }
  # "Run whether user is logged on or not" with a stored password, not S4U: the runner token is DPAPI CurrentUser, and an
  # S4U logon has no credentials to open it. Task Scheduler (schtasks /RP *) asks for the password itself.
  $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>HeadlessHost</Author><Description>Planner background runner before sign-in (after a power cut). Hands over to "Planner Runner" at sign-in. HeadlessHost.</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT1M</Delay></BootTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>$UserFull</UserId><LogonType>Password</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Hidden>true</Hidden>
    <RestartOnFailure><Interval>PT1M</Interval><Count>30</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>powershell.exe</Command><Arguments>-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "$pre"</Arguments></Exec></Actions>
</Task>
"@
  $xmlFile = Join-Path $env:TEMP 'headless-planner-prelogin.xml'
  Write-Note "Task Scheduler now asks for your Windows password (for the pre-login Planner runner). Typed into schtasks, not this script."
  $ok = Invoke-Change "schtasks /Create '$TaskPlannerPre' (at startup, run whether signed in or not; schtasks prompts for the password)" {
    [IO.File]::WriteAllText($xmlFile, $xml, (New-Object Text.UnicodeEncoding($false, $true)))
    $c = Start-Visible 'schtasks.exe' "/Create /TN `"$TaskPlannerPre`" /XML `"$xmlFile`" /RU `"$UserFull`" /RP * /F"
    Remove-Item -LiteralPath $xmlFile -Force -ErrorAction SilentlyContinue
    ($c -eq 0)
  }
  if ($ok -eq $false) { Write-Warn 'pre-login task not created'; Add-Hands "Planner pre-login: run SETUP-HEADLESS again (or -SkipPlannerPreLogin)." }
}

# ================================================================================================ 9. self-check
function Get-SleepAc {
  $r = Invoke-Native 'powercfg.exe' @('/q', 'SCHEME_CURRENT', 'SUB_SLEEP', 'STANDBYIDLE')
  $hex = @([regex]::Matches($r.Out, '0x([0-9a-fA-F]{8})') | ForEach-Object { $_.Groups[1].Value })
  if ($hex.Count -lt 2) { return $null }
  return [Convert]::ToInt32($hex[$hex.Count - 2], 16)   # the last two are the AC and DC values
}

function Step-SelfCheck {
  Write-Step '9/9  Self-check'
  $checks = [ordered]@{}
  function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $checks[$Name] = [ordered]@{ ok = $Ok; detail = $Detail }
    $label = $Name.PadRight(20)
    if ($Ok) { Write-Host "    [ OK ] $label $Detail" -ForegroundColor Green } else { Write-Host "    [FAIL] $label $Detail" -ForegroundColor Red }
  }

  $st = Get-TailscaleState; $ip = ''
  $exe = Get-TailscaleExe
  if ($exe) { $ip = (Invoke-Native $exe @('ip', '-4')).Out.Trim() }
  $tsUp = $st -and "$($st.BackendState)" -eq 'Running'
  $tsDetail = 'not installed'
  if ($st) { $tsDetail = "$($st.BackendState) $ip" }
  Add-Check 'tailscale up' $tsUp $tsDetail

  $listen = @(Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue)
  $ak = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
  $nkeys = 0
  if (Test-Path -LiteralPath $ak) { $nkeys = @(Get-Content -LiteralPath $ak | Where-Object { Get-KeyFingerprint $_ }).Count }
  $fw = Get-NetFirewallRule -Name $FwRule -ErrorAction SilentlyContinue
  Add-Check 'sshd listening' (($listen.Count -gt 0) -and $nkeys -gt 0 -and [bool]$fw) ("port 22: $($listen.Count -gt 0), keys: $nkeys, tailnet-only rule: $([bool]$fw)")

  $p = Get-ParsecInfo
  $pd = 'not installed'
  if ($p.Service) { $pd = "$($p.Service.Name) $($p.Service.State), $($p.Service.StartMode)" }
  if ($p.PerUser -and -not $p.Machine) { $pd += ' (PER USER install)' }
  Add-Check 'parsec service' ([bool]$p.Service -and $p.Service.State -eq 'Running' -and $p.Machine) $pd

  $crd = Get-ServiceSafe 'chromoting'
  $cd = 'not installed'
  if ($crd) { $cd = "$($crd.Status), registered: $(Test-CrdConfigured)" }
  Add-Check 'chrome remote desk' ([bool]$crd -and (Test-CrdConfigured)) $cd

  $w = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -ErrorAction SilentlyContinue
  $al = $w -and $w.PSObject.Properties['AutoAdminLogon'] -and "$($w.AutoAdminLogon)" -eq '1'
  $ad = 'off'
  if ($al) { $ad = "on for $($w.DefaultUserName); lock task: $([bool](Get-TaskSafe $TaskLock))" }
  Add-Check 'autologon set' ([bool]$al) $ad

  $sl = Get-SleepAc
  $hib = $false
  try { $hib = -not (Test-Path -LiteralPath (Join-Path $env:SystemDrive 'hiberfil.sys')) } catch { }
  Add-Check 'sleep off' ($sl -eq 0) ("AC sleep timeout: $sl s, hibernation off: $hib")

  $distro = $script:RunnerDistroUsed
  if (-not $distro) { foreach ($d in (Get-WslDistros)) { if ($d -in @('Ubuntu-24.04', 'trun-runner', $RunnerDistro) -and (Test-RunnerDistro $d)) { $distro = $d; break } } }
  $dockerOk = $false; $dd = 'no runner distro'
  if ($distro) {
    # No double quotes inside these: Windows PowerShell 5.1 does not escape them for native programs.
    $r = Invoke-Wsl $distro @('sh', '-c', 'U=$(id -u runner) && sudo -u runner env XDG_RUNTIME_DIR=/run/user/$U DOCKER_HOST=unix:///run/user/$U/docker.sock docker info --format {{.ServerVersion}}')
    if ($r.Code -eq 0 -and $r.Out.Trim()) { $dockerOk = $true; $dd = "rootless Docker $($r.Out.Trim()) in $distro" } else { $dd = "no Docker in $distro" }
  }
  if (-not $dockerOk -and (Get-Command docker.exe -ErrorAction SilentlyContinue)) {
    $r = Invoke-Native 'docker.exe' @('info', '--format', '{{.ServerVersion}}')
    if ($r.Code -eq 0 -and $r.Out.Trim()) { $dockerOk = $true; $dd = "Docker Desktop $($r.Out.Trim())" }
  }
  if ($NoRunnerDocker -and -not $dockerOk) { $dd += ' (skipped: -NoRunnerDocker)' }
  Add-Check 'docker ok' $dockerOk $dd

  $runOk = $false; $rd = 'not set up'
  if ($distro) {
    $sshOn = (Invoke-Wsl $distro @('systemctl', 'is-active', '--quiet', 'ssh')).Code -eq 0
    $nk = (Invoke-Wsl $distro @('sh', '-c', 'grep -c restrict,command /home/runner/.ssh/authorized_keys 2>/dev/null || echo 0')).Out.Trim()
    $tsr = (Invoke-Wsl $distro @('sh', '-c', 'tailscale ip -4 2>/dev/null | head -1')).Out.Trim()
    $rn = (Invoke-Wsl $distro @('jq', '-r', '.name', '/home/runner/test-runner/runner.json')).Out.Trim()
    $runOk = $sshOn -and $tsr -and ($nk -match '^[1-9]')
    $rd = "'$rn' in $distro, sshd: $sshOn, client keys: $nk, tailnet: $tsr"
  }
  Add-Check 'runner registered' ([bool]$runOk) $rd

  $pl = Test-PlannerAlive
  Add-Check 'planner runner' ([bool]$pl) ("logon task: $([bool](Get-TaskSafe $TaskPlanner)), heartbeat fresh: $pl, pre-login task: $([bool](Get-TaskSafe $TaskPlannerPre))")
  $ln = Get-ServiceSafe 'LocalNotify'; $ops = Get-ServiceSafe 'AgentBusOps'
  $lnd = 'missing'; if ($ln) { $lnd = "$($ln.Status)" }
  $opd = 'missing'; if ($ops) { $opd = "$($ops.Status)" }
  Add-Check 'agent bus + ops' ([bool]$ln -and $ln.Status -eq 'Running' -and [bool]$ops -and $ops.Status -eq 'Running') ("LocalNotify: $lnd, AgentBusOps: $opd")

  $status = [ordered]@{
    computer = $Computer; user = $UserFull; time = (Get-Date).ToString('o'); dryRun = $script:DryRun
    tailscaleHostname = $TailscaleHostname; tailscaleIp = $ip; runner = $RunnerName; runnerDistro = $distro
    checks = $checks; todo = @($script:Hands)
  }
  $json = $status | ConvertTo-Json -Depth 6
  $target = $StatusFile
  if ($script:DryRun -or -not (Test-Path -LiteralPath $StateDir)) { $target = Join-Path $env:TEMP 'HeadlessHost-status.json' }
  try { [IO.File]::WriteAllText($target, $json, (New-Object Text.UTF8Encoding($false))); Write-Note "status written: $target" } catch { Write-Warn "could not write $target" }
  return $checks
}

# ================================================================================================ run
$steps = @(
  @{ Name = 'power'; Fn = { Step-Power } },
  @{ Name = 'tailscale'; Fn = { Step-Tailscale } },
  @{ Name = 'ssh'; Fn = { Step-OpenSsh } },
  @{ Name = 'autologon'; Fn = { Step-Autologon } },
  @{ Name = 'parsec'; Fn = { if ($SkipParsec) { Write-Step '5/9  Parsec: skipped (-SkipParsec)' } else { Step-Parsec } } },
  @{ Name = 'crd'; Fn = { if ($SkipChromeRemoteDesktop) { Write-Step '6/9  Chrome Remote Desktop: skipped' } else { Step-Crd } } },
  @{ Name = 'remote-ops'; Fn = { Step-RemoteOps } },
  @{ Name = 'runner'; Fn = { if ($SkipRunner) { Write-Step '8/9  Runner: skipped (-SkipRunner)'; Step-Planner } else { Step-Runner } } }
)
$started = Get-Date
if (-not $CheckOnly) {
  foreach ($s in $steps) {
    try { & $s.Fn } catch { Write-Bad "step $($s.Name) failed: $($_.Exception.Message)"; Add-Result $s.Name 'fail' $_.Exception.Message }
  }
  # The reminder wallpaper has done its job: put the old one back (Set-ReminderWallpaper.ps1 saved it).
  $wpScript = Join-Path $PSScriptRoot 'Set-ReminderWallpaper.ps1'
  if ((Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'HeadlessHost\wallpaper-previous.txt')) -and (Test-Path -LiteralPath $wpScript)) {
    Invoke-Change 'restore the wallpaper from before the reminder' { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wpScript -Restore } | Out-Null
  }
}
$checks = $null
try { $checks = Step-SelfCheck } catch { Write-Bad "self-check failed: $($_.Exception.Message)" }

Write-Host ''
Write-Host '---------------------------------------------------------------' -ForegroundColor DarkGray
if ($script:Hands.Count -gt 0) {
  Write-Host 'Still needs your hands:' -ForegroundColor White
  $i = 1
  # BIOS last: it reboots the PC.
  $ordered = @($script:Hands | Where-Object { $_ -notlike 'BIOS*' }) + @($script:Hands | Where-Object { $_ -like 'BIOS*' })
  foreach ($h in $ordered) { Write-Note ("{0}. {1}" -f $i, $h); $i++ }
}
Write-Host ''
Write-Host 'Test from MSI afterwards:' -ForegroundColor White
Write-Note "ssh $UserName@$TailscaleHostname            (PowerShell on this PC, key only)"
Write-Note "python bus.py remote $Computer status        (Agent Bus remote ops)"
Write-Note 'Re-check any time:  SETUP-HEADLESS.cmd -CheckOnly   (also works over SSH)'
Write-Note ("Took {0:N0} min. Log: {1}" -f ((Get-Date) - $started).TotalMinutes, $LogFile)
Write-Log 'end'
if ($Relaunched) { Write-Host ''; Read-Host 'Press Enter to close' | Out-Null }
