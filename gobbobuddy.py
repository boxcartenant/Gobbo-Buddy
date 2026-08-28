
import os
import json
import queue
import threading
import tkinter as tk
from tkinter import simpledialog
import time
import traceback

from pathlib import Path
from PIL import Image, ImageTk


# ============================================================
# CONFIGURATION
# ============================================================

SPRITE_SHEET_PATH = "gobbo sprites 2.png"

ROWS = 3
COLS = 4

EMOTION_MAP = {
    "neutral": (0, 0),
    "happy": (0, 1),
    "joking": (0, 1),
    "curious": (0, 2),
    "snarky": (0, 3),
    "sarcastic": (0, 3),
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


# ============================================================
# GOBBONET
# ============================================================

GOBBONET_BASE_URL = "http://127.0.0.1:9066"
GOBBONET_PASSWORD = "YOUR PASSWORD"

CHARACTER_NAME = "Fumo"
BUDDY_THREAD_NAME = "GobboBuddy"

GENERATION_TIMEOUT = 600
PAGE_READY_TIMEOUT = 60

STATE_EXPORT_PATH = Path("gobbonet-state-export.json")


# ============================================================
# HEADLESS GOBBONET BRIDGE
# ============================================================

class GobboNetBrowserBridge:

    def __init__(self):
        self._cmd_q = queue.Queue()
        self._worker = None
        self._started = threading.Event()
        self._start_error = None
        self._alive = False

        # Owned exclusively by worker thread
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._gobbonet_thread_id = None

    # --------------------------------------------------------
    # PUBLIC API
    # --------------------------------------------------------

    def start(self):
        if self._worker and self._worker.is_alive():
            self._started.wait(timeout=PAGE_READY_TIMEOUT + 30)

            if self._start_error:
                raise RuntimeError(self._start_error)

            return

        self._started.clear()
        self._start_error = None
        self._alive = True

        self._worker = threading.Thread(
            target=self._worker_main,
            name="GobboNetBridge",
            daemon=True,
        )

        self._worker.start()

        self._started.wait(timeout=PAGE_READY_TIMEOUT + 30)

        if self._start_error:
            raise RuntimeError(self._start_error)

        if not self._started.is_set():
            raise RuntimeError(
                "GobboNet bridge failed to start in time"
            )

    def stop(self):
        self._alive = False

        done = queue.Queue()

        try:
            self._cmd_q.put(
                ("stop", None, done),
                timeout=1,
            )
        except Exception:
            pass

        try:
            done.get(timeout=10)
        except Exception:
            pass

    def send_message(self, prompt_text):
        self.start()

        done = queue.Queue()

        self._cmd_q.put(
            ("send", prompt_text, done)
        )

        ok, payload = done.get(
            timeout=GENERATION_TIMEOUT + 60
        )

        if not ok:
            raise RuntimeError(payload)

        return payload

    def ensure_card_and_thread(self):
        self.start()

        done = queue.Queue()

        self._cmd_q.put(
            ("setup", None, done)
        )

        ok, payload = done.get(timeout=60)

        if not ok:
            raise RuntimeError(payload)

        return payload

    # --------------------------------------------------------
    # WORKER
    # --------------------------------------------------------

    def _worker_main(self):
        try:
            self._boot_browser()

            self._started.set()

        except Exception as e:
            self._start_error = (
                f"{e}\n{traceback.format_exc()}"
            )

            self._started.set()
            return

        while self._alive:

            try:
                cmd, arg, done = self._cmd_q.get(
                    timeout=0.5
                )

            except queue.Empty:
                continue

            try:

                if cmd == "stop":
                    self._shutdown_browser()
                    done.put((True, None))
                    break

                if cmd == "setup":
                    result = self._ensure_card_and_thread()
                    done.put((True, result))

                elif cmd == "send":
                    result = self._send_message_impl(arg)
                    done.put((True, result))

                else:
                    done.put(
                        (
                            False,
                            f"Unknown command: {cmd}",
                        )
                    )

            except Exception as e:
                done.put(
                    (
                        False,
                        f"{e}\n{traceback.format_exc()}",
                    )
                )

        self._alive = False

    # --------------------------------------------------------
    # BROWSER STARTUP
    # --------------------------------------------------------

    def _boot_browser(self):

        try:
            from playwright.sync_api import sync_playwright

        except ImportError as e:

            raise RuntimeError(
                "Playwright is required.\n"
                "pip install playwright\n"
                "playwright install chromium\n"
                f"Original error: {e}"
            ) from e

        print(
            "[Bridge] Starting headless Chromium…",
            flush=True,
        )

        self._playwright = sync_playwright().start()

        self._browser = self._playwright.chromium.launch(
            channel="msedge",
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        self._context = self._browser.new_context(
            viewport={
                "width": 1280,
                "height": 800,
            },
            user_agent=(
                "GobboBuddy/2.0 "
                "(headless Chromium; GobboNet client)"
            ),
        )

        self._page = self._context.new_page()

        self._page.set_default_timeout(
            PAGE_READY_TIMEOUT * 1000
        )

        # ----------------------------------------------------
        # Browser diagnostics
        # ----------------------------------------------------

        self._page.on(
            "console",
            lambda msg: print(
                f"[Browser console:{msg.type}] {msg.text}",
                flush=True,
            ),
        )

        self._page.on(
            "requestfailed",
            lambda request: print(
                f"[Browser REQUEST FAILED] "
                f"{request.method} "
                f"{request.url} → "
                f"{request.failure}",
                flush=True,
            ),
        )

        self._page.on(
            "framenavigated",
            lambda frame: (
                print(
                    f"[Browser NAV] {frame.url}",
                    flush=True,
                )
                if frame == self._page.main_frame
                else None
            ),
        )

        # ----------------------------------------------------
        # Login
        # ----------------------------------------------------

        self._open_and_login()

        # ----------------------------------------------------
        # Import the actual GobboNet state
        # ----------------------------------------------------

        self._import_gobbonet_state()

        # ----------------------------------------------------
        # Reload so GobboNet's OWN boot code consumes it
        # ----------------------------------------------------

        print(
            "[Bridge] Reloading GobboNet after state import…",
            flush=True,
        )

        self._page.reload(
            wait_until="domcontentloaded"
        )

        # ----------------------------------------------------
        # Wait for native application
        # ----------------------------------------------------

        self._wait_for_app_ready()

        self._inspect_browser_state()

        print(
            "[Bridge] GobboNet page ready.",
            flush=True,
        )

    # --------------------------------------------------------
    # LOGIN
    # --------------------------------------------------------

    def _open_and_login(self):

        page = self._page

        url = (
            GOBBONET_BASE_URL.rstrip("/")
            + "/"
        )

        print(
            f"[Bridge] Navigating to {url}",
            flush=True,
        )

        page.goto(
            url,
            wait_until="domcontentloaded",
        )

        max_attempts = 3

        for attempt in range(max_attempts):

            if page.locator(
                "#msg-input"
            ).count() > 0:

                print(
                    "[Bridge] Chat UI detected.",
                    flush=True,
                )

                return

            password = page.locator(
                'input[name="password"]'
            )

            if password.count() > 0:

                print(
                    f"[Bridge] Login page detected; "
                    f"submitting password "
                    f"(attempt {attempt + 1}/{max_attempts})…",
                    flush=True,
                )

                password.fill(
                    GOBBONET_PASSWORD
                )

                submit = page.locator(
                    'button[type="submit"]'
                )

                if submit.count() > 0:
                    submit.click()

                else:

                    submit = page.locator(
                        'input[type="submit"]'
                    )

                    if submit.count() > 0:
                        submit.click()

                    else:
                        password.press("Enter")

                print(
                    "[Bridge] Login submitted.",
                    flush=True,
                )

                try:
                    page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=10000,
                    )
                except Exception:
                    pass

                time.sleep(1)

                if page.locator(
                    'input[name="password"]'
                ).count() > 0:

                    print(
                        "[Bridge] Login still present.",
                        flush=True,
                    )

                    continue

                print(
                    "[Bridge] Login appears successful.",
                    flush=True,
                )

                return

            time.sleep(0.5)

        raise RuntimeError(
            "GobboNet login failed after "
            f"{max_attempts} attempts."
        )

    # --------------------------------------------------------
    # STATE IMPORT
    # --------------------------------------------------------
    
    def _import_gobbonet_state(self):
        """
        Import the exported GobboNet IndexedDB state into the headless
        browser's gobbonet-state database.

        IMPORTANT:
            The meta store uses an OUT-OF-LINE key:
                meta.put(app_record, "app")

            The other stores use inline keyPaths:
                telemetry -> turn_id
                threads   -> id
                vectors   -> hash
        """

        export_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "gobbonet-state-export.json",
        )

        if not os.path.exists(export_path):
            raise RuntimeError(
                f"GobboNet state export not found:\n{export_path}"
            )

        print("[Bridge] Loading GobboNet state export…", flush=True)

        import json

        with open(export_path, "r", encoding="utf-8") as f:
            exported = json.load(f)

        # ------------------------------------------------------------
        # Validate the basic export structure.
        # ------------------------------------------------------------

        database = exported.get("database", {})
        stores = exported.get("stores", {})

        print(
            f"[Bridge] Export database: "
            f"{database.get('name')} v{database.get('version')}",
            flush=True,
        )

        if database.get("name") != "gobbonet-state":
            raise RuntimeError(
                f"Unexpected database name in export: "
                f"{database.get('name')!r}"
            )

        if database.get("version") != 2:
            raise RuntimeError(
                f"Unexpected GobboNet database version: "
                f"{database.get('version')!r}"
            )

        required_stores = {
            "meta",
            "telemetry",
            "threads",
            "vectors",
        }

        missing = required_stores - set(stores.keys())

        if missing:
            raise RuntimeError(
                f"Export is missing IndexedDB stores: "
                f"{', '.join(sorted(missing))}"
            )

        # ------------------------------------------------------------
        # Find Fumo before touching the browser database.
        # ------------------------------------------------------------

        app_records = stores.get("meta", [])

        app_record = None

        for record in app_records:
            if not isinstance(record, dict):
                continue

            # Our exporter represents the "app" record as either:
            #
            #   {"key": "app", "value": {...}}
            #
            # or potentially just the application object.
            #
            # Support both forms.

            if record.get("key") == "app":
                candidate = record.get("value")

                if isinstance(candidate, dict):
                    app_record = candidate
                    break

            if "characterCards" in record:
                app_record = record
                break

        if not isinstance(app_record, dict):
            raise RuntimeError(
                "Could not find the GobboNet 'app' record in the export."
            )

        character_cards = app_record.get("characterCards") or []

        fumo = None

        for card in character_cards:
            if (
                isinstance(card, dict)
                and str(card.get("name", "")).strip().lower()
                == CHARACTER_NAME.strip().lower()
            ):
                fumo = card
                break

        if fumo is None:
            names = [
                card.get("name")
                for card in character_cards
                if isinstance(card, dict)
            ]

            raise RuntimeError(
                f"Character card '{CHARACTER_NAME}' not found in export "
                f"(have: {names})"
            )

        print(
            f"[Bridge] Export contains character card "
            f"'{fumo.get('name')}' ({fumo.get('id')}).",
            flush=True,
        )

        # ------------------------------------------------------------
        # Send the complete export into the browser.
        # ------------------------------------------------------------

        result = self._page.evaluate(
            """
            async (exported) => {

                const DB_NAME = "gobbonet-state";

                function requestToPromise(request) {
                    return new Promise((resolve, reject) => {
                        request.onsuccess = () => resolve(request.result);
                        request.onerror = () => reject(request.error);
                    });
                }

                function transactionToPromise(tx) {
                    return new Promise((resolve, reject) => {
                        tx.oncomplete = () => resolve();
                        tx.onerror = () => reject(tx.error);
                        tx.onabort = () =>
                            reject(tx.error || new Error("Transaction aborted"));
                    });
                }

                const stores = exported.stores || {};

                // ----------------------------------------------------
                // Open the EXISTING GobboNet database.
                // Do NOT recreate it.
                // ----------------------------------------------------

                const db = await new Promise((resolve, reject) => {
                    const req = indexedDB.open(DB_NAME);

                    req.onsuccess = () => resolve(req.result);
                    req.onerror = () => reject(req.error);
                });

                const result = {
                    databaseName: db.name,
                    version: db.version,
                    stores: Array.from(db.objectStoreNames),
                    imported: {}
                };

                // ----------------------------------------------------
                // Verify the schema we're expecting.
                // ----------------------------------------------------

                const expected = {
                    meta: null,
                    telemetry: "turn_id",
                    threads: "id",
                    vectors: "hash"
                };

                for (const [storeName, expectedKeyPath] of Object.entries(expected)) {

                    if (!db.objectStoreNames.contains(storeName)) {
                        throw new Error(
                            `IndexedDB store '${storeName}' does not exist`
                        );
                    }

                    const tx = db.transaction(storeName, "readonly");
                    const store = tx.objectStore(storeName);

                    if (store.keyPath !== expectedKeyPath) {
                        throw new Error(
                            `Unexpected keyPath for ${storeName}: ` +
                            `${JSON.stringify(store.keyPath)} ` +
                            `(expected ${JSON.stringify(expectedKeyPath)})`
                        );
                    }
                }

                // ----------------------------------------------------
                // Import everything in ONE transaction.
                // ----------------------------------------------------

                const tx = db.transaction(
                    ["meta", "telemetry", "threads", "vectors"],
                    "readwrite"
                );

                const metaStore = tx.objectStore("meta");
                const telemetryStore = tx.objectStore("telemetry");
                const threadsStore = tx.objectStore("threads");
                const vectorsStore = tx.objectStore("vectors");

                // ----------------------------------------------------
                // META
                //
                // This is the critical part.
                //
                // meta has keyPath === null, so the key MUST be
                // supplied explicitly.
                // ----------------------------------------------------

                let metaRecords = stores.meta || [];

                for (const record of metaRecords) {

                    let key = null;
                    let value = record;

                    if (
                        record &&
                        typeof record === "object" &&
                        Object.prototype.hasOwnProperty.call(record, "key") &&
                        Object.prototype.hasOwnProperty.call(record, "value")
                    ) {
                        key = record.key;
                        value = record.value;
                    }

                    if (key === null || key === undefined) {
                        // GobboNet's app record is stored under "app".
                        if (
                            value &&
                            typeof value === "object" &&
                            Object.prototype.hasOwnProperty.call(
                                value,
                                "characterCards"
                            )
                        ) {
                            key = "app";
                        }
                    }

                    if (key === null || key === undefined) {
                        throw new Error(
                            "Could not determine IndexedDB key for a meta record"
                        );
                    }

                    metaStore.put(value, key);
                    result.imported.meta =
                        (result.imported.meta || 0) + 1;
                }

                // ----------------------------------------------------
                // TELEMETRY
                //
                // keyPath = turn_id
                // ----------------------------------------------------

                for (const record of (stores.telemetry || [])) {
                    if (!record || record.turn_id === undefined) {
                        console.warn(
                            "[GobboBuddy] Skipping telemetry record " +
                            "without turn_id"
                        );
                        continue;
                    }

                    telemetryStore.put(record);
                    result.imported.telemetry =
                        (result.imported.telemetry || 0) + 1;
                }

                // ----------------------------------------------------
                // THREADS
                //
                // keyPath = id
                // ----------------------------------------------------

                for (const record of (stores.threads || [])) {
                    if (!record || record.id === undefined) {
                        console.warn(
                            "[GobboBuddy] Skipping thread without id"
                        );
                        continue;
                    }

                    threadsStore.put(record);
                    result.imported.threads =
                        (result.imported.threads || 0) + 1;
                }

                // ----------------------------------------------------
                // VECTORS
                //
                // keyPath = hash
                // ----------------------------------------------------

                for (const record of (stores.vectors || [])) {
                    if (!record || record.hash === undefined) {
                        console.warn(
                            "[GobboBuddy] Skipping vector without hash"
                        );
                        continue;
                    }

                    vectorsStore.put(record);
                    result.imported.vectors =
                        (result.imported.vectors || 0) + 1;
                }

                await transactionToPromise(tx);

                db.close();

                return result;
            }
            """,
            exported,
        )

        print(
            f"[Bridge] IndexedDB import complete: {result}",
            flush=True,
        )

        # ------------------------------------------------------------
        # Give GobboNet a chance to notice the imported state.
        # ------------------------------------------------------------

        print(
            "[Bridge] Reloading GobboNet so its JS state is rebuilt "
            "from the imported database…",
            flush=True,
        )

        self._page.reload(wait_until="domcontentloaded")

        self._page.wait_for_selector(
            "#msg-input",
            timeout=PAGE_READY_TIMEOUT * 1000,
        )

        self._page.wait_for_function(
            """() => {
                return typeof sendMessage === 'function'
                    && typeof getActiveThread === 'function'
                    && typeof state === 'object'
                    && state !== null
                    && Array.isArray(state.characterCards);
            }""",
            timeout=PAGE_READY_TIMEOUT * 1000,
        )

        time.sleep(1.0)

        # ------------------------------------------------------------
        # Verify what GobboNet itself sees.
        # ------------------------------------------------------------

        verification = self._page.evaluate(
            """() => ({
                activeCardId: state.activeCardId || null,
                activeThreadId: state.activeThreadId || null,
                characterCards: (state.characterCards || []).map(c => ({
                    id: c.id,
                    name: c.name
                })),
                threadCount: (state.threads || []).length,
                threadOrder: state.threadOrder || []
            })"""
        )

        print(
            "[Bridge] GobboNet state after import:",
            flush=True,
        )
        print(
            f"[Bridge]   activeCardId: "
            f"{verification.get('activeCardId')}",
            flush=True,
        )
        print(
            f"[Bridge]   activeThreadId: "
            f"{verification.get('activeThreadId')}",
            flush=True,
        )
        print(
            f"[Bridge]   characterCards: "
            f"{verification.get('characterCards')}",
            flush=True,
        )
        print(
            f"[Bridge]   threadCount: "
            f"{verification.get('threadCount')}",
            flush=True,
        )
        print(
            f"[Bridge]   threadOrder: "
            f"{verification.get('threadOrder')}",
            flush=True,
        )

        names = [
            c.get("name")
            for c in verification.get("characterCards", [])
        ]

        if CHARACTER_NAME.lower() not in [
            str(name).lower() for name in names
        ]:
            raise RuntimeError(
                f"IndexedDB import completed, but GobboNet JS still "
                f"does not see character '{CHARACTER_NAME}'. "
                f"Characters visible to JS: {names}"
            )

        print(
            f"[Bridge] SUCCESS: GobboNet itself sees '{CHARACTER_NAME}'.",
            flush=True,
        )

    # --------------------------------------------------------
    # WAIT FOR GOBBONET
    # --------------------------------------------------------

    def _wait_for_app_ready(self):

        page = self._page

        print(
            "[Bridge] Waiting for chat input…",
            flush=True,
        )

        page.wait_for_selector(
            "#msg-input",
            timeout=PAGE_READY_TIMEOUT * 1000,
        )

        print(
            "[Bridge] #msg-input attached.",
            flush=True,
        )

        print(
            "[Bridge] Waiting for GobboNet JS "
            "application functions…",
            flush=True,
        )

        page.wait_for_function(
            """
            () => {
                return (
                    typeof sendMessage === "function"
                    &&
                    typeof getActiveThread === "function"
                    &&
                    typeof state === "object"
                    &&
                    state !== null
                    &&
                    Array.isArray(
                        state.characterCards
                    )
                );
            }
            """,
            timeout=PAGE_READY_TIMEOUT * 1000,
        )

        print(
            "[Bridge] GobboNet JS application ready.",
            flush=True,
        )

        time.sleep(1)

    # --------------------------------------------------------
    # STATE INSPECTION
    # --------------------------------------------------------

    def _inspect_browser_state(self):

        print(
            "[Bridge] Inspecting GobboNet browser state…",
            flush=True,
        )

        result = self._page.evaluate(
            """
            () => {

                const cards =
                    Array.isArray(
                        state.characterCards
                    )
                        ? state.characterCards
                        : [];

                return {
                    activeCardId:
                        state.activeCardId,

                    activeThreadId:
                        state.activeThreadId,

                    threadOrder:
                        state.threadOrder,

                    characterCards:
                        cards.map(c => ({
                            id: c.id,
                            name: c.name
                        }))
                };
            }
            """
        )

        print(
            "[Bridge] activeCardId:",
            result.get("activeCardId"),
            flush=True,
        )

        print(
            "[Bridge] activeThreadId:",
            result.get("activeThreadId"),
            flush=True,
        )

        print(
            "[Bridge] threadOrder:",
            result.get("threadOrder"),
            flush=True,
        )

        print(
            "[Bridge] Character cards:",
            result.get("characterCards"),
            flush=True,
        )

    # --------------------------------------------------------
    # CARD + THREAD
    # --------------------------------------------------------

    def _ensure_card_and_thread(self):

        result = self._page.evaluate(
            """
            ({ characterName, existingThreadId }) => {

                const cards = state.characterCards || [];

                const card = cards.find(
                    c =>
                        (c.name || "").toLowerCase()
                        === characterName.toLowerCase()
                );

                if (!card) {
                    return {
                        ok: false,
                        error:
                            "Character card not found: "
                            + characterName
                            + " (have: "
                            + cards.map(c => c.name).join(", ")
                            + ")"
                    };
                }

                // Always make Fumo the active card.
                if (state.activeCardId !== card.id) {
                    if (typeof activateCard === "function") {
                        activateCard(card.id);
                    } else {
                        state.activeCardId = card.id;
                        saveState();
                    }
                }

                /*
                 * If GobboBuddy already has a thread, verify that GobboNet
                 * still has it. If it does, simply switch back to it.
                 */
                if (existingThreadId) {

                    const existing =
                        state.threads.find(
                            t => t.id === existingThreadId
                        );

                    if (existing) {

                        if (
                            state.activeThreadId
                            !== existingThreadId
                        ) {
                            switchThread(existingThreadId);
                        }

                        return {
                            ok: true,
                            cardId: card.id,
                            cardName: card.name,
                            threadId: existingThreadId,
                            created: false
                        };
                    }
                }

                /*
                 * No GobboBuddy thread exists yet.
                 *
                 * Let GobboNet create it through its own native function.
                 */
                if (typeof createThread !== "function") {
                    return {
                        ok: false,
                        error: "createThread() not available"
                    };
                }

                createThread();

                const thread = getActiveThread();

                if (!thread) {
                    return {
                        ok: false,
                        error:
                            "createThread() produced no active thread"
                    };
                }

                return {
                    ok: true,
                    cardId: card.id,
                    cardName: card.name,
                    threadId: thread.id,
                    created: true
                };
            }
            """,
            {
                "characterName": CHARACTER_NAME,
                "existingThreadId": self._gobbonet_thread_id,
            },
        )

        if not result or not result.get("ok"):
            err = (
                result or {}
            ).get(
                "error"
            ) or "unknown error"

            raise RuntimeError(
                f"GobboNet setup failed: {err}"
            )

        self._gobbonet_thread_id = result["threadId"]

        action = (
            "created"
            if result.get("created")
            else "selected"
        )

        print(
            f"[Bridge] Card "
            f"'{result.get('cardName')}' active; "
            f"thread {action}: "
            f"{result.get('threadId')}",
            flush=True,
        )

        return result

    # --------------------------------------------------------
    # SEND MESSAGE
    # --------------------------------------------------------

    def _send_message_impl(self, prompt_text):

        self._ensure_card_and_thread()

        page = self._page

        before = page.evaluate(
            """
            () => {

                const t =
                    getActiveThread();

                if (!t) {
                    return {
                        count: 0,
                        lastAssistant: ""
                    };
                }

                const msgs =
                    t.messages || [];

                let last = "";

                for (
                    let i = msgs.length - 1;
                    i >= 0;
                    i--
                ) {

                    if (
                        msgs[i].role
                        === "assistant"
                        &&
                        (msgs[i].content || "")
                            .trim()
                    ) {

                        last =
                            msgs[i].content;

                        break;
                    }
                }

                return {
                    count: msgs.length,
                    lastAssistant: last
                };
            }
            """
        )

        print(
            "[Bridge] Calling GobboNet's "
            "native sendMessage()…",
            flush=True,
        )

        page.evaluate(
            """
            (text) => {

                if (
                    typeof isGenerating
                    !== "undefined"
                    &&
                    isGenerating
                ) {
                    throw new Error(
                        "GobboNet is already generating"
                    );
                }

                window.__gobboBuddySendError =
                    null;

                const p =
                    sendMessage(text);

                window.__gobboBuddySend =
                    p;

                if (
                    p &&
                    typeof p.catch
                    === "function"
                ) {

                    p.catch(
                        err => {

                            console.error(
                                "[GobboBuddy] "
                                +
                                "sendMessage error:",
                                err
                            );

                            window
                                .__gobboBuddySendError =
                                String(
                                    err &&
                                    err.message
                                    ||
                                    err
                                );
                        }
                    );
                }
            }
            """,
            prompt_text,
        )

        deadline = (
            time.time()
            +
            GENERATION_TIMEOUT
        )

        last_log = 0

        while time.time() < deadline:

            status = page.evaluate(
                """
                () => {

                    const generating =
                        !!(
                            typeof isGenerating
                            !== "undefined"
                            &&
                            isGenerating
                        );

                    const err =
                        window
                            .__gobboBuddySendError
                        ||
                        null;

                    const t =
                        (
                            typeof getActiveThread
                            === "function"
                        )
                            ? getActiveThread()
                            : null;

                    const msgs =
                        (
                            t &&
                            t.messages
                        )
                        ||
                        [];

                    let lastAssistant = "";

                    for (
                        let i =
                            msgs.length - 1;
                        i >= 0;
                        i--
                    ) {

                        if (
                            msgs[i].role
                            === "assistant"
                        ) {

                            lastAssistant =
                                msgs[i].content
                                ||
                                "";

                            break;
                        }
                    }

                    return {
                        generating,
                        err,
                        count: msgs.length,
                        lastAssistant,
                        lastLen:
                            lastAssistant.length
                    };
                }
                """
            )

            if status.get("err"):

                raise RuntimeError(
                    "GobboNet sendMessage failed: "
                    +
                    status["err"]
                )

            if not status.get("generating"):

                content = (
                    status
                        .get("lastAssistant")
                        or ""
                ).strip()

                prev = (
                    before
                        .get("lastAssistant")
                        or ""
                ).strip()

                if (
                    content
                    and content != prev
                ):
                    return content

                if (
                    status.get("count", 0)
                    >
                    before.get("count", 0)
                    and content
                ):
                    return content

                time.sleep(0.3)

                status2 = page.evaluate(
                    """
                    () => {

                        const t =
                            getActiveThread();

                        const msgs =
                            (
                                t &&
                                t.messages
                            )
                            ||
                            [];

                        for (
                            let i =
                                msgs.length - 1;
                            i >= 0;
                            i--
                        ) {

                            if (
                                msgs[i].role
                                === "assistant"
                            ) {

                                return (
                                    msgs[i].content
                                    ||
                                    ""
                                ).trim();
                            }
                        }

                        return "";
                    }
                    """
                )

                if (
                    status2
                    and status2 != prev
                ):
                    return status2

                return (
                    status2
                    or content
                    or ""
                )

            now = time.time()

            if now - last_log > 5:

                print(
                    "[Bridge] generating… "
                    f"({status.get('lastLen', 0)} "
                    "chars so far)",
                    flush=True,
                )

                last_log = now

            time.sleep(0.2)

        raise RuntimeError(
            "GobboNet generation timed out after "
            f"{GENERATION_TIMEOUT}s"
        )

    # --------------------------------------------------------
    # SHUTDOWN
    # --------------------------------------------------------

    def _shutdown_browser(self):

        for obj in (
            self._page,
            self._context,
            self._browser,
        ):

            try:

                if obj:
                    obj.close()

            except Exception:
                pass

        self._page = None
        self._context = None
        self._browser = None

        if self._playwright:

            try:
                self._playwright.stop()

            except Exception:
                pass

            self._playwright = None


GOBBO_BRIDGE = GobboNetBrowserBridge()


# ============================================================
# TKINTER UI
# ============================================================

class GobboNetHelper(tk.Tk):

    def __init__(self):

        super().__init__()

        self.overrideredirect(True)

        self.wm_attributes(
            "-topmost",
            True
        )

        self.wm_attributes(
            "-transparentcolor",
            TRANSPARENT_COLOR
        )

        self.configure(
            bg=TRANSPARENT_COLOR
        )

        self._drag_start_x = 0
        self._drag_start_y = 0
        self._is_dragging = False

        self.response_queue = queue.Queue()

        self.raw_stream_text = ""

        self.parsed_emotion = None

        self.sprites = {}

        self.load_sprite_sheet()

        self.container = tk.Frame(
            self,
            bg=TRANSPARENT_COLOR
        )

        self.container.pack(
            fill="both",
            expand=True
        )

        self.bubble_frame = tk.Frame(
            self.container,
            bg="white",
            highlightbackground="black",
            highlightthickness=2,
            bd=0,
        )

        self.bubble_label = tk.Label(
            self.bubble_frame,
            text="",
            bg="white",
            fg="black",
            font=("Arial", 10),
            wraplength=250,
            justify="left",
            padx=10,
            pady=8,
        )

        self.bubble_label.pack()

        self.bubble_frame.pack_forget()

        self.sprite_label = tk.Label(
            self.container,
            bg=TRANSPARENT_COLOR,
            bd=0,
            cursor="fleur",
        )

        self.sprite_label.pack()

        self.current_emotion = DEFAULT_EMOTION

        self.update_sprite(
            DEFAULT_EMOTION
        )

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

        self.center_on_screen()

        self.check_queue()

        self.protocol(
            "WM_DELETE_WINDOW",
            self.on_close
        )

    def load_sprite_sheet(self):

        if not os.path.exists(
            SPRITE_SHEET_PATH
        ):

            placeholder = Image.new(
                "RGBA",
                (100, 100),
                color=(200, 200, 200),
            )

            self.placeholder_img = (
                ImageTk.PhotoImage(
                    placeholder
                )
            )

            for emotion in EMOTION_MAP:
                self.sprites[emotion] = (
                    self.placeholder_img
                )

            print(
                f"Warning: "
                f"'{SPRITE_SHEET_PATH}' "
                "not found. "
                "Using placeholder graphics.",
                flush=True,
            )

            return

        sheet = Image.open(
            SPRITE_SHEET_PATH
        ).convert("RGBA")

        sheet_w, sheet_h = sheet.size

        sprite_w = sheet_w // COLS
        sprite_h = sheet_h // ROWS

        for emotion, (row, col) in EMOTION_MAP.items():

            left = col * sprite_w
            top = row * sprite_h
            right = left + sprite_w
            bottom = top + sprite_h

            cropped = sheet.crop(
                (
                    left,
                    top,
                    right,
                    bottom,
                )
            )

            resized = cropped.resize(
                (130, 170),
                Image.Resampling.LANCZOS,
            )

            self.sprites[emotion] = (
                ImageTk.PhotoImage(
                    resized
                )
            )

    def update_sprite(self, emotion):

        emotion = (
            emotion
            .lower()
            .strip()
        )

        if emotion in self.sprites:

            self.current_emotion = emotion

            self.sprite_label.config(
                image=self.sprites[emotion]
            )

        else:

            self.sprite_label.config(
                image=self.sprites[
                    DEFAULT_EMOTION
                ]
            )

    def on_press(self, event):

        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._is_dragging = False

    def on_drag(self, event):

        if (
            abs(
                event.x
                -
                self._drag_start_x
            ) > 3
            or
            abs(
                event.y
                -
                self._drag_start_y
            ) > 3
        ):

            self._is_dragging = True

        if self._is_dragging:

            x = (
                self.winfo_x()
                +
                (
                    event.x
                    -
                    self._drag_start_x
                )
            )

            y = (
                self.winfo_y()
                +
                (
                    event.y
                    -
                    self._drag_start_y
                )
            )

            self.geometry(
                f"+{x}+{y}"
            )

    def on_release(self, event):

        if self._is_dragging:

            self.clamp_to_screen_bounds()

            self._is_dragging = False

        else:

            self.open_prompt_dialog()

    def clamp_to_screen_bounds(self):

        self.update_idletasks()

        screen_w = (
            self.winfo_screenwidth()
        )

        screen_h = (
            self.winfo_screenheight()
        )

        win_w = self.winfo_width()
        win_h = self.winfo_height()

        x = self.winfo_x()
        y = self.winfo_y()

        new_x = max(
            0,
            min(
                x,
                screen_w - win_w
            )
        )

        new_y = max(
            0,
            min(
                y,
                screen_h - win_h
            )
        )

        if (
            new_x != x
            or
            new_y != y
        ):

            self.geometry(
                f"+{new_x}+{new_y}"
            )

    def center_on_screen(self):

        self.update_idletasks()

        sw = (
            self.winfo_screenwidth()
        )

        sh = (
            self.winfo_screenheight()
        )

        self.geometry(
            f"+{sw // 2 - 50}"
            f"+{sh // 2 - 50}"
        )

    def open_prompt_dialog(self):

        user_text = simpledialog.askstring(
            "GobboNet",
            "Say something:"
        )

        if user_text:
            self.send_to_gobbonet(
                user_text
            )

    def set_speech_bubble(self, text):

        if text.strip():

            self.bubble_label.config(
                text=text
            )

            self.bubble_frame.pack(
                side="top",
                pady=(0, 6),
                before=self.sprite_label,
            )

        else:

            self.bubble_frame.pack_forget()

        self.clamp_to_screen_bounds()

    def send_to_gobbonet(
        self,
        prompt_text
    ):

        self.raw_stream_text = ""
        self.parsed_emotion = None

        self.update_sprite(
            "curious"
        )

        self.set_speech_bubble(
            "..."
        )

        threading.Thread(
            target=self._gobbo_worker,
            args=(prompt_text,),
            daemon=True,
        ).start()

    def _gobbo_worker(
        self,
        prompt_text
    ):

        print(
            "\n[GobboBuddy] "
            "========================================",
            flush=True,
        )

        print(
            "[GobboBuddy] MESSAGE:",
            prompt_text,
            flush=True,
        )

        started = time.time()

        try:

            content = (
                GOBBO_BRIDGE
                .send_message(
                    prompt_text
                )
            )

            elapsed = (
                time.time()
                -
                started
            )

            print(
                f"[GobboBuddy] RESPONSE "
                f"after {elapsed:.2f}s:",
                content,
                flush=True,
            )

            self.response_queue.put(
                content
            )

        except Exception as error:

            elapsed = (
                time.time()
                -
                started
            )

            print(
                f"[GobboBuddy] ERROR "
                f"after {elapsed:.2f}s:",
                repr(error),
                flush=True,
            )

            self.response_queue.put(
                f"[GOBBONET ERROR: {error}]"
            )

    def check_queue(self):

        tokens_received = False

        while not self.response_queue.empty():

            token = (
                self.response_queue.get()
            )

            self.raw_stream_text += token

            tokens_received = True

        if tokens_received:

            clean_text = (
                self.raw_stream_text
                .strip()
            )

            if (
                not self.parsed_emotion
                and
                " " in clean_text
            ):

                parts = clean_text.split(
                    " ",
                    1
                )

                candidate = (
                    parts[0]
                    .lower()
                    .replace("[", "")
                    .replace("]", "")
                    .strip()
                )

                if candidate in EMOTION_MAP:

                    self.parsed_emotion = (
                        candidate
                    )

                    self.update_sprite(
                        candidate
                    )

                    clean_text = parts[1]

                else:

                    self.parsed_emotion = (
                        DEFAULT_EMOTION
                    )

                    self.update_sprite(
                        DEFAULT_EMOTION
                    )

            elif (
                self.parsed_emotion
                and
                " " in clean_text
            ):

                parts = clean_text.split(
                    " ",
                    1
                )

                clean_text = parts[1]

            self.set_speech_bubble(
                clean_text
            )

        self.after(
            50,
            self.check_queue
        )

    def on_close(self):

        try:
            GOBBO_BRIDGE.stop()

        except Exception:
            pass

        self.destroy()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        GOBBO_BRIDGE.start()

        GOBBO_BRIDGE.ensure_card_and_thread()

    except Exception as error:

        print(
            "[GobboBuddy] STARTUP ERROR:",
            repr(error),
            flush=True,
        )

        print(
            "\nMake sure:\n"
            "  1. GobboNet is running "
            "(http://127.0.0.1:9066)\n"
            "  2. GOBBONET_PASSWORD matches "
            "your GobboNet password\n"
            "  3. Playwright is installed\n"
            "  4. gobbonet-state-export.json "
            "is beside this script\n"
            f"  5. Character card "
            f"'{CHARACTER_NAME}' exists in "
            "the export\n",
            flush=True,
        )

    app = GobboNetHelper()

    try:
        app.mainloop()

    finally:
        GOBBO_BRIDGE.stop()

