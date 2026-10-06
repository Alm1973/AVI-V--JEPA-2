$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
& $py -m pip install --quiet piper-tts 2>&1 | Select-Object -Last 5
& $py -c "import piper; print('piper import ok', getattr(piper,'__version__','?'))" 2>&1
