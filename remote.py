"""Phone <-> laptop from anywhere: a free Cloudflare quick tunnel gives the bridge (port 8765) an https address on
the internet, and that address is posted to a private ntfy.sh topic so the phone can find it when it isn't on the
same Wi-Fi. No accounts. Everything still needs the bridge token; the tunnel only carries the traffic.

The tunnel runs as its own process (kept alive by watchdog.py), so Friday restarting doesn't change the address;
it changes only when the tunnel itself restarts (a reboot), and the phone picks the new one up from the topic.
"""
import logging
import os
import re
import secrets
import subprocess
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
EXE = os.path.join(HERE, "bin", "cloudflared.exe")
LOG = os.path.join(DATA, "tunnel.log")
URL_FILE = os.path.join(DATA, "remote_url.txt")
TOPIC_FILE = os.path.join(DATA, "remote_topic.txt")
NO_WINDOW = 0x08000000
DETACHED = 0x00000008
log = logging.getLogger("friday")


def topic():
    """The private ntfy topic (random, made once): only the phone knows it, from its sync."""
    try:
        t = open(TOPIC_FILE, encoding="utf-8").read().strip()
        if t:
            return t
    except OSError:
        pass
    t = "friday-" + secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")
    os.makedirs(DATA, exist_ok=True)
    open(TOPIC_FILE, "w", encoding="utf-8").write(t)
    return t


def current_url():
    try:
        return open(URL_FILE, encoding="utf-8").read().strip()
    except OSError:
        return ""


def _token():
    try:
        return open(os.path.join(DATA, "bridge_token.txt"), encoding="utf-8").read().strip()
    except OSError:
        return ""


def _running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    return "cloudflared.exe" in out


def _works(url):
    """The address answers through the tunnel (and reaches Friday's bridge, which may be restarting)."""
    try:
        r = requests.get(url + "/ping", headers={"X-Friday-Token": _token()}, timeout=12)
        return r.status_code in (200, 401, 502, 503)  # 502/503: the tunnel's up, Friday's just restarting
    except requests.RequestException:
        return False


def publish(url):
    """Posts the address to the private topic (the phone reads the newest message)."""
    try:
        requests.post(f"https://ntfy.sh/{topic()}", data=url.encode(), timeout=15,
                      headers={"Title": "friday-url", "Cache": "yes"})
        return True
    except requests.RequestException as e:
        log.warning("couldn't post the remote address: %s", e)
        return False


def start():
    """Starts a fresh tunnel and waits (up to a minute) for its address."""
    subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], capture_output=True, creationflags=NO_WINDOW)
    os.makedirs(DATA, exist_ok=True)
    logf = open(LOG, "w", encoding="utf-8")
    subprocess.Popen([EXE, "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8765"], stdout=logf, stderr=logf,
                     stdin=subprocess.DEVNULL, cwd=HERE, creationflags=NO_WINDOW | DETACHED)
    for _ in range(60):
        time.sleep(1)
        try:  # (?!api.): not api.trycloudflare.com from its error text
            m = re.search(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com", open(LOG, encoding="utf-8", errors="ignore").read())
        except OSError:
            m = None
        if m:
            url = m.group(0)
            open(URL_FILE, "w", encoding="utf-8").write(url)
            time.sleep(4)  # the address answers a few seconds after it's announced
            publish(url)
            return url
    return ""


def ensure():
    """Called every few minutes (watchdog): a tunnel that's running and answering, its address posted."""
    if not os.path.isfile(EXE):
        return ""
    url = current_url()
    fails = os.path.join(DATA, "remote_fails")
    running = _running()
    if url and running and not _works(url):
        # one slow answer isn't a dead tunnel: restarting changes the address, so it takes two misses in a row
        n = int(open(fails).read() or 0) + 1 if os.path.exists(fails) else 1
        open(fails, "w").write(str(n))
        if n < 2:
            return url
    elif os.path.exists(fails):
        os.remove(fails)
    if url and running and not os.path.exists(fails):
        # re-post now and then (ntfy keeps messages ~12 h; the phone reads the newest)
        stamp = os.path.join(DATA, "remote_posted")
        if not os.path.exists(stamp) or time.time() - os.path.getmtime(stamp) > 6 * 3600:
            if publish(url):
                open(stamp, "w").close()
        return url
    url = start()
    if os.path.exists(fails):
        os.remove(fails)
    if url:
        open(os.path.join(DATA, "remote_posted"), "w").close()
    return url


if __name__ == "__main__":
    print(ensure() or "no tunnel")
    print("topic:", topic())
