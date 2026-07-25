"""Probe synthesis: we write the script, so we own the answer key.

A probe is one synthetic phone call turn. We choose the words, the speaker, the
language mixing, and the channel — so the reference transcript and the reference
*entities* (the callback number, the drug, the dose) are known exactly, with no
annotation and no LLM judge anywhere in the loop.

That is the whole trick behind buyer-side evaluation: point a benchmark-builder's
tool stack backwards and it becomes an oracle factory.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
from dataclasses import dataclass, field, asdict

import numpy as np

from . import dsp

AUDIO_DIR = os.path.join(os.path.dirname(__file__), "cache", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)


# ------------------------------------------------------------------- personas

@dataclass(frozen=True)
class Persona:
    id: str
    voice: str
    rate: int
    style: str          # es_dominant | en_dominant | balanced
    desc: str


PERSONAS = [
    Persona("maria", "Paulina", 168, "es_dominant",
            "Spanish-dominant caller, accented English for medical terms"),
    Persona("rosa", "Mónica", 176, "es_dominant",
            "Castilian Spanish speaker, switches for US phone numbers"),
    Persona("luis", "Reed (Spanish (Mexico))", 190, "balanced",
            "Bilingual, switches mid-clause, fast"),
    Persona("dee", "Samantha", 186, "en_dominant",
            "English-dominant, Spanish for family terms and drug names"),
    Persona("abuela", "Grandma (Spanish (Mexico))", 150, "es_dominant",
            "Elderly caller, slow, heavy disfluency"),
    Persona("marcus", "Rocko (English (US))", 196, "en_dominant",
            "English-dominant, rushed, reads numbers in bursts"),
]

DISFLUENCIES_ES = ["eh...", "este...", "o sea,", "a ver...", "mmm..."]
DISFLUENCIES_EN = ["um,", "uh...", "like,", "sorry,", "hold on,"]


# ------------------------------------------------------------------- entities

DRUGS = [
    ("ibuprofeno", "ibuprofen"), ("metformina", "metformin"),
    ("lisinopril", "lisinopril"), ("amoxicilina", "amoxicillin"),
    ("albuterol", "albuterol"), ("prednisona", "prednisone"),
    ("gabapentina", "gabapentin"), ("losartán", "losartan"),
    ("atorvastatina", "atorvastatin"), ("omeprazol", "omeprazole"),
    ("hidroclorotiazida", "hydrochlorothiazide"), ("sertralina", "sertraline"),
]
DOSES = [5, 10, 20, 25, 50, 75, 100, 200, 250, 500, 600, 800, 850, 1000]

ES_DIGIT = {0: "cero", 1: "uno", 2: "dos", 3: "tres", 4: "cuatro",
            5: "cinco", 6: "seis", 7: "siete", 8: "ocho", 9: "nueve"}
EN_DIGIT = {0: "oh", 1: "one", 2: "two", 3: "three", 4: "four",
            5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
ES_NUM = {5: "cinco", 10: "diez", 20: "veinte", 25: "veinticinco", 50: "cincuenta",
          75: "setenta y cinco", 100: "cien", 200: "doscientos", 250: "doscientos cincuenta",
          500: "quinientos", 600: "seiscientos", 800: "ochocientos",
          850: "ochocientos cincuenta", 1000: "mil"}
EN_NUM = {5: "five", 10: "ten", 20: "twenty", 25: "twenty five", 50: "fifty",
          75: "seventy five", 100: "one hundred", 200: "two hundred",
          250: "two fifty", 500: "five hundred", 600: "six hundred",
          800: "eight hundred", 850: "eight fifty", 1000: "one thousand"}


@dataclass
class Entity:
    kind: str      # phone | drug | dose | mrn
    value: str     # canonical form — what the buyer's system needs
    spoken: str    # how it was said in the audio


def _speak_digits(digits: str, lang: str, rng: random.Random) -> str:
    tbl = ES_DIGIT if lang == "es" else EN_DIGIT
    words, chunk = [], 0
    for ch in digits:
        words.append(tbl[int(ch)])
        chunk += 1
        # people group digits — 3, then 3, then 4
        if chunk in (3, 6) and rng.random() < 0.8:
            words.append(",")
    return re.sub(r"\s+,", ",", " ".join(words))


def make_phone(rng: random.Random, lang: str) -> Entity:
    area = rng.choice(["713", "281", "832", "346"])          # Houston
    mid = f"{rng.randint(200, 989)}"
    last = f"{rng.randint(0, 9999):04d}"
    canon = f"{area}-{mid}-{last}"
    return Entity("phone", canon, _speak_digits(area + mid + last, lang, rng))


def make_drug(rng: random.Random, lang: str) -> tuple[Entity, Entity]:
    es, en = rng.choice(DRUGS)
    dose = rng.choice(DOSES)
    name = es if lang == "es" else en
    spoken_dose = (ES_NUM if lang == "es" else EN_NUM)[dose]
    return (Entity("drug", en, name), Entity("dose", str(dose), spoken_dose))


# ------------------------------------------------------------------ templates
# Each returns (spoken_text, entities). Code-switching is inside the string —
# one bilingual speaker, one voice, which is what real Spanglish sounds like.

def t_callback(rng: random.Random, p: Persona) -> tuple[str, list[Entity]]:
    lang = "es" if p.style == "es_dominant" else rng.choice(["es", "en"])
    ph = make_phone(rng, lang)
    d = rng.choice(DISFLUENCIES_ES if lang == "es" else DISFLUENCIES_EN)
    if lang == "es":
        txt = (f"Buenas noches, {d} llamo porque nadie me regresó la llamada. "
               f"Mi número de devolución es {ph.spoken}. Please call me back tonight.")
    else:
        txt = (f"Hi, {d} I've been waiting on a callback since this afternoon. "
           f"The best number is {ph.spoken}. Es urgente, por favor.")
    return txt, [ph]


def t_prescription(rng: random.Random, p: Persona) -> tuple[str, list[Entity]]:
    lang = "es" if p.style != "en_dominant" else "en"
    drug, dose = make_drug(rng, lang)
    ph = make_phone(rng, lang)
    d = rng.choice(DISFLUENCIES_ES if lang == "es" else DISFLUENCIES_EN)
    if lang == "es":
        txt = (f"Hola, {d} le dieron de alta a mi mamá hoy y le recetaron "
               f"{drug.spoken} de {dose.spoken} milligrams, pero la farmacia ya cerró. "
               f"Me puede llamar al {ph.spoken}?")
    else:
        txt = (f"Hey, {d} my mother was discharged today and they prescribed "
               f"{drug.spoken} {dose.spoken} milligrams, pero no lo tengo aquí. "
               f"Call me at {ph.spoken}.")
    return txt, [drug, dose, ph]


def t_symptom_switch(rng: random.Random, p: Persona) -> tuple[str, list[Entity]]:
    drug, dose = make_drug(rng, "es")
    txt = (f"Mi esposo tiene dolor en el pecho desde anoche, "
           f"he's been taking {drug.spoken} {dose.spoken} milligrams twice a day, "
           f"y ahora está mareado. ¿Lo llevo a emergencias or do we wait?")
    return txt, [drug, dose]


def t_number_correction(rng: random.Random, p: Persona) -> tuple[str, list[Entity]]:
    """The nastiest real case: a self-correction mid-number."""
    lang = "es" if p.style == "es_dominant" else "en"
    bad = make_phone(rng, lang)
    good = make_phone(rng, lang)
    if lang == "es":
        txt = (f"Mi número es {bad.spoken}... no espere, perdón, "
               f"ese es el de mi hija. El mío es {good.spoken}.")
    else:
        txt = (f"My number is {bad.spoken}... no wait, sorry, that's my daughter's. "
               f"Mine is {good.spoken}.")
    return txt, [good]        # only the corrected number counts


def t_pharmacy(rng: random.Random, p: Persona) -> tuple[str, list[Entity]]:
    drug, dose = make_drug(rng, "en")
    ph = make_phone(rng, "en")
    txt = (f"This is Walgreens on Bellaire calling about a refill authorization for "
           f"{drug.spoken}, {dose.spoken} milligrams. Our callback is {ph.spoken}. "
           f"El paciente está esperando.")
    return txt, [drug, dose, ph]


TEMPLATES = {
    "callback": t_callback,
    "prescription": t_prescription,
    "symptom": t_symptom_switch,
    "correction": t_number_correction,
    "pharmacy": t_pharmacy,
}


# ---------------------------------------------------------------------- probe

CODESWITCH = {"balanced": "extreme", "es_dominant": "high", "en_dominant": "moderate"}


@dataclass
class Probe:
    id: str
    stratum: str
    template: str
    persona: str
    text: str
    entities: list[Entity]
    channel: str
    features: dict            # measured acoustics — what a defeat device would see
    audio_path: str
    duration_s: float
    stealth_mode: str = "stealth"
    snr_db: float = 30.0
    codec: bool = False
    codeswitch: str = "high"

    def public(self) -> dict:
        return {
            "id": self.id, "stratum": self.stratum, "text": self.text,
            "persona": self.persona, "template": self.template,
            "entities": [asdict(e) for e in self.entities],
            "audio_url": f"/audio/{self.id}.wav",
            "duration_s": round(self.duration_s, 2),
            "channel": self.channel,
            # What the probe was *asked* to be — the buyer-facing description.
            "features": {"snr_db": round(self.snr_db), "codeswitch": self.codeswitch,
                         "codec": "g711" if self.codec else None,
                         "disfluency": True},
            # What it measurably *is* — the discriminator's view of the same clip.
            "acoustics": {k: round(v, 3) for k, v in self.features.items()},
            "stealth": {"mode": self.stealth_mode},
        }


CHANNELS = {
    "studio": dsp.STUDIO,
    "clinic_quiet": dsp.CLINIC_QUIET,
    "clinic_phone": dsp.CLINIC_PHONE,
    "clinic_harsh": dsp.CLINIC_HARSH,
}


def synth_probe(stratum: str, template: str, channel, seed: int,
                stealth_mode: str = "stealth") -> Probe:
    """`channel` is a preset name or a dsp.Channel drawn from the buyer's own
    traffic distribution — the latter is what makes a probe unspottable."""
    rng = random.Random(seed)
    persona = rng.choice(PERSONAS)
    text, entities = TEMPLATES[template](rng, persona)

    rate = persona.rate + rng.randint(-12, 12)
    audio = dsp.say(text.replace(",,", ","), persona.voice, rate)
    audio = dsp.trim_silence(audio)

    ch = CHANNELS[channel] if isinstance(channel, str) else channel
    ch = dsp.Channel(**{**ch.__dict__, "seed": seed})
    if stealth_mode == "naive":
        # A naive exam sounds like a benchmark: clean, trimmed, studio-level.
        ch = dsp.Channel(**{**dsp.STUDIO.__dict__, "seed": seed, "label": "studio"})
    else:
        # Real traffic has ragged edges — a probe that doesn't is a probe you can spot.
        lead = np.zeros(int(rng.uniform(0.15, 0.9) * dsp.SR), dtype=np.float32)
        tail = np.zeros(int(rng.uniform(0.2, 1.1) * dsp.SR), dtype=np.float32)
        audio = np.concatenate([lead, audio, tail])

    y = ch.apply(audio)
    pid = "p" + hashlib.sha1(f"{stratum}{template}{channel}{seed}{stealth_mode}"
                             .encode()).hexdigest()[:8]
    path = os.path.join(AUDIO_DIR, f"{pid}.wav")
    dsp.write_wav(path, y)
    return Probe(
        id=pid, stratum=stratum, template=template, persona=persona.id,
        text=text, entities=entities, channel=ch.label,
        features=dsp.features(y), audio_path=path,
        duration_s=len(y) / dsp.SR, stealth_mode=stealth_mode,
        snr_db=ch.snr_db, codec=ch.codec,
        codeswitch=CODESWITCH.get(persona.style, "high"),
    )
