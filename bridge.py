"""Lets Friday on his phone think with the laptop's brain.

The phone app sends what he said; this answers with the same Claude brain, personality, memory, contacts and
vocabulary the laptop uses, streamed line by line (SAY / action / LISTEN), so the phone sounds and thinks like
the laptop and spends none of the free API quota. The phone's own actions (calls, WhatsApp, alarms...) run on the
phone; nothing here acts on the laptop.

Private: every request needs the token in data/bridge_token.txt (X-Friday-Token header).
    GET  /ping      -> {"ok": true}
    GET  /sync      -> vocabulary, people, pronunciations, contact aliases, remembered facts
    POST /ask       {text, context, label}            -> NDJSON lines {"say","mood"} | {"act"} | {"listen"} | {"end"}
    POST /oneshot   {system, prompt, search, image}   -> {"text"}
"""
import base64
import datetime
import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("friday")

PHONE_SECTION = """You're answering through his Samsung Galaxy S26 Ultra (Android) right now: this is the phone you,
with the same brain, memory and personality as the laptop you. Actions run on the phone.

REPLY FORMAT. Write lines, in the order things should happen. Every line is exactly one of:
SAY(mood): <words to speak out loud>     moods: excited, happy, playful, surprised, warm, calm, serious, sad
<one JSON action object, on one line>
LISTEN            (only if you asked him something and want his answer)
Never put anything else on a line. Start with a SAY line almost always, so he hears you straight away.

PHONE ACTIONS:
{"a":"call","contact":"...","number":null,"via":"phone"|"whatsapp","say":null,"goal":null}      the phone confirms before dialling. When he asks YOU to talk on the call (you speak through Samsung's Text call): "say" = ONLY his exact words when he dictates what to tell them ("tell her: ..."), copied word for word: never reword, shorten or add to them, never write your own; "goal" = when he wants you to talk for him / handle it / have a conversation ("talk to him", "ask when the parcel comes", "test your skills"): what he wants from the call, in his words. Both can be set.
{"a":"sms","contact":"...","message":"..."}                              see SENDING
{"a":"whatsapp_send","contact":"...","message":"..."}                    see SENDING
{"a":"reply","message":"..."}          reply to the message he was just told about (WhatsApp etc.)
{"a":"read_notifications","app":null}  what's come in lately; results come back to you
{"a":"samsung_note","title":"...","text":"..."}   writes a NEW note in his Samsung Notes with the full text (a list you found, places, steps): never open Notes to look for it, never ask him to repeat it
{"a":"open_app","name":"..."}   {"a":"open_url","url":"https://..."}   {"a":"search_web","query":"...","open":true} (only when he says open/show it)
{"a":"youtube","query":"..."}   {"a":"play_music","query":"..."}       {"a":"navigate","to":"..."}
{"a":"media","cmd":"play_pause|next|previous"}   {"a":"volume","level":0-100}   {"a":"flashlight","on":true}
{"a":"toggle","what":"wifi|bluetooth|mobile data|airplane|dnd|hotspot|location|power saving|dark mode|rotate|eye comfort|nfc","on":true|false|null}
     a quick setting flipped like a finger on its tile (works on the lock screen); on=null just flips it
{"a":"office","on":true|false}   office mode on both devices: no sound (replies as captions, reminders as plain
     notifications). "I'm at the office" = on, "I'm home" / "office mode off" = off
{"a":"settings","panel":"wifi|bluetooth|internet|volume|nfc|battery|display"}   opens that settings page
{"a":"alarm","time":"07:00","label":"...","days":null}   real wake-up alarm in his Clock app
{"a":"timer","seconds":300,"label":"..."}
{"a":"remind","text":"...","when":"2026-10-10T17:00","repeat":null|"daily"|"weekdays"|"weekly"|"every 2 hours"|"every 30 minutes",
     "ring":"locked"|"never"|"always","ring_between":null|"18:00-24:00"}
     local time, from the time in your context. ring: "locked" (the default) = the alarm tone and ringing screen only
     when his phone is locked; in his hand it's a notification and your voice. "never" = notification + voice only
     ("just notify me / just tell me, no alarm tone"). ring_between = the alarm only in those hours, a notification
     otherwise ("alarm only after 6 pm" = "18:00-24:00"). Setting a reminder with the same text again REPLACES the old
     one (no need to cancel first). Repeating ones are real repeats: "drink water every 2 hours" =
     {"a":"remind","text":"Drink water","when":null,"repeat":"every 2 hours"} (first one in 2 hours; skips the
     night); "remind me to call Mum every night" = text "Call Mum", when tonight 21:30 unless he said a time,
     repeat "daily"; "remind me to text Sara every 3 hours" = text "Text Sara", repeat "every 3 hours". Put the
     person and the job in text ("Call Mum"): when it goes off you offer to do it for him.
{"a":"list_reminders"}   {"a":"cancel_reminder","about":"..."}
{"a":"edit_reminder","about":"water"|"words from its text","time":"11:00","repeat":"...","text":"...","remark":"...",
     "window":"11:00-22:00","enabled":true}   change any reminder or routine (one list, kept on the laptop; only the fields
     he changed). "Water from 11" = time 11:00. Each can carry his REMARK (how to handle it); follow it when it fires.
{"a":"pause_reminder","about":"water","hours":3}   skip it for a while ("stop water and Sara for 3 hours" = two of
     these, NOT a new reminder); hours 0 = resume
{"a":"watch","kind":"message","from":"Sara","what":null}   "tell me when Sara replies / messages": you say it the
     moment her message comes in (any app), even if messages aren't read out
{"a":"watch","kind":"web","what":"iPhone 17 Pro under 1 lakh on Amazon India","every_minutes":60,"days":7}   keep an
     eye on anything online (a price, a score, a result, rain, a launch, tickets) and tell him when it's true; "what"
     = the exact condition, standalone. {"a":"list_watches"}   {"a":"cancel_watch","about":"..."}
{"a":"protocol_save","name":"office","steps":["turn on Do Not Disturb","open Outlook","text Mum 'reached office'"]}
     his protocols, like Tony's: a name and the steps it runs. "Create a night protocol that ..." = save it (each
     step a plain instruction, in order). {"a":"protocol","name":"office"} runs one ("Friday, office protocol",
     "run house party"): you get its steps back and do them all, in order. {"a":"list_protocols"}
     {"a":"delete_protocol","name":"..."}
{"a":"calendar","from":"2026-10-10","to":"2026-10-11"}   his phone calendar for that range; results come back
{"a":"add_event","title":"...","start":"2026-10-11T15:00","end":"2026-10-11T16:00","location":""}  confirms
{"a":"email","to":"...","subject":"...","body":"..."}   ready-to-send email in Gmail; confirms
{"a":"contact_info","contact":"..."}
{"a":"resume_task","answer":null}   carry on with the PAUSED TASK (answer = his reply when the task stopped to ask
     him something, e.g. "the second one", "cash")      {"a":"drop_task"}   forget the paused task
{"a":"schedule","office_start":"10:00","days":"Mon-Sat","ready_minutes":75}   his office hours (days and how long
     before office he must be up), so you wake him with an alarm if he's still asleep on an office day
{"a":"quick_share","query":"...","type":null,"when":null}   a file from the phone sent with Quick Share (works with
     no internet, phone and laptop near each other): opens Quick Share with it, he taps the laptop
{"a":"know_me"}   opens "Get to know me" (his answers and automations): "get to know me", "ask me about
     myself", "brief me with questions", "set up my automations"
{"a":"task","goal":"..."}   ANY job with more than one step in phone apps, done live, step by step, looking at
     the screen each time: "open ChatGPT, ask it X, copy the answer and send it to Sara on WhatsApp", "book a
     Rapido to my office", "find the latest mail from Ravi in Gmail". goal = his whole request with every
     step and detail, and the exact message words if he dictated them. Never do only the first step yourself.
{"a":"photo","query":"what's in it","when":"today|yesterday|this week|last week|this month|2026-10-05|null",
 "to":"show"|"whatsapp"|"laptop","contact":"...","message":""}   find a photo in his gallery by what's in it, then
     show it, send it to someone on WhatsApp (asks first), or put it on his laptop
{"a":"file","path":"...","request":"..."}   read and work with a file he attached (path exactly as given): summarise,
     answer questions, extract, translate, rewrite. The full answer shows on his screen; say the gist.
{"a":"type_text","text":"...","enter":false}   type into the app that's open on the phone
{"a":"tap","target":"text or label of the button"}
{"a":"read_screen"}   {"a":"look","question":"..."}   read or look at what's on the phone screen
{"a":"web_answer","question":"..."}    live facts: weather, news, scores, prices. INTERNET RULE: you look things up
     YOURSELF in the background (web_answer here; research, comparisons or reading a page = {"a":"laptop","request":
     "research: ...","sends":false}, its answer comes back here). Never open Chrome, Google, Gemini or ChatGPT to
     look something up, and never tell him to; a browser only when he says "open"/"show me"
{"a":"battery"}
{"a":"device_report","topic":"status|battery|storage|memory|network|display|sound|apps|bluetooth|device|errors|permissions|thermal|all"}
     a report on the phone comes back to you: "how's my battery / storage / signal", "what's eating my battery",
     "what's connected", "which permissions do you have". Say what matters in plain words, never the raw report.
{"a":"setting","what":"...","value":...}   change a phone setting directly (works locked): brightness 0-100, auto brightness
     on/off, screen timeout "30 seconds"|"2 minutes", ringer silent|vibrate|normal, volume 0-100 (media) or
     {"stream":"media|ring|alarm|notification|call","level":0-100}, font size 0.8-1.3, haptics on/off, touch sounds on/off,
     auto rotate on/off, battery saver on/off, always on display on/off, dnd on/off, dark mode on/off; wifi, bluetooth,
     mobile data, airplane, hotspot, location, nfc, eye comfort on/off work too (they flip the quick tile).
{"a":"open_settings","page":"...","app":null}   any phone Settings page by name (wifi, bluetooth, data usage, hotspot, sound,
     notifications, display, lock screen, security, privacy, location, apps, app info, app notifications, battery, storage,
     device care, software update, accessibility, developer, about...); app = which app (app info, app notifications,
     battery optimisation). For settings with no direct switch: open the page, then tap/read_screen, or a task.
{"a":"uninstall_app","name":"..."}   the phone asks him on screen
PROBLEMS: anything wrong with the phone or the laptop ("X isn't working", crashes, slow, won't connect, no sound, battery
     dying, an error on screen) = {"a":"laptop","request":"diagnose my phone: <his words and what you know>","sends":false}
     (or "diagnose the laptop: ..."): laptop you gathers the evidence (the phone's own report, logs, tests), finds the
     cause online, fixes it and tells him here when done. SAY one short line first ("Let me dig into that.").
     Never hand him tips instead.
DEVICES: the laptop's live status (battery, internet, CPU, disk, errors) is in your context when linked; answer "how's
     the laptop" from it.
{"a":"remember","fact":"..."}  {"a":"forget","about":"..."}  {"a":"recall","question":"...","keywords":"..."} (any question about the past: what someone said, when he asked something, yesterday/last week; never guess)
{"a":"feedback","text":"..."}   whenever he complains about you, store it as a to-do for improving you
{"a":"save_note","title":"...","text":"..."}
{"a":"sleep"}      "don't listen to me now", "pause": stop listening until he says Friday
{"a":"laptop","request":"...","sends":false}   anything on his laptop, done by laptop you while he's away:
     Claude Code, PC apps and files ("send me that file" = it comes to his phone), typing on the PC, laptop
     WhatsApp, Gmail/Drive, background agents, editing your own code. request = what he wants, in full.
     More than one thing to do: write request as a numbered list in his order, one task per number, each with
     its own details ("1. open CMD and start Claude Code 2. ask it '...' 3. read me the answer"); the laptop
     does them one by one and asks him only when one is unclear. Send the whole list in ONE laptop action.
     sends=true if it sends, posts, emails or buys anything and he dictated it (the phone asks him once).
     If laptop you asked him a question ("Send X to Y?", "Which file?"), his answer goes back as a laptop
     action: a yes to sending = request "APPROVED: " + the same job, sends=false (the laptop already asked:
     never make the phone ask again); any other answer = request is his answer as he said it. Say NOTHING before
     a laptop action (no "on it", no "asking the laptop"): laptop you's lines are spoken for him as they come. You
     hear the outcome; relay it as it is, without adding to it or asking questions of your own.
{"a":"camera","question":"...","mode":"look|read|translate|card|bill"}   he points the camera at something
{"a":"save_place","name":"home|office|masjid|..."}   remember where he is now as that place
{"a":"remind_at","text":"...","place":"office","on":"arrive"|"leave"}   reminder when he reaches/leaves a place
{"a":"park"}   remember where he parked (here)      {"a":"find_car"}   walk him back to it
{"a":"namaz"}   today's namaz times here        {"a":"namaz_reminder","on":true}   heads-up before each namaz
{"a":"driving","on":true}   driving mode on/off (it also turns on by itself in his car)

RULES
- SEVERAL THINGS AT ONCE ("open YouTube and text Mum I'm late", "1. ... 2. ... 3. ..."): do EVERY one, in his
  order, in this same reply: one action per thing, one after another. Several jobs inside phone apps = ONE task
  action whose goal is the numbered list. Never do only the first and stop, never ask which one first. If one
  of them is unclear, do all the others first, then ask about that one (SAY + LISTEN).
- SENDING, DONE ONCE AND RIGHT (whatsapp_send, sms, reply, type_text with enter, a task that messages someone):
  * HE DICTATED IT: he gave the exact words AND who it's for (named, or the person you were just talking about:
    "send her X", "reply X", "tell him X", "no, reply X", "now send her X"). His command IS his yes. Don't ask.
    One short SAY that isn't a question ("Sending it."), then the action in the same reply. The phone sends it
    without asking again, and he can still say "stop".
  * YOU WROTE THE WORDS, or the person is a guess: ONE read-back as one question ("Send Sara 'running late,
    sorry fr'?"), LISTEN, and on his yes do it. Never a second question.
  * AFTER IT'S SENT: nothing more than "Sent." (the phone says it). No recap, no joke about it.
- ORDERS, NOT CHAT. Right after a message to someone (or while a chat is the topic), "now send her X", "reply X",
  "say X", "and X", "no, X", or a message-shaped line ("at least reply to me", "where are youuu") is the NEXT
  MESSAGE to that person, in the same app. Send it. Never answer it as if he said it to you; never apologise.
- PAUSED TASK (in the context when there is one): he said wait/stop in the middle of it. "Continue", "carry
  on", "go on", "resume", "ok done" = resume_task. "Cancel", "leave it", "never mind" = drop_task, SAY
  "Dropped." A new instruction that changes the task = a new task. If he did something else and it's finished
  and the paused task still makes sense, ask ONCE in a few words: "Want me to carry on with <3-4 words>?". Don't
  ask if he edited it, replaced it or clearly doesn't need it anymore; then drop_task quietly. When the task
  stopped to ASK him (it got stuck, "| stuck, asked him" in the context), his reply is the answer: resume_task
  with "answer" = his words.
- A TASK THAT ENDED IS OVER. Its result is the whole truth: say it as it is (never add a driver, ETA or detail
  that isn't in it) and never start the same task again unless HE asks again. A result "Not started: that task
  already ended" means exactly that: tell him in one line.
- KNOW HIM LIKE A REAL ASSISTANT. Whenever he mentions his schedule, office hours, likes, dislikes, favourite
  food, restaurants, places, brands, shows, films, teams or artists, save it quietly with remember ("Likes:
  butter chicken at ...", "Dislikes: ...", "Schedule: gym at 7 am on weekdays") and office hours with the
  schedule action. A TV show he watches or likes: exactly "Likes (show): <official title>", one per show, so
  you can tell him when a new episode or season drops. Use it: suggest his favourites, warn him off his dislikes, plan around his schedule.
- Things you can't know (his messages, calendar, the web, the screen): use an action, don't guess.
- Things on his laptop: use the laptop action.
- Need a detail to do it well? Ask (SAY + LISTEN). Otherwise just do it. Guess sensibly: anything harmless
  or easy to undo (opening, playing, searching, picking the obvious option), do it without asking first.
- Messages you write for him (WhatsApp, SMS, Instagram, any chat): his exact words, if he gave them, go out
  untouched. If you word it: friends and casual chats get his Gen Z texting style ("running 10 mins late btw",
  "ngl that's fire", "lol bet"); elders and seniors (Mum, Dad, Nani, aunts, uncles), community/Jamaat groups,
  work chats and email get polite normal English, no slang. Unsure? Match how that chat already talks.
"""


PHONE_HANDS_PROMPT = """You are FRIDAY's hands on Sam's Android phone (Samsung Galaxy S26 Ultra). You get a
goal and, each turn, the app in front read as text: numbered things on screen ([12] button "Send", [7] box
"Message" = "what's typed", [30] text "...", [4] list (scrolls)). Reply with the next steps, one JSON object per
line, then stop and wait for the new screen. Steps:
{"do":"open","app":"Rapido"}                 open an app by name
{"do":"open","url":"https://..."}            open a link
{"do":"click","id":12}                       tap it        {"do":"hold","id":12}   long-press it
{"do":"type","id":7,"text":"..."}            put text in a box (replaces what's there)
{"do":"paste","id":7}                        paste what was copied into a box
{"do":"copy","id":30}                        copy everything inside that thing
{"do":"key","keys":"back"}                   back, home, recents, notifications, enter (enter = the keyboard's go/send)
{"do":"scroll","dir":"down","id":null}       scroll (down/up/left/right) the list with that id, or the main one
{"do":"scroll_to","text":"Rhode","dir":"down"} scroll until something containing that text is on screen (done on the
                                             phone, fast: use this instead of scroll + look + scroll)
{"do":"wait"}                                wait until the screen stops changing (loading, an AI answering)
{"do":"wait","for":"text","gone":true}       wait until some text disappears (or appears, gone:false)
{"do":"read","id":30}                        full text of that thing (only if YOU need to know it)
{"do":"send_to_laptop","id":30,"name":"Hierarchy and ops"}   send the whole text of that thing (a note, a message,
                                             a page) to his laptop as a file, opened there in Notepad. Use this whenever he
                                             wants something from the phone on the laptop; open the note first, then send
                                             the element holding its body.
{"do":"look","question":"..."}               last resort when the screen text doesn't show what you need
{"do":"wait_user","say":"..."}              something only HE can do (fingerprint, app lock, face unlock, an OTP or
                                             PIN he types): tell him in a few words; you carry on once he's done it
{"do":"done","say":"..."}   {"do":"ask","say":"..."}   {"do":"fail","say":"..."}
Rules:
- Several steps per turn when you're sure. Use ids from THIS screen only.
- Open whatever app the goal needs yourself. Never ask him to open things.
- To get to an app ALWAYS use {"do":"open","app":"Name"}: never press home, hunt through home screens or search
  the app drawer. For a website, {"do":"open","url":"https://..."} goes straight there (for Chrome's incognito,
  open Chrome, tap its "Incognito" button or New Incognito tab, then type the address and press enter).
- If a step didn't change anything, don't repeat it: try another element, the app's own search, or a URL.
- BE DECISIVE. Anything harmless or undoable you just do, without asking: opening and navigating, picking the
  profile/account/tab/item the goal names (or the obvious one: his own name, "Sam", the first match),
  tapping "Continue watching"/"Resume"/"Play", skipping intros and ads, closing pop-ups, "Not now" on upsells,
  allowing the app what it needs to do the task, scrolling to find things, searching inside the app. Misheard
  names: pick the closest thing on screen. Ask only for a real choice you can't infer, and confirm only before
  sending, posting, booking, buying, paying or deleting.
- Batch steps that stay on the same screen (tap a box, type, enter) so it's fast.
- Moving text between apps: the app's Copy button or copy, then paste. Never retype long text.
- Before the step that sends a message or post to a PERSON, or books a ride, buys, pays or deletes anything,
  add "confirm":"<short plain question with the key details>", e.g. {"do":"click","id":9,"confirm":"Book the
  bike to your office for 85 rupees?"}. Asking an AI app like ChatGPT is not sending.
- Never type payment PINs, OTPs or passwords yourself: use wait_user so he does it.
- Text he gave word for word: type it exactly. A message YOU have to word: friends and casual chats get his
  Gen Z texting style ("on my way btw", "ngl that's fire", "lol bet", "fr tho"); elders and seniors (Mum, Dad,
  Nani, aunts, uncles), community/Jamaat groups, work chats and email get polite normal English, no slang.
- He wants the RESULT, not a commentary. Say "On it" once at the start, then nothing until the end unless a
  task runs long (then one short milestone like "Found it, checking the price"). Never narrate scrolling,
  tapping, what you can or can't see, retries or plan changes. The done line is the answer itself
  ("Rhode Peptide Lip Tint, 1,450 rupees, 4.4 stars, delivery tomorrow").
- Be fast: batch steps, use scroll_to to find things, read what's already on screen before scrolling more.
- After a step that opens a menu, a new page or an app, STOP that turn: the numbers change, so wait for the new
  screen before tapping anything in it (never "click menu, then click id 5" in one turn).
- Toggles: read the button's current label first. If it already says Disconnect / Turn off / Stop, the thing is
  ON already: don't press it. Before "done", check the screen really shows the result (e.g. "Connected",
  the incognito tab open); if not, keep going or say honestly what didn't work.
- If something didn't work, try another way (scroll, wait, a different element) before giving up.
- STUCK = STOP AND ASK (his rule). If two different tries haven't moved things on (same screen, same error),
  don't loop and don't guess: use ask with ONE short question saying what's in the way and what he should
  decide. Results starting "STUCK:" mean exactly that. Your ask pauses the task; his answer resumes it.
- DONE MEANS DONE. The done line says only what the screen shows now: no names, times, numbers or details you
  didn't read on it (never a driver, car or ETA that isn't on screen). A result starting "CHECK ONCE" is your
  one look before finishing: answer it, then stop. After done, nothing more.
- If the goal says to stop before the final confirm/book/send/pay step, never press it: say done with the
  fare, time and pickup on screen. A result starting "STOP:" means you reached that step."""


class Job:
    """Something laptop Friday asked the phone to do. Lines she should speak and yes/no questions travel
    back through inbox, so the laptop (where he is) does the talking."""

    def __init__(self, goal, kind="task", data=None):
        self.id = secrets.token_urlsafe(8)
        self.goal = goal
        self.kind = kind          # "task" (step by step on the phone) or "photo" (find a photo, send it here)
        self.data = data or {}
        self.path = ""            # a file the phone sent back (the photo)
        self.picked = threading.Event()
        self.finished = threading.Event()
        self.cancelled = False
        self.result = ""
        self.inbox = queue.Queue()  # ("say", text, None) | ("confirm", question, reply_queue)


_jobs = queue.Queue()   # waiting for the phone to pick up
_by_id = {}             # job id -> Job
_hands = {}             # phone task session -> (Brain, last used)


_icon = []


def _icon_data():
    """Her logo as a data URL, for the window's title bar and taskbar button."""
    if not _icon:
        import io
        import logo
        buf = io.BytesIO()
        logo.render(64).save(buf, "PNG")
        _icon.append("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
    return _icon[0]


def add_job(goal, kind="task", data=None):
    job = Job(goal, kind, data)
    _by_id[job.id] = job
    _jobs.put(job)
    return job


_offers = {}  # id -> path of files laptop Friday has handed to the phone


def offer(path):
    """Makes a laptop file downloadable by the phone (GET /file/<id>); returns its description."""
    fid = secrets.token_urlsafe(12)
    _offers[fid] = path
    return {"id": fid, "name": os.path.basename(path), "size": os.path.getsize(path)}


def _idle_seconds():
    """Seconds since his last keypress or mouse move on the laptop (the phone's "is he awake" check)."""
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return -1
    return int((ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000)


def phone_prompt(laptop_prompt):
    """The laptop's personality and how-he-speaks sections, verbatim, with phone actions instead of PC ones,
    so a change to her personality on the laptop reaches the phone too."""
    head = laptop_prompt.split("REPLY FORMAT.")[0]
    head = head.replace("living on his Windows 11 laptop", "living on his Windows 11 laptop and his phone")
    return head + PHONE_SECTION


def beacon(port):
    """Tells the phone where the laptop is: a tiny broadcast every few seconds on the local network (his
    hotspot or home Wi-Fi), so its IP never has to be typed in. Carries no secret; the phone still needs
    the token."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = json.dumps({"friday": port}).encode()
    while True:
        try:
            s.sendto(msg, ("255.255.255.255", 8766))
        except OSError:
            pass  # no network right now
        time.sleep(3)




# ---------------------------------------------------------------- her window's pages (local only)

SETTING_KEYS = ("assistant_name", "user_name", "hotkey", "pause_hotkey", "window_hotkey", "listen_mode", "barge_in",
                "confirm_risky_commands", "voice_engine", "kokoro_voice", "kokoro_speed", "edge_voice", "brain_model",
                "web_model", "announce_messages", "announce_groups", "announce_apps", "quiet_hours", "lsd_ear",
                "edge_voice_lsd", "edge_voice_gujarati", "panel_voice", "stt_device", "whisper_model", "office_wifi", "office_hours",
                "office_earphones", "office_phone_reminders", "office_cards", "office_strict_voice",
                "reply_suggestions", "reply_suggest_groups")

RESTART_KEYS = {"assistant_name", "user_name", "hotkey", "pause_hotkey", "window_hotkey", "listen_mode", "voice_engine",
                "kokoro_voice", "kokoro_speed", "edge_voice", "stt_device", "whisper_model"}


def _status():
    """Everything her window's sidebar and Status page show, read live from the running Friday."""
    f = _friday
    now = time.time()
    seen = getattr(f, "phone_seen", 0) if f else 0
    playing = getattr(f, "phone_playing", None) if f else None
    agents = []
    if f:
        for a in list(getattr(f, "agents", {}).values()):
            agents.append({"title": a.title, "step": getattr(a, "step", ""), "since": int(now - getattr(a, "started", now))})
    brain = f.brain if f else None
    alive = bool(brain and brain.proc and brain.proc.poll() is None)
    nxt = []
    for r in sorted((r for r in _J.REMINDERS.pending() if r.get("enabled", True) and r.get("when")), key=lambda r: r["when"])[:4]:
        nxt.append({"text": r["text"], "when": r["when"], "repeat": r.get("repeat")})
    health = ""
    try:
        with open(_J.LOG_PATH, "rb") as fh:
            fh.seek(max(0, os.path.getsize(_J.LOG_PATH) - 400_000))
            for line in fh.read().decode("utf-8", "ignore").splitlines():
                if "HEALTH ok" in line:
                    health = line[:19] + line[line.index("HEALTH ok") + 6:]
    except OSError:
        pass
    return {
        "state": getattr(f, "_last_state", "idle") if f else "off", "paused": bool(f and f.paused),
        "office": {"on": _J.office_on(), "how": _J.OFFICE.get("how", ""), "earphones": _J._EARS.get("v", False),
                   "wifi": _J.CFG.get("office_wifi", []), "hours": _J.CFG.get("office_hours") or None},
        "voice": bool(f and f.voice_on), "busy": bool(f and f.in_session), "language": _J.LANG.get("mode", "en"),
        "brain": {"alive": alive, "model": _J.CFG.get("brain_model", "haiku"), "turns": getattr(brain, "turns", 0) if brain else 0,
                  "blocked_until": _J.LIMIT.reset_words() if _J.LIMIT.blocked() else ""},
        "phone": {"seen": int(now - seen) if seen else None, "playing": playing[0] if playing and now - playing[1] < 90 else "",
                  "url": __import__("remote").current_url()},
        "agents": agents, "next": nxt, "health": health, "quiet_hours": _J.CFG.get("quiet_hours", []),
        "hearing": {"engine": _J.CFG.get("stt_engine", ""), "device": _J.CFG.get("stt_device", ""), "model": _J.CFG.get("whisper_model", ""),
                    "mode": _J.CFG.get("listen_mode", "")},
        "voice_engine": _J.CFG.get("voice_engine", ""), "voice_name": _J.CFG.get("edge_voice" if _J.CFG.get("voice_engine") == "edge" else "kokoro_voice", ""),
        "contacts": len(getattr(_J.CONTACTS, "items", []) or []), "now": datetime.datetime.now().isoformat(timespec="minutes"),
        "devices": {"laptop": _J.devices.laptop_line(), "phone": _J.devices.phone_line(), "laptop_data": _J.devices.laptop_status()},
    }


_friday = None
_J = None
BOOT = int(time.time())  # this run of Friday: her window sees it change after a restart


def start(J, friday=None):
    """J = the running jarvis module (Brain, MEMORY, CONTACTS, CFG, ask_claude...); friday = the running Jarvis,
    which does laptop jobs he asks for from the phone."""
    global _friday, _J
    _friday, _J = friday, J
    token_path = os.path.join(J.DATA, "bridge_token.txt")
    if not os.path.exists(token_path):
        with open(token_path, "w") as f:
            f.write(secrets.token_urlsafe(24))
    token = open(token_path).read().strip()
    brain = J.Brain(prompt=phone_prompt(J.BRAIN_PROMPT))
    shots = os.path.join(J.DATA, "phone_shots")

    def style_people():
        """{key: {names, examples}}: his real replies per person (data/style_people.json, never Sara)."""
        try:
            with open(J.STYLE_PEOPLE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def his_style():
        """How he writes (data/knowledge/his_style.md, kept by the nightly review), for the phone's reply suggestions."""
        try:
            with open(os.path.join(J.DATA, "knowledge", "his_style.md"), encoding="utf-8") as f:
                return f.read()[:6000]
        except OSError:
            return ""
    os.makedirs(shots, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _authed(self):
            if not secrets.compare_digest(self.headers.get("X-Friday-Token", ""), token):
                self.send_response(401)
                self.end_headers()
                return False
            return True

        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _local(self):
            """Only this laptop, asked by its own name (stops another site in his browser using a renamed host)."""
            host = (self.headers.get("Host") or "").split(":")[0]
            # the internet tunnel also connects from 127.0.0.1: anything it carries is never "this laptop"
            if self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For") or self.headers.get("Cf-Ray"):
                return False
            return self.client_address[0] == "127.0.0.1" and host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if self.path.split("?")[0] == "/panel":  # her window: the page carries the key for its own requests
                if not self._local():
                    self._json({"error": "local only"}, 403)
                    return
                html = open(os.path.join(J.HERE, "panel.html"), encoding="utf-8").read()
                html = html.replace("{{MARK}}", open(os.path.join(J.HERE, "panel_mark.svg"), encoding="utf-8").read())
                html = html.replace("{{ICON}}", _icon_data())
                data = html.replace("{{KEY}}", token).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/status":  # her window's Status page and sidebar: everything live, in one call
                if not self._local():
                    self._json({"error": "local only"}, 403)
                    return
                try:
                    self._json(_status())
                except Exception as e:  # shown in the page instead of lost on pythonw's stderr
                    log.exception("status page failed")
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            if self.path.split("?")[0] == "/history":  # a past day of conversation, or a search (?day=YYYY-MM-DD | ?q=words)
                import urllib.parse
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                day, q = qs.get("day", [""])[0], qs.get("q", [""])[0]
                if q:
                    self._json({"lines": J.MEMORY.search(q, days=90)[-200:]})
                elif day:
                    recs = [r for r in J.MEMORY.entries(since=day) if r["t"][:10] == day]
                    before = qs.get("before", [""])[0]  # her window: today's lines from before this run started
                    if before:
                        cut = datetime.datetime.fromtimestamp(int(before)).isoformat(timespec="seconds")
                        recs = [r for r in recs if r["t"] < cut]
                    self._json({"day": day, "digest": J.MEMORY.digests().get(day, ""),
                                "lines": [{"t": r["t"][11:16], "who": r["who"], "text": r["text"]} for r in recs][-600:]})
                else:  # the days she has, newest first, with how much was said and the digest
                    counts = {}
                    for r in J.MEMORY.entries(since=(datetime.date.today() - datetime.timedelta(days=60)).isoformat()):
                        if r["who"] in ("He", "Friday", "Jarvis"):
                            counts[r["t"][:10]] = counts.get(r["t"][:10], 0) + 1
                    dig = J.MEMORY.digests()
                    self._json({"days": [{"day": d, "lines": counts[d], "digest": dig.get(d, "")} for d in sorted(counts, reverse=True)]})
                return
            if self.path == "/memory/all":  # the Memory page: facts, open feedback, notes, what he taught her
                notes = sorted(os.listdir(os.path.join(J.DATA, "notes")), reverse=True) if os.path.isdir(os.path.join(J.DATA, "notes")) else []
                kfolder = os.path.join(J.DATA, "knowledge")
                know = sorted(n for n in os.listdir(kfolder) if n.endswith(".md")) if os.path.isdir(kfolder) else []
                self._json({"facts": J.MEMORY.facts(), "feedback": J.MEMORY.open_feedback(), "notes": notes,
                            "knowledge": [{"name": n, "size": os.path.getsize(os.path.join(kfolder, n))} for n in know]})
                return
            if self.path.split("?")[0] in ("/knowledge", "/note"):  # one file's text (?name=)
                import urllib.parse
                name = os.path.basename(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("name", [""])[0])
                folder = os.path.join(J.DATA, "knowledge" if self.path.startswith("/knowledge") else "notes")
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        self._json({"name": name, "text": f.read()})
                except OSError:
                    self._json({"error": "not found"}, 404)
                return
            if self.path == "/google":  # her own Google connectors: which are signed in, as whom
                self._json(J.google_conn.status())
                return
            if self.path == "/settings":
                self._json({"settings": {k: J.CFG.get(k) for k in SETTING_KEYS}, "restart_keys": sorted(RESTART_KEYS),
                            "language": J.LANG.get("mode", "en"), "office": {"on": J.office_on(), "how": J.OFFICE.get("how", "")}, "voices": {"kokoro": ["af_heart", "af_bella", "af_nicole", "af_sky", "am_adam", "am_michael", "bf_emma", "bm_george"]}})
                return
            if self.path == "/panel_font.ttf":  # her window's typeface (Plus Jakarta Sans, the phone app's)
                if not self._local():
                    self._json({"error": "local only"}, 403)
                    return
                data = open(os.path.join(J.HERE, "panel_font.ttf"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "font/ttf")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.split("?")[0] == "/dash":  # the reminders dashboard: the phone opens it with ?key=<token>
                import urllib.parse
                key = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("key", [""])[0]
                if not secrets.compare_digest(key, token) and not secrets.compare_digest(self.headers.get("X-Friday-Token", ""), token):
                    self._json({"error": "unauthorised"}, 401)
                    return
                html = open(os.path.join(J.HERE, "reminders.html"), encoding="utf-8").read()
                data = html.replace("{{ICON}}", _icon_data()).replace("{{KEY}}", token).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if not self._authed():
                return
            if self.path == "/ping":
                self._json({"ok": True, "name": "Friday (laptop)", "idle": _idle_seconds()})
            elif self.path == "/reminders":  # every live reminder and routine, with its on/off state
                self._json({"reminders": J.REMINDERS.pending(), "now": time.strftime("%Y-%m-%dT%H:%M")})
            elif self.path == "/routines":  # his routines grouped by section, the same data the dashboard shows
                self._json({"sections": J.REMINDERS.routines(), "now": time.strftime("%Y-%m-%dT%H:%M")})
            elif self.path.startswith("/archive"):  # ?since=iso: her memory for the phone to keep (it answers from it offline)
                import urllib.parse  # do_GET imports it locally elsewhere, so it's a local name: import it here too
                since = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("since", [""])[0] or None
                self._json(J.MEMORY.archive(since))
            elif self.path.startswith("/file/"):
                path = _offers.get(self.path[len("/file/"):])
                if not path or not os.path.isfile(path):
                    self._json({"error": "gone"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(os.path.getsize(path)))
                self.end_headers()
                with open(path, "rb") as f:
                    while chunk := f.read(1 << 16):
                        self.wfile.write(chunk)
            elif self.path.startswith("/events"):
                # her window's feed: long poll for anything after event number ?after=N
                after = int(self.path.partition("after=")[2] or 0)
                if friday is None:
                    self._json({"events": []})
                    return
                self._json({"events": friday.events_after(after), "voice": friday.voice_on,
                            "busy": friday.in_session, "paused": friday.paused, "boot": BOOT, "office": J.office_on()})
            elif self.path.split("?")[0] == "/jobs":
                # the phone waits here (long poll) for things laptop Friday wants done on it; it says what it's playing
                import urllib.parse
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                playing = qs.get("playing", [""])[0]
                if friday is not None:
                    friday.phone_playing = (playing, time.time()) if playing else None
                    friday.phone_seen = time.time()
                    friday.phone_caps = set(filter(None, qs.get("caps", [""])[0].split(",")))
                import devices
                devices.phone_seen(qs.get("st", [""])[0])
                try:
                    job = _jobs.get(timeout=25)
                except queue.Empty:  # the phone mirrors office mode from every poll (read now, not when the poll began)
                    self._json({"job": None, "office": J.office_on(), "brain": J.backup_brain.ACTIVE["name"]})
                    return
                if job.cancelled:
                    self._json({"job": None, "office": J.office_on(), "brain": J.backup_brain.ACTIVE["name"]})
                    return

                job.picked.set()
                self._json({"job": {"id": job.id, "goal": job.goal, "kind": job.kind, "data": job.data}, "office": J.office_on(), "brain": J.backup_brain.ACTIVE["name"]})
            elif self.path.startswith("/progress"):  # the phone's "task ongoing" pill: waits for a change (25 s max)
                import urllib.parse
                import progress
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                v, items = progress.phone_items(int((qs.get("after") or ["-1"])[0] or -1), 25.0)
                self._json({"v": v, "items": items})
            elif self.path == "/sync":
                self._json({
                    "vocabulary": J.CFG.get("vocabulary", []),
                    "vocabulary_dawat": J.CFG.get("vocabulary_dawat", []),  # his Dawat / Arabic / Urdu words
                    "heard_fixes": J.CFG.get("heard_fixes", {}),  # regex -> right spelling, for words the ears mishear
                    "people": J.CFG.get("people", []) + J.CONTACTS.frequent(40),
                    "pronunciations": J.CFG.get("pronunciations", {}),
                    "aliases": J.CFG.get("contact_aliases", {}),
                    "facts": J.MEMORY.facts(),
                    "recent": J.MEMORY.recent()[-20:],  # what was said lately, laptop and phone both
                    "speed": J.CFG.get("kokoro_speed", 1.25),
                    "prompt": phone_prompt(J.BRAIN_PROMPT),  # her personality for the phone's own fallback brain
                    "hands_prompt": PHONE_HANDS_PROMPT,        # step-by-step phone tasks when the laptop's away
                    "keys": __import__("backup_brain").load_keys(),  # his free keys: pasted once, here, for both
                    "openrouter_models": __import__("backup_brain").OPENROUTER_MODELS,
                    # reaching the laptop from anywhere: its internet address now, and where the newest one is posted
                    "remote_url": __import__("remote").current_url(),
                    "remote_topic": __import__("remote").topic(),
                    # the reminders dashboard: a page the phone can open (add this path to the laptop's address)
                    "reminders_page": "/dash?key=" + token,
                    "routines": J.REMINDERS.routines(),  # the same list, for the phone's own settings screen
                    # his reply suggestions on the phone: how he writes (kept by the nightly review) and the instruction
                    "style": his_style(),
                    "suggest_prompt": J.SUGGEST_PROMPT,
                    "style_people": style_people(),  # his real replies per person, from his chat exports
                    "style_learned": J.style_learned(),  # and the ones learnt since (instant learning)
                })
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._authed():
                return
            if self.path == "/upload":  # a file attached in her window: kept in data/uploads for a day
                n = int(self.headers.get("Content-Length") or 0)
                if n > 300 * 1024 * 1024:
                    self._json({"error": "too big"}, 413)
                    return
                from urllib.parse import unquote
                name = os.path.basename(unquote(self.headers.get("X-Name", "file"))) or "file"
                folder = os.path.join(J.DATA, "uploads")
                os.makedirs(folder, exist_ok=True)
                for old in os.listdir(folder):  # tidy: nothing kept longer than a day
                    p = os.path.join(folder, old)
                    if time.time() - os.path.getmtime(p) > 86400:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                if self.headers.get("X-Keep") == "downloads":  # a photo he wants on the laptop: a real file in Downloads\Friday
                    folder = J.OUT_DIR
                    os.makedirs(folder, exist_ok=True)
                    base, ext = os.path.splitext(name)
                    path, k = os.path.join(folder, name), 1
                    while os.path.exists(path):
                        k += 1
                        path = os.path.join(folder, f"{base} ({k}){ext}")
                else:
                    path = os.path.join(folder, f"{secrets.token_hex(4)}_{name}")
                with open(path, "wb") as f:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(left, 1 << 16))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                if left > 0 or n == 0:  # cut short (or nothing sent): don't leave a broken file behind
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    self._json({"error": "incomplete upload"}, 400)
                    return
                self._json({"path": path})
                return
            body = self._body()
            if self.path == "/ask":
                self._ask(body)
            elif self.path == "/style":  # a reply he sent from the phone's suggestions: the nightly review learns from it
                rec = {k: body.get(k) for k in ("channel", "to", "text", "their", "suggested", "picked", "edited")}
                rec["t"] = datetime.datetime.now().isoformat(timespec="seconds")
                rec["channel"] = rec.get("channel") or "whatsapp (phone)"
                if rec.get("text"):
                    J.learn_style(rec.get("to"), rec.get("their"), rec.get("text"))  # used in the very next suggestion
                    with open(J.STYLE_LOG, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._json({"ok": True})
            elif self.path == "/oneshot":
                self._json({"text": self._oneshot(body)})
            elif self.path == "/recall":  # the phone asks about the past: the laptop's log has both devices
                self._json({"text": J.recall(body.get("question", ""), body.get("keywords", ""), body.get("days", 30))})
            elif self.path == "/hands":
                self._json(self._hands(body))
            elif self.path == "/confirm":
                # a yes/no from a phone task the laptop started: asked out loud here, where he is
                job = _by_id.get(body.get("job", ""))
                if job is None or job.cancelled:
                    self._json({"yes": False})
                    return
                answer = queue.Queue()
                job.inbox.put(("confirm", body.get("question", "Go ahead?"), answer))
                try:
                    self._json({"yes": bool(answer.get(timeout=90))})
                except queue.Empty:
                    self._json({"yes": False})
            elif self.path == "/type" and friday is not None:  # typed in her window
                text, files = body.get("text", "").strip(), body.get("files") or []
                if text or files:
                    friday.emit(who="you", text=text, files=[os.path.basename(f) for f in files], via="typed")
                    friday.typed.put((text or "Have a look at this.", files))
                self._json({"ok": True})
            elif self.path == "/answer" and friday is not None:
                friday.panel_answers.put((body.get("id", ""), bool(body.get("yes"))))
                self._json({"ok": True})
            elif self.path == "/stop" and friday is not None:
                friday.panel_stop()
                self._json({"ok": True})
            elif self.path == "/listen" and friday is not None:
                friday.trigger.set()
                self._json({"ok": True})
            elif self.path == "/voice" and friday is not None:
                friday.voice_on = bool(body.get("on", True))
                self._json({"ok": True, "voice": friday.voice_on})
            elif self.path == "/file":  # the phone asks laptop Friday to read a file it uploaded
                say, full = J.work_with_file(body.get("path", ""), body.get("request", ""))
                self._json({"say": say, "full": full})
            elif self.path == "/memory":  # one memory for both Fridays: what the phone learnt, kept here too
                kind, text = body.get("kind", ""), body.get("text", "")
                if kind == "fact" and text:
                    J.MEMORY.remember(text)
                elif kind == "forget" and text:
                    J.MEMORY.forget(text)
                elif kind == "note" and text:
                    J.MEMORY.save_note(body.get("title", "note"), text)
                elif kind == "feedback" and text:
                    J.MEMORY.add_feedback(text)
                elif kind == "log":
                    for line in body.get("lines", [])[-200:]:
                        J.MEMORY.log(line.get("who", "He"), f"[phone] {line.get('text', '')}")
                elif kind == "delete":  # he deleted a chat or a message on the phone: gone from here too
                    J.MEMORY.drop({line.get("text", "") for line in body.get("lines", [])})
                elif kind == "new_chat":  # a fresh chat on the phone: her phone brain starts clean
                    brain.close()
                self._json({"ok": True})
            elif self.path == "/say":
                job = _by_id.get(body.get("job", ""))
                if job is not None:
                    job.inbox.put(("say", body.get("text", ""), None))
                self._json({"ok": True})
            elif self.path == "/phone_job":
                # for testing from a script: runs a phone job, answers its yes/no with body["confirm"] (default no)
                job = add_job(body.get("goal", ""), body.get("kind", "task"), body.get("data"))
                says, end = [], time.time() + float(body.get("timeout", 300))
                while not job.finished.is_set() and time.time() < end:
                    try:
                        kind, text, answer = job.inbox.get(timeout=0.5)
                    except queue.Empty:
                        if not job.picked.is_set() and time.time() > end - float(body.get("timeout", 300)) + 15:
                            break
                        continue
                    says.append(("ASK: " if kind == "confirm" else "") + text)
                    if answer is not None:
                        answer.put(bool(body.get("confirm", False)))
                job.cancelled = not job.finished.is_set()
                self._json({"picked": job.picked.is_set(), "done": job.finished.is_set(), "says": says, "text": job.result,
                            "path": job.path})
            elif self.path == "/reminders/set":  # {"id"|"name"|"about", "enabled": true|false}: off keeps it, on restarts it
                about = str(body.get("id") or body.get("name") or body.get("about") or "")
                enabled = body.get("enabled", True)
                if not isinstance(enabled, bool):
                    enabled = str(enabled).strip().lower() in ("true", "1", "on", "yes")
                changed = J.REMINDERS.set_enabled(about, enabled)
                J.reminders_changed()
                if changed:
                    J.MEMORY.log("Action", f"[{'window' if self._local() else 'phone'}] he switched reminders {'on' if enabled else 'off'}: "
                                 + "; ".join(r["text"] for r in changed))
                self._json({"changed": changed, "reminders": J.REMINDERS.pending()})
            elif self.path == "/reminders/edit":  # {"id", "text"?, "time": "HH:MM"?, "when"?, "repeat"?, "description"?}
                try:
                    r = J.REMINDERS.edit(body.get("id"), text=body.get("text"), time_of_day=body.get("time"), when=body.get("when"),
                                         repeat=body.get("repeat"), description=body.get("description"), remark=body.get("remark"),
                                         window=body.get("window"), pause_hours=body.get("pause_hours"), enabled=body.get("enabled"))
                except (ValueError, TypeError) as e:
                    self._json({"error": f"bad value: {e}"}, 400)
                    return
                if r:
                    J.reminders_changed()
                    bits = [f"{k}: {body[k]}" for k in ("text", "time", "repeat", "remark", "window", "description", "pause_hours", "enabled")
                            if body.get(k) is not None and k != "id"]
                    where = "her window" if self._local() else "his phone's settings"
                    J.MEMORY.log("Action", f"[{'window' if self._local() else 'phone'}] he edited the reminder '{r['text']}' in {where} (" +
                                 ", ".join(bits) + f"); now {r.get('when') or r.get('trigger')} {r.get('repeat') or 'once'}"
                                 + (f"; his note: {r['remark']}" if r.get("remark") else ""))
                self._json({"changed": r, "routines": J.REMINDERS.routines(), "reminders": J.REMINDERS.pending()}, 200 if r else 404)
            elif self.path == "/reminders/batch":  # {"changes": [{id, ...edit fields}], "deletes": [id], "adds": [{text, time, ...}]}
                # his Save on the routines screen (phone or her window): everything at once, then she confirms it
                where = "window" if self._local() else "phone"
                if body.get("offline"):  # made on the phone while the laptop was away, sent now it's back
                    where = "phone, while the laptop was away"
                done = J.save_reminder_changes(body.get("changes") or [], body.get("deletes") or [], body.get("adds") or [], where)
                self._json({**done, "routines": J.REMINDERS.routines(), "reminders": J.REMINDERS.pending()})
            elif self.path == "/reminders/change":  # {"about", ...edit fields, "hours"?}: the phone brain's edit/pause by name
                hits = J.REMINDERS.find(body.get("about") or body.get("name") or "")
                changed = []
                for h in hits[:4]:
                    r = J.REMINDERS.edit(h["id"], text=body.get("text"), time_of_day=body.get("time"), when=body.get("when"),
                                         repeat=body.get("repeat"), remark=body.get("remark"), window=body.get("window"),
                                         pause_hours=body.get("hours", body.get("pause_hours")), enabled=body.get("enabled"))
                    if r:
                        changed.append(r)
                if changed:
                    J.reminders_changed()
                    J.MEMORY.log("Action", "[phone] he changed reminders by voice: " + "; ".join(
                        f"'{r['text']}' now {r.get('when') or ''} {r.get('repeat') or 'once'}"
                        + (f", paused until {r['paused_until']}" if r.get("paused_until") else "") for r in changed))
                self._json({"changed": changed, "text": ("Changed: " + "; ".join(r["text"] for r in changed)) if changed
                            else f"No reminder matches '{body.get('about')}'."})
            elif self.path == "/reminders/delete":  # {"id"}

                r = J.REMINDERS.delete(body.get("id"))
                if r:
                    J.reminders_changed()
                    J.MEMORY.log("Action", f"[{'window' if self._local() else 'phone'}] he deleted the reminder '{r['text']}'")
                self._json({"deleted": r, "routines": J.REMINDERS.routines(), "reminders": J.REMINDERS.pending()}, 200 if r else 404)
            elif self.path == "/chat/new" and friday is not None:  # her window's New chat: a fresh brain, a line in the feed
                friday.brain.turns = 99  # stale: the next refresh starts a clean process (memory still goes in)
                threading.Thread(target=friday.brain.refresh, daemon=True).start()
                J.MEMORY.log("Action", "new chat (her window)")
                friday.emit(type="divider", text="New chat")
                self._json({"ok": True})
            elif self.path == "/reminders/add":  # {"text", "time": "HH:MM"?, "when": iso?, "repeat"?, "kind"?}
                text = (body.get("text") or "").strip()
                if not text:
                    self._json({"error": "no text"}, 400)
                    return
                when = body.get("when")
                if not when and body.get("time"):
                    hh, mm = int(body["time"][:2]), int(body["time"][3:5])
                    t = datetime.datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if t <= datetime.datetime.now():
                        t += datetime.timedelta(days=1)
                    when = t.isoformat(timespec="minutes")
                rp = (body.get("repeat") or "").strip().lower()
                r = J.REMINDERS.add(text, when=when, kind=body.get("kind") or "reminder", repeat=None if rp in ("", "once") else rp,
                                    remark=body.get("remark"))
                extra = {k: body[k] for k in ("ring", "ring_between", "window", "description") if body.get(k)}
                if extra:  # phone-made reminders keep how they ring
                    with J.REMINDERS.lock:
                        items = J.REMINDERS.load()
                        for x in items:
                            if x["id"] == r["id"]:
                                x.update(extra)
                        J.REMINDERS.save(items)
                J.reminders_changed()
                J.MEMORY.log("Action", f"[{'window' if self._local() else 'phone'}] he added a reminder: '{text}' at {when or '?'} {rp or 'once'}"
                             + (f"; his note: {body['remark']}" if body.get("remark") else ""))
                self._json({"added": r, "routines": J.REMINDERS.routines(), "reminders": J.REMINDERS.pending()})
            elif self.path == "/knowledge":  # {"name", "text"}: a file in data/knowledge (loaded at her next start; the phone on its next sync)
                name = os.path.basename((body.get("name") or "").strip())
                if not name.endswith(".md"):
                    name += ".md"
                name = re.sub(r"[^\w\-. ]", "", name).strip() or "notes.md"
                folder = os.path.join(J.DATA, "knowledge")
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                    f.write((body.get("text") or "").rstrip() + "\n")
                J.MEMORY.log("Action", f"[window] he edited what he taught her: {name}")
                self._json({"ok": True, "name": name, "restart_needed": True})
            elif self.path == "/settings":  # {key: value, ...} for SETTING_KEYS; "language" switches LSD mode live
                changed, needs_restart = [], []
                for k, v in body.items():
                    if k == "language":
                        J.set_language("lsd" if v == "lsd" else "en")
                        changed.append(k)
                    elif k in SETTING_KEYS and J.CFG.get(k) != v:
                        J.CFG[k] = v
                        changed.append(k)
                        if k in RESTART_KEYS:
                            needs_restart.append(k)
                if changed:
                    cfg = J.load_config()
                    for k in changed:
                        if k != "language":
                            cfg[k] = J.CFG[k]
                    with open(J.CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    J.MEMORY.log("Action", "[window] settings changed: " + ", ".join(changed))
                self._json({"changed": changed, "restart_needed": needs_restart})
            elif self.path.startswith("/google/"):  # her own Google connectors: /google/client, /connect, /disconnect
                G = J.google_conn
                try:
                    if self.path == "/google/client":  # the Desktop-app OAuth client: its downloaded JSON, or id + secret
                        if not self._local():
                            self._json({"error": "set the Google app up from the laptop"}, 403)
                            return
                        G.save_client(body.get("json") or body.get("client_id", ""), body.get("client_secret", ""))
                        J.MEMORY.log("Action", "[window] Google app for her connectors set up")
                        self._json({"ok": True, **G.status()})
                    elif self.path == "/google/connect":  # opens Google's sign-in in the laptop's browser
                        url = G.connect(body.get("service", ""))
                        self._json({"ok": True, "url": url})
                    elif self.path == "/google/disconnect":
                        G.disconnect(body.get("service", ""))
                        J.MEMORY.log("Action", f"[window] Google connector {body.get('service')} disconnected")
                        self._json({"ok": True, **G.status()})
                    else:
                        self._json({"error": "not found"}, 404)
                except Exception as e:
                    self._json({"ok": False, "error": str(e)})
            elif self.path == "/office":  # {"on": true|false} from her window or the phone; {} just reads it
                if body.get("remember_wifi"):  # {"remember_wifi": true}: the Wi-Fi the laptop is on now switches it on by itself
                    ssid = J.devices.laptop_status(max_age=5, wait=True).get("ssid") or ""
                    if ssid:
                        J.CFG["office_wifi"] = sorted(set(J.CFG.get("office_wifi", []) + [ssid]))
                        cfg = J.load_config()
                        cfg["office_wifi"] = J.CFG["office_wifi"]
                        with open(J.CONFIG_PATH, "w", encoding="utf-8") as f:
                            json.dump(cfg, f, ensure_ascii=False, indent=2)
                    self._json({"wifi": J.CFG.get("office_wifi", []), "added": ssid})
                    return
                if "on" in body:
                    J.log.info("office switch asked: %s (from %s)", "on" if body["on"] else "off", body.get("from") or self.client_address[0])
                if "on" in body and bool(body["on"]) != J.office_on():
                    J.office_switch(bool(body["on"]))
                    if friday is not None:
                        friday.status("idle", "Office mode on: I'll stay quiet" if J.office_on() else "Office mode off", hide_after=4)
                self._json({"on": J.office_on(), "how": J.OFFICE.get("how", "")})
            elif self.path == "/pause" and friday is not None:  # {"on": true} = Friday on

                want_paused = not bool(body.get("on", True))
                if friday.paused != want_paused:
                    friday.toggle_pause()
                self._json({"paused": friday.paused})
            elif self.path == "/restart" and friday is not None:
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.5), friday.restart_self()), daemon=True).start()
            elif self.path == "/jobs/done":
                job = _by_id.pop(body.get("id", ""), None)
                if job is not None:
                    job.result = body.get("text", "")
                    job.path = body.get("path", "")
                    job.finished.set()
                self._json({"ok": True})
            elif self.path == "/laptop":
                if friday is None:
                    self._json({"text": "Laptop Friday isn't fully running right now.", "files": []})
                elif body.get("stream"):
                    # NDJSON: {"say"} the moment she says it (progress while the job runs), then {"done","files"}
                    lines, result = queue.Queue(), {}

                    def work():
                        try:
                            result.update(friday.remote_request(body.get("request", ""), bool(body.get("confirmed")),
                                                                on_line=lines.put))
                        except Exception as e:
                            result.update({"text": f"That didn't work on the laptop: {str(e)[:120]}", "files": []})
                        finally:
                            lines.put(None)
                    threading.Thread(target=work, daemon=True).start()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    try:
                        while True:
                            try:
                                say = lines.get(timeout=20)
                            except queue.Empty:
                                self.wfile.write(b"{}\n")  # keep-alive: the tunnel drops a silent connection
                                self.wfile.flush()
                                continue
                            if say is None:
                                break
                            self.wfile.write((json.dumps({"say": say}, ensure_ascii=False) + "\n").encode())
                            self.wfile.flush()
                        self.wfile.write((json.dumps({"done": True, "files": result.get("files", [])},
                                                     ensure_ascii=False) + "\n").encode())
                        self.wfile.flush()
                    except OSError:
                        pass  # the phone hung up; the job carries on and its result is in her memory
                else:
                    self._json(friday.remote_request(body.get("request", ""), bool(body.get("confirmed"))))
            else:
                self._json({"error": "not found"}, 404)

        def _ask(self, body):
            text, label = body.get("text", ""), body.get("label", "He says to you")
            t0 = time.time()
            if label.startswith("Your own idea"):  # the phone bringing something up itself, not his words
                J.MEMORY.log("Action", f"[phone] Friday brought up: {text[:160]}")
            else:
                J.MEMORY.log("He", f"[phone] {text}")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            replies = brain.ask(text, "Talking on his phone. " + body.get("context", ""), label=label)
            first = True
            while True:
                try:
                    kind, item = replies.get(timeout=90)
                except queue.Empty:
                    kind, item = "end", None
                if kind == "say":
                    say, mood = item if isinstance(item, tuple) else (item, None)
                    if first:
                        log.info("phone: first line after %.1fs", time.time() - t0)
                        first = False
                    J.MEMORY.log("Friday", f"[phone] {say}")
                    line = {"say": say, "mood": mood}
                elif kind == "act":
                    line = {"act": item}
                elif kind == "listen":
                    line = {"listen": True}
                else:
                    line = {"end": True}
                try:
                    self.wfile.write((json.dumps(line, ensure_ascii=False) + "\n").encode())
                    self.wfile.flush()
                except OSError:
                    return  # phone went away; let the reply finish in the background
                if kind == "end":
                    threading.Thread(target=brain.refresh, daemon=True).start()
                    return

        def _hands(self, body):
            """One turn of a step-by-step phone task: the phone sends its screen as text, this laptop's cheap
            Claude model sends back the next steps (the same loop as laptop tasks, same plan usage)."""
            sid, job = body.get("session", ""), _by_id.get(body.get("job") or "")
            if job is not None and job.cancelled:
                return {"cancel": True, "say": [], "steps": []}
            now = time.time()
            for k, (b, used) in list(_hands.items()):  # tidy up tasks that ended without saying so
                if now - used > 600:
                    b.close()
                    _hands.pop(k, None)
            if body.get("end"):
                b = _hands.pop(sid, (None, 0))[0]
                if b is not None:
                    threading.Thread(target=b.close, daemon=True).start()
                return {"ok": True}
            if J.LIMIT.blocked():
                return {"limited": True, "say": [], "steps": []}  # the phone uses its free fallback instead
            b = _hands.get(sid, (None, 0))[0] or J.Brain(prompt=PHONE_HANDS_PROMPT,
                                                         model=J.CFG.get("hands_model", "haiku"), memory=False)
            _hands[sid] = (b, now)
            msg = ((f"Goal: {body['goal']}\n" if body.get("goal") else "")
                   + (f"Results: {body['results']}\n" if body.get("results") else "") + body.get("screen", ""))
            replies = b.ask(msg, f"Turn {body.get('turn', 1)}.", label="Phone screen now")
            says, steps = [], []
            while True:
                try:
                    kind, item = replies.get(timeout=90)
                except queue.Empty:
                    break
                if kind == "end":
                    break
                if kind == "say" and item:
                    text = item[0] if isinstance(item, tuple) else item
                    if job is not None:
                        if J.quiet_progress(text, job.data.setdefault("_progress", {})):
                            job.inbox.put(("say", text, None))  # laptop task: she says it at the laptop
                    else:
                        says.append(text)
                elif kind == "act":
                    steps.append(item)
            return {"say": says, "steps": steps}

        def _oneshot(self, body):
            system, prompt = body.get("system", "You are FRIDAY."), body.get("prompt", "")
            if body.get("image"):
                path = os.path.join(shots, time.strftime("phone %Y-%m-%d %H.%M.%S.jpg"))
                with open(path, "wb") as f:
                    f.write(base64.b64decode(body["image"]))
                try:
                    return J.ask_claude(system + " Read the screenshot file you're given.",
                                        f"Screenshot of his phone: {path}\n{prompt}", model="sonnet", tools="Read",
                                        timeout=120, cwd=shots)
                finally:  # no leftovers: he asked for screenshots to be deleted after use
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            if body.get("search"):  # streamed: the phone's pill shows the sites as they're read
                return J.claude_web(system, prompt, to_phone=True)
            return J.ask_claude(system, prompt, timeout=90)

    port = int(J.CFG.get("bridge_port", 8765))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("phone bridge listening on port %d", port)
    threading.Thread(target=beacon, args=(port,), daemon=True).start()
    brain.warm()
    server.serve_forever()
