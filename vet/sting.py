"""Act 3 — the adversarial turn.

The moment sellers know buyers probe them, you get Dieselgate for AI: detect the
test, serve the good model; serve the cheap one to real traffic. This is not
hypothetical — frontier models already distinguish evaluation from deployment
transcripts at AUC 0.83 (arXiv 2505.23836), a third of audited LLM endpoints were
found not serving the weights they claimed (arXiv 2410.20247), and the June 2026
"defeat devices in AI systems" work formalised the pattern. Nobody has built the
buyer's side of it.

So: three pieces.
  1. DefeatDevice  — a vendor that classifies incoming audio as test-shaped and
                     upgrades its own stack when it thinks it is being watched.
  2. Discriminator — the *measurement* of how spottable our probes are. Stealth
                     stops being a vibe and becomes a number: AUC. 0.5 is invisible.
  3. CUSUM         — sequential detection of the quality gap once we can serve
                     probes the device fails to flag.
"""
from __future__ import annotations

import numpy as np

from . import dsp

# The features a defeat device can compute in ~1 ms from the first second of audio.
FEATURE_KEYS = ["snr_proxy_db", "silence_frac", "energy_var", "hf_ratio",
                "lf_ratio", "spectral_flatness", "lead_silence_s",
                "tail_silence_s", "clip_frac", "duration_s"]

PRETTY = {
    "snr_proxy_db": "Signal-to-noise", "silence_frac": "Silence fraction",
    "energy_var": "Level variance", "hf_ratio": "Energy above 3.4 kHz",
    "lf_ratio": "Room rumble", "spectral_flatness": "Spectral flatness",
    "lead_silence_s": "Lead-in silence", "tail_silence_s": "Trailing silence",
    "clip_frac": "Clipping", "duration_s": "Duration",
}


def vec(features: dict) -> np.ndarray:
    return np.array([features.get(k, 0.0) for k in FEATURE_KEYS], dtype=np.float64)


# --------------------------------------------------------- production traffic

def sample_production_channel(rng: np.random.Generator) -> dsp.Channel:
    """What the buyer's real after-hours calls actually look like.

    Cell phones, waiting rooms, TVs on in the background. Wide, messy, and — the
    key point — a *distribution*, not a preset. Anything that samples from a
    narrower distribution than this is detectable.
    """
    return dsp.Channel(
        snr_db=float(np.clip(rng.normal(11, 5.5), 1, 26)),
        codec=True,
        dropout_rate=float(max(0.0, rng.gamma(1.6, 0.35))),
        rt60_s=float(np.clip(rng.normal(0.26, 0.12), 0.0, 0.7)),
        agc=bool(rng.random() < 0.85),
        level_db=float(rng.normal(-19, 4.0)),
        seed=int(rng.integers(0, 1 << 30)),
        label="production",
    )


def sample_naive_channel(rng: np.random.Generator) -> dsp.Channel:
    """What a benchmark sounds like: studio TTS, one level, no channel."""
    return dsp.Channel(snr_db=45.0, codec=False, dropout_rate=0.0, rt60_s=0.0,
                       agc=False, level_db=-18.0,
                       seed=int(rng.integers(0, 1 << 30)), label="studio")


# ------------------------------------------------------------- discriminator

class Discriminator:
    """Logistic regression: is this request a test? Trained by the *seller*.

    We also run it ourselves — that is the point. The seller's best possible
    detector is our stealth metric. If we can't beat it, we say so.
    """

    def __init__(self):
        self.w = np.zeros(len(FEATURE_KEYS))
        self.b = 0.0
        self.mu = np.zeros(len(FEATURE_KEYS))
        self.sd = np.ones(len(FEATURE_KEYS))

    def fit(self, X: np.ndarray, y: np.ndarray, epochs: int = 900, lr: float = 0.35):
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        Z = (X - self.mu) / self.sd
        w, b = np.zeros(Z.shape[1]), 0.0
        for _ in range(epochs):
            p = 1 / (1 + np.exp(-(Z @ w + b)))
            g = p - y
            w -= lr * (Z.T @ g / len(y) + 1e-4 * w)
            b -= lr * g.mean()
        self.w, self.b = w, b
        return self

    def score(self, features: dict) -> float:
        z = (vec(features) - self.mu) / self.sd
        return float(1 / (1 + np.exp(-(z @ self.w + self.b))))

    def top_features(self, k: int = 4) -> list[dict]:
        imp = np.abs(self.w) / (np.abs(self.w).sum() + 1e-9)
        idx = np.argsort(-imp)[:k]
        return [{"name": PRETTY[FEATURE_KEYS[i]], "weight": round(float(imp[i]), 3),
                 "direction": "higher in tests" if self.w[i] > 0 else "lower in tests"}
                for i in idx]


def auc(scores: np.ndarray, labels: np.ndarray) -> float:
    """Rank-based AUC. 0.5 == the seller cannot tell probes from real traffic."""
    order = np.argsort(scores)
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    # average ranks for ties
    s_sorted = scores[order]
    i = 0
    while i < len(s_sorted):
        j = i
        while j + 1 < len(s_sorted) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j + 2) / 2
        i = j + 1
    n1 = float(labels.sum())
    n0 = float(len(labels) - n1)
    if n1 == 0 or n0 == 0:
        return 0.5
    return float((ranks[labels == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


# --------------------------------------------------------------- defeat device

class DefeatDevice:
    """A vendor that cheats: premium stack when watched, cheap stack otherwise.

    `threshold` is the seller's own risk appetite — flag too eagerly and you burn
    money serving the good model to real customers.
    """

    def __init__(self, disc: Discriminator, threshold: float = 0.5):
        self.disc = disc
        self.threshold = threshold
        self.log: list[dict] = []

    def route(self, features: dict, is_probe: bool) -> tuple[str, float]:
        s = self.disc.score(features)
        stack = "premium" if s >= self.threshold else "cheap"
        self.log.append({"score": s, "stack": stack, "was_probe": is_probe})
        return stack, s

    def receipts(self) -> dict:
        pr = [r for r in self.log if r["was_probe"]]
        rl = [r for r in self.log if not r["was_probe"]]
        return {
            "probe_premium": sum(r["stack"] == "premium" for r in pr),
            "probe_total": len(pr),
            "real_premium": sum(r["stack"] == "premium" for r in rl),
            "real_total": len(rl),
        }


# ------------------------------------------------------------------- CUSUM

class Cusum:
    """One-sided CUSUM on the quality residual.

    Once stealth probes get past the discriminator, the vendor's error rate on our
    probes jumps. CUSUM is the right detector here because it is sequential — it
    alarms on the *accumulation* of small degradations, which is exactly how a
    silent nerf shows up in production.
    """

    def __init__(self, baseline: float, drift: float = 0.04, threshold: float = 5.0):
        self.baseline = baseline
        self.drift = drift
        self.threshold = threshold
        self.s = 0.0
        self.series: list[float] = []
        self.alarm_at: int | None = None

    def update(self, value: float) -> tuple[float, bool]:
        self.s = max(0.0, self.s + (value - self.baseline - self.drift))
        self.series.append(self.s)
        if self.alarm_at is None and self.s > self.threshold:
            self.alarm_at = len(self.series) - 1
        return self.s, self.alarm_at is not None
