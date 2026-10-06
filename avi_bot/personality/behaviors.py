"""What to do when nobody has asked for anything. BUILD_SPEC 5.4.

Every 2-6 seconds, score each idle behaviour, then sample:

    utility = weight(emotion) x context x cooldown

and pick with a softmax at temperature 0.5. Sampling rather than taking the
best one is the whole point: an argmax over a slowly-changing mood produces the
same behaviour forever, which reads as broken rather than as calm.

Stillness dominates on purpose. Reachy Mini's idle policy rests about 60 % of
the time, and a desk robot that fidgets continuously is furniture you want to
unplug. So `still` carries a high weight in every mood, and the interesting
behaviours have to earn their way past it.

Nothing here moves a servo. `choose()` returns a name and the caller decides
what that means, so a 30-minute run can be simulated in milliseconds.
"""
import math
import random

# Per behaviour: how much each emotion wants it, how long before it may repeat,
# and whether it needs him present. Weights are starting points.
#
# `needs_target` behaviours are dropped when there is nothing to look at, and
# `needs_present` ones when he is not there -- rather than being given a low
# weight, because "glance at you" with nobody in the room is not unlikely, it
# is meaningless.
BEHAVIOURS = {
    "still": {
        "cooldown": 0.0,            # may always follow itself
        "weights": {"content": 6.0, "happy": 4.0, "excited": 2.5,
                    "curious": 3.0, "bored": 3.0, "sulky": 5.0,
                    "sad": 5.0, "sleepy": 5.0},
    },
    "glance_at_you": {
        "cooldown": 20.0, "needs_present": True,
        "weights": {"content": 1.2, "happy": 1.8, "excited": 2.0,
                    "curious": 1.0, "bored": 2.2, "sulky": 0.3,
                    "sad": 0.6, "sleepy": 0.4},
    },
    "look_around": {
        "cooldown": 25.0,
        "weights": {"content": 1.0, "happy": 1.2, "excited": 1.6,
                    "curious": 1.6, "bored": 2.0, "sulky": 0.2,
                    "sad": 0.4, "sleepy": 0.2},
    },
    "inspect_target": {
        "cooldown": 15.0, "needs_target": True,
        "weights": {"content": 1.5, "happy": 1.8, "excited": 2.2,
                    "curious": 5.0, "bored": 1.5, "sulky": 0.4,
                    "sad": 0.5, "sleepy": 0.2},
    },
    "fidget": {
        "cooldown": 30.0,
        "weights": {"content": 0.6, "happy": 1.2, "excited": 2.2,
                    "curious": 0.8, "bored": 2.4, "sulky": 0.2,
                    "sad": 0.3, "sleepy": 0.1},
    },
    "doze": {
        "cooldown": 60.0,
        "weights": {"content": 0.4, "happy": 0.1, "excited": 0.0,
                    "curious": 0.0, "bored": 0.6, "sulky": 0.5,
                    "sad": 0.8, "sleepy": 6.0},
    },
    "greet": {
        "cooldown": 300.0, "needs_present": True,
        "weights": {"content": 0.2, "happy": 1.0, "excited": 2.0,
                    "curious": 0.2, "bored": 0.4, "sulky": 0.0,
                    "sad": 0.1, "sleepy": 0.1},
    },
    "sulk_droop": {
        "cooldown": 40.0,
        "weights": {"content": 0.0, "happy": 0.0, "excited": 0.0,
                    "curious": 0.0, "bored": 0.0, "sulky": 4.0,
                    "sad": 2.0, "sleepy": 0.0},
    },
    "celebrate": {
        "cooldown": 120.0,
        "weights": {"content": 0.0, "happy": 0.4, "excited": 2.0,
                    "curious": 0.0, "bored": 0.0, "sulky": 0.0,
                    "sad": 0.0, "sleepy": 0.0},
    },
}

# The gesture each behaviour plays, where it plays one. None means it is a
# posture or a look rather than a gesture, and the caller handles it.
GESTURE = {
    "fidget": "excited_wiggle",
    "doze": "doze",
    "greet": "perk_up",
    "sulk_droop": "droop",
    "celebrate": "celebrate",
    "look_around": "thinking",
    "inspect_target": "curious_lean",
    "glance_at_you": "ahem",
    "still": None,
}

TEMPERATURE = 0.5
GAP_MIN, GAP_MAX = 2.0, 6.0

# Context gates from BUILD_SPEC 5.4. Focus mode is deliberately severe: the
# whole point of asking for focus is that the robot stops being interesting.
FOCUS_ALLOWED = ("still", "glance_at_you")
FOCUS_MULTIPLIER = {"still": 8.0, "glance_at_you": 0.3}
QUIET_ALLOWED = ("still", "doze")
AWAY_ALLOWED = ("still", "doze", "look_around")


class BehaviourSelector:
    def __init__(self, rng=None, clock=None):
        self.rng = rng or random.Random()
        self.clock = clock
        self.last_run = {}
        self.history = []
        self.next_due = None

    # --- scoring ---------------------------------------------------------
    def cooldown_factor(self, name, now):
        """0 right after it ran, ramping to 1 once its cooldown has passed.

        A hard block would make the set of available behaviours flip
        discontinuously; a ramp lets something become gradually plausible
        again, which is also what stops short loops.
        """
        cd = BEHAVIOURS[name]["cooldown"]
        if cd <= 0:
            return 1.0
        since = now - self.last_run.get(name, -1e9)
        return max(0.0, min(1.0, since / cd))

    def utilities(self, mood, now, quiet_hours=False):
        emotion = mood.emotion(now)
        out = {}
        for name, spec in BEHAVIOURS.items():
            if spec.get("needs_present") and not mood.present:
                continue
            if spec.get("needs_target") and mood.curiosity_target is None:
                continue
            if mood.focus and name not in FOCUS_ALLOWED:
                continue
            if quiet_hours and name not in QUIET_ALLOWED:
                continue
            if not mood.present and name not in AWAY_ALLOWED:
                continue
            u = spec["weights"].get(emotion, 0.0)
            if mood.focus:
                u *= FOCUS_MULTIPLIER.get(name, 1.0)
            u *= self.cooldown_factor(name, now)
            if u > 0.0:
                out[name] = u
        return out

    def choose(self, mood, now=None, quiet_hours=False):
        """Pick one behaviour. Returns its name, or "still" if nothing scores."""
        now = (self.clock() if self.clock else 0.0) if now is None else now
        us = self.utilities(mood, now, quiet_hours=quiet_hours)
        if not us:
            pick = "still"
        else:
            names = list(us)
            # Softmax on the logs, so a weight of 0 stays impossible rather
            # than becoming merely unlikely, and the temperature means what it
            # means in the spec.
            scores = [math.log(us[n]) / TEMPERATURE for n in names]
            top = max(scores)
            exps = [math.exp(s - top) for s in scores]
            total = sum(exps)
            r = self.rng.random() * total
            pick = names[-1]
            acc = 0.0
            for n, e in zip(names, exps):
                acc += e
                if r <= acc:
                    pick = n
                    break
        self.last_run[pick] = now
        self.history.append((now, pick))
        del self.history[:-400]
        self.next_due = now + self.rng.uniform(GAP_MIN, GAP_MAX)
        return pick

    def due(self, now):
        return self.next_due is None or now >= self.next_due

    # --- what the caller does with it ------------------------------------
    def gesture_for(self, name):
        return GESTURE.get(name)

    def repeats_longest_run(self, window=None):
        """Longest run of one behaviour back to back.

        M2's acceptance test is that a 30-minute unattended run never repeats
        the same behaviour sequence three times in a row, so this is the thing
        being measured, exposed rather than reimplemented in the test.
        `still` is excluded: stillness following stillness is the resting
        state, not a stuck loop.
        """
        names = [n for _, n in (self.history if window is None
                                else self.history[-window:])]
        longest = run = 0
        prev = None
        for n in names:
            if n == "still":
                run, prev = 0, None
                continue
            run = run + 1 if n == prev else 1
            prev = n
            longest = max(longest, run)
        return longest
