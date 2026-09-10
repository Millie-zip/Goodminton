param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "找不到虚拟环境 Python，请先运行 scripts\setup.ps1"
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot "config\booking.json"
}
$ConfigPath = [System.IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "找不到配置文件：$ConfigPath"
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

Write-Host "已注册 4 个当前用户任务："
Write-Host "  BUAA-Badminton-Dashboard（登录 Windows 后启动）"
Write-Host "  BUAA-Badminton-Prompt（每天 22:30）"
Write-Host "  BUAA-Badminton-Reminder（每天 22:50）"
Write-Host "  BUAA-Badminton-Run（每天 06:59）"

