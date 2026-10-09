"""Which stored facts name a day, and what tomorrow's rundown will say.

Run ON THE MSI; prints fact text the brain store already holds (his own
words to AVI) and the dates read from them. Read-only.

    python dated_check.py [store.db] [YYYY-MM-DD]
"""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avi_bot.memory import dated, db  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else str(
    Path.home() / "avi-remote" / "brain-dev.db")
day = (date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2
       else date.today())
store = db.Store(path)
for f in store.facts():
    d = dated.day_named(f["text"], f["created_at"], today=day)
    if d:
        print(f"{f['text']!r} said {datetime.fromtimestamp(f['created_at']):%a %d %b}"
              f" -> {d:%a %d %b}")
print("on", day, "ahead:", dated.ahead(store.facts(), day))
store.close()
