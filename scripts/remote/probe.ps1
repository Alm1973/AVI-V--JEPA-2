# Run latent_probe.py under the yolo venv, from the assistant folder so it
# finds latents.db next to the logger that wrote it.
param([int]$K = 8)
$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
Set-Location "$env:USERPROFILE\avi\assistant"
& $py -u "latent_probe.py" --k $K 2>&1
