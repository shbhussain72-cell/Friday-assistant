"""WhatsApp Desktop automation through UI Automation (the app is a WebView2 page)."""
import re
import time

import pyperclip
import uiautomation as auto

COMPOSER_PREFIX = "Type a message to "


class WhatsAppError(Exception):
    pass


def _retry(fn):
    """WebView2's UIA tree throws transient COM errors while the page re-renders."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        for attempt in range(3):
            try:
                return fn(*a, **kw)
            except WhatsAppError:
                raise
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(0.5)
    return wrapper


def _window():
    win = auto.WindowControl(searchDepth=1, ClassName="WinUIDesktopWin32WindowClass", SubName="WhatsApp")
    if not win.Exists(0.5):
        import subprocess
        subprocess.Popen(["explorer.exe", "whatsapp://"])
        if not win.Exists(15):
            raise WhatsAppError("WhatsApp didn't open")
        time.sleep(3)
    return win


def _edits(win):
    """Returns (search_box, composer_or_None). The page tree is deep (~25 levels)."""
    search = composer = None
    for c, _ in auto.WalkControl(win, maxDepth=40):
        if c.ControlTypeName != "EditControl":
            continue
        if (c.Name or "").startswith(COMPOSER_PREFIX):
            composer = composer or c
        elif search is None:
            search = c
        if search and composer:
            break
    return search, composer


def _focus(win):
    import ctypes
    hwnd = win.NativeWindowHandle
    if auto.IsIconic(hwnd):
        auto.ShowWindow(hwnd, auto.SW.Restore)
    # an input event lets a background process take the foreground; F24 does nothing in any app
    ctypes.windll.user32.keybd_event(0x87, 0, 0, 0)
    ctypes.windll.user32.keybd_event(0x87, 0, 2, 0)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)


def _paste(text):
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    auto.SendKeys("{Ctrl}v", waitTime=0.3)  # restoring the clipboard sooner can race the paste
    if old is not None:
        pyperclip.copy(old)


def current_chat():
    win = _window()
    _, composer = _edits(win)
    return composer.Name[len(COMPOSER_PREFIX):] if composer else None


@_retry
def open_chat(name):
    """Searches for `name`, opens the top result, returns the real chat name."""
    win = _window()
    _focus(win)
    search, composer = _edits(win)
    before = composer.Name if composer else None
    if not search:
        raise WhatsAppError("couldn't find the search box")
    search.Click(simulateMove=False, waitTime=0.05)
    auto.SendKeys("{Ctrl}a{Back}", waitTime=0.1)
    _paste(name)
    time.sleep(1.0)
    results = _search_results(win, search.BoundingRectangle.right)
    best = _best_match(name, results)
    if best:
        best[1].Click(simulateMove=False, waitTime=0.05)
    else:
        auto.SendKeys("{Enter}", waitTime=0.1)
    # The composer label changes once the new chat opens.
    now = None
    for _ in range(4):  # each check walks the page tree (~0.7 s)
        time.sleep(0.4)
        now = current_chat()
        if now and now != before:
            break
    opened = bool(best) or (now and now != before)
    # clear the search so the chat list isn't left filtered
    search.Click(simulateMove=False, waitTime=0.05)
    auto.SendKeys("{Ctrl}a{Back}", waitTime=0.1)
    if not now or not opened:
        raise WhatsAppError(f"couldn't find a chat for {name}")
    return chat_header() or now


_WHEN = (r"\d{1,2}:\d{2}\s?[ap]m|Yesterday|Today|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
         r"|\d{1,2}/\d{1,2}/\d{2,4}")
_ROW = re.compile(rf"^(?:\d+ unread messages? )?(.+?) (?:{_WHEN})$")


def _search_results(win, pane_right):
    """[(chat name, control)] for the rows in the filtered chat list."""
    out, seen = [], set()
    for c, _ in auto.WalkControl(win, maxDepth=40):
        if c.ControlTypeName != "DataItemControl":
            continue
        r = c.BoundingRectangle
        if r.left > pane_right or r.width() <= 0:
            continue
        m = _ROW.match((c.Name or "").strip())
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), c))
    return out


def _best_match(query, results):
    import difflib
    q = _loose(query)
    best, best_score = None, 0.0
    for i, (name, ctrl) in enumerate(results):
        n = _loose(name)
        score = difflib.SequenceMatcher(None, q, n).ratio()
        if n == q:
            score += 2
        elif n.startswith(q) or any(_loose(w) == q for w in name.split()):
            score += 1
        score -= i * 0.01  # tie-break toward WhatsApp's own ordering
        if score > best_score:
            best, best_score = (name, ctrl), score
    return best if best_score >= 0.45 else None


def _loose(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


@_retry
def type_message(text):
    """Puts text in the open chat's composer without sending."""
    win = _window()
    _focus(win)
    _, composer = _edits(win)
    if not composer:
        raise WhatsAppError("no chat is open")
    composer.Click(simulateMove=False, waitTime=0.05)
    auto.SendKeys("{Ctrl}a{Back}", waitTime=0.1)
    _paste(text)
    return composer.Name[len(COMPOSER_PREFIX):]


def press_send():
    auto.SendKeys("{Enter}", waitTime=0.3)


def _copy_files(paths):
    """Puts files on the clipboard the way Explorer's Copy does (CF_HDROP)."""
    import struct
    import win32clipboard
    names = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    drop = struct.pack("<IiiII", 20, 0, 0, 0, 1) + names  # DROPFILES header, wide-char names
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, drop)
    finally:
        win32clipboard.CloseClipboard()


def _send_button(win):
    """The lowest, right-most Send button (the attachment preview's), or None."""
    best = None
    for c, _ in auto.WalkControl(win, maxDepth=40):
        if c.ControlTypeName == "ButtonControl" and (c.Name or "").strip().lower().startswith("send"):
            r = c.BoundingRectangle
            if r.width() > 0 and (best is None or (r.bottom, r.right) > (best[0].bottom, best[0].right)):
                best = (r, c)
    return best[1] if best else None


@_retry
def attach_files(paths):
    """Pastes files into the open chat, which opens WhatsApp's send preview. Nothing is sent yet."""
    win = _window()
    _focus(win)
    _, composer = _edits(win)
    if not composer:
        raise WhatsAppError("no chat is open")
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    _copy_files(paths)
    composer.Click(simulateMove=False, waitTime=0.05)
    auto.SendKeys("{Ctrl}a{Back}", waitTime=0.1)
    auto.SendKeys("{Ctrl}v", waitTime=0.3)
    btn = None
    for _ in range(20):  # the preview takes a moment to load the files
        time.sleep(0.25)
        btn = _send_button(win)
        if btn:
            break
    if old is not None:  # give him his clipboard back
        pyperclip.copy(old)
    return btn


def send_attachment(btn=None):
    """Sends what's in the preview: presses its Send button (from attach_files, else found again)."""
    win = _window()
    _focus(win)
    btn = btn or _send_button(win)
    if btn:
        _press(btn)
    else:
        auto.SendKeys("{Enter}", waitTime=0.3)


def _press(btn):
    """Presses a button without the mouse (Friday's overlay can sit on top of WhatsApp's Send button)."""
    try:
        btn.GetInvokePattern().Invoke()
        time.sleep(0.3)
    except Exception:
        btn.Click(simulateMove=False, waitTime=0.3)


def discard_attachment():
    """Closes the send preview without sending (Esc, then 'Discard' on WhatsApp's 'Discard selection?')."""
    win = _window()
    _focus(win)
    auto.SendKeys("{Esc}", waitTime=0.5)
    for c, _ in auto.WalkControl(win, maxDepth=40):
        if c.ControlTypeName == "ButtonControl" and (c.Name or "").strip() == "Discard":
            _press(c)
            return


@_retry
def start_call(video=False):
    """Clicks the header's Voice call / Video call button in the open chat. True if it was there."""
    win = _window()
    _focus(win)
    want = "Video call" if video else "Voice call"
    for c, _ in auto.WalkControl(win, maxDepth=30):
        if c.ControlTypeName == "ButtonControl" and (c.Name or "").strip() == want and c.BoundingRectangle.top < 140:
            c.Click(simulateMove=False, waitTime=0.1)
            return True
    return False


_TIME = re.compile(r"^\s*\d{1,2}:\d{2}\s?[ap]m\b", re.I)
_NOISE = {"Delivered", "Read", "Sent", "Kept message", "Edited", "Forwarded", "wds-ic-read"}


@_retry
def chat_header():
    """Display name of the open chat (the composer label only has the number for some contacts)."""
    win = _window()
    search, composer = _edits(win)
    if not composer:
        return None
    pane = search.BoundingRectangle.right if search else 0
    # the header is the one wide button across the top of the chat pane
    best, width = None, 300
    for c, _ in auto.WalkControl(win, maxDepth=30):
        r = c.BoundingRectangle
        if c.ControlTypeName == "ButtonControl" and r.left > pane and 30 < r.top < 140 and r.width() > width and c.Name:
            best, width = c.Name.strip(), r.width()
    if best:
        return re.sub(r"\s+(click here for (contact|group) info|Message yourself|online|typing…?|last seen.*)$",
                      "", best, flags=re.I)
    return composer.Name[len(COMPOSER_PREFIX):]


@_retry
def read_messages(n=5):
    """Last n messages of the open chat as 'Sender: text' strings, oldest first."""
    win = _window()
    search, composer = _edits(win)
    if not composer:
        raise WhatsAppError("no chat is open")
    pane = search.BoundingRectangle.right if search else 0
    bottom = composer.BoundingRectangle.top
    msgs, seen_rows, sender = [], set(), None
    for c, _ in auto.WalkControl(win, maxDepth=40):
        if c.ControlTypeName != "DataItemControl":
            continue
        r = c.BoundingRectangle
        if r.left <= pane or r.top >= bottom or r.width() <= 0 or r.top in seen_rows:
            continue
        seen_rows.add(r.top)
        who, parts = None, []
        for d, _ in auto.WalkControl(c, maxDepth=12):
            name = (d.Name or "").strip()
            if not name:
                continue
            if d.ControlTypeName == "GroupControl" and name.endswith(":") and len(name) < 60:
                who = name[:-1]
            elif d.ControlTypeName == "TextControl" and not _TIME.match(name) and name not in _NOISE:
                if name not in parts:
                    parts.append(name)
            elif d.ControlTypeName == "ImageControl" and len(name) <= 2 and not parts:
                parts.append(name)  # emoji-only message
        if who:
            sender = who
        if parts:
            msgs.append((r.top, f"{sender or 'Them'}: {' '.join(parts)}"))
    msgs.sort()
    return [m for _, m in msgs][-n:]
