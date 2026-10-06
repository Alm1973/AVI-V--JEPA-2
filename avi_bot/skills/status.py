"""robot_status: mood, uptime, VRAM, provider health, today's counts.

BUILD_SPEC 9 lists no test for this one, which is a hint about what it is for
-- it exists so a human can ask the robot what is wrong with it. So it must
work when things ARE wrong: every source is optional and a missing one becomes
a dash rather than an exception, because the moment this is most needed is the
moment something is already broken.
"""
import time


def make(mood=None, budget=None, started_at=None, vram=None, clock=time.time):
    started_at = started_at or clock()

    def _uptime():
        seconds = int(clock() - started_at)
        if seconds < 90:
            return f"{seconds}s"
        if seconds < 5400:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"

    def _vram():
        if vram is not None:
            try:
                return vram()
            except Exception:
                return None
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            free, total = torch.cuda.mem_get_info()
            return {"free_GB": round(free / 1e9, 2),
                    "total_GB": round(total / 1e9, 2)}
        except Exception:
            return None

    def robot_status():
        data = {"uptime": _uptime()}
        try:
            data["mood"] = mood.emotion() if mood else None
        except Exception:
            data["mood"] = None
        data["vram"] = _vram()
        try:
            data["providers"] = budget.report() if budget else None
        except Exception:
            data["providers"] = None

        bits = [f"Up {data['uptime']}"]
        if data["mood"]:
            bits.append(str(data["mood"]))
        if data["vram"]:
            bits.append(f"{data['vram']['free_GB']:.1f}GB free")
        if data["providers"]:
            total = sum(p["requests"] for p in data["providers"].values())
            fails = sum(p["failures"] for p in data["providers"].values())
            bits.append(f"{total} calls" + (f", {fails} failed" if fails else ""))
        return ". ".join(bits) + ".", data

    return {"robot_status": robot_status}
