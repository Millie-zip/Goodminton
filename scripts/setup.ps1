param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$VenvDir = Join-Path $ProjectRoot ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 or newer is required. Install Python and reopen PowerShell."
    }
    python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Could not create the project virtual environment." }
}

& $PythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $PythonExe -m pip install -e $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw "Project dependency installation failed." }
& $PythonExe -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium installation failed." }

Write-Host "依赖安装完成。下一步运行："
Write-Host "  & '$PythonExe' -m buaa_booking init"
Write-Host "  & '$PythonExe' -m buaa_booking setup-secrets"
