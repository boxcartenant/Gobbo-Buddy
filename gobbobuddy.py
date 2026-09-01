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
SPRITE_SHEET_PATH = "gobbo sprites 2.png"
MAX_BUBBLE_LINES = 30
ROWS, COLS = 3, 4

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
GOBBONET_PASSWORD = "MY PASSWORD"
LLM_DIRECT_BASE = "http://127.0.0.1:11437"
LLM_DIRECT_TIMEOUT = 45
GENERATION_TIMEOUT = 600
PAGE_READY_TIMEOUT = 60

# ============================================================
# LIGHTWEIGHT OS WEBVIEW BRIDGE (replaces playwright)
# ============================================================
class GobboNetWebViewBridge:
    def __init__(self):
        self.window = None
        self.ready_event = threading.Event()

    def start_window(self):
        # No more profile wiping. private_mode handles the "start clean" requirement safely.
        self.window = webview.create_window(
            'GobboNet Engine',
            GOBBONET_BASE_URL,
            hidden=True,
            width=1024,
            height=768
        )
        self.window.events.loaded += self._on_loaded

        # private_mode=True → no persistent localStorage / IndexedDB / cookies
        # between launches. GobboNet therefore always sees an empty local state
        # and auto-restores from the server backup on every start.
        webview.start(
            private_mode=True
            # storage_path is ignored / irrelevant when private_mode=True
        )

    def _on_loaded(self):
        # Auto-login if login screen is active
        self.window.evaluate_js(f"""
            (function() {{
                const pw = document.querySelector('input[name="password"]');
                if (pw) {{
                    pw.value = "{GOBBONET_PASSWORD}";
                    const btn = document.querySelector('button[type="submit"], input[type="submit"]');
                    if (btn) btn.click();
                }}
            }})();
        """)
        
        for _ in range(30):
            ready = self.window.evaluate_js("""
                !!(document.querySelector('textarea, input[type="text"], #chat-input, .chat-input'))
            """)
            if ready:
                logger.info("GobboNet Engine successfully connected via WebView2.")
                self.ready_event.set()
                return
            time.sleep(1)

    def eval_js(self, script):
        return self.window.evaluate_js(script)

    def send_message(self, prompt_text):
        js_prompt = json.dumps(prompt_text)
        
        script = f"""
            window.__gobboBuddyLastAssistant = null;
            window.__gobboBuddyError = null;
            window.__gobboBuddyDone = false;
            
            (async () => {{
                try {{
                    const findInput = () => {{
                        return document.querySelector('textarea') ||
                               document.querySelector('input[type="text"]') ||
                               document.querySelector('[contenteditable="true"]') ||
                               document.querySelector('#chat-input, .chat-input, #message-input, .message-input');
                    }};

                    // Wait up to 5 seconds for the chat input to appear in the DOM
                    let input = findInput();
                    let attempts = 0;
                    while (!input && attempts < 20) {{
                        await new Promise(r => setTimeout(r, 250));
                        input = findInput();
                        attempts++;
                    }}

                    const getMsgs = () => {{
                        if (typeof getActiveThread === 'function') return getActiveThread()?.messages || [];
                        if (window.state?.activeThread?.messages) return window.state.activeThread.messages;
                        if (window.state?.threads && window.state?.activeThreadId) {{
                            const t = window.state.threads.find(x => x.id === window.state.activeThreadId);
                            if (t) return t.messages || [];
                        }}
                        return Array.from(document.querySelectorAll('.message, .chat-message, [data-role], .msg'));
                    }};

                    const beforeCount = getMsgs().length;

                    // 1. Try global functions first
                    if (typeof sendMessage === 'function') {{
                        sendMessage({js_prompt});
                    }} 
                    else if (typeof window.sendMessage === 'function') {{
                        window.sendMessage({js_prompt});
                    }}
                    // 2. Fall back to DOM manipulation
                    else if (input) {{
                        input.focus();
                        if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {{
                            // Trigger React / Vue state setters if active
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, "value"
                            )?.set || Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, "value"
                            )?.set;

                            if (nativeSetter) {{
                                nativeSetter.call(input, {js_prompt});
                            }} else {{
                                input.value = {js_prompt};
                            }}
                        }} else {{
                            input.innerText = {js_prompt};
                        }}

                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));

                        const sendBtn = document.querySelector('button[type="submit"], #send-btn, .send-button, button:has(svg)');
                        if (sendBtn) {{
                            sendBtn.click();
                        }} else {{
                            input.dispatchEvent(new KeyboardEvent('keydown', {{ 
                                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true 
                            }}));
                        }}
                    }} else {{
                        throw new Error("Could not locate any active chat input or global sendMessage function in GobboNet DOM.");
                    }}

                    // Poll for complete output
                    const check = setInterval(() => {{
                        const isGen = typeof isGenerating !== 'undefined' ? isGenerating : 
                            !!(document.querySelector('.generating, .loading, .spinner, button[title*="Stop"]'));
                        
                        let lastText = "";
                        const msgs = getMsgs();
                        
                        if (msgs.length > 0) {{
                            const lastMsg = msgs[msgs.length - 1];
                            if (typeof lastMsg === 'object' && lastMsg.content) {{
                                lastText = lastMsg.content;
                            }} else if (lastMsg instanceof HTMLElement) {{
                                lastText = lastMsg.innerText || lastMsg.textContent;
                            }}
                        }}

                        if (!isGen && lastText && (msgs.length > beforeCount || attempts > 0)) {{
                            clearInterval(check);
                            window.__gobboBuddyLastAssistant = lastText;
                            window.__gobboBuddyDone = true;
                        }}
                    }}, 300);

                }} catch(err) {{
                    window.__gobboBuddyError = String(err);
                    window.__gobboBuddyDone = true;
                }}
            }})();
        """
        self.window.evaluate_js(script)

        deadline = time.time() + GENERATION_TIMEOUT
        while time.time() < deadline:
            done = self.window.evaluate_js("window.__gobboBuddyDone")
            if done:
                err = self.window.evaluate_js("window.__gobboBuddyError")
                if err:
                    raise RuntimeError(f"GobboNet Generation Failed: {err}")
                return self.window.evaluate_js("window.__gobboBuddyLastAssistant")
            time.sleep(0.2)

        raise RuntimeError("Generation timed out.")

    def create_new_thread(self):
        return self.eval_js("""
            (() => {
                if (typeof createNewThread === "function") return createNewThread();
                if (typeof createThread === "function") return createThread();
                if (window.state && typeof window.createThread === "function") return window.createThread();
                
                const newBtn = document.querySelector('#new-thread-btn, .new-chat-btn, button[title*="New"]');
                if (newBtn) { newBtn.click(); return true; }
                return false;
            })()
        """)

    def stop_generation(self):
        return self.eval_js("""
            (() => {
                if (typeof stopGeneration === "function") { stopGeneration(); return true; }
                const btn = document.querySelector("#stop-btn, button.stop-generation, .btn-stop");
                if (btn) { btn.click(); return true; }
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
        "Choose the emotion that best matches the situation described.\n"
        "Sometimes the message will be judging or snarky. Try to find the emotion of the message.\n\n"

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

    grammar = (
        'root ::= emotion\n'
        'emotion ::= "[NEUTRAL]" | "[HAPPY]" | "[JOKING]" | "[CURIOUS]" | "[SNARKY]" | '
        '"[EXCITED]" | "[SKEPTICAL]" | "[JUDGING]" | "[SHOCKED]" | "[SAD]" | '
        '"[ANXIOUS]" | "[EMBARRASSED]" | "[FLATTERED]"'
    )

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
        self.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
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

        self.load_sprite_sheet()
        self.container = tk.Frame(self, bg=TRANSPARENT_COLOR)
        self.container.pack(fill="both", expand=True)

        # Bubble Frame
        self.bubble_frame = tk.Frame(self.container, bg="white", highlightbackground="black", highlightthickness=2, bd=0)
        self.bubble_text = tk.Text(self.bubble_frame, bg="white", fg="black", font=("Arial", 10), wrap="word", width=30, height=1, bd=0, relief="flat", highlightthickness=0, cursor="ibeam", padx=8, pady=6)
        self.bubble_text.pack()
        self.bubble_text.config(state="disabled")
        self.bubble_text.bind("<MouseWheel>", self.on_text_mousewheel)

        # Tail Canvas
        self.tail_canvas = tk.Canvas(self.container, width=24, height=12, bg=TRANSPARENT_COLOR, highlightthickness=0, bd=0)
        self.tail_canvas.create_polygon(2, 0, 22, 0, 12, 11, fill="white", outline="black", width=2)
        self.tail_canvas.create_line(3, 0, 21, 0, fill="white", width=3)

        # Sprite Label
        self.sprite_label = tk.Label(self.container, bg=TRANSPARENT_COLOR, bd=0, cursor="fleur")
        self.sprite_label.pack()

        self.current_emotion = DEFAULT_EMOTION
        self.update_sprite(DEFAULT_EMOTION)

        # Mouse Bindings
        self.sprite_label.bind("<ButtonPress-1>", self.on_press)
        self.sprite_label.bind("<B1-Motion>", self.on_drag)
        self.sprite_label.bind("<ButtonRelease-1>", self.on_release)
        for w in (self.sprite_label, self.bubble_frame, self.bubble_text, self.tail_canvas):
            w.bind("<Button-3>", self.show_context_menu)

        self.center_on_screen()
        self.update_sprite_anchor()
        self.check_queue()
        self._tick_proactive()

    def _schedule_next_opportunity(self):
        delay = random.uniform(RANDOM_WINDOW_MIN_SECONDS, RANDOM_WINDOW_MAX_SECONDS)
        #print(f"Proactive: next opportunity at {delay}")
        self._next_opportunity_at = time.time() + delay

    def _note_message_exchanged(self):
        self._last_message_time = time.time()
        self._schedule_next_opportunity()

    def _tick_proactive(self):
        try:
            if HAS_ACCESSIBILITY and self._next_opportunity_at and time.time() >= self._next_opportunity_at and not self._proactive_busy:
                if random.random() < RANDOM_WINDOW_PROBABILITY:
                    threading.Thread(target=self._proactive_worker, daemon=True).start()
                self._schedule_next_opportunity()
        except Exception as e:
            logger.error(f"Proactive Tick Error: {e}")
        self.after(2000, self._tick_proactive)

    def _proactive_worker(self):
        with self._proactive_lock:
            if self._proactive_busy: return
            self._proactive_busy = True
        try:
            info = get_active_window_info()
            if info and not ("python" in info["app_name"].lower() and "gobbo" in info["title"].lower()):
                summary = summarize_with_direct_gguf(info["app_name"], info["title"], info["content"])
                self.send_to_gobbonet(build_simple_user_message(info["app_name"], info["title"], summary))
        finally:
            self._proactive_busy = False

    def load_sprite_sheet(self):
        if not os.path.exists(SPRITE_SHEET_PATH):
            placeholder = Image.new("RGBA", (100, 100), color=(200, 200, 200))
            self.placeholder_img = ImageTk.PhotoImage(placeholder)
            for emotion in EMOTION_MAP: self.sprites[emotion] = self.placeholder_img
            return

        sheet = Image.open(SPRITE_SHEET_PATH).convert("RGBA")
        sw, sh = sheet.size
        sp_w, sp_h = sw // COLS, sh // ROWS
        for emotion, (r, c) in EMOTION_MAP.items():
            cropped = sheet.crop((c * sp_w, r * sp_h, (c + 1) * sp_w, (r + 1) * sp_h))
            self.sprites[emotion] = ImageTk.PhotoImage(cropped.resize((130, 170), Image.Resampling.LANCZOS))

    def update_sprite(self, emotion):
        emotion = emotion.lower().strip()
        self.current_emotion = emotion if emotion in self.sprites else DEFAULT_EMOTION
        self.sprite_label.config(image=self.sprites[self.current_emotion])

    def on_press(self, event):
        self._drag_start_x, self._drag_start_y = event.x, event.y
        self._is_dragging = False

    def on_drag(self, event):
        if abs(event.x - self._drag_start_x) > 3 or abs(event.y - self._drag_start_y) > 3:
            self._is_dragging = True
            x = self.winfo_x() + (event.x - self._drag_start_x)
            y = self.winfo_y() + (event.y - self._drag_start_y)
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
        if self._sprite_anchor_x is None: return
        self.update_idletasks()
        new_x = self._sprite_anchor_x - self.sprite_label.winfo_x()
        new_y = self._sprite_anchor_y - self.sprite_label.winfo_y()
        self.geometry(f"+{new_x}+{new_y}")

    def clamp_to_screen_bounds(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{max(0, min(self.winfo_x(), sw - ww))}+{max(0, min(self.winfo_y(), sh - wh))}")

    def center_on_screen(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{sw // 2 - 65}+{sh // 2 - 85}")

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

    def open_prompt_dialog(self):
        user_text = simpledialog.askstring("GobboNet", "Say something:")
        if user_text: self.send_to_gobbonet(user_text)

    def send_to_gobbonet(self, prompt_text):
        self.raw_stream_text = ""
        self.parsed_emotion = None
        self.update_sprite("curious")
        self.set_speech_bubble("...")
        self._note_message_exchanged()

        threading.Thread(target=self._gobbo_worker, args=(prompt_text,), daemon=True).start()

    def _gobbo_worker(self, prompt_text):
        try:
            content = GOBBO_BRIDGE.send_message(prompt_text)

            # Hybrid step: classify emotion with the direct tiny model + GBNF
            emotion = classify_emotion_with_direct_gguf(content)

            # Strip a leading [EMOTION] tag if GobboNet already emitted one
            clean = content.strip()
            if clean.startswith("[") and "]" in clean[:20]:
                clean = clean.split("]", 1)[1].lstrip()

            self.response_queue.put({"emotion": emotion, "text": clean})
        except Exception as error:
            self.response_queue.put({
                "emotion": DEFAULT_EMOTION,
                "text": f"[GOBBONET ERROR: {error}]"
            })

    def check_queue(self):
        while not self.response_queue.empty():
            item = self.response_queue.get()

            if isinstance(item, dict):
                emotion = item.get("emotion", DEFAULT_EMOTION)
                text = item.get("text", "")
            else:
                # Safety fallback for any unexpected string
                emotion = DEFAULT_EMOTION
                text = str(item)

            self.parsed_emotion = emotion
            self.update_sprite(emotion)
            self.set_speech_bubble(text)
            self._note_message_exchanged()

        self.after(50, self.check_queue)

    def show_context_menu(self, event):
        menu = tk.Menu(self, tearoff=False)
        persona_menu = tk.Menu(menu, tearoff=False)

        cards = GOBBO_BRIDGE.eval_js("(state.characterCards || []).map(c => ({ id: c.id, name: c.name || 'Unnamed' }))") or []
        if cards:
            for card in cards:
                persona_menu.add_command(label=card.get("name"), command=lambda cid=card.get("id"): self.select_character(cid))
        else:
            persona_menu.add_command(label="No characters found", state="disabled")

        menu.add_cascade(label="Persona", menu=persona_menu)
        menu.add_separator()
        menu.add_command(label="New Thread", command=self.new_thread)
        menu.add_command(label="Stop Generation", command=self.stop_generation)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.destroy)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def select_character(self, card_id):
        GOBBO_BRIDGE.eval_js(f"if (typeof activateCard === 'function') activateCard('{card_id}');")
        self.new_thread()

    def new_thread(self):
        GOBBO_BRIDGE.create_new_thread()
        self.set_speech_bubble("")
        self.update_sprite("neutral")
        self._note_message_exchanged()

    def stop_generation(self):
        GOBBO_BRIDGE.stop_generation()

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
