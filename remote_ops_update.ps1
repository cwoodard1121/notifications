# Remote ops self-update (docs/REMOTE-OPS.md). Install-RemoteOps.ps1 copies this into C:\Program Files\AgentBusOps
# (admin-only) and registers the on-demand task "AgentBusOps Self-Update" that runs it as Cameron (his normal,
# non-elevated token, so git uses his GitHub credentials; the repo is private and SYSTEM has none).
#
# Input:  C:\ProgramData\AgentBusOps\update\request.json  {"id": "<16 hex>", "ref": "ops-YYYYMMDD-N" | "<40 hex>"}
#         written by the AgentBusOps helper after it validated the request.
# Output: C:\ProgramData\AgentBusOps\update\result.json   {"id", "ok", "changed", "before", "after", "error"}
#
# Fast-forwards the notifications checkout to the ref ONLY when the ref is signed by a key in
# C:\ProgramData\AgentBusOps\allowed_signers (admin-only), the branch is main, tracked files are clean, and the move
# is a fast-forward. Repository hooks and fsmonitor are disabled for every git call. Never prints secrets.
$ErrorActionPreference = 'Continue'
Set-StrictMode -Version 2.0

$DataDir = Join-Path $env:ProgramData 'AgentBusOps'
$UpdateDir = Join-Path $DataDir 'update'
$Signers = Join-Path $DataDir 'allowed_signers'
$ResultPath = Join-Path $UpdateDir 'result.json'
$state = @{ id = $null; ok = $false; changed = $false; before = $null; after = $null; error = $null }

function Save-Result {
  $tmp = $ResultPath + '.tmp'
  [System.IO.File]::WriteAllText($tmp, ($state | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
  Move-Item -LiteralPath $tmp -Destination $ResultPath -Force
}

function Invoke-Git([string[]]$GitArgs) {
  $base = @('-C', $script:Repo, '-c', "core.hooksPath=$script:Hooks", '-c', 'core.fsmonitor=false',
            '-c', 'gpg.format=ssh', '-c', "gpg.ssh.allowedSignersFile=$Signers")
  $out = & $script:Git @($base + $GitArgs) 2>&1 | ForEach-Object { "$_" }
  return @{ code = $LASTEXITCODE; out = (($out -join "`n").Trim()) }
}

try {
  $req = Get-Content -LiteralPath (Join-Path $UpdateDir 'request.json') -Raw -ErrorAction Stop | ConvertFrom-Json
  if ($req.id -notmatch '^[0-9a-f]{16}$') { throw 'bad request id' }
  $state.id = [string]$req.id
  $ref = [string]$req.ref
  if ($ref -cnotmatch '^(ops-\d{8}-\d{1,3}|[0-9a-f]{40})$') { throw 'ref must be a tag ops-YYYYMMDD-N or a 40-hex sha' }
  if (-not (Test-Path -LiteralPath $Signers -PathType Leaf)) { throw 'no allowed_signers file: self-update is off' }

  $cfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'ops.json') -Raw -ErrorAction Stop | ConvertFrom-Json
  $script:Repo = [string]$cfg.repo
  $script:Git = [string]$cfg.git
  $script:Hooks = Join-Path $PSScriptRoot 'empty-hooks'
  if (-not (Test-Path -LiteralPath (Join-Path $script:Repo '.git'))) { throw "not a git checkout: $script:Repo" }

  $branch = Invoke-Git @('symbolic-ref', '--short', 'HEAD')
  if ($branch.code -ne 0 -or $branch.out -ne 'main') { throw "the checkout is not on main ($($branch.out))" }
  $dirty = Invoke-Git @('status', '--porcelain', '--untracked-files=no')
  if ($dirty.code -ne 0 -or $dirty.out) { throw 'tracked files have local changes; not updating' }

  $fetch = Invoke-Git @('fetch', '--no-recurse-submodules', '--tags', 'origin')
  if ($fetch.code -ne 0) { throw "git fetch failed: $($fetch.out)" }

  if ($ref -like 'ops-*') {
    $verify = Invoke-Git @('verify-tag', "refs/tags/$ref")
    $target = Invoke-Git @('rev-parse', '--verify', "refs/tags/$ref^{commit}")
  } else {
    $verify = Invoke-Git @('verify-commit', $ref)
    $target = Invoke-Git @('rev-parse', '--verify', "$ref^{commit}")
  }
  if ($target.code -ne 0) { throw "unknown ref $ref (pushed to origin?)" }
  if ($verify.code -ne 0) {
    $why = (($verify.out -split "`n") | Where-Object { $_.Trim() }) -join ' / '
    if ($why.Length -gt 300) { $why = $why.Substring(0, 300) }
    throw "signature check failed for ${ref} (not signed by a key in allowed_signers): $why"
  }

  $before = Invoke-Git @('rev-parse', 'HEAD')
  $state.before = $before.out
  if ($before.out -eq $target.out) {
    $state.ok = $true; $state.after = $before.out
  } else {
    $ancestor = Invoke-Git @('merge-base', '--is-ancestor', 'HEAD', $target.out)
    if ($ancestor.code -ne 0) { throw "$ref is not a fast-forward of the current main" }
    $merge = Invoke-Git @('merge', '--ff-only', '--no-edit', $target.out)
    if ($merge.code -ne 0) { throw "git merge --ff-only failed: $($merge.out)" }
    $state.after = (Invoke-Git @('rev-parse', 'HEAD')).out
    $state.ok = $true; $state.changed = $true
  }
} catch {
  $state.error = [string]$_.Exception.Message
}
Save-Result
