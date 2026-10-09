# Why did the MSI go offline? Sleep, hibernate, crash, update reboot or a
# lost network -- from the System event log and the keep-awake log, since
# the given hour. Prints events only; nothing about the room.
param([int]$Hours = 12)
$since = (Get-Date).AddHours(-$Hours)
Write-Output "now $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); booted $((Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('yyyy-MM-dd HH:mm:ss'))"
$ids = @{
  'Microsoft-Windows-Kernel-Power' = @(41, 42, 107, 109, 506, 507)
  'Microsoft-Windows-Kernel-General' = @(12, 13)
  'Microsoft-Windows-Power-Troubleshooter' = @(1)
  'User32' = @(1074)
  'EventLog' = @(6005, 6006, 6008)
  'Microsoft-Windows-WindowsUpdateClient' = @(19, 20, 43)
  'Tcpip' = @(4201, 4202)
}
$events = foreach ($p in $ids.Keys) {
  Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName=$p; StartTime=$since} -ErrorAction SilentlyContinue |
    Where-Object { $ids[$p] -contains $_.Id }
}
$events | Sort-Object TimeCreated | ForEach-Object {
  $msg = ($_.Message -split "`n")[0].Trim()
  if ($msg.Length -gt 140) { $msg = $msg.Substring(0, 140) }
  "{0}  {1,-40} {2,5}  {3}" -f $_.TimeCreated.ToString('MM-dd HH:mm:ss'), $_.ProviderName, $_.Id, $msg
}
Write-Output "--- powercfg /lastwake"
powercfg /lastwake 2>&1 | Select-Object -First 8
Write-Output "--- keep-awake log (tail)"
$log = Join-Path $env:USERPROFILE "avi-remote\keepawake.log"
if (Test-Path $log) { Get-Content $log -Tail 12 } else { "no keepawake.log" }
Write-Output "--- active power requests"
powercfg /requests 2>&1 | Select-Object -First 20
