$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
& $py -c "import sys,platform;print('python',sys.version.split()[0]);print('machine',platform.machine())"
& $py -m pip list 2>$null | Select-String -Pattern "kokoro|piper|soundfile|sounddevice|openai|pydantic|numpy|torch|misaki" | ForEach-Object { $_.Line }
