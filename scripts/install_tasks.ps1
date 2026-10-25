param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ConfigPath = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $ProjectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $ProjectPython) {
        $PythonExe = $ProjectPython
    } elseif (-not [string]::IsNullOrWhiteSpace($env:CONDA_PREFIX)) {
        $PythonExe = Join-Path $env:CONDA_PREFIX "python.exe"
    } else {
        throw "Run scripts/setup.ps1 first, or pass -PythonExe with an installed project environment."
    }
}
if (-not [System.IO.Path]::IsPathRooted($PythonExe)) {
    $PythonExe = Join-Path $ProjectRoot $PythonExe
}
$PythonExe = [System.IO.Path]::GetFullPath($PythonExe)
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}
& $PythonExe -c "import buaa_booking" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The selected Python cannot import buaa_booking. Install the project in that environment first."
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot "config\booking.json"
}
if (-not [System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot $ConfigPath
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
    -MultipleInstances IgnoreNew `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

function New-BookingAction([string]$Command) {
    $Arguments = "-m buaa_booking --config `"$ConfigPath`" $Command"
    return New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $ProjectRoot
}

$DashboardTriggers = @(
    New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    New-ScheduledTaskTrigger -Daily -At "22:29"
    New-ScheduledTaskTrigger -Daily -At "06:58"
)
$DashboardTask = New-ScheduledTask `
    -Action (New-BookingAction "serve") `
    -Trigger $DashboardTriggers `
    -Principal $Principal `
    -Settings $DashboardSettings
Register-ScheduledTask -TaskName "BUAA-Badminton-Dashboard" -InputObject $DashboardTask -Force | Out-Null

$PromptTask = New-ScheduledTask `
    -Action (New-BookingAction "prompt") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "22:30") `
    -Principal $Principal `
    -Settings $CommonSettings
Register-ScheduledTask -TaskName "BUAA-Badminton-Prompt" -InputObject $PromptTask -Force | Out-Null

$ReminderTimes = @(
    "22:45", "23:00", "23:15", "23:30", "23:45",
    "00:00", "00:15", "00:30", "00:45",
    "01:00", "01:15", "01:30", "01:45"
)
$ReminderTriggers = @(
    $ReminderTimes | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
)
$ReminderTask = New-ScheduledTask `
    -Action (New-BookingAction "prompt --reminder") `
    -Trigger $ReminderTriggers `
    -Principal $Principal `
    -Settings (New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5))
Register-ScheduledTask -TaskName "BUAA-Badminton-Reminder" -InputObject $ReminderTask -Force | Out-Null
& $PythonExe -m buaa_booking --config $ConfigPath sync-reminder
if ($LASTEXITCODE -ne 0) {
    throw "Reminder task state could not be synchronized with the saved plan."
}

$RunTask = New-ScheduledTask `
    -Action (New-BookingAction "run") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "06:59") `
    -Principal $Principal `
    -Settings $CommonSettings
Register-ScheduledTask -TaskName "BUAA-Badminton-Run" -InputObject $RunTask -Force | Out-Null
Start-ScheduledTask -TaskName "BUAA-Badminton-Dashboard"

Write-Host "Registered 4 scheduled tasks for the current Windows user:"
Write-Host "  Python: $PythonExe"
Write-Host "  BUAA-Badminton-Dashboard (started now; also at logon, 22:29, and 06:58)"
Write-Host "  BUAA-Badminton-Prompt (daily at 22:30)"
Write-Host "  BUAA-Badminton-Reminder (22:45-01:45 every 15 minutes; disabled after phone confirmation; re-enabled by the next nightly prompt)"
Write-Host "  BUAA-Badminton-Run (daily at 06:59)"
