<#
  Control the "AVI keepawake" scheduled task.
  A scheduled task is used because Windows OpenSSH kills processes when the SSH
  session ends, which would drop the keep-awake hold the moment we disconnect.
  Runs as SYSTEM so no stored password is needed.
#>
param(
  [ValidateSet("start","stop","status")][string]$Action = "status",
  [int]$Minutes = 60
)

$ErrorActionPreference = "Stop"
$TaskName = "AVI keepawake"
$Script   = "$env:USERPROFILE\avi-remote\keepawake.ps1"

function Show-Status {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { Write-Output "task: not installed"; }
  else {
    $i = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Output "task: $($t.State)  lastRun=$($i.LastRunTime)  lastResult=$($i.LastTaskResult)"
  }
  $procs = Get-Process powershell -ErrorAction SilentlyContinue |
           Where-Object { $_.SessionId -eq 0 }
  Write-Output "system-session powershell procs: $(@($procs).Count)"
  Write-Output "--- powercfg /requests (SYSTEM section matters) ---"
  powercfg /requests
}

switch ($Action) {
  "start" {
    if (-not (Test-Path $Script)) { throw "keepawake.ps1 missing at $Script - push it first" }

    # Release any hold that is already running, FIRST.
    #
    # Start-ScheduledTask on a task that is already running does not start a
    # second instance, so without this the new -Minutes is silently ignored and
    # whatever ceiling the previous hold was started with keeps running. That
    # cost a whole night once: a 480-minute default hold from 22:30 outlived a
    # 120-minute hold requested at 23:01, and the laptop stayed awake until
    # 06:30 with nothing running on it. A keep-awake that ignores its own
    # ceiling is worse than no keep-awake.
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing -and $existing.State -eq "Running") {
      Write-Output "releasing the hold that is already running before re-arming"
      Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
      Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -like "*keepawake.ps1*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
      Start-Sleep -Seconds 2
    }

    $log = "$env:USERPROFILE\avi-remote\keepawake.log"
    $arg = '-NoProfile -ExecutionPolicy Bypass -File "' + $Script + '" -Minutes ' + $Minutes + ' -LogPath "' + $log + '"'
    $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
    $pr  = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
    $st  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 9)
    Register-ScheduledTask -TaskName $TaskName -Action $act -Principal $pr `
        -Settings $st -Description "AVI desk: bounded keep-awake hold" -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    Write-Output "started keepawake for $Minutes minute(s) (until $((Get-Date).AddMinutes($Minutes).ToString('HH:mm:ss')))"
    Show-Status
    $log = "$env:USERPROFILE\avi-remote\keepawake.log"
    if (Test-Path $log) {
      Write-Output "--- last 5 log lines ---"
      Get-Content $log -Tail 5
    }
  }
  "stop" {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
      Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    # The hold dies with the process, but clear it explicitly in case one lingers.
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
      Where-Object { $_.CommandLine -like "*keepawake.ps1*" } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Output "keepawake stopped"
  }
  "status" { Show-Status }
}
