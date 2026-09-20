param(
    [switch]$AllowSubmit,
    [string]$StartTime = "07:00",
    [string]$EndTime = "08:00",
    [string]$RunDate,
    [int]$DaysAhead = 2,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot ".."))
$ConfigPath = Join-Path $ProjectRoot "config\booking.debug.json"
$TestPath = Join-Path $ProjectRoot "tests\test_full_flow.py"

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Debug 配置不存在：$ConfigPath"
}

$Arguments = @(
    $TestPath,
    "--config", $ConfigPath,
    "--start-time", $StartTime,
    "--end-time", $EndTime,
    "--days-ahead", $DaysAhead
)

if ($RunDate) {
    $Arguments += @("--run-date", $RunDate)
}

if ($AllowSubmit) {
    Write-Warning -Message "即将进入 test_full_flow.py 的真实提交分支；该分支可能创建预约订单。"
    $Arguments = $Arguments + @("--allow-submit")
} else {
    $Arguments = $Arguments + @("--calibrate")
    Write-Host "安全模式：仅校准页面，不提交订单。"
}

Push-Location $ProjectRoot
try {
    & $PythonExe @Arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
