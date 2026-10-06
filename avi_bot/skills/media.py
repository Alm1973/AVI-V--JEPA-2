"""Media and volume, via Windows virtual key codes. BUILD_SPEC 9.

The codes are sent with ctypes' keybd_event, which needs an INTERACTIVE
session -- the same constraint that already shapes this whole project, since
scheduled tasks on Windows run in session 0 where a key press reaches nothing.
That is why `send` is injected: the mapping and the refusals are testable
anywhere, and only the final keystroke needs the real desktop.

Volume steps are deliberately SMALL and repeated rather than one large jump.
Windows moves volume by about 2% per key press, so "turn it down" meaning one
press is almost imperceptible and meaning twenty is a shock; five is a change
you notice without flinching.
"""

# Virtual key codes, from BUILD_SPEC 9.
KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
}
# "stop" has no dedicated media key in that set; pause is what people mean.
ALIASES = {"stop": "play_pause", "pause": "play_pause", "play": "play_pause",
           "resume": "play_pause", "skip": "next", "back": "previous"}

REPEATS = {"volume_up": 5, "volume_down": 5}


class Media:
    def __init__(self, send=None):
        # Injected: (vk_code) -> None.
        self.send = send or self._real
        self.sent = []

    @staticmethod
    def _real(vk):
        import ctypes
        user32 = ctypes.windll.user32            # Windows only, by design
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def do(self, action):
        """Returns (text, data). Never raises."""
        name = ALIASES.get((action or "").strip().lower(),
                           (action or "").strip().lower())
        vk = KEYS.get(name)
        if vk is None:
            return f"{action!r} is not a media action.", None
        presses = REPEATS.get(name, 1)
        try:
            for _ in range(presses):
                self.send(vk)
                self.sent.append(vk)
        except Exception as exc:
            return "Couldn't reach the keyboard.", {"error": repr(exc)}
        # Media actions are plainly visible and audible, so they get a chirp
        # rather than words -- the speech policy would drop a sentence here
        # anyway. Returning None keeps that explicit.
        return None, {"action": name, "vk": vk, "presses": presses}


def make(media):
    return {"media": lambda action: media.do(action)}
