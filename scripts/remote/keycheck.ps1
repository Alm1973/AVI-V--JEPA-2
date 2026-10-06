# Does the MSI see the key, and is the cloud route available there?
$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
Set-Location "$env:USERPROFILE\avi"
& $py -c @"
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'assistant')
from avi_bot import env
names = env.load()
print('keys loaded from .env:', names)
from avi_bot.brain import providers
c = providers.Chain()
for n, ok, why in c.available():
    print(f'  {n:<11} {\"ready\" if ok else \"skipped\"}  ({why})')
"@ 2>&1
