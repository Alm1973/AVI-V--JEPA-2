<#
  Run a python script from avi\assistant as a scheduled task, so it survives the
  SSH session ending (Windows OpenSSH kills session-owned processes on disconnect).
  Logs to a file we can tail over SSH.

  Runs as the user with S4U logon: no stored password, and camera access was already
  proven to work from a non-interactive session. If the camera ever fails under the
  task but works over plain SSH, switch -LogonType to Interactive (needs him logged on).
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("start","stop","status","logs","daily","undaily")][string]$Action,
  [string]$TaskName = "AVI observer",
  [string]$Script   = "observer.py",
  [string]$ScriptArgs = "--no-preview",
  [int]$Tail = 40,
  [string]$At = "08:10"
)
$ErrorActionPreference = "Stop"
$Assistant = "$env:USERPROFILE\avi\assistant"
$Python    = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
$Log       = "$env:USERPROFILE\avi-remote\$($TaskName -replace '[^\w]','_').log"

function Show-Status {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { Write-Output "task '$TaskName': not installed"; return }
  $i = Get-ScheduledTaskInfo -TaskName $TaskName
  Write-Output "task '$TaskName': $($t.State)  lastRun=$($i.LastRunTime)  lastResult=$($i.LastTaskResult)"
  # 267009 = still running, 267014 = terminated by us, 0 = clean exit
  $p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
       Where-Object { $_.CommandLine -like "*$Script*" }
  Write-Output "matching python procs: $(@($p).Count)"
  if (Test-Path $Log) { Write-Output "log: $Log ($((Get-Item $Log).Length) bytes)" }
  else { Write-Output "log: none yet at $Log" }
}

switch ($Action) {
  "start" {
    if (-not (Test-Path "$Assistant\$Script")) { throw "$Assistant\$Script not found - deploy first" }
    if (-not (Test-Path $Python))              { throw "venv python not found at $Python" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null
    # cmd /c wrapper so stdout+stderr both land in the log file.
    # -u is essential: python block-buffers stdout when it is redirected to a file,
    # so without it the log stays empty for minutes and a healthy run looks hung.
    $cmd = "/c `"`"$Python`" -u `"$Assistant\$Script`" $ScriptArgs >> `"$Log`" 2>&1`""
    $act = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Assistant
    # $env:USERDOMAIN is not reliable over SSH; Register-ScheduledTask then fails
    # with 0x80070534 (no account-name-to-SID mapping). Use the real identity.
    $me  = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $pr  = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Highest
    $st  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $act -Principal $pr -Settings $st `
      -Description "AVI desk: $Script $ScriptArgs" -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 4
    Write-Output "started: $Script $ScriptArgs"
    Show-Status
  }
  "stop" {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
      Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -like "*$Script*" } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Output "stopped '$TaskName'"
  }
  "daily" {
    # The same run, started by Windows every day at $At instead of by hand.
    # Registered, NOT started: it first runs at the next $At.
    if (-not (Test-Path "$Assistant\$Script")) { throw "$Assistant\$Script not found - deploy first" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null
    $cmd = "/c `"`"$Python`" -u `"$Assistant\$Script`" $ScriptArgs >> `"$Log`" 2>&1`""
    $act = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Assistant
    $me  = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $pr  = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Highest
    $st  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 15) -MultipleInstances IgnoreNew
    $tr  = New-ScheduledTaskTrigger -Daily -At $At
    Register-ScheduledTask -TaskName "$TaskName (daily)" -Action $act -Principal $pr `
      -Settings $st -Trigger $tr -Description "AVI desk, daily at ${At}: $Script $ScriptArgs" `
      -Force | Out-Null
    Write-Output "registered '$TaskName (daily)' at ${At}: $Script $ScriptArgs"
  }
  "undaily" {
    if (Get-ScheduledTask -TaskName "$TaskName (daily)" -ErrorAction SilentlyContinue) {
      Unregister-ScheduledTask -TaskName "$TaskName (daily)" -Confirm:$false
      Write-Output "removed '$TaskName (daily)'"
    } else { Write-Output "'$TaskName (daily)' was not installed" }
  }
  "status" {
    Show-Status
    $d = Get-ScheduledTask -TaskName "$TaskName (daily)" -ErrorAction SilentlyContinue
    if ($d) { Write-Output "daily: installed, next run $((Get-ScheduledTaskInfo -TaskName "$TaskName (daily)").NextRunTime)" }
    else { Write-Output "daily: not installed" }
  }
  "logs"   { if (Test-Path $Log) { Get-Content $Log -Tail $Tail } else { Write-Output "(no log at $Log)" } }
}
