param(
    [switch]$Remove,
    [string]$UserHome
)
# Lets the LocalNotify service (NT SERVICE\LocalNotify) READ Claude Code session transcripts, so the Agent Console
# (http://127.0.0.1:8765/console) can show every session: read-only (RX) on each ~\.claude*\projects folder.
# The paths are added to data\windows-service.json acl_paths, so Install-Service.ps1 -Remove revokes them too.
# Re-run after adding a new Claude config dir (~\.claude-<name>). -Remove takes the access away again.
$ErrorActionPreference = 'Stop'
$serviceAccount = 'NT SERVICE\LocalNotify'
$dataPath = Join-Path $PSScriptRoot 'data'
$metadataPath = Join-Path $dataPath 'windows-service.json'
$resultPath = Join-Path $dataPath 'console-access-result.json'
if (-not $UserHome) { $UserHome = $HOME }
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null

function Write-Result([string]$Status, [string]$Message, $Paths) {
    @{ status = $Status; message = $Message; paths = @($Paths); time = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Result 'awaiting-elevation' 'Accept the Windows administrator prompt.' @()
    $arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $PSCommandPath + '" -UserHome "' + $UserHome + '"'
    if ($Remove) { $arguments += ' -Remove' }
    $process = Start-Process -FilePath (Get-Command powershell.exe).Source -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments -PassThru
    $process.WaitForExit()
    $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
    Write-Output "$($result.status): $($result.message)"
    foreach ($path in $result.paths) { Write-Output "  $path" }
    if ($result.status -ne 'granted' -and $result.status -ne 'removed') { exit 1 }
    exit 0
}

try {
    if (-not (Get-Service -Name LocalNotify -ErrorAction SilentlyContinue)) {
        throw 'The LocalNotify Windows service is not installed (a standalone service already runs as you and needs no grant).'
    }
    $paths = @(Get-ChildItem -LiteralPath $UserHome -Directory -Force -Filter '.claude*' |
        Where-Object { $_.Name -match '^\.claude(-[A-Za-z0-9_]+)?$' } |
        ForEach-Object { Join-Path $_.FullName 'projects' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Container })
    $metadata = $null
    if (Test-Path -LiteralPath $metadataPath) { $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json }
    $recorded = @()
    if ($metadata -and $metadata.acl_paths) { $recorded = @($metadata.acl_paths) }
    foreach ($path in $paths) {
        if ($Remove) {
            $output = & icacls.exe $path /remove:g $serviceAccount /Q 2>&1
        } else {
            $output = & icacls.exe $path /grant "${serviceAccount}:(OI)(CI)RX" /Q 2>&1
        }
        if ($LASTEXITCODE -ne 0) { throw "icacls failed on $path : $output" }
    }
    if ($metadata) {
        $kept = if ($Remove) { @($recorded | Where-Object { $paths -notcontains $_ }) } else { @($recorded + $paths | Select-Object -Unique) }
        $metadata.acl_paths = $kept
        $metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
    }
    if ($Remove) {
        Write-Result 'removed' 'The service can no longer read Claude Code transcripts.' $paths
    } else {
        Write-Result 'granted' 'The service can now read Claude Code transcripts (read-only) for the Agent Console.' $paths
    }
} catch {
    Write-Result 'failed' $_.Exception.Message @()
    Write-Error $_.Exception.Message
    exit 1
}
