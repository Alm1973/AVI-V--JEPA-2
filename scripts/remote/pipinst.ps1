param([string]$Packages = "")
$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
& $py -m pip install --quiet $Packages.Split(" ") 2>&1 | Select-Object -Last 4
