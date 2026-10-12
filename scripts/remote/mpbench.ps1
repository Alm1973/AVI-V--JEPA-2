# Run bench_mediapipe.py in its own Python 3.12 env (never .venv-yolo, which
# reproduces the paper's results and takes no new packages). Creates the env
# with the MSI's miniconda the first time. A -File script, so no path with a
# space has to survive ssh -> PowerShell quoting.
param([int]$Seconds = 60)
$ErrorActionPreference = "Stop"
$env_ = "$env:USERPROFILE\avi-desk\.conda312"
$py = "$env_\python.exe"
$script = "$env:USERPROFILE\avi\assistant\bench_mediapipe.py"
if (-not (Test-Path $script)) { throw "bench_mediapipe.py not found at $script - push it first" }
if (-not (Test-Path $py)) {
  $conda = @("$env:USERPROFILE\miniconda3\Scripts\conda.exe", "C:\ProgramData\miniconda3\Scripts\conda.exe") |
           Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $conda) { throw "miniconda not found; cannot create the Python 3.12 env" }
  Write-Output "creating Python 3.12 env at $env_ (one time)"
  & $conda create -y -q -p $env_ python=3.12 2>&1 | Select-Object -Last 3
}
& $py -m pip install -q mediapipe opencv-python psutil 2>&1 | Select-Object -Last 3
& $py -c "import mediapipe, cv2, psutil, sys; print('python', sys.version.split()[0], 'mediapipe', mediapipe.__version__)"
& $py -u $script --seconds $Seconds 2>&1
