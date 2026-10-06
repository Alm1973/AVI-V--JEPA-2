"""Dispatch a validated tool call to the thing that does it.

The router decides WHAT should happen and returns tool calls; this is where
they become effects. Keeping the two apart is what let the whole decision
layer be tested with no hardware, and the same split holds here: a skill is
registered as a plain callable, so a test can register a recording stub for
`lamp` and exercise the real dispatch logic without a lamp.

TWO RULES, both learned the hard way elsewhere in this project:

  A call is re-validated HERE, not trusted because the router validated it.
  The router is one caller; the dashboard and the local fallback are others,
  and a guard that only runs on one path is not a guard.

  An unknown or failing skill returns a REFUSAL, never an exception. A tool
  call that explodes must not take down the arm loop -- that is exactly how a
  SerialException killed the motion thread silently on 2026-10-05.
"""
import time

from ..brain import tools


class Result:
    __slots__ = ("name", "ok", "text", "data", "error", "ms")

    def __init__(self, name, ok, text=None, data=None, error=None, ms=0.0):
        self.name = name
        self.ok = ok
        self.text = text          # something sayable, if the skill produced one
        self.data = data
        self.error = error
        self.ms = ms

    def __repr__(self):
        return (f"<Result {self.name} {'ok' if self.ok else 'failed'}"
                f"{' ' + repr(self.text) if self.text else ''}"
                f"{' ' + self.error if self.error else ''}>")


class Registry:
    def __init__(self, clock=time.perf_counter):
        self.handlers = {}
        self.clock = clock
        self.calls = []

    def register(self, name, fn):
        if name not in tools.BY_NAME:
            raise ValueError(
                f"{name!r} is not in the tool schema. Registering a skill the "
                f"LLM has never been told about means it can only ever be "
                f"reached by a local rule, which is a silent half-feature.")
        self.handlers[name] = fn
        return fn

    def skill(self, name):
        """Decorator form."""
        def wrap(fn):
            self.register(name, fn)
            return fn
        return wrap

    def missing(self):
        """Tools in the schema with nothing behind them yet.

        Worth being able to ask: the schema is what the model is told it can
        do, so anything in here is a promise the robot cannot keep.
        """
        return sorted(set(tools.BY_NAME) - set(self.handlers))

    def run(self, name, args=None):
        """Execute one call. Never raises."""
        started = self.clock()
        clean, err = tools.validate_call(name, args or {})
        if err is not None:
            return Result(name, False, error=err,
                          ms=(self.clock() - started) * 1000)
        handler = self.handlers.get(name)
        if handler is None:
            return Result(name, False, error=f"{name} is not wired up yet",
                          ms=(self.clock() - started) * 1000)
        try:
            out = handler(**clean)
        except Exception as exc:
            return Result(name, False,
                          error=f"{type(exc).__name__}: {exc}",
                          ms=(self.clock() - started) * 1000)
        text, data = self._unpack(out)
        result = Result(name, True, text=text, data=data,
                        ms=(self.clock() - started) * 1000)
        self.calls.append((name, clean, result.ok))
        return result

    def run_all(self, calls):
        """Execute a decision's calls in order, stopping at the first failure.

        In order and stopping, because tool calls in one turn are usually a
        sequence rather than a set -- "set a timer and tell me the time" is
        fine either way, but "focus_start then lamp on" reads as one action and
        half of it happening is worse than none.
        """
        out = []
        for name, args in calls:
            result = self.run(name, args)
            out.append(result)
            if not result.ok:
                break
        return out

    @staticmethod
    def _unpack(out):
        """Skills may return a string, a (text, data) pair, a dict, or None."""
        if out is None:
            return None, None
        if isinstance(out, str):
            return out, None
        if isinstance(out, tuple) and len(out) == 2:
            return out[0], out[1]
        if isinstance(out, dict):
            return out.get("text"), out
        return None, out
