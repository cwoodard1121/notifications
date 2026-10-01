param([switch]$Standalone)
$ErrorActionPreference = 'Stop'
$windowsService = Get-Service -Name LocalNotify -ErrorAction SilentlyContinue
if (-not $Standalone -and $null -ne $windowsService) {
    if ($windowsService.Status -ne 'Stopped') {
        Stop-Service -Name LocalNotify
        $windowsService.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    Write-Output 'Local Notify Windows service stopped.'
    exit 0
}
$pidPath = Join-Path $PSScriptRoot 'data\service.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Output 'No managed service PID found.'; exit 0 }
$servicePid = [int](Get-Content -LiteralPath $pidPath -Raw)
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid"
if ($null -ne $process) {
    $scriptPath = Join-Path $PSScriptRoot 'notify.py'
    if ($process.Name -notmatch '^python(w)?\.exe$' -or $process.CommandLine -notlike "*$scriptPath*serve*") {
        throw 'PID belongs to a different process; refusing to stop it.'
    }
    Stop-Process -Id $servicePid
}
Remove-Item -LiteralPath $pidPath
Write-Output 'Local Notify stopped.'
