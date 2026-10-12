# Run bench_mediapipe.py in its own Python 3.12 env (never .venv-yolo, which
# reproduces the paper's results and takes no new packages). Creates the env
# with the MSI's miniconda the first time. A -File script, so no path with a
# space has to survive ssh -> PowerShell quoting.
#
# Native commands are checked by exit code, NOT by $ErrorActionPreference:
# in Windows PowerShell 5.1 any stderr line from conda or pip (a warning, a
# progress note) under "Stop" + 2>&1 becomes a terminating NativeCommandError
# that hides the actual message. That is how the first run died, 2026-10-12.
#
# conda-forge with --override-channels: the default Anaconda channels now
# require accepting their Terms of Service interactively, which over SSH just
# fails.
param([int]$Seconds = 60, [switch]$Mjpg, [string]$Size = "1280x720")
$ErrorActionPreference = "Continue"
$env_ = "$env:USERPROFILE\avi-desk\.conda312"
$py = "$env_\python.exe"
$script = "$env:USERPROFILE\avi\assistant\bench_mediapipe.py"
if (-not (Test-Path $script)) { Write-Output "bench_mediapipe.py not found at $script - push it first"; exit 1 }

if (-not (Test-Path $py)) {
  $conda = @("$env:USERPROFILE\miniconda3\Scripts\conda.exe", "C:\ProgramData\miniconda3\Scripts\conda.exe") |
           Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $conda) { Write-Output "miniconda not found; cannot create the Python 3.12 env"; exit 1 }
  # A half-made env from a failed run makes `conda create -p` refuse; clear it.
  if (Test-Path $env_) { Remove-Item -Recurse -Force $env_ }
  Write-Output "creating Python 3.12 env at $env_ (one time, a few minutes)"
  & $conda create -y -q -p $env_ -c conda-forge --override-channels python=3.12 pip 2>&1 | ForEach-Object { "$_" } | Select-Object -Last 15
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { Write-Output "conda create failed (exit $LASTEXITCODE); output above"; exit 1 }
}

# conda-forge's python ships without pip unless asked; an env made before
# `pip` was in the create line needs it bootstrapped.
& $py -c "import pip" 2>$null
if ($LASTEXITCODE -ne 0) {
  & $py -m ensurepip --upgrade 2>&1 | ForEach-Object { "$_" } | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { Write-Output "ensurepip failed (exit $LASTEXITCODE)"; exit 1 }
}
& $py -m pip install -q --disable-pip-version-check mediapipe opencv-python psutil 2>&1 | ForEach-Object { "$_" } | Select-Object -Last 10
if ($LASTEXITCODE -ne 0) { Write-Output "pip install failed (exit $LASTEXITCODE); output above"; exit 1 }
& $py -c "import mediapipe, cv2, psutil, sys; print('python', sys.version.split()[0], 'mediapipe', mediapipe.__version__)" 2>&1 | ForEach-Object { "$_" }
$extra = @("--size", $Size); if ($Mjpg) { $extra += "--mjpg" }
& $py -u $script --seconds $Seconds @extra 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -notmatch '^(W|I)0000|inference_feedback' }
exit $LASTEXITCODE
