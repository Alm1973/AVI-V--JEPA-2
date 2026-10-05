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

## Open items

- `assistant.zip` is **not on this Mac** — searched the whole home directory. Blocks Phase 2
  and 4. Need it from Shaurya.
- `powercfg /hibernate off` — needs Shaurya's go-ahead.
- Away-from-home wake needs a relay on the home LAN (Raspberry Pi on Tailscale, or a router
  WoL button). Unanswered.
- Usual desk hours, for wake timers. Unanswered.
- `~/.ssh/msi_ed25519.pub` is not installed on the MSI (optional; the existing key works).
