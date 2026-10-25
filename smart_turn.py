"""Smart end-of-turn: has he finished, or is he only pausing to think?

Pipecat's Smart Turn v3.2 (models/smart_turn/smart-turn-v3.2-cpu.onnx, 8 MB, BSD-2) listens to the last 8 s
of his voice (intonation, the words trailing off or hanging) and says how sure it is that the turn is over.
Runs on the CPU in ~15-40 ms. The log-mel front end below is pipecat's numpy copy of Whisper's feature
extractor (no transformers/torch needed).
"""
import os
import threading

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "smart_turn", "smart-turn-v3.2-cpu.onnx")
RATE = 16000
_N_FFT, _HOP, _N_MELS = 400, 160, 80


def _hz_to_mel(f):
    f = np.atleast_1d(np.asarray(f, dtype=np.float64))
    m = 3.0 * f / 200.0
    hi = f >= 1000.0
    m[hi] = 15.0 + np.log(f[hi] / 1000.0) * (27.0 / np.log(6.4))
    return m


def _mel_to_hz(m):
    m = np.atleast_1d(np.asarray(m, dtype=np.float64))
    f = 200.0 * m / 3.0
    hi = m >= 15.0
    f[hi] = 1000.0 * np.exp(np.log(6.4) / 27.0 * (m[hi] - 15.0))
    return f


def _filterbank():
    bins = _N_FFT // 2 + 1
    mel = np.linspace(_hz_to_mel(0.0)[0], _hz_to_mel(RATE / 2.0)[0], _N_MELS + 2)
    ff = _mel_to_hz(mel)
    fft = np.linspace(0, RATE // 2, bins)
    diff = np.diff(ff)
    slopes = ff[None, :] - fft[:, None]
    fb = np.maximum(0.0, np.minimum(-slopes[:, :-2] / diff[:-1], slopes[:, 2:] / diff[1:]))
    return fb * (2.0 / (ff[2:_N_MELS + 2] - ff[:_N_MELS]))[None, :]


_FB = _filterbank()
_WIN = np.hanning(_N_FFT + 1)[:-1]


def features(audio):
    """Whisper log-mel (80 x 800) of exactly 8 s of 16 kHz audio, zero-padded at the start like pipecat."""
    x = np.asarray(audio, dtype=np.float32)[-RATE * 8:]
    if x.size < RATE * 8:
        x = np.pad(x, (RATE * 8 - x.size, 0))
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    padded = np.pad(x.astype(np.float64), (_N_FFT // 2, _N_FFT // 2), mode="reflect")
    frames = sliding_window_view(padded, _N_FFT)[::_HOP]
    power = (np.abs(np.fft.rfft(frames * _WIN, axis=-1)) ** 2).T
    log_spec = np.log10(np.maximum(1e-10, _FB.T @ power))[:, :-1]
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)


class SmartTurn:
    def __init__(self, path=MODEL):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 2
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
        self.lock = threading.Lock()

    def complete(self, audio):
        """0..1: how sure the model is that he's finished. `audio` = float32 at 16 kHz, newest last."""
        f = features(audio)[None, ...]
        with self.lock:
            return float(np.asarray(self.session.run(None, {"input_features": f})[0]).ravel()[0])


def load():
    """The model, or None if it's missing or won't load (then the fixed silence is used)."""
    if not os.path.exists(MODEL):
        return None
    try:
        return SmartTurn()
    except Exception:
        return None
