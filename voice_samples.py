"""Plays the candidate voices one after another so you can pick one for config.json."""
import asyncio
import sys
import time

import edge_tts
import miniaudio
import numpy as np
import sounddevice as sd

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from friday import master  # noqa: E402

LINE = ("Right, so I checked your calendar, and it's empty. Which is either very good news, or you've "
        "forgotten something important. Shall I panic now, or after lunch?")

CANDIDATES = [
    ("1", "en-US-AndrewMultilingualNeural", "-2Hz", "+0%"),
    ("2", "en-US-BrianMultilingualNeural", "-3Hz", "+0%"),
    ("3", "en-GB-RyanNeural", "-4Hz", "+0%"),
    ("4", "en-US-ChristopherNeural", "-2Hz", "-2%"),
    ("5", "en-GB-ThomasNeural", "-2Hz", "+0%"),
    ("6", "en-GB-RyanNeural", "+0Hz", "+4%"),
]


async def fetch(text, voice, pitch, rate):
    data = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, pitch=pitch, rate=rate).stream():
        if chunk["type"] == "audio":
            data.extend(chunk["data"])
    return bytes(data)


for label, voice, pitch, rate in CANDIDATES:
    if len(sys.argv) > 1 and label not in sys.argv[1:]:
        continue
    print(f"Voice {label}: {voice}", flush=True)
    intro = f"Voice {label}. "
    mp3 = asyncio.run(fetch(intro + LINE, voice, pitch, rate))
    pcm = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=24000)
    sd.play(master(np.frombuffer(pcm.samples, dtype=np.int16), 24000), 24000, blocking=True)
    time.sleep(0.8)
