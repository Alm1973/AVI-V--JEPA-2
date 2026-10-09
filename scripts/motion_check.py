#!/usr/bin/env python3
"""The dance and self-model checks on the robot, no model requests used.

  1. a small dance (size 1), then a normal one, judged from the picture:
     did the view move, did it come back (Arm.perform)
  2. motor babbling: small single-joint moves, and how the old rule, the
     3D body model and the learned slopes predicted them (POST /babble)

Prints numbers only; frames never leave the MSI. Moves are small and slow
(CLAUDE.md): dances at 27 deg/s, babbling at most --deg degrees.

Usage: scripts/motion_check.py [dance|babble|all] [--deg 6] [--n 16]
"""
import json
import sys
import time
import urllib.request

HOST = "http://msi.your-tailnet.ts.net:8000"


def post(path, body=None, timeout=600):
    req = urllib.request.Request(HOST + path,
                                 data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def get_state():
    return json.load(urllib.request.urlopen(HOST + "/state", timeout=30))


def dance(name, moves):
    before = (get_state().get("last_performance") or {})
    r = post("/perform", {"name": name, "moves": moves})
    if not r.get("ok"):
        print(f"FAIL  {name}: {r.get('error')}")
        return None
    secs = (r.get("data") or {}).get("seconds") or 20
    print(f"      {name}: {secs} s, {(r.get('data') or {}).get('notes')}")
    t_end = time.time() + secs + 25
    while time.time() < t_end:
        time.sleep(3)
        got = get_state().get("last_performance")
        if got and got != before:
            ok = got.get("moved") is True
            print(f"{'PASS' if ok else 'FAIL'}  {name}: moved "
                  f"{got.get('moved')}, back {got.get('back')}  votes "
                  f"{json.dumps(got.get('votes'))}  diff max "
                  f"{got.get('max_diff')} end {got.get('final_diff')} jepa "
                  f"min {got.get('jepa_min')} light {got.get('light')}")
            return got
    print(f"FAIL  {name}: no verdict in time")
    return None


def babble(n, deg):
    r = post("/babble", {"n": n, "max_deg": deg}, timeout=1200)
    if r.get("error"):
        print("FAIL  babble:", r["error"])
        return None
    rows = r.get("rows", [])
    print(f"      babble: {len(rows)} measured moves (seed {r.get('seed')})")
    for which, rep in (r.get("compare") or {}).items():
        print(f"  [{which}]")
        for j, row in rep.get("joints", {}).items():
            print(f"    {j:9s} n={row['n']:2d}  learned px/deg "
                  f"{row.get('measured_px_per_deg')}")
            for k in ("old_rule", "model", "learned"):
                if k in row:
                    print(f"      {k:9s} err {row[k]['mean_err_px']:7.1f} px"
                          f"  direction right {row[k]['direction_right']}")
    return r


def main(argv):
    what = argv[1] if len(argv) > 1 and not argv[1].startswith("-") \
        else "all"
    deg = float(argv[argv.index("--deg") + 1]) if "--deg" in argv else 6.0
    n = int(argv[argv.index("--n") + 1]) if "--n" in argv else 16
    h = post("/health")
    print(("PASS" if h.get("summary") == "all running" else "FAIL"),
          " health ", h.get("summary"))
    if what in ("dance", "all"):
        small = [{"move": m, "size": 1} for m in
                 ("sway", "bob", "leap", "wave")]
        dance("motion check small", small)
        dance("motion check dolphin", [
            {"move": "leap"}, {"move": "wave"}, {"move": "leap"},
            {"move": "sway", "times": 2}])
        # Test dances are not his: keep them out of the panel and the
        # model's context.
        for name in ("motion check small", "motion check dolphin"):
            post("/forget_move", {"name": name})
    if what in ("babble", "all"):
        babble(n, deg)


if __name__ == "__main__":
    main(sys.argv)
