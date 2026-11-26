<#
Sets up the SSC Tender Tracker on this Windows laptop.

  Setup:   powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1                  (asks every hour, 09:00-21:00)
           powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -From 10:00 -Hours 8
  Non-stop: powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -Auto   (no popup, scans 24x7, email when new)
  Remove:  powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -Remove

What it does:
  1. Task "SSC Tender Tracker": every hour a popup asks "SSC websites scan karun?" [Haan] [Abhi nahi].
     No popup while a scan is running or within 50 minutes after one.
  2. Registers the "ssctracker://" link for the current user, so the popup's "Haan" button starts the scan:
     scan -> new documents downloaded -> read by Claude (your Claude plan) -> Excel updated -> email.
#>
param(
    [string]$From = "09:00",
    [int]$Hours = 12,
    [switch]$Remove,
    [switch]$Auto
)

$TaskName = "SSC Tender Tracker"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Click = Join-Path $Root "tracker_click.pyw"
$ProtocolKey = "HKCU:\Software\Classes\ssctracker"

# Older setups had a fixed daily scan and an email fallback task.
Unregister-ScheduledTask -TaskName "SSC Tender Tracker - Email Fallback" -Confirm:$false -ErrorAction SilentlyContinue

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -Path $ProtocolKey -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Removed the task and the ssctracker:// link."
    exit 0
}

if (-not (Test-Path $Pythonw)) {
    Write-Error ".venv not found in $Root - see README.md"
    exit 1
}

# 1. Link used by the popup buttons.
New-Item -Path "$ProtocolKey\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path $ProtocolKey -Name "(default)" -Value "URL:SSC Tender Tracker"
Set-ItemProperty -Path $ProtocolKey -Name "URL Protocol" -Value ""
Set-ItemProperty -Path "$ProtocolKey\shell\open\command" -Name "(default)" -Value "`"$Pythonw`" `"$Click`" `"%1`""

$user = "$env:USERDOMAIN\$env:USERNAME"

if ($Auto) {
    # Non-stop: no popup. The task fires every 15 minutes; while a scan runs (about 2 hours) the next
    # start is skipped, so a new scan begins soon after the previous one ends. Email only when something is new.
    $bat = Join-Path $Root "run_daily.bat"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$bat`" --force`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 5) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force `
        -Description "Scans SSC, NSDC and NCVET websites non-stop; emails when something new is found." | Out-Null
    Write-Host "Ready: non-stop scanning. First scan starts in about 1 minute; each new scan starts when the last one ends."
    Write-Host "Progress: logs\run_<date>.log   Stop: setup_scheduler.ps1 -Remove"
    exit 0
}

# 2. Hourly question.
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m tender_tracker --ask" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Daily -At $From
$repeat = New-ScheduledTaskTrigger -Once -At $From -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours $Hours)
$trigger.Repetition = $repeat.Repetition
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "Every hour asks whether to scan SSC, NSDC and NCVET websites for new tenders." | Out-Null

Write-Host "Ready: popup every hour from $From for $Hours hours. 'Haan' starts scan + Claude reading + Excel + email."
Write-Host "Try it now:  .venv\Scripts\python -m tender_tracker --ask"
