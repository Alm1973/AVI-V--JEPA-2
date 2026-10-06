# Run latent_log.py directly (not as a task), for short smoke runs where the
# output should come straight back over ssh.
param([double]$Hours = 0.05, [double]$Interval = 15)
$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
Set-Location "$env:USERPROFILE\avi\assistant"
& $py -u "latent_log.py" --hours $Hours --interval $Interval 2>&1
