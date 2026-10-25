"""Friday looking after herself and after him, unasked.

- Nightly self-review (`nightly_review`): once a day, early morning, she reads yesterday's conversation, his open
  feedback and the log's failures, and rewrites data/knowledge/lessons.md (part of her prompt on both devices) with
  concrete behaviour rules; feedback a lesson covers is ticked; code changes she'd want go to data/wishes.md for him.
  She never edits her code at night. If the lessons changed she restarts herself (quiet hours, nobody talking) so
  the new rules load.
- Device watch (`device_watch`): every three minutes she reads the devices line. Safe fixes happen on their own
  (temp files cleared when C: is nearly full, Wi-Fi reconnected and DNS flushed when the internet drops, a diagnose
  started when updates keep failing or an app keeps crashing); the rest is a heads-up (laptop or phone battery low,
  phone out of space, a restart pending), each once a day, never in quiet hours or while he's away.
- Day checks (`day_checks`): at the configured times (12:30 and 17:00 by default) when he's at the laptop, a quick
  look at his calendar and Gmail: only what needs him in the next few hours, said once; nothing if nothing.

State lives in data/care.json. Everything here runs on one background thread started by `start(J)`."""

import datetime as dt
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("friday")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE = os.path.join(DATA, "care.json")
LESSONS = os.path.join(DATA, "knowledge", "lessons.md")
WISHES = os.path.join(DATA, "wishes.md")

_J = None
_state = {}


def _load():
    global _state
    try:
        with open(STATE, encoding="utf-8") as f:
            _state = json.load(f)
    except (OSError, ValueError):
        _state = {}


def _save():
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=1)
    except OSError:
        log.exception("care state save failed")


def _today():
    return dt.date.today().isoformat()


def _once(key):
    """True the first time today for this key (and remembers it)."""
    if _state.get(key) == _today():
        return False
    _state[key] = _today()
    _save()
    return True


def _tell(text, mood="calm", urgent=False):
    """A heads-up for him, said when he's there and it's a decent hour; otherwise a card in her window and the log."""
    J = _J
    J.MEMORY.log("Friday", f"[care] {text}")
    J.friday.emit(who="friday", text=text)
    if J.friday.quiet_hours() or J.session_locked() or getattr(J.friday, "sleeping", False) or J.friday.paused:
        J.friday.status("idle", text, hide_after=20)
        return
    J.friday.announce.put(("care", (text, mood)))


# ---------------------------------------------------------------- the nightly self-review

REVIEW_PROMPT = """You are FRIDAY's reflective mind, running on her own while he sleeps. You read one day of her life with Sam
and make her better at being HIS assistant tomorrow, through rules, not code.
You get: yesterday's conversation log (He / Friday / Action lines), his open feedback about her (a to-do list he
dictated), the day's failures from her log, and the current lessons file. Do exactly this:
1. Rewrite {lessons} completely. It is "LESSONS SHE LEARNT" and goes straight into her prompt on both devices, so
   every line costs tokens: at most 40 lines, each a concrete, testable behaviour rule in plain English ("When he says
   'type X' in a chat window, X is the whole message: don't ask"), grouped under 3-5 short headings. Keep rules
   from the current file that still hold, merge duplicates, drop anything fixed or stale, add what yesterday
   showed: where she misread him, asked twice, was slow, talked over him, missed a reminder, got a fact about him
   wrong, or where something worked well and should stay. Facts about him (names, schedule, likes) belong in her
   memory, not here, unless a misunderstanding came from not knowing one: then one line. Never write a language
   rule that contradicts what he taught her about Lisan ud-Dawat (that file wins); at most point at it.
2. In {feedback}, tick ("- [x]") only items that the lessons now genuinely cover or that the log shows fixed; leave
   the rest. Never delete lines, never reword his words.
3. Anything that needs a CODE change (a bug, a missing ability, a crash) is not yours to do: append one line per
   item to {wishes} ("- <date>: <what and why, one sentence>"), skipping ones already there.
4. Keep {style} ("HOW HE WRITES AND SPEAKS") up to date: at most 50 lines on how HE talks and texts, learnt from
   his own lines (He:), the messages he sent (SENT MESSAGES: his final text, whether he dictated it, her lines just
   before, any replies she suggested): his English texting (words, slang, emoji, length, punctuation) per kind of
   person (Sara and friends / Mum, Dad and elders / community and work), his Lisan ud-Dawat (romanized spellings
   and phrases he really uses), and above all HOW HE CHANGED HER DRAFTS OR SUGGESTIONS (what he cut, added or
   reworded): those are the strongest signal. Keep what still holds, add only what the day shows, never invent.
   His chats with Sara are private: never learn from, quote or describe anything to or from her.
   Keep its five headings exactly (## Everywhere, ## Work, ## Family, ## Friends, ## Suggesting replies...):
   the phone sends only the sections that fit the person, picked by those headings.
   It goes into her prompt (drafting messages for him, reply suggestions), so concrete examples beat adjectives.
5. Your final message is ONE short spoken sentence for him in the morning, warm and casual, no numbers unless
   useful: what you tightened ("Overnight I fixed how I handle 'reply X' and stopped asking twice.") or "Nothing to
   change from yesterday." Nothing technical, no file names."""


def _sent(day):
    """That day's sent messages from data/style_log.jsonl, one compact line each."""
    out = []
    try:
        with open(os.path.join(DATA, "style_log.jsonl"), encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if "sara" in str(r.get("to", "")).lower():
                    continue  # his chats with Sara are private: never learnt from (his rule, 12 Oct)
                if r.get("t", "")[:10] == day:
                    out.append(json.dumps({k: r.get(k) for k in ("t", "channel", "to", "text", "their", "dictated",
                                                                 "his_words", "her_lines", "suggested", "picked", "edited")
                                           if r.get(k) not in (None, "", [])}, ensure_ascii=False)[:1200])
    except OSError:
        pass
    return out[-60:]


def _log_failures(day):

    """Failure lines from friday.log for that day: errors, loops, refused actions, things that fell over."""
    out = []
    try:
        with open(os.path.join(HERE, "friday.log"), "rb") as f:
            f.seek(max(0, os.path.getsize(os.path.join(HERE, "friday.log")) - 3_000_000))
            for line in f.read().decode("utf-8", "ignore").splitlines():
                if line.startswith(day) and re.search(r"ERROR|WARNING|unknown action|brain looping|fell over|didn't work|"
                                                     r"can't reach|held off|progress kept quiet|still-talking|Traceback", line):
                    out.append(line[11:220])
    except OSError:
        pass
    return out[-120:]


def nightly_review():
    """Yesterday, reviewed; lessons rewritten; a line kept for the morning. Returns the spoken line or ""."""
    J = _J
    day = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    recs = [r for r in J.MEMORY.entries(since=day) if r["t"][:10] == day]
    lines = [f"{r['t'][11:16]} {r['who']}: {r['text']}" for r in recs]
    if len(lines) < 6:
        log.info("REVIEW %s: too little said, skipped", day)
        return ""
    os.makedirs(os.path.dirname(LESSONS), exist_ok=True)
    before = ""
    try:
        with open(LESSONS, encoding="utf-8") as f:
            before = f.read()
    except OSError:
        pass
    failures = _log_failures(day)
    prompt = (f"DAY: {dt.date.fromisoformat(day):%A %d %B %Y}\n\n=== CONVERSATION ===\n" + "\n".join(lines)[-30000:]
              + "\n\n=== HIS OPEN FEEDBACK ===\n" + "\n".join(f"- {x}" for x in J.MEMORY.open_feedback()[-25:])
              + "\n\n=== FAILURES IN HER LOG ===\n" + ("\n".join(failures) or "(none)")
              + "\n\n=== SENT MESSAGES ===\n" + ("\n".join(_sent(day)) or "(none)")
              + "\n\n=== CURRENT LESSONS FILE ===\n" + (before or "(empty)"))
    system = REVIEW_PROMPT.format(lessons=LESSONS, feedback=os.path.join(DATA, "feedback.md"), wishes=WISHES,
                                  style=os.path.join(DATA, "knowledge", "his_style.md"))
    log.info("REVIEW %s: %d lines, %d failures", day, len(lines), len(failures))
    out = J.ask_claude(system, prompt, model=J.CFG.get("review_model", "sonnet"), tools="Read,Edit,Write,Glob,Grep",
                       timeout=900, cwd=DATA)
    after = ""
    try:
        with open(LESSONS, encoding="utf-8") as f:
            after = f.read()
    except OSError:
        pass
    changed = after.strip() != before.strip()
    note = (out or "").strip().splitlines()[-1] if out else ""
    note = re.sub(r"[*_#`]", "", note)[:300]
    log.info("REVIEW done: changed=%s: %s", changed, note)
    J.MEMORY.log("Action", f"nightly self-review of {day}: {'lessons updated' if changed else 'no change'}. {note}")
    _state["review_note"] = note
    _state["review_changed"] = changed
    _save()
    return note


def morning_note():
    """The review's one-liner, once, for the first conversation of the day (the briefing or her first reply)."""
    note = _state.get("review_note")
    if note and _once("review_told"):
        return note
    return ""


# ---------------------------------------------------------------- the device watch

def _num(pat, text, default=None):
    m = re.search(pat, text or "")
    return float(m.group(1)) if m else default


def device_watch():
    J = _J
    d = J.devices.laptop_status(max_age=170)
    if d:
        bat, st = d.get("battery"), d.get("status")
        if bat is not None and st == 1 and bat <= 20 and _once("laptop_battery"):
            _tell(f"Your laptop's at {bat} percent and not plugged in. Grab the charger.", "serious")
        if bat is not None and (st != 1 or bat > 50):
            _state.pop("laptop_battery", None)
        # the internet: a real drop (two checks in a row, Wi-Fi still joined) gets a reconnect and a DNS flush
        if d.get("ssid") and not d.get("internet"):
            _state["net_down"] = _state.get("net_down", 0) + 1
            if _state["net_down"] == 2 and _once("net_fix"):
                J.devices.set_laptop("flush dns")
                ok, text = J.devices.set_laptop("reconnect wifi")
                time.sleep(8)
                if J.devices.internet_ok():
                    _tell("The internet had dropped, so I reconnected the Wi-Fi. It's back.", "calm")
                else:
                    _tell("Heads up, the Wi-Fi's connected but there's no internet, and a reconnect didn't help. "
                          "Might be the hotspot or the line.", "serious")
        else:
            _state["net_down"] = 0
        # space on C:
        c = next((x for x in d.get("disks") or [] if x.get("d") == "C:"), None)
        if c and c.get("free", 99) < 8 and _once("c_space"):
            ok, text = J.devices.set_laptop("clear temp")
            J.MEMORY.log("Action", f"[care] C: was at {c['free']} GB: {text}")
            J.devices.laptop_status(max_age=0)
            _tell(f"Your C drive was down to {c['free']} gigs, so I cleared the old temp files. {text} "
                  "If it's still tight, say the word and I'll find what's hogging it.", "calm")
        if d.get("pending_reboot") and _once("pending_reboot"):
            _tell("Windows is waiting for a restart to finish updates. Tell me when it's a good time.", "calm")
        # updates failing again and again, or one app crashing repeatedly: she looks into it herself
        if (d.get("update_failures") or 0) >= 2 and _once("update_fix") and not J.friday.in_session:
            J.MEMORY.log("Action", "[care] updates keep failing: starting a diagnosis")
            J.friday.do({"a": "diagnose", "device": "laptop",
                         "problem": "Windows or Store updates keep failing today (seen in the event log). Find why and fix it."})
        if (d.get("app_crashes") or 0) >= 3 and _once("crash_fix") and not J.friday.in_session:
            J.MEMORY.log("Action", "[care] repeated app crashes: starting a diagnosis")
            J.friday.do({"a": "diagnose", "device": "laptop",
                         "problem": "An app on the laptop has crashed or hung three or more times in the last day (event log). Find which, why, and fix it."})
    # the phone, from its own status line
    ph = J.devices._phone
    if ph.get("line") and time.time() - ph.get("seen", 0) < 300:
        line = ph["line"]
        pb = _num(r"battery (\d+)%", line)
        charging = bool(re.search(r"charging|plugged", line))
        if pb is not None and pb <= 15 and not charging and _once("phone_battery"):
            _tell(f"Your phone's at {int(pb)} percent. Charger time.", "serious")
        if pb is not None and (charging or pb > 50):
            _state.pop("phone_battery", None)
        free = _num(r"(\d+) GB free", line)
        if free is not None and free < 5 and _once("phone_space"):
            _tell(f"Your phone's down to {int(free)} gigs free. Want me to see what's eating it?", "calm")
        if "NO INTERNET" in line and _once("phone_net"):
            _tell("Your phone's connected but has no internet right now.", "calm")
        if re.search(r"running (hot|very hot|dangerously hot)", line) and _once("phone_heat"):
            _tell("Your phone's running hot. Maybe give it a break from whatever's cooking it.", "calm")


# ---------------------------------------------------------------- the day checks (calendar and mail)

def day_checks():
    J = _J
    now = dt.datetime.now()
    for slot in J.CFG.get("care_checks", ["12:30", "17:00"]):
        hh, mm = int(slot[:2]), int(slot[3:5])
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if not (due <= now < due + dt.timedelta(minutes=40)) or _state.get(f"check_{slot}") == _today():
            continue
        if J.friday.quiet_hours() or J.session_locked() or J.friday.paused or J.bridge_idle() > 1800:
            continue  # not there: it'll be tried again within the 40-minute window, else skipped today
        _state[f"check_{slot}"] = _today()
        _save()
        text = J.ask_google(
            "Look at his Google Calendar and Gmail and tell him only what NEEDS him in the next three hours: a meeting "
            "or event (time and who, in words), or an important unread email from today that wants a reply or an action "
            "(skip newsletters, promotions, receipts, automated mail, anything he's already replied to). At most three "
            "short spoken sentences, casual. If nothing needs him, reply with exactly the word NOTHING.",
            services=("calendar", "gmail"), timeout=180)
        text = (text or "").strip()
        if not text or text.upper().startswith("NOTHING") or "didn't answer" in text or "needs my Claude brain" in text:
            log.info("CARE check %s: nothing", slot)
            continue
        J.MEMORY.log("Action", f"[care] {slot} check: {text}")
        _tell(text, "warm")


# ---------------------------------------------------------------- the loop

def _loop():
    _load()
    last_watch = 0
    while True:
        try:
            now = dt.datetime.now()
            if time.time() - last_watch > 180:
                last_watch = time.time()
                device_watch()
            day_checks()
            # the self-review: once a day from 03:30, when nobody's talking (or at the first quiet moment after)
            if (now.hour >= 3 and (now.hour, now.minute) >= (3, 30) and _state.get("reviewed") != _today()
                    and not _J.friday.in_session and _J.CFG.get("nightly_review", True)):
                _state["reviewed"] = _today()
                _save()
                nightly_review()
                if _state.get("review_changed") and not _J.friday.in_session and _J.friday.quiet_hours():
                    log.info("REVIEW: lessons changed, restarting to load them")
                    time.sleep(2)
                    _J.friday.restart_self()
        except Exception:
            log.exception("care loop failed")
        time.sleep(60)


def start(J):
    """J = the friday module (it holds CFG, MEMORY, devices, ask_claude, ask_google, session_locked and `friday`,
    the running Jarvis)."""
    global _J
    _J = J
    threading.Thread(target=_loop, daemon=True, name="care").start()
