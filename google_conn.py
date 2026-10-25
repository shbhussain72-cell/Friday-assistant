"""Her own Google connectors: Gmail, Google Calendar and Google Drive, each signed in separately ("Sign in with Google"
in her window), so each can be a different Google account and none depends on his Claude account's connectors.

How it signs in: an installed-app OAuth client of his own (data/google_client.json, the JSON Google Cloud Console
downloads for a "Desktop app" client, or google_client_id= / google_client_secret= lines in data/keys.txt). The browser
opens Google's consent page, Google sends the code back to a one-off page on 127.0.0.1, and the refresh token is kept
in data/google/<service>.json (never leaves the laptop). Plain HTTPS (urllib), no Google libraries.

Used by google_mcp.py (tools for the claude brain), by friday.ask_google, and by bridge.py (/google routes)."""
import base64
import datetime as dt
import email.utils
import hashlib
import html
import http.server
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.mime.text import MIMEText

log = logging.getLogger("friday")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TOKENS = os.path.join(DATA, "google")
CLIENT_PATH = os.path.join(DATA, "google_client.json")

SERVICES = {
    "gmail": {"name": "Gmail", "scopes": ["https://www.googleapis.com/auth/gmail.modify"]},
    "calendar": {"name": "Google Calendar", "scopes": ["https://www.googleapis.com/auth/calendar"]},
    "drive": {"name": "Google Drive", "scopes": ["https://www.googleapis.com/auth/drive"]},
}
ID_SCOPES = ["openid", "email", "profile"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
TZ = "Asia/Kolkata"
_lock = threading.Lock()
_pending = {}  # service -> {"url", "started", "error", "done"} while a sign-in is open in the browser


class GoogleError(RuntimeError):
    pass


# ---------------------------------------------------------------- the OAuth client (his own Google Cloud app)

def client():
    """(client_id, client_secret) or None."""
    try:
        j = json.load(open(CLIENT_PATH, encoding="utf-8"))
        c = j.get("installed") or j.get("web") or j
        if c.get("client_id"):
            return c["client_id"], c.get("client_secret", "")
    except (OSError, ValueError):
        pass
    try:
        keys = {}
        for ln in open(os.path.join(DATA, "keys.txt"), encoding="utf-8"):
            if "=" in ln:
                k, v = ln.split("=", 1)
                keys[k.strip().lower()] = v.strip()
        if keys.get("google_client_id"):
            return keys["google_client_id"], keys.get("google_client_secret", "")
    except OSError:
        pass
    return None


def save_client(text_or_id, secret=""):
    """Stores the OAuth client: the downloaded JSON pasted whole, or an id and a secret."""
    text_or_id = (text_or_id or "").strip()
    if text_or_id.startswith("{"):
        j = json.loads(text_or_id)
        c = j.get("installed") or j.get("web") or j
        if not c.get("client_id"):
            raise GoogleError("that JSON has no client_id")
        data = {"installed": {"client_id": c["client_id"], "client_secret": c.get("client_secret", "")}}
    else:
        if not text_or_id.endswith(".apps.googleusercontent.com"):
            raise GoogleError("a client ID ends in .apps.googleusercontent.com")
        data = {"installed": {"client_id": text_or_id, "client_secret": secret.strip()}}
    os.makedirs(DATA, exist_ok=True)
    json.dump(data, open(CLIENT_PATH, "w", encoding="utf-8"), indent=2)
    return True


# ---------------------------------------------------------------- tokens

def _token_path(service):
    return os.path.join(TOKENS, f"{service}.json")


def _load(service):
    try:
        return json.load(open(_token_path(service), encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(service, tok):
    os.makedirs(TOKENS, exist_ok=True)
    json.dump(tok, open(_token_path(service), "w", encoding="utf-8"), indent=2)


def connected(service):
    tok = _load(service)
    return bool(tok and tok.get("refresh_token"))


def account(service):
    tok = _load(service)
    return (tok or {}).get("account", "")


def status():
    """For her window and the brain: each connector, its account, and any sign-in in progress."""
    out = {"client": bool(client()), "services": []}
    for s, meta in SERVICES.items():
        tok = _load(s) or {}
        p = _pending.get(s) or {}
        out["services"].append({
            "id": s, "name": meta["name"], "connected": bool(tok.get("refresh_token")), "account": tok.get("account", ""),
            "since": tok.get("connected", ""), "error": tok.get("error", "") or p.get("error", ""),
            "signing_in": bool(p and not p.get("done") and time.time() - p.get("started", 0) < 300),
        })
    return out


def _post_form(url, fields, timeout=20):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        raise GoogleError(f"Google said {e.code}: {body[:300]}")


def access_token(service):
    """A fresh access token, refreshing it when it's about to run out."""
    with _lock:
        tok = _load(service)
        if not tok or not tok.get("refresh_token"):
            raise GoogleError(f"{SERVICES[service]['name']} isn't connected: sign in from Friday's Settings > Connectors")
        if tok.get("access_token") and tok.get("expires", 0) > time.time() + 60:
            return tok["access_token"]
        c = client()
        if not c:
            raise GoogleError("Friday's Google app isn't set up (Settings > Connectors)")
        try:
            r = _post_form(TOKEN_URL, {"client_id": c[0], "client_secret": c[1], "refresh_token": tok["refresh_token"],
                                       "grant_type": "refresh_token"})
        except GoogleError as e:
            if "invalid_grant" in str(e):  # revoked, password changed, or a Testing-mode token past its 7 days
                tok["error"] = "Google signed this connector out: connect it again"
                tok.pop("access_token", None)
                _save(service, tok)
                raise GoogleError(f"{SERVICES[service]['name']} was signed out by Google: connect it again in Settings")
            raise
        tok.update(access_token=r["access_token"], expires=time.time() + int(r.get("expires_in", 3600)), error="")
        if r.get("refresh_token"):
            tok["refresh_token"] = r["refresh_token"]
        _save(service, tok)
        return tok["access_token"]


# ---------------------------------------------------------------- signing in (browser + a one-off local page)

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Friday</title>
<body style="font:16px system-ui;background:#0f1115;color:#e8e8ea;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center;max-width:420px;padding:16px"><div style="font-size:40px">{icon}</div><h2>{title}</h2>
<p style="color:#9a9aa3">{text}</p></div>"""


def connect(service, open_browser=True, wait=False):
    """Starts a sign-in for one connector: opens Google's consent page in the browser and waits (in a thread) for the
    code to come back. Returns the consent URL. wait=True blocks until it's done (or 5 minutes pass)."""
    if service not in SERVICES:
        raise GoogleError(f"unknown connector {service}")
    c = client()
    if not c:
        raise GoogleError("Friday's Google app isn't set up yet: paste its client JSON in Settings > Connectors first")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    redirect = f"http://127.0.0.1:{port}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c[0], "redirect_uri": redirect, "response_type": "code", "access_type": "offline",
        "prompt": "consent select_account", "include_granted_scopes": "false", "state": state,
        "scope": " ".join(SERVICES[service]["scopes"] + ID_SCOPES), "code_challenge": challenge,
        "code_challenge_method": "S256"})
    job = {"url": url, "started": time.time(), "error": "", "done": False}
    _pending[service] = job
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in q and q.get("state", [""])[0] == state
            body = PAGE.format(icon="✓" if ok else "✕", title=f"{SERVICES[service]['name']} connected" if ok else "Not connected",
                               text="You can close this tab. Friday has it from here." if ok
                               else html.escape(q.get("error", ["Something went wrong"])[0]))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    srv.timeout = 1

    def run():
        try:
            end = time.time() + 300
            while not got and time.time() < end:
                srv.handle_request()
            if not got:
                raise GoogleError("the sign-in wasn't finished within 5 minutes")
            if got.get("error"):
                raise GoogleError(f"Google said: {got['error']}")
            if got.get("state") != state:
                raise GoogleError("the sign-in reply didn't match (state)")
            r = _post_form(TOKEN_URL, {"client_id": c[0], "client_secret": c[1], "code": got["code"], "code_verifier": verifier,
                                       "redirect_uri": redirect, "grant_type": "authorization_code"})
            if not r.get("refresh_token"):
                raise GoogleError("Google gave no refresh token (remove Friday's access at myaccount.google.com and try again)")
            granted = set((r.get("scope") or "").split())
            missing = [s for s in SERVICES[service]["scopes"] if s not in granted]
            if missing:
                raise GoogleError("the permission box for " + SERVICES[service]["name"] + " was left unticked: connect again and tick it")
            who = ""
            try:
                req = urllib.request.Request("https://openidconnect.googleapis.com/v1/userinfo",
                                             headers={"Authorization": "Bearer " + r["access_token"]})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    who = json.loads(resp.read().decode()).get("email", "")
            except Exception:
                pass
            _save(service, {"account": who, "refresh_token": r["refresh_token"], "access_token": r["access_token"],
                            "expires": time.time() + int(r.get("expires_in", 3600)), "scopes": sorted(granted),
                            "connected": dt.datetime.now().isoformat(timespec="minutes"), "error": ""})
            log.info("google: %s connected as %s", service, who or "?")
        except Exception as e:
            job["error"] = str(e)
            log.warning("google: %s sign-in failed: %s", service, e)
        finally:
            job["done"] = True
            srv.server_close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    if open_browser:
        webbrowser.open(url)
    if wait:
        t.join(310)
        if job["error"]:
            raise GoogleError(job["error"])
    return url


def disconnect(service):
    tok = _load(service)
    if tok and tok.get("refresh_token"):
        try:
            _post_form(REVOKE_URL, {"token": tok["refresh_token"]}, timeout=10)
        except Exception as e:
            log.info("google: revoke for %s failed (removed here anyway): %s", service, e)
    try:
        os.remove(_token_path(service))
    except OSError:
        pass
    log.info("google: %s disconnected", service)


# ---------------------------------------------------------------- calling the APIs

def _api(service, method, url, params=None, body=None, raw=False, timeout=30):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + access_token(service), **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = r.read()
                return out if raw else (json.loads(out.decode()) if out else {})
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")
            if e.code == 401 and attempt == 1:  # token went stale early: refresh once
                tok = _load(service) or {}
                tok["expires"] = 0
                _save(service, tok)
                continue
            if e.code == 403 and ("has not been used" in msg or "is disabled" in msg):
                raise GoogleError(f"the {SERVICES[service]['name']} API isn't switched on in his Google Cloud project")
            raise GoogleError(f"{SERVICES[service]['name']} said {e.code}: {msg[:300]}")


# ----- Gmail

def _hdr(msg, name):
    return next((h["value"] for h in msg.get("payload", {}).get("headers", []) if h["name"].lower() == name.lower()), "")


def _body(part):
    """The plain text of a message (its text/plain part, else its HTML with the tags taken out)."""
    plain, htm = [], []

    def walk(p):
        mt, data = p.get("mimeType", ""), p.get("body", {}).get("data")
        if data and mt == "text/plain":
            plain.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "ignore"))
        elif data and mt == "text/html":
            htm.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "ignore"))
        for c in p.get("parts", []) or []:
            walk(c)
    walk(part)
    if plain:
        return "\n".join(plain).strip()
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(htm))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).replace("\xa0", " ").strip()


def gmail_search(query="is:unread newer_than:1d", max_results=10):
    """Messages matching a Gmail search (same syntax as the Gmail search box)."""
    r = _api("gmail", "GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages",
             {"q": query, "maxResults": max(1, min(int(max_results), 25))})
    out = []
    for m in r.get("messages", []) or []:
        full = _api("gmail", "GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
                    {"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
        out.append({"id": m["id"], "thread": full.get("threadId"), "from": _hdr(full, "From"), "to": _hdr(full, "To"),
                    "subject": _hdr(full, "Subject"), "date": _hdr(full, "Date"), "snippet": html.unescape(full.get("snippet", "")),
                    "unread": "UNREAD" in full.get("labelIds", [])})
    return out


def gmail_read(message_id, max_chars=6000):
    m = _api("gmail", "GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}", {"format": "full"})
    return {"id": message_id, "thread": m.get("threadId"), "from": _hdr(m, "From"), "to": _hdr(m, "To"), "cc": _hdr(m, "Cc"),
            "subject": _hdr(m, "Subject"), "date": _hdr(m, "Date"), "message_id_header": _hdr(m, "Message-ID"),
            "body": _body(m.get("payload", {}))[:max_chars],
            "attachments": [p.get("filename") for p in (m.get("payload", {}).get("parts") or []) if p.get("filename")]}


def _mime(to, subject, body, cc="", reply_to_id=""):
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"], msg["Subject"] = to, subject
    if cc:
        msg["Cc"] = cc
    thread = None
    if reply_to_id:
        orig = gmail_read(reply_to_id, max_chars=10)
        thread = orig.get("thread")
        if orig.get("message_id_header"):
            msg["In-Reply-To"] = msg["References"] = orig["message_id_header"]
        if not subject:
            msg.replace_header("Subject", "Re: " + re.sub(r"^(re:\s*)+", "", orig.get("subject", ""), flags=re.I))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw, **({"threadId": thread} if thread else {})}


def gmail_send(to, subject, body, cc="", reply_to_id=""):
    r = _api("gmail", "POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", body=_mime(to, subject, body, cc, reply_to_id))
    return {"sent": True, "id": r.get("id")}


def gmail_draft(to, subject, body, cc="", reply_to_id=""):
    r = _api("gmail", "POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
             body={"message": _mime(to, subject, body, cc, reply_to_id)})
    return {"draft": True, "id": r.get("id")}


def gmail_mark_read(message_id, read=True):
    _api("gmail", "POST", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/modify",
         body={"removeLabelIds": ["UNREAD"]} if read else {"addLabelIds": ["UNREAD"]})
    return {"ok": True}


# ----- Calendar

def _when(e, key):
    v = e.get(key, {})
    return v.get("dateTime") or v.get("date") or ""


def calendar_events(start="", end="", query="", max_results=25):
    """Events on every calendar he shows, between start and end (ISO dates or date-times; default: now to 2 days)."""
    now = dt.datetime.now().astimezone()
    t0 = _iso(start) if start else now.isoformat()
    t1 = _iso(end, end_of_day=True) if end else (now + dt.timedelta(days=2)).isoformat()
    cals = _api("calendar", "GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList").get("items", [])
    cals = [c for c in cals if c.get("selected", c.get("primary"))] or [{"id": "primary", "summary": "primary"}]
    out = []
    for c in cals[:8]:
        params = {"timeMin": t0, "timeMax": t1, "singleEvents": "true", "orderBy": "startTime", "maxResults": int(max_results),
                  "timeZone": TZ}
        if query:
            params["q"] = query
        try:
            items = _api("calendar", "GET", f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(c['id'])}/events",
                         params).get("items", [])
        except GoogleError as e:
            log.info("google: calendar %s skipped: %s", c.get("summary"), e)
            continue
        for e in items:
            out.append({"id": e.get("id"), "calendar": c.get("summary", ""), "calendar_id": c["id"], "title": e.get("summary", "(no title)"),
                        "start": _when(e, "start"), "end": _when(e, "end"), "location": e.get("location", ""),
                        "attendees": [a.get("email") for a in e.get("attendees", [])][:12], "meet": e.get("hangoutLink", ""),
                        "organizer": (e.get("organizer") or {}).get("email", ""), "status": e.get("status", "")})
    return sorted(out, key=lambda e: e["start"])


def _iso(s, end_of_day=False):
    """'2026-10-12' or '2026-10-12T15:00' (local) -> an RFC 3339 time."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        d = dt.datetime.fromisoformat(s) + (dt.timedelta(days=1) if end_of_day else dt.timedelta())
        return d.astimezone().isoformat()
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (d if d.tzinfo else d.astimezone()).isoformat()


def calendar_create(title, start, end="", location="", description="", attendees=None, calendar_id="primary"):
    s = _iso(start)
    e = _iso(end) if end else (dt.datetime.fromisoformat(s) + dt.timedelta(hours=1)).isoformat()
    body = {"summary": title, "start": {"dateTime": s, "timeZone": TZ}, "end": {"dateTime": e, "timeZone": TZ}}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", (start or "").strip()):  # a whole-day event
        body["start"], body["end"] = {"date": start.strip()}, {"date": (end or "").strip() or (dt.date.fromisoformat(start.strip()) + dt.timedelta(days=1)).isoformat()}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    r = _api("calendar", "POST", f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events", body=body,
             params={"sendUpdates": "all" if attendees else "none"})
    return {"created": True, "id": r.get("id"), "link": r.get("htmlLink")}


def calendar_update(event_id, calendar_id="primary", **changes):
    body = {}
    if changes.get("title"):
        body["summary"] = changes["title"]
    if changes.get("start"):
        body["start"] = {"dateTime": _iso(changes["start"]), "timeZone": TZ}
    if changes.get("end"):
        body["end"] = {"dateTime": _iso(changes["end"]), "timeZone": TZ}
    for k in ("location", "description"):
        if changes.get(k):
            body[k] = changes[k]
    r = _api("calendar", "PATCH", f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events/{event_id}",
             body=body)
    return {"updated": True, "id": r.get("id")}


def calendar_delete(event_id, calendar_id="primary"):
    _api("calendar", "DELETE", f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events/{event_id}")
    return {"deleted": True}


# ----- Drive

EXPORT = {"application/vnd.google-apps.document": "text/plain", "application/vnd.google-apps.spreadsheet": "text/csv",
          "application/vnd.google-apps.presentation": "text/plain"}


def drive_search(query="", max_results=15):
    """Files whose name or text matches; empty = the most recently changed. Advanced: a raw Drive query (it has ' in ')."""
    if " in " in query or "=" in query or "contains" in query:
        q = query
    elif query:
        safe = query.replace("\\", "\\\\").replace("'", "\\'")
        q = f"(name contains '{safe}' or fullText contains '{safe}') and trashed = false"
    else:
        q = "trashed = false"
    params = {"q": q, "pageSize": max(1, min(int(max_results), 50)),
              "fields": "files(id,name,mimeType,modifiedTime,owners(emailAddress),webViewLink,size)",
              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
    if not query:
        params["orderBy"] = "modifiedTime desc"  # (Drive refuses orderBy together with a fullText search)
    r = _api("drive", "GET", "https://www.googleapis.com/drive/v3/files", params)
    return [{"id": f["id"], "name": f["name"], "type": f["mimeType"].split(".")[-1].split("/")[-1], "modified": f.get("modifiedTime", ""),
             "owner": ((f.get("owners") or [{}])[0]).get("emailAddress", ""), "link": f.get("webViewLink", ""), "size": f.get("size")}
            for f in r.get("files", [])]


def drive_read(file_id, max_chars=12000):
    meta = _api("drive", "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}",
                {"fields": "id,name,mimeType,size,webViewLink", "supportsAllDrives": "true"})
    mt = meta.get("mimeType", "")
    if mt in EXPORT:
        data = _api("drive", "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}/export", {"mimeType": EXPORT[mt]}, raw=True)
    elif mt.startswith("text/") or mt in ("application/json", "text/csv"):
        if int(meta.get("size") or 0) > 3_000_000:
            return {**meta, "text": "", "note": "too big to read here"}
        data = _api("drive", "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}", {"alt": "media"}, raw=True)
    elif mt == "application/pdf" and int(meta.get("size") or 0) < 15_000_000:
        data = _api("drive", "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}", {"alt": "media"}, raw=True)
        try:
            import io
            from pypdf import PdfReader
            text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:30])
            return {**meta, "text": text[:max_chars]}
        except Exception:
            return {**meta, "text": "", "note": "a PDF I can't read the text of here; the link opens it"}
    else:
        return {**meta, "text": "", "note": "not a text file; the link opens it"}
    return {**meta, "text": data.decode("utf-8", "ignore")[:max_chars]}


def drive_download(file_id, folder):
    meta = _api("drive", "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}",
                {"fields": "id,name,mimeType", "supportsAllDrives": "true"})
    mt, name = meta["mimeType"], meta["name"]
    if mt.startswith("application/vnd.google-apps"):
        fmt = {"document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
               "spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
               "presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx")}.get(mt.split(".")[-1])
        if not fmt:
            raise GoogleError("that kind of Google file can't be downloaded")
        data = _api("drive", "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}/export", {"mimeType": fmt[0]}, raw=True, timeout=120)
        name += fmt[1]
    else:
        data = _api("drive", "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}", {"alt": "media"}, raw=True, timeout=120)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, re.sub(r'[\\/:*?"<>|]', "_", name))
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "name": name}


# ---------------------------------------------------------------- a plain digest (no AI): the backup brain's view

def digest(services=("calendar", "gmail")):
    """Today's events and the last day's unread mail as compact text, for when Claude can't be used."""
    parts = []
    if "calendar" in services and connected("calendar"):
        try:
            today = dt.date.today().isoformat()
            ev = calendar_events(today, today)
            parts.append("CALENDAR TODAY: " + ("; ".join(f"{e['start'][11:16] or 'all day'} {e['title']}" for e in ev) or "nothing"))
        except Exception as e:
            parts.append(f"CALENDAR: couldn't read it ({e})")
    if "gmail" in services and connected("gmail"):
        try:
            mails = gmail_search("is:unread newer_than:1d -category:promotions -category:social", 8)
            parts.append("UNREAD MAIL (1 day): " + ("; ".join(f"{email.utils.parseaddr(m['from'])[0] or m['from']}: {m['subject']}"
                                                              for m in mails) or "none"))
        except Exception as e:
            parts.append(f"GMAIL: couldn't read it ({e})")
    return "\n".join(parts)
