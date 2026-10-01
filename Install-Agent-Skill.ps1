param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'skills\notify-user\SKILL.md'
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$roots = @((Join-Path $codexRoot 'skills\notify-user'), (Join-Path $env:USERPROFILE '.claude\skills\notify-user'))
foreach ($root in $roots) {
    $target = Join-Path $root 'SKILL.md'
    if ($Remove) {
        if (Test-Path -LiteralPath $target) {
            if ((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $source).Hash) {
                throw "Installed skill has been edited; remove it manually if desired: $target"
            }
            Remove-Item -LiteralPath $target
        }
        continue
    }
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $source).Hash) {
            $backup = Join-Path $PSScriptRoot ('data\backups\notify-user-' + [guid]::NewGuid().ToString('N') + '.md')
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup
        }
    }
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Force
    Write-Output "Installed skill: $target"
}
