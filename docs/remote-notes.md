# Remote build notes

Running log of what has actually been tested from the Mac, with results.
Nothing here is assumed — every line was run.

Mac: `the Mac`, Tailscale `100.a.b.c`, LAN `192.168.0.236`.

---

## Phase 0 — SSH from the Mac to the MSI

**Status: DONE. No in-person step was needed.** (2026-10-04)

The doc expected sshd to be off and to need a PowerShell block pasted at the MSI.
It was already set up:

| Check | Result |
| --- | --- |
| Tailscale on the Mac | was **stopped**; started with `tailscale up --accept-routes` |
| MagicDNS `msi` | resolves to `100.x.y.z` |
| `tailscale ping msi` | pong in 9 ms, via relay `lax` |
| sshd on `100.x.y.z:22` | already **open** |
| sshd on LAN `192.168.0.27:22` | also open |
| Working username | **`msi-user`** (`whoami` = `msi\msi-user`) — *not* `MSI-User` |
| Key that works | the pre-existing `~/.ssh/id_ed25519` was already authorized |
| Default remote shell | PowerShell **5.1.26100.9549** (already configured) |
| `$env:USERPROFILE` | `C:\Users\MSI-User` (confirmed, has the space) |
| `msi-user` is an administrator | **True** |

A fresh dedicated key was generated anyway for hygiene:
`~/.ssh/msi_ed25519` (public key not yet installed on the MSI — the existing key works,
so this is optional; `~/.ssh/config` offers both).

`~/.ssh/config` gained a `Host msi` block (HostName `msi.your-tailnet.ts.net`, User `msi-user`).
Previous config is backed up at `~/.ssh/config.pre-avidesk.bak`. The older `robot-pc`
entry pointing at the same machine was left alone.

**Gotcha found:** Tailscale on the Mac reported the MSI `offline` for ~15 s after
`tailscale up`, because peer state was stale, not because the MSI was asleep. Don't trust a
single `tailscale status` reading right after starting it — probe port 22 instead.

---

## Phase 1 — Wake and sleep from the Mac

### Wake-on-LAN: **CONFIRMED WORKING** (2026-10-04)

The doc listed this as "not yet confirmed", with the Ethernet cable unplugged.
**The cable is now plugged in** — the Killer NIC answers ARP at `192.168.0.27`
(MAC `XX:XX:XX:XX:XX:XX`). The MSI is no longer on the old Wi-Fi address `192.168.0.119`.

Clean test, from a genuine sleep (Tailscale `offline, last seen 1m ago`;
`tailscale ping` timed out; port 22 unreachable on both addresses):

```
15:35:52  magic packet -> 192.168.0.255:9, :7 and 192.168.0.27:9
15:36:00  sshd reachable          => woke in 8 s
```

Repeated twice more via `scripts/wake-msi.sh`: **6 s** each time. Reliable so far (3/3).

### Sleep state: **S3, not Modern Standby**

`powercfg /a`:

- Available: **Standby (S3)**, Hibernate, Fast Startup
- **Not** available: S0 Low Power Idle (firmware doesn't support it), S1, S2, Hybrid Sleep

So the Modern Standby complications the doc warned about **do not apply**. Plain S3 + WoL,
which is the well-behaved combination.

**Hibernate is still enabled.** The doc wants `powercfg /hibernate off` so that
`SetSuspendState 0,1,0` sleeps rather than hibernates. Not done yet — it changes a system
setting, so it needs Shaurya's go-ahead. In practice `SetSuspendState` has produced a real
S3 sleep that WoL wakes, so this is not currently blocking.

### Why it kept dying mid-session — diagnosed

It dropped twice in the middle of a command, ~2 minutes after each wake. Not the idle timer:

```
Sleep after      (STANDBYIDLE)  : AC 0 = never,  DC 18000 s (5 h)
Hibernate after  (HIBERNATEIDLE): AC 0 = never,  DC 900 s (15 min)
Allow wake timers(RTCWAKE)      : AC 1 = ENABLED, DC 0 = disabled
```

It's on AC (`PowerOnline = True`), where the plan says **never sleep**. The ~2-minute death
is the hidden **unattended sleep timeout** — Windows' default 120 s rule for a machine that
woke without a user present, which is exactly our situation on every WoL wake. It is not
visible in `powercfg /q` output because the setting is hidden by default.

Useful side effect: **wake timers are already enabled on AC**, so the doc's fallback plan
needs no setup.

### Sleep from the Mac: **WORKING, but the doc's command is wrong**

The doc's Phase 1 command does **not** work over SSH:

```
ssh msi "rundll32.exe powrprof.dll,SetSuspendState 0,1,0"     # DOES NOT WORK
```

Two failures at once: sshd waits on the foreground `rundll32` process, so the connection
stalls for `ServerAliveInterval x CountMax` (180 s with our config), **and** the suspend
never takes effect. Detaching it fixes both:

```
ssh msi 'Start-Process rundll32.exe -ArgumentList "powrprof.dll,SetSuspendState","0,1,0"'
```

ssh then returns immediately and the machine goes down. Measured: **11 s** to sleep after the
command, with keep-awake released first (see the `WakeOnPattern` section below for why
confirming this is unreliable).

**Release keep-awake before sleeping.** `sleep-msi.sh` does this automatically. With the
hold still active the machine stays up.

**`SetSuspendState` does not always fire.** Observed once with `powercfg /requests` showing
`SYSTEM: None.` and keep-awake already terminated: the machine simply stayed awake. Issuing
the same command again slept it immediately. `sleep-msi.sh` now re-issues up to 3 times
rather than only probing, so a silent no-op doesn't leave the laptop running.

### The big one: `WakeOnPattern` is enabled, so network traffic wakes it

```
Get-NetAdapterPowerManagement:
  Ethernet : WakeOnMagicPacket Enabled, WakeOnPattern ENABLED
  Wi-Fi    : WakeOnMagicPacket Enabled, WakeOnPattern ENABLED
```

With `WakeOnPattern` on, an ordinary incoming TCP SYN wakes the machine, not just a magic
packet. Two consequences, both confirmed by behaviour:

1. **My own confirmation loop was waking it.** `sleep-msi.sh` originally probed port 22
   every 3 s to confirm sleep, which woke it back up as fast as it slept, then reported
   "it may not have slept". The script was fighting itself. Widening the window from 45 s to
   120 s did **not** help, because the cause was the probing, not the timing. Now fixed: a
   quiet 45 s wait with no packets, then at most 3 sparing probes.
2. **Tailscale keepalives can wake it too.** The MSI is on Ethernet (`192.168.0.27`) *and*
   Wi-Fi (`192.168.0.119`) at the same time, and Tailscale's direct path was observed on the
   **Wi-Fi** address. So the tailnet's own keepalive traffic lands on a NIC that wakes on any
   pattern. Very likely why the laptop does not reliably stay asleep.

**Honest status of sleep:** the command is issued correctly and the machine *does* sleep,
observed asleep repeatedly including for minutes at a time (`offline, last seen 1m ago`).
But **confirming it from the Mac is unreliable**, and it does not stay asleep dependably,
because of `WakeOnPattern`.

**FIXED 2026-10-04, with Shaurya's go-ahead:**

```powershell
Set-NetAdapterPowerManagement -Name "Ethernet" -WakeOnPattern Disabled
Set-NetAdapterPowerManagement -Name "Wi-Fi"    -WakeOnPattern Disabled
```

Both adapters now read `WakeOnMagicPacket Enabled, WakeOnPattern Disabled`.

Verified immediately after the change:

- sleep issued, 50 s quiet wait, single probe -> **asleep and stayed asleep**
- `wake-msi.sh` -> **awake in 18 s**

So sleep now sticks and Wake-on-LAN still works. Phase 1 is genuinely complete.

### Wake times are variable

Five measured wakes via `wake-msi.sh`: **8 s, 6 s, 6 s, 37 s, 19 s**. All succeeded (5/5),
but don't assume it's always under 10 s. The 90 s poll window is right.

### Keep-awake: **WORKING** (`scripts/keepawake-msi.sh`)

Self-expiring by design, as the doc asks — a forgotten session can't leave the laptop on.
Holds `ES_CONTINUOUS | ES_SYSTEM_REQUIRED`, re-asserts every 30 s, releases in a `finally`,
and has a hard 8-hour ceiling.

Runs as a **scheduled task** (`AVI keepawake`, user `SYSTEM`) because Windows OpenSSH kills
session processes on disconnect, which would drop the hold the moment we log out. SYSTEM
avoids needing a stored password.

Verified: `powercfg /requests` SYSTEM section shows the holding
`powershell.exe`, and the hold defeats the unattended-sleep timeout.
**Endurance test passed: stayed awake 362 s**, against the 120 s threshold that was killing
every earlier session.

**Bug hit and fixed:** first run failed with `lastResult=1` and no hold. Cause — the task
runs as SYSTEM, so `$env:USERPROFILE` inside the script resolved to the *system* profile,
whose `avi-remote\` directory doesn't exist, so the first log write threw under
`$ErrorActionPreference = "Stop"` and killed the script before it did anything.
Fix: pass `-LogPath` explicitly from the caller (resolved in `msi-user`'s context) and make
the log function create its directory and never throw. **Lesson: never let logging be able
to kill the thing it is logging.**

### Remote helper location

Our PowerShell helpers live in **`C:\Users\MSI-User\avi-remote\`**, deliberately
*outside* `avi\`, so the existing git repo is not touched. Pushed by `scripts/push-remote.sh`.

**scp gotcha:** quoting a Windows path containing a space through `scp` is unreliable.
All file pushes go over `ssh` as base64 and are written with
`[IO.File]::WriteAllBytes(...)` instead. `deploy.sh` and `push-remote.sh` both do this.

---

## Phase 3 — Hardware access over SSH

### 1. GPU from an SSH session: **WORKS**

```
python        3.13.13
torch         2.13.0+cu126
cuda_avail    True
gpu           NVIDIA GeForce RTX 3060 Laptop GPU
vram_free_GB  5.38 of 6.44
transformers  5.14.1
bitsandbytes  0.50.0
ultralytics   8.4.116
cv2           5.0.0
```

Every version matches the doc. 5.38 GB free against the logger's ~3.8 GB need — fits, with
no room for anything else running alongside (as the doc warns).

Harmless warning on every import: `triton not found; flop counting will not work`.

### 2. Camera from an SSH session: **WORKS** (2026-10-04)

No session isolation problem. The doc worried that `cv2.VideoCapture(1, cv2.CAP_DSHOW)`
might fail outside the interactive desktop session. It does not - it opens and delivers real
frames from a plain non-interactive SSH command. **The interactive scheduled task is not
needed just for camera access.**

Cameras present (`Get-PnpDevice`): `Logi C270 HD WebCam`, `Integrated Camera`.
The doc's index mapping is correct: **index 1 = C270**, index 0 = the laptop's Integrated
Camera. Both open at 1280x720 via DSHOW. DSHOW reports `CAP_PROP_FPS = -1.0`; ignore it.

Privacy: no frame was saved or copied off the MSI. Only metadata (shape, dtype, mean
brightness, nonzero pixel count) was returned.

#### Trap: the C270's first ~5 frames are pure black, and `read()` reports success

Mean pixel value over 12 consecutive frames, index 1:

```
[0.0, 0.0, 0.0, 0.0, 0.0, 153.6, 149.7, 140.8, 137.0, 137.0, 137.0, 137.0]
```

`ok=True` on every one of those reads, and the first five are `mean=0.0` with
**0 of 2,764,800 pixels** above 8. A first-frame grab returns a fully black image while
reporting success. Index 0 (Integrated Camera) does not do this - it is usable immediately,
which is why the two look different in a naive test.

**Why this matters more than it looks:** the observer asks the VLM yes/no questions about
frames. Asked "is there a cup?" about a black frame, the model will answer confidently and
wrongly, and by design nothing enters memory without a VLM "yes". That is precisely the
**false belief** error the paper calls the worst kind, and it would be invisible in the logs.

**Required in any camera open path:** discard frames until the frame is actually exposed -
e.g. read until `frame.mean() > 1.0`, with a cap of ~15 attempts and a hard failure if it
never exposes. Do not just `sleep()` and hope, and do not trust `ok`. Check this against
`assistant/settings.py` and `camera.py` when `assistant.zip` arrives.

### 3. Arduino on COM5: **not touched, by design**

Opening the port resets the Arduino and centres both servos with nobody watching.
Needs Shaurya's explicit go-ahead.

---

## Phase 4 machinery — built and proven ahead of `assistant.zip`

`scripts/observer.sh start|stop|status|logs|once|today|test`, backed by
`avi-remote\task-ctl.ps1`. Verified end-to-end on 2026-10-04 with a throwaway
`_tasktest.py` (since removed from both machines):

- `deploy.sh` pushed a real file and listed it on the MSI — **first real test, works**
- task started, then **kept running and logging across several separate SSH sessions**
  (ticks 7-12 appeared while disconnected), confirming it survives disconnect
- `logs` tails the file; `stop` is clean (state `Ready`, `lastResult=267014`, 0 python procs)

Short runs (`once`, `today`, `test`) go over plain SSH — no task needed.
Long runs (`start`) go through the task **and** take a 240-minute keep-awake.

Two bugs found and fixed in the process:

- **`-ScriptArgs ''` breaks.** An empty string is stripped by the bash -> ssh -> PowerShell
  layers and PowerShell then reports the parameter as missing. `observer.sh` omits the
  parameter entirely when there are no args.
- **`$env:USERDOMAIN` is wrong over SSH.** It reports `WORKGROUP`, while the real identity
  is `MSI\msi-user`. `Register-ScheduledTask` then fails with `0x80070534`
  (no account-name-to-SID mapping). Fixed by using
  `[Security.Principal.WindowsIdentity]::GetCurrent().Name`.

Task runs with **S4U logon** (no stored password). Camera access already works from a
non-interactive session, so the doc's "must be the interactive session" requirement does not
apply. If the camera ever fails under the task but works over plain SSH, switch
`-LogonType` to `Interactive`.

## Phase 4 — the logger on real hardware (2026-10-04)

`assistant.zip` arrived: 10 files, exactly as the doc listed. Unpacked to `assistant/` on the
Mac (source of truth) and deployed to `C:\Users\MSI-User\avi\assistant\`.

| Check | Result |
| --- | --- |
| `test_observer.py` | **15 passed**, twice - before and after the `--once` patch |
| Startup "token id" error | **none** - the letter and yes/no token checks pass on real weights |
| Peak VRAM | **3520 MiB (3.44 GiB)** of 6144, against the ~3.8 GB estimate. Fits, with nothing to spare. Returns to 39 MiB on stop (no leak) |
| Model load | Qwen2.5-VL-3B 4-bit, 824 shards, ~9 s warm / ~35 s cold page cache |
| Camera in the real pipeline | opens at index 1, warm-up handled, first observation succeeded |
| Thumbnails | `AUDIT_RATE = 0.0` by default, so no frames are saved |

First observations, both from a cold process:

```
[21:53:03] startup  not at desk (0.90) | no verified objects | 2503 ms, 1 VLM calls
[21:57:28] startup  not at desk (0.82) | no verified objects | 2286 ms, 1 VLM calls
```

### `--once` added to `observer.py`

Implies `--no-preview` (nobody is at the desk to see a window), and has a 60 s deadline so it
cannot hang a remote session. Self-checks still 15 passed afterwards.

**Bug caught in my own patch before it shipped:** the `--once` timeout path used `sys.stderr`,
but `observer.py` never imported `sys`. It would have raised `NameError` on exactly the path
meant to report a failure. Fixed by adding the import.

### The camera trap was already handled - no change needed

`open_camera()` already loops until `frame.mean() >= 1.0` with an 8 s budget and raises
`"camera still returns blank frames after 8 s"` otherwise. The C270's black period is ~0.75 s,
well inside that. `CAMERA_INDEX = 1` is correct for the C270.

### Monitoring bug: python must run with `-u`

The task runner redirected stdout to a file without `-u`. Python block-buffers stdout when it
is not a terminal, so `observer.sh logs` stayed empty for minutes and a perfectly healthy run
looked hung. Only the bitsandbytes warning showed through, because stderr is not buffered.
Fixed in `task-ctl.ps1`; the log now streams weight-loading progress live.

### Latency: RESOLVED, and better than target

```
[21:59:13] startup    not at desk (0.97) | no verified objects | 2141 ms, 1 VLM calls
[22:04:13] heartbeat  not at desk (0.97) | no verified objects |  575 ms, 1 VLM calls
```

**Warm observation: 575 ms.** The earlier 2141-2503 ms figures were entirely cold-process
CUDA kernel warm-up on the first VLM call, not a real cost. Steady state is well inside the
1-2 s target.

Be precise about what 575 ms covers: that observation made **1 VLM call**, because nobody was
at the desk and the detector proposed no objects to verify. A full observation adds up to five
yes/no crop checks at roughly 0.3 s each, which lands around 2 s - exactly the doc's 1-2 s
range. So 575 ms is the floor case, not the typical one. Both are healthy.

**The bitsandbytes warning is a red herring.** It appears on every run:

```
bitsandbytes\\backends\\cuda\\ops.py:944: UserWarning: inner dimension (3420) is not aligned
for fast kernel with blocksize=64, falling back to slower implementation.
```

At 575 ms for one call against the paper's ~334 ms, there is no meaningful gap to chase.
Leave it alone. Noted here so it is not re-investigated later.

**Away heartbeat fires exactly on time:** 21:59:13 + 300 s = 22:04:13, to the second.
`AWAY_HEARTBEAT_S = 300` behaves as designed on real hardware.

### Side effect worth knowing

Ultralytics downloaded `yolov8s-worldv2.pt` (24.7 MB) plus a 338 MB CLIP backbone into the
working directory on first run, so those now live in `avi\assistant\`. Expected, cached, and
not re-downloaded.

## Room tracker (2026-10-04)

Motion on -> the logger runs. No motion -> the model is not even loaded, so the GPU is free.
This is what resolves the VRAM conflict: the logger no longer has to hold 3.4 GB across a
7.5-hour desk window.

- `assistant/room_tracker.py` - cheap watcher and orchestrator, runs on the MSI
- `observer.py --exit-after-away SECONDS` - the logger exits itself when the desk is empty
- `scripts/tracker.sh start|stop|status|logs` - control from the Mac

While idle the tracker holds the camera and does nothing but a 160x90 grayscale frame
difference: no model, **zero VRAM**. It reuses `ChangeDetector._prep/_fraction` rather than
reimplementing the differencing, so both agree on what a changed pixel is.

**Camera ownership is exclusive.** Only one process can hold the C270 through DirectShow, so
the camera belongs to exactly one of the two at any moment: released by the tracker before
the logger starts (with a 1.5 s pause for DirectShow to let go), re-acquired only after the
logger has exited.

Verified end to end, with VRAM sampled from the Mac throughout:

```
22:22:50 watching for motion, no model loaded
22:22:51 motion -> handing over the camera
22:22:53 starting logger: observer.py --no-preview --exit-after-away 60
22:23:41 startup  not at desk (0.82) | no verified objects | 2294 ms, 1 VLM calls
         nobody at the desk for 60s - exiting so the GPU is free
22:24:43 logger exited rc=0 after 110s - GPU released
```

VRAM: **39 -> 3404 -> 39 MiB.** Releases cleanly, no leak across cycles.

A crash-loop guard is included: if the logger exits in under 30 s something is wrong (bad
camera handoff, model failure), so the tracker backs off 30 s rather than hot-looping a
3.4 GB model load.

### Not yet calibrated: `--motion-fraction`

**The 0.02 default is a guess, not a measurement.** The handoff above was forced with
`--motion-fraction 0` to prove the pipeline, because the room was genuinely static and even
0.0005 never triggered - so the quiet-room noise floor is below 0.0005, which is the one real
data point. 0.02 (2% of pixels) is very likely too high: small movements like typing may not
reach it, and the logger would then never start while Shaurya is sitting still.

There is no way to pick this number from here. `--report-every` (default 300 s) now logs the
peak idle motion, so the threshold can be set from the room's own data:

```
[tracker 14:35:02] idle: peak motion 0.00021 over the last 300s (threshold 0.02)
```

Run it through one real session, read the peaks with him at the desk and away, then set the
threshold between the two.

### Caveat worth saying out loud

While "off", the tracker still **holds the camera open**, so the C270's activity light stays
on even when no model is loaded and nothing is being logged. The GPU is free, but the camera
is live. If that is unwanted, the tracker would have to open and close the device around each
poll, which is slow and hard on DirectShow.

## RESOLVED: it was the prompt, not the camera (2026-10-04, live session)

**Every observation says `not at desk` with 0.82-0.97 confidence, including with Shaurya
sitting at the desk.** The VLM is not wrong. The camera is pointed somewhere that does not
include the desk.

Evidence, gathered without any image leaving the MSI:

| Probe | Result |
| --- | --- |
| YOLO-World person confidence | **0.784 - he IS in frame** |
| VLM "is there a person?" | YES (+0.27) |
| VLM "is there a chair?" | YES (+2.27) |
| VLM "is there a desk or table?" | **no (-0.44)** |
| VLM "is there a monitor or laptop screen?" | **no (-3.72)** |
| VLM "is there a keyboard?" | **no (-4.39)** |
| Detector object candidates | **0 of 14 desk objects** |
| VLM "is the image blurry?" | YES (+0.98) |
| wall / ceiling / floor / bed / window | all no |

So the camera sees **him and his chair, but not his desk.** Given `ACTIVITIES`, every option
except `not at desk` and `other` requires seeing hands on a desk (laptop, writing on paper,
reading, phone, eating, guitar), and `ACTIVITY_QUESTION` literally begins "Look at the person
at the desk." With no desk in view, `nobody is sitting at the desk` is the closest match the
model can pick. **The pipeline is behaving correctly on a bad view.**

**Why this mattered to catch now:** logging for the two weeks milestone 5 needs would have
produced a database where every row says nobody was home, at high confidence, with nothing in
the logs looking wrong. That is exactly the false-belief failure the paper calls the worst
kind. `audit.py` would eventually have caught it - after days of wasted logging.

**I first concluded the camera needed physical re-aiming. That was wrong.** The evidence above
is real, but the defect was entirely in the prompt and label design. Shaurya's own instinct -
"it doesn't matter where I test it, it just matters that it works anywhere" - was correct and
led to the actual fix.

**The real bug.** `ACTIVITY_QUESTION` began "Look at the person at the desk", and the only
non-desk fallback in `ACTIVITIES` was `("not at desk", "nobody is sitting at the desk")`. With
no desk in view the model had nothing else to pick, so a present person scored *absent* at
0.82-0.97. The label conflated two different things: **no desk visible** and **no person
present.**

**The fix** (no hardware touched, camera unmoved):

- `ACTIVITY_QUESTION` -> "Look at this picture. Which one best describes what the person is
  doing right now?" - no desk presumed
- `("away", "there is no person in the picture")` replaces the old desk-shaped label, and
  `NOBODY = "away"`
- new `("idle", "a person is present but not doing any of these")` so a visible but inactive
  person is not scored as absent

Result on the very next observation, same camera, same angle:

```
[22:50:36] startup  laptop (0.89) | glasses | 2291 ms, 2 VLM calls
```

Correct activity, a verified object, and the view the system had been calling empty all
evening. All 15 self-checks still pass; every code path referenced `settings.NOBODY` rather
than the literal string, so the rename was safe.

**Note for the pre-registered metric:** `ACTIVITIES` is what the 80%-agreement audit scores
against, and it changed. This happened *before* any real logging, so no data is invalidated -
but the list should be frozen now, before the two-week run. `("talking", "talking with
someone")` is a sensible later addition; each extra option dilutes probability mass against
`ACTIVITY_MIN_P = 0.6`, so add deliberately, not casually.

**Still open, lower priority:** the VLM calls the image blurry (+0.98). The C270 is
fixed-focus, so distance and a lens wipe are worth checking - but it is clearly not blocking
correct classification.

### Motion threshold: measured

| State | `frac > CHANGE_PIXEL_DIFF(25)` |
| --- | --- |
| Still room | **0.00000** |
| Small / decaying motion | 0.003 - 0.07 |
| Hand waving at the lens | **0.67 - 0.83** |

Also established: **the stream is live, not frozen.** 0 of 110 consecutive *raw* 1280x720
frame pairs were bit-identical, median raw max-abs-diff 51. The near-zero prepped numbers are
`ChangeDetector` working as designed - resizing 1280x720 to 160x90 averages 64 pixels per
cell, which crushes sensor noise. **Never diagnose a frozen camera from the prepped frames;
compare raw ones.**

Default `--motion-fraction` changed from the guessed 0.02 to **0.005**, comfortably above a
0.00000 floor. Provisional: the view changes completely once the camera is re-aimed, so
re-measure then.

## Open items

- `assistant.zip` is **not on this Mac** — searched the whole home directory. Blocks Phase 2
  and 4. Need it from Shaurya.
- `powercfg /hibernate off` — needs Shaurya's go-ahead.
- Away-from-home wake needs a relay on the home LAN (Raspberry Pi on Tailscale, or a router
  WoL button). Unanswered.
- Usual desk hours, for wake timers. Unanswered.
- `~/.ssh/msi_ed25519.pub` is not installed on the MSI (optional; the existing key works).

---

## 2026-10-05 — new build spec adopted; M1 motion foundation

The plan changed. `BUILD_SPEC.md` now defines the product (a Dum-E-style desk
companion) and is the authority over `CLAUDE.md` where they differ. M0 is the
work already logged above. M1 is the motion foundation, and almost all of it is
Mac-testable, so it got built without touching the MSI.

**Measured: easing curves cost peak speed.** Sampled each curve at 20 000
points and took the steepest segment, as a multiple of the average rate:

| curve | peak / average |
|---|---|
| linear | 1.00x |
| ease_in_out | 1.571x (exactly pi/2) |
| ease_in, ease_out | 3.00x |
| every *_back curve | 4.70x |

This matters because safety caps every joint at 60 deg/s *in firmware*. Nine of
the thirteen gestures in BUILD_SPEC 6.3 ask for more than that:

| gesture | peak as specified | fits the cap at |
|---|---|---|
| shake | 218 deg/s | 1.35 s (spec 0.9 s) |
| startled | 147 deg/s | 1.2 s with linear easing |
| perk_up / celebrate | 146 deg/s | 1.45 s, split into rise + overshoot |
| nod | 131 deg/s | 1.1 s (spec 0.8 s) |
| excited_wiggle | 126 deg/s | 1.2 s (spec 0.9 s) |
| ahem, droop | 63 deg/s | 1.1 s / 1.7 s |
| curious_lean | 3600 deg/s | — that one was a bug, see below |

Going over the cap is not unsafe: the host limiter clamps and the firmware
clamps again. It is worse than unsafe, it is **silently wrong**. The clamp
smears the gesture into a different shape — a shake that cannot reach its
reversal in time reads as a slow sway — and nothing in the logs says so. Every
gesture is now the fastest version of its specified shape that fits 60 deg/s,
with the spec's own value in a comment, and `tests/test_motion.py` recomputes
the whole table from whatever `speed_cap_deg_s` says. Raise the cap and the
test tells you which gestures can be shortened again.

**The 3600 deg/s was a real bug.** `SequenceMove` discarded each leg's offset
at the seam between legs, so `curious_lean`'s "extend and hold" held nothing
and snapped back to base in one tick. It carries offsets forward now. This
would have been invisible on hardware: the arm would just have looked oddly
twitchy at the end of a lean, and the clamp would have hidden the spike.

**A second startup jump, by another route.** `setPWMFreq()` loads the
prescaler, which requires putting the PCA9685 to sleep, which stops the pulses
— for those milliseconds a loaded arm can sag. So firmware v2 reads `PRE_SCALE`
and `MODE1` first and only reprograms the frequency if it is not already right,
which it never is after a bare Uno reset (the PCA9685 keeps power and keeps
driving). It prints which path it took.

**Two independent fences, not one.** `avi_bot/motion/safety.py` clamps on the
host and the firmware clamps again from its own copy of the calibration table.
Two copies can drift, so `tests/test_firmware_agrees.py` parses the `.ino` and
compares every number, both degree-to-count maps over all 1801 tenth-degrees,
the speed cap, the watchdog, and the v1 channel-1 bug. It also asserts that
`setup()` writes no servo position at all.

**Elbow and wrist stay pinned.** Their direction signs are unknown, so the
limiter forces both to center and `nod` (wrist-only) does nothing on hardware.
`drive_undriven=True` previews the full motion in the sim only.

36 tests, all passing on the Mac, no GPU, no camera, no serial port:
`./scripts/test.sh`.

Still owed before M1 closes: the dashboard, and one live hardware run with
Shaurya watching. The arm has not moved.

## 2026-10-05 02:30 — two bugs found in the overnight run

Checked on the evening's logging run. It did not survive the night, and the
reason it did not was not the reason the laptop was still awake. Two separate
faults.

**1. The tracker died on a camera handoff, after 20 minutes.**

    [23:23:52] logger exited rc=0 after 1239s - GPU released
    RuntimeError: camera still returns blank frames after 8 s

The logger exited cleanly on its `--exit-after-away 900`, exactly as designed.
Then the tracker tried to take the camera back, and DirectShow had not released
it yet: `open_camera`'s 8-second blank-frame budget was not enough after a
20-minute hold, and the 1.5 s settle before re-acquiring was too short. Nothing
caught the exception — the loop only caught `KeyboardInterrupt` — so the process
exited rc=1 and the task went Ready. It happened twice.

Fixed: `acquire_camera()` retries with 2/4/8/16/30 s backoff, the settle is
2.5 s in *both* directions (the logger's process has to die before the device
frees), and each cycle is wrapped so a transient fault costs one cycle instead
of the run, giving up only after 5 consecutive failures. Unit-tested on the Mac
with `cv2` stubbed, including that it re-raises on the last attempt and does
not sleep after it: `tests/test_room_tracker.py`.

**2. The laptop stayed awake all night, and the keep-awake ceiling was a lie.**

The tracker was started with `TRACKER_KEEPAWAKE_MIN=120`, and the scheduled
task really was registered with `-Minutes 120`. But a keep-awake from 22:30 was
already running with the 480-minute default, and `Start-ScheduledTask` on an
already-running task does not start a second instance. So the new ceiling was
silently discarded and the old one won:

    22:30:23  keepawake START for 480 min (until 06:30:23)   <- the one that ran
    23:01     task re-registered with -Minutes 120           <- never started

The MSI was therefore held awake from 22:30 to 06:30 while the thing it was
being held awake *for* had been dead since 23:23. A keep-awake that ignores its
own ceiling is worse than no keep-awake, because the whole point is that this
machine does not stay on.

Fixed: `keepawake-ctl.ps1 start` now stops any running hold and kills its
process before re-arming, and prints the actual deadline it ended up with.
Verified by arming 60 minutes and then re-arming 15 while it ran:

    02:25:15  keepawake START for 60 min (until 03:25:15)
    02:25:25  keepawake START for 15 min (until 02:40:25)

The shorter ceiling now takes effect. Hold released, MSI asleep.

**Also corrected:** I had written in HANDOFF that the logger was deployed to
`avi-remote\assistant\`. It is not; `deploy.sh` puts it in `avi\assistant\`,
inside the `avi\` git repo, so it can import the GPU modules beside it. Only
our own helper scripts live in `avi-remote\`.

## 2026-10-05 09:40 — camera stream, and wake timers for being away from home

Shaurya asked for a live camera view while away. The MSI was asleep and there
was **no way to reach it**: wake-on-LAN needs a packet on the home broadcast
domain, his Mac was on 172.16.2.250 rather than 192.168.0.x, Tailscale carries
IP and not the layer-2 broadcast WoL needs, the MSI's Tailscale client is off
while it sleeps, and the tailnet holds only those two machines so there was
nothing at home to relay through. Somebody at home turned it on by hand.

**The stream works.** `scripts/stream.sh start 20`, then `http://msi:8080/`.
Measured: HTTP 200 in 43 ms, and a 4-second probe pulled 894 918 bytes across
40 frame boundaries — 10 fps at about 22 KB a frame. Frame size is also the
check that the image is not black without anyone having to look at it: a black
1280x720 frame compresses to roughly 2 KB. Windows Firewall does not block 8080
on the Tailscale interface, which was the one thing that could not be tested
from the Mac.

It binds to the Tailscale address only and refuses to start if it cannot find
one, rather than falling back to 0.0.0.0 and putting a camera that faces his
room on the whole house network.

**Wake timers installed**, so this cannot happen again: task `AVI wake`,
`WakeToRun`, holding 10 minutes per wake. Two-hourly from 08:05 to 22:05, plus
14:28 on Mon/Wed/Thu/Fri and 19:58 on Tuesdays for desk hours. Each wake runs
the same bounded keepawake hold the rest of the project uses, because a wake
with nobody present hits the hidden ~120 s unattended-sleep timeout and goes
straight back down; two minutes is not a window you can count on catching.

**A silent failure, caught by checking rather than assuming.** The first
version used one trigger repeating every two hours. The task installed fine and
reported `wakeToRun: True`, but `powercfg /waketimers` showed the next armed
wake at **14:27**, not 10:05:

    Timer set by [SERVICE] svchost.exe (SystemEventsBroker) expires at
    2:27:30 PM. Reason: Windows will execute 'NT TASK\AVI wake' scheduled task
    that requested waking the computer.

Windows arms a hardware wake for a trigger's **start boundary**, not for the
repetition occurrences beneath it. So the two-hourly grid would have fired only
when the machine was already awake — precisely the case it exists to cover —
and nothing in the task's own properties said so. Replaced with eight separate
daily triggers; the armed wake moved to 10:04:30, 21 minutes out. Always check
`powercfg /waketimers`, never the task's `WakeToRun` flag.

**Known limit, not fixed:** `allow-wake-timers` is AC=1, DC=0 on this machine.
Wake timers do not fire on battery. If the MSI is ever unplugged these timers
stop working with no indication, and remote access is gone again. Left alone
rather than changed unasked; `wake-timers.sh status` prints it and warns.

**Still unverified:** that a wake timer actually brings it up out of S3. The
hardware timer is armed, which is good evidence, but it has not been watched
from sleep to awake yet. Test it with margin at a window that is not adjacent
to a keepawake expiry.

## 2026-10-05 — M1 closed: every gesture runs on the real arm

Firmware v2 was flashed over SSH using the arduino-cli bundled inside Arduino
IDE 2.x, so nobody had to sit at the machine. All four joints are driven, all
six poses were taught on the arm with `calibrate.py`, and `gesture_check.py`
ran every gesture against the camera.

**13/13 moved the arm and the picture.** The check reports two numbers per
gesture and the pair is the point: *commanded* is the largest angle any joint
was actually told to move, taken from the limiter's output, and *observed* is
how much the picture changed, since the camera rides the arm. Together they
separate three failures that look identical in a log — a gesture being scaled
down, a servo not following, and a gesture working.

**Three gestures were unusably slow, and the easing curve was the whole cause.**

| gesture | before | after |
|---|---|---|
| yawn_wake | 16.01 s | 5.95 s |
| celebrate | 11.38 s | 6.70 s |
| perk_up | 10.18 s | 5.50 s |

A `*_back` curve costs 4.70x in peak speed and `ease_out` costs 3.00x, and they
were being applied across the *whole* 88-101 degrees between the taught poses,
against an arm that manages 27 deg/s. 92 x 4.70 / 27 is 16 seconds; there was
no bug, just arithmetic nobody had done. The travel now uses `ease_in_out` at
1.571x and the overshoot is a separate fixed-size leg, so it stays sharp
however far the arm had to come. Observed camera motion went *up* in all three
as a result, since faster motion changes the picture more.

**The oscillating gestures are tiny.** shake commands 4.5 deg, nod 3.6,
startled 3.2, excited_wiggle 2.7 — all about 45 % of their intended amplitude,
because that is what fits 27 deg/s while keeping their timing. They register on
camera (17-24) but they are twitches rather than gestures. Raising
`achievable_deg_s` once the 5 V 4 A supply is installed gives every one of them
its size back with no code change.

**What the taught poses say about the design.** TALL and HOME sit at nearly the
same shoulder angle, 78.4 against 72.5, and differ mainly at the wrist, 176
against 87.5. Alertness on this arm is carried by where the camera looks, not
by how tall it stands — which is not what BUILD_SPEC's "shoulder up, elbow
extended" assumed, and it is why the hand-written pose table was worth deleting
rather than tuning.

Also closed: the camera stream, teleop's driving and calibrate's pose teaching
are folded into one dashboard on port 8000, built on calibrate's interaction
model because that is the one that worked.

## 2026-10-05 19:25 — four ways not to measure a joint direction

`base_yaw`'s sign is still unverified, and this is the record of what does not
work so nobody spends a fifth evening on it.

| method | what happened |
|---|---|
| phase correlation, 15° move | peak response 0.00–0.07, i.e. no peak found. Reported 5.8 px for a move worth ~160 px, and gave two OPPOSITE commands the same sign. |
| phase correlation, 3° move | same, response 0.02–0.07. |
| dense Farneback flow | repeats of the same move disagreed by 17–78 px. |
| template matching, 3 patches | match scores 0.68–0.75 and repeats disagreeing by up to 165 px: it finds FALSE matches and one of them carries the median. |

**The scene is not the problem, and that was worth measuring separately.** With
the arm completely still, template matching shows 4 px of apparent movement at
match scores of 0.98. So the picture is stable to 4 px; what fails is
establishing correspondence across a shift of 50–150 px on a scene whose
features are not distinctive enough to be unambiguous. Every method above
degrades into a confident wrong answer rather than into silence, which is why
each one needed its own disproof.

**What to do instead: test the loop, not the geometry.** `look_at` only cares
whether its correction converges. Put a person or object in frame, run the
closed loop, and watch whether the error shrinks or grows; if it grows, flip the
sign. That cannot be confused by which frame of reference "right" means, and it
tests the thing that actually matters rather than a proxy for it. It needs M3's
person detector, which already exists on the MSI as part of the logger.

**Available sooner:** if Shaurya is sitting at the desk, YOLO person detection
gives an unambiguous box. Move the yaw a few degrees and see which way the box
moves. One run settles it.

Until then `config/joints.toml` keeps `sign = -1` for `base_yaw`, which is what
his hands-on report implies, and `look_at` must not ship on it.
