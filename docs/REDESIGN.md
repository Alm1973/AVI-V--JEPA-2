# REDESIGN: one directive, self-supervised learning

Status: **direction approved, open questions answered.** Agreed in conversation on 2026-10-12. No code yet.

Where this disagrees with `BUILD_SPEC.md` sections 5, 9, 10 and 11, this file wins once Shaurya signs off.
`docs/LEARNING-DESIGN.md` (layers 1-4) carries over almost whole. This doc puts it at the centre.

---

## 1. The thesis

> **AVI learns by itself.** It improves at helping its owner over time from its own
> experience. Nobody labels data for it and nobody trains it by hand.

Two things are kept apart:

- **Training it** means a human labels data and fits a model. This is **not** allowed.
- **Learning by itself** means AVI produces its own labels from what happens next, from tracking
  across frames, and from Shaurya's reactions. It updates its own models continuously. This is
  self-supervised and online learning, and it is the whole point.

Pretrained encoders are allowed. They work as AVI's senses, like a newborn's visual cortex: they
turn pixels into numbers and never learn anything about Shaurya. **Everything about Shaurya is
learned by AVI.**

## 2. The one directive

> Interact with Shaurya and help him in any way you can, without being annoying.

There are no task-specific behaviours to program. AVI decides what helping means from the
directive, from its memory, and from what has worked before.

## 3. The loop

```
 camera, mic ──► SENSES (local, frozen)  ──► events + state, as TEXT
                         │                          │
                         ▼                          ▼
              SELF-LEARNED MODELS (local)  ──►  BRAIN (cloud LLM, text only)
               routine, identity, gestures,        │  directive + state + memories
               what-helps, body, curiosity         ▼
                         ▲                     TOOLS (whitelist)
                         │                      speak, gesture, look, remember,
                         │                      remind, lamp, media, ask
                         │                          │
                         └──── OUTCOME ◄────────────┘
                     (thanks / ignored / "stop" / thumbs up or down / undone)
```

Every night a **reflection** pass reviews the day. It retrains the self-learned models on the
day's logs, writes lessons, merges or drops skills, and picks small goals for tomorrow.

## 4. Senses: the camera becomes text

The LLM never sees a frame. Pixels stay on the MSI and only text crosses to the cloud.

| Sense | Model (frozen, local) | Emits |
|---|---|---|
| Face | InsightFace embeddings | `face_cluster_3 (0.93)` |
| Hands | MediaPipe Hands, 21 points, CPU | hand-shape vectors, built-in gestures |
| Body | MediaPipe Pose / Face Mesh | sitting / standing / lying, gaze at AVI, nod, yawn |
| Room | frame brightness, change detector (exists) | lights on/off, motion |
| Scene, on demand | Qwen2.5-VL-3B (exists, ~0.6 s) | answers to `look("...")` |
| Voice | wake word + faster-whisper (spec section 7) | transcript |

The LLM receives three things:

1. **A state summary** with every call. For example:
   `16:42 Shaurya at desk 38 min (face 0.93), looking at laptop · lights on · last gesture: thumbs-up 16:31 · routine: usually leaves ~17:30 (8/10 days) · unnamed: person_B x3, gesture_7 x12`
2. **Events** that wake it up: arrival or departure, a known gesture, a stranger, lights off,
   the wake word, a predicted routine change. The LLM is **not** called per frame.
3. **The `look` tool.** The LLM asks a narrow question and the local VLM answers in text.
   The paper's rule still holds: the vision model checks, it doesn't steer.

The cost is that the LLM only knows what the senses report or what it thinks to ask. The answer
is the self-learned vocabulary below, which grows over time.

## 5. What AVI learns by itself

| What | Self-generated labels | Model | Pre-registered metric |
|---|---|---|---|
| **Routine and sleep** | the next event that actually happens | sequence predictor, retrained nightly | held-out error vs. "same as yesterday" |
| **Who's who** | one face tracked across frames = free same-person pairs | online clustering + small projection head on his faces, his lighting | cluster purity; **false identity kept separate** |
| **Gestures and meaning** | recurring hand/body motions, linked to what follows | unsupervised motion clustering + association | does gesture_k predict the same outcome on held-out days |
| **What helps** | his reactions to each action | contextual bandit over (situation, action) | suggestion acceptance rate, week over week |
| **Its own body** | motor babbling (`selfmodel.py`, exists) | self-model; JEPA-AC later | pixel error on held-out moves |
| **What to explore** | where its own prediction error is falling fastest | learning-progress curiosity (LEARNING-DESIGN layer 3) | n/a, it is a driver |

Things AVI discovers get placeholder names (`person_B`, `gesture_7`). The LLM can ask Shaurya
**once** what they are. That is a name, not a training label, and the clustering already happened.

**Sleep.** The C270 cannot see in the dark, so sleep is inferred from last seen, lights off,
lying down, lights on and first seen. The camera can see the bed (decision 2), so lying in bed is observed directly
while the lights are on.

## 6. Proving the thesis

- **Curves, pre-registered before launch:** routine prediction error, acceptance rate and
  false-belief rate, plotted weekly from day 1.
- **Ablation:** AVI with day-1 memory against AVI with week-4 memory, **same LLM**, on the same
  held-out situations. Any difference comes from what AVI learned itself, not from the LLM.
- **False belief is reported separately from accuracy**, as in the paper. Nothing becomes a
  fact (a name, a routine, a skill) without a confidence gate.

## 7. Boundaries

- **No screen, app, file or browser access** for now (Shaurya, 2026-10-12). Maybe later.
- Tools are whitelisted. No shell. The firmware limits and watchdog stay untouched.
- Only text crosses to the cloud, never frames or audio. Face and hand data are stored as
  vectors, never as images.
- The speaking-up gates from CLAUDE.md (quiet hours, at most 4 a day, 45 min apart, no
  interruptions during calls) stay, but the bandit learns **within** them.

## 8. What stays, what goes

| Stays | Goes, or comes back only as a tool that earns it |
|---|---|
| firmware v2, joint limits, measured speed (27 deg/s) | YOLO-World in any decision path |
| remote ops: wake, sleep, deploy, task-ctl | hand-tuned OpenCV heuristics for presence |
| Qwen2.5-VL-3B, demoted to the `look` tool | JEPA as a move *checker* (returns as JEPA-AC body model, later) |
| `selfmodel.py` babbling, daily self-check | scripted behaviour selection (replaced by the loop + bandit) |
| gestures and chirps, as **tools** | |
| the free LLM provider chain (spec section 8.2) | |

## 9. Decisions (Shaurya, 2026-10-12)

1. **Captions: yes.** A local one-line scene caption may go to the cloud LLM every few minutes.
   It is generated on the MSI; the frame never leaves.
2. **Bed: yes, the camera can see it.** Sleep can use lying-down detection in the bed area,
   not only desk presence. Lights off still stands in for "asleep" once it is dark, since the
   C270 cannot see without light.
3. **Naming: yes.** AVI may ask him **once** to name a person or gesture cluster it discovered.

## 10. Order of work (proposed)

1. Senses → text events: face, hands, pose, lights. Log only, no brain yet. Start immediately,
   because every self-learned model needs data.
2. Self-learned identity and routine models on those logs. Check the curves.
3. The brain loop with tools and memory. Reactions logged as feedback.
4. Bandit over actions, and nightly reflection.
5. Pre-registered 4-week run and the ablation.

## 11. Measured (2026-10-12)

| Check | Result | Consequence |
|---|---|---|
| Groq free tier, live headers (`scripts/groq-limits.sh`) | `gpt-oss-20b`, `gpt-oss-120b`, `qwen3.8-27b`: **1,000 requests/day and 8K tokens/min each** (200K tokens/day each, per providers.py) | ~85 turns per model per day at ~2.3K tokens/turn, so **~255/day across the three**, plus OpenRouter 50 |
| MediaPipe hands + body + face together on the MSI CPU, live camera, person in view (`scripts/mp-bench.sh 60`) | **3 % of the machine** (16 threads), 8 % total. Median per frame: hands 19 ms, body 14 ms, face 11 ms | CPU is **not** a constraint. The senses fit with room to spare |
| Same run, camera rate | **9.9 fps** at 1280x720; hands got 8.6 Hz of a 15 Hz target | MJPG at 720p: the driver **ignored the request** and stayed YUY2 (raw), 9.8 fps. 640x480 raw: **14.7 fps** at brightness 112/255. **Capture at 640x480** (the senses downscale to 640 anyway). The hands shortfall was partly a bench scheduling bug, fixed |
| `gpt-oss-20b` on the R820 | not run: the R820 is not yet on the tailnet | `scripts/r820-bench.sh` once it is |

The Python 3.12 env for the new pipeline now exists on the MSI at `%USERPROFILE%\avi-desk\.conda312`
(conda-forge, mediapipe 1.1.0). `.venv-yolo` is untouched.
