"""Watches his phone's notifications (Phone Link's notifications.db) for new messages.

Phone Link keeps the notifications currently on his phone in a SQLite table with the details as JSON.
Read-only, WAL-aware (mode=ro, not immutable, or the newest rows sitting in the -wal file are missed).
"""
import glob
import json
import logging
import os
import re
import sqlite3

log = logging.getLogger("friday")
DB_GLOB = os.path.expandvars(r"%LOCALAPPDATA%\Packages\Microsoft.YourPhone_8wekyb3d8bbwe\LocalCache\Indexed"
                             r"\*\System\Database\notifications.db")

APPS = {
    "com.whatsapp": "WhatsApp", "com.whatsapp.w4b": "WhatsApp Business",
    "com.google.android.apps.messaging": "Messages", "com.android.mms": "Messages",
    "com.samsung.android.messaging": "Messages", "com.google.android.apps.dynamite": "Google Chat",
    "org.telegram.messenger": "Telegram", "com.instagram.android": "Instagram",
}
SUMMARY = re.compile(r"^\s*\d+\s+(new\s+)?messages?\b|^\s*\d+ messages from \d+ chats", re.I)


def _find(obj, *keys):
    """First non-empty string under any of these keys, searched case-insensitively at any depth."""
    want = {k.lower() for k in keys}
    stack = [obj]
    while stack:
        o = stack.pop(0)
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in want and isinstance(v, str) and v.strip():
                    return v.strip()
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return ""


class PhoneNotifications:
    def __init__(self):
        self.seen = set()
        self.primed = False

    def _connect(self):
        paths = sorted(glob.glob(DB_GLOB), key=os.path.getmtime)
        if not paths:
            return None
        return sqlite3.connect("file:" + paths[-1].replace("\\", "/") + "?mode=ro", uri=True, timeout=2)

    def new_messages(self):
        """Messages that arrived since the last call: [{app, sender, text, group}]. The first call only
        records what's already there (no reading out a backlog)."""
        c = self._connect()
        if c is None:
            return []
        try:
            rows = c.execute("select id, notification_id, package_name, json, post_time from notifications").fetchall()
        finally:
            c.close()
        out = []
        for rid, nid, pkg, js, posted in rows:
            key = (nid, posted)
            if key in self.seen:
                continue
            self.seen.add(key)
            if not self.primed or pkg not in APPS:
                continue
            try:
                data = json.loads(js)
            except (TypeError, ValueError):
                continue
            sender = _find(data, "title", "conversationTitle", "sender", "senderName")
            text = _find(data, "bigText", "text", "body", "message", "content")
            if not text or SUMMARY.search(text) or SUMMARY.search(sender):
                continue
            group = bool(re.search(r"\s@\s|:\s", sender)) or bool(_find(data, "isGroupConversation") == "true")
            out.append({"app": APPS[pkg], "sender": sender or APPS[pkg], "text": text, "group": group})
        self.primed = True
        return out
