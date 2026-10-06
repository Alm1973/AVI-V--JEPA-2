# HANDOFF — state, decisions, and what to do next

Read this first, then `CLAUDE.md`, then `BUILD_SPEC.md`.
Updated 2026-10-05, end of the first hardware day.

---

## 1. Where the build is

| Milestone | State |
|---|---|
| **M0** remote ops | **done.** Wake, sleep, deploy, run-as-task, GPU, camera, all over SSH. |
| **M1** motion foundation | **done.** Firmware v2 flashed, all 4 joints driven, 6 poses taught on the arm, all 13 gestures verified moving on hardware, dashboard folded. |
| **M2** personality core | **started.** Mood engine written and tested. Attention, behaviour selection and beeps not started. |
| M3–M7 | not started. |

Everything is reachable from the Mac. Nobody has had to sit at the MSI since
the one-time SSH bootstrap — **including flashing the Arduino**, which goes
through the `arduino-cli` bundled inside Arduino IDE 2.x.

## 2. How to run it

```bash
cd ~/avi-desk
./runHome                 # the dashboard: camera, sliders, gestures, poses
./runHome teach 60        # pose teaching only
./runHome pull            # copy taught poses back to the Mac
./runHome drive           # teleop's WASD driving (superseded by the dashboard)
./runHome stop
./scripts/test.sh         # the whole suite, on the Mac, no GPU or hardware
```

Ports: dashboard **8000**, teleop 8081, calibrate 8082, stream 8080. Only one
process can hold the camera or the serial port, so each script stops the others
first.

## 3. Hard-won facts — do not re-derive these

**The arm manages about 27 °/s, not the 60 the project assumed.** Measured by
raising the firmware cap in steps and timing a 60° sweep optically: it plateaus,
and a commanded 140 is no faster than a commanded 60. That is ~10× slower than
these servos' datasheet, which points at the **UBEC current-limiting them**. The
5 V 4 A supply is on order. `config/joints.toml` keeps two separate numbers for
this: `speed_cap_deg_s` (60) is a safety clamp on what may be *commanded*;
`achievable_deg_s` (27) is a physical fact, and gesture durations derive from it.

**Consequence:** `shake` commands 4.5°, `nod` 3.6°, `startled` 3.2°,
`excited_wiggle` 2.7° — about 45 % of intended, which is what fits 27 °/s while
keeping their rhythm. They are twitches. **One edit to `achievable_deg_s` after
the new supply returns all of it, with no code change.**

**Easing curves cost peak speed, and that cost is the real constraint.**
Measured multipliers over the average rate: `linear` 1.00, `ease_in_out` 1.571
(exactly π/2), `ease_in`/`ease_out` 3.00, every `*_back` curve **4.70**. A
`*_back` curve across the 90°+ between taught poses is what made `yawn_wake`
take 16 s. Long travel now uses `ease_in_out` and the overshoot is a separate
fixed-size leg.

**Directions, measured and settled:**
- `base_yaw` sign is **−1** in config, but see §4 — it is the one direction not
  properly verified.
- **Lower shoulder degrees raise the arm** (ATTENTIVE 29.5 raised, REST 150
  folded).
- **Higher wrist degrees tilt the camera UP** (REST, camera-down, is 30.5).
  I concluded the opposite from a two-position sweep, said so twice, and was
  wrong; the taught poses settled it.
- Every direction claim in `BUILD_SPEC` §3 was measured through v1's relative
  `X:/Y:` API and **does not carry over** to v2's absolute map. Treat them all
  as unverified.

**The camera at mechanical centre points at the carpet.** So `OBSERVE` is
deliberately not the centre pose, and `poses.py` no longer asserts that it is.

**Sleep is the hard direction and it is solved.** Use `scripts/sleep-msi.sh`
and do not simplify it: the suspend must be detached via `Start-Process`, the
wait must be *quiet* (probing port 22 wakes the machine), and `SetSuspendState`
silently no-ops often enough to need the retry.

**Windows kills session-owned processes when SSH disconnects.** Anything
long-running goes through `scripts/remote/task-ctl.ps1` as a scheduled task.
Always pass `python -u`, or the log stays empty and a healthy run looks hung.

**Wake timers:** installed, two-hourly 08:05–22:05 plus desk-hour starts. They
**do not fire on battery** (`allow-wake-timers` is AC=1 DC=0) — keep the MSI
plugged in or remote access silently stops. `powercfg /waketimers` is the only
real evidence a wake is armed; a task's own `WakeToRun` flag is not.

**Deploying files to Windows has three traps**, all of which reported success
while transferring nothing. `scripts/push-file.sh` handles all three and
verifies the byte count afterwards:
1. base64 as one command-line argument — Windows caps a command line at 32767
   characters, so files over ~24 KB silently stopped transferring.
2. piping base64 over ssh stdin — hangs, because Windows OpenSSH does not
   propagate EOF to PowerShell.
3. `Remove-Item` on a missing path returns a **failing** exit status over ssh
   even with `-ErrorAction SilentlyContinue`.

Plus: empty files (`__init__.py`) produce no chunks and need writing directly,
and `ssh` inside a `while read` loop eats the loop's stdin — use `ssh -n`.

**A serial handle can go bad while the device is fine.** A write raised
`WriteFile failed (PermissionError(13))` while Windows still listed "Arduino
Uno (COM5)" as Status OK. `SerialBackend.send` now reconnects on a backoff, and
reconnecting refuses to open a port that was never opened deliberately —
recovering a connection is not the same as making one.

**`PAGE` is a Python bytes literal**, so a `\n` written for JavaScript becomes a
real newline in the served HTML and breaks the string it was inside. A dead page
looks exactly like healthy software from the server. `tests/test_page_js.py`
parses every served page with `node --check` and verifies every
`getElementById` resolves.

**Pages must send `Cache-Control: no-store`.** A cached page is
indistinguishable from a broken one. Every page now shows a build hash that the
server also logs; if they differ, the browser is stale.

**Never diagnose a frozen camera from the downscaled change-detector frames** —
they sit at max-diff 1–2 on a live stream. Compare raw 1280×720.

**The C270's first ~5 frames are black with `ok=True`.** `open_camera()` loops
until `frame.mean() >= 1.0`; keep that.

## 4. Open questions

**`base_yaw`'s sign is not verified, and `look_at` must not ship until it is.**
`look_at` is a feedback loop: with the sign backwards it accelerates away from
whatever it is tracking instead of settling. His calibration table says count
460 is far right (⇒ sign +1); driving the arm he reported A and D reversed
(⇒ sign −1, which is what is set). The likely cause is that "right" means
opposite things in the two observations — the arm's own right versus the
direction the scene slides in a wrist-mounted camera's picture.

**Three visual attempts to settle it all failed, and the method was the
problem, not the scene.** Phase correlation returned peak responses of 0.00–0.07
(no peak at all) and gave two opposite commands the same sign. Dense Farneback
flow disagreed with itself by 17–78 px between repeats of the same move. But
**template matching with the arm still shows only 4 px of noise at match scores
of 0.98**, against ~46 px for a 10° wrist move. So the next attempt should use
`cv2.matchTemplate`, not flow, and `assistant/direction_check.py` should be
rewritten onto it.

**Test the loop, not the geometry.** `look_at` only cares whether its
correction converges. Put a person or object in frame, run the closed loop, and
watch whether the error shrinks or grows; if it grows, flip the sign. That
cannot be confused by which frame "right" refers to, and it tests the thing
that matters instead of a proxy. Needs M3's detector — which already exists on
the MSI as part of the logger, so **if Shaurya is at the desk, one run of YOLO
person detection plus a few degrees of yaw settles it today.**

Also open:
- Can the arm beat 27 °/s with the new supply? All gesture sizing depends on it.
- `audit.py` needs a GUI window (`cv2.imshow`) so it cannot run over SSH. A
  remote audit path is still owed: browser labeller on the MSI (frames never
  leave) versus copying thumbnails to the Mac — his call, the camera faces his
  room.
- Weekend desk hours, for the observer schedule and wake timers.

## 5. Owed by Shaurya

1. **Install the 5 V 4 A servo supply** when it arrives, then say so — biggest
   single win available.
2. **Keep the MSI plugged in.** Wake timers do not fire on battery.
3. **Weekend desk hours.**
4. **The 7 stale rows.** `desk_memory.db` holds 7 observations labelled
   `not at desk`, a label that no longer exists, so they can never match
   anything in an audit and would drag the 80 % figure down for no reason.
   They are test rows with no research value:
   `cd ~/avi-desk/scripts && ./observer.sh today --forget`
5. **Audit UI decision** (see §4).
6. **An always-on home device with Tailscale** would give on-demand wake and
   close the 22:15–08:05 blackout. Any old phone, Pi or desktop.

## 6. What is running, and what is not

Nothing runs by default. The observation logger lives at
`C:\Users\MSI-User\avi\assistant\` on the MSI — inside the `avi\` repo,
where `deploy.sh` puts it so it can import the GPU modules beside it. Only the
helper scripts live in `avi-remote\`. **BUILD_SPEC §13 wants the whole thing in
`avi-desk\` instead; do that move and the `avi_bot/perception/` reorganisation
together at M3, with the tracker stopped.**

## 7. Repo layout

```
avi-desk/
  CLAUDE.md  BUILD_SPEC.md  runHome  .env.example
  config/     joints.toml  poses.toml
  avi_bot/
    motion/      easing poses safety backends engine gestures
    personality/ mood
    dashboard/   server        (the dashboard; port 8000)
  assistant/   observer room_tracker calibrate teleop camera_stream
               gesture_check direction_check dashboard(shim) + the logger
  firmware/avi_servo_v2/avi_servo_v2.ino
  scripts/     wake-msi sleep-msi keepawake-msi wake-timers deploy push-file
               dashboard calibrate teleop stream tracker observer msi-run test
  tests/       the whole suite, none of it needing hardware
  docs/        HANDOFF.md remote-notes.md taught_poses.json
```

`docs/remote-notes.md` is the measured evidence log, in date order, with the
numbers behind every claim in §3.
