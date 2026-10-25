"""Friday's hands on the laptop: reads any window as text through Windows UI Automation (the names and
roles screen readers use, so no screenshots and few tokens) and does one step at a time: open, focus,
click, type, press, scroll, wait, copy, read. Waiting ("until ChatGPT has finished answering") happens
here on the laptop, so it costs no AI calls; copied text goes clipboard to clipboard, never through the AI."""
import re
import time
import webbrowser

import pyperclip
import uiautomation as auto
from uiautomation import uiautomation as _U

_client = _U._AutomationClient.instance()
UIA = _client.IUIAutomation
NAME, TYPE, RECT, OFFSCREEN, VALUE, FOCUSED, ENABLED = 30005, 30003, 30001, 30022, 30045, 30008, 30010
DESCENDANTS = 4
_req = UIA.CreateCacheRequest()
for _p in (NAME, TYPE, RECT, VALUE, FOCUSED, ENABLED):
    _req.AddProperty(_p)
_visible = UIA.CreatePropertyCondition(OFFSCREEN, False)

KINDS = {"Button": "button", "Edit": "box", "Hyperlink": "link", "MenuItem": "menu", "TabItem": "tab",
         "ListItem": "item", "CheckBox": "checkbox", "ComboBox": "dropdown", "RadioButton": "option",
         "DataItem": "row", "TreeItem": "item", "Text": "text", "Document": "page", "SplitButton": "button"}
WINDOW_BUTTONS = {"Minimise", "Minimize", "Maximise", "Maximize", "Restore", "Close"}
MAX_LINES = 150
TICKING = re.compile(r"\d+:\d{2}|\d+\s?%|\d+\s?(s|sec|min|mins|h|hr)\b|left$|remaining", re.I)


class Item:
    __slots__ = ("id", "kind", "name", "value", "rect", "focused", "el")

    def __init__(self, i, kind, name, value, rect, focused, el):
        self.id, self.kind, self.name, self.value, self.rect, self.focused, self.el = i, kind, name, value, rect, focused, el

    def line(self):
        s = f"[{self.id}] {self.kind} \"{_short(self.name)}\""
        if self.value and self.value != self.name:
            s += f" = \"{_short(self.value, 60)}\""
        return s + (" (focused)" if self.focused else "")


def _short(t, n=80):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def windows():
    """Titles of the open app windows, front first."""
    out = []
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.Name and w.ControlTypeName == "WindowControl" and not w.IsOffscreen:
                out.append(w)
        except Exception:
            pass
    return out


def find_window(title):
    t = (title or "").lower()
    for w in windows():
        if t and t in w.Name.lower():
            return w
    return None


def focus(win):
    import ctypes
    hwnd = win.NativeWindowHandle
    if auto.IsIconic(hwnd):
        auto.ShowWindow(hwnd, auto.SW.Restore)
    ctypes.windll.user32.keybd_event(0x87, 0, 0, 0)  # F24: lets a background process take the foreground
    ctypes.windll.user32.keybd_event(0x87, 0, 2, 0)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def browser_tabs():
    """[(window, tab title)] for every tab in the open Chrome/Edge windows (only the active one shows in titles)."""
    out = []
    for w in windows():
        if w.ClassName != "Chrome_WidgetWin_1":
            continue
        try:
            arr = w.Element.FindAllBuildCache(DESCENDANTS, UIA.CreatePropertyCondition(TYPE, 50019), _req)  # TabItem
            for k in range(arr.Length):
                name = (arr.GetElement(k).CachedName or "").strip()
                if name:
                    out.append((w, re.sub(r"\s+-\s+Memory usage.*$", "", name)))
        except Exception:
            pass
    return out


def switch_tab(title):
    """Brings the browser tab whose title contains title to the front. Returns its window or None."""
    t = (title or "").lower()
    for w, name in browser_tabs():
        if t and t in name.lower():
            focus(w)
            s = Screen(w)
            tab = next((i for i in s.items if i.kind == "tab" and t in i.name.lower()), None)
            if tab:
                click(tab)
                time.sleep(0.6)
            return w
    return None


def front():
    return auto.ControlFromHandle(auto.GetForegroundWindow())


class Screen:
    """One reading of a window: numbered things he could click, type into or read."""

    def __init__(self, win=None):
        self.win = win or front()
        self.items = []
        try:
            arr = self.win.Element.FindAllBuildCache(DESCENDANTS, _visible, _req)
            if arr.Length < 25 and self.win.ClassName == "Chrome_WidgetWin_1":
                # Chrome only builds a page's tree once something asks for it: the first read is nearly empty
                time.sleep(0.5)
                arr = self.win.Element.FindAllBuildCache(DESCENDANTS, _visible, _req)
        except Exception:
            arr = None
        seen = set()
        for k in range(arr.Length if arr else 0):
            e = arr.GetElement(k)
            try:
                kind = KINDS.get(auto.ControlTypeNames.get(e.CachedControlType, "").replace("Control", ""))
                name = (e.CachedName or "").strip()
                r = e.CachedBoundingRectangle
            except Exception:
                continue
            if not kind or (r.right - r.left) <= 0 or (r.bottom - r.top) <= 0:
                continue
            if kind == "button" and name in WINDOW_BUTTONS:
                continue
            value = ""
            if kind in ("box", "dropdown"):
                try:
                    value = (e.GetCachedPropertyValue(VALUE) or "").strip()
                except Exception:
                    pass
            if kind == "text" and (not name or (name, r.top) in seen):
                continue
            seen.add((name, r.top))
            try:
                focused = bool(e.CachedHasKeyboardFocus)
            except Exception:
                focused = False
            self.items.append(Item(len(self.items) + 1, kind, name, value, (r.left, r.top, r.right, r.bottom),
                                   focused, e))

    def text(self):
        lines = [i.line() for i in self.items]
        if len(lines) > MAX_LINES:  # long pages: keep the top and the latest part (where new content appears)
            lines = lines[:60] + [f"... {len(lines) - MAX_LINES} more lines ..."] + lines[-(MAX_LINES - 60):]
        return f'Window: "{self.win.Name}"\n' + "\n".join(lines)

    def signature(self):
        """What's on screen, minus things that tick on their own (video times, countdowns), which never settle."""
        return "\n".join(f"{i.kind}{i.name}{i.value}" for i in self.items
                         if not (i.kind == "text" and TICKING.search(i.name)))

    def get(self, ref):
        """An item by its number, else by (part of) its name."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            n = int(ref)
            return self.items[n - 1] if 0 < n <= len(self.items) else None
        r = (ref or "").lower()
        exact = [i for i in self.items if i.name.lower() == r]
        return (exact or [i for i in self.items if r and r in i.name.lower()] or [None])[-1]


def control(item):
    return auto.Control.CreateControlFromElement(item.el)


def click(item):
    """Invoke without the mouse when the control allows it (overlays can't get in the way), else a click."""
    c = control(item)
    for get in ("GetInvokePattern", "GetSelectionItemPattern", "GetTogglePattern", "GetExpandCollapsePattern"):
        try:
            p = getattr(c, get)()
            if p is None:
                continue
            {"GetInvokePattern": lambda: p.Invoke(), "GetSelectionItemPattern": lambda: p.Select(),
             "GetTogglePattern": lambda: p.Toggle(), "GetExpandCollapsePattern": lambda: p.Expand()}[get]()
            return
        except Exception:
            continue
    x1, y1, x2, y2 = item.rect
    auto.Click((x1 + x2) // 2, (y1 + y2) // 2)


def type_into(item, text, clear=True):
    """Puts text in a box by pasting (fast, any language), then gives his clipboard back."""
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    if item is not None:
        c = control(item)
        try:
            c.SetFocus()
        except Exception:
            click(item)
        time.sleep(0.1)
    if clear:
        auto.SendKeys("{Ctrl}a{Back}", waitTime=0.05)
    pyperclip.copy(text)
    auto.SendKeys("{Ctrl}v", waitTime=0.3)
    if old is not None:
        pyperclip.copy(old)


def item_text(item):
    """All the words inside a control (a whole message or answer)."""
    c = control(item)
    try:
        t = c.GetTextPattern().DocumentRange.GetText(-1)
        if t and t.strip():
            return t.strip()
    except Exception:
        pass
    parts = []
    try:
        arr = item.el.FindAllBuildCache(DESCENDANTS, _visible, _req)
        for k in range(arr.Length):
            n = (arr.GetElement(k).CachedName or "").strip()
            if n and (not parts or parts[-1] != n):
                parts.append(n)
    except Exception:
        pass
    return "\n".join(parts) or item.name


KEYS = {"enter": "{Enter}", "tab": "{Tab}", "esc": "{Esc}", "escape": "{Esc}", "backspace": "{Back}",
        "up": "{Up}", "down": "{Down}", "left": "{Left}", "right": "{Right}", "pagedown": "{PageDown}",
        "pageup": "{PageUp}", "home": "{Home}", "end": "{End}", "delete": "{Del}", "space": " "}


def press(keys):
    """'enter', 'ctrl+a', 'ctrl+shift+t', 'alt+tab'."""
    parts = [p.strip().lower() for p in keys.split("+")]
    mods = "".join({"ctrl": "{Ctrl}", "control": "{Ctrl}", "alt": "{Alt}", "shift": "{Shift}", "win": "{Win}"}.get(p, "")
                   for p in parts[:-1])
    last = parts[-1]
    auto.SendKeys(mods + KEYS.get(last, last if len(last) == 1 else "{" + last.capitalize() + "}"), waitTime=0.1)


def scroll(item=None, direction="down", amount=5):
    if item is not None:
        x1, y1, x2, y2 = item.rect
        auto.MoveTo((x1 + x2) // 2, (y1 + y2) // 2, moveSpeed=0, waitTime=0.05)
    (auto.WheelDown if direction == "down" else auto.WheelUp)(wheelTimes=int(amount), waitTime=0.05)


def wait_until(win, text=None, gone=False, timeout=90):
    """Polls the window (free, no AI): until some text appears / disappears, or, with no text, until the
    page stops changing for 1.5 s (an AI finished answering, a page finished loading)."""
    end = time.time() + timeout
    last, since = None, time.time()
    while time.time() < end:
        s = Screen(win)
        if text:
            there = any(text.lower() in (i.name + " " + i.value).lower() for i in s.items)
            if there != gone:
                return True
        else:
            sig = s.signature()
            if sig != last:
                last, since = sig, time.time()
            elif time.time() - since >= 1.5:
                return True
        time.sleep(0.4)
    return False


def open_url(url):
    webbrowser.open(url)
    time.sleep(2.5)
