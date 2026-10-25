"""Friday's watchdog, run every few minutes by Task Scheduler ("Friday Watchdog").

Starts Friday if she isn't running, unless he quit her on purpose from the tray (data/stopped exists;
the login shortcut and "Restart Friday" clear it). Logs to friday.log.
"""
import ctypes
import logging
import os
import subprocess
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
STOPPED = os.path.join(HERE, "data", "stopped")

log = logging.getLogger("watchdog")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(HERE, "friday.log"), maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s watchdog: %(message)s"))
log.addHandler(_h)


def running():
    # Friday holds this mutex for as long as she's alive
    h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Global\\JarvisVoiceAssistant")
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


if __name__ == "__main__":
    # she moved from C:\Users\Dell\Scripts\jarvis to D:\dev\Friday on 10 Oct 2026; the old folder stayed behind, empty, while something held it
    old = r"C:\Users\Dell\Scripts\jarvis"
    if os.path.isdir(old) and not os.listdir(old):
        try:
            os.rmdir(old)
        except OSError:
            pass
    if os.path.exists(STOPPED):
        raise SystemExit  # he quit her on purpose
    try:  # the phone's way in from anywhere (its own process, so Friday restarting keeps the same address)
        import remote
        url = remote.ensure()
        if url and url != getattr(remote, "_last", ""):
            log.info("remote address: %s", url)
    except Exception as e:
        log.warning("remote tunnel check failed: %s", e)
    if not running():
        log.warning("Friday wasn't running: starting her")
        subprocess.Popen([os.path.join(HERE, ".venv", "Scripts", "pythonw.exe"), os.path.join(HERE, "friday.py")],
                         cwd=HERE, creationflags=0x08000000)
