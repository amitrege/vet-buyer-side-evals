"""Audio synthesis + degradation.

Everything here runs locally on CPU: macOS `say` for speech, numpy/scipy for the
channel simulation. The point is that we author the script, so the transcript is
ground truth *by construction* — no labelling, no judge, no oracle problem.

The degradation chain is also the stealth knob. A probe that has been through
babble + G.711 + jitter is statistically much harder to tell apart from a real
call than a clean TTS render, which is exactly what Act 3 measures.
"""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass

import numpy as np
from scipy import signal

SR = 16_000
CACHE = os.path.join(os.path.dirname(__file__), "cache", "tts")
os.makedirs(CACHE, exist_ok=True)


# ---------------------------------------------------------------- wav helpers

def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2, f"expected 16-bit, got {w.getsampwidth()*8}"
        raw = w.readframes(w.getnframes())
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if w.getnchannels() == 2:
            x = x.reshape(-1, 2).mean(axis=1)
        if w.getframerate() != SR:
            x = resample(x, w.getframerate(), SR)
    return x


def write_wav(path: str, x: np.ndarray, sr: int = SR) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    y = np.clip(x, -1.0, 1.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767.0).astype("<i2").tobytes())


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return x
    g = math.gcd(sr_in, sr_out)
    return signal.resample_poly(x, sr_out // g, sr_in // g).astype(np.float32)


def pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767.0).astype("<i2").tobytes()


# ---------------------------------------------------------------------- speech

def say(text: str, voice: str, rate: int = 180) -> np.ndarray:
    """macOS TTS -> mono float32 @16k. Cached on (text, voice, rate)."""
    key = hashlib.sha1(f"{voice}|{rate}|{text}".encode()).hexdigest()[:16]
    wav_path = os.path.join(CACHE, f"{key}.wav")
    if not os.path.exists(wav_path):
        with tempfile.TemporaryDirectory() as td:
            aiff = os.path.join(td, "a.aiff")
            subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", aiff, text],
                           check=True, capture_output=True)
            subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{SR}", "-c", "1",
                            aiff, wav_path], check=True, capture_output=True)
    return read_wav(wav_path)


def trim_silence(x: np.ndarray, thresh: float = 2e-3) -> np.ndarray:
    idx = np.where(np.abs(x) > thresh)[0]
    if len(idx) == 0:
        return x
    pad = int(0.02 * SR)
    return x[max(0, idx[0] - pad): idx[-1] + pad]


# ------------------------------------------------------------------ the channel

def _babble(n: int, rng: np.random.Generator) -> np.ndarray:
    """Call-centre background: several detuned speech-shaped streams + room hum.

    Speech-shaped noise = white noise filtered to the long-term average speech
    spectrum, amplitude-modulated at syllable rate (~4 Hz). Summing a handful of
    those with different rates gives convincing multi-talker babble.
    """
    out = np.zeros(n, dtype=np.float32)
    for _ in range(6):
        w = rng.normal(0, 1, n).astype(np.float32)
        b, a = signal.butter(2, [200 / (SR / 2), 3800 / (SR / 2)], btype="band")
        v = signal.lfilter(b, a, w).astype(np.float32)
        # syllabic envelope
        f = rng.uniform(2.5, 5.5)
        ph = rng.uniform(0, 2 * np.pi)
        # Shallow modulation: deep syllabic dips make this a *competing talker*,
        # which masks far more aggressively than real room babble.
        env = 0.72 + 0.28 * np.sin(2 * np.pi * f * np.arange(n) / SR + ph)
        out += (v * env).astype(np.float32)
    hum = 0.06 * np.sin(2 * np.pi * 60 * np.arange(n) / SR).astype(np.float32)
    out = out / (np.std(out) + 1e-9) + hum
    return out.astype(np.float32)


def add_noise(x: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    n = _babble(len(x), rng)
    sp = np.mean(x ** 2) + 1e-12
    npow = np.mean(n ** 2) + 1e-12
    scale = math.sqrt(sp / (npow * (10 ** (snr_db / 10.0))))
    return (x + scale * n).astype(np.float32)


def _mulaw_roundtrip(x: np.ndarray) -> np.ndarray:
    """G.711 μ-law quantisation — 8 bits, the actual codec on a phone call."""
    mu = 255.0
    y = np.sign(x) * np.log1p(mu * np.abs(np.clip(x, -1, 1))) / np.log1p(mu)
    q = np.round((y + 1) * 127.5) / 127.5 - 1
    return (np.sign(q) * (1 / mu) * ((1 + mu) ** np.abs(q) - 1)).astype(np.float32)


def phone_codec(x: np.ndarray) -> np.ndarray:
    """Narrowband telephony: 300–3400 Hz, 8 kHz sample rate, μ-law."""
    b, a = signal.butter(4, [300 / (SR / 2), 3400 / (SR / 2)], btype="band")
    y = signal.lfilter(b, a, x).astype(np.float32)
    y = resample(y, SR, 8000)
    y = _mulaw_roundtrip(y)
    return resample(y, 8000, SR)


def dropouts(x: np.ndarray, rate_per_s: float, rng: np.random.Generator) -> np.ndarray:
    """Cellular packet loss: 20–120 ms holes."""
    y = x.copy()
    n_drop = rng.poisson(rate_per_s * len(x) / SR)
    for _ in range(int(n_drop)):
        d = int(rng.uniform(0.02, 0.12) * SR)
        s = rng.integers(0, max(1, len(y) - d))
        y[s:s + d] *= rng.uniform(0.0, 0.15)
    return y


def reverb(x: np.ndarray, rt60_s: float, rng: np.random.Generator) -> np.ndarray:
    n = int(rt60_s * SR)
    if n < 8:
        return x
    ir = rng.normal(0, 1, n) * np.exp(-6.9 * np.arange(n) / n)
    ir[0] = 1.0
    ir /= np.linalg.norm(ir)
    return signal.fftconvolve(x, ir)[: len(x)].astype(np.float32)


def agc_pump(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Cheap handset AGC: slow gain rides + occasional clipping."""
    n = len(x)
    ctrl = signal.lfilter(*signal.butter(1, 0.7 / (SR / 2)), rng.normal(0, 1, n))
    g = 1.0 + 0.35 * ctrl / (np.std(ctrl) + 1e-9)
    return np.clip(x * g.astype(np.float32), -0.98, 0.98)


@dataclass
class Channel:
    """One realisation of 'what this call sounded like when it arrived'."""
    snr_db: float = 30.0
    codec: bool = False
    dropout_rate: float = 0.0
    rt60_s: float = 0.0
    agc: bool = False
    level_db: float = -18.0
    seed: int = 0
    label: str = "studio"

    def apply(self, x: np.ndarray) -> np.ndarray:
        """Gain staging matters more than any single effect here.

        Level the *speech* before mixing noise in. Normalising the mixture
        instead (the obvious mistake) pushes speech down by exactly the SNR, so
        a '9 dB SNR' clip arrives 9 dB quieter than intended and then loses most
        of its resolution to the 8-bit codec. That compounding is what turns a
        merely-noisy call into an unintelligible one.
        """
        rng = np.random.default_rng(self.seed)
        y = x.astype(np.float32)
        if self.rt60_s > 0:
            y = reverb(y, self.rt60_s, rng)

        rms = float(np.sqrt(np.mean(y ** 2)) + 1e-9)
        y *= (10 ** (self.level_db / 20.0)) / rms       # speech sits at level_db

        if self.snr_db < 40:
            y = add_noise(y, self.snr_db, rng)          # noise relative to speech
        if self.agc:
            y = agc_pump(y, rng)
        if self.codec:
            y = phone_codec(y)
        if self.dropout_rate > 0:
            y = dropouts(y, self.dropout_rate, rng)

        peak = float(np.max(np.abs(y)) + 1e-9)          # peak-limit only
        if peak > 0.97:
            y *= 0.97 / peak
        return np.clip(y, -1.0, 1.0).astype(np.float32)


# Named channel presets. STUDIO is what a naive benchmark sounds like; the others
# are what the buyer's actual traffic sounds like.
STUDIO = Channel(snr_db=45, level_db=-18, label="studio")
CLINIC_QUIET = Channel(snr_db=30, codec=True, rt60_s=0.10, agc=True,
                       level_db=-16, label="clinic_quiet")
CLINIC_PHONE = Channel(snr_db=22, codec=True, dropout_rate=0.05, rt60_s=0.18,
                       agc=True, level_db=-16, label="clinic_phone")
CLINIC_HARSH = Channel(snr_db=18, codec=True, dropout_rate=0.05, rt60_s=0.22,
                       agc=True, level_db=-16, label="clinic_harsh")


# --------------------------------------------------------- features (for Act 3)

def features(x: np.ndarray) -> dict[str, float]:
    """The signals a defeat device would actually use to spot a test.

    These are all cheap, all computable from the first second of audio, and all
    are things a lazy benchmark leaks: too clean, too even, too well-trimmed.
    """
    eps = 1e-12
    frame = int(0.025 * SR)
    hop = int(0.010 * SR)
    n_f = max(1, (len(x) - frame) // hop)
    energies = np.array([np.sum(x[i * hop: i * hop + frame] ** 2) for i in range(n_f)]) + eps
    e_db = 10 * np.log10(energies)
    thresh = np.percentile(e_db, 25) + 6.0
    speech = e_db > thresh

    f, pxx = signal.welch(x, SR, nperseg=min(1024, len(x)))
    hi = float(np.sum(pxx[f > 3600]) / (np.sum(pxx) + eps))     # codec kills >3.4k
    lo = float(np.sum(pxx[f < 250]) / (np.sum(pxx) + eps))      # room rumble
    flat = float(np.exp(np.mean(np.log(pxx + eps))) / (np.mean(pxx) + eps))

    lead = float(np.argmax(np.abs(x) > 0.01)) / SR if np.any(np.abs(x) > 0.01) else 0.0
    tail = 0.0
    nz = np.where(np.abs(x) > 0.01)[0]
    if len(nz):
        tail = (len(x) - nz[-1]) / SR
    return {
        "snr_proxy_db": float(np.mean(e_db[speech]) - np.mean(e_db[~speech]))
        if speech.any() and (~speech).any() else 40.0,
        "silence_frac": float(np.mean(~speech)),
        "energy_var": float(np.var(e_db)),
        "hf_ratio": hi,
        "lf_ratio": lo,
        "spectral_flatness": flat,
        "lead_silence_s": lead,
        "tail_silence_s": tail,
        "clip_frac": float(np.mean(np.abs(x) > 0.97)),
        "duration_s": len(x) / SR,
    }
