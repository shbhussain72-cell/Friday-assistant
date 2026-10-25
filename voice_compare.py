"""Plays candidate Jarvis voices back to back with the same line.

    python voice_compare.py        all candidates
    python voice_compare.py 1 3    only those
"""
import asyncio
import os
import sys
import time

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from friday import master  # noqa: E402

LINE = ("Wait, you finished all thirty three findings tonight? Dude, that's huge. "
        "Okay, now please go to sleep before your laptop files a complaint.")

CANDIDATES = [
    ("1", "edge", "en-US-AndrewMultilingualNeural", "Andrew (online, American, warm)"),
    ("2", "edge", "en-US-BrianMultilingualNeural", "Brian (online, American, casual)"),
    ("3", "edge", "en-US-ChristopherNeural", "Christopher (online, American, deeper)"),
    ("4", "kokoro", "am_michael", "Michael (offline)"),
    ("5", "kokoro", "am_fenrir", "Fenrir (offline)"),
    ("6", "edge", "en-US-AvaMultilingualNeural", "Ava (online, American, female)"),
    ("7", "kokoro", "af_heart", "Heart (offline, female, Kokoro's best voice)"),
]


def edge(text, voice):
    import edge_tts
    import miniaudio

    async def fetch():
        data = bytearray()
        async for c in edge_tts.Communicate(text, voice).stream():
            if c["type"] == "audio":
                data.extend(c["data"])
        return bytes(data)

    pcm = miniaudio.decode(asyncio.run(fetch()), output_format=miniaudio.SampleFormat.SIGNED16,
                           nchannels=1, sample_rate=24000)
    return np.frombuffer(pcm.samples, dtype=np.int16), 24000


def kokoro(text, voice):
    from kokoro_onnx import Kokoro
    k = Kokoro(os.path.join(HERE, "models", "kokoro-v1.0.onnx"), os.path.join(HERE, "models", "voices-v1.0.bin"))
    s, r = k.create(text, voice=voice, speed=1.05, lang="en-us")
    return (s * 32767).astype(np.int16), r


pick = sys.argv[1:]
for label, engine, voice, desc in CANDIDATES:
    if pick and label not in pick:
        continue
    samples, rate = (edge if engine == "edge" else kokoro)(f"Voice {label}. " + LINE, voice)
    print(f"Voice {label}: {desc}", flush=True)
    sd.play(master(samples, rate, bass_db=3), rate, blocking=True)  # light touch: loud enough, no heavy bass
    time.sleep(0.7)
