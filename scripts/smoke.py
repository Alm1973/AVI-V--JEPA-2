#!/usr/bin/env python3
"""After a deploy: is the robot alive, and do moves and words still work?

Small on purpose. The camera-judged moves (/verify_move, both directions
plus a no-move control) cost no model requests; only two chat commands do,
because the free model budget is shared with him (~85 turns per model per
day). Exit status 0 only if everything passed.

Usage: scripts/smoke.py [--no-move] [--host http://msi.your-tailnet.ts.net:8000]
"""
import json
import sys
import time
import urllib.request

from msi_host import host as _host  # noqa: E402

HOST = _host()


def post(path, body=None, timeout=90):
    req = urllib.request.Request(HOST + path,
                                 data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def main(argv):
    global HOST
    if "--host" in argv:
        HOST = argv[argv.index("--host") + 1].rstrip("/")
    move = "--no-move" not in argv
    fails = []

    def check(name, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}".rstrip())
        if not ok:
            fails.append(name)

    h = post("/health")
    check("health", h.get("summary") == "all running", h.get("summary", ""))
    if "voice" in h:
        check("voice", h["voice"]["state"] in ("ok", "off"),
              json.dumps(h["voice"]))
    if move:
        for direction, deg in (("right", 8), ("left", 8), ("left", 0)):
            v = post("/verify_move", {"direction": direction,
                                      "degrees": deg})
            if deg:
                ok = v.get("judged") and v.get("agrees")
                what = f"{direction} {deg} deg"
            else:                    # the control: nothing moved
                votes = set(v.get("votes", {}).values())
                ok = votes <= {"none"}
                what = "no-move control"
                if ok and not votes:
                    # Not wrong, but blind: nobody said "still" either.
                    print(f"WARN  camera judge, {what}  no judge could see")
                    time.sleep(1.0)
                    continue
            check(f"camera judge, {what}", bool(ok),
                  json.dumps(v.get("votes")))
            time.sleep(1.0)
        for text, tool in (("look a little to the right", "nudge"),
                           ("what time is it", "get_time")):
            r = post("/ask", {"text": text})
            did = [d["tool"] for d in r.get("did", [])]
            verdicts = [d.get("verdict") for d in r.get("did", [])
                        if d.get("verdict")]
            ok = (tool in did or (tool == "nudge" and "move_joints" in did)
                  or (tool == "get_time" and r.get("say")))
            ok = ok and all(v.get("agrees") or not v.get("judged")
                            for v in verdicts)
            check(f"ask {text!r}", bool(ok),
                  f"via {r.get('provider') or r.get('route')}: {did} "
                  f"{r.get('say') or ''}")
            time.sleep(2.0)
    print(f"\n{'all passed' if not fails else 'FAILED: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
