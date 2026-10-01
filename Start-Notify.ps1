param([switch]$Restart)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = (Get-Command python.exe).Source
$windowsService = Get-Service -Name LocalNotify -ErrorAction SilentlyContinue
if ($null -ne $windowsService) {
    if ($Restart -and $windowsService.Status -ne 'Stopped') {
        Stop-Service -Name LocalNotify
        $windowsService.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    Start-Service -Name LocalNotify
    $windowsService.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
    & $pythonPath "$PSScriptRoot\notify.py" status
    exit $LASTEXITCODE
}
if ($Restart) { & "$PSScriptRoot\Stop-Notify.ps1" }
function Get-NotifyStatus {
    try {
        $response = & $pythonPath "$PSScriptRoot\notify.py" status 2>$null
        if ($LASTEXITCODE -eq 0) { return $response }
    } catch { }
    return $null
}
$statusJson = Get-NotifyStatus
if ($null -ne $statusJson) {
    Write-Output 'Local Notify is already running.'
    exit 0
}
$dataPath = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-u', ('"' + (Join-Path $PSScriptRoot 'notify.py') + '"'), 'serve') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataPath 'stdout.log') -RedirectStandardError (Join-Path $dataPath 'stderr.log') -PassThru
Set-Content -LiteralPath (Join-Path $dataPath 'service.pid') -Value $process.Id
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 200
    if ($process.HasExited) { throw "Service exited. See $dataPath\stderr.log" }
    $statusJson = Get-NotifyStatus
    if ($null -ne $statusJson) { Write-Output $statusJson; exit 0 }
}
throw "Service did not become ready. See $dataPath\stderr.log"
