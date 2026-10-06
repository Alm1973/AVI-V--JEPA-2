# What should actually be driving this robot

Written 2026-10-05, answering four questions Shaurya asked in one go: should
OpenCV be the thing or a tool for the thing; do the VLA models fit; local or
cloud; and can JEPA get it closer to sentient.

Confidence is marked per claim. VRAM figures are approximate and none of them
have been tried on this machine.

## 1. "OpenCV should be a tool for the AI, not the entire thing"

**Agreed, and it is an inversion of the current control flow, not a new
component.** Right now the detector decides what matters and the VLM is called
to verify it (`MAX_VERIFY = 5` crops per observation). So OpenCV sets the
agenda and the model rubber-stamps it. That is backwards for an assistant: it
can only ever notice things that are in `settings.DESK_OBJECTS`.

The inversion: the model holds the goal ("is he focusing, and has anything
changed that I should react to"), and asks the cheap tools for exactly what it
needs — "where is the person", "did the scene change", "what is on the desk".
`change.py` and the detector become callable instruments.

This is a real refactor of `observer.py`, and it is worth doing. It is also
cheap, because it reuses everything already built.

## 2. The VLA models: π0, OpenVLA, GR00T N1, Helix, SmolVLA

**Wrong tool for this robot. High confidence.**

Every one of these is a *manipulation* policy. They are trained to output
end-effector actions — 7-DOF deltas, gripper open/close — on datasets like
Open X-Embodiment, for the task of picking things up and putting them
somewhere. AVI is a stationary 4-DOF camera on a stick **with no gripper**. It
does not manipulate anything. Fine-tuning a manipulation policy to drive a
pan-tilt camera is using a very large hammer on a problem that is already
solved by the closed-loop servo in `attention.py`.

Specifically:

| Model | Fits 6 GB? | Why it still does not fit the job |
|---|---|---|
| **OpenVLA** (7B) | 4-bit maybe, very tight | Outputs 7-DOF end-effector deltas. Wrong action space, and leaves no VRAM for YOLO + VLM. |
| **π0** | No, realistically | Diffusion/flow action expert for dexterous manipulation. |
| **GR00T N1** | No | Humanoid-oriented, wants a real GPU. |
| **Helix** (Figure) | n/a | Not publicly released. |
| **SmolVLA** (~450M) | Yes | The only runnable one, and the closest to sensible — but still a LeRobot manipulation policy, and it would need teleop demonstrations of a task AVI does not have. |

**What AVI's "action model" problem actually is:** choosing *which gesture to
play when*. That is a 9-way discrete choice over a ~2 s horizon, already
implemented in `behaviors.py` as utility × context × cooldown with a softmax.
A VLA would replace a working 100-line policy with a 7B model and a data
collection project, to produce the same nine outputs.

**The honest exception:** if AVI ever gets a gripper and is meant to hand
things over, SmolVLA becomes the right starting point. Not before.

## 3. Local or cloud

**Hybrid, split by latency budget, text-only across the boundary.**

Three tiers, and the split is forced by physics and by the privacy rule, not
by preference:

- **Local, hard real-time — the control loop.** Gaze and gesture must stay on
  the MSI. A cloud round trip is 300–800 ms at best and fails when the network
  does; you cannot close a feedback loop over the internet, and an arm that
  freezes mid-gesture when the wifi hiccups is worse than a dumb one.
- **Local, soft — perception.** Detector and VLM. Also forced:
  `CLAUDE.md` says keep everything local and never upload frames, because the
  camera watches his room. That rule is not negotiable for images.
- **Cloud, best — language and planning.** A frontier model is far stronger
  than anything that fits in 6 GB, and 1–2 s is fine for something that is
  about to speak. This is where "you have been at this two hours" should come
  from.

**The boundary rule that makes this safe: only text crosses it.** The cloud
call gets labels and summaries read out of `desk_memory.db` — "at desk 94 min,
focused, phone picked up 3x in last 20 min" — and never a frame. That keeps
the privacy rule intact and still buys a much better brain than the laptop can
host.

So: **not faster, but stronger, and only where latency allows.** Cloud is
strictly slower than local for anything in the loop. It wins on quality for
anything that produces words.

## 4. JEPA, and "as close to sentient as possible"

Two parts, and they deserve different answers.

**On the architecture.** V-JEPA 2 is a representation learner: it predicts in
latent space instead of reconstructing pixels, which is genuinely more
efficient, and V-JEPA 2-AC is the action-conditioned variant used for
planning. The claim that this suits edge hardware is fair in relative terms.
But the encoders are large, the action-conditioned work was demonstrated on
manipulation, and there is no pretrained model for "desk companion". Running
it meaningfully alongside the detector and the VLM on 6 GB is not realistic
today. *Medium-high confidence; I have not benchmarked it here.*

**On sentience.** "As close to sentient as possible" is not an engineering
spec, and it is worth being blunt about why that matters: a world model does
not produce the feeling of aliveness. **Timing does.** What makes Dum-E read
as a character is that it reacts at the right moment, hesitates, and has
moods that persist — which is `mood.py` and `behaviors.py`, already built and
already working. Spending the whole VRAM budget on a world model would buy
the least visible part of the stack.

**But there is a real JEPA-shaped idea here that fits, and it is cheap.**

The useful thing to predict is not pixels — it is *him*. Train a small model
on `desk_memory.db` to predict the next state (at desk / away / focused /
distracted) from recent history and time of day. Then:

- **prediction error = surprise**, and surprise is what should drive attention
  and mood. That is the actual mechanism behind curiosity in the
  cognitive-science literature, and `mood.py` already has the inputs for it.
- it is a latent-space predictive model of the world as the robot experiences
  it, which is the JEPA *idea* applied where this robot has data.
- it is kilobytes, trains in seconds, and runs in microseconds.
- it degrades honestly: with no data it predicts badly, is surprised often,
  and therefore acts curious — which is correct behaviour for a robot that
  has just met you.

That is the version worth building. It also gives `audit.py` a second job:
the labels that check the robot's read of him are exactly the training signal
this needs.

## Recommended order

1. **Finish M3's look-at** (positive-parallax rewrite). Nothing above matters
   if it cannot reliably see him.
2. **Invert the observer** so the model drives the tools (§1). Cheap, reuses
   everything.
3. **Add the cloud language tier, text-only** (§3). Biggest visible jump in
   "it feels like an assistant" per hour spent.
4. **The surprise model** (§4). Small, novel, and the honest route to the
   quality he is asking for.
5. VLA models: revisit only if a gripper ever gets added.
