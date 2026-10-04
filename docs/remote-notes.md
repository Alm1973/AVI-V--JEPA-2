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

### Keep-awake: **WORKING** (`scripts/keepawake-msi.sh`)

Self-expiring by design, as the doc asks — a forgotten session can't leave the laptop on.
Holds `ES_CONTINUOUS | ES_SYSTEM_REQUIRED`, re-asserts every 30 s, releases in a `finally`,
and has a hard 8-hour ceiling.

Runs as a **scheduled task** (`AVI keepawake`, user `SYSTEM`) because Windows OpenSSH kills
session processes on disconnect, which would drop the hold the moment we log out. SYSTEM
avoids needing a stored password.

Verified: `powercfg /requests` SYSTEM section shows the holding
`powershell.exe`, and the hold defeats the unattended-sleep timeout.

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

### 2. Camera from an SSH session: **not tested yet**

Needs `assistant/` on the MSI, and a snapshot must not leave the machine without Shaurya's OK.

### 3. Arduino on COM5: **not touched, by design**

Opening the port resets the Arduino and centres both servos with nobody watching.
Needs Shaurya's explicit go-ahead.

---

## Open items

- `assistant.zip` is **not on this Mac** — searched the whole home directory. Blocks Phase 2
  and 4. Need it from Shaurya.
- `powercfg /hibernate off` — needs Shaurya's go-ahead.
- Away-from-home wake needs a relay on the home LAN (Raspberry Pi on Tailscale, or a router
  WoL button). Unanswered.
- Usual desk hours, for wake timers. Unanswered.
- `~/.ssh/msi_ed25519.pub` is not installed on the MSI (optional; the existing key works).
