"""What Friday is busy with right now, for the floating "task ongoing" pill: on the laptop screen (a task asked here)
and on his phone (a task asked from there, fetched with GET /progress). A task has a title, its current step, a step
count and, during web searches, the sites she's reading (shown as chips with their icons, like ChatGPT voice mode).
A finished task stays 3 s as "Done", then goes."""
import itertools
import threading
import time
from urllib.parse import urlparse

_cv = threading.Condition()
_tasks = {}  # id -> item
_ids = itertools.count(1)
_v = 0
listeners = []  # called (on any thread) with the laptop's items after every change: the laptop pill
DONE_FOR = 3.0


def _changed():
    global _v
    _v += 1
    _cv.notify_all()
    local = [dict(t, sites=list(t["sites"])) for t in _live() if not t["to_phone"]]
    for fn in list(listeners):
        try:
            fn(local)
        except Exception:
            pass


def _live():
    now = time.time()
    for tid in [k for k, t in _tasks.items() if t["ended"] and now - t["ended"] > DONE_FOR]:
        _tasks.pop(tid, None)
    return sorted(_tasks.values(), key=lambda t: t["started"])


def start(title, kind="task", to_phone=False):
    """A task begins: returns its id. kind: task (steps on screen), web (a search), agent (a background job)."""
    with _cv:
        tid = next(_ids)
        _tasks[tid] = {"id": tid, "title": (title or "Working on it")[:80], "kind": kind, "step": "", "n": 0, "sites": [],
                       "to_phone": bool(to_phone), "started": time.time(), "ended": 0, "ok": True}
        _changed()
        return tid


def step(tid, text, count=True):
    with _cv:
        t = _tasks.get(tid)
        if not t or t["ended"] or not text or text == t["step"]:
            return
        t["step"] = text[:120]
        if count:
            t["n"] += 1
        _changed()


def site(tid, url, title=""):
    """A site she's searching or reading: its domain becomes a chip (max 6, no repeats)."""
    try:
        host = urlparse(url if "://" in url else "https://" + url).hostname or ""
    except ValueError:
        return
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return
    with _cv:
        t = _tasks.get(tid)
        if not t or t["ended"] or any(s["domain"] == host for s in t["sites"]) or len(t["sites"]) >= 6:
            return
        t["sites"].append({"domain": host, "url": url, "title": (title or "")[:60]})
        t["step"] = f"Reading {host}"
        _changed()


def done(tid, ok=True, text=""):
    with _cv:
        t = _tasks.get(tid)
        if not t or t["ended"]:
            return
        t["ended"], t["ok"] = time.time(), ok
        t["step"] = text or ("Done" if ok else "Stopped")
        _changed()
    # a last change after it's gone, so the pills drop it
    threading.Timer(DONE_FOR + 0.2, lambda: _poke()).start()


def _poke():
    with _cv:
        _changed()


def phone_items(after=-1, timeout=25.0):
    """For the phone's long-poll: (version, its items) once something changes after `after`, or at the timeout."""
    end = time.time() + timeout
    with _cv:
        while _v <= after and time.time() < end:
            _cv.wait(max(0.1, end - time.time()))
        return _v, [dict(t, sites=list(t["sites"])) for t in _live() if t["to_phone"]]


class Tracker:
    """with progress.Tracker("Booking the Rapido", to_phone=...) as p: p.step(...); p.site(url)"""

    def __init__(self, title, kind="task", to_phone=False):
        self.id = start(title, kind, to_phone)
        self.ok = True

    def step(self, text, count=True):
        step(self.id, text, count)

    def site(self, url, title=""):
        site(self.id, url, title)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        done(self.id, ok=exc_type is None and self.ok)
        return False
