"""Plays Kokoro voices so you can pick one for config.json.

    python kokoro_samples.py           British male voices
    python kokoro_samples.py am        American male voices
    python kokoro_samples.py am 2 4    only voices 2 and 4 of that set
"""
import os
import sys
import time

import numpy as np
import sounddevice as sd
from kokoro_onnx import Kokoro

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from friday import master  # noqa: E402

LINE = ("Okay so, I checked your calendar and it's completely empty. Which is either amazing news, or you "
        "forgot something huge. Honestly? My money's on the second one.")

args = sys.argv[1:]
prefix = args.pop(0) if args and not args[0].isdigit() else "bm"
lang = "en-us" if prefix.startswith("a") else "en-gb"
kokoro = Kokoro(os.path.join(HERE, "models", "kokoro-v1.0.onnx"), os.path.join(HERE, "models", "voices-v1.0.bin"))
voices = [v for v in kokoro.get_voices() if v.startswith(prefix + "_")]
for i, v in enumerate(voices, 1):
    if args and str(i) not in args:
        continue
    samples, rate = kokoro.create(f"Voice {i}. " + LINE, voice=v, speed=1.05, lang=lang)
    print(f"Voice {i}: {v}", flush=True)
    sd.play(master((samples * 32767).astype(np.int16), rate, bass_db=7), rate, blocking=True)
    time.sleep(0.7)
