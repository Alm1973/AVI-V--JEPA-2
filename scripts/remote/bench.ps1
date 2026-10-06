# Run bench_encoder.py under the yolo venv.
# Exists because quoting a path with a space through ssh -> PowerShell -> python
# fails in several different ways; a -File script takes no quoting at all.
param([string]$Only = "", [int]$Runs = 10, [int]$Size = 256, [switch]$Camera)
$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
$script = "$env:USERPROFILE\avi\assistant\bench_encoder.py"
if (-not (Test-Path $py))     { throw "venv python not found at $py" }
if (-not (Test-Path $script)) { throw "bench_encoder.py not found at $script" }
$a = @("-u", $script, "--runs", $Runs, "--size", $Size)
if ($Only)   { $a += @("--only", $Only) }
if ($Camera) { $a += "--camera" }
& $py @a 2>&1
