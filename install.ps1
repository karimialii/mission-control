# Installs Mission Control as a global Claude Code skill on Windows (a directory junction, so edits here apply everywhere).
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = "Stop"
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillsDir = Join-Path $HOME ".claude\skills"
$Dest = Join-Path $SkillsDir "mission-control"
New-Item -ItemType Directory -Force -Path $SkillsDir, (Join-Path $HOME ".mission-control") | Out-Null
if (Test-Path $Dest) {
    $item = Get-Item $Dest -Force
    if ($item.LinkType) { Remove-Item $Dest -Force }
    else { Write-Error "$Dest exists and is not a link; move it away first."; exit 1 }
}
New-Item -ItemType Junction -Path $Dest -Target $Src | Out-Null  # junctions need no admin rights or Developer Mode
Write-Host "Installed: $Dest -> $Src"
# A small mc.cmd, so `mc open` works from any project folder once its folder is on your PATH
$Bin = Join-Path $HOME ".mission-control\bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Set-Content -Path (Join-Path $Bin "mc.cmd") -Value "@echo off`r`npython `"$Src\scripts\mc.py`" %*" -Encoding ASCII
Write-Host "Command: $Bin\mc.cmd"
if (-not (($env:PATH -split ";") -contains $Bin)) { Write-Host "Add $Bin to your PATH (Settings > System > About > Advanced system settings > Environment Variables), then open a new terminal." }
Write-Host "Open the dashboard from a project folder:  mc open   (or: python `"$Src\scripts\mc.py`" open)"
