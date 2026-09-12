param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ConfigPath = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    if ([string]::IsNullOrWhiteSpace($env:CONDA_PREFIX)) {
        throw "No active Conda environment. Activate buaa-booking before running this script."
    }
    $PythonExe = Join-Path $env:CONDA_PREFIX "python.exe"
}
$PythonExe = [System.IO.Path]::GetFullPath($PythonExe)
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}
& $PythonExe -c "import buaa_booking" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The active Conda environment does not contain buaa_booking. Install the project before registering tasks."
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot "config\booking.json"
}
$ConfigPath = [System.IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Configuration file not found: $ConfigPath"
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
$CommonSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$DashboardSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

function New-BookingAction([string]$Command) {
    $Arguments = "-m buaa_booking --config `"$ConfigPath`" $Command"
    return New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $ProjectRoot
}

$DashboardTask = New-ScheduledTask `
    -Action (New-BookingAction "serve") `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser) `
    -Principal $Principal `
    -Settings $DashboardSettings
Register-ScheduledTask -TaskName "BUAA-Badminton-Dashboard" -InputObject $DashboardTask -Force | Out-Null

$PromptTask = New-ScheduledTask `
    -Action (New-BookingAction "prompt") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "22:30") `
    -Principal $Principal `
    -Settings $CommonSettings
Register-ScheduledTask -TaskName "BUAA-Badminton-Prompt" -InputObject $PromptTask -Force | Out-Null

$ReminderTask = New-ScheduledTask `
    -Action (New-BookingAction "prompt --reminder") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "22:50") `
    -Principal $Principal `
    -Settings $CommonSettings
Register-ScheduledTask -TaskName "BUAA-Badminton-Reminder" -InputObject $ReminderTask -Force | Out-Null

$RunTask = New-ScheduledTask `
    -Action (New-BookingAction "run") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "06:59") `
    -Principal $Principal `
    -Settings $CommonSettings
Register-ScheduledTask -TaskName "BUAA-Badminton-Run" -InputObject $RunTask -Force | Out-Null

Write-Host "Registered 4 scheduled tasks for the current Windows user:"
Write-Host "  Python: $PythonExe"
Write-Host "  BUAA-Badminton-Dashboard (at Windows logon)"
Write-Host "  BUAA-Badminton-Prompt (daily at 22:30)"
Write-Host "  BUAA-Badminton-Reminder (daily at 22:50)"
Write-Host "  BUAA-Badminton-Run (daily at 06:59)"
