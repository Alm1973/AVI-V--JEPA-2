#!/usr/bin/env python3
"""Say things to AVI from the Mac, the way the page's ask box does.

    scripts/ask.py "look at the mug" "what time is it"   [--gap 20]

Prints what AVI said and what it DID, with the camera's verdict on each
move. Every line is a real model request from his free budget (~85 a day
per model): keep runs short.
"""
import json
import sys
import time
import urllib.request

from msi_host import host as _host  # noqa: E402

HOST = _host()


def ask(text, timeout=120):
    req = urllib.request.Request(HOST + "/ask",
                                 data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def main(argv):
    gap = 20.0
    if "--gap" in argv:
        i = argv.index("--gap")
        gap = float(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    for n, text in enumerate(argv):
        if n:
            time.sleep(gap)
        t0 = time.time()
        try:
            r = ask(text)
        except Exception as exc:
            print(f"### {text!r}: no answer ({exc!r})")
            continue
        print(f"### {text!r}  ({time.time() - t0:.1f}s, "
              f"{r.get('provider') or r.get('route')})")
        print(f"  say: {r.get('say')}" + ("  [quick rules]"
                                          if r.get("fallback") else ""))
        for d in r.get("did", []):
            v = d.get("verdict") or {}
            seen = (f" camera: {v.get('votes')} "
                    f"{'OK' if v.get('agrees') else 'MISMATCH'}"
                    if v.get("judged") else "")
            if d.get("in_view") is not None:
                seen += f" in view: {d['in_view']}"
            print(f"  did: {d['tool']} {d.get('args')}"
                  f"{'' if d.get('ok') else ' FAILED ' + str(d.get('error'))}"
                  f"{seen}")
        if r.get("error"):
            print(f"  error: {r['error']}")
        sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
