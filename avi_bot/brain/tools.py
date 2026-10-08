"""Skill schemas, shared by the local router and the LLM. BUILD_SPEC 9.

ONE definition per skill, used twice. The local rules in intents.py emit the
same tool name and the same argument names the LLM is told to emit, so the
executor downstream never has to care which route produced a call. When those
two drift apart you get a skill that works when you phrase it one way and
fails when you phrase it another, which is indistinguishable from a flaky
robot.

`open_app` is a whitelist and `lamp` touches real hardware, so both are
marked `confirm` -- the executor is expected to refuse anything not in
config/apps.toml rather than trusting the model's path.
"""

# Mirrored from schema.EMOTIONS rather than imported, to keep this module free
# of a dependency on pydantic for the sake of one list.
EMOTION_NAMES = ("neutral", "happy", "excited", "curious", "sad", "sulky",
                 "confused", "alert", "sleepy", "affectionate", "proud")


# The arm's own gesture library (motion/gestures.py), mirrored for the same
# reason as EMOTION_NAMES. A name outside it failed silently on the arm.
ARM_GESTURES = ("nod", "shake", "perk_up", "droop", "celebrate", "ahem",
                "yawn_wake", "thinking", "embarrassed", "curious_lean",
                "excited_wiggle", "startled", "doze")
JOINT_NAMES = ("base_yaw", "shoulder", "elbow", "wrist")


def _tool(name, description, properties=None, required=None, **extra):
    schema = {"type": "object", "properties": properties or {},
              "required": required or [], "additionalProperties": False}
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": schema},
            **extra}


SECONDS = {"type": "integer", "minimum": 1, "maximum": 86400,
           "description": "duration in seconds"}
MINUTES = {"type": "integer", "minimum": 1, "maximum": 1440,
           "description": "duration in minutes"}

TOOLS = [
    _tool("set_timer", "Start a timer. Survives a restart.",
          {"seconds": SECONDS, "label": {"type": "string", "maxLength": 60}},
          ["seconds"]),
    _tool("cancel_timer", "Cancel a timer, or all of them if no id is given.",
          {"id": {"type": "integer"}}),
    _tool("list_timers", "What timers are running."),
    _tool("set_reminder", "Remind him of something at a time.",
          {"when": {"type": "string",
                    "description": "natural time, e.g. 'at 5', 'in 20 min'"},
           "text": {"type": "string", "maxLength": 200}},
          ["when", "text"]),
    _tool("get_schedule", "Read the calendar.",
          {"day": {"type": "string",
                   "enum": ["today", "tomorrow", "this week"]}}),
    _tool("focus_start", "Start quiet focus mode.", {"minutes": MINUTES}),
    _tool("focus_stop", "End focus mode and summarise it."),
    _tool("focus_status", "How the current focus session is going."),
    _tool("find_object", "Where something was last seen on the desk.",
          {"name": {"type": "string", "maxLength": 40}}, ["name"]),
    _tool("lamp", "Control the desk lamp.",
          {"action": {"type": "string", "enum": ["on", "off", "toggle"]}},
          ["action"], confirm=False),
    _tool("media", "Media and volume keys.",
          {"action": {"type": "string",
                      "enum": ["play_pause", "stop", "next", "previous",
                               "volume_up", "volume_down", "mute"]}},
          ["action"]),
    _tool("open_app", "Open an app from the whitelist in config/apps.toml. "
                      "Anything not listed is refused.",
          {"name": {"type": "string", "maxLength": 40}}, ["name"],
          confirm=True),
    _tool("weather", "Weather for Phoenix.",
          {"day": {"type": "string", "enum": ["today", "tomorrow"]}}),
    _tool("remember", "Store a fact about him. At most 280 characters.",
          {"text": {"type": "string", "maxLength": 280}}, ["text"]),
    _tool("forget", "Drop a stored fact.",
          {"text": {"type": "string", "maxLength": 280}}, ["text"]),
    _tool("what_do_you_know", "List stored facts, optionally matching a query.",
          {"query": {"type": "string", "maxLength": 80}}),
    _tool("look_at", "Turn the camera to him ('me') or to a named object "
                     "AVI has seen (it searches the desk if unknown).",
          {"target": {"type": "string", "maxLength": 40}}, ["target"]),
    _tool("play_emotion", "Play one of the arm's built-in gestures.",
          {"name": {"type": "string", "enum": list(ARM_GESTURES)}},
          ["name"]),
    # Free motion: what lets the model do something it was not given a
    # gesture for ("wave hi", "look a bit left") instead of nothing.
    _tool("nudge", "Turn the camera a little: left/right turns the base, "
                   "up/down tilts the wrist.",
          {"direction": {"type": "string",
                         "enum": ["left", "right", "up", "down"]},
           "degrees": {"type": "integer", "minimum": 2, "maximum": 45}},
          ["direction"]),
    _tool("move_joints", "Set joints to absolute degrees (clamped to their "
                         "limits). Omit a joint to leave it where it is.",
          {j: {"type": "number", "minimum": 0, "maximum": 180}
           for j in JOINT_NAMES}),
    _tool("motion_sequence", "Improvise a movement: 2-10 poses played in "
                             "order, each held hold_s seconds, then back to "
                             "where it started. E.g. a wave: wrist up/down "
                             "a few times.",
          {"steps": {"type": "array", "minItems": 1, "maxItems": 10,
                     "items": {"type": "object", "properties": {
                         **{j: {"type": "number", "minimum": 0,
                                "maximum": 180} for j in JOINT_NAMES},
                         "hold_s": {"type": "number", "minimum": 0.1,
                                    "maximum": 3.0}}}}},
          ["steps"]),
    _tool("go_to_pose", "Move to a taught pose by name (listed in the "
                        "context).",
          {"name": {"type": "string", "maxLength": 40}}, ["name"]),
    _tool("robot_status", "Mood, uptime, VRAM, provider health, request counts."),
    _tool("get_time", "The current time."),
    _tool("sleep", "Go to the rest pose and pause the observer."),
    _tool("wake", "Wake up from sleep."),
    _tool("quiet", "Nonverbal only for a while.", {"minutes": MINUTES}),
    _tool("guest_mode", "Stop logging while someone else is here."),
    _tool("stop", "Stop moving right now."),
    _tool("praise", "He was pleased."),
    _tool("scold", "He was not pleased."),
]

# THE REPLY IS A TOOL, because that is what the model does anyway.
#
# gpt-oss-20b on Groq emits the final JSON as a tool call named "json", and
# Groq rejects the whole request with a 400 -- "attempted to call tool 'json'
# which was not in request.tools" -- so the turn is lost even though the
# arguments were a perfectly valid Reply. Measured 2026-10-06 against the live
# API; two of four open questions failed this way and nothing in the local
# tests could have caught it.
#
# Declaring it makes the model's natural behaviour legal instead of fatal. It
# is kept OUT of TOOLS/BY_NAME so no skill can ever be registered for it and
# the registry's "every tool has a handler" check stays meaningful: this is an
# envelope, not an action.
REPLY_TOOL_NAMES = ("json", "reply", "final_reply")

REPLY_TOOL = _tool(
    "json",
    "Your final reply. Call this LAST, after any action tools, exactly once.",
    {"say": {"type": ["string", "null"],
             "description": "at most 20 words, or null for no speech"},
     "beep": {"type": ["string", "null"], "enum": list(EMOTION_NAMES) + [None]},
     "gesture": {"type": ["string", "null"]}})

BY_NAME = {t["function"]["name"]: t for t in TOOLS}
NAMES = tuple(BY_NAME)

# What gets SENT to a provider: the skills plus the reply envelope.
WIRE_TOOLS = TOOLS + [REPLY_TOOL]

# Simple intents the local VLM fallback can choose between with a
# multiple-choice, first-token call (BUILD_SPEC 8.1 step 3). Deliberately
# short: this path exists for when the cloud is gone, not to replace it, and
# a long list makes a 3B model's single-token pick unreliable.
LOCAL_CHOICES = ("set_timer", "focus_start", "focus_stop", "lamp", "media",
                 "find_object", "get_time", "get_schedule", "robot_status",
                 "sleep", "wake", "quiet", "stop")


def validate_call(name, args):
    """Check a tool call against its schema. Returns (ok_args, error).

    Hand-rolled rather than jsonschema: the shapes used here are small and
    adding a dependency to the one module that guards what a model is allowed
    to make the robot DO is a poor trade.
    """
    tool = BY_NAME.get(name)
    if tool is None:
        return None, f"unknown tool {name!r}"
    spec = tool["function"]["parameters"]
    props, required = spec["properties"], spec["required"]
    args = dict(args or {})
    for key in required:
        if key not in args:
            return None, f"{name}: missing {key!r}"
    out = {}
    for key, value in args.items():
        if key not in props:
            return None, f"{name}: unexpected {key!r}"
        rule = props[key]
        kind = rule.get("type")
        if kind == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return None, f"{name}.{key}: {value!r} is not an integer"
            if "minimum" in rule and value < rule["minimum"]:
                return None, f"{name}.{key}: {value} below {rule['minimum']}"
            if "maximum" in rule and value > rule["maximum"]:
                return None, f"{name}.{key}: {value} above {rule['maximum']}"
        elif kind == "string":
            if not isinstance(value, str):
                return None, f"{name}.{key}: {value!r} is not a string"
            if "maxLength" in rule and len(value) > rule["maxLength"]:
                return None, f"{name}.{key}: longer than {rule['maxLength']}"
            if "enum" in rule and value not in rule["enum"]:
                return None, (f"{name}.{key}: {value!r} not in "
                              f"{rule['enum']}")
        out[key] = value
    return out, None
