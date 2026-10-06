"""The desk lamp, over python-kasa. BUILD_SPEC 9.

Runs in a mock until Shaurya is home with the plug. The reason that is
acceptable is that the interesting part is not the protocol -- it is what
happens when the plug does not answer, which is the normal case for a smart
plug on a home wifi and the thing a mock tests better than the real device.

NO STATE IS CACHED OPTIMISTICALLY. A lamp this believes is on while it is
actually off makes "toggle" flip the wrong way, and the robot then argues with
the room. Every call reads the plug, so a toggle is always relative to what is
really true, and a read failure is reported rather than papered over with a
remembered value.
"""
import os

ON = "on"
OFF = "off"
TOGGLE = "toggle"


class Lamp:
    def __init__(self, host=None, transport=None, credentials=None):
        self.host = host or os.environ.get("KASA_HOST")
        # Injected: (action, host, credentials) -> bool|None for the new state.
        self.transport = transport or self._real
        self.credentials = credentials or (
            os.environ.get("KASA_USERNAME"), os.environ.get("KASA_PASSWORD"))

    def _real(self, action, host, credentials):
        from kasa import Credentials, Discover
        import asyncio

        async def go():
            user, password = credentials
            creds = Credentials(user, password) if user else None
            device = await Discover.discover_single(host, credentials=creds)
            await device.update()
            if action == ON:
                await device.turn_on()
            elif action == OFF:
                await device.turn_off()
            elif action == TOGGLE:
                await (device.turn_off() if device.is_on else device.turn_on())
            await device.update()
            return bool(device.is_on)

        return asyncio.run(go())

    def set(self, action):
        """Returns (text, data). Never raises."""
        action = (action or "").strip().lower()
        if action not in (ON, OFF, TOGGLE):
            return f"{action!r} is not on, off or toggle.", None
        if not self.host:
            return "No lamp configured.", {"state": None}
        try:
            state = self.transport(action, self.host, self.credentials)
        except Exception as exc:
            # Named plainly. "The lamp didn't answer" is a thing he can act on
            # (check the plug); a stack trace in the log is not, and the
            # speech policy will only let this be said once anyway.
            return "The lamp didn't answer.", {"state": None,
                                               "error": repr(exc)}
        if state is None:
            return "The lamp didn't answer.", {"state": None}
        return ("Lamp on." if state else "Lamp off."), {"state": bool(state)}


def make(lamp):
    return {"lamp": lambda action: lamp.set(action)}
