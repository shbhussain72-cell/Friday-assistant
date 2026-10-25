"""Friday's backup brain for when his Claude plan limit runs out: Mistral's free tier first (the biggest
allowance), then Google Gemini, then Groq, the same order as the phone app (Friday-Android Brain.kt).
Free keys go in data\\keys.txt, one per line; they're recognised by their shape (Gemini keys start "AIza" or
"AQ.", Groq "gsk_", Tavily web search "tvly-"; a Mistral key has no prefix, so it's the bare 32-letter one,
or a line "mistral=<key>").

Also tracks what Claude reports about the plan limit (rate_limit_event / a usage-limit error), so the
laptop brain knows when to switch over and when to try Claude again."""
import json
import logging
import os
import re
import threading
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_PATH = os.path.join(HERE, "data", "keys.txt")
GEMINI_MODEL = "gemini-flash-lite-latest"
GROQ_MODEL = "openai/gpt-oss-120b"
MISTRAL_MODEL = "mistral-medium-latest"
# OpenRouter's free models (":free") come and go, so a few in order of preference; one that's gone paid, refused or
# busy is skipped (a day, or a few minutes when it's just busy). Tested 10 Oct 2026: nemotron super ~2.5 s, in character.
OPENROUTER_MODELS = ["nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-31b-it:free",
                     "google/gemma-4-26b-a4b-it:free", "nvidia/nemotron-3-ultra-550b-a55b:free"]
_or_resting = {}  # model -> time it can be tried again
MAX_OUT = 600
MAX_TURNS = 12  # user + model lines kept, so a long chat doesn't grow the request forever

log = logging.getLogger("friday")


# ---------------------------------------------------------------- Claude plan limit

class ClaudeLimit:
    """When Claude says the plan limit is used up, until when, and how full the plan is."""

    def __init__(self):
        self.until = 0.0  # epoch seconds; Claude is skipped until then
        self.used = {}  # {"five_hour": 0.07, "seven_day": 0.05}

    def blocked(self):
        return time.time() < self.until

    def nearly_used(self, at=0.8):
        """True once the 5-hour or weekly plan window is at least this full."""
        return any(u >= at for u in self.used.values())

    def note_event(self, ev):
        """Reads a stream-json line from any claude -p run. Returns True if it says we're out."""
        if ev.get("type") == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            for name, w in (info.get("unifiedWindows") or {}).items():
                if isinstance(w, dict) and w.get("utilization") is not None:
                    self.used[name] = w["utilization"]
            if info.get("status") == "rejected":
                self.hit(info.get("resetsAt"))
                return True
        elif ev.get("type") == "result" and ev.get("is_error") and is_limit_text(ev.get("result")):
            self.hit()
            return True
        return False

    def hit(self, resets_at=None):
        # no reset time given: try Claude again in 30 min (a wasted try costs nothing, it just fails fast)
        until = float(resets_at) if resets_at else time.time() + 1800
        if until > self.until:
            self.until = until
            log.warning("Claude plan limit reached: using the backup brain until %s",
                        time.strftime("%H:%M", time.localtime(until)))

    def reset_words(self):
        return time.strftime("%I:%M %p", time.localtime(self.until)).lstrip("0")


def is_limit_text(text):
    t = (text or "").lower()
    return "limit" in t and any(w in t for w in ("usage", "hit your", "reached", "resets"))


LIMIT = ClaudeLimit()


# ---------------------------------------------------------------- backup providers

def load_keys():
    keys = {}
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            for raw in f:
                if raw.lstrip().startswith("#"):
                    continue
                name, _, val = raw.strip().rpartition("=")
                k = val.strip().strip("\"'")
                if name.strip().lower() in ("gemini", "groq", "mistral", "openrouter", "tavily") and k:
                    keys[name.strip().lower()] = k
                    continue
                if k.startswith(("AIza", "AQ.")):  # Google's older and newer key formats
                    keys["gemini"] = k
                elif k.startswith("gsk_"):
                    keys["groq"] = k
                elif k.startswith("sk-or-"):
                    keys["openrouter"] = k
                elif k.startswith("mstr") or re.fullmatch(r"[A-Za-z0-9]{32}", k):  # newer and older Mistral keys
                    keys["mistral"] = k
                elif k.startswith("tvly-"):
                    keys["tavily"] = k
    except OSError:
        pass
    return keys


class Quota:
    """Skips a provider after it says it's out, until its reset (Gemini's daily quota resets at midnight
    Pacific; Groq says when in retry-after)."""

    def __init__(self):
        self.out_until = {}

    def ok(self, name):
        return time.time() >= self.out_until.get(name, 0)

    def out(self, name, seconds):
        self.out_until[name] = time.time() + seconds
        log.warning("backup %s out of free quota for %d min", name, seconds // 60)


QUOTA = Quota()


class HttpError(Exception):
    def __init__(self, code, body, retry_after=None):
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code, self.body, self.retry_after = code, body, retry_after


def _post(url, body, headers, stream=True):
    r = requests.post(url, json=body, headers=headers, stream=stream, timeout=(8, 30))
    if r.status_code != 200:
        ra = r.headers.get("retry-after")
        raise HttpError(r.status_code, r.text, float(ra) if ra and ra.replace(".", "").isdigit() else None)
    r.encoding = "utf-8"  # event streams don't name a charset, and requests would guess Latin-1 ("Iâm")
    return r


def _sse(r):
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("data:"):
            d = line[5:].strip()
            if d and d != "[DONE]":
                try:
                    yield json.loads(d)
                except ValueError:
                    pass


def _gemini(key, system, turns, search=False):
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": role, "parts": [{"text": t}]} for role, t in turns],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": MAX_OUT}}
    if search:
        body["tools"] = [{"google_search": {}}]
    r = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:streamGenerateContent?alt=sse",
              body, {"x-goog-api-key": key})
    for d in _sse(r):
        for part in ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []:
            if part.get("text"):
                yield part["text"]


def _groq(key, system, turns, search=False, model=None, max_tokens=None):
    model = model or GROQ_MODEL
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": "assistant" if role == "model" else "user", "content": t} for role, t in turns]
    body = {"model": model, "messages": msgs, "stream": True, "temperature": 0.6, "max_tokens": max_tokens or MAX_OUT + 200}
    if search:
        body["tools"] = [{"type": "browser_search"}]  # gpt-oss on Groq searches the web itself
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
    r = _post("https://api.groq.com/openai/v1/chat/completions", body, {"Authorization": f"Bearer {key}"})
    for d in _sse(r):
        text = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content")
        if text:
            yield text


def _mistral(key, system, turns, search=False):
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": "assistant" if role == "model" else "user", "content": t} for role, t in turns]
    body = {"model": MISTRAL_MODEL, "messages": msgs, "stream": True, "temperature": 0.6, "max_tokens": MAX_OUT}
    r = _post("https://api.mistral.ai/v1/chat/completions", body, {"Authorization": f"Bearer {key}"})
    for d in _sse(r):
        text = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content")
        if text:
            yield text


def _openrouter(key, system, turns, search=False, models=None, max_tokens=None):
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": "assistant" if role == "model" else "user", "content": t} for role, t in turns]
    r, last = None, None
    for model in (models or OPENROUTER_MODELS):
        if time.time() < _or_resting.get(model, 0):
            continue
        body = {"model": model, "messages": msgs, "stream": True, "temperature": 0.6, "max_tokens": max_tokens or MAX_OUT,
                "reasoning": {"enabled": False}}  # a free "thinking" model otherwise spends the whole reply thinking
        try:
            r = _post("https://openrouter.ai/api/v1/chat/completions", body,
                      {"Authorization": f"Bearer {key}", "X-Title": "Friday"})
            break
        except HttpError as e:
            last = e
            _or_resting[model] = time.time() + (300 if e.code == 429 else 86400)
            log.info("openrouter %s skipped (HTTP %s)", model, e.code)
    if r is None:
        raise last or HttpError(429, "every free OpenRouter model is busy")
    for d in _sse(r):
        text = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content")
        if text:
            yield text


def tavily_search(query, max_results=5, time_range="week"):
    """Web search made for assistants (free key, ~1,000 a month): [{title, url, content}], [] without a key."""
    key = load_keys().get("tavily")
    if not key:
        return []
    r = requests.post("https://api.tavily.com/search", timeout=(8, 30), headers={"Authorization": f"Bearer {key}"},
                      json={"query": query, "max_results": max_results, "search_depth": "basic", "time_range": time_range})
    if r.status_code != 200:
        log.warning("tavily search failed: HTTP %s %s", r.status_code, r.text[:200])
        return []
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "content": (x.get("content") or "")[:500]}
            for x in r.json().get("results", [])]


def _secs_to_pacific_midnight():
    try:
        from zoneinfo import ZoneInfo
        import datetime as dt
        now = dt.datetime.now(ZoneInfo("America/Los_Angeles"))
        return int((now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1) - now).total_seconds())
    except Exception:
        return 6 * 3600


ACTIVE = {"name": "claude", "t": 0.0}  # the brain that answered last (shown on the phone: only the API in use)


def mark(name):
    ACTIVE.update(name=name, t=time.time())


def stream(system, turns, search=False):
    """Yields text chunks of one reply from the first backup that works. Raises RuntimeError if none can.
    A provider that fails before sending anything is skipped; one that fails mid-reply just ends the reply."""
    keys = load_keys()
    if not keys:
        raise RuntimeError("no backup keys")
    tries = [("gemini", lambda: _gemini(keys["gemini"], system, turns, search))] if "gemini" in keys else []
    if "openrouter" in keys and not search:  # free models, no web search
        tries.insert(0, ("openrouter", lambda: _openrouter(keys["openrouter"], system, turns)))
    if "mistral" in keys and not search:  # only works on a paid Mistral plan now; skipped after a 429
        tries.insert(0, ("mistral", lambda: _mistral(keys["mistral"], system, turns)))
    if "groq" in keys:
        groq = ("groq", lambda: _groq(keys["groq"], system, turns, search))
        # Gemini's free tier refuses Google Search ("quota exceeded"), so web lookups go to Groq first
        tries.insert(0, groq) if search else tries.append(groq)
    for name, make in tries:
        if not QUOTA.ok(name):
            continue
        sent = False
        try:
            for chunk in make():
                if not sent:
                    mark(name)
                sent = True
                yield chunk
            log.info("backup brain answered with %s", name)
            return
        except HttpError as e:
            log.warning("backup %s failed: %s", name, e)
            if e.code == 429 and not (search and name == "gemini"):  # a refused search says nothing about chat quota
                daily = name == "gemini" and "perday" in e.body.lower()
                # a free Mistral key is refused outright now (paid plans only): rest it for hours, not a minute
                QUOTA.out(name, _secs_to_pacific_midnight() if daily else 6 * 3600 if name == "mistral" else int(e.retry_after or 60))
        except Exception as e:
            log.warning("backup %s failed: %s", name, e)
        if sent:
            return
    raise RuntimeError("no backup could answer")


def one_shot(system, prompt, search=False):
    """A whole answer as one string, or "" if no backup could answer."""
    try:
        text = "".join(stream(system, [("user", prompt)], search=search))
        return re.sub(r"\s*【[^】]*】", "", text).strip()  # Groq's search citations, e.g. 【1†L5-L7】
    except RuntimeError:
        return ""


class Chat:
    """The backup brain's short memory of the conversation (Claude's process keeps its own)."""

    def __init__(self):
        self.turns = []
        self.lock = threading.Lock()

    def add(self, role, text):
        with self.lock:
            self.turns.append((role, text))
            while len(self.turns) > MAX_TURNS or (self.turns and self.turns[0][0] != "user"):
                self.turns.pop(0)

    def drop_last(self):
        with self.lock:
            if self.turns:
                self.turns.pop()

    def __len__(self):
        return len(self.turns)
