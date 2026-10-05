# assistant: desk assistant, observation logger

Milestone 4 of the [Stage 1 design](https://claude.ai/code/artifact/8880fcd3-b6c1-4108-8abc-48ccec42f90a). The logger watches the desk, labels what you're doing, notes which objects are really there, and saves it all as text in SQLite. Later milestones (routines, the voice loop) read from the same database and reuse the same loaded model.

## Setup

1. Put this `assistant/` folder next to `avi/` in the repo.
2. Use the same Python environment as `avi/`. No new packages are needed: torch, transformers, bitsandbytes, ultralytics and opencv are already in `requirements.txt`. pytest is optional, for the self-checks.
3. Run everything from inside this folder (`cd assistant`), the same way as `avi/`.

The logger needs about 3.8 GB of free GPU memory, the same as the 1 September run. Don't run it at the same time as an `avi/` experiment or ComfyUI.

## Run

| Command | What it does |
|---|---|
| `python test_observer.py` | Self-checks. No GPU, camera or model download needed |
| `python observer.py` | Start logging, with a small preview window |
| `python observer.py --no-preview` | Start logging without a window |
| `python observer.py --audit-rate 0.05` | Also save a 320 px thumbnail for 5% of observations, so you can check the labels |
| `python today.py` | Today's timeline, hours per activity, and where objects were last seen |
| `python audit.py` | Label the audit thumbnails yourself and get the agreement score |
| `python today.py --forget` | Delete everything logged today, including thumbnails |

Preview keys: `p` pause or resume, `g` guest mode on or off, `q` quit. Creating a file named `PAUSE` in this folder also pauses logging, which is how the voice loop will mute it later.

## How it works

1. **Every frame (5 per second):** an OpenCV change check runs on a 160×90 grayscale copy, which is almost free. It fires once the scene has changed and then gone still for 1.5 s. If something passes through and the scene goes back to how it was, it doesn't fire. If you keep moving (typing), it fires anyway after 8 s.
2. **On a change, or on a heartbeat** (60 s, or 300 s while nobody is at the desk), it observes:
    - The VLM gets the frame and a multiple-choice question with your activity list. The answer comes from the first-token letter scores, the same trick as the yes/no verifier, with no text generated. The option order is shuffled on every call, so a bias toward one letter can't pick the answer. Below 0.6 probability, the observation is stored as `uncertain`.
    - YOLO-World proposes desk objects. The VLM checks the top 5 crops with the verifier prompt from the paper. Only crops it says yes to count as sightings.
3. One observation takes about 1–2 s of GPU time: one multiple-choice call plus up to five yes/no calls at roughly 0.3 s each.

## What gets stored

| Table | One row per | Holds |
|---|---|---|
| `observations` | observation | time, trigger, activity, the full probability spread, detector person score, latency |
| `sightings` | object checked | label, detector score, VLM margin (> 0 = verified), box, zone (left, middle or right of the view) |
| `sessions` | block of one activity | start, end, minutes. Rebuilt by `today.py` and when the logger stops |

No frames are saved, except audit thumbnails when you turn them on. `today.py --forget` and `audit.py --delete-after` remove them.

## Milestone 4 test

Log for a few days with `--audit-rate 0.05`, then run `python audit.py`. The goal is at least 80% agreement on 100 labels. If one pair keeps getting mixed up (say, reading vs writing on paper), try `--mc-passes 2`, reword that option in `settings.py`, or merge the two.

## Known limits

- Activities come from a fixed list in `settings.py`. Anything else is "other".
- Zones are thirds of the camera view, not real desk positions. Good enough for "left side" answers.
- "water bottle" is left out of the object list, because YOLO-World's scores for it were inverted on your held-out data.
- Qwen3-VL-4B segfaulted while loading in 4-bit in August (transformers 5.14.1, bitsandbytes 0.50.0). Stay on Qwen2.5-VL-3B unless you upgrade both packages.

## Next

Milestone 5 (routine mining) reads the `sessions` table nightly. It needs about two weeks of logs first.
