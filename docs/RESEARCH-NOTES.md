# What similar projects already learned, and what AVI should change

Read 2026-10-05, after M4. Only findings that change a decision are here;
things that merely agree with the plan are noted in one line and dropped.

Four items are actionable. **One contradicts a design already written into
BUILD_SPEC**, and that is the most valuable thing in this document.

---

## 1. The novelty effect wears off in about two months — and that is the case FOR the learning stack

Long-term HRI studies converge on the same finding: engagement with a social
robot declines after the novelty passes, typically settling after **about two
months**, and the decline shows up in behavioural measures, not just surveys.
Children's engagement with robot tutors drops over repeated sessions even when
the robot keeps working perfectly.

**Why this matters more than it sounds.** Everything that makes AVI charming
on day one — the gestures, the chirps, the mood — is exactly the kind of thing
this literature says stops working by week eight. A fixed-weight personality
engine has a shelf life. That is not a criticism of M2; it is the reason M6
exists.

**What to do:** make engagement a **measured quantity with a two-month
horizon**, not an impression. The tables for it already exist — `interactions`
and `feedback` in `avi_bot/memory/db.py`. Track per week:

- utterances directed at AVI
- acceptance rate of its suggestions (`Store.acceptance_rate`)
- time at desk with it awake vs asleep

If those are flat or rising at week 10, the learning stack is doing its job.
If they fall like the literature says, it is decoration. **This is the only
honest test of the whole project**, and it cannot be run in an evening.

## 2. Reward learning PROGRESS, not surprise — confirmed, and there is a 2025 method for it

`LEARNING-DESIGN.md` Layer 3 already says to use learning progress rather than
raw prediction error, on the reasoning that a surprise-seeker finds a noise
source and stares at it. That reasoning is the well-known **noisy-TV problem**,
it traces to Oudeyer's Intelligent Adaptive Curiosity ("maximise learning
progress", seeking what is "neither too predictable nor too unpredictable"),
and it remains an open problem rather than a solved one.

Newer and directly usable: **Learning Progress Monitoring** rewards *model
improvement* instead of prediction error or novelty
([arXiv 2509.25438](https://arxiv.org/pdf/2509.25438)).

**What to do:** implement Layer 3 as the *change* in held-out prediction error
for a region of latent space over a sliding window, not the error itself.
Concretely: keep per-cluster error history, and let curiosity attend to the
cluster whose error has fallen most recently. A cluster whose error is high and
*flat* is noise and gets ignored — which is the whole point.

## 3. AVI's focus-nudge trigger is the kind the research says does not work

**This is the contradiction.** BUILD_SPEC 9 specifies: *"Phone use over 2 min →
an `ahem`."* That is a **time-based trigger**, and the interruption literature
is specifically unkind to those:

> Time-based triggers, such as user idleness, are unreliable indicators of
> readiness for assistance because they often misinterpret user intent; pauses
> may reflect planning, reading, or external coordination rather than
> confusion or need.

What does work is interrupting at **task transitions / breakpoints** —
subtask completion, boundary shifts — and poorly timed interruptions measurably
increase the chance the user ignores or rejects the system entirely. So a
mistimed `ahem` does not just fail once; it trains him to disregard AVI.

**What to do, and it is cheap because the detector already exists:** gate the
nudge on a **breakpoint**, not a stopwatch. `assistant/change.py` already
fires on "the scene changed and then settled", which is a breakpoint detector
in all but name, and the latent stream gives a second, semantic one (the
predicted state changed). Revised rule for M5's `focus_start`:

- accumulate phone time as evidence, **do not act on it alone**
- nudge when phone time is over threshold **AND** a breakpoint has just
  fired (he put the phone down, shifted posture, the scene settled)
- keep the existing cap of 2 nudges per session
- if no breakpoint arrives, **say nothing** and let the end-of-session
  summary carry it

BUILD_SPEC needs amending for this; it is recorded here and flagged in the
spec rather than silently changed.

## 4. Expressive motion is worth more than it costs — and AVI is in a real product category

Apple's **ELEGNT** (already cited in BUILD_SPEC 12) studied a lamp-like
non-anthropomorphic robot and found that **expression-driven movement
significantly improved user engagement and perceived qualities versus
function-driven movement, most strongly in social tasks**. AVI is the same
shape of problem: a few joints, no face, no gripper.

Two things follow:

- The motion-first ordering of this project (M1/M2 before the brain) is the
  right way round, and the 13 gestures are not decoration. Keep investing there
  over adding skills.
- Apple is reportedly building a desktop robot (**J595**) with an iPad-like
  display on a rotating arm, with a large team. AVI is not a novelty; it is a
  hobby-scale attempt at a category a trillion-dollar company is staffing up
  for. Worth knowing when deciding how much polish is "enough".

## 5. Memory: add retrospective refinement, not just a bigger store

**Reflective Memory Management**
([arXiv 2503.08026](https://arxiv.org/pdf/2503.08026), Google) splits memory
into two loops: **prospective reflection**, summarising interactions at several
granularities into a memory bank, and **retrospective reflection**, refining
*retrieval* online based on which memories the model actually cited. Reported
**>10% accuracy improvement** on LongMemEval.

AVI's `facts` table already does half of the cheap version: `Store.recall()`
marks facts as used and eviction takes the oldest *unused* fact. But "used"
currently means "was retrieved", which is not the same as "was useful" — a
fact retrieved and ignored looks identical to one that answered the question.

**What to do:** record **citation**, not just retrieval. One extra column and
one call: after a reply, mark which of the recalled facts the model's answer
actually drew on, and rank future retrieval by citations rather than
retrievals. Small, local, and it directly improves the thing he asked for —
the robot knowing *him* rather than accumulating trivia.

---

## Not changing anything (noted and dropped)

- **Reachy Mini** ($399, 11", Python, HF integration) — already the source of
  the layer/`moves.py` structure in BUILD_SPEC 12. Nothing new to take.
- **MemAgent** / long-context memory architectures — aimed at O(N) scaling
  over very long contexts. AVI's context is capped at 6000 characters by free
  tiers; this is solving a problem it does not have.
- **VLA models** (π0, OpenVLA, GR00T) — assessed in
  `ARCHITECTURE-CHOICES.md`; manipulation policies, and AVI has no gripper.

## Order these should be done in

1. **The focus-nudge breakpoint fix** (§3) — before M5 builds the wrong trigger.
2. **Citation-weighted recall** (§5) — hours, and compounds from day one.
3. **Learning progress, not error** (§2) — when Layer 3 gets built.
4. **The two-month engagement metric** (§1) — set up now, read in December.

## Sources

- Novelty effect / long-term engagement: [Springer s12369-023-01076-z](https://link.springer.com/10.1007/s12369-023-01076-z), [Frontiers in Robotics and AI](https://www.frontiersin.org/journals/robotics-and-ai/articles/10.3389/frobt.2021.669972/pdf), [longitudinal child-robot study](https://arxiv.org/pdf/2404.10593)
- Learning progress: [Oudeyer, Intelligent Adaptive Curiosity](https://www.frontiersin.org/journals/neuroscience/articles/10.3389/neuro.01.1.1.017.2007/pdf), [Beyond Noisy-TVs: Learning Progress Monitoring](https://arxiv.org/pdf/2509.25438)
- Interruption timing: [Precision Proactivity](https://arxiv.org/pdf/2505.10742), [Bailey & Konstan, CHB](https://interruptions.net/literature/Bailey-CHB06_1.pdf), [Microsoft Research, Paying Attention to Interruption](https://www.microsoft.com/en-us/research/?p=183260)
- Expressive motion: [ELEGNT (Apple)](https://pr-mlr-shield-prod.apple.com/research/elegnt-expressive-functional-movement), [J595 desktop robot report](https://www.iclarified.com/96374/apple-researchers-unveil-lamplike-robot-with-expressive-movements-video)
- Memory: [Reflective Memory Management](https://arxiv.org/pdf/2503.08026)
- Reachy Mini: [Hugging Face](https://huggingface.co/blog/reachy-mini), [Pollen Robotics](https://pollen-robotics.com/reachy-mini)
