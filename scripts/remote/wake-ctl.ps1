<#
  Wake timers, so the MSI can be reached from away from home.

  Wake-on-LAN only works from the home LAN, so when Shaurya is out there is no
  way to bring this machine up on demand. These timers wake it on a schedule
  instead and hold it awake just long enough to be useful, then let it sleep.

  WHY IT HOLDS AWAKE RATHER THAN JUST WAKING
  A wake with nobody present hits Windows' hidden unattended-sleep timeout
  (about 120 s, invisible in `powercfg /q`) and goes straight back down. Two
  minutes is not a window you can rely on catching, so each wake runs the same
  bounded keepawake hold the rest of the project uses.

  REQUIRES "Allow wake timers" to be enabled. It is ON for AC and OFF for DC on
  this machine, so these timers do not fire on battery. Keep it plugged in.
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("install","remove","status")][string]$Action,
  [int]$HoldMinutes = 10,
  [string]$TaskName = "AVI wake"
)
$ErrorActionPreference = "Stop"
$Script = "$env:USERPROFILE\avi-remote\keepawake.ps1"
$Log    = "$env:USERPROFILE\avi-remote\wake.log"

function Show-Status {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { Write-Output "task '$TaskName': not installed"; return }
  $i = $t | Get-ScheduledTaskInfo
  Write-Output "task '$TaskName': $($t.State)  lastRun=$($i.LastRunTime)  lastResult=$($i.LastTaskResult)"
  Write-Output "next wake: $($i.NextRunTime)"
  Write-Output "wakeToRun: $($t.Settings.WakeToRun)"
  foreach ($tr in $t.Triggers) {
    $rep = ""
    if ($tr.Repetition -and $tr.Repetition.Interval) {
      $rep = " every $($tr.Repetition.Interval) for $($tr.Repetition.Duration)"
    }
    Write-Output ("  trigger: " + $tr.StartBoundary + $rep +
                  $(if ($tr.DaysOfWeek) { " days=$($tr.DaysOfWeek)" } else { "" }))
  }
  # The setting that makes all of this work, or silently not work.
  $ac = (powercfg /q SCHEME_CURRENT SUB_SLEEP bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d |
         Select-String "Current AC Power Setting Index").ToString().Split(":")[-1].Trim()
  $dc = (powercfg /q SCHEME_CURRENT SUB_SLEEP bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d |
         Select-String "Current DC Power Setting Index").ToString().Split(":")[-1].Trim()
  Write-Output "allow-wake-timers: AC=$ac DC=$dc   (0=off 1=on 2=important-only)"
  if ($dc -eq "0x00000000") {
    Write-Output "NOTE: wake timers will NOT fire on battery. Keep it plugged in."
  }
}

switch ($Action) {
  "install" {
    if (-not (Test-Path $Script)) { throw "keepawake.ps1 missing at $Script - push it first" }

    $arg = '-NoProfile -ExecutionPolicy Bypass -File "' + $Script +
           '" -Minutes ' + $HoldMinutes + ' -LogPath "' + $Log + '"'
    $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg

    # A two-hourly grid across the waking day, as EIGHT SEPARATE daily triggers
    # rather than one trigger repeating every two hours.
    #
    # This is not a style choice. Windows arms a hardware wake timer for a
    # trigger's start boundary, but NOT for the repetition occurrences under
    # it. With a repeating trigger, `powercfg /waketimers` listed only the next
    # start boundary -- so the grid would have fired only when the machine
    # happened to be awake already, which is exactly the case it exists to
    # cover. Separate triggers each arm their own wake.
    $grid = @()
    foreach ($h in 8, 10, 12, 14, 16, 18, 20, 22) {
      # Off-the-hour minute on purpose, so a wake never lands on the same
      # instant as anything else scheduled on this machine.
      $grid += New-ScheduledTaskTrigger -Daily -At ("{0:00}:05" -f $h)
    }

    # Desk hours, so it is already up when he sits down: weekdays 14:30,
    # Tuesdays 20:00. Two minutes early, to be up and on the tailnet by then.
    $desk = New-ScheduledTaskTrigger -Weekly -At "14:28" `
              -DaysOfWeek Monday,Wednesday,Thursday,Friday
    $tue  = New-ScheduledTaskTrigger -Weekly -At "19:58" -DaysOfWeek Tuesday

    $pr = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
    $st = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -MultipleInstances IgnoreNew
    # StartWhenAvailable catches up a wake the machine slept through, e.g. after
    # being unplugged; without it a missed window is simply lost.
    $st.StartWhenAvailable = $true

    Register-ScheduledTask -TaskName $TaskName -Action $act -Principal $pr `
        -Settings $st -Trigger ($grid + $desk + $tue) `
        -Description "AVI desk: wake the MSI so it can be reached from away from home" `
        -Force | Out-Null
    Write-Output "installed '$TaskName', holding $HoldMinutes min per wake"
    Show-Status
  }
  "remove" {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "removed '$TaskName'"
  }
  "status" { Show-Status }
}
