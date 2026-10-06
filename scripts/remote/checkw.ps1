param([string]$Only = "")
$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
Set-Location "$env:USERPROFILE\avi\assistant"
if ($Only) { & $py -u "check_weights.py" $Only 2>&1 } else { & $py -u "check_weights.py" 2>&1 }
