# AVI: a desk robot that learns by watching

AVI is a 4-servo robot arm with a camera on its wrist. It lives on my desk. It can't grab things. It's useful because it **watches, remembers, reminds and moves**: it notices when I sit down, keeps track of where things are on the desk, runs focus sessions and timers, and says what's coming up. It also learns new things on its own instead of being programmed for each one.

The perception and memory run on a laptop (RTX 3060, 6 GB) next to the arm. Only short text ever goes to a free cloud language model. Camera frames and audio never leave the machine.

## What it does

- **Understands commands it was never programmed for.** A language model turns a request into the robot's own tools: looking, nudging, timers, finding things, gestures. Ask "do an interpretive dance like a dolphin" and it pictures how a dolphin moves and builds a dance from 17 motion primitives, such as a wave rolling down the arm or a leap that arcs up and dives. With no network, it makes one up from the name.
- **Checks every move with its own eyes.** Two judges compare camera frames from before and after each move: **V-JEPA 2** (a self-supervised video world model) and ORB feature matching. If the camera didn't go where it was told, AVI says so.
- **Has a 3D model of its own body.** Forward kinematics, anchored to a ruler measurement, predicts what each joint should do to the picture. Shoulder and elbow moves couldn't be checked before; with the model they're judged correctly 15/16 (ORB) and 12/13 (V-JEPA) on the robot.
- **Learns its own body.** It makes small moves of each joint ("motor babbling") and fits what each one does to the picture. This found ~18° of backlash in the base and ~1.5° in each tilt joint, which are now corrected for. A daily self-check repeats this and reports any joint that stops moving.
- **Learns from use.** It keeps the dances it invents and changes them when told "faster" or "bigger". Praise keeps a dance and a scold remakes it. Commands that get the same answer twice become instant shortcuts that work offline. It also learned that a poster on the wall is not me.
- **Knows when I'm at the desk.** Presence detection runs from the seat view, with a learned list of decoys. It gives a morning rundown of dated plans ("Today: Chemistry test."), an evening heads-up for tomorrow, break nudges, and a wave when I sit down.

## How it's built

| Part | What |
|---|---|
| Arm | 4 servos (base yaw, shoulder, elbow, wrist) on a PCA9685, Arduino Uno firmware with joint limits and a watchdog |
| Camera | Logitech C270 on the wrist |
| Vision | V-JEPA 2 ViT-L (move judging, scene latents), YOLO-World (people and objects), OpenCV ORB |
| Brain | Free-tier LLMs with tool calling, tried in order: Groq gpt-oss-20b, then gpt-oss-120b, then Qwen, then OpenRouter, then GitHub Models; local rules and learned shortcuts first |
| Voice | Piper TTS |
| Memory | SQLite: facts, reminders, dances, shortcuts, presence |

```
avi_bot/
  brain/        router, tools, local rules, learned shortcuts, providers
  motion/       motion engine, gestures, choreography, 3D body model, backlash
  perception/   move judge, presence, room map, self-model (babbling)
  memory/       fact store, dated facts
  dashboard/    the web dashboard that runs the robot
assistant/      scripts that run on the robot's laptop
scripts/        deploy, start/stop, smoke and motion checks (run from a Mac)
tests/          1,100+ unit tests; they run with no hardware
docs/           design notes, research and a running log of what was measured
```

The research behind the motion work is in [`docs/RESEARCH-motion.md`](docs/RESEARCH-motion.md). It compares GenEM, V-JEPA 2-AC, Lipson's self-modelling robots and Apple's ELEGNT with what was measured on this arm. [`docs/HANDOFF.md`](docs/HANDOFF.md) is the running log.

## Running the tests

```bash
./scripts/test.sh
```
