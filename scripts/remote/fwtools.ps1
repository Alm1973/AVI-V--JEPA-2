# Report what is available on the MSI for compiling and flashing the Uno.
# Reads nothing from the serial port; safe to run with the dashboard up.
$found = @()
$cli = Get-Command arduino-cli -ErrorAction SilentlyContinue
if ($cli) { $found += "arduino-cli on PATH: $($cli.Source)" }
$cands = @(
  "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
  "$env:ProgramFiles\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
  "$env:ProgramFiles\Arduino CLI\arduino-cli.exe",
  "${env:ProgramFiles(x86)}\Arduino\arduino-cli.exe",
  "${env:ProgramFiles(x86)}\Arduino\arduino_debug.exe",
  "${env:ProgramFiles(x86)}\Arduino\arduino.exe")
foreach ($c in $cands) { if (Test-Path $c) { $found += "exists: $c" } }
$a15 = "$env:LOCALAPPDATA\Arduino15"
if (Test-Path $a15) {
  $found += "Arduino15: $a15"
  Get-ChildItem "$a15\packages" -Directory -ErrorAction SilentlyContinue | ForEach-Object { $found += "  package: $($_.Name)" }
  $avr = Get-ChildItem "$a15\packages\arduino\hardware\avr" -Directory -ErrorAction SilentlyContinue
  foreach ($v in $avr) { $found += "  avr core: $($v.Name)" }
  $dude = Get-ChildItem "$a15\packages\arduino\tools\avrdude" -Recurse -Filter avrdude.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($dude) { $found += "  avrdude: $($dude.FullName)" }
}
$lib = "$env:USERPROFILE\Documents\Arduino\libraries"
if (Test-Path $lib) { Get-ChildItem $lib -Directory | ForEach-Object { $found += "  library: $($_.Name)" } }
if (-not $found) { "NOTHING FOUND: no arduino-cli, IDE, or Arduino15 directory" } else { $found }
