"""Her Google connectors as an MCP server (stdio, JSON-RPC, one message per line), so the claude brain behind
ask_google and her background agents can use his Gmail, Calendar and Drive through her own sign-ins.

    pythonw google_mcp.py --services gmail,calendar,drive

Only connected services are offered. Sending mail and changing events need confirmed=true: the caller only sets it
after he said yes to the exact read-back."""
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
logging.basicConfig(filename=os.path.join(HERE, "data", "google_mcp.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
import google_conn as G  # noqa: E402

S, I, B, A = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "array", "items": {"type": "string"}}
CONFIRM = {"type": "boolean", "description": "true only after he said yes to the exact read-back of this change"}


def tool(name, service, desc, props, required=()):
    return {"name": name, "service": service, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": list(required)}}


TOOLS = [
    tool("google_accounts", None, "Which Google account each of his connectors (Gmail, Calendar, Drive) is signed in as.", {}),
    tool("gmail_search", "gmail", "Search his Gmail (Gmail search syntax, e.g. 'is:unread newer_than:1d', 'from:ravi', "
         "'subject:invoice after:2026/10/01'). Returns id, from, subject, date, snippet.", {"query": S, "max_results": I}),
    tool("gmail_read", "gmail", "The full text of one email by id.", {"id": S}, ["id"]),
    tool("gmail_send", "gmail", "Send an email (or a reply when reply_to_id is given). Needs confirmed=true.",
         {"to": S, "subject": S, "body": S, "cc": S, "reply_to_id": S, "confirmed": CONFIRM}, ["to", "body", "confirmed"]),
    tool("gmail_draft", "gmail", "Save an email as a draft in his Gmail (nothing is sent).",
         {"to": S, "subject": S, "body": S, "cc": S, "reply_to_id": S}, ["to", "body"]),
    tool("gmail_mark_read", "gmail", "Mark an email read (or unread with read=false).", {"id": S, "read": B}, ["id"]),
    tool("calendar_events", "calendar", "Events on all his calendars between start and end (ISO date '2026-10-12' or "
         "date-time '2026-10-12T15:00', India time; default now to two days ahead), optional text query.",
         {"start": S, "end": S, "query": S, "max_results": I}),
    tool("calendar_create", "calendar", "Add an event (start/end as local ISO date-times; a date alone = all day). "
         "Attendees get invites. Needs confirmed=true.",
         {"title": S, "start": S, "end": S, "location": S, "description": S, "attendees": A, "calendar_id": S, "confirmed": CONFIRM},
         ["title", "start", "confirmed"]),
    tool("calendar_update", "calendar", "Change an event (any of title, start, end, location, description). Needs confirmed=true.",
         {"id": S, "calendar_id": S, "title": S, "start": S, "end": S, "location": S, "description": S, "confirmed": CONFIRM},
         ["id", "confirmed"]),
    tool("calendar_delete", "calendar", "Delete an event. Needs confirmed=true.", {"id": S, "calendar_id": S, "confirmed": CONFIRM},
         ["id", "confirmed"]),
    tool("drive_search", "drive", "Find files in his Google Drive by name or text (empty query = most recently changed).",
         {"query": S, "max_results": I}),
    tool("drive_read", "drive", "The text of a Drive file by id (Docs, Sheets as CSV, Slides, text files, PDFs).", {"id": S}, ["id"]),
    tool("drive_download", "drive", "Download a Drive file to his laptop (Downloads\\Friday); Google files as Office files.",
         {"id": S}, ["id"]),
]


def call(name, a):
    if name == "google_accounts":
        return {s["name"]: (s["account"] or "connected") if s["connected"] else "not connected" for s in G.status()["services"]}
    if name in ("gmail_send", "calendar_create", "calendar_update", "calendar_delete") and a.get("confirmed") is not True:
        raise G.GoogleError("not done: read the details back to him and only call this again with confirmed=true after his yes")
    if name == "gmail_search":
        return G.gmail_search(a.get("query") or "is:unread newer_than:1d", a.get("max_results", 10))
    if name == "gmail_read":
        return G.gmail_read(a["id"])
    if name == "gmail_send":
        return G.gmail_send(a["to"], a.get("subject", ""), a["body"], a.get("cc", ""), a.get("reply_to_id", ""))
    if name == "gmail_draft":
        return G.gmail_draft(a["to"], a.get("subject", ""), a["body"], a.get("cc", ""), a.get("reply_to_id", ""))
    if name == "gmail_mark_read":
        return G.gmail_mark_read(a["id"], a.get("read", True))
    if name == "calendar_events":
        return G.calendar_events(a.get("start", ""), a.get("end", ""), a.get("query", ""), a.get("max_results", 25))
    if name == "calendar_create":
        return G.calendar_create(a["title"], a["start"], a.get("end", ""), a.get("location", ""), a.get("description", ""),
                                 a.get("attendees"), a.get("calendar_id") or "primary")
    if name == "calendar_update":
        keep = {k: a[k] for k in ("title", "start", "end", "location", "description") if a.get(k)}
        return G.calendar_update(a["id"], a.get("calendar_id") or "primary", **keep)
    if name == "calendar_delete":
        return G.calendar_delete(a["id"], a.get("calendar_id") or "primary")
    if name == "drive_search":
        return G.drive_search(a.get("query", ""), a.get("max_results", 15))
    if name == "drive_read":
        return G.drive_read(a["id"])
    if name == "drive_download":
        return G.drive_download(a["id"], os.path.join(os.path.expanduser("~"), "Downloads", "Friday"))
    raise G.GoogleError(f"unknown tool {name}")


def main():
    wanted = set(G.SERVICES)
    if "--services" in sys.argv:
        wanted = set(sys.argv[sys.argv.index("--services") + 1].split(","))
    tools = [t for t in TOOLS if t["service"] is None or (t["service"] in wanted and G.connected(t["service"]))]
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    out = sys.stdout
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if mid is None:
            continue  # a notification (initialized, cancelled): nothing to answer
        try:
            if method == "initialize":
                res = {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                       "serverInfo": {"name": "friday-google", "version": "1.0"}}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in tools]}
            elif method == "tools/call":
                name = params.get("name", "")
                if name not in {t["name"] for t in tools}:
                    raise G.GoogleError(f"{name} isn't available (that connector isn't signed in)")
                logging.info("call %s %s", name, json.dumps(params.get("arguments") or {})[:300])
                try:
                    text = json.dumps(call(name, params.get("arguments") or {}), ensure_ascii=False, indent=1)
                    res = {"content": [{"type": "text", "text": text[:60000]}], "isError": False}
                except Exception as e:
                    logging.warning("%s failed: %s", name, e)
                    res = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
            else:
                out.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}) + "\n")
                out.flush()
                continue
            out.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}, ensure_ascii=False) + "\n")
        except Exception as e:
            out.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}}) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
