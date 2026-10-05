<#
  Bench-check firmware v2, with a REAL Uno reset.

  .NET's SerialPort leaves DtrEnable false, so opening the port does NOT pulse
  DTR and does NOT reset the Uno. An earlier version of this script "reset" the
  board four times without ever rebooting it, and the position surviving was
  just RAM in a board that had never restarted -- which looked exactly like a
  successful PCA9685 read-back. DtrEnable is set explicitly here, and the boot
  banner is the proof that a reboot actually happened.
#>
param([double]$Target = 95.0, [double]$Seconds = 3.0)
$ErrorActionPreference = "Stop"

function Open-Board([bool]$Reset = $true) {
  $p = New-Object System.IO.Ports.SerialPort "COM5",115200,"None",8,"one"
  $p.ReadTimeout = 250
  $p.NewLine = "`n"
  $p.DtrEnable = $Reset          # THIS is what resets the Uno
  $p.Open()
  if ($Reset) {
    $p.DtrEnable = $false; Start-Sleep -Milliseconds 60
    $p.DtrEnable = $true         # a clean low->high pulse
  }
  Start-Sleep -Milliseconds 3000 # bootloader timeout, then the sketch starts
  return $p
}

# Read for a fixed window rather than until the first timeout, so a reply that
# arrives in pieces is not mistaken for silence.
function Collect($p, [int]$ms = 900) {
  $out = @()
  $end = (Get-Date).AddMilliseconds($ms)
  while ((Get-Date) -lt $end) {
    try { $out += $p.ReadLine().Trim() } catch { Start-Sleep -Milliseconds 30 }
  }
  return ($out | Where-Object { $_ }) -join " | "
}
function Ask($p, $cmd, [int]$ms = 900) {
  Collect $p 150 | Out-Null
  $p.WriteLine($cmd)
  return (Collect $p $ms)
}

"=== first open, with a real DTR reset ==="
$p = Open-Board $true
try {
  "banner: " + (Collect $p 1200)
  "mode:   " + (Ask $p "M")
  "pos0:   " + (Ask $p "?")

  "--- driving base_yaw to $Target for $Seconds s ---"
  $end = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $end) {
    $p.WriteLine("P $Target 90 90 90")
    Start-Sleep -Milliseconds 20
  }
  "pos1:   " + (Ask $p "?")
} finally { if ($p.IsOpen) { $p.Close() } }

Start-Sleep -Milliseconds 900
"=== reopen WITH a reset: does it read its position back? ==="
$p = Open-Board $true
try {
  "banner: " + (Collect $p 1200)
  "mode:   " + (Ask $p "M")
  "pos2:   " + (Ask $p "?") + "   <- $Target means the read-back worked; 90.0 means it did not"
} finally { if ($p.IsOpen) { $p.Close() } }
