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

# (name, base_url, model, requests/day at the free tier, requests/min)
# Limits are from the Aug-Oct 2026 docs listed in BUILD_SPEC 17 and MOVE.
# Re-check them when a provider starts refusing; do not raise them to make a
# test pass.
CHAIN = [
    ("groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-20b",
     1000, 30),
    ("openrouter", "https://openrouter.ai/api/v1",
     "meta-llama/llama-3.3-70b-instruct:free", 50, 20),
    ("github", "https://models.github.ai/inference", "openai/gpt-4o-mini",
     150, 15),
    ("local", None, "Qwen2.5-VL-3B-Instruct", None, None),
]

KEY_ENV = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
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
        self.db = sqlite3.connect(path)
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

    def __init__(self, budget=None, chain=None, send=None, env=None,
                 clock=time.time):
        self.budget = budget or Budget()
        self.chain = list(chain if chain is not None else CHAIN)
        self.send = send or self._real_send
        self.env = env if env is not None else os.environ
        self.clock = clock
        self.tried = []

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
            try:
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
        except ImportError as exc:
            raise ProviderError(name, f"openai sdk missing: {exc}")
        client = OpenAI(base_url=url, api_key=self.env.get(KEY_ENV[name]),
                        timeout=20.0)
        try:
            kwargs = {"model": model, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise ProviderError(name, f"{type(exc).__name__}: {exc}")
