<#
.SYNOPSIS
  Installs (or removes) the AgentBusOps helper: the privileged half of Agent Bus remote ops (docs/REMOTE-OPS.md).

.DESCRIPTION
  Run elevated on the PC that should accept remote ops (SETUP-HEADLESS calls it). Idempotent; -WhatIf changes nothing.

    powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-RemoteOps.ps1 [-SigningKeyFile C:\path\key.pub]

  1. Copies remote_ops.py, remote_ops_helper.py and remote_ops_update.ps1 into C:\Program Files\AgentBusOps
     (admin-only), so pulling the repo never changes privileged code; writes ops.json (repo + git paths).
  2. C:\ProgramData\AgentBusOps: SYSTEM/Administrators full, Users read; update\ also writable by you (the task below).
  3. -SigningKeyFile: writes allowed_signers (principal *, namespace git) so `self-update` accepts refs signed by that
     key. Without it self-update stays off. Only the key's fingerprint is printed.
  4. Service AgentBusOps (LocalSystem, Automatic, restart on failure): python -I -B remote_ops_helper.py. It serves
     \\.\pipe\agent-bus-remote-ops, which only SYSTEM and NT SERVICE\LocalNotify may open.
  5. On-demand task "AgentBusOps Self-Update", as you (interactive, not elevated): the git half of self-update.

.PARAMETER SigningKeyFile
  An SSH public key file (.pub) that signs ops-* tags on MSI. One line; never printed.

.PARAMETER PythonPath
  python.exe for the service. Default: the one LocalNotify runs, else python.exe on PATH.

.PARAMETER Remove
  Stops and deletes the service and the task and removes C:\Program Files\AgentBusOps (logs are kept).
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$SigningKeyFile = '',
  [string]$PythonPath = '',
  [switch]$Remove
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
# Import-Module has no -WhatIf; its alias setup still prints "What if" noise under -WhatIf, so mute the preference.
$savedWhatIf = $WhatIfPreference
$WhatIfPreference = $false
Import-Module CimCmdlets -ErrorAction SilentlyContinue
$WhatIfPreference = $savedWhatIf

$ServiceName = 'AgentBusOps'
$TaskName = 'AgentBusOps Self-Update'
$PipeName = 'agent-bus-remote-ops'
$InstallDir = Join-Path $env:ProgramFiles 'AgentBusOps'
$DataDir = Join-Path $env:ProgramData 'AgentBusOps'
$UpdateDir = Join-Path $DataDir 'update'
$Repo = $PSScriptRoot
$Files = @('remote_ops.py', 'remote_ops_helper.py', 'remote_ops_update.ps1')

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "    !!  $Text" -ForegroundColor Yellow }
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
  $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
  if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed ($LASTEXITCODE): $($out -join ' ')" }
  return $out
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $WhatIfPreference) { throw 'Run Install-RemoteOps.ps1 elevated (admin PowerShell).' }

# The interactive user the self-update task runs as (the elevated token is the same account when he ran it).
$UpdateUser = $null
try { $UpdateUser = (Get-CimInstance Win32_ComputerSystem).UserName } catch { $UpdateUser = $null }
if (-not $UpdateUser) { $UpdateUser = "$env:USERDOMAIN\$env:USERNAME" }

if ($Remove) {
  Write-Step "Removing $ServiceName"
  $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if ($svc -and $PSCmdlet.ShouldProcess($ServiceName, 'stop and delete service')) {
    if ($svc.Status -ne 'Stopped') { Stop-Service -Name $ServiceName -Force; $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)) }
    Invoke-Native 'sc.exe' @('delete', $ServiceName) | Out-Null
  }
  if ((Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) -and $PSCmdlet.ShouldProcess($TaskName, 'unregister task')) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  }
  if ((Test-Path -LiteralPath $InstallDir) -and $PSCmdlet.ShouldProcess($InstallDir, 'remove folder')) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
  }
  Write-Ok "removed (logs kept in $DataDir)"
  exit 0
}

# ---------------------------------------------------------------------------------------------- python + git
Write-Step 'Finding Python and git'
if (-not $PythonPath) {
  $ln = Get-CimInstance Win32_Service -Filter "Name = 'LocalNotify'" -ErrorAction SilentlyContinue
  if ($ln -and $ln.PathName -match '^"([^"]+python[^"]*\.exe)"') { $PythonPath = $Matches[1] }
}
if (-not $PythonPath) { $cmd = Get-Command python.exe -ErrorAction SilentlyContinue; if ($cmd) { $PythonPath = $cmd.Source } }
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'python.exe not found; pass -PythonPath.' }
if ($PythonPath -like '*\WindowsApps\*') { throw "That python.exe is the Microsoft Store alias ($PythonPath); pass -PythonPath to a real install." }
$git = Get-Command git.exe -ErrorAction SilentlyContinue
$GitPath = if ($git) { $git.Source } else { Join-Path $env:ProgramFiles 'Git\cmd\git.exe' }
if (-not (Test-Path -LiteralPath $GitPath -PathType Leaf)) { Write-Warn 'git.exe not found: self-update will fail until Git is installed' }
Write-Ok "python: $PythonPath"
Write-Ok "git:    $GitPath"
foreach ($f in $Files) { if (-not (Test-Path -LiteralPath (Join-Path $Repo $f) -PathType Leaf)) { throw "missing $f next to this script" } }

# ---------------------------------------------------------------------------------------------- program files copy
Write-Step "Copying the helper into $InstallDir (admin-only)"
if ($PSCmdlet.ShouldProcess($InstallDir, 'copy helper files and write ops.json')) {
  New-Item -ItemType Directory -Force -Path $InstallDir, (Join-Path $InstallDir 'empty-hooks') | Out-Null
  # Admin-only: SYSTEM + Administrators full, Users read/execute, nothing inherited from elsewhere.
  Invoke-Native 'icacls.exe' @($InstallDir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F',
    '*S-1-5-32-545:(OI)(CI)RX', '/Q') | Out-Null
  foreach ($f in $Files) { Copy-Item -LiteralPath (Join-Path $Repo $f) -Destination (Join-Path $InstallDir $f) -Force }
  $ops = @{ repo = $Repo; git = $GitPath } | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllText((Join-Path $InstallDir 'ops.json'), $ops, (New-Object System.Text.UTF8Encoding($false)))
  Write-Ok 'copied'
}

# ---------------------------------------------------------------------------------------------- data dir
Write-Step "Data folder $DataDir"
if ($PSCmdlet.ShouldProcess($DataDir, 'create and set ACLs')) {
  New-Item -ItemType Directory -Force -Path $DataDir, $UpdateDir | Out-Null
  Invoke-Native 'icacls.exe' @($DataDir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F',
    '*S-1-5-32-545:(OI)(CI)RX', '/Q') | Out-Null
  Invoke-Native 'icacls.exe' @($UpdateDir, '/grant', "${UpdateUser}:(OI)(CI)M", '/Q') | Out-Null
  Write-Ok "update\ writable by $UpdateUser (the self-update task)"
}

# ---------------------------------------------------------------------------------------------- allowed signers
Write-Step 'Self-update signing key'
$signers = Join-Path $DataDir 'allowed_signers'
if ($SigningKeyFile) {
  if (-not (Test-Path -LiteralPath $SigningKeyFile -PathType Leaf)) { throw "SigningKeyFile not found: $SigningKeyFile" }
  $line = @(Get-Content -LiteralPath $SigningKeyFile | Where-Object { $_.Trim() }) | Select-Object -First 1
  $m = [regex]::Match([string]$line, '^\s*((?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com)\s+[A-Za-z0-9+/=]{40,})')
  if (-not $m.Success) { throw 'SigningKeyFile is not an SSH public key line (ssh-ed25519 AAAA...).' }
  if ($PSCmdlet.ShouldProcess($signers, 'write allowed_signers')) {
    $entry = '* namespaces="git" ' + ($m.Groups[1].Value -replace '\s+', ' ')
    [System.IO.File]::WriteAllText($signers, $entry + "`n", (New-Object System.Text.ASCIIEncoding))
    $kg = Get-Command ssh-keygen.exe -ErrorAction SilentlyContinue
    $fp = if ($kg) { (& $kg.Source -lf $SigningKeyFile 2>$null) -join ' ' } else { '(ssh-keygen not found)' }
    Write-Ok "self-update accepts refs signed by: $fp"
  }
} elseif (Test-Path -LiteralPath $signers) {
  Write-Ok 'allowed_signers already present (self-update on)'
} else {
  Write-Warn 'no -SigningKeyFile: self-update stays OFF (status, service restarts, tailscale-up and reboot still work)'
}

# ---------------------------------------------------------------------------------------------- service
Write-Step "Service $ServiceName (LocalSystem)"
$binary = '"' + $PythonPath + '" -I -B "' + (Join-Path $InstallDir 'remote_ops_helper.py') + '"'
$existing = Get-CimInstance Win32_Service -Filter "Name = '$ServiceName'" -ErrorAction SilentlyContinue
if ($PSCmdlet.ShouldProcess($ServiceName, "create/update service: $binary")) {
  if (-not $existing) {
    New-Service -Name $ServiceName -BinaryPathName $binary -DisplayName 'Agent Bus Remote Ops' -StartupType Automatic `
      -Description 'Runs the fixed remote-ops allowlist for the Agent Bus (LocalNotify) over a local pipe. docs/REMOTE-OPS.md' | Out-Null
  } else {
    if ($existing.State -ne 'Stopped') { Stop-Service -Name $ServiceName -Force; (Get-Service $ServiceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)) }
    Invoke-Native 'sc.exe' @('config', $ServiceName, 'binPath=', $binary, 'start=', 'auto', 'obj=', 'LocalSystem') | Out-Null
  }
  Invoke-Native 'sc.exe' @('failure', $ServiceName, 'reset=', '86400', 'actions=', 'restart/5000/restart/30000/restart/60000') | Out-Null
  Invoke-Native 'sc.exe' @('failureflag', $ServiceName, '1') | Out-Null
  Start-Service -Name $ServiceName
  (Get-Service $ServiceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
  Start-Sleep -Seconds 2
  $pipes = [System.IO.Directory]::GetFiles('\\.\pipe\') | ForEach-Object { Split-Path $_ -Leaf }
  if ($pipes -contains $PipeName) { Write-Ok "running; \\.\pipe\$PipeName is up (only SYSTEM and NT SERVICE\LocalNotify may open it)" }
  else { Write-Warn "running, but the pipe is not up yet; see $DataDir\helper.log" }
}

# ---------------------------------------------------------------------------------------------- self-update task
Write-Step "Task '$TaskName' (as $UpdateUser, not elevated, on demand)"
if ($PSCmdlet.ShouldProcess($TaskName, 'register on-demand task')) {
  $script = Join-Path $InstallDir 'remote_ops_update.ps1'
  $action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`""
  $principal = New-ScheduledTaskPrincipal -UserId $UpdateUser -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
  Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings -Force `
    -Description 'Agent Bus remote ops: fast-forward the notifications repo to a signed ref (docs/REMOTE-OPS.md).' | Out-Null
  Write-Ok 'registered'
}

Write-Host ''
Write-Host "Remote ops ready on $env:COMPUTERNAME. From MSI:  python bus.py remote $env:COMPUTERNAME status" -ForegroundColor Green
exit 0
