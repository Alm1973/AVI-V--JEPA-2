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

### M4 is done (2026-10-05)

`avi_bot/brain/` — router, intents, providers, tools, schema, speech, context.
All three acceptance criteria met: **20/20** scripted utterances (bar was 18),
provider failover plus a *pre-send* budget guard, and the speech policy applied
once to every route.

- **Kokoro cannot be installed on the MSI.** Its venv is Python **3.13.13** and
  Kokoro needs < 3.13. A version wall, not a speed problem.
- **Piper is the primary and it is fast: RTF 0.036–0.039** (en_US-amy-medium,
  22050 Hz, CPU, 4.3 s one-off load). 26× real time, so a 20-word reply
  synthesises in under a quarter of a second. Nothing is lost.
- `providers.redact()` strips keys and the calendar URL in the one place the
  prompt is assembled. **The secret iCal address IS the credential** — a
  provider that logs it has read access to his calendar indefinitely.
- `context.build()` takes typed arguments with **no free-form parameter**, on
  purpose: that is how a frame gets smuggled into a cloud request.

Everything is injected (the network call into `Chain`, the 3B pick into
`Router`), so the whole decision layer tests with no keys, no network, no GPU
and no arm.

### Two things the research changed — read `docs/RESEARCH-NOTES.md`

1. **The focus nudge was specified wrong.** "Phone use over 2 min → ahem" is a
   time-based trigger, which the interruption literature finds unreliable; a
   mistimed nudge trains him to ignore AVI entirely. Now gated on a
   **breakpoint** (`change.py` already detects them). BUILD_SPEC 9 amended.
2. **The novelty effect wears off in ~2 months.** Everything charming about M2
   has a shelf life. The honest test of this project is whether utterances,
   suggestion-acceptance and awake-time are flat or rising at **week 10**.

### M5 is done, and so is the assembly (2026-10-06)

**486 tests**, all Mac-side, no hardware. `Registry.missing()` is empty — every
one of the 27 tools in the schema has an implementation.

- `avi_bot/main.py` assembles the whole robot **headless**. If that ever stops
  being true, remote development stops working.
- `avi_bot/scheduler.py` is the only part that speaks unprompted: one
  announcement at a time, nothing during quiet/sleep/guest, stale items
  dropped rather than delivered late, and nothing retired until delivery is
  confirmed.
- `avi_bot/hearing/listener.py` has the echo gate (deaf while speaking + 300 ms)
  with everything else injected, since the reSpeaker lands Thursday.
- 10 TTS-generated WAV fixtures in `fixtures/audio/`. The 63 MB voice model is
  gitignored; `models/` holds it.

**Three bugs found by tests rather than by reading**, all worth knowing:

1. **`"a"` was in the word-number table**, so *"set **a** timer for 5 minutes"*
   parsed `a` as the duration and "5 minutes" as the *label* — a one-minute
   timer, confidently.
2. **Transcripts spell numbers out.** *"Focus for twenty five minutes"* didn't
   parse, so the spoken path silently lost the duration while the typed path
   worked. `avi_bot/numbers.py` now handles compounds; it refuses "five twenty"
   (a time) and "twenty twenty" (a year).
3. **Calendar warnings dedup *on read*** while `Scheduler.due()` is polled by
   design — so any warning that wasn't the soonest item in a tick was consumed
   and never spoken. Calendar announcements are buffered now; timers and
   reminders don't need it because they live in a table.

### Voice: Piper, not Kokoro

`RTF 0.036–0.039` measured on the MSI (en_US-amy-medium, 22050 Hz, CPU). 26×
real time. Kokoro **cannot be installed** — it needs Python < 3.13 and the
MSI venv is 3.13.13. Nothing is lost.

### State at 2026-10-06 morning — 540 tests, M0–M5 done

**M3 closed on hardware.** The yaw-sign fix verified on real detections of
him: yaw 70 → 76.5, error **−1.0° to +0.5°** (bar is ±5°), **111/111 hits**
at conf 0.82–0.90, 3 s to centre, no oscillation.

**Route 2 (cloud) works live.** 6/6 open questions through Groq, 0 failures,
363 ms–1.0 s, with `set_reminder` and `look_at` called from plain speech.

**The dashboard now does everything at once** — tracking, V-JEPA latents,
idle behaviours, one camera. Measured 5.38 GB of 6.44 free with YOLO
resident, and V-JEPA needs 0.74, so the turn-taking I had been doing all
morning was never necessary. Encode is *faster* folded in (369 ms vs ~600),
because the clip comes from the already-decoded stream.

**`/pov`** is a tiny status page: camera view plus the eight numbers that say
whether the robot is alive.

### The false positives were a low-light artifact

| Pose | Dark, empty | Daylight, empty |
|---|---|---|
| HOME | 3 boxes, **0.61–0.68** | 0 |
| ATTENTIVE | **0.678** | 0 |
| TALL | **0.696, 0.606** | 0.314 |

Yesterday's whole confidence crisis — a 0.75 floor finding nothing in 84
frames, a 0.70 floor locking onto the arm — was **substantially about
darkness**. In daylight there is a clean gap between 0.31 and his 0.82. So the
trust threshold now follows measured brightness: **0.65 lit / 0.75 dim / 0.85
dark**, strictest when brightness is unknown.

### Two things I got wrong and corrected

1. **"OBSERVE points at a blank wall."** It does not. That was sd 16 in an
   unlit room at 23:00 — I was reading the darkness, not the framing. In
   daylight it saw the desk fine. It *was* badly aimed, which is a smaller and
   different fault; retaught arithmetically (yaw 90→60, wrist 136→147.9) and
   the laptop went from the top-left corner to near centre.
2. **Parallax cannot be trusted on people.** It assumes the target holds still
   during the pan. He shifted in his seat and picked up a spurious "attached"
   vote; two would have rejected him. Tracking works today because UNKNOWN
   falls back to confidence, not because parallax carries it.

### Blocked on him

- **Tailscale HTTPS Certificates** (admin console → DNS) — without it an
  Artifact cannot show the stream, since a browser blocks http:// inside an
  https:// page. Frames would still go MSI → browser directly, never through
  claude.ai.
- **A daytime latent run with him present.** The overnight probe was
  *inconclusive*: `d = 1.367` looked like a pass but the window contained
  sunrise, and the only brightness bands showing structure were the two
  widest in brightness. Needs steady light and a moving room.

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

## 2026-10-06, late morning (remote, from school)

### What the admin console problem was
Not the Tailscale account. The school network resets connections to
`login.tailscale.com` specifically -- `github.com` returns 200 from the same
Mac -- and Tailscale's own health check says it cannot reach the coordination
server. The tailnet keeps working because the MSI link was already established
as a direct connection to the home IP before this network, so the data plane
is up while the control plane is blocked. **Enable HTTPS Certificates from a
phone with wifi off, or from home.** It cannot be done from school.

### Deploy now goes over sftp (~100x faster)
The base64-through-PowerShell transport sent ~6000 characters per SSH
connection, about 14 s per file, and today it wedged completely: 13 files in
three minutes, then nine minutes stalled mid-file. Earlier in the session the
same transport produced `chunk 1 failed at offset 6000`.

`scripts/sync.sh` replaces it: **78 files in 11 s, 78/78 byte-for-byte.** SFTP
is a separate subsystem of the same sshd, so the whole bash -> ssh ->
PowerShell quoting chain disappears, along with the 32767-character
command-line limit. Neither of push-file.sh's two recorded dead ends applies to
it, and home-relative paths keep the space in "MSI-User" out of every
filename. Verification does not trust the transport's exit status: the manifest
of expected sizes goes over the same connection and `sizecheck.ps1` compares it
on the MSI through `-File`.

`scripts/deploy.sh` still exists and still works; prefer sync.sh.

### Three real bugs found by reading code rather than running it

**The room map forgot the room every night.** Solving the decay for the purge
threshold: `weight * 0.5**(age/6h) >= 0.35` means lifetime grows as
`log2(weight)`, so every doubling of the evidence bought a flat six hours and
nothing could ever become permanent. Worse, weight counted frames, so it
measured how long the camera stared rather than how reliably a thing is there
-- a mug in view ten minutes outscored a monitor seen on thirty separate days.
Durability is now earned by SPACING: each visit more than `VISIT_GAP_S` apart
multiplies the half-life. Glimpsed once, it fades by the next evening; in the
same spot on five separate days, it survives a month away. `MAX_WEIGHT = 20`
caps the position-average mass, which also fixes a thing seen a thousand times
being unable to be seen to MOVE.

**find() never once looked at what it found.** It read a `cx` column that
production does not have (sightings store x/y/w/h as fractions), so the yaw was
always None and `main._find` only turns the arm when it is not None. Behind
that, `centre_yaw` defaulted to 90 while the logger only records from OBSERVE,
which was retaught to 60 -- so fixing the first bug alone would have put every
answer a third of a frame out. All of it survived because the test fixture
declared its own sightings table WITH a cx column; the fixture now reads the
real SCHEMA out of `assistant/memory.py` with ast.

**Avi ran on two clocks.** The calendar was built with no `clock=`, because
every other clock in AVI returns epoch seconds and Calendar's wanted an aware
datetime. So it read real time while timers, focus and modes read the injected
clock. `test_a_due_timer_surfaces_through_tick` failed for one minute a day
(the ten minutes before the fixture's 10:00 Chemistry), and
`test_the_schedule_comes_from_the_fixture` was passing *because* of the bug --
the fixture is hard-dated 2026-10-06 and so is today. It would have failed on
any other day of the year.

### The survey is instrumented, not yet tuned
"2 sightings across 6 stops" is the same output whether the detector faced an
empty wall, saw the desk at 0.25 and lost it to the 0.30 floor, or saw only a
person. Detection now runs down to `REPORT_FLOOR = 0.10` and `/survey` returns
`per_stop` with what each stop kept, what fell below the floor, and how many
people it skipped. Tune the grid after reading that, not before.

### State right now
- **567 tests pass** on the Mac, no hardware.
- Dashboard up ~7.4 h remaining on its task; **latents recording** into
  `latents-dev.db` (433 ms/encode, no errors). Tracking and idle are OFF.
- `map_places: 0` -- the map is empty and will stay so until a survey runs
  under the new code.
- **The dashboard has NOT been restarted**, so it is still running the old
  code. Restarting reopens COM5, which resets the Arduino and drives both
  servos to centre, and nobody is at the desk. It needs Shaurya present.
- The room map now follows the launch-day rule: `roommap-dev.json` by default,
  `roommap.json` only when asked. `tests/test_launch_profiles.py` pins that
  rule for both the map and the latents, which until now rested on two
  unexamined default strings in unrelated files with nothing checking either.

### Caveat on today's latent run
The room is empty and static while he is at school, so high temporal coherence
would be trivially achievable and the `static_scene` guard should flag it. What
today yields is a **brightness-varied, no-activity baseline** -- useful as the
null, not as a positive result. The real probe needs him at the desk.

## 2026-10-06 midday: 3D, and a correction to a hardware "fact"

POV stream and the Tailscale cert chase are **dropped** at his request -- the
video feed was not worth the trouble. Focus is 3D and spatial awareness.

### Depth is possible from one camera, because the camera is not on the axis
`avi_bot/perception/depth.py`. When base_yaw turns, the camera rotates AND
swings along an arc of radius r (its horizontal offset from the base axis).
That arc is a stereo baseline whose length is known from the joint angles.

    da/dth = -1 - (r/rho) cos(eta)

The -1 is rotation, identical for everything, no depth. The second term is the
signal. Inverted: rho = r cos(eta) / (s/g - 1).

**Identifiability, counted honestly:** n measurements, n+1 unknowns. Under-
determined by one for ANY number of tracks. I first claimed the rate ratio
cancels g and gives relative depth free; that is WRONG, and a test pins it --
a true ratio of 1.75 reads 1.51 / 1.73 / 2.51 for g = 0.55 / 0.62 / 0.70.
What IS free is the **ordering** (`depth.ordering()`): s is monotonic in
1/rho for any positive g. Metric needs one external fact plus r.

Assuming the slowest track is at infinity biases ranges **LONG** -- with
nothing past 0.70 m in view, 0.40 m reads as 1.20 m.

### yaw_deg_per_frame = 83.9 may not be a joint constant
The wrist axis passes through the camera (r~0, so s = g ~ 1, matching 32.2 vs
a 31 deg vertical FOV). The base axis is an arm away, so parallax is large.
The asymmetry the config blames on under-travel is exactly what parallax
predicts with NO under-travel: s = 1 - r/rho = 0.62 puts the calibration
landmark at rho ~ 0.47 m, a desk distance.

**POST /probe_depth decides it in one pan.** Equal rates across the scene =>
under-travel. Rates differing by tens of per cent => parallax, and 83.9 is
distance-dependent. Tracking is unaffected either way (it corrects observed
error), so this is mistuning, not breakage.

### Does V-JEPA encode depth? Now testable
`avi_bot/perception/depth_probe.py`. Measured ranges are labels; fit a LINEAR
head to log-range against a permutation null. The fold design is the whole
thing: latents 15 s apart are 0.994 cosine-similar, so random k-fold leaks.
Measured on two INDEPENDENT random walks with no shared information:

    random folds      R2 = +0.837   <- would have been called a discovery
    contiguous folds  R2 = +0.064   -> "no better than chance"

Validated both ways: a depth-carrying representation gives R2 0.998 against a
null max of 0.022 (z 85.8); 1024 noise dims against 240 samples does not beat
chance.

### 3D RoPE, for the record
V-JEPA 2 already uses it, but its "3D" is (time, height, width) -- tensor
indices, not depth. It gives no spatial awareness in the sense wanted. The
real opening is that V-JEPA 2-AC applies temporal RoPE to **action and pose
tokens**: AVI's joint angles are exactly that input, so pose conditioning is a
first-class interface rather than a hack. Adding a pose axis to the RoPE split
itself needs fine-tuning (R820 work), since the pretrained weights expect
thirds.

### Owed by Shaurya, now on the critical path
1. **camera_radius_m** -- horizontal distance, base rotation axis to lens, at
   OBSERVE. One ruler measurement. Without it depth is relative only.
2. **camera_faces** -- does the lens look outward from the base axis, or back
   across the desk? Sets the parallax sign. Just look at it.
3. Then set `[kinematics] measured = true` in config/joints.toml.

### State
- **616 tests pass**, Mac-side, no hardware. Deployed 80/80 byte-for-byte.
- Latents still recording into `latents-dev.db`, ~7 h left on the task.
- Dashboard still on the OLD code; a restart reopens COM5 and centres both
  servos, so it waits for him to be present.

## The dome (his frame, 2026-10-06 afternoon)

`avi_bot/perception/dome.py`. Look directions get a coordinate system anchored
to a pose he taught: **elevation = angular distance below TALL, azimuth =
rotation around it.**

**TALL is the top because the arm cannot look higher.** Its wrist is 176.0 and
the wrist's soft limit is 176.0 -- the pole is on the edge of reachability.

**Elevation is a signed SUM of the three pitch joints**, because shoulder,
elbow and wrist all rotate the camera about the same axis and orientation adds
along a serial chain. Each gain should be near +-1; the wrist's measured 0.96
agrees. base_yaw's 0.62 does not, which is the SAME anomaly depth.py found --
now reached twice by independent routes. Something is wrong with that constant.

**The signs were inferred, then checked, and the check is the evidence.** One
of four sign combinations puts TALL top and REST bottom, and it also lands the
three desk poses within 4 degrees of each other:

    TALL 0.0   ATTENTIVE -39.1   OBSERVE -40.7   HOME -43.1   LOW -64.6

Taught by hand on different days, no model in mind. A test asserts no other
combination does this.

**Two hard limits worth remembering:**
- REST is at -269.6 degrees, which is not a direction. The arm is folded back
  on itself there and the linear model is simply wrong. The dome refuses past
  MAX_ELEVATION rather than returning arithmetic.
- **The dome is ~87 degrees wide, not 360.** base_yaw spans 140 degrees of
  command x 0.62 gain = +-43 degrees of azimuth. Covering a room needs the
  BASE physically repositioned. No joint sequence will do it. This bounds
  every sweep plan, and 360 is the natural wrong assumption.

**Redundancy is resolved toward taught shapes.** Three pitch joints serve one
degree of elevation, so a direction has infinitely many solutions; the one
nearest a pose he taught is chosen, so the arm holds postures he picked rather
than contorting.

**POST /build_dome trains it from motion.** Each joint moves ALONE by 5 degrees
while a landmark is tracked; the pixel slide is that joint's effect on the look
direction. One at a time is required, not convenient: joints that always move
together have unidentifiable gains, and `_fit_pitch` refuses an arbitrary split
rather than inventing one. **shoulder and elbow have never been measured on
hardware** -- `unknown()` names them as assumptions until they have been.

Dome gains are CALIBRATION, so they carry over launch day. A measurement about
the hardware, not about him.

**Blocked on him being present**: /build_dome needs the dashboard restarted
onto the new code (reopens COM5, centres both servos) and it swings shoulder
and elbow, which no probe has moved before.

### Next: re-express roommap on the dome
roommap stores raw (yaw, wrist) and is only valid at one shoulder/elbow -- it
refuses to mix shapes. The dome handles all four joints, so a place stored as a
dome direction survives a change of shape. That is the real 3D map, and it is
the natural next piece once the gains are measured.

## 2026-10-06 afternoon: TALL as the pole, and the map finally works

He asked for this and it repaired the design rather than patching it.

### The dome model was wrong across shapes, and TALL's shape fixes it
The three-pitch-joint elevation model predicts where the camera POINTS and
ignores where it IS. On a serial arm shoulder and elbow move the lens a long
way. Two measurements proved it: OBSERVE sits between HOME and ATTENTIVE in
pitch sum yet sees something completely different, and once the gains were
measured the pose ordering stopped matching his own names (ATTENTIVE "slightly
raised" came out BELOW LOW "down and small"). **The 4-degree clustering I
called evidence was an artefact of assuming every pitch gain was +-1.**

Holding shoulder and elbow at the POLE's values removes the problem. The camera
then turns in place: base_yaw pans, the wrist tilts, nothing translates -- the
one regime where the dome is exact. And the wrist alone spans **219 degrees**
of elevation from TALL, more than a hemisphere, so shoulder and elbow were
never needed for looking around.

`dome.pose_at(az, el)`, `ladder(step)`, `grid(el, az)`.

### Measured gains (config [dome], 2026-10-06)
    base_yaw 0.6917   shoulder 1.1538   elbow 1.7189   wrist 1.2734
r2 0.990 / 0.986, both trustworthy by the probe's own residual check.
**elbow is 1.72, not ~1** -- it dominates elevation, which is why a 5 degree
elbow step threw the tracked laptop out of frame.

### SMALL COMMANDS DO NOT MOVE THIS ARM (he said it before the data did)
px/deg against step size: 11.5 at 2 deg, 13.3 at 4, 17.8 at 8, 19.1 at 16.
Negative travel was worse -- 2 and 4 degree commands gave 3 px, which is noise,
and 8 degrees moved the view the WRONG WAY. Fixed by approaching every target
from one side (overshoot, come back).
**This is a better explanation of the wrist loop hunting +-5 degrees than
mistuned gain is**: the loop's small corrections are under-delivered.

### OBSERVE is retired as the logging pose; it faced a blank wall
Daylight sweep of every taught pose: TALL sd 65.5 laptop 0.94 / ATTENTIVE sd
62.9 three objects / HOME sd 63.2 laptop 0.70 / **OBSERVE sd 15.5 NOTHING at
any base_yaw from 60 to 110** / LOW sd 13.0 nothing / REST sd 47.0 nothing.
OBSERVE was the one pose that could not see the desk and it was the pose the
whole logging pipeline read from. I broke it earlier by retaching base_yaw
90->60. Now named once as `poses.OBSERVE_POSE`; surveys run from TALL's shape.

### The survey works now
    before:  6 stops,  2 sightings, 0 confirmed
    after:  30 stops, 44 sightings, 7 confirmed, seen-once 13 -> 5
    laptop: 12 sightings from 5 distinct viewpoints, conf 0.89

### Two wrong turns, recorded so they are not retried
1. One laptop mapped as two places 20 deg apart. I blamed the vertical
   constant and swapped 32.2 for the dome-derived 24.3. **The gap stayed at
   20.2 and the pen doubled too.** The real cause: a large box's centre stays
   near frame centre across several tilt rows, so each row files it at its own
   elevation. Fixed by scaling the merge threshold with the object's apparent
   size -- you cannot localise a thing more precisely than its own width. The
   constant swap stays on its own merits, not on the argument I gave.
2. depth.py and dome.py each had their own pixel-to-angle conversion, which is
   the exact failure find.py's docstring warns about, committed hours after
   writing that warning. Now `optics.py`, with which-to-use-when: linear is
   the convention the measured deg-per-frame constants were fitted with;
   pinhole is the physics and depth needs it, because the 0.72 degree gap
   between them is the same size as the parallax it measures.

### Still open
- laptop has 3 places: one is 89 deg away (probably a genuinely different
  screen), one is 26.6 deg below the main place (a true duplicate; the size
  threshold computes 23.5, just short). Not tuned further on purpose.
- yaw_deg_per_frame: 83.9 in [camera] vs 75.2 from the dome gains. Both kept,
  side by side. Pick one deliberately.
- Still owed: **camera_radius_m** (base axis to lens) and **which way the lens
  faces**, for metric depth.

## The hemisphere sweep (2026-10-06, early afternoon)

His spec: sweep every level of the hemisphere, V-JEPA at each, slow to avoid
motion blur, then check whether the video actually swept. `POST /sweep_dome`.

**It swept.** 37 levels from TALL down, verified three ways: `between` 33.8 vs
`within` 2.1 (each level distinct AND stationary, 15.8x), 16/16 distinct frame
sequence numbers at every level, zero repeats. Latents saved with their
elevation, time, light and sd to `avi-remote/sweep-dev.npz` (dev profile).

**Map of the dome from TALL:** desk band at elevation 0-48 (textured, sd
37-68); a **flat blank wall at 48-120** (sd 14-17 -- what OBSERVE was staring
at); texture again 120-186; and below wrist ~16 the **wrist stops moving**:
`between` collapses to 3-4 px while `within` rises to 5-7. The view is frozen
but the frame shakes -- the servo stalling against a mechanical stop well above
its 4-degree soft limit. Stall current on a current-limited UBEC is a brownout
risk. **Raising the wrist soft_min to ~20 is recommended, not yet done** -- it
is his hardware config.

**The confound, caught on real data.** The first sweep was top-to-bottom, so
elevation and time correlate at -1.00. Its raw Mantel r is **0.75** and the
nearest latent is the adjacent level **94.6%** of the time against 5.6% chance
-- which would have been reported as "V-JEPA strongly encodes elevation". The
probe declared it CONFOUNDED instead. Without the time control it would have
passed (a test pins that). `order=shuffle` exists to break the tie.

### The test suite segfaulted, and it was not the new code
Adding view_probe made the full suite crash deterministically inside
`np.triu_indices`, never when run alone. After the suite, `import numpy`
recursed forever. `tests/test_room_tracker.py` popped `"numpy"` from
`sys.modules` in every setUp and never restored it, so the next real-numpy test
re-ran numpy's `__init__` over a still-loaded C core. Hidden for weeks because
the numpy-heavy tests sort before "room". It no longer touches numpy and
restores every stub it installs.

### Bugs of mine this stretch, recorded so they are not repeated
- `_frame()` returned the SAME image on every call in a loop (`get_after(-1)`
  and the sequence thrown away). The "median of nine frames" in build_dome was
  the median of nine copies of one frame, so it reduced nothing. Every per-stop
  average in the survey and both parallax probes had the same hole. Fixed with
  `_look(latest, n)`. **The dome gains in config were fitted on duplicated
  frames and should be re-measured** -- blocked by daylight right now (nothing
  clears 0.35 at HOME any more).
- `sweep_dome` first called `enc.encode(frames)`; the Encoder is called
  directly and returns (vector, ms). Would have failed on the MSI only.
- Silenced `dashboard.sh start` to /dev/null again and missed that it had not
  started. Third time this session.

## 2026-10-06 afternoon: AVI can find things and look straight at them

### MAIN DIRECTIVE (saved to memory, his words)
"An inference level robot whose one task is to learn about Shaurya and help
him out as much as it can, learning about him by observing his surroundings."
Everything below is in service of that. Finding his things is the first
helping behaviour that works end to end.

### The vertical sign was backwards in the map and in find
`POST /find` turned the arm right in yaw and wrong in the vertical, always the
same way (laptop 22%, pen 14% below centre). Proved on the arm: wrist
151.3 / 157.3 / 163.3 put the laptop at cy 344 / 516 / 599 -- raising the
wrist moves a landmark DOWN at 21.3 px/deg, so below centre means LESS wrist.
`direction_of` and `find.wrist_for` ADDED the offset.

It also explains the duplicate places: with the sign inverted an object is
filed at 2*row - truth, so copies separate by twice the row spacing (~19 deg;
observed 20.2). **My two earlier explanations -- the vertical constant, then
large box centres -- were both wrong.**

How it survived: a test stated the right premise and inverted the conclusion
(`test_above_centre_means_a_lower_wrist`); the synthetic-sighting helper had
the same wrong sign; and a test only checked that find and roommap AGREED --
they did, both wrong. The tracking loop had it right all along. A wrist
round-trip against it now exists.

**Result, after rebuilding the map:**

    object     before         after
    laptop     11% / 22%      1.6% / 3.2%
    pen         1% / 14%      0.4% / 2.8%
    book       not in frame   1.0% / 2.2%
    backpack   --             6.2% / 2.1%
    mug        --             not detected at its aim (weak detection, fading light)

pen and backpack collapsed from 2 places to 1; seen-once 13 -> 3. The second
"laptop" is 91 deg away in yaw at the same height -- a different object.

### Re-measured gains (real frame averaging)
base_yaw 0.7492, shoulder 1.5426, elbow 1.1877, wrist 1.0148. The wrist now
cross-validates: 30.5 deg/frame vs 32.2 (probe_wrist) vs 33.9 (direct). The
first set was fitted on duplicated frames and is void. **Retracted: "elbow is
1.72 and dominates elevation."** Shoulder/elbow are still loose, but looking
around holds them at TALL's values, so only yaw and wrist matter -- the two
measured well.

### Wrist soft minimum 4 -> 20 (his request)
Stalls below ~16 (view frozen, frame shaking). Host clamps it now; the firmware
sketch is updated too but **only takes effect after the Arduino is reflashed**.

### Latent bug that would have bitten on the robot
`run_survey` used a name it never defined after the _frame refactor; it only
surfaced as a NameError on the MSI. `tests/test_undefined_names.py` now scans
every shipped module with symtable (no pyflakes installed). On the committed
file it reports exactly that one; nowhere else has another.

### V-JEPA
Shuffled sweep: partial Mantel r 0.745 controlling for time and light, null
max 0.27, p 0.0005, nearest neighbour 100% adjacent. **V-JEPA's space is
organised by viewing direction.** Not yet shown: that this beats raw pixels
(adjacent tilts overlap, so any appearance representation may do the same).
The pixel-baseline sweep is the next result.

## 2026-10-06 ~14:30: flashed, the NAT64 trap, and AVI starts learning *him*

**Firmware reflashed remotely** (`scripts/remote/flash.ps1`), at his request:
COM5 auto-detected as Uno, 10414 bytes written and **10414 verified by
avrdude read-back**, boot banner `start mode READBACK` (holds position at boot,
no lurch), query returned exactly the prior position. Wrist soft-min 20 is now
on the board as well as the host.

**The "hung dashboard" was NAT64, not the flash.** He moved from school wifi
to a T-Mobile hotspot (IPv6-only + NAT64). macOS rewrote the IPv4 literal
100.x.y.z into 2607:7700:0:32::644b:b126 and sent it over cellular, past
Tailscale. ssh/sftp/nc kept working, so it looked exactly like a wedged
server. **Use the MagicDNS name for HTTP** (`MSI_HTTP` in common.sh) or `curl -4`.

**V-JEPA beats raw pixels.** Shuffled sweep, same controls and null: V-JEPA
partial r 0.727 (null max 0.248); 32x18 mean-centred thumbnails 0.184 (null
max 0.343 -- not distinguishable from chance). Gap +0.543. First measured
evidence the representation carries viewing direction beyond raw appearance.
Limit: thumbnails are a weak baseline; a learned single-frame model (DINOv2,
the VLM's vision tower) is the next comparison, to ask whether *video* matters.

**Mug:** found and centred within 6% on re-test; the earlier miss is not
reproducible and no cause is claimed. Found instead: the detector vocabulary
has near-synonyms (mug/cup, book/notebook) and book/notebook visibly split one
object. The map now files them under one name.

**Presence — AVI learns when he is at the desk** (`perception/presence.py`,
`GET /presence`). Looks every 20 s. Absence only counts from the seat view
(ATTENTIVE); hysteresis 2 to arrive / 6 to leave; arrivals and departures now
fire the mood events automatically (they were buttons). Learns sessions,
presence by hour (with look counts), today's minutes, and a usual arrival time
once 3+ arrivals exist. presence-dev.db, in the launch-day test.
**The empty chair scores 0.38-0.39 as a person** (a static 602x660 box, >90%
of the frame) -- it was counted once at a 0.40 floor. Floor is now 0.50.
**Unvalidated with him in the chair** -- expect 0.6+; check this first.

## 2026-10-06 ~15:20: AVI sees someone at the desk, and what changes

**Presence validated against a real person.** At 15:14:39 a person appeared at
the seat view at **0.924 / 0.910 / 0.904** -- against the empty chair's
0.38-0.39 -- and "arrived" was logged after the second sighting. The 0.50 floor
sits in a gap of half a unit. While the arm was away scanning, misses were not
counted as absence; the session survived a dashboard restart. (Not confirmed it
was Shaurya -- could be anyone at the desk.)

**Break coach** (`skills/breaks.py`, `pause_loop`): after 50 min at the desk,
waits for a NATURAL pause from change.py ("change" = motion that settled; never
"change-forced"), then the yawn/stretch gesture. Detector suppressed while the
arm moves so its own gesture is not read as his pause. Session began 15:15.

**Desk changes** (`perception/desk_changes.py`, `POST /desk_scan`,
`/desk_history`): survey snapshot -> diff with the previous one -> appeared /
disappeared / moved / stayed, with whether he was here. Rules: gone only if the
new scan LOOKED there (else "unchecked"); a single sighting is a flicker;
boxes clipped by the frame edge are excluded (they split the laptop, backpack
and a pen into pairs 13-16 deg apart); snapshots are VERSIONED and never
compared across a code change (that crossing produced a fake "phone, wallet,
backpack disappeared").
Clean result so far: laptops and both pens stayed; the mug moved 28 deg while
someone sat at the desk (tentative -- 3 sightings).
Known gap: an object too big to ever fit whole in one frame (the backpack) is
now excluded from snapshots entirely.

**Also fixed:** run_survey filed sightings under the shape it had BEFORE
switching to TALL's (worked only because /survey happened to pass pose=TALL).

**Honest correction on V-JEPA:** sweep clips are stationary within a level
(within ~2), so "V-JEPA beats pixels" is about its SPATIAL encoding. Whether the
video/dynamics part helps is untested; that needs clips taken during known arm
motion.

## 2026-10-06 ~16:45: AVI knows which way is level -- 3D without a ruler

**The 1.8x "under-travel" was never real.** `POST /probe_horizon` turns the
base 8 deg twice at 14 wrist tilts on TALL's shape and registers the pictures
(ORB + RANSAC). A turn about the base axis ROLLS the image by g sin(e) and
SLIDES it by g cos(e). Roll needs no FOV and carries no parallax, so fitting it
across tilts (26 measurements, rms 0.066, bootstrap CIs) gives:

| | value | 95% | note |
|---|---|---|---|
| base_yaw gain g | 0.95 | 0.89-0.98 | config assumed ~0.55 |
| level | wrist 160.3 | 155-164 | TALL looks ~16 deg ABOVE level |
| wrist gain | 1.03 | 0.93-1.09 | dome's independent 1.0148 agrees |
| FOV (linear) | ~48.6 | +-3 | trades off against g; datasheet 52 |

52/83.9 = 0.62 = cos 52; the dome's 0.7492 = cos 41. The low slide was the
camera looking DOWN. Wrist ~44 on this shape sees the robot's own base (no
roll, no slide); below that it looks back under itself (slide reverses).
Looking down, the desk slides 0.3-0.4 deg/deg faster than the far room: the
lens is in FRONT of the base axis (camera faces outward), offset/height ~0.35.
All in `[horizon]`, config/joints.toml.

**Backlash: 13.1 deg** (`POST /probe_backlash`: same yaw from above vs below,
13.5 / 12.7; from below twice repeats to 0.8). Up to ~16 effective when the
arm looks down. Every yaw move now ends in an upward run of 18 deg unless the
slack is KNOWN to be taken up (`motion/backlash.py`, `Slack`). Two real bugs
it explained:
- every survey row started with a downward jump, so its first column was
  misplaced (a pen 11 deg off; the next 13 deg step moved it 32 px, not 280);
- /find turned 10.9 deg UP from a survey's downward park and never reached
  the laptop.
Stops near soft_min cannot get a full run-up; their sightings are dropped
(`yaw_settled` per stop) and they do not count as "looked there" for desk diffs.

**The map now works on the sphere** (`perception/world.py`, `RoomMap(view=)`).
On the same dense survey (42 settled boxes): world geometry gives ONE cluster
per object at 8/15/25 deg thresholds, scatter 2.37 deg; the flat constants
split the laptop in two at 8 deg and scattered the mug by 6.1. The flat map
on disk was archived to `roommap-dev.json.flat.json`. /find from the new map
centres things within ~1-5 deg in a 48 deg frame. desk_changes snapshot
version 3 (new instrument).

Still not modelled: parallax from the lens offset (near left-desk objects are
~0.5 deg worse in world geometry than flat, which absorbed some of it by
accident). Needs one length -- see below.

**Only ONE ruler number is now needed for metric 3D**: lens height above the
desk at TALL's shape, OR the lens's horizontal offset from the base axis. The
ratio between them (~0.35) is already measured.

### base_yaw goes silent under sustained work (2026-10-06 ~16:30 and ~16:42)
Twice, partway through a dense survey, base_yaw stopped moving: commands went
out, nothing in the log, the WRIST kept moving, and every stop showed the same
picture. The first time eight stops filed one pen under eight yaws. The
firmware has no stall cutoff (it keeps writing PWM), so this is the servo or
its supply -- consistent with the current-limited UBEC, since the base moves
the whole arm. Confirmed directly: /probe_backlash read 0.02 deg against 13.1
an hour earlier. The survey now registers each settled stop against the last
one and refuses to file a stop where a 5+ deg turn changed nothing; two in a
row end it (`horizon.stuck`). **The 5 V 4 A supply is the likely fix.**

### Desk positions and words (`world.on_plane`, `perception/spatial.py`)
A box's bottom edge, projected onto the desk with the lens offset corrected,
gives a position in lens heights. Desk snapshots carry it; `POST /desk_layout`
lists objects nearest-first with a sentence each, and /find appends it:
"In front of and left of the laptop, nearer to me." Viewpoint is AVI's (his
seat is not measured), units relative until the lens height is. Things above
level (the "mug" at wrist 164) get no desk position -- they are not on this
desk plane. From the clean survey: pen 2.4, laptop 3.7, backpacks 2.7 and 4.0
lens heights from the base axis.

### 3D is done up to one ruler number (2026-10-06 ~17:10)
Every desk object now has (x, y, height) in lens heights, from AVI's base:
`POST /desk_layout`. Two live scans 10 min apart agree to 0.1-0.24 h. Live:

| object | range | height | where |
|---|---|---|---|
| pen | 2.30 h | 0.04 h | in front of and left of the laptop, nearer to me |
| laptop | 3.41 h | 0.63 h | left of the backpack |
| backpack | 3.86 h | 0.74 h | right of the laptop, farther from me |

/find says it: "The desk, around 4:58. In front of and left of the laptop,
nearer to me." Set `[horizon] lens_height_m` (ruler: lens to desk surface at
TALL) and both report metres too. **Provisional, not entered:** the laptop
screen standing 0.63-0.72 h tall, against a typical 0.22-0.25 m open screen,
puts the lens at ~0.3 m.

Also fixed: /find crashed on the MSI for every object it had seen --
`strftime("%-I")` is invalid on Windows (Mac tests could not see it); same bug
in reminders more than an hour out. `numbers.clock_time()`, plus a test that
fails on any "%-X" in the robot's code.

base_yaw recovered from its stall after ~13 min idle with no reset (backlash
then read 14.95, repeat 0.8): it rests itself, i.e. heat or supply sag, not a
latch. Long dense surveys back to back trigger it; desk scans (35 stops) have
been fine.

**What is left for 3D:** the ruler number; parallax correction in the
DIRECTION map (desk positions already correct for it); measuring [horizon]
at ATTENTIVE's shape (one roll measurement) if the seat view ever needs world
directions.

## 2026-10-06 evening: the voice brain runs on the real arm

`avi_bot.main.Avi` -- router, 27 tools, timers, reminders, memory, focus,
scheduler -- was complete and ran nowhere. It is now hosted in the dashboard
(`dashboard/brain_host.py`) with the real arm: **`POST /ask {"text": ...}`**.
The reSpeaker feeds the same call on Thursday. Verified live:

| he says | AVI | route |
|---|---|---|
| what time is it | 5:32. | local |
| where's my pen | The desk, around 5:05. In front of and left of the laptop, nearer to me. (and turns to it) | local |
| what timers do I have | No timers. | groq |
| remember that my chemistry test is on friday | (stored) | groq |
| what do you know about me | Chemistry test on Friday | groq |

Three bugs found getting there:
1. Every turn after the first failed: the brain's sqlite stores refused
   use from another thread, and every HTTP request is its own thread. Shared
   connections now, turns serialised by the host.
2. Every cloud question came back "confused": **the MSI venv has no `openai`
   package**. Rather than install one, the provider falls back to plain HTTPS
   (the API is JSON); the router already read dict responses.
3. The brain's find had its own, older path; it now uses the dashboard's
   (world map, backlash-safe turn, desk layout), so there is one way to find.

Announcements (timers, reminders) are shown on the dashboard and cued with
the `ahem` gesture, **only while he is at the desk** -- a cue to an empty room
is not a delivery, so it stays pending and the scheduler retires it if stale.
Brain memory is `brain-dev.db` (launch-day rule, pinned by the profile test).

The Mac's .env has CALENDAR_ICS_URL, KASA_* and OPENROUTER keys; the MSI's has
only GROQ. Copying secrets across is his call.

Also: latent recording now survives restarts (`dashboard-settings.json`);
fifteen restarts today had silently turned it off. Surveys skip stops that can
never settle (~18 of 99 moves). Desk diffs ignore faint objects (fewer than 3
hits or conf under 0.40), which had produced fake "appeared" events.

### Surveys go column by column (2026-10-06 ~18:45)
Row by row, every elevation row swept base_yaw across its range and jumped
back with a run-up: ~2600 deg of base travel per dense survey, on the joint
that stalls under sustained work. `backlash.column_order` steps the base UP
once per column and sweeps the wrist down instead. Same 99-stop grid, live:
**base travel 107 deg (24x less), 190 s instead of 253, 81/81 stops settled
(the 18 unsettleable ones are skipped), no stall**, and the map came out with
one pen instead of two. The stall check now compares each stop with the same
wrist level one column back.

The brain also got a body: reply gestures are performed (the arm can play 13
of the brain's 19 names; the rest map to the nearest), and until the speakers
arrive a beep becomes a gesture -- "set a timer" now nods. Memory questions,
remember/forget and "what timers do I have" are local rules; the dashboard
page has an "ask avi" box.

### AVI looks for things it hasn't seen (2026-10-06 ~18:50)
"Where are my keys" with no keys in the map used to end at "Haven't seen it
today". Now: "Haven't seen it today. I'll look." -- a desk scan runs in the
background (~1 min) and the result is told to him when he is next at the desk
("Found your keys. Left of the laptop." / "I looked over the desk; no keys.").
Things the detector has no word for get "I can't recognise a charger yet."
instead of a fake search. After any find, the arm looks for 15 s and then
goes back to the seat view, so presence keeps working.

Scheduled desk scans exist but are OFF: `POST /settings
{"desk_scan_every_min": 60}` turns them on (never while he is at the desk,
07:00-23:00 only). The dashboard runs for a fixed time per start (default
60 min); it is running until ~22:00 tonight. A daily schedule for it is his
call.

## 2026-10-07 morning: things that were built and never connected (deployed 21:01)

The MSI was asleep and this network blocks Tailscale, so none of this has run
on the robot. First thing on the home network: `./scripts/wake-msi.sh`,
`./scripts/sync.sh`, `./scripts/dashboard.sh stop && ./scripts/dashboard.sh start N`.

- **Usual start = first sit-down per day** (`perception/habits.py`). Every
  return from a break used to count. Days run 04:00-04:00. Also learns usual
  finish, session length, break length (3+ days before claiming any).
- **Missed reminders are told, not dropped.** Stale or unheard reminders are
  `status='missed'` in brain-dev.db and said as "While you were away at 3:00
  PM: ..." when he is next present. Timers still go stale. Quiet/guest/sleep
  now hold AVI's own news too. The brain is built at dashboard start
  (`Host(eager=True)`), so reminders fire without a first /ask.
- **Cloud context** now includes the time, presence and the desk's solid
  labels (`Arm.situation`). Before: `now=None`, nothing else.
- **Morning rundown** on the first arrival since 04:00 (`Avi.rundown`):
  reminders + calendar still ahead today; silent when nothing is.
- **Break coach learns** (`presence.break_outcomes`): taken = left within
  7 min; each ignored offer in a row adds 15 min (max +45); taking one resets.
- **Focus mode is fed**: cell-phone detections (seat view, conf >= 0.40)
  while a session is active; settled pauses go to focus instead of the coach;
  timed sessions end with the summary. Mood's focus flag now set, so idle
  behaviour quiets during a session.
- **Mood events wired**: task_ok/task_failed per turn, new_object from desk
  scans. `_mood_event` no longer needs an arm.

To verify on the robot: say "remind me in 2 minutes to test", walk away for
15 min, come back -- expect "While you were away...". Start "focus for 5
minutes", hold a phone up for 2+ min, pause -- expect the ahem glance.

**Deployed 2026-10-07 21:01** (build 7efa6ec). 5 V 4 A supply now on the
PCA9685 V+ terminal (board is reverse-polarity protected). /selftest: all four
joints MOVED (camera diff 28-37). /health all running; the brain is built at
start. Cloud context verified live ("2 hours 57 minutes until midnight" at
21:03). **Daily start installed**: 08:10 for 830 min with --keep-awake.
Last night's log also shows the break coach firing for real (21:49, 65 min,
at a natural pause).

**Voice (2026-10-07 21:12): en_US-ryan-high, speed 1.15, pitch 0.95** -- his
pick ("so much better"); en_GB-alan-medium sounded scary to him even at
1.15/1.12. Settings `voice`, `voice_speed`, `voice_pitch` in
dashboard-settings.json, POST /settings, no restart. Amy and Alan are still in
assistant/ on the MSI.
