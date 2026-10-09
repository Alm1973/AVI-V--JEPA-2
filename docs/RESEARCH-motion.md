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

## Status (robot results, 2026-10-09 morning, daylight, nobody in the room)

- [x] **Dances on the real arm.** With `scripts/motion_check.py dance` plus the stored dolphin dance:
  - Every dance was seen moving by both the thumbnails and V-JEPA. JEPA similarity fell to 0.60–0.78, and the "still" threshold is 0.85.
  - The first runs ended **13° off** where they started. This was the base's 18° of backlash. After every joint was made to arrive from a fixed side, the dances end 2–24 px (<1°) off. ORB and V-JEPA both say each one came back.
- [x] **The AI composes the dance.** "do an interpretive dance like a dolphin" through `/ask` went to Groq, which composed wave ×3, leap ×2, a slow spin and rise ×2. The arm played it, the dance was saved, and replaying it from memory needed no AI call.
- [x] **Self-learning (`/babble`), 32 small moves at the seat view.** It also showed every pitch joint has ~1–1.5° of slack (handled in `backlash.ARRIVE`).

  | joint | old rule | 3D model | learned |
  |---|---|---|---|
  | shoulder direction (ORB / JEPA) | 0/8 / 0/7 | **7/8 / 6/7** | 7/8 / 6/7 |
  | elbow direction | 0/8 / 0/6 | **8/8 / 6/6** | 8/8 / 6/6 |
  | pixel error (all joints) | 17–95 px | 17–45 px | **9–23 px** |

  **Answer:** the 3D model gives the camera checks control over the two joints they could not check before. What AVI learned for itself is the most accurate. `reach_judged = true`; elbow gain 0.79 measured. Live `/verify_move` on shoulder ±6° and elbow ±6°: ORB and V-JEPA agreed with the model 4/4.
- [ ] Measuring the three links with a ruler would make the 3D positions real. They are: desk to shoulder axle, shoulder axle to elbow axle, and wrist axle to lens.
