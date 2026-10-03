<#
.SYNOPSIS
  Sets up Cameron's Agent Bus on another computer and links it to his other PCs through the planner relay.

.DESCRIPTION
  Run it from the unzipped notifications folder (agent-bus.zip from planner Settings > Computers > Set up another
  computer, unzipped into C:\Users\<you>\Desktop\Projects\notifications):

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Setup-RelayMachine.ps1

  1. Python 3.11+ (installs Python 3.12 for this user with winget when it is missing).
  2. config.json with `python notify.py setup local` when there is none (an existing config.json is never changed).
  3. The LocalNotify Windows service (Install-Service.ps1 asks for administrator rights), or with -SkipService a
     background process (Start-Notify.ps1).
  4. The Agent Bus clients: Claude Code plugin, Codex skill, Claude desktop app MCP server (Install-AgentBus.ps1).
  5. `python notify.py relay pair` (sign in to the planner in the browser), then `python notify.py relay status`.

  Idempotent: run it again any time. It never prints secrets. Guide: docs\RELAY.md.

.PARAMETER Server       Planner server (default https://146-190-249-220.sslip.io).
.PARAMETER Machine      This PC's name on the relay (default: its hostname).
.PARAMETER SkipService  Run Local Notify as a background process instead of a Windows service (no admin prompt).
.PARAMETER NoPair       Do not pair now (later: python notify.py relay pair).
#>
[CmdletBinding()]
param(
    [string]$Server = 'https://146-190-249-220.sslip.io',
    [string]$Machine,
    [switch]$SkipService,
    [switch]$NoPair
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
$NotifyPy = Join-Path $Root 'notify.py'
# Python output goes through a pipe below; unbuffered, so the pairing link shows up while it waits for the browser.
$env:PYTHONUNBUFFERED = '1'

function Step([string]$Text) { Write-Output ''; Write-Output "== $Text" }
function Info([string]$Text) { Write-Output "   $Text" }

# Native programs write progress to stderr; under 'Stop' Windows PowerShell 5.1 would turn that into an exception.
function Invoke-Tool([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments | Out-Host
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-Script([string]$Name, [string[]]$Arguments) {
    $all = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Root $Name)) + $Arguments
    return Invoke-Tool (Get-Command powershell.exe).Source $all
}

function Get-PyOutput([string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $script:Py @Arguments 2>$null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { return $null }
    return ((@($out) | ForEach-Object { "$_" }) -join "`n").Trim()
}

function Find-Python {
    $candidates = @()
    foreach ($name in @('python', 'python3', 'py')) {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            if ($cmd.Source -notlike '*\WindowsApps\*') { $candidates += $cmd.Source }
        }
    }
    foreach ($version in @('Python313', 'Python312', 'Python311')) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\$version\python.exe")
    }
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $out = @(& $candidate -c 'import sys; print(sys.executable); print(sys.version_info >= (3, 11))' 2>$null)
            $code = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($code -eq 0 -and $out.Count -ge 2 -and "$($out[1])".Trim() -eq 'True') { return "$($out[0])".Trim() }
    }
    return $null
}

Write-Output "Agent Bus: set up this computer ($env:COMPUTERNAME) and link it through the planner relay"
Write-Output "   folder: $Root"
Write-Output "   server: $Server"
if ($Machine -and $Machine -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$') {
    throw '-Machine must be 1-40 letters, digits, . _ - starting with a letter or digit.'
}
foreach ($file in @('notify.py', 'config.py', 'service.py', 'relay_client.py', 'config.example.json', 'Install-Service.ps1',
                    'Install-AgentBus.ps1', 'Start-Notify.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $file))) {
        throw "Missing $file in $Root. Run this script from the unzipped notifications folder."
    }
}
# Files from a downloaded zip carry the internet mark; the scripts below must run without prompts.
Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

# ------------------------------------------------------------------ 1. Python
Step 'Python 3.11+'
$script:Py = Find-Python
if (-not $script:Py) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Python 3.11+ is missing and winget is not available. Install Python 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this script again.'
    }
    Info 'Not found: installing Python 3.12 for this user with winget...'
    $code = Invoke-Tool $winget.Source @('install', '-e', '--id', 'Python.Python.3.12', '--scope', 'user',
                                         '--accept-package-agreements', '--accept-source-agreements')
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $script:Py = Find-Python
    if (-not $script:Py) {
        throw "Python is still not found after winget (exit code $code). Open a new PowerShell window and run this script again."
    }
}
# Child scripts (Start-Notify.ps1, Install-Service.ps1) look python.exe up on PATH: make it this one, not the Store stub.
$env:Path = (Split-Path -Parent $script:Py) + ';' + $env:Path
Info "python: $script:Py"

# ------------------------------------------------------------------ 2. config.json
Step 'Configuration (config.json)'
$configPath = Join-Path $Root 'config.json'
$freshConfig = $false
if (Test-Path -LiteralPath $configPath) {
    Info 'config.json exists: kept as it is.'
} else {
    $code = Invoke-Tool $script:Py @($NotifyPy, 'setup', 'local')
    if ($code -ne 0) { throw 'python notify.py setup local failed (see above).' }
    $freshConfig = $true
    # The study mailbox folder in the defaults belongs to the first PC; switch the bridge off when it does not exist here,
    # so the mailbox agent (study-claude) is not also registered on this PC.
    $mailbox = Get-PyOutput @('-c', "import json,pathlib; p=pathlib.Path('config.json'); c=json.loads(p.read_text(encoding='utf-8-sig')); m=c.setdefault('bus',{}).setdefault('mailbox',{}); dirs=m.get('dirs') or []; off=bool(m.get('enabled',True) and dirs and not any(pathlib.Path(d).expanduser().parent.is_dir() for d in dirs)); m.update({'enabled': False} if off else {}); off and p.write_text(json.dumps(c,indent=2)+chr(10),encoding='utf-8'); print('off' if off else 'on')")
    if ($mailbox -eq 'off') {
        Info 'Study mailbox bridge switched off (its folder does not exist on this PC). If study-Claude moves to this PC,'
        Info 'set bus.mailbox.enabled to true here and to false on the other PC (docs\RELAY.md, Limits).'
    }
    Info 'Created config.json (phone delivery: local only on this PC).'
}

# ------------------------------------------------------------------ 3. the service
if ($SkipService) {
    Step 'Local Notify as a background process (-SkipService)'
    $code = Invoke-Script 'Start-Notify.ps1' @()
    if ($code -ne 0) { throw "Start-Notify.ps1 failed; see $Root\data\stderr.log" }
} else {
    Step 'Local Notify Windows service'
    $service = Get-Service -Name LocalNotify -ErrorAction SilentlyContinue
    if ($service -and $service.Status -eq 'Running') {
        Info 'LocalNotify is installed and running.'
    } elseif ($service) {
        Info "LocalNotify is installed but $($service.Status): starting it."
        $code = Invoke-Script 'Start-Notify.ps1' @()
        if ($code -ne 0) { throw 'Could not start the LocalNotify service (see above).' }
    } else {
        # Install-Service.ps1 needs the Codex sessions folder even on a PC without Codex.
        $sessions = Get-PyOutput @('-c', 'import config; from watcher import CodexWatcher; from types import SimpleNamespace; print(CodexWatcher(SimpleNamespace(cfg=config.load())).root.resolve())')
        if (-not $sessions) { throw 'Could not read config.json (python -c "import config; config.load()" shows why).' }
        if (-not (Test-Path -LiteralPath $sessions)) {
            New-Item -ItemType Directory -Force -Path $sessions | Out-Null
            Info "Created $sessions (empty; the service only reads it)."
        }
        $resultPath = Join-Path $Root 'data\service-install-result.json'
        $before = if (Test-Path -LiteralPath $resultPath) { (Get-Item -LiteralPath $resultPath).LastWriteTimeUtc } else { [datetime]::MinValue }
        Info 'Installing the LocalNotify service: accept the Windows administrator prompt.'
        $code = Invoke-Script 'Install-Service.ps1' @('-PythonPath', $script:Py, '-CodexSessions', $sessions)
        # Without admin rights it relaunches itself elevated and returns at once: wait for its result file.
        $result = $null
        $deadline = (Get-Date).AddMinutes(5)
        while ((Get-Date) -lt $deadline) {
            if (Test-Path -LiteralPath $resultPath) {
                $item = Get-Item -LiteralPath $resultPath
                try { $current = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json } catch { $current = $null }
                if ($current -and $item.LastWriteTimeUtc -gt $before -and $current.status -in @('installed', 'failed', 'elevation-cancelled')) {
                    $result = $current
                    break
                }
            }
            if ($code -ne 0 -and -not (Test-Path -LiteralPath $resultPath)) { break }
            Start-Sleep -Seconds 2
        }
        if (-not $result) {
            throw "The service installer did not finish (exit code $code). Details: $Root\data\service-install.log. Or run this script with -SkipService."
        }
        if ($result.status -ne 'installed') {
            throw "Service install $($result.status): $($result.message) (or run this script with -SkipService)"
        }
        Info 'LocalNotify is installed and running (starts at boot).'
    }
}

# ------------------------------------------------------------------ 4. Agent Bus clients
Step 'Agent Bus clients (Claude Code plugin, Codex skill, Claude desktop MCP server)'
$code = Invoke-Script 'Install-AgentBus.ps1' @('-Python', $script:Py)
if ($code -ne 0) {
    Write-Warning 'Install-AgentBus.ps1 reported a problem (see above; details in data\agentbus-install-result.json). Continuing.'
}

# ------------------------------------------------------------------ 5. pair + status
if ($NoPair) {
    Step 'Pairing skipped (-NoPair)'
    Info 'Pair later with: python notify.py relay pair'
} else {
    Step 'Pair this PC with the planner'
    $paired = Get-PyOutput @('-c', 'import config; print(int(config.relay_settings(config.load(validate=False))[3]))')
    if ($paired -eq '1') {
        Info 'Already paired (relay.url and relay.token are set). To pair again: python notify.py relay pair'
    } else {
        $pairArgs = @($NotifyPy, 'relay', 'pair', '--url', $Server)
        if ($Machine) { $pairArgs += @('--machine', $Machine) }
        $code = Invoke-Tool $script:Py $pairArgs
        if ($code -ne 0) {
            Write-Warning 'Pairing did not finish (see above). Run it again any time: python notify.py relay pair'
        } elseif (-not (Get-Service -Name LocalNotify -ErrorAction SilentlyContinue)) {
            # A background process (no Windows service) only reads config.json at start.
            Info 'Restarting the background Local Notify process so it loads the pairing...'
            $code = Invoke-Script 'Start-Notify.ps1' @('-Restart')
            if ($code -ne 0) { Write-Warning "Restart failed; run .\Start-Notify.ps1 -Restart (see $Root\data\stderr.log)" }
            Start-Sleep -Seconds 3
        }
    }
}

Step 'Relay status'
$code = Invoke-Tool $script:Py @($NotifyPy, 'relay', 'status')

Write-Output ''
Write-Output 'Done. `python bus.py agents` lists the agents on this PC and on your other PCs (as machine:account/folder).'
if ($freshConfig) {
    Write-Output 'Phone pings from agents on this PC stay local until you copy the "provider", "ntfy" and "gateway" sections'
    Write-Output 'from the first PC''s config.json into this one (never the "relay" section: each PC has its own bus token).'
    Write-Output 'The Windows service reloads config.json by itself; a background process needs .\Start-Notify.ps1 -Restart.'
}
