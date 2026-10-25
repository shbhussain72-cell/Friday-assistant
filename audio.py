"""Full-duplex audio with echo cancellation.

One sounddevice stream plays Jarvis and records the mic on the same clock, so we know exactly which
output sample every mic sample can contain an echo of. An adaptive filter (partitioned-block
frequency-domain NLMS) learns the speaker -> room -> mic path and subtracts Jarvis's own voice from the
mic, leaving mostly him. That cleaned signal feeds wake word, barge-in and speech recognition, so he can
talk over Jarvis on laptop speakers.

Runs at 24 kHz (Kokoro's native rate, so the voice isn't degraded) and hands 16 kHz int16 blocks of
1280 samples (80 ms) to the rest of Jarvis.
"""
import json
import logging
import os
import queue
import threading

import numpy as np

IO_RATE = 24000
IO_BLOCK = 1920        # 80 ms at 24 kHz
OUT_BLOCK = 1280       # 80 ms at 16 kHz
HIST = IO_RATE * 8     # output history kept for alignment and delay measurement

log = logging.getLogger("friday")


class Downsampler:
    """Stateful 24 kHz -> 16 kHz (x2 up, low-pass, /3 down) so block edges don't click."""

    def __init__(self):
        from scipy.signal import firwin, lfilter_zi
        self.h = firwin(97, 7600, fs=48000)
        self.zi = lfilter_zi(self.h, 1.0) * 0.0

    def __call__(self, x):
        from scipy.signal import lfilter
        up = np.zeros(len(x) * 2, np.float32)
        up[::2] = x * 2
        y, self.zi = lfilter(self.h, 1.0, up, zi=self.zi)
        return y[::3]


class EchoCanceller:
    """Partitioned-block frequency-domain NLMS, block B, P partitions (filter covers B*P samples)."""

    def __init__(self, block=384, partitions=5, mu=0.35):
        self.B, self.P, self.mu = block, partitions, mu
        bins = block + 1
        self.W = np.zeros((partitions, bins), complex)
        self.X = np.zeros((partitions, bins), complex)
        self.xbuf = np.zeros(2 * block)
        self.power = np.full(bins, 1e-4)
        self.erle = 0.0          # smoothed echo reduction in dB while Jarvis talks
        self.converged = False

    def reset(self):
        self.W[:] = 0
        self.converged = False

    def process(self, d, x):
        """d = mic block, x = aligned reference (what was played), both float, length B."""
        B = self.B
        self.xbuf[:B] = self.xbuf[B:]
        self.xbuf[B:] = x
        self.X[1:] = self.X[:-1]
        self.X[0] = np.fft.rfft(self.xbuf)
        y = np.fft.irfft((self.W * self.X).sum(0), 2 * B)[B:]
        e = d - y
        ex, ed, ee = float(np.dot(x, x)), float(np.dot(d, d)), float(np.dot(e, e))
        if ex > 1e-7 * B:  # Jarvis is talking: track how well we cancel and keep learning
            if ed > 1e-9 * B:
                self.erle = 0.9 * self.erle + 0.1 * 10 * np.log10((ed + 1e-12) / (ee + 1e-12))
            self.converged = self.converged or self.erle > 8
            # double-talk guard: once converged, a residual nearly as loud as the mic is him talking,
            # and adapting on his voice would wreck the filter
            if not (self.converged and ee > 0.35 * ed):
                self.power = 0.8 * self.power + 0.2 * (np.abs(self.X) ** 2).mean(0)
                E = np.fft.rfft(np.concatenate([np.zeros(B), e]))
                G = self.mu * np.conj(self.X) * E / (self.power * self.P + 1e-6)
                g = np.fft.irfft(G, 2 * B, axis=1)
                g[:, B:] = 0  # keep the filter causal / linear convolution
                self.W += np.fft.rfft(g, axis=1)
            if ee > 4 * ed + 1e-6:  # diverged: start over rather than make it worse
                self.reset()
                e = d
        return e


class AudioEngine:
    def __init__(self, on_block, state_path=None):
        self.on_block = on_block                       # gets 16 kHz int16 blocks of 1280
        self.state_path = state_path
        self.play_q = queue.Queue()
        self.cur = None                                # (samples float32, position)
        self.out_hist = np.zeros(HIST, np.float32)     # ring buffer of everything played
        self.out_n = 0                                 # total samples played
        self.lock = threading.Lock()
        self.work = queue.Queue()
        self.aec = EchoCanceller()
        self.down = Downsampler()
        self.delay = None                              # echo lag in samples (mic vs output)
        self.calib_mic = []
        self.measured = False                          # re-measured every start, the saved value is a head start
        self.playing = threading.Event()
        self.stream = None
        self._load()

    # --- persistence of the measured echo delay
    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.delay = int(json.load(f)["delay"])
                log.info("echo delay %d samples (saved)", self.delay)
        except Exception:
            pass

    def _save(self):
        if self.state_path:
            try:
                with open(self.state_path, "w", encoding="utf-8") as f:
                    json.dump({"delay": self.delay}, f)
            except OSError:
                pass

    # --- stream
    def start(self):
        import sounddevice as sd
        self.stream = sd.Stream(samplerate=IO_RATE, blocksize=IO_BLOCK, channels=1, dtype="float32",
                                callback=self._callback)
        self.stream.start()
        if self.delay is None:  # until measured, guess from the reported latencies
            lat = self.stream.latency
            self.delay = int(sum(lat) * IO_RATE) if isinstance(lat, (tuple, list)) else IO_BLOCK * 2
        threading.Thread(target=self._worker, daemon=True).start()

    def _callback(self, indata, outdata, frames, t, status):
        out = np.zeros(frames, np.float32)
        filled = 0
        while filled < frames:
            if self.cur is None:
                try:
                    self.cur = [self.play_q.get_nowait(), 0]
                except queue.Empty:
                    break
            buf, pos = self.cur
            take = min(frames - filled, len(buf) - pos)
            out[filled:filled + take] = buf[pos:pos + take]
            filled += take
            pos += take
            if pos >= len(buf):
                self.cur = None
            else:
                self.cur[1] = pos
        if filled == 0 and self.cur is None and self.play_q.empty():
            self.playing.clear()
        outdata[:, 0] = out
        with self.lock:
            i = self.out_n % HIST
            end = i + frames
            if end <= HIST:
                self.out_hist[i:end] = out
            else:
                self.out_hist[i:] = out[:HIST - i]
                self.out_hist[:end - HIST] = out[HIST - i:]
            n0 = self.out_n
            self.out_n += frames
        self.work.put((indata[:, 0].copy(), n0))

    def _ref(self, n0, length):
        """Output samples [n0, n0+length) from the ring buffer (zeros if out of range)."""
        out = np.zeros(length, np.float32)
        if n0 + length <= 0 or self.out_n - n0 > HIST:
            return out
        with self.lock:
            idx = (np.arange(max(n0, 0), n0 + length) % HIST)
            out[max(0, -n0):] = self.out_hist[idx]
        return out

    def _worker(self):
        B = self.aec.B
        while True:
            mic, n0 = self.work.get()
            ref = self._ref(n0 - self.delay + 64, len(mic))  # small pre-delay margin for the filter
            if not self.measured:
                self._collect(mic, n0)
            clean = np.empty_like(mic)
            for k in range(0, len(mic), B):
                clean[k:k + B] = self.aec.process(mic[k:k + B].astype(np.float64), ref[k:k + B].astype(np.float64))
            y = self.down(clean)
            self.on_block((np.clip(y, -1, 1) * 32767).astype(np.int16))

    # --- delay measurement: cross-correlate what was played with what the mic heard
    def _collect(self, mic, n0):
        self.calib_mic.append((n0, mic))
        total = sum(len(m) for _, m in self.calib_mic)
        if total < IO_RATE * 5:
            return
        start = self.calib_mic[0][0]
        mic_all = np.concatenate([m for _, m in self.calib_mic])
        ref_all = self._ref(start - IO_RATE // 2, len(mic_all) + IO_RATE // 2)
        self.calib_mic = []
        if float(np.sqrt(np.mean(ref_all ** 2))) < 0.01:
            return  # Jarvis wasn't talking: nothing to measure against
        n = 1 << int(np.ceil(np.log2(len(ref_all) * 2)))
        corr = np.fft.irfft(np.fft.rfft(mic_all, n) * np.conj(np.fft.rfft(ref_all, n)), n)
        # corr[l] = sum mic[t+l]*ref_all[t]. ref_all starts half a second before the mic, so an echo
        # delay of D samples peaks at l = D - IO_RATE/2; search D in 0..0.5 s
        lags = np.arange(-(IO_RATE // 2), 1)
        vals = corr[lags % n]
        best = int(np.argmax(np.abs(vals)))
        peak, floor = abs(vals[best]), float(np.median(np.abs(vals))) + 1e-9
        if peak / floor > 6:
            self.delay = int(lags[best] + IO_RATE // 2)
            self.measured = True
            self.aec.reset()
            self._save()
            log.info("echo delay measured: %d samples (%.0f ms), peak/median %.0f",
                     self.delay, self.delay / IO_RATE * 1000, peak / floor)

    # --- playback API
    def play(self, samples, rate=IO_RATE):
        """Queues int16 or float audio (24 kHz)."""
        x = samples.astype(np.float32)
        if samples.dtype == np.int16:
            x /= 32768.0
        if rate != IO_RATE:
            from scipy.signal import resample_poly
            x = resample_poly(x, IO_RATE, rate).astype(np.float32)
        self.playing.set()
        self.play_q.put(x)

    def stop(self):
        while True:
            try:
                self.play_q.get_nowait()
            except queue.Empty:
                break
        self.cur = None
        self.playing.clear()

    def busy(self):
        return self.cur is not None or not self.play_q.empty()

    def ref_level(self, seconds=0.45):
        """RMS (int16 scale) of the loudest 50 ms of output in the echo window just past."""
        n = int(seconds * IO_RATE)
        seg = self._ref(self.out_n - n - (self.delay or 0), n)
        step = IO_RATE // 20
        lv = [float(np.sqrt(np.mean(seg[i:i + step] ** 2))) for i in range(0, len(seg) - step + 1, step)]
        return max(lv, default=0.0) * 32768
