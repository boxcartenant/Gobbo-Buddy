import json
import os
import queue
import threading
import tkinter as tk
from tkinter import simpledialog
import requests
from PIL import Image, ImageTk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid
import time

# ==========================================
# CONFIGURATION
# ==========================================
SPRITE_SHEET_PATH = "gobbo sprites 2.png"  # Path to your sprite sheet
ROWS = 3
COLS = 4

# Map emotion tags to grid coordinates (row, col) - 0-indexed
EMOTION_MAP = {
    "neutral": (0, 0),
    "happy": (0, 1),
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

# API Settings for GobboNet
GOBBONET_API_URL = "http://127.0.0.1:9066/llm/v1/chat/completions"
CHARACTER_NAME = "Fumo"
THREAD_ID = None
GOBBONET_LOGIN_URL = "http://127.0.0.1:9066/login"
GOBBONET_PASSWORD = "YOUR PASSWORD"

#bridge stuff
GOBBO_BRIDGE_PORT = 8765
GOBBO_BRIDGE_LOCK = threading.Lock()
GOBBO_COMMAND_QUEUE = []
GOBBO_RESULT_QUEUE = []

# ==========================================
# Bridge to JS
# ==========================================
class GobboBridgeHandler(BaseHTTPRequestHandler):

    # ==========================================================
    # RESPONSE HELPERS
    # ==========================================================

    def _send_json(self, status, data):

        body = json.dumps(data).encode("utf-8")

        #print(
        #    f"[GobboBridge HTTP] "
        #    f"RESPONSE {status}: {data}",
        #    flush=True
        #)

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        # Allow GobboNet's browser page to communicate with us.
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)


    # ==========================================================
    # CORS / PRIVATE NETWORK ACCESS
    # ==========================================================

    def do_OPTIONS(self):

        #print(
        #    "[GobboBridge HTTP] OPTIONS",
        #    self.path,
        #    flush=True
        #)

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

        # Chromium can require this when a web page makes a
        # request to a local/private-network HTTP server.
        self.send_header(
            "Access-Control-Allow-Private-Network",
            "true"
        )

        self.send_header(
            "Content-Length",
            "0"
        )

        self.end_headers()


    # ==========================================================
    # GET
    # ==========================================================

    def do_GET(self):

        #print(
        #    f"[GobboBridge HTTP] GET {self.path}",
        #    flush=True
        #)


        # ------------------------------------------------------
        # /command
        #
        # GobboNet polls this endpoint looking for work.
        # ------------------------------------------------------

        if self.path.startswith("/command"):

            with GOBBO_BRIDGE_LOCK:

                if GOBBO_COMMAND_QUEUE:

                    command = GOBBO_COMMAND_QUEUE.pop(0)

                    #print(
                    #    "[GobboBridge] "
                    #    ">>> GIVING COMMAND TO GOBBONET:",
                    #    command,
                    #    flush=True
                    #)

                else:

                    command = {}


            self._send_json(
                200,
                command
            )

            return


        # ------------------------------------------------------
        # /result
        #
        # Kept as GET for debugging/manual inspection.
        # Normal operation uses POST /result.
        # ------------------------------------------------------

        if self.path.startswith("/result"):

            with GOBBO_BRIDGE_LOCK:

                if GOBBO_RESULT_QUEUE:

                    result = GOBBO_RESULT_QUEUE.pop(0)

                    #print(
                    #    "[GobboBridge] "
                    #    "<<< GIVING RESULT:",
                    #    result,
                    #    flush=True
                    #)

                else:

                    result = {}


            self._send_json(
                200,
                result
            )

            return


        # ------------------------------------------------------
        # /health
        # ------------------------------------------------------

        if self.path.startswith("/health"):

            self._send_json(
                200,
                {
                    "ok": True,
                    "server": "GobboBridge"
                }
            )

            return


        # ------------------------------------------------------
        # Unknown endpoint.
        # ------------------------------------------------------

        self._send_json(
            404,
            {
                "error": "not found"
            }
        )


    # ==========================================================
    # POST
    # ==========================================================

    def do_POST(self):

        #print(
        #    f"[GobboBridge HTTP] POST {self.path}",
        #    flush=True
        #)


        # ------------------------------------------------------
        # Read request body.
        # ------------------------------------------------------

        try:

            content_length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )


            body = self.rfile.read(
                    content_length
                )


            body_text = body.decode(
                    "utf-8",
                    errors="replace"
                )


            #print(
            #    "[GobboBridge HTTP] BODY:",
            #    body_text,
            #    flush=True
            #)


            data = json.loads(
                    body_text
                )


        except Exception as error:

            print(
                "[GobboBridge] "
                "!!! BAD REQUEST:",
                repr(error),
                flush=True
            )


            self._send_json(
                400,
                {
                    "ok": False,
                    "error": str(error)
                }
            )

            return


        # ------------------------------------------------------
        # /result
        #
        # GobboNet posts completed responses here.
        # ------------------------------------------------------

        if self.path == "/result":

            with GOBBO_BRIDGE_LOCK:

                GOBBO_RESULT_QUEUE.append(
                    data
                )


            #print(
            #    "[GobboBridge] "
            #    "<<< RECEIVED RESULT:",
            #    data,
            #    flush=True
            #)


            #print(
            #    "[GobboBridge] "
            #    "Result queue length:",
            #    len(GOBBO_RESULT_QUEUE),
            #    flush=True
            #)


            self._send_json(
                200,
                {
                    "ok": True
                }
            )

            return


        # ------------------------------------------------------
        # /heartbeat
        #
        # Intentionally removed.
        # We don't need a heartbeat now that the bridge works.
        # ------------------------------------------------------

        if self.path == "/heartbeat":

            self._send_json(
                410,
                {
                    "ok": False,
                    "error": "heartbeat disabled"
                }
            )

            return


        # ------------------------------------------------------
        # Unknown endpoint.
        # ------------------------------------------------------

        self._send_json(
            404,
            {
                "error": "not found"
            }
        )


    # ==========================================================
    # HTTP LOGGING
    # ==========================================================

    def log_message(self, format, *args):
        None
        #print(
        #    "[GobboBridge HTTP]",
        #    format % args,
        #    flush=True
        #)

# ==========================================
# MAIN APPLICATION
# ==========================================
class GobboNetHelper(tk.Tk):

    def __init__(self):
        super().__init__()

        # --- Window Setup ---
        self.overrideredirect(True)  # Frameless
        self.wm_attributes("-topmost", True)  # Always-on-top
        self.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.configure(bg=TRANSPARENT_COLOR)

        # --- Drag State ---
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._is_dragging = False

        # --- Queue & Threading Setup ---
        self.response_queue = queue.Queue()
        self.raw_stream_text = ""
        self.parsed_emotion = None

        # --- Load Sprites ---
        self.sprites = {}
        self.load_sprite_sheet()

        # --- Build UI Layout ---
        self.container = tk.Frame(self, bg=TRANSPARENT_COLOR)
        self.container.pack(fill="both", expand=True)

        # Speech Bubble (Top)
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
            wraplength=250,  # Auto-wraps long LLM responses
            justify="left",
            padx=10,
            pady=8,
        )
        self.bubble_label.pack()
        self.bubble_frame.pack_forget()

        # Sprite Display (Bottom)
        self.sprite_label = tk.Label(
            self.container, bg=TRANSPARENT_COLOR, bd=0, cursor="fleur"
        )
        self.sprite_label.pack()

        # Set initial sprite image
        self.current_emotion = DEFAULT_EMOTION
        self.update_sprite(DEFAULT_EMOTION)

        # --- Event Bindings ---
        self.sprite_label.bind("<ButtonPress-1>", self.on_press)
        self.sprite_label.bind("<B1-Motion>", self.on_drag)
        self.sprite_label.bind("<ButtonRelease-1>", self.on_release)

        # Initial screen positioning
        self.center_on_screen()

        # Start Queue Polling for Streamed UI Updates
        self.check_queue()

        # --- Animation Pipeline Placeholder ---
        # NOTE: Commented out for now as requested.
        # self.after(100, self.animation_tick)

    # ------------------------------------------
    # SPRITE HANDLING
    # ------------------------------------------
    def load_sprite_sheet(self):
        """Cuts the 3x4 sprite sheet into individual PhotoImages mapped by emotion."""
        if not os.path.exists(SPRITE_SHEET_PATH):
            placeholder = Image.new("RGBA", (100, 100), color=(200, 200, 200))
            self.placeholder_img = ImageTk.PhotoImage(placeholder)
            for emotion in EMOTION_MAP:
                self.sprites[emotion] = self.placeholder_img
            print(
                f"Warning: '{SPRITE_SHEET_PATH}' not found. Using placeholder graphics."
            )
            return

        sheet = Image.open(SPRITE_SHEET_PATH).convert("RGBA")
        sheet_w, sheet_h = sheet.size
        sprite_w = sheet_w // COLS
        sprite_h = sheet_h // ROWS

        for emotion, (row, col) in EMOTION_MAP.items():
            left = col * sprite_w
            top = row * sprite_h
            right = left + sprite_w
            bottom = top + sprite_h

            cropped = sheet.crop((left, top, right, bottom))
            resized = cropped.resize((130,170), Image.Resampling.LANCZOS)
            self.sprites[emotion] = ImageTk.PhotoImage(resized)

    def update_sprite(self, emotion):
        """Switch current sprite based on emotion string."""
        emotion = emotion.lower().strip()
        if emotion in self.sprites:
            self.current_emotion = emotion
            self.sprite_label.config(image=self.sprites[emotion])
        else:
            self.sprite_label.config(image=self.sprites[DEFAULT_EMOTION])

    # ------------------------------------------
    # DRAG & CLICK & BOUNDARY CLAMPING
    # ------------------------------------------
    def on_press(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._is_dragging = False

    def on_drag(self, event):
        if (
            abs(event.x - self._drag_start_x) > 3
            or abs(event.y - self._drag_start_y) > 3
        ):
            self._is_dragging = True

        if self._is_dragging:
            x = self.winfo_x() + (event.x - self._drag_start_x)
            y = self.winfo_y() + (event.y - self._drag_start_y)
            self.geometry(f"+{x}+{y}")

    def on_release(self, event):
        if self._is_dragging:
            self.clamp_to_screen_bounds()
            self._is_dragging = False
        else:
            self.open_prompt_dialog()

    def clamp_to_screen_bounds(self):
        """Snaps the helper back inside screen boundaries if dragged off-screen."""
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()

        win_w = self.winfo_width()
        win_h = self.winfo_height()

        x = self.winfo_x()
        y = self.winfo_y()

        new_x = max(0, min(x, screen_w - win_w))
        new_y = max(0, min(y, screen_h - win_h))

        if new_x != x or new_y != y:
            self.geometry(f"+{new_x}+{new_y}")

    def center_on_screen(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{sw // 2 - 50}+{sh // 2 - 50}")

    # ------------------------------------------
    # SPEECH BUBBLE & PROMPT INTERFACE
    # ------------------------------------------
    def open_prompt_dialog(self):
        user_text = simpledialog.askstring("GobboNet", "Say something to Elodine:")
        if user_text:
            self.send_to_gobbonet(user_text)

    def set_speech_bubble(self, text):
        if text.strip():
            self.bubble_label.config(text=text)
            self.bubble_frame.pack(side="top", pady=(0, 6), before=self.sprite_label)
        else:
            self.bubble_frame.pack_forget()

        self.clamp_to_screen_bounds()

    # ------------------------------------------
    # BACKEND STREAMING INTEGRATION
    # ------------------------------------------
    def send_to_gobbonet(self, prompt_text):
        """Prepares state and spins off a thread to query GobboNet API."""
        self.raw_stream_text = ""
        self.parsed_emotion = None

        # Immediate visual feedback
        self.update_sprite("curious")
        self.set_speech_bubble("...")

        # Run network call in background thread
        threading.Thread(
            target=self._stream_worker,
            args=(prompt_text,),
            daemon=True
        ).start()


    def _stream_worker(self, prompt_text):
        """Send a message through the GobboNet browser bridge."""

        command_id = str(uuid.uuid4())

        command = {
            "id": command_id,
            "message": prompt_text,
        }

        print(
            "\n[GobboBridge] ========================================",
            flush=True
        )
        #print("[GobboBridge] QUEUING COMMAND", flush=True)
        #print("[GobboBridge] ID:", command_id, flush=True)
        #print("[GobboBridge] Thread:", THREAD_ID, flush=True)
        #print("[GobboBridge] Message:", prompt_text, flush=True)

        with GOBBO_BRIDGE_LOCK:
            GOBBO_COMMAND_QUEUE.append(command)

            print(
                "[GobboBridge] Queue length:",
                len(GOBBO_COMMAND_QUEUE),
                flush=True
            )

        print(
            "[GobboBridge] Waiting for GobboNet to poll /command...",
            flush=True
        )

        started = time.time()
        next_diagnostic = 5

        while True:

            with GOBBO_BRIDGE_LOCK:

                result = None

                for i, candidate in enumerate(GOBBO_RESULT_QUEUE):

                    if candidate.get("id") == command_id:
                        result = GOBBO_RESULT_QUEUE.pop(i)
                        break

            if result is not None:

                elapsed = time.time() - started

                print(
                    f"[GobboBridge] GOT RESULT after {elapsed:.2f}s:",
                    result,
                    flush=True
                )

                if result.get("ok"):

                    content = result.get("content", "")

                    print(
                        "[GobboBridge] RESPONSE:",
                        content,
                        flush=True
                    )

                    self.response_queue.put(content)

                else:

                    error = result.get(
                        "error",
                        "unknown error"
                    )

                    print(
                        "[GobboBridge] GOBBONET ERROR:",
                        error,
                        flush=True
                    )

                    self.response_queue.put(
                        f" [GOBBONET ERROR: {error}]"
                    )

                return

            elapsed = time.time() - started

            if elapsed >= next_diagnostic:

                with GOBBO_BRIDGE_LOCK:
                    queue_length = len(GOBBO_COMMAND_QUEUE)

                print(
                    f"[GobboBridge] Still waiting... "
                    f"{elapsed:.0f}s | "
                    f"command queue={queue_length}",
                    flush=True
                )

                next_diagnostic += 5

            time.sleep(0.05)


    def check_queue(self):
        """Polls queue for incoming tokens and updates UI incrementally."""
        tokens_received = False

        while not self.response_queue.empty():
            token = self.response_queue.get()
            self.raw_stream_text += token
            tokens_received = True

        if tokens_received:
            clean_text = self.raw_stream_text.strip()

            # Only attempt to parse the emotion once we actually have
            # enough text to contain the first word.
            if not self.parsed_emotion and " " in clean_text:
                parts = clean_text.split(" ", 1)

                candidate = (
                    parts[0]
                    .lower()
                    .replace("[", "")
                    .replace("]", "")
                    .strip()
                )

                if candidate in EMOTION_MAP:
                    self.parsed_emotion = candidate
                    self.update_sprite(candidate)
                    clean_text = parts[1]
                else:
                    self.parsed_emotion = DEFAULT_EMOTION
                    self.update_sprite(DEFAULT_EMOTION)

            elif self.parsed_emotion and " " in clean_text:
                parts = clean_text.split(" ", 1)
                clean_text = parts[1]

            self.set_speech_bubble(clean_text)

        self.after(50, self.check_queue)

    # ------------------------------------------
    # ANIMATION TICK PLACEHOLDER
    # ------------------------------------------
    # def animation_tick(self):
    #     self.after(100, self.animation_tick)


if __name__ == "__main__":
    def start_gobbo_bridge():
        server = ThreadingHTTPServer(
            ("127.0.0.1", GOBBO_BRIDGE_PORT),
            GobboBridgeHandler
        )

        threading.Thread(
            target=server.serve_forever,
            daemon=True
        ).start()

        print(
            f"GobboBridge listening on "
            f"http://127.0.0.1:{GOBBO_BRIDGE_PORT}"
        )

        return server
    gobbo_bridge_server = start_gobbo_bridge()
    app = GobboNetHelper()
    app.mainloop()
