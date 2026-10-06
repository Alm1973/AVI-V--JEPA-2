# HANDOFF — state, decisions, and what to do next

Read this first, then `CLAUDE.md`, then `BUILD_SPEC.md`.
Updated 2026-10-05, end of the first hardware day.

---

## 1. Where the build is

| Milestone | State |
|---|---|
| **M0** remote ops | **done.** Wake, sleep, deploy, run-as-task, GPU, camera, all over SSH. |
| **M1** motion foundation | **done.** Firmware v2 flashed, all 4 joints driven, 6 poses taught on the arm, all 13 gestures verified moving on hardware, dashboard folded. |
| **M2** personality core | **done.** Mood, idle behaviour selection, attention and the chirp synthesizer, all wired into the dashboard and watched idling on the real arm. M2's acceptance test passes; 22 chirp WAVs are in `docs/beeps/`. No speaker is installed yet, so the dashboard reports which chirp *would* play. |
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

On the dashboard: mood bars and the live emotion, buttons for all nine mood
events, and an **idle behaviour** toggle. Turning it on makes AVI choose its own
behaviours; click `arrived` first, or it correctly assumes nobody is there and
only sits still.
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
- `base_yaw` sign is **−1**, and that is now **confirmed** by closing the loop
  on a tracked person: starting 0.247 of a frame off centre, the correction
  took the error through zero rather than further out. `look_at` can ship on it.
- **About 29 commanded degrees of yaw move the image one frame width** —
  measured, against the ~52° the C270's datasheet implies. Use this for
  look-at gains; using 60 made the loop apply twice the intended gain and it
  oscillated instead of settling. It lives in `config/joints.toml`'s
  `[camera]`. The **wrist**'s equivalent is still unmeasured and set
  conservatively at 60 — run the same closed-loop check vertically.
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

### The wrist is 32.2°/frame, not 60 — and higher degrees look UP

Measured 2026-10-05 by `POST /probe_wrist`, off a detected laptop rather than
optical flow: wrist 125 → 143° moved it 95 → 494 px down a 720 px frame,
slope 22.33 px/deg, **r² = 0.986**. 720 / 22.33 = **32.2°/frame**.

The old 60.0 was a guess and nearly **double**. With gain 0.45 the vertical
loop was correcting 0.84 of the real error per step, not 0.45 — close to
hunting rather than slow. (I had this backwards earlier in the session and
said the guess made vertical tracking *slow*; it made it twitchy.)

**Direction, settled three ways:** higher wrist degrees tilt the view **UP**.
The taught poses (REST, asleep, camera down = 30.5; OBSERVE = 136; TALL = 176),
Shaurya asking for the *max* raised to 170+ so it could look up, and this
probe. `config/joints.toml` carried a comment claiming the opposite — it is
fixed. **Say which thing moves:** the landmark moves *down*, the view goes
*up*. Conflating those is how the direction got stated backwards twice.

### yaw_deg_per_frame is 83.9, not 29.0 — this was the tracking bug

Measured by `POST /probe_yaw`: base_yaw stepped to five absolute positions,
80 → 100°, moved a detected laptop 702 → 407 px across a 1280 px frame.
Slope −15.26 px/deg, **r² = 0.976**. 1280 / 15.26 = **83.9**.

**The old 29.0 was wrong by 2.9×, and it was not a sloppy reading.** It came
from dividing a *commanded* correction by the image shift that followed it
(7.4° → 0.248 of a frame; 8.4° → 0.323). Both readings were taken while the
loop was oscillating — which is exactly when `steer()` was firing ~20× per
detection, so the arm was moving far more than the single correction being
credited with the shift. Divide a real shift by a fraction of the motion that
caused it and the answer comes out small, by very nearly 2.9×. It was also
measured off a **person**, who moves. The new one uses a laptop and no
feedback loop at all.

**Consequence:** the loop was commanding 2.9× too little yaw, i.e. correcting
~16% of the error per measurement instead of 45%. From the outside that looks
precisely like "the tracker isn't converging", which is what we were staring
at. `tests/test_attention.py` now has
`test_a_mismatched_constant_is_what_actually_hurts` covering both failure
modes: too small crawls, too large hunts.

**`MAX_TARGET_DEG` had to go 30 → 50.** Centring a box at the frame edge now
needs 0.5 × 83.9 = 42° of accumulated target, so the old bound would have
pinned the loop short of the edges of its own picture.

**Why 83.9 is the believable one:** the C270 is ~52° horizontal at 16:9, so
52/83.9 means base_yaw travels about **62%** of the degrees it is commanded.
The wrist, measured the same way, is 32.2 against a ~31° vertical FOV — very
nearly 1:1. A base that under-travels and a wrist that doesn't is physically
sensible: the base carries the whole arm and this arm is current-limited.
29.0 would have required the base to *over*-travel by 1.8×, which nothing
explains.

Leading explanation, **not verified**: the full mechanical sweep (counts
75–460, found by driving to each stop) was labelled 0–180 logical degrees but
may really be ~290°; 180/290 = 0.62. If so these are honest logical degrees
and nothing is broken — but do not reuse them as real angles. To settle it,
measure a known real angle (two marks a known distance apart at a known
range) rather than trusting the lens spec.

### The convergence tests could never have caught this

`run_loop()` applies each correction instantly, so the constant appears in the
correction and in the response and **cancels exactly**. The suite was green
with a value out by 2.9×, and would be green with 500. Two tests now pin this
down: `test_the_ideal_loop_cannot_check_the_constant` records the blind spot,
and `test_it_settles_when_the_arm_lags_behind` models the rate-limited arm
(27°/s against 0.4 s detections) where accumulated-target windup actually
lives. Gain 0.45 is stable there with the corrected constant — checked, not
assumed.

### Person detection: confidence cannot do this job, parallax can

Two overlapping ranges, measured 2026-10-05: **Shaurya 0.75–0.89** and **the
arm's own hardware in shot 0.56–0.72**. A floor between them is a coin toss —
at 0.70 the tracker locked onto itself and saturated at ±30°, at 0.75 it
reported 0 hits in 84 frames. Do not go back to tuning the floor.

The separation that works is geometric and is worth a factor of thirty. The
camera is bolted to the arm, so anything else bolted to the arm sits at a
**fixed image position** no matter how far the arm swings; a real person slides
by `Δyaw / yaw_deg_per_frame` of the frame width. The log that proved it:
`base_yaw` swung 40.4 → 56.9° (0.59 of a frame, ~750 px at 1280) while the
suspect box stayed at x = 1023…1072.

`avi_bot/perception/parallax.py` implements it. Two things to know before
changing it:

- The static test needs the partner sighting at the **same image position with
  the largest yaw difference**; the moving test needs the partner **nearest in
  time**. An early version used the first match for both and learned nothing
  off 1° pans.
- Only **magnitudes** are compared, never directions. "Did it move" is the
  whole question.

`POST /probe_static` asks the question on purpose rather than waiting for a pan,
because the failure it prevents is precisely the one where the arm has
saturated and *stopped* panning.

### "0 hits in N frames" is usually an empty room

`detect_debug` exists because that counter has two completely different causes
and no amount of staring at it tells them apart. On 2026-10-05 at 21:25,
`track_misses` was 1686 with zero hits and the detector was **working
perfectly**: laptop 0.45, mug 0.24, headphones 0.09, best person 0.134. Nobody
was there. Check `POST /detect_debug` before touching a threshold.

It returns **labels and numbers only, never pixels** — the camera faces
Shaurya's room, so the frame stays on the MSI.

## 4. Open questions

**Resolved today:** `base_yaw`'s sign, by closing the loop on a tracked person
rather than measuring geometry. See `remote-notes.md` for the four scene-feature
methods that failed first and why none of them should be tried again.

Also open:
- Can the arm beat 27 °/s with the new supply? All gesture sizing depends on it.
- `audit.py` needs a GUI window (`cv2.imshow`) so it cannot run over SSH. A
  remote audit path is still owed: browser labeller on the MSI (frames never
  leave) versus copying thumbnails to the Mac — his call, the camera faces his
  room.
- Weekend desk hours, for the observer schedule and wake timers.
- Is the ~29°/frame figure a narrow field of view or the yaw under-travelling
  what it is commanded? **Probably under-travel, ~1.8×** — see the wrist
  measurement above, which acts as the control. Not settled: it rests on a
  datasheet FOV. To close it, measure a known real angle (two marks a known
  distance apart at a known range) rather than trusting the lens spec.

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
