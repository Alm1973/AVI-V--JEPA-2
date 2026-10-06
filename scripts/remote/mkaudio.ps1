$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
Set-Location "$env:USERPROFILE\avi\assistant"
& $py -u "make_audio_fixtures.py" --out "fixtures_audio" 2>&1
