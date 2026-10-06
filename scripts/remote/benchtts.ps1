param([string]$Model = "en_US-amy-medium")
$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
Set-Location "$env:USERPROFILE\avi\assistant"
& $py -u "bench_tts.py" --model $Model 2>&1
