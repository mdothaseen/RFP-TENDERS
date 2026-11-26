<#
Sets up the SSC Tender Tracker on this Windows laptop.

  Setup:   powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1                  (asks every hour, 09:00-21:00)
           powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -From 10:00 -Hours 8
  24x7:    powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -Auto -From 08:00 -EveryHours 8 -User tenderbot
           (no popup: scans at 08:00, 16:00, 00:00 as the user "tenderbot", also while nobody is signed in;
            asks once for that user's password. Run it in an administrator PowerShell.)
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
    [switch]$Auto,
    [int]$EveryHours = 8,
    [string]$User = ""
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

$currentUser = "$env:USERDOMAIN\$env:USERNAME"   # not $user: PowerShell names ignore case and $User is the -User parameter

if ($Auto) {
    # No popup: a full scan + email every $EveryHours hours from $From. A start is skipped while a scan still runs.
    $bat = Join-Path $Root "run_daily.bat"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$bat`" --force`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Daily -At $From
    $repeat = New-ScheduledTaskTrigger -Once -At $From -RepetitionInterval (New-TimeSpan -Hours $EveryHours) `
        -RepetitionDuration (New-TimeSpan -Hours 24)
    $trigger.Repetition = $repeat.Repetition
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 5) -MultipleInstances IgnoreNew
    $description = "Scans SSC, NSDC and NCVET websites every $EveryHours hours from $From and emails the summary."
    if ($User) {
        # Runs as this account whether or not anyone is signed in; Windows keeps the password, not this script.
        $cred = Get-Credential -UserName $User -Message "Password of the Windows user $User (used only by Task Scheduler)"
        if (-not $cred) { Write-Error "No password given."; exit 1 }
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force `
            -Description $description | Out-Null
        $who = "$User (also while nobody is signed in)"
    } else {
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -Principal $principal -Force -Description $description | Out-Null
        $who = "$currentUser (only while signed in)"
    }
    $times = (0..([math]::Max(1, [int](24 / $EveryHours)) - 1) | ForEach-Object {
        ([datetime]$From).AddHours($_ * $EveryHours).ToString("HH:mm") }) -join ", "
    Write-Host "Ready: scan + email at $times every day, as $who."
    Write-Host "Progress: logs\run_<date>.log   Remove: setup_scheduler.ps1 -Remove"
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
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "Every hour asks whether to scan SSC, NSDC and NCVET websites for new tenders." | Out-Null

Write-Host "Ready: popup every hour from $From for $Hours hours. 'Haan' starts scan + Claude reading + Excel + email."
Write-Host "Try it now:  .venv\Scripts\python -m tender_tracker --ask"
