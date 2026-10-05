"""Check the robot's activity labels against your own (milestone 4's test).

Run the observer with --audit-rate 0.05 for a few days, then:
    python audit.py                 # label the saved thumbnails
    python audit.py --delete-after  # also delete each thumbnail once labeled

For each thumbnail, press the number of what you were really doing,
s to skip, q to stop. Your labels go to audit_labels.csv, so you can stop
and come back later. The goal is at least 80% agreement on 100 labels.
"""
import argparse
import csv
import os
from collections import Counter

import cv2

import settings
from memory import Memory

LABELS_CSV = "audit_labels.csv"


def load_done():
    if not os.path.exists(LABELS_CSV):
        return {}
    with open(LABELS_CSV, newline="") as f:
        return {int(r["observation_id"]): r for r in csv.DictReader(f)}


def report(done):
    rows = [r for r in done.values() if r["robot"] != settings.UNCERTAIN]
    if not rows:
        print("No labeled observations yet.")
        return
    agree = sum(r["robot"] == r["you"] for r in rows)
    n_unc = sum(r["robot"] == settings.UNCERTAIN for r in done.values())
    print(f"\nAgreement: {agree}/{len(rows)} = {100 * agree / len(rows):.0f}% "
          f"(goal: 80% on 100 labels). {n_unc} were stored as uncertain.")
    misses = Counter((r["you"], r["robot"]) for r in rows if r["robot"] != r["you"])
    if misses:
        print("Most common mix-ups (you -> robot):")
        for (you, robot), n in misses.most_common(5):
            print(f"  {you} -> {robot}: {n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=settings.DB_PATH)
    ap.add_argument("--delete-after", action="store_true")
    args = ap.parse_args()

    mem = Memory(args.db)
    done = load_done()
    todo = mem.db.execute(
        "SELECT id, activity, activity_raw, activity_p, audit_path, local_time"
        " FROM observations WHERE audit_path IS NOT NULL ORDER BY ts").fetchall()
    todo = [r for r in todo if r["id"] not in done and os.path.exists(r["audit_path"])]
    labels = [label for label, _ in settings.ACTIVITIES]
    print(f"{len(todo)} thumbnails to label. Keys:")
    for i, label in enumerate(labels, 1):
        print(f"  {i} = {label}")
    print("  s = skip, q = quit")

    new_file = not os.path.exists(LABELS_CSV)
    with open(LABELS_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["observation_id", "robot", "robot_raw",
                                          "robot_p", "you"])
        if new_file:
            w.writeheader()
        for r in todo:
            img = cv2.imread(r["audit_path"])
            cv2.imshow("audit (press a number)", img)
            print(f"\n{r['local_time']}  robot said: {r['activity']}")
            choice = None
            while choice is None:
                key = cv2.waitKey(0) & 0xFF
                if key == ord("q"):
                    cv2.destroyAllWindows()
                    report(load_done())
                    return
                if key == ord("s"):
                    choice = "skip"
                elif ord("1") <= key < ord("1") + len(labels):
                    choice = labels[key - ord("1")]
            if choice == "skip":
                continue
            row = {"observation_id": r["id"], "robot": r["activity"],
                   "robot_raw": r["activity_raw"], "robot_p": r["activity_p"],
                   "you": choice}
            w.writerow(row)
            f.flush()
            done[r["id"]] = row
            print(f"  you: {choice}")
            if args.delete_after:
                os.remove(r["audit_path"])
    cv2.destroyAllWindows()
    report(done)


if __name__ == "__main__":
    main()
