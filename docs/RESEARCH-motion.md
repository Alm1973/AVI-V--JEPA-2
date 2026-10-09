# Motion research: making up moves, and a model of its own body

Shaurya's ask (2026-10-09): AVI should build things itself instead of being taught them. "Do an interpretive dance like a dolphin" should produce a dance it made up. He also asked whether giving JEPA a 3D model of the robot would give it full control of its body.

## What similar projects do

| Project | Pipeline | What AVI takes from it |
|---|---|---|
| **GenEM**, [Mahadevan et al., HRI 2024](https://arxiv.org/abs/2401.14673) | An LLM first describes how a person would express the request, step by step. A second step writes code from the robot's primitive skills and its learned behaviours. User feedback then edits either the plan or the code. Users rated the results as clear, and some beat an animator's. | `perform`: the model pictures the motion (`idea`) and composes it from 17 primitives. Each one is kept by name and changed by praise or scolding. |
| **Voyager** (skill library) | New skills are stored and checked, then reused by name. | `learned_moves`: a second "dolphin dance" replays the first one. |
| **Apple ELEGNT** ([coverage](https://www.yankodesign.com/2025/02/07/apple-researchers-built-a-pixar-style-robot-lamp-that-moves-and-emotes-like-a-living-creature/amp/)) | A lamp-like arm. Expressive motion (intention, attention, emotion) made people more engaged than purely functional motion. | Keep moves visible and in character. The arm's 27°/s sets the tempo and the size is kept, as in `gestures.py`. |
| **V-JEPA 2-AC**, [Meta 2025](https://arxiv.org/abs/2506.09985) | A frozen V-JEPA 2 encoder plus a predictor that takes actions and the arm's own state. It trains on about 62 h of robot video and plans by searching for actions that reach a goal image. **No 3D model goes in.** | Motor control comes from learning what actions do to the picture. It does not come from a mesh. |
| **Lipson lab self-models** ([Columbia, Nat. Mach. Intell. 2025](https://www.engineering.columbia.edu/about/news/robot-learns-imagine-itself)) | The robot learns a kinematic model of its own body from camera video. It uses that model to plan, to spot abnormal behaviour and to recover from damage. | Motor babbling: AVI moves each joint a little and watches what the picture does. |

## Answer to "would a 3D model give JEPA full motor control?"

**Not by itself.** JEPA is an encoder for what the camera sees, and it has no input for a mesh. What works, in both V-JEPA 2-AC and Lipson's robots, is to pair two things:

1. A model of the body that **predicts** what a move will do.
2. Vision that **checks** whether it did.

Both pieces are now in AVI:

- **`motion/body.py`** is the 3D model. Forward kinematics turns the four joint angles into 3D points for the base, shoulder, elbow, wrist and lens, plus the direction the camera points.
  - It is fitted to Shaurya's ruler measurement: the lens at TALL is 25.0 cm out and 34.4 cm up.
  - It is also fitted to the measured level of the camera.
  - The three link lengths are still estimates.
  - It does three jobs:
    - It predicts the view change for **every** joint. Shoulder and elbow moves used to have no expected direction, so they were never judged.
    - It keeps improvised dances off the desk.
    - It draws the arm live on the dashboard.
- **`perception/selfmodel.py` + `POST /babble`** is the learning part. AVI makes small single-joint moves and measures each one with ORB and V-JEPA. It then compares three predictors on moves they did not see:
  - the old rule (base and wrist only),
  - the 3D model,
  - the slopes it learned itself.

  The result decides whether shoulder and elbow joints are judged (`body.reach_judged`).

## Status

- [x] `perform` with offline improvisation (the dolphin leaps and swims), kept per name, judged from the picture (did it move, did it come back), scaled up when nothing visibly moved, rated by praise or scolding.
- [x] The cloud model composes its own dance. Dry run on 2026-10-09: wave ×2, leap ×3, rise, duck ×2, spin, circle; 29.6 s.
- [x] 3D body model, live drawing, and shoulder/elbow prediction behind `reach_judged`.
- [ ] Robot runs in daylight: a dance at size 1 and then normal size; `/babble` 16 moves; `/verify_move` with a joint. The results go into `docs/HANDOFF.md` and `[body]` in `config/joints.toml`.
- [ ] Measuring the three links with a ruler would make the 3D positions real. They are: desk to shoulder axle, shoulder axle to elbow axle, and wrist axle to lens.
