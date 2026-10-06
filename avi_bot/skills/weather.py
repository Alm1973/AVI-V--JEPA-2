"""Weather from Open-Meteo. BUILD_SPEC 9.

Free, no key, and the terms require attribution (CC BY 4.0) which belongs in
the README. Phoenix coordinates live in the config.

The acceptance test is "fixture JSON -> the right sentence", so the FETCH and
the SENTENCE are separate functions. That split matters beyond testing: the
sentence has to fit the 20-word speech cap, and a forecast is the easiest
thing in this project to accidentally turn into a paragraph.
"""
import json

PHOENIX = (33.4484, -112.0740)
ENDPOINT = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo WMO weather codes, collapsed to words a desk robot would use.
# Deliberately coarse: "light drizzle" and "moderate drizzle" are the same
# thing to someone deciding whether to take a jacket.
CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "drizzling", 53: "drizzling", 55: "drizzling",
    61: "raining", 63: "raining", 65: "pouring",
    66: "freezing rain", 67: "freezing rain",
    71: "snowing", 73: "snowing", 75: "snowing heavily",
    80: "showers", 81: "showers", 82: "heavy showers",
    95: "thunderstorms", 96: "thunderstorms", 99: "thunderstorms",
}


def url(lat=PHOENIX[0], lon=PHOENIX[1], days=2):
    return (f"{ENDPOINT}?latitude={lat}&longitude={lon}"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min"
            f"&temperature_unit=fahrenheit&timezone=America%2FPhoenix"
            f"&forecast_days={days}")


def describe(payload, day="today"):
    """A forecast as one short sentence. Returns (text, data) or (None, None).

    Takes already-parsed JSON so the same function serves the fixture test and
    the live call.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        payload = json.loads(payload)
    daily = (payload or {}).get("daily") or {}
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    index = {"today": 0, "tomorrow": 1}.get(day, 0)
    if index >= len(codes) or index >= len(highs):
        return None, None
    word = CODES.get(int(codes[index]), "unsettled")
    high = round(float(highs[index]))
    low = round(float(lows[index])) if index < len(lows) else None
    when = "Today" if index == 0 else "Tomorrow"
    if low is None:
        text = f"{when}: {word}, {high} degrees."
    else:
        text = f"{when}: {word}, {high} over {low}."
    return text, {"code": int(codes[index]), "word": word,
                  "high": high, "low": low, "day": day}


def fetch(day="today", lat=PHOENIX[0], lon=PHOENIX[1], opener=None, timeout=8):
    """Live forecast. Returns (text, data); (None, None) on any failure.

    Weather is never worth an exception reaching the arm loop.
    """
    import urllib.request
    opener = opener or (lambda u, t: urllib.request.urlopen(u, timeout=t).read())
    try:
        raw = opener(url(lat, lon), timeout)
        return describe(raw, day=day)
    except Exception:
        return None, None
