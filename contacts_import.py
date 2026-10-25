"""Builds data/contacts.json for Friday from Phone Link's copy of his phone contacts.

    python contacts_import.py --schema    show the database layout only (no contact data)
    python contacts_import.py             import names + numbers into data/contacts.json
"""
import glob
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERN = os.path.expandvars(r"%LOCALAPPDATA%\Packages\Microsoft.YourPhone_8wekyb3d8bbwe\LocalCache\Indexed"
                             r"\*\System\Database\contacts.db")


def connect(db="contacts.db"):
    paths = sorted(glob.glob(PATTERN.replace("contacts.db", db)), key=os.path.getmtime)
    if not paths:
        sys.exit("Phone Link contacts database not found")
    # read-only and immutable: Phone Link may hold the file open, and we must never change it
    return sqlite3.connect("file:" + paths[-1].replace("\\", "/") + "?mode=ro&immutable=1", uri=True)


def schema(c):
    for (name,) in c.execute("select name from sqlite_master where type='table'"):
        n = c.execute(f'select count(*) from "{name}"').fetchone()[0]
        cols = [r[1] for r in c.execute(f'pragma table_info("{name}")')]
        print(f"{name} ({n} rows): {cols}")


def digits(number):
    d = "".join(ch for ch in (number or "") if ch.isdigit())
    return d[-10:]  # Indian numbers: the last 10 digits identify the line with or without +91 / 0


def build():
    c = connect()
    contacts = {}
    for cid, name, nick, given, family in c.execute(
            "select contact_id, display_name, nickname, given_name, family_name from contact"):
        if name and name.strip():
            contacts[cid] = {"name": name.strip(), "nickname": (nick or "").strip(), "numbers": [], "emails": [],
                             "calls": 0}
    for cid, number, display, ptype in c.execute(
            "select contact_id, phone_number, display_phone_number, phone_number_type from phonenumber"):
        if cid in contacts and (display or number):
            num = (display or number).strip()
            if digits(num) not in {digits(n) for n in contacts[cid]["numbers"]}:
                contacts[cid]["numbers"].append(num)
    for cid, address in c.execute("select contact_id, address from emailaddress"):
        if cid in contacts and address:
            contacts[cid]["emails"].append(address)
    # how often he calls each one, from Phone Link's call history, so frequent people rank first
    by_number = {}
    for cid, ct in contacts.items():
        for n in ct["numbers"]:
            by_number[digits(n)] = cid
    try:
        for (number,) in connect("calling.db").execute("select phone_number from call_history"):
            cid = by_number.get(digits(number))
            if cid:
                contacts[cid]["calls"] += 1
    except Exception as e:
        print("no call history:", e)
    out = sorted((ct for ct in contacts.values() if ct["numbers"] or ct["emails"]),
                 key=lambda ct: (-ct["calls"], ct["name"].lower()))
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    with open(os.path.join(HERE, "data", "contacts.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    called = sum(1 for ct in out if ct["calls"])
    print(f"saved {len(out)} contacts ({sum(len(ct['numbers']) for ct in out)} numbers); "
          f"{called} have call history")


if __name__ == "__main__":
    if "--schema" in sys.argv:
        schema(connect(sys.argv[-1] if sys.argv[-1].endswith(".db") else "contacts.db"))
    elif "--notif-sample" in sys.argv:
        # layout of the notification rows (app names and column types only, no message text)
        c = connect("notifications.db")
        for row in c.execute("select * from notifications order by rowid desc limit 1"):
            print([(d[0], type(v).__name__, (str(v)[:40] if d[0] in ("app_name", "package_name", "post_time",
                    "timestamp", "id", "notification_id") else "…")) for d, v in zip(c.execute(
                    "select * from notifications limit 1").description, row)])
    else:
        build()
