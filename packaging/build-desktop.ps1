param([switch]$Clean)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue 'build\intuition', 'dist\iNTUition'
}
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\intuition.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
Write-Host "Built dist\iNTUition\iNTUition.exe"
