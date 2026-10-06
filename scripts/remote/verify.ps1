# Import-check the deployed avi_bot package on the MSI.
$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
Set-Location "$env:USERPROFILE\avi"
& $py -c @"
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'assistant')
from avi_bot import main, scheduler, numbers
from avi_bot.brain import router, providers, tools, context, schema, speech
from avi_bot.memory import db
from avi_bot.hearing import listener
from avi_bot.voice import tts
from avi_bot.skills import (registry, timers, apps, weather, find, focus,
                            calendar, reminders, lamp, media, modes,
                            memory_tools, status)
a = main.Avi(send=lambda *x: (_ for _ in ()).throw(RuntimeError('offline')), env={})
print('avi_bot imports on the MSI; tools wired:', len(a.skills.handlers),
      'missing:', a.skills.missing())
a.close()
"@ 2>&1
