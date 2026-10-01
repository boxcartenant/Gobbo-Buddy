import os
import json
import queue
import shutil
import threading
import logging
import tkinter as tk
from tkinter import simpledialog, filedialog, messagebox, colorchooser, ttk
import time
import random
import requests
from pathlib import Path
from PIL import Image, ImageTk
import webview
import sys

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("GobboBuddy")

# ============================================================
# PATHS
# ============================================================
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

SETTINGS_PATH = SCRIPT_DIR / "gobbo_buddy_config.json"
PROFILES_DIR = SCRIPT_DIR / "profiles"
try:
    PROFILES_DIR.mkdir(exist_ok=True)
except OSError as e:
    logger.warning(f"Could not create profiles directory {PROFILES_DIR}: {e}")

# ============================================================
# HARD-CODED FALLBACK DEFAULTS
# ------------------------------------------------------------
# These are the literal "no profile matches" appearance: gray box, no
# sprite, default prompts. They are intentionally NOT part of the editable
# settings file -- they're the safety net under everything else, and they
# also seed the starting values of the Profile Editor when creating a
# brand-new profile.
# ============================================================
DEFAULT_SPRITE_WIDTH = 120
DEFAULT_SPRITE_HEIGHT = 120
DEFAULT_ROWS = 3
DEFAULT_COLS = 4
DEFAULT_BUBBLE_OFFSET_X = 0
DEFAULT_BUBBLE_OFFSET_Y = 0

DEFAULT_EMOTIONS = {
    "neutral":     [0, 0],
    "happy":       [0, 1],
    "joking":      [0, 1],
    "curious":     [0, 2],
    "snarky":      [0, 3],
    "excited":     [1, 0],
    "skeptical":   [1, 1],
    "judging":     [1, 2],
    "shocked":     [1, 3],
    "sad":         [2, 0],
    "anxious":     [2, 1],
    "embarrassed": [2, 2],
    "flattered":   [2, 3],
}

BUDDY_NAME = ""

DEFAULT_PROACTIVE_PROMPTS = [
    "What do you think about this?",
    "Does anything here stand out to you?",
    "What does this remind you of?",
    "Is there anything interesting happening here?",
    "Does anything about this seem unusual?",
    "What would you pay attention to here?",
    "Is there anything here worth looking into?",
    "What do you make of this?",
    "Does this seem familiar to you?",
    "What would you change about this?",
    "Is there anything here that seems confusing?",
    "What question would you ask about this?",
    "What would you do next?",
    "Does this look like it's working as intended?",
    "Is there anything here that looks important?",
    "What detail might be easy to overlook?",
    "Does anything here seem out of place?",
    "What would you investigate if you had more time?",
    "Is there a simpler way to approach this?",
    "What do you think is going on here?",
    "Does this suggest anything interesting?",
    "What would you expect to happen next?",
    "Is there anything here that deserves a closer look?",
    "What part of this catches your attention?",
    "Would you approach this differently?",
    "Does anything here raise a question?",
    "What would you want to know about this?",
    "Is there anything here that seems worth remembering?",
    "What connection do you see here?",
    "What is the most interesting thing about this?",
    "Does anything here look particularly useful?",
    "What would you check first?",
    "Could there be something being overlooked here?",
    "What would you point out to someone seeing this for the first time?"
]

# {app_name}, {what}, {summary}, {prompt} are substituted
DEFAULT_PROACTIVE_TEMPLATE = (
    "I'm using '{app_name}', looking at '{what}'. "
    "The page shows things like ['{summary}']. \n\n{prompt}"
)

# ============================================================
# Optional Accessibility Stack
# ============================================================
try:
    try:
        import comtypes.client as _comtypes_client
        COMTYPES_CACHE_DIR = SCRIPT_DIR / "comtypes_cache"
        COMTYPES_CACHE_DIR.mkdir(exist_ok=True)

        _comtypes_client.gen_dir = str(COMTYPES_CACHE_DIR)
    except OSError as _cache_err:
        # Worst case, fall back to in-memory generation (slower, but works
        # without ever touching disk).
        logging.getLogger("GobboBuddy").warning(
            f"Could not create comtypes cache dir, generating in-memory: {_cache_err}"
        )
        _comtypes_client.gen_dir = None

    import uiautomation as auto
    import win32gui
    import win32process
    import psutil
    HAS_ACCESSIBILITY = True
except ImportError:
    HAS_ACCESSIBILITY = False

# ============================================================
# GLOBAL SETTINGS  (anything that does NOT make sense to customize
# per-character lives here; per-character stuff lives in profiles/*.json)
# ------------------------------------------------------------
# SETTINGS_SCHEMA is the single source of truth: it drives (a) the defaults
# written into a fresh settings file, (b) forward-compatible loading of an
# existing file, and (c) the fields shown in the live Settings window.
# ============================================================
SETTINGS_SCHEMA = [
    {"key": "gobbonet_base_url", "type": "str",
     "comment": "Base URL of the local GobboNet server."},
    {"key": "llm_direct_base", "type": "str",
     "comment": "Base URL of the direct local LLM server used for background summarizing/classifying."},
    {"key": "llm_direct_timeout", "type": "float",
     "comment": "Seconds to wait for the direct LLM before giving up."},
    {"key": "generation_timeout", "type": "float",
     "comment": "Seconds to wait for a GobboNet chat reply before giving up."},
    {"key": "page_ready_timeout", "type": "float",
     "comment": "Seconds to wait for the GobboNet page/state to become ready at startup."},
    {"key": "max_bubble_lines", "type": "int",
     "comment": "Maximum number of lines the speech bubble grows to before it scrolls."},
    {"key": "max_raw_content_chars", "type": "int",
     "comment": "Truncates the raw accessibility text before sending it out. Set to 0 to disable truncation."},
    {"key": "max_summary_chars", "type": "int",
     "comment": "Truncates the returned screen-summary text to this many characters."},
    {"key": "summary_max_tokens", "type": "int",
     "comment": "Token limit for the background screen-summary generation call."},
    {"key": "summary_temperature", "type": "float",
     "comment": "Sampling temperature for the background screen-summary generation call."},
    {"key": "classifier_max_tokens", "type": "int",
     "comment": "Token limit for the emotion classifier call."},
    {"key": "classifier_temperature", "type": "float",
     "comment": "Sampling temperature for the emotion classifier call."},
    {"key": "classifier_input_chars", "type": "int",
     "comment": "Truncates the message text before it's handed to the emotion classifier. Set to 0 to disable truncation."},
    {"key": "proactive_check_period_min", "type": "float",
     "comment": "Default minimum seconds between proactive opportunities (used by any profile that doesn't set its own rate)."},
    {"key": "proactive_check_period_max", "type": "float",
     "comment": "Default maximum seconds between proactive opportunities."},
    {"key": "proactive_probability", "type": "float",
     "comment": "Default chance (0-1) that a proactive opportunity actually triggers. At each opportunity, if this roll fails, nothing happens and it just reschedules."},
    {"key": "proactive_enabled", "type": "bool",
     "comment": "Master switch for proactive screen-watching. Same as the right-click 'Enable Proactive' checkbox."},
    {"key": "auto_switch_profile_on_select", "type": "bool",
     "comment": "When on, picking a character from the Character menu (or GobboNet switching characters on its own) automatically loads that character's profile. When off, only 'Load Profile' switches profiles manually -- except the very first character detected after launch, which always tries to auto-load its profile. Same as the right-click 'Auto-switch Profile on Character Select' checkbox."},
    {"key": "default_emotion", "type": "str",
     "comment": "Emotion tag used as a fallback whenever a requested/classified emotion isn't recognized."},
    {"key": "fallback_transparent_color", "type": "color",
     "comment": "Window color-key used ONLY when no sprite is loaded, so the gray placeholder box stays visible. When a real sprite sheet is loaded, its own top-left pixel is sampled and used instead."},
]

SETTINGS_GLOBAL_NAMES = {
    "gobbonet_base_url": "GOBBONET_BASE_URL",
    "llm_direct_base": "LLM_DIRECT_BASE",
    "llm_direct_timeout": "LLM_DIRECT_TIMEOUT",
    "generation_timeout": "GENERATION_TIMEOUT",
    "page_ready_timeout": "PAGE_READY_TIMEOUT",
    "max_bubble_lines": "MAX_BUBBLE_LINES",
    "max_raw_content_chars": "MAX_RAW_CONTENT_CHARS",
    "max_summary_chars": "MAX_SUMMARY_CHARS",
    "summary_max_tokens": "SUMMARY_MAX_TOKENS",
    "summary_temperature": "SUMMARY_TEMPERATURE",
    "classifier_max_tokens": "CLASSIFIER_MAX_TOKENS",
    "classifier_temperature": "CLASSIFIER_TEMPERATURE",
    "classifier_input_chars": "CLASSIFIER_INPUT_CHARS",
    "proactive_check_period_min": "RANDOM_WINDOW_MIN_SECONDS",
    "proactive_check_period_max": "RANDOM_WINDOW_MAX_SECONDS",
    "proactive_probability": "RANDOM_WINDOW_PROBABILITY",
    "proactive_enabled": "PROACTIVE_ENABLED",
    "auto_switch_profile_on_select": "AUTO_SWITCH_PROFILE_ON_SELECT",
    "default_emotion": "DEFAULT_EMOTION",
    "fallback_transparent_color": "FALLBACK_TRANSPARENT_COLOR",
}

SETTINGS_DEFAULTS = {
    "gobbonet_base_url": "http://127.0.0.1:9066",
    "llm_direct_base": "http://127.0.0.1:11437",
    "llm_direct_timeout": 45,
    "generation_timeout": 600,
    "page_ready_timeout": 60,
    "max_bubble_lines": 30,
    "max_raw_content_chars": 1800,
    "max_summary_chars": 180,
    "summary_max_tokens": 150,
    "summary_temperature": 0.3,
    "classifier_max_tokens": 12,
    "classifier_temperature": 0.1,
    "classifier_input_chars": 1200,
    "proactive_check_period_min": 45,
    "proactive_check_period_max": 180,
    "proactive_probability": 0.35,
    "proactive_enabled": True,
    "auto_switch_profile_on_select": True,
    "default_emotion": "neutral",
    "fallback_transparent_color": "#15181D",
}

# Keys that used to live in this file before profiles existed. If we see
# them, we leave them alone on disk (in case the user wants to copy the
# values into a profile by hand) but we never read them anymore.
_LEGACY_SETTINGS_KEYS = {
    "sprite_sheet", "sprite_width", "sprite_height", "rows", "cols",
    "emotions", "proactive_prompts", "proactive_template",
}


def _settings_comment_key(key):
    return key.upper() + "_COMMENT"


def default_settings_dict():
    cfg = {}
    for field in SETTINGS_SCHEMA:
        cfg[_settings_comment_key(field["key"])] = field["comment"]
        cfg[field["key"]] = SETTINGS_DEFAULTS[field["key"]]
    return cfg


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            changed = False
            for field in SETTINGS_SCHEMA:
                key = field["key"]
                if key not in cfg:
                    cfg[key] = SETTINGS_DEFAULTS[key]
                    changed = True
                cfg.setdefault(_settings_comment_key(key), field["comment"])

            found_legacy = _LEGACY_SETTINGS_KEYS.intersection(cfg.keys())
            if found_legacy:
                logger.warning(
                    "Settings file contains legacy per-character keys that are now "
                    f"handled by profiles and will be ignored: {sorted(found_legacy)}. "
                    "Use 'Edit / Create Profile...' to recreate that character as a profile."
                )
            if changed:
                save_settings(cfg)
            return cfg
        except Exception as e:
            logger.warning(f"Could not read settings, using defaults: {e}")
    else:
        logger.warning("NO SETTINGS FILE FOUND. One will be created with defaults.")

    cfg = default_settings_dict()
    save_settings(cfg)
    logger.info(f"Created default settings file at {SETTINGS_PATH}")
    return cfg


def save_settings(cfg):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not save settings: {e}")


def apply_settings_to_globals(cfg):
    """Push every settings value into its live module-level global."""
    g = globals()
    for field in SETTINGS_SCHEMA:
        key = field["key"]
        gname = SETTINGS_GLOBAL_NAMES[key]
        g[gname] = cfg.get(key, SETTINGS_DEFAULTS[key])


SETTINGS = load_settings()
apply_settings_to_globals(SETTINGS)

logger.info(f"Settings file: {SETTINGS_PATH}")
logger.info(f"Profiles directory: {PROFILES_DIR}")

# ============================================================
# PASSWORD
# ============================================================
def get_password():
    temp_root = tk.Tk()
    temp_root.withdraw()
    password = simpledialog.askstring("Gobbonet Auth", "Enter Gobbonet password:", show="*")
    temp_root.destroy()
    return password or ""

GOBBONET_PASSWORD = get_password()

# ============================================================
# EMOTION MAP HELPERS
# ------------------------------------------------------------
# An emotion map is normalized at runtime to dict[tag] -> [(row, col), ...].
# Multiple coordinates per tag are fully supported: whenever a tag is
# registered more than once (as repeated rows in a profile's "emotions"
# list, or as multiple pairs under one dict key), every coordinate is kept,
# and one is chosen at random each time that emotion is shown.
# ============================================================
def normalize_emotion_map(raw):
    """Accepts several authoring shapes and returns dict[tag] -> [(row, col), ...].

    Supported input shapes:
      - dict:  {"happy": [0, 1]}                     (single coordinate)
      - dict:  {"happy": [[0, 1], [1, 3]]}            (multiple coordinates)
      - list:  [["happy", 0, 1], ["happy", 1, 3]]     (flat rows; a tag may repeat)
      - list:  [{"tag": "happy", "row": 0, "col": 1}, ...]
    """
    result = {}

    def _add(tag, r, c):
        try:
            tag = str(tag).strip().lower()
            coord = (int(r), int(c))
        except (TypeError, ValueError):
            return
        if not tag:
            return
        result.setdefault(tag, [])
        if coord not in result[tag]:
            result[tag].append(coord)

    if isinstance(raw, dict):
        for tag, val in raw.items():
            if (isinstance(val, (list, tuple)) and len(val) == 2
                    and all(isinstance(x, (int, float)) for x in val)):
                _add(tag, val[0], val[1])
            elif isinstance(val, (list, tuple)):
                for pair in val:
                    if isinstance(pair, (list, tuple)) and len(pair) == 2:
                        _add(tag, pair[0], pair[1])
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 3:
                _add(entry[0], entry[1], entry[2])
            elif isinstance(entry, dict):
                _add(entry.get("tag"), entry.get("row"), entry.get("col"))

    return result


def emotion_map_to_rows(emotion_map):
    """Flattens a normalized {tag: [(r,c),...]} map into editable [tag, r, c] rows."""
    rows = []
    for tag, coords in emotion_map.items():
        for (r, c) in coords:
            rows.append([tag, r, c])
    return rows


def build_emotion_tag_list(emotion_map) -> str:
    """String injected into the classifier prompt/grammar. Coordinates aren't shown
    here since a single tag may now map to several sprite coordinates."""
    #Smaller models depend on the presence of an ordered list in the emotion classifier prompt
    # and they are heavily influenced by the ordering of the emotions.
    # Randomizing the list aims to reduce the influence of order-bias on the total experience.
    emotions = [tag for tag in emotion_map]
    shuffled_list = random.sample(emotions,len(emotions))
    return "\n".join(f"- {tag.upper()}" for tag in shuffled_list)


def sample_transparent_color(image):
    """Samples an image's top-left pixel and returns it as a '#rrggbb' string,
    to be used as this sprite sheet's chroma-key transparent color."""
    try:
        r, g, b = image.convert("RGB").getpixel((0, 0))
        return "#{:02x}{:02x}{:02x}".format(r, g, b)
    except Exception as e:
        logger.warning(f"Could not sample transparent color, using fallback: {e}")
        return FALLBACK_TRANSPARENT_COLOR

# ============================================================
# PROFILES
# ------------------------------------------------------------
# A profile is a pair of files in PROFILES_DIR: "<stem>.json" (settings)
# and, optionally, "<stem>.png" (sprite sheet). A profile's json declares
# "character_name"; that name is matched (case-insensitively) against the
# currently active GobboNet character card to decide whether to auto-load
# it. If no profile matches, the hard-coded defaults (gray box, no sprite,
# default prompts) are used instead.
# ============================================================
PROFILE_COMMENTS = {
    "character_name": "Must match a GobboNet character card's name (case-insensitive) for this profile to auto-load.",
    "sprite_sheet": "Filename of this profile's sprite sheet PNG, expected alongside this json in the profiles folder.",
    "sprite_width": "Final on-screen sprite width in pixels, after scaling.",
    "sprite_height": "Final on-screen sprite height in pixels, after scaling.",
    "rows": "Number of rows in the sprite sheet grid.",
    "cols": "Number of columns in the sprite sheet grid.",
    "bubble_offset_x": "Shifts the speech bubble (and its triangle) left/right relative to the "
                        "sprite, in pixels. 0 keeps it centered above the sprite as usual; "
                        "positive moves it right, negative moves it left. Handy for "
                        "left/right-facing sprites or ones with empty space on one side.",
    "bubble_offset_y": "Shifts the speech bubble (and its triangle) up/down relative to the "
                        "sprite, in pixels. 0 is the normal position just above the sprite; "
                        "positive moves it down (closer to/over the sprite), negative moves it "
                        "further up.",
    "emotions": "List of [tag, row, col] entries. It's fine to list the same tag more than "
                "once with different coordinates -- one is picked at random each time that "
                "emotion is shown.",
    "garden": "This character's list of proactive prompts (the 'garden'). A line is chosen "
              "at random each time.",
    "proactive_template": "Format string for accessibility data sent to the model for this "
                           "character. {app_name}, {what}, {summary}, {prompt} are substituted.",
    "proactive_check_period_min": "Minimum seconds between proactive opportunities for this "
                                   "character. Leave null to use the global default.",
    "proactive_check_period_max": "Maximum seconds between proactive opportunities for this "
                                   "character. Leave null to use the global default.",
    "proactive_probability": "Chance (0-1) that a proactive opportunity actually triggers for "
                              "this character. Leave null to use the global default.",
}

_PROFILE_KEY_ORDER = (
    "character_name", "sprite_sheet", "sprite_width", "sprite_height", "rows", "cols",
    "bubble_offset_x", "bubble_offset_y",
    "emotions", "garden", "proactive_template",
    "proactive_check_period_min", "proactive_check_period_max", "proactive_probability",
)


def _profile_comment_key(key):
    return key.upper() + "_COMMENT"


def sanitize_profile_stem(name):
    stem = "".join(ch if (ch.isalnum() or ch in " _-") else "_" for ch in str(name).strip())
    stem = stem.strip().strip("_") or "profile"
    return stem


def profile_json_path(stem):
    return PROFILES_DIR / f"{stem}.json"


def profile_png_path(stem):
    return PROFILES_DIR / f"{stem}.png"


def read_profile_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_profile_json(path, data):
    out = {}
    for key in _PROFILE_KEY_ORDER:
        if key in PROFILE_COMMENTS:
            out[_profile_comment_key(key)] = PROFILE_COMMENTS[key]
        if key in data:
            out[key] = data[key]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


def list_profiles():
    """Returns [(display_name, json_path), ...] sorted by display name."""
    results = []
    if not PROFILES_DIR.exists():
        return results
    for jf in PROFILES_DIR.glob("*.json"):
        try:
            data = read_profile_json(jf)
        except Exception as e:
            logger.warning(f"Skipping unreadable profile {jf.name}: {e}")
            continue
        display = str(data.get("character_name") or jf.stem)
        results.append((display, jf))
    results.sort(key=lambda pair: pair[0].lower())
    return results


def find_profile_for_character(char_name):
    """Returns (json_path, data) for the profile matching char_name, or None."""
    if not char_name:
        return None
    target = char_name.strip().lower()
    if not target or not PROFILES_DIR.exists():
        return None

    name_match = None
    stem_match = None
    for jf in PROFILES_DIR.glob("*.json"):
        try:
            data = read_profile_json(jf)
        except Exception as e:
            logger.warning(f"Skipping unreadable profile {jf.name}: {e}")
            continue
        declared = str(data.get("character_name", "")).strip().lower()
        if declared and declared == target and name_match is None:
            name_match = (jf, data)
        elif jf.stem.strip().lower() == target and stem_match is None:
            stem_match = (jf, data)

    return name_match or stem_match


def default_profile_data(character_name=""):
    """Starting template used when creating a brand-new profile in the editor."""
    return {
        "character_name": character_name,
        "sprite_sheet": "",
        "sprite_width": DEFAULT_SPRITE_WIDTH,
        "sprite_height": DEFAULT_SPRITE_HEIGHT,
        "rows": DEFAULT_ROWS,
        "cols": DEFAULT_COLS,
        "bubble_offset_x": DEFAULT_BUBBLE_OFFSET_X,
        "bubble_offset_y": DEFAULT_BUBBLE_OFFSET_Y,
        "emotions": emotion_map_to_rows(normalize_emotion_map(DEFAULT_EMOTIONS)),
        "garden": DEFAULT_PROACTIVE_PROMPTS.copy(),
        "proactive_template": DEFAULT_PROACTIVE_TEMPLATE,
        "proactive_check_period_min": None,
        "proactive_check_period_max": None,
        "proactive_probability": None,
    }

# ============================================================
# LIGHTWEIGHT OS WEBVIEW BRIDGE
# ============================================================
class GobboNetWebViewBridge:
    def __init__(self):
        self.window = None
        self.ui_ready_event = threading.Event()
        self.state_ready_event = threading.Event()
        self.ready_event = self.state_ready_event

        self._startup_lock = threading.Lock()
        self._startup_started = False
        self._startup_complete = False

        self.state_confirmed = False
        self.mutations_allowed = False
        self.safety_lock = False
        self.safety_lock_reason = None

        self.last_known_character_count = None
        self.last_known_thread_count = None
        self.operation_lock = threading.RLock()

        self.active_character_name = None
        self.active_character_id = None

    def close(self):
        with self.operation_lock:
            if not self.window:
                return
            try:
                self.window.destroy()
            except Exception as error:
                logger.error(f"Could not destroy GobboNet WebView: {error}")
            finally:
                self.window = None

    def start_window(self):
        self.window = webview.create_window(
            "GobboNet Engine",
            GOBBONET_BASE_URL,
            hidden=True,
            width=1024,
            height=768
        )
        self.window.events.loaded += self._on_loaded
        webview.start(private_mode=True)

    def _on_loaded(self):
        with self._startup_lock:
            if self._startup_started:
                logger.info("Ignoring duplicate GobboNet page-load event.")
                return
            self._startup_started = True

        try:
            self.eval_js("""
                window.confirm = function(msg) {
                    console.log('[GobboBuddy] Auto-accepting confirm:', msg);
                    return true;
                };
            """)
            logger.info("Installed confirm() auto-accept override.")

            self._attempt_login()
            self._wait_for_ui()
            self.ui_ready_event.set()

            logger.info("GobboNet UI is ready; waiting for application state...")
            time.sleep(1.5)
            self._nudge_server_restore()

            state = self.wait_for_gobbonet_state()
            self.validate_startup_state(state)

            self.state_confirmed = True
            self.mutations_allowed = True
            self._startup_complete = True
            self.state_ready_event.set()
            logger.info("GobboNet state verified. Mutations are now ENABLED.")

            try:
                name, char_id = self.get_active_character()
                if name:
                    self.active_character_name = name
                    self.active_character_id = char_id
                    logger.info(f"Detected currently active GobboNet character: {name!r}")
                else:
                    logger.warning("Could not determine the currently active GobboNet character.")
            except Exception as error:
                logger.warning(f"Active character detection failed (non-fatal): {error}")

        except Exception as error:
            self.state_confirmed = False
            self.mutations_allowed = False
            self._startup_complete = False
            logger.error(f"GobboNet startup safety validation failed: {error}")
            logger.error("GobboBuddy will remain READ-ONLY/LOCKED.")

    def _nudge_server_restore(self):
        with self.operation_lock:
            try:
                result = self.eval_js("""
                    (async () => {
                        try {
                            if (typeof restoreFromServer === 'function') {
                                const ok = await restoreFromServer({ silent: true, inPlace: true });
                                return { ok: !!ok, method: 'restoreFromServer' };
                            }
                            return { ok: false, reason: 'restoreFromServer not available' };
                        } catch (e) {
                            return { ok: false, reason: String(e) };
                        }
                    })()
                """)
                logger.info(f"Nudge restore result: {result}")
            except Exception as e:
                logger.warning(f"Nudge restore failed (non-fatal): {e}")

    def _attempt_login(self):
        password_json = json.dumps(GOBBONET_PASSWORD)
        self.eval_js(f"""
            (() => {{
                const pw = document.querySelector('input[name="password"]');
                if (!pw) return false;
                pw.value = {password_json};
                pw.dispatchEvent(new Event("input", {{ bubbles: true }}));
                pw.dispatchEvent(new Event("change", {{ bubbles: true }}));
                const btn = document.querySelector('button[type="submit"], input[type="submit"]');
                if (!btn) return false;
                btn.click();
                return true;
            }})()
        """)

    def _wait_for_ui(self, timeout=None):
        if timeout is None:
            timeout = PAGE_READY_TIMEOUT
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ready = self.eval_js("""
                    (() => {
                        return !!(
                            document.querySelector('textarea') ||
                            document.querySelector('input[type="text"]') ||
                            document.querySelector('[contenteditable="true"]') ||
                            document.querySelector('#chat-input, .chat-input, #message-input, .message-input')
                        );
                    })()
                """)
                if ready:
                    logger.info("GobboNet chat UI detected.")
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("GobboNet chat UI never became available.")

    def get_state_summary(self):
        return self.eval_js("""
            (() => {
                if (typeof state === "undefined" || state === null) {
                    return { ok: false, reason: "GobboNet global `state` unavailable" };
                }
                if (!Array.isArray(state.characterCards)) {
                    return { ok: false, reason: "state.characterCards is not initialized" };
                }
                if (!Array.isArray(state.threads)) {
                    return { ok: false, reason: "state.threads is not initialized" };
                }
                return {
                    ok: true,
                    characterCount: state.characterCards.length,
                    threadCount: state.threads.length,
                    activeThreadId: state.activeThreadId ?? null
                };
            })()
        """)

    def wait_for_gobbonet_state(self, timeout=None):
        if timeout is None:
            timeout = PAGE_READY_TIMEOUT
        deadline = time.time() + timeout
        last_reason = "unknown"
        while time.time() < deadline:
            try:
                result = self.get_state_summary()
                if result and result.get("ok"):
                    logger.info(
                        f"GobboNet state initialized: "
                        f"{result['characterCount']} characters, "
                        f"{result['threadCount']} threads."
                    )
                    return result
                if result:
                    last_reason = result.get("reason", "unknown")
            except Exception as error:
                last_reason = str(error)
            time.sleep(0.5)
        raise RuntimeError(f"GobboNet state never became available. Last reason: {last_reason}")

    def validate_startup_state(self, state_info):
        if not isinstance(state_info, dict):
            raise RuntimeError("GobboNet returned invalid state information.")
        character_count = state_info.get("characterCount")
        thread_count = state_info.get("threadCount")
        if not isinstance(character_count, int) or not isinstance(thread_count, int):
            raise RuntimeError("Invalid GobboNet character/thread count.")
        logger.info(f"Validated GobboNet state: {character_count} characters, {thread_count} threads.")
        self.last_known_character_count = character_count
        self.last_known_thread_count = thread_count
        if character_count == 0:
            logger.warning("GobboNet reported zero character cards at startup (may still be restoring).")

    def verify_state_still_exists(self):
        if self.safety_lock:
            return False
        try:
            state_info = self.get_state_summary()
        except Exception as error:
            self._engage_safety_lock(f"Could not read GobboNet state: {error}")
            return False
        if not state_info or not state_info.get("ok"):
            self._engage_safety_lock(
                f"GobboNet state became unavailable: "
                f"{state_info.get('reason') if state_info else 'unknown'}"
            )
            return False
        character_count = state_info["characterCount"]
        thread_count = state_info["threadCount"]
        if (
            self.last_known_character_count is not None
            and self.last_known_character_count > 0
            and character_count == 0
        ):
            self._engage_safety_lock(
                f"GobboNet character list unexpectedly changed from "
                f"{self.last_known_character_count} characters to ZERO."
            )
            return False
        self.last_known_character_count = character_count
        self.last_known_thread_count = thread_count
        return True

    def _engage_safety_lock(self, reason):
        self.safety_lock = True
        self.safety_lock_reason = reason
        self.mutations_allowed = False
        logger.error("==================================================")
        logger.error("GOBBONET SAFETY LOCK ENGAGED")
        logger.error(reason)
        logger.error("No GobboNet mutations will be attempted.")
        logger.error("==================================================")

    def assert_mutations_allowed(self):
        if self.safety_lock:
            raise RuntimeError(f"GobboNet safety lock is engaged: {self.safety_lock_reason}")
        if not self.state_confirmed:
            raise RuntimeError("GobboNet state has not been confirmed.")
        if not self.mutations_allowed:
            raise RuntimeError("GobboNet mutations are disabled.")
        if not self.verify_state_still_exists():
            raise RuntimeError("GobboNet state verification failed. Mutation blocked.")

    def eval_js(self, script):
        if not self.window:
            raise RuntimeError("GobboNet WebView does not exist.")
        return self.window.evaluate_js(script)

    def get_character_cards(self):
        result = self.eval_js("""
            (() => {
                if (typeof state === "undefined" || state === null) {
                    return { ok: false, reason: "GobboNet global `state` unavailable" };
                }
                if (!Array.isArray(state.characterCards)) {
                    return { ok: false, reason: "state.characterCards unavailable" };
                }
                return {
                    ok: true,
                    cards: state.characterCards.map(c => ({
                        id: c.id,
                        name: c.name || "Unnamed"
                    }))
                };
            })()
        """)
        if not result or not result.get("ok"):
            raise RuntimeError(
                "Could not safely read GobboNet character cards: "
                + str(result.get("reason") if result else "no result")
            )
        return result["cards"]

    def get_active_character(self):
        result = self.eval_js("""
            (() => {
                if (typeof state === "undefined" || state === null) {
                    return { ok: false, reason: "state unavailable" };
                }
                const cards = Array.isArray(state.characterCards) ? state.characterCards : [];
                if (cards.length === 0) {
                    return { ok: false, reason: "no character cards" };
                }

                const findCard = (id) => cards.find(c => c.id === id) || null;

                const directId = state.activeCardId
                if (directId) {
                    const c = findCard(directId);
                    if (c) return { ok: true, id: c.id, name: c.name || "Unnamed", method: "state id" };
                }

                return { ok: false, reason: "could not determine active character" };
            })()
        """)
        if not result or not result.get("ok"):
            logger.warning(
                "get_active_character: " + str(result.get("reason") if result else "no result")
            )
            return None, None
        logger.info(f"Active character resolved via '{result.get('method')}'.")
        return result.get("name"), result.get("id")

    def activate_character(self, card_id):
        with self.operation_lock:
            self.assert_mutations_allowed()
            cards = self.get_character_cards()
            matching = [card for card in cards if card.get("id") == card_id]
            if not matching:
                raise RuntimeError(f"Refusing to activate unknown character {card_id!r}.")
            escaped_id = json.dumps(card_id)
            result = self.eval_js(f"""
                (() => {{
                    if (typeof activateCard !== "function") {{
                        throw new Error("GobboNet activateCard() is unavailable.");
                    }}
                    activateCard({escaped_id});
                    return true;
                }})()
            """)
            if not result:
                raise RuntimeError("GobboNet failed to activate the selected character.")
            self.active_character_id = card_id
            self.active_character_name = matching[0].get("name", "Unnamed")
            return True

    def refresh_character_state(self):
        """Re-pulls character/thread state from the GobboNet server, in case
        it changed outside of this hidden window -- e.g. a card was added or
        renamed through the main GobboNet UI in another tab."""
        with self.operation_lock:
            if not self.state_ready_event.is_set():
                raise RuntimeError("GobboNet is not ready yet.")
            try:
                result = self.eval_js("""
                    (async () => {
                        try {
                            if (typeof restoreFromServer === 'function') {
                                const ok = await restoreFromServer({ silent: true, inPlace: true });
                                return { ok: !!ok };
                            }
                            return { ok: false, reason: 'restoreFromServer not available' };
                        } catch (e) {
                            return { ok: false, reason: String(e) };
                        }
                    })()
                """)
            except Exception as e:
                raise RuntimeError(f"Could not reach GobboNet: {e}")
            if not result or not result.get("ok"):
                raise RuntimeError(str(result.get("reason") if result else "unknown error"))

            state_info = self.get_state_summary()
            self.validate_startup_state(state_info)
            try:
                name, char_id = self.get_active_character()
                if name:
                    self.active_character_name = name
                    self.active_character_id = char_id
            except Exception:
                pass
            return True

    def create_new_thread(self):
        with self.operation_lock:
            self.assert_mutations_allowed()
            result = self.eval_js("""
                (() => {
                    try {
                        if (typeof createNewThread === "function") {
                            createNewThread();
                            return true;
                        }
                        if (typeof createThread === "function") {
                            createThread();
                            return true;
                        }
                        if (typeof window.createThread === "function") {
                            window.createThread();
                            return true;
                        }
                        const newBtn = document.querySelector(
                            '#new-thread-btn, .new-chat-btn, button[title*="New"]'
                        );
                        if (newBtn) {
                            newBtn.click();
                            return true;
                        }
                        return false;
                    } catch (e) {
                        console.error('[GobboBuddy] create_new_thread error:', e);
                        return false;
                    }
                })()
            """)
            if result is False:
                raise RuntimeError("GobboNet could not create a new thread.")
            return True

    def stop_generation(self):
        with self.operation_lock:
            if not self.state_ready_event.is_set():
                return False
            return self.eval_js("""
                (() => {
                    if (typeof stopGeneration === "function") {
                        stopGeneration();
                        return true;
                    }
                    const btn = document.querySelector(
                        "#stop-btn, button.stop-generation, .btn-stop"
                    );
                    if (btn) { btn.click(); return true; }
                    return false;
                })()
            """)

    def send_message(self, prompt_text):
        with self.operation_lock:
            self.assert_mutations_allowed()
            js_prompt = json.dumps(prompt_text, ensure_ascii=False)

            script = r"""
    (() => {
        window.__gobboBuddyLastAssistant = null;
        window.__gobboBuddyError = null;
        window.__gobboBuddyDone = false;

        (async () => {
            try {
                const findInput = () => {
                    return (
                        document.querySelector("textarea") ||
                        document.querySelector('input[type="text"]') ||
                        document.querySelector('[contenteditable="true"]') ||
                        document.querySelector(
                            "#chat-input, .chat-input, " +
                            "#message-input, .message-input"
                        )
                    );
                };

                const getMsgs = () => {
                    if (typeof getActiveThread === "function") {
                        const thread = getActiveThread();
                        return thread && Array.isArray(thread.messages)
                            ? thread.messages
                            : [];
                    }
                    if (typeof state !== "undefined" && state &&
                        state.activeThread && Array.isArray(state.activeThread.messages)) {
                        return state.activeThread.messages;
                    }
                    if (typeof state !== "undefined" && state &&
                        Array.isArray(state.threads) && state.activeThreadId) {
                        const thread = state.threads.find(x => x.id === state.activeThreadId);
                        if (thread && Array.isArray(thread.messages)) return thread.messages;
                    }
                    return Array.from(
                        document.querySelectorAll(".message, .chat-message, [data-role], .msg")
                    );
                };

                let input = findInput();
                let attempts = 0;
                while (!input && attempts < 20 &&
                       typeof window.sendMessage !== "function" &&
                       typeof sendMessage !== "function") {
                    await new Promise(resolve => setTimeout(resolve, 250));
                    input = findInput();
                    attempts++;
                }

                const beforeCount = getMsgs().length;

                if (typeof sendMessage === "function") {
                    sendMessage(PROMPT_HERE);
                } else if (typeof window.sendMessage === "function") {
                    window.sendMessage(PROMPT_HERE);
                } else if (input) {
                    input.focus();
                    if (input.tagName === "TEXTAREA" || input.tagName === "INPUT") {
                        const setter =
                            Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, "value"
                            )?.set ||
                            Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, "value"
                            )?.set;
                        if (setter) setter.call(input, PROMPT_HERE);
                        else input.value = PROMPT_HERE;
                    } else {
                        input.innerText = PROMPT_HERE;
                    }
                    input.dispatchEvent(new Event("input", { bubbles: true }));
                    input.dispatchEvent(new Event("change", { bubbles: true }));

                    const sendButton =
                        document.querySelector('button[type="submit"]') ||
                        document.querySelector("#send-btn") ||
                        document.querySelector(".send-button");
                    if (sendButton) {
                        sendButton.click();
                    } else {
                        input.dispatchEvent(new KeyboardEvent("keydown", {
                            key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true
                        }));
                    }
                } else {
                    throw new Error(
                        "GobboNet sendMessage() was unavailable and no chat input could be found."
                    );
                }

                const check = setInterval(() => {
                    try {
                        const generating =
                            typeof isGenerating !== "undefined"
                                ? isGenerating
                                : !!document.querySelector(
                                    ".generating, .loading, .spinner, " +
                                    'button[title*="Stop"]'
                                );
                        const msgs = getMsgs();
                        let lastText = "";
                        if (msgs.length > 0) {
                            const lastMsg = msgs[msgs.length - 1];
                            if (typeof lastMsg === "object" && lastMsg !== null &&
                                typeof lastMsg.content === "string") {
                                lastText = lastMsg.content;
                            } else if (lastMsg instanceof HTMLElement) {
                                lastText = lastMsg.innerText || lastMsg.textContent || "";
                            }
                        }
                        if (!generating && msgs.length > beforeCount && lastText.trim()) {
                            clearInterval(check);
                            window.__gobboBuddyLastAssistant = lastText;
                            window.__gobboBuddyDone = true;
                        }
                    } catch (pollError) {
                        clearInterval(check);
                        window.__gobboBuddyError = String(pollError);
                        window.__gobboBuddyDone = true;
                    }
                }, 300);
            } catch (error) {
                window.__gobboBuddyError = String(error);
                window.__gobboBuddyDone = true;
            }
        })();
    })();
    """
            script = script.replace("PROMPT_HERE", js_prompt)
            self.eval_js(script)

            deadline = time.time() + GENERATION_TIMEOUT
            while time.time() < deadline:
                done = self.eval_js("window.__gobboBuddyDone")
                if done:
                    error = self.eval_js("window.__gobboBuddyError")
                    if error:
                        raise RuntimeError(f"GobboNet generation failed: {error}")
                    result = self.eval_js("window.__gobboBuddyLastAssistant")
                    if not result:
                        raise RuntimeError("GobboNet returned an empty response.")
                    return result
                time.sleep(0.2)
            raise RuntimeError("GobboNet generation timed out.")

GOBBO_BRIDGE = GobboNetWebViewBridge()

# ============================================================
# ACCESSIBILITY & DIRECT GGUF HELPERS
# ============================================================
def get_active_window_info():
    if not HAS_ACCESSIBILITY:
        return None
    try:
        with auto.UIAutomationInitializerInThread():
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            title = win32gui.GetWindowText(hwnd) or "(no title)"
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc = psutil.Process(pid)
                app_name = proc.name() or f"pid:{pid}"
            except Exception:
                app_name = f"pid:{pid}"

            focused = auto.GetFocusedControl() or auto.ControlFromHandle(hwnd)
            pieces = []
            if focused:
                name = (focused.Name or "").strip()
                ctrl_type = getattr(focused, "ControlTypeName", "") or ""
                val = ""
                if hasattr(focused, "GetValuePattern"):
                    try:
                        val = focused.GetValuePattern().Value or ""
                    except Exception:
                        pass
                if name:
                    pieces.append(f"[{ctrl_type}] {name}")
                if val and val != name:
                    if MAX_RAW_CONTENT_CHARS is not None and MAX_RAW_CONTENT_CHARS > 0:
                        pieces.append(val[:MAX_RAW_CONTENT_CHARS])
                    else:
                        pieces.append(val)

            content = " | ".join(pieces).strip()
            if MAX_RAW_CONTENT_CHARS is not None and MAX_RAW_CONTENT_CHARS > 0:
                content = content[:MAX_RAW_CONTENT_CHARS]

            return {
                "app_name": app_name,
                "title": title[:300],
                "content": content or "(no readable content)"
            }
    except Exception:
        return None

def summarize_with_direct_gguf(app_name: str, title: str, content: str) -> str:
    prompt = f"The following is content data. Output a ONE SENTENCE summary of the data:\n{content}"
    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": SUMMARY_MAX_TOKENS,
        "temperature": SUMMARY_TEMPERATURE
    }
    try:
        r = requests.post(
            f"{LLM_DIRECT_BASE}/v1/chat/completions",
            json=payload,
            timeout=LLM_DIRECT_TIMEOUT
        )
        return r.json()["choices"][0]["message"]["content"].strip()[:MAX_SUMMARY_CHARS]
    except Exception:
        return f"looking at {title or app_name}"

def classify_emotion_with_direct_gguf(text: str) -> str:
    tag_list = build_emotion_tag_list(EMOTION_MAP)
    truncated = text[:CLASSIFIER_INPUT_CHARS] if CLASSIFIER_INPUT_CHARS else text

    # NOTE: previously only the first of these concatenated string pieces was
    # an f-string, so {tag_list}/{text} were never actually substituted here
    # -- the classifier was silently receiving literal "{tag_list}"/"{text}".
    # Fixed below so the real tag list and message are sent.
    #print(tag_list)
    prompt = ( #seems to work best for a long list of emotions and a small model.
        f"Here is a list of emotions:\n"
        f"{tag_list}\n"
        f"Choose one of the above to describe the emotions in the following message. Pick 'neutral' if you're not sure:\n\n{truncated}"
    )

    # test prompts... here for me to switch between them and try things out.
    prompt2 = (  # pretty good; snarky too often
        f"You overhear someone saying this:\n\n[[[\n{truncated}\n]]]\n\n"
        "What emotion was the speaker pretending to feel? Choose one of these:\n"
        f"{tag_list}"
    )

    prompt1 = (  # happy too often
        "You are an emotion classifier. You output emotion tags to improve immersion for a video game.\n\n"
        #"Do NOT judge whether the message is good, bad, funny, or polite.\n"
        #"Do NOT assume the speaker is happy just because they are talking conversationally.\n"
        f"You support the following tags: \n\n{tag_list}"
        "Examples:\n"
        "NPC: '''You look really happy today!''' > [NEUTRAL]\n"
        "NPC: '''I am absolutely terrified of spiders.''' > [ANXIOUS]\n"
        "NPC: '''Oh no, Box is sad.''' > [SAD]\n"
        "NPC: '''Wonderful. Another broken machine.''' > [SNARKY]\n"
        "NPC: '''Wait, you actually did it?!''' > [SHOCKED]\n"
        "NPC: '''He thinks he is clever.''' > [JUDGING]\n"
        "NPC: '''Haha, that was ridiculous.''' > [JOKING]\n"
        "NPC: '''I am so happy to see you!''' > [HAPPY]\n\n"

        "If the NPC's emotional state is unclear, output [NEUTRAL].\n"
        f"\n\nAn NPC is saying the following message. You must assign the NPC a facial expression by outputting an emotion tag. This is the message:\nNPC: '''{truncated}'''\n\n"
    )
    #print(truncated)

    grammar_parts = [f'"[{tag.upper()}]"' for tag in EMOTION_MAP]
    grammar = "root ::= " + " | ".join(grammar_parts)

    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": CLASSIFIER_MAX_TOKENS,
        "temperature": CLASSIFIER_TEMPERATURE,
        "grammar": grammar,
    }

    try:
        r = requests.post(
            f"{LLM_DIRECT_BASE}/v1/chat/completions",
            json=payload,
            timeout=LLM_DIRECT_TIMEOUT
        )
        raw = r.json()["choices"][0]["message"]["content"].strip()
        #print(f"Classifier: {raw}")
        for tag in EMOTION_MAP:
            if f"[{tag.upper()}]" in raw.upper():
                return tag
        return DEFAULT_EMOTION
    except Exception as e:
        logger.warning(f"Emotion classification failed: {e}")
        return DEFAULT_EMOTION

def proactive_prompt_rotator():
    if not PROACTIVE_PROMPTS:
        return "What do you think about this?"
    return random.choice(PROACTIVE_PROMPTS)

def build_simple_user_message(app_name: str, title: str, summary: str) -> str:
    what = title if title and title != "(no title)" else app_name
    what = what.replace("-", "")
    prompt = proactive_prompt_rotator()
    return PROACTIVE_TEMPLATE.format(
        app_name=app_name,
        what=what,
        summary=summary,
        prompt=prompt
    )

# ============================================================
# SMALL DIALOG HELPERS
# ============================================================
class SpriteSizeDialog(simpledialog.Dialog):
    def __init__(self, parent, current_w, current_h):
        self.w = current_w
        self.h = current_h
        super().__init__(parent, "Resize Sprite")

    def body(self, master):
        tk.Label(master, text="Sprite Width (px):").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.w_entry = tk.Entry(master)
        self.w_entry.insert(0, str(self.w))
        self.w_entry.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(master, text="Sprite Height (px):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.h_entry = tk.Entry(master)
        self.h_entry.insert(0, str(self.h))
        self.h_entry.grid(row=1, column=1, padx=5, pady=5)
        return self.w_entry

    def apply(self):
        try:
            self.result_w = int(self.w_entry.get())
            self.result_h = int(self.h_entry.get())
        except ValueError:
            self.result_w = self.w
            self.result_h = self.h


class AddEmotionRowDialog(simpledialog.Dialog):
    """Adds one (tag, row, col) entry to a profile's emotion table. The same
    tag can be added more than once with different coordinates on purpose --
    that's how a profile registers multiple sprites for one emotion."""

    def __init__(self, parent, tag="", row_=0, col_=0):
        self._tag = tag
        self._row = row_
        self._col = col_
        self.result = None
        super().__init__(parent, "Add Emotion Coordinate")

    def body(self, master):
        tk.Label(master, text="Emotion tag:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.tag_entry = tk.Entry(master)
        self.tag_entry.insert(0, self._tag)
        self.tag_entry.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(master, text="Row:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.row_entry = tk.Entry(master)
        self.row_entry.insert(0, str(self._row))
        self.row_entry.grid(row=1, column=1, padx=5, pady=5)

        tk.Label(master, text="Col:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.col_entry = tk.Entry(master)
        self.col_entry.insert(0, str(self._col))
        self.col_entry.grid(row=2, column=1, padx=5, pady=5)
        return self.tag_entry

    def apply(self):
        tag = self.tag_entry.get().strip().lower()
        try:
            r = int(self.row_entry.get())
            c = int(self.col_entry.get())
        except ValueError:
            r, c = 0, 0
        self.result = (tag, r, c) if tag else None


class EmotionGridPickerDialog(tk.Toplevel):
    """Visual grid picker: shows the sprite sheet with its row/col grid drawn
    over it, click a tile to select it, type a tag, and add it as an
    emotion-coordinate row. Stays open so several tiles can be added in a
    row -- handy for registering multiple sprites under one emotion tag."""

    MAX_DISPLAY = 480

    def __init__(self, parent, sheet_path, rows, cols, on_add):
        super().__init__(parent)
        self.title("Pick Emotion Coordinate")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.on_add = on_add
        self.rows = max(1, rows)
        self.cols = max(1, cols)
        self.selected = None

        image = Image.open(sheet_path).convert("RGBA")
        orig_w, orig_h = image.size

        # Scale to fit within MAX_DISPLAY, upscaling small sheets a bit too
        # so tiny tiles are still clickable.
        scale = self.MAX_DISPLAY / max(orig_w, orig_h)
        scale = min(scale, 3.0)
        disp_w = max(1, int(orig_w * scale))
        disp_h = max(1, int(orig_h * scale))
        self.disp_w, self.disp_h = disp_w, disp_h

        display_img = image.resize((disp_w, disp_h), Image.Resampling.NEAREST)
        self._photo = ImageTk.PhotoImage(display_img)

        self.canvas = tk.Canvas(self, width=disp_w, height=disp_h, highlightthickness=0)
        self.canvas.pack(padx=10, pady=10)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._draw_grid()
        self.canvas.bind("<Button-1>", self._on_click)

        self.selection_label = tk.Label(self, text="Click a tile to select it.")
        self.selection_label.pack(fill="x", padx=10)

        entry_row = tk.Frame(self)
        entry_row.pack(fill="x", padx=10, pady=(6, 10))
        tk.Label(entry_row, text="Tag:").pack(side="left")
        self.tag_var = tk.StringVar()
        tk.Entry(entry_row, textvariable=self.tag_var, width=18).pack(side="left", padx=6)
        tk.Button(entry_row, text="Add", command=self._add).pack(side="left", padx=6)
        tk.Button(entry_row, text="Close", command=self.destroy).pack(side="right")

    def _draw_grid(self):
        for r in range(1, self.rows):
            y = self.disp_h * r / self.rows
            self.canvas.create_line(0, y, self.disp_w, y, fill="#00ffff", dash=(3, 2))
        for c in range(1, self.cols):
            x = self.disp_w * c / self.cols
            self.canvas.create_line(x, 0, x, self.disp_h, fill="#00ffff", dash=(3, 2))

    def _on_click(self, event):
        col = min(self.cols - 1, max(0, int(event.x / self.disp_w * self.cols)))
        row = min(self.rows - 1, max(0, int(event.y / self.disp_h * self.rows)))
        self.selected = (row, col)
        self.selection_label.config(text=f"Selected: row {row}, col {col}")
        self._draw_highlight(row, col)

    def _draw_highlight(self, row, col):
        self.canvas.delete("highlight")
        x0 = self.disp_w * col / self.cols
        y0 = self.disp_h * row / self.rows
        x1 = self.disp_w * (col + 1) / self.cols
        y1 = self.disp_h * (row + 1) / self.rows
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#ff00ff", width=3, tags="highlight")

    def _add(self):
        if not self.selected:
            messagebox.showinfo("Pick Emotion Coordinate", "Click a tile first.", parent=self)
            return
        tag = self.tag_var.get().strip().lower()
        if not tag:
            messagebox.showinfo("Pick Emotion Coordinate", "Enter a tag name first.", parent=self)
            return
        row, col = self.selected
        self.on_add(tag, row, col)
        self.selection_label.config(
            text=f"Added [{tag}] at row {row}, col {col}. Pick another tile or close."
        )


class TopmostAskStringDialog(simpledialog.Dialog):
    """Same as tkinter.simpledialog.askstring(), but forces itself topmost
    and raises itself. Plain askstring() can otherwise end up rendered
    BEHIND its parent when that parent is itself an overrideredirect/topmost
    window (like the buddy's own sprite window, or the topmost editor
    windows) -- Tk doesn't propagate topmost-ness to dialogs on its own."""

    def __init__(self, parent, title, prompt):
        self._prompt = prompt
        self.result_text = None
        super().__init__(parent, title)

    def body(self, master):
        self.attributes("-topmost", True)
        self.lift()
        tk.Label(master, text=self._prompt).grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.entry = tk.Entry(master, width=40)
        self.entry.grid(row=1, column=0, padx=5, pady=5)
        return self.entry

    def apply(self):
        self.result_text = self.entry.get()

# ============================================================
# SETTINGS WINDOW  (right-click -> Settings...)
# ============================================================
class SettingsWindow(tk.Toplevel):
    """Global settings editor. 'Apply' pushes edits into the live running
    globals; 'Save to File' does that AND writes them to disk for future
    launches."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("GobboBuddy Settings")
        self.geometry("580x620")
        #self.attributes("-topmost", True)

        self.vars = {}
        self.types = {}

        # Reserved (side="bottom") before the scrollable area below, so the
        # buttons always keep their space rather than risk being squeezed
        # out by a long field list -- same fix applied to ProfileEditorWindow.
        button_bar = tk.Frame(self)
        button_bar.pack(side="bottom", fill="x", pady=6)

        outer = tk.Frame(self)
        outer.pack(side="top", fill="both", expand=True)

        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for row, field in enumerate(SETTINGS_SCHEMA):
            key = field["key"]
            gname = SETTINGS_GLOBAL_NAMES[key]
            current_value = globals().get(gname, SETTINGS_DEFAULTS[key])
            ftype = field["type"]
            self.types[key] = ftype

            tk.Label(
                self.scroll_frame, text=field["comment"], wraplength=380,
                justify="left", anchor="w", font=("Arial", 8), fg="#444"
            ).grid(row=row * 2, column=0, columnspan=2, sticky="w", padx=8, pady=(10, 0))

            tk.Label(self.scroll_frame, text=key, font=("Arial", 9, "bold")).grid(
                row=row * 2 + 1, column=0, sticky="w", padx=8
            )

            if ftype == "bool":
                var = tk.BooleanVar(value=bool(current_value))
                tk.Checkbutton(self.scroll_frame, variable=var).grid(
                    row=row * 2 + 1, column=1, sticky="w", padx=8, pady=(0, 4)
                )
            elif ftype == "color":
                var = tk.StringVar(value=str(current_value))
                entry_frame = tk.Frame(self.scroll_frame)
                entry_frame.grid(row=row * 2 + 1, column=1, sticky="w", padx=8, pady=(0, 4))
                tk.Entry(entry_frame, textvariable=var, width=12).pack(side="left")
                tk.Button(
                    entry_frame, text="Pick…", width=6,
                    command=lambda v=var: self._pick_color(v)
                ).pack(side="left", padx=(4, 0))
            else:
                var = tk.StringVar(value=str(current_value))
                tk.Entry(self.scroll_frame, textvariable=var, width=42).grid(
                    row=row * 2 + 1, column=1, sticky="w", padx=8, pady=(0, 4)
                )

            self.vars[key] = var

        tk.Button(button_bar, text="Apply", command=self.apply_only).pack(side="left", padx=6)
        tk.Button(button_bar, text="Save to File", command=self.apply_and_save).pack(side="left", padx=6)
        tk.Button(button_bar, text="Close", command=self.destroy).pack(side="right", padx=6)

    def _pick_color(self, var):
        color = colorchooser.askcolor(color=var.get() or "#ffffff")
        if color and color[1]:
            var.set(color[1])

    def _collect_values(self):
        values = {}
        errors = []
        for field in SETTINGS_SCHEMA:
            key = field["key"]
            ftype = self.types[key]
            raw = self.vars[key].get()
            try:
                if ftype == "int":
                    values[key] = int(float(raw))
                elif ftype == "float":
                    values[key] = float(raw)
                elif ftype == "bool":
                    values[key] = bool(raw)
                elif ftype == "color":
                    text = str(raw).strip()
                    if not (text.startswith("#") and len(text) == 7):
                        raise ValueError("expected a #rrggbb color")
                    values[key] = text
                else:
                    values[key] = str(raw)
            except Exception as e:
                errors.append(f"{key}: {e}")
        return values, errors

    def apply_only(self):
        values, errors = self._collect_values()
        if errors:
            messagebox.showerror("Settings", "Could not apply:\n" + "\n".join(errors))
            return
        g = globals()
        for key, value in values.items():
            g[SETTINGS_GLOBAL_NAMES[key]] = value
        SETTINGS.update(values)
        try:
            self.app.proactive_var.set(bool(PROACTIVE_ENABLED))
        except Exception:
            pass
        try:
            self.app.auto_switch_var.set(bool(AUTO_SWITCH_PROFILE_ON_SELECT))
        except Exception:
            pass
        if getattr(self.app, "_sheet_is_placeholder", False):
            self.app.set_transparent_color(FALLBACK_TRANSPARENT_COLOR)
        logger.info("Settings applied to the live program.")

    def apply_and_save(self):
        self.apply_only()
        save_settings(SETTINGS)
        logger.info(f"Settings saved to {SETTINGS_PATH}")

# ============================================================
# PROFILE EDITOR WINDOW  (right-click -> Edit / Create Profile...)
# ============================================================
class ProfileEditorWindow(tk.Toplevel):
    """Create or edit a character profile: sprite sheet, sizing, emotion
    map, proactive prompt 'garden', proactive template, and proactive rate."""

    def __init__(self, app, initial_character_name=""):
        super().__init__(app)
        self.app = app
        self.title("GobboBuddy Profile Editor")
        # +50px taller than the original, and fixed: see button_bar note below
        # for why the size is locked instead of made scrollable.
        self.geometry("640x770")
        self.resizable(False, False)
        #self.attributes("-topmost", True)

        self._current_stem = None
        self._pending_sprite_source = None
        self._emotion_rows = []

        top_bar = tk.Frame(self)
        top_bar.pack(side="top", fill="x", padx=8, pady=6)
        tk.Label(top_bar, text="Load existing:").pack(side="left")
        self.existing_var = tk.StringVar()
        self.existing_combo = ttk.Combobox(
            top_bar, textvariable=self.existing_var, state="readonly", width=26
        )
        self.existing_combo.pack(side="left", padx=6)
        self.existing_combo.bind("<<ComboboxSelected>>", self._on_pick_existing)
        tk.Button(top_bar, text="New Profile", command=self._new_profile).pack(side="left", padx=6)
        self._refresh_existing_list()

        # Packed (and reserved) BEFORE the form, with side="bottom", so the
        # Save/Apply/Delete buttons always keep their space at the bottom of
        # the fixed-size window instead of being squeezed off-screen by a
        # tall form. Populated further down; see button_bar below.
        button_bar = tk.Frame(self)
        button_bar.pack(side="bottom", fill="x", pady=6)

        form = tk.Frame(self)
        form.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        tk.Label(form, text="Character name (must match a GobboNet card name):").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        self.name_var = tk.StringVar(value=initial_character_name or "")
        tk.Entry(form, textvariable=self.name_var, width=42).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        tk.Label(form, text="Sprite sheet:").grid(row=2, column=0, sticky="w")
        self.sprite_label_var = tk.StringVar(value="(none)")
        tk.Label(form, textvariable=self.sprite_label_var, fg="#555").grid(row=2, column=1, sticky="w")
        tk.Button(form, text="Choose PNG…", command=self._choose_sprite).grid(row=2, column=2, sticky="w")

        tk.Label(form, text="Sprite width (px):").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.width_var = tk.StringVar(value=str(DEFAULT_SPRITE_WIDTH))
        tk.Entry(form, textvariable=self.width_var, width=8).grid(row=3, column=1, sticky="w", pady=(6, 0))

        tk.Label(form, text="Sprite height (px):").grid(row=4, column=0, sticky="w")
        self.height_var = tk.StringVar(value=str(DEFAULT_SPRITE_HEIGHT))
        tk.Entry(form, textvariable=self.height_var, width=8).grid(row=4, column=1, sticky="w")

        tk.Label(form, text="Sheet rows:").grid(row=5, column=0, sticky="w")
        self.rows_var = tk.StringVar(value=str(DEFAULT_ROWS))
        tk.Entry(form, textvariable=self.rows_var, width=8).grid(row=5, column=1, sticky="w")

        tk.Label(form, text="Sheet cols:").grid(row=6, column=0, sticky="w")
        self.cols_var = tk.StringVar(value=str(DEFAULT_COLS))
        tk.Entry(form, textvariable=self.cols_var, width=8).grid(row=6, column=1, sticky="w")

        bubble_offset_frame = tk.Frame(form)
        bubble_offset_frame.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Label(bubble_offset_frame, text="Speech bubble offset -- X:").pack(side="left")
        self.bubble_offset_x_var = tk.StringVar(value=str(DEFAULT_BUBBLE_OFFSET_X))
        tk.Entry(bubble_offset_frame, textvariable=self.bubble_offset_x_var, width=6).pack(
            side="left", padx=(4, 12)
        )
        tk.Label(bubble_offset_frame, text="Y:").pack(side="left")
        self.bubble_offset_y_var = tk.StringVar(value=str(DEFAULT_BUBBLE_OFFSET_Y))
        tk.Entry(bubble_offset_frame, textvariable=self.bubble_offset_y_var, width=6).pack(
            side="left", padx=4
        )
        tk.Label(
            bubble_offset_frame, text="px (0,0 = centered above the sprite, as usual)", fg="#555"
        ).pack(side="left", padx=(6, 0))

        tk.Label(
            form,
            text="Emotions (you can use the same tag for multiple coordinates to get \n"
                "a random sprite for that emotion, or use multiple tags for the same coordinate:"
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))

        tree_frame = tk.Frame(form)
        tree_frame.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(2, 4))
        self.emotion_tree = ttk.Treeview(
            tree_frame, columns=("tag", "row", "col"), show="headings", height=6
        )
        for col, width in (("tag", 160), ("row", 60), ("col", 60)):
            self.emotion_tree.heading(col, text=col.capitalize())
            self.emotion_tree.column(col, width=width, anchor="center")
        self.emotion_tree.pack(side="left", fill="both", expand=True)
        tree_scroll = tk.Scrollbar(tree_frame, command=self.emotion_tree.yview)
        tree_scroll.pack(side="right", fill="y")
        self.emotion_tree.configure(yscrollcommand=tree_scroll.set)

        emo_btns = tk.Frame(form)
        emo_btns.grid(row=10, column=0, columnspan=3, sticky="w")
        tk.Button(emo_btns, text="Pick From Sheet…", command=self._pick_from_sheet).pack(side="left")
        tk.Button(emo_btns, text="Add Row…", command=self._add_emotion_row).pack(side="left", padx=6)
        tk.Button(emo_btns, text="Remove Selected", command=self._remove_emotion_rows).pack(side="left", padx=6)
        tk.Button(emo_btns, text="Remove all", command=self._remove_all_emotions).pack(side="left", padx=6)

        tk.Label(form, text="Proactive prompts, one per line:").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        self.garden_text = tk.Text(form, height=6, width=62, wrap="word")
        self.garden_text.grid(row=12, column=0, columnspan=3, sticky="nsew")

        tk.Label(
            form, text="Proactive template ({app_name}, {what}, {summary}, {prompt}):"
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.template_text = tk.Text(form, height=4, width=62, wrap="word")
        self.template_text.grid(row=14, column=0, columnspan=3, sticky="nsew")

        self.rate_override_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            form, text="Override the global proactive rate for this character",
            variable=self.rate_override_var, command=self._toggle_rate_fields
        ).grid(row=15, column=0, columnspan=3, sticky="w", pady=(10, 0))

        rate_frame = tk.Frame(form)
        rate_frame.grid(row=16, column=0, columnspan=3, sticky="w")
        tk.Label(rate_frame, text="Min sec:").grid(row=0, column=0)
        self.rate_min_var = tk.StringVar(value=str(SETTINGS["proactive_check_period_min"]))
        self.rate_min_entry = tk.Entry(rate_frame, textvariable=self.rate_min_var, width=8)
        self.rate_min_entry.grid(row=0, column=1, padx=4)
        tk.Label(rate_frame, text="Max sec:").grid(row=0, column=2)
        self.rate_max_var = tk.StringVar(value=str(SETTINGS["proactive_check_period_max"]))
        self.rate_max_entry = tk.Entry(rate_frame, textvariable=self.rate_max_var, width=8)
        self.rate_max_entry.grid(row=0, column=3, padx=4)
        tk.Label(rate_frame, text="Probability:").grid(row=0, column=4)
        self.rate_prob_var = tk.StringVar(value=str(SETTINGS["proactive_probability"]))
        self.rate_prob_entry = tk.Entry(rate_frame, textvariable=self.rate_prob_var, width=8)
        self.rate_prob_entry.grid(row=0, column=5, padx=4)
        self._toggle_rate_fields()

        form.grid_rowconfigure(12, weight=1)
        form.grid_rowconfigure(14, weight=1)
        form.grid_columnconfigure(1, weight=1)

        # (button_bar itself was already created and packed at the top of
        # __init__, side="bottom", so its space is reserved first -- see note
        # there. We only add its buttons here, where the rest of the layout
        # logic lives.)
        tk.Button(button_bar, text="Save", command=self._save).pack(side="left", padx=6)
        tk.Button(button_bar, text="Save As New Name…", command=self._save_as).pack(side="left", padx=6)
        tk.Button(button_bar, text="Apply Now (Live)", command=self._apply_now).pack(side="left", padx=6)
        tk.Button(button_bar, text="Delete Profile", command=self._delete).pack(side="left", padx=6)
        tk.Button(button_bar, text="Close", command=self.destroy).pack(side="right", padx=6)

        if initial_character_name:
            found = find_profile_for_character(initial_character_name)
            if found:
                self._load_profile(found[0], found[1])
            else:
                self._load_data(default_profile_data(initial_character_name))
        else:
            self._load_data(default_profile_data(""))

    # -- population helpers --
    def _refresh_existing_list(self):
        self._profiles_index = list_profiles()
        self.existing_combo["values"] = [name for name, _ in self._profiles_index]

    def _on_pick_existing(self, event=None):
        selected = self.existing_var.get()
        for name, path in self._profiles_index:
            if name == selected:
                try:
                    data = read_profile_json(path)
                except Exception as e:
                    messagebox.showerror("Profile Editor", f"Could not read {path.name}: {e}")
                    return
                self._load_profile(path, data)
                return

    def _new_profile(self):
        self._current_stem = None
        self._pending_sprite_source = None
        self.existing_var.set("")
        self._load_data(default_profile_data(""))

    def _load_profile(self, path, data):
        self._current_stem = path.stem
        self._pending_sprite_source = None
        self.existing_var.set(str(data.get("character_name") or path.stem))
        self._load_data(data)

    def _load_data(self, data):
        self.name_var.set(str(data.get("character_name", "")))
        sprite_name = data.get("sprite_sheet") or ""
        self.sprite_label_var.set(sprite_name or "(none — gray placeholder box)")
        self.width_var.set(str(data.get("sprite_width", DEFAULT_SPRITE_WIDTH)))
        self.height_var.set(str(data.get("sprite_height", DEFAULT_SPRITE_HEIGHT)))
        self.rows_var.set(str(data.get("rows", DEFAULT_ROWS)))
        self.cols_var.set(str(data.get("cols", DEFAULT_COLS)))
        self.bubble_offset_x_var.set(str(data.get("bubble_offset_x", DEFAULT_BUBBLE_OFFSET_X)))
        self.bubble_offset_y_var.set(str(data.get("bubble_offset_y", DEFAULT_BUBBLE_OFFSET_Y)))

        self._emotion_rows = []
        normalized = normalize_emotion_map(data.get("emotions", []))
        for tag, coords in normalized.items():
            for (r, c) in coords:
                self._emotion_rows.append([tag, r, c])
        self._refresh_emotion_tree()

        self.garden_text.delete("1.0", tk.END)
        for line in (data.get("garden") or []):
            self.garden_text.insert(tk.END, line + "\n")

        self.template_text.delete("1.0", tk.END)
        self.template_text.insert(tk.END, data.get("proactive_template") or DEFAULT_PROACTIVE_TEMPLATE)

        has_override = any(
            data.get(k) is not None
            for k in ("proactive_check_period_min", "proactive_check_period_max", "proactive_probability")
        )
        self.rate_override_var.set(has_override)
        if data.get("proactive_check_period_min") is not None:
            self.rate_min_var.set(str(data["proactive_check_period_min"]))
        if data.get("proactive_check_period_max") is not None:
            self.rate_max_var.set(str(data["proactive_check_period_max"]))
        if data.get("proactive_probability") is not None:
            self.rate_prob_var.set(str(data["proactive_probability"]))
        self._toggle_rate_fields()

    def _refresh_emotion_tree(self):
        self.emotion_tree.delete(*self.emotion_tree.get_children())
        for i, (tag, r, c) in enumerate(self._emotion_rows):
            self.emotion_tree.insert("", "end", iid=str(i), values=(tag, r, c))

    def _add_emotion_row(self):
        dialog = AddEmotionRowDialog(self)
        if dialog.result:
            self._emotion_rows.append(list(dialog.result))
            self._refresh_emotion_tree()

    def _remove_emotion_rows(self):
        selected = self.emotion_tree.selection()
        indices = sorted((int(i) for i in selected), reverse=True)
        for i in indices:
            del self._emotion_rows[i]
        self._refresh_emotion_tree()

    def _remove_all_emotions (self):
        answer = messagebox.askyesno(title="Confirmation", message="Really remove all sprites?")
        if answer:
            del self._emotion_rows[:]
            self._refresh_emotion_tree()
       

    def _choose_sprite(self):
        path = filedialog.askopenfilename(
            title="Select Sprite Sheet PNG",
            filetypes=[("PNG images", "*.png"), ("All files", "*.*")]
        )
        if path:
            self._pending_sprite_source = path
            self.sprite_label_var.set(Path(path).name + "  (will be copied on save)")

    def _resolve_preview_sprite_path(self):
        """Whichever sprite sheet the editor currently has in hand: a
        newly-chosen-but-unsaved PNG takes priority, else the saved profile's
        own PNG on disk, if any."""
        if self._pending_sprite_source:
            return self._pending_sprite_source
        if self._current_stem:
            p = profile_png_path(self._current_stem)
            if p.exists():
                return p
        return None

    def _pick_from_sheet(self):
        sheet_path = self._resolve_preview_sprite_path()
        if not sheet_path:
            messagebox.showinfo(
                "Profile Editor", "Choose a sprite PNG first (or load a profile that has one)."
            )
            return
        try:
            rows_ = max(1, int(self.rows_var.get()))
            cols_ = max(1, int(self.cols_var.get()))
        except ValueError:
            messagebox.showerror("Profile Editor", "Rows/Cols must be whole numbers first.")
            return

        def _on_add(tag, r, c):
            self._emotion_rows.append([tag, r, c])
            self._refresh_emotion_tree()

        try:
            EmotionGridPickerDialog(self, sheet_path, rows_, cols_, _on_add)
        except Exception as e:
            messagebox.showerror("Profile Editor", f"Could not open the sprite sheet: {e}")

    def _toggle_rate_fields(self):
        state = "normal" if self.rate_override_var.get() else "disabled"
        self.rate_min_entry.configure(state=state)
        self.rate_max_entry.configure(state=state)
        self.rate_prob_entry.configure(state=state)

    # -- validation / gather --
    def _gather(self, target_stem):
        errors = []
        name = self.name_var.get().strip()
        if not name:
            errors.append("Character name is required.")

        def _int(var, label):
            try:
                return int(var.get())
            except ValueError:
                errors.append(f"{label} must be a whole number.")
                return 0

        def _float(var, label):
            try:
                return float(var.get())
            except ValueError:
                errors.append(f"{label} must be a number.")
                return 0.0

        width = _int(self.width_var, "Sprite width")
        height = _int(self.height_var, "Sprite height")
        rows_ = _int(self.rows_var, "Rows")
        cols_ = _int(self.cols_var, "Cols")
        bubble_offset_x = _int(self.bubble_offset_x_var, "Bubble offset X")
        bubble_offset_y = _int(self.bubble_offset_y_var, "Bubble offset Y")

        garden = [
            line.strip() for line in self.garden_text.get("1.0", tk.END).splitlines()
            if line.strip()
        ]
        template = self.template_text.get("1.0", tk.END).strip() or DEFAULT_PROACTIVE_TEMPLATE

        rate_min = rate_max = rate_prob = None
        if self.rate_override_var.get():
            rate_min = _float(self.rate_min_var, "Min seconds")
            rate_max = _float(self.rate_max_var, "Max seconds")
            rate_prob = _float(self.rate_prob_var, "Probability")

        sprite_filename = ""
        if self._pending_sprite_source:
            sprite_filename = f"{target_stem}.png"
        elif self._current_stem and profile_png_path(self._current_stem).exists():
            sprite_filename = f"{target_stem}.png"

        data = {
            "character_name": name,
            "sprite_sheet": sprite_filename,
            "sprite_width": width,
            "sprite_height": height,
            "rows": rows_,
            "cols": cols_,
            "bubble_offset_x": bubble_offset_x,
            "bubble_offset_y": bubble_offset_y,
            "emotions": [list(row) for row in self._emotion_rows],
            "garden": garden,
            "proactive_template": template,
            "proactive_check_period_min": rate_min,
            "proactive_check_period_max": rate_max,
            "proactive_probability": rate_prob,
        }
        return data, errors

    def _confirm_overwrite_if_needed(self, stem, data):
        """Guards against silently clobbering a different profile that
        happens to sanitize to the same filename stem."""
        existing_path = profile_json_path(stem)
        if not existing_path.exists() or stem == (self._current_stem or ""):
            return True
        try:
            existing_data = read_profile_json(existing_path)
        except Exception:
            existing_data = {}
        existing_name = str(existing_data.get("character_name", "")).strip().lower()
        if existing_name and existing_name != data["character_name"].strip().lower():
            return messagebox.askyesno(
                "Profile Editor",
                f"A different profile ('{existing_data.get('character_name')}') already "
                f"uses the filename '{stem}.json'. Overwrite it?"
            )
        return True

    def _write_to_disk(self, stem, data):
        write_profile_json(profile_json_path(stem), data)
        target_png = profile_png_path(stem)
        if self._pending_sprite_source:
            shutil.copyfile(self._pending_sprite_source, target_png)
            self._pending_sprite_source = None
        elif (self._current_stem and self._current_stem != stem
              and profile_png_path(self._current_stem).exists()):
            shutil.copyfile(profile_png_path(self._current_stem), target_png)

    def _save(self):
        stem = self._current_stem or sanitize_profile_stem(self.name_var.get())
        data, errors = self._gather(stem)
        if errors:
            messagebox.showerror("Profile Editor", "\n".join(errors))
            return
        if not self._confirm_overwrite_if_needed(stem, data):
            return
        self._write_to_disk(stem, data)
        self._current_stem = stem
        self._refresh_existing_list()
        self.existing_var.set(data["character_name"])
        messagebox.showinfo("Profile Editor", f"Saved profile '{data['character_name']}'.")

    def _save_as(self):
        new_name = TopmostAskStringDialog(self, "Save As New Profile", "New character name:").result_text
        if not new_name:
            return
        stem = sanitize_profile_stem(new_name)
        data, errors = self._gather(stem)
        if errors:
            messagebox.showerror("Profile Editor", "\n".join(errors))
            return
        data["character_name"] = new_name
        if not self._confirm_overwrite_if_needed(stem, data):
            return
        self._write_to_disk(stem, data)
        self._current_stem = stem
        self._refresh_existing_list()
        self.existing_var.set(new_name)
        messagebox.showinfo("Profile Editor", f"Saved new profile '{new_name}'.")

    def _apply_now(self):
        stem = self._current_stem or sanitize_profile_stem(self.name_var.get())
        data, errors = self._gather(stem)
        if errors:
            messagebox.showerror("Profile Editor", "\n".join(errors))
            return
        if not self._confirm_overwrite_if_needed(stem, data):
            return
        self._write_to_disk(stem, data)
        self._current_stem = stem
        self._refresh_existing_list()
        self.app.apply_profile(profile_json_path(stem), data)
        self.app.active_profile_name = data["character_name"]
        logger.info(f"Profile '{data['character_name']}' applied live (preview).")

    def _delete(self):
        if not self._current_stem:
            messagebox.showinfo(
                "Profile Editor", "Nothing to delete -- this profile hasn't been saved yet."
            )
            return
        if not messagebox.askyesno(
            "Delete Profile", f"Delete profile '{self._current_stem}'? This cannot be undone."
        ):
            return
        for p in (profile_json_path(self._current_stem), profile_png_path(self._current_stem)):
            try:
                if p.exists():
                    p.unlink()
            except OSError as e:
                logger.warning(f"Could not delete {p}: {e}")
        self._current_stem = None
        self._refresh_existing_list()
        self._new_profile()

# ============================================================
# TKINTER UI
# ============================================================
class GobboNetHelper(tk.Tk):

    def __init__(self):
        super().__init__()

        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)

        self.transparent_color = FALLBACK_TRANSPARENT_COLOR
        self._safe_set_transparentcolor(self.transparent_color)
        self.configure(bg=self.transparent_color)

        self.proactive_var = tk.BooleanVar(value=PROACTIVE_ENABLED)
        self.auto_switch_var = tk.BooleanVar(value=AUTO_SWITCH_PROFILE_ON_SELECT)

        self._drag_start_x = 0
        self._drag_start_y = 0
        self._is_dragging = False

        self._sprite_anchor_x = None
        self._sprite_anchor_y = None

        self.response_queue = queue.Queue()
        self.raw_stream_text = ""
        self.parsed_emotion = None
        self.sprites = {}
        self._raw_sheet = None
        self._sheet_is_placeholder = True
        self.active_profile_name = None
        # Tracks whether we've done the one-time startup profile resolution
        # yet. That first resolution always happens automatically -- see
        # activate_profile_for_character() -- regardless of the
        # auto-switch-on-select setting, which only governs later switches.
        self._startup_profile_resolved = False

        self._last_message_time = time.time()
        self._next_opportunity_at = None
        self._schedule_next_opportunity()

        self._proactive_lock = threading.Lock()
        self._proactive_busy = False

        self.container = tk.Frame(self, bg=self.transparent_color)
        self.container.pack(fill="both", expand=True)

        # Bubble
        self.bubble_frame = tk.Frame(
            self.container, bg="white",
            highlightbackground="black", highlightthickness=2, bd=0
        )
        self.bubble_text = tk.Text(
            self.bubble_frame, bg="white", fg="black", font=("Arial", 10),
            wrap="word", width=30, height=1, bd=0, relief="flat",
            highlightthickness=0, cursor="ibeam", padx=8, pady=6
        )
        self.bubble_text.pack()
        self.bubble_text.config(state="disabled")
        self.bubble_text.bind("<MouseWheel>", self.on_text_mousewheel)

        # Tail
        self.tail_canvas = tk.Canvas(
            self.container, width=24, height=12,
            bg=self.transparent_color, highlightthickness=0, bd=0
        )
        self.tail_canvas.create_polygon(2, 0, 22, 0, 12, 11, fill="white", outline="black", width=2)
        self.tail_canvas.create_line(3, 0, 21, 0, fill="white", width=3)

        # Sprite
        self.sprite_label = tk.Label(self.container, bg=self.transparent_color, bd=0, cursor="fleur")
        # Positioned via place() inside _relayout(), not packed here -- see
        # that method for why (it needs to apply BUBBLE_OFFSET_X/Y and keep
        # the sprite pinned to its on-screen anchor as the bubble and sprite
        # size change independently).
        self._bubble_visible = False

        self.current_emotion = DEFAULT_EMOTION

        # Nothing is known yet: show the hard-coded defaults (gray box, no
        # sprite, default prompts) until GobboNet tells us who's active.
        self.apply_defaults()

        # Drag bindings
        self.sprite_label.bind("<ButtonPress-1>", self.on_press)
        self.sprite_label.bind("<B1-Motion>", self.on_drag)
        self.sprite_label.bind("<ButtonRelease-1>", self.on_release)

        for widget in (self.sprite_label, self.bubble_frame, self.bubble_text, self.tail_canvas):
            widget.bind("<Button-3>", self.show_context_menu)

        self.center_on_screen()
        self.update_sprite_anchor()

        self.check_queue()
        self._tick_proactive()

    def quit_application(self):
        try:
            GOBBO_BRIDGE.close()
        except Exception as error:
            logger.error(f"Could not close GobboNet WebView: {error}")
        try:
            self.quit()
        finally:
            self.destroy()

    # ============================================================
    # TRANSPARENCY
    # ============================================================
    def _safe_set_transparentcolor(self, hex_color):
        try:
            self.wm_attributes("-transparentcolor", hex_color)
        except tk.TclError as e:
            logger.warning(f"This platform does not support -transparentcolor: {e}")

    def set_transparent_color(self, hex_color):
        if hex_color == getattr(self, "transparent_color", None):
            return
        self.transparent_color = hex_color
        self._safe_set_transparentcolor(hex_color)
        self.configure(bg=hex_color)
        self.container.configure(bg=hex_color)
        self.tail_canvas.configure(bg=hex_color)
        self.sprite_label.configure(bg=hex_color)

    # ============================================================
    # PROACTIVE
    # ============================================================
    def _schedule_next_opportunity(self):
        delay = random.uniform(RANDOM_WINDOW_MIN_SECONDS, RANDOM_WINDOW_MAX_SECONDS)
        self._next_opportunity_at = time.time() + delay
        #print(f"Proactive: next opportunity in {delay:.1f}s")

    def _note_message_exchanged(self):
        self._last_message_time = time.time()
        self._schedule_next_opportunity()

    def _sync_buddy_name_from_bridge(self):
        """Pick up the auto-detected active character as BUDDY_NAME, once
        available, and load its profile (or the defaults) accordingly."""
        global BUDDY_NAME
        if not GOBBO_BRIDGE.state_ready_event.is_set():
            return
        detected = GOBBO_BRIDGE.active_character_name
        if detected and BUDDY_NAME != detected:
            BUDDY_NAME = detected
            logger.info(f"BUDDY_NAME auto-set to '{BUDDY_NAME}' from active GobboNet character.")
            self.activate_profile_for_character(BUDDY_NAME)

    def toggle_proactive(self):
        global PROACTIVE_ENABLED
        PROACTIVE_ENABLED = self.proactive_var.get()
        SETTINGS["proactive_enabled"] = PROACTIVE_ENABLED
        save_settings(SETTINGS)
        logger.info(f"Proactive mode set to: {PROACTIVE_ENABLED}")

    def toggle_auto_switch_profile(self):
        global AUTO_SWITCH_PROFILE_ON_SELECT
        AUTO_SWITCH_PROFILE_ON_SELECT = self.auto_switch_var.get()
        SETTINGS["auto_switch_profile_on_select"] = AUTO_SWITCH_PROFILE_ON_SELECT
        save_settings(SETTINGS)
        logger.info(f"Auto-switch profile on character select set to: {AUTO_SWITCH_PROFILE_ON_SELECT}")

    def _tick_proactive(self):
        try:
            self._sync_buddy_name_from_bridge()
            if (
                PROACTIVE_ENABLED
                and HAS_ACCESSIBILITY
                and self._next_opportunity_at
                and time.time() >= self._next_opportunity_at
                and not self._proactive_busy
            ):
                if random.random() < RANDOM_WINDOW_PROBABILITY:
                    #print("Proactive: roll success. Starting worker.")
                    threading.Thread(target=self._proactive_worker, daemon=True).start()
                self._schedule_next_opportunity()
        except Exception as error:
            logger.error(f"Proactive Tick Error: {error}")
        self.after(2000, self._tick_proactive)

    def _proactive_worker(self):
        with self._proactive_lock:
            if self._proactive_busy:
                return
            self._proactive_busy = True
        try:
            if not GOBBO_BRIDGE.state_ready_event.is_set():
                return
            info = get_active_window_info()
            if info:
                if info["content"] == '(no readable content)':
                    summary = "Junk and garble. Nothing coherent to see."
                else:
                    summary = summarize_with_direct_gguf(
                        info["app_name"],
                        info["title"],
                        info["content"]
                    )
                self.send_to_gobbonet(
                    build_simple_user_message(info["app_name"], info["title"], summary)
                )
        except Exception as error:
            logger.error(f"Proactive Worker Error: {error}")
        finally:
            self._proactive_busy = False

    # ============================================================
    # SPRITES
    # ============================================================
    def load_sprite_sheet(self, path, width, height):
        """`path` may be a profile-relative/absolute path, or None for the
        no-sprite placeholder. On a real sheet, the top-left pixel is
        sampled and used as this window's transparent color; the
        placeholder always keeps FALLBACK_TRANSPARENT_COLOR instead, so the
        gray box stays visible rather than vanishing."""
        global SPRITE_SHEET_PATH, SPRITE_WIDTH, SPRITE_HEIGHT

        SPRITE_WIDTH = width
        SPRITE_HEIGHT = height

        resolved = None
        if path:
            p = Path(path)
            resolved = p if p.is_absolute() else (SCRIPT_DIR / p)

        if not resolved or not resolved.exists():
            if path:
                logger.warning(f"Sprite sheet not found: {resolved}")
            SPRITE_SHEET_PATH = None
            self._raw_sheet = None
            self._sheet_is_placeholder = True
            placeholder = Image.new(
                "RGBA", (max(1, width), max(1, height)), color=(200, 200, 200, 255)
            )
            self.placeholder_img = ImageTk.PhotoImage(placeholder)
            self.sprites = {emotion: [self.placeholder_img] for emotion in EMOTION_MAP}
            self.sprites.setdefault(DEFAULT_EMOTION, [self.placeholder_img])
            self.set_transparent_color(FALLBACK_TRANSPARENT_COLOR)
            self.update_sprite(self.current_emotion)
            return

        sheet = Image.open(resolved).convert("RGBA")
        self._raw_sheet = sheet
        self._sheet_is_placeholder = False
        SPRITE_SHEET_PATH = str(resolved)

        self.set_transparent_color(sample_transparent_color(sheet))
        self._rebuild_sprites()

    def _rebuild_sprites(self):
        if self._raw_sheet is None or self._sheet_is_placeholder:
            return

        sw, sh = self._raw_sheet.size
        cols = max(1, COLS)
        rows = max(1, ROWS)
        sp_w = max(1, sw // cols)
        sp_h = max(1, sh // rows)

        sprites = {}
        for emotion, coords in EMOTION_MAP.items():
            images = []
            for (r, c) in coords:
                box = (c * sp_w, r * sp_h, (c + 1) * sp_w, (r + 1) * sp_h)
                cropped = self._raw_sheet.crop(box)
                images.append(ImageTk.PhotoImage(
                    cropped.resize((SPRITE_WIDTH, SPRITE_HEIGHT), Image.Resampling.LANCZOS)
                ))
            if images:
                sprites[emotion] = images

        self.sprites = sprites or self.sprites
        self.update_sprite(self.current_emotion)

    def update_sprite(self, emotion):
        emotion = (emotion or "").lower().strip()
        if emotion not in self.sprites:
            emotion = DEFAULT_EMOTION if DEFAULT_EMOTION in self.sprites else next(iter(self.sprites), emotion)
        self.current_emotion = emotion
        choices = self.sprites.get(emotion)
        if choices:
            # Multiple sprites can be registered for one emotion; pick one at random.
            self._current_sprite_image = random.choice(choices)
            self.sprite_label.config(image=self._current_sprite_image)

    def change_sprite_size(self):
        """Session-only quick resize. Use 'Edit / Create Profile...' to persist it."""
        global SPRITE_WIDTH, SPRITE_HEIGHT
        dialog = SpriteSizeDialog(self, SPRITE_WIDTH, SPRITE_HEIGHT)
        if hasattr(dialog, "result_w") and hasattr(dialog, "result_h"):
            new_w = max(32, min(512, dialog.result_w))
            new_h = max(32, min(512, dialog.result_h))
            if new_w != SPRITE_WIDTH or new_h != SPRITE_HEIGHT:
                SPRITE_WIDTH, SPRITE_HEIGHT = new_w, new_h
                self._rebuild_sprites()
                self._relayout()
                self.update_sprite_anchor()
                logger.info(f"Sprite size set (session only) to {new_w}x{new_h}px")

    def change_sprite_sheet(self):
        """Session-only quick swap. Use 'Edit / Create Profile...' to persist it."""
        path = filedialog.askopenfilename(
            title="Select Sprite Sheet (session only)",
            filetypes=[("PNG images", "*.png"), ("All files", "*.*")],
            initialdir=SCRIPT_DIR
        )
        if not path:
            return
        self.load_sprite_sheet(path, SPRITE_WIDTH, SPRITE_HEIGHT)
        self._relayout()
        self.update_sprite_anchor()
        logger.info(f"Sprite sheet changed (session only) to {path}")

    # ============================================================
    # PROFILES
    # ============================================================
    def apply_profile(self, profile_path, data):
        """Applies a loaded profile dict to the running program: emotion
        map, garden, template, rate, and sprite sheet/sizing/grid."""
        global EMOTION_MAP, PROACTIVE_PROMPTS, PROACTIVE_TEMPLATE
        global RANDOM_WINDOW_MIN_SECONDS, RANDOM_WINDOW_MAX_SECONDS, RANDOM_WINDOW_PROBABILITY
        global ROWS, COLS, BUBBLE_OFFSET_X, BUBBLE_OFFSET_Y

        profile_dir = Path(profile_path).parent

        ROWS = int(data.get("rows") or DEFAULT_ROWS)
        COLS = int(data.get("cols") or DEFAULT_COLS)
        BUBBLE_OFFSET_X = int(data.get("bubble_offset_x") or 0)
        BUBBLE_OFFSET_Y = int(data.get("bubble_offset_y") or 0)

        EMOTION_MAP = normalize_emotion_map(data.get("emotions", [])) or normalize_emotion_map(DEFAULT_EMOTIONS)
        PROACTIVE_PROMPTS = data.get("garden") or DEFAULT_PROACTIVE_PROMPTS.copy()
        PROACTIVE_TEMPLATE = data.get("proactive_template") or DEFAULT_PROACTIVE_TEMPLATE

        rate_min = data.get("proactive_check_period_min")
        RANDOM_WINDOW_MIN_SECONDS = rate_min if rate_min is not None else SETTINGS["proactive_check_period_min"]
        rate_max = data.get("proactive_check_period_max")
        RANDOM_WINDOW_MAX_SECONDS = rate_max if rate_max is not None else SETTINGS["proactive_check_period_max"]
        rate_prob = data.get("proactive_probability")
        RANDOM_WINDOW_PROBABILITY = rate_prob if rate_prob is not None else SETTINGS["proactive_probability"]

        width = int(data.get("sprite_width") or DEFAULT_SPRITE_WIDTH)
        height = int(data.get("sprite_height") or DEFAULT_SPRITE_HEIGHT)
        sprite_name = data.get("sprite_sheet")
        sprite_path = (profile_dir / sprite_name) if sprite_name else None

        self.current_emotion = DEFAULT_EMOTION
        self.load_sprite_sheet(sprite_path, width, height)
        # Re-lays-out using the OLD anchor (so the sprite's top-left corner
        # stays pinned through the size change), then re-baselines the
        # anchor from wherever that landed -- for next time.
        self._relayout()
        self.update_sprite_anchor()

    def apply_defaults(self):
        """Loads the hard-coded fallback appearance: gray box, no sprite,
        default prompts, at the global proactive rate."""
        global EMOTION_MAP, PROACTIVE_PROMPTS, PROACTIVE_TEMPLATE
        global RANDOM_WINDOW_MIN_SECONDS, RANDOM_WINDOW_MAX_SECONDS, RANDOM_WINDOW_PROBABILITY
        global ROWS, COLS, BUBBLE_OFFSET_X, BUBBLE_OFFSET_Y

        ROWS, COLS = DEFAULT_ROWS, DEFAULT_COLS
        BUBBLE_OFFSET_X, BUBBLE_OFFSET_Y = DEFAULT_BUBBLE_OFFSET_X, DEFAULT_BUBBLE_OFFSET_Y
        EMOTION_MAP = normalize_emotion_map(DEFAULT_EMOTIONS)
        PROACTIVE_PROMPTS = DEFAULT_PROACTIVE_PROMPTS.copy()
        PROACTIVE_TEMPLATE = DEFAULT_PROACTIVE_TEMPLATE
        RANDOM_WINDOW_MIN_SECONDS = SETTINGS["proactive_check_period_min"]
        RANDOM_WINDOW_MAX_SECONDS = SETTINGS["proactive_check_period_max"]
        RANDOM_WINDOW_PROBABILITY = SETTINGS["proactive_probability"]

        self.current_emotion = DEFAULT_EMOTION
        self.load_sprite_sheet(None, DEFAULT_SPRITE_WIDTH, DEFAULT_SPRITE_HEIGHT)
        self._relayout()
        self.update_sprite_anchor()

    def activate_profile_for_character(self, char_name):
        """Loads the profile matching char_name if one exists, else the
        defaults. Safe to call repeatedly -- it's a no-op if that character
        is already active.

        Gated by AUTO_SWITCH_PROFILE_ON_SELECT, with one exception: the very
        first character resolution after launch always goes ahead, so a
        matching profile (or the defaults) is still tried automatically at
        startup even when auto-switching is turned off. After that, further
        switches only happen automatically if the setting is on; otherwise
        use 'Load Profile' to switch manually."""
        if char_name and self.active_profile_name == char_name:
            return

        allow_auto = (not self._startup_profile_resolved) or AUTO_SWITCH_PROFILE_ON_SELECT
        if not allow_auto:
            logger.info(
                f"Auto-switch profile is off; leaving the current profile loaded for the "
                f"new character '{char_name}'. Use 'Load Profile' to switch manually."
            )
            return

        found = find_profile_for_character(char_name) if char_name else None
        if found:
            profile_path, data = found
            logger.info(f"Loading profile '{profile_path.name}' for character '{char_name}'.")
            self.apply_profile(profile_path, data)
        else:
            if char_name:
                logger.info(f"No profile found for '{char_name}'; loading defaults.")
            self.apply_defaults()
        self.active_profile_name = char_name
        self._startup_profile_resolved = True

    def load_profile_manually(self, json_path):
        """Applies a specific profile file directly, regardless of which
        character is currently active in GobboNet. Used by the 'Load
        Profile' menu -- the manual counterpart to auto-switching."""
        try:
            data = read_profile_json(json_path)
        except Exception as e:
            logger.error(f"Could not read profile {json_path}: {e}")
            self.set_speech_bubble(f"[Could not read profile: {e}]")
            return
        self.apply_profile(json_path, data)
        self.active_profile_name = data.get("character_name") or Path(json_path).stem
        self._startup_profile_resolved = True
        logger.info(f"Manually loaded profile '{Path(json_path).name}'.")

    def sync_character_list(self):
        """Re-pulls the character list from the GobboNet server, in case it
        changed outside of this hidden window (e.g. a card was added or
        renamed through the main GobboNet UI elsewhere)."""
        try:
            GOBBO_BRIDGE.refresh_character_state()
            logger.info("Character list synced from GobboNet.")
            self.set_speech_bubble("Character list synced.")
        except Exception as error:
            logger.error(f"Could not sync character list: {error}")
            self.set_speech_bubble(f"[Sync failed: {error}]")

    def open_settings_window(self):
        SettingsWindow(self)

    def open_profile_editor(self):
        ProfileEditorWindow(self, initial_character_name=BUDDY_NAME)

    # ============================================================
    # WINDOW DRAGGING
    # ============================================================
    def on_press(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._is_dragging = False

    def on_drag(self, event):
        if abs(event.x - self._drag_start_x) > 3 or abs(event.y - self._drag_start_y) > 3:
            self._is_dragging = True
            x = self.winfo_x() + event.x - self._drag_start_x
            y = self.winfo_y() + event.y - self._drag_start_y
            self.geometry(f"+{x}+{y}")

    def on_release(self, event):
        if self._is_dragging:
            # No more snap-back-into-view: the window used to get clamped to
            # the screen based on its full (often mostly-transparent)
            # bounding box, which yanked the buddy back even when the actual
            # visible art was nowhere near the edge. Simplest fix is to just
            # not clamp -- if it's ever dragged somewhere inconvenient,
            # restarting the app re-centers it (see __init__/center_on_screen).
            self.update_sprite_anchor()
            self._is_dragging = False
        else:
            self.open_prompt_dialog()

    def update_sprite_anchor(self):
        """Records the sprite's CURRENT absolute on-screen position as the
        fixed point everything else (window resizes from the bubble
        growing/shrinking, profile/sprite-size changes) gets measured
        against, so the sprite itself doesn't visibly drift."""
        self.update_idletasks()
        self._sprite_anchor_x = self.winfo_x() + self.sprite_label.winfo_x()
        self._sprite_anchor_y = self.winfo_y() + self.sprite_label.winfo_y()

    def center_on_screen(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        ww = self.winfo_width()
        wh = self.winfo_height()
        self.geometry(f"+{sw // 2 - ww // 2}+{sh // 2 - wh // 2}")

    # ============================================================
    # SPEECH BUBBLE
    # ============================================================
    def on_text_mousewheel(self, event):
        self.bubble_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def set_speech_bubble(self, text):
        self._bubble_visible = bool(text.strip())
        if self._bubble_visible:
            self.bubble_text.config(state="normal")
            self.bubble_text.delete("1.0", tk.END)
            self.bubble_text.insert("1.0", text)

            # An unmapped Text widget has no resolved pixel width to wrap
            # against yet, and can report a bogus (often huge) wrapped-line
            # count as a result. Make sure it's actually mapped on screen at
            # least once before asking it to count display-lines below.
            if not self.bubble_frame.winfo_ismapped():
                self.bubble_frame.place(x=0, y=0)
            self.update_idletasks()

            num_lines = self.bubble_text.count("1.0", "end-1c", "displaylines")
            lines = (num_lines[0] if num_lines else 1) + 1
            self.bubble_text.config(height=min(lines, MAX_BUBBLE_LINES), state="disabled")
        self._relayout()

    # ============================================================
    # LAYOUT
    # ============================================================
    # Tail is a fixed 24x12px triangle canvas; kept as constants here since
    # they must match how self.tail_canvas was constructed in __init__.
    _TAIL_W = 24
    _TAIL_H = 12

    def _relayout(self):
        """Positions sprite_label, tail_canvas, and bubble_frame with
        place() (not pack()), applying the profile's BUBBLE_OFFSET_X/Y, then
        resizes+repositions the actual window so the sprite's on-screen
        anchor position is preserved (see update_sprite_anchor).

        Everything is computed in a virtual coordinate space where the
        sprite's own (un-shifted) top-left corner is (0, 0); the bubble/tail
        can extend to negative x/y (left of / above the sprite) or beyond
        its right/bottom edge depending on the offset and bubble size. The
        overall bounding box of sprite + tail + bubble then becomes the
        window's actual size, with a margin added wherever that box dips
        below (0, 0) so nothing is placed at a negative, invisible coordinate.
        """
        self.update_idletasks()

        sprite_w = max(1, SPRITE_WIDTH)
        sprite_h = max(1, SPRITE_HEIGHT)

        sprite_x0, sprite_y0 = 0, 0
        sprite_x1, sprite_y1 = sprite_w, sprite_h

        bubble_visible = bool(self._bubble_visible)
        if bubble_visible:
            bubble_w = max(1, self.bubble_frame.winfo_reqwidth())
            bubble_h = max(1, self.bubble_frame.winfo_reqheight())
            ox, oy = BUBBLE_OFFSET_X, BUBBLE_OFFSET_Y

            tail_x0 = (sprite_w - self._TAIL_W) / 2 + ox
            tail_y0 = -self._TAIL_H + oy
            tail_x1 = tail_x0 + self._TAIL_W
            tail_y1 = tail_y0 + self._TAIL_H

            bubble_x0 = (sprite_w - bubble_w) / 2 + ox
            bubble_y0 = tail_y0 - bubble_h
            bubble_x1 = bubble_x0 + bubble_w
            bubble_y1 = bubble_y0 + bubble_h

            all_x = [sprite_x0, sprite_x1, tail_x0, tail_x1, bubble_x0, bubble_x1]
            all_y = [sprite_y0, sprite_y1, tail_y0, tail_y1, bubble_y0, bubble_y1]
        else:
            all_x = [sprite_x0, sprite_x1]
            all_y = [sprite_y0, sprite_y1]

        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)

        margin_left = -min_x if min_x < 0 else 0
        margin_top = -min_y if min_y < 0 else 0
        total_w = max(1, int(round(max_x - min_x)))
        total_h = max(1, int(round(max_y - min_y)))

        sprite_local_x = int(round(sprite_x0 + margin_left))
        sprite_local_y = int(round(sprite_y0 + margin_top))
        self.sprite_label.place(x=sprite_local_x, y=sprite_local_y)

        if bubble_visible:
            self.tail_canvas.place(
                x=int(round(tail_x0 + margin_left)), y=int(round(tail_y0 + margin_top))
            )
            #self.tail_canvas.lift(self.sprite_label)
            self.bubble_frame.place(
                x=int(round(bubble_x0 + margin_left)), y=int(round(bubble_y0 + margin_top))
            )
            #self.bubble_frame.lift(self.tail_canvas)
            #self.sprite_label.lower()
            self.bubble_frame.lift()
            #self.tail_canvas.lift()
        else:
            self.tail_canvas.place_forget()
            self.bubble_frame.place_forget()

        # Keep the sprite's absolute screen position fixed at the existing
        # anchor (if we have one yet -- not true on the very first call
        # during __init__, before center_on_screen has run).
        if self._sprite_anchor_x is not None:
            new_win_x = self._sprite_anchor_x - sprite_local_x
            new_win_y = self._sprite_anchor_y - sprite_local_y
        else:
            new_win_x = self.winfo_x()
            new_win_y = self.winfo_y()

        self.geometry(f"{total_w}x{total_h}+{new_win_x}+{new_win_y}")

    # ============================================================
    # USER INPUT
    # ============================================================
    def open_prompt_dialog(self):
        if not GOBBO_BRIDGE.state_ready_event.is_set():
            self.set_speech_bubble("Still loading...")
            return
        dialog = TopmostAskStringDialog(self, BUDDY_NAME or "GobboBuddy", "Say something:")
        user_text = dialog.result_text
        if user_text:
            self.send_to_gobbonet(user_text)

    def send_to_gobbonet(self, prompt_text):
        if not GOBBO_BRIDGE.state_ready_event.is_set():
            self.set_speech_bubble("Not ready yet.")
            return

        self.raw_stream_text = ""
        self.parsed_emotion = None
        self.update_sprite("curious")
        self.set_speech_bubble("...")
        self._note_message_exchanged()

        threading.Thread(target=self._gobbo_worker, args=(prompt_text,), daemon=True).start()

    def _gobbo_worker(self, prompt_text):
        try:
            content = GOBBO_BRIDGE.send_message(prompt_text)
            emotion = classify_emotion_with_direct_gguf(content)
            clean = content.strip()
            if clean.startswith("[") and "]" in clean[:20]:
                clean = clean.split("]", 1)[1].lstrip()
            self.response_queue.put({"emotion": emotion, "text": clean})
        except Exception as error:
            logger.exception("Worker failed.")
            self.response_queue.put({
                "emotion": DEFAULT_EMOTION,
                "text": f"[ERROR: {error}]"
            })

    def check_queue(self):
        while not self.response_queue.empty():
            item = self.response_queue.get()
            if isinstance(item, dict):
                emotion = item.get("emotion", DEFAULT_EMOTION)
                text = item.get("text", "")
            else:
                emotion = DEFAULT_EMOTION
                text = str(item)

            self.parsed_emotion = emotion
            self.update_sprite(emotion)
            self.set_speech_bubble(text)
            self._note_message_exchanged()
        self.after(50, self.check_queue)

    # ============================================================
    # CONTEXT MENU
    # ============================================================
    def show_context_menu(self, event):
        menu = tk.Menu(self, tearoff=False)
        character_menu = tk.Menu(menu, tearoff=False)

        try:
            if not GOBBO_BRIDGE.state_ready_event.is_set():
                character_menu.add_command(label="Still loading...", state="disabled")
            else:
                character_menu.add_command(
                    label="\u21bb Sync Character List", command=self.sync_character_list
                )
                character_menu.add_separator()
                cards = GOBBO_BRIDGE.get_character_cards()
                if cards:
                    for card in cards:
                        card_id = card.get("id")
                        card_name = card.get("name", "Unnamed")
                        has_profile = find_profile_for_character(card_name) is not None
                        label = f"{card_name}  \u2713" if has_profile else card_name
                        character_menu.add_command(
                            label=label,
                            command=lambda cid=card_id, cname=card_name: self.select_character(cid, cname)
                        )
                else:
                    character_menu.add_command(label="No characters found", state="disabled")
        except Exception as error:
            logger.error(f"Could not read characters: {error}")
            character_menu.add_command(label="Characters unavailable", state="disabled")

        profile_menu = tk.Menu(menu, tearoff=False)
        profiles = list_profiles()
        if profiles:
            for name, jpath in profiles:
                profile_menu.add_command(
                    label=name,
                    command=lambda p=jpath: self.load_profile_manually(p)
                )
        else:
            profile_menu.add_command(label="No profiles yet", state="disabled")

        menu.add_cascade(label="Character", menu=character_menu)
        menu.add_cascade(label="Load Profile", menu=profile_menu)
        menu.add_separator()
        menu.add_command(label="New Thread", command=self.new_thread)
        menu.add_command(label="Stop Generation", command=self.stop_generation)
        menu.add_separator()
        if HAS_ACCESSIBILITY:
            menu.add_checkbutton(
                label="Enable Proactive",
                variable=self.proactive_var,
                command=self.toggle_proactive
            )
        menu.add_checkbutton(
            label="Auto-switch Profile on Character Select",
            variable=self.auto_switch_var,
            command=self.toggle_auto_switch_profile
        )
        menu.add_separator()
        menu.add_command(label="Resize Sprite… (session only)", command=self.change_sprite_size)
        menu.add_command(label="Change Sprite Sheet… (session only)", command=self.change_sprite_sheet)
        menu.add_separator()
        menu.add_command(label="Edit / Create Profile…", command=self.open_profile_editor)
        menu.add_command(label="Settings…", command=self.open_settings_window)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.quit_application)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ============================================================
    # CHARACTER / THREAD ACTIONS
    # ============================================================
    def select_character(self, card_id, card_name):
        try:
            global BUDDY_NAME
            BUDDY_NAME = card_name
            GOBBO_BRIDGE.activate_character(card_id)
            self.activate_profile_for_character(card_name)
            self.new_thread()
        except Exception as error:
            logger.error(f"Character activation blocked: {error}")
            self.set_speech_bubble(f"[Safety lock: {error}]")

    def new_thread(self):
        try:
            GOBBO_BRIDGE.create_new_thread()
            self.set_speech_bubble("")
            self.update_sprite("neutral")
            self._note_message_exchanged()
        except Exception as error:
            logger.error(f"New thread blocked: {error}")
            self.set_speech_bubble(f"[Safety lock: {error}]")

    def stop_generation(self):
        try:
            GOBBO_BRIDGE.stop_generation()
        except Exception as error:
            logger.error(f"Could not stop generation: {error}")

# ============================================================
# MAIN
# ============================================================
def run_app():
    app = GobboNetHelper()
    app.mainloop()

if __name__ == "__main__":
    tk_thread = threading.Thread(target=run_app, daemon=True)
    tk_thread.start()

    try:
        GOBBO_BRIDGE.start_window()
    except Exception as error:
        logger.error(f"STARTUP ERROR: {error}")
