<#
  Hold the MSI awake for a bounded number of minutes, then release.
  Self-expiring on purpose: a forgotten session must not leave the laptop on forever.
  Beats raising the power-plan timeouts, which stay changed.
#>
param(
  [int]$Minutes = 60,
  # Must be passed explicitly: this task runs as SYSTEM, whose $env:USERPROFILE
  # is the system profile, not C:\Users\MSI-User.
  [string]$LogPath = "C:\Users\MSI-User\avi-remote\keepawake.log"
)

$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class AviPower {
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern uint SetThreadExecutionState(uint esFlags);
}
"@

$ES_CONTINUOUS      = [uint32]2147483648  # 0x80000000
$ES_SYSTEM_REQUIRED = [uint32]1           # 0x00000001

function Log($msg) {
  # Never let logging throw; it must not be able to drop the keep-awake hold.
  try {
    $dir = Split-Path -Parent $LogPath
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Add-Content -Path $LogPath -ErrorAction SilentlyContinue
  } catch { }
}

if ($Minutes -lt 1)   { $Minutes = 1 }
if ($Minutes -gt 480) { $Minutes = 480 }   # hard ceiling: 8 hours

$until = (Get-Date).AddMinutes($Minutes)
Log "keepawake START for $Minutes min (until $($until.ToString('HH:mm:ss'))) pid=$PID"

$r = [AviPower]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
if ($r -eq 0) { Log "SetThreadExecutionState FAILED (returned 0)" } else { Log "hold acquired" }

try {
  while ((Get-Date) -lt $until) {
    Start-Sleep -Seconds 30
    # Re-assert periodically; cheap, and survives a transient reset.
    [AviPower]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null
  }
} finally {
  [AviPower]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
  Log "keepawake END - hold released"
}
