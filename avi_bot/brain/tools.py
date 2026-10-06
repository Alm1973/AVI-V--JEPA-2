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
    _tool("look_at", "Turn to look at something.",
          {"target": {"type": "string",
                      "enum": ["me", "desk", "door", "window", "screen"]}},
          ["target"]),
    _tool("play_emotion", "Express an emotion with motion and a chirp.",
          {"name": {"type": "string"}}, ["name"]),
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

BY_NAME = {t["function"]["name"]: t for t in TOOLS}
NAMES = tuple(BY_NAME)

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
