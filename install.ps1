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
Write-Host "Dashboard: python `"$Src\scripts\mc.py`" dashboard --open"
