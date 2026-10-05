# HANDOFF — current state and the next action

Read this first, then `CLAUDE.md`, then `BUILD_SPEC.md`.
Updated 2026-10-05.

## Where the build is

| Milestone | State |
|---|---|
| M0 remote ops | **done.** Wake, sleep, deploy, run-as-task, GPU and camera all verified over SSH. Evidence in `remote-notes.md`. |
| M1 motion foundation | **in progress.** Firmware v2, motion engine, poses, gestures and 36 Mac-side tests are done. Dashboard and the live hardware run are not. |
| M2–M7 | not started. |

## The next action

**Build the dashboard** (`avi_bot/dashboard/server.py`, BUILD_SPEC 14): a web page
on port 8000 over Tailscale showing the sim view, joint sliders, gesture
buttons, and pause/guest/kill. It is the last piece of M1 that needs no
hardware, and it is what lets Shaurya *watch* a gesture before it is ever sent
to the arm.

After that, M1 needs one thing from Shaurya that cannot be worked around: the
first live hardware run, with him watching. See "Owed by Shaurya".

## What is running right now

A motion-gated observation logger, from the **old** layout, deployed at
`C:\Users\MSI-User\avi-remote\assistant\` on the MSI and started through
`scripts/tracker.sh`. It writes to `desk_memory.db` with audit thumbnails at
5 %. Its keep-awake was bounded to 120 minutes, so the MSI put itself to sleep
after it.

It is deliberately **not** migrated into `avi_bot/perception/` yet: BUILD_SPEC
folds the observer in at M3, it is gathering the data M6 needs two weeks of,
and moving a running experiment to make a directory tidier is how you lose
data. `scripts/deploy.sh` still targets `avi-remote\`; BUILD_SPEC 13 wants
`avi-desk\`. Migrate both together at M3, on a fresh wake, with the tracker
stopped.

## Owed by Shaurya (blocking, in order)

1. **The 7 stale rows.** `desk_memory.db` holds 7 observations labelled
   `not at desk`, a label that no longer exists in `ACTIVITIES`. `audit.py`
   maps keypresses to the *current* label set, so those rows can never match
   anything he presses and would count as automatic disagreement against the
   80 % goal. They are from my own test runs on the evening of 2026-10-04 and
   have no research value. Needs his go-ahead to delete:
   `cd scripts && ./observer.sh today --forget` (wipes all of that day,
   including the 4 good post-fix rows, which is the cleaner option: it leaves
   one consistent label schema from the start).
2. **The first live motion run.** Nothing in M1 touches the arm without his
   explicit go-ahead. When he is ready: 15° maximum, slowest cap, and he should
   watch it (in person or on a video call). The bench check to do first is the
   PCA9685 register read-back — see `firmware/avi_servo_v2/avi_servo_v2.ino`.
3. **Weekend desk hours.** Weekdays 14:30–22:00, Tuesdays 20:00–22:00 are
   known. Needed for the wake timers and the observer schedule.
4. **The audit UI decision.** `audit.py` needs a GUI window (`cv2.imshow`), so
   it cannot run over SSH. Either a browser labeller served from the MSI
   (frames never leave the machine, more work) or copy thumbnails to the Mac
   (simpler, but frames leave the machine — his call, the camera faces his
   room).
5. **Away-from-home wake.** A Raspberry Pi on the tailnet, or a router wake
   button? Wake-on-LAN only works while this Mac is on the home LAN.

## Open questions I cannot answer from here

- **Can the arm actually do 60 °/s?** `config/joints.toml` says 60, BUILD_SPEC
  says "about 60", and the *v1 sketch* caps at about 37. Every gesture duration
  is derived from this number, so if the real figure is 37 the whole gesture
  table stretches by 1.6x and several gestures stop reading as themselves.
  Measure it on the first live run.
- **Elbow and wrist direction signs** are unknown, so `safety.Limiter` pins
  both to center and `nod` does nothing at all on hardware. Measure with a
  +5° move and optical flow (BUILD_SPEC 14), then set `sign` and `driven` in
  `config/joints.toml`.
- **Does the PCA9685 keep its output registers across an Uno reset?** The
  whole no-jump-on-startup design assumes yes. Unverified; bench test.

## Things not to re-solve

`CLAUDE.md`'s "Measured reality" section lists these with the evidence. The
short version: sleep needs the detached-and-quiet-and-retry dance, long jobs
need scheduled tasks, always pass `python -u`, the C270's first frames are
black, never diagnose a frozen camera from downscaled frames, and `ACTIVITIES`
is frozen because it is the pre-registered metric.
