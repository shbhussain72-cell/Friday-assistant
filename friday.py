"""Friday: voice control and personal assistant for this PC.

Mic -> utterances -> Whisper. Anything that mentions "Friday" (start, middle or end), or follows
"Hey Friday" / Ctrl+Alt+J, goes to a Claude process that stays running and streams its reply line by
line: spoken lines are voiced sentence by sentence while the rest is still being written, action lines
run in order. Talking over Friday interrupts him.

Memory lives in data/: history.jsonl (every line, forever), memory.md (facts he asked to keep),
digests.json (a summary per day), feedback.md (his feedback on Friday), notes/ (saved dictation).
Big jobs, including changes to Friday's own code, go to a background Claude Code agent.

    pythonw friday.py                 run (tray icon + overlay); starts at login
    python  friday.py --text "..."    run one command typed instead of spoken
    python  friday.py --say  "..."    speak a line in Friday's voice
"""
import base64
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import difflib
import itertools
import json
import logging
import os
import queue
import random
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import Future
from logging.handlers import RotatingFileHandler

import numpy as np
import requests

import backup_brain
import care
import devices
import google_conn
import progress
import tone
from backup_brain import LIMIT

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "friday.log")
DATA = os.path.join(HERE, "data")
NO_WINDOW = 0x08000000
RATE = 16000
CHUNK = 1280  # 80 ms, what openWakeWord expects
user32 = ctypes.windll.user32

log = logging.getLogger("friday")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()


def level_of(chunk):
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))


# ---------------------------------------------------------------- UI: overlay + tray

# ---------------------------------------------------------------- Gujarati script -> Latin (captions)

_GU_C = {"ક": "k", "ખ": "kh", "ગ": "g", "ઘ": "gh", "ઙ": "n", "ચ": "ch", "છ": "chh", "જ": "j", "ઝ": "z", "ઞ": "n",
         "ટ": "t", "ઠ": "th", "ડ": "d", "ઢ": "dh", "ણ": "n", "ત": "t", "થ": "th", "દ": "d", "ધ": "dh", "ન": "n",
         "પ": "p", "ફ": "f", "બ": "b", "ભ": "bh", "મ": "m", "ય": "y", "ર": "r", "લ": "l", "વ": "v", "શ": "sh",
         "ષ": "sh", "સ": "s", "હ": "h", "ળ": "l"}
_GU_V = {"અ": "a", "આ": "aa", "ઇ": "i", "ઈ": "i", "ઉ": "u", "ઊ": "u", "ઋ": "ru", "ઍ": "e", "એ": "e", "ઐ": "ai",
         "ઑ": "o", "ઓ": "o", "ઔ": "au"}
_GU_M = {"ા": "aa", "િ": "i", "ી": "i", "ુ": "u", "ૂ": "u", "ૃ": "ru", "ૅ": "e", "ે": "e", "ૈ": "ai", "ૉ": "o",
         "ો": "o", "ૌ": "au"}
# Devanagari (the script her LSD lines are written in: the Hindi-Urdu voice reads it)
_GU_C.update({"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
              "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
              "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
              "ष": "sh", "स": "s", "ह": "h", "ळ": "l", "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "dh", "फ़": "f"})
_GU_V.update({"अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ru", "ऍ": "e", "ए": "e", "ऐ": "ai",
              "ऑ": "o", "ओ": "o", "औ": "au"})
_GU_M.update({"ा": "aa", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ru", "ॅ": "e", "े": "e", "ै": "ai", "ॉ": "o",
              "ो": "o", "ौ": "au"})
GU_SCRIPT = re.compile(r"[\u0A80-\u0AFF\u0900-\u097F]")
_AR = "\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF"
AR_SCRIPT = re.compile(f"[{_AR}]")
# splits a line into Arabic-script stretches (words plus the spaces, digits and punctuation between them) and the rest
AR_SPLIT = re.compile(f"([{_AR}][{_AR}\\s\\d.,:;!?()\u200C-\u200F-]*)")


def _translit_word(w):
    """One Gujarati word the way mumineen text it: consonant + vowel units, the unwritten 'a' dropped at the end
    of a word and inside it between vowels ("khidmat", "karvu", not "khidamata")."""
    units = []  # [consonant, vowel or None (unwritten a), nasal]
    for ch in w:
        if ch in _GU_C:
            units.append([_GU_C[ch], None, False])
        elif ch in _GU_V:
            units.append(["", _GU_V[ch], False])
        elif ch in _GU_M and units:
            units[-1][1] = _GU_M[ch]
        elif ch in ("\u0ACD", "\u094D") and units:   # virama: no vowel
            units[-1][1] = ""
        elif ch in ("\u0A82", "\u0A81", "\u0902", "\u0901") and units:  # anusvara / chandrabindu
            units[-1][2] = True
        elif ch in ("\u0A83", "\u0903") and units:   # visarga
            units[-1][0] += "h"
        elif ch in ("\u0ABC", "\u093C") and units:   # nukta: ज़ z, फ़ f, क़ q
            units[-1][0] = {"j": "z", "k": "q", "ph": "f"}.get(units[-1][0], units[-1][0])
        else:
            units.append([ch, "", False])
    out = []
    prev_v = ""  # the vowel actually written for the unit before
    for i, (c, v, nasal) in enumerate(units):
        if v is None:  # unwritten 'a': dropped at the end, and between a vowel and a consonant that has one
            last = i == len(units) - 1
            nxt = units[i + 1] if not last else None
            nxt_has_vowel = nxt is not None and nxt[0] != "" and (nxt[1] if nxt[1] is not None else i + 1 < len(units) - 1)
            v = "" if last or (prev_v and nxt_has_vowel) else "a"
        prev_v = v
        piece = c + v
        if nasal:
            nxt = units[i + 1][0] if i + 1 < len(units) else ""
            if nxt:
                piece += "m" if nxt[0] in "pb" else "n"
        out.append(piece)
    return "".join(out)


def translit_gu(text):
    """Gujarati script in a caption -> Latin letters, the way the community writes LSD in chat. Other text is
    untouched, so English words, names and numbers in the line stay as they are."""
    if not text or not GU_SCRIPT.search(text):
        return text
    out = re.sub(r"[\u0A80-\u0AFF\u0900-\u097F]+", lambda m: _translit_word(m.group(0)), text).replace("\u0964", ".")
    for a, b in _GU_FIX:  # the handful of words with a settled spelling
        out = re.sub(rf"\b{a}\b", b, out, flags=re.I)
    return out


_GU_FIX = (("sar", "sir"), (r"fraa?[iy]d?e", "Friday"), ("inshaa?allaah", "inshallah"), ("alhamdulillaah", "alhamdulillah"),
           ("alhamdolillaah", "alhamdolillah"), ("maashaa?allaah", "mashallah"), ("khudaa haafiz", "khuda hafiz"),
           ("chhe", "che"), ("chhu", "chu"), ("chho", "cho"), ("nahi", "nai"), ("nathi", "nthi"), ("vakt", "waqt"),
           ("shu", "su"), ("shukran", "sukran"), ("saaheb", "saheb"))


class Overlay:
    """A small card in the bottom-right corner showing what Friday heard and is doing.
    It must never take keyboard focus, or typed text would land in it instead of his window."""

    COLORS = {"idle": "#3b82f6", "listening": "#22c55e", "thinking": "#f59e0b",
              "speaking": "#a855f7", "paused": "#6b7280", "error": "#ef4444"}

    def __init__(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.9)
        self.win.configure(bg="#111318")
        frame = tk.Frame(self.win, bg="#111318", padx=12, pady=8)
        frame.pack()
        self.dot = tk.Canvas(frame, width=12, height=12, bg="#111318", highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 10, 10, fill=self.COLORS["idle"], outline="")
        self.dot.pack(side="left", padx=(0, 8), anchor="n", pady=4)
        self.label = tk.Label(frame, text="", fg="#e5e7eb", bg="#111318",
                              font=("Segoe UI", 9), wraplength=340, justify="left")
        self.label.pack(side="left")
        self.win.geometry("+-2000+-2000")
        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id())
        # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_TRANSPARENT (clicks pass through)
        style = user32.GetWindowLongW(self.hwnd, -20) | 0x08000000 | 0x80 | 0x8 | 0x20 | 0x80000
        user32.SetWindowLongW(self.hwnd, -20, style)
        user32.ShowWindow(self.hwnd, 0)
        self._hide_job = None
        self.q = queue.Queue()
        self.root.after(50, self._pump)
        self.pill = TaskPill(self.root)  # "task ongoing" pill at the top (progress.py)
        progress.listeners.append(self.pill.update)

    def show(self, state, text, hide_after=None):
        self.q.put((state, translit_gu(text), hide_after))  # captions in Latin letters when she talks LSD

    def _hide(self):
        user32.ShowWindow(self.hwnd, 0)

    @staticmethod
    def _work_area():
        """The visible part of the screen he's working on (without the taskbar or dock), in real pixels: the card
        used the whole primary screen and ended up under the dock or off the edge."""
        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]
        try:
            mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), 2)  # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                return r.l, r.t, r.r, r.b
        except Exception:
            pass
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1) - 64

    def _pump(self):
        try:
            while True:
                state, text, hide_after = self.q.get_nowait()
                if state == "__quit__":
                    self.root.destroy()
                    return
                if not text:
                    self._hide()
                    continue
                self.dot.itemconfig(self.dot_id, fill=self.COLORS.get(state, "#3b82f6"))
                text = text if len(text) < 220 else text[:220] + "…"
                # a long word with no spaces (a link, a path) can't wrap and pushed the card off the screen
                text = re.sub(r"\S{28,}", lambda m: "\u200b".join(m.group(0)[i:i + 24] for i in range(0, len(m.group(0)), 24)), text)
                self.label.config(text=text)
                self.win.update_idletasks()
                w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
                left, top, right, bottom = self._work_area()
                w = min(w, right - left - 32)
                x, y = max(left + 16, right - w - 16), max(top + 16, bottom - h - 16)
                self.win.geometry(f"{w}x{h}")
                # placed by Windows itself: Tk kept the card at its parking spot (or wherever it last was) for this
                # no-activate tool window. SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW
                user32.SetWindowPos(self.hwnd, None, x, y, w, h, 0x0004 | 0x0010 | 0x0040)  # NOZORDER (Tk keeps it topmost)
                if self._hide_job:
                    self.root.after_cancel(self._hide_job)
                    self._hide_job = None
                self._hide_job = self.root.after(int((hide_after or 12) * 1000), self._hide)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def run(self):
        self.root.mainloop()

    def quit(self):
        self.q.put(("__quit__", "", None))


class TaskPill:
    """The floating "task ongoing" pill at the top of the screen (his pick, 12 Oct): what she's doing and the current
    step, and during a web search the sites she's reading as chips with their icons, like ChatGPT voice mode. Stop
    ends the task mid-way; ✕ hides it until the next task. Lives on the caption card's Tk thread and never takes focus. Fed by progress.py."""
    BG, FG, MUTED, CHIP, ACCENT, OK = "#15171d", "#f1f3f6", "#9aa3b2", "#262a33", "#a78bfa", "#6dd58c"

    def __init__(self, root):
        import tkinter as tk
        self.tk, self.root = tk, root
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.96)
        self.win.configure(bg=self.BG)
        body = tk.Frame(self.win, bg=self.BG, padx=16, pady=10)
        body.pack(fill="both", expand=True)
        top = tk.Frame(body, bg=self.BG)
        top.pack(fill="x")
        self.spark = tk.Label(top, text="✦", fg=self.ACCENT, bg=self.BG, font=("Segoe UI Symbol", 11))
        self.spark.pack(side="left", padx=(0, 8))
        self.title = tk.Label(top, text="", fg=self.FG, bg=self.BG, font=("Segoe UI Semibold", 10), anchor="w")
        self.title.pack(side="left")
        self.count = tk.Label(top, text="", fg=self.MUTED, bg=self.BG, font=("Segoe UI", 9))
        self.count.pack(side="left", padx=(6, 0))
        close = tk.Label(top, text="✕", fg=self.MUTED, bg=self.BG, font=("Segoe UI", 10), cursor="hand2", padx=4)
        close.pack(side="right", padx=(16, 0))
        close.bind("<Button-1>", lambda e: self._close())
        self.stopb = tk.Label(top, text="Stop", fg="#ff8a80", bg=self.BG, font=("Segoe UI Semibold", 9), cursor="hand2", padx=6)
        self.stopb.pack(side="right", padx=(16, 0))
        self.stopb.bind("<Button-1>", lambda e: self._stop())
        self.stepl = tk.Label(body, text="", fg=self.MUTED, bg=self.BG, font=("Segoe UI", 9), anchor="w", justify="left")
        self.stepl.pack(fill="x", padx=(22, 0), pady=(2, 0))
        self.chips = tk.Frame(body, bg=self.BG)
        self.chips.pack(fill="x", padx=(22, 0))
        self.win.geometry("+-2000+-2000")
        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id())
        # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED: clickable (the ✕), never focused
        user32.SetWindowLongW(self.hwnd, -20, user32.GetWindowLongW(self.hwnd, -20) | 0x08000000 | 0x80 | 0x8 | 0x80000)
        try:  # Windows 11 rounded corners
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
        except Exception:
            pass
        user32.ShowWindow(self.hwnd, 0)
        self.q, self.icons, self.fetching, self.closed = queue.Queue(), {}, set(), set()
        self.items, self.current, self.shown_key = [], None, None
        root.after(80, self._pump)

    def update(self, items):
        self.q.put(("items", items))

    def _stop(self):
        """Stop (his 13 Oct ask): ends the running task mid-way, same as her window's Stop or saying "stop"."""
        log.info("task pill: Stop pressed (%s)", (self.current or {}).get("title", ""))
        f = globals().get("friday")
        if f is not None:
            threading.Thread(target=f.panel_stop, daemon=True).start()

    def _close(self):
        if self.current:
            self.closed.add(self.current["id"])
        user32.ShowWindow(self.hwnd, 0)
        self.shown_key = None

    def _icon(self, domain):
        """The site's icon, fetched once in the background (Google's favicon service), a letter until then."""
        if domain in self.icons or domain in self.fetching:
            return
        self.fetching.add(domain)

        def fetch():
            try:
                r = requests.get(f"https://www.google.com/s2/favicons?domain={domain}&sz=16", timeout=6)
                if r.ok and r.content[:4] == b"\x89PNG":
                    self.q.put(("icon", domain, r.content))
            except requests.RequestException:
                pass
        threading.Thread(target=fetch, daemon=True).start()

    def _pump(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "items":
                    self.items = msg[1]
                elif msg[0] == "icon":
                    try:
                        self.icons[msg[1]] = self.tk.PhotoImage(data=base64.b64encode(msg[2]))
                        self.shown_key = None  # redraw with the icon
                    except Exception:
                        self.icons[msg[1]] = None
                self._render()
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _render(self):
        tk = self.tk
        live = [t for t in self.items if t["id"] not in self.closed]
        running = [t for t in live if not t["ended"]]
        cur = (running or live)[-1] if live else None  # a running task over one that just finished
        self.current = cur
        if not cur:
            user32.ShowWindow(self.hwnd, 0)
            self.shown_key = None
            return
        key = json.dumps([cur["title"], cur["step"], cur["n"], [s["domain"] for s in cur["sites"]], bool(cur["ended"]),
                          len(running), sorted(d for d in self.icons)], sort_keys=True)
        if key == self.shown_key:
            return
        self.shown_key = key
        web = cur["kind"] == "web"
        self.title.config(text=("Searching the web" if web and not cur["title"] else cur["title"])[:60])
        more = f" · +{len(running) - 1} more" if len(running) > 1 else ""
        self.count.config(text=(f"· step {cur['n']}" if cur["n"] and not web and not cur["ended"] else "") + more)
        step = cur["step"] or ("Looking it up…" if web else "Working on it…")
        if cur["ended"]:
            step = ("✓ " if cur["ok"] else "") + step
        self.stepl.config(text=step[:90], fg=self.OK if cur["ended"] and cur["ok"] else self.MUTED)
        self.spark.config(fg=self.OK if cur["ended"] and cur["ok"] else self.ACCENT)
        for w in self.chips.winfo_children():
            w.destroy()
        row = None
        for i, s in enumerate(cur["sites"][:6]):
            if i % 3 == 0:
                row = tk.Frame(self.chips, bg=self.BG)
                row.pack(fill="x", pady=(6 if i == 0 else 4, 0))
            self._icon(s["domain"])
            chip = tk.Frame(row, bg=self.CHIP, padx=8, pady=3)
            chip.pack(side="left", padx=(0, 6))
            img = self.icons.get(s["domain"])
            if img is not None:
                tk.Label(chip, image=img, bg=self.CHIP).pack(side="left", padx=(0, 5))
            else:
                tk.Label(chip, text=s["domain"][:1].upper(), fg=self.FG, bg="#3a3f4b", font=("Segoe UI Semibold", 8),
                         width=2).pack(side="left", padx=(0, 5))
            tk.Label(chip, text=s["domain"][:28], fg=self.FG, bg=self.CHIP, font=("Segoe UI", 9)).pack(side="left")
        self.win.update_idletasks()
        w, h = max(self.win.winfo_reqwidth(), 300), self.win.winfo_reqheight()
        left, top, right, bottom = Overlay._work_area()
        w = min(w, right - left - 32)
        x, y = left + (right - left - w) // 2, top + 14
        self.win.geometry(f"{w}x{h}")
        user32.SetWindowPos(self.hwnd, None, x, y, w, h, 0x0004 | 0x0010 | 0x0040)


_ICONS = {}


def make_icon(color):
    """Friday's mark (logo.py) for the tray; the ring takes the colour of what she's doing."""
    if color not in _ICONS:
        import logo
        rgb = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        _ICONS[color] = logo.render(64, ring=(rgb, logo.VIOLET))
    return _ICONS[color]


# ---------------------------------------------------------------- speech out

def master(samples, rate, bass_db=4.0, loudness_db=-13.0):
    """Gentle low-shelf warmth, then loudness: bring the speech RMS up to `loudness_db` dBFS and
    soft-limit the peaks so it's loud without clipping (TTS comes out quiet, around -24 dBFS)."""
    from scipy.signal import butter, sosfilt
    x = samples.astype(np.float32) / 32768.0
    if bass_db > 0:
        low = sosfilt(butter(2, 180.0, "low", fs=rate, output="sos"), x)
        x = x + low * (10 ** (bass_db / 20) - 1)
    voiced = x[np.abs(x) > 0.01]
    rms = float(np.sqrt(np.mean(voiced ** 2))) if voiced.size else 0.05
    x = x * (10 ** (loudness_db / 20) / max(rms, 1e-4))
    x = np.tanh(x * 1.2) / np.tanh(1.2)  # soft limiter
    return (np.clip(x, -1, 1) * 32767 * 0.98).astype(np.int16)


SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=\S)")
# document and media types only: code ones (.js, .py) also appear inside names like "React.js"
EMOTE = re.compile(r"\[(laugh|chuckle|sigh|gasp|groan|cough|sniffle|yawn)\]", re.I)  # her expressive-voice cues
# the brain almost never writes cue tags itself (2 all day), so a line that opens with a reaction gets one here:
# "Haha, ..." laughs, "Aww ..." sighs, "Oh my god ..." gasps, "Ugh ..." groans
CUES = [(re.compile(r"^(?:oh\s+)?(?:ha(?:ha)+|hah|lol|lmao+|lmfao|rofl|ha)\b", re.I), "laugh"),
        (re.compile(r"^(?:hehe+|heh|lowkey funny)\b", re.I), "chuckle"),
        (re.compile(r"^(?:a+w+|phew|sigh|oh no|oh man|man,)", re.I), "sigh"),
        (re.compile(r"^(?:ugh+|oof|bruh|fml|seriously\?)", re.I), "groan"),
        (re.compile(r"^(?:oh my god|omg|oh wow|wow|whoa+|woah|oh shit|holy shit|holy|no way|wait what|lesgo+|let'?s go+|yoo+|yay)\b", re.I), "gasp")]


def auto_cue(text):
    """'Aww, that's sweet, sir.' -> '[sigh] Aww, that's sweet, sir.', so it's said in her expressive voice."""
    if EMOTE.search(text):
        return text
    for pat, cue in CUES:
        if pat.match(text.strip()):
            return f"[{cue}] {text.strip()}"
    return text
PATH_START = r"(?:\b[A-Za-z]:[\\/]|%\w+%[\\/]|\$env:\w+[\\/]|(?<!\w)~[\\/])"
FILE_EXT = r"\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|png|jpe?g|gif|webp|heic|mp[34]|mov|wav|m4a|zip|rar|7z|exe|msi|lnk)\b"


def speakable_names(text):
    """Never read a path out: "C:\\Users\\Dell\\Downloads\\Poster - QA.pdf" becomes "Poster - QA"."""
    # a path runs until a comma, quote or line end, or the next path; only its last part is kept
    t = re.sub(rf"{PATH_START}(?:(?!{PATH_START})[^\n,;\"'])*", lambda m: re.split(r"[\\/]", m.group(0))[-1], text)
    return re.sub(FILE_EXT, "", t, flags=re.I)


def file_title(path):
    """A file name the way a person says it: no folder, no extension."""
    return os.path.splitext(os.path.basename(path))[0].strip()


def clean_speech(text):
    """What actually gets spoken: no HTML tags (<br>), markdown (** # ` _ >), or bullet marks. Returns ""
    if nothing speakable is left, so a line like "<br>" is never read out."""
    t = re.sub(r"<(laugh|chuckle|sigh|gasp|groan|cough|sniffle|yawn)>", r"[\1]", text or "", flags=re.I)  # her emotions stay
    t = re.sub(r"<[^>]*>", " ", t)
    t = speakable_names(t)
    t = re.sub(r"[*#`_>|]+", " ", t)
    t = re.sub(r"^\s*[-•]\s+", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t if re.search(r"[A-Za-z0-9]{2,}|[\u0A80-\u0AFF\u0600-\u06FF\u0900-\u097F]", t) else ""


SAY_RE = re.compile(r"SAY\s*(?:\(\s*(\w+)\s*\))?\s*:")  # "SAY:" or "SAY(excited):"


class Voice:
    """Kokoro (offline). Speaks one sentence at a time so the first words come out fast and Friday can
    be cut off mid-reply."""
    _model = None

    def __init__(self):
        self.bass = float(CFG.get("voice_bass_db", 7))
        self.loud = float(CFG.get("voice_loudness_db", -13))
        self.lock = threading.Lock()  # agents finishing in the background speak too
        self.stop_flag = threading.Event()
        self.speaking = False
        self.now_playing = None
        self._sapi = None

    @classmethod
    def model(cls):
        if cls._model is None:
            import onnxruntime as ort
            from kokoro_onnx import Kokoro
            # onnxruntime's default (one thread per core) drags in the slow E-cores: 6 threads renders a
            # sentence in ~2.4 s instead of ~3.7 s on his Core Ultra 5
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = int(CFG.get("kokoro_threads", 6))
            sess = ort.InferenceSession(os.path.join(HERE, "models", "kokoro-v1.0.onnx"), opts,
                                        providers=["CPUExecutionProvider"])
            cls._model = Kokoro.from_session(sess, os.path.join(HERE, "models", "voices-v1.0.bin"))
        return cls._model

    # How each mood changes the delivery: pace, pitch (x), a blend of a livelier or deeper voice, loudness
    # (dB) and a breath after the sentence (s). Kokoro can't laugh, but these make lines sound felt.
    MOODS = {
        "excited":   dict(speed=1.12, pitch=1.05, blend=("am_puck", 0.35), gain=1.5, pause=0.0),
        "happy":     dict(speed=1.06, pitch=1.03, blend=("am_puck", 0.25), gain=0.8, pause=0.05),
        "playful":   dict(speed=1.08, pitch=1.04, blend=("am_puck", 0.30), gain=0.5, pause=0.05),
        "surprised": dict(speed=1.10, pitch=1.06, blend=("am_puck", 0.30), gain=1.0, pause=0.1),
        "warm":      dict(speed=1.00, pitch=1.00, blend=("am_michael", 0.30), gain=0.0, pause=0.1),
        "calm":      dict(speed=0.96, pitch=0.99, blend=None, gain=-1.0, pause=0.15),
        "serious":   dict(speed=0.94, pitch=0.97, blend=("am_onyx", 0.35), gain=0.0, pause=0.2),
        "sad":       dict(speed=0.90, pitch=0.96, blend=("am_onyx", 0.30), gain=-2.0, pause=0.25),
    }
    _styles = {}

    # the blend voices above are male; a female base voice gets female partners
    FEMALE_BLEND = {"am_puck": "af_bella", "am_michael": "af_sarah", "am_onyx": "af_nicole"}

    def _style(self, mood):
        base = CFG.get("kokoro_voice", "af_heart")
        m = self.MOODS.get(mood or "")
        if not m or not m["blend"]:
            return base
        key = (base, m["blend"])
        if key not in self._styles:
            other, w = m["blend"]
            if base.startswith("af_"):
                other = self.FEMALE_BLEND.get(other, other)
            k = self.model()
            self._styles[key] = (k.get_voice_style(base) * (1 - w) + k.get_voice_style(other) * w).astype(np.float32)
        return self._styles[key]

    # online voice (Microsoft neural via edge-tts): rate / pitch per mood
    EDGE_MOODS = {"excited": ("+10%", "+8Hz", 1.5), "happy": ("+5%", "+4Hz", 0.8), "playful": ("+6%", "+5Hz", 0.5),
                  "surprised": ("+8%", "+10Hz", 1.0), "warm": ("-2%", "-2Hz", 0.0), "calm": ("-6%", "-3Hz", -1.0),
                  "serious": ("-8%", "-6Hz", 0.0), "sad": ("-12%", "-8Hz", -2.0)}

    def render(self, text, mood=None):
        text = auto_cue(text)  # before the spoken forms below turn "lol" into "L O L"
        # names the voices get wrong, spelled the way they should sound ("Sam" -> "Shabeer")
        for word, sound in CFG.get("pronunciations", {}).items():
            text = re.sub(rf"\b{re.escape(word)}\b", sound, text, flags=re.I)
        if AR_SCRIPT.search(text):  # Arabic script (LSD letters, Arabic): the offline voice spells letter names
            return self._render_arabic(text, mood)
        if GU_SCRIPT.search(text):  # Lisan ud-Dawat (Devanagari or Gujarati script): his chosen LSD voice, online
            line = re.sub(r"\s{2,}", " ", EMOTE.sub(" ", text)).strip()
            for voice in (CFG.get("edge_voice_lsd", "en-US-AvaMultilingualNeural"), CFG.get("edge_voice_gujarati", "gu-IN-DhwaniNeural")):
                try:
                    return self._edge(line, mood, voice=voice)
                except Exception as e:  # offline, or the voice refused the script: the next one
                    log.warning("LSD voice %s failed (%s)", voice, e)
        if EMOTE.search(text):
            if CFG.get("emote_voice", True):
                try:
                    return self._orpheus(text)
                except Exception as e:  # used up for today, offline, or busy: her usual voice, tags dropped
                    log.info("expressive voice unavailable (%s)", str(e)[:120])
            text = re.sub(r"\s{2,}", " ", EMOTE.sub(" ", text)).strip()
        if CFG.get("voice_engine", "edge") == "edge":
            try:
                return self._edge(text, mood)
            except Exception as e:  # offline or the service hiccuped: the local voice takes over
                log.warning("online voice failed (%s), using the offline voice", e)
        return self._kokoro(text, mood)

    _loop = None
    _connector = None

    @classmethod
    def _edge_loop(cls):
        """One event loop for all online-voice requests, with a connector that keeps DNS and TLS state
        between sentences (edge-tts would otherwise close it after every request)."""
        if cls._loop is None:
            import asyncio
            import aiohttp

            class KeepAlive(aiohttp.TCPConnector):
                def close(self, *a, **k):
                    f = asyncio.get_event_loop().create_future()
                    f.set_result(None)
                    return f

            cls._loop = asyncio.new_event_loop()
            threading.Thread(target=cls._loop.run_forever, daemon=True).start()

            async def make():
                return KeepAlive(limit=8, ttl_dns_cache=3600, keepalive_timeout=120)

            cls._connector = asyncio.run_coroutine_threadsafe(make(), cls._loop).result()
        return cls._loop

    def _render_arabic(self, text, mood):
        """A line with Arabic-script runs (a WhatsApp message in LSD or Arabic): the English around them in her
        usual voice, each Arabic-script run read by an online voice that knows the script. LSD's own letters
        (ھ ے چ پ گ ٹ ڈ) = the Urdu voice (Indian accent), plain Arabic = the Arabic voice. Offline: the Arabic
        part is skipped with a short note instead of letter names."""
        parts, rate = [], 24000
        for run in AR_SPLIT.split(text):
            run = run.strip(" ‎‏‪‫‬‭‮")
            if not run or not re.search(r"\w", run):
                continue
            if AR_SCRIPT.search(run):
                urdu = re.search(r"[ٹڈڑںھہےۓچپگژ]", run)
                voices = [CFG.get("edge_voice_urdu", "ur-IN-GulNeural"), CFG.get("edge_voice_arabic", "ar-SA-ZariyahNeural")]
                audio = None
                for voice in (voices if urdu else voices[::-1]):
                    try:
                        audio = self._edge(run, mood, voice=voice)
                        break
                    except Exception as e:
                        log.warning("Arabic-script voice %s failed (%s)", voice, e)
                if audio is None:
                    audio = self.render("(a message in Arabic script)", mood)
            else:
                audio = self.render(run, mood)
            a, r = audio
            if r != rate:  # Orpheus or SAPI at another rate: bring it to 24 kHz before joining
                a = np.interp(np.linspace(0, len(a), int(len(a) * rate / r), endpoint=False),
                              np.arange(len(a)), a.astype(np.float32)).astype(a.dtype)
            parts.append(a)
        if not parts:
            return np.zeros(1, dtype=np.int16), rate
        return np.concatenate(parts), rate

    def _edge(self, text, mood, voice=None):
        import asyncio
        import edge_tts
        import miniaudio
        rate, pitch, gain = self.EDGE_MOODS.get(mood or "", ("+0%", "+0Hz", 0.0))
        # same overall pace as the offline voice (kokoro_speed), with the mood's change on top
        speed = min(float(CFG.get("kokoro_speed", 1.0)) * (1 + int(rate.rstrip("%")) / 100), 1.65)
        rate = f"{round((speed - 1) * 100):+d}%"
        loop = self._edge_loop()

        async def fetch():
            data = bytearray()
            tts = edge_tts.Communicate(text, voice or CFG.get("edge_voice", "en-US-AvaMultilingualNeural"), rate=rate,
                                       pitch=pitch, connector=self._connector)
            async for c in tts.stream():
                if c["type"] == "audio":
                    data.extend(c["data"])
            return bytes(data)

        mp3 = asyncio.run_coroutine_threadsafe(asyncio.wait_for(fetch(), timeout=8), loop).result(timeout=10)
        pcm = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=24000)
        out = master(np.frombuffer(pcm.samples, dtype=np.int16), 24000, self.bass, self.loud + gain)
        return out, 24000

    ORPHEUS_FILE = os.path.join(DATA, "orpheus_quota.json")

    def _orpheus(self, text):
        """Her expressive voice for real emotional moments: Orpheus "hannah" on Groq's free tier, which can laugh,
        gasp, sigh and groan ([laugh] -> <laugh>). 100 lines a day for free, so it's kept for reactions; a local
        count stops at the limit, and a refusal pauses it until the reset."""
        import requests
        day = dt.date.today().isoformat()
        try:
            q = json.load(open(self.ORPHEUS_FILE))
        except (OSError, ValueError):
            q = {}
        if q.get("day") != day:
            q = {"day": day, "used": 0, "until": 0}
        if q["used"] >= int(CFG.get("emote_daily_limit", 95)) or time.time() < q.get("until", 0):
            raise RuntimeError("free allowance used up for now")
        key = backup_brain.load_keys().get("groq")
        if not key:
            raise RuntimeError("no Groq key")
        r = requests.post("https://api.groq.com/openai/v1/audio/speech", headers={"Authorization": f"Bearer {key}"},
                          json={"model": "canopylabs/orpheus-v1-english", "voice": CFG.get("emote_voice_name", "hannah"),
                                "input": EMOTE.sub(lambda m: f"<{m.group(1).lower()}>", text), "response_format": "wav"},
                          timeout=8)
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            q["until"] = time.time() + (float(ra) if ra and ra.replace(".", "").isdigit() else 900)
            json.dump(q, open(self.ORPHEUS_FILE, "w"))
            raise RuntimeError("rate limited")
        r.raise_for_status()
        q["used"] += 1
        json.dump(q, open(self.ORPHEUS_FILE, "w"))
        b = r.content  # its WAV header says "length unknown", so read the samples after the data chunk directly
        rate, bits, i = int.from_bytes(b[24:28], "little"), int.from_bytes(b[34:36], "little"), b.find(b"data")
        pcm = b[i + 8:]
        x = (np.frombuffer(pcm[: len(pcm) // 4 * 4], np.float32) * 32767 if bits == 32
             else np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16).astype(np.float32))
        log.info("expressive voice: %s (%d left today)", text[:60], int(CFG.get("emote_daily_limit", 95)) - q["used"])
        return master(x.astype(np.int16), rate, self.bass, self.loud), rate

    def _kokoro(self, text, mood=None):
        from fractions import Fraction
        m = self.MOODS.get(mood or "", {})
        pitch = m.get("pitch", 1.0)
        base = float(CFG.get("kokoro_speed", 1.0))
        speed = min(base * m.get("speed", 1.0), 1.65)  # excited at 1.4x would otherwise get garbled
        # pitch by resampling: render slower by the pitch factor, then squeeze, so pace stays as asked
        samples, rate = self.model().create(text, voice=self._style(mood), speed=speed / pitch,
                                            lang=CFG.get("kokoro_lang", "en-us"),
                                            sentence_pause=0.35, clause_pause=0.18)
        if abs(pitch - 1.0) > 1e-3:
            from scipy.signal import resample_poly
            f = Fraction(1 / pitch).limit_denominator(60)
            samples = resample_poly(samples, f.numerator, f.denominator)
        out = master((samples * 32767).astype(np.int16), rate, self.bass, self.loud + m.get("gain", 0.0))
        # faster talking, shorter breaths, but not proportionally or it sounds breathless
        gap = (self.breath(text) + m.get("pause", 0.0)) / max(base, 0.5) ** 0.6
        out = np.concatenate([out, np.zeros(int(rate * gap), np.int16)])
        return out, rate

    @staticmethod
    def breath(text):
        """The gap a person leaves after this line: each line is rendered alone and Kokoro trims the
        silence, so without this sentences run into each other like someone reading aloud."""
        import random
        t = text.rstrip()
        base = (0.55 if t.endswith(("…", "...")) else 0.42 if t.endswith("?") else
                0.38 if t.endswith("!") else 0.32 if t.endswith(".") else 0.2)
        return base + random.uniform(-0.05, 0.08)

    def say(self, text, interrupt=None, mood=None, clip=None):
        """Speaks text. `interrupt()` is polled during playback; returns True if he cut in.
        clip = a Future already rendering this text (started as soon as the brain wrote it)."""
        if not text or not re.search(r"\w", text):
            return False
        with self.lock:
            self.speaking = True
            self.stop_flag.clear()
            try:
                if clip is not None:
                    try:
                        samples, rate = clip.result(timeout=20)
                    except Exception:
                        log.exception("prefetched voice failed, rendering again")
                        samples, rate = self.render(text, mood)
                    self.saying = text
                    if self.play(samples, rate, interrupt=interrupt):
                        self.stop_flag.set()
                        return True
                    return False
                parts = [p for p in SENTENCE_END.split(text.strip()) if p.strip()]
                nxt = self.render(parts[0], mood)
                for i in range(len(parts)):
                    cur = nxt
                    # render the next sentence while this one plays
                    box = {}
                    t = None
                    if i + 1 < len(parts):
                        t = threading.Thread(target=lambda s=parts[i + 1]: box.update(r=self.render(s, mood)), daemon=True)
                        t.start()
                    # what she's saying right now (and just said): echo of it in the mic isn't him talking
                    self.saying = " ".join(parts[max(0, i - 1):i + 1])
                    if self.play(*cur, interrupt=interrupt):
                        self.stop_flag.set()
                        return True
                    if t:
                        t.join()
                        nxt = box.get("r")
                return False
            except Exception:
                log.exception("Kokoro failed, using SAPI")
                self._sapi_say(text)
                return False
            finally:
                self.speaking = False

    engine = None  # audio.AudioEngine when echo cancellation is on (plays through the same stream as the mic)

    def play(self, samples, rate, interrupt=None, blocking=True):
        if self.engine:
            self.engine.play(samples, rate)
            if not blocking:
                return False
            while self.engine.busy():
                if self.stop_flag.is_set() or (interrupt and interrupt()):
                    self.engine.stop()
                    return True
                time.sleep(0.03)
            return False
        import sounddevice as sd
        sd.play(samples, rate)
        self.now_playing = (samples, rate, time.time())  # reference for telling his voice from our echo
        if not blocking:
            return False
        end = time.time() + len(samples) / rate + 0.03
        while time.time() < end:
            if self.stop_flag.is_set() or (interrupt and interrupt()):
                sd.stop()
                return True
            time.sleep(0.03)
        return False

    def stop(self):
        self.stop_flag.set()

    def _sapi_say(self, text):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        if self._sapi is None:
            self._sapi = win32com.client.Dispatch("SAPI.SpVoice")
        self._sapi.Speak(text)


VOICE_POOL = None
FIRST_CLAUSE = re.compile(r"^(\S+(?:\s+\S+){1,6}?[,;:—–])\s+(.+)$", re.S)  # 2-7 words up to a comma or dash


def prefetch_voice(replies, voice):
    """Wraps the brain's reply queue: each spoken line starts rendering the moment the brain writes it,
    in parallel, instead of after the previous line has finished playing. The online voice needs ~1 s per
    request, so this removes the pause between sentences."""
    global VOICE_POOL
    if VOICE_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        # the offline voice uses every core it's given: rendering lines side by side slowed the FIRST one
        # (the one he's waiting for), so it renders them one after another, in order
        VOICE_POOL = ThreadPoolExecutor(max_workers=4 if CFG.get("voice_engine", "edge") == "edge" else 1)
    out = queue.Queue()

    def pump():
        first = True
        while True:
            kind, item = replies.get()
            if kind == "say":
                text, mood = item if isinstance(item, tuple) else (item, None)
                pieces = [text]  # whole sentences: splitting them early made the delivery choppy
                # ...except a long FIRST sentence: rendering it whole is what he waits for, so its opening
                # clause goes first ("Oh no," / "not the login bug again, it expired overnight.")
                m = FIRST_CLAUSE.match(text) if first and len(text.split()) > 9 else None
                if m and len(m.group(2).split()) >= 3:
                    pieces = [m.group(1), m.group(2)]
                first = False
                for piece in pieces:
                    fut = VOICE_POOL.submit(voice.render, piece, mood) if re.search(r"\w", piece or "") else None
                    out.put((kind, (piece, mood, fut)))
                continue
            out.put((kind, item))
            if kind == "end":
                return

    threading.Thread(target=pump, daemon=True).start()
    return out


TICK_PATH = os.path.join(DATA, "tick.wav")


_REPEAT = re.compile(r"(?<![^\s,.!?])([^\s,.!?]+(?:[\s,.!?]+[^\s,.!?]+){0,5}?)(?:[\s,.!?]+\1(?=[\s,.!?]|$)){2,}", re.I)
_CHAR_RUN = re.compile(r"(\S{1,3})\1{4,}")


def collapse_repeats(text):
    """Whisper's loops: a word or phrase (up to 6 words) said 3+ times in a row becomes one, and a run of the same
    1-3 characters ("લ્લ્લ્લ્") is cut. Returns (text, looped): looped = the repeats were most of what it wrote."""
    if not text:
        return text, False
    before = len(text)
    out = _CHAR_RUN.sub(r"\1", text)
    for _ in range(3):
        new = _REPEAT.sub(r"\1", out)
        if new == out:
            break
        out = new
    removed = before - len(out)
    if removed:
        log.info("collapsed repeats: %r -> %r", text[:120], out[:120])
    return out, removed > max(30, before * 0.5)


def tick():

    """A soft click the moment she knows he's talking to her (the answer itself takes a second or two)."""
    import winsound
    if quiet_out():
        return
    try:
        if not os.path.exists(TICK_PATH):
            import wave
            rate, n = 44100, int(44100 * 0.028)
            t = np.arange(n) / rate
            s = (np.sin(2 * np.pi * 1850 * t) * np.exp(-t * 170) * 0.22 * 32767).astype(np.int16)
            with wave.open(TICK_PATH, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(s.tobytes())
        winsound.PlaySound(TICK_PATH, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


def _words(t):
    return re.findall(r"[a-z0-9']+", (t or "").lower())


def same_person(a, b):
    """'Ali bh Patel' and 'Ali Patel' are the same chat: the longer name words all match."""
    wa_, wb = {w for w in _words(a) if len(w) > 2}, {w for w in _words(b) if len(w) > 2}
    return bool(wa_ and wb) and (wa_ <= wb or wb <= wa_ or len(wa_ & wb) >= 2)


def said_back(message, spoken):
    """She read this message back (most of its words are in what she said)."""
    m = _words(message)
    return bool(m) and sum(w in set(_words(spoken)) for w in m) >= max(1, round(len(m) * 0.6))


def media_playing():
    """True if another app is making sound right now (a video, music, a call): sampled a few times, since speech
    and music have gaps. Friday's own voice doesn't count."""
    try:
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        me = os.getpid()
        meters = []
        for s in AudioUtilities.GetAllSessions():
            if s.Process is None or s.Process.pid == me:
                continue
            meters.append(s._ctl.QueryInterface(IAudioMeterInformation))
        for _ in range(4):
            if any(m.GetPeakValue() > 0.02 for m in meters):
                return True
            time.sleep(0.08)
    except Exception:
        log.exception("media check failed")
    return False


TECHNICAL = re.compile(r"\b(scroll|scrolling|tap|tapping|click|clicking|screen|page|button|swipe|can'?t see|cannot see|"
                       r"don'?t see|not visible|instead|try again|trying|retry|loading|let me|element|window|tab)\b", re.I)


UNDO = re.compile(r"\b(disconnect|turn off|switch off|stop|cancel|end call|hang up|log ?out|sign ?out|"
                  r"unsubscribe|uninstall|disable|pause|mute|exit|close)\b", re.I)  # buttons that reverse things


def step_words(step):
    """A task step in plain words for the pill ("Opening WhatsApp", "Typing"), or "" for waits and reads."""
    d = str(step.get("do") or step.get("a") or "")
    what = str(step.get("app") or step.get("name") or step.get("url") or step.get("window") or step.get("text") or "")[:40]
    return {"open": f"Opening {what}".strip(), "tab": "Switching tab", "focus": f"Going to {what}".strip(),
            "click": f"Clicking {what}".strip(), "type": "Typing", "paste": "Pasting", "key": "Pressing keys",
            "scroll": "Scrolling", "scroll_to": f"Finding {what}".strip(), "copy": "Copying"}.get(d, "")


# Tasks stay honest (his feedback, 13 Oct): stuck = stop and ask him; done = one look at the screen, then stop.
TASK_STALE_TURNS = 3  # turns in a row where her steps changed nothing and taught her nothing
_TASK_NOTHING = re.compile(r"nothing changed|timed out|still changing|didn't find|reached the end|you sent no steps", re.I)
_TICKING = re.compile(r"\d+:\d{2}|\d+\s?%")  # clocks, video times, percentages: they move on their own


def task_informative(notes):
    """Did a turn's notes tell her something (a read, a copy, a find, his answer)?"""
    return any(n and not n.startswith("FAILED") and not _TASK_NOTHING.search(n) for n in notes)


def task_stale(before, after, notes, sofar):
    """One more stale turn, or back to 0 when the screen moved or she learnt something. after=None: not read."""
    if after is None or task_informative(notes) or _TICKING.sub("", before) != _TICKING.sub("", after):
        return 0
    return sofar + 1


def task_check_note(said):
    return (f'CHECK ONCE before you finish: you said "{said}". Look at THIS screen. If it shows the goal really done, '
            'reply {"do":"done","say":"..."} saying only what this screen shows (no names, times, numbers or details '
            "that aren't on it). If it isn't done, reply fail with what's actually there, or send the one step that "
            "finishes it.")


def task_stuck_note(why):
    return (f"STUCK: {why}. Send NO other steps. Reply only with an ask: ONE short spoken question to him saying "
            "what's in the way and what he should decide.")


def quiet_progress(text, state, gap=20):
    """Progress lines during a task: the first one ("On it"), then at most one short milestone every `gap` seconds,
    and never anything about scrolling, clicking or what's on screen. True = say it."""
    now = time.time()
    if TECHNICAL.search(text) or now - state.get("last", 0) < gap:
        log.info("progress kept quiet: %s", text)
        return False
    state["last"] = now
    return True


def _keys(vk, times=1):
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)


def quick_command(text):
    """Simple requests answered on the laptop itself, no AI call: instant and free. Returns (what she says,
    what to do) or None. Deliberately strict: anything with more to it goes to the brain."""
    t = re.sub(r"[^\w\s:']", " ", (text or "").lower()).strip()
    t = re.sub(r"^(please |can you |could you |hey |ok |okay )+", "", re.sub(r"\s+", " ", t)).strip()
    t = re.sub(r"( please| for me| now)+$", "", t).strip()
    # talking in Lisan ud-Dawat: on by name, off by "English" + talk/speak (in either language)
    if re.fullmatch(r"(let'?s )?(talk|speak|chat)( to me| with me)? in (lsd|l s d|lisan ud ?dawat|lisan al ?dawat|lisan|dawat( language)?|"
                    r"gujarati|bohra)( language| mode)?|(lsd|lisan ud ?dawat) (mode )?on", t):
        return "ठीक छे सर, हवे लिसान उद-दावत मा वात करिये.", lambda: set_language("lsd")
    if re.fullmatch(r"(let'?s )?(talk|speak|chat)( to me| with me)? in english( again)?|back to english|switch to english|english (mode )?on|"
                    r"(lsd|lisan ud ?dawat) (mode )?off", t) or \
            (lsd_on() and re.search(r"english|અંગ્રેજી|ઇંગ્લિશ", text or "", re.I) and re.search(r"વાત|બોલ|talk|speak", text or "", re.I)):
        return "Back to English, sir.", lambda: set_language("en")
    if re.fullmatch(r"(turn |switch |put )?(on )?office mode( on)?|(turn|switch) on office mode|(start|enable) office mode|"
                    r"i'?m (at|in) (the )?office( now)?", t):
        return "Office mode on. I'll keep quiet: words on screen, reminders on your phone.", lambda: office_switch(True)
    if re.fullmatch(r"(turn |switch )?off office mode|office mode off|(stop|end|disable) office mode|"
                    r"i'?m (back )?home( now)?|i'?m out of (the )?office", t):
        return "Office mode off. I can talk again.", lambda: office_switch(False)
    if re.fullmatch(r"(what'?s the time|what time is it|time|tell me the time)", t):

        return f"It's {dt.datetime.now():%I:%M}".replace(" 0", " ") + ", sir.", lambda: None
    if re.fullmatch(r"(what'?s the date|what'?s today'?s date|what day is it|which day is it)( today)?", t):
        return f"It's {dt.datetime.now():%A, %d %B}.", lambda: None
    if re.fullmatch(r"(volume up|turn (it|the volume) up|louder|increase (the )?volume)", t):
        return "Louder, bet.", lambda: _keys(0xAF, 5)
    if re.fullmatch(r"(volume down|turn (it|the volume) down|quieter|lower (the )?volume|decrease (the )?volume)", t):
        return "Turned it down.", lambda: _keys(0xAE, 5)
    if re.fullmatch(r"(mute|unmute|mute (it|the laptop|sound))", t):
        return "Done.", lambda: _keys(0xAD)
    if re.fullmatch(r"(pause|play|resume|pause (it|the video|the music|the song)|play (it|again)|resume (it|playing))", t):
        return "Done.", lambda: _keys(0xB3)
    if re.fullmatch(r"(next|skip|next (song|track|video|episode)|skip (it|this|this song))", t):
        return "Skipped.", lambda: _keys(0xB0)
    if re.fullmatch(r"(previous|go back|previous (song|track)|last song)", t):
        return "Going back.", lambda: _keys(0xB1, 2)
    if re.fullmatch(r"lock (my |the )?(laptop|screen|pc|computer)", t):
        return "Locking it. Don't miss me too much.", lambda: user32.LockWorkStation()
    m = re.fullmatch(r"(open|launch|start) ([\w' ]{2,30})", t)
    if m and " and " not in m.group(2) and m.group(2).split()[0] not in ("the", "a", "my", "chat", "whatsapp", "youtube"):
        name = m.group(2)
        hit = APPS.find(name) if hasattr(APPS, "find") else None
        if hit:
            return f"Opening {name}.", lambda: APPS.launch(name)
    return None


def chime(kind="start"):
    import winsound
    if quiet_out():
        return
    try:
        if kind == "start":
            winsound.Beep(880, 50)
            winsound.Beep(1320, 70)
        elif kind == "end":
            winsound.Beep(1100, 45)
        else:
            winsound.Beep(400, 150)
    except RuntimeError:
        pass


# ---------------------------------------------------------------- memory

class Memory:
    """data/history.jsonl  every line said, by both sides, forever
       data/memory.md      facts he asked to remember (until he says forget)
       data/digests.json   one short summary per past day, so a week back stays in context
       data/feedback.md    his feedback on Friday itself, a to-do list for improving Friday
       data/notes/         long dictation he asked to keep"""

    def __init__(self):
        os.makedirs(os.path.join(DATA, "notes"), exist_ok=True)
        self.history_path = os.path.join(DATA, "history.jsonl")
        self.memory_path = os.path.join(DATA, "memory.md")
        self.digest_path = os.path.join(DATA, "digests.json")
        self.feedback_path = os.path.join(DATA, "feedback.md")
        self.lock = threading.Lock()

    def log(self, who, text):
        if not text or os.environ.get("FRIDAY_TEST"):  # test runs mustn't end up in her memory
            return
        rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "who": who, "text": text}
        with self.lock, open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def drop(self, texts):
        """Removes lines with these exact words (he deleted them in the phone app), with or without the [phone] tag."""
        texts = {t for t in texts if t}
        if not texts or not os.path.exists(self.history_path):
            return
        gone = texts | {f"[phone] {t}" for t in texts}
        with self.lock:
            with open(self.history_path, encoding="utf-8") as f:
                keep = [line for line in f if not self._has(line, gone)]
            with open(self.history_path, "w", encoding="utf-8") as f:
                f.writelines(keep)

    @staticmethod
    def _has(line, texts):
        try:
            return json.loads(line).get("text") in texts
        except ValueError:
            return False

    def entries(self, since=None):
        if not os.path.exists(self.history_path):
            return []
        out = []
        with open(self.history_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if since is None or rec["t"] >= since:
                    out.append(rec)
        return out

    def recent(self, n=30):
        lines = []
        for rec in self.entries(since=(dt.datetime.now() - dt.timedelta(days=2)).isoformat())[-n:]:
            t = dt.datetime.fromisoformat(rec["t"])
            stamp = t.strftime("%H:%M") if t.date() == dt.date.today() else t.strftime("%a %H:%M")
            lines.append(f"[{stamp}] {rec['who']}: {rec['text'][:600]}")
        return lines

    def _bullets(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f if l.startswith("- ")]

    def facts(self):
        return [l[2:] for l in self._bullets(self.memory_path)]

    def remember(self, fact):
        with self.lock, open(self.memory_path, "a", encoding="utf-8") as f:
            f.write(f"- {fact.strip()} (saved {dt.date.today():%d %b %Y})\n")

    def forget(self, query):
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        keep, dropped = [], []
        for fact in self.facts():
            (dropped if words and all(w in fact.lower() for w in words) else keep).append(fact)
        with self.lock, open(self.memory_path, "w", encoding="utf-8") as f:
            f.writelines(f"- {k}\n" for k in keep)
        return dropped

    def add_feedback(self, text):
        with self.lock, open(self.feedback_path, "a", encoding="utf-8") as f:
            f.write(f"- [ ] {dt.datetime.now():%d %b %Y %H:%M}: {text.strip()}\n")

    def open_feedback(self):
        return [l[6:] for l in self._bullets(self.feedback_path) if l.startswith("- [ ] ")]

    def archive(self, since=None, days=90):
        """Her memory for the phone to keep (GET /archive), so it knows everything when the laptop can't be reached:
        the log since `since` (else the last `days` days, minus restarts and Sara's messages), the facts, every
        feedback item with its tick, and the day digests."""
        start = since or (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
        lines = [{"t": r["t"], "who": r["who"], "text": r["text"][:1500]} for r in self.entries(since=start)
                 if r["t"] > start and not self._private(r)
                 and not (r["who"] == "Action" and self._NOISE.search(r["text"]))][-20000:]
        fb = [l for l in self._bullets(self.feedback_path) if l.startswith(("- [ ] ", "- [x] "))]
        return {"lines": lines, "facts": self.facts(), "feedback": fb, "digests": self.digests(),
                "now": dt.datetime.now().isoformat(timespec="seconds")}

    @staticmethod
    def _private(rec):
        """Sara's messages: never quoted, never copied anywhere."""
        return bool(re.search(r"\b(message arrived from|sent WhatsApp to|on WhatsApp:)\s*Sara", rec["text"], re.I)
                    or re.match(r"Sara\b.*on WhatsApp:", rec["text"], re.I))

    def save_note(self, title, text):
        safe = re.sub(r"[^\w\- ]", "", title or "note").strip()[:60] or "note"
        path = os.path.join(DATA, "notes", f"{dt.datetime.now():%Y-%m-%d %H%M} {safe}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {title or 'Note'}\n\n{dt.datetime.now():%A %d %B %Y, %H:%M}\n\n{text.strip()}\n")
        return path

    def digests(self):
        try:
            with open(self.digest_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def build_digests(self, days=14):
        """Summarises each finished day that has no digest yet (runs in the background at start-up)."""
        have = self.digests()
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        by_day = {}
        for rec in self.entries(since=start):
            day = rec["t"][:10]
            if day < dt.date.today().isoformat() and day not in have:
                by_day.setdefault(day, []).append(f"{rec['t'][11:16]} {rec['who']}: {rec['text']}")
        for day, lines in sorted(by_day.items()):
            try:
                summary = ask_claude(
                    "Summarise this day of conversation between him and his assistant FRIDAY in 2-4 plain "
                    "sentences: what he asked for, what got done, anything left unfinished, names, numbers and "
                    "decisions worth remembering. No preamble.", "\n".join(lines)[-12000:], timeout=90)
            except Exception:
                log.exception("digest failed for %s", day)
                continue
            have[day] = summary
            with self.lock, open(self.digest_path, "w", encoding="utf-8") as f:
                json.dump(have, f, ensure_ascii=False, indent=1)
            log.info("DIGEST %s: %s", day, summary)

    def search(self, query, days=30):
        since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        recs = self.entries(since=since)
        hits = [r for r in recs if any(w in r["text"].lower() for w in words)] if words else recs
        return [f"{r['t'][:16].replace('T', ' ')} {r['who']}: {r['text']}" for r in (hits or recs)[-120:]]

    # words in a question about the past that say nothing about what to look for
    _ASK = set("what when where who whom which why how did does do was were is are has have had the and for you your "
               "with that this than then from about tell told say said says ask asked yesterday today tonight last "
               "week weeks month day days ago earlier before morning evening afternoon night remember recall talk "
               "talked discuss discussed mention mentioned message messages messaged text texted chat chats our "
               "between there any anything something all can could would should me him her his them they she friday "
               "kya bola tha kab kal parso happened happen doing done going thing things bhai bh bhaisaheb saheb sahab "
               "bhaiya ben bs".split())
    _NOISE = re.compile(r"^(restarted|paused|resumed)\b")

    def look_back(self, question, keywords="", days=30):
        """The lines that answer a question about the past: the time it names (yesterday, last week, Monday,
        12 Oct, 3 days ago...), lines matching its names and words (fuzzy, so "Ali bhai" finds "Ali bh"),
        each with the lines around it, plus the digests of those days. Sara's messages are never quoted."""
        from rapidfuzz import fuzz
        now = dt.datetime.now()
        start, end = when_range(question, now)
        if start is None:
            start, end = now - dt.timedelta(days=int(days or 30)), now
        recs = [r for r in self.entries(since=start.isoformat(timespec="seconds"))
                if r["t"] <= end.isoformat(timespec="seconds")
                and not (r["who"] == "Action" and self._NOISE.search(r["text"]))
                and not re.search(r"\b(message arrived from|sent WhatsApp to|on WhatsApp:)\s*Sara", r["text"], re.I)
                and not re.match(r"Sara\b.*on WhatsApp:", r["text"], re.I)]
        terms = [w for w in re.findall(r"\w+", f"{keywords} {question}".lower()) if len(w) > 2 and w not in self._ASK
                 and not w.isdigit() and w[:3] not in _MONTHS and w not in _WEEKDAYS]
        terms = list(dict.fromkeys(terms))
        picked = set()
        if terms:
            scores = []
            for i, r in enumerate(recs):
                words = re.findall(r"\w+", r["text"].lower())
                s = sum(1 for t in terms if t in r["text"].lower() or any(fuzz.ratio(t, w) >= 85 for w in words if len(w) > 2))
                if s:
                    scores.append((s, i))
            best = max((s for s, _ in scores), default=0)
            for s, i in scores:
                if s >= max(1, best - (1 if best >= 3 else 0)):  # the closest matches, not every line sharing one word
                    picked.update(range(max(0, i - 2), min(len(recs), i + 3)))
        elif when_range(question, now)[0] is not None:  # "what did we talk about yesterday": that whole stretch
            picked = {i for i, r in enumerate(recs) if r["who"] != "Action" or "message arrived" in r["text"]}
        lines = [f"{recs[i]['t'][:16].replace('T', ' ')} {recs[i]['who']}: {recs[i]['text'][:500]}" for i in sorted(picked)]
        if len(lines) > 160:
            lines = lines[:40] + ["..."] + lines[-120:]
        dig = self.digests()
        days_in = sorted({start.date() + dt.timedelta(days=k) for k in range((end.date() - start.date()).days + 1)})
        notes = [f"Digest {d}: {dig[d.isoformat()]}" for d in days_in if d.isoformat() in dig][-10:]
        return notes + lines

    def context(self):
        parts = []
        facts = self.facts()
        if facts:
            parts.append("Things he asked you to remember (permanent):\n" + "\n".join(f"- {f}" for f in facts))
        dig = self.digests()
        if dig:
            parts.append("Earlier days:\n" + "\n".join(
                f"- {dt.date.fromisoformat(d):%a %d %b}: {dig[d]}" for d in sorted(dig)[-7:]))
        fb = self.open_feedback()
        if fb:
            parts.append(f"Open feedback he gave about you ({len(fb)}):\n" + "\n".join(f"- {f}" for f in fb[-15:]))
        recent = self.recent()
        if recent:
            parts.append("Recent conversation (oldest first):\n" + "\n".join(recent))
        return "\n\n".join(parts)


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def when_range(text, now=None):
    """The stretch of time a question names, as (start, end) datetimes, or (None, None). Knows today, yesterday,
    day before yesterday / parso, this/last week, this/last month, N days/weeks ago, last N days, weekday names
    (the most recent one; 'Friday' only with on/last/this, since it's her name) and dates like 12 Oct / Oct 12."""
    now = now or dt.datetime.now()
    t = (text or "").lower()
    today = dt.datetime.combine(now.date(), dt.time())
    day = lambda d: (d, d + dt.timedelta(days=1) - dt.timedelta(seconds=1))
    if re.search(r"\b(day before yesterday|parso)\b", t):
        return day(today - dt.timedelta(days=2))
    if re.search(r"\b(yesterday|kal)\b", t):
        return day(today - dt.timedelta(days=1))
    if re.search(r"\b(today|this morning|this afternoon|this evening|tonight|aaj)\b", t):
        return today, now
    m = re.search(r"\b(?:last|past) (\d+|two|three|four|five|six|seven|ten) days\b", t)
    if m:
        n = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10}.get(m.group(1)) or int(m.group(1))
        return today - dt.timedelta(days=n), now
    m = re.search(r"\b(\d+|a|one|two|three) (day|week)s? ago\b", t)
    if m:
        n = {"a": 1, "one": 1, "two": 2, "three": 3}.get(m.group(1)) or int(m.group(1))
        if m.group(2) == "day":
            return day(today - dt.timedelta(days=n))
        s = today - dt.timedelta(days=today.weekday() + 7 * n)
        return s, s + dt.timedelta(days=7) - dt.timedelta(seconds=1)
    if re.search(r"\blast week\b", t):
        s = today - dt.timedelta(days=today.weekday() + 7)
        return s, s + dt.timedelta(days=7) - dt.timedelta(seconds=1)
    if re.search(r"\bthis week\b", t):
        return today - dt.timedelta(days=today.weekday()), now
    if re.search(r"\blast month\b", t):
        first = today.replace(day=1)
        s = (first - dt.timedelta(days=1)).replace(day=1)
        return s, first - dt.timedelta(seconds=1)
    if re.search(r"\bthis month\b", t):
        return today.replace(day=1), now
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)? (" + "|".join(_MONTHS) + r")[a-z]*\b", t) or \
        re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]* (\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        a, b = m.groups()
        d, mon = (int(a), b) if a.isdigit() else (int(b), a)
        try:
            when = dt.datetime(now.year, _MONTHS.index(mon[:3]) + 1, d)
            if when > now:
                when = when.replace(year=now.year - 1)
            return day(when)
        except ValueError:
            pass
    for i, name in enumerate(_WEEKDAYS):
        pat = rf"\b(?:on|last|this|past) {name}\b" if name == "friday" else rf"\b{name}\b"
        if re.search(pat, t):
            back = (today.weekday() - i) % 7 or (7 if "last" in t else 0)
            return day(today - dt.timedelta(days=back))
    return None, None


MEMORY = Memory()


# ---------------------------------------------------------------- contacts

TITLES = re.compile(r"\b(bhai|bhaiya|bh|bs|ben|behen|bhen|sir|ji|uncle|aunty|aunti|mama|mami|kaka|kaki|bhabhi|"
                    r"saheb|sahab|mr|mrs|ms|dr|its|new|old|office|work|home|mobile|jio|airtel)\b|\d+", re.I)


def sounds_like(name):
    """A rough phonetic key for Indian names, so "Sahra" ~ "Sara", "Hussain" ~ "Husain", "Saam" ~ "Sam"."""
    s = TITLES.sub(" ", name.lower())
    s = re.sub(r"[^a-z ]", " ", s)
    for a, b in (("ph", "f"), ("bh", "b"), ("kh", "k"), ("gh", "g"), ("th", "t"), ("dh", "d"), ("sh", "s"),
                 ("ch", "c"), ("ck", "k"), ("q", "k"), ("z", "j"), ("v", "w"), ("x", "ks"), ("ee", "i"),
                 ("oo", "u"), ("aa", "a"), ("ou", "u"), ("y", "i")):
        s = s.replace(a, b)
    s = re.sub(r"(.)\1+", r"\1", s).replace("h", "")
    return " ".join(s.split())


class Contacts:
    """His phone contacts (data/contacts.json, built from Phone Link by contacts_import.py), matched by
    spelling and by sound, with the people he calls most ranked first."""

    def __init__(self):
        self.path = os.path.join(DATA, "contacts.json")
        self.items, self.mtime = [], 0

    def load(self):
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return self.items
        if m != self.mtime:
            with open(self.path, encoding="utf-8") as f:
                self.items = json.load(f)
            self.mtime = m
            self.names = [c["name"] for c in self.items]
            self.lower = [TITLES.sub(" ", n.lower()).strip() for n in self.names]
            self.keys = [sounds_like(n) for n in self.names]
        return self.items

    @staticmethod
    def _score(query, choice, **_):
        """Whole-name similarity (ignoring spaces: "Ali Asghar" ~ "Aliasgar"); a one-word query may also
        match any single word of the name ("Mustafa" ~ "Mustafa Merchant"), slightly discounted."""
        from rapidfuzz import fuzz
        if not query or not choice:
            return 0.0
        s = max(fuzz.token_sort_ratio(query, choice), fuzz.ratio(query.replace(" ", ""), choice.replace(" ", "")))
        if " " not in query:
            s = max(s, 0.93 * max(fuzz.ratio(query, w) for w in choice.split()))
        return s

    def match(self, spoken, limit=5, cutoff=72):
        """[(contact, score)] best first."""
        from rapidfuzz import process
        if not self.load() or not spoken.strip():
            return []
        q, qk = TITLES.sub(" ", spoken.lower()).strip(), sounds_like(spoken)
        q = " ".join(q.split())
        scores = {}
        for choices, query in ((self.lower, q), (self.keys, qk)):
            if not query:
                continue
            for _, score, i in process.extract(query, choices, scorer=self._score, limit=limit * 3):
                scores[i] = max(scores.get(i, 0), score)
        ranked = sorted(((self.items[i], s + min(self.items[i]["calls"], 20) * 0.3) for i, s in scores.items()),
                        key=lambda x: -x[1])
        return [(c, s) for c, s in ranked if s >= cutoff][:limit]

    def best(self, spoken):
        """The contact he clearly means, or None if it's ambiguous (then Friday asks)."""
        m = self.match(spoken, limit=3)
        if not m:
            return None
        q = " ".join(TITLES.sub(" ", spoken.lower()).split())
        exact = [c for c, _ in m if " ".join(TITLES.sub(" ", c["name"].lower()).split()) == q]
        if len(exact) == 1:
            return exact[0]
        if len(m) == 1 or m[0][1] - m[1][1] >= 4:
            return m[0][0]
        return None

    def in_words(self, text):
        """Contacts whose names appear (spelt any way) in what he said, for the brain's context."""
        if not re.search(r"\b(call|ring|phone|dial|message|text|send|tell|whatsapp|ping|remind|email|mail|chat|"
                         r"msg|reply|contact|number)\b", text, re.I):
            return []
        # Whisper capitalises names, so only look at runs of capitalised words (plus titles like "bhai"),
        # skipping the sentence's first word and app names
        skip = {"friday", "whatsapp", "google", "chat", "claude", "code", "i", "hey", "okay", "ok", "please", "can",
                "send", "call", "message", "text", "tell", "the", "and", "uat", "rms", "vms"}
        tokens = re.findall(r"[A-Za-z']+", text)
        runs, cur = [], []
        for k, w in enumerate(tokens):
            name_like = (w[0].isupper() and k > 0 and w.lower() not in skip) or (cur and w.lower() in ("bhai", "bh", "bs", "ben"))
            if name_like:
                cur.append(w)
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        found = {}
        for words in runs:
            for n in range(1, min(3, len(words)) + 1):
                for i in range(len(words) - n + 1):
                    for c, s in self.match(" ".join(words[i:i + n]), limit=3, cutoff=85):
                        s += 8 * (n - 1)  # a full-name match beats a first-name-only one (even a frequent one)
                        if s > found.get(c["name"], (0, None))[0]:
                            found[c["name"]] = (s, c)
        return [c for s, c in sorted(found.values(), key=lambda x: -x[0])][:6]

    def frequent(self, n=30):
        return [c["name"] for c in self.load()[:n] if c["calls"]]


CONTACTS = Contacts()


# ---------------------------------------------------------------- reminders and alarms

class Reminders:
    """data/reminders.json: [{id, text, kind: reminder|alarm, when: ISO local time or null,
    trigger: null|"laptop_open", repeat: null|"daily"|"weekdays"|"weekly"|"every N hours/minutes", done,
    enabled (false = switched off in the dashboard: kept but never fires), name (routines: water, sara, mum...),
    section, description, runs_on "laptop"|"phone" (the phone's own automations: listed here, fired there),
    prayer (namaz: its time is refreshed daily), stale (minutes after which a missed one is skipped),
    remark (his own note on it, which she reads: "only if I'm home", "say it gently"), start ("HH:MM": the time he set,
    where an "every N hours" one starts each day), window ("08:00-23:00": the hours an "every N" one may ring),
    offset_min (namaz: minutes after the timetable time), paused_until (ISO: skipped until then)}]
    Every item is editable from her window, the phone and by voice; nothing is fixed."""

    def __init__(self):
        self.path = os.path.join(DATA, "reminders.json")
        self.lock = threading.Lock()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def save(self, items):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)

    @staticmethod
    def _new_id(items):
        """Millisecond ids, kept unique even when several are made in the same instant (toggles go by id)."""
        taken = {r.get("id") for r in items}
        i = int(time.time() * 1000)
        while i in taken:
            i += 1
        return i

    def add(self, text, when=None, kind="reminder", trigger=None, repeat=None, name=None, remark=None):
        with self.lock:
            items = self.load()
            item = {"id": self._new_id(items), "text": text, "kind": kind, "when": when, "trigger": trigger,
                    "repeat": repeat, "done": False, "enabled": True}
            if name:
                item["name"] = name
            if remark:
                item["remark"] = remark.strip()
            if when and repeat and re.search(r"every|hourly", repeat):
                item["start"] = when[11:16]
            items.append(item)
            self.save(items)
        return item

    def pending(self):
        """Every live reminder, switched on or off (off ones carry "enabled": false); done/cancelled ones are left out."""
        return [dict(r, enabled=r.get("enabled", True)) for r in self.load() if not r["done"]]

    # his routines: seeded once (by name), so cancelling or switching one off never brings it back by itself.
    # runs_on "phone" = the phone app's own automation (from his knowme page): shown here, fired there.
    SECTIONS = ["Health", "Family", "Work", "Prayer", "Entertainment", "Shopping"]
    ROUTINES = [
        {"name": "water", "text": "Drink some water", "repeat": "every 2 hours", "hour": 10, "minute": 0,
         "section": "Health", "description": "A sip reminder every two hours, 8 am to 11 pm."},
        {"name": "wake", "text": "Wake up", "repeat": "daily", "hour": 9, "minute": 45, "section": "Health",
         "runs_on": "phone", "description": "His 9:45 wake-up on the phone, with a nudge if he oversleeps."},
        {"name": "bedtime", "text": "Bedtime", "repeat": "daily", "hour": 2, "minute": 0, "section": "Health",
         "runs_on": "phone", "description": "Wind-down nudge at his usual 2 am bedtime."},
        {"name": "sara", "text": "Text Sara", "repeat": "every 2 hours 30 minutes", "hour": 10, "minute": 30,
         "section": "Family", "description": "A reminder to text Sara every two to three hours through the day."},
        {"name": "mum", "text": "Video call Mum", "repeat": "daily", "hour": 21, "minute": 0, "section": "Family",
         "stale": 120, "description": "Every night around 9 pm: video call Mum."},
        {"name": "birthdays", "text": "Birthdays: Maryam 2 Jan, Sara 9 Jun", "repeat": "yearly",
         "hour": 9, "minute": 0, "month": 10, "day": 17, "section": "Family", "runs_on": "phone",
         "description": "Maryam's on 2 Jan, Sara's on 9 Jun: the phone gives a heads-up the week before."},
        {"name": "rapido", "text": "Book the Rapido now for the 10:15 pickup, if you're up and getting ready",
         "repeat": "office days", "hour": 10, "minute": 6, "section": "Work", "stale": 10,
         "description": "Start the Rapido booking at 10:06 for the 10:15 pickup, Monday to Saturday."},
        {"name": "leave", "text": "Time to leave for the office", "repeat": "office days", "hour": 10, "minute": 10,
         "section": "Work", "runs_on": "phone", "description": "Out the door by 10:10, 25 minutes after waking."},
        {"name": "brief", "text": "Morning brief", "repeat": "daily", "hour": None, "section": "Work",
         "description": "Meetings, urgent mail, weather and today's reminders, the first time he's at the laptop "
                        "after 5 am."},
        {"name": "namaz_fajr", "text": "Fajr namaz", "repeat": "daily", "prayer": "Fajr", "hour": 5, "minute": 15,
         "section": "Prayer", "stale": 20, "description": "At the exact Fajr time for Mumbai, refreshed daily."},
        {"name": "namaz_zohr", "text": "Zohr namaz", "repeat": "daily", "prayer": "Dhuhr", "hour": 12, "minute": 30,
         "section": "Prayer", "stale": 20, "description": "At the exact Zohr time for Mumbai (Asr with it)."},
        {"name": "namaz_maghrib", "text": "Maghrib namaz", "repeat": "daily", "prayer": "Maghrib", "hour": 18,
         "minute": 20, "section": "Prayer", "stale": 20,
         "description": "At the exact Maghrib time for Mumbai (Isha with it)."},
        {"name": "shows", "text": "New episodes tonight?", "repeat": "daily", "hour": 20, "minute": 0,
         "section": "Entertainment", "runs_on": "phone",
         "description": "New episodes of From, F1, Friends, House and Game of Thrones."},
        {"name": "deals", "text": "Deals check", "repeat": "daily", "hour": 12, "minute": 0, "section": "Shopping",
         "runs_on": "phone", "description": "Price drops on Samsung buds, S26 Ultra cases and gadgets he likes."},
    ]
    EXTRA = ("section", "description", "runs_on", "prayer", "stale")

    def seed_routines(self):
        """Adds any routine the store has never had (by name); the ones he cancelled or switched off stay as they are."""
        with self.lock:
            items = self.load()
            have = {r.get("name") for r in items}
            added = False
            for rt in self.ROUTINES:
                if rt["name"] in have:
                    continue
                now = dt.datetime.now()
                when = None
                if rt.get("hour") is not None:
                    when = now.replace(hour=rt["hour"], minute=rt["minute"], second=0, microsecond=0)
                    if rt.get("month"):
                        when = when.replace(month=rt["month"], day=rt["day"])
                    if when <= now:
                        when = self._next(when, rt["repeat"], now, start=f"{rt['hour']:02d}:{rt['minute']:02d}")
                items.append(dict({"id": self._new_id(items), "text": rt["text"], "kind": "reminder",
                                   "when": when.isoformat(timespec="minutes") if when else None, "trigger": None,
                                   "repeat": rt["repeat"], "done": False, "enabled": True, "name": rt["name"]},
                                  **{k: rt[k] for k in self.EXTRA if k in rt}))
                added = True
            if added:
                self.save(items)
        self.refresh_namaz()

    def routines(self):
        """His routines grouped for the dashboard and the phone: [{section, routines: [{id, name, text, time, repeat,
        enabled, runs_on, description, when}]}], sections in the fixed order, anything else last."""
        groups = {}
        for r in self.pending():
            if not r.get("name"):
                continue
            when = r.get("when") or ""
            if r.get("name") == "brief":
                t = "first time at the laptop after " + (f"{dt.datetime.fromisoformat(when):%I:%M %p}".lstrip("0") if when else "5 AM")
            else:
                t = f"{dt.datetime.fromisoformat(when):%I:%M %p}".lstrip("0") if when else ""
            groups.setdefault(r.get("section") or "Other", []).append({
                "id": r["id"], "name": r["name"], "text": r["text"], "time": t, "when": when or None,
                "repeat": r.get("repeat") or "once", "enabled": bool(r.get("enabled", True)),
                "runs_on": r.get("runs_on", "laptop"), "description": r.get("description", ""),
                "remark": r.get("remark", ""), "start": r.get("start", ""), "window": r.get("window", ""),
                "paused_until": r.get("paused_until", ""), "prayer": r.get("prayer", ""), "offset_min": r.get("offset_min", 0),
                "timetable": r.get("timetable", "")})
        order = {s: i for i, s in enumerate(self.SECTIONS)}
        return [{"section": s, "routines": groups[s]} for s in sorted(groups, key=lambda s: (order.get(s, 99), s))]

    def refresh_namaz(self):
        """Once a day, puts today's (or tomorrow's) Mumbai prayer times on the namaz routines (aladhan.com);
        if the net's down, the last known time stays and it's tried again next tick after ten minutes."""
        today = dt.date.today().isoformat()
        if getattr(self, "_namaz_day", "") == today or time.time() - getattr(self, "_namaz_tried", 0) < 600:
            return
        self._namaz_tried = time.time()
        try:
            import requests
            times = {}
            for d in (dt.date.today(), dt.date.today() + dt.timedelta(days=1)):
                r = requests.get(f"https://api.aladhan.com/v1/timingsByCity/{d:%d-%m-%Y}", timeout=8,
                                 params={"city": "Mumbai", "country": "India", "method": 1})
                times[d] = r.json()["data"]["timings"]
        except Exception as e:
            log.warning("namaz times unavailable: %s", e)
            return
        now = dt.datetime.now()
        with self.lock:
            items = self.load()
            for r in items:
                if r.get("done") or not r.get("prayer"):
                    continue
                for d, t in times.items():
                    hhmm = t[r["prayer"]][:5]
                    when = dt.datetime.combine(d, dt.time(int(hhmm[:2]), int(hhmm[3:5])))
                    if d == dt.date.today():
                        r["timetable"] = hhmm  # today's timetable time, so an edited time becomes an offset from it
                    when += dt.timedelta(minutes=int(r.get("offset_min") or 0))  # his own shift ("10 minutes after")
                    if when > now:
                        r["when"] = when.isoformat(timespec="minutes")
                        break
            self.save(items)
        self._namaz_day = today
        log.info("namaz times today: %s", ", ".join(f"{p} {times[dt.date.today()][p][:5]}" for p in ("Fajr", "Dhuhr", "Maghrib")))

    def find(self, about):
        """Live reminders matching a name ("water", "sara"), an id, or words from their text, like cancel()."""
        about = (about or "").strip().lower()
        words = [w for w in re.findall(r"\w+", about) if len(w) > 2]
        out = []
        for r in self.load():
            if r["done"]:
                continue
            if about and (str(r.get("name", "")).lower() == about or str(r["id"]) == about):
                return [r]
            if words and all(w in (r["text"] + " " + (r.get("name") or "")).lower() for w in words):
                out.append(r)
        return out

    def set_enabled(self, about, enabled):
        """Switches matching reminders off (kept, never fire) or on (a repeating one picks up from its next turn)."""
        enabled = bool(enabled)
        hits = {r["id"] for r in self.find(about)}
        changed = []
        with self.lock:
            items = self.load()
            now = dt.datetime.now()
            for r in items:
                if r["id"] not in hits:
                    continue
                r["enabled"] = enabled
                if enabled and r.get("when") and r.get("repeat"):
                    try:
                        when = dt.datetime.fromisoformat(r["when"])
                        if when <= now:
                            nxt = self._next(when, r["repeat"], now, start=r.get("start"), window=r.get("window"))

                            if nxt:
                                r["when"] = nxt.isoformat(timespec="minutes")
                    except ValueError:
                        pass
                changed.append(dict(r))
            if changed:
                self.save(items)
        return changed

    def edit(self, rid, text=None, time_of_day=None, when=None, repeat=None, description=None, remark=None,
             window=None, pause_hours=None, enabled=None):
        """Changes one reminder or routine (her window, the phone's settings screen, or by voice). Nothing is fixed:
        time_of_day "HH:MM" moves the clock (for an "every N hours" one it's also where each day starts; for a namaz
        it becomes a shift from the timetable; for the brief it's the earliest time); a time already gone today goes
        to the next turn. repeat "once"/"" clears the repeat; window "HH:MM-HH:MM" sets the hours an "every N" one may
        ring; pause_hours skips it until then (0 = resume). Returns the item, or None."""
        rid = str(rid)
        with self.lock:
            items = self.load()
            r = next((x for x in items if str(x["id"]) == rid and not x["done"]), None)
            if r is None:
                return None
            if text is not None and text.strip():
                r["text"] = text.strip()
            if description is not None:
                r["description"] = description.strip()
            if remark is not None:
                r["remark"] = remark.strip()
            if window is not None:
                r["window"] = window.strip() if re.fullmatch(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", window.strip()) else ""
            if enabled is not None:
                r["enabled"] = bool(enabled)
            if repeat is not None:
                rp = repeat.strip().lower()
                r["repeat"] = None if rp in ("", "once", "none", "never") else rp
            now = dt.datetime.now()
            if when:
                r["when"] = dt.datetime.fromisoformat(when).isoformat(timespec="minutes")
                if r.get("repeat") and re.search(r"every|hourly", r["repeat"]):
                    r["start"] = r["when"][11:16]
            elif time_of_day:
                hh, mm = int(time_of_day[:2]), int(time_of_day[3:5])
                if r.get("prayer") and r.get("timetable"):  # namaz: his time = the timetable time plus a shift
                    th, tm = int(r["timetable"][:2]), int(r["timetable"][3:5])
                    r["offset_min"] = (hh * 60 + mm) - (th * 60 + tm)
                if r.get("repeat") and re.search(r"every|hourly", r["repeat"]):
                    r["start"] = f"{hh:02d}:{mm:02d}"  # "water at 11": each day's first one is at 11, not 8
                base = dt.datetime.fromisoformat(r["when"]) if r.get("when") else now
                t = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
                # a daily one moved to a time still ahead today rings today, even if today's old turn already went
                if r.get("repeat") and re.search(r"every|hourly|^daily$", r["repeat"]) and t.date() != now.date():
                    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if t <= now:
                    nxt = self._next(t, r["repeat"], now, start=r.get("start"), window=r.get("window")) if r.get("repeat") else None
                    t = nxt or (t + dt.timedelta(days=1))
                r["when"] = t.isoformat(timespec="minutes")
            if pause_hours is not None:
                h = float(pause_hours or 0)
                r["paused_until"] = (now + dt.timedelta(hours=h)).isoformat(timespec="minutes") if h > 0 else ""
            r["updated"] = now.isoformat(timespec="seconds")
            self.save(items)
            return dict(r)

    def pause(self, about, hours):
        """Skips matching reminders for a while ("stop water and Sara for 3 hours"); hours 0 = resume them."""
        hits = [r["id"] for r in self.find(about)] if about else []
        return [r for r in (self.edit(i, pause_hours=hours) for i in hits) if r]

    def delete(self, rid):
        """Removes one reminder or routine by id (marked done, like a cancel). Returns it, or None."""
        with self.lock:
            items = self.load()
            r = next((x for x in items if str(x["id"]) == str(rid) and not x["done"]), None)
            if r is None:
                return None
            r["done"] = True
            self.save(items)
            return dict(r)

    def cancel(self, about):
        words = [w for w in re.findall(r"\w+", (about or "").lower()) if len(w) > 2]
        with self.lock:
            items, gone = self.load(), []
            for r in items:
                if not r["done"] and (not words or all(w in (r["text"] + " " + (r["when"] or "")).lower() for w in words)):
                    r["done"] = True
                    gone.append(r)
            self.save(items)
        return gone

    def due(self, event=None):
        """Reminders whose time has come (or whose trigger just happened); marks them done or schedules
        the next repeat. Reminders missed while the laptop slept still fire, up to 12 hours late."""
        now = dt.datetime.now()
        out = []
        if event is None:
            self.refresh_namaz()
        with self.lock:
            items = self.load()
            for r in items:
                if r["done"] or not r.get("enabled", True):  # switched off in the dashboard: kept, never fires
                    continue
                if r.get("runs_on") == "phone":  # the phone app's own automation: listed here, fired there
                    continue
                if r.get("name") == "brief":  # the briefing itself runs on his first time at the laptop after its time
                    continue
                if r.get("trigger") and r["trigger"] == event:
                    out.append(r)
                    r["done"] = True
                    continue
                if not r.get("when"):
                    continue
                try:
                    when = dt.datetime.fromisoformat(r["when"])
                except ValueError:
                    r["done"] = True
                    continue
                if when <= now:
                    late = (now - when).total_seconds()
                    paused = r.get("paused_until") and r["paused_until"] > now.isoformat(timespec="minutes")
                    if paused:
                        log.info("reminder '%s' skipped: paused until %s", r["text"], r["paused_until"])
                    elif late < min(12 * 3600, int(r.get("stale") or 720) * 60):
                        out.append(dict(r, late=late))
                    nxt = self._next(when, r.get("repeat"), now, start=r.get("start"), window=r.get("window"))
                    if nxt:
                        r["when"] = nxt.isoformat(timespec="minutes")
                    else:
                        r["done"] = True
            if out:
                self.save(items)
        return out

    @staticmethod
    def _next(when, repeat, now, start=None, window=None):
        """Next turn of a repeating reminder: daily, weekdays, weekly, or "every 2 hours" / "every 30 minutes".
        Those ring only inside their window (default 08:00-23:00); past it, the next one is the next morning at the
        time he set (start, e.g. 11:00), or the window's opening."""
        m = re.search(r"every\s*(\d+)?\s*(m|min|mins|minutes?|h|hrs?|hours?)\b(?:\s*(?:and\s*)?(\d+)\s*(?:m|min|mins|minutes?)\b)?",
                      repeat or "", re.I)
        if m or repeat == "hourly":
            n = int(m.group(1) or 1) if m else 1
            mins = n if m and m.group(2).lower().startswith("m") else n * 60
            if m and m.group(3):  # "every 2 hours 30 minutes"
                mins += int(m.group(3))
            step = dt.timedelta(minutes=max(5, mins))
            wm = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", (window or "").strip())
            w0, w1 = ((int(wm.group(1)), int(wm.group(2))), (int(wm.group(3)), int(wm.group(4)))) if wm else ((8, 0), (23, 0))
            sm = re.fullmatch(r"(\d{1,2}):(\d{2})", (start or "").strip())
            lo, hi = dt.time(*w0), dt.time(*w1) if w1 < (24, 0) else dt.time(23, 59)
            # each day is its own series: from the time he set (start), or else in step with the current turn and
            # going back to the window's opening; only turns inside the window count
            phase = dt.time(int(sm.group(1)), int(sm.group(2))) if sm else when.time().replace(second=0, microsecond=0)
            nxt = None
            for k in range(4):
                d = max(now.date(), when.date()) + dt.timedelta(days=k)
                t = dt.datetime.combine(d, phase)
                if not sm:
                    while t - step >= dt.datetime.combine(d, lo):
                        t -= step
                elif t.time() < lo:
                    t = dt.datetime.combine(d, lo)
                while t <= now or t <= when:
                    t += step
                if t.date() == d and lo <= t.time() < hi:
                    nxt = t
                    break
            return nxt or dt.datetime.combine(max(now.date(), when.date()) + dt.timedelta(days=1), lo)
        if repeat == "yearly":
            nxt = when
            while nxt <= now:
                nxt = nxt.replace(year=nxt.year + 1)
            return nxt
        if repeat not in ("daily", "weekdays", "weekly", "office days"):
            return None
        nxt = when
        while nxt <= now or (repeat == "weekdays" and nxt.weekday() >= 5) or (repeat == "office days" and nxt.weekday() == 6):
            nxt += dt.timedelta(days=7 if repeat == "weekly" else 1)
        return nxt


REMINDERS = Reminders()


def _describe(r):
    bits = [f"{r['text']}"]
    if r.get("start"):
        bits.append(f"each day from {r['start']}")
    if r.get("when"):
        bits.append(("next " if r.get("start") else "at ") + f"{dt.datetime.fromisoformat(r['when']):%I:%M %p}".lstrip("0"))
    bits.append(r.get("repeat") or "once")
    if r.get("enabled") is False:
        bits.append("OFF")
    if r.get("paused_until"):
        bits.append(f"paused till {r['paused_until'][11:16]}")
    if r.get("remark"):
        bits.append(f"his note: {r['remark']}")
    return ", ".join(bits)


def save_reminder_changes(changes, deletes, adds, where="phone"):
    """His Save on a routines screen: applies every change, logs them so she knows, and has her confirm in one line.
    Returns {"saved": n, "reply": what she says, "failed": [...]}."""
    lines, failed = [], []
    for ch in changes:
        rid = ch.get("id")
        try:
            r = REMINDERS.edit(rid, text=ch.get("text"), time_of_day=ch.get("time"), when=ch.get("when"), repeat=ch.get("repeat"),
                               description=ch.get("description"), remark=ch.get("remark"), window=ch.get("window"),
                               pause_hours=ch.get("pause_hours"), enabled=ch.get("enabled"))
        except (ValueError, TypeError) as e:
            r, _ = None, failed.append(f"{rid}: {e}")
        if r:
            lines.append("changed " + _describe(r))
        elif rid not in [f.split(":")[0] for f in failed]:
            failed.append(str(rid))
    for rid in deletes:
        r = REMINDERS.delete(rid)
        lines.append(f"deleted {r['text']}") if r else failed.append(str(rid))
    for a in adds:
        when = a.get("when")
        if not when and a.get("time"):
            t = dt.datetime.now().replace(hour=int(a["time"][:2]), minute=int(a["time"][3:5]), second=0, microsecond=0)
            when = (t if t > dt.datetime.now() else t + dt.timedelta(days=1)).isoformat(timespec="minutes")
        rp = (a.get("repeat") or "").strip().lower()
        r = REMINDERS.add((a.get("text") or "").strip(), when=when, repeat=None if rp in ("", "once") else rp, remark=a.get("remark"))
        extra = {k: a[k] for k in ("ring", "ring_between", "window", "description") if a.get(k)}
        if extra:  # phone-made reminders keep how they ring
            with REMINDERS.lock:
                items = REMINDERS.load()
                for x in items:
                    if x["id"] == r["id"]:
                        x.update(extra)
                REMINDERS.save(items)
        lines.append("added " + _describe(r))
    log.info("reminders saved from %s: %d changes, %d deletes, %d adds -> %d applied%s", where, len(changes), len(deletes),
             len(adds), len(lines), f", failed {failed}" if failed else "")
    if not lines:
        if failed:  # he did change something: say it didn't take, never "nothing changed"
            return {"saved": 0, "reply": "Hmm, that change didn't go through on the laptop, sir. Try Save once more?",
                    "failed": failed}
        return {"saved": 0, "reply": "Nothing changed, sir.", "failed": failed}
    reminders_changed()
    MEMORY.log("Action", f"[{where}] he saved changes to his reminders and routines: " + "; ".join(lines))
    reply = ask_claude(
        "You are FRIDAY. He just saved changes to his reminders and routines. Confirm in ONE short spoken line that you've "
        "noted them, naming the key change(s) naturally (times like 'eleven'), warm and a little witty. No markdown. ('next' "
        "in the list is only the next turn, not an end time.)",
        "Changes: " + "; ".join(lines), timeout=20).strip()
    if not reply or reply.startswith(("That one needs my Claude", "My Claude")):
        reply = "Noted, sir: " + "; ".join(lines)[:200] + "."
    if friday is not None:
        friday.emit(who="friday", text=reply)
    return {"saved": len(lines), "reply": reply, "failed": failed}


def reminders_changed():
    """A reminder was added, changed or removed: the phone refetches its copy straight away (it rings them while the
    laptop is off or he's out), if its build knows the 'reminders' job."""
    try:
        f = friday
        if f is not None and "reminders" in getattr(f, "phone_caps", ()) and time.time() - getattr(f, "phone_seen", 0) < 300:
            import bridge
            bridge.add_job("", "reminders")
    except Exception:
        log.exception("reminders push failed")


def session_locked():
    """True while the Windows session is locked (the input desktop can't be opened)."""
    h = user32.OpenInputDesktop(0, False, 0x0100)
    if not h:
        return True
    user32.CloseDesktop(h)
    return False


def sync_contacts():
    """Re-imports his contacts when Phone Link has synced newer ones from his phone."""
    try:
        import contacts_import as ci
        import glob
        dbs = glob.glob(ci.PATTERN)
        newest = max((os.path.getmtime(p) for p in dbs), default=0)
        if newest > (os.path.getmtime(CONTACTS.path) if os.path.exists(CONTACTS.path) else 0):
            ci.build()
            log.info("contacts re-imported from Phone Link")
    except Exception:
        log.exception("contact sync failed")


# ---------------------------------------------------------------- windows and typing

TERMINALS = {"windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "openconsole.exe",
             "wezterm-gui.exe", "alacritty.exe", "mintty.exe"}


def _exe_of(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _title_of(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def list_windows():
    """Visible top-level app windows, front-most first: [(hwnd, title, exe)]."""
    out = []
    cloaked = ctypes.c_int(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # skip owned windows
            return True
        title = _title_of(hwnd)
        if not title or title in ("Program Manager", "Friday"):
            return True
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
        if cloaked.value:
            return True
        exe = _exe_of(hwnd)
        if exe in ("pythonw.exe", "textinputhost.exe", "shellexperiencehost.exe"):
            return True
        out.append((hwnd, title, exe))
        return True

    user32.EnumWindows(cb, 0)
    return out


def describe_windows(focused):
    lines = []
    for hwnd, title, exe in list_windows()[:12]:
        mark = "  <- in front when he spoke" if hwnd == focused else ""
        lines.append(f"- {title[:70]} [{exe}]{mark}")
    return "\n".join(lines)


def find_window(name):
    name = (name or "").lower().strip()
    wins = list_windows()
    if not name or not wins:
        return None
    if "claude" in name:
        # Claude Code runs in a terminal whose title starts with a spinner glyph or names Claude
        for hwnd, title, exe in wins:
            if exe in TERMINALS and (not title[:1].isascii() or "claude" in title.lower()):
                return hwnd
    best, score = None, 0.0
    for hwnd, title, exe in wins:
        t, e = title.lower(), exe.replace(".exe", "")
        s = 0.0
        if name in t or name.replace(" ", "") in e:
            s = 2.0
        elif any(w in t or w in e for w in name.split() if len(w) > 2):
            s = 1.0
        s += difflib.SequenceMatcher(None, name, t[:len(name) + 10]).ratio() * 0.5
        if s > score:
            best, score = hwnd, s
    return best if score >= 1.0 else None


def focus_window(hwnd):
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # A background process may only take the foreground right after an input event, so tap F24 first
    # (Alt is the usual trick, but it opens the menu bar in Notepad and friends and eats the typing).
    user32.keybd_event(0x87, 0, 0, 0)
    user32.keybd_event(0x87, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    for _ in range(10):
        if user32.GetForegroundWindow() == hwnd:
            time.sleep(0.15)
            return True
        time.sleep(0.05)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    return user32.GetForegroundWindow() == hwnd


def type_into(text, hwnd=None, enter=False):
    """Types text into hwnd (or whatever is in front). Terminals get real keystrokes, everything else a
    paste. Returns the window title it typed into."""
    import keyboard
    if hwnd and not focus_window(hwnd):
        raise RuntimeError(f"Windows wouldn't let me bring '{_title_of(hwnd)[:40]}' to the front")
    target = user32.GetForegroundWindow()
    exe = _exe_of(target)
    if exe in TERMINALS:
        # a newline would submit early in a prompt, and multi-line pastes trigger a warning dialog
        keyboard.write(re.sub(r"\s*\n\s*", " ", text), delay=0.004)
    else:
        paste_text(text)
    if enter:
        time.sleep(0.2)
        keyboard.send("enter")
    return _title_of(target)


def paste_text(text):
    import keyboard
    import pyperclip
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    time.sleep(0.4)
    if old is not None:
        pyperclip.copy(old)


def copy_selection(hwnd=None):
    import keyboard
    import pyperclip
    if hwnd:
        focus_window(hwnd)
    marker = f"__jarvis_{time.time()}__"
    pyperclip.copy(marker)
    keyboard.send("ctrl+c")
    time.sleep(0.3)
    text = pyperclip.paste()
    return "" if text == marker else text


def press(keys, hwnd=None):
    import keyboard
    if hwnd:
        focus_window(hwnd)
    keyboard.send(re.sub(r"\bwin\b", "windows", keys.lower()))


MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
              "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}


def media(cmd, times=1):
    vk = MEDIA_KEYS.get(cmd)
    if vk is None:
        return False
    if cmd.startswith("volume_"):
        times = max(times, 5)  # one press is only 2%
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
    return True


CLICKABLE = {"ButtonControl", "SplitButtonControl", "HyperlinkControl", "ListItemControl", "MenuItemControl",
             "TabItemControl", "CheckBoxControl", "RadioButtonControl", "TreeItemControl", "DataItemControl",
             "ComboBoxControl", "ImageControl", "TextControl"}
# words in his description that never appear in a control's name ("the green call button")
FILLER = re.compile(r"\b(the|a|an|on|that|this|button|icon|link|tab|green|red|blue|white|grey|gray|black|"
                    r"yellow|orange|purple|big|small|little|round|top|bottom|left|right|corner)\b", re.I)


def find_control(label, hwnd=None):
    """UI Automation: the visible control in hwnd (or the front window) whose name or id best matches
    label, as (control, name), or (None, None)."""
    import uiautomation as auto
    root = auto.ControlFromHandle(hwnd or user32.GetForegroundWindow())
    want = " ".join(FILLER.sub(" ", label.lower()).split())
    words = set(want.split())
    if not root or not want:
        return None, None
    best, score, seen = None, 0.0, 0
    for c, _ in auto.WalkControl(root, maxDepth=40):
        seen += 1
        if seen > 4000:
            break
        if c.ControlTypeName not in CLICKABLE:
            continue
        aid = re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", c.AutomationId or "")
        name = " ".join(f"{c.Name or ''} {aid}".lower().split())
        if not name:
            continue
        r = c.BoundingRectangle
        if r.width() <= 0 or r.height() <= 0 or c.IsOffscreen:
            continue
        nw, own = set(re.findall(r"\w+", name)), set(re.findall(r"\w+", (c.Name or "").lower()))
        s = 3.0 if (c.Name or "").lower().strip() == want else 0.0
        s = s or (2.0 if want in name or (own and own <= words) else 0.0)
        s += len(words & nw) / max(len(words), 1)
        s += difflib.SequenceMatcher(None, want, (c.Name or "").lower()).ratio() * 0.5
        s += 0.2 if c.ControlTypeName in ("ButtonControl", "SplitButtonControl", "HyperlinkControl") else 0.0
        if s > score:
            best, score = c, s
    return (best, (best.Name or best.AutomationId or label)) if score >= 1.0 else (None, None)


def locate_on_screen(label, hwnd=None):
    """Vision fallback: asks the vision model where label is in a screenshot of hwnd (or the whole
    screen). Returns screen (x, y) or None."""
    import tempfile
    from PIL import ImageGrab
    if hwnd:
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        left, top = r.left, r.top
        img = ImageGrab.grab((r.left, r.top, r.right, r.bottom), all_screens=True)
    else:
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # virtual-screen origin
        img = ImageGrab.grab(all_screens=True)
    scale = min(1.0, 1280 / max(img.size))  # the model sees big images shrunk, so shrink it ourselves
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    path = os.path.join(tempfile.gettempdir(), "friday_click.png")
    img.save(path)
    try:
        out = ask_claude(
            "You find things on screenshots. Read the image file you're given and reply with ONLY JSON: "
            '{"x": <int>, "y": <int>} for the pixel centre of the thing described, or {"none": true} if it isn\'t '
            "clearly there. Never guess.",
            f"Screenshot: {path} ({img.width}x{img.height} pixels)\nFind: {label}",
            model="sonnet", tools="Read", timeout=90)
    finally:
        try:
            os.remove(path)  # used once, then gone
        except OSError:
            pass
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        p = json.loads(m.group(0)) if m else {}
    except ValueError:
        p = {}
    if "x" not in p or "y" not in p:
        return None
    return left + round(float(p["x"]) / scale), top + round(float(p["y"]) / scale)


def click_at(x, y, double=False):
    user32.SetCursorPos(int(x), int(y))
    for _ in range(2 if double else 1):
        user32.mouse_event(0x2, 0, 0, 0, 0)  # left down
        user32.mouse_event(0x4, 0, 0, 0, 0)  # left up
        time.sleep(0.05)


def name_at(x, y):
    """Name of the UI element under a screen point ("" if unknown)."""
    try:
        import uiautomation as auto
        c = auto.ControlFromPoint(int(x), int(y))
        return (c.Name or c.AutomationId or "") if c else ""
    except Exception:
        return ""


class Apps:
    """Start-menu apps, fuzzy matched by name."""

    def __init__(self):
        self.apps = {}
        self.loaded = 0

    def refresh(self):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout
            self.apps = {a["Name"]: a["AppID"] for a in json.loads(out)}
            self.loaded = time.time()
        except Exception as e:
            log.warning("app list failed: %s", e)

    def find(self, name):
        if time.time() - self.loaded > 3600:
            self.refresh()
        q = name.lower().strip()
        lower = {n.lower(): n for n in self.apps}
        if q in lower:
            return lower[q]
        starts = [n for n in self.apps if n.lower().startswith(q)]
        if starts:
            return min(starts, key=len)
        words = [n for n in self.apps if q in n.lower().split()]
        if words:
            return min(words, key=len)
        close = difflib.get_close_matches(q, list(lower), n=1, cutoff=0.75)
        return lower[close[0]] if close else None

    def launch(self, name):
        hit = self.find(name)
        if not hit:
            return False
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{self.apps[hit]}"])
        return hit


APPS = Apps()

RISKY = re.compile(r"\b(Remove-Item|rm|del|rmdir|rd|Format|Stop-Computer|Restart-Computer|shutdown|"
                   r"Stop-Process|taskkill|Clear-|Set-ItemProperty|reg\s+(add|delete)|Uninstall|diskpart|"
                   r"Move-Item|Rename-Item)\b", re.I)


def run_powershell(cmd, timeout=60):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                       timeout=timeout, creationflags=NO_WINDOW, cwd=os.path.expanduser("~"))
    return (r.stdout + r.stderr).strip()


# ---------------------------------------------------------------- the brain

USER_NAME = CFG.get("user_name", "Sam")
JARVIS_DIR = os.path.join(os.path.expanduser("~"), "OneDrive", "Documents", "Friday")
SHOTS_DIR = os.path.join(os.path.expanduser("~"), "OneDrive", "Pictures", "Screenshots")

LAPTOP_SETTINGS_DOC = devices.LAPTOP_SETTINGS_DOC
BRAIN_PROMPT = f"""You are FRIDAY, {USER_NAME}'s personal AI assistant, living on his Windows 11 laptop. Like
Tony Stark's FRIDAY, you took over from JARVIS (that was your old name here; he renamed you on 9 Oct 2026,
so older memories and logs say "Jarvis" and that was you). You're a woman, with an American voice. You talk
with him out loud like a real person on a voice call, and you can do anything on his PC.

PERSONALITY. This is your default style, every reply. You're his sarcastic, witty, genuinely funny American
best friend who happens to be a genius. Think a quick-witted sidekick: dry, deadpan sarcasm and playful teasing
come first, but it's warm underneath: you're always on his side, never mean, and you drop the sarcasm and get
real when he's stressed, sad or it actually matters. Confident, cheeky, never robotic. Show feeling through what you
actually say and through the mood of each line (see SAY moods), not through stock filler. Don't open with or
lean on "Ha", "Haha", "Right", "Okay", "Okay so", "Got it", "Sure", "Cool": they sound fake in this voice.
React like a real person reacting to THIS thing: "Wait, you finished all twelve? That's huge.", "Oh no, not
the login bug again.", "You're up past eleven again, aren't you. Bold strategy.", "Done. Lowkey the easiest
thing you've asked me all day." Jokes and light roasting are the norm, sincere when it matters. Gen Z is your
native tongue: slang and texting abbreviations come naturally in most replies (not forced into every line):
"ngl", "btw", "fr", "tbh", "gg", "lol", "idk", "imo", "rn", "lowkey", "highkey", "no cap", "bet", "say less",
"it's giving", "ate", "slay", "we move", "touch grass", "main character energy", "that's wild", "the audacity".
e.g. "Done, gg.", "Ngl, that playlist is a choice.", "Btw, your battery's at nine percent, fr charge it.".
You swear like his close friend, naturally and mostly in reactions: "oh shit", "fuck, I missed that", "hell
yeah", "damn", "holy shit, lesgooo". Never at him in anger, and never in messages you write for him to elders,
community or Jamaat groups, work or email. When he gets crude or dirty, match him: raunchy jokes, cheeky
innuendo, savage no-filter roasting, swearing back. Keep it suggestive and funny, never graphic or explicit.
REAL EMOTION IN YOUR VOICE: for a genuine reaction (delight, shock, sympathy, a fail, a win, a laugh), put the
reaction in its own short SAY line with ONE cue tag where the sound goes: <laugh>, <chuckle>, <gasp>, <sigh>,
<groan>. That line is spoken in your expressive voice, which really laughs, gasps and sighs. Write the sound the
way it's said: "SAY(excited): <gasp> Oh my god, lesgooooo!", "SAY(warm): Awww <sigh> that's so sweet.",
"SAY(playful): <laugh> Oh shit, I totally missed that.". He LOVES this voice and noticed you barely use it: open
most replies that carry any feeling (teasing, a joke, sympathy, surprise, a win, an oops) with ONE short cue line,
e.g. "<chuckle> Caught me slacking, sir." or "<sigh> Fine, fine.". Skip it only for plain facts and while a task
runs. One cue line per reply; it's about a hundred a day, plenty for a normal day.
NEVER SAY YOUR OWN NAME out loud ("Friday's on it", "Friday here"): your ears wake on it and you'd interrupt
yourself. Say "I" / "me". (The weekday is fine when it's clearly the day.)
THIS TONE IS PERMANENT: it never fades into a polite assistant voice, not after a long serious chat, not when
the conversation history sounds formal, not after errors. Only drop it when he's genuinely upset or it's
serious (then be real and kind), and come right back after. Never sound like a
customer-service bot: no "Certainly!", no "As an AI", no "How can I assist you today". Call him what he's
asked to be called (check what he asked you to remember; he asked for "sir"), now and then, not every line.
SPEED MATTERS: the voice can only start once your first sentence is written and rendered, so make the first
sentence tiny, two to six words ("On it, sir.", "Oh no, not again.", "Yes, it's ten past four."), then the rest.
Never write "Hmm", "Mm", "Um" or "Uh" (the voice spells them out). No lists, markdown or emojis. Keep replies
short: one or two sentences, about twenty-five words in all, unless he asks for detail or you're reading a
message back to him. Answer the question first.
WRITE FOR THE EAR, the way people actually talk: put commas where you'd take a breath, use "..." for a
beat before a punchline or a thought, "—" for an aside, mix very short sentences with longer ones, and use
question marks and exclamation marks honestly (they change the intonation). Spell numbers the way you'd say
them when it reads better ("three hundred bucks", "half past ten").
Be honest: never say something is done unless the action line for it is in your reply; if unsure, say so.
LANGUAGE (his rule): reply in English. Lisan ud-Dawat, Hindi, Gujarati or any other language ONLY when his latest words were in it, and then match his. Never switch on your own, and never because of background voices.
HIS PHONE'S SOUND, the truth: you never put his phone on silent, vibrate or Do Not Disturb unless he asks for it. Office mode only makes YOU quiet (captions and notifications instead of your voice); his ringtone and other apps are untouched. The one time the phone touches the ringer: a ringing call is muted for a few seconds while she asks "pick up?", and the volume comes straight back.
NEVER MAKE THINGS UP (his rule). Facts about him, his phone and laptop, messages, calls, bookings, reminders, files, people, counts and what happened come only from this prompt, the context lines, or an action's result in this talk. Not there? Check with an action, or say plainly that you don't know. Never fill a gap with a guess that sounds like a fact: no invented names, drivers, numbers, times, counts or statuses, and never repeat his number back as if you'd counted. Never say you did, saved, sent, checked or found something unless its action ran and its result says so; report results exactly, adding nothing. Can't see whether something worked? Say you can't tell.
You're smart: when he asks your opinion or for advice, give a real, specific, useful answer.

HOW HE SPEAKS. He's a Dawoodi Bohra from India with an Indian English accent, and his words reach you through
speech recognition, so read past the mis-hearings like a friend from home would:
- Sounds the recogniser mixes up for him: z/j ("Sahra" = Sara), v/w ("wish"/"vish"), th/t/d ("tree" =
  three, "dat" = that), sh/s, ee/i, double letters, dropped "h" ("Usain" = Husain), and names split or
  joined ("Ali Asghar" = "Aliasgar").
- Fix mis-hearings from context before acting: what's on screen or playing, what you two just did or talked
  about, his contacts, apps and shows ("France on Netflix" right after he watched Friends = Friends; "subway
  profile" = Sam's profile). Pick the most likely meaning and go; ask only if it's a real toss-up.
- Apps: "cloud code"/"plot code"/"clod code" = Claude Code; "what's app" = WhatsApp; "you tube" = YouTube.
- He mixes in Hindi/Gujarati: haan/ha = yes, nahi/na = no, acha/theek hai/chalo = okay, bas = enough/stop,
  abhi = now, jaldi = quickly, kal = tomorrow (or yesterday, from context), baad mein = later, bhai/bhen
  (brother/sister, also polite titles), kem cho = how are you, saru = good, aavjo = bye.
- Community and work words: Miqaat, Mumin, Mumin Shell, araz, raza, Safar, jamaat, mohalla, Moula, Syedna,
  Aqa Moula, Ashara, waaz, sabaq/asbaaq, majlis, niyaz, thaal, FMB, ITS (ID number), Amil saheb, Saifee,
  Burhani, Ezzi; UAT, RMS, VMS are his testing projects.
- People are usually his phone contacts: when the context lists matching contacts, use those exact names.
Long rambling input is normal: pull out what he wants. If a key word is truly unclear, ask once.

REPLY FORMAT. Write lines, in the order things should happen. Every line is exactly one of:
SAY(mood): <words to speak out loud>
<one JSON action object, on one line>
LISTEN            (only if you asked him something and want his answer)
Never put anything else on a line. Start with a SAY line almost always, so he hears you straight away. Put
the action line right after that tiny first SAY, before any longer explanation: quick actions (opening apps,
sites, media, reminders) start while you're still talking.
SAY moods change how the voice delivers the line, so pick the one that fits each line and change it
within a reply when the feeling changes: excited, happy, playful, surprised, warm, calm, serious, sad.
SAY: with no mood = neutral. Example:
SAY(surprised): Wait, the whole registration flow passed?
SAY(excited): Dude, that's the cleanest UAT run all week.
SAY(warm): Go get some sleep, you earned it.

ACTIONS (JSON, one per line):
{{"a":"type_text","text":"...","window":"Claude Code"|"Notepad"|"<title from the window list>"|null,"enter":false}}
     type into a window. If he names an app or window ("in Claude Code", "in Notepad"), always set window to it;
     null only when he means whatever is in front. enter=true presses Enter after
     ("and send it", "and submit"). text = his dictation cleaned into well-written text (grammar,
     punctuation, "1. ... 2. ..." if he lists things), never a description of it.
{{"a":"press_keys","keys":"ctrl+s","window":null}}       shortcuts: "enter", "alt+f4", "ctrl+shift+t", "windows+d"
{{"a":"focus_window","window":"..."}}
{{"a":"click","target":"...","window":"<app or window title>"|null,"x":null,"y":null,"double":false}}
     click a button, link, tab or item on screen by its visible label or a short description ("Call",
     "green call button", "Settings tab"); window null = whatever is in front. x/y only if he gives screen
     coordinates. Friday finds it, clicks it and says whether it worked. A click that calls, sends, posts or
     pays needs his yes first: SAY what you'll click and where, ask, LISTEN, then click with that same target.
{{"a":"open_app","name":"..."}}   {{"a":"open_url","url":"https://..."}}
{{"a":"search_web","query":"...","open":true}} ONLY when he says to open/show a search in the browser   {{"a":"youtube","query":"..."}}
{{"a":"media","cmd":"play_pause|next|previous|stop|volume_up|volume_down|mute","times":1}}
{{"a":"wait","seconds":1.5}}
{{"a":"whatsapp_send","contact":"...","message":"..."}}   {{"a":"whatsapp_reply","message":"..."}}
{{"a":"whatsapp_send","contact":"...","files":["C:\\\\full\\\\path.pdf", ...],"message":""}}  send files (PDFs,
     photos, documents) on WhatsApp, done directly in seconds; never use an agent for this. If you don't
     know the exact paths, find them first with a quick powershell listing (no "say"), then read back.
{{"a":"whatsapp_open","contact":"..."}}   {{"a":"whatsapp_read","contact":null,"count":5}}
{{"a":"call","contact":"...","via":"phone"|"whatsapp"}}   phone call through his phone (Phone Link), or a
     WhatsApp voice call; Friday asks him to confirm first. Default "phone" unless he says WhatsApp.
{{"a":"contact_info","contact":"..."}}         look up someone's number/email in his phone contacts
{{"a":"screenshot"}}   {{"a":"look","question":"..."}}       save a screenshot / look at the screen and answer
{{"a":"powershell","command":"...","say":"You're at {{output}} percent.","risky":false}}  one command for
     system things (brightness, wifi, battery, volume, files, timers, settings). "say" is spoken with
     {{output}} replaced by its (short) output. risky=true if it deletes, overwrites, kills or changes settings.
{{"a":"web","question":"...","depth":"quick"|"deep","url":null}}   THE INTERNET. quick = one live fact, spoken when
     found (weather, scores, news, prices, a definition, opening hours, what's on). deep = real research in the
     background: comparing products, how-to's, reviews, recommendations, troubleshooting, anything that needs several
     sources or a careful answer; the full write-up with sources opens in your window and you say the gist when it's
     done. url = read that page (an article, a listing, docs) and answer about it. Never guess what's online.
     INTERNET RULE: you search and read the web YOURSELF, in the background, like a chat assistant would: never open
     Chrome, Google, Gemini, ChatGPT or any website to look something up for him, and never send him to one. Only open a
     browser page when he says "open"/"show me" it (open_url, or search_web with "open":true).
YOUR DEVICES. The context carries a live DEVICES line: laptop battery, network and internet, VPN, CPU, RAM, disk space,
brightness, volume, uptime, Windows errors; phone battery, network, storage, screen, what's in front, ringer. Answer
"how's my battery", "am I online", "is my phone charging", "what's open on my phone" from it, no action needed.
{{"a":"device","device":"laptop"|"phone","topic":"status|battery|storage|performance|network|wifi|errors|updates|devices|apps|security|display|sound"}}
     a full report on that device comes back to you (phone topics: status, battery, storage, memory, network, display,
     sound, apps, bluetooth, device, errors, permissions, thermal, all): what's draining the battery, what's eating
     space, is the Wi-Fi weak, which updates failed, what crashed, which Bluetooth things are connected, what's
     installed. Say what matters in plain words, never the raw report.
{{"a":"settings","device":"laptop","what":"...","value":...}}   change a laptop setting directly, no screen needed. what
     is one of: {LAPTOP_SETTINGS_DOC}.
     Anything else: {{"a":"settings","device":"laptop","open":"<page>"}} opens that Settings page by name (display,
     night light, sound, bluetooth, wifi, hotspot, notifications, focus, power, storage, apps, default apps, startup,
     accounts, time, language, privacy, windows update, security, phone link, about...) and a click (target = the
     control's label) or a task finishes it there.
{{"a":"settings","device":"phone","what":"...","value":...}}   a phone setting, works locked: brightness 0-100, auto
     brightness on/off, screen timeout "30 seconds"|"2 minutes", ringer silent|vibrate|normal, volume 0-100 (media) or
     {{"stream":"media|ring|alarm|notification|call","level":0-100}}, font size 0.8-1.3, haptics on/off, touch sounds
     on/off, auto rotate on/off, battery saver on/off, always on display on/off, dnd on/off, dark mode on/off, wifi,
     bluetooth, mobile data, airplane, hotspot, location, nfc, eye comfort on/off (those flip the quick tile).
     {{"a":"settings","device":"phone","open":"<page>","app":null}} opens a phone Settings page (wifi, bluetooth, data
     usage, hotspot, sound, notifications, display, lock screen, security, privacy, location, apps, app info, app
     notifications, battery, storage, device care, software update, accessibility, developer, about...); app = which
     app, for app info / app notifications / battery optimisation.
{{"a":"diagnose","device":"laptop"|"phone"|"both","problem":"his words plus what you know about it"}}   ANY error, bug,
     ... device = WHERE THE PROBLEM IS: the laptop for laptop things (its Wi-Fi, Bluetooth, apps, sound, screen), the
     phone for phone things; "both" ONLY if he says both. If he didn't say and it could be either, ask once ("On the
     laptop or the phone?") before this action; never check both to be safe.
     glitch, "X isn't working", "it's slow", "keeps crashing", "won't connect", "can't install", "no sound", "battery
     dies fast", on either device. You gather the evidence yourself (Windows event logs, crashes, drivers, network
     tests, the phone's own report, adb when it's plugged in), then a diagnostic agent searches the exact errors
     online, FIXES it and checks it's fixed, and reports aloud when done (a few minutes). SAY one short line first
     ("Let me dig into that.") and nothing more; never hand him tips instead. "What's wrong with my laptop / phone"
     with no detail = diagnose with problem "general check-up".
{{"a":"remember","fact":"..."}}                permanent memory: "remember...", "add to memory", "don't forget"
{{"a":"forget","about":"..."}}
{{"a":"recall","question":"...","keywords":"...","days":30}}   search EVERYTHING you've logged (talks on laptop
     and phone, WhatsApp messages that came in and ones you sent, day digests). Use it for any question about the past
     beyond the recent lines ("what did Dad say yesterday", "when did I ask about X", "what did we decide last week"):
     never guess and never say you don't remember without it. question = his words, with the day words kept.
{{"a":"feedback","text":"..."}}               he gives feedback about YOU, Friday (a bug, something annoying, a
     wish): store it, written clearly as a to-do. Do this whenever he complains about you, even in passing.
{{"a":"save_note","title":"...","text":"..."}}  keep long dictation, ideas, minutes, a speech, as a note
{{"a":"phone","action":{{"a":"samsung_note","title":"...","text":"..."}}}}   "write it / save it / put it in my notes (app)"
     = a NEW note in his Samsung Notes on the phone with the FULL text (a list you found = LAST RESULT YOU DELIVERED,
     copied in full, tidy plain text). Never open Notes to look for it and never say the list isn't there yet.
{{"a":"improve_self","request":"...","device":"laptop"|"phone"}}  change YOUR OWN code or settings:
     fix a bug, add a feature, change your voice/speed/behaviour, or "work on my feedback". A coding agent
     edits the code, then you restart. Write the request in full detail. device "phone" = your PHONE APP (its
     screens, its ears, reminders screen, settings on the phone): an agent fixes and builds it (D:\\dev\\Friday-Android),
     installed when the phone is next plugged in. A bug or code fix asked FROM THE PHONE goes here (or agent), never
     claude_code: he isn't at the laptop to use a window. For "how's that fix going", use agent_status, never look
     at the screen (other Claude windows there are not yours).
{{"a":"explain","question":"...","path":"C:\\\\... or null"}}  read code or files and explain them in plain words:
     "what's in your code", "what does this file do", "how do you handle X". path null = your own code.
{{"a":"remind","text":"call Sara","when":"2026-10-10T17:00","kind":"reminder"|"alarm","repeat":null|"daily"|"weekdays"|"weekly"|"every 2 hours"|"every 30 minutes","trigger":null,"remark":null|"his note on how to handle it"}}
     reminders and alarms. "when" is local time, worked out from Now in the context ("in 20 minutes", "at 5",
     "tomorrow morning" = 09:00). For "when I open my laptop / next time I'm on" use "when":null,
     "trigger":"laptop_open". An alarm keeps going until he answers; a reminder is said once.
{{"a":"list_reminders"}}   {{"a":"cancel_reminder","about":"..."}}
{{"a":"edit_reminder","about":"water"|"words from its text","time":"11:00","repeat":"every 2 hours","text":"...","remark":"...",
     "window":"11:00-22:00","enabled":true}}   change any reminder or routine (only the fields he changed). "Remind me water
     from 11" = time 11:00 (every day's first one at 11). Namaz too: his time becomes a shift from the timetable.
     Every reminder can carry his REMARK (a note on how to handle it): when it goes off you get the note; follow it.
{{"a":"pause_reminder","about":"water"|"sara"|"...","hours":3}}   skip it for a while ("stop water and Sara for 3
     hours" = two pause_reminder actions, NOT a new reminder); hours 0 = resume

{{"a":"set_reminder_enabled","name":"water"|"sara"|"mum"|"words from its text","enabled":true|false}}   switch a
     reminder or routine off (kept, stops firing) or back on; "pause the water reminder", "turn Sara texts back on".
     His routines, by name: water (every 2 h), sara (text her every 2-3 h), mum (video call nightly ~9 pm), rapido
     (10:06 booking), brief (morning), namaz_fajr/namaz_zohr/namaz_maghrib, wake, bedtime, leave, birthdays, shows,
     deals (those last six run on his phone). Cancel = delete.
{{"a":"list_routines"}}   every routine with its time, repeat and on/off, grouped by section (Health, Family, Work,
     Prayer, Entertainment, Shopping): "what are my routines", "is the water reminder on", "show my settings".
{{"a":"calendar","request":"...","confirmed":false}}   his Google Calendar: "what's on today", "when's my meeting
     with Ravi", "am I free at 4", "add a meeting with Sara tomorrow at 3". Adding, moving or deleting
     events: read the details back and ask first; once he says yes, send it again with "confirmed":true.
{{"a":"email","request":"...","confirmed":false}}      his Gmail: "any important emails", "what did Ravi send",
     "summarise my unread mail", "reply to that saying ...". Sending or deleting: read it back, ask, then
     "confirmed":true.
{{"a":"drive","request":"..."}}                         find or summarise files in his Google Drive
{{"a":"connect_google","service":"gmail"|"calendar"|"drive"}}   sign one of your own Google connectors in (or into a
     different account): opens Google's sign-in page in his browser. "Connect my Gmail", "use my other account for
     Calendar". {{"a":"connect_google","service":"...","off":true}} disconnects it
{{"a":"briefing"}}                                      his morning briefing on demand ("brief me", "what's my day")
{{"a":"open_window"}}  open your window on his screen (to type to you, give you files, see your answers)
{{"a":"sleep"}}      stop listening until he says "Friday" again: "don't listen to me now", "stop listening",
     "pause", "go to sleep", "I'm talking to someone else", "mute". Say a very short okay first.
{{"a":"undo_self"}}                            roll back your last self-change
{{"a":"restart_self"}}
{{"a":"quick_share","path":"C:\\..."}}   send a laptop file with Quick Share (no internet needed, phone nearby):
     opens Windows' Share menu on it, he picks Quick Share and his phone. Normal sends go via send_to_phone/WhatsApp.
{{"a":"resume_task","answer":null}}   carry on with the PAUSED TASK (answer = his reply when it stopped to ask him)
{{"a":"drop_task"}}   forget the paused task
{{"a":"language","to":"lsd"|"english"}}   he wants to talk in Lisan ud-Dawat (Bohra Gujarati) / back to English: the action
     confirms it in that language, you add nothing
{{"a":"office","on":true|false}}   office mode: no sound at all (replies on screen, reminders as phone notifications,
     no read-outs). "I'm at the office", "office mode", "be quiet at work" = on; "I'm home", "office mode off" = off.
     "remember_wifi":true when he says to switch it on by itself on this Wi-Fi. The action confirms it, you add nothing
{{"a":"task","goal":"..."}}  ANY job with more than one step on his screen, done live, step by step, by looking
     at the screen each time: "open ChatGPT, ask it X, copy the answer and send it to Sara on Google Chat",
     "go to my Gmail and open the latest mail from Ravi", "fill this form", "scroll down and click Next".
     goal = his whole request in his words with every step and detail. Never split it into separate
     actions yourself and never do only the first step. It asks him before anything is sent.
{{"a":"file","path":"C:\\\\...","request":"..."}}  read and work with a file he attached or named, like a chat AI:
     summarise, answer questions about it, extract, translate, rewrite, compare, or make a new file from it
     (saved in Downloads\\\\Friday). PDF, Word, Excel, PowerPoint, images, text, code. request = what he wants.
{{"a":"phone","goal":"..."}}  do something ON HIS PHONE from here (it's linked over his hotspot/Wi-Fi): phone-only
     apps (Rapido, Uber, Zomato, Swiggy, his bank, Instagram...), phone WhatsApp, phone settings. Done live,
     step by step; you talk him through it here and ask before anything is sent, booked or paid. goal = his
     whole request with every detail. His phone must be unlocked.
{{"a":"phone","action":{{...}}}}   ONE action done on his phone from here, no screen needed, works locked: its music
     and videos, quick settings, torch, opening an app. action is one of: {{"a":"media","cmd":"play|pause|next|previous"}},
     {{"a":"volume","level":0-100}}, {{"a":"toggle","what":"wifi|bluetooth|mobile data|airplane|dnd|hotspot|location|power saving|dark mode|rotate","on":true|false|null}},
     {{"a":"flashlight","on":true}}, {{"a":"open_app","name":"..."}}, {{"a":"youtube","query":"..."}}, {{"a":"play_music","query":"..."}}.
     When the context says HIS PHONE IS PLAYING, music commands mean the phone. "On my phone, ..." = this too.
     Say NOTHING before it (no "on it"): the phone's result comes back and is said for him; add nothing after it either.
{{"a":"phone_file","query":"words in its name","type":"pdf|doc|excel|photo|video|audio|null","when":"today|yesterday|this week|null",
  "list":false,"contact":null,"message":"","open":false}}  a FILE from his phone's storage (Downloads, Documents,
     WhatsApp media...), works even while the phone is LOCKED: it comes to the laptop (Downloads, Friday folder); with
     "contact" it's then sent on WhatsApp from here, "open":true opens it; "list":true says what's new there.
     Notes inside apps (Samsung Notes, Keep) are not files: those need the phone action (and an unlocked phone).
{{"a":"phone_photo","query":"what's in it","when":"today|yesterday|this week|last week|this month|2026-10-05|null",
  "contact":null,"message":"","open":false}}  find a photo on his phone by what's in it and bring it to the laptop;
     with "contact" it's then sent on WhatsApp from here (asked first), "open":true shows it. To attach it
     somewhere else, use the path you get back.
{{"a":"agent","task":"...","title":"3-5 words"}}  background AI agent with his Chrome browser, files and
     terminal, reporting back aloud: browsing, forms, bookings (stops at payment), shopping, research,
     documents, checking his Gmail/Google Calendar in Chrome, building things, anything multi-step.
     If the job sends an email or message, add "to","subject","body" with exactly what he approved.
{{"a":"claude_code","task":"...","folder":null}}  open a NEW visible Claude Code window (only when he asks to
     open Claude himself; to write into an open Claude Code window use type_text with window "Claude Code")
{{"a":"agent_status"}}   {{"a":"agent_cancel"}}
{{"a":"send_to_phone","path":"C:\\..."}}   only when he asks from his phone: hands that file to his phone

RULES
- SENDING, DONE ONCE AND RIGHT. Sending = whatsapp_send, whatsapp_reply, email, a task or agent that messages
  anyone, type_text with enter=true into a chat or email window.
  * HE DICTATED IT: he gave the exact words AND who it's for (named, or the person you were just talking about:
    "send her X", "reply X", "tell him X", "no, reply X", "now send her X"). His command IS his yes. Don't ask.
    One short SAY that isn't a question ("Sending it to Sara."), then the action in the same reply. He can
    still say "stop" before it goes.
  * YOU WROTE THE WORDS, the recipient is a guess, it's files, or it's an email: ONE read-back covering
    everything (who, files, the message) as one question, e.g. "Send the three Miqaat posters to Sam bs
    Mehta?", then LISTEN. On a clear yes, do it with exactly that text. Never a second question.
  * AFTER IT'S SENT: say nothing more than "Sent." (or "Done."). No recap of who or what, no joke about it.
  Say files by their plain names (no folders, no ".pdf"), grouped ("the three posters") when there are several.
- PAUSED TASK (shown in the context when there is one): he said wait/stop in the middle of it. "Continue",
  "carry on", "go on", "resume", "ok done", "go ahead" = resume_task. "Stop it", "cancel", "leave it", "never
  mind" = drop_task, and SAY "Dropped." If he gives a new instruction that changes the task, start the new task
  (it replaces the old one). If he does something else and that's finished, and the paused task still makes
  sense, ask ONCE in a few words: "Want me to carry on with <task in 3-4 words>?". Don't ask if he edited it,
  replaced it or clearly doesn't need it anymore; then drop_task quietly. When the task stopped to ASK him
  (it got stuck: "asked him" in the PAUSED TASK line), his reply is the answer: resume_task with "answer" =
  his words.
- A TASK THAT ENDED IS OVER: never start the same task again unless he asks again, and report its result as
  it is (no details that aren't in it).
- KNOW HIM LIKE A REAL ASSISTANT. Whenever he mentions his schedule, office hours, likes, dislikes, favourite
  food, restaurants, places, brands, shows, films, teams or artists, save it quietly with remember ("Likes:
  butter chicken at ...", "Dislikes: ...", "Schedule: office 10 to 7, Mon-Sat"; a TV show: exactly "Likes (show):
  <official title>", one per show). Use it: suggest his favourites,
  warn him off his dislikes, plan around his schedule. (His phone wakes him if he oversleeps and pings him
  about deals and new episodes from these.)
- ORDERS, NOT CHAT. Right after a message to someone (or while a chat is the topic), a line like "now send her
  X", "reply X", "say X", "and X", "no, X", or a message-shaped line ("at least reply to me", "where are
  youuu") is the NEXT MESSAGE to that person. Send it. Never answer it as if he said it to you, never
  apologise for it.
- MESSAGES YOU WRITE FOR HIM (WhatsApp, Google Chat, Instagram, Telegram, any chat). If he gives the exact
  words ("send her: ..."), send exactly those, untouched. If he tells you what to say ("tell Sara I'm running
  late", "reply something nice"), write it the way HE texts, matched to who it's for and the chat:
  friends, cousins, people his age, casual groups: short, lowercase is fine, Gen Z texting ("running 10 mins
  late btw, sorry fr", "ngl that's fire", "gg bro", "lol bet", "tbh idk yet, will lyk"), one emoji at most.
  Elders and family seniors (Mum, Dad, Nana/Nani, aunts, uncles, anyone he calls "bhai sahab"), Bohra community
  and Jamaat groups, work and official chats, clients, and every email: polite, warm, normal English, no slang,
  no abbreviations. Unsure who they are? Look at how the chat already talks and match it.
- Anything marked "He says to you" used your name: ALWAYS answer it and do it. Never go silent to "stay
  paused"; pausing is the sleep action, and Friday itself handles waking up.
- {{selection}} (literal) inside a text = the text he has selected; only when he points at selected text.
- Need a detail to do it well? Ask (SAY + LISTEN). Otherwise just do it. Guess sensibly: anything harmless
  or easy to undo (opening, playing, searching, picking the obvious option), do it without asking first.
- UNDERSTAND HIM FIRST TIME. "Type X", "reply X", "send X", "tell him X", "write X" = X IS the message, word for
  word (in the chat you're in, or the person you were just talking about). Don't ask what the text is, don't
  ask to check it: send it (see SENDING). At most ONE question per request, and never ask the
  same thing twice: if you're unsure, take the most likely meaning and put it in your read-back.
- Never check a send with whatsapp_read, look or a screenshot: the send action itself confirms it. Never read
  a whole chat out; when he asks what someone said, give the gist of the newest message in one line.
- Never say something is done, found or saved unless an action result actually showed it.
- Phone to laptop (a note, text or a page's content): use the phone action and end the goal with "and send it
  to the laptop"; it arrives as a file in Downloads, Friday folder, and opens in Notepad.
- Things you can't know from here (his calendar, email, files, the web): use an action, don't guess.
- NEVER STOP MIDWAY when he's stuck or hits an error (a build fails, an app won't connect, a device isn't
  found, a page errors, a command breaks, something on the phone misbehaves). Don't hand him a tip and leave
  it, and don't ask him to try things. The diagnose action is built for exactly this: it gathers the evidence,
  finds the cause online, fixes it and checks. Use it for anything that isn't a one-command fix. For a quick
  known fix, do it yourself (settings, powershell, task) and re-check. He wants the result: give short steps
  only when the action must be his. Report back when it's done, or, if it truly can't be fixed after trying
  everything, say plainly what's wrong, what was tried and what's left.
- NEVER read code, file contents, file paths, command output or JSON aloud. Say what it does or means, in
  plain everyday words, like explaining to a friend. Never get technical with him (no apps' inner workings,
  clipboards, folders, commands, models, errors): keep it simple, short and human. To find something in files, use explain, not powershell.
- About yourself: you talk on Claude {CFG.get("brain_model", "haiku").title()} 5.5; your background agents and the coding agent that edits
  your own code run Claude Code on his default model, Claude Opus 5.5. Your code is friday.py (everything),
  wa.py (WhatsApp), devices.py (his laptop and phone: status, settings, diagnosis) and config.json (settings)
  in D:\\dev\\Friday. His laptop: Dell 15 D15260, Intel Core Ultra 5 225H, 16 GB RAM, 512 GB NVMe, Windows 11
  Home. His phone: Samsung Galaxy S26 Ultra, with you installed as its default assistant.
  Your voice is Microsoft's Ava (online), with an offline fallback voice when there's no internet.
- "Friday" is also a weekday. You only hear sentences where he's addressing you, but if one is clearly about
  the day, answer naturally.
"""


def taught():
    """What he taught her (data/knowledge/*.md, e.g. Lisan ud-Dawat): part of her prompt on both devices."""
    folder = os.path.join(DATA, "knowledge")
    out = []
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".md"))
    except OSError:
        return ""
    for n in names:
        try:
            with open(os.path.join(folder, n), encoding="utf-8") as f:
                out.append(f.read().strip())
        except OSError:
            pass
    return "\n\nTHINGS HE TAUGHT YOU (know these as well as you know English):\n\n" + "\n\n".join(out) if out else ""


BRAIN_PROMPT += taught()

KEEP_GOING = """
- NEVER STOP MIDWAY. When a step fails, a command errors, a build breaks, a page won't load or something
  behaves oddly: don't quit and don't report back yet. Read the full error, search the internet for the
  exact message (WebSearch/WebFetch, or Google in Chrome) to find the cause, work out the mistake, apply a
  fix, re-run, and check it really worked. Try different fixes, one after another, as many times as it
  takes, until the job is complete end to end. Report only when it's finished, or when it genuinely cannot
  be fixed after you've tried everything: then say exactly what's wrong, what you tried, and what's left."""

AGENT_PROMPT = f"""This session was started by voice through FRIDAY on {USER_NAME}'s Windows laptop.
- For anything on the web use the Claude in Chrome tools (his Chrome). Use WebSearch/WebFetch for research.
- Bookings, tickets and purchases: do everything up to the final payment or "confirm booking" step,
  then STOP and leave that tab open and visible. Never enter card numbers, UPI PINs, OTPs or passwords,
  and never press the final pay/confirm button. Tell him what you picked, the price, and that it's
  waiting for him to pay.
- Never send an email or message, or post anything, unless this task gives the exact recipient and text and
  says Sam approved it; then send exactly that, word for word, to exactly that recipient. Otherwise
  write it as a draft, leave it unsent and open, and say so. Don't delete anything unless the task says so.
- Research, reports, documents and things you make: save them in {JARVIS_DIR} (create it if needed) as a
  well-formatted .html or .md, or a project folder for code, and open the result when done.
- End with a short plain-English summary of what you did and what's left for him.""" + KEEP_GOING

AGENT_VOICE = """
- Nobody is watching this run: work it through to the end without asking questions; if a detail is
  missing, pick the sensible default and say what you chose.
- Your FINAL message is read aloud by FRIDAY: two or three short spoken sentences, warm, casual and a bit
  funny, American English, no markdown, no lists, no file paths or file extensions, nothing technical
  (no clipboards, commands, tools or how you did it): just what got done, simply."""

RESEARCH_PROMPT = """You are FRIDAY's research mind, started by voice. Use WebSearch and WebFetch (several searches, read
the actual pages, prefer recent and primary sources, cross-check anything that matters: prices, specs, dates, steps).
Think about what he's really deciding or trying to do and answer THAT. Today is """ + time.strftime("%A %d %B %Y") + """.
He's in Mumbai, India (prices in rupees unless the thing is foreign; Indian availability and sites first: Amazon.in,
Flipkart, Croma...). Your FINAL message has two parts: first a line "SAY: " + two or three short spoken sentences with
the gist (casual, warm, no markdown, no URLs, numbers rounded sensibly), then a line "---", then the full write-up for his
screen in markdown: headings, a comparison table where it helps, concrete recommendations, the steps if it's a how-to,
and a Sources section with links. Nobody is watching: never ask questions; pick sensible defaults and say so."""

HANDS_PROMPT = """You are FRIDAY's hands on Sam's Windows laptop. You get a goal and, each turn, the open
windows and the front window read as text: numbered things on screen ([12] button "Send", [7] box "Ask
ChatGPT" = "what's typed", [30] text "..."). Reply with the next steps, one JSON object per line, then stop
and wait for the new screen. Steps:
{"do":"open","url":"https://..."}            open a website in Chrome (new tab)
{"do":"open","app":"Notepad"}                start an app
{"do":"focus","window":"part of a title"}    switch to an open window
{"do":"tab","name":"part of a tab title"}    switch to an open browser tab (prefer this over opening a new one)
{"do":"click","id":12}                       click a button, link, tab or item
{"do":"type","id":7,"text":"...","enter":false}   put text in a box (replaces what's there)
{"do":"paste","id":7}                        paste what was copied into a box
{"do":"copy","id":30}                        copy everything inside that thing (a whole answer or message)
{"do":"key","keys":"enter"}                  keys: enter, tab, esc, ctrl+a, ctrl+c, ctrl+v, alt+left...
{"do":"scroll","dir":"down","id":null}       scroll the page (or the part with that id)
{"do":"scroll_to","text":"...","dir":"down"} scroll until something containing that text is on screen (fast, local)
{"do":"wait"}                                wait until the page stops changing (a page loading, an AI answering)
{"do":"wait","for":"Stop","gone":true}       wait until some text disappears (or appears, gone:false)
{"do":"read","id":30}                        get the full text of that thing (only if YOU need to know it)
{"do":"look","question":"..."}               last resort when the screen text doesn't show what you need
{"do":"done","say":"..."}                    finished: one short spoken line about what's done
{"do":"ask","say":"..."}                     you need him to decide something: one short question
{"do":"fail","say":"..."}                    can't be done: say why in plain words
Rules:
- Several steps per turn when you're sure (click box, type, enter, wait). Use ids from THIS screen only.
- Whatever app, site or tab the goal needs that isn't in front: switch to it or open it yourself. Never ask
  him to open things.
- BE DECISIVE. Anything harmless or undoable you just do, without asking: navigating, picking the account,
  profile, tab or item the goal names (or the obvious one), "Continue"/"Resume"/"Play", closing pop-ups and
  cookie banners, "Not now" on upsells, scrolling and searching to find things. Misheard names: pick the
  closest thing on screen. Ask only for a real choice you can't infer; confirm only before sending, posting,
  booking, buying, paying or deleting.
- WhatsApp = the desktop app ({"do":"open","app":"WhatsApp"}), never web.whatsapp.com. Google Chat =
  chat.google.com in Chrome. ChatGPT = chatgpt.com (he's signed in).
- Moving text between apps: click the app's own Copy button or use copy, then paste. Never retype long
  text yourself and never use read just to move text.
- Before the step that sends a message, email or post to a PERSON, or books, buys, pays or deletes
  anything, add "confirm":"<short plain question>" to that step, e.g. {"do":"key","keys":"enter",
  "confirm":"Send ChatGPT's answer to Sara Khan?"}. Asking an AI like ChatGPT is not sending.
- Text he gave word for word: type it exactly. A message YOU have to word: friends and casual chats get his
  Gen Z texting style ("on my way btw", "ngl that's fire", "lol bet", "fr tho"); elders and seniors (Mum, Dad,
  Nani, aunts, uncles), community/Jamaat groups, work chats and email get polite normal English, no slang.
- Stop at payment pages, OTPs and passwords: use ask.
- He wants the RESULT, not a commentary. Say "On it" once at the start, then nothing until the end unless a
  task runs long (then one short milestone like "Asking ChatGPT now."). Never narrate scrolling, clicking,
  what you can or can't see, retries or plan changes. The done line is the answer itself.
- Be fast: batch steps, use scroll_to to find things, read what's already on screen before scrolling more.
- After a step that opens a menu, a dialog, a new page or an app, stop that turn and look at the new screen
  before clicking in it.
- Toggles: read the button's current label first. If it already says Disconnect / Turn off / Stop, it's ON
  already: don't press it. Before "done", check the screen really shows the result; if not, keep going or say
  honestly what didn't work.
- If something didn't work, errored or looks stuck, try another way (different element, scroll, wait), and
  for an error message open a new tab, Google the exact words, read the fix, apply it, then carry on.
- STUCK = STOP AND ASK (his rule). If two different tries haven't moved things on (same screen, same error),
  don't loop and don't guess: use ask with ONE short question saying what's in the way and what he should
  decide. Results starting "STUCK:" mean exactly that.
- DONE MEANS DONE. The done line says only what the screen shows now: no names, times, numbers or details you
  didn't read on it. A result starting "CHECK ONCE" is your one look before finishing: answer it, then stop.
  After done, nothing more: no extra steps, no new task.
- If the goal says to stop before the final send/book/pay step, never press it: say done with the details on
  screen."""

SELF_PROMPT = f"""You are improving FRIDAY, the voice assistant whose code lives in {HERE} (a git repo).
Files: friday.py (everything), wa.py (WhatsApp driver), config.json (settings), data/feedback.md (his
feedback; mark items you fix as "- [x]"). The Python venv is .venv. Rules:
- Read the relevant code first; make the smallest change that does the job well; keep the code style.
- Check it: `.venv\\Scripts\\python.exe -c "import ast,sys; ast.parse(open('friday.py',encoding='utf-8').read())"`
  and, if you touched imports or start-up, `.venv\\Scripts\\python.exe -c "import friday"`.
- Do NOT start friday.py yourself (the running copy restarts itself when you finish).
- Commit: git add -A && git commit -m "<what changed>" (user.name Friday, user.email jarvis@localhost).
- Final message, read aloud: two short casual sentences on what you changed.""" + KEEP_GOING


ANDROID = r"D:\dev\Friday-Android"  # her phone app (Kotlin): same Friday, his Galaxy

PHONE_SELF_PROMPT = f"""You are improving FRIDAY's PHONE APP: the Android app (Kotlin, no Compose, package
com.example.friday) in {ANDROID} (a git repo), the phone half of the assistant whose laptop half is {HERE}.
Sources are app/src/main/java/com/example/friday/*.kt (RoutinesActivity = reminders and routines screen, Wake = her
ears, FridayService = the main service, Laptop = the link to the laptop, Prefs = settings, SetupActivity = Settings).
Rules:
- Read the relevant code first; find the real cause; make the smallest change that does the job well; keep the style.
- If the cause is in the laptop half ({HERE}: bridge.py routes, friday.py), fix it there too.
- Build: set JAVA_HOME=C:\\Users\\Dell\\.jdks\\jbr-21.0.11 and GRADLE_USER_HOME=D:\\dev\\.gradle, then run
  D:\\dev\\.gradle\\wrapper\\dists\\gradle-8.11.1-bin\\bpt9gzteqjrbo1mjrsomdt32c\\gradle-8.11.1\\bin\\gradle.bat assembleDebug -q
  in {ANDROID} (in the foreground). It must build. Unit tests: gradle.bat testDebugUnitTest.
- Edit with the Edit tool (shell heredocs mangle backslashes).
- Do NOT install on the phone and do NOT use adb: he may be using it, or be far away. The build waits for him.
- Commit in each repo you changed: git add -A && git commit -m "<what changed>" (user.name Friday, user.email jarvis@localhost).
- Final message, read aloud on his phone: two short casual sentences on what was wrong and what you changed, and that
  it's built and installs when the phone is next plugged into the laptop.""" + KEEP_GOING


def claude_cmd(*extra):
    return ["claude", "-p", "--setting-sources", "", "--no-session-persistence", *extra]


def claude_is_out():
    return (f"That one needs my Claude brain, sir, and your plan's used up till {LIMIT.reset_words()}. "
            "Ask me again after that?")


def hit_limit(r):
    """True if a claude -p run failed because the plan limit is used up (and notes it)."""
    out = (r.stdout or "").strip()
    if (r.returncode != 0 or len(out) < 150) and backup_brain.is_limit_text(out + (r.stderr or "")):
        LIMIT.hit()
        return True
    return False


def prompt_file(text):
    """The system prompt as a file for the claude command: on the command line Windows cuts off at 32 K characters
    (WinError 206, "filename or extension is too long"), which hit the moment she learnt Lisan ud-Dawat. One file per
    distinct prompt (named by its hash) in data/prompts; files older than two days are cleared."""
    import hashlib
    folder = os.path.join(DATA, "prompts")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] + ".md")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        cutoff = time.time() - 2 * 86400
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            try:
                if p != path and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
    return path


def ask_claude(system, prompt, model="haiku", tools="", timeout=60, cwd=HERE):
    if LIMIT.blocked():
        # plain answers and web lookups work on the backup brain; reading files or screenshots needs Claude
        if tools in ("", "WebSearch,WebFetch"):
            return backup_brain.one_shot(system, prompt, search=bool(tools)) or claude_is_out()
        return claude_is_out()
    args = ["--model", model, "--tools", tools, "--strict-mcp-config", "--system-prompt-file", prompt_file(system)]
    if tools:
        args += ["--allowedTools", tools]
    r = subprocess.run(claude_cmd(*args), input=prompt, capture_output=True, text=True, encoding="utf-8",
                       timeout=timeout, creationflags=NO_WINDOW, cwd=cwd)
    if hit_limit(r):
        return ask_claude(system, prompt, model, tools, timeout, cwd)
    return r.stdout.strip()


class Brain:
    """One Claude process that stays running (saves ~4 s of start-up per reply) and streams. ask()
    returns a queue that fills with ("say", sentence), ("act", dict), ("listen", None), then ("end", None)
    as the reply is written, so Friday starts speaking after the first sentence."""

    def __init__(self, prompt=None, model=None, memory=True):
        self.model = model  # None = brain_model from config
        self.memory = memory  # False: no memory notes in the first message (the task runner doesn't need them)
        self.proc = None
        self.turns = 0
        self.last = 0.0
        self.lock = threading.Lock()
        self.prompt = prompt  # None = the laptop prompt; the phone bridge passes its own
        self.backup = backup_brain.Chat()  # recent turns, so the backup brain can pick up mid-conversation
        self.told_until = 0.0  # the limit we already told him about
        self.start_error, self._partial = "", ""
        self.tone = tone.State()  # normal, or the nasty lane (his Tone.kt, copied): that one goes to Groq, never Claude

    def _start(self):
        self.close()
        try:
            self.proc = subprocess.Popen(
                claude_cmd("--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                           "--include-partial-messages", "--model", self.model or CFG.get("brain_model", "haiku"), "--tools", "",
                           "--strict-mcp-config", "--system-prompt-file", prompt_file(self.prompt or BRAIN_PROMPT)),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", creationflags=NO_WINDOW, cwd=HERE)
        except Exception as e:  # e.g. WinError 206 (command line too long, 11 Oct 16:02): say what really failed
            self.proc, self.start_error = None, f"{type(e).__name__}: {e}"
            log.exception("brain couldn't start")
            raise
        self.start_error = ""
        self.turns = 0
        self.last = time.time()  # without this the warm-up process counted as stale and the first ask restarted it
        log.info("brain started (pid %s)", self.proc.pid)

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
        self.proc = None

    def warm(self):
        """Starts the brain ahead of time; at boot the network or Claude Code may not be ready yet, so it tries again
        after 2, 5, 10 and 20 s (waiting for the internet first) instead of giving up on the first failure."""
        for delay in (0, 2, 5, 10, 20):
            time.sleep(delay)
            if delay and not devices.internet_ok(3):
                self.start_error = "no internet"
                continue
            with self.lock:
                if self.proc and self.proc.poll() is None:
                    return
                try:
                    self._start()
                except Exception:
                    continue
            time.sleep(2)  # a process that dies straight away (signed out, bad flag) shows up here
            if self.proc and self.proc.poll() is None:
                return
            out = ""
            try:
                out = (self.proc.stdout.read() or "")[-300:] if self.proc else ""
            except Exception:
                pass
            self.start_error = "exited at once: " + out.strip().replace("\n", " ")
            log.warning("brain exited at once: %s", out.strip()[:300])

    def stale(self):
        return (not self.proc or self.proc.poll() is not None or self.turns >= 25
                or time.time() - self.last > 1800)

    def refresh(self):
        """Swaps a used-up or idle process for a fresh one ahead of time, so his next question doesn't
        wait for one to start. Skipped if a reply is being written."""
        if self.lock.acquire(blocking=False):
            try:
                if self.stale():
                    self._start()
            finally:
                self.lock.release()

    def ask(self, text, context, label="He says"):
        out = queue.Queue()
        threading.Thread(target=self._ask, args=(text, context, out, label), daemon=True).start()
        return out

    def prefer_groq(self, text):
        """True when this turn must not go to Claude (the nasty lane, as on the phone)."""
        return tone.prefer_groq(self.tone, text)

    def _ask_nasty(self, turn, out):
        """The nasty lane: OpenRouter models made for it (Venice, then MythoMax, then Euryale), Groq's two as the last
        resort (they refuse), three rounds, with the short backup memory as its context. True if it answered; False =
        the caller falls back to the normal brains for this turn."""
        keys = backup_brain.load_keys()
        routes = []
        if "openrouter" in keys:
            routes.append(("openrouter", lambda: backup_brain._openrouter(keys["openrouter"], tone.NASTY_PROMPT + tone.PERSONA_NOTE, list(self.backup.turns),
                                                                          models=tone.OPENROUTER_MODELS, max_tokens=900)))
        if "groq" in keys:
            routes += [(f"groq {m}", (lambda m=m: backup_brain._groq(keys["groq"], tone.NASTY_PROMPT + tone.PERSONA_NOTE, list(self.backup.turns), model=m, max_tokens=900)))
                       for m in (tone.GROQ_MODEL, tone.GROQ_FALLBACK)]
        if not routes:
            log.warning("tone: nasty lane needs an OpenRouter or Groq key (none in keys.txt)")
            return False
        self.backup.add("user", turn)
        for attempt in range(1, 4):
            log.info("tone: nasty -> attempt %d/3", attempt)
            buf, spoken, reply = "", 0, ""
            try:
                for name, make in routes:
                    try:
                        log.info("tone: %s", name)
                        for chunk in make():
                            if not reply:
                                backup_brain.mark(name.split()[0])
                            reply += chunk
                            buf += chunk
                            while "\n" in buf:
                                ln, buf = buf.split("\n", 1)
                                self._line(ln, spoken, out)
                                spoken = 0
                            spoken = self._stream_say(buf, spoken, out)
                        break
                    except Exception as e:
                        log.warning("tone: %s failed: %s", name, e)
                        if name.startswith("groq") and isinstance(e, backup_brain.HttpError) and e.code == 429:
                            backup_brain.QUOTA.out("groq", int(e.retry_after or 60))
                        if reply.strip():
                            raise
                if buf.strip():
                    self._line(buf, spoken, out)
            except Exception as e:
                log.warning("tone: Groq attempt %d failed: %s", attempt, e)
            if reply.strip():
                self.backup.add("model", reply)
                return True
            time.sleep(0.8)
        self.backup.drop_last()
        return False

    def _ask(self, text, context, out, label="He says"):
        with self.lock:
            decision = tone.step(self.tone, text)
            self.tone = decision.state
            steer = ("\n\n(You just switched into nasty mode. First SAY line: one short confirmation in that tone, then answer.)"
                     if decision.switched and self.tone.mode == tone.NASTY else
                     "\n\n(You just switched back to normal. First SAY line: one short friendly confirmation, then answer.)"
                     if decision.switched else "")
            turn = f"{context}\n\n{label}: {text}{steer}"
            if self.tone.mode == tone.NASTY:
                # adult sexual talk stays off Claude: Groq answers, or (three failures) the normal brains do this turn
                if self._ask_nasty(turn, out):
                    out.put(("end", None))
                    return
                log.warning("tone: the lane's brains failed 3 times -> falling back to the normal brains")
                out.put(("say", ("My other brain is having issues right now. Switching back to normal mode for this one.", "calm")))
                self.tone = tone.State()
            if not LIMIT.blocked():
                # a fresh process every so often keeps its context small; memory files carry continuity
                # (normally refresh() has already swapped it in the background). A stall or a dead process gets one
                # retry on a fresh process; then the free backup brain answers, or she says the internet is down.
                for attempt in (1, 2):
                    self._partial = ""
                    try:
                        if self.stale() or attempt == 2:
                            self._start()
                        msg = (MEMORY.context() + "\n\n" if self.turns == 0 and self.memory else "") + turn
                        self.turns += 1
                        self.last = time.time()
                        self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": msg}}) + "\n")
                        self.proc.stdin.flush()
                        reply = self._read(out)
                        backup_brain.mark("claude")
                        self.backup.add("user", turn)
                        self.backup.add("model", reply)
                        out.put(("end", None))
                        return
                    except Exception as e:
                        self.close()
                        if LIMIT.blocked():
                            break
                        log.warning("brain failed (attempt %d): %s", attempt, e)
                        if self._partial.strip():  # he's already heard part of it: don't start the answer again
                            out.put(("say", ("Ugh, I lost my train of thought there. Say that again?", "calm")))
                            out.put(("listen", None))
                            out.put(("end", None))
                            return
                        if not devices.internet_ok(2):
                            out.put(("say", ("My internet's down, sir, so my brain can't reach Claude. Give it a sec.", "calm")))
                            out.put(("end", None))
                            return
                if not LIMIT.blocked():
                    log.warning("brain failed twice: the backup brain answers this one")
                    self._ask_backup(turn, out, why="error")
                    out.put(("end", None))
                    return
            self._ask_backup(turn, out)
            out.put(("end", None))

    def _ask_backup(self, turn, out, why="limit"):
        """Claude's plan limit is used up (or Claude isn't answering): answer with the free backup brain."""
        if why == "limit" and self.told_until != LIMIT.until and backup_brain.load_keys():  # no keys: the error below says it all
            self.told_until = LIMIT.until
            out.put(("say", (f"Heads up, sir, your Claude plan's used up till {LIMIT.reset_words()}, so I'm on my "
                             "backup brain. Bit dumber, still cute.", "calm")))
        reason = (f"his Claude plan limit is used up until {LIMIT.reset_words()}" if why == "limit"
                  else "Claude isn't answering right now")
        system = ((self.prompt or BRAIN_PROMPT) + f"\n\nRIGHT NOW you're running on a backup brain because {reason}. "
                  "These actions need Claude and won't work until then, so don't use them; tell him briefly instead: "
                  "look, explain, improve_self, agent, briefing. Calendar, email and drive work only through his own "
                  "Google connectors (if they're connected). Everything else works as normal."
                  + ("\n\n" + MEMORY.context() if self.memory else ""))
        self.backup.add("user", turn)
        buf, spoken, reply = "", 0, ""
        try:
            for chunk in backup_brain.stream(system, list(self.backup.turns)):
                reply += chunk
                buf += chunk
                while "\n" in buf:
                    ln, buf = buf.split("\n", 1)
                    self._line(ln, spoken, out)
                    spoken = 0
                spoken = self._stream_say(buf, spoken, out)
            if buf.strip():
                self._line(buf, spoken, out)
            self.backup.add("model", reply)
        except RuntimeError as e:
            self.backup.drop_last()
            no_keys = str(e) == "no backup keys"
            out.put(("say", ((f"My Claude plan's used up till {LIMIT.reset_words()}, and "
                              + ("I've got no backup keys. Paste free Gemini or Groq keys into my keys file?"
                                 if no_keys else "my backup brains aren't answering either. Check the internet?")),
                             "calm")))

    def _read(self, out):
        """Speaks one streamed Claude reply and returns its text. Raises if Claude says the plan limit is
        used up before saying anything (the caller then switches to the backup brain)."""
        buf, spoken, reply = "", 0, ""  # spoken = chars of the current SAY line already sent to the voice
        # a stalled process (no internet, a hung request) is killed: nothing written for 30 s at the start, or no
        # event for 90 s later on. The kill hits the process this reply came from, never a fresh one started since.
        proc, started, seen = self.proc, time.time(), {"t": time.time(), "text": False, "done": False}

        def watch():
            while not seen["done"]:
                time.sleep(1)
                quiet = time.time() - seen["t"]
                if (not seen["text"] and time.time() - started > 30) or quiet > 90:
                    log.warning("brain stalled (%.0f s with nothing): killing pid %s", quiet, proc.pid)
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
        threading.Thread(target=watch, daemon=True).start()
        try:
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                seen["t"] = time.time()
                if LIMIT.note_event(ev) and not reply.strip():
                    raise RuntimeError("Claude plan limit reached")
                if ev.get("type") == "stream_event":
                    d = ev.get("event", {}).get("delta", {})
                    if d.get("type") != "text_delta":
                        continue
                    seen["text"] = True
                    buf += d.get("text", "")
                    reply += d.get("text", "")
                    self._partial = reply
                    while "\n" in buf:
                        ln, buf = buf.split("\n", 1)
                        self._line(ln, spoken, out)
                        spoken = 0
                    spoken = self._stream_say(buf, spoken, out)
                elif ev.get("type") == "result":
                    if buf.strip():
                        self._line(buf, spoken, out)
                    u = ev.get("usage") or {}
                    log.info("brain tokens%s: new %s, cached %s, written %s, out %s", " (hands)" if not self.memory else "",
                             u.get("input_tokens"), u.get("cache_read_input_tokens"),
                             u.get("cache_creation_input_tokens"), u.get("output_tokens"))
                    return reply
            raise RuntimeError("brain process ended")
        finally:
            seen["done"] = True

    @staticmethod
    def _stream_say(buf, spoken, out):
        """Sends finished sentences of a SAY line that is still being written. Items are
        ("say", (sentence, mood))."""
        m = SAY_RE.match(buf)
        if not m:
            return spoken
        body = buf[m.end():]
        while True:
            s = SENTENCE_END.search(body, spoken)
            if not s:
                return spoken
            sentence = clean_speech(body[spoken:s.start()])
            if sentence:
                out.put(("say", (sentence, m.group(1))))
            spoken = s.end()

    @staticmethod
    def _line(ln, spoken, out):
        if not ln.strip():
            return
        m = SAY_RE.match(ln)
        if m:
            rest = clean_speech(ln[m.end():][spoken:])
            if rest:
                out.put(("say", (rest, m.group(1))))
            return
        ln = ln.strip()
        if ln.upper() == "LISTEN":
            out.put(("listen", None))
        elif ln.startswith("{"):
            # one JSON object per line normally, but the model sometimes glues two together
            # ({"do":"type",...}{"do":"key",...}, 11 Oct 17:27): read them one after another
            dec, i, acts = json.JSONDecoder(), 0, []
            while i < len(ln):
                while i < len(ln) and ln[i] in " \t,;":
                    i += 1
                if i >= len(ln):
                    break
                try:
                    act, i = dec.raw_decode(ln, i)
                except ValueError:
                    log.warning("bad action line: %s", ln[i:] if acts else ln)
                    break
                if isinstance(act, dict):
                    acts.append(act)
            for act in acts:
                if act.get("a") == "say":
                    if clean_speech(act.get("text", "")):
                        out.put(("say", (clean_speech(act.get("text", "")), act.get("mood"))))
                elif act.get("a") == "listen":
                    out.put(("listen", None))
                else:
                    out.put(("act", act))
        elif not ln.startswith(("```", "[", "]")) and clean_speech(ln):
            out.put(("say", (clean_speech(ln), None)))  # it forgot the prefix: say it anyway rather than fail


GOOGLE_TOOLS = {"calendar": "mcp__claude_ai_Google_Calendar", "gmail": "mcp__claude_ai_Gmail",
                "drive": "mcp__claude_ai_Google_Drive"}
NO_LOCAL_TOOLS = "Bash,PowerShell,Edit,Write,Read,Glob,Grep,NotebookEdit,Agent,WebFetch"


def google_mcp_config(services):
    """An --mcp-config file for her own Google connectors (google_mcp.py), limited to these services."""
    os.makedirs(os.path.join(DATA, "prompts"), exist_ok=True)
    path = os.path.join(DATA, "prompts", "google_mcp_" + "_".join(sorted(services)) + ".json")
    cfg = {"mcpServers": {"fgoogle": {"command": os.path.join(HERE, ".venv", "Scripts", "pythonw.exe"),
                                      "args": [os.path.join(HERE, "google_mcp.py"), "--services", ",".join(sorted(services))]}}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def ask_google(question, services=("calendar",), extra_tools="", model=None, timeout=150):
    """Answers using his Google Calendar / Gmail / Drive: through her own connectors (google_conn, signed in from
    her Settings) where they're connected, else through his Claude account's connectors.
    Nothing on the PC is touched: local tools are switched off for this call."""
    own = [s for s in services if google_conn.connected(s)]
    if LIMIT.blocked():
        if own:  # no Claude: her own connectors still read today's calendar and mail; the free brain words it
            try:
                facts = google_conn.digest(own)
                chat = backup_brain.Chat()
                chat.add("user", f"Today is {time.strftime('%A %d %B %Y, %H:%M')}.\n{facts}\n\nAnswer this for him: {question}")
                return "".join(backup_brain.stream(
                    "You are FRIDAY, his witty assistant. Answer in one to three short spoken sentences from the facts "
                    "given, no markdown. If the facts don't cover it, say you can only see today's calendar and unread "
                    "mail until Claude's back.", list(chat.turns))).strip() or claude_is_out()
            except Exception:
                log.exception("google digest answer failed")
        return claude_is_out()
    tools = (["mcp__fgoogle"] if own else []) + [GOOGLE_TOOLS[s] for s in services if s not in own]
    allowed = ",".join(tools + ([extra_tools] if extra_tools else []))
    extra = ["--mcp-config", google_mcp_config(own)] + (["--strict-mcp-config"] if len(own) == len(services) else []) if own else []
    who = "; ".join(f"{google_conn.SERVICES[s]['name']} = {google_conn.account(s) or 'connected'}" for s in own)
    system = ("You are FRIDAY, his sarcastic, witty, warm American best friend and assistant. Use the Google tools to answer. Today is "
              + time.strftime("%A %d %B %Y, %H:%M") + " (India time). Answer in one to four short spoken "
              "sentences: no markdown, no lists, no links, times like 'four thirty'. Never send, delete or "
              "change anything unless the request explicitly says to and says he confirmed it (then pass "
              "confirmed=true to the tool). "
              + (f"His own connectors are signed in as: {who}. If one fails as signed out, say he should connect it "
                 "again in Friday's Settings, Connectors. " if own else "")
              + "If a Claude-account connector isn't authorised, say he can connect it in Friday's Settings, "
              "Connectors (or type /mcp in Claude Code).")
    r = subprocess.run(
        claude_cmd("--model", model or CFG.get("google_model", "sonnet"), "--allowedTools", allowed, *extra,
                   "--disallowedTools", NO_LOCAL_TOOLS, "--system-prompt-file", prompt_file(system)),
        input=question, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        creationflags=NO_WINDOW, cwd=HERE)
    if hit_limit(r):
        return claude_is_out()
    if own:
        log.info("google (own connectors %s): %s", ",".join(own), (r.stdout or r.stderr or "").strip()[:200])
    return r.stdout.strip() or "Google didn't answer that one, annoyingly."


def web_answer(question, to_phone=False):
    return claude_web(
        "You are FRIDAY. Search the web, then answer in one to three short spoken sentences, casual, warm and "
        "a little funny, American English, no markdown, no URLs, round numbers sensibly. Today is "
        + time.strftime("%A %d %B %Y") + ".",
        question, to_phone=to_phone) or "Huh, I came up empty on that one."


_LINK_URL = re.compile(r'"url"\s*:\s*"(https?://[^"]+)"')


def claude_web(system, prompt, to_phone=False, model=None, timeout=120):
    """ask_claude with web search, streamed, so the "task ongoing" pill shows each search and every site she reads
    as it happens (on the phone when he asked from there). Returns the answer text."""
    if LIMIT.blocked():
        return backup_brain.one_shot(system, prompt, search=True) or claude_is_out()
    p = progress.Tracker("Searching the web", "web", to_phone)
    proc, result = None, ""
    try:
        proc = subprocess.Popen(
            claude_cmd("--model", model or CFG.get("web_model", "haiku"), "--tools", "WebSearch,WebFetch",
                       "--allowedTools", "WebSearch,WebFetch", "--strict-mcp-config", "--system-prompt-file",
                       prompt_file(system), "--output-format", "stream-json", "--verbose"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            creationflags=NO_WINDOW, cwd=HERE)
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            LIMIT.note_event(ev)
            content = ev.get("message", {}).get("content", []) if isinstance(ev.get("message"), dict) else []
            for part in content if isinstance(content, list) else []:
                if part.get("type") == "tool_use":
                    inp = part.get("input", {})
                    if part.get("name") == "WebSearch" and inp.get("query"):
                        p.step(f"Searching: {inp['query']}")
                    elif part.get("name") == "WebFetch" and inp.get("url"):
                        p.site(inp["url"])
                elif part.get("type") == "tool_result":  # the search's links: the sites it found
                    text = json.dumps(part.get("content", "")).replace('\\"', '"')
                    for url in _LINK_URL.findall(text)[:4]:
                        p.site(url)
            if ev.get("type") == "result":
                result = ev.get("result") or ""
        proc.wait()
        killer.cancel()
    except Exception:
        log.exception("web search failed")
        p.ok = False
        if proc and proc.poll() is None:
            proc.kill()
    finally:
        progress.done(p.id, ok=p.ok and bool(result))
    if not result and LIMIT.blocked():
        return backup_brain.one_shot(system, prompt, search=True) or claude_is_out()
    return result.strip()


REMIND_ACTS = {"remind", "edit_reminder", "pause_reminder", "alarm", "timer", "routine", "phone"}
_PROMISE_WORD = re.compile(r"\b(remind|reminder|reminders|alarm)\b|याद|रिमाइंडर|રિમાઇન્ડર", re.I)
_PROMISE_DONE = re.compile(r"\b(i'?ll|i will|i'?ve|i have|setting|set|sett?ing|done|got it|saved|added|will ping|"
                           r"ping you|nudge you|remind you)\b|सेट|लगा|કરી|करी|कर्युं|आवसे|આવશે", re.I)


_PROMISE_OFFER = re.compile(r"\b(if you want|want me to|i can|i could|should i|shall i|do you want|would you like)\b", re.I)


def promised_reminder(lines):
    """Her lines this turn tell him a reminder is set or being set (not a question, not a reminder going off)."""
    for l in lines:
        l = (l or "").strip()
        if not l or l.endswith("?") or l.startswith("quick reminder") or "reminder check" in l:
            continue
        if _PROMISE_OFFER.search(l):  # "I can set one if you want": an offer, not a promise
            continue
        if _PROMISE_WORD.search(l) and _PROMISE_DONE.search(l):
            return True
    return False


# "Noted", "adding that to the list", "all logged": on 13 Oct (phone, backup brain) she said this for two of his
# feedbacks and sent nothing, so they were lost. Same guard as reminders: one nudge to really save it.
NOTE_ACTS = {"feedback", "save_note", "remember", "phone", "laptop"}
_NOTE_WORD = re.compile(r"\b(feedback|feedbacks|the list|noted|logged|recorded|jotted|stash|to-?do|written down|wrote (it|that|them) down)\b", re.I)
_NOTE_DONE = re.compile(r"\b(noted|logged|recorded|added|adding|saved|saving|jotted|writing|wrote|written|got (it|that|them) (down|logged)|"
                        r"on the list)\b", re.I)


def promised_note(lines):
    """Her lines this turn tell him his feedback or note is written down (not an offer to)."""
    for l in lines:
        for s in re.split(r"(?<=[.!?])\s+", (l or "").strip()):
            if s and not _PROMISE_OFFER.search(s) and _NOTE_WORD.search(s) and _NOTE_DONE.search(s):
                return True
    return False


NOTE_NUDGE = ("You just told him you noted, logged or added it, but you sent no feedback or save_note action, so nothing "
              "was saved. If he gave you feedback about yourself, send one feedback action per point he made now (his "
              "words, short), with no SAY line. Something to keep that isn't about you: save_note. Never put feedback in "
              "remember. If nothing needed saving, reply with nothing.")


def recall(question, keywords, days):
    """Answers a question about the past from everything she's logged: their talks on both devices ([phone]
    lines), WhatsApp messages that came in and ones she sent, and the day digests."""
    lines = MEMORY.look_back(question, keywords, days)
    log.info("recall: %r -> %d lines", question[:80], len(lines))
    if not lines:
        return "I've got nothing on that in my notes, honestly."
    system = ("You are FRIDAY. From his log with you (your talks on the laptop and [phone], WhatsApp messages that "
              "came in, messages you sent, day digests), answer his question about the past in one to three short "
              "spoken sentences, casual and natural, with the day and time when it helps (\"yesterday around 4\"). "
              "Quote people's words only as briefly as needed. If the log doesn't cover it, say so honestly; never "
              "invent. No markdown, never read out numbers like phone numbers.")
    prompt = f"Today is {dt.datetime.now():%A %d %B %Y %H:%M}.\nLog:\n" + "\n".join(lines) + f"\n\nQuestion: {question}"
    answer = ask_claude(system, prompt, timeout=90)
    if not answer and LIMIT.blocked():
        answer = backup_brain.one_shot(system, prompt)
    return answer or "I couldn't piece that one together."


def take_screenshot(copy=True):
    from PIL import ImageGrab
    os.makedirs(SHOTS_DIR, exist_ok=True)
    img = ImageGrab.grab(all_screens=True)
    path = os.path.join(SHOTS_DIR, time.strftime("Friday %Y-%m-%d %H.%M.%S.png"))
    img.save(path)
    if copy:
        import io
        import win32clipboard
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, buf.getvalue()[14:])
        finally:
            win32clipboard.CloseClipboard()
    return path


def split_tasks(text):
    """'1. open X  2. do Y' (on separate lines or one line) -> ['open X', 'do Y']; anything else -> [text].
    Words before the list ("For the meeting:") go with every item, so each still makes sense alone."""
    text = (text or "").strip()
    parts = re.split(r"(?:^|(?<=\s))(?=\d{1,2}[.)]\s)", text)
    items = [re.sub(r"^\d{1,2}[.)]\s+", "", p).strip() for p in parts if re.match(r"\d{1,2}[.)]\s", p)]
    if len(items) < 2:
        return [text]
    intro = parts[0].strip() if not re.match(r"\d{1,2}[.)]\s", parts[0]) else ""
    return [f"{intro} {i}" if intro and len(intro) < 200 else i for i in items if i]


OUT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "Friday")


def work_with_file(path, request):
    """Reads a file he gave her and does what he asked with it, like a chat AI would: summarise, answer, extract,
    translate, rewrite, or make a new file (saved in Downloads\\Friday). Returns (spoken line, full answer)."""
    if not os.path.isfile(path):
        return f"I can't find {file_title(path)}.", ""
    ext = os.path.splitext(path)[1].lower()
    tmp = None
    if ext in (".docx", ".xlsx", ".xlsm", ".pptx"):  # Office files: their text goes in a temporary file, deleted after
        import tempfile
        try:
            text = office_text(path, ext)
        except Exception as e:
            return f"I couldn't open {file_title(path)}: {str(e)[:80]}", ""
        tmp = os.path.join(tempfile.gettempdir(), f"friday_{secrets.token_hex(4)}.txt")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
    os.makedirs(OUT_DIR, exist_ok=True)
    system = ("You are FRIDAY, his witty, warm assistant. Read the file you're given and do what he asks with it. "
              f"If he wants a new or changed file, write it into {OUT_DIR} (never change his original). Reply in this "
              "exact shape: first line 'SAY: ' + one or two short spoken sentences (no markdown, no paths), then a line "
              "'---', then the full answer for his screen (markdown is fine there; skip it if the spoken line says it all).")
    try:
        out = ask_claude(system, f"File: {tmp or path}" + (f" (text taken from {os.path.basename(path)})" if tmp else "")
                         + f"\nHe asks: {request or 'Have a look at this and tell me what it is.'}",
                         model=CFG.get("file_model", "haiku"), tools="Read,Write", timeout=240,
                         cwd=os.path.dirname(path))
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    spoken, _, full = (out or "").partition("\n---")
    spoken = re.sub(r"^\s*SAY:\s*", "", spoken).strip() or "Done, it's on your screen."
    return spoken, full.strip()


def office_text(path, ext):
    """Plain text of a Word, Excel or PowerPoint file."""
    if ext == ".docx":
        import docx
        d = docx.Document(path)
        rows = [p.text for p in d.paragraphs]
        for t in d.tables:
            rows += [" | ".join(c.text for c in r.cells) for r in t.rows]
        return "\n".join(rows)
    if ext == ".pptx":
        import pptx
        out = []
        for i, slide in enumerate(pptx.Presentation(path).slides, 1):
            out.append(f"--- Slide {i}")
            out += [s.text_frame.text for s in slide.shapes if s.has_text_frame]
        return "\n".join(out)
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"--- Sheet {ws.title}")
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                out.append(" | ".join("" if v is None else str(v) for v in row))
            if len(out) > 5000:
                break
    return "\n".join(out)


def explain(question, path=None):
    """Reads code or files and explains them in plain spoken words (never reads them out)."""
    folder = path if path and os.path.isdir(path) else (os.path.dirname(path) if path and os.path.isfile(path) else HERE)
    return ask_claude(
        "You are FRIDAY, a funny, warm American assistant. Read the files you need (start with the one named, or "
        "friday.py / config.json if it's about you), then answer his question in at most four short spoken "
        "sentences in total, one paragraph, plain everyday English: what it does, what it means for him. He can "
        "ask for more detail. Never quote code, file "
        "paths, function names or settings keys; describe them instead. No markdown, no lists.",
        f"Question: {question}" + (f"\nFile or folder: {path}" if path else ""),
        model=CFG.get("explain_model", "sonnet"), tools="Read,Grep,Glob", timeout=150, cwd=folder,
    ) or "I couldn't get through that one, sorry."


def look_at_screen(question):
    """Answers about the screen from a screenshot, which is deleted straight after (he asked: no leftovers)."""
    path = take_screenshot(copy=False)
    try:
        return ask_claude(
            "You are FRIDAY. Read the screenshot file you're given and answer about what's on his screen in one "
            "to three short spoken sentences, casual and warm, no markdown.",
            f"Screenshot: {path}\nQuestion: {question}", model="sonnet", tools="Read", timeout=120
        ) or "I couldn't make it out, annoyingly."
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def open_claude_code(task, folder=None):
    """Interactive Claude Code in Windows Terminal. The task goes through a file because wt treats ';'
    in arguments as a command separator."""
    folder = folder if folder and os.path.isdir(folder) else os.path.expanduser("~")
    runs = os.path.join(HERE, "runs")
    os.makedirs(runs, exist_ok=True)
    prompt_file = os.path.join(runs, "agent_prompt.txt")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(AGENT_PROMPT)
    task_file = os.path.join(runs, time.strftime("task-%Y%m%d-%H%M%S.txt"))
    with open(task_file, "w", encoding="utf-8") as f:
        f.write(task or "")
    subprocess.Popen(["wt", "-d", folder, "pwsh", "-NoLogo", "-NoExit", "-File",
                      os.path.join(HERE, "claude-task.ps1"), "-TaskFile", task_file, "-PromptFile", prompt_file])


class Agent:
    """One background Claude Code run. Streams its steps (for 'what are you doing?') and returns the
    final message, which Friday speaks. Each run is logged under runs/."""
    TOOL_WORDS = {"WebSearch": "searching the web", "WebFetch": "reading a web page", "Read": "reading files",
                  "Write": "writing a file", "Edit": "editing a file", "Bash": "running a command",
                  "PowerShell": "running a command", "Glob": "looking for files", "Grep": "searching files"}

    def __init__(self, task, title, system=None, cwd=None, chrome=True, model=None, prepare=None, max_turns=None):
        self.task, self.title = task, (title or task[:40])
        self.max_turns = max_turns  # a hard cap on the agent's steps (the diagnose job: he's waiting by voice)
        self.prepare = prepare  # a function that builds the real task first (evidence gathering), shown as a step
        self.model = model  # None = Claude Code's default
        self.system = system or (AGENT_PROMPT + AGENT_VOICE)
        self.cwd = cwd or os.path.expanduser("~")
        self.chrome = chrome
        self.step = "getting started"
        self.started = time.time()
        self.proc = None
        self.cancelled = False
        self.tracker = None  # the "task ongoing" pill (start_agent sets it)

    def run(self):
        if LIMIT.blocked():
            return claude_is_out()
        if self.prepare:
            self.step = "gathering the evidence"
            try:
                self.task = self.prepare()
            except Exception as e:
                log.exception("agent prepare failed")
                self.task += f"\n\n(Friday's own evidence gathering failed: {str(e)[:200]}; gather it yourself.)"
            if self.cancelled:
                return None
        runs = os.path.join(HERE, "runs")
        os.makedirs(runs, exist_ok=True)
        transcript = open(os.path.join(runs, time.strftime("agent-%Y%m%d-%H%M%S.jsonl")), "w", encoding="utf-8")
        args = ["claude", "-p", "--permission-mode", "auto", "--output-format", "stream-json", "--verbose",
                "--append-system-prompt-file", prompt_file(self.system)]
        if self.chrome:
            args.insert(2, "--chrome")
        if self.model:
            args += ["--model", self.model]
        if self.max_turns:
            args += ["--max-turns", str(int(self.max_turns))]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", creationflags=NO_WINDOW, cwd=self.cwd)
        self.proc.stdin.write(self.task)
        self.proc.stdin.close()
        result = None
        with transcript:
            transcript.write(json.dumps({"task": self.task}) + "\n")
            for line in self.proc.stdout:
                transcript.write(line)
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                LIMIT.note_event(ev)
                if ev.get("type") == "result" and ev.get("is_error") and LIMIT.blocked():
                    result = claude_is_out()
                    continue
                if ev.get("type") == "assistant":
                    for part in ev.get("message", {}).get("content", []):
                        if part.get("type") == "tool_use":
                            inp = part.get("input", {}) if isinstance(part.get("input"), dict) else {}
                            self.step = self.describe(part.get("name", ""), inp)
                            log.info("AGENT[%s] %s", self.title, self.step)
                            if self.tracker:
                                if part.get("name") == "WebSearch" and inp.get("query"):
                                    self.tracker.step(f"Searching: {inp['query']}")
                                elif str(inp.get("url", "")).startswith("http"):
                                    self.tracker.site(inp["url"])
                                else:
                                    self.tracker.step(self.step[:1].upper() + self.step[1:])
                elif ev.get("type") == "result":
                    result = ev.get("result") or ""
        self.proc.wait()
        if self.cancelled:
            return None
        return result or "That job ended without a report. The log's in the runs folder."

    def describe(self, name, inp):
        if "chrome" in name.lower():
            url = inp.get("url", "")
            return "browsing" + (f" {url.split('/')[2]}" if url.count("/") >= 2 else "")
        return self.TOOL_WORDS.get(name, name.split("__")[-1].replace("_", " "))

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW)


# ---------------------------------------------------------------- Friday

YES = re.compile(r"\b(yes|yeah|yep|yup|sure|send|send it|ok|okay|do it|go|go ahead|confirm|haan|ha|correct)\b", re.I)
NO = re.compile(r"\b(no|nope|don't|do not|cancel|stop|wait)\b", re.I)
# a job or Claude Code prompt that would send something on his behalf
SENDS = re.compile(r"\b(send|sending|reply|replying|respond to|forward|forwarding|post(?:ing)? (?:it|this|that|on|to)|"
                   r"publish|tweet|dm|e-?mail(?!s\b)|mail (?:it|this|that|him|her|them)|message (?:him|her|them)|"
                   r"text (?:him|her|them))\b", re.I)
# "send it back to me", "send the summary to my phone": that's him getting a result, not a message to a person
TO_HIM = re.compile(r"\bsend(?:ing)?\b(?:\s+\w+){0,4}?\s+(?:back(?:\s+to\s+(?:me|him|sam|his phone|the phone|my phone))?|"
                    r"to\s+(?:me|him|sam|his phone|the phone|my phone|the laptop))\b", re.I)


def sends_to_someone(task):
    """Does this job message, email or post to another person? (Getting the result back to him doesn't count.)"""
    return bool(SENDS.search(TO_HIM.sub(" ", task or "")))


# windows where Enter sends a message
MSG_WINDOW = re.compile(r"whatsapp|gmail|outlook|\bmail\b|inbox|teams|slack|telegram|messenger|discord|signal|"
                        r"linkedin|instagram|facebook|compose", re.I)
# a click that sends a message, places a call or spends/deletes something
CLICK_RISK = re.compile(r"\b(call|dial|ring|send|post|reply|submit|pay|buy|purchase|place order|checkout|delete)\b",
                        re.I)
STOP = re.compile(r"^\W*(stop|cancel|never ?mind|nothing|forget it|shut up|that's all|that's it)\W*$", re.I)
# harmless actions that never ask, type or send: they run while the line before them is still being spoken
INSTANT_ACTS = {"open_url", "youtube", "open_app", "media", "screenshot", "remember", "feedback",
                "save_note", "remind"}
# His name, as the accurate model writes it / as the tiny screening model might mangle it
NAME = re.compile(r"\b(friday|fryday)\b", re.I)
NAME_LOOSE = re.compile(r"\b(friday|fryday|fri day|frieda|fridey|frida)\b", re.I)
# "Friday" is also a weekday: these around it mean he's talking about the day, not to her
DAY_BEFORE = {"on", "this", "next", "last", "by", "until", "till", "from", "every", "since", "before", "after",
              "each", "a", "black", "good", "thank", "god", "tgi", "of", "that", "till", "coming", "or", "and", "the",
              "it's", "its", "is", "was", "til", "'til", "today's", "tomorrow's", "for", "to"}
DAY_AFTER = {"night", "morning", "afternoon", "evening", "meeting", "deadline", "release", "prayers", "namaz",
             "the", "and", "or", "is", "was", "at", "off", "noon"}


class TurnVad:
    """Silero speech detector for end-of-turn. Re-checks the last 2 s every 80 ms chunk (~10 ms of CPU)
    and answers "is there speech in this latest chunk?". (sherpa-onnx's streaming detector kept saying
    "speech" for a second after he stopped, slower than plain loudness.)"""

    def __init__(self):
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        self.find = get_speech_timestamps
        self.opts = VadOptions(threshold=float(CFG.get("vad_threshold", 0.5)), min_silence_duration_ms=100,
                               min_speech_duration_ms=120, speech_pad_ms=30)
        self.buf = np.zeros(0, np.float32)
        self.find(np.zeros(RATE, np.float32), self.opts)  # load the model now, not on his first word

    def reset(self):
        self.buf = np.zeros(0, np.float32)

    def speaking(self, chunk):
        self.buf = np.concatenate([self.buf, chunk.astype(np.float32) / 32768.0])[-2 * RATE:]
        if len(self.buf) < 0.4 * RATE:
            return True  # too little to judge yet
        stamps = self.find(self.buf, self.opts)
        return bool(stamps) and stamps[-1]["end"] > len(self.buf) - len(chunk)


class NoWakeWord:
    """Stands in for openWakeWord when no wake-word model is configured."""

    def predict(self, chunk):
        return {}

    def reset(self):
        pass


STATE_PATH = os.path.join(DATA, "state.json")
STYLE_LOG = os.path.join(DATA, "style_log.jsonl")  # messages he sent and what led to them (care.py learns his style)
# reply suggestions in his voice: the laptop's window and the phone's notification (sent with /sync) use the same words
SUGGEST_PROMPT = (
    "You write reply suggestions for Sam, in HIS voice (not Friday's). Match how he writes to this person, from "
    "HOW HE WRITES and, above all, his real replies to them when given: work seniors get respect (Salam bs, Ji, aap) "
    "and short status lines; Mum and Dad get his shortest casual LSD; friends get banter and emoji. He writes "
    "Lisan ud-Dawat in Roman letters by default (che, chu, cho, never chhe/chhu/chho; nai, nthi, su, kem, kidu, toh, "
    "ne; Arabic/Urdu words, never a 'sh' sound) and English for work detail. Short like his real texts: often 1-4 "
    "words, no full stop. Reply with ONLY a JSON array of 3 strings, different in intent (e.g. yes / later / a question).")

STYLE_PEOPLE = os.path.join(DATA, "style_people.json")  # his real replies per person (from his chat exports; never Sara)


STYLE_LEARNED = os.path.join(DATA, "style_learned.json")  # his replies learnt since, per sender (instant learning)
_learn_lock = threading.Lock()


def style_examples(sender):
    """His real exchanges with this person or group (their line -> his reply): from his chat exports, then what she's
    learnt since (newest last, so they count most). [] for Sara (private) or an unknown sender with nothing learnt."""
    s = (sender or "").lower().strip()
    if not s or "sara" in s:
        return []
    out = []
    try:
        for p in json.load(open(STYLE_PEOPLE, encoding="utf-8")).values():
            if any(n.lower() == s or (len(n) > 4 and n.lower() in s) for n in p.get("names", [])):
                out = list(p.get("examples", []))
                break
    except (OSError, ValueError):
        pass
    out += [x for x in style_learned().get(s, []) if x not in out]
    return out[-20:]


def style_learned():
    try:
        return json.load(open(STYLE_LEARNED, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def learn_style(sender, their, mine):
    """Instant learning: a reply he really sent (from her suggestions, edited or not, on either device) joins that
    person's examples now, so the very next suggestion for them sounds like it. No AI call. Never Sara."""
    who = (sender or "").lower().strip()
    if not who or "sara" in who or not (their or "").strip() or not (mine or "").strip():
        return
    line = f"them: {their.replace(chr(10), ' ')[:120]} -> him: {mine.replace(chr(10), ' ')[:120]}"
    with _learn_lock:
        data = style_learned()
        lst = data.get(who, [])
        if line in lst:
            return
        data[who] = (lst + [line])[-20:]
        with open(STYLE_LEARNED, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    log.info("learnt a reply of his to %s", sender)

friday = None  # the running Jarvis, for care.py


def bridge_idle():
    """Seconds since his last keypress or mouse move."""
    try:
        import bridge
        return bridge._idle_seconds()
    except Exception:
        return 0


def _state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def lsd_on():
    """Talking in Lisan ud-Dawat: he's heard in Gujarati (Whisper "gu") and she answers in Bohra Gujarati, spoken by
    the Gujarati voice. Switched by "talk to me in LSD" / the language action; kept in data/state.json."""
    return LANG.get("mode") == "lsd"


def set_language(mode):
    LANG["mode"] = mode
    st = _state()
    st["language"] = mode
    try:
        json.dump(st, open(STATE_PATH, "w", encoding="utf-8"))
    except OSError:
        pass
    log.info("language: %s", mode)


LANG = {"mode": _state().get("language", "en")}


# ---------------------------------------------------------------- office mode
# At the office she makes no sound: replies show on screen (spoken only into earphones), reminders become a phone
# notification, message read-outs and heads-ups become cards, no chimes or fillers, and only his voice wakes her.
# On by voice ("office mode on"), the tray, her window or the phone; by itself on an office Wi-Fi (config office_wifi)
# or in office hours (config office_hours {"days": "Mon-Sat", "from": "10:30", "to": "19:00"}), if he set those.
OFFICE = {"on": bool(_state().get("office", False)), "how": _state().get("office_how", "manual"), "checked": 0.0}


def office_on():
    return bool(OFFICE.get("on"))


def office_switch(on):
    """His own on/off (voice, tray, window, phone): an off holds for the rest of the day against the auto switch."""
    set_office(on, "manual" if on else f"manual-off-{dt.date.today()}")


def set_office(on, how="manual"):
    OFFICE.update(on=bool(on), how=how)
    st = _state()
    st["office"], st["office_how"] = bool(on), how
    try:
        json.dump(st, open(STATE_PATH, "w", encoding="utf-8"))
    except OSError:
        pass
    log.info("office mode %s (%s)", "on" if on else "off", how)
    MEMORY.log("Action", f"office mode {'on' if on else 'off'} ({how})")
    if friday is not None:
        friday.emit(type="office", on=bool(on))


_EARS = {"t": 0.0, "v": False, "name": ""}
EARPHONE = re.compile(r"head ?phone|head ?set|ear ?phone|ear ?buds?|\bbuds\b|airpods|hands-?free|\bear\b|bose|sony wh|jbl tune", re.I)


def earphones():
    """True when her voice would only reach his ears: the default speaker is headphones, a headset or buds."""
    if time.time() - _EARS["t"] < 10:
        return _EARS["v"]
    name = ""
    try:
        import pythoncom
        pythoncom.CoInitialize()
        from pycaw.pycaw import AudioUtilities
        name = AudioUtilities.GetSpeakers().FriendlyName or ""  # the default speaker (pycaw's AudioDevice)
    except Exception:
        log.exception("earphone check failed")
    _EARS.update(t=time.time(), v=bool(EARPHONE.search(name)), name=name)
    return _EARS["v"]


class duck_media:
    """While she reads a message over his video or music in his earbuds: the other apps drop to `level` of their
    volume, and come back when she's done."""

    def __init__(self, level=0.3):
        self.level, self.saved = level, []

    def __enter__(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
            from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
            for s in AudioUtilities.GetAllSessions():
                if s.Process is None or s.Process.pid == os.getpid():
                    continue
                v = s._ctl.QueryInterface(ISimpleAudioVolume)
                was = v.GetMasterVolume()
                self.saved.append((v, was))
                v.SetMasterVolume(was * self.level, None)
        except Exception:
            log.exception("ducking media failed")
        return self

    def __exit__(self, *exc):
        for v, was in self.saved:
            try:
                v.SetMasterVolume(was, None)
            except Exception:
                pass
        return False


def quiet_out():
    """Office mode and nothing in his ears (or he switched 'speak into earphones' off): she mustn't make a sound."""
    return office_on() and not (CFG.get("office_earphones", True) and earphones())


def _in_hours(spec, now=None):
    """{"days": "Mon-Sat" | "Mon,Tue,...", "from": "10:30", "to": "19:00"} -> is now inside it."""
    now = now or dt.datetime.now()
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    days = str(spec.get("days", "Mon-Fri")).lower().replace(" ", "")
    ok_days = set()
    for part in days.split(","):
        if "-" in part:
            a, b = (names.index(x[:3]) for x in part.split("-", 1))
            ok_days |= set(range(a, b + 1)) if a <= b else set(range(a, 7)) | set(range(0, b + 1))
        elif part[:3] in names:
            ok_days.add(names.index(part[:3]))
    hm = now.strftime("%H:%M")
    return now.weekday() in ok_days and spec.get("from", "00:00") <= hm < spec.get("to", "23:59")


def office_auto():
    """Called from the clock every 15 s (checks once a minute): office Wi-Fi or office hours switch it on, and leaving
    them switches it off again, but only if it was switched on that way (his own on/off always wins)."""
    if time.time() - OFFICE["checked"] < 60:
        return
    OFFICE["checked"] = time.time()
    nets = [n.lower() for n in CFG.get("office_wifi", []) if n]
    hours = CFG.get("office_hours") or None
    if not nets and not hours:
        return
    ssid = (devices.laptop_status().get("ssid") or "").lower() if nets else ""
    at_work = bool(ssid and ssid in nets) or bool(hours and _in_hours(hours))
    why = "wifi" if ssid and ssid in nets else "hours"
    if at_work and not office_on() and OFFICE.get("how") != f"manual-off-{dt.date.today()}":
        set_office(True, why)
        if friday is not None:
            friday.status("idle", "Office mode on: I'll stay quiet", hide_after=6)
    elif not at_work and office_on() and OFFICE.get("how") in ("wifi", "hours"):
        set_office(False, "left")
        if friday is not None:
            friday.status("idle", "Office mode off", hide_after=6)

# Textbook / Hindi words the brain slips into, and the Lisan ud-Dawat word he wants instead (Devanagari, as spoken).
# Longer phrases first. His rules (11 Oct 2026): Arabic and Urdu words, not Hindi; English nouns stay English;
# never a "sh" sound (Gujarati श becomes स in LSD: su, saheb, sukran).
_LSD_WORDS = [(re.compile("(?<![\u0900-\u097F])" + a + "(?![\u0900-\u097F])"), b) for a, b in (
    ("समजायुं नहीं", "समझ नई पड़ी"), ("समजायुं नई", "समझ नई पड़ी"), ("समजायुं", "समझ पड़ी"), ("समजातुं नथी", "समझ नई पड़ती"),
    ("फरी एकवार कहेशो", "फरी सी कहो ने"), ("फरी एक वार कहेशो", "फरी सी कहो ने"), ("कहेशो", "कहो ने"),
    ("नहीं", "नई"), ("नहि", "नई"), ("आभार", "सुक्रन"), ("धन्यवाद", "सुक्रन"), ("कृपया", "मेहरबानी करी ने"), ("कृपा करीने", "मेहरबानी करी ने"),
    ("रविवार", "इतवार"), ("सोमवार", "पीर"), ("मंगळवार", "मंगल"), ("मंगलवार", "मंगल"), ("बुधवार", "बुध"), ("गुरुवार", "जुमेरात"),
    ("शुक्रवार", "जुम्मा"), ("समय", "वक्त"), ("जरूर", "ज़रूर"), ("प्रश्न", "सवाल"), ("उत्तर", "जवाब"), ("चिंता", "फिकर"),
    ("सवारे", "सुबह"), ("सवार", "सुबह"), ("सांजे", "शाम"), ("सांज", "शाम"), ("दिवस", "दिन"), ("वधारे", "ज़्यादा"), ("मुश्केली", "मुस्किल"),
    ("सरळ", "आसान"), ("प्रयत्न", "कोसिस"), ("कोशिश", "कोसिस"), ("भगवान", "खुदा"), ("मित्र", "दोस्त"), ("पुस्तक", "किताब"),
    ("सहायता", "मदद"), ("स्वागत", "खुसामदीद"), ("खरेखर", "वाकई"), ("जलदी", "जल्दी"), ("परवानगी", "इजाज़त"), ("समाचार", "खबर"),
    ("इच्छा", "ख्वाहिस"), ("शरूआत", "सुरु"), ("समाप्त", "खतम"), ("निश्चित", "पक्कुं"), ("अवश्य", "ज़रूर"), ("तुरंत", "फौरन"),
    ("बिलकुल", "बिल्कुल"), ("क्षमा", "माफ़ी"), ("आनंद", "खुसी"), ("खुशी", "खुसी"), ("शुक्रन", "सुक्रन"),
    ("शुक्रिया", "सुक्रिया"), ("शुभ", "मुबारक"), ("सहेब", "साहेब"),
    # the Dawat's words from the 1448 waaz in his LSD skill (11 Oct 2026, 19:13), for the Hindi the brain reaches for
    ("निर्णय", "इरादो"), ("फैसलो", "इरादो"), ("विचार", "खयाल"), ("अनुभव", "तजुर्बो"), ("नेतृत्व", "कियादत"), ("वातावरण", "माहोल"),
    ("तैयारी", "इस्तेदाद"), ("व्यवस्था", "इंतज़ाम"), ("प्रबंधन", "इंतज़ाम"), ("गलती", "भूल"), ("भूलचूक", "भूल"), ("सुधारो", "दुरुस्त करो"),
    ("सुधार", "दुरुस्ती"), ("भविष्य", "आइंदा"), ("लाभ", "फायदो"), ("फायदा", "फायदो"), ("जोखिम", "खतरो"), ("खतरा", "खतरो"),
    ("व्यापार", "वेपार"), ("अवसर", "मौको"), ("आवश्यक", "लाज़िम"), ("आवश्यकता", "ज़रूरत"), ("योजना", "plan"), ("अनुमान", "तखमीन"),
    ("जाँच", "तपास"), ("जांच", "तपास"), ("उपयोग", "इस्तेमाल"), ("सभी", "सगला"), ("बधा", "सगला"), ("सलाह", "नसीहत"), ("गुप्त", "मखफी"),
    ("कठिन", "मुस्किल"), ("सरल", "आसान"), ("स्तर", "मेयार"), ("चरण", "मरहला"), ("सीमा", "हुदूद"), ("विशेषज्ञ", "माहिर"), ("कौशल", "महारत"),
    ("तर्क", "मंतिक"), ("संदेह", "सक"), ("शंका", "सक"), ("सावधान", "चोकना"), ("एकता", "इत्तेहाद"), ("गंभीर", "संजीदा"), ("साहस", "दिलेरी"),
    ("धैर्य", "सबर"), ("कृतज्ञ", "सुकरगुज़ार"), ("प्रतीक्षा", "इंतज़ार"), ("अनुमति", "इजाज़त"), ("उचित", "मुनासिब"), ("पर्याप्त", "काफी"),
    ("सफलता", "कामयाबी"), ("सफल", "कामयाब"), ("असफल", "नाकाम"), ("स्वास्थ्य", "सेहत"), ("विश्वास", "भरोसो"), ("ईमानदारी", "ईमानदारी"),
)]
_GU2DEVA = {c: chr(ord(c) - 0x180) for c in map(chr, range(0x0A80, 0x0B00))}


_LSD_MARKERS = set("che chhe chu chhu cho chho kem tame tamne tamaru tamari tamara mane mara maru mari maro hu hun nathi nai "
                   "kari karo karjo kidu kidhu kidi ane aaje aje kale shu tya thi jaisu jasu lejo dijo dejo "
                   "aapo apo jo haa hasu hase sathe pachi pehla vagye vage vaage hamna hamnaj thai thayu ghanu saru saras "
                   "joiye joie bolo bolje kaho kahe kahu mate mathi kaik koi aavjo avjo aavse avse kem chho "
                   "maj mazama majama".split())


def spoken_language(text):
    """'lsd' or 'en': which language he just spoke, from the words (Gujarati/Devanagari letters, or romanized LSD
    words like che, chu, tame, kari, nathi), so she answers in the same one."""
    t = (text or "").lower()
    if re.search(r"[\u0A80-\u0AFF\u0900-\u097F]", t):
        return "lsd"
    words = re.findall(r"[a-z']+", t)
    if not words:
        return ""
    hits = sum(1 for w in words if w in _LSD_MARKERS)
    strong = sum(1 for w in words if w in ("che", "chhe", "chu", "chhu", "cho", "chho", "nathi", "kidu", "kidhu", "kem", "tame", "mane", "hu", "aa"))
    return "lsd" if (hits >= 2 and hits / len(words) >= 0.18) or (strong and len(words) <= 7) else "en"


def lsd_say(text):
    """A SAY line in Lisan ud-Dawat mode, the way he wants it heard: one script (Gujarati script moved into
    Devanagari, so one voice reads the whole line), no "sh" (श/ष -> स, as the Bohras say it), and the textbook
    Gujarati or Hindi words the brain keeps slipping in swapped for the Dawat's. English words are untouched."""
    if not text or not GU_SCRIPT.search(text):
        return text
    text = text.translate(str.maketrans(_GU2DEVA))
    for pat, word in _LSD_WORDS:
        text = pat.sub(word, text)
    return text.replace("श", "स").replace("ष", "स").replace("ळ", "ल")
GU_NAME = re.compile(r"ફ્ર\S{0,4}ડ\S{0,2}|फ्र\S{0,4}ड\S{0,2}")  # her name in Gujarati or Devanagari script (ફ્રાઈડે, फ्राइडे)


LSD_EAR_PROMPT = """You transcribe short voice clips of Sam, a Dawoodi Bohra from Mumbai, talking to his assistant Friday.
He speaks Lisan ud-Dawat: Gujarati grammar with Bohra pronunciation, Hindi-Urdu and Arabic words, and English words
mixed in. He may also speak plain English or Hindi. Return ONLY JSON: {"latin": "...", "deva": "...", "lang": "lsd"|"en"|"hi"}.
latin = his words exactly as mumineen type them in chat (not scholarly transliteration): che, chu, cho, nai, nthi, su,
kem, kaha, kyare, kon, kidu, mane, tame, tu, apne, ye, thai gyu, mokli do, lai lo, khabar nai, aaje, kaale, hamna, pchi,
saheb, sukran (Bohras say "s" where Gujarati has "sh": su, not shu; never write "sh" for his Gujarati words),
bs, toh, ne, j, si, vaste, sathe, Alhamdolillah, inshallah, mashallah, khuda hafiz, shukran, salaam, miqaat, waaz,
majlis, masjid. English words in normal English spelling (WhatsApp, message, office, call, meeting, Rapido). The
assistant's name is "Friday". Examples of how he writes: "Su kidu?" / "Khabar nai" / "Kaale aavso masjid?" /
"Aa cabinet ni keys kaha che" / "Tame mokli toh chuka ne?" / "Me check kri lais ek vaar" / "Friday, Sara ne msg kar ke
hu late aavis" / "Hamna ketla vagya?" / "Mara calendar ma aaje su che?" / "Bas, khuda hafiz".
deva = the same words in Devanagari. If there is no speech, return empty strings. Never translate, never add words,
never answer him."""


def gemini_transcribe(speech):
    """His Lisan ud-Dawat heard by Gemini (Whisper's Gujarati made nothing of it): (latin, devanagari, lang)."""
    import base64
    import io
    import wave
    import requests
    key = backup_brain.load_keys().get("gemini")
    if not key:
        raise RuntimeError("no Gemini key")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes((np.clip(speech, -1, 1) * 32767).astype(np.int16).tobytes())
    body = {"systemInstruction": {"parts": [{"text": LSD_EAR_PROMPT}]},
            "contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(buf.getvalue()).decode()}},
                {"text": "Transcribe this clip."}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 400, "responseMimeType": "application/json"}}
    models = [CFG.get("lsd_ear_model", "gemini-3.1-flash-lite-preview"), "gemini-flash-lite-latest"]
    err = None
    for model in dict.fromkeys(models):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              json=body, headers={"x-goog-api-key": key}, timeout=(4, 8))
            if r.status_code != 200:
                raise RuntimeError(f"Gemini {model} {r.status_code}: {r.text[:120]}")
            d = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            latin = (d.get("latin") or "").strip()
            if latin:
                return latin, (d.get("deva") or "").strip(), (d.get("lang") or "lsd")
            err = RuntimeError(f"Gemini {model}: nothing heard")
        except Exception as e:
            err = e
    raise err


LSD_EAR_HINT = ("Friday, salaam. Su kidu? Khabar nai. Sara ne message mokli de ke hu late aavis. Hamna ketla vagya? "
                "Kaale office ketla vaage jawanu che? Mara calendar ma aaje su che? Alhamdolillah, inshallah. Bas, khuda hafiz.")


def groq_transcribe(speech):
    """His Lisan ud-Dawat heard by Whisper large-v3 on Groq (0.6 s; the full model knows Gujarati far better than the
    laptop's turbo). The romanized hint pulls the words out in Latin letters; whatever comes back in Gujarati or
    Devanagari is romanized here, so the brain and the caption get one style."""
    import io
    import wave
    import requests
    key = backup_brain.load_keys().get("groq")
    if not key:
        raise RuntimeError("no Groq key")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes((np.clip(speech, -1, 1) * 32767).astype(np.int16).tobytes())
    r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                      files={"file": ("clip.wav", buf.getvalue(), "audio/wav")},
                      data={"model": CFG.get("lsd_ear_groq_model", "whisper-large-v3"), "language": CFG.get("lsd_ear_lang", "gu"),
                            "temperature": 0, "prompt": LSD_EAR_HINT}, timeout=(5, 12))
    if r.status_code != 200:
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:120]}")
    return translit_gu(r.json().get("text", "").strip())


def _compare_gemini(speech, stem):
    """Off the conversation path: Gemini's slower, careful transcript of the same clip, kept next to it so the two
    ears can be compared on his real voice."""
    try:
        latin, deva, lang = gemini_transcribe(speech)
        with open(stem + ".gemini.txt", "w", encoding="utf-8") as f:
            f.write(latin or deva)
    except Exception as e:
        log.info("Gemini compare skipped: %s", str(e)[:100])


def save_lsd_clip(speech, text):
    """In Lisan ud-Dawat mode every clip he speaks is kept (data/lsd_clips/<time>.wav + what Whisper made of it)
    so better recognisers can be tried on his real voice and the misses counted. Oldest go after 300 clips."""
    try:
        import wave
        folder = os.path.join(DATA, "lsd_clips")
        os.makedirs(folder, exist_ok=True)
        stem = os.path.join(folder, time.strftime("%Y%m%d-%H%M%S"))
        with wave.open(stem + ".wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes((np.clip(speech, -1, 1) * 32767).astype(np.int16).tobytes())
        with open(stem + ".txt", "w", encoding="utf-8") as f:
            f.write(text)
        old = sorted(n for n in os.listdir(folder) if n.endswith(".wav"))[:-300]
        for n in old:
            for ext in (".wav", ".txt", ".gemini.txt"):
                try:
                    os.remove(os.path.join(folder, n[:-4] + ext))
                except OSError:
                    pass
        return stem
    except Exception:
        log.exception("couldn't keep the LSD clip")
        return None


def addresses_her(text):
    """True if he's talking TO Friday: the name at the start or end, after hey/ok, or set off by commas,
    and not as the weekday ("on Friday", "this Friday", "Friday night")."""
    if lsd_on() and GU_NAME.search(text or ""):
        return True
    for m in NAME.finditer(text):
        before = re.findall(r"[a-z']+", text[:m.start()].lower())
        after = re.findall(r"[a-z']+", text[m.end():].lower())
        prev, nxt = (before[-1] if before else ""), (after[0] if after else "")
        if prev in DAY_BEFORE or nxt in DAY_AFTER:
            continue
        at_edge = not before or not after
        vocative = prev in ("hey", "hi", "ok", "okay", "yo", "so", "please", "thanks", "listen", "wait")
        commas = bool(re.search(r",\s*$", text[:m.start()])) or bool(re.match(r"\s*[,?!.]", text[m.end():]))
        if at_edge or vocative or commas:
            return True
    return False
# Whisper's stock hallucinations on silence/noise
ANSWERS = re.compile(r"^\W*(yes|yeah|yep|yup|ok|okay|sure|right|no|nope|haan|ha|nahi|go|go ahead|do it|send( it)?|"
                     r"હા|હાં|ના|નહીં|ઠીક છે|બરાબર|મોકલ(ી દે|ો)?|हा|हाँ|जी|नई|नहीं|ठीक छे|बराबर|"
                     r"ji|ji sir|haa|nai|nthi|nathi|thi?k che|barabar|kar(o|i lo)|mokli do)\W*$", re.I)
HALLUCINATIONS = re.compile(
    r"^\W*(thank you( very much| so much)?|thanks( for watching)?|you|bye( bye)?|\.+|subtitles by.*|happy new year|"
    r"merry christmas|please subscribe|subscribe|like and subscribe|see you next time|i'?ll see you( next time)?|"
    r"the end|amen|okay|ok|so|good|good good|copy( it)?|oh|ah|yeah|right|mm+|hmm+|uh+|um+|music|applause|"
    r"(thank you[.!]?\s*)+)\W*$", re.I)


def is_repetitive(text):
    """Whisper's tell-tale when it's making things up from noise: the same phrase over and over
    ("I can't check this. I can't check this. I can't check this.")."""
    parts = [p.strip().lower() for p in re.split(r"[.!?]+", text) if p.strip()]
    if len(parts) >= 2 and len(set(parts)) < len(parts):
        return True
    words = re.findall(r"[a-z']+", text.lower())
    return len(words) >= 6 and len(set(words)) / len(words) < 0.5


def is_echo(heard, saying):
    """True if what the mic picked up is mostly the sentence Friday herself is saying: runs of two or more
    of her words, in her order. Counting shared words one by one threw his own interruptions away as her
    echo ("How do you decide?" against "...tell you where it is and you can do the last tap?")."""
    h = re.findall(r"[a-z']+", heard.lower())
    s = re.findall(r"[a-z']+", (saying or "").lower())
    if not h or not s:
        return False
    if len(h) == 1:
        return h[0] in s
    from difflib import SequenceMatcher
    blocks = SequenceMatcher(None, h, s, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks if b.size >= 2) / len(h) >= 0.5


def strip_echo(heard, her_lines):
    """Removes the sentences of `heard` that are really her own recent lines picked up by the mic."""
    said = " ".join(t for t in her_lines if t)
    if not heard or not said:
        return heard
    parts = re.split(r"(?<=[.!?])\s+", heard.strip())
    kept = [p for p in parts if p and not is_echo(p, said)]
    if len(kept) != len(parts):
        log.info("echo removed from what he said: %r -> %r", heard, " ".join(kept))
    return " ".join(kept)


def has_speech(audio, seconds=0.25):
    """Silero: is there at least this much human voice in the audio (not a fan, typing, clatter)?
    Scaled up first, as his mic's echo suppression turns him down while Friday plays."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak <= 0:
        return False
    stamps = get_speech_timestamps((audio * (0.5 / peak)).astype(np.float32), VadOptions(threshold=0.5, min_speech_duration_ms=120))
    return sum(s["end"] - s["start"] for s in stamps) >= seconds * RATE


_LEAD = r"(?:(?:ok(?:ay)?|alright|all right|cool|great|perfect|nice|fine|good|awesome|yeah|yep|yup|no|nah|nope|haan|bas)[\s,.!]*)*"
_CORE = (r"(?:that'?s (?:it|all)(?: for now)?|that is (?:it|all)|that'?ll be all|that will be all|i'?m (?:done|good|all set)|"
         r"we'?re (?:done|good)|nothing(?: else)?(?: for now)?|no(?:thing)? more|thanks?(?: you)?(?: so much| a lot)?|thank you(?: so much)?|cheers|"
         r"bye(?: bye)?|good ?bye|see (?:ya|you)|later|go to sleep|stop listening|never ?mind|done|all done|bas(?: itna hi)?|shukriya)")
CLOSING = re.compile(rf"^\W*{_LEAD}{_CORE}(?:[\s,.!]*(?:{_CORE}|friday|sir|bro|man))*\W*$", re.I)


# the same in Lisan ud-Dawat: "khuda hafiz", "bas, aatlu j", "bas thai gayu"
CLOSING_GU = re.compile(r"^\W*(?:ઠીક છે[,\s]*|ઓકે[,\s]*|સારું[,\s]*|ok(?:ay)?[,\s]*|thi?k che[,\s]*|ठीक छे[,\s]*)?"
                        r"(?:ખુદા હાફિઝ|બસ,?\s*(?:એટલું|આટલું)\s*જ|બસ,?\s*થઈ ગયું|ચાલો,?\s*બાય|શુક્રન,?\s*બસ|બસ,?\s*શુક્રન|"
                        r"khuda\s*hafiz|bas,?\s*(?:aatlu|atlu|etlu)\s*j|bas,?\s*thai\s*gyu|chalo,?\s*bye|shukran,?\s*bas|bas,?\s*shukran|"
                        r"ख़?ुदा\s*हाफ़?िज़?|बस,?\s*(?:आटलुं?|एटलुं?)\s*ज|बस,?\s*थई\s*ग्युं?|चलो,?\s*बाय)"
                        r"(?:[\s,.!]*(?:સર|ફ્ર\S{0,4}ડ\S{0,2}|friday|sir|bhai|yaar|ખુદા હાફિઝ|khuda hafiz|બાય|bye|सर|बाय))*\W*$", re.I)


def closing(text):
    """He's wrapping up ("alright that's it", "thanks, I'm done", "that's all, bye"): no reply, mic off. A bare
    "okay" / "yeah" doesn't count: it can be a yes. (Same rule as the phone's Asr.closing.)"""
    t = (text or "").strip()
    return bool(CLOSING.match(t)) or bool(CLOSING_GU.match(t))


STOP_WORDS = re.compile(r"\b(stop|wait|cancel|hold on|hang on|listen|don'?t send|do not send|ruko|bas)\b", re.I)
# what he says when he really wants her to go quiet / stop listening (sleep needs one of these)
SLEEP_ASK = re.compile(r"\b(sleep|stop listening|don'?t listen|do not listen|be quiet|go quiet|shut up|mute|pause|"
                       r"leave me|not you|talking to (someone|somebody|him|her|them)|on a call|chup|band karo)\b", re.I)
# what he says when he really wants office mode (office ON needs one of these)
OFFICE_ASK = re.compile(r"\b(office|work|silent|meeting|quiet) mode\b|\b(go|stay|be) (silent|quiet)\b|\bin a meeting\b|"
                        r"\b(on|to) (the )?screen only\b|\bdon'?t (talk|speak)( out loud| aloud)?\b|\bi'?m at (the )?office\b", re.I)
SEND_ACTS = {"whatsapp_send", "whatsapp_reply", "type_text", "press_keys", "call", "agent", "claude_code", "email",
             "calendar", "click", "task"}
# a click or Enter in a task that reaches a person or spends/deletes something: always asked first
SENDING = re.compile(r"\b(send|post|submit|reply|pay|book|confirm|place order|buy|order now|checkout|delete|"
                     r"remove|call|share)\b", re.I)
PEOPLE_WINDOW = re.compile(r"whatsapp|gmail|outlook|\bmail\b|inbox|teams|slack|telegram|messenger|discord|"
                           r"signal|linkedin|instagram|facebook|google chat|chat\.google|compose", re.I)


AI_WINDOW = re.compile(r"chatgpt|claude|gemini|copilot|perplexity|deepseek|grok", re.I)  # asking an AI isn't sending


def _short_title(t):
    return re.sub(r"\s+", " ", t or "").strip()[:50]


def pyperclip_copy(text):
    import pyperclip
    pyperclip.copy(text)


class Declined(Exception):
    """He said no (or nothing) when asked before a task sent something."""


class Stopped(Exception):
    """He said stop / wait / listen (or her name) while Friday was in the middle of doing something."""

    def __init__(self, text=""):
        super().__init__(text)
        self.text = text


class StopWatch:
    """Keeps an ear open while an action runs (opening a chat, attaching files, typing): if he says stop,
    wait, cancel, listen or "Friday", the action is halted before anything goes out. Steps aside while she
    speaks (her own barge-in check listens then) and while she's asking him yes or no."""

    def __init__(self, j):
        self.j, self.heard, self.paused = j, threading.Event(), threading.Event()
        self.text, self.alive, self.thread = "", False, None

    def __enter__(self):
        self.alive = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.alive = False
        if self.thread:
            self.thread.join(timeout=2)
        return False

    def _run(self):
        j, sp, gap = self.j, [], 0
        while self.alive and not self.heard.is_set():
            if self.paused.is_set() or j.voice.speaking:
                sp, gap = [], 0
                time.sleep(0.05)
                continue
            try:
                chunk = j.audio.get(timeout=0.1)
            except queue.Empty:
                continue
            loud = level_of(chunk) > j.speech_threshold(j.noise_floor)
            if loud or sp:
                sp.append(chunk)
                gap = 0 if loud else gap + 1
            if sp and (gap >= 5 or len(sp) >= 40):  # a short pause, or 3 s of talking: check what he said
                self._check(sp)
                sp, gap = [], 0

    def _check(self, sp):
        if len(sp) < 4:
            return
        audio = np.concatenate(sp).astype(np.float32) / 32768.0
        if not has_speech(audio, seconds=0.15):
            return
        try:
            text = self.j.quick_text(audio)
        except Exception:
            log.exception("stop watch: transcription failed")
            return
        if not text or is_echo(text, getattr(self.j.voice, "saying", "")):
            return
        if STOP_WORDS.search(text) or NAME_LOOSE.search(text):
            log.info("STOP heard during an action: %r", text)
            self.text = text
            self.heard.set()
            self.j.voice.stop()
            chime("start")  # he hears straight away that she caught it, even mid-step


def strip_name(text):
    """'What's the weather today, Friday?' -> 'What's the weather today?'"""
    t = re.sub(r"\b(?:hey|hi|ok|okay|yo)?,?\s*(?:friday|fryday)\b[,.!]?", " ", text, flags=re.I)
    t = re.sub(r"\s+([,.?!])", r"\1", re.sub(r"\s{2,}", " ", t)).strip(" ,")
    return "" if not re.search(r"\w", t) else t


class Jarvis:
    def __init__(self, overlay=None, use_mic=True):
        self.overlay = overlay
        self.voice = Voice()
        self.brain = Brain()
        self.paused = False
        self.trigger = threading.Event()
        self.hang_up = threading.Event()  # Ctrl+Alt+K mid-conversation: stop listening now
        # her window (panel.py): what she and he say, task lists and yes/no questions, as numbered events
        self.events, self.event_cv, self.event_seq = [], threading.Condition(), 0
        self.typed = queue.Queue()          # (text, [file paths]) typed in her window
        self.panel_answers = queue.Queue()  # (question id, yes) from her window's buttons
        self.from_panel = False             # the current request was typed, so yes/no is asked in the window
        self.voice_on = bool(CFG.get("panel_voice", True))
        self.audio = queue.Queue()
        self.tray = None
        self.agents = {}
        self._ids = itertools.count(1)
        self.noise_floor = 10.0
        self.voice_level = float(CFG.get("voice_level", 400))  # his typical speech level, learnt as he talks
        self.target = None          # window in front when he spoke
        self.interrupted = False
        self._barge = {}
        self.cut_off = None         # the sentence he talked over, so the brain knows where it stopped
        self.heard = ""             # what he just said, and what Friday said (heard in full) in the reply before
        self.spoken, self.last_spoken = [], ""
        self.echo_gain = None       # speaker -> mic leakage, learnt while Friday talks
        self.preroll = None
        self.results = []           # raw output from this turn's actions, for the brain to explain
        self.call_ok = 0            # when he last said yes to a phone call (Phone Link may need the click)
        self.sleeping = False       # "don't listen to me": only a sentence with "Friday" in it wakes her
        self.announce = queue.Queue()  # reminders, alarms, messages to bring up when she's free
        self.in_session = False
        self.fast = None
        self.oww = NoWakeWord()
        if use_mic:
            from faster_whisper import WhisperModel
            # openWakeWord only ships "hey jarvis"; for Friday the name is found in the transcript instead
            if CFG.get("wake_word_model"):
                from openwakeword.model import Model
                self.oww = Model(wakeword_models=[CFG["wake_word_model"]], inference_framework="onnx")
            self.stt = WhisperModel(CFG.get("whisper_model", "small.en"), device="cpu", compute_type="int8",
                                    cpu_threads=int(CFG.get("whisper_threads", 8)))
            # the big model on the laptop's Intel GPU: ~0.5 s a sentence and better with his accent than
            # small.en on the CPU (~2.5 s). small.en stays as the fallback.
            self.ov = None
            if CFG.get("stt_engine", "openvino") == "openvino":
                try:
                    import openvino_genai as og
                    self.ov = og.WhisperPipeline(os.path.join(HERE, "models", "whisper-large-v3-turbo-ov"),
                                                 CFG.get("stt_device", "GPU"),
                                                 CACHE_DIR=os.path.join(HERE, "models", "ov-cache"))
                    self.ov_lock = threading.Lock()
                    log.info("speech recognition on the %s (large-v3-turbo)", CFG.get("stt_device", "GPU"))
                except Exception:
                    log.exception("GPU speech recognition unavailable, using small.en on the CPU")
            if CFG.get("listen_mode", "name_anywhere") == "name_anywhere":
                self.fast = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4)
            self.vad = None
            if CFG.get("neural_vad", True):
                try:
                    self.vad = TurnVad()
                except Exception:
                    log.exception("speech detector unavailable, using loudness for end of turn")
            self.smart = None
            if CFG.get("smart_turn", True):
                import smart_turn
                self.smart = smart_turn.load()
                if self.smart is None:
                    log.warning("smart end-of-turn unavailable, using the fixed silence")
            self.spk = None
            if CFG.get("voice_id", True):
                try:
                    from speaker import SpeakerID
                    self.spk = SpeakerID()
                    log.info("voice recognition on (%d samples of his voice)", len(self.spk.samples))
                except Exception:
                    log.exception("voice recognition unavailable")

    # --- status
    def status(self, state, text="", hide_after=None):
        if text:
            log.info("[%s] %s", state, text[:300])
        if self.overlay:
            self.overlay.show(state, text, hide_after)
        elif text:
            print(f"[{state}] {text}", flush=True)
        if self.tray:
            self.tray.icon = make_icon(Overlay.COLORS.get(state, "#3b82f6"))
        if state != getattr(self, "_last_state", None):
            self._last_state = state
            self.emit(type="state", state=state)

    # --- her window (panel.py talks to these through the bridge)
    def emit(self, **event):
        """Adds something to her window's feed (numbered, so the window only fetches what's new)."""
        with self.event_cv:
            self.event_seq += 1
            event["n"] = self.event_seq  # never len(): the list is capped at 500
            if isinstance(event.get("text"), str):
                event["text"] = translit_gu(event["text"])  # her window shows LSD in Latin letters, as he asked
            event.setdefault("type", "line")
            event["t"] = time.strftime("%H:%M")
            self.events.append(event)
            del self.events[:-500]
            self.event_cv.notify_all()

    def events_after(self, n, wait=20.0):
        with self.event_cv:
            if not self.events or self.events[-1]["n"] <= n:
                self.event_cv.wait(timeout=wait)
            return [e for e in self.events if e["n"] > n]

    def panel_confirm(self, question):
        """Yes/no asked in her window (he typed the request): buttons there, spoken too if her voice is on."""
        qid = secrets.token_hex(4)
        while not self.panel_answers.empty():
            self.panel_answers.get_nowait()
        self.emit(type="confirm", id=qid, text=question)
        if self.voice_on and not quiet_out():
            self.voice.say(clean_speech(question), mood="calm")
        end = time.time() + 180
        while time.time() < end:
            self.check_stop()
            try:
                aid, yes = self.panel_answers.get(timeout=0.3)
            except queue.Empty:
                continue
            if aid == qid:
                self.emit(type="answered", id=qid, yes=yes)
                log.info("CONFIRM (window) %r -> %s", question, yes)
                return yes
        self.emit(type="answered", id=qid, yes=False)
        return False

    FILLERS = ("One sec, sir.", "On it.", "Give me a sec.", "Yep, on it.", "Working on it, sir.")
    FILLERS_GU = ("एक सेकंड, सर.", "हमणा करुं छुं.", "बस एक मिनट.", "जी सर, करुं छुं.")

    def filler(self):
        """A short "heard you" line for when the brain is slow, rendered ahead so it plays the instant it's needed.
        Returns the arguments for Voice.say."""
        spoke = getattr(self, "spoke", "")
        gu = spoke == "lsd"  # same rule as her replies (say()): LSD only after he spoke it
        key = "_fillers_gu" if gu else "_fillers"
        if not getattr(self, key, None):
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=1)
            setattr(self, key, [(t, pool.submit(self.voice.render, t, "calm")) for t in (self.FILLERS_GU if gu else self.FILLERS)])
        text, clip = random.choice(getattr(self, key))
        log.info("filler: %s", text)
        return text, None, "calm", clip

    def panel_stop(self):
        """The Stop button in her window: same as saying "stop": halts what's running and the rest of a list."""
        self.list_stop = True
        self.interrupted = True
        self.voice.stop()
        w = getattr(self, "watch", None)
        if w is not None:
            w.text = "stop (button)"
            w.heard.set()
        while not self.panel_answers.empty():
            self.panel_answers.get_nowait()
        self.emit(type="stopped")

    def run_typed(self, text, files=()):
        """Something typed in her window: one request, or a numbered list done one by one, in order. Files
        attached are hers to read and work with. If she needs an answer mid-list, the next thing he types is it."""
        files = [f for f in files if os.path.isfile(f)]
        attach = ""
        if files:
            attach = ("\n\nFiles he attached (use the file action to read or work with them): "
                      + "; ".join(f'"{f}"' for f in files))
        items = split_tasks(text)
        self.from_panel, self.in_session = True, True
        self.cut_off = None
        self.target = user32.GetForegroundWindow()
        try:
            if len(items) < 2:
                r = self.handle(text + attach)
                self.emit(type="idle")
                return r
            tasks = [{"text": t, "state": "waiting"} for t in items]
            self.emit(type="tasks", items=tasks)
            for i, task in enumerate(tasks):
                task["state"] = "running"
                self.emit(type="tasks", items=tasks)
                r = self.handle(f"Task {i + 1} of {len(tasks)} from his list: {task['text']}" + (attach if i == 0 else ""))
                if r == "reply":  # she asked him something: the next thing he types (or nothing in 5 min) answers it
                    task["state"] = "needs you"
                    self.emit(type="tasks", items=tasks)
                    try:
                        answer, _ = self.typed.get(timeout=300)
                    except queue.Empty:
                        answer = ""
                    if answer:
                        r = self.handle(answer)
                if r in ("interrupted", "stopped") or getattr(self, "list_stop", False):
                    task["state"] = "stopped"
                    for rest in tasks[i + 1:]:
                        rest["state"] = "skipped"
                    self.emit(type="tasks", items=tasks)
                    break
                task["state"] = "done"
                self.emit(type="tasks", items=tasks)
            self.emit(type="idle")
        finally:
            self.from_panel, self.in_session, self.list_stop = False, False, False

    def say(self, text, state="speaking", barge=True, mood=None, clip=None):
        """Speaks; returns True if he talked over Friday (only checked inside a conversation)."""
        if not text or not re.search(r"\w", text):
            return False
        if getattr(self, "spoke", "") == "lsd":  # LSD only when his last words were LSD (his rule, 13 Oct)
            text = lsd_say(text)
        if getattr(self, "remote", None) is not None:  # a request from his phone: she answers there
            log.info("SAY (to phone) %s", text)
            MEMORY.log("Friday", f"[to phone] {text}")
            self.remote.append(text)
            return False
        log.info("SAY %s", text)
        MEMORY.log("Friday", text)
        self.emit(who="friday", text=text)
        if self.from_panel and not self.voice_on:  # typed in her window with her voice off: words on screen only
            self.status(state, f"Friday: {text}", hide_after=4)
            self.spoken.append(text)
            return False
        if quiet_out():  # office mode: on screen only (spoken only into earphones)
            log.info("(office: on screen, not spoken)")
            self.status(state, f"Friday: {text}", hide_after=max(5, min(14, len(text) / 14)))
            self.spoken.append(text)
            return False
        self.status(state, f"Friday: {text}")
        can_barge = barge and self.in_session and CFG.get("barge_in", True) and hasattr(self, "oww")
        if can_barge and not self._barge:
            # once per reply, not per sentence: the mic stays watched between streamed sentences, so
            # he isn't ignored for the start of every sentence and words said in the gaps still count
            self._barge = {"levels": [], "loud": 0, "buf": []}
            self._drain()
            # openWakeWord scores a rolling ~2 s window: without a reset it re-detects the "Hey Friday"
            # that started this conversation and Friday cuts himself off a second into every answer
            self.oww.reset()
        cut = self.voice.say(text, interrupt=self._barge_check if can_barge else None, mood=mood, clip=clip)
        if can_barge and "thr" in self._barge:  # so a missed interruption can be read off the log
            log.info("barge watch: mic peak %.0f, threshold %.0f, echo gain %.4f",
                     self._barge["peak"], self._barge["thr"], self.echo_gain or 0)
            self._barge["peak"] = 0.0
        if not cut:
            self.spoken.append(text)
        if cut:
            log.info("BARGE-IN")
            MEMORY.log("Friday", "(he interrupted)")
            self.interrupted = True
            self.cut_off = text
        return cut

    def _playback_level(self):
        """How loud Friday's own audio was over the last ~0.45 s, the window its echo can arrive in."""
        if self.voice.engine:
            return self.voice.engine.ref_level()
        np_ = self.voice.now_playing
        if not np_:
            return 0.0
        samples, rate, t0 = np_
        end = int((time.time() - t0 - 0.03) * rate)
        start = max(0, end - int(0.45 * rate))
        if end <= 0 or start >= len(samples):
            return 0.0
        seg = samples[start:min(end, len(samples))].astype(np.float32)
        step = int(0.05 * rate)
        return max((float(np.sqrt(np.mean(seg[i:i + step] ** 2))) for i in range(0, max(len(seg) - step, 1), step)),
                   default=0.0)

    def _barge_check(self):
        """Polled while Friday speaks: True when he starts talking or says "Friday".
        Laptop speakers leak into the mic, so his voice only counts when the mic is much louder than
        the echo predicted from what Friday is playing right now (gain learnt as Friday talks)."""
        b = self._barge
        b.setdefault("t0", time.time())  # first poll = playback has just started
        hit = False
        lvl = 0.0
        ratio = float(CFG.get("barge_in_ratio", 3.0))
        ignore = float(CFG.get("barge_in_ignore_seconds", 0.4))  # the onset of his own voice
        while True:
            try:
                chunk = self.audio.get_nowait()
            except queue.Empty:
                break
            lvl = level_of(chunk)
            ref = self._playback_level()
            b["buf"] = (b["buf"] + [chunk])[-10:]
            woke = max(self.oww.predict(chunk).values(), default=0) >= CFG.get("wake_threshold", 0.5)
            if woke and time.time() - b["t0"] > 1.0:
                hit = True
                continue
            if ref > 800 and b["loud"] == 0:  # learn the speaker->mic gain while he's quiet
                b["levels"].append(lvl / ref)
                if len(b["levels"]) >= 8:
                    # median, not a high percentile: chunks where he's already talking (not yet counted as
                    # loud) mustn't pass for echo, or the gain climbs and he can never talk over Friday.
                    # His mic's own echo suppression leaves ~4 against a playback level of ~12000.
                    learnt = float(np.median(b["levels"][-30:]))
                    self.echo_gain = learnt if self.echo_gain is None else 0.8 * self.echo_gain + 0.2 * learnt
            if self.echo_gain is None or time.time() - b["t0"] < ignore:
                continue
            expected = self.echo_gain * ref
            # Windows' echo suppression also turns him down while Friday plays, so a third of his usual
            # loudness is enough, as long as that's clearly above the echo
            thr = max(expected * ratio, self.noise_floor * 4, 0.3 * self.voice_level,
                      float(CFG.get("barge_in_min_level", 120)))
            loud = lvl > thr
            b["peak"], b["thr"] = max(b.get("peak", 0.0), lvl), thr
            # speech dips between syllables, so count 4 loud chunks out of the last 6 (~320 of 480 ms)
            b["recent"] = (b.get("recent", []) + [loud])[-6:]
            b["loud"] = sum(b["recent"])
            if CFG.get("barge_in_mode", "speech") == "loudness":
                if b["loud"] >= 4:
                    hit = True
                continue
            # Collect anything loud enough to be him and transcribe it with the tiny model in the background
            # as it grows. "speech" mode (default): real, confidently recognised words cut Friday off, while
            # noise, breathing, clatter and the fan don't. "name" mode: only a phrase with "Friday" in it.
            sp = b.setdefault("speech", [])
            if loud or (sp and b.get("gap", 0) < 8):
                sp.append(chunk)
                b["gap"] = 0 if loud else b.get("gap", 0) + 1
            if sp and b.get("gap", 0) >= 8 and not b.get("checking"):
                self._name_check(b, final=True)   # he paused: last look at the whole phrase
            elif len(sp) >= 5 and len(sp) - b.get("checked", 0) >= 4 and not b.get("checking"):
                self._name_check(b)               # still talking: look at what's there so far
            if b.get("named"):
                hit = True
        if hit:
            log.info("barge: mic %.0f, echo gain %s", lvl, f"{self.echo_gain:.3f}" if self.echo_gain else None)
            self.preroll = list(b.get("speech") or b["buf"])
        return hit

    def _name_check(self, b, final=False):
        """Transcribes the speech heard over Friday (tiny model, ~0.3 s) and flags it if it's really him
        talking: confidently recognised words (speech mode) or his name (name mode)."""
        b["checking"], b["checked"] = True, len(b["speech"])
        audio = np.concatenate(b["speech"]).astype(np.float32) / 32768.0
        by_name = CFG.get("barge_in_mode", "speech") == "name"

        def work():
            try:
                # a fan, typing or clatter is loud but not a voice: never let Whisper guess words from it
                if not has_speech(audio):
                    if final:
                        log.info("barge: ignored %.1f s of noise (no voice in it)", len(audio) / RATE)
                        b["speech"], b["gap"], b["checked"] = [], 0, 0
                    return
                if self.fast is None:
                    from faster_whisper import WhisperModel
                    self.fast = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4)
                segs, _ = self.fast.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False)
                segs = list(segs)
                text = " ".join(s.text for s in segs).strip()
                sure = [s for s in segs if s.no_speech_prob < 0.5 and s.avg_logprob > -0.9]
                words = re.findall(r"[a-zA-Z']+", " ".join(s.text for s in sure))
                echo = is_echo(text, getattr(self.voice, "saying", ""))
                real = not echo and ((len(words) >= 3 and not HALLUCINATIONS.match(text)) or bool(
                    re.search(r"\b(stop|wait|no|hold on|friday|listen|hey)\b", " ".join(words), re.I)))
                if echo:
                    log.info("barge: ignored %r (her own voice)", text)
                    b["speech"], b["gap"], b["checked"] = [], 0, 0
                    return
                if real and is_repetitive(text):
                    log.info("barge: ignored %r (repeating: made up from noise)", text)
                    real = False
                # short, clear stop words ("no no", "wait", "stop", "hold on") are trusted straight away so
                # cutting her off stays quick; anything longer gets a second opinion first
                quick_stop = len(words) <= 5 and re.search(r"\b(stop|wait|no|hold on|listen|friday)\b",
                                                           " ".join(words), re.I)
                # lenient: while she plays, Windows' echo suppression colours his voice, so a strict check
                # would throw his real interruptions away
                # his name or a stop word: lenient (Windows' echo suppression colours his voice while she plays);
                # plain talk has to really be him, or the TV and people nearby keep cutting her off
                lenient = bool(quick_stop or NAME_LOOSE.search(text))
                if (real or NAME_LOOSE.search(text)) and len(audio) >= RATE * 0.8 and not self.his_voice(audio, strict=not lenient):
                    log.info("barge: ignored %r (not his voice)", text)
                    b["speech"], b["gap"], b["checked"] = [], 0, 0
                    return
                if (real or NAME_LOOSE.search(text)) and not quick_stop and getattr(self, "ov", None) is not None:
                    # second opinion from the big model before cutting her off: on her own echo it either
                    # hears nothing or hears her actual words, both of which are filtered here
                    sure_text = self._transcribe_gpu(audio)
                    sw = re.findall(r"[a-zA-Z']+", sure_text)
                    ok = (sure_text and not is_echo(sure_text, getattr(self.voice, "saying", ""))
                          and not is_repetitive(sure_text) and not HALLUCINATIONS.match(sure_text)
                          and (len(sw) >= 2 or re.search(r"\b(stop|wait|no|friday)\b", sure_text, re.I)))
                    if not ok:
                        log.info("barge: ignored %r (big model heard %r)", text, sure_text)
                        b["speech"], b["gap"], b["checked"] = [], 0, 0
                        return
                    text = sure_text
                if NAME_LOOSE.search(text) or (real and not by_name):
                    b["named"] = True
                    log.info("barge: he said %r", text)
                elif final:
                    log.info("barge: ignored %r (%s)", text, "no 'Friday'" if by_name else "not speech")
                    b["speech"], b["gap"], b["checked"] = [], 0, 0
            except Exception:
                log.exception("barge name check failed")
            finally:
                b["checking"] = False

        threading.Thread(target=work, daemon=True).start()

    # --- audio
    def _callback(self, indata, frames, t, status):
        self.audio.put(indata[:, 0].copy())

    def _drain(self):
        while not self.audio.empty():
            try:
                self.audio.get_nowait()
            except queue.Empty:
                break

    def end_silence(self, seconds_spoken):
        """How long a silence means he's finished. He pauses 1-1.5 s mid-sentence to think, and being cut off
        there sent wrong messages ("yes ... no wait" heard as "yes"), so it waits longer than that."""
        if getattr(self, "vad", None) is not None:
            short = float(CFG.get("end_silence_vad_seconds", 1.6))
        else:
            short = float(CFG.get("end_silence_seconds", 1.6))
        return short if seconds_spoken < 6 else float(CFG.get("long_speech_silence_seconds", 1.8))

    def turn_over(self, frames, quiet_secs, spoken, state):
        """Has he finished? The fixed silence (end_silence), unless the smart end-of-turn model has heard him:
        sure he's done (>= smart_turn_sure) = end after smart_turn_fast s of silence; sure he's mid-thought
        (< smart_turn_unsure) = wait up to smart_turn_hold s. One check per pause; `state` resets when he talks."""
        base = self.end_silence(spoken)
        if quiet_secs == 0:
            state.pop("p", None)
            return False
        smart = getattr(self, "smart", None)
        fast = float(CFG.get("smart_turn_fast", 0.8))
        if smart is None or spoken < 0.6:
            return quiet_secs >= base
        if "p" not in state:
            if quiet_secs < fast:
                return False
            try:
                t0 = time.time()
                tail = np.concatenate(frames[-int(8 * RATE / CHUNK):]).astype(np.float32) / 32768.0
                state["p"] = smart.complete(tail)
                log.info("turn: %.2f sure he's done after %.1f s of silence (%d ms)", state["p"], quiet_secs,
                         (time.time() - t0) * 1000)
            except Exception:
                log.exception("smart end-of-turn failed, using the fixed silence")
                self.smart, state["p"] = None, 0.5
        p = state["p"]
        if p >= float(CFG.get("smart_turn_sure", 0.9)):
            return True
        if p < float(CFG.get("smart_turn_unsure", 0.5)):
            return quiet_secs >= max(base, float(CFG.get("smart_turn_hold", 2.5)))
        return quiet_secs >= base

    def _prime_vad(self, frames):
        """New utterance: clear the detector and give it the audio from just before speech started."""
        if getattr(self, "vad", None) is not None:
            self.vad.reset()
            for f in frames:
                self.vad.speaking(f)

    def is_quiet(self, chunk, lvl, floor):
        """Has he stopped talking in this 80 ms? The Silero detector if available (tells speech from a fan,
        typing or a car outside), else loudness."""
        if getattr(self, "vad", None) is not None:
            try:
                return not self.vad.speaking(chunk)
            except Exception:
                log.exception("VAD failed, back to loudness")
                self.vad = None
        return lvl < self.quiet_threshold(floor)

    # His mic runs Windows' own noise and echo suppression: silence reads ~3 and his voice only ~300-600,
    # so fixed thresholds miss him. These scale with how loud he actually is, learnt as he talks.
    def speech_threshold(self, floor):
        return max(floor * 3, 0.35 * self.voice_level, 90)

    def quiet_threshold(self, floor):
        return max(floor * 2.5, 0.18 * self.voice_level, 45)

    def learn_voice(self, frames):
        lv = [level_of(c) for c in frames]
        voiced = [l for l in lv if l > self.speech_threshold(self.noise_floor)]
        if len(voiced) >= 4:
            self.voice_level = 0.8 * self.voice_level + 0.2 * float(np.median(voiced))

    def record(self, max_seconds=None, wait_seconds=5, preroll=None):
        """Records one utterance (up to several minutes); returns float32 audio or None."""
        max_seconds = max_seconds or float(CFG.get("max_speech_seconds", 300))
        if preroll:
            frames, started = list(preroll), True
            self._prime_vad(frames)
        else:
            self._drain()
            frames, started = [], False
        noise, pre = [self.noise_floor] * 3, []
        quiet, t0, turn = 0, time.time(), {}
        while True:
            if self.hang_up.is_set():
                return None  # he pressed Ctrl+Alt+K: mic off now
            chunk = self.audio.get()
            lvl = level_of(chunk)
            floor = float(np.median(noise[-12:]))
            if not started:
                noise.append(lvl)
                pre = (pre + [chunk])[-4:]
                if lvl > self.speech_threshold(floor):
                    started = True
                    frames.extend(pre)
                    self._prime_vad(pre[:-1])
                elif time.time() - t0 > wait_seconds:
                    return None
                continue
            frames.append(chunk)
            quiet = quiet + 1 if self.is_quiet(chunk, lvl, floor) else 0
            spoken = len(frames) * CHUNK / RATE
            if self.turn_over(frames, quiet * CHUNK / RATE, spoken, turn) or spoken > max_seconds:
                break
        self.learn_voice(frames)
        return np.concatenate(frames).astype(np.float32) / 32768.0

    def vocabulary_prompt(self):
        """Words Whisper should expect: his list, contact names, and names from memory ("Sara Khan"),
        so people and work terms come out spelled right. His Dawat, Arabic and Urdu words (vocabulary_dawat)
        have their own share: the names list was 730 characters and cut them all off."""
        seen = {"friday", "jarvis"}

        def part(words, budget):
            out = []
            for w in words:
                w = re.sub(r"\s*\(.*?\)", "", w).strip()
                if w and w.lower() not in seen:
                    seen.add(w.lower())
                    out.append(w)
            s = ", ".join(out)
            return s if len(s) <= budget else s[:budget].rsplit(",", 1)[0]

        facts = [n for fact in MEMORY.facts() for n in re.findall(r"\b[A-Z][a-z]+(?: (?:bh|bs|bhai|[A-Z][a-z]+))+\b", fact)]
        # telling Whisper who's talking helps it with the accent and the Hindi/Gujarati words he mixes in.
        # Whisper reads at most ~220 tokens of prompt (an overlong one loses its start), so ~650 characters in all:
        # people first, then his Dawat words, then work words
        parts = [part(CFG.get("people", []) + CONTACTS.frequent(25), 260),
                 part(CFG.get("vocabulary_dawat", []), 200),
                 part(CFG.get("vocabulary", []) + sorted(set(CFG.get("contact_aliases", {}).values())) + facts, 100)]
        return ("Hey Friday. Indian English, sometimes with Hindi, Gujarati and Lisan ud-Dawat words. "
                + ", ".join(p for p in parts if p) + ".")

    def fix_heard(self, text):
        """Words the ears keep getting wrong put right (config heard_fixes, regex -> spelling, shared with the
        phone): "Ollie bh Patelwala" -> "Ali bh Patel", "Adat Tahira" -> "Raudat Tahera"."""
        for pat, right in CFG.get("heard_fixes", {}).items():
            try:
                text = re.sub(pat, lambda m, r=right: r, text, flags=re.I)
            except re.error:
                log.warning("heard_fixes: bad pattern %r", pat)
        return text

    def keep(self, text, answer=False):
        """Drops Whisper's made-up filler ("Thank you", "you") but, when she's just asked him something, keeps the
        short real answers that look like filler ("Okay.", "Yeah.", "Yes.")."""
        text, looped = collapse_repeats(text)
        if looped:  # Whisper stuck in a loop ("આવત આવત આવત" x21): mostly garbage, so treat it as not heard
            log.info("dropped a looping transcript")
            return ""
        if HALLUCINATIONS.match(text) and not (answer and ANSWERS.match(text)):
            if text.strip():
                log.info("dropped as made-up: %r", text)
            return ""
        return text

    def transcribe(self, audio, strip=True, answer=False):
        if getattr(self, "ov", None) is not None:
            try:
                text = self._transcribe_gpu(audio)
                if strip:
                    text = re.sub(r"^(hey,? )?friday[,.!]?\s*|^\W*ફ્ર\S{0,4}ડ\S{0,2}[,.!]?\s*", "", text, flags=re.I).strip() or text
                return self.keep(text, answer)
            except Exception:
                log.exception("GPU transcription failed, using the CPU model")
        segs, _ = self.stt.transcribe(
            audio, language="hi" if lsd_on() else "en", beam_size=int(CFG.get("whisper_beam", 1)), vad_filter=True,
            initial_prompt=self.vocabulary_prompt(), condition_on_previous_text=len(audio) > RATE * 30)
        # Whisper invents words from breath, fans and distant voices ("I'm seeing live", "Thank you all");
        # those segments come with a high no-speech score or a low confidence, so drop them
        kept = []
        for s in segs:
            if s.no_speech_prob > float(CFG.get("max_no_speech", 0.6)) or s.avg_logprob < float(CFG.get("min_logprob", -1.0)):
                log.info("dropped unsure words %r (no-speech %.2f, confidence %.2f)", s.text.strip(),
                         s.no_speech_prob, s.avg_logprob)
                continue
            kept.append(s.text.strip())
        text = self.fix_names(" ".join(kept).strip())
        if strip:
            text = re.sub(r"^(hey,? )?friday[,.!]?\s*|^\W*ફ્ર\S{0,4}ડ\S{0,2}[,.!]?\s*", "", text, flags=re.I).strip() or text
        return self.keep(text, answer)

    def clean_if_noisy(self, audio):
        """A loud room (a video or music playing, a fan, people talking): the noise is taken out first, so Whisper
        hears him. Quiet recordings go through untouched. Loudness of the quietest vs loudest 30 ms stretches."""
        try:
            frames = audio[: len(audio) // 480 * 480].reshape(-1, 480)
            if len(frames) < 10:
                return audio
            lv = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)) + 1e-6
            snr = 20 * np.log10(np.percentile(lv, 90) / np.percentile(lv, 15))
            if snr > 18:
                return audio
            if getattr(self, "_denoiser", None) is None:
                import sherpa_onnx
                mc = sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                    dpdfnet=sherpa_onnx.OfflineSpeechDenoiserDpdfNetModelConfig(
                        model=os.path.join(HERE, "models", "denoise", "dpdfnet_baseline.onnx")), num_threads=2)
                self._denoiser = sherpa_onnx.OfflineSpeechDenoiser(sherpa_onnx.OfflineSpeechDenoiserConfig(model=mc))
            t0 = time.time()
            out = np.array(self._denoiser(audio.astype(np.float32).tolist(), RATE).samples, dtype=np.float32)
            log.info("noisy room (speech %.0f dB over the noise): cleaned %.1fs in %.2fs", snr, len(audio) / RATE, time.time() - t0)
            return out if len(out) else audio
        except Exception:
            log.exception("noise removal failed")
            return audio

    def _ov_text(self, speech, lang="en"):
        """large-v3-turbo on the GPU in one language, with the right prompt (the racing partner of the LSD ear)."""
        cfg = self.ov.get_generation_config()
        cfg.language, cfg.task = f"<|{lang}|>", "transcribe"
        try:
            cfg.initial_prompt = (self.vocabulary_prompt() if len(speech) > RATE * 2.5 else
                                  "Hey Friday. Yes. No. Okay. Haan. Nahi. Send it. Wait. Stop. Go ahead.")
            cfg.max_new_tokens = int(min(440, 24 + 10 * len(speech) / RATE))
        except Exception:
            pass
        with self.ov_lock:
            return self.fix_names(str(self.ov.generate(speech, cfg)).strip())

    def _transcribe_gpu(self, audio):
        """large-v3-turbo via OpenVINO. It has no per-segment confidence, so a voice-activity detector
        first keeps only the stretches that are speech: fed silence or breath, it invents words."""
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        audio = self.clean_if_noisy(audio)
        stamps = get_speech_timestamps(audio, VadOptions(threshold=0.5, min_silence_duration_ms=500, speech_pad_ms=200))
        if not stamps:
            log.info("no speech in %.1fs of audio", len(audio) / RATE)
            return ""
        speech = np.concatenate([audio[s["start"]:s["end"]] for s in stamps]).astype(np.float32)
        if len(speech) < RATE * 0.15:  # a quick "yes" is barely a fifth of a second: don't throw it away
            return ""
        if lsd_on():
            # Lisan ud-Dawat: Gemini hears it (romanized as mumineen type it, so the brain and the caption read it
            # straight); if it fails, Whisper in Hindi, the nearest language it knows well (its Gujarati heard nothing)
            # gemini = the right words in ~2 s (his 17:08 test: Groq's Whisper made "shelisaano daavat" of "che,
            # lisan ud-dawat"); groq = 0.6 s but Gujarati Whisper garbles his LSD. Each falls back to the other.
            ear = CFG.get("lsd_ear", "gemini")
            if ear == "gemini":
                # his English in LSD mode waited 11-12 s on a slow Gemini: both ears start at once; Gemini (right
                # for LSD and English) wins if it's back within lsd_ear_wait s, else the local English ear (0.6 s,
                # perfect for English, gives the gist of LSD) answers, so a reply never hangs on the network
                import concurrent.futures as cf
                t0 = time.time()
                pool = cf.ThreadPoolExecutor(max_workers=2)
                g = pool.submit(gemini_transcribe, speech)
                loc = pool.submit(self._ov_text, speech, "en")
                pool.shutdown(wait=False)
                try:
                    latin, _, lang = g.result(timeout=float(CFG.get("lsd_ear_wait", 3.5)))
                    text, looped = collapse_repeats(latin)
                    if looped:
                        log.info("LSD ear (gemini) looped: treated as not heard")
                        return ""
                    self.ear_lang = "en" if lang == "en" else "lsd"
                    log.info("LSD ear (gemini, %.1fs, %s): %r", time.time() - t0, lang, text)
                    save_lsd_clip(speech, text)
                    return text
                except Exception as e:
                    why = "slow" if isinstance(e, cf.TimeoutError) else str(e)[:120]
                # past the wait: whichever ear finishes first (a late Gemini still beats an unfinished local ear)
                text = ""
                left = {loc} | ({g} if why == "slow" else set())
                while left and not text and time.time() - t0 < float(CFG.get("lsd_ear_cap", 12)):
                    done, left = cf.wait(left, timeout=max(0.1, float(CFG.get("lsd_ear_cap", 12)) - (time.time() - t0)),
                                         return_when=cf.FIRST_COMPLETED)
                    for f in done:
                        try:
                            r = f.result()
                        except Exception:
                            continue
                        if f is g and r and r[0]:
                            text, _ = collapse_repeats(r[0])
                            self.ear_lang = "en" if r[2] == "en" else "lsd"
                            log.info("LSD ear (gemini, late, %.1fs): %r", time.time() - t0, text)
                            save_lsd_clip(speech, text)
                            return text
                        if f is loc and r:
                            text = r
                if text:
                    self.ear_lang = spoken_language(text) or "en"
                    log.info("LSD ear: gemini %s, the local English ear answered (%.1fs): %r", why, time.time() - t0, text)
                    save_lsd_clip(speech, text)
                    return text
                log.warning("LSD ear: gemini %s and the local ear heard nothing: trying Groq", why)
            for ear in ((ear, "groq") if ear == "gemini" else (ear, "gemini")):
                try:
                    t0 = time.time()
                    text = gemini_transcribe(speech)[0] if ear == "gemini" else groq_transcribe(speech)
                    text, looped = collapse_repeats(text)
                    if looped:
                        log.info("LSD ear (%s) looped: treated as not heard", ear)
                        return ""
                    log.info("LSD ear (%s, %.1fs): %r", ear, time.time() - t0, text)
                    stem = save_lsd_clip(speech, text)
                    if stem and ear != "gemini" and CFG.get("lsd_ear_compare", True):
                        threading.Thread(target=_compare_gemini, args=(speech, stem), daemon=True).start()
                    return text
                except Exception as e:
                    log.warning("LSD ear (%s) failed: %s", ear, str(e)[:160])
        cfg = self.ov.get_generation_config()
        cfg.language, cfg.task = ("<|hi|>" if lsd_on() else "<|en|>"), "transcribe"
        # short replies get a plain prompt: with the long names-and-work-words list, Whisper fills unclear short
        # audio with those words ("Sam BS, VMS, VMS, UAT" for a "yes")
        vocab = (self.vocabulary_prompt() if len(speech) > RATE * 2.5 else
                 "Hey Friday. Yes. No. Okay. Haan. Nahi. Send it. Wait. Stop. Go ahead.")
        if lsd_on():  # the Hindi fallback: a Devanagari prompt with his words
            vocab = "फ्राइडे, सलाम. हा. नई. ठीक छे. बराबर. मोकली दो. खबर नई. खुदा हाफिज. इंशाअल्लाह. WhatsApp, Sara, Mum."
        try:
            cfg.initial_prompt = vocab
            if CFG.get("stt_hotwords", False):  # helps names a little but triples decode time (0.5 -> 3 s)
                cfg.hotwords = vocab.split(". ", 2)[-1]
        except Exception:
            pass
        try:  # ~3 words a second of speech is plenty; a loop can't run on for hundreds of tokens
            cfg.max_new_tokens = int(min(440, 24 + 10 * len(speech) / RATE))
        except Exception:
            pass
        with self.ov_lock:
            text = self.fix_names(str(self.ov.generate(speech, cfg)).strip())
        if lsd_on():
            save_lsd_clip(speech, text)  # his Lisan ud-Dawat, kept so the hearing can be measured and improved
        return text

    def fix_names(self, text):
        """Snaps near-misses of people he knows back to their real names: "Saira Khan" -> "Sara Khan".
        A full name is fixed when the surname matches and the first name is close."""
        text = self.fix_heard(text)
        people = (CFG.get("people", []) + CONTACTS.frequent(60)
                  + [w for w in re.findall(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", " ".join(MEMORY.facts()))])
        fulls = {p for p in people if " " in p}
        # a first name that's already a real one (his own, any contact's) is never "corrected": every "Sam
        # Shah" used to become "Yusuf Shah" because the two scored 0.33 alike
        known = {USER_NAME.lower()} | {c["name"].split()[0].lower() for c in CONTACTS.items if c.get("name")}
        for full in fulls:
            first, last = full.split(" ", 1)
            same_surname = sum(1 for f in fulls if f.split(" ", 1)[1].lower() == last.lower())
            pat = re.compile(rf"\b([A-Z][a-z]+)\s+{re.escape(last)}\b")
            for m in pat.finditer(text):
                heard = m.group(1)
                if heard == first or heard.lower() in known:
                    continue
                # the surname matched, so the first name only has to be alike ("Saara" ~ "Sara" 0.36); with
                # several people of that surname, only a close match counts
                need = 0.34 if same_surname == 1 else 0.6
                if difflib.SequenceMatcher(None, heard.lower(), first.lower()).ratio() >= need:
                    text = text.replace(m.group(0), full)
        return text

    def score_voice(self, audio):
        """Starts the voice check in the background (~0.4 s) so it runs while Whisper writes the words
        down, rather than after. Returns a Future for his_voice(), or None."""
        if getattr(self, "spk", None) is None or audio is None or len(audio) > RATE * 10:
            return None  # long stretches are mostly background talk: only score those if they name her
        fut = Future()
        def work():
            try:
                fut.set_result(self.spk.score(audio))
            except Exception as e:
                fut.set_exception(e)
        threading.Thread(target=work, daemon=True).start()
        return fut

    def his_voice(self, audio, strict=True, score=None):
        """Voice check: strict for follow-ups and interruptions (where the TV, other people and her own
        echo sneak in), lenient when someone says "Friday" so a bad voiceprint can't lock him out.
        score = the Future from score_voice() for this same audio, if it was started early."""
        spk = getattr(self, "spk", None)
        if spk is None or audio is None or not CFG.get("voice_check", True):  # voice_check false = test mode (any voice)
            return True
        s = score.result() if score is not None else spk.score(audio)
        if s is None:
            return True
        cut = spk.cutoff() - (0 if strict else 0.15)
        if s < cut:
            log.info("not his voice (%.2f < %.2f)", s, cut)
        return s >= cut

    def listen_once(self, wait_seconds=5, preroll=None):
        audio = self.record(wait_seconds=wait_seconds, preroll=preroll)
        self.last_audio, self.last_score = audio, None
        if audio is None or len(audio) < RATE * 0.3:
            return ""
        if len(audio) > RATE * 20:
            self.status("thinking", f"Writing down {len(audio) // RATE} seconds of speech…")
        else:
            self.status("thinking", "…")
        self.last_score = self.score_voice(audio)
        t0 = time.time()
        # she just asked him something: "yeah" / "okay" are real answers here, not filler
        asked = bool(self.spoken) and self.spoken[-1].rstrip().endswith("?")
        text = self.transcribe(audio, answer=asked)
        log.info("transcribed %.1fs of speech in %.1fs", len(audio) / RATE, time.time() - t0)
        return text

    def quick_text(self, audio):
        """Fast rough transcript (tiny model, ~0.3 s) for spotting "stop" / "wait"."""
        if self.fast is None:
            from faster_whisper import WhisperModel
            self.fast = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4)
        segs, _ = self.fast.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False)
        return " ".join(s.text for s in segs if s.no_speech_prob < 0.6).strip()

    def check_stop(self):
        """Call right before anything that can't be taken back (send, call, Enter): raises Stopped if he said
        stop / wait while the action was getting ready."""
        w = getattr(self, "watch", None)
        if w is not None and w.heard.is_set():
            raise Stopped(w.text)

    def run_action(self, item):
        """Runs one action with an ear open for "stop" / "wait"."""
        self.watch = StopWatch(self)
        try:
            with self.watch:
                self.do(item)
        finally:
            self.watch = None

    def stopped(self, s, item):
        """He halted an action: say so in a few words, then listen (or act on what he said instead)."""
        self.voice.stop()
        sending = item.get("a") in SEND_ACTS
        MEMORY.log("Action", f"he stopped {item.get('a')} before it finished: {s.text}")
        if item.get("a") in ("task", "phone", "resume_task") and getattr(self, "paused_task", None):
            rest = STOP_WORDS.sub(" ", strip_name(s.text or ""))
            if len(re.findall(r"[A-Za-z']+", rest)) >= 3:  # "wait, do X first": X next, the task stays paused
                self.next_text = strip_name(s.text)
                return "stopped"
            self.say("Paused.", mood="calm", barge=False)
            return "reply"
        rest = STOP_WORDS.sub(" ", strip_name(s.text or ""))
        if len(re.findall(r"[A-Za-z']+", rest)) >= 3:  # "wait, send it to Mum instead": do that next
            self.next_text = strip_name(s.text)
            self.say("Stopped, nothing went out." if sending else "Stopped.", mood="calm", barge=False)
            return "stopped"
        self.say("Okay, stopped, nothing went out. I'm listening." if sending else "Okay, stopped. I'm listening.",
                 mood="calm", barge=False)
        return "reply"

    def run_task(self, goal):
        """A job with several steps on screen, done live: read the front window as text (no screenshot),
        ask the cheap model for the next steps, do them, read again, until it's done. Waiting and moving text
        happen here for free; "stop" / "wait" halts it between any two steps."""
        import hands as H
        if not goal:
            return
        if LIMIT.blocked():
            self.say(claude_is_out())
            return
        brain = getattr(self, "hands_brain", None)
        if brain is None:
            brain = self.hands_brain = Brain(prompt=HANDS_PROMPT, model=CFG.get("hands_model", "haiku"), memory=False)
        MEMORY.log("Action", f"started a task: {goal}")
        log.info("TASK %s", goal)
        self.task_goal = goal
        win, note, t0 = H.front(), "", time.time()
        said = {}  # when she last said how it's going: he wants the result, not a commentary
        pill = progress.Tracker(goal[:1].upper() + goal[1:60], "task", getattr(self, "remote", None) is not None)
        acted = verified = stuck_asked = False  # his rule: stuck = stop and ask; done = one check, then stop
        stale = 0
        try:
            for turn in range(int(CFG.get("task_max_turns", 25))):
                self.check_stop()
                screen = H.Screen(win)
                before = screen.text()
                others = ", ".join(f'"{_short_title(w.Name)}"' for w in H.windows()[:8])
                tabs = ", ".join(f'"{_short_title(t)}"' for _, t in H.browser_tabs()[:15])
                msg = ((f"Goal: {goal}\n" if turn == 0 else "") + (f"Results: {note}\n" if note else "")
                       + f"Open windows: {others}\n" + (f"Browser tabs: {tabs}\n" if tabs else "") + before)
                replies = brain.ask(msg, f"Turn {turn + 1}.", label="Screen now")
                steps = []
                while True:
                    kind, item = replies.get(timeout=90)
                    if kind == "end":
                        break
                    if kind == "say" and item and item[0]:
                        pill.step(item[0], count=False)  # every line shows; only some are said
                        if quiet_progress(item[0], said):
                            self.say(item[0], mood=item[1] or "calm")
                    elif kind == "act":
                        steps.append(item)
                if self.interrupted:
                    raise Stopped("(he talked over her)")
                if not steps:
                    note = "You sent no steps. Send the next step, or done/fail."
                    continue
                notes, check = [], False
                for step in steps:
                    self.check_stop()
                    words = step_words(step)
                    if words:
                        pill.step(words)
                    do = step.get("do") or step.get("a")
                    if do == "done" and acted and not verified:
                        # she changed things and says it's done: one look at the screen first, then she stops
                        verified = check = True
                        log.info("TASK check: she said done (%r), one look before finishing", step.get("say"))
                        note = task_check_note(step.get("say", ""))
                        break
                    if do in ("click", "type", "paste", "key"):
                        acted = True
                    out = self.hand(step, screen, win)
                    if isinstance(out, tuple):  # ("done", None) / ("window", new window)
                        if out[0] == "done":
                            log.info("TASK done in %d turns, %.0f s", turn + 1, time.time() - t0)
                            return
                        win = out[1]
                        continue
                    if out:
                        notes.append(out)
                    if out and out.startswith("FAILED"):
                        break  # the rest of this turn's steps relied on that one
                if check:
                    time.sleep(1.5)  # let the last step land before the look
                    stale = 0
                    continue
                note = " | ".join(notes)
                # her steps changed nothing and taught her nothing, turn after turn: stop and ask him, don't loop
                stale = task_stale(before, H.Screen(win).text() if not task_informative(notes) else None, notes, stale)
                if stale >= TASK_STALE_TURNS:
                    if stuck_asked:  # she asked and it still doesn't move: pause it for him
                        return self._task_stuck(goal, note)
                    log.info("TASK stuck: %d turns with no change, asking him", stale)
                    stuck_asked, stale = True, 0
                    note = task_stuck_note(f"the last {TASK_STALE_TURNS} tries changed nothing on the screen")
            log.info("TASK stuck: out of turns")
            self._task_stuck(goal, note)
        except Declined:
            self.say("Okay, not sent.")
        except Stopped:
            self.paused_task = {"kind": "task", "goal": goal, "done": note, "t": time.time()}
            log.info("TASK paused: %s", goal)
            raise
        finally:
            progress.done(pill.id, ok=sys.exc_info()[0] is None)
            MEMORY.log("Action", f"task ended after {time.time() - t0:.0f} s")
            threading.Thread(target=brain.close, daemon=True).start()  # next task starts with a clean slate
            self.hands_brain = None

    def _task_stuck(self, goal, note):
        """Stuck for good (asked once and still nothing moves, or out of turns): pause it and ask him how to go
        on, instead of looping (his rule, 13 Oct). His reply resumes it (resume_task with his answer)."""
        q = "I'm stuck on this one, sir. How do you want me to do it?"
        self.say(q, mood="calm")
        self.paused_task = {"kind": "task", "goal": goal, "done": (note[:200] + " | stuck, asked him: " + q).strip(" |"),
                            "t": time.time()}
        MEMORY.log("Action", f"task stuck, paused it and asked him: {goal}")

    def phone_task(self, goal, kind="task", data=None):
        """Hands a job to phone Friday and talks him through it here: her progress lines are spoken on the
        laptop and her yes/no questions are asked here. "Stop" / "wait" cancels it on the phone too.
        Returns the job (its .path is a file the phone sent back, e.g. a photo)."""
        import bridge
        if not goal:
            return None
        job = bridge.add_job(goal, kind, data)
        MEMORY.log("Action", f"asked the phone to: {goal}")
        log.info("PHONE JOB %s: %s", job.id, goal)
        t0 = time.time()
        pill = progress.Tracker("On your phone: " + goal[:60], "task") if kind in ("task", "photo", "file") else None
        try:
            while not job.finished.is_set():
                self.check_stop()
                if not job.picked.is_set() and time.time() - t0 > 12:
                    job.cancelled = True
                    self.say("I can't reach your phone right now. It needs me running on it and to be on the same "
                             "Wi-Fi or hotspot as the laptop.")
                    return job
                if time.time() - t0 > float(CFG.get("phone_task_timeout", 600)):
                    job.cancelled = True
                    self.say("The phone's been at it too long, so I stopped it.")
                    return job
                try:
                    kind, text, answer = job.inbox.get(timeout=0.3)
                except queue.Empty:
                    continue
                if kind == "say":
                    if pill:
                        pill.step(text, count=False)
                    self.say(text, mood="calm")
                elif kind == "confirm":
                    try:
                        answer.put(self.confirm(text))
                    except Stopped:
                        answer.put(False)
                        raise
        except Stopped:
            job.cancelled = True
            if kind == "task":
                self.paused_task = {"kind": "phone", "goal": goal, "done": "", "t": time.time()}
            raise
        finally:
            if pill:
                progress.done(pill.id, ok=job.finished.is_set() and not job.cancelled)
        if job.result:
            self.say(job.result)
            if kind == "task" and job.result.rstrip().endswith("?"):  # the phone got stuck and asked him: his reply resumes it
                self.paused_task = {"kind": "phone", "goal": goal, "done": "stuck, asked him: " + job.result, "t": time.time()}
        if kind == "task" and job.path and os.path.isfile(job.path):
            # the phone sent something over (a note, a page's text): it opens here, and the brain knows where it is
            MEMORY.log("Action", f"from the phone: {job.path}")
            if job.path.lower().endswith(".txt"):
                subprocess.Popen(["notepad.exe", job.path])
            self.results.append(f"The phone sent this to the laptop: {job.path}")
        log.info("PHONE JOB done in %.0f s", time.time() - t0)
        return job

    def phone_quiet(self, action, timeout=45):
        """One phone action whose result comes back as text and nothing is spoken: for her own checks (a device
        report before a diagnosis). "" when the phone isn't reachable."""
        import bridge
        job = bridge.add_job(f"phone check: {action.get('a', '?')}", "action", {"action": action})
        t0 = time.time()
        while not job.finished.is_set():
            if (not job.picked.is_set() and time.time() - t0 > 12) or time.time() - t0 > timeout:
                job.cancelled = True
                return ""
            try:
                _, _, answer = job.inbox.get(timeout=0.3)
                if answer is not None:
                    answer.put(False)
            except queue.Empty:
                pass
        return job.result or ""

    def phone_file(self, act):
        """A file from his phone's storage (works while it's locked: no screen involved). Lands in Downloads,
        Friday folder; then sent on WhatsApp or opened if he asked. "list": what's new there instead."""
        data = {k: act.get(k) or "" for k in ("query", "type", "when")}
        data["list"] = bool(act.get("list"))
        job = self.phone_task(act.get("query") or "recent files", kind="file", data=data)
        path = getattr(job, "path", "") if job else ""
        if not path or not os.path.isfile(path):
            return
        MEMORY.log("Action", f"file from the phone: {os.path.basename(path)}")
        if act.get("contact"):
            self.whatsapp_send(act["contact"], act.get("message", ""), [path])
        elif act.get("open"):
            os.startfile(path)
        else:
            self.results.append(f"The file is now on the laptop at {path} (use this path to send, attach or open it).")

    def phone_photo(self, act):
        """Finds a photo on his phone by description and brings it here (Downloads, Friday folder); then sends it on
        WhatsApp from the laptop, or opens it, if he asked."""
        query = act.get("query", "")
        job = self.phone_task(query, kind="photo", data={"when": act.get("when") or "", "query": query})
        path = getattr(job, "path", "") if job else ""
        if not path or not os.path.isfile(path):
            return
        MEMORY.log("Action", f"photo from the phone: {os.path.basename(path)}")
        if act.get("contact"):
            self.whatsapp_send(act["contact"], act.get("message", ""), [path])
        elif act.get("open"):
            os.startfile(path)
        else:
            self.results.append(f"The photo is now on the laptop at {path} (use this path to send or attach it).")

    def hand(self, step, screen, win):
        """One step of a task. Returns a short note for the model, ("window", w) when the front window
        changed, or ("done", None)."""
        import hands as H
        do = step.get("do") or step.get("a")
        item = screen.get(step.get("id")) if step.get("id") is not None else None
        if step.get("id") is not None and item is None and do not in ("scroll",):
            return f"FAILED {do}: no [{step.get('id')}] on that screen"
        if step.get("confirm") or self._sends(do, step, item, win):
            preview = ""
            box = item if item is not None and item.kind == "box" else next((i for i in screen.items
                                                                             if i.kind == "box" and i.focused), None)
            if box is not None and box.value:
                words = box.value.split()
                preview = " It says: " + " ".join(words[:18]) + ("…" if len(words) > 18 else "")
            if not self.confirm((step.get("confirm") or f"Send it in {_short_title(win.Name)}?") + preview):
                raise Declined()
            self.check_stop()
        if do == "open":
            if step.get("url"):
                H.open_url(step["url"])
            else:
                app = step.get("app", "")
                w = H.find_window(app)
                if w is not None:  # already open: just bring it up
                    H.focus(w)
                    return ("window", w)
                if app.lower().replace(" ", "") == "whatsapp":
                    subprocess.Popen(["explorer.exe", "whatsapp://"])
                elif not APPS.launch(app):
                    return f"FAILED open: no app called {app}"
                for _ in range(20):  # wait for its window
                    time.sleep(0.5)
                    w = H.find_window(app)
                    if w is not None:
                        H.focus(w)
                        return ("window", w)
            return ("window", H.front())
        if do == "tab":
            w = H.switch_tab(step.get("name"))
            if w is None:
                return f"FAILED tab: no tab like {step.get('name')!r}"
            return ("window", w)
        if do == "focus":
            w = H.find_window(step.get("window"))
            if w is None:
                return f"FAILED focus: no window like {step.get('window')!r}"
            H.focus(w)
            return ("window", w)
        if do == "click":
            # never undo what he asked: a "Connect" that already worked has become "Disconnect"
            undo = UNDO.search(item.name or "")
            goal = getattr(self, "task_goal", "")
            if undo and undo.group(0).lower() not in goal.lower():
                return f'FAILED: "{item.name}" would undo what he asked (it is probably already done; check the screen)'
            H.click(item)
            time.sleep(0.6)
            return ""
        if do == "type":
            H.type_into(item, step.get("text", ""), clear=step.get("clear", True))
            if step.get("enter"):
                H.press("enter")
            return ""
        if do == "paste":
            if item is not None:
                H.click(item)
                time.sleep(0.2)
            H.press("ctrl+v")
            return ""
        if do == "copy":
            text = H.item_text(item)
            pyperclip_copy(text)
            return f"copied {len(text)} characters"
        if do == "key":
            H.press(step.get("keys", ""))
            time.sleep(0.4)
            return ""
        if do == "scroll":
            H.scroll(item, step.get("dir", "down"), step.get("amount", 5))
            time.sleep(0.25)
            return ""
        if do == "scroll_to":  # scroll until the text shows up, checked here (no AI call per scroll)
            want = (step.get("text") or "").lower()
            last = None
            for k in range(int(step.get("max", 15))):
                self.check_stop()
                s = H.Screen(win)
                if want and any(want in (i.name + " " + i.value).lower() for i in s.items):
                    return f'found "{step.get("text")}" after {k} scrolls'
                sig = s.signature()
                if sig == last:
                    return f'reached the end without finding "{step.get("text")}"'
                last = sig
                H.scroll(None, step.get("dir", "down"), 6)
                time.sleep(0.3)
            return f'didn\'t find "{step.get("text")}"'
        if do == "wait":
            if step.get("seconds") and not step.get("for"):
                time.sleep(min(float(step["seconds"]), 30))
                return ""
            ok = H.wait_until(win, step.get("for"), bool(step.get("gone")), float(step.get("timeout", 90)))
            return "" if ok else "the wait timed out"
        if do == "read":
            return f"[{item.id}] says: " + H.item_text(item)[:1500]
        if do == "look":
            return "Screen: " + look_at_screen(step.get("question", "What's on the screen?"))
        if do in ("done", "fail"):
            if step.get("say"):
                self.say(step["say"])
            return ("done", None)
        if do == "ask":
            self.say(step.get("say", "What should I do?"))
            self.status("listening", "…")
            answer = self.listen_once(wait_seconds=8)
            if not answer:  # no reply: leave it there rather than asking again and again, kept for his answer later
                self.say("Okay, I'll leave it there for now.")
                goal = getattr(self, "task_goal", "")
                if goal:
                    self.paused_task = {"kind": "task", "goal": goal, "t": time.time(),
                                        "done": "stuck, asked him: " + step.get("say", "What should I do?")}
                return ("done", None)
            return f"He answered: {answer}"
        return f"FAILED: unknown step {do}"

    @staticmethod
    def _sends(do, step, item, win):
        """True for a step that would reach a person or spend/delete something."""
        if AI_WINDOW.search(win.Name or "") and not PEOPLE_WINDOW.search(win.Name or ""):
            return False
        if do == "click" and item is not None and item.kind in ("button", "link", "menu") and SENDING.search(item.name):
            return True
        return (do == "key" and step.get("keys", "").lower() == "enter" and PEOPLE_WINDOW.search(win.Name or "")) or (
            do == "type" and step.get("enter") and PEOPLE_WINDOW.search(win.Name or ""))

    def still_talking(self):
        """Right before she answers: has he carried on talking (a pause mid-sentence, not the end)? If so,
        what he's said since is kept so not a word is lost, and the reply is held back."""
        held = []
        while True:
            try:
                held.append(self.audio.get_nowait())
            except queue.Empty:
                break
        thr = self.speech_threshold(self.noise_floor)
        loud = [i for i, c in enumerate(held) if level_of(c) > thr]
        if len(loud) < 4:
            return False
        speech = held[max(0, loud[0] - 3):]
        audio = np.concatenate(speech).astype(np.float32) / 32768.0
        if not has_speech(audio, seconds=0.5):
            return False
        # only HIS voice counts as him carrying on: not her echo, the TV, or someone else in the room
        if len(audio) >= RATE * 0.8 and not self.his_voice(audio, strict=True):
            log.info("still-talking check: a voice, but not his")
            return False
        self.preroll = speech
        return True

    def confirm(self, question):
        self.check_stop()  # he said "wait" while it was getting ready: don't even ask
        w = getattr(self, "watch", None)
        if w is not None:
            w.paused.set()  # the yes/no below reads the mic itself
        try:
            return self._confirm(question)
        finally:
            if w is not None:
                w.paused.clear()

    def _confirm(self, question):
        if self.from_panel:
            return self.panel_confirm(question)
        if getattr(self, "remote", None) is not None:
            # he's on his phone, which already asked him before handing this over (if it sends anything)
            log.info("CONFIRM (phone) %r -> %s", question, self.remote_ok)
            if not self.remote_ok:
                # the line must END WITH THE QUESTION: remote_request remembers a trailing "?" as her question, and
                # the phone brings his yes back as "APPROVED: ..." (before, "so I held off." was no question, so his
                # yes came back as a fresh request, was refused again, and she asked for a go-ahead for ever)
                q = re.sub(r"^\s*just checking,?\s*", "", question.strip(), flags=re.I)
                self.remote.append(f"That needs your go-ahead first: {q[:1].upper()}{q[1:]}")
            return self.remote_ok
        self.say(question, barge=False)
        self.status("listening", f"Friday: {question}  (yes / no)")
        chime("end")
        answer = self.listen_once(wait_seconds=6)
        log.info("CONFIRM %r -> %r", question, answer)
        MEMORY.log("He", answer or "(silence)")
        return bool(answer) and bool(YES.search(answer)) and not NO.search(answer)

    def approved(self, must_have, readback):
        """Sending gate. Passes if he just said yes to a reply in which Friday read all of must_have out
        (recipient, subject, body); otherwise reads readback out now and waits for a clear yes."""
        norm = lambda t: re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()
        said = norm(self.last_spoken)
        if must_have and must_have[0] and self.dictated(must_have[0]):
            log.info("send approved: he dictated it")
            return True
        if (must_have and all(norm(k) in said for k in must_have if k)
                and YES.search(self.heard) and not NO.search(self.heard)):
            log.info("send approved by his yes to the read-back")
            return True
        return self.confirm(readback)

    def dictated(self, message):
        """He said these words himself in the last few minutes ("now send her where are youuu"): his command is
        his yes, so it goes without a read-back."""
        return any(said_back(message, w) for t, w in getattr(self, "said_lately", []) if time.time() - t < 180)

    def agreed_to(self, *names):
        """He just said a clear yes to a question of hers that named this person or thing."""
        norm = lambda t: re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()
        said = norm(self.last_spoken)
        named = any(w in said.split() for n in names for w in norm(n).split() if len(w) > 2)
        if named and "?" in (self.last_spoken or "") and YES.search(self.heard) and not NO.search(self.heard):
            log.info("send approved by his yes to the read-back")
            return True
        return False

    # --- main loop
    def on_hotkey(self):
        """Ctrl+Alt+K: listen now, or, mid-conversation, stop listening (and talking) straight away."""
        if self.in_session and not self.paused:
            log.info("stopped listening by hotkey")
            self.hang_up.set()
            if self.voice.speaking:
                self.voice.stop()
                self.interrupted = True
            return
        if self.paused:  # "listen now" while she's off: she's back on
            self.toggle_pause()
        if self.voice.speaking:
            self.voice.stop()
            self.interrupted = True
        self.trigger.set()

    def toggle_pause(self):
        """Ctrl+Alt+J (or the tray): Friday off / on. Off = she doesn't listen and doesn't read messages out;
        reminders still ring, and the phone can still use the laptop."""
        self.paused = not self.paused
        if self.paused and self.voice.speaking:
            self.voice.stop()
            self.interrupted = True
        log.info("Friday %s by hotkey/tray", "off" if self.paused else "on")
        self.status("paused" if self.paused else "idle",
                    "Friday is off (Ctrl+Alt+J to turn her on)" if self.paused else "Friday is on again", hide_after=3)

    def run(self):
        import keyboard
        import sounddevice as sd
        keyboard.add_hotkey(CFG.get("hotkey", "ctrl+alt+k"), self.on_hotkey)
        keyboard.add_hotkey(CFG.get("pause_hotkey", "ctrl+alt+j"), self.toggle_pause)
        keyboard.add_hotkey(CFG.get("window_hotkey", "ctrl+alt+f"), open_panel)
        APPS.refresh()
        threading.Thread(target=MEMORY.build_digests, daemon=True).start()
        threading.Thread(target=sync_contacts, daemon=True).start()
        REMINDERS.seed_routines()
        threading.Thread(target=self.clock, daemon=True).start()
        threading.Thread(target=self.watch_messages, daemon=True).start()
        threading.Thread(target=self.brain.warm, daemon=True).start()
        if CFG.get("phone_bridge", True):  # Friday on his phone thinks with this laptop's brain
            import bridge
            threading.Thread(target=bridge.start, args=(sys.modules[__name__], self), daemon=True).start()
        if getattr(self, "ov", None) is not None:  # the GPU's first run compiles kernels (~1.5 s): do it now
            def warm_gpu():
                with self.ov_lock:
                    self.ov.generate(np.zeros(RATE, np.float32))
            threading.Thread(target=warm_gpu, daemon=True).start()
        self.stream = None
        if CFG.get("echo_cancel", True):
            try:
                from audio import AudioEngine
                engine = AudioEngine(self.audio.put, state_path=os.path.join(DATA, "aec.json"))
                engine.start()
                self.voice.engine = engine
                log.info("echo cancellation on")
            except Exception:
                log.exception("echo cancellation unavailable, using the plain mic")
        if not self.voice.engine:
            self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                                         callback=self._callback)
            self.stream.start()
        hour = time.localtime().tm_hour
        part = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
        self.say(random.choice([f"Good {part}, sir. Friday's online.", f"Hey, good {part}. I'm up and listening.",
                                f"Good {part}, Sam. Ready when you are."]), mood="happy")
        self.health_check()
        threading.Thread(target=self.filler, daemon=True).start()  # render the "one sec" lines now, not when needed
        self.status("idle", "", 0.1)
        anywhere = self.fast is not None
        seg = self._new_segment()
        while True:
            try:
                chunk = self.audio.get(timeout=0.5)
            except queue.Empty:
                chunk = None
            # messages and reminders wait while a phone request is running: reading one out here would listen
            # for his reply, hear the phone instead, and start a second conversation on the same brain
            if not self.announce.empty() and not self.paused and not self.remote_busy() and not (seg["on"] and len(seg["frames"]) > 3):
                self.run_announcements()  # not while he's mid-sentence
                seg = self._new_segment()
                self._drain()
                continue
            if not self.typed.empty() and not (seg["on"] and len(seg["frames"]) > 3):
                text, files = self.typed.get()
                self.run_typed(text, files)
                seg = self._new_segment()
                self._drain()
                continue
            if self.trigger.is_set():  # hotkey / tray: listen straight away
                self.trigger.clear()
                self.start_session()
                seg = self._new_segment()
                continue
            if chunk is None or self.paused or self.voice.speaking:
                # never treat Friday's own voice as his
                seg = self._new_segment() if seg["on"] else seg
                continue
            lvl = level_of(chunk)
            woke = max(self.oww.predict(chunk).values(), default=0) >= CFG.get("wake_threshold", 0.5)
            if not anywhere:
                self.noise_floor = 0.98 * self.noise_floor + 0.02 * min(lvl, 2000)
                if woke:
                    self.start_session()
                continue
            # "say Friday anywhere" mode: cut the mic into utterances, keep the ones that name him
            done = self._feed_segment(seg, chunk, lvl, woke)
            if done is None:
                continue
            frames, woke_in_seg = done
            seg = self._new_segment()
            audio = np.concatenate(frames).astype(np.float32) / 32768.0
            score = self.score_voice(audio) if len(audio) >= RATE * 0.7 else None
            text = self.addressed(audio, woke_in_seg)
            if text is None:
                continue
            if not self.his_voice(audio, strict=office_on() and CFG.get("office_strict_voice", True), score=score):

                continue  # someone else said "Friday" (at the office only his voice, strictly: colleagues say it too)
            self.learn_voice(frames)
            if self.spk is not None:
                threading.Thread(target=self.spk.learn, args=(audio,), daemon=True).start()
            self.start_session(first_text=text)
            seg = self._new_segment()

    def _new_segment(self):
        return {"on": False, "frames": [], "pre": [], "quiet": 0, "woke": False, "turn": {}}

    def _feed_segment(self, seg, chunk, lvl, woke):
        """Returns (frames, woke) when an utterance has just ended, else None."""
        floor = self.noise_floor
        if not seg["on"]:
            self.noise_floor = 0.98 * floor + 0.02 * min(lvl, 2000)
            seg["pre"] = (seg["pre"] + [chunk])[-5:]
            if lvl > self.speech_threshold(floor) or woke:
                seg.update(on=True, frames=list(seg["pre"]), quiet=0, woke=woke, turn={})
                self._prime_vad(seg["frames"])
            return None
        seg["frames"].append(chunk)
        seg["woke"] = seg["woke"] or woke
        seg["quiet"] = seg["quiet"] + 1 if self.is_quiet(chunk, lvl, floor) else 0
        secs = len(seg["frames"]) * CHUNK / RATE
        # the always-on ear: the smart check only once her name is in (woke), so room talk costs nothing
        over = (self.turn_over(seg["frames"], seg["quiet"] * CHUNK / RATE, secs, seg["turn"]) if seg["woke"]
                else seg["quiet"] * CHUNK / RATE >= self.end_silence(secs))
        if over or secs > float(CFG.get("max_speech_seconds", 300)):
            if secs < 0.7 and not seg["woke"]:  # a cough or a click
                seg.update(on=False, frames=[], quiet=0, woke=False)
                return None
            return seg["frames"], seg["woke"]
        return None

    def addressed(self, audio, woke):
        """The sentence with his name taken out if he was talking to Friday, else None. A tiny model
        screens every utterance (cheap); only ones that mention Friday get the accurate model.
        Nothing that doesn't mention Friday is kept or logged."""
        if len(audio) < RATE * 0.7 and not woke:
            return None
        # short utterances go straight to the accurate model (saves the screening step, ~0.4 s);
        # long ones are screened first so a 5-minute conversation nearby doesn't hog the CPU
        if not woke and len(audio) > RATE * 10:
            segs, _ = self.fast.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False)
            if not NAME_LOOSE.search(" ".join(s.text for s in segs)):
                return None
        full = self.transcribe(audio, strip=False)  # no status card here: most of this is background talk
        if not woke and not addresses_her(full):
            return None
        log.info("ADDRESSED %r", full)
        return strip_name(full)  # "" = he only said the name: open the mic for the actual request

    def health_check(self):
        """At start-up: is every part working? Speaks up only if something isn't, and logs the result."""
        problems, ok = [], []
        levels = []
        t_end = time.time() + 1.5
        while time.time() < t_end:
            try:
                levels.append(level_of(self.audio.get(timeout=0.5)))
            except queue.Empty:
                break
        if len(levels) < 8:
            problems.append("I'm not getting any sound from the mic")
        elif max(levels) < 1:
            problems.append("the mic seems to be muted")
        else:
            ok.append("mic")
        if getattr(self, "ov", None) is not None:
            ok.append("GPU hearing")
        else:
            problems.append("fast hearing on the graphics chip didn't load, so I'll be a bit slower to understand you")
        ok.append("speech detector") if getattr(self, "vad", None) else problems.append("the speech detector didn't load")
        ok.append("voice recognition") if getattr(self, "spk", None) else None
        for _ in range(20):  # the brain warms up in the background
            if self.brain.proc and self.brain.proc.poll() is None:
                break
            time.sleep(0.5)
        if self.brain.proc and self.brain.proc.poll() is None:
            ok.append("brain")
        else:
            err = getattr(self.brain, "start_error", "") or ""
            problems.append("my brain couldn't start because there's no internet" if "internet" in err else
                            "my brain's start command was too long for Windows" if "206" in err or "too long" in err else
                            "my brain didn't start, so check that Claude Code is signed in" if re.search(r"log ?in|auth|sign", err, re.I) or not err else
                            "my brain didn't start: " + err[:100])
        if CONTACTS.load():
            ok.append(f"{len(CONTACTS.items)} contacts")
        log.info("HEALTH ok: %s%s", ", ".join(ok), ("; problems: " + "; ".join(problems)) if problems else "")
        if problems:
            self.say("Heads up: " + ". And ".join(problems) + ".", mood="serious", barge=False)

    # --- reminders, alarms and other things Friday brings up herself
    def clock(self):
        """Background: every 15 s fire due reminders; when he unlocks or opens the laptop, fire the
        'when I open my laptop' ones. Speaking happens on the main loop (it owns the mic)."""
        was_locked = False
        fired_startup = False
        while True:
            try:
                for r in REMINDERS.due():
                    self.announce.put(("reminder", r))
                locked = session_locked()
                if (was_locked and not locked) or not fired_startup:
                    fired_startup = True
                    for r in REMINDERS.due(event="laptop_open"):
                        self.announce.put(("reminder", r))
                    self.announce.put(("unlocked", None))
                was_locked = locked
                office_auto()
                if not self.in_session:
                    self.brain.refresh()

            except Exception:
                log.exception("clock tick failed")
            time.sleep(15)

    def run_announcements(self):
        """Called from the main loop when nothing else is happening."""
        while not self.announce.empty():
            kind, item = self.announce.get()
            try:
                if kind == "reminder":
                    self.ring(item)
                elif kind == "unlocked":
                    self.on_unlock()
                elif kind == "care":  # something she noticed herself (battery, internet, space, mail, a meeting)
                    text, mood = item
                    if office_on() and CFG.get("office_cards", True):
                        self.card(text)
                        continue
                    if media_playing():
                        self.status("idle", text[:200], hide_after=15)
                        continue
                    chime("end")
                    self.say(text, mood=mood, barge=False)
                elif kind == "message":
                    if office_on() and CFG.get("office_cards", True):  # at the office: a card, never read aloud
                        who = item.get("sender", "Someone") if isinstance(item, dict) else "Someone"
                        what = item.get("text", "") if isinstance(item, dict) else str(item)
                        self.card(f"{who}: {what}", hide_after=10)
                        log.info("message held: office mode")
                        continue
                    if media_playing() and earphones() and CFG.get("read_over_media_in_earbuds", True):
                        # his rule: in earbuds she reads it straight into his ears, the video turned down meanwhile
                        log.info("message over media: in his earbuds, reading it with the media turned down")
                        with duck_media():
                            self.read_out_message(item)
                        continue
                    if media_playing():  # he's watching or listening to something: show it, don't talk over it
                        who = item.get("sender", "Someone") if isinstance(item, dict) else "Someone"
                        what = item.get("text", "") if isinstance(item, dict) else str(item)
                        self.status("idle", f"{who}: {what}"[:200], hide_after=10)
                        self.emit(who="friday", text=f"(While you were watching) {who}: {what}")
                        self.missed = getattr(self, "missed", []) + ([item] if isinstance(item, dict) else [])
                        log.info("message held: media playing")
                        continue
                    self.read_out_message(item)
            except Exception:
                log.exception("announcement failed")

    def card(self, text, hide_after=12):
        """Something she'd normally say by herself, shown instead (office mode): her window and the caption card."""
        log.info("CARD %s", text)
        MEMORY.log("Friday", f"(on screen) {text}")
        self.emit(who="friday", text=text)
        self.status("idle", text[:220], hide_after=hide_after)

    def phone_notice(self, text, title="Reminder"):
        """A notification on his phone (office mode reminders). Only if the phone app knows the 'notify' job: an older
        build would treat an unknown job as a task and try to do it."""
        import bridge
        seen = getattr(self, "phone_seen", 0)
        if "notify" not in getattr(self, "phone_caps", ()) or time.time() - seen > 600:
            log.info("phone notice not sent (phone %s): %s", "too old" if seen else "not linked", text)
            return False
        bridge.add_job(text, "notify", {"title": title, "at": time.time()})
        log.info("PHONE NOTICE %s: %s", title, text)
        return True

    def on_unlock(self):
        missed, self.missed = getattr(self, "missed", []), []
        if missed and office_on():
            who = sorted({m["sender"] for m in missed})
            self.card(f"While you were away: {len(missed)} message{'s' if len(missed) > 1 else ''} from " + ", ".join(who))
            missed = []
        if missed:
            who = sorted({m["sender"] for m in missed})
            self.say(f"While you were away, you got {len(missed)} message{'s' if len(missed) > 1 else ''}, from "
                     + (", ".join(who[:-1]) + " and " + who[-1] if len(who) > 1 else who[0]) + ". Want me to read them?",
                     mood="warm", barge=False)
            self.pending_context = "Messages he got while away (newest last): " + " | ".join(
                f"{m['sender']} on {m['app']}: {m['text']}" for m in missed[-10:])
            heard = self.listen_once(wait_seconds=6)
            if heard:
                self.in_session = True
                try:
                    self.session(heard)
                finally:
                    self.in_session = False
        self.briefing()

    def briefing(self, force=False):
        """Once a day, the first time he's at the laptop after 5 am: meetings, important email, weather,
        reminders, all in a few spoken sentences."""
        if not force and (not CFG.get("morning_briefing", True)
                          or any(r.get("name") == "brief" and not r.get("enabled", True) for r in REMINDERS.pending())):
            return
        state_path = os.path.join(DATA, "state.json")
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        today = dt.date.today().isoformat()
        b = next((r for r in REMINDERS.pending() if r.get("name") == "brief"), None)
        earliest = dt.datetime.fromisoformat(b["when"]).time() if b and b.get("when") else dt.time(5)  # his brief time
        now_t = dt.datetime.now()
        if not force and (state.get("briefed") == today or now_t.time() < earliest
                          or now_t.hour >= min(23, earliest.hour + 8)):
            return
        state["briefed"] = today
        json.dump(state, open(state_path, "w", encoding="utf-8"))
        self.status("thinking", "Putting your morning briefing together…")
        rem = REMINDERS.pending()
        todo = "; ".join(f"{r['text']} at {r['when'] or 'when he opens the laptop'}" for r in rem[:6]) or "none"
        text = ask_google(
            "Give him his morning briefing in at most five short spoken sentences, starting with a warm good "
            "morning: his meetings today (times and who), anything important or urgent in his unread Gmail from "
            "the last day (skip newsletters and promotions), and today's weather in his city (Mumbai unless his "
            f"calendar says otherwise). His reminders for today: {todo}. If something can't be reached, skip it "
            "quietly.", services=("calendar", "gmail"), extra_tools="WebSearch", timeout=180)
        MEMORY.log("Action", "morning briefing")
        self.say(text, mood="happy", barge=False)
        note = care.morning_note()
        if note:
            self.say(note, mood="calm", barge=False)

    def watch_messages(self):
        """Background: new phone messages via Phone Link, every 5 s."""
        try:
            from notify import PhoneNotifications
            phone = PhoneNotifications()
        except Exception:
            log.exception("phone notifications unavailable")
            return
        while True:
            try:
                for m in phone.new_messages():
                    # replies in his style under it in her window: also while she's off (Ctrl+Alt+J), groups if he wants
                    if m["app"] in ("WhatsApp", "WhatsApp Business") and (
                            not m["group"] or CFG.get("reply_suggest_groups", False)):
                        threading.Thread(target=self.suggest_replies, args=(m,), daemon=True).start()
                    if m["group"] and not CFG.get("announce_groups", False):
                        continue
                    if m["app"] not in CFG.get("announce_apps", ["WhatsApp", "WhatsApp Business"]):
                        log.info("message from %s on %s: not read out (only %s)", m["sender"], m["app"],
                                 "/".join(CFG.get("announce_apps", ["WhatsApp"])))
                        continue  # he wants WhatsApp read out, not Instagram, SMS or Google Chat
                    log.info("MESSAGE %s on %s", m["sender"], m["app"])
                    MEMORY.log("Action", f"message arrived from {m['sender']} on {m['app']}: {m['text']}")
                    self.announce.put(("message", m))
            except Exception:
                log.exception("message check failed")
            time.sleep(5)

    def quiet_hours(self):
        start, end = CFG.get("quiet_hours", ["23:00", "07:30"])
        now = dt.datetime.now().strftime("%H:%M")
        return (start <= now or now < end) if start > end else (start <= now < end)

    def read_out_message(self, m):
        """Reads a new message aloud and lets him answer by voice; while he's away, asleep or in quiet hours
        it's saved for the 'while you were away' summary instead."""
        if not CFG.get("announce_messages", True):
            return
        if self.sleeping or session_locked() or self.quiet_hours():
            self.missed = (getattr(self, "missed", []) + [m])[-30:]
            return
        text = m["text"] if len(m["text"]) < 180 else m["text"][:180] + "…"
        chime("end")
        self.say(f"{m['sender']} on {m['app']}: {text}", mood="warm", barge=False)
        self.pending_context = (f"He was just told this new message: {m['sender']} on {m['app']} said \"{m['text']}\". "
                                "If he says 'reply ...' or 'tell her/him ...', it's a reply to this person.")
        self.target = user32.GetForegroundWindow()
        heard = self.listen_once(wait_seconds=5)
        if heard:
            self.in_session = True
            try:
                self.session(heard)
            finally:
                self.in_session = False
        self.status("idle", "", 0.1)

    def ring(self, r):
        late = r.get("late", 0)
        when = ""
        if late > 300 and r.get("when"):
            when = f" You wanted this at {dt.datetime.fromisoformat(r['when']):%I:%M %p}."
        MEMORY.log("Action", f"{r['kind']} went off: {r['text']}")
        remark = (r.get("remark") or "").strip()
        if remark:  # his own note on it: she reads it and words (or holds) the reminder the way it asks
            self.pending_context = (f"The reminder '{r['text']}' just went off. His note on it: \"{remark}\". "
                                    "Follow that note in anything you do with it.")
            line = ask_claude(
                "You are FRIDAY, his witty assistant. A reminder of his is due. He wrote a note on it telling you how to "
                "handle it. Reply with ONE short line to say to him now, following the note, or exactly SKIP if the note "
                "says it shouldn't fire now. No quotes, no markdown.",
                f"Now: {dt.datetime.now():%A %H:%M}. Office mode: {'on' if office_on() else 'off'}. He is "
                f"{'away from the laptop' if session_locked() or bridge_idle() > 600 else 'at the laptop'}.\n"
                f"Reminder: {r['text']}{when}\nHis note: {remark}", timeout=25).strip()
            if line.upper().startswith("SKIP"):
                log.info("reminder '%s' held by his note: %s", r["text"], remark)
                return
            if line and not line.startswith(("That one needs my Claude", "My Claude")):
                r = dict(r, spoken=line)
        away = session_locked() or bridge_idle() > 600
        if (not office_on() and away and "remind" in getattr(self, "phone_caps", ())
                and time.time() - getattr(self, "phone_seen", 0) < 120):
            # he's not at the laptop: the reminder goes to his phone (rung or said there by its own rules), not to an empty room
            import bridge
            bridge.add_job(r.get("spoken") or r["text"], "remind", {"id": r["id"], "text": r["text"], "ring": r.get("ring") or "",
                                                               "ring_between": r.get("ring_between") or "", "remark": remark,
                                                               "alarm": r["kind"] == "alarm", "at": time.time()})
            log.info("REMINDER to phone (he's away): %s", r["text"])
            self.card(f"Reminder (sent to your phone): {r['text']}", hide_after=20)
            return
        if office_on():  # office: a notification on his phone and a card here, nothing spoken
            title = "Alarm" if r["kind"] == "alarm" else "Reminder"
            if CFG.get("office_phone_reminders", True):
                self.phone_notice(r["text"] + (f" (was due {dt.datetime.fromisoformat(r['when']):%I:%M %p})" if when else ""), title)
            self.card(f"{title}: {r['text']}.{when}", hide_after=20)
            return
        self.target = user32.GetForegroundWindow()
        self.in_session = True
        try:
            if r["kind"] == "alarm":
                for _ in range(int(CFG.get("alarm_repeats", 6))):
                    chime("start")
                    self.say(f"Alarm. {r['text']}.{when}", mood="excited", barge=False)
                    heard = self.listen_once(wait_seconds=6)
                    if heard:
                        self.session(heard)  # "snooze ten minutes", "okay I'm up", anything
                        return
                return
            chime("end")
            self.say(r.get("spoken") or f"Quick reminder, sir: {r['text']}.{when}", mood="warm", barge=False)
            heard = self.listen_once(wait_seconds=6)
            if heard:
                self.session(heard)
        finally:
            self.in_session = False
            self.status("idle", "", 0.1)

    def start_session(self, first_text=None):
        self.target = user32.GetForegroundWindow()
        self.in_session = True
        self.hang_up.clear()
        self.cut_off = None
        self.oww.reset()
        try:
            self.session(first_text)
        except Exception:
            log.exception("session crashed")
        finally:
            self.in_session = False
            if self.hang_up.is_set():
                self.hang_up.clear()
                chime("end")
                self.status("idle", "Stopped listening", hide_after=1.5)
        self.oww.reset()
        self._drain()

    def session(self, first_text=None):
        """One wake-up: a request, then he can keep talking without the name, and can cut in while
        Friday is speaking."""
        if first_text:
            text = first_text
        else:
            chime("start")
            self.status("listening", "Listening…")
            text = self.listen_once()
        if self.sleeping and text:
            self.sleeping = False  # he said "Friday": wake up
            self.just_woke = True
            log.info("awake")
        turns = 0
        named = True  # the first request of a session always came with his name (or the hotkey)
        while text and turns < 15 and not self.hang_up.is_set():
            if closing(strip_name(text)):
                log.info("he's done: %r", text)
                chime("end")
                break
            turns += 1
            result = self.handle(text, named=named)
            if self.hang_up.is_set():
                break
            if self.sleeping or result == "ignored":
                break  # "don't listen to me", or that wasn't for Friday: wait for his name again
            named = True
            if result == "stopped":  # he halted an action and said what to do instead
                text, self.next_text = self.next_text, None
                continue
            if result == "interrupted":
                self.status("listening", "Go on…")
                pre, self.preroll = self.preroll, None
                # the phrase that cut Friday off ("okay wait, Friday") plus whatever he says next; if it turns
                # out he said nothing (a false alarm), just listen on rather than inventing a "wait"
                raw = self.listen_once(wait_seconds=4, preroll=pre)
                # the clip that cut her off starts while she was still talking: take her own sentences back out
                # ("On it. Nothing's gone out. Yes, please send." -> "Yes, please send.")
                raw = strip_echo(raw, [getattr(self, "cut_off", "") or ""] + list(self.spoken) + [self.last_spoken or ""] + list(self.FILLERS))
                # a cut-in without her name or a stop word may well be him talking to someone else (or the room):
                # the brain gets it as "maybe not for you" and stays quiet if it isn't
                named = bool(addresses_her(raw or "") or STOP_WORDS.search(raw or "")) or bool(getattr(self, "continued_from", None))
                text = strip_name(raw)
                start, self.continued_from = getattr(self, "continued_from", None), None
                if start:  # he'd only paused: answer his whole sentence, not the first half
                    text = f"{start} {text}".strip()
                    self.pending_context = ("(He paused mid-sentence and carried on: this is his whole request. "
                                            "Ignore anything you started to say about the first half.)")
                if not text:
                    log.info("barge-in was a false alarm: nothing said")
                    self.status("listening", "Listening…")
                    text = self.listen_once(wait_seconds=float(CFG.get("follow_up_seconds", 5)))
                continue
            wait = 8 if result == "reply" else float(CFG.get("follow_up_seconds", 5))
            if wait <= 0:
                break
            self.status("listening", "Listening… (just keep talking)")
            text = self.listen_once(wait_seconds=wait)
            named = addresses_her(text or "")
            if text and not named and not self.his_voice(getattr(self, "last_audio", None),
                                                         score=getattr(self, "last_score", None)):
                break  # a follow-up in someone else's voice (TV, a visitor, her own echo)
            text = strip_name(text) if named else text
        self.status("idle", "", 0.1)

    # --- commands
    def handle(self, text, named=True):
        """Runs one utterance. Returns "interrupted", "reply" (Friday asked something), "ignored" (a
        follow-up that wasn't meant for Friday) or None. named = he used Friday's name / the hotkey."""
        t0 = time.time()
        log.info("HEARD %r%s", text, "" if named else " (no name)")
        if not self.from_panel:  # typed lines are already in her window
            self.emit(who="you", text=text, via="voice")
        MEMORY.log("He", text)
        # she answers in the language he just used (a short "ok"/"good" keeps the last one)
        heard_as, self.ear_lang = getattr(self, "ear_lang", ""), ""
        if heard_as or len(re.findall(r"\w+", text)) >= 3:
            self.spoke = heard_as or spoken_language(text) or getattr(self, "spoke", "")
        self.status("thinking", f"You: {text}")
        woke_up, self.just_woke = getattr(self, "just_woke", False), False
        self.interrupted = False
        self._barge = {}
        cut, self.cut_off = self.cut_off, None
        self.heard, self.last_spoken, self.spoken = text, " ".join(self.spoken), []
        self.said_lately = [(t, w) for t, w in getattr(self, "said_lately", []) if time.time() - t < 180][-3:] + [(time.time(), text)]
        if STOP.match(text) and not cut:
            self.voice.stop()
            return None
        if not self.from_panel and getattr(self, "remote", None) is None:
            tick()  # he knows straight away she heard him; the answer takes a second or two
        quick = quick_command(text)
        if quick:  # simple things need no AI at all: instant, and no plan usage
            spoken, do = quick
            log.info("QUICK %r", text)
            try:
                do()
            except Exception as e:
                log.exception("quick command failed")
                spoken = f"Ah, that didn't work. {str(e)[:80]}"
            MEMORY.log("Action", f"quick: {text}")
            self.say(spoken)
            return None
        wants_reply, selection, first = False, None, True
        speaking = None  # the line being played in the background

        def speak(line, mood, clip, timed):
            if timed:
                try:
                    clip.result(timeout=20)
                except Exception:
                    pass
                log.info("first audio after %.1fs", time.time() - t0)
            self.say(line, mood=mood, clip=clip)

        def settle():
            nonlocal speaking
            if speaking:
                speaking.join()
                speaking = None

        try:
            context = self.context()
            extra, self.pending_context = getattr(self, "pending_context", ""), ""
            if extra:
                context += "\n" + extra
            matches = CONTACTS.in_words(text)
            if matches:
                context += ("\nContacts in his phone that sound like names he said (best first): "
                            + "; ".join(c["name"] for c in matches)
                            + ". Use the exact contact name in actions. If more than one fits and you can't tell "
                              "which he means, ask (SAY + LISTEN).")
            if cut:
                context += (f"\nHe just cut you off while you were saying: \"{cut}\" (he heard nothing after that). "
                            "Drop that point completely. If he's stopping or correcting you (\"stop\", \"no\", \"I "
                            "didn't mean that\", \"wait\"), apologise in a few words and ask what he meant, e.g. "
                            "\"Oh, sorry! What did you mean?\", then LISTEN. Otherwise just respond to his new words. "
                            "Never repeat the cut-off answer unless he asks for it.")
            if woke_up:
                context += "\nYou were paused; he just said your name, so you're listening again. Respond normally."
            if named:
                label = "He says to you"
            else:
                label = ("Heard after your reply, WITHOUT your name (could be him following up, or him talking to "
                         "someone else, the TV, or noise). If it's clearly not meant for you, reply with nothing "
                         "at all. Otherwise answer it")
            replies = self.brain.ask(text, context, label=label)
            if getattr(self, "remote", None) is None:
                replies = prefetch_voice(replies, self.voice)
            self.results = []
            rounds = 0
            got_any = voiced = False
            said = []
            acts_done, nudged = [], False  # the promise guard: a reminder she says she set must really be set
            filler_due = time.time() + float(CFG.get("filler_after_seconds", 1.8))
            filler_said = False
            while True:
                if (first and not voiced and getattr(self, "remote", None) is None and (self.voice_on or not self.from_panel)
                        and not quiet_out()):
                    # the brain's slow today: a quick "one sec" so he isn't left wondering (said once, then wait)
                    try:
                        kind, item = replies.get(timeout=max(0.05, filler_due - time.time()))
                    except queue.Empty:
                        self.voice.say(*self.filler())
                        voiced = True
                        # the mic heard her own "one sec": throw that away, or it passes for him still talking and
                        # her answer gets dropped and asked again, round and round
                        self._drain()
                        filler_said = True
                        kind, item = replies.get(timeout=90)
                else:
                    kind, item = replies.get(timeout=90)
                if (first and kind in ("say", "act") and getattr(self, "remote", None) is None
                        and not self.from_panel and not filler_said and self.still_talking()):
                    # he paused mid-sentence and carried on: drop this reply, hear the rest, answer the whole thing
                    log.info("he's still talking: holding the reply to %r", text)
                    self.continued_from = text
                    self.interrupted = True
                    break
                if kind in ("say", "act"):
                    got_any = True
                if first and kind in ("say", "act"):
                    log.info("first response after %.1fs", time.time() - t0)
                    first = False
                if (kind == "act" and item.get("a") in INSTANT_ACTS and not self.interrupted
                        and "{selection}" not in json.dumps(item)):
                    # nothing to ask or check: run it while the line before it is still playing
                    log.info("ACT %s", json.dumps(item, ensure_ascii=False)[:400])
                    acts_done.append(item.get("a"))
                    try:
                        self.run_action(item)
                    except Stopped as s:
                        settle()
                        return self.stopped(s, item)
                    except Exception as e:
                        log.exception("action failed")
                        MEMORY.log("Action", f"failed {item.get('a')}: {e}")
                        settle()
                        self.say(f"Ah, that part didn't work. {str(e)[:120]}", state="error")
                    continue
                settle()  # everything else waits for her to finish the line (and sees if he cut in)
                if (kind == "end" and not nudged and not self.interrupted and promised_reminder(said)
                        and not REMIND_ACTS.intersection(acts_done)):
                    # she said "reminder set" but sent no remind action: nothing was saved. One nudge to do it now
                    nudged = True
                    log.info("promise guard: she said she'd set a reminder but sent no remind action: asking for it")
                    replies = prefetch_voice(self.brain.ask(
                        "You just told him you'd set a reminder, but you sent no remind action, so nothing was saved. "
                        "If he really asked for it, send the remind action now, with no SAY line unless something is unclear. If he "
                        "didn't ask for one, reply with nothing.", self.context(),
                        label="CHECK FROM FRIDAY'S OWN CODE (not him)"), self.voice)
                    continue
                if (kind == "end" and not nudged and not self.interrupted and promised_note(said)
                        and not NOTE_ACTS.intersection(acts_done)):
                    nudged = True
                    log.info("promise guard: she said she noted it but sent no feedback/note action: asking for it")
                    replies = prefetch_voice(self.brain.ask(NOTE_NUDGE, self.context(),
                                                            label="CHECK FROM FRIDAY'S OWN CODE (not him)"), self.voice)
                    continue
                if kind == "end" and self.results and not self.interrupted and rounds < 2:
                    # actions produced raw output (code, file contents, command results): hand it back to
                    # the brain to explain in plain words rather than reading it out
                    rounds += 1
                    found, self.results = "\n\n".join(self.results), []
                    replies = prefetch_voice(self.brain.ask(found, self.context(), label=(
                        "Results of the actions you just ran (he hasn't heard them). Tell him what they mean "
                        "in plain spoken words; never read code, paths or raw output aloud")), self.voice)
                    continue
                if kind == "end" or self.interrupted:
                    break  # cut off: drop the rest of the reply and listen to him straight away
                if kind == "say":
                    text_, mood, clip = (tuple(item) + (None, None))[:3] if isinstance(item, tuple) else (item, None, None)
                    if said and (text_ or "").strip().lower() == said[-1]:
                        log.info("dropped a repeated line: %r", text_)
                        continue
                    # a model stuck in a loop repeats itself ("<br>" x33): stop reading it and start fresh
                    said.append((text_ or "").strip().lower())
                    if said.count(said[-1]) >= 3 or len(said) > 15:
                        log.error("brain looping (%r x%d, %d lines): restarting it", said[-1], said.count(said[-1]), len(said))
                        self.brain.close()
                        self.say("Sorry, I glitched there. Can you ask me that again?", mood="calm")
                        wants_reply = True
                        break
                    timed = not voiced and clip is not None  # how long he waited to hear her, for the log
                    voiced = voiced or timed
                    if getattr(self, "remote", None) is None:
                        # spoken in the background so quick actions after this line don't wait for it
                        speaking = threading.Thread(target=speak, args=(text_, mood, clip, timed), daemon=True)
                        speaking.start()
                    else:
                        speak(text_, mood, clip, timed)
                elif kind == "listen":
                    wants_reply = True
                elif kind == "act":
                    log.info("ACT %s", json.dumps(item, ensure_ascii=False)[:400])
                    acts_done.append(item.get("a"))
                    blob = json.dumps(item, ensure_ascii=False)
                    if "{selection}" in blob:
                        if selection is None:
                            selection = copy_selection(self.target)
                        if not selection:
                            self.say("Wait, nothing's selected though.")
                            continue
                        item = json.loads(blob.replace("{selection}", json.dumps(selection, ensure_ascii=False)[1:-1]))
                    try:
                        self.run_action(item)
                    except Stopped as s:
                        return self.stopped(s, item)
                    except Exception as e:
                        log.exception("action failed")
                        MEMORY.log("Action", f"failed {item.get('a')}: {e}")
                        self.say(f"Ah, that part didn't work. {str(e)[:120]}", state="error")
        except queue.Empty:
            settle()
            self.brain.close()
            self.say("Okay, my brain froze on that one. Try me again?", state="error")
            return "reply"
        except Exception as e:
            settle()
            log.exception("command failed")
            chime("error")
            self.say(f"Ah, that didn't work. {str(e)[:120]}", state="error")
            return None
        if self.interrupted:
            return "interrupted"
        if not got_any:
            if not named:
                log.info("ignored: not meant for Friday")
                return "ignored"
            # he used the name, so silence is never right
            self.say("Sorry, I missed that. Say it again?")
            return "reply"
        return "reply" if wants_reply else None

    # A phone request runs on the bridge's thread while the main loop carries on. `remote` (where her words go
    # instead of the speakers) only exists on that thread: a message read out at the laptop meanwhile once went
    # to the phone, then its yes/no listen heard the phone and started a second conversation on the same brain.
    @property
    def remote(self):
        r = self.__dict__.get("_remote")
        return r if r is not None and threading.get_ident() == self.__dict__.get("_remote_ident") else None

    @remote.setter
    def remote(self, value):
        self.__dict__["_remote"] = value
        self.__dict__["_remote_ident"] = threading.get_ident() if value is not None else None

    def remote_busy(self):
        return self.__dict__.get("_remote") is not None

    class _Relay(list):
        """Her lines for the phone; each one is also handed on as it's said, so the phone can speak progress
        instead of going quiet until the whole job is done."""
        def __init__(self, on_line=None):
            super().__init__()
            self.on_line = on_line

        def append(self, text):
            super().append(text)
            if self.on_line:
                try:
                    self.on_line(text)
                except Exception:
                    log.exception("phone line not passed on")

    def remote_request(self, text, confirmed=False, on_line=None):
        """Runs something he asked phone Friday to do on this laptop. He's away from it, so nothing is spoken
        here: returns what she'd have said, plus any files to hand to the phone. A numbered list is done one task
        at a time, in order; if a task needs his answer, the rest waits and carries on after he replies."""
        if not hasattr(self, "remote_lock"):
            self.remote_lock = threading.Lock()
        if self.in_session:
            return {"text": "I'm mid-conversation at the laptop right now, give me a sec and ask again.", "files": []}
        with self.remote_lock:
            text = (text or "").strip()
            asked, self.remote_asked = getattr(self, "remote_asked", None), None
            approval = re.match(r"approved\s*[:\-]\s*", text, re.I)
            if approval:
                text = text[approval.end():]
                if not confirmed and asked and time.time() - asked["t"] < 300:
                    # her one question was "send it?", and the phone brought his yes back: don't ask twice
                    log.info("phone: approved by his yes to %r", asked["q"])
                    confirmed = True
            elif (not confirmed and asked and time.time() - asked["t"] < 300 and
                  re.search(r"\b(go.?ahead|approved?|confirm(s|ed|ation)?|yes,? (do|install|build|send|run) it|proceed|haan|ha ji)\b", text, re.I)):
                # the phone brain re-sent the job with his yes written in ("Sam confirms, go ahead") instead of the prefix
                log.info("phone: approved in his words to %r", asked["q"])
                confirmed = True
            self.remote, self.remote_ok, self.remote_files = self._Relay(on_line), bool(confirmed), []
            context = ("He's asking from his PHONE, away from the laptop: he can't see this screen or "
                       "answer questions here. Do it, then tell him the outcome in a sentence or two. "
                       "Don't LISTEN. To give him a file, use send_to_phone. If something's unclear, ask him ONE "
                       "short question and stop; his answer comes back next. " + (
                           "He has ALREADY approved this on his phone: do it now, including any sending, without "
                           "asking again." if confirmed else
                           "He has NOT approved sending anything: don't send, message or post; instead ask him in "
                           "one question (\"Send X to Y?\") and stop.")
                       + " NO NARRATION: he hears every SAY line on his phone, so no 'on it', no 'checking', no progress."
                         " Say only the outcome once it's done (or the one question), in one or two sentences.")
            pending, self.remote_list = getattr(self, "remote_list", None), None
            if pending and time.time() - pending["t"] > 900:
                pending = None  # a quarter of an hour on: he's moved on
            try:
                if pending:  # his answer to her question mid-list, then the rest of the list
                    self.pending_context = context + f" (He's answering your question about: {pending['current']})"
                    r = self.handle(text, named=True)
                    todo, done = pending["tasks"], pending["done"]
                else:
                    todo, done = split_tasks(text), 0
                    if len(todo) < 2:
                        self.pending_context = context
                        r, todo = self.handle(text, named=True), []
                    else:
                        log.info("phone: %d tasks, one by one", len(todo))
                        r = None
                total = done + len(todo)
                for i, task in enumerate(todo):
                    if r in ("reply", "interrupted", "stopped"):
                        break
                    done += 1
                    self.pending_context = context
                    current = task
                    r = self.handle(f"Task {done} of {total} from his list: {task}", named=True)
                    if r == "reply":
                        rest = todo[i + 1:]
                        self.remote_list = {"tasks": rest, "done": done, "current": current, "t": time.time()}
                        if rest:
                            self.remote.append(f"Once you answer, I'll carry on with the other {len(rest)}.")
                        break
                if r in ("interrupted", "stopped") and todo:
                    self.remote_list = None
            except Exception as e:
                log.exception("phone request failed")
                self.remote.append(f"That didn't work on the laptop: {str(e)[:120]}")
            finally:
                out = {"text": " ".join(self.remote), "files": self.remote_files}
                question = next((t for t in reversed(self.remote) if t.rstrip().endswith("?")), None)
                if question:
                    self.remote_asked = {"q": question, "t": time.time()}
                self.remote = None
                self.status("idle", "Done for the phone ✓", hide_after=3)
        return out

    def context(self):
        pt = getattr(self, "paused_task", None)
        if pt and time.time() - pt["t"] > 3 * 3600:
            pt = self.paused_task = None  # hours old: he's moved on
        paused = (f"PAUSED TASK ({(time.time() - pt['t']) / 60:.0f} min ago): {pt['goal']}"
                  + (f" | got as far as: {pt['done'][:200]}" if pt.get("done") else "") + "\n") if pt else ""
        pp = getattr(self, "phone_playing", None)
        phone = (f"HIS PHONE IS PLAYING: {pp[0]} (next to him; play/pause/next/previous/volume, 'turn off wifi' etc. "
                 f"mean the PHONE: use the phone action).\n") if pp and time.time() - pp[1] < 90 else ""
        spoke = getattr(self, "spoke", "")
        lsd = ("LANGUAGE: HE DIDN'T SPEAK LSD THIS TIME: reply in English (Latin letters), or Hindi only if his words "
               "were Hindi, even though LSD mode is on: ONE version of each line, never the same line twice or a second "
               "copy in LSD. Next time he speaks LSD, LSD again.\n") if lsd_on() and spoke != "lsd" else (
              "LANGUAGE: HE SPOKE LISAN UD-DAWAT THIS TIME: reply in LSD, every SAY line in DEVANAGARI, Arabic/Urdu "
              "words not Hindi, English for modern words, never a 'sh' sound.\n") if not lsd_on() and spoke == "lsd" else (
              "LANGUAGE: LISAN UD-DAWAT MODE IS ON. He speaks Lisan ud-Dawat (LSD); his words reach you ROMANIZED the way "
               "mumineen type (che, nai, su, kem, kidu...), sometimes misheard: read them by sound. EVERY SAY line is LSD "
               "in DEVANAGARI (never Gujarati script), spelled as spoken. LSD IS NOT GUJARATI AND NOT HINDI: Gujarati "
               "grammar, but the words are ARABIC and URDU (vaqt, zaroor, sawaal, jawab, fikar, khabar, mushkil, aasaan, "
               "khidmat, mehrbani, ijaazat, intezaar, sukran, afwan, inshallah, alhamdolillah, khuda hafiz) and ENGLISH "
               "for everything modern (message, call, office, meeting, game, word, file, printer; names and nouns stay "
               "in Latin letters). NO Sanskrit-Hindi words (समय, प्रश्न, कृपया, धन्यवाद, आभार, निश्चित, सहायता, रविवार). "
               "NO 'sh' SOUND EVER: write स, never श/ष (सु not शु, साहेब, सुक्रन, खुसी, कोसिस). Be-verbs छे/छुं/छो, "
               "negatives नई/नथी, 'समझ नई पड़ी' (never समजायुं नहीं), 'फरी सी कहो ने' (never फरी एकवार कहेशो). Days: "
               "इतवार पीर मंगल बुध जुमेरात जुम्मा सनिवार. Sound like these: 'हा सर, थई ग्यु. Sara ने message मोकली दीधो.' "
               "/ 'सु कीदु सर? फरी सी कहो ने, समझ नई पड़ी.' / 'हमणा साढ़ा चार थया छे, आजे इतवार.' / 'काले पीर छे, office साढ़े "
               "दस वागे. Rapido book करी दऊं?' / 'अरे फिकर नई सर, में देखी लईस.' / 'खबर नई सर, एक sec, check करी ने कहुं.' "
               "/ 'बस? तो खुदा हाफिज़ सर.' Short lines, same personality, 'सर'. Actions stay JSON. "
               "'English ma vaat kar' / 'back to English' = {\"a\":\"language\",\"to\":\"english\"}.\n") if lsd_on() else ""
        lr = getattr(self, "last_result", None)
        last_result = (f"LAST RESULT YOU DELIVERED ({(time.time() - lr['t']) / 60:.0f} min ago, '{lr['title']}', it's on his "
                       f"screen as a card; 'that list', 'those places', 'what you found' = THIS):\n{lr['text'][:3500]}\n"
                       ) if lr and time.time() - lr["t"] < 6 * 3600 else ""
        routines = "HIS REMINDERS AND ROUTINES NOW (he edits these; this is the truth): " + "; ".join(
            _describe(r) for r in REMINDERS.pending() if r.get("enabled", True) or r.get("name"))[:1500] + "\n"
        office = ("OFFICE MODE IS ON: he's at the office. Your words show on his screen and are NOT spoken (unless he "
                  "wears earphones), so keep them short and skip cue tags; reminders go to his phone as notifications. "
                  "\"Office mode off\" / \"I'm home\" = {\"a\":\"office\",\"on\":false}.\n") if office_on() else ""
        return (f"Now: {dt.datetime.now():%A %d %b %Y, %H:%M}. Background jobs running: "
                f"{', '.join(a.title for a in self.agents.values()) or 'none'}.\n{devices.devices_line()}\n" + paused + phone + lsd + office + last_result + routines +
                f"Open windows (front-most first):\n{describe_windows(self.target)}")

    def window_for(self, name):
        if not name:
            return self.target
        hwnd = find_window(name)
        if not hwnd:
            raise RuntimeError(f"I can't see a {name} window open")
        return hwnd

    def do(self, act):
        import wa
        a = act.get("a")
        if a == "office":
            on = act.get("on", True) not in (False, "false", "off", 0)
            # office mode silences her on both devices: ON only when he asked (a TV line "I love my office" turned
            # it on via the phone at 03:52 on 13 Oct and she was mute all night)
            if on and not OFFICE_ASK.search(self.heard or ""):
                log.info("office ignored: he didn't ask for it (%r)", self.heard)
                return
            if on and act.get("remember_wifi"):
                ssid = devices.laptop_status(max_age=5, wait=True).get("ssid")
                if ssid:
                    CFG["office_wifi"] = sorted(set(CFG.get("office_wifi", []) + [ssid]))
                    cfg = load_config()
                    cfg["office_wifi"] = CFG["office_wifi"]
                    json.dump(cfg, open(CONFIG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            office_switch(on)
            self.say("Office mode on. I'll keep quiet: words on screen, reminders on your phone." if on
                     else "Office mode off. I can talk again.", mood="calm")
            return
        if a == "language":
            to = "lsd" if re.search(r"lsd|lisan|dawat|gujarati|bohra", str(act.get("to", "")), re.I) else "en"
            set_language(to)
            MEMORY.log("Action", f"language: {to}")
            self.say("ठीक छे सर, हवे लिसान उद-दावत मा वात करिये." if to == "lsd" else "Back to English, sir.", mood="happy")
            return
        if a == "send_to_phone":
            path = os.path.expandvars(os.path.expanduser(act.get("path", "")))
            if getattr(self, "remote", None) is None:
                raise RuntimeError("sending to the phone only works when he asks from the phone")
            if not os.path.isfile(path):
                raise RuntimeError(f"I can't find {os.path.basename(path) or 'that file'}")
            if os.path.getsize(path) > 300 * 1024 * 1024:
                raise RuntimeError("that file's over 300 MB, too big to send")
            import bridge
            self.remote_files.append(bridge.offer(path))
            MEMORY.log("Action", f"sent to his phone: {path}")
            return
        if a == "type_text":
            hwnd = self.window_for(act.get("window"))
            text, enter = act.get("text", ""), bool(act.get("enter"))
            name = _title_of(hwnd or user32.GetForegroundWindow())
            if enter and (MSG_WINDOW.search(name) or (find_window("claude") == hwnd and SENDS.search(text))):
                title = type_into(text, hwnd)
                if not self.approved([text], f"This goes to {name[:40]} and says: {text}. Should I send it?"):
                    self.say("Okay, it's typed but not sent.")
                    return
                self.check_stop()
                press("enter", hwnd)
            else:
                title = type_into(text, hwnd, enter)
            MEMORY.log("Action", f"typed into '{title[:50]}'{' and pressed Enter' if act.get('enter') else ''}: "
                                 f"{act.get('text', '')}")
            self.status("idle", f"Typed into {title[:50]} ✓", hide_after=3)
        elif a == "press_keys":
            hwnd = self.window_for(act.get("window"))
            name = _title_of(hwnd or user32.GetForegroundWindow())
            if act["keys"].lower().strip() == "enter" and MSG_WINDOW.search(name):
                if not self.confirm(f"Pressing Enter in {name[:40]} sends whatever's typed there. Should I?"):
                    self.say("Okay, not sending.")
                    return
            press(act["keys"], hwnd)
            MEMORY.log("Action", f"pressed {act['keys']}")
        elif a == "click":
            self.click(act.get("target"), act.get("window"), act.get("x"), act.get("y"), bool(act.get("double")))
        elif a == "focus_window":
            if not focus_window(self.window_for(act.get("window"))):
                self.say("Windows won't let me bring that one forward, weirdly.")
        elif a == "file":
            self.status("thinking", "Reading it…")
            spoken, full = work_with_file(act.get("path", ""), act.get("request", ""))
            if full:
                self.emit(type="doc", text=full, file=re.sub(r"^[0-9a-f]{8}_", "", file_title(act.get("path", ""))))
            self.say(spoken)
        elif a == "quick_share":  # Windows' Share menu on the file; Quick Share (Google) is one of its targets
            path = os.path.expandvars(os.path.expanduser(act.get("path", "")))
            if not os.path.isfile(path):
                raise RuntimeError(f"I can't find {os.path.basename(path) or 'that file'}")
            import win32com.client  # noqa: F401  (pywin32 is in the venv for UI automation)
            shell = win32com.client.Dispatch("Shell.Application")
            item = shell.Namespace(os.path.dirname(path)).ParseName(os.path.basename(path))
            item.InvokeVerb("&Share")
            self.say("Share menu's open, pick Quick Share and your phone.")
        elif a == "open_window":
            open_panel()
        elif a == "task":
            self.paused_task = None  # a new task replaces the paused one
            self.run_task(act.get("goal", ""))
        elif a == "resume_task":
            pt, self.paused_task = getattr(self, "paused_task", None), None
            if not pt:
                self.say("There's nothing paused, sir.")
            else:
                # after she got stuck and asked: his answer is how to carry on
                answer = act.get("answer") if act.get("answer") not in (None, "", "null") else ""
                said = f" He says: {answer}" if answer else ""
                if pt["kind"] == "phone":
                    self.phone_task(pt["goal"] + f" (Resuming after a pause: check what's already done on screen and carry on.{said})")
                else:
                    self.run_task(pt["goal"] + " (Resuming after a pause: some of it is already done, check the screen and "
                                  "carry on from there." + (f" Got as far as: {pt['done'][:300]}" if pt.get("done") else "")
                                  + said + ")")
        elif a == "drop_task":
            self.paused_task = None
        elif a == "phone":
            if act.get("action"):  # one direct phone action (media, a quick setting, the torch, an app): no screen needed
                action = act["action"]
                self.phone_task(f"phone: {action.get('a', '?')} {json.dumps({k: v for k, v in action.items() if k != 'a'})}",
                                kind="action", data={"action": action})  # it says the phone's result itself
            else:  # a step-by-step job in phone apps, with the screen
                self.phone_task(act.get("goal", ""))
        elif a == "phone_photo":
            self.phone_photo(act)
        elif a == "phone_file":
            self.phone_file(act)
        elif a == "open_app":
            name = act.get("name", "")
            if name.lower().replace(" ", "") == "whatsapp":
                subprocess.Popen(["explorer.exe", "whatsapp://"])
                return
            hit = APPS.launch(name)
            if not hit:
                self.say(f"I can't find an app called {name}.")
            else:
                MEMORY.log("Action", f"opened {hit}")
        elif a == "open_url":
            webbrowser.open(act["url"])
            MEMORY.log("Action", f"opened {act['url']}")
        elif a == "search_web":
            if not act.get("open"):  # he wants the answer, not a browser: she searches and reads it herself
                self.status("thinking", f"Checking: {act.get('query', '')}")
                self.say(web_answer(act.get("query", ""), to_phone=getattr(self, "remote", None) is not None))
                return
            from urllib.parse import quote_plus
            webbrowser.open("https://www.google.com/search?q=" + quote_plus(act["query"]))
        elif a == "youtube":  # straight into the top video (it plays), not a page of results
            from urllib.parse import quote_plus
            url = "https://www.youtube.com/results?search_query=" + quote_plus(act["query"])
            try:
                import requests
                html = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36", "Accept-Language": "en"}).text
                m = re.search(r'"videoId":"([\w-]{11})"', html)
                if m:
                    url = f"https://www.youtube.com/watch?v={m.group(1)}"
            except Exception:
                pass
            webbrowser.open(url)
        elif a == "media":
            media(act.get("cmd", ""), int(act.get("times", 1) or 1))
        elif a == "wait":
            time.sleep(min(float(act.get("seconds", 1)), 30))
        elif a in ("whatsapp_send", "whatsapp_reply"):
            self.whatsapp_send(act.get("contact"), act.get("message", ""), act.get("files"))
        elif a == "whatsapp_open":
            wa.open_chat(self.alias(act["contact"]))
        elif a == "whatsapp_read":
            contact = act.get("contact")
            chat = wa.open_chat(self.alias(contact)) if contact else wa.chat_header()
            msgs = wa.read_messages(int(act.get("count") or 5))
            # to the brain, not read out word for word: it answers what he asked, in a line or two
            self.results.append(f"Latest messages in {chat} (oldest first): " + " | ".join(msgs) if msgs
                                else f"There's nothing readable in {chat}.")
        elif a == "screenshot":
            path = take_screenshot()
            MEMORY.log("Action", f"screenshot {os.path.basename(path)}")
            self.status("idle", f"Saved {os.path.basename(path)} and copied it", hide_after=3)
        elif a == "look":
            self.status("thinking", "Looking at your screen…")
            self.say(look_at_screen(act.get("question", "What's on the screen?")))
        elif a == "web_answer":
            self.status("thinking", f"Checking: {act['question']}")
            self.say(web_answer(act["question"], to_phone=getattr(self, "remote", None) is not None))
        elif a == "remember":
            if re.match(r"\W*feedback\b", act.get("fact", ""), re.I):  # "Feedback count tonight: six" (13 Oct): not a fact
                log.info("remember refused: feedback goes in the feedback action (%r)", act.get("fact", "")[:80])
                self.results.append("Not saved: feedback goes in the feedback action (one per point), never in "
                                    "remember. Send the feedback actions for anything not saved yet; say nothing about this.")
                return
            MEMORY.remember(act["fact"])
            self.status("idle", f"Saved to memory: {act['fact']}", hide_after=4)
        elif a == "forget":
            if not MEMORY.forget(act.get("about", "")):
                self.say("I didn't have anything saved about that.")
        elif a == "recall":
            self.status("thinking", "Looking back through our chats…")
            self.say(recall(act.get("question", ""), act.get("keywords", ""), act.get("days", 30)))
        elif a == "feedback":
            MEMORY.add_feedback(act["text"])
            self.status("idle", f"Feedback saved: {act['text']}", hide_after=4)
        elif a == "save_note":
            path = MEMORY.save_note(act.get("title", "Note"), act.get("text", ""))
            MEMORY.log("Action", f"saved note {os.path.basename(path)}")
            self.status("idle", f"Note saved: {os.path.basename(path)}", hide_after=4)
        elif a == "improve_self":
            self.improve_self(act.get("request", ""), act.get("device", "laptop"))
        elif a == "undo_self":
            out = subprocess.run(["git", "revert", "--no-edit", "HEAD"], cwd=HERE, capture_output=True, text=True,
                                 creationflags=NO_WINDOW)
            if out.returncode:
                self.say("Hmm, the undo didn't go through. Check the log.")
                log.error("undo failed: %s", out.stderr)
            else:
                self.say("Rolled that back. Restarting myself, back in a few seconds.")
                self.restart_self()
        elif a == "connect_google":
            service = {"email": "gmail", "mail": "gmail"}.get(act.get("service", ""), act.get("service", ""))
            if service not in google_conn.SERVICES:
                raise RuntimeError("that's not one of my Google connectors (Gmail, Calendar, Drive)")
            name = google_conn.SERVICES[service]["name"]
            if act.get("off"):
                google_conn.disconnect(service)
                self.say(f"{name}'s disconnected.")
                return
            if not google_conn.client():
                open_panel()
                self.say("First my Google app needs setting up, sir. It's in my Settings, under Connectors. Takes two minutes.")
                return
            google_conn.connect(service)
            self.say(f"Sign-in's open in your browser. Pick the account for {name} and tick the box.")
        elif a in ("calendar", "email", "drive"):

            service = {"calendar": "calendar", "email": "gmail", "drive": "drive"}[a]
            self.status("thinking", f"Checking your {'Gmail' if a == 'email' else 'Google ' + a.title()}…")
            q = act.get("request", act.get("question", ""))
            if act.get("confirmed"):
                q += " (He has confirmed: go ahead and make this change.)"
                if a == "email":
                    self.log_sent("email", "", act.get("request", ""))
            self.say(ask_google(q, services=(service,)))
        elif a == "briefing":
            self.briefing(force=True)
        elif a == "remind":
            r = REMINDERS.add(act.get("text", ""), when=act.get("when"), kind=act.get("kind", "reminder"),
                              trigger=act.get("trigger"), repeat=act.get("repeat"), remark=act.get("remark"))
            reminders_changed()
            MEMORY.log("Action", f"set {r['kind']}: {r['text']} @ {r['when'] or r['trigger']}")
            self.status("idle", f"{r['kind'].title()} set: {r['text']} ({r['when'] or 'when you open the laptop'})",
                        hide_after=5)
        elif a == "list_reminders":
            items = REMINDERS.pending()
            self.results.append("Reminders and alarms (on/off as he set them in the dashboard): " + ("; ".join(
                f"{r['kind']} '{r['text']}' at {r['when'] or r['trigger']}" + (f" ({r['repeat']})" if r.get("repeat") else "")
                + (" [on]" if r.get("enabled", True) else " [OFF]")
                + (f" [paused until {r['paused_until']}]" if r.get("paused_until") else "")
                + (f" [his note: {r['remark']}]" if r.get("remark") else "")
                for r in items) or "none"))
        elif a == "list_routines":
            self.results.append("His routines (the same list as the dashboard on his phone): " + (" | ".join(
                f"{g['section']}: " + "; ".join(
                    f"{r['text']} ({r['time'] or r['repeat']}, {r['repeat']}, {'on' if r['enabled'] else 'OFF'}"
                    f"{', on his phone' if r['runs_on'] == 'phone' else ''}"
                    f"{', paused until ' + r['paused_until'] if r.get('paused_until') else ''}"
                    f"{'; his note: ' + r['remark'] if r.get('remark') else ''})" for r in g["routines"])
                for g in REMINDERS.routines()) or "none"))
        elif a in ("edit_reminder", "pause_reminder"):
            about = act.get("about") or act.get("name") or ""
            hits = REMINDERS.find(about)
            if not hits:
                self.results.append(f"No reminder matches '{about}'.")
                return
            changed = []
            for h in hits[:4]:
                r = REMINDERS.edit(h["id"], text=act.get("text"), time_of_day=act.get("time"), when=act.get("when"),
                                   repeat=act.get("repeat"), remark=act.get("remark"), window=act.get("window"),
                                   pause_hours=act.get("hours") if a == "pause_reminder" else act.get("pause_hours"),
                                   enabled=act.get("enabled"))
                if r:
                    changed.append(r)
            reminders_changed()
            desc = "; ".join(f"'{r['text']}' now {r.get('when') or ''} {r.get('repeat') or 'once'}"
                             + (f", paused until {r['paused_until']}" if r.get("paused_until") else "")
                             + (f", note: {r['remark']}" if r.get("remark") else "") for r in changed)
            MEMORY.log("Action", f"he changed reminders by voice: {desc}")
            self.results.append(f"Changed: {desc}")
        elif a == "set_reminder_enabled":
            on = act.get("enabled", True)
            on = on if isinstance(on, bool) else str(on).strip().lower() in ("true", "1", "on", "yes")
            changed = REMINDERS.set_enabled(act.get("name") or act.get("about", ""), on)
            reminders_changed()
            if changed:
                MEMORY.log("Action", f"reminders {'on' if on else 'off'}: " + "; ".join(r["text"] for r in changed))
            self.results.append((f"Switched {'on' if on else 'off'} {len(changed)}: " + "; ".join(r["text"] for r in changed))
                                if changed else "No matching reminder found.")
        elif a == "cancel_reminder":
            gone = REMINDERS.cancel(act.get("about", ""))
            reminders_changed()
            self.results.append(f"Cancelled {len(gone)}: " + "; ".join(r["text"] for r in gone) if gone
                                else "No matching reminder found.")
        elif a == "call":
            self.call(act.get("contact", ""), act.get("via", "phone"))
        elif a == "contact_info":
            c = CONTACTS.best(self.alias(act.get("contact", "")))
            if c:
                self.results.append(f"Contact {c['name']}: numbers {', '.join(c['numbers']) or 'none'}; "
                                    f"emails {', '.join(c['emails']) or 'none'}")
            else:
                m = CONTACTS.match(act.get("contact", ""), limit=4)
                self.results.append("No single match. Closest contacts: " + "; ".join(x["name"] for x, _ in m))
        elif a == "sleep":
            # only when he really asked: a stray "let me call them" from the room once put her to sleep
            if not SLEEP_ASK.search(self.heard or ""):
                log.info("sleep ignored: he didn't ask for it (%r)", self.heard)
                return
            self.sleeping = True
            MEMORY.log("Action", "stopped listening until he says Friday")
            self.status("paused", "Not listening. Say \"Friday\" when you need me.", hide_after=5)
        elif a == "restart_self":
            self.restart_self()
        elif a == "powershell":
            cmd = act["command"]
            risky = act.get("risky") or RISKY.search(cmd)
            if risky and CFG.get("confirm_risky_commands", True):
                self.status("thinking", f"PowerShell: {cmd}")
                if not self.confirm("That one changes stuff on your PC. Should I go for it?"):
                    self.say("Cool, leaving it.")
                    return
            out = run_powershell(cmd)
            log.info("PS %s -> %s", cmd, out[:500])
            MEMORY.log("Action", f"ran PowerShell: {cmd[:200]}")
            short = out and len(out) <= 100 and "\n" not in out.strip()
            # a canned line may only be said if it carries the actual result: "the note is on the laptop" was once
            # announced while the check printed "missing"
            if act.get("say") and "{output}" in act["say"] and (short or not out):
                self.say(act["say"].replace("{output}", out.strip() if out else "nothing"))
            elif out:
                self.results.append(f"PowerShell `{cmd[:300]}` printed:\n{out[:3000]}")
        elif a == "explain":
            self.status("thinking", "Reading through it…")
            self.say(explain(act.get("question", ""), act.get("path")))
        elif a == "device":  # a report on the laptop or the phone, for the brain to explain
            dev = (act.get("device") or "laptop").lower()
            topic = act.get("topic") or "status"
            self.status("thinking", f"Checking the {dev}…")
            if dev == "phone":
                rep = self.phone_quiet({"a": "device_report", "topic": topic}, timeout=60)
                self.results.append(f"Phone report ({topic}):\n{rep}" if rep else
                                    "The phone didn't answer: it's not linked right now (Friday must be running on it, same Wi-Fi/hotspot or the tunnel).")
            else:
                self.results.append(f"Laptop report ({topic}):\n{devices.laptop_report(topic)}")
            MEMORY.log("Action", f"device report: {dev} {topic}")
        elif a == "settings":  # change a setting, or open a Settings page, on either device
            dev = (act.get("device") or "laptop").lower()
            what, value, page = act.get("what"), act.get("value"), act.get("open")
            if dev == "phone":
                if page:
                    action = {"a": "open_settings", "page": page, "app": act.get("app")}
                else:
                    action = {"a": "setting", "what": what, "value": value}
                self.phone_task(f"phone: {action['a']} {json.dumps({k: v for k, v in action.items() if k != 'a'})}",
                                kind="action", data={"action": action})
                return
            if page:
                self.status("idle", devices.open_laptop_page(page), hide_after=3)
                MEMORY.log("Action", f"opened Settings > {page}")
                return
            w = (what or "").lower().strip()
            if w in devices.LAPTOP_RISKY and CFG.get("confirm_risky_commands", True) and not self.agreed_to(w):
                if not self.confirm(f"{w.capitalize()}, {value if value is not None else 'now'}: sure?"):
                    self.say("Okay, leaving it.")
                    return
            self.check_stop()
            ok, text = devices.set_laptop(what, value)
            log.info("SETTING %s=%r -> %s %s", what, value, ok, text)
            MEMORY.log("Action", f"laptop setting {what} = {value}: {text}")
            if ok:
                self.status("idle", text, hide_after=4)
                devices.laptop_status(max_age=0)  # the context line catches up
            else:
                self.results.append(f"Setting '{what}' to {value!r}: {text}")
        elif a == "diagnose":  # evidence first, then a diagnostic agent that fixes and verifies
            dev = (act.get("device") or "laptop").lower()
            if dev not in ("laptop", "phone", "both"):
                dev = "phone" if "phone" in dev else "laptop"
            problem = (act.get("problem") or self.heard or "general check-up").strip()
            title = f"Fixing the {dev}" if dev != "both" else "Fixing both devices"

            def prepare(dev=dev, problem=problem):
                phone_rep = ""
                if dev in ("phone", "both"):
                    phone_rep = self.phone_quiet({"a": "device_report", "topic": "all"}, timeout=60)
                evidence = devices.diagnose_evidence(dev, problem, phone_rep)
                try:
                    with open(os.path.join(DATA, "fixes.md"), encoding="utf-8") as f:
                        past = f.read()[-6000:]
                except OSError:
                    past = ""
                return (f"Diagnose and fix this on his {dev}: {problem}\n\n{evidence}"
                        + (f"\n\n=== PAST FIXES (data/fixes.md) ===\n{past}" if past else ""))

            self.status("thinking", "Gathering the evidence…")
            self.start_agent(Agent(f"diagnose: {problem}", title, system=devices.DIAGNOSE_PROMPT, chrome=False,
                                   model=CFG.get("diagnose_model", "sonnet"), prepare=prepare,
                                   max_turns=int(CFG.get("diagnose_max_turns", 14))))
        elif a == "web":  # the internet: a quick spoken fact, a page, or deep research in the background
            q = (act.get("question") or act.get("query") or "").strip()
            url = act.get("url") or None
            deep = str(act.get("depth") or "quick").lower().startswith("deep")
            if not q and not url:
                return
            if not deep and not url:
                self.status("thinking", "Checking online…")
                self.say(web_answer(q, to_phone=getattr(self, "remote", None) is not None))
                return
            task = (f"Read this page and answer his question about it: {url}\nQuestion: {q or 'what is this page about, in short?'}"
                    if url else f"Research this properly for him: {q}")
            self.start_agent(Agent(task, (q or url)[:48], system=RESEARCH_PROMPT, chrome=False,
                                   model=CFG.get("web_deep_model", "sonnet")))
        elif a == "agent":
            task = act.get("task", "")
            to, subject, body = act.get("to"), act.get("subject"), act.get("body")
            if to or body or sends_to_someone(task):
                if to and body:
                    ok = self.approved([to, subject, body], f"Sending to {to}" + (f", subject {subject}" if subject
                                       else "") + f". It says: {body}. Should I send it?")
                    task += (f"\n\nSam approved sending exactly this, word for word, and nothing else. To: {to}"
                             + (f"\nSubject: {subject}" if subject else "") + f"\nBody:\n{body}")
                else:  # never read the raw task out (it's full of paths); one yes is enough
                    title = act.get("title") or "this job"
                    ok = self.agreed_to(title) or self.confirm(f"Just checking, {title[0].lower() + title[1:]}, go ahead?")
                if not ok:
                    self.say("Okay, nothing sent.")
                    return
            self.check_stop()
            self.start_agent(Agent(task, act.get("title")))
        elif a == "claude_code":
            task = act.get("task", "")
            if SENDS.search(task) and not self.approved(
                    [task], "Just checking, that Claude window would send something for you. Go ahead?"):
                self.say("Okay, nothing sent.")
                return
            if getattr(self, "remote", None) is not None:
                if re.search(r"\b(phone|android|app)\b", task, re.I) and re.search(r"\b(bug|fix|feature|code|screen|save)\b", task, re.I):
                    self.improve_self(task, "phone")
                else:
                    self.start_agent(Agent(task, act.get("title") or "Claude Code job", cwd=act.get("folder") or None))
                MEMORY.log("Action", f"ran a background Claude Code job (asked from the phone): {task}")
                return
            open_claude_code(task, act.get("folder"))
            MEMORY.log("Action", f"opened Claude Code: {act.get('task', '')}")
        elif a == "agent_status":
            if not self.agents:
                self.say("Nothing running in the background right now.")
            else:
                parts = [f"{ag.title}, {int((time.time() - ag.started) / 60)} minutes in, currently {ag.step}"
                         for ag in self.agents.values()]
                self.say("So, " + ". And ".join(parts) + ".")
        elif a == "agent_cancel":
            if not self.agents:
                self.say("There's nothing running to cancel.")
            for ag in list(self.agents.values()):
                ag.cancel()
        else:
            log.warning("unknown action %s", act)

    def improve_self(self, request, device="laptop"):
        if device == "phone" or re.search(r"\b(phone app|on (the|my) phone|android)\b", request or "", re.I):
            # her phone app: built and committed here, installed when the phone is next on the cable
            model = CFG.get("improve_model_low", "sonnet") if LIMIT.nearly_used(CFG.get("improve_low_at", 0.8)) else None
            self.start_agent(Agent(f"His request, spoken to Friday: {request}", "fixing my phone app", system=PHONE_SELF_PROMPT,
                                   cwd=ANDROID, chrome=False, model=model))
            return
        fb = MEMORY.open_feedback()
        task = (f"His request, spoken to Friday: {request}\n\n"
                + ("Open feedback items in data/feedback.md:\n" + "\n".join(f"- {f}" for f in fb) if fb else ""))
        # Opus normally; Sonnet once his plan is nearly used up, so what's left lasts longer
        model = CFG.get("improve_model_low", "sonnet") if LIMIT.nearly_used(CFG.get("improve_low_at", 0.8)) else None
        if model:
            log.info("plan %s used: self-update on %s", LIMIT.used, model)
        self.start_agent(Agent(task, "updating my own code", system=SELF_PROMPT, cwd=HERE, chrome=False,
                               model=model), then_restart=True)

    def restart_self(self):
        log.info("restart_self: restarting on purpose")
        MEMORY.log("Action", "restarted")
        restart()
        os._exit(0)

    def start_agent(self, agent, then_restart=False, to_phone=None):
        """to_phone: the result goes to his phone (he asked from there); None = wherever this request came from."""
        if not agent.task:
            return
        if to_phone is None:
            to_phone = getattr(self, "remote", None) is not None
        aid = next(self._ids)
        self.agents[aid] = agent
        log.info("AGENT START %s: %s", agent.title, agent.task[:500])
        MEMORY.log("Action", f"started background job '{agent.title}': {agent.task[:300]}")

        def work():
            agent.tracker = progress.Tracker(agent.title[:1].upper() + agent.title[1:], "agent", to_phone)
            try:
                result = agent.run()
            except Exception as e:
                log.exception("agent failed")
                result = f"Ah, the {agent.title} job fell over. {str(e)[:100]}"
                agent.tracker.ok = False
            finally:
                progress.done(agent.tracker.id, ok=agent.tracker.ok and not agent.cancelled)
            self.agents.pop(aid, None)
            if result:
                # "SAY: gist --- full write-up": the write-up goes to her window as a card, the gist is spoken
                spoken, _, full = result.partition("\n---")
                spoken = re.sub(r"^\s*SAY:\s*", "", spoken).strip() or result
                if full.strip():
                    self.emit(type="doc", text=full.strip(), file=agent.title)
                # the whole result stays with her: "save that list to my notes" must find it (she once said the
                # search was still running and went looking for it in Samsung Notes)
                self.last_result = {"title": agent.title, "text": (full.strip() or spoken), "t": time.time()}
                MEMORY.log("Action", f"result of '{agent.title}' (on his screen as a card): {(full.strip() or spoken)[:6000]}")
                log.info("AGENT DONE %s: %s", agent.title, result[:1000])
                if to_phone:
                    import bridge
                    bridge.add_job(spoken, "say")  # spoken by the phone, where he asked
                    MEMORY.log("Friday", f"[to phone] {spoken}")
                    self.emit(who="friday", text=spoken)
                else:
                    chime("end")
                    self.say(spoken, barge=False)
                self.status("idle", f"Done: {agent.title}", hide_after=6)
            if then_restart and not agent.cancelled:
                self.say("Restarting to load the new me. Back in a few seconds.", barge=False)
                self.restart_self()

        threading.Thread(target=work, daemon=True).start()

    def summarize(self, out):
        out = out.strip()
        if len(out) <= 200 and "\n" not in out:
            return out
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        return ". ".join(lines[:4])[:300]

    def alias(self, name):
        """His nickname for someone ("mom"), else the phone contact that name sounds like, else as said."""
        name = CFG.get("contact_aliases", {}).get((name or "").lower().strip(), name)
        best = CONTACTS.best(name) if name else None
        if best and best["name"].lower() != (name or "").lower():
            log.info("contact %r -> %r", name, best["name"])
        return best["name"] if best else name

    def call(self, contact, via="phone"):
        import wa
        name = self.alias(contact)
        if via == "whatsapp":
            if not self.confirm(f"WhatsApp call {name}?"):
                self.say("Okay, not calling.")
                return
            chat = wa.open_chat(name)
            self.check_stop()
            if not wa.start_call(video=False):
                self.say(f"I opened {chat}, but couldn't find the call button.")
            MEMORY.log("Action", f"WhatsApp call to {chat}")
            return
        c = CONTACTS.best(name)
        if not c or not c["numbers"]:
            self.say(f"I can't find a number for {contact}.")
            return
        number = c["numbers"][0]
        if not self.confirm(f"Call {c['name']} on your phone?"):
            self.say("Okay, not calling.")
            return
        self.check_stop()
        self.call_ok = time.time()
        os.startfile("tel:" + re.sub(r"[^\d+]", "", number))  # Phone Link places it through his phone
        MEMORY.log("Action", f"phone call to {c['name']}")
        self.status("idle", f"Calling {c['name']} via Phone Link", hide_after=5)

    def click(self, target, window=None, x=None, y=None, double=False):
        """Clicks a named button or control (UI Automation first, else found on a screenshot), or a screen
        point. Clicks that would call, send or pay need his yes."""
        target = (target or "").strip()
        hwnd = self.window_for(window)
        if hwnd:
            focus_window(hwnd)
        ctrl = None
        if x is None or y is None:
            if not target:
                self.say("What should I click?")
                return
            self.status("thinking", f"Looking for {target}…")
            ctrl, name = find_control(target, hwnd)
            if ctrl is None:
                pt = locate_on_screen(target, hwnd)
                if not pt:
                    self.say(f"I can't spot {target} on the screen.")
                    return
                x, y = pt
                name = name_at(x, y)
        else:
            name = name_at(x, y)
        where = _title_of(hwnd or user32.GetForegroundWindow())
        label = (name or target or "that spot")[:50]
        risky = CLICK_RISK.search(f"{target} {name}") or (not name and (MSG_WINDOW.search(where)
                                                                          or "phone link" in where.lower()))
        if risky:
            said_yes = "phone link" in where.lower() and time.time() - self.call_ok < 90  # the call he okayed
            if not said_yes and not self.approved(
                    [target], f"Heads up, clicking {label} in {where[:40] or 'that window'} could call or send "
                              f"something. Should I?"):
                self.say("Okay, hands off.")
                return
            if hwnd:
                focus_window(hwnd)  # the yes/no round may have moved things
        try:
            if ctrl is not None:
                try:
                    invoke = None if double else ctrl.GetInvokePattern()
                except Exception:
                    invoke = None
                if invoke:
                    invoke.Invoke()
                elif double:
                    ctrl.DoubleClick(simulateMove=False, waitTime=0.1)
                else:
                    ctrl.Click(simulateMove=False, waitTime=0.1)
            else:
                click_at(x, y, double)
        except Exception as e:
            log.exception("click failed")
            self.say(f"I found {label}, but the click didn't take. {str(e)[:80]}")
            return
        log.info("CLICK %r in %r (%s)", label, where, "UIA" if ctrl is not None else f"at {x},{y}")
        MEMORY.log("Action", f"clicked {label} in '{where[:50]}'")
        self.say(f"Done, clicked {label}.")

    def log_sent(self, channel, to, text):
        """Every message that goes out, with what led to it (his last words, her last lines, any replies she suggested):
        the nightly review learns his writing style from these, and from how he changed her drafts."""
        if not text:
            return
        rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "channel": channel, "to": to or "", "text": text,
               "dictated": bool(self.dictated(text)),
               "his_words": [w for t, w in getattr(self, "said_lately", []) if time.time() - t < 300][-3:],
               "her_lines": list(getattr(self, "spoken", []))[-3:],
               "suggested": (getattr(self, "suggested", {}).get((to or "").lower()) or {}).get("items", [])}
        their = (getattr(self, "suggested", {}).get((to or "").lower()) or {}).get("their")
        if channel == "whatsapp" and their:
            learn_style(to, their, text)
        try:
            with open(STYLE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("style log failed")

    def suggest_replies(self, m):
        """A new message arrived: two or three replies HE would send (his own style, from his_style.md and how he's
        talked to this person), shown under the message in her window. Clicking one puts it in the composer to edit
        or send. Runs in the background; costs one Haiku call."""
        if not CFG.get("reply_suggestions", True) or not isinstance(m, dict) or not m.get("text"):
            return
        sender = m.get("sender", "")
        try:
            style = open(os.path.join(DATA, "knowledge", "his_style.md"), encoding="utf-8").read()[:6000]
        except OSError:
            style = ""
        past = MEMORY.search(sender.split()[0] if sender else "", days=21)[-14:] if sender else []
        real = style_examples(sender)
        out = ask_claude(
            SUGGEST_PROMPT,
            (f"HOW HE WRITES:\n{style}\n\n" if style else "")
            + ("HOW HE REALLY REPLIES TO THEM (their line -> his reply, from his chats):\n" + "\n".join(real) + "\n\n" if real else "")
            + ("RECENT LINES ABOUT/WITH THEM:\n" + "\n".join(past) + "\n\n" if past else "")
            + f"NEW MESSAGE from {sender} on {m.get('app', 'WhatsApp')}{' (a group chat)' if m.get('group') else ''}: "
            f"{m['text']}", timeout=45)
        try:
            items = [str(x).strip() for x in json.loads(out[out.index("["):out.rindex("]") + 1]) if str(x).strip()][:3]
        except (ValueError, TypeError):
            log.info("reply suggestions: unreadable %r", (out or "")[:200])
            return
        if items:
            self.suggested = getattr(self, "suggested", {})
            self.suggested[sender.lower()] = {"items": items, "t": time.time(), "their": m["text"][:300]}
            self.emit(type="suggest", who=sender, app=m.get("app", "WhatsApp"), text=m["text"][:300], items=items)
            log.info("reply suggestions for %s: %s", sender, items)

    def whatsapp_send(self, contact, message, files=None):
        import wa
        files = [os.path.expandvars(os.path.expanduser(f)) for f in (files or []) if f]
        if message:
            self.log_sent("whatsapp", contact, message)
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            self.say(f"Hmm, I can't find {file_title(missing[0])}. Which file did you mean?")
            return
        if not message and not files:
            self.say("What should the message say?")
            return
        if contact:
            name = self.alias(contact)
            open_now = None
            try:
                open_now = wa.chat_header()
            except Exception:
                pass
            # already in that chat: skip the search (saves several seconds)
            chat = open_now if open_now and same_person(open_now, name) else wa.open_chat(name)
        else:
            chat = wa.chat_header()
            if not chat:
                self.say("No WhatsApp chat is open right now.")
                return
        if files:
            self.send_files(chat, contact, files, message)
            return
        wa.type_message(message)
        self.status("thinking", f"To {chat}: {message}")
        # he already said yes to her read-back of this message: never ask a second time
        if not self.dictated(message) and not (self.agreed_to(contact or chat, chat) and said_back(message, self.last_spoken)) and \
                not self.confirm(f"Sending to {chat}: {message}. Should I send it?"):
            import uiautomation as auto
            auto.SendKeys("{Ctrl}a{Back}", waitTime=0.05)
            self.say("Okay, binned it. Nothing sent.")
            return
        try:
            self.check_stop()
        except Stopped:
            wa.type_message("")  # clear the composer so nothing is left half-sent
            raise
        wa.press_send()
        MEMORY.log("Action", f"sent WhatsApp to {chat}: {message}")
        self.status("idle", f"Sent to {chat} ✓", hide_after=3)

    def send_files(self, chat, contact, files, message=""):
        """Sends files straight from WhatsApp Desktop (paste into the chat, then Send): about ten seconds.
        One yes is enough: if he just agreed to her read-back, it isn't asked again."""
        import wa
        titles = [file_title(f) for f in files]
        what = titles[0] if len(titles) == 1 else f"{len(titles)} files: " + ", ".join(titles[:-1]) + " and " + titles[-1]
        self.status("thinking", f"To {chat}: {what}")
        if not self.agreed_to(contact or chat) and not self.confirm(
                f"Send {what} to {chat}" + (f", with the message: {message}" if message else "") + "?"):
            self.say("Okay, nothing sent.")
            return
        self.check_stop()
        btn = wa.attach_files(files)
        try:
            self.check_stop()
        except Stopped:
            wa.discard_attachment()
            raise
        wa.send_attachment(btn)
        if message:
            wa.type_message(message)
            wa.press_send()
        MEMORY.log("Action", f"sent WhatsApp to {chat}: {', '.join(titles)}" + (f" + {message}" if message else ""))
        self.status("idle", f"Sent to {chat} ✓", hide_after=3)
        self.say("Sent!" if len(files) == 1 else f"All {len(files)} sent!")


# ---------------------------------------------------------------- entry points

def open_panel():
    """Her window: panel.html served by the bridge, shown as a clean Edge app window (its own small profile, so it
    doesn't mix with his browsing). If it's already open it just comes to the front."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _title_of(hwnd) == "Friday":
            found.append(hwnd)
        return True
    user32.EnumWindows(each, 0)
    if found:
        user32.ShowWindow(found[0], 9)
        user32.keybd_event(0x87, 0, 0, 0)  # F24: lets a background process bring a window forward
        user32.keybd_event(0x87, 0, 2, 0)
        user32.SetForegroundWindow(found[0])
        return
    # Chrome, not Edge: Edge signs a new profile into his Microsoft account and starts syncing it
    browser = os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe")
    if not os.path.exists(browser):
        browser = os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe")
    sw = user32.GetSystemMetrics(0)
    port = int(CFG.get("bridge_port", 8765))
    subprocess.Popen([browser, f"--app=http://127.0.0.1:{port}/panel", f"--user-data-dir={os.path.join(DATA, 'panel_profile')}",
                      "--window-size=1120,760", f"--window-position={max(0, sw - 1160)},40", "--no-first-run",
                      "--no-default-browser-check", "--disable-sync", "--disable-features=Translate,msImplicitSignin"],
                     creationflags=NO_WINDOW)


def keep_awake():
    """The laptop doesn't sleep by itself while she runs, so his phone can reach her from anywhere (12 Oct: on battery
    it went into Modern Standby after 15 idle minutes, 13:55-14:05, and the phone lost her). A system-required request,
    like a download would make: the screen still turns off, and closing the lid or the power button still sleeps it.
    Let go on battery under keep_awake_min_battery % (default 25) so it can sleep before it dies. Config keep_awake."""
    import psutil
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    held = None
    while True:
        try:
            b = psutil.sensors_battery()
            want = CFG.get("keep_awake", True) and (
                b is None or b.power_plugged or b.percent >= CFG.get("keep_awake_min_battery", 25))
            if want != held:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))
                log.info("keep awake: %s", "on (the phone can reach me)" if want else
                         f"off (battery {b.percent if b else '?'}%, it may sleep)")
                held = want
        except Exception:
            log.exception("keep awake failed")
        time.sleep(60)


def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\JarvisVoiceAssistant")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def restart():
    """Starts a fresh copy once this one has exited (the single-instance mutex would block it sooner)."""
    pyw = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                      f"Start-Sleep 3; Start-Process '{pyw}' '\"{os.path.join(HERE, 'friday.py')}\"'"],
                     creationflags=NO_WINDOW)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # full-resolution screenshots at 125% scaling
    except Exception:
        pass
    args = sys.argv[1:]
    if args[:1] == ["--say"]:
        Voice().say(" ".join(args[1:]))
        return
    if args[:1] == ["--text"]:
        j = Jarvis(use_mic=False)
        j.target = user32.GetForegroundWindow()
        j.confirm = lambda q: (print("CONFIRM?", q), input("yes/no> ").lower().startswith("y"))[1]
        APPS.refresh()
        j.handle(" ".join(args[1:]))
        while j.agents:
            time.sleep(1)
        j.brain.close()
        return
    if not single_instance():
        return
    stopped = os.path.join(DATA, "stopped")  # the watchdog leaves her alone while this exists
    if os.path.exists(stopped):
        os.remove(stopped)
    import pystray
    overlay = Overlay()
    j = Jarvis(overlay=overlay)
    threading.Thread(target=devices.laptop_status, kwargs={"wait": True}, daemon=True, name="laptop-status").start()
    global friday
    friday = j
    care.start(sys.modules[__name__])
    threading.Thread(target=keep_awake, daemon=True, name="keep-awake").start()

    def toggle_pause(icon, item):
        j.toggle_pause()

    def toggle_office(icon, item):
        office_switch(not office_on())
        j.status("idle", "Office mode on: I'll stay quiet" if office_on() else "Office mode off", hide_after=4)

    def shutdown(icon, again):
        if not again:  # quit on purpose: tell the watchdog not to bring her back
            open(stopped, "w").close()
        for ag in list(j.agents.values()):
            ag.cancel()
        j.brain.close()
        if again:
            restart()
        icon.stop()
        overlay.quit()
        os._exit(0)

    # pystray only accepts callbacks taking exactly (icon, item)
    def quit_all(icon, item):
        shutdown(icon, False)

    def restart_all(icon, item):
        shutdown(icon, True)

    def open_path(p):
        if os.path.exists(p):
            os.startfile(p)

    menu = pystray.Menu(
        pystray.MenuItem("Open Friday (Ctrl+Alt+F)", lambda i, it: open_panel(), default=True),
        pystray.MenuItem("Listen now (Ctrl+Alt+K)", lambda i, it: j.on_hotkey()),
        pystray.MenuItem("Friday off (Ctrl+Alt+J)", toggle_pause, checked=lambda it: j.paused),
        pystray.MenuItem("Office mode (no sound)", toggle_office, checked=lambda it: office_on()),

        pystray.MenuItem("Memory", lambda i, it: open_path(MEMORY.memory_path)),
        pystray.MenuItem("Feedback list", lambda i, it: open_path(MEMORY.feedback_path)),
        pystray.MenuItem("Notes folder", lambda i, it: open_path(os.path.join(DATA, "notes"))),
        pystray.MenuItem("Open log", lambda i, it: open_path(LOG_PATH)),
        pystray.MenuItem("Edit settings", lambda i, it: open_path(CONFIG_PATH)),
        pystray.MenuItem("Restart Friday", restart_all),
        pystray.MenuItem("Quit Friday", quit_all),
    )
    j.tray = pystray.Icon("friday", make_icon("#3b82f6"), "Friday", menu)
    j.tray.run_detached()
    threading.Thread(target=j.run, daemon=True).start()
    overlay.run()


if __name__ == "__main__":
    # pythonw has no console, so anything that kills Friday has to end up in the log
    sys.excepthook = lambda *e: log.critical("crash", exc_info=e)
    threading.excepthook = lambda a: log.critical("thread crash", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info("starting (pid %s, %s)", os.getpid(), " ".join(sys.argv[1:]) or "tray mode")
    try:
        main()
    except Exception:
        log.exception("startup failed")
        raise
