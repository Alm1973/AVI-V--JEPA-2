# A robot that actually learns from being watched

2026-10-05. Written after Shaurya pushed back on the honest ground that the
current stack does not learn anything, and that OpenCV gaze is a puppet
pretending to be a mind. Both true. This is what would actually work on a
6 GB laptop.

## The concession

`mood.py` and `behaviors.py` have **fixed, hand-written weights**. The softmax
over utilities never updates. The half-lives are constants I chose. Nothing in
the system is different after a month with him than on day one. It is a good
puppet -- the timing is real and the timing is most of what reads as alive --
but it is a puppet, and calling it learning would be a lie.

He is also right about JEPA specifically, and my first answer under-rated it.
The requirement "learn from real-world video, no labels" has essentially one
serious answer, and self-supervised latent prediction is it. Predicting the
next *representation* instead of the next pixels is what makes it tractable:
the model is free to discard what it cannot predict (sensor noise, exact
pixel values) and keep what it can (that a person is leaning forward).

## The one idea that makes this fit 6 GB

**Store latents, never video.**

The encoder runs once per sample and its output -- a vector of a few hundred
floats -- is what goes in the database. From then on every learning problem is
over vectors, not pixels:

- a frozen encoder is the only large model, and it only ever does inference
- weeks of his life is tens of megabytes, not terabytes
- training a head on it is seconds, on CPU if need be
- **no frame is ever stored**, which satisfies the privacy rule more strictly
  than the current audit thumbnails do

This is the difference between "needs a datacentre" and "trains overnight on a
laptop". It is the whole trick.

## The stack

### Layer 0 -- reflex (exists, keep it)
The gaze servo in `attention.py`. ~2.5 Hz detection, 50 Hz spring. This stays
dumb on purpose: it is a spinal reflex, not cognition, and it must work when
every model is unloaded. OpenCV's correct job is this and nothing more.

### Layer 1 -- frozen self-supervised video encoder
V-JEPA 2 (ViT-L class) or VideoMAE-base as the lighter fallback. Short clips
at a few fps -> one latent per sample. **Frozen.** No fine-tuning, no
gradients, ~600 MB in fp16. Pretrained on a million hours of video, which is
exactly the knowledge he cannot collect himself.

*Unverified on this machine. Benchmark before committing -- see risks.*

### Layer 2 -- a predictive model of HIM, trained on his own latents
A small sequence model (GRU or tiny transformer, single-digit MB) trained to
predict the next latent, and the latent N seconds ahead, from recent history
plus time of day. Retrained nightly on everything collected so far.

**This is the JEPA objective applied to his life rather than to the internet.**
It is self-supervised, it needs no labels, and it is the layer that genuinely
accumulates. On day one it predicts badly. After a month it knows that he sits
down at 4, that the mug appears before the laptop opens, that Sunday looks
different.

### Layer 3 -- intrinsic motivation from LEARNING PROGRESS, not surprise
Prediction error drives attention -- but **not raw error**, which is the
classic failure: a system that seeks surprise will find a source of pure noise
and stare at it forever, because noise is maximally unpredictable. That is the
"noisy TV" problem and it is a well-documented dead end.

The fix is to reward **reducible** error: how much the prediction improved on
this kind of thing recently. Attention goes to whatever it is currently
getting better at. Something genuinely random produces no learning progress
and is correctly ignored, and something already mastered is boring.

This is the layer that makes curiosity real instead of decorative, and it
replaces the hand-tuned `curiosity` weight in `behaviors.py` with something
measured.

### Layer 4 -- policy that improves from his reactions
The 9 gestures become a contextual bandit over the Layer 2 state, with reward
from:
- **explicit feedback** -- `mood.py` already has `praise` and `scold` events
  and a dashboard button for each. That is a labelled reward channel sitting
  unused.
- **implicit feedback** -- did he look at the robot after it acted. The
  detector already measures this for free.

Start pessimistic and conservative, so a new behaviour has to earn its place.
This is the layer where "it grew" becomes a number he can see: reward per
behaviour, over weeks.

## What is honest about this

**It is not sentience and I will not call it that.** Nothing here is aware of
anything. What it is: a system whose behaviour at week 8 is a function of
weeks 1-7, measurably, in a way the current one is not. That is a real and
much rarer thing than a chatbot bolted to a servo, and it is worth building
on its own terms.

**It needs data, and that is slow.** Layers 2-4 are nearly useless for the
first week or two. There is no shortcut; that is what learning from his life
means. The upside is that the thing he wants -- growth -- is only observable
on that timescale anyway.

**The encoder cannot be trained here, only used.** Pretraining a JEPA is a
large-cluster job. Fine-tuning it on his own video is possible in principle
with a frozen backbone, but the payoff is unclear and it should not be the
first thing attempted.

## Risks, in order

1. **The encoder may not fit the latency budget.** Benchmark V-JEPA 2 ViT-L
   and VideoMAE-base on the MSI before anything else. If neither clears a
   couple of hundred ms per sample, drop to per-frame DINOv2 features and
   accept losing motion information.
2. **Latents may not separate the states he cares about.** Testable
   immediately and cheaply: collect a few hours, cluster the latents, and
   check whether the clusters correspond to anything (at desk, away, typing).
   If they do not, the whole tower above is built on sand. **Do this second.**
3. **Reward is sparse.** He will not press praise/scold often. The implicit
   channel matters more than it looks.
4. **Nightly retraining can regress.** Keep the previous model and compare on
   held-out data before swapping.

## Order of work

1. Benchmark the encoder on the MSI. Latency and VRAM. (risk 1)
2. Log latents for a few days and cluster them. Does the representation see
   his life at all? (risk 2) **Stop here if the answer is no.**
3. Layer 2, the predictor. Nightly training, measure held-out prediction error
   falling week over week. That curve IS the growth.
4. Layer 3, learning progress into attention and mood.
5. Layer 4, the bandit over gestures.

Steps 1 and 2 are a few days and they decide whether any of the rest is real.
Nothing above step 2 should be built until step 2 passes.
