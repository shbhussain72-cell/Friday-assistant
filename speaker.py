"""Recognises his voice, so Friday can ignore the TV, other people and her own echo.

Speaker embeddings come from a small ONNX model (WeSpeaker ResNet34 via sherpa-onnx, CPU, ~30 ms per
utterance). Enrolment is passive: every request he makes by name is assumed to be him and averaged into a
voiceprint (data/voiceprint.npy). Until that has enough samples, everything is treated as him.
"""
import logging
import os
import threading

import numpy as np

log = logging.getLogger("friday")
HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "speaker", "wespeaker_en_voxceleb_resnet34_LM.onnx")
PRINT = os.path.join(HERE, "data", "voiceprint.npz")


class SpeakerID:
    MIN_SAMPLES = 8          # utterances before the voiceprint is trusted
    MAX_SAMPLES = 60

    def __init__(self, threshold=0.55):
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=2)
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.threshold = threshold
        self.lock = threading.Lock()
        self.samples = []
        try:
            self.samples = list(np.load(PRINT)["samples"])
        except (OSError, KeyError, ValueError):
            pass

    @property
    def ready(self):
        return len(self.samples) >= self.MIN_SAMPLES

    def embed(self, audio, rate=16000):
        """audio: float32 mono. Returns a unit vector, or None if there's too little speech."""
        if len(audio) < rate * 0.8:
            return None
        with self.lock:
            s = self.extractor.create_stream()
            s.accept_waveform(rate, audio.astype(np.float32))
            s.input_finished()
            if not self.extractor.is_ready(s):
                return None
            e = np.array(self.extractor.compute(s), dtype=np.float32)
        n = np.linalg.norm(e)
        return e / n if n else None

    def score(self, audio):
        """Cosine similarity to his voiceprint (1 = same voice), or None if unknown."""
        if not self.ready:
            return None
        e = self.embed(audio)
        if e is None:
            return None
        centre = np.mean(self.samples, axis=0)
        centre /= np.linalg.norm(centre)
        return float(np.dot(e, centre))

    def cutoff(self):
        """How close a voice must be to count as him: set from how consistent his own samples are (a real
        mic varies more than a test clip), never below the floor or above 0.85."""
        if not self.ready:
            return self.threshold
        centre = np.mean(self.samples, axis=0)
        centre /= np.linalg.norm(centre)
        own = np.array([float(np.dot(s, centre)) for s in self.samples])
        return float(np.clip(own.mean() - 2.5 * own.std() - 0.05, self.threshold, 0.85))

    def is_him(self, audio):
        """True/False once enrolled; True while still learning (so nothing gets ignored early on)."""
        s = self.score(audio)
        if s is None:
            return True
        return s >= self.cutoff()

    def learn(self, audio):
        """Adds an utterance he made by name to the voiceprint (skips ones that clearly aren't him)."""
        e = self.embed(audio)
        if e is None:
            return
        if self.ready:
            centre = np.mean(self.samples, axis=0)
            if float(np.dot(e, centre / np.linalg.norm(centre))) < self.threshold - 0.1:
                return  # someone else said "Friday": don't let them into his voiceprint
        self.samples = (self.samples + [e])[-self.MAX_SAMPLES:]
        os.makedirs(os.path.dirname(PRINT), exist_ok=True)
        np.savez(PRINT, samples=np.array(self.samples))
        if len(self.samples) == self.MIN_SAMPLES:
            log.info("voiceprint ready (%d samples)", len(self.samples))
