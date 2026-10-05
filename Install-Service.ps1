param(
    [switch]$Remove,
    [string]$PythonPath,
    [string]$CodexSessions,
    [string]$OwnerSid,
    [string]$StartupShortcut
)
$ErrorActionPreference = 'Stop'
$serviceName = 'LocalNotify'
$serviceAccount = 'NT SERVICE\LocalNotify'
$dataPath = Join-Path $PSScriptRoot 'data'
$resultPath = Join-Path $dataPath 'service-install-result.json'
$metadataPath = Join-Path $dataPath 'windows-service.json'
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
if (-not $PythonPath) { $PythonPath = (Get-Command python.exe).Source }
if (-not $CodexSessions) {
    $resolveCode = 'import config; from watcher import CodexWatcher; from types import SimpleNamespace; print(CodexWatcher(SimpleNamespace(cfg=config.load())).root.resolve())'
    Push-Location -LiteralPath $PSScriptRoot
    try { $CodexSessions = & $PythonPath -c $resolveCode } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the Codex sessions directory.' }
}
if (-not $OwnerSid) { $OwnerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
if (-not $StartupShortcut) { $StartupShortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Local Notify.lnk' }

function Write-Result([string]$Status, [string]$Message) {
    @{ status = $Status; message = $Message; time = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Result 'awaiting-elevation' 'Accept the Windows administrator prompt to install or remove the service.'
    $arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $PSCommandPath + '" -PythonPath "' + $PythonPath + '" -CodexSessions "' + $CodexSessions + '" -OwnerSid "' + $OwnerSid + '" -StartupShortcut "' + $StartupShortcut + '"'
    if ($Remove) { $arguments += ' -Remove' }
    try {
        $process = Start-Process -FilePath (Get-Command powershell.exe).Source -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments -PassThru
        Write-Output "Windows elevation requested. Installer PID: $($process.Id). Result: $resultPath"
    } catch {
        Write-Result 'elevation-cancelled' $_.Exception.Message
        throw
    }
    exit 0
}

function Invoke-Sc([string[]]$Arguments) {
    $output = & sc.exe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "sc.exe failed: $output" }
    Write-Output $output
}

function Grant-Directory([string]$Path, [string]$Rights) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "Directory is missing: $Path" }
    $output = & icacls.exe $Path /grant "${serviceAccount}:(OI)(CI)$Rights" /Q 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Could not grant service access to $Path : $output" }
}

$created = $false
$transcriptStarted = $false
try {
    Start-Transcript -LiteralPath (Join-Path $dataPath 'service-install.log') -Force | Out-Null
    $transcriptStarted = $true
    $existing = Get-CimInstance Win32_Service -Filter "Name = '$serviceName'"
    $entrypoint = Join-Path $PSScriptRoot 'windows_service.py'
    if ($existing -and $existing.PathName -notlike "*$entrypoint*") { throw 'A different service already uses the name LocalNotify.' }
    if ($Remove) {
        if ($existing) {
            if ($existing.State -ne 'Stopped') {
                Stop-Service -Name $serviceName
                (Get-Service $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
            }
            # Revoke only the ACEs for this dedicated service identity.
            $paths = @($PSScriptRoot, (Split-Path -Parent $PythonPath), $CodexSessions, $dataPath)
            if (Test-Path -LiteralPath $metadataPath) { $paths = (Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json).acl_paths }
            foreach ($path in $paths) {
                if (Test-Path -LiteralPath $path) {
                    $output = & icacls.exe $path /remove:g $serviceAccount /Q 2>&1
                    if ($LASTEXITCODE -ne 0) { throw "Could not remove service access from $path : $output" }
                }
            }
            Invoke-Sc -Arguments @('delete', $serviceName)
        }
        if (Test-Path -LiteralPath $metadataPath) { Remove-Item -LiteralPath $metadataPath }
        Write-Result 'removed' 'Windows service removed. Source files, settings and history are retained.'
        exit 0
    }

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'Python executable is missing.' }
    if (-not (Test-Path -LiteralPath $CodexSessions -PathType Container)) { throw 'Codex sessions directory is missing.' }
    if ($existing -and $existing.State -ne 'Stopped') {
        Stop-Service -Name $serviceName
        (Get-Service $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    $binaryPath = '"' + $PythonPath + '" -B "' + $entrypoint + '" --sessions-dir "' + $CodexSessions + '"'
    if (-not $existing) {
        # Keep disabled from automatic launch until the limited account is configured.
        New-Service -Name $serviceName -BinaryPathName $binaryPath -DisplayName 'Local Notify' -StartupType Manual -Description 'Local agent notifications, Codex usage alerts and daily recaps.' | Out-Null
        $created = $true
    } else {
        if ($existing.PathName -ne $binaryPath) { throw 'Service executable paths differ. Remove the existing service before reinstalling.' }
    }
    Invoke-Sc -Arguments @('config', $serviceName, 'obj=', $serviceAccount)
    Invoke-Sc -Arguments @('sidtype', $serviceName, 'unrestricted')
    $pythonRoot = Split-Path -Parent $PythonPath
    Grant-Directory $pythonRoot 'RX'
    Grant-Directory $PSScriptRoot 'RX'
    Grant-Directory $CodexSessions 'RX'
    Grant-Directory $dataPath 'M'
    # Agent Console: read-only access to Claude Code transcripts (~\.claude*\projects); see Grant-ConsoleAccess.ps1.
    $userHome = Split-Path -Parent (Split-Path -Parent $CodexSessions)
    $claudeProjects = @(Get-ChildItem -LiteralPath $userHome -Directory -Force -Filter '.claude*' |
        Where-Object { $_.Name -match '^\.claude(-[A-Za-z0-9_]+)?$' } |
        ForEach-Object { Join-Path $_.FullName 'projects' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Container })
    foreach ($path in $claudeProjects) { Grant-Directory $path 'RX' }

    # Let the installing user query/start/stop this service without repeated UAC prompts.
    # Preserve the existing service DACL; grant neither reconfiguration nor deletion.
    $sddl = (& sc.exe sdshow $serviceName | Where-Object { $_ -match '^D:' }) -join ''
    if (-not $sddl) { throw 'Could not read service permissions.' }
    $descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new($sddl)
    $ownerIdentity = [System.Security.Principal.SecurityIdentifier]::new($OwnerSid)
    $hasAce = $false
    foreach ($ace in $descriptor.DiscretionaryAcl) {
        if ($ace -is [System.Security.AccessControl.CommonAce] -and $ace.SecurityIdentifier -eq $ownerIdentity -and ($ace.AccessMask -band 0x3d) -eq 0x3d) { $hasAce = $true }
    }
    if (-not $hasAce) {
        $ace = [System.Security.AccessControl.CommonAce]::new([System.Security.AccessControl.AceFlags]::None, [System.Security.AccessControl.AceQualifier]::AccessAllowed, 0x3d, $ownerIdentity, $false, $null)
        $descriptor.DiscretionaryAcl.InsertAce($descriptor.DiscretionaryAcl.Count, $ace)
        Invoke-Sc -Arguments @('sdset', $serviceName, $descriptor.GetSddlForm([System.Security.AccessControl.AccessControlSections]::All))
    }
    Invoke-Sc -Arguments @('failure', $serviceName, 'reset=', '86400', 'actions=', 'restart/5000/restart/30000/restart/60000')
    Invoke-Sc -Arguments @('failureflag', $serviceName, '1')
    Set-Service -Name $serviceName -StartupType Automatic
    & (Join-Path $PSScriptRoot 'Stop-Notify.ps1') -Standalone
    Start-Service -Name $serviceName
    (Get-Service $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
    $health = $null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $raw = & $PythonPath (Join-Path $PSScriptRoot 'notify.py') status 2>$null
            if ($LASTEXITCODE -eq 0) {
                $health = $raw | ConvertFrom-Json
                if ($health.runtime -eq 'windows-service') { break }
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $health -or $health.runtime -ne 'windows-service') { throw 'Service was created but its HTTP health check failed.' }
    # Only remove the old login shortcut after the boot service passes its health check.
    if (Test-Path -LiteralPath $StartupShortcut) { Remove-Item -LiteralPath $StartupShortcut }
    @{ service = $serviceName; account = $serviceAccount; python = $PythonPath; sessions = $CodexSessions; owner_sid = $OwnerSid; acl_paths = @($PSScriptRoot, $pythonRoot, $CodexSessions, $dataPath) + $claudeProjects } | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
    Write-Result 'installed' 'LocalNotify is running as an Automatic Windows service under its own virtual account.'
} catch {
    $failureMessage = $_.Exception.Message
    if ($created) {
        Stop-Service -Name $serviceName -ErrorAction SilentlyContinue
        foreach ($path in @($PSScriptRoot, (Split-Path -Parent $PythonPath), $CodexSessions, $dataPath) + @($claudeProjects | Where-Object { $_ })) {
            if (Test-Path -LiteralPath $path) { & icacls.exe $path /remove:g $serviceAccount /Q | Out-Null }
        }
        & sc.exe delete $serviceName | Out-Null
    }
    Write-Result 'failed' $failureMessage
    Write-Error $failureMessage
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
