"""The free provider chain, and the guard that keeps it free. BUILD_SPEC 8.2.

Four providers in a fixed order, each OpenAI-compatible so one client talks to
all of them with a different base_url. Groq first because it is fastest and
retains nothing by default; local Qwen last because it is unlimited but can
only handle simple intents.

TWO SEPARATE REASONS TO MOVE ON, and conflating them is how a chain ends up
spending money:

  FAILURE -- an HTTP 429, a timeout, a 5xx. Try the next provider now.
  BUDGET  -- this provider has used 80% of its free daily allowance. Skip it
             BEFORE sending, for the rest of the day.

The budget guard is the whole point of the ordering. Discovering a limit by
being refused is fine on a free tier and catastrophic on a paid one, so the
count is kept locally in SQLite and checked first. Nothing here can upgrade a
plan, and there is no code path that sends a payment detail.

WHAT IS NEVER SENT (BUILD_SPEC 8.2): images, audio, raw logs, the calendar
URL, keys. `redact()` enforces the last two on the way out rather than
trusting every caller to remember.
"""
import os
import re
import sqlite3
import time
from datetime import date

from avi_bot.memory import locked

# (name, base_url, model, requests/day at the free tier, requests/min)
# Limits are from the Aug-Oct 2026 docs listed in BUILD_SPEC 17 and MOVE.
# Re-check them when a provider starts refusing; do not raise them to make a
# test pass.
CHAIN = [
    ("groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-20b",
     1000, 30),
    # Groq's free limits are PER MODEL: 8K tokens/min and 200K tokens/day
    # each (console.groq.com/docs/rate-limits, 2026-10-08). A turn is ~2.3K
    # tokens, so one model is ~85 turns a day -- a morning of testing used
    # it up and every turn went to OpenRouter's 50/day. Same key, separate
    # buckets; both made the right tool calls from the Mac (wake, nudge).
    ("groq-120b", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b",
     1000, 30),
    ("groq-qwen", "https://api.groq.com/openai/v1", "qwen/qwen3.8-27b",
     1000, 30),
    # llama-3.3-70b-instruct:free was withdrawn (404 "unavailable for
    # free", 2026-10-08). Nemotron 3 Super is free, takes tools, and made
    # the right nudge call in 2.4 s from the MSI. Never a paid slug here.
    ("openrouter", "https://openrouter.ai/api/v1",
     "nvidia/nemotron-3-super-120b-a12b:free", 50, 20),
    ("github", "https://models.github.ai/inference", "openai/gpt-4o-mini",
     150, 15),
    ("local", None, "Qwen2.5-VL-3B-Instruct", None, None),
]

KEY_ENV = {"groq": "GROQ_API_KEY", "groq-120b": "GROQ_API_KEY",
           "groq-qwen": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
           "github": "GITHUB_TOKEN", "local": None}

# Stay this far inside each free limit. Not 100%: the counter and the
# provider's own counter can disagree about the day boundary and about
# requests that failed in flight.
BUDGET_FRACTION = 0.80

SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_usage (
  day      TEXT NOT NULL,
  provider TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, provider)
);
"""

# Anything that looks like a key or the calendar's secret address. Both are
# disqualifying rather than embarrassing: the iCal URL IS the credential, so a
# model provider logging a prompt that contains it has read access to his
# calendar forever.
_SECRETS = [
    (re.compile(r"\b(?:sk|gsk|xpl|ghp|gho|github_pat)_[A-Za-z0-9_\-]{10,}"),
     "[key]"),
    (re.compile(r"https?://[^\s]*calendar\.google\.com[^\s]*"), "[calendar]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "[email]"),
]


def redact(text):
    """Strip secrets from anything about to leave the machine."""
    if not text:
        return text
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


class Budget:
    def __init__(self, path=":memory:", today=None):
        # Shared across the dashboard's request threads; the host
        # serialises turns (see memory/db.py).
        self.db = locked.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._today = today or (lambda: date.today().isoformat())

    def used(self, provider):
        row = self.db.execute(
            "SELECT requests FROM provider_usage WHERE day=? AND provider=?",
            (self._today(), provider)).fetchone()
        return row["requests"] if row else 0

    def note(self, provider, failed=False):
        self.db.execute(
            "INSERT INTO provider_usage (day, provider, requests, failures)"
            " VALUES (?,?,1,?) ON CONFLICT(day, provider) DO UPDATE SET"
            " requests = requests + 1, failures = failures + ?",
            (self._today(), provider, int(failed), int(failed)))
        self.db.commit()

    def allows(self, provider, limit):
        if limit is None:
            return True                      # local: unlimited
        return self.used(provider) < limit * BUDGET_FRACTION

    def report(self):
        return {r["provider"]: {"requests": r["requests"],
                                "failures": r["failures"]}
                for r in self.db.execute(
                    "SELECT * FROM provider_usage WHERE day=?",
                    (self._today(),))}

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):
        # Budgets are created per Chain and Chains are created per test; an
        # unclosed sqlite handle per instance turns a clean run into a wall of
        # ResourceWarnings that hides real output.
        try:
            self.db.close()
        except Exception:
            pass


# "try again in 2.5s", "450ms", and -- when a day's tokens are spent --
# "8m15.072s" or "1h2m3s", which the seconds-only pattern read as nothing.
_RETRY_IN = re.compile(
    r"try again in ((?:[0-9.]+h)?(?:[0-9.]+m(?!s))?(?:[0-9.]+(?:ms|s))?)",
    re.I)
_PART = re.compile(r"([0-9.]+)(h|ms|m|s)", re.I)
_UNIT_S = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def retry_after(why):
    """Seconds a rate-limit refusal says to wait, or None if it is not one."""
    if not why or "429" not in why:
        return None
    m = _RETRY_IN.search(why)
    if not m or not m.group(1):
        return None
    return sum(float(v) * _UNIT_S[u.lower()]
               for v, u in _PART.findall(m.group(1)))


class ProviderError(Exception):
    """A provider failed in a way that means: try the next one."""

    def __init__(self, provider, why, retryable=True):
        super().__init__(f"{provider}: {why}")
        self.provider = provider
        self.why = why
        self.retryable = retryable


class Chain:
    """Walks the provider list. The network call is injected, so every
    ordering, budget and failover rule is testable with no keys and no
    network -- which is the only way this could be built remotely at all."""

    # A per-minute limit says when to try again ("Please try again in
    # 6.02s"). Waiting that long once beats failing the turn: the free tier
    # allows ~3 brain turns a minute, and the 4th used to come back
    # "confused" (2026-10-08). Longer waits are not worth holding a turn.
    RETRY_MAX_WAIT_S = 10.0

    def __init__(self, budget=None, chain=None, send=None, env=None,
                 clock=time.time, sleep=time.sleep):
        self.sleep = sleep
        self.budget = budget or Budget()
        self.chain = list(chain if chain is not None else CHAIN)
        self.send = send or self._real_send
        self.env = env if env is not None else os.environ
        self.clock = clock
        self.tried = []
        # provider -> when it said to come back ("try again in 8m15s" when
        # its day's tokens are spent). Skipped until then instead of being
        # asked, and refused, on every turn.
        self.cool_until = {}

    def have_key(self, name):
        var = KEY_ENV.get(name)
        if var is None:
            return True                       # local needs none
        return bool(self.env.get(var))

    def available(self):
        """Providers that could be used right now, in order, with reasons."""
        out = []
        for name, url, model, per_day, per_min in self.chain:
            if not self.have_key(name):
                out.append((name, False, "no key"))
            elif not self.budget.allows(name, per_day):
                out.append((name, False,
                            f"budget {self.budget.used(name)}/{per_day}"))
            else:
                out.append((name, True, "ready"))
        return out

    def ask(self, messages, tools=None):
        """First provider that answers. Returns (name, result).

        Raises ProviderError only if EVERY provider is unusable, because at
        that point there is nothing left to fall back to and the caller owes
        the user a confused chirp rather than a retry.
        """
        self.tried = []
        safe = [{**m, "content": redact(m.get("content"))} for m in messages]
        last = None
        for name, url, model, per_day, per_min in self.chain:
            if not self.have_key(name):
                self.tried.append((name, "no key"))
                continue
            if not self.budget.allows(name, per_day):
                self.tried.append((name, "over budget"))
                continue
            back = self.cool_until.get(name, 0.0) - self.clock()
            if back > 0:
                self.tried.append((name, f"cooling down {back:.0f}s"))
                continue
            try:
                try:
                    result = self.send(name, url, model, safe, tools)
                except ProviderError as exc:
                    wait = retry_after(exc.why)
                    if wait is not None and wait > self.RETRY_MAX_WAIT_S:
                        self.cool_until[name] = self.clock() + wait
                    if wait is None or wait > self.RETRY_MAX_WAIT_S:
                        raise
                    self.budget.note(name, failed=True)
                    self.tried.append((name, f"rate limited; retried after "
                                             f"{wait:.1f}s"))
                    self.sleep(wait + 0.3)
                    result = self.send(name, url, model, safe, tools)
            except ProviderError as exc:
                self.budget.note(name, failed=True)
                self.tried.append((name, exc.why))
                last = exc
                continue
            except Exception as exc:                  # treat as retryable
                self.budget.note(name, failed=True)
                self.tried.append((name, f"{type(exc).__name__}: {exc}"))
                last = ProviderError(name, str(exc))
                continue
            self.budget.note(name)
            self.tried.append((name, "ok"))
            return name, result
        raise ProviderError("chain", f"every provider unusable: {self.tried}"
                            ) from last

    # --- the real network call, kept small and last ---------------------
    def _real_send(self, name, url, model, messages, tools):
        if name == "local":
            raise ProviderError(
                "local", "the local fallback is not a chat provider; the "
                "router uses intents.match plus a multiple-choice VLM call "
                "instead", retryable=False)
        try:
            from openai import OpenAI
        except ImportError:
            return self._http_send(name, url, model, messages, tools)
        client = OpenAI(base_url=url, api_key=self.env.get(KEY_ENV[name]),
                        timeout=20.0)
        try:
            kwargs = {"model": model, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise ProviderError(name, f"{type(exc).__name__}: {exc}")

    def _http_send(self, name, url, model, messages, tools):
        """The same call over plain HTTPS, when the openai SDK is absent.

        The MSI's venv has no openai package, so every question the local
        rules could not answer came back "confused" -- found 2026-10-06 when
        the brain was first hosted on the real arm. Installing packages there
        needs Shaurya's OK, and the API is JSON over HTTPS anyway. The router
        already reads a plain-dict response. The key goes in a header and
        never into an error message.
        """
        import json as _json
        import urllib.error
        import urllib.request
        body = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            url.rstrip("/") + "/chat/completions",
            data=_json.dumps(body).encode(),
            headers={"Authorization": "Bearer "
                     + (self.env.get(KEY_ENV[name]) or ""),
                     "Content-Type": "application/json",
                     # urllib's default agent is refused by some CDNs.
                     "User-Agent": "avi-desk/1.0"},
            method="POST")
        try:
            with self.urlopen(req, timeout=20.0) as resp:
                return _json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read()[:300].decode(errors="replace")
            except Exception:
                detail = ""
            finally:
                exc.close()
            raise ProviderError(name, f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            raise ProviderError(name, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def urlopen(req, timeout):
        import urllib.request
        return urllib.request.urlopen(req, timeout=timeout)
