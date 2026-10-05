# AVI Desk Assistant: remote build from the Mac

Paste this into Claude Code, or save it as `CLAUDE.md` in the project folder so it loads on its own every session.

**What to build is defined in `BUILD_SPEC.md`**, a Dum-E-style desk companion with personality, beeps and daily-life skills. Read it next. This file covers remote setup, the machines and the gotchas. Where the two disagree, `BUILD_SPEC.md` wins.

## The goal

Build the AVI desk assistant **entirely from this Mac**. Shaurya does not want to sit at the MSI laptop. The MSI is the robot's brain (GPU, camera, arm). It sleeps when idle and should not stay on all the time. You work on it over SSH through Tailscale: wake it, deploy, run, check, put it back to sleep.

Shaurya is a high-school researcher. He wants short, to-the-point answers, with the answer first. Ask only the questions you actually need answered, one at a time.

---

## 1. Machines and network

| Item | Value |
|---|---|
| MSI laptop | Windows, hostname `MSI`, profile `C:\Users\MSI-User` (**the path has a space**), RTX 3060 Laptop GPU 6 GB |
| Tailscale on the MSI | installed, IP `100.x.y.z`, MagicDNS name `msi`, tailnet `your-tailnet.ts.net` |
| MSI Ethernet | Killer E3100G 2.5 GbE, MAC `XX-XX-XX-XX-XX-XX`. Wake-on-LAN settings are enabled (Wake on Magic Packet, both power-management boxes). **CONFIRMED WORKING** (6/6 wakes, 6-37 s, measured 2026-10-04). The cable is plugged in. `WakeOnPattern` was deliberately DISABLED on both adapters, with Shaurya's permission, because any incoming TCP SYN was waking the machine; `WakeOnMagicPacket` stays on. |
| Home LAN | 192.168.0.0/24, router 192.168.0.1, broadcast 192.168.0.255 |
| Robot | Arduino Uno + PCA9685, 4-servo arm (2 in use: base pan + shoulder; **the shoulder moves the whole camera**). Logitech C270 at camera index `1` (DirectShow), 1280×720. Serial port `COM5` (hard-coded in `arm.py`, `home.py`, `preflight.py`). |
| Parts ordered Oct 1 (not yet installed) | reSpeaker Lite USB 2-mic, Seeed 4 Ω 5 W speaker, 5 V 4 A servo supply + 2.1 mm screw-terminal adapter + in-line switch, Sabrent 4-port powered USB hub (one cable to the laptop), Kasa EP10 smart plugs, 1000 µF capacitor. (A NeoPixel Stick was also ordered but **is not used**.) |

## 2. Existing code

- **MSI working folder:** `C:\Users\MSI-User\avi`. It's a flat folder and a git repo, with years of experiment scripts and logs. Don't reorganize it.
  - GPU environment: `C:\Users\MSI-User\avi\.venv-yolo`. Python 3.13, torch 2.13.0+cu126, transformers 5.14.1, bitsandbytes 0.50.0, ultralytics 8.4.116, opencv 5.0.0. This is the **research environment that reproduces the paper's results. Don't install anything into it.** The new project gets its own Python 3.12 environment (see `BUILD_SPEC.md` section 4), because kokoro and RealtimeSTT need Python < 3.13.
  - Key modules: `config.py`, `camera.py`, `run_study.py` (`make_fast_verifier()` loads Qwen2.5-VL-3B in 4-bit NF4 and answers yes/no from first-token logits), `yolo_tracker.py`, `recovery_pipeline.py`, `arm.py`.
- **Public cleaned repo:** https://github.com/Alm1973/Adaptability-of-VLMs-in-Robotic-Control-Systems. The `avi/` folder is the final system from the paper. It's good for reading the code. The MSI folder is the live one.
- **New: `assistant/` observation logger** (milestone 4). Already unpacked into this repo (now `avi_bot/perception/`) and **verified on real hardware over SSH** on 2026-10-04: 15/15 self-checks, 575 ms warm VLM call, 3520 MiB peak VRAM, releases back to 39 MiB. Don't ask where `assistant.zip` is; it's in git.
  - Files: `settings.py`, `vlm.py`, `change.py`, `detector.py`, `memory.py`, `observer.py`, `today.py`, `audit.py`, `test_observer.py`, `README.md`.
  - Its `README.md` explains how it works. Read it before changing anything.

## 3. Background: the Stage 1 notes (`BUILD_SPEC.md` replaces these where they differ)

A desk robot that stays in one place, with no manipulation and no mobility. Camera and voice feed **one decision step**. It learns Shaurya's routines by watching, and learns facts from conversation. Perception runs locally on Qwen2.5-VL-3B. Conversation and planning now use a **free** cloud LLM, with text only (`BUILD_SPEC.md` section 8). Full design doc (Shaurya can open it, you can't): https://claude.ai/code/artifact/8880fcd3-b6c1-4108-8abc-48ccec42f90a

**Stack:**
- openWakeWord (CPU, Silero voice detection built in, custom "hey AVI" later)
- faster-whisper small.en int8 (CPU)
- Qwen2.5-VL-3B NF4 through Transformers (GPU)
- YOLO-World `yolov8s-worldv2.pt` (GPU)
- Piper TTS (`pip install piper-tts`, CPU)
- SQLite memory

**Design rules from Shaurya's own paper. Follow them:**
1. The VLM checks, it doesn't steer. Ask yes/no or pick-from-a-list questions and keep geometry in OpenCV. When asked where to look, Qwen answered "left" on 80 of 80 frames.
2. Call the VLM only on events (1.6% of frames, about 334 ms per call, against 29 ms for a normal frame).
3. Assume clutter. The detector reported a red cup on 62.6% of cup-absent frames in clutter. Nothing goes into memory without a VLM "yes."
4. Re-check confirmed objects on a timer, not only after a miss.
5. Memory answers include times ("last seen 4:12, left side"), never "it's there."
6. False belief (confident and wrong) is the worst error.
7. Validate on real days, not synthetic data.

**Each decision returns:** `say` + one `gesture` (look at you, look at object, perk up, nod, shake, rest) + at most one `action` from a whitelist (Kasa lamp, Windows media keys, timers and reminders, pre-approved apps). No shell commands.

**Speaking up on its own** needs all of these gates:
- routine seen on at least 6 of the last 10 matching days
- VLM score of at least 0.8
- at most 4 remarks a day, at least 45 minutes apart
- quiet hours 22:00–07:00
- nothing in the first 25 minutes of a work session
- no other speech heard (a call or conversation)

**Roadmap:**

| # | Milestone | Done when |
|---|---|---|
| 1 | Hardware + voice loop | wake word → STT → reply → Piper works 9 of 10 times |
| 2 | Seeing + talking together | answers "what's this?" correctly on 8 of 10 desk objects |
| 3 | Actions | each action works by voice, and requests outside the whitelist are refused |
| 4 | Observation logging (**do first**) | at least 80% agreement with Shaurya's labels on 100 audit thumbnails |
| 5 | Routines + memory | needs about 2 weeks of logs; beats a "same as yesterday" guess on held-out time slots |
| 6 | Speaking up + feedback | at least half of suggestions accepted over a week |

---

## 4. Plan

### Phase 0: SSH from the Mac to the MSI (one-time bootstrap)

You can't reach the MSI until its SSH server is on. This is the **only** thing Shaurya has to do on the MSI itself. He can do it through Chrome Remote Desktop if that's set up, or in person one last time.

1. On the Mac, generate a key: `ssh-keygen -t ed25519 -f ~/.ssh/msi_ed25519 -N ""`. Give Shaurya the public key.
2. Give Shaurya this block to paste into **PowerShell (Run as administrator)** on the MSI, with the key filled in:
   ```powershell
   Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
   Set-Service sshd -StartupType Automatic; Start-Service sshd
   New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force
   New-NetFirewallRule -Name sshd-tailscale -DisplayName "OpenSSH via Tailscale" -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress 100.64.0.0/10 -Action Allow
   $key = "PASTE_PUBLIC_KEY_HERE"
   Add-Content -Path C:\ProgramData\ssh\administrators_authorized_keys -Value $key
   icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
   powercfg /hibernate off
   powercfg /a
   whoami
   ```
   If his account is **not** an administrator, the key goes in `C:\Users\MSI-User\.ssh\authorized_keys` instead. `powercfg /a` shows whether the MSI uses Modern Standby ("S0 Low Power Idle"). That changes how wake and sleep behave, so note it. Turning hibernate off makes the sleep command below sleep instead of hibernate.
3. Mac `~/.ssh/config`, using the username from `whoami` (quote it if it has a space):
   ```
   Host msi
     HostName msi
     User "<windows username>"
     IdentityFile ~/.ssh/msi_ed25519
     ServerAliveInterval 30
   ```
4. Verify: `ssh msi "hostname; nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv"`.

### Phase 1: Wake and sleep from the Mac

Put these in `scripts/` in the Mac project:

- **`wake-msi.sh`:** send a Wake-on-LAN magic packet to `XX:XX:XX:XX:XX:XX`, then poll `ssh msi exit` for up to about 90 s.
  - **On the home network:** send it straight from the Mac, as UDP broadcast to `192.168.0.255:9`.
  - **Away from home:** the packet has to start on the home network. **Ask Shaurya** which he has: a Raspberry Pi 5 at home on Tailscale (`ssh pi wakeonlan XX:XX:XX:XX:XX:XX`), or a router app with a Wake-on-LAN button.
  - First, confirm Wake-on-LAN works at all, with the Ethernet cable plugged in. If it doesn't, check Energy Efficient Ethernet (turn it off) and Modern Standby.
- **`sleep-msi.sh`:** `ssh msi "rundll32.exe powrprof.dll,SetSuspendState 0,1,0"`. Stop the observer first.
- **Staying awake during a work session:** SSH activity does **not** reset Windows' idle timer, so the MSI can fall asleep mid-session. Use a keep-awake that **expires on its own**, so a forgotten session can't leave the laptop on forever. For example, a scheduled task that calls `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` for N minutes. Prefer this over changing power-plan timeouts.
- **Fallback if Wake-on-LAN won't work:** wake timers. Enable "Allow wake timers" (`powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1`) and register a scheduled task with `-WakeToRun` at the times Shaurya usually works.

### Phase 2: Dev loop (the Mac is the source of truth)

- The Mac project folder (git) holds the code (`BUILD_SPEC.md` section 13 has the layout), `scripts/` and this file. Never leave code only on the MSI, and don't edit files there by hand.
- **`deploy.sh`:** copy the project to `C:\Users\MSI-User\avi-desk\`, using `scp` over the `msi` host. The path has a space, so quote it carefully and test it. Create the Python 3.12 environment there (the MSI has miniconda3) the first time.
- **Run remotely:** `ssh msi "cd 'C:\Users\MSI-User\avi-desk'; & '<path to the 3.12 env>\python.exe' -m pytest -q"`
- **Long-running processes:** Windows OpenSSH can kill processes started in a session when that session ends. Start anything long-running (the observer, keep-awake) as a **scheduled task in Shaurya's interactive session** ("run only when user is logged on"), triggered with `schtasks /run /tn <name>`. Have it write logs to a file you can tail over SSH.
- Pull `desk_memory.db` back to the Mac with `scp` when you need to analyze it.

### Phase 3: Check hardware access over SSH (test it, don't assume)

Record each result in `docs/remote-notes.md`:

1. **GPU from an SSH session:** `torch.cuda.is_available()`, free VRAM. The logger needs about 3.8 GB.
2. **Camera from an SSH session:** open `cv2.VideoCapture(1, cv2.CAP_DSHOW)` and read one frame. This may fail outside the interactive desktop session (session isolation or camera privacy settings). If it does, run camera work through the interactive scheduled task above. Copy a test snapshot to the Mac only with Shaurya's OK, since the camera faces his room.
3. **Arduino on COM5:** **don't open it without Shaurya's explicit go-ahead.** Opening the port resets the Arduino and drives both servos to center, and nobody is there to watch. Keep any remote motion test small and slow.

### Phase 4: Get the logger running (start right after M0, alongside M1)

Routine learning needs about 2 weeks of logs, so start logging early, even before the personality work. At M3 the logger gets folded into `avi_bot/perception`.

1. Deploy, then run `test_observer.py`. Expect `15 passed`.
2. Add a `--once` flag to `observer.py` (one observation, print it, exit) for remote testing. Then run it and check:
   - startup doesn't raise the "token id" error (the letter and yes/no token checks)
   - each observation takes about 1–2 s
   - VRAM use is about 3.8 GB
3. Run it as the interactive scheduled task "AVI observer" with `--no-preview`, logging to a file. Add `scripts/observer.sh start|stop|status|today` on the Mac.
4. **Open design question, ask Shaurya before building:** the logger only learns while the MSI is awake and he's at the desk, but he wants it asleep when idle. Proposal:
   - wake timers at his usual desk hours, which you need to ask him for
   - the observer puts the MSI to sleep after about 45 minutes of "not at desk" outside those hours
   - he can wake it manually any time
5. Have him log for a few days with `--audit-rate 0.05`, then run `audit.py`. The goal is at least 80% agreement on 100 labels.

### Phase 5 and on: remaining milestones, built to be tested remotely

- Give every hardware piece a **file-based test mode**, so you can test without anyone at the desk:
  - audio input from WAV files
  - TTS output to WAV (copy it back to the Mac to listen)
  - camera input from recorded clips
  - servo commands in a dry-run mode that logs instead of moving
- **Milestone 1 needs the new parts plugged in.** That's the one physical step that can't be avoided. When the parts arrive, give Shaurya a short wiring checklist:
  - 5 V 4 A supply → in-line switch → screw adapter → PCA9685 V+/GND. Never connect the supply's + to the Uno.
  - 1000 µF cap across V+/GND
  - reSpeaker, C270 and Uno → powered USB hub → one cable to the laptop
  - speaker → reSpeaker speaker port, so echo cancellation works
  - servos never powered from the hub
- Then follow the milestones in `BUILD_SPEC.md` section 15. They replace the Stage 1 roadmap above.

---

## 5. Gotchas (all learned the hard way)

- **6 GB VRAM.** Never run ComfyUI or other `avi/` experiments alongside the observer.
- **Qwen3-VL-4B segfaulted** while loading in 4-bit with transformers 5.14.1 + bitsandbytes 0.50.0. Stay on Qwen2.5-VL-3B.
- **llama.cpp / Ollama + qwen2.5-vl on CUDA gives degenerate output** (upstream issue #14663). Use Transformers.
- **YOLO-World "water bottle" / "bottle" confidences are inverted.** They're excluded from the object list on purpose.
- **The shoulder servo moves the camera.** Any gesture must set the self-motion flag (`ChangeDetector.suppress()`).
- **Privacy:** the camera watches Shaurya's room. Keep everything local and never upload frames. Frames are saved only as opt-in audit thumbnails.
- **Shaurya's research habits:** pre-register metrics, keep false belief separate from accuracy, log everything, and check for silent failures. He has caught 13 measurement bugs this way.

## 6. Your first moves

1. Check whether `ssh msi` works. If it doesn't, run Phase 0: generate the key and hand Shaurya the bootstrap block.
2. Ask, one at a time and only when needed:
   - Where is `assistant.zip` on this Mac?
   - Raspberry Pi or router app for waking the MSI away from home?
   - What are his usual desk hours?
3. Set up the Mac project folder with git, then go phase by phase. Tick off steps in `docs/remote-notes.md` as you finish them.


---

## Measured reality (added by Claude Code, 2026-10-04)

M0 is **done**. `docs/remote-notes.md` is the evidence log; `docs/HANDOFF.md` is the current
state and the next action. Read HANDOFF first, it is short.

Facts measured here that override anything stale above or in `BUILD_SPEC.md`:

- **Wake works.** `scripts/wake-msi.sh`, 6/6, 6-37 s. The Mac must be on the home LAN
  (192.168.0.0/24) for the magic packet to reach the MSI; Tailscale alone cannot wake it.
- **Sleep is the hard direction, and it is solved.** Use `scripts/sleep-msi.sh`. Do not
  "simplify" it: the suspend must be detached via `Start-Process` (a direct
  `ssh msi rundll32 ...` stalls ~180 s and never suspends), the wait must be *quiet*
  (probing port 22 wakes the machine back up), and `SetSuspendState` silently no-ops often
  enough to need the retry loop.
- **Windows kills session-owned processes when SSH disconnects.** Anything long-running goes
  through `scripts/remote/task-ctl.ps1` as a scheduled task. This is also what BUILD_SPEC
  section 4 means by "interactive scheduled task."
- **There is a hidden unattended-sleep timeout** (~120 s after a wake with nobody present).
  It is invisible in `powercfg /q`. `scripts/keepawake-msi.sh` holds the machine up with a
  self-expiring `SetThreadExecutionState` hold; always bound it with a minutes ceiling so the
  MSI puts itself to sleep when you are done.
- **Always pass `python -u`** for anything whose log you intend to read. Block-buffered
  stdout makes a healthy run look hung.
- **The C270's first ~5 frames are black with `ok=True`.** `open_camera()` already loops
  until `frame.mean() >= 1.0`; keep that.
- **Never diagnose a frozen camera from the downscaled change-detector frames.** They sit at
  max-diff 1-2 on a perfectly live stream. Compare raw 1280x720 frames.
- **Don't presume the camera sees a desk.** A label set that said "nobody is sitting at the
  desk" as its only fallback scored 0.82-0.97 on frames with a person plainly in shot. The
  system must work wherever it is pointed. `ACTIVITIES` in `avi_bot/perception/settings.py`
  is the pre-registered metric for the 80 %-agreement audit: it is now **frozen**.
- **`audit.py` needs a GUI window** (`cv2.imshow`), so it cannot run over SSH as written.
  A remote audit path is still owed. See HANDOFF.
