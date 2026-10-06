# AVI: build spec for a Dum-E-style desk companion

**For Claude Code.** Read `CLAUDE.md` first (remote setup, machines, gotchas), then this file. This spec is the product definition. Where it differs from the Stage 1 notes in `CLAUDE.md`, this file wins.

Build it milestone by milestone (section 15). Every milestone ends with a test you run and a short note in `docs/remote-notes.md`. Ask Shaurya only when you're blocked on a decision or a physical step. Keep messages short, with the answer first.

---

## 1. What AVI is

AVI is a robot arm that lives on Shaurya's desk and acts like a loyal, eager, slightly over-enthusiastic workshop helper, in the spirit of Dum-E. It can't grab things. Its usefulness comes from **watching, remembering, reminding and controlling things**, and its charm comes from **how it moves and chirps**.

**A normal afternoon:**
1. Shaurya sits down at 4:05. The arm perks up and gives a happy two-note chirp.
2. It glances at his calendar and says one sentence: "Chem quiz tomorrow, essay draft due Friday."
3. He says "focus 45." The arm settles into a calm, still posture, and the lamp turns on.
4. Twenty minutes in, he picks up his phone. A minute later the arm slowly turns to look at the phone and makes a short "ahem" chirp.
5. He says "where's my calculator?" The arm swings toward the left side of the desk: "Saw it there at 3:58."
6. When the timer ends, it does a little celebration wiggle and asks with a rising chirp whether he wants a break.
7. He says "good job." It bounces.
8. Later he snaps "stop" while it's fidgeting. It droops and sulks for a few minutes.

## 2. Hard constraints

| Constraint | What it means for the build |
|---|---|
| **Free only** | No paid APIs, ever. Never add a credit card to any provider. Use only free tiers and local models. If a provider starts requiring billing, drop it from the chain. |
| **Hybrid brain** | Camera, audio, memory and all perception stay on the MSI. Only short **text** (a transcript plus a compact context) goes to a free cloud LLM. |
| **Mostly beeps** | Emotion and acknowledgement are chirps plus motion. Speech only for information he asked for, reminders, and the morning rundown. One sentence, 20 words max. |
| **Remote build** | Shaurya builds from his Mac. Everything needs a simulator or a file-based test mode. Hardware motion needs his go-ahead (see `CLAUDE.md`). |
| **Hardware is fixed** | The existing 4-servo arm with the C270 camera on the wrist, the reSpeaker Lite mic + speaker, and a Kasa plug for the lamp. **No gripper, no NeoPixel, no new sensors.** |
| **6 GB VRAM** | Qwen2.5-VL-3B (4-bit) + YOLO-World is about 3.8 GB. Speech-to-text and TTS run on the CPU. |
| **Privacy** | No images or audio leave the MSI. Frames are never written to disk except opt-in audit thumbnails. Every observation can be paused, guest mode and wiped. |
| **Safety** | Joint limits and speed caps are enforced **in firmware**. A watchdog holds position if the host goes silent. No surprise motion on startup. |

## 3. Hardware facts (from `docs/servo-calibration.md` and the firmware in the AVI repo)

**Servos** (PCA9685 channels; raw PWM counts, bench-calibrated 2026-08-04):

| Channel | Joint | Min | Center | Max | Today |
|---|---|---|---|---|---|
| 0 | Base yaw (look left/right) | 75 | 250 | 460 | driven |
| 1 | Shoulder (whole arm up/down) | 75 | 370 | 580 | driven, but the firmware wrongly uses the wrist's range (140/380/580) |
| 2 | Elbow | 80 | 430 | 570 | **not driven** |
| 3 | Wrist / camera tilt | 140 | 380 | 580 | **not driven** |

- **Current firmware** (`firmware/servo_controller/sketch_jul1a.ino`): relative `X:<deg> Y:<deg>` lines at 115200 baud, a 12 ms easing loop (proportional with a ~37°/s cap), and a piecewise angle→pulse mapping around the calibrated center. Opening the port resets the Uno, which snaps the servos to 90/90.
- **Direction already confirmed:** `arm.update(+10, 0)` moves the view right; `arm.update(0, +10)` moves the view down. The directions of the elbow and wrist are **unknown**, so measure them (M1).
- **Camera on the wrist:** every move changes the view. Moving the shoulder *translates* the camera rather than rotating it.
- **No roll joint:** it can't do a sideways head tilt. Make "curious" from a lean-in plus a wrist pitch instead.
- **Servo power** comes from a separate supply (a UBEC today, and a 5 V 4 A supply is on order). Grounds are shared. Servos are never powered from USB.

## 4. Architecture

```
            ┌──────────── MSI (Windows, one Python 3.12 process "avi") ────────────┐
 C270 ──▶ CameraService ─▶ ChangeDetector ─▶ Observer (logger) ─▶ Memory (SQLite)
              │                 │                 │
              │                 └─▶ events ──┐     └─▶ sightings, sessions, routines
              ▼                              ▼
          Perception: YOLO-World + Qwen2.5-VL verifier (local GPU)
                                             │
 reSpeaker ─▶ Hearing: openWakeWord ─▶ VAD ─▶ faster-whisper (CPU) ─▶ Transcript
                                             │
                                     ┌───────▼────────┐
                                     │   EVENT BUS    │◀── Scheduler (timers, calendar, focus)
                                     └───┬────────┬───┘
                                         │        │
                          Personality engine   Brain router ──▶ local intents (rules + Qwen choice)
                          (mood, attention,        │       └──▶ free cloud LLM chain (text only)
                           behavior selection)     ▼
                                         │      Skills (tools): timers, calendar, focus, find,
                                         │      lamp, media, apps, weather, memory, look_at...
                                         ▼
                    Expression: Motion engine (50 Hz) ──▶ Serial ──▶ Uno + PCA9685 ──▶ 4 servos
                                Voice: beep synth / Kokoro TTS ──▶ reSpeaker speaker
                    Dashboard (web, port 8000, over Tailscale): camera, joints, mood, logs, sim view
```

**Process model:** one Python process.
- An asyncio event loop runs the bus, brain and skills.
- Dedicated threads run the motion loop (50 Hz), camera capture, audio in and audio out.
- It's started as an **interactive scheduled task** on the MSI, so it can reach the camera, audio and the desktop. The SSH notes in `CLAUDE.md` explain why.
- Logs go to `logs/avi.log`.

**Python environment:** a new venv or conda env with **Python 3.12**.
- `kokoro` and `RealtimeSTT` need Python < 3.13, and `.venv-yolo` is 3.13.
- **Don't install anything into `.venv-yolo`.** It reproduces the paper's results.
- Pin the versions known to load Qwen2.5-VL in 4-bit on this laptop: torch 2.13.0+cu126, transformers 5.14.1, bitsandbytes 0.50.0. Then add the new packages.

## 5. Personality engine (the heart of it)

### 5.1 Character sheet (`personality/avi.md`)

The format is adapted from Reachy Mini profiles: TOML front matter lists the allowed tools, and markdown sections hold the prompt.

- **Identity:** AVI, Shaurya's desk robot. Eager, loyal, curious, a bit over-enthusiastic. Tries hard and is proud of small wins.
- **Communication:** chirps first. Speaks only when it has information to give: one sentence, dry and funny at most, never rambling.
- **Manners:** stays quiet while he's focusing, never nags twice, accepts "no" gracefully. A short sulk after a scolding is fine; meanness is not.
- **Original character.** Don't copy movie dialogue, sound effects or catchphrases.

### 5.2 Mood state (updated on events, fading back to baseline over time)

| Variable | Range | Baseline | Half-life | Meaning |
|---|---|---|---|---|
| `valence` | −1…1 | +0.2 | 15 min | unhappy ↔ happy |
| `arousal` | 0…1 | 0.3 | 3 min | calm ↔ excited |
| `social` | 0…1 | 0.2 | rises when ignored | wants attention |
| `energy` | 0…1 | follows the time of day | n/a | sleepy at night and in quiet hours |
| `sulk_until` | timestamp | none | n/a | set by a scolding |
| `curiosity_target` | object or zone | none | 60 s | something new to look at |

**Events → mood changes** (starting values; tune them):

| Event | Effect |
|---|---|
| praise ("good job", "nice", "thanks AVI") | valence +0.4, arousal +0.3, clears `sulk_until` |
| scold ("bad", "stop it", "no!") | valence −0.5, `sulk_until` = now + 5 min |
| Shaurya arrives (person verified after an absence of 10 min or more) | valence +0.2, arousal +0.4, social → 0 |
| Shaurya leaves | arousal −0.2 |
| task succeeds | valence +0.2 |
| can't do it / doesn't know | valence −0.1, plays "embarrassed" |
| new object on the desk (verified sighting, not seen in 24 h) | arousal +0.2, `curiosity_target` set |
| no interaction for 45 min while he's present | social +0.1 every 15 min |
| focus session starts | arousal forced down, quiet mode on |

**Emotion label** (picked from the mood each tick):
- excited: valence > 0.3, arousal > 0.6
- happy: valence > 0.3
- content: valence > 0, arousal < 0.3
- curious: `curiosity_target` is set
- bored: social > 0.6, arousal < 0.3
- sulky: `sulk_until` is in the future
- sad: valence < −0.2
- sleepy: energy < 0.3

### 5.3 Attention system

It picks one thing to look at, by salience:
1. Shaurya speaking (wake word or voice activity).
2. Motion near him.
3. `curiosity_target`.
4. Shaurya's face.
5. A random spot (idle gaze).

Look-at uses closed-loop visual servoing like the AVI tracker: small yaw and wrist-pitch corrections from the target box's offset from image center. A critically damped spring smooths it so it never snaps.

### 5.4 Behavior selection (when no task is running)

Every 2–6 s (random), score these behaviors:
- stay still (breathing only)
- glance at you
- look around (a saccade sweep)
- inspect the curiosity target
- fidget
- doze
- greet
- sulk-droop
- celebrate

`utility = weight(emotion) × context × cooldown`. Then sample with a softmax at temperature 0.5. Reachy Mini's idle policy rests 60% of the time, so stillness should dominate here too.

Context multipliers:
- **Focus mode:** only stillness and an occasional slow glance.
- **Quiet hours** (22:00–07:00): doze only.
- **Away:** rest pose, with the slow heartbeat observer.

### 5.5 Expressive and functional motion (from Apple's ELEGNT research)

Every functional move also says something.

- **Intention:** look before acting. Turn toward the lamp, then switch it.
- **Attention:** face whoever is talking.
- **Attitude:** a confident yes is quick and crisp; an unsure one is slow, with a pause.
- **Emotion:** the same move gets a different style depending on mood. Happy is bouncy and faster; sad is slower and lower; sulky is reluctant and delayed.

ELEGNT found expressive movement mattered most in social moments and barely at all in purely functional tasks. So don't add flourishes to commands Shaurya gives while he's focusing.

## 6. Motion engine

### 6.1 Joints and poses

Work in degrees per joint, centered at 90, with per-joint soft limits stored in `config/joints.toml` and set inside the calibrated range. Named poses (tune them on hardware):

| Pose | Use |
|---|---|
| `HOME` / `OBSERVE` | neutral, camera on the desk. **The observer only logs object positions from this pose.** |
| `ATTENTIVE` | slightly raised, camera toward where Shaurya sits |
| `TALL` | alert: shoulder up, elbow extended, wrist level |
| `LOW` | shy or sulky: shoulder down, elbow curled, wrist pitched down |
| `REST` | sleep: folded low, camera down |

### 6.2 Layers (structure adapted from Reachy Mini's `moves.py`)

- **Primary layer:** a queue of moves (gestures, go-to-pose, look sequences). Only one runs at a time. Switching between moves cross-fades over 0.3 s.
- **Secondary layers** are added on top and weighted by state:
  - **breathing:** a 0.1–0.2 Hz sine on shoulder and elbow, about 1–2°
  - **saccades:** a small random yaw/pitch offset every 1–4 s, drawn from a normal distribution, as in Stack-chan's `saccade.ts`
  - **look-at:** the spring-smoothed tracking target
  - **speech bob:** a small wrist pitch that follows the speech or beep loudness
- **Final pose** = clamp(base pose + primary + Σ secondary), sent at 50 Hz.
- **Style modifiers from mood:** speed scale, amplitude scale and "bounce." Bounce turns on an overshoot easing curve (ease-out-back), and anticipation uses ease-in-back. Write the easing functions yourself (about 20 lines) or use `pytweening` (MIT).

### 6.3 Gesture library (`motion/gestures.py`)

Each gesture is a short keyframed curve. The values are starting points.

| Gesture | Motion | Duration |
|---|---|---|
| nod | wrist pitch −8° → +4° → 0, twice | 0.8 s |
| shake | base yaw ±10°, twice | 0.9 s |
| perk_up | small dip (anticipation), then up to `TALL`, slight overshoot | 0.7 s |
| droop | slow ease down to `LOW`, wrist down last | 1.5 s |
| curious_lean | elbow and shoulder extend toward the target, wrist pitch eases, hold | 1.2 s + hold |
| excited_wiggle | fast ±6° yaw and wrist bob, 3 cycles | 0.9 s |
| celebrate | perk_up + excited_wiggle + happy chirp | 1.8 s |
| startled | quick pull-back 6°, freeze 0.3 s, slow return | 1.2 s |
| thinking | slow look up and to one side, pause, return | 1.5 s |
| embarrassed | wrist down, small yaw away, slow return | 1.4 s |
| ahem | slow turn toward the target, small nod | 1.0 s |
| doze | ease to `REST`, deeper breathing | continuous |
| yawn_wake | slow rise from `REST` with a stretch overshoot | 2.0 s |

### 6.4 Firmware v2 (`firmware/avi_servo_v2/avi_servo_v2.ino`)

Rewrite the sketch. Keep the existing easing idea and the piecewise mapping around the center.

- **Protocol** (115200 baud, newline-terminated):
  - `P a0 a1 a2 a3` — absolute target angles in degrees, sent at about 50 Hz by the host. The firmware still eases toward them with a per-joint speed cap.
  - `H` — go home slowly.
  - `?` — reply `S a0 a1 a2 a3 v2`.
  - `E` — hold the current position and ignore `P` until `R` (resume).
- **Safety in firmware:**
  - Clamp every channel to its calibrated min/max (table in section 3).
  - Speed cap of about 60°/s per joint.
  - Watchdog: no `P` for 500 ms → hold the last position. Never move on its own.
- **No jump on startup:**
  - (a) On boot, try reading each channel's current PWM register back from the PCA9685, which keeps outputting while powered. Ramp from there instead of snapping to center. **Verify this on the bench before relying on it.**
  - (b) Otherwise, wait for the first `P` and ramp at a low speed.
  - (c) Optional hardware fix: a 10 µF capacitor between the Uno's RESET and GND stops the reset when the port opens. Remove it to upload a new sketch.
- **Fix the bug:** channel 1 uses the shoulder's range, not the wrist's.

### 6.5 Backends and simulation

Use a `MotionBackend` interface with three implementations:
- `SerialBackend` — the real arm.
- `SimBackend` — keeps the joint state and draws a 2D side view plus a top view in the dashboard (canvas), from joint angles and approximate link lengths set in `config/joints.toml`.
- `DryRunBackend` — logs commands only.

**Every gesture and behavior must run in sim before it touches hardware.** Shaurya can watch the sim in the dashboard from his Mac.

### 6.6 Coupling with perception

- Every move calls `ChangeDetector.suppress(until=move_end + 0.5 s)`, so self-motion never counts as a scene change.
- Store the current pose on every observation. Object zones ("left side") only count from `OBSERVE`.
- The observer pauses while the arm is away from `OBSERVE` for long gestures.

## 7. Voice and hearing

### 7.1 Beep synthesizer (`voice/beeps.py`, original and procedural, no samples)

Each phrase is a chain of syllables.
- **Syllable:** a sine or triangle tone (plus some 2nd harmonic), 40–180 ms long, with a pitch glide from a start to an end frequency, optional vibrato, and an ADSR envelope.
- **Gaps:** 20–80 ms.
- Randomize within each preset's ranges on every call, so it never repeats exactly.
- Output at 24 kHz, the same rate as Kokoro.

| Emotion | Syllables | Pitch | Contour | Feel |
|---|---|---|---|---|
| happy | 2–3 | 900–1600 Hz | rising | bright, quick |
| excited | 4–6 | 1000–2200 Hz | fast trills | rapid |
| affirm | 2 | 800 → 1200 Hz | up | crisp |
| deny | 2 | 1100 → 700 Hz | down | firm |
| question / curious | 1–2 | ends high | rising end | — |
| sad | 2 | 700 → 400 Hz | slow fall | soft |
| sulky | 1 | 300–450 Hz | flat, square-ish | short "hmph" |
| ahem | 2 | 500 Hz, then 650 Hz | — | soft, polite |
| sleepy | 1 | 600 → 300 Hz | long glide | quiet "yawn" |
| alert / reminder | 3 | 1300 Hz ×3 | — | clear, a bit louder |

There's a volume setting, quiet hours mute everything except reminders flagged urgent, and an `--export` option renders every preset to WAV so Shaurya can listen on the Mac. **Don't use** `ttastromech` or any movie sound effects. The first bundles sound samples of unclear origin.

### 7.2 Speech

- **Kokoro-82M** (Apache-2.0) on the CPU. On Windows it needs espeak-ng installed (GPL-3, a separate program). The fallback is **Piper** (`piper-tts`, GPL-3).
- Measure how fast it runs. If Kokoro is slower than real time on this CPU, switch to Piper.
- Optional: a light robotic tint on the voice (a subtle ring modulator), behind a setting.

**Speech policy:**
- Speak only to: answer a question with information, read a reminder or schedule, give the morning rundown, or explain an error once.
- 20 words max. Split into sentences and stream them.
- Everything else gets a chirp.
- The brain returns `say` and `beep` separately, and the policy enforces the rules.

### 7.3 Hearing

- **RealtimeSTT** (MIT) handles the wake word, voice activity detection and faster-whisper transcription. Check its README for the current parameter names.
- Wake word: start with openWakeWord's pre-trained **"hey jarvis"**, a fitting Iron Man nod. Those models are CC BY-NC-SA, which is fine for personal use. Train a custom **"hey AVI"** in openWakeWord's Colab at M7.
- Speech-to-text: faster-whisper `small.en`, int8, on the CPU.
- Echo: all playback goes through the reSpeaker, so its echo cancellation has a reference. Also ignore the wake word while AVI is playing sound, plus 300 ms after.
- **Test mode:** feed WAV files instead of the mic (`fixtures/audio/*.wav`). Generate fixtures with TTS so they contain no personal audio.

## 8. Brain (free hybrid)

### 8.1 Router (in order)

1. **Local rules (instant, offline):** regex and keyword intents for the frequent commands:
    - timers, focus start/stop
    - lamp on/off, music, volume
    - "where's my X", what time is it
    - praise and scold
    - sleep/wake, quiet, guest mode, stop
2. **Free cloud LLM with tools** for everything open-ended.
3. **Fallback if the cloud fails:** local Qwen picks from the intent list using the same first-token multiple-choice method as the logger. If nothing fits, a "confused" chirp plus a shake.

### 8.2 Provider chain (OpenAI-compatible, one client: the `openai` SDK with a different `base_url` per provider)

| Order | Provider | Model (verify current IDs) | Free limits (Aug–Oct 2026) | Notes |
|---|---|---|---|---|
| 1 | Groq | `openai/gpt-oss-20b` (fast); `llama-3.3-70b-versatile` | no card; about 30 requests/min. Daily limits vary by model, so check the console. | **No data retention by default.** Turn on Zero Data Retention in its console. |
| 2 | OpenRouter | a current `:free` model with tool support | 20/min, **50/day** | Some free upstream models log or train, so strip personal facts from what's sent here. |
| 3 | GitHub Models | a low-tier model with tool calling | 15/min, 150/day, 8k input tokens | Uses his GitHub account. |
| 4 | local Qwen2.5-VL-3B | n/a | unlimited | simple intents only |

- **Base URLs** (confirm against the docs at build time): Groq `https://api.groq.com/openai/v1`, OpenRouter `https://openrouter.ai/api/v1`, GitHub Models `https://models.github.ai/inference`.
- Keys go in `.env`, never in git.
- Gemini's free tier is **not** used by default. Its free-tier prompts may be used to improve Google products, and Shaurya's Google account hit restrictions with Gemini earlier.
- Check each provider's terms before signing up. Some require users to be 18 or older.
- **Budget guard:** count requests per provider per day in SQLite and stay under 80% of the free limit. On HTTP 429 or a timeout, move to the next provider. Never upgrade to a paid plan.
- **Context sent:** personality prompt, date and time, the current activity plus verified objects (as text), the mood label, the next 3 calendar events, up to 10 matching facts, and the last 6 turns.
- **Never sent:** images, audio, raw logs, the calendar URL, keys.
- **Output contract:** tool calls for actions, then a final JSON reply `{"say": str|null, "beep": emotion|null, "gesture": name|null}`, validated with pydantic. If it's invalid, retry once, then fall back to the "confused" chirp.

## 9. Skills (each is a tool with a JSON schema, used by both the local router and the LLM)

| Skill | What it does | Test |
|---|---|---|
| `set_timer` / `cancel_timer` / `list_timers` | Timers stored in SQLite, so they survive restarts. When one ends: alert chirp, then "Timer: <label>". | a 5 s timer fires, and still fires after a restart |
| `set_reminder(when, text)` | Natural time ("at 5", "in 20 min") parsed by the LLM or `dateparser` | 10 phrasings parse correctly |
| `get_schedule(day)` | Reads Google Calendar through its **secret iCal address**. It's read-only and needs no login. Polled every 10 min with `icalendar` and `recurring-ical-events`. Reminds 10 min before events, and gives a 3-item **morning rundown** the first time he sits down each day. | a fixture `.ics` file gives the right events and the right reminder times |
| `focus_start(min)` / `focus_stop` / `focus_status` | Quiet companion mode, plus a lamp-on option. **Phone check:** YOLO-World proposes "cell phone," then the VLM is asked "Is the person holding or using a phone?" Phone use over 2 min → an `ahem` (glance + chirp). Second time → one short sentence. At most 2 nudges per session. Suggests a break at 50 min. Spoken summary at the end. | replay fixture frames → the right nudges at the right times |
| `find_object(name)` | Looks up the latest verified sighting from `OBSERVE` (with synonyms, e.g. phone → cell phone). Speaks the time and zone, then looks toward that zone. Not seen today → shake + "Haven't seen it today." | a seeded database gives the right answer and the right yaw |
| `lamp(on/off/toggle)` | `python-kasa` against the plug's IP. Credentials from `.env` if newer firmware needs them. | runs in a mock; real plug once Shaurya is home |
| `media(action)` | Windows media keys through `ctypes` (play/pause 0xB3, next 0xB0, previous 0xB1, volume 0xAF/0xAE, mute 0xAD) | key sent in the interactive session |
| `open_app(name)` | Whitelist in `config/apps.toml` (name → path or URL) only | an unlisted app is refused |
| `weather(day)` | Open-Meteo, free with no key for personal use; requires CC BY 4.0 attribution in the README. Phoenix coordinates in the config. | fixture JSON → the right sentence |
| `remember` / `forget` / `what_do_you_know` | Facts table: at most 100 facts, 280 characters each (pattern from Reachy Mini `memory.py`) | round trip |
| `look_at(target)` / `play_emotion(name)` | Lets the LLM express itself | sim shows the motion |
| `robot_status` | Mood, uptime, VRAM, provider health, today's request counts | — |
| `sleep` / `wake` / `quiet(min)` / `guest_mode` | Rest pose with the observer paused (sleep), only nonverbal (quiet), no logging (guest) | — |

## 10. Memory and learning

Reuse the `assistant/` logger tables (`observations`, `sightings`, `sessions`) and add:
- `facts(id, text, created_at, source)`
- `reminders(id, due_ts, text, kind, status)`
- `interactions(ts, utterance, route, provider, tools, latency_ms, outcome)`
- `feedback(ts, suggestion, response)`
- `mood_log(ts, valence, arousal, social, emotion)`
- `provider_usage(date, provider, requests)`

Routines are mined nightly from `sessions` (6 of the last 10 matching days, as in the Stage 1 design). Preferences grow from praise and scolding (which behaviors get praised more, which get stopped) and from which suggestions he accepts. Behavior weights shift slowly over weeks.

## 11. Speaking up on its own (nonverbal first)

Start with the Stage 1 gates:
- routine seen on at least 6 of the last 10 days
- activity confidence of at least 0.8
- he's present
- at most 4 *spoken* remarks a day, at least 45 minutes apart
- quiet hours
- focus protection
- someone else talking → hold off

Escalation ladder:
1. A glance plus a soft chirp. No limit, but at most every 10 min.
2. Perk up plus a questioning chirp.
3. One sentence. Counts toward the daily limit.

Two rejections in a week pause that suggestion type for a week.

## 12. What to reuse (and how)

**Rules:**
- Prefer pip dependencies over copying code.
- When adapting code, rewrite it for this 4-joint arm. Put an attribution header in each file, and list every source in `NOTICE` and `THIRD_PARTY.md`.
- Never copy sounds, art or dialogue from films.

| Source | Take | How | License |
|---|---|---|---|
| **Shaurya's AVI repo** (Alm1973/Adaptability-of-VLMs-in-Robotic-Control-Systems) | `avi/camera.py` warm-up; `make_fast_verifier()` in `avi/run_study.py`; `UNRELIABLE_CLASSES` in `avi/yolo_tracker.py`; the firmware easing and mapping; the servo calibration table; `avi/teleop.py` (stdlib HTTP + MJPEG + arm buttons) → the dashboard base | adapt | his own |
| **`assistant/` logger** (built Oct 2026) | observer, change detector, VLM multiple-choice scoring, memory, today, audit | use it, then fold it into `avi_bot/perception` at M3 | his own |
| [pollen-robotics/reachy_mini_conversation_app](https://github.com/pollen-robotics/reachy_mini_conversation_app) | `moves.py` layered motion (primary queue + blended secondary, breathing idle, single control point); `idle_policy.py` weighted idle choice (60% stillness); the `EMOTION_INTENTS` vocabulary in `tools/play_emotion.py`; the `profiles/*/profile.md` format; `memory.py` remember/forget; the background tool manager | adapt the patterns (its moves target Reachy's 6-DoF head, so they can't be ported directly) | Apache-2.0 |
| [stack-chan/stack-chan](https://github.com/stack-chan/stack-chan) | `firmware/host/modules/ui/components/face/motions/saccade.ts`: random gaze jitter at random intervals | port to about 15 lines of Python | Apache-2.0 |
| [ELEGNT (Apple, 2025)](https://arxiv.org/html/2501.12493v1) | intention / attention / attitude / emotion framework; expressive and functional movement | design only | paper |
| [KoljaB/RealtimeSTT](https://github.com/KoljaB/RealtimeSTT) | wake word + voice detection + faster-whisper pipeline | dependency | MIT |
| [dscripka/openWakeWord](https://github.com/dscripka/openWakeWord) | wake word engine, "hey jarvis" model, custom training notebook | dependency | code Apache-2.0; pre-trained models CC BY-NC-SA 4.0 (personal use) |
| faster-whisper | speech-to-text | dependency | MIT |
| [hexgrad/kokoro](https://github.com/hexgrad/kokoro) | TTS | dependency (+ espeak-ng) | Apache-2.0 (espeak-ng GPL-3) |
| piper-tts | fallback TTS | dependency | GPL-3.0 |
| python-kasa | lamp plug | dependency | GPL-3.0 |
| icalendar / recurring-ical-events | calendar | dependency | BSD-2 / LGPL-3.0 |
| sounddevice | audio in and out | dependency | MIT |
| openai (SDK) | one client for every provider | dependency | Apache-2.0 |
| ultralytics | YOLO-World (already used) | dependency | AGPL-3.0 |
| Open-Meteo | weather data | API | CC BY 4.0, free for personal use |

**Licensing:** for a private project, all of this is fine. If the repo becomes public, ultralytics (AGPL) plus the GPL packages mean the simplest path is to license the project **AGPL-3.0** and keep `NOTICE`.

## 13. Repo layout (Mac = source of truth; deploy to `C:\Users\MSI-User\avi-desk\`)

```
avi-desk/
  CLAUDE.md  BUILD_SPEC.md  NOTICE  THIRD_PARTY.md  README.md
  config/settings.toml  config/joints.toml  config/apps.toml  .env.example
  personality/avi.md
  avi_bot/
    main.py  bus.py  scheduler.py
    perception/  camera.py change.py detector.py vlm.py observer.py tracker.py
    hearing/     listener.py
    voice/       beeps.py tts.py speech_policy.py audio_out.py
    brain/       router.py intents.py llm.py providers.py context.py tools.py
    personality/ mood.py attention.py behaviors.py expression.py
    motion/      engine.py gestures.py poses.py easing.py backends.py safety.py
    skills/      timers.py calendar.py focus.py find.py lamp.py media.py apps.py weather.py memory_tools.py status.py
    memory/      db.py routines.py
    dashboard/   server.py static/
  firmware/avi_servo_v2/avi_servo_v2.ino
  scripts/       wake-msi.sh sleep-msi.sh deploy.sh avi.sh (start|stop|status|logs|today)
  tests/  fixtures/audio fixtures/frames fixtures/ics
  docs/remote-notes.md
```

## 14. Remote development and testing

- **Dashboard** (from `teleop.py`) at `http://msi:8000` over Tailscale. It shows:
  - the camera stream (only while Shaurya allows it)
  - the sim view and joint sliders
  - gesture and emotion buttons
  - the mood readout and event log
  - pause, guest mode and kill controls
- **Run modes:**
  - `--sim` — no serial port, no camera; frames come from fixtures.
  - `--dry-run` — logs motion only.
  - `--fixtures` — WAV and frame inputs.
  - `--live` — the real thing.
- **Tests:** unit tests for mood, behaviors, the router, skills, beeps and the motion clamp. A scripted end-to-end test feeds a WAV of "hey jarvis, where's my calculator" against a seeded database and expects the right speech text and yaw. Run them over SSH on the MSI, and also on the Mac where possible (no GPU).
- **Nobody at the desk:** no live motion without Shaurya's go-ahead. Small, slow moves only, at most 15° per test. The camera is on the wrist, so its view shows whether the arm moved; use frame differences to confirm motion without anyone there.
- **Measuring the elbow and wrist directions remotely:** with his OK, move one joint +5° slowly and watch which way the image shifts (optical flow). Wrist pitch up → scene moves down in the image. Record the signs in `config/joints.toml`.

## 15. Milestones

| # | Milestone | Done when | Needs Shaurya physically? |
|---|---|---|---|
| M0 | Remote ops (`CLAUDE.md` phases 0–3): SSH, wake/sleep, deploy, GPU/camera checks over SSH | `ssh msi` works; the wake script works; the camera is reachable from the interactive task | one-time SSH bootstrap; Ethernet plugged in |
| M1 | **Motion foundation:** firmware v2, motion engine, sim, dashboard, poses and gestures | every gesture runs in sim. On hardware (with his OK): no jump on startup; all 4 joints within limits; the watchdog holds; joint signs recorded | watching the first live run (camera or a phone video call is enough) |
| M2 | **Personality core:** mood, attention, behaviors, beeps | a 30-min unattended sim run without the same behavior sequence repeating 3 times in a row; scripted events move mood as in the table; beep WAVs exported | no |
| M3 | **Senses:** fold in the observer; person tracking (look at you); hearing with fixtures | WAV fixtures transcribe; the wake word fires on its fixture; look-at keeps a person box within ±5° of center in replay | plugging in the reSpeaker, speaker and hub when they arrive |
| M4 | **Brain:** router, local intents, provider chain, tools, speech policy, Kokoro | at least 18 of 20 scripted utterances give the right tool calls; Groq blocked → fallback works; every reply follows the speech policy | no |
| M5 | **Skills:** timers, reminders, calendar, focus, find, lamp, media, apps, weather, memory | each skill's test in section 9 passes | lamp test once home |
| M6 | **Proactive + learning:** routines, gates, feedback | needs about 2 weeks of logs; beats "same as yesterday"; acceptance rate tracked | living with it |
| M7 | **Polish:** custom "hey AVI", tuning, startup on wake, crash recovery | runs for a week without manual restarts | recording a few wake-word samples |

## 16. Risks and open questions

- **Camera and GPU access from SSH sessions on Windows:** unknown. Plan for the interactive scheduled task.
- **Free tier limits change.** The provider chain plus local fallback keep it working, so re-check the limits when something breaks.
- **Kokoro speed on this CPU:** unmeasured; Piper is the fallback.
- **Reading PCA9685 registers on boot:** unverified; it's a bench test before use.
- **Small-model tool calling:** if `gpt-oss-20b` gets tool calls wrong, try `llama-3.3-70b-versatile` on Groq.
- **Desk hours** are needed for wake timers and the observer schedule. Ask Shaurya.
- ~~**The C270's field of view** is needed for look-at gains.~~ **MEASURED
  2026-10-05**, and what look-at needs is not the FOV but commanded degrees
  per frame: **yaw 83.9**, **wrist 32.2** (`config/joints.toml [camera]`). Both
  off a tracked desk object with no feedback loop in the path, r² 0.976 and
  0.986. An earlier yaw figure of 29.0 was wrong by 2.9× because it divided a
  commanded correction by a shift measured through an oscillating loop. Do not
  reuse either number as a field of view: yaw's implies base_yaw travels only
  ~62% of the degrees it is commanded.

## 17. Sources checked (Oct 2026)

- Reachy Mini conversation app code and README (cloned): https://github.com/pollen-robotics/reachy_mini_conversation_app
- Stack-chan firmware, saccade motion (cloned): https://github.com/stack-chan/stack-chan
- ELEGNT paper: https://arxiv.org/html/2501.12493v1
- RealtimeSTT: https://github.com/KoljaB/RealtimeSTT
- Kokoro: https://github.com/hexgrad/kokoro
- openWakeWord license (README): https://github.com/dscripka/openWakeWord
- python-kasa authentication notes (README): https://github.com/python-kasa/python-kasa
- Groq models: https://console.groq.com/docs/models
- Groq data retention: https://console.groq.com/docs/your-data
- OpenRouter free limits: https://openrouter.ai/docs/api-reference/limits
- GitHub Models limits: https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models
- Free API comparison (Aug 2026): https://aireiter.com/blog/best-free-ai-api
- Google OAuth "Testing" tokens expire in 7 days (why this uses the iCal address instead of OAuth): https://developers.google.com/identity/protocols/oauth2
- Google Calendar secret iCal address: https://support.google.com/calendar/answer/37648
- Open-Meteo terms: https://open-meteo.com/en/terms
- Package versions, licenses and Python requirements: PyPI JSON for each package (kokoro and RealtimeSTT need Python < 3.13)
