<#
.SYNOPSIS
  Renders a wallpaper that says "Run SETUP-HEADLESS on the Desktop" and, on DESKTOP-5BEGSB5, sets it as the wallpaper.

.DESCRIPTION
  Run it anywhere to render the image (System.Drawing, no downloads):
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Set-ReminderWallpaper.ps1
  On MSI (or any other PC) it only renders the PNG and tells you where it is: a wallpaper can only change on the PC
  itself, so it takes effect once you are signed in at DESKTOP-5BEGSB5 (or pass -Apply to set it here anyway).
  On DESKTOP-5BEGSB5 it also remembers the current wallpaper and sets the reminder. Setup-HeadlessHost.ps1 puts the
  old wallpaper back at the end (or run this with -Restore).

.PARAMETER OutFile
  Where to write the PNG. Default: %LOCALAPPDATA%\HeadlessHost\reminder-wallpaper.png

.PARAMETER Apply
  Set it as this PC's wallpaper even when this is not DESKTOP-5BEGSB5.

.PARAMETER Restore
  Put back the wallpaper that was there before the reminder.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$OutFile = '',
  [string]$TargetComputer = 'DESKTOP-5BEGSB5',
  [switch]$Apply,
  [switch]$Restore
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$dir = Join-Path $env:LOCALAPPDATA 'HeadlessHost'
$prevFile = Join-Path $dir 'wallpaper-previous.txt'
if (-not $OutFile) { $OutFile = Join-Path $dir 'reminder-wallpaper.png' }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HeadlessWallpaper {
  [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  public static extern bool SystemParametersInfo(uint action, uint param, string value, uint winIni);
  public static bool Set(string path) { return SystemParametersInfo(0x0014, 0, path, 0x01 | 0x02); } // SPI_SETDESKWALLPAPER, update ini + broadcast
}
'@

function Set-Wallpaper([string]$Path) {
  if ($PSCmdlet.ShouldProcess("wallpaper of $env:USERNAME on $env:COMPUTERNAME", "set to $Path")) {
    if (-not [HeadlessWallpaper]::Set($Path)) { throw "SystemParametersInfo failed ($([Runtime.InteropServices.Marshal]::GetLastWin32Error()))" }
    Write-Host "Wallpaper set: $Path" -ForegroundColor Green
  }
}

if ($Restore) {
  if (-not (Test-Path -LiteralPath $prevFile)) { Write-Host 'Nothing to restore (no saved wallpaper).'; exit 0 }
  $prev = (Get-Content -LiteralPath $prevFile -Raw).Trim()
  if ($prev -and (Test-Path -LiteralPath $prev)) { Set-Wallpaper $prev } else { Write-Host "The old wallpaper file is gone ($prev); leaving the current one." }
  if ($PSCmdlet.ShouldProcess($prevFile, 'remove')) { Remove-Item -LiteralPath $prevFile -Force }
  exit 0
}

# ---- render
Add-Type -AssemblyName System.Drawing
$w = 1920; $h = 1080
try {
  Add-Type -AssemblyName System.Windows.Forms
  $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  if ($b.Width -ge 800 -and $b.Height -ge 600) { $w = $b.Width; $h = $b.Height }
} catch { }

$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
try {
  $g.SmoothingMode = 'AntiAlias'
  $g.TextRenderingHint = 'AntiAliasGridFit'
  $rect = New-Object System.Drawing.Rectangle(0, 0, $w, $h)
  $bg = New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, [System.Drawing.Color]::FromArgb(18, 22, 30), [System.Drawing.Color]::FromArgb(34, 18, 18), 90.0)
  $g.FillRectangle($bg, $rect)
  $accent = [System.Drawing.Color]::FromArgb(255, 176, 64)
  $g.FillRectangle((New-Object System.Drawing.SolidBrush($accent)), [int]($w * 0.08), [int]($h * 0.30), [int]($w * 0.006) + 4, [int]($h * 0.36))

  $x = [single]($w * 0.08 + $w * 0.006 + 40)
  $s = $h / 1080.0
  $fBig = New-Object System.Drawing.Font('Segoe UI Semibold', [single](78 * $s), [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
  $fMid = New-Object System.Drawing.Font('Segoe UI', [single](34 * $s), [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
  $fSmall = New-Object System.Drawing.Font('Consolas', [single](26 * $s), [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
  $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(245, 245, 245))
  $grey = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(170, 178, 190))
  $amber = New-Object System.Drawing.SolidBrush($accent)

  $y = [single]($h * 0.31)
  $g.DrawString('Run SETUP-HEADLESS', $fBig, $white, $x, $y); $y += [single](96 * $s)
  $g.DrawString('on the Desktop', $fBig, $amber, $x, $y); $y += [single](120 * $s)
  $g.DrawString('Power cut Oct 14: this PC was stuck at the login screen with no way in.', $fMid, $grey, $x, $y); $y += [single](48 * $s)
  $g.DrawString('One run = Tailscale, SSH, Parsec, autologon, runner. Notes: WHEN-YOU-GET-HERE.txt', $fMid, $grey, $x, $y); $y += [single](72 * $s)
  $g.DrawString('notifications\scripts\second-pc\SETUP-HEADLESS.cmd', $fSmall, $grey, $x, $y)

  $parent = Split-Path -Parent $OutFile
  if (-not (Test-Path -LiteralPath $parent)) { [void][IO.Directory]::CreateDirectory($parent) }
  $bmp.Save($OutFile, [System.Drawing.Imaging.ImageFormat]::Png)
  Write-Host "Rendered $OutFile ($w x $h)"
} finally {
  $g.Dispose(); $bmp.Dispose()
}

# ---- apply (only on the target PC, unless -Apply)
if ($env:COMPUTERNAME -ne $TargetComputer -and -not $Apply) {
  Write-Host "Not applied: this is $env:COMPUTERNAME. It takes effect on ${TargetComputer}: run this there once you are signed in (or pass -Apply)." -ForegroundColor Yellow
  exit 0
}
$current = ''
try { $current = (Get-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name WallPaper).WallPaper } catch { }
if (-not (Test-Path -LiteralPath $prevFile) -and $current -and $current -ne $OutFile) {
  if ($PSCmdlet.ShouldProcess($prevFile, "remember the current wallpaper ($current)")) {
    if (-not (Test-Path -LiteralPath $dir)) { [void][IO.Directory]::CreateDirectory($dir) }
    [IO.File]::WriteAllText($prevFile, $current)
  }
}
Set-Wallpaper $OutFile
