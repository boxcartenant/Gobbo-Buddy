import os
import json
import queue
import threading
import logging
import tkinter as tk
from tkinter import simpledialog
import time
import traceback
import random
import requests
from pathlib import Path
from PIL import Image, ImageTk
import webview
import tempfile


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
# LOGGING SETUP (Prevents console pollution)
# ============================================================
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("GobboBuddy")

# We deliberately do NOT use a persistent isolated profile.
# private_mode=True makes the WebView start empty every launch so
# GobboNet's own boot logic auto-restores character cards / personas /
# threads from the server /state backup. This prevents the buddy from
# ever overwriting the real data.
# (The old CLEAN_BUDDY_STORAGE_ON_START + rmtree approach was dangerous
#  because a partial wipe + push race could still wipe the server.)

# ============================================================
# CONFIGURATION
# ============================================================
#GOBBONET_WEBVIEW_PROFILE = (
#    Path.home() / "GobboBuddy" / "gobbonet_webview_profile"
#)
#GOBBONET_WEBVIEW_PROFILE.mkdir(parents=True, exist_ok=True)

SPRITE_SHEET_PATH = "gobbo sprites 2.png"
MAX_BUBBLE_LINES = 30
ROWS, COLS = 3, 4
SPRITE_SIZE_X = 120
SPRITE_SIZE_Y = 120

EMOTION_MAP = {
    "neutral": (0, 0),
    "happy": (0, 1),
    "joking": (0, 1),
    "curious": (0, 2),
    "snarky": (0, 3),
    "excited": (1, 0),
    "skeptical": (1, 1),
    "judging": (1, 2),
    "shocked": (1, 3),
    "sad": (2, 0),
    "anxious": (2, 1),
    "embarrassed": (2, 2),
    "flattered": (2, 3),
}

DEFAULT_EMOTION = "neutral"
TRANSPARENT_COLOR = "#15181D"

# Proactive Settings
RANDOM_WINDOW_MIN_SECONDS = 45
RANDOM_WINDOW_MAX_SECONDS = 180
RANDOM_WINDOW_PROBABILITY = 0.35
MAX_RAW_CONTENT_CHARS = 1800
MAX_SUMMARY_CHARS = 180

# GobboNet URLs & Credentials
GOBBONET_BASE_URL = "http://127.0.0.1:9066"
LLM_DIRECT_BASE = "http://127.0.0.1:11437"
LLM_DIRECT_TIMEOUT = 45
GENERATION_TIMEOUT = 600
PAGE_READY_TIMEOUT = 60

# ============================================================
# NEED THAT PASSWORD
# ============================================================
def get_password():
    # 1. Create temporary root and hide it instantly
    temp_root = tk.Tk()
    temp_root.withdraw()

    # 2. Show prompt (built-in modal window)
    password = simpledialog.askstring("Gobbonet Auth", "Enter Gobbonet password:", show="*")

    # 3. Clean up the temporary root entirely
    temp_root.destroy()
    
    return password or ""

GOBBONET_PASSWORD = get_password()

# ============================================================
# LIGHTWEIGHT OS WEBVIEW BRIDGE (replaces playwright)
# ============================================================

class GobboNetWebViewBridge:
    """
    Server-safe interface to the GobboNet Web UI.

    IMPORTANT:
    GobboNet's `state` appears to be a JavaScript global binding,
    NOT necessarily `window.state`. Therefore we access it directly
    with `typeof state !== "undefined"`.

    Safety rules:
        - Persistent browser profile.
        - Never delete the WebView profile.
        - Never treat missing state as empty state.
        - Never enable mutations until state is verified.
        - Never automatically repair/reset server state.
        - If known-good character state suddenly becomes zero,
          lock all mutations.
    """

    def __init__(self):
        self.window = None

        # --------------------------------------------------------
        # Readiness
        # --------------------------------------------------------
        self.ui_ready_event = threading.Event()
        self.state_ready_event = threading.Event()
        self.ready_event = self.state_ready_event

        # --------------------------------------------------------
        # Startup guard
        #
        # pywebview can fire "loaded" more than once.
        # Only one startup sequence is allowed.
        # --------------------------------------------------------
        self._startup_lock = threading.Lock()
        self._startup_started = False
        self._startup_complete = False

        # --------------------------------------------------------
        # Mutation safety
        # --------------------------------------------------------
        self.state_confirmed = False
        self.mutations_allowed = False

        self.safety_lock = False
        self.safety_lock_reason = None

        # --------------------------------------------------------
        # Last known-good state
        # --------------------------------------------------------
        self.last_known_character_count = None
        self.last_known_thread_count = None

        # --------------------------------------------------------
        # Serialize WebView operations.
        # --------------------------------------------------------
        self.operation_lock = threading.RLock()

    def close(self):
        """
        Close the GobboNet WebView so pywebview.start() can return.
        """

        with self.operation_lock:

            if not self.window:
                return

            try:
                self.window.destroy()
            except Exception as error:
                logger.error(
                    f"Could not destroy GobboNet WebView: {error}"
                )

            finally:
                self.window = None

        # ============================================================
    # STARTUP
    # ============================================================

    def start_window(self):
        """
        Start GobboNet with a persistent WebView profile.

        GobboBuddy NEVER deletes this profile.
        """

        #GOBBONET_WEBVIEW_PROFILE.mkdir(
        #    parents=True,
        #    exist_ok=True
        #)

        #logger.info(
        #    "Using persistent GobboNet WebView profile: "
        #    f"{GOBBONET_WEBVIEW_PROFILE}"
        #)

        self.window = webview.create_window(
            "GobboNet Engine",
            GOBBONET_BASE_URL,
            hidden=True,
            width=1024,
            height=768
        )

        self.window.events.loaded += self._on_loaded

        webview.start(
            private_mode=True#,
            #storage_path=str(GOBBONET_WEBVIEW_PROFILE)
        )

    def _on_loaded(self):
        """
        Handle page loads.

        pywebview may fire this multiple times, so only the first
        invocation is permitted to perform startup.
        """

        with self._startup_lock:

            if self._startup_started:
                logger.info(
                    "Ignoring duplicate GobboNet page-load event."
                )
                return

            self._startup_started = True

        try:
            self._attempt_login()

            self._wait_for_ui()

            self.ui_ready_event.set()

            logger.info(
                "GobboNet UI is ready; "
                "waiting for application state..."
            )

            state = self.wait_for_gobbonet_state(
                timeout=PAGE_READY_TIMEOUT
            )

            self.validate_startup_state(state)

            self.state_confirmed = True
            self.mutations_allowed = True
            self._startup_complete = True

            self.state_ready_event.set()

            logger.info(
                "GobboNet state verified. "
                "Mutations are now ENABLED."
            )

        except Exception as error:

            self.state_confirmed = False
            self.mutations_allowed = False
            self._startup_complete = False

            logger.error(
                "GobboNet startup safety validation failed: "
                f"{error}"
            )

            logger.error(
                "GobboBuddy will remain "
                "READ-ONLY/LOCKED."
            )

    def _attempt_login(self):
        """
        Attempt automatic login if the login form is present.
        """

        password_json = json.dumps(
            GOBBONET_PASSWORD
        )

        self.eval_js(f"""
            (() => {{
                const pw = document.querySelector(
                    'input[name="password"]'
                );

                if (!pw)
                    return false;

                pw.value = {password_json};

                pw.dispatchEvent(
                    new Event(
                        "input",
                        {{ bubbles: true }}
                    )
                );

                pw.dispatchEvent(
                    new Event(
                        "change",
                        {{ bubbles: true }}
                    )
                );

                const btn = document.querySelector(
                    'button[type="submit"], input[type="submit"]'
                );

                if (!btn)
                    return false;

                btn.click();
                return true;
            }})()
        """)

    # ============================================================
    # UI READINESS
    # ============================================================

    def _wait_for_ui(self, timeout=PAGE_READY_TIMEOUT):
        """
        Wait for the GobboNet chat UI.

        This only establishes that the UI exists.
        It does NOT authorize mutations.
        """

        deadline = time.time() + timeout

        while time.time() < deadline:

            try:
                ready = self.eval_js("""
                    (() => {
                        return !!(
                            document.querySelector('textarea') ||
                            document.querySelector(
                                'input[type="text"]'
                            ) ||
                            document.querySelector(
                                '[contenteditable="true"]'
                            ) ||
                            document.querySelector(
                                '#chat-input, .chat-input, ' +
                                '#message-input, .message-input'
                            )
                        );
                    })()
                """)

                if ready:
                    logger.info(
                        "GobboNet chat UI detected."
                    )
                    return

            except Exception:
                pass

            time.sleep(0.5)

        raise RuntimeError(
            "GobboNet chat UI never became available."
        )

    # ============================================================
    # GOBBONET STATE
    # ============================================================

    def get_state_summary(self):
        """
        Safely inspect GobboNet's application state.

        IMPORTANT:
            We deliberately use `state`, not `window.state`.

        `state` may be a top-level lexical/global binding created
        with `let`, `const`, or otherwise attached by GobboNet's
        application code.
        """

        return self.eval_js("""
            (() => {

                // ------------------------------------------------
                // Do NOT use window.state here.
                // ------------------------------------------------
                if (
                    typeof state === "undefined" ||
                    state === null
                ) {
                    return {
                        ok: false,
                        reason: "GobboNet global `state` unavailable"
                    };
                }

                // ------------------------------------------------
                // Character cards
                // ------------------------------------------------
                if (
                    !Array.isArray(
                        state.characterCards
                    )
                ) {
                    return {
                        ok: false,
                        reason:
                            "state.characterCards is not initialized"
                    };
                }

                // ------------------------------------------------
                // Threads
                // ------------------------------------------------
                if (
                    !Array.isArray(
                        state.threads
                    )
                ) {
                    return {
                        ok: false,
                        reason:
                            "state.threads is not initialized"
                    };
                }

                return {
                    ok: true,

                    characterCount:
                        state.characterCards.length,

                    threadCount:
                        state.threads.length,

                    activeThreadId:
                        state.activeThreadId ?? null
                };
            })()
        """)

    def wait_for_gobbonet_state(
        self,
        timeout=PAGE_READY_TIMEOUT
    ):
        """
        Wait until GobboNet exposes initialized application state.

        Missing state is NOT converted into an empty state.
        """

        deadline = time.time() + timeout

        last_reason = "unknown"

        while time.time() < deadline:

            try:

                result = self.get_state_summary()

                if (
                    result
                    and result.get("ok")
                ):
                    logger.info(
                        "GobboNet state initialized: "
                        f"{result['characterCount']} "
                        "characters, "
                        f"{result['threadCount']} "
                        "threads."
                    )

                    return result

                if result:
                    last_reason = result.get(
                        "reason",
                        "unknown"
                    )

            except Exception as error:

                last_reason = str(error)

            time.sleep(0.5)

        raise RuntimeError(
            "GobboNet state never became available. "
            f"Last reason: {last_reason}"
        )

    def validate_startup_state(self, state_info):
        """
        Validate that application state is genuinely initialized.

        We know GobboNet contains character cards, so a zero-card
        startup state is considered unsafe.
        """

        if not isinstance(
            state_info,
            dict
        ):
            raise RuntimeError(
                "GobboNet returned invalid state information."
            )

        character_count = (
            state_info.get(
                "characterCount"
            )
        )

        thread_count = (
            state_info.get(
                "threadCount"
            )
        )

        if not isinstance(
            character_count,
            int
        ):
            raise RuntimeError(
                "Invalid GobboNet character count."
            )

        if not isinstance(
            thread_count,
            int
        ):
            raise RuntimeError(
                "Invalid GobboNet thread count."
            )

        logger.info(
            "Validated GobboNet state: "
            f"{character_count} characters, "
            f"{thread_count} threads."
        )

        # --------------------------------------------------------
        # IMPORTANT:
        #
        # For your installation, zero characters is NOT treated
        # as a legitimate initial state.
        #
        # This is the emergency brake against exactly the class
        # of bug we're trying to prevent.
        # --------------------------------------------------------
        if character_count == 0:
            raise RuntimeError(
                "GobboNet reported ZERO character cards during "
                "startup. Refusing to enable mutations."
            )

        self.last_known_character_count = (
            character_count
        )

        self.last_known_thread_count = (
            thread_count
        )

    # ============================================================
    # CONTINUING STATE SAFETY
    # ============================================================

    def verify_state_still_exists(self):
        """
        Verify that the known-good GobboNet state still exists.

        If state disappears or characters suddenly go to zero,
        mutations are permanently disabled for this process.
        """

        if self.safety_lock:
            return False

        try:
            state_info = self.get_state_summary()

        except Exception as error:

            self._engage_safety_lock(
                "Could not read GobboNet state: "
                f"{error}"
            )

            return False

        if (
            not state_info
            or not state_info.get("ok")
        ):

            self._engage_safety_lock(
                "GobboNet state became unavailable: "
                f"{state_info.get('reason') if state_info else 'unknown'}"
            )

            return False

        character_count = (
            state_info["characterCount"]
        )

        thread_count = (
            state_info["threadCount"]
        )

        # --------------------------------------------------------
        # The big safety tripwire.
        # --------------------------------------------------------
        if (
            self.last_known_character_count is not None
            and self.last_known_character_count > 0
            and character_count == 0
        ):

            self._engage_safety_lock(
                "GobboNet character list unexpectedly "
                f"changed from "
                f"{self.last_known_character_count} "
                "characters to ZERO."
            )

            return False

        self.last_known_character_count = (
            character_count
        )

        self.last_known_thread_count = (
            thread_count
        )

        return True

    def _engage_safety_lock(self, reason):
        """
        Disable all mutations for the remainder of this process.

        No automatic restoration is attempted.
        """

        self.safety_lock = True
        self.safety_lock_reason = reason
        self.mutations_allowed = False

        logger.error(
            "=================================================="
        )
        logger.error(
            "GOBBONET SAFETY LOCK ENGAGED"
        )
        logger.error(
            reason
        )
        logger.error(
            "No GobboNet mutations will be attempted."
        )
        logger.error(
            "=================================================="
        )

    def assert_mutations_allowed(self):
        """
        Hard gate before every GobboNet-changing operation.
        """

        if self.safety_lock:

            raise RuntimeError(
                "GobboNet safety lock is engaged: "
                f"{self.safety_lock_reason}"
            )

        if not self.state_confirmed:

            raise RuntimeError(
                "GobboNet state has not been confirmed."
            )

        if not self.mutations_allowed:

            raise RuntimeError(
                "GobboNet mutations are disabled."
            )

        if not self.verify_state_still_exists():

            raise RuntimeError(
                "GobboNet state verification failed. "
                "Mutation blocked."
            )

    # ============================================================
    # GENERAL JS
    # ============================================================

    def eval_js(self, script):

        if not self.window:

            raise RuntimeError(
                "GobboNet WebView does not exist."
            )

        return self.window.evaluate_js(script)

    # ============================================================
    # CHARACTER READ
    # ============================================================

    def get_character_cards(self):
        """
        Read character cards from GobboNet's actual `state`.

        Missing state raises an error instead of producing [].
        """

        result = self.eval_js("""
            (() => {

                if (
                    typeof state === "undefined" ||
                    state === null
                ) {
                    return {
                        ok: false,
                        reason:
                            "GobboNet global `state` unavailable"
                    };
                }

                if (
                    !Array.isArray(
                        state.characterCards
                    )
                ) {
                    return {
                        ok: false,
                        reason:
                            "state.characterCards unavailable"
                    };
                }

                return {
                    ok: true,
                    cards:
                        state.characterCards.map(c => ({
                            id: c.id,
                            name:
                                c.name ||
                                "Unnamed"
                        }))
                };

            })()
        """)

        if (
            not result
            or not result.get("ok")
        ):

            raise RuntimeError(
                "Could not safely read GobboNet "
                "character cards: "
                +
                str(
                    result.get("reason")
                    if result
                    else "no result"
                )
            )

        return result["cards"]

    # ============================================================
    # CHARACTER ACTIVATION
    # ============================================================

    def activate_character(self, card_id):

        with self.operation_lock:

            self.assert_mutations_allowed()

            cards = self.get_character_cards()

            matching = [
                card
                for card in cards
                if card.get("id") == card_id
            ]

            if not matching:

                raise RuntimeError(
                    "Refusing to activate unknown "
                    f"character {card_id!r}."
                )

            escaped_id = json.dumps(
                card_id
            )

            result = self.eval_js(f"""
                (() => {{

                    if (
                        typeof activateCard !==
                        "function"
                    ) {{
                        throw new Error(
                            "GobboNet activateCard() "
                            "is unavailable."
                        );
                    }}

                    activateCard(
                        {escaped_id}
                    );

                    return true;

                }})()
            """)

            if not result:

                raise RuntimeError(
                    "GobboNet failed to activate "
                    "the selected character."
                )

            return True

    # ============================================================
    # NEW THREAD
    # ============================================================

    def create_new_thread(self):

        with self.operation_lock:

            self.assert_mutations_allowed()

            result = self.eval_js("""
                (() => {

                    if (
                        typeof createNewThread ===
                        "function"
                    ) {
                        return createNewThread();
                    }

                    if (
                        typeof createThread ===
                        "function"
                    ) {
                        return createThread();
                    }

                    if (
                        typeof window.createThread ===
                        "function"
                    ) {
                        return window.createThread();
                    }

                    const newBtn =
                        document.querySelector(
                            '#new-thread-btn, ' +
                            '.new-chat-btn, ' +
                            'button[title*="New"]'
                        );

                    if (newBtn) {
                        newBtn.click();
                        return true;
                    }

                    return false;

                })()
            """)

            if not result:

                raise RuntimeError(
                    "GobboNet could not create "
                    "a new thread."
                )

            return result

    # ============================================================
    # STOP GENERATION
    # ============================================================

    def stop_generation(self):

        with self.operation_lock:

            if not self.state_ready_event.is_set():
                return False

            return self.eval_js("""
                (() => {

                    if (
                        typeof stopGeneration ===
                        "function"
                    ) {
                        stopGeneration();
                        return true;
                    }

                    const btn =
                        document.querySelector(
                            "#stop-btn, " +
                            "button.stop-generation, " +
                            ".btn-stop"
                        );

                    if (btn) {
                        btn.click();
                        return true;
                    }

                    return false;

                })()
            """)


    # ============================================================
    # MESSAGE SENDING
    # ============================================================

    def send_message(self, prompt_text):
        """
        Send a message through GobboNet's own browser-side pipeline.

        GobboBuddy does NOT construct prompts from character cards,
        personas, lore, or other GobboNet state.

        GobboNet's own sendMessage() function remains the preferred path.
        The DOM path exists only as a browser-UI fallback.

        Safety:
            - GobboNet state must already be confirmed.
            - State is re-verified immediately before sending.
            - WebView operations are serialized.
        """

        with self.operation_lock:

            self.assert_mutations_allowed()

            # Safely encode the user's text as a JavaScript string literal.
            js_prompt = json.dumps(
                prompt_text,
                ensure_ascii=False
            )

            # ------------------------------------------------------------
            # IMPORTANT:
            #
            # This is deliberately NOT an f-string.
            #
            # JavaScript braces therefore do not need Python's doubled
            # {{ }} escaping, eliminating an entire class of syntax bugs.
            # ------------------------------------------------------------

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
                    // Prefer GobboNet's own active-thread accessor.
                    if (typeof getActiveThread === "function") {
                        const thread = getActiveThread();
                        return thread && Array.isArray(thread.messages)
                            ? thread.messages
                            : [];
                    }

                    // Read-only fallback into GobboNet's actual state.
                    if (
                        typeof state !== "undefined" &&
                        state &&
                        state.activeThread &&
                        Array.isArray(state.activeThread.messages)
                    ) {
                        return state.activeThread.messages;
                    }

                    if (
                        typeof state !== "undefined" &&
                        state &&
                        Array.isArray(state.threads) &&
                        state.activeThreadId
                    ) {
                        const thread = state.threads.find(
                            x => x.id === state.activeThreadId
                        );

                        if (thread && Array.isArray(thread.messages)) {
                            return thread.messages;
                        }
                    }

                    // Last-resort DOM read.
                    return Array.from(
                        document.querySelectorAll(
                            ".message, .chat-message, [data-role], .msg"
                        )
                    );
                };

                // --------------------------------------------------------
                // Wait for the chat input only if GobboNet's own
                // sendMessage() function is unavailable.
                // --------------------------------------------------------
                let input = findInput();
                let attempts = 0;

                while (
                    !input &&
                    attempts < 20 &&
                    typeof window.sendMessage !== "function" &&
                    typeof sendMessage !== "function"
                ) {
                    await new Promise(
                        resolve => setTimeout(resolve, 250)
                    );

                    input = findInput();
                    attempts++;
                }

                const beforeCount = getMsgs().length;

                // --------------------------------------------------------
                // PRIMARY PATH:
                //
                // Let GobboNet handle the message exactly as its normal
                // browser UI does.
                // --------------------------------------------------------
                if (typeof sendMessage === "function") {
                    sendMessage(PROMPT_HERE);
                }
                else if (typeof window.sendMessage === "function") {
                    window.sendMessage(PROMPT_HERE);
                }

                // --------------------------------------------------------
                // FALLBACK PATH:
                //
                // If GobboNet does not expose sendMessage(), behave like
                // an ordinary browser user typing into its chat box.
                // --------------------------------------------------------
                else if (input) {

                    input.focus();

                    if (
                        input.tagName === "TEXTAREA" ||
                        input.tagName === "INPUT"
                    ) {
                        const setter =
                            Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype,
                                "value"
                            )?.set ||
                            Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype,
                                "value"
                            )?.set;

                        if (setter) {
                            setter.call(input, PROMPT_HERE);
                        }
                        else {
                            input.value = PROMPT_HERE;
                        }
                    }
                    else {
                        input.innerText = PROMPT_HERE;
                    }

                    input.dispatchEvent(
                        new Event("input", { bubbles: true })
                    );

                    input.dispatchEvent(
                        new Event("change", { bubbles: true })
                    );

                    const sendButton =
                        document.querySelector(
                            'button[type="submit"]'
                        ) ||
                        document.querySelector("#send-btn") ||
                        document.querySelector(".send-button");

                    if (sendButton) {
                        sendButton.click();
                    }
                    else {
                        input.dispatchEvent(
                            new KeyboardEvent(
                                "keydown",
                                {
                                    key: "Enter",
                                    code: "Enter",
                                    keyCode: 13,
                                    which: 13,
                                    bubbles: true
                                }
                            )
                        );
                    }
                }
                else {
                    throw new Error(
                        "GobboNet sendMessage() was unavailable and " +
                        "no chat input could be found."
                    );
                }

                // --------------------------------------------------------
                // Poll GobboNet's conversation for the completed response.
                // --------------------------------------------------------
                const check = setInterval(() => {
                    try {
                        const generating =
                            typeof isGenerating !== "undefined"
                                ? isGenerating
                                : !!document.querySelector(
                                    ".generating, " +
                                    ".loading, " +
                                    ".spinner, " +
                                    'button[title*="Stop"]'
                                );

                        const msgs = getMsgs();

                        let lastText = "";

                        if (msgs.length > 0) {
                            const lastMsg = msgs[msgs.length - 1];

                            if (
                                typeof lastMsg === "object" &&
                                lastMsg !== null &&
                                typeof lastMsg.content === "string"
                            ) {
                                lastText = lastMsg.content;
                            }
                            else if (
                                lastMsg instanceof HTMLElement
                            ) {
                                lastText =
                                    lastMsg.innerText ||
                                    lastMsg.textContent ||
                                    "";
                            }
                        }

                        // A newly completed assistant message should cause
                        // the message collection to grow beyond its original
                        // size.
                        if (
                            !generating &&
                            msgs.length > beforeCount &&
                            lastText.trim()
                        ) {
                            clearInterval(check);

                            window.__gobboBuddyLastAssistant =
                                lastText;

                            window.__gobboBuddyDone = true;
                        }
                    }
                    catch (pollError) {
                        clearInterval(check);

                        window.__gobboBuddyError =
                            String(pollError);

                        window.__gobboBuddyDone = true;
                    }
                }, 300);
            }
            catch (error) {
                window.__gobboBuddyError = String(error);
                window.__gobboBuddyDone = true;
            }
        })();
    })();
    """

            # ------------------------------------------------------------
            # Insert the JSON-encoded prompt without using an f-string.
            # ------------------------------------------------------------
            script = script.replace(
                "PROMPT_HERE",
                js_prompt
            )

            # ------------------------------------------------------------
            # Execute the browser-side operation.
            # ------------------------------------------------------------
            self.eval_js(script)

            # ------------------------------------------------------------
            # Wait for GobboNet to finish.
            # ------------------------------------------------------------
            deadline = (
                time.time() +
                GENERATION_TIMEOUT
            )

            while time.time() < deadline:

                done = self.eval_js(
                    "window.__gobboBuddyDone"
                )

                if done:

                    error = self.eval_js(
                        "window.__gobboBuddyError"
                    )

                    if error:
                        raise RuntimeError(
                            f"GobboNet generation failed: {error}"
                        )

                    result = self.eval_js(
                        "window.__gobboBuddyLastAssistant"
                    )

                    if not result:
                        raise RuntimeError(
                            "GobboNet returned an empty response."
                        )

                    return result

                time.sleep(0.2)

            raise RuntimeError(
                "GobboNet generation timed out."
            )

    # ============================================================
    # THREAD CREATION
    # ============================================================

    def create_new_thread(self):
        """
        Create a new thread only after state has been verified.
        """

        with self.operation_lock:

            self.assert_mutations_allowed()

            result = self.eval_js("""
                (() => {

                    if (
                        typeof createNewThread ===
                        "function"
                    ) {
                        return createNewThread();
                    }

                    if (
                        typeof createThread ===
                        "function"
                    ) {
                        return createThread();
                    }

                    if (
                        window.state &&
                        typeof window.createThread ===
                        "function"
                    ) {
                        return window.createThread();
                    }

                    // DOM fallback is retained, but only after
                    // all safety gates above have passed.
                    const newBtn =
                        document.querySelector(
                            '#new-thread-btn, ' +
                            '.new-chat-btn, ' +
                            'button[title*="New"]'
                        );

                    if (newBtn) {
                        newBtn.click();
                        return true;
                    }

                    return false;
                })()
            """)

            if not result:
                raise RuntimeError(
                    "GobboNet could not create a new thread."
                )

            return result

    # ============================================================
    # GENERATION CONTROL
    # ============================================================

    def stop_generation(self):
        """
        Stopping generation is intentionally NOT considered a
        destructive data mutation, but it is still serialized with
        the WebView.
        """

        with self.operation_lock:

            if not self.state_ready_event.is_set():
                return False

            return self.eval_js("""
                (() => {

                    if (
                        typeof stopGeneration ===
                        "function"
                    ) {
                        stopGeneration();
                        return true;
                    }

                    const btn =
                        document.querySelector(
                            "#stop-btn, " +
                            "button.stop-generation, " +
                            ".btn-stop"
                        );

                    if (btn) {
                        btn.click();
                        return true;
                    }

                    return false;
                })()
            """)

GOBBO_BRIDGE = GobboNetWebViewBridge()

# ============================================================
# ACCESSIBILITY & DIRECT GGUF HELPERS
# ============================================================
def get_active_window_info():
    if not HAS_ACCESSIBILITY: return None
    try:
        with auto.UIAutomationInitializerInThread():
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd: return None
            title = win32gui.GetWindowText(hwnd) or "(no title)"
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try: proc = psutil.Process(pid); app_name = proc.name() or f"pid:{pid}"
            except Exception: app_name = f"pid:{pid}"

            focused = auto.GetFocusedControl() or auto.ControlFromHandle(hwnd)
            pieces = []
            if focused:
                name = (focused.Name or "").strip()
                ctrl_type = getattr(focused, "ControlTypeName", "") or ""
                val = getattr(focused.GetValuePattern(), "Value", "") if hasattr(focused, "GetValuePattern") else ""
                if name: pieces.append(f"[{ctrl_type}] {name}")
                if val and val != name: pieces.append(val[:MAX_RAW_CONTENT_CHARS])

            content = " | ".join(pieces).strip()[:MAX_RAW_CONTENT_CHARS]
            return {"app_name": app_name, "title": title[:300], "content": content or "(no readable content)"}
    except Exception:
        return None

def summarize_with_direct_gguf(app_name: str, title: str, content: str) -> str:
    prompt = f"Summarize desktop window in ONE short sentence.\nApp: {app_name}\nTitle: {title}\nContent: {content}"
    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 80,
        "temperature": 0.3
    }
    try:
        r = requests.post(f"{LLM_DIRECT_BASE}/v1/chat/completions", json=payload, timeout=LLM_DIRECT_TIMEOUT)
        return r.json()["choices"][0]["message"]["content"].strip()[:MAX_SUMMARY_CHARS]
    except Exception:
        return f"looking at {title or app_name}"

def classify_emotion_with_direct_gguf(text: str) -> str:
    """
    Force a tiny model to pick exactly one emotion tag for the given reply.
    Returns one of the keys in EMOTION_MAP (lowercase, no brackets).
    """
    prompt = (
        "You are an emotion classifier.\n"
        "Determine the primary emotion expressed by the speaker in the message.\n"
        #"Do NOT judge whether the message is good, bad, funny, or polite.\n"
        #"Do NOT assume the speaker is happy just because they are talking conversationally.\n"
        "The speaker in these messages is a rough goblin. He is often neutral.\n\n"

        "EMOTION DEFINITIONS:\n"
        "[NEUTRAL] = no strong emotion\n"
        "[HAPPY] = pleasure, joy, satisfaction, contentment\n"
        "[JOKING] = intentionally humorous or playful\n"
        "[CURIOUS] = wanting to know or understand something\n"
        "[SNARKY] = mocking, sarcastic, or derisive\n"
        "[EXCITED] = strong enthusiasm or eager anticipation\n"
        "[SKEPTICAL] = doubt, disbelief, or suspicion\n"
        "[JUDGING] = disapproval, criticism, or condemnation\n"
        "[SHOCKED] = sudden surprise or astonishment\n"
        "[SAD] = sorrow, grief, disappointment, or misery\n"
        "[ANXIOUS] = fear, worry, dread, or concern about danger\n"
        "[EMBARRASSED] = shame, awkwardness, or social discomfort\n"
        "[FLATTERED] = pleased by praise or admiration\n\n"

        "Reply with ONLY one emotion tag.\n\n"
        f"Message:\n{text[:1200]}\n\n"
        "Emotion:"
    )
    #print(text[:1200])

    grammar = 'root ::= "[NEUTRAL]" | "[HAPPY]" | "[JOKING]" | "[CURIOUS]" | "[SNARKY]" | "[EXCITED]" | "[SKEPTICAL]" | "[JUDGING]" | "[SHOCKED]" | "[SAD]" | "[ANXIOUS]" | "[EMBARRASSED]" | "[FLATTERED]"'

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

        for tag in EMOTION_MAP:
            if f"[{tag.upper()}]" in raw.upper():
                return tag
        return DEFAULT_EMOTION
    except Exception as e:
        logger.warning(f"Emotion classification failed: {e}")
        return DEFAULT_EMOTION

def build_simple_user_message(app_name: str, title: str, summary: str) -> str:
    what = title if title and title != "(no title)" else app_name
    return f"I'm in {app_name} looking at '{what}'. {summary}. What do you think about that? Give me a short comment on it."

# ============================================================
# TKINTER UI
# ============================================================
class GobboNetHelper(tk.Tk):

    def __init__(self):
        super().__init__()

        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        self.wm_attributes(
            "-transparentcolor",
            TRANSPARENT_COLOR
        )
        self.configure(bg=TRANSPARENT_COLOR)

        self._drag_start_x = 0
        self._drag_start_y = 0
        self._is_dragging = False

        self._sprite_anchor_x = None
        self._sprite_anchor_y = None

        self.response_queue = queue.Queue()

        self.raw_stream_text = ""
        self.parsed_emotion = None
        self.sprites = {}

        self._last_message_time = time.time()
        self._next_opportunity_at = None

        self._schedule_next_opportunity()

        self._proactive_lock = threading.Lock()
        self._proactive_busy = False

        # --------------------------------------------------------
        # UI
        # --------------------------------------------------------

        self.load_sprite_sheet()

        self.container = tk.Frame(
            self,
            bg=TRANSPARENT_COLOR
        )
        self.container.pack(
            fill="both",
            expand=True
        )

        # Bubble Frame
        self.bubble_frame = tk.Frame(
            self.container,
            bg="white",
            highlightbackground="black",
            highlightthickness=2,
            bd=0
        )

        self.bubble_text = tk.Text(
            self.bubble_frame,
            bg="white",
            fg="black",
            font=("Arial", 10),
            wrap="word",
            width=30,
            height=1,
            bd=0,
            relief="flat",
            highlightthickness=0,
            cursor="ibeam",
            padx=8,
            pady=6
        )

        self.bubble_text.pack()
        self.bubble_text.config(state="disabled")

        self.bubble_text.bind(
            "<MouseWheel>",
            self.on_text_mousewheel
        )

        # Tail
        self.tail_canvas = tk.Canvas(
            self.container,
            width=24,
            height=12,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            bd=0
        )

        self.tail_canvas.create_polygon(
            2, 0,
            22, 0,
            12, 11,
            fill="white",
            outline="black",
            width=2
        )

        self.tail_canvas.create_line(
            3, 0,
            21, 0,
            fill="white",
            width=3
        )

        # Sprite
        self.sprite_label = tk.Label(
            self.container,
            bg=TRANSPARENT_COLOR,
            bd=0,
            cursor="fleur"
        )

        self.sprite_label.pack()

        self.current_emotion = DEFAULT_EMOTION
        self.update_sprite(DEFAULT_EMOTION)

        # Mouse bindings
        self.sprite_label.bind(
            "<ButtonPress-1>",
            self.on_press
        )

        self.sprite_label.bind(
            "<B1-Motion>",
            self.on_drag
        )

        self.sprite_label.bind(
            "<ButtonRelease-1>",
            self.on_release
        )

        for widget in (
            self.sprite_label,
            self.bubble_frame,
            self.bubble_text,
            self.tail_canvas
        ):
            widget.bind(
                "<Button-3>",
                self.show_context_menu
            )

        self.center_on_screen()
        self.update_sprite_anchor()

        self.check_queue()
        self._tick_proactive()

    def quit_application(self):
        """
        Shut down both the Tkinter helper and the pywebview event loop.
        """

        try:
            GOBBO_BRIDGE.close()
        except Exception as error:
            logger.error(
                f"Could not close GobboNet WebView: {error}"
            )

        try:
            self.quit()
        finally:
            self.destroy()

    # ============================================================
    # PROACTIVE
    # ============================================================

    def _schedule_next_opportunity(self):
        delay = random.uniform(
            RANDOM_WINDOW_MIN_SECONDS,
            RANDOM_WINDOW_MAX_SECONDS
        )

        self._next_opportunity_at = (
            time.time() + delay
        )
        #print(f"Next proactive in {delay} seconds")

    def _note_message_exchanged(self):
        self._last_message_time = time.time()
        self._schedule_next_opportunity()

    def _tick_proactive(self):
        try:

            if (
                HAS_ACCESSIBILITY
                and self._next_opportunity_at
                and time.time() >=
                    self._next_opportunity_at
                and not self._proactive_busy
            ):

                if (
                    random.random() <
                    RANDOM_WINDOW_PROBABILITY
                ):
                    threading.Thread(
                        target=self._proactive_worker,
                        daemon=True
                    ).start()

                self._schedule_next_opportunity()

        except Exception as error:
            logger.error(
                f"Proactive Tick Error: {error}"
            )

        self.after(
            2000,
            self._tick_proactive
        )

    def _proactive_worker(self):

        with self._proactive_lock:

            if self._proactive_busy:
                return

            self._proactive_busy = True

        try:

            # ----------------------------------------------------
            # Do not even attempt proactive interaction until
            # GobboNet has reached the safe state.
            # ----------------------------------------------------
            #print("proactive worker")
            if not GOBBO_BRIDGE.state_ready_event.is_set():
                return

            info = get_active_window_info()

            if (info):

                summary = summarize_with_direct_gguf(
                    info["app_name"],
                    info["title"],
                    info["content"]
                )

                self.send_to_gobbonet(
                    build_simple_user_message(
                        info["app_name"],
                        info["title"],
                        summary
                    )
                )
                #print("proactive success")

        except Exception as error:
            logger.error(
                f"Proactive Worker Error: {error}"
            )

        finally:
            self._proactive_busy = False

    # ============================================================
    # SPRITES
    # ============================================================

    def load_sprite_sheet(self):

        if not os.path.exists(
            SPRITE_SHEET_PATH
        ):

            placeholder = Image.new(
                "RGBA",
                (100, 100),
                color=(200, 200, 200)
            )

            self.placeholder_img = (
                ImageTk.PhotoImage(placeholder)
            )

            for emotion in EMOTION_MAP:
                self.sprites[emotion] = (
                    self.placeholder_img
                )

            return

        sheet = Image.open(
            SPRITE_SHEET_PATH
        ).convert("RGBA")

        sw, sh = sheet.size

        sp_w = sw // COLS
        sp_h = sh // ROWS

        for emotion, (r, c) in EMOTION_MAP.items():

            cropped = sheet.crop(
                (
                    c * sp_w,
                    r * sp_h,
                    (c + 1) * sp_w,
                    (r + 1) * sp_h
                )
            )

            self.sprites[emotion] = (
                ImageTk.PhotoImage(
                    cropped.resize(
                        (SPRITE_SIZE_X, SPRITE_SIZE_Y),
                        Image.Resampling.LANCZOS
                    )
                )
            )

    def update_sprite(self, emotion):

        emotion = emotion.lower().strip()

        self.current_emotion = (
            emotion
            if emotion in self.sprites
            else DEFAULT_EMOTION
        )

        self.sprite_label.config(
            image=self.sprites[
                self.current_emotion
            ]
        )

    # ============================================================
    # WINDOW DRAGGING
    # ============================================================

    def on_press(self, event):

        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._is_dragging = False

    def on_drag(self, event):

        if (
            abs(event.x - self._drag_start_x) > 3
            or
            abs(event.y - self._drag_start_y) > 3
        ):

            self._is_dragging = True

            x = (
                self.winfo_x()
                +
                event.x
                -
                self._drag_start_x
            )

            y = (
                self.winfo_y()
                +
                event.y
                -
                self._drag_start_y
            )

            self.geometry(
                f"+{x}+{y}"
            )

    def on_release(self, event):

        if self._is_dragging:

            self.clamp_to_screen_bounds()
            self.update_sprite_anchor()

            self._is_dragging = False

        else:
            self.open_prompt_dialog()

    def update_sprite_anchor(self):

        self.update_idletasks()

        self._sprite_anchor_x = (
            self.winfo_x()
            +
            self.sprite_label.winfo_x()
        )

        self._sprite_anchor_y = (
            self.winfo_y()
            +
            self.sprite_label.winfo_y()
        )

    def reposition_window_to_anchor(self):

        if self._sprite_anchor_x is None:
            return

        self.update_idletasks()

        new_x = (
            self._sprite_anchor_x
            -
            self.sprite_label.winfo_x()
        )

        new_y = (
            self._sprite_anchor_y
            -
            self.sprite_label.winfo_y()
        )

        self.geometry(
            f"+{new_x}+{new_y}"
        )

    def clamp_to_screen_bounds(self):

        self.update_idletasks()

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()

        ww = self.winfo_width()
        wh = self.winfo_height()

        self.geometry(
            f"+{max(0, min(self.winfo_x(), sw - ww))}"
            f"+{max(0, min(self.winfo_y(), sh - wh))}"
        )

    def center_on_screen(self):

        self.update_idletasks()

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()

        self.geometry(
            f"+{sw // 2 - 65}+{sh // 2 - 85}"
        )

    # ============================================================
    # SPEECH BUBBLE
    # ============================================================

    def on_text_mousewheel(self, event):

        self.bubble_text.yview_scroll(
            int(-1 * (event.delta / 120)),
            "units"
        )

        return "break"

    def set_speech_bubble(self, text):

        if not text.strip():

            self.bubble_frame.pack_forget()
            self.tail_canvas.pack_forget()

            self.reposition_window_to_anchor()

            return

        self.bubble_frame.pack(
            side="top",
            pady=0,
            before=self.sprite_label
        )

        self.tail_canvas.pack(
            side="top",
            pady=0,
            before=self.sprite_label
        )

        self.bubble_text.config(
            state="normal"
        )

        self.bubble_text.delete(
            "1.0",
            tk.END
        )

        self.bubble_text.insert(
            "1.0",
            text
        )

        self.update_idletasks()

        num_lines = self.bubble_text.count(
            "1.0",
            "end-1c",
            "displaylines"
        )

        lines = (
            num_lines[0]
            if num_lines
            else 1
        ) + 1

        self.bubble_text.config(
            height=min(
                lines,
                MAX_BUBBLE_LINES
            ),
            state="disabled"
        )

        self.reposition_window_to_anchor()

    # ============================================================
    # USER INPUT
    # ============================================================

    def open_prompt_dialog(self):

        # --------------------------------------------------------
        # Don't allow interaction before GobboNet is safe.
        # --------------------------------------------------------
        if not GOBBO_BRIDGE.state_ready_event.is_set():

            self.set_speech_bubble(
                "GobboNet is still loading..."
            )

            return

        user_text = simpledialog.askstring(
            "GobboNet",
            "Say something:"
        )

        if user_text:
            self.send_to_gobbonet(
                user_text
            )

    def send_to_gobbonet(self, prompt_text):

        if not GOBBO_BRIDGE.state_ready_event.is_set():

            self.set_speech_bubble(
                "GobboNet isn't ready yet."
            )

            return

        self.raw_stream_text = ""
        self.parsed_emotion = None

        self.update_sprite("curious")
        self.set_speech_bubble("...")

        self._note_message_exchanged()

        threading.Thread(
            target=self._gobbo_worker,
            args=(prompt_text,),
            daemon=True
        ).start()

    def _gobbo_worker(self, prompt_text):

        try:

            content = GOBBO_BRIDGE.send_message(
                prompt_text
            )

            emotion = (
                classify_emotion_with_direct_gguf(
                    content
                )
            )

            clean = content.strip()

            if (
                clean.startswith("[")
                and
                "]" in clean[:20]
            ):
                clean = clean.split(
                    "]",
                    1
                )[1].lstrip()

            self.response_queue.put({
                "emotion": emotion,
                "text": clean
            })

        except Exception as error:

            logger.exception(
                "GobboNet worker failed."
            )

            self.response_queue.put({
                "emotion": DEFAULT_EMOTION,
                "text": (
                    f"[GOBBONET ERROR: {error}]"
                )
            })

    # ============================================================
    # RESPONSE QUEUE
    # ============================================================

    def check_queue(self):

        while not self.response_queue.empty():

            item = self.response_queue.get()

            if isinstance(item, dict):

                emotion = item.get(
                    "emotion",
                    DEFAULT_EMOTION
                )

                text = item.get(
                    "text",
                    ""
                )

            else:

                emotion = DEFAULT_EMOTION
                text = str(item)

            self.parsed_emotion = emotion

            self.update_sprite(
                emotion
            )

            self.set_speech_bubble(
                text
            )

            self._note_message_exchanged()

        self.after(
            50,
            self.check_queue
        )

    # ============================================================
    # CONTEXT MENU
    # ============================================================

    def show_context_menu(self, event):

        menu = tk.Menu(
            self,
            tearoff=False
        )

        persona_menu = tk.Menu(
            menu,
            tearoff=False
        )

        # --------------------------------------------------------
        # READ SERVER/APPLICATION STATE.
        #
        # If state isn't ready, do NOT interpret that as zero
        # characters.
        # --------------------------------------------------------
        try:

            if not GOBBO_BRIDGE.state_ready_event.is_set():

                persona_menu.add_command(
                    label="GobboNet still loading...",
                    state="disabled"
                )

            else:

                cards = (
                    GOBBO_BRIDGE.get_character_cards()
                )

                if cards:

                    for card in cards:

                        card_id = card.get("id")
                        card_name = card.get(
                            "name",
                            "Unnamed"
                        )

                        persona_menu.add_command(
                            label=card_name,
                            command=lambda cid=card_id:
                                self.select_character(cid)
                        )

                else:

                    # This means GobboNet genuinely reported
                    # an initialized zero-card state.
                    #
                    # We still do NOT perform any mutation.
                    persona_menu.add_command(
                        label="No characters found",
                        state="disabled"
                    )

        except Exception as error:

            logger.error(
                f"Could not read GobboNet characters: {error}"
            )

            persona_menu.add_command(
                label="Characters unavailable",
                state="disabled"
            )

        menu.add_cascade(
            label="Persona",
            menu=persona_menu
        )

        menu.add_separator()

        menu.add_command(
            label="New Thread",
            command=self.new_thread
        )

        menu.add_command(
            label="Stop Generation",
            command=self.stop_generation
        )

        menu.add_separator()

        menu.add_command(
            label="Quit",
            command=self.quit_application
        )

        try:

            menu.tk_popup(
                event.x_root,
                event.y_root
            )

        finally:

            menu.grab_release()

    # ============================================================
    # CHARACTER SELECTION
    # ============================================================

    def select_character(self, card_id):

        try:

            # ----------------------------------------------------
            # activate_character() verifies that the requested
            # card actually exists before touching GobboNet.
            # ----------------------------------------------------
            GOBBO_BRIDGE.activate_character(
                card_id
            )

            self.new_thread()

        except Exception as error:

            logger.error(
                f"Character activation blocked: {error}"
            )

            self.set_speech_bubble(
                "[GobboNet safety lock: "
                f"{error}]"
            )

    # ============================================================
    # NEW THREAD
    # ============================================================

    def new_thread(self):

        try:

            GOBBO_BRIDGE.create_new_thread()

            self.set_speech_bubble("")
            self.update_sprite("neutral")
            self._note_message_exchanged()

        except Exception as error:

            logger.error(
                f"New thread blocked: {error}"
            )

            self.set_speech_bubble(
                "[GobboNet safety lock: "
                f"{error}]"
            )

    # ============================================================
    # STOP GENERATION
    # ============================================================

    def stop_generation(self):

        try:

            GOBBO_BRIDGE.stop_generation()

        except Exception as error:

            logger.error(
                f"Could not stop generation: {error}"
            )

# ============================================================
# MAIN
# ============================================================
def run_app():
    app = GobboNetHelper()
    app.mainloop()

if __name__ == "__main__":
    # 1. Spawn Tkinter in a background thread
    tk_thread = threading.Thread(target=run_app, daemon=True)
    tk_thread.start()

    # 2. Start pywebview on the MAIN thread
    try:
        GOBBO_BRIDGE.start_window()
    except Exception as error:
        logger.error(f"STARTUP ERROR: {error}")
