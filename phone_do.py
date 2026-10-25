"""Run one action on his phone through the running Friday and print the phone's answer.

    python phone_do.py '{"a":"device_report","topic":"battery"}'
    python phone_do.py '{"a":"setting","what":"brightness","value":40}'
    python phone_do.py '{"a":"open_settings","page":"app notifications","app":"WhatsApp"}'
    python phone_do.py '{"a":"task","goal":"open Settings > Apps > Instagram and force stop it"}' --timeout 300

For her diagnostic agent and for tests. Needs Friday running on the laptop (bridge on 127.0.0.1:8765) and the phone
linked. A yes/no the phone asks is answered "no" unless --yes is given."""

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    try:
        action = json.loads(args[0])
    except ValueError:
        action = {"a": "task", "goal": " ".join(args)}  # plain words = a step-by-step phone task
    timeout = 120
    if "--timeout" in sys.argv:
        timeout = int(sys.argv[sys.argv.index("--timeout") + 1])
    with open(os.path.join(HERE, "data", "bridge_token.txt"), encoding="utf-8") as f:
        token = f.read().strip()
    kind = "task" if action.get("a") == "task" else "action"
    body = {"goal": action.get("goal") or f"phone: {action.get('a')}", "kind": kind, "timeout": timeout,
            "confirm": "--yes" in sys.argv, "data": {"action": action} if kind == "action" else {}}
    req = urllib.request.Request("http://127.0.0.1:8765/phone_job", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "X-Friday-Token": token, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 20) as r:
            out = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"Friday's bridge didn't answer: {e}")
        return 1
    if not out.get("picked"):
        print("The phone didn't pick the job up: it's not linked right now (Friday must be running on it, on the same Wi-Fi/hotspot or the tunnel).")
        return 1
    for line in out.get("says") or []:
        print("phone said:", line)
    print(out.get("text") or "(no text back)")
    if out.get("path"):
        print("file:", out["path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
