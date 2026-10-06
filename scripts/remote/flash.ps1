<#
  Compile and flash firmware/avi_servo_v2 to the Uno on COM5, then prove the
  new image is the one running. Sends NO motion command.

  Proof is two-part, because "upload finished" is not proof of anything:
    1. arduino-cli upload --verify: avrdude reads the flash back and compares
       it byte-for-byte with what it wrote.
    2. A real DTR reset (DtrEnable pulsed, as fw-check.ps1 explains .NET
       otherwise leaves it low and never reboots the board) and the boot
       banner, which only the running sketch prints.

  The dashboard must be stopped first: it holds COM5, and a busy port makes
  avrdude fail partway, which is the one way to leave the board without a
  sketch.
#>
param([string]$Port = "COM5", [string]$Fqbn = "arduino:avr:uno")
$ErrorActionPreference = "Continue"
$cli = "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
$sketch = "$env:USERPROFILE\avi\firmware\avi_servo_v2"

if (-not (Test-Path "$sketch\avi_servo_v2.ino")) { "NO SKETCH at $sketch"; exit 3 }
"sketch: $((Get-Item "$sketch\avi_servo_v2.ino").Length) bytes"
"wrist row: " + (Select-String -Path "$sketch\avi_servo_v2.ino" -Pattern "// 3 wrist" -SimpleMatch).Line.Trim()

"=== board list ==="
& $cli board list 2>&1 | ForEach-Object { "$_" }

"=== compile ==="
& $cli compile --fqbn $Fqbn $sketch 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { "COMPILE FAILED ($LASTEXITCODE) -- nothing was written to the board"; exit 1 }

"=== upload, with read-back verify ==="
& $cli upload -p $Port --fqbn $Fqbn --verify -v $sketch 2>&1 |
  Where-Object { "$_" -match "avrdude|verif|bytes|error|Error|done|flash" } |
  ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { "UPLOAD FAILED ($LASTEXITCODE)"; exit 2 }

"=== boot check: real reset, banner, position query; no motion ==="
Start-Sleep -Milliseconds 1500
$p = New-Object System.IO.Ports.SerialPort $Port,115200,"None",8,"one"
$p.ReadTimeout = 250
$p.NewLine = "`n"
$p.DtrEnable = $true
try {
  $p.Open()
  $p.DtrEnable = $false; Start-Sleep -Milliseconds 60; $p.DtrEnable = $true
  Start-Sleep -Milliseconds 3000
  $lines = @()
  $end = (Get-Date).AddMilliseconds(1500)
  while ((Get-Date) -lt $end) { try { $lines += $p.ReadLine().Trim() } catch { Start-Sleep -Milliseconds 30 } }
  "banner: " + (($lines | Where-Object { $_ }) -join " | ")
  $p.WriteLine("?")
  $lines = @()
  $end = (Get-Date).AddMilliseconds(900)
  while ((Get-Date) -lt $end) { try { $lines += $p.ReadLine().Trim() } catch { Start-Sleep -Milliseconds 30 } }
  "query:  " + (($lines | Where-Object { $_ }) -join " | ")
} finally { if ($p.IsOpen) { $p.Close() } }
"FLASH OK"
