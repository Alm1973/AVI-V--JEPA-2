# AVI: a desk robot that learns its own body and its owner

AVI is a 4-servo robot arm with a camera on its wrist. It lives on my desk. It watches, remembers, reminds and moves. When you give it a command it has never seen, like **"do an interpretive dance like a dolphin"**, it composes the movement itself, performs it, and checks with its own camera that the movement really happened.

Everything runs on a laptop next to the arm (RTX 3060, 6 GB). Only short text goes to free-tier language models. Camera frames and audio never leave the machine.

**This repo, in 7 days (Oct 4–10, 2026):** 265 commits, ~24,000 lines of Python, 1,161 automated tests, and every feature below tested on the real robot.

---

## Results (measured on the robot)

| What | Result |
|---|---|
| Commands it was never programmed for | "act like a sleepy cat stretching" → the AI composed *look up, pause, lean in, pause, look down, pause, wave*, and the arm performed it. **2.5 s** from command to motion. |
| Commands it has learned | The same words answered the same way twice become a local shortcut: **0.7 s**, no AI call, works offline. |
| Checking every move with vision (ORB + V-JEPA 2) | All 10 test moves (6–20°, four directions) judged correctly; no-move controls read "still". Post-deploy check: **7/7**. |
| Shoulder and elbow moves, checked against the 3D self-model | Old rule: **0/16** predictable. 3D body model: **15/16** (ORB), **12/13** (V-JEPA). Live check: **4/4**. |
| Self-learned joint slopes | Pixel error **9–23 px**, against 17–45 px for the hand-built 3D model. |
| How close a dance ends to where it started | First runs: **13° off** (gear backlash). After the fix: **< 1°**. |
| Dances judged by the camera | Every dance seen moving by both judges, and seen returning to its start. |
| Presence (is he at the desk?) | A poster of a person caused false arrivals at sunrise. AVI learned it is not me in **5 minutes**, then ignored it **1,197** times over a school day with **zero** false arrivals. |
| Daily self-check | One 4° move per joint, every day while I'm away. All joints healthy (0.80–1.32 of the learned motion). |

## Findings

These are the things the robot taught me, each measured, not assumed.

1. **The servos lie about where they are.** The base has **13–18° of backlash** (13.5° vs 12.7° from either side; repeatable to 0.8° from one side). The tilt joints have about **1–1.5°** each. Every joint now approaches its target from one fixed side (`motion/backlash.py`).
2. **Small commands don't move this arm.** Picture slide per degree was 11.5 px at 2°, 19.1 px at 16°, and 8° moves in one direction went the *wrong way*. I noticed it before the data showed it, and the fix was the same one-side approach.
3. **A "1.8× under-travel" was really camera tilt.** A base turn rolls the image by g·sin(e) and slides it by g·cos(e). Fitting the roll across 14 tilts (26 measurements) gave a base gain of **0.95** [0.89–0.98], so the joint was fine. It also gave where level is (wrist 160.3°) and the lens field of view (48.6°), with no ruler.
4. **V-JEPA's tokens remember where they sit in the image.** Raw matching always peaked at "no shift". Scoring each shift against the frames' own self-similarity, `S_ab(s) − (S_aa(s)+S_bb(s))/2`, cancels the position signal, so the true shift wins.
5. **Detector confidence can't tell me from the robot's own parts** (me 0.75–0.89, the arm's hardware 0.56–0.72). Geometry can: anything bolted to the arm stays at a fixed image position when the arm turns, a **30× separation**.
6. **The arm tops out at about 27°/s**, roughly a tenth of the datasheet speed, most likely because the power supply limits it. Every dance and gesture is timed against the measured speed, not the commanded one.
7. **One pose looked at a blank wall.** The pose the logging pipeline read from saw nothing at any angle; a daylight sweep of every pose found it and it was replaced.

## How it learns by itself

| Research | What AVI does with it |
|---|---|
| **GenEM** (Mahadevan et al., HRI 2024): LLM → procedure → robot primitives, refined by feedback | `perform`: the model pictures the motion and composes it from 17 primitives. "Do it faster / bigger" edits the saved dance. Praise keeps a dance; a scold remakes it. |
| **V-JEPA 2-AC** (Meta, 2025): world models learn what actions do to the picture | A 3D model doesn't go *into* JEPA. The body model **predicts** what a move does, and JEPA **checks** it. |
| **Self-modelling robots** (Lipson lab, 2022/2025) | Motor babbling: small moves of each joint, each measured, fitted into a self-model; a daily self-check notices a joint that stops responding. |
| **Voyager** skill library | Dances and command shortcuts are kept, reused by name, and dropped when they fail. |
| **Anki Cozmo / Vector** | No gesture plays the same twice: size and tempo vary within ±15%. |

Full write-up: [`docs/RESEARCH-motion.md`](docs/RESEARCH-motion.md).

## How I built it: with Claude

I built AVI with **Claude Code** as my engineering partner, entirely from my Mac. The robot's laptop sits at home, and Claude reached it over SSH through Tailscale.

**What I did**
- Set the goals: a robot that learns instead of being scripted; V-JEPA as its eyes; free models only; privacy first.
- Ran the hardware: calibrating servos, teaching poses, measuring the lens position with a ruler.
- Caught problems by watching the real robot: reversed joints, small moves that didn't happen, a poster it thought was me.
- Made the calls: which experiments to run, what to keep, what to throw away.

**What Claude did**
- Researched papers and turned them into code.
- Wrote and tested most of the code.
- Ran the experiments on the robot remotely, read the numbers, and fixed what the measurements showed, often in long self-paced loops while I was at school.
- Kept a running log of every measurement and every mistake ([`docs/HANDOFF.md`](docs/HANDOFF.md)).

The way of working that made it go: **measure on the real hardware before believing anything.** Several "facts" in this repo were overturned by a later measurement, and the log keeps both versions and the reason, so nothing gets re-derived.

What I learned along the way: forward kinematics and gear backlash; how self-supervised video models like V-JEPA represent motion; how to give a language model tools and constrain what it can do; and how to design an experiment whose answer I don't already know.

This builds on my earlier research on vision-language models in robot control ([Adaptability of VLMs in Robotic Control Systems](https://github.com/Alm1973/Adaptability-of-VLMs-in-Robotic-Control-Systems)). One rule from that paper is still the core design rule here: **the model checks, it doesn't steer.**

---

## How it's built

| Part | What |
|---|---|
| Arm | 4 servos (base yaw, shoulder, elbow, wrist) on a PCA9685, Arduino firmware with joint limits and a watchdog |
| Camera | Logitech C270 on the wrist |
| Vision | V-JEPA 2 ViT-L (move judging, scene latents), YOLO-World (people, objects), OpenCV ORB |
| Brain | Free-tier LLMs with tool calling, tried in order: Groq gpt-oss-20b, gpt-oss-120b, Qwen, then OpenRouter, then GitHub Models. Local rules and learned shortcuts run first. |
| Voice | Piper TTS |
| Memory | SQLite: facts, reminders, dances, shortcuts, presence (all on the robot's laptop) |

```
avi_bot/
  brain/        router, tools, local rules, learned shortcuts, providers
  motion/       motion engine, gestures, choreography, 3D body model, backlash
  perception/   move judge, presence, room map, self-model (babbling)
  memory/       fact store, dated facts
  dashboard/    the web dashboard that runs the robot
assistant/      scripts that run on the robot's laptop
scripts/        deploy, start/stop, smoke and motion checks (run from a Mac)
tests/          1,161 unit tests; they run with no hardware
docs/           research, design notes, and the running measurement log
```

```bash
./scripts/test.sh               # every test, no hardware needed
python3 scripts/smoke.py        # on the robot: health, judged moves, two chat commands
python3 scripts/motion_check.py # on the robot: dances and the self-model
```
