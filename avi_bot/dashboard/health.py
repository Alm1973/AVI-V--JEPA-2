"""Is every part of AVI actually running? One answer per part.

Two silent failures in one day motivated this: latent recording switched off
by every restart, and base_yaw going limp mid-survey with nothing in the log.
Each check reports ok / stale / off with the evidence, so "is it learning?"
has an answer that does not depend on reading logs.
"""

# A loop that has not produced anything for this long is not running.
PRESENCE_STALE_S = 120          # samples every 20 s
LATENT_STALE_S = 120            # one latent every 15 s


def check(now, presence_last_ts=None, latents_on=False, latent_last_ts=None,
          latent_error=None, brain_error=None, brain_started=False,
          scan_every_min=0.0, last_scan_ts=None, ends_in_s=None):
    out = {}
    if presence_last_ts is None:
        out["presence"] = {"state": "stale", "why": "no looks recorded"}
    else:
        age = now - presence_last_ts
        out["presence"] = {"state": "ok" if age < PRESENCE_STALE_S
                           else "stale", "last_look_s": round(age)}
    if not latents_on:
        out["latents"] = {"state": "off"}
    elif latent_error:
        out["latents"] = {"state": "broken", "why": latent_error}
    elif latent_last_ts is None or now - latent_last_ts > LATENT_STALE_S:
        out["latents"] = {"state": "stale",
                          "last_s": None if latent_last_ts is None
                          else round(now - latent_last_ts)}
    else:
        out["latents"] = {"state": "ok",
                          "last_s": round(now - latent_last_ts)}
    if brain_error:
        out["brain"] = {"state": "broken", "why": brain_error}
    else:
        out["brain"] = {"state": "ok" if brain_started else "idle"}
    out["desk_scans"] = {
        "state": "on" if scan_every_min else "off",
        "every_min": scan_every_min or 0,
        "last_s": None if last_scan_ts is None else round(now - last_scan_ts)}
    if ends_in_s is not None:
        out["dashboard"] = {"state": "ok", "ends_in_min":
                            round(ends_in_s / 60.0)}
    bad = [k for k, v in out.items() if v["state"] in ("stale", "broken")]
    out["summary"] = "all running" if not bad else "check: " + ", ".join(bad)
    return out
