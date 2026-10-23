<#
Sets up the SSC Tender Tracker on this Windows laptop.

  Setup:   powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1                  (asks every hour, 09:00-21:00)
           powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -From 10:00 -Hours 8
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
    [switch]$Remove
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

# 2. Hourly question.
$user = "$env:USERDOMAIN\$env:USERNAME"
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
