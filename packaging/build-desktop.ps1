param([switch]$Clean)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$buildArgs = @('--skip-tests')
if ($Clean) { $buildArgs += '--clean' }
python tools\build.py @buildArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
Write-Host "Built dist\iNTUition\iNTUition.exe"
