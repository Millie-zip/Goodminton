param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$VenvDir = Join-Path $ProjectRoot ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    python -m venv $VenvDir
}

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -e $ProjectRoot
& $PythonExe -m playwright install chromium

Write-Host "依赖安装完成。下一步运行："
Write-Host "  & '$PythonExe' -m buaa_booking init"
Write-Host "  & '$PythonExe' -m buaa_booking setup-secrets"
