"""Settings for the desk assistant's observation logger.

Starting values. Tune them after a few days of real logging.
"""

# Camera. Same index and resolution as avi/config.py.
CAMERA_INDEX = 1
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
LOOP_FPS = 5                 # logging only needs a few frames per second

# When to look closer.
HEARTBEAT_S = 60             # observe at least this often while you're at the desk
AWAY_HEARTBEAT_S = 300       # slower heartbeat while nobody is at the desk
CHANGE_COOLDOWN_S = 10       # minimum gap between change-triggered observations

# OpenCV change detector (runs on every loop frame, on a 160x90 grayscale copy).
CHANGE_SIZE = (160, 90)
CHANGE_PIXEL_DIFF = 25       # a pixel counts as changed above this gray-level difference
CHANGE_FRACTION = 0.03       # share of pixels that must differ from the reference to count as a change
SETTLE_FRACTION = 0.01       # frame-to-frame motion below this counts as "still"
SETTLE_S = 1.5               # the scene must stay still this long before observing
FORCE_AFTER_S = 8.0          # observe anyway if motion never settles (typing, fidgeting)

# Activity labels: (stored label, wording shown to the VLM).
# The order is shuffled on every call, so position bias can't pick the answer.
# These must not presume a desk is in shot. The old list had
# "nobody is sitting at the desk" as the only fallback, so any view without a
# visible desk scored it 0.82-0.97 even with a person plainly in frame
# (YOLO person_conf 0.784). "away" now means no person at all.
#
# Exactly ONE catch-all for "present but unrecognised". A first attempt had both
# "idle" ("a person is present but not doing any of these") and "other"
# ("something else"): the model could not separate two synonyms, split its mass
# between them, and three observations in a row landed under ACTIVITY_MIN_P and
# were stored as uncertain (p = 0.22, 0.51, 0.57). Keep the options mutually
# exclusive in plain English, and keep the list short - every extra option makes
# the 0.6 floor harder to clear.
ACTIVITIES = [
    ("laptop", "using a laptop or computer"),
    ("writing on paper", "writing on paper"),
    ("reading", "reading a book or papers"),
    ("phone", "using a phone"),
    ("eating or drinking", "eating or drinking"),
    ("guitar", "playing guitar"),
    ("other", "a person is there, doing something else"),
    ("away", "there is no person in the picture"),
]
NOBODY = "away"
UNCERTAIN = "uncertain"
ACTIVITY_MIN_P = 0.6         # below this the observation is stored as "uncertain"
MC_PASSES = 1                # 2 = ask with two different orders and average (costs ~0.3 s more)
ACTIVITY_QUESTION = ("Look at this picture. Which one best describes what the "
                     "person is doing right now?")

# Objects to remember. "water bottle" and "bottle" are left out on purpose:
# YOLO-World's confidences were inverted for them on your held-out data
# (see UNRELIABLE_CLASSES in avi/yolo_tracker.py).
DESK_OBJECTS = [
    "laptop", "cell phone", "notebook", "book", "calculator", "pen",
    "headphones", "keys", "mug", "cup", "guitar", "backpack", "glasses",
    "wallet",
]
DETECTOR_MODEL = "yolov8s-worldv2.pt"
DETECTOR_CONF = 0.20         # candidates below this are ignored
MAX_VERIFY = 5               # VLM checks per observation, highest detector score first
VERIFY_PAD = 0.25            # crop padding on each side, as in the paper
VERIFY_PROMPT = "Answer yes or no only. Is the main object in this image a {label}?"

# VLM. Same model and settings as make_fast_verifier() in avi/run_study.py.
VLM_MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
VLM_MAX_PIXELS = 100352

# Storage. Only text and numbers go in the database. Frames are never saved,
# except audit thumbnails when you turn on --audit-rate.
DB_PATH = "desk_memory.db"
AUDIT_DIR = "audit"
AUDIT_RATE = 0.0             # e.g. 0.05 saves a thumbnail for 5% of observations
AUDIT_WIDTH = 320
PAUSE_FILE = "PAUSE"         # if this file exists, the logger stops looking and logging

# Sessions.
SESSION_MAX_GAP_S = 150      # same-label observations closer than this merge into one session
SESSION_MIN_MINUTES = 2      # shorter sessions are dropped from the timeline
