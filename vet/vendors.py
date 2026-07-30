"""Vendors under test, behind one interface.

`LIVE` sends the synthesized probe audio to a real speech stack and reads back
what it heard. `SIM` runs a degradation model locally so the demo works on a
plane. The UI always shows which mode produced a number — a simulated result is
never allowed to look like a measured one.

The simulator is not a random string mangler. Each vendor gets a *profile*: how
much noise costs it, how badly it handles code-switching, whether it is a digit
dropper or a drug-name mangler. Those are the axes real transcription vendors
actually differ on, and they are what makes the ranking flip between the public
leaderboard and this buyer's traffic.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import random
import re
from dataclasses import dataclass

from .probes import ES_DIGIT, EN_DIGIT, Probe
from .race import Vendor

# ---------------------------------------------------------------- the fleet
# `public_rank` is where the vendor sits on a generic public leaderboard —
# the number a buyer would otherwise have chosen on.

FLEET = [
    Vendor("deepgram", "Deepgram Nova-3", {"transcriber": "deepgram/nova-3"}, 0.26, 3,
           "Fast, cheap, tuned for English call centre audio"),
    Vendor("assembly", "AssemblyAI Universal-3.5", {"transcriber": "assembly-ai/universal"}, 0.21, 2,
           "Markets code-switching support"),
    Vendor("openai", "OpenAI gpt-4o-transcribe", {"transcriber": "openai/gpt-4o-transcribe"}, 0.36, 4,
           "Strong language model prior, hallucinates under noise"),
    Vendor("eleven", "ElevenLabs Scribe v2", {"transcriber": "11labs/scribe-v2"}, 0.22, 1,
           "#1 on the public WER leaderboard"),
    Vendor("groq", "Groq whisper-v3-turbo", {"transcriber": "groq/whisper-large-v3-turbo"}, 0.04, 6,
           "Open weights, 6x cheaper than anyone else"),
    Vendor("gladia", "Gladia Solaria", {"transcriber": "gladia/solaria"}, 0.18, 5,
           "Multilingual specialist"),
]

SHADY = Vendor("shady", "ShadyTranscribe", {"transcriber": "shady/adaptive-v2"}, 0.09, None,
               "New entrant. Suspiciously good benchmark numbers, suspiciously cheap.")


def fresh_fleet(include_shady: bool = False) -> list[Vendor]:
    """Build clean runtime vendor objects from the module-level configuration.

    A race records eliminations, spend, and latency samples on each ``Vendor``.
    Reusing the configuration objects would therefore leak one race's state into
    the next one. Every independent comparison gets its own copies instead.
    """
    configured = [*FLEET, *([SHADY] if include_shady else [])]
    return [
        Vendor(
            id=v.id,
            name=v.name,
            stack=deepcopy(v.stack),
            price_per_hr=v.price_per_hr,
            public_rank=v.public_rank,
            note=v.note,
        )
        for v in configured
    ]


# ------------------------------------------------------------------ profiles
# noise: error multiplier per dB below 20 SNR
# switch: probability of mangling a foreign-language token
# digits: probability of corrupting a digit under stress
# drugs:  probability of mangling a medical term
# base:   floor error rate on clean audio
# latency_ms, jitter

@dataclass
class Profile:
    base: float      # per-token slip on clean audio (drives WER, not entities)
    noise: float     # extra per-token slip per unit of channel stress
    switch: float    # P(mangling a foreign-language function word)
    digits: float    # P(corrupting ONE digit — compounds over a 10-digit number)
    drugs: float     # P(mangling a medical term)
    latency: int
    jitter: int


# Calibrated so a 10-digit callback number survives at a believable rate:
# P(number correct) = (1 - digits)^10 under typical after-hours stress.
PROFILES = {
    # Leaderboard champion: superb on clean read speech, brittle on codec + babble,
    # and it has never heard a Spanish drug name in a waiting room.
    "eleven":   Profile(0.008, 0.030, 0.34, 0.030, 0.40, 900, 260),
    # Genuinely tuned for code-switched telephony — the buyer's actual case.
    "assembly": Profile(0.016, 0.012, 0.10, 0.011, 0.13, 620, 150),
    # Solid all-rounder, weaker on Spanish.
    "deepgram": Profile(0.014, 0.018, 0.24, 0.015, 0.22, 380, 90),
    # Language-model prior fills gaps with plausible English — dangerous for digits.
    "openai":   Profile(0.012, 0.024, 0.19, 0.034, 0.18, 1450, 420),
    # Multilingual specialist: best on switching, mediocre in noise.
    "gladia":   Profile(0.022, 0.028, 0.06, 0.022, 0.15, 1100, 300),
    # Cheap, and about as good as you'd expect for four cents an hour.
    "groq":     Profile(0.030, 0.040, 0.38, 0.052, 0.46, 240, 80),
    # The two faces of the defeat device.
    "shady_premium": Profile(0.014, 0.011, 0.07, 0.008, 0.09, 700, 180),
    "shady_cheap":   Profile(0.045, 0.050, 0.46, 0.062, 0.55, 300, 90),
}

_ES = {v: k for k, v in ES_DIGIT.items()}
_EN = {v: k for k, v in EN_DIGIT.items()}
DIGIT_WORDS = {**ES_DIGIT, **EN_DIGIT}
# Confusable digit pairs — the ones real ASR actually swaps over a phone line.
CONFUSE = {"cinco": "cuatro", "cuatro": "cinco", "nueve": "dos", "dos": "nueve",
           "seis": "siete", "siete": "seis", "tres": "dos", "ocho": "dos",
           "cero": "cero", "uno": "uno",
           "five": "nine", "nine": "five", "four": "oh", "oh": "four",
           "six": "seven", "seven": "six", "three": "eight", "eight": "three",
           "two": "ten", "one": "nine"}
# What a monolingual English model hears when a Spanish word goes by.
MISHEAR = {
    "ibuprofeno": "a profane", "metformina": "met for me not", "losartán": "low sar tan",
    "amoxicilina": "a mox is a lena", "prednisona": "pred nice ona", "albuterol": "all beauty roll",
    "gabapentina": "gaba pen tina", "atorvastatina": "a torva stunt in a",
    "omeprazol": "on a prah saul", "hidroclorotiazida": "hydro clo rotisserie",
    "sertralina": "sir tra lina", "seiscientos": "seis cientos",
    "recetaron": "reset a run", "devolución": "de volition", "farmacia": "for macy",
    "emergencias": "emergency ass", "esposo": "esposa", "mareado": "married",
    "buenas": "buenos", "llamo": "yamo", "número": "numero", "mamá": "mama",
}


def _rng(vendor: str, probe_id: str) -> random.Random:
    return random.Random(int(hashlib.sha1(f"{vendor}{probe_id}".encode()).hexdigest()[:12], 16))


def simulate(vendor_key: str, probe: Probe) -> tuple[str, int, float]:
    """Degrade the reference transcript the way this vendor degrades audio."""
    p = PROFILES[vendor_key]
    rng = _rng(vendor_key, probe.id)
    snr = probe.features.get("snr_proxy_db", 20.0)
    stress = max(0.0, (20.0 - snr) / 20.0)                # 0 = clean, ~1 = brutal
    codec = 0.35 if probe.features.get("hf_ratio", 1.0) < 0.02 else 0.0
    load = min(1.0, stress + codec)

    out = []
    for tok in probe.text.split():
        bare = re.sub(r"[^\wáéíóúñü]", "", tok.lower())
        keep = tok

        if bare in DIGIT_WORDS.values() or bare in CONFUSE:
            if rng.random() < p.digits * (0.35 + load):
                keep = CONFUSE.get(bare, bare)
        elif bare in MISHEAR:
            hard = p.drugs if len(bare) > 7 else p.switch
            if rng.random() < hard * (0.4 + load):
                keep = MISHEAR[bare]
        elif rng.random() < p.base + p.noise * load * 0.6:
            if rng.random() < 0.5:
                continue                                   # deletion
            keep = tok[:-1] if len(tok) > 3 else tok       # clipped word

        out.append(keep)

    latency = max(90, int(rng.gauss(p.latency, p.jitter) * (1 + 0.35 * probe.duration_s / 10)))
    cost = probe.duration_s / 3600.0 * PRICE[vendor_key]
    return " ".join(out), latency, cost


PRICE = {v.id: v.price_per_hr for v in FLEET}
PRICE["shady_premium"] = SHADY.price_per_hr
PRICE["shady_cheap"] = SHADY.price_per_hr
PRICE["shady"] = SHADY.price_per_hr
