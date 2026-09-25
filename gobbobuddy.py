import os
import json
import queue
import threading
import logging
import tkinter as tk
from tkinter import simpledialog, filedialog
import time
import random
import requests
from pathlib import Path
from PIL import Image, ImageTk
import webview
from screeninfo import get_monitors

# Optional Accessibility Stack
try:
    import uiautomation as auto
    import win32gui
    import win32process
    import psutil
    HAS_ACCESSIBILITY = True
except ImportError:
    HAS_ACCESSIBILITY = False

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("GobboBuddy")

# ============================================================
# PATHS & DEFAULTS  (shippable – all character flavour lives here)
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "gobbo_buddy_config.json"

DEFAULT_SPRITE_SHEET = "gobbo sprites 2.png"
DEFAULT_SPRITE_WIDTH = 120
DEFAULT_SPRITE_HEIGHT = 120
DEFAULT_ROWS = 3
DEFAULT_COLS = 4

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

DEFAULT_EMOTION = "neutral"
TRANSPARENT_COLOR = "#15181D"
MAX_BUBBLE_LINES = 30

# Character-specific defaults (Fumo / goblin theme)
BUDDY_NAME = ""

DEFAULT_PROACTIVE_PROMPTS = [
    "What do the goblin warriors do when they encounter this kind of thing?",
    "What would a goblin warrior think about this?",
    "Does this remind you of the wars?",
    "Does anything here look suspicious?",
    "How would you conquer this task?",
    "What does this remind you of?",
    "What weapon would you choose here?",
    "There is a mighty dragon here.",
    "It is office work! Oh my!",
    "Is there anything here worth looting?",
    "What kind of dark magic forced this onto the screen?",
    "This could be a bandit hideout. Prepare for battle.",
    "How do you think this would taste?",
    "Can we throw a rock at this?",
    "Does this look like a good spot to stop and take a nap?",
    "What terrible curse brought this awful sight before us?",
    "Call the horde to smash whatever is happening here.",
    "Is there any beer nearby to help us with this?",
    "How does this situation smell to your sharp goblin nose?",
    "How can we sabotage it?",
    "Could this be a map to a hidden dungeon?",
    "Can we trade this to an ogre for a chicken leg?",
    "What would the warlocks say about this?",
    "It is from the ancient spellbooks of the high elves.",
    "This reminds me of the goblin wars.",
    "How did the goblin wizards handle these in the past?",
    "Is it a trap?",
    "What kind of potion do we need for this?",
    "Is it an omen?",
    "What kind of potion could we make with this?",
    "How did the clan chief instruct us to handle this?",
    "How did you handle this last time you were in the woods?",
    "RAAAAAAARRRRGH!!!! Arm yourself! It's about to attack!",
    "Should we handle this the sneaky way, or attack it head on?"
]

# {app_name}, {what}, {summary}, {prompt} are substituted
DEFAULT_PROACTIVE_TEMPLATE = (
    "I'm using '{app_name}', looking at '{what}'. "
    "The page shows things like ['{summary}']. \n\n{prompt}"
)

# Proactive timing (still global – rarely needs per-character change)
RANDOM_WINDOW_MIN_SECONDS = 45
RANDOM_WINDOW_MAX_SECONDS = 180
RANDOM_WINDOW_PROBABILITY = 0.35
DEFAULT_MAX_RAW_CONTENT_CHARS = 1800
DEFAULT_PROACTIVE_ENABLED = True
MAX_SUMMARY_CHARS = 180

# GobboNet connection
GOBBONET_BASE_URL = "http://127.0.0.1:9066"
LLM_DIRECT_BASE = "http://127.0.0.1:11437"
LLM_DIRECT_TIMEOUT = 45
GENERATION_TIMEOUT = 600
PAGE_READY_TIMEOUT = 60

# ============================================================
# CONFIG LOAD / SAVE
# ============================================================
def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Ensure every key exists (forward-compatible)
            cfg.setdefault("sprite_sheet", DEFAULT_SPRITE_SHEET)
            cfg.setdefault("sprite_width", DEFAULT_SPRITE_WIDTH)
            cfg.setdefault("sprite_height", DEFAULT_SPRITE_HEIGHT)
            cfg.setdefault("rows", DEFAULT_ROWS)
            cfg.setdefault("cols", DEFAULT_COLS)
            cfg.setdefault("emotions", DEFAULT_EMOTIONS.copy())
            cfg.setdefault("proactive_prompts", DEFAULT_PROACTIVE_PROMPTS.copy())
            cfg.setdefault("proactive_template", DEFAULT_PROACTIVE_TEMPLATE)
            cfg.setdefault("proactive_enabled", DEFAULT_PROACTIVE_ENABLED)
            cfg.setdefault("max_raw_content_chars", DEFAULT_MAX_RAW_CONTENT_CHARS)
            return cfg
        except Exception as e:
            logger.warning(f"Could not read config, using defaults: {e}")
    else:
        logger.warning(f"NO CONFIG FOUND. One will be created. You should definitely change the contents.")

    # First run – write complete defaults
    cfg = {
        "SPRITE_COMMENT": "Change sprite_sheet to the sheet you want to load at start.",
        "sprite_sheet": DEFAULT_SPRITE_SHEET,
        "SPRITE_HW_COMMENT": "Sprites are captured based on row/col count, and then scaled to these pixel dimensions:",
        "sprite_width": DEFAULT_SPRITE_WIDTH,
        "sprite_height": DEFAULT_SPRITE_HEIGHT,
        "ROW_COL_COMMENT": "This is how many rows and columns your sprite sheet has. They should be evenly spaced.",
        "rows": DEFAULT_ROWS,
        "cols": DEFAULT_COLS,
        "EMOTION_COMMENT": "Here you can register emotion words by sprite sheet row/column. It's ok to have many words for the same coordinate." ,
        "emotions": DEFAULT_EMOTIONS.copy(),
        "PROACT_PROMPT_COMMENT": "These prompts are passed to the model with screen/accessibility data, so the model can say something interesting about what you're doing. A line is chosen at random each time. Add/remove/change them at will.",
        "proactive_prompts": DEFAULT_PROACTIVE_PROMPTS.copy(),
        "PROACT_TMPLT_COMMENT": "This is the format for accessibility data sent to the model for summary.",
        "proactive_template": DEFAULT_PROACTIVE_TEMPLATE,
        "PROACT_ENABLE_COMMENT": "If you don't want this program reading your screen, just set proactive_enabled to false.",
        "proactive_enabled": DEFAULT_PROACTIVE_ENABLED,
        "RAW_CONTENT_COMMENT": "I am testing this with a very limited model, so I truncate the {what} part of the accessibility date before sending it. Set this to 0 to disable truncation.",
        "max_raw_content_chars": DEFAULT_MAX_RAW_CONTENT_CHARS,
    }
    save_config(cfg)
    logger.info(f"Created default config at {CONFIG_PATH}")
    return cfg

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not save config: {e}")

CONFIG = load_config()

# Live values (updated when user changes them via the menu)
SPRITE_SHEET_PATH = CONFIG["sprite_sheet"]
SPRITE_WIDTH = CONFIG["sprite_width"]
SPRITE_HEIGHT = CONFIG["sprite_height"]
ROWS = CONFIG["rows"]
COLS = CONFIG["cols"]
EMOTION_MAP = CONFIG["emotions"]
PROACTIVE_PROMPTS = CONFIG["proactive_prompts"]
PROACTIVE_TEMPLATE = CONFIG["proactive_template"]
PROACTIVE_ENABLED = CONFIG.get("proactive_enabled", DEFAULT_PROACTIVE_ENABLED)
MAX_RAW_CONTENT_CHARS = CONFIG.get("max_raw_content_chars", DEFAULT_MAX_RAW_CONTENT_CHARS)

def build_emotion_tag_list(emotions: dict) -> str:
    """String injected into the classifier prompt."""
    parts = []
    for tag, (r, c) in emotions.items():
        parts.append(f"[{tag.upper()}] (grid {r},{c})")
    return " | ".join(parts)

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

            state = self.wait_for_gobbonet_state(timeout=PAGE_READY_TIMEOUT)
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

    def _wait_for_ui(self, timeout=PAGE_READY_TIMEOUT):
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

    def wait_for_gobbonet_state(self, timeout=PAGE_READY_TIMEOUT):
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
        "max_tokens": 150,
        "temperature": 0.3
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
    prompt = (
        f"It is fun for RPG characters to show dramatic feelings with their body language when they speak, like these:\n"
        "{tag_list}\n"
        "Which of the listed emotions would be interesting to see in an RPG character saying this:\n\n\"\"\"\n{text}\n\"\"\"\n\n"        
        )

    #test prompts... here for me to switch between them and try things out.
    prompt2 = (#pretty good; snarky too often
        f"You overhear someone saying this:\n\n[[[\n{text}\n]]]\n\n"
        "What emotion was the speaker pretending to feel? Choose one of these:\n"
        "{tag_list}"
        )
    
    prompt1 = (#happy too often
        "You are an emotion classifier. You output emotion tags to improve immersion for a video game.\n\n"
        #"Do NOT judge whether the message is good, bad, funny, or polite.\n"
        #"Do NOT assume the speaker is happy just because they are talking conversationally.\n"
        "You support the following tags: \n\n{tag_list}"
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
        f"\n\nAn NPC is saying the following message. You must assign the NPC a facial expression by outputting an emotion tag. This is the message:\nNPC: '''{text[:1200]}'''\n\n"
    )
    #print(text[:1200])

    grammar_parts = [f'"[{tag.upper()}]"' for tag in EMOTION_MAP]
    grammar = "root ::= " + " | ".join(grammar_parts)

    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 12,
        "temperature": 0.1,
        "grammar": grammar,
    }

    try:
        r = requests.post(
            f"{LLM_DIRECT_BASE}/v1/chat/completions",
            json=payload,
            timeout=LLM_DIRECT_TIMEOUT
        )
        raw = r.json()["choices"][0]["message"]["content"].strip()
        print(f"Classifier: {raw}")
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
# SPRITE RESIZE DIALOG HELPER
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

# ============================================================
# TKINTER UI
# ============================================================
class GobboNetHelper(tk.Tk):

    def __init__(self):
        super().__init__()

        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        self.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.configure(bg=TRANSPARENT_COLOR)

        self.proactive_var = tk.BooleanVar(value=PROACTIVE_ENABLED)

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

        self._last_message_time = time.time()
        self._next_opportunity_at = None
        self._schedule_next_opportunity()

        self._proactive_lock = threading.Lock()
        self._proactive_busy = False

        self.container = tk.Frame(self, bg=TRANSPARENT_COLOR)
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
            bg=TRANSPARENT_COLOR, highlightthickness=0, bd=0
        )
        self.tail_canvas.create_polygon(2, 0, 22, 0, 12, 11, fill="white", outline="black", width=2)
        self.tail_canvas.create_line(3, 0, 21, 0, fill="white", width=3)

        # Sprite
        self.sprite_label = tk.Label(self.container, bg=TRANSPARENT_COLOR, bd=0, cursor="fleur")
        self.sprite_label.pack()

        self.current_emotion = DEFAULT_EMOTION

        # Load sprites with explicit width and height
        self.load_sprite_sheet()

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
    # PROACTIVE
    # ============================================================
    def _schedule_next_opportunity(self):
        delay = random.uniform(RANDOM_WINDOW_MIN_SECONDS, RANDOM_WINDOW_MAX_SECONDS)
        self._next_opportunity_at = time.time() + delay
        print(f"Proactive: next opportunity in {delay:.1f}s")

    def _note_message_exchanged(self):
        self._last_message_time = time.time()
        self._schedule_next_opportunity()

    def _sync_buddy_name_from_bridge(self):
        """Pick up the auto-detected active character as BUDDY_NAME, once available."""
        global BUDDY_NAME
        if not GOBBO_BRIDGE.state_ready_event.is_set():
            return
        detected = GOBBO_BRIDGE.active_character_name
        if detected and BUDDY_NAME != detected:
            BUDDY_NAME = detected
            logger.info(f"BUDDY_NAME auto-set to '{BUDDY_NAME}' from active GobboNet character.")

    def toggle_proactive(self):
        global PROACTIVE_ENABLED
        PROACTIVE_ENABLED = self.proactive_var.get()
        CONFIG["proactive_enabled"] = PROACTIVE_ENABLED
        save_config(CONFIG)
        logger.info(f"Proactive mode set to: {PROACTIVE_ENABLED}")

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
    def load_sprite_sheet(self, path=None, width=None, height=None):
        global SPRITE_SHEET_PATH, SPRITE_WIDTH, SPRITE_HEIGHT, ROWS, COLS, EMOTION_MAP

        path = path or SPRITE_SHEET_PATH
        width = width or SPRITE_WIDTH
        height = height or SPRITE_HEIGHT

        sheet_path = Path(path)
        if not sheet_path.is_absolute():
            sheet_path = SCRIPT_DIR / sheet_path

        if not sheet_path.exists():
            logger.warning(f"Sprite sheet not found: {sheet_path}")
            placeholder = Image.new("RGBA", (width, height), color=(200, 200, 200))
            self._raw_sheet = placeholder
            self.placeholder_img = ImageTk.PhotoImage(placeholder)
            for emotion in EMOTION_MAP:
                self.sprites[emotion] = self.placeholder_img
            return

        sheet = Image.open(sheet_path).convert("RGBA")
        self._raw_sheet = sheet
        SPRITE_SHEET_PATH = str(path)
        SPRITE_WIDTH = width
        SPRITE_HEIGHT = height

        self._rebuild_sprites()

    def _rebuild_sprites(self):
        if self._raw_sheet is None:
            return

        sw, sh = self._raw_sheet.size
        sp_w = sw // COLS
        sp_h = sh // ROWS

        self.sprites.clear()
        for emotion, (r, c) in EMOTION_MAP.items():
            cropped = self._raw_sheet.crop(
                (c * sp_w, r * sp_h, (c + 1) * sp_w, (r + 1) * sp_h)
            )
            self.sprites[emotion] = ImageTk.PhotoImage(
                cropped.resize((SPRITE_WIDTH, SPRITE_HEIGHT), Image.Resampling.LANCZOS)
            )

        self.update_sprite(self.current_emotion)

    def update_sprite(self, emotion):
        emotion = emotion.lower().strip()
        self.current_emotion = emotion if emotion in self.sprites else DEFAULT_EMOTION
        if self.current_emotion in self.sprites:
            self.sprite_label.config(image=self.sprites[self.current_emotion])

    def change_sprite_size(self):
        global SPRITE_WIDTH, SPRITE_HEIGHT
        dialog = SpriteSizeDialog(self, SPRITE_WIDTH, SPRITE_HEIGHT)
        if hasattr(dialog, "result_w") and hasattr(dialog, "result_h"):
            new_w = max(32, min(512, dialog.result_w))
            new_h = max(32, min(512, dialog.result_h))
            if new_w != SPRITE_WIDTH or new_h != SPRITE_HEIGHT:
                SPRITE_WIDTH = new_w
                SPRITE_HEIGHT = new_h
                CONFIG["sprite_width"] = new_w
                CONFIG["sprite_height"] = new_h
                save_config(CONFIG)
                self._rebuild_sprites()
                self.update_sprite_anchor()
                self.reposition_window_to_anchor()
                logger.info(f"Sprite size set to {new_w}x{new_h}px")

    def change_sprite_sheet(self):
        path = filedialog.askopenfilename(
            title="Select Sprite Sheet",
            filetypes=[("PNG images", "*.png"), ("All files", "*.*")],
            initialdir=SCRIPT_DIR
        )
        if not path:
            return
        try:
            rel = Path(path).relative_to(SCRIPT_DIR)
            store_path = str(rel)
        except ValueError:
            store_path = path

        global SPRITE_SHEET_PATH
        SPRITE_SHEET_PATH = store_path
        CONFIG["sprite_sheet"] = store_path
        save_config(CONFIG)
        self.load_sprite_sheet(path=store_path, width=SPRITE_WIDTH, height=SPRITE_HEIGHT)
        self.update_sprite_anchor()
        self.reposition_window_to_anchor()
        logger.info(f"Sprite sheet changed to {store_path}")

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
            self.clamp_to_screen_bounds()
            self.update_sprite_anchor()
            self._is_dragging = False
        else:
            self.open_prompt_dialog()

    def update_sprite_anchor(self):
        self.update_idletasks()
        self._sprite_anchor_x = self.winfo_x() + self.sprite_label.winfo_x()
        self._sprite_anchor_y = self.winfo_y() + self.sprite_label.winfo_y()

    def reposition_window_to_anchor(self):
        if self._sprite_anchor_x is None:
            return
        self.update_idletasks()
        new_x = self._sprite_anchor_x - self.sprite_label.winfo_x()
        new_y = self._sprite_anchor_y - self.sprite_label.winfo_y()
        self.geometry(f"+{new_x}+{new_y}")

    def clamp_to_screen_bounds(self):
        self.update_idletasks()
        ww = self.winfo_width()
        wh = self.winfo_height()
        wx = self.winfo_x()
        wy = self.winfo_y()

        monitors = get_monitors()
        target_monitor = monitors[0]
        for monitor in monitors:
            if (monitor.x <= wx < monitor.x + monitor.width) and \
               (monitor.y <= wy < monitor.y + monitor.height):
                target_monitor = monitor
                break

        clamped_x = max(target_monitor.x, min(wx, target_monitor.x + target_monitor.width - ww))
        clamped_y = max(target_monitor.y, min(wy, target_monitor.y + target_monitor.height - wh))
        self.geometry(f"+{clamped_x}+{clamped_y}")

    def center_on_screen(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{sw // 2 - 65}+{sh // 2 - 85}")

    # ============================================================
    # SPEECH BUBBLE
    # ============================================================
    def on_text_mousewheel(self, event):
        self.bubble_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def set_speech_bubble(self, text):
        if not text.strip():
            self.bubble_frame.pack_forget()
            self.tail_canvas.pack_forget()
            self.reposition_window_to_anchor()
            return

        self.bubble_frame.pack(side="top", pady=0, before=self.sprite_label)
        self.tail_canvas.pack(side="top", pady=0, before=self.sprite_label)

        self.bubble_text.config(state="normal")
        self.bubble_text.delete("1.0", tk.END)
        self.bubble_text.insert("1.0", text)
        self.update_idletasks()

        num_lines = self.bubble_text.count("1.0", "end-1c", "displaylines")
        lines = (num_lines[0] if num_lines else 1) + 1
        self.bubble_text.config(height=min(lines, MAX_BUBBLE_LINES), state="disabled")
        self.reposition_window_to_anchor()

    # ============================================================
    # USER INPUT
    # ============================================================
    def open_prompt_dialog(self):
        if not GOBBO_BRIDGE.state_ready_event.is_set():
            self.set_speech_bubble("Still loading...")
            return
        user_text = simpledialog.askstring(BUDDY_NAME, "Say something:")
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
                cards = GOBBO_BRIDGE.get_character_cards()
                if cards:
                    for card in cards:
                        card_id = card.get("id")
                        card_name = card.get("name", "Unnamed")
                        character_menu.add_command(
                            label=card_name,
                            command=lambda cid=card_id, cname=card_name: self.select_character(cid,cname)
                        )
                else:
                    character_menu.add_command(label="No characters found", state="disabled")
        except Exception as error:
            logger.error(f"Could not read characters: {error}")
            character_menu.add_command(label="Characters unavailable", state="disabled")

        menu.add_cascade(label="Character", menu=character_menu)
        menu.add_separator()
        menu.add_command(label="New Thread", command=self.new_thread)
        menu.add_command(label="Stop Generation", command=self.stop_generation)
        menu.add_separator()
        menu.add_checkbutton(
            label="Enable Proactive",
            variable=self.proactive_var,
            command=self.toggle_proactive
        )
        menu.add_separator()
        menu.add_command(label="Resize Sprite…", command=self.change_sprite_size)
        menu.add_command(label="Change Sprite Sheet…", command=self.change_sprite_sheet)
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
